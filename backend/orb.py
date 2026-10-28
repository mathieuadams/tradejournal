"""Opening range breakout + FVG retest + engulfing (the "3 to 1" set-up), on 1-minute candles.

  1. The opening range: the high / low of the first `rangeMin` minutes (the 09:30 5-minute candle by default).
  2. A 1-minute close outside it = the breakout, and the move leaves a fair value gap (3 candles: the third's high
     under the first's low for a break down; the mirror up) within `fvgWin` candles of the breakout candle.
  3. Price comes back into the gap (the retest); a close beyond the far side of the gap kills the set-up.
  4. An engulfing candle in the breakout's direction within `confWin` candles of the retest: enter at the next open.
  5. Stop beyond the retest's extreme (the highest high since the retest for a short), target `tgtR` x the risk,
     flat at 15:55. One trade per ticker per day; no entry after `lastEntry`.
Same candle through the stop and the target: the stop counts."""
import datetime as dt

DEFAULT = {"tickers": ["QQQ", "SPY"], "rangeMin": 5, "tgtR": 3.0, "fvgWin": 5, "confWin": 10, "lastEntry": "11:30", "side": "both", "days": 250}
ROW = ("tgtR", (2.0, 3.0, 4.0), "R target")
COL = ("rangeMin", (5, 15, 30))
COL_NAMES = {5: "5-min range", 15: "15-min range", 30: "30-min range"}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("s", "Ticker"), ("wd", "Weekday"), ("rng", "Opening range size"),
           ("gap", "Gap size vs the range"), ("wait", "Minutes from the breakout to the entry")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "orbfvg", "tf": 1, "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    try:
        out["rangeMin"] = int(p.get("rangeMin")) if int(p.get("rangeMin")) in (1, 5, 10, 15, 30, 60) else 5
    except (TypeError, ValueError):
        out["rangeMin"] = 5
    for k, lo, hi in (("tgtR", 0.5, 10), ("fvgWin", 1, 30), ("confWin", 1, 60), ("days", 20, 800)):
        try:
            out[k] = max(lo, min(hi, float(p.get(k))))
        except (TypeError, ValueError):
            out[k] = DEFAULT[k]
    for k in ("fvgWin", "confWin", "days"):
        out[k] = int(out[k])
    le = str(p.get("lastEntry") or "11:30")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "09:35" <= le <= "15:30" else "11:30"
    tk = p.get("tickers") or DEFAULT["tickers"]
    tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
    out["tickers"] = [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:6] or list(DEFAULT["tickers"])
    return out


def describe(p):
    side = "both sides" if p["side"] == "both" else ("breaks up → long" if p["side"] == "long" else "breaks down → short")
    return (f"{', '.join(p['tickers'])} 1-minute: a close outside the first {p['rangeMin']}-minute range ({side}) leaving a fair value gap, "
            f"price back into the gap, an engulfing candle the breakout's way: enter the next open, stop beyond the retest, "
            f"target {p['tgtR']:g}R, no entry after {p['lastEntry']}, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def _short(B, p):
    """The short set-up of one session on candles B (prices may be negated for the long side). One trade or None."""
    end_rng = (dt.datetime.strptime("09:30", "%H:%M") + dt.timedelta(minutes=p["rangeMin"])).strftime("%H:%M")
    rng = [b for b in B if b[0][11:16] < end_rng]
    if not rng:
        return None
    lo, hi = min(b[3] for b in rng), max(b[2] for b in rng)
    k = len(rng)
    n = len(B)
    bk = None
    while k < n:                                       # 2. the breakout: a close under the range
        if B[k][4] < lo:
            bk = k
            break
        if B[k][0][11:16] >= p["lastEntry"]:
            return None
        k += 1
    if bk is None:
        return None
    fvg = None
    for m in range(max(bk, 2), min(n, bk + p["fvgWin"] + 1)):  # the gap: candle m's high under candle m-2's low
        if B[m][2] < B[m - 2][3]:
            fvg = (B[m][2], B[m - 2][3], m)            # (bottom, top, its last candle)
            break
    if not fvg:
        return None
    bot, top, m0 = fvg
    touch = None
    for k in range(m0 + 1, n):                         # 3. back into the gap
        t, o, h, l, c = B[k]
        if t[11:16] >= p["lastEntry"]:
            return None
        if c > top:
            return None
        if h >= bot:
            touch = k
            break
    if touch is None:
        return None
    hi_since = B[touch][2]
    for k in range(touch, min(n - 1, touch + p["confWin"] + 1)):   # 4. a bearish engulfing
        t, o, h, l, c = B[k]
        hi_since = max(hi_since, h)
        if c > top or t[11:16] >= p["lastEntry"]:
            return None
        pt, po, ph, pl, pc = B[k - 1]
        if pc > po and c < o and o >= pc and c <= po:
            e = B[k + 1][1]
            stop = hi_since
            if stop <= e:
                return None
            R = stop - e
            tgt = e - p["tgtR"] * R
            for j in range(k + 1, n):                  # 5. the trade
                tj, oj, hj, lj, cj = B[j]
                if j > k + 1 and oj >= stop:
                    x, why = oj, "stop"
                elif hj >= stop:
                    x, why = stop, "stop"
                elif j > k + 1 and oj <= tgt:
                    x, why = oj, "target"
                elif lj <= tgt:
                    x, why = tgt, "target"
                elif tj[11:16] >= "15:55" or j == n - 1:
                    x, why = cj, "close"
                else:
                    continue
                return {"in": B[k + 1][0], "out": tj, "e": e, "x": x, "why": why, "R": R, "stop": stop, "tgt": tgt, "rng": hi - lo,
                        "gap": top - bot, "bk": B[bk][0], "touch": B[touch][0], "k": k + 1}
    return None


def setups(B, p):
    out = []
    if p["side"] in ("short", "both"):
        x = _short(B, p)
        if x:
            out.append({**x, "side": "short", "pct": (x["e"] - x["x"]) / x["e"] * 100, "r": (x["e"] - x["x"]) / x["R"],
                        "tgtDist": x["e"] - x["tgt"], "stopDist": x["stop"] - x["e"]})
    if p["side"] in ("long", "both"):
        N = [(t, -o, -l, -h, -c) for t, o, h, l, c in B]
        x = _short(N, p)
        if x:
            e, xx = -x["e"], -x["x"]
            out.append({**x, "e": e, "x": xx, "stop": -x["stop"], "tgt": -x["tgt"], "side": "long", "pct": (xx - e) / e * 100, "r": (xx - e) / x["R"],
                        "tgtDist": x["e"] - x["tgt"], "stopDist": x["stop"] - x["e"]})
    out.sort(key=lambda z: z["in"])
    return out[:1]                                    # one trade per ticker per day: the first


def all_trades(sub, p, cells=None, prog=None):
    import random
    import range50
    trades, base = [], []
    rnd = random.Random(5)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {p['days']} days of 1-minute candles")
        by = range50.bars(sub, sym, p["days"], 1)
        for d in sorted(by):
            B = by[d]
            if len(B) < 60:
                continue
            for x in setups(B, p):
                x.update(s=sym, d=d, cell=(cells or {}).get(d))
                trades.append(x)
                base.append((d, x["side"], range50.baseline(B, x, rnd)))
    return trades, base


def decorate(x, p):
    import range50
    x["tod"] = range50.tod(x["in"])
    x["wd"] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.date.fromisoformat(x["d"]).weekday()]
    rp = x["rng"] / abs(x["e"]) * 100 if x["e"] else 0
    x["rng"] = "under 0.3%" if rp < 0.3 else "0.3-0.6%" if rp < 0.6 else "0.6%+"
    gr = x["gap"] / (rp / 100 * abs(x["e"])) if rp else 0
    x["gap"] = "under 10% of the range" if gr < 0.1 else "10-30%" if gr < 0.3 else "30%+"
    w = (int(x["in"][11:13]) * 60 + int(x["in"][14:16])) - (int(x["bk"][11:13]) * 60 + int(x["bk"][14:16]))
    x["wait"] = "under 5 min" if w < 5 else "5-15 min" if w < 15 else "15-30 min" if w < 30 else "30 min+"


def coverage(sub, p, prog):
    import range50
    return range50.coverage(sub, {**p, "tf": 1}, prog)
