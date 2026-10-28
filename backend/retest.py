"""Breakout retest: resistance turned support (the "2x US Investing Champion" set-up).

  1. Daily: a breakout level = the highest high of the `lookback` days before; a breakout day = the first close above it.
  2. In the `window` trading days after: price pulls back to the level (a candle's low within `tol`% above it, or under
     it), on `tf`-minute candles (15 or 60). A daily close more than `tol`% under the level kills the set-up.
  3. Support from the 10 / 20-day EMA (`ema`: off, 10, 20 or either): the same candle also tests the EMA (low within
     `tol`% of it) and closes above it (the EMA from the previous close: known then).
  4. A bullish reversal candle there (hammer: lower wick >= 2x the body, close in the top third; or a bullish engulfing),
     closing above the level: buy the next candle's open.
  5. Stop under the reversal candle's low; target `tgtR` x the risk; out at the close after `hold` trading days.
One trade per breakout. Same candle through the stop and the target: the stop counts."""
import random

DEFAULT = {"tf": 60, "lookback": 20, "window": 10, "tol": 0.5, "ema": "either", "tgtR": 2.0, "hold": 5, "days": 250,
           "list": "Main", "tickers": []}
ROW = ("tgtR", (1.0, 2.0, 3.0), "R target")
COL = ("ema", ("off", "10", "20", "either"))
COL_NAMES = {"off": "no EMA", "10": "EMA 10", "20": "EMA 20", "either": "EMA 10 or 20"}
FACTORS = [("pat", "Reversal candle"), ("depth", "How deep the pullback went"), ("dsb", "Days since the breakout"),
           ("conf", "EMA within 1% of the level"), ("bk", "Breakout close vs the level"), ("tod", "Time of the entry"),
           ("s", "Ticker"), ("wd", "Weekday")]
_CACHE = {}


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "retest", "tf": 15 if str(p.get("tf")) == "15" else 60, "ema": p.get("ema") if p.get("ema") in COL[1] else "either",
           "list": str(p.get("list") or "Main")[:60], "side": "long"}
    for k, lo, hi in (("lookback", 5, 120), ("window", 2, 30), ("tol", 0.05, 3), ("tgtR", 0.5, 10), ("hold", 1, 20), ("days", 30, 800)):
        try:
            out[k] = max(lo, min(hi, float(p.get(k))))
        except (TypeError, ValueError):
            out[k] = DEFAULT[k]
    for k in ("lookback", "window", "hold", "days"):
        out[k] = int(out[k])
    tk = p.get("tickers") or []
    tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
    out["tickers"] = [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:80]
    return out


def describe(p):
    e = {"off": "", "10": " and the 10-day EMA", "20": " and the 20-day EMA", "either": " and the 10 or 20-day EMA"}[p["ema"]]
    return (f"A close above the {p['lookback']}-day high, then within {p['window']} days a pullback to that level{e} "
            f"({p['tol']:g}%): a bullish reversal {'1-hour' if p['tf'] == 60 else '15-minute'} candle there, buy the next open; "
            f"stop under its low, target {p['tgtR']:g}R, out after {p['hold']} days")


def tickers(sub, p):
    import wallwatch
    if p.get("tickers"):
        return list(p["tickers"])
    cfg = wallwatch.config(sub)
    lst = next((L for L in cfg["lists"] if L["name"] == p.get("list")), None) or cfg["lists"][0]
    return list(lst["tickers"])


def intraday(sub, sym, days, tf):
    """{day: [(t, o, h, l, c)]} regular hours, 15-minute candles (or hourly from 09:30, made from them)."""
    import alpaca
    import daytrade
    from datetime import timedelta
    from util import now_ny
    k = (sub, sym, days, now_ny().strftime("%Y-%m-%d %H"))
    if k not in _CACHE:
        if len(_CACHE) > 200:
            _CACHE.clear()
        c = daytrade.data_creds(sub)
        if not c:
            raise ValueError("Connect an Alpaca account to load the 15-minute candles.")
        now = now_ny().replace(tzinfo=None)
        start = (now - timedelta(days=int(days * 1.5) + 5)).replace(hour=4, minute=0, second=0, microsecond=0)
        live = c.get("feed") == "sip"
        end = now - timedelta(minutes=1 if live else 16)
        _CACHE[k] = alpaca.bars(c, sym, start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S"), 15, max_pages=20)
    by = {}
    for b in _CACHE[k]:
        hm = b["t"][11:16]
        if "09:30" <= hm < "16:00":
            by.setdefault(b["t"][:10], []).append((b["t"][:16], b["o"], b["h"], b["l"], b["c"]))
    if tf == 15:
        return by
    out = {}
    for d, xs in by.items():
        grp = {}
        for t, o, h, l, c in xs:
            m = int(t[11:13]) * 60 + int(t[14:16]) - 570          # minutes from 09:30
            grp.setdefault(m // 60, []).append((t, o, h, l, c))
        out[d] = [(g[0][0], g[0][1], max(x[2] for x in g), min(x[3] for x in g), g[-1][4]) for _, g in sorted(grp.items())]
    return out


def _reversal(prev, cur):
    t, o, h, l, c = cur
    body = abs(c - o)
    rng = h - l
    if rng <= 0:
        return None
    lower = min(o, c) - l
    if c >= o and lower >= 2 * max(body, rng * 0.05) and c >= l + rng * 2 / 3:
        return "hammer"
    if prev and prev[4] < prev[1] and c > o and c >= prev[1] and o <= prev[4]:
        return "engulfing"
    return None


def _walk(cs, k0, e, stop, tgt, last_day):
    """From candle k0 on (entry at its open e): stop / target / time (the close of last_day). (exit, time, why, index)."""
    for j in range(k0, len(cs)):
        t, o, h, l, c = cs[j]
        if j > k0 and o <= stop:
            return o, t, "stop", j
        if l <= stop:
            return stop, t, "stop", j
        if j > k0 and o >= tgt:
            return o, t, "target", j
        if h >= tgt:
            return tgt, t, "target", j
        if t[:10] >= last_day and (j + 1 == len(cs) or cs[j + 1][0][:10] > t[:10]):
            return c, t, "time", j
    return cs[-1][4], cs[-1][0], "time", len(cs) - 1


def setups(daily, intra, p, sym=""):
    """daily: {day: (o, h, l, c, v)}; intra: {day: [candles]}. The trades of one ticker."""
    import macro
    days = sorted(daily)
    cl = [daily[d][3] for d in days]
    e10, e20 = macro.ema(cl, 10), macro.ema(cl, 20)
    cs = [x for d in sorted(intra) for x in intra[d]]
    pos = {}
    for k, x in enumerate(cs):
        pos.setdefault(x[0][:10], k)
    tol = p["tol"] / 100
    out = []
    i, n = p["lookback"], len(days)
    busy_until = ""
    while i < n:
        d = days[i]
        lvl = max(daily[x][1] for x in days[i - p["lookback"]:i])
        if not (cl[i] > lvl and cl[i - 1] <= lvl) or d < busy_until:
            i += 1
            continue
        bk = (cl[i] / lvl - 1) * 100
        trade = None
        for w in range(1, p["window"] + 1):
            j = i + w
            if j >= n:
                break
            dj = days[j]
            if dj not in pos:
                continue
            em = {"10": e10[j - 1], "20": e20[j - 1]}
            k = pos[dj]
            while k < len(cs) and cs[k][0][:10] == dj:
                t, o, h, l, c = cs[k]
                if l <= lvl * (1 + tol) and c > lvl:
                    ok_ema = True
                    if p["ema"] != "off":
                        keys = ("10", "20") if p["ema"] == "either" else (p["ema"],)
                        ok_ema = any(em[q] and l <= em[q] * (1 + tol) and c > em[q] for q in keys)
                    pat = _reversal(cs[k - 1] if k and cs[k - 1][0][:10] == dj else None, cs[k]) if ok_ema else None
                    if pat and k + 1 < len(cs):
                        e = cs[k + 1][1]
                        stop = l * 0.999
                        if e > stop and (e - stop) / e <= 0.04:
                            R = e - stop
                            day0 = cs[k + 1][0][:10]
                            di = days.index(day0) if day0 in daily else j
                            last = days[min(n - 1, di + p["hold"] - 1)]
                            x, tx, why, kx = _walk(cs, k + 1, e, stop, e + p["tgtR"] * R, last)
                            depth = "under the level" if l < lvl * (1 - 0.003) else "at the level (±0.3%)" if l <= lvl * 1.003 else "just above it"
                            conf = any(em[q] and abs(em[q] / lvl - 1) <= 0.01 for q in ("10", "20"))
                            trade = {"s": sym, "d": day0, "in": cs[k + 1][0], "out": tx, "e": e, "x": x, "why": why, "side": "long",
                                     "pct": (x / e - 1) * 100, "r": (x - e) / R, "riskPct": R / e * 100, "lvl": lvl, "pat": pat, "depth": depth, "bkDay": d, "stop": stop, "tgt": e + p["tgtR"] * R,
                                     "dsb": f"{w} day{'s' if w > 1 else ''}" if w <= 3 else "4+ days", "conf": "yes" if conf else "no",
                                     "bk": "under 1%" if bk < 1 else "1-3%" if bk < 3 else "3%+", "touch": t, "k": k + 1, "kx": kx}
                            busy_until = tx[:10]
                    break_ = trade is not None
                    if break_:
                        break
                k += 1
            if trade:
                break
            if cl[j] < lvl * (1 - tol):                   # closed back under the level: the set-up is dead
                break
        if trade:
            out.append(trade)
        i += 1
    return out, cs


def baseline(cs, tr, p, rnd, n=3):
    """The same bracket (same risk %, same R target, same hold) entered at random candles within 30 days."""
    out = []
    k0 = tr["k"]
    lo, hi = max(0, k0 - 30 * 7), min(len(cs) - 2, k0 + 30 * 7)
    for _ in range(n):
        k = rnd.randint(lo, hi) if hi > lo else k0
        e = cs[k][1]
        stop = e * (1 - tr["riskPct"] / 100)
        R = e - stop
        d0 = cs[k][0][:10]
        dd = sorted({x[0][:10] for x in cs[k:k + 7 * (p["hold"] + 2)]})
        last = dd[min(len(dd) - 1, p["hold"] - 1)] if dd else d0
        x, _, _, _ = _walk(cs, k, e, stop, e + p["tgtR"] * R, last)
        out.append((x / e - 1) * 100)
    return out


def all_trades(sub, p, cells=None, prog=None):
    import plab
    trades, base = [], []
    rnd = random.Random(11)
    tk = tickers(sub, p)
    for n_, sym in enumerate(tk):
        if prog and n_ % 5 == 0:
            prog(f"{sym}: daily levels + {p['tf']}-minute candles ({n_ + 1}/{len(tk)})")
        try:
            daily = plab._bars(sym, 3)
            intra = intraday(sub, sym, p["days"], p["tf"])
        except Exception as e:
            print("retest data failed", sym, e)
            continue
        if not daily or not intra:
            continue
        tr, cs = setups(daily, intra, p, sym)
        for x in tr:
            x["cell"] = (cells or {}).get(x["d"])
            trades.append(x)
            base.append((x["d"], "long", baseline(cs, x, p, rnd)))
    return trades, base


def decorate(x, p):
    import range50
    x["tod"] = range50.tod(x["in"])
    import datetime as dt
    x["wd"] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.date.fromisoformat(x["d"]).weekday()]


def coverage(sub, p, prog):
    cov = []
    for n_, sym in enumerate(tickers(sub, p)):
        if prog and n_ % 5 == 0:
            prog(f"{sym}: {p['days']} days of 15-minute candles")
        try:
            by = intraday(sub, sym, p["days"], p["tf"])
        except Exception as e:
            print("retest coverage failed", sym, e)
            continue
        ds = sorted(by)
        if ds:
            cov.append({"s": sym, "from": ds[0], "to": ds[-1], "days": len(ds), "bars": sum(len(v) for v in by.values())})
    return cov
