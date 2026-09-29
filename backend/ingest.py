"""Store fills (deduplicated, never edited) and rebuild the trade list from them."""
import db
from grouping import group_fills

ENRICH = ("maePx", "mfePx", "q")
COMPARE = ("ctx", "status", "closeTs", "qty", "closedQty", "entry", "exit", "net", "fees", "fillIds")


def fill_sk(f):
    return f"FILL#{f['acct']}#{f['ts']}#{f['id']}"


def save_fills(sub, fills):
    """Store fills that aren't already stored. Duplicates are detected by fill id (a hash of the fill's own
    content or the broker's execution id), not by storage key, so re-importing a file with a different
    time-zone choice can't create a second copy."""
    pk = db.upk(sub)
    existing = {i.get("id") for i in db.q_prefix(pk, "FILL#")}
    new, seen = [], set()
    for f in fills:
        if f["id"] in existing or f["id"] in seen:
            continue
        seen.add(f["id"])
        new.append(f)
    db.batch_write(puts=[{"PK": pk, "SK": fill_sk(f), **f} for f in new])
    return len(new), len(fills) - len(new)


def regroup(sub):
    pk = db.upk(sub)
    raw = db.q_prefix(pk, "FILL#")
    # Remove duplicate copies of the same fill (same id stored under different keys by older versions).
    by_id, dupes = {}, []
    for i in raw:
        if i.get("id") in by_id:
            dupes.append((pk, i["SK"]))
        else:
            by_id[i.get("id")] = i
    if dupes:
        db.batch_write(deletes=dupes)
    fills = [{k: v for k, v in i.items() if k not in ("PK", "SK")} for i in by_id.values()]
    gstats = {}
    trades = group_fills(fills, gstats)
    old = {i["id"]: i for i in db.q_prefix(pk, "TRADE#")}
    puts, keep = [], set()
    for t in trades:
        sk = f"TRADE#{t['openTs']}#{t['id']}"
        keep.add(sk)
        o = old.get(t["id"])
        if o:
            for f in ENRICH:
                if f in o and o.get("closeTs") == t.get("closeTs"):
                    t[f] = o[f]
            if "ctx" in o:  # chart context depends only on symbol and entry time
                t["ctx"] = o["ctx"]
            if o["SK"] == sk and all(o.get(k) == t.get(k) for k in COMPARE):
                continue
        puts.append({"PK": pk, "SK": sk, **t})
    deletes = [(pk, o["SK"]) for o in old.values() if o["SK"] not in keep]
    db.batch_write(puts=puts, deletes=deletes)
    return {"trades": len(trades), "changed": len(puts), "removed": len(deletes), "duplicateFillsRemoved": len(dupes),
            "unmatchedCloses": gstats.get("unmatchedCloses", 0)}
