"""Alpaca read-only integration: fills sync and intraday bars for charts / MAE-MFE.

API keys are encrypted with a KMS key before they are stored and never returned to the browser.
"""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import db
from grouping import multiplier
from util import BadRequest, Unavailable, iso, ny_to_utc, parse_iso, utc_to_ny

TRADING = {"paper": "https://paper-api.alpaca.markets", "live": "https://api.alpaca.markets"}
DATA = "https://data.alpaca.markets"
TF = {1: "1Min", 5: "5Min", 15: "15Min"}


def _kms():
    import boto3
    return boto3.client("kms")


def save_creds(sub, key_id, secret, env, feed="iex"):
    if env not in TRADING:
        raise BadRequest("Environment must be paper or live.")
    creds = {"key": key_id.strip(), "secret": secret.strip(), "env": env, "feed": feed}
    _get(TRADING[env] + "/v2/account", creds)  # fails with a clear message if the keys are wrong
    blob = _kms().encrypt(KeyId=os.environ["KMS_KEY_ID"], Plaintext=json.dumps(creds).encode())["CiphertextBlob"]
    old = db.get(db.upk(sub), "BROKER#alpaca") or {}
    db.put({"PK": db.upk(sub), "SK": "BROKER#alpaca", "enc": base64.b64encode(blob).decode(),
            "env": env, "feed": feed, "keyHint": key_id.strip()[-4:], "lastSync": old.get("lastSync"),
            "status": "connected"})


def creds(sub):
    item = db.get(db.upk(sub), "BROKER#alpaca")
    if not item:
        return None
    raw = _kms().decrypt(CiphertextBlob=base64.b64decode(item["enc"]))["Plaintext"]
    return json.loads(raw)


def _get(url, c):
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"]})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise BadRequest("Alpaca rejected these API keys. Check the key, secret and paper/live setting.")
        raise Unavailable(f"Alpaca returned an error ({e.code}).")
    except urllib.error.URLError as e:
        raise Unavailable(f"Alpaca couldn't be reached: {e.reason}")


def _utc_to_local_iso(s):
    dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    return iso(utc_to_ny(dt))


def fills(c, after=None):
    """All fills after an ISO UTC timestamp, oldest first, normalized like CSV fills."""
    out, token = [], None
    acct = "Alpaca " + c["env"]
    for _ in range(200):
        q = {"direction": "asc", "page_size": 100}
        if after:
            q["after"] = after
        if token:
            q["page_token"] = token
        page = _get(TRADING[c["env"]] + "/v2/account/activities/FILL?" + urllib.parse.urlencode(q), c)
        for a in page:
            side = "buy" if a.get("side") == "buy" else "sell"
            out.append({
                "id": "alp-" + a["id"], "acct": acct, "ts": _utc_to_local_iso(a["transaction_time"]),
                "sym": a["symbol"], "side": side, "qty": abs(float(a["qty"])), "price": float(a["price"]),
                "fees": 0.0, "mult": multiplier(a["symbol"]),
                "utc": a["transaction_time"],
            })
        if len(page) < 100:
            break
        token = page[-1]["id"]
    return out


def bars(c, sym, start_local, end_local, tf_min):
    """Bars between two US/Eastern local timestamps. Returns [{t,o,h,l,c}] with t in local time."""
    if tf_min not in TF:
        raise BadRequest("Timeframe must be 1, 5 or 15 minutes.")
    start = ny_to_utc(parse_iso(start_local)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = ny_to_utc(parse_iso(end_local)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out, token = [], None
    for _ in range(10):
        q = {"timeframe": TF[tf_min], "start": start, "end": end, "limit": 10000,
             "adjustment": "raw", "feed": c.get("feed", "iex")}
        if token:
            q["page_token"] = token
        page = _get(f"{DATA}/v2/stocks/{urllib.parse.quote(sym)}/bars?" + urllib.parse.urlencode(q), c)
        for b in page.get("bars") or []:
            out.append({"t": _utc_to_local_iso(b["t"]), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
        token = page.get("next_page_token")
        if not token:
            break
    return out


def window(trade, tf_min, pre=30, post=15):
    start = parse_iso(trade["openTs"]) - timedelta(minutes=tf_min * pre)
    end_base = parse_iso(trade["closeTs"]) if trade.get("closeTs") else parse_iso(trade["openTs"]) + timedelta(hours=2)
    return iso(start), iso(end_base + timedelta(minutes=tf_min * post))
