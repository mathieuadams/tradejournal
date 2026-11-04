"""London breakout of the Asian range.

  The Asian range: high / low of the session from 18:00 to `asiaEnd` (02:00). After it, the first `tf`-minute close
  outside it, before `lastEntry`, is the trade (in the EMA bias direction; against it = no trade that night): enter at
  that close. Skipped when the range is under `minRangePct` / over `maxRangePct` % of the price.
  Stop (`stop`): opp = the other side of the range, mid = its middle, bar = the breakout candle (+ `bufTicks`).
  Target: the broken edge + `tgtMult` x the range (0 = none), or the managed exit. Flat at `flatAt`."""
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "asiaEnd": "02:00", "lastEntry": "06:00", "flatAt": "09:30", "stop": "mid", "tgtMult": 1.0, "minRangePct": 0.0,
           "maxRangePct": 0.0, "bias": "all3", "side": "both", "exit": "target", "bufTicks": 2, "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("rg", "Asian range"), ("vr", "Volatility (percentile of the daily range)")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "ldnbrk", "tf": 15 if str(p.get("tf")) == "15" else 5, "asiaEnd": p.get("asiaEnd") if p.get("asiaEnd") in ("00:00", "01:00", "02:00", "03:00") else "02:00",
           "lastEntry": N.hhmm(p.get("lastEntry"), "06:00", "00:30", "15:30"), "flatAt": p.get("flatAt") if p.get("flatAt") in ("06:00", "08:00", "09:30", "11:00", "15:55") else "09:30",
           "stop": p.get("stop") if p.get("stop") in ("opp", "mid", "bar") else "mid"}
    N.K.num(p, out, (("tgtMult", 0, 10, 1.0), ("minRangePct", 0, 5, 0.0), ("maxRangePct", 0, 10, 0.0)))
    N.clean_common(p, out)
    return out


def describe(p):
    b = {"all3": " in the 15 / 30 / 60-minute EMAs' direction", "h1": " in the hourly EMA's direction", "none": ""}[p["bias"]]
    st = {"opp": "the other side of the range", "mid": "the middle of the range", "bar": "the breakout candle"}[p["stop"]]
    tg = (f", target the edge + {p['tgtMult']:g}× the range" if p["tgtMult"] else "") if p["exit"] == "target" else N.ifvg._exit_txt(p)
    flt = (f", range at least {p['minRangePct']:g}%" if p["minRangePct"] else "") + (f", at most {p['maxRangePct']:g}%" if p["maxRangePct"] else "")
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute: the Asian range (18:00-{p['asiaEnd']} ET){flt}; the first close outside it{b} before "
            f"{p['lastEntry']}: enter at that close, stop at {st} + {p['bufTicks']} tick(s){tg}, flat at {p['flatAt']}")


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    ta, tl, tf_ = N.stamp(F, p["asiaEnd"]), N.stamp(F, p["lastEntry"]), N.stamp(F, p["flatAt"])
    if not (ta < tl <= tf_):
        return []
    asia = [b for b in F if b[0][:16] < ta]
    if len(asia) < 3:
        return []
    hi, lo = max(b[2] for b in asia), min(b[3] for b in asia)
    rng, c0 = hi - lo, asia[-1][4]
    if rng <= 0 or (p["minRangePct"] and rng < c0 * p["minRangePct"] / 100) or (p["maxRangePct"] and rng > c0 * p["maxRangePct"] / 100):
        return []
    buf = p["bufTicks"] * N.K.TICK
    for k in range(len(asia), len(F)):
        t, o, h, l, c = F[k]
        if t[:16] >= tl:
            return []
        dd = 1 if c > hi else -1 if c < lo else 0
        if not dd:
            continue
        if not N.ok_side(dd, N.bias(E, t, c, p), p):
            return []                                  # the first breakout is the one
        stop = {"opp": lo if dd > 0 else hi, "mid": (hi + lo) / 2, "bar": l if dd > 0 else h}[p["stop"]] - dd * buf
        risk = (c - stop) * dd
        if risk <= 0:
            return []
        tgt = ((hi if dd > 0 else lo) + dd * p["tgtMult"] * rng) if p["tgtMult"] and p["exit"] == "target" else None
        if tgt is not None and (tgt - c) * dd <= 0:
            return []
        x, j = N.trade(F, k, dd, c, stop, tgt if tgt is not None else c + dd * N.FAR, risk, p, E, tf_, rhi=hi, rlo=lo, rg=rng / c * 100)
        return [(x, (len(asia), tl, tf_))]
    return []


def all_trades(sub, p, cells=None, prog=None):
    vr = {s: N.vol_rank(s) for s in p["tickers"]}
    tr, base = N.run(sub, p, engine, 79, prog, "the Asian range")
    for x in tr:
        x["vr"] = vr.get(x["s"], {}).get(x["d"], vr.get(x["s"], {}).get("next"))
    return tr, base


def decorate(x, p):
    N.decorate_common(x)
    g = x.get("rg") or 0
    x["rg"] = "under 0.3%" if g < 0.3 else "0.3-0.6%" if g < 0.6 else "0.6%+"
    v = x.get("vr")
    x["vr"] = "—" if v is None else "low (under 33)" if v < 33 else "middle (33-66)" if v < 67 else "high (67+)"


def overlay(sub, p, sym, start, end):
    ov = N.overlay(sub, p, sym, start, end)
    out = []                                           # this rule's Asian range (to `asiaEnd`), drawn to the last entry
    for d, on, R, F, V in N.ifvg._series(sym, {"tf": p["tf"], "days": p["days"], "_start": start})[0]:
        if d > end or not F:
            continue
        ta = N.stamp(F, p["asiaEnd"])
        a = [b for b in F if b[0][:16] < ta]
        if a:
            out.append({"day": d, "hi": max(b[2] for b in a), "lo": min(b[3] for b in a), "t0": a[0][0], "t1": a[-1][0], "t2": N.stamp(F, p["lastEntry"]), "label": "Asia"})
    ov["ranges"] = out
    return ov
