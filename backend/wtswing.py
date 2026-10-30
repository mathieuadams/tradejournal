"""WaveTrend [Krypt] dots as entry and exit, on NQ / ES daily or intraday (regular hours) candles.

Long: buy the open of the candle after a blue (buy) dot; sell the open of the candle after the next red (sell) dot.
Short (side short / both): the mirror. Stop (for the R and as a hard exit): `stopAtr` x the 14-candle average range
under / over the entry (0 = no stop, R measured with 1 average range). Intraday: flat at 15:55 when `flatEod`."""
import datetime as dt
import random

import wavetrend

DEFAULT = {"tickers": ["NQ"], "tf": "1D", "side": "long", "stopAtr": 1.5, "flatEod": True, "days": 500}
FACTORS = [("side", "Side"), ("s", "Ticker"), ("wd", "Weekday"), ("hold", "Candles held")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    tf = str(p.get("tf"))
    out = {"setup": "wt", "tf": tf if tf in ("1D", "60", "15", "5") else "1D",
           "side": p.get("side") if p.get("side") in ("long", "short", "both") else "long", "flatEod": bool(p.get("flatEod", True))}
    for k, lo, hi in (("stopAtr", 0, 10), ("days", 20, 3000)):
        try:
            out[k] = max(lo, min(hi, float(p.get(k))))
        except (TypeError, ValueError):
            out[k] = DEFAULT[k]
    out["days"] = int(out["days"])
    tk = p.get("tickers") or DEFAULT["tickers"]
    tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
    out["tickers"] = [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:2] or ["NQ"]
    return out


def describe(p):
    tf = "daily" if p["tf"] == "1D" else f"{p['tf']}-minute"
    side = {"long": "long only", "short": "short only", "both": "long and short"}[p["side"]]
    stop = f"stop {p['stopAtr']:g}× the average range" if p["stopAtr"] else "no stop"
    return (f"{', '.join(p['tickers'])} {tf} WaveTrend [Krypt]: in at the open after a buy dot, out at the open after a sell dot "
            f"({side}), {stop}" + (", flat at 15:55" if p["tf"] != "1D" and p["flatEod"] else ""))


def tickers(sub, p):
    return list(p["tickers"])


def candles(sym, p):
    """[(t, o, h, l, c)] in time order."""
    import futdata
    if p["tf"] == "1D":
        D = futdata.daily(sym)
        if p.get("_start"):
            D = [d for d in D if d[0] >= (dt.date.fromisoformat(p["_start"]) - dt.timedelta(days=200)).isoformat()]
        else:
            D = D[-p["days"]:]
        return [(d[0] + "T09:30", d[1], d[2], d[3], d[4]) for d in D]
    start = (dt.date.fromisoformat(p["_start"]) - dt.timedelta(days=30)).isoformat() if p.get("_start") else None
    by = futdata.rth(sym, p["days"], int(p["tf"]), start=start)
    return [c for d in sorted(by) for c in by[d]]


def _atr(C, n=14):
    out, a = [], None
    for i, (t, o, h, l, c) in enumerate(C):
        pc = C[i - 1][4] if i else o
        tr = max(h - l, abs(h - pc), abs(l - pc))
        a = tr if a is None else (a * (n - 1) + tr) / n
        out.append(a)
    return out


def run(C, p):
    if len(C) < 40:
        return []
    r = wavetrend.wt([c[4] for c in C])
    buy, sell, atr = r["buy"], r["sell"], _atr(C)
    intraday = p["tf"] != "1D"
    out, pos, n = [], None, len(C)
    for i in range(1, n):
        t, o, h, l, c = C[i]
        if pos:
            d = pos["d"]
            x = why = None
            if pos["stop"] is not None and ((d > 0 and l <= pos["stop"]) or (d < 0 and h >= pos["stop"])):
                x, why = ((min(o, pos["stop"]) if d > 0 else max(o, pos["stop"])), "stop")
            elif (d > 0 and sell[i - 1]) or (d < 0 and buy[i - 1]):
                x, why = o, "dot"
            elif intraday and p["flatEod"] and (t[11:16] >= "15:55" or (i + 1 < n and C[i + 1][0][:10] != t[:10])):
                x, why = c, "close"
            if x is not None:
                out.append({**pos, "x": x, "out": t, "why": why, "hold": i - pos["k"]})
                pos = None
        if pos is None and i < n - 1:
            sig = 1 if buy[i - 1] and p["side"] in ("long", "both") else -1 if sell[i - 1] and p["side"] in ("short", "both") else 0
            if sig and not (intraday and t[11:16] >= "15:30"):
                a = atr[i - 1]
                stop = o - sig * p["stopAtr"] * a if p["stopAtr"] else None
                pos = {"d": sig, "e": o, "in": t, "k": i, "stop": stop, "stopDist": (p["stopAtr"] or 1) * a}
    if pos:                                            # still open at the last candle
        out.append({**pos, "x": C[-1][4], "out": C[-1][0], "why": "open", "hold": n - 1 - pos["k"]})
    res = []
    for x in out:
        d = x.pop("d")
        res.append({**x, "side": "long" if d > 0 else "short", "pct": d * (x["x"] / x["e"] - 1) * 100,
                    "r": d * (x["x"] - x["e"]) / x["stopDist"] if x["stopDist"] else None})
    return res


def baseline(C, tr, rnd, n=3):
    """The same side and holding time from random candles: % gross."""
    out = []
    h = max(1, tr["hold"])
    d = 1 if tr["side"] == "long" else -1
    for _ in range(n):
        if len(C) <= h + 2:
            break
        k = rnd.randrange(1, len(C) - h - 1)
        out.append(d * (C[k + h][1] / C[k][1] - 1) * 100)
    return out


def all_trades(sub, p, cells=None, prog=None):
    trades, base = [], []
    rnd = random.Random(13)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: WaveTrend on {'daily' if p['tf'] == '1D' else p['tf'] + '-minute'} candles")
        C = candles(sym, p)
        for x in run(C, p):
            d = x["in"][:10]
            x.update(s=sym, d=d, cell=(cells or {}).get(d))
            trades.append(x)
            base.append((d, x["side"], baseline(C, x, rnd)))
    return trades, base


def decorate(x, p):
    x["wd"] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.date.fromisoformat(x["d"]).weekday()]
    h = x.get("hold") or 0
    x["hold"] = "1-2" if h <= 2 else "3-5" if h <= 5 else "6-15" if h <= 15 else "16+"
