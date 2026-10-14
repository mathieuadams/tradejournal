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


def fetch_skew(key, sym, days=250, max_age_s=3 * 86400):
    """Constant ~30-day skew by day: each day uses the monthly expiration closest to 30 days out (14 to 60 days)."""
    import wallwatch
    have = db.get(SKEW_PK, sym) or {}
    if have.get("at") and time.time() - float(have.get("ts") or 0) < max_age_s and int(have.get("days") or 0) >= days:
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


# ---------------- dark pool history (prints >= 0.25% of the average daily volume) ----------------

DP_PK = "UWDPH"              # compact prints by ticker and month: [[time, price, premium], ...]


def fetch_darkpool(key, sym, days=250, budget_end=None):
    """Every day's big dark pool prints (one request per day, each day cached for good), packed by month.
    Returns the number of days stored, or None when the time ran out (resume next round)."""
    import flowdata
    import wallwatch
    from charts import _yahoo
    meta = db.get(DP_PK, f"{sym}#META") or {}
    if meta.get("ts") and time.time() - float(meta["ts"]) < 3 * 86400 and int(meta.get("days") or 0) >= days:
        return int(meta.get("n") or 0)
    now = now_ny().replace(tzinfo=None)
    daily = [b for b in (_yahoo(sym.replace(".", "-"), "1d", now - timedelta(days=60), now + timedelta(days=1)) or [])
             if b["t"][:10] < now.strftime("%Y-%m-%d") and b.get("v")]
    _, _, min_sh, _, floor = wallwatch.dp_floor(daily)
    dates = [d for d in wallwatch._weekdays(days, now.date()) if d < now.strftime("%Y-%m-%d")]
    by_m, n = {}, 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [(d, pool.submit(flowdata.darkpool_day, key, sym, d, floor)) for d in dates]
        for d, f in futs:
            if budget_end and time.time() > budget_end:
                pool.shutdown(wait=False, cancel_futures=True)
                return None
            prints = f.result()
            n += 1
            keep = [[p["t"], p["price"], p["premium"]] for p in prints if not min_sh or p["size"] >= min_sh]
            if keep:
                by_m.setdefault(d[:7], []).extend(keep)
    for m, rows in by_m.items():
        rows.sort()
        db.put({"PK": DP_PK, "SK": f"{sym}#{m}", "prints": rows[-3000:]})
    db.put({"PK": DP_PK, "SK": f"{sym}#META", "days": days, "n": n, "floor": floor, "minShares": round(min_sh or 0),
            "at": iso(now_ny()), "ts": int(time.time())})
    return n


def stored_darkpool(sym):
    out = []
    for r in db.q_prefix(DP_PK, f"{sym}#"):
        if not r["SK"].endswith("#META"):
            out += [{"t": x[0], "price": float(x[1]), "premium": float(x[2])} for x in r.get("prints") or []]
    out.sort(key=lambda x: x["t"])
    return out


def dp_features(prints, day, entry, support, target, atr):
    """Dark pool known before `day`: top 5 levels of the previous 45 trading days (as on the chart),
    a level near the support, a level between the entry and the target, and the last 5 days' activity vs normal."""
    import flowdata
    if not prints or not atr:
        return None
    d0 = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=66)).strftime("%Y-%m-%d")
    win = [p for p in prints if d0 <= p["t"][:10] < day]
    if len(win) < 5:
        return None
    levels = flowdata.dp_levels(win, 5)
    near = any(abs(lv["price"] - support) <= 0.5 * atr for lv in levels)
    way = any(entry + 0.25 * atr < lv["price"] < target for lv in levels) if target else None
    d5 = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    last5 = sum(p["premium"] for p in win if p["t"][:10] >= d5)
    avg5 = sum(p["premium"] for p in win) / max(1.0, len({p["t"][:10] for p in win})) * 5
    return {"dpNear": near, "dpWay": way, "dpAct": round(last5 / avg5, 2) if avg5 else None}


def skew_pct_map(skew, dates):
    """{date: skew percentile known before that date opens}."""
    out = {}
    for d in dates:
        f = skew_features(skew, d) if skew else None
        if f:
            out[d] = f["skewPct"]
    return out


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
    dp_done = set(st.get("dpDone") or [])
    if len(done) >= len(tickers) and not left and len(skew_done) >= len(tickers):
        import flowdata
        key = flowdata._key(sub)
        for sym in [t for t in tickers if t not in dp_done]:
            if time.time() - t0 > budget_s - 20:
                break
            try:
                n = fetch_darkpool(key, sym, days, t0 + budget_s)
                if n is None:
                    break                              # out of time: this ticker resumes next round (cached days are skipped)
                st["dpDays"] = {**(st.get("dpDays") or {}), sym: n}
            except Exception as e:
                errors.append(f"{sym} dark pool: {str(e)[:80]}")
            dp_done.add(sym)
        st["dpDone"] = sorted(dp_done)
    more = len(done) < len(tickers) or left > 0 or len(skew_done) < len(tickers) or len(dp_done) < len(tickers)
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
    ("skewOn", "Skew normal (middle third of 6 months)"), ("spyUp", "SPY trend up (6 days)"), ("kcNotExt", "Under the upper Keltner band"), ("orbAbove", "Above the opening range high"),
]
OFF = {"usePW": True, "useFlip": True, "greenOnly": False, "aboveFlip": False, "vwapOn": False, "emaOn": False,
       "emaOverVwap": False, "noAboveCW": False, "cwRoomOn": False, "earnOn": False,
       "spyUp": False, "kcNotExt": False, "orbAbove": False, "skewOn": False, "kcRide": False, "cwLost": False, "dpLost": False,
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
    ("Your exits + Keltner ride", "mine+ride"),
    ("Your exits without Keltner ride", "mine-ride"),
    ("Your exits + back under CW", "mine+cw"),
    ("Your exits + back under dark pool", "mine+dp"),
    ("Your exits without the back-under exits", "mine-lost"),
]
EXIT_KEYS = ("stopOn", "trailOn", "targetOn", "flipExit", "vwapExit", "emaExit", "trailAtr", "tgtBuf", "kcRide", "cwLost", "dpLost")


def _ticker_data(sub, sym, days, cfg, full=True):
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
    dp = stored_darkpool(sym)
    days_ = sorted({b["t"][:10] for b in bars})
    sk = stored_skew(sym) if full or cfg.get("skewOn") else {}
    return {"bars": bars, "datr": wallwatch.daily_atr(daily), "walls": walls, "alerts": stored_alerts(sub, sym),
            "skew": sk, "dp": dp,
            "vmap": {b["t"][:16]: v for b, v in zip(bars, vw)}, "emap": {b["t"][:16]: v for b, v in zip(bars, em)},
            "extra": {"kc": {b["t"][:16]: v for b, v in zip(bars, kc)}, "spy": wallwatch.spy_trend(start, now) if full or cfg.get("spyUp") else {},
                      "dpLevels": wallwatch.dp_level_map(dp, days_), "skewPct": skew_pct_map(sk, days_) if sk else {}}}


def _run(sym, data, cfg, first):
    rows = _run0(sym, data, cfg, first)
    sk, dp = data.get("skew") or {}, data.get("dp") or []
    memo = data.setdefault("_memo", {})                # the same trade shows up in many variants: compute once
    for r in rows:
        key = (r["at"], r["support"], r["entry"], r["cw"])
        if key not in memo:
            f = {}
            if sk:
                f.update(_skew_day(data, r["at"][:10]) or {})
            if dp:
                f.update(_dp_feat(data, r["at"][:10], r["entry"], r["support"], r["cw"], r["atr"]) or {})
            memo[key] = f
        r.update(memo[key])
        for k in ("entry", "support", "cw", "atr"):
            r.pop(k, None)
    return rows


def _skew_day(data, day):
    c = data.setdefault("_skew", {})
    if day not in c:
        c[day] = skew_features(data.get("skew") or {}, day)
    return c[day]


def _dp_feat(data, day, entry, support, target, atr):
    """dp_features with the 2-month window found by binary search and its top levels computed once per day."""
    import bisect
    import flowdata
    if not atr:
        return None
    dp = data["dp"]
    if "_dpt" not in data:
        data["_dpt"] = [p["t"][:10] for p in dp]
    ts, cache = data["_dpt"], data.setdefault("_dpd", {})
    if day not in cache:
        d0 = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=66)).strftime("%Y-%m-%d")
        d5 = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        win = dp[bisect.bisect_left(ts, d0):bisect.bisect_left(ts, day)]
        if len(win) < 5:
            cache[day] = None
        else:
            last5 = sum(p["premium"] for p in win if p["t"][:10] >= d5)
            avg5 = sum(p["premium"] for p in win) / max(1.0, len({p["t"][:10] for p in win})) * 5
            cache[day] = (flowdata.dp_levels(win, 5), round(last5 / avg5, 2) if avg5 else None)
    if not cache[day]:
        return None
    levels, act = cache[day]
    return {"dpNear": any(abs(lv["price"] - support) <= 0.5 * atr for lv in levels),
            "dpWay": any(entry + 0.25 * atr < lv["price"] < target for lv in levels) if target else None, "dpAct": act}


def _run0(sym, data, cfg, first):
    import wallwatch
    sigs, _ = wallwatch.detect_retest(data["bars"], data["walls"], first, cfg, data["datr"], data["alerts"], None,
                                      data["vmap"], data["emap"], data.get("extra"))
    out = []
    for s in sigs:
        if s.get("R") is None:
            continue                                   # still-open trades count at their last price (marked to market)
        cw = s.get("cw")
        out.append({"sym": sym, "at": s["at"], "R": s["R"], "pct": s["pct"], "exit": s["status"], "level": s.get("levelName"),
                    "entry": s["entry"], "support": s.get("level"), "cw": cw, "atr": s.get("atr"),
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
    return {"n": n, "open": sum(1 for r in rows if r.get("exit") == "open"), "win": round(len(wins) / n * 100, 1), "avgR": avg(rs), "totR": round(sum(rs), 1),
            "avgWin": avg(wins), "avgLoss": avg(loss), "h1": avg(a), "h1n": len(a), "h2": avg(b), "h2n": len(b),
            "pf": round(sum(wins) / -sum(loss), 2) if loss and sum(loss) < 0 else None}


BUCKETS = [
    ("Call alert", "prem", [(0, 250_000, "$100–250K"), (250_000, 500_000, "$250–500K"), (500_000, 1_000_000, "$500K–1M"), (1_000_000, 1e18, "$1M+")]),
    ("Room to the call wall (ATR)", "room", [(-1e9, 0.5, "< 0.5"), (0.5, 1, "0.5–1"), (1, 2, "1–2"), (2, 1e9, "2+")]),
    ("Wait after the alert", "waitH", [(-1, 7, "same day"), (7, 48, "1–2 days"), (48, 1e9, "3+ days")]),
    ("Skew (25Δ put IV − call IV) vs its last 6 months", "skewPct", [(-1, 33.4, "low: calls bid (bullish demand)"), (33.4, 66.7, "normal"), (66.7, 101, "high: puts bid (fear)")]),
    ("Dark pool activity, last 5 days vs the previous 2 months", "dpAct", [(-1, 0.7, "quiet (< 0.7×)"), (0.7, 1.5, "normal"), (1.5, 1e9, "heavy (> 1.5×)")]),
    ("Skew change over 5 days", "skewChg", [(-1e9, -0.005, "falling: puts cheaper"), (-0.005, 0.005, "flat"), (0.005, 1e9, "rising: puts richer")]),
]


def rule_test(sub, params=None, budget_s=480):
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
    common = {k: cfg.get(k) for k in ("retestDays", "retestZone", "cwRoom", "emaLen", "vwapDays", "trailAtr", "tgtBuf", "kcLen", "kcMult", "relFlow", "relMult")}
    base = {**common, **OFF, "stopAtr": stop, "retestMin": vmin, **fixed}
    mine_entry = {k: cfg.get(k) for k in wallwatch.SWITCHES if k not in EXIT_KEYS}
    mine_entry.update(aboveFlip=cfg.get("aboveFlip"), earnOn=False)      # no earnings history: off in the test
    full = bool(P.get("full"))
    variants = [("base", "Base: alert + retest of PW / flip", base)]
    if full:                                           # every rule alone (slower); the default run is base vs your settings
        variants += [(k, f"+ {lab}", {**base, k: True}) for k, lab in RULES]
        variants += [("pwOnly", "Support: put wall only", {**base, "useFlip": False}),
                     ("flipOnly", "Support: flip only", {**base, "usePW": False})]
    variants.append(("mine", "Your entry settings", {**base, **mine_entry}))
    exits = []
    for lab, ex in (EXITS if full else [x for x in EXITS if str(x[1]).startswith("mine") or x[0].startswith("Fixed")]):
        if str(ex).startswith("mine"):
            ex = {"fixR": 0, "maxDays": 0, **{k: cfg.get(k) for k in EXIT_KEYS},
                  **{"mine+ride": {"kcRide": True}, "mine-ride": {"kcRide": False}, "mine+cw": {"cwLost": True},
                     "mine+dp": {"dpLost": True}, "mine-lost": {"cwLost": False, "dpLost": False}}.get(ex, {})}
        exits.append((lab, {**base, **mine_entry, **ex}))
    tickers = cfg["tickers"]
    db.put({"PK": pk, "SK": TEST_SK, **(db.get(pk, TEST_SK) or {}), "running": iso(now_ny()), "params": P})
    res = {k: [] for k, _, _ in variants}
    exr = {lab: [] for lab, _ in exits}
    errors, used, first_bar, n_alerts = [], 0, None, 0

    def one(sym):
        d = _ticker_data(sub, sym, days, cfg, full)
        if not d:
            return sym, None, None, None
        out = {k: _run(sym, d, c, first) for k, _, c in variants}
        ex_out = {lab: _run(sym, d, c, first) for lab, c in exits}
        return sym, out, ex_out, (d["bars"][0]["t"][:10], len([a for a in d["alerts"] if a["c"] and a["p"] >= vmin and a["t"][:10] >= first]))

    pool = ThreadPoolExecutor(max_workers=8)
    if True:
        futs = [pool.submit(one, t) for t in tickers]
        for f in futs:
            try:
                sym, out, ex_out, info = f.result(timeout=max(1, budget_s - (time.time() - t0)))
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
    pool.shutdown(wait=False, cancel_futures=True)       # never wait past the budget (the function has 10 minutes)
    if used < len(tickers):
        errors.insert(0, f"Time limit: {used} of {len(tickers)} tickers tested. Run again for the rest of the cache to help, or test fewer days.")
    base_rows = res["base"] if full else []
    buckets = []
    for title, key, bs in BUCKETS:
        for lo, hi, lab in bs:
            rows = [r for r in base_rows if r.get(key) is not None and lo <= r[key] < hi]
            buckets.append({"group": title, "label": lab, **_stats(rows, mid)})
    for title, key, labs in (("Keltner channel (21, 3)", "kcPos", ("below lower", "lower–mid", "mid–upper", "above upper")),
                             ("Opening range (09:30–10:30)", "orbPos", ("first hour", "below ORB low", "inside ORB", "above ORB high")),
                             ("SPY trend (6 days)", "spy", (1, -1)),
                             ("Dark pool level within 0.5 ATR of the support", "dpNear", (True, False)),
                             ("Dark pool level between the entry and the call wall", "dpWay", (True, False))):
        for lab in labs:
            buckets.append({"group": title, "label": {1: "up", -1: "down"}.get(lab, lab) if key == "spy" else ({True: "yes", False: "no"}[lab] if isinstance(lab, bool) else lab),
                            **_stats([r for r in base_rows if r.get(key) == lab], mid)})
    for lab, f_ in (("First hour (09:30)", lambda r: r["hour"] == "09:30"), ("Later in the day", lambda r: r["hour"] != "09:30")):
        buckets.append({"group": "Entry candle", "label": lab, **_stats([r for r in base_rows if f_(r)], mid)})
    if not full:
        buckets = []                                   # splits only in the full analysis
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


ROB_SK = "WWRROB"


def _mc(rs, runs=2000, seed=7):
    """Monte Carlo of the trade order: the drawdown (in R) you should expect, the longest losing streak,
    and the chance the next N trades lose money overall (bootstrap)."""
    import random
    if len(rs) < 5:
        return {}
    rnd = random.Random(seed)
    dds, neg = [], 0
    for _ in range(runs):
        seq = rnd.sample(rs, len(rs))
        eq = peak = dd = 0.0
        for r in seq:
            eq += r
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        dds.append(dd)
        if sum(rnd.choice(rs) for _ in range(len(rs))) < 0:
            neg += 1
    dds.sort()
    streak = cur = 0
    for r in rs:
        cur = cur + 1 if r <= 0 else 0
        streak = max(streak, cur)
    return {"ddMedian": round(dds[len(dds) // 2], 1), "dd95": round(dds[int(len(dds) * 0.95)], 1),
            "lossStreak": streak, "pLoss": round(neg / runs * 100, 1), "n": len(rs)}


def robust_test(sub, params=None, budget_s=480):
    """Your full settings (entry + exits) re-run with each setting nudged up and down: an edge that collapses when a
    number moves a little is fitted to the past. Plus: the share of the profit made by the best 3 tickers and a
    Monte Carlo of the trade order (expected drawdown, losing streak, chance of losing over the same number of trades)."""
    import wallwatch
    t0 = time.time()
    pk = db.upk(sub)
    P = {"days": 250, **(params or {})}
    cfg = wallwatch.config(sub)
    days = max(30, min(500, int(P["days"])))
    first = (now_ny() - timedelta(days=int(days * 1.45))).strftime("%Y-%m-%d")
    mid = (now_ny() - timedelta(days=int(days * 1.45) // 2)).strftime("%Y-%m-%d")
    mine = {**{k: cfg.get(k) for k in PARAMS_KEYS(cfg)}, "earnOn": False, "fixR": 0, "maxDays": 0}
    st, room, km, zone = float(cfg.get("stopAtr") or 0.5), float(cfg.get("cwRoom") or 1), float(cfg.get("kcMult") or 3), float(cfg.get("retestZone") or 0.25)
    vmin, wait = float(cfg.get("retestMin") or 250_000), int(cfg.get("retestDays") or 5)
    variants = [("Your settings as they are", {})]
    variants += [(f"Stop {v:g} ATR (you: {st:g})", {"stopAtr": v}) for v in (round(st * 0.8, 2), round(st * 1.2, 2))]
    if cfg.get("cwRoomOn"):
        variants += [(f"Room to CW {v:g} ATR (you: {room:g})", {"cwRoom": v}) for v in (max(0.25, room - 0.5), room + 0.5)]
    if cfg.get("kcNotExt") or cfg.get("kcRide"):
        variants += [(f"Keltner × {v:g} (you: {km:g})", {"kcMult": v}) for v in (km - 0.5, km + 0.5)]
    variants += [(f"Retest zone {v:g} ATR (you: {zone:g})", {"retestZone": v}) for v in (round(zone * 0.6, 2), round(zone * 1.4, 2))]
    variants += [(f"Call alert ≥ ${int(v / 1000)}K (you: ${int(vmin / 1000)}K)", {"retestMin": v}) for v in (vmin * 1.4,)]
    variants += [(f"Wait {v} days (you: {wait})", {"retestDays": v}) for v in (max(1, wait - 1), wait + 1)]
    db.put({"PK": pk, "SK": ROB_SK, **(db.get(pk, ROB_SK) or {}), "running": iso(now_ny())})
    res = {lab: [] for lab, _ in variants}
    errors = []

    def one(sym):
        d = _ticker_data(sub, sym, days, cfg)
        if not d:
            return sym, None
        out = {}
        for lab, ch in variants:
            c = {**mine, **ch}
            if "kcMult" in ch:                          # the Keltner bands themselves change
                kc = wallwatch.keltner(d["bars"], int(cfg.get("kcLen") or 21), float(ch["kcMult"]))
                d2 = {**d, "extra": {**d["extra"], "kc": {b["t"][:16]: v for b, v in zip(d["bars"], kc)}}}
                out[lab] = _run(sym, d2, c, first)
            else:
                out[lab] = _run(sym, d, c, first)
        return sym, out

    pool = ThreadPoolExecutor(max_workers=8)
    done_n = 0
    for f in [pool.submit(one, t) for t in cfg["tickers"]]:
        try:
            sym, out = f.result(timeout=max(1, budget_s - (time.time() - t0)))
            done_n += 1
            for k, v in (out or {}).items():
                res[k] += v
        except Exception as e:
            errors.append(str(e)[:100] or "time limit")
    pool.shutdown(wait=False, cancel_futures=True)
    if done_n < len(cfg["tickers"]):
        errors.insert(0, f"Time limit: {done_n} of {len(cfg['tickers'])} tickers tested.")
    base_rows = res[variants[0][0]]
    rows = [{"label": lab, **_stats(res[lab], mid)} for lab, _ in variants]
    by = {}
    for r in base_rows:
        by[r["sym"]] = by.get(r["sym"], 0) + r["R"]
    tot = sum(by.values())
    top3 = sorted(by.items(), key=lambda x: -x[1])[:3]
    rest = [r for r in base_rows if r["sym"] not in {t for t, _ in top3}]
    conc = {"top3": [{"sym": t, "R": round(v, 1)} for t, v in top3], "top3Share": round(sum(v for _, v in top3) / tot * 100) if tot > 0 else None,
            "withoutTop3": _stats(rest, mid), "tickers": len(by)}
    out = {"PK": pk, "SK": ROB_SK, "running": None, "at": iso(now_ny()), "secs": round(time.time() - t0), "rows": rows,
           "concentration": conc, "monteCarlo": _mc([r["R"] for r in sorted(base_rows, key=lambda r: r["at"])]), "errors": errors[:5]}
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


PV_SK = "WWPVSTUDY"
PV_H = [("1 day", 7), ("3 days", 21), ("5 days", 35), ("10 days", 70)]


def pv_study(sub, params=None, budget_s=480):
    """How the stock moves after a VWAP reset candle (the hourly candle with the highest volume of the period) that
    touches the put wall, vs resets elsewhere and any day. Forward returns from the crossing candle's close, plus
    the best (MFE) and worst (MAE) move over the next 5 days. Baseline: every hourly candle."""
    import wallwatch
    t0 = time.time()
    pk = db.upk(sub)
    P = {"days": 250, **(params or {})}
    cfg = wallwatch.config(sub)
    days = max(30, min(500, int(P["days"])))
    first = (now_ny() - timedelta(days=int(days * 1.45))).strftime("%Y-%m-%d")
    mid = (now_ny() - timedelta(days=int(days * 1.45) // 2)).strftime("%Y-%m-%d")
    db.put({"PK": pk, "SK": PV_SK, **(db.get(pk, PV_SK) or {}), "running": iso(now_ny())})
    rows, base, errors, done_n, trades = [], [], [], 0, []

    def fwd(bars, i):
        e = bars[i]["c"]
        r = {lab: round((bars[i + n]["c"] / e - 1) * 100, 2) if i + n < len(bars) else None for lab, n in PV_H}
        seg = bars[i + 1:i + 36]
        r["mfe"] = round((max(b["h"] for b in seg) / e - 1) * 100, 2) if seg else None
        r["mae"] = round((min(b["l"] for b in seg) / e - 1) * 100, 2) if seg else None
        return r

    def one(sym):
        d = _ticker_data(sub, sym, days, cfg, False)
        if not d:
            return sym, [], []
        bars = d["bars"]
        res = wallwatch.anchored_vwap(bars, int(cfg.get("vwapDays") or wallwatch.VWAP_DAYS))[1]
        kc = wallwatch.keltner(bars, int(cfg.get("kcLen") or 21), float(cfg.get("kcMult") or 3))
        ev = [e for e in wallwatch.pw_vwap_events(bars, d["walls"], res, kc=kc) if e["t"][:10] >= first]
        ttr = wallwatch.pv_long_trades(bars, d["walls"], ev, cfg, d["datr"], kc=kc,
                                       dpl=(d.get("extra") or {}).get("dpLevels"), start=first)
        trades.extend({"sym": sym, "at": t["at"], "R": t["R"], "pct": t["pct"], "exit": t["status"], "days": t["days"],
                       "entry": t["entry"], "pw": t["pw"], "cw": t["cw"]} for t in ttr if t.get("R") is not None)
        out = [{"sym": sym, "at": e["t"], "touch": e["touch"], "above": e["above"], "green": e["green"], "kcHalf": e.get("kcHalf"), "side": e.get("side"), "same": e.get("same"), "cwTouch": e.get("cwTouch"),
                "kcPos": e.get("kcPos"), **fwd(bars, e["i"])} for e in ev]
        bl = [{"at": b["t"], **fwd(bars, i)} for i, b in enumerate(bars) if b["t"][:10] >= first and i % 7 == 3]   # one candle a day
        return sym, out, bl

    pool = ThreadPoolExecutor(max_workers=8)
    for f in [pool.submit(one, t) for t in cfg["tickers"]]:
        try:
            sym, out, bl = f.result(timeout=max(1, budget_s - (time.time() - t0)))
            rows += out
            base += bl
            done_n += 1
        except Exception as e:
            errors.append(str(e)[:100] or "time limit")
    pool.shutdown(wait=False, cancel_futures=True)

    def st(rs):
        out = {"n": len(rs)}
        for lab, _ in PV_H:
            xs = sorted(r[lab] for r in rs if r.get(lab) is not None)
            if xs:
                out[lab] = {"up": round(sum(1 for x in xs if x > 0) / len(xs) * 100), "avg": round(sum(xs) / len(xs), 2),
                            "med": xs[len(xs) // 2]}
        for k in ("mfe", "mae"):
            xs = [r[k] for r in rs if r.get(k) is not None]
            out[k] = round(sum(xs) / len(xs), 2) if xs else None
        h1 = [r["5 days"] for r in rs if r.get("5 days") is not None and r["at"][:10] < mid]
        h2 = [r["5 days"] for r in rs if r.get("5 days") is not None and r["at"][:10] >= mid]
        out["h1"] = round(sum(h1) / len(h1), 2) if h1 else None
        out["h2"] = round(sum(h2) / len(h2), 2) if h2 else None
        out["h1n"], out["h2n"] = len(h1), len(h2)
        return out

    t_ = [r for r in rows if r["touch"] and not r.get("same")]
    groups = [
        ("LONG: reset on the put wall, lower Keltner half", [r for r in rows if r.get("side") == "long"]),
        ("SHORT: reset on the call wall, upper Keltner half (wins when the stock goes down)", [r for r in rows if r.get("side") == "short"]),
        ("All resets touching the put wall (PW ≠ CW)", t_),
        ("   … closes above the put wall", [r for r in t_ if r["above"]]),
        ("   … closes under the put wall", [r for r in t_ if not r["above"]]),
        ("   … green candle", [r for r in t_ if r["green"]]),
        ("   … red candle", [r for r in t_ if not r["green"]]),
        ("   … green and closes above the put wall", [r for r in t_ if r["green"] and r["above"]]),
        ("   … closes in the lower half of the Keltner channel", [r for r in t_ if r.get("kcHalf") == "lower"]),
        ("   … closes in the upper half of the Keltner channel", [r for r in t_ if r.get("kcHalf") == "upper"]),
        ("   … lower Keltner half and closes above the put wall", [r for r in t_ if r.get("kcHalf") == "lower" and r["above"]]),
        ("VWAP reset away from the put wall", [r for r in rows if not r["touch"]]),
        ("Any day (baseline)", base),
    ]
    out = {"PK": pk, "SK": PV_SK, "running": None, "at": iso(now_ny()), "secs": round(time.time() - t0), "tickers": done_n,
           "period": {"from": first, "mid": mid}, "groups": [{"label": lab, **st(rs)} for lab, rs in groups],
           "recent": sorted([r for r in rows if r.get("side")], key=lambda r: r["at"])[-40:],
           "trades": {**_stats(trades, mid), "exits": {k: sum(1 for t in trades if t["exit"] == k) for k in sorted({t["exit"] for t in trades})},
                      "avgDays": round(sum(t["days"] for t in trades) / len(trades), 1) if trades else None,
                      "list": sorted(trades, key=lambda t: t["at"])[-60:]}, "errors": errors[:5]}
    if done_n < len(cfg["tickers"]):
        out["errors"].insert(0, f"Time limit: {done_n} of {len(cfg['tickers'])} tickers.")
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


def pv_result(sub):
    r = _unstick(db.get(db.upk(sub), PV_SK) or {})
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


def PARAMS_KEYS(cfg):
    import wallwatch
    return [k for k in wallwatch.PARAMS if k in cfg]


def robust_result(sub):
    r = _unstick(db.get(db.upk(sub), ROB_SK) or {})
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


def _unstick(r):
    """A run the 10-minute limit killed never cleared its flag: after 12 minutes it isn't running any more."""
    if r.get("running") and r["running"] < iso(now_ny() - timedelta(minutes=12)):
        r = {**r, "running": None, "errors": ["The last run hit the 10-minute limit and stopped: run it again."] + list(r.get("errors") or [])}
    return r


def test_result(sub):
    r = _unstick(db.get(db.upk(sub), TEST_SK) or {})
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}
