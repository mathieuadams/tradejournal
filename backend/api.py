"""HTTP API (API Gateway v2, Cognito JWT). One Lambda routes every request.

The user id always comes from the verified token (sub claim), never from the request.
"""
import base64
import json
import os
import re
import uuid

import alpaca
import charts
import chat
import context
import quality
import db
import review
import schwab
import views
from util import BadRequest, NotFound, Unavailable, iso, now_ny, read_body, resp

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def _lambda():
    import boto3
    return boto3.client("lambda")


def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))


def _num(v, name):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number.")
    if f != f or f < 0:
        raise BadRequest(f"{name} must be a positive number.")
    return f


def _str(v, name, n):
    if v is None:
        return None
    if not isinstance(v, str):
        raise BadRequest(f"{name} must be text.")
    return v.strip()[:n]


# ---------- profile & settings ----------

@route("GET", "/me")
def me(sub, claims, body, q):
    pk = db.upk(sub)
    p = db.get(pk, "PROFILE")
    if not p:
        p = {"PK": pk, "SK": "PROFILE", "email": claims.get("email", ""), "createdAt": iso(now_ny()), "settings": {}}
        db.put(p)
    accounts = sorted({t["acct"] for t in db.q_prefix(pk, "TRADE#")})
    return {"email": p.get("email"), "settings": views.load_settings(sub), "accounts": accounts,
            "mistakes": views.MISTAKES, "emotions": views.EMOTIONS}


@route("PUT", "/settings")
def put_settings(sub, claims, body, q):
    s = views.load_settings(sub)
    if "riskPerTrade" in body:
        s["riskPerTrade"] = _num(body["riskPerTrade"], "Risk per trade") or 0
    if "setups" in body:
        if not isinstance(body["setups"], list):
            raise BadRequest("Setups must be a list.")
        s["setups"] = [x.strip()[:60] for x in body["setups"] if isinstance(x, str) and x.strip()][:40]
    if "prop" in body and isinstance(body["prop"], dict):
        p = body["prop"]
        s["prop"] = {
            "enabled": bool(p.get("enabled")), "account": _str(p.get("account"), "Account", 60) or "",
            "start": _str(p.get("start"), "Start date", 10) or "",
            "balance": _num(p.get("balance"), "Starting balance") or 0,
            "trailing": _num(p.get("trailing"), "Trailing drawdown") or 0,
            "target": _num(p.get("target"), "Profit target") or 0,
            "dailyLoss": _num(p.get("dailyLoss"), "Daily loss limit") or 0,
        }
        if s["prop"]["start"] and not re.match(r"^\d{4}-\d{2}-\d{2}$", s["prop"]["start"]):
            raise BadRequest("Start date must be YYYY-MM-DD.")
    if "liveCoach" in body and isinstance(body["liveCoach"], dict):
        lc = body["liveCoach"]
        s["liveCoach"] = {k: bool(lc.get(k, s["liveCoach"].get(k))) for k in ("enabled", "premarket", "preclose", "entry")}
    if "notify" in body and isinstance(body["notify"], dict):
        n = body["notify"]
        phone = re.sub(r"[\s\-().]", "", str(n.get("phone") or ""))
        if phone and not phone.startswith("+"):
            phone = "+1" + phone if len(phone) == 10 else "+" + phone
        if phone and not re.match(r"^\+[1-9]\d{7,14}$", phone):
            raise BadRequest("Enter the phone number with country code, e.g. +19165551234.")
        ev = [e for e in (n.get("events") or ["entry", "fill", "exit", "closed", "cancel"]) if e in ("entry", "fill", "exit", "closed", "cancel")]
        email = str(n.get("email") or "").strip()
        if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise BadRequest("Enter a valid email address.")
        s["notify"] = {"phone": phone, "sms": bool(n.get("sms")) and bool(phone), "events": ev, "email": email}
        if email:
            try:
                import notify as _n
                _n.subscribe_email(sub, email)
            except Exception as e:
                print("subscribe failed", e)
    if "rules" in body:
        s["rules"] = _str(body["rules"], "Rules", 4000) or ""
    if "accountSize" in body:
        s["accountSize"] = _num(body["accountSize"], "Account size") or 0
    if "maxPositionPct" in body:
        s["maxPositionPct"] = _num(body["maxPositionPct"], "Max position %") or 0
    pk = db.upk(sub)
    p = db.get(pk, "PROFILE") or {"PK": pk, "SK": "PROFILE", "createdAt": iso(now_ny())}
    p["settings"] = s
    db.put(p)
    return s


# ---------- trades & journal ----------

@route("GET", "/trades")
def list_trades(sub, claims, body, q):
    return {"trades": views.load_views(sub)}


def _trade_or_404(sub, tid):
    t = views.find_trade(sub, tid)
    if not t:
        raise NotFound("Trade not found.")
    return t


@route("PATCH", r"/trades/(?P<tid>[a-f0-9]{16})")
def patch_trade(sub, claims, body, q, tid):
    t = _trade_or_404(sub, tid)
    pk = db.upk(sub)
    before = db.get(pk, f"JRNL#{tid}") or {"PK": pk, "SK": f"JRNL#{tid}"}
    j = dict(before)
    if "setup" in body:
        j["setup"] = _str(body["setup"], "Setup", 60) or ""
    if "tags" in body:
        if not isinstance(body["tags"], list):
            raise BadRequest("Tags must be a list.")
        j["tags"] = list(dict.fromkeys(x.strip()[:40] for x in body["tags"] if isinstance(x, str) and x.strip()))[:20]
    if "emotion" in body:
        j["emotion"] = _str(body["emotion"], "Emotion", 40) or ""
    if "notes" in body:
        j["notes"] = _str(body["notes"], "Notes", 5000) or ""
    if "plan" in body:
        p = body["plan"] or {}
        if not isinstance(p, dict):
            raise BadRequest("Plan must be an object.")
        j["plan"] = {"entry": _num(p.get("entry"), "Planned entry"), "stop": _num(p.get("stop"), "Stop"),
                     "target": _num(p.get("target"), "Target"), "thesis": _str(p.get("thesis"), "Thesis", 2000) or ""}
    j["updatedAt"] = iso(now_ny())
    db.put(j)
    audit = {k: before.get(k) for k in ("setup", "tags", "emotion", "notes", "plan")}
    after = {k: j.get(k) for k in ("setup", "tags", "emotion", "notes", "plan")}
    if audit != after:
        db.put({"PK": pk, "SK": f"AUDIT#{j['updatedAt']}#{tid}", "tradeId": tid, "before": audit, "after": after})
    reviewed = db.get(pk, f"REVIEW#{tid}") is not None
    return views.merge(t, j, reviewed)


@route("GET", r"/trades/(?P<tid>[a-f0-9]{16})/review")
def get_review(sub, claims, body, q, tid):
    r = db.get(db.upk(sub), f"REVIEW#{tid}")
    if not r:
        raise NotFound("No review yet.")
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


@route("POST", r"/trades/(?P<tid>[a-f0-9]{16})/review")
def post_review(sub, claims, body, q, tid):
    t = _trade_or_404(sub, tid)
    if t["status"] != "closed":
        raise BadRequest("The trade is still open. Reviews run once it closes.")
    return review.run(sub, t)


@route("GET", r"/trades/(?P<tid>[a-f0-9]{16})/bars")
def trade_bars(sub, claims, body, q, tid):
    t = _trade_or_404(sub, tid)
    return charts.get_bars(sub, t, q.get("tf") or "5m")


@route("POST", r"/trades/(?P<tid>[a-f0-9]{16})/context")
def trade_context(sub, claims, body, q, tid):
    t = _trade_or_404(sub, tid)
    t.pop("ctx", None) if body.get("force") else None
    context.for_trade(sub, t)
    j = db.get(db.upk(sub), f"JRNL#{tid}")
    return views.merge(t, j, db.get(db.upk(sub), f"REVIEW#{tid}") is not None)


@route("POST", r"/trades/(?P<tid>[a-f0-9]{16})/quality")
def trade_quality(sub, claims, body, q, tid):
    t = _trade_or_404(sub, tid)
    quality.for_trade(sub, t, force=bool(body.get("force")))
    j = db.get(db.upk(sub), f"JRNL#{tid}")
    return views.merge(t, j, db.get(db.upk(sub), f"REVIEW#{tid}") is not None)


@route("POST", "/analysis/quality")
def analysis_quality(sub, claims, body, q):
    return quality.analyze(sub)


@route("GET", "/gex")
def gamma_exposure(sub, claims, body, q):
    import gex
    return gex.run(sub, q.get("symbol"), q.get("days") or 45, q.get("expiry") or None, q.get("strikes") or None)


# ---------- paper bot ----------

@route("GET", "/bot")
def bot_home(sub, claims, body, q):
    import autotrader
    if q.get("sync") != "0":
        autotrader.sync(sub)
    allrecs = db.q_prefix(db.upk(sub), "BOT#", desc=True)
    active = [r for r in allrecs if r.get("status") in ("submitting", "submitted", "open", "closing")]
    closed = [r for r in allrecs if r.get("status") == "closed"][:60]
    others = [r for r in allrecs if r not in active and r not in closed][:60]
    recs = sorted(active + closed + others, key=lambda r: r["SK"], reverse=True)
    import notify
    state = db.get(db.upk(sub), "BOTSTATE") or {}
    return {"settings": autotrader.settings(sub), "summary": autotrader.summary(sub),
            "state": {k: state.get(k) for k in ("lastFlowRun", "lastFlowResult", "lastFlowAlerts")},
            "notifications": notify.recent(sub, 20),
            "items": [{k: v for k, v in r.items() if k not in ("PK", "SK")} for r in recs]}


@route("GET", "/bot/evaluations")
def bot_evaluations(sub, claims, body, q):
    """Evaluations that didn't become trades, newest first, 20 per page."""
    size = max(5, min(int(q.get("size") or 20), 100))
    page = max(1, int(q.get("page") or 1))
    ev = [r for r in db.q_prefix(db.upk(sub), "BOT#", desc=True)
          if r.get("status") not in ("submitting", "submitted", "open", "closing", "closed") and not r.get("orderId")]
    total = len(ev)
    chunk = ev[(page - 1) * size: page * size]
    return {"page": page, "size": size, "total": total, "pages": max(1, -(-total // size)),
            "items": [{k: v for k, v in r.items() if k not in ("PK", "SK")} for r in chunk]}


@route("PUT", "/bot/settings")
def bot_settings(sub, claims, body, q):
    import autotrader
    cur = autotrader.settings(sub)
    num = lambda k, lo, hi: max(lo, min(hi, float(body[k]))) if k in body and body[k] not in (None, "") else cur[k]
    new = {
        "enabled": bool(body.get("enabled", cur["enabled"])), "autoSubmit": bool(body.get("autoSubmit", cur["autoSubmit"])),
        "watchlist": [t.strip().upper()[:8] for t in (body.get("watchlist") if isinstance(body.get("watchlist"), list) else cur["watchlist"])
                      if isinstance(t, str) and t.strip() and t.strip().replace(".", "").isalnum()][:40],
        "dteMin": int(num("dteMin", 1, 365)), "dteMax": int(num("dteMax", 1, 400)),
        "deltaMin": num("deltaMin", 0.05, 0.99), "deltaMax": num("deltaMax", 0.05, 0.99),
        "minOi": int(num("minOi", 0, 100000)), "maxSpreadPct": num("maxSpreadPct", 1, 100),
        "stopPct": num("stopPct", 5, 100), "targetPct": num("targetPct", 5, 1000), "timeStopDte": int(num("timeStopDte", 0, 120)),
        "maxPositions": int(num("maxPositions", 1, 50)), "crossWindow": int(num("crossWindow", 1, 60)),
        "maxExtAtr": num("maxExtAtr", 0.1, 10), "minRoomRatio": num("minRoomRatio", 0, 10),
        "requireAboveFlip": bool(body.get("requireAboveFlip", cur["requireAboveFlip"])),
        "earnings": {k.upper()[:8]: v for k, v in (body.get("earnings") if isinstance(body.get("earnings"), dict) else cur["earnings"]).items()
                     if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}( (AMC|BMO))?$", v)},
        "noEntryDays": int(num("noEntryDays", 0, 60)),
        "minGrowth30": num("minGrowth30", -100, 1000),
        "invalidationOnClose": bool(body.get("invalidationOnClose", cur["invalidationOnClose"])),
        "emergencyAtr": num("emergencyAtr", 0, 10), "trailAfterTarget": bool(body.get("trailAfterTarget", cur["trailAfterTarget"])),
        "trailPct": num("trailPct", 5, 90),
        "flowAuto": bool(body.get("flowAuto", cur["flowAuto"])), "flowMinPremium": num("flowMinPremium", 0, 1e9),
        "flowMinDte": int(num("flowMinDte", 0, 400)), "flowMaxDte": int(num("flowMaxDte", 0, 800)),
        "flowAskSide": bool(body.get("flowAskSide", cur["flowAskSide"])), "flowSweeps": bool(body.get("flowSweeps", cur["flowSweeps"])),
        "flowMinVolOi": num("flowMinVolOi", 0, 1000), "excludeEtfs": bool(body.get("excludeEtfs", cur["excludeEtfs"])),
        "flowCooldownMin": int(num("flowCooldownMin", 1, 1440)), "flowMaxEvals": int(num("flowMaxEvals", 1, 10)),
        "chaseStep": num("chaseStep", 0.05, 5), "chaseSeconds": int(num("chaseSeconds", 5, 60)),
        "chaseMaxSteps": int(num("chaseMaxSteps", 0, 20)), "chaseMaxPct": num("chaseMaxPct", 0, 50),
    }
    if new["dteMin"] > new["dteMax"] or new["deltaMin"] > new["deltaMax"]:
        raise BadRequest("Minimums must be below maximums.")
    pk = db.upk(sub)
    p = db.get(pk, "PROFILE") or {"PK": pk, "SK": "PROFILE", "createdAt": iso(now_ny()), "settings": {}}
    p.setdefault("settings", {})["autotrade"] = new
    db.put(p)
    return new


@route("POST", "/bot/evaluate")
def bot_evaluate(sub, claims, body, q):
    import autotrader
    rec = autotrader.evaluate(sub, body.get("symbol"), body.get("earnings") if "earnings" in body else None)
    return rec


@route("POST", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})/order")
def bot_order(sub, claims, body, q, bid):
    import autotrader
    rec = autotrader.place(sub, bid, body.get("qty"), body.get("limit"))
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "chase", "id": bid}))
    return rec


@route("POST", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})/chase")
def bot_chase(sub, claims, body, q, bid):
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "chase", "id": bid}))
    return {"status": "chasing"}


@route("POST", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})/close")
def bot_close(sub, claims, body, q, bid):
    import autotrader
    return autotrader.close(sub, bid, "manual")


@route("POST", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})/dismiss")
def bot_dismiss(sub, claims, body, q, bid):
    rec = db.get(db.upk(sub), f"BOT#{bid}")
    if not rec:
        raise NotFound("Not found.")
    if rec.get("status") == "proposed":
        db.update(db.upk(sub), f"BOT#{bid}", {"status": "dismissed"})
    return {"ok": True}


@route("POST", "/bot/run")
def bot_run(sub, claims, body, q):
    job = "scan" if body.get("job") == "scan" else "monitor"
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event", Payload=json.dumps({"sub": sub, "job": job}))
    return {"status": "started"}


@route("GET", "/gex/market")
def gamma_market(sub, claims, body, q):
    import gex
    return gex.summary(sub, q.get("symbol") or "SPY", int(q.get("days") or 30), int(q.get("strikes") or 40))


@route("POST", r"/imports/(?P<iid>[0-9]{14}-[a-f0-9]{6})/undo")
def undo_import(sub, claims, body, q, iid):
    import ingest
    pk = db.upk(sub)
    rec = db.get(pk, f"IMPORT#{iid}")
    if not rec:
        raise NotFound("Import not found.")
    mine = [(pk, f["SK"]) for f in db.q_prefix(pk, "FILL#") if f.get("importId") == iid]
    if not mine:
        raise BadRequest("This import was made before undo was available, so its fills can't be told apart. "
                         "Use Settings → Data → Remove imported fills from an account instead.")
    db.batch_write(deletes=mine)
    g = ingest.regroup(sub)
    db.update(pk, f"IMPORT#{iid}", {"status": "undone", "undoneFills": len(mine)})
    return {"removedFills": len(mine), "trades": g["trades"]}


@route("POST", "/maintenance/remove-csv-fills")
def remove_csv_fills(sub, claims, body, q):
    """Delete fills that came from file imports in one account (broker-synced fills are kept)."""
    import ingest
    acct = _str(body.get("account"), "Account", 60)
    if not acct:
        raise BadRequest("Choose an account.")
    pk = db.upk(sub)
    rm = [(pk, f["SK"]) for f in db.q_prefix(pk, "FILL#")
          if f.get("acct") == acct and not str(f.get("id", "")).startswith(("alp-", "sch-"))]
    db.batch_write(deletes=rm)
    g = ingest.regroup(sub)
    return {"removedFills": len(rm), "trades": g["trades"]}


@route("GET", "/notify/status")
def notify_status(sub, claims, body, q):
    import notify
    email = (views.load_settings(sub).get("notify") or {}).get("email")
    try:
        st = notify.email_status(email)
    except Exception as e:
        st = f"unknown ({str(e)[:80]})"
    return {"email": email, "emailStatus": st}


@route("POST", "/notify/test")
def notify_test(sub, claims, body, q):
    import notify
    return notify.send(sub, "Trade Journal: test message. Paper bot order alerts will look like this.", "test")


@route("GET", "/flow/status")
def flow_status(sub, claims, body, q):
    import flowdata
    return flowdata.status(sub)


@route("PUT", "/flow/key")
def flow_key(sub, claims, body, q):
    import flowdata
    return flowdata.save_key(sub, _str(body.get("key"), "API key", 200) or "")


@route("GET", "/flow")
def flow_alerts(sub, claims, body, q):
    import flowdata
    return flowdata.alerts(sub, float(q.get("minPremium") or 100000), q.get("type") or "call",
                           int(q.get("minDte") or 0), int(q.get("maxDte") or 120),
                           q.get("askSide", "1") == "1", q.get("sweeps") == "1", q.get("ticker") or None,
                           float(q.get("minVolOi") or 0), int(q.get("days") or 1), exclude_etfs=q.get("noEtf") == "1",
                           period=q.get("period") or "today", date_from=q.get("from"), date_to=q.get("to"),
                           tz=q.get("tz") or "America/Los_Angeles")


@route("POST", "/maintenance/rebuild")
def rebuild(sub, claims, body, q):
    import ingest
    return ingest.regroup(sub)


@route("POST", "/analysis/context")
def analysis_context(sub, claims, body, q):
    return context.analyze(sub)


# ---------- daily journal ----------

@route("GET", r"/daily/(?P<day>\d{4}-\d{2}-\d{2})")
def get_daily(sub, claims, body, q, day):
    d = db.get(db.upk(sub), f"DAY#{day}") or {}
    return {k: d.get(k) for k in ("pre", "rec", "mood", "sleep", "focus")}


@route("PUT", r"/daily/(?P<day>\d{4}-\d{2}-\d{2})")
def put_daily(sub, claims, body, q, day):
    item = {"PK": db.upk(sub), "SK": f"DAY#{day}", "pre": _str(body.get("pre"), "Plan", 5000) or "",
            "rec": _str(body.get("rec"), "Recap", 5000) or "", "updatedAt": iso(now_ny())}
    for k in ("mood", "sleep", "focus"):
        v = body.get(k)
        if v is not None:
            if v not in (1, 2, 3, 4, 5):
                raise BadRequest(f"{k.capitalize()} must be 1 to 5.")
            item[k] = v
    db.put(item)
    return {k: item.get(k) for k in ("pre", "rec", "mood", "sleep", "focus")}


# ---------- imports ----------

@route("POST", "/imports")
def create_import(sub, claims, body, q):
    name = _str(body.get("fileName"), "File name", 120) or "trades.csv"
    account = _str(body.get("account"), "Account", 60) or "Default"
    tz = body.get("tz") if body.get("tz") in ("auto", "ET", "CT", "MT", "PT") else "auto"
    iid = now_ny().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    acct = base64.urlsafe_b64encode(account.encode()).decode().rstrip("=")
    key = f"imports/{sub}/{iid}/{acct}/{safe}"
    db.put({"PK": db.upk(sub), "SK": f"IMPORT#{iid}", "fileName": name, "account": account,
            "status": "waiting", "tz": tz, "createdAt": iso(now_ny())})
    url = _s3().generate_presigned_url("put_object", ExpiresIn=300, HttpMethod="PUT",
                                       Params={"Bucket": os.environ["UPLOAD_BUCKET"], "Key": key, "ContentType": "text/csv"})
    return {"importId": iid, "uploadUrl": url}


@route("GET", "/imports")
def list_imports(sub, claims, body, q):
    items = db.q_prefix(db.upk(sub), "IMPORT#", desc=True, limit=25)
    return {"imports": [{"id": i["SK"][7:], **{k: v for k, v in i.items() if k not in ("PK", "SK")}} for i in items]}


# ---------- broker ----------

@route("GET", "/broker/alpaca")
def get_broker(sub, claims, body, q):
    b = db.get(db.upk(sub), "BROKER#alpaca")
    if not b:
        return {"connected": False}
    return {"connected": True, **{k: b.get(k) for k in ("env", "feed", "keyHint", "status", "lastSync", "lastResult", "error")}}


@route("PUT", "/broker/alpaca")
def put_broker(sub, claims, body, q):
    key, secret = _str(body.get("key"), "Key", 100), _str(body.get("secret"), "Secret", 200)
    if not key or not secret:
        raise BadRequest("Enter both the API key and the secret.")
    feed = body.get("feed") if body.get("feed") in ("iex", "sip") else "iex"
    alpaca.save_creds(sub, key, secret, body.get("env") or "paper", feed)
    _lambda().invoke(FunctionName=os.environ["SYNC_FUNCTION"], InvocationType="Event", Payload=json.dumps({"sub": sub, "broker": "alpaca"}))
    return get_broker(sub, claims, body, q)


@route("DELETE", "/broker/alpaca")
def del_broker(sub, claims, body, q):
    db.delete(db.upk(sub), "BROKER#alpaca")
    return {"connected": False}


@route("POST", "/broker/alpaca/sync")
def sync_broker(sub, claims, body, q):
    if not db.get(db.upk(sub), "BROKER#alpaca"):
        raise BadRequest("Connect Alpaca first.")
    db.update(db.upk(sub), "BROKER#alpaca", {"status": "syncing"})
    _lambda().invoke(FunctionName=os.environ["SYNC_FUNCTION"], InvocationType="Event", Payload=json.dumps({"sub": sub, "broker": "alpaca"}))
    return {"status": "syncing"}


@route("GET", "/broker/schwab")
def get_schwab(sub, claims, body, q):
    return schwab.status(sub)


@route("POST", "/broker/schwab/authorize")
def schwab_authorize(sub, claims, body, q):
    name = _str(body.get("journalAccount"), "Journal account", 60) or "Schwab"
    return {"url": schwab.authorize_url(sub, name)}


@route("POST", "/broker/schwab/callback")
def schwab_callback(sub, claims, body, q):
    code = _str(body.get("code"), "Code", 2000)
    if not code:
        raise BadRequest("Schwab didn't return a login code.")
    st = schwab.complete(sub, code, _str(body.get("state"), "State", 200))
    db.update(db.upk(sub), schwab.SK, {"status": "syncing"})
    _lambda().invoke(FunctionName=os.environ["SYNC_FUNCTION"], InvocationType="Event", Payload=json.dumps({"sub": sub, "broker": "schwab"}))
    return {**st, "status": "syncing"}


@route("POST", "/broker/schwab/sync")
def schwab_sync(sub, claims, body, q):
    if not db.get(db.upk(sub), schwab.SK):
        raise BadRequest("Connect Schwab first.")
    db.update(db.upk(sub), schwab.SK, {"status": "syncing"})
    _lambda().invoke(FunctionName=os.environ["SYNC_FUNCTION"], InvocationType="Event", Payload=json.dumps({"sub": sub, "broker": "schwab"}))
    return {"status": "syncing"}


@route("DELETE", "/broker/schwab")
def schwab_disconnect(sub, claims, body, q):
    return schwab.disconnect(sub)


# ---------- coach ----------

@route("POST", "/coach/chat")
def coach_chat(sub, claims, body, q):
    return chat.run(sub, body.get("messages"))


@route("GET", "/coach/notes")
def coach_notes(sub, claims, body, q):
    items = db.q_prefix(db.upk(sub), "COACHNOTE#", desc=True, limit=int(q.get("limit") or 20))
    if q.get("tradeId"):
        items = [i for i in items if i.get("tradeId") == q["tradeId"]]
    return {"notes": [{k: v for k, v in i.items() if k not in ("PK", "SK")} for i in items]}


@route("POST", "/coach/notes")
def coach_run(sub, claims, body, q):
    kind = body.get("kind") if body.get("kind") in ("premarket", "preclose", "manual", "entry") else "manual"
    payload = {"sub": sub, "kind": kind}
    if body.get("tradeId"):
        payload["tradeId"] = _str(body["tradeId"], "Trade", 20)
    _lambda().invoke(FunctionName=os.environ["LIVECOACH_FUNCTION"], InvocationType="Event", Payload=json.dumps(payload))
    return {"status": "started"}


@route("GET", "/reports/latest")
def latest_report(sub, claims, body, q):
    r = db.q_prefix(db.upk(sub), "REPORT#", desc=True, limit=1)
    if not r:
        raise NotFound("No report yet.")
    return {k: v for k, v in r[0].items() if k not in ("PK", "SK")}


@route("POST", "/reports")
def make_report(sub, claims, body, q):
    _lambda().invoke(FunctionName=os.environ["WEEKLY_FUNCTION"], InvocationType="Event", Payload=json.dumps({"sub": sub}))
    return {"status": "started"}


# ---------- entry point ----------

def handler(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath") or "/"
    stage = event["requestContext"].get("stage")
    if stage and stage != "$default" and path.startswith(f"/{stage}/"):
        path = path[len(stage) + 1:]
    try:
        claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
        sub = claims["sub"]
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
