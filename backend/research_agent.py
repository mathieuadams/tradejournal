"""The AI research agent, kept apart from everything else.

It has ONE job: read a record of past ideas and propose new ones as plain data. It cannot run backtests, read
candles, change settings, change the grading criteria or write to the database; research.py calls propose(),
then cleans, gates, tests and grades what comes back with its own deterministic code.

What it is shown is limited on purpose: past ideas with their 1st-half results only (trades, average R, t, PF, win
rate, which criteria failed on the 1st half). The 2nd half is the out-of-sample check of the pipeline: if the agent
saw it, every new round would be tuned on it and it would stop being out of sample."""
import os

SYSTEM = """You are the research agent of a systematic trading desk. You propose NEW strategy ideas to test on
historical data. Each idea is a set of changes to the trader's CURRENT settings: the rule keys and parameter keys of
the catalog (exact keys; true/false for rules, numbers for parameters) AND formula rules.

Formula rules let you test ANY idea the data allows, not only the built-in rules: write a condition as an expression
with the series / functions of "formulas" (e.g. "close > vwap and crossUp(ema(close, 9), ema(close, 21)) and cvd > 0").
The formula keys are listed in "formulaKeys": an entry formula is an extra condition (AND) on the entry candle, an exit
formula sells at the close of the first candle where it is true. To test a formula on its own, switch the other entry
rules off in the same idea. The ONLY limit is the data: Alpaca candles (1-minute / hourly / daily, with volume, so an
estimated CVD) and trades, Unusual Whales gamma walls / flip, dark pool levels and options flow alerts. Something that
needs other data (news, order book depth, earnings estimates, fundamentals...) goes in "missing".

You do not run tests and you never see the final grades. A separate pipeline tests every idea on the same history with
the same criteria; you are shown the in-sample (1st half) results of past ideas only. The required t-stat rises with
the number of ideas tested, so prefer a few well-reasoned ideas with a market mechanism over blind parameter fiddling.

Before proposing, study the RECORD of every idea already tried:
- never repeat an idea or a trivial variant of one that did badly (same rules, nearby numbers);
- build on what worked in-sample and say what you learned from; avoid rules that keep showing up in poor results;
- too few trades -> looser filters; many trades with t near 0 -> a real entry filter, not another exit tweak;
- change one theme at a time so the result teaches something.
Express every idea with the catalog and formulas when the data exists; only data that does not exist goes in
"missing" (the idea is then recorded as a feature request and not tested).
Follow the trader's focus request when one is given. Short, plain English."""

SCHEMA = {"type": "object", "properties": {
    "ideas": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string", "description": "short title, under 60 characters"},
        "hypothesis": {"type": "string", "description": "why this should work: the market mechanism, 1-3 sentences"},
        "changes": {"type": "object", "description": "catalog key -> value (true/false for rules, a number for parameters)",
                    "additionalProperties": True},
        "missing": {"type": "array", "items": {"type": "string"}, "description": "rules / indicators needed that the catalog lacks"},
        "learnedFrom": {"type": "string", "description": "which past results this idea builds on or avoids"}},
        "required": ["name", "hypothesis", "changes"]}},
    "notes": {"type": "string", "description": "what the record shows so far, 1-3 sentences"}},
    "required": ["ideas"]}

STRATEGY_TEXT = {"day": "1-minute day trading of QQQ / SPY (longs and shorts)",
                 "swing": "swing trading of a stock watchlist on hourly or daily candles (long only), around gamma walls and options flow"}


def public_criteria(c):
    """The criteria the agent may know (the bar to clear), without anything about the 2nd half."""
    return {k: c[k] for k in ("minTrades", "minT", "minPF", "minWin", "minAvgR", "costR") if k in c}


def record(xs, limit=120):
    """Past ideas as the agent may see them: what was tried and the 1st-half (in-sample) result. No grade, no 2nd
    half, no full-period numbers."""
    out = []
    for x in xs[:limit]:
        h1 = (x.get("metrics") or {}).get("h1") or {}
        r = {"name": x.get("name"), "by": x.get("by"), "changes": x.get("changes"), "status": x.get("status")}
        if x.get("missing"):
            r["missing"] = x["missing"]
        if x.get("status") == "tested":
            r["inSample"] = {k: h1.get(k) for k in ("n", "avgR", "t", "pf", "win", "totR")}
        out.append(r)
    return out


FORMULA_KEYS = {"day": {"fxLong": "extra entry condition for longs", "fxShort": "extra entry condition for shorts",
                        "fxExitLong": "exit a long at the close when true", "fxExitShort": "exit a short at the close when true"},
                "swing": {"fxEntry": "extra entry condition (long only)", "fxExit": "exit at the close of the first candle where true"}}


def propose(strategy, catalog, settings, crit, rec, n, focus, n_tested):
    """-> ([{name, hypothesis, changes, missing, learnedFrom}], notes). Plain data; research.py validates it."""
    import claude
    import formula
    sp = formula.spec()
    sp["series"] = {k: v for k, v in sp["series"].items() if k not in formula.UNAVAILABLE.get(strategy, ())}
    payload = {"strategy": STRATEGY_TEXT.get(strategy, strategy), "catalog": catalog or [{"key": k} for k in settings],
               "formulas": sp, "formulaKeys": FORMULA_KEYS.get(strategy, {}),
               "currentSettings": settings, "criteria": crit, "ideasTestedSoFar": n_tested, "record": rec,
               "focus": focus or None, "howMany": n}
    model = os.environ.get("RESEARCH_MODEL") or os.environ.get("COACH_MODEL", "claude-sonnet-5")
    out = claude.json_call(model, SYSTEM, payload, 4000, SCHEMA)
    props = [x for x in (out.get("ideas") or []) if isinstance(x, dict)][:n]
    for x in props:                                    # nothing but data comes back
        x["changes"] = x.get("changes") if isinstance(x.get("changes"), dict) else {}
        x["missing"] = [str(m) for m in (x.get("missing") or []) if m][:6] if isinstance(x.get("missing"), list) else []
    return props, str(out.get("notes") or "")[:600]
