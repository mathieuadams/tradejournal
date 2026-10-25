"""Wall watch 2: a separate copy of the wall watch idea (the live wall watch is untouched), long AND short.

One engine for daily or hourly candles. Every rule is mirrored: a long rule's short twin is the same test upside down.
  * indicators: 3 EMAs, Keltner channel, TTM squeeze, inertia (on the chart's candles), fair value gaps, CVD, gamma
    walls (call wall / put wall / flip of the day, Unusual Whales, as stored by the wall watch);
  * entry at the close of the candle where every rule that is on agrees (long, short or both sides);
  * exits: stop x ATR (always), target R, trailing x ATR, breakeven, EMA / Keltner middle / inertia / squeeze / opposite
    FVG / time / opposite signal (optionally reversing);
  * no look-ahead: a candle's signal uses only that candle and the ones before; stops / targets are checked from the
    next candle (a stop and a target on the same candle count as the stop).
Backtest of a list (Mag 7 or the wall watch lists) in the background: train = first 60% of the period, test = the rest."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import db
from util import BadRequest, iso, now_ny

CFG_SK = "WW2CFG"
BT_SK = "WW2BT"
TRAIN = 0.6
MAG7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]

PARAMS = {
    "side": "both",                                   # long / short / both
    "tf": "1D",
    # indicators
    "ema1": 9, "ema2": 21, "ema3": 50, "kcLen": 20, "kcMult": 2.0, "sqLen": 20, "sqBB": 2.0, "sqKC": 1.5, "sqFireN": 3,
    "inN1": 70, "inN2": 40, "inN3": 20, "fvgMin": 0.2, "fvgDays": 10, "cvdN": 20,
    # entry rules (each mirrored for shorts)
    "emaStack": True,                                 # EMA1 > EMA2 > EMA3 (short: EMA1 < EMA2 < EMA3)
    "emaCross": False,                                # EMA1 crossed EMA2 on this candle, in the trade's direction
    "priceEma": False, "pEma": 2,                     # close above EMA n (short: under)
    "emaSlope": False, "sEma": 3,                     # EMA n rising (short: falling)
    "kcOn": False, "kcMode": "pull",                  # break: close beyond the band; pull: back to the middle and held; revert: back inside from beyond the opposite band
    "sqFire": False,                                  # the squeeze fired within sqFireN candles, momentum in the direction
    "sqMom": False,                                   # squeeze momentum on the side and moving with it
    "sqOnly": False,                                  # only while a squeeze is on (side-neutral)
    "inOn": False, "inMode": "match",                 # match: inertia green (short: red); notOpp: not red (short: not green)
    "fvgOn": False,                                   # the candle taps an open bullish FVG and closes above its bottom (short: bearish FVG, under its top)
    "candleDir": True,                                # green candle (short: red)
    "wallOn": False, "wallMode": "flip",              # flip: close above the flip (short: under); walls: above the put wall (short: under the call wall)
    "roomOn": False, "roomAtr": 1.0,                  # room to the call wall (short: to the put wall) of at least this x ATR
    "cvdOn": False,                                   # CVD over cvdN candles > 0 (short: < 0)
    # exits
    "stopAtr": 1.5,
    "tgtOn": True, "tgtR": 2.0,
    "trailOn": False, "trailAtr": 2.0,
    "beOn": False, "beR": 1.0,
    "emaExit": False, "xEma": 2,                      # close back across EMA n
    "kcExit": False,                                  # close back across the Keltner middle
    "inExit": False,                                  # inertia turns to the other color
    "sqExit": False,                                  # squeeze momentum turns against the trade
    "fvgExit": False,                                 # an opposite FVG forms
    "timeOn": False, "maxBars": 10,
    "oppExit": True, "reverse": False,                # out on the opposite signal; reverse: and take it
}
BOOL = [k for k, v in PARAMS.items() if isinstance(v, bool)]


def clean(cfg):
    P = dict(PARAMS)
    for k, v in (cfg or {}).items():
        if k not in PARAMS:
            continue
        d = PARAMS[k]
        try:
            P[k] = bool(v) if isinstance(d, bool) else int(float(v)) if isinstance(d, int) else float(v) if isinstance(d, float) else str(v)
        except (TypeError, ValueError):
            pass
    P["side"] = P["side"] if P["side"] in ("long", "short", "both") else "both"
    P["tf"] = P["tf"] if P["tf"] in ("1D", "1h") else "1D"
    for k in ("ema1", "ema2", "ema3", "kcLen", "sqLen", "inN1", "inN2", "inN3", "cvdN"):
        P[k] = max(2, min(400, P[k]))
    for k in ("pEma", "sEma", "xEma"):
        P[k] = max(1, min(3, P[k]))
    P["stopAtr"] = max(0.1, P["stopAtr"])
    return P


# ---------------------------------------------------------------- data
_DATA = {}


def bars_of(sym, tf, days):
    """Finished candles of the last `days` trading days (+ warm-up for the long indicators), cached for 5 minutes."""
    import wallwatch
    from charts import _yahoo
    now = now_ny().replace(tzinfo=None)
    k = (sym, tf, days)
    hit = _DATA.get(k)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    if tf == "1h":
        start = now - timedelta(days=min(720, int(days * 1.45) + 60))
        bars = wallwatch.finished(_yahoo(sym.replace(".", "-"), "1h", start, now + timedelta(days=1)) or [], now)
        bars = [{**b, "t": b["t"][:16]} for b in bars]
    else:
        start = now - timedelta(days=int(days * 1.45) + 160)
        raw = _yahoo(sym.replace(".", "-"), "1d", start, now + timedelta(days=1)) or []
        today = now.strftime("%Y-%m-%d")
        bars = [{**b, "t": b["t"][:10] + "T09:30"} for b in raw if b["t"][:10] < today or now.strftime("%H:%M") >= "16:05"]
    if len(_DATA) > 300:
        _DATA.clear()
    _DATA[k] = (time.time(), bars)
    return bars


def walls_of(sym):
    import flowdata
    try:
        return flowdata.stored_walls(sym)
    except Exception:
        return {}


# ---------------------------------------------------------------- the indicators
def series(bars, P):
    import wallwatch
    cl = [b["c"] for b in bars]
    return {"atr": wallwatch.atr14(bars),
            "ema": [wallwatch._ema_series(cl, int(P[k])) for k in ("ema1", "ema2", "ema3")],
            "kc": wallwatch.keltner(bars, int(P["kcLen"]), float(P["kcMult"])),
            "sq": wallwatch.squeeze(bars, int(P["sqLen"]), float(P["sqBB"]), float(P["sqKC"]), int(P["sqFireN"])),
            "inr": wallwatch.inertia_hourly(bars, (int(P["inN1"]), int(P["inN2"]), int(P["inN3"]))),
            "cvd": wallwatch.cvd_roll(bars, int(P["cvdN"])),
            "fvg": wallwatch.fvg_zones(bars, float(P["fvgMin"]), None, int(P["fvgDays"]))}


def _fvg_tap(bars, zones):
    """Per candle: +1 when it taps an open bullish gap and closes above its bottom, -1 for a bearish one (both: 0)."""
    out = [0] * len(bars)
    for j, b in enumerate(bars):
        bull = bear = False
        for z in zones:
            if z["i"] >= j or (z["fillI"] is not None and z["fillI"] < j):
                continue
            if z["type"] == "bull" and b["l"] <= z["hi"] and b["c"] > z["lo"]:
                bull = True
            elif z["type"] == "bear" and b["h"] >= z["lo"] and b["c"] < z["hi"]:
                bear = True
        out[j] = 1 if bull and not bear else -1 if bear and not bull else 0
    return out


def _new_fvg(bars, zones):
    out = [0] * len(bars)
    for z in zones:
        out[z["i"]] = 1 if z["type"] == "bull" else -1
    return out


def detect(bars, walls, P, start=None):
    """(trades, signals, series) on these candles. trades: [{side, at, px, stop, tgt, exitAt, exitPx, why, R, status}]."""
    P = clean(P)
    n = len(bars)
    if n < 30:
        return [], [], series(bars, P) if n else {}
    S = series(bars, P)
    atr, ema, kc, sq, inr, cvd = S["atr"], S["ema"], S["kc"], S["sq"], S["inr"], S["cvd"]
    tap, newf = _fvg_tap(bars, S["fvg"]), _new_fvg(bars, S["fvg"])
    wall = lambda i: walls.get(bars[i]["t"][:10]) or {}
    sides = (1,) if P["side"] == "long" else (-1,) if P["side"] == "short" else (1, -1)

    def ok(i, d):
        """Every rule that is on, for side d (+1 long / -1 short), at the close of candle i."""
        b, c = bars[i], bars[i]["c"]
        e = [x[i] for x in ema]
        if P["emaStack"] and (None in e or not (d * (e[0] - e[1]) > 0 and d * (e[1] - e[2]) > 0)):
            return False
        if P["emaCross"]:
            if i < 1 or None in (ema[0][i], ema[1][i], ema[0][i - 1], ema[1][i - 1]):
                return False
            if not (d * (ema[0][i] - ema[1][i]) > 0 and d * (ema[0][i - 1] - ema[1][i - 1]) <= 0):
                return False
        if P["priceEma"]:
            v = ema[P["pEma"] - 1][i]
            if v is None or not d * (c - v) > 0:
                return False
        if P["emaSlope"]:
            s_ = ema[P["sEma"] - 1]
            if i < 1 or s_[i] is None or s_[i - 1] is None or not d * (s_[i] - s_[i - 1]) > 0:
                return False
        if P["kcOn"]:
            k_, kp = kc[i], kc[i - 1] if i else None
            if not k_ or not kp:
                return False
            mid, up, lo = k_
            if P["kcMode"] == "break" and not (c > up if d > 0 else c < lo):
                return False
            if P["kcMode"] == "pull" and not ((b["l"] <= mid and c > mid) if d > 0 else (b["h"] >= mid and c < mid)):
                return False
            if P["kcMode"] == "revert" and not ((bars[i - 1]["c"] < kp[2] and c > lo) if d > 0 else (bars[i - 1]["c"] > kp[1] and c < up)):
                return False
        x = sq[i]
        if P["sqFire"] and not (x and x["fired"] and d * x["mom"] > 0):
            return False
        if P["sqMom"] and not (x and d * x["mom"] > 0 and (x["up"] if d > 0 else not x["up"])):
            return False
        if P["sqOnly"] and not (x and x["on"]):
            return False
        if P["inOn"]:
            r = (inr[i] or {}).get("s")
            if not r or (P["inMode"] == "match" and r != ("G" if d > 0 else "R")) or (P["inMode"] != "match" and r == ("R" if d > 0 else "G")):
                return False
        if P["fvgOn"] and tap[i] != d:
            return False
        if P["candleDir"] and not d * (c - b["o"]) > 0:
            return False
        if P["wallOn"] or P["roomOn"]:
            w = wall(i)
            cw, pw, fl = w.get("callWall"), w.get("putWall"), w.get("flip")
            if P["wallOn"]:
                lvl = fl if P["wallMode"] == "flip" else (pw if d > 0 else cw)
                if lvl is None or not d * (c - lvl) > 0:
                    return False
            if P["roomOn"]:
                tw = cw if d > 0 else pw
                if tw is None or not d * (tw - c) >= float(P["roomAtr"]) * (atr[i] or 0):
                    return False
        if P["cvdOn"] and not d * (cvd[i] or 0) > 0:
            return False
        return True

    trades, sigs, pos = [], [], None
    warm = max(int(P["ema3"]), int(P["kcLen"]), int(P["sqLen"]) * 2, int(P["inN1"]) + 1, 20)
    st0 = start or ""

    def close(i, px, why):
        nonlocal pos
        R = pos["d"] * (px - pos["px"]) / pos["risk"]
        pos.update(exitAt=bars[i]["t"], exitPx=round(px, 4), why=why, R=round(R, 3), status="closed", bars=i - pos["i"])
        trades.append(pos)
        pos = None

    for i in range(warm, n):
        b = bars[i]
        only = sides
        if pos:                                        # 1. the open trade on this candle
            d = pos["d"]
            hit_stop = (b["l"] <= pos["stop"]) if d > 0 else (b["h"] >= pos["stop"])
            hit_tgt = pos["tgt"] is not None and ((b["h"] >= pos["tgt"]) if d > 0 else (b["l"] <= pos["tgt"]))
            if hit_stop:
                close(i, min(pos["stop"], b["o"]) if d > 0 else max(pos["stop"], b["o"]), "stop" if not pos["moved"] else "trail")
            elif hit_tgt:
                close(i, max(pos["tgt"], b["o"]) if d > 0 else min(pos["tgt"], b["o"]), "target")
            else:
                c, why = b["c"], None
                xe = ema[P["xEma"] - 1][i]
                if P["emaExit"] and xe is not None and d * (c - xe) < 0:
                    why = f"EMA{P['xEma']}"
                elif P["kcExit"] and kc[i] and d * (c - kc[i][0]) < 0:
                    why = "Keltner middle"
                elif P["inExit"] and (inr[i] or {}).get("s") == ("R" if d > 0 else "G"):
                    why = "inertia"
                elif P["sqExit"] and sq[i] and sq[i - 1] and (not sq[i]["up"] if d > 0 else sq[i]["up"]):
                    why = "squeeze momentum"
                elif P["fvgExit"] and newf[i] == -d:
                    why = "opposite FVG"
                elif P["timeOn"] and i - pos["i"] >= int(P["maxBars"]):
                    why = "time"
                elif P["oppExit"] and b["t"] >= st0 and ok(i, -d):
                    why = "opposite signal"
                if why:
                    opp = why == "opposite signal"
                    close(i, c, why)
                    if opp:                            # reverse: the opposite trade at this close; else none on this candle
                        only = (-d,) if P["reverse"] and -d in sides else ()
                else:                                  # still open: breakeven / trailing for the next candle
                    a = atr[i] or 0
                    if P["beOn"] and not pos["be"] and d * (b["h" if d > 0 else "l"] - pos["px"]) >= float(P["beR"]) * pos["risk"]:
                        pos["be"] = True
                        if d * (pos["px"] - pos["stop"]) > 0:
                            pos["stop"], pos["moved"] = pos["px"], True
                    if P["trailOn"] and a:
                        ts = (b["h"] - float(P["trailAtr"]) * a) if d > 0 else (b["l"] + float(P["trailAtr"]) * a)
                        if d * (ts - pos["stop"]) > 0:
                            pos["stop"], pos["moved"] = round(ts, 4), True
                    continue
        if pos or b["t"] < st0:                        # 2. a new trade at this close
            continue
        for d in only:
            if ok(i, d):
                a = atr[i] or 0
                if a <= 0:
                    break
                px, risk = b["c"], float(P["stopAtr"]) * a
                pos = {"side": "long" if d > 0 else "short", "d": d, "i": i, "at": b["t"], "px": px, "risk": risk,
                       "stop": round(px - d * risk, 4), "tgt": round(px + d * float(P["tgtR"]) * risk, 4) if P["tgtOn"] else None,
                       "stop0": round(px - d * risk, 4), "moved": False, "be": False}
                sigs.append({"t": b["t"], "side": pos["side"]})
                break
    if pos:                                            # still open: marked at the last close
        last = bars[-1]
        pos.update(status="open", R=round(pos["d"] * (last["c"] - pos["px"]) / pos["risk"], 3), exitAt=None, exitPx=None, why=None,
                   bars=n - 1 - pos["i"])
        trades.append(pos)
    for t in trades:
        t.pop("d", None)
        t.pop("i", None)
        t["risk"] = round(t["risk"], 4)
    return trades, sigs, S


def stats(trades):
    done = [t for t in trades if t.get("status") == "closed"]
    rs = [t["R"] for t in done]
    if not rs:
        return {"n": 0, "open": len(trades) - len(done)}
    m = sum(rs) / len(rs)
    sd = (sum((r - m) ** 2 for r in rs) / max(1, len(rs) - 1)) ** 0.5
    eq = pk = dd = 0.0
    for r in rs:
        eq += r
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    gw, gl = sum(r for r in rs if r > 0), -sum(r for r in rs if r <= 0)
    side = lambda s: [t["R"] for t in done if t["side"] == s]
    L, Sh = side("long"), side("short")
    return {"n": len(rs), "open": len(trades) - len(done), "win": round(sum(1 for r in rs if r > 0) / len(rs) * 100), "avgR": round(m, 3),
            "totR": round(sum(rs), 2), "t": round(m / sd * len(rs) ** 0.5, 2) if sd > 0 and len(rs) >= 5 else None,
            "pf": round(gw / gl, 2) if gl > 0 else None, "dd": round(dd, 2),
            "long": {"n": len(L), "totR": round(sum(L), 2), "avgR": round(sum(L) / len(L), 3) if L else None},
            "short": {"n": len(Sh), "totR": round(sum(Sh), 2), "avgR": round(sum(Sh) / len(Sh), 3) if Sh else None},
            "avgBars": round(sum(t.get("bars") or 0 for t in done) / len(done), 1)}


# ---------------------------------------------------------------- one ticker on the chart
def chart(sub, sym, tf, days, cfg):
    sym = (sym or "").upper().replace("/", ".")[:10]
    if not sym:
        raise BadRequest("Pick a ticker.")
    P = clean({**(cfg or {}), "tf": tf})
    days = max(20, min(700, int(days or 250)))
    bars = bars_of(sym, P["tf"], days)
    if not bars:
        raise BadRequest(f"No {P['tf']} candles for {sym}.")
    walls = walls_of(sym)
    first = sorted({b["t"][:10] for b in bars})[-days:][0] if bars else None
    trades, sigs, S = detect(bars, walls, P, first)
    k0 = next((i for i, b in enumerate(bars) if b["t"][:10] >= first), 0)
    view = bars[k0:]
    r = lambda v: None if v is None else round(v, 4)
    return {"t": sym, "tf": P["tf"], "params": P, "bars": view, "interval": 390 if P["tf"] == "1D" else 60,
            "ema": [[r(v) for v in e[k0:]] for e in S["ema"]], "kc": [list(x) if x else None for x in S["kc"][k0:]],
            "sq": [{k: x[k] for k in ("on", "fired", "mom", "up")} if x else None for x in S["sq"][k0:]],
            "inr": [(x or {}).get("s") for x in S["inr"][k0:]], "cvd": [round(v) for v in S["cvd"][k0:]],
            "fvg": [{k: z[k] for k in ("t", "type", "lo", "hi", "fillAt")} for z in S["fvg"] if (z.get("fillAt") or "9") >= view[0]["t"]][-200:],
            "walls": [{"t": b["t"], **{k: (walls.get(b["t"][:10]) or {}).get(k) for k in ("callWall", "putWall", "flip")}} for b in view],
            "trades": trades, "signals": sigs, "stats": stats(trades), "wallDays": sum(1 for b in view if walls.get(b["t"][:10]))}


# ---------------------------------------------------------------- settings
def get_cfg(sub):
    it = db.get(db.upk(sub), CFG_SK) or {}
    try:
        return clean(json.loads(it["data"])) if it.get("data") else dict(PARAMS)
    except ValueError:
        return dict(PARAMS)


def save_cfg(sub, cfg):
    P = clean(cfg)
    db.put({"PK": db.upk(sub), "SK": CFG_SK, "data": json.dumps(P)})
    return P


# ---------------------------------------------------------------- the list backtest (background)
def bt_public(sub):
    it = db.get(db.upk(sub), BT_SK) or {}
    out = json.loads(it["data"]) if it.get("data") else {}
    if out.get("running") and out["running"] < iso(now_ny() - timedelta(minutes=12)):
        out["running"] = None
    return out


def _bt_save(sub, x):
    db.put({"PK": db.upk(sub), "SK": BT_SK, "data": json.dumps(x, default=str, separators=(",", ":"))})


def bt_start(sub, b):
    b = b or {}
    cur = bt_public(sub)
    if cur.get("running"):
        return cur
    x = {"running": iso(now_ny()), "step": "starting", "universe": b.get("universe") if b.get("universe") in ("mag7", "all") else "mag7",
         "days": max(60, min(700, int(b.get("days") or 500))), "cfg": clean(b.get("cfg") or get_cfg(sub))}
    _bt_save(sub, x)
    import research
    try:
        research._invoke(sub, "ww2bt", {})
    except Exception as e:                             # not started: not left "running"
        x.update(running=None, step=None, error=f"couldn't start: {str(e)[:120]}")
        _bt_save(sub, x)
    return x


def bt_run(sub, budget_s=520):
    import smcopt
    import wallwatch
    t0 = time.time()
    x = bt_public(sub)
    P, days = clean(x.get("cfg")), int(x.get("days") or 500)
    tick = MAG7 if x.get("universe") != "all" else list(wallwatch.config(sub).get("tickers") or [])
    x.update(running=iso(now_ny()), step=f"loading {len(tick)} tickers", tickers=len(tick))
    _bt_save(sub, x)
    now = now_ny()
    first = (now - timedelta(days=int(days * 1.45))).strftime("%Y-%m-%d")
    cut = (now - timedelta(days=int(days * 1.45 * (1 - TRAIN)))).strftime("%Y-%m-%d")

    def one(sym):
        try:
            bars = bars_of(sym, P["tf"], days)
        except Exception:
            time.sleep(4)
            bars = bars_of(sym, P["tf"], days)
        return sym, bars, walls_of(sym)
    rows, allt, errs = [], [], []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in [ex.submit(one, s) for s in tick]:
            try:
                sym, bars, walls = f.result(timeout=max(1, budget_s - (time.time() - t0)))
            except Exception as e:
                errs.append(str(e)[:80])
                continue
            tr, _, _ = detect(bars, walls, P, first)
            done = [t for t in tr if t["status"] == "closed"]
            allt += [(t["at"][:10], sym, t["R"], t["side"]) for t in done]
            rows.append({**stats(tr), "sym": sym, "walls": sum(1 for b in bars if walls.get(b["t"][:10]))})
    part = lambda a, b: [t for t in allt if a <= t[0] < b]
    sc = lambda tr: {**smcopt.score(tr, cost=0), "long": round(sum(t[2] for t in tr if t[3] == "long"), 2),
                     "short": round(sum(t[2] for t in tr if t[3] == "short"), 2),
                     "nLong": sum(1 for t in tr if t[3] == "long"), "nShort": sum(1 for t in tr if t[3] == "short")}
    x.update(running=None, step=None, at=iso(now_ny()), split={"from": first, "cut": cut, "to": now.strftime("%Y-%m-%d")},
             all=sc(part(first, "9999")), train=sc(part(first, cut)), test=sc(part(cut, "9999")),
             byTicker=sorted(rows, key=lambda r: -(r.get("totR") or 0)), errors=errs[:5], loaded=len(rows))
    _bt_save(sub, x)
    return x


def bt_job(sub, params=None):
    try:
        return {"n": (bt_run(sub).get("all") or {}).get("n")}
    except Exception as e:
        x = bt_public(sub)
        x.update(running=None, step=None, error=str(e)[:200], at=iso(now_ny()))
        _bt_save(sub, x)
        return {"error": str(e)[:200]}
