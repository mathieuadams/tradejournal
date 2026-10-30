"""HTTP API (API Gateway v2, Cognito JWT). One Lambda routes every request.

The user id always comes from the verified token (sub claim), never from the request."""
import re
from datetime import date, timedelta

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


def _estimate(sub, kind, syms, d0, d1):
    key = databento.key(sub)
    avail = databento.dataset_range(key)["end"]         # Databento refuses an end past the data it has (today)
    if kind == "candles":
        s, e = futdata.utc_iso(d0), futdata.utc_iso((date.fromisoformat(d1) + timedelta(days=1)).isoformat())
        schema = "ohlcv-1m"
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
    return {"coverage": futdata.coverage(), "job": jobs.status(sub, "data"), "databento": databento.status(sub)}


@route("POST", "/data/estimate")
def data_estimate(sub, claims, body, q):
    kind = body.get("kind") if body.get("kind") in ("candles", "footprint") else "candles"
    syms, d0, d1 = _range(body)
    return {"usd": round(_estimate(sub, kind, syms, d0, d1), 2), "kind": kind}


@route("POST", "/data/download")
def data_download(sub, claims, body, q):
    kind = body.get("kind") if body.get("kind") in ("candles", "footprint") else "candles"
    syms, d0, d1 = _range(body)
    if kind == "footprint" and len(_weekdays(d0, d1)) > 66:
        raise BadRequest("Footprint (every trade) is downloaded at most 3 months at a time.")
    usd = _estimate(sub, kind, syms, d0, d1)
    cap = databento.status(sub)["maxCost"]
    if usd > cap and not body.get("confirm"):
        return {"needConfirm": True, "usd": round(usd, 2), "cap": cap}
    if kind == "candles":
        st = jobs.start(sub, "data", "candles", {"syms": syms, "from": d0, "to": d1})
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
    if q.get("strat") and out["days"]:
        try:
            out["marks"] = strategy.marks(sub, q["strat"], sym, out["days"][0], out["days"][-1])
        except NotFound:
            out["marks"] = []
    return out


@route("GET", "/footprint")
def footprint(sub, claims, body, q):
    sym = futdata.check_sym(q.get("sym") or "NQ")
    fp = futdata.footprint(sym, _day(q.get("day"), "day"))
    if not fp:
        raise NotFound("No footprint for that day: download the trades on the Data page.")
    return fp


# ---------------------------------------------------------------- strategies

@route("GET", "/running")
def running(sub, claims, body, q):
    return strategy.running(sub)


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
    if step not in ("study", "validate"):
        raise BadRequest("step must be study or validate.")
    strategy.get(sub, sid)
    if step == "study" and not all(x["ok"] for x in strategy.data_check(sub, sid)):
        raise BadRequest("Not enough data: download at least 60 days of candles on the Data page.")
    return jobs.start(sub, "strat#" + sid, step, {"id": sid})


@route("POST", "/strategies/(?P<sid>[a-f0-9]{10})/action")
def strategy_action(sub, claims, body, q, sid):
    return strategy.action(sub, sid, body.get("action"))


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
