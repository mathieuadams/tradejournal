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


def alerts(sub, min_premium=100000, opt_type="call", min_dte=0, max_dte=120, ask_side=True, sweeps_only=False,
           ticker=None, min_vol_oi=0.0, days=1, since_utc=None, exclude_etfs=False):
    """Today's (or the last `days` sessions') flow alerts matching the filters.

    Filters are sent to Unusual Whales so the 200-per-page limit is spent on relevant alerts, then pages are
    walked backwards (older_than) to cover the whole session. The same filters are re-applied here in case a
    parameter isn't supported by the plan."""
    from datetime import timedelta
    from util import ny_to_utc, utc_to_ny
    key = _key(sub)
    start_ny = now_ny().replace(hour=4, minute=0, second=0, microsecond=0) - timedelta(days=max(0, days - 1))
    start_utc = since_utc or ny_to_utc(start_ny).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = {"limit": 200, "min_premium": int(min_premium), "min_dte": int(min_dte), "max_dte": int(max_dte),
            "newer_than": start_utc}
    if opt_type == "call":
        base["is_call"] = "true"; base["is_put"] = "false"
    elif opt_type == "put":
        base["is_put"] = "true"; base["is_call"] = "false"
    if ask_side:
        base["is_ask_side"] = "true"
    if sweeps_only:
        base["is_sweep"] = "true"
    if ticker:
        base["ticker_symbol"] = ticker.upper()
    if min_vol_oi:
        base["min_volume_oi_ratio"] = min_vol_oi
    raw, older, pages = [], None, 0
    for pages in range(1, 16):
        q = dict(base)
        if older:
            q["older_than"] = older
        page = _get(key, q).get("data") or []
        raw += page
        if len(page) < 200:
            break
        older = min((a.get("created_at") or "") for a in page)
        if older and older < start_utc:
            break
    today = now_ny().date()
    out, seen, stale = [], set(), 0
    for a in raw:
        created = (a.get("created_at") or "").replace(" ", "T")
        # Never trust the date filter blindly: drop anything older than the requested window.
        if not created or created[:19] < start_utc[:19]:
            stale += 1
            continue
        uid = a.get("id") or (a.get("option_chain"), a.get("created_at"))
        if uid in seen:
            continue
        seen.add(uid)
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
        if exclude_etfs:
            it = (a.get("issue_type") or "").lower()
            if "etf" in it or "fund" in it:
                continue
            if not it:
                from charts import is_etf
                if is_etf(a.get("ticker") or ""):
                    continue
        voi = _f(a.get("volume_oi_ratio"))
        if min_vol_oi and voi < min_vol_oi:
            continue
        try:
            at_et = utc_to_ny(datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            at_et = created[:16]
        out.append({"ticker": a.get("ticker"), "type": typ, "strike": _f(a.get("strike")), "expiry": exp, "dte": dte,
                    "premium": prem, "askPct": round(ask_pct), "price": _f(a.get("price")), "underlying": _f(a.get("underlying_price")),
                    "volume": a.get("volume"), "openInterest": a.get("open_interest"), "volOi": round(voi, 2),
                    "sweep": bool(a.get("has_sweep")), "rule": a.get("alert_rule"), "contract": a.get("option_chain"),
                    "at": created, "atEt": at_et})
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    tickers = {}
    for a in out:
        t = tickers.setdefault(a["ticker"], {"ticker": a["ticker"], "alerts": 0, "premium": 0.0})
        t["alerts"] += 1
        t["premium"] += a["premium"]
    top = sorted(tickers.values(), key=lambda x: -x["premium"])[:10]
    return {"alerts": out, "fetched": len(raw), "stale": stale, "pages": pages, "since": start_ny.strftime("%Y-%m-%d %H:%M"),
            "totalPremium": round(sum(a["premium"] for a in out)), "topTickers": top}
