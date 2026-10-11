"""Chart for an evaluation: 15-minute price bars of the ticker with every options alert bought at the ask (calls and
puts, from the smallest print the bot counts) over the same days, so alerts can be lined up with price."""
from datetime import datetime, timedelta

import db
from util import BadRequest, NotFound, now_ny


def _trading_days_back(d, n):
    """The date n trading days (weekdays) before d, d included as day 1."""
    count = 0
    while True:
        if d.weekday() < 5:
            count += 1
            if count >= n:
                return d
        d -= timedelta(days=1)


def build(sub, bot_id, days=5):
    days = 45 if int(days or 5) > 10 else 5
    import autotrader
    import flowdata
    from charts import _yahoo
    rec = db.get(db.upk(sub), f"BOT#{bot_id}")
    if not rec:
        raise NotFound("Not found.")
    sym = rec["symbol"]
    cfg = autotrader.settings(sub)
    ev_at = (rec.get("createdAt") or "")[:16]
    ev_day = datetime.strptime(ev_at[:10], "%Y-%m-%d").date() if ev_at else now_ny().date()
    today = now_ny().date()
    long_view = days > 10
    if long_view:              # the last 45 trading days to today, hourly bars
        first, last, tf = _trading_days_back(today, days), today, "1h"
    else:
        first = _trading_days_back(min(ev_day, today), days)
        if (today - first).days > 40:
            raise BadRequest("Alert charts are available for evaluations from the last 4 weeks. Use the 45-day view.")
        # the chart runs from `days` trading days before the evaluation to now (at most 2 weeks after it)
        last, tf = min(today, ev_day + timedelta(days=14)), "15m"
    bars = _yahoo(sym.replace(".", "-"), tf, datetime.combine(first, datetime.min.time()),
                  datetime.combine(last, datetime.min.time()) + timedelta(days=1)) or []
    bars = [{"t": b["t"][:16], "o": round(b["o"], 4), "h": round(b["h"], 4), "l": round(b["l"], 4), "c": round(b["c"], 4), "v": b.get("v")}
            for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    alerts, note = [], None
    try:
        res = flowdata.alerts(sub, cfg["repeatMinPremium"], "all", 0, 400, True, False, sym, 0, (today - first).days + 1,
                              exclude_etfs=False)
        for a in res.get("alerts") or []:
            at = (a.get("atEt") or "").replace(" ", "T")[:16]
            if (a.get("ticker") or "").upper() != sym or not at or at[:10] < first.isoformat() or at[:10] > last.isoformat():
                continue
            alerts.append({"t": at, "premium": round(a.get("premium") or 0), "type": "put" if a.get("type") == "put" else "call",
                           "contract": a.get("contract"), "big": (a.get("premium") or 0) >= cfg["flowMinPremium"],
                           "sweep": bool(a.get("sweep")), "rule": a.get("rule")})
    except Exception as e:
        note = f"Alerts unavailable: {str(e)[:120]}"
    alerts.sort(key=lambda a: a["t"])
    cap = 800 if long_view else 400
    if len(alerts) > cap:                     # keep the biggest prints
        keep = set(id(a) for a in sorted(alerts, key=lambda a: -a["premium"])[:cap])
        alerts = [a for a in alerts if id(a) in keep]
    walls, now_w, wnote, wsrc, winfo = [], None, None, "snapshots", None
    uw_ok = False
    try:                                         # day-by-day walls from each day's open interest (Unusual Whales)
        dates, d = [], first
        while d <= last:
            if d.weekday() < 5:
                dates.append(d.isoformat())
            d += timedelta(days=1)
        hist, uerr = flowdata.gex_history(sub, sym, dates)
        winfo = {"days": len(hist), "of": len(dates), "error": uerr}
        if hist:
            uw_ok, wsrc = True, "uw"
            walls = [{"t": f"{h['date']}T09:30", "callWall": h.get("callWall"), "putWall": h.get("putWall"), "flip": h.get("flip"), "src": "uw",
                      "callGex": h.get("callGex"), "putGex": h.get("putGex")} for h in hist]
            lh = hist[-1]
            now_w = {"t": f"{lh['date']}T09:30", "callWall": lh.get("callWall"), "putWall": lh.get("putWall"), "flip": lh.get("flip"),
                     "source": "Unusual Whales, all expirations, open interest of " + lh["date"]}
        if not hist:
            wnote = (f"Unusual Whales gave no day-by-day walls for {sym}" + (f" ({uerr})" if uerr else "")
                     + "; showing the bot's own readings (evaluations and chart openings).")
    except Exception as e:
        wnote = f"Day-by-day walls unavailable ({str(e)[:120]}); showing the bot's own readings (evaluations and chart openings)."
    if not uw_ok:
        try:
            import gex
            since = first.isoformat()
            for r in db.q_prefix(db.upk(sub), "BOT#"):           # every evaluation of the ticker recorded the walls then
                if r.get("symbol") == sym and (r.get("createdAt") or "") >= since and (r.get("gamma") or {}).get("callWall") is not None:
                    gm = r["gamma"]
                    walls.append({"t": r["createdAt"][:16], "callWall": gm.get("callWall"), "putWall": gm.get("putWall"),
                                  "flip": gm.get("gammaFlip"), "src": "evaluation"})
            walls += gex.wall_history(sym, since)
            if last >= today:
                now_w = gex.walls_now(sub, sym)
                walls.append({**{k: now_w.get(k) for k in ("t", "callWall", "putWall", "flip")}, "src": "now"})
        except Exception as e:
            wnote = f"Gamma walls unavailable: {str(e)[:120]}"
    if now_w and wsrc == "snapshots":
        now_w = {**now_w, "source": f"options expiring in 45 days, {now_w.get('source') or ''} {now_w['t'].replace('T', ' ')[5:]} ET"}
    seen, wl = set(), []
    for w in sorted(walls, key=lambda w: w["t"]):
        if w["t"] in seen or w.get("callWall") is None:
            continue
        seen.add(w["t"])
        if wl and all(wl[-1].get(k) == w.get(k) for k in ("callWall", "putWall", "flip")) and w["src"] != "now":
            continue                              # unchanged: the step line already covers it
        wl.append(w)
    o = rec.get("origin") or {}
    return {"walls": wl, "wallsNow": now_w, "wallNote": wnote, "wallSource": wsrc, "wallInfo": winfo, "days": days, "interval": 60 if long_view else 15,
            "symbol": sym, "evaluatedAt": ev_at.replace(" ", "T"), "from": first.isoformat(), "to": last.isoformat(),
            "bars": bars, "alerts": alerts, "minPrint": cfg["repeatMinPremium"], "bigPrint": cfg["flowMinPremium"],
            "trigger": {"contract": o.get("contract"), "premium": o.get("topPremium") or o.get("premium")} if o.get("type") == "flow" else None,
            "note": note}


def darkpool(sub, bot_id, days=5, min_premium=None):
    import flowdata
    rec = db.get(db.upk(sub), f"BOT#{bot_id}")
    if not rec:
        raise NotFound("Not found.")
    days = 45 if int(days or 5) > 10 else 5
    today = now_ny().date()
    if days > 10:
        first, last = _trading_days_back(today, days), today
    else:
        ev_day = datetime.strptime((rec.get("createdAt") or "")[:10], "%Y-%m-%d").date() if rec.get("createdAt") else today
        first = _trading_days_back(min(ev_day, today), days)
        last = min(today, ev_day + timedelta(days=14))
    dates, d = [], first
    while d <= last:
        if d.weekday() < 5:
            dates.append(d.isoformat())
        d += timedelta(days=1)
    mp = float(min_premium) if min_premium else (2_000_000 if days > 10 else 500_000)
    out = flowdata.darkpool(sub, rec["symbol"], dates, mp)
    out["symbol"] = rec["symbol"]
    return out
