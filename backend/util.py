"""Small shared helpers: hashing, US/Eastern time conversion, HTTP responses."""
import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal


def sha(s, n=16):
    return hashlib.sha256(s.encode()).hexdigest()[:n]


def _nth_sunday(year, month, n):
    d = datetime(year, month, 1)
    first = d + timedelta(days=(6 - d.weekday()) % 7)
    return first + timedelta(weeks=n - 1)


def ny_offset(utc_naive):
    """UTC offset in hours for America/New_York at a naive UTC datetime (no tzdata needed)."""
    y = utc_naive.year
    start = _nth_sunday(y, 3, 2).replace(hour=7)   # 2:00 EST
    end = _nth_sunday(y, 11, 1).replace(hour=6)    # 2:00 EDT
    return -4 if start <= utc_naive < end else -5


def utc_to_ny(dt):
    return dt + timedelta(hours=ny_offset(dt))


def ny_to_utc(dt):
    return dt - timedelta(hours=ny_offset(dt + timedelta(hours=5)))


def now_ny():
    return utc_to_ny(datetime.now(timezone.utc).replace(tzinfo=None))


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def parse_iso(s):
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")


def _default(o):
    if isinstance(o, Decimal):
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, set):
        return list(o)
    raise TypeError(type(o))


def resp(status, body=None):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": "" if body is None else json.dumps(body, default=_default),
    }


class BadRequest(Exception):
    status = 400


class NotFound(Exception):
    status = 404


class Unavailable(Exception):
    status = 503


def read_body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise BadRequest("Request body is not valid JSON.")
    if not isinstance(data, dict):
        raise BadRequest("Request body must be a JSON object.")
    return data
