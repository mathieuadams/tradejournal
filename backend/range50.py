"""Intraday 50% range retrace (the "A+ NQ scalper" setup), on NQ / ES minute candles.

Long (after a DOWN leg, the mirror for a short after an UP leg):
  1. a leg: a high H, then the lowest low L after it, at least `legPct`% of the price (the range R = H - L);
  2. price retraces to the range's 50% (L + 0.5 R), on a later candle than the one that made L;
  3. it falls back to 25% of the range (L + 0.25 R): buy there (limit), as long as it hasn't made a new low under L
     (the leg then grows: back to 1.) nor gone back over H (a new leg starts);
  4. target: the range's 50%; stop: under L (L - stopBuf x R); flat at 15:55.
The signal is only known from candles already closed; the order is a resting limit, so the entry is the 25% price
itself. Same candle hitting the stop and the target: the stop counts (the worst case)."""
import random

RTH0, RTH1, FLAT, LAST_ENTRY = "09:30", "16:00", "15:55", "15:30"
LEGS = (0.2, 0.35, 0.5, 0.75)                          # % of the price: the leg size grid (NQ 0.35% ~ 85 points)
STOPS = (0.0, 0.1, 0.25)                               # the stop under the low, as a share of the range
DEFAULT = {"tf": 1, "legPct": 0.35, "stopBuf": 0.0, "side": "both", "tickers": ["NQ"], "days": 250, "entryLvl": 0.25, "targetLvl": 0.5}
_CACHE = {}


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "range50", "tf": 5 if str(p.get("tf")) == "5" else 1, "side": p.get("side") if p.get("side") in ("long", "short", "both") else "both"}
    for k, lo, hi in (("legPct", 0.05, 5), ("stopBuf", 0, 1), ("days", 20, 800), ("entryLvl", 0.05, 0.45), ("targetLvl", 0.3, 1.0)):
        try:
            out[k] = max(lo, min(hi, float(p.get(k))))
        except (TypeError, ValueError):
            out[k] = DEFAULT[k]
    out["days"] = int(out["days"])
    tk = p.get("tickers") or DEFAULT["tickers"]
    tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
    out["tickers"] = [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:6] or list(DEFAULT["tickers"])
    return out


def bars(sub, sym, days, tf, start=None):
    """{day: [(t, o, h, l, c)]} regular hours, tf minutes (stored Databento candles)."""
    import futdata
    return futdata.rth(sym, days, tf, start=start)


def _run_long(B, th_pct, stop_buf, ent=0.25, tgt=0.5):
    """The long setups of one session (B: [(t, o, h, l, c)] with prices maybe negated for the short side)."""
    out = []
    H, L, Li, st = None, None, None, "leg"
    Ht = None
    lv = None
    k = 0
    n = len(B)
    while k < n:
        t, o, h, l, c = B[k]
        if st == "leg":
            if H is None or h > H:
                H, L, Li, Ht = h, None, None, t
            elif L is None or l < L:
                L, Li = l, k
            if L is not None and k > Li and (H - L) >= abs(H) * th_pct / 100 and h >= L + 0.5 * (H - L):
                R = H - L
                lv = {"H": H, "L": L, "R": R, "E": L + ent * R, "T": L + tgt * R, "S": L - stop_buf * R, "touch": t, "legT": Ht}
                st = "retraced"
            k += 1
            continue
        if st == "retraced":                               # (a new low under L goes through the 25% first: it fills)
            if h > lv["H"]:                                # back over the top: a new leg
                H, L, Li, st, Ht = h, None, None, "leg", t
                k += 1
                continue
            if l <= lv["E"] and t[11:16] < LAST_ENTRY:     # the limit at 25% fills
                e = lv["E"]
                res = None
                if l <= lv["S"]:                           # the same candle runs through the stop: a loss
                    res = (lv["S"], t, "stop")
                j = k + 1
                while res is None and j < n:
                    tj, oj, hj, lj, cj = B[j]
                    if lj <= lv["S"]:
                        res = (min(oj, lv["S"]) if oj < lv["S"] else lv["S"], tj, "stop")
                    elif hj >= lv["T"]:
                        res = (max(oj, lv["T"]) if oj > lv["T"] else lv["T"], tj, "target")
                    elif tj[11:16] >= FLAT:
                        res = (cj, tj, "close")
                    j += 1
                if res is None:
                    res = (B[-1][4], B[-1][0], "close")
                out.append({**lv, "in": t, "e": e, "x": res[0], "out": res[1], "why": res[2], "k": k})
                H, L, Li, st = None, None, None, "leg"     # a new leg from the exit on
                while k < n and B[k][0] <= res[1]:
                    k += 1
                continue
            if t[11:16] >= LAST_ENTRY:
                st = "done"
            k += 1
            continue
        k += 1
    return out


def setups(B, p):
    """Both sides of one session: [{side, in, out, e, x, pct, r, ...}] (pct gross, % of the entry)."""
    out = []
    if p["side"] in ("long", "both"):
        for x in _run_long(B, p["legPct"], p["stopBuf"], p["entryLvl"], p["targetLvl"]):
            out.append({"side": "long", "in": x["in"], "out": x["out"], "e": x["e"], "x": x["x"], "why": x["why"], "R": x["R"],
                        "pct": (x["x"] / x["e"] - 1) * 100, "r": (x["x"] - x["e"]) / (x["e"] - x["S"]) if x["e"] > x["S"] else None,
                        "touch": x["touch"], "k": x["k"], "tgtDist": x["T"] - x["e"], "stopDist": x["e"] - x["S"], "legT": x.get("legT"),
                        "lv": {"top": x["H"], "bot": x["L"], "mid": x["T"], "ent": x["E"], "stop": x["S"]}})
    if p["side"] in ("short", "both"):
        N = [(t, -o, -l, -h, -c) for t, o, h, l, c in B]
        for x in _run_long(N, p["legPct"], p["stopBuf"], p["entryLvl"], p["targetLvl"]):
            e, xx = -x["e"], -x["x"]
            out.append({"side": "short", "in": x["in"], "out": x["out"], "e": e, "x": xx, "why": x["why"], "R": x["R"],
                        "pct": (e - xx) / e * 100, "r": (x["x"] - x["e"]) / (x["e"] - x["S"]) if x["e"] > x["S"] else None,
                        "touch": x["touch"], "k": x["k"], "tgtDist": x["T"] - x["e"], "stopDist": x["e"] - x["S"], "legT": x.get("legT"),
                        "lv": {"top": -x["L"], "bot": -x["H"], "mid": -x["T"], "ent": -x["E"], "stop": -x["S"]}})
    return out


def baseline(B, tr, rnd, n=3):
    """The same bracket (same distances, same side) entered at random candles of the same session: % gross."""
    out = []
    m = len(B)
    for _ in range(n):
        k = rnd.randrange(0, max(1, m - 5))
        if B[k][0][11:16] >= LAST_ENTRY:
            continue
        e = B[k][4]
        d = 1 if tr["side"] == "long" else -1
        T, S = e + d * tr["tgtDist"], e - d * tr["stopDist"]
        x = None
        for j in range(k + 1, m):
            t, o, h, l, c = B[j]
            if (d > 0 and l <= S) or (d < 0 and h >= S):
                x = S
            elif (d > 0 and h >= T) or (d < 0 and l <= T):
                x = T
            elif t[11:16] >= FLAT:
                x = c
            if x is not None:
                break
        if x is None:
            x = B[-1][4]
        out.append(d * (x / e - 1) * 100)
    return out


def all_trades(sub, p, cells=None, prog=None):
    """Every setup of every session of every ticker, with its market cell, time of day and the random baseline."""
    days_cell = cells or {}
    trades, base = [], []
    rnd = random.Random(7)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {p['days']} days of {p['tf']}-minute candles")
        by = bars(sub, sym, p["days"], p["tf"], p.get("_start"))
        for d in sorted(by):
            B = by[d]
            if len(B) < 30:
                continue
            for x in setups(B, p):
                x.update(s=sym, d=d, cell=days_cell.get(d))
                trades.append(x)
                base.append((d, x["side"], baseline(B, x, rnd)))
    return trades, base


def tod(t):
    hm = t[11:16]
    return "09:30-10:00" if hm < "10:00" else "10:00-11:00" if hm < "11:00" else "11:00-13:00" if hm < "13:00" else "13:00-14:30" if hm < "14:30" else "14:30-15:30"


ROW = ("legPct", LEGS, "% leg")
COL = ("stopBuf", STOPS)
COL_NAMES = {0.0: "at the range end", 0.1: "10% past the end", 0.25: "25% past the end"}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("s", "Ticker"), ("wd", "Weekday"), ("wait", "Minutes from the 50% to the entry"),
           ("size", "Leg size vs the rule's minimum")]


def describe(p):
    side = 'down legs → long' if p.get('side') == 'long' else 'up legs → short' if p.get('side') == 'short' else 'both sides'
    stop = 'at the range end' if not p.get('stopBuf') else f"{round(p['stopBuf'] * 100)}% of the range past its end"
    return (f"{', '.join(p.get('tickers') or [])} {p.get('tf', 1)}-minute: after a leg of at least {p.get('legPct')}% ({side}), a retrace to "
            f"the range's 50%, then back to {round(p.get('entryLvl', 0.25) * 100)}%: enter there, target the 50%, stop {stop}, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


def decorate(x, p):
    import datetime as dt
    x["tod"] = tod(x["in"])
    x["wd"] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.date.fromisoformat(x["d"]).weekday()]
    w = (int(x["in"][11:13]) * 60 + int(x["in"][14:16])) - (int(x["touch"][11:13]) * 60 + int(x["touch"][14:16]))
    x["wait"] = "under 5 min" if w < 5 else "5-15 min" if w < 15 else "15-60 min" if w < 60 else "60 min+"
    rel = x["R"] / (abs(x["e"]) * p["legPct"] / 100) if x["e"] else 1
    x["size"] = "1-1.5x" if rel < 1.5 else "1.5-2x" if rel < 2 else "2x+"


def coverage(sub, p, prog):
    cov = []
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {p['days']} days of minute candles")
        by = bars(sub, sym, p["days"], p["tf"])
        ds = sorted(by)
        if ds:
            cov.append({"s": sym, "from": ds[0], "to": ds[-1], "days": len(ds), "bars": sum(len(v) for v in by.values())})
    return cov
