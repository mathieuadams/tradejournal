"""Chat with your data: Claude answers using typed tools over the user's trades, never raw guesses."""
import os

import analytics
import claude
from util import BadRequest, now_ny
from views import load_views

FILTERS = {
    "symbol": {"type": "string", "description": "Ticker or underlying, e.g. TSLA, NVDA or MNQ. Matches the stock and all its options."},
    "asset_type": {"type": "string", "enum": ["stock", "option", "future"]},
    "option_type": {"type": "string", "enum": ["call", "put"]},
    "max_days_to_expiry": {"type": "integer", "description": "Options with at most this many days to expiry at entry"},
    "setup": {"type": "string", "description": "Setup name, case-insensitive substring"},
    "tag": {"type": "string", "description": "Mistake tag, e.g. Revenge, Moved stop"},
    "direction": {"type": "string", "enum": ["Long", "Short"]},
    "account": {"type": "string"},
    "start_date": {"type": "string", "description": "YYYY-MM-DD inclusive"},
    "end_date": {"type": "string", "description": "YYYY-MM-DD inclusive"},
    "time_from": {"type": "string", "description": "HH:MM exchange time, inclusive"},
    "time_to": {"type": "string", "description": "HH:MM exchange time, exclusive"},
    "result": {"type": "string", "enum": ["win", "loss"]},
}
TOOLS = [
    {"name": "query_trades", "description": "List closed trades matching filters. The UI shows the trades from your last call as a table.",
     "input_schema": {"type": "object", "properties": {**FILTERS,
        "sort": {"type": "string", "enum": ["net_desc", "net_asc", "date_desc", "r_desc", "r_asc"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 25}}}},
    {"name": "get_stats", "description": "Win rate, profit factor, expectancy, net P&L, drawdown for trades matching filters, optionally grouped.",
     "input_schema": {"type": "object", "properties": {**FILTERS,
        "group_by": {"type": "string", "enum": ["none", "setup", "symbol", "underlying", "asset_type", "option_type", "days_to_expiry", "trend", "vs_50day_ma", "extension_from_20ma", "relative_volume", "vs_hvc", "vs_anchored_vwap", "fair_value_gap", "hour", "weekday", "tag", "emotion", "account", "direction"]}}}},
]
SYSTEM = """You are a trading coach answering questions about the user's own trade journal.
Get every number from the tools; never estimate or invent. Times are exchange local (US/Eastern for stocks).
"First hour" means 09:30 to 10:30. Today is {today}.
Answer in 1-4 short sentences of plain text (no markdown tables; the app shows the trades from your last query_trades call).
If the data can't answer the question, say what's missing."""


def _filter(views, a):
    out = []
    for t in views:
        if t["status"] != "closed":
            continue
        if a.get("symbol") and a["symbol"].upper() not in (t["sym"].upper(), (t.get("underlying") or "").upper()):
            continue
        if a.get("asset_type") and t.get("assetType") != a["asset_type"]:
            continue
        if a.get("option_type") and t.get("optType") != a["option_type"]:
            continue
        if a.get("max_days_to_expiry") is not None and (t.get("dte") is None or t["dte"] > a["max_days_to_expiry"]):
            continue
        if a.get("setup") and a["setup"].lower() not in t["setup"].lower():
            continue
        if a.get("tag") and a["tag"].lower() not in [x.lower() for x in t["tags"]]:
            continue
        if a.get("direction") and t["dir"] != a["direction"]:
            continue
        if a.get("account") and t["acct"] != a["account"]:
            continue
        if a.get("start_date") and t["date"] < a["start_date"]:
            continue
        if a.get("end_date") and t["date"] > a["end_date"]:
            continue
        if a.get("time_from") and t["time"] < a["time_from"]:
            continue
        if a.get("time_to") and t["time"] >= a["time_to"]:
            continue
        if a.get("result") == "win" and t["net"] <= 0:
            continue
        if a.get("result") == "loss" and t["net"] > 0:
            continue
        out.append(t)
    return out


def _run_tool(name, args, views, state):
    rows = _filter(views, args)
    if name == "query_trades":
        key = {"net_desc": (lambda t: -t["net"]), "net_asc": (lambda t: t["net"]),
               "date_desc": (lambda t: t["openTs"]), "r_desc": (lambda t: -(t["r"] or -99)),
               "r_asc": (lambda t: t["r"] if t["r"] is not None else 99)}
        s = args.get("sort", "date_desc")
        rows = sorted(rows, key=key.get(s, key["date_desc"]), reverse=(s == "date_desc"))
        rows = rows[: min(int(args.get("limit") or 10), 25)]
        state["trade_ids"] = [t["id"] for t in rows]
        return {"count": len(rows), "trades": [{k: t[k] for k in ("date", "time", "sym", "underlying", "assetType", "optType", "expiry", "strike", "dir", "setup", "tags", "net", "r", "hold")} for t in rows]}
    if name == "get_stats":
        by = args.get("group_by", "none")
        return analytics.breakdown(rows, by) if by != "none" else analytics.stats(rows)
    return {"error": "unknown tool"}


def run(sub, history):
    if not isinstance(history, list) or not history:
        raise BadRequest("Send a list of messages.")
    msgs = []
    for m in history[-12:]:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip():
            msgs.append({"role": m["role"], "content": m["content"][:2000]})
    if not msgs or msgs[-1]["role"] != "user":
        raise BadRequest("The last message must be from the user.")
    views = load_views(sub)
    state = {"trade_ids": []}
    system = SYSTEM.format(today=now_ny().strftime("%Y-%m-%d"))
    model = os.environ.get("COACH_MODEL", "claude-sonnet-5")
    for _ in range(6):
        r = claude.messages(model, system, msgs, 1000, TOOLS)
        if r.get("stop_reason") != "tool_use":
            return {"reply": claude.text_of(r).strip(), "tradeIds": state["trade_ids"]}
        msgs.append({"role": "assistant", "content": r["content"]})
        results = []
        for b in r["content"]:
            if b.get("type") == "tool_use":
                import json
                out = _run_tool(b["name"], b.get("input") or {}, views, state)
                results.append({"type": "tool_result", "tool_use_id": b["id"], "content": json.dumps(out, default=str)})
        msgs.append({"role": "user", "content": results})
    return {"reply": "I couldn't finish that question. Try asking it more narrowly.", "tradeIds": state["trade_ids"]}
