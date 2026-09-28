"""Paper bot: evaluates a ticker with the trader's own method, picks a call contract, and (optionally) places the
order on the Alpaca PAPER account, then manages the exit. Long calls only for now.

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
            "timeStopDte": 14, "maxPositions": 5, "crossWindow": 10, "maxExtAtr": 1.5, "earnings": {}, "noEntryDays": 5,
            "chaseStep": 0.10, "chaseSeconds": 12, "chaseMaxSteps": 5, "chaseMaxPct": 10,
            "requireAboveFlip": False, "minRoomRatio": 1.5}


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
    return {
        "price": round(last["c"], 2), "asOf": last["t"][:10], "ema21": round(ema[i], 2), "atr21": round(atr[i], 2),
        "extAtr": round(ext, 2) if ext is not None else None, "crossAgo": cross_ago,
        "momentum": round(mom[i], 3) if mom[i] is not None else None,
        "momentumPrev": round(mom[i - 1], 3) if mom[i - 1] is not None else None,
        "study": st,
        "checks": {
            "bullFvg": bool(st.get("bullFvg")), "noBearFvg": not st.get("bearFvg"),
            "aboveEma": last["c"] > ema[i], "recentCross": cross_ago is not None,
            "momentumUp": mom[i] is not None and mom[i - 1] is not None and mom[i] > mom[i - 1],
            "aboveAvwap": st.get("vsAvwap") == "above", "notExtended": ext is not None and ext <= cfg["maxExtAtr"],
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


# ---------------- evaluation ----------------

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


def evaluate(sub, symbol, earnings_date=None):
    symbol = (symbol or "").strip().upper()
    if not symbol or not symbol.replace(".", "").isalnum() or len(symbol) > 8:
        raise BadRequest("Enter a ticker symbol.")
    if earnings_date is not None:
        set_earnings(sub, symbol, earnings_date.strip())
    cfg = settings(sub)
    base = load_settings(sub)
    sig = chart_signals(symbol, cfg)
    chain = gex.fetch_chain(sub, symbol, max(cfg["dteMax"] + 10, 90))
    spot = chain["spot"] or sig["price"]
    near = [c for c in chain["contracts"] if c["exp"] <= (now_ny() + timedelta(days=45)).strftime("%Y-%m-%d")]
    g = gex.compute(near or chain["contracts"], spot)
    em = gex.expected_moves(chain["contracts"], spot, limit=16)
    em_list = em["byExpiration"]
    events = iv_events(em_list)
    earnings_raw = (cfg.get("earnings") or {}).get(symbol)
    earnings, earn_timing = parse_earn(earnings_raw)
    cands = pick_contract(chain["contracts"], spot, em_list, cfg, earnings, g)
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

    c = sig["checks"]
    today = now_ny().date().isoformat()
    earn_soon = bool(earnings and today <= earnings <= (now_ny() + timedelta(days=cfg["noEntryDays"])).strftime("%Y-%m-%d"))
    event_in_window = [e for e in events if best and e["between"][1] <= best["exp"]]
    checks = [
        ("chart", "Active bull fair value gap" + (f" ({sig['study']['bullFvg'][0]}–{sig['study']['bullFvg'][1]})" if sig["study"].get("bullFvg") else " (none active)"), c["bullFvg"], True),
        ("chart", "No active bear (negative) fair value gap" + (f": bear gap at {sig['study']['bearFvg'][0]}–{sig['study']['bearFvg'][1]} overhead" if sig["study"].get("bearFvg") else ""), c["noBearFvg"], True),
        ("chart", "Close above the 21 EMA", c["aboveEma"], True),
        ("chart", f"Crossed above the 21 EMA in the last {cfg['crossWindow']} days" + (f" ({sig['crossAgo']} days ago)" if sig["crossAgo"] is not None else ""), c["recentCross"], True),
        ("chart", f"Momentum rising ({sig['momentumPrev']} → {sig['momentum']})", c["momentumUp"], True),
        ("chart", "Close above the anchored VWAP", c["aboveAvwap"], True),
        ("chart", f"Not extended ({sig['extAtr']} ATR above the EMA, limit {cfg['maxExtAtr']})", c["notExtended"], True),
        ("gamma", (f"Price {'above' if spot > g['gammaFlip'] else 'below'} the gamma flip ({g['gammaFlip']})" if g.get("gammaFlip")
                   else f"No gamma flip within ±15%: gamma is {g['regime']} across the whole range"),
         (spot > g["gammaFlip"]) if g.get("gammaFlip") else g["regime"] == "positive", cfg["requireAboveFlip"]),
        ("gamma", f"Room {room:.1f}% to target {target_lvl} vs {risk:.1f}% to invalidation {round(stop_lvl, 2)} (ratio {ratio and round(ratio, 1)} : 1, need {cfg['minRoomRatio']})",
         bool(ratio and ratio >= cfg["minRoomRatio"]), True),
        ("events", f"No earnings within {cfg['noEntryDays']} days" + (f" (entered: {earnings} {'after close' if earn_timing == 'AMC' else 'before open'}; the bot exits before the report)" if earnings else " (no date entered)"), not earn_soon, True),
        ("events", "No implied-volatility jump (likely event) before the chosen expiration" +
         (f": IV jumps between {event_in_window[0]['between'][0]} and {event_in_window[0]['between'][1]}" if event_in_window else ""),
         not event_in_window, False),
        ("contract", f"Liquid contract found: {symbol} {best['exp']} {best['strike']} call (delta {best['delta']}, mid {best['mid']}, open interest {best['oi']})" if best else (f"Best contract {symbol} {cands[0]['exp']} {cands[0]['strike']} call (delta {cands[0]['delta']}, mid {cands[0]['mid']}, {cands[0]['dte']} days) fails: " + "; ".join(cands[0]["problems"]) if cands else f"No call with delta {cfg['deltaMin']}-{cfg['deltaMax']} expiring in {cfg['dteMin']}-{cfg['dteMax']} days"), bool(best), True),
    ]
    hard_fail = [t for (_, t, ok, hard) in checks if hard and not ok]
    soft_fail = [t for (_, t, ok, hard) in checks if not hard and not ok]
    chart_ok = all(ok for (grp, _, ok, _) in checks if grp == "chart")
    decision = "BUY" if not hard_fail else ("WAIT" if chart_ok or sum(1 for (grp, _, ok, _) in checks if grp == "chart" and not ok) <= 2 else "SKIP")

    proposal = None
    if best:
        risk_amt = base.get("riskPerTrade") or 200
        per_contract_loss = best["mid"] * 100 * cfg["stopPct"] / 100
        qty = int(risk_amt // per_contract_loss) if per_contract_loss else 0
        if base.get("accountSize") and base.get("maxPositionPct"):
            cap = base["accountSize"] * base["maxPositionPct"] / 100
            qty = min(qty, int(cap // (best["mid"] * 100)))
        proposal = {"contract": best["symbol"], "underlying": symbol, "exp": best["exp"], "strike": best["strike"],
                    "qty": qty, "limit": _tick(best["mid"]), "cost": round(qty * best["mid"] * 100, 2),
                    "optionStop": round(best["mid"] * (1 - cfg["stopPct"] / 100), 2),
                    "optionTarget": round(best["mid"] * (1 + cfg["targetPct"] / 100), 2),
                    "underlyingStop": round(stop_lvl, 2), "underlyingTarget": round(target_lvl, 2),
                    "riskAtStop": round(qty * per_contract_loss, 2)}
        if qty < 1:
            decision = "SKIP" if decision == "BUY" else decision
            hard_fail.append(f"One contract risks ${per_contract_loss:.0f} at the {cfg['stopPct']}% stop, more than your ${risk_amt} risk per trade")

    rec = {"id": now_ny().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6], "symbol": symbol, "createdAt": iso(now_ny()),
           "decision": decision, "checks": [{"group": grp, "text": t, "ok": ok, "required": hard} for (grp, t, ok, hard) in checks],
           "blocking": hard_fail, "warnings": soft_fail, "proposal": proposal, "candidates": cands,
           "signals": {k: sig[k] for k in ("price", "asOf", "ema21", "atr21", "extAtr", "crossAgo", "momentum", "momentumPrev")},
           "gamma": {k: g.get(k) for k in ("gammaFlip", "callWall", "putWall", "netGex", "regime")},
           "events": events, "source": chain["source"], "status": "proposed"}
    db.put({"PK": db.upk(sub), "SK": f"BOT#{rec['id']}", **rec})
    return rec


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


def place(sub, bot_id, qty=None, limit=None, placed_by="manual"):
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
    order = _alp(c, "POST", "/v2/orders", {"symbol": p["contract"], "qty": str(q), "side": "buy", "type": "limit",
                                           "limit_price": f"{lim:.2f}", "time_in_force": "day"})
    db.update(pk, f"BOT#{bot_id}", {"status": "submitted", "orderId": order.get("id"), "qty": q, "limit": lim,
                                    "firstLimit": lim, "chaseSteps": 0, "submittedAt": iso(now_ny()), "placedBy": placed_by})
    return db.get(pk, f"BOT#{bot_id}")


def _order_state(c, oid):
    o = _alp(c, "GET", f"/v2/orders/{oid}") or {}
    return o.get("status"), float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)


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
        o = _alp(c, "POST", "/v2/orders", {"symbol": rec["proposal"]["contract"], "qty": str(remaining), "side": "buy",
                                           "type": "limit", "limit_price": f"{new_limit:.2f}", "time_in_force": "day"})
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
    return db.get(pk, f"BOT#{bot_id}")


def close(sub, bot_id, reason="manual"):
    c = _paper_creds(sub)
    pk = db.upk(sub)
    rec = db.get(pk, f"BOT#{bot_id}")
    if not rec:
        raise BadRequest("Bot trade not found.")
    if rec.get("status") == "submitted" and rec.get("orderId"):
        o = _alp(c, "GET", f"/v2/orders/{rec['orderId']}")
        if o and o.get("status") in ("new", "accepted", "partially_filled", "pending_new"):
            _alp(c, "DELETE", f"/v2/orders/{rec['orderId']}")
            if not float(o.get("filled_qty") or 0):
                db.update(pk, f"BOT#{bot_id}", {"status": "cancelled", "exitReason": reason, "closedAt": iso(now_ny())})
                return db.get(pk, f"BOT#{bot_id}")
    res = _alp(c, "DELETE", f"/v2/positions/{rec['proposal']['contract']}")
    db.update(pk, f"BOT#{bot_id}", {"status": "closing", "exitReason": reason, "exitOrderId": (res or {}).get("id"),
                                    "closedAt": iso(now_ny())})
    return db.get(pk, f"BOT#{bot_id}")


# ---------------- monitoring ----------------

def _refresh(sub, c, rec):
    """Bring one bot record up to date with Alpaca (fill, live value, exit fill). Returns (rec, position)."""
    pk = db.upk(sub)
    st, p = rec.get("status"), rec["proposal"]
    if st == "submitted" and rec.get("orderId"):
        o = _alp(c, "GET", f"/v2/orders/{rec['orderId']}")
        if o and o.get("status") == "filled":
            fq, fp = float(o.get("filled_qty") or 0), float(o.get("filled_avg_price") or 0)
            q = rec.get("prevFilledQty", 0) + fq
            avg = (rec.get("prevFilledCost", 0) + fq * fp) / q if q else fp
            db.update(pk, rec["SK"], {"status": "open", "fillPrice": round(avg, 4), "filledQty": q, "filledAt": o.get("filled_at")})
            rec.update(status="open", fillPrice=round(avg, 4), filledQty=q)
        elif o and o.get("status") in ("canceled", "expired", "rejected") and not rec.get("prevFilledQty"):
            db.update(pk, rec["SK"], {"status": "not filled", "exitReason": o.get("status")})
            rec["status"] = "not filled"
            return rec, None
        else:
            return rec, None
    if rec["status"] not in ("open", "closing"):
        return rec, None
    pos = _alp(c, "GET", f"/v2/positions/{p['contract']}")
    if not pos:
        upd = {"status": "closed"}
        if rec.get("exitOrderId"):
            o = _alp(c, "GET", f"/v2/orders/{rec['exitOrderId']}") or {}
            if o.get("filled_avg_price"):
                upd["exitPrice"] = float(o["filled_avg_price"])
        exit_px = upd.get("exitPrice") or rec.get("lastMark")
        qty = rec.get("filledQty") or rec.get("qty") or 0
        if exit_px is not None and rec.get("fillPrice"):
            upd["realizedPl"] = round((exit_px - rec["fillPrice"]) * qty * 100, 2)
            upd["realizedPct"] = round((exit_px / rec["fillPrice"] - 1) * 100, 1)
        if rec["status"] == "open" and not rec.get("exitReason"):
            upd["exitReason"] = "closed outside the bot"
        db.update(pk, rec["SK"], upd)
        rec.update(upd)
        return rec, None
    mark = float(pos.get("current_price") or 0)
    upd = {"lastMark": mark, "lastPlPct": round(float(pos.get("unrealized_plpc") or 0) * 100, 1),
           "lastPl": round(float(pos.get("unrealized_pl") or 0), 2), "marketValue": round(float(pos.get("market_value") or 0), 2),
           "lastCheck": iso(now_ny())}
    if not rec.get("fillPrice") and pos.get("avg_entry_price"):
        upd["fillPrice"] = float(pos["avg_entry_price"])
    snaps = list(rec.get("marks") or [])
    if not snaps or (datetime.strptime(snaps[-1]["t"], "%Y-%m-%dT%H:%M:%S") <= now_ny() - timedelta(minutes=14)):
        snaps.append({"t": iso(now_ny()), "mark": mark, "plPct": upd["lastPlPct"]})
        upd["marks"] = snaps[-300:]
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


def monitor(sub):
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
        pl_pct = rec["lastPlPct"]
        try:
            bars = _yahoo(rec["symbol"].replace(".", "-"), "5m", now_ny() - timedelta(days=3), now_ny() + timedelta(hours=1))
            under = bars[-1]["c"] if bars else None
        except Exception:
            under = None
        if under is not None:
            db.update(pk, rec["SK"], {"lastUnderlying": under})
        dte = (datetime.strptime(p["exp"], "%Y-%m-%d").date() - now_ny().date()).days
        due, earn_reason = earnings_exit_due((cfg.get("earnings") or {}).get(rec["symbol"]))
        reason = None
        if pl_pct <= -cfg["stopPct"]:
            reason = f"option stop ({pl_pct:.0f}%)"
        elif under is not None and under < p["underlyingStop"]:
            reason = f"{rec['symbol']} below invalidation {p['underlyingStop']} ({under:.2f})"
        elif pl_pct >= cfg["targetPct"]:
            reason = f"option target (+{pl_pct:.0f}%)"
        elif under is not None and under >= p["underlyingTarget"]:
            reason = f"{rec['symbol']} reached target {p['underlyingTarget']}"
        elif dte <= cfg["timeStopDte"]:
            reason = f"time stop ({dte} days to expiry)"
        elif due:
            reason = earn_reason
        if reason:
            close(sub, rec["id"], reason)
            actions.append((rec["symbol"], reason))
    return {"actions": actions}


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


def handler(event, context_):
    job = event.get("job", "monitor")
    if event.get("sub"):
        subs = [event["sub"]]
    elif job == "scan":   # watchlist scans only for users who turned the schedule on
        subs = [p["PK"][5:] for p in db.scan_sk("PROFILE") if ((p.get("settings") or {}).get("autotrade") or {}).get("enabled")]
    else:                 # exits are always managed for every open bot position, schedule on or off
        subs = sorted({r["PK"][5:] for r in db.scan_prefix("BOT#", ("submitted", "open", "closing"))})
    out = {}
    for sub in subs:
        try:
            if job == "chase":
                out[sub] = {"status": (chase(sub, event["id"]) or {}).get("status")}
            else:
                out[sub] = scan(sub) if job == "scan" else monitor(sub)
        except Exception as e:
            print("autotrade error", sub, e)
            out[sub] = {"error": str(e)[:200]}
    return out
