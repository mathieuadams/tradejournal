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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
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


ALERTS_V = 2   # 2: alerts whose contract has expired since are kept (they were dropped before)


def _fetch_alerts(sub, sym, days):
    """Call alerts >= $100K bought at the ask, as far back as Unusual Whales pages (stored by month, kept for good).
    A ticker already loaded for at least `days` is only topped up since its last load."""
    import flowdata
    pk = db.upk(sub)
    meta = db.get(pk, f"WWHMETA#{sym}") or {}
    cal = int(days * 1.45) + 3
    if meta.get("v") != ALERTS_V:                      # loaded before the expired-contract fix: load it all again
        meta = {}
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
    db.put({"PK": pk, "SK": f"WWHMETA#{sym}", "v": ALERTS_V, "days": max(int(meta.get("days") or 0), days), "at": iso(now_ny()),
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
    ("greenOnly", "Green candle"), ("aboveFlip", "Above the flip"), ("abovePW", "Above the put wall"), ("belowPW", "Below the put wall"), ("cvdUp", "CVD > 0"), ("cvdDn", "CVD < 0"), ("vwapOn", "Above VWAP"), ("emaOn", "Above EMA"),
    ("emaOverVwap", "EMA > VWAP"), ("noAboveCW", "Not above CW"), ("cwRoomOn", "Room to CW"),
    ("skewOn", "Skew normal (middle third of 6 months)"), ("spyUp", "SPY trend up (6 days)"), ("kcNotExt", "Under the upper Keltner band"), ("orbAbove", "Above the opening range high"),
]
OFF = {"usePW": True, "useFlip": True, "greenOnly": False, "aboveFlip": False, "abovePW": False, "belowPW": False, "cvdUp": False, "cvdDn": False, "vwapOn": False, "emaOn": False,
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


def _ticker_data(sub, sym, days, cfg, full=True, alerts=True):
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
    kc = wallwatch.keltner(bars, int(cfg.get("kcLen") or 21), float(cfg.get("kcMult") or 3), bool(cfg.get("kcDaily", True)))
    dp = stored_darkpool(sym)
    days_ = sorted({b["t"][:10] for b in bars})
    sk = stored_skew(sym) if full or cfg.get("skewOn") else {}
    return {"bars": bars, "datr": wallwatch.daily_atr(daily), "walls": walls, "alerts": stored_alerts(sub, sym) if alerts else [],
            "skew": sk, "dp": dp,
            "vmap": {b["t"][:16]: v for b, v in zip(bars, vw)}, "emap": {b["t"][:16]: v for b, v in zip(bars, em)},
            "extra": {"kc": {b["t"][:16]: v for b, v in zip(bars, kc)},
                      "kch": {b["t"][:16]: v for b, v in zip(bars, wallwatch.keltner(bars, int(cfg.get("kchLen") or 21), float(cfg.get("kchMult") or 3)))},
                      "inr": {b["t"][:16]: v for b, v in zip(bars, wallwatch.inertia_regime(bars, (int(cfg.get("inN1") or 70), int(cfg.get("inN2") or 40), int(cfg.get("inN3") or 20))))},
                      "spy": wallwatch.spy_trend(start, now) if full or cfg.get("spyUp") else {},
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
        out.append({"sym": sym, "at": s["at"], "R": s["R"], "pct": s["pct"], "exit": s["status"], "exitAt": s.get("exitAt"), "level": s.get("levelName"),
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


def _with_lab(cfg, P):
    """Settings for a test: the live ones, or the Strategy lab's when P["cfg"] is given (nothing is saved)."""
    import wallwatch
    lab = P.get("cfg") if isinstance(P.get("cfg"), dict) else None
    if not lab:
        return cfg
    return {**cfg, **{k: v for k, v in lab.items() if k in wallwatch.PARAMS and k != "pvSetup"}}


def rule_test(sub, params=None, budget_s=480):
    import wallwatch
    t0 = time.time()
    pk = db.upk(sub)
    P = {"days": 250, "stopAtr": None, "fixR": 2.0, "maxDays": 5, "retestMin": None, **(params or {})}
    cfg = _with_lab(wallwatch.config(sub), P)
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
    mine_entry.update(aboveFlip=cfg.get("aboveFlip"), abovePW=cfg.get("abovePW"), belowPW=cfg.get("belowPW"), cvdUp=cfg.get("cvdUp"), cvdDn=cfg.get("cvdDn"), earnOn=False)      # no earnings history: off in the test
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
    cfg = _with_lab(wallwatch.config(sub), P)
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
    on = lambda k: bool(cfg.get(k, wallwatch.PARAMS.get(k)))
    if on("alertReq"):                                 # flow numbers only matter when a call alert is required
        variants += [(f"Call alert ≥ ${int(v / 1000)}K (you: ${int(vmin / 1000)}K)", {"retestMin": v}) for v in (vmin * 1.4,)]
        variants += [(f"Wait {v} days (you: {wait})", {"retestDays": v}) for v in (max(1, wait - 1), wait + 1)]
        if on("relFlow"):
            rm = float(cfg.get("relMult") or 3)
            variants += [(f"Unusual × {v:g} median (you: {rm:g})", {"relMult": v}) for v in (max(1.5, rm - 1), rm + 1)]
    if on("trailOn"):
        tr = float(cfg.get("trailAtr") or 1)
        variants += [(f"Trailing stop {v:g} ATR (you: {tr:g})", {"trailAtr": v}) for v in (round(tr * 0.75, 2), round(tr * 1.25, 2))]
    if on("kcNotExt") or on("kcdLower") or on("kcRide"):
        kl = int(cfg.get("kcLen") or 21)
        variants += [(f"Keltner daily EMA {v} (you: {kl})", {"kcLen": v}) for v in (max(5, kl - 5), kl + 5)]
    if on("kchLower"):
        hm = float(cfg.get("kchMult") or 3)
        variants += [(f"Keltner hourly × {v:g} (you: {hm:g})", {"kchMult": v}) for v in (max(0.5, hm - 0.5), hm + 0.5)]
    if on("fvgOn") or on("fvgExit"):
        fm, fn, fd = float(cfg.get("fvgMin") or 0.2), float(cfg.get("fvgNear") if cfg.get("fvgNear") is not None else 0.25), int(cfg.get("fvgDays") or 10)
        variants += [(f"FVG min size {v:g} ATR (you: {fm:g})", {"fvgMin": v}) for v in (round(fm * 0.5, 3), round(fm * 1.5, 3))]
        if on("fvgOn"):
            variants += [(f"FVG margin {v:g} ATR (you: {fn:g})", {"fvgNear": v}) for v in (round(fn * 0.5, 3), round(fn * 1.5, 3))]
            variants += [(f"FVG max age {v} days (you: {fd})", {"fvgDays": v}) for v in (max(1, fd // 2), fd * 2)]
    if on("vwapOn") or on("vwapReset") or on("vwapExit") or on("emaOverVwap"):
        vd = int(cfg.get("vwapDays") or 10)
        variants += [(f"VWAP reset {v} days (you: {vd})", {"vwapDays": v}) for v in (max(3, vd - 3), vd + 3)]
    if on("emaOn") or on("emaOverVwap") or on("emaExit"):
        el = int(cfg.get("emaLen") or 20)
        variants += [(f"EMA {v} (you: {el})", {"emaLen": v}) for v in (max(5, el - 5), el + 5)]
    if on("inGreen") or on("inNotRed") or on("inRedExit"):
        ns_ = (int(cfg.get("inN1") or 70), int(cfg.get("inN2") or 40), int(cfg.get("inN3") or 20))
        for f_ in (0.75, 1.25):
            nn = [max(5, round(x * f_)) for x in ns_]
            variants.append((f"Inertia {nn[0]}/{nn[1]}/{nn[2]} days (you: {ns_[0]}/{ns_[1]}/{ns_[2]})", {"inN1": nn[0], "inN2": nn[1], "inN3": nn[2]}))
    if on("scaleOn"):
        sr, sp = float(cfg.get("scaleR") or 1), float(cfg.get("scalePct") or 50)
        variants += [(f"Sell part at {v:g}R (you: {sr:g})", {"scaleR": v}) for v in (max(0.25, sr - 0.25), sr + 0.25)]
        variants += [(f"Sell {v:g}% (you: {sp:g})", {"scalePct": v}) for v in (max(5, sp - 15), min(95, sp + 15))]
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
            d2 = d
            if "kcMult" in ch or "kcLen" in ch:         # the daily Keltner bands themselves change
                kc = wallwatch.keltner(d["bars"], int(c.get("kcLen") or 21), float(c.get("kcMult") or 3), bool(cfg.get("kcDaily", True)))
                d2 = {**d2, "extra": {**d2["extra"], "kc": {b["t"][:16]: v for b, v in zip(d["bars"], kc)}}}
            if "kchMult" in ch:                         # the hourly Keltner bands
                kh = wallwatch.keltner(d["bars"], int(c.get("kchLen") or 21), float(c["kchMult"]))
                d2 = {**d2, "extra": {**d2["extra"], "kch": {b["t"][:16]: v for b, v in zip(d["bars"], kh)}}}
            if "vwapDays" in ch:                        # the anchored VWAP restarts on another window
                vw = wallwatch.anchored_vwap(d["bars"], int(ch["vwapDays"]))[0]
                d2 = {**d2, "vmap": {b["t"][:16]: v for b, v in zip(d["bars"], vw)}}
            if "inN1" in ch:                            # the daily inertia on other lengths
                ir = wallwatch.inertia_regime(d["bars"], (int(ch["inN1"]), int(ch["inN2"]), int(ch["inN3"])))
                d2 = {**d2, "extra": {**d2["extra"], "inr": {b["t"][:16]: v for b, v in zip(d["bars"], ir)}}}
            if "emaLen" in ch:
                em = wallwatch._ema_series([b["c"] for b in d["bars"]], int(ch["emaLen"]))
                d2 = {**d2, "emap": {b["t"][:16]: v for b, v in zip(d["bars"], em)}}
            out[lab] = _run(sym, d2, c, first)
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
        kc = wallwatch.keltner(bars, int(cfg.get("kcLen") or 21), float(cfg.get("kcMult") or 3), bool(cfg.get("kcDaily", True)))
        kch = wallwatch.keltner(bars, int(cfg.get("kchLen") or 21), float(cfg.get("kchMult") or 3))
        ev = [e for e in wallwatch.pw_vwap_events(bars, d["walls"], res, kc=kch) if e["t"][:10] >= first]
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


REG_SK = "WWREGIME"
REG_H = [("1 day", 1), ("3 days", 3), ("5 days", 5), ("10 days", 10)]


def _daily(bars):
    """Daily candles from hourly ones: [{d, o, h, l, c}]."""
    out = []
    for b in bars:
        d = b["t"][:10]
        if not out or out[-1]["d"] != d:
            out.append({"d": d, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
        else:
            x = out[-1]
            x["h"], x["l"], x["c"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"]
    return out


def regime_rows(daily, walls, first):
    """One row per day with stored walls: the wall regime, where the close sits, where the flip sits, the change of
    the walls vs the previous stored day, and the forward moves from that close (no look-ahead in the labels)."""
    rows, prev = [], None
    for i, x in enumerate(daily):
        w = walls.get(x["d"]) or {}
        pw, cw, fl = (float(w[k]) if w.get(k) else None for k in ("putWall", "callWall", "flip"))
        if not pw or not cw:
            prev = None
            continue
        c = x["c"]
        same = abs(cw - pw) < 1e-6
        lo, hi = min(pw, cw), max(pw, cw)
        r = {"d": x["d"], "regime": "same" if same else "normal" if cw > pw else "inverted",
             "px": ("below" if c < lo else "above" if c > hi else "between"),
             "flipSide": None if not fl else ("above" if c >= fl else "below"),
             "flipLoc": None if not fl or same else ("below" if fl < lo else "above" if fl > hi else "between")}
        if prev:
            ppw, pcw = prev
            r["cwToPw"] = abs(pw - pcw) / pcw <= 0.005 and pw > ppw * 1.002      # yesterday's call wall is today's put wall
            r["pwDown"], r["pwUp"] = pw < ppw * 0.995, pw > ppw * 1.005
            r["cwDown"], r["cwUp"] = cw < pcw * 0.995, cw > pcw * 1.005
            r["merged"] = same and abs(pcw - ppw) > 1e-6
            r["inverted"] = cw < pw and not pcw < ppw
            r["normal"] = cw > pw and not pcw > ppw
        prev = (pw, cw)
        if x["d"] < first:
            continue
        for lab, n in REG_H:
            r[lab] = round((daily[i + n]["c"] / c - 1) * 100, 2) if i + n < len(daily) else None
        seg = daily[i + 1:i + 6]
        r["mfe"] = round((max(y["h"] for y in seg) / c - 1) * 100, 2) if seg else None
        r["mae"] = round((min(y["l"] for y in seg) / c - 1) * 100, 2) if seg else None
        rows.append(r)
    return rows


def _streaks(rows, key):
    """Average length in days of a run of the same regime (per ticker, consecutive stored days)."""
    runs, cur, n = {}, None, 0
    for r in rows + [{"regime": None, "sym": None}]:
        k = (r.get("sym"), r.get(key))
        if k == cur:
            n += 1
            continue
        if cur and cur[1]:
            runs.setdefault(cur[1], []).append(n)
        cur, n = k, 1
    return {k: round(sum(v) / len(v), 1) for k, v in runs.items()}


def regime_study(sub, params=None, budget_s=480):
    """How the stock moves after each gamma wall configuration: call wall above / equal to / under the put wall, the
    close vs the walls and the flip, and the day the walls move (yesterday's call wall becomes the put wall, the put
    wall steps down...). Daily closes, forward 1/3/5/10 days, best / worst move over 5 days."""
    import wallwatch
    t0 = time.time()
    pk = db.upk(sub)
    P = {"days": 250, **(params or {})}
    cfg = wallwatch.config(sub)
    days = max(30, min(500, int(P["days"])))
    first = (now_ny() - timedelta(days=int(days * 1.45))).strftime("%Y-%m-%d")
    mid = (now_ny() - timedelta(days=int(days * 1.45) // 2)).strftime("%Y-%m-%d")
    db.put({"PK": pk, "SK": REG_SK, **(db.get(pk, REG_SK) or {}), "running": iso(now_ny())})
    rows, errors, done_n = [], [], 0

    def one(sym):
        d = _ticker_data(sub, sym, days, cfg, False)
        if not d:
            return []
        return [{**r, "sym": sym} for r in regime_rows(_daily(d["bars"]), d["walls"], first)]

    pool = ThreadPoolExecutor(max_workers=8)
    for f in [pool.submit(one, t) for t in cfg["tickers"]]:
        try:
            rows += f.result(timeout=max(1, budget_s - (time.time() - t0)))
            done_n += 1
        except Exception as e:
            errors.append(str(e)[:100] or "time limit")
    pool.shutdown(wait=False, cancel_futures=True)
    total = len(rows) or 1

    def st(rs):
        out = {"n": len(rs), "share": round(len(rs) / total * 100, 1)}
        for lab, _ in REG_H:
            xs = sorted(r[lab] for r in rs if r.get(lab) is not None)
            if xs:
                out[lab] = {"up": round(sum(1 for x in xs if x > 0) / len(xs) * 100), "avg": round(sum(xs) / len(xs), 2),
                            "med": xs[len(xs) // 2]}
        for k in ("mfe", "mae"):
            xs = [r[k] for r in rs if r.get(k) is not None]
            out[k] = round(sum(xs) / len(xs), 2) if xs else None
        h1 = [r["5 days"] for r in rs if r.get("5 days") is not None and r["d"] < mid]
        h2 = [r["5 days"] for r in rs if r.get("5 days") is not None and r["d"] >= mid]
        out["h1"] = round(sum(h1) / len(h1), 2) if h1 else None
        out["h2"] = round(sum(h2) / len(h2), 2) if h2 else None
        out["h1n"], out["h2n"] = len(h1), len(h2)
        return out

    R = lambda **kw: [r for r in rows if all(r.get(k) == v for k, v in kw.items())]
    sections = [
        ("Wall regime", [
            ("CW above PW (normal)", R(regime="normal")),
            ("CW = PW (same strike)", R(regime="same")),
            ("CW under PW (inverted)", R(regime="inverted"))]),
        ("CW above PW: where the close is", [
            ("Under the put wall", R(regime="normal", px="below")),
            ("Between PW and CW", R(regime="normal", px="between")),
            ("Above the call wall", R(regime="normal", px="above"))]),
        ("CW = PW: where the close is", [
            ("Under the wall", R(regime="same", px="below")),
            ("Above the wall", R(regime="same", px="above"))]),
        ("CW under PW: where the close is", [
            ("Under both walls", R(regime="inverted", px="below")),
            ("Between CW and PW", R(regime="inverted", px="between")),
            ("Above both walls", R(regime="inverted", px="above"))]),
        ("The flip", [
            ("Close above the flip", R(flipSide="above")),
            ("Close under the flip", R(flipSide="below")),
            ("CW > PW, flip between the walls, close above it", R(regime="normal", flipLoc="between", flipSide="above")),
            ("CW > PW, flip between the walls, close under it", R(regime="normal", flipLoc="between", flipSide="below")),
            ("CW > PW, flip under the put wall", R(regime="normal", flipLoc="below")),
            ("CW > PW, flip above the call wall", R(regime="normal", flipLoc="above")),
            ("CW < PW, close above the flip", R(regime="inverted", flipSide="above")),
            ("CW < PW, close under the flip", R(regime="inverted", flipSide="below"))]),
        ("The day the walls move", [
            ("Yesterday's CW became today's PW", R(cwToPw=True)),
            ("PW stepped down", R(pwDown=True)),
            ("PW stepped up", R(pwUp=True)),
            ("CW stepped up", R(cwUp=True)),
            ("CW stepped down", R(cwDown=True)),
            ("Walls merged (CW = PW starts)", R(merged=True)),
            ("Walls turned inverted (CW < PW starts)", R(inverted=True)),
            ("Back to normal (CW > PW starts)", R(normal=True))]),
        ("Baseline", [("Every day", rows)]),
    ]
    rows.sort(key=lambda r: (r["sym"], r["d"]))
    out = {"PK": pk, "SK": REG_SK, "running": None, "at": iso(now_ny()), "secs": round(time.time() - t0), "tickers": done_n,
           "days": len(rows), "period": {"from": first, "mid": mid}, "streaks": _streaks(rows, "regime"),
           "sections": [{"title": t, "groups": [{"label": lab, **st(rs)} for lab, rs in gs]} for t, gs in sections],
           "errors": errors[:5]}
    if done_n < len(cfg["tickers"]):
        out["errors"].insert(0, f"Time limit: {done_n} of {len(cfg['tickers'])} tickers.")
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


LAB_SK = "WWLAB"


def lab_test(sub, params=None, budget_s=480):
    """Strategy lab: the settings being tried (not saved) vs the live settings, on every ticker of the watchlist,
    each trade held with its own exits (open trades at their last price)."""
    import wallwatch
    t0 = time.time()
    pk = db.upk(sub)
    P = {"days": 250, "cfg": {}, **(params or {})}
    live = wallwatch.config(sub)
    lab = {**live, **{k: v for k, v in (P.get("cfg") or {}).items() if k in wallwatch.PARAMS and k != "pvSetup"}}
    days = max(30, min(500, int(P["days"])))
    first = (now_ny() - timedelta(days=int(days * 1.45))).strftime("%Y-%m-%d")
    mid = (now_ny() - timedelta(days=int(days * 1.45) // 2)).strftime("%Y-%m-%d")
    db.put({"PK": pk, "SK": LAB_SK, **(db.get(pk, LAB_SK) or {}), "running": iso(now_ny())})
    rows_l, rows_v, errors, done_n = [], [], [], 0

    def one(sym):
        d = _ticker_data(sub, sym, days, lab, False)
        if not d:
            return [], []
        return _run0(sym, d, {**lab, "earnOn": False}, first), _run0(sym, d, {**live, "earnOn": False}, first)

    def one_retry(sym):
        try:
            return one(sym)
        except Exception:                              # market data rate limit: wait and try once more
            time.sleep(8)
            return one(sym)

    pool = ThreadPoolExecutor(max_workers=6)
    failed, timed_out = [], 0
    for t_, f in [(t, pool.submit(one_retry, t)) for t in live["tickers"]]:
        try:
            a, b = f.result(timeout=max(1, budget_s - (time.time() - t0)))
            rows_l += a
            rows_v += b
            done_n += 1
        except FutureTimeout:
            timed_out += 1
        except Exception as e:
            failed.append(t_)
            errors.append(f"{t_}: {str(e)[:80]}")
    pool.shutdown(wait=False, cancel_futures=True)

    def pack(rows):
        st = _stats(rows, mid)
        st["exits"] = {k: sum(1 for r in rows if r["exit"] == k) for k in sorted({r["exit"] for r in rows})}
        by = {}
        for r in rows:
            by.setdefault(r["sym"], []).append(r["R"])
        st["top"] = sorted(({"sym": k, "n": len(v), "totR": round(sum(v), 2)} for k, v in by.items()), key=lambda x: -x["totR"])
        tot = sum(x["totR"] for x in st["top"])
        st["top3Share"] = round(sum(x["totR"] for x in st["top"][:3]) / tot * 100) if tot > 0 else None
        st["top"] = st["top"][:5] + st["top"][-3:] if len(st["top"]) > 8 else st["top"]
        return st

    out = {"PK": pk, "SK": LAB_SK, "running": None, "at": iso(now_ny()), "secs": round(time.time() - t0), "tickers": done_n,
           "period": {"from": first, "mid": mid}, "cfg": P.get("cfg") or {}, "lab": pack(rows_l), "live": pack(rows_v),
           "trades": sorted(rows_l, key=lambda r: r["at"])[-400:], "errors": errors[:5]}
    if timed_out:
        out["errors"].insert(0, f"Time limit: {done_n} of {len(live['tickers'])} tickers.")
    if failed:
        out["errors"].insert(0, f"No market data for {len(failed)} ticker{'s' if len(failed) > 1 else ''} ({', '.join(failed[:8])}) — tested {done_n} of {len(live['tickers'])}.")
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


def lab_result(sub):
    r = _unstick(db.get(db.upk(sub), LAB_SK) or {})
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


SEARCH_SK = "WWSEARCH"


def _replay(cands_by_sym, need, mid):
    """One trade at a time per ticker, keeping only the candles that pass every rule in need."""
    rows = []
    for sym, cs in cands_by_sym.items():
        last = ""
        for c in cs:
            if c["at"] <= last or not all(c["f"].get(r) for r in need):
                continue
            rows.append({"at": c["at"], "R": c["R"], "sym": sym})
            last = c["end"]
    st = _stats(rows, mid)
    return st


def rule_search(sub, params=None, budget_s=480):
    """Search the mixes of entry rules: every candle at the support is traded alone with your exits and tagged with
    each rule; any mix of rules is then replayed one trade at a time per ticker. Single rules, pairs, and a step-by-step
    search (add the rule that helps most, again and again), on the live settings or the Strategy lab's."""
    import wallwatch
    from itertools import combinations
    t0 = time.time()
    pk = db.upk(sub)
    P = {"days": 250, "minTrades": 60, **(params or {})}
    cfg = _with_lab(wallwatch.config(sub), P)
    days = max(30, min(500, int(P["days"])))
    min_n = max(10, int(P.get("minTrades") or 60))
    first = (now_ny() - timedelta(days=int(days * 1.45))).strftime("%Y-%m-%d")
    mid = (now_ny() - timedelta(days=int(days * 1.45) // 2)).strftime("%Y-%m-%d")
    db.put({"PK": pk, "SK": SEARCH_SK, **(db.get(pk, SEARCH_SK) or {}), "running": iso(now_ny())})
    rules = list(wallwatch.SEARCH_RULES)
    cands, errors, done_n = {}, [], 0
    base_cfg = {**cfg, "earnOn": False, "fixR": 0, "maxDays": 0}

    def one(sym):
        d = _ticker_data(sub, sym, days, base_cfg, False)
        if not d:
            return sym, []
        out = []
        wallwatch.detect_retest(d["bars"], d["walls"], first, {**base_cfg, "_collect": out}, d["datr"], d["alerts"], None,
                                d["vmap"], d["emap"], d.get("extra"))
        return sym, sorted(out, key=lambda c: c["at"])

    pool = ThreadPoolExecutor(max_workers=8)
    for f in [pool.submit(one, t) for t in cfg["tickers"]]:
        try:
            sym, cs = f.result(timeout=max(1, budget_s * 0.8 - (time.time() - t0)))
            if cs:
                cands[sym] = cs
            done_n += 1
        except Exception as e:
            errors.append(str(e)[:100] or "time limit")
    pool.shutdown(wait=False, cancel_futures=True)

    seen = {}
    def ev(need):
        k = tuple(sorted(need))
        if k not in seen:
            seen[k] = _replay(cands, k, mid)
        return seen[k]
    ok = lambda st: st.get("n", 0) >= min_n
    score = lambda st: st.get("avgR") if ok(st) and st.get("avgR") is not None else -9

    mine = [r for r in rules if cfg.get(r, wallwatch.PARAMS.get(r))]
    out_rows = {"base": {"rules": [], **ev(())}, "mine": {"rules": mine, **ev(mine)}}
    singles = []
    for r in rules:                                    # each rule turned on / off from your settings
        alt = [x for x in mine if x != r] if r in mine else mine + [r]
        singles.append({"rule": r, "change": "off" if r in mine else "on", "rules": alt, **ev(alt)})
    pairs = []
    for a, b in combinations(rules, 2):                # every pair, from the base
        if time.time() - t0 > budget_s:
            break
        pairs.append({"rules": [a, b], **ev((a, b))})
    path, cur = [], []
    for _ in range(8):                                 # step by step: add the rule that raises the average R the most
        best = max(((r, ev(cur + [r])) for r in rules if r not in cur), key=lambda x: score(x[1]), default=None)
        if not best or score(best[1]) <= score(ev(cur)) or not ok(best[1]):
            break
        cur = cur + [best[0]]
        path.append({"rules": list(cur), **best[1]})
    allc = [{"rules": list(k), **v} for k, v in seen.items() if ok(v)]
    both = [x for x in allc if (x.get("h1") or 0) > 0 and (x.get("h2") or 0) > 0]
    top, sig = [], set()                               # a rule that changes no trade is dropped: the shortest mix is kept
    for x in sorted(both, key=lambda x: (-(x.get("avgR") or 0), len(x["rules"]))):
        k = (x.get("n"), x.get("avgR"), x.get("totR"), x.get("h1"), x.get("h2"))
        if k in sig:
            continue
        sig.add(k)
        top.append(x)
        if len(top) >= 15:
            break
    out = {"PK": pk, "SK": SEARCH_SK, "running": None, "at": iso(now_ny()), "secs": round(time.time() - t0), "tickers": done_n,
           "candidates": sum(len(v) for v in cands.values()), "tested": len(seen), "minTrades": min_n, "period": {"from": first, "mid": mid},
           "lab": bool(P.get("cfg")), "base": out_rows["base"], "mine": out_rows["mine"], "singles": singles, "path": path, "top": top,
           "pairs": sorted([p_ for p_ in pairs if ok(p_)], key=lambda x: -(x.get("avgR") or 0))[:10], "errors": errors[:5]}
    if done_n < len(cfg["tickers"]):
        out["errors"].insert(0, f"Time limit: {done_n} of {len(cfg['tickers'])} tickers.")
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


def search_result(sub):
    r = _unstick(db.get(db.upk(sub), SEARCH_SK) or {})
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


def regime_result(sub):
    r = _unstick(db.get(db.upk(sub), REG_SK) or {})
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


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
