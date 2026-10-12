"""Wall watchlist: one simple setup checked every hour on a fixed list of tickers.

Setup (on each finished hourly candle):
  call wall > gamma flip > put wall   (walls and flip of the day, Unusual Whales, from that day's open interest)
  green candle (close > open) whose low is at or below the put wall and whose close is above it (it crosses the put wall)
Trade: entry = that candle's close, target = the call wall of that day, stop = put wall - 0.5 hourly ATR (fixed, hit intrabar).
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


def config(sub):
    c = db.get(db.upk(sub), "WWCFG") or {}
    return {"tickers": c.get("tickers") or DEFAULT, "enabled": c.get("enabled", True)}


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
    db.put({"PK": db.upk(sub), "SK": "WWCFG", **cur})
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


STOP_ATR = 0.5      # stop = put wall - 0.5 x the hourly ATR at the signal, fixed for the trade


def atr14(bars):
    out, tr0 = [], 0.0
    for i, b in enumerate(bars):
        pc = bars[i - 1]["c"] if i else b["o"]
        tr = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
        tr0 = (tr0 * i + tr) / (i + 1) if i < 14 else (tr0 * 13 + tr) / 14
        out.append(tr0)
    return out


def detect(bars, walls, start=None):
    """Signals and their outcome. bars: finished hourly candles (earlier ones warm up the ATR);
    walls: {date: {callWall, flip, putWall}}; start: first date where signals can start."""
    sigs, open_ = [], None
    atr = atr14(bars)
    for i, b in enumerate(bars):
        if open_:                                      # follow the open signal first
            s = open_
            if b["l"] <= s["stop"]:                    # fixed stop under the put wall (stop order)
                s.update(status="stop", exitAt=b["t"][:16], exit=round(min(b["o"], s["stop"]), 4))
            elif b["h"] >= s["target"]:
                s.update(status="target", exitAt=b["t"][:16], exit=round(max(b["o"], s["target"]), 4))
            else:
                s["last"], s["lastAt"] = b["c"], b["t"][:16]
                s["mfe"] = max(s.get("mfe", s["entry"]), b["h"])
                s["mae"] = min(s.get("mae", s["entry"]), b["l"])
            if s["status"] != "open":
                open_ = None
            continue
        if start and b["t"][:10] < start:
            continue
        w = walls.get(b["t"][:10])
        if not setup_ok(w):
            continue
        pw, cw = float(w["putWall"]), float(w["callWall"])
        if b["c"] > b["o"] and b["l"] <= pw < b["c"]:
            a_ = atr[i]
            s = {"at": b["t"][:16], "entry": b["c"], "pw": pw, "flip": float(w["flip"]), "cw": cw, "target": cw,
                 "atr": round(a_, 4), "stop": round(pw - STOP_ATR * a_, 4),
                 "o": b["o"], "h": b["h"], "l": b["l"], "status": "open", "last": b["c"], "lastAt": b["t"][:16]}
            sigs.append(s)
            open_ = s
    for s in sigs:
        ex = s.get("exit") if s["status"] != "open" else s["last"]
        s["pct"] = round((ex / s["entry"] - 1) * 100, 2)
        s["tgtPct"] = round((s["target"] / s["entry"] - 1) * 100, 2)
        s["riskPct"] = round((s["entry"] - s["stop"]) / s["entry"] * 100, 2)
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
    sigs = detect(bars, walls, first)                  # earlier candles only warm up the ATR
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
            "setup": setup_ok(w), "signals": len(sigs), "daysWithWalls": len(walls), "days": len(dates)}


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
    since = {}
    for s in db.q_prefix(db.upk(sub), "WWSIG#"):
        if s.get("symbol") and (s.get("status") == "open" or s.get("stop") is None):   # open, or made with the old stop rule
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
        for d in dates:
            r = have.get(d)
            data = (r or {}).get("data")
            if d == ts:
                continue                                # today: refreshed by the hourly check
            if r and (data is None or (data.get("callWall") is not None and data.get("flip") is not None and not data.get("noflip"))):
                continue
            todo.append((sym, d))
    return todo, len(dates) * len(tickers)


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
            futs = [(t, ex.submit(flowdata.gex_day, key, t[0], t[1], True)) for t in todo[k:k + 12]]
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


def view(sub):
    if not db.get(db.upk(sub), "WWCFG"):               # first visit: the default list, on (the hourly job finds it)
        save_config(sub, {})
    cfg = config(sub)
    run_ = db.get(db.upk(sub), "WWRUN") or {}
    sigs = sorted(db.q_prefix(db.upk(sub), "WWSIG#"), key=lambda s: s.get("at") or "", reverse=True)
    sigs = [{k: v for k, v in s.items() if k not in ("PK", "SK")} for s in sigs if s.get("symbol") in cfg["tickers"]]
    done = [s for s in sigs if s.get("status") in ("target", "stop")]
    hit = [s for s in done if s["status"] == "target"]
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
    summary = {"signals": len(sigs), "open": len(sigs) - len(done), "closed": len(done), "targets": len(hit),
               "hitPct": round(len(hit) / len(done) * 100) if done else None,
               "avgPct": avg([s["pct"] for s in done]), "avgR": avg([s["R"] for s in done if s.get("R") is not None]),
               "avgWin": avg([s["pct"] for s in hit]), "avgLoss": avg([s["pct"] for s in done if s["status"] == "stop"]),
               "totalPct": round(sum(s["pct"] for s in done), 2) if done else None,
               "avgTgt": avg([s["tgtPct"] for s in sigs]), "avgRisk": avg([s["riskPct"] for s in sigs])}
    by = {}
    for s in done:
        b = by.setdefault(s["symbol"], {"symbol": s["symbol"], "n": 0, "targets": 0, "total": 0.0})
        b["n"] += 1
        b["targets"] += s["status"] == "target"
        b["total"] = round(b["total"] + s["pct"], 2)
    fill = db.get(db.upk(sub), "WWFILL") or {}
    return {"config": cfg, "run": {k: run_.get(k) for k in ("at", "running", "checked", "errors", "bfLeft", "bfAt", "bfDays")},
            "fill": {k: fill.get(k) for k in ("at", "running", "total", "left", "fetched", "errors")},
            "status": run_.get("status") or [], "signals": sigs[:500], "summary": summary,
            "byTicker": sorted(by.values(), key=lambda b: -b["total"])}


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
    alerts, flow_note = [], None
    if flow and dates:
        try:
            alerts = flow_alerts(sub, sym, dates[0], dates[-1])
        except Exception as e:
            flow_note = f"Options flow unavailable: {str(e)[:120]}"
    fill = db.get(db.upk(sub), f"WWFILL#{sym}") or {}
    return {"fill": {k: fill.get(k) for k in ("running", "total", "left", "at", "errors")}, "missingDays": len(missing), "noflipDays": len(noflip),
            "symbol": sym, "bars": bars, "walls": walls, "wallsNow": last, "signals": sigs, "alerts": alerts, "note": flow_note, "evals": [],
            "interval": 60, "days": days, "wallNote": note, "setupNow": setup_ok(last)}
