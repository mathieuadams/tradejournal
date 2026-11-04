"""Inside bar breakout.

  The mother candle, then an inside candle entirely within it (its high at or under the mother's high and its low at or
  over the mother's low, wicks included). Within `wait` candles after the inside candle, price breaks its high (long)
  or its low (short): `entry` break = a stop order one tick past it (filled at the open when the candle gaps through);
  close = at the first close past it. A candle that breaks both sides is skipped (which came first is unknown).
  Stop: past the other side of the inside candle (+ `bufTicks`). Target `tgtR` x the risk, or the managed exit.
  Filters: the mother at least `minMother` x the average range of the last 20 candles; the inside candle at most
  `maxInside`% of the mother's range; the EMA bias (none by default). `hours`: rth (09:30-`lastEntry`) or all (from
  18:00). One position at a time, at most `maxDay` a session, flat at 15:55. In the entry candle the stop is checked
  before the target."""
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "entry": "break", "wait": 1, "minMother": 0.0, "maxInside": 100, "hours": "rth", "lastEntry": "15:00", "maxDay": 5,
           "tgtR": 2.0, "bias": "none", "side": "both", "exit": "target", "bufTicks": 0, "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("ms", "Mother candle (× the average range)"), ("is", "Inside candle (% of the mother)"),
           ("wb", "With the EMA bias"), ("ses", "Session")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    tf = str(p.get("tf"))
    out = {"setup": "insidebar", "tf": 1 if tf == "1" else 15 if tf == "15" else 5, "entry": p.get("entry") if p.get("entry") in ("break", "close") else "break",
           "hours": p.get("hours") if p.get("hours") in ("rth", "all") else "rth", "lastEntry": N.hhmm(p.get("lastEntry"), "15:00", "09:35", "15:50")}
    N.K.num(p, out, (("wait", 1, 20, 1), ("minMother", 0, 10, 0.0), ("maxInside", 5, 100, 100), ("maxDay", 1, 50, 5)), ("wait", "maxDay"))
    N.clean_common(p, out, bias="none")
    if p.get("bufTicks") in (None, ""):
        out["bufTicks"] = 0
    if p.get("tgtR") in (None, ""):
        out["tgtR"] = 2.0
    return out


def describe(p):
    b = {"all3": ", with the 15 / 30 / 60-minute EMA bias", "h1": ", with the hourly EMA bias", "none": ""}[p["bias"]]
    ent = "a stop order 1 tick past the inside candle's high / low" if p["entry"] == "break" else "the first close past the inside candle's high / low"
    flt = (f", mother at least {p['minMother']:g}× the average range" if p["minMother"] else "") + (f", inside candle at most {p['maxInside']:g}% of it" if p["maxInside"] < 100 else "")
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute ({'overnight + NYSE' if p['hours'] == 'all' else 'NYSE session'}): an inside candle (entirely within the "
            f"one before){flt}; enter on {ent} within {p['wait']} candle(s){b} ({side}), stop past its other side + {p['bufTicks']} tick(s)" + N.exit_txt(p)
            + f", no entry after {p['lastEntry']}, at most {p['maxDay']} a session, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    out, k, tick = [], 1, N.K.TICK
    buf, rth0, flat = p["bufTicks"] * tick, f"{d}T09:30", f"{d}T15:55"
    lo_i = 0 if p["hours"] == "all" else next((i for i, b in enumerate(F) if b[0] >= rth0), len(F))
    last = f"{d}T{p['lastEntry']}"
    while k < len(F) - 1 and len(out) < p["maxDay"]:
        m, ib = F[k - 1], F[k]
        if ib[0][:16] >= last:
            break
        if k - 1 < lo_i or "16:00" <= ib[0][11:16] < "18:00" or not (ib[2] <= m[2] and ib[3] >= m[3]):
            k += 1
            continue
        mr, ir = m[2] - m[3], ib[2] - ib[3]
        a = N.avg_range(F, k - 1, prev=prev)
        if mr <= 0 or (p["minMother"] and mr < p["minMother"] * a) or (p["maxInside"] < 100 and ir > mr * p["maxInside"] / 100):
            k += 1
            continue
        hi, lo = ib[2], ib[3]
        up, dn = hi + tick, lo - tick
        hit = None
        for j in range(k + 1, min(len(F), k + 1 + p["wait"])):
            t, o, h, l, c = F[j]
            if t[:16] >= last or "16:00" <= t[11:16] < "18:00":
                break
            if p["entry"] == "break":
                bu, bd = h >= up, l <= dn
                if bu and bd:
                    break                                   # both sides in one candle: unknown order, skip
                if bu or bd:
                    dd = 1 if bu else -1
                    e = (max(o, up) if dd > 0 else min(o, dn))
                    hit = (j, dd, e)
                    break
            else:
                if c > hi or c < lo:
                    hit = (j, 1 if c > hi else -1, c)
                    break
        if not hit:
            k += 1
            continue
        j, dd, e = hit
        if not N.ok_side(dd, N.bias(E, F[j][0], e, p), p):
            k = j
            continue
        stop = (lo - tick - buf) if dd > 0 else (hi + tick + buf)
        risk = (e - stop) * dd
        if risk <= 0:
            k = j
            continue
        tgt = e + dd * p["tgtR"] * risk if p["exit"] == "target" and p["tgtR"] else None
        t, o, h, l, c = F[j]
        b = N.bias(E, t, e, {**p, "bias": "all3"})
        extra = dict(ms=mr / a if a else None, isz=ir / mr * 100, wb=b == dd, ses=t >= rth0, rt=F[k][0], rhi=hi, rlo=lo)
        if p["entry"] == "break" and ((dd > 0 and l <= stop) or (dd < 0 and h >= stop)):
            x = {"in": t, "out": t, "e": e, "x": stop, "why": "stop", "side": "long" if dd > 0 else "short", "stop": stop, "tgt": tgt, "stopDist": risk,
                 "tgtDist": abs(tgt - e) if tgt else None, "cf": t, "part": None, "pct": dd * (stop / e - 1) * 100, "r": -1.0, **extra}
            jx = j
        elif p["entry"] == "break" and tgt is not None and ((dd > 0 and h >= tgt) or (dd < 0 and l <= tgt)):
            x = {"in": t, "out": t, "e": e, "x": tgt, "why": "target", "side": "long" if dd > 0 else "short", "stop": stop, "tgt": tgt, "stopDist": risk,
                 "tgtDist": abs(tgt - e), "cf": t, "part": None, "pct": dd * (tgt / e - 1) * 100, "r": p["tgtR"], **extra}
            jx = j
        else:
            x, jx = N.trade(F, j, dd, e, stop, tgt, risk, p, E, flat, **extra)
            if p["entry"] == "break":
                x["in"] = t                                # filled inside the candle, not at its close
        out.append((x, (max(lo_i, 1), last, flat)))
        k = jx + 1
    return out


def all_trades(sub, p, cells=None, prog=None):
    return N.run(sub, p, engine, 103, prog, f"{p['tf']}-minute candles")


def decorate(x, p):
    N.decorate_common(x)
    v = x.get("ms")
    x["ms"] = "—" if v is None else "under 1×" if v < 1 else "1-1.5×" if v < 1.5 else "1.5-2.5×" if v < 2.5 else "2.5×+"
    v = x.get("isz") or 0
    x["is"] = "under 33%" if v < 33 else "33-50%" if v < 50 else "50-75%" if v < 75 else "75%+"
    x["wb"] = "with the bias" if x.get("wb") else "against / no bias"
    x["ses"] = "NYSE" if x.get("ses") else "overnight"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
