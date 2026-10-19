"""The AI research agent, kept apart from everything else.

It has ONE job: read a record of past ideas and propose new ones as plain data. It cannot run backtests, read
candles, change settings, change the grading criteria or write to the database; research.py calls propose(),
then cleans, gates, tests and grades what comes back with its own deterministic code.

What it is shown is limited on purpose: past ideas with their 1st-half results only (trades, average R, t, PF, win
rate, which criteria failed on the 1st half). The 2nd half is the out-of-sample check of the pipeline: if the agent
saw it, every new round would be tuned on it and it would stop being out of sample."""
import os

SYSTEM = """You are the research agent of a systematic trading desk. You propose NEW strategy ideas to test on
historical data. Each idea is a COMPLETE strategy that you build from scratch: you start from "startingPoint", where
every entry rule, filter and exit is OFF (only a stop at its default size), and "changes" says what to turn on and set:
rule keys and parameter keys of the catalog (exact keys; true/false for rules, numbers for parameters), stop / target
/ exits, and formula rules. Give every idea at least one entry condition and the exits it needs. You design the
strategies; nobody gives you a strategy to tweak.

Formula rules let you test ANY idea the data allows, not only the built-in rules: write a condition as an expression
with the series / functions of "formulas" (e.g. "close > vwap and crossUp(ema(close, 9), ema(close, 21)) and cvd > 0").
The formula keys are listed in "formulaKeys": an entry formula is an extra condition (AND) on the entry candle, an exit
formula sells at the close of the first candle where it is true. To test a formula on its own, switch the other entry
rules off in the same idea. The ONLY limit is the data: Alpaca candles (1-minute / hourly / daily, with volume, so an
estimated CVD) and trades, Unusual Whales gamma walls / flip, dark pool levels and options flow alerts.
Day trading runs on 5-minute candles ("barMin": 5; 1 is allowed) and its formulas can also use the MARKET CONTEXT
series: options flow of QQQ / SPY during the candle (callPrem, putPrem, netPrem, netPremDay, sweepCall, sweepPut),
dealer gamma now (gex), market breadth (breadth, sectorBreadth, megaBreadth), volatility (vixy, vixyChg, vixTerm),
event days (fomc, cpi, nfp, opex, qend, eventDay), the pre-market / overnight move (onMove, pmRange) and intraday walls
(putWallNow, callWallNow, flipNow). This is information the price alone doesn't have: prefer ideas that use it. Something that
needs other data (news, order book depth, earnings estimates, fundamentals...) goes in "missing".

NEW INDICATORS: when an idea needs an indicator the app doesn't have, CREATE it: add it to "newIndicators" with a short
camelCase name and its formula built from the series / functions (e.g. {"name": "vwapDist", "formula": "(close - vwap)
/ atr(14)"}), then use that name in the idea's formulas. Indicators you created before are in "indicators": reuse them
by name. Only data that does not exist goes in "missing".

VARIETY: cover different families of ideas over time (trend following, mean reversion to VWAP / levels, opening range
and gap plays, gamma wall / flip / dark pool levels, order flow / CVD divergence, volatility squeeze and expansion,
time-of-day effects, momentum after strong days). The record shows which families were tried and how they did: don't
spend a round on another variant of a family that keeps failing in-sample; switch family.

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
        "newIndicators": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string", "description": "camelCase, 2-40 characters, not a built-in name"},
            "formula": {"type": "string", "description": "built from the series / functions / other indicators"},
            "what": {"type": "string", "description": "what it measures, one sentence"}}, "required": ["name", "formula"]},
            "description": "indicators this idea needs that don't exist yet, defined as formulas"},
        "missing": {"type": "array", "items": {"type": "string"}, "description": "DATA the app doesn't have (not indicators: create those)"},
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
        ch = dict(x.get("changes") or {})
        ch.update(x.get("readable") or {})             # formulas as written, with the indicator names
        r = {"name": x.get("name"), "by": x.get("by"), "status": x.get("status"),
             "changes": ch if x.get("origin", "blank") == "blank" else None,
             "settings": None if x.get("origin", "blank") == "blank" else {k: v for k, v in (x.get("full") or {}).items() if v not in (False, "", None)}}
        if x.get("missing"):
            r["missing"] = x["missing"]
        if x.get("status") == "tested":
            r["inSample"] = {k: h1.get(k) for k in ("n", "avgR", "t", "pf", "win", "totR")}
        out.append(r)
    return out


FORMULA_KEYS = {"day": {"fxLong": "extra entry condition for longs", "fxShort": "extra entry condition for shorts",
                        "fxExitLong": "exit a long at the close when true", "fxExitShort": "exit a short at the close when true"},
                "swing": {"fxEntry": "extra entry condition (long only)", "fxExit": "exit at the close of the first candle where true"}}


def _spec(strategy):
    import formula
    sp = formula.spec()
    sp["series"] = {k: v for k, v in sp["series"].items() if k not in formula.UNAVAILABLE.get(strategy, ())}
    return sp


def _list(v):
    """The model sometimes sends a list as a JSON string: read it either way."""
    import json
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def _ask(system, payload, schema, key):
    """One structured call (two tries when nothing usable comes back)."""
    import claude
    model = os.environ.get("RESEARCH_MODEL") or os.environ.get("COACH_MODEL", "claude-sonnet-5")
    out = {}
    for _ in range(2):
        out = claude.json_call(model, system, payload, 12000, schema) or {}
        items = [x for x in _list(out.get(key)) if isinstance(x, dict)]
        if items:
            return out, items
    raise ValueError(f"the AI returned no usable {key} (keys: {', '.join(out) or 'none'})")


def _clean(x):
    import json
    if isinstance(x.get("changes"), str):
        try:
            x["changes"] = json.loads(x["changes"])
        except ValueError:
            x["changes"] = {}
    x["newIndicators"] = _list(x.get("newIndicators"))
    x["missing"] = _list(x.get("missing")) if not isinstance(x.get("missing"), list) else x["missing"]
    x["changes"] = x.get("changes") if isinstance(x.get("changes"), dict) else {}
    x["missing"] = [str(m) for m in (x.get("missing") or []) if m][:6] if isinstance(x.get("missing"), list) else []
    x["newIndicators"] = [{"name": str(i.get("name") or "")[:40], "formula": str(i.get("formula") or "")[:600], "what": str(i.get("what") or "")[:200]}
                          for i in (x.get("newIndicators") or []) if isinstance(i, dict)][:6] if isinstance(x.get("newIndicators"), list) else []
    return x


BUILD_SYSTEM = """You are the research agent of a systematic trading desk. Some of your strategy ideas could not be
tested because they need something the app doesn't have. For each one, CREATE what is missing as new indicators:
formulas built from the series / functions given (and the indicators already created), then rewrite the idea's
"changes" so its formulas use them. The data is limited to Alpaca candles / trades and Unusual Whales walls / flip /
dark pool / options flow. Each idea lists its "problems": the exact errors of the formula checker (an unknown name, a
length that is not a plain whole number, ...): fix every one of them. When a missing piece truly needs other data
(news, order book, fundamentals...), say so in
"impossible" for that idea and give no changes. Keep each idea's intent; don't redesign it."""

BUILD_SCHEMA = {"type": "object", "properties": {"ideas": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "string"},
    "newIndicators": {"type": "array", "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "formula": {"type": "string"}, "what": {"type": "string"}}, "required": ["name", "formula"]}},
    "changes": {"type": "object", "additionalProperties": True, "description": "the idea's complete changes, formulas using the new indicators"},
    "impossible": {"type": "array", "items": {"type": "string"}, "description": "missing pieces that need data the app doesn't have"}},
    "required": ["id"]}}}, "required": ["ideas"]}


def build(strategy, catalog, settings, library, items):
    """Ideas waiting for an indicator -> [{id, newIndicators, changes, impossible}]. Plain data again."""
    payload = {"strategy": STRATEGY_TEXT.get(strategy, strategy), "catalog": catalog or [{"key": k} for k in settings],
               "formulas": _spec(strategy), "formulaKeys": FORMULA_KEYS.get(strategy, {}), "startingPoint": settings,
               "indicators": library, "ideas": items}
    out, got = _ask(BUILD_SYSTEM, payload, BUILD_SCHEMA, "ideas")
    res = []
    for x in got:
        if isinstance(x, dict) and x.get("id"):
            x = _clean(x)
            x["impossible"] = [str(m) for m in _list(x.get("impossible")) if m][:6]
            res.append(x)
    return res


def propose(strategy, catalog, settings, crit, rec, n, focus, n_tested, library=None):
    """-> ([{name, hypothesis, changes, missing, learnedFrom}], notes). Plain data; research.py validates it."""
    import claude
    payload = {"strategy": STRATEGY_TEXT.get(strategy, strategy), "catalog": catalog or [{"key": k} for k in settings],
               "formulas": _spec(strategy), "formulaKeys": FORMULA_KEYS.get(strategy, {}), "indicators": library or [],
               "startingPoint": settings, "criteria": crit, "ideasTestedSoFar": n_tested, "record": rec,
               "focus": focus or None, "howMany": n}
    out, props = _ask(SYSTEM, payload, SCHEMA, "ideas")
    props = props[:n]
    return [_clean(x) for x in props], str(out.get("notes") or "")[:600]
