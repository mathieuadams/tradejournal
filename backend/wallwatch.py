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
          "kcDaily": True,     # over-extension (Under upper Keltner, Keltner ride) on the daily channel, else hourly
          "kchLower": False, "kchLen": 21, "kchMult": 3.0, "kcdLower": False,   # kcdLower: close in the lower half of the DAILY channel   # hourly Keltner channel   # entry fine tuning: the entry candle closes in the lower half of the HOURLY Keltner channel
          # building blocks (no dedicated strategies): alertReq = a call alert must arm the ticker first;
          # vwapReset = the entry candle is a VWAP reset candle (highest volume of the period); pwNeCw = no entry when PW = CW
          "alertReq": True, "vwapReset": False, "pwNeCw": False,
          "skewOn": False, "kcRide": False, "cwLost": False, "dpLost": False,
          # pvSetup: the VWAP reset on the put wall strategy replaces the flow retest (LONG signal of pw_vwap_events)
          # pvEod: with it, the entry day must close above the put wall, else sold at that close
          "pvSetup": False, "pvEod": False,
          # relFlow: a call alert arms the ticker when its premium is at least relMult x the median premium of the
          # ticker's call alerts of the previous 30 days (unusual for THIS ticker); retestMin is the fallback with < 5 alerts
          "relFlow": True, "relMult": 3.0,
          # fvgOn: the entry candle must trade into an open bullish fair value gap (3-candle gap of at least fvgMin x ATR)
          "fvgOn": False, "fvgMin": 0.2, "fvgNear": 0.25, "fvgDays": 10,   # fvgDays: a gap not filled after N sessions expires   # fvgNear: a low within this x ATR above the gap counts as a touch
          # fvgExit: sell at the close of the candle that completes a new bearish fair value gap after the entry
          "fvgExit": False,
          # daily inertia (TOS inertia regime): linear-regression value of the daily closes over n1 / n2 / n3 days;
          # green = all three rising, red = all three falling, yellow = mixed. inGreen / inNotRed: entry filters;
          # inRedExit: sell at the daily close when the regime is red
          "inN1": 70, "inN2": 40, "inN3": 20, "inGreen": False, "inNotRed": False, "inRedExit": False,
          # scaleOn (lab / backtest): sell scalePct % of the position when price reaches entry + scaleR x risk;
          # scaleBE: then move the stop of the rest to the entry price
          "scaleOn": False, "scaleR": 1.0, "scalePct": 50, "scaleBE": True,
          # inYExit: sell at the daily close when the daily inertia is no longer green (yellow or red)
          "inYExit": False, "fxEntry": "", "fxExit": "", "hvcAbove": False, "hvcBelow": False, "abovePW": False, "belowPW": False, "cvdUp": False, "cvdDn": False, "cvdN": 20, "sqhTurn": False, "sqdTurn": False, "sqTurnN": 1, "pwExit": False, "inFall": False, "inhFall": False, "sqhRise": False, "sqhFall": False, "sqdRise": False, "sqdFall": False, "cvdRise": False, "cvdFall": False, "inRiseX": False, "inhRiseX": False, "inhRedX": False, "sqhRiseX": False, "sqdRiseX": False, "cvdRiseX": False, "cvdFallX": False,
          # lab: tf = "1D" runs the whole strategy on daily candles (entry at the daily close); pwTest = the daily low
          # tests the put wall (within the zone) and the day closes above it
          "tf": "1h", "pwTest": False,
          # hourly inertia (lab): the same regression on the hourly closes, lengths in hourly candles
          "earnExit": True,    # live: sell a wall watch trade before earnings (15:30 the day before, or the AMC day)
          "inhN1": 70, "inhN2": 40, "inhN3": 20, "inhGreen": False, "inhNotRed": False, "inhYExit": False,
          # TTM squeeze (lab), hourly (sqh*) and daily (sqd*): Bollinger(len, bb x stdev) inside Keltner(len, kc x ATR)
          # = squeeze on; fired = it turns off; momentum = regression of close - midpoint of the range / average.
          # On = squeeze on at entry; Fire = fired within the last sqFireN candles with momentum > 0;
          # Mom = momentum above 0 and rising; Exit = momentum falls (hourly: at that close; daily: at the daily close)
          "sqhLen": 20, "sqhBB": 2.0, "sqhKC": 1.5, "sqdLen": 20, "sqdBB": 2.0, "sqdKC": 1.5, "sqFireN": 3,
          "sqhOn": False, "sqhFire": False, "sqhMom": False, "sqhExit": False,
          "sqdOn": False, "sqdFire": False, "sqdMom": False, "sqdExit": False}       # skewOn: entry only when the 25-delta skew is in the middle third of its last 6 months   # tgtBuf: % under the call wall
OPTS = {"autoAdd": True, "trade": True, "flowRule": None}   # flowRule: index in FLOW_BUCKETS a signal must pass to be traded        # add unusual-flow tickers to the list; buy the signals (Alpaca paper)


MAIN_V1 = DEFAULT + ["V"]          # the main list as set on 2026-10-16
FLOW_LIST = "Unusual flow"


def _uniq(xs):
    seen, out = set(), []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def config(sub):
    c = db.get(db.upk(sub), "WWCFG") or {}
    if c and c.get("listsV") != 1:                     # one time: main list reset, the rest moved to the flow list
        old = list(c.get("tickers") or DEFAULT)
        extra = [t for t in old if t not in MAIN_V1]
        c = {**c, "tickers": list(MAIN_V1), "lists": [{"name": FLOW_LIST, "tickers": extra}], "listsV": 1}
        db.put(c)
    if c and not c.get("listsFix"):                    # one time: flow tickers that went to Main by mistake -> Unusual flow
        ad = c.get("added") or {}
        moved = [t for t in (c.get("tickers") or []) if t not in MAIN_V1 and (ad.get(t) or {}).get("by") == "flow"]
        ls = [dict(x) for x in (c.get("lists") or [])]
        fl = next((x for x in ls if x.get("name") == FLOW_LIST), None)
        if not fl:
            fl = {"name": FLOW_LIST, "tickers": []}
            ls.insert(0, fl)
        fl["tickers"] = list(fl.get("tickers") or []) + [t for t in moved if t not in (fl.get("tickers") or [])]
        c = {**c, "tickers": [t for t in (c.get("tickers") or []) if t not in moved], "lists": ls, "listsFix": 1}
        db.put(c)
    main = list(c.get("tickers") or MAIN_V1)
    lists = [{"name": str(x.get("name") or "List"), "tickers": list(x.get("tickers") or [])} for x in (c.get("lists") or [])]
    if not any(x["name"] == FLOW_LIST for x in lists):
        lists.insert(0, {"name": FLOW_LIST, "tickers": []})
    out = {"tickers": _uniq(main + [t for x in lists for t in x["tickers"]]), "main": main,
           "lists": [{"name": "Main", "tickers": main}] + lists, "enabled": c.get("enabled", True)}
    out.update({k: c.get(k, v) for k, v in PARAMS.items()})
    out.update({k: c.get(k, v) for k, v in OPTS.items()})
    out["added"] = c.get("added") or {}                 # {ticker: {"at", "by", "premium"}} for tickers added from the flow
    out["tf"] = "1h"                                    # the live bot trades hourly candles; 1D is for the lab
    out["pvSetup"] = False                              # no dedicated strategies: the VWAP reset is a rule (vwapReset) now
    if FLOW_ONLY:                                       # only the flow retest setup is used for now
        out.update(pwSetup=False, flipSetup=False, mergedSetup=False, retestSetup=True)
    return out


FLOW_ONLY = True


def version(cfg):
    """Signals made with other settings are recomputed."""
    return (f"7|{float(cfg['stopAtr']):g}|{float(cfg['trailAtr']):g}|{int(bool(cfg['pwSetup']))}{int(bool(cfg['flipSetup']))}"
            f"{int(bool(cfg.get('mergedSetup', True)))}|{float(cfg.get('tgtBuf', 0.5)):g}"
            f"|{int(bool(cfg.get('retestSetup', True)))}|{float(cfg.get('retestMin', 500000)):g}|{int(cfg.get('retestDays', 5))}|r4|{int(bool(cfg.get('aboveFlip', False)))}|{float(cfg.get('retestZone', 0.25)):g}|{int(bool(cfg.get('flipExit', True)))}|{int(cfg.get('earnDays', 10))}|cw2|v{int(cfg.get('vwapDays', 10))}|room{float(cfg.get('cwRoom', 1.0)):g}"
            f"|sw{''.join(str(int(bool(cfg.get(k, PARAMS[k])))) for k in SWITCHES)}|e{int(cfg.get('emaLen', 20))}|kc{int(cfg.get('kcLen', 21))}x{float(cfg.get('kcMult', 3.0)):g}{'D' if cfg.get('kcDaily', True) else 'H'}h{int(cfg.get('kchLen', 21))}x{float(cfg.get('kchMult', 3.0)):g}|rel{float(cfg.get('relMult', 3.0)):g}|fvg{float(cfg.get('fvgMin', 0.2)):g}n{float(cfg.get('fvgNear', 0.25)):g}d{int(cfg.get('fvgDays', 10))}|sc{float(cfg.get('scaleR', 1.0)):g}x{float(cfg.get('scalePct', 50)):g}|in{int(cfg.get('inN1', 70))}.{int(cfg.get('inN2', 40))}.{int(cfg.get('inN3', 20))}"
            + (("|x" + "".join(str(int(bool(cfg.get(k)))) for k in EXTRA_SW)
                + "|" + ".".join(f"{float(cfg.get(k, PARAMS[k])):g}" for k in EXTRA_NUM)) if any(cfg.get(k) for k in EXTRA_SW) else "")
            + (("|fx" + __import__("hashlib").sha1(f"{cfg.get('fxEntry') or ''}|{cfg.get('fxExit') or ''}".encode()).hexdigest()[:10])
               if (cfg.get("fxEntry") or cfg.get("fxExit")) else ""))


# rules added later: in the version only when one is on (signals made before them stay valid)
EXTRA_SW = ("inFall", "inhFall", "sqhRise", "sqhFall", "sqdRise", "sqdFall", "cvdRise", "cvdFall", "inRiseX", "inhRiseX", "inhRedX", "sqhRiseX", "sqdRiseX", "cvdRiseX", "cvdFallX", "pwExit", "abovePW", "belowPW", "cvdUp", "cvdDn", "sqhTurn", "sqdTurn", "inYExit", "inhGreen", "inhNotRed", "inhYExit", "sqhOn", "sqhFire", "sqhMom", "sqhExit", "sqdOn", "sqdFire", "sqdMom", "sqdExit")
EXTRA_NUM = ("cvdN", "sqTurnN", "inhN1", "inhN2", "inhN3", "sqhLen", "sqhBB", "sqhKC", "sqdLen", "sqdBB", "sqdKC", "sqFireN")


SWITCHES = ("usePW", "useFlip", "greenOnly", "noAboveCW", "cwRoomOn", "earnOn", "vwapOn", "emaOn", "stopOn", "trailOn", "targetOn", "vwapExit", "emaExit", "emaOverVwap", "spyUp", "kcNotExt", "orbAbove", "skewOn", "kcRide", "cwLost", "dpLost", "pvSetup", "pvEod", "relFlow", "fvgOn", "fvgExit", "scaleOn", "scaleBE", "kchLower", "alertReq", "vwapReset", "pwNeCw", "kcdLower", "inGreen", "inNotRed", "inRedExit", "hvcAbove", "hvcBelow")


def save_config(sub, body):
    cur = config(sub)
    main = list(cur["main"])
    extra = [dict(x) for x in cur["lists"][1:]]
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
        main = out[:150]
    if "enabled" in body:
        cur["enabled"] = bool(body["enabled"])
    before = version(cur)
    for k, lo_, hi_ in (("inN1", 5, 250), ("cvdN", 1, 500), ("sqTurnN", 1, 20), ("inhN1", 5, 500), ("sqhLen", 5, 200), ("sqdLen", 5, 200), ("sqFireN", 1, 20), ("inhN2", 5, 500), ("inhN3", 5, 500), ("inN2", 5, 250), ("inN3", 5, 250), ("fvgDays", 1, 250), ("retestMin", 50_000, 100_000_000), ("retestDays", 1, 20), ("earnDays", 0, 60), ("vwapDays", 1, 30), ("emaLen", 2, 200), ("kcLen", 2, 200), ("kchLen", 2, 200)):
        if k in body:
            try:
                cur[k] = int(max(lo_, min(hi_, float(body[k]))))
            except (TypeError, ValueError):
                pass
    for k in ("stopAtr", "trailAtr", "tgtBuf", "retestZone", "cwRoom", "kcMult", "kchMult", "relMult", "fvgMin", "fvgNear", "scaleR", "sqhBB", "sqhKC", "sqdBB", "sqdKC"):
        if k in body:
            try:
                cur[k] = max(0.0, min(10.0, round(float(body[k]), 2)))
            except (TypeError, ValueError):
                pass
    if "scalePct" in body:
        try:
            cur["scalePct"] = max(1, min(99, int(float(body["scalePct"]))))
        except (TypeError, ValueError):
            pass
    if "kcDaily" in body:
        cur["kcDaily"] = bool(body["kcDaily"])
    for k in ("pwSetup", "flipSetup", "mergedSetup", "retestSetup", "autoAdd", "trade", "aboveFlip", "abovePW", "belowPW", "cvdUp", "cvdDn", "sqhTurn", "sqdTurn", "pwExit", "inFall", "inhFall", "sqhRise", "sqhFall", "sqdRise", "sqdFall", "cvdRise", "cvdFall", "inRiseX", "inhRiseX", "inhRedX", "sqhRiseX", "sqdRiseX", "cvdRiseX", "cvdFallX", "flipExit", "earnExit", "inYExit", "pwTest", "inhGreen", "inhNotRed", "inhYExit", "sqhOn", "sqhFire", "sqhMom", "sqhExit", "sqdOn", "sqdFire", "sqdMom", "sqdExit") + SWITCHES:
        if k in body:
            cur[k] = bool(body[k])
    for k in ("fxEntry", "fxExit"):                   # formula rules (formula.py)
        if k in body:
            import formula
            v = str(body[k] or "").strip()
            if v:
                try:
                    formula.check(v, formula.UNAVAILABLE["swing"])
                except formula.FormulaError as e:
                    from util import BadRequest
                    raise BadRequest(f"{'Entry' if k == 'fxEntry' else 'Exit'} formula: {e}")
            cur[k] = v
    if "flowRule" in body:
        try:
            v = body["flowRule"]
            cur["flowRule"] = None if v in (None, "", -1, "-1") else int(v)
        except (TypeError, ValueError):
            pass
    ln = str(body.get("list") or (FLOW_LIST if body.get("addedBy") == "flow" else "Main"))[:40]   # the list an add / remove / move works on
    def lst(name):
        if name == "Main":
            return main
        x = next((x for x in extra if x["name"] == name), None)
        return x["tickers"] if x else None
    if body.get("newList"):                              # create a list
        nm = str(body["newList"]).strip()[:40]
        if nm and nm != "Main" and lst(nm) is None and len(extra) < 20:
            extra.append({"name": nm, "tickers": []})
    if body.get("deleteList") and body["deleteList"] not in ("Main", FLOW_LIST):
        extra = [x for x in extra if x["name"] != body["deleteList"]]
    if body.get("renameList") and body.get("to"):
        x = next((x for x in extra if x["name"] == body["renameList"] and x["name"] != FLOW_LIST), None)
        nm = str(body["to"]).strip()[:40]
        if x and nm and nm != "Main" and lst(nm) is None:
            x["name"] = nm
    removed = dict(cur.get("removed") or {})            # {ticker: when}: the flow doesn't add it back for 7 days
    if body.get("add"):                                  # add one ticker (to a list, Main by default)
        t = str(body["add"]).strip().upper().replace("/", ".")[:10]
        if body.get("addedBy") == "flow" and (removed.get(t) or "") >= iso(now_ny() - timedelta(days=7)):
            t = ""                                       # removed by you recently
        elif t in removed and body.get("addedBy") != "flow":
            removed.pop(t, None)
        L = lst(ln)
        if t and L is not None and t not in L:
            L.append(t)
            if t not in cur.get("added", {}):
                cur["added"] = {**cur.get("added", {}), t: {"at": iso(now_ny()), "by": body.get("addedBy") or "you",
                                                           **({"premium": round(body["premium"])} if body.get("premium") else {})}}
    if body.get("remove"):                               # remove from that list (or from every list)
        t = str(body["remove"]).strip().upper()
        for L in ([lst(ln)] if body.get("list") else [main] + [x["tickers"] for x in extra]):
            if L is not None and t in L:
                L.remove(t)
        if t not in main and not any(t in x["tickers"] for x in extra):
            removed[t] = iso(now_ny())
    if body.get("move") and body.get("to"):              # move a ticker to another list
        t = str(body["move"]).strip().upper()
        dst = lst(str(body["to"]))
        if dst is not None:
            for L in [main] + [x["tickers"] for x in extra]:
                if t in L:
                    L.remove(t)
            dst.append(t)
    cur["tickers"] = main
    from decimal import Decimal
    db.put({"PK": db.upk(sub), "SK": "WWCFG", **{k: Decimal(str(v)) if isinstance(v, float) else v for k, v in cur.items()
                                                 if k not in ("main",)}, "lists": extra, "listsV": 1, "listsFix": 1, "removed": removed})
    cur = config(sub)
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


def cvd_roll(bars, n=20):
    """Per candle: the estimated volume delta (buy - sell) summed over the last n candles, this one included. Each
    candle's volume is split by where it closes in its range (close at the high = all buying). Same on the chart."""
    n = max(1, int(n or 20))
    d, out, run = [], [], 0.0
    for i, b in enumerate(bars):
        v, r = float(b.get("v") or 0), b["h"] - b["l"]
        pc = bars[i - 1]["c"] if i else b["o"]
        buy = v * (b["c"] - b["l"]) / r if r > 0 else (v if b["c"] > pc else 0 if b["c"] < pc else v / 2)
        d.append(2 * buy - v)
        run += d[-1]
        if i >= n:
            run -= d[i - n]
        out.append(run)
    return out


RISE_FALL_EXITS = ("inRiseX", "inhRiseX", "inhRedX", "sqhRiseX", "sqdRiseX", "cvdRiseX", "cvdFallX")


def fx_series(bars, P, walls, intraday=True, vwap=None, dpl=None, alerts=None):
    """(entry, exit) [bool per candle] of the formula rules that are set, else None."""
    if not (P.get("fxEntry") or P.get("fxExit")):
        return None, None
    import formula
    cx = formula.Ctx(bars, intraday, walls, None, dpl or {}, None, None, vwap, alerts)
    ev = lambda e: formula.evaluate(e, cx, formula.UNAVAILABLE["swing"]) if e else None
    return ev(P.get("fxEntry")), ev(P.get("fxExit"))


def cvd_ok(P, v, ago=None, prev=None):
    """CVD > 0 / CVD < 0 filters (both on: never); CVD rising / falling (higher / lower than on the candle before)."""
    on = lambda k: bool(P.get(k, PARAMS[k]))
    if on("cvdRise") and not (v is not None and prev is not None and v > prev):
        return False
    if on("cvdFall") and not (v is not None and prev is not None and v < prev):
        return False
    return not ((on("cvdUp") and not (v is not None and v > 0)) or (on("cvdDn") and not (v is not None and v < 0)))


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


def _step(s, b, walls, k_trail, flip_exit, is_last, vw=None, em=None, kcu=None, dpl=None, bear=False, reg=None, hreg=None, sqh=None, sqd=None, cv=None, fx=False):
    """One hourly candle of an open retest trade: stop / target, trailing, and the daily close under the flip.
    Backtest only: fixT (entry + N x risk) and maxDays (sell at the close of the Nth day held).
    kcRide (kcu = upper Keltner band of this candle): an hourly close above the upper band starts a ride -> the target,
    the time / flip / VWAP / EMA exits are suspended, and the trade is sold at the first daily close under the band
    (the same day included). A target reached on a candle that closes above the band doesn't sell: it starts the ride."""
    if s.get("maxDays") and b["t"][:10] != s.get("_dd"):
        s["_dd"] = b["t"][:10]
        s["_dn"] = s.get("_dn", -1) + 1
    if is_last and s.get("kind") == "retest":         # each daily close: what the exits saw (shown on the chart)
        fl_ = (walls.get(b["t"][:10]) or {}).get("flip")
        s.setdefault("dlog", []).append([b["t"][:10], round(b["c"], 2), round(kcu, 2) if kcu is not None else None,
                                         float(fl_) if fl_ else None, 1 if s.get("riding") else 0])
        s["dlog"] = s["dlog"][-40:]
    if b["l"] <= s["stop"]:
        st_ = "breakeven" if s.get("scaled") and abs(s["stop"] - s["entry"]) < 1e-9 else "trail" if s.get("stops") else "stop"
        s.update(status=st_, exitAt=b["t"][:16], exit=round(min(b["o"], s["stop"]), 4))
        return
    if s.get("scaleOn") and not s.get("scaled"):     # part of the position sold at entry + N x risk
        lvl = s["entry"] + float(s.get("scaleR") or 1) * (s["entry"] - s["stop0"])
        if s["entry"] > s["stop0"] and b["h"] >= lvl:
            px_ = round(max(b["o"], lvl), 4)
            s["scaled"] = {"at": b["t"][:16], "px": px_, "frac": max(0.01, min(0.99, float(s.get("scalePct") or 50) / 100))}
            if s.get("scaleBE") and s["stop"] < s["entry"]:
                s["stop"] = s["entry"]                    # the rest can't lose any more
                s.setdefault("stops", [[s["at"], s["stop0"]]]).append([b["t"][:16], s["entry"]])
    if s.get("pvEod") and is_last and b["t"][:10] == s["at"][:10] and s.get("level") and b["c"] < float(s["level"]):
        s.update(status="eodpw", exitAt=b["t"][:16], exit=b["c"])        # the entry day closed under the support: out
        return
    pw_d = (walls.get(b["t"][:10]) or {}).get("putWall")
    if s.get("pwExit") and is_last and pw_d and b["c"] < float(pw_d):
        s.update(status="pwlost", exitAt=b["t"][:16], exit=b["c"], lostLevel=f"put wall {float(pw_d):g}")
        return                                         # the day closed under the day's put wall: out
    rf = (("inhRiseX", hreg and hreg.get("s") == "G", "inertiaH"), ("inhRedX", hreg and hreg.get("s") == "R", "inertiaH"),
          ("sqhRiseX", sqh and sqh["up"], "squeeze"),
          ("inRiseX", is_last and reg and reg.get("s") == "G", "inertia"), ("sqdRiseX", is_last and sqd and sqd["up"], "squeeze"),
          ("cvdRiseX", cv and cv[0] is not None and cv[1] is not None and cv[0] > cv[1], "cvd"),
          ("cvdFallX", cv and cv[0] is not None and cv[1] is not None and cv[0] < cv[1], "cvd"))
    if s.get("fxExit") and fx:
        s.update(status="formula", exitAt=b["t"][:16], exit=b["c"], exitWhy="formula")
        return                                         # the exit formula is true at this close
    for k_, hit, st_ in rf:                            # rising / falling exits: at this candle's close
        if s.get(k_) and hit:
            s.update(status=st_, exitAt=b["t"][:16], exit=b["c"], exitWhy=k_)
            return
    if s.get("sqhExit") and sqh and not sqh["up"]:
        s.update(status="squeeze", exitAt=b["t"][:16], exit=b["c"])     # hourly squeeze momentum falling: out at this close
        return
    if s.get("sqdExit") and is_last and sqd and not sqd["up"]:
        s.update(status="squeeze", exitAt=b["t"][:16], exit=b["c"])     # daily squeeze momentum falling at the daily close
        return
    if s.get("inhYExit") and hreg and hreg.get("s") != "G":
        s.update(status="inertiaH", exitAt=b["t"][:16], exit=b["c"])    # the hourly inertia is no longer green: out at this close
        return
    if s.get("inYExit") and is_last and reg and reg.get("s") != "G":
        s.update(status="inertia", exitAt=b["t"][:16], exit=b["c"])     # the day closes with the daily inertia no longer green: out
        return
    if s.get("inRedExit") and is_last and reg and reg.get("s") == "R":
        s.update(status="inertia", exitAt=b["t"][:16], exit=b["c"])     # the day closes with the daily inertia red: out
        return
    if s.get("fvgExit") and bear:                    # this candle completes a new bearish fair value gap: out at its close
        s.update(status="fvg", exitAt=b["t"][:16], exit=b["c"])
        return
    if s.get("cwLost") or s.get("dpLost"):           # passed a level, then the day closes back under it: out
        lv = []
        cw_d = (walls.get(b["t"][:10]) or {}).get("callWall")
        if s.get("cwLost") and cw_d:
            lv.append(("call wall", float(cw_d)))
        if s.get("dpLost") and dpl:
            lv += [("dark pool level", float(x)) for x in dpl if float(x) > s["entry"]]
        passed = s.setdefault("passed", [])
        for name, px in lv:
            if b["c"] > px and [name, px] not in passed:
                passed.append([name, px])                 # an hourly close above the level
        if is_last and passed:
            name, px = max(passed, key=lambda x: x[1])
            if b["c"] < px:
                s.update(status="lost", exitAt=b["t"][:16], exit=b["c"], lostLevel=f"{name} {px:g}")
                return
    ride_on = bool(s.get("kcRide")) and kcu is not None
    if ride_on and not s.get("riding") and b["c"] > kcu:
        s["riding"], s["rideAt"] = True, b["t"][:16]      # an hourly close above the upper Keltner band starts the ride
    if s.get("riding"):
        if is_last and kcu is not None and b["c"] < kcu:
            s.update(status="keltner", exitAt=b["t"][:16], exit=b["c"])  # the day closed under the upper band: out
            return
    else:
        hits = [(k, st) for k, st in (("target", "target"), ("fixT", "R")) if s.get(k) and b["h"] >= s[k]]
        if hits:
            k, st = hits[0]
            s.update(status=st, exitAt=b["t"][:16], exit=round(max(b["o"], s[k]), 4))
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
    sc = s.get("scaled")
    if sc:                                             # partial exit: the two parts weighted by their size
        pct = round(sc["frac"] * (sc["px"] / s["entry"] - 1) * 100 + (1 - sc["frac"]) * (ex / s["entry"] - 1) * 100, 2)
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


def hvc_series(bars, resets, adr_n=20):
    """Daily high-volume close (the TOS HighVolClose), set by the VWAP resets: at each reset the line becomes the
    close of that day, from the next day on (no look-ahead); bands = HVC × (1 + ADR%) and × (1 − 0.618 ADR%), ADR% =
    the average daily high / low range of the 20 days up to it. -> [(hvc, up, dn) or None per candle]."""
    D = {}
    for b in bars:
        if not ("09:30" <= b["t"][11:16] < "16:00"):
            continue
        d = b["t"][:10]
        x = D.get(d)
        if not x:
            D[d] = {"h": b["h"], "l": b["l"], "c": b["c"]}
        else:
            x["h"], x["l"], x["c"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"]
    days = sorted(D)
    pos = {d: k for k, d in enumerate(days)}
    lvl_at = {}                                        # the day the new level starts -> (hvc, up, dn)
    for i in resets:
        if i <= 0 or i >= len(bars):
            continue
        d = bars[i]["t"][:10]
        k = pos.get(d)
        if k is None or k + 1 >= len(days):
            continue                                   # the reset day isn't over yet: its close isn't known
        w = days[max(0, k - adr_n + 1):k + 1]
        adr = 100 * (sum(D[x]["h"] / D[x]["l"] for x in w if D[x]["l"] > 0) / max(1, len(w)) - 1)
        c = D[d]["c"]
        lvl_at[days[k + 1]] = (round(c, 4), round(c * (1 + adr / 100), 4), round(c * (1 - adr * 0.618 / 100), 4))
    out, cur = [], None
    for b in bars:
        if b["t"][:10] in lvl_at and (cur is None or cur[1] != b["t"][:10]):
            cur = (lvl_at[b["t"][:10]], b["t"][:10])
        out.append(cur[0] if cur else None)
    return out


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


def dp_level_map(prints, dates, top_n=5, window_days=66):
    """{date: [prices]}: the top dark pool levels of the ~2 months before each date (as drawn on the chart)."""
    import bisect
    import flowdata
    if not prints:
        return {}
    prints = sorted(prints, key=lambda p: p["t"])
    ts = [p["t"][:10] for p in prints]
    out = {}
    for d in dates:
        d0 = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")
        win = prints[bisect.bisect_left(ts, d0):bisect.bisect_left(ts, d)]
        if len(win) >= 5:
            out[d] = [lv["price"] for lv in flowdata.dp_levels(win, top_n)]
    return out


def pw_vwap_events(bars, walls, resets, near_pct=0.3, kc=None):
    """VWAP reset candles (the hourly candle with the highest volume of the period) vs the gamma walls of the day.
    touch / cwTouch: the candle's range touches the put wall / call wall (within near_pct %).
    kcPos: close in the Keltner channel (0 = lower band, 100 = upper band), kcHalf "lower"/"upper" (vs the midline).
    side: "long" = touches the put wall, closes in the lower Keltner half; "short" = touches the call wall, closes in
    the upper Keltner half; None otherwise. No side when the put wall equals the call wall.
    kc: keltner(bars) list aligned with bars (computed if missing)."""
    if kc is None:
        kc = keltner(bars, daily=True)
    out = []
    for j in sorted(set(resets)):
        if j <= 0 or j >= len(bars):
            continue
        b = bars[j]
        w = walls.get(b["t"][:10]) or {}
        pw, cw = w.get("putWall"), w.get("callWall")
        if not pw:
            continue
        pw, cw = float(pw), (float(cw) if cw else None)
        hit = lambda lv: lv is not None and b["l"] <= lv * (1 + near_pct / 100) and b["h"] >= lv * (1 - near_pct / 100)
        same = cw is not None and abs(pw - cw) < 1e-6
        k = _kc_pos(b["c"], kc[j] if j < len(kc) else None)
        touch, ctouch = hit(pw), hit(cw)
        side = None
        if not same and touch and k["kcHalf"] == "lower":
            side = "long"
        elif not same and ctouch and k["kcHalf"] == "upper":
            side = "short"
        out.append({"i": j, "t": b["t"][:16], "pw": pw, "cw": cw, "same": same, "touch": touch, "cwTouch": ctouch, "side": side,
                    "above": b["c"] >= pw, "green": b["c"] >= b["o"],
                    "o": round(b["o"], 2), "h": round(b["h"], 2), "l": round(b["l"], 2), "c": round(b["c"], 2), **k})
    return out


def _kc_pos(price, k):
    """Where a price sits in the Keltner channel: kcPos 0 = lower band, 50 = midline, 100 = upper band."""
    if not k or k[1] <= k[2]:
        return {"kcPos": None, "kcHalf": None}
    pos = round((price - k[2]) / (k[1] - k[2]) * 100)
    return {"kcPos": pos, "kcHalf": "upper" if price >= k[0] else "lower",
            "kcM": round(k[0], 2), "kcU": round(k[1], 2), "kcL": round(k[2], 2)}


def keltner(bars, n=21, mult=3.0, daily=False):
    """Keltner channel: EMA(n) of the close +/- mult x EMA(n) of the true range. [(mid, upper, lower) or None] per candle.
    daily: on daily candles, as a daily chart shows it: for each hourly candle, the EMAs of the previous days updated with
    today's candle so far (close, high, low up to this hour), so no later price is used."""
    if daily:
        return _keltner_daily(bars, n, mult)
    closes = [b["c"] for b in bars]
    tr = [max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"])) if i else b["h"] - b["l"]
          for i, b in enumerate(bars)]
    mid, rng = _ema_series(closes, n), _ema_series(tr, n)
    return [(round(m, 4), round(m + mult * r, 4), round(m - mult * r, 4)) if m is not None and r is not None else None
            for m, r in zip(mid, rng)]


def _keltner_daily(bars, n=21, mult=3.0):
    k = 2 / (n + 1)
    out, closes, trs = [], [], []                      # completed days
    e_prev = a_prev = None                             # EMAs at the previous day's close
    day, dh, dl, pc = None, None, None, None
    for i, b in enumerate(bars):
        d = b["t"][:10]
        if d != day:
            if day is not None:                         # yesterday is complete: commit it
                c_, tr_ = bars[i - 1]["c"], max(dh - dl, abs(dh - pc), abs(dl - pc)) if pc is not None else dh - dl
                closes.append(c_)
                trs.append(tr_)
                if e_prev is None:
                    if len(closes) >= n:
                        e_prev, a_prev = sum(closes[-n:]) / n, sum(trs[-n:]) / n
                else:
                    e_prev, a_prev = c_ * k + e_prev * (1 - k), tr_ * k + a_prev * (1 - k)
                pc = c_
            day, dh, dl = d, b["h"], b["l"]
        else:
            dh, dl = max(dh, b["h"]), min(dl, b["l"])
        if e_prev is None:
            out.append(None)
            continue
        tr = max(dh - dl, abs(dh - pc), abs(dl - pc)) if pc is not None else dh - dl
        m, r = b["c"] * k + e_prev * (1 - k), tr * k + a_prev * (1 - k)
        out.append((round(m, 4), round(m + mult * r, 4), round(m - mult * r, 4)))
    return out


REGIME_PTS = {"RY": 2.0, "YG": 1.5, "GG": 3.0, "YY": 0.5, "RR": -2.0, "RG": 1.5, "YR": -2.0, "GY": -1.0}


def _linreg_end(ys):
    """Least-squares line through ys (x = 0..n-1), its value at the last point (the TOS inertia)."""
    n = len(ys)
    sx, sxx = n * (n - 1) / 2, (n - 1) * n * (2 * n - 1) / 6
    sy = sum(ys)
    sxy = sum(i * y for i, y in enumerate(ys))
    den = n * sxx - sx * sx
    if not den:
        return ys[-1]
    a = (n * sxy - sx * sy) / den
    b = (sy - a * sx) / n
    return a * (n - 1) + b


def inertia_regime(bars, ns=(70, 40, 20)):
    """Daily inertia regime on hourly candles, as a daily chart shows it: for each candle, the inertia of the daily closes
    (today's close so far as the last one) vs the same at yesterday's close, for each length in ns.
    G = all rising, R = none rising, Y = mixed; pts = the TOS regime points of (yesterday's state -> today's).
    [{"s": "G"|"Y"|"R", "p": yesterday's state, "pts": float} or None]"""
    closes, out = [], []
    prev_i, prev_state, cur_day, sums = None, None, None, None
    for k, b in enumerate(bars):
        d = b["t"][:10]
        if d != cur_day:
            if cur_day is not None:                    # yesterday is complete
                closes.append(bars[k - 1]["c"])
                if len(closes) >= max(ns):
                    new_i = [_linreg_end(closes[-n:]) for n in ns]
                    if prev_i is not None:
                        prev_state = "G" if all(x > y for x, y in zip(new_i, prev_i)) else "R" if all(x <= y for x, y in zip(new_i, prev_i)) else "Y"
                    prev_i = new_i
                    # today's inertia = line through the last n-1 closes + today's close: their sums are fixed for the day
                    sums = []
                    for n in ns:
                        w = closes[-(n - 1):]
                        sx, sxx = n * (n - 1) / 2, (n - 1) * n * (2 * n - 1) / 6
                        sums.append((n, sum(w), sum(i * y for i, y in enumerate(w)), sx, n * sxx - sx * sx))
            cur_day = d
        if prev_i is None:
            out.append(None)
            continue
        c, up = b["c"], []
        for (n, sy0, sxy0, sx, den), pi in zip(sums, prev_i):
            sy, sxy = sy0 + c, sxy0 + (n - 1) * c
            a = (n * sxy - sx * sy) / den if den else 0.0
            up.append(a * (n - 1) + (sy - a * sx) / n > pi)
        st = "G" if all(up) else "R" if not any(up) else "Y"
        out.append({"s": st, "p": prev_state, "pts": REGIME_PTS.get((prev_state or st) + st, 0.0)})
    return out


def inertia_hourly(bars, ns=(70, 40, 20)):
    """Inertia regime on the hourly closes: for each candle, the regression end value over the last n closes vs the same
    one candle earlier, for each n. G = all rising, R = none, Y = mixed. [{"s"} or None] (rolling sums: O(candles))."""
    ys = [b["c"] for b in bars]
    N = len(ys)
    up = []
    for n in ns:
        r = [None] * N
        if N > n:
            sx, sxx = n * (n - 1) / 2, (n - 1) * n * (2 * n - 1) / 6
            den = n * sxx - sx * sx
            sy = sum(ys[:n])
            sxy = sum(i * y for i, y in enumerate(ys[:n]))
            prev = None
            for k in range(n - 1, N):
                if k >= n:                                 # slide the window by one candle
                    y_out, y_in = ys[k - n], ys[k]
                    sxy = sxy - sy + y_out + (n - 1) * y_in
                    sy = sy - y_out + y_in
                a = (n * sxy - sx * sy) / den if den else 0.0
                cur = a * (n - 1) + (sy - a * sx) / n
                if prev is not None:
                    r[k] = cur > prev
                prev = cur
        up.append(r)
    out = []
    for k in range(N):
        v = [u[k] for u in up]
        out.append(None if any(x is None for x in v) else {"s": "G" if all(v) else "R" if not any(v) else "Y"})
    return out


def squeeze(bars, n=20, bb=2.0, kc=1.5, fire_n=3):
    """TTM squeeze per candle, as thinkorswim's TTM_Squeeze computes it: {"on": BB inside KC, "fired": turned off within
    the last fire_n candles, "mom": momentum, "up": momentum rising} or None.
    BB = SMA(n) +/- bb x stdev(n); KC = SMA(n) +/- kc x SMA(n) of the true range (TOS KeltnerChannels, simple averages);
    squeeze on = upper BB under the upper KC. Momentum = linear regression end value (Inertia) over n of
    close - ((highest high + lowest low) / 2 + EMA(n) of the close) / 2."""
    N = len(bars)
    cl = [b["c"] for b in bars]
    tr = [max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"])) if i else b["h"] - b["l"]
          for i, b in enumerate(bars)]
    em = _ema_series(cl, n)
    on, dev = [None] * N, [None] * N
    for i in range(n - 1, N):
        w = cl[i - n + 1:i + 1]
        m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        atr_ = sum(tr[i - n + 1:i + 1]) / n
        on[i] = (m + bb * sd) < (m + kc * atr_)
        if em[i] is not None:
            hh = max(b["h"] for b in bars[i - n + 1:i + 1])
            ll = min(b["l"] for b in bars[i - n + 1:i + 1])
            dev[i] = cl[i] - ((hh + ll) / 2 + em[i]) / 2
    out = [None] * N
    last_turn = None
    for i in range(N):
        if on[i] is None or i < n - 1 or any(v is None for v in dev[i - n + 1:i + 1]):
            continue
        mom = _linreg_end(dev[i - n + 1:i + 1])
        prev = out[i - 1]["mom"] if out[i - 1] else None
        prev2 = out[i - 2]["mom"] if i >= 2 and out[i - 2] else None
        fired = any(on[j - 1] and on[j] is False for j in range(max(1, i - fire_n + 1), i + 1) if on[j - 1] is not None)
        # turn up (the yellow arrow on the chart): momentum under 0 that was falling and now rises
        if prev is not None and prev2 is not None and mom < 0 and mom > prev and prev2 > prev:
            last_turn = i
        out[i] = {"on": bool(on[i]), "fired": bool(fired), "mom": round(mom, 4), "up": prev is not None and mom > prev,
                  "tAgo": (i - last_turn) if last_turn is not None else None}
    return out


def _sq_ok(x, kind, turn_n=1):
    """A squeeze rule on one candle's squeeze state."""
    if not x:
        return False
    if kind == "turn":                                 # turned up on this candle or within the last turn_n candles
        return x.get("tAgo") is not None and x["tAgo"] < max(1, int(turn_n or 1))
    if kind == "rise":
        return bool(x["up"])
    if kind == "fall":
        return not x["up"]
    if kind == "on":
        return x["on"]
    if kind == "fire":
        return x["fired"] and x["mom"] > 0
    return x["mom"] > 0 and x["up"]                    # "mom"


def squeeze_daily_prev(hbars, n=20, bb=2.0, kc=1.5, fire_n=3):
    """For each hourly candle: the daily squeeze of the PREVIOUS completed day (no look-ahead)."""
    db_ = daily_bars(hbars)
    sq = squeeze(db_, n, bb, kc, fire_n)
    prev = {}
    for k, b in enumerate(db_):
        prev[b["t"][:10]] = sq[k - 1] if k else None
    return [prev.get(b["t"][:10]) for b in hbars]


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


def fvg_zones(bars, min_atr=0.2, atr=None, max_days=10):
    """Fair value gaps on these candles: bullish when the low of candle i is above the high of candle i-2, bearish when
    the high of candle i is under the low of candle i-2; kept when the gap is at least min_atr x ATR(14).
    A zone is usable from candle i+1 and is filled when a later candle trades through it (low under the bottom of a
    bullish gap / high over the top of a bearish one). [{i, t, type, lo, hi, fillI, fillAt}]"""
    atr = atr or atr14(bars)
    out = []
    for i in range(2, len(bars)):
        a, c = bars[i - 2], bars[i]
        m = (atr[i] or 0) * min_atr
        if c["l"] > a["h"] and c["l"] - a["h"] >= m:
            out.append({"i": i, "t": bars[i - 1]["t"][:16], "type": "bull", "lo": round(a["h"], 4), "hi": round(c["l"], 4)})
        elif c["h"] < a["l"] and a["l"] - c["h"] >= m:
            out.append({"i": i, "t": bars[i - 1]["t"][:16], "type": "bear", "lo": round(c["h"], 4), "hi": round(a["l"], 4)})
    for z in out:                                      # filled, or expired after max_days sessions
        z["fillI"] = z["fillAt"] = z["end"] = None
        d0, nd = bars[z["i"]]["t"][:10], 0
        for j in range(z["i"] + 1, len(bars)):
            if bars[j]["t"][:10] != bars[j - 1]["t"][:10]:
                nd += 1
            if max_days and nd > max_days:
                z["fillI"], z["fillAt"], z["end"] = j, bars[j]["t"][:16], "expired"
                break
            if (z["type"] == "bull" and bars[j]["l"] < z["lo"]) or (z["type"] == "bear" and bars[j]["h"] > z["hi"]):
                z["fillI"], z["fillAt"], z["end"] = j, bars[j]["t"][:16], "filled"
                break
    return out


def fvg_bear_new(bars, min_atr=0.2, atr=None):
    """Per candle: True when it completes a bearish fair value gap (its high under the low of two candles before)."""
    out = [False] * len(bars)
    for z in fvg_zones(bars, min_atr, atr):
        if z["type"] == "bear":
            out[z["i"]] = True
    return out


def fvg_bull_touch(bars, zones, near=0.0, atr=None):
    """Per candle: the open bullish FVG (formed before it, not filled before it) that its low trades into, or comes
    within near x ATR above, while it closes above the bottom of the gap, else None."""
    out = [None] * len(bars)
    bulls = [z for z in zones if z["type"] == "bull"]
    atr = atr or (atr14(bars) if near else [0] * len(bars))
    for j, b in enumerate(bars):
        m = near * (atr[j] or 0)
        for z in bulls:
            if z["i"] < j and (z["fillI"] is None or z["fillI"] >= j) and b["l"] <= z["hi"] + m and b["c"] > z["lo"]:
                out[j] = z
                break
    return out


REL_DAYS, REL_MIN_N, REL_FLOOR = 30, 5, 50_000


def unusual_calls(alerts, fallback=500_000, mult=3.0):
    """Call alerts that are unusual for this ticker: premium >= mult x the median premium of its call alerts of the
    previous REL_DAYS days (only past alerts: no look-ahead), and at least REL_FLOOR. With fewer than REL_MIN_N past
    alerts the fixed `fallback` is used. Adds "x" = premium / that median (None on the fallback)."""
    import bisect
    calls = sorted((a for a in (alerts or []) if a.get("c")), key=lambda a: a["t"])
    ts = [a["t"] for a in calls]
    out = []
    for k, a in enumerate(calls):
        try:
            lo_t = (datetime.strptime(a["t"][:10], "%Y-%m-%d") - timedelta(days=REL_DAYS)).strftime("%Y-%m-%d")
        except ValueError:
            continue
        prev = sorted(float(x.get("p") or 0) for x in calls[bisect.bisect_left(ts, lo_t):k])
        p = float(a.get("p") or 0)
        if len(prev) >= REL_MIN_N:
            med = prev[len(prev) // 2]
            if med > 0 and p >= max(REL_FLOOR, mult * med):
                out.append({**a, "x": round(p / med, 1)})
        elif p >= fallback:
            out.append({**a, "x": None})
    return out


SEARCH_RULES = ["alertReq", "relFlow", "greenOnly", "aboveFlip", "abovePW", "belowPW", "vwapOn", "emaOn", "emaOverVwap", "kcNotExt", "kcdLower",
                "kchLower", "orbAbove", "fvgOn", "noAboveCW", "cwRoomOn", "vwapReset", "pwNeCw", "spyUp", "skewOn", "inGreen", "inNotRed"]


def _search_candidates(bars, walls, start, P, out, mk, a_of, alerts, vmin, ndays, zone_atr, sup_keys, vwap, ema, kc, orb, spy,
                       skp, earn_d, earn_days, cw_room, k_trail, flip_exit, dplv, bear, inr=None):
    """Every candle whose low comes back to the support and closes above it, traded alone with the exits of P, and
    whether each entry rule would let it in. The search then replays any mix of rules one trade at a time."""
    n_ = len(bars)
    kch = keltner(bars, int(P.get("kchLen") or 21), float(P.get("kchMult") or 3))
    fvg_t = fvg_bull_touch(bars, fvg_zones(bars, float(P.get("fvgMin") or 0.2), atr14(bars), int(P.get("fvgDays") or 10)),
                           float(P.get("fvgNear") if P.get("fvgNear") is not None else 0.25), atr14(bars))
    vreset = set(anchored_vwap(bars, int(P.get("vwapDays") or VWAP_DAYS))[1])
    fixed = sorted((a for a in (alerts or []) if a.get("c") and (a.get("p") or 0) >= vmin), key=lambda a: a["t"])
    rel = unusual_calls(alerts, vmin, float(P.get("relMult") or 3))
    days = sorted({b["t"][:10] for b in bars})
    didx = {d: k for k, d in enumerate(days)}
    import bisect
    ts_of = {id(fixed): [a["t"] for a in fixed], id(rel): [a["t"] for a in rel]}
    def armed(lst, k_day, end):
        ts = ts_of[id(lst)]
        j = bisect.bisect_right(ts, end) - 1
        if j < 0:
            return False
        dk = bisect.bisect_right(days, lst[j]["t"][:10]) - 1
        return dk >= 0 and k_day - dk <= ndays
    for i, b in enumerate(bars):
        day = b["t"][:10]
        if start and day < start:
            continue
        w = walls.get(day) or {}
        lv = [(float(w[k]), nm) for k, nm in sup_keys if w.get(k)]
        under = [x for x in lv if x[0] < b["c"]]
        if not under:
            continue
        lvl, lname = max(under)
        a_ = a_of(i, day)
        if not a_ or not b["l"] <= lvl + max(lvl * RETEST_NEAR / 100, zone_atr * a_):
            continue
        end = _bar_end(b["t"]).strftime("%Y-%m-%dT%H:%M")
        cw_ = float(w["callWall"]) if w.get("callWall") else None
        f = {"alertReq": armed(fixed, didx[day], end), "relFlow": armed(rel, didx[day], end), "greenOnly": b["c"] > b["o"],
             "aboveFlip": bool(w.get("flip") and b["c"] > float(w["flip"])),
             "abovePW": bool(w.get("putWall") and b["c"] > float(w["putWall"])),
             "belowPW": bool(w.get("putWall") and b["c"] < float(w["putWall"])),
             "vwapOn": vwap[i] is not None and b["c"] > vwap[i], "emaOn": ema[i] is not None and b["c"] > ema[i],
             "emaOverVwap": ema[i] is not None and vwap[i] is not None and ema[i] > vwap[i],
             "kcNotExt": bool(kc[i] and b["c"] < kc[i][1]), "kcdLower": bool(kc[i] and b["c"] <= kc[i][0]),
             "kchLower": bool(kch[i] and b["c"] <= kch[i][0]), "orbAbove": bool(orb[i] and b["c"] > orb[i][0]),
             "fvgOn": bool(fvg_t[i]), "noAboveCW": not (cw_ and (b["o"] > cw_ or b["c"] > cw_)),
             "cwRoomOn": not (cw_ and cw_room and cw_ - b["c"] < cw_room * a_), "vwapReset": i in vreset,
             "pwNeCw": not (w.get("putWall") and w.get("callWall") and abs(float(w["putWall"]) - float(w["callWall"])) < 1e-6),
             "inGreen": bool(inr and inr[i] and inr[i]["s"] == "G"), "inNotRed": bool(inr and inr[i] and inr[i]["s"] != "R"),
             "spyUp": spy.get(b["t"][:16]) != -1, "skewOn": skp.get(day) is not None and 33.4 <= skp[day] < 66.7,
             "earnOn": not (earn_d and earn_days and 0 <= (earn_d - datetime.strptime(day, "%Y-%m-%d")).days <= earn_days)}
        s = mk(i, b, w, lvl, lname, a_, {"armedAt": None, "armPremium": 0}, day)
        for j in range(i + 1, n_):
            _step(s, bars[j], walls, k_trail, flip_exit, is_last=(j + 1 == n_ or bars[j + 1]["t"][:10] != bars[j]["t"][:10]),
                  vw=vwap[j], em=ema[j], kcu=kc[j][1] if kc[j] else None, dpl=dplv.get(bars[j]["t"][:10]), bear=bear[j], reg=inr[j] if inr else None)
            if s["status"] != "open":
                break
        pct, r = _result(s)
        if r is None:
            continue
        out.append({"at": b["t"][:16], "end": s.get("exitAt") or "9999", "R": r, "f": f})



def detect_retest(bars, walls, start=None, cfg=None, datr=None, alerts=None, earnings=None, vwap_map=None, ema_map=None,
                  extra=None):
    """Flow retest: a call alert of at least retestMin arms the ticker (a new alert re-arms it). The support is the highest
    of put wall / flip at or under the price at the close of the alert's candle. From that candle on, within
    retestDays trading days: a green hourly candle whose low reaches the support (within 0.3%) and that closes above it
    is the entry. An hourly close under the stop before the entry ends the watch. Target: the call wall of the day if it's above the entry (minus the buffer), else trailing only.
    Returns (signals, current watch or None)."""
    P = {**PARAMS, **(cfg or {})}
    if P.get("tf") == "1D":                            # lab: the strategy on daily candles
        if P.get("_collect") is not None:
            return [], None
        return detect_daily(bars, walls, start, P, earnings, alerts, (extra or {}).get("dpLevels")), None
    if P.get("pvSetup"):                               # the VWAP reset on the put wall strategy
        return detect_pvlong(bars, walls, start, P, datr, earnings, vwap_map, ema_map, extra), None
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
    cvr = cvd_roll(bars, int(P.get("cvdN") or 20)) if any(on(k) for k in ("cvdUp", "cvdDn", "cvdRise", "cvdFall", "cvdRiseX", "cvdFallX")) else None
    ema = [ema_map.get(b["t"][:16]) for b in bars] if ema_map else _ema_series([b["c"] for b in bars], int(P.get("emaLen") or 20))
    extra = extra or {}
    kc = [extra["kc"].get(b["t"][:16]) for b in bars] if extra.get("kc") else keltner(bars, int(P.get("kcLen") or 21), float(P.get("kcMult") or 3), bool(P.get("kcDaily", True)))
    kch = ([extra["kch"].get(b["t"][:16]) for b in bars] if extra.get("kch") else keltner(bars, int(P.get("kchLen") or 21), float(P.get("kchMult") or 3))) \
        if on("kchLower") else None
    orb = orb_levels(bars)
    spy = extra.get("spy") or {}
    skp = extra.get("skewPct") or {}
    dplv = extra.get("dpLevels") or {}                  # {date: top dark pool level prices known before that day}
    force = extra.get("force")                          # {"at", "level", "levelName"}: replay the exits of a given entry
    misses = extra.get("misses")                        # optional list: candles at the support that didn't enter, and why
    seen_miss = set()

    def miss(b, why):
        if misses is None or (start and b["t"][:10] < start):
            return
        k = (b["t"][:10], why)
        if k not in seen_miss:                          # once per day and reason
            seen_miss.add(k)
            misses.append({"t": b["t"][:16], "r": why, "l": b["l"]})                    # {date: percentile of the previous session's skew}
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
    fvg_t = fvg_bull_touch(bars, fvg_zones(bars, float(P.get("fvgMin") or 0.2), atr, int(P.get("fvgDays") or 10)),
                           float(P.get("fvgNear") if P.get("fvgNear") is not None else 0.25), atr) if on("fvgOn") else None
    bear = fvg_bear_new(bars, float(P.get("fvgMin") or 0.2), atr) if on("fvgExit") else [False] * len(bars)
    fxe, fxx = fx_series(bars, P, walls, True, vwap, dplv, alerts)
    in_ns = (int(P.get("inN1") or 70), int(P.get("inN2") or 40), int(P.get("inN3") or 20))
    inr = (extra.get("inr") and [extra["inr"].get(b["t"][:16]) for b in bars]) or \
        (inertia_regime(bars, in_ns) if (on("inGreen") or on("inNotRed") or on("inRedExit") or on("inFall") or on("inRiseX") or P.get("_collect") is not None) else None)
    inh = inertia_hourly(bars, (int(P.get("inhN1") or 70), int(P.get("inhN2") or 40), int(P.get("inhN3") or 20))) \
        if any(on(k) for k in ("inhGreen", "inhNotRed", "inhYExit", "inhFall", "inhRiseX", "inhRedX")) else None
    fire_n = int(P.get("sqFireN") or 3)
    sqh = squeeze(bars, int(P.get("sqhLen") or 20), float(P.get("sqhBB") or 2), float(P.get("sqhKC") or 1.5), fire_n) \
        if any(on(k) for k in ("sqhOn", "sqhFire", "sqhMom", "sqhExit", "sqhTurn", "sqhRise", "sqhFall", "sqhRiseX")) else None
    sqd_e = squeeze_daily_prev(bars, int(P.get("sqdLen") or 20), float(P.get("sqdBB") or 2), float(P.get("sqdKC") or 1.5), fire_n) \
        if any(on(k) for k in ("sqdOn", "sqdFire", "sqdMom", "sqdTurn", "sqdRise", "sqdFall")) else None      # entries: yesterday's daily squeeze
    sqd_x = None
    if on("sqdExit") or on("sqdRiseX"):                                  # exits: the day's own squeeze at its close
        db0 = daily_bars(bars)
        s0 = squeeze(db0, int(P.get("sqdLen") or 20), float(P.get("sqdBB") or 2), float(P.get("sqdKC") or 1.5), fire_n)
        at0 = {b["t"][:10]: x for b, x in zip(db0, s0)}
        sqd_x = [at0.get(b["t"][:10]) for b in bars]
    hvl = None
    if on("hvcAbove") or on("hvcBelow"):              # daily high-volume close, set by the VWAP resets
        if extra.get("hvc") is not None:
            hvl = [extra["hvc"].get(b["t"][:16]) for b in bars]
        else:
            if extra.get("vwResets") is not None:
                rs0 = set(extra["vwResets"])
                ridx = [i for i, b in enumerate(bars) if b["t"][:16] in rs0]
            else:
                ridx = anchored_vwap(bars, int(P.get("vwapDays") or VWAP_DAYS))[1]
            hvl = [x[0] if x else None for x in hvc_series(bars, ridx)]
    if on("vwapReset"):
        if extra.get("vwResets") is not None:
            rs_ = set(extra["vwResets"])
            vreset = {i for i, b in enumerate(bars) if b["t"][:16] in rs_}
        else:
            vreset = set(anchored_vwap(bars, int(P.get("vwapDays") or VWAP_DAYS))[1])
    need_alert = on("alertReq")
    big = [] if not need_alert else (unusual_calls(alerts, vmin, float(P.get("relMult") or 3)) if on("relFlow") else
                                     sorted((a for a in (alerts or []) if a.get("c") and (a.get("p") or 0) >= vmin), key=lambda a: a["t"]))
    def mk(i, b, w, lvl, lname, a_, watch, day):
        """A new trade on candle i at the support lvl (the same for real entries and for the rule search)."""
        cw = float(w["callWall"]) if w.get("callWall") else None
        tgt = round(cw * (1 - buf / 100), 4) if on("targetOn") and cw and cw * (1 - buf / 100) > b["c"] * 1.002 else None
        st = round(lvl - k_stop * a_, 4)              # stop0: the risk unit for R, even with the stop off
        s = {"at": b["t"][:16], "kind": "retest", "level": lvl, "levelName": lname, "armedAt": watch["armedAt"],
             "armPremium": watch["armPremium"], "armX": watch.get("armX"), "entry": b["c"], "pw": float(w["putWall"]) if w.get("putWall") else None,
             "flip": float(w["flip"]) if w.get("flip") else None, "cw": cw, "target": tgt, "atr": round(a_, 4),
             "stop": st if on("stopOn") else 0.0, "stop0": st, "v": ver, "o": b["o"], "h": b["h"], "l": b["l"], "status": "open",
             "last": b["c"], "lastAt": b["t"][:16], "i0": i,
             "aboveFlipAtEntry": bool(w.get("flip") and b["c"] > float(w["flip"])), "flipChg": _flip_chg(walls, day),
             "vwap": vwap[i], "ema": round(ema[i], 4) if ema[i] is not None else None,
             "vwapExit": on("vwapExit"), "emaExit": on("emaExit"), "kcRide": on("kcRide"),
             "cwLost": on("cwLost"), "dpLost": on("dpLost"), "fvgExit": on("fvgExit"), "pvEod": on("pvEod"), "pwExit": on("pwExit"), **{k_: on(k_) for k_ in RISE_FALL_EXITS}, "fxExit": bool(P.get("fxExit")),
             "scaleOn": on("scaleOn"), "scaleR": float(P.get("scaleR") or 1), "scalePct": float(P.get("scalePct") or 50), "scaleBE": on("scaleBE"),
             "inRedExit": on("inRedExit"), "inYExit": on("inYExit"), "inhYExit": on("inhYExit"), "sqhExit": on("sqhExit"), "sqdExit": on("sqdExit"),
             "inhSt": (inh[i] or {}).get("s") if inh else None, "inSt": (inr[i] or {}).get("s") if inr else None,
             "skewPct": skp.get(day), "kc": list(kc[i]) if kc[i] else None, "orb": list(orb[i]) if orb[i] else None, "spy": spy.get(b["t"][:16])}
        if fvg_t is not None and fvg_t[i]:        # the gap the entry used (shown on the chart)
            z_ = fvg_t[i]
            s["fvgZone"] = {"t": z_["t"], "lo": z_["lo"], "hi": z_["hi"]}
        if P.get("fixR"):                          # backtest: fixed target at N x the risk
            s["fixT"] = round(b["c"] + float(P["fixR"]) * (b["c"] - st), 4)
        if P.get("maxDays"):
            s["maxDays"], s["_dd"], s["_dn"] = int(P["maxDays"]), day, 0
        return s

    collect = P.get("_collect")
    if collect is not None:                            # rule search: every candle at the support, each rule as a flag
        _search_candidates(bars, walls, start, P, collect, mk, a_of=lambda i, d: (_atr_on(datr, d) if datr else None) or atr[i],
                           alerts=alerts, vmin=vmin, ndays=ndays, zone_atr=zone_atr, sup_keys=sup_keys, vwap=vwap, ema=ema, kc=kc,
                           orb=orb, spy=spy, skp=skp, earn_d=earn_d, earn_days=earn_days, cw_room=cw_room, k_trail=k_trail,
                           flip_exit=flip_exit, dplv=dplv, bear=bear, inr=inr)
        return [], None

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
            _step(open_, b, walls, k_trail, flip_exit, is_last=(i + 1 == len(bars) or bars[i + 1]["t"][:10] != day), vw=vwap[i], em=ema[i],
                  kcu=kc[i][1] if kc[i] else None, dpl=dplv.get(day), bear=bear[i], reg=inr[i] if inr else None, hreg=inh[i] if inh else None,
                  sqh=sqh[i] if sqh else None, sqd=sqd_x[i] if sqd_x else None, cv=(cvr[i], cvr[i - 1] if i else None) if cvr else None,
                  fx=bool(fxx and fxx[i]))
            if open_["status"] != "open":
                open_ = None
            continue
        if force is not None:                          # review: the entry is given, only the exits run (current settings)
            if not sigs and b["t"][:16] == force["at"]:
                s = mk(i, b, walls.get(day) or {}, float(force.get("level") or b["l"]), force.get("levelName") or "level", a_,
                       {"armedAt": None, "armPremium": 0}, day)
                sigs.append(s)
                open_ = s
            continue
        w = walls.get(day) or {}
        if arm:                                        # call flow (new or again): the support is the level at / under price
            lv = [(float(w[k]), n) for k, n in sup_keys if w.get(k)]   # the call wall is the target, never the support
            under = [x for x in lv if x[0] <= b["c"] * (1 + RETEST_NEAR / 100)] if sup_keys else [(0.0, "")]
            if under:
                lvl0, name = max(under)
                watch = {"armedAt": arm["t"], "armPremium": arm["p"], "armX": arm.get("x"), "level": lvl0, "levelName": name, "day0": day, "days": 0,
                         "since": b["t"][:16]}
        if not watch and not need_alert:              # no call alert needed: always watching
            watch = {"armedAt": None, "armPremium": 0, "level": 0, "levelName": "", "day0": day, "days": 0, "since": b["t"][:16], "free": True}
        if not watch:
            if misses is not None:                      # at a support without a recent call alert: say so
                lv0 = [float(w[k]) for k, n in sup_keys if w.get(k)]
                if any(b["l"] <= x + max(x * RETEST_NEAR / 100, zone_atr * a_) and b["c"] > x for x in lv0):
                    miss(b, f"no unusual call alert (≥ {float(P.get('relMult') or 3):g}× its 30-day median) in the {ndays} days before"
                         if on("relFlow") else f"no call alert ≥ ${vmin / 1000:,.0f}K in the {ndays} days before")
            continue
        if day != watch.get("_d"):
            watch["_d"] = day
            if day != watch["day0"]:
                watch["days"] += 1
        if watch["days"] > ndays and not watch.get("free"):
            watch = None
            continue
        if not watch.get("free") and b["c"] < watch["level"] - k_stop * a_:     # the support of the alert broken before any retest
            miss(b, f"closed under the {watch['levelName']} {watch['level']:g} − stop: watch cancelled, waiting for a new call alert")
            watch = None
            continue
        # the support follows the price: the highest put wall / flip of the day under this candle's close
        lv = [(float(w[k]), n) for k, n in sup_keys if w.get(k)]   # the call wall is the target, never the support
        under = [x for x in lv if x[0] < b["c"]]
        if not sup_keys:                               # no support rule on: any candle, the stop goes under its low
            under = [(b["l"], "candle low")]
        if not under:
            continue
        lvl, lname = max(under)
        zone = max(lvl * RETEST_NEAR / 100, zone_atr * a_)
        # entry: a green candle whose low comes back to the support (within the zone) and that closes above it
        if b["l"] <= lvl + zone and b["c"] <= b["o"] and on("greenOnly"):
            miss(b, "red candle (Green candle rule)")
        if (b["c"] > b["o"] or not on("greenOnly")) and b["l"] <= lvl + zone:
            if P.get("aboveFlip") and not (w.get("flip") and b["c"] > float(w["flip"])):
                miss(b, f"close under the flip {w.get('flip')} (Above the flip rule)")
                continue                               # only above the gamma flip (positive gamma): wait for another candle
            if on("abovePW") and not (w.get("putWall") and b["c"] > float(w["putWall"])):
                miss(b, f"close under the put wall {w.get('putWall')} (Above the PW rule)")
                continue                               # only above the put wall
            if on("belowPW") and not (w.get("putWall") and b["c"] < float(w["putWall"])):
                miss(b, f"close above the put wall {w.get('putWall')} (Below the PW rule)")
                continue
            if fxe is not None and not fxe[i]:
                miss(b, f"entry formula false ({P.get('fxEntry')})")
                continue
            if cvr is not None and not cvd_ok(P, cvr[i], None, cvr[i - 1] if i else None):
                miss(b, f"CVD of the last {int(P.get('cvdN') or 20)} candles {cvr[i]:,.0f}" + (" · not rising" if on("cvdRise") else "") + (" · not falling" if on("cvdFall") else "") + " (CVD rule)")
                continue
            if earn_d and earn_days and 0 <= (earn_d - datetime.strptime(day, "%Y-%m-%d")).days <= earn_days:
                miss(b, f"earnings {str(earnings)[:10]} within {earn_days} days")
                continue                               # earnings within the next N days: no entry
            cw_ = float(w["callWall"]) if w.get("callWall") else None
            if on("vwapReset") and i not in vreset:
                miss(b, "not a VWAP reset candle")
                continue                               # rule on: the entry candle must be the VWAP reset candle
            if on("pwNeCw") and w.get("putWall") and w.get("callWall") and abs(float(w["putWall"]) - float(w["callWall"])) < 1e-6:
                miss(b, "put wall = call wall")
                continue                               # rule on: no entry when the walls are the same strike
            if on("vwapOn") and not (vwap[i] is not None and b["c"] > vwap[i]):
                miss(b, f"close {b['c']:.2f} under the VWAP {vwap[i]:.2f}" if vwap[i] is not None else "no VWAP yet")
                continue                               # rule on: the close must be above the VWAP
            if on("hvcAbove") and not (hvl[i] is not None and b["c"] > hvl[i]):
                miss(b, f"close {b['c']:.2f} under the daily HVC {hvl[i]:.2f}" if hvl[i] is not None else "no daily HVC yet")
                continue                               # rule on: the close must be above the daily high-volume close
            if on("hvcBelow") and not (hvl[i] is not None and b["c"] < hvl[i]):
                miss(b, f"close {b['c']:.2f} above the daily HVC {hvl[i]:.2f}" if hvl[i] is not None else "no daily HVC yet")
                continue                               # rule on: the close must be under the daily high-volume close
            if on("emaOn") and not (ema[i] is not None and b["c"] > ema[i]):
                miss(b, "close under the EMA")
                continue                               # rule on: the close must be above the EMA
            if on("emaOverVwap") and not (ema[i] is not None and vwap[i] is not None and ema[i] > vwap[i]):
                miss(b, "EMA under the VWAP")
                continue                               # rule on: the EMA must be above the VWAP
            if on("skewOn") and not (skp.get(day) is not None and 33.4 <= skp[day] < 66.7):
                miss(b, "skew not normal")
                continue                               # rule on: skew must be normal (middle third of its last 6 months)
            if on("spyUp") and spy.get(b["t"][:16]) == -1:
                miss(b, "SPY trending down")
                continue                               # rule on: SPY must be up over the last 6 sessions
            if on("fvgOn") and not fvg_t[i]:
                miss(b, f"not in (or within {float(P.get('fvgNear') if P.get('fvgNear') is not None else 0.25):g} ATR of) an open bullish FVG")
                continue                               # rule on: the candle must trade into an open bullish fair value gap
            if on("kchLower") and not (kch[i] and b["c"] <= kch[i][0]):
                miss(b, "close in the upper half of the hourly Keltner")
                continue                               # rule on: the close must be in the lower half of the hourly channel
            if on("inGreen") and not (inr and inr[i] and inr[i]["s"] == "G"):
                miss(b, f"daily inertia {(inr[i] or {}).get('s', '?') if inr else '?'} (not green)")
                continue                               # rule on: the 3 daily inertias must all be rising
            if on("inNotRed") and not (inr and inr[i] and inr[i]["s"] != "R"):
                miss(b, "daily inertia red")
                continue                               # rule on: not all 3 daily inertias falling
            sq_bad = next((f"{tf_} squeeze: {nm_}" for k, tf_, kind, nm_, arr in (
                ("sqhOn", "hourly", "on", "not on", sqh), ("sqhFire", "hourly", "fire", "not fired up", sqh), ("sqhMom", "hourly", "mom", "momentum not rising", sqh),
                ("sqdOn", "daily", "on", "not on", sqd_e), ("sqdFire", "daily", "fire", "not fired up", sqd_e), ("sqdMom", "daily", "mom", "momentum not rising", sqd_e),
                ("sqhTurn", "hourly", "turn", "no turn up", sqh), ("sqdTurn", "daily", "turn", "no turn up", sqd_e),
                ("sqhRise", "hourly", "rise", "momentum not rising", sqh), ("sqhFall", "hourly", "fall", "momentum not falling", sqh),
                ("sqdRise", "daily", "rise", "momentum not rising", sqd_e), ("sqdFall", "daily", "fall", "momentum not falling", sqd_e))
                if on(k) and not _sq_ok(arr[i] if arr else None, kind, P.get("sqTurnN"))), None)
            if sq_bad:
                miss(b, sq_bad)
                continue                               # rule on: the squeeze condition must hold
            if on("inFall") and not (inr and inr[i] and inr[i]["s"] == "R"):
                miss(b, "daily inertia not falling (red)")
                continue
            if on("inhFall") and not (inh and inh[i] and inh[i]["s"] == "R"):
                miss(b, "hourly inertia not falling (red)")
                continue
            if on("inhGreen") and not (inh and inh[i] and inh[i]["s"] == "G"):
                miss(b, "hourly inertia not green")
                continue                               # rule on: the 3 hourly inertias must all be rising
            if on("inhNotRed") and not (inh and inh[i] and inh[i]["s"] != "R"):
                miss(b, "hourly inertia red")
                continue
            if on("kcdLower") and not (kc[i] and b["c"] <= kc[i][0]):
                miss(b, f"close above the middle of the {'daily' if P.get('kcDaily', True) else 'hourly'} Keltner")
                continue                               # rule on: the close must be in the lower half of the daily channel
            if on("kcNotExt") and not (kc[i] and b["c"] < kc[i][1]):
                miss(b, f"close above the upper {'daily' if P.get('kcDaily', True) else 'hourly'} Keltner band")
                continue                               # rule on: the close must be under the upper Keltner band
            if on("orbAbove") and not (orb[i] and b["c"] > orb[i][0]):
                miss(b, "close under the opening range high")
                continue                               # rule on: the close must be above the opening range high
            if on("noAboveCW") and cw_ and (b["o"] > cw_ or b["c"] > cw_):
                miss(b, "above the call wall")
                continue                               # opening or closing above the call wall: no entry
            if cw_ and cw_room and cw_ - b["c"] < cw_room * a_:
                miss(b, f"only {(cw_ - b['c']) / a_:.1f} ATR under the call wall {cw_:g} (Room to CW ≥ {cw_room:g})")
                continue                               # too close under the call wall: not enough room to the target
            if start and day < start:
                watch = None
                continue
            watch["levelName"] = lname
            s = mk(i, b, w, lvl, lname, a_, watch, day)
            sigs.append(s)
            open_, watch = s, None
            if on("pvEod") and (i + 1 < len(bars) and bars[i + 1]["t"][:10] != day) and b["c"] < lvl:
                s.update(status="eodpw", exitAt=b["t"][:16], exit=b["c"])   # last candle of the day, under the support
                open_ = None
    for s in sigs:
        s["pct"], s["R"] = _result(s)
        s["tgtPct"] = round((s["target"] / s["entry"] - 1) * 100, 2) if s.get("target") else None
        s["riskPct"] = round((s["entry"] - s["stop0"]) / s["entry"] * 100, 2)
        i0 = s.pop("i0")                               # the same entry with the other choice for the daily flip exit
        alt = {k: v for k, v in s.items() if k not in ("stops", "exit", "exitAt", "mfe", "mae", "_dd", "_dn", "riding", "rideAt", "tgtHit", "dlog", "passed", "lostLevel", "scaled", "fvgZone")}
        if alt.get("maxDays"):
            alt["_dd"], alt["_dn"] = alt["at"][:10], 0
        alt.update(status="open", stop=s["stop0"] if on("stopOn") else 0.0, last=s["entry"], lastAt=s["at"])
        for j in range(i0 + 1, len(bars)):
            _step(alt, bars[j], walls, k_trail, not flip_exit, is_last=(j + 1 == len(bars) or bars[j + 1]["t"][:10] != bars[j]["t"][:10]), vw=vwap[j], em=ema[j],
                  kcu=kc[j][1] if kc[j] else None, dpl=dplv.get(bars[j]["t"][:10]), bear=bear[j], reg=inr[j] if inr else None, hreg=inh[j] if inh else None,
                  sqh=sqh[j] if sqh else None, sqd=sqd_x[j] if sqd_x else None, cv=(cvr[j], cvr[j - 1] if j else None) if cvr else None,
                  fx=bool(fxx and fxx[j]))
            if alt["status"] != "open":
                break
        ap, ar = _result(alt)
        s["alt"] = {"flipExit": not flip_exit, "status": alt["status"], "pct": ap, "R": ar}
    if watch:
        watch.pop("_d", None)
    return sigs, watch


def daily_bars(bars):
    """Hourly candles -> daily candles (t = the day's last hourly candle, "full" = the day reached its 15:30 candle)."""
    out = []
    for b in bars:
        d = b["t"][:10]
        if out and out[-1]["t"][:10] == d:
            x = out[-1]
            x.update(t=b["t"][:16], h=max(x["h"], b["h"]), l=min(x["l"], b["l"]), c=b["c"], v=(x["v"] or 0) + (b.get("v") or 0))
        else:
            out.append({"t": b["t"][:16], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v") or 0})
        out[-1]["full"] = b["t"][11:16] >= "15:30"
    return out


def detect_daily(hbars, walls, start=None, cfg=None, earnings=None, alerts=None, dpl=None):
    """The lab strategy on DAILY candles (built from the hourly ones). Entry at the close of a finished day that passes
    the rules that are on: pwTest (low within the zone of the put wall, close above it), inGreen / inNotRed (daily
    inertia), fvgOn (an open bullish daily FVG at or under the close: formed before, not filled, not expired),
    greenOnly (green day), aboveFlip, emaOn (daily EMA), kcdLower / kcNotExt (daily Keltner), pwNeCw, noAboveCW,
    cwRoom, earnings. Stop = put wall (or the day's low without pwTest) - stopAtr x daily ATR. Exits step on daily
    candles with the same switches (stop, trailing, call wall target, scale, inertia yellow / red, bearish FVG,
    call wall lost, Keltner ride, flip, EMA)."""
    P = {**PARAMS, **(cfg or {})}
    on = lambda k: bool(P.get(k, PARAMS[k]))
    bars = daily_bars(hbars)
    if not bars:
        return []
    k_stop, k_trail, buf = float(P["stopAtr"]), float(P["trailAtr"]) if on("trailOn") else 0.0, float(P.get("tgtBuf") or 0)
    zone_atr = float(P.get("retestZone") if P.get("retestZone") is not None else 0.25)
    cw_room = float(P.get("cwRoom") if P.get("cwRoom") is not None else 1.0) if P.get("cwRoomOn", True) else 0.0
    flip_exit = bool(P.get("flipExit", True))
    earn_days = int(P.get("earnDays") if P.get("earnDays") is not None else 10)
    try:
        earn_d = datetime.strptime(str(earnings)[:10], "%Y-%m-%d") if (earnings and on("earnOn")) else None
    except ValueError:
        earn_d = None
    atr = atr14(bars)
    ema = _ema_series([b["c"] for b in bars], int(P.get("emaLen") or 20))
    kc = keltner(bars, int(P.get("kcLen") or 21), float(P.get("kcMult") or 3))
    inr = inertia_regime(bars, (int(P.get("inN1") or 70), int(P.get("inN2") or 40), int(P.get("inN3") or 20)))
    inh = [None] * len(bars)                           # hourly inertia at each day's last hourly candle (its close)
    if any(on(k) for k in ("inhGreen", "inhNotRed", "inhYExit", "inhFall", "inhRiseX", "inhRedX")):
        ih_ = inertia_hourly(hbars, (int(P.get("inhN1") or 70), int(P.get("inhN2") or 40), int(P.get("inhN3") or 20)))
        at_ = {b["t"][:16]: x for b, x in zip(hbars, ih_)}
        inh = [at_.get(b["t"]) for b in bars]
    fire_n = int(P.get("sqFireN") or 3)
    sqd = squeeze(bars, int(P.get("sqdLen") or 20), float(P.get("sqdBB") or 2), float(P.get("sqdKC") or 1.5), fire_n)
    sqh = [None] * len(bars)                           # hourly squeeze at each day's last hourly candle
    if any(on(k) for k in ("sqhOn", "sqhFire", "sqhMom", "sqhExit", "sqhTurn", "sqhRise", "sqhFall", "sqhRiseX")):
        hs_ = squeeze(hbars, int(P.get("sqhLen") or 20), float(P.get("sqhBB") or 2), float(P.get("sqhKC") or 1.5), fire_n)
        at_ = {b["t"][:16]: x for b, x in zip(hbars, hs_)}
        sqh = [at_.get(b["t"]) for b in bars]
    zones = fvg_zones(bars, float(P.get("fvgMin") or 0.2), atr, int(P.get("fvgDays") or 10))
    bulls = [z for z in zones if z["type"] == "bull"]
    bear = [False] * len(bars)
    for z in zones:
        if z["type"] == "bear":
            bear[z["i"]] = True
    cvr = cvd_roll(bars, int(P.get("cvdN") or 20)) if any(on(k) for k in ("cvdUp", "cvdDn", "cvdRise", "cvdFall", "cvdRiseX", "cvdFallX")) else None
    fxe, fxx = fx_series(bars, P, walls, False, None, dpl, alerts)
    ver = version(P) + "|1D"
    sigs, open_ = [], None
    for i, b in enumerate(bars):
        day = b["t"][:10]
        if open_:
            _step(open_, b, walls, k_trail, flip_exit, True, em=ema[i] if on("emaExit") else None,
                  kcu=kc[i][1] if kc[i] else None, bear=bear[i] and on("fvgExit"), reg=inr[i], hreg=inh[i],
                  sqh=sqh[i], sqd=sqd[i], cv=(cvr[i], cvr[i - 1] if i else None) if cvr else None, fx=bool(fxx and fxx[i]))
            if open_["status"] != "open":
                open_ = None
            continue
        if not b["full"] or (start and day < start):
            continue
        w = walls.get(day) or {}
        pw = float(w["putWall"]) if w.get("putWall") else None
        cw = float(w["callWall"]) if w.get("callWall") else None
        fl = float(w["flip"]) if w.get("flip") else None
        a_ = atr[i] or 0
        if not a_:
            continue
        if on("pwTest") and not (pw and b["l"] <= pw + max(pw * RETEST_NEAR / 100, zone_atr * a_) and b["c"] > pw):
            continue
        if on("greenOnly") and not b["c"] > b["o"]:
            continue
        if on("inGreen") and not (inr[i] and inr[i]["s"] == "G"):
            continue
        if on("inNotRed") and not (inr[i] and inr[i]["s"] != "R"):
            continue
        if on("inFall") and not (inr[i] and inr[i]["s"] == "R"):
            continue
        if on("inhFall") and not (inh[i] and inh[i]["s"] == "R"):
            continue
        if on("inhGreen") and not (inh[i] and inh[i]["s"] == "G"):
            continue
        if on("inhNotRed") and not (inh[i] and inh[i]["s"] != "R"):
            continue
        if any(on(k) and not _sq_ok(arr[i], kind, P.get("sqTurnN")) for k, kind, arr in (
                ("sqhOn", "on", sqh), ("sqhFire", "fire", sqh), ("sqhMom", "mom", sqh),
                ("sqdOn", "on", sqd), ("sqdFire", "fire", sqd), ("sqdMom", "mom", sqd),
                ("sqhTurn", "turn", sqh), ("sqdTurn", "turn", sqd), ("sqhRise", "rise", sqh), ("sqhFall", "fall", sqh),
                ("sqdRise", "rise", sqd), ("sqdFall", "fall", sqd))):
            continue
        fz = None
        if on("fvgOn"):
            fz = next((z for z in reversed(bulls) if z["i"] < i and (z["fillI"] is None or z["fillI"] > i) and z["lo"] <= b["c"]), None)
            if not fz:
                continue
        if P.get("aboveFlip") and not (fl and b["c"] > fl):
            continue
        if on("abovePW") and not (pw and b["c"] > pw):
            continue
        if on("belowPW") and not (pw and b["c"] < pw):
            continue
        if cvr is not None and not cvd_ok(P, cvr[i], None, cvr[i - 1] if i else None):
            continue
        if fxe is not None and not fxe[i]:
            continue
        if on("emaOn") and not (ema[i] is not None and b["c"] > ema[i]):
            continue
        if on("kcdLower") and not (kc[i] and b["c"] <= kc[i][0]):
            continue
        if on("kcNotExt") and not (kc[i] and b["c"] < kc[i][1]):
            continue
        if on("pwNeCw") and pw and cw and abs(pw - cw) < 1e-6:
            continue
        if on("noAboveCW") and cw and (b["o"] > cw or b["c"] > cw):
            continue
        if cw and cw_room and cw - b["c"] < cw_room * a_:
            continue
        if earn_d and earn_days and 0 <= (earn_d - datetime.strptime(day, "%Y-%m-%d")).days <= earn_days:
            continue
        lvl = pw if (on("pwTest") and pw) else b["l"]
        st = round(lvl - k_stop * a_, 4)
        tgt = round(cw * (1 - buf / 100), 4) if on("targetOn") and cw and cw * (1 - buf / 100) > b["c"] * 1.002 else None
        s = {"at": b["t"], "kind": "retest", "tf": "1D", "level": lvl, "levelName": "put wall" if on("pwTest") and pw else "day low",
             "armedAt": None, "armPremium": 0, "entry": b["c"], "pw": pw, "flip": fl, "cw": cw, "target": tgt, "atr": round(a_, 4),
             "stop": st if on("stopOn") else 0.0, "stop0": st, "v": ver, "o": b["o"], "h": b["h"], "l": b["l"], "status": "open",
             "last": b["c"], "lastAt": b["t"], "aboveFlipAtEntry": bool(fl and b["c"] > fl), "flipChg": _flip_chg(walls, day),
             "ema": round(ema[i], 4) if ema[i] is not None else None, "emaExit": on("emaExit"), "kcRide": on("kcRide"),
             "cwLost": on("cwLost"), "fvgExit": on("fvgExit"), "pwExit": on("pwExit"), **{k_: on(k_) for k_ in RISE_FALL_EXITS}, "fxExit": bool(P.get("fxExit")), "scaleOn": on("scaleOn"), "scaleR": float(P.get("scaleR") or 1),
             "scalePct": float(P.get("scalePct") or 50), "scaleBE": on("scaleBE"), "inRedExit": on("inRedExit"),
             "inYExit": on("inYExit"), "inhYExit": on("inhYExit"), "inhSt": (inh[i] or {}).get("s"),
             "sqhExit": on("sqhExit"), "sqdExit": on("sqdExit"),
             "inSt": (inr[i] or {}).get("s"), "kc": list(kc[i]) if kc[i] else None}
        if fz:
            s["fvgZone"] = {"t": fz["t"], "lo": fz["lo"], "hi": fz["hi"]}
        if P.get("fixR"):
            s["fixT"] = round(b["c"] + float(P["fixR"]) * (b["c"] - st), 4)
        if P.get("maxDays"):
            s["maxDays"], s["_dd"], s["_dn"] = int(P["maxDays"]), day, 0
        sigs.append(s)
        open_ = s
    for s in sigs:
        s["pct"], s["R"] = _result(s)
        s["tgtPct"] = round((s["target"] / s["entry"] - 1) * 100, 2) if s.get("target") else None
        s["riskPct"] = round((s["entry"] - s["stop0"]) / s["entry"] * 100, 2)
    return sigs


def pv_long_trades(bars, walls, events, cfg=None, datr=None, vwap=None, ema=None, kc=None, dpl=None, start=None, earnings=None):
    """LONG signals of pw_vwap_events traded with the strategy's exits: entry at the close of the reset candle, stop
    stopAtr x ATR under the put wall, target the call wall (minus the buffer), and the same switches (Keltner ride,
    close under the flip, back under CW / dark pool, VWAP / EMA exits, trailing). Plus: the entry day must close above
    the put wall, else the trade is sold at that close (status "eodpw"). One trade at a time per ticker.
    vwap / ema / kc: lists aligned with bars. dpl: {date: dark pool levels}. Returns trades with pct / R."""
    P = {**PARAMS, **(cfg or {})}
    on = lambda k: bool(P.get(k, PARAMS[k]))
    k_stop, buf = float(P["stopAtr"]), float(P.get("tgtBuf") or 0)
    k_trail = float(P["trailAtr"]) if on("trailOn") else 0.0
    flip_exit = bool(P.get("flipExit", True))
    atr = atr14(bars)
    kc = kc or keltner(bars, int(P.get("kcLen") or 21), float(P.get("kcMult") or 3), bool(P.get("kcDaily", True)))
    vwap = vwap or anchored_vwap(bars, int(P.get("vwapDays") or VWAP_DAYS))[0]
    ema = ema or _ema_series([b["c"] for b in bars], int(P.get("emaLen") or 20))
    dpl = dpl or {}
    earn_days = int(P.get("earnDays") if P.get("earnDays") is not None else 10) if on("earnOn") else 0
    try:
        earn_d = datetime.strptime(str(earnings)[:10], "%Y-%m-%d") if earnings and earn_days else None
    except ValueError:
        earn_d = None
    fvg_t = fvg_bull_touch(bars, fvg_zones(bars, float(P.get("fvgMin") or 0.2), atr, int(P.get("fvgDays") or 10)),
                           float(P.get("fvgNear") if P.get("fvgNear") is not None else 0.25), atr) if on("fvgOn") else None
    bear = fvg_bear_new(bars, float(P.get("fvgMin") or 0.2), atr) if on("fvgExit") else [False] * len(bars)
    inr = inertia_regime(bars, (int(P.get("inN1") or 70), int(P.get("inN2") or 40), int(P.get("inN3") or 20))) if on("inRedExit") else None
    out, busy = [], -1
    for e in events:
        i = e["i"]
        if e.get("side") != "long" or i <= busy or (start and e["t"][:10] < start):
            continue
        b, day = bars[i], bars[i]["t"][:10]
        w = walls.get(day) or {}
        a_ = (_atr_on(datr, day) if datr else None) or atr[i]
        if not a_:
            continue
        cw = float(w["callWall"]) if w.get("callWall") else None
        st = round(e["pw"] - k_stop * a_, 4)
        if st >= b["c"]:
            continue                                   # closed under the stop already
        tgt = round(cw * (1 - buf / 100), 4) if on("targetOn") and cw and cw * (1 - buf / 100) > b["c"] * 1.002 else None
        if earn_d and earn_days and 0 <= (earn_d - datetime.strptime(day, "%Y-%m-%d")).days <= earn_days:
            continue                                   # earnings within the next N days: no entry
        if fvg_t is not None and not fvg_t[i]:
            continue                                   # FVG rule on: the reset candle must trade into an open bullish gap
        s = {"at": b["t"][:16], "kind": "retest", "setup": "pvlong", "level": e["pw"], "levelName": "put wall · VWAP reset",
             "armedAt": None, "armPremium": 0, "entry": b["c"], "pw": e["pw"], "cw": cw, "target": tgt, "atr": round(a_, 4),
             "flip": float(w["flip"]) if w.get("flip") else None, "flipChg": _flip_chg(walls, day), "v": version(P),
             "o": b["o"], "h": b["h"], "l": b["l"],
             "stop": st if on("stopOn") else 0.0, "stop0": st, "status": "open", "last": b["c"], "lastAt": b["t"][:16],
             "aboveFlipAtEntry": bool(w.get("flip") and b["c"] > float(w["flip"])),
             "vwap": vwap[i], "ema": round(ema[i], 4) if ema[i] is not None else None,
             "vwapExit": on("vwapExit"), "emaExit": on("emaExit"), "kcRide": on("kcRide"), "cwLost": on("cwLost"), "dpLost": on("dpLost"),
             "pvEod": on("pvEod"), "fvgExit": on("fvgExit"), "pwExit": on("pwExit"),
             "scaleOn": on("scaleOn"), "scaleR": float(P.get("scaleR") or 1), "scalePct": float(P.get("scalePct") or 50), "scaleBE": on("scaleBE"), "kc": list(kc[i]) if kc[i] else None,
             "kcPos": e.get("kcPos"), "above": e.get("above"), "green": e.get("green")}
        if fvg_t is not None and fvg_t[i]:
            s["fvgZone"] = {"t": fvg_t[i]["t"], "lo": fvg_t[i]["lo"], "hi": fvg_t[i]["hi"]}
        j = i
        last_of_day = lambda k: k + 1 == len(bars) or bars[k + 1]["t"][:10] != bars[k]["t"][:10]
        if on("pvEod") and last_of_day(i) and i + 1 < len(bars) and b["c"] < e["pw"]:
            s.update(status="eodpw", exitAt=b["t"][:16], exit=b["c"])   # entry candle is the last of the day, closed under the PW
        for j in range(i + 1, len(bars)):
            if s["status"] != "open":
                break
            _step(s, bars[j], walls, k_trail, flip_exit, is_last=last_of_day(j),
                  vw=vwap[j], em=ema[j], kcu=kc[j][1] if kc[j] else None, dpl=dpl.get(bars[j]["t"][:10]), bear=bear[j], reg=inr[j] if inr else None)
            if s["status"] != "open":
                break
            if on("pvEod") and bars[j]["t"][:10] == day and last_of_day(j) and bars[j]["c"] < e["pw"]:
                s.update(status="eodpw", exitAt=bars[j]["t"][:16], exit=bars[j]["c"])   # entry day closed under the put wall: out
                break
        busy = j if s["status"] != "open" else len(bars)
        s["pct"], s["R"] = _result(s)
        s["tgtPct"] = round((s["target"] / s["entry"] - 1) * 100, 2) if s.get("target") else None
        s["riskPct"] = round((s["entry"] - s["stop0"]) / s["entry"] * 100, 2)
        s["days"] = len({x["t"][:10] for x in bars[i:j + 1]}) - 1
        out.append(s)
    return out


def detect_pvlong(bars, walls, start=None, cfg=None, datr=None, earnings=None, vwap_map=None, ema_map=None, extra=None):
    """The VWAP reset on the put wall strategy, in the flow retest's signal format (kind "retest", setup "pvlong").
    extra["vwResets"]: timestamps of the VWAP reset candles computed on a longer history (else from these bars)."""
    P = {**PARAMS, **(cfg or {})}
    extra = extra or {}
    kc = [extra["kc"].get(b["t"][:16]) for b in bars] if extra.get("kc") else keltner(bars, int(P.get("kcLen") or 21), float(P.get("kcMult") or 3), bool(P.get("kcDaily", True)))
    kch = [extra["kch"].get(b["t"][:16]) for b in bars] if extra.get("kch") else keltner(bars, int(P.get("kchLen") or 21), float(P.get("kchMult") or 3))
    vwap = [vwap_map.get(b["t"][:16]) for b in bars] if vwap_map else None
    ema = [ema_map.get(b["t"][:16]) for b in bars] if ema_map else None
    if extra.get("vwResets") is not None:
        rs = set(extra["vwResets"])
        resets = [i for i, b in enumerate(bars) if b["t"][:16] in rs]
    else:
        resets = anchored_vwap(bars, int(P.get("vwapDays") or VWAP_DAYS))[1]
    ev = pw_vwap_events(bars, walls, resets, kc=kch)    # lower / upper half: hourly channel (entry fine tuning)
    return pv_long_trades(bars, walls, ev, P, datr, vwap=vwap, ema=ema, kc=kc, dpl=extra.get("dpLevels"), start=start, earnings=earnings)


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
    start = (now - timedelta(days=int(days * 1.5) + 7 + 120)).replace(hour=0, minute=0, second=0, microsecond=0)  # + VWAP / daily Keltner / inertia warm-up
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
            vw_all, vres_full = anchored_vwap(bars, int(cfg.get("vwapDays") or VWAP_DAYS))
            vmap = {b["t"][:16]: v for b, v in zip(bars, vw_all)}
            emap = {b["t"][:16]: v for b, v in zip(bars, _ema_series([b["c"] for b in bars], int(cfg.get("emaLen") or 20)))}
            kmap = {b["t"][:16]: v for b, v in zip(bars, keltner(bars, int(cfg.get("kcLen") or 21), float(cfg.get("kcMult") or 3), bool(cfg.get("kcDaily", True))))}
            khmap = {b["t"][:16]: v for b, v in zip(bars, keltner(bars, int(cfg.get("kchLen") or 21), float(cfg.get("kchMult") or 3)))}
            spy = spy_trend(start, now) if cfg.get("spyUp") else {}      # only fetched when the rule is used
            skp = {}
            if cfg.get("skewOn"):
                try:
                    import backtest
                    import flowdata
                    sk = backtest.fetch_skew(flowdata._key(sub), sym, 250, max_age_s=20 * 3600)
                    skp = backtest.skew_pct_map(sk, sorted({b["t"][:10] for b in rb}))
                except Exception as e:
                    print("skew failed", sym, e)
            misses = []
            dpm = {}
            if cfg.get("dpLost"):
                try:
                    import backtest
                    pr = backtest.stored_darkpool(sym) or darkpool(sub, sym, None, 66).get("prints") or []
                    dpm = dp_level_map(pr, sorted({b["t"][:10] for b in rb}))
                except Exception as e:
                    print("dark pool levels failed", sym, e)
            rs, watch = detect_retest(rb, walls, first, cfg, datr, ticker_alerts(sub, sym, max_age_h=1), earn, vmap, emap,
                                      {"kc": kmap, "kch": khmap, "spy": spy, "skewPct": skp, "misses": misses, "dpLevels": dpm,
                                       "vwResets": [bars[i]["t"][:16] for i in vres_full if i > 0],
                                       "hvc": {b["t"][:16]: x[0] for b, x in zip(bars, hvc_series(bars, vres_full)) if x}})
            try:
                db.put({"PK": db.upk(sub), "SK": f"WWMISS#{sym}", "misses": misses[-400:], "v": version(cfg), "at": iso(now_ny())})
            except Exception as e:
                print("misses not stored", sym, e)
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
    cur = db.get(db.upk(sub), "WWCFG") or {"PK": db.upk(sub), "SK": "WWCFG"}
    cut = iso(now_ny() - timedelta(days=7))
    lists = [dict(x) for x in (cur.get("lists") or [])]
    fl = next((x for x in lists if x.get("name") == FLOW_LIST), None)
    if not fl:
        fl = {"name": FLOW_LIST, "tickers": []}
        lists.insert(0, fl)
    fl["tickers"] = list(fl.get("tickers") or [])
    other = set(cfg["tickers"]) - set(fl["tickers"])   # in Main or another list: left there
    new = [t for t in sorted(prem, key=lambda t: -prem[t]) if t not in cfg["tickers"] and (cur.get("removed") or {}).get(t, "") < cut]
    added = dict(cur.get("added") or {})
    changed = False
    for t in sorted(prem, key=lambda t: -prem[t]):     # flow again on a ticker of the list: it moves to today
        if t in fl["tickers"] and t not in other and (added.get(t) or {}).get("at", "")[:10] != iso(now_ny())[:10]:
            added[t] = {"at": iso(now_ny()), "by": "flow", "premium": round(prem[t])}
            changed = True
    for t in new[:20]:                                 # into the "Unusual flow" list, not the main one
        if t not in fl["tickers"] and len(cfg["tickers"]) + len(fl["tickers"]) < 400:
            fl["tickers"].append(t)
            added[t] = {"at": iso(now_ny()), "by": "flow", "premium": round(prem[t])}
            changed = True
    dropped = prune_flow(sub, fl, added)               # only today, yesterday and the day before are kept
    if changed or dropped:
        db.update(db.upk(sub), "WWCFG", {"lists": lists, "added": added, "listsV": 1, **({} if cur.get("tickers") else {"tickers": list(cfg["main"])})})
    return {"alerts": len(res.get("alerts") or []), "added": new[:20], "dropped": dropped}


FLOW_DAYS = 3


def flow_days(fl, added, today=None):
    """The days the Unusual flow list keeps: today (on a weekday) and the latest days a ticker came in from the
    flow, newest first, FLOW_DAYS of them."""
    today = today or iso(now_ny())[:10]
    ds = {(added.get(t) or {}).get("at", "")[:10] for t in fl.get("tickers") or []} - {""}
    if datetime.strptime(today, "%Y-%m-%d").weekday() < 5:
        ds.add(today)
    return sorted(ds, reverse=True)[:FLOW_DAYS]


def prune_flow(sub, fl, added, today=None):
    """Drops from the Unusual flow list the tickers whose last flow is older than the kept days (a ticker with an
    open wall watch position or an open signal stays). -> the dropped tickers."""
    keep_days = set(flow_days(fl, added, today))
    if not keep_days:
        return []
    old = [t for t in fl.get("tickers") or [] if (added.get(t) or {}).get("at", "")[:10] not in keep_days]
    if not old:                                        # (positions are only looked up when something would go)
        return []
    held = set()
    try:
        held = {r.get("symbol") for r in db.q_prefix(db.upk(sub), "BOT#") if r.get("status") in ("submitted", "open", "closing")}
        held |= {s_.get("symbol") for s_ in db.q_prefix(db.upk(sub), "WWSIG#") if s_.get("status") == "open"}
    except Exception as e:
        print("flow prune: positions failed", e)
    out, kept = [], []
    for t in fl.get("tickers") or []:
        d = (added.get(t) or {}).get("at", "")[:10]
        if d in keep_days or t in held:
            kept.append(t)
        else:
            out.append(t)
    if out:
        fl["tickers"] = kept
        for t in out:
            if (added.get(t) or {}).get("by") == "flow":
                added.pop(t, None)
    return out


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
              "kcRide": bool(cfg.get("kcRide")), "kcLen": int(cfg.get("kcLen") or 21), "kcMult": float(cfg.get("kcMult") or 3),
              "kcDaily": bool(cfg.get("kcDaily", True)),
              "cwLost": bool(cfg.get("cwLost")), "dpLost": bool(cfg.get("dpLost")), "pwExit": bool(cfg.get("pwExit")),
              **{k_: bool(cfg.get(k_)) for k_ in RISE_FALL_EXITS}, "cvdN": int(cfg.get("cvdN") or 20), "tf": cfg.get("tf") or "1h",
              "vwapDays": int(cfg.get("vwapDays") or VWAP_DAYS),
              "stopOn": bool(cfg.get("stopOn", True)),
              "target": sg.get("target"), "level": sg.get("level"), "entry": sg["entry"], "stopAtr": float(cfg["stopAtr"]),
              "flipExit": bool(cfg.get("flipExit", True)), "aboveFlipAtEntry": bool(sg.get("aboveFlipAtEntry")),
              "pvEodPw": sg.get("level") if sg.get("pvEod") else None,
              "scaleOn": bool(cfg.get("scaleOn")), "scaleR": float(cfg.get("scaleR") or 1), "scalePct": float(cfg.get("scalePct") or 50),
              "scaleBE": bool(cfg.get("scaleBE", True)),
              "fvgExit": bool(cfg.get("fvgExit")), "fvgMin": float(cfg.get("fvgMin") or 0.2),
              "earnExit": bool(cfg.get("earnExit", True)),
              "inYExit": bool(cfg.get("inYExit")), "inhYExit": bool(cfg.get("inhYExit")),
              "inhNs": [int(cfg.get("inhN1") or 70), int(cfg.get("inhN2") or 40), int(cfg.get("inhN3") or 20)],
              "sqhExit": bool(cfg.get("sqhExit")), "sqdExit": bool(cfg.get("sqdExit")), "sqFireN": int(cfg.get("sqFireN") or 3),
              "sqh": [int(cfg.get("sqhLen") or 20), float(cfg.get("sqhBB") or 2), float(cfg.get("sqhKC") or 1.5)],
              "sqd": [int(cfg.get("sqdLen") or 20), float(cfg.get("sqdBB") or 2), float(cfg.get("sqdKC") or 1.5)],
              "inRedExit": bool(cfg.get("inRedExit")), "inNs": [int(cfg.get("inN1") or 70), int(cfg.get("inN2") or 40), int(cfg.get("inN3") or 20)]}
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


def review_open(sub, auto_close=False):
    """Open wall watch positions checked against the CURRENT settings: would today's entry rules have taken the trade,
    and would today's exits have closed it already (its entry replayed with the current exits)?"""
    import autotrader
    import backtest
    from concurrent.futures import ThreadPoolExecutor
    cfg = config(sub)
    try:
        bcfg = autotrader.settings(sub)
    except Exception:
        bcfg = {}
    recs = [r for r in db.q_prefix(db.upk(sub), "BOT#") if r.get("status") in ("submitted", "open", "closing")
            and (r.get("proposal") or {}).get("wallwatch")]
    now = now_ny().replace(tzinfo=None)

    def one(r):
        p = r["proposal"]
        ww, sym = p["wallwatch"], r["symbol"]
        at = str(ww.get("signalAt") or "")[:16]
        row = {"id": r.get("id"), "symbol": sym, "contract": p.get("contract"), "plPct": r.get("lastPlPct"), "at": at,
               "status": r.get("status")}
        try:
            days = max(10, (now - datetime.strptime(at[:10], "%Y-%m-%d")).days) + 70
            d = backtest._ticker_data(sub, sym, days, cfg, False)
            if not d:
                return {**row, "verdict": "unknown", "why": ["no market data"]}
            misses = []
            sigs, _ = detect_retest(d["bars"], d["walls"], at[:10], cfg, d["datr"], d["alerts"], None, d["vmap"], d["emap"],
                                    {**d["extra"], "misses": misses})
            entry_ok = any(x["at"] == at for x in sigs)
            why = [m["r"] for m in misses if m["t"][:16] == at] or [m["r"] for m in misses if m["t"][:10] == at[:10]][:2]
            if not entry_ok and not why:
                why = ["not a signal with the current entry rules"]
            fs, _ = detect_retest(d["bars"], d["walls"], None, cfg, d["datr"], d["alerts"], None, d["vmap"], d["emap"],
                                  {**d["extra"], "force": {"at": at, "level": ww.get("level"), "levelName": "support"}})
            f = fs[0] if fs else None
            ex = {"status": f["status"], "at": f.get("exitAt"), "px": f.get("exit"), "pct": f.get("pct")} if f and f["status"] != "open" else None
            earn = None
            if cfg.get("earnExit", True) and bcfg:
                due, why_e = autotrader.earnings_exit_due(autotrader.earn_for(bcfg, sym)[0])
                earn = why_e if due else None
            verdict = "exit" if (ex or earn) else "keep" if entry_ok else "no entry"
            return {**row, "verdict": verdict, "entryOk": entry_ok, "why": [] if entry_ok else why[:3], "exitNow": ex, "earnings": earn,
                    "stopNow": f.get("stop") if f else None, "targetNow": f.get("target") if f else None,
                    "last": f.get("last") if f else None}
        except Exception as e:
            return {**row, "verdict": "unknown", "why": [str(e)[:120]]}

    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(one, recs))
    rows.sort(key=lambda x: {"exit": 0, "no entry": 1, "unknown": 2, "keep": 3}.get(x["verdict"], 4))
    if auto_close:                                     # positions that don't fit the current settings: sold at market
        for x in rows:
            if x["verdict"] not in ("exit", "no entry"):
                continue
            why = (f"current settings: {x['exitNow']['status']} exit {(x['exitNow'].get('at') or '').replace('T', ' ')}" if x.get("exitNow")
                   else x.get("earnings") or "current settings: " + "; ".join(x.get("why") or ["would not enter"]))
            try:
                autotrader.close(sub, x["id"], why[:200])
                x["closed"] = True
            except Exception as e:
                x["closeError"] = str(e)[:120]
    return {"at": iso(now_ny()), "rows": rows, "autoClose": bool(auto_close)}


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


def fill_walls(sub, days=45, budget_s=480, ticker=None, mp=True, sk=None, tickers=None):
    """Fetch the walls and the flip of every day for every watchlist ticker (or one ticker), as many as the time allows."""
    import flowdata
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    pk = db.upk(sub)
    cfg = config(sub)
    key = flowdata._key(sub)
    sk = sk or (f"WWFILL#{ticker}" if ticker else "WWFILL")
    todo, total = walls_todo(list(tickers) if tickers else [ticker] if ticker else cfg["tickers"], days, need_mp=mp)
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
    done = [s for s in sigs if s.get("status") not in (None, "open")]
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
    done = [s for s in sigs if s.get("status") not in (None, "open")]
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


def chart(sub, sym, days=45, flow=True, lab=None):
    """Hourly chart of a watchlist ticker: candles, walls + flip by day, its signals, and the call / put flow.
    lab: settings to try (not saved): every signal is replayed with the live settings + these, none read from the store."""
    import flowdata
    from charts import _yahoo
    sym = (sym or "").upper().replace("/", ".")
    if not sym:
        from util import BadRequest
        raise BadRequest("Pick a ticker.")
    now = now_ny().replace(tzinfo=None)
    start = (now - timedelta(days=int(days * 1.5) + 120)).replace(hour=0, minute=0, second=0, microsecond=0)   # + VWAP / daily Keltner / inertia warm-up
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
    if lab:
        ccfg = {**ccfg, **{k: v for k, v in lab.items() if k in PARAMS and k != "pvSetup"}}
        sigs = []
    vw, vres = anchored_vwap(bars_all, int(ccfg.get("vwapDays") or VWAP_DAYS))
    off = len(bars_all) - len(bars)
    em = _ema_series([b["c"] for b in bars_all], int(ccfg.get("emaLen") or 20))[off:]
    kcs = keltner(bars_all, int(ccfg.get("kcLen") or 21), float(ccfg.get("kcMult") or 3), bool(ccfg.get("kcDaily", True)))[off:]
    kc = [{"t": b["t"], "m": k[0], "u": k[1], "l": k[2]} for b, k in zip(bars, kcs) if k]
    kchs = keltner(bars_all, int(ccfg.get("kchLen") or 21), float(ccfg.get("kchMult") or 3))[off:]
    kch = [{"t": b["t"], "m": k[0], "u": k[1], "l": k[2]} for b, k in zip(bars, kchs) if k]
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
    replay_misses = []
    long_bars = None
    cut = "9999" if lab else dates[-45] if len(dates) > 45 else None   # lab: everything replayed
    if cut:                                            # longer view: the signals older than the stored 45 days, replayed
        try:
            import backtest
            data = backtest._ticker_data(sub, sym, len(dates) + (120 if ccfg.get("tf") == "1D" else 5), ccfg, False,
                                            alerts=bool(flow or (ccfg.get("alertReq", True) and ccfg.get("tf") != "1D")))   # 1D: daily inertia warm-up
            if data:
                long_bars = data["bars"]
                extra = {**data["extra"], "misses": replay_misses}
                old, _ = detect_retest(data["bars"], data["walls"], dates[0], ccfg, data["datr"], data["alerts"], None,
                                       data["vmap"], data["emap"], extra)
                for x in old:
                    x.pop("i0", None)
                    x["replay"] = True
                sigs = [x for x in old if x["at"][:10] < cut] + [x for x in sigs if x["at"][:10] >= cut]
                replay_misses = [m for m in replay_misses if m["t"][:10] < cut]
                if flow:                               # calls older than the live window, from the stored history
                    import autotrader
                    big = autotrader.settings(sub)["flowMinPremium"]
                    seen = {a["t"] for a in alerts}
                    alerts = [{"t": a["t"], "premium": round(a["p"]), "type": "call", "contract": None, "big": a["p"] >= big,
                               "sweep": False, "rule": "history"} for a in data["alerts"]
                              if a.get("c") and dates[0] <= a["t"][:10] < (alerts[0]["t"][:10] if alerts else cut) and a["t"] not in seen] + alerts
        except Exception as e:
            note = (note + " · " if note else "") + f"Older signals unavailable: {str(e)[:100]}"
    hv = hvc_series(bars_all, vres)[off:]               # daily high-volume close, set by the VWAP resets
    hvc = [{"t": b["t"], "v": x[0], "u": x[1], "l": x[2]} for b, x in zip(bars, hv) if x]
    vw, vres = vw[off:], [i - off for i in vres if i - off >= 0]
    vwap = [{"t": b["t"], "v": v} for b, v in zip(bars, vw)]
    fill = db.get(db.upk(sub), f"WWFILL#{sym}") or {}
    mrec = db.get(db.upk(sub), f"WWMISS#{sym}") or {}
    misses = replay_misses + [m for m in (mrec.get("misses") or []) if mrec.get("v") == cur_v and m["t"][:10] >= (cut or (dates[0] if dates else ""))]
    try:
        vres_all = anchored_vwap(bars_all, int(ccfg.get("vwapDays") or VWAP_DAYS))[1]
        kc_all = keltner(bars_all, int(ccfg.get("kchLen") or 21), float(ccfg.get("kchMult") or 3))   # halves: hourly
        pv = [{k: e.get(k) for k in ("t", "pw", "cw", "side", "above", "green", "o", "h", "l", "c", "kcPos", "kcHalf", "kcM", "kcU", "kcL")}
              for e in pw_vwap_events(bars_all, stored, vres_all, kc=kc_all)
              if e["side"] == "long" and e["t"][:10] >= (dates[0] if dates else "")]
    except Exception as e:
        print("pw/vwap events failed", sym, e)
        pv = []
    try:
        fz = fvg_zones(daily_bars(long_bars or bars_all) if ccfg.get("tf") == "1D" else (long_bars or bars_all), float(ccfg.get("fvgMin") or 0.2), None, int(ccfg.get("fvgDays") or 10))
        fvg = [{k: z[k] for k in ("t", "type", "lo", "hi", "fillAt", "end")} for z in fz
               if dates and (z["fillAt"] is None or z["fillAt"][:10] >= dates[0])]
    except Exception as e:
        print("fvg failed", sym, e)
        fvg = []
    try:
        ir = inertia_regime(bars_all, (int(ccfg.get("inN1") or 70), int(ccfg.get("inN2") or 40), int(ccfg.get("inN3") or 20)))[off:]
        inertia = [{"t": b["t"], "s": x["s"], "pts": x["pts"]} for b, x in zip(bars, ir) if x]
    except Exception as e:
        print("inertia failed", sym, e)
        inertia = []
    try:
        ih = inertia_hourly(bars_all, (int(ccfg.get("inhN1") or 70), int(ccfg.get("inhN2") or 40), int(ccfg.get("inhN3") or 20)))[off:]
        inertia_h = [{"t": b["t"], "s": x["s"]} for b, x in zip(bars, ih) if x]
    except Exception as e:
        print("hourly inertia failed", sym, e)
        inertia_h = []
    try:
        fn_ = int(ccfg.get("sqFireN") or 3)
        sh = squeeze(bars_all, int(ccfg.get("sqhLen") or 20), float(ccfg.get("sqhBB") or 2), float(ccfg.get("sqhKC") or 1.5), fn_)[off:]
        sqz_h = [{"t": b["t"], "on": x["on"], "f": x["fired"], "m": x["mom"], "u": x["up"]} for b, x in zip(bars, sh) if x]
        dball = daily_bars(bars_all)
        sd = squeeze(dball, int(ccfg.get("sqdLen") or 20), float(ccfg.get("sqdBB") or 2), float(ccfg.get("sqdKC") or 1.5), fn_)
        d0 = dates[0] if dates else ""
        sqz_d = [{"t": b["t"][:10], "on": x["on"], "f": x["fired"], "m": x["mom"], "u": x["up"]} for b, x in zip(dball, sd) if x and b["t"][:10] >= d0]
    except Exception as e:
        print("squeeze failed", sym, e)
        sqz_h, sqz_d = [], []
    return {"sqzH": sqz_h, "sqzD": sqz_d, "inertia": inertia, "inertiaH": inertia_h, "fvg": fvg, "lab": bool(lab), "tf": ccfg.get("tf") or "1h", "pwVwap": pv, "curV": cur_v, "misses": misses, "kc": kc, "kch": kch, "kcDaily": bool(ccfg.get("kcDaily", True)), "orb": orbs, "spyDown": spy_dn, "vwap": vwap, "hvc": hvc, "ema": ema, "emaLen": int(ccfg.get("emaLen") or 20), "staleSignals": len(stale), "vwapResets": [bars[i]["t"] for i in vres if i > 0], "fill": {k: fill.get(k) for k in ("running", "total", "left", "at", "errors")}, "missingDays": len(missing), "noflipDays": len(noflip),
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


DP_LEVEL_DAYS = 180


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
    # the levels always come from the last DP_LEVEL_DAYS calendar days (the same lines whatever the chart shows);
    # the prints drawn are the ones of the chart's period
    span = _weekdays(DP_LEVEL_DAYS * 5 // 7, now_ny().date())
    out = flowdata.darkpool(sub, sym, span, floor, budget_s=22, workers=8)          # fetched with a round $ floor (cached per day)
    allp = [p for p in out.get("prints") or [] if not min_sh or p["size"] >= min_sh]
    first = sorted(_weekdays(days, now_ny().date()))[0] if days else ""
    prints = [p for p in allp if p["t"][:10] >= first]
    buy = sum(p["premium"] for p in prints if p.get("side") == "buy")
    sell = sum(p["premium"] for p in prints if p.get("side") == "sell")
    return {"symbol": sym, "prints": prints, "levels": flowdata.dp_levels(allp, 10), "profile": flowdata.dp_levels(allp, 400),
            "levelDays": DP_LEVEL_DAYS, "missingDays": out.get("missing") or 0,
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
