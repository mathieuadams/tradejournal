"""Earnings strategy backtest and optimizer: what the event study's rules would have made, in dollars.

Four legs, each optional, each with its own settings and a fixed stake per trade (default $1,000):
  A  After the call, buy at the open: the "after the call" model's out-of-sample prediction is in the top `cut` of the
     previous year's predictions, and the release isn't "bad news + gap up". Buy at the day-0 open, sell at the close.
  B  Fade "bad news + gap up": post-release score in the bottom `band` and a gap above `gap`. Short at the open, cover at
     the close.
  C  Post-release long: score in the top `band`. Buy at the day-0 close, hold `hold` sessions (5, 20 or 40). Filters:
     the market weak going in (below its 200-day average or down over 3 months), day 0 up >2% on heavy volume (≥2.5×),
     the stock more than 25% below its 52-week high. Hedge: none, SPY 1:1 or beta × SPY.
  D  Post-release short: score in the bottom `band`. Short at the day-0 close, hold `hold` sessions. Filter: the market
     strong going in (above its 200-day average and up over 3 months). Hedge as C.

No look-ahead: the score uses signals ranked against earlier releases only, the "after the call" prediction is the
walk-forward (out-of-sample) one, and "top / bottom" is judged against the previous 365 days of releases.

The optimizer works like the Research one: every setting of a leg's grid is traded over the whole period, the trades
are split at a date (the first 60% of the period = train, the rest = test), a setting is ranked on the TRAIN trades
only (the t of the P/L per trade, at least MIN_TRADES trades), and the test columns show whether the ranking holds.
Costs: a round trip in basis points of the stake (default 10 = $1 per $1,000; doubled when hedged: the hedge is a second
trade), plus, for shorts, a borrow fee per year."""
import bisect
import itertools
import math
from datetime import date, timedelta

WINDOW = 365
MIN_PRIOR = 200
TRAIN = 0.6
MIN_TRADES = 60


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


LEG_DEFAULT = {"A": {"on": True, "cut": 0.2},
               "B": {"on": True, "band": 0.2, "gap": 0.02},
               "C": {"on": True, "band": 0.2, "hold": 40, "hedge": "spy", "weak": True, "heavy": False, "far": False},
               "D": {"on": True, "band": 0.2, "hold": 40, "hedge": "spy", "strong": True}}
DEFAULT = {"stake": 1000.0, "costBps": 10.0, "borrowPct": 1.0, "from": None, "to": None}
HOLDS = (5, 20, 40)
HEDGES = ("none", "spy", "beta")


def clean(cfg):
    cfg = cfg or {}
    c = {**DEFAULT, **{k: v for k, v in cfg.items() if k in DEFAULT}}
    c["stake"] = max(10.0, min(1e7, float(c["stake"])))
    c["costBps"] = max(0.0, min(500.0, float(c["costBps"])))
    c["borrowPct"] = max(0.0, min(50.0, float(c["borrowPct"])))
    legs = {}
    old_legs, old_f = cfg.get("legs") or {}, cfg.get("filters") or {}
    for k, d in LEG_DEFAULT.items():
        v = old_legs.get(k)
        leg = {**d, **(v if isinstance(v, dict) else {"on": v} if isinstance(v, bool) else {})}
        if k in "CD":                                  # the earlier, shared settings
            if "hold" in cfg and not isinstance(v, dict):
                leg["hold"] = cfg["hold"]
            if "hedge" in cfg and not isinstance(v, dict):
                leg["hedge"] = cfg["hedge"]
        if k == "C" and old_f and not isinstance(v, dict):
            leg.update(weak=bool(old_f.get("cWeakMarket")), heavy=bool(old_f.get("cHeavyUp")), far=bool(old_f.get("cFarFromHigh")))
        if k == "D" and old_f and not isinstance(v, dict):
            leg["strong"] = bool(old_f.get("dStrongMarket"))
        leg["on"] = bool(leg["on"])
        for f in ("band", "cut"):
            if f in leg:
                leg[f] = max(0.05, min(0.5, float(leg[f])))
        if "gap" in leg:
            leg["gap"] = max(0.0, min(0.2, float(leg["gap"])))
        if "hold" in leg:
            leg["hold"] = int(leg["hold"]) if int(leg["hold"]) in HOLDS else 40
        if "hedge" in leg and leg["hedge"] not in HEDGES:
            leg["hedge"] = "spy"
        legs[k] = leg
    c["legs"] = legs
    return c


def _f(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def _o2c(r):
    """Day-0 open → close of the stock alone (from the day-0 return and the gap)."""
    r1, gap = _f(r.get("r1")), _f(r.get("gap"))
    if r1 is None or gap is None or gap <= -1:
        return None
    return (1 + r1) / (1 + gap) - 1


def _hold(r, n, hedge):
    """Day-0 close → n sessions later: the stock alone, minus SPY, or minus beta × SPY."""
    r1, rn = _f(r.get("r1")), _f(r.get(f"r{n}"))
    if r1 is None or rn is None or r1 <= -1:
        return None
    raw = (1 + rn) / (1 + r1) - 1
    if hedge == "none":
        return raw
    ab = _f(r.get(f"drift{n}"))
    if hedge == "spy" or ab is None:
        return ab
    b = _f(r.get("beta"))
    return raw - (b if b is not None else 1.0) * (raw - ab)


def context(rows):
    """What every setting shares: the trailing percentiles of the score and of the model's prediction."""
    return {"score": trailing_pct(rows, "postScore"), "pred": trailing_pct(rows, "oosO2c")}


def trades_for(rows, ctx, c, only=None):
    """The trades of the enabled legs (or of one leg, `only`), oldest first."""
    stake, legs = c["stake"], c["legs"]
    base_cost, borrow_y = c["costBps"] / 1e4, c["borrowPct"] / 100
    score_p, pred_p = ctx["score"], ctx["pred"]
    out = []
    want = [only] if only else [k for k in "ABCD" if legs[k]["on"]]
    for i, r in enumerate(rows):
        d0 = r.get("day0")
        if not d0 or (c["from"] and d0 < c["from"]) or (c["to"] and d0 > c["to"]):
            continue
        sp, pp = score_p.get(i), pred_p.get(i)
        gap = _f(r.get("gap"))

        def add(leg, side, ret, exit_d, days, cost, why, xret=None):
            if ret is None or ret <= -1:
                return
            borrow = borrow_y * days / 365 if side == "short" else 0.0
            sg = 1 if side == "long" else -1
            t = {"leg": leg, "sym": r["sym"], "release": r["date"], "entry": d0, "exit": exit_d, "side": side, "ret": round(ret, 6),
                 "pnl": round(stake * (sg * ret - cost - borrow), 2), "why": why}
            if xret is not None:                       # the same trade's result beyond the market (vs SPY, as if hedged)
                xcost = base_cost * (2 if leg in "CD" else 1)
                t["alpha"] = round(stake * (sg * xret - xcost - borrow), 2)
            out.append(t)

        for k in want:
            L = legs[k]
            if k == "A":
                bad = sp is not None and sp <= legs["B"]["band"] and gap is not None and gap > legs["B"]["gap"]
                if pp is not None and pp >= 1 - L["cut"] and not bad:
                    add("A", "long", _o2c(r), d0, 1, base_cost, f"after-call model top {100 - pp * 100:.0f}%", _f(r.get("o2c")))
            elif k == "B":
                if sp is not None and sp <= L["band"] and gap is not None and gap > L["gap"]:
                    add("B", "short", _o2c(r), d0, 1, base_cost, f"score bottom {sp * 100:.0f}%, gap {gap * 100:+.1f}%", _f(r.get("o2c")))
            else:
                n = L["hold"]
                exit_d = r.get(f"t{n}")
                if not exit_d or sp is None:
                    continue
                cost = base_cost * (2 if L["hedge"] != "none" else 1)
                days = (_d(exit_d) - _d(d0)).days
                if k == "C" and sp >= 1 - L["band"]:
                    ok = True
                    if L["weak"]:
                        ok &= (_f(r.get("spyVs200")) or 0) < 0 or (_f(r.get("spy63")) or 0) < 0
                    if L["heavy"]:
                        ok &= (_f(r.get("r1ab")) or 0) > 0.02 and (_f(r.get("vol0")) or 0) >= 2.5
                    if L["far"]:
                        ok &= (_f(r.get("offHigh")) or 0) < -0.25
                    if ok:
                        add("C", "long", _hold(r, n, L["hedge"]), exit_d, days, cost, f"score top {100 - sp * 100:.0f}%", _hold(r, n, "spy"))
                elif k == "D" and sp <= L["band"]:
                    if not L["strong"] or ((_f(r.get("spyVs200")) or 0) > 0 and (_f(r.get("spy63")) or 0) > 0):
                        add("D", "short", _hold(r, n, L["hedge"]), exit_d, days, cost, f"score bottom {sp * 100:.0f}%", _hold(r, n, "spy"))
    return out


def split_date(rows):
    ds = sorted(r["day0"] for r in rows if r.get("day0"))
    if not ds:
        return None
    a, b = _d(ds[0]), _d(ds[-1])
    return (a + timedelta(days=int((b - a).days * TRAIN))).isoformat()


def run(rows, cfg, ctx=None):
    c = clean(cfg)
    ctx = ctx or context(rows)
    out = summarize(trades_for(rows, ctx, c), c)
    out["alpha"] = _stats(out["_all"], "alpha")
    sd = split_date(rows)
    if sd:
        tr = [t for t in out["_all"] if t["entry"] < sd]
        te = [t for t in out["_all"] if t["entry"] >= sd]
        out["split"] = {"date": sd, "train": _stats(tr), "test": _stats(te), "trainX": _stats(tr, "alpha"), "testX": _stats(te, "alpha")}
    out.pop("_all", None)
    return out


def _stats(ts, key="pnl"):
    if not ts:
        return {"n": 0}
    p = [t[key] for t in ts if t.get(key) is not None]
    if not p:
        return {"n": 0}
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]
    m = sum(p) / len(p)
    sd = math.sqrt(sum((x - m) ** 2 for x in p) / (len(p) - 1)) if len(p) > 1 else 0
    cum = peak = dd = 0.0
    for x in sorted(ts, key=lambda t: t["exit"]):
        cum += x.get(key) or 0
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": len(p), "total": round(sum(p), 2), "avg": round(m, 2), "win": len(wins) / len(p),
            "avgWin": round(sum(wins) / len(wins), 2) if wins else None, "avgLoss": round(sum(losses) / len(losses), 2) if losses else None,
            "profitFactor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "best": round(max(p), 2), "worst": round(min(p), 2), "t": round(m / (sd / math.sqrt(len(p))), 2) if sd else None,
            "dd": round(dd, 2)}


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
    ev.sort(key=lambda e: (e[0], e[2]))                # same day: positions closed before new ones count
    open_n, max_open, max_at, area, last_d = 0, 0, None, 0.0, None
    for d, _, k in ev:
        if last_d is not None:
            area += open_n * (_d(d) - _d(last_d)).days
        last_d = d
        open_n += k
        if open_n > max_open:
            max_open, max_at = open_n, d
    span = (_d(ev[-1][0]) - _d(ev[0][0])).days if ev else 0
    avg_open = area / span if span else 0
    years, legs = {}, {}
    for t in trades:
        years.setdefault(t["exit"][:4], []).append(t)
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
            "trades": trades[-3000:], "nTrades": len(trades), "_all": trades}


# ---------------------------------------------------------------- the optimizer

GRID = {
    "A": {"cut": [0.1, 0.2, 0.3]},
    "B": {"band": [0.1, 0.2, 0.3], "gap": [0.01, 0.02, 0.04]},
    "C": {"band": [0.1, 0.2, 0.3], "hold": [5, 20, 40], "hedge": ["none", "spy", "beta"], "weak": [False, True], "heavy": [False, True],
          "far": [False, True]},
    "D": {"band": [0.1, 0.2, 0.3], "hold": [5, 20, 40], "hedge": ["none", "spy", "beta"], "strong": [False, True]},
}


def combos(leg):
    g = GRID[leg]
    keys = list(g)
    return [dict(zip(keys, v)) for v in itertools.product(*[g[k] for k in keys])]


def optimize(rows, cfg, prog=None, deadline=None, done=None):
    """Every setting of every leg's grid, train / test split at 60% of the period, ranked on train (t of P/L per trade).
    `done`: rows already computed (a resumed run). Returns (rows, complete)."""
    import time
    c = clean(cfg)
    ctx = context(rows)
    sd = split_date(rows)
    res = list(done or [])
    seen = {(x["leg"], tuple(sorted(x["p"].items()))) for x in res}
    todo = [(leg, p) for leg in "ABCD" for p in combos(leg)]
    for n, (leg, p) in enumerate(todo):
        if (leg, tuple(sorted(p.items()))) in seen:
            continue
        if deadline and time.time() > deadline:
            return res, False
        if prog and n % 10 == 0:
            prog(f"Optimizing: setting {n + 1} of {len(todo)}")
        trial = {**c, "legs": {**c["legs"], leg: {**c["legs"][leg], **p, "on": True}}}
        ts = trades_for(rows, ctx, trial, only=leg)
        tr, te = [t for t in ts if t["entry"] < sd], [t for t in ts if t["entry"] >= sd]
        res.append({"leg": leg, "p": p, "train": _stats(tr), "test": _stats(te), "trainX": _stats(tr, "alpha"), "testX": _stats(te, "alpha")})
    return res, True


def ranked(res, on="alpha"):
    """Per leg, best first on the train t (settings with fewer than MIN_TRADES train trades last). on="alpha": the profit
    beyond the market (the same trades vs SPY) — an unhedged long in a rising market otherwise wins on the market alone;
    on="pnl": the profit as traded."""
    k = "trainX" if on == "alpha" else "train"
    out = {}
    for leg in "ABCD":
        xs = [x for x in res if x["leg"] == leg]
        xs.sort(key=lambda x: ((x["train"].get("n") or 0) >= MIN_TRADES, round((x.get(k) or {}).get("t") or -1e9, 2),
                              x["train"].get("t") or -1e9), reverse=True)   # ties (hedge choices): the better as traded
        out[leg] = xs
    return out


def opt_job(sub, prog=None, deadline=None):
    """The optimizer over the last study's releases (a background job); results in fund/u/<sub>/btopt."""
    import store
    from util import BadRequest, iso, now_ny
    rows = (store.get_json(f"fund/u/{sub}/study_rows") or {}).get("rows") or []
    if not rows or not any("postScore" in r for r in rows[-500:]):
        raise BadRequest("Run the event study first (once with this version).")
    prev = store.get_json(f"fund/u/{sub}/btopt") or {}
    cfg = prev.get("cfg") or {}
    res, ok = optimize(rows, cfg, prog, deadline, None if prev.get("complete") else prev.get("res"))
    store.put_json(f"fund/u/{sub}/btopt", {"at": iso(now_ny()), "cfg": clean(cfg), "split": split_date(rows), "res": res, "complete": ok,
                                           "grid": GRID, "total": sum(len(combos(k)) for k in GRID)})
    return ok


def opt_view(sub, on="alpha", top=12):
    import store
    o = store.get_json(f"fund/u/{sub}/btopt")
    if not o:
        return None
    rk = ranked(o["res"], on)
    return {k: v for k, v in o.items() if k != "res"} | {"on": on, "minTrades": MIN_TRADES, "legs": {k: v[:top] for k, v in rk.items()},
                                                          "done": len(o["res"])}
