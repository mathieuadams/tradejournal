"""Daily bars of a diversified futures portfolio (the Turtle markets traded on CME Globex) from Databento, back-adjusted.

Databento GLBX.MDP3 ohlcv-1d, continuous by volume: ROOT.v.0 (the front) and ROOT.v.1 (the next). Continuous prices
are the real contracts' prices, so a roll leaves a jump. Back-adjusted (Panama, additive): on the day the front
changes contract, the new contract's close the day before (from v.1) minus the old one's is added to every earlier
bar. Point differences, and so P/L in $, stay exact; old prices can go negative (percentages are meaningless).
Soft commodities (coffee, sugar, cotton, cocoa) and Brent trade on ICE, not on Globex: not included."""
import time

import databento
import store
from util import BadRequest, iso, now_ny

START = "2010-06-07"                       # GLBX.MDP3 history starts in June 2010
# root: (name, group, $ per point, tick)
SPEC = {
    "ES": ("E-mini S&P 500", "Equity index", 50, 0.25), "NQ": ("E-mini Nasdaq-100", "Equity index", 20, 0.25),
    "YM": ("E-mini Dow", "Equity index", 5, 1.0), "RTY": ("E-mini Russell 2000", "Equity index", 50, 0.1),
    "ZT": ("2-year Note", "Rates", 2000, 0.00390625), "ZF": ("5-year Note", "Rates", 1000, 0.0078125),
    "ZN": ("10-year Note", "Rates", 1000, 0.015625), "ZB": ("30-year Bond", "Rates", 1000, 0.03125),
    "GC": ("Gold", "Metals", 100, 0.1), "SI": ("Silver", "Metals", 5000, 0.005), "HG": ("Copper", "Metals", 25000, 0.0005),
    "PL": ("Platinum", "Metals", 50, 0.1),
    "CL": ("Crude oil (WTI)", "Energy", 1000, 0.01), "NG": ("Natural gas", "Energy", 10000, 0.001),
    "HO": ("Heating oil", "Energy", 42000, 0.0001), "RB": ("Gasoline (RBOB)", "Energy", 42000, 0.0001),
    "ZC": ("Corn", "Grains", 50, 0.25), "ZS": ("Soybeans", "Grains", 50, 0.25), "ZW": ("Wheat", "Grains", 50, 0.25),
    "ZL": ("Soybean oil", "Grains", 600, 0.01), "ZM": ("Soybean meal", "Grains", 100, 0.1),
    "LE": ("Live cattle", "Meats", 400, 0.025), "HE": ("Lean hogs", "Meats", 400, 0.025), "GF": ("Feeder cattle", "Meats", 500, 0.025),
    "6E": ("Euro", "Currencies", 125000, 0.00005), "6J": ("Japanese yen", "Currencies", 12500000, 0.0000005),
    "6B": ("British pound", "Currencies", 62500, 0.0001), "6A": ("Australian dollar", "Currencies", 100000, 0.00005),
    "6C": ("Canadian dollar", "Currencies", 100000, 0.00005), "6S": ("Swiss franc", "Currencies", 125000, 0.00005),
}
GROUPS = list(dict.fromkeys(v[1] for v in SPEC.values()))


def _syms(roots):
    return [f"{r}.v.{k}" for r in roots for k in (0, 1)]


def _end(key):
    return databento.dataset_range(key)["end"][:19]


def cost(sub, roots=None):
    key = databento.key(sub)
    roots = roots or list(SPEC)
    r = databento._retry_end(lambda e: databento._json(key, "metadata.get_cost", {"dataset": databento.DATASET, "symbols": ",".join(_syms(roots)),
                                                                                "schema": "ohlcv-1d", "start": START, "end": e, "stype_in": "continuous"}),
                             _end(key))
    try:
        return float(r)
    except (TypeError, ValueError):
        return float((r or {}).get("cost") or 0) if isinstance(r, dict) else 0.0


def _rows(key, sym, end):
    import csv
    import io
    params = {"dataset": databento.DATASET, "symbols": sym, "schema": "ohlcv-1d", "start": START, "end": end, "stype_in": "continuous",
              "encoding": "csv", "compression": "none", "pretty_px": "true", "pretty_ts": "true"}
    r = databento._retry_end(lambda e: databento._open(key, "POST", "timeseries.get_range", {**params, "end": e}, timeout=300), end)
    out = []
    try:
        for x in csv.DictReader(io.TextIOWrapper(r, encoding="utf-8", newline="")):
            try:
                out.append([x["ts_event"][:10], float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]), int(float(x["volume"])),
                            int(x["instrument_id"])])
            except (KeyError, ValueError):
                continue
    finally:
        r.close()
    return out


def adjust(v0, v1):
    """Back-adjusted bars [date, o, h, l, c, v] from the front (v0) and next (v1) rows [date, o, h, l, c, v, id]."""
    nxt = {r[0]: (r[6], r[4]) for r in v1}
    gaps, missing = [0.0] * len(v0), 0
    for i in range(1, len(v0)):
        if v0[i][6] != v0[i - 1][6]:
            prev = nxt.get(v0[i - 1][0])
            if prev and prev[0] == v0[i][6]:
                gaps[i] = prev[1] - v0[i - 1][4]
            else:
                missing += 1                        # the new contract's close the day before is unknown: no adjustment
    adj, out = 0.0, [None] * len(v0)
    for i in range(len(v0) - 1, -1, -1):
        r = v0[i]
        out[i] = [r[0], r[1] + adj, r[2] + adj, r[3] + adj, r[4] + adj, r[5]]
        adj += gaps[i]
    return out, sum(1 for g in gaps if g), missing


def download(sub, prog=None, deadline=None, cont=None):
    """Every root (resumable: returns the index to continue from, or None when done)."""
    key = databento.key(sub)
    end = _end(key)
    roots = list(SPEC)
    for i in range((cont or 0), len(roots)):
        if deadline and time.time() > deadline:
            return i
        r = roots[i]
        if prog:
            prog(f"Futures daily bars: {r} ({i + 1}/{len(roots)})")
        try:
            v0, v1 = _rows(key, f"{r}.v.0", end), _rows(key, f"{r}.v.1", end)
        except BadRequest as e:
            store.put_json(f"fut/d/{r}", {"bars": [], "error": str(e), "at": iso(now_ny())})
            continue
        bars, rolls, missing = adjust(v0, v1)
        store.put_json(f"fut/d/{r}", {"bars": bars, "rolls": rolls, "missing": missing, "at": iso(now_ny())})
    return None


def bars(root):
    return (store.get_json(f"fut/d/{root}") or {}).get("bars") or []


def status():
    out = []
    for r, (name, grp, pv, tick) in SPEC.items():
        d = store.get_json(f"fut/d/{r}") or {}
        b = d.get("bars") or []
        out.append({"root": r, "name": name, "group": grp, "pv": pv, "tick": tick, "n": len(b), "first": b[0][0] if b else None,
                    "last": b[-1][0] if b else None, "rolls": d.get("rolls"), "missing": d.get("missing"), "error": d.get("error"), "at": d.get("at")})
    return out


def start(sub):
    import jobs
    c = cost(sub)
    cap = databento.status(sub)["maxCost"]
    if c > cap:
        raise BadRequest(f"Databento would bill ${c:.2f} for {len(SPEC)} markets' daily bars since {START}: above your cost limit (${cap:.2f}, Data page).")
    return jobs.start(sub, "swing", "futdaily", {})
