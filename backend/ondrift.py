"""Overnight drift: hold with the EMA bias through the night.

  At `entryAt` (18:00 = the session's open), when the 15 / 30 / 60-minute 20 EMAs point one way (bias; `none` = always
  `side`), enter at that candle's close in that direction; out at `exitAt` (that candle's close), or at the stop:
  `stopMult` x the average candle range of the last 20 candles (0 = no stop: R counts 10x that range as the risk).
  Optional target / managed exit. One trade a session. The control of every overnight idea: if plain holding does as
  well, the entries add nothing."""
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "entryAt": "18:00", "exitAt": "09:30", "stopMult": 8.0, "bias": "all3", "side": "both", "exit": "target", "tgtR": 0.0,
           "days": 500}
FACTORS = [("side", "Side"), ("wd", "Weekday"), ("al", "EMAs agreeing"), ("vr", "Volatility (percentile of the daily range)")]
ENTRY = ("18:00", "19:00", "20:00", "22:00", "00:00", "02:00", "03:00")
EXIT = ("00:00", "02:00", "04:00", "06:00", "08:30", "09:30", "11:00", "15:55")


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "ondrift", "tf": 5, "entryAt": p.get("entryAt") if p.get("entryAt") in ENTRY else "18:00",
           "exitAt": p.get("exitAt") if p.get("exitAt") in EXIT else "09:30"}
    N.K.num(p, out, (("stopMult", 0, 100, 8.0),))
    N.clean_common(p, out)
    if p.get("tgtR") in (None, ""):
        out["tgtR"] = 0.0
    return out


def describe(p):
    b = {"all3": "the 15 / 30 / 60-minute 20 EMAs", "h1": "the hourly 20 EMA", "none": "no bias"}[p["bias"]]
    st = f"stop {p['stopMult']:g}× the average 5-minute range" if p["stopMult"] else "no stop"
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])}: at {p['entryAt']} ET enter with {b} ({side}), out at {p['exitAt']} ET, {st}" + N.exit_txt(p)
            + " (the overnight control)")


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    t0, t1 = N.stamp(F, p["entryAt"]), N.stamp(F, p["exitAt"])
    if t1 <= t0:
        return []
    k = next((i for i, b in enumerate(F) if b[0][:16] >= t0), None)
    if k is None or F[k][0][:16] >= t1:
        return []
    c = F[k][4]
    b = N.bias(E, F[k][0], c, p)
    dirs = [b] if p["bias"] != "none" else ([1] if p["side"] == "long" else [-1] if p["side"] == "short" else [1])
    dirs = [x for x in dirs if x and N.ok_side(x, b, p)]
    if not dirs:
        return []
    dd = dirs[0]
    a = N.avg_range(F, k, prev=prev)
    if a <= 0:
        return []
    risk = (p["stopMult"] or 10) * a
    stop = c - dd * risk if p["stopMult"] else c - dd * N.FAR
    row = E.get(F[k][0]) or []
    al = sum(1 for em, up, dn in row if (c - em) * dd > 0 and (up if dd > 0 else dn))
    x, j = N.trade(F, k, dd, c, stop, None, risk, p, E, t1, al=al)
    return [(x, (0, t1, t1))]                        # random entries: anywhere in the session before the exit time


def all_trades(sub, p, cells=None, prog=None):
    vr = {s: N.vol_rank(s) for s in p["tickers"]}
    tr, base = N.run(sub, p, engine, 73, prog, "the overnight hold")
    for x in tr:
        x["vr"] = vr.get(x["s"], {}).get(x["d"], vr.get(x["s"], {}).get("next"))
    return tr, base


def decorate(x, p):
    N.decorate_common(x)
    x["al"] = f"{x.get('al', 0)} of 3"
    v = x.get("vr")
    x["vr"] = "—" if v is None else "low (under 33)" if v < 33 else "middle (33-66)" if v < 67 else "high (67+)"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
