"""Market data files (S3, gzip JSON). Keys look like "NQ/1m/2026-10" and are stored as "<key>.json.gz".

LOCAL (a folder) replaces S3 for the dev server and the tests."""
import gzip
import json
import os
import time

LOCAL = None
_s3 = None
_cache = {}                 # key -> (loaded at, value): this container's copy of files that rarely change
CACHE_MAX = 64


def _client():
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client("s3")
    return _s3


def _bucket():
    return os.environ.get("DATA_BUCKET", "")


def put_json(key, obj):
    data = gzip.compress(json.dumps(obj, separators=(",", ":")).encode())
    if LOCAL:
        path = os.path.join(LOCAL, key + ".json.gz")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    else:
        _client().put_object(Bucket=_bucket(), Key=key + ".json.gz", Body=data, ContentType="application/gzip")
    _cache.pop(key, None)


def get_json(key, default=None, max_age=None):
    """The file's content, or `default` when it doesn't exist. max_age (seconds): reuse this container's copy."""
    if max_age is not None and key in _cache and time.time() - _cache[key][0] < max_age:
        return _cache[key][1]
    try:
        if LOCAL:
            with open(os.path.join(LOCAL, key + ".json.gz"), "rb") as f:
                raw = f.read()
        else:
            raw = _client().get_object(Bucket=_bucket(), Key=key + ".json.gz")["Body"].read()
    except FileNotFoundError:
        return default
    except Exception as e:
        if type(e).__name__ in ("NoSuchKey",) or "NoSuchKey" in str(e) or "404" in str(e):
            return default
        raise
    val = json.loads(gzip.decompress(raw))
    if max_age is not None:
        if len(_cache) >= CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = (time.time(), val)
    return val


def delete(key):
    if LOCAL:
        try:
            os.remove(os.path.join(LOCAL, key + ".json.gz"))
        except FileNotFoundError:
            pass
    else:
        _client().delete_object(Bucket=_bucket(), Key=key + ".json.gz")
    _cache.pop(key, None)
