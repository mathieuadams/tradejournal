"""Post-trade AI review: plan vs. what happened, in a few seconds."""
import os
from datetime import timedelta

import alpaca
import analytics
import claude
import db
from grouping import describe, is_future, is_option
from util import iso, now_ny, parse_iso
from views import MISTAKES, load_settings, load_views, merge

SYSTEM = """You are an experienced trading coach reviewing one closed trade for a discretionary swing/momentum trader
(stocks and options). You get the trade, the chart context of the underlying before entry, what the underlying did while
the trade was open, and the trader's own statistics for similar trades.

Your job is to give insight the trader could not see at a glance. Rules:
- Every point must cite a specific number from the data (a price, %, R, $, count or win rate). No generic advice.
- Never say "write a plan", "document your setup", "use a stop" or similar boilerplate. If there is no plan, at most one
  short clause may mention it, and only if it changes the conclusion.
- Separate the decision from the outcome: was the entry location sensible given trend, extension from the 20-day MA,
  volume and gap? Did the exit capture or waste the move the underlying actually made (use the excursion numbers)?
- For options, judge the contract choice: days to expiry, how far out of the money the strike was relative to the
  underlying, premium paid vs typical size, and whether the underlying move was big enough for this contract to pay.
- Compare with the trader's history for similar situations (same ticker, same trend / extension / DTE / time bucket).
  Say whether this trade fits a pattern that usually makes or loses money for them, with the numbers.
- Do not recommend buying or selling any security; talk about process, entry location, sizing and exits only.

Answer by calling the submit tool. At most 3 items per list. Plain words. Dollar amounts like $120, percentages like 4.2%."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["followed_plan", "partial", "broke_plan", "no_plan"]},
        "summary": {"type": "string", "description": "One sentence: what really decided this trade's result."},
        "what_worked": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "what_broke": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "pattern": {"type": "string", "description": "One sentence tying this trade to the trader's numbers for similar trades, or empty."},
        "suggested_tags": {"type": "array", "items": {"type": "string"}, "description": "Only from allowed_tags, not already set."},
        "lesson": {"type": "string", "description": "One concrete, testable adjustment for the next similar trade."},
    },
    "required": ["verdict", "summary", "what_worked", "what_broke", "pattern", "suggested_tags", "lesson"],
}


def enrich_excursion(sub, trade):
    """Fill in the worst/best price during the trade from 1-minute bars (stocks with Alpaca keys)."""
    if trade.get("maePx") is not None or trade["status"] != "closed":
        return trade
    if is_future(trade["sym"]) or is_option(trade["sym"]):
        return trade
    try:
        c = alpaca.creds(sub)
    except Exception:
        return trade
    if not c:
        return trade
    try:
        bs = alpaca.bars(c, trade["sym"], trade["openTs"][:16] + ":00", trade["closeTs"], 1)
    except Exception:
        return trade
    bs = [b for b in bs if trade["openTs"][:16] <= b["t"][:16] <= trade["closeTs"][:16]]
    if not bs:
        return trade
    lo, hi = min(b["l"] for b in bs), max(b["h"] for b in bs)
    mae, mfe = (lo, hi) if trade["dir"] == "Long" else (hi, lo)
    db.update(db.upk(sub), trade["SK"], {"maePx": mae, "mfePx": mfe})
    trade.update({"maePx": mae, "mfePx": mfe})
    return trade


def _grp(views, pred):
    xs = [v for v in views if v["status"] == "closed" and pred(v)]
    if not xs:
        return None
    st = analytics.stats(xs)
    return {"trades": st["trades"], "net": st["net"], "win_rate": st["win_rate"], "avg_net": round(st["net"] / st["trades"], 2)}


def _ext_bucket(x):
    return None if x is None else "below" if x < 0 else "0-1" if x < 1 else "1-2" if x < 2 else "2-3" if x < 3 else "3+"


def _dte_bucket(d):
    return None if d is None else "0-7" if d <= 7 else "8-30" if d <= 30 else "31-60" if d <= 60 else "60+"


def underlying_path(sub, trade):
    """What the underlying did between entry and exit (from intraday or daily bars)."""
    import charts
    from datetime import datetime as _dt
    age = (_dt.utcnow() - parse_iso(trade["openTs"])).days
    hold = trade.get("hold") or 0
    tf = "5m" if hold < 1440 and age < 55 else "1h" if age < 700 and hold < 20 * 1440 else "1d"
    try:
        bars = charts.get_bars(sub, trade, tf)["bars"]
    except Exception:
        return None
    key = 10 if tf == "1d" else 16
    during = [b for b in bars if trade["openTs"][:key] <= b["t"][:key] <= (trade["closeTs"] or trade["openTs"])[:key]]
    if not during:
        return None
    u_in, u_out = during[0]["o"], during[-1]["c"]
    hi, lo = max(b["h"] for b in during), min(b["l"] for b in during)
    bullish = (trade["dir"] == "Long") == (describe(trade["sym"]).get("optType") != "put")
    fav = (hi / u_in - 1) * 100 if bullish else (1 - lo / u_in) * 100
    adv = (1 - lo / u_in) * 100 if bullish else (hi / u_in - 1) * 100
    return {"timeframe": tf, "underlying_at_entry": round(u_in, 4), "underlying_at_exit": round(u_out, 4),
            "underlying_move_pct": round((u_out / u_in - 1) * 100, 2),
            "best_move_in_your_favor_pct": round(fav, 2), "worst_move_against_you_pct": round(adv, 2),
            "you_needed": "up" if bullish else "down"}


def run(sub, trade):
    import context
    pk = db.upk(sub)
    trade = enrich_excursion(sub, trade)
    context.for_trade(sub, trade)
    j = db.get(pk, f"JRNL#{trade['id']}")
    v = merge(trade, j)
    settings = load_settings(sub)
    views = [x for x in load_views(sub) if x["id"] != v["id"]]
    prior = [x for x in views if x["openTs"] < v["openTs"]]
    ctx = v.get("ctx") if v.get("ctx") and not v["ctx"].get("missing") else None
    path = underlying_path(sub, trade)
    d = describe(trade["sym"])
    option = None
    if d["assetType"] == "option":
        u = (path or {}).get("underlying_at_entry") or (ctx or {}).get("prevClose")
        option = {"type": d["optType"], "strike": d["strike"], "expiry": d["expiry"], "days_to_expiry_at_entry": v["dte"],
                  "premium_paid_per_contract": round(trade["entry"] * 100, 2), "contracts": trade["qty"],
                  "strike_vs_underlying_pct": round((d["strike"] / u - 1) * 100, 2) if u else None,
                  "option_return_pct": v.get("retPct")}
    same_day = sorted([x for x in prior if x["date"] == v["date"]], key=lambda x: x["openTs"])
    last2 = sorted([x for x in prior if x["status"] == "closed" and (x.get("closeTs") or "") <= v["openTs"]],
                   key=lambda x: x["closeTs"])[-2:]
    history = {
        "all_trades": _grp(views, lambda x: True),
        "same_ticker": _grp(views, lambda x: x.get("underlying") == v.get("underlying")),
        "same_setup": _grp(views, lambda x: v["setup"] and x["setup"] == v["setup"]),
        "same_asset_type": _grp(views, lambda x: x.get("assetType") == v.get("assetType")),
        "same_dte_bucket": _grp(views, lambda x: v.get("dte") is not None and _dte_bucket(x.get("dte")) == _dte_bucket(v["dte"])),
        "same_trend": _grp(views, lambda x: ctx and (x.get("ctx") or {}).get("trend") == ctx.get("trend")),
        "same_extension_bucket": _grp(views, lambda x: ctx and _ext_bucket((x.get("ctx") or {}).get("ext20Adr")) == _ext_bucket(ctx.get("ext20Adr"))),
        "same_hour": _grp(views, lambda x: x["time"][:2] == v["time"][:2]),
        "position_size_larger_than_this": _grp(views, lambda x: (x.get("cost") or 0) > (v.get("cost") or 0)),
    }
    payload = {
        "trade": {k: v.get(k) for k in ("sym", "underlying", "assetType", "dir", "acct", "date", "time", "hold", "qty",
                                         "entry", "exit", "net", "fees", "cost", "retPct", "r", "riskD", "setup", "tags",
                                         "emotion", "notes")},
        "hold_minutes": v["hold"],
        "option": option,
        "plan": v["plan"] or None,
        "chart_context_before_entry": ctx,
        "underlying_during_trade": path,
        "same_day_before_this_trade": [{"time": x["time"], "ticker": x.get("underlying"), "net": x["net"] if x["status"] == "closed" else "open"} for x in same_day],
        "previous_two_closed_trades_net": [x["net"] for x in last2],
        "history_for_similar_trades": {k: val for k, val in history.items() if val},
        "planned_risk_per_trade": settings["riskPerTrade"],
        "allowed_tags": MISTAKES,
        "glossary": "ext20Adr = distance of the prior close above the 20-day MA in average-daily-ranges; rvol = entry-day "
                    "volume / 20-day average; fromHigh20Pct = prior close vs 20-day high; retPct = trade P&L / capital used. study = the trader's own "
                    "thinkorswim study on daily bars: hvc = high-volume close (close of the latest day whose volume was the "
                    "highest of 21 days) with hvUp/hvDn bands, avwap = VWAP anchored on the day the HVC changed, bullFvg/bearFvg "
                    "= active daily fair value gaps (gap > 33% of 20-day ATR, kept until price closes through). intraday = the "
                    "5-min entry bar volume vs prior 20 bars, day VWAP, and study levels vs the actual entry price. option = the "
                    "contract's volume that day vs its 5-day average.",
    }
    out = claude.json_call(os.environ.get("REVIEW_MODEL", "claude-haiku-4-5-20251001"), SYSTEM, payload, 2000, SCHEMA)
    review = {
        "verdict": out.get("verdict", "partial"),
        "summary": str(out.get("summary") or ""),
        "what_worked": [str(x) for x in (out.get("what_worked") or [])][:3],
        "what_broke": [str(x) for x in (out.get("what_broke") or [])][:3],
        "pattern": str(out.get("pattern") or ""),
        "suggested_tags": [x for x in (out.get("suggested_tags") or []) if x in MISTAKES and x not in v["tags"]],
        "lesson": str(out.get("lesson") or ""),
        "model": os.environ.get("REVIEW_MODEL"), "createdAt": iso(now_ny()),
    }
    db.put({"PK": pk, "SK": f"REVIEW#{trade['id']}", **review})
    return review


def is_recent(trade, days=3):
    if not trade.get("closeTs"):
        return False
    return parse_iso(trade["closeTs"]) >= now_ny() - timedelta(days=days)
