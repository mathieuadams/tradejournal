"""Store fills (deduplicated, never edited) and rebuild the trade list from them."""
import db
from grouping import group_fills

ENRICH = ("maePx", "mfePx")
COMPARE = ("status", "closeTs", "qty", "closedQty", "entry", "exit", "net", "fees", "fillIds")


def fill_sk(f):
    return f"FILL#{f['acct']}#{f['ts']}#{f['id']}"


def save_fills(sub, fills):
    pk = db.upk(sub)
    existing = {i["SK"] for i in db.q_prefix(pk, "FILL#")}
    new = [f for f in fills if fill_sk(f) not in existing]
    db.batch_write(puts=[{"PK": pk, "SK": fill_sk(f), **f} for f in new])
    return len(new), len(fills) - len(new)


def regroup(sub):
    pk = db.upk(sub)
    fills = [{k: v for k, v in i.items() if k not in ("PK", "SK")} for i in db.q_prefix(pk, "FILL#")]
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
            if o["SK"] == sk and all(o.get(k) == t.get(k) for k in COMPARE):
                continue
        puts.append({"PK": pk, "SK": sk, **t})
    deletes = [(pk, o["SK"]) for o in old.values() if o["SK"] not in keep]
    db.batch_write(puts=puts, deletes=deletes)
    return {"trades": len(trades), "changed": len(puts), "removed": len(deletes),
            "unmatchedCloses": gstats.get("unmatchedCloses", 0)}
