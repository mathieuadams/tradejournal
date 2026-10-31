"""Trend Volume Profile & Rolling POC [BigBeluga] as a strategy, on NQ / ES candles.

The indicator (ported from the Pine v6 source):
  basis = MA(close, maLen) (SMA / EMA / WMA / HMA / RMA / VWMA); ATR = RMA of the true range (atrLen);
  upper / lower band = basis +/- atrMult x ATR.
  Bull trend: the close crosses above the upper band. Bear trend: a close under the lower band while bullish
  (as in the source, a bear trend only follows a bull one).
  Rolling POC: the volume profile of every candle since the trend started (bins of binMult x ATR, a candle's volume
  spread over the bins it covers): the price of the fullest bin.
Entries (`entry`):
  flip  - in the new trend's direction at the next candle's open after a trend change;
  poc   - during a trend (at least `minBars` candles old): a candle trades through the rolling POC and closes back
          on the trend's side: enter at the next open.
Exits: the next trend change (at the next open); the stop (`stop` band: the opposite band at the signal candle,
none: no stop, R measured with 1 ATR); intraday: flat at 15:55 when `flatEod`. One position at a time."""
import datetime as dt
import math
import random

DEFAULT = {"tickers": ["NQ"], "tf": "15", "maType": "SMA", "maLen": 25, "atrLen": 50, "atrMult": 1.0, "binMult": 0.2,
           "entry": "flip", "side": "both", "stop": "band", "minBars": 3, "flatEod": True, "days": 250}
MAS = ("SMA", "EMA", "WMA", "HMA", "RMA", "VWMA")
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("hold", "Candles held"), ("age", "Trend age at entry")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    tf = str(p.get("tf"))
    out = {"setup": "tvpoc", "tf": tf if tf in ("5", "15", "60", "1D") else "15", "maType": p.get("maType") if p.get("maType") in MAS else "SMA",
           "entry": "poc" if p.get("entry") == "poc" else "flip", "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both",
           "stop": "none" if p.get("stop") == "none" else "band", "flatEod": bool(p.get("flatEod", True))}
    for k, lo, hi in (("maLen", 2, 400), ("atrLen", 2, 400), ("atrMult", 0.1, 10), ("binMult", 0.01, 5), ("minBars", 0, 200), ("days", 20, 3000)):
        try:
            out[k] = max(lo, min(hi, float(p.get(k))))
        except (TypeError, ValueError):
            out[k] = DEFAULT[k]
    for k in ("maLen", "atrLen", "minBars", "days"):
        out[k] = int(out[k])
    tk = p.get("tickers") or DEFAULT["tickers"]
    tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
    out["tickers"] = [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:2] or ["NQ"]
    return out


def describe(p):
    tf = "daily" if p["tf"] == "1D" else f"{p['tf']}-minute"
    how = "at the trend change" if p["entry"] == "flip" else f"on a retest of the rolling POC (trend {p['minBars']}+ candles old)"
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])} {tf} Trend Volume Profile ({p['maType']} {p['maLen']}, ATR {p['atrLen']} × {p['atrMult']:g}): enter {how}, "
            f"{side}, out at the next trend change" + (", stop at the opposite band" if p["stop"] == "band" else "") +
            (", flat at 15:55" if p["tf"] != "1D" and p["flatEod"] else ""))


def tickers(sub, p):
    return list(p["tickers"])


# ---------------------------------------------------------------- moving averages (Pine semantics)

def _sma(x, n):
    out, s = [], 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def _ema(x, n, alpha=None):
    a = alpha or 2 / (n + 1)
    out, e = [], None
    for i, v in enumerate(x):
        if e is None:
            if i == n - 1:
                e = sum(x[:n]) / n                     # seeded with the SMA, as Pine does
            out.append(e)
            continue
        e = a * v + (1 - a) * e
        out.append(e)
    return out


def _rma(x, n):
    return _ema(x, n, 1 / n)


def _wma(x, n):
    w = n * (n + 1) / 2
    return [sum(x[i - k] * (n - k) for k in range(n)) / w if i >= n - 1 and all(x[i - k] is not None for k in range(n)) else None for i in range(len(x))]


def _hma(x, n):
    h, s = _wma(x, max(1, n // 2)), _wma(x, n)
    d = [2 * a - b if a is not None and b is not None else None for a, b in zip(h, s)]
    k = max(1, int(round(math.sqrt(n))))
    idx = next((i for i, v in enumerate(d) if v is not None), len(d))
    tail = _wma(d[idx:], k)
    return [None] * idx + tail


def _vwma(c, v, n):
    num, den = _sma([a * b for a, b in zip(c, v)], n), _sma(v, n)
    return [a / b if a is not None and b else None for a, b in zip(num, den)]


def indicator(C, p):
    """Per candle: basis, upper, lower, ATR, active direction, trend start index, rolling POC (or None).
    C: [(t, o, h, l, c, v)]."""
    n = len(C)
    cl, vo = [c[4] for c in C], [c[5] for c in C]
    m = p["maType"]
    basis = {"SMA": lambda: _sma(cl, p["maLen"]), "EMA": lambda: _ema(cl, p["maLen"]), "WMA": lambda: _wma(cl, p["maLen"]),
             "HMA": lambda: _hma(cl, p["maLen"]), "RMA": lambda: _rma(cl, p["maLen"]), "VWMA": lambda: _vwma(cl, vo, p["maLen"])}[m]()
    tr = [C[0][2] - C[0][3]] + [max(C[i][2] - C[i][3], abs(C[i][2] - C[i - 1][4]), abs(C[i][3] - C[i - 1][4])) for i in range(1, n)]
    atr = _rma(tr, p["atrLen"])
    up = [b + a * p["atrMult"] if b is not None and a is not None else None for b, a in zip(basis, atr)]
    lo = [b - a * p["atrMult"] if b is not None and a is not None else None for b, a in zip(basis, atr)]
    pend, cnt, act, start = 0, 0, 0, 0
    dirs, starts, flips, poc = [], [], [], []
    for i in range(n):
        prev_pend = pend
        if up[i] is not None and i and up[i - 1] is not None and cl[i] > up[i] and cl[i - 1] <= up[i - 1]:
            pend = 1
        elif lo[i] is not None and cl[i] < lo[i] and pend > 0:
            pend = -1
        cnt = 1 if pend != prev_pend else cnt + 1
        flip = False
        if act == 0 and pend != 0:
            act, start, flip = pend, i - cnt + 1, True
        elif cnt >= 1 and pend != act:
            act, start, flip = pend, i - cnt + 1, True
        dirs.append(act)
        starts.append(start)
        flips.append(flip)
        poc.append(_poc(C, start, i, atr[i], p["binMult"]) if act != 0 and atr[i] and p.get("_poc", True) else None)
    return {"basis": basis, "up": up, "lo": lo, "atr": atr, "dir": dirs, "start": starts, "flip": flips, "poc": poc}


def _poc(C, s, i, atr, binMult):
    s = max(0, min(s, i), i - 400)                     # very long trends: the last 400 candles
    rl = min(C[k][3] for k in range(s, i + 1))
    rh = max(C[k][2] for k in range(s, i + 1))
    span = max(rh - rl, atr * 0.1)
    bh = max(atr * binMult, 0.0001)
    nb = min(max(int(round(span / bh)), 2), 100)
    bh = span / nb
    vol = [0.0] * nb
    for k in range(s, i + 1):
        _, _, h, l, _, v = C[k]
        r = h - l
        if r <= 0:
            vol[min(max(int((l - rl) // bh), 0), nb - 1)] += v
            continue
        b0, b1 = max(int((l - rl) // bh), 0), min(int((h - rl) // bh), nb - 1)
        for b in range(b0, b1 + 1):
            lo_e = rl + b * bh
            ov = min(h, lo_e + bh) - max(l, lo_e)
            if ov > 0:
                vol[b] += v * ov / r
    j = max(range(nb), key=lambda b: (vol[b], -b))
    return rl + (j + 0.5) * bh


# ---------------------------------------------------------------- data and trades

def candles(sym, p, start=None):
    """[(t, o, h, l, c, v)] regular hours (daily: the regular-hours day)."""
    import futdata
    if p["tf"] == "1D":
        D = futdata.daily(sym)
        if start:
            D = [d for d in D if d[0] >= (dt.date.fromisoformat(start) - dt.timedelta(days=400)).isoformat()]
        else:
            D = D[-p["days"]:]
        return [(d[0] + "T09:30", d[1], d[2], d[3], d[4], d[5]) for d in D]
    by = futdata.sessions(sym, None if start else p["days"], rth=True,
                          start=(dt.date.fromisoformat(start) - dt.timedelta(days=40)).isoformat() if start else None)
    return [tuple(b) for d in sorted(by) for b in futdata.agg(by[d], int(p["tf"]))]


def run(C, p):
    if len(C) < max(p["maLen"], p["atrLen"]) + 5:
        return []
    ind = indicator(C, {**p, "_poc": p["entry"] == "poc"})
    intraday = p["tf"] != "1D"
    allow = lambda d: p["side"] == "both" or (p["side"] == "long" and d > 0) or (p["side"] == "short" and d < 0)
    out, pos, n = [], None, len(C)
    for i in range(1, n):
        t, o, h, l, c, v = C[i]
        j = i - 1                                      # the signal candle (closed)
        if pos:
            d, x, why = pos["d"], None, None
            if pos["stop"] is not None and ((d > 0 and l <= pos["stop"]) or (d < 0 and h >= pos["stop"])):
                x, why = (min(o, pos["stop"]) if d > 0 else max(o, pos["stop"])), "stop"
            elif ind["flip"][j] and ind["dir"][j] != d:
                x, why = o, "trend change"
            elif intraday and p["flatEod"] and (t[11:16] >= "15:55" or (i + 1 < n and C[i + 1][0][:10] != t[:10])):
                x, why = c, "close"
            if x is not None:
                out.append({**pos, "x": x, "out": t, "why": why, "hold": i - pos["k"]})
                pos = None
        if pos is None and i < n - 1 and not (intraday and t[11:16] >= "15:30"):
            d = ind["dir"][j]
            sig = False
            if p["entry"] == "flip":
                sig = ind["flip"][j] and d != 0
            elif d != 0 and ind["poc"][j] is not None and j - ind["start"][j] >= p["minBars"]:
                pc, (_, _, hj, lj, cj, _) = ind["poc"][j], C[j]
                sig = (d > 0 and lj <= pc < cj) or (d < 0 and hj >= pc > cj)
            if sig and allow(d):
                a = ind["atr"][j]
                band = ind["lo"][j] if d > 0 else ind["up"][j]
                stop = band if p["stop"] == "band" and band is not None and (o - band) * d > 0 else None
                pos = {"d": d, "e": o, "in": t, "k": i, "stop": stop, "stopDist": abs(o - stop) if stop is not None else a,
                       "age": j - ind["start"][j], "poc": ind["poc"][j]}
    if pos:
        out.append({**pos, "x": C[-1][4], "out": C[-1][0], "why": "open", "hold": n - 1 - pos["k"]})
    res = []
    for x in out:
        d = x.pop("d")
        res.append({**x, "side": "long" if d > 0 else "short", "pct": d * (x["x"] / x["e"] - 1) * 100,
                    "r": d * (x["x"] - x["e"]) / x["stopDist"] if x["stopDist"] else None})
    return res


def baseline(C, tr, rnd, n=3):
    out, h, d = [], max(1, tr["hold"]), 1 if tr["side"] == "long" else -1
    for _ in range(n):
        if len(C) <= h + 2:
            break
        k = rnd.randrange(1, len(C) - h - 1)
        out.append(d * (C[k + h][1] / C[k][1] - 1) * 100)
    return out


def all_trades(sub, p, cells=None, prog=None):
    trades, base = [], []
    rnd = random.Random(23)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: Trend Volume Profile on {'daily' if p['tf'] == '1D' else p['tf'] + '-minute'} candles")
        C = candles(sym, p, p.get("_start"))
        for x in run(C, p):
            d = x["in"][:10]
            x.update(s=sym, d=d, cell=(cells or {}).get(d))
            trades.append(x)
            base.append((d, x["side"], baseline(C, x, rnd)))
    return trades, base


def decorate(x, p):
    import range50
    x["tod"] = range50.tod(x["in"]) if p["tf"] != "1D" else "daily"
    x["wd"] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.date.fromisoformat(x["d"]).weekday()]
    h = x.get("hold") or 0
    x["hold"] = "1-2" if h <= 2 else "3-8" if h <= 8 else "9-25" if h <= 25 else "26+"
    a = x.get("age") or 0
    x["age"] = "new (0-2)" if a <= 2 else "3-10" if a <= 10 else "11-30" if a <= 30 else "30+"


def overlay(sub, p, sym, start, end):
    """The bands, the basis and the rolling POC, for the chart."""
    C = candles(sym, p, start)
    if len(C) < 10:
        return None
    ind = indicator(C, {**p, "_poc": True})
    keep = [i for i, c in enumerate(C) if start <= c[0][:10] <= end]
    pts = lambda arr: [[C[i][0], round(arr[i], 2)] for i in keep if arr[i] is not None]
    return {"lines": [{"name": "Upper band", "color": "rgba(255,88,88,.7)", "pts": pts(ind["up"])},
                      {"name": "Lower band", "color": "rgba(0,242,254,.7)", "pts": pts(ind["lo"])},
                      {"name": "Basis", "color": "rgba(160,170,185,.5)", "pts": pts(ind["basis"])},
                      {"name": "Rolling POC", "color": "#FFD700", "pts": pts(ind["poc"])}], "ranges": []}
