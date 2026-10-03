"""Paper bot: evaluates a ticker with the trader's own method, picks a call contract, and (optionally) places the
order on the Alpaca PAPER account, then manages the exit.

Structures (setting `strategy`):
  long_call  one call (delta/DTE window below).
  bull_call  bull call spread: buy the long call above, sell a higher-strike call in the same expiration
             (short delta window, strike as close as possible to the target level). Net debit, one multi-leg order.
  diagonal   buy a longer-dated in-the-money call (diag long windows) and sell a call in the closest expiration
             cycle (diag short windows) that pays at least `diagMinShortCredit` $ per contract, strike above the long
             strike and the stock. The short call is rolled every cycle: on its roll day (from 15:30 ET) the bot buys
             it back and sells the next cycle's call in one multi-leg order, so the long call keeps being shorted
             against until a stop, invalidation, earnings or the long call's time stop closes the whole position.
Spreads are sent to Alpaca as one multi-leg (mleg) limit order at the net debit and closed as one mleg order;
if Alpaca rejects the combined close, the short call is bought back first, then the long call is sold.

Decision = transparent rules, no black box:
  Chart (daily): active bull FVG and no bear FVG, close above the 21 EMA with a cross in the last N bars,
                 momentum rising (TTM-squeeze-style momentum), close above the anchored VWAP, not extended.
  Gamma:         price above the gamma flip (preferred), room to the call wall vs distance to the invalidation level.
  Events:        no new entry within `noEntryDays` of earnings (the option is already inflated). Expirations after
                 earnings are allowed: they gain implied volatility into the report, and the bot exits before it.
                 Implied-volatility jumps between expirations flag a likely event even without a date.
  Contract:      expiration in the DTE window, delta in range, liquid (open interest, bid/ask spread),
                 breakeven inside the expected move for that expiration.
  Size:          contracts so that the option stop (stopPct of premium) costs about the risk per trade.
Exits (monitor): option -stopPct%, option +targetPct%, underlying closes below the invalidation level,
                 underlying reaches the target level, days to expiry <= timeStopDte, and on earnings day before the
                 announcement (after-close reports: from 15:30 ET that day; before-open reports: from 15:30 ET the
                 previous trading day) to sell while implied volatility is at its peak and avoid the post-report crush.
"""
import json
import math
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta

import db
import gex
import indicators
from charts import _yahoo
from util import BadRequest, Unavailable, iso, now_ny
from views import load_settings

DEFAULTS = {"enabled": False, "autoSubmit": False, "watchlist": [], "dteMin": 40, "dteMax": 65,
            "deltaMin": 0.50, "deltaMax": 0.70, "minOi": 100, "maxSpreadPct": 12, "stopPct": 40, "targetPct": 80,
            "timeStopDte": 14, "maxPositions": 5, "crossWindow": 10, "maxExtAtr": 1.5, "earnings": {}, "earningsAuto": {}, "noEntryDays": 5,
            "chaseStep": 0.10, "chaseSeconds": 12, "chaseMaxSteps": 5, "chaseMaxPct": 10,
            "minGrowth30": 10,
            "invalidationOnClose": True, "emergencyAtr": 1.0, "trailAfterTarget": True, "trailPct": 25,
            "flowAuto": False, "flowMinPremium": 100000, "flowMinDte": 20, "flowMaxDte": 120, "flowAskSide": True,
            "flowSweeps": False, "flowMinVolOi": 0, "excludeEtfs": True, "flowCooldownMin": 120, "flowMaxEvals": 3,
            "requireAboveFlip": False, "minRoomRatio": 1.5,
            "strategy": "long_call",
            "spreadShortDeltaMin": 0.20, "spreadShortDeltaMax": 0.40, "spreadMaxDebitPct": 70,
            "spreadStopPct": 50, "spreadTargetPct": 70,
            "diagLongDteMin": 60, "diagLongDteMax": 150, "diagLongDeltaMin": 0.65, "diagLongDeltaMax": 0.85,
            "diagShortDteMin": 1, "diagShortDteMax": 21, "diagShortDeltaMin": 0.20, "diagShortDeltaMax": 0.40,
            "diagMinShortCredit": 100, "diagRollDte": 0, "diagRollWaitMin": 3, "diagTargetPct": 0,
            # final visual check by Claude before any order
            "aiCheck": True, "aiAllowCaution": True, "aiBlockOnError": True, "aiMaxAgeMin": 30,
            # Claude's end-of-day review of every open bot position (15:40 ET): hold overnight or close
            "aiExitReview": True, "aiExitAutoClose": True, "aiExitMinConf": 60,
            # a HOLD with low confidence on a losing position also closes it
            "aiExitHoldMinConf": 50, "aiExitHoldLossPct": 25}

STRATEGIES = ("long_call", "bull_call", "diagonal")
SPREADS = ("bull_call", "diagonal")


def is_spread(p):
    return bool(p) and p.get("strategy") in SPREADS and bool(p.get("shortContract"))


def is_diag(p):
    return bool(p) and p.get("strategy") == "diagonal"


def stop_pct(cfg, p):
    return cfg["spreadStopPct"] if p and p.get("strategy") in SPREADS else cfg["stopPct"]


def target_pct(cfg, p, fill=None):
    """Take-profit as % gain on the debit paid. Bull call: spreadTargetPct of the max profit (width - debit)."""
    if is_diag(p):
        return cfg["diagTargetPct"] or None          # 0 = no take-profit: keep rolling the short call
    if not is_spread(p):
        return cfg["targetPct"]
    debit = fill or p.get("debit") or p.get("limit")
    width = p.get("width") or 0
    if not debit or debit <= 0 or width <= debit:
        return cfg["spreadTargetPct"]
    return (width - debit) * cfg["spreadTargetPct"] / 100 / debit * 100


def settings(sub):
    s = load_settings(sub)
    return {**DEFAULTS, **(s.get("autotrade") or {})}


# ---------------- chart signals ----------------

def _ema_series(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for i, x in enumerate(xs):
        if i < n - 1:
            out.append(None)
            continue
        e = sum(xs[:n]) / n if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def _linreg_last(ys):
    n = len(ys)
    xs = range(n)
    mx, my = (n - 1) / 2, sum(ys) / n
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sum((x - mx) ** 2 for x in xs) or 1)
    return my + b * (n - 1 - mx)


def momentum_series(bars, n=20):
    """TTM-squeeze-style momentum: linear regression of close minus the mean of (Donchian mid, SMA)."""
    closes = [b["c"] for b in bars]
    diffs = []
    for i in range(len(bars)):
        if i < n - 1:
            diffs.append(None)
            continue
        w = bars[i - n + 1:i + 1]
        mid = (max(b["h"] for b in w) + min(b["l"] for b in w)) / 2
        sma = sum(closes[i - n + 1:i + 1]) / n
        diffs.append(closes[i] - (mid + sma) / 2)
    out = []
    for i in range(len(diffs)):
        w = diffs[i - n + 1:i + 1] if i >= n - 1 else []
        out.append(_linreg_last(w) if len(w) == n and None not in w else None)
    return out


def chart_signals(symbol, cfg):
    now = now_ny()
    bars = _yahoo(symbol.replace(".", "-"), "1d", now - timedelta(days=420), now + timedelta(hours=1))
    if len(bars) < 60:
        raise BadRequest(f"Not enough price history for {symbol}.")
    closes = [b["c"] for b in bars]
    ema = _ema_series(closes, 21)
    trs = [bars[0]["h"] - bars[0]["l"]] + [max(b["h"], bars[i - 1]["c"]) - min(b["l"], bars[i - 1]["c"]) for i, b in enumerate(bars) if i]
    atr = _ema_series(trs, 21)
    mom = momentum_series(bars)
    last, i = bars[-1], len(bars) - 1
    st = indicators.state_at(bars, i)
    cross_ago = next((k for k in range(0, min(cfg["crossWindow"] + 1, i)) if closes[i - k] > ema[i - k] and closes[i - k - 1] <= ema[i - k - 1]), None)
    ext = (last["c"] - ema[i]) / atr[i] if atr[i] else None
    cut = (datetime.strptime(last["t"][:10], "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    window = [b for b in bars if b["t"][:10] > cut]
    low_bar = min(window, key=lambda b: b["l"]) if window else None
    growth30 = (last["c"] / low_bar["l"] - 1) * 100 if low_bar and low_bar["l"] else None
    return {
        "price": round(last["c"], 2), "asOf": last["t"][:10], "ema21": round(ema[i], 2), "atr21": round(atr[i], 2),
        "extAtr": round(ext, 2) if ext is not None else None, "crossAgo": cross_ago,
        "growth30": round(growth30, 1) if growth30 is not None else None,
        "low30": round(low_bar["l"], 2) if low_bar else None, "low30Date": low_bar["t"][:10] if low_bar else None,
        "momentum": round(mom[i], 3) if mom[i] is not None else None,
        "momentumPrev": round(mom[i - 1], 3) if mom[i - 1] is not None else None,
        "study": st,
        "checks": {
            "bullFvg": bool(st.get("bullFvg")), "noBearFvg": not st.get("bearFvg"),
            "aboveEma": last["c"] > ema[i], "recentCross": cross_ago is not None,
            "momentumUp": mom[i] is not None and mom[i - 1] is not None and mom[i] > mom[i - 1],
            "aboveAvwap": st.get("vsAvwap") == "above", "notExtended": ext is not None and ext <= cfg["maxExtAtr"],
            "growth30": growth30 is not None and growth30 >= cfg["minGrowth30"],
        },
    }


# ---------------- events (earnings) ----------------

def parse_earn(v):
    """'2026-10-28' or '2026-10-28 AMC' / 'BMO' -> (date, timing). Default timing: after the close."""
    if not v:
        return None, None
    parts = str(v).split()
    timing = parts[1].upper() if len(parts) > 1 and parts[1].upper() in ("AMC", "BMO") else "AMC"
    return parts[0], timing


def prev_trading_day(d):
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def earnings_exit_due(earn_value, now=None):
    """True once we're in the exit window before the announcement (or past it)."""
    date, timing = parse_earn(earn_value)
    if not date:
        return False, None
    now = now or now_ny()
    ed = datetime.strptime(date, "%Y-%m-%d").date()
    exit_day = ed if timing == "AMC" else prev_trading_day(ed)
    today = now.date()
    if today > exit_day:
        return True, f"earnings {date} ({'after close' if timing == 'AMC' else 'before open'}) already due"
    if today == exit_day and now.hour * 60 + now.minute >= 15 * 60 + 30:
        return True, f"exit before earnings ({date}, {'after close' if timing == 'AMC' else 'before open'}) to keep the IV run-up"
    return False, None

def iv_events(em_list):
    """Implied-volatility jump between consecutive expirations (forward vol much higher) = likely event before exp."""
    flags = []
    prev = None
    for m in em_list:
        if not m.get("ivAtm") or m["dte"] < 1:
            continue
        T = m["dte"] / 365
        if prev:
            T1, iv1 = prev["dte"] / 365, prev["ivAtm"]
            fwd_var = (m["ivAtm"] ** 2 * T - iv1 ** 2 * T1) / max(T - T1, 1e-6)
            if fwd_var > 0 and math.sqrt(fwd_var) > 1.35 * iv1 and m["ivAtm"] > iv1 * 1.05:
                flags.append({"between": [prev["exp"], m["exp"]], "fwdVol": round(math.sqrt(fwd_var), 3), "ivBefore": iv1, "ivAfter": m["ivAtm"]})
        prev = m
    return flags


# ---------------- contract selection ----------------

def _tick(p):
    step = 0.05 if p < 3 else 0.10
    return round(round(p / step) * step, 2)


def pick_contract(contracts, spot, em_list, cfg, earnings_date, g):
    today = now_ny().date()
    em_by = {m["exp"]: m for m in em_list}
    cands = []
    for c in contracts:
        if not c["call"]:
            continue
        dte = (datetime.strptime(c["exp"], "%Y-%m-%d").date() - today).days
        if not (cfg["dteMin"] <= dte <= cfg["dteMax"]):
            continue
        d = c.get("delta")
        if d is None and c.get("iv"):
            d, _ = gex.bs_greeks(spot, c["strike"], gex._years(c["exp"]), c["iv"], True)
        bid, ask = c.get("bid") or 0, c.get("ask") or 0
        mid = (bid + ask) / 2 if bid and ask else c.get("mark")
        if d is None or not mid or mid <= 0.05:
            continue
        spread = (ask - bid) / mid * 100 if bid and ask else None
        em = em_by.get(c["exp"])
        be = c["strike"] + mid
        reasons = []
        ok = cfg["deltaMin"] <= d <= cfg["deltaMax"]
        if not ok:
            continue
        if (c.get("oi") or 0) < cfg["minOi"]:
            reasons.append(f"open interest {c.get('oi') or 0} < {cfg['minOi']}")
        if spread is None or spread > cfg["maxSpreadPct"]:
            reasons.append("wide bid/ask spread" if spread is not None else "no live quote")
        if em and be > em["upper"]:
            reasons.append("breakeven outside the expected move")
        score = 3 - abs(d - (cfg["deltaMin"] + cfg["deltaMax"]) / 2) * 5 - (spread or 30) / 20 + min(math.log10((c.get("oi") or 1) + 1), 4) / 2
        if em:
            score += (em["upper"] - be) / max(em["move"], 0.01)
        cands.append({"symbol": c["sym"], "exp": c["exp"], "dte": dte, "strike": c["strike"], "delta": round(d, 3),
                      "bid": bid, "ask": ask, "mid": round(mid, 2), "spreadPct": round(spread, 1) if spread is not None else None,
                      "oi": c.get("oi"), "iv": c.get("iv"), "breakeven": round(be, 2),
                      "emUpper": em["upper"] if em else None, "problems": reasons, "score": round(score, 3)})
    cands.sort(key=lambda x: (len(x["problems"]), -x["score"]))
    return cands[:5]


def _leg(c, spot, today):
    """Normalized call quote: dte, delta (Black-Scholes when missing), mid, bid/ask spread %."""
    dte = (datetime.strptime(c["exp"], "%Y-%m-%d").date() - today).days
    d = c.get("delta")
    if d is None and c.get("iv"):
        d, _ = gex.bs_greeks(spot, c["strike"], gex._years(c["exp"]), c["iv"], True)
    bid, ask = c.get("bid") or 0, c.get("ask") or 0
    mid = (bid + ask) / 2 if bid and ask else c.get("mark")
    spread = (ask - bid) / mid * 100 if bid and ask and mid else None
    return {"symbol": c["sym"], "exp": c["exp"], "dte": dte, "strike": c["strike"], "delta": round(d, 3) if d is not None else None,
            "bid": bid, "ask": ask, "mid": round(mid, 2) if mid else None, "spreadPct": round(spread, 1) if spread is not None else None,
            "oi": c.get("oi"), "iv": c.get("iv")}


def pick_spread(contracts, spot, long, cfg, strategy, target_lvl):
    """Short call for a bull call spread (same expiry) or a diagonal (nearer expiry), paired with `long`.
    Returns candidate spreads, best first; each has legs, net debit, width, max profit/loss and problems."""
    today = now_ny().date()
    if strategy == "bull_call":
        dmin, dmax = cfg["spreadShortDeltaMin"], cfg["spreadShortDeltaMax"]
    else:
        dmin, dmax = cfg["diagShortDeltaMin"], cfg["diagShortDeltaMax"]
    out = []
    for c in contracts:
        if not c["call"] or c["strike"] <= long["strike"]:
            continue
        if strategy == "bull_call" and c["exp"] != long["exp"]:
            continue
        s = _leg(c, spot, today)
        if strategy == "diagonal":
            if not (cfg["diagShortDteMin"] <= s["dte"] <= cfg["diagShortDteMax"]) or s["exp"] >= long["exp"] or c["strike"] <= spot:
                continue
        if s["delta"] is None or not s["mid"] or not (dmin <= s["delta"] <= dmax):
            continue
        debit = round(long["mid"] - s["mid"], 2)
        width = round(s["strike"] - long["strike"], 2)
        problems = []
        if (s.get("oi") or 0) < cfg["minOi"]:
            problems.append(f"short call open interest {s.get('oi') or 0} < {cfg['minOi']}")
        if s["spreadPct"] is None or s["spreadPct"] > cfg["maxSpreadPct"]:
            problems.append("short call bid/ask too wide" if s["spreadPct"] is not None else "short call has no live quote")
        if debit <= 0:
            problems.append("no net debit")
        elif strategy == "bull_call" and debit > width * cfg["spreadMaxDebitPct"] / 100:
            problems.append(f"debit {debit:.2f} is {debit / width * 100:.0f}% of the {width:g} width (max {cfg['spreadMaxDebitPct']:g}%)")
        elif strategy == "diagonal" and debit >= width:
            problems.append(f"debit {debit:.2f} ≥ strike width {width:g} (loses on a big rally)")
        if strategy == "diagonal" and s["mid"] * 100 < cfg["diagMinShortCredit"]:
            problems.append(f"short call pays ${s['mid'] * 100:.0f} per contract (min ${cfg['diagMinShortCredit']:g})")
        if strategy == "bull_call":
            score = -abs(s["strike"] - target_lvl) / max(spot * 0.01, 0.01)
            max_profit = round(width - debit, 2)
            breakeven = round(long["strike"] + debit, 2)
        else:
            score = -abs(s["delta"] - (dmin + dmax) / 2) * 10
            max_profit = None
            breakeven = None
        score += min(math.log10((s.get("oi") or 1) + 1), 4) / 2 - (s["spreadPct"] or 30) / 20
        out.append({"strategy": strategy, "long": long, "short": s, "debit": debit, "width": width,
                    "maxProfit": max_profit, "maxLoss": debit, "breakeven": breakeven, "problems": problems, "score": round(score, 3)})
    if strategy == "diagonal":   # closest expiration cycle first
        out.sort(key=lambda x: (len(x["problems"]), x["short"]["exp"], -x["score"]))
    else:
        out.sort(key=lambda x: (len(x["problems"]), -x["score"]))
    return out[:5]


def pick_roll_short(contracts, spot, p, cfg, after_exp):
    """Next short call for a diagonal: closest expiration after `after_exp` (and before the long call), strike above
    the long strike and the stock, delta in the short window, paying at least diagMinShortCredit per contract."""
    today = now_ny().date()
    ok = []
    for c in contracts:
        if not c["call"] or c["exp"] <= after_exp or c["exp"] >= p["exp"] or c["strike"] <= max(p["strike"], spot):
            continue
        s = _leg(c, spot, today)
        if not (max(1, cfg["diagShortDteMin"]) <= s["dte"] <= cfg["diagShortDteMax"]) or s["delta"] is None or not s["mid"]:
            continue
        if not (cfg["diagShortDeltaMin"] <= s["delta"] <= cfg["diagShortDeltaMax"]) or s["mid"] * 100 < cfg["diagMinShortCredit"]:
            continue
        if (s.get("oi") or 0) < cfg["minOi"] or s["spreadPct"] is None or s["spreadPct"] > cfg["maxSpreadPct"]:
            continue
        mid_d = (cfg["diagShortDeltaMin"] + cfg["diagShortDeltaMax"]) / 2
        ok.append((s["exp"], abs(s["delta"] - mid_d), s))
    ok.sort(key=lambda x: (x[0], x[1]))
    return ok[0][2] if ok else None


# ---------------- evaluation ----------------

def earn_for(cfg, symbol):
    """Earnings date used by the rules: one you entered (if not already past), else the one pulled automatically."""
    today = now_ny().date().isoformat()
    manual = (cfg.get("earnings") or {}).get(symbol)
    if manual and parse_earn(manual)[0] >= today:
        return manual, "entered"
    auto = (cfg.get("earningsAuto") or {}).get(symbol) or {}
    if auto.get("v") and parse_earn(auto["v"])[0] >= today:
        return auto["v"], "Unusual Whales"
    return None, None


def refresh_earnings(sub, symbols, max_age_hours=12):
    """Pull the next earnings date of each ticker from Unusual Whales (at most every `max_age_hours`) into the bot
    settings (earningsAuto). Silently does nothing without an Unusual Whales key. Returns the symbols refreshed."""
    import flowdata
    pk = db.upk(sub)
    p = db.get(pk, "PROFILE") or {"PK": pk, "SK": "PROFILE", "createdAt": iso(now_ny()), "settings": {}}
    at = {**DEFAULTS, **((p.get("settings") or {}).get("autotrade") or {})}
    auto = dict(at.get("earningsAuto") or {})
    cut = iso(now_ny() - timedelta(hours=max_age_hours))
    done = []
    for sym in dict.fromkeys(s for s in symbols if s):
        if (auto.get(sym) or {}).get("at", "") >= cut:
            continue
        try:
            v = flowdata.next_earnings(sub, sym)
        except Exception as e:
            print("earnings pull failed", sym, e)
            continue
        if v is None:
            return done                      # no Unusual Whales key
        auto[sym] = {"v": v, "at": iso(now_ny())}
        done.append(sym)
    if done:
        at["earningsAuto"] = auto
        p.setdefault("settings", {})["autotrade"] = at
        db.put(p)
    return done


def set_earnings(sub, symbol, date):
    """Save (or clear with an empty date) the next earnings date for a ticker in the bot settings."""
    import re
    pk = db.upk(sub)
    p = db.get(pk, "PROFILE") or {"PK": pk, "SK": "PROFILE", "createdAt": iso(now_ny()), "settings": {}}
    at = {**DEFAULTS, **((p.get("settings") or {}).get("autotrade") or {})}
    earn = dict(at.get("earnings") or {})
    if date:
        if not re.match(r"^\d{4}-\d{2}-\d{2}( (AMC|BMO))?$", date):
            raise BadRequest("Earnings must be YYYY-MM-DD, optionally followed by AMC or BMO.")
        earn[symbol] = date
    else:
        earn.pop(symbol, None)
    at["earnings"] = earn
    p.setdefault("settings", {})["autotrade"] = at
    db.put(p)


def evaluate(sub, symbol, earnings_date=None, source=None):
    symbol = (symbol or "").strip().upper()
    if not symbol or not symbol.replace(".", "").isalnum() or len(symbol) > 8:
        raise BadRequest("Enter a ticker symbol.")
    if earnings_date is not None:        # your own date (blank clears it; the automatic one still applies)
        set_earnings(sub, symbol, earnings_date.strip())
    refresh_earnings(sub, [symbol])
    cfg = settings(sub)
    base = load_settings(sub)
    strategy = cfg["strategy"] if cfg.get("strategy") in STRATEGIES else "long_call"
    # the long leg: the normal call window, or the longer-dated in-the-money window for a diagonal
    lcfg = cfg if strategy != "diagonal" else {**cfg, "dteMin": cfg["diagLongDteMin"], "dteMax": cfg["diagLongDteMax"],
                                               "deltaMin": cfg["diagLongDeltaMin"], "deltaMax": cfg["diagLongDeltaMax"]}
    sig = chart_signals(symbol, cfg)
    chain = gex.fetch_chain(sub, symbol, max(lcfg["dteMax"] + 10, 90))
    spot = chain["spot"] or sig["price"]
    near = [c for c in chain["contracts"] if c["exp"] <= (now_ny() + timedelta(days=45)).strftime("%Y-%m-%d")]
    g = gex.compute(near or chain["contracts"], spot)
    em = gex.expected_moves(chain["contracts"], spot, limit=16)
    em_list = em["byExpiration"]
    events = iv_events(em_list)
    earnings_raw, earn_src = earn_for(cfg, symbol)
    earnings, earn_timing = parse_earn(earnings_raw)
    cands = pick_contract(chain["contracts"], spot, em_list, lcfg, earnings, g)
    best = cands[0] if cands and not cands[0]["problems"] else None

    # invalidation level: nearest gamma support below price, capped at the expected move of the chosen expiry
    heavy = [b["strike"] for b in g["strikes"] if abs(b["putGex"]) >= 0.25 * max(1, max(abs(x["putGex"]) for x in g["strikes"]))]
    # ignore levels hugging the price: a stop needs at least 0.75 ATR of room or normal noise hits it
    min_gap = 0.75 * sig["atr21"]
    below = [v for v in [g.get("gammaFlip"), g.get("putWall"), sig["ema21"], *heavy] if v and v <= spot - min_gap]
    stop_lvl = max(below) if below else spot - 1.5 * sig["atr21"]
    emx = next((m for m in em_list if best and m["exp"] == best["exp"]), None)
    if emx and stop_lvl < emx["lower"]:
        stop_lvl = emx["lower"]
    target_lvl = g["callWall"] if g["callWall"] > spot else (emx["upper"] if emx else spot + 2 * sig["atr21"])
    room = (target_lvl - spot) / spot * 100
    risk = (spot - stop_lvl) / spot * 100
    ratio = room / risk if risk > 0 else None
    spreads = pick_spread(chain["contracts"], spot, best, cfg, strategy, target_lvl) if best and strategy in SPREADS else []
    spread = spreads[0] if spreads and not spreads[0]["problems"] else None

    c = sig["checks"]
    today = now_ny().date().isoformat()
    from charts import instrument_type
    itype = instrument_type(symbol)
    etf_blocked = cfg["excludeEtfs"] and itype in ("ETF", "MUTUALFUND")
    earn_soon = bool(earnings and today <= earnings <= (now_ny() + timedelta(days=cfg["noEntryDays"])).strftime("%Y-%m-%d"))
    event_in_window = [e for e in events if best and e["between"][1] <= best["exp"]]
    checks = [
        ("chart", "Active bull fair value gap" + (f" ({sig['study']['bullFvg'][0]}–{sig['study']['bullFvg'][1]})" if sig["study"].get("bullFvg") else " (none active)"), c["bullFvg"], True),
        ("chart", "No active bear (negative) fair value gap" + (f": bear gap at {sig['study']['bearFvg'][0]}–{sig['study']['bearFvg'][1]} overhead" if sig["study"].get("bearFvg") else ""), c["noBearFvg"], True),
        ("chart", "Close above the 21 EMA", c["aboveEma"], True),
        ("chart", f"Crossed above the 21 EMA in the last {cfg['crossWindow']} days" + (f" ({sig['crossAgo']} days ago)" if sig["crossAgo"] is not None else ""), c["recentCross"], True),
        ("chart", f"Momentum rising ({sig['momentumPrev']} → {sig['momentum']})", c["momentumUp"], True),
        ("chart", "Close above the anchored VWAP", c["aboveAvwap"], True),
        ("chart", f"Up at least {cfg['minGrowth30']:g}% from the 30-day low (low {sig['low30']} on {sig['low30Date']}, now {sig['growth30']:+.1f}%)" if sig.get("growth30") is not None else "30-day low unavailable",
         c["growth30"] or cfg["minGrowth30"] <= -100, cfg["minGrowth30"] > -100),
        ("chart", f"Not extended ({sig['extAtr']} ATR above the EMA, limit {cfg['maxExtAtr']})", c["notExtended"], True),
        ("gamma", (f"Price {'above' if spot > g['gammaFlip'] else 'below'} the gamma flip ({g['gammaFlip']})" if g.get("gammaFlip")
                   else f"No gamma flip within ±15%: gamma is {g['regime']} across the whole range"),
         (spot > g["gammaFlip"]) if g.get("gammaFlip") else g["regime"] == "positive", cfg["requireAboveFlip"]),
        ("gamma", f"Room {room:.1f}% to target {target_lvl} vs {risk:.1f}% to invalidation {round(stop_lvl, 2)} (ratio {ratio and round(ratio, 1)} : 1, need {cfg['minRoomRatio']})",
         bool(ratio and ratio >= cfg["minRoomRatio"]), True),
        ("events", f"No earnings within {cfg['noEntryDays']} days" + (f" ({earn_src}: {earnings} {'after close' if earn_timing == 'AMC' else 'before open'}; the bot exits before the report)" if earnings else " (no upcoming date found)"), not earn_soon, True),
        ("events", "No implied-volatility jump (likely event) before the chosen expiration" +
         (f": IV jumps between {event_in_window[0]['between'][0]} and {event_in_window[0]['between'][1]}" if event_in_window else ""),
         not event_in_window, False),
        ("events", f"{symbol} is an ETF and ETFs are excluded in the bot settings" if etf_blocked else f"Not an excluded ETF ({itype.lower()})", not etf_blocked, True),
        ("contract", f"Liquid {'long call' if strategy != 'long_call' else 'contract'} found: {symbol} {best['exp']} {best['strike']} call (delta {best['delta']}, mid {best['mid']}, open interest {best['oi']})" if best else (f"Best contract {symbol} {cands[0]['exp']} {cands[0]['strike']} call (delta {cands[0]['delta']}, mid {cands[0]['mid']}, {cands[0]['dte']} days) fails: " + "; ".join(cands[0]["problems"]) if cands else f"No call with delta {lcfg['deltaMin']}-{lcfg['deltaMax']} expiring in {lcfg['dteMin']}-{lcfg['dteMax']} days"), bool(best), True),
    ]
    if strategy in SPREADS and best:
        sw = "same expiration" if strategy == "bull_call" else f"{cfg['diagShortDteMin']}-{cfg['diagShortDteMax']} days"
        dw = (cfg["spreadShortDeltaMin"], cfg["spreadShortDeltaMax"]) if strategy == "bull_call" else (cfg["diagShortDeltaMin"], cfg["diagShortDeltaMax"])
        name = "Bull call spread" if strategy == "bull_call" else "Diagonal"
        if spread:
            txt = (f"{name}: sell {spread['short']['exp']} {spread['short']['strike']} call (delta {spread['short']['delta']}, mid {spread['short']['mid']}), "
                   f"net debit {spread['debit']:.2f}, width {spread['width']:g}" + (f", max profit {spread['maxProfit']:.2f}" if spread['maxProfit'] is not None else ""))
        elif spreads:
            s0 = spreads[0]
            txt = f"{name}: best short call {s0['short']['exp']} {s0['short']['strike']} fails: " + "; ".join(s0["problems"])
        else:
            txt = f"{name}: no short call above {best['strike']} with delta {dw[0]}-{dw[1]} ({sw})"
        checks.append(("contract", txt, bool(spread), True))
    hard_fail = [t for (_, t, ok, hard) in checks if hard and not ok]
    soft_fail = [t for (_, t, ok, hard) in checks if not hard and not ok]
    chart_ok = all(ok for (grp, _, ok, _) in checks if grp == "chart")
    decision = "BUY" if not hard_fail else ("WAIT" if chart_ok or sum(1 for (grp, _, ok, _) in checks if grp == "chart" and not ok) <= 2 else "SKIP")

    proposal = None
    if best and strategy in SPREADS:
        risk_amt = base.get("riskPerTrade") or 200
        if spread:
            debit = spread["debit"]
            per_contract_loss = debit * 100 * cfg["spreadStopPct"] / 100
            qty = int(risk_amt // per_contract_loss) if per_contract_loss else 0
            if base.get("accountSize") and base.get("maxPositionPct"):
                qty = min(qty, int(base["accountSize"] * base["maxPositionPct"] / 100 // (debit * 100)))
            sh = spread["short"]
            proposal = {"strategy": strategy, "contract": best["symbol"], "shortContract": sh["symbol"], "underlying": symbol,
                        "exp": best["exp"], "strike": best["strike"], "shortExp": sh["exp"], "shortStrike": sh["strike"],
                        "legs": [{"symbol": best["symbol"], "side": "buy", "exp": best["exp"], "strike": best["strike"], "mid": best["mid"], "delta": best["delta"]},
                                 {"symbol": sh["symbol"], "side": "sell", "exp": sh["exp"], "strike": sh["strike"], "mid": sh["mid"], "delta": sh["delta"]}],
                        "debit": debit, "width": spread["width"], "maxProfit": spread["maxProfit"], "maxLoss": spread["maxLoss"],
                        "breakeven": spread["breakeven"], "qty": qty, "limit": _tick(debit), "cost": round(qty * debit * 100, 2),
                        "underlyingStop": round(stop_lvl, 2), "underlyingTarget": round(target_lvl, 2), "underlyingAtr": sig["atr21"],
                        "riskAtStop": round(qty * per_contract_loss, 2)}
            proposal["optionStop"] = round(debit * (1 - cfg["spreadStopPct"] / 100), 2)
            tp = target_pct(cfg, proposal)
            proposal["optionTarget"] = round(debit * (1 + tp / 100), 2) if tp else None
            if strategy == "diagonal":
                proposal["shortCredit"] = sh["mid"]
            if qty < 1:
                decision = "SKIP" if decision == "BUY" else decision
                hard_fail.append(f"One spread risks ${per_contract_loss:.0f} at the {cfg['spreadStopPct']:g}% stop, more than your ${risk_amt} risk per trade")
    elif best:
        risk_amt = base.get("riskPerTrade") or 200
        per_contract_loss = best["mid"] * 100 * cfg["stopPct"] / 100
        qty = int(risk_amt // per_contract_loss) if per_contract_loss else 0
        if base.get("accountSize") and base.get("maxPositionPct"):
            cap = base["accountSize"] * base["maxPositionPct"] / 100
            qty = min(qty, int(cap // (best["mid"] * 100)))
        proposal = {"strategy": "long_call", "contract": best["symbol"], "underlying": symbol, "exp": best["exp"], "strike": best["strike"],
                    "qty": qty, "limit": _tick(best["mid"]), "cost": round(qty * best["mid"] * 100, 2),
                    "optionStop": round(best["mid"] * (1 - cfg["stopPct"] / 100), 2),
                    "optionTarget": round(best["mid"] * (1 + cfg["targetPct"] / 100), 2),
                    "underlyingStop": round(stop_lvl, 2), "underlyingTarget": round(target_lvl, 2), "underlyingAtr": sig["atr21"],
                    "riskAtStop": round(qty * per_contract_loss, 2)}
        if qty < 1:
            decision = "SKIP" if decision == "BUY" else decision
            hard_fail.append(f"One contract risks ${per_contract_loss:.0f} at the {cfg['stopPct']}% stop, more than your ${risk_amt} risk per trade")

    rec = {"id": now_ny().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6], "symbol": symbol, "createdAt": iso(now_ny()),
           "decision": decision, "checks": [{"group": grp, "text": t, "ok": ok, "required": hard} for (grp, t, ok, hard) in checks],
           "blocking": hard_fail, "warnings": soft_fail, "proposal": proposal, "candidates": cands, "strategy": strategy,
           "spreadCandidates": [{"exp": x["short"]["exp"], "dte": x["short"]["dte"], "strike": x["short"]["strike"], "delta": x["short"]["delta"],
                                 "mid": x["short"]["mid"], "debit": x["debit"], "width": x["width"], "maxProfit": x["maxProfit"],
                                 "problems": x["problems"]} for x in spreads],
           "signals": {k: sig.get(k) for k in ("price", "asOf", "ema21", "atr21", "extAtr", "crossAgo", "momentum", "momentumPrev", "growth30", "low30", "low30Date")},
           "gamma": {k: g.get(k) for k in ("gammaFlip", "callWall", "putWall", "netGex", "regime")},
           "events": events, "source": chain["source"], "status": "proposed", "origin": source or {"type": "manual"}}
    _carry_ai(sub, rec, cfg)
    db.put({"PK": db.upk(sub), "SK": f"BOT#{rec['id']}", **rec})
    return rec


def _carry_ai(sub, rec, cfg):
    """A re-evaluation creates a new record: keep Claude's last chart check for this ticker with it.
    Same contract and still fresh -> it counts for placing the order (aiCheck). Otherwise it is shown as the previous
    check (prevAiCheck) and Claude looks again before an order."""
    p = rec.get("proposal") or {}
    for old in db.q_prefix(db.upk(sub), "BOT#", desc=True, limit=300):
        if old.get("symbol") != rec["symbol"] or not old.get("aiCheck") or old.get("id") == rec["id"]:
            continue
        chk = {**old["aiCheck"], "fromId": old.get("aiFrom") or old["id"]}
        same = (old.get("proposal") or {}).get("contract") == p.get("contract") and p.get("contract")
        fresh = chk.get("at", "") >= iso(now_ny() - timedelta(minutes=cfg["aiMaxAgeMin"]))
        if same and fresh:
            rec["aiCheck"], rec["aiFrom"] = chk, chk["fromId"]
        else:
            rec["prevAiCheck"] = {**chk, "contract": (old.get("proposal") or {}).get("contract")}
        return


# ---------------- Alpaca paper orders ----------------

def _paper_creds(sub):
    import alpaca
    c = alpaca.creds(sub)
    if not c:
        raise BadRequest("Connect Alpaca (paper) in Settings → Brokers first.")
    if c.get("env") != "paper":
        raise BadRequest("The bot only trades the Alpaca PAPER account. Reconnect Alpaca with paper keys.")
    return c


def _alp(c, method, path, body=None):
    url = "https://paper-api.alpaca.markets" + path
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise BadRequest(f"Alpaca refused the request ({e.code}): {e.read().decode(errors='replace')[:200]}")
    except urllib.error.URLError as e:
        raise Unavailable(f"Alpaca couldn't be reached: {e.reason}")


def _desc(rec):
    p = rec.get("proposal") or {}
    d = lambda e: (e or "")[5:].replace("-", "/")
    if is_spread(p) and p["strategy"] == "bull_call":
        return f"{rec.get('symbol')} {d(p.get('exp'))} {p.get('strike'):g}/{p.get('shortStrike'):g}C spread"
    if is_spread(p):
        return f"{rec.get('symbol')} diagonal {d(p.get('exp'))} {p.get('strike'):g}C / {d(p.get('shortExp'))} {p.get('shortStrike'):g}C"
    if is_diag(p):
        return f"{rec.get('symbol')} diagonal {d(p.get('exp'))} {p.get('strike'):g}C (no short call)"
    return f"{rec.get('symbol')} {d(p.get('exp'))} {p.get('strike')}C"


def _entry_body(rec, qty, limit, cid):
    """Opening order: one limit buy for a call, or one multi-leg limit order at the net debit for a spread
    (Alpaca mleg: a positive limit_price is a debit)."""
    p = rec["proposal"]
    if is_spread(p):
        return {"order_class": "mleg", "qty": str(qty), "type": "limit", "limit_price": f"{limit:.2f}", "time_in_force": "day",
                "client_order_id": cid,
                "legs": [{"symbol": p["contract"], "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
                         {"symbol": p["shortContract"], "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"}]}
    return {"symbol": p["contract"], "qty": str(qty), "side": "buy", "type": "limit", "limit_price": f"{limit:.2f}",
            "time_in_force": "day", "client_order_id": cid}


def _notify(sub, text, kind):
    try:
        import notify
        notify.send(sub, text, kind)
    except Exception as e:
        print("notify failed", e)


def ai_gate(sub, rec, cfg, inline, override=False):
    """Claude's chart check before an order. inline=True (bot runs) draws the charts and asks Claude now;
    from the web app the check runs first in the background (POST /bot/{id}/aicheck) and is read here."""
    if not cfg["aiCheck"] or override:
        return
    chk = rec.get("aiCheck")
    fresh = chk and chk.get("at") and chk["at"] >= iso(now_ny() - timedelta(minutes=cfg["aiMaxAgeMin"]))
    if not fresh:
        if not inline:
            raise BadRequest("Run Claude's chart check first (it takes about 20 seconds).")
        import aicheck
        try:
            chk = aicheck.run(sub, rec)
        except Exception as e:
            note = f"Claude's chart check failed: {str(e)[:160]}"
            db.update(db.upk(sub), rec["SK"], {"aiError": note})
            if cfg["aiBlockOnError"]:
                _notify(sub, f"Paper bot NOT placed {_desc(rec)}: {note}", "ai")
                raise BadRequest(note)
            return
    v = chk["verdict"]
    if v == "reject" or (v == "caution" and not cfg["aiAllowCaution"]):
        db.update(db.upk(sub), rec["SK"], {"aiBlocked": True})
        _notify(sub, f"Paper bot NOT placed {_desc(rec)}: Claude {v.upper()} ({chk.get('confidence')}%) {chk.get('summary', '')}", "ai")
        raise BadRequest(f"Claude's chart check: {v} — {chk.get('summary', '')}")


def place(sub, bot_id, qty=None, limit=None, placed_by="manual", ai_inline=True, override=False):
    c = _paper_creds(sub)
    pk = db.upk(sub)
    rec = db.get(pk, f"BOT#{bot_id}")
    if not rec or not rec.get("proposal"):
        raise BadRequest("Proposal not found.")
    if rec.get("status") not in ("proposed",):
        raise BadRequest(f"This proposal is already {rec.get('status')}.")
    p = rec["proposal"]
    q = int(qty or p["qty"])
    if q < 1:
        raise BadRequest("Quantity must be at least 1.")
    lim = _tick(float(limit or p["limit"]))
    # 1) no second bot position or working order on the same underlying (our records)
    for other in db.q_prefix(pk, "BOT#"):
        if other.get("id") != bot_id and other.get("symbol") == rec["symbol"] and other.get("status") in ("submitting", "submitted", "open", "closing"):
            raise BadRequest(f"Already have a {other['status']} bot trade on {rec['symbol']} ({_desc(other)}). Close it first.")
    # 2) broker truth: no existing position or open buy order on this underlying at Alpaca
    root = rec["symbol"].upper()
    same = lambda sym: sym and sym.upper().startswith(root) and sym[len(root):len(root) + 1].isdigit()
    for pos in (_alp(c, "GET", "/v2/positions") or []):
        if same(pos.get("symbol")):
            raise BadRequest(f"Alpaca already holds {pos['symbol']} ({pos.get('qty')}). Not placing a second order.")
    for o in (_alp(c, "GET", "/v2/orders?status=open&limit=200&nested=true") or []):
        legs = [o] + list(o.get("legs") or [])
        hit = next((x for x in legs if same(x.get("symbol")) and (x.get("side") == "buy" or o.get("order_class") == "mleg")), None)
        if hit:
            raise BadRequest(f"Alpaca already has an open order for {hit['symbol']}. Not placing a second order.")
    ai_gate(sub, rec, settings(sub), ai_inline, override)
    # 3) atomic claim so two clicks / two runs can't both submit
    if not db.claim(pk, f"BOT#{bot_id}", ["proposed"], "submitting"):
        raise BadRequest("This proposal is already being submitted.")
    try:
        order = _alp(c, "POST", "/v2/orders", _entry_body(rec, q, lim, f"tj-{bot_id}-0"))   # Alpaca rejects a reused id
    except Exception:
        db.update(pk, f"BOT#{bot_id}", {"status": "proposed"})
        raise
    db.update(pk, f"BOT#{bot_id}", {"status": "submitted", "orderId": order.get("id"), "qty": q, "limit": lim,
                                    "firstLimit": lim, "chaseSteps": 0, "submittedAt": iso(now_ny()), "placedBy": placed_by})
    origin = rec.get("origin") or {}
    why = f" | flow {origin.get('premium', 0) / 1000:.0f}K {'sweep' if origin.get('sweep') else ''}".rstrip() if origin.get("type") == "flow" else ""
    _notify(sub, f"Paper bot BUY {_desc(rec)} x{q} limit {lim:.2f} ({'auto' if placed_by != 'manual' else 'manual'}){why}", "entry")
    return db.get(pk, f"BOT#{bot_id}")


def _order_state(c, oid):
    o = _alp(c, "GET", f"/v2/orders/{oid}") or {}
    return o.get("status"), float(o.get("filled_qty") or 0), abs(float(o.get("filled_avg_price") or 0))   # mleg: net price


def chase(sub, bot_id):
    """Walk an unfilled buy limit up by `chaseStep` every `chaseSeconds` (cancel + resubmit the remaining quantity)
    until filled, `chaseMaxSteps` is reached, or the price would exceed the first limit by more than `chaseMaxPct`."""
    cfg = settings(sub)
    c = _paper_creds(sub)
    pk = db.upk(sub)
    rec = db.get(pk, f"BOT#{bot_id}")
    if not rec or rec.get("status") != "submitted" or not rec.get("orderId"):
        return rec
    first = rec.get("firstLimit") or rec["limit"]
    cap = first * (1 + cfg["chaseMaxPct"] / 100)
    limit, oid = rec["limit"], rec["orderId"]
    done_qty, done_cost = rec.get("prevFilledQty", 0), rec.get("prevFilledCost", 0)
    total = rec["qty"]
    steps = rec.get("chaseSteps", 0)
    while steps < cfg["chaseMaxSteps"]:
        time.sleep(cfg["chaseSeconds"])
        st, fq, fp = _order_state(c, oid)
        if st == "filled":
            break
        if st in ("canceled", "expired", "rejected"):
            db.update(pk, f"BOT#{bot_id}", {"status": "not filled", "exitReason": f"order {st}"})
            return db.get(pk, f"BOT#{bot_id}")
        new_limit = _tick(limit + cfg["chaseStep"])
        if new_limit > cap + 1e-9:
            db.update(pk, f"BOT#{bot_id}", {"chaseNote": f"stopped chasing at {limit:.2f} (cap {cap:.2f})"})
            return db.get(pk, f"BOT#{bot_id}")
        _alp(c, "DELETE", f"/v2/orders/{oid}")
        for _ in range(10):                         # wait for the cancel to settle
            st, fq, fp = _order_state(c, oid)
            if st in ("canceled", "filled", "expired", "rejected"):
                break
            time.sleep(0.5)
        if st == "filled":
            break
        done_qty += fq
        done_cost += fq * fp
        remaining = int(round(total - done_qty))
        if remaining <= 0:
            break
        o = _alp(c, "POST", "/v2/orders", _entry_body(rec, remaining, new_limit, f"tj-{bot_id}-{steps + 1}"))
        steps += 1
        oid, limit = o.get("id"), new_limit
        db.update(pk, f"BOT#{bot_id}", {"orderId": oid, "limit": limit, "chaseSteps": steps,
                                        "prevFilledQty": done_qty, "prevFilledCost": done_cost})
    for _ in range(6):                               # the last order often fills a few seconds later
        st, fq, fp = _order_state(c, oid)
        if st != "new" and st != "accepted" and st != "pending_new":
            break
        time.sleep(5)
    if st == "filled":
        q = done_qty + fq
        avg = (done_cost + fq * fp) / q if q else fp
        db.update(pk, f"BOT#{bot_id}", {"status": "open", "fillPrice": round(avg, 4), "filledQty": q, "filledAt": iso(now_ny())})
        _notify(sub, f"Paper bot FILLED {_desc(rec)} x{q:g} @ {avg:.2f}", "fill")
    return db.get(pk, f"BOT#{bot_id}")


def close(sub, bot_id, reason="manual"):
    c = _paper_creds(sub)
    pk = db.upk(sub)
    rec = db.get(pk, f"BOT#{bot_id}")
    if not rec:
        raise BadRequest("Bot trade not found.")
    if rec.get("status") not in ("submitted", "open"):
        return rec                                        # already closing/closed: never send a second exit
    if rec.get("status") == "submitted" and rec.get("orderId"):
        o = _alp(c, "GET", f"/v2/orders/{rec['orderId']}")
        if o and o.get("status") in ("new", "accepted", "partially_filled", "pending_new"):
            _alp(c, "DELETE", f"/v2/orders/{rec['orderId']}")
            if not float(o.get("filled_qty") or 0):
                db.update(pk, f"BOT#{bot_id}", {"status": "cancelled", "exitReason": reason, "closedAt": iso(now_ny())})
                _notify(sub, f"Paper bot CANCELLED unfilled order {_desc(rec)} ({reason})", "cancel")
                return db.get(pk, f"BOT#{bot_id}")
    if not db.claim(pk, f"BOT#{bot_id}", ["open"], "closing"):
        return db.get(pk, f"BOT#{bot_id}")
    p = rec["proposal"]
    if rec.get("rollOrderId"):
        try:
            _alp(c, "DELETE", f"/v2/orders/{rec['rollOrderId']}")
        except Exception as e:
            print("roll cancel failed", bot_id, e)
        db.update(pk, f"BOT#{bot_id}", {"rollOrderId": ""})
    if is_diag(p):
        stock = _alp(c, "GET", f"/v2/positions/{rec['symbol']}")
        if stock and float(stock.get("qty") or 0) != 0:          # short call was assigned: flatten the shares too
            _alp(c, "DELETE", f"/v2/positions/{rec['symbol']}")
    if is_spread(p):
        try:
            exit_id, leg_ids = _close_spread(c, bot_id, p)
        except Exception:
            db.update(pk, f"BOT#{bot_id}", {"status": "open"})   # nothing was sent: the next check tries again
            raise
        db.update(pk, f"BOT#{bot_id}", {"status": "closing", "exitReason": reason, "exitOrderId": exit_id,
                                        "exitLegOrderIds": leg_ids, "closedAt": iso(now_ny())})
    else:
        res = _alp(c, "DELETE", f"/v2/positions/{p['contract']}")
        db.update(pk, f"BOT#{bot_id}", {"status": "closing", "exitReason": reason, "exitOrderId": (res or {}).get("id"),
                                        "closedAt": iso(now_ny())})
    pl = rec.get("lastPl")
    _notify(sub, f"Paper bot SELL {_desc(rec)} x{(rec.get('filledQty') or rec.get('qty') or 0):g} at market: {reason}"
                 + (f" (P&L ~{'+' if pl >= 0 else '-'}${abs(pl):,.0f})" if pl is not None else ""), "exit")
    return db.get(pk, f"BOT#{bot_id}")


def _close_spread(c, bot_id, p):
    """Close both legs as one multi-leg market order. If Alpaca refuses it (or one leg is already gone),
    buy back the short call first so the account is never left short a naked call, then sell the long call.
    Returns (combined order id or None, [leg order ids])."""
    lp = _alp(c, "GET", f"/v2/positions/{p['contract']}")
    sp = _alp(c, "GET", f"/v2/positions/{p['shortContract']}")
    if lp and sp:
        qty = int(min(abs(float(lp.get("qty") or 0)), abs(float(sp.get("qty") or 0))))
        if qty >= 1 and abs(float(lp.get("qty") or 0)) == abs(float(sp.get("qty") or 0)):
            try:
                o = _alp(c, "POST", "/v2/orders", {
                    "order_class": "mleg", "qty": str(qty), "type": "market", "time_in_force": "day",
                    "client_order_id": f"tj-{bot_id}-x{int(time.time()) % 100000}",
                    "legs": [{"symbol": p["contract"], "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"},
                             {"symbol": p["shortContract"], "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"}]})
                return (o or {}).get("id"), []
            except BadRequest as e:
                print("mleg close refused, closing leg by leg", bot_id, e)
    ids = []
    if sp:
        ids.append((_alp(c, "DELETE", f"/v2/positions/{p['shortContract']}") or {}).get("id"))
    if lp:
        ids.append((_alp(c, "DELETE", f"/v2/positions/{p['contract']}") or {}).get("id"))
    return None, ids


# ---------------- monitoring ----------------

def _refresh(sub, c, rec):
    """Bring one bot record up to date with Alpaca (fill, live value, exit fill). Returns (rec, position)."""
    pk = db.upk(sub)
    st, p = rec.get("status"), rec["proposal"]
    if st == "submitted" and rec.get("orderId"):
        o = _alp(c, "GET", f"/v2/orders/{rec['orderId']}")
        if o and o.get("status") == "filled":
            fq, fp = float(o.get("filled_qty") or 0), abs(float(o.get("filled_avg_price") or 0))   # mleg: net debit
            q = rec.get("prevFilledQty", 0) + fq
            avg = (rec.get("prevFilledCost", 0) + fq * fp) / q if q else fp
            db.update(pk, rec["SK"], {"status": "open", "fillPrice": round(avg, 4), "filledQty": q, "filledAt": o.get("filled_at")})
            rec.update(status="open", fillPrice=round(avg, 4), filledQty=q)
            _notify(sub, f"Paper bot FILLED {_desc(rec)} x{q:g} @ {avg:.2f}", "fill")
        elif o and o.get("status") in ("canceled", "expired", "rejected") and not rec.get("prevFilledQty"):
            db.update(pk, rec["SK"], {"status": "not filled", "exitReason": o.get("status")})
            rec["status"] = "not filled"
            return rec, None
        else:
            return rec, None
    if rec["status"] not in ("open", "closing"):
        return rec, None
    spread, diag = is_spread(p), is_diag(p)
    if spread or diag:
        lp = _alp(c, "GET", f"/v2/positions/{p['contract']}")
        sp = _alp(c, "GET", f"/v2/positions/{p['shortContract']}") if p.get("shortContract") else None
        pos = {"long": lp, "short": sp} if (lp or sp) else None
    else:
        pos = _alp(c, "GET", f"/v2/positions/{p['contract']}")
    if not pos:
        upd = {"status": "closed"}
        if rec.get("exitOrderId"):
            o = _alp(c, "GET", f"/v2/orders/{rec['exitOrderId']}") or {}
            if o.get("filled_avg_price"):
                upd["exitPrice"] = abs(float(o["filled_avg_price"])) + (rec.get("cashAdj") or 0 if diag else 0)   # net, after rolls
        exit_px = upd.get("exitPrice") or rec.get("lastMark")
        qty = rec.get("filledQty") or rec.get("qty") or 0
        if not rec.get("riskAtFill") and rec.get("fillPrice"):
            upd.update(_risk_at_fill(sub, rec, rec["fillPrice"]))
        if exit_px is not None and rec.get("fillPrice"):
            upd["realizedPl"] = round((exit_px - rec["fillPrice"]) * qty * 100, 2)
            upd["realizedPct"] = round((exit_px / rec["fillPrice"] - 1) * 100, 1)
            risk = upd.get("riskAtFill") or rec.get("riskAtFill")
            if risk:
                upd["realizedR"] = round(upd["realizedPl"] / risk, 2)
        if rec["status"] == "open" and not rec.get("exitReason"):
            upd["exitReason"] = "closed outside the bot"
        db.update(pk, rec["SK"], upd)
        rec.update(upd)
        rp = upd.get("realizedPl")
        _notify(sub, f"Paper bot CLOSED {_desc(rec)}" + (f" @ {upd['exitPrice']:.2f}" if upd.get("exitPrice") else "")
                     + (f": {'+' if rp >= 0 else '-'}${abs(rp):,.0f} ({upd.get('realizedPct', 0):+.1f}%)" if rp is not None else "")
                     + f" | {rec.get('exitReason') or ''}", "closed")
        return rec, None
    if spread and not (lp and sp) and not (diag and lp and rec.get("rollOrderId")):   # mid-roll: the fill is handled by the roll
        # one leg is gone: the short call was assigned/exercised or a leg was closed outside the bot
        missing = p["contract"] if not lp else p["shortContract"]
        db.update(pk, rec["SK"], {"legMissing": missing, "lastCheck": iso(now_ny())})
        rec["legMissing"] = missing
        return rec, pos
    if diag and not lp:
        missing = p["contract"]
        db.update(pk, rec["SK"], {"legMissing": missing, "lastCheck": iso(now_ny())})
        rec["legMissing"] = missing
        return rec, pos
    if spread or diag:
        # value per spread = long - short (+ net cash from diagonal rolls since entry), compared with the entry debit
        f = lambda x, k: float((x or {}).get(k) or 0)
        adj = (rec.get("cashAdj") or 0) if diag else 0
        mark = round(f(lp, "current_price") - abs(f(sp, "current_price")) + adj, 4)
        fill = rec.get("fillPrice") or (round(abs(f(lp, "avg_entry_price")) - abs(f(sp, "avg_entry_price")), 4) if sp else 0)
        qty = rec.get("filledQty") or rec.get("qty") or 0
        upd = {"lastMark": mark, "lastPlPct": round((mark / fill - 1) * 100, 1) if fill and fill > 0 else 0.0,
               "lastPl": round((mark - fill) * qty * 100, 2) if fill else round(f(lp, "unrealized_pl") + f(sp, "unrealized_pl"), 2),
               "marketValue": round(f(lp, "market_value") + f(sp, "market_value"), 2), "lastCheck": iso(now_ny())}
        if not rec.get("fillPrice") and fill > 0:
            upd["fillPrice"] = fill
    else:
        mark = float(pos.get("current_price") or 0)
        upd = {"lastMark": mark, "lastPlPct": round(float(pos.get("unrealized_plpc") or 0) * 100, 1),
               "lastPl": round(float(pos.get("unrealized_pl") or 0), 2), "marketValue": round(float(pos.get("market_value") or 0), 2),
               "lastCheck": iso(now_ny())}
        if not rec.get("fillPrice") and pos.get("avg_entry_price"):
            upd["fillPrice"] = float(pos["avg_entry_price"])
    snaps = list(rec.get("marks") or [])
    if not snaps or (datetime.strptime(snaps[-1]["t"], "%Y-%m-%dT%H:%M:%S") <= now_ny() - timedelta(minutes=14)):
        snaps.append({"t": iso(now_ny()), "mark": mark, "plPct": upd["lastPlPct"]})
        while len(snaps) > 600:          # keep the whole trade: thin the older part (every other point), keep the last 200 as is
            snaps = snaps[:-200][::2] + snaps[-200:]
        upd["marks"] = snaps
    fill = upd.get("fillPrice") or rec.get("fillPrice")
    if fill and not rec.get("riskAtFill"):
        upd.update(_risk_at_fill(sub, rec, fill))
    db.update(pk, rec["SK"], upd)
    rec.update(upd)
    return rec, pos


def sync(sub):
    """Quick status refresh for the page (no exit decisions)."""
    try:
        c = _paper_creds(sub)
    except BadRequest:
        return
    for rec in db.q_prefix(db.upk(sub), "BOT#"):
        if rec.get("status") in ("submitted", "open", "closing"):
            try:
                _refresh(sub, c, rec)
            except Exception as e:
                print("sync failed", rec.get("id"), e)


def summary(sub):
    items = db.q_prefix(db.upk(sub), "BOT#")
    closed = [r for r in items if r.get("status") == "closed" and r.get("realizedPl") is not None]
    opn = [r for r in items if r.get("status") == "open"]
    wins = [r for r in closed if r["realizedPl"] > 0]
    out = {"realized": round(sum(r["realizedPl"] for r in closed), 2), "closedTrades": len(closed), "wins": len(wins),
           "winRate": round(len(wins) / len(closed) * 100) if closed else None,
           "avgReturnPct": round(sum(r.get("realizedPct") or 0 for r in closed) / len(closed), 1) if closed else None,
           "unrealized": round(sum(r.get("lastPl") or 0 for r in opn), 2), "openPositions": len(opn),
           "capitalInOpen": round(sum(r.get("marketValue") or 0 for r in opn), 2)}
    out["total"] = round(out["realized"] + out["unrealized"], 2)
    try:
        c = _paper_creds(sub)
        acct = _alp(c, "GET", "/v2/account") or {}
        eq, last = float(acct.get("equity") or 0), float(acct.get("last_equity") or 0)
        out["account"] = {"equity": eq, "dayChange": round(eq - last, 2), "cash": float(acct.get("cash") or 0),
                          "buyingPower": float(acct.get("options_buying_power") or acct.get("buying_power") or 0)}
        h = _alp(c, "GET", "/v2/account/portfolio/history?period=3M&timeframe=1D") or {}
        out["equityHistory"] = [{"t": datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"), "equity": e}
                                for ts, e in zip(h.get("timestamp") or [], h.get("equity") or []) if e is not None]
    except Exception as e:
        out["accountError"] = str(e)[:160]
    return out


def _risk_at_fill(sub, rec, fill):
    """Dollars at risk when the order filled: actual filled quantity x fill price x the stop % in force then.
    Results are measured in R = P&L / this amount."""
    cfg = settings(sub)
    p = rec.get("proposal") or {}
    qty = rec.get("filledQty") or rec.get("qty") or (p.get("qty") or 0)
    stp = stop_pct(cfg, p)
    risk = round(float(fill) * float(qty) * 100 * stp / 100, 2)
    return {"riskAtFill": risk, "stopPctAtFill": stp} if risk > 0 else {}


def _excursions(rec, under):
    """Best and worst seen during the trade (checked every minute): option price, P&L %, and the stock price.
    Only the changed fields are returned, so nothing grows over time."""
    upd, now = {}, iso(now_ny())
    mark, pct = rec.get("lastMark"), rec.get("lastPlPct")
    for key, v, hi in (("optMax", mark, True), ("optMin", mark, False), ("plMax", pct, True), ("plMin", pct, False),
                       ("undMax", under, True), ("undMin", under, False)):
        if v is None:
            continue
        cur = rec.get(key)
        if cur is None or (v > cur if hi else v < cur):
            upd[key], upd[key + "At"] = v, now
    return upd


def monitor(sub):
    held = [r["symbol"] for r in db.q_prefix(db.upk(sub), "BOT#") if r.get("status") in ("submitted", "open", "closing")]
    if held:
        refresh_earnings(sub, held)          # existing positions keep an up-to-date earnings date (pulled at most every 12 h)
    cfg = settings(sub)
    pk = db.upk(sub)
    try:
        c = _paper_creds(sub)
    except BadRequest:
        return {"skipped": "no paper account"}
    actions = []
    for rec in db.q_prefix(pk, "BOT#"):
        if rec.get("status") not in ("submitted", "open", "closing"):
            continue
        rec, pos = _refresh(sub, c, rec)
        if rec.get("status") != "open" or not pos:
            continue
        p = rec["proposal"]
        if rec.get("legMissing") and is_diag(p) and rec["legMissing"] == p.get("shortContract"):
            stock = _alp(c, "GET", f"/v2/positions/{rec['symbol']}")
            if not (stock and float(stock.get("qty") or 0) != 0):
                # short call expired worthless or was bought back: keep the long call and sell the next cycle
                note = f"short {p['shortContract']} no longer held (expired or bought back): selling the next cycle"
                p = {**p, "shortContract": None, "shortExp": None, "shortStrike": None}
                db.update(pk, rec["SK"], {"proposal": p, "legMissing": ""})
                rec.update(proposal=p, legMissing="")
                _notify(sub, f"Paper bot {_desc(rec)}: {note}", "roll")
                actions.append((rec["symbol"], note))
        if rec.get("legMissing"):
            reason = f"{rec['legMissing']} is no longer held (assigned or closed outside the bot): closing the other leg"
            close(sub, rec["id"], reason)
            actions.append((rec["symbol"], reason))
            continue
        pl_pct = rec["lastPlPct"]
        stp, tgt = stop_pct(cfg, p), target_pct(cfg, p, rec.get("fillPrice"))
        try:
            bars = _yahoo(rec["symbol"].replace(".", "-"), "5m", now_ny() - timedelta(days=3), now_ny() + timedelta(hours=1))
            under = bars[-1]["c"] if bars else None
        except Exception:
            under = None
        exc = _excursions(rec, under)
        if under is not None:
            exc["lastUnderlying"] = under
        if exc:
            db.update(pk, rec["SK"], exc)
            rec.update(exc)
        dte = (datetime.strptime(p["exp"], "%Y-%m-%d").date() - now_ny().date()).days
        due, earn_reason = earnings_exit_due(earn_for(cfg, rec["symbol"])[0])
        reason = None
        mark = rec.get("lastMark") or 0
        now = now_ny()
        near_close = now.hour * 60 + now.minute >= 15 * 60 + 50
        atr = p.get("underlyingAtr") or 0
        emergency = p["underlyingStop"] - cfg["emergencyAtr"] * atr if atr else None
        # trailing stop once the target was reached: let winners run, exit on a pullback from the best price
        peak = max(rec.get("peakMark") or 0, mark)
        trailing = rec.get("trailing") or False
        profit_exits = tgt is not None        # diagonal with no take-profit: only stops/invalidation/time close it
        if not trailing and profit_exits and cfg["trailAfterTarget"] and (pl_pct >= tgt or (under is not None and under >= p["underlyingTarget"])):
            trailing = True
        if peak != rec.get("peakMark") or trailing != rec.get("trailing"):
            db.update(pk, rec["SK"], {"peakMark": peak, "trailing": trailing})
        if pl_pct <= -stp:
            reason = f"{'spread' if is_spread(p) else 'option'} stop ({pl_pct:.0f}%)"
        elif under is not None and under < p["underlyingStop"] and (not cfg["invalidationOnClose"] or near_close):
            reason = f"{rec['symbol']} {'closing' if cfg['invalidationOnClose'] else 'trading'} below invalidation {p['underlyingStop']} ({under:.2f})"
        elif under is not None and emergency is not None and under < emergency:
            reason = f"{rec['symbol']} fell {cfg['emergencyAtr']} ATR below invalidation ({under:.2f} < {emergency:.2f})"
        elif trailing and cfg["trailAfterTarget"]:
            if peak and mark <= peak * (1 - cfg["trailPct"] / 100):
                reason = f"trailing stop: option {mark:.2f} is {cfg['trailPct']:.0f}% below its peak {peak:.2f} (target was reached)"
        elif profit_exits and pl_pct >= tgt:
            reason = f"{'spread' if is_spread(p) else 'option'} target (+{pl_pct:.0f}%)"
        elif profit_exits and under is not None and under >= p["underlyingTarget"]:
            reason = f"{rec['symbol']} reached target {p['underlyingTarget']}"
        if not reason and dte <= cfg["timeStopDte"]:
            reason = f"time stop ({dte} days to expiry)"
        elif not reason and due:
            reason = earn_reason
        if reason:
            close(sub, rec["id"], reason)
            actions.append((rec["symbol"], reason))
        elif is_diag(p):
            try:
                note = manage_diagonal(sub, c, cfg, rec)
            except Exception as e:
                note = f"roll error: {str(e)[:160]}"
                db.update(pk, rec["SK"], {"rollNote": note})
            if note:
                actions.append((rec["symbol"], note))
    return {"actions": actions}


# ---------------- diagonal: keep shorting against the long call ----------------

def _roll_due(p, cfg, now):
    if not p.get("shortContract"):
        return True
    short_dte = (datetime.strptime(p["shortExp"], "%Y-%m-%d").date() - now.date()).days
    return short_dte < cfg["diagRollDte"] or (short_dte == cfg["diagRollDte"] and now.hour * 60 + now.minute >= 15 * 60 + 30)


def _fill_credit(o, limit):
    """Net credit per spread from a filled roll/sell order: sold legs minus bought legs (falls back to the limit)."""
    legs = o.get("legs") or []
    if legs and all(l.get("filled_avg_price") for l in legs):
        return round(sum(float(l["filled_avg_price"]) * (1 if l.get("side") == "sell" else -1) for l in legs), 4)
    if o.get("order_class") != "mleg" and o.get("filled_avg_price"):
        return float(o["filled_avg_price"])        # single sell_to_open
    return -float(limit or 0)                      # mleg limit: negative = credit


def manage_diagonal(sub, c, cfg, rec):
    """Roll the short call to the next expiration cycle (or sell one when none is held). Returns a note or None."""
    pk, p, now = db.upk(sub), rec["proposal"], now_ny()
    qty = int(rec.get("filledQty") or rec.get("qty") or 0)
    if qty < 1:
        return None
    oid = rec.get("rollOrderId")
    if oid:
        o = _alp(c, "GET", f"/v2/orders/{oid}?nested=true") or {}
        st = o.get("status")
        if st == "filled":
            new = rec["rollTo"]
            credit = _fill_credit(o, rec.get("rollLimit"))
            old = p.get("shortContract")
            legs = [p["legs"][0], {"symbol": new["symbol"], "side": "sell", "exp": new["exp"], "strike": new["strike"],
                                   "mid": new["mid"], "delta": new["delta"]}]
            p = {**p, "shortContract": new["symbol"], "shortExp": new["exp"], "shortStrike": new["strike"], "legs": legs}
            rolls = list(rec.get("rolls") or []) + [{"at": iso(now), "from": old, "to": new["symbol"], "credit": credit}]
            db.update(pk, rec["SK"], {"proposal": p, "rolls": rolls[-100:], "cashAdj": round((rec.get("cashAdj") or 0) + credit, 4),
                                      "rollOrderId": "", "rollTo": "", "rollMarket": False, "rollNote": ""})
            rec["proposal"] = p
            what = f"rolled {old} → {new['symbol']}" if old else f"sold {new['symbol']}"
            _notify(sub, f"Paper bot {_desc(rec)}: {what} for net {'credit' if credit >= 0 else 'debit'} {abs(credit):.2f}", "roll")
            return what
        if st in ("canceled", "expired", "rejected"):
            db.update(pk, rec["SK"], {"rollOrderId": "", "rollMarket": True, "rollNote": f"roll order {st}"})
            return None
        started = datetime.strptime(rec.get("rollAt") or iso(now), "%Y-%m-%dT%H:%M:%S")
        if now - started >= timedelta(minutes=cfg["diagRollWaitMin"]) and not rec.get("rollMarket"):
            _alp(c, "DELETE", f"/v2/orders/{oid}")      # not filled at mid: next check resends at market
            db.update(pk, rec["SK"], {"rollMarket": True, "rollNote": "roll limit not filled, retrying at market"})
        return None
    if not _roll_due(p, cfg, now) or not market_open(now):
        return None
    last = rec.get("rollSearchAt")
    if last and not p.get("shortContract") and now - datetime.strptime(last, "%Y-%m-%dT%H:%M:%S") < timedelta(minutes=15):
        return None                                     # no qualifying short last time: look again every 15 minutes
    chain = gex.fetch_chain(sub, rec["symbol"], max(30, cfg["diagShortDteMax"] + 5))
    spot = chain["spot"]
    after = max(p.get("shortExp") or "", now.date().isoformat())
    new = pick_roll_short(chain["contracts"], spot, p, cfg, after)
    mk = rec.get("rollMarket")
    if not new:
        upd = {"rollSearchAt": iso(now), "rollNote": f"no call in the next cycles pays ${cfg['diagMinShortCredit']:g}+ per contract in the delta window"}
        if p.get("shortContract"):                     # don't let the current short expire/get assigned: buy it back
            sp = _alp(c, "GET", f"/v2/positions/{p['shortContract']}")
            cost = abs(float((sp or {}).get("current_price") or 0))
            if sp:
                _alp(c, "DELETE", f"/v2/positions/{p['shortContract']}")
            upd.update(proposal={**p, "shortContract": None, "shortExp": None, "shortStrike": None},
                       cashAdj=round((rec.get("cashAdj") or 0) - cost, 4))
            _notify(sub, f"Paper bot {_desc(rec)}: bought back {p['shortContract']}; {upd['rollNote']}, will retry", "roll")
        db.update(pk, rec["SK"], upd)
        return upd["rollNote"]
    cid = f"tj-{rec['id']}-r{len(rec.get('rolls') or [])}{'m' if mk else ''}{int(time.time()) % 10000}"
    if p.get("shortContract"):
        old_q = next((x for x in chain["contracts"] if x["sym"] == p["shortContract"]), None)
        old_mid = _leg(old_q, spot, now.date())["mid"] if old_q else None
        if old_mid is None:
            old_mid = abs(float((_alp(c, "GET", f"/v2/positions/{p['shortContract']}") or {}).get("current_price") or 0))
        net = old_mid - new["mid"]                      # positive = debit, negative = credit (Alpaca mleg sign)
        lim = round(math.ceil(round(net / 0.05, 6)) * 0.05, 2)
        body = {"order_class": "mleg", "qty": str(qty), "time_in_force": "day", "client_order_id": cid,
                "legs": [{"symbol": p["shortContract"], "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"},
                         {"symbol": new["symbol"], "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"}]}
        body.update({"type": "market"} if mk else {"type": "limit", "limit_price": f"{lim:.2f}"})
        try:
            o = _alp(c, "POST", "/v2/orders", body)
        except BadRequest as e:
            # combined roll refused: buy back the short now, sell the new one on the next check
            sp = _alp(c, "GET", f"/v2/positions/{p['shortContract']}")
            cost = abs(float((sp or {}).get("current_price") or old_mid or 0))
            if sp:
                _alp(c, "DELETE", f"/v2/positions/{p['shortContract']}")
            db.update(pk, rec["SK"], {"proposal": {**p, "shortContract": None, "shortExp": None, "shortStrike": None},
                                      "cashAdj": round((rec.get("cashAdj") or 0) - cost, 4), "rollNote": f"mleg roll refused ({str(e)[:80]}), legging"})
            return "bought back short call (roll legged)"
    else:
        lim = max(0.05, round(math.floor(round(new["mid"] / 0.05, 6)) * 0.05, 2))
        body = {"symbol": new["symbol"], "qty": str(qty), "side": "sell", "position_intent": "sell_to_open",
                "time_in_force": "day", "client_order_id": cid}
        body.update({"type": "market"} if mk else {"type": "limit", "limit_price": f"{lim:.2f}"})
        o = _alp(c, "POST", "/v2/orders", body)
        lim = -lim
    db.update(pk, rec["SK"], {"rollOrderId": (o or {}).get("id"), "rollAt": iso(now), "rollLimit": 0 if mk else lim,
                              "rollTo": {k: new[k] for k in ("symbol", "exp", "strike", "mid", "delta")}, "rollSearchAt": ""})
    return f"{'rolling' if p.get('shortContract') else 'selling'} short call → {new['symbol']} ({'market' if mk else f'limit {lim:+.2f}'})"


def scan(sub):
    cfg = settings(sub)
    if not cfg["enabled"]:
        return {"skipped": "bot disabled"}
    pk = db.upk(sub)
    open_n = sum(1 for r in db.q_prefix(pk, "BOT#") if r.get("status") in ("submitted", "open"))
    held = {r["symbol"] for r in db.q_prefix(pk, "BOT#") if r.get("status") in ("submitted", "open")}
    results = []
    for sym in cfg["watchlist"]:
        if sym in held:
            continue
        try:
            rec = evaluate(sub, sym)
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)[:160]})
            continue
        results.append({"symbol": sym, "decision": rec["decision"]})
        if cfg["autoSubmit"] and rec["decision"] == "BUY" and open_n < cfg["maxPositions"]:
            try:
                place(sub, rec["id"], placed_by="auto")
                chase(sub, rec["id"])
                open_n += 1
            except Exception as e:
                results[-1]["orderError"] = str(e)[:160]
    return {"results": results}


def market_open(now=None):
    now = now or now_ny()
    m = now.hour * 60 + now.minute
    return now.weekday() < 5 and 9 * 60 + 35 <= m <= 15 * 60 + 50


def flow_scan(sub, cfg=None):
    """Pull new unusual-flow alerts, evaluate the tickers and trade the ones that pass every rule."""
    import flowdata
    cfg = cfg or settings(sub)
    pk = db.upk(sub)
    state = db.get(pk, "BOTSTATE") or {"PK": pk, "SK": "BOTSTATE"}
    last = state.get("lastFlowAt")
    res = flowdata.alerts(sub, cfg["flowMinPremium"], "call", cfg["flowMinDte"], cfg["flowMaxDte"], cfg["flowAskSide"],
                          cfg["flowSweeps"], None, cfg["flowMinVolOi"], 1, since_utc=last, exclude_etfs=cfg["excludeEtfs"])
    # only act on fresh alerts (the last 20 minutes), whatever the data source returned
    fresh_cut = (datetime.utcnow() - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S")
    alerts = [x for x in res["alerts"] if (x.get("at") or "")[:19] >= fresh_cut]
    newest = max([a["at"] for a in alerts if a.get("at")] + ([last] if last else []), default=None)
    recs = db.q_prefix(pk, "BOT#")
    held = {r["symbol"] for r in recs if r.get("status") in ("submitting", "submitted", "open", "closing")}
    open_n = sum(1 for r in recs if r.get("status") in ("submitting", "submitted", "open"))
    evaluated = dict(state.get("evaluated") or {})
    cutoff = iso(now_ny() - timedelta(minutes=cfg["flowCooldownMin"]))
    by_ticker = {}
    for a in alerts:                                   # biggest premium per ticker first
        t = by_ticker.setdefault(a["ticker"], {"ticker": a["ticker"], "premium": 0.0, "alerts": []})
        t["premium"] += a["premium"]
        t["alerts"].append(a)
    queue = sorted(by_ticker.values(), key=lambda t: -t["premium"])
    done = []
    for t in queue:
        if len(done) >= cfg["flowMaxEvals"]:
            break
        sym = t["ticker"]
        if not sym or sym in held or evaluated.get(sym, "") > cutoff:
            continue
        top = max(t["alerts"], key=lambda a: a["premium"])
        origin = {"type": "flow", "premium": round(t["premium"]), "alerts": len(t["alerts"]), "sweep": any(a["sweep"] for a in t["alerts"]),
                  "contract": top.get("contract"), "askPct": top.get("askPct"), "volOi": top.get("volOi"), "at": top.get("at")}
        try:
            rec = evaluate(sub, sym, source=origin)
        except Exception as e:
            done.append({"symbol": sym, "error": str(e)[:120]})
            evaluated[sym] = iso(now_ny())
            continue
        evaluated[sym] = iso(now_ny())
        item = {"symbol": sym, "decision": rec["decision"]}
        if cfg["autoSubmit"] and rec["decision"] == "BUY" and open_n < cfg["maxPositions"]:
            try:
                place(sub, rec["id"], placed_by="auto-flow")
                chase(sub, rec["id"])
                open_n += 1
                held.add(sym)
                item["ordered"] = True
            except Exception as e:
                item["orderError"] = str(e)[:160]
        done.append(item)
    evaluated = {k: v for k, v in evaluated.items() if v > iso(now_ny() - timedelta(days=1))}
    db.update(pk, "BOTSTATE", {"lastFlowAt": newest or last, "evaluated": evaluated, "lastFlowRun": iso(now_ny()),
                               "lastFlowResult": done[-10:], "lastFlowAlerts": len(alerts)})
    return {"alerts": len(alerts), "evaluated": done}


def tick(sub):
    """Runs every minute in market hours: manage exits, then (if enabled) act on new unusual flow."""
    if not db.try_lock(db.upk(sub), "BOTLOCK", 170):
        return {"skipped": "previous run still working"}
    try:
        out = {"monitor": monitor(sub)}
        cfg = settings(sub)
        if cfg["flowAuto"] and market_open():
            out["flow"] = flow_scan(sub, cfg)
        return out
    finally:
        db.unlock(db.upk(sub), "BOTLOCK")


def handler(event, context_):
    job = event.get("job", "monitor")
    if event.get("sub"):
        subs = [event["sub"]]
    elif job == "scan":   # watchlist scans only for users who turned the schedule on
        subs = [p["PK"][5:] for p in db.scan_sk("PROFILE") if ((p.get("settings") or {}).get("autotrade") or {}).get("enabled")]
    elif job == "shadow":  # after the close: users with evaluations that weren't placed
        subs = sorted({r["PK"][5:] for r in db.scan_prefix("BOT#", ("proposed", "dismissed", "closed"))})
    elif job == "review":  # end of day: users with open bot positions
        subs = sorted({r["PK"][5:] for r in db.scan_prefix("BOT#", ("open",))})
    elif job == "tick":   # every minute: users with open bot trades or flow trading on
        flow_users = {p["PK"][5:] for p in db.scan_sk("PROFILE") if ((p.get("settings") or {}).get("autotrade") or {}).get("flowAuto")}
        subs = sorted(flow_users | {r["PK"][5:] for r in db.scan_prefix("BOT#", ("submitted", "open", "closing"))})
    else:                 # exits are always managed for every open bot position, schedule on or off
        subs = sorted({r["PK"][5:] for r in db.scan_prefix("BOT#", ("submitted", "open", "closing"))})
    out = {}
    for sub in subs:
        try:
            if job == "tick":
                out[sub] = tick(sub)
            elif job == "chase":
                out[sub] = {"status": (chase(sub, event["id"]) or {}).get("status")}
            elif job == "shadow":
                import shadow
                try:
                    out[sub] = shadow.run(sub)
                    db.put({"PK": db.upk(sub), "SK": "SHADOWRUN", "status": "done", "at": iso(now_ny()), "result": out[sub]})
                except Exception as e:
                    db.put({"PK": db.upk(sub), "SK": "SHADOWRUN", "status": "error", "at": iso(now_ny()), "result": {"error": str(e)[:200]}})
                    raise
            elif job in ("review", "aireview"):
                import aicheck
                out[sub] = aicheck.review_all(sub, only_id=event.get("id"), auto_close=(job == "review"))
            elif job == "aicheck":
                import aicheck
                rec = db.get(db.upk(sub), f"BOT#{event['id']}")
                try:
                    out[sub] = {"verdict": aicheck.run(sub, rec)["verdict"]} if rec else {"error": "not found"}
                    db.update(db.upk(sub), f"BOT#{event['id']}", {"aiRunning": False, "aiError": ""})
                except Exception as e:
                    db.update(db.upk(sub), f"BOT#{event['id']}", {"aiRunning": False, "aiError": f"Claude's chart check failed: {str(e)[:160]}"})
                    raise
            else:
                out[sub] = scan(sub) if job == "scan" else monitor(sub)
        except Exception as e:
            print("autotrade error", sub, e)
            out[sub] = {"error": str(e)[:200]}
    return out
