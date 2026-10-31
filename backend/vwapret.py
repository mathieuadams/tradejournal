"""VWAP retest, overnight included, on NQ / ES (whole Globex session, VWAP anchored at 18:00 ET).

Short (long = the mirror):
  1. Below VWAP: the `trend` candles before the touch all closed under the VWAP.
  2. The touch: a green candle that opened under the VWAP and whose high reaches it.
  3. The entry: the next candle is red and closes under the VWAP: sell at that close.
  4. Stop: above the higher high of the two candles (+ `bufTicks` ticks). Target: `tgtR` x the risk.
  5. Flat at `flatAt` ET on the session day; at most `maxDay` trades per session; one position at a time;
     `hours`: all (overnight + day), on (overnight only: entries 18:00-09:30), rth (09:30-16:00).
Same candle through the stop and the target: the stop counts."""
import datetime as dt
import random

DEFAULT = {"tickers": ["NQ"], "tf": 5, "trend": 3, "tgtR": 2.0, "bufTicks": 2, "maxDay": 3, "hours": "all", "flatAt": "15:55",
           "side": "both", "days": 250}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("dist", "Distance from VWAP at entry"), ("risk", "Risk (points)")]
TICK = 0.25


def clean(p):
    p = {**DEFAULT, **(p or {})}
    tf = int(p.get("tf") or 5) if str(p.get("tf")) in ("1", "5", "15") else 5
    out = {"setup": "vwapret", "tf": tf, "hours": p.get("hours") if p.get("hours") in ("all", "on", "rth") else "all",
           "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    for k, lo, hi in (("trend", 0, 50), ("tgtR", 0.5, 10), ("bufTicks", 0, 40), ("maxDay", 1, 20), ("days", 20, 3000)):
        try:
            out[k] = max(lo, min(hi, float(p.get(k))))
        except (TypeError, ValueError):
            out[k] = DEFAULT[k]
    for k in ("trend", "bufTicks", "maxDay", "days"):
        out[k] = int(out[k])
    fa = str(p.get("flatAt") or "15:55")
    out["flatAt"] = fa if len(fa) == 5 and fa[2] == ":" and "09:35" <= fa <= "16:55" else "15:55"
    tk = p.get("tickers") or DEFAULT["tickers"]
    tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
    out["tickers"] = [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:2] or ["NQ"]
    return out


def describe(p):
    hrs = {"all": "overnight and day", "on": "overnight only", "rth": "regular hours only"}[p["hours"]]
    side = {"long": "long only", "short": "short only", "both": "both sides"}[p["side"]]
    return (f"{', '.join(p['tickers'])} {p['tf']}-minute VWAP retest ({hrs}, {side}): after {p['trend']} closes on one side, a candle against "
            f"the move touches the VWAP; the next candle closes back away from it in the move's direction: enter at that close, stop past "
            f"the two candles + {p['bufTicks']} tick(s), target {p['tgtR']:g}R, flat at {p['flatAt']}, at most {p['maxDay']} a session")


def tickers(sub, p):
    return list(p["tickers"])


def candles(sym, p, start=None):
    """{session: [(t, o, h, l, c, vwap at the candle's close)]}: whole Globex sessions, tf-minute candles."""
    import futdata
    by = futdata.sessions(sym, None if start else p["days"], rth=False, start=start)
    out = {}
    for d in sorted(by):
        pv = vv = 0.0
        vw = {}
        for t, o, h, l, c, v in by[d]:                 # the VWAP from 1-minute candles, read at each tf candle's last minute
            pv += (h + l + c) / 3 * v
            vv += v
            vw[t] = pv / vv if vv else c
        cs, last = [], None
        for b in futdata.agg(by[d], p["tf"]):
            m = int(b[0][11:13]) * 60 + int(b[0][14:16])
            for k in range(p["tf"] - 1, -1, -1):          # the last 1-minute candle inside this candle
                mm = m + k
                key = f"{b[0][:11]}{mm // 60 % 24:02d}:{mm % 60:02d}"
                if key in vw:
                    last = vw[key]
                    break
            cs.append((b[0], b[1], b[2], b[3], b[4], last if last is not None else b[4]))
        out[d] = cs
    return out


def _in_hours(t, day, hours):
    rth = t[:10] == day and "09:30" <= t[11:16] < "16:00"
    return hours == "all" or (hours == "rth" and rth) or (hours == "on" and not rth and not (t[:10] == day and t[11:16] >= "16:00"))


def _end(t, tf):
    m = int(t[11:13]) * 60 + int(t[14:16]) + tf - 1
    return f"{t[:11]}{m // 60 % 24:02d}:{m % 60:02d}"


def session(B, day, p):
    """The trades of one session. B: [(t, o, h, l, c, vwap)]."""
    out, k, n, done = [], 1, len(B), 0
    flat = f"{day}T{p['flatAt']}"
    while k < n - 1 and done < p["maxDay"]:
        t, o, h, l, c, vw = B[k]
        if t >= flat:
            break
        nt, no, nh, nl, nc, nvw = B[k + 1]
        side = None
        if k >= p["trend"]:
            below = all(B[j][4] < B[j][5] for j in range(k - p["trend"], k))
            above = all(B[j][4] > B[j][5] for j in range(k - p["trend"], k))
            if p["side"] in ("short", "both") and below and c > o and o < vw <= h and nc < no and nc < nvw:
                side = "short"
            elif p["side"] in ("long", "both") and above and c < o and o > vw >= l and nc > no and nc > nvw:
                side = "long"
        if not side or not _in_hours(_end(nt, p["tf"]), day, p["hours"]) or _end(nt, p["tf"]) >= flat:
            k += 1
            continue
        d = -1 if side == "short" else 1
        e = nc
        stop = (max(h, nh) + p["bufTicks"] * TICK) if d < 0 else (min(l, nl) - p["bufTicks"] * TICK)
        R = abs(e - stop)
        if R <= 0:
            k += 1
            continue
        tgt = e + d * p["tgtR"] * R
        x = why = None
        j = k + 2
        while j < n:
            tj, oj, hj, lj, cj, _ = B[j]
            if (d < 0 and hj >= stop) or (d > 0 and lj <= stop):
                x, why = ((max(oj, stop) if d < 0 else min(oj, stop)), "stop")
            elif (d < 0 and lj <= tgt) or (d > 0 and hj >= tgt):
                x, why = ((min(oj, tgt) if d < 0 else max(oj, tgt)), "target")
            elif tj >= flat:
                x, why = oj, "time"
            if x is not None:
                break
            j += 1
        if x is None:
            j, x, why = n - 1, B[-1][4], "close"
        out.append({"in": _end(nt, p["tf"]), "out": B[j][0], "e": e, "x": x, "why": why, "side": side, "stop": stop, "tgt": tgt,
                    "stopDist": R, "tgtDist": p["tgtR"] * R, "rt": t, "cf": nt, "dist": abs(e - nvw), "k": k + 1,
                    "pct": d * (x / e - 1) * 100, "r": d * (x - e) / R})
        done += 1
        k = j + 1                                      # one position at a time: look again after the exit
    return out


def baseline(B, tr, rnd, flat, n=3):
    """The same bracket from random candles of the session: % gross."""
    out, d = [], 1 if tr["side"] == "long" else -1
    m = len(B)
    for _ in range(n):
        if m < 5:
            break
        k = rnd.randrange(1, m - 2)
        if B[k][0] >= flat:
            continue
        e = B[k][4]
        T, S = e + d * tr["tgtDist"], e - d * tr["stopDist"]
        x = None
        for j in range(k + 1, m):
            tj, oj, hj, lj, cj, _ = B[j]
            if (d > 0 and lj <= S) or (d < 0 and hj >= S):
                x = S
            elif (d > 0 and hj >= T) or (d < 0 and lj <= T):
                x = T
            elif tj >= flat:
                x = oj
            if x is not None:
                break
        out.append(d * ((x if x is not None else B[-1][4]) / e - 1) * 100)
    return out


def all_trades(sub, p, cells=None, prog=None):
    trades, base = [], []
    rnd = random.Random(29)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {p['tf']}-minute candles of the whole session, VWAP from 18:00")
        by = candles(sym, p, p.get("_start"))
        for d in sorted(by):
            B = by[d]
            if len(B) < 20:
                continue
            for x in session(B, d, p):
                x.update(s=sym, d=d, cell=(cells or {}).get(d))
                trades.append(x)
                base.append((d, x["side"], baseline(B, x, rnd, f"{d}T{p['flatAt']}")))
    return trades, base


def decorate(x, p):
    hm, day = x["in"][11:16], x["in"][:10]
    x["tod"] = ("overnight" if day < x["d"] or hm < "09:30" else "09:30-10:30" if hm < "10:30" else "10:30-12:00" if hm < "12:00"
                else "12:00-14:00" if hm < "14:00" else "14:00-16:00")
    x["wd"] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.date.fromisoformat(x["d"]).weekday()]
    dd = x.get("dist") or 0
    x["dist"] = "under 5 pts" if dd < 5 else "5-15 pts" if dd < 15 else "15+ pts"
    rk = x.get("stopDist") or 0
    x["risk"] = "under 10 pts" if rk < 10 else "10-25 pts" if rk < 25 else "25+ pts"
