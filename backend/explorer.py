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


def _bursts_by_ticker(pk):
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
    bursts = _bursts_by_ticker(pk)
    out = []
    for r in db.q_prefix(pk, "BOT#"):
        at = r.get("createdAt") or ""
        if at < cut or (r.get("shadow") or {}).get("status") == "dup":
            continue
        R, st = _result(r)
        if R is None:
            continue
        f = {"id": r["id"], "symbol": r.get("symbol"), "at": at, "R": R, "status": st, "decision": r.get("decision")}
        checks = r.get("checks") or []
        for key, prefix, _ in CHECKS:
            c = next((c for c in checks if (c.get("text") or "").startswith(prefix)), None)
            f[key] = None if c is None else bool(c.get("ok"))
        if f.get("aboveFlip") is None:                       # "Price below the gamma flip" / "No gamma flip ... positive"
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
            prev2 = (datetime.strptime(d0, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
        except ValueError:
            prev2 = d0
        f["burstSameDay"] = any(d == d0 and s <= hm for d, s in bs)
        f["burst2d"] = any(prev2 <= d < d0 for d, s in bs) or f["burstSameDay"]
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
