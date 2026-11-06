"""Overnight -> day reversal: fade a big overnight move from the open.

  The gap: today's 09:30 open vs yesterday's 16:00 close, at least `minPct`% (at most `maxPct`%, 0 = no limit).
  Fade it at the open (the close of the 09:25 candle): target `fill` x the gap back toward yesterday's close (1 = the
  close); stop `stopGap` x the gap past the open; out at `exitAt`. With the EMA bias if set (none by default)."""
import nightkit as N

SETUP = "onrev"
DEFAULT = {"tickers": ["NQ"], "tf": 5, "minPct": 0.6, "maxPct": 0.0, "fill": 1.0, "stopGap": 1.0, "exitAt": "11:30", "bias": "none", "side": "both",
           "exit": "target", "days": 500}
FACTORS = [("side", "Side"), ("wd", "Weekday"), ("gp", "Gap size"), ("vr", "Volatility (percentile of the daily range)")]
EXITS = ("10:00", "10:30", "11:30", "13:00", "15:55")


def clean(p, setup=None, default=None):
    p = {**(default or DEFAULT), **(p or {})}
    out = {"setup": setup or SETUP, "tf": 5, "exitAt": p.get("exitAt") if p.get("exitAt") in EXITS else (default or DEFAULT)["exitAt"]}
    N.K.num(p, out, (("minPct", 0, 10, 0.6), ("maxPct", 0, 20, 0.0), ("fill", 0.1, 2, 1.0), ("stopGap", 0.1, 10, 1.0)))
    N.clean_common(p, out, bias="none")
    return out


def describe(p):
    rng = f"at least {p['minPct']:g}%" + (f" and under {p['maxPct']:g}%" if p["maxPct"] else "")
    tg = "yesterday's close" if p["fill"] == 1 else f"{p['fill'] * 100:g}% of the way back to yesterday's close"
    return (f"{', '.join(p['tickers'])}: a gap of {rng} between yesterday's 16:00 close and today's 09:30 open: fade it at the open, target {tg}, "
            f"stop {p['stopGap']:g}× the gap past the open, out at {p['exitAt']}" + ("" if p["exit"] == "target" else N.ifvg._exit_txt(p)))


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    k = N.idx_at(F, f"{d}T09:25")
    pc = N.prev_close(prev)
    if k is None or not pc:
        return []
    o = F[k][4]
    g = (o - pc) / pc * 100
    if not g or abs(g) < p["minPct"] or (p["maxPct"] and abs(g) >= p["maxPct"]):
        return []
    dd = -1 if g > 0 else 1
    if not N.ok_side(dd, N.bias(E, F[k][0], o, p), p):
        return []
    gap = abs(o - pc)
    tgt = o + dd * p["fill"] * gap
    risk = p["stopGap"] * gap
    x, j = N.trade(F, k, dd, o, o - dd * risk, tgt if p["exit"] == "target" else None, risk, p, E, f"{d}T{p['exitAt']}", gp=g, sweepLvl=pc)
    return [(x, (k, f"{d}T09:30", f"{d}T{p['exitAt']}"))]


def all_trades(sub, p, cells=None, prog=None):
    vr = {s: N.vol_rank(s) for s in p["tickers"]}
    tr, base = N.run(sub, p, engine, 109, prog, "the opening gap")
    for x in tr:
        x["vr"] = vr.get(x["s"], {}).get(x["d"], vr.get(x["s"], {}).get("next"))
    return tr, base


def decorate(x, p):
    N.decorate_common(x)
    v = abs(x.get("gp") or 0)
    x["gp"] = "under 0.25%" if v < 0.25 else "0.25-0.5%" if v < 0.5 else "0.5-1%" if v < 1 else "1-2%" if v < 2 else "2%+"
    v = x.get("vr")
    x["vr"] = "—" if v is None else "low (under 33)" if v < 33 else "middle (33-66)" if v < 67 else "high (67+)"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
