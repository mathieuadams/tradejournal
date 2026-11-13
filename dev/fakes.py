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


# ---------------------------------------------------------------- fake Alpaca

ALP_PX = {"QQQ": 660.0, "SPY": 680.0}


def fake_alpaca_get(creds, path, params, timeout=60):
    """Synthetic stock bars (UTC, every minute of the day at the asked timeframe); any key ID starting with PK works."""
    from util import BadRequest
    if not str(creds[0]).startswith("PK"):
        raise BadRequest("Alpaca refused the request (401): forbidden.")
    sym = path.split("/")[1]
    tf = params.get("timeframe", "15Min")
    step = 1440 if tf == "1Day" else int(tf.replace("Min", ""))
    t0 = datetime.strptime(params["start"], "%Y-%m-%dT%H:%M:%SZ")
    t1 = datetime.strptime(params["end"], "%Y-%m-%dT%H:%M:%SZ")
    t0 = t0.replace(second=0, minute=t0.minute - t0.minute % step if step < 60 else 0)
    rnd = random.Random(sym)
    px, out, t = ALP_PX.get(sym, 100.0), [], t0
    while t < t1:
        if t.weekday() < 5:
            o = px
            px = max(1.0, px * (1 + rnd.gauss(0, 0.0015) + 0.0004 * math.sin(t.timestamp() / 9000)))
            hi, lo = max(o, px) * (1 + abs(rnd.gauss(0, 0.0005))), min(o, px) * (1 - abs(rnd.gauss(0, 0.0005)))
            out.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": round(o, 2), "h": round(hi, 2), "l": round(lo, 2),
                        "c": round(px, 2), "v": rnd.randint(1000, 90000)})
        t += timedelta(minutes=step)
    lim, at = int(params.get("limit") or 10000), int(params.get("page_token") or 0)      # paged like Alpaca
    nxt = str(at + lim) if at + lim < len(out) else None
    return {"bars": out[at:at + lim], "symbol": sym, "next_page_token": nxt}


# ---------------------------------------------------------------- fake Unusual Whales

UW_PX = {"QQQ": 656.10, "SPY": 682.40}


def _uw_profile(sym):
    px = UW_PX.get(sym, 100.0)
    try:                                   # follow the fake Alpaca price, so the dev chart and the gamma agree
        import alpaca
        from datetime import datetime, timedelta
        b = fake_alpaca_get(("PKx", "s"), f"stocks/{sym}/bars", {"timeframe": "15Min", "start": (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "end": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")})["bars"]
        px = round(b[-1]["c"], 2) if b else px
    except Exception:
        pass
    rnd = random.Random("uw" + sym)
    rows = []
    for k in range(int(px * 0.85), int(px * 1.15) + 1):
        d = (k - px) / px
        call = 4e9 * math.exp(-((d - 0.02) / 0.015) ** 2) * (1 + 0.3 * rnd.random())
        put = -2.5e9 * math.exp(-((d + 0.03) / 0.02) ** 2) * (1 + 0.3 * rnd.random()) + 1.2e9 * math.exp(-((d + 0.015) / 0.006) ** 2)
        rows.append({"strike": str(k), "price": str(px), "time": "2026-11-06T20:59:00Z",
                     "call_gamma_oi": f"{call:.2f}", "put_gamma_oi": f"{put:.2f}",
                     "call_gamma_vol": f"{call * 0.6:.2f}", "put_gamma_vol": f"{put * 0.6:.2f}"})
    return px, rows


def fake_uw_get(token, path, params=None, timeout=60):
    """Unusual Whales stand-in: any token starting with "uw_" works."""
    from util import BadRequest
    import uwhales
    if not str(token).startswith("uw_"):
        raise BadRequest("Unusual Whales refused the token (401).")
    params = params or {}
    sym = path.split("/")[1]
    px, rows = _uw_profile(sym)
    if path.startswith("option-contract/") and path.endswith("/intraday"):
        osym = path.split("/")[1]
        typ, k = osym[-9], int(osym[-8:]) / 1000
        day = params.get("date")
        rnd2 = random.Random(osym + str(day))
        out = []
        root = osym[:len(osym) - 15]                    # SPXW / NDXP: the index (SPY x 10, QQQ x 41)
        base_px = {"SPXW": UW_PX["SPY"] * 10.03, "SPX": UW_PX["SPY"] * 10.03, "NDXP": UW_PX["QQQ"] * 41.0, "NDX": UW_PX["QQQ"] * 41.0}.get(root, UW_PX.get(root, 100.0))
        for mm in range(0, 390, 3):
            from util import ny_to_utc
            t = ny_to_utc(datetime.strptime(day, "%Y-%m-%d").replace(hour=9, minute=30)) + timedelta(minutes=mm)   # 09:30 ET (DST-aware)
            under = base_px * (1 + 0.004 * math.sin(mm / 60))
            intr = max(0.0, under - k) if typ == "C" else max(0.0, k - under)
            c = round(intr + 2.0 * math.exp(-((k - under) / (base_px * 0.012)) ** 2) * (1 - mm / 500) + 0.02, 2)
            out.append({"start_time": t.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "close": str(c), "volume_bid_side": rnd2.randint(0, 40),
                        "volume_ask_side": rnd2.randint(0, 40), "volume_mid_side": 0, "volume_no_side": 0})
        return {"data": out}
    if path.endswith("/option-chains"):
        day = params.get("date")
        d0 = datetime.strptime(day, "%Y-%m-%d").date()
        exps = [d0 + timedelta(days=i) for i in (0, 1, 2, 7) if (d0 + timedelta(days=i)).weekday() < 5]
        rows = []
        for ex in exps[:2]:
            for k in range(int(px * 0.96), int(px * 1.04) + 1):
                for typ in "CP":
                    oi = int(15000 * math.exp(-((k - px * (1.01 if typ == "C" else 0.985)) / (px * 0.01)) ** 2)) + 30
                    rows.append({"option_symbol": f"{sym}{ex.strftime('%y%m%d')}{typ}{k * 1000:08d}", "strike": k, "expires": ex.isoformat(),
                                 "option_type": "call" if typ == "C" else "put", "open_interest": oi,
                                 "gamma": round(0.05 / (1 + abs(k - px) / 3), 5)})
        lim, page = int(params.get("limit") or 500), int(params.get("page") or 0)
        return {"data": rows[page * lim:(page + 1) * lim]}
    if path.endswith("/expiry-breakdown"):
        d0 = datetime.now().date()
        days = [d0 + timedelta(days=i) for i in range(0, 20) if (d0 + timedelta(days=i)).weekday() < 5][:6]
        return {"data": [{"expires": d.isoformat() + "T00:00:00.000Z", "open_interest": 100000 - i * 9000, "volume": 50000, "chains": 400} for i, d in enumerate(days)]}
    if path.endswith("/option-contracts"):
        ex = str(params.get("expiry"))[:10]
        out = []
        for k in range(int(px * 0.94), int(px * 1.06) + 1):
            for typ in "CP":
                intr = max(0.0, px - k) if typ == "C" else max(0.0, k - px)
                tv = 2.2 * math.exp(-((k - px) / (px * 0.012)) ** 2) + 0.03
                mid = intr + tv
                oi = int(20000 * math.exp(-((k - px * (1.01 if typ == "C" else 0.985)) / (px * 0.01)) ** 2)) + 50
                out.append({"option_symbol": f"{sym}{ex[2:4]}{ex[5:7]}{ex[8:10]}{typ}{k * 1000:08d}", "open_interest": oi, "volume": oi // 3,
                            "nbbo_bid": f"{max(0.01, mid - 0.03):.2f}", "nbbo_ask": f"{mid + 0.03:.2f}", "last_price": f"{mid:.2f}"})
        lim, page = int(params.get("limit") or 500), int(params.get("page") or 0)
        return {"data": out[page * lim:(page + 1) * lim]}
    if path.endswith("/oi-per-strike"):
        px0 = px
        return {"data": [{"strike": r["strike"], "call_oi": str(int(30000 * math.exp(-((float(r["strike"]) - px0 * 1.02) / (px0 * 0.02)) ** 2)) + 100),
                          "put_oi": str(int(40000 * math.exp(-((float(r["strike"]) - px0 * 0.97) / (px0 * 0.025)) ** 2)) + 100)} for r in rows]}
    if path.endswith("/gex-levels"):
        src = params.get("source") or "vol"
        prof = sorted(({"strike": float(r["strike"]), "net": float(r[f"call_gamma_{src}"]) + float(r[f"put_gamma_{src}"])} for r in rows), key=lambda r: r["strike"])
        w = uwhales.walls(prof, px)
        return {"data": {"call_wall": str(w["call_wall"]), "put_wall": str(w["put_wall"]), "gamma_magnet": str(w["gamma_magnet"]),
                         "gamma_flip": str(w["gamma_flip"]), "nearby_flips": [str(x) for x in w["nearby_flips"]],
                         "source": src, "date": "2026-11-06T00:00:00.000Z", "time": "2026-11-06T20:59:00Z"}}
    lo, hi = float(params.get("min_strike") or 0), float(params.get("max_strike") or 1e9)
    sel = [r for r in rows if lo <= float(r["strike"]) <= hi]
    lim, page = int(params.get("limit") or 500), int(params.get("page") or 0)
    return {"data": sel[page * lim:(page + 1) * lim]}


class FakeAlpacaBroker:
    """paper-api orders / account and v1beta1 option snapshots, in memory."""
    def __init__(self, spot=600.0):
        self.spot, self.orders, self.n = spot, {}, 0

    def __call__(self, creds, method, url, body=None, timeout=30):
        import alpaca
        if "paper-api.alpaca.markets" not in url and "/v1beta1/" not in url:
            raise AssertionError("not the paper endpoint: " + url)
        path = url.split("/v2/")[-1] if "/v2/" in url else url.split("/v1beta1/")[-1]
        if path == "account":
            return {"account_number": "PA1TEST", "status": "ACTIVE", "cash": "100000", "options_buying_power": "100000", "options_trading_level": 3}
        if path.startswith("options/snapshots/"):
            q = dict(urllib.parse.parse_qsl(path.split("?", 1)[1]))
            und, call = path.split("?")[0].split("/")[-1], q["type"] == "call"
            exp = q["expiration_date"][2:].replace("-", "")
            out = {}
            for k in range(int(self.spot) - 10, int(self.spot) + 11):
                m = (self.spot - k) if call else (k - self.spot)
                d = 1 / (1 + math.exp(-m / 2.5))
                px = max(0.05, round(max(0, m) + 1.2 * d * (1 - d) * 4, 2))
                out[f"{und}{exp}{'C' if call else 'P'}{k * 1000:08d}"] = {"greeks": {"delta": d if call else -d}, "latestQuote": {"bp": px - 0.02, "ap": px + 0.02}}
            return {"snapshots": out}
        if path == "orders" and method == "POST":
            self.n += 1
            oid = f"o{self.n}"
            k = int(body["symbol"][-8:]) / 1000
            call = body["symbol"][-9] == "C"
            m = (self.spot - k) if call else (k - self.spot)
            px = round(max(0.05, max(0, m) + 1.0), 2)
            self.orders[oid] = {"id": oid, "status": "filled", "filled_avg_price": str(px), "filled_at": "2026-11-12T15:00:00Z", **body}
            return {"id": oid, "status": "accepted"}
        if path.startswith("orders/"):
            oid = path.split("/")[1]
            if method == "DELETE":
                self.orders[oid]["status"] = "canceled"
                return {}
            return self.orders[oid]
        raise AssertionError("unexpected " + method + " " + url)


# ---------------------------------------------------------------- fake SEC EDGAR (Earnings page)

SEC_COS = {"AAA": (1001, "Alpha Growth Inc.", 400e6, 0.25, 0.80), "BBB": (1002, "Beta Steady Corp.", 2e9, 0.06, 1.50),
           "CCC": (1003, "Gamma Turnaround Co.", 150e6, 0.15, -0.30)}


def _sec_facts(sym):
    from datetime import date as _dt
    cik, name, rev0, g, eps0 = SEC_COS[sym]
    rnd = random.Random(sym)
    rev, eps, ni, cfo, assets = [], [], [], [], []
    y0 = _dt.today().year - 6
    qrev, qeps = rev0 / 4, eps0 / 4
    for y in range(y0, _dt.today().year):
        ytd = {"rev": 0, "eps": 0, "ni": 0, "cfo": 0}
        starts = [_dt(y, 1, 1), _dt(y, 4, 1), _dt(y, 7, 1), _dt(y, 10, 1)]
        ends = [_dt(y, 3, 31), _dt(y, 6, 30), _dt(y, 9, 30), _dt(y, 12, 31)]
        for k in range(4):
            qrev *= 1 + g / 4 + rnd.gauss(0, 0.01) + (0.01 if sym == "AAA" and y >= y0 + 4 else 0)
            qeps = qeps + abs(eps0) * 0.04 + rnd.gauss(0, 0.01) if sym == "CCC" else qeps * (1 + g / 4 + rnd.gauss(0, 0.02))
            qni = qeps * 100e6
            qcfo = qni * (1.1 if sym != "CCC" else 0.5) + rnd.gauss(0, 1e6)
            ytd = {"rev": ytd["rev"] + qrev, "eps": ytd["eps"] + qeps, "ni": ytd["ni"] + qni, "cfo": ytd["cfo"] + qcfo}
            filed = (ends[k] + timedelta(days=35)).isoformat()
            form = "10-K" if k == 3 else "10-Q"
            s0, e0 = starts[k].isoformat(), ends[k].isoformat()
            fy0 = starts[0].isoformat()
            if k < 3:                                  # 10-Q: the quarter and the year to date; 10-K: the year only
                rev.append({"start": s0, "end": e0, "val": round(qrev), "form": form, "filed": filed})
                eps.append({"start": s0, "end": e0, "val": round(qeps, 2), "form": form, "filed": filed})
                ni.append({"start": s0, "end": e0, "val": round(qni), "form": form, "filed": filed})
            for arr, kk in ((rev, "rev"), (ni, "ni"), (cfo, "cfo")):
                if k > 0 or kk == "cfo":
                    arr.append({"start": fy0, "end": e0, "val": round(ytd[kk]), "form": form, "filed": filed})
            if k == 3:
                eps.append({"start": fy0, "end": e0, "val": round(ytd["eps"], 2), "form": form, "filed": filed})
            assets.append({"end": e0, "val": round(qrev * 6), "form": form, "filed": filed})
    U = lambda rows, u="USD": {"units": {u: rows}}
    return {"cik": cik, "entityName": name, "facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": U(rev), "EarningsPerShareDiluted": U(eps, "USD/shares"),
        "NetIncomeLoss": U(ni), "NetCashProvidedByUsedInOperatingActivities": U(cfo), "Assets": U(assets)}}}


def _sec_subs(sym):
    from datetime import date as _dt
    cik, name, *_ = SEC_COS[sym]
    rec = {k: [] for k in ("accessionNumber", "filingDate", "acceptanceDateTime", "form", "items")}
    y0 = _dt.today().year - 6
    n = 0
    for y in range(y0, _dt.today().year + 1):
        for m in (1, 4, 7, 10):
            d = _dt(y, m, 28)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            if d >= _dt.today():
                continue
            n += 1
            t = "21:05:12" if (n + len(sym)) % 2 else "12:01:44"
            for form, items in (("8-K", "2.02,9.01"), ("4", "")):
                rec["accessionNumber"].append(f"0000{cik}-{y % 100:02d}-{n:06d}")
                rec["filingDate"].append(d.isoformat())
                rec["acceptanceDateTime"].append(f"{d.isoformat()}T{t}.000Z")
                rec["form"].append(form)
                rec["items"].append(items)
    return {"cik": str(cik), "name": name, "fiscalYearEnd": "1231", "filings": {"recent": rec, "files": []}}


def fake_sec_http(url, ua, timeout=40):
    if "company_tickers" in url:
        return {str(i): {"cik_str": v[0], "ticker": k, "title": v[1]} for i, (k, v) in enumerate(SEC_COS.items())}
    cik = int(url.rsplit("CIK", 1)[1][:10]) if "CIK" in url else None
    sym = next((k for k, v in SEC_COS.items() if v[0] == cik), None)
    if not sym:
        return None
    return _sec_facts(sym) if "companyfacts" in url else _sec_subs(sym)


# ---------------------------------------------------------------- fake Financial Modeling Prep (Earnings page)

def fake_fmp_get(key, path, params, timeout=40):
    from datetime import date as _dt
    if path == "sp500-constituent":
        return [{"symbol": k, "name": v[1]} for k, v in SEC_COS.items()]
    if path == "historical-sp500-constituent":
        return [{"date": (_dt.today() - timedelta(days=400)).isoformat(), "removedTicker": "DDD", "symbol": "XYZ"}]
    sym = params.get("symbol")
    if sym == "AAPL":                                  # the plan probe asks about AAPL
        sym = "AAA"
    if path == "grades":
        if sym not in SEC_COS:
            return []
        rnd = random.Random("gr" + sym)
        out = []
        for k in range(120):
            d = _dt.today() - timedelta(days=k * 17 + rnd.randint(0, 9))
            a = rnd.choice(["upgrade", "downgrade", "maintain", "maintain"])
            out.append({"symbol": sym, "date": d.isoformat(), "gradingCompany": rnd.choice(["Firm A", "Firm B", "Firm C"]), "previousGrade": "Hold",
                        "newGrade": {"upgrade": "Buy", "downgrade": "Sell"}.get(a, "Hold"), "action": a})
        return out
    if path == "price-target-consensus":
        return [{"symbol": sym, "targetHigh": 150, "targetLow": 90, "targetConsensus": 125, "targetMedian": 124}] if sym in SEC_COS else []
    if path in ("earning-call-transcript-dates", "earning-call-transcript"):
        if sym not in SEC_COS:
            return []
        dates = []
        for y in range(_dt.today().year - 6, _dt.today().year + 1):
            for qn, m in enumerate((1, 4, 7, 10), start=1):
                d = _dt(y, m, 28)
                while d.weekday() >= 5:
                    d += timedelta(days=1)
                if d < _dt.today():
                    dates.append({"quarter": qn, "fiscalYear": y, "date": d.isoformat()})
        if path == "earning-call-transcript-dates":
            return dates[::-1]
        d = next((x for x in dates if x["fiscalYear"] == int(params["year"]) and x["quarter"] == int(params["quarter"])), None)
        if not d:
            return []
        rnd = random.Random(sym + d["date"])
        mood = rnd.choice(["strong", "weak", "steady"])
        g = {"strong": "We are raising our full-year guidance and now expect revenue at the upper end of the range.",
             "weak": "Given softer demand we are lowering our outlook for the full year and expect headwinds to persist.",
             "steady": "We reaffirm our full-year guidance."}[mood]
        body = {"strong": "Demand was strong and we delivered record growth with robust margins and great momentum. ",
                "weak": "Results were weak, with declining orders, pricing pressure and a difficult environment. ",
                "steady": "The quarter was solid, and results were consistent with expectations. "}[mood]
        content = ("Operator: Good afternoon and welcome.\nCEO: " + body * 3 + g + "\nCFO: " + body + g +
                   "\nOperator: We will now begin the question-and-answer session. Our first question comes from Analyst.\n"
                   "Analyst: Could you discuss the uncertainty around margins?\nCEO: It may vary, but we believe trends are " + ("good." if mood != "weak" else "challenging and uncertain."))
        return [{"symbol": sym, "period": "Q%d" % d["quarter"], "year": d["fiscalYear"], "date": d["date"], "content": content}]
    if path != "earnings":
        return []
    if sym not in SEC_COS:
        return []
    rnd = random.Random("fmp" + sym)
    out, n = [], 0
    for y in range(_dt.today().year - 6, _dt.today().year + 2):
        for m in (1, 4, 7, 10):
            d = _dt(y, m, 28)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            n += 1
            est = round(0.5 + 0.02 * n, 2)
            past = d < _dt.today()
            if not past and d > _dt.today() + timedelta(days=120):
                continue
            out.append({"symbol": sym, "date": d.isoformat(), "epsEstimated": est, "epsActual": round(est * (1 + rnd.gauss(0.03, 0.06)), 2) if past else None,
                        "revenueEstimated": 1e9 + n * 1e7, "revenueActual": round((1e9 + n * 1e7) * (1 + rnd.gauss(0.01, 0.02))) if past else None,
                        "time": "amc" if (n + len(sym)) % 2 else "bmo"})
    return out[::-1]


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
    import alpaca
    import uwhales
    alpaca._kms = lambda: FakeKMS()
    uwhales._kms = lambda: FakeKMS()
    import fmp
    fmp._kms = lambda: FakeKMS()
    if fake_databento:
        databento._open = fake_open
        alpaca._get = fake_alpaca_get
        uwhales._get = fake_uw_get
        import fundamentals
        fundamentals._http = fake_sec_http
        import fmp
        fmp._get = fake_fmp_get
    _ = urllib.parse
