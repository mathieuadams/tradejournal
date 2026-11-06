"""Prior-week high / low sweep and reclaim: the weekly version of the overnight sweep.

  The levels: last week's high and low (regular hours, Monday-Friday). In regular hours, before `lastEntry`, price runs
  past one and closes back inside within `maxAbove` candles (the sweep at least `minDepth` points): fade it at that
  close (onsweep's rules). Stop past the sweep (+ `bufTicks`); target (`tgt`) vwap / mid (the middle of last week's range)
  / other (its other side) / r. With `fresh`, a level this week already traded through is skipped. One trade per side
  per session, flat at 15:55."""
import datetime as dt

import futdata
import onsweep
import sessionkit as K

DEFAULT = {"tickers": ["NQ"], "tf": 5, "lastEntry": "15:00", "maxAbove": 3, "minDepth": 0.0, "bufTicks": 2, "tgt": "vwap", "tgtR": 2.0, "minRR": 1.0,
           "fresh": True, "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("depth", "How far past the level")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "pwsweep", "tf": 1 if str(p.get("tf")) == "1" else 5, "tgt": p.get("tgt") if p.get("tgt") in ("vwap", "mid", "other", "r") else "vwap",
           "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both", "fresh": str(p.get("fresh")).lower() not in ("false", "0", "")}
    le = str(p.get("lastEntry") or "15:00")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "09:35" <= le <= "15:30" else "15:00"
    K.num(p, out, (("minDepth", 0, 500, 0.0), ("maxAbove", 1, 30, 3), ("bufTicks", 0, 40, 2), ("tgtR", 0.5, 10, 2.0), ("minRR", 0, 10, 1.0), ("days", 20, 3000, 500)),
          ("maxAbove", "bufTicks", "days"))
    out["tickers"] = K.tickers_of(p)
    return out


def describe(p):
    tg = {"vwap": "the VWAP", "mid": "the middle of last week's range", "other": "the other side of last week's range", "r": f"{p['tgtR']:g}R"}[p["tgt"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute: before {p['lastEntry']}, price runs past last week's high / low" + (" (not yet taken this week)" if p["fresh"] else "")
            + (f" by {p['minDepth']:g}+ pts" if p["minDepth"] else "") + f" and closes back inside within {p['maxAbove']} candles: fade it, stop past the sweep + "
            f"{p['bufTicks']} tick(s), target {tg}" + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "") + ", flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def weeks(sym):
    """{day: (last week's high, low, this week's high so far before the day, low so far)}."""
    D = futdata.daily(sym)
    wk = lambda d: dt.date.fromisoformat(d).isocalendar()[:2]
    byw = {}
    for x in D:
        byw.setdefault(wk(x[0]), []).append(x)
    order = sorted(byw)
    out = {}
    for i, w in enumerate(order[1:], 1):
        prev = byw[order[i - 1]]
        ph, pl = max(x[2] for x in prev), min(x[3] for x in prev)
        hs, ls = None, None
        for x in byw[w]:
            out[x[0]] = (ph, pl, hs, ls)
            hs, ls = max(hs or x[2], x[2]), min(ls or x[3], x[3])
    return out


def all_trades(sub, p, cells=None, prog=None):
    import random
    trades, base = [], []
    rnd = random.Random(149)
    for sym in p["tickers"]:
        W = weeks(sym)
        for d, (on, R, F, V) in sorted(K.sessions(sym, p).items()):
            if d not in W or len(R) < 10:
                continue
            ph, pl, hs, ls = W[d]
            sides = [-1, 1]
            if p["fresh"]:
                sides = [s for s in sides if not (s < 0 and hs is not None and hs > ph) and not (s > 0 and ls is not None and ls < pl)]
            if R[0][1] > ph:
                sides = [s for s in sides if s > 0]
            if R[0][1] < pl:
                sides = [s for s in sides if s < 0]
            for x in onsweep.fade(R, V, p, ph, pl, sides):
                x.update(s=sym, d=d, cell=None)
                trades.append(x)
                base.append((d, x["side"], K.baseline(R, x, rnd)))
    return trades, base


def decorate(x, p):
    x["tod"] = K.tod(x["in"])
    x["wd"] = K.weekday(x["d"])
    dp = x.get("depth") or 0
    x["depth"] = "under 5 pts" if dp < 5 else "5-15 pts" if dp < 15 else "15+ pts"


def overlay(sub, p, sym, start, end):
    W = weeks(sym)
    lines = {"h": {"name": "Last week's high", "color": "#E07AE0", "pts": []}, "l": {"name": "Last week's low", "color": "#E07AE0", "pts": []}}
    for d, (on, R, F, V) in sorted(K.sessions(sym, {**p, "_start": start}).items()):
        if start <= d <= end and d in W and R:
            lines["h"]["pts"] += [[R[0][0], W[d][0]], [R[-1][0], W[d][0]]]
            lines["l"]["pts"] += [[R[0][0], W[d][1]], [R[-1][0], W[d][1]]]
    return {"lines": list(lines.values()), "ranges": []}
