"""Strategies and the validation protocol: Data -> Study -> Validation -> Paper -> Live.

Item (user partition) STRAT#<id>: name, setup, params, costs, the study / validation summaries, the approved market
conditions and the paper state. The trades themselves are files: strat/<sub>/<id>/study and .../paper.

Money: points x point value ($20 NQ, $50 ES) x contracts, minus commission and slippage on both sides.
R: points / the trade's stop distance. Edge: average points vs the same bracket entered at random times."""
import math
import statistics
import uuid
from collections import defaultdict

import db
import futdata
import orb
import orbema
import range50
import silver
import ibbreak
import odpull
import biaspull
import flatrev
import ldnbrk
import ondrift
import postclose
import smt
import sungap
import fpdisp
import ifvg
import imom
import onrev
import gapfill
import onmid
import onhold
import tom
import vwapband
import rsi2
import squeeze
import fxstrat
import fxvariants
import stockdata
import volclimax
import pwsweep
import trendday
import insidebar
import nrbreak
import onsweep
import pdbreak
import pdsweep
import tvpoc
import vwapret
import store
import wtswing
from util import BadRequest, NotFound, iso, now_ny

SETUPS = {"orb": orb, "orbema": orbema, "range50": range50, "silver": silver, "tvpoc": tvpoc, "vwapret": vwapret, "wt": wtswing, "onsweep": onsweep, "ibbreak": ibbreak, "odpull": odpull, "pdbreak": pdbreak, "pdsweep": pdsweep, "nrbreak": nrbreak, "fpdisp": fpdisp, "ifvg": ifvg, "biaspull": biaspull, "ondrift": ondrift, "ldnbrk": ldnbrk, "smt": smt, "flatrev": flatrev, "postclose": postclose, "sungap": sungap, "insidebar": insidebar, "imom": imom, "onrev": onrev, "gapfill": gapfill, "onmid": onmid, "onhold": onhold, "tom": tom, "vwapband": vwapband, "rsi2": rsi2, "squeeze": squeeze, "volclimax": volclimax, "pwsweep": pwsweep, "trendday": trendday, "fractal": fxstrat, **fxvariants.MODULES}
NAMES = {"orb": "Opening range break + FVG retest", "orbema": "Opening range break + 20 EMA retest", "range50": "Range 50% retrace", "silver": "ICT Silver Bullet", "tvpoc": "Trend volume profile + rolling POC", "vwapret": "VWAP retest (overnight included)", "onsweep": "Overnight high / low sweep and reclaim", "ibbreak": "Initial balance breakout", "odpull": "Opening drive, first pullback", "wt": "WaveTrend dots", "pdbreak": "Prior-day high / low break and retest", "pdsweep": "Prior-day high / low sweep and reclaim", "nrbreak": "Narrow-range day breakout", "fpdisp": "Fair price displacement", "ifvg": "FVG inversion with the EMA bias", "biaspull": "EMA bias pullback (no FVG control)", "ondrift": "Overnight drift (hold with the bias)", "ldnbrk": "London breakout of the Asian range", "smt": "ES / NQ divergence (SMT)", "flatrev": "Flat-bias mean reversion to VWAP", "postclose": "Post-close continuation (16:00-17:00)", "sungap": "Sunday / reopen gap", "insidebar": "Inside bar breakout", "imom": "Intraday momentum (first → last half hour)", "onrev": "Overnight → day reversal", "gapfill": "Regular-hours gap fill", "onmid": "Overnight range midpoint → overnight high / low", "onhold": "Overnight half → hold to a set time (no target)", "tom": "Turn of the month", "vwapband": "VWAP 2σ band fade", "rsi2": "RSI(2) pullback with the trend (hourly)", "squeeze": "Squeeze breakout", "volclimax": "Volume climax reversal", "pwsweep": "Prior-week high / low sweep", "trendday": "Trend-day hold", "fractal": "Fractal (TradeWellX): pivot cross, level fade, wave count", **fxvariants.NAMES}
SK = "STRAT#"
STOCK_SETUPS = ("fractal", "smfade", "oaride", "latewave")                            # the setups that also run on QQQ / SPY (Alpaca candles)
COSTS = {"contracts": 1, "comm": 2.25, "slip": 1.0, "dailyLoss": 500.0}
MIN_TRADES, PF_MIN, MONTH_MAX, TRAIN = 100, 1.3, 0.4, 0.6
PHASES, VOLS = ("Bull", "Sideways", "Bear"), ("calm", "volatile")


def _pk(sub):
    return db.upk(sub)


def _mod(setup):
    if setup not in SETUPS:
        raise BadRequest("Unknown setup.")
    return SETUPS[setup]


def _params(setup, params):
    p = _mod(setup).clean(params or {})
    ok = futdata.SYMS + (stockdata.SYMS if setup in STOCK_SETUPS else ())
    p["tickers"] = [t for t in p.get("tickers") or [] if t in ok] or ["NQ"]
    if any(stockdata.is_stock(t) for t in p["tickers"]):
        p["tickers"] = [t for t in p["tickers"] if stockdata.is_stock(t)]     # stocks and futures are not mixed
        if "hours" in p:
            p["hours"] = "rth"                         # regular hours only for the stocks
    return p


def _num(v, lo, hi, name):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number.")
    if not lo <= f <= hi:
        raise BadRequest(f"{name} must be between {lo:g} and {hi:g}.")
    return f


def _fresh(s):
    """A stored strategy with its rules completed by the setup's current defaults (rules added since it was saved)."""
    try:
        s["params"] = _params(s["setup"], s.get("params") or {})
    except Exception as e:
        print("params refresh failed", s.get("id"), e)
    if s.get("setup") == "ifvg" and (s.get("migIfvg") or (1 if s.get("migAsia") else 0)) < 2 and not (s.get("paper") or {}).get("on") and "PK" in s:
        try:                                           # once: the FVG inversion as asked: EMA bias only, overnight + NYSE, managed exit
            s["params"].update(asia="none", hours="all", exit="trail")
            s.update(migAsia=True, migIfvg=2, stage="idea", study=None, valid=None, cells={})
            db.put(s)
        except Exception as e:
            print("ifvg migration failed", s.get("id"), e)
    return s


def _all(sub):
    return [_fresh(s) for s in db.q_prefix(_pk(sub), SK)]


def get(sub, sid):
    s = db.get(_pk(sub), SK + sid)
    if not s:
        raise NotFound("No such strategy.")
    return _fresh(s)


def _view(s):
    s = {k: v for k, v in s.items() if k not in ("PK", "SK")}
    try:
        s["describe"] = _mod(s["setup"]).describe(s["params"])
    except Exception as e:                             # one odd strategy never breaks the whole list
        print("describe failed", s.get("id"), e)
        s["describe"] = ""
    s["setupName"] = NAMES.get(s["setup"], s["setup"])
    return s


def listing(sub):
    return [_view(s) for s in _all(sub)]


def view(sub, sid):
    v = _view(get(sub, sid))
    v["coverage"] = {t: _coverage(t) for t in v["params"]["tickers"]}
    return v


def create(sub, b):
    setup = b.get("setup")
    p = _params(setup, b.get("params"))
    sid = uuid.uuid4().hex[:10]
    s = {"PK": _pk(sub), "SK": SK + sid, "id": sid, "setup": setup, "name": str(b.get("name") or NAMES[setup])[:80],
         "params": p, **COSTS, "stage": "idea", "cells": {}, "created": iso(now_ny()), "paper": {"on": False}, "migAsia": True, "migIfvg": 2}
    db.put(s)
    return _view(s)


def update(sub, sid, b):
    s = get(sub, sid)
    if "name" in b:
        s["name"] = str(b["name"] or s["name"]).strip()[:80] or s["name"]
    if "params" in b:
        if s.get("paper", {}).get("on"):
            raise BadRequest("Stop paper trading before changing the rules.")
        s["params"] = _params(s["setup"], b["params"])
        s.update(stage="idea", study=None, valid=None, cells={})
    if "contracts" in b:
        s["contracts"] = int(_num(b["contracts"], 1, 50, "Contracts"))
    if b.get("edgeTest") in ("timing", "rule"):
        s["edgeTest"] = b["edgeTest"]
    if "comm" in b:
        s["comm"] = _num(b["comm"], 0, 50, "Commission")
    if "slip" in b:
        s["slip"] = _num(b["slip"], 0, 20, "Slippage")
    if "dailyLoss" in b:
        s["dailyLoss"] = _num(b["dailyLoss"], 0, 1e6, "Daily loss limit")
    if isinstance(b.get("cells"), dict):
        for k, v in b["cells"].items():
            if k.count("|") == 1 and k.split("|")[0] in PHASES and k.split("|")[1] in VOLS:
                s["cells"][k] = bool(v)
    db.put(s)
    return _view(s)


def delete(sub, sid):
    s = get(sub, sid)
    if s.get("paper", {}).get("on"):
        raise BadRequest("Stop paper trading first.")
    db.delete(_pk(sub), SK + sid)
    db.delete(_pk(sub), "SEARCH#" + sid)
    for k in ("study", "paper", "opt", "search"):
        store.delete(f"strat/{sub}/{sid}/{k}")
    return {"ok": True}


# ---------------------------------------------------------------- trades and metrics

def _cost(s, sym):
    sp = futdata.SPEC[sym]
    comm = sp["comm"] if "comm" in sp else s.get("comm", COSTS["comm"])
    return (2 * comm + 2 * s.get("slip", COSTS["slip"]) * sp["tick"] * sp["pv"]) * s.get("contracts", 1)


def _normal(s, x, bl, cellmap):
    sym = x.get("s") or s["params"]["tickers"][0]
    sp, c = futdata.SPEC[sym], s.get("contracts", 1)
    d = 1 if x["side"] == "long" else -1
    tk = sp["tick"]
    e, xx = round(x["e"] / tk) * tk, round(x["x"] / tk) * tk     # orders fill on the tick grid
    pts = d * (xx - e)
    risk = x.get("stopDist") or (abs(x["e"] - x["stop"]) if x.get("stop") is not None else None)
    bp = [v / 100 * x["e"] for v in bl] if bl else []
    t = {"s": sym, "d": x["d"], "side": x["side"], "in": x["in"], "out": x["out"], "e": e, "x": xx,
         "why": x.get("why"), "pts": round(pts, 2), "r": round(pts / risk, 3) if risk else None, "risk": round(risk, 2) if risk else None,
         "gross": round(pts * sp["pv"] * c, 2), "cost": round(_cost(s, sym), 2), "bp": round(sum(bp) / len(bp), 3) if bp else None,
         "cell": (cellmap.get(sym) or {}).get(x["d"]), "c": c}
    t["net"] = round(t["gross"] - t["cost"], 2)
    for k in ("stop", "tgt", "rhi", "rlo", "fvgBot", "fvgTop", "sweepLvl"):
        if x.get(k) is not None:
            t[k] = round(x[k], 2)
    for k in ("bk", "rt", "cf"):                       # when the setup's steps happened (drawn on the chart)
        if x.get(k):
            t[k] = x[k]
    if x.get("lv"):
        t["lv"] = {k: round(v, 2) for k, v in x["lv"].items()}
    return t


def metrics(ts, cm=1.0, base=False):
    """n, win %, avg R, profit factor, expectancy, total, max drawdown, points, edge vs random. cm: cost multiplier.
    base: the random-entry baseline's numbers (its average points in place of the trade's)."""
    ts = [t for t in ts if t.get("why") != "open"]
    if not ts:
        return {"n": 0}
    nets = []
    for t in ts:
        pv = futdata.SPEC[t["s"]]["pv"]
        g = (t["bp"] or 0) * pv * t.get("c", 1) if base else t["gross"]
        nets.append(g - t["cost"] * cm)
    pos, neg = sum(v for v in nets if v > 0), -sum(v for v in nets if v < 0)
    eq = peak = dd = 0.0
    for v in nets:
        eq += v
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    rs = [t["r"] for t in ts if t.get("r") is not None]
    pts = [(t["bp"] or 0) if base else t["pts"] for t in ts]
    bps = [t["bp"] for t in ts if t.get("bp") is not None]
    out = {"n": len(ts), "win": round(sum(1 for v in nets if v > 0) / len(ts) * 100, 1), "pf": round(pos / neg, 2) if neg else None,
           "exp": round(sum(nets) / len(ts), 2), "total": round(sum(nets), 2), "dd": round(dd, 2), "pts": round(sum(pts) / len(pts), 2)}
    if not base:
        out["avgR"] = round(sum(rs) / len(rs), 3) if rs else None
        out["sdR"] = round(statistics.pstdev(rs), 3) if len(rs) > 1 else None
        out["edge"] = round(sum(t["pts"] for t in ts) / len(ts) - sum(bps) / len(bps), 2) if bps else None
        b2 = [t["bp2"] for t in ts if t.get("bp2") is not None]
        if b2:
            out["edge2"] = round(sum(t["pts"] for t in ts) / len(ts) - sum(b2) / len(b2), 2)
    return out


def _factor_table(ts, keys):
    out = {}
    for k, label in keys:
        g = defaultdict(list)
        for t in ts:
            if t.get(k) is not None:
                g[str(t[k])].append(t)
        if len(g) > 1:
            out[label] = sorted(({"v": v, **{q: m.get(q) for q in ("n", "avgR", "exp", "win")}} for v, xs in g.items()
                                 for m in [metrics(xs)]), key=lambda r: -r["n"])
    return out


# ---------------------------------------------------------------- protocol

def _coverage(t):
    return stockdata.coverage(t) if stockdata.is_stock(t) else futdata.coverage()[t]


def _cells(t):
    return stockdata.cells(t) if stockdata.is_stock(t) else futdata.cells(t)


def _days(t):
    return stockdata.trading_days(t) if stockdata.is_stock(t) else futdata.trading_days(t)


def data_check(sub, sid, fetch=True):
    """fetch=False (the API, 30 s limit): QQQ / SPY count as ready; the job downloads them (data_check with fetch)."""
    s = get(sub, sid)
    out = []
    for t in s["params"]["tickers"]:
        if stockdata.is_stock(t):                      # QQQ / SPY: fetched from Alpaca and stored on first use
            if not fetch:
                out.append({"s": t, "days": None, "from": None, "to": None, "ok": True})
                continue
            stockdata.ensure(sub, t, int(s["params"].get("days") or 500))
        c = _coverage(t)
        out.append({"s": t, "days": c["days"], "from": c["from"], "to": c["to"], "ok": c["days"] >= 60})
    return out


def run_trades(sub, s, start=None, days=None, prog=None):
    mod = _mod(s["setup"])
    p = dict(s["params"])
    if start:
        p["_start"] = start
    if days:
        p["days"] = days
    cellmap = {t: _cells(t) for t in p["tickers"]}
    trades, base = mod.all_trades(sub, p, None, prog)
    out = []
    for x, b in zip(trades, base):
        t = _normal(s, x, b[2] if isinstance(b, (list, tuple)) and len(b) > 2 else [], cellmap)
        if isinstance(b, (list, tuple)) and len(b) > 3 and b[3]:   # random entries on a random side (the whole rule's baseline)
            t["bp2"] = round(sum(v / 100 * x["e"] for v in b[3]) / len(b[3]), 3)
        if hasattr(mod, "decorate"):
            try:
                y = dict(x)
                mod.decorate(y, p)
                for k, _ in getattr(mod, "FACTORS", []):
                    if k in y and k not in t and not isinstance(y[k], (dict, list)):
                        t[k] = y[k]
            except Exception as e:
                print("decorate failed", e)
        out.append(t)
    out.sort(key=lambda t: t["in"])
    return out


def study(sub, sid, prog=None):
    s = get(sub, sid)
    if not all(x["ok"] for x in data_check(sub, sid)):
        raise BadRequest("Not enough data: download at least 60 days of candles (Data page).")
    ts = run_trades(sub, s, prog=prog)
    store.put_json(f"strat/{sub}/{sid}/study", {"trades": ts})
    s["study"] = {"at": iso(now_ny()), "n": len(ts), "from": ts[0]["d"] if ts else None, "to": ts[-1]["d"] if ts else None,
                  "m": metrics(ts), "factors": _factor_table(ts, getattr(_mod(s["setup"]), "FACTORS", []))}
    s.update(valid=None, stage="study")
    db.put(s)
    return _view(s)


def _grid(ts):
    out = {}
    for ph in PHASES:
        for v in VOLS:
            xs = [t for t in ts if t.get("cell") == f"{ph}|{v}"]
            m = metrics(xs)
            out[f"{ph}|{v}"] = {"n": m["n"], "avgR": m.get("avgR"), "exp": m.get("exp"), "total": m.get("total")}
    return out


# ---------------------------------------------------------------- optimization

GRIDS = {
    "ifvg": {"hours": ["rth", "all"], "stop": ["fvg", "swing"], "trail": ["swing", "r", "ema15"], "trailR": [0.5, 1.0], "beR": [0, 0.5], "partPct": [0, 50]},
    "biaspull": {"hours": ["rth", "all"], "pbMin": [1, 2, 3], "trail": ["swing", "r", "ema15"], "beR": [0, 0.5]},
    "ondrift": {"entryAt": ["18:00", "20:00", "00:00", "02:00"], "exitAt": ["02:00", "06:00", "09:30", "15:55"], "bias": ["all3", "h1", "none"], "stopMult": [0, 4.0, 8.0]},
    "ldnbrk": {"asiaEnd": ["00:00", "02:00", "03:00"], "lastEntry": ["04:00", "06:00", "09:30"], "stop": ["opp", "mid", "bar"], "tgtMult": [0, 0.5, 1.0, 2.0], "bias": ["all3", "none"]},
    "smt": {"look": [6, 12, 24, 48], "wait": [1, 3, 6], "hours": ["rth", "all"], "tgt": ["vwap", "r"], "tradeSym": ["NQ", "ES"]},
    "flatrev": {"distMult": [1.5, 2.0, 3.0, 4.0], "hours": ["rth", "all"], "bias": ["all3", "h1"], "minRR": [0, 1.0]},
    "postclose": {"fromAt": ["14:00", "15:00", "15:30"], "exitAt": ["16:15", "16:30", "16:55"], "minMovePct": [0.1, 0.3, 0.6], "mode": ["follow", "fade"]},
    "sungap": {"which": ["monday", "all"], "smallPct": [0.1, 0.25, 0.5], "bigPct": [0.25, 0.5, 1.0], "exitAt": ["20:00", "02:00", "09:30"], "mode": ["both", "fade", "follow"]},
    "insidebar": {"tf": [1, 5, 15], "entry": ["break", "close"], "tgtR": [1.0, 1.5, 2.0, 3.0], "hours": ["rth", "all"], "bias": ["none", "all3"], "minMother": [0, 1.0, 1.5]},
    "imom": {"from": ["close", "open"], "entryAt": ["14:00", "15:00", "15:30", "15:45"], "minMovePct": [0, 0.25, 0.5], "mode": ["follow", "fade"], "stopMult": [0, 4.0]},
    "onrev": {"minPct": [0.4, 0.6, 1.0], "fill": [0.5, 1.0], "stopGap": [0.5, 1.0, 2.0], "exitAt": ["10:30", "11:30", "15:55"]},
    "onhold": {"stop": ["mid", "opp", "part"], "stopPart": [0.15, 0.25, 0.5], "deadPct": [0, 10, 20],
               "exitAt": ["10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00", "14:30", "15:00", "15:30", "15:55"]},
    "onmid": {"hold": ["close", "off"], "holdStop": ["be", "level", "orig"], "stop": ["mid", "opp", "part"], "stopPart": [0.15, 0.25, 0.5], "deadPct": [0, 10, 20], "exitAt": ["10:30", "11:30", "13:00", "15:55"], "minRR": [0, 0.5, 1]},
    "gapfill": {"minPct": [0.05, 0.1, 0.2], "maxPct": [0.3, 0.5, 0.8], "stopGap": [1.0, 1.5, 3.0], "exitAt": ["10:30", "13:00", "15:55"]},
    "tom": {"entryDay": [-3, -2, -1], "exitDay": [1, 2, 3, 4, 5], "trend": ["none", "sma50", "sma200"], "stopAtr": [0, 2.0]},
    "vwapband": {"k": [1.5, 2.0, 2.5, 3.0], "trendMult": [0, 0.7, 1.0], "minRR": [0, 1.0], "tf": [1, 5]},
    "rsi2": {"lo": [5, 10, 20], "hi": [55, 70, 85], "trendLen": [50, 100, 200], "maxHold": [5, 10, 20], "side": ["long", "both"]},
    "squeeze": {"tf": [5, 15], "kc": [1.25, 1.5, 2.0], "minBars": [3, 6, 12], "stop": ["mid", "opp"], "tgtR": [1.0, 2.0, 3.0], "hours": ["rth", "all"]},
    "volclimax": {"tf": [1, 5], "volMult": [2.0, 3.0, 5.0], "wickPct": [0, 40, 60], "tgt": ["vwap", "r"], "hours": ["rth", "all"]},
    "pwsweep": {"tgt": ["vwap", "mid", "r"], "lastEntry": ["11:00", "13:00", "15:00"], "minDepth": [0, 5, 15], "fresh": [True, False]},
    "trendday": {"rangeMult": [0.4, 0.6, 0.8, 1.0], "edgePct": [10, 20, 30], "stop": ["mid", "opp"], "exit": ["target", "trail"]},
    "fpdisp": {"fair": ["open", "vwap"], "bodyMult": [1.0, 1.5, 2.0], "stop": ["bar", "swing"], "lastEntry": ["11:00", "13:00", "15:00"]},
    "pdbreak": {"bias": ["close", "trend", "none"], "brkTf": [5, 15], "tgtAtr": [0.75, 1.0, 1.5]},
    "pdsweep": {"levels": ["pd", "outer"], "tgt": ["vwap", "mid", "other"], "lastEntry": ["10:30", "11:30", "13:00"], "minDepth": [0, 5]},
    "nrbreak": {"cond": ["nr7", "inside", "either", "any"], "orMin": [15, 30], "trend": ["sma", "none"], "tgtAtr": [0.75, 1.0]},
    "onsweep": {"bufTicks": [2, 8], "tgt": ["vwap", "mid", "other"], "lastEntry": ["10:00", "10:30"], "minDepth": [0, 5, 15]},
    "ibbreak": {"stop": ["mid", "opp", "bar"], "tgtMult": [0.5, 1.0, 1.5], "filter": ["vwap", "overnight", "none"]},
    "odpull": {"drivePct": [0.15, 0.25, 0.4], "pbMin": [1, 2, 3], "ema": [9, 20], "tgtR": [1.5, 2.0, 3.0]},
    "vwapret": {"trend": [2, 3, 5], "wait": [1, 3], "tgtR": [1.0, 1.5, 2.0, 3.0], "hours": ["all", "rth"]},
    "orbema": {"rangeMin": [15, 30], "ema": [9, 20], "minRR": [0, 1, 2], "entryAt": ["close", "open"]},
    "range50": {"legPct": [0.2, 0.35, 0.5], "entryLvl": [0.2, 0.25, 0.33], "stopBuf": [0, 0.1, 0.25]},
    "silver": {"pivLen": [3, 5, 10], "rr": [1.0, 1.5, 2.0, 3.0], "expiry": [10, 30]},
    "orb": {"rangeMin": [5, 15, 30], "tgtR": [1.0, 2.0, 3.0], "lastEntry": ["10:30", "11:30", "13:00"]},
    "tvpoc": {"entry": ["flip", "poc"], "atrMult": [0.75, 1.0, 1.5], "maLen": [20, 25, 50]},
    "smfade": {"smLevel": ["wall", "max", "median"], "smTgt": ["median", "close", "pivot"], "tf": [5, 15], "smMatch": ["hl", "weekday"], "macro": ["none", "with"]},
    "oaride": {"entry": ["cross", "vector"], "oaExit": [0.2, 0.3, 0.4, 0.5], "tf": [5, 15, 60], "wave": [7, 10], "macro": ["with", "none"]},
    "latewave": {"htf": ["60", "240", "D"], "lateN": [2, 3, 5], "lateStopP": [0.2, 0.1], "tf": [5, 15], "tgt": ["level", "prev"]},
    "fractal": {"entry": ["cross", "vector", "fade", "count"], "tf": [5, 15, 60], "wave": [7, 10], "macro": ["with", "none"], "tgt": ["level", "prev", "day", "pivot"]},
    "wt": {"tf": ["1D", "60", "15"], "stopAtr": [0, 1.0, 1.5, 2.0], "side": ["long", "both"]},
}
OPT_MIN = 30                                           # train trades a setting needs to be ranked


GRID_SKIP = {"ifvg": lambda c: c["trail"] != "r" and c["trailR"] != 0.5}   # the R distance only matters for the R trail


def _combos(setup):
    import itertools
    g = GRIDS.get(setup) or {}
    keys = list(g)
    skip = GRID_SKIP.get(setup) or (lambda c: False)
    return [c for c in (dict(zip(keys, vals)) for vals in itertools.product(*[g[k] for k in keys])) if not skip(c)]


def _brief(m):
    return {k: m.get(k) for k in ("n", "win", "avgR", "pf", "exp", "total", "dd", "edge")}


def optimize(sub, sid, prog=None, deadline=None):
    """Every combination of the setup's grid: the trades, split at the same date (first 60% of the sessions = train,
    the rest = test). Ranked on the train period only; the test columns show whether the ranking holds.
    Resumable: returns False when out of time (the rows so far are saved)."""
    import time as _t
    s = get(sub, sid)
    if not all(x["ok"] for x in data_check(sub, sid)):
        raise BadRequest("Not enough data: download at least 60 days of candles (Data page).")
    combos = _combos(s["setup"])
    if not combos:
        raise BadRequest("No settings grid for this setup.")
    key = f"strat/{sub}/{sid}/opt"
    cur = store.get_json(key, {}) or {}
    base = {k: v for k, v in s["params"].items()}
    if cur.get("base") != base or cur.get("grid") != GRIDS[s["setup"]]:
        days = [d for d in _days(s["params"]["tickers"][0])][-int(s["params"].get("days") or 250):]
        cur = {"base": base, "grid": GRIDS[s["setup"]], "split": days[int(len(days) * TRAIN)] if days else None, "rows": [],
               "total": len(combos), "complete": False, "at": iso(now_ny())}
    split, slow = cur["split"], 0.0
    for i in range(len(cur["rows"]), len(combos)):
        if deadline and _t.time() + 1.5 * slow > deadline:     # not enough time left for one more setting
            store.put_json(key, cur)
            return False
        if prog:
            prog(f"Optimizing: setting {i + 1} of {len(combos)}")
        t0 = _t.time()
        c = combos[i]
        trial = {**s, "params": _params(s["setup"], {**base, **c})}
        ts = [t for t in run_trades(sub, trial) if t.get("why") != "open"]
        tr, te = [t for t in ts if t["d"] < split], [t for t in ts if t["d"] >= split]
        cur["rows"].append({"p": c, "train": _brief(metrics(tr)), "test": _brief(metrics(te))})
        store.put_json(key, cur)                               # saved after every setting: a cut-off run loses at most one
        slow = max(slow, _t.time() - t0)
    cur.update(complete=True, at=iso(now_ny()))
    store.put_json(key, cur)
    return True


def opt_view(sub, sid):
    get(sub, sid)
    cur = store.get_json(f"strat/{sub}/{sid}/opt", None)
    if not cur:
        return None
    rows = sorted(cur["rows"], key=lambda r: (r["train"].get("n", 0) >= OPT_MIN, r["train"].get("exp") or -1e9), reverse=True)
    return {**cur, "rows": rows, "minTrades": OPT_MIN}


def validate(sub, sid):
    s = get(sub, sid)
    ts = [t for t in (store.get_json(f"strat/{sub}/{sid}/study", {}) or {}).get("trades") or [] if t.get("why") != "open"]
    if not ts:
        raise BadRequest("Run the study first.")
    k = int(len(ts) * TRAIN)
    train, test = ts[:k], ts[k:]
    mt, me, mb, m2 = metrics(train), metrics(test), metrics(test, base=True), metrics(test, cm=2)
    months = defaultdict(float)
    for t in ts:
        months[t["d"][:7]] += t["net"]
    tot = sum(months.values())
    share = max(months.values()) / tot if tot > 0 and months else None
    rule = s.get("edgeTest") == "rule" and me.get("edge2") is not None
    timing = {"k": "edge", "t": "Beats random entries, same side (the entry timing, test)", "v": f"{me.get('edge', 0):+.2f} pts" if me.get("edge") is not None else "—",
              "ok": bool(me.get("edge") is not None and me["edge"] > 0)}
    whole = {"k": "edge2", "t": "Beats random entries, random side (the whole rule, test)", "v": f"{me['edge2']:+.2f} pts", "ok": me["edge2"] > 0} \
        if me.get("edge2") is not None else None
    gates = [
        whole if rule else timing,
        {"k": "pf", "t": f"Profit factor ≥ {PF_MIN:g} (test)", "v": f"{me['pf']:.2f}" if me.get("pf") else "—", "ok": bool(me.get("pf") and me["pf"] >= PF_MIN)},
        {"k": "n", "t": f"At least {MIN_TRADES} trades", "v": f"{len(ts):,}", "ok": len(ts) >= MIN_TRADES},
        {"k": "cost", "t": "Still profitable at 2× costs (test)", "v": f"${m2.get('exp', 0):+,.0f} / trade", "ok": bool(m2.get("exp", 0) > 0)},
        {"k": "month", "t": f"No month over {MONTH_MAX:.0%} of the profit", "v": f"{share:.0%}" if share is not None else "no profit",
         "ok": share is not None and share <= MONTH_MAX},
    ]
    grid = _grid(test)
    if not s.get("cells"):                             # first validation: approve the conditions that made money on the test
        s["cells"] = {c: bool(g["n"] >= 10 and (g.get("avgR") or 0) > 0) for c, g in grid.items()}
    ok = all(g["ok"] for g in gates)
    info = [{**(timing if rule else whole), "info": True}] if whole else []        # the other random test: shown, not a gate
    s["valid"] = {"info": info, "edgeTest": "rule" if rule else "timing", "at": iso(now_ny()), "train": mt, "test": me, "base": mb, "cost2": m2, "gates": gates, "pass": ok, "grid": grid,
                  "split": test[0]["d"] if test else None}
    if s.get("stage") not in ("paper",):
        s["stage"] = "validated" if ok else "study"
    db.put(s)
    return _view(s)


def action(sub, sid, act):
    s = get(sub, sid)
    if act in ("paper", "paper_force"):
        if not s.get("valid"):
            raise BadRequest("Run the validation first.")
        if act == "paper" and not s["valid"].get("pass"):
            raise BadRequest("Paper trading needs a passed validation.")
        s["paper"] = {"forced": act == "paper_force"}
        if any(stockdata.is_stock(t) for t in s["params"]["tickers"]):
            raise BadRequest("Paper trading runs on the NQ / ES candles; QQQ / SPY strategies are for testing only.")
        if not any(s.get("cells", {}).values()):
            raise BadRequest("Approve at least one market condition first.")
        s["paper"] = {**s["paper"], "on": True, "since": now_ny().strftime("%Y-%m-%d"), "n": 0, "net": 0}
        store.delete(f"strat/{sub}/{sid}/paper")
        s["stage"] = "paper"
    elif act == "stop":
        s["paper"] = {**(s.get("paper") or {}), "on": False}
        s["stage"] = "validated" if (s.get("valid") or {}).get("pass") else "study"
    elif act == "retire":
        s["paper"] = {**(s.get("paper") or {}), "on": False}
        s["stage"] = "retired"
    elif act == "unretire":
        s["stage"] = "validated" if (s.get("valid") or {}).get("pass") else ("study" if s.get("study") else "idea")
    elif act == "live":
        raise BadRequest("Live trading needs a futures broker connection (not available yet).")
    else:
        raise BadRequest("Unknown action.")
    db.put(s)
    if act in ("paper", "paper_force"):
        try:
            paper_one(sub, s)
        except Exception as e:
            print("paper start failed", e)
    return _view(get(sub, sid))


# ---------------------------------------------------------------- paper (simulated on the stored candles)

def paper_one(sub, s):
    pp = s.get("paper") or {}
    if not pp.get("on"):
        return None
    ts = run_trades(sub, s, start=pp["since"], days=400)
    ts = [t for t in ts if t["d"] >= pp["since"]]
    last = {t: futdata.last_bar_time(t) for t in s["params"]["tickers"]}
    now = now_ny().strftime("%Y-%m-%dT%H:%M")
    kept, day_net = [], defaultdict(float)
    for t in ts:
        if t.get("cell") and not s.get("cells", {}).get(t["cell"]):
            continue                                   # a market condition that isn't approved
        if s.get("dailyLoss") and day_net[t["d"]] <= -s["dailyLoss"]:
            continue                                   # the day's loss limit was hit
        intraday = s["params"].get("tf") != "1D"
        if t["why"] in ("open", "close") and t["out"][:16] >= (last.get(t["s"]) or "")[:16] and (not intraday or now < f"{t['d']}T16:00"):
            t["status"] = "open"
        else:
            t["status"] = "closed"
            day_net[t["d"]] += t["net"]
        kept.append(t)
    store.put_json(f"strat/{sub}/{s['id']}/paper", {"trades": kept})
    closed = [t for t in kept if t["status"] == "closed"]
    m = metrics(closed)
    te = (s.get("valid") or {}).get("test") or {}
    z = None
    if m["n"] and m.get("avgR") is not None and te.get("avgR") is not None and te.get("sdR"):
        z = round((m["avgR"] - te["avgR"]) / (te["sdR"] / math.sqrt(m["n"])), 2)
    kill = None
    if m["n"] >= 10 and z is not None and z < -2:
        kill = f"z {z} after {m['n']} trades: worse than the test"
    elif m["n"] and te.get("dd") and m.get("dd", 0) > 2 * te["dd"]:
        kill = f"drawdown ${m['dd']:,.0f} over 2× the test's ${te['dd']:,.0f}"
    pp.update(n=m["n"], net=m.get("total", 0), avgR=m.get("avgR"), z=z, dd=m.get("dd"), at=iso(now_ny()),
              open=[t for t in kept if t["status"] == "open"][-3:])
    if kill:
        pp.update(on=False, kill=kill)
        s["stage"] = "validated"
    s["paper"] = pp
    db.put(s)
    return pp


def paper_tick(sub):
    n = 0
    for s in _all(sub):
        if (s.get("paper") or {}).get("on"):
            try:
                paper_one(sub, s)
                n += 1
            except Exception as e:
                print("paper failed", s.get("id"), e)
    return n


def trades(sub, sid, which):
    get(sub, sid)
    if which not in ("study", "paper"):
        raise BadRequest("Pick study or paper.")
    return (store.get_json(f"strat/{sub}/{sid}/{which}", {}) or {}).get("trades") or []


def marks(sub, sid, sym, start, end):
    """The strategy's trades between two dates, to draw on the chart."""
    s = get(sub, sid)
    if sym not in s["params"]["tickers"]:
        return []
    s = {**s, "params": {**s["params"], "tickers": [sym]}}
    return [t for t in run_trades(sub, s, start=start, days=60) if start <= t["d"] <= end]


def overlay(sub, sid, sym, start, end):
    """The setup's own lines (its EMA, its ranges) for the chart, when it has any."""
    s = get(sub, sid)
    mod = _mod(s["setup"])
    if sym not in s["params"]["tickers"] or not hasattr(mod, "overlay"):
        return None
    return mod.overlay(sub, s["params"], sym, start, end)


def running(sub):
    """The home page: the markets, the strategies trading (paper), today's fills and the risk used."""
    mk = [futdata.latest(t) for t in futdata.SYMS]
    rows, fills, today_net, limit, open_n = [], [], 0.0, 0.0, 0
    days = [m["session"] for m in mk if m.get("session")]
    today = max(days) if days else now_ny().strftime("%Y-%m-%d")
    for s in _all(sub):
        pp = s.get("paper") or {}
        if not pp.get("on") and not pp.get("kill"):
            continue
        ts = (store.get_json(f"strat/{sub}/{s['id']}/paper", {}) or {}).get("trades") or []
        td = [t for t in ts if t["d"] == today]
        net = sum(t["net"] for t in td if t["status"] == "closed")
        if pp.get("on"):
            today_net += net
            limit += s.get("dailyLoss") or 0
        op = [t for t in ts if t["status"] == "open"]
        open_n += len(op) * s.get("contracts", 1) if pp.get("on") else 0
        for t in td:
            fills.append({"t": t["in"][11:16], "side": "BUY" if t["side"] == "long" else "SELL", "what": f"{s['name']} · {t['s']} {s.get('contracts', 1)}", "px": t["e"]})
            if t["status"] == "closed":
                fills.append({"t": t["out"][11:16], "side": "SELL" if t["side"] == "long" else "BUY", "what": f"{s['name']} · exit ({t['why']})", "px": t["x"]})
        rows.append({"id": s["id"], "name": s["name"], "meta": f"{', '.join(s['params']['tickers'])} · {_tf(s)} · {s.get('contracts', 1)} contract{'s' if s.get('contracts', 1) > 1 else ''}",
                     "mode": "Paper", "on": bool(pp.get("on")), "kill": pp.get("kill"), "today": round(net, 2), "tradesToday": len(td),
                     "n": pp.get("n", 0), "net": pp.get("net", 0), "z": pp.get("z"), "avgR": pp.get("avgR"),
                     "pos": [{"s": t["s"], "side": t["side"], "e": t["e"]} for t in op]})
    fills.sort(key=lambda f: f["t"])
    return {"markets": mk, "strategies": rows, "fills": fills, "today": today,
            "risk": {"net": round(today_net, 2), "limit": limit, "open": open_n}, "coverage": futdata.coverage()}


def _tf(s):
    tf = s["params"].get("tf", 1)
    return "daily" if str(tf) == "1D" else f"{tf}m" if s["setup"] != "orb" else "1m"
