"""Final visual check before the paper bot places an order: draw the daily and hourly charts with the levels the rules
used (invalidation, target, gamma flip, walls, strikes, fair value gaps, 21 EMA, 50-day, anchored VWAP) and ask Claude
to look at them. Verdict: approve / caution / reject. Stored on the bot record; the images are stored beside it."""
import base64
import json
import os
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


def build_charts(rec):
    sym = rec["symbol"]
    now = now_ny()
    daily = _yahoo(sym.replace(".", "-"), "1d", now - timedelta(days=420), now + timedelta(hours=1))
    out = []
    if daily:
        cl = [b["c"] for b in daily]
        ema, sma50, ser = _ema(cl, 21), _sma(cl, 50), indicators.series(daily)
        k = max(0, len(daily) - 130)
        out.append(("daily (about 6 months)", chartimg.render(
            daily[k:], f"{sym} DAILY",
            lines=[("EMA21", ORANGE, ema[k:]), ("SMA50", BLUE, sma50[k:]), ("AVWAP", TEAL, [s["avwap"] for s in ser[k:]])],
            levels=_levels(rec), zones=_zones(ser, k))))
    hourly = _yahoo(sym.replace(".", "-"), "1h", now - timedelta(days=22), now + timedelta(hours=1))
    if hourly:
        hourly = hourly[-110:]
        out.append(("hourly (about 15 sessions)", chartimg.render(
            hourly, f"{sym} 1 HOUR", lines=[("EMA21", ORANGE, _ema([b["c"] for b in hourly], 21))], levels=_levels(rec))))
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
               "confidence": int(out.get("confidence") or 0), "summary": str(out.get("summary") or "")[:600],
               "supports": [str(x)[:240] for x in (out.get("supports") or [])][:8],
               "concerns": [str(x)[:240] for x in (out.get("concerns") or [])][:8],
               "model": model, "at": iso(now_ny()), "images": [label for label, _ in images]}
    db.put({"PK": pk, "SK": f"BOTCHART#{rec['id']}", "images": [{"label": l, "b64": base64.b64encode(png).decode()} for l, png in images],
            "createdAt": iso(now_ny())})
    db.update(pk, f"BOT#{rec['id']}", {"aiCheck": verdict})
    rec["aiCheck"] = verdict
    return verdict


def charts(sub, bot_id):
    it = db.get(db.upk(sub), f"BOTCHART#{bot_id}") or {}
    return {"images": it.get("images") or [], "createdAt": it.get("createdAt")}
