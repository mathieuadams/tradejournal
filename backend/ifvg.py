"""FVG inversion with the higher-timeframe EMA bias, on NQ / ES (5-minute, NYSE session).

Long (short = the mirror):
  1. Bias (`bias`), at the entry candle, from 20 EMAs of the full session's candles (overnight included), each the
     live value of the forming higher-timeframe candle (what the chart shows at that moment):
       h1   = the 5-minute close is above the hourly 20 EMA and the hourly EMA is rising;
       all3 = above the 15-, 30- and 60-minute 20 EMAs and all three rising.
     Asian session check (`asia`, 18:00-02:00 ET): dir = the Asian session closed up (long) / down (short);
     hold = the price is above the Asian session's middle (long) / under it (short); none.
  2. The pullback: a bearish fair value gap (candle 1's low above candle 3's high, at least `minGap` ticks) forms
     from `gapFrom` (08:00 = pre-market included; 18:00 = the whole overnight).
  3. The inversion: within `fvgMax` candles, a candle closes above the gap's top: buy at that close (from 09:35,
     before `lastEntry`), when the bias is long at that candle. A gap closed through outside those conditions is used up.
  4. Stop (`stop`): fvg = the gap's lower end; swing = the lowest low since the gap formed (- `bufTicks` ticks).
  5. Target (`tgt`): the previous liquidity = the nearest untaken swing high above the entry (`pivLen` candles each side,
     in the last `liqLook` candles, overnight included).
       liq2r = that liquidity, but at least 2R (2R when it's closer or there is none);  liq = the liquidity;  r = `tgtR` x the risk.
     Skipped when the target is under `minRR` x the risk.
  6. One position at a time, at most `maxDay` a session, flat at 15:55.
  7. Exit (`exit`): target = stop / target above; trail = stop to break-even once `beR` R in profit, `partPct`% off at
     `partR` R, the rest without a target, trailing (`trail`): swing = under the low of the last `trailN` closed candles;
     r = `trailR` x the risk behind the best price since the entry; ema15 = out at a close under the 15-minute 20 EMA; none = break-even stop only. Intrabar: the stop is checked first.
  8. Hours (`hours`): rth = entries 09:35-`lastEntry`; all = the overnight too (from 18:00; gaps from 18:00)."""
import random

import sessionkit as K

DEFAULT = {"tickers": ["NQ"], "tf": 5, "bias": "all3", "asia": "none", "minGap": 4, "fvgMax": 24, "gapFrom": "08:00", "stop": "fvg", "bufTicks": 0, "tgt": "liq2r", "tgtR": 2.0,
           "pivLen": 3, "liqLook": 150, "minRR": 1.0, "lastEntry": "14:00", "maxDay": 2, "hours": "all",
           "exit": "trail", "beR": 0.5, "partR": 1.0, "partPct": 50, "trail": "swing", "trailN": 3, "trailR": 0.5,
           "trigSize": "body", "trigMin": 0.0, "trigMax": 0.0, "bodyPct": 0, "side": "both", "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("al", "EMAs agreeing"), ("as", "Asian session"), ("tk", "Target"),
           ("gap", "Gap size"), ("ses", "Session"), ("tb", "Trigger candle body (× the average range)"), ("tr", "Trigger candle range (× the average range)"),
           ("bp", "Trigger candle body (% of its range)")]
EMAS = (15, 30, 60)


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "ifvg", "tf": 1 if str(p.get("tf")) == "1" else 5, "bias": p.get("bias") if p.get("bias") in ("h1", "all3") else "all3",
           "asia": p.get("asia") if p.get("asia") in ("dir", "hold", "none") else "none", "stop": p.get("stop") if p.get("stop") in ("fvg", "swing") else "fvg",
           "tgt": p.get("tgt") if p.get("tgt") in ("liq2r", "liq", "r") else "liq2r", "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    le = str(p.get("lastEntry") or "14:00")
    out["lastEntry"] = le if len(le) == 5 and le[2] == ":" and "09:40" <= le <= "15:30" else "14:00"
    gf = str(p.get("gapFrom") or "08:00")
    out["gapFrom"] = gf if len(gf) == 5 and gf[2] == ":" and ("04:00" <= gf <= "09:30" or gf == "18:00") else "08:00"
    out["hours"] = p.get("hours") if p.get("hours") in ("rth", "all") else "all"
    out["exit"] = p.get("exit") if p.get("exit") in ("target", "trail") else "trail"
    out["trail"] = p.get("trail") if p.get("trail") in ("swing", "r", "ema15", "none") else "swing"
    out["trigSize"] = p.get("trigSize") if p.get("trigSize") in ("body", "range") else "body"
    K.num(p, out, (("trigMin", 0, 10, 0.0), ("trigMax", 0, 20, 0.0), ("bodyPct", 0, 100, 0)), ("bodyPct",))
    K.num(p, out, (("beR", 0, 5, 0.5), ("partR", 0, 10, 1.0), ("partPct", 0, 100, 50), ("trailN", 1, 50, 3), ("trailR", 0.1, 10, 0.5)), ("trailN",))
    K.num(p, out, (("minGap", 0, 200, 4), ("fvgMax", 1, 100, 24), ("bufTicks", 0, 40, 0), ("tgtR", 0.5, 10, 2.0), ("pivLen", 1, 10, 3), ("liqLook", 10, 600, 150),
                   ("minRR", 0, 10, 1.0), ("maxDay", 1, 10, 2), ("days", 20, 3000, 500)), ("minGap", "fvgMax", "bufTicks", "pivLen", "liqLook", "maxDay", "days"))
    out["tickers"] = K.tickers_of(p)
    return out


def describe(p):
    b = "the hourly 20 EMA (price on its side, EMA sloping that way)" if p["bias"] == "h1" else "the 15 / 30 / 60-minute 20 EMAs (price on their side, all sloping that way)"
    a = {"dir": ", confirmed by the Asian session's direction", "hold": ", price on the bias side of the Asian session's middle", "none": ""}[p["asia"]]
    tg = {"liq2r": "the previous liquidity (at least 2R)", "liq": "the previous liquidity", "r": f"{p['tgtR']:g}R"}[p["tgt"]]
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute, NYSE session: bias from {b}{a}. A fair value gap against the bias (the pullback), then a close "
            f"through it (inversion, within {p['fvgMax']} candles): enter at that close ({side}, before {p['lastEntry']}), stop at "
            f"{'the far end of the gap' if p['stop'] == 'fvg' else 'the pullback extreme'}" + ("" if p["exit"] == "trail" else f", target {tg}" + (f" (skip under {p['minRR']:g}R)" if p["minRR"] else ""))
            + f", at most {p['maxDay']} a session, flat at 15:55" + _trig_txt(p) + _exit_txt(p)).replace("NYSE session", "overnight + NYSE session" if p["hours"] == "all" else "NYSE session")


def _trig_txt(p):
    parts = []
    m = "body" if p.get("trigSize", "body") == "body" else "range (wicks included)"
    if p.get("trigMin"):
        parts.append(f"at least {p['trigMin']:g}×")
    if p.get("trigMax"):
        parts.append(f"at most {p['trigMax']:g}×")
    txt = f". Trigger candle {m} {' and '.join(parts)} the average candle range (last 20)" if parts else ""
    if p.get("bodyPct"):
        txt += (", " if txt else ". Trigger candle ") + f"body at least {p['bodyPct']}% of its range"
    return txt


def _exit_txt(p):
    if p["exit"] != "trail":
        return ""
    tr = {"swing": f"the last {p['trailN']} candles' low / high", "r": f"{p['trailR']:g}R behind the best price", "ema15": "the 15-minute 20 EMA (a close through it)", "none": "nothing (flat at 15:55)"}[p["trail"]]
    return (". Managed: " + (f"stop to break-even at {p['beR']:g}R, " if p["beR"] else "") + (f"{p['partPct']:g}% off at {p['partR']:g}R, " if p["partPct"] else "")
            + f"the rest trails {tr}, no target")


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


def day_trades(day, on, R, Fd, Fall, fi, E, p, notes=None):
    """The session's trades. Gaps form on the full session's candles Fd (from `gapFrom`, pre-market included); entries in
    regular hours. notes: a list that gets every inversion and why it was / wasn't taken (for the chart)."""
    if len(Fd) < 5:
        return []
    asia = asia_of(on)
    out, gaps, buf, k = [], [], p["bufTicks"] * K.TICK, 2
    rth0 = f"{day}T09:30"
    while k < len(Fd) and len(out) < p["maxDay"]:
        t, o, h, l, c = Fd[k]
        today = t[:10] == day
        if today and K.end_of(t, p["tf"])[11:16] > p["lastEntry"]:
            break
        a, b3 = Fd[k - 2], Fd[k]
        if (today and t[11:16] >= p["gapFrom"]) or p["gapFrom"] == "18:00" or p["hours"] == "all":
            if a[3] - b3[2] >= p["minGap"] * K.TICK:      # bearish gap (candle 1's low over candle 3's high): a long when inverted
                gaps.append({"d": 1, "top": a[3], "bot": b3[2], "k": k})
            if b3[3] - a[2] >= p["minGap"] * K.TICK:      # bullish gap: a short when inverted
                gaps.append({"d": -1, "top": b3[3], "bot": a[2], "k": k})
        gaps = [g for g in gaps if k - g["k"] <= p["fvgMax"]]
        hit = next((g for g in gaps if g["k"] != k and ((g["d"] > 0 and c > g["top"]) or (g["d"] < 0 and c < g["bot"]))), None)
        if hit is None:
            k += 1
            continue
        gaps.remove(hit)                                         # inverted: used up either way
        d, why = hit["d"], None
        row = E.get(t)
        bias = bias_at(row, c, p) if row else 0
        ad = 0 if not asia else (1 if asia["c"] > asia["o"] else -1 if asia["c"] < asia["o"] else 0)
        am = 0 if not asia else (1 if c > (asia["h"] + asia["l"]) / 2 else -1)
        if p["hours"] == "rth" and (t < rth0 or K.end_of(t, p["tf"])[11:16] < "09:35"):
            why = "before 09:35"
        elif p["hours"] == "all" and "16:00" <= t[11:16] < "18:00":
            why = "market closed"
        elif (p["side"] == "long" and d < 0) or (p["side"] == "short" and d > 0):
            why = "other side"
        elif bias != d:
            why = "bias " + ("flat" if not bias else "against")
        elif p["asia"] == "dir" and ad != d:
            why = "Asia closed against"
        elif p["asia"] == "hold" and am != d:
            why = "Asia middle against"
        w = Fd[max(0, k - 20):k]
        avg = sum(b[2] - b[3] for b in w) / len(w) if w else 0
        body, rng = abs(c - o), h - l
        size = (body if p["trigSize"] == "body" else rng) / avg if avg > 0 else 0
        if not why and p["trigMin"] and size < p["trigMin"]:
            why = f"trigger candle {size:.1f}× < {p['trigMin']:g}×"
        elif not why and p["trigMax"] and size > p["trigMax"]:
            why = f"trigger candle {size:.1f}× > {p['trigMax']:g}×"
        elif not why and p["bodyPct"] and rng > 0 and body / rng * 100 < p["bodyPct"]:
            why = f"trigger body {body / rng * 100:.0f}% of its range"
        e = c
        if p["stop"] == "fvg":
            stop = hit["bot"] - buf if d > 0 else hit["top"] + buf
        else:
            seg = Fd[hit["k"] - 2:k + 1]
            stop = (min(x[3] for x in seg) - buf) if d > 0 else (max(x[2] for x in seg) + buf)
        risk = (e - stop) * d
        tgt = tk = None
        if not why and risk <= 0:
            why = "no risk"
        if not why and p["exit"] == "trail":                    # managed: no target, so no target filter
            tgt, tk = (e + d * p["partR"] * risk if p["partPct"] else e + d * 2 * risk), "trail"
        elif not why:
            liq = liquidity(Fall, fi[t], d, e, p) if t in fi else None
            two = e + d * 2 * risk
            if p["tgt"] == "liq2r":
                tgt, tk = (liq, "liquidity") if liq is not None and (liq - two) * d > 0 else (two, "2R")   # liquidity under 2R away: 2R
            elif p["tgt"] == "liq":
                tgt, tk = liq, "liquidity"
            else:
                tgt, tk = e + d * p["tgtR"] * risk, "R"
            if tgt is None:
                why = "no liquidity"
            elif (tgt - e) * d <= 0 or (p["minRR"] and abs(tgt - e) < p["minRR"] * risk):
                why = f"target {abs(tgt - e) / risk:.1f}R < {p['minRR']:g}R"
        if notes is not None:
            notes.append({"t": t, "p": e, "d": d, "ok": not why, "text": ("Long" if d > 0 else "Short") + (" entry" if not why else f" skipped: {why}"),
                          "gap": [Fd[hit["k"] - 2][0], hit["bot"], hit["top"]]})
        if why:
            k += 1
            continue
        x, ot, ex, j, part = manage(Fd, k + 1, d, e, stop, tgt, risk, p, E, f"{day}T15:55")
        al = sum(1 for em, up, dn in row if (c - em) * d > 0 and (up if d > 0 else dn)) if row else 0
        out.append({"in": K.end_of(t, p["tf"]), "out": ot, "e": e, "x": x, "why": ex, "side": "long" if d > 0 else "short", "stop": stop, "tgt": tgt,
                    "stopDist": risk, "tgtDist": abs(tgt - e), "fvgTop": hit["top"], "fvgBot": hit["bot"], "rt": Fd[hit["k"]][0], "cf": t,
                    "al": al, "as": ad == d, "tk": tk, "gap": hit["top"] - hit["bot"], "ses": t >= rth0, "part": part, "tb": body / avg if avg > 0 else None, "tr": rng / avg if avg > 0 else None,
                    "bp": body / rng * 100 if rng > 0 else None, "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk})
        k = j + 1
    return out


def manage(B, j0, d, e, stop, tgt, risk, p, E, flat):
    """The exit of a position entered at the close before candle j0. exit=target: stop / target; exit=trail: break-even,
    partial, trailing runner. flat: 'YYYY-MM-DDTHH:MM' (exit at that candle's close). Returns (average exit price, exit
    time, why, index, partial [time, price] or None). The stop is checked before anything else in a candle."""
    trail = p["exit"] == "trail"
    left, got, part, be_done, best = 1.0, 0.0, None, False, e
    st = stop
    pct = p["partPct"] / 100 if trail else 0
    for j in range(j0, len(B)):
        t, o, h, l, c = B[j]
        if (d > 0 and l <= st) or (d < 0 and h >= st):
            px = min(o, st) if d > 0 else max(o, st)
            why = "stop" if st == stop else "break-even" if abs(st - e) < 1e-9 else "trail"
            return got + left * px, t, why if not part else why + " (after partial)", j, part
        if not trail:
            if (d > 0 and h >= tgt) or (d < 0 and l <= tgt):
                return max(o, tgt) if d > 0 else min(o, tgt), t, "target", j, None
        else:
            fav = (h - e) if d > 0 else (e - l)
            if p["trail"] == "r":
                best = max(best, h) if d > 0 else min(best, l)
            if pct and not part and fav >= p["partR"] * risk:
                px = e + d * p["partR"] * risk
                got, left, part = got + pct * px, left - pct, [t, px]
            if p["beR"] and not be_done and fav >= p["beR"] * risk:
                st, be_done = (max(st, e) if d > 0 else min(st, e)), True
            if p["trail"] == "swing" and j - j0 + 1 >= p["trailN"]:
                w = B[j - p["trailN"] + 1:j + 1]
                lv = min(b[3] for b in w) if d > 0 else max(b[2] for b in w)
                if (lv - st) * d > 0 and (be_done or not p["beR"]):
                    st = lv
            elif p["trail"] == "r" and (be_done or not p["beR"]):
                best = max(best, h) if d > 0 else min(best, l)
                lv = best - d * p["trailR"] * risk
                if (lv - st) * d > 0:
                    st = lv
            elif p["trail"] == "ema15" and E.get(t) and (be_done or not p["beR"]) and (c - E[t][0][0]) * d < 0:
                return got + left * c, t, "trail (EMA)" + (" (after partial)" if part else ""), j, part
        if t[:16] >= flat:
            return got + left * c, t, "time", j, part
    return got + left * B[-1][4], B[-1][0], "close", len(B) - 1, part


def baseline(B, x, rnd, p, E, flat, last, lo, n=3, any_side=False):
    """The same stop distance and exit management from random candles between index lo and the flat time: % gross.
    any_side: a random side for each (the whole rule's baseline), else the trade's side (the entry timing's)."""
    d0 = 1 if x["side"] == "long" else -1
    idx = [k for k in range(lo, len(B) - 2) if B[k][0][:16] < last and not ("16:00" <= B[k][0][11:16] < "18:00")]
    out = []
    for _ in range(n):
        if not idx:
            break
        k = rnd.choice(idx)
        d = rnd.choice((1, -1)) if any_side else d0
        e = B[k][4]
        risk = x["stopDist"]
        tgt = e + d * x["tgtDist"]
        px, _, _, _, _ = manage(B, k + 1, d, e, e - d * risk, tgt, risk, p, E, flat)
        out.append(d * (px / e - 1) * 100)
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
    rnd, rnd2 = random.Random(59), random.Random(67)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {p['tf']}-minute candles + the 15 / 30 / 60-minute EMAs")
        rows, Fall, fi, E = _series(sym, p)
        for d, on, R, F, V in rows:
            for x in day_trades(d, on, R, F, Fall, fi, E, p):
                x.update(s=sym, d=d, cell=None)
                trades.append(x)
                lo = 0 if p["hours"] == "all" else next((i for i, b in enumerate(F) if b[0] >= f"{d}T09:35"), len(F))
                base.append((d, x["side"], baseline(F, x, rnd, p, E, f"{d}T15:55", f"{d}T{p['lastEntry']}", lo),
                             baseline(F, x, rnd2, p, E, f"{d}T15:55", f"{d}T{p['lastEntry']}", lo, any_side=True)))
    return trades, base


def decorate(x, p):
    x["tod"] = K.tod(x["in"]) if x.get("ses", True) else "overnight"
    x["wd"] = K.weekday(x["d"])
    x["ses"] = "NYSE" if x.get("ses", True) else "overnight"
    x["al"] = f"{x.get('al', 0)} of 3"
    x["as"] = "with the bias" if x.get("as") else "against / flat"
    g = x.get("gap") or 0
    x["gap"] = "under 5 pts" if g < 5 else "5-15 pts" if g < 15 else "15+ pts"
    for k in ("tb", "tr"):
        v = x.get(k)
        x[k] = "—" if v is None else "under 0.5×" if v < 0.5 else "0.5-1×" if v < 1 else "1-1.5×" if v < 1.5 else "1.5-2.5×" if v < 2.5 else "2.5×+"
    v = x.get("bp")
    x["bp"] = "—" if v is None else "under 40%" if v < 40 else "40-60%" if v < 60 else "60-80%" if v < 80 else "80%+ (small wicks)"


def overlay(sub, p, sym, start, end):
    """The three EMAs, each session's Asian range and every inversion: taken, or why not."""
    rows, Fall, fi, E = _series(sym, {**p, "_start": start})
    cols = {15: "#5FD4C4", 30: "#E7C35A", 60: "#E07AE0"}
    lines = {n: {"name": f"EMA 20 ({n}m)", "color": cols[n], "pts": []} for n in EMAS}
    rng, notes, strip = [], [], []
    for d, on, R, F, V in rows:
        if d > end:
            continue
        for b in F:
            for n, (em, up, dn) in zip(EMAS, E.get(b[0], [])):
                lines[n]["pts"].append([b[0], round(em, 2)])
            if E.get(b[0]):
                strip.append([b[0], bias_at(E[b[0]], b[4], p)])
        a = [b for b in on if b[0][11:16] >= "18:00" or b[0][11:16] < "02:00"]
        if a:
            rng.append({"day": d, "hi": max(b[2] for b in a), "lo": min(b[3] for b in a), "t0": a[0][0], "t1": a[-1][0], "t2": f"{d}T09:30", "label": "Asia"})
        day_trades(d, on, R, F, Fall, fi, E, p, notes)
    return {"lines": list(lines.values()), "ranges": rng, "notes": [x for x in notes if x["ok"]],         # entries only, no skip reasons
            "strip": {"name": "EMA bias (" + ("15 / 30 / 60" if p["bias"] == "all3" else "hourly") + "): long / short / none", "pts": strip}}
