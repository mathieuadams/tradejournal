"""In-memory stand-ins for DynamoDB, S3 presign, Lambda invoke and KMS, used by the local dev server.
Data is saved to dev/.localdb.json so it survives restarts. Delete that file to start fresh."""
import base64
import json
import os
import threading

import db

PATH = os.path.join(os.path.dirname(__file__), ".localdb.json")
STORE = {}
LOCK = threading.RLock()


def _load():
    if os.path.exists(PATH):
        with open(PATH) as f:
            for item in json.load(f):
                STORE[(item["PK"], item["SK"])] = item


def _save():
    with open(PATH, "w") as f:
        json.dump(list(STORE.values()), f)


def _norm(i):
    return db.from_ddb(db.to_ddb(dict(i)))


def q_prefix(pk, prefix, desc=False, limit=None):
    with LOCK:
        items = sorted((v for (p, s), v in STORE.items() if p == pk and s.startswith(prefix)), key=lambda i: i["SK"], reverse=desc)
        items = [_norm(i) for i in items]
    return items[:limit] if limit else items


def get(pk, sk):
    with LOCK:
        i = STORE.get((pk, sk))
        return _norm(i) if i else None


def put(item):
    with LOCK:
        STORE[(item["PK"], item["SK"])] = _norm(item)
        _save()


def delete(pk, sk):
    with LOCK:
        STORE.pop((pk, sk), None)
        _save()


def update(pk, sk, fields):
    with LOCK:
        i = STORE.setdefault((pk, sk), {"PK": pk, "SK": sk})
        i.update(_norm(fields))
        _save()


def batch_write(puts=(), deletes=()):
    with LOCK:
        for i in puts:
            STORE[(i["PK"], i["SK"])] = _norm(i)
        for pk, sk in deletes:
            STORE.pop((pk, sk), None)
        _save()


def scan_sk(v):
    with LOCK:
        return [_norm(i) for (p, s), i in STORE.items() if s == v]


class FakeKMS:
    def encrypt(self, KeyId, Plaintext):
        return {"CiphertextBlob": b"local:" + Plaintext}

    def decrypt(self, CiphertextBlob):
        return {"Plaintext": CiphertextBlob[len(b"local:"):]}


def install():
    _load()
    for name, fn in dict(q_prefix=q_prefix, get=get, put=put, delete=delete, update=update,
                         batch_write=batch_write, scan_sk=scan_sk).items():
        setattr(db, name, fn)
    import alpaca
    alpaca._kms = lambda: FakeKMS()
    _ = base64
