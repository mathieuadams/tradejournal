"""Live coach: reviews the open portfolio before the open, 30 minutes before the close, and right after a new entry.

Everything numeric is computed here (prices, P&L, extension, study levels, rule checks); Claude turns it into coaching.
The coach manages risk and process around the trader's OWN positions and rules. It never proposes new tickers.
"""
import json
import os
import time
from datetime import timedelta

import analytics
import claude
import context
import db
import indicators
from charts import _yahoo
from grouping import describe
from util import Unavailable, iso, now_ny, parse_iso
from views import load_settings, load_views

KINDS = {"premarket": "Pre-market check", "preclose": "Pre-close check (30 min before the close)",
         "entry": "New entry check", "manual": "Portfolio check"}

SYSTEM = """You are the trader's personal trading coach. The trader swing/momentum trades US stocks and options.
You receive their open positions with live numbers, the chart state of each underlying (trend, 21 EMA / Keltner channel,
the trader's own study: high-volume close, anchored VWAP, fair value gaps), today's activity, their own statistics and
THEIR OWN RULES. You coach like a seasoned trading coach standing next to them:

- Lead with what matters most right now (the one or two things that could hurt them or that they should act on).
- Judge every position against the trader's rules and plan. When a rule is broken or about to be, say so plainly and
  say what their rule implies (e.g. "your max-loss rule says this position should be closed below 71.20").
- Give concrete levels to watch from the data (EMA, anchored VWAP, HVC, FVG edges, prior close) and what each would mean.
- For options, weigh days to expiry, how far out of the money the strike is, and time decay against the planned hold.
- Point out portfolio-level risk: concentration, total premium at risk vs account size, correlated positions,
  too many new entries today, trading after consecutive losses, closeness to the daily loss limit.
- Pre-market: set the day's focus and the if/then plan per position. Pre-close: hold overnight or not, per rules and risk.
  New entry: was this entry consistent with their setup criteria and history; define the stop and the invalidation
  level if missing; what would make them add or cut.
- Use numbers from the data in every point. No generic advice, no boilerplate about "having a plan".
- Do NOT suggest new tickers or new positions. Talk only about managing existing positions and the trader's process.
  Phrase actions as options tied to their rules and the data ("consider trimming…", "your rule says…"), not as
  guarantees or predictions.
Answer by calling the submit tool."""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One sentence: the single most important thing right now."},
        "portfolio": {"type": "array", "maxItems": 4, "items": {"type": "string"},
                      "description": "Portfolio-level observations with numbers (risk, concentration, exposure, tilt)."},
        "positions": {"type": "array", "items": {"type": "object", "properties": {
            "ticker": {"type": "string"},
            "status": {"type": "string", "enum": ["on plan", "watch", "rule broken", "no plan"]},
            "note": {"type": "string", "description": "What the numbers say about this position now."},
            "levels": {"type": "string", "description": "Key levels to watch and what each would mean."},
            "action": {"type": "string", "description": "What the trader's rules imply now (hold / trim / close / set stop)."}},
            "required": ["ticker", "status", "note", "levels", "action"]}},
        "focus": {"type": "array", "maxItems": 3, "items": {"type": "string"},
                  "description": "Concrete focus items or if/then rules for the next session."},
        "rule_checks": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
            "rule": {"type": "string"}, "ok": {"type": "boolean"}, "detail": {"type": "string"}},
            "required": ["rule", "ok", "detail"]}},
    },
    "required": ["headline", "portfolio", "positions", "focus", "rule_checks"],
}


def _ema(xs, n):
    if len(xs) < n:
        return None
    k, e = 2 / (n + 1), sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def _keltner(bars):
    """21 EMA and 21 EMA of true range (the trader's Keltner channel, factor 3)."""
    if len(bars) < 30:
        return None
    closes = [b["c"] for b in bars]
    trs = [max(b["h"], bars[i - 1]["c"]) - min(b["l"], bars[i - 1]["c"]) if i else b["h"] - b["l"] for i, b in enumerate(bars)]
    ema, atr = _ema(closes, 21), _ema(trs, 21)
    if not ema or not atr:
        return None
    return {"ema21": round(ema, 4), "atr21": round(atr, 4), "upperBand": round(ema + 3 * atr, 4), "lowerBand": round(ema - 3 * atr, 4)}


def underlying_state(sym_key, cache):
    """Daily chart state for an underlying as of now (last price = latest bar)."""
    if sym_key in cache:
        return cache[sym_key]
    now = now_ny()
    try:
        daily = _yahoo(sym_key, "1d", now - timedelta(days=420), now + timedelta(hours=1))
    except Exception:
        daily = []
    try:
        intr = _yahoo(sym_key, "5m", now - timedelta(days=3), now + timedelta(hours=1))
    except Exception:
        intr = []
    if not daily:
        cache[sym_key] = None
        return None
    last = intr[-1]["c"] if intr else daily[-1]["c"]
    today = now.strftime("%Y-%m-%d")
    prior = [b for b in daily if b["t"][:10] < today]
    prev_close = prior[-1]["c"] if prior else None
    st = {"price": round(last, 4), "prevClose": prev_close,
          "dayChangePct": round((last / prev_close - 1) * 100, 2) if prev_close else None}
    k = _keltner(prior or daily)
    if k:
        st["keltner"] = k
        st["extFromEma21Atr"] = round((last - k["ema21"]) / k["atr21"], 2)
    ctx = context.compute(daily, today)
    if ctx:
        st["trend"] = ctx.get("trend")
        st["rsi14"] = ctx.get("rsi14")
        st["adrPct"] = ctx.get("adrPct")
        st["fromHigh20Pct"] = ctx.get("fromHigh20Pct")
    if prior:
        st["study"] = indicators.state_at(daily, daily.index(prior[-1]), price=last)
    cache[sym_key] = st
    return st


def build_payload(sub, kind, trade_id=None):
    settings = load_settings(sub)
    views = load_views(sub)
    open_ts = [v for v in views if v["status"] == "open"]
    closed = [v for v in views if v["status"] == "closed"]
    today = now_ny().strftime("%Y-%m-%d")
    cache, positions = {}, []

    # live option marks when Alpaca is connected; Schwab positions when Schwab is connected
    marks, schwab_pos = {}, None
    try:
        import alpaca
        import optiondata
        c = alpaca.creds(sub)
        if c:
            marks = optiondata.snapshots(c, [v["sym"] for v in open_ts if v.get("assetType") == "option"])
    except Exception:
        marks = {}
    try:
        import schwab
        schwab_pos = schwab.positions(sub)
    except Exception:
        schwab_pos = None
    sch_by_sym = {p["sym"]: p for p in (schwab_pos or []) if "sym" in p}

    for v in open_ts:
        d = describe(v["sym"])
        key = f"{d['underlying']}=F" if d["assetType"] == "future" else d["underlying"].replace(".", "-")
        u = underlying_state(key, cache)
        mult = v.get("mult") or 1
        mark = None
        if v["sym"] in sch_by_sym and sch_by_sym[v["sym"]].get("marketValue") is not None:
            sp = sch_by_sym[v["sym"]]
            mark = abs(sp["marketValue"]) / (abs(sp["qty"]) * mult) if sp["qty"] else None
        elif v["sym"] in marks:
            mark = marks[v["sym"]].get("mark")
        elif d["assetType"] != "option" and u:
            mark = u["price"]
        sg = 1 if v["dir"] == "Long" else -1
        pos = {
            "ticker": d["underlying"], "contract": v["sym"], "type": d["assetType"], "optType": d.get("optType"),
            "dir": v["dir"], "qty": v["qty"], "entry": v["entry"], "openedAt": v["openTs"],
            "heldDays": round((now_ny() - parse_iso(v["openTs"])).total_seconds() / 86400, 1),
            "costBasis": v.get("cost"), "setup": v["setup"], "tags": v["tags"], "notes": v["notes"],
            "plan": v["plan"] or None,
            "mark": round(mark, 4) if mark else None,
            "unrealizedPL": round((mark - v["entry"]) * sg * v["qty"] * mult - (v.get("fees") or 0), 2) if mark else None,
            "unrealizedPct": round((mark / v["entry"] - 1) * 100 * sg, 1) if mark and v["entry"] else None,
            "underlying": u,
        }
        if d["assetType"] == "option":
            pos["strike"], pos["expiry"] = d["strike"], d["expiry"]
            pos["daysToExpiry"] = (parse_iso(d["expiry"] + "T16:00:00") - now_ny()).days
            if u and u.get("price"):
                pos["strikeVsUnderlyingPct"] = round((d["strike"] / u["price"] - 1) * 100, 2)
            if v["sym"] in marks:
                pos["greeks"] = {k: marks[v["sym"]].get(k) for k in ("delta", "theta", "iv")}
        stop = (v["plan"] or {}).get("stop")
        if stop and mark:
            pos["distanceToStopPct"] = round((mark / stop - 1) * 100 * sg, 1)
        positions.append(pos)

    new_today = [v for v in views if v["date"] == today]
    closed_today = [v for v in closed if (v.get("closeTs") or "")[:10] == today]
    last5 = sorted(closed, key=lambda x: x.get("closeTs") or "")[-5:]
    streak = 0
    for t in reversed(last5):
        if t["net"] < 0:
            streak += 1
        else:
            break
    market = {}
    for idx in ("SPY", "QQQ"):
        st = underlying_state(idx, cache)
        if st:
            market[idx] = {k: st.get(k) for k in ("price", "dayChangePct", "trend", "extFromEma21Atr")}

    total_cost = sum(p["costBasis"] or 0 for p in positions)
    payload = {
        "check": KINDS.get(kind, kind), "time_et": iso(now_ny()),
        "trader_rules": settings.get("rules") or "(no written rules yet)",
        "settings": {"riskPerTrade": settings.get("riskPerTrade"), "accountSize": settings.get("accountSize"),
                     "maxPositionPct": settings.get("maxPositionPct"), "dailyLossLimit": (settings.get("prop") or {}).get("dailyLoss")},
        "market": market,
        "open_positions": positions,
        "portfolio": {
            "count": len(positions), "totalCostBasis": round(total_cost, 2),
            "largestPositionPctOfOpenCost": round(max((p["costBasis"] or 0) for p in positions) / total_cost * 100, 1) if total_cost else None,
            "optionsExpiringWithin7Days": sum(1 for p in positions if p.get("daysToExpiry") is not None and p["daysToExpiry"] <= 7),
            "unrealizedTotal": round(sum(p["unrealizedPL"] or 0 for p in positions), 2),
            "schwabBalances": next((p["balances"] for p in (schwab_pos or []) if "balances" in p), None),
        },
        "today": {"newEntries": len(new_today), "closedTrades": len(closed_today),
                  "realizedPL": round(sum(t["net"] for t in closed_today), 2), "lossStreak": streak,
                  "last5Results": [t["net"] for t in last5]},
        "history": {"allTrades": analytics.stats(closed), "patterns": analytics.patterns(closed[-200:])},
        "glossary": "extFromEma21Atr = distance of price above the 21 EMA in 21-day ATRs (Keltner bands are at +/-3). "
                    "study = trader's thinkorswim study: hvc (high-volume close) with bands, avwap anchored at the HVC day, "
                    "fvgState = active daily fair value gaps. distanceToStopPct > 0 means still above the planned stop.",
    }
    if trade_id:
        payload["new_entry"] = next((p for p in positions if p["contract"] == next((v["sym"] for v in open_ts if v["id"] == trade_id), None)), None)
        same = [v for v in closed if v.get("underlying") == (payload["new_entry"] or {}).get("ticker")]
        payload["history"]["sameTicker"] = analytics.stats(same) if same else None
    return payload


def run(sub, kind="manual", trade_id=None):
    payload = build_payload(sub, kind, trade_id)
    if not payload["open_positions"] and kind in ("premarket", "preclose"):
        note = {"headline": "No open positions.", "portfolio": [], "positions": [], "focus": [], "rule_checks": []}
    else:
        note = claude.json_call(os.environ.get("COACH_MODEL", "claude-sonnet-5"), SYSTEM, payload, 2500, SCHEMA)
    ts = iso(now_ny())
    item = {"PK": db.upk(sub), "SK": f"COACHNOTE#{ts}", "kind": kind, "tradeId": trade_id, "createdAt": ts,
            **{k: note.get(k) for k in ("headline", "portfolio", "positions", "focus", "rule_checks")},
            "snapshot": {"positions": len(payload["open_positions"]), "unrealized": payload["portfolio"]["unrealizedTotal"]}}
    db.put(item)
    return {k: v for k, v in item.items() if k not in ("PK",)}


def handler(event, context_):
    """Scheduled ({"kind": "premarket"|"preclose"}) or on demand ({"sub", "kind", "tradeId"})."""
    kind = event.get("kind", "manual")
    if event.get("sub"):
        return run(event["sub"], kind, event.get("tradeId"))
    done = 0
    for p in db.scan_sk("PROFILE"):
        lc = ((p.get("settings") or {}).get("liveCoach") or {})
        if not lc.get("enabled") or not lc.get(kind, True):
            continue
        sub = p["PK"][5:]
        try:
            import schwab
            if db.get(p["PK"], schwab.SK):
                schwab.sync_user(sub)          # freshest fills before the check
        except Exception as e:
            print("sync before coach failed", e)
        try:
            run(sub, kind)
            done += 1
        except Unavailable as e:
            print("coach skipped", sub, e)
    return {"notes": done}
