"""Real order flow from Alpaca trades (SIP): every trade of the day classified as a buy or a sell by the tick rule
(price above the previous different price = buy, below = sell, unchanged = same side as the previous trade), summed
per minute -> buy volume, sell volume, delta and the cumulative volume delta; plus the largest trades of the day.
Past days are stored for good ("OFLOW" sym#date); today is refreshed every 2 minutes (the SIP feed is 15 minutes
behind on the free data plan). Days of SPY / QQQ hold ~0.3-1M trades: they are loaded in a background job."""
import json
import time
import urllib.parse
from datetime import datetime, timedelta

import db
from util import iso, now_ny, ny_to_utc, utc_to_ny

PK = "OFLOW"
BIG_N = 25
SK_JOB = "OFLOWJOB"


def _ny_min(t):
    """Alpaca trade time (UTC, nanoseconds) -> NY minute 'YYYY-MM-DDTHH:MM'."""
    return utc_to_ny(datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")).strftime("%Y-%m-%dT%H:%M")


def day_flow(c, sym, date, budget_s=240, get=None):
    """{min: {t: [buy, sell]}, big: [[t, price, size, side]], n: trades, done: bool} for one regular session."""
    import stockday
    get = get or stockday._get
    day = datetime.strptime(date, "%Y-%m-%d")
    start = ny_to_utc(day.replace(hour=9, minute=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_l = min(day.replace(hour=16), now_ny().replace(tzinfo=None) - timedelta(minutes=16))
    end = ny_to_utc(end_l).strftime("%Y-%m-%dT%H:%M:%SZ")
    mins, big, n, token, last_p, last_side = {}, [], 0, None, None, 1
    t0, done = time.time(), False
    for _ in range(400):
        if time.time() - t0 > budget_s:
            break
        q = {"symbols": sym, "start": start, "end": end, "limit": 10000, "feed": "sip"}
        if token:
            q["page_token"] = token
        d = get(c, "https://data.alpaca.markets/v2/stocks/trades", q) or {}
        for tr in (d.get("trades") or {}).get(sym) or []:
            p, s = float(tr.get("p") or 0), float(tr.get("s") or 0)
            if p <= 0 or s <= 0:
                continue
            side = 1 if last_p is None or p > last_p else -1 if p < last_p else last_side
            last_p, last_side = p, side
            m = _ny_min(tr["t"])
            x = mins.setdefault(m, [0.0, 0.0])
            x[0 if side > 0 else 1] += s
            n += 1
            if len(big) < BIG_N or p * s > big[-1][1] * big[-1][2]:
                big.append([m, p, int(s), side])
                big.sort(key=lambda r: -r[1] * r[2])
                del big[BIG_N:]
        token = d.get("next_page_token")
        if not token:
            done = True
            break
    return {"min": {k: [int(v[0]), int(v[1])] for k, v in mins.items()}, "big": big, "n": n, "done": done}


def cached(sym, date):
    r = db.get(PK, f"{sym}#{date}")
    if not r:
        return None
    today = now_ny().strftime("%Y-%m-%d")
    if date >= today and time.time() - r.get("at", 0) > 120:
        return None
    return json.loads(r["data"]) if r.get("data") else None


def store(sym, date, data):
    db.put({"PK": PK, "SK": f"{sym}#{date}", "at": int(time.time()), "data": json.dumps(data, separators=(",", ":"))})


def get_days(sub, sym, dates):
    """What is stored for these days, and the days still to load."""
    out, missing = {}, []
    for d in dates:
        x = cached(sym, d)
        if x and x.get("done"):
            out[d] = x
        else:
            missing.append(d)
    return out, missing


def job(sub, params=None):
    """Background: load the missing days of one symbol (newest first)."""
    import stockday
    p = params or {}
    sym = str(p.get("sym") or "").upper()
    dates = sorted(p.get("dates") or [], reverse=True)[:10]
    c = stockday.data_creds(sub)
    if not c or not sym:
        return {"skipped": True}
    t0, done = time.time(), []
    for d in dates:
        if time.time() - t0 > 480:
            break
        if (cached(sym, d) or {}).get("done"):
            continue
        x = day_flow(c, sym, d, budget_s=max(30, 500 - (time.time() - t0)))
        if x["done"]:
            store(sym, d, x)
            done.append(d)
    db.put({"PK": db.upk(sub), "SK": f"{SK_JOB}#{sym}", "running": None, "at": iso(now_ny()), "done": done})
    return {"done": done}
