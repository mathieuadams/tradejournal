"""Alpaca market data (https://data.alpaca.markets/v2/), with the user's own API key ID + secret (KMS-encrypted).

Stock bars: GET /v2/stocks/{symbol}/bars, headers APCA-API-KEY-ID / APCA-API-SECRET-KEY, paged with next_page_token.
Feed: "sip" (all US exchanges; the free plan only allows data older than 15 minutes) or "iex" (free, real time, one exchange)."""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import db
from util import BadRequest, Unavailable, iso, now_ny, ny_to_utc, utc_to_ny

SK = "DATASOURCE#alpaca"
BASE = "https://data.alpaca.markets/v2/"
FEEDS = ("sip", "iex")
TFS = (1, 5, 15, 30, 60)


def _kms():
    import boto3
    return boto3.client("kms")


def _err(e):
    try:
        msg = json.loads(e.read() or b"{}").get("message") or ""
    except Exception:
        msg = ""
    if e.code in (401, 403):
        return BadRequest(f"Alpaca refused the request ({e.code}){': ' + msg if msg else ''}.")
    if e.code in (400, 422):
        return BadRequest(f"Alpaca: {msg or 'bad request'} ({e.code}).")
    if e.code == 429:
        return Unavailable("Alpaca rate limit reached. Try again in a minute.")
    return Unavailable(f"Alpaca error {e.code}{': ' + msg if msg else ''}.")


def _get(creds, path, params, timeout=60):
    url = BASE + path + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(url, headers={"APCA-API-KEY-ID": creds[0], "APCA-API-SECRET-KEY": creds[1],
                                               "Accept": "application/json", "User-Agent": "tradejournal-futures/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise _err(e)
    except urllib.error.URLError as e:
        raise Unavailable(f"Alpaca couldn't be reached: {e.reason}")


# ---------------------------------------------------------------- key storage

def save_key(sub, key_id, secret, feed="sip"):
    key_id, secret = (key_id or "").strip(), (secret or "").strip()
    if not (10 <= len(key_id) <= 64) or not (20 <= len(secret) <= 128):
        raise BadRequest("Paste the Alpaca API key ID and the secret key (Alpaca dashboard → API keys).")
    feed = feed if feed in FEEDS else "sip"
    _probe((key_id, secret), feed)                    # refused keys stop here
    blob = _kms().encrypt(KeyId=os.environ.get("KMS_KEY_ID", ""), Plaintext=json.dumps([key_id, secret]).encode())["CiphertextBlob"]
    db.put({"PK": db.upk(sub), "SK": SK, "enc": base64.b64encode(blob).decode(), "last4": key_id[-4:], "feed": feed,
            "savedAt": iso(now_ny()), "testedAt": iso(now_ny())})
    return status(sub)


def creds(sub):
    it = db.get(db.upk(sub), SK)
    if not it or not it.get("enc"):
        raise BadRequest("Connect Alpaca first (Data → Alpaca).")
    return tuple(json.loads(_kms().decrypt(CiphertextBlob=base64.b64decode(it["enc"]))["Plaintext"].decode()))


def status(sub):
    it = db.get(db.upk(sub), SK) or {}
    return {"connected": bool(it.get("enc")), "last4": it.get("last4"), "feed": it.get("feed", "sip"),
            "savedAt": it.get("savedAt"), "testedAt": it.get("testedAt")}


def settings(sub, b):
    it = db.get(db.upk(sub), SK)
    if not it:
        raise BadRequest("Connect Alpaca first.")
    if b.get("feed") in FEEDS:
        it["feed"] = b["feed"]
    db.put(it)
    return status(sub)


def delete_key(sub):
    db.delete(db.upk(sub), SK)
    return status(sub)


def test(sub):
    it = db.get(db.upk(sub), SK) or {}
    _probe(creds(sub), it.get("feed", "sip"))
    it["testedAt"] = iso(now_ny())
    db.put(it)
    return status(sub)


def _probe(c, feed):
    end = datetime.utcnow() - timedelta(minutes=20)
    _get(c, "stocks/SPY/bars", {"timeframe": "1Day", "start": (end - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 1, "feed": feed})


# ---------------------------------------------------------------- bars

def check_sym(sym):
    s = (sym or "").strip().upper()
    if not s or len(s) > 10 or not all(ch.isalnum() or ch in ".-" for ch in s):
        raise BadRequest("Enter a stock or ETF symbol, e.g. QQQ.")
    return s


def bars(sub, sym, days=10, tf=15, rth=True):
    """The last `days` sessions of `tf`-minute bars, ET times: [["YYYY-MM-DD HH:MM", o, h, l, c, v], ...]."""
    sym = check_sym(sym)
    if tf not in TFS:
        raise BadRequest(f"Aggregation must be one of {', '.join(map(str, TFS))} minutes.")
    days = max(1, min(60, int(days)))
    c = creds(sub)
    feed = status(sub)["feed"]
    end = datetime.utcnow() - timedelta(minutes=16 if feed == "sip" else 0)
    start = ny_to_utc(datetime.combine((utc_to_ny(end) - timedelta(days=days * 2 + 7)).date(), datetime.min.time()))
    out, token = [], None
    for _ in range(50):
        r = _get(c, f"stocks/{sym}/bars", {"timeframe": f"{tf if tf < 60 else 30}Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                           "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "adjustment": "raw",
                                           "feed": feed, "sort": "asc", "page_token": token})
        for b in r.get("bars") or []:
            t = utc_to_ny(datetime.strptime(b["t"][:19], "%Y-%m-%dT%H:%M:%S"))
            hm = t.strftime("%H:%M")
            if rth and not ("09:30" <= hm < "16:00"):
                continue
            out.append([t.strftime("%Y-%m-%d %H:%M"), b["o"], b["h"], b["l"], b["c"], b.get("v", 0)])
        token = r.get("next_page_token")
        if not token:
            break
    keep = sorted({b[0][:10] for b in out})[-days:]
    out = [b for b in out if b[0][:10] in keep]
    if tf == 60:                                      # Alpaca's hours start on the clock hour; TradeWellX's at 09:30
        import futdata
        out = futdata.agg(out, 60)
    return out
