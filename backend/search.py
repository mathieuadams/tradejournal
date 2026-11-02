"""The optimizer: an open-ended search over a strategy's settings, run in the background until stopped.

Each setting is a full study of the strategy (all its sessions); its trades are split at a date: the first 60% of the
sessions = train, the rest = test. A setting is scored on the TRAIN trades only: the t of the R per day (net of the
costs, in R of each trade's risk), with at least MIN_TRADES trades and no month over MONTH_SHARE of the profit. The
test days are never used to choose: they are reported, and a setting that is good on both is the one to try.

The search: the current rules first, then random settings (30%) and small changes of the best ones (70%).
~8 minutes per run, chained (one run at a time) while it's on. State in S3 (strat/<sub>/<id>/search); the controls
(on / stop / restart) in DynamoDB (SEARCH#<id>) so a page click reaches a running run."""
import json
import math
import random
import time
from datetime import timedelta

import db
import futdata
import store
import strategy
from util import BadRequest, iso, now_ny

MIN_TRADES = 60
MONTH_SHARE = 0.35
TOP = 15
RUN_S = 480
TEST_BUDGET = None          # tests: a shorter run
SEEN_MAX = 20000

# The settings each strategy's search may change (the rest stay as saved). Not listed: its optimizer grid.
SPACES = {
    "ifvg": {
        "tf": [5, 1], "bias": ["all3", "h1"], "asia": ["none", "dir", "hold"], "minGap": [0, 2, 4, 8, 16], "fvgMax": [6, 12, 24, 48],
        "gapFrom": ["08:00", "04:00", "09:30", "18:00"], "stop": ["fvg", "swing"], "bufTicks": [0, 2, 4, 8], "hours": ["all", "rth"],
        "lastEntry": ["10:30", "11:00", "12:00", "13:00", "14:00", "15:00"], "maxDay": [1, 2, 3, 5], "side": ["both", "long", "short"],
        "exit": ["trail", "target"], "tgt": ["liq2r", "liq", "r"], "tgtR": [1.0, 1.5, 2.0, 3.0, 4.0], "minRR": [0, 0.5, 1.0, 1.5],
        "pivLen": [2, 3, 5], "beR": [0, 0.5, 1.0], "partR": [0.5, 1.0, 1.5, 2.0], "partPct": [0, 25, 50, 75],
        "trail": ["swing", "r", "ema15", "none"], "trailN": [2, 3, 5, 8], "trailR": [0.25, 0.5, 1.0, 1.5, 2.0],
    },
}
# a setting that only matters when another one has a value: (key, needs) -> dropped from the key otherwise
NEEDS = {
    "ifvg": {"tgt": lambda g: g["exit"] == "target", "tgtR": lambda g: g["exit"] == "target" and g["tgt"] == "r",
             "minRR": lambda g: g["exit"] == "target", "pivLen": lambda g: g["exit"] == "target" and g["tgt"] != "r",
             "beR": lambda g: g["exit"] == "trail", "partR": lambda g: g["exit"] == "trail" and g["partPct"] > 0, "partPct": lambda g: g["exit"] == "trail",
             "trail": lambda g: g["exit"] == "trail", "trailN": lambda g: g["exit"] == "trail" and g["trail"] == "swing",
             "trailR": lambda g: g["exit"] == "trail" and g["trail"] == "r", "gapFrom": lambda g: g["hours"] == "rth"},
}


def space(setup):
    return SPACES.get(setup) or strategy.GRIDS.get(setup) or {}


def key(setup, g):
    """Same key for settings that trade the same (a value that doesn't apply doesn't count)."""
    nd = NEEDS.get(setup) or {}
    return json.dumps({k: v for k, v in g.items() if k not in nd or nd[k](g)}, sort_keys=True, default=str)


def _used(setup, g):
    nd = NEEDS.get(setup) or {}
    return [k for k in g if k not in nd or nd[k](g)]


def rand_g(setup, rng):
    return {k: rng.choice(v) for k, v in space(setup).items()}


def mutate(setup, g, rng):
    sp = space(setup)
    x = dict(g)
    for k in rng.sample(list(sp), min(len(sp), rng.choice([1, 1, 2, 3]))):
        vs = sp[k]
        if isinstance(vs[0], (int, float)) and not isinstance(vs[0], bool) and x.get(k) in vs and rng.random() < 0.7:
            i = vs.index(x[k]) + rng.choice([-1, 1])        # numbers: mostly the next value up / down
            x[k] = vs[max(0, min(len(vs) - 1, i))]
        else:
            x[k] = rng.choice([v for v in vs if v != x.get(k)] or vs)
    return x


def from_params(setup, P):
    sp = space(setup)
    g = {}
    for k, vs in sp.items():
        v = P.get(k, vs[0])
        g[k] = v if v in vs else min(vs, key=lambda o: abs(float(o) - float(v))) if isinstance(vs[0], (int, float)) and not isinstance(vs[0], bool) \
            and isinstance(v, (int, float)) else vs[0]
    return g


# ---------------------------------------------------------------- scoring
def r_net(t):
    pv = futdata.SPEC[t["s"]]["pv"]
    risk = t.get("risk")
    return t["net"] / (risk * pv * t.get("c", 1)) if risk else None


def score(ts):
    rs = [(t["d"], r_net(t)) for t in ts]
    rs = [(d, r) for d, r in rs if r is not None]
    n = len(rs)
    if not n:
        return {"n": 0}
    byd, bym = {}, {}
    for d, r in rs:
        byd[d] = byd.get(d, 0) + r
        bym[d[:7]] = bym.get(d[:7], 0) + r
    dv = list(byd.values())
    m = sum(dv) / len(dv)
    sd = math.sqrt(sum((v - m) ** 2 for v in dv) / max(1, len(dv) - 1))
    tot = sum(r for _, r in rs)
    eq = pk = dd = 0.0
    for _, r in rs:
        eq += r
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    pos_m = [v for v in bym.values() if v > 0]
    return {"n": n, "days": len(dv), "win": round(sum(1 for _, r in rs if r > 0) / n * 100), "avgR": round(tot / n, 3), "totR": round(tot, 2),
            "t": round(m / sd * math.sqrt(len(dv)), 2) if sd > 0 and len(dv) >= 5 else None, "dd": round(dd, 2),
            "monthShare": round(max(pos_m) / sum(pos_m), 2) if tot > 0 and pos_m else None}


def fitness(r, need=MIN_TRADES):
    if not r.get("n"):
        return -10.0
    f = r.get("t") or 0.0
    if r["n"] < need:
        f = min(f, 0) - 3 * (1 - r["n"] / need)           # too few trades: below every setting that has enough
    if (r.get("monthShare") or 0) > MONTH_SHARE:
        f -= 2 * (r["monthShare"] - MONTH_SHARE) / (1 - MONTH_SHARE) * max(1.0, abs(f))
    return round(f, 3)


# ---------------------------------------------------------------- state
def _key(sub, sid):
    return f"strat/{sub}/{sid}/search"


def _ctl(sub, sid):
    return db.get(db.upk(sub), "SEARCH#" + sid) or {"PK": db.upk(sub), "SK": "SEARCH#" + sid, "on": False}


def _set_ctl(sub, sid, **kw):
    it = _ctl(sub, sid)
    it.update(kw, at=iso(now_ny()))
    db.put(it)
    return it


def state(sub, sid):
    st = store.get_json(_key(sub, sid), None) or {}
    for k, v in (("evals", 0), ("top", []), ("seen", {}), ("log", []), ("runs", 0)):
        st.setdefault(k, v)
    return st


def public(sub, sid):
    s = strategy.get(sub, sid)
    st, c = state(sub, sid), _ctl(sub, sid)
    run = st.get("running")
    if run and run < iso(now_ny() - timedelta(minutes=16)):
        run = None                                      # a run that died
    return {"on": bool(c.get("on")), "running": run, "step": st.get("step") if run else None, "tried": st.get("evals", 0), "avgEval": st.get("avgEval"),
            "split": st.get("split"), "log": st["log"][-8:], "done": st.get("done"), "error": st.get("error"),
            "top": [{**x, "keys": _used(s["setup"], x["g"])} for x in st["top"][:TOP]], "minTrades": MIN_TRADES, "monthShare": MONTH_SHARE,
            "space": list(space(s["setup"]))}


def control(sub, sid, action):
    s = strategy.get(sub, sid)
    if not space(s["setup"]):
        raise BadRequest("No search space for this strategy.")
    if action == "start":
        if not all(x["ok"] for x in strategy.data_check(sub, sid)):
            raise BadRequest("Not enough data: download at least 60 days of candles (Data page).")
        _set_ctl(sub, sid, on=True)
        st = state(sub, sid)
        if not st.get("running") or st["running"] < iso(now_ny() - timedelta(minutes=16)):
            import jobs
            jobs._invoke({"sub": sub, "name": "search#" + sid, "job": "search", "params": {"id": sid}})
    elif action == "stop":
        _set_ctl(sub, sid, on=False, stopAt=iso(now_ny()))
    elif action == "restart":
        _set_ctl(sub, sid, resetAt=iso(now_ny()))
        st = state(sub, sid)
        if not st.get("running") or st["running"] < iso(now_ny() - timedelta(minutes=16)):
            store.put_json(_key(sub, sid), {"evals": 0, "top": [], "seen": {}, "log": [], "runs": 0})
    else:
        raise BadRequest("action must be start, stop or restart.")
    return public(sub, sid)


def _dedup(top):
    out, sigs = [], set()
    for x in sorted(top, key=lambda x: -x["fit"]):
        sig = (x["train"].get("n"), x["train"].get("totR"))
        if sig not in sigs:
            sigs.add(sig)
            out.append(x)
    return out


# ---------------------------------------------------------------- one run
def run(sub, sid, budget_s=RUN_S, seed=None):
    """One run (~8 minutes): returns True to continue in a new run, False when stopped / nothing left to try."""
    t0 = time.time()
    rng = random.Random(seed)
    s = strategy.get(sub, sid)
    setup, base = s["setup"], dict(s["params"])
    st = state(sub, sid)
    c = _ctl(sub, sid)
    if c.get("resetAt") and c["resetAt"] > (st.get("runStarted") or ""):
        st = {"evals": 0, "top": [], "seen": {}, "log": [], "runs": 0}
    if st.get("base") and st["base"].get("tickers") != base.get("tickers") or st.get("baseDays") not in (None, base.get("days")):
        st.update(top=[], seen={}, evals=0)               # other contracts / sessions: the scores don't compare
    st.update(running=iso(now_ny()), runStarted=iso(now_ny()), step="loading the candles", base=base, baseDays=base.get("days"), error=None)
    store.put_json(_key(sub, sid), st)
    days = futdata.trading_days(base["tickers"][0])[-int(base.get("days") or 250):]
    cut = days[int(len(days) * strategy.TRAIN)] if days else None
    st["split"] = {"from": days[0] if days else None, "cut": cut, "to": days[-1] if days else None,
                   "trainDays": sum(1 for d in days if d < cut), "testDays": sum(1 for d in days if d >= cut)}
    seen, top = st["seen"], _dedup(st["top"])
    queue = [from_params(setup, base)]
    n_run, times, last_save, misses, stop = 0, [], time.time(), 0, False
    st["step"] = "searching"
    while time.time() - t0 < budget_s - max(times[-5:] or [10]) * 1.5:
        if queue:
            g = queue.pop(0)
        elif not top or rng.random() < 0.3:
            g = rand_g(setup, rng)
        else:
            pool = top[:8]
            g = mutate(setup, rng.choice(pool[:3] if rng.random() < 0.5 else pool)["g"], rng)
        k = key(setup, g)
        if k in seen:
            misses += 1
            if misses > 400:                             # (nearly) every setting tried
                st["done"] = True
                stop = True
                break
            continue
        misses = 0
        t1 = time.time()
        trial = {**s, "params": strategy._params(setup, {**base, **g})}
        ts = [t for t in strategy.run_trades(sub, trial) if t.get("why") != "open"]
        r_tr = score([t for t in ts if t["d"] < cut])
        f = fitness(r_tr)
        seen[k] = f
        st["evals"] += 1
        n_run += 1
        same = any(x["train"].get("n") == r_tr.get("n") and x["train"].get("totR") == r_tr.get("totR") for x in top)
        if not same and (len(top) < TOP or f > top[-1]["fit"]):
            top.append({"g": g, "fit": f, "train": r_tr, "test": score([t for t in ts if t["d"] >= cut]), "at": iso(now_ny())})
            top.sort(key=lambda x: -x["fit"])
            del top[TOP:]
        times.append(time.time() - t1)
        if time.time() - last_save > 40:
            c = _ctl(sub, sid)
            if not c.get("on") or (c.get("resetAt") or "") > st["runStarted"]:
                stop = not c.get("on")
                break
            st.update(top=top, avgEval=round(sum(times[-20:]) / len(times[-20:]), 2), running=iso(now_ny()))
            store.put_json(_key(sub, sid), st)
            last_save = time.time()
    if len(seen) > SEEN_MAX:
        for kk in list(seen)[:len(seen) - SEEN_MAX]:
            seen.pop(kk)
    best = top[0] if top else None
    st.update(top=top, seen=seen, running=None, step=None, runs=st.get("runs", 0) + 1,
              avgEval=round(sum(times[-20:]) / len(times[-20:]), 2) if times else st.get("avgEval"))
    st["log"] = (st["log"] + [{"at": iso(now_ny()), "tried": n_run, "best": best and best["fit"]}])[-20:]
    c = _ctl(sub, sid)
    if (c.get("resetAt") or "") > st["runStarted"]:
        st = {"evals": 0, "top": [], "seen": {}, "log": [], "runs": 0}
    store.put_json(_key(sub, sid), st)
    return bool(c.get("on")) and not stop


def job(sub, sid, event, invoke):
    """One run at a time per strategy, chained while it's on."""
    pk = db.upk(sub)
    lock = db.get(pk, "SEARCHLOCK#" + sid)
    if lock and lock.get("at", "") > iso(now_ny() - timedelta(minutes=15)):
        return {"skipped": "a run is already going"}
    db.put({"PK": pk, "SK": "SEARCHLOCK#" + sid, "at": iso(now_ny())})
    try:
        more = run(sub, sid, budget_s=TEST_BUDGET or RUN_S)
    except Exception as e:
        import traceback
        traceback.print_exc()
        st = state(sub, sid)
        st.update(running=None, step=None, error=str(e)[:200])
        st["log"] = (st["log"] + [{"at": iso(now_ny()), "error": str(e)[:120]}])[-20:]
        store.put_json(_key(sub, sid), st)
        _set_ctl(sub, sid, on=False)
        more = False
    db.delete(pk, "SEARCHLOCK#" + sid)
    if more:
        invoke(event)
    return {"continued": more}
