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
          # exits
          "stopOn": True, "stopAtr": 1.5, "targetOn": True, "targetR": 2.0, "trailOn": False, "trailAtr": 2.0,
          "vwapExit": False, "emaExit": False, "sqExit": False, "inExit": False, "fvgExit": False}
ENTRY = ("aboveVwap", "vwapCross", "aboveEma", "emaStack", "greenBar", "kcNotExt", "kcLower", "sqOn", "sqFire", "sqMom",
         "inGreen", "inNotRed", "fvgTouch", "orbBreak", "pmBreak", "pwTouch", "aboveFlip")
EXITS = ("stopOn", "targetOn", "trailOn", "vwapExit", "emaExit", "sqExit", "inExit", "fvgExit")
INTS = {"maxTrades": (1, 50), "qty": (1, 100000), "maxQty": (1, 100000), "emaLen": (2, 400), "ema2Len": (2, 400),
        "kcLen": (2, 400), "sqLen": (5, 400), "sqFireN": (1, 60), "inN1": (5, 800), "inN2": (5, 800), "inN3": (5, 800),
        "fvgDays": (1, 20), "orbMin": (1, 120)}
FLOATS = {"kcMult": (0.1, 10), "sqBB": (0.1, 10), "sqKC": (0.1, 10), "fvgMin": (0, 10), "fvgNear": (0, 10), "zoneAtr": (0, 20),
          "stopAtr": (0.05, 50), "targetR": (0.1, 50), "trailAtr": (0.05, 50), "riskUsd": (0, 1e7)}


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
    cur = config(sub)
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
    for k in ("enabled", "trade", "longs", "shorts") + ENTRY + EXITS:
        if k in body:
            cur[k] = bool(body[k])
    for k in ("startT", "endT", "flatT"):
        if k in body:
            cur[k] = _hm(body[k], cur[k])
    if "tickers" in body:
        tk = body["tickers"]
        tk = tk.replace(",", " ").split() if isinstance(tk, str) else tk
        cur["tickers"] = [str(t).strip().upper()[:10] for t in (tk or []) if str(t).strip()][:10] or ["QQQ", "SPY"]
    from decimal import Decimal
    db.put({"PK": db.upk(sub), "SK": SK, **{k: Decimal(str(v)) if isinstance(v, float) else v for k, v in cur.items()}})
    return cur


# ---------------- data ----------------

def creds(sub):
    """The day-trading account (falls back to the main Alpaca keys for market data only)."""
    import alpaca
    return alpaca.creds(sub, CRED_SK)


def data_creds(sub):
    import alpaca
    return creds(sub) or alpaca.creds(sub)


def minute_bars(sub, sym, days, now=None):
    """1-minute candles of the last `days` trading days (pre-market included), NY time. [{t,o,h,l,c,v}]"""
    import alpaca
    c = data_creds(sub)
    if not c:
        raise BadRequest("Connect an Alpaca account (Day trading → account) to load minute candles.")
    now = now or now_ny().replace(tzinfo=None)
    start = (now - timedelta(days=int(days * 1.5) + 4)).replace(hour=4, minute=0, second=0, microsecond=0)
    end = now - timedelta(minutes=1) if c.get("feed", "iex") == "iex" else now - timedelta(minutes=16)
    out = alpaca.bars(c, sym, start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S"), 1)
    for b in out:
        b["t"] = b["t"][:16]
    return out


def walls_by_day(sub, sym, dates):
    import flowdata
    w = {}
    try:
        st = flowdata.stored_walls(sym)
        w = {d: st.get(d) for d in dates if st.get(d)}
    except Exception as e:
        print("day walls failed", sym, e)
    today = now_ny().strftime("%Y-%m-%d")
    if today in dates and today not in w:
        try:
            g = flowdata.gex_day(flowdata._key(sub), sym, today, True)
            if g:
                w[today] = {k: g.get(k) for k in ("callWall", "putWall", "flip")}
        except Exception as e:
            print("day walls today failed", sym, e)
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


def indicators(bars, ext, P):
    """bars: regular-session 1-minute candles; ext: all candles (pre-market for the pre-market high / low)."""
    import wallwatch as ww
    cl = [b["c"] for b in bars]
    atr = ww.atr14(bars)
    pm = {}
    for b in ext:
        if "04:00" <= b["t"][11:16] < "09:30":
            d = b["t"][:10]
            h, l = pm.get(d, (None, None))
            pm[d] = (max(h, b["h"]) if h is not None else b["h"], min(l, b["l"]) if l is not None else b["l"])
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
    return {"vwap": _session_vwap(bars), "ema": ww._ema_series(cl, int(P["emaLen"])), "ema2": ww._ema_series(cl, int(P["ema2Len"])),
            "kc": ww.keltner(bars, int(P["kcLen"]), float(P["kcMult"])),
            "sq": ww.squeeze(bars, int(P["sqLen"]), float(P["sqBB"]), float(P["sqKC"]), int(P["sqFireN"])),
            "inr": ww.inertia_hourly(bars, (int(P["inN1"]), int(P["inN2"]), int(P["inN3"]))),
            "atr": atr, "pm": pm, "orb": orb, "fvgBull": bt, "fvgBear": brt, "newBull": nb, "newBear": nbr}


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
    return out


def _r(s):
    side = s["side"]
    risk = abs(s["entry"] - s["stop0"])
    ex = s.get("exit") if s["status"] != "open" else s["last"]
    pnl = (ex - s["entry"]) * side
    return round(pnl / s["entry"] * 100, 3), (round(pnl / risk, 2) if risk > 0 else None)


def detect(bars_all, P, walls=None, start=None):
    """Signals on 1-minute candles. bars_all: every candle (pre-market included). Returns [signal]."""
    P = {**PARAMS, **(P or {})}
    walls = walls or {}
    on = lambda k: bool(P.get(k))
    bars = [b for b in bars_all if "09:30" <= b["t"][11:16] < "16:00"]
    if len(bars) < 30:
        return []
    I = indicators(bars, bars_all, P)
    rules = [k for k in ENTRY if on(k)]
    sides = ([1] if on("longs") else []) + ([-1] if on("shorts") else [])
    st_t, en_t, fl_t = P["startT"], P["endT"], P["flatT"]
    sigs, open_, per_day = [], None, {}
    for i, b in enumerate(bars):
        day, hm = b["t"][:10], b["t"][11:16]
        last_of_day = hm >= "15:59" or (i + 1 < len(bars) and bars[i + 1]["t"][:10] != day)   # end of data != end of day (live)
        if open_:
            s = open_
            side, a = s["side"], I["atr"][i] or s["atr"]
            exit_ = None
            if on("stopOn") and ((b["l"] <= s["stop"]) if side > 0 else (b["h"] >= s["stop"])):
                px = min(b["o"], s["stop"]) if side > 0 else max(b["o"], s["stop"])
                exit_ = ("trail" if s.get("trailed") else "stop", px)
            elif s.get("target") and ((b["h"] >= s["target"]) if side > 0 else (b["l"] <= s["target"])):
                px = max(b["o"], s["target"]) if side > 0 else min(b["o"], s["target"])
                exit_ = ("target", px)
            else:
                c = b["c"]
                vw, em, sq, ir = I["vwap"][i], I["ema"][i], I["sq"][i], I["inr"][i]
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
                elif hm >= fl_t or last_of_day:
                    exit_ = ("time", c)
            if exit_:
                s.update(status=exit_[0], exitAt=b["t"], exit=round(exit_[1], 4))
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
            continue
        if not rules or not sides or hm < st_t or hm >= en_t or last_of_day or (start and day < start):
            continue
        if per_day.get(day, 0) >= int(P["maxTrades"]):
            continue
        a = I["atr"][i] or 0
        if not a:
            continue
        w = walls.get(day) or {}
        prev = bars[i - 1] if i else None
        for side in sides:
            ck = _checks(i, b, prev, I, w, side, P)
            if all(ck[k] for k in rules):
                e = b["c"]
                st = round(e - side * float(P["stopAtr"]) * a, 4)
                tgt = round(e + side * float(P["targetR"]) * abs(e - st), 4) if on("targetOn") else None
                s = {"at": b["t"], "kind": "day", "side": side, "dir": "long" if side > 0 else "short", "entry": e,
                     "stop0": st, "stop": st if on("stopOn") else (0.0 if side > 0 else 1e12), "target": tgt, "atr": round(a, 4),
                     "status": "open", "last": e, "lastAt": b["t"], "o": b["o"], "h": b["h"], "l": b["l"],
                     "rules": rules, "level": st, "levelName": "stop"}
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
    P = {**config(sub), **(cfg or {})}
    days = max(1, min(30, int(days or 3)))
    allb = minute_bars(sub, sym, days + 3)
    if not allb:
        return {"symbol": sym, "bars": [], "error": "No minute candles."}
    dates = sorted({b["t"][:10] for b in allb})
    show = dates[-days:]
    W = walls_by_day(sub, sym, show)
    sigs = detect(allb, P, W, show[0])
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
    return {"symbol": sym, "bars": out_bars, "interval": tfm, "days": days, "dt": True,
            "vwap": series("vwap"), "ema": series("ema"), "ema2": series("ema2"), "emaLen": int(P["emaLen"]),
            "kch": [{"t": t, "m": r4(I["kc"][i][0]), "u": r4(I["kc"][i][1]), "l": r4(I["kc"][i][2])} for t, i in pick if I["kc"][i]],
            "inertia": [{"t": t, "s": I["inr"][i]["s"], "pts": 0} for t, i in pick if I["inr"][i]],
            "sqzH": [{"t": t, "on": I["sq"][i]["on"], "f": I["sq"][i]["fired"], "m": I["sq"][i]["mom"], "u": I["sq"][i]["up"]} for t, i in pick if I["sq"][i]],
            "pm": {d: {"h": I["pm"][d][0], "l": I["pm"][d][1]} for d in show if d in I["pm"]},
            "orbLv": {d: {"h": x[0], "l": x[1]} for d in show for x in [next((I["orb"][i] for i in keep if reg[i]["t"][:10] == d and I["orb"][i]), None)] if x},
            "walls": walls, "signals": sigs, "stats": stats(sigs), "alerts": [], "evals": [], "maxPain": [], "expirations": [],
            "trades": [], "wallsNow": W.get(show[-1]), "cfgAt": P.get("savedAt")}


def backtest(sub, days=20, cfg=None):
    P = {**config(sub), **(cfg or {})}
    days = max(2, min(60, int(days or 20)))
    rows, per, errors = [], {}, []
    for sym in P["tickers"]:
        try:
            allb = minute_bars(sub, sym, days + 2)
            dates = sorted({b["t"][:10] for b in allb})
            show = dates[-days:]
            sg = detect(allb, P, walls_by_day(sub, sym, show), show[0] if show else None)
            for s in sg:
                s["sym"] = sym
            rows += sg
            per[sym] = stats(sg)
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:120]}")
    rows.sort(key=lambda s: s["at"])
    byday = {}
    for s in rows:
        if s.get("R") is not None:
            byday[s["at"][:10]] = round(byday.get(s["at"][:10], 0) + s["R"], 2)
    return {"at": iso(now_ny()), "days": days, "all": stats(rows), "byTicker": per, "byDay": byday,
            "trades": [{k: s.get(k) for k in ("sym", "at", "dir", "entry", "exit", "exitAt", "status", "R", "pct")} for s in rows][-300:],
            "errors": errors}


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
    for sym in P["tickers"]:
        try:
            allb = [b for b in minute_bars(sub, sym, 3, now) if b["t"] <= (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")]
            sigs = detect(allb, P, walls_by_day(sub, sym, [today]), today)
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
