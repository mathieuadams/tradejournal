"""S3 trigger: parse an uploaded CSV, store new fills, rebuild trades.

Upload key layout: imports/<sub>/<importId>/<base64url account>/<file name>
"""
import base64
import urllib.parse

import db
import ingest
from parsers import ParseError, parse_csv
from util import iso, now_ny


def _s3():
    import boto3
    return boto3.client("s3")


def process(sub, import_id, account, text):
    pk = db.upk(sub)
    sk = f"IMPORT#{import_id}"
    rec = db.get(pk, sk) or {}
    db.update(pk, sk, {"status": "processing"})
    try:
        fills, skipped = parse_csv(text, account, rec.get("tz") or "auto")
    except ParseError as e:
        db.update(pk, sk, {"status": "error", "error": str(e), "finishedAt": iso(now_ny())})
        return {"status": "error", "error": str(e)}
    new, dupes = ingest.save_fills(sub, fills)
    g = ingest.regroup(sub)
    result = {"status": "done", "fills": len(fills), "newFills": new, "duplicates": dupes,
              "skippedRows": skipped, "trades": g["trades"], "unmatchedCloses": g["unmatchedCloses"],
              "finishedAt": iso(now_ny())}
    db.update(pk, sk, result)
    return result


def handler(event, context):
    for rec in event.get("Records", []):
        bucket = rec["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(rec["s3"]["object"]["key"])
        parts = key.split("/")
        if len(parts) < 5 or parts[0] != "imports":
            continue
        sub, import_id, acct_b64 = parts[1], parts[2], parts[3]
        account = base64.urlsafe_b64decode(acct_b64 + "=" * (-len(acct_b64) % 4)).decode() or "Default"
        body = _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
        if len(body) > 20 * 1024 * 1024:
            db.update(db.upk(sub), f"IMPORT#{import_id}", {"status": "error", "error": "File is larger than 20 MB."})
            continue
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("latin-1")
        try:
            process(sub, import_id, account, text)
        except Exception as e:  # keep the user informed instead of leaving the import stuck
            db.update(db.upk(sub), f"IMPORT#{import_id}", {"status": "error", "error": f"Import failed: {e}"})
            raise
