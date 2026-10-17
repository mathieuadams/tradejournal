"""Intraday scenario study on 1-minute candles (QQQ / SPY or any day ticker): what happens after a gap, and when price
comes back to a level (previous day high / low / close, pre-market high / low, opening range, gamma walls, dark pool
levels), with the confluence of dark pool and gamma levels.

Per day: previous regular-session close / high / low, the open, the gap (% and x the 14-day ATR), whether the gap
filled and when, the day's direction and range. Per level and side (first touch of the day, from below or from above):
the reaction (rejected / broke and held), and the move after the touch at +5 / +15 / +30 / +60 minutes and at the
close, signed so that + means "through the level" (for a test from below: higher). Everything known at that time
only: levels of the previous day, pre-market, the opening range once it is complete, walls and dark pool levels of
the day (dark pool from the ~2 months before the day).
"""
import time
from datetime import datetime, timedelta

import db
from util import iso, now_ny

SK = "DTSTUDY"
FWD = (5, 15, 30, 60)
CONF = 0.0015          # a dark pool level within 0.15% of a gamma level = confluence


def _days(bars):
    by = {}
    for b in bars:
        by.setdefault(b["t"][:10], []).append(b)
    return by


def gap_bucket(g):
    return "gap up > 1%" if g > 1 else "gap up 0.25-1%" if g > 0.25 else "gap down > 1%" if g < -1 else "gap down 0.25-1%" if g < -0.25 else "flat (±0.25%)"


def study_ticker(bars, walls=None, dp_map=None, orb_min=15, start=None):
    """bars: 1-minute candles (pre-market included). Returns (days [...], events [...])."""
    walls, dp_map = walls or {}, dp_map or {}
    by = _days(bars)
    dates = sorted(by)
    reg = {d: [b for b in by[d] if "09:30" <= b["t"][11:16] < "16:00"] for d in dates}
    dates = [d for d in dates if len(reg[d]) > 200]
    days, events = [], []
    trs = []
    for k in range(1, len(dates)):
        d, p = dates[k], dates[k - 1]
        rb, pb = reg[d], reg[p]
        ph, pl, pc = max(b["h"] for b in pb), min(b["l"] for b in pb), pb[-1]["c"]
        trs.append(max(ph - pl, abs(ph - (reg[dates[k - 2]][-1]["c"] if k >= 2 else pc)), abs(pl - (reg[dates[k - 2]][-1]["c"] if k >= 2 else pc))))
        atr = sum(trs[-14:]) / len(trs[-14:])
        if start and d < start:
            continue
        o, c = rb[0]["o"], rb[-1]["c"]
        hi, lo = max(b["h"] for b in rb), min(b["l"] for b in rb)
        gap = (o / pc - 1) * 100
        fill_i = next((i for i, b in enumerate(rb) if (b["l"] <= pc if gap > 0 else b["h"] >= pc)), None) if abs(gap) > 0.05 else 0
        pm = [b for b in by[d] if "04:00" <= b["t"][11:16] < "09:30"]
        pmh, pml = (max(b["h"] for b in pm), min(b["l"] for b in pm)) if pm else (None, None)
        orb = rb[:orb_min]
        oh, ol = max(b["h"] for b in orb), min(b["l"] for b in orb)
        w = walls.get(d) or {}
        flip = float(w["flip"]) if w.get("flip") else None
        days.append({"d": d, "gap": round(gap, 3), "gapAtr": round((o - pc) / atr, 2) if atr else None, "bucket": gap_bucket(gap),
                     "filled": fill_i is not None, "fillMin": fill_i, "oc": round((c / o - 1) * 100, 3),
                     "upOpen": round((hi / o - 1) * 100, 3), "dnOpen": round((lo / o - 1) * 100, 3),
                     "closeVsPrev": "above prev high" if c > ph else "below prev low" if c < pl else "inside prev range",
                     "aboveFlip": (o > flip) if flip else None})
        gw = [("put wall", w.get("putWall")), ("flip", w.get("flip")), ("call wall", w.get("callWall"))]
        gw = [(n, float(v)) for n, v in gw if v]
        dps = [float(x) for x in (dp_map.get(d) or [])]
        levels = [("prev high", ph, 0), ("prev low", pl, 0), ("prev close", pc, 0), ("opening range high", oh, orb_min),
                  ("opening range low", ol, orb_min)]
        if pmh is not None:
            levels += [("pre-market high", pmh, 0), ("pre-market low", pml, 0)]
        levels += [(n, v, 0) for n, v in gw]
        levels += [("dark pool", v, 0) for v in dps]
        for name, L, from_i in levels:
            conf = None
            if name == "dark pool":
                conf = next((gn for gn, gv in gw if abs(gv - L) / L <= CONF), None)
            elif name in ("put wall", "flip", "call wall"):
                conf = "dark pool" if any(abs(x - L) / L <= CONF for x in dps) else None
            for i in range(max(from_i, 1), len(rb)):
                b, pv = rb[i], rb[i - 1]["c"]
                side = 1 if pv < L <= b["h"] else -1 if pv > L >= b["l"] else 0
                if not side:
                    continue
                after = rb[i:]
                def fwd(m):
                    return round((after[min(m, len(after) - 1)]["c"] / L - 1) * 100 * side, 3)
                beyond = [x["c"] for x in after[1:31]]
                held = len(beyond) >= 15 and all((x - L) * side > 0 for x in beyond[14:30]) and any((x - L) * side > L * 0.0005 for x in beyond[:15])
                mfe = max(((x["h"] if side > 0 else -x["l"]) for x in after[:31]), default=0)
                mae = min(((x["l"] if side > 0 else -x["h"]) for x in after[:31]), default=0)
                events.append({"d": d, "t": b["t"], "level": name, "price": round(L, 4), "side": "from below" if side > 0 else "from above",
                               "bucket": days[-1]["bucket"], "conf": conf, "held": bool(held),
                               "f5": fwd(5), "f15": fwd(15), "f30": fwd(30), "f60": fwd(60), "fc": fwd(len(after) - 1),
                               "mfe30": round(((mfe if side > 0 else -mfe) / L - 1) * 100 * side, 3),
                               "mae30": round(((mae if side > 0 else -mae) / L - 1) * 100 * side, 3),
                               "minute": i})
                break                                   # first touch of the day for this level
    return days, events


def _agg(evs):
    n = len(evs)
    if not n:
        return {"n": 0}
    av = lambda k: round(sum(e[k] for e in evs) / n, 3)
    pos = lambda k: round(sum(1 for e in evs if e[k] > 0) / n * 100, 1)
    return {"n": n, "held": round(sum(1 for e in evs if e["held"]) / n * 100, 1),
            "f5": av("f5"), "f15": av("f15"), "f30": av("f30"), "f60": av("f60"), "fc": av("fc"),
            "p15": pos("f15"), "p60": pos("f60"), "mfe30": av("mfe30"), "mae30": av("mae30"),
            "minute": round(sum(e["minute"] for e in evs) / n)}


def summarize(days, events):
    gaps = {}
    for b in sorted({x["bucket"] for x in days}):
        ds = [x for x in days if x["bucket"] == b]
        n = len(ds)
        fl = [x for x in ds if x["filled"]]
        mins = sorted(x["fillMin"] for x in fl if x["fillMin"] is not None)
        gaps[b] = {"n": n, "filled": round(len(fl) / n * 100, 1), "fillMedMin": mins[len(mins) // 2] if mins else None,
                   "greenClose": round(sum(1 for x in ds if x["oc"] > 0) / n * 100, 1),
                   "oc": round(sum(x["oc"] for x in ds) / n, 3), "upOpen": round(sum(x["upOpen"] for x in ds) / n, 3),
                   "dnOpen": round(sum(x["dnOpen"] for x in ds) / n, 3),
                   "abovePH": round(sum(1 for x in ds if x["closeVsPrev"] == "above prev high") / n * 100, 1),
                   "belowPL": round(sum(1 for x in ds if x["closeVsPrev"] == "below prev low") / n * 100, 1)}
    key = lambda e: f"{e['level']} · {e['side']}"
    levels = {k: _agg([e for e in events if key(e) == k]) for k in sorted({key(e) for e in events})}
    by_gap = {}
    for b in sorted({e["bucket"] for e in events}):
        by_gap[b] = {k: _agg([e for e in events if e["bucket"] == b and key(e) == k]) for k in sorted({key(e) for e in events if e["bucket"] == b})}
    conf = {}
    for lv in ("dark pool", "put wall", "flip", "call wall"):
        for side in ("from below", "from above"):
            ev = [e for e in events if e["level"] == lv and e["side"] == side]
            if ev:
                conf[f"{lv} · {side}"] = {"with": _agg([e for e in ev if e["conf"]]), "without": _agg([e for e in ev if not e["conf"]])}
    flip_gap = {}
    for x in days:
        if x["aboveFlip"] is None:
            continue
        k = f"{'above' if x['aboveFlip'] else 'below'} the flip at the open · {x['bucket']}"
        flip_gap.setdefault(k, []).append(x)
    flip = {k: {"n": len(v), "greenClose": round(sum(1 for x in v if x["oc"] > 0) / len(v) * 100, 1),
                "oc": round(sum(x["oc"] for x in v) / len(v), 3)} for k, v in sorted(flip_gap.items())}
    return {"gaps": gaps, "levels": levels, "byGap": by_gap, "confluence": conf, "flipGap": flip}


def run(sub, params=None, budget_s=540):
    import backtest
    import daytrade
    import flowdata
    import wallwatch
    t0 = time.time()
    p = params or {}
    cfg = daytrade.config(sub)
    days_n = max(10, min(500, int(p.get("days") or 120)))
    tickers = [t for t in (p.get("tickers") or cfg["tickers"])][:6]
    all_days, all_ev, errors, feed = [], [], [], {}
    near_used = bool(p.get("wallNear", cfg.get("wallNear")))
    for sym in tickers:
        if time.time() - t0 > budget_s:
            errors.append(f"{sym}: time limit")
            continue
        try:
            try:
                bars, feed[sym] = daytrade.minute_bars(sub, sym, days_n + 2, feed="sip"), "sip"
            except Exception:
                bars, feed[sym] = daytrade.minute_bars(sub, sym, days_n + 2, feed="iex"), "iex"
            dates = sorted({b["t"][:10] for b in bars})[-days_n:]
            near = bool(p["wallNear"]) if "wallNear" in p else bool(cfg.get("wallNear"))
            try:
                walls = daytrade.walls_by_day(sub, sym, dates, near, budget_s=max(20, (budget_s - (time.time() - t0)) / 2)) if near else flowdata.stored_walls(sym)
            except Exception:
                walls = {}
            try:
                dpm = wallwatch.dp_level_map(backtest.stored_darkpool(sym), dates)
            except Exception:
                dpm = {}
            ds, ev = study_ticker(bars, walls, dpm, int(p.get("orbMin") or cfg.get("orbMin") or 15), dates[0] if dates else None)
            del bars
            for x in ds:
                x["sym"] = sym
            for e in ev:
                e["sym"] = sym
            all_days += ds
            all_ev += ev
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:120]}")
    out = {"at": iso(now_ny()), "days": days_n, "tickers": tickers, "wallNear": near_used, "feed": feed, "nDays": len(all_days),
           "nEvents": len(all_ev), "wallDays": len({e["d"] for e in all_ev if e["level"] in ("put wall", "flip", "call wall")}),
           "dpDays": len({e["d"] for e in all_ev if e["level"] == "dark pool"}),
           **summarize(all_days, all_ev), "recentDays": sorted(all_days, key=lambda x: x["d"])[-40:],
           "secs": round(time.time() - t0), "errors": errors}
    db.put({"PK": db.upk(sub), "SK": SK, **out, "running": None})
    return out


def result(sub):
    r = db.get(db.upk(sub), SK) or {}
    if r.get("running") and r["running"] < iso(now_ny() - timedelta(minutes=12)):
        r = {**r, "running": None, "errors": ["The last run hit the 10-minute limit: try fewer days."]}
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}
