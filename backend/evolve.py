"""Evolution: a genetic search over formula strategies (day trading), next to the AI agent. No AI in here.

A genome = a side (long only / short only / both), 1-3 conditions ("atoms") from a fixed grammar over the series the
app has (hourly structure, levels, relative strength, options flow, gamma, breadth, volatility, squeeze, CVD, time,
gap...), a stop and a target. Its formula is the AND of its atoms (mirrored for shorts).

Honest by construction:
  * fitness uses the 1st half of the research period ONLY (the 2nd half is never seen here; the holdout days are
    outside the research period altogether);
  * walk-forward: the 1st half is cut in 3 windows; fitness = t x (share of windows positive), so one lucky stretch
    doesn't win;
  * complexity costs (-0.25 per extra atom), innovation pays (+0.15 per piece used in fewer than 5 tested ideas);
  * pieces RETIRED by the pipeline (not paying on the 2nd half) are taken out of the grammar, and every atom set
    whose promoted idea did not pay on the 2nd half is taboo: never evaluated again;
  * only the best genome of a run is promoted into the ideas list, graded by the same pipeline, and its t needed
    counts every genome evaluated so far (the price of a big search).
"""
import json
import random
import time

import db
from util import iso, now_ny

SK = "EVOLVE"
POP = 24
ELITE = 4
MAX_ATOMS = 3
RUNS_PER_DAY = 40
NOVEL_USES = 5

# (long, short, {param: choices}): {p} in the strings is replaced by the chosen value (short: {q} = mirrored value)
GRAMMAR = [
    ("close > vwap", "close < vwap", {}),
    ("hTrend > 0", "hTrend < 0", {}),
    ("low <= hBrokeUp and close > hBrokeUp", "high >= hBrokeDn and close < hBrokeDn", {}),
    ("close > hLvlBelow and close - hLvlBelow < atr(14)", "close < hLvlAbove and hLvlAbove - close < atr(14)", {}),
    ("hLvlAbove - close > {p} * atr(14)", "close - hLvlBelow > {p} * atr(14)", {"p": [1, 2, 3]}),
    ("close > prevHigh", "close < prevLow", {}),
    ("close > pmHigh", "close < pmLow", {}),
    ("close > orHigh({p})", "close < orLow({p})", {"p": [3, 6, 12]}),
    ("rs > {p}", "rs < -{p}", {"p": [0, 0.2, 0.5]}),
    ("megaBreadth > {p}", "megaBreadth < {q}", {"p": [0.5, 0.6, 0.7]}),
    ("breadth > {p}", "breadth < {q}", {"p": [0.5, 0.6, 0.7]}),
    ("gex < 0", "gex < 0", {}),
    ("gex > 0", "gex > 0", {}),
    ("sweepCall > 0", "sweepPut > 0", {}),
    ("netPremDay > 0", "netPremDay < 0", {}),
    ("netPrem > 0", "netPrem < 0", {}),
    ("sqFire(20) == 1 and sqMom(20) > 0", "sqFire(20) == 1 and sqMom(20) < 0", {}),
    ("rising(sqMom(20), 1)", "falling(sqMom(20), 1)", {}),
    ("rising(cvdSum({p}), 1)", "falling(cvdSum({p}), 1)", {"p": [10, 20]}),
    ("pmRange > {p}", "pmRange > {p}", {"p": [0.5, 1, 1.5]}),
    ("vixyChg < {p}", "vixyChg < {p}", {"p": [-2, 0, 2]}),
    ("vixyChg > {p}", "vixyChg > {p}", {"p": [0, 2]}),
    ("tod >= {p} and tod < {p} + 90", "tod >= {p} and tod < {p} + 90", {"p": [5, 60, 150, 240, 300]}),
    ("gap > {p}", "gap < -{p}", {"p": [0.3, 1]}),
    ("gap < -{p}", "gap > {p}", {"p": [0.3, 1]}),
    ("gapFilled == 1", "gapFilled == 1", {}),
    ("gapFilled == 0", "gapFilled == 0", {}),
    ("rvol(20) > {p}", "rvol(20) > {p}", {"p": [1.5, 2]}),
    ("rsi(14) < {p}", "rsi(14) > {q}", {"p": [30, 40]}),
    ("close > flip", "close < flip", {}),
    ("eventDay == 0", "eventDay == 0", {}),
]
STOPS = [1.0, 1.3, 1.6, 2.0]
TARGETS = [1.5, 2.0, 2.5, 3.0]
SIDES = ["long", "short", "both"]


def _mirror(name, v):
    if name == "p" and isinstance(v, float) and 0 < v < 1:
        return round(1 - v, 2)
    return 100 - v if isinstance(v, (int, float)) and v >= 20 else v


def atom_expr(a, short=False):
    i, v = a
    lo, sh, ps = GRAMMAR[i]
    s = sh if short else lo
    if ps:
        s = s.replace("{q}", str(_mirror("p", v))).replace("{p}", str(v))
    return s


def formulas(g):
    lo = " and ".join(f"({atom_expr(a)})" for a in g["atoms"])
    sh = " and ".join(f"({atom_expr(a, True)})" for a in g["atoms"])
    return (lo if g["side"] in ("long", "both") else ""), (sh if g["side"] in ("short", "both") else "")


def key(g):
    return f"{g['side']}|{','.join(f'{i}:{v}' for i, v in sorted(g['atoms'], key=str))}|{g['stop']}|{g['target']}"


def atom_set(g):
    return "|".join(sorted(f"{i}:{v}" for i, v in g["atoms"])) + "|" + g["side"]


def changes_of(g):
    fl, fs = formulas(g)
    return {"fxLong": fl, "fxShort": fs, "longs": g["side"] != "short", "shorts": g["side"] != "long",
            "stopOn": True, "stopAtr": g["stop"], "targetOn": True, "targetR": g["target"], "barMin": 5}


def name_of(g):
    fl, fs = formulas(g)
    side = {"long": "Long-only", "short": "Short-only", "both": "Both-side"}[g["side"]]
    body = " + ".join(atom_expr(a) for a in g["atoms"])
    return f"Evolved {side}: {body}"[:80]


# ---------------------------------------------------------------- the grammar the search may use now
def allowed_atoms(xs, c):
    """Atom indexes whose pieces are not retired."""
    import research
    gone = set(research.retired(xs, c))
    ok = []
    for i in range(len(GRAMMAR)):
        lo, sh, ps = GRAMMAR[i]
        v = ps["p"][0] if ps else None
        g = {"side": "both", "atoms": [(i, v)], "stop": 1.3, "target": 2.0}
        pieces = research.blocks_of({"full": changes_of(g)})
        if not (pieces & gone):
            ok.append(i)
    return ok


def novelty(g, uses):
    import research
    pieces = research.blocks_of({"full": changes_of(g)})
    return sum(1 for p in pieces if uses.get(p, 0) < NOVEL_USES)


def piece_uses(xs):
    import research
    u = {}
    for x in xs:
        if x.get("status") == "tested":
            for p in research.blocks_of(x):
                u[p] = u.get(p, 0) + 1
    return u


# ---------------------------------------------------------------- genomes
def rand_atom(allowed, rng):
    i = rng.choice(allowed)
    ps = GRAMMAR[i][2]
    return (i, rng.choice(ps["p"]) if ps else None)


def rand_genome(allowed, rng):
    n = rng.choice([1, 2, 2, 3])
    atoms = []
    while len(atoms) < n:
        a = rand_atom(allowed, rng)
        if a[0] not in [b[0] for b in atoms]:
            atoms.append(a)
    return {"side": rng.choice(SIDES), "atoms": atoms, "stop": rng.choice(STOPS), "target": rng.choice(TARGETS)}


def mutate(g, allowed, rng):
    g = {**g, "atoms": list(g["atoms"])}
    op = rng.choice(["swap", "param", "add", "drop", "side", "exit"])
    if op == "swap" and g["atoms"]:
        k = rng.randrange(len(g["atoms"]))
        a = rand_atom(allowed, rng)
        if a[0] not in [b[0] for b in g["atoms"]]:
            g["atoms"][k] = a
    elif op == "param" and g["atoms"]:
        k = rng.randrange(len(g["atoms"]))
        i, _ = g["atoms"][k]
        ps = GRAMMAR[i][2]
        if ps:
            g["atoms"][k] = (i, rng.choice(ps["p"]))
    elif op == "add" and len(g["atoms"]) < MAX_ATOMS:
        a = rand_atom(allowed, rng)
        if a[0] not in [b[0] for b in g["atoms"]]:
            g["atoms"].append(a)
    elif op == "drop" and len(g["atoms"]) > 1:
        g["atoms"].pop(rng.randrange(len(g["atoms"])))
    elif op == "side":
        g["side"] = rng.choice([s for s in SIDES if s != g["side"]])
    else:
        g["stop"], g["target"] = rng.choice(STOPS), rng.choice(TARGETS)
    g["atoms"] = [a for a in g["atoms"] if a[0] in allowed] or [rand_atom(allowed, rng)]
    return g


def cross(a, b, rng):
    pool = {x[0]: x for x in a["atoms"] + b["atoms"]}
    atoms = rng.sample(list(pool.values()), min(len(pool), rng.choice([1, 2, 3])))
    return {"side": rng.choice([a["side"], b["side"]]), "atoms": atoms, "stop": rng.choice([a["stop"], b["stop"]]),
            "target": rng.choice([a["target"], b["target"]])}


# ---------------------------------------------------------------- fitness (1st half only)
def fitness(rows, mid, cost, g, uses, first):
    """-> {fit, n, avgR, t, win, posWin}. Only the trades before `mid` (the 1st half) exist here."""
    import research
    rs = sorted((r["at"], float(r["R"]) - cost) for r in rows if r.get("R") is not None and r["at"][:10] < mid)
    n = len(rs)
    out = {"n": n}
    if n < 60:                                         # too rare to judge: about 2 trades a week at least
        out["fit"] = round(-5 + n / 60, 3)
        return out
    xs = [x for _, x in rs]
    t = research.tstat(xs) or 0
    k = n // 3
    wins = [xs[:k], xs[k:2 * k], xs[2 * k:]]
    pos = sum(1 for w in wins if w and sum(w) > 0)
    nov = novelty(g, uses)
    fit = t * pos / 3 - 0.25 * (len(g["atoms"]) - 1) + 0.15 * nov
    out.update(fit=round(fit, 3), avgR=round(sum(xs) / n, 3), t=t, win=round(sum(1 for x in xs if x > 0) / n * 100, 1),
               posWin=pos, novel=nov)
    return out


# ---------------------------------------------------------------- state
def state(sub):
    r = db.get(db.upk(sub), SK) or {}
    st = json.loads(r["data"]) if r.get("data") else {}
    st.setdefault("on", False)
    st.setdefault("days", 250)
    st.setdefault("pop", [])
    st.setdefault("gen", 0)
    st.setdefault("evals", 0)
    st.setdefault("seen", {})
    st.setdefault("taboo", [])
    st.setdefault("promoted", [])
    st.setdefault("log", [])
    st.setdefault("runs", {})
    return st


def _save(sub, st):
    if len(st["seen"]) > 3000:                         # keep the evaluated set bounded (best ones kept)
        st["seen"] = dict(sorted(st["seen"].items(), key=lambda kv: -(kv[1].get("fit") or -99))[:2000])
    db.put({"PK": db.upk(sub), "SK": SK, "data": json.dumps(st, default=str, separators=(",", ":"))})


def public(sub):
    from datetime import timedelta
    st = state(sub)
    if st.get("running") and st["running"] < iso(now_ny() - timedelta(minutes=12)):
        st["running"] = None
    top = sorted(st["pop"], key=lambda p: -((p.get("res") or {}).get("fit") or -99))[:8]
    return {"on": st["on"], "days": st["days"], "gen": st["gen"], "evals": st["evals"], "running": st.get("running"),
            "last": st.get("last"), "taboo": len(st["taboo"]), "promoted": st["promoted"][-12:], "log": st["log"][-10:],
            "top": [{"name": name_of(p["g"]), "side": p["g"]["side"], "stop": p["g"]["stop"], "target": p["g"]["target"],
                     "formulas": dict(zip(("long", "short"), formulas(p["g"]))), **(p.get("res") or {})} for p in top]}


def save_cfg(sub, body):
    st = state(sub)
    b = body or {}
    if "on" in b:
        st["on"] = bool(b["on"])
    if b.get("days"):
        st["days"] = max(60, min(500, int(b["days"])))
    if b.get("reset"):
        st.update(pop=[], gen=0)
    _save(sub, st)
    return public(sub)


# ---------------------------------------------------------------- one run (as many generations as the time allows)
def run(sub, budget_s=470, seed=None):
    import research
    t0 = time.time()
    rng = random.Random(seed)
    st = state(sub)
    st["running"] = iso(now_ny())
    _save(sub, st)
    c = research.criteria(sub)
    hist = research.ideas(sub, "day")
    uses = piece_uses(hist)
    allowed = allowed_atoms(hist, c)
    if not allowed:
        st.update(running=None, last={"at": iso(now_ny()), "error": "every atom of the grammar is retired"})
        _save(sub, st)
        return st
    cost = float(c["costR"])
    base = research.base_cfg(sub, "day")
    if c.get("dayTickers"):
        base["tickers"] = list(c["dayTickers"])
    blank = research.blank_cfg(sub, "day")
    if c.get("dayTickers"):
        blank["tickers"] = list(c["dayTickers"])
    bench = research.DayBench(sub, int(st["days"]), base, holdout=int(c.get("holdoutDays") or 0))
    if not bench.mid:
        st.update(running=None, last={"at": iso(now_ny()), "error": "no history loaded", "errors": bench.errors[:3]})
        _save(sub, st)
        return st
    taboo = set(st["taboo"])
    evals0 = st["evals"]

    def evaluate(g):
        k = key(g)
        if k in st["seen"]:
            return st["seen"][k]
        if atom_set(g) in taboo:
            res = {"fit": -9, "n": 0, "taboo": True}
        else:
            P = {**base, **blank, **changes_of(g)}
            try:
                res = fitness(bench.rows(P), bench.mid, cost, g, uses, bench.first)
            except Exception as e:
                res = {"fit": -9, "n": 0, "error": str(e)[:80]}
            st["evals"] += 1
        st["seen"][k] = res
        return res

    pop = [p for p in st["pop"] if all(a[0] in allowed for a in p["g"]["atoms"]) and atom_set(p["g"]) not in taboo]
    pop = [{"g": {**p["g"], "atoms": [tuple(a) for a in p["g"]["atoms"]]}, "res": p.get("res")} for p in pop]
    while len(pop) < POP:
        pop.append({"g": rand_genome(allowed, rng)})
    gens = 0
    while time.time() - t0 < budget_s * 0.85:
        for p in pop:
            if time.time() - t0 > budget_s * 0.85:
                break
            if not p.get("res"):
                p["res"] = evaluate(p["g"])
        pop.sort(key=lambda p: -((p.get("res") or {}).get("fit") or -99))
        if time.time() - t0 > budget_s * 0.85:
            break
        nxt = pop[:ELITE]
        scored = [p for p in pop if p.get("res")]

        def pick():
            a, b = rng.sample(scored, 2) if len(scored) > 1 else (scored[0], scored[0])
            return a if (a["res"].get("fit") or -99) >= (b["res"].get("fit") or -99) else b
        while len(nxt) < POP:
            r_ = rng.random()
            if r_ < 0.15:
                g = rand_genome(allowed, rng)          # fresh blood: innovation
            elif r_ < 0.45:
                g = cross(pick()["g"], pick()["g"], rng)
            else:
                g = mutate(pick()["g"], allowed, rng)
            if any(key(g) == key(q["g"]) for q in nxt):
                continue
            nxt.append({"g": g, "res": st["seen"].get(key(g))})
        pop = nxt
        gens += 1
        st["gen"] += 1
    pop.sort(key=lambda p: -((p.get("res") or {}).get("fit") or -99))
    st["pop"] = [{"g": p["g"], "res": p.get("res")} for p in pop]
    promoted = promote(sub, st, pop, bench, base, c, hist)
    today = now_ny().strftime("%Y-%m-%d")
    st["runs"] = {today: st["runs"].get(today, 0) + 1}
    best = (pop[0].get("res") or {}) if pop else {}
    st["log"] = (st["log"] + [{"at": iso(now_ny()), "gens": gens, "evals": st["evals"] - evals0, "best": best.get("fit"),
                               "promoted": promoted, "secs": round(time.time() - t0)}])[-30:]
    st.update(running=None, last={"at": iso(now_ny()), "gens": gens, "evals": st["evals"] - evals0, "errors": bench.errors[:3]})
    _save(sub, st)
    return st


def promote(sub, st, pop, bench, base, c, hist):
    """The best genome not promoted before (fitness >= 1.5) goes into the ideas list, tested by the pipeline with
    every evaluation counted in its t needed; one that doesn't pay on the 2nd half makes its atom set taboo."""
    import research
    done = {p.get("key") for p in st["promoted"]}
    n_tested = sum(1 for x in hist if x.get("status") == "tested")
    out = []
    for p in pop:
        r = p.get("res") or {}
        if (r.get("fit") or -99) < 1.5 or key(p["g"]) in done:
            continue
        g = p["g"]
        x = research.new_idea("day", research.blank_cfg(sub, "day"), name_of(g), changes_of(g), "evolve", int(st["days"]),
                              f"Found by the genetic search: 1st-half t {r.get('t')}, {r.get('posWin')}/3 windows positive, "
                              f"{r.get('novel', 0)} rarely used pieces.", origin="blank")
        x["searchCount"] = st["evals"]
        x = research.admit(x, set(), banned=research.retired(hist, c))
        if x["status"] == "queued":
            research.test_idea(bench, base, x, c, n_tested + 1 + st["evals"])
            if research.rob_due(x, c):
                x["robust"] = {"status": "queued", "at": iso(now_ny())}
            if ((x.get("metrics") or {}).get("h2") or {}).get("avgR") is None or x["metrics"]["h2"]["avgR"] <= 0:
                st["taboo"] = (st["taboo"] + [atom_set(g)])[-500:]      # didn't pay on the 2nd half: never again
        research._put_idea(sub, x)
        if (x.get("robust") or {}).get("status") == "queued":
            research.rob_kick(sub)
        st["promoted"].append({"key": key(g), "id": x["id"], "name": x["name"], "grade": x.get("grade"), "at": iso(now_ny()),
                               "h2": ((x.get("metrics") or {}).get("h2") or {}).get("avgR")})
        out.append(x["id"])
        break                                          # one a run: the rest keeps evolving
    return out


def job(sub, params=None):
    import research
    try:
        st = run(sub)
    except Exception as e:
        st = state(sub)
        st.update(running=None, last={"at": iso(now_ny()), "error": str(e)[:200]})
        _save(sub, st)
        return {"error": str(e)[:200]}
    if st["on"] and st["runs"].get(now_ny().strftime("%Y-%m-%d"), 0) < RUNS_PER_DAY and not (params or {}).get("once"):
        research._invoke(sub, "evolve", {})            # the next run right away (at most RUNS_PER_DAY a day)
    return {"gen": st["gen"], "evals": st["evals"]}


def kick():
    """Hourly: restart the chain of every user with the evolution on (after the daily reset or a failed run)."""
    import research
    from datetime import timedelta
    n = 0
    for it in db.scan_sk(SK):
        try:
            st = json.loads(it["data"])
        except (KeyError, ValueError):
            continue
        if not st.get("on"):
            continue
        if st.get("running") and st["running"] > iso(now_ny() - timedelta(minutes=12)):
            continue
        if (st.get("runs") or {}).get(now_ny().strftime("%Y-%m-%d"), 0) >= RUNS_PER_DAY:
            continue
        research._invoke(it["PK"][5:], "evolve", {})
        n += 1
    return n
