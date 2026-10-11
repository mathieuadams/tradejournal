"""Mix-and-match explorer: every evaluation (taken: real result; skipped: shadow result) as one row of features,
filtered by any combination of conditions, plus an automatic search for the best combinations.

Overfitting guard: the search only keeps combinations with at least `min_n` results, and reports whether the
combination holds in both halves of the period (older ideas vs newer ideas)."""
import itertools
import re
from datetime import datetime, timedelta

import db
from util import now_ny

TAKEN = ("submitting", "submitted", "open", "closing", "closed")

# rule checks, by the start of their text
CHECKS = [
    ("bullFvg", "Active bull fair value gap", "Active bull fair value gap"),
    ("noBearFvg", "No active bear", "No bear fair value gap overhead"),
    ("aboveEma", "Close above the 21 EMA", "Close above the 21 EMA"),
    ("recentCross", "Crossed above the 21 EMA", "Crossed above the 21 EMA recently"),
    ("momUp", "Momentum rising", "Momentum rising"),
    ("aboveAvwap", "Close above the anchored VWAP", "Above the anchored VWAP"),
    ("up30", "Up at least", "Up 10%+ from the 30-day low"),
    ("notExtended", "Not extended", "Not extended above the EMA"),
    ("aboveFlip", "Price above the gamma flip", "Above the gamma flip"),
    ("roomOk", "Room ", "Enough room to target vs stop"),
    ("repeatBuy", "Repeat buying", "Repeat buying (smaller call buys)"),
    ("putsOk", "Puts not piling in", "Puts not piling in"),
    ("noEarnings", "No earnings", "No earnings soon"),
]

# feature key -> (label, kind, choices for the search)
FEATURES = {
    **{k: (label, "bool", None) for k, _, label in CHECKS},
    "decisionBuy": ("Bot decision was BUY", "bool", None),
    "taken": ("The bot took the trade", "bool", None),
    "sweep": ("Flow alert was a sweep", "bool", None),
    "burstSameDay": ("Call burst on the ticker that day (before the evaluation)", "bool", None),
    "burst2d": ("Call burst on the ticker in the 2 days before", "bool", None),
    "claudeApprove": ("Claude approved the entry", "bool", None),
    "claudeReject": ("Claude rejected the entry", "bool", None),
    "callPut": ("Calls vs puts bought on the ticker (x to 1, last 5 days)", "num", [(">=", 2), (">=", 3), (">=", 5), ("<=", 2)]),
    "flowPremium": ("Flow alert premium ($)", "num", [(">=", 250000), (">=", 500000), (">=", 1000000)]),
    "volOi": ("Flow volume / open interest", "num", [(">=", 1), (">=", 3), ("<=", 1)]),
    "askPct": ("Flow % bought at the ask", "num", [(">=", 80), (">=", 95)]),
    "smallCalls": ("Smaller call buys on the ticker (last 5 days)", "num", [(">=", 4), (">=", 10), ("<=", 0)]),
    "extAtr": ("ATR above the 21 EMA", "num", [("<=", 0.5), ("<=", 1.0), ("<=", 1.5), (">=", 1.5)]),
    "growth30": ("% up from the 30-day low", "num", [(">=", 5), (">=", 10), (">=", 20), ("<=", 5)]),
    "hour": ("Hour of the evaluation (ET)", "num", [("<=", 10), (">=", 14)]),
    # options volume on the ticker (alerts bought at the ask)
    "callPrem5d": ("Call premium bought on the ticker, last 5 days ($)", "num", [(">=", 1_000_000), (">=", 5_000_000), (">=", 15_000_000)]),
    "putPrem5d": ("Put premium bought on the ticker, last 5 days ($)", "num", [("<=", 1_000_000), (">=", 5_000_000)]),
    "callPremToday": ("Call premium bought on the ticker that day ($)", "num", [(">=", 500_000), (">=", 2_000_000)]),
    "callHits5d": ("Call alerts on the ticker, last 5 days", "num", [(">=", 20), (">=", 50)]),
    # gamma walls of the evaluation day (stored by day from Unusual Whales greek exposure)
    "cwAbovePw": ("Call wall above the put wall", "bool", None),
    "pwDist": ("% above the put wall", "num", [("<=", 1), ("<=", 2), ("<=", 5), (">=", 5)]),
    "cwRoom": ("% room to the call wall", "num", [(">=", 3), (">=", 5), (">=", 10), ("<=", 0)]),
    "betweenWalls": ("Price between the put wall and the call wall", "bool", None),
    "pwUp": ("Put wall moved up since the day before", "bool", None),
    "pwDown": ("Put wall moved down since the day before", "bool", None),
    "cwUp": ("Call wall moved up since the day before", "bool", None),
    "cwDown": ("Call wall moved down since the day before", "bool", None),
    "risingFloor": ("Put wall up, call wall flat or up", "bool", None),
    "wallWidth": ("% gap between the put wall and the call wall", "num", [("<=", 3), ("<=", 6), (">=", 10)]),
    "pwChg": ("Put wall change since the day before (%)", "num", [(">=", 1), (">=", 3), ("<=", -1)]),
    "cwChg": ("Call wall change since the day before (%)", "num", [(">=", 1), (">=", 3), ("<=", -1)]),
    "pwUp2": ("Put wall up 2 days in a row", "bool", None),
    "belowPw": ("Price below the put wall", "bool", None),
    "aboveCw": ("Price above the call wall", "bool", None),
    "atPw": ("Price within 1% of the put wall", "bool", None),
    # price / trend values at evaluation time
    "emaDist": ("% above the 21 EMA", "num", [("<=", 0), ("<=", 2), ("<=", 5), (">=", 5)]),
    "atrPct": ("ATR as % of price", "num", [("<=", 2), ("<=", 3), (">=", 4)]),
    "crossAgo": ("Bars since the 21 EMA cross", "num", [("<=", 3), ("<=", 10), (">=", 20)]),
    "momentum": ("Momentum value", "num", [(">=", 0), ("<=", 0)]),
    "momAccel": ("Momentum change vs the bar before", "num", [(">=", 0), ("<=", 0)]),
    "low30Age": ("Days since the 30-day low", "num", [("<=", 5), ("<=", 10), (">=", 20)]),
    # gamma exposure at evaluation time (option chain)
    "flipDist": ("% above the gamma flip", "num", [(">=", 0), (">=", 2), ("<=", 0), ("<=", -2)]),
    "posGex": ("Net gamma positive", "bool", None),
    "chainPwDist": ("% above the put wall (live chain)", "num", [("<=", 2), ("<=", 5), (">=", 5)]),
    "chainCwRoom": ("% room to the call wall (live chain)", "num", [(">=", 3), (">=", 5), (">=", 10), ("<=", 0)]),
    # trade plan
    "roomRatio": ("Room to target / risk to stop", "num", [(">=", 1.5), (">=", 2), (">=", 3)]),
    "stopDist": ("% to the stop (underlying)", "num", [("<=", 2), ("<=", 4), (">=", 6)]),
    "targetDist": ("% to the target (underlying)", "num", [(">=", 3), (">=", 6), (">=", 10)]),
    # chosen contract
    "dte": ("Days to expiration of the call", "num", [("<=", 14), ("<=", 30), (">=", 30), (">=", 60)]),
    "delta": ("Delta of the call", "num", [(">=", 0.5), (">=", 0.6), ("<=", 0.4)]),
    "iv": ("Implied volatility of the call (%)", "num", [("<=", 40), ("<=", 60), (">=", 60), (">=", 90)]),
    "spreadPct": ("Bid/ask spread of the call (% of mid)", "num", [("<=", 3), ("<=", 6), (">=", 10)]),
    "oi": ("Open interest of the call", "num", [(">=", 500), (">=", 2000), ("<=", 200)]),
    "otmPct": ("% out of the money (strike vs price)", "num", [("<=", 0), ("<=", 3), (">=", 5)]),
    "ivEvent": ("Volatility jump between expirations (likely event)", "bool", None),
    # the flow alert itself
    "flowAlerts": ("Alerts in the flow trigger", "num", [(">=", 2), (">=", 4)]),
    "topPremium": ("Biggest single alert premium ($)", "num", [(">=", 250_000), (">=", 1_000_000)]),
    "flowOptPrice": ("Option price of the alert ($)", "num", [("<=", 1), ("<=", 3), (">=", 5)]),
    "repeatRule": ("Unusual Whales flagged repeated hits", "bool", None),
    "flowLagMin": ("Minutes from the alert to the evaluation", "num", [("<=", 5), ("<=", 30), (">=", 60)]),
    # options volume on the ticker
    "putPremToday": ("Put premium bought on the ticker that day ($)", "num", [("<=", 100_000), (">=", 1_000_000)]),
    "callsOverPutsToday": ("Calls bought > puts bought that day", "bool", None),
    "todayShare": ("% of the 5-day call premium bought that day", "num", [(">=", 50), (">=", 80), ("<=", 30)]),
    "bigCalls5d": ("Big call alerts on the ticker, last 5 days", "num", [(">=", 2), (">=", 5)]),
    "bigPuts5d": ("Big put alerts on the ticker, last 5 days", "num", [("<=", 0), (">=", 2)]),
    "flowDays": ("Days with call buying, last 5 days", "num", [(">=", 2), (">=", 4)]),
    "callContracts5d": ("Different call contracts bought, last 5 days", "num", [(">=", 3), (">=", 8)]),
    "repeatQualifies": ("Repeat buying qualifies (bot rule)", "bool", None),
    # time
    "weekday": ("Weekday (1 = Monday … 5 = Friday)", "num", [("<=", 1), ("<=", 2), (">=", 4), (">=", 5)]),
    "minsOpen": ("Minutes since the open", "num", [("<=", 30), ("<=", 60), (">=", 300)]),
}

CATS = [("Chart rules", [k for k, _, _ in CHECKS]),
        ("Price & trend", ["emaDist", "atrPct", "extAtr", "crossAgo", "momentum", "momAccel", "growth30", "low30Age"]),
        ("Gamma walls (stored by day)", ["cwAbovePw", "pwDist", "cwRoom", "betweenWalls", "atPw", "belowPw", "aboveCw", "wallWidth",
                                         "pwUp", "pwDown", "pwUp2", "pwChg", "cwUp", "cwDown", "cwChg", "risingFloor"]),
        ("Gamma (live chain)", ["flipDist", "posGex", "chainPwDist", "chainCwRoom"]),
        ("Flow alert", ["sweep", "flowPremium", "topPremium", "flowAlerts", "volOi", "askPct", "flowOptPrice", "repeatRule", "flowLagMin"]),
        ("Options volume on the ticker", ["callPut", "callPrem5d", "putPrem5d", "callPremToday", "putPremToday", "callsOverPutsToday",
                                          "todayShare", "callHits5d", "smallCalls", "bigCalls5d", "bigPuts5d", "flowDays", "callContracts5d",
                                          "repeatQualifies", "burstSameDay", "burst2d"]),
        ("Contract", ["dte", "delta", "iv", "spreadPct", "oi", "otmPct", "ivEvent"]),
        ("Trade plan", ["roomRatio", "stopDist", "targetDist"]),
        ("Time", ["hour", "minsOpen", "weekday"]),
        ("Bot & Claude", ["decisionBuy", "taken", "claudeApprove", "claudeReject"])]
CAT_OF = {k: c for c, ks in CATS for k in ks}
FLOW_KEYS = ("callPut", "flowPremium", "volOi", "askPct", "smallCalls", "sweep", "repeatBuy", "putsOk", "burstSameDay", "burst2d",
             "callPrem5d", "putPrem5d", "callPremToday", "callHits5d", "topPremium", "flowAlerts", "flowOptPrice", "repeatRule",
             "flowLagMin", "putPremToday", "callsOverPutsToday", "todayShare", "bigCalls5d", "bigPuts5d", "flowDays",
             "callContracts5d", "repeatQualifies")
WALL_KEYS = ("cwAbovePw", "pwDist", "cwRoom", "betweenWalls", "pwUp", "pwDown", "cwUp", "cwDown", "risingFloor",
             "wallWidth", "pwChg", "cwChg", "pwUp2", "belowPw", "aboveCw", "atPw")
GAMMA_KEYS = WALL_KEYS + ("flipDist", "posGex", "chainPwDist", "chainCwRoom", "aboveFlip")
TREND_KEYS = ("emaDist", "atrPct", "extAtr", "crossAgo", "momentum", "momAccel", "growth30", "low30Age",
              "bullFvg", "noBearFvg", "aboveEma", "recentCross", "momUp", "aboveAvwap", "up30", "notExtended")
GROUPS = {"walls": ("Gamma walls", None), "flow": ("Options volume / flow", None), "wallsflow": ("Gamma walls + options volume", None),
          "gamma": ("All gamma", None), "trend": ("Price & trend", None), "trendflow": ("Price & trend + options volume", None)}


def wall_features(r, stored):
    """Wall features of one evaluation from the stored walls of its ticker ({date: walls})."""
    out = {k: None for k in WALL_KEYS}
    d0, price = (r.get("createdAt") or "")[:10], (r.get("signals") or {}).get("price")
    days = sorted(d for d in stored if d <= d0)
    if not days or not price:
        return out
    w = stored[days[-1]]
    if (datetime.strptime(d0, "%Y-%m-%d") - datetime.strptime(days[-1], "%Y-%m-%d")).days > 5:
        return out                                   # too old to describe that day
    pw, cw = w.get("putWall"), w.get("callWall")
    if pw:
        out["pwDist"] = round((price - pw) / pw * 100, 2)
    if cw:
        out["cwRoom"] = round((cw - price) / price * 100, 2)
    if pw:
        out["belowPw"] = price < pw
        out["atPw"] = abs(price - pw) / pw * 100 <= 1
    if cw:
        out["aboveCw"] = price > cw
    if pw and cw:
        out["betweenWalls"] = pw <= price <= cw
        out["cwAbovePw"] = cw > pw
        out["wallWidth"] = round((cw - pw) / pw * 100, 2)
    if len(days) >= 2:
        p0 = stored[days[-2]]
        if pw and p0.get("putWall"):
            out["pwUp"], out["pwDown"] = pw > p0["putWall"], pw < p0["putWall"]
            out["pwChg"] = round((pw - p0["putWall"]) / p0["putWall"] * 100, 2)
            if len(days) >= 3 and (stored[days[-3]].get("putWall") or 0):
                out["pwUp2"] = out["pwUp"] and p0["putWall"] > stored[days[-3]]["putWall"]
        if cw and p0.get("callWall"):
            out["cwUp"], out["cwDown"] = cw > p0["callWall"], cw < p0["callWall"]
            out["cwChg"] = round((cw - p0["callWall"]) / p0["callWall"] * 100, 2)
        if out["pwUp"] is not None and out["cwDown"] is not None:
            out["risingFloor"] = out["pwUp"] and not out["cwDown"]
    return out


def bursts_by_ticker(pk):
    """{ticker: [(date, 'HH:MM' window start)]} from live bursts and the 30-day replay."""
    out = {}
    for e in db.q_prefix(pk, "BURST#"):
        if e.get("ticker") and e.get("date"):
            out.setdefault(e["ticker"], []).append((e["date"], e.get("start") or "00:00"))
    st = db.get(pk, "BURSTSTUDY") or {}
    for r in st.get("bursts") or []:
        if r.get("strict") and r.get("ticker") and r.get("date"):
            out.setdefault(r["ticker"], []).append((r["date"], r.get("start") or "00:00"))
    return out


def features_of(r, bursts):
    """Every condition feature of one evaluation (known at evaluation time, except taken / Claude)."""
    at = r.get("createdAt") or ""
    f = {}
    checks = r.get("checks") or []
    for key, prefix, _ in CHECKS:
        c = next((c for c in checks if (c.get("text") or "").startswith(prefix)), None)
        f[key] = None if c is None else bool(c.get("ok"))
    if f.get("aboveFlip") is None:
        c = next((c for c in checks if c.get("group") == "gamma" and "flip" in (c.get("text") or "")), None)
        f["aboveFlip"] = None if c is None else bool(c.get("ok"))
    o = r.get("origin") or {}
    tf = r.get("tickerFlow") or {}
    sig = r.get("signals") or {}
    pr = tf.get("putRatio")
    calls = ((tf.get("window") or {}).get("calls") or {}).get("premium")
    f.update(
        decisionBuy=r.get("decision") == "BUY", taken=bool(r.get("orderId") or r.get("status") in TAKEN),
        sweep=bool(o.get("sweep")) if o.get("type") == "flow" else None,
        claudeApprove=None if not r.get("aiCheck") else r["aiCheck"].get("verdict") == "approve",
        claudeReject=None if not r.get("aiCheck") else r["aiCheck"].get("verdict") == "reject",
        callPut=None if not tf else (round(1 / pr, 2) if pr else (99.0 if calls else None)),
        flowPremium=o.get("premium") if o.get("type") == "flow" else None,
        volOi=o.get("volOi"), askPct=o.get("askPct"),
        smallCalls=((tf.get("window") or {}).get("smallCalls") or {}).get("hits") if tf else None,
        extAtr=sig.get("extAtr"), growth30=sig.get("growth30"),
        callPrem5d=(((tf.get("window") or {}).get("calls") or {}).get("premium")) if tf else None,
        putPrem5d=(((tf.get("window") or {}).get("puts") or {}).get("premium")) if tf else None,
        callPremToday=(((tf.get("today") or {}).get("calls") or {}).get("premium")) if tf else None,
        callHits5d=(((tf.get("window") or {}).get("calls") or {}).get("hits")) if tf else None,
        hour=int(at[11:13]) if len(at) >= 13 else None,
    )
    bs = bursts.get(r.get("symbol")) or []
    d0, hm = at[:10], at[11:16]
    try:
        prev = (datetime.strptime(d0, "%Y-%m-%d") - timedelta(days=4)).strftime("%Y-%m-%d")
    except ValueError:
        prev = d0
    f["burstSameDay"] = any(d == d0 and s_ <= hm for d, s_ in bs)
    f["burst2d"] = any(prev <= d < d0 for d, s_ in bs) or f["burstSameDay"]
    f.update(_more_features(r, at))
    return f


def _pct(a, b):
    try:
        return round((float(a) - float(b)) / float(b) * 100, 2) if a is not None and b else None
    except (TypeError, ValueError):
        return None


def _more_features(r, at):
    """Numeric values stored with the evaluation: trend, live gamma, trade plan, contract, flow alert, ticker volume, time."""
    sig, g, o = r.get("signals") or {}, r.get("gamma") or {}, r.get("origin") or {}
    tf, p = r.get("tickerFlow") or {}, r.get("proposal") or r.get("altProposal") or {}
    price = sig.get("price")
    w, td = tf.get("window") or {}, tf.get("today") or {}
    f = {}
    f["emaDist"] = _pct(price, sig.get("ema21"))
    f["atrPct"] = round(sig["atr21"] / price * 100, 2) if price and sig.get("atr21") else None
    f["crossAgo"] = sig.get("crossAgo")
    f["momentum"] = sig.get("momentum")
    f["momAccel"] = round(sig["momentum"] - sig["momentumPrev"], 4) if sig.get("momentum") is not None and sig.get("momentumPrev") is not None else None
    try:
        f["low30Age"] = (datetime.strptime(at[:10], "%Y-%m-%d") - datetime.strptime(sig["low30Date"][:10], "%Y-%m-%d")).days if sig.get("low30Date") else None
    except (ValueError, TypeError):
        f["low30Age"] = None
    f["flipDist"] = _pct(price, g.get("gammaFlip"))
    f["posGex"] = None if g.get("netGex") is None else g["netGex"] > 0
    f["chainPwDist"] = _pct(price, g.get("putWall"))
    f["chainCwRoom"] = _pct(g.get("callWall"), price)
    stp, tgt = p.get("underlyingStop"), p.get("underlyingTarget")
    f["stopDist"] = round((price - stp) / price * 100, 2) if stp and price else None
    f["targetDist"] = _pct(tgt, price) if tgt else None
    f["roomRatio"] = round((tgt - price) / (price - stp), 2) if f["targetDist"] is not None and f["stopDist"] and f["targetDist"] is not None and f["stopDist"] > 0 else None
    c = next((c for c in r.get("candidates") or [] if c.get("symbol") == p.get("contract")), None) or ((r.get("candidates") or [None])[0] or {})
    f["dte"], f["delta"], f["spreadPct"], f["oi"] = c.get("dte"), c.get("delta"), c.get("spreadPct"), c.get("oi")
    f["iv"] = round(c["iv"] * 100, 1) if c.get("iv") else None
    f["otmPct"] = _pct(c.get("strike"), price) if c.get("strike") else None
    f["ivEvent"] = bool(r.get("events")) if "events" in r else None
    flow = o.get("type") == "flow"
    f["flowAlerts"] = o.get("alerts") if flow else None
    f["topPremium"] = o.get("topPremium") if flow else None
    f["flowOptPrice"] = o.get("optPrice") if flow else None
    f["repeatRule"] = ("repeat" in (o.get("rule") or "").lower()) if flow else None
    f["flowLagMin"] = None
    if flow and o.get("at"):
        try:
            from util import utc_to_ny
            a = str(o["at"])[:19]
            t0 = datetime.strptime(a, "%Y-%m-%dT%H:%M:%S")
            if a > at[:19]:                            # alert time in UTC
                t0 = utc_to_ny(t0).replace(tzinfo=None)
            f["flowLagMin"] = max(0, round((datetime.strptime(at[:19], "%Y-%m-%dT%H:%M:%S") - t0).total_seconds() / 60))
        except Exception:
            pass
    if tf:
        cw_, ct_ = (w.get("calls") or {}).get("premium"), (td.get("calls") or {}).get("premium")
        pt_ = (td.get("puts") or {}).get("premium")
        f["putPremToday"] = pt_
        f["callsOverPutsToday"] = None if ct_ is None else (ct_ or 0) > (pt_ or 0)
        f["todayShare"] = round((ct_ or 0) / cw_ * 100, 1) if cw_ else None
        f["bigCalls5d"] = (w.get("bigCalls") or {}).get("hits")
        f["bigPuts5d"] = w.get("bigPuts")
        f["flowDays"] = (w.get("calls") or {}).get("days")
        f["callContracts5d"] = (w.get("calls") or {}).get("contracts")
        f["repeatQualifies"] = tf.get("repeatQualifies")
    try:
        d = datetime.strptime(at[:16], "%Y-%m-%dT%H:%M")
        f["weekday"] = d.isoweekday()
        f["minsOpen"] = (d.hour * 60 + d.minute) - 570
    except ValueError:
        pass
    return f


def _result(r):
    """(R, status) for an evaluation: the real trade if taken, otherwise its shadow result."""
    if r.get("orderId") or r.get("status") in TAKEN:
        if not r.get("fillPrice"):
            return None, None
        if r.get("realizedR") is not None:
            return r["realizedR"], "done"
        if r.get("riskAtFill") and r.get("lastPl") is not None:
            return round(r["lastPl"] / r["riskAtFill"], 2), "done" if r.get("status") == "closed" else "open"
        return None, None
    sh = r.get("shadow") or {}
    if sh.get("status") in ("done", "tracking") and sh.get("R") is not None:
        return sh["R"], "done" if sh["status"] == "done" else "open"
    return None, None


def rows(sub, days=35):
    pk = db.upk(sub)
    cut = (now_ny() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    bursts = bursts_by_ticker(pk)
    out, walls = [], {}
    import flowdata
    for r in db.q_prefix(pk, "BOT#"):
        at = r.get("createdAt") or ""
        if at < cut or (r.get("shadow") or {}).get("status") == "dup":
            continue
        R, st = _result(r)
        if R is None:
            continue
        f = {"id": r["id"], "symbol": r.get("symbol"), "at": at, "R": R, "status": st, "decision": r.get("decision")}
        p = r.get("proposal") or {}
        f["_path"] = (r.get("shadow") or {}).get("path") or r.get("path")
        f["_entry"] = float(p.get("limit") or p.get("debit") or 0)
        f["_prop"] = {k: p.get(k) for k in ("exp", "underlyingStop", "underlyingTarget", "strategy", "width", "debit", "limit")}
        cand = next((c for c in r.get("candidates") or [] if c.get("symbol") == p.get("contract")), None) or ((r.get("candidates") or [None])[0] or {})
        f["_spread"] = cand.get("spreadPct")                     # bid/ask spread of the contract when evaluated, % of mid
        f.update(features_of(r, bursts))
        sym = r.get("symbol")
        if sym and sym not in walls:
            try:
                walls[sym] = flowdata.stored_walls(sym)
            except Exception:
                walls[sym] = {}
        f.update(wall_features(r, walls.get(sym) or {}))
        out.append(f)
    return out


def _match(row, cond):
    v = row.get(cond["key"])
    if v is None:
        return False
    if cond.get("op") in (">=", "<="):
        try:
            x = float(cond["value"])
        except (TypeError, ValueError):
            return False
        return v >= x if cond["op"] == ">=" else v <= x
    return bool(v) == bool(cond.get("value", True))


def stats(rs):
    n = len(rs)
    if not n:
        return {"n": 0, "done": 0, "open": 0, "winPct": None, "avgR": None, "totalR": None}
    Rs = [r["R"] for r in rs]
    return {"n": n, "done": sum(1 for r in rs if r["status"] == "done"), "open": sum(1 for r in rs if r["status"] != "done"),
            "winPct": round(sum(1 for x in Rs if x > 0) / n * 100), "avgR": round(sum(Rs) / n, 2), "totalR": round(sum(Rs), 2)}


def describe(cond):
    label = FEATURES.get(cond["key"], (cond["key"],))[0]
    if cond.get("op") in (">=", "<="):
        v = cond["value"]
        vs = f"{v:,.0f}" if isinstance(v, (int, float)) and abs(v) >= 1000 else f"{v:g}" if isinstance(v, (int, float)) else str(v)
        return f"{label} {'≥' if cond['op'] == '>=' else '≤'} {vs}"
    return label if cond.get("value", True) else f"NOT: {label}"


def filter_rows(rs, conds):
    return [r for r in rs if all(_match(r, c) for c in conds)]


def halves(rs):
    """Average R of the older half and the newer half of the matching ideas (by evaluation time)."""
    s = sorted(rs, key=lambda r: r["at"])
    h = len(s) // 2
    a, b = s[:h], s[h:]
    avg = lambda xs: round(sum(x["R"] for x in xs) / len(xs), 2) if xs else None
    return avg(a), avg(b)


def candidates():
    out = []
    for key, (label, kind, choices) in FEATURES.items():
        if key == "taken":
            continue
        if kind == "bool":
            out += [{"key": key, "value": True}, {"key": key, "value": False}]
        else:
            out += [{"key": key, "op": op, "value": v} for op, v in choices]
    return out


def _group_keys(group):
    if group == "walls":
        return set(WALL_KEYS), set(WALL_KEYS)                       # (allowed, required)
    if group == "flow":
        return set(FLOW_KEYS), set(FLOW_KEYS)
    if group == "wallsflow":
        return set(WALL_KEYS) | set(FLOW_KEYS), set(WALL_KEYS)
    if group == "gamma":
        return set(GAMMA_KEYS), set(GAMMA_KEYS)
    if group == "trend":
        return set(TREND_KEYS), set(TREND_KEYS)
    if group == "trendflow":
        return set(TREND_KEYS) | set(FLOW_KEYS), set(TREND_KEYS)
    return None, None


def search(rs, min_n=15, max_depth=3, top=25, group=None, beam=250):
    """Best combinations of up to `max_depth` conditions by average R (at least min_n ideas), with the half-split check.
    Pairs are tried exhaustively over the most promising singles; deeper levels extend the best `beam` combinations of the
    level before with every condition (beam search), so 4, 5 or 6 conditions stay fast.
    group: only combinations made of that group's conditions (at least one of the group's own)."""
    base = stats(rs)
    allowed, required = _group_keys(group)
    Rs = [r["R"] for r in rs]
    idx = {r["id"]: i for i, r in enumerate(rs)}

    nb = (len(Rs) + 7) // 8
    tab = []                                     # sum of R for each byte value of each 8-row chunk
    for c in range(nb):
        part = Rs[c * 8:c * 8 + 8]
        tab.append([sum(part[j] for j in range(len(part)) if v >> j & 1) for v in range(256)])

    def summ(mask):
        n = bin(mask).count("1")
        if n < min_n:
            return n, 0.0
        return n, sum(t[b] for t, b in zip(tab, mask.to_bytes(nb, "little")) if b)

    singles = []
    for c in candidates():
        if allowed and c["key"] not in allowed:
            continue
        mask = 0
        for r in rs:
            if _match(r, c):
                mask |= 1 << idx[r["id"]]
        n, tot = summ(mask)
        if n >= min_n:
            singles.append((c, mask, tot / n))
    singles.sort(key=lambda x: -x[2])
    pool = singles[:60]
    if required:
        pool += [x for x in singles[60:] if x[0]["key"] in required][:20]
    found = {}

    def add(conds, mask):
        n, tot = summ(mask)
        if n < min_n:
            return None
        key = tuple(sorted((c["key"], c.get("op"), str(c.get("value"))) for c in conds))
        if key not in found:
            found[key] = (conds, mask, tot / n)
        return tot / n

    level = []
    for c, mask, avg in singles:
        if not required or c["key"] in required:
            add([c], mask)
        level.append(([c], mask, avg))
    level = [x for x in level if any(x[0][0] is p_[0] for p_ in pool)]
    tested = len(singles)
    for depth in range(2, max(2, min(6, max_depth)) + 1):
        nxt = []
        for conds, mask, _ in level:
            keys = {c["key"] for c in conds}
            for c, m2, _ in pool:
                if c["key"] in keys:
                    continue
                if depth == 2 and id(c) <= id(conds[0]):
                    continue                     # each pair once
                mm = mask & m2
                if mm == mask or mm == m2 and depth > 2:
                    continue                     # adds nothing
                tested += 1
                cc = conds + [c]
                if required and not any(x["key"] in required for x in cc):
                    n, tot = summ(mm)
                    if n >= min_n:
                        nxt.append((cc, mm, tot / n))
                    continue
                avg = add(cc, mm)
                if avg is not None:
                    nxt.append((cc, mm, avg))
        if not nxt:
            break
        nxt.sort(key=lambda x: -x[2])
        seen, level = set(), []
        for x in nxt:                            # keep the best distinct selections for the next level
            if x[1] in seen:
                continue
            seen.add(x[1])
            level.append(x)
            if len(level) >= beam:
                break
    res = []
    best = sorted(found.values(), key=lambda x: (-x[2], len(x[0])))
    shown, picked = set(), []
    for x in best:                               # full stats only for the best distinct selections
        if x[1] in shown:
            continue
        shown.add(x[1])
        picked.append(x)
        if len(picked) >= top * 3:
            break
    for conds, mask, _ in picked:
        m = [rs[i] for i in range(len(rs)) if mask >> i & 1]
        s = stats(m)
        h1, h2 = halves(m)
        res.append({"conds": conds, "text": " + ".join(describe(c) for c in conds), **s, "older": h1, "newer": h2, "size": len(conds),
                    "holds": h1 is not None and h2 is not None and h1 > (base["avgR"] or 0) and h2 > (base["avgR"] or 0)})
    res.sort(key=lambda x: (-(x["avgR"] or -9), -x["n"], x["size"]))
    seen, uniq = set(), []
    for x in res:                                # skip combos that select exactly the same ideas as a better (shorter) one
        sig = (x["n"], x["totalR"])
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(x)
        if len(uniq) >= top:
            break
    return {"baseline": base, "results": uniq, "minN": min_n, "tested": len(found), "group": group, "depth": max_depth,
            "known": {k: sum(1 for r in rs if r.get(k) is not None) for k in (required or ())}}


def explore(sub, days=35, conds=None, do_search=False, min_n=15, group=None, depth=3):
    rs = rows(sub, days)
    conds = [c for c in (conds or []) if c.get("key") in FEATURES]
    m = filter_rows(rs, conds)
    h1, h2 = halves(m)
    out = {"days": days, "all": stats(rs), "match": stats(m), "older": h1, "newer": h2,
           "conds": [{**c, "text": describe(c)} for c in conds],
           "features": [{"key": k, "label": v[0], "kind": v[1], "cat": CAT_OF.get(k, "Other"),
                         "known": sum(1 for r in rs if r.get(k) is not None)} for k, v in FEATURES.items()],
           "cats": [c for c, _ in CATS],
           "items": sorted([{k: r[k] for k in ("id", "symbol", "at", "R", "status", "decision", "taken")} for r in m],
                           key=lambda r: -r["R"])[:200]}
    if do_search:
        out["search"] = search(rs, min_n=min_n, max_depth=depth, group=group if group in GROUPS else None)
    run = db.get(db.upk(sub), "WALLSRUN") or {}
    out["walls"] = {"known": sum(1 for r in rs if r.get("pwDist") is not None), "of": len(rs),
                    **{k: run.get(k) for k in ("at", "running", "stored", "left", "tickers", "errors")}}
    return out


# ---------------- Monte Carlo ----------------

def monte_carlo(sub, days=35, conds=None, trades=100, runs=5000, risk=None, account=None, seed=None):
    """Resample the R results of the matching ideas (with replacement) to simulate `trades` future trades, `runs` times.
    Also a permutation test: how often does a random group of the same size, drawn from all ideas, do as well?"""
    import random
    rnd = random.Random(seed)
    rs = rows(sub, days)
    m = filter_rows(rs, [c for c in (conds or []) if c.get("key") in FEATURES])
    done = [r["R"] for r in m if r["status"] == "done"]
    note = None
    sample = done
    if len(done) < 15:
        sample = [r["R"] for r in m]
        note = f"Only {len(done)} finished ideas: open ideas are included at their current result."
    if len(sample) < 5:
        return {"error": f"Not enough ideas to simulate ({len(sample)}). Loosen the conditions or wait for more results."}
    trades, runs = max(10, min(int(trades), 500)), max(500, min(int(runs), 10000))
    totals, dds, streaks = [], [], []
    marks = [i for i in range(0, trades + 1, max(1, trades // 50))]
    if marks[-1] != trades:
        marks.append(trades)
    paths = [[] for _ in marks]
    for _ in range(runs):
        eq = peak = dd = 0.0
        streak = worst = 0
        mi = 0
        if marks[0] == 0:
            paths[0].append(0.0)
            mi = 1
        for i in range(1, trades + 1):
            x = rnd.choice(sample)
            eq += x
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
            streak = streak + 1 if x <= 0 else 0
            worst = max(worst, streak)
            if mi < len(marks) and marks[mi] == i:
                paths[mi].append(eq)
                mi += 1
        totals.append(eq)
        dds.append(dd)
        streaks.append(worst)
    q = lambda xs, p: sorted(xs)[min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))]
    fan = [{"trade": t, "p5": round(q(v, 5), 2), "p25": round(q(v, 25), 2), "p50": round(q(v, 50), 2), "p75": round(q(v, 75), 2),
            "p95": round(q(v, 95), 2)} for t, v in zip(marks, paths)]
    lo, hi = min(totals), max(totals)
    bins = 24
    width = (hi - lo) / bins or 1
    hist = [0] * bins
    for t in totals:
        hist[min(bins - 1, int((t - lo) / width))] += 1
    # permutation test on the average R
    allR = [r["R"] for r in rs if (r["status"] == "done" or note)]
    obs = sum(sample) / len(sample)
    perm = None
    if len(allR) > len(sample) >= 5:
        k, better = len(sample), 0
        for _ in range(2000):
            if sum(rnd.sample(allR, k)) / k >= obs:
                better += 1
        perm = round(better / 2000 * 100, 1)
    out = {
        "n": len(sample), "finished": len(done), "note": note, "trades": trades, "runs": runs, "avgR": round(obs, 3),
        "winPct": round(sum(1 for x in sample if x > 0) / len(sample) * 100),
        "total": {"p5": round(q(totals, 5), 1), "p50": round(q(totals, 50), 1), "p95": round(q(totals, 95), 1)},
        "probPositive": round(sum(1 for t in totals if t > 0) / runs * 100, 1),
        "drawdown": {"p50": round(q(dds, 50), 1), "p95": round(q(dds, 5), 1)},
        "losingStreak": {"p50": q(streaks, 50), "p95": q(streaks, 95)},
        "fan": fan, "hist": {"from": round(lo, 2), "width": round(width, 3), "counts": hist},
        "randomAsGood": perm, "conds": [{**c, "text": describe(c)} for c in (conds or []) if c.get("key") in FEATURES],
    }
    if risk:
        out["dollars"] = {"risk": risk, "p5": round(out["total"]["p5"] * risk), "p50": round(out["total"]["p50"] * risk),
                          "p95": round(out["total"]["p95"] * risk), "dd50": round(out["drawdown"]["p50"] * risk),
                          "dd95": round(out["drawdown"]["p95"] * risk)}
        if account:
            ruin_lvl = -0.5 * account / risk                  # losing half the account, in R
            out["dollars"]["halfAccountPct"] = round(sum(1 for d in dds if d <= ruin_lvl) / runs * 100, 1)
            out["dollars"]["account"] = account
    return out


# ---------------- best exit ----------------

def exit_grid():
    out = []
    for stop in (20, 30, 40, 50, 60):
        for target in (None, 40, 60, 80, 100, 150):
            for trail in ((None,) if target is None else (None, 15, 25, 35)):
                for max_days in (None, 5, 10):
                    for inv in (True, False):
                        out.append({"stop": stop, "target": target, "trail": trail, "maxDays": max_days, "invalidation": inv})
    return out


def describe_exit(e):
    parts = [f"stop -{e['stop']:g}%"]
    if e.get("target") is None:
        parts.append("no target")
    elif e.get("trail"):
        parts.append(f"at +{e['target']:g}% trail {e['trail']:g}%")
    else:
        parts.append(f"sell at +{e['target']:g}%")
    parts.append(f"max {e['maxDays']} days" if e.get("maxDays") else "hold up to 20 days")
    parts.append("invalidation on" if e.get("invalidation", True) else "no invalidation exit")
    return " · ".join(parts)


def best_exit(sub, days=35, conds=None, min_n=15, top=10, default_spread=10.0, spread_mult=1.0):
    """Replay every exit-rule combination on the stored price paths of the matching ideas; rank by average R."""
    import autotrader
    import shadow
    cfg = autotrader.settings(sub)
    rs = filter_rows(rows(sub, days), [c for c in (conds or []) if c.get("key") in FEATURES])
    ideas = [r for r in rs if r.get("_path") and r.get("_entry", 0) > 0]
    if len(ideas) < min_n:
        return {"error": f"Only {len(ideas)} matching ideas have a stored price path (need {min_n}). Paths are saved by "
                         f"the 16:20 update: press Update now on Skipped & what-if, or loosen the conditions."}
    ideas.sort(key=lambda r: r["at"])
    half = len(ideas) // 2
    sp = lambda r: (r.get("_spread") if r.get("_spread") is not None else default_spread) * spread_mult
    avg_spread = round(sum(sp(r) for r in ideas) / len(ideas), 1)
    def run(params):
        res = [shadow.sim_path(r["_path"], r["_entry"], r["_prop"],
                               {**params, "timeStopDte": cfg["timeStopDte"], "halfSpread": sp(r) / 200}) for r in ideas]
        Rs = [x["R"] for x in res]
        a, b = Rs[:half], Rs[half:]
        return {"n": len(Rs), "avgR": round(sum(Rs) / len(Rs), 3), "winPct": round(sum(1 for x in Rs if x > 0) / len(Rs) * 100),
                "totalR": round(sum(Rs), 2), "older": round(sum(a) / len(a), 2) if a else None, "newer": round(sum(b) / len(b), 2) if b else None,
                "avgDays": round(sum(x["days"] for x in res) / len(res), 1)}
    cur_p = shadow.params_of(cfg, {}, 1.0)
    current = {"params": {k: cur_p[k] for k in ("stop", "target", "trail", "maxDays", "invalidation")}, **run(cur_p)}
    current["text"] = describe_exit(current["params"])
    res = []
    for e in exit_grid():
        st = run(e)
        res.append({"params": e, "text": describe_exit(e), **st, "tight": e["stop"] < 2 * avg_spread,
                    "holds": st["older"] is not None and st["newer"] is not None and st["older"] > current["older"] and st["newer"] > current["newer"]})
    res.sort(key=lambda x: (-x["avgR"], -x["n"]))
    return {"n": len(ideas), "tested": len(res), "current": current, "results": res[:top], "avgSpread": avg_spread,
            "spreadKnown": sum(1 for r in ideas if r.get("_spread") is not None), "spreadMult": spread_mult,
            "conds": [{**c, "text": describe(c)} for c in (conds or []) if c.get("key") in FEATURES]}
