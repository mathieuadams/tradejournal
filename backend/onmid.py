"""Overnight range midpoint: the 09:30 open's half of the overnight range decides the side, the overnight extreme on that
side is the target.

  Overnight = 18:00 (the day before) to 09:30 ET. Mid = (overnight high + overnight low) / 2.
  Open (the close of the 09:25 candle) in the top half: buy, target the overnight high; in the bottom half: sell, target
  the overnight low. Opens within `deadPct`% of the range around the mid, or outside the overnight range (the target is
  already behind), take no trade.
  Hold (`hold`, on by default): a candle that closes beyond the target (above the overnight high for a long, below the
  low for a short) doesn't take profit there: the trade is held until 15:55, with its stop moved (`holdStop`) to the
  entry (be), the broken overnight extreme (level) or left where it was (orig). A touch without a close beyond still
  takes profit at the target.
  Stop (`stop`): mid (the midpoint), opp (the other overnight extreme) or part (`stopPart` x the overnight range from the
  open). Out at `exitAt` if neither is hit. `minRR`: skip when the target is less than that x the risk away."""
import nightkit as N

SETUP = "onmid"
DEFAULT = {"tickers": ["NQ"], "tf": 5, "stop": "mid", "stopPart": 0.25, "deadPct": 0, "minRR": 0, "exitAt": "15:55", "hold": "close", "holdStop": "be", "bias": "none",
           "side": "both", "exit": "target", "days": 500}
FACTORS = [("side", "Side"), ("wd", "Weekday"), ("pos", "Open in the overnight range"), ("held", "Broke and closed beyond the target"), ("onr", "Overnight range"),
           ("vr", "Volatility (percentile of the daily range)")]
EXITS = ("10:00", "10:30", "11:00", "11:30", "12:00", "13:00", "14:00", "15:55")


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": SETUP, "tf": 5, "exitAt": p.get("exitAt") if p.get("exitAt") in EXITS else DEFAULT["exitAt"],
           "stop": p.get("stop") if p.get("stop") in ("mid", "opp", "part") else "mid",
           "hold": "off" if p.get("hold") == "off" else "close",
           "holdStop": p.get("holdStop") if p.get("holdStop") in ("be", "level", "orig") else "be"}
    N.K.num(p, out, (("stopPart", 0.05, 2, 0.25), ("deadPct", 0, 45, 0), ("minRR", 0, 10, 0)))
    N.clean_common(p, out, bias="none")
    return out


def describe(p):
    st = {"mid": "stop at the midpoint", "opp": "stop at the other overnight extreme",
          "part": f"stop {p['stopPart']:g}× the overnight range from the open"}[p["stop"]]
    return (f"{', '.join(p['tickers'])}: the 09:30 open in the top half of the overnight range (18:00-09:30): buy, target the overnight high; "
            f"in the bottom half: sell, target the overnight low; {st}"
            + (f", no trade within {p['deadPct']:g}% of the range around the mid" if p["deadPct"] else "")
            + (f", only when the target is at least {p['minRR']:g}× the risk" if p["minRR"] else "")
            + f", out at {p['exitAt']}"
            + ("" if p["hold"] == "off" else "; a candle closing beyond the target holds the trade to 15:55 with the stop at "
               + {"be": "the entry", "level": "the broken overnight extreme", "orig": "its original place"}[p["holdStop"]])
            + ("" if p["exit"] == "target" else N.ifvg._exit_txt(p)))


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    k = N.idx_at(F, f"{d}T09:25")
    if k is None or not on:
        return []
    hi, lo = max(b[2] for b in on), min(b[3] for b in on)
    rng = hi - lo
    if rng <= 0:
        return []
    o = F[k][4]
    mid = (hi + lo) / 2
    pos = (o - lo) / rng                                  # 0 = the overnight low, 1 = the high
    if pos <= 0 or pos >= 1 or abs(pos - 0.5) * 100 < p["deadPct"]:
        return []
    dd = 1 if o > mid else -1
    if not N.ok_side(dd, N.bias(E, F[k][0], o, p), p):
        return []
    tgt = hi if dd > 0 else lo
    stop = mid if p["stop"] == "mid" else (lo if dd > 0 else hi) if p["stop"] == "opp" else o - dd * p["stopPart"] * rng
    risk = (o - stop) * dd
    if risk <= 0 or (tgt - o) * dd <= 0 or (p["minRR"] and (tgt - o) * dd < p["minRR"] * risk):
        return []
    if p["hold"] == "off" or p["exit"] != "target":
        x, j = N.trade(F, k, dd, o, stop, tgt if p["exit"] == "target" else None, risk, p, E, f"{d}T{p['exitAt']}",
                       pos=pos, onr=rng / o * 100, sweepLvl=tgt, held=False)
        return [(x, (k, f"{d}T09:30", f"{d}T{p['exitAt']}"))]
    x = hold_trade(F, k, dd, o, stop, tgt, risk, p, d)
    x.update(pos=pos, onr=rng / o * 100, sweepLvl=tgt)
    return [(x, (k, f"{d}T09:30", f"{d}T15:55"))]


def hold_trade(F, k, d, e, stop, tgt, risk, p, day):
    """Enter at the close of candle k. Stop first in each candle; the target: a close beyond it -> hold to 15:55 (stop
    moved by holdStop), else take profit at it. Out at exitAt (before a break) or 15:55 (after)."""
    st, held, x, why, ot = stop, False, None, None, None
    for j in range(k + 1, len(F)):
        t, op, h, l, c = F[j][:5]
        if (d > 0 and l <= st) or (d < 0 and h >= st):
            x, ot = (min(op, st) if d > 0 else max(op, st)), t
            why = "stop" if not held else {"be": "break-even (after the break)", "level": "back inside (after the break)", "orig": "stop (after the break)"}[p["holdStop"]]
            break
        if not held and ((d > 0 and h >= tgt) or (d < 0 and l <= tgt)):
            if (c - tgt) * d > 0:                        # closed beyond: hold to the end of the day
                held = True
                st = {"be": e, "level": tgt, "orig": st}[p["holdStop"]]
                st = max(st, stop) if d > 0 else min(st, stop)
            else:
                x, ot, why = (max(op, tgt) if d > 0 else min(op, tgt)), t, "target"
                break
        flat = f"{day}T15:55" if held else f"{day}T{p['exitAt']}"
        if t[:16] >= flat:
            x, ot, why = c, t, "time (held to the close)" if held else "time"
            break
    if x is None:
        x, ot, why = F[-1][4], F[-1][0], "close"
    t0 = F[k][0]
    return {"in": N.K.end_of(t0, p["tf"]), "out": ot, "e": e, "x": x, "why": why, "side": "long" if d > 0 else "short", "stop": stop, "tgt": tgt,
            "stopDist": risk, "tgtDist": abs(tgt - e), "cf": t0, "part": None, "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk, "held": held}


def all_trades(sub, p, cells=None, prog=None):
    vr = {s: N.vol_rank(s) for s in p["tickers"]}
    tr, base = N.run(sub, p, engine, 211, prog, "the overnight range")
    for x in tr:
        x["vr"] = vr.get(x["s"], {}).get(x["d"], vr.get(x["s"], {}).get("next"))
    return tr, base


def decorate(x, p):
    N.decorate_common(x)
    v = x.get("pos")
    x["pos"] = "—" if v is None else "bottom quarter" if v < 0.25 else "lower middle" if v < 0.5 else "upper middle" if v < 0.75 else "top quarter"
    x["held"] = "yes: held to 15:55" if x.get("held") else "no"
    v = x.get("onr") or 0
    x["onr"] = "under 0.5%" if v < 0.5 else "0.5-1%" if v < 1 else "1-1.5%" if v < 1.5 else "1.5%+"
    v = x.get("vr")
    x["vr"] = "—" if v is None else "low (under 33)" if v < 33 else "middle (33-66)" if v < 67 else "high (67+)"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
