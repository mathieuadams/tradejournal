"""Prior-day high / low sweep and reclaim, on NQ / ES (daily levels, 5-minute entry).

Short (long = the mirror at the low):
  1. The level (`levels`): pd = yesterday's regular-hours high; outer = the higher of yesterday's high and the
     overnight high. With `fresh`, a pd level the overnight already traded through is skipped.
  2-5. As the overnight sweep: in regular hours, before `lastEntry`, price trades above the level and closes back under
     it within `maxAbove` candles (the sweep at least `minDepth` points): sell at that close, stop past the sweep
     (+ `bufTicks`), target (`tgt`) vwap / mid (the middle of yesterday's range) / other (its other side) / r.
  6. One trade per side per session, flat at 15:55."""
import onsweep
import sessionkit as K

DEFAULT = {"tickers": ["NQ"], "tf": 5, "levels": "pd", "fresh": True, "lastEntry": "11:30", "maxAbove": 3, "minDepth": 0.0, "bufTicks": 2,
           "tgt": "vwap", "tgtR": 2.0, "minRR": 1.0, "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("depth", "How far past the level"), ("gap", "Where today opened")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "pdsweep", "tf": 1 if str(p.get("tf")) == "1" else 5, "levels": p.get("levels") if p.get("levels") in ("pd", "outer") else "pd",
           "fresh": bool(p.get("fresh")), "tgt": p.get("tgt") if p.get("tgt") in ("vwap", "mid", "other", "r") else "vwap",
           "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    le = str(p.get("lastEntry") or "11:30")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "09:35" <= le <= "15:30" else "11:30"
    K.num(p, out, (("minDepth", 0, 500, 0.0), ("maxAbove", 1, 30, 3), ("bufTicks", 0, 40, 2), ("tgtR", 0.5, 10, 2.0), ("minRR", 0, 10, 1.0),
                   ("days", 20, 3000, 500)), ("maxAbove", "bufTicks", "days"))
    out["tickers"] = K.tickers_of(p)
    return out


def describe(p):
    lv = "yesterday's high / low" + (" (not already taken overnight)" if p["fresh"] else "") if p["levels"] == "pd" else "the outer of yesterday's and the overnight high / low"
    rg = "yesterday's range" if p["levels"] == "pd" else "that range"
    tg = {"vwap": "the VWAP", "mid": f"the middle of {rg}", "other": f"the other side of {rg}", "r": f"{p['tgtR']:g}R"}[p["tgt"]]
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute: before {p['lastEntry']}, price runs past {lv}"
            + (f" by {p['minDepth']:g}+ pts" if p["minDepth"] else "") + f" and closes back inside (within {p['maxAbove']} candles): fade it at that close "
            f"({side}), stop past the sweep + {p['bufTicks']} tick(s), target {tg}" + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "") + ", flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def engine(day, on, R, F, V, H, p):
    if not H or not R:
        return []
    y = H[-1]
    onh = max((b[2] for b in on), default=None)
    onl = min((b[3] for b in on), default=None)
    hi, lo, sides = y["h"], y["l"], [-1, 1]
    if p["levels"] == "outer" and on:
        hi, lo = max(hi, onh), min(lo, onl)
    elif p["fresh"] and on:
        sides = [s for s in sides if not (s < 0 and onh > y["h"]) and not (s > 0 and onl < y["l"])]
    if R[0][1] > hi or R[0][1] < lo:
        sides = [s for s in sides if not (s < 0 and R[0][1] > hi) and not (s > 0 and R[0][1] < lo)]   # opened past it: nothing to sweep
    out = onsweep.fade(R, V, p, hi, lo, sides)
    gap = 1 if R[0][1] > y["h"] else -1 if R[0][1] < y["l"] else 0
    for x in out:
        x["gap"] = gap
    return out


def all_trades(sub, p, cells=None, prog=None):
    return K.run_daily(sub, p, engine, "prior-day levels", need=3, seed=43)


def decorate(x, p):
    x["tod"] = K.tod(x["in"])
    x["wd"] = K.weekday(x["d"])
    dp = x.get("depth") or 0
    x["depth"] = "under 5 pts" if dp < 5 else "5-15 pts" if dp < 15 else "15+ pts"
    x["gap"] = {1: "above yesterday's high", -1: "below yesterday's low", 0: "inside yesterday's range"}.get(x.get("gap"), "inside yesterday's range")


def overlay(sub, p, sym, start, end):
    return {"lines": K.level_lines(sym, p, start, end, [("h", "Yesterday's high", "#E7C35A"), ("l", "Yesterday's low", "#E7C35A")]), "ranges": []}
