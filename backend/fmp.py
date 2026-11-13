"""Financial Modeling Prep (https://financialmodelingprep.com/stable/), with the user's own API key (KMS-encrypted).

Used by the Earnings page:
  earnings?symbol=X                 every release: date, EPS / revenue actual and consensus estimate, report time
  sp500-constituent                 the current S&P 500
  historical-sp500-constituent      additions / removals (adds the companies that left the index: less survivorship bias)

FMP's terms: displaying or redistributing its data to other people needs FMP's data display licence; the key here is
the user's own, for their own research."""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

import db
import store
from util import BadRequest, Unavailable, iso, now_ny

SK = "DATASOURCE#fmp"
BASE = "https://financialmodelingprep.com/stable/"
_last = [0.0]


def _kms():
    import boto3
    return boto3.client("kms")


def _get(key, path, params, timeout=40):
    """GET a FMP JSON endpoint (patched in the tests / dev server)."""
    wait = 0.09 - (time.time() - _last[0])            # Premium: 750 calls / minute
    if wait > 0:
        time.sleep(wait)
    _last[0] = time.time()
    url = BASE + path + "?" + urllib.parse.urlencode({**{k: v for k, v in params.items() if v not in (None, "")}, "apikey": key})
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "tradejournal/1.0"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read() or b"[]")
            if isinstance(data, dict) and data.get("Error Message"):
                raise BadRequest("FMP: " + str(data["Error Message"])[:200])
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            try:
                msg = (json.loads(e.read() or b"{}") or {}).get("Error Message") or ""
            except Exception:
                msg = ""
            if e.code in (401, 403):
                raise BadRequest(f"FMP refused the request ({e.code}){': ' + msg[:160] if msg else ''}. Check the key and that your plan includes this data.")
            if e.code == 402:
                raise BadRequest("FMP: this endpoint isn't in your plan" + (": " + msg[:160] if msg else "."))
            raise Unavailable(f"FMP error {e.code}{': ' + msg[:160] if msg else ''}.")
        except urllib.error.URLError as e:
            raise Unavailable(f"FMP couldn't be reached: {e.reason}")
    raise Unavailable("FMP rate limit: try again in a minute.")


def save_key(sub, key):
    key = (key or "").strip()
    if not (16 <= len(key) <= 64) or not key.isalnum():
        raise BadRequest("Paste the FMP API key (financialmodelingprep.com → Dashboard).")
    _get(key, "earnings", {"symbol": "AAPL", "limit": 1})   # refused keys stop here
    blob = _kms().encrypt(KeyId=os.environ.get("KMS_KEY_ID", ""), Plaintext=key.encode())["CiphertextBlob"]
    db.put({"PK": db.upk(sub), "SK": SK, "enc": base64.b64encode(blob).decode(), "last4": key[-4:], "savedAt": iso(now_ny())})
    return probe(sub)


def _key(sub):
    it = db.get(db.upk(sub), SK)
    if not it or not it.get("enc"):
        raise BadRequest("Connect Financial Modeling Prep first (Data → Financial Modeling Prep).")
    return _kms().decrypt(CiphertextBlob=base64.b64decode(it["enc"]))["Plaintext"].decode()


def status(sub):
    it = db.get(db.upk(sub), SK) or {}
    return {"connected": bool(it.get("enc")), "last4": it.get("last4"), "savedAt": it.get("savedAt"), "plan": it.get("plan"),
            "testedAt": it.get("testedAt")}


def delete_key(sub):
    db.delete(db.upk(sub), SK)
    return status(sub)


def _sym(s):
    return s.replace(".", "-")


def _f(v):
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


TIMES = {"bmo": "bmo", "pre-market": "bmo", "premarket": "bmo", "before market open": "bmo", "amc": "amc",
         "after-hours": "amc", "after market close": "amc", "post-market": "amc", "dmh": "intraday", "during market hours": "intraday"}


def earnings(sub, sym, max_age_h=72):
    """[{date, epsAct, epsEst, revAct, revEst, timing}] oldest first, upcoming ones included (actuals None)."""
    key = "fund/fmp/" + sym
    c = store.get_json(key)
    if c and c.get("at", "") > iso(now_ny() - timedelta(hours=max_age_h)):
        return c["rows"]
    raw = _get(_key(sub), "earnings", {"symbol": _sym(sym), "limit": 1000, "includeReportTimes": "true"}) or []
    rows = []
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("date") or "")[:10]
        if len(d) != 10:
            continue
        t = str(r.get("time") or r.get("reportTime") or r.get("releaseTime") or r.get("when") or "").strip().lower()
        rows.append({"date": d, "epsAct": _f(r.get("epsActual")), "epsEst": _f(r.get("epsEstimated", r.get("epsEstimate"))),
                     "revAct": _f(r.get("revenueActual")), "revEst": _f(r.get("revenueEstimated", r.get("revenueEstimate"))),
                     "timing": TIMES.get(t)})
    rows.sort(key=lambda x: x["date"])
    store.put_json(key, {"at": iso(now_ny()), "rows": rows})
    return rows


def sp500(sub, years=10):
    """The current S&P 500, plus (when the plan allows it) the companies removed in the last `years` years."""
    k = _key(sub)
    cur = _get(k, "sp500-constituent", {}) or []
    syms = [str(r.get("symbol") or "").upper() for r in cur if r.get("symbol")]
    removed, note = [], ""
    try:
        hist = _get(k, "historical-sp500-constituent", {}) or []
        since = (now_ny().date() - timedelta(days=365 * years)).isoformat()
        for r in hist:
            s = str(r.get("removedTicker") or "").upper()
            d = str(r.get("date") or r.get("dateAdded") or "")[:10]
            if s and d >= since and s not in syms and s not in removed:
                removed.append(s)
    except Exception as e:                            # not in every plan: the current list alone
        note = f"Removed companies not available ({str(e)[:120]})."
    return {"current": syms, "removed": removed, "note": note}


# ---------------------------------------------------------------- what the key's plan includes

def probe(sub):
    """Which datasets this key's plan answers (FMP refuses what the plan doesn't include)."""
    k, out = _key(sub), {}
    for name, path, params in (("earnings", "earnings", {"symbol": "AAPL", "limit": 1}), ("sp500", "sp500-constituent", {}),
                               ("grades", "grades", {"symbol": "AAPL", "limit": 1}), ("priceTarget", "price-target-consensus", {"symbol": "AAPL"}),
                               ("transcripts", "earning-call-transcript-dates", {"symbol": "AAPL"})):
        try:
            r = _get(k, path, params)
            out[name] = bool(r)
        except Exception as e:
            out[name] = False
            out[name + "Error"] = str(e)[:160]
    it = db.get(db.upk(sub), SK) or {}
    if it:
        it["plan"], it["testedAt"] = out, iso(now_ny())
        db.put(it)
    return {**status(sub), "plan": out}


# ---------------------------------------------------------------- analysts

def grades(sub, sym, max_age_h=24 * 7):
    """Analyst rating actions, oldest first: [{date, firm, from, to, action}] (upgrade / downgrade / maintain / init …)."""
    key = "fund/grades/" + sym
    c = store.get_json(key)
    if c and c.get("at", "") > iso(now_ny() - timedelta(hours=max_age_h)):
        return c["rows"]
    raw = _get(_key(sub), "grades", {"symbol": _sym(sym), "limit": 5000}) or []
    rows = []
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("date") or "")[:10]
        if len(d) != 10:
            continue
        act = str(r.get("action") or "").lower()
        rows.append({"date": d, "firm": r.get("gradingCompany") or r.get("company"), "from": r.get("previousGrade"), "to": r.get("newGrade"),
                     "action": "up" if act.startswith("up") else "down" if act.startswith("down") else "init" if act.startswith("init") else "keep"})
    rows.sort(key=lambda x: x["date"])
    store.put_json(key, {"at": iso(now_ny()), "rows": rows})
    return rows


def price_target(sub, sym):
    """Today's consensus price target (a snapshot: used for the next release only, never in the backtest)."""
    r = _get(_key(sub), "price-target-consensus", {"symbol": _sym(sym)}) or []
    r = r[0] if isinstance(r, list) and r else r if isinstance(r, dict) else {}
    return {k: _f(r.get(k)) for k in ("targetConsensus", "targetMedian", "targetHigh", "targetLow")} if r else None


# ---------------------------------------------------------------- earnings call transcripts (Ultimate)

def transcript_dates(sub, sym):
    raw = _get(_key(sub), "earning-call-transcript-dates", {"symbol": _sym(sym)}) or []
    out = []
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("date") or "")[:10]
        q, y = r.get("quarter") or r.get("period"), r.get("fiscalYear") or r.get("year")
        try:
            q, y = int(str(q).lstrip("Qq")), int(y)
        except (TypeError, ValueError):
            continue
        if len(d) == 10:
            out.append({"date": d, "year": y, "quarter": q})
    return sorted(out, key=lambda x: x["date"])


def transcript(sub, sym, year, quarter):
    raw = _get(_key(sub), "earning-call-transcript", {"symbol": _sym(sym), "year": year, "quarter": quarter}) or []
    r = raw[0] if isinstance(raw, list) and raw else raw if isinstance(raw, dict) else {}
    return {"date": str(r.get("date") or "")[:10], "content": r.get("content") or ""} if r.get("content") else None
