"""Alpaca fill sync. Runs nightly for every connected user, or on demand with {"sub": ...}."""
import alpaca
import db
import ingest
from util import iso, now_ny


def sync_user(sub):
    pk = db.upk(sub)
    item = db.get(pk, "BROKER#alpaca")
    if not item:
        return {"status": "not_connected"}
    db.update(pk, "BROKER#alpaca", {"status": "syncing"})
    try:
        c = alpaca.creds(sub)
        fills = alpaca.fills(c, after=item.get("lastFillUtc"))
        new, dupes = ingest.save_fills(sub, [{k: v for k, v in f.items() if k != "utc"} for f in fills])
        g = ingest.regroup(sub) if new else {"trades": None}
        upd = {"status": "connected", "lastSync": iso(now_ny()), "lastResult": f"{new} new fills", "error": ""}
        if fills:
            upd["lastFillUtc"] = fills[-1]["utc"]
        db.update(pk, "BROKER#alpaca", upd)
        return {"status": "ok", "newFills": new, "trades": g["trades"]}
    except Exception as e:
        db.update(pk, "BROKER#alpaca", {"status": "error", "error": str(e)[:300], "lastSync": iso(now_ny())})
        return {"status": "error", "error": str(e)}


def handler(event, context):
    if event.get("sub"):
        return sync_user(event["sub"])
    results = {}
    for item in db.scan_sk("BROKER#alpaca"):
        results[item["PK"][5:]] = sync_user(item["PK"][5:])
    return results
