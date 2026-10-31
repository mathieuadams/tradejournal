"""In-memory stand-ins for DynamoDB and KMS (saved to dev/.localdb.json), a local folder for S3, and an optional
fake Databento (synthetic NQ / ES candles and trades) for the dev server and the tests."""
import io
import json
import math
import os
import random
import threading
import urllib.parse
from datetime import datetime, timedelta

import db

PATH = os.path.join(os.path.dirname(__file__), ".localdb.json")
STORE = {}
LOCK = threading.RLock()
PERSIST = True


def _load():
    if PERSIST and os.path.exists(PATH):
        with open(PATH) as f:
            for item in json.load(f):
                STORE[(item["PK"], item["SK"])] = item


def _save():
    if PERSIST:
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


def owners():
    with LOCK:
        return sorted({p[5:] for (p, s) in STORE if p.startswith("USER#") and s == "PROFILE"})


def note_owner(sub):
    pass


class FakeKMS:
    def encrypt(self, KeyId, Plaintext):
        return {"CiphertextBlob": b"local:" + Plaintext}

    def decrypt(self, CiphertextBlob):
        return {"Plaintext": CiphertextBlob[len(b"local:"):]}


# ---------------------------------------------------------------- fake Databento

BASE_PX = {"NQ": 24800.0, "ES": 6740.0}
LAG_MIN = 15                                           # the fake's data ends this many minutes before now


def _ny_off(dt):
    import util
    return util.ny_offset(dt)


def _price(sym, dt):
    """A smooth random-walk-ish price for a UTC minute (the same for the same minute every time)."""
    k = int(dt.timestamp() // 60)
    b = BASE_PX[sym]
    return round((b * (1 + 0.06 * math.sin(k / 9000) + 0.015 * math.sin(k / 700) + 0.004 * math.sin(k / 37) + 0.0015 * math.sin(k * 1.7))) * 4) / 4


def _open_minute(dt):
    et = dt + timedelta(hours=_ny_off(dt))
    wd, hm = et.weekday(), et.strftime("%H:%M")
    if wd == 5 or (wd == 6 and hm < "18:00") or (wd == 4 and hm >= "17:00"):
        return False
    return not ("17:00" <= hm < "18:00")


def _end_avail():
    return datetime.utcnow().replace(second=0, microsecond=0) - timedelta(minutes=LAG_MIN)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def fake_open(key, method, path, params, timeout=60):
    import databento
    from util import BadRequest
    if not key.startswith("db-"):
        raise BadRequest("Databento refused the API key (401).")
    p = dict(params or {})
    if path == "metadata.list_datasets":
        return _Resp(json.dumps(["GLBX.MDP3", "XNAS.ITCH"]).encode())
    if path == "metadata.get_dataset_range":
        return _Resp(json.dumps({"start": "2017-05-21T00:00:00.000000000Z", "end": _end_avail().strftime("%Y-%m-%dT%H:%M:%S.000000000Z")}).encode())
    if path in ("metadata.get_cost", "timeseries.get_range") and p.get("end", "")[:19] > _end_avail().strftime("%Y-%m-%dT%H:%M:%S"):
        raise BadRequest("Databento: The end time must be before the available end of the dataset (422).")
    if path == "metadata.get_cost":
        s, e = datetime.fromisoformat(p["start"][:19]), datetime.fromisoformat(p["end"][:19])
        days = max(0.0, (e - s).total_seconds() / 86400)
        per = 0.002 if p["schema"].startswith("ohlcv") else 0.6
        return _Resp(json.dumps(round(days * per * len(p["symbols"].split(",")), 4)).encode())
    if path == "timeseries.get_range":
        sym = next(k for k, v in databento.CONT.items() if v == p["symbols"])
        s, e = datetime.fromisoformat(p["start"][:19]), min(datetime.fromisoformat(p["end"][:19]), _end_avail())
        out = io.StringIO()
        if p["schema"] == "ohlcv-1m":
            out.write("ts_event,rtype,publisher_id,instrument_id,open,high,low,close,volume,symbol\n")
            t = s
            while t < e:
                if _open_minute(t):
                    o, c = _price(sym, t), _price(sym, t + timedelta(minutes=1))
                    rnd = random.Random(int(t.timestamp()))
                    h, l = max(o, c) + 0.25 * rnd.randint(0, 8), min(o, c) - 0.25 * rnd.randint(0, 8)
                    out.write(f"{t.strftime('%Y-%m-%dT%H:%M:%S')}.000000000Z,33,1,42,{o:.2f},{h:.2f},{l:.2f},{c:.2f},{rnd.randint(200, 4000)},{p['symbols']}\n")
                t += timedelta(minutes=1)
        elif p["schema"] == "trades":
            out.write("ts_recv,ts_event,rtype,publisher_id,instrument_id,action,side,depth,price,size,flags,ts_in_delta,sequence,symbol\n")
            t = s
            while t < e:
                if _open_minute(t):
                    rnd = random.Random(int(t.timestamp()) + 7)
                    o, c = _price(sym, t), _price(sym, t + timedelta(minutes=1))
                    for j in range(12):
                        px = round((o + (c - o) * j / 11 + 0.25 * rnd.randint(-3, 3)) * 4) / 4
                        ts = (t + timedelta(seconds=j * 5)).strftime("%Y-%m-%dT%H:%M:%S") + ".000000000Z"
                        side = "B" if rnd.random() < (0.6 if c >= o else 0.4) else "A"
                        sz = rnd.choice([1, 1, 2, 3, 5, 8, 25, 60])
                        out.write(f"{ts},{ts},0,1,42,T,{side},0,{px:.2f},{sz},0,0,0,{p['symbols']}\n")
                t += timedelta(minutes=1)
        elif p["schema"] == "mbp-10" and p.get("encoding") == "dbn":
            import dbn
            buf = io.BytesIO()
            buf.write(dbn.header())
            t = s
            while t < e:
                if _open_minute(t):
                    rnd = random.Random(int(t.timestamp()) + 11)
                    o, c = _price(sym, t), _price(sym, t + timedelta(minutes=1))
                    for j in range(40):
                        mid = round((o + (c - o) * j / 39) * 4) / 4
                        ts = int((t - datetime(1970, 1, 1)).total_seconds() * 1e9) + j * 1_500_000_000
                        bids = [(mid - 0.25 * (k + 1), rnd.randint(2, 60) + (120 if k == 6 else 0)) for k in range(10)]
                        asks = [(mid + 0.25 * k, rnd.randint(2, 60) + (150 if k == 4 else 0)) for k in range(10)]
                        if j % 3 == 0:
                            side = "B" if rnd.random() < (0.6 if c >= o else 0.4) else "A"
                            buf.write(dbn.pack_mbp10(ts, asks[0][0] if side == "B" else bids[0][0], rnd.choice([1, 2, 3, 5, 25, 60]), "T", side, bids, asks))
                        else:
                            buf.write(dbn.pack_mbp10(ts, mid, 1, "A", "B", bids, asks))
                t += timedelta(minutes=1)
            return _Resp(buf.getvalue())
        else:
            raise BadRequest(f"Databento: schema {p['schema']} not in the fake (422).")
        return _Resp(out.getvalue().encode())
    raise BadRequest(f"unknown path {path}")


def install(fake_databento=False, data_dir=None, persist=True):
    global PERSIST
    PERSIST = persist
    _load()
    for name, fn in dict(q_prefix=q_prefix, get=get, put=put, delete=delete, owners=owners, note_owner=note_owner).items():
        setattr(db, name, fn)
    import databento
    import store
    databento._kms = lambda: FakeKMS()
    store.LOCAL = data_dir or os.path.join(os.path.dirname(__file__), ".localdata")
    if fake_databento:
        databento._open = fake_open
    _ = urllib.parse
