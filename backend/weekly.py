"""Weekly AI report: top leaks in dollars and one concrete rule for next week.

Runs every Sunday for every user, or on demand with {"sub": ...}.
"""
import os
from datetime import timedelta

import analytics
import claude
import db
from util import Unavailable, iso, now_ny
from views import load_views

SYSTEM = """You are a trading coach writing a trader's weekly report from their own journal data.
All numbers are already computed; use them exactly and never invent any.
Answer by calling the submit tool. Use at most 3 leaks, largest dollar loss first, only ones with negative dollars.
A good rule is specific enough that the trader can tell at the end of each day whether they followed it."""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"}, "summary": {"type": "string"},
        "leaks": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {
            "label": {"type": "string"}, "dollars": {"type": "number"}, "comment": {"type": "string"}},
            "required": ["label", "dollars", "comment"]}},
        "rule": {"type": "string"}, "rule_reason": {"type": "string"},
        "last_rule_followed": {"type": "string", "enum": ["yes", "no", "unknown"]},
    },
    "required": ["headline", "summary", "leaks", "rule", "rule_reason", "last_rule_followed"],
}


def run_for(sub):
    views = load_views(sub)
    today = now_ny()
    week_start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    q_start = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    week = [v for v in views if v["date"] > week_start]
    quarter = [v for v in views if v["date"] > q_start]
    pk = db.upk(sub)
    prev = db.q_prefix(pk, "REPORT#", desc=True, limit=1)
    payload = {
        "week_ending": today.strftime("%Y-%m-%d"),
        "week_stats": analytics.stats(week),
        "week_leaks": analytics.leaks(week),
        "patterns_last_90_days": analytics.patterns(quarter),
        "setups_this_week": analytics.breakdown(week, "setup"),
        "last_week_rule": prev[0].get("rule") if prev else None,
        "leak_definition": "Each trade counts once: first mistake tag, else untagged trades opened after 11:00, else clean.",
    }
    if not week:
        report = {"headline": "No closed trades this week.", "summary": "Nothing to review. Import or sync your trades to get a report.",
                  "leaks": [], "rule": prev[0].get("rule") if prev else "", "rule_reason": "", "last_rule_followed": "unknown"}
    else:
        out = claude.json_call(os.environ.get("COACH_MODEL", "claude-sonnet-5"), SYSTEM, payload, 1500, SCHEMA)
        report = {k: out.get(k) for k in ("headline", "summary", "leaks", "rule", "rule_reason", "last_rule_followed")}
    report.update({"weekEnding": payload["week_ending"], "stats": payload["week_stats"],
                   "computedLeaks": payload["week_leaks"], "createdAt": iso(today)})
    db.put({"PK": pk, "SK": f"REPORT#{payload['week_ending']}", **report})
    return report


def handler(event, context):
    subs = [event["sub"]] if event.get("sub") else [p["PK"][5:] for p in db.scan_sk("PROFILE")]
    done = 0
    for sub in subs:
        try:
            run_for(sub)
            done += 1
        except Unavailable as e:
            print("weekly report skipped:", sub, e)
    return {"reports": done}
