"""The Sunday (or daily) reopen gap: the 18:00 open vs the previous session's last price.

  `which`: monday = the Sunday 18:00 open vs Friday's close; all = every 18:00 reopen after the 17:00-18:00 halt.
  A gap under `smallPct`% is faded (target: the previous close); one of `bigPct`% or more is followed (`mode` both),
  only faded / only followed (`mode`). Entry at the close of the first candle (18:00-18:05); stop `stopMult` x the
  average candle range of the previous session's last 20 candles; out at the target, the stop or `exitAt`."""
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "which": "monday", "smallPct": 0.25, "bigPct": 0.5, "mode": "both", "exitAt": "02:00", "stopMult": 4.0, "tgtR": 2.0,
           "bias": "none", "side": "both", "exit": "target", "days": 500}
FACTORS = [("side", "Side"), ("kind", "Gap"), ("gp", "Gap size")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "sungap", "tf": 5, "which": p.get("which") if p.get("which") in ("monday", "all") else "monday",
           "mode": p.get("mode") if p.get("mode") in ("both", "fade", "follow") else "both",
           "exitAt": p.get("exitAt") if p.get("exitAt") in ("20:00", "22:00", "00:00", "02:00", "06:00", "09:30") else "02:00"}
    N.K.num(p, out, (("smallPct", 0, 5, 0.25), ("bigPct", 0, 10, 0.5), ("stopMult", 0.5, 50, 4.0)))
    N.clean_common(p, out, bias="none")
    return out


def describe(p):
    w = "the Sunday 18:00 open vs Friday's close" if p["which"] == "monday" else "every 18:00 reopen vs the last price before the halt"
    m = {"both": f"fade a gap under {p['smallPct']:g}% (target the previous close), follow one of {p['bigPct']:g}%+",
         "fade": f"fade a gap under {p['smallPct']:g}% (target the previous close)", "follow": f"follow a gap of {p['bigPct']:g}%+"}[p["mode"]]
    return (f"{', '.join(p['tickers'])}: {w}: {m}, at the first candle's close, stop {p['stopMult']:g}× the average range"
            + (f", followed gaps target {p['tgtR']:g}R" if p["mode"] != "fade" and p["exit"] == "target" and p["tgtR"] else "") + f", out at {p['exitAt']}")


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    import datetime as dt
    if not prev or len(F) < 3 or F[0][0][11:16] != "18:00":
        return []
    if p["which"] == "monday" and dt.date.fromisoformat(d).weekday() != 0:
        return []
    pc, o0, c = prev[-1][4], F[0][1], F[0][4]
    g = (o0 - pc) / pc * 100
    kind = "fade" if abs(g) < p["smallPct"] else "follow" if abs(g) >= p["bigPct"] else None
    if not kind or not g or (p["mode"] != "both" and p["mode"] != kind):
        return []
    dd = (-1 if g > 0 else 1) if kind == "fade" else (1 if g > 0 else -1)
    if not N.ok_side(dd, N.bias(E, F[0][0], c, p), p):
        return []
    a = N.avg_range(prev, len(prev))
    if a <= 0:
        return []
    risk = p["stopMult"] * a
    t1 = N.stamp(F, p["exitAt"])
    tgt = pc if kind == "fade" else None
    if tgt is not None and (tgt - c) * dd <= 0:
        return []                                      # already filled in the first candle
    x, j = N.trade(F, 0, dd, c, c - dd * risk, tgt, risk, p, E, t1, kind=kind, gp=g, sweepLvl=pc)
    return [(x, (0, t1, t1))]


def all_trades(sub, p, cells=None, prog=None):
    return N.run(sub, p, engine, 101, prog, "the reopen gap")


def decorate(x, p):
    N.decorate_common(x)
    x["kind"] = "faded (small)" if x.get("kind") == "fade" else "followed (big)"
    v = abs(x.get("gp") or 0)
    x["gp"] = "under 0.1%" if v < 0.1 else "0.1-0.25%" if v < 0.25 else "0.25-0.5%" if v < 0.5 else "0.5-1%" if v < 1 else "1%+"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
