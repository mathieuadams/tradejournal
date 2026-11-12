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

def trade_days(trades):
    """[(etf, day, side 'C' / 'P')] of the same-day regular-hours trades."""
    out = set()
    for t in trades:
        if t.get("status") == "open" or not t.get("out") or t["in"][:10] != t["out"][:10] or not ("09:29" <= t["in"][11:16] < "16:00"):
            continue                                    # (an entry at the 09:25 candle's close is stamped 09:29)
        e = ETF.get(t["s"])
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


def coverage(trades, dte=0):
    need = trade_days(trades)
    have = sum(1 for e, d, s in need if _have(e, d, s, dte))
    return {"need": len(need), "have": have, "from": need[0][1] if need else None, "to": need[-1][1] if need else None}


def job(sub, trades, prog, dl, invoke, event, dte=0):
    """Download the missing days (resumes in a new run when the time runs out)."""
    import time
    import stockdata
    need = trade_days(trades)
    for e in sorted({x[0] for x in need}):
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
                old = store.get_json(_key(e, d, s)) if not dte else None   # a day stored on its own by build 86 (minute lists)
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


def replay(trades, slip=0.02, comm=0.65, budget=1000.0, deltas=DELTAS, tf=5, dte=0):
    import nightkit as N
    rows = {d: [] for d in deltas}
    skipped = 0
    for t in trades:
        if t.get("status") == "open" or not t.get("out") or t["in"][:10] != t["out"][:10] or not ("09:29" <= t["in"][11:16] < "16:00"):
            continue
        e = ETF.get(t["s"])
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
    return _summary(rows, deltas, skipped, slip, comm, budget)


def _one(t, e, day, side, data, tf, deltas, slip, comm, budget, N):
    hin = max(t["in"][11:16], "09:30")
    hout = N.K.end_of(t["out"], tf)[11:16] if len(t["out"]) >= 16 else t["out"][11:16]
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
        got.append((dt_, {"d": day, "side": t["side"], "k": k, "delta": dl_, "in": buy, "out": sell, "pct": (sell - buy) / buy * 100,
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
