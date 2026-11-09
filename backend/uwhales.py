"""Unusual Whales API (https://api.unusualwhales.com), with the user's own API token (KMS-encrypted).

Gamma for a stock / ETF (QQQ, SPY):
  GET /api/stock/{ticker}/gex-levels?source=oi|vol      call_wall, put_wall, gamma_magnet, gamma_flip, nearby_flips
  GET /api/stock/{ticker}/spot-exposures/strike         per strike: call_gamma_oi / put_gamma_oi (and _vol, _ask, _bid),
                                                         price, time; $ gamma per 1% move of the underlying
The app also works the walls out itself from the per-strike rows (same definitions), so the numbers can later be
compared with a Schwab option chain computed the same way."""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import db
from util import BadRequest, Unavailable, iso, now_ny

SK = "DATASOURCE#uw"
BASE = "https://api.unusualwhales.com/api/"
SOURCES = ("oi", "vol")


def _kms():
    import boto3
    return boto3.client("kms")


def _err(e):
    try:
        b = json.loads(e.read() or b"{}")
        msg = b.get("message") or b.get("msg") or b.get("error") or ""
    except Exception:
        msg = ""
    if e.code in (401, 403):
        return BadRequest(f"Unusual Whales refused the token ({e.code}){': ' + msg if msg else ''}. API access is set up at unusualwhales.com/dashboard/api.")
    if e.code == 422:
        return BadRequest(f"Unusual Whales: {msg or 'bad request'} (422).")
    if e.code in (429, 503):
        return Unavailable("Unusual Whales rate limit reached. Try again in a minute.")
    return Unavailable(f"Unusual Whales error {e.code}{': ' + msg if msg else ''}.")


def _get(token, path, params=None, timeout=60):
    url = BASE + path + ("?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")}) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                                               "User-Agent": "tradejournal-futures/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise _err(e)
    except urllib.error.URLError as e:
        raise Unavailable(f"Unusual Whales couldn't be reached: {e.reason}")


# ---------------------------------------------------------------- token storage

def save_key(sub, token):
    token = (token or "").strip()
    if not (20 <= len(token) <= 200) or any(ch.isspace() for ch in token):
        raise BadRequest("Paste the Unusual Whales API token (unusualwhales.com/dashboard/api).")
    _get(token, "stock/SPY/gex-levels")               # a refused token stops here
    blob = _kms().encrypt(KeyId=os.environ.get("KMS_KEY_ID", ""), Plaintext=token.encode())["CiphertextBlob"]
    db.put({"PK": db.upk(sub), "SK": SK, "enc": base64.b64encode(blob).decode(), "last4": token[-4:],
            "savedAt": iso(now_ny()), "testedAt": iso(now_ny())})
    return status(sub)


def token(sub):
    it = db.get(db.upk(sub), SK)
    if not it or not it.get("enc"):
        raise BadRequest("Connect Unusual Whales first (Data → Unusual Whales).")
    return _kms().decrypt(CiphertextBlob=base64.b64decode(it["enc"]))["Plaintext"].decode()


def status(sub):
    it = db.get(db.upk(sub), SK) or {}
    return {"connected": bool(it.get("enc")), "last4": it.get("last4"), "savedAt": it.get("savedAt"), "testedAt": it.get("testedAt")}


def delete_key(sub):
    db.delete(db.upk(sub), SK)
    return status(sub)


def test(sub):
    it = db.get(db.upk(sub), SK) or {}
    _get(token(sub), "stock/SPY/gex-levels")
    it["testedAt"] = iso(now_ny())
    db.put(it)
    return status(sub)


# ---------------------------------------------------------------- gamma

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_sym(sym):
    s = (sym or "").strip().upper()
    if not s or len(s) > 10 or not all(ch.isalnum() or ch in ".-" for ch in s):
        raise BadRequest("Enter a stock or ETF symbol, e.g. QQQ.")
    return s


def walls(rows, price):
    """The levels from per-strike gamma, with Unusual Whales' definitions:
      call wall    = strike above the price with the largest positive net gamma (resistance)
      put wall     = strike below the price with the largest positive net gamma (support)
      gamma magnet = strike with the largest net gamma by size (the strongest pin)
      gamma flips  = where net gamma changes sign between strikes, nearest the price first (linear in between)
    rows: [{"strike", "net"}] sorted by strike."""
    above = [r for r in rows if r["strike"] > price and r["net"] > 0]
    below = [r for r in rows if r["strike"] < price and r["net"] > 0]
    out = {"call_wall": max(above, key=lambda r: r["net"])["strike"] if above else None,
           "put_wall": max(below, key=lambda r: r["net"])["strike"] if below else None,
           "gamma_magnet": max(rows, key=lambda r: abs(r["net"]))["strike"] if rows else None}
    flips = []
    for a, b in zip(rows, rows[1:]):
        if a["net"] == 0 or (a["net"] > 0) != (b["net"] > 0):
            x = a["strike"] if a["net"] == b["net"] else a["strike"] + (b["strike"] - a["strike"]) * (-a["net"]) / (b["net"] - a["net"])
            flips.append(round(x, 2))
    flips.sort(key=lambda x: abs(x - price))
    out["nearby_flips"] = flips[:5]
    out["gamma_flip"] = flips[0] if flips else None
    return out


def gamma(sub, sym, source="oi", date=None, band=0.08):
    """Today's (or `date`'s) gamma for `sym`: Unusual Whales' own levels, the per-strike profile within +/- band of the
    price, and the same levels worked out here from that profile."""
    sym, source = check_sym(sym), source if source in SOURCES else "oi"
    tok = token(sub)
    lv = _get(tok, f"stock/{sym}/gex-levels", {"source": source, "date": date})
    lv = lv.get("data", lv) if isinstance(lv, dict) else {}
    first = _get(tok, f"stock/{sym}/spot-exposures/strike", {"date": date, "limit": 1})
    price = _num(((first.get("data") or [{}])[0]).get("price"))
    if not price:
        raise Unavailable(f"Unusual Whales has no gamma data for {sym}{' on ' + date if date else ''}.")
    rows, page = [], 0
    while page < 10:
        r = _get(tok, f"stock/{sym}/spot-exposures/strike", {"date": date, "min_strike": round(price * (1 - band), 2),
                                                               "max_strike": round(price * (1 + band), 2), "limit": 500, "page": page})
        data = r.get("data") or []
        rows += data
        if len(data) < 500:
            break
        page += 1
    prof = []
    for x in rows:
        k = _num(x.get("strike"))
        c, p = _num(x.get(f"call_gamma_{source}")) or 0.0, _num(x.get(f"put_gamma_{source}")) or 0.0
        if k is not None:
            prof.append({"strike": k, "call": c, "put": p, "net": c + p})
    prof.sort(key=lambda r: r["strike"])
    uw = {k: _num(lv.get(k)) for k in ("call_wall", "put_wall", "gamma_magnet", "gamma_flip")}
    uw["nearby_flips"] = [v for v in (_num(f) for f in lv.get("nearby_flips") or []) if v is not None]
    t = ((rows or [{}])[0]).get("time") or lv.get("time")
    return {"sym": sym, "source": source, "price": price, "time": t, "date": lv.get("date"),
            "uw": uw, "computed": walls(prof, price), "strikes": prof}
