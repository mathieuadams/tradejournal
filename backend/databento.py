"""Databento historical API (https://hist.databento.com/v0/), with the user's own API key (KMS-encrypted).

CME Globex (GLBX.MDP3), continuous front month rolled on volume (NQ.v.0 / ES.v.0, stype_in=continuous).
Requests: HTTP basic auth (the key as user name, empty password); metadata.* as GET, timeseries.get_range as POST,
CSV with prices and times in plain form (pretty_px / pretty_ts)."""
import base64
import csv
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import db
from util import BadRequest, Unavailable, iso, now_ny

SK = "DATASOURCE#databento"
BASE = "https://hist.databento.com/v0/"
DATASET = "GLBX.MDP3"
CONT = {"NQ": "NQ.v.0", "ES": "ES.v.0"}


def _kms():
    import boto3
    return boto3.client("kms")


def _err(e):
    try:
        body = json.loads(e.read() or b"{}")
        msg = body.get("detail") or body.get("message") or body
        if isinstance(msg, dict):
            msg = msg.get("message") or json.dumps(msg)[:200]
    except Exception:
        msg = ""
    if e.code in (401, 403):
        return BadRequest(f"Databento refused the API key ({e.code}){': ' + str(msg) if msg else ''}.")
    if e.code in (400, 422):
        return BadRequest(f"Databento: {msg or 'bad request'} ({e.code}).")
    return Unavailable(f"Databento error {e.code}{': ' + str(msg) if msg else ''}.")


def _open(key, method, path, params, timeout=60):
    auth = base64.b64encode(f"{key}:".encode()).decode()
    data = None
    url = BASE + path
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params or {})
    else:
        data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": f"Basic {auth}", "Accept": "*/*",
                                                                        "User-Agent": "tradejournal-futures/1.0"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise _err(e)
    except urllib.error.URLError as e:
        raise Unavailable(f"Databento couldn't be reached: {e.reason}")


def _json(key, path, params=None):
    with _open(key, "GET", path, params) as r:
        return json.loads(r.read() or b"null")


# ---------------------------------------------------------------- key storage

def save_key(sub, key):
    key = (key or "").strip()
    if not key.startswith("db-") or len(key) < 20 or len(key) > 64:
        raise BadRequest("A Databento API key starts with db- (Databento portal → API keys).")
    ds = list_datasets(key)                            # refused keys stop here
    if DATASET not in ds:
        raise BadRequest(f"This key works but has no access to {DATASET} (CME Globex). Add it in the Databento portal.")
    blob = _kms().encrypt(KeyId=os.environ.get("KMS_KEY_ID", ""), Plaintext=key.encode())["CiphertextBlob"]
    cur = db.get(db.upk(sub), SK) or {}
    db.put({**cur, "PK": db.upk(sub), "SK": SK, "enc": base64.b64encode(blob).decode(), "last4": key[-4:], "savedAt": iso(now_ny()),
            "auto": cur.get("auto", True), "autoFp": cur.get("autoFp", True), "maxCost": cur.get("maxCost", 5.0)})
    return status(sub)


def key(sub):
    it = db.get(db.upk(sub), SK)
    if not it or not it.get("enc"):
        raise BadRequest("Connect Databento first (Data → Databento).")
    return _kms().decrypt(CiphertextBlob=base64.b64decode(it["enc"]))["Plaintext"].decode()


def status(sub):
    it = db.get(db.upk(sub), SK) or {}
    return {"connected": bool(it.get("enc")), "last4": it.get("last4"), "savedAt": it.get("savedAt"), "dataset": DATASET,
            "auto": bool(it.get("auto", True)), "autoFp": bool(it.get("autoFp", True)), "maxCost": float(it.get("maxCost", 5.0)),
            "range": it.get("range"), "testedAt": it.get("testedAt")}


def settings(sub, b):
    it = db.get(db.upk(sub), SK)
    if not it:
        raise BadRequest("Connect Databento first.")
    for k in ("auto", "autoFp"):
        if k in b:
            it[k] = bool(b[k])
    if b.get("maxCost") not in (None, ""):
        try:
            it["maxCost"] = max(0.0, min(500.0, float(b["maxCost"])))
        except (TypeError, ValueError):
            raise BadRequest("The cost limit must be a number of dollars.")
    db.put(it)
    return status(sub)


def delete_key(sub):
    db.delete(db.upk(sub), SK)
    return status(sub)


def test(sub):
    k = key(sub)
    rng = dataset_range(k)
    it = db.get(db.upk(sub), SK)
    it.update(range=rng, testedAt=iso(now_ny()))
    db.put(it)
    return status(sub)


# ---------------------------------------------------------------- metadata

def list_datasets(key):
    r = _json(key, "metadata.list_datasets")
    return r if isinstance(r, list) else []


def dataset_range(key):
    """{"start", "end"} (UTC ISO) of the data available now for GLBX.MDP3."""
    r = _json(key, "metadata.get_dataset_range", {"dataset": DATASET}) or {}
    return {"start": str(r.get("start") or r.get("start_date") or "")[:19], "end": str(r.get("end") or r.get("end_date") or "")[:19]}


def cost(key, schema, syms, start, end):
    """US dollars Databento would bill for this request (0 within a plan's included data)."""
    r = _json(key, "metadata.get_cost", {"dataset": DATASET, "symbols": ",".join(CONT[s] for s in syms), "schema": schema,
                                         "start": start, "end": end, "stype_in": "continuous"})
    try:
        return float(r)
    except (TypeError, ValueError):
        return float((r or {}).get("cost") or 0) if isinstance(r, dict) else 0.0


def rows(key, schema, sym, start, end, timeout=300):
    """CSV records (dicts) of one continuous contract between start and end (UTC ISO, end exclusive)."""
    params = {"dataset": DATASET, "symbols": CONT[sym], "schema": schema, "start": start, "end": end, "stype_in": "continuous",
              "encoding": "csv", "compression": "none", "pretty_px": "true", "pretty_ts": "true", "map_symbols": "true"}
    r = _open(key, "POST", "timeseries.get_range", params, timeout=timeout)
    try:
        for row in csv.DictReader(io.TextIOWrapper(r, encoding="utf-8", newline="")):
            yield row
    finally:
        r.close()
