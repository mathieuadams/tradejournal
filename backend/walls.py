"""Walls and Peak Zone from price history (the density-graph method, phase 1: realized moves, no options data).

For a day, over the `lookback` sessions before it (optionally the same weekday only): each session's move from its
reference (the 09:30 open, or the previous 16:00 close) to its regular-hours high and low, in %. Optionally
volatility-adjusted: each move divided by that day's 14-day average range (%), times today's.
  Peak (the middle lane, where price usually ranges)  = the `peak` percentile (median by default) of the moves up / down
  Inner wall / True wall / Overflow wall            = the `inner` / `true` / `over` percentiles
Each level comes with how many of the sessions went beyond it, and the density map: where price spent its time
(each 5-minute close, by time of day and % from the reference) plus the cone of the running high / low."""
import bisect
import datetime as dt

import futdata

LEVELS = (("peak", "Peak"), ("inner", "Inner wall"), ("true", "True wall"), ("over", "Overflow wall"))
DEFAULT = {"lookback": 250, "ref": "open", "weekday": False, "match": "none", "scale": "none", "peak": 50.0, "inner": 75.0, "true": 90.0, "over": 97.5}


def params(q):
    p = dict(DEFAULT)
    try:
        p["lookback"] = max(20, min(2000, int(q.get("lookback") or 250)))
        for k in ("peak", "inner", "true", "over"):
            if q.get(k) not in (None, ""):
                p[k] = max(1.0, min(99.9, float(q[k])))
    except ValueError:
        pass
    p["ref"] = q.get("ref") if q.get("ref") in ("open", "close") else "open"
    p["weekday"] = str(q.get("weekday")).lower() in ("1", "true", "yes")
    p["match"] = q.get("match") if q.get("match") in ("none", "weekday", "hl") else ("weekday" if p["weekday"] else "none")
    p["weekday"] = p["match"] == "weekday"
    p["scale"] = q.get("scale") if q.get("scale") in ("none", "vol") else "none"
    return p


def pct(xs, q):
    """The q-th percentile (0-100) of the sorted list xs, interpolated."""
    if not xs:
        return None
    k = (len(xs) - 1) * q / 100
    i = int(k)
    return xs[i] if i + 1 >= len(xs) else xs[i] + (xs[i + 1] - xs[i]) * (k - i)


def _sample(D, i, p, day=None):
    """The sessions before D[i] used for it, with their % moves up / down from the reference (and their ATR %)."""
    rows = []
    trs = []
    for j in range(len(D)):
        pc = D[j - 1][4] if j else D[j][1]
        trs.append(max(D[j][2], pc) - min(D[j][3], pc))
    atrp = [None] * len(D)
    for j in range(14, len(D)):
        atrp[j] = sum(trs[j - 14:j]) / 14 / D[j - 1][4] * 100       # known before day j
    wd = dt.date.fromisoformat(day or (D[i][0] if i < len(D) else D[-1][0])).weekday()
    hl = lambda j: (D[j][2] > D[j - 1][2], D[j][3] < D[j - 1][3]) if j >= 1 else None
    want = hl(i - 1) if p.get("match") == "hl" and i - 1 >= 1 else None   # yesterday's pattern (higher high? lower low?)
    for j in range(i - 1, 0, -1):
        if len(rows) >= p["lookback"]:
            break
        d = D[j]
        if p["weekday"] and dt.date.fromisoformat(d[0]).weekday() != wd:
            continue
        if want is not None and (j - 1 < 1 or hl(j - 1) != want):
            continue                                    # HL matching: the day before had the same pattern as yesterday
        ref = d[1] if p["ref"] == "open" else D[j - 1][4]
        up, dn = (d[2] - ref) / ref * 100, (ref - d[3]) / ref * 100
        if p["scale"] == "vol":
            if not atrp[j]:
                continue
            up, dn = up / atrp[j], dn / atrp[j]
        rows.append((d[0], up, dn, ref, (d[4] - ref) / ref * 100, d[2] > D[j - 1][2], d[3] < D[j - 1][3]))
    cur = atrp[i] if i < len(D) else (sum(trs[-14:]) / 14 / D[-1][4] * 100 if len(D) >= 14 else None)
    return rows, cur


def levels(sym, day, q=None):
    """{"ref", "refKind", "levels": [{k, label, side, pct, price, beyond, of}], "stats", "sample": n} for the day."""
    p = params(q or {})
    D = futdata.daily(sym)
    days = [x[0] for x in D]
    i = bisect.bisect_left(days, day)
    if i < 15:
        return None
    rows, cur = _sample(D, i, p, day)
    if len(rows) < 20:
        return None
    today = D[i] if i < len(D) and D[i][0] == day else None
    if not today:                                      # the session in progress: its regular hours so far, if they've started
        live = futdata.rth(sym, None, 1, start=day, end=day).get(day) or []
        if live:
            today = [day, live[0][1], max(b[2] for b in live), min(b[3] for b in live), live[-1][4], 0]
    if p["ref"] == "open" and today:
        ref, kind = today[1], "today's 09:30 open"
    else:
        ref, kind = D[i - 1][4], ("yesterday's 16:00 close" if p["ref"] == "close" else "yesterday's close (today's open isn't in yet)")
    mult = cur if p["scale"] == "vol" else 1.0
    ups, dns = sorted(r[1] for r in rows), sorted(r[2] for r in rows)
    out = []
    for k, lab in LEVELS:
        for side, xs in (("up", ups), ("down", dns)):
            v = pct(xs, p[k]) * mult
            price = ref * (1 + v / 100) if side == "up" else ref * (1 - v / 100)
            beyond = sum(1 for x in xs if x * mult > v)
            out.append({"k": k, "label": lab, "side": side, "pct": round(v, 3), "price": round(price, 2), "beyond": beyond, "of": len(xs)})
    L = {(x["k"], x["side"]): x["pct"] / mult for x in out}
    n = len(rows)
    st = {
        "medianTagged": round(sum(1 for r in rows if r[1] >= L[("peak", "up")] or r[2] >= L[("peak", "down")]) / n * 100),
        "bothPeaks": round(sum(1 for r in rows if r[1] >= L[("peak", "up")] and r[2] >= L[("peak", "down")]) / n * 100),
        "bothWalls": round(sum(1 for r in rows if r[1] >= L[("true", "up")] and r[2] >= L[("true", "down")]) / n * 100),
        "anyWall": round(sum(1 for r in rows if r[1] >= L[("true", "up")] or r[2] >= L[("true", "down")]) / n * 100),
        "overflow": round(sum(1 for r in rows if r[1] > L[("over", "up")] or r[2] > L[("over", "down")]) / n * 100),
    }
    mean = lambda xs: sum(xs) / len(xs)
    fan = {}
    for side, xs in (("up", ups), ("down", dns)):
        sg = 1 if side == "up" else -1
        pts = [("Max", xs[-1]), ("MaxAvg (wall)", pct(xs, 75)), ("Avg", mean(xs)), ("Median", pct(xs, 50)), ("MinAvg", pct(xs, 25))]
        fan[side] = [{"label": lab, "pct": round(v * mult, 3), "price": round(ref * (1 + sg * v * mult / 100), 2), "odds": round(sum(1 for x in xs if x >= v) / n * 100, 1)} for lab, v in pts]
    top = max(ups[-1], dns[-1]) * mult
    grid = [top * k / 40 for k in range(41)]
    odds = {"pct": [round(g, 3) for g in grid], "up": [round(sum(1 for x in ups if x * mult >= g) / n * 100, 1) for g in grid],
            "down": [round(sum(1 for x in dns if x * mult >= g) / n * 100, 1) for g in grid]}
    wl, ws = L[("inner", "up")], L[("inner", "down")]
    cnt = lambda f: [sum(1 for r in rows if f(r)), n]
    st["lines"] = [
        ["Highs below the long wall (inner)", *cnt(lambda r: r[1] < wl)], ["Highs at or above the long median (Peak)", *cnt(lambda r: r[1] >= L[("peak", "up")])],
        ["Lows above the short wall (inner)", *cnt(lambda r: r[2] < ws)], ["Lows at or below the short median (Peak)", *cnt(lambda r: r[2] >= L[("peak", "down")])],
        ["Closed above the reference", *cnt(lambda r: r[4] > 0)], ["Made a higher high than the day before", *cnt(lambda r: r[5])],
        ["Made a lower low than the day before", *cnt(lambda r: r[6])], ["Both (an outside day)", *cnt(lambda r: r[5] and r[6])],
    ]
    return {"day": day, "ref": round(ref, 2), "refKind": kind, "levels": out, "stats": st, "sample": n, "fan": fan, "odds": odds, "from": rows[-1][0], "to": rows[0][0],
            "params": p, "atrPct": round(cur, 3) if cur else None, "actual": {"h": today[2], "l": today[3], "c": today[4], "o": today[1]} if today else None}


def density(sym, day, q=None):
    """levels() + the density map: 5-minute closes by time of day x % from the reference (sessions before the day), the
    cone (percentiles of the running high / low by time of day), and the day's own path when it has candles."""
    p = params(q or {})
    lv = levels(sym, day, q)
    if not lv:
        return None
    D = futdata.daily(sym)
    days = [x[0] for x in D]
    i = bisect.bisect_left(days, day)
    rows, cur = _sample(D, i, p, day)
    keep = {r[0]: r[3] for r in rows}
    start = min(keep) if keep else day
    by = futdata.rth(sym, None, 5, start=start, end=(dt.date.fromisoformat(day)).isoformat())
    times = [f"{h:02d}:{m:02d}" for h in range(9, 16) for m in range(0, 60, 5) if "09:30" <= f"{h:02d}:{m:02d}" < "16:00"]
    ti = {t: k for k, t in enumerate(times)}
    mult = cur if p["scale"] == "vol" else 1.0
    span = max(abs(x["pct"]) for x in lv["levels"]) * 1.25
    nb = 60
    step = 2 * span / nb
    grid = [[0] * nb for _ in times]
    runH = [[] for _ in times]
    runL = [[] for _ in times]
    trs_atr = {}
    if p["scale"] == "vol":
        trs = []
        for j in range(len(D)):
            pc = D[j - 1][4] if j else D[j][1]
            trs.append(max(D[j][2], pc) - min(D[j][3], pc))
            if j >= 14:
                trs_atr[D[j][0]] = sum(trs[j - 14:j]) / 14 / D[j - 1][4] * 100
    for d, bars in by.items():
        if d not in keep or d >= day:
            continue
        ref = keep[d]
        f = (mult / trs_atr[d]) if p["scale"] == "vol" and trs_atr.get(d) else 1.0
        hi = lo = 0.0
        for t, o, h, l, c in bars:
            k = ti.get(t[11:16])
            if k is None:
                continue
            hi, lo = max(hi, (h - ref) / ref * 100 * f), min(lo, (l - ref) / ref * 100 * f)
            x = (c - ref) / ref * 100 * f
            b = int((x + span) / step)
            if 0 <= b < nb:
                grid[k][b] += 1
            runH[k].append(hi)
            runL[k].append(-lo)
    cone = {}
    for name, key in (("peak", "peak"), ("inner", "inner"), ("true", "true"), ("over", "over")):
        cone[name] = {"up": [round(pct(sorted(xs), p[key]), 3) if xs else None for xs in runH],
                      "down": [round(pct(sorted(xs), p[key]), 3) if xs else None for xs in runL]}
    path = []
    if day in by:
        ref = lv["ref"]
        for t, o, h, l, c in by[day]:
            if t[11:16] in ti:
                path.append([ti[t[11:16]], round((c - ref) / ref * 100, 3)])
    return {**lv, "times": times, "span": round(span, 3), "bins": nb, "grid": grid, "cone": cone, "path": path}


def chart_levels(sym, days, q=None):
    """{day: [{k, label, side, price}]} for the chart's sessions."""
    out = {}
    for d in days:
        try:
            lv = levels(sym, d, q)
        except Exception as e:
            print("walls failed", d, e)
            lv = None
        if lv:
            out[d] = [{"k": x["k"], "label": x["label"], "side": x["side"], "price": x["price"]} for x in lv["levels"]]
    return out
