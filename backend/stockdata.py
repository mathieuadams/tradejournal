"""Stock candles for the strategies (QQQ, SPY): Alpaca 5-minute regular-hours bars, stored once and topped up.

Stored in store "stockdata/<SYM>/5m" as [["YYYY-MM-DDTHH:MM", o, h, l, c, v], ...] (ET, bar start), shared by every
user (market data); fetched with the requesting user's Alpaca key. 15 / 30 / 60-minute candles are built from them
(60 starts at 09:30, like TradeWellX). A strategy unit = 100 shares (no commission, 1 cent slippage a side)."""
import time
from datetime import datetime, timedelta

import store
from util import BadRequest, ny_to_utc, utc_to_ny

SYMS = ("QQQ", "SPY")
NAMES = {"QQQ": "Invesco QQQ (Nasdaq-100 ETF)", "SPY": "SPDR S&P 500 ETF"}
_MEM = {}


def is_stock(sym):
    return sym in SYMS


def _key(sym):
    return f"stockdata/{sym}/5m"


def bars(sym):
    """The stored 5-minute bars (cached in this process for 10 minutes)."""
    hit = _MEM.get(sym)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    b = store.get_json(_key(sym), []) or []
    _MEM[sym] = (time.time(), b)
    return b


def refresh(sub, sym, days=520):
    """Fetch what is missing: the whole history the first time (`days` sessions), then only the new sessions."""
    import alpaca
    if sym not in SYMS:
        raise BadRequest(f"Stocks for the strategies: {', '.join(SYMS)}.")
    if not alpaca.status(sub).get("connected"):
        raise BadRequest("Connect Alpaca on the Data page to test strategies on QQQ / SPY.")
    have = list(bars(sym))
    c, feed = alpaca.creds(sub), alpaca.status(sub)["feed"]
    end = datetime.utcnow() - timedelta(minutes=16 if feed == "sip" else 0)
    want_from = (utc_to_ny(end) - timedelta(days=int(days * 1.47) + 10)).date()
    if have and have[0][0][:10] <= (want_from + timedelta(days=10)).isoformat():
        start_day = datetime.strptime(have[-1][0][:10], "%Y-%m-%d").date()            # re-fetch the last stored day (may be partial)
        have = [b for b in have if b[0][:10] < start_day.isoformat()]
    else:
        start_day, have = want_from, []
    start = ny_to_utc(datetime.combine(start_day, datetime.min.time()))
    new, token = [], None
    for _ in range(200):
        r = alpaca._get(c, f"stocks/{sym}/bars", {"timeframe": "5Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                  "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "adjustment": "split",
                                                  "feed": feed, "sort": "asc", "page_token": token})
        for b in r.get("bars") or []:
            t = utc_to_ny(datetime.strptime(b["t"][:19], "%Y-%m-%dT%H:%M:%S"))
            hm = t.strftime("%H:%M")
            if "09:30" <= hm < "16:00":
                new.append([t.strftime("%Y-%m-%dT%H:%M"), b["o"], b["h"], b["l"], b["c"], b.get("v", 0)])
        token = r.get("next_page_token")
        if not token:
            break
    out = have + new
    store.put_json(_key(sym), out)
    _MEM[sym] = (time.time(), out)
    return len(out)


def ensure(sub, sym, days):
    """Stored and fresh (topped up when the last stored session is older than the last weekday)."""
    b = bars(sym)
    today = utc_to_ny(datetime.utcnow()).date()
    last_wd = today - timedelta(days=1)
    while last_wd.weekday() >= 5:
        last_wd -= timedelta(days=1)
    have_days = len({x[0][:10] for x in b})
    if not b or b[-1][0][:10] < last_wd.isoformat() or have_days < min(days, 480):
        try:
            refresh(sub, sym, max(days + 30, 520))
        except BadRequest:
            if not b:
                raise


def trading_days(sym):
    return sorted({b[0][:10] for b in bars(sym)})


def daily(sym):
    """[[day, o, h, l, c, v]] regular-hours daily candles."""
    out = {}
    for t, o, h, l, c, v in bars(sym):
        d = t[:10]
        if d not in out:
            out[d] = [d, o, h, l, c, v]
        else:
            x = out[d]
            x[2], x[3], x[4], x[5] = max(x[2], h), min(x[3], l), c, x[5] + v
    return [out[d] for d in sorted(out)]


def coverage(sym):
    days = trading_days(sym)
    return {"name": NAMES.get(sym, sym), "days": len(days), "from": days[0] if days else None, "to": days[-1] if days else None,
            "bars": len(bars(sym)), "months": 0, "fpDays": 0, "fpFrom": None}


def cells(sym):
    import futdata
    return futdata.cells(sym, daily(sym))


def series(sym, p):
    """Like ifvg._series for the futures: [(day, on, R, F, V)] (F = R: regular hours only), all candles, their index and the
    EMA track."""
    import futdata
    import ifvg
    by = {}
    for b in bars(sym):
        by.setdefault(b[0][:10], []).append(b)
    days = sorted(by)
    if p.get("_start"):
        keep = [d for d in days if d >= p["_start"]]
        first = days.index(keep[0]) if keep else len(days)
        days = days[max(0, first - 3):]
    else:
        days = days[-(p["days"] + 3):]
    rows, Fall = [], []
    for d in days:
        A = futdata.agg([list(x) for x in by[d]], p["tf"]) if p["tf"] != 5 else by[d]
        R = [tuple(b[:5]) for b in A]
        pv = vv = 0.0
        V = []
        for b in A:                                      # the session VWAP at each candle's close
            pv += (b[2] + b[3] + b[4]) / 3 * (b[5] or 1)
            vv += b[5] or 1
            V.append(pv / vv)
        rows.append((d, [], R, R, V))
        Fall += R
    return rows, Fall, {b[0]: i for i, b in enumerate(Fall)}, ifvg.ema_track(Fall)
