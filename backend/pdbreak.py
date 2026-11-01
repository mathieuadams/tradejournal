"""Prior-day high / low break and retest, on NQ / ES (daily levels, 5 / 15-minute entry).

Long (short = the mirror at the prior-day low):
  1. The level: yesterday's regular-hours high (PDH).
  2. Bias (`bias`): close = only when yesterday closed in the top `closePct` of its range (bottom for shorts);
     trend = yesterday's close above its 20-day average (below for shorts); none.
  3. The break: a `brkTf`-minute candle closes above the PDH (before `lastEntry`).
  4. The retest: within `wait` candles, a `tf`-minute candle trades back to within `tolTicks` ticks of the PDH and
     closes above it: buy at that close. A close back under the PDH first = the break failed: no trade.
  5. Stop: under the retest candle's low / the PDH, whichever is lower (- `bufTicks` ticks).
  6. Target (`tgt`): atr = today's open + `tgtAtr` x the 14-day average range; r = `tgtR` x the risk. Skipped when the
     target isn't at least `minRR` x the risk.
  7. One trade per side per session, flat at 15:55."""
import sessionkit as K

DEFAULT = {"tickers": ["NQ"], "tf": 5, "brkTf": 15, "bias": "close", "closePct": 0.33, "wait": 12, "tolTicks": 4, "bufTicks": 2,
           "tgt": "atr", "tgtAtr": 1.0, "tgtR": 2.0, "minRR": 1.0, "lastEntry": "12:00", "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("yc", "Where yesterday closed"), ("gap", "Where today opened"),
           ("tr", "Yesterday vs its 20-day average")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    tf = 15 if str(p.get("tf")) == "15" else 5
    bt = int(p.get("brkTf")) if str(p.get("brkTf")) in ("5", "15", "30", "60") else 15
    out = {"setup": "pdbreak", "tf": tf, "brkTf": max(tf, bt),
           "bias": p.get("bias") if p.get("bias") in ("close", "trend", "none") else "close",
           "tgt": p.get("tgt") if p.get("tgt") in ("atr", "r") else "atr",
           "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    le = str(p.get("lastEntry") or "12:00")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "09:45" <= le <= "15:30" else "12:00"
    K.num(p, out, (("closePct", 0.05, 0.5, 0.33), ("wait", 1, 60, 12), ("tolTicks", 0, 80, 4), ("bufTicks", 0, 40, 2), ("tgtAtr", 0.25, 4, 1.0),
                   ("tgtR", 0.5, 10, 2.0), ("minRR", 0, 10, 1.0), ("days", 20, 3000, 500)), ("wait", "tolTicks", "bufTicks", "days"))
    out["tickers"] = K.tickers_of(p)
    return out


def describe(p):
    bias = {"close": f" when yesterday closed in its top / bottom {round(p['closePct'] * 100)}%", "trend": " with the 20-day trend", "none": ""}[p["bias"]]
    tg = f"today's open ± {p['tgtAtr']:g}× the daily ATR" if p["tgt"] == "atr" else f"{p['tgtR']:g}R"
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])}: a {p['brkTf']}-minute close past yesterday's high / low{bias} ({side}), then a {p['tf']}-minute retest "
            f"(within {p['wait']} candles, {p['tolTicks']} ticks) that closes back out: enter at that close, stop back inside yesterday's range "
            f"+ {p['bufTicks']} tick(s), target {tg}" + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "") + f", no entry after {p['lastEntry']}, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def _mins(t):
    return int(t[11:13]) * 60 + int(t[14:16]) - 570          # minutes since 09:30


def engine(day, on, R, F, V, H, p):
    if len(H) < 15 or not R:
        return []
    y = H[-1]
    a, sma = K.atr(H), K.sma_close(H)
    pos = (y["c"] - y["l"]) / (y["h"] - y["l"]) if y["h"] > y["l"] else 0.5
    allow = {1: True, -1: True}
    if p["bias"] == "close":
        allow = {1: pos >= 1 - p["closePct"], -1: pos <= p["closePct"]}
    elif p["bias"] == "trend":
        if sma is None:
            return []
        allow = {1: y["c"] > sma, -1: y["c"] < sma}
    out, o0, tol, buf = [], R[0][1], p["tolTicks"] * K.TICK, p["bufTicks"] * K.TICK
    for d in ((-1,) if p["side"] == "short" else (1,) if p["side"] == "long" else (-1, 1)):
        if not allow[d]:
            continue
        lvl = y["h"] if d > 0 else y["l"]
        brk = None
        for k, (t, o, h, l, c) in enumerate(R):
            if K.end_of(t, p["tf"])[11:16] > p["lastEntry"]:
                break
            bucket_end = (_mins(t) + p["tf"]) % p["brkTf"] == 0
            if brk is None:
                if bucket_end and (c - lvl) * d > 0:
                    brk = k                                       # the higher-timeframe close past the level
                continue
            if (c - lvl) * d <= 0:
                break                                             # closed back inside before a retest: failed break
            if k - brk > p["wait"]:
                break
            touch = (l <= lvl + tol) if d > 0 else (h >= lvl - tol)
            if not touch:
                continue
            e = c
            stop = (min(l, lvl) - buf) if d > 0 else (max(h, lvl) + buf)
            risk = abs(e - stop)
            tgt = o0 + d * p["tgtAtr"] * a if p["tgt"] == "atr" else e + d * p["tgtR"] * risk
            if risk <= 0 or (tgt - e) * d <= 0 or (p["minRR"] and abs(tgt - e) < p["minRR"] * risk):
                break                                             # target behind / too close: no trade this side
            x, ot, why, _ = K.walk(R, k + 1, d, e, stop, tgt)
            out.append({"in": K.end_of(t, p["tf"]), "out": ot, "e": e, "x": x, "why": why, "side": "long" if d > 0 else "short",
                        "stop": stop, "tgt": tgt, "stopDist": risk, "tgtDist": abs(tgt - e), "bk": R[brk][0], "rt": t, "sweepLvl": lvl,
                        "yc": pos, "gap": 1 if o0 > y["h"] else -1 if o0 < y["l"] else 0, "trd": (y["c"] > sma) if sma else None,
                        "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk})
            break
    return out


def all_trades(sub, p, cells=None, prog=None):
    return K.run_daily(sub, p, engine, "prior-day levels")


def decorate(x, p):
    x["tod"] = K.tod(x["in"])
    x["wd"] = K.weekday(x["d"])
    pos = x.get("yc")
    pos = 0.5 if pos is None else pos
    x["yc"] = "top third" if pos >= 2 / 3 else "bottom third" if pos <= 1 / 3 else "middle third"
    x["gap"] = {1: "above yesterday's high", -1: "below yesterday's low", 0: "inside yesterday's range"}.get(x.get("gap"), "inside yesterday's range")
    x["tr"] = "—" if x.get("trd") is None else "above the 20-day average" if x["trd"] else "below the 20-day average"


def overlay(sub, p, sym, start, end):
    return {"lines": K.level_lines(sym, p, start, end, [("h", "Yesterday's high", "#E7C35A"), ("l", "Yesterday's low", "#E7C35A")]), "ranges": []}
