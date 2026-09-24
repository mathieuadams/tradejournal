"""Post-trade AI review: plan vs. what happened, in a few seconds."""
import os
from datetime import timedelta

import alpaca
import analytics
import claude
import db
from grouping import is_future, is_option
from util import iso, now_ny, parse_iso
from views import MISTAKES, load_settings, load_views, merge

SYSTEM = """You are a trading coach reviewing one closed trade from a trader's own journal.
Judge execution against the trader's plan and rules, not whether the market went their way.
Use only the numbers provided. Never invent prices, levels or events. If there is no plan, say so and judge only what the data shows.
Respond with JSON only, no prose, no code fences, in exactly this shape:
{"verdict":"followed_plan|partial|broke_plan|no_plan",
 "what_worked":["short sentence", "..."],
 "what_broke":["short sentence", "..."],
 "suggested_tags":["tags from allowed_tags that clearly apply and are not already set"],
 "lesson":"one sentence the trader can act on tomorrow"}
At most 3 items per list. Write dollar amounts like $120 and R like +1.4R. Plain words, no jargon."""


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


def run(sub, trade):
    pk = db.upk(sub)
    trade = enrich_excursion(sub, trade)
    j = db.get(pk, f"JRNL#{trade['id']}")
    v = merge(trade, j)
    settings = load_settings(sub)
    all_views = load_views(sub)
    same_day = [x for x in all_views if x["date"] == v["date"] and x["openTs"] < v["openTs"] and x["status"] == "closed"]
    since = iso(parse_iso(v["openTs"]) - timedelta(days=90))
    setup_hist = [x for x in all_views if v["setup"] and x["setup"] == v["setup"] and x["openTs"] >= since and x["id"] != v["id"]]
    payload = {
        "trade": {k: v[k] for k in ("sym", "dir", "acct", "date", "time", "hold", "qty", "entry", "exit",
                                    "net", "fees", "r", "riskD", "targetR", "mae", "mfe", "setup", "tags",
                                    "emotion", "notes")},
        "plan": v["plan"] or None,
        "planned_risk_per_trade": settings["riskPerTrade"],
        "earlier_trades_same_day": [{"time": x["time"], "sym": x["sym"], "net": x["net"], "tags": x["tags"]}
                                    for x in same_day],
        "same_setup_last_90_days": analytics.stats(setup_hist) if setup_hist else None,
        "allowed_tags": MISTAKES,
        "note": "mae/mfe are how far price went against/for the trade in R; null when unknown.",
    }
    out = claude.json_call(os.environ.get("REVIEW_MODEL", "claude-haiku-4-5-20251001"), SYSTEM, payload, 900)
    review = {
        "verdict": out.get("verdict", "partial"),
        "what_worked": [str(x) for x in (out.get("what_worked") or [])][:3],
        "what_broke": [str(x) for x in (out.get("what_broke") or [])][:3],
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
