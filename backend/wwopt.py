"""Unusual-flow (wall watch) optimizer: the wall watch rules and numbers tuned on the watchlist's history, the same
way as the smart-money optimizer (smcopt.py):

  * the data: every ticker of the wall watch lists, hourly candles + walls + call alerts (backtest._ticker_data),
    loaded once per container;
  * train = the first 60% of the period, test = the rest (never used to choose);
  * a setting = the switches of the entry / exit rules and their numbers; scored on the train trades by the t of the R
    per day (smcopt.score / fitness), at least MIN_TRADES trades, no month over 35% of the profit;
  * search: random settings + changes of 1-3 rules / numbers of the best ones; the best TOP kept (no two with the same
    trades). "Use" puts a setting in the Strategy lab (test it there, then "Use these settings live").
Runs in the background, a run of ~8 minutes starting the next one while it is on."""
import json
import random
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import db
from util import iso, now_ny

SK = "WWOPT"
VERSION = 4                                            # 4: scored on the weakest third of the train period too
MIN_TRADES = 40                                        # default; the panel picks 40 / 80 / 150
MIN_CHOICES = (40, 80, 150)
FOLDS = 3                                              # the train period cut in 3: the edge must be there in each part
TRAIN = 0.6
TOP = 15
MAX_ITEM = 300_000

BOOL_KEYS = ["alertReq", "relFlow", "greenOnly", "aboveFlip", "abovePW", "belowPW", "vwapOn", "emaOn", "emaOverVwap", "kcNotExt",
             "kcdLower", "kchLower", "orbAbove", "fvgOn", "noAboveCW", "cwRoomOn", "vwapReset", "pwNeCw", "inGreen", "inNotRed",
             "usePW", "useFlip", "pwSetup", "flipSetup", "mergedSetup",
             "stopOn", "trailOn", "targetOn", "flipExit", "vwapExit", "emaExit", "fvgExit", "inRedExit", "inYExit", "cwLost", "scaleOn", "scaleBE"]
NUMS = {"stopAtr": [0.3, 0.5, 0.75, 1.0, 1.5], "trailAtr": [0.5, 1.0, 1.5, 2.0, 3.0], "tgtBuf": [0.0, 0.25, 0.5, 1.0],
        "retestDays": [2, 3, 5, 8], "retestZone": [0.1, 0.25, 0.5, 0.75], "cwRoom": [0.5, 1.0, 1.5, 2.0], "relMult": [2.0, 3.0, 5.0],
        "retestMin": [250000, 500000, 1000000], "scaleR": [0.5, 1.0, 1.5, 2.0], "scalePct": [25, 50, 75], "fvgMin": [0.1, 0.2, 0.4]}
NEEDS = {"stopAtr": "stopOn", "trailAtr": "trailOn", "cwRoom": "cwRoomOn", "relMult": "relFlow", "scaleR": "scaleOn", "scalePct": "scaleOn",
         "scaleBE": "scaleOn", "fvgMin": "fvgOn"}
SPACE = {**{k: [True, False] for k in BOOL_KEYS}, **NUMS}
# daily candles (wallwatch.detect_daily, entry at the daily close): the rules that engine reads
DAILY_BOOL = ["pwTest", "greenOnly", "inGreen", "inNotRed", "fvgOn", "aboveFlip", "abovePW", "belowPW", "emaOn", "kcdLower", "kcNotExt",
              "pwNeCw", "noAboveCW", "cwRoomOn", "inhGreen", "inhNotRed", "sqdOn", "sqdFire", "sqdMom",
              "stopOn", "trailOn", "targetOn", "flipExit", "emaExit", "fvgExit", "inRedExit", "inYExit", "inhYExit", "cwLost", "kcRide",
              "pwExit", "sqdExit", "scaleOn", "scaleBE"]
DAILY_NUMS = {k: NUMS[k] for k in ("stopAtr", "trailAtr", "tgtBuf", "retestZone", "cwRoom", "scaleR", "scalePct", "fvgMin")}
DSPACE = {**{k: [True, False] for k in DAILY_BOOL}, **DAILY_NUMS}
TFS = ("1h", "1D")
_MODE = {"tf": "1h"}                                   # the timeframe of the run going (its search space)


def sp():
    return DSPACE if _MODE["tf"] == "1D" else SPACE


MAG7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]
UNIVERSES = ("mag7", "all")                            # mag7 first, then every ticker of the wall watch lists


def tickers(st, live):
    return list(MAG7) if st.get("universe", "mag7") == "mag7" else list(live.get("tickers") or [])


def from_cfg(c):
    import wallwatch
    return {k: c.get(k, wallwatch.PARAMS.get(k)) for k in sp()}


def key(g):
    k = dict(g)
    for n, blk in NEEDS.items():
        if not k.get(blk):
            k.pop(n, None)
    return json.dumps(k, sort_keys=True, default=str)


def valid(g):
    if _MODE["tf"] == "1D":
        return bool(g.get("stopOn") or g.get("trailOn") or g.get("targetOn"))
    return bool(g.get("usePW") or g.get("useFlip")) and bool(g.get("pwSetup") or g.get("flipSetup") or g.get("mergedSetup")) \
        and bool(g.get("stopOn") or g.get("trailOn") or g.get("targetOn"))


def rand_g(rng, base):
    for _ in range(50):
        g = dict(base)
        for k in rng.sample(list(sp()), rng.randint(3, 8)):
            g[k] = rng.choice(sp()[k])
        if valid(g):
            return g
    return dict(base)


def mutate(g, rng):
    for _ in range(30):
        x = dict(g)
        for k in rng.sample(list(sp()), rng.choice([1, 1, 2, 3])):
            vs = sp()[k]
            if k in NUMS and x.get(k) in vs and rng.random() < 0.7:
                i = vs.index(x[k]) + rng.choice([-1, 1])
                x[k] = vs[max(0, min(len(vs) - 1, i))]
            else:
                x[k] = rng.choice([v for v in vs if v != x.get(k)] or vs)
        if valid(x) and x != g:
            return x
    return g


def mutate_heavy(g, rng):
    """A change of 1-2 exits / numbers / setups (the entry rules are screened separately)."""
    import wallwatch
    hk = [k for k in SPACE if k not in wallwatch.SEARCH_RULES]
    for _ in range(30):
        x = dict(g)
        for k in rng.sample(hk, rng.choice([1, 1, 2])):
            vs = SPACE[k]
            if k in NUMS and x.get(k) in vs and rng.random() < 0.7:
                i = vs.index(x[k]) + rng.choice([-1, 1])
                x[k] = vs[max(0, min(len(vs) - 1, i))]
            else:
                x[k] = rng.choice([v for v in vs if v != x.get(k)] or vs)
        if valid(x) and x != g:
            return x
    return g


# ---------------------------------------------------------------- the data
_CACHE = {}


def load(sub, days, live, budget_s=300):
    """{sym: ticker data} for the wall watch tickers (hourly candles, walls, alerts), loaded in parallel."""
    import backtest
    tick = list(live.get("tickers") or [])
    ck = (sub, days, tuple(sorted(tick)), now_ny().strftime("%Y-%m-%d"), live.get("_wallsAt"), live.get("vwapDays"), live.get("emaLen"), live.get("kcLen"), live.get("kcMult"))
    if _CACHE.get("k") == ck:
        return _CACHE["v"], _CACHE["err"]
    t0, data, errs = time.time(), {}, []

    def one(sym):
        try:
            return sym, backtest._ticker_data(sub, sym, days, live, False)
        except Exception:
            time.sleep(6)                              # market data rate limit: once more
            return sym, backtest._ticker_data(sub, sym, days, live, False)
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(one, t) for t in tick]
        for f in futs:
            try:
                sym, d = f.result(timeout=max(1, budget_s - (time.time() - t0)))
                if d:
                    data[sym] = d
            except Exception as e:
                errs.append(str(e)[:80] or "time limit")
    _CACHE.update(k=ck, v=data, err=errs)
    return data, errs


def trades(data, cfg, first, cut=None, part="train"):
    """[(day, sym, R, exit)] of a setting on the tickers (train: before `cut`, test: from it)."""
    import wallwatch
    out = []
    for sym, d in data.items():
        try:
            sigs, _ = wallwatch.detect_retest(d["bars"], d["walls"], first, cfg, d["datr"], d["alerts"], None, d["vmap"], d["emap"], d.get("extra"))
        except Exception:
            continue
        for s in sigs:
            if s.get("R") is None:
                continue
            if cut and ((s["at"][:10] >= cut) if part == "train" else (s["at"][:10] < cut)):
                continue
            out.append((s["at"][:10], sym, float(s["R"]), s.get("status")))
    return out


def train_slice(data, cut):
    """The tickers' data cut at the end of the train period: a train evaluation doesn't compute the test months."""
    return {sym: {**d, "bars": [b for b in d["bars"] if b["t"][:10] < cut]} for sym, d in data.items()}


def entry_keys():
    import wallwatch
    return [k for k in BOOL_KEYS if k in wallwatch.SEARCH_RULES]


def heavy_key(g):
    """The part of a setting that changes the candidate trades (exits, numbers, setups); the entry rules don't."""
    ek = set(entry_keys())
    return key({k: v for k, v in g.items() if k not in ek})


def candidates(data, cfg, first):
    """{sym: [candidate]}: every candle at the support traded alone with the exits of cfg, tagged with each entry
    rule (wallwatch _collect): any mix of entry rules is then replayed in milliseconds."""
    import wallwatch
    out = {}
    base = {**cfg, **{k: False for k in entry_keys()}}
    for sym, d in data.items():
        col = []
        try:
            wallwatch.detect_retest(d["bars"], d["walls"], first, {**base, "_collect": col}, d["datr"], d["alerts"], None, d["vmap"], d["emap"], d.get("extra"))
        except Exception:
            continue
        out[sym] = sorted(col, key=lambda c: c["at"])
    return out


def replay(cands, g):
    """The trades of the entry rules of g on the candidates: one at a time per ticker (the quick screen)."""
    need = [k for k in entry_keys() if g.get(k)]
    out = []
    for sym, cs in cands.items():
        last = ""
        for c in cs:
            if c["at"] <= last or not all(c["f"].get(r) for r in need):
                continue
            out.append((c["at"][:10], sym, float(c["R"]), "screen"))
            last = c["end"]
    return out


def fit(tr, need, bounds):
    """The t of the R per day (smcopt.fitness), capped by the weakest third of the train period (its t x sqrt(3),
    the same scale): a mix that is only good in one stretch of months — luck — scores low. Returns (fit, fold ts)."""
    import smcopt
    r = smcopt.score(tr, cost=0)
    f = smcopt.fitness(r, need)
    fts = []
    for a, b in zip(bounds, bounds[1:]):
        rf = smcopt.score([t for t in tr if a <= t[0] < b], cost=0)
        fts.append(rf.get("t") if rf.get("t") is not None else (-1.0 if (rf.get("totR") or 0) <= 0 else 0.0))
    if fts and f > 0:
        f = round(min(f, min(fts) * FOLDS ** 0.5), 3)
    return f, r, fts


# ---------------------------------------------------------------- state
def state(sub):
    r = db.get(db.upk(sub), SK) or {}
    st = json.loads(r["data"]) if r.get("data") else {}
    if st and st.get("v") != VERSION:
        st.update(top=[], seen={}, evals=0, log=[])
    st["v"] = VERSION
    for k, v in (("on", False), ("days", 250), ("universe", "mag7"), ("minTrades", 80), ("tf", "1h"), ("evals", 0), ("top", []), ("seen", {}), ("log", []), ("runs", 0)):
        st.setdefault(k, v)
    return st


USER_KEYS = ("on", "days", "universe", "minTrades", "tf", "stopReq", "resetAt")


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
    import smcopt
    st = state(sub)
    timed_out = bool(st.get("running") and st["running"] < iso(now_ny() - timedelta(minutes=12)))
    return {k: st.get(k) for k in ("on", "days", "universe", "tf", "evals", "step", "last", "split", "nTickers", "avgEval", "runs", "loadErrors", "cycle")} | {
        "running": None if timed_out else st.get("running"), "log": st["log"][-8:], "tried": st.get("evals", 0), "screened": st.get("screened", 0),
        "walls": walls_status(sub),
        "top": [{**t, "params": t["g"]} for t in smcopt._dedup(st["top"])[:TOP]], "minTrades": st.get("minTrades") or MIN_TRADES}


def save_cfg(sub, b):
    st = state(sub)
    b = b or {}
    if "on" in b:
        st["on"] = bool(b["on"])
        if not st["on"] and st.get("running"):
            st["stopReq"] = iso(now_ny())
        if st["on"]:
            st.pop("stopReq", None)
    if b.get("days"):
        nd = max(60, min(500, int(b["days"])))
        if nd != st["days"]:
            b["reset"] = True
        st["days"] = nd
    if b.get("minTrades") and int(b["minTrades"]) in MIN_CHOICES and int(b["minTrades"]) != st.get("minTrades"):
        st["minTrades"] = int(b["minTrades"])
        b["reset"] = True
    if b.get("tf") in TFS and b["tf"] != st.get("tf"):
        st["tf"] = b["tf"]
        b["reset"] = True
    if b.get("universe") in UNIVERSES and b["universe"] != st.get("universe"):
        st["universe"] = b["universe"]
        b["reset"] = True
    if b.get("reset"):
        st.update(top=[], seen={}, evals=0, log=[], resetAt=iso(now_ny()))
    _save(sub, st, user=True)
    return public(sub)


def run(sub, budget_s=470, seed=None):
    import smcopt
    import wallwatch
    t0 = time.time()
    rng = random.Random(seed)
    st = state(sub)
    tf = _MODE["tf"] = st.get("tf") or "1h"
    st["running"] = st["runStarted"] = iso(now_ny())
    st["step"] = "loading the watchlist history (hourly candles, walls, call alerts)"
    st.pop("stopReq", None)
    _save(sub, st)
    live = wallwatch.config(sub)
    wa = (db.get(db.upk(sub), WALLS_SK) or {}).get("at")
    data, errs = load(sub, st["days"] + (85 if tf == "1D" else 0), {**live, "tickers": tickers(st, live), "_wallsAt": wa}, budget_s=min(300, budget_s * 0.6))
    if not data:
        raise ValueError("No ticker data: the wall watch lists are empty, or the market data couldn't be loaded.")
    days = st["days"]
    first = (now_ny() - timedelta(days=int(days * 1.45))).strftime("%Y-%m-%d")
    cut = (now_ny() - timedelta(days=int(days * 1.45 * (1 - TRAIN)))).strftime("%Y-%m-%d")
    st["split"] = {"from": first, "cut": cut, "to": now_ny().strftime("%Y-%m-%d")}
    st["nTickers"], st["loadErrors"] = len(data), len(errs)
    st["step"] = "searching"
    _save(sub, st)
    base = from_cfg(live)
    seen, top = st["seen"], smcopt._dedup(st["top"])
    st["top"] = top
    fixed = {**live, "earnOn": False, "fixR": 0, "maxDays": 0, "pvSetup": False, "tf": tf, "fxEntry": "", "fxExit": ""}
    tdata = train_slice(data, cut)
    need = int(st.get("minTrades") or MIN_TRADES)
    d0, d1 = now_ny() - timedelta(days=int(days * 1.45)), now_ny() - timedelta(days=int(days * 1.45 * (1 - TRAIN)))
    bounds = [(d0 + (d1 - d0) * i / FOLDS).strftime("%Y-%m-%d") for i in range(FOLDS)] + [cut]
    bounds[0] = first
    ek = entry_keys()
    n_run, last_save, times = 0, time.time(), []
    st["screened"] = st.get("screened", 0)
    ccache = {}

    def confirm(g):
        """The exact train result (full engine, train months only); in the list if good enough (then its test)."""
        nonlocal n_run
        k = key(g)
        if k in seen:
            return
        t1 = time.time()
        cfg = {**fixed, **g}
        f, r_tr, fts = fit(trades(tdata, cfg, first), need, bounds)
        r_tr["folds"] = fts
        seen[k] = f
        st["evals"] += 1
        n_run += 1
        same = any((x["train"] or {}).get("n") == r_tr.get("n") and (x["train"] or {}).get("totR") == r_tr.get("totR") for x in top)
        if not same and (len(top) < TOP or f > top[-1]["fit"]):
            r_te = smcopt.score(trades(data, cfg, first, cut, "test"), cost=0)
            top.append({"g": g, "fit": f, "train": r_tr, "test": r_te, "at": iso(now_ny())})
            top.sort(key=lambda x: -x["fit"])
            del top[TOP:]
        times.append(time.time() - t1)

    first_cycle = True
    while tf == "1D" and time.time() - t0 < budget_s - max(times[-5:] or [0.5]) * 3:
        if first_cycle and key(base) not in seen:
            g = base
        elif not top or rng.random() < 0.2:
            g = rand_g(rng, base)
        else:
            g = mutate(rng.choice(top[:3] if rng.random() < 0.6 else top)["g"], rng)
        first_cycle = False
        c0 = time.time()
        confirm(g)
        st["cycle"] = round(time.time() - c0, 2)
        if time.time() - last_save > 45:
            cur_st = state(sub)
            if not cur_st["on"] or (cur_st.get("stopReq") or "") > st["runStarted"] or (cur_st.get("resetAt") or "") > st["runStarted"] \
                    or cur_st["days"] != st["days"] or cur_st.get("universe") != st.get("universe") or cur_st.get("tf") != tf:
                break
            st["avgEval"] = round(sum(times[-20:]) / len(times[-20:]), 2) if times else None
            _save(sub, st)
            st = {**st, **{k: v for k, v in state(sub).items() if k in USER_KEYS}}
            seen, top = st["seen"], st["top"]
            last_save = time.time()
    while tf == "1h" and time.time() - t0 < budget_s - max(times[-5:] or [2]) * 3:
        # 1. the exits / numbers / setups: the live ones first, then from the best (changed) or random
        if first_cycle and key(base) not in seen:
            parent = base
        elif not top or rng.random() < 0.25:
            parent = rand_g(rng, base)
        else:
            parent = rng.choice(top[:3] if rng.random() < 0.5 else top[:8])["g"]
            parent = mutate_heavy(parent, rng) if not first_cycle else parent
        first_cycle = False
        c0 = time.time()
        hk = heavy_key(parent)
        if hk not in ccache:
            if len(ccache) >= 4:
                ccache.pop(next(iter(ccache)))
            ccache[hk] = candidates(tdata, {**fixed, **parent}, first)     # one pass: every candle at the support
        cands = ccache[hk]
        # 2. the entry rules screened on those candidates (milliseconds each): hill climbing from the parent's mix
        scored = {}
        def scr(g):
            kk = key(g)
            if kk not in scored:
                scored[kk] = (fit(replay(cands, g), need, bounds)[0], g)
                st["screened"] += 1
            return scored[kk][0]
        cur = dict(parent)
        scr(cur)
        for _ in range(150):
            x = dict(cur)
            for kk in rng.sample(ek, rng.choice([1, 1, 2])):
                x[kk] = not x.get(kk)
            if scr(x) >= scr(cur):
                cur = x
            if time.time() - t0 > budget_s * 0.9:
                break
        # 3. only the best screened mix that can enter the list is confirmed by the full engine (the exact trades)
        worst = top[-1]["fit"] if len(top) >= TOP else -1e9
        for f_, g in sorted(scored.values(), key=lambda v: -v[0])[:3]:
            if time.time() - t0 > budget_s - max(times[-5:] or [2]) * 1.5:
                break
            if key(g) not in seen and f_ > worst:
                confirm(g)
                break
        st["cycle"] = round(time.time() - c0, 1)
        if time.time() - last_save > 45:
            cur_st = state(sub)
            if not cur_st["on"] or (cur_st.get("stopReq") or "") > st["runStarted"] or (cur_st.get("resetAt") or "") > st["runStarted"] or cur_st["days"] != st["days"] or cur_st.get("universe") != st.get("universe") or cur_st.get("tf") != st.get("tf"):
                break
            st["avgEval"] = round(sum(times[-20:]) / len(times[-20:]), 2) if times else None
            _save(sub, st)
            st = {**st, **{k: v for k, v in state(sub).items() if k in USER_KEYS}}
            seen, top = st["seen"], st["top"]
            last_save = time.time()
    st["avgEval"] = round(sum(times[-20:]) / len(times[-20:]), 2) if times else None
    st.update(running=None, step=None, runs=st.get("runs", 0) + 1)
    best = top[0] if top else None
    st["last"] = {"at": iso(now_ny()), "tried": n_run, "best": best and best["fit"]}
    st["log"] = (st["log"] + [{"at": iso(now_ny()), "tried": n_run, "best": best and best["fit"]}])[-20:]
    _save(sub, st)
    return st


LOCK_S = 560


def job(sub, params=None):
    """One run at a time: two runs in parallel would share the CPU and overwrite each other's results."""
    import research
    pk = db.upk(sub)
    if not db.try_lock(pk, SK + "#LOCK", LOCK_S):
        return {"skipped": "a run is already going"}
    try:
        run(sub)
    except Exception as e:
        db.unlock(pk, SK + "#LOCK")
        st = state(sub)
        st.update(running=None, step=None, last={"at": iso(now_ny()), "error": str(e)[:200]})
        st["log"] = (st.get("log") or [])[-19:] + [{"at": iso(now_ny()), "error": str(e)[:120]}]
        _save(sub, st, user=True)
        return {"error": str(e)[:200]}
    db.unlock(pk, SK + "#LOCK")
    st = state(sub)
    if st["on"] and not (params or {}).get("once"):
        research._invoke(sub, "wwopt", {})
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
        research._invoke(it["PK"][5:], "wwopt", {})
        n += 1
    return n


# ---------------------------------------------------------------- the walls history of the optimizer's tickers
WALLS_SK = "WWOPTWALLS"


def walls_days(days):
    """Weekdays covered by the optimizer's period (it starts days x 1.45 calendar days back)."""
    return int(days * 1.45 * 5 / 7) + 3


def walls_status(sub):
    it = db.get(db.upk(sub), WALLS_SK) or {}
    return {k: it.get(k) for k in ("running", "total", "left", "fetched", "errors", "at", "days")}


def walls_start(sub):
    it = db.get(db.upk(sub), WALLS_SK) or {}
    db.put({**it, "PK": db.upk(sub), "SK": WALLS_SK, "running": iso(now_ny()), "errors": []})
    import research
    research._invoke(sub, "wwoptwalls", {})
    return public(sub)


def walls_job(sub, params=None):
    """Fetch the call wall / put wall / flip of every day of the optimizer's period for its tickers (Mag 7 or the
    watchlist), ~8 minutes per run, chained until every day is stored; then the optimizer restarts on the full data."""
    import research
    import wallwatch
    st = state(sub)
    live = wallwatch.config(sub)
    try:
        f = wallwatch.fill_walls(sub, days=walls_days(st["days"]), budget_s=500, mp=False, sk=WALLS_SK, tickers=tickers(st, live))
    except Exception as e:
        db.put({**(db.get(db.upk(sub), WALLS_SK) or {}), "PK": db.upk(sub), "SK": WALLS_SK, "running": None,
                "errors": [str(e)[:160]], "at": iso(now_ny())})
        return {"error": str(e)[:160]}
    if f["left"] and f["fetched"]:                       # progress made: the next run
        db.update(db.upk(sub), WALLS_SK, {"running": iso(now_ny())})
        research._invoke(sub, "wwoptwalls", {})
    elif not f["left"]:                                  # every day stored: the optimizer starts over on the new data
        save_cfg(sub, {"reset": True})
    return f
