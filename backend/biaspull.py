"""EMA bias pullback (no FVG): the control for the FVG inversion strategy.

Same bias, hours, Asian check and exit management as the FVG inversion (ifvg); only the entry differs:
  Long (short = the mirror): the bias is long at the candle; the `pbMin`+ candles before it closed down (the pullback);
  this candle closes up and above the previous candle's high: buy at that close. Stop under the pullback's low
  (- `bufTicks` ticks); skipped when the risk is under `minRisk` ticks.
If it does as well as the FVG inversion, the FVG step adds nothing: the edge is the bias + the exit."""
import random

import ifvg
import sessionkit as K

DEFAULT = {**ifvg.DEFAULT, "pbMin": 2, "minRisk": 8, "exit": "trail", "tgt": "r", "tgtR": 2.0}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("al", "EMAs agreeing"), ("ses", "Session"), ("pb", "Pullback candles")]
KEYS = ("tf", "bias", "asia", "bufTicks", "hours", "lastEntry", "maxDay", "side", "exit", "tgtR", "beR", "partR", "partPct", "trail", "trailN", "trailR",
        "days", "tickers")


def clean(p):
    p = {**DEFAULT, **(p or {})}
    q = ifvg.clean({k: p[k] for k in KEYS if k in p})
    out = {k: q[k] for k in KEYS}
    out["setup"] = "biaspull"
    K.num(p, out, (("pbMin", 1, 20, 2), ("minRisk", 0, 400, 8)), ("pbMin", "minRisk"))
    return out


def describe(p):
    q = {**ifvg.clean({k: p[k] for k in KEYS}), "exit": p["exit"]}
    b = "the hourly 20 EMA" if p["bias"] == "h1" else "the 15 / 30 / 60-minute 20 EMAs"
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    hrs = "overnight + NYSE" if p["hours"] == "all" else "NYSE session"
    ex = f", target {p['tgtR']:g}R" if p["exit"] == "target" else ifvg._exit_txt(q)
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute, {hrs}, no FVG (the control): bias from {b}; after {p['pbMin']}+ candles against it, the first "
            f"close back with it above / under the previous candle: enter at that close ({side}, before {p['lastEntry']}), stop past the pullback "
            f"+ {p['bufTicks']} tick(s){ex}, at most {p['maxDay']} a session, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def day_trades(day, on, F, E, p):
    asia = ifvg.asia_of(on)
    out, k, buf, rth0 = [], p["pbMin"] + 1, p["bufTicks"] * K.TICK, f"{day}T09:30"
    while k < len(F) and len(out) < p["maxDay"]:
        t, o, h, l, c = F[k]
        today = t[:10] == day
        if today and K.end_of(t, p["tf"])[11:16] > p["lastEntry"]:
            break
        if p["hours"] == "rth" and (t < rth0 or K.end_of(t, p["tf"])[11:16] < "09:35"):
            k += 1
            continue
        if "16:00" <= t[11:16] < "18:00":
            k += 1
            continue
        row = E.get(t)
        d = ifvg.bias_at(row, c, p) if row else 0
        if not d or (p["side"] == "long" and d < 0) or (p["side"] == "short" and d > 0):
            k += 1
            continue
        pb = 0
        while k - 1 - pb >= 0 and (F[k - 1 - pb][4] - F[k - 1 - pb][1]) * d < 0:
            pb += 1                                           # candles closing against the bias, right before this one
        prev = F[k - 1]
        go = pb >= p["pbMin"] and (c - o) * d > 0 and ((c > prev[2]) if d > 0 else (c < prev[3]))
        if go and asia and p["asia"] != "none":
            ad = 1 if asia["c"] > asia["o"] else -1 if asia["c"] < asia["o"] else 0
            am = 1 if c > (asia["h"] + asia["l"]) / 2 else -1
            go = (ad if p["asia"] == "dir" else am) == d
        if not go:
            k += 1
            continue
        seg = F[k - pb:k + 1]
        stop = (min(b[3] for b in seg) - buf) if d > 0 else (max(b[2] for b in seg) + buf)
        risk = (c - stop) * d
        if risk <= 0 or risk < p["minRisk"] * K.TICK:
            k += 1
            continue
        e, tgt = c, c + d * p["tgtR"] * risk
        x, ot, ex, j, part = ifvg.manage(F, k + 1, d, e, stop, tgt, risk, p, E, f"{day}T15:55")
        al = sum(1 for em, up, dn in row if (c - em) * d > 0 and (up if d > 0 else dn))
        out.append({"in": K.end_of(t, p["tf"]), "out": ot, "e": e, "x": x, "why": ex, "side": "long" if d > 0 else "short", "stop": stop,
                    "tgt": tgt if p["exit"] == "target" else None, "stopDist": risk, "tgtDist": abs(tgt - e), "rt": F[k - pb][0], "cf": t,
                    "al": al, "ses": t >= rth0, "pb": pb, "part": part, "pct": d * (x / e - 1) * 100, "r": d * (x - e) / risk})
        k = j + 1
    return out


def all_trades(sub, p, cells=None, prog=None):
    p = {**p, "gapFrom": "08:00", "fvgMax": 24, "minGap": 0, "stop": "swing", "tgt": "r", "minRR": 0, "pivLen": 3, "liqLook": 150}
    trades, base = [], []
    rnd, rnd2 = random.Random(61), random.Random(71)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {p['tf']}-minute candles + the 15 / 30 / 60-minute EMAs")
        rows, Fall, fi, E = ifvg._series(sym, p)
        for d, on, R, F, V in rows:
            for x in day_trades(d, on, F, E, p):
                x.update(s=sym, d=d, cell=None)
                trades.append(x)
                lo = 0 if p["hours"] == "all" else next((i for i, b in enumerate(F) if b[0] >= f"{d}T09:35"), len(F))
                base.append((d, x["side"], ifvg.baseline(F, x, rnd, p, E, f"{d}T15:55", f"{d}T{p['lastEntry']}", lo),
                             ifvg.baseline(F, x, rnd2, p, E, f"{d}T15:55", f"{d}T{p['lastEntry']}", lo, any_side=True)))
    return trades, base


def decorate(x, p):
    ifvg.decorate(x, p)
    n = x.get("pb") or 0
    x["pb"] = f"{n}" if n < 5 else "5+"


def overlay(sub, p, sym, start, end):
    ov = ifvg.overlay(sub, {**ifvg.clean({k: p[k] for k in KEYS}), "asia": p["asia"]}, sym, start, end)
    ov["notes"] = []                                      # the FVG inversion's own entries: not this strategy's
    return ov
