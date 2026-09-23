"""Turn fills into round-trip trades, FIFO per account and symbol.

A trade opens when the position leaves zero and closes when it returns to zero.
Scale-ins average the entry, partial exits average the exit, and a fill that
flips the position closes the current trade and opens a new one with the rest.
"""
import re

from util import parse_iso, sha

MULT = {
    "MNQ": 2, "NQ": 20, "MES": 5, "ES": 50, "M2K": 5, "RTY": 50, "MYM": 0.5, "YM": 5,
    "MCL": 100, "CL": 1000, "MGC": 10, "GC": 100, "SI": 5000, "SIL": 1000, "ZB": 1000,
    "ZN": 1000, "6E": 125000, "M6E": 12500, "NG": 10000, "HG": 25000,
}
FUT = re.compile(r"^([A-Z0-9]{1,4}?)([FGHJKMNQUVXZ])(\d{1,2})$")
OCC = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
EPS = 1e-9


def root(sym):
    s = sym.upper().replace("/", "").strip()
    if s in MULT:
        return s
    m = FUT.match(s)
    if m and m.group(1) in MULT:
        return m.group(1)
    return s


def is_option(sym):
    return bool(OCC.match(sym.upper().replace(" ", "")))


def is_future(sym):
    return root(sym) in MULT


def multiplier(sym, given=None):
    if given:
        return float(given)
    if is_option(sym):
        return 100.0
    return float(MULT.get(root(sym), 1))


def _new(f):
    return {
        "acct": f["acct"], "sym": f["sym"], "dir": "Long" if f["side"] == "buy" else "Short",
        "openTs": f["ts"], "closeTs": None, "mult": f.get("mult") or multiplier(f["sym"]),
        "inQ": 0.0, "inV": 0.0, "outQ": 0.0, "outV": 0.0, "fees": 0.0,
        "fillIds": [], "firstId": f["id"],
    }


def _add(t, f, q):
    opening = (t["dir"] == "Long") == (f["side"] == "buy")
    if opening:
        t["inQ"] += q
        t["inV"] += q * f["price"]
    else:
        t["outQ"] += q
        t["outV"] += q * f["price"]
    if f["qty"]:
        t["fees"] += (f.get("fees") or 0) * q / f["qty"]
    t["fillIds"].append(f["id"])


def _finalize(t):
    sg = 1 if t["dir"] == "Long" else -1
    entry = t["inV"] / t["inQ"]
    exit_ = t["outV"] / t["outQ"] if t["outQ"] > EPS else None
    gross = (exit_ - entry) * sg * t["outQ"] * t["mult"] if exit_ is not None else 0.0
    closed = t["closeTs"] is not None
    hold = None
    if closed:
        hold = max(0, round((parse_iso(t["closeTs"]) - parse_iso(t["openTs"])).total_seconds() / 60))
    return {
        "id": sha(f'{t["acct"]}|{t["sym"]}|{t["openTs"]}|{t["firstId"]}'),
        "acct": t["acct"], "sym": t["sym"], "dir": t["dir"],
        "status": "closed" if closed else "open",
        "openTs": t["openTs"], "closeTs": t["closeTs"],
        "qty": round(t["inQ"], 8), "closedQty": round(t["outQ"], 8),
        "entry": round(entry, 6), "exit": round(exit_, 6) if exit_ is not None else None,
        "gross": round(gross, 2), "fees": round(t["fees"], 2), "net": round(gross - t["fees"], 2),
        "hold": hold, "mult": t["mult"], "fillIds": t["fillIds"],
    }


def group_fills(fills, stats=None):
    """stats (optional dict) receives "unmatchedCloses": closing fills whose opening fill isn't in the data,
    e.g. a position opened before the first imported statement. Those fills are left out of trades."""
    fills = sorted(fills, key=lambda f: (f["ts"], f["id"]))
    pos, cur, out = {}, {}, []
    unmatched = 0
    for f in fills:
        k = (f["acct"], f["sym"])
        q = f["qty"] if f["side"] == "buy" else -f["qty"]
        p = pos.get(k, 0.0)
        if f.get("effect") == "close":
            if abs(p) < EPS or (q > 0) == (p > 0):
                unmatched += 1
                continue
            if abs(q) > abs(p) + EPS:  # closing more than is open: close what is open, ignore the rest
                unmatched += 1
                f = {**f, "qty": abs(p), "fees": (f.get("fees") or 0) * abs(p) / f["qty"]}
                q = f["qty"] if f["side"] == "buy" else -f["qty"]
        if abs(p) < EPS:
            p = 0.0
            cur[k] = _new(f)
        t = cur[k]
        np_ = p + q
        if p != 0 and abs(np_) > EPS and (np_ > 0) != (p > 0):
            _add(t, f, abs(p))
            t["closeTs"] = f["ts"]
            out.append(t)
            nt = _new(f)
            _add(nt, f, abs(np_))
            cur[k] = nt
            pos[k] = np_
            continue
        _add(t, f, f["qty"])
        pos[k] = np_
        if abs(np_) < EPS:
            t["closeTs"] = f["ts"]
            out.append(t)
            pos[k] = 0.0
    for k, p in pos.items():
        if abs(p) > EPS:
            out.append(cur[k])
    if stats is not None:
        stats["unmatchedCloses"] = unmatched
    return [_finalize(t) for t in out]
