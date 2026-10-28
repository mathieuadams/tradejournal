"""ICT Silver Bullet (ported from the user's Pine v6 strategy, bar for bar), on QQQ / SPY 1 or 5-minute candles.

  - Liquidity: confirmed swing highs / lows (pivot of `pivLen` bars each side) = buy-side / sell-side liquidity; the most
    recent one on each side is tracked until swept.
  - Sweep sets the bias: trading through the swing high -> short bias, through the swing low -> long bias (both on one
    bar: nothing). The sweep extreme keeps extending; the bias expires after `expiry` bars.
  - Sessions (New York): 10:00-11:00 and 14:00-15:00. (03:00-04:00 London needs futures: stocks have no candles then.)
  - Entry: in a session, flat, after the sweep bar: the first fair value gap in the bias direction (bull: low > high
    two bars back; bear: high < low two bars back) -> enter at that bar's close.
  - Stop beyond the sweep extreme or the gap's first candle, whichever is further, + `bufTicks` x $0.01;
    target `rr` x the risk. The bias resets after each entry; one position at a time.
  - Exits: the stop / the target (same candle: the stop). The Pine script has no session exit; on stocks the default
    here closes at 15:55 (`flatEod`), switch it off to carry overnight like the script (an open gap fills at the open)."""
import datetime as dt

DEFAULT = {"tickers": ["QQQ", "SPY"], "tf": 1, "pivLen": 3, "expiry": 30, "rr": 2.0, "bufTicks": 2, "flatEod": True, "days": 250, "side": "both"}
ROW = ("rr", (1.5, 2.0, 3.0), "R target")
COL = ("pivLen", (2, 3, 5))
COL_NAMES = {2: "swing length 2", 3: "swing length 3", 5: "swing length 5"}
FACTORS = [("side", "Side"), ("sess", "Session"), ("zone", "Premium / discount (dealing range)"), ("wait", "Bars from the sweep to the entry"),
           ("risk", "Stop distance"), ("s", "Ticker"), ("wd", "Weekday")]
SESS = {10: "NY AM (10-11)", 14: "NY PM (14-15)"}


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "silver", "tf": 5 if str(p.get("tf")) == "5" else 1, "flatEod": p.get("flatEod") not in (False, "false", 0, "0"),
           "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    for k, lo, hi in (("pivLen", 1, 20), ("expiry", 1, 300), ("rr", 0.5, 10), ("bufTicks", 0, 100), ("days", 20, 800)):
        try:
            out[k] = max(lo, min(hi, float(p.get(k))))
        except (TypeError, ValueError):
            out[k] = DEFAULT[k]
    for k in ("pivLen", "expiry", "bufTicks", "days"):
        out[k] = int(out[k])
    tk = p.get("tickers") or DEFAULT["tickers"]
    tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
    out["tickers"] = [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:6] or list(DEFAULT["tickers"])
    return out


def describe(p):
    return (f"ICT Silver Bullet on {', '.join(p['tickers'])} {p['tf']}-minute: a sweep of the last swing high / low (swing {p['pivLen']}) sets the bias "
            f"for {p['expiry']} bars; in 10-11 or 14-15 ET the first fair value gap the bias way enters at the close; stop beyond the sweep / the gap's "
            f"first candle + {p['bufTicks']} ticks, target {p['rr']:g}R" + (", flat at 15:55" if p["flatEod"] else ", held overnight if needed"))


def tickers(sub, p):
    return list(p["tickers"])


def run(C, p, trace=None):
    """C: continuous candles [(t, o, h, l, c)] (regular hours, every day in a row). The trades, as the Pine script takes them."""
    L, n = p["pivLen"], len(C)
    buf = p["bufTicks"] * 0.01
    lastPH = lastPL = None
    rTop = rBot = None
    bias, biasBar, sweepExt = 0, 0, None
    sweepT = sweepLvl = None
    pos = None
    out = []
    H = [c[2] for c in C]
    Lo = [c[3] for c in C]
    for i in range(n):
        t, o, h, l, c = C[i]
        if pos:                                        # the open position: stop / target from the bar after the entry
            x = why = None
            if pos["d"] > 0:
                if l <= pos["stop"]:
                    x, why = (o if o < pos["stop"] else pos["stop"]), "stop"
                elif h >= pos["tgt"]:
                    x, why = (o if o > pos["tgt"] else pos["tgt"]), "target"
            else:
                if h >= pos["stop"]:
                    x, why = (o if o > pos["stop"] else pos["stop"]), "stop"
                elif l <= pos["tgt"]:
                    x, why = (o if o < pos["tgt"] else pos["tgt"]), "target"
            if x is None and p["flatEod"] and t[11:16] >= "15:55":
                x, why = c, "close"
            if x is not None:
                pos.update(x=x, out=t, why=why)
                out.append(pos)
                pos = None
        ph = pl = None                                 # pivots confirmed on this bar (centre i - L)
        if i >= 2 * L:
            cI = i - L
            if H[cI] > max(H[i - 2 * L:cI]) and H[cI] >= max(H[cI + 1:i + 1]):
                ph = H[cI]
            if Lo[cI] < min(Lo[i - 2 * L:cI]) and Lo[cI] <= min(Lo[cI + 1:i + 1]):
                pl = Lo[cI]
        if ph is not None:
            lastPH = ph
            rTop = ph
            if rBot is not None and rBot >= rTop:
                rBot = min(Lo[max(0, i - L):i + 1])
        if pl is not None:
            lastPL = pl
            rBot = pl
            if rTop is not None and rTop <= rBot:
                rTop = max(H[max(0, i - L):i + 1])
        if rTop is not None and h > rTop:
            rTop = h
        if rBot is not None and l < rBot:
            rBot = l
        sH = lastPH is not None and h > lastPH
        sL = lastPL is not None and l < lastPL
        if trace is not None:
            trace.append((t, lastPH, lastPL))
        if sH and not sL:
            sweepT, sweepLvl = t, lastPH
            bias, biasBar, sweepExt, lastPH = -1, i, h, None
        elif sL and not sH:
            sweepT, sweepLvl = t, lastPL
            bias, biasBar, sweepExt, lastPL = 1, i, l, None
        elif bias == -1:
            sweepExt = max(sweepExt, h)
        elif bias == 1:
            sweepExt = min(sweepExt, l)
        if bias != 0 and i - biasBar > p["expiry"]:
            bias = 0
        hr = int(t[11:13])
        if hr in SESS and pos is None and i > biasBar and i >= 2 and t[11:16] < "15:55":
            bull, bear = l > H[i - 2], h < Lo[i - 2]
            zone = None
            if rTop is not None and rBot is not None and rTop > rBot:
                zone = "discount" if c < (rTop + rBot) / 2 else "premium"
            if bias == 1 and bull and p["side"] in ("long", "both"):
                stop = min(sweepExt, Lo[i - 2]) - buf
                risk = c - stop
                if risk > 0.01:
                    pos = {"d": 1, "e": c, "stop": stop, "tgt": c + risk * p["rr"], "in": t, "R": risk, "k": i, "sess": SESS[hr], "zone": zone,
                           "sweepBars": i - biasBar, "sweepT": sweepT, "sweepLvl": sweepLvl, "fvgT": C[i - 2][0], "fvgBot": H[i - 2], "fvgTop": l}
                    bias = 0
            elif bias == -1 and bear and p["side"] in ("short", "both"):
                stop = max(sweepExt, H[i - 2]) + buf
                risk = stop - c
                if risk > 0.01:
                    pos = {"d": -1, "e": c, "stop": stop, "tgt": c - risk * p["rr"], "in": t, "R": risk, "k": i, "sess": SESS[hr], "zone": zone,
                           "sweepBars": i - biasBar, "sweepT": sweepT, "sweepLvl": sweepLvl, "fvgT": C[i - 2][0], "fvgBot": h, "fvgTop": Lo[i - 2]}
                    bias = 0
    if pos:
        pos.update(x=C[-1][4], out=C[-1][0], why="close")
        out.append(pos)
    res = []
    for x in out:
        d = x["d"]
        res.append({**x, "side": "long" if d > 0 else "short", "pct": d * (x["x"] / x["e"] - 1) * 100, "r": d * (x["x"] - x["e"]) / x["R"],
                    "tgtDist": abs(x["tgt"] - x["e"]), "stopDist": abs(x["e"] - x["stop"]), "touch": x["in"]})
    return res


def all_trades(sub, p, cells=None, prog=None):
    import random
    import range50
    trades, base = [], []
    rnd = random.Random(9)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {p['days']} days of {p['tf']}-minute candles")
        by = range50.bars(sub, sym, p["days"], p["tf"])
        C = [c for d in sorted(by) for c in by[d]]
        for x in run(C, p):
            d = x["in"][:10]
            x.update(s=sym, d=d, cell=(cells or {}).get(d))
            trades.append(x)
            base.append((d, x["side"], range50.baseline(by.get(d) or [], x, rnd)))
    return trades, base


def decorate(x, p):
    x["wd"] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.date.fromisoformat(x["d"]).weekday()]
    w = x["sweepBars"]
    x["wait"] = "1-5 bars" if w <= 5 else "6-15 bars" if w <= 15 else "16+ bars"
    rp = x["R"] / x["e"] * 100 if x["e"] else 0
    x["risk"] = "under 0.15%" if rp < 0.15 else "0.15-0.3%" if rp < 0.3 else "0.3%+"
    x["zone"] = x.get("zone") or "no range yet"
    if x["side"] == "long":
        x["zone"] = "long from " + x["zone"]
    else:
        x["zone"] = "short from " + x["zone"]


def coverage(sub, p, prog):
    import range50
    return range50.coverage(sub, p, prog)
