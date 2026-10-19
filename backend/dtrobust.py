"""Robustness check of day-trading settings (one click): is the result a broad edge or one lucky set of numbers?

On the same candles and days (run_variants), the settings are run with:
* every number nudged down and up (about -20% / +20%: stop, target, trailing, breakeven, time stop, filters...);
* every number inside the formulas nudged the same way (thresholds and lengths, one at a time);
* every entry rule / filter that is ON taken out, one at a time;
* longs only / shorts only; entry window 30 minutes shorter on each side.
Plus, on the trades of the settings as they are: twice the cost per trade, and without the best 5% of the trades.

Verdict: robust when at least 70% of the variants stay positive and the median variant keeps at least half of the
average R; fragile when under half stay positive."""
import ast
import math
import time

import db
from util import iso, now_ny

SK = "DTROB"
NUMS = [("stopAtr", "stopOn"), ("targetR", "targetOn"), ("trailAtr", "trailOn"), ("beR", "beOn"), ("scaleR", "scaleOn"),
        ("timeMin", "timeOn"), ("rvolMin", "rvolOn"), ("gapMin", "gapOn"), ("dayStopR", "dayStop"), ("emaLen", "aboveEma"),
        ("ema2Len", "emaStack"), ("orbMin", "orbBreak"), ("formWin", "sqTurn"), ("swingBuf", None), ("volN", None)]
MAX_VARIANTS = 34


def _nudge(v, f, is_int):
    x = v * f
    if is_int:
        x = int(round(x))
        if x == int(v):
            x = int(v) + (1 if f > 1 else -1)
        return max(1, x)
    return round(x, 4)


def formula_variants(key, expr):
    """[(label, new formula)]: each number of the formula -20% / +20% (lengths stay whole numbers >= 1)."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return []
    consts = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)]
    out = []
    for k, c in enumerate(consts[:5]):
        if c.value == 0:
            continue
        for f in (0.8, 1.2):
            t2 = ast.parse(expr, mode="eval")
            c2 = [n for n in ast.walk(t2) if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)][k]
            is_int = isinstance(c.value, int)
            c2.value = _nudge(c.value, f, is_int)
            if c2.value == c.value:
                continue
            out.append((f"{key}: {c.value} → {c2.value}", ast.unparse(t2)))
    return out


def variants(P):
    import daytrade
    vs = [("as they are", {})]
    for k, sw in NUMS:
        if k not in P or (sw and not P.get(sw)) or (k == "swingBuf" and P.get("stopBase") != "swing") or (k == "volN" and P.get("stopBase") != "avgN"):
            continue
        v = P[k]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not v:
            continue
        is_int = k in daytrade.INTS
        for f in (0.8, 1.2):
            nv = _nudge(float(v), f, is_int)
            vs.append((f"{k} {v} → {nv}", {k: nv}))
    for k in daytrade.FORMULAS:
        if P.get(k):
            vs += [(lab, {k: e}) for lab, e in formula_variants(k, P[k])]
    for k in daytrade.ENTRY + daytrade.FILTERS:
        if P.get(k):
            vs.append((f"without {k}", {k: False}))
    if P.get("longs") and P.get("shorts"):
        vs += [("longs only", {"shorts": False}), ("shorts only", {"longs": False})]

    def shift(hm, m):
        h, mm = int(hm[:2]), int(hm[3:5])
        t = max(570, min(959, h * 60 + mm + m))
        return f"{t // 60:02d}:{t % 60:02d}"
    vs += [("entries start 30 min later", {"startT": shift(P["startT"], 30)}), ("entries stop 30 min earlier", {"endT": shift(P["endT"], -30)})]
    return vs[:MAX_VARIANTS]


def _st(rs):
    import dtsearch
    n = len(rs)
    if not n:
        return {"n": 0}
    w, l = [x for x in rs if x > 0], [x for x in rs if x <= 0]
    return {"n": n, "avgR": round(sum(rs) / n, 3), "totR": round(sum(rs), 1), "t": dtsearch.tstat(rs),
            "win": round(len(w) / n * 100, 1), "pf": round(sum(w) / -sum(l), 2) if l and sum(l) < 0 else None}


def run(sub, params=None, budget_s=520):
    import daytrade
    t0 = time.time()
    p = params or {}
    base = daytrade.config(sub)
    P = daytrade.clean_cfg(base, p["cfg"]) if isinstance(p.get("cfg"), dict) else base
    days = max(20, min(500, int(p.get("days") or 250)))
    cost = max(0.0, float(p.get("cost") if p.get("cost") is not None else 0.05))
    vs = variants(P)
    rr, per, all_days, feed, errors = daytrade.run_variants(sub, days, [{**P, **ch} for _, ch in vs], budget_s, P["tickers"])
    ds = sorted(all_days)
    mid = ds[len(ds) // 2] if ds else ""
    rows = []
    for i, (lab, ch) in enumerate(vs):
        rs = [(r["at"], r["R"] - cost) for r in rr.get(i, []) if r.get("R") is not None]
        rows.append({"label": lab, "changes": ch, "all": _st([x for _, x in rs]), "h1": _st([x for a, x in rs if a[:10] < mid]),
                     "h2": _st([x for a, x in rs if a[:10] >= mid])})
    b = rows[0]["all"] if rows else {"n": 0}
    rs0 = sorted((r["R"] - cost for r in rr.get(0, []) if r.get("R") is not None), reverse=True)
    extra = []
    if rs0:
        extra.append({"label": f"twice the cost ({2 * cost:g}R a trade)", "all": _st([x - cost for x in rs0])})
        k = max(1, int(math.ceil(len(rs0) * 0.05)))
        extra.append({"label": f"without the best 5% of trades ({k})", "all": _st(rs0[k:])})
    others = [r for r in rows[1:] if r["all"].get("n")]
    pos = sum(1 for r in others if (r["all"].get("avgR") or 0) > 0)
    med = sorted(r["all"]["avgR"] for r in others)[len(others) // 2] if others else None
    share = round(pos / len(others) * 100) if others else None
    verdict = ("robust" if share is not None and share >= 70 and b.get("avgR") and med is not None and med >= 0.5 * b["avgR"] > 0
               else "fragile" if share is not None and share < 50 else "mixed")
    out = {"at": iso(now_ny()), "days": days, "daysFound": len(ds), "mid": mid, "cost": cost, "base": b, "rows": rows, "extra": extra,
           "positive": pos, "variants": len(others), "share": share, "median": med, "verdict": verdict,
           "used": daytrade.used_summary(P), "secs": round(time.time() - t0), "errors": errors[:6]}
    _save(sub, out)
    return out


def _save(sub, out):
    import json
    from decimal import Decimal
    db.put({"PK": db.upk(sub), "SK": SK, **json.loads(json.dumps(out, default=str), parse_float=Decimal), "running": None})


def job(sub, params=None):
    try:
        return run(sub, params)
    except Exception as e:
        out = {"at": iso(now_ny()), "errors": [str(e)[:200]]}
        _save(sub, out)
        return out


def result(sub):
    from datetime import timedelta
    r = db.get(db.upk(sub), SK) or {}
    if r.get("running") and r["running"] < iso(now_ny() - timedelta(minutes=12)):
        r = {**r, "running": None, "errors": ["The last run hit the 10-minute limit: try fewer days."]}
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}
