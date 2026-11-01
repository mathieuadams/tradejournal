"""FVG inversion with the higher-timeframe EMA bias, on NQ / ES (5-minute, NYSE session).

Long (short = the mirror):
  1. Bias (`bias`), at the entry candle, from 20 EMAs of the full session's candles (overnight included), each the
     live value of the forming higher-timeframe candle (what the chart shows at that moment):
       h1   = the 5-minute close is above the hourly 20 EMA and the hourly EMA is rising;
       all3 = above the 15-, 30- and 60-minute 20 EMAs and all three rising.
     Asian session check (`asia`, 18:00-02:00 ET): dir = the Asian session closed up (long) / down (short);
     hold = the price is above the Asian session's middle (long) / under it (short); none.
  2. The pullback: a bearish fair value gap (candle 1's low above candle 3's high, at least `minGap` ticks) forms in
     regular hours.
  3. The inversion: within `fvgMax` candles, a candle closes above the gap's top: buy at that close (from 09:35,
     before `lastEntry`), when the bias is long at that candle.
  4. Stop (`stop`): fvg = the gap's lower end; swing = the lowest low since the gap formed (- `bufTicks` ticks).
  5. Target (`tgt`): the previous liquidity = the nearest untaken swing high above the entry (`pivLen` candles each side,
     in the last `liqLook` candles, overnight included).
       liq2r = that liquidity, or 2R when it's farther (and 2R when there is none);  liq = the liquidity;  r = `tgtR` x the risk.
     Skipped when the target is under `minRR` x the risk.
  6. One position at a time, at most `maxDay` a session, flat at 15:55."""
import random

import sessionkit as K

DEFAULT = {"tickers": ["NQ"], "tf": 5, "bias": "all3", "asia": "dir", "minGap": 4, "fvgMax": 24, "stop": "fvg", "bufTicks": 0, "tgt": "liq2r", "tgtR": 2.0,
           "pivLen": 3, "liqLook": 150, "minRR": 1.0, "lastEntry": "12:00", "maxDay": 2, "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("al", "EMAs agreeing"), ("as", "Asian session"), ("tk", "Target"),
           ("gap", "Gap size")]
EMAS = (15, 30, 60)


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "ifvg", "tf": 1 if str(p.get("tf")) == "1" else 5, "bias": p.get("bias") if p.get("bias") in ("h1", "all3") else "all3",
           "asia": p.get("asia") if p.get("asia") in ("dir", "hold", "none") else "dir", "stop": p.get("stop") if p.get("stop") in ("fvg", "swing") else "fvg",
           "tgt": p.get("tgt") if p.get("tgt") in ("liq2r", "liq", "r") else "liq2r", "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    le = str(p.get("lastEntry") or "12:00")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "09:40" <= le <= "15:30" else "12:00"
    K.num(p, out, (("minGap", 0, 200, 4), ("fvgMax", 1, 100, 24), ("bufTicks", 0, 40, 0), ("tgtR", 0.5, 10, 2.0), ("pivLen", 1, 10, 3), ("liqLook", 10, 600, 150),
                   ("minRR", 0, 10, 1.0), ("maxDay", 1, 10, 2), ("days", 20, 3000, 500)), ("minGap", "fvgMax", "bufTicks", "pivLen", "liqLook", "maxDay", "days"))
    out["tickers"] = K.tickers_of(p)
    return out


def describe(p):
    b = "the hourly 20 EMA (price on its side, EMA sloping that way)" if p["bias"] == "h1" else "the 15 / 30 / 60-minute 20 EMAs (price on their side, all sloping that way)"
    a = {"dir": ", confirmed by the Asian session's direction", "hold": ", price on the bias side of the Asian session's middle", "none": ""}[p["asia"]]
    tg = {"liq2r": "the previous liquidity (capped at 2R)", "liq": "the previous liquidity", "r": f"{p['tgtR']:g}R"}[p["tgt"]]
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute, NYSE session: bias from {b}{a}. A fair value gap against the bias (the pullback), then a close "
            f"through it (inversion, within {p['fvgMax']} candles): enter at that close ({side}, before {p['lastEntry']}), stop at "
            f"{'the far end of the gap' if p['stop'] == 'fvg' else 'the pullback extreme'}, target {tg}" + (f" (at least {p['minRR']:g}R)" if p["minRR"] else "")
            + f", at most {p['maxDay']} a session, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def _mins(t):
    return int(t[11:13]) * 60 + int(t[14:16])


def ema_track(F):
    """For the full-session candles F (all days, in order): {time: [(ema, rising) for 15, 30, 60]}: the 20 EMA with the
    forming higher-timeframe candle at this candle's close, and whether it is above the last completed one."""
    m = 2 / 21
    st = {n: {"key": None, "done": None, "last": None} for n in EMAS}
    out = {}
    for t, o, h, l, c in F:
        row = []
        for n in EMAS:
            s, key = st[n], (t[:10], _mins(t) // n)
            if key != s["key"]:
                if s["last"] is not None:                       # the previous candle closed: fold it in
                    s["done"] = s["last"] if s["done"] is None else m * s["last"] + (1 - m) * s["done"]
                s["key"] = key
            s["last"] = c
            now = c if s["done"] is None else m * c + (1 - m) * s["done"]
            row.append((now, s["done"] is not None and now > s["done"], s["done"] is not None and now < s["done"]))
        out[t] = row
    return out


def bias_at(row, c, p):
    use = row[2:] if p["bias"] == "h1" else row
    if all(c > e and up for e, up, dn in use):
        return 1
    if all(c < e and dn for e, up, dn in use):
        return -1
    return 0


def asia_of(on):
    a = [b for b in on if b[0][11:16] >= "18:00" or b[0][11:16] < "02:00"]
    if not a:
        return None
    return {"o": a[0][1], "c": a[-1][4], "h": max(b[2] for b in a), "l": min(b[3] for b in a)}


def liquidity(F, i, d, e, p):
    """The nearest untaken swing high (d > 0) / low above / under e, confirmed before candle i of F."""
    n, best = p["pivLen"], None
    for k in range(max(n, i - p["liqLook"]), i - n):
        win = F[k - n:k + n + 1]
        if d > 0:
            lv = F[k][2]
            if lv > e and lv == max(b[2] for b in win) and all(b[2] <= lv for b in F[k + 1:i]):
                best = lv if best is None else min(best, lv)
        else:
            lv = F[k][3]
            if lv < e and lv == min(b[3] for b in win) and all(b[3] >= lv for b in F[k + 1:i]):
                best = lv if best is None else max(best, lv)
    return best


def day_trades(day, on, R, Fd, Fall, fi, E, p):
    """fi: {time: index in Fall}."""
    if len(R) < 5:
        return []
    asia = asia_of(on)
    out, gaps, k, buf = [], [], 2, p["bufTicks"] * K.TICK
    while k < len(R) and len(out) < p["maxDay"]:
        t, o, h, l, c = R[k]
        if K.end_of(t, p["tf"])[11:16] > p["lastEntry"]:
            break
        a, b3 = R[k - 2], R[k]
        # new gaps formed by candles k-2, k-1, k: bearish (a low > b3 high) -> a long setup when inverted; bullish -> short
        if a[3] - b3[2] >= p["minGap"] * K.TICK:
            gaps.append({"d": 1, "top": a[3], "bot": b3[2], "k": k})
        if b3[3] - a[2] >= p["minGap"] * K.TICK:
            gaps.append({"d": -1, "top": b3[3], "bot": a[2], "k": k})
        gaps = [g for g in gaps if k - g["k"] <= p["fvgMax"]]
        hit = None
        for g in gaps:
            if g["k"] == k:
                continue
            if (g["d"] > 0 and c > g["top"]) or (g["d"] < 0 and c < g["bot"]):
                hit = g
                break
        if hit is None or K.end_of(t, p["tf"])[11:16] < "09:35":
            k += 1
            continue
        gaps.remove(hit)                                         # inverted: used up either way
        d = hit["d"]
        ok = not ((p["side"] == "long" and d < 0) or (p["side"] == "short" and d > 0))
        row = E.get(t)
        bias = bias_at(row, c, p) if row else 0
        ad = 0 if not asia else (1 if asia["c"] > asia["o"] else -1 if asia["c"] < asia["o"] else 0)
        am = 0 if not asia else (1 if c > (asia["h"] + asia["l"]) / 2 else -1)
        if p["asia"] == "dir":
            ok = ok and ad == d
        elif p["asia"] == "hold":
            ok = ok and am == d
        if not ok or bias != d:
            k += 1
            continue
        e = c
        if p["stop"] == "fvg":
            stop = hit["bot"] - buf if d > 0 else hit["top"] + buf
        else:
            seg = R[hit["k"] - 2:k + 1]
            stop = (min(x[3] for x in seg) - buf) if d > 0 else (max(x[2] for x in seg) + buf)
        risk = (e - stop) * d
        if risk <= 0:
            k += 1
            continue
        liq = liquidity(Fall, fi[t], d, e, p) if t in fi else None
        two = e + d * 2 * risk
        if p["tgt"] == "liq2r":
            tgt, tk = (liq, "liquidity") if liq is not None and (liq - two) * d < 0 else (two, "2R")
        elif p["tgt"] == "liq":
            tgt, tk = liq, "liquidity"
        else:
            tgt, tk = e + d * p["tgtR"] * risk, "R"
        if tgt is None or (tgt - e) * d <= 0 or (p["minRR"] and abs(tgt - e) < p["minRR"] * risk):
            k += 1
            continue
        x, ot, why, j = K.walk(R, k + 1, d, e, stop, tgt)
        al = sum(1 for em, up, dn in row if (c - em) * d > 0 and (up if d > 0 else dn)) if row else 0
        out.append({"in": K.end_of(t, p["tf"]), "out": ot, "e": e, "x": x, "why": why, "side": "long" if d > 0 else "short", "stop": stop, "tgt": tgt,
                    "stopDist": risk, "tgtDist": abs(tgt - e), "fvgTop": hit["top"], "fvgBot": hit["bot"], "rt": R[hit["k"]][0], "cf": t,
                    "al": al, "as": ad == d, "tk": tk, "gap": hit["top"] - hit["bot"], "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk})
        k = j + 1
    return out


def _series(sym, p, start=None):
    """[(day, on, R, F)] for the sessions, plus the whole-run F, its index and the EMA track (3 warm-up sessions)."""
    import datetime as dt
    q = dict(p)
    if p.get("_start"):
        q["_start"] = (dt.date.fromisoformat(p["_start"]) - dt.timedelta(days=7)).isoformat()
    else:
        q["days"] = p["days"] + 3
    by = K.sessions(sym, q)
    days = sorted(by)
    keep = set(d for d in days if d >= p["_start"]) if p.get("_start") else set(days[-p["days"]:])
    Fall = [b for d in days for b in by[d][2]]
    return [(d, *by[d]) for d in days if d in keep], Fall, {b[0]: i for i, b in enumerate(Fall)}, ema_track(Fall)


def all_trades(sub, p, cells=None, prog=None):
    trades, base = [], []
    rnd = random.Random(59)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {p['tf']}-minute candles + the 15 / 30 / 60-minute EMAs")
        rows, Fall, fi, E = _series(sym, p)
        for d, on, R, F, V in rows:
            for x in day_trades(d, on, R, F, Fall, fi, E, p):
                x.update(s=sym, d=d, cell=None)
                trades.append(x)
                base.append((d, x["side"], K.baseline(R, x, rnd)))
    return trades, base


def decorate(x, p):
    x["tod"] = K.tod(x["in"])
    x["wd"] = K.weekday(x["d"])
    x["al"] = f"{x.get('al', 0)} of 3"
    x["as"] = "with the bias" if x.get("as") else "against / flat"
    g = x.get("gap") or 0
    x["gap"] = "under 5 pts" if g < 5 else "5-15 pts" if g < 15 else "15+ pts"


def overlay(sub, p, sym, start, end):
    """The three EMAs and each session's Asian range."""
    rows, Fall, fi, E = _series(sym, {**p, "_start": start})
    cols = {15: "#5FD4C4", 30: "#E7C35A", 60: "#E07AE0"}
    lines = {n: {"name": f"EMA 20 ({n}m)", "color": cols[n], "pts": []} for n in EMAS}
    rng = []
    for d, on, R, F, V in rows:
        if d > end:
            continue
        for b in F:
            for n, (em, up, dn) in zip(EMAS, E.get(b[0], [])):
                lines[n]["pts"].append([b[0], round(em, 2)])
        a = [b for b in on if b[0][11:16] >= "18:00" or b[0][11:16] < "02:00"]
        if a:
            rng.append({"day": d, "hi": max(b[2] for b in a), "lo": min(b[3] for b in a), "t0": a[0][0], "t1": a[-1][0], "t2": f"{d}T09:30"})
    return {"lines": list(lines.values()), "ranges": rng}
