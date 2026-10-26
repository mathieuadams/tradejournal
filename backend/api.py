"""HTTP API (API Gateway v2, Cognito JWT). One Lambda routes every request.

The user id always comes from the verified token (sub claim), never from the request.
"""
import base64
import json
import os
import re
import uuid
from datetime import timedelta

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
        ev = [e for e in (n.get("events") or ["entry", "fill", "exit", "closed", "cancel", "roll", "ai"]) if e in ("entry", "fill", "exit", "closed", "cancel", "roll", "ai")]
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
            "state": {k: state.get(k) for k in ("lastFlowRun", "lastFlowResult", "lastFlowAlerts", "lastFlowWindow", "lastFlowTickers",
                                                "lastFlowSkips", "uwOkAt", "uwErrorAt", "uwError", "flowDay", "alertsToday", "evalsToday")},
            "notifications": notify.recent(sub, 20), "active": _active(sub),
            "items": [{k: v for k, v in r.items() if k not in ("PK", "SK")} for r in recs]}


def _active(sub):
    try:
        import strats
        return strats.active_info(sub)
    except Exception:
        return None


@route("GET", "/bot/tickers")
def bot_tickers(sub, claims, body, q):
    """Every ticker the bot has evaluated, newest first: the latest evaluation's id, time, decision, price and walls."""
    seen, out = {}, []
    held = set()
    for r in db.q_prefix(db.upk(sub), "BOT#", desc=True):
        sym = r.get("symbol")
        if r.get("status") in ("submitting", "submitted", "open", "closing"):
            held.add(sym)
        if not sym:
            continue
        if sym in seen:
            seen[sym]["count"] += 1
            continue
        g = r.get("gamma") or {}
        seen[sym] = row = {"symbol": sym, "id": r.get("id"), "at": r.get("createdAt"), "decision": r.get("decision"), "count": 1,
                           "status": r.get("status"), "price": (r.get("signals") or {}).get("price"),
                           "callWall": g.get("callWall"), "putWall": g.get("putWall"), "flip": g.get("gammaFlip"),
                           "contract": (r.get("proposal") or {}).get("contract"), "limit": (r.get("proposal") or {}).get("limit"),
                           "exp": (r.get("proposal") or {}).get("exp"), "strike": (r.get("proposal") or {}).get("strike")}
        out.append(row)
    for row in out:
        row["held"] = row["symbol"] in held
    return {"items": out}


@route("GET", "/bot/evaluations")
def bot_evaluations(sub, claims, body, q):
    """Evaluations newest first, 20 per page.
    ?group=ticker: one row per ticker (its latest evaluation, how many, decisions), ordered by the latest one.
    ?symbol=XYZ: every evaluation of that ticker, including the ones that became trades.
    Otherwise: evaluations that didn't become trades."""
    size = max(5, min(int(q.get("size") or 20), 100))
    page = max(1, int(q.get("page") or 1))
    claude_only = q.get("claude") in ("1", "true")
    taken = lambda r: r.get("status") in ("submitting", "submitted", "open", "closing", "closed") or r.get("orderId")
    allr = db.q_prefix(db.upk(sub), "BOT#", desc=True)
    clean = lambda r: {k: v for k, v in r.items() if k not in ("PK", "SK")}
    sym = (q.get("symbol") or "").strip().upper()
    if sym:
        ev = [r for r in allr if r.get("symbol") == sym]
        total = len(ev)
        chunk = ev[(page - 1) * size: page * size]
        return {"page": page, "size": size, "total": total, "pages": max(1, -(-total // size)), "symbol": sym,
                "items": [{**clean(r), "taken": bool(taken(r))} for r in chunk]}
    needle = (q.get("q") or "").strip().upper()
    ev = [r for r in allr if not taken(r) and (not claude_only or (r.get("aiCheck") or {}).get("verdict"))
          and (not needle or needle in (r.get("symbol") or ""))]
    if q.get("group") == "ticker":
        groups, order = {}, []
        for r in ev:                                   # newest first, so the first one seen is the latest
            g = groups.get(r.get("symbol"))
            if not g:
                g = groups[r.get("symbol")] = {"symbol": r.get("symbol"), "count": 0, "decisions": {}, "first": r.get("createdAt"),
                                                "latest": {k: r.get(k) for k in ("id", "createdAt", "decision", "status", "proposal", "aiCheck",
                                                                                  "shadow", "blocking", "origin")}}
                order.append(g)
            g["count"] += 1
            g["first"] = r.get("createdAt")
            g["decisions"][r.get("decision") or "?"] = g["decisions"].get(r.get("decision") or "?", 0) + 1
        held = {r.get("symbol") for r in allr if r.get("status") in ("submitting", "submitted", "open", "closing")}
        for g in order:
            g["held"] = g["symbol"] in held
        total = len(order)
        return {"page": page, "size": size, "total": total, "pages": max(1, -(-total // size)), "claude": claude_only, "group": "ticker",
                "items": order[(page - 1) * size: page * size]}
    total = len(ev)
    chunk = ev[(page - 1) * size: page * size]
    return {"page": page, "size": size, "total": total, "pages": max(1, -(-total // size)), "claude": claude_only,
            "items": [clean(r) for r in chunk]}


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
        "aiCheck": bool(body.get("aiCheck", cur["aiCheck"])), "aiAllowCaution": bool(body.get("aiAllowCaution", cur["aiAllowCaution"])),
        "aiBlockOnError": bool(body.get("aiBlockOnError", cur["aiBlockOnError"])), "aiMaxAgeMin": int(num("aiMaxAgeMin", 5, 1440)),
        "aiExitReview": bool(body.get("aiExitReview", cur["aiExitReview"])), "aiExitAutoClose": bool(body.get("aiExitAutoClose", cur["aiExitAutoClose"])),
        "aiExitMinConf": int(num("aiExitMinConf", 0, 100)), "aiExitHoldMinConf": int(num("aiExitHoldMinConf", 0, 100)),
        "aiExitHoldLossPct": num("aiExitHoldLossPct", 0, 100),
        "repeatEnabled": bool(body.get("repeatEnabled", cur["repeatEnabled"])), "repeatDays": int(num("repeatDays", 1, 20)), "repeatMinPremium": num("repeatMinPremium", 1000, 1e7),
        "repeatMinHits": int(num("repeatMinHits", 2, 100)), "repeatMinTotal": num("repeatMinTotal", 1000, 1e9),
        "repeatMinMinutes": int(num("repeatMinMinutes", 1, 390)),
        "putCheck": bool(body.get("putCheck", cur["putCheck"])), "putBlock": bool(body.get("putBlock", cur["putBlock"])),
        "putMaxRatio": num("putMaxRatio", 0, 10),
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
        "flowMinVolOi": num("flowMinVolOi", 0, 1000), "flowWindowMin": int(num("flowWindowMin", 5, 240)),
        "burstMinPremium": num("burstMinPremium", 10000, 1e8), "burstMinRatio": num("burstMinRatio", 1, 100),
        "burstMinPrints": int(num("burstMinPrints", 2, 200)), "burstMinCallShare": num("burstMinCallShare", 0.5, 1),
        "burstCooldownMin": int(num("burstCooldownMin", 15, 390)), "excludeEtfs": bool(body.get("excludeEtfs", cur["excludeEtfs"])),
        "flowCooldownMin": int(num("flowCooldownMin", 1, 1440)), "flowMaxEvals": int(num("flowMaxEvals", 1, 10)),
        "chaseStep": num("chaseStep", 0.05, 5), "chaseSeconds": int(num("chaseSeconds", 5, 60)),
        "chaseMaxSteps": int(num("chaseMaxSteps", 0, 20)), "chaseMaxPct": num("chaseMaxPct", 0, 50),
        "strategy": body.get("strategy") if body.get("strategy") in autotrader.STRATEGIES else cur["strategy"],
        "spreadShortDeltaMin": num("spreadShortDeltaMin", 0.05, 0.95), "spreadShortDeltaMax": num("spreadShortDeltaMax", 0.05, 0.95),
        "spreadMaxDebitPct": num("spreadMaxDebitPct", 10, 100), "spreadStopPct": num("spreadStopPct", 5, 100),
        "spreadTargetPct": num("spreadTargetPct", 5, 100),
        "diagLongDteMin": int(num("diagLongDteMin", 1, 800)), "diagLongDteMax": int(num("diagLongDteMax", 1, 800)),
        "diagLongDeltaMin": num("diagLongDeltaMin", 0.05, 0.99), "diagLongDeltaMax": num("diagLongDeltaMax", 0.05, 0.99),
        "diagShortDteMin": int(num("diagShortDteMin", 0, 120)), "diagShortDteMax": int(num("diagShortDteMax", 0, 120)),
        "diagShortDeltaMin": num("diagShortDeltaMin", 0.05, 0.95), "diagShortDeltaMax": num("diagShortDeltaMax", 0.05, 0.95),
        "diagTargetPct": num("diagTargetPct", 0, 1000), "diagMinShortCredit": num("diagMinShortCredit", 0, 10000),
        "diagRollDte": int(num("diagRollDte", 0, 10)), "diagRollWaitMin": int(num("diagRollWaitMin", 1, 30)),
    }
    if new["dteMin"] > new["dteMax"] or new["deltaMin"] > new["deltaMax"]:
        raise BadRequest("Minimums must be below maximums.")
    if (new["spreadShortDeltaMin"] > new["spreadShortDeltaMax"] or new["diagLongDteMin"] > new["diagLongDteMax"]
            or new["diagLongDeltaMin"] > new["diagLongDeltaMax"] or new["diagShortDteMin"] > new["diagShortDteMax"]
            or new["diagShortDeltaMin"] > new["diagShortDeltaMax"]):
        raise BadRequest("Spread minimums must be below maximums.")
    if new["diagShortDteMax"] >= new["diagLongDteMin"]:
        raise BadRequest("Diagonal: the short call must expire before the long call (short max days < long min days).")
    pk = db.upk(sub)
    p = db.get(pk, "PROFILE") or {"PK": pk, "SK": "PROFILE", "createdAt": iso(now_ny()), "settings": {}}
    new["earningsAuto"] = ((p.get("settings") or {}).get("autotrade") or {}).get("earningsAuto") or {}   # kept: pulled automatically
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
    rec = autotrader.place(sub, bid, body.get("qty"), body.get("limit"), ai_inline=False, override=bool(body.get("override")))
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "chase", "id": bid}))
    return rec


@route("POST", "/bot/shadow/run")
def bot_shadow_run(sub, claims, body, q):
    """Run shadow tracking now (normally after the close): follows the skipped evaluations of the last 5 weeks."""
    db.put({"PK": db.upk(sub), "SK": "SHADOWRUN", "status": "running", "at": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "shadow"}))
    return {"status": "running"}


@route("GET", "/bot/wallwatch")
def bot_wallwatch(sub, claims, body, q):
    import wallwatch
    return wallwatch.view(sub)


@route("GET", "/daytrade")
def daytrade_get(sub, claims, body, q):
    import daytrade
    return daytrade.view(sub)


@route("PUT", "/daytrade")
def daytrade_put(sub, claims, body, q):
    import daytrade
    daytrade.save_config(sub, body or {})
    return daytrade.view(sub)


@route("PUT", "/daytrade/account")
def daytrade_account(sub, claims, body, q):
    import daytrade
    key, secret = _str((body or {}).get("key"), "Key", 100), _str((body or {}).get("secret"), "Secret", 200)
    if not key or not secret:
        raise BadRequest("Enter both the API key and the secret of the second (paper) account.")
    return daytrade.connect(sub, key, secret)


@route("DELETE", "/daytrade/account")
def daytrade_account_del(sub, claims, body, q):
    import daytrade
    db.delete(db.upk(sub), daytrade.CRED_SK)
    return daytrade.view(sub)


@route("POST", "/daytrade/context")
def daytrade_context(sub, claims, body, q):
    """The last 30 sessions of one ticker: range, trends, support / resistance zones, volume profile, gamma walls."""
    import daytrade
    return daytrade.context(sub, (body or {}).get("t"))


@route("GET", "/daytrade/movers")
def daytrade_movers(sub, claims, body, q):
    """Today's pre-market movers (09:15) and the stocks in play of the previous days: the side list of the chart."""
    import stockday
    return {"days": stockday.movers(sub)}


@route("POST", "/daytrade/smc")
def daytrade_smc(sub, claims, body, q):
    """Smart-money model on one ticker: liquidity, sweeps, MSS, FVG, retracement entries, every step written down."""
    import smc
    b = body or {}
    return smc.run(sub, b.get("t"), b.get("days") or 5, b.get("cfg") if isinstance(b.get("cfg"), dict) else None)


@route("GET", "/pipeline")
def pipe_list(sub, claims, body, q):
    """Strategy pipeline: every strategy (research / paper / live), the older bots, the accounts, today's market cell."""
    import pipeline
    return pipeline.overview(sub)


@route("POST", "/pipeline")
def pipe_new(sub, claims, body, q):
    import pipeline
    return pipeline.detail(sub, pipeline.create(sub, body or {})["id"])


@route("POST", "/pipeline/accounts")
def pipe_acc(sub, claims, body, q):
    import pipeline
    return {"accounts": pipeline.connect(sub, body or {})}


@route("GET", r"/pipeline/(?P<pid>[a-f0-9]{10})")
def pipe_get(sub, claims, body, q, pid):
    import pipeline
    return pipeline.detail(sub, pid)


@route("PUT", r"/pipeline/(?P<pid>[a-f0-9]{10})")
def pipe_put(sub, claims, body, q, pid):
    import pipeline
    return pipeline.action(sub, pid, body or {})


@route("DELETE", r"/pipeline/(?P<pid>[a-f0-9]{10})")
def pipe_del(sub, claims, body, q, pid):
    import pipeline
    return pipeline.delete(sub, pid)


@route("POST", r"/pipeline/(?P<pid>[a-f0-9]{10})/run")
def pipe_run(sub, claims, body, q, pid):
    import pipeline
    pipeline.run_start(sub, pid, (body or {}).get("step") or "all")
    return pipeline.detail(sub, pid)


@route("GET", r"/pipeline/(?P<pid>[a-f0-9]{10})/chart")
def pipe_chart(sub, claims, body, q, pid):
    import pipeline
    return pipeline.chart(sub, pid, q.get("t"))


@route("GET", "/uoa")
def uoa_get(sub, claims, body, q):
    """Unusual options activity study: the alert history (calls + puts) and the study by condition."""
    import uoa
    return {"hist": uoa.hist_public(sub), "study": uoa.study_public(sub)}


@route("POST", "/uoa/history")
def uoa_hist(sub, claims, body, q):
    import uoa
    return {"hist": uoa.hist_start(sub, body or {})}


@route("POST", "/uoa/study")
def uoa_study(sub, claims, body, q):
    import uoa
    return {"study": uoa.study_start(sub, body or {})}


@route("GET", "/ropt")
def ropt_get(sub, claims, body, q):
    """Regime optimizer: the best strategies for bull / sideways / bear and the combined portfolio."""
    import regime
    import ropt
    return {**ropt.public(sub), "lists": [{"name": "__etf", "label": "Index + sector ETFs (no stock picking)", "n": 15}] + regime.lists(sub)}


@route("PUT", "/ropt")
def ropt_set(sub, claims, body, q):
    import regime
    import research
    import ropt
    b = body or {}
    out = ropt.save_cfg(sub, b)
    if b.get("on") and not out.get("running"):
        research._invoke(sub, "ropt", {})
    return {**out, "lists": [{"name": "__etf", "label": "Index + sector ETFs (no stock picking)", "n": 15}] + regime.lists(sub)}


@route("GET", "/plab")
def plab_get(sub, claims, body, q):
    """Portfolio lab: the layered strategy backtested as one portfolio (the last run and its settings)."""
    import plab
    import regime
    return {**plab.public(sub), "lists": [{"name": "__etf", "label": "Index + sector ETFs (no stock picking)", "n": 15}] + regime.lists(sub)}


@route("POST", "/plab")
def plab_run(sub, claims, body, q):
    import plab
    import regime
    return {**plab.start(sub, body or {}), "lists": [{"name": "__etf", "label": "Index + sector ETFs (no stock picking)", "n": 15}] + regime.lists(sub)}


@route("GET", "/regime")
def regime_get(sub, claims, body, q):
    """Market regimes: the last study (labels, durations, the market / sectors / watchlist in each regime)."""
    import regime
    return {**regime.public(sub), "lists": regime.lists(sub)}


@route("POST", "/regime")
def regime_run(sub, claims, body, q):
    import regime
    return {**regime.start(sub, body or {}), "lists": regime.lists(sub)}


@route("GET", "/ww2")
def ww2_get(sub, claims, body, q):
    """Wall watch 2 (long + short copy of the wall watch): the saved settings and the last list backtest."""
    import ww2
    return {"cfg": ww2.get_cfg(sub), "defaults": ww2.PARAMS, "bt": ww2.bt_public(sub), "mag7": ww2.MAG7}


@route("PUT", "/ww2/cfg")
def ww2_cfg(sub, claims, body, q):
    import ww2
    return {"cfg": ww2.save_cfg(sub, (body or {}).get("cfg") or {})}


@route("POST", "/ww2/chart")
def ww2_chart(sub, claims, body, q):
    import ww2
    b = body or {}
    return ww2.chart(sub, b.get("t"), b.get("tf") or "1D", b.get("days") or 250, b.get("cfg") if isinstance(b.get("cfg"), dict) else {})


@route("POST", "/ww2/bt")
def ww2_bt(sub, claims, body, q):
    """The settings on the Mag 7 or every watchlist ticker, train / test (background)."""
    import ww2
    return ww2.bt_start(sub, body or {})


@route("GET", "/ww2/bt")
def ww2_bt_get(sub, claims, body, q):
    import ww2
    return ww2.bt_public(sub)


@route("POST", "/daytrade/smc/windows")
def daytrade_smc_windows(sub, claims, body, q):
    """The blocks in the morning / midday / afternoon windows, one ticker (the page adds QQQ and SPY up)."""
    import smc
    b = body or {}
    return smc.windows(sub, b.get("t"), b.get("days") or 30, b.get("cfg") if isinstance(b.get("cfg"), dict) else None, float(b.get("cost") or 0.05))


@route("POST", "/daytrade/smc/stress")
def daytrade_smc_stress(sub, claims, body, q):
    """Smart-money stress test of one ticker (the page runs it ticker by ticker and adds them up)."""
    import smc
    b = body or {}
    return smc.stress(sub, b.get("t"), b.get("days") or 30, b.get("cfg") if isinstance(b.get("cfg"), dict) else None, float(b.get("cost") or 0.05))


@route("PUT", "/daytrade/feed")
def daytrade_feed(sub, claims, body, q):
    """The day-trading account's market data: iex (free) or sip (Algo Trader Plus, real time)."""
    import daytrade
    return daytrade.set_feed(sub, (body or {}).get("feed"))


@route("GET", "/daytrade/smc/live")
def daytrade_smc_live(sub, claims, body, q):
    import smclive
    return smclive.view(sub)


@route("POST", "/daytrade/smc/live/speed")
def daytrade_smc_live_speed(sub, claims, body, q):
    """Crypto speed test (any hour): $10 BTC bought and sold at market on the paper account, every step timed."""
    import smclive
    return smclive.start_speed(sub)


@route("POST", "/daytrade/smc/live/test")
def daytrade_smc_live_test(sub, claims, body, q):
    """Loop test: 1 share of SPY bought at market, exits at the broker, closed by the minute run after 2 minutes."""
    import smclive
    return smclive.arm_test(sub, ((body or {}).get("t") or "SPY").upper())


@route("GET", "/daytrade/smc/live/check")
def daytrade_smc_live_check(sub, claims, body, q):
    """Why a trade did / didn't go out: the live state right now, nothing sent."""
    import smclive
    return smclive.check(sub)


@route("PUT", "/daytrade/smc/live")
def daytrade_smc_live_set(sub, claims, body, q):
    """Smart money on the day-trading paper account: save the strategy, watch / trade on-off, size."""
    import smclive
    return smclive.save(sub, body or {})


@route("GET", "/daytrade/smc/opt")
def daytrade_smc_opt(sub, claims, body, q):
    import smcopt
    return smcopt.public(sub)


@route("PUT", "/daytrade/smc/opt")
def daytrade_smc_opt_set(sub, claims, body, q):
    """Smart money optimizer: on / off, days, restart, the blocks to start from."""
    import research
    import smcopt
    b = body or {}
    out = smcopt.save_cfg(sub, b)
    if b.get("on") and not out.get("running"):
        research._invoke(sub, "smcopt", {})
    return out


@route("POST", "/daytrade/chart")
def daytrade_chart(sub, claims, body, q):
    import daytrade
    b = body or {}
    cfg = b.get("cfg") if isinstance(b.get("cfg"), dict) else None
    return daytrade.chart(sub, b.get("t"), b.get("days") or 3, b.get("tf") or 1, cfg, b.get("end"))


@route("POST", "/daytrade/backtest")
def daytrade_backtest(sub, claims, body, q):
    import daytrade
    b = body or {}
    cfg = b.get("cfg") if isinstance(b.get("cfg"), dict) else None
    db.put({"PK": db.upk(sub), "SK": daytrade.BT_SK, **daytrade.backtest_result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "dtbt", "params": {"days": b.get("days") or 20, "cfg": cfg, "opt": b.get("opt") if isinstance(b.get("opt"), dict) else None}}, default=str))
    return {"status": "running"}


@route("GET", "/daytrade/presets")
def daytrade_presets(sub, claims, body, q):
    import daytrade
    return {"presets": daytrade.presets(sub)}


@route("PUT", "/daytrade/presets")
def daytrade_presets_put(sub, claims, body, q):
    import daytrade
    b = body or {}
    return {"presets": daytrade.save_preset(sub, b.get("name"), b.get("cfg") if isinstance(b.get("cfg"), dict) else {})}


@route("DELETE", "/daytrade/presets")
def daytrade_presets_del(sub, claims, body, q):
    import daytrade
    return {"presets": daytrade.delete_preset(sub, (q or {}).get("name") or (body or {}).get("name"))}


@route("POST", "/daytrade/compare")
def daytrade_compare(sub, claims, body, q):
    import daytrade
    b = body or {}
    vs = [v for v in (b.get("variants") or []) if isinstance(v, dict)][:10]
    if not vs:
        raise BadRequest("Pick at least one setting to compare.")
    db.put({"PK": db.upk(sub), "SK": daytrade.CMP_SK, **daytrade.compare_result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "dtcmp", "params": {"days": b.get("days") or 60, "variants": vs,
                                                                               "opt": b.get("opt") if isinstance(b.get("opt"), dict) else None}}, default=str))
    return {"status": "running"}


@route("GET", "/daytrade/compare")
def daytrade_compare_get(sub, claims, body, q):
    import daytrade
    return daytrade.compare_result(sub)


@route("POST", "/daytrade/search")
def daytrade_search(sub, claims, body, q):
    import dtsearch
    b = body or {}
    db.put({"PK": db.upk(sub), "SK": dtsearch.SK, **dtsearch.result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "dtsearch", "params": {"days": b.get("days") or 250, "rounds": b.get("rounds") or 3,
                                                                                  "cfg": b.get("cfg") if isinstance(b.get("cfg"), dict) else None}}, default=str))
    return {"status": "running"}


@route("GET", "/daytrade/search")
def daytrade_search_get(sub, claims, body, q):
    import dtsearch
    return dtsearch.result(sub)


@route("GET", "/research")
def research_get(sub, claims, body, q):
    import research
    st = (q or {}).get("strategy")
    if (q or {}).get("lite"):                          # the page's status check: a few hundred bytes
        return research.lite(sub, st if st in research.STRATS else None)
    return research.state(sub, st if st in research.STRATS else None)


@route("PUT", "/research/criteria")
def research_criteria(sub, claims, body, q):
    import research
    c = research.save_criteria(sub, body or {})
    try:
        queued = research.rob_backfill(sub)
    except Exception:
        queued = 0
    return {"criteria": c, "robQueued": queued}


@route("POST", "/research/run")
def research_run(sub, claims, body, q):
    import research
    b = body or {}
    st = research.state(sub)
    if (st.get("run") or {}).get("running"):
        return {"status": "running"}
    strategy = b.get("strategy") if b.get("strategy") in research.STRATS else "day"
    research._set_run(sub, {"running": iso(now_ny()), "strategy": strategy, "at": iso(now_ny()), "step": "starting", "errors": []})
    if "focus" in b:                                   # the focus is kept: autopilot runs use it too
        research.save_auto(sub, strategy, {"focus": b.get("focus")})
    params = {"strategy": strategy, "n": b.get("n") or 4, "rounds": b.get("rounds") or 1, "days": b.get("days") or 250,
              "focus": str(b.get("focus") or "")[:600], "catalog": b.get("catalog") if isinstance(b.get("catalog"), list) else None,
              "lab": b.get("lab") if isinstance(b.get("lab"), dict) else None}
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "research", "params": params}, default=str))
    return {"status": "running"}


@route("POST", "/research/idea")
def research_idea(sub, claims, body, q):
    """An idea of your own: the same pipeline (no AI involved)."""
    import research
    b = body or {}
    strategy = b.get("strategy") if b.get("strategy") in research.STRATS else "day"
    if not isinstance(b.get("changes"), dict) or not b["changes"]:
        raise BadRequest("No settings to test.")
    if (research.state(sub).get("run") or {}).get("running"):
        raise BadRequest("A research run is going on: try again when it is done.")
    lab = b.get("lab") if isinstance(b.get("lab"), dict) else None
    x = research.submit(sub, strategy, b.get("name") or "my idea", b["changes"], lab, int(b.get("days") or 250), b.get("hypothesis") or "")
    if x["status"] == "queued":
        research._set_run(sub, {"running": iso(now_ny()), "strategy": strategy, "at": iso(now_ny()), "step": "starting", "errors": []})
        _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                         Payload=json.dumps({"sub": sub, "job": "rstest", "params": {"strategy": strategy, "days": b.get("days") or 250, "lab": lab}}, default=str))
    return {"idea": x}


@route("PUT", "/research/auto")
def research_auto(sub, claims, body, q):
    """Autopilot: the agent proposes + the pipeline tests on its own, every hour, up to N runs a day."""
    import research
    b = body or {}
    strategy = b.get("strategy") if b.get("strategy") in research.STRATS else "day"
    if isinstance(b.get("catalog"), list):
        research.save_catalog(sub, strategy, b["catalog"])
    a = research.save_auto(sub, strategy, b)
    if a[strategy]["on"]:                              # start now, not at the next hour
        research._start(sub, research.auto_due(sub))
    return {"auto": a, "autoToday": research.auto_today(sub)}


@route("POST", "/research/marketctx")
def research_mctx(sub, claims, body, q):
    """Load the market context (options flow, gamma, breadth, VIXY) of the last N days in the background."""
    import marketctx
    n = max(20, min(500, int((body or {}).get("days") or 250)))
    started = marketctx.kick(sub, n, retry=True)        # a click also retries the days that had nothing
    return {"started": started, "state": marketctx.job_state(sub)}


@route("POST", "/research/strategy")
def research_strat_save(sub, claims, body, q):
    """Save a strategy (an idea of the record, or settings): kept, shown to the agent, re-checked every day."""
    import research
    b = body or {}
    try:
        x = research.save_strategy(sub, b.get("strategy") or "day", b.get("name"), b.get("cfg") if isinstance(b.get("cfg"), dict) else None, b.get("ideaId"))
    except ValueError as e:
        raise BadRequest(str(e))
    research._invoke(sub, "rsstrat", {})              # first check right away
    return {"strategy": x}


@route("POST", "/research/sipprep")
def research_sipprep(sub, claims, body, q):
    """Build / extend the stocks-in-play research history (background, resumable)."""
    import research
    import sipbench
    st = sipbench.prep_state()
    if st.get("running") and st["running"] > iso(now_ny() - timedelta(minutes=12)):
        return {"status": "running", "sip": st}
    research._invoke(sub, "sipprep", {"days": int((body or {}).get("days") or 500)})
    return {"status": "running", "sip": {**st, "running": iso(now_ny()), "progress": "starting"}}


@route("PUT", "/research/evolve")
def research_evolve(sub, claims, body, q):
    """The genetic search: on / off, history days, reset; turning it on starts the first run."""
    import evolve
    import research
    b = body or {}
    out = evolve.save_cfg(sub, b)
    if b.get("on") or b.get("run"):
        research._invoke(sub, "evolve", {} if b.get("on") else {"once": True})
    return {"evolve": out}


@route("POST", "/research/holdout")
def research_holdout(sub, claims, body, q):
    """The final check of one finalist on the holdout days (runs in the background; the result lands on the idea)."""
    import research
    b = body or {}
    if not int(research.criteria(sub).get("holdoutDays") or 0):
        raise BadRequest("No holdout: set holdout days in the criteria first.")
    research._invoke(sub, "rshold", {"id": b.get("id"), "sid": b.get("sid")})
    return {"status": "running"}


@route("PUT", "/research/strategy/stage")
def research_strat_stage(sub, claims, body, q):
    """A saved strategy's stage by hand: restart its paper period, drop it, or promote it."""
    import research
    b = body or {}
    try:
        st = research.set_stage(sub, b.get("id"), b.get("action"))
    except ValueError as e:
        raise BadRequest(str(e))
    return {"stage": st, "basket": research.basket(sub)}


@route("PUT", "/research/basket")
def research_basket(sub, claims, body, q):
    """Basket settings (auto-switch on / off, which side, margin) or put one strategy in use (body.use = id)."""
    import research
    b = body or {}
    try:
        if b.get("use"):
            research.use_strategy(sub, b["use"])
        else:
            research.save_basket(sub, b)
            if b.get("auto"):
                research.basket_switch(sub)
    except ValueError as e:
        raise BadRequest(str(e))
    return {"basket": research.basket(sub)}


@route("POST", "/research/strategy/check")
def research_strat_check(sub, claims, body, q):
    import research
    research._invoke(sub, "rsstrat", {"days": (body or {}).get("days") or 250})
    return {"status": "running"}


@route("DELETE", "/research/strategy")
def research_strat_del(sub, claims, body, q):
    import research
    return {"deleted": research.delete_strategy(sub, (q or {}).get("id"))}


@route("DELETE", "/research/idea")
def research_del(sub, claims, body, q):
    import research
    return {"deleted": research.delete_idea(sub, (q or {}).get("id"))}


@route("POST", "/daytrade/robust")
def daytrade_robust(sub, claims, body, q):
    import dtrobust
    b = body or {}
    db.put({"PK": db.upk(sub), "SK": dtrobust.SK, **dtrobust.result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "dtrob", "params": {"days": b.get("days") or 250, "cost": b.get("cost"),
                                                                               "cfg": b.get("cfg") if isinstance(b.get("cfg"), dict) else None}}, default=str))
    return {"status": "running"}


@route("GET", "/daytrade/robust")
def daytrade_robust_get(sub, claims, body, q):
    import dtrobust
    return dtrobust.result(sub)


@route("GET", "/daytrade/flipcheck")
def daytrade_flipcheck(sub, claims, body, q):
    import daytrade
    return daytrade.flip_check(sub)


@route("GET", "/stockday")
def stockday_get(sub, claims, body, q):
    import stockday
    return stockday.view(sub)


@route("PUT", "/stockday")
def stockday_put(sub, claims, body, q):
    import stockday
    stockday.save_config(sub, body or {})
    return stockday.view(sub)


@route("PUT", "/stockday/account")
def stockday_account(sub, claims, body, q):
    import stockday
    b = body or {}
    return stockday.connect(sub, b.get("key"), b.get("secret"))


@route("DELETE", "/stockday/account")
def stockday_account_del(sub, claims, body, q):
    import stockday
    db.delete(db.upk(sub), stockday.CRED_SK)
    return stockday.view(sub)


@route("POST", "/stockday/prep")
def stockday_prep(sub, claims, body, q):
    import stockday
    b = body or {}
    st = stockday.prep_status(sub)
    if st.get("running") and st["running"] > iso(now_ny() - timedelta(minutes=12)):
        return {"status": "running"}
    db.put({"PK": db.upk(sub), "SK": stockday.PREP_SK, **st, "running": iso(now_ny()), "error": None})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "sdprep", "params": {"days": b.get("days") or 500}}, default=str))
    return {"status": "running"}


@route("POST", "/stockday/watch")
def stockday_watch(sub, claims, body, q):
    import stockday
    d = now_ny().strftime("%Y-%m-%d")
    db.put({"PK": db.upk(sub), "SK": f"SDWATCH#{d}", **{k: v for k, v in (db.get(db.upk(sub), f"SDWATCH#{d}") or {}).items() if k not in ("PK", "SK")},
            "building": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "sdwatch", "params": {}}, default=str))
    return stockday.view(sub)


@route("POST", "/stockday/backtest")
def stockday_bt(sub, claims, body, q):
    import stockday
    b = body or {}
    cfg = b.get("cfg") if isinstance(b.get("cfg"), dict) else None
    db.put({"PK": db.upk(sub), "SK": stockday.BT_SK, **stockday.backtest_result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "sdbt", "params": {"days": b.get("days") or 250, "cfg": cfg}}, default=str))
    return {"status": "running"}


@route("GET", "/stockday/backtest")
def stockday_bt_get(sub, claims, body, q):
    import stockday
    return stockday.backtest_result(sub)


@route("POST", "/stockday/chart")
def stockday_chart(sub, claims, body, q):
    import stockday
    b = body or {}
    return stockday.chart(sub, b.get("t"), b.get("date"))


@route("POST", "/flow")
def flow_days(sub, claims, body, q):
    """Real order flow (CVD + largest trades) of a symbol for the days on a 1-minute chart."""
    import orderflow
    b = body or {}
    sym = str(b.get("t") or "").upper().strip()[:10]
    dates = sorted({str(x)[:10] for x in (b.get("dates") or []) if str(x)[:4].isdigit()})[-10:]
    if not sym or not dates:
        raise BadRequest("Give a ticker and the days.")
    days, missing = orderflow.get_days(sub, sym, dates)
    running = False
    if missing:
        st = db.get(db.upk(sub), f"{orderflow.SK_JOB}#{sym}") or {}
        running = bool(st.get("running") and st["running"] > iso(now_ny() - timedelta(minutes=10)))
        if not running:
            db.put({"PK": db.upk(sub), "SK": f"{orderflow.SK_JOB}#{sym}", "running": iso(now_ny())})
            _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                             Payload=json.dumps({"sub": sub, "job": "oflow", "params": {"sym": sym, "dates": missing}}))
            running = True
    return {"symbol": sym, "days": days, "missing": missing, "running": running}


@route("POST", "/daytrade/why")
def daytrade_why(sub, claims, body, q):
    import daytrade
    b = body or {}
    return daytrade.why(sub, b.get("t"), b.get("at"), b.get("cfg") if isinstance(b.get("cfg"), dict) else None)


@route("POST", "/daytrade/study")
def daytrade_study(sub, claims, body, q):
    import daystudy
    b = body or {}
    db.put({"PK": db.upk(sub), "SK": daystudy.SK, **daystudy.result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "dtstudy", "params": {"days": b.get("days") or 120, "tickers": b.get("tickers"), **({"wallNear": bool(b["wallNear"])} if "wallNear" in b else {})}}, default=str))
    return {"status": "running"}


@route("GET", "/daytrade/study")
def daytrade_study_get(sub, claims, body, q):
    import daystudy
    return daystudy.result(sub)


@route("GET", "/daytrade/backtest")
def daytrade_backtest_get(sub, claims, body, q):
    import daytrade
    return daytrade.backtest_result(sub)


@route("GET", "/bot/wallwatch/quotes")
def bot_wallwatch_quotes(sub, claims, body, q):
    import flowdata
    import wallwatch
    return flowdata.stock_quotes(sub, wallwatch.config(sub)["tickers"])


@route("POST", "/bot/wallwatch/review")
def bot_wallwatch_review(sub, claims, body, q):
    import wallwatch
    return wallwatch.review_open(sub, auto_close=bool((body or {}).get("close", True)))


@route("PUT", "/bot/wallwatch")
def bot_wallwatch_save(sub, claims, body, q):
    import wallwatch
    wallwatch.save_config(sub, body or {})
    return wallwatch.view(sub)


@route("GET", "/bot/wallwatch/chart")
def bot_wallwatch_chart(sub, claims, body, q):
    import wallwatch
    try:
        days = max(20, min(250, int((q or {}).get("days") or 45)))
    except (TypeError, ValueError):
        days = 45
    return wallwatch.chart(sub, (q or {}).get("t"), days=days, flow=(q or {}).get("flow") != "0")


@route("GET", "/bot/wallwatch/darkpool")
def bot_wallwatch_darkpool(sub, claims, body, q):
    import wallwatch
    q = q or {}
    try:
        days = max(20, min(250, int(q.get("days") or 45)))
    except (TypeError, ValueError):
        days = 45
    return wallwatch.darkpool(sub, q.get("t"), q.get("pct"), days)


@route("POST", "/bot/wallwatch/flowstudy")
def bot_wallwatch_flowstudy(sub, claims, body, q):
    """Match every signal with the options flow before it (background job)."""
    db.put({**(db.get(db.upk(sub), "WWFSTUDY") or {}), "PK": db.upk(sub), "SK": "WWFSTUDY", "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwflowstudy"}))
    return {"status": "running"}


@route("POST", "/bot/wallwatch/walls")
def bot_wallwatch_walls(sub, claims, body, q):
    """Fetch every day's call wall, put wall and flip for every watchlist ticker (45 days), then find the signals."""
    t = ((body or {}).get("t") or "").upper().replace("/", ".")[:10]
    sk = f"WWFILL#{t}" if t else "WWFILL"
    fill = db.get(db.upk(sub), sk) or {}
    db.put({**fill, "PK": db.upk(sub), "SK": sk, "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwfill", **({"t": t} if t else {})}))
    return {"status": "running"}


@route("GET", "/bot/wallwatch/opt")
def wallwatch_opt(sub, claims, body, q):
    import wwopt
    return wwopt.public(sub)


@route("PUT", "/bot/wallwatch/opt")
def wallwatch_opt_set(sub, claims, body, q):
    """Unusual-flow (wall watch) optimizer: on / off, days, restart."""
    import research
    import wwopt
    b = body or {}
    out = wwopt.save_cfg(sub, b)
    if b.get("on") and not out.get("running"):
        research._invoke(sub, "wwopt", {})
    return out


@route("GET", "/bot/wallwatch/edge")
def wallwatch_edge(sub, claims, body, q):
    import wwopt
    return wwopt.edge_public(sub)


@route("POST", "/bot/wallwatch/edge")
def wallwatch_edge_run(sub, claims, body, q):
    """Edge builder: each daily rule alone vs the baseline, then the ladder (background)."""
    import wwopt
    return wwopt.edge_start(sub)


@route("POST", "/bot/wallwatch/opt/walls")
def wallwatch_opt_walls(sub, claims, body, q):
    """The walls history of the optimizer's tickers over its whole period (background, chained runs)."""
    import wwopt
    return wwopt.walls_start(sub)


@route("GET", "/bot/wallwatch/walls")
def bot_wallwatch_walls_status(sub, claims, body, q):
    """The walls fetch of one ticker (or of the watchlist): running / left / at. Cheap: polled while it runs."""
    t = (q.get("t") or "").upper().replace("/", ".")[:10]
    f = db.get(db.upk(sub), f"WWFILL#{t}" if t else "WWFILL") or {}
    return {k: f.get(k) for k in ("running", "total", "left", "at", "errors")}


@route("GET", "/bot/wallwatch/ruletest")
def bot_ruletest_get(sub, claims, body, q):
    import backtest
    return {"test": backtest.test_result(sub), "history": backtest.hist_status(sub), "robust": backtest.robust_result(sub),
            "pv": backtest.pv_result(sub), "regime": backtest.regime_result(sub), "search": backtest.search_result(sub)}


@route("POST", "/bot/wallwatch/pvstudy")
def bot_pvstudy(sub, claims, body, q):
    import backtest
    db.put({"PK": db.upk(sub), "SK": backtest.PV_SK, **backtest.pv_result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwpvstudy", "params": {"days": (body or {}).get("days") or 250}}))
    return {"status": "running"}


@route("POST", "/bot/wallwatch/regime")
def bot_regime(sub, claims, body, q):
    import backtest
    db.put({"PK": db.upk(sub), "SK": backtest.REG_SK, **backtest.regime_result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwregime", "params": {"days": (body or {}).get("days") or 250}}))
    return {"status": "running"}


@route("POST", "/bot/lab/chart")
def bot_lab_chart(sub, claims, body, q):
    import wallwatch
    b = body or {}
    try:
        days = max(20, min(250, int(b.get("days") or 45)))
    except (TypeError, ValueError):
        days = 45
    cfg = b.get("cfg") if isinstance(b.get("cfg"), dict) else {}
    return wallwatch.chart(sub, b.get("t"), days=days, flow=b.get("flow", True) is not False, lab=cfg or {"_": 1})


@route("GET", "/bot/lab/options")
def bot_lab_options_get(sub, claims, body, q):
    import optiontest
    return optiontest.result(sub)


@route("GET", "/bot/lab/chartoptions")
def bot_lab_chartopt_get(sub, claims, body, q):
    import optiontest
    return optiontest.result_sym(sub, (q.get("t") or "").upper())


@route("POST", "/bot/lab/chartoptions")
def bot_lab_chartopt(sub, claims, body, q):
    import optiontest
    b = body or {}
    sym = str(b.get("t") or "").upper()[:10]
    if not sym:
        raise BadRequest("Pick a ticker.")
    trades = [{k: x.get(k) for k in ("at", "entry", "exitAt", "exit", "R", "pct")} for x in (b.get("trades") or []) if isinstance(x, dict)][-60:]
    P = {}
    for k, lo, hi in (("dteMin", 0, 120), ("dteMax", 1, 180), ("money", -30, 30), ("tp", 0, 1000), ("sl", 0, 100), ("slip", 0, 20)):
        if b.get(k) not in (None, ""):
            try:
                P[k] = max(lo, min(hi, float(b[k])))
            except (TypeError, ValueError):
                pass
    P["follow"] = b.get("follow") is not False
    P["sel"] = "manual" if b.get("sel") == "manual" else "live"
    db.put({"PK": db.upk(sub), "SK": f"WWOPTSYM#{sym}", **optiontest.result_sym(sub, sym), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwoptsym", "params": {"sym": sym, "trades": trades, "P": P}}, default=str))
    return {"status": "running"}


@route("POST", "/bot/lab/options")
def bot_lab_options(sub, claims, body, q):
    import optiontest
    b = body or {}
    P = {}
    for k, lo, hi in (("dteMin", 0, 120), ("dteMax", 1, 180), ("money", -30, 30), ("tp", 0, 1000), ("sl", 0, 100), ("slip", 0, 20), ("maxTrades", 10, 400)):
        if b.get(k) not in (None, ""):
            try:
                P[k] = max(lo, min(hi, float(b[k])))
            except (TypeError, ValueError):
                pass
    P["follow"] = b.get("follow") is not False
    P["sel"] = "manual" if b.get("sel") == "manual" else "live"
    if P.get("dteMax", 21) < P.get("dteMin", 7):
        P["dteMax"] = P.get("dteMin", 7)
    db.put({"PK": db.upk(sub), "SK": optiontest.OPT_SK, **optiontest.result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwopt", "params": P}))
    return {"status": "running"}


@route("GET", "/bot/lab/test")
def bot_lab_test_get(sub, claims, body, q):
    import backtest
    return backtest.lab_result(sub)


@route("POST", "/bot/lab/test")
def bot_lab_test(sub, claims, body, q):
    import backtest
    b = body or {}
    cfg = b.get("cfg") if isinstance(b.get("cfg"), dict) else {}
    db.put({"PK": db.upk(sub), "SK": backtest.LAB_SK, **backtest.lab_result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwlab", "params": {"days": b.get("days") or 250, "cfg": cfg}}))
    return {"status": "running"}


@route("POST", "/bot/wallwatch/search")
def bot_search(sub, claims, body, q):
    import backtest
    b = body or {}
    db.put({"PK": db.upk(sub), "SK": backtest.SEARCH_SK, **backtest.search_result(sub), "running": iso(now_ny())})
    params = {"days": b.get("days") or 250, "minTrades": b.get("minTrades") or 60}
    if isinstance(b.get("cfg"), dict):
        params["cfg"] = b["cfg"]
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwsearch", "params": params}))
    return {"status": "running"}


@route("POST", "/bot/wallwatch/robust")
def bot_robust(sub, claims, body, q):
    import backtest
    db.put({"PK": db.upk(sub), "SK": backtest.ROB_SK, **backtest.robust_result(sub), "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwrobust", "params": {"days": (body or {}).get("days") or 250,
                                                                                    **({"cfg": body["cfg"]} if isinstance((body or {}).get("cfg"), dict) else {})}}))
    return {"status": "running"}


@route("POST", "/bot/wallwatch/history")
def bot_history(sub, claims, body, q):
    """Load the call alerts and the walls of the last `days` trading days for every watchlist ticker (background)."""
    import backtest
    try:
        days = int((body or {}).get("days") or 250)
    except (TypeError, ValueError):
        days = 250
    st = backtest.start_history(sub, days)
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwhist"}))
    return st


@route("POST", "/bot/wallwatch/ruletest")
def bot_ruletest(sub, claims, body, q):
    import backtest
    b = body or {}
    params = {}
    params["full"] = bool(b.get("full"))
    if isinstance(b.get("cfg"), dict):                 # Strategy lab settings (not saved)
        params["cfg"] = b["cfg"]
    for k, lo, hi in (("days", 30, 500), ("stopAtr", 0.1, 5), ("fixR", 0, 10), ("maxDays", 0, 30)):
        if b.get(k) not in (None, ""):
            try:
                params[k] = max(lo, min(hi, float(b[k])))
            except (TypeError, ValueError):
                pass
    cur = backtest.test_result(sub)
    db.put({"PK": db.upk(sub), "SK": backtest.TEST_SK, **cur, "running": iso(now_ny()), "params": params})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwruletest", "params": params}))
    return {"status": "running"}


@route("GET", "/bot/wallwatch/intraday")
def bot_intraday_get(sub, claims, body, q):
    import intraday
    return intraday.result(sub)


@route("POST", "/bot/wallwatch/intraday")
def bot_intraday(sub, claims, body, q):
    """Intraday gamma-level test on 5-minute candles (background job)."""
    import intraday
    b, params = body or {}, {}
    for k, lo, hi in (("fastK", 0.5, 20), ("appBars", 2, 24), ("zoneK", 0, 5), ("bufK", 0, 5), ("minRR", 0, 10),
                      ("maxRiskD", 0.05, 5), ("maxTrades", 1, 10)):
        if b.get(k) not in (None, ""):
            try:
                params[k] = max(lo, min(hi, float(b[k])))
            except (TypeError, ValueError):
                pass
    for k in ("appBars", "maxTrades"):
        if k in params:
            params[k] = int(params[k])
    for k in ("watchlist", "skipEvents", "longs", "shorts", "useFlip"):
        if k in b:
            params[k] = bool(b[k])
    if b.get("tickers"):
        params["tickers"] = str(b["tickers"])[:400]
    if b.get("sessionEnd") and len(str(b["sessionEnd"])) == 5:
        params["sessionEnd"] = str(b["sessionEnd"])
    cur = intraday.result(sub)
    db.put({"PK": db.upk(sub), "SK": intraday.SK, **cur, "running": iso(now_ny()), "params": params})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wwintraday", "params": params}))
    return {"status": "running"}


@route("POST", "/bot/wallwatch/run")
def bot_wallwatch_run(sub, claims, body, q):
    """Check the watchlist now (days=3) or backfill the last `days` trading days (background job)."""
    try:
        days = max(3, min(60, int((body or {}).get("days") or 3)))
    except (TypeError, ValueError):
        days = 3
    db.put({**(db.get(db.upk(sub), "WWRUN") or {}), "PK": db.upk(sub), "SK": "WWRUN", "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "wallwatch", "days": days}))
    return {"status": "running"}


@route("POST", "/bot/walls/backfill")
def bot_walls_backfill(sub, claims, body, q):
    """Store the call / put walls of every day for every evaluated ticker (runs in the background)."""
    db.put({**(db.get(db.upk(sub), "WALLSRUN") or {}), "PK": db.upk(sub), "SK": "WALLSRUN", "running": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "walls"}))
    return {"status": "running"}


@route("POST", "/bot/flow/test")
def bot_flow_test(sub, claims, body, q):
    """Is the Unusual Whales feed alive (one request, no filters: the newest alerts), and how many alerts pass your
    flow filters in the last 20 and 60 minutes (to tell a quiet market from a problem)."""
    import autotrader, flowdata, time as _t
    from datetime import datetime, timedelta as _td
    t0 = _t.time()
    try:
        r = flowdata.ping(sub)
    except Exception as e:
        db.update(db.upk(sub), "BOTSTATE", {"uwErrorAt": iso(now_ny()), "uwError": str(e)[:240]})
        return {"ok": False, "error": str(e)[:240]}
    ms = round((_t.time() - t0) * 1000)
    db.update(db.upk(sub), "BOTSTATE", {"uwOkAt": iso(now_ny())})
    cfg = autotrader.settings(sub)
    out = {"ok": True, **r, "ms": ms}
    try:
        since = (datetime.utcnow() - _td(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = flowdata.alerts(sub, cfg["flowMinPremium"], "call", cfg["flowMinDte"], cfg["flowMaxDte"], cfg["flowAskSide"],
                              cfg["flowSweeps"], None, cfg["flowMinVolOi"], 1, since_utc=since, exclude_etfs=cfg["excludeEtfs"])
        al = res.get("alerts") or []
        cut20 = (datetime.utcnow() - _td(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S")
        out.update(filtered60=len(al), filtered20=sum(1 for a in al if (a.get("at") or "")[:19] >= cut20),
                   filteredTickers=len({a.get("ticker") for a in al}), filteredNewest=max([a.get("atEt") or "" for a in al], default=None))
    except Exception as e:
        out["filterError"] = str(e)[:200]
    return out


@route("POST", "/bot/explore")
def bot_explore(sub, claims, body, q):
    """Filter every evaluation (taken and skipped) by any combination of conditions; optionally search the best ones."""
    import explorer
    try:
        days = max(1, min(60, int(body.get("days") or 35)))
        min_n = max(5, min(200, int(body.get("minN") or 15)))
        depth = max(1, min(6, int(body.get("depth") or 3)))
    except (TypeError, ValueError):
        days, min_n, depth = 35, 15, 3
    conds = body.get("conds") if isinstance(body.get("conds"), list) else []
    return explorer.explore(sub, days, conds[:25], bool(body.get("search")), min_n, body.get("group") or None, depth)


@route("POST", "/bot/exitopt")
def bot_exitopt(sub, claims, body, q):
    """Best exit rules for the ideas matching some conditions, replayed on their stored price paths."""
    import explorer
    try:
        days = max(1, min(60, int(body.get("days") or 35)))
        min_n = max(5, min(200, int(body.get("minN") or 15)))
    except (TypeError, ValueError):
        days, min_n = 35, 15
    conds = body.get("conds") if isinstance(body.get("conds"), list) else []
    try:
        mult = max(0.0, min(3.0, float(body.get("spreadMult", 1))))
    except (TypeError, ValueError):
        mult = 1.0
    return explorer.best_exit(sub, days, conds[:25], min_n, spread_mult=mult)


@route("GET", "/bot/strategies")
def bot_strategies(sub, claims, body, q):
    import strats
    return {"items": strats.results(sub), "active": strats.active_info(sub), "stops": list(strats.STOPS)}


@route("PUT", "/bot/strategies/active")
def bot_strategy_active(sub, claims, body, q):
    import strats
    return strats.set_active(sub, body)


@route("POST", "/bot/strategies")
def bot_strategy_create(sub, claims, body, q):
    import strats
    return strats.create(sub, body)


@route("PUT", r"/bot/strategies/(?P<sid>[a-f0-9]{10})")
def bot_strategy_update(sub, claims, body, q, sid):
    import strats
    return strats.update(sub, sid, body)


@route("DELETE", r"/bot/strategies/(?P<sid>[a-f0-9]{10})")
def bot_strategy_delete(sub, claims, body, q, sid):
    import strats
    return strats.delete(sub, sid)


@route("POST", "/bot/montecarlo")
def bot_montecarlo(sub, claims, body, q):
    import explorer
    s = (db.get(db.upk(sub), "PROFILE") or {}).get("settings") or {}
    try:
        days = max(1, min(60, int(body.get("days") or 35)))
        trades = int(body.get("trades") or 100)
        risk = float(body.get("risk") or s.get("riskPerTrade") or 0) or None
        account = float(body.get("account") or s.get("accountSize") or 0) or None
    except (TypeError, ValueError):
        raise BadRequest("Check the numbers.")
    conds = body.get("conds") if isinstance(body.get("conds"), list) else []
    return explorer.monte_carlo(sub, days, conds[:25], trades, 5000, risk, account)


@route("GET", "/bot/bursts")
def bot_bursts(sub, claims, body, q):
    import bursts
    try:
        days = max(1, min(30, int(q.get("days") or 10)))
    except ValueError:
        days = 10
    return bursts.page(sub, days)


@route("POST", "/bot/bursts/replay")
def bot_bursts_replay(sub, claims, body, q):
    """Start (restart=true) or continue the 30-day burst replay in the bot function (a few minutes per part)."""
    pk = db.upk(sub)
    st = db.get(pk, "BURSTSTUDY") or {}
    restart = bool(body.get("restart")) or st.get("status") not in ("partial",)
    if restart:
        db.put({"PK": pk, "SK": "BURSTSTUDY", "status": "starting", "startedAt": iso(now_ny()), "done": 0, "universe": [], "bursts": []})
    else:
        db.update(pk, "BURSTSTUDY", {"status": "running"})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "burstreplay", "restart": restart}))
    return {"status": "running"}


@route("GET", "/bot/flow/summary")
def bot_flow_summary(sub, claims, body, q):
    import shadow
    try:
        days = max(1, min(35, int(q.get("days") or 35)))
    except ValueError:
        days = 35
    return shadow.flow_summary(sub, days)


@route("GET", "/bot/shadow/list")
def bot_shadow_list(sub, claims, body, q):
    import shadow
    try:
        days = max(1, min(35, int(q.get("days") or 35)))
    except ValueError:
        days = 35
    return shadow.skipped_list(sub, days, why=q.get("why"), claude=q.get("claude"))


@route("GET", "/bot/shadow/summary")
def bot_shadow_summary(sub, claims, body, q):
    import shadow
    try:
        days = max(1, min(35, int(q.get("days") or 5)))
    except ValueError:
        days = 5
    return shadow.summary(sub, days)


@route("GET", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})")
def bot_one(sub, claims, body, q, bid):
    rec = db.get(db.upk(sub), f"BOT#{bid}")
    if not rec:
        raise NotFound("Not found.")
    return {k: v for k, v in rec.items() if k not in ("PK", "SK")}


@route("POST", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})/aicheck")
def bot_aicheck(sub, claims, body, q, bid):
    """Claude's chart check runs in the bot function (it takes ~20 s); the page polls the record for aiCheck."""
    if not db.get(db.upk(sub), f"BOT#{bid}"):
        raise NotFound("Not found.")
    db.update(db.upk(sub), f"BOT#{bid}", {"aiRunning": True, "aiError": "", "aiRequestedAt": iso(now_ny())})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "aicheck", "id": bid}))
    return {"status": "running"}


@route("POST", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})/aireview")
def bot_aireview(sub, claims, body, q, bid):
    """On-demand hold/close review of an open position (doesn't auto-close; the end-of-day run does)."""
    rec = db.get(db.upk(sub), f"BOT#{bid}")
    if not rec:
        raise NotFound("Not found.")
    if rec.get("status") != "open":
        raise BadRequest("Only open positions can be reviewed.")
    import aicheck
    if aicheck.is_wallwatch(rec):
        raise BadRequest("Wall watch trades are managed only by the wall watch exits (stop, trail, call wall, flip close).")
    db.update(db.upk(sub), f"BOT#{bid}", {"aiReviewRunning": True, "aiReviewError": ""})
    _lambda().invoke(FunctionName=os.environ["BOT_FUNCTION"], InvocationType="Event",
                     Payload=json.dumps({"sub": sub, "job": "aireview", "id": bid}))
    return {"status": "running"}


@route("GET", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})/reviewcharts")
def bot_reviewcharts(sub, claims, body, q, bid):
    import aicheck
    return aicheck.review_charts(sub, bid)


@route("GET", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})/darkpool")
def bot_darkpool(sub, claims, body, q, bid):
    import flowchart
    return flowchart.darkpool(sub, bid, int((q or {}).get("days") or 5), (q or {}).get("min"))


@route("GET", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})/flowchart")
def bot_flowchart(sub, claims, body, q, bid):
    import flowchart
    return flowchart.build(sub, bid, int((q or {}).get("days") or 5))


@route("GET", r"/bot/(?P<bid>[0-9]{14}-[a-f0-9]{6})/charts")
def bot_charts(sub, claims, body, q, bid):
    import aicheck
    return aicheck.charts(sub, bid)


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
