"""DynamoDB single-table access.

  USER#<sub> / PROFILE                     email
  USER#<sub> / DATASOURCE#databento         KMS-encrypted Databento key + download settings
  USER#<sub> / STRAT#<id>                   a strategy: rules, costs, study / validation summaries, paper state
  USER#<sub> / JOB#<name>                   background job status
  FUTDATA / COV#<SYM>                       which candles and footprints are stored (files in the data bucket)
"""
import os
from decimal import Decimal

_table = None


def table():
    global _table
    if _table is None:
        import boto3
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE"])
    return _table


def upk(sub):
    return f"USER#{sub}"


def to_ddb(v):
    if isinstance(v, bool) or v is None or isinstance(v, (str, int, Decimal)):
        return v
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return Decimal(str(round(v, 8)))
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            y = to_ddb(x)
            if y is not None:
                out[k] = y
        return out
    if isinstance(v, (list, tuple)):
        return [y for y in (to_ddb(x) for x in v) if y is not None]
    return v


def from_ddb(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: from_ddb(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [from_ddb(x) for x in v]
    return v


def q_prefix(pk, prefix, desc=False, limit=None):
    from boto3.dynamodb.conditions import Key
    kw = {                                             # an empty prefix = the whole partition (begins_with("") is refused)
        "KeyConditionExpression": (Key("PK").eq(pk) & Key("SK").begins_with(prefix)) if prefix else Key("PK").eq(pk),
        "ScanIndexForward": not desc,
    }
    items = []
    while True:
        if limit:
            kw["Limit"] = limit - len(items)
        r = table().query(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r or (limit and len(items) >= limit):
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return [from_ddb(i) for i in items]


def get(pk, sk):
    r = table().get_item(Key={"PK": pk, "SK": sk})
    return from_ddb(r["Item"]) if "Item" in r else None


def put(item):
    table().put_item(Item=to_ddb(item))


def delete(pk, sk):
    table().delete_item(Key={"PK": pk, "SK": sk})


def update(pk, sk, fields):
    names, values, sets = {}, {}, []
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"] = k
        values[f":v{i}"] = to_ddb(v)
        sets.append(f"#f{i} = :v{i}")
    table().update_item(
        Key={"PK": pk, "SK": sk},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def batch_write(puts=(), deletes=()):
    if not puts and not deletes:
        return
    with table().batch_writer(overwrite_by_pkeys=["PK", "SK"]) as b:
        for i in puts:
            b.put_item(Item=to_ddb(i))
        for pk, sk in deletes:
            b.delete_item(Key={"PK": pk, "SK": sk})


# ---------------------------------------------------------------- single owner: no table scans
# The app has one user: the scheduled jobs used to scan the whole table (~25 s with the stored market history) to
# find "every user with X". The owner's id is kept in one item instead (set by the first page request), and the
# "every user" reads are reads of that user's own items.
OWNER = ("APP", "OWNER")
_owners = None


def owners():
    global _owners
    if _owners is None:
        it = get(*OWNER)
        if it and it.get("subs"):
            _owners = list(it["subs"])
        else:                                          # never set yet: found once from the profiles, then kept
            subs = sorted({i["PK"][5:] for i in _scan_full(Attr_eq("SK", "PROFILE")) if str(i.get("PK", "")).startswith("USER#")})
            if subs:
                put({"PK": OWNER[0], "SK": OWNER[1], "subs": subs})
            _owners = subs
    return _owners


def note_owner(sub):
    """Called on each page request: the signed-in user is the owner (kept once)."""
    global _owners
    if not sub or (_owners is not None and sub in _owners):
        return
    cur = owners()
    if sub not in cur:
        _owners = sorted(set(cur) | {sub})
        put({"PK": OWNER[0], "SK": OWNER[1], "subs": _owners})


def Attr_eq(k, v):
    from boto3.dynamodb.conditions import Attr
    return Attr(k).eq(v)


def _scan_full(f):
    kw = {"FilterExpression": f}
    items = []
    while True:
        r = table().scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return [from_ddb(i) for i in items]


def scan_sk(sk_value):
    """The item `sk_value` of each user (the owner): direct reads, no table scan."""
    out = []
    for sub in owners():
        it = get(upk(sub), sk_value)
        if it:
            out.append(it)
    return out


def scan_prefix(prefix, statuses=None):
    """The items of each user (the owner) whose SK starts with prefix, optionally filtered by status: a query of
    the owner's partition, no table scan."""
    out = []
    for sub in owners():
        out += [i for i in q_prefix(upk(sub), prefix) if not statuses or i.get("status") in statuses]
    return out

def try_lock(pk, sk, seconds):
    """Take a short lock (conditional write). Returns False if someone else holds it."""
    import time as _t
    from botocore.exceptions import ClientError
    now = int(_t.time())
    try:
        table().put_item(Item={"PK": pk, "SK": sk, "until": now + seconds},
                         ConditionExpression="attribute_not_exists(PK) OR #u < :now",
                         ExpressionAttributeNames={"#u": "until"}, ExpressionAttributeValues={":now": now})
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def unlock(pk, sk):
    delete(pk, sk)


def claim(pk, sk, from_statuses, to_status, extra=None):
    """Atomically move an item's status (e.g. proposed -> submitting). False if another request got there first."""
    from botocore.exceptions import ClientError
    names, values, sets = {"#s": "status"}, {":to": to_status}, ["#s = :to"]
    for i, (k, v) in enumerate((extra or {}).items()):
        names[f"#e{i}"] = k
        values[f":e{i}"] = to_ddb(v)
        sets.append(f"#e{i} = :e{i}")
    conds = []
    for i, st in enumerate(from_statuses):
        values[f":f{i}"] = st
        conds.append(f"#s = :f{i}")
    try:
        table().update_item(Key={"PK": pk, "SK": sk}, UpdateExpression="SET " + ", ".join(sets),
                            ConditionExpression=" OR ".join(conds), ExpressionAttributeNames=names, ExpressionAttributeValues=values)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
