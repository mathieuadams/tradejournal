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


PARAMS = {"stopAtr": 0.5, "trailAtr": 1.0, "pwSetup": True, "flipSetup": True}


def config(sub):
    c = db.get(db.upk(sub), "WWCFG") or {}
    out = {"tickers": c.get("tickers") or DEFAULT, "enabled": c.get("enabled", True)}
    out.update({k: c.get(k, v) for k, v in PARAMS.items()})
    return out


def version(cfg):
    """Signals made with other settings are recomputed."""
    return f"5|{float(cfg['stopAtr']):g}|{float(cfg['trailAtr']):g}|{int(bool(cfg['pwSetup']))}{int(bool(cfg['flipSetup']))}"


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
    for k in ("stopAtr", "trailAtr"):
        if k in body:
            try:
                cur[k] = max(0.0, min(10.0, round(float(body[k]), 2)))
            except (TypeError, ValueError):
                pass
    for k in ("pwSetup", "flipSetup"):
        if k in body:
            cur[k] = bool(body[k])
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
    """'pw' when call wall > flip > put wall, 'flip' when call wall > put wall > flip, else None."""
    cw, fl, pw = (w or {}).get("callWall"), (w or {}).get("flip"), (w or {}).get("putWall")
    if not (cw and fl and pw):
        return None
    cw, fl, pw = float(cw), float(fl), float(pw)
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
    k_stop, k_trail = float(P["stopAtr"]), float(P["trailAtr"])
    kinds = {k for k, on in (("pw", P["pwSetup"]), ("flip", P["flipSetup"])) if on}
    ver = version(P)
    sigs, open_ = [], None
    atr = atr14(bars)                                  # fallback when the 30-day daily ATR isn't available
    for i, b in enumerate(bars):
        if open_:                                      # follow the open signal first
            s = open_
            if b["l"] <= s["stop"]:                    # stop order (first stop, or trailed up)
                s.update(status="trail" if s["stop"] > s["stop0"] else "stop", exitAt=b["t"][:16], exit=round(min(b["o"], s["stop"]), 4))
            elif b["h"] >= s["target"]:
                s.update(status="target", exitAt=b["t"][:16], exit=round(max(b["o"], s["target"]), 4))
            else:
                s["last"], s["lastAt"] = b["c"], b["t"][:16]
                s["mfe"] = max(s.get("mfe", s["entry"]), b["h"])
                s["mae"] = min(s.get("mae", s["entry"]), b["l"])
                if k_trail > 0:                        # trailing stop: k x entry ATR under the highest high
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
        lvl = pw if kind == "pw" else fl                # the level the green candle has to cross
        if b["c"] > b["o"] and b["l"] <= lvl < b["c"]:
            a_ = (_atr_on(datr, b["t"][:10]) if datr else None) or atr[i]
            st = round(lvl - k_stop * a_, 4)
            s = {"at": b["t"][:16], "kind": kind, "level": lvl, "entry": b["c"], "pw": pw, "flip": fl, "cw": cw, "target": cw,
                 "atr": round(a_, 4), "stop": st, "stop0": st, "v": ver,
                 "o": b["o"], "h": b["h"], "l": b["l"], "status": "open", "last": b["c"], "lastAt": b["t"][:16]}
            sigs.append(s)
            open_ = s
    for s in sigs:
        ex = s.get("exit") if s["status"] != "open" else s["last"]
        s["pct"] = round((ex / s["entry"] - 1) * 100, 2)
        s["tgtPct"] = round((s["target"] / s["entry"] - 1) * 100, 2)
        s["riskPct"] = round((s["entry"] - s["stop0"]) / s["entry"] * 100, 2)
        s["R"] = round(s["pct"] / s["riskPct"], 2) if s["riskPct"] > 0 else None
    return sigs


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
    start = (now - timedelta(days=int(days * 1.5) + 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    if since:
        start = min(start, datetime.strptime(since, "%Y-%m-%d") - timedelta(days=3))
    bars = _yahoo(sym.replace(".", "-"), "1h", start, now + timedelta(days=1)) or []
    bars = finished(bars, now)
    dates = sorted({b["t"][:10] for b in bars})
    first = dates[-days] if len(dates) >= days else (dates[0] if dates else "9")
    if since:
        first = min(first, since)
    dates = [d for d in dates if d >= first]
    walls = _walls(key, sym, dates, now.strftime("%Y-%m-%d"))
    try:                                               # 30-day ATR from daily candles
        daily = _yahoo(sym.replace(".", "-"), "1d", start - timedelta(days=70), now + timedelta(days=1)) or []
        daily = [b for b in daily if b["t"][:10] < now.strftime("%Y-%m-%d")]      # today's candle isn't finished
        datr = daily_atr(daily)
    except Exception as e:
        print("wallwatch daily atr failed", sym, e)
        datr = None
    sigs = detect(bars, walls, first, config(sub), datr)
    bars = [b for b in bars if b["t"][:10] >= first]
    pk = db.upk(sub)
    for old in db.q_prefix(pk, f"WWSIG#{sym}#"):       # recomputed from `first` on: replace what was there
        if (old.get("at") or "") >= first:
            db.delete(pk, old["SK"])
    for s in sigs:
        db.put({"PK": pk, "SK": f"WWSIG#{sym}#{s['at']}", "symbol": sym, **s})
    last = bars[-1] if bars else None
    w = walls.get(last["t"][:10]) if last else None
    return {"symbol": sym, "price": last["c"] if last else None, "barAt": last["t"][:16] if last else None, "walls": w,
            "setup": setup_kind(w), "signals": len(sigs), "daysWithWalls": len(walls), "days": len(dates)}


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
    return {"checked": len(rows), "errors": len(errors)}


def _weekdays(n, today):
    out, d = [], today
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return sorted(out)


def walls_todo(tickers, days=45):
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
            if r and d in mp and (data is None or (data.get("callWall") is not None and data.get("flip") is not None and not data.get("noflip"))):
                continue
            todo.append((sym, d))
    return todo, len(dates) * len(tickers)


def _walls_and_mp(key, sym, d):
    import flowdata
    w = flowdata.gex_day(key, sym, d, True)
    try:
        maxpain_day(key, sym, d)
    except Exception as e:
        print("max pain failed", sym, d, e)
    return w


def fill_walls(sub, days=45, budget_s=480, ticker=None):
    """Fetch the walls and the flip of every day for every watchlist ticker (or one ticker), as many as the time allows."""
    import flowdata
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    pk = db.upk(sub)
    cfg = config(sub)
    key = flowdata._key(sub)
    sk = f"WWFILL#{ticker}" if ticker else "WWFILL"
    todo, total = walls_todo([ticker] if ticker else cfg["tickers"], days)
    db.put({"PK": pk, "SK": sk, "running": iso(now_ny()), "total": total, "left": len(todo), "days": days,
            "at": (db.get(pk, sk) or {}).get("at")})
    done, errors = 0, []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for k in range(0, len(todo), 12):
            if time.time() - t0 > budget_s:
                break
            futs = [(t, ex.submit(_walls_and_mp, key, t[0], t[1])) for t in todo[k:k + 12]]
            for t, f in futs:
                try:
                    f.result()
                    done += 1
                except Exception as e:
                    errors.append(f"{t[0]} {t[1]}: {str(e)[:80]}")
    left = max(0, len(todo) - done - len(errors))
    out = {"PK": pk, "SK": sk, "running": None, "total": total, "left": left, "fetched": done, "days": days,
           "errors": errors[:5], "at": iso(now_ny())}
    if ticker and not left:                              # the ticker's signals over those days
        try:
            check_ticker(sub, key, ticker, days, now_ny().replace(tzinfo=None))
        except Exception as e:
            out["errors"] = (out["errors"] + [f"signals: {str(e)[:80]}"])[:5]
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


def summarize(sigs):
    done = [s for s in sigs if s.get("status") in ("target", "stop", "trail")]
    hit = [s for s in done if s["status"] == "target"]
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    summary = {"signals": len(sigs), "open": len(sigs) - len(done), "closed": len(done), "targets": len(hit),
               "hitPct": round(len(hit) / len(done) * 100) if done else None,
               "avgPct": avg([s["pct"] for s in done]), "avgR": avg([s["R"] for s in done if s.get("R") is not None]),
               "avgWin": avg([s["pct"] for s in hit]), "avgLoss": avg([s["pct"] for s in done if s["status"] == "stop"]), "trails": sum(1 for s in done if s["status"] == "trail"),
               "avgTrail": avg([s["pct"] for s in done if s["status"] == "trail"]),
               "totalPct": round(sum(s["pct"] for s in done), 2) if done else None,
               "avgTgt": avg([s["tgtPct"] for s in sigs]), "avgRisk": avg([s["riskPct"] for s in sigs])}
    return summary


def view(sub):
    if not db.get(db.upk(sub), "WWCFG"):               # first visit: the default list, on (the hourly job finds it)
        save_config(sub, {})
    cfg = config(sub)
    run_ = db.get(db.upk(sub), "WWRUN") or {}
    sigs = sorted(db.q_prefix(db.upk(sub), "WWSIG#"), key=lambda s: s.get("at") or "", reverse=True)
    sigs = [{k: v for k, v in s.items() if k not in ("PK", "SK")} for s in sigs if s.get("symbol") in cfg["tickers"]]
    summary = summarize(sigs)
    by_kind = {k: summarize([x for x in sigs if (x.get("kind") or "pw") == k]) for k in ("pw", "flip")}
    done = [s for s in sigs if s.get("status") in ("target", "stop", "trail")]
    by = {}
    for s in done:
        b = by.setdefault(s["symbol"], {"symbol": s["symbol"], "n": 0, "targets": 0, "total": 0.0})
        b["n"] += 1
        b["targets"] += s["status"] == "target"
        b["total"] = round(b["total"] + s["pct"], 2)
    fill = db.get(db.upk(sub), "WWFILL") or {}
    return {"config": cfg, "run": {k: run_.get(k) for k in ("at", "running", "checked", "errors", "bfLeft", "bfAt", "bfDays")},
            "fill": {k: fill.get(k) for k in ("at", "running", "total", "left", "fetched", "errors")},
            "status": run_.get("status") or [], "signals": sigs[:500], "summary": summary, "byKind": by_kind,
            "byTicker": sorted(by.values(), key=lambda b: -b["total"])}


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
    start = (now - timedelta(days=int(days * 1.5) + 5)).replace(hour=0, minute=0, second=0, microsecond=0)
    bars = _yahoo(sym.replace(".", "-"), "1h", start, now + timedelta(days=1)) or []
    bars = [{"t": b["t"][:16], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v")}
            for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
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
    sigs = [{k: v for k, v in s.items() if k not in ("PK", "SK")} for s in db.q_prefix(db.upk(sub), f"WWSIG#{sym}#")]
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
    fill = db.get(db.upk(sub), f"WWFILL#{sym}") or {}
    return {"fill": {k: fill.get(k) for k in ("running", "total", "left", "at", "errors")}, "missingDays": len(missing), "noflipDays": len(noflip),
            "symbol": sym, "bars": bars, "walls": walls, "wallsNow": last, "signals": sigs, "alerts": alerts, "note": flow_note, "evals": [],
            "interval": 60, "days": days, "wallNote": note, "setupNow": setup_kind(last),
            "maxPain": maxpain, "expirations": exps, "maxPainDays": len(maxpain),
            "maxPainMissing": sum(1 for d in dates if d != today and d not in mpd)}


def darkpool(sub, sym, min_premium=None, days=45):
    """Dark pool prints of the last `days` trading days at or above min_premium (default $2M), plus the top levels."""
    import flowdata
    sym = (sym or "").upper().replace("/", ".")
    mp = float(min_premium) if min_premium else 2_000_000
    out = flowdata.darkpool(sub, sym, _weekdays(days, now_ny().date()), max(100_000, mp))
    out["symbol"] = sym
    return out
