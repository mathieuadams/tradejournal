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
    first = _trading_days_back(min(ev_day, today), days)
    if (today - first).days > 40:
        raise BadRequest("Alert charts are available for evaluations from the last 4 weeks.")
    # the chart runs from `days` trading days before the evaluation to now (at most 2 weeks after it)
    last = min(today, ev_day + timedelta(days=14))
    bars = _yahoo(sym.replace(".", "-"), "15m", datetime.combine(first, datetime.min.time()),
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
    if len(alerts) > 400:                     # keep the biggest prints
        keep = set(id(a) for a in sorted(alerts, key=lambda a: -a["premium"])[:400])
        alerts = [a for a in alerts if id(a) in keep]
    o = rec.get("origin") or {}
    return {"symbol": sym, "evaluatedAt": ev_at.replace(" ", "T"), "from": first.isoformat(), "to": last.isoformat(),
            "bars": bars, "alerts": alerts, "minPrint": cfg["repeatMinPremium"], "bigPrint": cfg["flowMinPremium"],
            "trigger": {"contract": o.get("contract"), "premium": o.get("topPremium") or o.get("premium")} if o.get("type") == "flow" else None,
            "note": note}
