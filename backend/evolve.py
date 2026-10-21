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
RUNS_PER_DAY = 10 ** 9                                 # no daily limit: runs back to back while it is on
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
    # the chart structure of the 30 sessions before the day
    ("dTrend > 0", "dTrend < 0", {}),
    ("close > supZone and close - supZone < {p} * dAtr", "close < resZone and resZone - close < {p} * dAtr", {"p": [0.1, 0.25]}),
    ("inZone == 1 and zoneTouches >= {p}", "inZone == 1 and zoneTouches >= {p}", {"p": [3, 5]}),
    ("rangePos < {p}", "rangePos > {q}", {"p": [0.2, 0.35]}),
    ("rangePos > {q}", "rangePos < {p}", {"p": [0.2, 0.35]}),
    ("close > vaHigh", "close < vaLow", {}),
    ("close < poc30 and close > vaLow", "close > poc30 and close < vaHigh", {}),
    ("resZone - close > {p} * dAtr", "close - supZone > {p} * dAtr", {"p": [0.5, 1]}),
]
STOPS = [1.0, 1.3, 1.6, 2.0]
TARGETS = [1.5, 2.0, 2.5, 3.0, "trail1.5", "trail2.5"]   # a target in R, or a trailing stop (ATR) instead
EXIT_CHOICES = list(TARGETS)                           # the exits the search may use now (retired ones taken out)
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


def taboo_key(universe):
    """One never-again list per research universe (a combination that failed on QQQ may work on stocks in play)."""
    return "tabooInplay" if universe == "inplay" else "taboo"


def _taboo_sets(taboo):
    out = []
    for t in taboo:
        parts = t.split("|")
        out.append((frozenset(parts[:-1]), parts[-1]))
    return out


def is_taboo(g, sets):
    """Taboo when the genome contains every condition of a failed set (same side): a failed core with filters added
    on top is the same failed idea."""
    mine = {f"{i}:{v}" for i, v in g["atoms"]}
    return any(side == g["side"] and core <= mine for core, side in sets)


def _jac(a, b):
    return len(a & b) / len(a | b) if a | b else 1.0


def rank(pop):
    """Fitness sharing: sorted by fitness, each genome loses 0.75 for every better one it nearly copies (same
    conditions, ignoring their numbers, the side and the exits: overlap >= 2/3), so the population stays varied."""
    pop.sort(key=lambda p: -((p.get("res") or {}).get("fit") or -99))
    kept = []
    for p in pop:
        f = (p.get("res") or {}).get("fit")
        ids = frozenset(i for i, _ in p["g"]["atoms"])
        dup = sum(1 for q in kept if _jac(ids, q) >= 0.66)
        p["adj"] = (f if f is not None else -99) - 0.75 * dup
        kept.append(ids)
    pop.sort(key=lambda p: -p["adj"])
    return pop


def changes_of(g):
    fl, fs = formulas(g)
    out = {"fxLong": fl, "fxShort": fs, "longs": g["side"] != "short", "shorts": g["side"] != "long",
           "stopOn": True, "stopAtr": g["stop"], "barMin": 5}
    t = g.get("target")
    if isinstance(t, str) and t.startswith("trail"):
        out.update(trailOn=True, trailAtr=float(t[5:]), targetOn=False)
    elif t:
        out.update(targetOn=True, targetR=float(t))
    else:
        out.update(targetOn=False)                     # stop only, out at the close
    return out


def allowed_exits(gone):
    ex = [t for t in TARGETS if (isinstance(t, str) and "trailOn" not in gone) or (not isinstance(t, str) and "targetOn" not in gone)]
    return ex or [None]


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
        g = {"side": "both", "atoms": [(i, v)], "stop": 1.3, "target": None}
        fl, fs = formulas(g)
        pieces = research.blocks_of({"full": {"fxLong": fl, "fxShort": fs}})      # the condition's own pieces only
        if not (pieces & gone):
            ok.append(i)
    return ok


def novelty(g, uses):
    import research
    fl, fs = formulas(g)                               # the conditions' own pieces (not the side, stop or exit)
    pieces = research.blocks_of({"full": {"fxLong": fl, "fxShort": fs}})
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


RUN_SIDE = None                                        # the side this run focuses on (long / short / both, in turn)
SIDE_CYCLE = ["long", "short", "both"]
JOLT_EVERY = 3                                         # every 3rd run: the conditions of the top 5 are banned for the run


def _side(rng):
    return RUN_SIDE if RUN_SIDE and rng.random() < 0.7 else rng.choice(SIDES)


def rand_genome(allowed, rng):
    n = rng.choice([1, 2, 2, 3])
    atoms = []
    while len(atoms) < min(n, len(allowed)):
        a = rand_atom(allowed, rng)
        if a[0] not in [b[0] for b in atoms]:
            atoms.append(a)
    return {"side": _side(rng), "atoms": atoms, "stop": rng.choice(STOPS), "target": rng.choice(EXIT_CHOICES)}


def _fam_of_key(k):
    parts = k.split("|")
    if len(parts) < 2:
        return None
    ids = sorted({int(a.split(":")[0]) for a in parts[1].split(",") if a})
    return f"{parts[0]}|{','.join(str(i) for i in ids)}"


def family_key(g):
    """The idea behind a genome: its side and its conditions, ignoring their numbers, the stop and the exit."""
    return f"{g['side']}|{','.join(str(i) for i in sorted({a[0] for a in g['atoms']}))}"


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
        g["stop"], g["target"] = rng.choice(STOPS), rng.choice(EXIT_CHOICES)
    g["atoms"] = [a for a in g["atoms"] if a[0] in allowed] or [rand_atom(allowed, rng)]
    if g.get("target") not in EXIT_CHOICES:
        g["target"] = rng.choice(EXIT_CHOICES)
    return g


def cross(a, b, rng):
    pool = {x[0]: x for x in a["atoms"] + b["atoms"]}
    atoms = rng.sample(list(pool.values()), min(len(pool), rng.choice([1, 2, 3])))
    return {"side": rng.choice([a["side"], b["side"]]), "atoms": atoms, "stop": rng.choice([a["stop"], b["stop"]]),
            "target": rng.choice([t for t in (a.get("target"), b.get("target")) if t in EXIT_CHOICES] or EXIT_CHOICES)}


# ---------------------------------------------------------------- fitness (1st half only)
def screen_result(rows, cost):
    """The quick screen (a third of the 1st-half days): None = promising, test it fully; else the result it gets
    without the full test (too few trades for the full test's 60, or losing on the sample)."""
    n = len(rows)
    if n * 3 < 48:                                     # the full 1st half would have under ~60 trades
        return {"fit": round(-5 + n * 3 / 60, 3), "n": n * 3, "screened": True}
    xs = [float(r["R"]) - cost for r in rows if r.get("R") is not None]
    avg = sum(xs) / len(xs) if xs else 0
    if len(xs) >= 20 and avg <= 0:                     # losing on the sample: not worth the full test
        return {"fit": round(-1 + max(-1.0, avg), 3), "n": n * 3, "avgR": round(avg, 3), "screened": True}
    return None


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
    byd = {}
    for a, x in rs:
        byd[a[:10]] = byd.get(a[:10], 0.0) + x
    if len(byd) < 20:                                  # trades bunched on a few days: not enough separate bets
        out.update(fit=round(-3 + len(byd) / 20, 3), days=len(byd))
        return out
    tt = research.tstat(xs) or 0
    td = research.tstat(list(byd.values())) or 0
    t = min(tt, td)                                    # trades of one day win / lose together: the day is the bet
    ds = sorted(byd)                                   # 3 CALENDAR windows of the 1st half (not 3 groups of trading days:
    lo, hi = (first or ds[0])[:10], mid               # trades bunched in one month would fill all three)
    from datetime import date
    d0, d1 = date.fromisoformat(lo), date.fromisoformat(hi)
    sp = max(1, (d1 - d0).days)
    edges = [date.fromordinal(d0.toordinal() + sp * i // 3).isoformat() for i in range(4)]
    wins = [[byd[d] for d in ds if edges[i] <= d < edges[i + 1] or (i == 2 and d >= edges[3])] for i in range(3)]
    pos = sum(1 for w in wins if w and sum(w) > 0)
    mo = {}
    for d in ds:
        mo[d[:7]] = mo.get(d[:7], 0) + 1
    share = max(mo.values()) / len(ds)                 # the busiest month's share of the trading days
    nov = novelty(g, uses)
    fit = t * pos / 3 - 0.25 * (len(g["atoms"]) - 1) + (min(0.3, 0.1 * nov) if t > 0 else 0)   # a small bonus, only on a real edge
    if share > 0.35:                                   # one market period: not an edge (penalty grows with the share)
        fit -= 4 * (share - 0.35)
    out.update(fit=round(fit, 3), avgR=round(sum(xs) / n, 3), t=t, tTrade=tt, tDay=td, days=len(byd), win=round(sum(1 for x in xs if x > 0) / n * 100, 1),
               posWin=pos, novel=nov, monthShare=round(share, 2))
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
    st.setdefault("tickers", [])
    return st


USER_KEYS = ("on", "days", "tickers")                 # set from the page: a run in progress never overwrites them


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
    if len(st["seen"]) > 3000:                         # keep the evaluated set bounded (best ones kept)
        st["seen"] = dict(sorted(st["seen"].items(), key=lambda kv: -(kv[1].get("fit") or -99))[:2000])
    data = json.dumps(st, default=str, separators=(",", ":"))
    while len(data.encode()) > MAX_ITEM and st["seen"]:   # DynamoDB: 400 KB an item; the cached scores give way first
        st["seen"] = dict(sorted(st["seen"].items(), key=lambda kv: -(kv[1].get("fit") or -99))[:int(len(st["seen"]) * 0.7)])
        data = json.dumps(st, default=str, separators=(",", ":"))
    if len(data.encode()) > MAX_ITEM:                  # then the oldest never-again sets and the log
        for k in [k for k in st if k == "taboo" or str(k).startswith("taboo")]:
            if isinstance(st[k], list):
                st[k] = st[k][-200:]
        st["log"] = st.get("log", [])[-10:]
        data = json.dumps(st, default=str, separators=(",", ":"))
    db.put({"PK": db.upk(sub), "SK": SK, "data": data})


MAX_ITEM = 360_000


def _universe(sub):
    try:
        import research
        return research.universe_of(research.criteria(sub))
    except Exception:
        return "tickers"


def public(sub):
    from datetime import timedelta
    st = state(sub)
    timed_out = bool(st.get("running") and st["running"] < iso(now_ny() - timedelta(minutes=12)))
    if timed_out:
        st["running"] = None
    top = _best_of((st.get("hof") or []) + st["pop"])[:8]   # the best found so far (kept through jolts), then the population
    return {"on": st["on"], "days": st["days"], "gen": st["gen"], "evals": st["evals"], "running": st.get("running"),
            "step": st.get("step"), "timedOut": timed_out, "tickers": st.get("tickers") or [], "avgEval": st.get("avgEval"),
            "last": st.get("last"), "taboo": len(st.get(taboo_key(_universe(sub))) or []), "runsToday": (st.get("runs") or {}).get(now_ny().strftime("%Y-%m-%d"), 0), "runsMax": None, "universe": _universe(sub), "promoted": st["promoted"][-12:], "log": st["log"][-10:],
            "top": [{"name": name_of(p["g"]), "side": p["g"]["side"], "stop": p["g"]["stop"], "target": p["g"].get("target"),
                     "formulas": dict(zip(("long", "short"), formulas(p["g"]))), **(p.get("res") or {})} for p in top]}


def _best_of(items, n=None):
    """Ranked, one per strategy (the hall of fame and the population together)."""
    seen, out = set(), []
    for p in rank([{"g": {**p["g"], "atoms": [tuple(a) for a in p["g"]["atoms"]]}, "res": p.get("res")} for p in items]):
        k = key(p["g"])
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out[:n] if n else out


SEEN_KEYS = ("fit", "n", "avgR", "t", "days", "win", "posWin", "novel", "monthShare", "taboo", "screened")
HOF = 12                                               # hall of fame: the best strategies ever found (a jolt keeps them)


def save_cfg(sub, body):
    st = state(sub)
    b = body or {}
    if "on" in b:
        st["on"] = bool(b["on"])
    if b.get("days"):
        st["days"] = max(60, min(500, int(b["days"])))
    if b.get("reset"):                                 # a full restart: new population, scores, sent list; the never-again
        st.update(pop=[], gen=0, evals=0, seen={}, promoted=[], log=[], hof=[])   # list (failed on the 2nd half) is kept
    if "tickers" in b:
        tk = b["tickers"]
        tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
        st["tickers"] = [str(t).strip().upper()[:10] for t in tk or [] if str(t).strip()][:20]
    _save(sub, st, user=True)
    return public(sub)


# ---------------------------------------------------------------- one run (as many generations as the time allows)
def run(sub, budget_s=470, seed=None):
    import research
    t0 = time.time()
    rng = random.Random(seed)
    st = state(sub)
    st["running"] = iso(now_ny())
    st["step"] = "starting"
    _save(sub, st)
    c = research.criteria(sub)
    hist = research.same_universe(research.ideas(sub, "day"), c)
    uses = piece_uses(hist)
    global EXIT_CHOICES
    allowed = allowed_atoms(hist, c)
    EXIT_CHOICES = allowed_exits(set(research.retired(hist, c)))
    if not allowed:
        st.update(running=None, last={"at": iso(now_ny()), "error": "every condition of the grammar uses a retired piece"})
        _save(sub, st)
        return st
    cost = float(c["costR"])
    inplay = c.get("universe") == "inplay"
    tick = st.get("tickers") or (list(c.get("dayTickers") or [])[:4]) or None   # few tickers: many more strategies a run
    base = research.base_cfg(sub, "day")
    blank = research.blank_cfg(sub, "day")
    if tick and not inplay:
        base["tickers"] = blank["tickers"] = list(tick)
    st["step"] = (f"loading {int(st['days'])} days of stocks in play (top {int(c.get('sipTop') or 15)} a day)" if inplay else
                  f"loading {int(st['days'])} days of candles for {', '.join(base.get('tickers') or [])}")
    _save(sub, st)
    tl = time.time()
    if inplay:
        bench = research.make_bench(sub, int(st["days"]), base, c)
    else:
        bench = research.DayBench(sub, int(st["days"]), base, holdout=int(c.get("holdoutDays") or 0))
    st["loadSecs"] = round(time.time() - tl)
    if not bench.mid:
        st.update(running=None, step=None, last={"at": iso(now_ny()), "error": "no history loaded", "errors": bench.errors[:3]})
        _save(sub, st)
        return st
    tk = taboo_key(research.universe_of(c))
    st.setdefault(tk, [])
    if st.get("universe") != research.universe_of(c):    # another universe: a new population
        st.update(pop=[], universe=research.universe_of(c))
    tsets = _taboo_sets(st[tk])
    fcombos = research.failed_combos(sub, hist)        # the agent's / your failed combinations count here too
    evals0 = st["evals"]

    times = []
    ukey = f"v4|{research.universe_of(c)}"              # scores cached per universe (and per fitness version)
    live = {"pop": []}                                 # the population being evaluated (saved with the progress)

    def evaluate(g):
        k = f"{ukey}|{key(g)}"
        if is_taboo(g, tsets) or research.failed_hit({"full": changes_of(g)}, fcombos):
            return {"fit": -9, "n": 0, "taboo": True}
        if k in st["seen"]:
            return st["seen"][k]
        if True:
            P = {**base, **blank, **changes_of(g)}
            te = time.time()
            try:
                res = None
                if getattr(bench, "screen", None):     # quick screen on every 3rd day of the 1st half first
                    rs = bench.rows_on(P, bench.screen)
                    res = screen_result(rs, cost)
                if res is None:                        # passed (or no screen): the full 1st half
                    rows = bench.rows_on(P, bench.h1) if getattr(bench, "h1", None) else bench.rows(P)
                    res = fitness(rows, bench.mid, cost, g, uses, bench.first)
            except Exception as e:
                res = {"fit": -9, "n": 0, "error": str(e)[:80]}
            times.append(time.time() - te)
            st["evals"] += 1
            st["avgEval"] = round(sum(times) / len(times), 1)
        st["seen"][k] = {kk: v for kk, v in res.items() if kk in SEEN_KEYS}   # small: thousands are cached
        st["step"] = f"generation {st['gen'] + 1}: {st['evals'] - evals0} strategies evaluated this run (~{st.get('avgEval')} s each)"
        if len(times) % 3 == 1:                        # progress kept even if the run is cut off (and shown on the page)
            st["pop"] = [{"g": p["g"], "res": p.get("res") if p["g"] is not g else res} for p in live["pop"]]
            _save(sub, st)
        return res

    def time_left():                                   # room for one more evaluation, the promotion's test and saving
        per = (sum(times) / len(times)) if times else min(20.0, budget_s / 50)
        return time.time() - t0 + 2.5 * per + min(15.0, 0.05 * budget_s) < budget_s

    global RUN_SIDE
    n_run = int(st.get("runCount") or 0)
    st["runCount"] = n_run + 1
    RUN_SIDE = SIDE_CYCLE[n_run % 3]                   # long, short, both in turn: each side gets its runs
    jolt = []
    if n_run % JOLT_EVERY == JOLT_EVERY - 1 and st.get("pop"):    # a jolt: the conditions of the top 5 are out this run
        top5 = rank([{"g": {**p["g"], "atoms": [tuple(a) for a in p["g"]["atoms"]]}, "res": p.get("res")} for p in st["pop"]])[:5]
        jolt = sorted({a[0] for p in top5 for a in p["g"]["atoms"]})
        allowed = [i for i in allowed if i not in jolt] or allowed
        st["pop"] = []                                 # a fresh population from the other conditions
    st["runSide"], st["jolt"] = RUN_SIDE, [atom_expr((i, (GRAMMAR[i][2].get("p") or [None])[0])) for i in jolt]
    pop = [p for p in st["pop"] if all(a[0] in allowed for a in p["g"]["atoms"]) and not is_taboo(p["g"], tsets)]
    pop = [{"g": {**p["g"], "atoms": [tuple(a) for a in p["g"]["atoms"]]}, "res": p.get("res")} for p in pop
           if p["g"].get("target") in EXIT_CHOICES]
    while len(pop) < POP:
        pop.append({"g": rand_genome(allowed, rng)})
    if st.get("popKey") != ukey:                       # scores from another universe / fitness version: scored again
        for p in pop:
            p["res"] = None
        st["popKey"] = ukey
        st["hof"] = []
    gens = 0
    live["pop"] = pop
    while time_left():
        live["pop"] = pop
        for p in pop:
            if not time_left():
                break
            if not p.get("res"):
                p["res"] = evaluate(p["g"])
        rank(pop)
        if not time_left():
            break
        nxt = [{"g": p["g"], "res": p.get("res")} for p in pop[:ELITE]]
        scored = [p for p in pop if p.get("res")]

        def pick():
            a, b = rng.sample(scored, 2) if len(scored) > 1 else (scored[0], scored[0])
            return a if a.get("adj", -99) >= b.get("adj", -99) else b
        while len(nxt) < POP:
            r_ = rng.random()
            if r_ < 0.15:
                g = rand_genome(allowed, rng)          # fresh blood: innovation
            elif r_ < 0.45:
                g = cross(pick()["g"], pick()["g"], rng)
            else:
                g = mutate(pick()["g"], allowed, rng)
            if any(key(g) == key(q["g"]) for q in nxt) or is_taboo(g, tsets):
                continue
            nxt.append({"g": g, "res": st["seen"].get(f"{ukey}|{key(g)}")})
        pop = nxt
        gens += 1
        st["gen"] += 1
    rank(pop)
    hof = [p for p in (st.get("hof") or []) if not is_taboo(p["g"], tsets)]
    hof = _best_of([p for p in hof + [{"g": q["g"], "res": q.get("res")} for q in pop] if (p.get("res") or {}).get("fit") is not None])
    st["hof"] = [{"g": p["g"], "res": p.get("res")} for p in hof[:HOF]]
    if jolt:                                           # after a jolt the best ever join the new population again
        pop = _best_of([{"g": q["g"], "res": q.get("res")} for q in pop] + [dict(p) for p in hof[:ELITE]], POP)
    st["pop"] = [{"g": p["g"], "res": p.get("res")} for p in pop]
    promoted = promote(sub, st, pop, bench, base, c, hist)
    today = now_ny().strftime("%Y-%m-%d")
    st["runs"] = {today: st["runs"].get(today, 0) + 1}
    best = (pop[0].get("res") or {}) if pop else {}
    st["log"] = (st["log"] + [{"at": iso(now_ny()), "gens": gens, "evals": st["evals"] - evals0, "best": best.get("fit"),
                               "promoted": promoted, "secs": round(time.time() - t0)}])[-30:]
    st.update(running=None, step=None, last={"at": iso(now_ny()), "gens": gens, "evals": st["evals"] - evals0, "errors": bench.errors[:3],
                                             "loadSecs": st.get("loadSecs"), "tickers": base.get("tickers"), "side": RUN_SIDE,
                                             "jolt": st.get("jolt") or []})
    _save(sub, st)
    return st


def promote(sub, st, pop, bench, base, c, hist):
    """The best genome not promoted before (fitness >= 1.5) goes into the ideas list, tested by the pipeline with
    every evaluation counted in its t needed; one that doesn't pay on the 2nd half makes its atom set taboo."""
    import research
    done = {p.get("key") for p in st["promoted"]}
    fams = {p.get("fam") or _fam_of_key(p.get("key") or "") for p in st["promoted"]}
    n_tested = research.n_trials(hist, c)
    out = []
    order = [p for p in pop if p["g"]["side"] == RUN_SIDE] + [p for p in pop if p["g"]["side"] != RUN_SIDE] if RUN_SIDE else pop
    for p in order:
        if RUN_SIDE and p["g"]["side"] != RUN_SIDE:
            break                                      # this run sends only an idea of its side (the others get their runs)
        if family_key(p["g"]) in fams:
            continue                                   # the same idea with other numbers was sent already
        r = p.get("res") or {}
        tk = taboo_key(research.universe_of(c))
        if (r.get("fit") or -99) < 1.5 or (r.get("t") or 0) < 1.8 or (r.get("posWin") or 0) < 2 or key(p["g"]) in done or is_taboo(p["g"], _taboo_sets(st.get(tk) or [])):
            continue
        g = p["g"]
        x = research.new_idea("day", research.blank_cfg(sub, "day"), name_of(g), changes_of(g), "evolve", int(st["days"]),
                              f"Found by the genetic search: 1st-half t {r.get('t')}, {r.get('posWin')}/3 windows positive, "
                              f"{r.get('novel', 0)} rarely used pieces.", origin="blank")
        x["searchCount"] = st["evals"]
        x["universe"] = research.universe_of(c)
        x = research.admit(x, set(), banned=research.retired(hist, c))
        if x["status"] == "queued":
            research.test_idea(bench, base, x, c, n_tested + 1 + st["evals"],
                               "trials: the families tested + every strategy the evolution scored")    # every evaluation is a trial here
            if research.rob_due(x, c):
                x["robust"] = {"status": "queued", "at": iso(now_ny())}
            if ((x.get("metrics") or {}).get("h2") or {}).get("avgR") is None or x["metrics"]["h2"]["avgR"] <= 0:
                st[tk] = ((st.get(tk) or []) + [atom_set(g)])[-500:]      # didn't pay on the 2nd half: never again
        research._put_idea(sub, x)
        if (x.get("robust") or {}).get("status") == "queued":
            research.rob_kick(sub)
        st["promoted"].append({"key": key(g), "fam": family_key(g), "id": x["id"], "name": x["name"], "grade": x.get("grade"), "at": iso(now_ny()),
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
        st.update(running=None, step=None, last={"at": iso(now_ny()), "error": str(e)[:200]})
        _save(sub, st)
        return {"error": str(e)[:200]}
    st = state(sub)                                    # the switch as it is NOW (it may have been turned off meanwhile)
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
