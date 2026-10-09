"""Mix-and-match explorer: every evaluation (taken: real result; skipped: shadow result) as one row of features,
filtered by any combination of conditions, plus an automatic search for the best combinations.

Overfitting guard: the search only keeps combinations with at least `min_n` results, and reports whether the
combination holds in both halves of the period (older ideas vs newer ideas)."""
import itertools
import re
from datetime import datetime, timedelta

import db
from util import now_ny

TAKEN = ("submitting", "submitted", "open", "closing", "closed")

# rule checks, by the start of their text
CHECKS = [
    ("bullFvg", "Active bull fair value gap", "Active bull fair value gap"),
    ("noBearFvg", "No active bear", "No bear fair value gap overhead"),
    ("aboveEma", "Close above the 21 EMA", "Close above the 21 EMA"),
    ("recentCross", "Crossed above the 21 EMA", "Crossed above the 21 EMA recently"),
    ("momUp", "Momentum rising", "Momentum rising"),
    ("aboveAvwap", "Close above the anchored VWAP", "Above the anchored VWAP"),
    ("up30", "Up at least", "Up 10%+ from the 30-day low"),
    ("notExtended", "Not extended", "Not extended above the EMA"),
    ("aboveFlip", "Price above the gamma flip", "Above the gamma flip"),
    ("roomOk", "Room ", "Enough room to target vs stop"),
    ("repeatBuy", "Repeat buying", "Repeat buying (smaller call buys)"),
    ("putsOk", "Puts not piling in", "Puts not piling in"),
    ("noEarnings", "No earnings", "No earnings soon"),
]

# feature key -> (label, kind, choices for the search)
FEATURES = {
    **{k: (label, "bool", None) for k, _, label in CHECKS},
    "decisionBuy": ("Bot decision was BUY", "bool", None),
    "taken": ("The bot took the trade", "bool", None),
    "sweep": ("Flow alert was a sweep", "bool", None),
    "burstSameDay": ("Call burst on the ticker that day (before the evaluation)", "bool", None),
    "burst2d": ("Call burst on the ticker in the 2 days before", "bool", None),
    "claudeApprove": ("Claude approved the entry", "bool", None),
    "claudeReject": ("Claude rejected the entry", "bool", None),
    "callPut": ("Calls vs puts bought on the ticker (x to 1, last 5 days)", "num", [(">=", 2), (">=", 3), (">=", 5), ("<=", 2)]),
    "flowPremium": ("Flow alert premium ($)", "num", [(">=", 250000), (">=", 500000), (">=", 1000000)]),
    "volOi": ("Flow volume / open interest", "num", [(">=", 1), (">=", 3), ("<=", 1)]),
    "askPct": ("Flow % bought at the ask", "num", [(">=", 80), (">=", 95)]),
    "smallCalls": ("Smaller call buys on the ticker (last 5 days)", "num", [(">=", 4), (">=", 10), ("<=", 0)]),
    "extAtr": ("ATR above the 21 EMA", "num", [("<=", 0.5), ("<=", 1.0), ("<=", 1.5), (">=", 1.5)]),
    "growth30": ("% up from the 30-day low", "num", [(">=", 5), (">=", 10), (">=", 20), ("<=", 5)]),
    "hour": ("Hour of the evaluation (ET)", "num", [("<=", 10), (">=", 14)]),
}


def bursts_by_ticker(pk):
    """{ticker: [(date, 'HH:MM' window start)]} from live bursts and the 30-day replay."""
    out = {}
    for e in db.q_prefix(pk, "BURST#"):
        if e.get("ticker") and e.get("date"):
            out.setdefault(e["ticker"], []).append((e["date"], e.get("start") or "00:00"))
    st = db.get(pk, "BURSTSTUDY") or {}
    for r in st.get("bursts") or []:
        if r.get("strict") and r.get("ticker") and r.get("date"):
            out.setdefault(r["ticker"], []).append((r["date"], r.get("start") or "00:00"))
    return out


def features_of(r, bursts):
    """Every condition feature of one evaluation (known at evaluation time, except taken / Claude)."""
    at = r.get("createdAt") or ""
    f = {}
    checks = r.get("checks") or []
    for key, prefix, _ in CHECKS:
        c = next((c for c in checks if (c.get("text") or "").startswith(prefix)), None)
        f[key] = None if c is None else bool(c.get("ok"))
    if f.get("aboveFlip") is None:
        c = next((c for c in checks if c.get("group") == "gamma" and "flip" in (c.get("text") or "")), None)
        f["aboveFlip"] = None if c is None else bool(c.get("ok"))
    o = r.get("origin") or {}
    tf = r.get("tickerFlow") or {}
    sig = r.get("signals") or {}
    pr = tf.get("putRatio")
    calls = ((tf.get("window") or {}).get("calls") or {}).get("premium")
    f.update(
        decisionBuy=r.get("decision") == "BUY", taken=bool(r.get("orderId") or r.get("status") in TAKEN),
        sweep=bool(o.get("sweep")) if o.get("type") == "flow" else None,
        claudeApprove=None if not r.get("aiCheck") else r["aiCheck"].get("verdict") == "approve",
        claudeReject=None if not r.get("aiCheck") else r["aiCheck"].get("verdict") == "reject",
        callPut=None if not tf else (round(1 / pr, 2) if pr else (99.0 if calls else None)),
        flowPremium=o.get("premium") if o.get("type") == "flow" else None,
        volOi=o.get("volOi"), askPct=o.get("askPct"),
        smallCalls=((tf.get("window") or {}).get("smallCalls") or {}).get("hits") if tf else None,
        extAtr=sig.get("extAtr"), growth30=sig.get("growth30"),
        hour=int(at[11:13]) if len(at) >= 13 else None,
    )
    bs = bursts.get(r.get("symbol")) or []
    d0, hm = at[:10], at[11:16]
    try:
        prev = (datetime.strptime(d0, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
    except ValueError:
        prev = d0
    f["burstSameDay"] = any(d == d0 and s_ <= hm for d, s_ in bs)
    f["burst2d"] = any(prev <= d < d0 for d, s_ in bs) or f["burstSameDay"]
    return f


def _result(r):
    """(R, status) for an evaluation: the real trade if taken, otherwise its shadow result."""
    if r.get("orderId") or r.get("status") in TAKEN:
        if not r.get("fillPrice"):
            return None, None
        if r.get("realizedR") is not None:
            return r["realizedR"], "done"
        if r.get("riskAtFill") and r.get("lastPl") is not None:
            return round(r["lastPl"] / r["riskAtFill"], 2), "done" if r.get("status") == "closed" else "open"
        return None, None
    sh = r.get("shadow") or {}
    if sh.get("status") in ("done", "tracking") and sh.get("R") is not None:
        return sh["R"], "done" if sh["status"] == "done" else "open"
    return None, None


def rows(sub, days=35):
    pk = db.upk(sub)
    cut = (now_ny() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    bursts = bursts_by_ticker(pk)
    out = []
    for r in db.q_prefix(pk, "BOT#"):
        at = r.get("createdAt") or ""
        if at < cut or (r.get("shadow") or {}).get("status") == "dup":
            continue
        R, st = _result(r)
        if R is None:
            continue
        f = {"id": r["id"], "symbol": r.get("symbol"), "at": at, "R": R, "status": st, "decision": r.get("decision")}
        p = r.get("proposal") or {}
        f["_path"] = (r.get("shadow") or {}).get("path") or r.get("path")
        f["_entry"] = float(p.get("limit") or p.get("debit") or 0)
        f["_prop"] = {k: p.get(k) for k in ("exp", "underlyingStop", "underlyingTarget", "strategy", "width", "debit", "limit")}
        cand = next((c for c in r.get("candidates") or [] if c.get("symbol") == p.get("contract")), None) or ((r.get("candidates") or [None])[0] or {})
        f["_spread"] = cand.get("spreadPct")                     # bid/ask spread of the contract when evaluated, % of mid
        f.update(features_of(r, bursts))
        out.append(f)
    return out


def _match(row, cond):
    v = row.get(cond["key"])
    if v is None:
        return False
    if cond.get("op") in (">=", "<="):
        try:
            x = float(cond["value"])
        except (TypeError, ValueError):
            return False
        return v >= x if cond["op"] == ">=" else v <= x
    return bool(v) == bool(cond.get("value", True))


def stats(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "done": 0, "open": 0, "winPct": None, "avgR": None, "totalR": None}
    Rs = [r["R"] for r in rs]
    return {"n": n, "done": sum(1 for r in rs if r["status"] == "done"), "open": sum(1 for r in rs if r["status"] != "done"),
            "winPct": round(sum(1 for x in Rs if x > 0) / n * 100), "avgR": round(sum(Rs) / n, 2), "totalR": round(sum(Rs), 2)}


def describe(cond):
    label = FEATURES.get(cond["key"], (cond["key"],))[0]
    if cond.get("op") in (">=", "<="):
        v = cond["value"]
        vs = f"{v:,.0f}" if isinstance(v, (int, float)) and abs(v) >= 1000 else f"{v:g}" if isinstance(v, (int, float)) else str(v)
        return f"{label} {'≥' if cond['op'] == '>=' else '≤'} {vs}"
    return label if cond.get("value", True) else f"NOT: {label}"


def filter_rows(rs, conds):
    return [r for r in rs if all(_match(r, c) for c in conds)]


def halves(rs):
    """Average R of the older half and the newer half of the matching ideas (by evaluation time)."""
    s = sorted(rs, key=lambda r: r["at"])
    h = len(s) // 2
    a, b = s[:h], s[h:]
    avg = lambda xs: round(sum(x["R"] for x in xs) / len(xs), 2) if xs else None
    return avg(a), avg(b)


def candidates():
    out = []
    for key, (label, kind, choices) in FEATURES.items():
        if key == "taken":
            continue
        if kind == "bool":
            out += [{"key": key, "value": True}, {"key": key, "value": False}]
        else:
            out += [{"key": key, "op": op, "value": v} for op, v in choices]
    return out


def search(rs, min_n=15, max_depth=3, top=25):
    """Best single conditions, pairs and triples by average R (at least min_n ideas), with the half-split check."""
    base = stats(rs)
    singles = []
    for c in candidates():
        m = filter_rows(rs, [c])
        if len(m) >= min_n:
            singles.append((c, m))
    singles.sort(key=lambda x: -(stats(x[1])["avgR"] or -9))
    pool = singles[:30]                          # combine only the 30 most promising single conditions
    found = [([c], m) for c, m in singles]
    for depth in range(2, max_depth + 1):
        for combo in itertools.combinations(pool, depth):
            keys = [c["key"] for c, _ in combo]
            if len(set(keys)) < len(keys):       # one condition per feature
                continue
            ids = set(r["id"] for r in combo[0][1])
            for _, m in combo[1:]:
                ids &= set(r["id"] for r in m)
            if len(ids) < min_n:
                continue
            found.append(([c for c, _ in combo], [r for r in rs if r["id"] in ids]))
    res = []
    for conds, m in found:
        s = stats(m)
        h1, h2 = halves(m)
        res.append({"conds": conds, "text": " + ".join(describe(c) for c in conds), **s, "older": h1, "newer": h2,
                    "holds": h1 is not None and h2 is not None and h1 > (base["avgR"] or 0) and h2 > (base["avgR"] or 0)})
    res.sort(key=lambda x: (-(x["avgR"] or -9), -x["n"]))
    seen, uniq = set(), []
    for x in res:                                # skip combos that select exactly the same ideas as a better one
        sig = (x["n"], x["totalR"])
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(x)
        if len(uniq) >= top:
            break
    return {"baseline": base, "results": uniq, "minN": min_n, "tested": len(found)}


def explore(sub, days=35, conds=None, do_search=False, min_n=15):
    rs = rows(sub, days)
    conds = [c for c in (conds or []) if c.get("key") in FEATURES]
    m = filter_rows(rs, conds)
    h1, h2 = halves(m)
    out = {"days": days, "all": stats(rs), "match": stats(m), "older": h1, "newer": h2,
           "conds": [{**c, "text": describe(c)} for c in conds],
           "features": [{"key": k, "label": v[0], "kind": v[1], "known": sum(1 for r in rs if r.get(k) is not None)} for k, v in FEATURES.items()],
           "items": sorted([{k: r[k] for k in ("id", "symbol", "at", "R", "status", "decision", "taken")} for r in m],
                           key=lambda r: -r["R"])[:200]}
    if do_search:
        out["search"] = search(rs, min_n=min_n)
    return out


# ---------------- Monte Carlo ----------------

def monte_carlo(sub, days=35, conds=None, trades=100, runs=5000, risk=None, account=None, seed=None):
    """Resample the R results of the matching ideas (with replacement) to simulate `trades` future trades, `runs` times.
    Also a permutation test: how often does a random group of the same size, drawn from all ideas, do as well?"""
    import random
    rnd = random.Random(seed)
    rs = rows(sub, days)
    m = filter_rows(rs, [c for c in (conds or []) if c.get("key") in FEATURES])
    done = [r["R"] for r in m if r["status"] == "done"]
    note = None
    sample = done
    if len(done) < 15:
        sample = [r["R"] for r in m]
        note = f"Only {len(done)} finished ideas: open ideas are included at their current result."
    if len(sample) < 5:
        return {"error": f"Not enough ideas to simulate ({len(sample)}). Loosen the conditions or wait for more results."}
    trades, runs = max(10, min(int(trades), 500)), max(500, min(int(runs), 10000))
    totals, dds, streaks = [], [], []
    marks = [i for i in range(0, trades + 1, max(1, trades // 50))]
    if marks[-1] != trades:
        marks.append(trades)
    paths = [[] for _ in marks]
    for _ in range(runs):
        eq = peak = dd = 0.0
        streak = worst = 0
        mi = 0
        if marks[0] == 0:
            paths[0].append(0.0)
            mi = 1
        for i in range(1, trades + 1):
            x = rnd.choice(sample)
            eq += x
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
            streak = streak + 1 if x <= 0 else 0
            worst = max(worst, streak)
            if mi < len(marks) and marks[mi] == i:
                paths[mi].append(eq)
                mi += 1
        totals.append(eq)
        dds.append(dd)
        streaks.append(worst)
    q = lambda xs, p: sorted(xs)[min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))]
    fan = [{"trade": t, "p5": round(q(v, 5), 2), "p25": round(q(v, 25), 2), "p50": round(q(v, 50), 2), "p75": round(q(v, 75), 2),
            "p95": round(q(v, 95), 2)} for t, v in zip(marks, paths)]
    lo, hi = min(totals), max(totals)
    bins = 24
    width = (hi - lo) / bins or 1
    hist = [0] * bins
    for t in totals:
        hist[min(bins - 1, int((t - lo) / width))] += 1
    # permutation test on the average R
    allR = [r["R"] for r in rs if (r["status"] == "done" or note)]
    obs = sum(sample) / len(sample)
    perm = None
    if len(allR) > len(sample) >= 5:
        k, better = len(sample), 0
        for _ in range(2000):
            if sum(rnd.sample(allR, k)) / k >= obs:
                better += 1
        perm = round(better / 2000 * 100, 1)
    out = {
        "n": len(sample), "finished": len(done), "note": note, "trades": trades, "runs": runs, "avgR": round(obs, 3),
        "winPct": round(sum(1 for x in sample if x > 0) / len(sample) * 100),
        "total": {"p5": round(q(totals, 5), 1), "p50": round(q(totals, 50), 1), "p95": round(q(totals, 95), 1)},
        "probPositive": round(sum(1 for t in totals if t > 0) / runs * 100, 1),
        "drawdown": {"p50": round(q(dds, 50), 1), "p95": round(q(dds, 5), 1)},
        "losingStreak": {"p50": q(streaks, 50), "p95": q(streaks, 95)},
        "fan": fan, "hist": {"from": round(lo, 2), "width": round(width, 3), "counts": hist},
        "randomAsGood": perm, "conds": [{**c, "text": describe(c)} for c in (conds or []) if c.get("key") in FEATURES],
    }
    if risk:
        out["dollars"] = {"risk": risk, "p5": round(out["total"]["p5"] * risk), "p50": round(out["total"]["p50"] * risk),
                          "p95": round(out["total"]["p95"] * risk), "dd50": round(out["drawdown"]["p50"] * risk),
                          "dd95": round(out["drawdown"]["p95"] * risk)}
        if account:
            ruin_lvl = -0.5 * account / risk                  # losing half the account, in R
            out["dollars"]["halfAccountPct"] = round(sum(1 for d in dds if d <= ruin_lvl) / runs * 100, 1)
            out["dollars"]["account"] = account
    return out


# ---------------- best exit ----------------

def exit_grid():
    out = []
    for stop in (20, 30, 40, 50, 60):
        for target in (None, 40, 60, 80, 100, 150):
            for trail in ((None,) if target is None else (None, 15, 25, 35)):
                for max_days in (None, 5, 10):
                    for inv in (True, False):
                        out.append({"stop": stop, "target": target, "trail": trail, "maxDays": max_days, "invalidation": inv})
    return out


def describe_exit(e):
    parts = [f"stop -{e['stop']:g}%"]
    if e.get("target") is None:
        parts.append("no target")
    elif e.get("trail"):
        parts.append(f"at +{e['target']:g}% trail {e['trail']:g}%")
    else:
        parts.append(f"sell at +{e['target']:g}%")
    parts.append(f"max {e['maxDays']} days" if e.get("maxDays") else "hold up to 20 days")
    parts.append("invalidation on" if e.get("invalidation", True) else "no invalidation exit")
    return " · ".join(parts)


def best_exit(sub, days=35, conds=None, min_n=15, top=10, default_spread=10.0, spread_mult=1.0):
    """Replay every exit-rule combination on the stored price paths of the matching ideas; rank by average R."""
    import autotrader
    import shadow
    cfg = autotrader.settings(sub)
    rs = filter_rows(rows(sub, days), [c for c in (conds or []) if c.get("key") in FEATURES])
    ideas = [r for r in rs if r.get("_path") and r.get("_entry", 0) > 0]
    if len(ideas) < min_n:
        return {"error": f"Only {len(ideas)} matching ideas have a stored price path (need {min_n}). Paths are saved by "
                         f"the 16:20 update: press Update now on Skipped & what-if, or loosen the conditions."}
    ideas.sort(key=lambda r: r["at"])
    half = len(ideas) // 2
    sp = lambda r: (r.get("_spread") if r.get("_spread") is not None else default_spread) * spread_mult
    avg_spread = round(sum(sp(r) for r in ideas) / len(ideas), 1)
    def run(params):
        res = [shadow.sim_path(r["_path"], r["_entry"], r["_prop"],
                               {**params, "timeStopDte": cfg["timeStopDte"], "halfSpread": sp(r) / 200}) for r in ideas]
        Rs = [x["R"] for x in res]
        a, b = Rs[:half], Rs[half:]
        return {"n": len(Rs), "avgR": round(sum(Rs) / len(Rs), 3), "winPct": round(sum(1 for x in Rs if x > 0) / len(Rs) * 100),
                "totalR": round(sum(Rs), 2), "older": round(sum(a) / len(a), 2) if a else None, "newer": round(sum(b) / len(b), 2) if b else None,
                "avgDays": round(sum(x["days"] for x in res) / len(res), 1)}
    cur_p = shadow.params_of(cfg, {}, 1.0)
    current = {"params": {k: cur_p[k] for k in ("stop", "target", "trail", "maxDays", "invalidation")}, **run(cur_p)}
    current["text"] = describe_exit(current["params"])
    res = []
    for e in exit_grid():
        st = run(e)
        res.append({"params": e, "text": describe_exit(e), **st, "tight": e["stop"] < 2 * avg_spread,
                    "holds": st["older"] is not None and st["newer"] is not None and st["older"] > current["older"] and st["newer"] > current["newer"]})
    res.sort(key=lambda x: (-x["avgR"], -x["n"]))
    return {"n": len(ideas), "tested": len(res), "current": current, "results": res[:top], "avgSpread": avg_spread,
            "spreadKnown": sum(1 for r in ideas if r.get("_spread") is not None), "spreadMult": spread_mult,
            "conds": [{**c, "text": describe(c)} for c in (conds or []) if c.get("key") in FEATURES]}
