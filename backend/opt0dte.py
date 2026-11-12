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


def occ(etf, day, side, strike):
    return f"{etf}{day[2:4]}{day[5:7]}{day[8:10]}{side}{int(round(strike * 1000)):08d}"


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


def etf_price(etf, day, hm):
    """The ETF's price at hh:mm: the open of the 5-minute bar starting then (else the last close before)."""
    import stockdata
    bars = [b for b in stockdata.bars(etf) if b[0][:10] == day]
    for b in bars:
        if b[0][11:16] >= hm:
            return b[1] if b[0][11:16] == hm else b[4]
    return bars[-1][4] if bars else None


def fetch_day(sub, etf, day, side, width=0.02):
    """The minute prices of `day`'s 0DTE contracts on `side`, strikes within `width` of the open (stored)."""
    import uwhales
    if store.get_json(_key(etf, day, side)) is not None:
        return False
    px = etf_price(etf, day, "09:30")
    if not px:
        store.put_json(_key(etf, day, side), {"none": "no ETF price"})
        return True
    tok = uwhales.token(sub)
    lo, hi = math.floor(px * (1 - width)), math.ceil(px * (1 + width))
    out = {"open": px, "strikes": {}}
    for k in range(lo, hi + 1):
        try:
            r = uwhales._get_retry(tok, f"option-contract/{occ(etf, day, side, k)}/intraday", {"date": day})
        except Exception as e:                              # not listed (no such strike / no 0DTE that day)
            print("opt0dte", etf, day, side, k, e)
            continue
        rows = []
        for m in r.get("data") or []:
            t = utc_to_ny(datetime.strptime(str(m["start_time"])[:19], "%Y-%m-%dT%H:%M:%S")).strftime("%H:%M")
            o, c = uwhales._num(m.get("open")), uwhales._num(m.get("close"))
            if c:
                rows.append([t, o or c, c])
        if rows:
            out["strikes"][str(k)] = sorted(rows)
    store.put_json(_key(etf, day, side), out)
    return True


def coverage(trades):
    need = trade_days(trades)
    have = sum(1 for e, d, s in need if store.get_json(_key(e, d, s)) is not None)
    return {"need": len(need), "have": have, "from": need[0][1] if need else None, "to": need[-1][1] if need else None}


def job(sub, trades, prog, dl, invoke, event):
    """Download the missing days (resumes in a new run when the time runs out)."""
    import time
    import stockdata
    need = trade_days(trades)
    for e in sorted({x[0] for x in need}):
        stockdata.ensure(sub, e, 520)
    todo = [x for x in need if store.get_json(_key(*x)) is None]
    for i, (e, d, s) in enumerate(todo):
        if time.time() > dl - 120:
            invoke(event)
            return False
        prog(f"0DTE options: {e} {d} {'calls' if s == 'C' else 'puts'} ({len(need) - len(todo) + i + 1}/{len(need)})")
        fetch_day(sub, e, d, s)
    return True


# ---------------------------------------------------------------- the replay

def _mins(hm):
    return int(hm[:2]) * 60 + int(hm[3:5])


def _at_or_after(rows, hm):
    for r in rows:
        if r[0] >= hm:
            return r
    return None


def _last_before(rows, hm):
    out = None
    for r in rows:
        if r[0] > hm:
            break
        out = r
    return out


def replay(trades, slip=0.02, comm=0.65, budget=1000.0, deltas=DELTAS, tf=5):
    import nightkit as N
    rows = {d: [] for d in deltas}
    skipped = 0
    for t in trades:
        if t.get("status") == "open" or not t.get("out") or t["in"][:10] != t["out"][:10] or not ("09:29" <= t["in"][11:16] < "16:00"):
            continue
        e = ETF.get(t["s"])
        side = "C" if t["side"] == "long" else "P"
        day = t["in"][:10]
        data = store.get_json(_key(e, day, side)) if e else None
        if not data or not data.get("strikes"):
            skipped += 1
            continue
        hin = max(t["in"][11:16], "09:30")
        hout = N.K.end_of(t["out"], tf)[11:16] if len(t["out"]) >= 16 else t["out"][11:16]
        S = etf_price(e, day, hin) or data["open"]
        T = max(1, (16 * 60) - (int(hin[:2]) * 60 + int(hin[3:5]))) / YEAR_MIN
        cands = []
        for k, mins in data["strikes"].items():
            a = _at_or_after(mins, hin)
            b = _last_before(mins, hout)
            if not a or not b or b[0] < a[0] or _mins(a[0]) > _mins(hin) + 5:     # no trade within 5 minutes of the entry
                continue
            ent = a[1]
            if ent < 0.05:
                continue
            cands.append((delta(S, float(k), T, ent, side == "C"), float(k), ent, b[2]))
        if not cands:
            skipped += 1
            continue
        for dt_ in deltas:
            dl_, k, ent, ex = min(cands, key=lambda c: abs(c[0] - dt_))
            if abs(dl_ - dt_) > 0.12:                     # nothing close enough to this delta that day
                continue
            buy, sell = ent + slip, max(0.0, ex - slip)
            per = (sell - buy) * 100 - 2 * comm
            n = max(1, int(budget // (buy * 100)))
            rows[dt_].append({"d": day, "side": t["side"], "k": k, "delta": dl_, "in": buy, "out": sell, "pct": (sell - buy) / buy * 100,
                              "pnl1": per, "pnlB": per * n, "n": n, "under": t.get("r")})
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
