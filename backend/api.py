"""HTTP API (API Gateway v2, Cognito JWT). One Lambda routes every request.

The user id always comes from the verified token (sub claim), never from the request."""
import re
from datetime import date, timedelta

import alpaca
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
    # Stats days: the history the levels and wave odds are drawn from. TradeWellX uses waves from before the window it
    # shows (its levels at 10/30 come from moves no wave on screen had made), so this is separate from Days to load.
    stats = _int(q.get("stats"), "Stats days", 1, 120, max(days, 30))
    stats = max(stats, days)
    agg = _int(q.get("agg"), "Aggregation", 1, 60, 15)
    sym = (q.get("sym") or "QQQ").strip().upper()
    rth_only = q.get("rth") in ("1", "true")
    # extra sessions before the stats history, so the MA, pivot and MacroTrend (50 bars) all exist on its first bar
    warm = 2 + (max(2 * wave, 51) * agg) // 390
    if sym in futdata.SYMS:                            # NQ / ES: the stored Databento 1-minute candles, all hours by default
        bars, source = _fut_bars(sym, stats + warm, agg, rth_only), "databento"
    else:                                              # stocks / ETFs: Alpaca, regular hours
        bars, source = alpaca.bars(sub, sym, stats + warm, agg), "alpaca"
    dates = sorted({b[0][:10] for b in bars})
    first = lambda d: next((i for i, b in enumerate(bars) if b[0][:10] >= d), 0)
    if not dates:
        raise BadRequest(f"No {sym} bars came back.")
    win_s, win = first(dates[-min(stats, len(dates))]), first(dates[-min(days, len(dates))])
    if win_s < 2 * wave:
        win_s = min(2 * wave, win)                     # not enough history: start the stats where the MA exists
    if win < 2 * wave:
        raise BadRequest(f"Not enough history before the window for Wave {wave}: lower the wave or the days.")
    r = fractal.analyze(bars, wave, win_s, show=win)
    r["bars"] = bars[win:]
    r["params"] = {"sym": sym, "wave": wave, "days": days, "stats": stats, "statsFrom": bars[win_s][0], "agg": agg, "source": source, "rth": rth_only or source == "alpaca",
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
    return {"coverage": futdata.coverage(), "job": jobs.status(sub, "data"), "databento": databento.status(sub), "alpaca": alpaca.status(sub), "refresh": jobs.status(sub, "refresh")}


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


@route("POST", "/strategies/(?P<sid>[a-f0-9]{10})/run")
def strategy_run(sub, claims, body, q, sid):
    step = body.get("step")
    if step not in ("study", "validate", "optimize"):
        raise BadRequest("step must be study, validate or optimize.")
    strategy.get(sub, sid)
    if step == "study" and not all(x["ok"] for x in strategy.data_check(sub, sid)):
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
