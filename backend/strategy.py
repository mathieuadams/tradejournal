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
import tvpoc
import store
import wtswing
from util import BadRequest, NotFound, iso, now_ny

SETUPS = {"orb": orb, "orbema": orbema, "range50": range50, "silver": silver, "tvpoc": tvpoc, "wt": wtswing}
NAMES = {"orb": "Opening range break + FVG retest", "orbema": "Opening range break + 20 EMA retest", "range50": "Range 50% retrace", "silver": "ICT Silver Bullet", "tvpoc": "Trend volume profile + rolling POC", "wt": "WaveTrend dots"}
SK = "STRAT#"
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
    p["tickers"] = [t for t in p.get("tickers") or [] if t in futdata.SYMS] or ["NQ"]
    return p


def _num(v, lo, hi, name):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number.")
    if not lo <= f <= hi:
        raise BadRequest(f"{name} must be between {lo:g} and {hi:g}.")
    return f


def get(sub, sid):
    s = db.get(_pk(sub), SK + sid)
    if not s:
        raise NotFound("No such strategy.")
    return s


def _view(s):
    s = {k: v for k, v in s.items() if k not in ("PK", "SK")}
    s["describe"] = _mod(s["setup"]).describe(s["params"])
    s["setupName"] = NAMES.get(s["setup"], s["setup"])
    return s


def listing(sub):
    return [_view(s) for s in db.q_prefix(_pk(sub), SK)]


def view(sub, sid):
    v = _view(get(sub, sid))
    v["coverage"] = {t: futdata.coverage()[t] for t in v["params"]["tickers"]}
    return v


def create(sub, b):
    setup = b.get("setup")
    p = _params(setup, b.get("params"))
    sid = uuid.uuid4().hex[:10]
    s = {"PK": _pk(sub), "SK": SK + sid, "id": sid, "setup": setup, "name": str(b.get("name") or NAMES[setup])[:80],
         "params": p, **COSTS, "stage": "idea", "cells": {}, "created": iso(now_ny()), "paper": {"on": False}}
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
    for k in ("study", "paper"):
        store.delete(f"strat/{sub}/{sid}/{k}")
    return {"ok": True}


# ---------------------------------------------------------------- trades and metrics

def _cost(s, sym):
    sp = futdata.SPEC[sym]
    return (2 * s.get("comm", COSTS["comm"]) + 2 * s.get("slip", COSTS["slip"]) * sp["tick"] * sp["pv"]) * s.get("contracts", 1)


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

def data_check(sub, sid):
    s = get(sub, sid)
    cov = futdata.coverage()
    out = []
    for t in s["params"]["tickers"]:
        c = cov[t]
        out.append({"s": t, "days": c["days"], "from": c["from"], "to": c["to"], "ok": c["days"] >= 60})
    return out


def run_trades(sub, s, start=None, days=None, prog=None):
    mod = _mod(s["setup"])
    p = dict(s["params"])
    if start:
        p["_start"] = start
    if days:
        p["days"] = days
    cellmap = {t: futdata.cells(t) for t in p["tickers"]}
    trades, base = mod.all_trades(sub, p, None, prog)
    out = []
    for x, b in zip(trades, base):
        t = _normal(s, x, b[2] if isinstance(b, (list, tuple)) and len(b) > 2 else [], cellmap)
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
    gates = [
        {"k": "edge", "t": "Beats random entries (test)", "v": f"{me.get('edge', 0):+.2f} pts" if me.get("edge") is not None else "—",
         "ok": bool(me.get("edge") is not None and me["edge"] > 0)},
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
    s["valid"] = {"at": iso(now_ny()), "train": mt, "test": me, "base": mb, "cost2": m2, "gates": gates, "pass": ok, "grid": grid,
                  "split": test[0]["d"] if test else None}
    if s.get("stage") not in ("paper",):
        s["stage"] = "validated" if ok else "study"
    db.put(s)
    return _view(s)


def action(sub, sid, act):
    s = get(sub, sid)
    if act == "paper":
        if not (s.get("valid") or {}).get("pass"):
            raise BadRequest("Paper trading needs a passed validation.")
        if not any(s.get("cells", {}).values()):
            raise BadRequest("Approve at least one market condition first.")
        s["paper"] = {"on": True, "since": now_ny().strftime("%Y-%m-%d"), "n": 0, "net": 0}
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
    if act == "paper":
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
        if t["why"] in ("open", "close") and t["out"][:16] >= (last.get(t["s"]) or "")[:16] and (not intraday or now[:10] == t["d"] and now[11:16] < "16:00"):
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
    for s in db.q_prefix(_pk(sub), SK):
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
    for s in db.q_prefix(_pk(sub), SK):
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
