"""Market context for the day trading (QQQ / SPY), loaded per day and kept for good (PK "MCTX", SK date):

* Unusual Whales, per ticker: net call / put premium minute by minute (net-prem-ticks), dealer gamma exposure minute
  by minute (spot-exposures, $ per 1% move) and the large sweeps (flow alerts >= $250K, per 5 minutes);
* Alpaca, per 5 minutes: breadth = the share of a basket (11 sector ETFs + 8 mega caps) above their session VWAP,
  and VIXY (volatility ETF) with its previous close;
* Yahoo, per day: VIX / VIX3M of the previous close (term structure; > 1 = backwardation, fear);
* calendar: FOMC, CPI, jobs report, monthly options expiration, quarter end;
* intraday walls / flip: the snapshots recorded every 30 minutes (flowdata.flip_snapshot), going forward.

formula.py reads all of it (callPrem, netPremDay, gex, breadth, vixy, vixTerm, fomc, onMove...). Everything is aligned
so that a candle only sees what was known at its close. A loader job fills the missing days in the background."""
import json
import time
from datetime import date as _date, datetime, timedelta

import db
from util import iso, now_ny, ny_to_utc, utc_to_ny

PK = "MCTX"
SECTORS = ["XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC"]
MEGA = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA"]
TICKERS = ("QQQ", "SPY")
N1, N5 = 390, 78
SWEEP_MIN = 250_000
SK_JOB = "MCTXJOB"

# ---------------------------------------------------------------- calendar (announcement days, ET)
FOMC = {"2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
        "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
        "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
        "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"}
CPI = {"2023-01-12", "2023-02-14", "2023-03-14", "2023-04-12", "2023-05-10", "2023-06-13", "2023-07-12", "2023-08-10",
       "2023-09-13", "2023-10-12", "2023-11-14", "2023-12-12",
       "2024-01-11", "2024-02-13", "2024-03-12", "2024-04-10", "2024-05-15", "2024-06-12", "2024-07-11", "2024-08-14",
       "2024-09-11", "2024-10-10", "2024-11-13", "2024-12-11",
       "2025-01-15", "2025-02-12", "2025-03-12", "2025-04-10", "2025-05-13", "2025-06-11", "2025-07-15", "2025-08-12",
       "2025-09-11", "2025-10-24", "2025-12-18",
       "2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
       "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10"}


def _first_friday(y, m):
    d = _date(y, m, 1)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def event_flags(day, trading_days=None):
    """{"fomc", "cpi", "nfp", "opex", "qend"} -> 0 / 1 for a date. NFP = the first Friday of the month (the usual
    release day; a few were moved), OPEX = the third Friday, quarter end = the last trading day of Mar / Jun / Sep /
    Dec (from trading_days when given, else the last weekday)."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    ff = _first_friday(d.year, d.month)
    out = {"fomc": float(day in FOMC), "cpi": float(day in CPI), "nfp": float(d == ff), "opex": float(d == ff + timedelta(days=14))}
    qe = 0.0
    if d.month in (3, 6, 9, 12):
        if trading_days:
            same = [x for x in trading_days if x[:7] == day[:7]]
            qe = float(bool(same) and day == max(same))
        else:
            nm = _date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)
            while nm.weekday() > 4:
                nm -= timedelta(days=1)
            qe = float(d == nm)
    out["qend"] = qe
    out["eventDay"] = float(any(out[k] for k in ("fomc", "cpi", "nfp")))
    return out


# ---------------------------------------------------------------- storage
def _enc(v):
    return ",".join("" if x is None else (str(int(x)) if float(x).is_integer() else "%.6g" % x) for x in v)


def _dec(s):
    return [None if x == "" else float(x) for x in (s or "").split(",")] if s else []


def get(d):
    r = db.get(PK, d) or {}
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


def put(d, fields):
    cur = db.get(PK, d) or {"PK": PK, "SK": d}
    db.put({**cur, **fields, "at": int(time.time())})


def for_symbol(sym, dates):
    """{date: {"c", "p", "g", "sc", "sp", "br", "brS", "brM", "vx", "vxp", "term"}} for the dates that are loaded."""
    tm = vix_term_map()
    out = {}
    for d in dates:
        r = get(d)
        if not r:
            continue
        x = {"term": tm.get(d)}
        for k in ("br", "brS", "brM", "vx"):
            if r.get(k):
                x[k] = _dec(r[k])
        if r.get("vxp") is not None:
            x["vxp"] = float(r["vxp"])
        for k in ("c", "p", "g", "sc", "sp"):
            if r.get(f"{sym}_{k}"):
                x[k] = _dec(r[f"{sym}_{k}"])
        out[d] = x
    return out


def _uw_ok(r):
    try:
        return any(v == "ok" for v in json.loads(r.get("uw") or "{}").values())
    except ValueError:
        return False


def coverage(dates):
    """Days with data (not just tried): Unusual Whales flow / gamma, Alpaca breadth / VIXY."""
    have = {"uw": 0, "alp": 0}
    for d in dates:
        r = db.get(PK, d) or {}
        have["uw"] += _uw_ok(r)
        have["alp"] += r.get("alp") == "ok"
    return {**have, "days": len(dates)}


# ---------------------------------------------------------------- Yahoo: VIX term structure
def vix_term_map():
    """{day: previous close of VIX / VIX3M} (cached 6 hours)."""
    import charts
    hit = db.get("VIXD", "term")
    if hit and time.time() - hit.get("at", 0) < 6 * 3600 and hit.get("m"):
        return {k: float(v) for k, v in hit["m"].items()}
    end = now_ny().replace(tzinfo=None)
    try:
        a = {b["t"][:10]: b["c"] for b in charts._yahoo("^VIX", "1d", end - timedelta(days=1100), end + timedelta(days=1)) or []}
        b3 = {b["t"][:10]: b["c"] for b in charts._yahoo("^VIX3M", "1d", end - timedelta(days=1100), end + timedelta(days=1)) or []}
    except Exception as e:
        print("vix term failed", e)
        return {}
    ds, m, prev = sorted(set(a) & set(b3)), {}, None
    for d in ds:
        if prev is not None:
            m[d] = prev
        prev = round(a[d] / b3[d], 4) if b3[d] else None
    if prev is not None:
        m[(datetime.strptime(ds[-1], "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")] = prev
    from decimal import Decimal
    db.put({"PK": "VIXD", "SK": "term", "at": int(time.time()), "m": {k: Decimal(str(v)) for k, v in m.items() if v is not None}})
    return m


# ---------------------------------------------------------------- Unusual Whales
def _minute(t_utc):
    """'2024-05-01T13:31:00Z' (UTC) -> minutes since 09:30 NY, or None."""
    try:
        t = utc_to_ny(datetime.strptime(str(t_utc)[:19].replace(" ", "T"), "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    m = t.hour * 60 + t.minute - 570
    return m if 0 <= m < N1 else None


def uw_day(key, sym, d):
    """{"c", "p": net call / put premium per minute, "g": gamma per minute (carried forward), "sc", "sp": sweep
    premium per 5 minutes} or None when Unusual Whales has nothing for that day."""
    import flowdata
    import urllib.parse
    q = urllib.parse.quote(sym)
    c, p, g = [0.0] * N1, [0.0] * N1, [None] * N1
    got = False
    r = flowdata._uw(key, f"/api/stock/{q}/net-prem-ticks", {"date": d}) or {}
    for t in (r.get("data") if isinstance(r, dict) else r) or []:
        if not isinstance(t, dict):
            continue
        m = _minute(t.get("tape_time") or t.get("time") or t.get("timestamp"))
        if m is None:
            continue
        c[m] += flowdata._pick(t, "net_call_premium", "call_premium") or 0
        p[m] += flowdata._pick(t, "net_put_premium", "put_premium") or 0
        got = True
    r = flowdata._uw(key, f"/api/stock/{q}/spot-exposures", {"date": d}) or {}
    for t in (r.get("data") if isinstance(r, dict) else r) or []:
        if not isinstance(t, dict):
            continue
        m = _minute(t.get("time") or t.get("timestamp") or t.get("tape_time"))
        v = flowdata._pick(t, "gamma_per_one_percent_move_oi", "gamma_per_one_percent_move_vol", "gamma_exposure", "gamma")
        if m is not None and v is not None:
            g[m] = v
            got = True
    last = None
    for i in range(N1):                                # carried forward: the last value known at that minute
        if g[i] is None:
            g[i] = last
        else:
            last = g[i]
    sc, sp = [0.0] * N5, [0.0] * N5
    day0 = datetime.strptime(d, "%Y-%m-%d")
    r = flowdata._uw(key, "/api/option-trades/flow-alerts", {
        "ticker_symbol": sym, "is_sweep": "true", "min_premium": SWEEP_MIN, "limit": 200,
        "newer_than": ny_to_utc(day0.replace(hour=9, minute=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "older_than": ny_to_utc(day0.replace(hour=16)).strftime("%Y-%m-%dT%H:%M:%SZ")}) or {}
    for a in (r.get("data") if isinstance(r, dict) else r) or []:
        if not isinstance(a, dict):
            continue
        m = _minute(a.get("created_at") or a.get("executed_at"))
        prem = flowdata._pick(a, "total_premium", "premium") or 0
        if m is None or prem < SWEEP_MIN:
            continue
        (sc if str(a.get("type") or a.get("option_type") or "").lower().startswith("c") else sp)[m // 5] += prem
        got = True
    return {"c": c, "p": p, "g": g, "sc": sc, "sp": sp} if got else None


# ---------------------------------------------------------------- Alpaca: breadth + VIXY (5-minute candles)
def _vwap_above(bars):
    """[(bucket, above)] for one symbol's session 5-minute candles."""
    out, pv, vv = [], 0.0, 0.0
    for b in bars:
        v = float(b.get("v") or 0)
        pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        vv += v
        k = (int(b["t"][11:13]) * 60 + int(b["t"][14:16]) - 570) // 5
        if 0 <= k < N5 and vv:
            out.append((k, b["c"] > pv / vv))
    return out


def alpaca_days(c, days, feed="sip"):
    """{date: {"br", "brS", "brM", "vx", "vxp"}} for consecutive trading days (one request series)."""
    import stockday
    syms = SECTORS + MEGA + ["VIXY"]
    d0, d1 = datetime.strptime(days[0], "%Y-%m-%d"), datetime.strptime(days[-1], "%Y-%m-%d")
    bars = stockday.multi_bars(c, syms, "5Min", d0.replace(hour=9, minute=30) - timedelta(days=5), d1.replace(hour=16), feed=feed)
    by = {}
    for s, bs in bars.items():
        for b in bs:
            if "09:30" <= b["t"][11:16] < "16:00":
                by.setdefault((s, b["t"][:10]), []).append(b)
    allds = sorted({d for _, d in by})
    out = {}
    for d in days:
        if not any((s, d) in by for s in SECTORS):
            continue
        cnt = {g: [[0, 0] for _ in range(N5)] for g in ("S", "M")}
        for g, group in (("S", SECTORS), ("M", MEGA)):
            for s in group:
                for k, ab in _vwap_above(by.get((s, d)) or []):
                    cnt[g][k][0] += ab
                    cnt[g][k][1] += 1
        f = lambda xs: [round(a / n, 3) if n else None for a, n in xs]
        both = [[cnt["S"][k][0] + cnt["M"][k][0], cnt["S"][k][1] + cnt["M"][k][1]] for k in range(N5)]
        vx = [None] * N5
        for b in by.get(("VIXY", d)) or []:
            k = (int(b["t"][11:13]) * 60 + int(b["t"][14:16]) - 570) // 5
            if 0 <= k < N5:
                vx[k] = b["c"]
        prev = [x for x in allds if x < d]
        pb = by.get(("VIXY", prev[-1])) if prev else None
        out[d] = {"br": f(both), "brS": f(cnt["S"]), "brM": f(cnt["M"]), "vx": vx, "vxp": pb[-1]["c"] if pb else None}
    return out


# ---------------------------------------------------------------- loader (background job, resumable)
def _days(c, n):
    import stockday
    end = now_ny().date()
    ds = stockday.trading_days(c, (end - timedelta(days=int(n * 1.5) + 10)).isoformat(), end.isoformat())
    today = end.isoformat()
    ds = [d for d in ds if d < today or now_ny().strftime("%H:%M") >= "16:20"]
    return ds[-n:]


def load(sub, n=250, budget_s=420, invoke_next=None, retry=False):
    """Fills the missing days (newest first). Returns {"done", "left"}."""
    import flowdata
    import stockday
    t0 = time.time()
    c = stockday.data_creds(sub)
    if not c:
        return {"error": "no Alpaca data account"}
    try:
        key = flowdata._key(sub)
    except Exception:
        key = None
    days = _days(c, n)
    have = {d: db.get(PK, d) or {} for d in days}
    miss_a = [d for d in days if not have[d].get("alp") or (retry and have[d].get("alp") != "ok")]
    done = 0
    for k in range(0, len(miss_a), 15):                 # Alpaca: 15 days per request series
        if time.time() - t0 > budget_s * 0.4:
            break
        chunk = miss_a[k:k + 15]
        try:
            res = alpaca_days(c, chunk, c.get("feed", "sip") if c.get("feed") != "iex" else "iex")
        except Exception as e:
            print("breadth load failed", chunk[0], e)
            try:
                res = alpaca_days(c, chunk, "iex")
            except Exception as e2:
                print("breadth iex failed", e2)
                continue
        for d in chunk:
            x = res.get(d)
            fields = {"alp": "ok" if x else "none"}
            if x:
                fields.update({k2: _enc(v) for k2, v in x.items() if isinstance(v, list)})
                if x.get("vxp") is not None:
                    from decimal import Decimal
                    fields["vxp"] = Decimal(str(round(x["vxp"], 4)))
            put(d, fields)
            have[d] = {**have[d], **fields}
            done += 1
    if key:
        for d in sorted(days, reverse=True):
            if time.time() - t0 > budget_s:
                break
            if have[d].get("uw") and (_uw_ok(have[d]) or not retry):
                continue
            fields, st = {}, {}
            for sym in TICKERS:
                try:
                    x = uw_day(key, sym, d)
                except Exception as e:
                    print("uw context failed", sym, d, e)
                    x = None
                st[sym] = "ok" if x else "none"
                if x:
                    fields.update({f"{sym}_{k2}": _enc(v) for k2, v in x.items()})
            put(d, {**fields, "uw": json.dumps(st)})
            done += 1
    rows = {d: db.get(PK, d) or {} for d in days}
    left = sum(1 for d in days if not rows[d].get("alp") or (key and not rows[d].get("uw")))
    cov = {"uw": sum(_uw_ok(r) for r in rows.values()), "alp": sum(r.get("alp") == "ok" for r in rows.values())}
    db.put({"PK": db.upk(sub), "SK": SK_JOB, "at": iso(now_ny()), "running": iso(now_ny()) if left and invoke_next and done else None,
            "left": left, "days": len(days), "cov": cov, "uwKey": bool(key), "retry": retry if left else False})
    if left and invoke_next and done:
        invoke_next()
    return {"done": done, "left": left, "days": len(days), "cov": cov}


def job(sub, params=None):
    import os
    import boto3
    n = int((params or {}).get("days") or 250)
    retry = bool((params or {}).get("retry"))

    def nxt():
        fn = os.environ.get("BOT_FUNCTION") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        if fn:
            boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event",
                                          Payload=json.dumps({"sub": sub, "job": "mctx", "params": {"days": n, "retry": retry}}))
    return load(sub, n, invoke_next=nxt, retry=retry)


def job_state(sub):
    r = db.get(db.upk(sub), SK_JOB) or {}
    if r.get("running") and r["running"] < iso(now_ny() - timedelta(minutes=15)):
        r = {**r, "running": None}
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


def kick(sub, n=250, retry=False):
    """Start the loader unless it is running (called by the research runs)."""
    import os
    import boto3
    st = job_state(sub)
    if st.get("running"):
        return False
    db.put({"PK": db.upk(sub), "SK": SK_JOB, **st, "running": iso(now_ny())})
    fn = os.environ.get("BOT_FUNCTION") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if fn:
        boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event",
                                      Payload=json.dumps({"sub": sub, "job": "mctx", "params": {"days": n, "retry": retry}}))
    return True


def live_day(sub, sym, d):
    """Today's context for the live tick (cached 2 minutes): Unusual Whales flow / gamma / sweeps + breadth / VIXY."""
    import flowdata
    import stockday
    hit = db.get(PK, f"LIVE#{d}#{sym}")
    if hit and time.time() - hit.get("at", 0) < 120 and hit.get("data"):
        return json.loads(hit["data"])
    x = {"term": vix_term_map().get(d)}
    try:
        u = uw_day(flowdata._key(sub), sym, d)
        if u:
            x.update(u)
    except Exception as e:
        print("live uw failed", e)
    try:
        c = stockday.data_creds(sub)
        a = alpaca_days(c, [d], "iex") if c else {}
        if a.get(d):
            x.update(a[d])
    except Exception as e:
        print("live breadth failed", e)
    db.put({"PK": PK, "SK": f"LIVE#{d}#{sym}", "at": int(time.time()), "data": json.dumps(x)})
    return x


def snaps_map(sym):
    """{date: [(hm, putWall, callWall, flip)]} of the intraday walls recorded (every 30 minutes, going forward)."""
    import flowdata
    out = {}
    for r in flowdata.flip_snapshots(sym):
        out.setdefault(r.get("date"), []).append((r.get("hm"), r.get("putWall"), r.get("callWall"), r.get("flip")))
    for v in out.values():
        v.sort(key=lambda x: x[0] or "")
    return out
