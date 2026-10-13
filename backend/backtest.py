"""Flow retest rule tester: load as much history as the data sources give, then measure every rule on it.

1. load_history(): call alerts (Unusual Whales) and daily walls / flip for every watchlist ticker, as far back
   as asked (the sources may give less: the oldest day actually stored is reported).
2. rule_test(): on that history, with ONE fixed exit (stop N x ATR under the support, target N x the risk,
   or sold after N days):
   - the base: every call alert followed by a retest of the put wall or the flip (no other filter);
   - each rule added alone to the base (a real re-run: a rule that skips a candle waits for the next one);
   - your current settings;
   - buckets of the base by alert size, room to the call wall, wait after the alert, time of day;
   - the exits compared on your current entry settings.
   Every line shows trades, win rate, average R (= expectancy) and the average R of each half of the period:
   a rule that only works in one half is probably noise.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import db
from util import iso, now_ny

HIST_SK = "WWHIST"
TEST_SK = "WWRTEST"
MIN_ALERT = 100_000          # call alerts kept in the history (the lowest "call alert >=" you can set)


# ---------------- history ----------------

def _alerts_key(sym, month):
    return f"WWHFLOW#{sym}#{month}"


def stored_alerts(sub, sym):
    """Call alerts of the history + the recent cache, sorted, without duplicates."""
    import wallwatch
    pk = db.upk(sub)
    out = []
    for r in db.q_prefix(pk, f"WWHFLOW#{sym}#"):
        out += r.get("alerts") or []
    try:
        out += [a for a in wallwatch.ticker_alerts(sub, sym, max_age_h=24) if a.get("c")]
    except Exception as e:
        print("recent alerts failed", sym, e)
    seen, uniq = set(), []
    for a in sorted(out, key=lambda x: x["t"]):
        k = (a["t"], a.get("c"), a.get("p"))
        if k not in seen:
            seen.add(k)
            uniq.append({"t": a["t"], "c": int(a.get("c") or 0), "p": float(a.get("p") or 0)})
    return uniq


def _fetch_alerts(sub, sym, days):
    """Call alerts >= $100K bought at the ask, as far back as Unusual Whales pages (stored by month, kept for good).
    A ticker already loaded for at least `days` is only topped up since its last load."""
    import flowdata
    pk = db.upk(sub)
    meta = db.get(pk, f"WWHMETA#{sym}") or {}
    cal = int(days * 1.45) + 3
    if meta.get("days") and int(meta["days"]) >= days and meta.get("at"):
        since = (now_ny().date() - datetime.strptime(meta["at"][:10], "%Y-%m-%d").date()).days + 2
        cal = max(2, min(cal, since))                  # only what's new since the last load
    res = flowdata.alerts(sub, MIN_ALERT, "call", 0, 400, True, False, sym, 0, cal, exclude_etfs=False)
    by_m = {}
    for a in res.get("alerts") or []:
        if (a.get("ticker") or "").upper() != sym or not a.get("atEt"):
            continue
        t = a["atEt"].replace(" ", "T")[:16]
        by_m.setdefault(t[:7], []).append({"t": t, "c": 1, "p": round(a.get("premium") or 0)})
    for m, al in by_m.items():
        old = db.get(pk, _alerts_key(sym, m)) or {}
        seen = {(x["t"], x["p"]) for x in old.get("alerts") or []}
        merged = list(old.get("alerts") or []) + [x for x in al if (x["t"], x["p"]) not in seen]
        db.put({"PK": pk, "SK": _alerts_key(sym, m), "alerts": sorted(merged, key=lambda x: x["t"])[-4000:]})
    ts = sorted(t for al in by_m.values() for t in (x["t"] for x in al))
    oldest = min([x for x in (meta.get("oldest"), ts[0][:10] if ts else None) if x] or [None])
    db.put({"PK": pk, "SK": f"WWHMETA#{sym}", "days": max(int(meta.get("days") or 0), days), "at": iso(now_ny()),
            "oldest": oldest, "n": int(meta.get("n") or 0) + len(ts)})
    return {"n": len(ts), "oldest": oldest, "topUp": cal < int(days * 1.45) + 3}


# ---------------- skew (25-delta risk reversal: put IV - call IV) ----------------

SKEW_PK = "UWSKEW"           # market data: shared, kept for good, topped up every 3 days


def _rr_series(key, sym, exp, until):
    """Daily 25-delta risk reversal of one expiration: {date: value}."""
    import flowdata
    for delta in ("25", "0.25"):
        r = flowdata._uw(key, f"/api/stock/{sym}/historical-risk-reversal-skew",
                         {"expiry": exp, "delta": delta, "timeframe": "1Y", "date": until})
        rows = (r or {}).get("data") or []
        if rows:
            out = {}
            for x in rows:
                try:
                    out[str(x["date"])[:10]] = float(x["risk_reversal"])
                except (KeyError, TypeError, ValueError):
                    pass
            return out
    return {}


def fetch_skew(key, sym, days=250):
    """Constant ~30-day skew by day: each day uses the monthly expiration closest to 30 days out (14 to 60 days)."""
    import wallwatch
    have = db.get(SKEW_PK, sym) or {}
    if have.get("at") and time.time() - float(have.get("ts") or 0) < 3 * 86400 and int(have.get("days") or 0) >= days:
        return have.get("data") or {}
    today = now_ny().date()
    first = (today - timedelta(days=int(days * 1.45))).isoformat()
    exps = wallwatch.monthly_exps((today - timedelta(days=int(days * 1.45) - 14)).isoformat(), (today + timedelta(days=62)).isoformat())
    by_day = {}                                        # date -> (|dte - 30|, value)
    for exp in exps:
        until = min(today - timedelta(days=1), datetime.strptime(exp, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
        for d, v in _rr_series(key, sym, exp, until).items():
            if d < first:
                continue
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - datetime.strptime(d, "%Y-%m-%d").date()).days
            if 14 <= dte <= 60 and (d not in by_day or abs(dte - 30) < by_day[d][0]):
                by_day[d] = (abs(dte - 30), v)
    data = {d: round(v[1], 5) for d, v in sorted(by_day.items())}
    db.put({"PK": SKEW_PK, "SK": sym, "data": data, "days": days, "at": iso(now_ny()), "ts": int(time.time())})
    return data


def stored_skew(sym):
    return (db.get(SKEW_PK, sym) or {}).get("data") or {}


def skew_features(skew, day):
    """Skew known before `day` opens (previous session): its value, its percentile vs the prior ~6 months, its 5-day change."""
    prior = sorted(d for d in skew if d < day)
    if len(prior) < 25:
        return None
    vals = [skew[d] for d in prior[-126:]]
    v = vals[-1]
    pct = sum(1 for x in vals[:-1] if x <= v) / (len(vals) - 1) * 100
    chg = v - skew[prior[-6]] if len(prior) >= 6 else None
    return {"skew": v, "skewPct": round(pct), "skewChg": round(chg, 4) if chg is not None else None}


def hist_status(sub):
    st = db.get(db.upk(sub), HIST_SK) or {}
    return {k: v for k, v in st.items() if k not in ("PK", "SK")}


def start_history(sub, days):
    days = max(45, min(500, int(days or 250)))
    st = {"PK": db.upk(sub), "SK": HIST_SK, "running": iso(now_ny()), "days": days, "round": 0, "alertsDone": [],
          "oldest": {}, "wallsLeft": None, "wallsTotal": None, "errors": [], "startedAt": iso(now_ny())}
    db.put(st)
    return {k: v for k, v in st.items() if k not in ("PK", "SK")}


def load_history(sub, budget_s=520):
    """One round: alerts first (ticker by ticker), then walls; True when there is more to do."""
    import wallwatch
    t0 = time.time()
    pk = db.upk(sub)
    st = db.get(pk, HIST_SK) or {}
    if not st.get("running"):
        return False
    days = int(st.get("days") or 250)
    tickers = wallwatch.config(sub)["tickers"]
    done = set(st.get("alertsDone") or [])
    oldest = dict(st.get("oldest") or {})
    errors = list(st.get("errors") or [])
    for sym in [t for t in tickers if t not in done]:
        if time.time() - t0 > budget_s * 0.4:
            break
        try:
            r = _fetch_alerts(sub, sym, days)
            oldest[sym] = r["oldest"]
        except Exception as e:
            errors.append(f"{sym} alerts: {str(e)[:80]}")
        done.add(sym)
    left = 1
    if len(done) >= len(tickers) and time.time() - t0 < budget_s:
        f = wallwatch.fill_walls(sub, days=days, budget_s=max(30, budget_s - (time.time() - t0)), mp=False, sk="WWFILLH")
        left = f.get("left") or 0
        st.update(wallsLeft=left, wallsTotal=f.get("total"))
        errors += f.get("errors") or []
    skew_done = set(st.get("skewDone") or [])
    if len(done) >= len(tickers) and not left:
        import flowdata
        key = flowdata._key(sub)
        for sym in [t for t in tickers if t not in skew_done]:
            if time.time() - t0 > budget_s:
                break
            try:
                n = len(fetch_skew(key, sym, days))
                oldest_sk = st.get("skewDays") or {}
                st["skewDays"] = {**oldest_sk, sym: n}
            except Exception as e:
                errors.append(f"{sym} skew: {str(e)[:80]}")
            skew_done.add(sym)
        st["skewDone"] = sorted(skew_done)
    more = len(done) < len(tickers) or left > 0 or len(skew_done) < len(tickers)
    st.update(alertsDone=sorted(done), oldest=oldest, errors=errors[-8:], round=int(st.get("round") or 0) + 1,
              at=iso(now_ny()), running=iso(now_ny()) if more and int(st.get("round") or 0) < 80 else None)
    if not st["running"]:
        st["finishedAt"] = iso(now_ny())
    db.put(st)
    return bool(st["running"])


def continue_history(sub):
    """Next round in a new invocation of the same function (the history takes more than one run)."""
    import boto3
    fn = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not fn:
        return
    boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event",
                                  Payload=json.dumps({"sub": sub, "job": "wwhist"}))


# ---------------- rule tester ----------------

RULES = [  # (switch, label)
    ("greenOnly", "Green candle"), ("aboveFlip", "Above the flip"), ("vwapOn", "Above VWAP"), ("emaOn", "Above EMA"),
    ("emaOverVwap", "EMA > VWAP"), ("noAboveCW", "Not above CW"), ("cwRoomOn", "Room to CW"),
    ("spyUp", "SPY trend up (6 days)"), ("kcNotExt", "Under the upper Keltner band"), ("orbAbove", "Above the opening range high"),
]
OFF = {"usePW": True, "useFlip": True, "greenOnly": False, "aboveFlip": False, "vwapOn": False, "emaOn": False,
       "emaOverVwap": False, "noAboveCW": False, "cwRoomOn": False, "earnOn": False,
       "spyUp": False, "kcNotExt": False, "orbAbove": False,
       "stopOn": True, "trailOn": False, "targetOn": False, "flipExit": False, "vwapExit": False, "emaExit": False}
EXITS = [  # label, exit settings on top of the fixed stop
    ("Fixed: target N x risk or N days", {}),
    ("Trailing stop", {"fixR": 0, "trailOn": True}),
    ("Call wall target", {"fixR": 0, "targetOn": True}),
    ("Call wall + trailing", {"fixR": 0, "targetOn": True, "trailOn": True}),
    ("Close under flip", {"fixR": 0, "flipExit": True}),
    ("Close under VWAP", {"fixR": 0, "vwapExit": True}),
    ("Close under EMA", {"fixR": 0, "emaExit": True}),
    ("Your exits", "mine"),
]
EXIT_KEYS = ("stopOn", "trailOn", "targetOn", "flipExit", "vwapExit", "emaExit", "trailAtr", "tgtBuf")


def _ticker_data(sub, sym, days, cfg):
    import flowdata
    import wallwatch
    from charts import _yahoo
    now = now_ny().replace(tzinfo=None)
    start = now - timedelta(days=int(days * 1.45) + 40)
    bars = wallwatch.finished(_yahoo(sym.replace(".", "-"), "1h", max(start, now - timedelta(days=725)), now + timedelta(days=1)) or [], now)
    if not bars:
        return None
    daily = _yahoo(sym.replace(".", "-"), "1d", start - timedelta(days=70), now + timedelta(days=1)) or []
    daily = [b for b in daily if b["t"][:10] < now.strftime("%Y-%m-%d")]
    walls = flowdata.stored_walls(sym)
    vw, _ = wallwatch.anchored_vwap(bars, int(cfg.get("vwapDays") or wallwatch.VWAP_DAYS))
    em = wallwatch._ema_series([b["c"] for b in bars], int(cfg.get("emaLen") or 20))
    kc = wallwatch.keltner(bars, int(cfg.get("kcLen") or 21), float(cfg.get("kcMult") or 3))
    return {"bars": bars, "datr": wallwatch.daily_atr(daily), "walls": walls, "alerts": stored_alerts(sub, sym), "skew": stored_skew(sym),
            "vmap": {b["t"][:16]: v for b, v in zip(bars, vw)}, "emap": {b["t"][:16]: v for b, v in zip(bars, em)},
            "extra": {"kc": {b["t"][:16]: v for b, v in zip(bars, kc)}, "spy": wallwatch.spy_trend(start, now)}}


def _run(sym, data, cfg, first):
    rows = _run0(sym, data, cfg, first)
    sk = data.get("skew") or {}
    for r in rows:
        f = skew_features(sk, r["at"][:10]) if sk else None
        if f:
            r.update(f)
    return rows


def _run0(sym, data, cfg, first):
    import wallwatch
    sigs, _ = wallwatch.detect_retest(data["bars"], data["walls"], first, cfg, data["datr"], data["alerts"], None,
                                      data["vmap"], data["emap"], data.get("extra"))
    out = []
    for s in sigs:
        if s["status"] == "open" or s.get("R") is None:
            continue
        cw = s.get("cw")
        out.append({"sym": sym, "at": s["at"], "R": s["R"], "pct": s["pct"], "exit": s["status"], "level": s.get("levelName"),
                    "prem": s.get("armPremium") or 0, "room": round((cw - s["entry"]) / s["atr"], 2) if cw and s.get("atr") else None,
                    "waitH": round((datetime.strptime(s["at"], "%Y-%m-%dT%H:%M") - datetime.strptime(s["armedAt"][:16], "%Y-%m-%dT%H:%M")).total_seconds() / 3600, 1) if s.get("armedAt") else None,
                    "hour": s["at"][11:16], "spy": s.get("spy"),
                    "kcPos": None if not s.get("kc") else ("above upper" if s["entry"] >= s["kc"][1] else "mid–upper" if s["entry"] >= s["kc"][0] else "lower–mid" if s["entry"] >= s["kc"][2] else "below lower"),
                    "orbPos": "first hour" if not s.get("orb") else ("above ORB high" if s["entry"] > s["orb"][0] else "below ORB low" if s["entry"] < s["orb"][1] else "inside ORB")})
    return out


def _stats(rows, mid):
    n = len(rows)
    if not n:
        return {"n": 0}
    rs = [r["R"] for r in rows]
    a = [r["R"] for r in rows if r["at"][:10] < mid]
    b = [r["R"] for r in rows if r["at"][:10] >= mid]
    avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    wins = [x for x in rs if x > 0]
    loss = [x for x in rs if x <= 0]
    return {"n": n, "win": round(len(wins) / n * 100, 1), "avgR": avg(rs), "totR": round(sum(rs), 1),
            "avgWin": avg(wins), "avgLoss": avg(loss), "h1": avg(a), "h1n": len(a), "h2": avg(b), "h2n": len(b),
            "pf": round(sum(wins) / -sum(loss), 2) if loss and sum(loss) < 0 else None}


BUCKETS = [
    ("Call alert", "prem", [(0, 250_000, "$100–250K"), (250_000, 500_000, "$250–500K"), (500_000, 1_000_000, "$500K–1M"), (1_000_000, 1e18, "$1M+")]),
    ("Room to the call wall (ATR)", "room", [(-1e9, 0.5, "< 0.5"), (0.5, 1, "0.5–1"), (1, 2, "1–2"), (2, 1e9, "2+")]),
    ("Wait after the alert", "waitH", [(-1, 7, "same day"), (7, 48, "1–2 days"), (48, 1e9, "3+ days")]),
    ("Skew (25Δ put IV − call IV) vs its last 6 months", "skewPct", [(-1, 33.4, "low: calls bid (bullish demand)"), (33.4, 66.7, "normal"), (66.7, 101, "high: puts bid (fear)")]),
    ("Skew change over 5 days", "skewChg", [(-1e9, -0.005, "falling: puts cheaper"), (-0.005, 0.005, "flat"), (0.005, 1e9, "rising: puts richer")]),
]


def rule_test(sub, params=None, budget_s=540):
    import wallwatch
    t0 = time.time()
    pk = db.upk(sub)
    P = {"days": 250, "stopAtr": None, "fixR": 2.0, "maxDays": 5, "retestMin": None, **(params or {})}
    cfg = wallwatch.config(sub)
    days = max(30, min(500, int(P["days"])))
    stop = float(P["stopAtr"] if P["stopAtr"] is not None else cfg.get("stopAtr") or 0.5)
    vmin = float(P["retestMin"] or cfg.get("retestMin") or 500_000)
    first = (now_ny() - timedelta(days=int(days * 1.45))).strftime("%Y-%m-%d")
    mid_d = now_ny() - timedelta(days=int(days * 1.45) // 2)
    mid = mid_d.strftime("%Y-%m-%d")
    fixed = {"fixR": float(P["fixR"] or 0), "maxDays": int(P["maxDays"] or 0)}
    common = {k: cfg.get(k) for k in ("retestDays", "retestZone", "cwRoom", "emaLen", "vwapDays", "trailAtr", "tgtBuf", "kcLen", "kcMult")}
    base = {**common, **OFF, "stopAtr": stop, "retestMin": vmin, **fixed}
    mine_entry = {k: cfg.get(k) for k in wallwatch.SWITCHES if k not in EXIT_KEYS}
    mine_entry.update(aboveFlip=cfg.get("aboveFlip"), earnOn=False)      # no earnings history: off in the test
    variants = [("base", "Base: alert + retest of PW / flip", base)]
    variants += [(k, f"+ {lab}", {**base, k: True}) for k, lab in RULES]
    variants += [("pwOnly", "Support: put wall only", {**base, "useFlip": False}),
                 ("flipOnly", "Support: flip only", {**base, "usePW": False})]
    variants.append(("mine", "Your entry settings", {**base, **mine_entry}))
    exits = []
    for lab, ex in EXITS:
        if ex == "mine":
            ex = {"fixR": 0, "maxDays": 0, **{k: cfg.get(k) for k in EXIT_KEYS}}
        exits.append((lab, {**base, **mine_entry, **ex}))
    tickers = cfg["tickers"]
    db.put({"PK": pk, "SK": TEST_SK, **(db.get(pk, TEST_SK) or {}), "running": iso(now_ny()), "params": P})
    res = {k: [] for k, _, _ in variants}
    exr = {lab: [] for lab, _ in exits}
    errors, used, first_bar, n_alerts = [], 0, None, 0

    def one(sym):
        d = _ticker_data(sub, sym, days, cfg)
        if not d:
            return sym, None, None, None
        out = {k: _run(sym, d, c, first) for k, _, c in variants}
        ex_out = {lab: _run(sym, d, c, first) for lab, c in exits}
        return sym, out, ex_out, (d["bars"][0]["t"][:10], len([a for a in d["alerts"] if a["c"] and a["p"] >= vmin and a["t"][:10] >= first]))

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(one, t) for t in tickers]
        for f in futs:
            try:
                sym, out, ex_out, info = f.result(timeout=max(5, budget_s - (time.time() - t0)))
            except Exception as e:
                errors.append(str(e)[:100])
                continue
            if out is None:
                continue
            used += 1
            first_bar = min(first_bar or info[0], info[0])
            n_alerts += info[1]
            for k, v in out.items():
                res[k] += v
            for k, v in ex_out.items():
                exr[k] += v
    base_rows = res["base"]
    buckets = []
    for title, key, bs in BUCKETS:
        for lo, hi, lab in bs:
            rows = [r for r in base_rows if r.get(key) is not None and lo <= r[key] < hi]
            buckets.append({"group": title, "label": lab, **_stats(rows, mid)})
    for title, key, labs in (("Keltner channel (21, 3)", "kcPos", ("below lower", "lower–mid", "mid–upper", "above upper")),
                             ("Opening range (09:30–10:30)", "orbPos", ("first hour", "below ORB low", "inside ORB", "above ORB high")),
                             ("SPY trend (6 days)", "spy", (1, -1))):
        for lab in labs:
            buckets.append({"group": title, "label": {1: "up", -1: "down"}.get(lab, lab) if key == "spy" else lab,
                            **_stats([r for r in base_rows if r.get(key) == lab], mid)})
    for lab, f_ in (("First hour (09:30)", lambda r: r["hour"] == "09:30"), ("Later in the day", lambda r: r["hour"] != "09:30")):
        buckets.append({"group": "Entry candle", "label": lab, **_stats([r for r in base_rows if f_(r)], mid)})
    by_sym = {}
    for r in res["mine"]:
        by_sym.setdefault(r["sym"], []).append(r)
    out = {"PK": pk, "SK": TEST_SK, "running": None, "at": iso(now_ny()), "params": {**P, "stopAtr": stop, "retestMin": vmin},
           "period": {"from": first, "mid": mid, "to": now_ny().strftime("%Y-%m-%d"), "firstBar": first_bar},
           "tickers": used, "alerts": n_alerts, "errors": errors[:5], "secs": round(time.time() - t0),
           "rules": [{"key": k, "label": lab, **_stats(res[k], mid)} for k, lab, _ in variants],
           "buckets": buckets,
           "exits": [{"label": lab, **_stats(exr[lab], mid)} for lab, _ in exits],
           "bySymbol": sorted([{"sym": s_, **_stats(v, mid)} for s_, v in by_sym.items()], key=lambda x: -(x.get("totR") or 0))[:60]}
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


def test_result(sub):
    r = db.get(db.upk(sub), TEST_SK) or {}
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}
