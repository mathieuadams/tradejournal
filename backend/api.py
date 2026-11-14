"""HTTP API (API Gateway v2, Cognito JWT). One Lambda routes every request.

The user id always comes from the verified token (sub claim), never from the request."""
import re
from datetime import date, timedelta

import alpaca
import uwhales
import databento
import db
import futdata
import jobs
import strategy
from util import BadRequest, NotFound, Unavailable, iso, now_ny, read_body, resp

ROUTES = []
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def _day(v, name):
    if not isinstance(v, str) or not DAY.match(v):
        raise BadRequest(f"{name} must be a date (YYYY-MM-DD).")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise BadRequest(f"{name} is not a real date.")
    return v


def _syms(v):
    v = v if isinstance(v, list) else [v]
    out = [futdata.check_sym(s) for s in v if s]
    if not out:
        raise BadRequest("Pick NQ and / or ES.")
    return sorted(set(out))


# ---------------------------------------------------------------- profile

@route("GET", "/me")
def me(sub, claims, body, q):
    pk = db.upk(sub)
    p = db.get(pk, "PROFILE")
    if not p:
        p = {"PK": pk, "SK": "PROFILE", "email": claims.get("email", ""), "createdAt": iso(now_ny())}
        db.put(p)
    return {"email": p.get("email"), "databento": databento.status(sub), "specs": futdata.SPEC}


# ---------------------------------------------------------------- Databento

@route("GET", "/databento")
def db_status(sub, claims, body, q):
    return databento.status(sub)


@route("PUT", "/databento")
def db_save(sub, claims, body, q):
    if "key" in body:
        databento.save_key(sub, body.get("key"))
        try:
            databento.test(sub)
        except Exception as e:
            print("range check failed", e)
    return databento.settings(sub, body) if any(k in body for k in ("auto", "autoFp", "maxCost")) else databento.status(sub)


@route("DELETE", "/databento")
def db_delete(sub, claims, body, q):
    return databento.delete_key(sub)


@route("POST", "/databento/test")
def db_test(sub, claims, body, q):
    return databento.test(sub)


# ---------------------------------------------------------------- Alpaca + fractal waves

@route("GET", "/alpaca")
def alp_status(sub, claims, body, q):
    return alpaca.status(sub)


@route("PUT", "/alpaca")
def alp_save(sub, claims, body, q):
    if body.get("keyId") or body.get("secret"):
        return alpaca.save_key(sub, body.get("keyId"), body.get("secret"), body.get("feed") or "sip")
    return alpaca.settings(sub, body)


@route("DELETE", "/alpaca")
def alp_delete(sub, claims, body, q):
    return alpaca.delete_key(sub)


@route("POST", "/alpaca/test")
def alp_test(sub, claims, body, q):
    return alpaca.test(sub)


def _int(v, name, lo, hi, default):
    try:
        n = int(v if v not in (None, "") else default)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a whole number.")
    if not lo <= n <= hi:
        raise BadRequest(f"{name} must be between {lo} and {hi}.")
    return n


@route("GET", "/fractal")
def fractal_view(sub, claims, body, q):
    import fractal
    wave = _int(q.get("wave"), "Wave", 2, 100, 10)
    days = _int(q.get("days"), "Days to load", 1, 60, 10)
    # Stats days: the history the levels and wave odds are drawn from. TradeWellX uses the waves of the window it shows
    # (its ClusterWave marks), so this defaults to Days to load; a longer history can be set to compare.
    stats = _int(q.get("stats"), "Stats days", 1, 120, days)     # TradeWellX: the waves of the window itself
    stats = max(stats, days)
    agg = _int(q.get("agg"), "Aggregation", 1, 60, 15)
    sym = (q.get("sym") or "QQQ").strip().upper()
    rth_only = q.get("rth") in ("1", "true")
    # extra sessions before the stats history, so the MA, pivot and MacroTrend (50 bars) all exist on its first bar
    warm = 2 + (max(2 * wave, 51) * agg) // 390
    if sym in futdata.SYMS:                            # NQ / ES: the stored Databento 1-minute candles, all hours by default
        bars, source = _fut_bars(sym, stats + warm, agg, rth_only), "databento"
    else:                                              # stocks / ETFs: Alpaca, regular hours
        bars, source = alpaca.bars(sub, sym, stats + warm, agg, close_bar=q.get("close16") not in ("0", "false")), "alpaca"
    dates = sorted({b[0][:10] for b in bars})
    first = lambda d: next((i for i, b in enumerate(bars) if b[0][:10] >= d), 0)
    if not dates:
        raise BadRequest(f"No {sym} bars came back.")
    win_s, win = first(dates[-min(stats, len(dates))]), first(dates[-min(days, len(dates))])
    cold = q.get("cold") in ("1", "true")
    if cold:
        # TradeWellX: no history before the window; the MA starts from the window's own bars, so its first pivot (and
        # first wave) differ from a warmed-up chart, and those moves go into its cluster marks
        bars = bars[min(win_s, win):]
        win_s, win = 0, win - min(win_s, win)
    else:
        if win_s < 2 * wave:
            win_s = min(2 * wave, win)                 # not enough history: start the stats where the MA exists
        if win < 2 * wave:
            raise BadRequest(f"Not enough history before the window for Wave {wave}: lower the wave or the days.")
    macro = q.get("macro") if q.get("macro") in fractal.MACROS else "ema30"
    r = fractal.analyze(bars, wave, win_s, show=win, cold=cold, macro=macro)
    r["bars"] = bars[win:]
    r["params"] = {"sym": sym, "wave": wave, "days": days, "stats": stats, "statsFrom": bars[win_s][0], "cold": cold, "macro": macro, "agg": agg, "source": source, "rth": rth_only or source == "alpaca",
                   "feed": alpaca.status(sub)["feed"] if source == "alpaca" else "GLBX.MDP3"}
    return r


def _fut_bars(sym, days, agg, rth_only):
    """The last `days` calendar dates that have candles (like TradeWellX's NQ=F "days to load"), agg-minute bars."""
    end = now_ny().date()
    one = futdata.bars_between(sym, (end - timedelta(days=days * 2 + 7)).isoformat(), end.isoformat())
    if rth_only:
        one = [b for b in one if futdata.RTH0 <= b[0][11:16] < futdata.RTH1]
    keep = set(sorted({b[0][:10] for b in one})[-days:])
    if not keep:
        raise BadRequest(f"No {sym} candles stored yet: download them on the Data page.")
    return [[b[0][:16].replace("T", " ")] + b[1:6] for b in futdata.agg([b for b in one if b[0][:10] in keep], agg)]


# ---------------------------------------------------------------- Unusual Whales: gamma, call / put walls

@route("GET", "/uw")
def uw_status(sub, claims, body, q):
    return uwhales.status(sub)


@route("PUT", "/uw")
def uw_save(sub, claims, body, q):
    return uwhales.save_key(sub, body.get("token"))


@route("DELETE", "/uw")
def uw_delete(sub, claims, body, q):
    return uwhales.delete_key(sub)


@route("POST", "/uw/test")
def uw_test(sub, claims, body, q):
    return uwhales.test(sub)


@route("GET", "/landscape")
def landscape_view(sub, claims, body, q):
    base = {"NQ": "QQQ", "ES": "SPY"}.get((q.get("sym") or "QQQ").upper(), q.get("sym") or "QQQ")
    r = uwhales.landscape(sub, base, q.get("expiry") or None)
    fut = FUT_FOR.get(r["sym"])
    if fut:
        px, t = _fut_last(fut)
        if px:
            r["fut"] = {"sym": fut, "price": px, "time": t, "ratio": px / r["price"]}
    return r


@route("GET", "/mmhistory")
def mm_history(sub, claims, body, q):
    base = {"NQ": "QQQ", "ES": "SPY"}.get((q.get("sym") or "QQQ").upper(), q.get("sym") or "QQQ")
    days = _int(q.get("days"), "Days", 1, 20, 3)
    r = {"sym": uwhales.check_sym(base), "days": uwhales.history(sub, base, days)}
    fut = FUT_FOR.get(r["sym"])
    if fut:
        px, t = _fut_last(fut)
        last = next((d["rows"][-1]["price"] for d in reversed(r["days"]) if d["rows"]), None)
        if px and last:
            r["fut"] = {"sym": fut, "price": px, "time": t, "ratio": px / last}
    return r


@route("POST", "/mmhistory/backfill")
def mm_backfill(sub, claims, body, q):
    base = {"NQ": "QQQ", "ES": "SPY"}.get((body.get("sym") or "QQQ").upper(), body.get("sym") or "QQQ")
    n = _int(body.get("days"), "Days", 1, 120, 3)
    syms = [uwhales.check_sym(base)] if body.get("only") else list(uwhales.SNAP_SYMS)
    uwhales.token(sub)                                 # connected?
    today, days, d = now_ny().date(), [], now_ny().date()
    while len(days) < n:                               # the last n weekdays before today
        d -= timedelta(days=1)
        if d.weekday() < 5:
            days.append(d.isoformat())
    return jobs.start(sub, "mm", "mmbackfill", {"syms": syms, "days": sorted(days)})


@route("GET", "/mmhistory/status")
def mm_status(sub, claims, body, q):
    return jobs.status(sub, "mm")


@route("POST", "/mmhistory/snapshot")
def mm_snapshot(sub, claims, body, q):
    base = {"NQ": "QQQ", "ES": "SPY"}.get((body.get("sym") or "QQQ").upper(), body.get("sym") or "QQQ")
    return uwhales.snapshot(sub, base)


@route("GET", "/volproj")
def volproj_view(sub, claims, body, q):
    import volproj
    sym = (q.get("sym") or "QQQ").strip().upper()
    base = {"NQ": "QQQ", "ES": "SPY"}.get(sym, sym)
    wave = _int(q.get("wave"), "Wave", 1, 50, 1)
    days = _int(q.get("days"), "Aggregations", 30, 1000, 200)
    fwd = _int(q.get("fwd"), "Forward looking days", 1, 10, 1)
    match = "weekday" if q.get("match") == "weekday" else "hl"
    bars = alpaca.daily(sub, base, days + 2 * wave + 5)
    try:
        r = volproj.project(bars[-(days + 2 * wave):] if len(bars) > days else bars, wave, fwd, match, q.get("asof") or None)
    except ValueError as e:
        raise BadRequest(str(e))
    r["sym"] = base
    fut = FUT_FOR.get(base) if sym != base else None
    if fut:
        px, t = _fut_last(fut)
        if px:
            r["fut"] = {"sym": fut, "price": px, "time": t, "ratio": px / bars[-1][4]}
    return r


FUT_FOR = {"QQQ": "NQ", "SPY": "ES"}


def _fut_last(fut):
    """The futures' latest stored 1-minute close and its time (None when no candles are stored)."""
    end = now_ny().date()
    one = futdata.bars_between(fut, (end - timedelta(days=7)).isoformat(), end.isoformat())
    return (one[-1][4], one[-1][0][:16].replace("T", " ")) if one else (None, None)


@route("GET", "/gamma")
def gamma_view(sub, claims, body, q):
    syms = [uwhales.check_sym(x) for x in (q.get("syms") or "QQQ,SPY").split(",") if x.strip()][:4]
    source = q.get("source") if q.get("source") in uwhales.SOURCES else "oi"
    out = []
    for sym in syms:
        g = uwhales.gamma(sub, sym, source, q.get("date") or None)
        fut = FUT_FOR.get(sym)
        if fut:
            px, t = _fut_last(fut)
            if px:   # the same levels in futures points: level x (futures / ETF), at the latest prices
                ratio = px / g["price"]
                conv = lambda v: None if v is None else round(v * ratio * 4) / 4
                g["fut"] = {"sym": fut, "price": px, "time": t, "ratio": ratio,
                            "uw": {k: conv(g["uw"].get(k)) for k in ("call_wall", "put_wall", "gamma_magnet", "gamma_flip")},
                            "computed": {k: conv(g["computed"].get(k)) for k in ("call_wall", "put_wall", "gamma_magnet", "gamma_flip")}}
        out.append(g)
    return {"source": source, "items": out}


# ---------------------------------------------------------------- data

def _range(body):
    syms = _syms(body.get("syms") or ["NQ", "ES"])
    d0, d1 = _day(body.get("from"), "From"), _day(body.get("to"), "To")
    if d0 > d1:
        raise BadRequest("From must be before To.")
    if d1 > now_ny().strftime("%Y-%m-%d"):
        d1 = now_ny().strftime("%Y-%m-%d")
    return syms, d0, d1


def _weekdays(d0, d1):
    d, out = date.fromisoformat(d0), []
    while d <= date.fromisoformat(d1):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _estimate(sub, kind, syms, d0, d1, window="rth"):
    key = databento.key(sub)
    avail = databento.dataset_range(key, {"candles": "ohlcv-1m", "book": "mbp-10"}.get(kind, "trades"))["end"]   # Databento refuses an end past its data
    if kind == "candles":
        s, e = futdata.utc_iso(d0), futdata.utc_iso((date.fromisoformat(d1) + timedelta(days=1)).isoformat())
        schema = "ohlcv-1m"
    elif kind == "book":                               # the order book of each day's window: priced day by day
        w0, w1 = futdata.WINDOWS.get(window, futdata.WINDOWS["rth"])
        total = 0.0
        for d in _weekdays(d0, d1):
            s, e = futdata.utc_iso(d, w0), futdata.utc_iso(d, w1)
            if avail and e > avail:
                e = avail
            if avail and s >= e:
                continue
            total += databento.cost(key, "mbp-10", syms, s, e)
        return total
    else:                                              # trades: regular hours only, priced on the whole span (an upper bound)
        s, e = futdata.utc_iso(d0, "09:30"), futdata.utc_iso(d1, "16:00")
        schema = "trades"
    if avail and e > avail:
        e = avail
    if avail and s >= e:
        return 0.0
    return databento.cost(key, schema, syms, s, e)


@route("GET", "/data")
def data_view(sub, claims, body, q):
    return {"coverage": futdata.coverage(), "job": jobs.status(sub, "data"), "databento": databento.status(sub), "alpaca": alpaca.status(sub), "uw": uwhales.status(sub), "fmp": __import__("fmp").status(sub), "refresh": jobs.status(sub, "refresh")}


@route("POST", "/data/estimate")
def data_estimate(sub, claims, body, q):
    kind = body.get("kind") if body.get("kind") in ("candles", "footprint", "book") else "candles"
    syms, d0, d1 = _range(body)
    if kind == "book" and len(_weekdays(d0, d1)) > 22:
        raise BadRequest("The order book is downloaded at most a month (22 sessions) at a time.")
    return {"usd": round(_estimate(sub, kind, syms, d0, d1, body.get("window") or "rth"), 2), "kind": kind}


@route("POST", "/data/download")
def data_download(sub, claims, body, q):
    kind = body.get("kind") if body.get("kind") in ("candles", "footprint", "book") else "candles"
    syms, d0, d1 = _range(body)
    window = body.get("window") if body.get("window") in futdata.WINDOWS else "rth"
    if kind == "footprint" and len(_weekdays(d0, d1)) > 66:
        raise BadRequest("Footprint (every trade) is downloaded at most 3 months at a time.")
    if kind == "book" and len(_weekdays(d0, d1)) > 22:
        raise BadRequest("The order book is downloaded at most a month (22 sessions) at a time.")
    usd = _estimate(sub, kind, syms, d0, d1, window)
    cap = databento.status(sub)["maxCost"]
    if usd > cap and not body.get("confirm"):
        return {"needConfirm": True, "usd": round(usd, 2), "cap": cap}
    if kind == "candles":
        st = jobs.start(sub, "data", "candles", {"syms": syms, "from": d0, "to": d1})
    elif kind == "book":
        st = jobs.start(sub, "data", "book", {"syms": syms, "days": _weekdays(d0, d1), "window": window})
    else:
        st = jobs.start(sub, "data", "footprint", {"syms": syms, "days": _weekdays(d0, d1)})
    return {"job": st, "usd": round(usd, 2)}


@route("POST", "/data/refresh")
def data_refresh(sub, claims, body, q):
    return jobs.start(sub, "data", "paper", {}) if body.get("paperOnly") else jobs.start(sub, "data", "candles", {
        "syms": [s for s in futdata.SYMS if futdata.last_bar_time(s)] or ["NQ", "ES"],
        "from": min([futdata.last_bar_time(s)[:10] for s in futdata.SYMS if futdata.last_bar_time(s)] or [(now_ny() - timedelta(days=30)).strftime("%Y-%m-%d")]),
        "to": now_ny().strftime("%Y-%m-%d")})


@route("GET", "/chart")
def chart(sub, claims, body, q):
    sym = futdata.check_sym(q.get("sym") or "NQ")
    try:
        tf, days = int(q.get("tf") or 5), int(q.get("days") or 3)
    except ValueError:
        raise BadRequest("tf and days must be numbers.")
    end = _day(q["end"], "end") if q.get("end") else None
    out = futdata.chart(sym, tf, days, end, rth_only=q.get("rth") == "1")
    if q.get("walls") == "1" and out["days"]:
        import walls
        out["walls"] = walls.chart_levels(sym, out["days"], q)
    if q.get("strat") and out["days"]:
        try:
            out["marks"] = strategy.marks(sub, q["strat"], sym, out["days"][0], out["days"][-1])
            out["overlay"] = strategy.overlay(sub, q["strat"], sym, out["days"][0], out["days"][-1])
            out["stratTf"] = strategy.get(sub, q["strat"])["params"].get("tf")
        except NotFound:
            out["marks"] = []
    return out


@route("GET", "/walls")
def walls_view(sub, claims, body, q):
    import walls
    sym = futdata.check_sym(q.get("sym") or "NQ")
    day = _day(q["day"], "day") if q.get("day") else futdata.session_of(futdata.last_bar_time(sym) or now_ny().strftime("%Y-%m-%dT%H:%M"))
    out = walls.density(sym, day, q)
    if not out:
        raise NotFound("Not enough stored sessions before that day (at least 35 regular-hours sessions).")
    out["sym"] = sym
    return out


@route("GET", "/cycles")
def cycles_view(sub, claims, body, q):
    import cycles
    out = cycles.build(futdata.check_sym(q.get("sym") or "NQ"), q)
    if not out:
        raise NotFound("Not enough candles: download more history on the Data page.")
    return out


@route("GET", "/confluence")
def confluence_view(sub, claims, body, q):
    import confluence
    out = confluence.build(futdata.check_sym(q.get("sym") or "NQ"), q)
    if not out:
        raise NotFound("No candles stored yet.")
    return out


@route("GET", "/footprint")
def footprint(sub, claims, body, q):
    sym = futdata.check_sym(q.get("sym") or "NQ")
    fp = futdata.footprint(sym, _day(q.get("day"), "day"))
    if not fp:
        raise NotFound("No footprint for that day: download the trades on the Data page.")
    return fp


@route("GET", "/heatmap")
def heatmap(sub, claims, body, q):
    sym = futdata.check_sym(q.get("sym") or "NQ")
    hm = futdata.heatmap(sym, _day(q.get("day"), "day"))
    if not hm:
        raise NotFound("No order book for that day: download it on the Data page.")
    return hm


# ---------------------------------------------------------------- strategies

@route("GET", "/running")
def running(sub, claims, body, q):
    out = strategy.running(sub)
    out["refresh"] = jobs.status(sub, "refresh")
    out["lastBar"] = {s: futdata.last_bar_time(s) for s in futdata.SYMS}
    return out


@route("GET", "/strategies")
def strategies(sub, claims, body, q):
    return {"strategies": strategy.listing(sub), "setups": strategy.NAMES, "coverage": futdata.coverage()}


@route("POST", "/strategies")
def strategy_new(sub, claims, body, q):
    return strategy.create(sub, body)


@route("GET", "/strategies/(?P<sid>[a-f0-9]{10})")
def strategy_get(sub, claims, body, q, sid):
    v = strategy.view(sub, sid)
    v["job"] = jobs.status(sub, "strat#" + sid)
    return v


@route("PUT", "/strategies/(?P<sid>[a-f0-9]{10})")
def strategy_put(sub, claims, body, q, sid):
    return strategy.update(sub, sid, body)


@route("DELETE", "/strategies/(?P<sid>[a-f0-9]{10})")
def strategy_del(sub, claims, body, q, sid):
    return strategy.delete(sub, sid)


@route("GET", "/strategies/(?P<sid>[a-f0-9]{10})/options")
def strategy_options(sub, claims, body, q, sid):
    """The study trades replayed on 0DTE options (QQQ / SPY) by delta, on the days downloaded so far."""
    import opt0dte
    ts = strategy.trades(sub, sid, "study")
    f = lambda k, d, lo, hi: min(hi, max(lo, float(q.get(k) or d)))
    try:
        dte = int(q.get("dte") or 0) if str(q.get("dte") or 0).isdigit() and int(q.get("dte") or 0) in opt0dte.DTES else 0
        und = "index" if q.get("und") == "index" else "etf"
        cov = opt0dte.coverage(ts, dte, und)
        r = opt0dte.replay(ts, f("slip", 0.02, 0, 1), f("comm", 0.65, 0, 5), f("budget", 1000, 50, 1e6), dte=dte, und=und) if cov["have"] else {"rows": []}
    except Exception as e:                              # show what went wrong instead of a bare 500
        import traceback
        traceback.print_exc()
        raise BadRequest(f"Options replay failed: {type(e).__name__}: {e}")
    return {**r, "dte": dte, "und": und, "coverage": cov,
            "job": jobs.status(sub, f"opt0dte-{sid}" + (f"-{dte}" if dte else "") + ("-idx" if und == "index" else ""))}


@route("GET", "/strategies/(?P<sid>[a-f0-9]{10})/compare")
def strategy_compare(sub, claims, body, q, sid):
    """Futures vs each downloaded option expiry: the same trades at the same $ risk, train / test."""
    import opt0dte
    f = lambda k, d, lo, hi: min(hi, max(lo, float(q.get(k) or d)))
    try:
        return opt0dte.compare(strategy.trades(sub, sid, "study"), f("slip", 0.02, 0, 1), f("comm", 0.65, 0, 5), f("risk", 1000, 50, 1e6))
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise BadRequest(f"Compare failed: {type(e).__name__}: {e}")


@route("GET", "/strategies/(?P<sid>[a-f0-9]{10})/alpopt")
def strategy_alpopt(sub, claims, body, q, sid):
    """Same-day options on the Alpaca paper account following this strategy's paper trades."""
    import alpopt
    return alpopt.view(sub, strategy.get(sub, sid))


@route("POST", "/strategies/(?P<sid>[a-f0-9]{10})/alpopt")
def strategy_alpopt_set(sub, claims, body, q, sid):
    import alpopt
    import db
    s = strategy.get(sub, sid)
    cfg = alpopt.clean(body)
    if cfg["on"]:
        if any(t not in alpopt.UND for t in s["params"]["tickers"]):
            raise BadRequest("Alpaca options follow NQ (→ QQQ) or ES (→ SPY) strategies.")
        a = alpopt.account(sub)                      # the key must work on paper-api.alpaca.markets
        if a.get("level") is not None and int(a["level"] or 0) < 2:
            raise BadRequest(f"The Alpaca paper account's options level is {a['level']}: buying calls / puts needs level 2.")
    elif (s.get("alpOpt") or {}).get("on"):
        alpopt.close_all(sub, s)
    s["alpOpt"] = cfg
    db.put(s)
    return alpopt.view(sub, s)


@route("POST", "/strategies/(?P<sid>[a-f0-9]{10})/options")
def strategy_options_download(sub, claims, body, q, sid):
    import opt0dte
    import uwhales
    ts = strategy.trades(sub, sid, "study")
    opt0dte.check(ts)
    if not uwhales.status(sub).get("connected"):
        raise BadRequest("Connect Unusual Whales on the Data page: the option prices come from it.")
    dte = int(body.get("dte") or 0)
    if dte not in opt0dte.DTES:
        raise BadRequest(f"Days to expiry: one of {', '.join(map(str, opt0dte.DTES))}.")
    und = "index" if body.get("und") == "index" else "etf"
    return {"job": jobs.start(sub, f"opt0dte-{sid}" + (f"-{dte}" if dte else "") + ("-idx" if und == "index" else ""), "opt0dte",
                              {"id": sid, "dte": dte, "und": und})}


@route("POST", "/strategies/(?P<sid>[a-f0-9]{10})/run")
def strategy_run(sub, claims, body, q, sid):
    step = body.get("step")
    if step not in ("study", "validate", "optimize"):
        raise BadRequest("step must be study, validate or optimize.")
    strategy.get(sub, sid)
    if step == "study" and not all(x["ok"] for x in strategy.data_check(sub, sid, fetch=False)):
        raise BadRequest("Not enough data: download at least 60 days of candles on the Data page.")
    return jobs.start(sub, "strat#" + sid, step, {"id": sid})


@route("POST", "/strategies/(?P<sid>[a-f0-9]{10})/action")
def strategy_action(sub, claims, body, q, sid):
    return strategy.action(sub, sid, body.get("action"))


@route("GET", "/strategies/(?P<sid>[a-f0-9]{10})/optimize")
def strategy_opt(sub, claims, body, q, sid):
    return {"opt": strategy.opt_view(sub, sid)}


@route("GET", "/strategies/(?P<sid>[a-f0-9]{10})/search")
def strategy_search(sub, claims, body, q, sid):
    import search
    return search.public(sub, sid)


@route("POST", "/strategies/(?P<sid>[a-f0-9]{10})/search")
def strategy_search_ctl(sub, claims, body, q, sid):
    import search
    return search.control(sub, sid, body.get("action"))


@route("GET", "/strategies/(?P<sid>[a-f0-9]{10})/trades")
def strategy_trades(sub, claims, body, q, sid):
    return {"trades": strategy.trades(sub, sid, q.get("set") or "study")}


# ---------------------------------------------------------------- Earnings: fundamentals screen + event study

@route("GET", "/fund")
def fund_view(sub, claims, body, q):
    import fundamentals as F
    st = F.settings(sub)
    rows = []
    for s in st["tickers"]:
        c = F.cached(s)
        rows.append(F.screen_row(c) if c else {"sym": s, "missing": True})
    ev = F.events(sub)
    sd = F.study(sub)
    import fmp
    return {"settings": st, "rows": rows, "job": jobs.status(sub, "fund"), "alpaca": alpaca.status(sub)["connected"], "fmp": fmp.status(sub),
            "events": {"n": len(ev["events"]), "tickers": len({e["sym"] for e in ev["events"]}), "cols": ev.get("cols") or [], "at": ev.get("at"),
                       "sample": ev["events"][:5]},
            "study": {k: sd.get(k) for k in ("at", "n", "tickers", "from", "to", "bench", "sources", "withEst", "withTx", "withGrades", "txStatus")} if sd else None, "benches": F.BENCHES}


@route("PUT", "/fund/settings")
def fund_settings(sub, claims, body, q):
    import fundamentals as F
    return F.save_settings(sub, body)


@route("POST", "/fund/refresh")
def fund_refresh(sub, claims, body, q):
    import fundamentals as F
    syms = F.settings(sub)["tickers"]
    if body.get("sym"):
        syms = [F.check_sym(body["sym"])]
    if not syms:
        raise BadRequest("Add tickers to the screen first.")
    return jobs.start(sub, "fund", "fundrefresh", {"syms": syms})


@route("GET", "/fund/company")
def fund_company(sub, claims, body, q):
    import fundamentals as F
    return F.company_view(sub, q.get("sym") or "", q.get("bench") if q.get("bench") in F.BENCHES else None)


@route("GET", "/fund/project")
def fund_project(sub, claims, body, q):
    import fundamentals as F
    if not alpaca.status(sub)["connected"]:
        raise BadRequest("The projection uses Alpaca daily bars: connect Alpaca on the Data page.")
    return F.project(sub, q.get("sym") or "")


@route("PUT", "/fmp")
def fmp_save(sub, claims, body, q):
    import fmp
    return fmp.save_key(sub, body.get("key"))


@route("DELETE", "/fmp")
def fmp_delete(sub, claims, body, q):
    import fmp
    return fmp.delete_key(sub)


@route("POST", "/fmp/test")
def fmp_test(sub, claims, body, q):
    import fmp
    return fmp.probe(sub)


@route("GET", "/fund/transcript")
def fund_transcript(sub, claims, body, q):
    import fundamentals as F
    d = q.get("date") or ""
    if len(d) != 10:
        raise BadRequest("date: YYYY-MM-DD.")
    return F.transcript_view(sub, q.get("sym") or "", d)


@route("POST", "/fund/universe")
def fund_universe(sub, claims, body, q):
    import fundamentals as F
    if body.get("source") == "sp500":
        return F.load_universe(sub, "sp500", int(body.get("years") or 10), bool(body.get("removed", True)))
    return F.save_settings(sub, {"universe": body.get("tickers") or [], "uniName": "Custom" if body.get("tickers") else ""})


@route("GET", "/fund/study/export")
def fund_export(sub, claims, body, q):
    import fundamentals as F
    kind = q.get("kind") if q.get("kind") in ("review", "csv") else "review"
    link = F.export_link(sub, kind)
    if link:
        return link
    st = jobs.status(sub, "fund") or {}
    if st.get("status") != "running":
        jobs.start(sub, "fund", "fundexport", {})
    return {"pending": True}


@route("GET", "/fund/today")
def fund_today(sub, claims, body, q):
    import fundamentals as F
    day = q.get("day") or F.now_ny().date().isoformat()
    return {**F.today_view(sub, day), "job": jobs.status(sub, "fund")}


@route("POST", "/fund/today")
def fund_today_build(sub, claims, body, q):
    import fmp
    import fundamentals as F
    day = body.get("day") or F.now_ny().date().isoformat()
    try:
        F.date.fromisoformat(day)
    except ValueError:
        raise BadRequest("day: YYYY-MM-DD.")
    if not fmp.status(sub)["connected"] or not alpaca.status(sub)["connected"]:
        raise BadRequest("Today's list needs FMP and Alpaca (Data page).")
    return jobs.start(sub, "fund", "fundtoday", {"day": day})


@route("POST", "/fund/backtest")
def fund_backtest(sub, claims, body, q):
    import backtest
    import fundamentals as F
    rows = (F.store.get_json(f"fund/u/{sub}/study_rows", max_age=600) or {}).get("rows") or []
    if not rows:
        raise BadRequest("Run the event study first.")
    if not any("postScore" in r for r in rows[-500:]):
        raise BadRequest("This study predates the strategy backtest: re-run the event study once.")
    out = backtest.run(rows, body.get("cfg") or {})
    F.store.put_json(f"fund/u/{sub}/backtest", {k: v for k, v in out.items() if k != "trades"})
    return out


@route("POST", "/fund/backtest/opt")
def fund_bt_opt(sub, claims, body, q):
    import fundamentals as F
    if body.get("cfg"):                                # stake, costs and borrow the optimizer trades with
        F.store.put_json(f"fund/u/{sub}/btopt", {"cfg": body["cfg"], "res": []})
    return jobs.start(sub, "fund", "fundopt", {})


@route("GET", "/fund/backtest/opt")
def fund_bt_opt_view(sub, claims, body, q):
    import backtest
    return {"opt": backtest.opt_view(sub, "pnl" if q.get("on") == "pnl" else "alpha"), "job": jobs.status(sub, "fund")}


@route("GET", "/fund/study/rows")
def fund_study_rows(sub, claims, body, q):
    import fundamentals as F
    try:
        page = max(0, int(q.get("page") or 0))
    except ValueError:
        raise BadRequest("page must be a number.")
    return F.study_rows(sub, page)


@route("POST", "/fund/events")
def fund_import(sub, claims, body, q):
    import fundamentals as F
    text = body.get("csv") or ""
    if len(text) > 4_000_000:
        raise BadRequest("The CSV is too large (4 MB at most).")
    return F.import_events(sub, text, "merge" if body.get("mode") == "merge" else "replace")


@route("DELETE", "/fund/events")
def fund_clear(sub, claims, body, q):
    import fundamentals as F
    return F.clear_events(sub)


@route("POST", "/fund/study")
def fund_study_run(sub, claims, body, q):
    import fundamentals as F
    st = F.settings(sub)
    if not alpaca.status(sub)["connected"]:
        raise BadRequest("The event study measures price reactions with Alpaca daily bars: connect Alpaca on the Data page.")
    if not F.events(sub)["events"] and not (st["useSec"] and F.study_tickers(st)):
        raise BadRequest("No releases to study: import your CSV, or add tickers and use their SEC 8-K dates.")
    opts = {k: body[k] for k in ("bench", "years", "useSec") if k in body}
    if opts:
        F.save_settings(sub, opts)
    return jobs.start(sub, "fund", "fundstudy", opts)


@route("GET", "/fund/study")
def fund_study(sub, claims, body, q):
    import fundamentals as F
    return F.study(sub) or {}


# ---------------------------------------------------------------- dispatch

def handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath") or "/"
    stage = event["requestContext"].get("stage")
    if stage and stage != "$default" and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1:]
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        sub = claims["sub"]
        try:
            db.note_owner(sub)                         # the single user's id, for the scheduled jobs (no table scans)
        except Exception as e:
            print("owner note failed", e)
    except KeyError:
        return resp(401, {"error": "Sign in required."})
    q = event.get("queryStringParameters") or {}
    for m, pat, fn in ROUTES:
        if m != method:
            continue
        match = pat.match(path)
        if match:
            try:
                return resp(200, fn(sub, claims, read_body(event), q, **match.groupdict()))
            except (BadRequest, NotFound, Unavailable) as e:
                return resp(e.status, {"error": str(e)})
            except Exception as e:  # log, but don't leak internals
                print("ERROR", method, path, repr(e))
                import traceback
                traceback.print_exc()
                return resp(500, {"error": "Something went wrong on the server. Try again."})
    return resp(404, {"error": f"No route for {method} {path}."})
