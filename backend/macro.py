"""Market awareness, on top of the regimes (regime.py): what bonds, rates, credit, the dollar, commodities and breadth
are doing, how the market and the sectors moved afterwards in each of those states, and how fast each early-warning
signal caught the selloffs (and the recoveries) vs the confirmed regime.

Every state of a day uses only the closes up to that day. Forward moves: the next 5 / 20 / 60 trading days."""
import math

SYMS = ["^TNX", "^IRX", "TLT", "IEF", "HYG", "LQD", "UUP", "GLD", "USO", "HG=F", "GC=F", "RSP"]
ASSETS = [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("IWM", "Russell 2000"), ("SMH", "Semiconductors"), ("XLK", "Technology"), ("XLC", "Communication"),
          ("XLY", "Consumer discr."), ("XLF", "Financials"), ("XLV", "Health care"), ("XLI", "Industrials"), ("XLE", "Energy"), ("XLB", "Materials"),
          ("XLP", "Consumer staples"), ("XLU", "Utilities"), ("XLRE", "Real estate"), ("TLT", "Long Treasuries"), ("HYG", "High yield bonds"),
          ("GLD", "Gold"), ("UUP", "US dollar"), ("USO", "Oil")]
HZ = (5, 20, 60)
CONFIRM = 3
DD = 0.08                                             # a selloff = SPY down at least 8% from its high


# ---------------------------------------------------------------- helpers on the trading days of SPY
def align(series, days):
    """[value or None] per day, the last close carried over at most 3 days (holidays of other markets)."""
    out, last, age = [], None, 99
    for d in days:
        v = series.get(d) if series else None
        if v is not None:
            last, age = v, 0
        else:
            age += 1
        out.append(last if age <= 3 else None)
    return out


def ema(xs, n):
    out, e, k, cnt = [], None, 2 / (n + 1), 0
    for x in xs:
        if x is None:
            out.append(e)
            continue
        cnt += 1
        e = x if e is None else x * k + e * (1 - k)
        out.append(e if cnt >= n else None)
    return out


def chg(xs, n, pct=True):
    return [None if i < n or xs[i] is None or xs[i - n] in (None, 0) else (xs[i] / xs[i - n] - 1 if pct else xs[i] - xs[i - n])
            for i in range(len(xs))]


def ratio(a, b):
    return [x / y if x is not None and y not in (None, 0) else None for x, y in zip(a, b)]


def confirm(raw, n=CONFIRM):
    out, cur, cand, run = [], None, None, 0
    for x in raw:
        if x is None:
            out.append(cur)
            continue
        if cur is None:
            cur = x
        elif x == cur:
            cand, run = None, 0
        else:
            run = run + 1 if x == cand else 1
            cand = x
            if run >= n:
                cur, cand, run = x, None, 0
        out.append(cur)
    return out


def above(xs, n=50, up="up", dn="down"):
    e = ema(xs, n)
    return [None if x is None or m is None else up if x > m else dn for x, m in zip(xs, e)]


def corr(a, b, n=60):
    out = [None] * len(a)
    for i in range(n, len(a)):
        xs, ys = a[i - n + 1:i + 1], b[i - n + 1:i + 1]
        if any(v is None for v in xs + ys):
            continue
        mx, my = sum(xs) / n, sum(ys) / n
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        out[i] = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else None
    return out


def rets(xs):
    return [None] + [None if a in (None, 0) or b is None else b / a - 1 for a, b in zip(xs, xs[1:])]


# ---------------------------------------------------------------- the states
def factors(S, days, breadth, gamma=None):
    """[(key, name, explanation, [state per day])]. S: {sym: [aligned close]}."""
    tnx, irx = S.get("^TNX"), S.get("^IRX")
    out = []
    if tnx:
        d20 = chg(tnx, 20, pct=False)
        out.append(("rates", "10-year yield, last 20 days", "Rising: up at least 0.15 points (15 bp) in 20 days; Falling: down as much",
                    confirm([None if v is None else "Rising" if v >= 0.15 else "Falling" if v <= -0.15 else "Flat" for v in d20])))
        d252 = chg(tnx, 252, pct=False)
        out.append(("cycle", "10-year yield vs a year ago", "Hiking: at least 0.5 points higher than a year ago; Easing: 0.5 lower",
                    confirm([None if v is None else "Higher (tightening)" if v >= 0.5 else "Lower (easing)" if v <= -0.5 else "About the same" for v in d252])))
    if tnx and irx:
        cv = [None if a is None or b is None else a - b for a, b in zip(tnx, irx)]
        out.append(("curve", "Yield curve (10-year - 3-month)", "Inverted: the 3-month yield above the 10-year (a classic recession warning)",
                    confirm([None if v is None else "Inverted" if v < 0 else "Normal" for v in cv])))
        c20 = chg(cv, 20, pct=False)
        out.append(("curveMove", "Yield curve, last 20 days", "Steepening / flattening by at least 0.15 points in 20 days",
                    confirm([None if v is None else "Steepening" if v >= 0.15 else "Flattening" if v <= -0.15 else "Stable" for v in c20])))
    if S.get("TLT"):
        out.append(("bonds", "Long Treasuries (TLT)", "TLT above / under its 50-day EMA (bond prices up = yields down)",
                    confirm(above(S["TLT"], 50, "Bonds up", "Bonds down"))))
    if S.get("HYG") and S.get("IEF"):
        out.append(("credit", "Credit (high yield vs Treasuries)", "HYG / IEF above its 50-day EMA: investors take credit risk; under: they flee to safety",
                    confirm(above(ratio(S["HYG"], S["IEF"]), 50, "Risk-on", "Risk-off"))))
    if S.get("UUP"):
        out.append(("dollar", "US dollar (UUP)", "UUP above / under its 50-day EMA", confirm(above(S["UUP"], 50, "Dollar up", "Dollar down"))))
    if S.get("USO"):
        o20 = chg(S["USO"], 20)
        out.append(("oil", "Oil, last 20 days", "Spike: oil up 10%+ in 20 days; Crash: down 10%+",
                    confirm([None if v is None else "Spike" if v >= 0.10 else "Crash" if v <= -0.10 else "Normal" for v in o20])))
    if S.get("HG=F") and S.get("GC=F"):
        out.append(("cugold", "Copper vs gold", "Copper / gold above its 50-day EMA: growth bets; under: fear (gold wins)",
                    confirm(above(ratio(S["HG=F"], S["GC=F"]), 50, "Growth", "Fear"))))
    if breadth:
        out.append(("breadth", "Breadth (watchlist above 50-day)", "Share of the watchlist stocks above their 50-day EMA: Strong 60%+, Weak under 40%",
                    confirm([None if v is None else "Strong" if v >= 0.6 else "Weak" if v < 0.4 else "Mixed" for v in breadth])))
    if S.get("IWM") and S.get("SPY"):
        out.append(("small", "Small caps vs S&P (IWM / SPY)", "IWM / SPY above / under its 50-day EMA",
                    confirm(above(ratio(S["IWM"], S["SPY"]), 50, "Small caps leading", "Small caps lagging"))))
    if S.get("RSP") and S.get("SPY"):
        out.append(("width", "Equal weight vs S&P (RSP / SPY)", "RSP / SPY above its 50-day EMA: a broad market; under: a few mega caps carry it",
                    confirm(above(ratio(S["RSP"], S["SPY"]), 50, "Broad", "Narrow"))))
    if S.get("TLT") and S.get("SPY"):
        c = corr(rets(S["SPY"]), rets(S["TLT"]), 60)
        out.append(("sbcorr", "Stocks vs bonds (60-day correlation)", "Positive: stocks and bonds fall together (inflation / rate fears); negative: bonds hedge stocks",
                    confirm([None if v is None else "Together (no hedge)" if v > 0.2 else "Opposite (bonds hedge)" if v < -0.2 else "Unrelated" for v in c])))
    if gamma:
        out.append(("gamma", "SPY vs gamma flip", "Above the flip: dealers damp the moves; under: they amplify them",
                    confirm([None if v is None else "Above flip" if v else "Under flip" for v in gamma])))
    return out


def state_stats(states, days, S, spy):
    """Per state: days, share, times, median length, and per asset the forward 5 / 20 / 60-day move (avg %, % up, vs SPY)."""
    st = sorted({s for s in states if s})
    segs = []
    for i, s in enumerate(states):
        if s is None:
            continue
        if segs and segs[-1][0] == s and segs[-1][2] == i - 1:
            segs[-1][2] = i
        else:
            segs.append([s, i, i])
    out = {}
    tot = sum(1 for s in states if s)
    for s in st:
        lens = sorted(b - a + 1 for x, a, b in segs[:-1] if x == s) or [segs[-1][2] - segs[-1][1] + 1]
        idx = [i for i, x in enumerate(states) if x == s]
        by = {}
        for sym, _ in ASSETS:
            px = S.get(sym)
            if not px:
                continue
            f, fw, fx = [], [], []
            for h in HZ:
                xs = [(px[i + h] / px[i] - 1, spy[i + h] / spy[i] - 1) for i in idx
                      if i + h < len(days) and None not in (px[i], px[i + h], spy[i], spy[i + h]) and px[i]]
                f.append(round(sum(a for a, _ in xs) / len(xs) * 100, 2) if xs else None)
                fw.append(round(sum(1 for a, _ in xs if a > 0) / len(xs) * 100) if xs else None)
                fx.append(round(sum(a - b for a, b in xs) / len(xs) * 100, 2) if xs else None)
            by[sym] = {"f": f, "fw": fw, "fx": fx}
        out[s] = {"days": len(idx), "share": round(len(idx) / tot * 100, 1) if tot else None, "times": sum(1 for x in segs if x[0] == s),
                  "median": lens[len(lens) // 2], "by": by}
    cur = states[-1]
    since = segs[-1][1] if segs and segs[-1][0] == cur and segs[-1][2] == len(states) - 1 else None
    return out, {"state": cur, "since": days[since] if since is not None else None, "days": (len(days) - since) if since is not None else None}


# ---------------------------------------------------------------- early warning: how fast each signal caught the selloffs
def selloffs(spy, days, dd=DD):
    """[{peak, trough, back (new high or None), depth %}] of the drops of at least dd from the last high."""
    out, pk, pi, lo, li = [], None, None, None, None
    for i, c in enumerate(spy):
        if c is None:
            continue
        if pk is None or c > pk:
            if pk is not None and lo is not None and lo <= pk * (1 - dd):
                out.append({"pi": pi, "ti": li, "bi": i})
            pk, pi, lo, li = c, i, c, i
        elif c < lo:
            lo, li = c, i
    if pk is not None and lo is not None and lo <= pk * (1 - dd):
        out.append({"pi": pi, "ti": li, "bi": None})
    for e in out:
        e.update(peak=days[e["pi"]], trough=days[e["ti"]], back=days[e["bi"]] if e["bi"] is not None else None,
                 depth=round((spy[e["ti"]] / spy[e["pi"]] - 1) * 100, 1), days=e["ti"] - e["pi"])
    return out


def warnings(S, days, breadth, lab, gamma=None):
    """[(key, name, [bool per day])]: the warning signals (raw, no confirmation) and the confirmed regime."""
    spy, vix, v3 = S["SPY"], S.get("^VIX"), S.get("^VIX3M")
    e20, e50 = ema(spy, 20), ema(spy, 50)
    w = []
    if vix:
        w.append(("vix20", "VIX at 20+", [None if v is None else v >= 20 for v in vix]))
        vj = chg(vix, 5)
        w.append(("vixjump", "VIX up 30%+ in 5 days", [None if v is None else v >= 0.30 for v in vj]))
    if vix and v3:
        w.append(("vixinv", "VIX above VIX3M", [None if a is None or b is None else a > b for a, b in zip(vix, v3)]))
    if gamma:
        w.append(("gamma", "SPY under the gamma flip", [None if v is None else not v for v in gamma]))
    w.append(("ema20", "SPY under its 20-day EMA", [None if c is None or m is None else c < m for c, m in zip(spy, e20)]))
    w.append(("ema50", "SPY under its 50-day EMA", [None if c is None or m is None else c < m for c, m in zip(spy, e50)]))
    if breadth:
        w.append(("breadth", "Breadth under 40%", [None if v is None else v < 0.4 for v in breadth]))
    if S.get("HYG") and S.get("IEF"):
        r = ratio(S["HYG"], S["IEF"])
        er = ema(r, 50)
        w.append(("credit", "Credit risk-off (HYG/IEF under 50-day)", [None if a is None or b is None else a < b for a, b in zip(r, er)]))
    if S.get("IWM"):
        r = chg(ratio(S["IWM"], spy), 20)
        w.append(("small", "Small caps 3%+ behind in 20 days", [None if v is None else v <= -0.03 for v in r]))
    if S.get("^TNX"):
        d = chg(S["^TNX"], 20, pct=False)
        w.append(("rates", "10-year yield up 0.25+ in 20 days", [None if v is None else v >= 0.25 for v in d]))
    if lab:
        w.append(("regRange", "Confirmed: trend no longer Up", [None if d not in lab else lab[d]["trend"] != "Up" for d in days]))
        w.append(("regDown", "Confirmed: trend Down", [None if d not in lab else lab[d]["trend"] == "Down" for d in days]))
    return w


def detection(spy, days, sig, eps, before=20):
    """Per selloff: the first time the signal turned on from `before` days ahead of the peak to the trough
    (lag in trading days from the peak, negative = ahead of it; the drop already done), and the 'all clear' after the
    trough (off 3 days in a row: lag from the trough, the gain already made)."""
    out = []
    for e in eps:
        pi, ti = e["pi"], e["ti"]
        a = max(1, pi - before)
        hit = None
        if sig[a - 1]:                                 # on since before the window: no new alarm for this one
            hit = {"already": True}
        else:
            for i in range(a, ti + 1):
                if sig[i] and not sig[i - 1]:
                    hit = {"lag": i - pi, "drop": round((spy[i] / spy[pi] - 1) * 100, 1),
                           "share": round((spy[pi] - spy[i]) / (spy[pi] - spy[ti]) * 100) if spy[pi] != spy[ti] else None}
                    break
        clr = None
        end = e["bi"] if e["bi"] is not None else len(days) - 1
        run = 0
        for i in range(ti, min(len(days), end + 60)):
            run = run + 1 if sig[i] is False else 0
            if run >= 3:
                j = i - 2
                clr = {"lag": j - ti, "gain": round((spy[j] / spy[ti] - 1) * 100, 1),
                       "share": round((spy[j] - spy[ti]) / (spy[pi] - spy[ti]) * 100) if spy[pi] != spy[ti] else None}
                break
        out.append({"on": hit, "clear": clr})
    return out


def alarms(spy, days, sig, eps, look=30, drop=0.05):
    """Every time the signal turned on: did a drop of 5%+ follow within 30 days (or was it inside a selloff)?"""
    inside = set()
    for e in eps:
        inside.update(range(e["pi"], e["ti"] + 1))
    fires, last_off = [], -99
    for i in range(1, len(sig)):
        if sig[i] is False:
            last_off = i
        if sig[i] and not sig[i - 1] and i - last_off <= 1:
            if fires and i - fires[-1] < 5:            # a blip: the same alarm
                continue
            fires.append(i)
    hits, fwd = 0, []
    for i in fires:
        lo = min((c for c in spy[i + 1:i + 1 + look] if c is not None), default=spy[i])
        if i in inside or lo <= spy[i] * (1 - drop):
            hits += 1
        if i + 20 < len(spy) and spy[i + 20] and spy[i]:
            fwd.append(spy[i + 20] / spy[i] - 1)
    on = sum(1 for v in sig if v)
    yrs = len(days) / 252
    return {"fires": len(fires), "perYear": round(len(fires) / yrs, 1) if yrs else None, "hit": round(hits / len(fires) * 100) if fires else None,
            "fwd20": round(sum(fwd) / len(fwd) * 100, 2) if fwd else None, "onShare": round(on / max(1, sum(1 for v in sig if v is not None)) * 100, 1),
            "now": bool(sig[-1])}


def breadth_of(stocks_px, days):
    """Share of the watchlist stocks above their 50-day EMA, per day (None until 10 stocks have the history)."""
    cols = []
    for px in stocks_px:
        al = align(px, days)
        e = ema(al, 50)
        cols.append([None if c is None or m is None else c > m for c, m in zip(al, e)])
    out = []
    for i in range(len(days)):
        xs = [c[i] for c in cols if c[i] is not None]
        out.append(sum(xs) / len(xs) if len(xs) >= 10 else None)
    return out


def study(data, days, lab, stocks, gamma_walls=None):
    """Everything the page shows. data: {sym: {date: close}} (SPY, ^VIX, ^VIX3M, the ASSETS, SYMS, the stocks)."""
    S = {k: align(v, days) for k, v in data.items() if k in {s for s, _ in ASSETS} | set(SYMS) | {"^VIX", "^VIX3M", "SPY"}}
    spy = S["SPY"]
    breadth = breadth_of([data[s] for s in stocks if s in data], days)
    gamma = None
    if gamma_walls:
        g = [None if (gamma_walls.get(d) or {}).get("flip") is None or c is None else c > gamma_walls[d]["flip"] for d, c in zip(days, spy)]
        gamma = g if sum(1 for v in g if v is not None) >= 120 else None
    fs = []
    for key, name, expl, states in factors(S, days, breadth, gamma):
        st, now = state_stats(states, days, S, spy)
        fs.append({"key": key, "name": name, "expl": expl, "states": st, "now": now})
    eps = selloffs(spy, days)
    ws = warnings(S, days, breadth, lab, gamma)
    det = {k: detection(spy, days, sig, eps) for k, _, sig in ws}
    return {"hz": list(HZ), "assets": [{"sym": s, "name": n} for s, n in ASSETS if S.get(s)], "factors": fs,
            "episodes": [{k: e[k] for k in ("peak", "trough", "back", "depth", "days")} for e in eps],
            "signals": [{"key": k, "name": n, "alarms": alarms(spy, days, sig, eps), "det": det[k]} for k, n, sig in ws],
            "dd": DD, "gamma": bool(gamma), "breadthNow": round(breadth[-1] * 100) if breadth and breadth[-1] is not None else None}
