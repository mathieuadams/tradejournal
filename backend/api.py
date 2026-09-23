"""HTTP API (API Gateway v2, Cognito JWT). One Lambda routes every request.

The user id always comes from the verified token (sub claim), never from the request.
"""
import base64
import json
import os
import re
import uuid

import alpaca
import chat
import db
import review
import views
from grouping import is_future, is_option
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
    if is_future(t["sym"]) or is_option(t["sym"]):
        raise BadRequest("Charts for futures and options need a futures or options data feed, which isn't connected yet.")
    c = alpaca.creds(sub)
    if not c:
        raise BadRequest("Connect Alpaca in Settings to load charts. Free Alpaca keys work.")
    tf = int(q.get("tf") or 5)
    start, end = alpaca.window(t, tf)
    return {"tf": tf, "bars": alpaca.bars(c, t["sym"], start, end, tf)}


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
    _lambda().invoke(FunctionName=os.environ["SYNC_FUNCTION"], InvocationType="Event", Payload=json.dumps({"sub": sub}))
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
    _lambda().invoke(FunctionName=os.environ["SYNC_FUNCTION"], InvocationType="Event", Payload=json.dumps({"sub": sub}))
    return {"status": "syncing"}


# ---------- coach ----------

@route("POST", "/coach/chat")
def coach_chat(sub, claims, body, q):
    return chat.run(sub, body.get("messages"))


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
