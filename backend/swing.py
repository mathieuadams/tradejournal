"""Swing strategies on daily bars: the Turtle breakout and Qullamaggie's breakout, backtested over the earnings
universes (S&P 500 and / or Nasdaq-100, removed companies included) or the ETF list.

TURTLE (Dennis / Faith, "Way of the Turtle"):
  N = the 20-day average true range. Long when the high breaks the highest high of the last `entryN` days (20:
  System 1, 55: System 2), filled at that level (at the open on a gap through it). Stop `stopN` × N below the entry;
  exit when the low breaks the lowest low of the last `exitN` days (10 / 20) — whichever level is hit first. Pyramid
  (option): one more unit every ½ N in favour, up to 4 units, the stop of every unit raised to 2 N below the last
  add. Shorts mirror it (option). System 1's filter (option): skip a breakout when this symbol's last trade won.
  Market filter (option): longs only above the benchmark's 200-day average, shorts only below.
QULLAMAGGIE BREAKOUT (Kristjan Kullamägi):
  Setup at a close: ADR(20) ≥ `minAdr`, up ≥ `prior` over the last `priorDays` (30 or 60) days (close vs that low), above the
  50-day average with the 10 above the 20, a tight base — the last `base` days' range ≤ `tight` × ADR × base^0.5
  … simplified to: range of the last `base` days ≤ `tight` (share of the price) — price ≥ $5 and the 20-day average $ volume ≥ `minDv` ($10M).
  Entry the next day when the high breaks the base's high (filled there, or at the open on a gap above it). Stop:
  that day's low; skipped when the stop is further than one ADR. After `partialDay` sessions `partial` of the
  position is sold at the close and the stop goes to breakeven; the rest is sold on the first close below the
  `trail`-day average. Market filter (option): the benchmark above its 20-day average.
Fills at a level hit during the day are at the level (or the open when it gaps through); a day that touches the stop
and the exit channel takes the first one a falling price meets. $`stake` per unit; costs `costBps` round trip; shorts
pay `borrowPct` a year. Test period: the last 5 years; the optimizer ranks on the years before only."""
import itertools
import math
import time
from datetime import date

import store
from util import BadRequest, iso, now_ny

TEST_YEARS = 5
FROM = "2016-01-01"
MIN_TRADES = 30
UNIS = ("fut", "spy", "qqq", "both", "etf")
T_DEFAULT = {"uni": "fut", "entryN": 20, "exitN": 10, "stopN": 2.0, "pyramid": False, "shorts": True, "skipWin": False, "mkt": False,
             "stake": 1000.0, "costBps": 10.0, "borrowPct": 1.0, "risk": 1000.0, "whole": False, "comm": 5.0, "slip": 1.0, "groups": None}
Q_DEFAULT = {"uni": "both", "minAdr": 0.04, "prior": 0.3, "priorDays": 60, "minDv": 10e6, "sizing": "account", "acct": 10000.0, "riskPct": 1.0, "maxPos": 25.0, "base": 10, "tight": 0.15, "partialDay": 3, "partial": 1 / 3, "trail": 10, "mkt": False,
             "stake": 1000.0, "costBps": 10.0, "borrowPct": 1.0}
T_GRID = {"entryN": [20, 55], "stopN": [2.0, 3.0], "pyramid": [False, True], "shorts": [False, True], "mkt": [False, True]}
Q_GRID = {"minAdr": [0.03, 0.05], "prior": [0.3, 0.5], "priorDays": [30, 60], "tight": [0.10, 0.20], "trail": [10, 20], "partialDay": [0, 3], "mkt": [False, True]}


def _d(s):
    return date.fromisoformat(s)


def clean(kind, cfg):
    base = T_DEFAULT if kind == "turtle" else Q_DEFAULT
    c = {**base, **{k: v for k, v in (cfg or {}).items() if k in base}}
    c["uni"] = c["uni"] if c["uni"] in UNIS else ("fut" if kind == "turtle" else "both")
    if kind == "qulla" and c["uni"] == "fut":
        c["uni"] = "both"                                  # percentages on back-adjusted futures prices mean nothing
    for k in ("stake", "costBps", "borrowPct"):
        c[k] = max(0.0, float(c[k]))
    c["stake"] = max(10.0, c["stake"])
    if kind == "turtle":
        c["entryN"] = int(c["entryN"]) if int(c["entryN"]) in (20, 55) else 20
        c["exitN"] = 10 if c["entryN"] == 20 else 20
        c["stopN"] = max(0.5, min(6.0, float(c["stopN"])))
        for k in ("pyramid", "shorts", "skipWin", "mkt", "whole"):
            c[k] = bool(c[k])
        c["risk"] = max(10.0, float(c["risk"]))
        c["comm"], c["slip"] = max(0.0, float(c["comm"])), max(0.0, float(c["slip"]))
        import futdaily
        g = [x for x in (c["groups"] or []) if x in futdaily.GROUPS]
        c["groups"] = g or None
        if c["uni"] == "fut":
            c["mkt"] = False
    else:
        c["minAdr"] = max(0.0, min(0.3, float(c["minAdr"])))
        c["prior"] = max(0.0, min(5.0, float(c["prior"])))
        c["priorDays"] = 30 if int(c["priorDays"]) == 30 else 60
        c["sizing"] = "fixed" if c["sizing"] == "fixed" else "account"
        c["acct"] = max(500.0, float(c["acct"]))
        c["riskPct"] = max(0.05, min(10.0, float(c["riskPct"])))
        c["maxPos"] = max(1.0, min(100.0, float(c["maxPos"])))
        c["minDv"] = max(0.0, float(c["minDv"]))
        c["base"] = int(c["base"]) if int(c["base"]) in (5, 10, 15, 20) else 10
        c["tight"] = max(0.02, min(0.6, float(c["tight"])))
        c["partialDay"] = int(c["partialDay"]) if int(c["partialDay"]) in (0, 3, 4, 5) else 3
        c["partial"] = max(0.0, min(0.9, float(c["partial"])))
        c["trail"] = int(c["trail"]) if int(c["trail"]) in (10, 20, 50) else 10
        c["mkt"] = bool(c["mkt"])
    return c


# ---------------------------------------------------------------- data

def universe(sub, uni):
    import etf
    import fundamentals as F
    import space
    if uni == "etf":
        return etf.settings(sub)["syms"], "SPY"
    if uni == "fut":
        import futdaily
        out = [r for r in futdaily.SPEC if futdaily.bars(r)]
        if not out:
            raise BadRequest("No futures daily bars yet: press “Download futures daily bars” (Databento).")
        return out, None
    out = []
    for sp in (("spy", "qqq") if uni == "both" else (uni,)):
        prev = space.get()
        space.set(sp)
        try:
            out += [s for s in F.settings(sub)["universe"] if s not in out]
        finally:
            space.set(prev)
    if not out:
        raise BadRequest("The universe is empty: load the S&P 500 / Nasdaq-100 on the Earnings pages first.")
    return out, "QQQ" if uni == "qqq" else "SPY"


def sym_data(sub, sym, fut):
    if fut:
        import futdaily
        return futdaily.bars(sym), futdaily.SPEC[sym]
    return bars_of(sub, sym), None


def bars_of(sub, sym):
    import fundamentals as F
    b = (store.get_json("fund/px/" + sym) or {}).get("bars")
    if not b:
        try:
            b = F.prices(sub, sym, FROM, max_age_h=24 * 365)
        except Exception:
            return None
    return [x for x in b if x[0] >= FROM and x[1] and x[2] and x[3] and x[4]]


def _sma(xs, n):
    out, s = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def market(sub, sym):
    if not sym:
        return {}
    b = bars_of(sub, sym) or []
    c = [x[4] for x in b]
    s20, s200 = _sma(c, 20), _sma(c, 200)
    return {x[0]: (x[4], s20[i], s200[i]) for i, x in enumerate(b)}


# ---------------------------------------------------------------- the simulations (one symbol)

def _cost(c, units, short, days):
    return c["stake"] * units * (c["costBps"] / 1e4 + (c["borrowPct"] / 100 * days / 365 if short else 0))


def turtle(sym, b, c, mkt, spec=None):
    """Turtle trades of one symbol (open = still open at the last bar)."""
    n = len(b)
    if n < 80:
        return []
    o, h, l, cl, ds = [x[1] for x in b], [x[2] for x in b], [x[3] for x in b], [x[4] for x in b], [x[0] for x in b]
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - cl[i - 1]), abs(l[i] - cl[i - 1])) for i in range(1, n)]
    N = _sma(tr, 20)
    E, X, SN = c["entryN"], c["exitN"], c["stopN"]
    out, pos, last_win = [], None, False
    for t in range(max(E, 21), n):
        if pos is None:
            if N[t - 1] is None:
                continue
            hi, lo = max(h[t - E:t]), min(l[t - E:t])
            m = mkt.get(ds[t - 1]) if c["mkt"] else None
            up_ok = not c["mkt"] or (m and m[2] and m[0] > m[2])
            dn_ok = not c["mkt"] or (m and m[2] and m[0] < m[2])
            side = None
            if h[t] > hi and up_ok:
                side, px = 1, max(o[t], hi)
            elif c["shorts"] and l[t] < lo and dn_ok:
                side, px = -1, min(o[t], lo)
            if side is None:
                continue
            if c["skipWin"] and E == 20 and last_win:
                last_win = False                       # System 1: skip the breakout after a winner (once)
                continue
            n0 = N[t - 1]
            if spec:
                q = c["risk"] / (n0 * spec[2]) if n0 > 0 else 0   # contracts per unit: 1 N move = the $ risk per unit
                if c["whole"]:
                    q = float(int(q))
                if q <= 0:
                    continue
            else:
                q = None
            pos = {"q": q,"side": side, "e": t, "units": [px], "n0": n0, "stop": px - side * SN * n0}
            # pyramid on the entry day itself (a strong breakout day)
        side, n0 = pos["side"], pos["n0"]
        if t > pos["e"]:                                # exits (from the day after the entry)
            ch = min(l[t - X:t]) if side == 1 else max(h[t - X:t])
            lvl = max(pos["stop"], ch) if side == 1 else min(pos["stop"], ch)
            if (side == 1 and l[t] <= lvl) or (side == -1 and h[t] >= lvl):
                fill = min(o[t], lvl) if side == 1 else max(o[t], lvl)
                tr_ = _turtle_trade(sym, ds, pos, t, fill, "stop" if lvl == pos["stop"] else f"{X}-day {'low' if side == 1 else 'high'}", c, spec)
                out.append(tr_)
                last_win = tr_["pnl"] > 0
                pos = None
                continue
        if c["pyramid"]:
            while len(pos["units"]) < 4:
                nxt = pos["units"][-1] + side * 0.5 * n0
                if (side == 1 and h[t] >= nxt) or (side == -1 and l[t] <= nxt):
                    fillp = max(o[t], nxt) if side == 1 else min(o[t], nxt)
                    if t == pos["e"] and len(pos["units"]) == 1 and ((side == 1 and fillp < pos["units"][0]) or (side == -1 and fillp > pos["units"][0])):
                        break
                    pos["units"].append(fillp)
                    pos["stop"] = fillp - side * SN * n0
                else:
                    break
    if pos is not None:
        tr_ = _turtle_trade(sym, ds, pos, n - 1, cl[-1], "open", c, spec)
        tr_["open"] = True
        out.append(tr_)
    return out


def _turtle_trade(sym, ds, pos, x, fill, why, c, spec=None):
    side, us = pos["side"], pos["units"]
    days = (_d(ds[x]) - _d(ds[pos["e"]])).days
    if spec:
        q, pv, tick = pos["q"], spec[2], spec[3]
        gross = sum(q * side * (fill - u) * pv for u in us)
        pnl = gross - len(us) * q * (c["comm"] + 2 * c["slip"] * tick * pv)
        risk = c["stopN"] * pos["n0"] * q * pv
        return {"sym": sym, "side": "long" if side == 1 else "short", "entry": ds[pos["e"]], "exit": ds[x], "in": round(us[0], 6), "out": round(fill, 6),
                "units": len(us), "contracts": round(q * len(us), 2), "days": days, "why": why, "ret": None,
                "pts": round(side * (fill - us[0]), 6), "pnl": round(pnl, 2), "R": round(pnl / risk, 2) if risk > 0 else None, "group": spec[1]}
    gross = sum(c["stake"] * side * (fill / u - 1) for u in us)
    pnl = gross - _cost(c, len(us), side == -1, days)
    risk = c["stake"] * c["stopN"] * pos["n0"] / us[0]          # the first unit's risk in $
    return {"sym": sym, "side": "long" if side == 1 else "short", "entry": ds[pos["e"]], "exit": ds[x], "in": round(us[0], 4), "out": round(fill, 4),
            "units": len(us), "days": days, "why": why, "ret": round(side * (fill / us[0] - 1), 5), "pnl": round(pnl, 2),
            "R": round(pnl / risk, 2) if risk > 0 else None}


def qulla(sym, b, c, mkt):
    """Qullamaggie breakout trades of one symbol."""
    n = len(b)
    if n < 80:
        return []
    o, h, l, cl, v, ds = [x[1] for x in b], [x[2] for x in b], [x[3] for x in b], [x[4] for x in b], [x[5] for x in b], [x[0] for x in b]
    s10, s20, s50 = _sma(cl, 10), _sma(cl, 20), _sma(cl, 50)
    st = _sma(cl, c["trail"]) if c["trail"] not in (10, 20) else (s10 if c["trail"] == 10 else s20)
    rng = [h[i] / l[i] - 1 for i in range(n)]
    adr = _sma(rng, 20)
    dv = _sma([cl[i] * v[i] for i in range(n)], 20)
    B = c["base"]
    out, pos = [], None
    for t in range(61, n):
        p = t - 1
        if pos is None:
            if s50[p] is None or adr[p] is None or dv[p] is None:
                continue
            if cl[p] < 5 or dv[p] < c["minDv"] or adr[p] < c["minAdr"]:
                continue
            if not (cl[p] > s50[p] and s10[p] > s20[p]):
                continue
            if cl[p] / min(l[p - c["priorDays"]:p + 1]) - 1 < c["prior"]:
                continue
            bh, bl = max(h[p - B + 1:p + 1]), min(l[p - B + 1:p + 1])
            if bh / bl - 1 > c["tight"]:
                continue
            if c["mkt"]:
                m = mkt.get(ds[p])
                if not (m and m[1] and m[0] > m[1]):
                    continue
            if h[t] <= bh:
                continue
            px = max(o[t], bh)
            stop = l[t]
            if px <= stop or (px - stop) / px > adr[p]:
                continue                                # the stop is further than one ADR: skip
            pos = {"e": t, "px": px, "stop": stop, "left": 1.0, "parts": [], "risk": (px - stop) / px}
            continue
        k = t - pos["e"]                               # sessions since the entry
        if l[t] <= pos["stop"]:
            fill = min(o[t], pos["stop"])
            pos["parts"].append((pos["left"], fill))
            out.append(_q_trade(sym, ds, pos, t, "stop" if pos["stop"] < pos["px"] else "breakeven stop", c))
            pos = None
            continue
        if c["partialDay"] and k == c["partialDay"] and pos["left"] == 1.0 and c["partial"] > 0:
            pos["parts"].append((c["partial"], cl[t]))
            pos["left"] -= c["partial"]
            pos["stop"] = max(pos["stop"], pos["px"])  # breakeven
        if st[t] is not None and cl[t] < st[t] and k >= 1:
            pos["parts"].append((pos["left"], cl[t]))
            out.append(_q_trade(sym, ds, pos, t, f"close below the {c['trail']}-day average", c))
            pos = None
    if pos is not None:
        pos["parts"].append((pos["left"], cl[-1]))
        tr_ = _q_trade(sym, ds, pos, n - 1, "open", c)
        tr_["open"] = True
        out.append(tr_)
    return out


def _q_trade(sym, ds, pos, x, why, c):
    ret = sum(w * (f / pos["px"] - 1) for w, f in pos["parts"])
    days = (_d(ds[x]) - _d(ds[pos["e"]])).days
    pnl = c["stake"] * ret - _cost(c, 1, False, days)
    return {"sym": sym, "side": "long", "entry": ds[pos["e"]], "exit": ds[x], "in": round(pos["px"], 4), "out": round(pos["parts"][-1][1], 4),
            "units": 1, "days": days, "why": why, "ret": round(ret, 5), "pnl": round(pnl, 2), "risk": round(pos["risk"], 5),
            "R": round(ret / pos["risk"], 2) if pos["risk"] > 0 else None}


def setups(sym, b, c, kind):
    """At the last close: what would trigger at the next session (a watchlist)."""
    n = len(b)
    if n < 80:
        return None
    h, l, cl, ds = [x[2] for x in b], [x[3] for x in b], [x[4] for x in b], [x[0] for x in b]
    p = n - 1
    if kind == "turtle":
        E = c["entryN"]
        hi = max(h[p - E + 1:p + 1])
        tr = [max(h[i] - l[i], abs(h[i] - cl[i - 1]), abs(l[i] - cl[i - 1])) for i in range(p - 19, p + 1)]
        N = sum(tr) / 20
        dist = (hi - cl[p]) / N if N else None
        if dist is not None and dist <= 0.5:
            return {"sym": sym, "date": ds[p], "level": round(hi, 2), "close": cl[p], "note": f"{dist:.2f} N under the {E}-day high", "stop": round(hi - c["stopN"] * N, 2)}
        return None
    v = [x[5] for x in b]
    s10, s20, s50 = sum(cl[p - 9:p + 1]) / 10, sum(cl[p - 19:p + 1]) / 20, sum(cl[p - 49:p + 1]) / 50
    adr = sum(h[i] / l[i] - 1 for i in range(p - 19, p + 1)) / 20
    dv = sum(cl[i] * v[i] for i in range(p - 19, p + 1)) / 20
    if cl[p] < 5 or dv < c["minDv"] or adr < c["minAdr"] or not (cl[p] > s50 and s10 > s20):
        return None
    prior = cl[p] / min(l[p - c["priorDays"]:p + 1]) - 1
    if prior < c["prior"]:
        return None
    bh, bl = max(h[p - c["base"] + 1:p + 1]), min(l[p - c["base"] + 1:p + 1])
    if bh / bl - 1 > c["tight"]:
        return None
    return {"sym": sym, "date": ds[p], "level": round(bh, 2), "close": cl[p], "note": f"ADR {adr * 100:.1f}% · up {prior * 100:.0f}% in {c['priorDays']}d · base {(bh / bl - 1) * 100:.1f}%",
            "stop": None}


# ---------------------------------------------------------------- run (a background job) and view

def _stats(ts):
    import backtest
    s = backtest._stats(ts)
    rs = [t["R"] for t in ts if t.get("R") is not None]
    if rs:
        s["avgR"] = round(sum(rs) / len(rs), 2)
        s["big"] = sum(1 for r in rs if r >= 3) / len(rs)
    return s


def account(ts, c, start=None):
    """Compounding account: each trade sized at riskPct of the equity ÷ its stop distance, capped at maxPos of the
    equity and at the cash left (no margin); taken in entry order, exits of a day before its entries. Equity for the
    sizing = cash + open positions at cost. Returns the curve and the stats."""
    ev = []
    for i, t in enumerate(ts):
        if start and t["entry"] < start:
            continue
        ev.append((t["entry"], 1, i))
        ev.append((t["exit"], 0, i))
    ev.sort()
    cash = eq = c["acct"]
    held, out, curve, taken, skipped, peak, mdd = {}, {}, [], 0, 0, c["acct"], 0.0
    for d, kind, i in ev:
        t = ts[i]
        if kind == 1:
            eq_now = cash + sum(held.values())
            r = t.get("risk") or 0
            size = eq_now * c["riskPct"] / 100 / r if r > 0 else eq_now * c["maxPos"] / 100
            size = min(size, eq_now * c["maxPos"] / 100, cash)
            if size < 50:
                skipped += 1
                continue
            held[i] = size
            cash -= size
            taken += 1
        elif i in held:
            size = held.pop(i)
            pnl = size * t["ret"] - size * c["costBps"] / 1e4
            cash += size + pnl
            out[i] = (size, pnl)
            eq = cash + sum(held.values())
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
            if curve and curve[-1][0] == d:
                curve[-1][1] = round(eq, 2)
            else:
                curve.append([d, round(eq, 2)])
    final = cash + sum(held.values())
    years = {}
    prev = c["acct"]
    for d, v in curve:
        years[d[:4]] = v
    yr = []
    for y in sorted(years):
        yr.append({"year": y, "end": years[y], "ret": round(years[y] / prev - 1, 4)})
        prev = years[y]
    span = (_d(curve[-1][0]) - _d(curve[0][0])).days / 365.25 if len(curve) > 1 else 0
    return {"start": c["acct"], "from": start or (curve[0][0] if curve else None), "final": round(final, 2), "ret": round(final / c["acct"] - 1, 4),
            "cagr": round((final / c["acct"]) ** (1 / span) - 1, 4) if span > 0.5 and final > 0 else None, "maxDD": round(mdd, 4),
            "taken": taken, "skipped": skipped, "years": yr, "curve": curve[-1500:],
            "sizes": {i: round(v[0], 2) for i, v in out.items()}}


def split_date(last):
    d = _d(last)
    return d.replace(year=d.year - TEST_YEARS).isoformat()


def run_job(sub, kind, prog=None):
    key = f"swing/u/{sub}/{kind}"
    st = store.get_json(key) or {}
    c = clean(kind, st.get("cfg") or {})
    syms, bench = universe(sub, c["uni"])
    mkt = market(sub, bench)
    allt, watch, t_last, last = [], [], 0.0, FROM
    fn = turtle if kind == "turtle" else qulla
    fut = c["uni"] == "fut"
    if fut and c["groups"]:
        import futdaily
        syms = [x for x in syms if futdaily.SPEC[x][1] in c["groups"]]
    for i, s in enumerate(syms):
        if prog and time.time() - t_last > 3:
            prog(f"{'Turtle' if kind == 'turtle' else 'Qullamaggie'}: {i + 1}/{len(syms)} {s}")
            t_last = time.time()
        b, spec = sym_data(sub, s, fut)
        if not b:
            continue
        last = max(last, b[-1][0])
        allt.extend(fn(s, b, c, mkt, spec) if spec else fn(s, b, c, mkt))
        w = setups(s, b, c, kind)
        if w and b[-1][0] >= last:
            watch.append(w)
    opens = [t for t in allt if t.get("open")]
    ts = sorted([t for t in allt if not t.get("open")], key=lambda t: (t["exit"], t["sym"]))
    sd = split_date(last)
    tr, te = [t for t in ts if t["entry"] < sd], [t for t in ts if t["entry"] >= sd]
    eq, cum = [], 0.0
    for t in ts:
        cum += t["pnl"]
        if eq and eq[-1][0] == t["exit"]:
            eq[-1][1] = round(cum, 2)
        else:
            eq.append([t["exit"], round(cum, 2)])
    years, bys = {}, {}
    for t in ts:
        years.setdefault(t["exit"][:4], []).append(t)
        bys.setdefault(t["sym"], []).append(t)
    ss = sorted(({"sym": k, **_stats(v)} for k, v in bys.items() if len(v) >= 3), key=lambda x: -x["total"])
    ev = sorted([(t["entry"], 1) for t in ts] + [(t["exit"], -1) for t in ts])
    k = peak = 0
    for _, d_ in ev:
        k += d_
        peak = max(peak, k)
    why = {}
    for t in ts:
        why.setdefault(t["why"].split(" ")[0] if t["why"].startswith(("10-", "20-")) else t["why"], []).append(t)
    grp = {}
    for t in ts:
        if t.get("group"):
            grp.setdefault(t["group"], []).append(t)
    acct = None
    if kind == "qulla" and c["sizing"] == "account":
        a_all, a_test = account(ts, c), account(ts, c, sd)
        for i, t in enumerate(ts):
            if i in a_all["sizes"]:
                t["size"] = a_all["sizes"][i]
        a_all.pop("sizes"); a_test.pop("sizes")
        acct = {"all": a_all, "test": a_test}
    res = {"kind": kind, "cfg": c, "acct": acct, "groups": [{"group": g, **_stats(v)} for g, v in sorted(grp.items(), key=lambda z: -sum(t["pnl"] for t in z[1]))], "at": iso(now_ny()), "nSyms": len(syms), "bench": bench, "n": len(ts), "all": _stats(ts),
           "split": {"date": sd, "train": _stats(tr), "test": _stats(te)}, "years": [{"year": y, **_stats(v)} for y, v in sorted(years.items())],
           "best": ss[:12], "worst": ss[-12:][::-1], "why": [{"why": w, **_stats(v)} for w, v in sorted(why.items(), key=lambda z: -len(z[1]))],
           "equity": eq, "peak": peak, "trades": ts[-400:], "open": sorted(opens, key=lambda t: t["entry"])[-60:], "watch": watch[:80], "last": last}
    store.put_json(key, {"cfg": c, "res": res})
    return res


def start(sub, kind, cfg):
    import jobs
    if kind not in ("turtle", "qulla"):
        raise BadRequest("Unknown strategy.")
    c = clean(kind, cfg)
    universe(sub, c["uni"])                            # an empty universe fails here, on the page, not in the job
    st = store.get_json(f"swing/u/{sub}/{kind}") or {}
    store.put_json(f"swing/u/{sub}/{kind}", {**st, "cfg": c})
    return jobs.start(sub, "swing" + kind, "swingrun", {"kind": kind})


def view(sub, kind):
    import jobs
    st = store.get_json(f"swing/u/{sub}/{kind}") or {}
    o = store.get_json(f"swing/u/{sub}/{kind}opt")
    opt = None
    if o:
        rk = sorted(o.get("res") or [], key=lambda x: ((x["train"].get("n") or 0) >= MIN_TRADES, x["train"].get("t") or -1e9), reverse=True)
        opt = {k: v for k, v in o.items() if k not in ("res", "acc")} | {"top": rk[:15], "done": len(o.get("res") or []), "minTrades": MIN_TRADES}
    import futdaily
    from datetime import timedelta

    def js(name):
        j = jobs.status(sub, name)
        if j.get("status") == "running" and j.get("at", "") < iso(now_ny() - timedelta(minutes=20)):
            j["stale"] = True                          # silent for 20 minutes: the run died (it can be started again)
        return j
    fut = futdaily.status() if kind == "turtle" else None
    return {"cfg": st.get("cfg") or clean(kind, {}), "res": st.get("res"), "opt": opt, "job": js("swing" + kind), "fut": fut,
            "futJob": js("swingfut") if kind == "turtle" else None,
            "groups": futdaily.GROUPS}


# ---------------------------------------------------------------- the optimizer: symbols outside, settings inside

def grid(kind, uni=None):
    g = T_GRID if kind == "turtle" else Q_GRID
    if uni == "fut":
        g = {k: v for k, v in g.items() if k != "mkt"}
    keys = list(g)
    return [dict(zip(keys, v)) for v in itertools.product(*[g[k] for k in keys])]


def opt_start(sub, kind, cfg):
    import jobs
    c = clean(kind, cfg)
    universe(sub, c["uni"])
    store.put_json(f"swing/u/{sub}/{kind}opt", {"cfg": c, "i": 0, "acc": None})
    return jobs.start(sub, "swing" + kind, "swingopt", {"kind": kind})


def _acc_add(a, pnl, r):
    a[0] += 1; a[1] += pnl; a[2] += pnl * pnl
    if pnl > 0:
        a[3] += 1; a[4] += pnl
    else:
        a[5] += pnl
    if r is not None:
        a[6] += r; a[7] += 1; a[8] += 1 if r >= 3 else 0


def _acc_stats(a):
    n, s1, s2, w, sw, sl, sr, nr, big = a
    if not n:
        return {"n": 0}
    m = s1 / n
    sd = math.sqrt(max(0.0, (s2 - n * m * m) / (n - 1))) if n > 1 else 0
    return {"n": n, "total": round(s1, 2), "avg": round(m, 2), "win": w / n, "profitFactor": round(sw / -sl, 2) if sl < 0 else None,
            "t": round(m / (sd / math.sqrt(n)), 2) if sd else None, "avgR": round(sr / nr, 2) if nr else None, "big": big / nr if nr else None}


def opt_job(sub, kind, prog=None, deadline=None):
    """Every setting on every symbol; resumable (the symbol index and running sums per setting are saved)."""
    key = f"swing/u/{sub}/{kind}opt"
    st = store.get_json(key) or {}
    base = clean(kind, st.get("cfg") or {})
    syms, bench = universe(sub, base["uni"])
    mkt = market(sub, bench)
    fut = base["uni"] == "fut"
    if fut and base["groups"]:
        import futdaily
        syms = [x for x in syms if futdaily.SPEC[x][1] in base["groups"]]
    combos = grid(kind, base["uni"])
    sd = split_date(max(mkt) if mkt else (max((sym_data(sub, x, fut)[0] or [[FROM]])[-1][0] for x in syms) if fut else now_ny().date().isoformat()))
    acc = st.get("acc") or [{"train": [0] * 9, "test": [0] * 9} for _ in combos]
    fn = turtle if kind == "turtle" else qulla
    cfgs = [clean(kind, {**base, **p}) for p in combos]
    i0, t_last = st.get("i") or 0, 0.0
    for i in range(i0, len(syms)):
        if deadline and time.time() > deadline:
            store.put_json(key, {"cfg": base, "i": i, "acc": acc, "total": len(combos), "split": sd, "n": len(syms)})
            return False
        if prog and time.time() - t_last > 3:
            prog(f"Optimizer ({'Turtle' if kind == 'turtle' else 'Qullamaggie'}): {i + 1}/{len(syms)} symbols × {len(combos)} settings")
            t_last = time.time()
        b, spec = sym_data(sub, syms[i], fut)
        if not b:
            continue
        for j, c in enumerate(cfgs):
            for t in (fn(syms[i], b, c, mkt, spec) if spec else fn(syms[i], b, c, mkt)):
                if not t.get("open"):
                    _acc_add(acc[j]["train" if t["entry"] < sd else "test"], t["pnl"], t.get("R"))
    res = [{"p": p, "train": _acc_stats(a["train"]), "test": _acc_stats(a["test"])} for p, a in zip(combos, acc)]
    store.put_json(key, {"cfg": base, "res": res, "split": sd, "total": len(combos), "at": iso(now_ny()), "complete": True})
    return True
