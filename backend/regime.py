"""Market regimes: label every trading day, measure how long each regime lasts, and how the market, the sectors and the
main watchlist move in each one.

The labels (no look-ahead: a day's label uses only the closes up to that day):
  * trend of SPY: Up = close > EMA50 > EMA200 and the EMA50 rising over 20 days; Down = the mirror; else Range;
  * volatility: Calm = VIX < 20, Volatile = VIX >= 20; stress = VIX above VIX3M (the curve inverted);
  * a new label only counts after CONFIRM days in a row (no flip-flopping);
  * the regime = trend x volatility (6 regimes).
How each asset moves in a regime: the NEXT day's return (close of the labeled day -> next close), so the figures are
what a trader knowing the regime at the close would have seen."""
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import db
from util import iso, now_ny

SK = "REGIME"
CONFIRM = 3
VIX_HI = 20.0
MARKET = [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("IWM", "Russell 2000 (RTY)")]
SECTORS = [("XLK", "Technology"), ("XLC", "Communication"), ("XLY", "Consumer discr."), ("XLF", "Financials"), ("XLV", "Health care"),
           ("XLI", "Industrials"), ("XLE", "Energy"), ("XLB", "Materials"), ("XLP", "Consumer staples"), ("XLU", "Utilities"), ("XLRE", "Real estate"),
           ("SMH", "Semiconductors")]
HORIZONS = (5, 10, 20)                                 # forward returns, in trading days
TRENDS = ("Up", "Range", "Down")
VOLS = ("Calm", "Volatile")
LABELS = [f"{t} · {v}" for t in TRENDS for v in VOLS]


def _daily(sym, years):
    from charts import _yahoo
    now = now_ny().replace(tzinfo=None)
    raw = _yahoo(sym, "1d", now - timedelta(days=int(years * 365.25) + 320), now + timedelta(days=1)) or []
    today = now.strftime("%Y-%m-%d")
    out = {}
    for b in raw:
        d = b["t"][:10]
        if d < today or now.strftime("%H:%M") >= "16:05":
            out[d] = b["c"]
    return out


def _ema(xs, n):
    out, e, k = [], None, 2 / (n + 1)
    for i, x in enumerate(xs):
        if i + 1 < n:
            out.append(None)
            continue
        e = sum(xs[i + 1 - n:i + 1]) / n if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def _confirm(raw, n=CONFIRM):
    """The label in force each day: it changes only after n days in a row of the new raw label."""
    out, cur, cand, run = [], None, None, 0
    for x in raw:
        if x is None:
            out.append(cur)
            continue
        if cur is None:
            cur = x
        elif x == cur:
            cand, run = None, 0
        else:
            run = run + 1 if x == cand else 1
            cand = x
            if run >= n:
                cur, cand, run = x, None, 0
        out.append(cur)
    return out


def label_days(spy, vix, vix3m=None):
    """{date: {trend, vol, label, stress, vix}} for every day with enough history. spy / vix: {date: close}."""
    days = sorted(spy)
    c = [spy[d] for d in days]
    e50, e200 = _ema(c, 50), _ema(c, 200)
    raw_t, raw_v = [], []
    last_vix = None
    for i, d in enumerate(days):
        if e200[i] is None or i < 20 or e50[i - 20] is None:
            raw_t.append(None)
        else:
            up = e50[i] > e50[i - 20]
            raw_t.append("Up" if c[i] > e50[i] > e200[i] and up else "Down" if c[i] < e50[i] < e200[i] and not up else "Range")
        v = vix.get(d, last_vix)
        last_vix = v
        raw_v.append(None if v is None else "Volatile" if v >= VIX_HI else "Calm")
    tr, vo = _confirm(raw_t), _confirm(raw_v)
    out = {}
    for i, d in enumerate(days):
        if tr[i] is None or vo[i] is None:
            continue
        v3 = (vix3m or {}).get(d)
        out[d] = {"trend": tr[i], "vol": vo[i], "label": f"{tr[i]} · {vo[i]}", "vix": vix.get(d),
                  "stress": bool(v3 and vix.get(d) and vix[d] > v3)}
    return out


def segments(days, lab, key="label"):
    """[(label, start, end, n days)] of consecutive days with the same label."""
    out = []
    for d in days:
        x = lab[d][key]
        if out and out[-1][0] == x:
            out[-1][2], out[-1][3] = d, out[-1][3] + 1
        else:
            out.append([x, d, d, 1])
    return out


def durations(segs, total, last_open=True):
    """Per label: how many times, how long (mean / median / quartiles / max in trading days), the share of the days,
    and the chance it is still on after 5 / 10 / 20 / 40 days. The last (still running) segment counts in the share
    but not in the lengths."""
    by = {}
    for k, s in enumerate(segs):
        b = by.setdefault(s[0], {"lens": [], "days": 0})
        b["days"] += s[3]
        if not (last_open and k == len(segs) - 1):
            b["lens"].append(s[3])
    out = {}
    for l, b in by.items():
        xs = sorted(b["lens"])
        q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))] if xs else None
        out[l] = {"n": len(xs), "mean": round(sum(xs) / len(xs), 1) if xs else None, "median": q(0.5), "p25": q(0.25), "p75": q(0.75),
                  "max": xs[-1] if xs else None, "share": round(b["days"] / total * 100, 1) if total else None,
                  "surv": {str(k): round(sum(1 for x in xs if x > k) / len(xs) * 100) if xs else None for k in (5, 10, 20, 40)}}
    return out


def transitions(segs):
    out = {}
    for a, b in zip(segs, segs[1:]):
        out.setdefault(a[0], {})
        out[a[0]][b[0]] = out[a[0]].get(b[0], 0) + 1
    return out


def asset_stats(px, spy, days, lab):
    """Per regime: the next-day returns of the asset (close of a labeled day -> the next close). avg / annualized %,
    up days %, compounded %, excess vs SPY (avg per day, annualized) and beta to SPY in that regime."""
    rows, fwd = {}, {}
    ds = [d for d in days if d in px]
    for i, (a, b) in enumerate(zip(ds, ds[1:])):
        if a not in lab or b not in spy or a not in spy:
            continue
        r, m = px[b] / px[a] - 1, spy[b] / spy[a] - 1
        ls = (lab[a]["label"], lab[a]["trend"], lab[a]["vol"], "All") + (("Stress",) if lab[a]["stress"] else ())
        for l in ls:
            rows.setdefault(l, []).append((r, m))
        for k, h in enumerate(HORIZONS):              # the move over the next h trading days (windows overlap)
            if i + h < len(ds) and ds[i + h] in spy:
                e = ds[i + h]
                fr, fm = px[e] / px[a] - 1, spy[e] / spy[a] - 1
                for l in ls:
                    fwd.setdefault(l, [[] for _ in HORIZONS])[k].append((fr, fm))
    out = {}
    for l, xs in rows.items():
        n = len(xs)
        rs, ms = [x[0] for x in xs], [x[1] for x in xs]
        mr, mm = sum(rs) / n, sum(ms) / n
        var = sum((x - mm) ** 2 for x in ms) / max(1, n - 1)
        cov = sum((x - mr) * (y - mm) for x, y in zip(rs, ms)) / max(1, n - 1)
        cum = 1.0
        for x in rs:
            cum *= 1 + x
        sd = math.sqrt(sum((x - mr) ** 2 for x in rs) / max(1, n - 1))
        out[l] = {"n": n, "avg": round(mr * 100, 3), "ann": round(mr * 252 * 100, 1), "up": round(sum(1 for x in rs if x > 0) / n * 100),
                  "cum": round((cum - 1) * 100, 1), "ex": round((mr - mm) * 252 * 100, 1), "beta": round(cov / var, 2) if var > 0 else None,
                  "vol": round(sd * math.sqrt(252) * 100, 1)}
        fs = fwd.get(l) or [[] for _ in HORIZONS]      # forward: avg %, % of windows up, avg % vs SPY
        out[l]["f"] = [round(sum(x[0] for x in w) / len(w) * 100, 2) if w else None for w in fs]
        out[l]["fw"] = [round(sum(1 for x in w if x[0] > 0) / len(w) * 100) if w else None for w in fs]
        out[l]["fx"] = [round(sum(x[0] - x[1] for x in w) / len(w) * 100, 2) if w else None for w in fs]
    return out


# ---------------------------------------------------------------- the study (background job, stored)
def public(sub, macro=True):
    it = db.get(db.upk(sub), SK) or {}
    out = json.loads(it["data"]) if it.get("data") else {}
    if macro:
        for k_, sk_ in (("macro", "#MACRO"), ("rank", "#RANK")):
            m = db.get(db.upk(sub), SK + sk_) or {}
            if m.get("data"):
                out[k_] = json.loads(m["data"])
    if out.get("running") and out["running"] < iso(now_ny() - timedelta(minutes=12)):
        out["running"] = None
    return out


def _save(sub, x):
    data = json.dumps(x, default=str, separators=(",", ":"))
    if len(data) > 380_000:                            # DynamoDB item limit: the less used figures go first
        for a in x.get("assets") or []:
            for v in (a.get("by") or {}).values():
                v.pop("vol", None)
                v.pop("cum", None)
        data = json.dumps(x, default=str, separators=(",", ":"))
    db.put({"PK": db.upk(sub), "SK": SK, "data": data})


def lists(sub):
    import wallwatch
    return [{"name": x["name"], "n": len(x["tickers"])} for x in wallwatch.config(sub)["lists"] if x["tickers"]]


def start(sub, b):
    b = b or {}
    cur = public(sub, macro=False)
    if cur.get("running"):
        return cur
    x = {**{k: cur.get(k) for k in ("at", "list", "years")}, "running": iso(now_ny()), "step": "starting",
         "list": str(b.get("list") or cur.get("list") or "Main")[:60], "years": max(2, min(20, int(b.get("years") or cur.get("years") or 10)))}
    if cur.get("at"):                                  # the previous result stays on the page while the new one runs
        x = {**cur, **x}
    _save(sub, x)
    import research
    try:
        research._invoke(sub, "regime", {})
    except Exception as e:
        x.update(running=None, step=None, error=f"couldn't start: {str(e)[:120]}")
        _save(sub, x)
    return x


def run(sub):
    import macro
    import wallwatch
    x = public(sub, macro=False)
    years, lname = int(x.get("years") or 10), x.get("list") or "Main"
    lst = next((L for L in wallwatch.config(sub)["lists"] if L["name"] == lname), None) or wallwatch.config(sub)["lists"][0]
    stocks = [s for s in lst["tickers"] if s not in {m for m, _ in MARKET + SECTORS}]
    syms = [m for m, _ in MARKET + SECTORS] + stocks + ["^VIX", "^VIX3M"] + [m for m in macro.SYMS if m not in {a for a, _ in MARKET + SECTORS}]
    x.update(step=f"loading {len(syms)} daily histories", list=lst["name"])
    _save(sub, x)
    data, errs = {}, []

    def one(s):
        try:
            return s, _daily(s.replace(".", "-") if not s.startswith("^") else s, years)
        except Exception:
            time.sleep(5)
            return s, _daily(s.replace(".", "-") if not s.startswith("^") else s, years)
    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in [ex.submit(one, s) for s in syms]:
            try:
                s, d = f.result(timeout=400)
                if d:
                    data[s] = d
            except Exception as e:
                errs.append(str(e)[:80])
    spy, vix = data.get("SPY"), data.get("^VIX")
    if not spy or not vix:
        raise ValueError("SPY or VIX history couldn't be loaded.")
    lab = label_days(spy, vix, data.get("^VIX3M"))
    first = (now_ny() - timedelta(days=int(years * 365.25))).strftime("%Y-%m-%d")
    days = [d for d in sorted(lab) if d >= first]
    lab = {d: lab[d] for d in days}
    segs = segments(days, lab)
    st_segs = [s for s in segments(days, {d: {"s": "Stress" if lab[d]["stress"] else "-"} for d in days}, "s") if s[0] == "Stress"]
    spy_seg = lambda s: round((spy[s[2]] / spy[s[1]] - 1) * 100, 1) if s[1] in spy and s[2] in spy else None
    names = dict(MARKET + SECTORS)
    assets = []
    for s in [m for m, _ in MARKET] + [m for m, _ in SECTORS] + stocks:
        if s not in data:
            continue
        kind = "market" if s in dict(MARKET) else "sector" if s in dict(SECTORS) else "stock"
        assets.append({"sym": s, "name": names.get(s, ""), "kind": kind, "from": min(data[s]), "by": asset_stats(data[s], spy, days, lab)})
    cur = lab[days[-1]]
    since = segs[-1][1]
    step = max(1, len(days) // 900)
    x.update(running=None, step=None, at=iso(now_ny()), error=None, errors=errs[:5], labels=LABELS, confirm=CONFIRM, vixHi=VIX_HI,
             span={"from": days[0], "to": days[-1], "days": len(days)},
             current={**cur, "since": since, "days": segs[-1][3], "trendDays": segments(days, lab, "trend")[-1][3],
                      "volDays": segments(days, lab, "vol")[-1][3]},
             segments=[{"l": s[0], "s": s[1], "e": s[2], "d": s[3], "spy": spy_seg(s)} for s in segs],
             dur={"regime": durations(segs, len(days)), "trend": durations(segments(days, lab, "trend"), len(days)),
                  "vol": durations(segments(days, lab, "vol"), len(days)),
                  "stress": durations([[s[0], s[1], s[2], s[3]] for s in st_segs], len(days), last_open=bool(lab[days[-1]]["stress"]))},
             trans=transitions(segs),
             spy=[[d, round(spy[d], 2), LABELS.index(lab[d]["label"]), 1 if lab[d]["stress"] else 0] for d in days[::step]] + ([[days[-1], round(spy[days[-1]], 2), LABELS.index(cur["label"]), 1 if cur["stress"] else 0]] if (len(days) - 1) % step else []),
             assets=assets, nStocks=len([a for a in assets if a["kind"] == "stock"]))
    x["step"] = "bonds, rates, credit and the early warnings"
    try:
        import flowdata
        gw = flowdata.stored_walls("SPY")
    except Exception:
        gw = None
    try:
        m = macro.study(data, days, lab, [s for s in stocks if s in data], gw)
        db.put({"PK": db.upk(sub), "SK": SK + "#MACRO", "data": json.dumps(m, default=str, separators=(",", ":"))})
    except Exception as e:
        x["errors"] = (x.get("errors") or []) + [f"market awareness: {str(e)[:120]}"]
    try:
        import rank
        al = {k: macro.align(v, days) for k, v in data.items()}
        rk = rank.study(al, days, {"index": [m for m, _ in MARKET], "sector": [m for m, _ in SECTORS], "stock": [s for s in stocks if s in data]},
                        dict(MARKET + SECTORS), al.get("^IRX"), al.get("^VIX"))
        db.put({"PK": db.upk(sub), "SK": SK + "#RANK", "data": json.dumps(rk, default=str, separators=(",", ":"))})
    except Exception as e:
        x["errors"] = (x.get("errors") or []) + [f"weekly ranking: {str(e)[:120]}"]
    x["step"] = None
    _save(sub, x)
    return x


def job(sub, params=None):
    try:
        return {"days": (run(sub).get("span") or {}).get("days")}
    except Exception as e:
        x = public(sub)
        x.update(running=None, step=None, error=str(e)[:200], at=x.get("at"))
        _save(sub, x)
        return {"error": str(e)[:200]}
