"""Post-close continuation: the last hour's move into the 16:00 close carries on (or reverses) until 17:00.

  From `fromAt` (15:00) to the 16:00 close, price moved at least `minMovePct`% of the price: enter at the 16:00 close
  (the close of the 15:55 candle) in that direction (`mode` follow) or against it (fade), with the EMA bias when it's
  set. Out at `exitAt` (16:55 = before the 17:00 halt), or at the stop: `stopMult` x the average candle range (last 20)."""
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "fromAt": "15:00", "exitAt": "16:55", "minMovePct": 0.3, "mode": "follow", "stopMult": 3.0, "bias": "none",
           "side": "both", "exit": "target", "tgtR": 0.0, "days": 500}
FACTORS = [("side", "Side"), ("wd", "Weekday"), ("mv", "Last hour's move"), ("vr", "Volatility (percentile of the daily range)")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "postclose", "tf": 5, "fromAt": p.get("fromAt") if p.get("fromAt") in ("14:00", "15:00", "15:30") else "15:00",
           "exitAt": p.get("exitAt") if p.get("exitAt") in ("16:15", "16:30", "16:55") else "16:55", "mode": p.get("mode") if p.get("mode") in ("follow", "fade") else "follow"}
    N.K.num(p, out, (("minMovePct", 0, 5, 0.3), ("stopMult", 0.5, 50, 3.0)))
    N.clean_common(p, out, bias="none")
    if p.get("tgtR") in (None, ""):
        out["tgtR"] = 0.0
    return out


def describe(p):
    b = {"all3": ", with the 15 / 30 / 60-minute EMA bias", "h1": ", with the hourly EMA bias", "none": ""}[p["bias"]]
    return (f"{', '.join(p['tickers'])}: a {p['minMovePct']:g}%+ move from {p['fromAt']} to the 16:00 close: {'follow' if p['mode'] == 'follow' else 'fade'} it at the "
            f"close{b}, stop {p['stopMult']:g}× the average 5-minute range, out at {p['exitAt']}" + N.exit_txt(p))


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    t0, tc, t1 = f"{d}T{p['fromAt']}", f"{d}T15:55", f"{d}T{p['exitAt']}"
    i0 = next((i for i, b in enumerate(F) if b[0][:16] >= t0), None)
    k = next((i for i, b in enumerate(F) if b[0][:16] >= tc), None)
    if i0 is None or k is None or F[k][0][:16] != tc or k + 1 >= len(F):
        return []
    c, mv = F[k][4], F[k][4] - F[i0][1]
    if abs(mv) < c * p["minMovePct"] / 100 or not mv:
        return []
    dd = (1 if mv > 0 else -1) * (1 if p["mode"] == "follow" else -1)
    if not N.ok_side(dd, N.bias(E, F[k][0], c, p), p):
        return []
    a = N.avg_range(F, k)
    if a <= 0:
        return []
    risk = p["stopMult"] * a
    x, j = N.trade(F, k, dd, c, c - dd * risk, None, risk, p, E, t1, mv=mv / c * 100)
    return [(x, (i0, f"{d}T16:00", t1))]               # random entries: the same last hour


def all_trades(sub, p, cells=None, prog=None):
    vr = {s: N.vol_rank(s) for s in p["tickers"]}
    tr, base = N.run(sub, p, engine, 97, prog, "the last hour")
    for x in tr:
        x["vr"] = vr.get(x["s"], {}).get(x["d"], vr.get(x["s"], {}).get("next"))
    return tr, base


def decorate(x, p):
    N.decorate_common(x)
    v = abs(x.get("mv") or 0)
    x["mv"] = "under 0.5%" if v < 0.5 else "0.5-1%" if v < 1 else "1%+"
    v = x.get("vr")
    x["vr"] = "—" if v is None else "low (under 33)" if v < 33 else "middle (33-66)" if v < 67 else "high (67+)"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
