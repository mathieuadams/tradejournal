"""Automatic settings search for the 1-minute day trading: the math instead of the eye.

The candles of every day ticker are loaded once. Starting from the current settings, every single change is tested
(each entry rule / filter switched, each exit switched, stop and target sizes). The days are split in two: a change is
judged ONLY on the 1st half: the most total R, kept when it beats the current settings by more than the noise (half
of spread x √trades, at least 2R) and its t-stat (average R / (spread / √trades)) is at least 1; the next round starts
from it (up to 3 changes). The 2nd half is never
used to choose: it shows whether the result holds on days the search did not see. A t-stat under ~2 = could be luck.
"""
import math
import time

import db
from util import iso, now_ny

SK = "DTSEARCH"
STOPS = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)
TARGETS = (1.0, 1.5, 2.0, 3.0)
EXIT_SW = ("trailOn", "vwapExit", "emaExit", "sqExit", "inExit", "fvgExit", "beOn", "scaleOn", "timeOn", "lvlTarget")
MIN_N = 40


def tstat(rs):
    n = len(rs)
    if n < 2:
        return None
    m = sum(rs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (n - 1))
    return round(m / sd * math.sqrt(n), 2) if sd else None


def need(tr):
    """How much more total R (1st half) a change must make to count: half the noise of a total over these trades
    (spread x √trades / 2), at least 2R."""
    n, t, m = tr.get("n") or 0, tr.get("t"), tr.get("avgR")
    if not n or t in (None, 0) or m is None:
        return 2.0
    sd = abs(m) / abs(t) * math.sqrt(n)
    return max(2.0, 0.5 * sd * math.sqrt(n))


def score(rows, mid):
    tr = [r["R"] for r in rows if r.get("R") is not None and r["at"][:10] < mid]
    te = [r["R"] for r in rows if r.get("R") is not None and r["at"][:10] >= mid]
    al = tr + te
    f = lambda xs: {"n": len(xs), "avgR": round(sum(xs) / len(xs), 3) if xs else None, "totR": round(sum(xs), 1), "t": tstat(xs),
                    "win": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1) if xs else None}
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in sorted((r for r in rows if r.get("R") is not None), key=lambda r: r["at"]):
        eq += r["R"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return {"train": f(tr), "test": f(te), "all": {**f(al), "maxDD": round(dd, 1)}}


def candidates(P):
    """(label, changes) of every single change from P."""
    import daytrade
    out = []
    for k in daytrade.ENTRY + daytrade.FILTERS:
        out.append((f"{'remove' if P.get(k) else 'add'} {k}", {k: not P.get(k)}))
    for k in EXIT_SW:
        out.append((f"{'remove' if P.get(k) else 'add'} {k}", {k: not P.get(k)}))
    if P.get("longs") and P.get("shorts"):
        out += [("longs only", {"shorts": False}), ("shorts only", {"longs": False})]
    elif not (P.get("longs") and P.get("shorts")):
        out.append(("longs and shorts", {"longs": True, "shorts": True}))
    for v in STOPS:
        if abs(float(P["stopAtr"]) - v) > 1e-9:
            out.append((f"stop {v} ATR", {"stopAtr": v, "stopOn": True}))
    for v in TARGETS:
        if not P.get("targetOn") or abs(float(P["targetR"]) - v) > 1e-9:
            out.append((f"target {v}R", {"targetR": v, "targetOn": True}))
    if P.get("targetOn"):
        out.append(("no R target", {"targetOn": False}))
    for v in (1, 2, 5):
        if int(P["maxTrades"]) != v:
            out.append((f"max {v} trades a day", {"maxTrades": v}))
    return out


def load(sub, days, P, errors, feed):
    """[{sym, allb, show, ctx, walls}] for every day ticker (context for every rule the search may switch on)."""
    import daytrade
    import backtest as _bt
    import wallwatch
    tickers = P["tickers"]
    vix = {}
    try:
        vix = daytrade.vix_map()
    except Exception as e:
        errors.append(f"VIX: {str(e)[:80]}")
    data, maps = [], {}
    for sym in tickers:
        try:
            allb = daytrade._bars_any(sub, sym, days + 2, feed)
            dates = sorted({b["t"][:10] for b in allb})
            show = dates[-days:]
            try:
                dp = wallwatch.dp_level_map(_bt.stored_darkpool(sym), show)
            except Exception:
                dp = {}
            maps[sym] = daytrade.peer_map(allb)
            data.append({"sym": sym, "allb": allb, "show": show, "dp": dp,
                         "walls": daytrade.walls_by_day(sub, sym, show, bool(P.get("wallNear")), budget_s=120)})
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:120]}")
    for x in data:
        ps = daytrade.peer_of(x["sym"], tickers)
        if ps not in maps:
            try:
                maps[ps] = daytrade.peer_map(daytrade._bars_any(sub, ps, days + 2, {}))
            except Exception as e:
                errors.append(f"{ps} (other ticker): {str(e)[:80]}")
        x["ctx"] = {"dp": x.pop("dp"), "vix": vix, "peer": maps.get(ps) or {}, "cache": {}}
    return data


def evaluate(data, P):
    import daytrade
    rows = []
    for x in data:
        sg = daytrade.detect(x["allb"], P, x["walls"], x["show"][0] if x["show"] else None, x["ctx"])
        for s in sg:
            s["sym"] = x["sym"]
        rows += sg
    return rows


def run(sub, params=None, budget_s=520):
    import daytrade
    t0 = time.time()
    p = params or {}
    base = daytrade.config(sub)
    if isinstance(p.get("cfg"), dict):
        base = daytrade.clean_cfg(base, p["cfg"])
    days = max(20, min(500, int(p.get("days") or 250)))
    rounds_max = max(1, min(3, int(p.get("rounds") or 3)))
    errors, feed = [], {}
    data = load(sub, days, base, errors, feed)
    all_days = sorted({d for x in data for d in x["show"]})
    if not all_days:
        out = {"at": iso(now_ny()), "errors": errors or ["No candles."]}
        db.put({"PK": db.upk(sub), "SK": SK, **_dec(out), "running": None})
        return out
    mid = all_days[len(all_days) // 2]
    cur, cur_s = dict(base), score(evaluate(data, base), mid)
    start_s = cur_s
    path, last_tab, tested = [], [], 0
    for rnd in range(rounds_max):
        tab = []
        for label, ch in candidates(cur):
            if time.time() - t0 > budget_s:
                errors.append(f"time limit in round {rnd + 1}: {len(tab)} changes tested")
                break
            P = {**cur, **ch}
            sc = score(evaluate(data, P), mid)
            tested += 1
            tab.append({"label": label, "changes": ch, **sc})
        last_tab = tab
        ok = [x for x in tab if x["train"]["n"] >= MIN_N and x["train"]["t"] is not None and x["train"]["t"] >= 1]
        ok.sort(key=lambda x: -x["train"]["totR"])
        if not ok or ok[0]["train"]["totR"] < cur_s["train"]["totR"] + need(cur_s["train"]):
            break
        best = ok[0]
        cur = {**cur, **best["changes"]}
        cur_s = {k: best[k] for k in ("train", "test", "all")}
        path.append({"round": rnd + 1, "label": best["label"], "changes": best["changes"], **cur_s})
        if time.time() - t0 > budget_s:
            break
    last_tab.sort(key=lambda x: -x["train"]["totR"])
    tt = cur_s["test"]["t"]
    verdict = ("edge" if tt is not None and tt >= 2 and cur_s["test"]["avgR"] > 0 else
               "weak" if tt is not None and tt >= 1 and cur_s["test"]["avgR"] > 0 else "none")
    changed = {k: cur[k] for k in daytrade.PARAMS if k not in ("enabled", "trade") and cur.get(k) != base.get(k)}
    out = {"at": iso(now_ny()), "days": days, "daysFound": len(all_days), "first": all_days[0], "mid": mid, "feed": feed,
           "start": start_s, "final": cur_s, "path": path, "changes": changed, "verdict": verdict, "tested": tested,
           "table": [{k: x[k] for k in ("label", "changes", "train", "test", "all")} for x in last_tab[:40]],
           "secs": round(time.time() - t0), "errors": errors}
    db.put({"PK": db.upk(sub), "SK": SK, **_dec(out), "running": None})
    return out


def _dec(out):
    import json
    from decimal import Decimal
    return json.loads(json.dumps(out, default=str), parse_float=Decimal)


def result(sub):
    from datetime import timedelta
    r = db.get(db.upk(sub), SK) or {}
    if r.get("running") and r["running"] < iso(now_ny() - timedelta(minutes=12)):
        r = {**r, "running": None, "errors": ["The last run hit the 10-minute limit: try fewer days."]}
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}
