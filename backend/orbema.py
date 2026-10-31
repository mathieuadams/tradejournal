"""Opening range breakout, then a retest of the 20 EMA, on NQ / ES regular-hours candles.

Short (long = the mirror):
  1. The opening range: high / low of the first `rangeMin` minutes (15 or 30) from 09:30.
  2. The breakout: a candle closes below the range low.
  3. The retest from the underside: a later candle closes ABOVE the `ema`-period EMA, then a candle closes back
     BELOW it (the confirmation): sell at its close (`entryAt` close) or the next candle's open (`entryAt` open).
  4. Stop: above the confirmation candle's high (+ `bufTicks` ticks). Target: the low of the day so far.
     Skipped when the target is less than `minRR` x the risk away (0 = never skipped).
  5. One trade per day, no entry after `lastEntry`, flat at 15:55. Same candle through the stop and the target:
     the stop counts.
The EMA runs on the timeframe's candles of the whole Globex session (`emaOn` full: the overnight included, as a futures
chart shows it) or on the regular-hours candles only (`emaOn` rth)."""
import datetime as dt
import random

DEFAULT = {"tickers": ["NQ"], "tf": 5, "rangeMin": 15, "ema": 20, "bufTicks": 1, "minRR": 1.0, "lastEntry": "12:00", "entryAt": "close", "emaOn": "full",
           "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("wait", "Candles from the breakout"), ("rr", "Target / risk")]
TICK = 0.25


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "orbema", "tf": 1 if str(p.get("tf")) == "1" else 5, "rangeMin": 30 if str(p.get("rangeMin")) == "30" else 15,
           "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both",
           "entryAt": "open" if p.get("entryAt") == "open" else "close", "emaOn": "rth" if p.get("emaOn") == "rth" else "full"}
    for k, lo, hi in (("ema", 3, 200), ("bufTicks", 0, 40), ("minRR", 0, 10), ("days", 20, 3000)):
        try:
            out[k] = max(lo, min(hi, float(p.get(k))))
        except (TypeError, ValueError):
            out[k] = DEFAULT[k]
    for k in ("ema", "bufTicks", "days"):
        out[k] = int(out[k])
    le = str(p.get("lastEntry") or "12:00")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "09:45" <= le <= "15:30" else "12:00"
    tk = p.get("tickers") or DEFAULT["tickers"]
    tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
    out["tickers"] = [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:2] or ["NQ"]
    return out


def describe(p):
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    rr = f", only if the target is {p['minRR']:g}R or more" if p["minRR"] else ""
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute: a close outside the first {p['rangeMin']}-minute range ({side}), then a retest of the "
            f"{p['ema']} EMA (a close back through it, then a close back out): enter {'at that close' if p.get('entryAt', 'close') == 'close' else 'the next open'}, stop past the confirmation candle "
            f"+ {p['bufTicks']} tick(s), target the low / high of the day{rr}, no entry after {p['lastEntry']}, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def _short(B, E, p):
    """The short of one session. B: [(t, o, h, l, c)] (prices may be negated for the long side), E: the EMA per candle."""
    n_rng = max(1, p["rangeMin"] // p["tf"])
    if len(B) <= n_rng + 3:
        return None
    rlo = min(b[3] for b in B[:n_rng])
    lod = min(b[3] for b in B[:n_rng])
    bk = above = None
    for k in range(n_rng, len(B) - 1):
        t, o, h, l, c = B[k]
        if t[11:16] > p["lastEntry"]:
            return None
        if bk is None:
            if c < rlo:
                bk = k
            lod = min(lod, l)
            continue
        if above is None:
            if c > E[k]:
                above = k                              # the retest: a close above the EMA
            lod = min(lod, l)
            continue
        if c < E[k]:                                   # the confirmation: back under the EMA
            lod = min(lod, l)
            at_close = p.get("entryAt", "close") == "close"
            if at_close:                               # enter at the confirmation candle's close
                nt, e, j0 = _end(t, p["tf"]), c, k + 1
            else:                                      # or at the next candle's open
                nt, e, j0 = B[k + 1][0], B[k + 1][1], k + 1
            if nt[11:16] > p["lastEntry"]:
                return None
            stop = h + p["bufTicks"] * TICK
            tgt = lod
            R = stop - e
            if R <= 0 or e - tgt <= 0 or (p["minRR"] and (e - tgt) < p["minRR"] * R):
                above = None                           # not worth it: wait for the next retest
                continue
            x = why = None
            for j in range(j0, len(B)):
                tj, oj, hj, lj, cj = B[j]
                gap = at_close or j > k + 1            # the open can be past the stop / target
                if hj >= stop:
                    x, why = (max(oj, stop) if gap else stop), "stop"
                elif lj <= tgt:
                    x, why = (min(oj, tgt) if gap else tgt), "target"
                elif tj[11:16] >= "15:55":
                    x, why = cj, "close"
                if x is not None:
                    break
            if x is None:
                tj, x, why = B[-1][0], B[-1][4], "close"
            return {"in": nt, "out": tj, "e": e, "x": x, "why": why, "stop": stop, "tgt": tgt, "R": R, "k": j0,
                    "bk": B[bk][0], "rt": B[above][0], "cf": t, "wait": j0 - bk, "rhi": max(b[2] for b in B[:n_rng]), "rlo": rlo}
        lod = min(lod, l)
    return None


def _end(t, tf):
    """The last minute of a tf-minute candle starting at t (when its close is known)."""
    m = int(t[11:13]) * 60 + int(t[14:16]) + tf - 1
    return f"{t[:11]}{m // 60:02d}:{m % 60:02d}"


def ema_for(sym, by, p):
    """The EMA at each regular-hours candle of `by` ({day: candles}), computed on the full Globex session
    (`emaOn` full, what a futures chart shows by default) or on the regular-hours candles only (`emaOn` rth)."""
    import futdata
    days = sorted(by)
    if not days:
        return {}
    if p.get("emaOn", "full") == "rth":
        allc = [c for d in days for c in by[d]]
        E = _ema([c[4] for c in allc], p["ema"])
        out, k = {}, 0
        for d in days:
            out[d] = E[k:k + len(by[d])]
            k += len(by[d])
        return out
    full = futdata.sessions(sym, None, end=days[-1], rth=False, start=days[0])
    seq = [b for d in sorted(full) for b in futdata.agg(full[d], p["tf"])]
    E = _ema([b[4] for b in seq], p["ema"])
    at = {b[0]: v for b, v in zip(seq, E)}
    out = {}
    for d in days:
        last, xs = None, []
        for c in by[d]:
            last = at.get(c[0], last)
            xs.append(last if last is not None else c[4])
        out[d] = xs
    return out


def _ema(xs, n):
    m, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else m * x + (1 - m) * e
        out.append(e)
    return out


def setups(B, E, p):
    out = []
    if p["side"] in ("short", "both"):
        x = _short(B, E, p)
        if x:
            out.append({**x, "side": "short", "pct": (x["e"] - x["x"]) / x["e"] * 100, "r": (x["e"] - x["x"]) / x["R"],
                        "tgtDist": x["e"] - x["tgt"], "stopDist": x["stop"] - x["e"]})
    if p["side"] in ("long", "both"):
        N = [(t, -o, -l, -h, -c) for t, o, h, l, c in B]
        x = _short(N, [-v for v in E], p)
        if x:
            e, xx = -x["e"], -x["x"]
            out.append({**x, "e": e, "x": xx, "stop": -x["stop"], "tgt": -x["tgt"], "rhi": -x["rlo"], "rlo": -x["rhi"], "side": "long", "pct": (xx - e) / e * 100,
                        "r": (xx - e) / x["R"], "tgtDist": x["e"] - x["tgt"], "stopDist": x["stop"] - x["e"]})
    out.sort(key=lambda z: z["in"])
    return out[:1]                                     # one trade per day: the first


def all_trades(sub, p, cells=None, prog=None):
    import range50
    trades, base = [], []
    rnd = random.Random(17)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {p['days']} days of {p['tf']}-minute candles")
        by = range50.bars(sub, sym, p["days"], p["tf"], p.get("_start"))
        days = sorted(by)
        EM = ema_for(sym, by, p)
        for d in days:
            B = by[d]
            Ed = EM.get(d) or []
            if len(B) < 30:
                continue
            for x in setups(B, Ed, p):
                x.update(s=sym, d=d, cell=(cells or {}).get(d))
                trades.append(x)
                base.append((d, x["side"], range50.baseline(B, x, rnd)))
    return trades, base


def decorate(x, p):
    import range50
    x["tod"] = range50.tod(x["in"])
    x["wd"] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.date.fromisoformat(x["d"]).weekday()]
    w = x.get("wait") or 0
    x["wait"] = "1-3" if w <= 3 else "4-8" if w <= 8 else "9-20" if w <= 20 else "21+"
    rr = x["tgtDist"] / x["stopDist"] if x.get("stopDist") else 0
    x["rr"] = "under 1.5R" if rr < 1.5 else "1.5-3R" if rr < 3 else "3R+"


def overlay(sub, p, sym, start, end):
    """What to draw with the trades: the EMA the rule uses (its own candles) and each day's opening range."""
    import range50
    d0 = (dt.date.fromisoformat(start) - dt.timedelta(days=10)).isoformat()
    by = range50.bars(sub, sym, p["days"], p["tf"], d0)
    days = sorted(by)
    if p.get("emaOn", "full") == "rth":
        EM = ema_for(sym, by, p)
        pts = [[_end(c[0], p["tf"]), round(v, 2)] for d in days if start <= d <= end for c, v in zip(by[d], EM[d])]
    else:                                              # the whole Globex session, as on a futures chart
        import futdata
        full = futdata.sessions(sym, None, end=end, rth=False, start=d0)
        seq = [b for d in sorted(full) for b in futdata.agg(full[d], p["tf"])]
        E = _ema([b[4] for b in seq], p["ema"])
        pts = [[_end(b[0], p["tf"]), round(v, 2)] for b, v in zip(seq, E) if start <= futdata.session_of(b[0]) <= end]
    n = max(1, p["rangeMin"] // p["tf"])
    rng = [{"day": d, "hi": max(b[2] for b in by[d][:n]), "lo": min(b[3] for b in by[d][:n]), "t0": by[d][0][0],
            "t1": _end(by[d][n - 1][0], p["tf"]), "t2": f"{d}T{p['lastEntry']}"} for d in days if start <= d <= end and len(by[d]) > n]
    return {"lines": [{"name": f"EMA {p['ema']} ({p['tf']}m)", "color": "#E7C35A", "pts": pts}], "ranges": rng}
