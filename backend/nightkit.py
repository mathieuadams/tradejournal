"""Shared pieces of the overnight / intermarket strategies (overnight drift, London breakout, SMT divergence,
flat-bias mean reversion, post-close continuation, Sunday gap): the full-session candles with the FVG strategy's EMA
track, the session VWAP on every candle, the exit management (ifvg.manage), the random-entry baseline, the daily
volatility rank."""
import random

import ifvg
import sessionkit as K

MANAGE = {"exit": "target", "tgtR": 2.0, "beR": 0.0, "partR": 1.0, "partPct": 0, "trail": "swing", "trailN": 3, "trailR": 0.5}
FAR = 1e9


def clean_common(p, out, bias="all3", exit="target"):
    out["bias"] = p.get("bias") if p.get("bias") in ("all3", "h1", "none") else bias
    out["side"] = p.get("side") if p.get("side") in ("long", "short", "both") else "both"
    out["exit"] = p.get("exit") if p.get("exit") in ("target", "trail") else exit
    out["trail"] = p.get("trail") if p.get("trail") in ("swing", "r", "ema15", "none") else "swing"
    K.num(p, out, (("tgtR", 0, 20, 2.0), ("beR", 0, 5, 0.0), ("partR", 0, 10, 1.0), ("partPct", 0, 100, 0), ("trailN", 1, 50, 3), ("trailR", 0.1, 10, 0.5),
                   ("bufTicks", 0, 80, 2), ("days", 20, 3000, 500)), ("trailN", "bufTicks", "days"))
    out["tickers"] = K.tickers_of(p)
    return out


def hhmm(v, dflt, lo="00:00", hi="23:59"):
    v = str(v or dflt)
    return v if len(v) == 5 and v[2] == ":" and lo <= v <= hi else dflt


def bias(E, t, c, p):
    """+1 / -1 / 0 from the EMAs; 'none' = no bias (0)."""
    if p["bias"] == "none" or not E.get(t):
        return 0
    return ifvg.bias_at(E[t], c, {"bias": p["bias"]})


def ok_side(d, b, p):
    """The side allowed, and (unless bias = none) with the EMA bias."""
    if (p["side"] == "long" and d < 0) or (p["side"] == "short" and d > 0):
        return False
    return p["bias"] == "none" or b == d


def avg_range(F, k, n=20, prev=None):
    """The average range of the n candles before F[k] (reaching into the previous session `prev` when needed)."""
    w = F[max(0, k - n):k]
    if len(w) < n and prev:
        w = list(prev[-(n - len(w)):]) + list(w)
    return sum(b[2] - b[3] for b in w) / len(w) if w else 0


def stamp(F, hm):
    """The timestamp of hh:mm in the session of the candles F (18:00+ = the evening before)."""
    if not F:
        return None
    eve, day = F[0][0][:10], F[-1][0][:10]
    return f"{eve if hm >= '18:00' else day}T{hm}"


def exit_txt(p):
    if p["exit"] != "trail":
        return f", target {p['tgtR']:g}R" if p.get("tgtR") else ""
    return ifvg._exit_txt(p)


def trade(F, k, d, e, stop, tgt, risk, p, E, flat, **extra):
    """Enter at the close of candle k; the exit by ifvg.manage. tgt None = no target (time / stop / trail)."""
    q = {**MANAGE, **p}
    if q["exit"] == "target" and not q.get("tgtR") and tgt is None:
        tgt = e + d * FAR
    tgt = tgt if tgt is not None else (e + d * q["tgtR"] * risk if q.get("tgtR") else e + d * FAR)
    x, ot, why, j, part = ifvg.manage(F, k + 1, d, e, stop, tgt, risk, q, E, flat)
    t = F[k][0]
    out = {"in": K.end_of(t, p["tf"]), "out": ot, "e": e, "x": x, "why": why, "side": "long" if d > 0 else "short",
           "stop": stop if abs(stop) < FAR / 10 else None, "tgt": tgt if abs(tgt) < FAR / 10 else None, "stopDist": risk,
           "tgtDist": abs(tgt - e) if abs(tgt) < FAR / 10 else None, "cf": t, "part": part, "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk}
    out.update(extra)
    return out, j


def baseline(F, x, rnd, p, E, flat, lo, hi_t, n=3):
    """The same side, stop distance and exit from random candles F[lo:] before hi_t: % gross."""
    d = 1 if x["side"] == "long" else -1
    idx = [k for k in range(lo, len(F) - 2) if F[k][0][:16] < hi_t and not ("16:00" <= F[k][0][11:16] < "18:00")]
    out = []
    q = {**MANAGE, **p}
    for _ in range(n):
        if not idx:
            break
        k = rnd.choice(idx)
        e, risk = F[k][4], x["stopDist"]
        stop = e - d * risk if x.get("stop") is not None else e - d * FAR
        tgt = e + d * x["tgtDist"] if x.get("tgtDist") else e + d * FAR
        px, _, _, _, _ = ifvg.manage(F, k + 1, d, e, stop, tgt, risk, q, E, flat)
        out.append(d * (px / e - 1) * 100)
    return out


def run(sub, p, engine, seed, prog=None, label="candles"):
    """all_trades for an engine(sym, day, on, R, F, VF, E, prev, p) -> [(trade, (lo, hi_t, flat))]."""
    trades, base = [], []
    rnd = random.Random(seed)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {label}")
        rows, Fall, fi, E = ifvg._series(sym, p)
        prev = None
        for d, on, R, F, V in rows:
            if len(F) < 5:
                prev = F or prev
                continue
            VF = K.full_vwap(sym, p["tf"], d)
            for x, (lo, hi_t, flat) in engine(sym, d, on, R, F, VF, E, prev, p):
                x.update(s=sym, d=d, cell=None)
                trades.append(x)
                base.append((d, x["side"], baseline(F, x, rnd, p, E, flat, lo, hi_t)))
            prev = F
    return trades, base


def tod(t):
    hm = t[11:16]
    if "09:30" <= hm < "16:00":
        return K.tod(t)
    return ("18:00-21:00" if hm >= "18:00" and hm < "21:00" else "21:00-00:00" if hm >= "21:00" else "00:00-03:00" if hm < "03:00"
            else "03:00-06:00" if hm < "06:00" else "06:00-09:30" if hm < "09:30" else "16:00-17:00")


def decorate_common(x):
    x["tod"] = tod(x["in"])
    x["wd"] = K.weekday(x["d"])


def overlay(sub, p, sym, start, end):
    """The FVG strategy's drawing (the 3 EMAs, the bias band, the Asian range), without its entries."""
    keys = ("tf", "bias", "days", "tickers")
    q = ifvg.clean({k: p[k] for k in keys if k in p})
    if p.get("bias") == "none":
        q["bias"] = "all3"
    ov = ifvg.overlay(sub, q, sym, start, end)
    ov["notes"] = []
    return ov


_VR = {}


def vol_rank(sym):
    """{day: percentile (0-100) of the 14-day average daily range (known before the day) among the last 250 days}."""
    import futdata
    last = futdata.last_bar_time(sym)
    if _VR.get(sym, (None,))[0] == last:
        return _VR[sym][1]
    D = futdata.daily(sym)
    trs, atr, out = [], [], {}
    for i, x in enumerate(D):
        pc = D[i - 1][4] if i else x[4]
        trs.append(max(x[2], pc) - min(x[3], pc))
        atr.append(sum(trs[-14:]) / len(trs[-14:]))
    for i, x in enumerate(D):
        if i < 20:
            continue
        a, hist = atr[i - 1], atr[max(0, i - 251):i - 1]
        out[x[0]] = round(sum(1 for v in hist if v <= a) / len(hist) * 100) if hist else None
    if D and len(D) > 20:                                # the session in progress: from the last full day
        hist = atr[max(0, len(D) - 251):len(D) - 1]
        out["next"] = round(sum(1 for v in hist if v <= atr[-1]) / len(hist) * 100) if hist else None
    _VR[sym] = (last, out)
    return out


_ATR = {}


def atr_map(sym, n=14):
    """{day: the average regular-hours daily range of the n days before it} (+ "next": for the session in progress)."""
    import futdata
    last = futdata.last_bar_time(sym)
    if _ATR.get((sym, n), (None,))[0] == last:
        return _ATR[(sym, n)][1]
    D = futdata.daily(sym)
    trs, out = [], {}
    for i, x in enumerate(D):
        if len(trs) >= n:
            out[x[0]] = sum(trs[-n:]) / n
        pc = D[i - 1][4] if i else x[4]
        trs.append(max(x[2], pc) - min(x[3], pc))
    if len(trs) >= n:
        out["next"] = sum(trs[-n:]) / n
    _ATR[(sym, n)] = (last, out)
    return out


def idx_at(F, ts):
    """The index of the candle starting at ts (YYYY-MM-DDTHH:MM), else None."""
    return next((i for i, b in enumerate(F) if b[0][:16] == ts), None)


def prev_close(prev):
    """The previous session's 16:00 close (its 15:55 5-minute candle / last regular-hours candle)."""
    if not prev:
        return None
    r = [b for b in prev if "09:30" <= b[0][11:16] < "16:00" and b[0][:10] == prev[-1][0][:10]]
    return r[-1][4] if r else None
