"""Day trading on 1-minute candles (QQQ / SPY by default), on a SECOND Alpaca paper account.

Same idea as the Strategy lab: indicators with their settings, and ON/OFF entry and exit rules. Every rule that is ON
must pass on the entry candle (long, or the mirror for short). Entry at the close of the 1-minute candle; one trade at
a time per ticker, at most maxTrades a day, no entry outside startT-endT, everything flat at flatT.

Indicators (regular session candles, continuous across days): session VWAP (from 09:30), EMA fast / slow, Keltner
channel, TTM squeeze, inertia (linear regression of the closes, 3 lengths), fair value gaps, opening range (first
orbMin minutes), pre-market high / low (04:00-09:30 candles), gamma walls of the day (put wall, flip, call wall).

Exits: stop (stopAtr x 1-min ATR), target (targetR x risk), trailing stop (trailAtr x ATR from the best price),
close back through the VWAP / the fast EMA, squeeze momentum turning, inertia no longer with the trade, a new FVG
against the trade, and the time exit (flatT).

Live (every minute, job "daytick"): today's candles are replayed with the current settings; a trade that the engine
opens on the last finished candle is bought / sold short at market on the day-trading account, and closed when the
engine closes it (or at flatT). Items: DTCFG (settings), DTPOS#<ticker>#<entry time> (live trades), BROKER#alpacaday.
"""
import math
import re
import time
from datetime import datetime, timedelta

import db
from util import BadRequest, iso, now_ny

SK = "DTCFG"
CRED_SK = "BROKER#alpacaday"
PARAMS = {"enabled": False, "trade": False, "tickers": ["QQQ", "SPY"], "longs": True, "shorts": True,
          "startT": "09:35", "endT": "15:30", "flatT": "15:55", "maxTrades": 3, "qty": 10, "riskUsd": 0, "maxQty": 500,
          # indicators
          "emaLen": 9, "ema2Len": 21, "kcLen": 20, "kcMult": 2.0, "sqLen": 20, "sqBB": 2.0, "sqKC": 1.5, "sqFireN": 3,
          "inN1": 70, "inN2": 40, "inN3": 20, "fvgMin": 0.3, "fvgNear": 0.25, "fvgDays": 1, "orbMin": 15, "zoneAtr": 1.0,
          # entry rules
          "aboveVwap": True, "vwapCross": False, "aboveEma": False, "emaStack": True, "greenBar": True, "kcNotExt": False,
          "kcLower": False, "sqOn": False, "sqFire": False, "sqMom": False, "inGreen": False, "inNotRed": False,
          "fvgTouch": False, "orbBreak": False, "pmBreak": False, "pwTouch": False, "aboveFlip": False,
          "wallNear": False,      # gamma walls of the nearest expiration only (0DTE on SPY / QQQ) instead of all
          # entry rules from the chart studies
          "bandForm": False, "lisForm": False, "lisAbove": False, "pmzTouch": False, "fibTouch": False, "dpTouch": False,
          "prevTouch": False, "sqTurn": False, "sqTurnTf": 1, "formWin": 5,
          # day filters
          "gapOn": False, "gapMin": 0.25, "gapMax": 99.0, "gapWith": False, "gapFade": False, "flipOpen": False, "flipFade": False, "flipPrev": False, "cvdSide": False, "cvdReal": False, "pwSide": False,
          "peerAgree": False, "rvolOn": False, "rvolMin": 1.5, "vixOn": False, "vixMin": 0.0, "vixMax": 25.0,
          "noLunch": False, "lunchFrom": "11:30", "lunchTo": "13:30", "dayStop": False, "dayStopR": 2.0,
          # formula rules (formula.py): an extra condition per side, written as an expression ("" = off)
          "fxLong": "", "fxShort": "", "fxExitLong": "", "fxExitShort": "",
          "barMin": 1,            # candle size the strategy runs on: 1 or 5 minutes (the 1-minute candles grouped)
          # exits
          "stopBase": "atr1",     # what the stop / trailing sizes multiply: atr1 / atr5 (5-min ATR) / atrD (daily ATR) / pct / avgN
          "volN": 300,            # avgN: the average move (true range) of the last N 1-minute candles
          "swingN": 5, "swingBuf": 0.05,   # swing: stop under the previous swing low (short: above the swing high) - buf %
          "stopOn": True, "stopAtr": 1.5, "targetOn": True, "targetR": 2.0, "trailOn": False, "trailAtr": 2.0,
          "vwapExit": False, "emaExit": False, "sqExit": False, "inExit": False, "fvgExit": False,
          "beOn": False, "beR": 1.0, "scaleOn": False, "scaleR": 1.0, "timeOn": False, "timeMin": 30, "lvlTarget": False}
ENTRY = ("aboveVwap", "vwapCross", "aboveEma", "emaStack", "greenBar", "kcNotExt", "kcLower", "sqOn", "sqFire", "sqMom",
         "inGreen", "inNotRed", "fvgTouch", "orbBreak", "pmBreak", "pwTouch", "aboveFlip",
         "bandForm", "lisForm", "lisAbove", "pmzTouch", "fibTouch", "dpTouch", "prevTouch", "sqTurn")
FILTERS = ("gapOn", "gapWith", "gapFade", "flipOpen", "flipFade", "cvdSide", "pwSide", "peerAgree", "rvolOn", "vixOn", "noLunch")
EXITS = ("stopOn", "targetOn", "trailOn", "vwapExit", "emaExit", "sqExit", "inExit", "fvgExit", "beOn", "scaleOn", "timeOn",
         "lvlTarget", "dayStop")
INTS = {"maxTrades": (1, 50), "qty": (1, 100000), "maxQty": (1, 100000), "emaLen": (2, 400), "ema2Len": (2, 400),
        "kcLen": (2, 400), "sqLen": (5, 400), "sqFireN": (1, 60), "inN1": (5, 800), "inN2": (5, 800), "inN3": (5, 800),
        "fvgDays": (1, 20), "orbMin": (1, 120), "formWin": (1, 120), "timeMin": (1, 400), "volN": (20, 3000), "swingN": (1, 60), "sqTurnTf": (1, 30), "barMin": (1, 15)}
FLOATS = {"kcMult": (0.1, 10), "sqBB": (0.1, 10), "sqKC": (0.1, 10), "fvgMin": (0, 10), "fvgNear": (0, 10), "zoneAtr": (0, 20),
          "stopAtr": (0.05, 50), "targetR": (0.1, 50), "trailAtr": (0.05, 50), "riskUsd": (0, 1e7),
          "gapMin": (0, 99), "gapMax": (0, 99), "rvolMin": (0, 50), "vixMin": (0, 200), "vixMax": (0, 200), "dayStopR": (0.1, 50),
          "beR": (0.1, 20), "scaleR": (0.1, 20), "swingBuf": (0, 5)}
TIMES = ("startT", "endT", "flatT", "lunchFrom", "lunchTo")
STOP_BASES = ("atr1", "atr5", "atrD", "pct", "avgN", "swing")


def config(sub):
    c = db.get(db.upk(sub), SK) or {}
    out = {k: c.get(k, v) for k, v in PARAMS.items()}
    for k in INTS:
        out[k] = int(out[k])
    for k in FLOATS:
        out[k] = float(out[k])
    return out


def _hm(v, d):
    v = str(v or "").strip()
    try:
        h, m = v.split(":")
        h, m = int(h), int(m)
        if 4 <= h <= 20 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except ValueError:
        pass
    return d


def save_config(sub, body):
    cur = clean_cfg(config(sub), body)
    from decimal import Decimal
    db.put({"PK": db.upk(sub), "SK": SK, **{k: Decimal(str(v)) if isinstance(v, float) else v for k, v in cur.items()}})
    return cur


FORMULAS = ("fxLong", "fxShort", "fxExitLong", "fxExitShort")


MX_NAMES = ("callPrem", "putPrem", "netPrem", "netPremDay", "sweepCall", "sweepPut", "gex", "breadth", "sectorBreadth",
            "megaBreadth", "vixy", "vixyChg")
MX_UW = ("callPrem", "putPrem", "netPrem", "netPremDay", "sweepCall", "sweepPut", "gex")


def market_ctx(sym, dates):
    """Options flow / gamma / breadth / VIXY / VIX term (marketctx.py) and the intraday wall snapshots for formulas."""
    import marketctx
    try:
        return {"mx": marketctx.for_symbol(sym, dates), "snaps": marketctx.snaps_map(sym)}
    except Exception as e:
        print("market context failed", sym, e)
        return {}


def agg_bars(bars, n):
    """1-minute candles -> n-minute candles (t = the first minute; pre-market grouped too). The last group is dropped
    when it isn't complete yet (live)."""
    if n <= 1:
        return bars
    out, key = [], None
    for b in bars:
        mm = int(b["t"][11:13]) * 60 + int(b["t"][14:16])
        base = mm - (mm - 570) % n if mm >= 570 else mm - mm % n
        k = (b["t"][:10], base)
        if k != key:
            key = k
            out.append({"t": f"{b['t'][:11]}{base // 60:02d}:{base % 60:02d}", "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                        "v": float(b.get("v") or 0), "_last": mm, "_base": base})
        else:
            x = out[-1]
            x["h"], x["l"], x["c"], x["v"], x["_last"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"], x["v"] + float(b.get("v") or 0), mm
    if out and out[-1]["_last"] < out[-1]["_base"] + n - 1 and out[-1]["_last"] < 959:
        out.pop()                                      # still forming
    for x in out:
        x.pop("_last", None)
        x.pop("_base", None)
    return out


def fx_series(bars, I, walls, ctx, P):
    """{key: [bool per candle]} of the formula rules that are set (computed once per formula and candles)."""
    import formula
    out = {k: None for k in FORMULAS}
    if not any(P.get(k) for k in FORMULAS):
        return out
    cache = (ctx or {}).get("cache")
    ck = ("fxctx", bool(P.get("flipPrev")), int(P.get("barMin") or 1))
    cx = cache.get(ck) if cache is not None else None
    if cx is None:
        c_ = ctx or {}
        cx = formula.Ctx(bars, True, walls, I.get("pm") or {}, c_.get("dp") or {}, c_.get("vix") or {},
                         c_.get("peer") or {}, I.get("vwap"), interval=int(P.get("barMin") or 1), mx=c_.get("mx"),
                         snaps=c_.get("snaps"), pm_last=I.get("pmLast"))
        if cache is not None:
            cache[ck] = cx
    for k in FORMULAS:
        if P.get(k):
            out[k] = formula.evaluate(P[k], cx, formula.UNAVAILABLE["day"])
    return out


def clean_cfg(cur, body):
    """cur (a full config) updated with the valid values of body."""
    cur = dict(cur)
    for k, (lo, hi) in INTS.items():
        if k in body:
            try:
                cur[k] = int(max(lo, min(hi, float(body[k]))))
            except (TypeError, ValueError):
                pass
    for k, (lo, hi) in FLOATS.items():
        if k in body:
            try:
                cur[k] = max(lo, min(hi, round(float(body[k]), 4)))
            except (TypeError, ValueError):
                pass
    for k in ("enabled", "trade", "longs", "shorts", "wallNear", "flipPrev", "cvdReal") + ENTRY + FILTERS + EXITS:
        if k in body:
            cur[k] = bool(body[k])
    for k in TIMES:
        if k in body:
            cur[k] = _hm(body[k], cur[k])
    if body.get("stopBase") in STOP_BASES:
        cur["stopBase"] = body["stopBase"]
    for k in FORMULAS:
        if k in body:
            import formula
            v = str(body[k] or "").strip()
            if v:
                try:
                    formula.check(v, formula.UNAVAILABLE["day"])
                except formula.FormulaError as e:
                    raise BadRequest(f"Formula {k}: {e}")
            cur[k] = v
    if "tickers" in body:
        tk = body["tickers"]
        tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
        cur["tickers"] = [str(t).strip().upper()[:10] for t in (tk or []) if str(t).strip()][:10] or ["QQQ", "SPY"]
    return cur


def full_cfg(c):
    """A stored config / preset -> every parameter with its type."""
    out = {k: (c or {}).get(k, v) for k, v in PARAMS.items()}
    for k in INTS:
        out[k] = int(out[k])
    for k in FLOATS:
        out[k] = float(out[k])
    return out


# ---------------- data ----------------

def creds(sub):
    """The day-trading account (falls back to the main Alpaca keys for market data only)."""
    import alpaca
    return alpaca.creds(sub, CRED_SK)


def data_creds(sub):
    import alpaca
    return creds(sub) or alpaca.creds(sub)


def minute_bars(sub, sym, days, now=None, feed=None):
    """1-minute candles of the last `days` trading days (pre-market included), NY time. [{t,o,h,l,c,v}]"""
    import alpaca
    c = data_creds(sub)
    if not c:
        raise BadRequest("Connect an Alpaca account (Day trading → account) to load minute candles.")
    now = now or now_ny().replace(tzinfo=None)
    start = (now - timedelta(days=int(days * 1.5) + 4)).replace(hour=4, minute=0, second=0, microsecond=0)
    fd = feed or c.get("feed", "iex")
    end = now - timedelta(minutes=1) if fd == "iex" else now - timedelta(minutes=16)   # SIP: not the last 15 minutes
    pages = max(10, int(days * 1000 / 10000) + 3)
    out = alpaca.bars(c, sym, start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S"), 1, max_pages=pages, feed=fd)
    out = [b for b in out if "04:00" <= b["t"][11:16] < "16:00"]         # after-hours not used
    for b in out:
        b["t"] = b["t"][:16]
    return out


def walls_by_day(sub, sym, dates, near=None, budget_s=240):
    """{date: {callWall, putWall, flip}}: all expirations (stored days), or the nearest expiration only (near=True,
    or the wallNear setting when near is None; missing days are fetched)."""
    import flowdata
    if near is None:
        try:
            near = bool(config(sub).get("wallNear"))
        except Exception:
            near = False
    if near:
        try:
            return flowdata.near_walls(sub, sym, list(dates), budget_s)
        except Exception as e:
            print("near walls failed", sym, e)
            return {}
    w = {}
    try:
        st = flowdata.stored_walls(sym)
        w = {d: st.get(d) for d in dates if st.get(d)}
    except Exception as e:
        print("day walls failed", sym, e)
    today = now_ny().strftime("%Y-%m-%d")
    todo = [d for d in dates if d not in w]
    if todo:                                           # days not stored yet: fetched from Unusual Whales (kept for good)
        import time as _t
        from concurrent.futures import ThreadPoolExecutor
        t0 = _t.time()
        try:
            key = flowdata._key(sub)
        except Exception:
            key = None
        if key:
            with ThreadPoolExecutor(max_workers=4) as ex:
                for i in range(0, len(todo), 8):
                    if _t.time() - t0 > budget_s:
                        break
                    futs = {d: ex.submit(flowdata.gex_day, key, sym, d, True) for d in todo[i:i + 8]}
                    for d, f in futs.items():
                        try:
                            g = f.result()
                            if g:
                                w[d] = {k: g.get(k) for k in ("callWall", "putWall", "flip")}
                        except Exception as e:
                            print("day walls failed", sym, d, e)
    return w


# ---------------- indicators ----------------

def _session_vwap(bars):
    out, pv, vv, day = [], 0.0, 0.0, None
    for b in bars:
        if b["t"][:10] != day:
            day, pv, vv = b["t"][:10], 0.0, 0.0
        v = float(b.get("v") or 0) or 1.0
        pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        vv += v
        out.append(pv / vv)
    return out


def _fvg_scan(bars, atr, min_atr, near, max_days):
    """Per candle: an open bullish gap it trades into (bull) / an open bearish gap it trades into (bear), and whether
    it completes a new bullish / bearish gap. Active gaps only (filled / expired ones dropped as we go)."""
    n = len(bars)
    bull_t, bear_t, new_bull, new_bear = [None] * n, [None] * n, [False] * n, [False] * n
    act = []                                           # [type, lo, hi, i, day count, last day]
    for j in range(n):
        b = bars[j]
        m = near * (atr[j] or 0)
        keep = []
        for z in act:
            if b["t"][:10] != z[5]:
                z[4] += 1
                z[5] = b["t"][:10]
            if z[4] > max_days:
                continue
            if z[0] == "bull":
                if b["l"] < z[1]:
                    continue                           # filled
                if bull_t[j] is None and b["l"] <= z[2] + m and b["c"] > z[1]:
                    bull_t[j] = (z[1], z[2])
            else:
                if b["h"] > z[2]:
                    continue
                if bear_t[j] is None and b["h"] >= z[1] - m and b["c"] < z[2]:
                    bear_t[j] = (z[1], z[2])
            keep.append(z)
        act = keep
        if j >= 2:
            a, c = bars[j - 2], b
            mm = (atr[j] or 0) * min_atr
            if c["l"] > a["h"] and c["l"] - a["h"] >= mm:
                new_bull[j] = True
                act.append(["bull", a["h"], c["l"], j, 0, b["t"][:10]])
            elif c["h"] < a["l"] and a["l"] - c["h"] >= mm:
                new_bear[j] = True
                act.append(["bear", c["h"], a["l"], j, 0, b["t"][:10]])
        if len(act) > 300:
            act = act[-300:]
    return bull_t, bear_t, new_bull, new_bear


def _cvd_est(bars):
    """Cumulative volume delta from each day's open, estimated from the candles: the volume split by where each candle
    closes in its range (close at the high = all buying)."""
    out, cum, day = [], 0.0, None
    for i, b in enumerate(bars):
        if b["t"][:10] != day:
            day, cum = b["t"][:10], 0.0
        v, r = float(b.get("v") or 0), b["h"] - b["l"]
        pc = bars[i - 1]["c"] if i and bars[i - 1]["t"][:10] == day else b["o"]
        buy = v * (b["c"] - b["l"]) / r if r > 0 else (v if b["c"] > pc else 0 if b["c"] < pc else v / 2)
        cum += 2 * buy - v
        out.append(cum)
    return out


def real_cvd(sym, dates):
    """{minute: cumulative delta from the open} from the stored real order flow (orderflow.py), days that are loaded."""
    import orderflow
    out = {}
    for d in dates:
        x = orderflow.cached(sym, d)
        if not x or not x.get("done"):
            continue
        cum = 0.0
        for m in sorted(x.get("min") or {}):
            b, s_ = x["min"][m]
            cum += b - s_
            out[m] = cum
    return out


def _atr5(bars):
    """Per 1-minute candle: the 14-period ATR of the 5-minute candles finished before it (the current one included
    as it forms)."""
    import wallwatch as ww
    out, cur, b5, keys = [], None, [], []
    for b in bars:
        hm = int(b["t"][11:13]) * 60 + int(b["t"][14:16])
        k = b["t"][:11] + str(hm // 5)
        if k != cur:
            cur = k
            b5.append({"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
        else:
            x = b5[-1]
            x["h"], x["l"], x["c"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"]
        keys.append(len(b5) - 1)
    a5 = ww.atr14(b5)
    return [a5[k] for k in keys]


def _avg_move(bars, n):
    """Per candle: the average true range of the last n 1-minute candles (across sessions): the current volatility,
    steadier than a 14-candle ATR."""
    out, tot, trs = [], 0.0, []
    for i, b in enumerate(bars):
        pc = bars[i - 1]["c"] if i else b["o"]
        tr = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
        trs.append(tr)
        tot += tr
        if len(trs) > n:
            tot -= trs[-n - 1]
        out.append(tot / min(len(trs), n))
    return out


def _sq_turns(sq):
    """Per candle: +1 = squeeze momentum below 0 bottoms (turn up), -1 = above 0 peaks (turn down), 0 = none. As the
    chart's squeeze-turn arrows, known at the close of the candle."""
    out = [0] * len(sq)
    for i in range(2, len(sq)):
        if not (sq[i] and sq[i - 1] and sq[i - 2]):
            continue
        a, b, c = sq[i - 2]["mom"], sq[i - 1]["mom"], sq[i]["mom"]
        if c < 0 and c > b and a > b:
            out[i] = 1
        elif c > 0 and c < b and a < b:
            out[i] = -1
    return out


def _sq_turns_tf(bars, sq, P):
    """Squeeze turns on 1-minute candles, or on m-minute candles (sqTurnTf = m, as the 5m chart shows them): a turn
    of an m-minute candle counts at its last minute (when that candle is complete)."""
    import wallwatch as ww
    m = int(P.get("sqTurnTf") or 1)
    if m <= 1:
        return _sq_turns(sq)
    groups, key = [], None
    for i, b in enumerate(bars):
        hm = int(b["t"][11:13]) * 60 + int(b["t"][14:16])
        k = b["t"][:10] + str(570 + (hm - 570) // m * m)
        if k != key:
            key = k
            groups.append({"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v") or 0, "last": i})
        else:
            g = groups[-1]
            g.update(h=max(g["h"], b["h"]), l=min(g["l"], b["l"]), c=b["c"], last=i)
    sqm = ww.squeeze(groups, int(P["sqLen"]), float(P["sqBB"]), float(P["sqKC"]), int(P["sqFireN"]))
    tm = _sq_turns(sqm)
    out = [0] * len(bars)
    for g, t in zip(groups, tm):
        if t:
            out[g["last"]] = t
    return out


def _pivots(bars, k):
    """Swing lows / highs: a candle whose low is under the k candles before and not above the k after (high: the
    mirror). Known only k candles later: [(index known from, price)] in order."""
    n, lo, hi = len(bars), [], []
    for j in range(k, n - k):
        l, h = bars[j]["l"], bars[j]["h"]
        if all(l < bars[x]["l"] for x in range(j - k, j)) and all(l <= bars[x]["l"] for x in range(j + 1, j + k + 1)):
            lo.append((j + k, l))
        if all(h > bars[x]["h"] for x in range(j - k, j)) and all(h >= bars[x]["h"] for x in range(j + 1, j + k + 1)):
            hi.append((j + k, h))
    return lo, hi


def swing_stop(I, i, price, side, buf_pct, max_back=60):
    """The stop under the most recent swing low already known at candle i and below the price (short: above the most
    recent swing high above it), minus buf_pct % of the price. None if there is none."""
    import bisect
    piv = I["pivLo"] if side > 0 else I["pivHi"]
    k = bisect.bisect_right(piv, (i, float("inf"))) - 1
    for j in range(k, max(-1, k - max_back), -1):
        p = piv[j][1]
        if (p < price) if side > 0 else (p > price):
            return round(p - side * price * buf_pct / 100, 4)
    return None


def _atr_day(bars, n=14):
    """Per candle: the average daily range (true range) of the n sessions before its day."""
    days, order = {}, []
    for b in bars:
        d = b["t"][:10]
        if d not in days:
            days[d] = [b["h"], b["l"], b["c"]]
            order.append(d)
        x = days[d]
        x[0], x[1], x[2] = max(x[0], b["h"]), min(x[1], b["l"]), b["c"]
    trs, by, pc = [], {}, None
    for d in order:
        h, l, c = days[d]
        by[d] = sum(trs[-n:]) / len(trs[-n:]) if trs else None
        trs.append(max(h - l, abs(h - pc), abs(l - pc)) if pc is not None else h - l)
        pc = c
    return [by[b["t"][:10]] for b in bars]


def stop_unit(I, i, price, P):
    """What the stop and trailing sizes multiply at candle i: the 1-minute ATR, the 5-minute ATR, the daily ATR, or
    1% of the price."""
    base = P.get("stopBase") or "atr1"
    if base == "pct":
        return price / 100
    if base == "avgN":
        return I["avgN"][i] or I["atr"][i] or 0
    if base == "atr5":
        return I["atr5"][i] or I["atr"][i] or 0
    if base == "atrD":
        return I["atrD"][i] or (I["atr5"][i] or 0) * 6
    return I["atr"][i] or 0


def _rvol(bars, look=10):
    """Relative volume: today's volume so far vs the average volume at the same minute of the session over the
    previous `look` days (cumulative). None for the first days."""
    out, hist, cur, day, k = [], [], [], None, 0
    for b in bars:
        d = b["t"][:10]
        if d != day:
            if cur:
                hist.append(cur)
                hist = hist[-look:]
            day, cur, k = d, [], 0
        cur.append((cur[-1] if cur else 0) + float(b.get("v") or 0))
        ref = [h[min(k, len(h) - 1)] for h in hist if h]
        avg = sum(ref) / len(ref) if len(ref) >= 3 else 0
        out.append(round(cur[-1] / avg, 3) if avg > 0 else None)
        k += 1
    return out


def studies(bars, ext, P):
    """What the chart studies give the engine, known at each candle: per day the previous session high / low / close,
    the open and the gap, the auto fibs (previous day), the pre-market zone, the line in the sand; per candle the
    direction of a recent EMA-band formation / LIS formation (within formWin minutes, the LIS one once confirmed)."""
    import tr3ndy
    idx = {b["t"]: i for i, b in enumerate(bars)}
    days, prev = {}, None
    for b in bars:
        d = b["t"][:10]
        if d not in days:
            days[d] = {"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
        x = days[d]
        x["h"], x["l"], x["c"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"]
    info = {}
    for d in sorted(days):
        x = days[d]
        info[d] = {"open": x["o"], "prev": prev, "gap": round((x["o"] / prev["c"] - 1) * 100, 3) if prev else None}
        prev = x
    fb = tr3ndy.fibs(bars)
    b5 = tr3ndy.agg(ext, 5)
    lines, lisf, _ = tr3ndy.lis(b5)
    pz = tr3ndy.pmz(b5, lines)
    win = int(P.get("formWin") or 5)
    band = [0] * len(bars)
    _, bf = tr3ndy.bands(bars)
    for f in bf:
        j = idx.get(f["t"])
        if j is not None:
            for k in range(j, min(len(bars), j + win)):
                if bars[k]["t"][:10] == f["t"][:10]:
                    band[k] = f["dir"]
    lisd = [0] * len(bars)
    for f in lisf:
        t = datetime.strptime(f["t"], "%Y-%m-%dT%H:%M") + timedelta(minutes=9)     # confirmed at the close of the next 5-min candle
        j = idx.get(t.strftime("%Y-%m-%dT%H:%M"))
        if j is not None:
            for k in range(j, min(len(bars), j + win)):
                if bars[k]["t"][:10] == f["t"][:10]:
                    lisd[k] = f["dir"]
    return {"info": info, "fibs": {d: [v for _, v in x] for d, x in fb.items()}, "pmz": pz, "lis": lines, "band": band, "lisF": lisd}


def _test(b, L, zone, long_):
    return (b["l"] <= L + zone and b["c"] > L) if long_ else (b["h"] >= L - zone and b["c"] < L)


def indicators(bars, ext, P):
    """bars: regular-session 1-minute candles; ext: all candles (pre-market for the pre-market high / low)."""
    import wallwatch as ww
    cl = [b["c"] for b in bars]
    atr = ww.atr14(bars)
    pm, pm_last = {}, {}
    for b in ext:
        if "04:00" <= b["t"][11:16] < "09:30":
            d = b["t"][:10]
            h, l = pm.get(d, (None, None))
            pm[d] = (max(h, b["h"]) if h is not None else b["h"], min(l, b["l"]) if l is not None else b["l"])
            pm_last[d] = b["c"]
    orb, cur, day = [], None, None
    om = int(P["orbMin"])
    end_m = 9 * 60 + 30 + om
    for b in bars:
        d, hm = b["t"][:10], int(b["t"][11:13]) * 60 + int(b["t"][14:16])
        if d != day:
            day, cur, hi_, lo_ = d, None, b["h"], b["l"]
        if hm < end_m:
            hi_, lo_ = max(hi_, b["h"]), min(lo_, b["l"])
            orb.append(None)
            if hm == end_m - 1:
                cur = (hi_, lo_)
        else:
            if cur is None:
                cur = (hi_, lo_)
            orb.append(cur)
    bt, brt, nb, nbr = _fvg_scan(bars, atr, float(P["fvgMin"]), float(P["fvgNear"]), int(P["fvgDays"]))
    lo_p, hi_p = _pivots(bars, int(P.get("swingN") or 5))
    sq = ww.squeeze(bars, int(P["sqLen"]), float(P["sqBB"]), float(P["sqKC"]), int(P["sqFireN"]))
    return {"cvdE": _cvd_est(bars), "pivLo": lo_p, "pivHi": hi_p, "avgN": _avg_move(bars, int(P.get("volN") or 300)), "atr5": _atr5(bars), "atrD": _atr_day(bars), "rvol": _rvol(bars), "vwap": _session_vwap(bars), "ema": ww._ema_series(cl, int(P["emaLen"])), "ema2": ww._ema_series(cl, int(P["ema2Len"])),
            "kc": ww.keltner(bars, int(P["kcLen"]), float(P["kcMult"])),
            "sq": sq, "sqTurnD": _sq_turns_tf(bars, sq, P), "tt": [b["t"] for b in bars],
            "inr": ww.inertia_hourly(bars, (int(P["inN1"]), int(P["inN2"]), int(P["inN3"]))),
            "atr": atr, "pm": pm, "pmLast": pm_last, "orb": orb, "fvgBull": bt, "fvgBear": brt, "newBull": nb, "newBear": nbr}


# ---------------- engine ----------------

def _checks(i, b, prev, I, w, side, P):
    """{rule: passes} for the candle i, long (side 1) or short (side -1)."""
    long_ = side > 0
    vw, em, em2, kc, sq, ir, a = I["vwap"][i], I["ema"][i], I["ema2"][i], I["kc"][i], I["sq"][i], I["inr"][i], I["atr"][i] or 0
    pm = I["pm"].get(b["t"][:10])
    orb = I["orb"][i]
    pc = prev["c"] if prev and prev["t"][:10] == b["t"][:10] else None
    zone = float(P["zoneAtr"]) * a
    pw, fl, cw = (float(w[k]) if w.get(k) else None for k in ("putWall", "flip", "callWall"))
    c = b["c"]
    out = {
        "aboveVwap": c > vw if long_ else c < vw,
        "vwapCross": pc is not None and ((pc <= I["vwap"][i - 1] and c > vw) if long_ else (pc >= I["vwap"][i - 1] and c < vw)),
        "aboveEma": em is not None and (c > em if long_ else c < em),
        "emaStack": em is not None and em2 is not None and (em > em2 if long_ else em < em2),
        "greenBar": c > b["o"] if long_ else c < b["o"],
        "kcNotExt": bool(kc) and (c < kc[1] if long_ else c > kc[2]),
        "kcLower": bool(kc) and (c <= kc[0] if long_ else c >= kc[0]),
        "sqOn": bool(sq) and sq["on"],
        "sqFire": bool(sq) and sq["fired"] and (sq["mom"] > 0 if long_ else sq["mom"] < 0),
        "sqMom": bool(sq) and ((sq["mom"] > 0 and sq["up"]) if long_ else (sq["mom"] < 0 and not sq["up"])),
        "inGreen": bool(ir) and ir["s"] == ("G" if long_ else "R"),
        "inNotRed": bool(ir) and ir["s"] != ("R" if long_ else "G"),
        "fvgTouch": (I["fvgBull"] if long_ else I["fvgBear"])[i] is not None,
        "orbBreak": bool(orb) and pc is not None and ((pc <= orb[0] < c) if long_ else (pc >= orb[1] > c)),
        "pmBreak": bool(pm) and pc is not None and ((pc <= pm[0] < c) if long_ else (pc >= pm[1] > c)),
        "pwTouch": (pw is not None and b["l"] <= pw + zone and c > pw) if long_ else (cw is not None and b["h"] >= cw - zone and c < cw),
        "aboveFlip": fl is not None and (c > fl if long_ else c < fl),
    }
    X = I.get("X")
    if X is None:
        return out
    d, hm = b["t"][:10], b["t"][11:16]
    inf = X["info"].get(d) or {}
    pv = inf.get("prev") or {}
    z = X["pmz"].get(d)
    lis = X["lis"].get(d)
    sd = 1 if long_ else -1
    gap = inf.get("gap")
    ctx = I.get("ctx") or {}
    dps = (ctx.get("dp") or {}).get(d) or []
    vix = (ctx.get("vix") or {}).get(d)
    peer = (ctx.get("peer") or {}).get(b["t"])
    rv = I["rvol"][i]
    out.update({
        "bandForm": X["band"][i] == sd,
        "lisForm": X["lisF"][i] == sd,
        "lisAbove": lis is not None and (c > lis if long_ else c < lis),
        "pmzTouch": bool(z) and ((b["l"] <= z["up"] and c > z["up"]) if long_ else (b["h"] >= z["lo"] and c < z["lo"])),
        "fibTouch": any(_test(b, L, zone, long_) for L in X["fibs"].get(d) or []),
        "dpTouch": any(_test(b, float(L), zone, long_) for L in dps),
        "prevTouch": bool(pv) and any(_test(b, L, zone, long_) for L in (pv["h"], pv["l"], pv["c"])),
        "sqTurn": any(I["sqTurnD"][j] == sd and I["tt"][j][:10] == d for j in range(max(0, i - int(P["formWin"]) + 1), i + 1)),
        "gapOn": gap is not None and float(P["gapMin"]) <= abs(gap) <= float(P["gapMax"]),
        "gapWith": gap is not None and gap * sd > 0,
        "gapFade": gap is not None and gap * sd < 0,
        "flipOpen": fl is not None and bool(inf) and ((inf["open"] > fl) if long_ else (inf["open"] < fl)),
        "flipFade": fl is not None and bool(inf) and ((inf["open"] < fl) if long_ else (inf["open"] > fl)),
        "pwSide": pw is not None and ((c > pw) if long_ else (c < pw)),
        "cvdSide": (lambda v: v is not None and (v > 0 if long_ else v < 0))(
            (ctx.get("cvd") or {}).get(b["t"], I["cvdE"][i]) if P.get("cvdReal") else I["cvdE"][i]),
        "peerAgree": peer == sd,
        "rvolOn": rv is not None and rv >= float(P["rvolMin"]),
        "vixOn": vix is not None and float(P["vixMin"]) <= vix <= float(P["vixMax"]),
        "noLunch": not (P["lunchFrom"] <= hm < P["lunchTo"]),
    })
    return out


def _levels(I, i, b, w, ctx):
    """Every level known at candle i (for the level target)."""
    X = I.get("X") or {}
    d = b["t"][:10]
    inf = (X.get("info") or {}).get(d) or {}
    pv = inf.get("prev") or {}
    lv = list((X.get("fibs") or {}).get(d) or [])
    lv += [float(w[k]) for k in ("putWall", "flip", "callWall") if w.get(k)]
    lv += [pv[k] for k in ("h", "l", "c") if pv]
    lv += [float(x) for x in ((ctx or {}).get("dp") or {}).get(d) or []]
    pm = I["pm"].get(d)
    if pm:
        lv += [pm[0], pm[1]]
    if I["orb"][i]:
        lv += list(I["orb"][i])
    return lv


def _r(s):
    side = s["side"]
    risk = abs(s["entry"] - s["stop0"])
    ex = s.get("exit") if s["status"] != "open" else s["last"]
    pnl = (ex - s["entry"]) * side
    if s.get("scaled") is not None:                        # half off at the scale-out price
        pnl = 0.5 * (s["scaled"] - s["entry"]) * side + 0.5 * pnl
    return round(pnl / s["entry"] * 100, 3), (round(pnl / risk, 2) if risk > 0 else None)


IND_KEYS = ("emaLen", "ema2Len", "kcLen", "kcMult", "sqLen", "sqBB", "sqKC", "sqFireN", "inN1", "inN2", "inN3", "fvgMin",
            "fvgNear", "fvgDays", "orbMin", "volN", "swingN", "sqTurnTf", "barMin")
STUDY_RULES = ("bandForm", "lisForm", "lisAbove", "pmzTouch", "fibTouch", "dpTouch", "prevTouch", "sqTurn") + FILTERS


def detect(bars_all, P, walls=None, start=None, ctx=None):
    """Signals on 1-minute candles. bars_all: every candle (pre-market included). ctx (optional): {"dp": {day: [dark
    pool levels]}, "peer": {minute: +1 / -1 the other ticker vs its VWAP}, "vix": {day: previous VIX close}, "cache": {}
    (indicators reused across settings that share them, for the same candles)}. Returns [signal]."""
    P = {**PARAMS, **(P or {})}
    walls = walls or {}
    if P.get("flipPrev") and walls:                    # the walls / flip of the PREVIOUS stored day (surely known at the open)
        ds = sorted(walls)
        walls = {ds[k]: walls[ds[k - 1]] for k in range(1, len(ds))}
    ctx = ctx or {}
    on = lambda k: bool(P.get(k))
    cache = ctx.get("cache")
    bm = int(P.get("barMin") or 1)
    if bm > 1:                                         # the strategy on n-minute candles
        if cache is not None and ("agg", bm) in cache:
            bars_all = cache[("agg", bm)]
        else:
            bars_all = agg_bars(bars_all, bm)
            if cache is not None:
                cache[("agg", bm)] = bars_all
    if cache is not None and ("bars", bm) in cache:
        bars = cache[("bars", bm)]
    else:
        bars = [b for b in bars_all if "09:30" <= b["t"][11:16] < "16:00"]
        if cache is not None:
            cache[("bars", bm)] = bars
    if len(bars) < 30:
        return []
    ik = tuple(P[k] for k in IND_KEYS)
    I = cache.get(ik) if cache is not None else None
    if I is None:
        I = indicators(bars, bars_all, P)
        if cache is not None:
            cache[ik] = I
    need_x = any(on(k) for k in STUDY_RULES) or on("lvlTarget")
    if need_x:
        xk = ("X", int(P["formWin"]), bm)
        X = cache.get(xk) if cache is not None else None
        if X is None:
            X = studies(bars, bars_all, P)
            if cache is not None:
                cache[xk] = X
        I = {**I, "X": X, "ctx": ctx}
    rules = [k for k in ENTRY + FILTERS if on(k)]
    fx = fx_series(bars, I, walls, ctx, P)
    fx_in = {1: fx["fxLong"], -1: fx["fxShort"]}
    fx_out = {1: fx["fxExitLong"], -1: fx["fxExitShort"]}
    sides = ([1] if on("longs") else []) + ([-1] if on("shorts") else [])
    st_t, en_t, fl_t = P["startT"], P["endT"], P["flatT"]
    sigs, open_, per_day, day_r = [], None, {}, {}
    for i, b in enumerate(bars):
        day, hm = b["t"][:10], b["t"][11:16]
        last_of_day = hm >= "15:59" or (i + 1 < len(bars) and bars[i + 1]["t"][:10] != day)   # end of data != end of day (live)
        if open_:
            s = open_
            side, a = s["side"], s["unit"]
            risk = abs(s["entry"] - s["stop0"])
            exit_ = None
            if on("stopOn") and ((b["l"] <= s["stop"]) if side > 0 else (b["h"] >= s["stop"])):
                px = min(b["o"], s["stop"]) if side > 0 else max(b["o"], s["stop"])
                exit_ = ("trail" if s.get("trailed") else "breakeven" if s.get("be") else "stop", px)
            else:
                if on("scaleOn") and s.get("scaled") is None and risk > 0:
                    sp = s["entry"] + side * float(P["scaleR"]) * risk
                    if (b["h"] >= sp) if side > 0 else (b["l"] <= sp):
                        s["scaled"] = round(max(b["o"], sp) if side > 0 else min(b["o"], sp), 4)
                        s["scaledAt"] = b["t"]
                if s.get("target") and ((b["h"] >= s["target"]) if side > 0 else (b["l"] <= s["target"])):
                    px = max(b["o"], s["target"]) if side > 0 else min(b["o"], s["target"])
                    exit_ = ("level" if s.get("lvlT") else "target", px)
            if not exit_:
                c = b["c"]
                vw, em, sq, ir = I["vwap"][i], I["ema"][i], I["sq"][i], I["inr"][i]
                mins = (datetime.strptime(b["t"], "%Y-%m-%dT%H:%M") - datetime.strptime(s["at"], "%Y-%m-%dT%H:%M")).seconds // 60
                if on("vwapExit") and ((c < vw) if side > 0 else (c > vw)):
                    exit_ = ("vwap", c)
                elif on("emaExit") and em is not None and ((c < em) if side > 0 else (c > em)):
                    exit_ = ("ema", c)
                elif on("sqExit") and sq and ((not sq["up"]) if side > 0 else sq["up"]):
                    exit_ = ("squeeze", c)
                elif on("inExit") and ir and ir["s"] != ("G" if side > 0 else "R"):
                    exit_ = ("inertia", c)
                elif on("fvgExit") and (I["newBear"][i] if side > 0 else I["newBull"][i]):
                    exit_ = ("fvg", c)
                elif fx_out[side] is not None and fx_out[side][i]:
                    exit_ = ("formula", c)
                elif on("timeOn") and mins >= int(P["timeMin"]):
                    exit_ = ("time stop", c)
                elif hm >= fl_t or last_of_day:
                    exit_ = ("time", c)
            if exit_:
                s.update(status=exit_[0], exitAt=b["t"], exit=round(exit_[1], 4))
                day_r[day] = day_r.get(day, 0) + (_r(s)[1] or 0)
                open_ = None
            else:
                s["last"], s["lastAt"] = b["c"], b["t"]
                best = max(s.get("best", s["entry"]), b["h"]) if side > 0 else min(s.get("best", s["entry"]), b["l"])
                s["best"] = best
                if on("trailOn") and a:
                    lvl = best - side * float(P["trailAtr"]) * a
                    if (lvl > s["stop"]) if side > 0 else (lvl < s["stop"]):
                        s["stop"] = round(lvl, 4)
                        s["trailed"] = True
                        s.setdefault("stops", [[s["at"], s["stop0"]]]).append([b["t"], s["stop"]])
                if on("beOn") and on("stopOn") and not s.get("be") and risk > 0 and (best - s["entry"]) * side >= float(P["beR"]) * risk:
                    if (s["entry"] > s["stop"]) if side > 0 else (s["entry"] < s["stop"]):
                        s["stop"] = s["entry"]
                        s.setdefault("stops", [[s["at"], s["stop0"]]]).append([b["t"], s["stop"]])
                    s["be"] = True
            continue
        if not (rules or fx_in[1] is not None or fx_in[-1] is not None) or not sides or hm < st_t or hm >= en_t or last_of_day or (start and day < start):
            continue
        if per_day.get(day, 0) >= int(P["maxTrades"]):
            continue
        if on("dayStop") and day_r.get(day, 0) <= -float(P["dayStopR"]):
            continue
        a = I["atr"][i] or 0
        if not a:
            continue
        w = walls.get(day) or {}
        prev = bars[i - 1] if i else None
        for side in sides:
            if fx_in[side] is not None:
                if not fx_in[side][i]:
                    continue                               # the formula rule of this side
            elif not rules:
                continue                                   # a formula for the other side only
            ck = _checks(i, b, prev, I, w, side, P)
            if all(ck[k] for k in rules):
                e = b["c"]
                sw = swing_stop(I, i, e, side, float(P["swingBuf"])) if P.get("stopBase") == "swing" else None
                if sw is not None:
                    st, u = sw, abs(e - sw)                    # trailing size: x this risk
                else:
                    u = stop_unit(I, i, e, P) or a
                    st = round(e - side * float(P["stopAtr"]) * u, 4)
                tgt = round(e + side * float(P["targetR"]) * abs(e - st), 4) if on("targetOn") else None
                lvl_t = False
                if on("lvlTarget"):
                    beyond = [L for L in _levels(I, i, b, w, ctx) if (L - e) * side >= 0.25 * a]
                    if beyond:
                        tgt, lvl_t = round(min(beyond, key=lambda L: abs(L - e)), 4), True
                s = {"at": b["t"], "kind": "day", "side": side, "dir": "long" if side > 0 else "short", "entry": e,
                     "stop0": st, "stop": st if on("stopOn") else (0.0 if side > 0 else 1e12), "target": tgt, "atr": round(a, 4),
                     "unit": round(u, 4),
                     "status": "open", "last": e, "lastAt": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
                     "rules": rules, "level": st, "levelName": "stop"}
                if lvl_t:
                    s["lvlT"] = True
                if not on("stopOn"):
                    s["stop"] = -1e12 if side > 0 else 1e12
                sigs.append(s)
                open_ = s
                per_day[day] = per_day.get(day, 0) + 1
                break
    for s in sigs:
        s["pct"], s["R"] = _r(s)
        if abs(s["stop"]) >= 1e11:
            s["stop"] = None
    return sigs


def stats(rows):
    rs = [r["R"] for r in rows if r.get("R") is not None]
    n = len(rs)
    if not n:
        return {"n": 0}
    w = [x for x in rs if x > 0]
    lo = [x for x in rs if x <= 0]
    return {"n": n, "win": round(len(w) / n * 100, 1), "avgR": round(sum(rs) / n, 3), "totR": round(sum(rs), 2),
            "pf": round(sum(w) / -sum(lo), 2) if lo and sum(lo) < 0 else None, "avgWin": round(sum(w) / len(w), 2) if w else None,
            "avgLoss": round(sum(lo) / len(lo), 2) if lo else None,
            "longs": sum(1 for r in rows if r.get("side") == 1), "shorts": sum(1 for r in rows if r.get("side") == -1),
            "exits": {k: sum(1 for r in rows if r["status"] == k) for k in sorted({r["status"] for r in rows})}}


# ---------------- chart / backtest ----------------

def _agg(bars, m):
    """1-minute candles -> m-minute candles aligned on 09:30 (regular session only)."""
    out, key = [], None
    for b in bars:
        hm = int(b["t"][11:13]) * 60 + int(b["t"][14:16])
        k0 = 570 + (hm - 570) // m * m
        k = f"{b['t'][:10]}T{k0 // 60:02d}:{k0 % 60:02d}"
        if k != key:
            key = k
            out.append({"t": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v") or 0, "_last": b["t"]})
        else:
            x = out[-1]
            x.update(h=max(x["h"], b["h"]), l=min(x["l"], b["l"]), c=b["c"], v=(x["v"] or 0) + (b.get("v") or 0), _last=b["t"])
    return out


def chart(sub, sym, days=3, tf=1, cfg=None):
    sym = (sym or "QQQ").upper()
    P = clean_cfg(config(sub), cfg or {})
    days = max(1, min(30, int(days or 3)))
    allb = minute_bars(sub, sym, days + 3)
    if not allb:
        return {"symbol": sym, "bars": [], "error": "No minute candles."}
    dates = sorted({b["t"][:10] for b in allb})
    show = dates[-days:]
    W = walls_by_day(sub, sym, dates[-days - 1:] if P.get("flipPrev") else show, bool(P.get("wallNear")), budget_s=15)
    ctx = live_ctx(sub, sym, P, show, days + 3)
    sigs = detect(allb, P, W, show[0], ctx)
    for x in sigs:                                     # the chart draws a scale-out as {at, px, frac}
        if isinstance(x.get("scaled"), (int, float)):
            x["scaled"] = {"at": x.get("scaledAt"), "px": x["scaled"], "frac": 0.5}
    reg = [b for b in allb if "09:30" <= b["t"][11:16] < "16:00"]
    I = indicators(reg, allb, P)
    keep = [i for i, b in enumerate(reg) if b["t"][:10] >= show[0]]
    tfm = int(tf or 1)
    if tfm > 1:
        out_bars = _agg([reg[i] for i in keep], tfm)
        last_i = {reg[i]["t"]: i for i in keep}
        pick = [(x["t"], last_i[x["_last"]]) for x in out_bars]
        for x in out_bars:
            x.pop("_last", None)
    else:
        out_bars = [{k: reg[i][k] for k in ("t", "o", "h", "l", "c", "v")} for i in keep]
        pick = [(reg[i]["t"], i) for i in keep]
    r4 = lambda v: round(v, 4) if v is not None else None
    series = lambda key: [{"t": t, "v": r4(I[key][i])} for t, i in pick if I[key][i] is not None]
    walls = [{"t": f"{d}T09:30", "callWall": (W.get(d) or {}).get("callWall"), "putWall": (W.get(d) or {}).get("putWall"),
              "flip": (W.get(d) or {}).get("flip"), "src": "uw" if W.get(d) else "gap"} for d in show]
    import tr3ndy
    b5 = tr3ndy.agg(allb, 5)
    lines, lisf, sent = tr3ndy.lis(b5)
    bser, bform = tr3ndy.bands(out_bars)
    sqz_h = [{"t": t, "m": I["sq"][i]["mom"]} for t, i in pick if I["sq"][i]]
    prev_d = [x for x in dates if x < show[0]][-1:]
    fb = {d: v for d, v in tr3ndy.fibs([b for b in allb if b["t"][:10] in prev_d + show]).items() if d in show}   # previous day's range
    pz = tr3ndy.pmz(b5, lines)
    studies = {"bands": bser, "bandsF": bform, "lis": {d: lines[d] for d in show if d in lines},
               "lisF": [x for x in lisf if x["t"][:10] >= show[0]], "lisSent": sent.get(show[-1]),
               "pmz": {d: pz[d] for d in show if d in pz}, "fibs": fb,
               "turns": [{"t": reg[i]["t"], "dir": I["sqTurnD"][i]} for i in keep if I["sqTurnD"][i]]}   # the turns the rule uses
    return _pack({"symbol": sym, "bars": out_bars, "interval": tfm, "days": days, "dt": True, **studies,
            "vwap": series("vwap"), "ema": series("ema"), "ema2": series("ema2"), "emaLen": int(P["emaLen"]),
            "kch": [{"t": t, "m": r4(I["kc"][i][0]), "u": r4(I["kc"][i][1]), "l": r4(I["kc"][i][2])} for t, i in pick if I["kc"][i]],
            "inertia": [{"t": t, "s": I["inr"][i]["s"], "pts": 0} for t, i in pick if I["inr"][i]],
            "sqzH": [{"t": t, "on": I["sq"][i]["on"], "f": I["sq"][i]["fired"], "m": I["sq"][i]["mom"], "u": I["sq"][i]["up"]} for t, i in pick if I["sq"][i]],
            "pm": {d: {"h": I["pm"][d][0], "l": I["pm"][d][1]} for d in show if d in I["pm"]},
            "orbLv": {d: {"h": x[0], "l": x[1]} for d in show for x in [next((I["orb"][i] for i in keep if reg[i]["t"][:10] == d and I["orb"][i]), None)] if x},
            "walls": walls, "signals": sigs, "stats": stats(sigs), "alerts": [], "evals": [], "maxPain": [], "expirations": [],
            "trades": [], "wallsNow": W.get(show[-1]), "cfgAt": P.get("savedAt")})


PACK = ("bars", "bands", "sqzH", "kch", "inertia", "ema", "ema2", "vwap")


def _pack(out):
    """Long per-candle series as columns + rows, the time as the index of the candle (the response must stay under the
    6 MB Lambda limit on 30 days of 1-minute candles). The frontend unpacks them back to [{t, ...}]."""
    bars = out.get("bars") or []
    ix = {b["t"]: i for i, b in enumerate(bars)}
    for k in PACK:
        v = out.get(k)
        if not isinstance(v, list) or not v or not isinstance(v[0], dict):
            continue
        cols = [c for c in v[0] if c != "t"]
        if k == "bars":
            rows = [[x["t"]] + [x.get(c) for c in cols] for x in v]
            out[k] = {"_p": 1, "cols": ["t"] + cols, "rows": rows}
        else:
            if any(x.get("t") not in ix for x in v):
                continue
            out[k] = {"_p": 1, "cols": ["i"] + cols, "rows": [[ix[x["t"]]] + [x.get(c) for c in cols] for x in v]}
    return out


def live_ctx(sub, sym, P, dates, n_days, now=None, peer_bars=None):
    """Context for the chart / the live tick: dark pool levels, the other ticker vs its VWAP, VIX (only when used);
    with formula rules: everything, plus the market context (today's live)."""
    fx_on = any(P.get(k) for k in FORMULAS)
    on = lambda k: fx_on or bool(P.get(k))
    ctx = {}
    if fx_on:
        ctx.update(market_ctx(sym, dates))
        try:
            import marketctx
            today = (now or now_ny()).strftime("%Y-%m-%d")
            if today in dates:
                live = marketctx.live_day(sub, sym, today)
                if live:
                    ctx.setdefault("mx", {})[today] = live
        except Exception as e:
            print("live market context failed", sym, e)
    if on("dpTouch") or on("lvlTarget"):
        try:
            import backtest as _bt
            import wallwatch
            ctx["dp"] = wallwatch.dp_level_map(_bt.stored_darkpool(sym), dates)
        except Exception as e:
            print("dark pool levels failed", sym, e)
    if on("peerAgree"):
        try:
            ps = peer_of(sym, P.get("tickers") or [])
            pb = (peer_bars or {}).get(ps)
            if pb is None:
                pb = minute_bars(sub, ps, n_days, now)
                if peer_bars is not None:
                    peer_bars[ps] = pb
            ctx["peer"] = peer_map(pb)
        except Exception as e:
            print("peer failed", sym, e)
    if on("vixOn"):
        try:
            ctx["vix"] = vix_map()
        except Exception as e:
            print("vix failed", e)
    if on("cvdReal"):
        try:
            ctx["cvd"] = real_cvd(sym, dates)
        except Exception as e:
            print("real cvd failed", e)
    return ctx


def why(sub, sym, at, cfg=None):
    """Why a trade was / was not taken at one minute: every rule that is ON, long and short, plus the time window,
    the trades of the day and an open trade."""
    sym = (sym or "QQQ").upper()
    P = clean_cfg(config(sub), cfg or {})
    at = str(at or "")[:16].replace(" ", "T")
    try:
        when = datetime.strptime(at, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise BadRequest("Give the minute as YYYY-MM-DD HH:MM.")
    back = max(3, min(40, (now_ny().replace(tzinfo=None) - when).days + 4))
    allb = minute_bars(sub, sym, back)
    bars = [b for b in allb if "09:30" <= b["t"][11:16] < "16:00"]
    tt = [b["t"] for b in bars]
    if at not in tt:
        raise BadRequest(f"No 1-minute candle at {at.replace('T', ' ')} (regular session only).")
    i = tt.index(at)
    d = at[:10]
    dates = sorted({b["t"][:10] for b in allb})
    W = walls_by_day(sub, sym, dates[-back:], bool(P.get("wallNear")), budget_s=15)
    ctx = live_ctx(sub, sym, P, dates[-back:], back)
    ctx["cache"] = {}
    sigs = detect(allb, P, W, None, ctx)
    I = ctx["cache"].get(tuple(P[k] for k in IND_KEYS)) or indicators(bars, allb, P)
    X = ctx["cache"].get(("X", int(P["formWin"]))) or studies(bars, allb, P)
    I = {**I, "X": X, "ctx": ctx}
    w = W.get(d) or {}
    if P.get("flipPrev") and W:
        ds = sorted(W)
        w = W.get(ds[ds.index(d) - 1]) if d in ds and ds.index(d) > 0 else {}
    rules = [k for k in ENTRY + FILTERS if P.get(k)]
    out = {"at": at, "symbol": sym, "close": bars[i]["c"], "rules": []}
    for side in (1, -1):
        ck = _checks(i, bars[i], bars[i - 1] if i else None, I, w, side, P)
        for k in rules:
            out["rules"].append({"rule": k, "side": "long" if side > 0 else "short", "ok": bool(ck.get(k))})
    hm = at[11:16]
    notes = []
    if not (P["startT"] <= hm < P["endT"]):
        notes.append(f"outside the entry window {P['startT']}-{P['endT']}")
    if not P.get("longs"):
        notes.append("longs are OFF")
    if not P.get("shorts"):
        notes.append("shorts are OFF")
    day_sigs = [x for x in sigs if x["at"][:10] == d]
    before = [x for x in day_sigs if x["at"] < at]
    if len(before) >= int(P["maxTrades"]):
        notes.append(f"max {P['maxTrades']} trades a day already taken")
    open_ = next((x for x in day_sigs if x["at"] < at and (x.get("exitAt") or "9999") >= at), None)
    if open_:
        notes.append(f"already in a {open_['dir']} trade since {open_['at'][11:16]}")
    if any(x["at"] == at for x in day_sigs):
        notes.append("a trade was taken on this candle")
    out["notes"] = notes
    out["cvd"] = round(I["cvdE"][i])
    out["walls"] = {k: w.get(k) for k in ("putWall", "flip", "callWall")}
    return out


BT_SK = "DTBT"


def deep_stats(rows, days_list):
    """Validation statistics on the trades (R): halves, by month / weekday / entry hour, drawdown, streaks, equity."""
    rs = [r for r in rows if r.get("R") is not None]
    out = {"all": stats(rs)}
    if not rs:
        return out
    mid = days_list[len(days_list) // 2] if days_list else rs[len(rs) // 2]["at"][:10]
    out["h1"], out["h2"] = stats([r for r in rs if r["at"][:10] < mid]), stats([r for r in rs if r["at"][:10] >= mid])
    out["mid"] = mid
    grp = lambda key: {k: stats([r for r in rs if key(r) == k]) for k in sorted({key(r) for r in rs})}
    wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    out["byMonth"] = grp(lambda r: r["at"][:7])
    out["byWeekday"] = {wd[k]: v for k, v in grp(lambda r: datetime.strptime(r["at"][:10], "%Y-%m-%d").weekday()).items()}
    out["byHour"] = grp(lambda r: r["at"][11:13] + ":00")
    out["byDir"] = grp(lambda r: r.get("dir") or "long")
    eq, peak, dd, cur_l, max_l, curve = 0.0, 0.0, 0.0, 0, 0, []
    for r in rs:
        eq += r["R"]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
        cur_l = cur_l + 1 if r["R"] <= 0 else 0
        max_l = max(max_l, cur_l)
        curve.append(round(eq, 2))
    step = max(1, len(curve) // 300)
    out["curve"] = curve[::step] + ([curve[-1]] if (len(curve) - 1) % step else [])
    out["maxDD"], out["maxLosing"] = round(dd, 2), max_l
    byday = {}
    for r in rs:
        byday[r["at"][:10]] = byday.get(r["at"][:10], 0) + r["R"]
    dv = list(byday.values())
    mean = sum(dv) / len(dv)
    sd = (sum((x - mean) ** 2 for x in dv) / len(dv)) ** 0.5 if len(dv) > 1 else 0
    out["daysTraded"], out["daysTotal"] = len(dv), len(days_list)
    out["greenDays"] = sum(1 for x in dv if x > 0)
    out["dailySharpe"] = round(mean / sd * (252 ** 0.5), 2) if sd else None
    out["avgDayR"] = round(mean, 3)
    return out


def peer_of(sym, tickers):
    o = [t for t in tickers if t != sym]
    return o[0] if o else ("SPY" if sym == "QQQ" else "QQQ")


def peer_map(bars_all):
    """{minute: +1 / -1}: the close above / under its session VWAP (for the 'other ticker agrees' filter)."""
    reg = [b for b in bars_all if "09:30" <= b["t"][11:16] < "16:00"]
    vw = _session_vwap(reg)
    out = {b["t"]: (1 if b["c"] > v else -1 if b["c"] < v else 0) for b, v in zip(reg, vw)}
    day, o = None, None
    for b in reg:                                      # "chg|minute": % since the day's open (relative strength, rs)
        if b["t"][:10] != day:
            day, o = b["t"][:10], b["o"]
        if o:
            out["chg|" + b["t"]] = round((b["c"] / o - 1) * 100, 4)
    return out


def vix_map():
    """{day: the previous day's VIX close} (Yahoo daily, cached 6 hours)."""
    import charts
    hit = db.get("VIXD", "daily")
    if hit and time.time() - hit.get("at", 0) < 6 * 3600 and hit.get("m"):
        return {k: float(v) for k, v in hit["m"].items()}
    end = now_ny().replace(tzinfo=None)
    bars = charts._yahoo("^VIX", "1d", end - timedelta(days=1100), end + timedelta(days=1))
    m, prev = {}, None
    for b in bars:
        d = b["t"][:10]
        if prev is not None:
            m[d] = prev
        prev = round(b["c"], 2)
    m[(end + timedelta(days=1)).strftime("%Y-%m-%d")] = prev
    if prev is not None:                                  # today (no daily bar yet before the close)
        m.setdefault(end.strftime("%Y-%m-%d"), prev)
    try:
        from decimal import Decimal
        db.put({"PK": "VIXD", "SK": "daily", "at": int(time.time()), "m": {k: Decimal(str(v)) for k, v in m.items()}})
    except Exception as e:
        print("vix cache skipped", e)
    return m


def _bars_any(sub, sym, n, feed_used):
    try:
        out = minute_bars(sub, sym, n, feed="sip")
        feed_used[sym] = "sip"
    except Exception:
        out = minute_bars(sub, sym, n, feed="iex")
        feed_used[sym] = "iex"
    return out


def run_variants(sub, days, variants, budget_s=540, tickers=None):
    """Every variant (full settings) on the same candles: the last `days` trading days of every day ticker, loaded
    once. Context loaded only when a variant needs it (dark pool levels, the other ticker vs its VWAP, VIX).
    Returns ({variant index: [trades]}, {variant index: {ticker: stats}}, sorted days, feed, errors)."""
    t0 = time.time()
    days = max(2, min(500, int(days or 20)))
    tickers = tickers or variants[0]["tickers"]
    fx_on = any(v.get(k) for v in variants for k in FORMULAS)     # formulas may use any context
    on = lambda k: fx_on or any(v.get(k) for v in variants)
    rows = {i: [] for i in range(len(variants))}
    per = {i: {} for i in range(len(variants))}
    errors, all_days, feed_used = [], set(), {}
    vix = {}
    if on("vixOn"):
        try:
            vix = vix_map()
        except Exception as e:
            errors.append(f"VIX: {str(e)[:100]}")
    peers = {}
    if on("peerAgree"):
        for sym in tickers:
            ps = peer_of(sym, tickers)
            if ps in peers:
                continue
            try:
                bb = _bars_any(sub, ps, days + 2, {})
                peers[ps] = peer_map(bb)
                del bb
            except Exception as e:
                errors.append(f"{ps} (other ticker): {str(e)[:100]}")
    for sym in tickers:
        if time.time() - t0 > budget_s:
            errors.append(f"{sym}: time limit")
            continue
        try:
            allb = _bars_any(sub, sym, days + 2, feed_used)
            dates = sorted({b["t"][:10] for b in allb})
            show = dates[-days:]
            all_days.update(show)
            dp = {}
            if on("dpTouch") or on("lvlTarget"):
                try:
                    import backtest as _bt
                    import wallwatch
                    dp = wallwatch.dp_level_map(_bt.stored_darkpool(sym), show)
                except Exception as e:
                    print("dark pool levels failed", sym, e)
            ctx = {"dp": dp, "vix": vix, "peer": peers.get(peer_of(sym, tickers)) or {}, "cache": {}}
            if fx_on:
                ctx.update(market_ctx(sym, show))
            if on("cvdReal"):
                ctx["cvd"] = real_cvd(sym, show)
            W = {}
            for i, P in enumerate(variants):
                if time.time() - t0 > budget_s + 30:
                    errors.append(f"{sym}: time limit at variant {i + 1}")
                    break
                mode = bool(P.get("wallNear"))
                if mode not in W:
                    W[mode] = walls_by_day(sub, sym, show, mode, budget_s=max(30, budget_s - (time.time() - t0) - 90))
                sg = detect(allb, P, W[mode], show[0] if show else None, ctx)
                for x in sg:
                    x["sym"] = sym
                rows[i] += sg
                per[i][sym] = stats(sg)
            del allb, ctx
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:120]}")
    for i in rows:
        rows[i].sort(key=lambda x: x["at"])
    return rows, per, sorted(all_days), feed_used, errors


TRADE_KEYS = ("sym", "at", "dir", "entry", "exit", "exitAt", "status", "R", "pct", "optPct", "optSym", "optIn", "optOut")


def backtest(sub, days=20, cfg=None, budget_s=540, opt=None):
    """The settings on the last `days` trading days of 1-minute candles (SIP history when the account allows it,
    else IEX), every day ticker, then the validation statistics; opt: price every trade on a real 0DTE option."""
    t0 = time.time()
    P = clean_cfg(config(sub), cfg or {})
    days = max(2, min(500, int(days or 20)))
    ob = 150 if opt and opt.get("on") else 0
    rr, per, all_days, feed_used, errors = run_variants(sub, days, [P], budget_s - ob)
    rows = rr[0]
    mx_used = [n for n in MX_NAMES if any(re.search(rf"\b{n}\b", P.get(k) or "") for k in FORMULAS)]
    if mx_used and all_days:                           # formulas on market context: days without it can't trade
        try:
            import marketctx
            cov = marketctx.coverage(sorted(all_days))
            need_uw = any(n in MX_UW for n in mx_used)
            have = min(cov["alp"], cov["uw"]) if need_uw else cov["alp"]
            if have < len(all_days):
                errors.insert(0, f"The formulas use market context ({', '.join(mx_used)}), loaded for {have} of {len(all_days)} days: "
                                 f"the other days can't trade. Load more days in AI research → Market context.")
        except Exception as e:
            print("coverage failed", e)
    optst = None
    if ob:
        import dtoptions
        optst = dtoptions.price(sub, rows, opt, budget_s=max(60, budget_s - (time.time() - t0) - 20))
    byday = {}
    for s in rows:
        if s.get("R") is not None:
            byday[s["at"][:10]] = round(byday.get(s["at"][:10], 0) + s["R"], 2)
    ds = deep_stats(rows, all_days)
    return {"at": iso(now_ny()), "days": days, "daysFound": len(all_days), "first": all_days[0] if all_days else None,
            "all": ds.pop("all"), **ds, "byTicker": per[0], "byDay": byday, "feed": feed_used, "secs": round(time.time() - t0),
            "opt": optst, "used": used_summary(P), "trades": [{k: s.get(k) for k in TRADE_KEYS} for s in rows][-300:], "errors": errors}


def used_summary(P):
    """The settings a run used, in short (shown with the result)."""
    on = lambda k: bool(P.get(k))
    unit = {"atr1": "× 1-min ATR", "atr5": "× 5-min ATR", "atrD": "× daily ATR", "pct": "% of price", "avgN": f"× avg move {P.get('volN')}",
            "swing": "swing"}.get(P.get("stopBase") or "atr1", "")
    stop = (f"swing low −{P.get('swingBuf')}%" if P.get("stopBase") == "swing" else f"{P.get('stopAtr')} {unit}") if on("stopOn") else "no stop"
    return {"entry": [k for k in ENTRY if on(k)], "filter": [k for k in FILTERS if on(k)], "exit": [k for k in EXITS if on(k) and k != "stopOn"],
            "stop": stop, "target": f"{P.get('targetR')}R" if on("targetOn") else None,
            "sides": "longs + shorts" if on("longs") and on("shorts") else "longs only" if on("longs") else "shorts only",
            "time": f"{P.get('startT')}-{P.get('endT')}, flat {P.get('flatT')}, max {P.get('maxTrades')}/day",
            "walls": ("previous day's" if on("flipPrev") else "same day") + (", nearest expiration" if on("wallNear") else ""),
            "formulas": {k: P[k] for k in FORMULAS if P.get(k)} or None, "barMin": int(P.get("barMin") or 1)}


PRE = "DTPRE#"
CMP_SK = "DTCMP"


def presets(sub):
    return sorted(({"name": r.get("name"), "at": r.get("at"), "cfg": full_cfg(r.get("cfg") or {})} for r in db.q_prefix(db.upk(sub), PRE)),
                  key=lambda x: (x["name"] or "").lower())


def save_preset(sub, name, cfg=None):
    from decimal import Decimal
    name = str(name or "").strip()[:40]
    if not name:
        raise BadRequest("Give the settings a name.")
    cur = clean_cfg(config(sub), cfg or {})
    if len(list(db.q_prefix(db.upk(sub), PRE))) >= 30 and not db.get(db.upk(sub), PRE + name.lower()):
        raise BadRequest("30 saved settings at most: delete one first.")
    db.put({"PK": db.upk(sub), "SK": PRE + name.lower(), "name": name, "at": iso(now_ny()),
            "cfg": {k: Decimal(str(v)) if isinstance(v, float) else v for k, v in cur.items()}})
    return presets(sub)


def delete_preset(sub, name):
    db.delete(db.upk(sub), PRE + str(name or "").strip().lower())
    return presets(sub)


def compare(sub, days=60, variants=None, opt=None, budget_s=540):
    """Several settings (named variants: saved ones, the current one, a parameter sweep) on the same days and candles,
    statistics side by side."""
    t0 = time.time()
    base = config(sub)
    vs = []
    for v in (variants or [])[:10]:
        cfg = v.get("cfg") if isinstance(v.get("cfg"), dict) else {}
        vs.append({"name": str(v.get("name") or f"#{len(vs) + 1}")[:60], "P": {**clean_cfg(base, cfg), "tickers": base["tickers"]}})
    if not vs:
        raise BadRequest("Pick at least one setting to compare.")
    ob = 160 if opt and opt.get("on") else 0
    rr, per, all_days, feed_used, errors = run_variants(sub, days, [v["P"] for v in vs], budget_s - ob, base["tickers"])
    out = []
    if ob:
        import dtoptions
        left = max(60, budget_s - (time.time() - t0) - 20)
        oc = {}                                            # the same contracts' bars for every variant
        for i in range(len(vs)):
            dtoptions.price(sub, rr[i], opt, budget_s=left / len(vs), cache=oc)
    for i, v in enumerate(vs):
        ds = deep_stats(rr[i], all_days)
        o = None
        if ob:
            import dtoptions
            o = dtoptions.summary([r["optPct"] for r in rr[i] if r.get("optPct") is not None])
        import dtsearch
        out.append({"name": v["name"], "t": dtsearch.tstat([r["R"] for r in rr[i] if r.get("R") is not None]), "all": ds.get("all"), "h1": ds.get("h1"), "h2": ds.get("h2"), "maxDD": ds.get("maxDD"),
                    "maxLosing": ds.get("maxLosing"), "greenDays": ds.get("greenDays"), "daysTraded": ds.get("daysTraded"),
                    "dailySharpe": ds.get("dailySharpe"), "avgDayR": ds.get("avgDayR"), "curve": (ds.get("curve") or [])[-200:],
                    "byTicker": per[i], "byDir": ds.get("byDir"), "opt": o,
                    "changes": {k: v["P"][k] for k in PARAMS if k not in ("enabled", "trade") and v["P"].get(k) != base.get(k)}})
    return {"at": iso(now_ny()), "days": days, "daysFound": len(all_days), "first": all_days[0] if all_days else None,
            "mid": all_days[len(all_days) // 2] if all_days else None, "feed": feed_used, "variants": out,
            "secs": round(time.time() - t0), "errors": errors}


def compare_job(sub, params=None):
    p = params or {}
    try:
        out = compare(sub, p.get("days") or 60, p.get("variants"), p.get("opt"))
    except Exception as e:
        out = {"at": iso(now_ny()), "errors": [str(e)[:200]]}
    import json as _j
    from decimal import Decimal
    db.put({"PK": db.upk(sub), "SK": CMP_SK, **_j.loads(_j.dumps(out, default=str), parse_float=Decimal), "running": None})
    return out


def compare_result(sub):
    r = db.get(db.upk(sub), CMP_SK) or {}
    if r.get("running") and r["running"] < iso(now_ny() - timedelta(minutes=12)):
        r = {**r, "running": None, "errors": ["The last run hit the 10-minute limit: try fewer days or fewer settings."]}
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


def backtest_job(sub, params=None):
    pk = db.upk(sub)
    p = params or {}
    try:
        out = backtest(sub, p.get("days") or 20, p.get("cfg"), opt=p.get("opt"))
    except Exception as e:
        out = {"at": iso(now_ny()), "errors": [str(e)[:200]]}
    import json as _j
    from decimal import Decimal
    item = _j.loads(_j.dumps(out), parse_float=Decimal)
    db.put({"PK": pk, "SK": BT_SK, **item, "running": None})
    return out


def backtest_result(sub):
    r = db.get(db.upk(sub), BT_SK) or {}
    if r.get("running") and r["running"] < iso(now_ny() - timedelta(minutes=12)):
        r = {**r, "running": None, "errors": ["The last run hit the 10-minute limit: try fewer days."]}
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


# ---------------- live (second Alpaca paper account) ----------------

def _alp(c, method, path, body=None):
    import autotrader
    return autotrader._alp(c, method, path, body)


def view(sub):
    cfg = config(sub)
    acct = db.get(db.upk(sub), CRED_SK)
    info = {"connected": bool(acct), **({k: acct.get(k) for k in ("env", "keyHint", "status")} if acct else {})}
    if acct:
        try:
            a = _alp(creds(sub), "GET", "/v2/account") or {}
            info.update(equity=a.get("equity"), cash=a.get("cash"), buyingPower=a.get("buying_power"), number=a.get("account_number"))
            info["positions"] = [{"symbol": p.get("symbol"), "qty": p.get("qty"), "side": p.get("side"), "entry": p.get("avg_entry_price"),
                                  "price": p.get("current_price"), "pl": p.get("unrealized_pl"), "plPct": p.get("unrealized_plpc")}
                                 for p in (_alp(creds(sub), "GET", "/v2/positions") or [])]
        except Exception as e:
            info["error"] = str(e)[:160]
    recs = sorted((r for r in db.q_prefix(db.upk(sub), "DTPOS#")), key=lambda r: r.get("at", ""))[-60:]
    return {"config": cfg, "account": info, "trades": [{k: v for k, v in r.items() if k not in ("PK",)} for r in recs]}


def connect(sub, key, secret):
    import alpaca
    alpaca.save_creds(sub, key, secret, "paper", "iex", sk=CRED_SK)
    main = db.get(db.upk(sub), "BROKER#alpaca") or {}
    if main.get("keyHint") and main.get("keyHint") == key.strip()[-4:]:
        raise BadRequest("These look like the keys of your main Alpaca account: use a second paper account for day trading.")
    return view(sub)


def _qty(P, s):
    risk = abs(s["entry"] - s["stop0"])
    if float(P.get("riskUsd") or 0) > 0 and risk > 0:
        q = int(float(P["riskUsd"]) // risk)
    else:
        q = int(P.get("qty") or 1)
    return max(1, min(int(P.get("maxQty") or 500), q))


def tick(sub, now=None):
    """Every minute: replay today with the current settings; open what the engine opens on the last finished candle,
    close what it closes (and everything at flatT)."""
    P = config(sub)
    if not P.get("enabled"):
        return {"skipped": "off"}
    c = creds(sub)
    if not c:
        return {"skipped": "no day-trading account"}
    now = now or now_ny().replace(tzinfo=None)
    today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    if now.weekday() >= 5 or not ("09:30" <= hm < "16:00"):
        return {"skipped": "market closed"}
    pk = db.upk(sub)
    acts = []
    cut = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    loaded = {}
    for sym in P["tickers"]:
        try:
            loaded[sym] = [b for b in minute_bars(sub, sym, 3, now) if b["t"] <= cut]
        except Exception as e:
            acts.append(f"{sym}: {str(e)[:120]}")
    for sym in P["tickers"]:
        if sym not in loaded:
            continue
        try:
            allb = loaded[sym]
            pbars = {k: v for k, v in loaded.items()}
            ctx = live_ctx(sub, sym, P, [today], 3, now, pbars)
            sigs = detect(allb, P, walls_by_day(sub, sym, sorted({b["t"][:10] for b in allb} | {today})[-2:] if P.get("flipPrev") else [today], bool(P.get("wallNear")), budget_s=10), today, ctx)
            recs = {r["at"]: r for r in db.q_prefix(pk, f"DTPOS#{sym}#") if r.get("at", "")[:10] == today}
            last_t = allb[-1]["t"] if allb else ""
            for s in sigs:
                r = recs.get(s["at"])
                if r and r.get("status") == "open":
                    why = None
                    if s["status"] != "open":
                        why = f"{s['status']} exit {s['exitAt'][11:16]} @ {s['exit']}"
                    elif hm >= P["flatT"]:
                        why = f"flat at {P['flatT']}"
                    if why:
                        res = _alp(c, "DELETE", f"/v2/positions/{sym}") or {}
                        db.update(pk, r["SK"], {"status": "closed", "exitReason": why, "closedAt": iso(now_ny()),
                                                "exitOrderId": res.get("id"), "engineExit": s.get("exit"), "R": s.get("R")})
                        acts.append(f"{sym} closed: {why}")
                elif not r and s["status"] == "open" and s["at"] == last_t and P.get("trade") and hm < P["flatT"]:
                    pos = _alp(c, "GET", f"/v2/positions/{sym}")
                    if pos:
                        continue                       # something already held on this ticker
                    q = _qty(P, s)
                    o = _alp(c, "POST", "/v2/orders", {"symbol": sym, "qty": str(q), "side": "buy" if s["side"] > 0 else "sell",
                                                       "type": "market", "time_in_force": "day"}) or {}
                    db.put({"PK": pk, "SK": f"DTPOS#{sym}#{s['at']}", "symbol": sym, "at": s["at"], "dir": s["dir"], "qty": q,
                            "entry": s["entry"], "stop0": s["stop0"], "target": s.get("target"), "orderId": o.get("id"),
                            "status": "open", "openedAt": iso(now_ny()), "rules": s.get("rules")})
                    acts.append(f"{sym} {s['dir']} {q} @ ~{s['entry']}")
            if hm >= P["flatT"]:                       # safety: nothing left open after the flat time
                for r in recs.values():
                    if r.get("status") == "open":
                        _alp(c, "DELETE", f"/v2/positions/{sym}")
                        db.update(pk, r["SK"], {"status": "closed", "exitReason": f"flat at {P['flatT']}", "closedAt": iso(now_ny())})
        except Exception as e:
            acts.append(f"{sym}: {str(e)[:120]}")
    return {"actions": acts}


# ---------- morning flip check: the flip as seen before / at the open vs the one the backtest uses ----------

SNAP_TIMES = ("09:20", "09:32", "09:40")


def flip_snap_job(now=None):
    """Scheduled 09:20 / 09:32 / 09:40 ET: one Unusual Whales key (the first user with day trading set up, else any
    key), the day tickers of every day-trading user (QQQ / SPY at least)."""
    import flowdata
    now = now or now_ny()
    users = [p["PK"][5:] for p in db.scan_sk(SK)]
    tickers = {"QQQ", "SPY"}
    for u in users:
        try:
            tickers |= set(config(u)["tickers"])
        except Exception:
            pass
    keyed = users + [r["PK"][5:] for r in db.scan_sk(flowdata.SK)]
    key, sub = None, None
    for u in keyed:
        try:
            key, sub = flowdata._key(u), u
            break
        except Exception:
            continue
    if not key:
        return {"skipped": "no Unusual Whales key"}

    def price(sym):
        try:
            if now.strftime("%H:%M") < "09:31":
                return None
            bs = minute_bars(sub, sym, 1, now.replace(tzinfo=None), feed="iex")
            reg = [b for b in bs if b["t"][:10] == now.strftime("%Y-%m-%d") and b["t"][11:16] >= "09:30"]
            return reg[0]["o"] if reg else None                 # the day's open
        except Exception:
            return None
    return flowdata.flip_snapshot(key, sorted(tickers), now, price)


def flip_check(sub, days=60):
    """Per day and ticker: the flip at 09:20 / 09:32 / 09:40 (as it was then), the stored one (what the backtest
    uses), the open, and whether 'opened above / below the flip' agrees. Summary: how often it agrees."""
    import flowdata
    snaps = flowdata.flip_snapshots()
    by = {}
    for r in snaps:
        by.setdefault((r["date"], r["sym"]), {})[r["hm"]] = r
    rows, agree, n = [], 0, 0
    stored = {}
    num = lambda v: (lambda x: x if x == x else None)(float(v)) if v not in (None, "", "None") else None   # bad / missing -> None
    for (d, sym) in sorted(by, reverse=True)[: days * 4]:
      try:
        if sym not in stored:
            try:
                stored[sym] = flowdata.stored_walls(sym)
            except Exception:
                stored[sym] = {}
        s = by[(d, sym)]
        st = (stored[sym].get(d) or {}) if isinstance(stored[sym], dict) else {}
        early = {h: x for h, x in s.items() if h and h <= "09:40"}          # the morning snapshots (not the ones during the day)
        op = next((num(x.get("price")) for h, x in sorted(early.items()) if num(x.get("price"))), None)
        morning = next((num(early[h].get("flip")) for h in ("09:40", "09:32", "09:20") if h in early and num(early[h].get("flip"))), None)
        sf = num(st.get("flip"))
        side = lambda f: None if (f is None or op is None) else ("above" if op > f else "below")
        ok = side(morning) is not None and side(sf) is not None and side(morning) == side(sf)
        if side(morning) is not None and side(sf) is not None:
            n += 1
            agree += ok
        rows.append({"date": d, "sym": sym, "open": op, "snaps": {h: num((s.get(h) or {}).get("flip")) for h in SNAP_TIMES},
                     "stored": sf, "sideMorning": side(morning), "sideStored": side(sf), "agree": ok if side(morning) and side(sf) else None})
      except Exception as e:                           # one bad record must not break the whole check
        print("flip check row failed", d, sym, e)
    return {"rows": rows[:200], "n": n, "agree": agree, "pct": round(agree / n * 100, 1) if n else None}


# ---------------- market context of one ticker over the last 30 sessions (the box under the day-trading chart)
def context(sub, sym, days=30):
    """Where the price sits in its last `days` sessions: range, hourly and daily trend, support / resistance zones
    (hourly pivots and impulse opens clustered into zones, with how often price reacted there), the volume profile
    (point of control, value area) and the gamma walls, with a plain-English reading of the spot."""
    import gala
    import stockday
    sym = (sym or "QQQ").upper()
    c = stockday.data_creds(sub)
    if not c:
        return {"symbol": sym, "error": "Connect an Alpaca account to load the candles."}
    now = now_ny().replace(tzinfo=None)
    end = now - timedelta(minutes=16)                  # the free SIP plan refuses the last 15 minutes
    h30 = stockday.multi_bars(c, [sym], "30Min", (now - timedelta(days=int(days * 1.6) + 5)).replace(hour=4, minute=0), end, max_pages=10).get(sym) or []
    reg = [b for b in h30 if "09:30" <= b["t"][11:16] < "16:00"]
    ds = sorted({b["t"][:10] for b in reg})[-days:]
    reg = [b for b in reg if b["t"][:10] in ds]
    if len(ds) < 5:
        return {"symbol": sym, "error": "Not enough history for this ticker."}
    dly = stockday.multi_bars(c, [sym], "1Day", now - timedelta(days=60), end, max_pages=2).get(sym) or []
    trs = []
    for i in range(1, len(dly)):
        p = dly[i - 1]["c"]
        trs.append(max(dly[i]["h"] - dly[i]["l"], abs(dly[i]["h"] - p), abs(dly[i]["l"] - p)))
    atr = sum(trs[-14:]) / len(trs[-14:]) if trs else (max(b["h"] for b in reg) - min(b["l"] for b in reg)) / 10
    price = (h30[-1]["c"] if h30 else reg[-1]["c"])
    hi, lo = max(b["h"] for b in reg), min(b["l"] for b in reg)
    H = gala.agg(reg, 60)
    htr = gala.trend(H, 2)
    closes = [b["c"] for b in dly]
    dtr = 0
    if len(closes) >= 21:
        sma = sum(closes[-20:]) / 20
        dtr = 1 if closes[-1] > sma and closes[-1] > closes[-6] else -1 if closes[-1] < sma and closes[-1] < closes[-6] else 0
    # support / resistance zones: hourly levels within a quarter ATR of each other are one zone
    lv = sorted(x["p"] for x in gala.levels(H, {"gPivot": 2, "gImpulse": 1.5, "gMaxCross": 4}, 0.1 * atr))
    zones = []
    for p in lv:
        if zones and p - zones[-1]["hi"] <= 0.25 * atr:
            zones[-1]["hi"] = p
            zones[-1]["n"] += 1
        else:
            zones.append({"lo": p, "hi": p, "n": 1})
    pad = 0.08 * atr
    for z in zones:
        z["lo"], z["hi"] = round(z["lo"] - pad, 4), round(z["hi"] + pad, 4)
        z["touches"] = sum(1 for b in H if b["l"] <= z["hi"] and b["h"] >= z["lo"])
        z["kind"] = "support" if z["hi"] < price else "resistance" if z["lo"] > price else "at price"
        z["dist"] = round(((z["lo"] - price) if z["lo"] > price else (z["hi"] - price) if z["hi"] < price else 0) / atr, 2)
    zones.sort(key=lambda z: abs(z["dist"]))
    zones = zones[:8]
    # volume profile of the 30 sessions (30-minute candles, typical price)
    step = max(atr / 8, price * 0.0005)
    vp = {}
    for b in reg:
        k = round(((b["h"] + b["l"] + b["c"]) / 3) / step)
        vp[k] = vp.get(k, 0) + (b.get("v") or 0)
    poc = va_lo = va_hi = None
    if vp:
        tot = sum(vp.values())
        pk = max(vp, key=vp.get)
        lo_k = hi_k = pk
        acc = vp[pk]
        while acc < 0.7 * tot and (lo_k - 1 in vp or hi_k + 1 in vp):
            up, dn = vp.get(hi_k + 1, -1), vp.get(lo_k - 1, -1)
            if up >= dn:
                hi_k += 1
                acc += max(0, up)
            else:
                lo_k -= 1
                acc += max(0, dn)
        poc, va_lo, va_hi = round(pk * step, 2), round(lo_k * step, 2), round(hi_k * step, 2)
    # gamma walls (the latest day that has them)
    W = {}
    try:
        W = walls_by_day(sub, sym, ds[-5:] + [now.strftime("%Y-%m-%d")], None, budget_s=10)
    except Exception as e:
        print("context walls failed", sym, e)
    wd = max((d for d, w in W.items() if w and (w.get("putWall") or w.get("callWall"))), default=None)
    w = W.get(wd) or {}
    pw, cw, fl = (float(w[k]) if w.get(k) else None for k in ("putWall", "callWall", "flip"))
    notes = []
    near = 0.5 * atr
    if pw and abs(price - pw) <= near:
        notes.append(f"sitting on the put wall {pw:g} (dealer support)")
    elif pw and price < pw:
        notes.append(f"below the put wall {pw:g} (support broken: moves can speed up)")
    if cw and abs(price - cw) <= near:
        notes.append(f"testing the call wall {cw:g} (dealer resistance)")
    elif cw and price > cw:
        notes.append(f"above the call wall {cw:g} (breakout over resistance)")
    if fl:
        notes.append(f"{'above' if price > fl else 'below'} the gamma flip {fl:g} ({'dampened' if price > fl else 'amplified'} moves)")
    sup = next((z for z in zones if z["kind"] == "support"), None)
    res = next((z for z in zones if z["kind"] == "resistance"), None)
    atz = next((z for z in zones if z["kind"] == "at price"), None)
    if atz:
        notes.append(f"inside a {atz['touches']}-touch zone {atz['lo']:.2f}–{atz['hi']:.2f}")
    if poc:
        notes.append("inside the 30-day value area" if va_lo <= price <= va_hi else
                     f"{'above' if price > va_hi else 'below'} the 30-day value area ({va_lo:g}–{va_hi:g})")
    pos = round((price - lo) / (hi - lo) * 100) if hi > lo else 50
    return {"symbol": sym, "price": round(price, 4), "atr": round(atr, 4), "days": len(ds), "from": ds[0], "to": ds[-1],
            "hi": hi, "lo": lo, "pos": pos, "trendH": htr, "trendD": dtr, "zones": zones, "poc": poc, "vaLo": va_lo, "vaHi": va_hi,
            "walls": {"date": wd, "putWall": pw, "callWall": cw, "flip": fl},
            "support": sup, "resistance": res, "notes": notes}
