"""VWAP 2-standard-deviation bands: fade a close outside the band once price closes back inside, target the VWAP.

  Regular hours: the bands = the session VWAP +/- `k` x the standard deviation of the 5-minute closes around it
  (since 09:30, at least 6 candles). The previous candle closed outside the band, this one closes back inside: fade
  at its close; stop past the extreme of the last 3 candles (+ `bufTicks`); target the VWAP (skipped under `minRR`).
  Trend-day filter: no trade once the day's range has reached `trendMult` x the 14-day average daily range (0 = off).
  One position at a time, at most `maxDay` a session, no entry after `lastEntry`, flat at 15:55."""
import math

import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "k": 2.0, "trendMult": 1.0, "lastEntry": "15:00", "maxDay": 3, "minRR": 1.0, "bias": "none", "side": "both",
           "exit": "target", "bufTicks": 2, "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("dr", "Day's range so far (× the daily average)")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "vwapband", "tf": 1 if str(p.get("tf")) == "1" else 15 if str(p.get("tf")) == "15" else 5, "lastEntry": N.hhmm(p.get("lastEntry"), "15:00", "09:45", "15:50")}
    N.K.num(p, out, (("k", 0.5, 5, 2.0), ("trendMult", 0, 5, 1.0), ("maxDay", 1, 20, 3), ("minRR", 0, 10, 1.0)), ("maxDay",))
    N.clean_common(p, out, bias="none")
    return out


def describe(p):
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute, regular hours: a close outside the VWAP ± {p['k']:g} standard deviations, then one back inside: fade it, "
            f"stop past the last 3 candles + {p['bufTicks']} tick(s), target the VWAP" + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "")
            + (f", not once the day's range reaches {p['trendMult']:g}× the daily average" if p["trendMult"] else "") + f", at most {p['maxDay']} a session, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    i0 = N.idx_at(F, f"{d}T09:30")
    if i0 is None:
        return []
    atr = N.atr_map(sym)
    A = atr.get(d, atr.get("next"))
    out, k, buf, flat, last = [], i0 + 7, p["bufTicks"] * N.K.TICK, f"{d}T15:55", f"{d}T{p['lastEntry']}"
    while k < len(F) and k < len(VF) and len(out) < p["maxDay"]:
        t, o, h, l, c = F[k]
        if t[:16] >= last:
            break
        seg = F[i0:k + 1]
        if p["trendMult"] and A and max(b[2] for b in seg) - min(b[3] for b in seg) >= p["trendMult"] * A:
            break                                      # a trend day: no more fades
        dev = [F[i][4] - VF[i] for i in range(i0, k)]
        sd = math.sqrt(sum(x * x for x in dev) / len(dev)) if dev else 0
        pc, pv = F[k - 1][4], VF[k - 1]
        if sd <= 0 or abs(pc - pv) <= p["k"] * sd:
            k += 1
            continue
        dd = 1 if pc < pv else -1
        if abs(c - VF[k]) > p["k"] * sd or (VF[k] - c) * dd <= 0 or not N.ok_side(dd, N.bias(E, t, c, p), p):
            k += 1
            continue
        w = F[k - 2:k + 1]
        stop = (min(b[3] for b in w) - buf) if dd > 0 else (max(b[2] for b in w) + buf)
        risk, tgt = (c - stop) * dd, VF[k]
        if risk <= 0 or (p["minRR"] and abs(tgt - c) < p["minRR"] * risk):
            k += 1
            continue
        x, j = N.trade(F, k, dd, c, stop, tgt if p["exit"] == "target" else None, risk, p, E, flat,
                       dr=(max(b[2] for b in seg) - min(b[3] for b in seg)) / A if A else None)
        out.append((x, (i0 + 7, last, flat)))
        k = j + 1
    return out


def all_trades(sub, p, cells=None, prog=None):
    return N.run(sub, p, engine, 127, prog, "candles + the VWAP bands")


def decorate(x, p):
    N.decorate_common(x)
    v = x.get("dr")
    x["dr"] = "—" if v is None else "under 0.5×" if v < 0.5 else "0.5-0.8×" if v < 0.8 else "0.8×+"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
