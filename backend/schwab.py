"""Charles Schwab Trader API: OAuth connection and trade sync.

Flow
  1. The app (one Schwab developer app for the whole site) sends the user to Schwab's login page.
  2. Schwab redirects back to https://<site>/schwab?code=...&state=...; the web app posts the code here.
  3. We exchange it for an access token (30 min) and refresh token (7 days), encrypt both with KMS,
     and sync TRADE transactions for every linked account.
Schwab requires logging in again every 7 days; the settings page shows when.
"""
import base64
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import db
from grouping import multiplier
from util import BadRequest, Unavailable, iso, now_ny, utc_to_ny

AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"
TRADER = "https://api.schwabapi.com/trader/v1"
SK = "BROKER#schwab"
_app = None


class Expired(Exception):
    pass


def app_config():
    """App key/secret for the site's Schwab developer app (Secrets Manager)."""
    global _app
    if _app is None:
        arn = os.environ.get("SCHWAB_SECRET_ARN")
        if not arn:
            _app = {}
        else:
            import boto3
            raw = boto3.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"]
            try:
                _app = json.loads(raw)
            except ValueError:
                _app = {}
    return _app if _app.get("appKey") and _app.get("appSecret") else None


def callback_url():
    return os.environ.get("SCHWAB_CALLBACK", "")


def _kms():
    import boto3
    return boto3.client("kms")


def _encrypt(obj):
    blob = _kms().encrypt(KeyId=os.environ["KMS_KEY_ID"], Plaintext=json.dumps(obj).encode())["CiphertextBlob"]
    return base64.b64encode(blob).decode()


def _decrypt(s):
    return json.loads(_kms().decrypt(CiphertextBlob=base64.b64decode(s))["Plaintext"])


def _http(method, url, headers=None, data=None):
    req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        if e.code in (400, 401) and "oauth/token" in url:
            raise Expired(f"Schwab login expired or was refused: {detail}")
        if e.code == 401:
            raise Expired("Schwab rejected the access token.")
        raise Unavailable(f"Schwab returned an error ({e.code}): {detail}")
    except urllib.error.URLError as e:
        raise Unavailable(f"Schwab couldn't be reached: {e.reason}")


def _token_request(form):
    app = app_config()
    basic = base64.b64encode(f"{app['appKey']}:{app['appSecret']}".encode()).decode()
    return _http("POST", TOKEN_URL, {"Authorization": f"Basic {basic}",
                                     "Content-Type": "application/x-www-form-urlencoded"},
                 urllib.parse.urlencode(form).encode())


# ---------- connect ----------

def authorize_url(sub, journal_account):
    app = app_config()
    if not app:
        raise BadRequest("The Schwab connection isn't set up on the server yet (app key and secret missing).")
    state = secrets.token_urlsafe(24)
    db.put({"PK": db.upk(sub), "SK": f"SCHWABSTATE#{state}", "journalAccount": journal_account,
            "createdAt": iso(now_ny())})
    q = urllib.parse.urlencode({"client_id": app["appKey"], "redirect_uri": callback_url(), "state": state})
    return f"{AUTH_URL}?{q}"


def complete(sub, code, state):
    pk = db.upk(sub)
    st = db.get(pk, f"SCHWABSTATE#{state}") if state else None
    if not st:
        raise BadRequest("This Schwab login link expired or wasn't started here. Click Connect Schwab again.")
    db.delete(pk, f"SCHWABSTATE#{state}")
    if datetime.strptime(st["createdAt"], "%Y-%m-%dT%H:%M:%S") < now_ny() - timedelta(minutes=15):
        raise BadRequest("The Schwab login took too long. Click Connect Schwab again.")
    try:
        tok = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": callback_url()})
    except Expired as e:
        raise BadRequest(f"Schwab didn't accept the login: {e}")
    now = now_ny()
    tokens = {"access": tok["access_token"], "refresh": tok["refresh_token"],
              "accessExp": iso(now + timedelta(seconds=int(tok.get("expires_in", 1800)) - 60)),
              "refreshExp": iso(now + timedelta(days=7) - timedelta(minutes=10))}
    nums = _http("GET", f"{TRADER}/accounts/accountNumbers", {"Authorization": f"Bearer {tokens['access']}",
                                                              "Accept": "application/json"}) or []
    name = st.get("journalAccount") or "Schwab"
    accounts = [{"hash": a["hashValue"], "last4": str(a["accountNumber"])[-4:],
                 "name": name if len(nums) == 1 else f"{name} ••{str(a['accountNumber'])[-4:]}"} for a in nums]
    old = db.get(pk, SK) or {}
    db.put({"PK": pk, "SK": SK, "enc": _encrypt(tokens), "accounts": accounts, "status": "connected",
            "refreshExpires": tokens["refreshExp"], "connectedAt": iso(now), "journalAccount": name,
            "lastSync": old.get("lastSync"), "syncFrom": old.get("syncFrom") or {}, "error": ""})
    return status(sub)


def status(sub):
    b = db.get(db.upk(sub), SK)
    out = {"configured": bool(app_config()), "callback": callback_url(), "connected": bool(b)}
    if b:
        out.update({k: b.get(k) for k in ("status", "lastSync", "lastResult", "error", "refreshExpires", "journalAccount")})
        out["accounts"] = [{"last4": a["last4"], "name": a["name"]} for a in b.get("accounts") or []]
    return out


def disconnect(sub):
    db.delete(db.upk(sub), SK)
    return status(sub)


# ---------- tokens ----------

def _access_token(sub, item):
    tokens = _decrypt(item["enc"])
    now = now_ny()
    if datetime.strptime(tokens["refreshExp"], "%Y-%m-%dT%H:%M:%S") <= now:
        raise Expired("The Schwab login is more than 7 days old. Reconnect Schwab in Settings.")
    if datetime.strptime(tokens["accessExp"], "%Y-%m-%dT%H:%M:%S") > now:
        return tokens["access"]
    tok = _token_request({"grant_type": "refresh_token", "refresh_token": tokens["refresh"]})
    tokens["access"] = tok["access_token"]
    tokens["accessExp"] = iso(now + timedelta(seconds=int(tok.get("expires_in", 1800)) - 60))
    if tok.get("refresh_token") and tok["refresh_token"] != tokens["refresh"]:
        tokens["refresh"] = tok["refresh_token"]
    db.update(item["PK"], SK, {"enc": _encrypt(tokens)})
    return tokens["access"]


# ---------- transactions -> fills ----------

ASSETS = {"EQUITY", "OPTION", "COLLECTIVE_INVESTMENT", "ETF", "FUTURE", "INDEX"}


def _utc_iso_to_ny(s):
    return iso(utc_to_ny(datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")))


def to_fills(transactions, account_name):
    """Map Schwab TRADE transactions to normalized fills (one fill per traded instrument leg)."""
    fills = []
    for tx in transactions:
        if tx.get("type") != "TRADE" or tx.get("status", "VALID") not in ("VALID", None):
            continue
        items = tx.get("transferItems") or []
        legs = [it for it in items if (it.get("instrument") or {}).get("assetType") in ASSETS and not it.get("feeType")]
        fees = sum(abs(it.get("cost") or 0) for it in items if it.get("feeType"))
        total_qty = sum(abs(it.get("amount") or 0) for it in legs) or 1
        ts = _utc_iso_to_ny(tx["time"])
        for i, it in enumerate(legs):
            ins = it["instrument"]
            qty = it.get("amount") or 0
            if not qty or it.get("price") is None:
                continue
            sym = (ins.get("symbol") or "").replace(" ", "").upper()
            effect = {"OPENING": "open", "CLOSING": "close"}.get(it.get("positionEffect"), "")
            fills.append({
                "id": f"sch-{tx['activityId']}-{i}", "acct": account_name, "ts": ts, "sym": sym,
                "side": "buy" if qty > 0 else "sell", "qty": abs(float(qty)), "price": float(it["price"]),
                "fees": round(fees * abs(qty) / total_qty, 4), "mult": multiplier(sym), "effect": effect,
            })
    fills.sort(key=lambda f: (f["ts"], f["id"]))
    return fills


def transactions(access, account_hash, start_ny, end_ny):
    """TRADE transactions between two US/Eastern datetimes, fetched in 60-day windows (API limit 1 year / 3000 rows)."""
    from util import ny_to_utc
    out, cur = [], start_ny
    while cur < end_ny:
        nxt = min(cur + timedelta(days=60), end_ny)
        q = urllib.parse.urlencode({
            "startDate": ny_to_utc(cur).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "endDate": ny_to_utc(nxt).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "types": "TRADE"})
        page = _http("GET", f"{TRADER}/accounts/{account_hash}/transactions?{q}",
                     {"Authorization": f"Bearer {access}", "Accept": "application/json"}) or []
        out += page
        cur = nxt
    return out


def sync_user(sub):
    import ingest
    pk = db.upk(sub)
    item = db.get(pk, SK)
    if not item:
        return {"status": "not_connected"}
    db.update(pk, SK, {"status": "syncing"})
    try:
        access = _access_token(sub, item)
        now = now_ny()
        sync_from = dict(item.get("syncFrom") or {})
        total_new = 0
        for a in item.get("accounts") or []:
            start = sync_from.get(a["hash"])
            if start:
                start_dt = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S") - timedelta(days=3)
            else:
                # First sync: pick up right after the last fill already in this journal account
                # (e.g. from CSV statements) so nothing is counted twice; otherwise go back a year.
                prev = [f["ts"] for f in db.q_prefix(pk, f"FILL#{a['name']}#")]
                start_dt = (datetime.strptime(max(prev), "%Y-%m-%dT%H:%M:%S") + timedelta(seconds=1)) if prev \
                    else now - timedelta(days=364)
            txs = transactions(access, a["hash"], start_dt, now + timedelta(hours=1))
            fills = [f for f in to_fills(txs, a["name"]) if f["ts"] >= iso(start_dt)] if not start else to_fills(txs, a["name"])
            new, _ = ingest.save_fills(sub, fills)
            total_new += new
            sync_from[a["hash"]] = iso(now)
        if total_new:
            ingest.regroup(sub)
        db.update(pk, SK, {"status": "connected", "lastSync": iso(now), "syncFrom": sync_from,
                           "lastResult": f"{total_new} new fills", "error": ""})
        return {"status": "ok", "newFills": total_new}
    except Expired as e:
        db.update(pk, SK, {"status": "expired", "error": str(e), "lastSync": iso(now_ny())})
        return {"status": "expired"}
    except Exception as e:
        db.update(pk, SK, {"status": "error", "error": str(e)[:300], "lastSync": iso(now_ny())})
        return {"status": "error", "error": str(e)}


def positions(sub):
    """Current positions from Schwab (None when not connected or the login expired)."""
    item = db.get(db.upk(sub), SK)
    if not item or not app_config():
        return None
    try:
        access = _access_token(sub, item)
    except Exception:
        return None
    out = []
    for a in item.get("accounts") or []:
        try:
            acct = _http("GET", f"{TRADER}/accounts/{a['hash']}?fields=positions",
                         {"Authorization": f"Bearer {access}", "Accept": "application/json"}) or {}
        except Exception:
            continue
        sa = acct.get("securitiesAccount") or {}
        for p in sa.get("positions") or []:
            ins = p.get("instrument") or {}
            qty = (p.get("longQuantity") or 0) - (p.get("shortQuantity") or 0)
            if not qty:
                continue
            out.append({"acct": a["name"], "sym": (ins.get("symbol") or "").replace(" ", "").upper(),
                        "assetType": ins.get("assetType"), "qty": qty, "avgPrice": p.get("averagePrice"),
                        "marketValue": p.get("marketValue"), "dayPL": p.get("currentDayProfitLoss"),
                        "openPL": p.get("longOpenProfitLoss") if qty > 0 else p.get("shortOpenProfitLoss")})
        bal = sa.get("currentBalances") or {}
        if bal:
            out.append({"acct": a["name"], "balances": {k: bal.get(k) for k in ("liquidationValue", "cashBalance", "buyingPower", "equity") if k in bal}})
    return out
