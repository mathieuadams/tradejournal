"""Squeeze breakout: Bollinger Bands inside the Keltner Channels, then the first close outside the bands.

  On `tf`-minute candles (full session): the bands = the 20-candle average close +/- 2 standard deviations; the
  channels = the same average +/- `kc` x the 20-candle average range. A squeeze = the bands inside the channels for at
  least `minBars` candles in a row. When it ends, the first close outside the bands (within `wait` candles) is the trade,
  in that direction (with the EMA bias if set): enter at that close; stop at the 20-candle average (`stop` mid) or the
  other band (opp) (+ `bufTicks`); target `tgtR` x the risk or the managed exit. `hours`: rth (09:35-`lastEntry`) or all.
  One position at a time, at most `maxDay` a session, flat at 15:55."""
import math

import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": 5, "kc": 1.5, "minBars": 6, "wait": 6, "stop": "mid", "tgtR": 2.0, "hours": "rth", "lastEntry": "15:00", "maxDay": 3,
           "bias": "none", "side": "both", "exit": "target", "bufTicks": 0, "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("sq", "Squeeze length (candles)"), ("ses", "Session"), ("wb", "With the EMA bias")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "squeeze", "tf": 1 if str(p.get("tf")) == "1" else 15 if str(p.get("tf")) == "15" else 5, "stop": p.get("stop") if p.get("stop") in ("mid", "opp") else "mid",
           "hours": p.get("hours") if p.get("hours") in ("rth", "all") else "rth", "lastEntry": N.hhmm(p.get("lastEntry"), "15:00", "09:40", "15:50")}
    N.K.num(p, out, (("kc", 0.5, 4, 1.5), ("minBars", 1, 100, 6), ("wait", 1, 50, 6), ("maxDay", 1, 20, 3)), ("minBars", "wait", "maxDay"))
    N.clean_common(p, out, bias="none")
    if p.get("bufTicks") in (None, ""):
        out["bufTicks"] = 0
    return out


def describe(p):
    b = {"all3": ", with the 15 / 30 / 60-minute EMA bias", "h1": ", with the hourly EMA bias", "none": ""}[p["bias"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute ({'overnight + NYSE' if p['hours'] == 'all' else 'NYSE session'}): Bollinger Bands (20, 2) inside the Keltner "
            f"Channels (20, {p['kc']:g}× the average range) for {p['minBars']}+ candles, then the first close outside the bands within {p['wait']}{b}: enter at that close, "
            f"stop at {'the middle' if p['stop'] == 'mid' else 'the other band'} + {p['bufTicks']} tick(s)" + N.exit_txt(p) + f", at most {p['maxDay']} a session, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def bands(F, i, n=20):
    w = F[max(0, i - n + 1):i + 1]
    if len(w) < n:
        return None
    m = sum(b[4] for b in w) / n
    sd = math.sqrt(sum((b[4] - m) ** 2 for b in w) / n)
    ar = sum(b[2] - b[3] for b in w) / n
    return m, sd, ar


def engine(sym, d, on, R, F, VF, E, prev, p):
    G = list(prev or [])[-25:] + list(F)                 # the previous session's end warms the 20-candle bands up
    off = len(G) - len(F)
    out, buf, rth0, flat, last = [], p["bufTicks"] * N.K.TICK, f"{d}T09:30", f"{d}T15:55", f"{d}T{p['lastEntry']}"
    run_, k, armed, length = 0, max(off, 20), None, 0
    while k < len(G) and len(out) < p["maxDay"]:
        t, o, h, l, c = G[k]
        if t[:16] >= last:
            break
        bb = bands(G, k)
        if not bb:
            k += 1
            continue
        m, sd, ar = bb
        if (m + 2 * sd) < (m + p["kc"] * ar):            # the bands inside the channels: squeezed
            run_ += 1
            k += 1
            continue
        if run_ >= p["minBars"]:
            armed, length = k + p["wait"], run_          # the squeeze just ended: watch the next `wait` candles
        run_ = 0
        if armed is None or k >= armed:
            armed = None
            k += 1
            continue
        dd = 1 if c > m + 2 * sd else -1 if c < m - 2 * sd else 0
        ok_h = p["hours"] == "all" or (t >= rth0 and N.K.end_of(t, p["tf"])[11:16] >= "09:35")
        if not dd or not ok_h or "16:00" <= t[11:16] < "18:00" or not N.ok_side(dd, N.bias(E, t, c, p), p):
            k += 1
            continue
        stop = (m if p["stop"] == "mid" else m - dd * 2 * sd) - dd * buf
        risk = (c - stop) * dd
        if risk <= 0:
            k += 1
            continue
        b = N.bias(E, t, c, {**p, "bias": "all3"})
        x, j = N.trade(G, k, dd, c, stop, None, risk, p, E, flat, sq=length, ses=t >= rth0, wb=b == dd)
        out.append((x, (off, last, flat)))
        armed, k = None, j + 1
    return [(x, (0, w[1], w[2])) for x, w in out]


def all_trades(sub, p, cells=None, prog=None):
    return N.run(sub, p, engine, 137, prog, "candles + the squeeze")


def decorate(x, p):
    N.decorate_common(x)
    v = x.get("sq") or 0
    x["sq"] = "under 10" if v < 10 else "10-20" if v < 20 else "20+"
    x["ses"] = "NYSE" if x.get("ses") else "overnight"
    x["wb"] = "with the bias" if x.get("wb") else "against / no bias"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
