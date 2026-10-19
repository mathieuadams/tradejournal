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
            "costR": 0.05, "bothHalves": True, "adjustT": True, "autoRob": True, "robT": 2.0, "robDays": 500, "dayTickers": [], "familyCap": 15}
LIMITS = {"minTrades": (10, 5000), "minHalf": (0, 2500), "minT": (0, 10), "minTestT": (-5, 10), "minPF": (0, 10),
          "minWin": (0, 100), "minAvgR": (-1, 5), "costR": (0, 2), "robT": (0, 10), "robDays": (60, 500), "familyCap": (0, 200)}
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
    for k in ("bothHalves", "adjustT", "autoRob"):
        if k in (body or {}):
            cur[k] = bool(body[k])
    cur["minTrades"], cur["minHalf"], cur["robDays"] = int(cur["minTrades"]), int(cur["minHalf"]), int(cur["robDays"])
    cur["familyCap"] = int(cur["familyCap"])
    if "dayTickers" in (body or {}):
        tk = body["dayTickers"]
        tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
        cur["dayTickers"] = [str(t).strip().upper()[:10] for t in tk or [] if str(t).strip()][:30]
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
    return {"criteria": criteria(sub), "run": run, "ideas": xs, "blocks": block_map(xs, ("h1", "h2"))[:40],
            "families": sorted(([k, v] for k, v in families(xs).items()), key=lambda z: -z[1])[:20], "auto": auto_cfg(sub), "autoToday": auto_today(sub), "indicators": library(sub), "runlog": run_log(sub)[-15:], "mctx": _mctx_state(sub),
            "strategies": [{**x, "checks": [{k: v for k, v in c.items() if k not in ("daily", "dayList")} for c in x.get("checks") or []]}
                           for x in (strategies(sub, strategy) if strategy else strategies(sub))],
            "basket": basket(sub) if strategy in (None, "day") else None, "tested": sum(1 for x in xs if x.get("status") == "tested"),
            "tReq": t_required(criteria(sub), sum(1 for x in xs if x.get("status") == "tested"))}


def lite(sub, strategy=None):
    """Run status + how many ideas there are (the page reloads the list only when this changes)."""
    from datetime import timedelta
    r = db.get(db.upk(sub), SK) or {}
    run = json.loads(r["run"]) if r.get("run") else {}
    if run.get("running") and run["running"] < iso(now_ny() - timedelta(minutes=12)):
        run = {**run, "running": None}
    n = len(db.q_prefix(db.upk(sub), IDEA))
    return {"run": {k: run.get(k) for k in ("running", "step", "made", "testedNow", "at", "strategy", "errors")}, "nIdeas": n, "robAt": r.get("robAt"),
            "autoToday": auto_today(sub), "auto": auto_cfg(sub)}


def _mctx_state(sub):
    try:
        import marketctx
        return marketctx.job_state(sub)
    except Exception:
        return {}


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
        b["barMin"] = 5                                # the research runs on 5-minute candles (the agent may pick 1)
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
        import daytrade
        import marketctx
        for x in self.data:                            # options flow / gamma / breadth / VIXY / events for the formulas
            x["ctx"].update(daytrade.market_ctx(x["sym"], x["show"]))
        ds = sorted({d for x in self.data for d in x["show"]})
        try:
            cov = marketctx.coverage(ds)
            self.coverage = cov
            if ds and (cov["alp"] < 0.9 * len(ds) or cov["uw"] < 0.9 * len(ds)):
                self.errors.append(f"market context loaded for {min(cov['alp'], cov['uw'])} of {len(ds)} days (loading the rest in the background)")
                marketctx.kick(sub, len(ds))
        except Exception as e:
            print("market context coverage failed", e)
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
# ---------------------------------------------------------------- indicators created by the agent (formulas)
def library(sub):
    r = _item(sub)
    return json.loads(r["indicators"]) if r.get("indicators") else []


def lib_map(sub):
    return {x["name"]: x["formula"] for x in library(sub)}


def lib_add(sub, inds, strategy, by="ai", idea=None):
    """Checks and stores new indicators. -> (names added or already there, problems). A name already used with a
    different formula is refused (the old ideas keep their meaning)."""
    import formula
    lib = library(sub)
    have = {x["name"]: x for x in lib}
    ok, bad = [], []
    for ind in inds or []:
        nm, fx = str(ind.get("name") or "").strip(), str(ind.get("formula") or "").strip()
        if nm in formula.SERIES and nm not in formula.UNAVAILABLE[strategy]:
            continue                                   # it exists already as a built-in series: the formulas use that one
        if not formula.valid_name(nm):
            bad.append(f"indicator name “{nm}” not allowed (camelCase, not a built-in name)")
            continue
        if nm in have:
            if have[nm]["formula"].replace(" ", "") != fx.replace(" ", ""):
                bad.append(f"indicator “{nm}” already exists with another formula: use it or pick another name")
            else:
                ok.append(nm)
            continue
        try:
            cur = {**{k: v["formula"] for k, v in have.items()}, nm: fx}
            formula.check(formula.expand(fx, {k: v for k, v in cur.items() if k != nm}), formula.UNAVAILABLE[strategy])
        except formula.FormulaError as e:
            bad.append(f"indicator “{nm}”: {e}")
            continue
        have[nm] = {"name": nm, "formula": fx, "what": str(ind.get("what") or "")[:200], "by": by, "at": iso(now_ny()),
                    "strategy": strategy, "idea": idea}
        ok.append(nm)
    r = _item(sub)
    db.put({**r, "indicators": json.dumps(list(have.values()), default=str)})
    return ok, bad


def expand_changes(strategy, changes, custom):
    """(changes with formulas expanded, readable formulas, problems)."""
    import formula
    out, readable, bad = dict(changes or {}), {}, []
    for k in FX_KEYS[strategy]:
        v = str(out.get(k) or "").strip()
        if not v:
            continue
        try:
            e = formula.expand(v, custom)
        except formula.FormulaError as err:
            bad.append(f"formula {k}: {err}")
            out.pop(k, None)
            continue
        if e.replace(" ", "") != v.replace(" ", ""):
            readable[k] = v
        out[k] = e
    return out, readable, bad


def new_idea(strategy, base, name, changes, by, days, hypothesis="", missing=(), learned="", focus=None, k=0, origin="blank", custom=None):
    """An idea record: only valid settings of the strategy, kept as the difference from its starting point (blank:
    every rule off; saved: your settings), plus the complete settings it is tested with. Formulas using indicators
    the agent created are kept as written ("readable") and expanded into plain formulas for the test."""
    changes, readable, bad_fx = expand_changes(strategy, changes, custom or {})
    ch, unknown = clean_changes(strategy, changes or {})
    unknown = bad_fx + unknown
    eff = {k2: v for k2, v in ch.items() if base.get(k2) != v}
    return {"id": uuid.uuid4().hex[:12], "at": iso(now_ny()), "by": by, "strategy": strategy, "origin": origin, "readable": readable,
            "full": tunable(strategy, {**base, **eff}),
            "name": str(name or "idea")[:80], "hypothesis": str(hypothesis or "")[:600], "learnedFrom": str(learned or "")[:300],
            "changes": eff, "focus": focus or None, "days": days, "fp": fingerprint(strategy, eff),
            "missing": [str(m)[:160] for m in (missing or [])][:6] + [str(u)[:200] for u in unknown][:6]}


def admit(x, seen, fam=None, cap=0):
    """The gate before testing: feature requests, empty and repeated ideas are recorded, not tested; an idea of an
    entry family already tested `cap` times is recorded as "family exhausted" (no more one-knob tweaks of it)."""
    if x["missing"]:
        x["status"] = "needs feature"
    elif not x["changes"]:
        x["status"] = "no change"
    elif x["fp"] in seen:
        x["status"] = "duplicate"
    elif cap and fam is not None and fam.get(family_of(x), 0) >= cap:
        x["status"] = "family exhausted"
        x["fails"] = [f"its entry family ({family_of(x)}) has {fam[family_of(x)]} tested ideas already (cap {cap})"]
    else:
        x["status"] = "queued"
    return x


# ---------------------------------------------------------------- building blocks: what each piece adds, across all ideas
def _fx_names(expr):
    import ast
    try:
        tree = ast.parse(str(expr), mode="eval")
    except SyntaxError:
        return set()
    calls = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and id(n) not in calls and n.id not in ("and", "or", "not")}


def blocks_of(x):
    """The pieces an idea is made of: every rule / filter / exit switched on, every series or indicator its formulas
    use (by role: entry or exit), the side and the candle size."""
    f = dict(x.get("full") or x.get("changes") or {})
    f.update(x.get("readable") or {})
    out = set()
    for k, v in f.items():
        if v is True and k not in ("enabled", "trade", "longs", "shorts"):
            out.add(k)
        elif isinstance(v, str) and k.startswith("fx") and v.strip():
            role = "exit" if "Exit" in k else "entry"
            out |= {f"{n} ({role} formula)" for n in _fx_names(v)}
    if f.get("longs") is False:
        out.add("shorts only")
    if f.get("shorts") is False:
        out.add("longs only")
    if f.get("barMin"):
        out.add(f"{int(f['barMin'])}-min candles")
    return out


def family_of(x):
    """The entry family: the entry rules and the series of the entry formulas (exits and numbers don't count)."""
    import daytrade
    b = blocks_of(x)
    ent = sorted(k for k in b if k in daytrade.ENTRY or k in daytrade.FILTERS or k.endswith("(entry formula)"))
    return " + ".join(k.replace(" (entry formula)", "") for k in ent) or "no entry rule"


def families(xs):
    out = {}
    for x in xs:
        if x.get("status") == "tested":
            out[family_of(x)] = out.get(family_of(x), 0) + 1
    return out


def block_map(xs, halves=("h1",), min_n=5):
    """[{block, n, nOut, lift: {h1, h2}, t}] best first: the average R of the ideas WITH the block minus the ideas
    WITHOUT it (cost included), and the t of that difference on the 1st half. halves=("h1",) for the agent (it never
    sees the 2nd half); ("h1", "h2") for the trader, to see which pieces still help on the unseen half."""
    ts = [x for x in xs if x.get("status") == "tested" and (x.get("metrics") or {}).get("h1", {}).get("n")]
    bl = {x["id"]: blocks_of(x) for x in ts}
    allb = {b for v in bl.values() for b in v}

    def av(v):
        return sum(v) / len(v) if v else 0.0

    def var(v):
        m = av(v)
        return sum((y - m) ** 2 for y in v) / max(1, len(v) - 1)
    out = []
    for b in allb:
        w = [x for x in ts if b in bl[x["id"]]]
        o = [x for x in ts if b not in bl[x["id"]]]
        if len(w) < min_n or len(o) < min_n:
            continue
        row = {"block": b, "n": len(w), "nOut": len(o), "lift": {}}
        for h in halves:
            vw = [((x["metrics"].get(h) or {}).get("avgR") or 0) for x in w]
            vo = [((x["metrics"].get(h) or {}).get("avgR") or 0) for x in o]
            row["lift"][h] = round(av(vw) - av(vo), 3)
            if h == "h1":
                se = math.sqrt(var(vw) / len(vw) + var(vo) / len(vo)) or 1e-9
                row["t"] = round((av(vw) - av(vo)) / se, 2)
        out.append(row)
    out.sort(key=lambda r: -(r["t"] or 0))
    return out


def test_queued(sub, strategy, days, lab=None, budget_s=540, t0=None, run_=None):
    """Tests every queued idea of the strategy with the same criteria and history. Knows nothing about the AI."""
    t0 = t0 or time.time()
    run_ = run_ if run_ is not None else {"running": iso(now_ny()), "strategy": strategy, "at": iso(now_ny()), "errors": [], "testedNow": 0}
    q = [x for x in ideas(sub, strategy) if x.get("status") == "queued"]
    if not q:
        return run_
    c = criteria(sub)
    base = base_cfg(sub, strategy, lab)
    if strategy == "day" and c.get("dayTickers"):
        base["tickers"] = list(c["dayTickers"])        # research on a wider list than the live day tickers
    run_.update(step=f"loading {days} days of history" + (f" on {len(base.get('tickers') or [])} tickers" if strategy == "day" else ""))
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
            if strategy == "day" and rob_due(x, c):
                x["robust"] = {"status": "queued", "at": iso(now_ny())}
        except Exception as e:
            x.update(status="error", fails=[str(e)[:160]])
        _put_idea(sub, x)
    if strategy == "day" and any((x.get("robust") or {}).get("status") == "queued" for x in q):
        rob_kick(sub)
    return run_


# ---------------------------------------------------------------- automatic robustness check of the good ones
def rob_due(x, c):
    """Grade C or better with a full-period t above robT -> the robustness check on robDays days, once."""
    m = x.get("metrics") or {}
    return bool(c.get("autoRob")) and x.get("status") == "tested" and x.get("grade") in ("A", "B", "C") \
        and (m.get("t") or 0) > float(c.get("robT") or 2) and not x.get("robust")


def rob_kick(sub, force=False):
    """Starts the robustness job unless one is running (one idea at a time; each run starts the next)."""
    from datetime import timedelta
    r = db.get(db.upk(sub), SK) or {"PK": db.upk(sub), "SK": SK}
    if not force and r.get("robRunning") and r["robRunning"] > iso(now_ny() - timedelta(minutes=12)):
        return False
    db.put({**r, "robRunning": iso(now_ny())})
    _invoke(sub, "rsrob", {})
    return True


def _rob_summary(out):
    def st(x):
        return {k: x.get(k) for k in ("n", "avgR", "t", "win", "pf", "totR")}
    return {"status": "done", "at": out.get("at"), "days": out.get("days"), "daysFound": out.get("daysFound"), "verdict": out.get("verdict"),
            "share": out.get("share"), "positive": out.get("positive"), "variants": out.get("variants"), "median": out.get("median"),
            "base": st(out.get("base") or {}), "h1": st((out.get("rows") or [{}])[0].get("h1") or {}) if out.get("rows") else {},
            "h2": st((out.get("rows") or [{}])[0].get("h2") or {}) if out.get("rows") else {},
            "worst": sorted(({"label": x["label"], "avgR": x["all"].get("avgR"), "n": x["all"].get("n")} for x in (out.get("rows") or [])[1:] if x["all"].get("n")),
                            key=lambda y: y["avgR"] or 0)[:5],
            "extra": [{"label": x["label"], **st(x["all"])} for x in out.get("extra") or []], "errors": (out.get("errors") or [])[:3]}


def rob_job(sub, params=None):
    """The next idea waiting for its robustness check: dtrobust on robDays days with the idea's full settings
    (kept on the idea, the Backtest & tools result is not touched), then the next one."""
    import dtrobust
    c = criteria(sub)
    p = params or {}
    from datetime import timedelta
    stale = iso(now_ny() - timedelta(minutes=12))

    def waiting(x):
        rb = x.get("robust") or {}
        return rb.get("status") == "queued" or (rb.get("status") == "running" and (rb.get("at") or "") < stale)
    xs = [x for x in ideas(sub, "day") if waiting(x) and (not p.get("id") or x["id"] == p["id"])]
    r = db.get(db.upk(sub), SK) or {"PK": db.upk(sub), "SK": SK}
    if not xs:
        db.put({**r, "robRunning": None})
        return {"done": None}
    x = sorted(xs, key=lambda y: -((y.get("metrics") or {}).get("t") or 0))[0]
    x["robust"] = {"status": "running", "at": iso(now_ny())}
    _put_idea(sub, x)
    try:
        cfg = {**base_cfg(sub, "day"), **(x.get("full") or x.get("changes") or {})}
        if c.get("dayTickers"):
            cfg["tickers"] = list(c["dayTickers"])
        out = dtrobust.run(sub, {"cfg": cfg, "days": int(c.get("robDays") or 500), "cost": float(c["costR"])}, save=False)
        x["robust"] = _rob_summary(out)
    except Exception as e:
        x["robust"] = {"status": "error", "at": iso(now_ny()), "errors": [str(e)[:200]]}
    _put_idea(sub, x)
    r = db.get(db.upk(sub), SK) or {"PK": db.upk(sub), "SK": SK}
    db.put({**r, "robAt": iso(now_ny()), "robRunning": None})
    if any((y.get("robust") or {}).get("status") == "queued" for y in ideas(sub, "day")):
        rob_kick(sub)
    return {"done": x["id"], "verdict": x["robust"].get("verdict")}


def rob_backfill(sub):
    """Ideas tested before the automatic check existed (or after the criteria changed): queue the ones that qualify."""
    c = criteria(sub)
    n = 0
    for x in ideas(sub, "day"):
        if rob_due(x, c):
            x["robust"] = {"status": "queued", "at": iso(now_ny())}
            _put_idea(sub, x)
            n += 1
    if n:
        rob_kick(sub)
    return n


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
        rebuilt = build_missing(sub, strategy, catalog, base, hist, days, run_)       # ideas waiting for an indicator
        if rebuilt:
            hist = ideas(sub, strategy)
        run_.update(step=f"round {rnd + 1}: the AI agent is reading {len(hist)} past ideas")
        _set_run(sub, run_)
        try:
            bm = block_map(hist, ("h1",))
            fam = families(hist)
            cap = int(c.get("familyCap") or 0)
            props, note = research_agent.propose(strategy, catalog, tunable(strategy, base),
                                                 research_agent.public_criteria(c), research_agent.record(hist), n, p.get("focus"),
                                                 sum(1 for x in hist if x.get("status") == "tested"), library(sub), keepers(sub, strategy),
                                                 {"best": bm[:15], "worst": bm[-10:][::-1] if len(bm) > 15 else [],
                                                  "exhaustedFamilies": sorted(([k, v] for k, v in fam.items() if cap and v >= cap), key=lambda z: -z[1])[:10],
                                                  "familyCounts": sorted(([k, v] for k, v in fam.items()), key=lambda z: -z[1])[:15],
                                                  "basketNeeds": _basket_needs(sub) if strategy == "day" else []})
        except Exception as e:
            run_["errors"].append(f"AI agent: {str(e)[:200]}")
            break
        if note:
            notes.append(note)
        seen = {x.get("fp") for x in hist if x.get("fp") and x.get("status") in ("tested", "queued")}
        seen |= {x.get("fp") for x in strategies(sub, strategy) if x.get("fp")}
        for k, pr in enumerate(props):
            made, bad = lib_add(sub, pr.get("newIndicators"), strategy, "ai", pr.get("name"))
            x = admit(new_idea(strategy, base, pr.get("name"), pr.get("changes"), "ai", days, pr.get("hypothesis"),
                               list(pr.get("missing") or []) + bad, pr.get("learnedFrom"), p.get("focus"), k, custom=lib_map(sub)), seen,
                      fam if strategy == "day" else None, cap)
            x["indicators"] = made
            seen.add(x["fp"])
            _put_idea(sub, x)
            run_["made"] += 1
        build_missing(sub, strategy, catalog, base, ideas(sub, strategy), days, run_)   # fix what can't run yet, right away
        try:
            test_queued(sub, strategy, days, None, budget_s, t0, run_)
        except Exception as e:
            run_["errors"].append(f"testing: {str(e)[:160]}")
            break
    run_.update(running=None, step="done", notes=notes, secs=round(time.time() - t0), days=days, errors=run_["errors"][:8],
                auto=bool(p.get("auto")))
    _set_run(sub, run_)
    log_run(sub, {k: run_.get(k) for k in ("at", "strategy", "made", "testedNow", "secs", "errors", "auto", "notes")} | {"focus": p.get("focus") or None})
    last3 = [x for x in run_log(sub) if x.get("strategy") == strategy][-3:]
    if p.get("auto") and len(last3) == 3 and all(x.get("auto") and not x.get("made") for x in last3):
        save_auto(sub, strategy, {"on": False})        # 3 autopilot runs in a row without a single idea: stop, don't burn calls
        run_["errors"] = (run_.get("errors") or []) + ["Autopilot paused: 3 runs in a row without an idea (see Last runs); turn it on again once fixed."]
        _set_run(sub, run_)
        return run_
    if p.get("auto"):                                  # autopilot: the next run starts right away, up to the daily limit
        try:
            _start(sub, auto_due(sub))
        except Exception as e:
            print("autopilot chain failed", e)
    return run_


# ---------------------------------------------------------------- saved strategies (kept, re-checked every day)
STRAT = "RSTRAT#"


def strategies(sub, strategy=None):
    out = []
    for r in db.q_prefix(db.upk(sub), STRAT):
        try:
            x = json.loads(r["data"])
        except (KeyError, ValueError):
            continue
        if not strategy or x.get("strategy") == strategy:
            out.append(x)
    return sorted(out, key=lambda x: x.get("at") or "")


def _put_strat(sub, x):
    db.put({"PK": db.upk(sub), "SK": f"{STRAT}{x['id']}", "data": json.dumps(x, default=str, separators=(",", ":"))})


def side_of(full):
    """"long" (long only), "short" (short only) or "both"."""
    lo, sh = (full or {}).get("longs", True) is not False, (full or {}).get("shorts", True) is not False
    return "long" if lo and not sh else "short" if sh and not lo else "both"


def save_strategy(sub, strategy, name, cfg=None, idea_id=None):
    """Keep a strategy: an idea of the record (its complete settings) or settings given. Re-checked every day."""
    if strategy not in STRATS:
        raise ValueError("unknown strategy")
    src, base = None, None
    if idea_id:
        src = next((x for x in ideas(sub, strategy) if x.get("id") == idea_id), None)
        if not src:
            raise ValueError("idea not found")
        full = dict(src.get("full") or {})
    else:
        full = tunable(strategy, {**base_cfg(sub, strategy), **(clean_changes(strategy, cfg or {})[0])})
    x = {"id": uuid.uuid4().hex[:10], "at": iso(now_ny()), "strategy": strategy, "name": str(name or (src or {}).get("name") or "strategy")[:80],
         "full": full, "idea": idea_id, "hypothesis": (src or {}).get("hypothesis"), "readable": (src or {}).get("readable"),
         "baseline": {k: (src or {}).get("metrics", {}).get(k) for k in ("n", "avgR", "t", "pf", "win")} if src else None,
         "checks": [], "fp": fingerprint(strategy, {k: v for k, v in full.items() if blank_cfg(sub, strategy).get(k) != v})}
    x["side"] = side_of(full)
    _put_strat(sub, x)
    return x


def delete_strategy(sub, sid):
    db.delete(db.upk(sub), f"{STRAT}{sid}")
    return True


def keepers(sub, strategy):
    """The saved strategies as the agent may see them: name, idea, settings that are on (no results)."""
    out = []
    for x in strategies(sub, strategy):
        on = {k: v for k, v in (x.get("full") or {}).items() if v not in (False, "", None, 0)}
        on.update(x.get("readable") or {})
        out.append({"name": x["name"], "side": side_of(x.get("full")), "hypothesis": x.get("hypothesis"), "settings": on})
    return out


def check_strategies(sub, days=250, cost=None):
    """Every saved day strategy on the last `days` days (one load for all): full period, last 60 and last 20 trading
    days. Kept as a history (the last 90 checks) so a strategy that weakens shows it."""
    import daytrade
    ss = strategies(sub, "day")
    if not ss:
        return []
    c = criteria(sub)
    cost = float(c["costR"] if cost is None else cost)
    tickers = daytrade.config(sub)["tickers"]
    vs = [{**daytrade.PARAMS, **x["full"], "tickers": tickers} for x in ss]
    rr, per, all_days, feed, errors = daytrade.run_variants(sub, days, vs, 500, tickers)
    ds = sorted(all_days)
    l60, l20 = (ds[-60] if len(ds) >= 60 else ds[0] if ds else ""), (ds[-20] if len(ds) >= 20 else ds[0] if ds else "")
    out = []
    for i, x in enumerate(ss):
        rows = [r for r in rr.get(i, []) if r.get("R") is not None]
        f = lambda since: metrics([r for r in rows if r["at"][:10] >= since], "9999", cost) if since else {"n": 0}
        m = metrics(rows, ds[len(ds) // 2] if ds else "", cost)
        chk = {"at": iso(now_ny()), "days": len(ds), "cost": cost, "all": {k: m.get(k) for k in ("n", "avgR", "t", "pf", "win", "maxDD")},
               "last60": {k: f(l60).get(k) for k in ("n", "avgR", "totR", "win")}, "last20": {k: f(l20).get(k) for k in ("n", "avgR", "totR", "win")},
               "errors": errors[:3]}
        for dn in ("long", "short"):                   # each side on its own
            sr = [r for r in rows if (r.get("dir") or "long") == dn]
            if sr:
                ms = metrics(sr, "9999", cost)
                chk[dn] = {k: ms.get(k) for k in ("n", "avgR", "t", "win")}
        daily = {}
        for r in rows:                                 # R per day (after the cost): the basket, the correlations
            daily[r["at"][:10]] = daily.get(r["at"][:10], 0.0) + float(r["R"]) - cost
        chk["daily"] = {d: round(v, 3) for d, v in daily.items()}
        chk["dayList"] = ds[-260:]
        avg, a60 = chk["all"].get("avgR"), chk["last60"].get("avgR")
        chk["status"] = ("healthy" if (avg or 0) > 0 and (a60 is None or a60 > 0) else
                         "weakening" if (avg or 0) > 0 else "failing")
        for old in x.get("checks") or []:              # only the latest check keeps the day-by-day series (item size)
            old.pop("daily", None)
            old.pop("dayList", None)
        x["checks"] = (x.get("checks") or [])[-89:] + [chk]
        x["side"] = side_of(x.get("full"))
        _put_strat(sub, x)
        out.append({"id": x["id"], "status": chk["status"]})
    r = _item(sub)
    db.put({**r, "stratCheck": iso(now_ny())})
    try:
        basket_switch(sub)
    except Exception as e:
        print("basket switch failed", e)
    return out


# ---------------------------------------------------------------- the basket: every saved strategy, the best one in use
BASKET = {"auto": False, "side": "any", "margin": 0.25}


def basket_cfg(sub):
    r = _item(sub)
    c = json.loads(r["basket"]) if r.get("basket") else {}
    return {**BASKET, **{k: v for k, v in c.items() if k in BASKET or k in ("active", "log")}}


def save_basket(sub, body):
    c = basket_cfg(sub)
    b = body or {}
    if "auto" in b:
        c["auto"] = bool(b["auto"])
    if b.get("side") in ("any", "long", "short", "both"):
        c["side"] = b["side"]
    if "margin" in b:
        try:
            c["margin"] = max(0.0, min(2.0, float(b["margin"])))
        except (TypeError, ValueError):
            pass
    r = _item(sub)
    db.put({**r, "basket": json.dumps(c, default=str)})
    return c


def _daily_vec(chk, days):
    d = chk.get("daily") or {}
    return [float(d.get(x, 0.0)) for x in days]


def _corr(a, b):
    n = len(a)
    if n < 10:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    return round(sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb), 2)


def _series_stats(v):
    n = len(v)
    if not n:
        return {"days": 0}
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / max(1, n - 1))
    eq = peak = dd = 0.0
    for x in v:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"days": n, "totR": round(sum(v), 1), "avgDay": round(m, 3), "t": round(m / sd * math.sqrt(n), 2) if sd > 0 else None,
            "maxDD": round(dd, 1), "greenPct": round(sum(1 for x in v if x > 0) / max(1, sum(1 for x in v if x != 0)) * 100)}


def basket(sub):
    """The saved day strategies as one basket: side (long only / short only / both), status, a score on the last 60
    days (t of the daily R), the best one overall and per side, how much their days move together, and the basket
    (all healthy ones, equal weight) against the best one alone."""
    ss = [x for x in strategies(sub, "day") if x.get("checks")]
    cfg = basket_cfg(sub)
    rows = []
    for x in ss:
        c = x["checks"][-1]
        days = c.get("dayList") or []
        v60 = _daily_vec(c, days[-60:])
        st60 = _series_stats(v60)
        rows.append({"id": x["id"], "name": x["name"], "side": x.get("side") or side_of(x.get("full")), "status": c.get("status"),
                     "score": st60.get("t"), "last60": {**(c.get("last60") or {}), **{k: st60.get(k) for k in ("totR", "t", "maxDD")}},
                     "all": c.get("all"), "long": c.get("long"), "short": c.get("short"), "at": c.get("at"),
                     "_days": days, "_c": c})
    ok = [r for r in rows if r["status"] == "healthy" and (r["score"] or 0) > 0]
    best = max(ok, key=lambda r: r["score"] or 0) if ok else None
    by_side = {}
    for sd in ("long", "short", "both"):
        g = [r for r in ok if r["side"] == sd]
        by_side[sd] = max(g, key=lambda r: r["score"] or 0)["id"] if g else None
    common = sorted(set().union(*[set(r["_days"]) for r in rows])) if rows else []
    common = common[-250:]
    vec = {r["id"]: _daily_vec(r["_c"], common) for r in rows}
    corr = [[(1.0 if a["id"] == b["id"] else _corr(vec[a["id"]], vec[b["id"]])) for b in rows] for a in rows]
    combo = None
    if ok and common:
        cv = [sum(vec[r["id"]][i] for r in ok) / len(ok) for i in range(len(common))]
        combo = {"members": len(ok), **_series_stats(cv)}
    single = _series_stats(vec[best["id"]]) if best and common else None
    for r in rows:
        r.pop("_days")
        r.pop("_c")
    return {"rows": rows, "best": best["id"] if best else None, "bySide": by_side, "corr": corr, "combined": combo,
            "bestAlone": single, "cfg": cfg, "missingSides": [sd for sd in ("long", "short", "both") if not by_side[sd]]}


def _basket_needs(sub):
    try:
        return basket(sub)["missingSides"]
    except Exception:
        return []


def basket_switch(sub, force=False):
    """Auto-switch (when on): after the daily check, the best healthy strategy (of the chosen side) goes into the live
    day-trading settings, when it beats the one in use by the margin or the one in use stopped being healthy."""
    import daytrade
    cfg = basket_cfg(sub)
    if not cfg.get("auto") and not force:
        return None
    b = basket(sub)
    ok = [r for r in b["rows"] if r["status"] == "healthy" and (r["score"] or 0) > 0 and (cfg["side"] == "any" or r["side"] == cfg["side"])]
    if not ok:
        return None
    top = max(ok, key=lambda r: r["score"] or 0)
    cur = next((r for r in b["rows"] if r["id"] == cfg.get("active")), None)
    if cur and cur["id"] == top["id"]:
        return None
    if cur and cur["status"] == "healthy" and (cur["score"] or 0) > 0 and (top["score"] or 0) < (cur["score"] or 0) * (1 + float(cfg["margin"])) and not force:
        return None
    x = next(s for s in strategies(sub, "day") if s["id"] == top["id"])
    keep = ("enabled", "trade", "tickers")
    daytrade.save_config(sub, {k: v for k, v in (x.get("full") or {}).items() if k not in keep})
    cfg["active"] = top["id"]
    cfg["log"] = (cfg.get("log") or [])[-19:] + [{"at": iso(now_ny()), "to": top["name"], "from": cur["name"] if cur else None,
                                                   "score": top["score"]}]
    r = _item(sub)
    db.put({**r, "basket": json.dumps(cfg, default=str)})
    return top["id"]


def use_strategy(sub, sid):
    """Put one saved strategy into the live day-trading settings by hand (it becomes the one in use)."""
    import daytrade
    x = next((s for s in strategies(sub, "day") if s["id"] == sid), None)
    if not x:
        raise ValueError("strategy not found")
    daytrade.save_config(sub, {k: v for k, v in (x.get("full") or {}).items() if k not in ("enabled", "trade", "tickers")})
    cfg = basket_cfg(sub)
    cfg["active"] = sid
    cfg["log"] = (cfg.get("log") or [])[-19:] + [{"at": iso(now_ny()), "to": x["name"], "by": "you"}]
    r = _item(sub)
    db.put({**r, "basket": json.dumps(cfg, default=str)})
    return cfg


def strat_job(sub, params=None):
    try:
        return {"checked": check_strategies(sub, int((params or {}).get("days") or 250))}
    except Exception as e:
        print("strategy check failed", e)
        return {"error": str(e)[:200]}


# ---------------------------------------------------------------- autopilot: the agent runs on its own
AUTO = {"on": False, "perDay": 4, "n": 4, "days": 250, "focus": ""}


def log_run(sub, entry):
    """The last 40 runs, for the 'Last runs' list (what each run did, its errors)."""
    r = _item(sub)
    lg = json.loads(r["runlog"]) if r.get("runlog") else []
    lg = (lg + [entry])[-40:]
    db.put({**r, "runlog": json.dumps(lg, default=str)})


def run_log(sub):
    r = _item(sub)
    return json.loads(r["runlog"]) if r.get("runlog") else []


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
    if "focus" in body:
        cur["focus"] = str(body["focus"] or "")[:600]
    for k, lo, hi in (("perDay", 0, 24), ("n", 1, MAX_IDEAS), ("days", 30, 500)):     # perDay 0 = continuous
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
    on = [s for s in STRATS if a[s]["on"] and (not a[s]["perDay"] or cnt.get(f"{today}|{s}", 0) < a[s]["perDay"])]
    if not on:
        return []
    s = min(on, key=lambda x: cnt.get(f"{today}|{x}", 0))
    cnt = {k: v for k, v in cnt.items() if k.startswith(today)}
    cnt[f"{today}|{s}"] = cnt.get(f"{today}|{s}", 0) + 1
    db.put({**r, "autoRuns": json.dumps(cnt), "run": json.dumps({"running": iso(now_ny()), "strategy": s, "at": iso(now_ny()),
                                                                 "step": "autopilot: starting", "errors": [], "auto": True})})
    return [(s, {"strategy": s, "n": a[s]["n"], "days": a[s]["days"], "rounds": 1, "auto": True, "focus": a[s].get("focus") or ""})]


def _start(sub, due):
    import boto3
    # the research job runs in the bot function: from the API it is BOT_FUNCTION, from the bot function itself
    fn = os.environ.get("BOT_FUNCTION") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
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


def _invoke(sub, job, params):
    import boto3
    fn = os.environ.get("BOT_FUNCTION") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if fn:
        boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event", Payload=json.dumps({"sub": sub, "job": job, "params": params}))


def auto_kick():
    """Hourly: start an autopilot run for every user due (each run starts the next one when it is done; this hourly
    kick restarts the chain after the daily reset or a failed run)."""
    started = 0
    for it in db.scan_sk(SK):
        if it.get("auto"):
            sub = it["PK"][5:]
            started += _start(sub, auto_due(sub))
            try:                                       # the saved strategies, once a day after the close
                if now_ny().strftime("%H:%M") >= "17:00" and (it.get("stratCheck") or "")[:10] < now_ny().strftime("%Y-%m-%d") \
                        and strategies(sub, "day"):
                    db.put({**_item(sub), "stratCheck": iso(now_ny())})
                    _invoke(sub, "rsstrat", {})
            except Exception as e:
                print("strategy check kick failed", e)
            try:                                       # robustness checks waiting (a run that died restarts the queue)
                rob_backfill(sub) or (any((x.get("robust") or {}).get("status") in ("queued", "running") for x in ideas(sub, "day"))
                                      and rob_kick(sub))
            except Exception as e:
                print("robustness kick failed", e)
            try:                                       # the market context of the last session, once it is over
                import marketctx
                st = marketctx.job_state(sub)
                if not st.get("running") and (st.get("left") or st.get("at", "")[:10] < now_ny().strftime("%Y-%m-%d")) \
                        and now_ny().strftime("%H:%M") >= "16:30":
                    marketctx.kick(sub)
            except Exception as e:
                print("market context kick failed", e)
    return started


BUILD_TRIES = 3


def available_named(strategy, texts):
    """[(series, description)] of the built-in series that a "missing data" text names (the agent claiming data is
    missing when the app has it)."""
    import re
    import formula
    out = []
    blob = " ".join(str(t) for t in texts or [])
    for k, d in formula.SERIES.items():
        if k in formula.UNAVAILABLE[strategy] or len(k) < 4:
            continue
        if re.search(rf"\b{re.escape(k)}\b", blob):
            out.append((k, d))
    return out


def build_missing(sub, strategy, catalog, base, hist, days, run_, limit=4):
    """Ideas that need something the app doesn't have: the agent creates the missing indicators as formulas, the
    pipeline checks them, rebuilds the idea with them and queues it again (one attempt per idea). What truly needs
    other data stays a feature request ("needs data")."""
    import research_agent
    for x in hist:                                     # wrongly parked as "needs data" for a series the app has: reopened
        fake = available_named(strategy, x.get("missing")) if x.get("status") == "needs data" else []
        if fake:
            x.update(status="needs feature", buildTries=0, buildTried=None,
                     missing=[f"“{n}” IS available as a built-in series ({d}): use it directly in the formula, don't create it or call it missing"
                              for n, d in fake])
            _put_idea(sub, x)
    todo = [x for x in hist if x.get("status") == "needs feature" and int(x.get("buildTries") or (1 if x.get("buildTried") else 0)) < BUILD_TRIES][:limit]
    if not todo:
        return 0
    run_.update(step=f"the AI agent is creating indicators for {len(todo)} waiting idea{'s' if len(todo) > 1 else ''}")
    _set_run(sub, run_)
    items = [{"id": x["id"], "name": x["name"], "hypothesis": x.get("hypothesis"), "changes": {**(x.get("changes") or {}), **(x.get("readable") or {})},
              "problems": x.get("missing"), "attempt": int(x.get("buildTries") or 0) + 1,
              "note": "fix every problem listed: they are the exact errors of the formula checker"} for x in todo]
    try:
        res = {r["id"]: r for r in research_agent.build(strategy, catalog, tunable(strategy, base), library(sub), items)}
    except Exception as e:
        run_["errors"].append(f"AI agent (indicators): {str(e)[:160]}")
        return 0
    seen = {x.get("fp") for x in hist if x.get("fp") and x.get("status") in ("tested", "queued")}
    done = 0
    for x in todo:
        r = res.get(x["id"]) or {}
        x["buildTried"] = iso(now_ny())
        x["buildTries"] = int(x.get("buildTries") or 0) + 1
        if not r or not (r.get("changes") or r.get("impossible")):
            _put_idea(sub, x)                          # the agent had nothing for it: stays waiting (no new attempt)
            continue
        fake = available_named(strategy, r.get("impossible"))
        if fake:                                       # "missing" data that the app has: back to the agent, said plainly
            x.update(status="needs feature", missing=[f"“{n}” IS available as a built-in series ({d}): use it directly in the formula, "
                                                      f"don't create it or call it missing" for n, d in fake])
            _put_idea(sub, x)
            continue
        if r.get("impossible") and not r.get("changes"):
            x.update(status="needs data", missing=r["impossible"])
            _put_idea(sub, x)
            continue
        made, bad = lib_add(sub, r.get("newIndicators"), strategy, "ai", x["name"])
        y = new_idea(strategy, base, x["name"], r.get("changes") or x.get("changes"), x.get("by") or "ai", days, x.get("hypothesis"),
                     list(r.get("impossible") or []) + bad, x.get("learnedFrom"), x.get("focus"), custom=lib_map(sub))
        y = admit(y, seen)
        if y["status"] == "needs feature":
            y["status"] = "needs data" if r.get("impossible") else "needs feature"
        was = x.get("wasMissing") or x.get("missing")
        x.update({k: y[k] for k in ("changes", "full", "readable", "missing", "fp", "status")}, indicators=made, wasMissing=was)
        x["rebuilt"] = x["status"] not in ("needs feature", "needs data")
        seen.add(x["fp"])
        _put_idea(sub, x)
        done += x["status"] == "queued"
    return done


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
