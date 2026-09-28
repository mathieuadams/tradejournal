"""Unusual options activity from the Unusual Whales API (your own API key, stored encrypted with KMS).

Endpoint: GET https://api.unusualwhales.com/api/option-trades/flow-alerts  (Authorization: Bearer <key>)
Filtering (premium, calls/puts, days to expiry, ask side, sweeps) is done here on the returned alerts.
"""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import db
from util import BadRequest, Unavailable, iso, now_ny

SK = "DATASOURCE#unusualwhales"
URL = "https://api.unusualwhales.com/api/option-trades/flow-alerts"


def _kms():
    import boto3
    return boto3.client("kms")


def _get(key, params):
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params),
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise BadRequest("Unusual Whales rejected the API key (or the plan doesn't include this endpoint).")
        raise Unavailable(f"Unusual Whales returned an error ({e.code}).")
    except urllib.error.URLError as e:
        raise Unavailable(f"Unusual Whales couldn't be reached: {e.reason}")


def save_key(sub, key):
    key = (key or "").strip()
    if not key:
        db.delete(db.upk(sub), SK)
        return status(sub)
    _get(key, {"limit": 1})     # verify before saving
    blob = _kms().encrypt(KeyId=os.environ["KMS_KEY_ID"], Plaintext=key.encode())["CiphertextBlob"]
    db.put({"PK": db.upk(sub), "SK": SK, "enc": base64.b64encode(blob).decode(), "hint": key[-4:], "savedAt": iso(now_ny())})
    return status(sub)


def status(sub):
    it = db.get(db.upk(sub), SK)
    return {"connected": bool(it), "hint": (it or {}).get("hint")}


def _key(sub):
    it = db.get(db.upk(sub), SK)
    if not it:
        raise BadRequest("Add your Unusual Whales API key in Settings → Data sources to see unusual options flow.")
    return _kms().decrypt(CiphertextBlob=base64.b64decode(it["enc"]))["Plaintext"].decode()


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def alerts(sub, min_premium=100000, opt_type="call", min_dte=0, max_dte=120, ask_side=True, sweeps_only=False, ticker=None):
    data = _get(_key(sub), {"limit": 200}).get("data") or []
    today = now_ny().date()
    out = []
    for a in data:
        exp = a.get("expiry")
        try:
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        except (TypeError, ValueError):
            continue
        prem, ask = _f(a.get("total_premium")), _f(a.get("total_ask_side_prem"))
        typ = (a.get("type") or "").lower()
        if ticker and (a.get("ticker") or "").upper() != ticker.upper():
            continue
        if opt_type in ("call", "put") and typ != opt_type:
            continue
        if prem < min_premium or not (min_dte <= dte <= max_dte):
            continue
        ask_pct = ask / prem * 100 if prem else 0
        if ask_side and ask_pct < 60:
            continue
        if sweeps_only and not a.get("has_sweep"):
            continue
        out.append({"ticker": a.get("ticker"), "type": typ, "strike": _f(a.get("strike")), "expiry": exp, "dte": dte,
                    "premium": prem, "askPct": round(ask_pct), "price": _f(a.get("price")), "underlying": _f(a.get("underlying_price")),
                    "volume": a.get("volume"), "openInterest": a.get("open_interest"), "volOi": round(_f(a.get("volume_oi_ratio")), 2),
                    "sweep": bool(a.get("has_sweep")), "rule": a.get("alert_rule"), "contract": a.get("option_chain"),
                    "at": a.get("created_at")})
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return {"alerts": out, "fetched": len(data)}
