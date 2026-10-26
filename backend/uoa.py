"""Unusual options activity study: WHEN do unusual call / put buys lead the stock?

1. History (background, chained): every call AND put flow alert of at least minPrem, bought at the ask (60%+ of the
   premium), any expiration, for every watchlist ticker, as far back as Unusual Whales pages — with the details
   (strike, expiration, underlying price, sweep, volume / open interest). Stored by ticker and month, kept for good,
   topped up afterwards.
2. Study: the alerts of a ticker / day / side become one EVENT (the biggest alert + the count + the total premium),
   so a burst of alerts counts once. For each event: the stock's move from that day's close over the next
   1 / 3 / 5 / 10 trading days, in the alert's direction (a put event: the move upside down), raw and vs SPY.
   Then by condition — market (phase, VIX, risk dial), the stock (weekly EMA score, EMA 50 / 200, momentum) and the
   alert (premium, unusual for the ticker, days to expiration, moneyness, sweep, opening, time of day, burst) — one
   condition at a time and in pairs, on the train period (the first 60%), with the test period shown.
Everything known at the alert's day close; nothing later."""
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import db
from util import iso, now_ny

META = "UOAMETA#"
SK = "UOASTUDY"
HIST_SK = "UOAHIST"
TRAIN = 0.6
HZ = (1, 3, 5, 10)


def _key(sym, m):
    return f"UOA#{sym}#{m}"


# ---------------------------------------------------------------- 1. the history
def _page(key, q):
    import flowdata
    for k in range(4):
        try:
            return flowdata._get(key, q).get("data") or []
        except Exception as e:
            if "429" in str(e) or "error (5" in str(e):
                time.sleep(2 + 3 * k)
                continue
            raise
    return []


def fetch(sub, key, sym, side, days, min_prem, budget_s):
    """Page back through a ticker's alerts of one side until `days` back (resumable: the cursor is kept)."""
    import flowdata
    pk = db.upk(sub)
    mk = f"{META}{sym}#{side}"
    meta = db.get(pk, mk) or {}
    start = (now_ny() - timedelta(days=int(days * 1.45) + 3)).strftime("%Y-%m-%dT%H:%M:%S")
    deep = meta.get("exhausted") or (meta.get("oldest") or "9999") <= (now_ny() - timedelta(days=int(days * 1.45) - 5)).strftime("%Y-%m-%d")
    if meta.get("done") and deep and meta.get("days", 0) >= days and meta.get("minPrem") == min_prem:
        stop = max(start, (meta.get("newest") or start))          # top-up: only what's new
        older = None
    else:
        stop = start
        older = meta.get("cursor") if meta.get("minPrem") == min_prem and meta.get("days") == days else None
    base = {"limit": 200, "min_premium": int(min_prem), "min_dte": 0, "max_dte": 800, "ticker_symbol": sym, "is_ask_side": "true",
            "is_call": "true" if side == "call" else "false", "is_put": "true" if side == "put" else "false"}
    t0, n, by_m, newest, oldest, finished, why = time.time(), 0, {}, meta.get("newest"), meta.get("oldest"), False, None
    while time.time() - t0 < budget_s:
        q = dict(base)
        if older:
            q["older_than"] = older
        page = _page(key, q)
        for a in page:
            created = (a.get("created_at") or "").replace(" ", "T")[:19]
            if not created or created < stop:
                continue
            if (a.get("ticker") or "").upper() != sym or (a.get("type") or "").lower() != side:
                continue
            prem, ask = flowdata._f(a.get("total_premium")), flowdata._f(a.get("total_ask_side_prem"))
            if prem < min_prem or (prem and ask / prem < 0.6):
                continue
            try:
                from util import utc_to_ny
                et = utc_to_ny(datetime.strptime(created, "%Y-%m-%dT%H:%M:%S")).strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                continue
            row = {"t": et, "p": round(prem), "k": flowdata._f(a.get("strike")), "x": a.get("expiry"), "u": flowdata._f(a.get("underlying_price")),
                   "sw": 1 if a.get("has_sweep") else 0, "vo": round(flowdata._f(a.get("volume_oi_ratio")), 2), "id": a.get("id")}
            by_m.setdefault(et[:7], []).append(row)
            n += 1
            newest = max(newest or "", created)
            oldest = min(oldest or "9999", et[:10])
        if not page:                                   # (a short page isn't the end: the plan may cap the page size)
            finished, why = True, "empty"
            break
        oc = min((a.get("created_at") or "") for a in page)
        if not oc or oc.replace(" ", "T")[:19] < stop or oc == older:
            finished = True
            break
        older = oc
    for m, rows in by_m.items():
        it = db.get(pk, _key(sym, m)) or {}
        cur = it.get(side) or []
        seen = {(r.get("id") or (r["t"], r["p"])) for r in cur}
        cur += [r for r in rows if (r.get("id") or (r["t"], r["p"])) not in seen]
        db.put({**it, "PK": pk, "SK": _key(sym, m), side: sorted(cur, key=lambda r: r["t"])[-6000:]})
    meta.update(days=days, minPrem=min_prem, newest=newest, oldest=oldest, at=iso(now_ny()), n=int(meta.get("n") or 0) + n,
                cursor=None if finished else older, done=bool(finished or meta.get("done")),
                exhausted=bool(meta.get("exhausted") or why == "empty"))
    db.put({"PK": pk, "SK": mk, **meta})
    return finished, n


def hist_public(sub):
    st = db.get(db.upk(sub), HIST_SK) or {}
    return {k: st.get(k) for k in ("running", "days", "minPrem", "total", "done", "alerts", "errors", "at", "oldest", "newestOldest")}


def hist_start(sub, b):
    import research
    b = b or {}
    st = db.get(db.upk(sub), HIST_SK) or {}
    if b.get("reload"):                                # page everything again (after a fix of the paging)
        for it in db.q_prefix(db.upk(sub), META):
            db.put({**it, "done": False, "cursor": None, "newest": None})
    st.update(PK=db.upk(sub), SK=HIST_SK, running=iso(now_ny()), days=max(60, min(800, int(b.get("days") or st.get("days") or 500))),
              minPrem=int(b.get("minPrem") or st.get("minPrem") or 100000), errors=[], doneList=[])
    db.put(st)
    research._invoke(sub, "uoahist", {})
    return hist_public(sub)


def hist_job(sub, params=None, budget_s=480):
    """One round over the tickers (both sides), resumed by the next run until every ticker is loaded."""
    import flowdata
    import research
    import wallwatch
    t0 = time.time()
    pk = db.upk(sub)
    st = db.get(pk, HIST_SK) or {}
    if not st.get("running"):
        return {"idle": True}
    key = flowdata._key(sub)
    tick = wallwatch.config(sub)["tickers"]
    done = set(st.get("doneList") or [])
    errs = list(st.get("errors") or [])
    for sym in [t for t in tick if t not in done]:
        if time.time() - t0 > budget_s:
            break
        ok = True
        for side in ("call", "put"):
            try:
                fin, _ = fetch(sub, key, sym, side, st["days"], st["minPrem"], max(20, (budget_s - (time.time() - t0)) / 2))
                ok = ok and fin
            except Exception as e:
                errs.append(f"{sym} {side}: {str(e)[:80]}")
                ok = True
        if ok:
            done.add(sym)
    n = sum(int((db.get(pk, f"{META}{s}#{sd}") or {}).get("n") or 0) for s in tick for sd in ("call", "put")) if len(done) >= len(tick) else st.get("alerts")
    olds = [x for x in ((db.get(pk, f"{META}{s}#{sd}") or {}).get("oldest") for s in done for sd in ("call", "put")) if x]
    st.update(doneList=sorted(done), done=len(done), total=len(tick), errors=errs[-8:], at=iso(now_ny()), alerts=n,
              oldest=min(olds) if olds else st.get("oldest"), newestOldest=max(olds) if olds else None, running=iso(now_ny()) if len(done) < len(tick) else None)
    db.put(st)
    if len(done) < len(tick):
        research._invoke(sub, "uoahist", {})
    return {"done": len(done), "total": len(tick)}


def stored(sub, sym):
    """{"call": [...], "put": [...]} of a ticker, oldest first."""
    out = {"call": [], "put": []}
    for it in db.q_prefix(db.upk(sub), f"UOA#{sym}#"):
        for side in ("call", "put"):
            out[side] += it.get(side) or []
    for side in out:
        out[side].sort(key=lambda r: r["t"])
    return out


# ---------------------------------------------------------------- 2. the study
def _bucket(v, edges, labels):
    if v is None:
        return None
    for e, l in zip(edges, labels):
        if v < e:
            return l
    return labels[-1]


FACTORS = [
    ("phase", "Market phase (SPY trend)"), ("vix", "VIX"), ("dial", "Risk dial"), ("spy50", "SPY vs its 50-day"),
    ("ws", "Stock's weekly EMA score"), ("e50", "Stock vs its 50-day"), ("e200", "Stock vs its 200-day"), ("mom", "Stock's last 20 days"),
    ("prem", "Premium (biggest alert)"), ("rel", "Unusual for the ticker"), ("dte", "Days to expiration"), ("mny", "Moneyness"),
    ("sweep", "Sweep"), ("open", "Opening (volume > open interest)"), ("tod", "Time of the day"), ("burst", "Alerts that day (same side)"),
    ("cluster", "Single vs cluster (same side, last 5 days)"), ("repeat", "Same contract bought again that day"), ("total", "Total premium that day"),
    ("mixed", "Other side that day"),
]


def events(alerts, side):
    """A ticker's alerts of one side -> one event per day: the biggest alert, the count, the total premium."""
    by = {}
    for a in alerts:
        by.setdefault(a["t"][:10], []).append(a)
    out = []
    for d, xs in sorted(by.items()):
        big = max(xs, key=lambda a: a["p"])
        ks = {}
        for a in xs:
            ks[(a.get("k"), a.get("x"))] = ks.get((a.get("k"), a.get("x")), 0) + 1
        out.append({"d": d, "side": side, "big": big, "n": len(xs), "tot": sum(a["p"] for a in xs), "first": min(a["t"] for a in xs),
                    "rep": max(ks.values()) if ks else 1})
    return out


def features(ev, ctx, i, hist_prem):
    """The conditions of an event (its day's index i in the trading days)."""
    b, side = ev["big"], ev["side"]
    c = ctx["c"][i]
    f = {}
    f["phase"] = ctx["phase"][i]
    v = ctx["vix"][i]
    f["vix"] = _bucket(v, (15, 20, 30), ("under 15", "15-20", "20-30", "30+"))
    f["dial"] = "alarm" if ctx["alarm"][i] else "caution" if ctx["caution"][i] else "normal"
    f["spy50"] = None if ctx["spy"][i] is None or ctx["spy50"][i] is None else ("above" if ctx["spy"][i] > ctx["spy50"][i] else "under")
    ws = ctx["ws"][i]
    f["ws"] = None if ws is None else ("+2" if ws == 2 else "+1" if ws == 1 else "0" if ws == 0 else "-1" if ws == -1 else "-2")
    f["e50"] = None if c is None or ctx["e50"][i] is None else ("above" if c > ctx["e50"][i] else "under")
    f["e200"] = None if c is None or ctx["e200"][i] is None else ("above" if c > ctx["e200"][i] else "under")
    m = c / ctx["c"][i - 20] - 1 if i >= 20 and c and ctx["c"][i - 20] else None
    f["mom"] = _bucket(None if m is None else m * 100, (-5, 0, 5), ("down 5%+", "down 0-5%", "up 0-5%", "up 5%+"))
    f["prem"] = _bucket(b["p"], (250e3, 1e6, 5e6), ("100-250K", "250K-1M", "1-5M", "5M+"))
    med = sorted(hist_prem)[len(hist_prem) // 2] if len(hist_prem) >= 5 else None
    f["rel"] = None if med is None else ("3x+ its median" if b["p"] >= 3 * med else "normal size")
    try:
        dte = (datetime.strptime(b["x"], "%Y-%m-%d").date() - datetime.strptime(ev["d"], "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        dte = None
    f["dte"] = _bucket(dte, (8, 31, 91), ("0-7", "8-30", "31-90", "91+"))
    u, k = b.get("u") or c, b.get("k")
    if u and k:
        otm = (k / u - 1) * 100 if side == "call" else (1 - k / u) * 100
        f["mny"] = _bucket(otm, (-2, 2, 10), ("in the money", "at the money", "OTM 2-10%", "OTM 10%+"))
    else:
        f["mny"] = None
    f["sweep"] = "sweep" if b.get("sw") else "no sweep"
    f["open"] = None if not b.get("vo") else ("opening (vol > OI)" if b["vo"] > 1 else "vol <= OI")
    hm = ev["first"][11:16]
    f["tod"] = "before 10:30" if hm < "10:30" else "last hour" if hm >= "15:00" else "10:30-15:00"
    f["burst"] = "1" if ev["n"] == 1 else "2-4" if ev["n"] < 5 else "5+"
    pr = ev.get("prior", 0)                           # days of the 5 before with alerts of the same side
    f["cluster"] = "single (none in 5 days)" if pr == 0 and ev["n"] == 1 else "first day of a burst" if pr == 0 else "cluster 1-2 days" if pr <= 2 else "cluster 3+ days"
    f["repeat"] = "same contract 2x+" if ev.get("rep", 1) >= 2 else "different contracts"
    f["total"] = _bucket(ev["tot"], (500e3, 2e6, 10e6), ("under 500K", "500K-2M", "2-10M", "10M+"))
    f["mixed"] = "other side too" if ev.get("mixed") else "this side only"
    return f


def _agg(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if not n:
        return {"n": 0}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, n - 1))
    return {"n": n, "avg": round(m, 3), "win": round(sum(1 for x in xs if x > 0) / n * 100), "t": round(m / sd * math.sqrt(n), 2) if sd > 0 else None}


def study_public(sub):
    it = db.get(db.upk(sub), SK) or {}
    out = json.loads(it["data"]) if it.get("data") else {}
    if out.get("running") and out["running"] < iso(now_ny() - timedelta(minutes=12)):
        out["running"] = None
    return out


def _save(sub, x):
    db.put({"PK": db.upk(sub), "SK": SK, "data": json.dumps(x, default=str, separators=(",", ":"))})


def study_start(sub, b):
    import research
    x = study_public(sub)
    if x.get("running"):
        return x
    x.update(running=iso(now_ny()), step="starting", error=None)
    _save(sub, x)
    research._invoke(sub, "uoastudy", {})
    return x


def run_study(sub):
    import macro
    import plab
    import rank
    import regime
    import wallwatch
    x = study_public(sub)
    tick = wallwatch.config(sub)["tickers"]
    x.update(step=f"loading the alerts and the daily candles of {len(tick)} tickers")
    _save(sub, x)
    al = {s: stored(sub, s) for s in tick}
    tick = [s for s in tick if al[s]["call"] or al[s]["put"]]
    if not tick:
        raise ValueError("No alert history yet: press Get the alert history first.")
    first_alert = min(a["t"][:10] for s in tick for side in ("call", "put") for a in al[s][side][:1])
    years = max(2, min(15, (now_ny().date() - datetime.strptime(first_alert, "%Y-%m-%d").date()).days / 365.25 + 1.2))
    data = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for s, f in [(s, ex.submit(plab._bars, s, years)) for s in dict.fromkeys(tick + ["SPY", "^VIX", "^VIX3M", "HYG", "IEF"])]:
            try:
                d = f.result(timeout=300)
                if d:
                    data[s] = d
            except Exception:
                pass
    days = sorted(data["SPY"])
    x["step"] = "the market and the stocks' conditions"
    _save(sub, x)
    X = plab.prepare(data, days, [s for s in tick if s in data], [], years)
    spyc = X["A"]["SPY"]["c"]
    lab = regime.label_days({d: v[3] for d, v in data["SPY"].items()}, {d: v[3] for d, v in data["^VIX"].items()},
                            {d: v[3] for d, v in data.get("^VIX3M", {}).items()})
    ctx0 = {"phase": [{"Up": "bull", "Range": "sideways", "Down": "bear"}.get((lab.get(d) or {}).get("trend")) for d in days],
            "vix": macro.align({d: v[3] for d, v in data["^VIX"].items()}, days), "alarm": X["alarm"], "caution": X["caution"],
            "spy": spyc, "spy50": X["A"]["SPY"]["e50"]}
    pos = {d: i for i, d in enumerate(days)}
    rows = []
    for s in tick:
        if s not in X["A"]:
            continue
        A = X["A"][s]
        clf = macro.align({d: v[3] for d, v in data[s].items()}, days)
        ctx = {**ctx0, "c": A["c"], "e50": A["e50"], "e200": macro.ema(clf, 200), "ws": X["w"][s]}
        for side in ("call", "put"):
            hist = []
            other = {a["t"][:10] for a in al[s]["put" if side == "call" else "call"]}
            seen_i = []
            for ev in events(al[s][side], side):
                i = pos.get(ev["d"])
                ev["mixed"] = ev["d"] in other
                ev["prior"] = sum(1 for j in seen_i[-6:] if i is not None and i - 5 <= j < i)
                if i is not None:
                    seen_i.append(i)
                if i is None or A["c"][i] is None:
                    hist.append(ev["big"]["p"])
                    continue
                f = features(ev, ctx, i, hist[-60:])
                hist.append(ev["big"]["p"])
                sg = 1 if side == "call" else -1
                fw, fx = {}, {}
                for h in HZ:
                    j = i + h
                    if j < len(days) and A["c"][j] and spyc[j] and spyc[i]:
                        r = A["c"][j] / A["c"][i] - 1
                        fw[h] = round(sg * r * 100, 3)
                        fx[h] = round(sg * (r - (spyc[j] / spyc[i] - 1)) * 100, 3)
                rows.append({"s": s, "d": ev["d"], "side": side, "f": f, "fw": fw, "fx": fx, "i": i})
    rows.sort(key=lambda r: r["d"])
    cut = rows[int(len(rows) * TRAIN)]["d"] if rows else None
    x["step"] = f"{len(rows)} events: by condition, alone and in pairs"
    _save(sub, x)

    def agg(rs, h, k):
        return _agg([r[k].get(h) for r in rs])
    out = {"sides": {}, "factors": {}, "pairs": {}, "split": {"from": rows[0]["d"] if rows else None, "cut": cut, "to": rows[-1]["d"] if rows else None}}
    for side in ("call", "put"):
        R = [r for r in rows if r["side"] == side]
        tr, te = [r for r in R if r["d"] < cut], [r for r in R if r["d"] >= cut]
        out["sides"][side] = {p: {str(h): {"raw": agg(rs, h, "fw"), "vs": agg(rs, h, "fx")} for h in HZ} for p, rs in (("all", R), ("train", tr), ("test", te))}
        fac = {}
        for fk, _ in FACTORS:
            vals = sorted({r["f"].get(fk) for r in R if r["f"].get(fk) is not None})
            fac[fk] = {v: {p: {str(h): {"raw": agg([r for r in rs if r["f"].get(fk) == v], h, "fw"), "vs": agg([r for r in rs if r["f"].get(fk) == v], h, "fx")}
                               for h in HZ} for p, rs in (("train", tr), ("test", te))} for v in vals}
        out["factors"][side] = fac
        best = []                                      # pairs of conditions, scored on the train 5-day move vs SPY
        keys = [fk for fk, _ in FACTORS]
        for a_i, fa in enumerate(keys):
            for fb in keys[a_i + 1:]:
                groups = {}
                for r in tr:
                    va, vb = r["f"].get(fa), r["f"].get(fb)
                    if va is not None and vb is not None and 5 in r["fx"]:
                        groups.setdefault((va, vb), []).append(r["fx"][5])
                for (va, vb), xs in groups.items():
                    if len(xs) < 60:
                        continue
                    g = _agg(xs)
                    if g.get("t") is not None:
                        best.append((g["t"], fa, va, fb, vb, g))
        best.sort(key=lambda z: -z[0])
        pairs = []
        for tt, fa, va, fb, vb, g in best[:30]:
            sel = lambda rs: [r for r in rs if r["f"].get(fa) == va and r["f"].get(fb) == vb]
            pairs.append({"a": [fa, va], "b": [fb, vb], "train": {str(h): agg(sel(tr), h, "fx") for h in HZ},
                          "test": {str(h): agg(sel(te), h, "fx") for h in HZ}, "trainRaw": {str(h): agg(sel(tr), h, "fw") for h in HZ},
                          "testRaw": {str(h): agg(sel(te), h, "fw") for h in HZ}})
        out["pairs"][side] = pairs
    x["step"] = "validating the candidate rules"
    _save(sub, x)
    try:
        out["validation"] = validate(rows, X, days, cut, ctx0, [s for s in tick if s in X["A"]])
    except Exception as e:
        out["validation"] = {"error": str(e)[:160]}
    for r in rows:
        r.pop("i", None)
    x.update(running=None, step=None, at=iso(now_ny()), error=None, n=len(rows), nTickers=len(tick), hz=list(HZ),
             factorNames=dict(FACTORS), **out)
    _save(sub, x)
    return x


def study_job(sub, params=None):
    try:
        return {"n": run_study(sub).get("n")}
    except Exception as e:
        x = study_public(sub)
        x.update(running=None, step=None, error=str(e)[:200])
        _save(sub, x)
        return {"error": str(e)[:200]}


# ---------------------------------------------------------------- 3. validation of a candidate rule (stage 2 -> 3)
RULES = [
    {"key": "callSpyWeak", "name": "Call alerts while SPY is under its 50-day", "side": "call", "cond": {"spy50": "under"}},
    {"key": "callSpyWeakStock", "name": "… and the stock under its 50-day too", "side": "call", "cond": {"spy50": "under", "e50": "under"}},
    {"key": "callCaution", "name": "Call alerts while the risk dial says caution", "side": "call", "cond": {"dial": "caution"}},
]
HOLDS = (3, 5, 10, 20)
STOPS = (0, 2.0, 3.0)
COSTS = (5, 10, 20, 40)


def _trade(A, i, side, hold, stop, cost_bp, entry="close"):
    """One trade of the stock (long for a call alert, short for a put): entry at the event day's close (or the next
    open), out after `hold` trading days at the close, or at the stop (k x ATR, on the daily lows / highs). % net."""
    r, c, atr = A["r"], A["c"], A["atr"]
    n = len(c)
    d = 1 if side == "call" else -1
    j0 = i if entry == "close" else i + 1
    if j0 >= n or not r[j0] or not atr[i]:
        return None
    px = c[i] if entry == "close" else r[j0][0]
    st = px - d * stop * atr[i] if stop else None
    for k in range(j0 + 1, min(n, j0 + hold + 1)):
        if not r[k]:
            continue
        o, h, l, cl = r[k][:4]
        if st is not None and ((d > 0 and l <= st) or (d < 0 and h >= st)):
            ex = min(o, st) if d > 0 else max(o, st)
            return d * (ex / px - 1) * 100 - cost_bp / 100
        if k == j0 + hold:
            return d * (cl / px - 1) * 100 - cost_bp / 100
    return None


def _cond_ok(f, cond):
    return all(f.get(k) == v for k, v in cond.items())


def _base_f(ctx0, A, i):
    """The conditions a baseline day needs (market + the stock vs its 50-day)."""
    return {"spy50": None if ctx0["spy"][i] is None or ctx0["spy50"][i] is None else ("above" if ctx0["spy"][i] > ctx0["spy50"][i] else "under"),
            "dial": "alarm" if ctx0["alarm"][i] else "caution" if ctx0["caution"][i] else "normal",
            "e50": None if A["c"][i] is None or A["e50"][i] is None else ("above" if A["c"][i] > A["e50"][i] else "under"),
            "phase": ctx0["phase"][i]}


def validate(rows, X, days, cut, ctx0, tick, cost_bp=10):
    """For each candidate rule: the trade results by holding time x stop (train / test), vs the baseline (every
    ticker on every day where the same conditions hold, same trade), the cost stress, the months, the tickers."""
    out = []
    first = rows[0]["d"] if rows else days[0]
    base_cache = {}
    for R in RULES:
        ev = [r for r in rows if r["side"] == R["side"] and _cond_ok(r["f"], R["cond"])]
        grid = []
        for hold in HOLDS:
            for stop in STOPS:
                tr, te = [], []
                for r in ev:
                    x = _trade(X["A"][r["s"]], r["i"], R["side"], hold, stop, cost_bp)
                    if x is not None:
                        (tr if r["d"] < cut else te).append(x)
                bk = (R["side"], tuple(sorted(R["cond"].items())), hold, stop)
                if bk not in base_cache:                # the baseline: every ticker, every matching day (every 4th day: lighter)
                    btr, bte = [], []
                    for s in tick:
                        A = X["A"][s]
                        for i in range(0, len(days), 4):
                            if days[i] < first or A["c"][i] is None or not _cond_ok(_base_f(ctx0, A, i), R["cond"]):
                                continue
                            x = _trade(A, i, R["side"], hold, stop, cost_bp)
                            if x is not None:
                                (btr if days[i] < cut else bte).append(x)
                    base_cache[bk] = (_agg(btr), _agg(bte))
                b_tr, b_te = base_cache[bk]
                a_tr, a_te = _agg(tr), _agg(te)
                grid.append({"hold": hold, "stop": stop, "train": a_tr, "test": a_te, "baseTrain": b_tr, "baseTest": b_te,
                             "edgeTrain": round(a_tr["avg"] - b_tr["avg"], 3) if a_tr.get("n") and b_tr.get("n") else None,
                             "edgeTest": round(a_te["avg"] - b_te["avg"], 3) if a_te.get("n") and b_te.get("n") else None})
        costs = []
        for c_ in COSTS:
            xs = [_trade(X["A"][r["s"]], r["i"], R["side"], 10, 0, c_) for r in ev]
            costs.append({"bp": c_, "all": _agg(xs)})
        months = {}
        for r in ev:
            x = _trade(X["A"][r["s"]], r["i"], R["side"], 10, 0, cost_bp)
            if x is not None:
                months.setdefault(r["d"][:7], []).append(x)
        by_t = {}
        for r in ev:
            by_t[r["s"]] = by_t.get(r["s"], 0) + 1
        top = sorted(by_t.items(), key=lambda kv: -kv[1])[:8]
        out.append({**{k: R[k] for k in ("key", "name", "side")}, "cond": R["cond"], "events": len(ev),
                    "trainEvents": sum(1 for r in ev if r["d"] < cut), "testEvents": sum(1 for r in ev if r["d"] >= cut),
                    "grid": grid, "costs": costs, "months": [{"m": m, **_agg(v)} for m, v in sorted(months.items())],
                    "tickers": [{"s": k, "n": v} for k, v in top], "top5Share": round(sum(v for _, v in top[:5]) / len(ev) * 100) if ev else None})
    return {"rules": out, "costBp": cost_bp, "holds": list(HOLDS), "stops": list(STOPS)}


# ---------------------------------------------------------------- the daily top-up (after the close)
def daily_kick():
    """Every weekday after 16:30 ET: top up the alert history of every user who has one (only the new alerts)."""
    n = 0
    now = now_ny()
    if now.weekday() >= 5 or now.strftime("%H:%M") < "16:30":
        return 0
    for it in db.scan_sk(HIST_SK):
        if it.get("running") and it["running"] > iso(now - timedelta(minutes=30)):
            continue
        if (it.get("daily") or "") >= now.strftime("%Y-%m-%d") or not it.get("at"):
            continue
        sub = it["PK"][5:]
        db.put({**it, "daily": now.strftime("%Y-%m-%d")})
        hist_start(sub, {})
        n += 1
    return n
