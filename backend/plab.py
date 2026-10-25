"""Portfolio lab: the layered strategy built from the market studies, backtested as ONE portfolio (shares, daily candles).

Layers (each one a switch, so its value can be measured):
  1. risk dial (market, daily): alarm = VIX up 30%+ in 5 days or VIX above VIX3M -> no new trend entries for 5 days;
     caution = SPY under its 20-day EMA, breadth under 40% or credit risk-off (HYG/IEF under its 50-day) -> half risk;
     all clear = VIX back under VIX3M 3 days in a row after an alarm -> the rebound sleeve buys;
  2. regime filter: no new entries while the confirmed regime is Range · Volatile;
  3. where to look (weekly EMA 21/50/200 score, regime/rank): the stock's sector scored +1 or more; laggards (stock -1 / -2) skipped;
  4. trend sleeve (watchlist stocks): a pullback to the 21-day EMA that holds (low at / under it, green close above it,
     close above the 50-day); out on a close under the 50-day, the trailing stop, or after max days;
     rebound sleeve: SPY / QQQ / IWM / SMH bought on the all-clear day, held 20 days (or the stop);
  5. sizing: each trade risks riskPct of the equity (shares = risk / stop distance), at most maxPos positions and
     maxPct of the equity in one; no margin.
Signals at a close, fills at the next open (stops: the stop price, or the open if it gaps through), slippage on each fill.
Train = the first 60% of the days, test = the rest. Benchmarks: SPY held, and the watchlist equally weighted."""
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import db
from util import iso, now_ny

SK = "PLAB"
ETF_LIST = "__etf"                                     # the universe = SPY, QQQ, IWM and the sector ETFs
TRAIN = 0.6
PARAMS = {"years": 15, "list": "Main", "riskPct": 1.0, "maxPos": 10, "maxPct": 20.0, "stopAtr": 2.0, "trailAtr": 3.0, "maxDays": 40,
          "rebDays": 20, "rebStopAtr": 3.0, "slipBp": 5.0, "rebound": "SPY,QQQ,IWM,SMH",
          "trend": True, "reb": True, "alarm": True, "caution": True, "regime": True, "sector": True, "laggard": True}
LAYERS = [("alarm", "Risk dial: alarm blocks new trend entries"), ("caution", "Risk dial: caution = half risk"),
          ("regime", "Regime: no entries in Range · Volatile"), ("sector", "Sector scored +1 or more"),
          ("laggard", "Skip laggard stocks (-1 / -2)"), ("reb", "Rebound sleeve (all clear)"), ("trend", "Trend sleeve (pullbacks)")]


def clean(b):
    P = dict(PARAMS)
    for k, v in (b or {}).items():
        if k not in PARAMS:
            continue
        d = PARAMS[k]
        try:
            P[k] = bool(v) if isinstance(d, bool) else int(float(v)) if isinstance(d, int) else float(v) if isinstance(d, float) else str(v)[:200]
        except (TypeError, ValueError):
            pass
    P["years"] = max(3, min(20, P["years"]))
    P["maxPos"] = max(1, min(40, P["maxPos"]))
    return P


# ---------------------------------------------------------------- data (daily OHLC)
_D = {}


def _bars(sym, years):
    from charts import _yahoo
    now = now_ny().replace(tzinfo=None)
    k = (sym, years, now.strftime("%Y-%m-%d"))
    if k in _D:
        return _D[k]
    raw = _yahoo(sym if sym.startswith("^") else sym.replace(".", "-"), "1d", now - timedelta(days=int(years * 365.25) + 420), now + timedelta(days=1)) or []
    today = now.strftime("%Y-%m-%d")
    out = {b["t"][:10]: (b["o"], b["h"], b["l"], b["c"], b.get("v") or 0) for b in raw if b["t"][:10] < today or now.strftime("%H:%M") >= "16:05"}
    if len(_D) > 400:
        _D.clear()
    _D[k] = out
    return out


def _al(series, days):
    """[(o, h, l, c) or None] per day (no carry: a missing day is no trade)."""
    return [series.get(d) for d in days]


def _ema(xs, n):
    import macro
    return macro.ema(xs, n)


def _atr(rows, n=14):
    out, a, pc = [], None, None
    for r in rows:
        if r is None:
            out.append(a)
            continue
        o, h, l, c = r[:4]
        tr = h - l if pc is None else max(h - l, abs(h - pc), abs(l - pc))
        a = tr if a is None else (a * (n - 1) + tr) / n
        pc = c
        out.append(a)
    return out


# ---------------------------------------------------------------- the backtest
def prepare(data, days, stocks, sectors, years):
    """Everything the simulation reads, per day. data: {sym: {date: (o,h,l,c)}}."""
    import macro
    import rank
    import regime
    A = {}
    for s, ser in data.items():
        rows = _al(ser, days)
        cl = [r[3] if r else None for r in rows]
        clf = macro.align({d: v[3] for d, v in ser.items()}, days)
        A[s] = {"r": rows, "c": cl, "e21": _ema(clf, 21), "e50": _ema(clf, 50), "atr": _atr(rows), "sc": rank.scores(clf)}
    wk = [rank._wk(d) for d in days]
    wscore = {}
    for s in A:                                      # the score at the last close of the previous week (known on Monday)
        out, last, cur = [], None, None
        for i in range(len(days)):
            if i and wk[i] != wk[i - 1]:
                last = cur
            cur = A[s]["sc"][i][0] if A[s]["sc"][i] else cur
            out.append(last)
        wscore[s] = out
    cut = int(len(days) * TRAIN)
    sect = {}
    for s in stocks:                                 # the sector a stock follows, from the train days only
        if s in A:
            sect[s] = rank.sector_of(A[s]["c"][:cut], {x: A[x]["c"][:cut] for x in sectors if x in A})[0]
    vix = macro.align({d: v[3] for d, v in data.get("^VIX", {}).items()}, days)
    v3 = macro.align({d: v[3] for d, v in data.get("^VIX3M", {}).items()}, days)
    spy = A["SPY"]["c"]
    e20 = _ema(macro.align({d: v[3] for d, v in data["SPY"].items()}, days), 20)
    br = macro.breadth_of([{d: v[3] for d, v in data[s].items()} for s in stocks if s in data], days)
    hy, ie = data.get("HYG"), data.get("IEF")
    cr = macro.ratio(macro.align({d: v[3] for d, v in hy.items()}, days), macro.align({d: v[3] for d, v in ie.items()}, days)) if hy and ie else [None] * len(days)
    cre = _ema(cr, 50)
    alarm, caution, clear = [], [], []
    in_alarm, run, last_alarm = False, 0, -99
    for i in range(len(days)):
        vj = vix[i] is not None and i >= 5 and vix[i - 5] and vix[i] / vix[i - 5] - 1 >= 0.30
        inv = vix[i] is not None and v3[i] is not None and vix[i] > v3[i]
        a = bool(vj or inv)
        alarm.append(a)
        if a:
            in_alarm, run, last_alarm = True, 0, i
        ev = False
        if in_alarm and not a and vix[i] is not None and v3[i] is not None and vix[i] < v3[i]:
            run += 1
            if run >= 3:
                ev, in_alarm, run = True, False, 0
        clear.append(ev)
        caution.append(bool((spy[i] is not None and e20[i] is not None and spy[i] < e20[i]) or (br[i] is not None and br[i] < 0.4)
                            or (cr[i] is not None and cre[i] is not None and cr[i] < cre[i])))
    lab = regime.label_days({d: v[3] for d, v in data["SPY"].items()}, {d: v[3] for d, v in data.get("^VIX", {}).items()},
                            {d: v[3] for d, v in data.get("^VIX3M", {}).items()})
    rv = [(lab.get(d) or {}).get("label") == "Range · Volatile" for d in days]
    recent_alarm = [any(alarm[max(0, i - 5):i + 1]) for i in range(len(days))]
    return {"A": A, "w": wscore, "sect": sect, "alarm": recent_alarm, "caution": caution, "clear": clear, "rv": rv, "cut": cut}


def simulate(X, days, stocks, P, start):
    A, W = X["A"], X["w"]
    slip = P["slipBp"] / 10000
    reb = [s.strip().upper() for s in P["rebound"].split(",") if s.strip().upper() in A]
    cash, pos, pend, trades, eq = 100000.0, {}, [], [], []
    expo = []
    for i in range(start, len(days)):
        # 1. the orders of yesterday's close, at today's open
        for o in pend:
            s, r = o["sym"], A[o["sym"]]["r"][i]
            if r is None:
                continue
            if o["kind"] == "exit":
                p = pos.pop(s, None)
                if p:
                    px = r[0] * (1 - slip)
                    cash += p["sh"] * px
                    trades.append({**p, "exitAt": days[i], "exitPx": round(px, 2), "why": o["why"], "R": round((px - p["px"]) / p["risk"], 3)})
            elif s not in pos and len(pos) < P["maxPos"]:
                px = r[0] * (1 + slip)
                sh = int(o["risk$"] / o["dist"])
                sh = min(sh, int(o["eq"] * P["maxPct"] / 100 / px), int(cash / px))
                if sh <= 0:
                    continue
                cash -= sh * px
                pos[s] = {"sym": s, "sleeve": o["sleeve"], "at": days[i], "i": i, "px": round(px, 2), "sh": sh, "stop": px - o["dist"],
                          "risk": o["dist"], "hc": px}
        pend = []
        # 2. stops during the day, trailing at the close
        for s in list(pos):
            p, r = pos[s], A[s]["r"][i]
            if r is None:
                continue
            o, h, l, c = r[:4]
            if l <= p["stop"]:
                px = min(o, p["stop"]) * (1 - slip)
                cash += p["sh"] * px
                trades.append({**pos.pop(s), "exitAt": days[i], "exitPx": round(px, 2), "why": "stop", "R": round((px - p["px"]) / p["risk"], 3)})
                continue
            p["hc"] = max(p["hc"], c)
            a = A[s]["atr"][i]
            if p["sleeve"] == "trend" and a:
                p["stop"] = max(p["stop"], p["hc"] - P["trailAtr"] * a)
        # 3. exits at the close (filled tomorrow)
        for s, p in pos.items():
            c, e50 = A[s]["c"][i], A[s]["e50"][i]
            if c is None:
                continue
            held = i - p["i"]
            why = None
            if p["sleeve"] == "trend":
                why = "close under the 50-day" if e50 and c < e50 else "max days" if held >= P["maxDays"] else None
            elif held >= P["rebDays"]:
                why = f"held {P['rebDays']} days"
            if why:
                pend.append({"kind": "exit", "sym": s, "why": why})
        mv = sum(p["sh"] * (A[s]["c"][i] or p["px"]) for s, p in pos.items())
        equity = cash + mv
        eq.append(equity)
        expo.append(mv / equity if equity else 0)
        if i >= len(days) - 1:
            break
        # 4. new entries at the close
        if P["regime"] and X["rv"][i]:
            continue
        risk_pct = P["riskPct"] * (0.5 if P["caution"] and X["caution"][i] else 1.0)
        free = P["maxPos"] - len(pos) + sum(1 for o in pend if o["kind"] == "exit")
        cands = []
        if P["reb"] and X["clear"][i]:
            for s in reb:
                a, c = A[s]["atr"][i], A[s]["c"][i]
                if a and c and s not in pos:
                    cands.append((0, -1, s, "rebound", P["rebStopAtr"] * a))
        if P["trend"] and not (P["alarm"] and X["alarm"][i]):
            for s in stocks:
                if s not in A or s in pos:
                    continue
                r, e21, e50, a = A[s]["r"][i], A[s]["e21"][i], A[s]["e50"][i], A[s]["atr"][i]
                if not r or None in (e21, e50, a):
                    continue
                o, h, l, c = r[:4]
                if not (l <= e21 * 1.005 and c > e21 and c > o and c > e50):
                    continue
                ws = W[s][i]
                if P["laggard"] and ws is not None and ws <= -1:
                    continue
                se = X["sect"].get(s)
                if P["sector"] and (not se or W[se][i] is None or W[se][i] < 1):
                    continue
                mom = c / A[s]["c"][i - 20] - 1 if i >= 20 and A[s]["c"][i - 20] else 0
                cands.append((1, -mom, s, "trend", P["stopAtr"] * a))
        cands.sort()
        for _, _, s, sleeve, dist in cands[:max(0, free)]:
            pend.append({"kind": "entry", "sym": s, "sleeve": sleeve, "dist": dist, "risk$": equity * risk_pct / 100, "eq": equity})
    for s, p in pos.items():                          # open at the end: marked at the last close
        c = A[s]["c"][len(days) - 1] or p["px"]
        trades.append({**p, "exitAt": None, "exitPx": round(c, 2), "why": "open", "R": round((c - p["px"]) / p["risk"], 3)})
    for t in trades:
        t.pop("i", None)
        t.pop("hc", None)
        t["stop"] = round(t["stop"], 2)
        t["risk"] = round(t["risk"], 3)
    return eq, expo, trades


def metrics(eq, ds, expo=None):
    if len(eq) < 2:
        return {}
    rs = [b / a - 1 for a, b in zip(eq, eq[1:]) if a]
    m = sum(rs) / len(rs)
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / max(1, len(rs) - 1))
    pk, dd = eq[0], 0.0
    for x in eq:
        pk = max(pk, x)
        dd = min(dd, x / pk - 1)
    yrs = len(eq) / 252
    out = {"tot": round((eq[-1] / eq[0] - 1) * 100, 1), "cagr": round(((eq[-1] / eq[0]) ** (1 / yrs) - 1) * 100, 2) if yrs > 0 and eq[-1] > 0 else None,
           "dd": round(dd * 100, 1), "sharpe": round(m / sd * math.sqrt(252), 2) if sd else None, "from": ds[0], "to": ds[-1]}
    if expo:
        out["expo"] = round(sum(expo) / len(expo) * 100)
    return out


def tstats(trades, a, b):
    xs = [t for t in trades if a <= t["at"] < b and t["why"] != "open"]
    if not xs:
        return {"n": 0}
    rs = [t["R"] for t in xs]
    gw, gl = sum(r for r in rs if r > 0), -sum(r for r in rs if r <= 0)
    by = {}
    for t in xs:
        by.setdefault(t["sleeve"], []).append(t["R"])
    return {"n": len(xs), "win": round(sum(1 for r in rs if r > 0) / len(rs) * 100), "avgR": round(sum(rs) / len(rs), 3),
            "pf": round(gw / gl, 2) if gl else None,
            "sleeves": {k: {"n": len(v), "avgR": round(sum(v) / len(v), 3), "win": round(sum(1 for r in v if r > 0) / len(v) * 100)} for k, v in by.items()}}


def evaluate(X, days, stocks, P, start):
    eq, expo, trades = simulate(X, days, stocks, P, start)
    ds = days[start:start + len(eq)]
    cut = X["cut"] - start
    parts = {"all": (0, len(eq)), "train": (0, cut), "test": (cut, len(eq))}
    res = {k: {**metrics(eq[a:b], ds[a:b], expo[a:b]), **{"trades": tstats(trades, ds[a], ds[b] if b < len(ds) else "9999")}} for k, (a, b) in parts.items() if b - a > 20}
    return res, eq, trades


def bench(X, days, stocks, start):
    A = X["A"]
    spy = A["SPY"]["c"][start:]
    sp = [spy[0]]
    for a, b in zip(spy, spy[1:]):
        sp.append(sp[-1] * (b / a) if a and b else sp[-1])
    ew = [100000.0]
    for i in range(start + 1, len(days)):
        rs = [A[s]["c"][i] / A[s]["c"][i - 1] - 1 for s in stocks if s in A and A[s]["c"][i] and A[s]["c"][i - 1]]
        ew.append(ew[-1] * (1 + (sum(rs) / len(rs) if rs else 0)))
    return [x / sp[0] * 100000 for x in sp], ew


# ---------------------------------------------------------------- the job
def public(sub):
    it = db.get(db.upk(sub), SK) or {}
    out = json.loads(it["data"]) if it.get("data") else {}
    if out.get("running") and out["running"] < iso(now_ny() - timedelta(minutes=12)):
        out["running"] = None
    out.setdefault("cfg", dict(PARAMS))
    out["defaults"], out["layers"] = PARAMS, LAYERS
    return out


def _save(sub, x):
    x = {k: v for k, v in x.items() if k not in ("defaults", "layers")}
    db.put({"PK": db.upk(sub), "SK": SK, "data": json.dumps(x, default=str, separators=(",", ":"))})


def start(sub, b):
    cur = public(sub)
    if cur.get("running"):
        return cur
    x = {**cur, "running": iso(now_ny()), "step": "starting", "error": None, "cfg": clean((b or {}).get("cfg") or cur.get("cfg"))}
    _save(sub, x)
    import research
    try:
        research._invoke(sub, "plab", {})
    except Exception as e:
        x.update(running=None, step=None, error=f"couldn't start: {str(e)[:120]}")
        _save(sub, x)
    return public(sub)


def run(sub, budget_s=540):
    import regime
    import wallwatch
    t0 = time.time()
    x = public(sub)
    P = clean(x.get("cfg"))
    cfg = wallwatch.config(sub)
    lst = next((L for L in cfg["lists"] if L["name"] == P["list"]), None) or cfg["lists"][0]
    etfs = [m for m, _ in regime.MARKET + regime.SECTORS]
    stocks = list(etfs) if P["list"] == ETF_LIST else [s for s in lst["tickers"] if s not in etfs]   # ETFs: no hindsight in the pick
    syms = list(dict.fromkeys(etfs + stocks + ["^VIX", "^VIX3M", "HYG", "IEF"]))
    x.update(step=f"loading {len(syms)} daily histories")
    _save(sub, x)
    data = {}
    errs = []

    def one(s):
        try:
            return s, _bars(s, P["years"])
        except Exception:
            time.sleep(5)
            return s, _bars(s, P["years"])
    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in [ex.submit(one, s) for s in syms]:
            try:
                s, d = f.result(timeout=300)
                if d:
                    data[s] = d
            except Exception as e:
                errs.append(str(e)[:80])
    if "SPY" not in data or "^VIX" not in data:
        raise ValueError("SPY or VIX history couldn't be loaded.")
    days = sorted(data["SPY"])
    first = (now_ny() - timedelta(days=int(P["years"] * 365.25))).strftime("%Y-%m-%d")
    start_i = next((i for i, d in enumerate(days) if d >= first), None)
    if start_i is None or start_i < 200:
        start_i = min(max(200, start_i or 0), len(days) - 60)
    x["step"] = "the backtests (the strategy, then each layer switched off)"
    _save(sub, x)
    stocks = [s for s in stocks if s in data]
    X = prepare(data, days, stocks, [m for m, _ in regime.SECTORS], P["years"])
    X["cut"] = start_i + int((len(days) - start_i) * TRAIN)
    res, eq, trades = evaluate(X, days, stocks, P, start_i)
    abl = []
    for k, name in LAYERS:
        if time.time() - t0 > budget_s:
            break
        if not P[k]:
            continue
        r2, _, _ = evaluate(X, days, stocks, {**P, k: False}, start_i)
        abl.append({"layer": k, "name": name, "res": r2})
    if time.time() - t0 < budget_s:
        raw = {**P, **{k: False for k in ("alarm", "caution", "regime", "sector", "laggard", "reb")}, "trend": True}
        r3, _, _ = evaluate(X, days, stocks, raw, start_i)
        abl.append({"layer": "raw", "name": "Trend sleeve alone, no filter", "res": r3})
    sp, ew = bench(X, days, stocks, start_i)
    ds = days[start_i:]
    cut = X["cut"] - start_i
    bm = {}
    for nm, e in (("spy", sp), ("ew", ew)):
        bm[nm] = {k: metrics(e[a:b], ds[a:b]) for k, (a, b) in (("all", (0, len(e))), ("train", (0, cut)), ("test", (cut, len(e))))}
    step = max(1, len(eq) // 600)
    curve = [[ds[i], round(eq[i]), round(sp[i]), round(ew[i])] for i in range(0, len(eq), step)] + [[ds[len(eq) - 1], round(eq[-1]), round(sp[len(eq) - 1]), round(ew[len(eq) - 1])]]
    last = len(days) - 1
    now = {"alarm": X["alarm"][last], "caution": X["caution"][last], "rangeVolatile": X["rv"][last],
           "clearDays": next((last - i for i in range(last, max(0, last - 60), -1) if X["clear"][i]), None)}
    x.update(running=None, step=None, at=iso(now_ny()), cfg=P, res=res, ablation=abl, bench=bm, curve=curve, now=now,
             split={"from": ds[0], "cut": ds[cut] if cut < len(ds) else None, "to": ds[-1]}, nStocks=len(stocks), errors=errs[:5],
             trades=sorted(trades, key=lambda t: t["at"], reverse=True)[:400], nTrades=len(trades),
             sectors={s: X["sect"].get(s) for s in stocks})
    _save(sub, x)
    return x


def job(sub, params=None):
    try:
        return {"n": (run(sub).get("res") or {}).get("all", {}).get("trades", {}).get("n")}
    except Exception as e:
        x = public(sub)
        x.update(running=None, step=None, error=str(e)[:200])
        _save(sub, x)
        return {"error": str(e)[:200]}
