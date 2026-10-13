"""Intraday gamma-level test (the NQ day-trading framework, on 5-minute candles of QQQ / SPY / your tickers).

Levels of the day: put wall, gamma flip, call wall (Unusual Whales, from that day's open interest).
Session: entries only in the first hours (default 09:30-11:30), flat at the close.

Long  at a put wall / flip reached from above:
  - slow approach: a candle touches the level and closes back above it (rejection) -> buy the close;
  - fast approach (the last 30 min moved more than N x the 5-min ATR): wait for a close under the level (overshoot),
    then a close back above it (reclaim) -> buy the close.
Short at a call wall / flip reached from below: the mirror.
Stop: beyond the low (high) of the reaction / overshoot + a buffer. Target: the next level (flip or the opposite wall),
skipped when it gives less than the minimum reward : risk. A level that stopped a trade is not traded again that day,
at most N trades a day, one at a time. Option expiration days (monthly OPEX, triple witching, VIX expiration) can be skipped.

Not testable here: futures order flow, 0DTE-only gamma, volatility surface / skew, convexity (no data).
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import db
from util import iso, now_ny

SK = "WWINTRA"
DEFAULTS = {"tickers": "QQQ SPY", "watchlist": False, "sessionEnd": "11:30", "fastK": 4.0, "appBars": 6,
            "zoneK": 0.5, "bufK": 0.25, "minRR": 1.5, "maxRiskD": 0.5, "maxTrades": 2, "skipEvents": True,
            "longs": True, "shorts": True, "useFlip": True}


# ---------------- calendar ----------------

def _third_friday(y, m):
    d = date(y, m, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def event_days(y0, y1):
    """{date: name}: monthly options expiration (3rd Friday), triple witching (Mar/Jun/Sep/Dec), VIX expiration
    (the Wednesday 30 days before the next month's 3rd Friday)."""
    out = {}
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            f = _third_friday(y, m)
            out[f.isoformat()] = "triple witching" if m in (3, 6, 9, 12) else "monthly OPEX"
            ny, nm = (y + (m == 12), 1 if m == 12 else m + 1)
            v = _third_friday(ny, nm) - timedelta(days=30)
            out.setdefault(v.isoformat(), "VIX expiration")
    return out


# ---------------- simulation ----------------

def _atr(bars, i, n=14):
    lo = max(1, i - n + 1)
    trs = [max(bars[k]["h"] - bars[k]["l"], abs(bars[k]["h"] - bars[k - 1]["c"]), abs(bars[k]["l"] - bars[k - 1]["c"]))
           for k in range(lo, i + 1)]
    return sum(trs) / len(trs) if trs else bars[i]["h"] - bars[i]["l"]


def simulate(bars, walls, datr, P, events=None, sym=""):
    """bars: 5-minute candles (several days, regular hours); walls: {date: {putWall, flip, callWall}};
    datr: {date: daily ATR}. Returns the trades."""
    P = {**DEFAULTS, **(P or {})}
    days = {}
    for i, b in enumerate(bars):
        days.setdefault(b["t"][:10], []).append(i)
    trades = []
    for d, idx in sorted(days.items()):
        w = walls.get(d) or {}
        if not (w.get("putWall") and w.get("callWall")):
            continue
        if P["skipEvents"] and events and d in events:
            continue
        dA = datr.get(d) if datr else None
        lv = [("put wall", float(w["putWall"])), ("call wall", float(w["callWall"]))]
        if P["useFlip"] and w.get("flip"):
            lv.append(("flip", float(w["flip"])))
        lv.sort(key=lambda x: x[1])
        burned, n_tr, open_ = set(), 0, None
        state = {}                                     # level -> {"over": i} after a close beyond it (fast approach)
        for k, i in enumerate(idx):
            b = bars[i]
            hm = b["t"][11:16]
            if open_:
                t = open_
                L = t["side"] == "long"
                hit_stop = b["l"] <= t["stop"] if L else b["h"] >= t["stop"]
                hit_1r = b["h"] >= t["r1"] if L else b["l"] <= t["r1"]
                hit_tgt = b["h"] >= t["target"] if L else b["l"] <= t["target"]
                if not t.get("half") and hit_1r and not hit_stop:
                    t["half"] = True                   # scale-out variant: half at 1R, stop of the rest to breakeven
                if hit_stop:
                    ex = min(b["o"], t["stop"]) if L else max(b["o"], t["stop"])
                    _close(t, ex, b["t"], "stop")
                elif hit_tgt:
                    _close(t, t["target"], b["t"], "target")
                elif hm >= "15:55" or k == len(idx) - 1:
                    _close(t, b["c"], b["t"], "close")
                if t.get("status"):
                    trades.append(t)
                    if t["status"] == "stop":
                        burned.add(t["level"])
                    open_ = None
                continue
            if k < P["appBars"] or hm > P["sessionEnd"] or n_tr >= P["maxTrades"] or hm >= "15:30":
                continue
            a5 = _atr(bars, i)
            if a5 <= 0:
                continue
            zone, buf = P["zoneK"] * a5, P["bufK"] * a5
            prev = bars[idx[k - 1]]
            back = bars[idx[k - P["appBars"]]]
            for name, L in lv:
                key = (name, L)
                if key in burned:
                    continue
                for side in ("long", "short"):
                    if not P["longs" if side == "long" else "shorts"]:
                        continue
                    if side == "long" and name == "call wall" or side == "short" and name == "put wall":
                        continue
                    up = side == "long"
                    move = (back["c"] - b["l"]) if up else (b["h"] - back["c"])        # speed of the approach
                    fast = move > P["fastK"] * a5
                    came = back["c"] > L if up else back["c"] < L                      # came to the level from the right side
                    st = state.setdefault((key, side), {})
                    beyond = b["c"] < L if up else b["c"] > L
                    if beyond and came:
                        st["over"] = k                                                 # closed through the level
                        st["ext"] = min(st.get("ext", b["l"]), b["l"]) if up else max(st.get("ext", b["h"]), b["h"])
                    entry = None
                    if st.get("over") is not None and k - st["over"] <= 6 and not beyond and (b["c"] > L if up else b["c"] < L):
                        entry, kind, ext = b["c"], "reclaim", st["ext"]                 # overshoot then reclaim
                        st.clear()
                    elif not fast and came and (b["l"] <= L + zone and b["c"] > L and b["c"] > b["o"] if up
                                                else b["h"] >= L - zone and b["c"] < L and b["c"] < b["o"]):
                        entry, kind, ext = b["c"], "rejection", (min(b["l"], prev["l"]) if up else max(b["h"], prev["h"]))
                    if entry is None:
                        continue
                    stop = ext - buf if up else ext + buf
                    risk = (entry - stop) if up else (stop - entry)
                    if risk <= 0 or (dA and risk > P["maxRiskD"] * dA):
                        continue
                    nxt = [x for x in lv if (x[1] > entry + 0.25 * a5 if up else x[1] < entry - 0.25 * a5) and x[1] != L]
                    if not nxt:
                        continue
                    tgt = min(x[1] for x in nxt) if up else max(x[1] for x in nxt)
                    rr = ((tgt - entry) if up else (entry - tgt)) / risk
                    if rr < P["minRR"]:
                        continue
                    open_ = {"sym": sym, "at": b["t"][:16], "side": side, "level": key, "levelName": name, "levelPx": L,
                             "kind": kind, "fast": fast, "entry": entry, "stop": round(stop, 4), "risk": risk,
                             "target": tgt, "rr": round(rr, 2), "r1": entry + risk if up else entry - risk, "event": (events or {}).get(d)}
                    n_tr += 1
                    break
                if open_:
                    break
        if open_:                                      # day ended without an exit candle (data gap)
            _close(open_, bars[idx[-1]]["c"], bars[idx[-1]]["t"], "close")
            trades.append(open_)
    for t in trades:
        t.pop("level", None)
    return trades


def _close(t, px, at, why):
    up = t["side"] == "long"
    r = ((px - t["entry"]) if up else (t["entry"] - px)) / t["risk"]
    t.update(status=why, exit=round(px, 4), exitAt=at[:16], R=round(r, 3))
    # scale-out: half at 1R, the rest with the stop at breakeven
    if t.get("half"):
        rest = r if why != "stop" else 0.0
        t["Rscale"] = round(0.5 + 0.5 * rest, 3)
    else:
        t["Rscale"] = t["R"]


# ---------------- run ----------------

def _stats(rows, mid, key="R"):
    n = len(rows)
    if not n:
        return {"n": 0}
    rs = [r[key] for r in rows]
    a = [r[key] for r in rows if r["at"][:10] < mid]
    b = [r[key] for r in rows if r["at"][:10] >= mid]
    avg = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    wins = [x for x in rs if x > 0]
    return {"n": n, "win": round(len(wins) / n * 100, 1), "avgR": avg(rs), "totR": round(sum(rs), 1),
            "h1": avg(a), "h1n": len(a), "h2": avg(b), "h2n": len(b)}


def run(sub, params=None, budget_s=540):
    import flowdata
    import wallwatch
    from charts import _yahoo
    t0 = time.time()
    pk = db.upk(sub)
    P = {**DEFAULTS, **(params or {})}
    syms = [x.strip().upper() for x in str(P["tickers"]).replace(",", " ").split() if x.strip()]
    if P.get("watchlist"):
        syms += [t for t in wallwatch.config(sub)["tickers"] if t not in syms]
    syms = syms[:90]
    now = now_ny().replace(tzinfo=None)
    start = now - timedelta(days=58)                   # Yahoo keeps ~60 days of 5-minute candles
    events = event_days(start.year, now.year + 1)
    key = flowdata._key(sub)
    db.put({"PK": pk, "SK": SK, **(db.get(pk, SK) or {}), "running": iso(now_ny()), "params": P})
    errors = []

    def one(sym):
        bars = _yahoo(sym.replace(".", "-"), "5m", start, now + timedelta(days=1)) or []
        bars = [b for b in bars if "09:30" <= b["t"][11:16] < "16:00" and b["t"][:10] < now.strftime("%Y-%m-%d")]
        if not bars:
            return sym, [], 0
        dates = sorted({b["t"][:10] for b in bars})
        walls = flowdata.stored_walls(sym)
        miss = [d for d in dates if not (walls.get(d) or {}).get("callWall")]
        for d in miss:                                 # walls of the 5-minute window, fetched once and kept
            if time.time() - t0 > budget_s * 0.6:
                break
            try:
                wallwatch._walls_and_mp(key, sym, d, False)
            except Exception as e:
                errors.append(f"{sym} {d}: {str(e)[:60]}")
        if miss:
            walls = flowdata.stored_walls(sym)
        daily = _yahoo(sym.replace(".", "-"), "1d", start - timedelta(days=60), now + timedelta(days=1)) or []
        datr = wallwatch.daily_atr([b for b in daily if b["t"][:10] < now.strftime("%Y-%m-%d")])
        return sym, simulate(bars, walls, datr, P, events, sym), len([d for d in dates if (walls.get(d) or {}).get("callWall")])

    trades, used, days_n = [], 0, 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for f in [pool.submit(one, s) for s in syms]:
            try:
                sym, tr, nd = f.result(timeout=max(5, budget_s - (time.time() - t0)))
                trades += tr
                used += 1 if nd else 0
                days_n += nd
            except Exception as e:
                errors.append(str(e)[:100])
    all_d = sorted({t["at"][:10] for t in trades})
    mid = all_d[len(all_d) // 2] if all_d else now.strftime("%Y-%m-%d")
    S = lambda rows: _stats(rows, mid)
    groups = []
    for title, f_ in (("Side", lambda t: "long" if t["side"] == "long" else "short"),
                      ("Approach", lambda t: "fast → overshoot + reclaim" if t["kind"] == "reclaim" else "slow → rejection at the level"),
                      ("Level", lambda t: t["levelName"]),
                      ("Entry time", lambda t: "09:30–10:00" if t["at"][11:16] < "10:00" else "10:00–10:30" if t["at"][11:16] < "10:30" else "10:30+"),
                      ("Reward : risk to the next level", lambda t: "1.5–2.5" if t["rr"] < 2.5 else "2.5–4" if t["rr"] < 4 else "4+")):
        vals = {}
        for t in trades:
            vals.setdefault(f_(t), []).append(t)
        for k in sorted(vals):
            groups.append({"group": title, "label": k, **S(vals[k])})
    by_sym = {}
    for t in trades:
        by_sym.setdefault(t["sym"], []).append(t)
    out = {"PK": pk, "SK": SK, "running": None, "at": iso(now_ny()), "params": P, "secs": round(time.time() - t0),
           "tickers": used, "days": days_n, "period": {"from": start.strftime("%Y-%m-%d"), "mid": mid, "to": now.strftime("%Y-%m-%d")},
           "all": S(trades), "scale": _stats(trades, mid, "Rscale"), "groups": groups,
           "exits": [{"label": "Target = next level", **S(trades)}, {"label": "Half at 1R, rest to the next level (stop to breakeven)", **_stats(trades, mid, "Rscale")}],
           "bySymbol": sorted([{"sym": s_, **S(v)} for s_, v in by_sym.items()], key=lambda x: -(x.get("totR") or 0))[:60],
           "recent": sorted(trades, key=lambda t: t["at"])[-40:], "errors": errors[:5]}
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


def result(sub):
    import backtest
    r = backtest._unstick(db.get(db.upk(sub), SK) or {})
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}
