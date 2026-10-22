"""Smart money optimizer: the building blocks of smc.py tuned on the stocks-in-play history (the same data as the
day trading evolution: each day's top 15 stocks by opening-range RVOL, 5-minute candles, up to 500 days).

  * train = the first 60% of the days, test = the last 40% (never used to choose: it only shows whether a setting
    found on the past still works on days it has not seen);
  * each setting is scored on the train days: t of the R summed per day (after a cost per trade), with at least
    MIN_TRADES trades and no month making more than MONTH_SHARE of the profit;
  * search: random settings + changes of 1-3 blocks / numbers of the best ones (hill climbing), each setting tried
    once; the best TOP kept with their train and test results.

Runs in the background (a Lambda run of ~8 minutes starts the next one while it is on)."""
import json
import math
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import db
import smc
from util import iso, now_ny

SK = "SMCOPT"
VERSION = 4                                            # 4: a 2nd (afternoon) entry window in the search
COST = 0.05
MIN_TRADES = 80
MONTH_SHARE = 0.35
TRAIN = 0.6
TOP = 15
MAX_ITEM = 300_000

# the search space (5-minute candles: the history has no 1-minute candles)
SPACE = {
    "poolPD": [True, False], "poolPM": [True, False], "poolOR": [True, False], "poolSwing": [True, False], "eqOnly": [False, True],
    "orMin": [5, 15, 30], "pivot": [1, 2, 3, 4, 5],
    "mssOn": [True, False], "mssBars": [3, 4, 6, 8, 12, 18, 24], "dispOn": [False, True], "dispMin": [0.5, 0.75, 1.0, 1.5, 2.0],
    "fvgOn": [True, False], "fvgMin": [0.0, 0.1, 0.2, 0.3, 0.5, 0.75],
    "entryAt": ["edge", "ce", "ob", "market"], "entryBars": [3, 6, 12, 18, 24, 36], "stopAt": ["sweep", "fvg"],
    "stopBuf": [0.0, 0.1, 0.2, 0.3, 0.5],
    "startT": ["09:35", "09:45", "10:00", "10:30"], "endT": ["10:30", "11:00", "11:30", "12:00", "13:30", "15:00"],
    "win2On": [False, True], "startT2": ["12:30", "13:00", "13:30", "14:00", "14:30"], "endT2": ["15:00", "15:30", "15:50"],
    "vwapOn": [False, True], "pdOn": [False, True], "rrOn": [False, True], "rrMin": [1.0, 1.5, 2.0, 3.0], "maxDay": [0, 1, 2, 3],
    "side": ["both", "long", "short"],
    "tpLiq": [True, False], "tpR": [1.0, 1.5, 2.0, 3.0, 4.0], "partialOn": [True, False], "partial": [0.25, 0.5, 0.75],
    "beTp1": [True, False], "beOn": [False, True], "beR": [0.5, 1.0, 1.5],
    "trailOn": [False, True], "fvgFail": [False, True], "mssX": [False, True], "vwapX": [False, True],
    "timeOn": [False, True], "timeBars": [6, 12, 24, 36],
}
NEEDS = {"startT2": "win2On", "endT2": "win2On", "orMin": "poolOR", "mssBars": None, "dispMin": "dispOn", "fvgMin": "fvgOn", "rrMin": "rrOn", "partial": "partialOn",
         "beR": "beOn", "timeBars": "timeOn"}


def to_params(g):
    P = {k: v for k, v in g.items() if k != "side"}
    side = g.get("side", "both")
    P["longs"], P["shorts"] = side in ("both", "long"), side in ("both", "short")
    P["tf"] = int(g.get("tf") or 5)
    if P.get("startT", "09:45") < "09:35":
        P["startT"] = "09:35"                          # the picks are known at 09:35
    return smc.clean(P)


def from_params(P):
    g = {k: P[k] for k in SPACE if k in P}
    g["tf"] = 1 if P.get("tf") == 1 else 5
    g["side"] = "both" if P.get("longs", True) and P.get("shorts", True) else "long" if P.get("longs", True) else "short"
    for k, vs in SPACE.items():
        g.setdefault(k, vs[0])
    return g


def key(g):
    """Same key for settings that trade the same (a number of a block that is off doesn't matter)."""
    k = dict(g)
    for n, blk in NEEDS.items():
        if blk and not k.get(blk):
            k.pop(n, None)
    if not k.get("mssOn"):
        k.pop("mssBars", None)
    return json.dumps(k, sort_keys=True)


def valid(g):
    return (g["poolPD"] or g["poolPM"] or g["poolOR"] or g["poolSwing"]) and g["startT"] < g["endT"] \
        and (not g.get("win2On") or g["endT"] <= g.get("startT2", "13:30") < g.get("endT2", "15:30"))


def rand_g(rng):
    while True:
        g = {k: rng.choice(v) for k, v in SPACE.items()}
        if valid(g):
            return g


def mutate(g, rng):
    for _ in range(20):
        x = dict(g)
        if "tf" in g and rng.random() < 0.08:
            x["tf"] = 1 if g["tf"] == 5 else 5         # QQQ / SPY: 1m or 5m candles
            return x
        for k in rng.sample(list(SPACE), rng.choice([1, 1, 2, 3])):
            vs = SPACE[k]
            if isinstance(vs[0], (int, float)) and not isinstance(vs[0], bool) and x[k] in vs and rng.random() < 0.7:
                i = vs.index(x[k]) + rng.choice([-1, 1])  # numbers: mostly the next value up / down
                x[k] = vs[max(0, min(len(vs) - 1, i))]
            else:
                x[k] = rng.choice([v for v in vs if v != x[k]] or vs)
        if valid(x) and x != g:
            return x
    return rand_g(rng)


# ---------------------------------------------------------------- the data: one series per stock-day
_CACHE = {}


INDEX = ("QQQ", "SPY")


def load_index(sub, days):
    """QQQ and SPY: 1-minute candles (pre-market included) of the last `days` days, ready for 1m and 5m."""
    import daytrade
    try:
        feed = (daytrade.data_creds(sub) or {}).get("feed")
    except Exception:
        feed = None
    ck = ("index", sub, days, now_ny().strftime("%Y-%m-%d"), feed)
    if _CACHE.get("k") == ck:
        return _CACHE["v"]
    with ThreadPoolExecutor(max_workers=2) as ex:
        mbs = dict(zip(INDEX, ex.map(lambda s: daytrade.minute_bars(sub, s, days + 2), INDEX)))
    data = {1: [], 5: []}
    for sym, mb in mbs.items():
        if not mb:
            continue
        pm_by = {}
        for b in mb:
            if "04:00" <= b["t"][11:16] < "09:30":
                pm_by.setdefault(b["t"][:10], []).append(b)
        for tf in (1, 5):
            allb = daytrade.agg_bars(mb, tf) if tf > 1 else list(mb)
            reg = [{k: b[k] for k in ("t", "o", "h", "l", "c", "v")} for b in allb if "09:30" <= b["t"][11:16] < "16:00"]
            atr = smc._atr(reg)
            by = {}
            for i, b in enumerate(reg):
                by.setdefault(b["t"][:10], []).append(i)
            ds = sorted(by)[-days:]
            allds = sorted(by)
            for d in ds:
                ix = by[d]
                k = allds.index(d)
                prev = [reg[i] for i in by[allds[k - 1]]] if k else []
                if len(ix) < 20:
                    continue
                data[tf].append({"sym": sym, "d": d, "reg": reg[ix[0]:ix[-1] + 1], "prev": prev, "pm": pm_by.get(d) or [],
                                 "atr": atr[ix[0]:ix[-1] + 1]})
    _CACHE.update(k=ck, v=data)
    return data


def load(days):
    import sipbench
    ds = sipbench.stored_days()[-days:]
    ck = (ds[0], ds[-1], len(ds)) if ds else None
    if ck and _CACHE.get("k") == ck:
        return _CACHE["v"]

    def one(d):
        x = sipbench.get_day(d)
        out = []
        if not x:
            return out
        for p in [q for q in x["picks"] if float(q.get("atr") or 0) >= sipbench.MIN_ATR][:sipbench.SIP_TOP]:
            s = p["sym"]
            reg = [b for b in x["b"].get(s) or [] if "09:30" <= b["t"][11:16] < "16:00"]
            if len(reg) < 20:
                continue
            h = x["h"].get(s) or []
            prior = [b for b in h if b["t"][:10] < d and "09:30" <= b["t"][11:16] < "16:00"]
            last = max((b["t"][:10] for b in prior), default=None)
            prev = [b for b in prior if b["t"][:10] == last]
            pm = [b for b in h if b["t"][:10] == d and "04:00" <= b["t"][11:16] < "09:30"]
            rng30 = [b["h"] - b["l"] for b in prev]
            seed = sum(rng30) / len(rng30) / math.sqrt(6) if rng30 else None    # a 30-min range -> about a 5-min one
            out.append({"sym": s, "d": d, "reg": reg, "prev": prev, "pm": pm, "atr": _atr_seeded(reg, seed)})
        return out
    data = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for xs in ex.map(one, ds):
            data += xs
    _CACHE.update(k=ck, v=data)
    return data


def _atr_seeded(bars, seed, n=14):
    out, a = [], seed
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"]))
        a = tr if a is None else a + (tr - a) / n
        out.append(a)
    return out


def trades(data, P):
    """[(day, sym, R)] of the setting on the series."""
    out = []
    for x in data:
        o = {"levels": [], "sweeps": [], "mss": [], "fvgs": [], "obs": [], "setups": []}
        try:
            smc._day(x["reg"], x["atr"], list(range(len(x["reg"]))), smc.day_pools(x["prev"], x["pm"], P), P, o)
        except Exception:
            continue
        out += [(x["d"], x["sym"], s["R"], s.get("exitWhy")) for s in o["setups"] if s.get("R") is not None]
    return out


def score(tr, cost=COST):
    n = len(tr)
    if not n:
        return {"n": 0}
    rs = [t[2] - cost for t in tr]
    byd, bym = {}, {}
    for t, r in zip(tr, rs):
        byd[t[0]] = byd.get(t[0], 0) + r
        bym[t[0][:7]] = bym.get(t[0][:7], 0) + r
    dv = list(byd.values())
    m = sum(dv) / len(dv)
    sd = math.sqrt(sum((v - m) ** 2 for v in dv) / max(1, len(dv) - 1))
    tot = sum(rs)
    w = [r for r in rs if r > 0]
    gl = -sum(r for r in rs if r <= 0)
    eq = pk = dd = 0.0
    for r in rs:
        eq += r
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    pos_m = [v for v in bym.values() if v > 0]
    return {"n": n, "days": len(dv), "totR": round(tot, 2), "avgR": round(tot / n, 4), "win": round(len(w) / n * 100),
            "pf": round(sum(w) / gl, 2) if gl > 0 else None, "t": round(m / sd * math.sqrt(len(dv)), 2) if sd > 0 and len(dv) >= 5 else None,
            "dd": round(dd, 2), "monthShare": round(max(pos_m) / sum(pos_m), 2) if tot > 0 and pos_m else None,
            "months": len(bym), "posMonths": len(pos_m)}


def min_trades(st):
    return 60 if st.get("universe", "index") == "index" else MIN_TRADES


def fitness(r, need=MIN_TRADES):
    if not r.get("n"):
        return -10.0
    f = r.get("t") or 0.0
    if r["n"] < need:
        f = min(f, 0) - 3 * (1 - r["n"] / need)  # too few trades: below every setting that has enough
    if (r.get("monthShare") or 0) > MONTH_SHARE:
        f -= 2 * (r["monthShare"] - MONTH_SHARE) / (1 - MONTH_SHARE) * max(1.0, abs(f))
    return round(f, 3)


# ---------------------------------------------------------------- state
def state(sub):
    r = db.get(db.upk(sub), SK) or {}
    st = json.loads(r["data"]) if r.get("data") else {}
    if st and st.get("v") != VERSION:                  # scores of an older trade simulation: tried again
        st.update(top=[], seen={}, evals=0, log=[])
    st["v"] = VERSION
    for k, v in (("on", False), ("days", 250), ("universe", "index"), ("evals", 0), ("top", []), ("seen", {}), ("log", []), ("runs", 0)):
        st.setdefault(k, v)
    return st


USER_KEYS = ("on", "days", "stopReq", "resetAt", "start", "universe")


def _save(sub, st, user=False):
    if not user:
        cur = db.get(db.upk(sub), SK)
        if cur and cur.get("data"):
            try:
                live = json.loads(cur["data"])
                for k in USER_KEYS:
                    if k in live:
                        st[k] = live[k]
            except ValueError:
                pass
        if st.get("resetAt") and st["resetAt"] > (st.get("runStarted") or ""):
            st.update(top=[], seen={}, evals=0, log=[])
    data = json.dumps(st, default=str, separators=(",", ":"))
    while len(data.encode()) > MAX_ITEM and st["seen"]:
        st["seen"] = dict(sorted(st["seen"].items(), key=lambda kv: -kv[1])[:int(len(st["seen"]) * 0.7)])
        data = json.dumps(st, default=str, separators=(",", ":"))
    db.put({"PK": db.upk(sub), "SK": SK, "data": data})


def public(sub):
    st = state(sub)
    timed_out = bool(st.get("running") and st["running"] < iso(now_ny() - timedelta(minutes=12)))
    return {k: st.get(k) for k in ("on", "days", "universe", "evals", "step", "last", "split", "nSeries", "avgEval", "runs")} | {
        "running": None if timed_out else st.get("running"), "log": st["log"][-8:], "tried": len(st["seen"]),
        "top": [{**t, "params": to_params(t["g"])} for t in st["top"][:TOP]], "cost": COST, "minTrades": min_trades(st)}


def save_cfg(sub, b):
    st = state(sub)
    b = b or {}
    if "on" in b:
        st["on"] = bool(b["on"])
        if not st["on"] and st.get("running"):
            st["stopReq"] = iso(now_ny())
    if b.get("days"):
        nd = max(60, min(500, int(b["days"])))
        if nd != st["days"]:
            b["reset"] = True
        st["days"] = nd
    if b.get("universe") in ("index", "sip"):
        if b["universe"] != st["universe"]:
            b["reset"] = True
        st["universe"] = b["universe"]
    if b.get("start"):                                 # the blocks on the page: the first setting tried
        st["start"] = from_params(smc.clean(b["start"]))
    if b.get("reset"):
        st.update(top=[], seen={}, evals=0, log=[], resetAt=iso(now_ny()))
    _save(sub, st, user=True)
    return public(sub)


# ---------------------------------------------------------------- one run
def run(sub, budget_s=470, seed=None):
    t0 = time.time()
    rng = random.Random(seed)
    st = state(sub)
    st["running"] = st["runStarted"] = iso(now_ny())
    st["step"] = "loading the stocks-in-play history"
    st.pop("stopReq", None)
    _save(sub, st)
    idx = st["universe"] == "index"
    if idx:
        st["step"] = "loading QQQ / SPY minute candles"
        _save(sub, st)
        by_tf = load_index(sub, st["days"])
        data = by_tf[5]
    else:
        data = load(st["days"])
        by_tf = {5: data}
    if not data:
        raise ValueError("No QQQ / SPY minute candles." if idx else "No stocks-in-play history yet: build it first (AI tab → criteria → Build stocks-in-play history).")
    ds = sorted({x["d"] for x in data})
    cut = ds[int(len(ds) * TRAIN)]
    split = {tf: ([x for x in xs if x["d"] < cut], [x for x in xs if x["d"] >= cut]) for tf, xs in by_tf.items()}
    need = min_trades(st)
    st["split"] = {"from": ds[0], "cut": cut, "to": ds[-1], "trainDays": sum(1 for d in ds if d < cut), "testDays": sum(1 for d in ds if d >= cut)}
    st["nSeries"] = len(data)
    st["step"] = "searching"
    _save(sub, st)
    seen, top = st["seen"], st["top"]
    queue = []
    fix = (lambda g: g if idx else {**g, "tf": 5})     # the stocks-in-play history is 5m only
    if st.get("start") and key(fix(st["start"])) not in seen:
        queue.append(fix(st["start"]))
    if not top and key(fix(from_params(smc.PARAMS))) not in seen:
        queue.append(fix(from_params(smc.PARAMS)))
    n_run, last_save, times = 0, time.time(), []
    while time.time() - t0 < budget_s - max(times[-5:] or [10]) * 1.5:
        if queue:
            g = queue.pop(0)
        elif not top or rng.random() < 0.3:
            g = {**rand_g(rng), "tf": rng.choice([1, 5]) if idx else 5}
        else:
            pool = top[:8]
            g = fix(mutate(rng.choice(pool[:3] if rng.random() < 0.5 else pool)["g"], rng))
        k = key(g)
        if k in seen:
            continue
        t1 = time.time()
        P = to_params(g)
        train, test = split.get(P["tf"]) or split[5]
        r_tr = score(trades(train, P))
        f = fitness(r_tr, need)
        seen[k] = f
        st["evals"] += 1
        n_run += 1
        if len(top) < TOP or f > top[-1]["fit"]:
            r_te = score(trades(test, P))
            top.append({"g": g, "fit": f, "train": r_tr, "test": r_te, "at": iso(now_ny())})
            top.sort(key=lambda x: -x["fit"])
            del top[TOP:]
        times.append(time.time() - t1)
        if time.time() - last_save > 45:
            cur = state(sub)
            if not cur["on"] or cur.get("stopReq") or (cur.get("resetAt") or "") > st["runStarted"] or cur["days"] != st["days"] or cur["universe"] != st["universe"]:
                break
            st["avgEval"] = round(sum(times[-20:]) / len(times[-20:]), 2)
            _save(sub, st)
            st = {**st, **{k: v for k, v in state(sub).items() if k in USER_KEYS}}
            seen, top = st["seen"], st["top"]
            last_save = time.time()
    st["avgEval"] = round(sum(times[-20:]) / len(times[-20:]), 2) if times else None
    st["running"] = None
    st["step"] = None
    st["runs"] = st.get("runs", 0) + 1
    best = top[0] if top else None
    st["last"] = {"at": iso(now_ny()), "tried": n_run, "best": best and best["fit"]}
    st["log"] = (st["log"] + [{"at": iso(now_ny()), "tried": n_run, "best": best and best["fit"],
                               "test": best and (best["test"] or {}).get("avgR")}])[-20:]
    _save(sub, st)
    return st


def job(sub, params=None):
    import research
    try:
        run(sub)
    except Exception as e:
        st = state(sub)
        st.update(running=None, step=None, last={"at": iso(now_ny()), "error": str(e)[:200]})
        _save(sub, st, user=True)
        return {"error": str(e)[:200]}
    st = state(sub)
    if st["on"] and not (params or {}).get("once"):
        research._invoke(sub, "smcopt", {})
    return {"evals": st["evals"]}


def kick():
    """Hourly: restart the chain of every user with the optimizer on (after a failed run)."""
    import research
    n = 0
    for it in db.scan_sk(SK):
        try:
            st = json.loads(it["data"])
        except (KeyError, ValueError):
            continue
        if not st.get("on") or (st.get("running") and st["running"] > iso(now_ny() - timedelta(minutes=12))):
            continue
        research._invoke(it["PK"][5:], "smcopt", {})
        n += 1
    return n
