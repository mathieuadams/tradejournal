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
        try:                                           # days to expiry on the day of the alert (an old alert's contract
            d_at = datetime.strptime(created[:10], "%Y-%m-%d").date()   # may have expired since: it still counts)
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - min(d_at, today)).days
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
                    "at": created, "atEt": at_et, "id": a.get("id")})
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


def stock_quotes(sub, symbols):
    """{symbol: {"m": mark, "ch": change, "pct": % change}} vs the previous close (Alpaca snapshots, IEX feed).
    Mark = bid/ask middle when the spread is tight (< 0.5%), else the last trade."""
    import alpaca
    c = alpaca.creds(sub)
    if not c or not symbols:
        return {}
    h = {"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"]}
    today = now_ny().strftime("%Y-%m-%d")
    out = {}
    syms = sorted({s.upper() for s in symbols if s})
    for i in range(0, len(syms), 100):
        q = urllib.parse.urlencode({"symbols": ",".join(syms[i:i + 100]), "feed": "iex"})
        req = urllib.request.Request(f"https://data.alpaca.markets/v2/stocks/snapshots?{q}", headers=h)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read()) or {}
        except (urllib.error.HTTPError, urllib.error.URLError):
            continue
        for sym, sn in data.items():
            sn = sn or {}
            tr, qt, db_, pdb = sn.get("latestTrade") or {}, sn.get("latestQuote") or {}, sn.get("dailyBar") or {}, sn.get("prevDailyBar") or {}
            bid, ask, last = qt.get("bp"), qt.get("ap"), tr.get("p") or db_.get("c")
            mark = (bid + ask) / 2 if bid and ask and ask >= bid and (ask - bid) / ((ask + bid) / 2) < 0.005 else last
            prev = pdb.get("c") if str(db_.get("t") or "")[:10] >= today else db_.get("c")
            if not mark:
                continue
            out[sym] = {"m": round(mark, 4), "ch": round(mark - prev, 4) if prev else None,
                        "pct": round((mark / prev - 1) * 100, 2) if prev else None}
    return out


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


def ping(sub):
    """Liveness check with no filters: the newest flow alerts of any kind. Returns how many came back and the newest
    alert's time (ET) and ticker."""
    from util import utc_to_ny
    key = _key(sub)
    d = _get(key, {"limit": 50})
    rows = d.get("data") or []
    newest = max(rows, key=lambda a: a.get("created_at") or "", default=None)
    at = None
    if newest and newest.get("created_at"):
        try:
            at = utc_to_ny(datetime.strptime(str(newest["created_at"])[:19], "%Y-%m-%dT%H:%M:%S")).strftime("%Y-%m-%d %H:%M")
        except Exception:
            at = str(newest["created_at"])[:16]
    return {"alerts": len(rows), "tickers": len({a.get("ticker") for a in rows}), "newest": at,
            "newestTicker": (newest or {}).get("ticker")}


# ---------- gamma walls by day (Unusual Whales greek exposure, from each day's open interest) ----------

def _uw(key, path, params=None, attempts=4):
    import time as _t
    url = "https://api.unusualwhales.com" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429 and attempts > 1:          # rate limited: wait and try again
            _t.sleep(1.5 * (5 - attempts))
            return _uw(key, path, params, attempts - 1)
        if e.code in (404, 422):
            return None
        if e.code in (401, 403):
            raise BadRequest("Unusual Whales: this plan doesn't include historical greek exposure.")
        raise Unavailable(f"Unusual Whales returned an error ({e.code}).")
    except urllib.error.URLError as e:
        raise Unavailable(f"Unusual Whales couldn't be reached: {e.reason}")


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _pick(r, *names):
    for n in names:
        v = _num(r.get(n))
        if v is not None:
            return v
    return None


def walls_from_strikes(strikes):
    """Call wall = strike with the most call gamma; put wall = strike with the most put gamma (anywhere: the call wall
    can end up below the put wall). strikes: [[strike, call_gex, put_gex], ...]"""
    if not strikes:
        return None, None
    cw = max(strikes, key=lambda r: r[1] or 0)
    pw = max(strikes, key=lambda r: abs(r[2] or 0))
    return (cw[0] if (cw[1] or 0) > 0 else None), (pw[0] if pw[2] else None)


def gex_day(key, ticker, date, flip=True):
    """Walls for one market date from that day's open interest across all expirations (Unusual Whales greek exposure
    by strike). The strikes are stored too, so the walls can be recomputed. Past days are kept for good; today is
    refreshed hourly; a day with no data is retried after 6 hours. flip=False skips the extra gamma-levels request."""
    import time as _t
    ck = f"{ticker}#{date}"
    hit = db.get("UWGEX2", ck)
    today = now_ny().strftime("%Y-%m-%d")
    if hit:
        age = _t.time() - hit.get("at", 0)
        data = hit.get("data")
        fresh = data and (date < today or age < 3600)
        if fresh and (not flip or not data.get("noflip")):
            return data
        if fresh and flip and data.get("noflip"):          # stored by the backfill without the flip: add it
            data = {**data, "flip": _flip(key, ticker, date)}
            data.pop("noflip", None)
            try:
                db.put({"PK": "UWGEX2", "SK": ck, "at": hit.get("at", int(_t.time())), "data": data})
            except Exception:
                pass
            return data
        if not data and age < 6 * 3600:
            return None
    s = _uw(key, f"/api/stock/{urllib.parse.quote(ticker)}/greek-exposure/strike", {"date": date}) or {}
    raw = s.get("data") if isinstance(s, dict) else s
    rows = [r for r in raw or [] if isinstance(r, dict) and _num(r.get("strike")) is not None]
    out = None
    if rows:
        cg = lambda r: _pick(r, "call_gex", "call_gamma", "call_gamma_oi") or 0
        pg = lambda r: _pick(r, "put_gex", "put_gamma", "put_gamma_oi") or 0
        strikes = sorted(([_num(r["strike"]), round(cg(r)), round(pg(r))] for r in rows), key=lambda x: -(abs(x[1]) + abs(x[2])))[:120]
        strikes.sort(key=lambda x: x[0])
        cw, pw = walls_from_strikes(strikes)
        if cw is not None or pw is not None:
            out = {"date": date, "callWall": cw, "putWall": pw, "strikes": strikes,
                   "callGex": round(sum(cg(r) for r in rows)), "putGex": round(sum(pg(r) for r in rows))}
            if flip:
                out["flip"] = _flip(key, ticker, date)
            else:
                out["noflip"] = True
    elif raw is not None and not isinstance(raw, list):
        print("greek exposure: unexpected shape", ticker, date, str(s)[:300])
    try:
        db.put({"PK": "UWGEX2", "SK": ck, "at": int(_t.time()), "data": out})
    except Exception as e:
        print("walls cache skipped", e)
    return out


def _flip(key, ticker, date):
    try:
        lv = _uw(key, f"/api/stock/{urllib.parse.quote(ticker)}/gex-levels", {"date": date, "source": "oi"}) or {}
        lv = lv.get("data", lv) if isinstance(lv, dict) else {}
        if isinstance(lv, list):
            lv = lv[0] if lv else {}
        f = _num(lv.get("gamma_flip"))
        return round(f, 2) if f else None
    except Exception as e:
        print("gex levels failed", ticker, date, e)
        return None


def stored_walls(ticker):
    """Every stored day of walls for a ticker: {date: {callWall, putWall, flip}}."""
    out = {}
    for r in db.q_prefix("UWGEX2", f"{ticker}#"):
        d = r.get("data")
        if d and d.get("date"):
            out[d["date"]] = {k: d.get(k) for k in ("callWall", "putWall", "flip")}
    return out


DAILY_EXP = {"SPY", "QQQ", "IWM", "SPX", "SPXW", "XSP", "NDX", "NDXP", "RUT", "DIA"}


def _exp_candidates(ticker, date):
    """Expirations to try for the nearest one on `date` (the same day first for the daily-expiry ETFs / indexes,
    the coming Friday first for the others, then the next weekdays)."""
    from datetime import timedelta
    d0 = datetime.strptime(date, "%Y-%m-%d").date()
    days = [d0 + timedelta(days=i) for i in range(0, 9)]
    days = [d for d in days if d.weekday() < 5]
    if ticker.upper() not in DAILY_EXP:
        days.sort(key=lambda d: (d.weekday() != 4, d))
    return [d.isoformat() for d in days[:6]]


def flip_from_strikes(strikes, lo=None, hi=None):
    """Approximate gamma flip: where the running net gamma (call − |put|, from the lowest strike up) turns from
    negative to positive; the crossing nearest the middle of [lo, hi] when there are several."""
    run, prev, xs = 0.0, None, []
    for k, cg, pg in sorted(strikes):
        run += (cg or 0) - abs(pg or 0)
        if prev is not None and prev < 0 <= run:
            xs.append(k)
        prev = run
    if not xs:
        return None
    mid = (lo + hi) / 2 if lo and hi else None
    return round(min(xs, key=lambda x: abs(x - mid)) if mid else xs[0], 2)


def gex_near_day(key, ticker, date):
    """Walls for one market date from the NEAREST expiration only (0DTE for SPY / QQQ): Unusual Whales greek exposure
    by strike for that expiry. Flip approximated from the strikes. Cached like gex_day."""
    import time as _t
    ck = f"{ticker}#{date}"
    hit = db.get("UWGEXN", ck)
    today = now_ny().strftime("%Y-%m-%d")
    if hit:
        age = _t.time() - hit.get("at", 0)
        data = hit.get("data")
        if data and (date < today or age < 3600):
            return data
        if not data and (date < today and age < 7 * 86400 or age < 6 * 3600):
            return None
    out = None
    for exp in _exp_candidates(ticker, date):
        s = _uw(key, f"/api/stock/{urllib.parse.quote(ticker)}/greek-exposure/strike-expiry", {"date": date, "expiry": exp}) or {}
        raw = s.get("data") if isinstance(s, dict) else s
        rows = [r for r in raw or [] if isinstance(r, dict) and _num(r.get("strike")) is not None]
        if not rows:
            continue
        cg = lambda r: _pick(r, "call_gex", "call_gamma", "call_gamma_oi") or 0
        pg = lambda r: _pick(r, "put_gex", "put_gamma", "put_gamma_oi") or 0
        strikes = sorted(([_num(r["strike"]), round(cg(r)), round(pg(r))] for r in rows), key=lambda x: -(abs(x[1]) + abs(x[2])))[:120]
        strikes.sort(key=lambda x: x[0])
        cw, pw = walls_from_strikes(strikes)
        if cw is None and pw is None:
            continue
        out = {"date": date, "expiry": exp, "callWall": cw, "putWall": pw, "strikes": strikes,
               "flip": flip_from_strikes(strikes, pw, cw)}
        break
    try:
        db.put({"PK": "UWGEXN", "SK": ck, "at": int(_t.time()), "data": out})
    except Exception as e:
        print("near walls cache skipped", e)
    return out


def stored_near_walls(ticker):
    out = {}
    for r in db.q_prefix("UWGEXN", f"{ticker}#"):
        d = r.get("data")
        if d and d.get("date"):
            out[d["date"]] = {k: d.get(k) for k in ("callWall", "putWall", "flip", "expiry")}
    return out


def near_walls(sub, ticker, dates, budget_s=240):
    """Nearest-expiration walls for each date: stored ones, the missing ones fetched (4 at a time, time-limited)."""
    import time as _t
    from concurrent.futures import ThreadPoolExecutor
    t0 = _t.time()
    have = stored_near_walls(ticker)
    w = {d: have[d] for d in dates if d in have}
    today = now_ny().strftime("%Y-%m-%d")
    todo = [d for d in dates if d not in w or d == today]
    if not todo:
        return w
    key = _key(sub)
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i in range(0, len(todo), 8):
            if _t.time() - t0 > budget_s:
                break
            futs = {d: ex.submit(gex_near_day, key, ticker, d) for d in todo[i:i + 8]}
            for d, f in futs.items():
                try:
                    g = f.result()
                    if g:
                        w[d] = {k: g.get(k) for k in ("callWall", "putWall", "flip", "expiry")}
                except Exception as e:
                    print("near walls failed", ticker, d, e)
    return w


def walls_backfill(sub, days=45, budget_s=480):
    """Store the walls of every day for every ticker the bot evaluated in the last `days` days (from 5 trading days
    before its first evaluation to today). Resumes where it stopped: days already stored are skipped."""
    import time as _t
    from datetime import timedelta
    from concurrent.futures import ThreadPoolExecutor
    t0 = _t.time()
    key = _key(sub)
    today = now_ny().date()
    cut = (now_ny() - timedelta(days=days)).strftime("%Y-%m-%d")
    first = {}
    for r in db.q_prefix(db.upk(sub), "BOT#"):
        sym, at = r.get("symbol"), (r.get("createdAt") or "")[:10]
        if sym and at >= cut:
            first[sym] = min(first.get(sym, at), at)
    todo = []
    for sym, f0 in sorted(first.items()):
        have = set(stored_walls(sym))
        d = datetime.strptime(f0, "%Y-%m-%d").date() - timedelta(days=7)
        while d <= today:
            ds = d.isoformat()
            if d.weekday() < 5 and ds not in have:
                todo.append((sym, ds))
            d += timedelta(days=1)
    done, errors = 0, 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i in range(0, len(todo), 8):
            if _t.time() - t0 > budget_s:
                break
            for f in [ex.submit(gex_day, key, s_, d_, False) for s_, d_ in todo[i:i + 8]]:
                try:
                    f.result()
                    done += 1
                except Exception:
                    errors += 1
    out = {"tickers": len(first), "missing": len(todo), "stored": done, "errors": errors, "left": max(0, len(todo) - done - errors),
           "at": now_ny().strftime("%Y-%m-%dT%H:%M")}
    db.put({"PK": db.upk(sub), "SK": "WALLSRUN", **out})
    return out


def gex_history(sub, ticker, dates):
    """Walls for each market date (fetched in parallel, cached per day). Returns (rows, error or None)."""
    from concurrent.futures import ThreadPoolExecutor
    key = _key(sub)
    rows, err = [], None
    with ThreadPoolExecutor(max_workers=4) as ex:          # gentle on the Unusual Whales rate limit
        futs = {d: ex.submit(gex_day, key, ticker, d) for d in dates}
        for d, f in futs.items():
            try:
                r = f.result()
                if r:
                    rows.append(r)
            except Exception as e:
                err = str(e)[:160]
    return sorted(rows, key=lambda r: r["date"]), err


# ---------- dark pool prints (Unusual Whales /api/darkpool/{ticker}, one request per market date, cached per day) ----------

def darkpool_day(key, ticker, date, min_premium):
    import time as _t
    from util import utc_to_ny
    ck = f"{ticker}#{date}#{int(min_premium)}#2"          # v2: with the bid / ask at the print (buy or sell side)
    hit = db.get("UWDP", ck)
    today = now_ny().strftime("%Y-%m-%d")
    if hit and (date < today or _t.time() - hit.get("at", 0) < 900):
        return hit.get("prints") or []
    s = _uw(key, f"/api/darkpool/{urllib.parse.quote(ticker)}", {"date": date, "min_premium": int(min_premium), "limit": 500,
                                                                  "order_by": "premium", "cancellation_status": "hide_cancelled"}) or {}
    raw = s.get("data") if isinstance(s, dict) else s
    out = []
    for r in raw or []:
        if not isinstance(r, dict) or r.get("canceled"):
            continue
        px_, sz, prem = _num(r.get("price")), _num(r.get("size")), _num(r.get("premium"))
        at = str(r.get("executed_at") or "")[:19]
        if not px_ or not at:
            continue
        try:
            t = utc_to_ny(datetime.strptime(at, "%Y-%m-%dT%H:%M:%S")).strftime("%Y-%m-%dT%H:%M")
        except ValueError:
            continue
        bid, ask = _num(r.get("nbbo_bid")), _num(r.get("nbbo_ask"))
        side = None
        if bid and ask and ask >= bid:              # at / above the ask: bought; at / below the bid: sold; between: unknown
            side = "buy" if px_ >= ask else "sell" if px_ <= bid else ("buy" if px_ > (bid + ask) / 2 else "sell" if px_ < (bid + ask) / 2 else None)
        out.append({"t": t, "price": round(px_, 4), "size": int(sz or 0), "premium": round(prem or (px_ * (sz or 0))), "side": side})
    try:
        db.put({"PK": "UWDP", "SK": ck, "at": int(_t.time()), "prints": out})
    except Exception as e:          # an oversized day: keep the biggest
        try:
            db.put({"PK": "UWDP", "SK": ck, "at": int(_t.time()), "prints": sorted(out, key=lambda x: -x["premium"])[:150]})
        except Exception:
            print("darkpool cache skipped", e)
    return out


def darkpool(sub, ticker, dates, min_premium=1_000_000, budget_s=None, workers=4):
    """Dark pool prints at or above min_premium for each date, plus the price levels where the most premium traded.
    budget_s: stop waiting after that many seconds (the days already fetched are cached; "missing" says how many are left)."""
    import time as _t
    from concurrent.futures import ThreadPoolExecutor, wait
    key = _key(sub)
    prints, err, missing = [], None, 0
    t0 = _t.time()
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = [ex.submit(darkpool_day, key, ticker, d, min_premium) for d in sorted(dates, reverse=True)]   # newest first
    if budget_s:
        wait(futs, timeout=max(1, budget_s - (_t.time() - t0)))
    for f in futs:
        if budget_s and not f.done():
            missing += 1
            continue
        try:
            prints += f.result()
        except Exception as e:
            err = str(e)[:160]
    ex.shutdown(wait=False, cancel_futures=True)
    prints.sort(key=lambda x: x["t"])
    return {"prints": prints, "levels": dp_levels(prints, 10), "minPremium": min_premium, "error": err, "missing": missing}


def dp_levels(prints, top_n=5):
    """Price levels where the most dark pool premium traded (bands of ~0.25% of price), with the bought / sold split."""
    levels = {}
    if prints:
        ps = sorted(p["price"] for p in prints)
        step = max(0.01, round(ps[len(ps) // 2] * 0.0025, 2))          # bucket ~0.25% of price
        for p in prints:
            b = round(round(p["price"] / step) * step, 2)
            lv = levels.setdefault(b, {"price": b, "premium": 0, "prints": 0, "_pw": 0.0, "buy": 0, "sell": 0, "times": []})
            if p.get("t"):
                lv["times"].append([p["t"], p["premium"]])
            lv["premium"] += p["premium"]
            lv["prints"] += 1
            lv["_pw"] += p["price"] * p["premium"]
            if p.get("side") in ("buy", "sell"):
                lv[p["side"]] += p["premium"]
    for lv in levels.values():          # the level sits at the premium-weighted price of its prints
        lv["price"] = round(lv.pop("_pw") / lv["premium"], 2) if lv["premium"] else lv["price"]
        lv["times"].sort()
        lv["first"] = lv["times"][0][0] if lv["times"] else None      # when the level started (first print)
        lv["last"] = lv["times"][-1][0] if lv["times"] else None
    out = sorted(levels.values(), key=lambda x: -x["premium"])[:top_n]
    if top_n > 20:                                     # the volume profile doesn't need every print time
        for lv in out:
            lv.pop("times", None)
    return out


# ---------- morning flip snapshots: the walls / flip exactly as Unusual Whales shows them before / at the open ----------

SNAP_PK = "FLIPSNAP"


def snap_walls(key, ticker, date):
    """Fresh (no cache) walls + flip for `date` as Unusual Whales returns them right now."""
    s = _uw(key, f"/api/stock/{urllib.parse.quote(ticker)}/greek-exposure/strike", {"date": date}) or {}
    raw = s.get("data") if isinstance(s, dict) else s
    rows = [r for r in raw or [] if isinstance(r, dict) and _num(r.get("strike")) is not None]
    cw = pw = None
    if rows:
        cg = lambda r: _pick(r, "call_gex", "call_gamma", "call_gamma_oi") or 0
        pg = lambda r: _pick(r, "put_gex", "put_gamma", "put_gamma_oi") or 0
        cw, pw = walls_from_strikes([[_num(r["strike"]), round(cg(r)), round(pg(r))] for r in rows])
    return {"callWall": cw, "putWall": pw, "flip": _flip(key, ticker, date)}


def flip_snapshot(key, tickers, now=None, price=None):
    """Store the walls / flip of today for each ticker, stamped with the time (FLIPSNAP / ticker#date#HH:MM)."""
    import time as _t
    from decimal import Decimal
    now = now or now_ny()
    d, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    out = {}
    for sym in tickers:
        try:
            w = snap_walls(key, sym, d)
            px = price(sym) if price else None
            item = {"PK": SNAP_PK, "SK": f"{sym}#{d}#{hm}", "sym": sym, "date": d, "hm": hm, "at": int(_t.time()),
                    **{k: Decimal(str(v)) for k, v in w.items() if v is not None}}
            if px:
                item["price"] = Decimal(str(round(px, 4)))
            db.put(item)
            out[sym] = {**w, "price": px}
        except Exception as e:
            out[sym] = {"error": str(e)[:120]}
    return out


def flip_snapshots(ticker=None):
    from decimal import Decimal
    rows = db.q_prefix(SNAP_PK, f"{ticker}#" if ticker else "")
    return [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in r.items() if k not in ("PK", "SK")} for r in rows]
