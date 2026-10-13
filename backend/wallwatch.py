"""Wall watchlist: two simple setups checked every hour on a fixed list of tickers.

Setups (on each finished hourly candle; walls and flip of the day, Unusual Whales, from that day's open interest):
  pw:   call wall > gamma flip > put wall, and a green candle crossing the put wall (low at or under it, close above it)
  flip: call wall > put wall > gamma flip, and a green candle crossing the flip (the flip acts as support)
Trade: entry = that candle's close, target = the call wall of that day,
stop = the level crossed (put wall or flip) - stopAtr x the 30-day daily ATR, hit intrabar;
trailing stop (trailAtr > 0): trailAtr x the entry ATR under the highest high since entry, never down.
One open signal per ticker at a time. Nothing is traded: signals are only recorded and followed.

Items: WWCFG (tickers, enabled), WWSIG#<ticker>#<bar time> (signals), WWRUN (last run)."""
import time
from datetime import datetime, timedelta

import db
from util import iso, now_ny

DEFAULT = ["AAPL", "ABNB", "AMAT", "AMD", "AMZN", "AVGO", "AXP", "BA", "BABA", "BE", "BRK.B", "CAT", "CVNA", "CVX", "DDOG",
           "DELL", "DIA", "GE", "GM", "GOOGL", "IBM", "JPM", "KTOS", "LHX", "LRCX", "META", "MSFT", "MSTR", "MU", "NFLX",
           "NVDA", "OKLO", "ORCL", "PANW", "PLTR", "QCOM", "QQQ", "ROKU", "RTX", "SBUX", "SFM", "SMH", "SPCX", "SPOT", "SPY",
           "STX", "TSLA", "TSM", "WDC", "WMT", "XOM"]


PARAMS = {"stopAtr": 0.5, "trailAtr": 1.0, "pwSetup": True, "flipSetup": True, "tgtBuf": 0.5, "mergedSetup": True,
          "retestSetup": True, "retestMin": 500000, "retestDays": 5, "aboveFlip": False, "retestZone": 0.25,
          "flipExit": True, "earnDays": 10, "vwapDays": 10, "cwRoom": 1.0,
          # rule switches (on/off) for the flow retest
          "usePW": True, "useFlip": True, "greenOnly": True, "noAboveCW": True, "cwRoomOn": True, "earnOn": True,
          "vwapOn": False, "emaOn": False, "emaLen": 20, "stopOn": True, "trailOn": True, "targetOn": True,
          "vwapExit": False, "emaExit": False, "emaOverVwap": False,
          "spyUp": False, "kcNotExt": False, "orbAbove": False, "kcLen": 21, "kcMult": 3.0,
          "skewOn": False}       # skewOn: entry only when the 25-delta skew is in the middle third of its last 6 months   # tgtBuf: % under the call wall
OPTS = {"autoAdd": True, "trade": True, "flowRule": None}   # flowRule: index in FLOW_BUCKETS a signal must pass to be traded        # add unusual-flow tickers to the list; buy the signals (Alpaca paper)


def config(sub):
    c = db.get(db.upk(sub), "WWCFG") or {}
    out = {"tickers": c.get("tickers") or DEFAULT, "enabled": c.get("enabled", True)}
    out.update({k: c.get(k, v) for k, v in PARAMS.items()})
    out.update({k: c.get(k, v) for k, v in OPTS.items()})
    out["added"] = c.get("added") or {}                 # {ticker: {"at", "by", "premium"}} for tickers added from the flow
    if FLOW_ONLY:                                       # only the flow retest setup is used for now
        out.update(pwSetup=False, flipSetup=False, mergedSetup=False, retestSetup=True)
    return out


FLOW_ONLY = True


def version(cfg):
    """Signals made with other settings are recomputed."""
    return (f"7|{float(cfg['stopAtr']):g}|{float(cfg['trailAtr']):g}|{int(bool(cfg['pwSetup']))}{int(bool(cfg['flipSetup']))}"
            f"{int(bool(cfg.get('mergedSetup', True)))}|{float(cfg.get('tgtBuf', 0.5)):g}"
            f"|{int(bool(cfg.get('retestSetup', True)))}|{float(cfg.get('retestMin', 500000)):g}|{int(cfg.get('retestDays', 5))}|r4|{int(bool(cfg.get('aboveFlip', False)))}|{float(cfg.get('retestZone', 0.25)):g}|{int(bool(cfg.get('flipExit', True)))}|{int(cfg.get('earnDays', 10))}|cw2|v{int(cfg.get('vwapDays', 10))}|room{float(cfg.get('cwRoom', 1.0)):g}"
            f"|sw{''.join(str(int(bool(cfg.get(k, PARAMS[k])))) for k in SWITCHES)}|e{int(cfg.get('emaLen', 20))}|kc{int(cfg.get('kcLen', 21))}x{float(cfg.get('kcMult', 3.0)):g}")


SWITCHES = ("usePW", "useFlip", "greenOnly", "noAboveCW", "cwRoomOn", "earnOn", "vwapOn", "emaOn", "stopOn", "trailOn", "targetOn", "vwapExit", "emaExit", "emaOverVwap", "spyUp", "kcNotExt", "orbAbove", "skewOn")


def save_config(sub, body):
    cur = config(sub)
    tk = body.get("tickers")
    if isinstance(tk, str):
        tk = tk.replace(",", " ").split()
    if isinstance(tk, list):
        seen, out = set(), []
        for t in tk:
            t = str(t).strip().upper().replace("/", ".")
            if t and t not in seen and len(t) <= 10:
                seen.add(t)
                out.append(t)
        cur["tickers"] = out[:150]
    if "enabled" in body:
        cur["enabled"] = bool(body["enabled"])
    before = version(cur)
    for k, lo_, hi_ in (("retestMin", 50_000, 100_000_000), ("retestDays", 1, 20), ("earnDays", 0, 60), ("vwapDays", 1, 30), ("emaLen", 2, 200), ("kcLen", 2, 200)):
        if k in body:
            try:
                cur[k] = int(max(lo_, min(hi_, float(body[k]))))
            except (TypeError, ValueError):
                pass
    for k in ("stopAtr", "trailAtr", "tgtBuf", "retestZone", "cwRoom", "kcMult"):
        if k in body:
            try:
                cur[k] = max(0.0, min(10.0, round(float(body[k]), 2)))
            except (TypeError, ValueError):
                pass
    for k in ("pwSetup", "flipSetup", "mergedSetup", "retestSetup", "autoAdd", "trade", "aboveFlip", "flipExit") + SWITCHES:
        if k in body:
            cur[k] = bool(body[k])
    if "flowRule" in body:
        try:
            v = body["flowRule"]
            cur["flowRule"] = None if v in (None, "", -1, "-1") else int(v)
        except (TypeError, ValueError):
            pass
    if body.get("add"):                                  # add one ticker
        t = str(body["add"]).strip().upper().replace("/", ".")[:10]
        if t and t not in cur["tickers"]:
            cur["tickers"] = cur["tickers"] + [t]
            cur["added"] = {**cur.get("added", {}), t: {"at": iso(now_ny()), "by": body.get("addedBy") or "you",
                                                       **({"premium": round(body["premium"])} if body.get("premium") else {})}}
    if body.get("remove"):
        t = str(body["remove"]).strip().upper()
        cur["tickers"] = [x for x in cur["tickers"] if x != t]
    from decimal import Decimal
    db.put({"PK": db.upk(sub), "SK": "WWCFG", **{k: Decimal(str(v)) if isinstance(v, float) else v for k, v in cur.items()}})
    if version(cur) != before:                          # new settings: the 45-day history is recomputed from scratch
        run_ = db.get(db.upk(sub), "WWRUN") or {}
        if run_:
            db.put({**run_, "bfDone": []})
    return cur


def _bar_end(t):
    d = datetime.strptime(t[:16], "%Y-%m-%dT%H:%M")
    return min(d + timedelta(minutes=60), d.replace(hour=16, minute=0))


def finished(bars, now):
    """Only hourly candles that are over (Yahoo includes the one in progress)."""
    return [b for b in bars if "09:30" <= b["t"][11:16] < "16:00" and _bar_end(b["t"]) <= now]


def setup_ok(w):
    cw, fl, pw = (w or {}).get("callWall"), (w or {}).get("flip"), (w or {}).get("putWall")
    return bool(cw and fl and pw and cw > fl > pw)


def setup_kind(w):
    """'pw' when call wall > flip > put wall, 'flip' when call wall > put wall > flip,
    'merged' when the put wall and the call wall are the same strike and the flip is above it, else None."""
    cw, fl, pw = (w or {}).get("callWall"), (w or {}).get("flip"), (w or {}).get("putWall")
    if not (cw and fl and pw):
        return None
    cw, fl, pw = float(cw), float(fl), float(pw)
    if abs(cw - pw) <= cw * 0.0005 and fl > cw:
        return "merged"
    return "pw" if cw > fl > pw else "flip" if cw > pw > fl else None


def atr14(bars):
    out, tr0 = [], 0.0
    for i, b in enumerate(bars):
        pc = bars[i - 1]["c"] if i else b["o"]
        tr = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
        tr0 = (tr0 * i + tr) / (i + 1) if i < 14 else (tr0 * 13 + tr) / 14
        out.append(tr0)
    return out


ATR_DAYS = 30


def daily_atr(daily, n=ATR_DAYS):
    """{date: ATR} = average true range of the n trading days before that date (known at its open)."""
    daily = sorted(daily, key=lambda b: b["t"])
    trs, out = [], {}
    for i, b in enumerate(daily):
        d = b["t"][:10]
        if len(trs) >= n:
            out[d] = sum(trs[-n:]) / n
        pc = daily[i - 1]["c"] if i else b["o"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    if trs and len(trs) >= n:                           # the next session (today when the last daily bar is yesterday)
        out["next"] = sum(trs[-n:]) / n
    return out


def _atr_on(datr, day):
    if day in datr:
        return datr[day]
    later = [d for d in datr if d != "next" and d > day]
    return None if later else datr.get("next")


def detect(bars, walls, start=None, cfg=None, datr=None):
    """Signals and their outcome. bars: finished hourly candles (earlier ones warm up the ATR);
    walls: {date: {callWall, flip, putWall}}; start: first date where signals can start; cfg: stop / trail / setups."""
    P = {**PARAMS, **(cfg or {})}
    k_stop, k_trail, buf = float(P["stopAtr"]), float(P["trailAtr"]), float(P.get("tgtBuf") or 0)
    kinds = {k for k, on in (("pw", P["pwSetup"]), ("flip", P["flipSetup"]), ("merged", P.get("mergedSetup", True))) if on}
    ver = version(P)
    sigs, open_ = [], None
    atr = atr14(bars)                                  # fallback when the 30-day daily ATR isn't available
    for i, b in enumerate(bars):
        if open_:                                      # follow the open signal first
            s = open_
            if s["kind"] == "merged":                  # merged walls: the stop follows the call wall of each new day, up only
                wd = walls.get(b["t"][:10]) or {}
                if wd.get("callWall") and b["t"][:10] > s["at"][:10]:
                    lvl = round(float(wd["callWall"]) - k_stop * s["atr"], 4)
                    if lvl > s["stop"]:
                        s["stop"] = lvl
                        s.setdefault("stops", [[s["at"], s["stop0"]]]).append([b["t"][:16], lvl])
            if b["l"] <= s["stop"]:                    # stop order (first stop, or trailed up)
                s.update(status="trail" if s["stop"] > s["stop0"] else "stop", exitAt=b["t"][:16], exit=round(min(b["o"], s["stop"]), 4))
            elif s.get("target") and b["h"] >= s["target"]:
                s.update(status="target", exitAt=b["t"][:16], exit=round(max(b["o"], s["target"]), 4))
            else:
                s["last"], s["lastAt"] = b["c"], b["t"][:16]
                s["mfe"] = max(s.get("mfe", s["entry"]), b["h"])
                s["mae"] = min(s.get("mae", s["entry"]), b["l"])
                if k_trail > 0 and s["kind"] != "merged":   # trailing stop: k x entry ATR under the highest high
                    lvl = round(s["mfe"] - k_trail * s["atr"], 4)
                    if lvl > s["stop"]:
                        s["stop"] = lvl
                        s.setdefault("stops", [[s["at"], s["stop0"]]]).append([b["t"][:16], lvl])
            if s["status"] != "open":
                open_ = None
            continue
        if start and b["t"][:10] < start:
            continue
        w = walls.get(b["t"][:10])
        kind = setup_kind(w)
        if kind not in kinds:
            continue
        pw, cw, fl = float(w["putWall"]), float(w["callWall"]), float(w["flip"])
        lvl = pw if kind == "pw" else fl if kind == "flip" else cw   # the level the green candle has to cross
        if b["c"] > b["o"] and b["l"] <= lvl < b["c"]:
            a_ = (_atr_on(datr, b["t"][:10]) if datr else None) or atr[i]
            st = round(lvl - k_stop * a_, 4)
            s = {"at": b["t"][:16], "kind": kind, "level": lvl, "entry": b["c"], "pw": pw, "flip": fl, "cw": cw,
                 "target": None if kind == "merged" else round(cw * (1 - buf / 100), 4),   # merged: no target, the stop trails
                 "atr": round(a_, 4), "stop": st, "stop0": st, "v": ver,
                 "o": b["o"], "h": b["h"], "l": b["l"], "status": "open", "last": b["c"], "lastAt": b["t"][:16]}
            sigs.append(s)
            open_ = s
    for s in sigs:
        ex = s.get("exit") if s["status"] != "open" else s["last"]
        s["pct"] = round((ex / s["entry"] - 1) * 100, 2)
        s["tgtPct"] = round((s["target"] / s["entry"] - 1) * 100, 2) if s.get("target") else None
        s["riskPct"] = round((s["entry"] - s["stop0"]) / s["entry"] * 100, 2)
        s["R"] = round(s["pct"] / s["riskPct"], 2) if s["riskPct"] > 0 else None
    return sigs


RETEST_NEAR = 0.3     # % above the support level that counts as a retest
RETEST_AWAY = 0.5     # x ATR: price has to close this far above the support (after the alert) before a retest counts


def _step(s, b, walls, k_trail, flip_exit, is_last, vw=None, em=None):
    """One hourly candle of an open retest trade: stop / target, trailing, and the daily close under the flip.
    Backtest only: fixT (entry + N x risk) and maxDays (sell at the close of the Nth day held)."""
    if s.get("maxDays") and b["t"][:10] != s.get("_dd"):
        s["_dd"] = b["t"][:10]
        s["_dn"] = s.get("_dn", -1) + 1
    if b["l"] <= s["stop"]:
        s.update(status="trail" if s.get("stops") else "stop", exitAt=b["t"][:16], exit=round(min(b["o"], s["stop"]), 4))
        return
    if s.get("target") and b["h"] >= s["target"]:
        s.update(status="target", exitAt=b["t"][:16], exit=round(max(b["o"], s["target"]), 4))
        return
    if s.get("fixT") and b["h"] >= s["fixT"]:
        s.update(status="R", exitAt=b["t"][:16], exit=round(max(b["o"], s["fixT"]), 4))
        return
    if s.get("maxDays") and is_last and s.get("_dn", 0) >= int(s["maxDays"]):
        s.update(status="time", exitAt=b["t"][:16], exit=b["c"])
        return
    fl = (walls.get(b["t"][:10]) or {}).get("flip")
    if flip_exit and is_last and s.get("aboveFlipAtEntry") and fl and b["c"] < float(fl):
        s.update(status="flip", exitAt=b["t"][:16], exit=b["c"])          # the day closed under the gamma flip
        return
    if s.get("vwapExit") and vw is not None and b["c"] < vw:
        s.update(status="vwap", exitAt=b["t"][:16], exit=b["c"])          # hourly close under the VWAP
        return
    if s.get("emaExit") and em is not None and b["c"] < em:
        s.update(status="ema", exitAt=b["t"][:16], exit=b["c"])           # hourly close under the EMA
        return
    s["last"], s["lastAt"] = b["c"], b["t"][:16]
    s["mfe"] = max(s.get("mfe", s["entry"]), b["h"])
    s["mae"] = min(s.get("mae", s["entry"]), b["l"])
    if k_trail > 0:
        lvl = round(s["mfe"] - k_trail * s["atr"], 4)
        if lvl > s["stop"]:
            s["stop"] = lvl
            s.setdefault("stops", [[s["at"], s["stop0"]]]).append([b["t"][:16], lvl])


def _result(s):
    ex = s.get("exit") if s["status"] != "open" else s["last"]
    pct = round((ex / s["entry"] - 1) * 100, 2)
    risk = round((s["entry"] - s["stop0"]) / s["entry"] * 100, 2)
    return pct, (round(pct / risk, 2) if risk > 0 else None)


VWAP_DAYS = 10     # the VWAP restarts on the hourly candle with the highest volume of the last 10 trading days


def anchored_vwap(bars, days=VWAP_DAYS):
    """VWAP anchored at the last candle whose volume is the highest of the previous `days` trading days.
    Returns (values per candle, indexes of the reset candles)."""
    out, resets, pv, vv = [], [], 0.0, 0.0
    day_of = [b["t"][:10] for b in bars]
    for i, b in enumerate(bars):
        v = float(b.get("v") or 0)
        seen, j, hi = set(), i - 1, 0.0
        while j >= 0:                                  # candles of the previous `days` trading days (and earlier today)
            if day_of[j] != day_of[i]:
                seen.add(day_of[j])
                if len(seen) > days:
                    break
            hi = max(hi, float(bars[j].get("v") or 0))
            j -= 1
        enough = len(seen) >= days                     # a full window is needed before a reset counts
        if i == 0 or (enough and v > hi):
            pv, vv = 0.0, 0.0
            resets.append(i)
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * v
        vv += v
        out.append(round(pv / vv, 4) if vv else round(tp, 4))
    return out, resets


def _flip_chg(walls, day):
    """% change of the flip from the previous stored day (None when unknown)."""
    days = sorted(d for d in walls if d < day and (walls[d] or {}).get("flip"))
    cur = (walls.get(day) or {}).get("flip")
    if not days or not cur:
        return None
    prev = float(walls[days[-1]]["flip"])
    return round((float(cur) - prev) / prev * 100, 2) if prev else None


def sig_sk(sym, s):
    return f"WWSIG#{sym}#{s['at']}" + ("#R" if s.get("kind") == "retest" else "")


def _ema_series(xs, n):
    """Exponential moving average of the closes (None until n values)."""
    out, e, k = [], None, 2 / (n + 1)
    for i, x in enumerate(xs):
        if i + 1 < n:
            out.append(None)
            continue
        e = sum(xs[i + 1 - n:i + 1]) / n if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def keltner(bars, n=21, mult=3.0):
    """Keltner channel: EMA(n) of the close +/- mult x EMA(n) of the true range. [(mid, upper, lower) or None]."""
    closes = [b["c"] for b in bars]
    tr = [max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"])) if i else b["h"] - b["l"]
          for i, b in enumerate(bars)]
    mid, rng = _ema_series(closes, n), _ema_series(tr, n)
    return [(round(m, 4), round(m + mult * r, 4), round(m - mult * r, 4)) if m is not None and r is not None else None
            for m, r in zip(mid, rng)]


def orb_levels(bars):
    """Opening range = the 09:30-10:30 hourly candle of the day: [(high, low) known after that candle, else None]."""
    out, cur, day = [], None, None
    for b in bars:
        if b["t"][:10] != day:
            day, cur = b["t"][:10], None
        if b["t"][11:16] == "09:30":
            out.append(None)
            cur = (b["h"], b["l"])
        else:
            out.append(cur)
    return out


_SPY = {}


def spy_trend(start, now=None):
    """{hourly candle 'YYYY-MM-DDTHH:MM': 1 / -1}: SPY at that candle's close vs SPY's close 6 sessions before that day
    (the TOS 'ttmTrend' of SPY on daily closes, 6 bars). Cached for the hour."""
    from charts import _yahoo
    now = now or now_ny().replace(tzinfo=None)
    key = now.strftime("%Y-%m-%dT%H")
    if key in _SPY:
        return _SPY[key]
    start = now - timedelta(days=725)                  # one fetch covers every chart / backtest (hourly: ~2 years max)
    try:
        hb = finished(_yahoo("SPY", "1h", start, now + timedelta(days=1)) or [], now)
        db_ = [b for b in (_yahoo("SPY", "1d", start - timedelta(days=20), now + timedelta(days=1)) or []) if b.get("c")]
    except Exception as e:
        print("spy trend failed", e)
        return {}
    dates = [b["t"][:10] for b in db_]
    closes = {b["t"][:10]: b["c"] for b in db_}
    out, j = {}, 0
    for b in hb:
        d = b["t"][:10]
        while j < len(dates) and dates[j] < d:
            j += 1
        if j >= 6:
            out[b["t"][:16]] = 1 if b["c"] - closes[dates[j - 6]] >= 0 else -1
    _SPY.clear()
    _SPY[key] = out
    return out


def detect_retest(bars, walls, start=None, cfg=None, datr=None, alerts=None, earnings=None, vwap_map=None, ema_map=None,
                  extra=None):
    """Flow retest: a call alert of at least retestMin arms the ticker (a new alert re-arms it). The support is the highest
    of put wall / flip at or under the price at the close of the alert's candle. From that candle on, within
    retestDays trading days: a green hourly candle whose low reaches the support (within 0.3%) and that closes above it
    is the entry. An hourly close under the stop before the entry ends the watch. Target: the call wall of the day if it's above the entry (minus the buffer), else trailing only.
    Returns (signals, current watch or None)."""
    P = {**PARAMS, **(cfg or {})}
    k_stop, k_trail, buf = float(P["stopAtr"]), float(P["trailAtr"]), float(P.get("tgtBuf") or 0)
    vmin, ndays = float(P.get("retestMin") or 500000), int(P.get("retestDays") or 5)
    zone_atr = float(P.get("retestZone") if P.get("retestZone") is not None else 0.25)
    cw_room = float(P.get("cwRoom") if P.get("cwRoom") is not None else 1.0) if P.get("cwRoomOn", True) else 0.0
    on = lambda k: bool(P.get(k, PARAMS[k]))
    if not on("trailOn"):
        k_trail = 0.0
    if not on("earnOn"):
        earnings = None
    sup_keys = tuple(x for x, k in ((("putWall", "put wall"), "usePW"), (("flip", "flip"), "useFlip")) if on(k))
    ema = [ema_map.get(b["t"][:16]) for b in bars] if ema_map else _ema_series([b["c"] for b in bars], int(P.get("emaLen") or 20))
    extra = extra or {}
    kc = [extra["kc"].get(b["t"][:16]) for b in bars] if extra.get("kc") else keltner(bars, int(P.get("kcLen") or 21), float(P.get("kcMult") or 3))
    orb = orb_levels(bars)
    spy = extra.get("spy") or {}
    skp = extra.get("skewPct") or {}                    # {date: percentile of the previous session's skew}
    flip_exit, earn_days = bool(P.get("flipExit", True)), int(P.get("earnDays") if P.get("earnDays") is not None else 10)
    earn_d = None
    try:
        earn_d = datetime.strptime(str(earnings)[:10], "%Y-%m-%d") if earnings else None
    except ValueError:
        earn_d = None
    ver = version(P)
    atr = atr14(bars)
    if vwap_map:                                       # computed on a longer history by the caller
        vwap = [vwap_map.get(b["t"][:16]) for b in bars]
    else:
        vwap, _ = anchored_vwap(bars, int(P.get("vwapDays") or VWAP_DAYS))
    big = sorted((a for a in (alerts or []) if a.get("c") and (a.get("p") or 0) >= vmin), key=lambda a: a["t"])
    sigs, open_, watch, ai = [], None, None, 0
    for i, b in enumerate(bars):
        day = b["t"][:10]
        a_ = (_atr_on(datr, day) if datr else None) or atr[i]
        end = _bar_end(b["t"]).strftime("%Y-%m-%dT%H:%M")
        arm = None
        prev_end = _bar_end(bars[i - 1]["t"]).strftime("%Y-%m-%dT%H:%M") if i else (datetime.strptime(b["t"][:16], "%Y-%m-%dT%H:%M") - timedelta(hours=18)).strftime("%Y-%m-%dT%H:%M")
        while ai < len(big) and big[ai]["t"] <= end:  # call alerts printed since the previous candle closed
            if big[ai]["t"] > prev_end:
                arm = big[ai]
            ai += 1
        if open_:
            _step(open_, b, walls, k_trail, flip_exit, is_last=(i + 1 == len(bars) or bars[i + 1]["t"][:10] != day), vw=vwap[i], em=ema[i])
            if open_["status"] != "open":
                open_ = None
            continue
        w = walls.get(day) or {}
        if arm:                                        # call flow (new or again): the support is the level at / under price
            lv = [(float(w[k]), n) for k, n in sup_keys if w.get(k)]   # the call wall is the target, never the support
            under = [x for x in lv if x[0] <= b["c"] * (1 + RETEST_NEAR / 100)]
            if under:
                lvl0, name = max(under)
                watch = {"armedAt": arm["t"], "armPremium": arm["p"], "level": lvl0, "levelName": name, "day0": day, "days": 0,
                         "since": b["t"][:16]}
        if not watch:
            continue
        if day != watch.get("_d"):
            watch["_d"] = day
            if day != watch["day0"]:
                watch["days"] += 1
        if watch["days"] > ndays:
            watch = None
            continue
        if b["c"] < watch["level"] - k_stop * a_:     # the support of the alert broken before any retest
            watch = None
            continue
        # the support follows the price: the highest put wall / flip of the day under this candle's close
        lv = [(float(w[k]), n) for k, n in sup_keys if w.get(k)]   # the call wall is the target, never the support
        under = [x for x in lv if x[0] < b["c"]]
        if not under:
            continue
        lvl, lname = max(under)
        zone = max(lvl * RETEST_NEAR / 100, zone_atr * a_)
        # entry: a green candle whose low comes back to the support (within the zone) and that closes above it
        if (b["c"] > b["o"] or not on("greenOnly")) and b["l"] <= lvl + zone:
            if P.get("aboveFlip") and not (w.get("flip") and b["c"] > float(w["flip"])):
                continue                               # only above the gamma flip (positive gamma): wait for another candle
            if earn_d and earn_days and 0 <= (earn_d - datetime.strptime(day, "%Y-%m-%d")).days <= earn_days:
                continue                               # earnings within the next N days: no entry
            cw_ = float(w["callWall"]) if w.get("callWall") else None
            if on("vwapOn") and not (vwap[i] is not None and b["c"] > vwap[i]):
                continue                               # rule on: the close must be above the VWAP
            if on("emaOn") and not (ema[i] is not None and b["c"] > ema[i]):
                continue                               # rule on: the close must be above the EMA
            if on("emaOverVwap") and not (ema[i] is not None and vwap[i] is not None and ema[i] > vwap[i]):
                continue                               # rule on: the EMA must be above the VWAP
            if on("skewOn") and not (skp.get(day) is not None and 33.4 <= skp[day] < 66.7):
                continue                               # rule on: skew must be normal (middle third of its last 6 months)
            if on("spyUp") and spy.get(b["t"][:16]) == -1:
                continue                               # rule on: SPY must be up over the last 6 sessions
            if on("kcNotExt") and not (kc[i] and b["c"] < kc[i][1]):
                continue                               # rule on: the close must be under the upper Keltner band
            if on("orbAbove") and not (orb[i] and b["c"] > orb[i][0]):
                continue                               # rule on: the close must be above the opening range high
            if on("noAboveCW") and cw_ and (b["o"] > cw_ or b["c"] > cw_):
                continue                               # opening or closing above the call wall: no entry
            if cw_ and cw_room and cw_ - b["c"] < cw_room * a_:
                continue                               # too close under the call wall: not enough room to the target
            if start and day < start:
                watch = None
                continue
            watch["levelName"] = lname
            cw = float(w["callWall"]) if w.get("callWall") else None
            tgt = round(cw * (1 - buf / 100), 4) if on("targetOn") and cw and cw * (1 - buf / 100) > b["c"] * 1.002 else None
            st = round(lvl - k_stop * a_, 4)              # stop0: the risk unit for R, even with the stop off
            s = {"at": b["t"][:16], "kind": "retest", "level": lvl, "levelName": watch["levelName"], "armedAt": watch["armedAt"],
                 "armPremium": watch["armPremium"], "entry": b["c"], "pw": float(w["putWall"]) if w.get("putWall") else None,
                 "flip": float(w["flip"]) if w.get("flip") else None, "cw": cw, "target": tgt, "atr": round(a_, 4),
                 "stop": st if on("stopOn") else 0.0, "stop0": st, "v": ver, "o": b["o"], "h": b["h"], "l": b["l"], "status": "open",
                 "last": b["c"], "lastAt": b["t"][:16], "i0": i,
                 "aboveFlipAtEntry": bool(w.get("flip") and b["c"] > float(w["flip"])), "flipChg": _flip_chg(walls, day),
                 "vwap": vwap[i], "ema": round(ema[i], 4) if ema[i] is not None else None,
                 "vwapExit": on("vwapExit"), "emaExit": on("emaExit"),
                 "skewPct": skp.get(day), "kc": list(kc[i]) if kc[i] else None, "orb": list(orb[i]) if orb[i] else None, "spy": spy.get(b["t"][:16])}
            if P.get("fixR"):                          # backtest: fixed target at N x the risk
                s["fixT"] = round(b["c"] + float(P["fixR"]) * (b["c"] - st), 4)
            if P.get("maxDays"):
                s["maxDays"], s["_dd"], s["_dn"] = int(P["maxDays"]), day, 0
            sigs.append(s)
            open_, watch = s, None
    for s in sigs:
        s["pct"], s["R"] = _result(s)
        s["tgtPct"] = round((s["target"] / s["entry"] - 1) * 100, 2) if s.get("target") else None
        s["riskPct"] = round((s["entry"] - s["stop0"]) / s["entry"] * 100, 2)
        i0 = s.pop("i0")                               # the same entry with the other choice for the daily flip exit
        alt = {k: v for k, v in s.items() if k not in ("stops", "exit", "exitAt", "mfe", "mae", "_dd", "_dn")}
        if alt.get("maxDays"):
            alt["_dd"], alt["_dn"] = alt["at"][:10], 0
        alt.update(status="open", stop=s["stop0"] if on("stopOn") else 0.0, last=s["entry"], lastAt=s["at"])
        for j in range(i0 + 1, len(bars)):
            _step(alt, bars[j], walls, k_trail, not flip_exit, is_last=(j + 1 == len(bars) or bars[j + 1]["t"][:10] != bars[j]["t"][:10]), vw=vwap[j], em=ema[j])
            if alt["status"] != "open":
                break
        ap, ar = _result(alt)
        s["alt"] = {"flipExit": not flip_exit, "status": alt["status"], "pct": ap, "R": ar}
    if watch:
        watch.pop("_d", None)
    return sigs, watch


def _walls(key, sym, dates, today):
    """{date: walls} for the dates; past days from the store (flip added when missing), today refreshed hourly."""
    import flowdata
    out = {}
    for d in dates:
        try:
            w = flowdata.gex_day(key, sym, d, True)
        except Exception as e:
            print("wallwatch walls failed", sym, d, e)
            w = None
        if w:
            out[d] = {k: w.get(k) for k in ("callWall", "putWall", "flip")}
    return out


def check_ticker(sub, key, sym, days, now, since=None):
    """since: date of the oldest open signal of the ticker (followed until it ends)."""
    from charts import _yahoo
    start = (now - timedelta(days=int(days * 1.5) + 7 + 25)).replace(hour=0, minute=0, second=0, microsecond=0)  # + VWAP warm-up
    if since:
        start = min(start, datetime.strptime(since, "%Y-%m-%d") - timedelta(days=3))
    bars = _yahoo(sym.replace(".", "-"), "1h", start, now + timedelta(days=1)) or []
    bars = finished(bars, now)
    dates = sorted({b["t"][:10] for b in bars})
    first = dates[-days] if len(dates) >= days else (dates[0] if dates else "9")
    if since:
        first = min(first, since)
    cfg = config(sub)
    all_dates = dates
    dates = [d for d in dates if d >= first]
    pre = [d for d in all_dates if d < first][-int(cfg.get("retestDays") or 5):] if cfg.get("retestSetup", True) else []
    walls = _walls(key, sym, pre + dates, now.strftime("%Y-%m-%d"))
    try:                                               # 30-day ATR from daily candles
        daily = _yahoo(sym.replace(".", "-"), "1d", start - timedelta(days=70), now + timedelta(days=1)) or []
        daily = [b for b in daily if b["t"][:10] < now.strftime("%Y-%m-%d")]      # today's candle isn't finished
        datr = daily_atr(daily)
    except Exception as e:
        print("wallwatch daily atr failed", sym, e)
        datr = None
    sigs = detect(bars, walls, first, cfg, datr)
    watch = None
    if cfg.get("retestSetup", True):
        try:
            rb = [b for b in bars if not pre or b["t"][:10] >= pre[0]] if pre else [b for b in bars if b["t"][:10] >= first]
            earn = None
            try:
                import autotrader
                earn = autotrader.parse_earn(autotrader.earn_for(autotrader.settings(sub), sym)[0])[0]
            except Exception as e:
                print("earnings lookup failed", sym, e)
            vw_all, _ = anchored_vwap(bars, int(cfg.get("vwapDays") or VWAP_DAYS))
            vmap = {b["t"][:16]: v for b, v in zip(bars, vw_all)}
            emap = {b["t"][:16]: v for b, v in zip(bars, _ema_series([b["c"] for b in bars], int(cfg.get("emaLen") or 20)))}
            kmap = {b["t"][:16]: v for b, v in zip(bars, keltner(bars, int(cfg.get("kcLen") or 21), float(cfg.get("kcMult") or 3)))}
            spy = spy_trend(start, now)
            skp = {}
            if cfg.get("skewOn"):
                try:
                    import backtest
                    import flowdata
                    sk = backtest.fetch_skew(flowdata._key(sub), sym, 250, max_age_s=20 * 3600)
                    skp = backtest.skew_pct_map(sk, sorted({b["t"][:10] for b in rb}))
                except Exception as e:
                    print("skew failed", sym, e)
            rs, watch = detect_retest(rb, walls, first, cfg, datr, ticker_alerts(sub, sym, max_age_h=1), earn, vmap, emap,
                                      {"kc": kmap, "spy": spy, "skewPct": skp})
            sigs += rs
        except Exception as e:
            print("retest failed", sym, e)
    bars = [b for b in bars if b["t"][:10] >= first]
    pk = db.upk(sub)
    keep = {}
    for old in db.q_prefix(pk, f"WWSIG#{sym}#"):       # recomputed from `first` on: replace what was there
        if (old.get("at") or "") >= first:
            keep[old["SK"]] = {k: old[k] for k in ("botId", "tradeError", "flow") if old.get(k)}
            db.delete(pk, old["SK"])
    enter = []
    last_end = _bar_end(bars[-1]["t"]) if bars else None
    for s in sigs:
        s.update(keep.get(sig_sk(sym, s), {}))
        db.put({"PK": pk, "SK": sig_sk(sym, s), "symbol": sym, **s})
        fresh = last_end and s["at"] == bars[-1]["t"][:16] and (now - last_end) <= timedelta(minutes=45)
        if fresh and s["status"] == "open" and not s.get("botId") and not s.get("tradeError"):
            enter.append(s)
    last = bars[-1] if bars else None
    w = walls.get(last["t"][:10]) if last else None
    return {"symbol": sym, "price": last["c"] if last else None, "barAt": last["t"][:16] if last else None, "walls": w,
            "setup": setup_kind(w), "signals": len(sigs), "daysWithWalls": len(walls), "days": len(dates), "_enter": enter,
            "watch": watch}


def run(sub, days=3, budget_s=500):
    """Check every ticker of the watchlist. days=3 for the hourly run; a backfill uses more days."""
    import flowdata
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    cfg = config(sub)
    key = flowdata._key(sub)
    now = now_ny().replace(tzinfo=None)
    rows, errors = [], []
    prev = db.get(db.upk(sub), "WWRUN") or {}
    done_bf = set(prev.get("bfDone") or []) if days > 3 else set()
    todo = [t for t in cfg["tickers"] if t not in done_bf]
    try:                                                # next earnings dates (Unusual Whales, at most every 12 h)
        import autotrader
        autotrader.refresh_earnings(sub, todo)
    except Exception as e:
        print("earnings refresh failed", e)
    since, ver = {}, version(cfg)
    for s in db.q_prefix(db.upk(sub), "WWSIG#"):
        if s.get("symbol") and (s.get("status") == "open" or s.get("v") != ver):   # open, or made with other settings
            since[s["symbol"]] = min(since.get(s["symbol"], "9"), s["at"][:10])
    with ThreadPoolExecutor(max_workers=4) as ex:
        for k in range(0, len(todo), 8):
            if time.time() - t0 > budget_s:
                break
            futs = {t: ex.submit(check_ticker, sub, key, t, days, now, since.get(t)) for t in todo[k:k + 8]}
            for t, f in futs.items():
                try:
                    rows.append(f.result())
                    if days > 3:
                        done_bf.add(t)
                except Exception as e:
                    errors.append(f"{t}: {str(e)[:100]}")
    entries = []
    try:
        import autotrader
        mkt = autotrader.market_open()
    except Exception:
        mkt = False
    for r in rows:
        if not mkt:                                     # no entry after the close (the 15:30 candle ends at 16:00)
            r.pop("_enter", None)
            continue
        for sg in r.pop("_enter", []) or []:
            if cfg.get("trade") and time.time() - t0 < budget_s + 60:
                entries.append(enter(sub, r["symbol"], sg))
    status = {r["symbol"]: r for r in (prev.get("status") or [])}
    for r in rows:
        status[r["symbol"]] = r
    out = {"PK": db.upk(sub), "SK": "WWRUN", "at": iso(now_ny()), "running": None, "checked": len(rows), "errors": errors[:10],
           "status": [status[t] for t in cfg["tickers"] if t in status]}
    if days > 3:
        left = [t for t in cfg["tickers"] if t not in done_bf]
        out.update(bfDone=sorted(done_bf) if left else [], bfLeft=len(left), bfAt=iso(now_ny()), bfDays=days)
    else:
        out.update({k: prev.get(k) for k in ("bfDone", "bfLeft", "bfAt", "bfDays")})
    db.put(out)
    return {"checked": len(rows), "errors": len(errors), "entries": entries}


# ---------------- live: add flow tickers, buy the signals ----------------

def flow_add(sub, cfg=None):
    """Every minute: tickers with new unusual call flow (the bot's flow filters) are added to the watchlist."""
    import autotrader
    import flowdata
    cfg = cfg or config(sub)
    if not cfg.get("enabled", True) or not cfg.get("autoAdd", True):
        return {"skipped": "off"}
    bc = autotrader.settings(sub)
    since = (datetime.utcnow() - timedelta(minutes=bc["flowWindowMin"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    res = flowdata.alerts(sub, bc["flowMinPremium"], "call", bc["flowMinDte"], bc["flowMaxDte"], bc["flowAskSide"],
                          bc["flowSweeps"], None, bc["flowMinVolOi"], 1, since_utc=since, exclude_etfs=bc["excludeEtfs"])
    prem = {}
    for a in res.get("alerts") or []:
        t = (a.get("ticker") or "").upper().replace("/", ".")
        if t:
            prem[t] = prem.get(t, 0) + (a.get("premium") or 0)
    new = [t for t in sorted(prem, key=lambda t: -prem[t]) if t not in cfg["tickers"]]
    if new:
        cur = db.get(db.upk(sub), "WWCFG") or {"PK": db.upk(sub), "SK": "WWCFG"}
        tickers = list(cur.get("tickers") or cfg["tickers"])
        added = dict(cur.get("added") or {})
        for t in new[:20]:
            if t not in tickers and len(tickers) < 300:
                tickers.append(t)
                added[t] = {"at": iso(now_ny()), "by": "flow", "premium": round(prem[t])}
        db.update(db.upk(sub), "WWCFG", {"tickers": tickers, "added": added})
    return {"alerts": len(res.get("alerts") or []), "added": new[:20]}


def enter(sub, sym, sg):
    """Buy a call for a fresh signal (Alpaca paper): the bot picks the contract, the signal sets the stop and the target,
    then the limit is chased up until filled. The trade is then managed by the wall watch exit only."""
    import autotrader
    pk = db.upk(sub)
    sk = sig_sk(sym, sg)
    cfg = config(sub)
    rule = cfg.get("flowRule")
    if rule is not None and 0 <= int(rule) < len(FLOW_BUCKETS):
        try:
            f = flow_features(sg, ticker_alerts(sub, sym, max_age_h=0))
            db.update(pk, sk, {"flow": f})
        except Exception as e:
            f = None
            print("flow check failed", sym, e)
        name, fn = FLOW_BUCKETS[int(rule)]
        if not f or not fn(f):
            db.update(pk, sk, {"tradeError": f"flow filter not met: {name}"})
            return {"symbol": sym, "skipped": name}
    try:
        rec = autotrader.evaluate(sub, sym, source={"type": "wallwatch", "kind": sg.get("kind"), "signalAt": sg["at"],
                                                    "level": sg.get("level"), "entry": sg["entry"]})
        p = rec.get("proposal") or rec.get("altProposal")
        if not p:
            raise ValueError("no call contract in the expiration / delta window")
        ww = {"kind": sg.get("kind"), "signalAt": sg["at"], "atr": sg["atr"], "stop0": sg["stop0"], "trailAtr": float(cfg["trailAtr"]) if cfg.get("trailOn", True) else 0.0,
              "vwapExit": bool(cfg.get("vwapExit")), "emaExit": bool(cfg.get("emaExit")), "emaLen": int(cfg.get("emaLen") or 20),
              "vwapDays": int(cfg.get("vwapDays") or VWAP_DAYS),
              "stopOn": bool(cfg.get("stopOn", True)),
              "target": sg.get("target"), "level": sg.get("level"), "entry": sg["entry"], "stopAtr": float(cfg["stopAtr"]),
              "flipExit": bool(cfg.get("flipExit", True)), "aboveFlipAtEntry": bool(sg.get("aboveFlipAtEntry"))}
        p = {**p, "qty": max(1, int(p.get("qty") or 1)), "underlyingStop": sg["stop"], "underlyingTarget": sg.get("target"),
             "strategyOnly": "wallwatch", "wallwatch": ww}
        db.update(pk, f"BOT#{rec['id']}", {"proposal": p, "decision": "BUY", "strategyOnly": "wallwatch", "wallwatch": ww})
        db.update(pk, sk, {"botId": rec["id"]})
        autotrader.place(sub, rec["id"], placed_by="auto-wallwatch", ai_inline=False, override=True)
        out = autotrader.chase(sub, rec["id"]) or {}
        return {"symbol": sym, "botId": rec["id"], "status": out.get("status"), "contract": p.get("contract")}
    except Exception as e:
        db.update(pk, sk, {"tradeError": str(e)[:160]})
        return {"symbol": sym, "error": str(e)[:160]}


def trades_for(sub, sym):
    """The real (paper) trades of a ticker placed from wall watch signals, for the chart."""
    out = []
    for r in db.q_prefix(db.upk(sub), "BOT#"):
        if r.get("symbol") != sym or not (r.get("wallwatch") or (r.get("placedBy") or "") == "auto-wallwatch"):
            continue
        p = r.get("proposal") or {}
        out.append({"id": r.get("id"), "signalAt": (r.get("wallwatch") or {}).get("signalAt"), "contract": p.get("contract"),
                    "exp": p.get("exp"), "strike": p.get("strike"), "qty": r.get("filledQty") or r.get("qty"),
                    "at": (r.get("filledAt") or r.get("submittedAt") or r.get("createdAt") or "")[:16],
                    "fill": r.get("fillPrice"), "limit": r.get("limit"), "status": r.get("status"),
                    "under": (r.get("wallwatch") or {}).get("entry"), "stop": p.get("underlyingStop"), "target": p.get("underlyingTarget"),
                    "exitAt": (r.get("closedAt") or "")[:16] or None, "exit": r.get("exitPrice"), "exitReason": r.get("exitReason"),
                    "pl": r.get("realizedPl") if r.get("status") == "closed" else r.get("lastPl"),
                    "plPct": r.get("realizedPct") if r.get("status") == "closed" else r.get("lastPlPct"),
                    "underExit": r.get("lastUnderlying")})
    return sorted(out, key=lambda x: x["at"])


def _weekdays(n, today):
    out, d = [], today
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return sorted(out)


def walls_todo(tickers, days=45, need_mp=True):
    """(ticker, date) pairs whose call wall / put wall / flip aren't stored yet. Days with no data (holidays) count as done."""
    today = now_ny().date()
    dates = _weekdays(days, today)
    ts = today.strftime("%Y-%m-%d")
    todo = []
    for sym in tickers:
        have = {}
        for r in db.q_prefix("UWGEX2", f"{sym}#"):
            have[r["SK"].split("#", 1)[1]] = r
        mp = {r["SK"].split("#", 1)[1] for r in db.q_prefix("UWMP", f"{sym}#")}
        for d in dates:
            r = have.get(d)
            data = (r or {}).get("data")
            if d == ts:
                continue                                # today: refreshed by the hourly check
            if r and (d in mp or not need_mp) and (data is None or (data.get("callWall") is not None and data.get("flip") is not None and not data.get("noflip"))):
                continue
            todo.append((sym, d))
    return todo, len(dates) * len(tickers)


def _walls_and_mp(key, sym, d, mp=True):
    import flowdata
    w = flowdata.gex_day(key, sym, d, True)
    if not mp:
        return w
    try:
        maxpain_day(key, sym, d)
    except Exception as e:
        print("max pain failed", sym, d, e)
    return w


def fill_walls(sub, days=45, budget_s=480, ticker=None, mp=True, sk=None):
    """Fetch the walls and the flip of every day for every watchlist ticker (or one ticker), as many as the time allows."""
    import flowdata
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    pk = db.upk(sub)
    cfg = config(sub)
    key = flowdata._key(sub)
    sk = sk or (f"WWFILL#{ticker}" if ticker else "WWFILL")
    todo, total = walls_todo([ticker] if ticker else cfg["tickers"], days, need_mp=mp)
    db.put({"PK": pk, "SK": sk, "running": iso(now_ny()), "total": total, "left": len(todo), "days": days,
            "at": (db.get(pk, sk) or {}).get("at")})
    done, errors = 0, []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for k in range(0, len(todo), 12):
            if time.time() - t0 > budget_s:
                break
            futs = [(t, ex.submit(_walls_and_mp, key, t[0], t[1], mp)) for t in todo[k:k + 12]]
            for t, f in futs:
                try:
                    f.result()
                    done += 1
                except Exception as e:
                    errors.append(f"{t[0]} {t[1]}: {str(e)[:80]}")
    left = max(0, len(todo) - done - len(errors))
    out = {"PK": pk, "SK": sk, "running": None, "total": total, "left": left, "fetched": done, "days": days,
           "errors": errors[:5], "at": iso(now_ny())}
    if ticker and not left and mp:                       # the ticker's signals over those days
        try:
            check_ticker(sub, key, ticker, days, now_ny().replace(tzinfo=None))
        except Exception as e:
            out["errors"] = (out["errors"] + [f"signals: {str(e)[:80]}"])[:5]
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


def summarize(sigs):
    done = [s for s in sigs if s.get("status") in ("target", "stop", "trail", "flip")]
    hit = [s for s in done if s["status"] == "target"]
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    summary = {"signals": len(sigs), "open": len(sigs) - len(done), "closed": len(done), "targets": len(hit),
               "hitPct": round(len(hit) / len(done) * 100) if done else None,
               "avgPct": avg([s["pct"] for s in done]), "avgR": avg([s["R"] for s in done if s.get("R") is not None]),
               "avgWin": avg([s["pct"] for s in hit]), "avgLoss": avg([s["pct"] for s in done if s["status"] == "stop"]), "trails": sum(1 for s in done if s["status"] == "trail"),
               "avgTrail": avg([s["pct"] for s in done if s["status"] == "trail"]),
               "totalPct": round(sum(s["pct"] for s in done), 2) if done else None,
               "avgTgt": avg([s["tgtPct"] for s in sigs if s.get("tgtPct") is not None]), "avgRisk": avg([s["riskPct"] for s in sigs if s.get("riskPct") is not None])}
    return summary


def view(sub):
    if not db.get(db.upk(sub), "WWCFG"):               # first visit: the default list, on (the hourly job finds it)
        save_config(sub, {})
    cfg = config(sub)
    run_ = db.get(db.upk(sub), "WWRUN") or {}
    sigs = sorted(db.q_prefix(db.upk(sub), "WWSIG#"), key=lambda s: s.get("at") or "", reverse=True)
    sigs = [{k: v for k, v in s.items() if k not in ("PK", "SK")} for s in sigs if s.get("symbol") in cfg["tickers"]
            and (not FLOW_ONLY or s.get("kind") == "retest")]
    summary = summarize(sigs)
    by_kind = {k: summarize([x for x in sigs if (x.get("kind") or "pw") == k]) for k in ("pw", "flip", "merged", "retest")}
    done = [s for s in sigs if s.get("status") in ("target", "stop", "trail", "flip")]
    by = {}
    for s in done:
        b = by.setdefault(s["symbol"], {"symbol": s["symbol"], "n": 0, "targets": 0, "total": 0.0})
        b["n"] += 1
        b["targets"] += s["status"] == "target"
        b["total"] = round(b["total"] + s["pct"], 2)
    fill = db.get(db.upk(sub), "WWFILL") or {}
    positions = []
    for r in db.q_prefix(db.upk(sub), "BOT#"):           # every open (or working) paper position of the bot
        if r.get("status") not in ("submitting", "submitted", "open", "closing"):
            continue
        p = r.get("proposal") or {}
        positions.append({"id": r.get("id"), "symbol": r.get("symbol"), "status": r.get("status"), "contract": p.get("contract"),
                          "exp": p.get("exp"), "strike": p.get("strike"), "qty": r.get("filledQty") or r.get("qty"),
                          "fill": r.get("fillPrice"), "limit": r.get("limit"), "mark": r.get("lastMark"), "pl": r.get("lastPl"),
                          "plPct": r.get("lastPlPct"), "under": r.get("lastUnderlying"), "stop": p.get("underlyingStop"),
                          "target": p.get("underlyingTarget"), "at": (r.get("filledAt") or r.get("submittedAt") or "")[:16],
                          "wallwatch": bool(r.get("wallwatch") or p.get("wallwatch")),
                          "kind": (p.get("wallwatch") or {}).get("kind")})
    positions.sort(key=lambda x: x["at"], reverse=True)
    return {"positions": positions, "config": cfg, "run": {k: run_.get(k) for k in ("at", "running", "checked", "errors", "bfLeft", "bfAt", "bfDays")},
            "fill": {k: fill.get(k) for k in ("at", "running", "total", "left", "fetched", "errors")},
            "status": run_.get("status") or [], "signals": sigs[:500], "summary": summary, "byKind": by_kind,
            "byTicker": sorted(by.values(), key=lambda b: -b["total"]),
            "retestStudy": retest_table(sigs),
            "flowStudy": {**{k: (db.get(db.upk(sub), "WWFSTUDY") or {}).get(k) for k in ("at", "running", "tickers", "of", "errors")},
                          **flow_table(sigs)}}


# ---------------- monthly expirations and max pain ----------------

def monthly_exps(first, last):
    """Monthly option expirations (3rd Friday, Thursday when the Friday is a market holiday is not modeled)."""
    out = []
    y, m = int(first[:4]), int(first[5:7])
    while True:
        d = datetime(y, m, 1)
        d += timedelta(days=(4 - d.weekday()) % 7 + 14)
        ds = d.strftime("%Y-%m-%d")
        if ds > last:
            break
        if ds >= first:
            out.append(ds)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _is_monthly(exp):
    try:
        d = datetime.strptime(exp[:10], "%Y-%m-%d")
    except ValueError:
        return False
    return 15 <= d.day <= 21 and d.weekday() in (3, 4)


def parse_max_pain(raw):
    """{expiry: max pain strike} from Unusual Whales' max-pain answer (list of rows or a dict by expiry)."""
    import flowdata
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    out = {}
    if isinstance(data, dict) and not any(k in data for k in ("expiry", "expiration")):
        for k, v in data.items():
            n = flowdata._num(v if not isinstance(v, dict) else (v.get("max_pain") or v.get("price")))
            if n and len(str(k)) >= 10:
                out[str(k)[:10]] = n
        return out
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    for r in rows:
        if not isinstance(r, dict):
            continue
        exp = str(r.get("expiry") or r.get("expiration") or r.get("expires") or r.get("date") or "")[:10]
        v = flowdata._pick(r, "max_pain", "maxPain", "max_pain_strike", "strike", "price")
        if exp and v:
            out[exp] = v
    return out


def maxpain_day(key, ticker, date):
    """{monthly expiry: max pain} as of `date` (cached; past days kept for good, today refreshed hourly)."""
    import flowdata
    ck = f"{ticker}#{date}"
    hit = db.get("UWMP", ck)
    today = now_ny().strftime("%Y-%m-%d")
    if hit and (date < today or time.time() - hit.get("at", 0) < 3600):
        return hit.get("mp") or {}
    raw = flowdata._uw(key, f"/api/stock/{ticker}/max-pain", {"date": date})
    mp = {k: v for k, v in parse_max_pain(raw or {}).items() if _is_monthly(k) and k >= date}
    from decimal import Decimal
    try:
        db.put({"PK": "UWMP", "SK": ck, "at": int(time.time()), "mp": {k: Decimal(str(round(v, 2))) for k, v in mp.items()}})
    except Exception as e:
        print("max pain cache failed", e)
    return mp


def stored_maxpain(ticker):
    out = {}
    for r in db.q_prefix("UWMP", f"{ticker}#"):
        out[r["SK"].split("#", 1)[1]] = {k: float(v) for k, v in (r.get("mp") or {}).items()}
    return out


def maxpain_series(mp_by_day, dates):
    """For each date: the next monthly expiration and its max pain as of that date."""
    out = []
    for d in dates:
        mp = mp_by_day.get(d) or {}
        nxt = min((e for e in mp if e >= d), default=None)
        if nxt:
            out.append({"t": f"{d}T09:30", "exp": nxt, "maxPain": mp[nxt]})
    return out


def flow_alerts(sub, sym, first, last):
    """Calls and puts bought at the ask on the ticker (Unusual Whales alerts), biggest 800 kept."""
    import autotrader
    import flowdata
    cfg = autotrader.settings(sub)
    out = []
    nd = (now_ny().date() - datetime.strptime(first, "%Y-%m-%d").date()).days + 1
    res = flowdata.alerts(sub, cfg["repeatMinPremium"], "all", 0, 400, True, False, sym, 0, min(nd, 70), exclude_etfs=False)
    for a in res.get("alerts") or []:
        at = (a.get("atEt") or "").replace(" ", "T")[:16]
        if (a.get("ticker") or "").upper() != sym or not at or at[:10] < first or at[:10] > last:
            continue
        out.append({"t": at, "premium": round(a.get("premium") or 0), "type": "put" if a.get("type") == "put" else "call",
                    "contract": a.get("contract"), "big": (a.get("premium") or 0) >= cfg["flowMinPremium"],
                    "sweep": bool(a.get("sweep")), "rule": a.get("rule")})
    out.sort(key=lambda a: a["t"])
    if len(out) > 800:
        keep = set(id(a) for a in sorted(out, key=lambda a: -a["premium"])[:800])
        out = [a for a in out if id(a) in keep]
    return out


def chart(sub, sym, days=45, flow=True):
    """Hourly chart of a watchlist ticker: candles, walls + flip by day, its signals, and the call / put flow."""
    import flowdata
    from charts import _yahoo
    sym = (sym or "").upper().replace("/", ".")
    if not sym:
        from util import BadRequest
        raise BadRequest("Pick a ticker.")
    now = now_ny().replace(tzinfo=None)
    start = (now - timedelta(days=int(days * 1.5) + 25)).replace(hour=0, minute=0, second=0, microsecond=0)   # + VWAP warm-up
    bars = _yahoo(sym.replace(".", "-"), "1h", start, now + timedelta(days=1)) or []
    bars = [{"t": b["t"][:16], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v")}
            for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    bars_all = bars
    dates = sorted({b["t"][:10] for b in bars})[-days:]
    bars = [b for b in bars if dates and b["t"][:10] >= dates[0]]
    stored = flowdata.stored_walls(sym)
    today = now.strftime("%Y-%m-%d")
    note = None
    if dates and dates[-1] == today:
        try:
            w = flowdata.gex_day(flowdata._key(sub), sym, today, True)
            if w:
                stored[today] = {k: w.get(k) for k in ("callWall", "putWall", "flip")}
        except Exception as e:
            note = f"Today's walls unavailable: {str(e)[:100]}"
    walls, prev = [], None
    for d in dates:
        w = stored.get(d)
        if not w or w.get("callWall") is None:
            walls.append({"t": f"{d}T09:30", "callWall": None, "putWall": None, "flip": None, "src": "gap"})
            prev = None
            continue
        cur = {"t": f"{d}T09:30", "callWall": w.get("callWall"), "putWall": w.get("putWall"), "flip": w.get("flip"), "src": "uw"}
        if prev and all(prev.get(k) == cur.get(k) for k in ("callWall", "putWall", "flip")):
            continue
        walls.append(cur)
        prev = cur
    missing = [d for d in dates if not (stored.get(d) or {}).get("callWall")]
    noflip = [d for d in dates if (stored.get(d) or {}).get("callWall") and not stored[d].get("flip")]
    if missing or noflip:
        note = (note + " · " if note else "") + (f"Walls missing for {len(missing)} of {len(dates)} days" if missing else "") \
            + (f"{' · ' if missing else ''}flip missing for {len(noflip)} days" if noflip else "") + ": press Get walls (45 days)."
    sigs = [{k: v for k, v in s.items() if k not in ("PK", "SK")} for s in db.q_prefix(db.upk(sub), f"WWSIG#{sym}#")
            if not FLOW_ONLY or s.get("kind") == "retest"]
    sigs.sort(key=lambda s: s["at"])
    last = next((stored.get(d) for d in reversed(dates) if (stored.get(d) or {}).get("callWall")), None)
    mpd = stored_maxpain(sym)
    if dates and dates[-1] == today:
        try:
            mpd[today] = maxpain_day(flowdata._key(sub), sym, today)
        except Exception as e:
            print("max pain today failed", sym, e)
    maxpain = maxpain_series(mpd, dates)
    exps = monthly_exps(dates[0], (datetime.strptime(dates[-1], "%Y-%m-%d") + timedelta(days=40)).strftime("%Y-%m-%d")) if dates else []
    alerts, flow_note = [], None
    if flow and dates:
        try:
            alerts = flow_alerts(sub, sym, dates[0], dates[-1])
        except Exception as e:
            flow_note = f"Options flow unavailable: {str(e)[:120]}"
    ccfg = config(sub)
    vw, vres = anchored_vwap(bars_all, int(ccfg.get("vwapDays") or VWAP_DAYS))
    off = len(bars_all) - len(bars)
    em = _ema_series([b["c"] for b in bars_all], int(ccfg.get("emaLen") or 20))[off:]
    kcs = keltner(bars_all, int(ccfg.get("kcLen") or 21), float(ccfg.get("kcMult") or 3))[off:]
    kc = [{"t": b["t"], "m": k[0], "u": k[1], "l": k[2]} for b, k in zip(bars, kcs) if k]
    orbs = [{"d": b["t"][:10], "h": b["h"], "l": b["l"]} for b in bars if b["t"][11:16] == "09:30"]
    try:
        sp = spy_trend(start, now) if bars else {}
    except Exception:
        sp = {}
    spy_dn = [b["t"] for b in bars if sp.get(b["t"][:16]) == -1]
    ema = [{"t": b["t"], "v": round(v, 4)} for b, v in zip(bars, em) if v is not None]
    cur_v = version({**PARAMS, **ccfg})
    stale = [x for x in sigs if x.get("v") and x["v"] != cur_v and x.get("status") != "open" and not x.get("botId")]
    sigs = [x for x in sigs if x not in stale]
    vw, vres = vw[off:], [i - off for i in vres if i - off >= 0]
    vwap = [{"t": b["t"], "v": v} for b, v in zip(bars, vw)]
    fill = db.get(db.upk(sub), f"WWFILL#{sym}") or {}
    return {"kc": kc, "orb": orbs, "spyDown": spy_dn, "vwap": vwap, "ema": ema, "emaLen": int(ccfg.get("emaLen") or 20), "staleSignals": len(stale), "vwapResets": [bars[i]["t"] for i in vres if i > 0], "fill": {k: fill.get(k) for k in ("running", "total", "left", "at", "errors")}, "missingDays": len(missing), "noflipDays": len(noflip),
            "symbol": sym, "bars": bars, "walls": walls, "wallsNow": last, "signals": sigs, "alerts": alerts, "note": flow_note, "evals": [],
            "interval": 60, "days": days, "wallNote": note, "setupNow": setup_kind(last),
            "maxPain": maxpain, "expirations": exps, "maxPainDays": len(maxpain), "trades": trades_for(sub, sym),
            "added": (config(sub).get("added") or {}).get(sym),
            "maxPainMissing": sum(1 for d in dates if d != today and d not in mpd)}


def dp_floor(daily, pct=0.25):
    """Dark pool size floor from the 30-day average daily volume: (adv, price, min shares, $ at min shares, $ floor)."""
    vols = [b["v"] for b in daily[-30:] if b.get("v")]
    adv = sum(vols) / len(vols) if vols else None
    price = daily[-1]["c"] if daily else None
    min_sh = adv * pct / 100 if adv else None
    dollars = (min_sh * price) if min_sh and price else 2_000_000
    floor = max([x for x in (250_000, 500_000, 1e6, 2e6, 5e6, 10e6, 25e6, 50e6, 100e6) if x <= dollars * 0.8] or [250_000])
    return adv, price, min_sh, dollars, floor


def darkpool(sub, sym, min_pct=None, days=45):
    """Dark pool prints of the last `days` trading days whose size is at least `min_pct` % of the 30-day average daily
    volume (default 0.25%), with the side (bought at the ask / sold at the bid) and the top levels."""
    import flowdata
    from charts import _yahoo
    sym = (sym or "").upper().replace("/", ".")
    pct = float(min_pct) if min_pct not in (None, "") else 0.25
    now = now_ny().replace(tzinfo=None)
    daily = _yahoo(sym.replace(".", "-"), "1d", now - timedelta(days=60), now + timedelta(days=1)) or []
    daily = [b for b in daily if b["t"][:10] < now.strftime("%Y-%m-%d") and b.get("v")]
    adv, price, min_sh, dollars, floor = dp_floor(daily, pct)
    out = flowdata.darkpool(sub, sym, _weekdays(days, now_ny().date()), floor)     # fetched with a round $ floor (cached)
    prints = [p for p in out.get("prints") or [] if not min_sh or p["size"] >= min_sh]
    buy = sum(p["premium"] for p in prints if p.get("side") == "buy")
    sell = sum(p["premium"] for p in prints if p.get("side") == "sell")
    return {"symbol": sym, "prints": prints, "levels": flowdata.dp_levels(prints), "profile": flowdata.dp_levels(prints, 400),
            "minPremium": round(dollars),
            "minPct": pct, "adv": round(adv) if adv else None, "minShares": round(min_sh) if min_sh else None,
            "buy": buy, "sell": sell, "error": out.get("error")}


# ---------------- options flow vs signals ----------------

FLOW_DAYS = 3                # "recent" flow = the 3 trading days before the signal candle closed


def ticker_alerts(sub, sym, max_age_h=6):
    """Calls and puts bought at the ask on the ticker over ~70 days (Unusual Whales), cached a few hours."""
    import autotrader
    import flowdata
    pk = db.upk(sub)
    hit = db.get(pk, f"WWFLOW#{sym}")
    if hit and time.time() - hit.get("ts", 0) < max_age_h * 3600:
        return hit.get("alerts") or []
    bc = autotrader.settings(sub)
    fresh_days = 2 if hit and time.time() - hit.get("full", 0) < 3 * 86400 else 70    # top up the last 2 days, full refetch every 3 days
    res = flowdata.alerts(sub, bc["repeatMinPremium"], "all", 0, 400, True, False, sym, 0, fresh_days, exclude_etfs=False)
    out = list(hit.get("alerts") or []) if fresh_days == 2 else []
    for a in res.get("alerts") or []:
        if (a.get("ticker") or "").upper() != sym or not a.get("atEt"):
            continue
        out.append({"t": a["atEt"].replace(" ", "T")[:16], "c": 0 if a.get("type") == "put" else 1,
                    "p": round(a.get("premium") or 0), "u": a.get("underlying")})
    seen, uniq = set(), []
    for a in sorted(out, key=lambda x: x["t"]):
        k = (a["t"], a["c"], a["p"])
        if k not in seen:
            seen.add(k)
            uniq.append(a)
    out = uniq
    try:
        db.put({"PK": pk, "SK": f"WWFLOW#{sym}", "ts": int(time.time()), "alerts": out[-3000:],
                "full": int(time.time()) if fresh_days == 70 else hit.get("full", 0)})
    except Exception as e:
        print("flow cache failed", sym, e)
    return out


def flow_features(sig, alerts, big=500_000):
    """What the options flow looked like before a signal (only alerts printed before the signal candle closed)."""
    end = _bar_end(sig["at"]).strftime("%Y-%m-%dT%H:%M")
    d0 = datetime.strptime(sig["at"][:10], "%Y-%m-%d")
    days, d = [], d0
    while len(days) < FLOW_DAYS:
        if d.weekday() < 5:
            days.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    start3, start1 = days[-1], (_bar_end(sig["at"]) - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M")
    prior = [a for a in alerts if a["t"] < end]
    w3 = [a for a in prior if a["t"][:10] >= start3]
    w1 = [a for a in prior if a["t"] >= start1]
    lvl = sig.get("level") or sig.get("pw")
    c3 = sum(a["p"] for a in w3 if a["c"]); p3 = sum(a["p"] for a in w3 if not a["c"])
    c1 = sum(a["p"] for a in w1 if a["c"]); p1 = sum(a["p"] for a in w1 if not a["c"])
    near = sum(a["p"] for a in w3 if a["c"] and a.get("u") and lvl and abs(a["u"] - lvl) / lvl <= 0.01)
    last = max((a["t"] for a in prior if a["c"] and a["p"] >= 100_000), default=None)
    age = None
    if last:
        age = round((datetime.strptime(end, "%Y-%m-%dT%H:%M") - datetime.strptime(last, "%Y-%m-%dT%H:%M")).total_seconds() / 3600, 1)
    return {"c3": c3, "p3": p3, "c1": c1, "p1": p1, "ratio3": round(c3 / p3, 2) if p3 else (99.0 if c3 else None),
            "big3": sum(1 for a in w3 if a["c"] and a["p"] >= big), "nearLevel": near, "lastCallH": age}


FLOW_BUCKETS = [
    ("Any call flow in the 3 days before", lambda f: (f.get("c3") or 0) > 0),
    ("No call flow in the 3 days before", lambda f: (f.get("c3") or 0) == 0),
    ("Calls ≥ $1M in the 3 days before", lambda f: (f.get("c3") or 0) >= 1_000_000),
    ("Calls ≥ $5M in the 3 days before", lambda f: (f.get("c3") or 0) >= 5_000_000),
    ("Calls > puts (3 days)", lambda f: (f.get("c3") or 0) > (f.get("p3") or 0)),
    ("Calls ≥ 2× puts (3 days)", lambda f: (f.get("c3") or 0) > 0 and (f.get("c3") or 0) >= 2 * (f.get("p3") or 0)),
    ("Puts > calls (3 days)", lambda f: (f.get("p3") or 0) > (f.get("c3") or 0)),
    ("Calls in the last 7 hours", lambda f: (f.get("c1") or 0) > 0),
    ("Calls ≥ $1M in the last 7 hours", lambda f: (f.get("c1") or 0) >= 1_000_000),
    ("A call alert ≥ $500K (3 days)", lambda f: (f.get("big3") or 0) >= 1),
    ("3+ call alerts ≥ $500K (3 days)", lambda f: (f.get("big3") or 0) >= 3),
    ("Calls bought while price was within 1% of the level", lambda f: (f.get("nearLevel") or 0) > 0),
    ("Last big call alert < 24 h before", lambda f: f.get("lastCallH") is not None and f.get("lastCallH") < 24),
]


def flow_study(sub, budget_s=480):
    """Attach the flow features to every signal of the watchlist, then compare the results with and without each
    flow condition. Runs in the background (one Unusual Whales request per ticker, cached)."""
    t0 = time.time()
    pk = db.upk(sub)
    cfg = config(sub)
    sigs = [x for x in db.q_prefix(pk, "WWSIG#") if x.get("symbol") in cfg["tickers"]]
    by = {}
    for x in sigs:
        by.setdefault(x["symbol"], []).append(x)
    done, errors = 0, []
    db.put({**(db.get(pk, "WWFSTUDY") or {}), "PK": pk, "SK": "WWFSTUDY", "running": iso(now_ny())})
    for sym, xs in sorted(by.items()):
        if time.time() - t0 > budget_s:
            break
        try:
            al = ticker_alerts(sub, sym)
        except Exception as e:
            errors.append(f"{sym}: {str(e)[:80]}")
            continue
        for x in xs:
            f = flow_features(x, al)
            db.update(pk, x["SK"], {"flow": f})
            x["flow"] = f
        done += 1
    out = {"PK": pk, "SK": "WWFSTUDY", "running": None, "at": iso(now_ny()), "tickers": done, "of": len(by), "errors": errors[:5]}
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


def flow_table(sigs):
    """Results of the finished signals with and without each flow condition."""
    fin = [x for x in sigs if x.get("status") in ("target", "stop", "trail", "flip") and x.get("flow")]
    rows = []
    base = summarize(fin)
    for name, fn in FLOW_BUCKETS:
        yes = [x for x in fin if fn(x["flow"])]
        if not yes:
            rows.append({"name": name, "n": 0})
            continue
        sm = summarize(yes)
        rows.append({"name": name, "n": len(yes), "hitPct": sm["hitPct"], "avgPct": sm["avgPct"], "avgR": sm["avgR"],
                     "totalPct": sm["totalPct"], "winPct": round(sum(1 for x in yes if x["pct"] > 0) / len(yes) * 100)})
    return {"base": {"n": len(fin), "hitPct": base["hitPct"], "avgPct": base["avgPct"], "avgR": base["avgR"], "totalPct": base["totalPct"],
                     "winPct": round(sum(1 for x in fin if x["pct"] > 0) / len(fin) * 100) if fin else None},
            "rows": rows, "withFlow": sum(1 for x in sigs if x.get("flow")), "signals": len(sigs)}


def retest_table(sigs):
    """Flow retest signals split by the support used, the size of the call alert and the wait before the retest."""
    fin = [x for x in sigs if x.get("kind") == "retest" and x.get("status") in ("target", "stop", "trail", "flip")]

    def wait_h(x):
        try:
            return (datetime.strptime(x["at"][:16], "%Y-%m-%dT%H:%M") - datetime.strptime(x["armedAt"][:16], "%Y-%m-%dT%H:%M")).total_seconds() / 3600
        except (KeyError, ValueError, TypeError):
            return None
    groups = [
        ("Support: put wall", lambda x: x.get("levelName") == "put wall"),
        ("Support: call wall", lambda x: x.get("levelName") == "call wall"),
        ("Support: flip", lambda x: x.get("levelName") == "flip"),
        ("Call alert $500K – $1M", lambda x: (x.get("armPremium") or 0) < 1_000_000),
        ("Call alert $1M – $5M", lambda x: 1_000_000 <= (x.get("armPremium") or 0) < 5_000_000),
        ("Call alert ≥ $5M", lambda x: (x.get("armPremium") or 0) >= 5_000_000),
        ("Retest within 1 day of the alert", lambda x: (wait_h(x) or 0) <= 24),
        ("Retest after 1 – 3 days", lambda x: 24 < (wait_h(x) or 0) <= 72),
        ("Retest after 3+ days", lambda x: (wait_h(x) or 0) > 72),
        ("Flip went down vs the day before", lambda x: x.get("flipChg") is not None and x["flipChg"] < 0),
        ("Flip flat or up vs the day before", lambda x: x.get("flipChg") is not None and x["flipChg"] >= 0),
        ("Entry above the VWAP (reset on high volume)", lambda x: x.get("vwap") is not None and x["entry"] > x["vwap"]),
        ("Entry below the VWAP", lambda x: x.get("vwap") is not None and x["entry"] <= x["vwap"]),
        ("Entry above the EMA", lambda x: x.get("ema") is not None and x["entry"] > x["ema"]),
        ("Entry below the EMA", lambda x: x.get("ema") is not None and x["entry"] <= x["ema"]),
        ("EMA above the VWAP", lambda x: x.get("ema") is not None and x.get("vwap") is not None and x["ema"] > x["vwap"]),
        ("EMA below the VWAP", lambda x: x.get("ema") is not None and x.get("vwap") is not None and x["ema"] <= x["vwap"]),
        ("Entry above the gamma flip", lambda x: x.get("flip") is not None and x["entry"] > x["flip"]),
        ("Entry below the gamma flip", lambda x: x.get("flip") is not None and x["entry"] <= x["flip"]),
        ("Call wall target above the entry", lambda x: bool(x.get("target"))),
        ("No target (trailing only)", lambda x: not x.get("target")),
    ]
    rows = []
    for name, fn in groups:
        yes = [x for x in fin if fn(x)]
        sm = summarize(yes) if yes else {}
        rows.append({"name": name, "n": len(yes), "hitPct": sm.get("hitPct"), "avgPct": sm.get("avgPct"), "avgR": sm.get("avgR"),
                     "totalPct": sm.get("totalPct"), "winPct": round(sum(1 for x in yes if x["pct"] > 0) / len(yes) * 100) if yes else None})
    base = summarize(fin) if fin else {}
    # the same entries with the other choice for the daily flip exit (computed with each signal)
    alts = [x["alt"] for x in fin if x.get("alt") and x["alt"].get("status") in ("target", "stop", "trail", "flip")]
    cmp_ = None
    if alts:
        avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
        cmp_ = {"flipExit": alts[0]["flipExit"], "n": len(alts), "avgPct": avg([a["pct"] for a in alts]),
                "avgR": avg([a["R"] for a in alts if a.get("R") is not None]), "totalPct": round(sum(a["pct"] for a in alts), 2),
                "winPct": round(sum(1 for a in alts if a["pct"] > 0) / len(alts) * 100),
                "flipExits": sum(1 for x in fin if x["status"] == "flip")}
    return {"base": {"n": len(fin), "hitPct": base.get("hitPct"), "avgPct": base.get("avgPct"), "avgR": base.get("avgR"),
                     "totalPct": base.get("totalPct"), "winPct": round(sum(1 for x in fin if x["pct"] > 0) / len(fin) * 100) if fin else None},
            "rows": rows, "exitCompare": cmp_}
