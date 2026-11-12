"""A strategy's trades replayed on 0DTE options (QQQ for NQ / QQQ trades, SPY for ES / SPY trades), by delta.

  For every study trade that opens and closes in the same regular session: a long buys a call, a short buys a put,
  expiring that day. The contract: the one whose delta at the entry is nearest the target delta (0.1 ... 0.9). Delta from
  Black-Scholes with the implied volatility of the option's own price at the entry (time left to 16:00, no rates).
  Entry: the option's first minute at / after the trade's entry time (+ `slip` per contract); exit: its last price at or
  before the trade's exit (- `slip`), the moment the underlying trade exits (stop / target / time). Commission `comm`
  per contract per side. Sized at 1 contract and at a fixed `budget` of premium per trade.

  Option minutes: Unusual Whales (option-contract/<OCC>/intraday?date=), downloaded once per day (job "opt0dte") for the
  strikes around the money on the trade's side, stored in store "opt0dte/<ETF>/<day>-<C|P>". The ETF price: Alpaca's
  5-minute bars (stockdata)."""
import math
from datetime import datetime

import store
from util import BadRequest, utc_to_ny

ETF = {"NQ": "QQQ", "QQQ": "QQQ", "ES": "SPY", "SPY": "SPY"}
INDEX = {"NQ": "NDX", "QQQ": "NDX", "ES": "SPX", "SPY": "SPX"}
UND = {"etf": ETF, "index": INDEX}
# index options: the PM-settled daily root first (SPXW, NDXP), its ETF for a first guess of the level (x ratio), the
# strike steps near the money / further out. The level itself comes from the options (put-call parity at the money).
IDX = {"SPX": {"etf": "SPY", "roots": ("SPXW", "SPX"), "ratio": 10.03, "step": 5, "far": 25},
       "NDX": {"etf": "QQQ", "roots": ("NDXP", "NDX"), "ratio": 41.0, "step": 10, "far": 50}}
DELTAS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
YEAR_MIN = 365 * 24 * 60


def _key(etf, day, side):
    return f"opt0dte/{etf}/{day}-{side}"


DTES = (0, 1, 2, 3, 5, 7, 10)                      # trading days to expiry (0 = the same day)


def _pack_key(etf, dte=0):
    return f"opt0dte/{etf}/pack" if not dte else f"opt0dte/{etf}/pack-{dte}"


def expiry(day, dte):
    """`dte` weekdays after `day` (0 = the same day). A holiday on that day: the job tries the next weekday."""
    from datetime import date, timedelta
    d = date.fromisoformat(day)
    n = 0
    while n < dte:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return d.isoformat()


_PACK = {}


def pack(etf, dte=0):
    """{"day-side": data} of every downloaded day for that expiry (one stored file, rewritten by the download job)."""
    import time
    hit = _PACK.get((etf, dte))
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    pk = store.get_json(_pack_key(etf, dte), {}) or {}
    _PACK[(etf, dte)] = (time.time(), pk)
    return pk


def _save_pack(etf, pk, dte=0):
    import time
    store.put_json(_pack_key(etf, dte), pk)
    _PACK[(etf, dte)] = (time.time(), pk)


def _have(etf, day, side, dte=0):
    return f"{day}-{side}" in pack(etf, dte)


def occ(etf, exp, side, strike):
    return f"{etf}{exp[2:4]}{exp[5:7]}{exp[8:10]}{side}{int(round(strike * 1000)):08d}"


# ---------------------------------------------------------------- Black-Scholes (no rates, no dividends)

def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, v, call):
    if T <= 0 or v <= 0:
        return max(0.0, S - K) if call else max(0.0, K - S)
    d1 = (math.log(S / K) + 0.5 * v * v * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    return S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)


def delta(S, K, T, price, call):
    """|delta| from the implied volatility of `price` (bisection); 1 / 0 when the price is at / under its bounds."""
    intr = max(0.0, S - K) if call else max(0.0, K - S)
    if price <= intr + 1e-6:
        return 1.0 if intr > 0 else 0.0
    lo, hi = 0.01, 8.0
    if bs(S, K, T, hi, call) < price:
        return 0.5
    for _ in range(60):
        m = (lo + hi) / 2
        if bs(S, K, T, m, call) < price:
            lo = m
        else:
            hi = m
    v = (lo + hi) / 2
    d1 = (math.log(S / K) + 0.5 * v * v * T) / (v * math.sqrt(T))
    return _ncdf(d1) if call else _ncdf(-d1)


# ---------------------------------------------------------------- data

def trade_days(trades, und="etf"):
    """[(underlying, day, side 'C' / 'P')] of the same-day regular-hours trades (und: etf = QQQ / SPY, index = NDX / SPX)."""
    out = set()
    for t in trades:
        if t.get("status") == "open" or not t.get("out") or t["in"][:10] != t["out"][:10] or not ("09:29" <= t["in"][11:16] < "16:00"):
            continue                                    # (an entry at the 09:25 candle's close is stamped 09:29)
        e = UND[und].get(t["s"])
        if e:
            out.add((e, t["in"][:10], "C" if t["side"] == "long" else "P"))
    return sorted(out, key=lambda x: (x[1], x[0], x[2]))


_ED = {}


def _etf_days(etf):
    import stockdata
    b = stockdata.bars(etf)
    hit = _ED.get(etf)
    if hit and hit[0] == (len(b), b[-1][0] if b else None):
        return hit[1]
    by = {}
    for x in b:
        by.setdefault(x[0][:10], []).append(x)
    _ED[etf] = ((len(b), b[-1][0] if b else None), by)
    return by


def etf_price(etf, day, hm):
    """The ETF's price at hh:mm: the open of the 5-minute bar starting then (else the last close before)."""
    bars = _etf_days(etf).get(day) or []
    for b in bars:
        if b[0][11:16] >= hm:
            return b[1] if b[0][11:16] == hm else b[4]
    return bars[-1][4] if bars else None


def fetch_day(sub, etf, day, side, dte=0):
    """`day`'s minute prices of the contracts expiring `dte` weekdays later on `side`, strikes around the open: ±1.2% in
    $1 steps for 0DTE; for later expiries ±1.5% in $1 steps, then $2 steps out to ±(1.5% + 1% per day to expiry, up to
    5%) (deltas 0.1-0.9 sit further out when there's more time). Returned; the job stores them in the pack."""
    import uwhales
    if etf in IDX:
        return fetch_index_day(sub, etf, day, side, dte)
    px = etf_price(etf, day, "09:30")
    if not px:
        return {"none": "no ETF price"}
    tok = uwhales.token(sub)
    if not dte:
        ks = list(range(math.floor(px * 0.988), math.ceil(px * 1.012) + 1))
    else:
        near, far = 0.015, min(0.05, 0.015 + 0.01 * dte)
        ks = sorted(set(range(math.floor(px * (1 - near)), math.ceil(px * (1 + near)) + 1))
                    | set(range(2 * math.floor(px * (1 - far) / 2), math.ceil(px * (1 + far)) + 1, 2)))
    for exp in (expiry(day, dte), expiry(day, dte + 1)) if dte else (day,):   # a holiday on the expiry: the next weekday
        out = {"open": px, "exp": exp, "strikes": {}}
        miss = 0
        for k in ks:
            try:
                r = uwhales._get_retry(tok, f"option-contract/{occ(etf, exp, side, k)}/intraday", {"date": day})
            except Exception as e:                          # not listed (no such strike / no expiry that day)
                print("opt0dte", etf, day, exp, side, k, e)
                miss += 1
                if miss >= 4 and not out["strikes"]:       # 4 misses in a row and nothing yet: no contracts for this expiry
                    break
                continue
            rows = []
            for m in r.get("data") or []:
                t = utc_to_ny(datetime.strptime(str(m["start_time"])[:19], "%Y-%m-%dT%H:%M:%S")).strftime("%H:%M")
                o, c = uwhales._num(m.get("open")), uwhales._num(m.get("close"))
                if c:
                    rows.append([t, o or c, c])
            sl = _slots(rows)
            if sl:
                out["strikes"][str(k)] = sl
        if out["strikes"]:
            return out
    return out


def _minute_rows(r):
    import uwhales
    rows = []
    for m in r.get("data") or []:
        t = utc_to_ny(datetime.strptime(str(m["start_time"])[:19], "%Y-%m-%dT%H:%M:%S")).strftime("%H:%M")
        o, c = uwhales._num(m.get("open")), uwhales._num(m.get("close"))
        if c:
            rows.append([t, o or c, c])
    return rows


def _contract(tok, root, exp, side, k, day):
    import uwhales
    try:
        return _slots(_minute_rows(uwhales._get_retry(tok, f"option-contract/{occ(root, exp, side, k)}/intraday", {"date": day})))
    except Exception as e:                              # not listed
        print("opt0dte", root, day, exp, side, k, e)
        return None


def fetch_index_day(sub, idx, day, side, dte=0):
    """SPX / NDX options: the index level from put-call parity at the money (S = K + call - put, each 5-minute slot), then
    the strikes on `side` around it (the same widths as the ETFs, in the index's strike steps)."""
    import uwhales
    sp = IDX[idx]
    px = etf_price(sp["etf"], day, "09:30")
    if not px:
        return {"none": "no ETF price"}
    tok = uwhales.token(sub)
    st = sp["step"]
    k0 = round(px * sp["ratio"] / st) * st
    for exp in (expiry(day, dte), expiry(day, dte + 1)) if dte else (day,):
        for root in sp["roots"]:
            pair = None
            for k in (k0, k0 + st, k0 - st, k0 + 2 * st, k0 - 2 * st):
                c, p = _contract(tok, root, exp, "C", k, day), _contract(tok, root, exp, "P", k, day)
                if c and p and c["o"][0] is not None and p["o"][0] is not None:
                    pair = (k, c, p)
                    break
            if not pair:
                continue
            k, c, p = pair
            S = [round(k + c["c"][i] - p["c"][i], 2) if c["c"][i] is not None and p["c"][i] is not None else None for i in range(78)]
            S0 = round(k + c["o"][0] - p["o"][0], 2)
            near, far = (0.012, 0.012) if not dte else (0.015, min(0.05, 0.015 + 0.01 * dte))
            ks = sorted(set(range(int(math.floor(S0 * (1 - near) / st) * st), int(math.ceil(S0 * (1 + near) / st) * st) + 1, st))
                        | set(range(int(math.floor(S0 * (1 - far) / sp["far"]) * sp["far"]), int(math.ceil(S0 * (1 + far) / sp["far"]) * sp["far"]) + 1, sp["far"])))
            out = {"open": S0, "S": S, "root": root, "exp": exp, "strikes": {}}
            have = {k: (c if side == "C" else p)}
            for kk in ks:
                sl = have.get(kk) or _contract(tok, root, exp, side, kk, day)
                if sl:
                    out["strikes"][str(kk)] = sl
            if out["strikes"]:
                return out
    return {"none": f"no {idx} options found for {day}"}


def _slots(rows):
    """Minute rows [[hh:mm, open, close]] -> {"o": [first open in each 5-minute slot], "c": [last close in each]} for
    the 78 slots 09:30-15:55 (None where it didn't trade): small enough to keep 2 years in one file."""
    o, c = [None] * 78, [None] * 78
    for t, op, cl in sorted(rows):
        i = (_mins(t) - 570) // 5
        if 0 <= i < 78:
            if o[i] is None:
                o[i] = round(op, 2)
            c[i] = round(cl, 2)
    return {"o": o, "c": c} if any(x is not None for x in c) else None


def coverage(trades, dte=0, und="etf"):
    need = trade_days(trades, und)
    have = sum(1 for e, d, s in need if _have(e, d, s, dte))
    return {"need": len(need), "have": have, "from": need[0][1] if need else None, "to": need[-1][1] if need else None}


def job(sub, trades, prog, dl, invoke, event, dte=0, und="etf"):
    """Download the missing days (resumes in a new run when the time runs out)."""
    import time
    import stockdata
    need = trade_days(trades, und)
    for e in sorted({IDX[x[0]]["etf"] if x[0] in IDX else x[0] for x in need}):
        stockdata.ensure(sub, e, 520)
    packs = {e: dict(pack(e, dte)) for e in {x[0] for x in need}}
    todo = [x for x in need if f"{x[1]}-{x[2]}" not in packs[x[0]]]
    dirty = set()
    try:
        for i, (e, d, s) in enumerate(todo):
            if time.time() > dl - 120:
                invoke(event)
                return False
            prog(f"{dte}DTE options: {e} {d} {'calls' if s == 'C' else 'puts'} ({len(need) - len(todo) + i + 1}/{len(need)})")
            try:
                old = store.get_json(_key(e, d, s)) if not dte and e not in IDX else None   # a day stored on its own by build 86 (minute lists)
                if old is not None and isinstance(old.get("strikes"), dict):
                    old["strikes"] = {k: v for k, v in ((k, _slots(v) if isinstance(v, list) else v) for k, v in old["strikes"].items()) if v}
                packs[e][f"{d}-{s}"] = old if old is not None else fetch_day(sub, e, d, s, dte)
            except Exception as ex:                    # one bad day doesn't stop the download
                print("opt0dte day failed", e, d, s, ex)
                packs[e][f"{d}-{s}"] = {"none": str(ex)[:120]}
            dirty.add(e)
            if (i + 1) % 20 == 0:                       # save as it goes (a crash keeps what's done)
                for x in dirty:
                    _save_pack(x, packs[x], dte)
                dirty = set()
    finally:
        for x in dirty:
            _save_pack(x, packs[x], dte)
    return True


# ---------------------------------------------------------------- the replay

def _mins(hm):
    return int(hm[:2]) * 60 + int(hm[3:5])


def replay(trades, slip=0.02, comm=0.65, budget=1000.0, deltas=DELTAS, tf=5, dte=0, keep=False, und="etf"):
    import nightkit as N
    rows = {d: [] for d in deltas}
    skipped = 0
    for t in trades:
        if t.get("status") == "open" or not t.get("out") or t["in"][:10] != t["out"][:10] or not ("09:29" <= t["in"][11:16] < "16:00"):
            continue
        e = UND[und].get(t["s"])
        side = "C" if t["side"] == "long" else "P"
        day = t["in"][:10]
        data = pack(e, dte).get(f"{day}-{side}") if e else None
        if not data or not data.get("strikes"):
            skipped += 1
            continue
        try:
            got = _one(t, e, day, side, data, tf, deltas, slip, comm, budget, N)
        except Exception as ex:                        # a malformed day: skip it, count it
            print("opt0dte replay", day, ex)
            got = None
        if not got:
            skipped += 1
            continue
        for dt_, row in got:
            rows[dt_].append(row)
    if keep:
        return rows
    return _summary(rows, deltas, skipped, slip, comm, budget)


def _one(t, e, day, side, data, tf, deltas, slip, comm, budget, N):
    hin = max(t["in"][11:16], "09:30")
    hout = N.K.end_of(t["out"], tf)[11:16] if len(t["out"]) >= 16 else t["out"][11:16]
    if data.get("S"):                                   # an index: its level from put-call parity at that slot
        i0 = (_mins(hin) - 570) // 5                     # the same moment as the option's entry price (see below)
        S = (data["S"][i0 - 1] if 0 < i0 <= 78 else None) or data["open"]
    else:
        S = etf_price(e, day, hin) or data["open"]
    from datetime import date
    days_left = (date.fromisoformat(data.get("exp") or day) - date.fromisoformat(day)).days    # calendar days to expiry
    T = max(1, (16 * 60) - (int(hin[:2]) * 60 + int(hin[3:5])) + days_left * 1440) / YEAR_MIN
    cands = []
    i_in, i_out = (_mins(hin) - 570) // 5, (_mins(hout) - 570) // 5
    if not (0 <= i_in < 78):
        return None
    for k, sl in data["strikes"].items():
        o, c = sl["o"], sl["c"]
        # entry: at 09:30 the first trade of that slot; later, the last price of the slot before (known then)
        ent = o[i_in] if i_in == 0 or c[i_in - 1] is None else c[i_in - 1]
        ex = next((c[j] for j in range(min(77, max(i_out, i_in)), i_in - 1, -1) if c[j] is not None), None)
        if ent is None or ex is None or ent < 0.05:
            continue
        cands.append((delta(S, float(k), T, ent, side == "C"), float(k), ent, ex))
    if not cands:
        return None
    got = []
    for dt_ in deltas:
        dl_, k, ent, ex = min(cands, key=lambda c: abs(c[0] - dt_))
        if abs(dl_ - dt_) > 0.12:                     # nothing close enough to this delta that day
            continue
        buy, sell = ent + slip, max(0.0, ex - slip)
        per = (sell - buy) * 100 - 2 * comm
        n = max(1, int(budget // (buy * 100)))
        got.append((dt_, {"d": day, "side": t["side"], "k": k, "delta": dl_, "in": buy, "out": sell, "raw": (ent, ex), "pct": (sell - buy) / buy * 100,
                          "pnl1": per, "pnlB": per * n, "n": n, "under": t.get("r")}))
    return got


def _summary(rows, deltas, skipped, slip, comm, budget):
    res = []
    for dt_ in deltas:
        r = rows[dt_]
        if not r:
            res.append({"delta": dt_, "n": 0})
            continue
        w = [x for x in r if x["pnl1"] > 0]
        gp, gl = sum(x["pnlB"] for x in r if x["pnlB"] > 0), -sum(x["pnlB"] for x in r if x["pnlB"] < 0)
        eq = peak = dd = 0.0
        for x in sorted(r, key=lambda x: x["d"]):
            eq += x["pnlB"]
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        res.append({"delta": dt_, "n": len(r), "win": len(w) / len(r) * 100, "avgPct": sum(x["pct"] for x in r) / len(r),
                    "medPct": sorted(x["pct"] for x in r)[len(r) // 2], "avgPrem": sum(x["in"] for x in r) / len(r),
                    "avgDelta": sum(x["delta"] for x in r) / len(r), "pnl1": sum(x["pnl1"] for x in r), "pnlB": sum(x["pnlB"] for x in r),
                    "pf": gp / gl if gl else None, "dd": dd, "best": max(x["pct"] for x in r), "worst": min(x["pct"] for x in r)})
    return {"rows": res, "skipped": skipped, "slip": slip, "comm": comm, "budget": budget, "trades": {str(k): v[-60:] for k, v in rows.items()}}


def check(trades):
    if not trade_days(trades):
        raise BadRequest("No same-day regular-hours trades on NQ / ES / QQQ / SPY in this study.")


# ---------------------------------------------------------------- futures vs options, at the same risk

def _stats(v):
    """Per-trade P/L list (same $ risk each) -> n, total, mean, t, median, without the 5 best, max drawdown."""
    n = len(v)
    if not n:
        return {"n": 0}
    m = sum(v) / n
    sd = (sum((x - m) ** 2 for x in v) / (n - 1)) ** 0.5 if n > 1 else 0
    eq = pk = dd = 0.0
    for x in v:
        eq += x
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    return {"n": n, "total": sum(v), "mean": m, "t": m / (sd / n ** 0.5) if sd else 0, "median": sorted(v)[n // 2],
            "no5": sum(sorted(v)[:-5]) if n > 5 else 0, "dd": dd, "win": sum(1 for x in v if x > 0) / n * 100}


def _quarters(days_pnl):
    q = {}
    for d, x in days_pnl:
        k = f"{d[:4]} Q{(int(d[5:7]) - 1) // 3 + 1}"
        q[k] = q.get(k, 0) + x
    return q


def compare(trades, slip=0.02, comm=0.65, risk=1000.0, train=0.6, tf=5):
    """One row per version (the futures, then each downloaded expiry at its best train delta), every trade sized to lose
    `risk` at most: futures at `risk` / the stop's $ risk contracts (fractions allowed), options at `risk` of premium.
    Each expiry's delta is chosen on the train trades only (the first `train` share, by date), like the validation."""
    import futdata
    done = [t for t in trades if t.get("status") != "open" and t.get("out") and t.get("net") is not None]
    done.sort(key=lambda t: t["in"])
    if not done:
        return {"rows": []}
    cut = done[int(len(done) * train)]["in"][:10]
    out = []
    fr, fr2 = [], []                                   # futures: net $ scaled to `risk` on the stop's $ risk
    for t in done:
        sp = futdata.SPEC.get(t["s"]) or {"pv": 1}
        c = t.get("c") or 1
        rd = (t.get("risk") or 0) * sp["pv"] * c
        if rd <= 0:
            continue
        k = risk / rd
        fr.append((t["in"][:10], t["net"] * k))
        fr2.append((t["in"][:10], (t["net"] - (t.get("cost") or 0)) * k))
    out.append(_row("Futures (" + ", ".join(sorted({t["s"] for t in done})) + ")", None, None, fr, fr2, cut))
    sized = lambda r, sl, cm: [(x["d"], ((max(0.0, x["raw"][1] - sl) - (x["raw"][0] + sl)) * 100 - 2 * cm) / ((x["raw"][0] + sl) * 100) * risk) for x in r]
    for und in ("etf", "index"):
        unds = {UND[und].get(t["s"]) for t in done} - {None}
        for dte in DTES:                               # each underlying / expiry with downloaded prices, at its best train delta
            if not any(pack(e, dte) for e in unds):
                continue
            rows = replay(done, slip, comm, risk, DELTAS, tf, dte, keep=True, und=und)
            best, bt = None, None
            most = max(len([1 for x in rows[dl_] if x["d"] < cut]) for dl_ in DELTAS)
            for dl_ in DELTAS:
                tr = [v for d, v in sized(rows[dl_], slip, comm) if d < cut]
                if tr and len(tr) >= min(20, most) and (bt is None or sum(tr) > bt):
                    best, bt = dl_, sum(tr)
            if best is None:
                continue
            out.append(_row(f"{'/'.join(sorted(unds))} {'same-day (0DTE)' if not dte else f'{dte} days out'} options", dte, best,
                            sized(rows[best], slip, comm), sized(rows[best], 2 * slip, 2 * comm), cut))
    return {"rows": out, "cut": cut, "risk": risk, "slip": slip, "comm": comm}


def _row(name, dte, dl_, pnl, pnl2, cut):
    tr = [x for d, x in pnl if d < cut]
    te = [x for d, x in pnl if d >= cut]
    te2 = [x for d, x in pnl2 if d >= cut]
    q = _quarters(pnl)
    return {"name": name, "dte": dte, "delta": dl_, "train": _stats(tr), "test": _stats(te), "test2x": sum(te2) if te2 else None,
            "worstQ": list(min(q.items(), key=lambda kv: kv[1])) if q else None, "posQ": sum(1 for v in q.values() if v > 0), "nQ": len(q)}
