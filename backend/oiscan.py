"""Truly unusual options: a quiet option chain (almost no open interest anywhere) and one contract where somebody just
spent big money - the EGO Nov-20 36C case: 1,506 contracts on 44 open interest, ~$375K, in a chain of tens / hundreds.

Every 15 minutes in the session (and once after the close) the scanner reads full option chains from the Schwab
Market Data API (volume, open interest, bid / ask / last for every contract, every expiration) for the universe:
the watchlists + the tickers added here + today's NYSE / NASDAQ movers (volume and % up / down). A contract is a hit when
  - premium today (volume x mark x 100) >= minPremium,
  - volume >= minVolOi x its open interest (OI ~0: nobody held it before today),
  - and the whole chain is quiet: total open interest across ALL expirations <= maxChainOi.
Each hit is kept (first seen / last seen, the volume as it grows) and followed: the stock's move after it, in the
contract's direction, 1 / 3 / 5 / 10 trading days later. No history exists before the scanner runs: it collects forward."""
import json
import time
import urllib.parse
from datetime import datetime, timedelta

import db
from util import BadRequest, iso, now_ny

CFG = "OISCANCFG"
RUN = "OISCANRUN"
HIT = "OIHIT#"
MD = "https://api.schwabapi.com/marketdata/v1"
DEFAULT = {"enabled": True, "minPremium": 300000, "minVolOi": 10.0, "maxChainOi": 20000, "movers": True, "extra": [], "maxTickers": 250,
           "skip": ["SPY", "QQQ", "IWM", "DIA", "SPX", "NDX", "TSLA", "NVDA", "AAPL", "AMZN", "META", "MSFT", "GOOGL", "GOOG", "AMD"]}
HZ = (1, 3, 5, 10)


def config(sub):
    it = db.get(db.upk(sub), CFG) or {}
    return {k: it.get(k, v) for k, v in DEFAULT.items()}


def save(sub, b):
    c = config(sub)
    b = b or {}
    if "enabled" in b:
        c["enabled"] = bool(b["enabled"])
    if "movers" in b:
        c["movers"] = bool(b["movers"])
    for k, lo, hi in (("minPremium", 10000, 50e6), ("minVolOi", 1, 1000), ("maxChainOi", 100, 10e6), ("maxTickers", 10, 600)):
        if b.get(k) not in (None, ""):
            try:
                c[k] = max(lo, min(hi, float(b[k])))
            except (TypeError, ValueError):
                raise BadRequest(f"{k} must be a number.")
    for k in ("minPremium", "maxChainOi", "maxTickers"):
        c[k] = int(c[k])
    for k in ("extra", "skip"):
        if k in b:
            v = b[k]
            v = v.replace(",", " ").split() if isinstance(v, str) else (v or [])
            c[k] = sorted({str(t).strip().upper()[:10] for t in v if str(t).strip()})[:500]
    db.put({"PK": db.upk(sub), "SK": CFG, **c})
    return c


def _get(sub, path, q=None):
    import schwab
    item = db.get(db.upk(sub), schwab.SK)
    if not item:
        raise BadRequest("Connect Schwab in Settings → Brokers (with the Market Data API on the Schwab app).")
    access = schwab._access_token(sub, item)
    url = f"{MD}{path}" + (("?" + urllib.parse.urlencode(q)) if q else "")
    return schwab._http("GET", url, {"Authorization": f"Bearer {access}", "Accept": "application/json"})


def chain(sub, sym):
    return _get(sub, "/chains", {"symbol": sym, "contractType": "ALL", "range": "ALL", "includeUnderlyingQuote": "true"}) or {}


def movers(sub):
    out = []
    for idx in ("NYSE", "NASDAQ"):
        for sort in ("VOLUME", "PERCENT_CHANGE_UP", "PERCENT_CHANGE_DOWN"):
            try:
                r = _get(sub, f"/movers/{urllib.parse.quote(idx)}", {"sort": sort, "frequency": 0}) or {}
                out += [x.get("symbol") for x in (r.get("screeners") or []) if x.get("symbol")]
            except Exception as e:
                print("oiscan movers failed", idx, sort, e)
    return out


def universe(sub, c):
    import wallwatch
    tk = list(wallwatch.config(sub)["tickers"]) + list(c["extra"])
    if c["movers"]:
        tk += movers(sub)
    skip = set(c["skip"])
    return [t for t in dict.fromkeys(x.upper() for x in tk if x) if t not in skip and t.isalpha()][:c["maxTickers"]]


def contracts(ch):
    """[(putCall, exp, strike, contract dict)] of a Schwab chain."""
    out = []
    for side, key in (("call", "callExpDateMap"), ("put", "putExpDateMap")):
        for exp, strikes in (ch.get(key) or {}).items():
            for k, lst in (strikes or {}).items():
                for x in lst or []:
                    out.append((side, exp.split(":")[0], float(k), x))
    return out


def hits_of(ch, sym, c, at):
    cs = contracts(ch)
    if not cs:
        return [], 0
    chain_oi = sum(int(x.get("openInterest") or 0) for _, _, _, x in cs)
    if chain_oi > c["maxChainOi"]:
        return [], chain_oi
    under = ch.get("underlyingPrice") or (ch.get("underlying") or {}).get("last")
    out = []
    for side, exp, k, x in cs:
        vol, oi = int(x.get("totalVolume") or 0), int(x.get("openInterest") or 0)
        bid, ask, last = float(x.get("bid") or 0), float(x.get("ask") or 0), float(x.get("last") or 0)
        mark = float(x.get("mark") or 0) or ((bid + ask) / 2 if ask else last)
        prem = vol * mark * 100
        if prem < c["minPremium"] or vol < c["minVolOi"] * max(oi, 1):
            continue
        pos = (last - bid) / (ask - bid) if ask > bid and last else None
        try:
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - datetime.strptime(at[:10], "%Y-%m-%d").date()).days
        except ValueError:
            dte = None
        out.append({"sym": sym, "contract": x.get("symbol"), "side": side, "strike": k, "exp": exp, "dte": dte, "vol": vol, "oi": oi,
                    "ratio": round(vol / max(oi, 1), 1), "prem": round(prem), "mark": mark, "bid": bid, "ask": ask, "last": last,
                    "askSide": None if pos is None else ("at the ask" if pos >= 0.6 else "at the bid" if pos <= 0.4 else "mid"),
                    "otm": None if not under else round(((k / under - 1) if side == "call" else (1 - k / under)) * 100, 1),
                    "chainOi": chain_oi, "under": under})
    return out, chain_oi


def scan(sub, budget_s=480):
    c = config(sub)
    now = now_ny()
    at = iso(now)
    tk = universe(sub, c)
    t0, n, found, errs = time.time(), 0, 0, []
    pk = db.upk(sub)
    for sym in tk:
        if time.time() - t0 > budget_s:
            break
        try:
            ch = chain(sub, sym)
        except Exception as e:
            errs.append(f"{sym}: {str(e)[:80]}")
            if "expired" in str(e).lower() or "rejected" in str(e).lower():
                break
            continue
        n += 1
        hs, _ = hits_of(ch, sym, c, at)
        for h in hs:
            key = f"{HIT}{at[:10]}#{sym}#{h['contract']}"
            old = db.get(pk, key) or {}
            db.put({**old, **h, "PK": pk, "SK": key, "day": at[:10], "firstSeen": old.get("firstSeen") or at,
                    "firstUnder": old.get("firstUnder") or h["under"], "firstVol": old.get("firstVol") or h["vol"], "lastSeen": at})
            found += 1
        time.sleep(0.45)                               # Schwab market data: 120 requests a minute
    run = {"at": at, "tickers": n, "universe": len(tk), "hits": found, "errors": errs[-8:]}
    db.put({"PK": pk, "SK": RUN, **run})
    return run


def kick():
    """Every 15 minutes in the session: each user with the scanner on and Schwab connected."""
    hm = now_ny().strftime("%H:%M")
    if now_ny().weekday() >= 5 or not ("09:35" <= hm <= "16:20"):
        return 0
    n = 0
    for sub in db.owners():
        try:
            if config(sub)["enabled"] and db.get(db.upk(sub), "BROKER#schwab"):
                scan(sub)
                n += 1
        except Exception as e:
            print("oiscan failed", e)
    return n


def view(sub, days=60):
    """The hits of the last `days` days, newest first, each with the stock's move after it (in its direction)."""
    import plab
    pk = db.upk(sub)
    since = (now_ny() - timedelta(days=days)).strftime("%Y-%m-%d")
    hits = [{k: v for k, v in h.items() if k not in ("PK", "SK")} for h in db.q_prefix(pk, HIT) if (h.get("day") or "") >= since]
    bars = {}
    for h in hits:
        s = h["sym"]
        if s not in bars:
            try:
                bars[s] = plab._bars(s, 1) or {}
            except Exception:
                bars[s] = {}
        b = bars[s]
        ds = sorted(d for d in b if d > h["day"])
        d = 1 if h["side"] == "call" else -1
        u = h.get("firstUnder") or h.get("under")
        h["moves"] = {str(k): round(d * (b[ds[k - 1]][3] / u - 1) * 100, 2) for k in HZ if u and len(ds) >= k}
    hits.sort(key=lambda h: h.get("firstSeen") or "", reverse=True)
    summ = {}
    for k in HZ:
        xs = [h["moves"][str(k)] for h in hits if str(k) in h.get("moves", {})]
        if xs:
            summ[str(k)] = {"n": len(xs), "avg": round(sum(xs) / len(xs), 2), "win": round(sum(1 for x in xs if x > 0) / len(xs) * 100)}
    return {"cfg": config(sub), "run": {k: v for k, v in (db.get(pk, RUN) or {}).items() if k not in ("PK", "SK")}, "hits": hits[:300],
            "summary": summ, "schwab": bool(db.get(pk, "BROKER#schwab"))}


def job(sub, params=None):
    return scan(sub)
