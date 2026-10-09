"""Shadow tracking: what the trades the bot did NOT take would have done.

Every evaluation that had a proposed order but wasn't placed (WAIT, SKIP, blocked by Claude, max positions, dismissed)
is followed for up to 20 trading days after the evaluation, using daily option bars (Alpaca) and daily stock bars, and
the bot's own exit rules are applied to it: option stop, invalidation close, option target, stock target, time stop,
earnings exit. Runs once a day after the close. Daily bars make it an approximation (intraday order of a stop and a
target on the same day isn't known: the stop is assumed first, the conservative side).

Stored on the evaluation as `shadow`: status tracking | done | dup | nodata, entry, last, plPct, R, exitReason,
exitDate, mfePct / maePct (best / worst option move), undMax / undMin, days.
"""
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import db
from util import iso, now_ny

DAYS = 20
SIM_VERSION = 3          # 3: stores the daily path; evaluation-day closes only; the target starts the trailing stop
TAKEN = ("submitting", "submitted", "open", "closing", "closed")
DATA = "https://data.alpaca.markets/v1beta1/options/bars"


def _creds(sub):
    import alpaca
    c = alpaca.creds(sub)
    return c


def option_bars(c, symbols, start, errors=None):
    """Daily bars per option symbol since `start` (YYYY-MM-DD). {symbol: [{t,o,h,l,c,v}]}
    Requests end 20 minutes ago (free market-data plans can't query the most recent data). A failing batch is
    retried symbol by symbol so one bad contract doesn't stop the others; failures are appended to `errors`."""
    import urllib.error
    out = {}
    errors = errors if errors is not None else []
    end = (datetime.utcnow() - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def fetch(batch):
        token = None
        for _ in range(20):
            q = {"symbols": ",".join(batch), "timeframe": "1Day", "start": start, "end": end, "limit": 10000}
            if token:
                q["page_token"] = token
            req = urllib.request.Request(f"{DATA}?{urllib.parse.urlencode(q)}",
                                         headers={"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"]})
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read())
            for sym, bars in (d.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            token = d.get("next_page_token")
            if not token:
                return

    def why(e):
        if isinstance(e, urllib.error.HTTPError):
            return f"{e.code} {e.read().decode(errors='replace')[:140]}"
        return str(e)[:140]

    syms = sorted(set(s for s in symbols if s))
    for i in range(0, len(syms), 50):
        batch = syms[i:i + 50]
        try:
            fetch(batch)
        except Exception as e:
            first = why(e)
            if len(batch) == 1:
                errors.append(f"{batch[0]}: {first}")
                continue
            for one in batch:
                try:
                    fetch([one])
                except Exception as e2:
                    errors.append(f"{one}: {why(e2)}")
    return out


def _day(t):
    return str(t)[:10]


def _taken(r):
    return r.get("orderId") or r.get("status") in TAKEN


def candidates(recs, today):
    """Evaluations to follow: a proposed contract, not placed, from the last ~5 weeks, not finished yet.
    The same contract evaluated several times the same day is followed once (the first evaluation)."""
    cut = (today - timedelta(days=35)).isoformat()
    seen, todo, dups = {}, [], []
    for r in sorted(recs, key=lambda x: x.get("createdAt") or ""):
        p = r.get("proposal") or {}
        if _taken(r) or not p.get("contract") or (r.get("createdAt") or "") < cut:
            continue
        key = (r.get("symbol"), p["contract"], p.get("shortContract"), (r.get("createdAt") or "")[:10])
        if key in seen:
            if (r.get("shadow") or {}).get("status") != "dup":
                dups.append((r, seen[key]))
            continue
        seen[key] = r["id"]
        sh = r.get("shadow") or {}
        if sh.get("status") in ("done", "nodata") and sh.get("v") == SIM_VERSION:
            continue                          # finished with the current rules (older results are redone once)
        todo.append(r)
    return todo, dups


def build_path(rec, obars, ubars):
    """Daily path after the evaluation: [[date, opt_high, opt_low, opt_close, stock_high, stock_low, stock_close], ...].
    The evaluation day counts with its closes only (its high/low may be from before the evaluation). Spreads: closes only."""
    p = rec["proposal"]
    d0 = (rec.get("createdAt") or "")[:10]
    spread = bool(p.get("shortContract"))
    longb = {_day(b["t"]): b for b in obars.get(p["contract"], [])}
    shortb = {_day(b["t"]): b for b in obars.get(p.get("shortContract"), [])} if spread else {}
    ub = {_day(b["t"]): b for b in ubars}
    days = sorted(d for d in longb if d > d0 and (not spread or d in shortb))
    if "09:30" <= (rec.get("createdAt") or "")[11:16] < "16:00" and d0 in longb and (not spread or d0 in shortb):
        days = [d0] + days
    out = []
    for d in days[:DAYS]:
        b = longb[d]
        if spread:
            cl = float(b["c"]) - float(shortb[d]["c"])
            hi = lo = cl
        else:
            hi, lo, cl = float(b["h"]), float(b["l"]), float(b["c"])
        u = ub.get(d)
        uh, ul, uc = (float(u["h"]), float(u["l"]), float(u["c"])) if u else (None, None, None)
        if d == d0:
            hi = lo = cl
            if u:
                uh = ul = uc
        r4 = lambda v: round(v, 4) if v is not None else None
        out.append([d, r4(hi), r4(lo), r4(cl), r4(uh), r4(ul), r4(uc)])
    return out


def sim_path(path, entry, prop, params):
    """Apply one set of exit rules to a stored path. params: stop (% loss), target (% gain or None), trail (% below the
    best after the target, or None = sell at the target), maxDays (or None), invalidation (bool), stockTarget (bool),
    timeStopDte, earnings (date or None). Returns {R, plPct, exitReason, exitDate, exitPrice, days, done}."""
    stp, tgt, trail = params["stop"], params.get("target"), params.get("trail")
    hs = params.get("halfSpread") or 0.0      # half the bid/ask spread: paid on the way in, given up on the way out
    raw_entry = entry
    entry = raw_entry * (1 + hs)               # what you actually pay (toward the ask)
    stop_px = entry * (1 - stp / 100)          # levels are on what you'd get (the bid)
    tgt_px = entry * (1 + tgt / 100) if tgt else None
    trailing, peak, note = False, 0.0, None
    exit_px = reason = exit_day = None
    last, n = None, 0
    for n, (d, hi, lo, cl, uh, ul, uc) in enumerate(path, 1):
        hi, lo, cl = hi * (1 - hs), lo * (1 - hs), cl * (1 - hs)      # sell side: the bid
        last = cl
        if trailing:
            peak = max(peak, hi)
        dte = (datetime.strptime(prop["exp"], "%Y-%m-%d").date() - datetime.strptime(d, "%Y-%m-%d").date()).days if prop.get("exp") else 99
        if trailing and lo <= peak * (1 - trail / 100):
            exit_px, reason = max(peak * (1 - trail / 100), lo), f"{note}, then trailing stop {trail:g}%"
        elif lo <= stop_px:
            exit_px, reason = stop_px, f"option stop (-{stp:g}%)"
        elif params.get("invalidation", True) and uc is not None and prop.get("underlyingStop") and uc < prop["underlyingStop"]:
            exit_px, reason = cl, f"closed below invalidation {prop['underlyingStop']}"
        elif not trailing and ((tgt_px and hi >= tgt_px) or (params.get("stockTarget", True) and uh is not None
                                                                and prop.get("underlyingTarget") and uh >= prop["underlyingTarget"])):
            what = f"option target (+{tgt:g}%)" if tgt_px and hi >= tgt_px else f"stock reached target {prop['underlyingTarget']}"
            if trail:
                trailing, peak, note = True, max(hi, cl), what
            else:
                exit_px, reason = (tgt_px if tgt_px and hi >= tgt_px else cl), what
        if exit_px is None:
            if dte <= params.get("timeStopDte", 14):
                exit_px, reason = cl, f"time stop ({dte} days left)"
            elif params.get("maxDays") and n >= params["maxDays"]:
                exit_px, reason = cl, f"held {n} days"
            elif params.get("earnings") and d >= (datetime.strptime(params["earnings"], "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"):
                exit_px, reason = cl, "exit before earnings"
        if exit_px is not None:
            exit_day = d
            break
    done = exit_px is not None or len(path) >= DAYS
    if exit_px is None and done:
        exit_px, reason, exit_day = last, f"{DAYS} trading days, still open", path[-1][0]
    px = exit_px if exit_px is not None else last
    pl = (px / entry - 1) * 100 if px is not None else 0.0
    return {"R": round(pl / stp, 2), "plPct": round(pl, 1), "exitReason": reason, "exitDate": exit_day,
            "exitPrice": round(exit_px, 2) if exit_px is not None else None, "days": n, "done": done, "trailing": trailing and exit_px is None}


def params_of(cfg, prop, entry, earnings=None):
    """Exit rules in force for this idea: a strategy's exit plan if it has one, otherwise the bot settings."""
    import autotrader
    plan = prop.get("exitPlan") or {}
    stp = plan.get("stop") or autotrader.stop_pct(cfg, prop)
    tgt = plan["target"] if "target" in plan else autotrader.target_pct(cfg, prop, entry)
    trail = plan["trail"] if "trail" in plan else (cfg.get("trailPct", 25) if cfg.get("trailAfterTarget") else None)
    return {"stop": stp, "target": tgt, "trail": trail, "maxDays": plan.get("maxDays"),
            "invalidation": plan.get("invalidation", True), "stockTarget": plan.get("stockTarget", True),
            "timeStopDte": cfg["timeStopDte"], "earnings": earnings}


def simulate(rec, obars, ubars, cfg, earnings=None):
    """Apply the bot's exit rules to the daily path after the evaluation day. Returns the shadow dict (with the path,
    so other exit rules can be replayed later without fetching prices again)."""
    p = rec["proposal"]
    entry = float(p.get("limit") or p.get("debit") or 0)
    if entry <= 0:
        return {"status": "nodata", "note": "no entry price", "updatedAt": iso(now_ny()), "v": SIM_VERSION}
    d0 = (rec.get("createdAt") or "")[:10]
    path = build_path(rec, obars, ubars)
    prm = params_of(cfg, p, entry, earnings)
    out = {"entry": round(entry, 2), "stopPct": prm["stop"], "targetPct": prm["target"], "approx": True,
           "updatedAt": iso(now_ny()), "v": SIM_VERSION, "path": path}
    if not path:
        stale = (now_ny().date() - datetime.strptime(d0, "%Y-%m-%d").date()).days > 8 if d0 else True
        return {**out, "status": "nodata" if stale else "tracking", "days": 0,
                "note": "no trading day since the evaluation yet" if not stale else "no trades in this contract"}
    r = sim_path(path, entry, p, prm)
    hi_all, lo_all = max(x[1] for x in path), min(x[2] for x in path)
    uh = [x[4] for x in path if x[4] is not None]
    ul = [x[5] for x in path if x[5] is not None]
    out.update({
        "status": "done" if r["done"] else "tracking", "days": r["days"], "last": round(path[-1][3], 2), "plPct": r["plPct"],
        "R": r["R"], "exitReason": r["reason"] if "reason" in r else r["exitReason"], "exitDate": r["exitDate"],
        "mfePct": round((hi_all / entry - 1) * 100, 1), "maePct": round((lo_all / entry - 1) * 100, 1),
        "undMax": round(max(uh), 2) if uh else None, "undMin": round(min(ul), 2) if ul else None,
        "closesOnly": bool(p.get("shortContract")), "trailing": r["trailing"],
    })
    if r["done"] and r["exitPrice"] is not None:
        out["exitPrice"] = r["exitPrice"]
    return out


AFTER_DAYS = 10


def exits_to_follow(recs, today):
    """Closed bot trades from the last ~5 weeks whose after-exit tracking isn't finished."""
    cut = (today - timedelta(days=35)).isoformat()
    out = []
    for r in recs:
        p = r.get("proposal") or {}
        if r.get("status") != "closed" or not p.get("contract") or not r.get("fillPrice"):
            continue
        if (r.get("closedAt") or r.get("lastCheck") or "") < cut or (r.get("afterExit") or {}).get("status") == "done":
            continue
        out.append(r)
    return out


def after_exit(rec, obars):
    """What the option did in the 10 trading days after the bot closed it: best/worst and the price 1, 5 and 10 days
    later, compared with the exit price, in % and in R (positive = it kept going up, the exit was early)."""
    p = rec["proposal"]
    exit_px = rec.get("exitPrice") or rec.get("lastMark")
    d0 = (rec.get("closedAt") or rec.get("lastCheck") or "")[:10]
    out = {"updatedAt": iso(now_ny()), "exitPrice": exit_px, "exitDate": d0}
    if not exit_px or not d0:
        return {**out, "status": "done", "note": "no exit price"}
    spread = bool(p.get("shortContract"))
    longb = {_day(b["t"]): b for b in obars.get(p["contract"], [])}
    shortb = {_day(b["t"]): b for b in obars.get(p.get("shortContract"), [])} if spread else {}
    days = sorted(d for d in longb if d > d0 and (not spread or d in shortb))[:AFTER_DAYS]
    qty = rec.get("filledQty") or rec.get("qty") or 0
    risk = rec.get("riskAtFill")
    to_r = lambda px: round((px - exit_px) * qty * 100 / risk, 2) if risk else None
    pct = lambda px: round((px / exit_px - 1) * 100, 1)
    if not days:
        stale = (now_ny().date() - datetime.strptime(d0, "%Y-%m-%d").date()).days > 8
        return {**out, "status": "done" if stale else "tracking", "days": 0, "note": "no trades in this contract since the exit"}
    closes, hi, lo = [], None, None
    for d in days:
        b = longb[d]
        if spread:
            c = float(b["c"]) - float(shortb[d]["c"])
            h = l = c
        else:
            c, h, l = float(b["c"]), float(b["h"]), float(b["l"])
        closes.append(c)
        hi = h if hi is None else max(hi, h)
        lo = l if lo is None else min(lo, l)
    at = lambda n: closes[n - 1] if len(closes) >= n else None
    out.update({
        "status": "done" if len(days) >= AFTER_DAYS else "tracking", "days": len(days),
        "bestPct": pct(hi), "worstPct": pct(lo), "bestR": to_r(hi), "worstR": to_r(lo),
        "day1Pct": pct(at(1)) if at(1) else None, "day5Pct": pct(at(5)) if at(5) else None, "day10Pct": pct(at(10)) if at(10) else None,
        "day1R": to_r(at(1)) if at(1) else None, "day5R": to_r(at(5)) if at(5) else None, "day10R": to_r(at(10)) if at(10) else None,
        "lastPct": pct(closes[-1]), "lastR": to_r(closes[-1]), "closesOnly": spread,
    })
    return out


def run(sub):
    """Update the shadow result of every skipped evaluation, and the after-exit path of every closed bot trade."""
    import autotrader
    from charts import _yahoo
    c = _creds(sub)
    if not c:
        return {"skipped": "no Alpaca account"}
    pk = db.upk(sub)
    today = now_ny().date()
    recs = db.q_prefix(pk, "BOT#")
    todo, dups = candidates(recs, today)
    exits = exits_to_follow(recs, today)
    # taken trades too: the same 20-day path from the evaluation, so exit rules can be compared on every idea
    cut = (today - timedelta(days=35)).isoformat()
    paths = [r for r in recs if _taken(r) and (r.get("proposal") or {}).get("contract") and (r.get("createdAt") or "") >= cut
             and len(r.get("path") or []) < DAYS]
    for r, of in dups:
        db.update(pk, r["SK"], {"shadow": {"status": "dup", "of": of, "updatedAt": iso(now_ny())}})
    if not todo and not exits and not paths:
        return {"tracked": 0, "dups": len(dups), "exits": 0}
    starts = [(r.get("createdAt") or today.isoformat())[:10] for r in todo + paths] + \
             [(r.get("closedAt") or r.get("lastCheck") or today.isoformat())[:10] for r in exits]
    start = min(starts)
    syms = [x for r in todo + exits + paths for x in (r["proposal"]["contract"], r["proposal"].get("shortContract"))]
    errors = []
    obars = option_bars(c, syms, start, errors)
    if errors:
        print("shadow: option bars errors", errors[:5])
    if not obars and errors:
        return {"error": f"Alpaca option bars failed: {errors[0]}", "errors": len(errors)}
    ucache = {}
    cfg = autotrader.settings(sub)
    n = 0
    for r in todo:
        sym = r["symbol"]
        if sym not in ucache:
            try:
                ucache[sym] = _yahoo(sym.replace(".", "-"), "1d", datetime.strptime(start, "%Y-%m-%d") - timedelta(days=3),
                                     now_ny() + timedelta(hours=1)) or []
            except Exception:
                ucache[sym] = []
        earn = (autotrader.parse_earn(autotrader.earn_for(cfg, sym)[0])[0] or None)
        sh = simulate(r, obars, ucache[sym], cfg, earn)
        db.update(pk, r["SK"], {"shadow": sh})
        n += 1
    for r in exits:
        db.update(pk, r["SK"], {"afterExit": after_exit(r, obars)})
    for r in paths:
        sym = r["symbol"]
        if sym not in ucache:
            try:
                ucache[sym] = _yahoo(sym.replace(".", "-"), "1d", datetime.strptime(start, "%Y-%m-%d") - timedelta(days=3),
                                     now_ny() + timedelta(hours=1)) or []
            except Exception:
                ucache[sym] = []
        db.update(pk, r["SK"], {"path": build_path(r, obars, ucache[sym])})
    out = {"tracked": n, "dups": len(dups), "exits": len(exits)}
    if errors:
        out.update(errors=len(errors), firstError=errors[0])
    return out


# ---------------- summary: what the skipped trades would have done ----------------

import re as _re


def _why(r):
    """Main reason the evaluation wasn't taken, as a short stable label."""
    if r.get("aiBlocked") or ((r.get("aiCheck") or {}).get("verdict") == "reject"):
        return "Claude rejected"
    if r.get("decision") == "BUY":
        return "Passed, not placed (dismissed / max positions / auto off)"
    b = (r.get("blocking") or [""])[0]
    b = _re.sub(r"\s*\(.*$", "", b)                       # drop the numbers in parentheses
    b = _re.sub(r"[-+]?\d[\d.,]*", "#", b).strip(" :;.")    # and numbers inside the text
    return b[:90] or "Other"


def _stats(rows):
    done = [x for x in rows if x["status"] == "done"]
    track = [x for x in rows if x["status"] == "tracking"]
    allr = [x for x in rows if x.get("R") is not None]
    rs = [x["R"] for x in allr]
    return {"n": len(rows), "done": len(done), "tracking": len(track),
            "winPct": round(sum(1 for x in allr if x["plPct"] > 0) / len(allr) * 100) if allr else None,
            "avgR": round(sum(rs) / len(rs), 2) if rs else None, "totalR": round(sum(rs), 2) if rs else None,
            "bestR": max(rs) if rs else None, "worstR": min(rs) if rs else None}


def skipped_list(sub, days=35, why=None, claude=None):
    """The skipped trades behind one row of the summary (a skip reason or a Claude verdict), best R first."""
    s = summary(sub, days, with_rows=True)
    rows = s["rows"]
    if why is not None:
        rows = [x for x in rows if x["why"] == why]
    if claude is not None:
        rows = [x for x in rows if (x["claude"] or "not checked") == claude]
    rows.sort(key=lambda x: (x["R"] is None, -(x["R"] or 0)))
    return {"days": days, "why": why, "claude": claude, "n": len(rows), "items": rows}


def summary(sub, days=5, with_rows=False):
    pk = db.upk(sub)
    cut = (now_ny() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    all_recs = db.q_prefix(pk, "BOT#")
    recs = [r for r in all_recs if (r.get("createdAt") or "") >= cut]
    skipped, taken = [], []
    for r in recs:
        if _taken(r):
            if r.get("status") == "closed" or r.get("fillPrice"):
                rr = r.get("realizedR")
                if rr is None and r.get("riskAtFill") and r.get("lastPl") is not None:
                    rr = round(r["lastPl"] / r["riskAtFill"], 2)
                taken.append({"status": "done" if r.get("status") == "closed" else "tracking", "R": rr,
                              "plPct": r.get("realizedPct") if r.get("status") == "closed" else r.get("lastPlPct") or 0})
            continue
        sh = r.get("shadow") or {}
        if sh.get("status") not in ("done", "tracking") or sh.get("plPct") is None:
            continue
        skipped.append({"id": r["id"], "symbol": r.get("symbol"), "at": r.get("createdAt"), "decision": r.get("decision"),
                        "claude": (r.get("aiCheck") or {}).get("verdict"), "why": _why(r), "status": sh["status"],
                        "plPct": sh["plPct"], "R": sh.get("R"), "exitReason": sh.get("exitReason"), "days": sh.get("days"),
                        "contract": (r.get("proposal") or {}).get("contract"), "exp": (r.get("proposal") or {}).get("exp"),
                        "strike": (r.get("proposal") or {}).get("strike"), "reason": (r.get("blocking") or [""])[0]})
    groups = {}
    for x in skipped:
        groups.setdefault(x["why"], []).append(x)
    by_decision = {}
    for x in skipped:
        by_decision.setdefault(x["decision"] or "?", []).append(x)
    by_claude = {}
    for x in skipped:
        by_claude.setdefault(x["claude"] or "not checked", []).append(x)
    ex = {}
    for r in all_recs:
        ae = r.get("afterExit") or {}
        if r.get("status") != "closed" or ae.get("days") in (None, 0) or (r.get("closedAt") or r.get("lastCheck") or "") < cut:
            continue
        why = _re.sub(r"\s*\(.*$", "", r.get("exitReason") or "closed")
        why = _re.sub(r"[-+]?\d[\d.,]*", "#", why).strip(" :;.")[:80]
        ex.setdefault(why, []).append(ae)
    def _ex(rows):
        g = lambda k: [x[k] for x in rows if x.get(k) is not None]
        avg = lambda v: round(sum(v) / len(v), 2) if v else None
        last = g("lastPct")
        return {"n": len(rows), "day1R": avg(g("day1R")), "day5R": avg(g("day5R")), "day10R": avg(g("day10R")),
                "bestR": avg(g("bestR")), "day5Pct": avg(g("day5Pct")), "lastPct": avg(last),
                "earlyPct": round(sum(1 for v in last if v > 0) / len(last) * 100) if last else None}
    run = db.get(pk, "SHADOWRUN") or {}
    pending = sum(1 for r in recs if not _taken(r) and (r.get("proposal") or {}).get("contract") and not r.get("shadow"))
    return {
        "days": days, "skipped": _stats(skipped), "taken": _stats(taken), "pending": pending,
        "byReason": sorted([{"why": k, **_stats(v)} for k, v in groups.items()], key=lambda g: -g["n"]),
        "byDecision": [{"decision": k, **_stats(v)} for k, v in sorted(by_decision.items())],
        "byClaude": [{"verdict": k, **_stats(v)} for k, v in sorted(by_claude.items())],
        "best": sorted([x for x in skipped if x["R"] is not None], key=lambda x: -x["R"])[:5],
        "worst": sorted([x for x in skipped if x["R"] is not None], key=lambda x: x["R"])[:5],
        "run": {k: run.get(k) for k in ("status", "at", "result")},
        "exits": sorted([{"why": k, **_ex(v)} for k, v in ex.items()], key=lambda g: -g["n"]),
        **({"rows": skipped} if with_rows else {}),
    }


# ---------------- flow quality: which unusual-flow alerts are worth acting on ----------------

def _occ(sym):
    """Parse an OCC option symbol -> (expiration date, 'C'/'P', strike) or None."""
    m = _re.match(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$", sym or "")
    if not m:
        return None
    return datetime.strptime(m.group(2), "%y%m%d").date(), m.group(3), int(m.group(4)) / 1000


def _bucket(v, edges, labels):
    if v is None:
        return None
    for e, l in zip(edges, labels):
        if v < e:
            return l
    return labels[-1]


def _result(r):
    """R for an idea, whatever the bot did with it: the real trade if taken, the shadow result otherwise."""
    if _taken(r):
        if not r.get("fillPrice"):
            return None, None
        if r.get("realizedR") is not None:
            return r["realizedR"], "done"
        if r.get("riskAtFill") and r.get("lastPl") is not None:
            return round(r["lastPl"] / r["riskAtFill"], 2), "done" if r.get("status") == "closed" else "tracking"
        return None, None
    sh = r.get("shadow") or {}
    if sh.get("status") in ("done", "tracking") and sh.get("R") is not None:
        return sh["R"], sh["status"]
    return None, None


DIMENSIONS = [
    ("premium", "Flow premium", ["< $250k", "$250k–500k", "$500k–1M", "$1M–5M", "$5M+"]),
    ("sweep", "Sweep", ["Sweep", "No sweep"]),
    ("ask", "Bought at the ask", ["< 70%", "70–85%", "85–95%", "95%+"]),
    ("voi", "Volume / open interest", ["< 1 (may be closing)", "1–3", "3–10", "10+"]),
    ("alerts", "Alerts on the ticker", ["1 alert", "2–3 alerts", "4+ alerts"]),
    ("tod", "Time of the alert (ET)", ["9:30–10:30", "10:30–12:00", "12:00–14:00", "14:00–16:00", "Outside hours"]),
    ("dte", "Flow contract days to expiry", ["< 30", "30–60", "60–120", "120+"]),
    ("otm", "Flow strike vs price", ["In the money", "0–5% OTM", "5–10% OTM", "10%+ OTM"]),
    ("repeat", "Smaller call buys on the ticker (last days, any contract)", ["None", "1 print", "2–3 prints", "4–9 prints", "10+ prints"]),
    ("repeatOk", "Repeat buyer (smaller buys meet the thresholds)", ["Yes", "No"]),
    ("puts", "Puts bought on the ticker, last days (vs calls)", ["No puts", "Under 25%", "25–50%", "50–100%", "More puts than calls"]),
    ("taken", "What the bot did", ["Taken", "Skipped"]),
]


def _puts_bucket(r):
    tf = r.get("tickerFlow")
    if not tf:
        return None
    w = tf.get("window") or {}
    if not (w.get("puts") or {}).get("premium"):
        return "No puts"
    r_ = tf.get("putRatio") or 99
    return "Under 25%" if r_ < 0.25 else "25–50%" if r_ < 0.5 else "50–100%" if r_ <= 1 else "More puts than calls"


def _dims(r):
    o = r.get("origin") or {}
    created = r.get("createdAt") or ""
    hm = created[11:16]
    tod = None
    if hm:
        tod = ("Outside hours" if not ("09:30" <= hm < "16:00") else "9:30–10:30" if hm < "10:30" else "10:30–12:00" if hm < "12:00"
               else "12:00–14:00" if hm < "14:00" else "14:00–16:00")
    dte = otm = None
    occ = _occ(o.get("contract"))
    price = o.get("underlying") or (r.get("signals") or {}).get("price")
    if occ:
        try:
            dte = (occ[0] - datetime.strptime(created[:10], "%Y-%m-%d").date()).days
        except ValueError:
            dte = None
        if price:
            d = (occ[2] / price - 1) * 100 if occ[1] == "C" else (1 - occ[2] / price) * 100
            otm = "In the money" if d < 0 else "0–5% OTM" if d < 5 else "5–10% OTM" if d < 10 else "10%+ OTM"
    n = o.get("alerts") or 1
    tf = r.get("tickerFlow")
    hits = ((tf or {}).get("window") or {}).get("smallCalls", {}).get("hits") if tf else None
    repeat = None if not tf else \
        ("None" if not hits else "1 print" if hits == 1 else "2–3 prints" if hits <= 3 else "4–9 prints" if hits <= 9 else "10+ prints")
    return {
        "puts": _puts_bucket(r), "repeat": repeat, "repeatOk": None if not tf else ("Yes" if tf.get("repeatQualifies") else "No"),
        "premium": _bucket(o.get("premium"), [250e3, 500e3, 1e6, 5e6], ["< $250k", "$250k–500k", "$500k–1M", "$1M–5M", "$5M+"]),
        "sweep": "Sweep" if o.get("sweep") else "No sweep",
        "ask": _bucket(o.get("askPct"), [70, 85, 95], ["< 70%", "70–85%", "85–95%", "95%+"]),
        "voi": _bucket(o.get("volOi"), [1, 3, 10], ["< 1 (may be closing)", "1–3", "3–10", "10+"]),
        "alerts": "1 alert" if n == 1 else "2–3 alerts" if n <= 3 else "4+ alerts",
        "tod": tod,
        "dte": _bucket(dte, [30, 60, 120], ["< 30", "30–60", "60–120", "120+"]),
        "otm": otm,
        "taken": "Taken" if _taken(r) else "Skipped",
    }


def flow_summary(sub, days=35):
    """Results of flow-originated ideas (taken: real result; skipped: shadow result), bucketed by the flow's
    characteristics. Same contract evaluated twice on one day counts once."""
    pk = db.upk(sub)
    cut = (now_ny() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = []
    for r in db.q_prefix(pk, "BOT#"):
        if (r.get("createdAt") or "") < cut or (r.get("origin") or {}).get("type") != "flow":
            continue
        if (r.get("shadow") or {}).get("status") == "dup":
            continue
        R, st = _result(r)
        if R is None:
            continue
        rows.append({"R": R, "status": st, "plPct": R, **_dims(r)})
    out = {"days": days, "n": len(rows), "all": _stats(rows), "dimensions": []}
    for key, title, labels in DIMENSIONS:
        groups = []
        for l in labels:
            g = [x for x in rows if x.get(key) == l]
            if g:
                groups.append({"label": l, **_stats(g)})
        unknown = [x for x in rows if x.get(key) is None]
        if unknown:
            groups.append({"label": "Unknown (older evaluations)", **_stats(unknown)})
        out["dimensions"].append({"key": key, "title": title, "groups": groups})
    return out
