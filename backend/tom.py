"""Turn of the month: hold the index from the end of one month into the start of the next (held across days).

  Enter at the 16:00 close of the `entryDay` trading day from the month's end (-1 = the last day, -2 = the one
  before); out at the 16:00 close of the `exitDay` trading day of the new month (1 = its first). Optional stop:
  `stopAtr` x the 14-day average daily range below the entry (checked on each day's regular-hours low; filled at the
  open when it gaps through), 0 = none (R then counts 1x that range). Optional trend filter (`trend`): above the 50
  or 200-day average close. Long only by default (`side`)."""
import datetime as dt
import random

import nightkit as N

DEFAULT = {"tickers": ["NQ"], "tf": "1D", "entryDay": -1, "exitDay": 3, "stopAtr": 0.0, "trend": "none", "side": "long", "days": 500}
FACTORS = [("side", "Side"), ("mo", "Month"), ("tr", "Trend (vs the 50-day average)")]


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "tom", "tf": "1D", "trend": p.get("trend") if p.get("trend") in ("none", "sma50", "sma200") else "none",
           "side": p.get("side") if p.get("side") in ("long", "short") else "long", "tickers": N.K.tickers_of(p)}
    N.K.num(p, out, (("entryDay", -5, -1, -1), ("exitDay", 1, 10, 3), ("stopAtr", 0, 10, 0.0), ("days", 60, 5000, 500)), ("entryDay", "exitDay", "days"))
    return out


def describe(p):
    e = "last" if p["entryDay"] == -1 else f"{-p['entryDay']}th-to-last" if p["entryDay"] < -3 else {-2: "2nd-to-last", -3: "3rd-to-last"}[p["entryDay"]]
    tr = {"none": "", "sma50": ", only above the 50-day average", "sma200": ", only above the 200-day average"}[p["trend"]]
    return (f"{', '.join(p['tickers'])} daily: {'buy' if p['side'] == 'long' else 'sell'} at the 16:00 close of the month's {e} trading day{tr}, out at the close of "
            f"the new month's trading day {p['exitDay']}" + (f", stop {p['stopAtr']:g}× the daily average range" if p["stopAtr"] else ", no stop"))


def tickers(sub, p):
    return list(p["tickers"])


def candles(sym, p):
    import futdata
    D = futdata.daily(sym)
    if p.get("_start"):
        return [d for d in D if d[0] >= (dt.date.fromisoformat(p["_start"]) - dt.timedelta(days=320)).isoformat()]
    return D[-(p["days"] + 220):]


def run(D, p, keep):
    out = []
    trs, closes = [], []
    dd = 1 if p["side"] == "long" else -1
    for i, x in enumerate(D):
        pc = D[i - 1][4] if i else x[4]
        trs.append(max(x[2], pc) - min(x[3], pc))
        closes.append(x[4])
    month_idx = {}
    for i, x in enumerate(D):
        month_idx.setdefault(x[0][:7], []).append(i)
    months = sorted(month_idx)
    for mi, m in enumerate(months[:-1]):
        idx, nxt = month_idx[m], month_idx[months[mi + 1]]
        if len(idx) < -p["entryDay"] or len(nxt) < p["exitDay"]:
            continue
        i, j = idx[p["entryDay"]], nxt[p["exitDay"] - 1]
        if D[i][0] not in keep or i < 20:
            continue
        sma = lambda n: sum(closes[i - n + 1:i + 1]) / n if i >= n - 1 else None
        if p["trend"] != "none":
            s = sma(50 if p["trend"] == "sma50" else 200)
            if s is None or (closes[i] - s) * dd <= 0:
                continue
        atr = sum(trs[i - 13:i + 1]) / 14
        e = closes[i]
        risk = (p["stopAtr"] or 1) * atr
        stop = e - dd * risk if p["stopAtr"] else None
        xp, xi, why = closes[j], j, "time"
        if stop is not None:
            for k in range(i + 1, j + 1):
                o, h, l = D[k][1], D[k][2], D[k][3]
                if (dd > 0 and l <= stop) or (dd < 0 and h >= stop):
                    xp, xi, why = (min(o, stop) if dd > 0 else max(o, stop)), k, "stop"
                    break
        s50 = sma(50)
        out.append({"in": D[i][0] + "T16:00", "out": D[xi][0] + "T16:00", "e": e, "x": xp, "why": why, "side": "long" if dd > 0 else "short", "stop": stop,
                    "tgt": None, "stopDist": risk, "tgtDist": None, "hold": xi - i, "mo": D[j][0][5:7], "tr": None if s50 is None else e > s50,
                    "pct": dd * (xp / e - 1) * 100, "r": dd * (xp - e) / risk, "_i": i})
    return out


def all_trades(sub, p, cells=None, prog=None):
    trades, base = [], []
    rnd = random.Random(113)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: daily candles")
        D = candles(sym, p)
        keep = set(x[0] for x in D if x[0] >= p["_start"]) if p.get("_start") else set(x[0] for x in D[-p["days"]:])
        for x in run(D, p, keep):
            x.pop("_i", None)
            d = x["in"][:10]
            x.update(s=sym, d=d, cell=(cells or {}).get(d))
            trades.append(x)
            h, sgn, b = max(1, x["hold"]), (1 if x["side"] == "long" else -1), []
            for _ in range(3):                            # the same side and holding time from random days
                if len(D) > h + 2:
                    k = rnd.randrange(1, len(D) - h - 1)
                    b.append(sgn * (D[k + h][4] / D[k][4] - 1) * 100)
            base.append((d, x["side"], b))
    return trades, base


def decorate(x, p):
    x["wd"] = N.K.weekday(x["d"])
    x["mo"] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][int(x.get("mo") or 1) - 1]
    x["tr"] = "—" if x.get("tr") is None else "above" if x["tr"] else "below"


def overlay(sub, p, sym, start, end):
    return {"lines": [], "ranges": []}
