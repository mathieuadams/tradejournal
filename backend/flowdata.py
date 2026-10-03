"""Unusual options activity from the Unusual Whales API (your own API key, stored encrypted with KMS).

Endpoint: GET https://api.unusualwhales.com/api/option-trades/flow-alerts  (Authorization: Bearer <key>)
Filtering (premium, calls/puts, days to expiry, ask side, sweeps) is done here on the returned alerts.
"""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import db
from util import BadRequest, Unavailable, iso, now_ny

SK = "DATASOURCE#unusualwhales"
URL = "https://api.unusualwhales.com/api/option-trades/flow-alerts"


def _kms():
    import boto3
    return boto3.client("kms")


def _get(key, params):
    req = urllib.request.Request(URL + "?" + urllib.parse.urlencode(params),
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise BadRequest("Unusual Whales rejected the API key (or the plan doesn't include this endpoint).")
        raise Unavailable(f"Unusual Whales returned an error ({e.code}).")
    except urllib.error.URLError as e:
        raise Unavailable(f"Unusual Whales couldn't be reached: {e.reason}")


def save_key(sub, key):
    key = (key or "").strip()
    if not key:
        db.delete(db.upk(sub), SK)
        return status(sub)
    _get(key, {"limit": 1})     # verify before saving
    blob = _kms().encrypt(KeyId=os.environ["KMS_KEY_ID"], Plaintext=key.encode())["CiphertextBlob"]
    db.put({"PK": db.upk(sub), "SK": SK, "enc": base64.b64encode(blob).decode(), "hint": key[-4:], "savedAt": iso(now_ny())})
    return status(sub)


def status(sub):
    it = db.get(db.upk(sub), SK)
    return {"connected": bool(it), "hint": (it or {}).get("hint")}


def _key(sub):
    it = db.get(db.upk(sub), SK)
    if not it:
        raise BadRequest("Add your Unusual Whales API key in Settings → Data sources to see unusual options flow.")
    return _kms().decrypt(CiphertextBlob=base64.b64decode(it["enc"]))["Plaintext"].decode()


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def alerts(sub, min_premium=100000, opt_type="call", min_dte=0, max_dte=120, ask_side=True, sweeps_only=False,
           ticker=None, min_vol_oi=0.0, days=1, since_utc=None, exclude_etfs=False, period=None, date_from=None,
           date_to=None, tz="America/Los_Angeles"):
    """Today's (or the last `days` sessions') flow alerts matching the filters.

    Filters are sent to Unusual Whales so the 200-per-page limit is spent on relevant alerts, then pages are
    walked backwards (older_than) to cover the whole session. The same filters are re-applied here in case a
    parameter isn't supported by the plan."""
    from datetime import timedelta
    from util import ny_to_utc, utc_to_ny
    key = _key(sub)
    start_ny = now_ny().replace(hour=4, minute=0, second=0, microsecond=0) - timedelta(days=max(0, days - 1))
    start_utc = since_utc or ny_to_utc(start_ny).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = "9999"
    label = start_ny.strftime("%Y-%m-%d %H:%M") + " ET"
    if period and not since_utc:
        from zoneinfo import ZoneInfo
        from datetime import timezone
        z = ZoneInfo(tz or "America/Los_Angeles")
        today_local = datetime.now(z).date()
        if period == "today":
            d0 = d1 = today_local
        elif period == "yesterday":
            d0 = d1 = today_local - timedelta(days=1)
        elif period == "7d":
            d0, d1 = today_local - timedelta(days=6), today_local
        else:
            try:
                d0 = datetime.strptime(date_from, "%Y-%m-%d").date()
                d1 = datetime.strptime(date_to or date_from, "%Y-%m-%d").date()
            except (TypeError, ValueError):
                raise BadRequest("Choose a start and end date.")
            if d1 < d0:
                d0, d1 = d1, d0
            if (d1 - d0).days > 31:
                raise BadRequest("Choose a range of 31 days or less.")
        s_loc = datetime(d0.year, d0.month, d0.day, 0, 0, 0, tzinfo=z)
        e_loc = datetime(d1.year, d1.month, d1.day, 23, 59, 59, tzinfo=z)
        start_utc = s_loc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc = e_loc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tzname = s_loc.strftime("%Z")
        label = f"{d0.isoformat()} 00:00 to {d1.isoformat()} 23:59 {tzname}"
    # No date parameter: its format isn't reliable, so we page back from the newest alerts ourselves and
    # stop once we pass the start of the window.
    base = {"limit": 200, "min_premium": int(min_premium), "min_dte": int(min_dte), "max_dte": int(max_dte)}
    if opt_type == "call":
        base["is_call"] = "true"; base["is_put"] = "false"
    elif opt_type == "put":
        base["is_put"] = "true"; base["is_call"] = "false"
    if ask_side:
        base["is_ask_side"] = "true"
    if sweeps_only:
        base["is_sweep"] = "true"
    if ticker:
        base["ticker_symbol"] = ticker.upper()
    if min_vol_oi:
        base["min_volume_oi_ratio"] = min_vol_oi
    raw, older, pages = [], None, 0
    for pages in range(1, 41):
        q = dict(base)
        if older:
            q["older_than"] = older
        page = _get(key, q).get("data") or []
        raw += page
        if len(page) < 200:
            break
        oldest = min((a.get("created_at") or "") for a in page)
        if not oldest or oldest[:19] < start_utc[:19] or oldest == older:   # passed the window, or paging not moving
            break
        older = oldest
    today = now_ny().date()
    out, seen, stale = [], set(), 0
    for a in raw:
        created = (a.get("created_at") or "").replace(" ", "T")
        # Never trust the date filter blindly: drop anything older than the requested window.
        if not created or created[:19] < start_utc[:19] or created[:19] > end_utc[:19]:
            stale += 1
            continue
        uid = a.get("id") or (a.get("option_chain"), a.get("created_at"))
        if uid in seen:
            continue
        seen.add(uid)
        exp = a.get("expiry")
        try:
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        except (TypeError, ValueError):
            continue
        prem, ask = _f(a.get("total_premium")), _f(a.get("total_ask_side_prem"))
        typ = (a.get("type") or "").lower()
        if ticker and (a.get("ticker") or "").upper() != ticker.upper():
            continue
        if opt_type in ("call", "put") and typ != opt_type:
            continue
        if prem < min_premium or not (min_dte <= dte <= max_dte):
            continue
        ask_pct = ask / prem * 100 if prem else 0
        if ask_side and ask_pct < 60:
            continue
        if sweeps_only and not a.get("has_sweep"):
            continue
        if exclude_etfs:
            it = (a.get("issue_type") or "").lower()
            if "etf" in it or "fund" in it:
                continue
            if not it:
                from charts import is_etf
                if is_etf(a.get("ticker") or ""):
                    continue
        voi = _f(a.get("volume_oi_ratio"))
        if min_vol_oi and voi < min_vol_oi:
            continue
        try:
            at_et = utc_to_ny(datetime.strptime(created[:19], "%Y-%m-%dT%H:%M:%S")).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            at_et = created[:16]
        out.append({"ticker": a.get("ticker"), "type": typ, "strike": _f(a.get("strike")), "expiry": exp, "dte": dte,
                    "premium": prem, "askPct": round(ask_pct), "price": _f(a.get("price")), "underlying": _f(a.get("underlying_price")),
                    "volume": a.get("volume"), "openInterest": a.get("open_interest"), "volOi": round(voi, 2),
                    "sweep": bool(a.get("has_sweep")), "rule": a.get("alert_rule"), "contract": a.get("option_chain"),
                    "at": created, "atEt": at_et})
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    try:
        live_prices(sub, out)
    except Exception as e:
        print("live prices skipped", e)
    tickers = {}
    for a in out:
        t = tickers.setdefault(a["ticker"], {"ticker": a["ticker"], "alerts": 0, "premium": 0.0})
        t["alerts"] += 1
        t["premium"] += a["premium"]
    top = sorted(tickers.values(), key=lambda x: -x["premium"])[:10]
    def _et(c):
        try:
            return utc_to_ny(datetime.strptime(c[:19].replace(" ", "T"), "%Y-%m-%dT%H:%M:%S")).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return None
    stamps = sorted(c for c in (a.get("created_at") for a in raw) if c)
    return {"alerts": out, "fetched": len(raw), "stale": stale, "pages": pages, "window": label,
            "returnedFrom": _et(stamps[0]) if stamps else None, "returnedTo": _et(stamps[-1]) if stamps else None, "since": start_ny.strftime("%Y-%m-%d %H:%M"),
            "totalPremium": round(sum(a["premium"] for a in out)), "topTickers": top}


def live_prices(sub, rows):
    """Add the option's current price and the stock's current price to each alert (Alpaca market data)."""
    import alpaca
    c = alpaca.creds(sub)
    if not c or not rows:
        return
    h = {"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"]}

    def fetch(url):
        req = urllib.request.Request(url, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError):
            return {}

    contracts = sorted({r["contract"] for r in rows if r.get("contract")})
    opt = {}
    for i in range(0, len(contracts), 100):
        q = urllib.parse.urlencode({"symbols": ",".join(contracts[i:i + 100]), "feed": "indicative"})
        for sym, snap in (fetch(f"https://data.alpaca.markets/v1beta1/options/snapshots?{q}").get("snapshots") or {}).items():
            qt, tr = snap.get("latestQuote") or {}, snap.get("latestTrade") or {}
            bid, ask = qt.get("bp"), qt.get("ap")
            opt[sym] = {"mark": round((bid + ask) / 2, 2) if bid and ask else tr.get("p"), "bid": bid, "ask": ask}
    tickers = sorted({r["ticker"] for r in rows if r.get("ticker")})
    stk = {}
    for i in range(0, len(tickers), 100):
        q = urllib.parse.urlencode({"symbols": ",".join(tickers[i:i + 100]), "feed": "iex"})
        data = fetch(f"https://data.alpaca.markets/v2/stocks/snapshots?{q}") or {}
        for sym, snap in data.items():
            p = ((snap or {}).get("latestTrade") or {}).get("p") or ((snap or {}).get("dailyBar") or {}).get("c")
            if p:
                stk[sym] = p
    for r in rows:
        o = opt.get(r.get("contract") or "")
        if o and o.get("mark"):
            r["nowPrice"], r["nowBid"], r["nowAsk"] = o["mark"], o.get("bid"), o.get("ask")
            if r.get("price"):
                r["nowChangePct"] = round((o["mark"] / r["price"] - 1) * 100, 1)
        sp = stk.get(r.get("ticker"))
        if sp:
            r["nowStock"] = round(sp, 2)
            if r.get("underlying"):
                r["stockChangePct"] = round((sp / r["underlying"] - 1) * 100, 2)
    return rows


def next_earnings(sub, symbol):
    """Next earnings date for a ticker from Unusual Whales (GET /api/stock/{ticker}/info: next_earnings_date,
    announce_time premarket/afterhours/unknown). Returns 'YYYY-MM-DD BMO|AMC', '' when none is scheduled
    (ETFs, unknown), or None when there is no Unusual Whales key."""
    it = db.get(db.upk(sub), SK)
    if not it:
        return None
    key = _key(sub)
    req = urllib.request.Request(f"https://api.unusualwhales.com/api/stock/{urllib.parse.quote(symbol)}/info",
                                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = (json.loads(r.read()) or {}).get("data") or {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return ""
        raise Unavailable(f"Unusual Whales returned an error ({e.code}) for {symbol} earnings.")
    except urllib.error.URLError as e:
        raise Unavailable(f"Unusual Whales couldn't be reached: {e.reason}")
    d = str(data.get("next_earnings_date") or "")[:10]
    if len(d) != 10:
        return ""
    # unknown timing -> before the open: the bot then exits a day earlier, the safe side
    return f"{d} {'AMC' if data.get('announce_time') == 'afterhours' else 'BMO'}"
