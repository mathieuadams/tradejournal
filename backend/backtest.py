"""Earnings strategy backtest: what the rules found in the event study would have made, in dollars.

Four legs, each optional, each a fixed stake per trade (default $1,000):
  A  After the call, buy at the open: the "after the call" model's out-of-sample prediction is in the top fifth of the
     previous year's predictions, and the release isn't "bad news + gap up". Buy at the day-0 open, sell at the close.
  B  Fade "bad news + gap up": post-release score in the bottom fifth and a gap above +2%. Short at the open, cover at
     the close.
  C  Post-release long: score in the top fifth. Buy at the day-0 close, hold N sessions (20 or 40). Optional filters:
     the market weak going in (below its 200-day average or down over 3 months), day 0 up >2% on heavy volume (≥2.5×),
     the stock more than 25% below its 52-week high.
  D  Post-release short: score in the bottom fifth. Short at the day-0 close, hold N sessions. Optional filter: the
     market strong going in (above its 200-day average and up over 3 months).

No look-ahead: the score uses signals ranked against earlier releases only, the "after the call" prediction is the
walk-forward (out-of-sample) one, and "top / bottom fifth" is judged against the previous 365 days of releases.
Caveat: the rules themselves were chosen after looking at this data, so the result is optimistic; the year-by-year
table is the honest check.

Hedge (legs C and D; A and B are day trades, unhedged): none (the stock alone), SPY 1:1, or beta × SPY. Costs: a round trip in basis points of the stake (default 10 =
$1 per $1,000), plus, for shorts, a borrow fee per year."""
import bisect
import math
from datetime import date, timedelta

WINDOW = 365
MIN_PRIOR = 200


def _d(s):
    return date.fromisoformat(s[:10])


def trailing_pct(rows, key, when="day0"):
    """{index: percentile (0..1) of rows[i][key] among the previous WINDOW days' values}; None while fewer than
    MIN_PRIOR earlier values exist."""
    order = sorted((i for i, r in enumerate(rows) if r.get(key) is not None and r.get(when)), key=lambda i: rows[i][when])
    win, q, qi, out = [], [], 0, {}
    for i in order:
        w = rows[i][when]
        cut = (_d(w) - timedelta(days=WINDOW)).isoformat()
        while qi < len(q) and q[qi][0] < cut:
            v = q[qi][1]
            j = bisect.bisect_left(win, v)
            if j < len(win) and win[j] == v:
                win.pop(j)
            qi += 1
        v = rows[i][key]
        if len(win) >= MIN_PRIOR:
            lo, hi = bisect.bisect_left(win, v), bisect.bisect_right(win, v)
            out[i] = (lo + hi) / 2 / len(win)
        q.append((w, v))
        bisect.insort(win, v)
    return out


DEFAULT = {"stake": 1000.0, "costBps": 10.0, "borrowPct": 1.0, "hedge": "none", "hold": 40,
           "legs": {"A": True, "B": True, "C": True, "D": True},
           "filters": {"cWeakMarket": False, "cHeavyUp": False, "cFarFromHigh": False, "dStrongMarket": False},
           "from": None, "to": None}


def clean(cfg):
    c = {**DEFAULT, **{k: v for k, v in (cfg or {}).items() if k in DEFAULT}}
    c["legs"] = {**DEFAULT["legs"], **((cfg or {}).get("legs") or {})}
    c["filters"] = {**DEFAULT["filters"], **((cfg or {}).get("filters") or {})}
    c["stake"] = max(10.0, min(1e7, float(c["stake"])))
    c["costBps"] = max(0.0, min(500.0, float(c["costBps"])))
    c["borrowPct"] = max(0.0, min(50.0, float(c["borrowPct"])))
    c["hold"] = 20 if int(c["hold"]) == 20 else 40
    if c["hedge"] not in ("none", "spy", "beta"):
        c["hedge"] = "none"
    return c


def _f(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def _o2c(r, hedge):
    """Day-0 open → close: raw from the day-0 return and the gap; hedged with the benchmark's own open → close."""
    r1, gap, ab = _f(r.get("r1")), _f(r.get("gap")), _f(r.get("o2c"))
    if r1 is None or gap is None or gap <= -1:
        return None
    raw = (1 + r1) / (1 + gap) - 1
    if hedge == "none":
        return raw
    if ab is None:
        return None
    spy = raw - ab
    if hedge == "spy":
        return ab
    b = _f(r.get("beta"))
    return raw - (b if b is not None else 1.0) * spy


def _hold(r, n, hedge):
    """Day-0 close → n sessions later."""
    r1, rn = _f(r.get("r1")), _f(r.get(f"r{n}"))
    if r1 is None or rn is None or r1 <= -1:
        return None
    raw = (1 + rn) / (1 + r1) - 1
    if hedge == "none":
        return raw
    if hedge == "spy":
        return _f(r.get(f"drift{n}"))
    v = _f(r.get(f"drift{n}b"))
    return v if v is not None else _f(r.get(f"drift{n}"))


def run(rows, cfg):
    c = clean(cfg)
    stake = c["stake"]
    cost_day = c["costBps"] / 1e4                      # A, B: day trades, not hedged
    cost = c["costBps"] / 1e4 * (2 if c["hedge"] != "none" else 1)   # C, D: the hedge is a second trade
    score_p = trailing_pct(rows, "postScore")
    pred_p = trailing_pct(rows, "oosO2c")
    vol0_ok = lambda r: (_f(r.get("vol0")) or 0) >= 2.5
    trades = []
    for i, r in enumerate(rows):
        d0 = r.get("day0")
        if not d0 or (c["from"] and d0 < c["from"]) or (c["to"] and d0 > c["to"]):
            continue
        sp, pp = score_p.get(i), pred_p.get(i)
        gap = _f(r.get("gap"))
        bad_gap_up = sp is not None and sp <= 0.2 and gap is not None and gap > 0.02

        def add(leg, side, ret, exit_d, days, why):
            if ret is None or ret <= -1:
                return
            borrow = c["borrowPct"] / 100 * days / 365 if side == "short" else 0.0
            pnl = stake * ((ret if side == "long" else -ret) - (cost_day if leg in "AB" else cost) - borrow)
            trades.append({"leg": leg, "sym": r["sym"], "release": r["date"], "entry": d0, "exit": exit_d, "side": side,
                           "ret": round(ret, 6), "pnl": round(pnl, 2), "why": why})

        if c["legs"]["A"] and pp is not None and pp >= 0.8 and not bad_gap_up:
            add("A", "long", _o2c(r, "none"), d0, 1, f"after-call model top {100 - pp * 100:.0f}%")
        if c["legs"]["B"] and bad_gap_up:
            add("B", "short", _o2c(r, "none"), d0, 1, f"score bottom fifth, gap {gap * 100:+.1f}%")
        n = c["hold"]
        exit_d = r.get(f"t{n}")
        if not exit_d:
            continue
        cal_days = (_d(exit_d) - _d(d0)).days
        if c["legs"]["C"] and sp is not None and sp >= 0.8:
            f = c["filters"]
            ok = True
            if f["cWeakMarket"]:
                ok &= (_f(r.get("spyVs200")) or 0) < 0 or (_f(r.get("spy63")) or 0) < 0
            if f["cHeavyUp"]:
                ok &= (_f(r.get("r1ab")) or 0) > 0.02 and vol0_ok(r)
            if f["cFarFromHigh"]:
                ok &= (_f(r.get("offHigh")) or 0) < -0.25
            if ok:
                add("C", "long", _hold(r, n, c["hedge"]), exit_d, cal_days, f"score top fifth ({sp * 100:.0f}th pct)")
        if c["legs"]["D"] and sp is not None and sp <= 0.2:
            ok = True
            if c["filters"]["dStrongMarket"]:
                ok = (_f(r.get("spyVs200")) or 0) > 0 and (_f(r.get("spy63")) or 0) > 0
            if ok:
                add("D", "short", _hold(r, n, c["hedge"]), exit_d, cal_days, f"score bottom fifth ({sp * 100:.0f}th pct)")
    return summarize(trades, c)


def _stats(ts):
    if not ts:
        return {"n": 0}
    p = [t["pnl"] for t in ts]
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]
    m = sum(p) / len(p)
    sd = math.sqrt(sum((x - m) ** 2 for x in p) / (len(p) - 1)) if len(p) > 1 else 0
    return {"n": len(p), "total": round(sum(p), 2), "avg": round(m, 2), "win": len(wins) / len(p),
            "avgWin": round(sum(wins) / len(wins), 2) if wins else None, "avgLoss": round(sum(losses) / len(losses), 2) if losses else None,
            "profitFactor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "best": round(max(p), 2), "worst": round(min(p), 2), "t": round(m / (sd / math.sqrt(len(p))), 2) if sd else None}


def summarize(trades, c):
    trades.sort(key=lambda t: (t["exit"], t["entry"], t["sym"]))
    eq, cum, peak, dd, dd_at = [], 0.0, 0.0, 0.0, None
    by_day = {}
    for t in trades:
        by_day[t["exit"]] = by_day.get(t["exit"], 0.0) + t["pnl"]
    for d in sorted(by_day):
        cum += by_day[d]
        peak = max(peak, cum)
        if peak - cum > dd:
            dd, dd_at = peak - cum, d
        eq.append([d, round(cum, 2)])
    ev = []                                            # capital in use: open positions over time
    for t in trades:
        ev.append((t["entry"], 0, 1))
        ev.append(((_d(t["exit"]) + timedelta(days=1)).isoformat(), 0, -1))     # held through the exit day's close
    ev.sort()
    open_n, max_open, max_at, area, last_d = 0, 0, None, 0.0, None
    ev.sort(key=lambda e: (e[0], e[2]))                # same day: positions closed before new ones count
    for d, _, k in ev:
        if last_d is not None:
            area += open_n * (_d(d) - _d(last_d)).days
        last_d = d
        open_n += k
        if open_n > max_open:
            max_open, max_at = open_n, d
    span = (_d(ev[-1][0]) - _d(ev[0][0])).days if ev else 0
    avg_open = area / span if span else 0
    years = {}
    for t in trades:
        years.setdefault(t["exit"][:4], []).append(t)
    legs = {}
    for t in trades:
        legs.setdefault(t["leg"], []).append(t)
    st = _stats(trades)
    first, last = (eq[0][0], eq[-1][0]) if eq else (None, None)
    yrs = max(1e-9, ((_d(last) - _d(first)).days / 365.25)) if eq else None
    return {"cfg": c, **st, "maxDrawdown": round(dd, 2), "maxDrawdownAt": dd_at, "maxOpen": max_open, "maxOpenAt": max_at,
            "capitalNeeded": round(max_open * c["stake"], 2), "avgOpen": round(avg_open, 1), "avgCapital": round(avg_open * c["stake"], 2),
            "from": first, "to": last,
            "perYear": round(st.get("total", 0) / yrs, 2) if yrs else None,
            "returnOnCapital": round(st.get("total", 0) / (max_open * c["stake"]), 4) if max_open else None,
            "returnOnAvgCapitalPerYear": round(st.get("total", 0) / yrs / (avg_open * c["stake"]), 4) if yrs and avg_open else None,
            "years": [{"year": y, **_stats(ts)} for y, ts in sorted(years.items())],
            "legs": [{"leg": k, **_stats(v)} for k, v in sorted(legs.items())],
            "equity": eq[::max(1, len(eq) // 600)] + ([eq[-1]] if eq and len(eq) > 600 else []),
            "trades": trades[-3000:], "nTrades": len(trades)}
