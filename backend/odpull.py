"""Opening drive, then the first pullback, on NQ / ES.

Long (short = the mirror):
  1. The drive: from 09:30 to 09:30 + `driveMin`, price rises at least `drivePct`% and closes in the top `strength`
     of that move's range.
  2. The pullback: after the drive, at least `pbMin` candles in a row close lower, and price comes back to the
     `ema`-period EMA (a low at or under it; EMA on the whole session's candles).
  3. The entry: a buy stop one tick over the previous candle's high, from the candle after the pullback; filled
     when price trades there (or at the open if it gaps over), before `lastEntry`.
  4. Stop: under the pullback's low (- `bufTicks` ticks). Target: `tgtR` x the risk. One trade per session,
     flat at 15:55. Same candle through the stop and the target: the stop counts."""
import sessionkit as K

DEFAULT = {"tickers": ["NQ"], "tf": 5, "driveMin": 15, "drivePct": 0.25, "strength": 0.25, "pbMin": 2, "ema": 9, "bufTicks": 2,
           "tgtR": 2.0, "lastEntry": "11:30", "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("drive", "Drive size"), ("pb", "Pullback depth (of the drive)")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "odpull", "tf": 1 if str(p.get("tf")) == "1" else 5, "driveMin": 30 if str(p.get("driveMin")) == "30" else 15,
           "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    le = str(p.get("lastEntry") or "11:30")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "09:50" <= le <= "15:30" else "11:30"
    K.num(p, out, (("drivePct", 0.05, 3, 0.25), ("strength", 0.05, 1, 0.25), ("pbMin", 1, 20, 2), ("ema", 3, 100, 9), ("bufTicks", 0, 40, 2),
                   ("tgtR", 0.5, 10, 2.0), ("days", 20, 3000, 500)), ("pbMin", "ema", "bufTicks", "days"))
    out["tickers"] = K.tickers_of(p)
    return out


def describe(p):
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute: a {p['driveMin']}-minute opening drive of at least {p['drivePct']:g}% closing near its "
            f"extreme, then the first pullback ({p['pbMin']}+ candles) to the {p['ema']} EMA: enter on a break of the previous candle, stop past "
            f"the pullback + {p['bufTicks']} tick(s), target {p['tgtR']:g}R, no entry after {p['lastEntry']}, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def engine(day, on, R, F, V, p):
    d_end = f"{day}T{9 + (30 + p['driveMin']) // 60:02d}:{(30 + p['driveMin']) % 60:02d}"
    dr = [b for b in R if b[0] < d_end]
    if len(dr) < 2 or len(R) <= len(dr) + 2:
        return []
    o0, c0 = dr[0][1], dr[-1][4]
    hi, lo = max(b[2] for b in dr), min(b[3] for b in dr)
    move = c0 - o0
    if abs(move) < o0 * p["drivePct"] / 100 or hi - lo <= 0:
        return []
    d = 1 if move > 0 else -1
    if (d > 0 and (hi - c0) > p["strength"] * (hi - lo)) or (d < 0 and (c0 - lo) > p["strength"] * (hi - lo)):
        return []                                      # closed far from its extreme: not a drive
    if (p["side"] == "long" and d < 0) or (p["side"] == "short" and d > 0):
        return []
    E = K.ema([b[4] for b in F], p["ema"])
    em = {b[0]: v for b, v in zip(F, E)}
    k0 = len(dr)
    run, touched, ext, armed = 0, False, None, False
    for k in range(k0, len(R)):
        t, o, h, l, c = R[k]
        if t[11:16] > p["lastEntry"]:
            return []
        if armed:                                      # a stop order one tick past the previous candle's extreme
            trig = R[k - 1][2] + K.TICK if d > 0 else R[k - 1][3] - K.TICK
            if (d > 0 and h >= trig) or (d < 0 and l <= trig):
                e = max(o, trig) if d > 0 else min(o, trig)
                stop = ext - p["bufTicks"] * K.TICK if d > 0 else ext + p["bufTicks"] * K.TICK
                risk = abs(e - stop)
                if risk <= 0:
                    return []
                tgt = e + d * p["tgtR"] * risk
                x, ot, why, _ = K.walk(R, k, d, e, stop, tgt, stop_ok_first=False)
                return [{"in": t, "out": ot, "e": e, "x": x, "why": why, "side": "long" if d > 0 else "short", "stop": stop, "tgt": tgt,
                         "stopDist": risk, "tgtDist": p["tgtR"] * risk, "bk": dr[0][0], "rt": R[k - 1][0], "cf": t,
                         "drive": abs(move) / o0 * 100, "pb": abs((hi if d > 0 else lo) - ext) / (hi - lo),
                         "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk}]
        ev = em.get(t)
        against = (d > 0 and c < R[k - 1][4]) or (d < 0 and c > R[k - 1][4])
        if against or armed:                           # the pullback goes on (an armed order follows it)
            run += 1 if against else 0
            ext = (l if ext is None else min(ext, l)) if d > 0 else (h if ext is None else max(ext, h))
            if ev is not None and ((d > 0 and l <= ev) or (d < 0 and h >= ev)):
                touched = True
            armed = armed or (run >= p["pbMin"] and touched)
        else:
            run, touched, ext = 0, False, None         # not a pullback yet: start over
    return []


def all_trades(sub, p, cells=None, prog=None):
    return K.run_all(sub, p, engine, f"{p['tf']}-minute candles + the opening drive", seed=41)


def decorate(x, p):
    x["tod"] = K.tod(x["in"])
    x["wd"] = K.weekday(x["d"])
    dv = x.get("drive") or 0
    x["drive"] = "under 0.4%" if dv < 0.4 else "0.4-0.8%" if dv < 0.8 else "0.8%+"
    pb = x.get("pb") or 0
    x["pb"] = "shallow (under 38%)" if pb < 0.382 else "38-62%" if pb < 0.618 else "deep (62%+)"
