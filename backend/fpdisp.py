"""Fair price displacement, on NQ / ES.

Short (long = the mirror under the fair price):
  1. Fair price (`fair`): open = the 09:30 open; vwap = the session VWAP (from 18:00) at that candle.
  2. Price is above the fair price, by at least `minAwayPct`% of the price.
  3. The displacement candle: a red candle whose body is at least `bodyMult` x the previous candle's body and that
     closes below the previous candle's low (below its wick). Sell at that close (from `firstEntry`, before `lastEntry`).
  4. Stop (`stop`): bar = the displacement candle's high, swing = the highest high of the last `swingN` candles
     (+ `bufTicks` ticks).
  5. Target: the fair price (the 09:30 open, or the VWAP at the entry). Skipped when it's under `minRR` x the risk.
  6. Flat before the next one; at most `maxDay` trades a session, flat at 15:55."""
import sessionkit as K

DEFAULT = {"tickers": ["NQ"], "tf": 5, "fair": "open", "minAwayPct": 0.1, "bodyMult": 1.0, "stop": "bar", "swingN": 5, "bufTicks": 2, "minRR": 1.0,
           "firstEntry": "09:35", "lastEntry": "13:00", "maxDay": 2, "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("away", "Distance from the fair price"), ("body", "Body vs the previous"),
           ("nth", "Trade of the session")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    tf = str(p.get("tf"))
    out = {"setup": "fpdisp", "tf": 1 if tf == "1" else 15 if tf == "15" else 5, "fair": p.get("fair") if p.get("fair") in ("open", "vwap") else "open",
           "stop": p.get("stop") if p.get("stop") in ("bar", "swing") else "bar", "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    for k, dflt, lo in (("firstEntry", "09:35", "09:31"), ("lastEntry", "13:00", "09:35")):
        v = str(p.get(k) or dflt)
        out[k] = v if len(v) == 5 and v[2] == ":" and lo <= v <= "15:30" else dflt
    K.num(p, out, (("minAwayPct", 0, 5, 0.1), ("bodyMult", 0.5, 10, 1.0), ("swingN", 1, 50, 5), ("bufTicks", 0, 40, 2), ("minRR", 0, 10, 1.0),
                   ("maxDay", 1, 10, 2), ("days", 20, 3000, 500)), ("swingN", "bufTicks", "maxDay", "days"))
    out["tickers"] = K.tickers_of(p)
    return out


def describe(p):
    fair = "the 09:30 open" if p["fair"] == "open" else "the VWAP"
    st = "the displacement candle" if p["stop"] == "bar" else f"the last {p['swingN']} candles"
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute: fair price = {fair}. Away from it (by {p['minAwayPct']:g}%+), a displacement candle back toward it "
            f"(body {p['bodyMult']:g}×+ the previous, closing past its wick): enter at that close ({side}, {p['firstEntry']}-{p['lastEntry']}), "
            f"stop past {st} + {p['bufTicks']} tick(s), target the fair price" + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "")
            + f", at most {p['maxDay']} a session, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def engine(day, on, R, F, V, p):
    if len(R) < 3:
        return []
    o0, out, k, buf = R[0][1], [], 1, p["bufTicks"] * K.TICK
    while k < len(R) and len(out) < p["maxDay"]:
        t, o, h, l, c = R[k]
        if K.end_of(t, p["tf"])[11:16] > p["lastEntry"]:
            break
        if K.end_of(t, p["tf"])[11:16] < p["firstEntry"]:
            k += 1
            continue
        fair = o0 if p["fair"] == "open" else V[k]
        pt, po, ph, pl, pc = R[k - 1]
        d = -1 if o > fair else 1                       # above fair: look for a red displacement down (short)
        body, pbody = abs(c - o), abs(pc - po)
        disp = (c - o) * d > 0 and body >= p["bodyMult"] * pbody and body > 0 and ((c < pl) if d < 0 else (c > ph))
        away = abs(o - fair) >= o * p["minAwayPct"] / 100
        ok_side = not ((p["side"] == "long" and d < 0) or (p["side"] == "short" and d > 0))
        if not (disp and away and ok_side and (fair - c) * d > 0):
            k += 1
            continue
        e = c
        if p["stop"] == "bar":
            stop = h + buf if d < 0 else l - buf
        else:
            w = R[max(0, k - p["swingN"] + 1):k + 1]
            stop = max(b[2] for b in w) + buf if d < 0 else min(b[3] for b in w) - buf
        risk, tgt = abs(stop - e), fair
        if risk <= 0 or (p["minRR"] and abs(tgt - e) < p["minRR"] * risk):
            k += 1
            continue
        x, ot, why, j = K.walk(R, k + 1, d, e, stop, tgt)
        out.append({"in": K.end_of(t, p["tf"]), "out": ot, "e": e, "x": x, "why": why, "side": "long" if d > 0 else "short", "stop": stop, "tgt": tgt,
                    "stopDist": risk, "tgtDist": abs(tgt - e), "cf": t, "sweepLvl": fair, "away": abs(o - fair) / o * 100,
                    "body": body / pbody if pbody else 99, "nth": len(out) + 1, "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk})
        k = j + 1                                        # flat before the next one
    return out


def all_trades(sub, p, cells=None, prog=None):
    return K.run_all(sub, p, engine, f"{p['tf']}-minute candles + the fair price", seed=53)


def decorate(x, p):
    x["tod"] = K.tod(x["in"])
    x["wd"] = K.weekday(x["d"])
    a = x.get("away") or 0
    x["away"] = "under 0.25%" if a < 0.25 else "0.25-0.5%" if a < 0.5 else "0.5%+"
    b = x.get("body") or 0
    x["body"] = "1-1.5×" if b < 1.5 else "1.5-2.5×" if b < 2.5 else "2.5×+"
    x["nth"] = "1st" if x.get("nth") == 1 else "2nd+"


def overlay(sub, p, sym, start, end):
    """The fair price (the 09:30 open) of each session; the VWAP version uses the chart's VWAP."""
    if p["fair"] != "open":
        return {"lines": [], "ranges": []}
    pts = []
    for d, (on, R, F, V) in K.sessions(sym, {**p, "_start": start}).items():
        if start <= d <= end and R:
            pts += [[R[0][0], R[0][1]], [R[-1][0], R[0][1]]]
    return {"lines": [{"name": "Fair price (09:30 open)", "color": "#7FD88F", "pts": pts}], "ranges": []}
