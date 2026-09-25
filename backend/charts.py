"""Candles for the trade chart. Options are charted on the underlying stock, futures on the
continuous front-month contract.

Source: Alpaca market data when the user connected Alpaca (stocks), otherwise Yahoo Finance's
public chart endpoint (no key; intraday history limited to ~60 days for 5m/15m and ~2 years for 1h).
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import alpaca
from grouping import describe
from util import BadRequest, Unavailable, iso, parse_iso, utc_to_ny

TFS = {  # timeframe -> (minutes per bar, bars before entry, bars after exit)
    "5m": (5, 60, 30), "15m": (15, 50, 25), "1h": (60, 50, 25), "4h": (240, 40, 20), "1d": (1440, 120, 30),
}
YAHOO_INTERVAL = {"5m": "5m", "15m": "15m", "1h": "60m", "4h": "60m", "1d": "1d"}


def chart_symbol(trade):
    d = describe(trade["sym"])
    return d["underlying"], d["assetType"]


def window(trade, tf):
    mins, pre, post = TFS[tf]
    # Calendar padding: markets are closed nights/weekends, so pad generously.
    pad_pre = timedelta(days=pre * 1.6) if tf == "1d" else timedelta(minutes=mins * pre * (4 if mins >= 60 else 3))
    pad_post = timedelta(days=post * 1.6) if tf == "1d" else timedelta(minutes=mins * post * (4 if mins >= 60 else 3))
    start = parse_iso(trade["openTs"]) - pad_pre
    end = (parse_iso(trade["closeTs"]) if trade.get("closeTs") else parse_iso(trade["openTs"])) + pad_post
    now = datetime.utcnow() - timedelta(hours=4)
    return start, min(end, now + timedelta(hours=8))


def _yahoo(symbol, tf, start, end):
    params = {"interval": YAHOO_INTERVAL[tf], "includePrePost": "false", "events": "",
              "period1": _epoch(start),
              "period2": _epoch(end)}
    last = None
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (tradejournal)", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            last = body
            if e.code in (400, 422):
                try:
                    msg = json.loads(body)["chart"]["error"]["description"]
                except Exception:
                    msg = body[:200]
                raise BadRequest(f"No {tf} bars for {symbol} in this period: {msg}")
            time.sleep(1)
        except urllib.error.URLError as e:
            last = str(e.reason)
            time.sleep(1)
    else:
        raise Unavailable(f"Market data couldn't be loaded right now. Try again in a minute. ({str(last)[:120]})")
    res = (data.get("chart") or {}).get("result") or []
    if not res or not res[0].get("timestamp"):
        return []
    r0 = res[0]
    q = r0["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(r0["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        v = (q.get("volume") or [None] * (i + 1))[i] or 0
        t = utc_to_ny(datetime.utcfromtimestamp(ts))
        out.append({"t": iso(t), "o": round(o, 4), "h": round(h, 4), "l": round(l, 4), "c": round(c, 4), "v": v})
    return out


def _epoch(local_ny):
    from util import ny_to_utc
    from calendar import timegm
    return timegm(ny_to_utc(local_ny).timetuple())


def _to_4h(bars):
    """Aggregate 1h regular-session bars into 4h bars: 9:30-13:30 and 13:30-16:00 each day."""
    out, key = [], None
    for b in bars:
        m = int(b["t"][11:13]) * 60 + int(b["t"][14:16])
        k = (b["t"][:10], 0 if m < 810 else 1)
        if k != key:
            out.append(dict(b))
            key = k
        else:
            a = out[-1]
            a["h"], a["l"], a["c"] = max(a["h"], b["h"]), min(a["l"], b["l"]), b["c"]
            a["v"] = a.get("v", 0) + b.get("v", 0)
    return out


def get_bars(sub, trade, tf):
    if tf not in TFS:
        raise BadRequest("Timeframe must be one of 5m, 15m, 1h, 4h, 1d.")
    symbol, kind = chart_symbol(trade)
    start, end = window(trade, tf)
    c = None
    if kind in ("stock", "option"):
        try:
            c = alpaca.creds(sub)
        except Exception:
            c = None
    if c:
        bars = alpaca.bars(c, symbol, iso(start), iso(end), tf)
        source = "Alpaca"
    else:
        ysym = f"{symbol}=F" if kind == "future" else symbol.replace(".", "-")
        bars = _yahoo(ysym, tf, start, end)
        if tf == "4h":
            bars = _to_4h(bars)
        source = "Yahoo Finance"
    if not bars:
        raise BadRequest(f"No {tf} bars for {symbol} in this period. Try the daily chart.")
    return {"tf": tf, "symbol": symbol, "kind": kind, "source": source, "bars": bars}
