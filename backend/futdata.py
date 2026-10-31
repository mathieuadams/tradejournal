"""NQ / ES market data from Databento, kept in S3, and everything built from it.

Files (store.py):
  <SYM>/1m/<YYYY-MM>     1-minute candles of the month (ET times): {"b": [[t, o, h, l, c, v], ...]}
  <SYM>/fp/<YYYY-MM-DD>  regular-hours footprint of the day, from every trade:
                         {"m": [[hh:mm, [[price, bid volume, ask volume], ...]], ...], "big": [[hh:mm:ss, price, size, "B"|"S"], ...]}
                         bid volume = sellers hitting the bid, ask volume = buyers lifting the offer.
Coverage (DynamoDB, PK FUTDATA, SK COV#<SYM>): the months stored (with each month's daily regular-hours candles)
and the footprint days stored.

A trading session ("day" D) runs from 18:00 ET on the evening before to 17:00 ET on D. Regular hours: 09:30-16:00."""
import time
from datetime import date, datetime, timedelta

import databento
import db
import store
from util import BadRequest, now_ny, ny_to_utc, utc_to_ny

SYMS = ("NQ", "ES")
SPEC = {"NQ": {"name": "E-mini Nasdaq-100", "tick": 0.25, "pv": 20.0, "big": 20},
        "ES": {"name": "E-mini S&P 500", "tick": 0.25, "pv": 50.0, "big": 50}}
PK = "FUTDATA"
RTH0, RTH1 = "09:30", "16:00"


def check_sym(sym):
    s = (sym or "").upper()
    if s not in SYMS:
        raise BadRequest("Symbol must be NQ or ES.")
    return s


# ---------------------------------------------------------------- coverage

def cov(sym):
    return db.get(PK, "COV#" + sym) or {"PK": PK, "SK": "COV#" + sym, "months": {}, "fp": []}


def coverage():
    out = {}
    for s in SYMS:
        c = cov(s)
        ms = sorted(c.get("months") or {})
        days = [d[0] for m in ms for d in (c["months"][m].get("d") or [])]
        out[s] = {"name": SPEC[s]["name"], "months": len(ms), "from": c["months"][ms[0]]["first"][:10] if ms else None,
                  "to": c["months"][ms[-1]]["last"] if ms else None, "bars": sum(int(c["months"][m].get("n") or 0) for m in ms),
                  "days": len(days), "fpDays": len(c.get("fp") or []), "fpFrom": min(c.get("fp") or [None]) if c.get("fp") else None,
                  "fpTo": max(c.get("fp") or [None]) if c.get("fp") else None, "hmDays": len(c.get("hm") or []),
                  "hmLast": sorted(c.get("hm") or [])[-12:]}
    return out


# ---------------------------------------------------------------- time helpers

def et(ts):
    """Databento time (ISO UTC with nanoseconds, or integer nanoseconds) -> ET 'YYYY-MM-DDTHH:MM:SS'."""
    ts = str(ts)
    if ts.isdigit():
        dt = datetime(1970, 1, 1) + timedelta(microseconds=int(ts) // 1000)
    else:
        dt = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
    return utc_to_ny(dt).strftime("%Y-%m-%dT%H:%M:%S")


def utc_iso(day, hm="00:00"):
    """ET date + HH:MM -> UTC ISO string for the API."""
    dt = datetime.strptime(f"{day}T{hm}", "%Y-%m-%dT%H:%M")
    return ny_to_utc(dt).strftime("%Y-%m-%dT%H:%M:%S")


def session_of(t):
    """The trading session (date) a bar at ET time t belongs to."""
    d = date.fromisoformat(t[:10])
    if t[11:16] >= "18:00":
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def _ym(d):
    return d[:7]


def _next_month(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y + (m == 12)}-{1 if m == 12 else m + 1:02d}"


# ---------------------------------------------------------------- candles

def load_month(sym, ym):
    cur = now_ny().strftime("%Y-%m")
    return (store.get_json(f"{sym}/1m/{ym}", {"b": []}, max_age=3600 * 12 if ym < cur else 60) or {}).get("b") or []


def _daily_of(bars):
    """Regular-hours daily candles [[day, o, h, l, c, v]] of a list of 1-minute bars."""
    by = {}
    for t, o, h, l, c, v in bars:
        if RTH0 <= t[11:16] < RTH1:
            by.setdefault(t[:10], []).append((o, h, l, c, v))
    return [[d, xs[0][0], max(x[1] for x in xs), min(x[2] for x in xs), xs[-1][3], sum(x[4] for x in xs)]
            for d, xs in sorted(by.items()) if len(xs) >= 30]


def _merge(sym, new):
    """Merge [[t, o, h, l, c, v]] into the month files and the coverage."""
    by = {}
    for b in new:
        by.setdefault(_ym(b[0]), []).append(b)
    c = cov(sym)
    for ym, xs in by.items():
        cur = {b[0]: b for b in load_month(sym, ym)}
        for b in xs:
            cur[b[0]] = b
        bars = [cur[k] for k in sorted(cur)]
        store.put_json(f"{sym}/1m/{ym}", {"b": bars})
        c["months"][ym] = {"n": len(bars), "first": bars[0][0], "last": bars[-1][0], "d": _daily_of(bars)}
    db.put(c)
    return c


def download_candles(sub, sym, start, end, prog=None, deadline=None):
    """1-minute candles for ET dates start..end (inclusive), a month per request. Returns None when done, else the
    date to continue from (out of time)."""
    sym = check_sym(sym)
    key = databento.key(sub)
    avail = databento.dataset_range(key)["end"]
    d = date.fromisoformat(start)
    stop = date.fromisoformat(end) + timedelta(days=1)
    while d < stop:
        if deadline and time.time() > deadline:
            return d.isoformat()
        nm = date.fromisoformat(_next_month(d.isoformat()[:7]) + "-01")
        nxt = min(nm, stop)
        s_utc, e_utc = utc_iso(d.isoformat()), utc_iso(nxt.isoformat())
        if avail and s_utc >= avail:
            break
        if avail and e_utc > avail:
            e_utc = avail
        if prog:
            prog(f"{sym}: candles {d.isoformat()} → {(nxt - timedelta(days=1)).isoformat()}")
        new = []
        for r in databento.rows(key, "ohlcv-1m", sym, s_utc, e_utc):
            try:
                new.append([et(r["ts_event"])[:16], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), int(float(r["volume"]))])
            except (KeyError, ValueError):
                continue
        if new:
            _merge(sym, new)
        d = nxt
    return None


def last_bar_time(sym):
    ms = sorted(cov(sym).get("months") or {})
    return cov(sym)["months"][ms[-1]]["last"] if ms else None


# ---------------------------------------------------------------- footprint (trades)

def download_footprint(sub, sym, day, force=False):
    """Every regular-hours trade of the day -> volume at each price per minute, split by aggressor side."""
    sym = check_sym(sym)
    c = cov(sym)
    if day in (c.get("fp") or []) and not force:
        return False
    if date.fromisoformat(day).weekday() >= 5:
        return False
    key = databento.key(sub)
    avail = databento.dataset_range(key)["end"]
    s_utc, e_utc = utc_iso(day, RTH0), utc_iso(day, RTH1)
    if avail and s_utc >= avail:
        return False
    if avail and e_utc > avail:
        e_utc = avail
    mins, big, big_min = {}, [], SPEC[sym]["big"]
    for r in databento.rows(key, "trades", sym, s_utc, e_utc):
        try:
            t, px, sz, side = et(r.get("ts_event") or r.get("ts_recv")), float(r["price"]), int(float(r["size"])), r.get("side") or "N"
        except (KeyError, ValueError):
            continue
        lv = mins.setdefault(t[11:16], {}).setdefault(px, [0, 0])
        if side == "A":                                # sell aggressor: traded on the bid
            lv[0] += sz
        elif side == "B":                              # buy aggressor: lifted the offer
            lv[1] += sz
        if sz >= big_min:
            big.append([t[11:19], px, sz, "B" if side == "B" else "S" if side == "A" else "N"])
    if not mins:
        return False
    _save_fp(sym, day, mins, big, c)
    db.put(c)
    return True


def _save_fp(sym, day, mins, big, c):
    store.put_json(f"{sym}/fp/{day}", {"m": [[m, sorted([[p, v[0], v[1]] for p, v in lv.items()], reverse=True)] for m, lv in sorted(mins.items())],
                                       "big": big[-4000:]})
    c["fp"] = sorted(set(c.get("fp") or []) | {day})


WINDOWS = {"rth": ("09:30", "16:00"), "am": ("09:30", "11:30"), "open": ("09:30", "10:30")}
HM_STEP = 2                                            # seconds per heatmap column


def download_book(sub, sym, day, window="rth", force=False):
    """The order book (MBP-10: the 10 best price levels on each side after every event) of the day's window, kept as
    a heatmap: one column every 2 seconds with the resting size at each of those levels, the volume bought / sold
    and the last trade price. The trades in the same stream
    give the footprint too (it replaces a trades download for that day)."""
    import dbn
    sym = check_sym(sym)
    c = cov(sym)
    if day in (c.get("hm") or []) and not force:
        return False
    if date.fromisoformat(day).weekday() >= 5:
        return False
    w0, w1 = WINDOWS.get(window, WINDOWS["rth"])
    key = databento.key(sub)
    avail = databento.dataset_range(key)["end"]
    s_utc, e_utc = utc_iso(day, w0), utc_iso(day, w1)
    if avail and s_utc >= avail:
        return False
    if avail and e_utc > avail:
        e_utc = avail
    off = int((datetime.strptime(f"{day}T{w0}", "%Y-%m-%dT%H:%M") - datetime.strptime(s_utc, "%Y-%m-%dT%H:%M:%S")).total_seconds())
    tick, big_min = SPEC[sym]["tick"], SPEC[sym]["big"]
    mins, big, cols = {}, [], []
    cur_b, last = None, None
    lo_i, hi_i = None, None

    tv = [0, 0, 0.0, None]                             # this column's trades: bought, sold, sum price x size, last price

    def snap(bucket, rec):
        nonlocal lo_i, hi_i
        bids, asks = dbn.levels(rec)
        bi = [v for p, s_ in bids for v in (round(p / tick), s_)]
        ai = [v for p, s_ in asks for v in (round(p / tick), s_)]
        for v in bi[0::2] + ai[0::2]:
            lo_i = v if lo_i is None else min(lo_i, v)
            hi_i = v if hi_i is None else max(hi_i, v)
        vol = tv[0] + tv[1]
        # [seconds, bids, asks, bought, sold, VWAP of the column's trades (ticks), last trade (ticks)]
        cols.append([bucket * HM_STEP, bi, ai, tv[0], tv[1], round(tv[2] / vol / tick) if vol else None,
                     round(tv[3] / tick) if tv[3] is not None else None])
        tv[0], tv[1], tv[2] = 0, 0, 0.0

    r = databento.raw(key, "mbp-10", sym, s_utc, e_utc)
    try:
        for rt, rec in dbn.records(r):
            if rt != dbn.MBP10:
                continue
            sec = (dbn.ts_recv(rec) // 1_000_000_000 + off) % 86400
            b_ = sec // HM_STEP
            if b_ != cur_b:
                if last is not None:
                    snap(cur_b, last)
                cur_b = b_
            last = rec
            if rec[28] == dbn.T:
                px, sz, side = dbn.trade(rec)
                if side == "B":
                    tv[0] += sz
                elif side == "A":
                    tv[1] += sz
                tv[2] += px * sz
                tv[3] = px
                hm = f"{sec // 3600:02d}:{sec // 60 % 60:02d}"
                lv = mins.setdefault(hm, {}).setdefault(px, [0, 0])
                if side == "A":
                    lv[0] += sz
                elif side == "B":
                    lv[1] += sz
                if sz >= big_min:
                    big.append([f"{hm}:{sec % 60:02d}", px, sz, "B" if side == "B" else "S" if side == "A" else "N"])
        if last is not None:
            snap(cur_b, last)
    finally:
        r.close()
    if not cols:
        return False
    base = lo_i or 0                                   # prices as ticks from the lowest level seen
    for col in cols:
        col[1] = [v - base if k % 2 == 0 else v for k, v in enumerate(col[1])]
        col[2] = [v - base if k % 2 == 0 else v for k, v in enumerate(col[2])]
        col[5] = col[5] - base if col[5] is not None else None
        col[6] = col[6] - base if col[6] is not None else None
    store.put_json(f"{sym}/hm/{day}", {"step": HM_STEP, "tick": tick, "base": base, "window": [w0, w1], "c": cols})
    if mins:
        _save_fp(sym, day, mins, big, c)
    c["hm"] = sorted(set(c.get("hm") or []) | {day})
    db.put(c)
    return True


def heatmap(sym, day):
    return store.get_json(f"{check_sym(sym)}/hm/{day}", None, max_age=3600)


def footprint(sym, day):
    return store.get_json(f"{check_sym(sym)}/fp/{day}", None, max_age=3600)


# ---------------------------------------------------------------- loaders

def bars_between(sym, d0, d1):
    """[[t, o, h, l, c, v]] for ET calendar dates d0..d1 (inclusive)."""
    out, ym = [], d0[:7]
    while ym <= d1[:7]:
        out += [b for b in load_month(sym, ym) if d0 <= b[0][:10] <= d1]
        ym = _next_month(ym)
    return out


def daily(sym):
    """[[day, o, h, l, c, v]] regular-hours daily candles of every stored session."""
    c = cov(sym)
    return [d for m in sorted(c.get("months") or {}) for d in (c["months"][m].get("d") or [])]


def trading_days(sym):
    return [d[0] for d in daily(sym)]


def sessions(sym, n=None, end=None, rth=True, start=None):
    """{day: [[t, o, h, l, c, v]]} of the last n sessions up to `end` (or from `start`). rth: 09:30-16:00 only,
    else the whole Globex session (18:00 the evening before to 17:00)."""
    days = [d for d in trading_days(sym) if (not end or d <= end) and (not start or d >= start)]
    if n:
        days = days[-n:]
    if not days:
        return {}
    d0 = (date.fromisoformat(days[0]) - timedelta(days=3)).isoformat()
    want = set(days)
    out = {}
    for b in bars_between(sym, d0, days[-1]):
        if rth:
            if RTH0 <= b[0][11:16] < RTH1 and b[0][:10] in want:
                out.setdefault(b[0][:10], []).append(b)
        else:
            s = session_of(b[0])
            if s in want:
                out.setdefault(s, []).append(b)
    return out


def agg(bars, tf):
    """1-minute bars -> tf-minute bars (buckets from midnight ET: 09:30 starts a 5 / 15-minute bar; hourly at :30
    in regular hours)."""
    if tf == 1:
        return bars
    out, key = [], None
    for t, o, h, l, c, v in bars:
        m = int(t[11:13]) * 60 + int(t[14:16])
        b = m - ((m - 570) % tf if tf == 60 else m % tf)
        k = t[:11] + f"{b // 60 % 24:02d}:{b % 60:02d}"
        if k != key:
            out.append([k, o, h, l, c, v])
            key = k
        else:
            x = out[-1]
            x[2], x[3], x[4], x[5] = max(x[2], h), min(x[3], l), c, x[5] + v
    return out


def rth(sym, days, tf=1, end=None, start=None):
    """For the strategy engines: {day: [(t, o, h, l, c)]} regular hours, tf-minute candles."""
    by = sessions(sym, None if start else days, end=end, rth=True, start=start)
    return {d: [tuple(b[:5]) for b in agg(xs, tf)] for d, xs in by.items()}


# ---------------------------------------------------------------- market conditions

def _sma(xs, n):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def cells(sym):
    """{day: "Bull|calm"}: the market condition of each session, known before its open (from the closes before it).
    Phase: close vs its 50-day average and that average's 10-day slope (Bull / Bear, else Sideways).
    Volatility: 14-day average range as % of the close vs its median of the year before (calm / volatile)."""
    D = daily(sym)
    if not D:
        return {}
    cl = [x[4] for x in D]
    s50 = _sma(cl, 50)
    rngp = []
    for i, x in enumerate(D):
        pc = D[i - 1][4] if i else x[1]
        rngp.append(max(x[2] - x[3], abs(x[2] - pc), abs(x[3] - pc)) / x[4] * 100)
    a14 = _sma(rngp, 14)
    out = {}
    for i in range(1, len(D)):
        j = i - 1                                      # yesterday's close: known before today's open
        if s50[j] is None or a14[j] is None:
            continue
        slope = s50[j] - s50[j - 10] if j >= 10 and s50[j - 10] is not None else 0
        ph = "Bull" if cl[j] > s50[j] and slope > 0 else "Bear" if cl[j] < s50[j] and slope < 0 else "Sideways"
        hist = sorted(x for x in a14[max(0, j - 252):j] if x is not None)
        med = hist[len(hist) // 2] if hist else a14[j]
        out[D[i][0]] = f"{ph}|{'volatile' if a14[j] > med else 'calm'}"
    if D:                                              # the next session (not stored yet): from the last close
        j = len(D) - 1
        if s50[j] is not None and a14[j] is not None:
            slope = s50[j] - s50[j - 10] if j >= 10 and s50[j - 10] is not None else 0
            ph = "Bull" if cl[j] > s50[j] and slope > 0 else "Bear" if cl[j] < s50[j] and slope < 0 else "Sideways"
            hist = sorted(x for x in a14[max(0, j - 252):j] if x is not None)
            med = hist[len(hist) // 2] if hist else a14[j]
            out["next"] = f"{ph}|{'volatile' if a14[j] > med else 'calm'}"
    return out


# ---------------------------------------------------------------- views

def _levels(sym, days):
    """{day: {pdh, pdl, pdc, onh, onl, open}} for the sessions."""
    D = {d[0]: d for d in daily(sym)}
    order = sorted(D)
    out = {}
    for day, xs in days.items():
        lv = {}
        k = order.index(day) if day in D else -1
        if k > 0:
            p = D[order[k - 1]]
            lv.update(pdh=p[2], pdl=p[3], pdc=p[4])
        on = [b for b in xs if not (b[0][:10] == day and b[0][11:16] >= RTH0)]
        if on:
            lv.update(onh=max(b[2] for b in on), onl=min(b[3] for b in on))
        r = [b for b in xs if b[0][:10] == day and RTH0 <= b[0][11:16] < RTH1]
        if r:
            lv["open"] = r[0][1]
        out[day] = lv
    return out


def sd_zones(sessions_bars, tf):
    """Supply / demand zones, drawn fresh for every session from that session's own candles (the overnight first, then
    the day): a base of 1-3 small candles (each under 0.7 x the 14-candle average range) right before an impulse candle
    (range >= 2 x the average, body >= 60% of it). Demand before a move up, supply before a move down. A zone lasts
    until a candle closes through its far side, or the session ends: the next session starts from a clean chart.
    sessions_bars: {day: [[t, o, h, l, c, v], ...]} (whole Globex sessions, 1-minute)."""
    out, a = [], None
    for day in sorted(sessions_bars):
        B = agg(sessions_bars[day], tf)
        atr = []
        for i, b in enumerate(B):
            pc = B[i - 1][4] if i else b[1]
            tr = max(b[2] - b[3], abs(b[2] - pc), abs(b[3] - pc))
            a = tr if a is None else (a * 13 + tr) / 14   # the average range carries over from the previous session
            atr.append(a)
        for i in range(1, len(B)):
            at = atr[i - 1]
            _, o, h, l, c, _ = B[i]
            r = h - l
            if not (r >= 2 * at and abs(c - o) >= 0.6 * r):
                continue
            k, lo, hi, cnt = i - 1, float("inf"), float("-inf"), 0
            while k >= 0 and cnt < 3 and B[k][2] - B[k][3] <= 0.7 * at:
                lo, hi, k, cnt = min(lo, B[k][3]), max(hi, B[k][2]), k - 1, cnt + 1
            if not cnt:
                continue
            up = c > o
            end, broken = B[-1][0], False
            for j in range(i + 1, len(B)):
                if (up and B[j][4] < lo) or (not up and B[j][4] > hi):
                    end, broken = B[j][0], True
                    break
            out.append({"k": "demand" if up else "supply", "lo": lo, "hi": hi, "t0": B[k + 1][0], "t1": end, "day": day,
                        "broken": broken, "on": B[k + 1][0][:10] < day or B[k + 1][0][11:16] < RTH0})
    return out


def chart(sym, tf=5, days=3, end=None, rth_only=False):
    sym = check_sym(sym)
    tf = tf if tf in (1, 5, 15, 60) else 5
    cap = {1: 10, 5: 30, 15: 60, 60: 180}[tf]                 # keeps the answer a few MB at most
    n = max(1, min(days, cap))
    full = sessions(sym, n, end=end, rth=False)
    by = {d: [b for b in xs if b[0][:10] == d and RTH0 <= b[0][11:16] < RTH1] for d, xs in full.items()} if rth_only else full
    bars = []
    for d in sorted(by):
        bars += [b + [d] for b in agg(by[d], tf)]
    zones = sd_zones(full, tf)
    fp = set(cov(sym).get("fp") or [])
    hm = set(cov(sym).get("hm") or [])
    return {"sym": sym, "tf": tf, "spec": SPEC[sym], "bars": bars, "days": sorted(by), "levels": _levels(sym, by),
            "fpDays": sorted(d for d in by if d in fp), "hmDays": sorted(d for d in by if d in hm), "cells": {d: c for d, c in cells(sym).items() if d in by},
            "zones": zones}


def latest(sym):
    """The market tile: last price, change vs the previous regular-hours close, session high / low, VWAP, overnight
    high / low, today's condition."""
    D = daily(sym)
    last_t = last_bar_time(sym)
    if not last_t:
        return {"sym": sym, "name": SPEC[sym]["name"], "empty": True}
    day = session_of(last_t)
    xs = sessions(sym, 1, end=day, rth=False).get(day) or []
    if not xs:
        xs = bars_between(sym, last_t[:10], last_t[:10])
    last = xs[-1] if xs else None
    prev = [d for d in D if d[0] < day]
    pc = prev[-1][4] if prev else None
    pv = sum((b[2] + b[3] + b[4]) / 3 * b[5] for b in xs)
    vv = sum(b[5] for b in xs)
    on = [b for b in xs if not (b[0][:10] == day and b[0][11:16] >= RTH0)]
    cl = cells(sym)
    return {"sym": sym, "name": SPEC[sym]["name"], "session": day, "t": last[0] if last else None, "last": last[4] if last else None,
            "prevClose": pc, "chg": (last[4] - pc) if last and pc else None, "high": max(b[2] for b in xs) if xs else None,
            "low": min(b[3] for b in xs) if xs else None, "vwap": pv / vv if vv else None,
            "onh": max(b[2] for b in on) if on else None, "onl": min(b[3] for b in on) if on else None,
            "cell": cl.get(day) or cl.get("next"), "spark": [b[4] for b in agg(xs, 15)][-60:]}
