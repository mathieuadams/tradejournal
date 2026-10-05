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
        if (r.get("shadow") or {}).get("status") in ("done", "nodata"):
            continue
        todo.append(r)
    return todo, dups


def simulate(rec, obars, ubars, cfg, earnings=None):
    """Apply the bot's exit rules to the daily path after the evaluation day. Returns the shadow dict."""
    import autotrader
    p = rec["proposal"]
    entry = float(p.get("limit") or p.get("debit") or 0)
    if entry <= 0:
        return {"status": "nodata", "note": "no entry price", "updatedAt": iso(now_ny())}
    d0 = (rec.get("createdAt") or "")[:10]
    spread = bool(p.get("shortContract"))
    longb = {_day(b["t"]): b for b in obars.get(p["contract"], [])}
    shortb = {_day(b["t"]): b for b in obars.get(p.get("shortContract"), [])} if spread else {}
    ub = {_day(b["t"]): b for b in ubars}
    days = sorted(d for d in longb if d > d0 and (not spread or d in shortb))
    # evaluated during the session: that day's close counts too (close only: its high/low may be from before the evaluation)
    if "09:30" <= (rec.get("createdAt") or "")[11:16] < "16:00" and d0 in longb and (not spread or d0 in shortb):
        days = [d0] + days
    stp = autotrader.stop_pct(cfg, p)
    tgt = autotrader.target_pct(cfg, p, entry)
    stop_px = entry * (1 - stp / 100)
    tgt_px = entry * (1 + tgt / 100) if tgt else None
    out = {"entry": round(entry, 2), "stopPct": stp, "targetPct": tgt, "approx": True, "updatedAt": iso(now_ny())}
    if not days:
        stale = (now_ny().date() - datetime.strptime(d0, "%Y-%m-%d").date()).days > 8 if d0 else True
        return {**out, "status": "nodata" if stale else "tracking", "days": 0,
                "note": "no trading day since the evaluation yet" if not stale else "no trades in this contract"}
    hi_all, lo_all, umax, umin = None, None, None, None
    exit_px = exit_reason = exit_day = None
    last = None
    for n, d in enumerate(days[:DAYS], 1):
        b = longb[d]
        if spread:
            s = shortb[d]
            cl = float(b["c"]) - float(s["c"])
            hi = lo = cl                      # spread high/low per day isn't known from leg bars: closes only
        else:
            hi, lo, cl = float(b["h"]), float(b["l"]), float(b["c"])
        if d == d0:
            hi = lo = cl
        last = cl
        hi_all = hi if hi_all is None else max(hi_all, hi)
        lo_all = lo if lo_all is None else min(lo_all, lo)
        u = ub.get(d)
        if u:
            umax = u["h"] if umax is None else max(umax, u["h"])
            umin = u["l"] if umin is None else min(umin, u["l"])
        dte = (datetime.strptime(p["exp"], "%Y-%m-%d").date() - datetime.strptime(d, "%Y-%m-%d").date()).days if p.get("exp") else 99
        if lo <= stop_px:
            exit_px, exit_reason = stop_px, f"option stop (-{stp:g}%)"
        elif u and p.get("underlyingStop") and u["c"] < p["underlyingStop"]:
            exit_px, exit_reason = cl, f"closed below invalidation {p['underlyingStop']}"
        elif tgt_px and hi >= tgt_px:
            exit_px, exit_reason = tgt_px, f"option target (+{tgt:.0f}%)"
        elif u and p.get("underlyingTarget") and u["h"] >= p["underlyingTarget"]:
            exit_px, exit_reason = cl, f"stock reached target {p['underlyingTarget']}"
        elif dte <= cfg["timeStopDte"]:
            exit_px, exit_reason = cl, f"time stop ({dte} days left)"
        elif earnings and d >= (datetime.strptime(earnings, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d"):
            exit_px, exit_reason = cl, "exit before earnings"
        if exit_px is not None:
            exit_day = d
            break
    tracked = days.index(exit_day) + 1 if exit_day else min(len(days), DAYS)
    done = exit_px is not None or len(days) >= DAYS
    if exit_px is None and done:
        exit_px, exit_reason, exit_day = last, f"{DAYS} trading days, still open", days[DAYS - 1]
    px = exit_px if exit_px is not None else last
    pl = (px / entry - 1) * 100
    out.update({
        "status": "done" if done else "tracking", "days": tracked, "last": round(last, 2), "plPct": round(pl, 1),
        "R": round(pl / stp, 2) if stp else None, "exitReason": exit_reason, "exitDate": exit_day,
        "mfePct": round((hi_all / entry - 1) * 100, 1), "maePct": round((lo_all / entry - 1) * 100, 1),
        "undMax": round(umax, 2) if umax is not None else None, "undMin": round(umin, 2) if umin is not None else None,
        "closesOnly": spread,
    })
    if exit_px is not None:
        out["exitPrice"] = round(exit_px, 2)
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
    for r, of in dups:
        db.update(pk, r["SK"], {"shadow": {"status": "dup", "of": of, "updatedAt": iso(now_ny())}})
    if not todo and not exits:
        return {"tracked": 0, "dups": len(dups), "exits": 0}
    starts = [(r.get("createdAt") or today.isoformat())[:10] for r in todo] + \
             [(r.get("closedAt") or r.get("lastCheck") or today.isoformat())[:10] for r in exits]
    start = min(starts)
    syms = [x for r in todo + exits for x in (r["proposal"]["contract"], r["proposal"].get("shortContract"))]
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


def summary(sub, days=5):
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
                        "plPct": sh["plPct"], "R": sh.get("R"), "exitReason": sh.get("exitReason"), "days": sh.get("days")})
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
    ("repeat", "Smaller prints on the contract (that session)", ["None", "1 print", "2–3 prints", "4–9 prints", "10+ prints"]),
    ("repeatOk", "Repeat buyer (smaller prints meet the thresholds)", ["Yes", "No"]),
    ("puts", "Puts bought on the ticker that day (vs calls)", ["No puts", "Under 25%", "25–50%", "50–100%", "More puts than calls"]),
    ("taken", "What the bot did", ["Taken", "Skipped"]),
]


def _puts_bucket(r):
    txt = next((c.get("text") for c in r.get("checks") or [] if (c.get("text") or "").startswith("Puts not piling in: $")), None)
    if not txt:
        return None
    m = _re.search(r"\$([\d,]+) of puts bought.*?vs \$([\d,]+) of calls", txt)
    if not m:
        return None
    pp, cp = float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))
    if not pp:
        return "No puts"
    r_ = pp / cp if cp else 9
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
    rp = o.get("repeat") or {}
    hits = rp.get("hits")
    repeat = None if "repeat" not in o else \
        ("None" if not hits else "1 print" if hits == 1 else "2–3 prints" if hits <= 3 else "4–9 prints" if hits <= 9 else "10+ prints")
    return {
        "puts": _puts_bucket(r), "repeat": repeat, "repeatOk": None if "repeat" not in o else ("Yes" if rp.get("qualifies") else "No"),
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
