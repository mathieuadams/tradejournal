"""Overnight high / low sweep and reclaim, on NQ / ES.

Short (long = the mirror at the overnight low):
  1. In regular hours, before `lastEntry`, a candle trades above the overnight high (ONH: 18:00-09:30).
  2. The reclaim: a candle closes back under the ONH (the same candle or a later one), before `maxAbove` candles
     have closed above it (acceptance above = no trade): sell at that close. Only once the sweep went at least
     `minDepth` points past the ONH; no trade when the overnight range is under `minOnPct`% of the price.
  3. Stop: above the highest high since the sweep began (+ `bufTicks` ticks).
  4. Target (`tgt`): vwap (the session VWAP at the entry), mid (the middle of the overnight range), other (the other
     side of the overnight range) or r (`tgtR` x the risk). Skipped when the target isn't at least `minRR` x the risk.
  5. One trade per side per session, flat at 15:55."""
import sessionkit as K

DEFAULT = {"tickers": ["NQ"], "tf": 5, "lastEntry": "10:30", "maxAbove": 3, "minDepth": 0.0, "minOnPct": 0.0, "bufTicks": 2, "tgt": "vwap", "tgtR": 2.0, "minRR": 1.0,
           "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("depth", "How far past the overnight level"), ("onr", "Overnight range")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "onsweep", "tf": 1 if str(p.get("tf")) == "1" else 5, "tgt": p.get("tgt") if p.get("tgt") in ("vwap", "mid", "other", "r") else "vwap",
           "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    le = str(p.get("lastEntry") or "10:30")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "09:35" <= le <= "15:30" else "10:30"
    K.num(p, out, (("minDepth", 0, 500, 0.0), ("minOnPct", 0, 5, 0.0), ("maxAbove", 1, 30, 3), ("bufTicks", 0, 40, 2), ("tgtR", 0.5, 10, 2.0), ("minRR", 0, 10, 1.0), ("days", 20, 3000, 500)),
          ("maxAbove", "bufTicks", "days"))
    out["tickers"] = K.tickers_of(p)
    return out


def describe(p):
    tg = {"vwap": "the VWAP", "mid": "the middle of the overnight range", "other": "the other side of the overnight range", "r": f"{p['tgtR']:g}R"}[p["tgt"]]
    side = {"long": "long only (ONL sweeps)", "short": "short only (ONH sweeps)", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute: before {p['lastEntry']}, price runs past the overnight high / low and closes back inside "
            f"(within {p['maxAbove']} candles): fade it at that close ({side}), stop past the sweep + {p['bufTicks']} tick(s), target {tg}"
            + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "") + ", flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def engine(day, on, R, F, V, p):
    if not on:
        return []
    onh, onl = max(b[2] for b in on), min(b[3] for b in on)
    if p.get("minOnPct") and onh - onl < onl * p["minOnPct"] / 100:
        return []                                      # the overnight range is too tight
    out = []
    for d in ((-1,) if p["side"] == "short" else (1,) if p["side"] == "long" else (-1, 1)):
        lvl = onh if d < 0 else onl
        swept, ext, above = None, None, 0
        for k, (t, o, h, l, c) in enumerate(R):
            if K.end_of(t, p["tf"])[11:16] > p["lastEntry"]:
                break
            poke = h > lvl if d < 0 else l < lvl
            if swept is None and poke:
                swept, ext = k, (h if d < 0 else l)
            if swept is None:
                continue
            ext = max(ext, h) if d < 0 else min(ext, l)
            back = c < lvl if d < 0 else c > lvl
            if back and p.get("minDepth") and abs(ext - lvl) < p["minDepth"]:
                continue                               # back inside, but the sweep wasn't deep enough: wait for a deeper one
            if not back:
                above += 1
                if above >= p["maxAbove"]:
                    break                              # accepted beyond the level: no fade
                continue
            e = c
            stop = ext + p["bufTicks"] * K.TICK if d < 0 else ext - p["bufTicks"] * K.TICK
            risk = abs(stop - e)
            tgt = {"vwap": V[k], "mid": (onh + onl) / 2, "other": onl if d < 0 else onh, "r": e + d * p["tgtR"] * risk}[p["tgt"]]
            if risk <= 0 or (tgt - e) * d <= 0 or (p["minRR"] and abs(tgt - e) < p["minRR"] * risk):
                break                                  # the target is behind or too close: no trade this side
            x, ot, why, _ = K.walk(R, k + 1, d, e, stop, tgt)
            out.append({"in": K.end_of(t, p["tf"]), "out": ot, "e": e, "x": x, "why": why, "side": "long" if d > 0 else "short",
                        "stop": stop, "tgt": tgt, "stopDist": risk, "tgtDist": abs(tgt - e), "rt": R[swept][0], "cf": t,
                        "depth": abs(ext - lvl), "onr": onh - onl, "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk})
            break
    return out


def all_trades(sub, p, cells=None, prog=None):
    return K.run_all(sub, p, engine, f"{p['tf']}-minute candles + the overnight range")


def decorate(x, p):
    x["tod"] = K.tod(x["in"])
    x["wd"] = K.weekday(x["d"])
    dp = x.get("depth") or 0
    x["depth"] = "under 5 pts" if dp < 5 else "5-15 pts" if dp < 15 else "15+ pts"
    r = x.get("onr") or 0
    x["onr"] = "tight (under 0.5%)" if r < x["e"] * 0.005 else "normal (0.5-1%)" if r < x["e"] * 0.01 else "wide (1%+)"
