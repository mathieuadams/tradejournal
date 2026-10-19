"""Research pipeline: ideas (from the AI agent in research_agent.py, or your own) are tested on the same history with
the same criteria and graded. The pipeline is deterministic code: it never asks the AI anything. The agent is kept
apart: it only receives a read-only record of past ideas (1st-half results only) and returns proposals as data, which
this file cleans, gates and tests.

Per idea: the changes are applied to your current settings (day trading or swing lab), the backtest runs on the
loaded history, a cost per trade (in R) is taken off every trade, and the result is graded:
  A = every criterion passes and the 2nd half alone is significant (t >= the required t)
  B = every criterion passes
  C = positive on both halves but at least one criterion fails
  F = the rest
The required t rises with the number of ideas tested (Bonferroni: one idea in 100 passes t = 2 by luck), so a long
search doesn't "find" luck. Ideas that need a rule / indicator the app doesn't have are kept as "needs feature" with
what is missing; repeats of an idea already tested are skipped."""
import hashlib
import json
import math
import os
import time
import uuid
from statistics import NormalDist

import db
from util import iso, now_ny

SK = "RESEARCH"
IDEA = "RIDEA#"
CRITERIA = {"minTrades": 100, "minHalf": 40, "minT": 2.0, "minTestT": 1.0, "minPF": 1.2, "minWin": 0.0, "minAvgR": 0.05,
            "costR": 0.05, "bothHalves": True, "adjustT": True}
LIMITS = {"minTrades": (10, 5000), "minHalf": (0, 2500), "minT": (0, 10), "minTestT": (-5, 10), "minPF": (0, 10),
          "minWin": (0, 100), "minAvgR": (-1, 5), "costR": (0, 2)}
STRATS = ("day", "swing")
MAX_IDEAS = 8


# ---------------------------------------------------------------- criteria / storage
def criteria(sub):
    r = db.get(db.upk(sub), SK) or {}
    c = json.loads(r["criteria"]) if r.get("criteria") else {}
    return {**CRITERIA, **{k: v for k, v in c.items() if k in CRITERIA}}


def save_criteria(sub, body):
    cur = criteria(sub)
    for k, (lo, hi) in LIMITS.items():
        if k in (body or {}):
            try:
                cur[k] = max(lo, min(hi, float(body[k])))
            except (TypeError, ValueError):
                pass
    for k in ("bothHalves", "adjustT"):
        if k in (body or {}):
            cur[k] = bool(body[k])
    cur["minTrades"], cur["minHalf"] = int(cur["minTrades"]), int(cur["minHalf"])
    r = db.get(db.upk(sub), SK) or {"PK": db.upk(sub), "SK": SK}
    db.put({**r, "criteria": json.dumps(cur)})
    return cur


def ideas(sub, strategy=None, limit=300):
    rows = db.q_prefix(db.upk(sub), IDEA, desc=True, limit=limit)
    out = []
    for r in rows:
        try:
            x = json.loads(r["data"])
        except (KeyError, ValueError, TypeError):
            continue
        if strategy and x.get("strategy") != strategy:
            continue
        out.append(x)
    return out


def _put_idea(sub, x):
    db.put({"PK": db.upk(sub), "SK": f"{IDEA}{x['at']}#{x['id']}", "data": json.dumps(x, default=str, separators=(",", ":"))})


def delete_idea(sub, idea_id):
    for r in db.q_prefix(db.upk(sub), IDEA):
        if r["SK"].endswith("#" + str(idea_id)):
            db.delete(r["PK"], r["SK"])
            return True
    return False


def state(sub, strategy=None):
    from datetime import timedelta
    r = db.get(db.upk(sub), SK) or {}
    run = json.loads(r["run"]) if r.get("run") else {}
    if run.get("running") and run["running"] < iso(now_ny() - timedelta(minutes=12)):
        run = {**run, "running": None, "errors": (run.get("errors") or []) + ["The last run hit the 10-minute limit."]}
    xs = ideas(sub, strategy)
    return {"criteria": criteria(sub), "run": run, "ideas": xs, "auto": auto_cfg(sub), "autoToday": auto_today(sub), "tested": sum(1 for x in xs if x.get("status") == "tested"),
            "tReq": t_required(criteria(sub), sum(1 for x in xs if x.get("status") == "tested"))}


def _set_run(sub, run):
    r = db.get(db.upk(sub), SK) or {"PK": db.upk(sub), "SK": SK}
    db.put({**r, "run": json.dumps(run, default=str)})


# ---------------------------------------------------------------- settings of each strategy
def base_cfg(sub, strategy, lab=None):
    if strategy == "day":
        import daytrade
        b = daytrade.config(sub)
        return daytrade.clean_cfg(b, lab) if isinstance(lab, dict) else b
    import wallwatch
    b = dict(wallwatch.config(sub))
    if isinstance(lab, dict):
        b.update(clean_changes("swing", lab)[0])
    return b


KEEP = {"day": ("longs", "shorts"), "swing": ("retestSetup", "pwSetup", "flipSetup", "mergedSetup", "kcDaily", "scaleBE")}


def blank_cfg(sub, strategy):
    """The starting point of the agent's strategies: your tickers, every rule, filter and exit OFF (stop on, with
    the default size), no formula. The agent builds the whole strategy on top of it."""
    P = params_of(strategy)
    cur = base_cfg(sub, strategy)
    b = {**P, **{k: cur.get(k) for k in FIXED[strategy] if k in cur}}
    for k, v in P.items():
        if isinstance(v, bool) and k not in FIXED[strategy] and k not in KEEP[strategy]:
            b[k] = False
    for k in FX_KEYS[strategy]:
        b[k] = ""
    b["stopOn"] = True
    if strategy == "day":
        b["longs"] = b["shorts"] = True
    else:
        b["tf"], b["earnOn"] = "1h", False
    return b


def tunable(strategy, cfg):
    return {k: cfg.get(k) for k in params_of(strategy) if k not in FIXED[strategy]}


def params_of(strategy):
    if strategy == "day":
        import daytrade
        return daytrade.PARAMS
    import wallwatch
    return wallwatch.PARAMS


FIXED = {"day": ("enabled", "trade", "tickers"), "swing": ("enabled", "trade", "tickers", "main", "lists", "autoAdd", "pvSetup")}


FX_KEYS = {"day": ("fxLong", "fxShort", "fxExitLong", "fxExitShort"), "swing": ("fxEntry", "fxExit")}


def clean_changes(strategy, changes):
    """(valid changes, problems). Only settings the strategy has; values coerced to the type of the default;
    formula rules checked (formula.py)."""
    import formula
    P = params_of(strategy)
    ok, unknown = {}, []
    for k, v in (changes or {}).items():
        if k not in P or k in FIXED[strategy]:
            unknown.append(f"setting “{k}” does not exist")
            continue
        if k in FX_KEYS[strategy]:
            e = str(v or "").strip()
            try:
                if e:
                    formula.check(e, formula.UNAVAILABLE[strategy])
                ok[k] = e
            except formula.FormulaError as err:
                unknown.append(f"formula {k}: {err}")
            continue
        d = P[k]
        try:
            if isinstance(d, bool):
                ok[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
            elif isinstance(d, int):
                ok[k] = int(float(v))
            elif isinstance(d, float):
                ok[k] = float(v)
            elif isinstance(d, str):
                ok[k] = str(v)[:20]
            else:
                unknown.append(f"setting “{k}” can't be set")
        except (TypeError, ValueError):
            unknown.append(f"setting “{k}”: bad value {str(v)[:30]}")
    if strategy == "day" and ok:
        import daytrade
        full = daytrade.clean_cfg({**daytrade.PARAMS}, ok)      # day trading: the ranges of the settings form
        ok = {k: full[k] for k in ok}
    return ok, unknown


def fingerprint(strategy, effective):
    return hashlib.sha1(json.dumps([strategy, sorted(effective.items())], default=str).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- statistics / grade
def tstat(xs):
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return round(m / sd * math.sqrt(n), 2) if sd else None


def metrics(rows, mid, cost=0.0):
    """rows: [{"at", "R"}]. Each trade's R minus the cost."""
    rs = [(r["at"], float(r["R"]) - cost) for r in rows if r.get("R") is not None]
    rs.sort()

    def f(xs):
        n = len(xs)
        if not n:
            return {"n": 0, "avgR": None, "t": None, "win": None, "pf": None, "totR": 0}
        w, l = [x for x in xs if x > 0], [x for x in xs if x <= 0]
        return {"n": n, "avgR": round(sum(xs) / n, 3), "t": tstat(xs), "win": round(len(w) / n * 100, 1),
                "pf": round(sum(w) / -sum(l), 2) if l and sum(l) < 0 else (99.0 if w else None), "totR": round(sum(xs), 1)}
    al = f([x for _, x in rs])
    eq = peak = dd = 0.0
    for _, x in rs:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    al["maxDD"] = round(dd, 1)
    return {**al, "h1": f([x for a, x in rs if a[:10] < mid]), "h2": f([x for a, x in rs if a[:10] >= mid]), "cost": cost}


def t_required(c, n_tested):
    """The t a result needs: the minimum, raised for the number of ideas already tested (two-sided 5% shared
    across all of them) when adjustT is on."""
    if not c.get("adjustT") or n_tested <= 1:
        return float(c["minT"])
    return round(max(float(c["minT"]), NormalDist().inv_cdf(1 - 0.025 / max(1, n_tested))), 2)


def grade(m, c, n_tested):
    treq = t_required(c, n_tested)
    fails = []
    if m["n"] < c["minTrades"]:
        fails.append(f"{m['n']} trades < {int(c['minTrades'])}")
    if min(m["h1"]["n"], m["h2"]["n"]) < c["minHalf"]:
        fails.append(f"{min(m['h1']['n'], m['h2']['n'])} trades in a half < {int(c['minHalf'])}")
    if m["t"] is None or m["t"] < treq:
        fails.append(f"t {m['t']} < {treq}" + (f" (raised for {n_tested} ideas)" if treq > c["minT"] else ""))
    if m["h2"]["t"] is None or m["h2"]["t"] < c["minTestT"]:
        fails.append(f"2nd half t {m['h2']['t']} < {c['minTestT']}")
    if m["pf"] is None or m["pf"] < c["minPF"]:
        fails.append(f"PF {m['pf']} < {c['minPF']}")
    if c["minWin"] and (m["win"] or 0) < c["minWin"]:
        fails.append(f"win {m['win']}% < {c['minWin']:g}%")
    if m["avgR"] is None or m["avgR"] < c["minAvgR"]:
        fails.append(f"avg {m['avgR']}R < {c['minAvgR']}R")
    both = (m["h1"]["avgR"] or 0) > 0 and (m["h2"]["avgR"] or 0) > 0
    if c.get("bothHalves") and not both:
        fails.append("not positive on both halves")
    g = ("A" if not fails and (m["h2"]["t"] or 0) >= treq else "B" if not fails else "C" if both else "F")
    return g, fails, treq


# ---------------------------------------------------------------- testing
class DayBench:
    """1-minute day trading: candles of every day ticker loaded once (dtsearch), each idea run on them."""

    def __init__(self, sub, days, base):
        import dtsearch
        self.errors, self.feed = [], {}
        self.data = dtsearch.load(sub, days, base, self.errors, self.feed)
        ds = sorted({d for x in self.data for d in x["show"]})
        self.first, self.mid, self.last = (ds[0], ds[len(ds) // 2], ds[-1]) if ds else (None, None, None)

    def rows(self, P):
        import dtsearch
        return dtsearch.evaluate(self.data, P)


class SwingBench:
    """Swing (wall watch / lab): hourly candles of every watchlist ticker loaded once, each idea run on them."""

    def __init__(self, sub, days, base, budget_s=200):
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
        from datetime import timedelta
        import backtest
        t0 = time.time()
        self.errors, self.data = [], {}
        self.first = (now_ny() - timedelta(days=int(days * 1.45))).strftime("%Y-%m-%d")
        self.mid = (now_ny() - timedelta(days=int(days * 1.45) // 2)).strftime("%Y-%m-%d")
        self.last = now_ny().strftime("%Y-%m-%d")
        pool = ThreadPoolExecutor(max_workers=6)
        futs = [(s, pool.submit(backtest._ticker_data, sub, s, days, base, False)) for s in base.get("tickers") or []]
        for s, f in futs:
            try:
                d = f.result(timeout=max(1, budget_s - (time.time() - t0)))
                if d:
                    self.data[s] = d
            except FutureTimeout:
                self.errors.append(f"{s}: time limit")
            except Exception as e:
                self.errors.append(f"{s}: {str(e)[:60]}")
        pool.shutdown(wait=False, cancel_futures=True)
        self.base = base

    MAPS = ("vwapDays", "emaLen", "kcLen", "kcMult", "kcDaily", "kchLen", "kchMult", "inN1", "inN2", "inN3")

    def _maps(self, d, P):
        """The indicator maps rebuilt when the idea changes their lengths (no new download)."""
        import wallwatch
        if all(P.get(k) == self.base.get(k) for k in self.MAPS):
            return d
        bars = d["bars"]
        vw, _ = wallwatch.anchored_vwap(bars, int(P.get("vwapDays") or wallwatch.VWAP_DAYS))
        em = wallwatch._ema_series([b["c"] for b in bars], int(P.get("emaLen") or 20))
        kc = wallwatch.keltner(bars, int(P.get("kcLen") or 21), float(P.get("kcMult") or 3), bool(P.get("kcDaily", True)))
        kch = wallwatch.keltner(bars, int(P.get("kchLen") or 21), float(P.get("kchMult") or 3))
        inr = wallwatch.inertia_regime(bars, (int(P.get("inN1") or 70), int(P.get("inN2") or 40), int(P.get("inN3") or 20)))
        t = [b["t"][:16] for b in bars]
        return {**d, "vmap": dict(zip(t, vw)), "emap": dict(zip(t, em)),
                "extra": {**d["extra"], "kc": dict(zip(t, kc)), "kch": dict(zip(t, kch)), "inr": dict(zip(t, inr))}}

    def rows(self, P):
        import backtest
        out = []
        for s, d in self.data.items():
            try:
                out += backtest._run0(s, self._maps(d, P), {**P, "earnOn": False}, self.first)
            except Exception as e:
                self.errors.append(f"{s}: {str(e)[:60]}")
        return out


def test_idea(bench, base, x, c, n_tested):
    P = {**base, **(x.get("full") or x["changes"])}            # the idea's complete settings, on your tickers
    t0 = time.time()
    rows = bench.rows(P)
    m = metrics(rows, bench.mid, float(c["costR"]))
    g, fails, treq = grade(m, c, n_tested)
    by = {}
    for r in rows:
        by.setdefault(r.get("sym") or "?", []).append(r["R"] - float(c["costR"]))
    x.update(status="tested", metrics=m, grade=g, fails=fails, tReq=treq, secs=round(time.time() - t0, 1),
             period={"from": bench.first, "mid": bench.mid, "to": bench.last},
             bySym=sorted(({"sym": k, "n": len(v), "totR": round(sum(v), 1)} for k, v in by.items()), key=lambda r: -r["totR"])[:12])
    return x


# ---------------------------------------------------------------- the pipeline (no AI in here)
def new_idea(strategy, base, name, changes, by, days, hypothesis="", missing=(), learned="", focus=None, k=0, origin="blank"):
    """An idea record: only valid settings of the strategy, kept as the difference from its starting point (blank:
    every rule off; saved: your settings), plus the complete settings it is tested with."""
    ch, unknown = clean_changes(strategy, changes or {})
    eff = {k2: v for k2, v in ch.items() if base.get(k2) != v}
    return {"id": uuid.uuid4().hex[:12], "at": iso(now_ny()), "by": by, "strategy": strategy, "origin": origin,
            "full": tunable(strategy, {**base, **eff}),
            "name": str(name or "idea")[:80], "hypothesis": str(hypothesis or "")[:600], "learnedFrom": str(learned or "")[:300],
            "changes": eff, "focus": focus or None, "days": days, "fp": fingerprint(strategy, eff),
            "missing": [str(m)[:160] for m in (missing or [])][:6] + [str(u)[:200] for u in unknown][:6]}


def admit(x, seen):
    """The gate before testing: feature requests, empty and repeated ideas are recorded, not tested."""
    if x["missing"]:
        x["status"] = "needs feature"
    elif not x["changes"]:
        x["status"] = "no change"
    elif x["fp"] in seen:
        x["status"] = "duplicate"
    else:
        x["status"] = "queued"
    return x


def test_queued(sub, strategy, days, lab=None, budget_s=540, t0=None, run_=None):
    """Tests every queued idea of the strategy with the same criteria and history. Knows nothing about the AI."""
    t0 = t0 or time.time()
    run_ = run_ if run_ is not None else {"running": iso(now_ny()), "strategy": strategy, "at": iso(now_ny()), "errors": [], "testedNow": 0}
    q = [x for x in ideas(sub, strategy) if x.get("status") == "queued"]
    if not q:
        return run_
    c = criteria(sub)
    base = base_cfg(sub, strategy, lab)
    run_.update(step=f"loading {days} days of history")
    _set_run(sub, run_)
    bench = DayBench(sub, days, base) if strategy == "day" else SwingBench(sub, days, base, budget_s=min(240, budget_s / 2))
    run_["errors"] = (run_.get("errors") or []) + bench.errors[:5]
    run_["period"] = {"from": bench.first, "mid": bench.mid, "to": bench.last}
    if not bench.mid:
        run_["errors"].append("No history loaded.")
        return run_
    n_tested = sum(1 for x in ideas(sub, strategy) if x.get("status") == "tested")
    for x in sorted(q, key=lambda x: x["at"]):
        if time.time() - t0 > budget_s:
            run_["errors"].append("time limit: some ideas are still queued (run again)")
            break
        run_.update(step=f"testing “{x['name']}”")
        _set_run(sub, run_)
        try:
            test_idea(bench, base, x, c, n_tested + 1)
            n_tested += 1
            run_["testedNow"] = run_.get("testedNow", 0) + 1
        except Exception as e:
            x.update(status="error", fails=[str(e)[:160]])
        _put_idea(sub, x)
    return run_


def submit(sub, strategy, name, changes, lab=None, days=250, hypothesis=""):
    """An idea of the trader's own (no AI): into the same queue, the same test, the same grade."""
    base = base_cfg(sub, strategy, lab)
    seen = {x.get("fp") for x in ideas(sub, strategy) if x.get("status") == "tested"}
    x = admit(new_idea(strategy, base, name, changes, "you", days, hypothesis, origin="saved"), seen)
    _put_idea(sub, x)
    return x


# ---------------------------------------------------------------- one run: the agent proposes, the pipeline tests
def run(sub, params=None, budget_s=540):
    """1. the agent (research_agent.py) reads the record and proposes ideas: plain data, nothing else;
    2. the pipeline cleans and gates them, 3. tests and grades them. The agent can't run tests, change the criteria,
    the history or any setting; it only ever sees 1st-half results (the 2nd half stays out of its sight)."""
    import research_agent
    t0 = time.time()
    p = params or {}
    strategy = p.get("strategy") if p.get("strategy") in STRATS else "day"
    n = max(1, min(MAX_IDEAS, int(p.get("n") or 4)))
    rounds = max(1, min(3, int(p.get("rounds") or 1)))
    days = max(30, min(500, int(p.get("days") or 250)))
    c = criteria(sub)
    run_ = {"running": iso(now_ny()), "strategy": strategy, "at": iso(now_ny()), "step": "starting", "errors": [], "made": 0, "testedNow": 0}
    base = blank_cfg(sub, strategy)                    # the agent builds whole strategies from scratch
    if isinstance(p.get("catalog"), list):
        save_catalog(sub, strategy, p["catalog"])
    catalog = p.get("catalog") or load_catalog(sub, strategy)
    notes = []
    for rnd in range(rounds):
        if time.time() - t0 > budget_s * 0.5:
            break
        hist = ideas(sub, strategy)
        run_.update(step=f"round {rnd + 1}: the AI agent is reading {len(hist)} past ideas")
        _set_run(sub, run_)
        try:
            props, note = research_agent.propose(strategy, catalog, tunable(strategy, base),
                                                 research_agent.public_criteria(c), research_agent.record(hist), n, p.get("focus"),
                                                 sum(1 for x in hist if x.get("status") == "tested"))
        except Exception as e:
            run_["errors"].append(f"AI agent: {str(e)[:200]}")
            break
        if note:
            notes.append(note)
        seen = {x.get("fp") for x in hist if x.get("fp") and x.get("status") in ("tested", "queued")}
        for k, pr in enumerate(props):
            x = admit(new_idea(strategy, base, pr.get("name"), pr.get("changes"), "ai", days, pr.get("hypothesis"),
                               pr.get("missing"), pr.get("learnedFrom"), p.get("focus"), k), seen)
            seen.add(x["fp"])
            _put_idea(sub, x)
            run_["made"] += 1
        try:
            test_queued(sub, strategy, days, None, budget_s, t0, run_)
        except Exception as e:
            run_["errors"].append(f"testing: {str(e)[:160]}")
            break
    run_.update(running=None, step="done", notes=notes, secs=round(time.time() - t0), days=days, errors=run_["errors"][:8],
                auto=bool(p.get("auto")))
    _set_run(sub, run_)
    if p.get("auto"):                                  # autopilot: the next run starts right away, up to the daily limit
        try:
            _start(sub, auto_due(sub))
        except Exception as e:
            print("autopilot chain failed", e)
    return run_


# ---------------------------------------------------------------- autopilot: the agent runs on its own
AUTO = {"on": False, "perDay": 4, "n": 4, "days": 250}


def _item(sub):
    return db.get(db.upk(sub), SK) or {"PK": db.upk(sub), "SK": SK}


def save_catalog(sub, strategy, catalog):
    r = _item(sub)
    cats = json.loads(r["catalogs"]) if r.get("catalogs") else {}
    cats[strategy] = catalog[:400]
    db.put({**r, "catalogs": json.dumps(cats, default=str)})


def load_catalog(sub, strategy):
    r = _item(sub)
    return (json.loads(r["catalogs"]) if r.get("catalogs") else {}).get(strategy)


def auto_cfg(sub):
    r = _item(sub)
    a = json.loads(r["auto"]) if r.get("auto") else {}
    return {s: {**AUTO, **(a.get(s) or {})} for s in STRATS}


def save_auto(sub, strategy, body):
    if strategy not in STRATS:
        return auto_cfg(sub)
    a = auto_cfg(sub)
    cur = a[strategy]
    if "on" in body:
        cur["on"] = bool(body["on"])
    for k, lo, hi in (("perDay", 1, 24), ("n", 1, MAX_IDEAS), ("days", 30, 500)):
        if k in body:
            try:
                cur[k] = int(max(lo, min(hi, float(body[k]))))
            except (TypeError, ValueError):
                pass
    r = _item(sub)
    db.put({**r, "auto": json.dumps(a)})
    return a


def auto_due(sub):
    """[(strategy, params)] to start now: autopilot on, nothing running, fewer runs today than allowed (the day
    counts in New York time). Runs alternate between the strategies that are on."""
    r = _item(sub)
    run_ = json.loads(r["run"]) if r.get("run") else {}
    if run_.get("running") and run_["running"] >= iso(now_ny() - __import__("datetime").timedelta(minutes=12)):
        return []
    a, today = auto_cfg(sub), now_ny().strftime("%Y-%m-%d")
    cnt = json.loads(r["autoRuns"]) if r.get("autoRuns") else {}
    on = [s for s in STRATS if a[s]["on"] and cnt.get(f"{today}|{s}", 0) < a[s]["perDay"]]
    if not on:
        return []
    s = min(on, key=lambda x: cnt.get(f"{today}|{x}", 0))
    cnt = {k: v for k, v in cnt.items() if k.startswith(today)}
    cnt[f"{today}|{s}"] = cnt.get(f"{today}|{s}", 0) + 1
    db.put({**r, "autoRuns": json.dumps(cnt), "run": json.dumps({"running": iso(now_ny()), "strategy": s, "at": iso(now_ny()),
                                                                 "step": "autopilot: starting", "errors": [], "auto": True})})
    return [(s, {"strategy": s, "n": a[s]["n"], "days": a[s]["days"], "rounds": 1, "auto": True})]


def _start(sub, due):
    import boto3
    fn = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    for strategy, params in due:
        if fn:
            boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event",
                                          Payload=json.dumps({"sub": sub, "job": "research", "params": params}))
    return len(due)


def auto_today(sub):
    r = _item(sub)
    cnt = json.loads(r["autoRuns"]) if r.get("autoRuns") else {}
    today = now_ny().strftime("%Y-%m-%d")
    return {s: cnt.get(f"{today}|{s}", 0) for s in STRATS}


def auto_kick():
    """Hourly: start an autopilot run for every user due (each run starts the next one when it is done; this hourly
    kick restarts the chain after the daily reset or a failed run)."""
    started = 0
    for it in db.scan_sk(SK):
        if it.get("auto"):
            started += _start(it["PK"][5:], auto_due(it["PK"][5:]))
    return started


def test_job(sub, params=None):
    """Only the pipeline: test the queued ideas (your own ones)."""
    p = params or {}
    strategy = p.get("strategy") if p.get("strategy") in STRATS else "day"
    t0 = time.time()
    run_ = {"running": iso(now_ny()), "strategy": strategy, "at": iso(now_ny()), "step": "starting", "errors": [], "made": 0, "testedNow": 0}
    try:
        test_queued(sub, strategy, max(30, min(500, int(p.get("days") or 250))), p.get("lab"), 540, t0, run_)
    except Exception as e:
        run_["errors"].append(f"testing: {str(e)[:160]}")
    run_.update(running=None, step="done", secs=round(time.time() - t0))
    _set_run(sub, run_)
    return run_
