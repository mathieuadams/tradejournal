"""Weekly ranking from the daily EMAs 21 / 50 / 200 (a top-down method in the style taught by Maverick Trading — the
exact rules here are our own), and a test of whether "score -> option structure" holds.

Score at each week's last close, from 5 checks: close > EMA21, close > EMA50, close > EMA200, EMA21 > EMA50,
EMA50 > EMA200 (all on daily closes): 5 true = +2, 4 = +1, 2-3 = 0, 1 = -1, 0 = -2.
Structure for a score: +2 long call, +1 bull put spread, 0 iron condor, -1 bear call spread, -2 long put.

The test: for every week and ticker, the move over the next 1 / 2 / 4 weeks, and seven 30-day option structures held to
expiration (21 trading days), priced with Black-Scholes. There is no option price history here: the implied volatility
is the VIX for SPY and the 20-day realized volatility x a multiplier (1.0 / 1.1 / 1.2) for the rest, no skew, the 3-month
T-bill as the rate. So the figures show how each structure pays in each score, approximately — the credit structures
depend on that IV assumption (the volatility premium), which is why three are shown."""
import math

STRUCT = {2: "Long call", 1: "Bull put spread", 0: "Iron condor", -1: "Bear call spread", -2: "Long put"}
STRATS = ["Long call", "Call debit spread", "Bull put spread", "Iron condor", "Bear call spread", "Put debit spread", "Long put"]
MULTS = (1.0, 1.1, 1.2)
HOLD = 21                                             # trading days (~30 calendar days to expiration)
FWD = (5, 10, 20)


def ema(xs, n):
    out, e, k, cnt = [], None, 2 / (n + 1), 0
    for x in xs:
        if x is None:
            out.append(e if cnt >= n else None)
            continue
        cnt += 1
        e = x if e is None else x * k + e * (1 - k)
        out.append(e if cnt >= n else None)
    return out


def checks(c, a, b, d):
    return [c > a, c > b, c > d, a > b, b > d]


def score_of(pts):
    return 2 if pts == 5 else 1 if pts == 4 else 0 if pts >= 2 else -1 if pts == 1 else -2


def scores(px):
    """[(score, [5 checks]) or None] per day."""
    e21, e50, e200 = ema(px, 21), ema(px, 50), ema(px, 200)
    out = []
    for c, a, b, d in zip(px, e21, e50, e200):
        if None in (c, a, b, d):
            out.append(None)
            continue
        ck = checks(c, a, b, d)
        out.append((score_of(sum(ck)), ck))
    return out


def rvol(px, n=20):
    out = [None] * len(px)
    lr = [None] + [math.log(b / a) if a and b else None for a, b in zip(px, px[1:])]
    for i in range(n, len(px)):
        w = lr[i - n + 1:i + 1]
        if any(v is None for v in w):
            continue
        m = sum(w) / n
        out[i] = math.sqrt(sum((v - m) ** 2 for v in w) / (n - 1)) * math.sqrt(252)
    return out


def _N(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, v, r, call):
    if v <= 0 or T <= 0:
        return max(0.0, S - K) if call else max(0.0, K - S)
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    return S * _N(d1) - K * math.exp(-r * T) * _N(d2) if call else K * math.exp(-r * T) * _N(-d2) - S * _N(-d1)


def structures(S, ST, v, r, T=30 / 365):
    """{structure: R} = profit at expiration / the most it could lose. Strikes in steps of the expected move
    (S x v x sqrt(T)): spreads short at 0.5, long at 1.0 expected moves out; condor 0.75 / 1.25; debit spreads ATM -> 1."""
    em = v * math.sqrt(T)
    k = lambda m: S * math.exp(m * em)
    C = lambda K: bs(S, K, T, v, r, True)
    P = lambda K: bs(S, K, T, v, r, False)
    cp = lambda K: max(0.0, ST - K)
    pp = lambda K: max(0.0, K - ST)
    out = {}
    c0, p0 = C(S), P(S)
    out["Long call"] = (cp(S) - c0) / c0 if c0 > 0 else None
    out["Long put"] = (pp(S) - p0) / p0 if p0 > 0 else None
    d = c0 - C(k(1.0))                                 # call debit spread ATM / +1
    out["Call debit spread"] = ((cp(S) - cp(k(1.0))) - d) / d if d > 0 else None
    d = p0 - P(k(-1.0))
    out["Put debit spread"] = ((pp(S) - pp(k(-1.0))) - d) / d if d > 0 else None

    def credit(short, long_, call):
        cr = (C(short) - C(long_)) if call else (P(short) - P(long_))
        w = abs(long_ - short)
        loss = (cp(short) - cp(long_)) if call else (pp(short) - pp(long_))
        return cr, w, loss
    cr, w, loss = credit(k(-0.5), k(-1.0), False)
    out["Bull put spread"] = (cr - loss) / (w - cr) if w - cr > 0 else None
    cr, w, loss = credit(k(0.5), k(1.0), True)
    out["Bear call spread"] = (cr - loss) / (w - cr) if w - cr > 0 else None
    c1, w1, l1 = credit(k(-0.75), k(-1.25), False)
    c2, w2, l2 = credit(k(0.75), k(1.25), True)
    risk = max(w1, w2) - (c1 + c2)
    out["Iron condor"] = (c1 + c2 - l1 - l2) / risk if risk > 0 else None
    return out


def _agg(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if not n:
        return {"n": 0}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, n - 1))
    return {"n": n, "avg": round(m, 3), "win": round(sum(1 for x in xs if x > 0) / n * 100), "t": round(m / sd * math.sqrt(n), 1) if sd > 0 else None}


def sector_of(stock, sectors):
    """The sector ETF whose daily moves follow the stock the most (correlation over their common history)."""
    best, bc = None, -2
    for sym, sp in sectors.items():
        xs, ys = [], []
        for a, b, c, d in zip(stock, stock[1:], sp, sp[1:]):
            if None not in (a, b, c, d) and a and c:
                xs.append(b / a - 1)
                ys.append(d / c - 1)
        if len(xs) < 120:
            continue
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
        sy = math.sqrt(sum((y - my) ** 2 for y in ys))
        c = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else -2
        if c > bc:
            best, bc = sym, c
    return best, round(bc, 2) if best else None


def study(S, days, groups, names, irx=None, vix=None):
    """S: {sym: [aligned daily close]}; groups: {"index": [...], "sector": [...], "stock": [...]}."""
    weeks = [i for i in range(len(days) - 1) if days[i][:10] and _wk(days[i]) != _wk(days[i + 1])] + [len(days) - 1]
    sc = {s: scores(S[s]) for g in groups.values() for s in g if S.get(s)}
    rv = {s: rvol(S[s]) for s in sc}
    sect = {s: sector_of(S[s], {x: S[x] for x in groups["sector"] if S.get(x)}) for s in groups["stock"] if s in sc}
    spy_sc = sc.get("SPY")
    dirs, strat, top = {}, {}, {}
    for g, syms in groups.items():
        for s in syms:
            if s not in sc:
                continue
            px = S[s]
            for i in weeks:
                x = sc[s][i]
                if not x or px[i] is None:
                    continue
                v = x[0]
                for h in FWD:
                    if i + h < len(px) and px[i + h]:
                        dirs.setdefault((g, v, h), []).append((px[i + h] / px[i] - 1) * 100)
                if g == "stock" and spy_sc and spy_sc[i] and sect.get(s) and sc.get(sect[s][0]) and sc[sect[s][0]][i]:
                    ss, ms = sc[sect[s][0]][i][0], spy_sc[i][0]   # top-down: the stock by its sector's / the market's score
                    sg = lambda z: (z > 0) - (z < 0)
                    al = ("aligned" if sg(ss) == sg(v) and sg(ms) == sg(v) else "against" if sg(ms) == -sg(v) else "mixed") if v else None
                    for h in FWD:
                        if i + h < len(px) and px[i + h]:
                            rr = (px[i + h] / px[i] - 1) * 100
                            top.setdefault(("sec", ss, h), []).append(rr)
                            top.setdefault(("bySec", v, ss, h), []).append(rr)
                            top.setdefault(("bySpy", v, ms, h), []).append(rr)
                            if al:
                                top.setdefault(("al", v, al, h), []).append(rr * sg(v))
                if i + HOLD >= len(px) or not px[i + HOLD]:
                    continue
                r = (irx[i] / 100) if irx and irx[i] is not None else 0.02
                for mi, m in enumerate(MULTS):
                    iv = (vix[i] / 100) if s == "SPY" and vix and vix[i] else (rv[s][i] * m if rv[s][i] else None)
                    if not iv:
                        continue
                    for k_, R in structures(px[i], px[i + HOLD], iv, r).items():
                        strat.setdefault((g, mi, v, k_), []).append(R)
                        strat.setdefault(("all", mi, v, k_), []).append(R)
    out_dir = {g: {str(v): {str(h): _agg(dirs.get((g, v, h), [])) for h in FWD} for v in (2, 1, 0, -1, -2)} for g in groups}
    out_st = {g: [{str(v): {k_: _agg(strat.get((g, mi, v, k_), [])) for k_ in STRATS} for v in (2, 1, 0, -1, -2)} for mi in range(len(MULTS))]
              for g in list(groups) + ["all"]}
    SC = (2, 1, 0, -1, -2)
    td = {str(h): {"sec": {str(x): _agg(top.get(("sec", x, h), [])) for x in SC},
                   "bySec": {str(v): {str(x): _agg(top.get(("bySec", v, x, h), [])) for x in SC} for v in SC},
                   "bySpy": {str(v): {str(x): _agg(top.get(("bySpy", v, x, h), [])) for x in SC} for v in SC},
                   "align": {str(v): {a: _agg(top.get(("al", v, a, h), [])) for a in ("aligned", "mixed", "against")} for v in (2, 1, -1, -2)}}
          for h in FWD}
    now = []
    last = len(days) - 1
    hist_w = weeks[-8:]
    for g, syms in groups.items():
        for s in syms:
            if s not in sc or not sc[s][last]:
                continue
            v, ck = sc[s][last]
            row = {"sym": s, "name": names.get(s, ""), "group": g, "score": v, "checks": ck, "struct": STRUCT[v],
                   "hist": [sc[s][i][0] if sc[s][i] else None for i in hist_w], "close": round(S[s][last], 2)}
            if s in sect and sect[s][0]:
                se = sect[s][0]
                ssv = sc[se][last][0] if sc.get(se) and sc[se][last] else None
                spv = spy_sc[last][0] if spy_sc and spy_sc[last] else None
                row.update(sector=se, sectorCorr=sect[s][1], sectorScore=ssv, spyScore=spv,
                           aligned=bool(v and ssv and spv and (v > 0) == (ssv > 0) == (spv > 0)))
            now.append(row)
    return {"asOf": days[last], "weeksHist": [days[i] for i in hist_w], "structOf": {str(k): v for k, v in STRUCT.items()}, "strats": STRATS,
            "mults": list(MULTS), "hold": HOLD, "fwd": list(FWD), "dir": out_dir, "strat": out_st, "topdown": td, "now": now,
            "weeks": len(weeks)}


def _wk(d):
    import datetime as dt
    y, w, _ = dt.date.fromisoformat(d[:10]).isocalendar()
    return (y, w)
