"""Final visual check before the paper bot places an order: draw the daily and hourly charts with the levels the rules
used (invalidation, target, gamma flip, walls, strikes, fair value gaps, 21 EMA, 50-day, anchored VWAP) and ask Claude
to look at them. Verdict: approve / caution / reject. Stored on the bot record; the images are stored beside it."""
import base64
import json
import os
import re
from datetime import timedelta

import chartimg
import claude
import db
import indicators
from charts import _yahoo
from util import iso, now_ny

GRAY, RED, GREEN, AMBER, BLUE, PURPLE = (110, 116, 128), (220, 38, 38), (22, 163, 74), (217, 119, 6), (37, 99, 235), (147, 51, 234)
ORANGE, TEAL, MAGENTA = (234, 88, 12), (13, 148, 136), (190, 24, 93)

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "caution", "reject"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string", "description": "One or two sentences: what the charts show and the decision."},
        "supports": {"type": "array", "items": {"type": "string"}, "description": "What on the charts supports the trade."},
        "concerns": {"type": "array", "items": {"type": "string"}, "description": "What on the charts argues against it."},
    },
    "required": ["verdict", "confidence", "summary", "supports", "concerns"],
}

SYSTEM = """You are the final visual check for an automated options trading bot (paper account). The trade idea came from
unusual options flow and has already passed rule-based checks (chart signals, gamma exposure, events, contract
liquidity). Your job is the last look a discretionary trader would take at the chart before clicking buy.

Look at the actual charts and judge whether the setup is real and well placed:
- Trend and structure: higher highs/lows for a bullish trade, clean base or breakout vs. choppy, overlapping range.
- Extension: is price stretched far from the 21 EMA or after several large up days (chasing / parabolic)?
- Overhead supply: prior highs, gaps or congestion between price and the target that will likely stop the move.
- The invalidation level: is it a logical place (below support / the gap / the EMA) or inside normal noise?
- Fair value gaps and anchored VWAP: does price respect them? Is a bear gap overhead?
- Volume: does it confirm (expansion on up days, drying up on pullbacks) or diverge?
- Hourly chart: is the short-term entry timing reasonable (not into a spike, not breaking down intraday)?
- Anything the rules can't see: gap risk, a recent failed breakout, a wide-range reversal bar, a blow-off top.

Verdict: "approve" when the charts clearly support the trade as specified; "reject" when something on the charts clearly
contradicts it; "caution" when it is mixed or marginal. Be concrete: name levels and bars you see. Do not restate the
rule checks; judge the picture. Keep each list item to one short sentence."""


def _ema(xs, n):
    k, e, out = 2 / (n + 1), None, []
    for i, x in enumerate(xs):
        if i < n - 1:
            out.append(None)
            continue
        e = sum(xs[:n]) / n if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def _sma(xs, n):
    return [None if i < n - 1 else sum(xs[i - n + 1:i + 1]) / n for i in range(len(xs))]


def _zones(ser, offset):
    """Current bull/bear fair value gaps with the bar index where each started (relative to the plotted window)."""
    out = []
    last = ser[-1]
    for key, label, col in (("bull", "BULL FVG", (22, 163, 74)), ("bear", "BEAR FVG", (220, 38, 38))):
        top, bot = last[f"{key}Top"], last[f"{key}Bot"]
        if top is None:
            continue
        i = len(ser) - 1
        while i > 0 and ser[i - 1][f"{key}Top"] == top and ser[i - 1][f"{key}Bot"] == bot:
            i -= 1
        out.append((label, min(top, bot), max(top, bot), col, max(0, i - 2 - offset)))
    return out


def _levels(rec):
    p, g = rec.get("proposal") or {}, rec.get("gamma") or {}
    lv = [("LAST", (rec.get("signals") or {}).get("price"), GRAY, False),
          ("STOP", p.get("underlyingStop"), RED, True), ("TARGET", p.get("underlyingTarget"), GREEN, True),
          ("FLIP", g.get("gammaFlip"), AMBER, False), ("CALL WALL", g.get("callWall"), BLUE, False),
          ("PUT WALL", g.get("putWall"), PURPLE, False), ("STRIKE", p.get("strike"), GRAY, True)]
    if p.get("shortStrike") is not None:
        lv.append(("SHORT", p.get("shortStrike"), MAGENTA, True))
    return lv


def _with_today(daily, hourly, now):
    """The daily feed can lag (today's bar missing or not updated after the close). Rebuild today's daily bar from
    today's hourly bars so the close, the fair value gaps and the EMA reflect today."""
    if not daily or not hourly:
        return daily
    today = now.date().isoformat()
    hs = [b for b in hourly if b["t"][:10] == today]
    if not hs:
        return daily
    bar = {"t": today + "T00:00:00", "o": hs[0]["o"], "h": max(b["h"] for b in hs), "l": min(b["l"] for b in hs),
           "c": hs[-1]["c"], "v": sum(b.get("v") or 0 for b in hs)}
    if daily[-1]["t"][:10] == today:
        d = daily[-1]
        return daily[:-1] + [{**d, "h": max(d["h"], bar["h"]), "l": min(d["l"], bar["l"]), "c": bar["c"], "v": max(d.get("v") or 0, bar["v"])}]
    if daily[-1]["t"][:10] < today:
        return daily + [bar]
    return daily


def build_charts(rec, ctx=None):
    """Daily + hourly charts with the trade's levels. ctx (dict, optional) receives the current price and indicator
    values; when given, the price line is the current price (NOW) instead of the price at evaluation (LAST)."""
    sym = rec["symbol"]
    now = now_ny()
    daily = _yahoo(sym.replace(".", "-"), "1d", now - timedelta(days=420), now + timedelta(hours=1))
    hourly = _yahoo(sym.replace(".", "-"), "1h", now - timedelta(days=22), now + timedelta(hours=1))
    daily = _with_today(daily, hourly, now)
    out = []
    levels = _levels(rec)
    if ctx is not None and (hourly or daily):
        price = (hourly or daily)[-1]["c"]
        ctx["price"] = round(price, 2)
        levels = [("NOW", price, GRAY, False)] + [lv for lv in levels if lv[0] != "LAST"]
    if daily:
        cl = [b["c"] for b in daily]
        ema, sma50, ser = _ema(cl, 21), _sma(cl, 50), indicators.series(daily)
        k = max(0, len(daily) - 130)
        if ctx is not None:
            ctx.update(ema21=round(ema[-1], 2) if ema[-1] else None, sma50=round(sma50[-1], 2) if sma50[-1] else None,
                       avwap=ser[-1]["avwap"], bullFvg=[ser[-1]["bullBot"], ser[-1]["bullTop"]] if ser[-1]["bullTop"] else None,
                       bearFvg=[ser[-1]["bearBot"], ser[-1]["bearTop"]] if ser[-1]["bearTop"] else None,
                       high20=round(max(b["h"] for b in daily[-20:]), 2), lastDaily=daily[-1]["t"][:10])
        out.append(("daily (about 6 months)", chartimg.render(
            daily[k:], f"{sym} DAILY",
            lines=[("EMA21", ORANGE, ema[k:]), ("SMA50", BLUE, sma50[k:]), ("AVWAP", TEAL, [s["avwap"] for s in ser[k:]])],
            levels=levels, zones=_zones(ser, k))))
    if hourly:
        hourly = hourly[-110:]
        out.append(("hourly (about 15 sessions)", chartimg.render(
            hourly, f"{sym} 1 HOUR", lines=[("EMA21", ORANGE, _ema([b["c"] for b in hourly], 21))], levels=levels)))
    return out


def _brief(rec):
    p = rec.get("proposal") or {}
    keep = ("strategy", "contract", "shortContract", "exp", "strike", "shortExp", "shortStrike", "qty", "limit", "debit", "width",
            "maxProfit", "maxLoss", "breakeven", "underlyingStop", "underlyingTarget", "optionStop", "optionTarget")
    return {
        "symbol": rec["symbol"], "trade": {k: p.get(k) for k in keep if p.get(k) is not None},
        "why_flagged": rec.get("origin") or {"type": "manual"},
        "rule_checks": [f"{'PASS' if c['ok'] else 'FAIL'}{'' if c.get('required') else ' (info)'}: {c['text']}" for c in rec.get("checks") or []],
        "signals": rec.get("signals"), "gamma": rec.get("gamma"),
        "chart_legend": "Candles green up / red down, volume at the bottom. Orange = 21 EMA, blue = 50-day SMA, teal = anchored VWAP "
                        "(daily only). Shaded bands = active fair value gaps. Dashed red STOP = invalidation level, dashed green TARGET = "
                        "target level, amber FLIP = gamma flip, blue CALL WALL / purple PUT WALL = gamma walls, dashed gray STRIKE = "
                        "option strike, dashed magenta SHORT = short leg strike.",
    }


def _clean(t):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(t or ""))).strip()


def _items(v):
    """A list of short points. The model sometimes sends one string (JSON text, <li> markup or lines) instead of a list."""
    if isinstance(v, str):
        t = v.strip()
        try:
            parsed = json.loads(t)
            v = parsed if isinstance(parsed, list) else [t]
        except ValueError:
            parts = re.split(r"</?li>|</?ul>|\n+|(?:^|\s)[-•*]\s+", t)
            v = parts if len([x for x in parts if x.strip()]) > 1 else [t]
    out = [_clean(x)[:240] for x in (v or []) if _clean(x)]
    return out[:8]


def run(sub, rec):
    """Draw the charts, ask Claude, store the verdict and the images. Returns the verdict dict."""
    pk = db.upk(sub)
    images = build_charts(rec)
    if not images:
        raise claude.Unavailable("No price data to draw the chart.")
    model = os.environ.get("VISION_MODEL") or os.environ.get("COACH_MODEL") or "claude-sonnet-5"
    text = ("Trade to check (JSON):\n" + json.dumps(_brief(rec), default=str) +
            "\n\nLook at both charts and submit your verdict.")
    out = claude.vision_json(model, SYSTEM, images, text, SCHEMA)
    verdict = {"verdict": out.get("verdict") if out.get("verdict") in ("approve", "caution", "reject") else "caution",
               "confidence": int(out.get("confidence") or 0), "summary": _clean(out.get("summary"))[:600],
               "supports": _items(out.get("supports")), "concerns": _items(out.get("concerns")),
               "model": model, "at": iso(now_ny()), "images": [label for label, _ in images]}
    db.put({"PK": pk, "SK": f"BOTCHART#{rec['id']}", "images": [{"label": l, "b64": base64.b64encode(png).decode()} for l, png in images],
            "createdAt": iso(now_ny())})
    db.update(pk, f"BOT#{rec['id']}", {"aiCheck": verdict, "aiFrom": "", "prevAiCheck": {}})
    rec["aiCheck"] = verdict
    return verdict


def charts(sub, bot_id):
    pk = db.upk(sub)
    it = db.get(pk, f"BOTCHART#{bot_id}")
    if not it:                                   # a re-evaluation shows the charts of the check it carried over
        rec = db.get(pk, f"BOT#{bot_id}") or {}
        src = rec.get("aiFrom") or (rec.get("prevAiCheck") or {}).get("fromId")
        it = db.get(pk, f"BOTCHART#{src}") if src else None
    it = it or {}
    return {"images": it.get("images") or [], "createdAt": it.get("createdAt")}


# ---------------- end-of-day review of open positions ----------------

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["hold", "close"]},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "summary": {"type": "string", "description": "One or two sentences: what the charts show now and the decision."},
        "hold_reasons": {"type": "array", "items": {"type": "string"}},
        "close_reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "confidence", "summary", "hold_reasons", "close_reasons"],
}

REVIEW_SYSTEM = """You are reviewing an OPEN options position of an automated trading bot (paper account) 20 minutes
before the close. Decide: hold it overnight, or close it now. The bot's own rules (option stop, invalidation level,
target, trailing stop, time stop, earnings) still run; you are the discretionary trader's end-of-day look at the chart.

Judge from the charts whether the reason for the trade is still intact:
- Did price lose the structure that justified the entry (back inside / below a range, below the breakout level,
  below the 21 EMA or anchored VWAP, filled the fair value gap it was supposed to hold)?
- Failed breakout, lower highs, distribution (heavy volume on down days), a wide-range reversal or blow-off bar?
- Momentum fading into resistance or the target, with overhead supply making the remaining reward small?
- Time decay: with few days to expiration and no progress, holding is costly.
- Overnight risk: gaps, an event, a weak close near the low of the day.
- Options flow: put_flow_by_day shows puts vs calls bought at the ask on the ticker each day since entry (ratio = puts / calls
  over the last trading days). Puts piling in, especially while price stalls, is a reason for caution.

Close when the chart has turned against the trade or the remaining reward no longer justifies the overnight risk.
Hold when the structure is intact, even if the position is down a little within the plan.

Be consistent with your own recent views on this ticker (listed in the brief). If a recent entry check rejected a NEW
entry here because the setup has broken down, holding the existing position needs a clear reason visible on the chart
now (for example price reclaimed the level since). "Would I open this trade today?" is a fair test: if the answer is a
clear no and the position is already well into its loss, closing is usually right. A large loss close to the option
stop with no sign of recovery is not "within the plan". Name the levels and bars you
see. Keep each list item to one short sentence."""


def _recent_claude(sub, rec, days=5):
    """Claude's recent entry checks and reviews on this ticker, from any record (newest first)."""
    from datetime import timedelta as _td
    cut = iso(now_ny() - _td(days=days))
    out = []
    for r in db.q_prefix(db.upk(sub), "BOT#", desc=True, limit=400):
        if r.get("symbol") != rec["symbol"]:
            continue
        c = r.get("aiCheck") or {}
        if c.get("verdict") and c.get("at", "") >= cut and not r.get("aiFrom"):
            out.append({"kind": "entry check (new entry on this ticker)", "at": c["at"], "verdict": c["verdict"], "confidence": c.get("confidence"),
                        "contract": (r.get("proposal") or {}).get("contract"), "summary": c.get("summary"), "concerns": c.get("concerns")})
        for v in (r.get("aiReviews") or []) if r.get("id") == rec["id"] else []:
            if v.get("at", "") >= cut:
                out.append({"kind": "end-of-day review of this position", "at": v["at"], "verdict": v["action"], "confidence": v.get("confidence"),
                            "summary": v.get("summary")})
        if len(out) >= 8:
            break
    out.sort(key=lambda x: x["at"], reverse=True)
    return out[:6]


def _review_brief(sub, rec, ctx, cfg):
    """What Claude needs to decide now. The entry-day rule results are left out on purpose: they describe the chart as it
    was when the trade was opened, not now, and made Claude lean toward holding."""
    from datetime import datetime
    p = rec.get("proposal") or {}
    days = None
    try:
        days = (now_ny().date() - datetime.strptime((rec.get("filledAt") or rec.get("submittedAt") or rec["createdAt"])[:10], "%Y-%m-%d").date()).days
    except Exception:
        pass
    try:
        dte = (datetime.strptime(p["exp"], "%Y-%m-%d").date() - now_ny().date()).days
    except Exception:
        dte = None
    price = ctx.get("price")
    pct = lambda lvl: round((lvl / price - 1) * 100, 1) if lvl and price else None
    import autotrader
    stop = autotrader.stop_pct(cfg, p)
    keep = ("strategy", "contract", "shortContract", "exp", "strike", "shortExp", "shortStrike", "width", "maxProfit", "maxLoss", "breakeven")
    return {
        "symbol": rec["symbol"], "trade": {k: p.get(k) for k in keep if p.get(k) is not None},
        "now": {"stock_price": price, "ema21": ctx.get("ema21"), "sma50": ctx.get("sma50"), "anchored_vwap": ctx.get("avwap"),
                "active_bull_fvg": ctx.get("bullFvg"), "active_bear_fvg": ctx.get("bearFvg"), "high_last_20_days": ctx.get("high20")},
        "plan": {"invalidation_level": p.get("underlyingStop"), "pct_from_price_to_invalidation": pct(p.get("underlyingStop")),
                 "target_level": p.get("underlyingTarget"), "pct_from_price_to_target": pct(p.get("underlyingTarget")),
                 "option_stop_pct": -stop},
        "position": {"entry_option_price": rec.get("fillPrice"), "option_price_now": rec.get("lastMark"), "pl_pct": rec.get("lastPlPct"),
                     "pl_dollars": rec.get("lastPl"), "peak_option_price": rec.get("peakMark"), "days_held": days, "days_to_expiration": dte,
                     "contracts": rec.get("filledQty") or rec.get("qty"), "why_entered": rec.get("origin") or {"type": "manual"}},
        "recent_claude_views_on_this_ticker": _recent_claude(sub, rec),
        "put_flow_by_day": [{k: x.get(k) for k in ("day", "todayPuts", "todayCalls", "ratio", "bigPuts")} for x in (rec.get("putWatch") or [])[-10:]],
        "chart_legend": "Candles green up / red down, volume at the bottom. Orange = 21 EMA, blue = 50-day SMA, teal = anchored VWAP "
                        "(daily only). Shaded bands = active fair value gaps. Gray NOW = current price, dashed red STOP = invalidation "
                        "level, dashed green TARGET = target, amber FLIP = gamma flip (at entry), blue/purple = call/put wall (at entry), "
                        "dashed gray STRIKE / magenta SHORT = option strikes.",
    }


def review(sub, rec, auto_close=True):
    """Ask Claude hold/close for one open position. Closes it (market) when Claude says close with enough confidence
    and auto-close is on. Returns the review."""
    import autotrader
    cfg = autotrader.settings(sub)
    pk = db.upk(sub)
    ctx = {}
    try:
        autotrader.put_watch(sub, rec, cfg)            # today's put accumulation on the ticker, also shown to Claude
    except Exception as e:
        print("put watch failed", rec.get("symbol"), e)
    images = build_charts(rec, ctx)
    if not images:
        raise claude.Unavailable("No price data to draw the chart.")
    model = os.environ.get("VISION_MODEL") or os.environ.get("COACH_MODEL") or "claude-sonnet-5"
    out = claude.vision_json(model, REVIEW_SYSTEM, images,
                             "Open position (JSON):\n" + json.dumps(_review_brief(sub, rec, ctx, cfg), default=str) + "\n\nHold overnight or close now?",
                             REVIEW_SCHEMA)
    rv = {"action": "close" if out.get("action") == "close" else "hold", "confidence": int(out.get("confidence") or 0),
          "summary": _clean(out.get("summary"))[:600], "supports": _items(out.get("hold_reasons")),
          "concerns": _items(out.get("close_reasons")), "model": model, "at": iso(now_ny()),
          "plPct": rec.get("lastPlPct"), "mark": rec.get("lastMark")}
    weak_hold = (rv["action"] == "hold" and rv["confidence"] < cfg["aiExitHoldMinConf"]
                 and (rec.get("lastPlPct") or 0) <= -cfg["aiExitHoldLossPct"])
    will_close = (rv["action"] == "close" and rv["confidence"] >= cfg["aiExitMinConf"] or weak_hold) and auto_close and cfg["aiExitAutoClose"]
    rv["closed"] = bool(will_close)
    rv["weakHold"] = bool(weak_hold)
    reviews = list(rec.get("aiReviews") or []) + [rv]
    db.put({"PK": pk, "SK": f"BOTREVIEW#{rec['id']}", "images": [{"label": l, "b64": base64.b64encode(png).decode()} for l, png in images],
            "createdAt": iso(now_ny())})
    db.update(pk, f"BOT#{rec['id']}", {"aiReview": rv, "aiReviews": reviews[-20:], "aiReviewRunning": False, "aiReviewError": ""})
    rec.update(aiReview=rv, aiReviews=reviews[-20:])
    if will_close:
        why = (f"Claude end-of-day review: HOLD only {rv['confidence']}% confident with the option down {rec.get('lastPlPct')}%"
               if weak_hold else f"Claude end-of-day review ({rv['confidence']}%): {rv['summary'][:200]}")
        autotrader.close(sub, rec["id"], why)
    elif rv["action"] == "close":
        autotrader._notify(sub, f"Paper bot {autotrader._desc(rec)}: Claude suggests CLOSE ({rv['confidence']}%), not closed "
                                f"({'below the confidence threshold' if auto_close and cfg['aiExitAutoClose'] else 'auto-close off'}). {rv['summary'][:200]}", "ai")
    return rv


def review_all(sub, only_id=None, auto_close=True):
    import autotrader
    cfg = autotrader.settings(sub)
    if not only_id and not cfg["aiExitReview"]:
        return {"skipped": "end-of-day review off"}
    pk = db.upk(sub)
    out = []
    recs = [db.get(pk, f"BOT#{only_id}")] if only_id else db.q_prefix(pk, "BOT#")
    for rec in recs:
        if not rec or rec.get("status") != "open":
            continue
        try:
            rv = review(sub, rec, auto_close)
            out.append({"symbol": rec["symbol"], "action": rv["action"], "confidence": rv["confidence"], "closed": rv["closed"]})
        except Exception as e:
            db.update(pk, f"BOT#{rec['id']}", {"aiReviewRunning": False, "aiReviewError": f"Claude's review failed: {str(e)[:160]}"})
            out.append({"symbol": rec["symbol"], "error": str(e)[:160]})
    return {"reviews": out}


def review_charts(sub, bot_id):
    it = db.get(db.upk(sub), f"BOTREVIEW#{bot_id}") or {}
    return {"images": it.get("images") or [], "createdAt": it.get("createdAt")}
