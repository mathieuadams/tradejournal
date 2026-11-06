"""RSI(2) pullback with the trend (after Larry Connors), on hourly candles.

  Hourly candles of the full session (overnight included; they start at :30). Long: price above its `trendLen`-hour EMA and the 2-period
  RSI under `lo`: buy at that hour's close. Out at the first close with the RSI over `hi`, after `maxHold` hours, at the
  stop (`stopMult` x the average hourly range, 0 = none: R then counts 2x that range) or at 15:55 (`flatEod`); short =
  the mirror (RSI over 100 - lo under the EMA)."""
import random

import ifvg
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 60, "trendLen": 200, "lo": 10.0, "hi": 70.0, "maxHold": 10, "stopMult": 0.0, "flatEod": True, "side": "long", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Hour of the entry"), ("wd", "Weekday"), ("rv", "RSI at the entry")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "rsi2", "tf": 60, "side": p.get("side") if p.get("side") in ("long", "short", "both") else "long", "tickers": N.K.tickers_of(p),
           "flatEod": str(p.get("flatEod")).lower() not in ("false", "0", "none", "")}
    N.K.num(p, out, (("trendLen", 20, 400, 200), ("lo", 1, 40, 10.0), ("hi", 50, 99, 70.0), ("maxHold", 1, 100, 10), ("stopMult", 0, 20, 0.0), ("days", 60, 3000, 500)),
          ("trendLen", "maxHold", "days"))
    return out


def describe(p):
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])} hourly (overnight included), {side}: with the {p['trendLen']}-hour EMA, the 2-period RSI under {p['lo']:g} (over "
            f"{100 - p['lo']:g} for shorts): enter at that close; out when the RSI crosses {p['hi']:g} ({100 - p['hi']:g} for shorts), after {p['maxHold']} hours"
            + (f", stop {p['stopMult']:g}× the average hourly range" if p["stopMult"] else ", no stop") + (", flat at 15:55" if p["flatEod"] else ""))


def tickers(sub, p):
    return list(p["tickers"])


def indicators(C, n):
    """RSI(2) and EMA(n) of the closes, average range (14)."""
    rsi, ema, rng = [], [], []
    ag = al = None
    e = None
    m = 2 / (n + 1)
    for i, (t, o, h, l, c) in enumerate(C):
        ch = c - C[i - 1][4] if i else 0
        g, lo_ = max(ch, 0), max(-ch, 0)
        ag = g if ag is None else (ag + g) / 2
        al = lo_ if al is None else (al + lo_) / 2
        rsi.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
        e = c if e is None else m * c + (1 - m) * e
        ema.append(e)
        w = C[max(0, i - 13):i + 1]
        rng.append(sum(b[2] - b[3] for b in w) / len(w))
    return rsi, ema, rng


def all_trades(sub, p, cells=None, prog=None):
    trades, base = [], []
    rnd = random.Random(131)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: hourly candles")
        q = {**p, "days": p["days"] + 40} if not p.get("_start") else p
        rows, Fall, fi, E = ifvg._series(sym, q)
        keep = set(d for d, *_ in rows[-p["days"]:]) if not p.get("_start") else set(d for d, *_ in rows if d >= p["_start"])
        C, day = [], []
        for d, on, R, F, V in rows:
            C += F
            day += [d] * len(F)
        rsi, ema, rng = indicators(C, p["trendLen"])
        i = max(p["trendLen"] // 4, 3)
        while i < len(C) - 1:
            t, o, h, l, c = C[i]
            d = day[i]
            if d not in keep or (p["flatEod"] and "14:30" <= t[11:16] < "18:00" and t[:10] == d) or "16:00" <= t[11:16] < "17:30":
                i += 1
                continue
            dd = 1 if c > ema[i] and rsi[i] < p["lo"] else -1 if c < ema[i] and rsi[i] > 100 - p["lo"] else 0
            if not dd or (p["side"] == "long" and dd < 0) or (p["side"] == "short" and dd > 0):
                i += 1
                continue
            risk = (p["stopMult"] or 2) * rng[i]
            stop = c - dd * risk if p["stopMult"] else None
            xp, xi, why = None, None, None
            for j in range(i + 1, len(C)):
                tj, oj, hj, lj, cj = C[j]
                if stop is not None and ((dd > 0 and lj <= stop) or (dd < 0 and hj >= stop)):
                    xp, xi, why = (min(oj, stop) if dd > 0 else max(oj, stop)), j, "stop"
                    break
                if (dd > 0 and rsi[j] > p["hi"]) or (dd < 0 and rsi[j] < 100 - p["hi"]):
                    xp, xi, why = cj, j, "target"
                    break
                if j - i >= p["maxHold"]:
                    xp, xi, why = cj, j, "time"
                    break
                if p["flatEod"] and (day[j] != d or (tj[:10] == d and tj[11:16] >= "15:30")):
                    xp, xi, why = C[j - 1][4], j - 1, "time"      # the hour ending 15:29 (hourly candles start at :30)
                    break
            if xp is None:
                xp, xi, why = C[-1][4], len(C) - 1, "open"
            x = {"in": N.K.end_of(t, 60), "out": C[xi][0], "e": c, "x": xp, "why": why, "side": "long" if dd > 0 else "short", "stop": stop, "tgt": None,
                 "stopDist": risk, "tgtDist": None, "cf": t, "rv": rsi[i], "pct": dd * (xp / c - 1) * 100, "r": dd * (xp - c) / risk, "s": sym, "d": d, "cell": None}
            trades.append(x)
            h_, b = max(1, xi - i), []
            for _ in range(3):
                if len(C) > h_ + 2:
                    k = rnd.randrange(1, len(C) - h_ - 1)
                    b.append(dd * (C[k + h_][4] / C[k][4] - 1) * 100)
            base.append((d, x["side"], b))
            i = xi + 1
    return trades, base


def decorate(x, p):
    N.decorate_common(x)
    v = x.get("rv") or 0
    x["rv"] = "under 5 / over 95" if v < 5 or v > 95 else "5-10 / 90-95" if v < 10 or v > 90 else "10+ / under 90"


def overlay(sub, p, sym, start, end):
    return {"lines": [], "ranges": []}
