"""Regime optimizer: one strategy for each market phase — bull (SPY trend Up), sideways (Range), bear (Down) —
searched over every indicator and market-awareness rule, on the watchlist's daily candles.

A strategy (a 'genome') = a side (long / short), 1-4 entry rules that must all hold at a day's close, a stop in x ATR,
a trailing stop (or none), a max holding time and an exit on a close across the 21 / 50-day EMA (or none).
Entry at the next open; one trade at a time per ticker; the phase of the ENTRY day decides which strategy trades.
Each phase is scored on the trades it entered during the train days (the t of the R per day, smcopt.fitness, at least
the phase's minimum trades); the test days are only reported. A phase with nothing better than 0 stays in cash.
The best of each phase are then run together as one portfolio (capital, max positions, risk per trade).
Runs in the background, ~8 minutes per run, chained (one at a time)."""
import json
import math
import random
import time
from datetime import timedelta

import db
from util import iso, now_ny

SK = "ROPT"
VERSION = 2                                            # 2: scored on the edge over the phase's baseline
TRAIN = 0.6
TOP = 10
PHASES = (("bull", "Up", "Bull (SPY trend Up)"), ("side", "Range", "Sideways (Range)"), ("bear", "Down", "Bear (SPY trend Down)"))
NEED = {"bull": 120, "side": 60, "bear": 25}
LONG = {"pull21": "Pullback to EMA21 holds", "pull50": "Pullback to EMA50 holds", "break20": "New 20-day closing high",
        "above50": "Close above EMA50", "above200": "Close above EMA200", "stack": "EMA21 > 50 > 200", "rsi2": "RSI(2) under 10 (oversold)",
        "dip3": "3 down closes in a row", "kcOut": "Close under the Keltner lower band", "sqFire": "Squeeze fired up", "green": "Green candle",
        "inGreen": "Inertia green", "fvg": "Taps an open bullish FVG", "ws": "Weekly EMA score +1 / +2", "wsWeak": "Weekly EMA score -1 / -2",
        "sec": "Sector weekly score +1 / +2", "secWeak": "Sector weekly score -1 / -2"}
SHORT = {"pull21": "Rally to EMA21 fails", "pull50": "Rally to EMA50 fails", "break20": "New 20-day closing low",
         "above50": "Close under EMA50", "above200": "Close under EMA200", "stack": "EMA21 < 50 < 200", "rsi2": "RSI(2) over 90 (overbought)",
         "dip3": "3 up closes in a row", "kcOut": "Close over the Keltner upper band", "sqFire": "Squeeze fired down", "green": "Red candle",
         "inGreen": "Inertia red", "fvg": "Taps an open bearish FVG", "ws": "Weekly EMA score -1 / -2", "wsWeak": "Weekly EMA score +1 / +2",
         "sec": "Sector weekly score -1 / -2", "secWeak": "Sector weekly score +1 / +2"}
MARKET = {"mCalm": "Risk dial: no caution", "mCaution": "Risk dial: caution", "mNoAlarm": "No VIX alarm (5 days)", "mAlarm": "VIX alarm (5 days)",
          "mClear": "Within 10 days of an all clear", "mVixHi": "VIX 20+", "mVixLo": "VIX under 20", "mBrWeak": "Breadth under 40%",
          "mBrStrong": "Breadth 60%+", "mVolUp": "Volume 1.5x its 20-day average"}
RULES = list(LONG) + list(MARKET)
NUMS = {"stopAtr": [1.0, 1.5, 2.0, 3.0], "trailAtr": [0, 2.0, 3.0, 4.0], "maxDays": [5, 10, 20, 40], "exitEma": [0, 21, 50]}


def label(g):
    names = LONG if g["side"] == "long" else SHORT
    return [names.get(r) or MARKET.get(r) or r for r in g["rules"]]


def key(g):
    return json.dumps({**g, "rules": sorted(g["rules"])}, sort_keys=True)


def rand_g(rng, side=None):
    g = {"side": side or rng.choice(["long", "short"]), "rules": rng.sample(RULES, rng.randint(1, 3))}
    g.update({k: rng.choice(v) for k, v in NUMS.items()})
    return g


def mutate(g, rng):
    x = {**g, "rules": list(g["rules"])}
    m = rng.random()
    if m < 0.3 and len(x["rules"]) < 4:
        x["rules"].append(rng.choice([r for r in RULES if r not in x["rules"]]))
    elif m < 0.5 and len(x["rules"]) > 1:
        x["rules"].remove(rng.choice(x["rules"]))
    elif m < 0.7:
        x["rules"][rng.randrange(len(x["rules"]))] = rng.choice([r for r in RULES if r not in x["rules"]])
    elif m < 0.95:
        k = rng.choice(list(NUMS))
        x[k] = rng.choice([v for v in NUMS[k] if v != x[k]])
    else:
        x["side"] = "short" if x["side"] == "long" else "long"
    return x


# ---------------------------------------------------------------- features (per stock, per day, causal)
def _rsi2(cl):
    out, up, dn = [None] * len(cl), None, None
    for i in range(1, len(cl)):
        d = cl[i] - cl[i - 1]
        u, w = max(d, 0), max(-d, 0)
        up = u if up is None else (up + u) / 2
        dn = w if dn is None else (dn + w) / 2
        if i >= 3:
            out[i] = 100 if dn == 0 else 100 - 100 / (1 + up / dn)
    return out


def features(rows, wscore, secscore):
    """{"L": {rule: [bool]}, "S": {rule: [bool]}} on the days of `rows` ((o,h,l,c,v) or None), plus o/h/l/c/atr/e21/e50."""
    import macro
    import plab
    import wallwatch
    n = len(rows)
    idx = [i for i, r in enumerate(rows) if r]
    bars = [{"t": f"{i:06d}T09:30", "o": rows[i][0], "h": rows[i][1], "l": rows[i][2], "c": rows[i][3], "v": rows[i][4] if len(rows[i]) > 4 else 0} for i in idx]
    cl = [b["c"] for b in bars]
    e21, e50, e200 = macro.ema(cl, 21), macro.ema(cl, 50), macro.ema(cl, 200)
    atr = plab._atr([(b["o"], b["h"], b["l"], b["c"]) for b in bars])
    rsi = _rsi2(cl)
    sq = wallwatch.squeeze(bars, 20, 2.0, 1.5, 3) if len(bars) > 45 else [None] * len(bars)
    inr = wallwatch.inertia_hourly(bars, (70, 40, 20)) if len(bars) > 75 else [None] * len(bars)
    zones = wallwatch.fvg_zones(bars, 0.2, None, 10) if len(bars) > 3 else []
    tapL, tapS = [False] * len(bars), [False] * len(bars)
    for z in zones:
        end = z["fillI"] if z["fillI"] is not None else len(bars)
        for j in range(z["i"] + 1, min(end + 1, len(bars))):
            b = bars[j]
            if z["type"] == "bull" and b["l"] <= z["hi"] and b["c"] > z["lo"]:
                tapL[j] = True
            elif z["type"] == "bear" and b["h"] >= z["lo"] and b["c"] < z["hi"]:
                tapS[j] = True
    vavg = macro.ema([b["v"] or 0 for b in bars], 20)
    L = {k: [False] * n for k in LONG}
    Sh = {k: [False] * n for k in SHORT}
    vol_up = [False] * n
    O, H, Lo, C, A, E21, E50 = ([None] * n for _ in range(7))
    for j, i in enumerate(idx):
        b = bars[j]
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]
        O[i], H[i], Lo[i], C[i], A[i], E21[i], E50[i] = o, h, l, c, atr[j], e21[j], e50[j]
        if None in (e21[j], e50[j], e200[j], atr[j]) or j < 21:
            continue
        hi20, lo20 = max(cl[j - 20:j]), min(cl[j - 20:j])
        kc_lo, kc_hi = e21[j] - 2 * atr[j], e21[j] + 2 * atr[j]
        s_ = sq[j]
        ws, ss = wscore[i], secscore[i] if secscore else None
        L["pull21"][i] = l <= e21[j] * 1.005 and c > e21[j] and c > o
        L["pull50"][i] = l <= e50[j] * 1.005 and c > e50[j] and c > o
        L["break20"][i] = c > hi20
        L["above50"][i], L["above200"][i] = c > e50[j], c > e200[j]
        L["stack"][i] = e21[j] > e50[j] > e200[j]
        L["rsi2"][i] = rsi[j] is not None and rsi[j] < 10
        L["dip3"][i] = j >= 3 and cl[j] < cl[j - 1] < cl[j - 2] < cl[j - 3]
        L["kcOut"][i] = c < kc_lo
        L["sqFire"][i] = bool(s_ and s_["fired"] and s_["mom"] > 0)
        L["green"][i] = c > o
        L["inGreen"][i] = bool(inr[j] and inr[j]["s"] == "G")
        L["fvg"][i] = tapL[j]
        L["ws"][i], L["wsWeak"][i] = ws is not None and ws >= 1, ws is not None and ws <= -1
        L["sec"][i], L["secWeak"][i] = ss is not None and ss >= 1, ss is not None and ss <= -1
        Sh["pull21"][i] = h >= e21[j] * 0.995 and c < e21[j] and c < o
        Sh["pull50"][i] = h >= e50[j] * 0.995 and c < e50[j] and c < o
        Sh["break20"][i] = c < lo20
        Sh["above50"][i], Sh["above200"][i] = c < e50[j], c < e200[j]
        Sh["stack"][i] = e21[j] < e50[j] < e200[j]
        Sh["rsi2"][i] = rsi[j] is not None and rsi[j] > 90
        Sh["dip3"][i] = j >= 3 and cl[j] > cl[j - 1] > cl[j - 2] > cl[j - 3]
        Sh["kcOut"][i] = c > kc_hi
        Sh["sqFire"][i] = bool(s_ and s_["fired"] and s_["mom"] < 0)
        Sh["green"][i] = c < o
        Sh["inGreen"][i] = bool(inr[j] and inr[j]["s"] == "R")
        Sh["fvg"][i] = tapS[j]
        Sh["ws"][i], Sh["wsWeak"][i] = L["wsWeak"][i], L["ws"][i]
        Sh["sec"][i], Sh["secWeak"][i] = L["secWeak"][i], L["sec"][i]
        vol_up[i] = bool(vavg[j - 1] and b["v"] > 1.5 * vavg[j - 1]) if j else False
    return {"L": L, "S": Sh, "volUp": vol_up, "o": O, "h": H, "l": Lo, "c": C, "atr": A, "e21": E21, "e50": E50}


def market(X, days):
    """{rule: [bool]} of the market-wide rules (risk dial, VIX, breadth) from plab.prepare's arrays."""
    n = len(days)
    al, ca, cl = X["alarm"], X["caution"], X["clear"]
    last_clear, clear10 = -99, []
    for i in range(n):
        if cl[i]:
            last_clear = i
        clear10.append(i - last_clear <= 10)
    vix, br = X.get("vix") or [None] * n, X.get("breadth") or [None] * n
    return {"mCalm": [not x for x in ca], "mCaution": list(ca), "mNoAlarm": [not x for x in al], "mAlarm": list(al), "mClear": clear10,
            "mVixHi": [v is not None and v >= 20 for v in vix], "mVixLo": [v is not None and v < 20 for v in vix],
            "mBrWeak": [b is not None and b < 0.4 for b in br], "mBrStrong": [b is not None and b >= 0.6 for b in br]}


def build(data, days, stocks, sectors):
    import macro
    import plab
    import regime
    X = plab.prepare(data, days, stocks, sectors, 15)
    X["vix"] = macro.align({d: v[3] for d, v in data["^VIX"].items()}, days)
    X["breadth"] = macro.breadth_of([{d: v[3] for d, v in data[s].items()} for s in stocks if s in data], days)
    F = {}
    for s in stocks:
        if s not in X["A"]:
            continue
        se = X["sect"].get(s)
        F[s] = features(X["A"][s]["r"], X["w"][s], X["w"].get(se) if se else None)
    M = market(X, days)
    lab = regime.label_days({d: v[3] for d, v in data["SPY"].items()}, {d: v[3] for d, v in data["^VIX"].items()},
                            {d: v[3] for d, v in data.get("^VIX3M", {}).items()})
    phase = []
    for d in days:
        t = (lab.get(d) or {}).get("trend")
        phase.append({"Up": "bull", "Range": "side", "Down": "bear"}.get(t))
    return {"X": X, "F": F, "M": M, "phase": phase}


# ---------------------------------------------------------------- one strategy, trade by trade
def trade(f, i, g, n):
    """The trade of a signal at the close of day i: (exit day index, R) or None."""
    d = 1 if g["side"] == "long" else -1
    j = i + 1
    if j >= n or f["o"][j] is None or not f["atr"][i]:
        return None
    px, a = f["o"][j], f["atr"][i]
    risk = g["stopAtr"] * a
    stop, ext = px - d * risk, px
    for k in range(j, min(n, j + int(g["maxDays"]) + 1)):
        o, h, l, c = f["o"][k], f["h"][k], f["l"][k], f["c"][k]
        if c is None:
            continue
        if (d > 0 and l <= stop) or (d < 0 and h >= stop):
            ex = min(o, stop) if d > 0 else max(o, stop)
            return k, d * (ex - px) / risk
        ext = max(ext, c) if d > 0 else min(ext, c)
        if g["trailAtr"] and f["atr"][k]:
            ts = ext - d * g["trailAtr"] * f["atr"][k]
            stop = max(stop, ts) if d > 0 else min(stop, ts)
        e = f["e21"][k] if g["exitEma"] == 21 else f["e50"][k] if g["exitEma"] == 50 else None
        if (e and d * (c - e) < 0) or k - j >= g["maxDays"]:
            nx = k + 1
            ex = f["o"][nx] if nx < n and f["o"][nx] is not None else c
            return nx, d * (ex - px) / risk
    return None


def signals(B, g, s, i):
    f = B["F"][s]
    side = f["L"] if g["side"] == "long" else f["S"]
    for r in g["rules"]:
        arr = f["volUp"] if r == "mVolUp" else B["M"].get(r) if r in MARKET else side.get(r)
        if arr is None or not arr[i]:
            return False
    return True


def trades_of(B, g, ph, a, b, days):
    """[(date, sym, R, side)] of the strategy, entries on days [a, b) in phase ph."""
    out = []
    n = len(days)
    for s, f in B["F"].items():
        nxt = a
        for i in range(a, b):
            if i < nxt or B["phase"][i] != ph or not signals(B, g, s, i):
                continue
            t = trade(f, i, g, n)
            if not t:
                continue
            nxt = t[0]
            out.append((days[i], s, round(t[1], 3), g["side"]))
    return out


def stats(tr):
    import smcopt
    return smcopt.score(tr, cost=0)


def baseline(B, g, ph, a, b, days):
    """The same side and exits, entered on EVERY day of the phase (one trade at a time per ticker): what simply being
    long (or short) the watchlist made. A strategy's edge = its R minus this average R."""
    k = (ph, g["side"], g["stopAtr"], g["trailAtr"], g["maxDays"], g["exitEma"], a, b)
    c = B.setdefault("base", {})
    if k not in c:
        tr = trades_of(B, {**g, "rules": []}, ph, a, b, days)
        rs = [t[2] for t in tr]
        c[k] = {"n": len(rs), "avgR": round(sum(rs) / len(rs), 4) if rs else 0.0, "win": round(sum(1 for r in rs if r > 0) / len(rs) * 100) if rs else None}
    return c[k]


def edge_stats(B, g, ph, a, b, days):
    """(raw stats, baseline, stats of R - baseline avg R) of a strategy on days [a, b)."""
    tr = trades_of(B, g, ph, a, b, days)
    base = baseline(B, g, ph, a, b, days)
    return stats(tr), base, stats([(d, s_, r - base["avgR"], sd) for d, s_, r, sd in tr])


# ---------------------------------------------------------------- the combined portfolio (capital, max positions)
def portfolio(B, best, days, a, P):
    X, F = B["X"], B["F"]
    slip = P.get("slipBp", 5) / 10000
    cash, pos, pend, eq = 100000.0, {}, [], []
    n = len(days)
    for i in range(a, n):
        for o in pend:
            f = F[o["sym"]]
            if f["o"][i] is None or o["sym"] in pos or len(pos) >= P["maxPos"]:
                continue
            d = o["d"]
            px = f["o"][i] * (1 + d * slip)
            sh = min(int(o["risk$"] / o["dist"]), int(o["eq"] * P["maxPct"] / 100 / px), int(cash / px))
            if sh <= 0:
                continue
            cash -= sh * px
            pos[o["sym"]] = {"d": d, "px": px, "sh": sh, "stop": px - d * o["dist"], "ext": px, "i": i, "g": o["g"]}
        pend = []
        for s in list(pos):
            p, f = pos[s], F[s]
            o_, h, l, c = f["o"][i], f["h"][i], f["l"][i], f["c"][i]
            if c is None:
                continue
            d, g = p["d"], p["g"]
            ex = None
            if (d > 0 and l <= p["stop"]) or (d < 0 and h >= p["stop"]):
                ex = (min(o_, p["stop"]) if d > 0 else max(o_, p["stop"]))
            else:
                p["ext"] = max(p["ext"], c) if d > 0 else min(p["ext"], c)
                if g["trailAtr"] and f["atr"][i]:
                    ts = p["ext"] - d * g["trailAtr"] * f["atr"][i]
                    p["stop"] = max(p["stop"], ts) if d > 0 else min(p["stop"], ts)
                e = f["e21"][i] if g["exitEma"] == 21 else f["e50"][i] if g["exitEma"] == 50 else None
                if (e and d * (c - e) < 0) or i - p["i"] >= g["maxDays"]:
                    p["out"] = True
            if ex is not None:
                cash += p["sh"] * (p["px"] + d * (ex * (1 - d * slip) - p["px"]))
                pos.pop(s)
        for s in [s for s, p in pos.items() if p.get("out")]:
            f, p = F[s], pos[s]
            nx = f["o"][i + 1] if i + 1 < n and f["o"][i + 1] is not None else f["c"][i]
            cash += p["sh"] * (p["px"] + p["d"] * (nx * (1 - p["d"] * slip) - p["px"]))
            pos.pop(s)
        val = sum(p["sh"] * (p["px"] + p["d"] * ((F[s]["c"][i] or p["px"]) - p["px"])) for s, p in pos.items())
        eq.append(cash + val)
        ph = B["phase"][i]
        g = best.get(ph)
        if not g or i >= n - 1:
            continue
        if P.get("alarmBlock") and X["alarm"][i]:
            continue
        risk = P["riskPct"] / 100 * eq[-1] * (P.get("cautionMult", 1.0) if X["caution"][i] else 1.0)
        free = P["maxPos"] - len(pos)
        cands = []
        for s, f in F.items():
            if s in pos or not f["atr"][i] or not signals(B, g, s, i):
                continue
            mom = (f["c"][i] / f["c"][i - 20] - 1) if i >= 20 and f["c"][i] and f["c"][i - 20] else 0
            cands.append((-mom if g["side"] == "long" else mom, s))
        for _, s in sorted(cands)[:max(0, free)]:
            pend.append({"sym": s, "d": 1 if g["side"] == "long" else -1, "dist": g["stopAtr"] * F[s]["atr"][i], "risk$": risk, "eq": eq[-1], "g": g})
    return eq


# ---------------------------------------------------------------- state / run
def state(sub):
    r = db.get(db.upk(sub), SK) or {}
    st = json.loads(r["data"]) if r.get("data") else {}
    if st.get("v") != VERSION:
        st.update(top={p: [] for p, _, _ in PHASES}, seen={}, evals=0, log=[], combined=None)
    st["v"] = VERSION
    for k, v in (("on", False), ("years", 15), ("list", "Main"), ("top", {p: [] for p, _, _ in PHASES}), ("seen", {}), ("evals", 0), ("log", []),
                 ("pf", {"riskPct": 1.0, "maxPos": 10, "maxPct": 20.0, "slipBp": 5.0})):
        st.setdefault(k, v)
    return st


USER = ("on", "years", "list", "stopReq", "resetAt", "pf")


def _save(sub, st, user=False):
    if not user:
        cur = db.get(db.upk(sub), SK)
        if cur and cur.get("data"):
            try:
                live = json.loads(cur["data"])
                for k in USER:
                    if k in live:
                        st[k] = live[k]
            except ValueError:
                pass
        if st.get("resetAt") and st["resetAt"] > (st.get("runStarted") or ""):
            st.update(top={p: [] for p, _, _ in PHASES}, seen={}, evals=0, log=[], combined=None)
    data = json.dumps(st, default=str, separators=(",", ":"))
    while len(data) > 330_000 and st["seen"]:
        st["seen"] = dict(list(st["seen"].items())[-int(len(st["seen"]) * 0.6):])
        data = json.dumps(st, default=str, separators=(",", ":"))
    db.put({"PK": db.upk(sub), "SK": SK, "data": data})


def public(sub):
    st = state(sub)
    if st.get("running") and st["running"] < iso(now_ny() - timedelta(minutes=12)):
        st["running"] = None
    return {k: v for k, v in st.items() if k != "seen"} | {"phases": [{"key": k, "trend": t, "name": n, "need": NEED[k]} for k, t, n in PHASES],
                                                         "rules": {"long": LONG, "short": SHORT, "market": MARKET}}


def save_cfg(sub, b):
    st = state(sub)
    b = b or {}
    if "on" in b:
        st["on"] = bool(b["on"])
        if not st["on"] and st.get("running"):
            st["stopReq"] = iso(now_ny())
        if st["on"]:
            st.pop("stopReq", None)
    for k in ("years", "list"):
        if b.get(k) and b[k] != st.get(k):
            st[k] = max(5, min(20, int(b[k]))) if k == "years" else str(b[k])[:60]
            b["reset"] = True
    if isinstance(b.get("pf"), dict):
        st["pf"] = {**st["pf"], **{k: float(v) for k, v in b["pf"].items() if k in st["pf"]}}
    if b.get("reset"):
        st.update(top={p: [] for p, _, _ in PHASES}, seen={}, evals=0, log=[], combined=None, resetAt=iso(now_ny()))
    _save(sub, st, user=True)
    return public(sub)


_B = {}


def load(sub, st):
    import plab
    import regime
    import wallwatch
    from concurrent.futures import ThreadPoolExecutor
    cfg = wallwatch.config(sub)
    lst = next((L for L in cfg["lists"] if L["name"] == st["list"]), None) or cfg["lists"][0]
    etfs = [m for m, _ in regime.MARKET + regime.SECTORS]
    stocks = [s for s in lst["tickers"] if s not in etfs]
    k = (sub, st["years"], tuple(stocks), now_ny().strftime("%Y-%m-%d"))
    if _B.get("k") == k:
        return _B["v"]
    data = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for s, f in [(s, ex.submit(plab._bars, s, st["years"])) for s in etfs + stocks + ["^VIX", "^VIX3M", "HYG", "IEF"]]:
            try:
                d = f.result(timeout=300)
                if d:
                    data[s] = d
            except Exception:
                pass
    if "SPY" not in data or "^VIX" not in data:
        raise ValueError("SPY or VIX history couldn't be loaded.")
    days = sorted(data["SPY"])
    stocks = [s for s in stocks if s in data]
    B = build(data, days, stocks, [m for m, _ in regime.SECTORS])
    first = (now_ny() - timedelta(days=int(st["years"] * 365.25))).strftime("%Y-%m-%d")
    a = max(200, next((i for i, d in enumerate(days) if d >= first), 200))
    B.update(days=days, a=a, cut=a + int((len(days) - a) * TRAIN), n=len(days), stocks=stocks)
    _B.update(k=k, v=B)
    return B


def run(sub, budget_s=470, seed=None):
    import smcopt
    t0 = time.time()
    rng = random.Random(seed)
    st = state(sub)
    st["running"] = st["runStarted"] = iso(now_ny())
    st["step"] = "loading the daily candles and the indicators"
    st.pop("stopReq", None)
    _save(sub, st)
    B = load(sub, st)
    days, a, cut, n = B["days"], B["a"], B["cut"], B["n"]
    st.update(step="searching", split={"from": days[a], "cut": days[cut], "to": days[-1]}, nStocks=len(B["F"]),
              phaseDays={p: sum(1 for i in range(a, n) if B["phase"][i] == p) for p, _, _ in PHASES})
    seen, top = st["seen"], st["top"]
    n_run = 0
    seeds = {"bull": {"side": "long", "rules": ["pull21", "above50", "sec"], "stopAtr": 2.0, "trailAtr": 3.0, "maxDays": 40, "exitEma": 50},
             "side": {"side": "long", "rules": ["rsi2", "above200"], "stopAtr": 2.0, "trailAtr": 0, "maxDays": 5, "exitEma": 0},
             "bear": {"side": "long", "rules": ["mClear"], "stopAtr": 3.0, "trailAtr": 0, "maxDays": 20, "exitEma": 0}}

    def ev(ph, g):
        nonlocal n_run
        k = ph + key(g)
        if k in seen:
            return seen[k]
        r, base, ed = edge_stats(B, g, ph, a, cut, days)
        f = smcopt.fitness({**ed, "n": r.get("n", 0)}, NEED[ph])
        seen[k] = f
        st["evals"] += 1
        n_run += 1
        T = top[ph]
        same = any((x["train"] or {}).get("n") == r.get("n") and (x["train"] or {}).get("totR") == r.get("totR") for x in T)
        if not same and (len(T) < TOP or f > T[-1]["fit"]):
            te, tb, tx = edge_stats(B, g, ph, cut, n, days)
            T.append({"g": g, "fit": f, "train": {**_slim(r), "base": base["avgR"], "edge": ed.get("avgR"), "edgeT": ed.get("t")},
                      "test": {**_slim(te), "base": tb["avgR"], "edge": tx.get("avgR"), "edgeT": tx.get("t")}, "rules": label(g), "at": iso(now_ny())})
            T.sort(key=lambda x: -x["fit"])
            del T[TOP:]
        return f

    last_save = time.time()
    k_ph = 0
    reserve = min(60.0, budget_s * 0.2)
    while time.time() - t0 < budget_s - reserve:          # the last minute: the portfolio search
        ph = PHASES[k_ph % 3][0]
        k_ph += 1
        T = top[ph]
        if not T:
            g = seeds[ph]
        elif rng.random() < 0.25:
            g = rand_g(rng)
        else:
            g = mutate(rng.choice(T[:3] if rng.random() < 0.6 else T)["g"], rng)
        cur, f = g, ev(ph, g)
        for _ in range(4):                             # a short climb from there
            if time.time() - t0 > budget_s:
                break
            x = mutate(cur, rng)
            fx = ev(ph, x)
            if fx >= f:
                cur, f = x, fx
        if time.time() - last_save > 45:
            cs = state(sub)
            if not cs["on"] or (cs.get("stopReq") or "") > st["runStarted"] or (cs.get("resetAt") or "") > st["runStarted"]:
                break
            _save(sub, st)
            st = {**st, **{k: v for k, v in state(sub).items() if k in USER}}
            seen, top = st["seen"], st["top"]
            last_save = time.time()
    # the portfolio: which strategy (of the best 3, or cash) per phase, the sizing and the risk-dial cuts, chosen on
    # the Sharpe of the train days; the test days only shown
    import plab
    sp = [B["X"]["A"]["SPY"]["c"][i] for i in range(a, n)]
    spn = [x / sp[0] * 100000 if x else None for x in sp]
    for i in range(1, len(spn)):
        spn[i] = spn[i] or spn[i - 1]
    c = cut - a
    ds = days[a:]
    parts = (("all", (0, n - a)), ("train", (0, c)), ("test", (c, n - a)))
    st["step"] = "the portfolio: strategies per phase, sizing, risk-dial cuts"
    opts = {p: [None] + [x["g"] for x in top[p][:3] if x["fit"] > 0] for p, _, _ in PHASES}
    base_pf = {"riskPct": 1.0, "maxPos": 10, "maxPct": 20.0, "slipBp": 5.0, "cautionMult": 1.0, "alarmBlock": False, **(st.get("pf") or {})}
    tried, pfs = set(), []

    def pf_eval(choice, P):
        kk = json.dumps([choice, P], sort_keys=True)
        if kk in tried:
            return
        tried.add(kk)
        best = {p: (opts[p][choice[p]] if choice[p] < len(opts[p]) else None) for p in opts}
        eq = portfolio(B, best, days, a, P)
        res = {k: plab.metrics(eq[x:y], ds[x:y]) for k, (x, y) in parts}
        pfs.append({"choice": {p: (label(best[p]) if best[p] else None) for p in best}, "pick": choice, "P": P, "res": res,
                    "fit": (res["train"].get("sharpe") or -9), "_eq": eq})
    first = {p: (1 if len(opts[p]) > 1 else 0) for p in opts}
    pf_eval(first, base_pf)
    t1 = time.time()
    while time.time() - t1 < reserve * 0.8 and time.time() - t0 < budget_s + 30:
        ch = {p: rng.randrange(len(opts[p])) for p in opts}
        P = {**base_pf, "riskPct": rng.choice([0.5, 0.75, 1.0, 1.5]), "maxPos": rng.choice([5, 8, 10, 15, 20]), "maxPct": rng.choice([10.0, 15.0, 20.0, 30.0]),
             "cautionMult": rng.choice([1.0, 0.5, 0.0]), "alarmBlock": rng.choice([False, True])}
        pf_eval(ch, P)
    pfs.sort(key=lambda x: -x["fit"])
    top_pf = pfs[0]
    eq = top_pf["_eq"]
    st["combined"] = {"best": top_pf["choice"], "P": top_pf["P"], "res": top_pf["res"],
                      "spy": {k: plab.metrics(spn[x:y], ds[x:y]) for k, (x, y) in parts},
                      "curve": [[ds[i], round(eq[i]), round(spn[i]), round(spn[i])] for i in range(0, len(eq), max(1, len(eq) // 500))],
                      "tried": len(pfs), "others": [{k: v for k, v in x.items() if k != "_eq"} for x in pfs[:8]]}
    st.update(running=None, step=None, runs=st.get("runs", 0) + 1)
    st["log"] = (st["log"] + [{"at": iso(now_ny()), "tried": n_run, "best": {p: (top[p][0]["fit"] if top[p] else None) for p, _, _ in PHASES}}])[-20:]
    _save(sub, st)
    return st


def _slim(r):
    return {k: r.get(k) for k in ("n", "days", "totR", "avgR", "win", "pf", "t", "dd", "monthShare")}


def job(sub, params=None):
    import research
    pk = db.upk(sub)
    if not db.try_lock(pk, SK + "#LOCK", 560):
        return {"skipped": "a run is already going"}
    try:
        run(sub)
    except Exception as e:
        db.unlock(pk, SK + "#LOCK")
        st = state(sub)
        st.update(running=None, step=None)
        st["log"] = (st.get("log") or [])[-19:] + [{"at": iso(now_ny()), "error": str(e)[:160]}]
        _save(sub, st, user=True)
        return {"error": str(e)[:200]}
    db.unlock(pk, SK + "#LOCK")
    st = state(sub)
    if st["on"] and not (params or {}).get("once"):
        research._invoke(sub, "ropt", {})
    return {"evals": st["evals"]}


def kick():
    import research
    n = 0
    for it in db.scan_sk(SK):
        try:
            st = json.loads(it["data"])
        except (KeyError, ValueError):
            continue
        if not st.get("on") or (st.get("running") and st["running"] > iso(now_ny() - timedelta(minutes=12))):
            continue
        research._invoke(it["PK"][5:], "ropt", {})
        n += 1
    return n
