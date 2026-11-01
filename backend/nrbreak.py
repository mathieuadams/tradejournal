"""Narrow-range day breakout, on NQ / ES (daily setup, 15 / 5-minute entry).

  1. The day before (`cond`): nr7 = yesterday's regular-hours range was the narrowest of the last 7 days; inside =
     yesterday stayed inside the day before; either; any = every day (the comparison).
  2. Trend (`trend`): sma = only in the direction of yesterday's close vs its 20-day average; none.
  3. The opening range: the first `orMin` minutes of regular hours. The first `tf`-minute close outside it, before
     `lastEntry`, is the trade (against the trend: no trade that day): enter at that close.
  4. Stop (`stop`): opp = the other side of the opening range, mid = its middle (+ `bufTicks` ticks).
  5. Target: today's open + `tgtAtr` x the 14-day average range (skipped when that's under `minRR` x the risk).
  6. One trade per session, flat at 15:55."""
import sessionkit as K

DEFAULT = {"tickers": ["NQ"], "tf": 5, "cond": "nr7", "trend": "sma", "orMin": 15, "stop": "opp", "bufTicks": 2, "tgtAtr": 1.0, "minRR": 0.5,
           "lastEntry": "12:00", "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("kind", "Yesterday was"), ("with", "With the 20-day trend"),
           ("orr", "Opening range vs ATR")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "nrbreak", "tf": 15 if str(p.get("tf")) == "15" else 5, "cond": p.get("cond") if p.get("cond") in ("nr7", "inside", "either", "any") else "nr7",
           "trend": p.get("trend") if p.get("trend") in ("sma", "none") else "sma", "orMin": 30 if str(p.get("orMin")) == "30" else 60 if str(p.get("orMin")) == "60" else 15,
           "stop": p.get("stop") if p.get("stop") in ("opp", "mid") else "opp", "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    le = str(p.get("lastEntry") or "12:00")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "09:45" <= le <= "15:30" else "12:00"
    K.num(p, out, (("bufTicks", 0, 40, 2), ("tgtAtr", 0.25, 4, 1.0), ("minRR", 0, 10, 0.5), ("days", 20, 3000, 500)), ("bufTicks", "days"))
    if out["tf"] > out["orMin"]:
        out["tf"] = 5
    out["tickers"] = K.tickers_of(p)
    return out


def describe(p):
    cond = {"nr7": "after an NR7 day (yesterday the narrowest range of 7)", "inside": "after an inside day", "either": "after an NR7 or inside day",
            "any": "every day"}[p["cond"]]
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute, {cond}: the first close outside the {p['orMin']}-minute opening range"
            + (" in the 20-day trend's direction" if p["trend"] == "sma" else "") + f" ({side}, before {p['lastEntry']}): enter at that close, "
            f"stop at {'the other side' if p['stop'] == 'opp' else 'the middle'} of the range + {p['bufTicks']} tick(s), target today's open ± "
            f"{p['tgtAtr']:g}× the daily ATR" + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "") + ", flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def kind(H):
    y, rng = H[-1], [x["h"] - x["l"] for x in H[-7:]]
    nr7 = len(H) >= 7 and rng[-1] <= min(rng)
    inside = len(H) >= 2 and y["h"] <= H[-2]["h"] and y["l"] >= H[-2]["l"]
    return nr7, inside


def engine(day, on, R, F, V, H, p):
    if len(H) < 20 or not R:
        return []
    nr7, inside = kind(H)
    if not {"nr7": nr7, "inside": inside, "either": nr7 or inside, "any": True}[p["cond"]]:
        return []
    y, a, sma = H[-1], K.atr(H), K.sma_close(H)
    td = 1 if y["c"] > sma else -1
    end = f"{day}T{9 + (30 + p['orMin']) // 60:02d}:{(30 + p['orMin']) % 60:02d}"
    orb = [b for b in R if b[0] < end]
    if not orb or len(R) <= len(orb):
        return []
    hi, lo, o0 = max(b[2] for b in orb), min(b[3] for b in orb), R[0][1]
    for k in range(len(orb), len(R)):
        t, o, h, l, c = R[k]
        if K.end_of(t, p["tf"])[11:16] > p["lastEntry"]:
            return []
        d = 1 if c > hi else -1 if c < lo else 0
        if not d:
            continue
        if (p["side"] == "long" and d < 0) or (p["side"] == "short" and d > 0) or (p["trend"] == "sma" and d != td):
            return []                                         # the first breakout is the one
        e, buf = c, p["bufTicks"] * K.TICK
        stop = ((lo if d > 0 else hi) if p["stop"] == "opp" else (hi + lo) / 2) - d * buf
        tgt = o0 + d * p["tgtAtr"] * a
        risk = abs(e - stop)
        if risk <= 0 or (tgt - e) * d <= 0 or (p["minRR"] and abs(tgt - e) < p["minRR"] * risk):
            return []
        x, ot, why, _ = K.walk(R, k + 1, d, e, stop, tgt)
        return [{"in": K.end_of(t, p["tf"]), "out": ot, "e": e, "x": x, "why": why, "side": "long" if d > 0 else "short", "stop": stop, "tgt": tgt,
                 "stopDist": risk, "tgtDist": abs(tgt - e), "cf": t, "rhi": hi, "rlo": lo, "kind": [nr7, inside], "with": d == td,
                 "orr": (hi - lo) / a if a else None, "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk}]
    return []


def all_trades(sub, p, cells=None, prog=None):
    return K.run_daily(sub, p, engine, "daily ranges", seed=47)


def decorate(x, p):
    x["tod"] = K.tod(x["in"])
    x["wd"] = K.weekday(x["d"])
    nr7, inside = x.get("kind") or [False, False]
    x["kind"] = "NR7 + inside" if nr7 and inside else "NR7" if nr7 else "inside day" if inside else "neither"
    x["with"] = "with the trend" if x.get("with") else "against the trend"
    r = x.get("orr")
    x["orr"] = "—" if r is None else "under 0.25 ATR" if r < 0.25 else "0.25-0.5 ATR" if r < 0.5 else "0.5+ ATR"


def overlay(sub, p, sym, start, end):
    """The opening range of each session, plus yesterday's high / low."""
    rng = []
    for d, (on, R, F, V) in K.sessions(sym, {**p, "_start": start}).items():
        if not (start <= d <= end):
            continue
        e = f"{d}T{9 + (30 + p['orMin']) // 60:02d}:{(30 + p['orMin']) % 60:02d}"
        orb = [b for b in R if b[0] < e]
        if orb:
            rng.append({"day": d, "hi": max(b[2] for b in orb), "lo": min(b[3] for b in orb), "t0": orb[0][0], "t1": K.end_of(orb[-1][0], p["tf"]),
                        "t2": f"{d}T{p['lastEntry']}"})
    return {"lines": K.level_lines(sym, p, start, end, [("h", "Yesterday's high", "rgba(231,195,90,.6)"), ("l", "Yesterday's low", "rgba(231,195,90,.6)")]),
            "ranges": rng}
