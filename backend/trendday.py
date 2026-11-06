"""Trend-day hold: a wide first hour closing at its extreme, held to the close.

  The first hour (09:30-10:30): its range at least `rangeMult` x the 14-day average daily range, and the 10:25 candle
  closing in the top (bottom) `edgePct`% of that range: buy (sell) at 10:30 (that close). Stop (`stop`): opp = the
  other side of the first hour, mid = its middle (+ `bufTicks`). Hold to 15:55 (or the managed exit / a `tgtR` target).
  With the EMA bias if set."""
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "rangeMult": 0.6, "edgePct": 20, "stop": "mid", "bias": "none", "side": "both", "exit": "target", "tgtR": 0.0,
           "bufTicks": 2, "days": 500}
FACTORS = [("side", "Side"), ("wd", "Weekday"), ("hr", "First hour (× the daily average range)"), ("gap", "Opened outside yesterday's range")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "trendday", "tf": 5, "stop": p.get("stop") if p.get("stop") in ("opp", "mid") else "mid"}
    N.K.num(p, out, (("rangeMult", 0.1, 3, 0.6), ("edgePct", 1, 50, 20)))
    N.clean_common(p, out, bias="none")
    if p.get("tgtR") in (None, ""):
        out["tgtR"] = 0.0
    return out


def describe(p):
    return (f"{', '.join(p['tickers'])}: a first hour of {p['rangeMult']:g}×+ the daily average range closing (10:30) in its top / bottom {p['edgePct']:g}%: go with it at "
            f"10:30, stop at {'its middle' if p['stop'] == 'mid' else 'its other side'} + {p['bufTicks']} tick(s), hold to 15:55" + N.exit_txt(p))


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    i0, k = N.idx_at(F, f"{d}T09:30"), N.idx_at(F, f"{d}T10:25")
    atr = N.atr_map(sym)
    A = atr.get(d, atr.get("next"))
    if i0 is None or k is None or not A:
        return []
    hr = F[i0:k + 1]
    hi, lo = max(b[2] for b in hr), min(b[3] for b in hr)
    rg, c = hi - lo, F[k][4]
    if rg < p["rangeMult"] * A:
        return []
    pos = (c - lo) / rg * 100
    dd = 1 if pos >= 100 - p["edgePct"] else -1 if pos <= p["edgePct"] else 0
    if not dd or not N.ok_side(dd, N.bias(E, F[k][0], c, p), p):
        return []
    buf = p["bufTicks"] * N.K.TICK
    stop = ((hi + lo) / 2 if p["stop"] == "mid" else (lo if dd > 0 else hi)) - dd * buf
    risk = (c - stop) * dd
    if risk <= 0:
        return []
    pr = [b for b in (prev or []) if "09:30" <= b[0][11:16] < "16:00" and b[0][:10] == (prev[-1][0][:10] if prev else "")]
    gap = bool(pr) and (F[i0][1] > max(b[2] for b in pr) or F[i0][1] < min(b[3] for b in pr))
    x, j = N.trade(F, k, dd, c, stop, None, risk, p, E, f"{d}T15:55", hr=rg / A, gap=gap, rhi=hi, rlo=lo)
    return [(x, (k, f"{d}T15:00", f"{d}T15:55"))]


def all_trades(sub, p, cells=None, prog=None):
    return N.run(sub, p, engine, 151, prog, "the first hour")


def decorate(x, p):
    N.decorate_common(x)
    v = x.get("hr") or 0
    x["hr"] = "under 0.8×" if v < 0.8 else "0.8-1.2×" if v < 1.2 else "1.2×+"
    x["gap"] = "yes" if x.get("gap") else "no"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
