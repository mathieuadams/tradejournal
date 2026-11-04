"""Shared pieces of the session strategies (overnight sweep, initial balance, opening drive): the session's candles
split in overnight / regular hours, the exit walk, the random-entry baseline, the time-of-day buckets."""
import datetime as dt
import random

TICK = 0.25
RTH0, RTH1 = "09:30", "16:00"


_CACHE = {}


def sessions(sym, p):
    """{day: (overnight 1-minute candles, regular-hours tf candles, full-session tf candles, session VWAP at each
    regular-hours candle's close)} for the strategy's sessions. Cached per process (the optimizer runs the same
    sessions for every setting); read-only for the callers."""
    import futdata
    key = (sym, p.get("_start"), None if p.get("_start") else p["days"], p["tf"], futdata.last_bar_time(sym))
    if key not in _CACHE:
        if len(_CACHE) >= 4:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = _sessions(sym, p)
    return _CACHE[key]


def _sessions(sym, p):
    import futdata
    start = p.get("_start")
    by = futdata.sessions(sym, None if start else p["days"], rth=False, start=start)
    out = {}
    for d in sorted(by):
        bars = by[d]
        on = [b for b in bars if not (b[0][:10] == d and b[0][11:16] >= RTH0)]
        rth1 = [b for b in bars if b[0][:10] == d and RTH0 <= b[0][11:16] < RTH1]
        pv = vv = 0.0
        vw = {}
        for t, o, h, l, c, v in bars:
            pv += (h + l + c) / 3 * v
            vv += v
            vw[t] = pv / vv if vv else c
        R = [tuple(b[:5]) for b in futdata.agg(rth1, p["tf"])]
        V = []
        for b in R:
            m = int(b[0][11:13]) * 60 + int(b[0][14:16]) + p["tf"] - 1
            V.append(vw.get(f"{b[0][:11]}{m // 60:02d}:{m % 60:02d}") or vw.get(b[0]) or b[4])
        F = [tuple(b[:5]) for b in futdata.agg(bars, p["tf"])]
        VF = []
        for b in F:                                     # the session VWAP at each full-session candle's last minute
            m = int(b[0][11:13]) * 60 + int(b[0][14:16]) + p["tf"] - 1
            VF.append(vw.get(f"{b[0][:11]}{m // 60 % 24:02d}:{m % 60:02d}") or vw.get(b[0]) or b[4])
        _VF[(sym, p["tf"], d)] = VF
        out[d] = (on, R, F, V)
    return out


_VF = {}


def full_vwap(sym, tf, day):
    """The session VWAP (from 18:00) at each full-session candle of the day (after sessions() has run for it)."""
    return _VF.get((sym, tf, day)) or []


def end_of(t, tf):
    m = int(t[11:13]) * 60 + int(t[14:16]) + tf - 1
    return f"{t[:11]}{m // 60:02d}:{m % 60:02d}"


def walk(B, j0, d, e, stop, tgt, flat="15:55", stop_ok_first=True):
    """Exit of a position entered before candle j0: stop / target / flat at `flat` (that candle's close).
    Gaps through a level fill at the open. Returns (exit price, exit time, why, index)."""
    for j in range(j0, len(B)):
        t, o, h, l, c = B[j]
        if (d > 0 and l <= stop) or (d < 0 and h >= stop):
            return ((min(o, stop) if d > 0 else max(o, stop)) if j > j0 or stop_ok_first else stop), t, "stop", j
        if tgt is not None and ((d > 0 and h >= tgt) or (d < 0 and l <= tgt)):
            return ((max(o, tgt) if d > 0 else min(o, tgt)) if j > j0 or stop_ok_first else tgt), t, "target", j
        if t[11:16] >= flat:
            return c, t, "time", j
    return B[-1][4], B[-1][0], "close", len(B) - 1


def baseline(B, tr, rnd, n=3, flat="15:55"):
    """The same bracket (side, stop and target distances) from random regular-hours candles: % gross."""
    out, d = [], 1 if tr["side"] == "long" else -1
    if len(B) < 6:
        return out
    for _ in range(n):
        k = rnd.randrange(1, len(B) - 3)
        if B[k][0][11:16] >= "15:30":
            continue
        e = B[k][4]
        stop = e - d * tr["stopDist"]
        tgt = e + d * tr["tgtDist"] if tr.get("tgtDist") else None
        x, _, _, _ = walk(B, k + 1, d, e, stop, tgt, flat)
        out.append(d * (x / e - 1) * 100)
    return out


def tod(t):
    hm = t[11:16]
    return "09:30-10:00" if hm < "10:00" else "10:00-10:30" if hm < "10:30" else "10:30-11:30" if hm < "11:30" else "11:30-13:30" if hm < "13:30" else "13:30-16:00"


def weekday(d):
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.date.fromisoformat(d).weekday()]


def ema(xs, n):
    m, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else m * x + (1 - m) * e
        out.append(e)
    return out


def tickers_of(p, default=("NQ",)):
    tk = p.get("tickers") or list(default)
    tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
    return [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:2] or list(default)


def num(p, out, specs, ints=()):
    for k, lo, hi, dflt in specs:
        try:
            out[k] = max(lo, min(hi, float(p.get(k))))
        except (TypeError, ValueError):
            out[k] = dflt
    for k in ints:
        out[k] = int(out[k])
    return out


def run_all(sub, p, engine, label, prog=None, seed=31):
    """all_trades for a per-session engine(day, on, R, F, V, p) -> [trades]."""
    trades, base = [], []
    rnd = random.Random(seed)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {label}")
        for d, (on, R, F, V) in sessions(sym, p).items():
            if len(R) < 10:
                continue
            for x in engine(d, on, R, F, V, p):
                x.update(s=sym, d=d, cell=None)
                trades.append(x)
                base.append((d, x["side"], baseline(R, x, rnd)))
    return trades, base


# ---------------------------------------------------------------- daily context (prior-day levels, ATR, trend)

def day_bar(d, R):
    return {"d": d, "o": R[0][1], "h": max(b[2] for b in R), "l": min(b[3] for b in R), "c": R[-1][4]}


def atr(H, n=14):
    """Average regular-hours true range of the last n days of H (oldest first)."""
    trs = [max(y["h"], x["c"]) - min(y["l"], x["c"]) for x, y in zip(H[-n - 1:-1], H[-n:])]
    return sum(trs) / len(trs) if trs else None


def sma_close(H, n=20):
    return sum(x["c"] for x in H[-n:]) / n if len(H) >= n else None


def daily_sessions(sym, p, start=None, need=25):
    """[(day, on, R, F, V, H)]: the sessions() tuple plus H, the regular-hours daily candles of the sessions before
    (oldest first, at most `need`), for the strategy's sessions (or those from `start`)."""
    import datetime as _dt
    if start or p.get("_start"):
        s0 = start or p["_start"]
        q = {**p, "_start": (_dt.date.fromisoformat(s0) - _dt.timedelta(days=int(need * 1.6) + 7)).isoformat()}
    else:
        s0, q = None, {**p, "days": p["days"] + need}
    by = sessions(sym, q)
    days = sorted(by)
    keep = set(d for d in days if d >= s0) if s0 else set(days[-p["days"]:])
    out, H = [], []
    for d in days:
        on, R, F, V = by[d]
        if not R:
            continue
        if d in keep:
            out.append((d, on, R, F, V, list(H[-need:])))
        H.append(day_bar(d, R))
    return out


def run_daily(sub, p, engine, label, need=25, seed=41):
    """all_trades for an engine(day, on, R, F, V, H, p) that also reads the days before."""
    trades, base = [], []
    rnd = random.Random(seed)
    for sym in p["tickers"]:
        for d, on, R, F, V, H in daily_sessions(sym, p, need=need):
            if len(R) < 10:
                continue
            for x in engine(d, on, R, F, V, H, p):
                x.update(s=sym, d=d, cell=None)
                trades.append(x)
                base.append((d, x["side"], baseline(R, x, rnd)))
    return trades, base


def level_lines(sym, p, start, end, names):
    """Overlay lines of the prior-day levels: names = [(key in the daily candle, label, color)]."""
    lines = {k: {"name": lab, "color": col, "pts": []} for k, lab, col in names}
    for d, on, R, F, V, H in daily_sessions(sym, p, start=start, need=2):
        if d > end or not H:
            continue
        y = H[-1]
        for k in lines:
            lines[k]["pts"] += [[R[0][0], y[k]], [R[-1][0], y[k]]]
    return list(lines.values())
