"""Flat-bias mean reversion: when the EMAs have no bias, fade stretched moves back to the VWAP.

  The bias is flat (the 15 / 30 / 60-minute 20 EMAs don't agree, or the hourly one is flat), and the previous candle
  closed at least `distMult` x the average candle range (last 20) away from the session VWAP. This candle closes back
  toward the VWAP (a green candle under it / a red one over it): enter at its close. Stop past the extreme of the last
  3 candles (+ `bufTicks`). Target the VWAP (skipped under `minRR` x the risk), or the managed exit.
  `hours`: rth (09:35-`lastEntry`) or all (from 18:00). One position at a time, at most `maxDay` a session."""
import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "distMult": 2.0, "hours": "all", "lastEntry": "15:00", "maxDay": 3, "minRR": 1.0, "bias": "all3", "side": "both",
           "exit": "target", "bufTicks": 2, "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("dist", "Distance from the VWAP"), ("ses", "Session")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "flatrev", "tf": 1 if str(p.get("tf")) == "1" else 15 if str(p.get("tf")) == "15" else 5,
           "hours": p.get("hours") if p.get("hours") in ("rth", "all") else "all", "lastEntry": N.hhmm(p.get("lastEntry"), "15:00", "09:40", "15:30")}
    N.K.num(p, out, (("distMult", 0.5, 20, 2.0), ("maxDay", 1, 20, 3), ("minRR", 0, 10, 1.0)), ("maxDay",))
    N.clean_common(p, out)
    if out["bias"] == "none":
        out["bias"] = "all3"                           # 'flat' needs an EMA rule to be flat against
    return out


def describe(p):
    b = "the 15 / 30 / 60-minute EMAs disagree" if p["bias"] == "all3" else "the hourly EMA has no clear bias"
    tg = (" target the VWAP" + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "")) if p["exit"] == "target" else N.ifvg._exit_txt(p)
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute ({'overnight + NYSE' if p['hours'] == 'all' else 'NYSE session'}): when {b}, a close "
            f"{p['distMult']:g}× the average range from the VWAP, then a candle back toward it: fade at that close, stop past the last 3 candles + "
            f"{p['bufTicks']} tick(s),{tg}, at most {p['maxDay']} a session, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def engine(sym, d, on, R, F, VF, E, prev, p):
    out, k, buf = [], 21, p["bufTicks"] * N.K.TICK
    rth0, flat = f"{d}T09:30", f"{d}T15:55"
    lo_i = 0 if p["hours"] == "all" else next((i for i, b in enumerate(F) if b[0] >= f"{d}T09:35"), len(F))
    while k < len(F) and len(out) < p["maxDay"] and k < len(VF):
        t, o, h, l, c = F[k]
        if t[:10] == d and N.K.end_of(t, p["tf"])[11:16] > p["lastEntry"]:
            break
        if k < lo_i or "16:00" <= t[11:16] < "18:00" or N.bias(E, t, c, p) != 0:
            k += 1
            continue
        a = N.avg_range(F, k)
        pc, pv = F[k - 1][4], VF[k - 1]
        if a <= 0 or abs(pc - pv) < p["distMult"] * a:
            k += 1
            continue
        dd = 1 if pc < pv else -1                      # back up toward the VWAP (long) / down (short)
        if (c - o) * dd <= 0 or (VF[k] - c) * dd <= 0 or not N.ok_side(dd, dd, {**p, "bias": "none"}):
            k += 1
            continue
        seg = F[k - 2:k + 1]
        stop = (min(b[3] for b in seg) - buf) if dd > 0 else (max(b[2] for b in seg) + buf)
        risk = (c - stop) * dd
        tgt = VF[k] if p["exit"] == "target" else None
        if risk <= 0 or (tgt is not None and p["minRR"] and abs(tgt - c) < p["minRR"] * risk):
            k += 1
            continue
        x, j = N.trade(F, k, dd, c, stop, tgt, risk, p, E, flat, dist=abs(pc - pv) / a, ses=t >= rth0)
        out.append((x, (lo_i, f"{d}T{p['lastEntry']}", flat)))
        k = j + 1
    return out


def all_trades(sub, p, cells=None, prog=None):
    return N.run(sub, p, engine, 89, prog, "candles + the session VWAP")


def decorate(x, p):
    N.decorate_common(x)
    v = x.get("dist") or 0
    x["dist"] = "2-3× avg range" if v < 3 else "3-5×" if v < 5 else "5×+"
    x["ses"] = "NYSE" if x.get("ses") else "overnight"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
