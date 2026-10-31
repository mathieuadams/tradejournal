"""Initial balance breakout, on NQ / ES.

  1. The initial balance (IB): high / low of the first `ibMin` minutes of regular hours (60: 09:30-10:30).
  2. The breakout: the first `tf`-minute candle after the IB that closes outside it, before `lastEntry`: enter at that
     close in the breakout's direction.
  3. Filter (`filter`): vwap = the breakout candle closes on the breakout's side of the session VWAP (from 18:00);
     overnight = only in the overnight's direction (the 09:29 price vs the 18:00 open), when the overnight moved at
     least `onMinPct`% (a smaller move counts as flat: both directions allowed); none.
  4. Stop (`stop`): mid = the middle of the IB, opp = the other side of the IB, bar = the breakout candle's far end
     (+ `bufTicks` ticks). Target: the broken IB edge + `tgtMult` x the IB range.
  5. One trade per session, flat at 15:55."""
import sessionkit as K

DEFAULT = {"tickers": ["NQ"], "tf": 5, "ibMin": 60, "filter": "vwap", "onMinPct": 0.3, "stop": "mid", "bufTicks": 2, "tgtMult": 1.0, "lastEntry": "14:00",
           "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("ibr", "IB range"), ("with", "With the overnight"), ("vw", "Breakout vs the VWAP")]


def clean(p):
    raw = p or {}
    p = {**DEFAULT, **raw}
    if "filter" not in raw and raw.get("trendFilter") is not None:   # saved before the filter choice existed
        p["filter"] = "overnight" if raw.get("trendFilter") else "none"
    out = {"setup": "ibbreak", "tf": 1 if str(p.get("tf")) == "1" else 15 if str(p.get("tf")) == "15" else 5,
           "ibMin": 30 if str(p.get("ibMin")) == "30" else 60,
           "filter": p.get("filter") if p.get("filter") in ("vwap", "overnight", "none") else "vwap",
           "stop": p.get("stop") if p.get("stop") in ("mid", "opp", "bar") else "mid",
           "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    le = str(p.get("lastEntry") or "14:00")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "10:00" <= le <= "15:30" else "14:00"
    K.num(p, out, (("bufTicks", 0, 40, 2), ("tgtMult", 0.25, 5, 1.0), ("onMinPct", 0, 5, 0.3), ("days", 20, 3000, 500)), ("bufTicks", "days"))
    out["tickers"] = K.tickers_of(p)
    return out


def describe(p):
    st = {"mid": "the middle of the IB", "opp": "the other side of the IB", "bar": "the breakout candle"}[p["stop"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute: the first close outside the {p['ibMin']}-minute initial balance"
            + {"vwap": " on the breakout's side of the VWAP", "overnight": f" in the overnight's direction (a {p['onMinPct']:g}%+ overnight move)",
               "none": ""}[p["filter"]] + f" (before {p['lastEntry']}): enter at that close, stop at {st}, "
            f"target the IB edge + {p['tgtMult']:g}× the IB range, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def engine(day, on, R, F, V, p):
    ib_end = f"{day}T{9 + (30 + p['ibMin']) // 60:02d}:{(30 + p['ibMin']) % 60:02d}"
    ib = [b for b in R if b[0] < ib_end]
    if len(ib) < 2 or len(R) <= len(ib):
        return []
    hi, lo = max(b[2] for b in ib), min(b[3] for b in ib)
    rng = hi - lo
    ond = 0
    if on and abs(on[-1][4] - on[0][1]) >= on[0][1] * p["onMinPct"] / 100:
        ond = 1 if on[-1][4] > on[0][1] else -1        # a smaller overnight move counts as flat
    for k in range(len(ib), len(R)):
        t, o, h, l, c = R[k]
        if K.end_of(t, p["tf"])[11:16] > p["lastEntry"]:
            return []
        d = 1 if c > hi else -1 if c < lo else 0
        if not d:
            continue
        blocked = (p["filter"] == "overnight" and ond and d != ond) or (p["filter"] == "vwap" and (c - V[k]) * d <= 0)
        if (p["side"] == "long" and d < 0) or (p["side"] == "short" and d > 0) or blocked:
            return []                                  # the first breakout is the one: against the filter, no trade today
        e = c
        buf = p["bufTicks"] * K.TICK
        stop = {"mid": (hi + lo) / 2, "opp": lo if d > 0 else hi, "bar": l if d > 0 else h}[p["stop"]] - d * buf
        tgt = (hi if d > 0 else lo) + d * p["tgtMult"] * rng
        risk = abs(e - stop)
        if risk <= 0 or (tgt - e) * d <= 0:
            return []
        x, ot, why, _ = K.walk(R, k + 1, d, e, stop, tgt)
        return [{"in": K.end_of(t, p["tf"]), "out": ot, "e": e, "x": x, "why": why, "side": "long" if d > 0 else "short", "stop": stop, "tgt": tgt,
                 "stopDist": risk, "tgtDist": abs(tgt - e), "cf": t, "rhi": hi, "rlo": lo, "ibr": rng, "with": d == ond, "vw": (c - V[k]) * d > 0,
                 "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk}]
    return []


def all_trades(sub, p, cells=None, prog=None):
    return K.run_all(sub, p, engine, f"{p['tf']}-minute candles + the {p['ibMin']}-minute initial balance", seed=37)


def decorate(x, p):
    x["tod"] = K.tod(x["in"])
    x["wd"] = K.weekday(x["d"])
    r = x.get("ibr") or 0
    x["ibr"] = "narrow (under 0.6%)" if r < x["e"] * 0.006 else "normal (0.6-1.2%)" if r < x["e"] * 0.012 else "wide (1.2%+)"
    x["with"] = "with the overnight" if x.get("with") else "against / flat overnight"
    x["vw"] = "VWAP side" if x.get("vw") else "against the VWAP"


def overlay(sub, p, sym, start, end):
    """The initial balance of each session, drawn like the opening range."""
    out = []
    for d, (on, R, F, V) in K.sessions(sym, {**p, "_start": start}).items():
        if not (start <= d <= end):
            continue
        ib_end = f"{d}T{9 + (30 + p['ibMin']) // 60:02d}:{(30 + p['ibMin']) % 60:02d}"
        ib = [b for b in R if b[0] < ib_end]
        if ib:
            out.append({"day": d, "hi": max(b[2] for b in ib), "lo": min(b[3] for b in ib), "t0": ib[0][0], "t1": K.end_of(ib[-1][0], p["tf"]),
                        "t2": f"{d}T{p['lastEntry']}"})
    return {"lines": [], "ranges": out}
