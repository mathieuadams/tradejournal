"""ETF pairs strategy: trade the correlation — when two ETFs that usually move together drift apart, bet they come back.

For every pair of the ETF list (etf.settings):
  spread   = log(A) − log(B) (a dollar-neutral pair: $stake long one, $stake short the other)
  z        = (spread − its average over `look` days) / its standard deviation over the same days
  corr     = correlation of the two ETFs' daily returns over the last 60 days
Entry, decided on the close of day t and traded at the OPEN of day t+1 (no look-ahead): corr ≥ `minCorr` and
  z ≥ +`entry` → short A, long B (A ran ahead) · z ≤ −`entry` → long A, short B.
Exit, decided on a close and traded at the next open: z back to `exit` (0 = the average) · `stop`: |z| widened to
  this (0 = no stop) · `hold`: the most sessions held. One position per pair at a time.
P/L: $stake per leg; costs `costBps` round trip on each leg; the short leg pays `borrowPct` a year.
Prices: Alpaca daily bars, split-adjusted (fund/px/<SYM>), since 2016. Test period: the last 5 years; the optimizer
ranks on the years before only."""
import math
import time
from datetime import date, timedelta

import store
from util import BadRequest, iso, now_ny

DEFAULT = {"look": 40, "entry": 2.0, "exit": 0.0, "stop": 0.0, "hold": 20, "minCorr": 0.7, "stake": 1000.0, "costBps": 5.0, "borrowPct": 1.0}
CORR_WIN = 60
TEST_YEARS = 5
GRID = {"look": [20, 40, 60], "entry": [1.5, 2.0, 2.5], "exit": [0.0, 0.5], "stop": [0.0, 3.5], "hold": [10, 20, 40], "minCorr": [0.6, 0.8]}
MIN_TRADES = 30
FROM = "2016-01-01"


def _d(s):
    return date.fromisoformat(s)


def clean(cfg):
    c = {**DEFAULT, **{k: v for k, v in (cfg or {}).items() if k in DEFAULT}}
    c["look"] = int(c["look"]) if int(c["look"]) in (10, 20, 40, 60, 90, 120) else 40
    c["entry"] = max(0.5, min(5.0, float(c["entry"])))
    c["exit"] = max(-1.0, min(c["entry"] - 0.1, float(c["exit"])))
    c["stop"] = max(0.0, min(10.0, float(c["stop"])))
    if c["stop"] and c["stop"] <= c["entry"]:
        c["stop"] = 0.0
    c["hold"] = max(1, min(250, int(c["hold"])))
    c["minCorr"] = max(-1.0, min(0.99, float(c["minCorr"])))
    c["stake"] = max(10.0, min(1e7, float(c["stake"])))
    c["costBps"] = max(0.0, min(200.0, float(c["costBps"])))
    c["borrowPct"] = max(0.0, min(50.0, float(c["borrowPct"])))
    return c


# ---------------------------------------------------------------- data

def load(sub):
    """{sym: {date: (open, close)}} for the ETF list."""
    import etf
    import fundamentals as F
    syms = etf.settings(sub)["syms"]
    px, missing = {}, []
    for s in syms:
        try:
            bars = F.prices(sub, s, FROM, max_age_h=12)
        except Exception as ex:
            missing.append(f"{s}: {str(ex)[:60]}")
            continue
        px[s] = {b[0]: (b[1], b[4]) for b in bars if b[0] >= FROM and b[1] and b[4]}
    return px, missing


def _roll(xs, w):
    """Rolling mean and standard deviation over the last w values (None until w values)."""
    m, sd, s, s2 = [None] * len(xs), [None] * len(xs), 0.0, 0.0
    for i, x in enumerate(xs):
        s += x
        s2 += x * x
        if i >= w:
            s -= xs[i - w]
            s2 -= xs[i - w] ** 2
        if i >= w - 1:
            mu = s / w
            v = max(0.0, s2 / w - mu * mu)
            m[i], sd[i] = mu, math.sqrt(v)
    return m, sd


def prep_pair(pa, pb, looks):
    """One pair's aligned series: dates, opens, the z-score per lookback and the rolling return correlation."""
    ds = sorted(set(pa) & set(pb))
    if len(ds) < max(looks) + CORR_WIN + 5:
        return None
    oa = [pa[d][0] for d in ds]
    ob = [pb[d][0] for d in ds]
    ca = [pa[d][1] for d in ds]
    cb = [pb[d][1] for d in ds]
    sp = [math.log(a) - math.log(b) for a, b in zip(ca, cb)]
    z = {}
    for L in looks:
        m, sd = _roll(sp, L)
        z[L] = [((sp[i] - m[i]) / sd[i]) if m[i] is not None and sd[i] else None for i in range(len(sp))]
    ra = [0.0] + [math.log(ca[i] / ca[i - 1]) for i in range(1, len(ca))]
    rb = [0.0] + [math.log(cb[i] / cb[i - 1]) for i in range(1, len(cb))]
    corr = [None] * len(ds)
    sa = sb = saa = sbb = sab = 0.0
    w = CORR_WIN
    for i in range(1, len(ds)):
        a, b = ra[i], rb[i]
        sa += a; sb += b; saa += a * a; sbb += b * b; sab += a * b
        if i > w:
            a0, b0 = ra[i - w], rb[i - w]
            sa -= a0; sb -= b0; saa -= a0 * a0; sbb -= b0 * b0; sab -= a0 * b0
        if i >= w:
            cov = sab / w - (sa / w) * (sb / w)
            va, vb = saa / w - (sa / w) ** 2, sbb / w - (sb / w) ** 2
            corr[i] = cov / math.sqrt(va * vb) if va > 0 and vb > 0 else None
    return {"d": ds, "oa": oa, "ob": ob, "z": z, "corr": corr}


def prep_all(px, looks):
    syms = sorted(px)
    out = {}
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            p = prep_pair(px[syms[i]], px[syms[j]], looks)
            if p:
                out[(syms[i], syms[j])] = p
    return out


# ---------------------------------------------------------------- the simulation

def sim_pair(a, b, p, c):
    """The trades of one pair; the last one may still be open (exit None)."""
    z, corr, ds, oa, ob = p["z"][c["look"]], p["corr"], p["d"], p["oa"], p["ob"]
    E, X, ST, H, MC = c["entry"], c["exit"], c["stop"], c["hold"], c["minCorr"]
    out, pos, n = [], None, len(ds)
    for t in range(n - 1):
        zt = z[t]
        if zt is None:
            continue
        if pos is None:
            ct = corr[t]
            if ct is None or ct < MC:
                continue
            if zt >= E:
                pos = {"side": -1, "e": t + 1, "z0": zt, "c0": ct}       # A rich: short A, long B
            elif zt <= -E:
                pos = {"side": 1, "e": t + 1, "z0": zt, "c0": ct}
            continue
        s = pos["side"]
        why = None
        if (s == -1 and zt <= X) or (s == 1 and zt >= -X):
            why = "back to the mean"
        elif ST and ((s == -1 and zt >= ST) or (s == 1 and zt <= -ST)):
            why = "stop"
        elif t + 1 - pos["e"] >= H:
            why = "time"
        if why:
            out.append(_trade(a, b, p, pos, t + 1, why, c))
            pos = None
    if pos is not None:
        tr = _trade(a, b, p, pos, n - 1, "open", c)
        tr["open"] = True
        out.append(tr)
    return out


def _trade(a, b, p, pos, x, why, c):
    ds, oa, ob, s, e = p["d"], p["oa"], p["ob"], pos["side"], pos["e"]
    if x <= e:
        x = e
    ra, rb = oa[x] / oa[e] - 1, ob[x] / ob[e] - 1
    days = (_d(ds[x]) - _d(ds[e])).days
    st = c["stake"]
    gross = st * (s * ra - s * rb)
    cost = st * 2 * c["costBps"] / 1e4 + st * c["borrowPct"] / 100 * days / 365
    return {"pair": f"{a}/{b}", "long": a if s == 1 else b, "short": b if s == 1 else a, "entry": ds[e], "exit": ds[x],
            "sessions": x - e, "z0": round(pos["z0"], 2), "corr0": round(pos["c0"], 2), "why": why,
            "ret": round(s * ra - s * rb, 5), "pnl": round(gross - cost, 2)}


def trades(pre, c):
    out = []
    for (a, b), p in pre.items():
        out.extend(sim_pair(a, b, p, c))
    return out


def split_date(pre):
    last = max(p["d"][-1] for p in pre.values())
    return _d(last).replace(year=_d(last).year - TEST_YEARS).isoformat()


def _stats(ts):
    import backtest
    return backtest._stats(ts)


def run(sub, cfg):
    c = clean(cfg)
    px, missing = load(sub)
    if len(px) < 2:
        raise BadRequest("Not enough ETFs with prices. " + "; ".join(missing))
    pre = prep_all(px, [c["look"]])
    allt = trades(pre, c)
    opens = [t for t in allt if t.get("open")]
    ts = sorted([t for t in allt if not t.get("open")], key=lambda t: (t["exit"], t["pair"]))
    sd = split_date(pre)
    tr, te = [t for t in ts if t["entry"] < sd], [t for t in ts if t["entry"] >= sd]
    eq, cum = [], 0.0
    for t in ts:
        cum += t["pnl"]
        if eq and eq[-1][0] == t["exit"]:
            eq[-1][1] = round(cum, 2)
        else:
            eq.append([t["exit"], round(cum, 2)])
    years, pairs = {}, {}
    for t in ts:
        years.setdefault(t["exit"][:4], []).append(t)
        pairs.setdefault(t["pair"], []).append(t)
    pr = sorted(({"pair": k, **_stats(v)} for k, v in pairs.items()), key=lambda x: -x["total"])
    # the most positions open at once (capital: 2 × stake each)
    ev = sorted([(t["entry"], 1) for t in ts] + [(t["exit"], -1) for t in ts], key=lambda x: (x[0], x[1]))
    k = peak = 0
    for _, d in ev:
        k += d
        peak = max(peak, k)
    now = []
    for (a, b), p in pre.items():                      # today: every pair's z and correlation
        z, cr = p["z"][c["look"]][-1], p["corr"][-1]
        if z is None:
            continue
        op = next((t for t in opens if t["pair"] == f"{a}/{b}"), None)
        sig = None
        if not op and cr is not None and cr >= c["minCorr"] and abs(z) >= c["entry"]:
            sig = f"long {b} / short {a}" if z > 0 else f"long {a} / short {b}"
        now.append({"pair": f"{a}/{b}", "z": round(z, 2), "corr": round(cr, 2) if cr is not None else None, "open": op, "signal": sig,
                    "date": p["d"][-1]})
    now.sort(key=lambda x: -abs(x["z"]))
    return {"cfg": c, "n": len(ts), "all": _stats(ts), "split": {"date": sd, "train": _stats(tr), "test": _stats(te)},
            "years": [{"year": y, **_stats(v)} for y, v in sorted(years.items())], "pairs": pr[:12], "worstPairs": pr[-12:][::-1],
            "nPairs": len(pre), "equity": eq, "peakPositions": peak, "trades": ts[-300:], "nTrades": len(ts),
            "openTrades": opens, "now": now[:40], "from": min(p["d"][0] for p in pre.values()), "to": max(p["d"][-1] for p in pre.values()),
            "missing": missing}


# ---------------------------------------------------------------- the optimizer (a background job)

def combos():
    keys = list(GRID)
    import itertools
    return [dict(zip(keys, v)) for v in itertools.product(*[GRID[k] for k in keys])]


def opt_job(sub, prog=None, deadline=None):
    key = f"etf/u/{sub}/opt"
    prev = store.get_json(key) or {}
    base = clean(prev.get("cfg") or {})
    res = [] if prev.get("complete") else (prev.get("res") or [])
    done = {tuple(sorted(x["p"].items())) for x in res}
    px, _ = load(sub)
    pre = prep_all(px, GRID["look"])
    sd = split_date(pre)
    allc = combos()
    t_last, stopped = 0.0, False
    for n, p in enumerate(allc):
        if tuple(sorted(p.items())) in done:
            continue
        if deadline and time.time() > deadline:
            stopped = True
            break
        if prog and time.time() - t_last > 3:
            prog(f"ETF pairs optimizer: setting {n + 1} of {len(allc)}")
            t_last = time.time()
        c = clean({**base, **p})
        ts = [t for t in trades(pre, c) if not t.get("open")]
        tr, te = [t for t in ts if t["entry"] < sd], [t for t in ts if t["entry"] >= sd]
        res.append({"p": p, "train": _stats(tr), "test": _stats(te)})
        if n % 20 == 0:
            store.put_json(key, {"cfg": base, "res": res, "complete": False, "split": sd, "total": len(allc)})
    store.put_json(key, {"cfg": base, "res": res, "complete": not stopped, "split": sd, "total": len(allc), "at": iso(now_ny())})
    return not stopped


def opt_view(sub, top=15):
    o = store.get_json(f"etf/u/{sub}/opt")
    if not o:
        return None
    rk = sorted(o["res"], key=lambda x: ((x["train"].get("n") or 0) >= MIN_TRADES, x["train"].get("t") or -1e9), reverse=True)
    return {k: v for k, v in o.items() if k != "res"} | {"top": rk[:top], "done": len(o["res"]), "minTrades": MIN_TRADES, "grid": GRID}


def opt_start(sub, cfg):
    import jobs
    store.put_json(f"etf/u/{sub}/opt", {"cfg": clean(cfg), "res": [], "complete": False})
    return jobs.start(sub, "etf", "etfopt", {})
