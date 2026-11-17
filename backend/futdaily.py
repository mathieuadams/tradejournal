"""Daily bars of a diversified futures portfolio (the Turtle markets traded on CME Globex) from Databento, back-adjusted.

Databento GLBX.MDP3 ohlcv-1d: one request per market for every contract (stype parent, ES.FUT; raw symbols mapped,
spreads dropped), the front picked here — the most traded, rolled only forward. Fallback (an answer without symbols):
continuous by volume, ROOT.v.0 / ROOT.v.1 in 3-year windows. Contract prices jump at a roll, so the series is
back-adjusted (Panama, additive): the new contract's close the day before minus the old one's is added to every earlier
bar. Point differences, and so P/L in $, stay exact; old prices can go negative (percentages are meaningless).
Soft commodities (coffee, sugar, cotton, cocoa) and Brent trade on ICE, not on Globex: not included."""
import time

import databento
import store
from util import BadRequest, iso, now_ny

TIMEOUT = 90                               # seconds per request: a stalled answer must not eat the job's 15 minutes
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
    """The end of the daily bars (ohlcv-1d stops at the last complete day, before the dataset's own end)."""
    return databento.dataset_range(key, "ohlcv-1d")["end"][:19]


def cost(sub, roots=None):
    key = databento.key(sub)
    roots = roots or list(SPEC)
    r = databento._retry_end(lambda e: databento._json(key, "metadata.get_cost", {"dataset": databento.DATASET, "symbols": ",".join(f"{x}.FUT" for x in roots),
                                                                                "schema": "ohlcv-1d", "start": START, "end": e, "stype_in": "parent"}),
                             _end(key))
    try:
        return float(r)
    except (TypeError, ValueError):
        return float((r or {}).get("cost") or 0) if isinstance(r, dict) else 0.0


def _windows(end):
    """3-year request windows from START to end: a short request answers fast and a stalled one costs little."""
    y0, y1 = int(START[:4]), int(end[:4])
    out, a = [], START
    for y in range(y0 + 3, y1 + 4, 3):
        b = min(f"{y}-01-01T00:00:00", end)
        if b > a:
            out.append((a if "T" in a else a + "T00:00:00", b))
        a = b
    return out


def _read_all(r, t0):
    """The whole body, but never longer than TIMEOUT in total (a trickling answer must not hold the job)."""
    buf = bytearray()
    while True:
        if time.time() - t0 > TIMEOUT:
            raise TimeoutError(f"Databento took more than {TIMEOUT} s")
        chunk = r.read(1 << 16)
        if not chunk:
            return bytes(buf)
        buf += chunk


WORKERS = 24                               # requests in flight at once (Databento answers each slowly, not many slowly)


def _window(key, sym, a, b):
    """One request: [date, o, h, l, c, v, instrument id] of a continuous symbol between a and b (retried once)."""
    import csv
    import io
    params = {"dataset": databento.DATASET, "symbols": sym, "schema": "ohlcv-1d", "start": a, "end": b, "stype_in": "continuous",
              "encoding": "csv", "compression": "none", "pretty_px": "true", "pretty_ts": "true"}
    for attempt in (1, 2):
        t0 = time.time()
        try:
            r = databento._retry_end(lambda e: databento._open(key, "POST", "timeseries.get_range", {**params, "end": e}, timeout=TIMEOUT), b)
            try:
                body = _read_all(r, t0)
            finally:
                r.close()
            break
        except (TimeoutError, OSError) as e:
            if attempt == 2:
                raise TimeoutError(f"{sym} {a[:10]} → {b[:10]}: {e}")
    out = []
    for x in csv.DictReader(io.StringIO(body.decode("utf-8"))):
        try:
            out.append([x["ts_event"][:10], float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]), int(float(x["volume"])),
                        int(x["instrument_id"])])
        except (KeyError, ValueError):
            continue
    return out


def _rows(key, sym, end):
    """Every window of one continuous symbol, in order (sequential; the download runs windows in parallel)."""
    return _merge([_window(key, sym, a, b) for a, b in _windows(end)])


def _merge(parts):
    seen, out = set(), []
    for part in parts:
        for x in part:
            if x[0] not in seen:
                seen.add(x[0])
                out.append(x)
    out.sort(key=lambda x: x[0])
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


MONTHS = "FGHJKMNQUVXZ"
PARENT_TIMEOUT = 240                       # one request per market for every contract since 2010


def _parent(key, root, end):
    """Every outright contract's daily bars of one market in ONE request (stype parent ROOT.FUT, raw symbols mapped):
    {date: {symbol: [o, h, l, c, v]}}; spreads (ESZ6-ESH7) and options are left out. None when the answer has no
    symbol column (then the continuous way is used)."""
    import csv
    import io
    import re
    params = {"dataset": databento.DATASET, "symbols": f"{root}.FUT", "schema": "ohlcv-1d", "start": START, "end": end, "stype_in": "parent",
              "encoding": "csv", "compression": "none", "pretty_px": "true", "pretty_ts": "true", "map_symbols": "true"}
    t0 = time.time()
    r = databento._retry_end(lambda e: databento._open(key, "POST", "timeseries.get_range", {**params, "end": e}, timeout=PARENT_TIMEOUT), end)
    try:
        body = bytearray()
        while True:
            if time.time() - t0 > PARENT_TIMEOUT:
                raise TimeoutError(f"{root}: Databento took more than {PARENT_TIMEOUT} s")
            ch = r.read(1 << 16)
            if not ch:
                break
            body += ch
    finally:
        r.close()
    rd = csv.DictReader(io.StringIO(body.decode("utf-8")))
    if "symbol" not in (rd.fieldnames or []):
        return None
    pat = re.compile("^" + re.escape(root) + "[" + MONTHS + "][0-9]{1,2}$")
    out = {}
    for x in rd:
        sym = x.get("symbol") or ""
        if not pat.match(sym):
            continue
        try:
            out.setdefault(x["ts_event"][:10], {})[sym] = [float(x["open"]), float(x["high"]), float(x["low"]), float(x["close"]), int(float(x["volume"]))]
        except (KeyError, ValueError):
            continue
    return out


def _expiry(root, sym, day):
    """(year, month) of a contract from its code: ESZ6 on 2026-11-16 → (2026, 12); one digit = the nearest decade ahead."""
    code = sym[len(root):]
    m = MONTHS.index(code[0]) + 1
    y0 = int(day[:4])
    if len(code) == 3:
        y = y0 // 100 * 100 + int(code[1:])
    else:
        y = y0 // 10 * 10 + int(code[1:])
        if y < y0 - 1:
            y += 10
    return (y, m)


def front(root, by_day):
    """Back-adjusted front month from every contract: the most traded one, rolled only forward (to a later expiry, when
    it trades more), the jump at each roll (the new contract's close the day before minus the old one's) added to every
    earlier bar. Returns bars, rolls, rolls not adjusted."""
    days = sorted(by_day)
    cur, seq, gaps, missing = None, [], [], 0
    for k, d in enumerate(days):
        bars = by_day[d]
        if not bars:
            continue
        best = max(bars, key=lambda s: bars[s][4])
        nxt = cur
        if cur is None or cur not in bars:
            later = [s for s in bars if cur is None or _expiry(root, s, d) > _expiry(root, cur, d)]
            nxt = max(later or list(bars), key=lambda s: bars[s][4])
        elif best != cur and _expiry(root, best, d) > _expiry(root, cur, d) and bars[best][4] > bars[cur][4]:
            nxt = best
        g = 0.0
        if cur is not None and nxt != cur:
            prev = by_day[days[k - 1]] if k else {}
            if nxt in prev and cur in prev:
                g = prev[nxt][3] - prev[cur][3]
            else:
                missing += 1
        cur = nxt
        seq.append([d] + bars[cur])
        gaps.append(g)
    adj, out = 0.0, [None] * len(seq)
    for i in range(len(seq) - 1, -1, -1):
        x = seq[i]
        out[i] = [x[0], x[1] + adj, x[2] + adj, x[3] + adj, x[4] + adj, x[5]]
        adj += gaps[i]
    return out, sum(1 for g in gaps if g), missing


def _market(key, root, end):
    """One market: every contract in one request (fast); the continuous way only if that answer can't be used."""
    p = _parent(key, root, end)
    if p:
        return front(root, p)
    v0, v1 = _rows(key, f"{root}.v.0", end), _rows(key, f"{root}.v.1", end)
    return adjust(v0, v1)


def download(sub, prog=None, deadline=None, cont=None):
    """Every market at once (one request each, in parallel). Resumable: returns the index to continue from, or None."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    key = databento.key(sub)
    end = _end(key)
    roots = list(SPEC)
    today = iso(now_ny())[:10]
    todo = []
    for i in range((cont or 0), len(roots)):
        old = store.get_json(f"fut/d/{roots[i]}") or {}
        if not (old.get("bars") and not old.get("error") and (old.get("at") or "")[:10] == today):
            todo.append(roots[i])                      # not fetched today yet (a resumed download skips the rest)
    if deadline and time.time() > deadline - PARENT_TIMEOUT - 60:
        return cont or 0
    done = len(roots) - len(todo)
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_market, key, r, end): r for r in todo}
        for f in as_completed(futs):
            r = futs[f]
            done += 1
            try:
                bars, rolls, missing = f.result()
                store.put_json(f"fut/d/{r}", {"bars": bars, "rolls": rolls, "missing": missing, "at": iso(now_ny())})
            except Exception as e:                     # refused, timed out: note it on the market, go on
                old = store.get_json(f"fut/d/{r}") or {}
                store.put_json(f"fut/d/{r}", {**old, "bars": old.get("bars") or [], "error": (str(e) or type(e).__name__)[:300], "at": iso(now_ny())})
            if prog:
                prog(f"Futures daily bars: {done}/{len(roots)} markets (last: {r})")
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
    return jobs.start(sub, "swingfut", "futdaily", {})
