"""DynamoDB single-table access. Every item has PK = USER#<cognito sub> and a typed SK.

SK layout
  PROFILE                         settings, email
  FILL#<acct>#<ts>#<fillId>       immutable broker fills (audit source of truth)
  TRADE#<openTs>#<tradeId>        trades derived from fills (rebuilt on every import)
  JRNL#<tradeId>                  user journal: plan, setup, tags, emotion, notes
  REVIEW#<tradeId>                AI post-trade review
  DAY#<yyyy-mm-dd>                daily journal
  REPORT#<yyyy-mm-dd>             weekly AI report
  IMPORT#<importId>               CSV import status
  BROKER#alpaca                   KMS-encrypted read-only API keys
  AUDIT#<ts>#<tradeId>            before/after of every journal edit
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
    kw = {
        "KeyConditionExpression": Key("PK").eq(pk) & Key("SK").begins_with(prefix),
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


def scan_sk(sk_value):
    from boto3.dynamodb.conditions import Attr
    kw = {"FilterExpression": Attr("SK").eq(sk_value)}
    items = []
    while True:
        r = table().scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return [from_ddb(i) for i in items]


def scan_prefix(prefix, statuses=None):
    """All items (any user) whose SK starts with prefix, optionally filtered by status."""
    from boto3.dynamodb.conditions import Attr
    f = Attr("SK").begins_with(prefix)
    if statuses:
        f = f & Attr("status").is_in(list(statuses))
    kw = {"FilterExpression": f}
    items = []
    while True:
        r = table().scan(**kw)
        items += r["Items"]
        if "LastEvaluatedKey" not in r:
            break
        kw["ExclusiveStartKey"] = r["LastEvaluatedKey"]
    return [from_ddb(i) for i in items]


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
