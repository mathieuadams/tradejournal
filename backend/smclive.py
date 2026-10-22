"""Smart money, live on the day-trading paper account (the second Alpaca paper account, daytrade.CRED_SK).

Every minute (the "daytick" schedule, 09:00-15:59 ET, fired at the start of each minute):
  1. the 1-minute candles of today up to the candle that just closed (waits up to ~12 s for it to be published);
     5-minute strategy: only the 5-minute candles that are complete;
  2. smc.detect() on them with the saved blocks: the same code as the chart, the stress test and the optimizer;
  3. orders, one setup at a time per ticker:
       * a setup waiting for its retracement -> a LIMIT order at the entry (FVG edge / middle / order block),
         cancelled when the engine gives the setup up (ran away, no retrace in time, filter failed, past endT);
       * market entry -> a market order right after the candle that completes the setup;
       * once filled -> exit orders at the broker: OCO (take profit + stop) for TP1 (the partial) and for the rest
         at TP2; when the engine moves the stop (breakeven at TP1, breakeven at xR, trail) the OCO is replaced;
       * an early exit of the engine (FVG fails, opposite MSS, VWAP, time stop) or the flat time -> exit orders
         cancelled and the position closed at market.
Each action stores the candle it acted on and the seconds between that candle's close and the order."""
import time
from datetime import datetime, timedelta

import db
import smc
from util import iso, now_ny

SK = "SMCLIVE"
POS = "SMCPOS#"
DEFAULT = {"enabled": False, "trade": False, "tickers": ["QQQ", "SPY"], "riskUsd": 100.0, "qty": 0, "maxQty": 5000,
           # risk management from the account value (equity of the paper account, read every minute)
           "riskMode": "pct", "riskPct": 0.5, "maxPosPct": 50.0, "openRiskPct": 1.0, "dayLossPct": 2.0, "maxTrades": 4,
           "lossStreak": 3, "flattenOnLimit": True,
           "cfg": {}, "name": "", "savedAt": None, "source": None}


def config(sub):
    r = db.get(db.upk(sub), SK) or {}
    out = {k: r.get(k, v) for k, v in DEFAULT.items()}
    out["cfg"] = smc.clean(out.get("cfg") or {})
    return out


def save(sub, b):
    cur = config(sub)
    b = b or {}
    for k in ("enabled", "trade"):
        if k in b:
            cur[k] = bool(b[k])
    if "tickers" in b:
        tk = b["tickers"]
        tk = tk.replace(",", " ").split() if isinstance(tk, str) else (tk or [])
        cur["tickers"] = [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:6] or ["QQQ", "SPY"]
    for k, lo, hi in (("riskUsd", 0, 100000), ("qty", 0, 100000), ("maxQty", 1, 100000), ("riskPct", 0.01, 5), ("maxPosPct", 1, 400),
                      ("openRiskPct", 0.01, 20), ("dayLossPct", 0, 50), ("maxTrades", 0, 50), ("lossStreak", 0, 50)):
        if b.get(k) is not None and b.get(k) != "":
            cur[k] = max(lo, min(hi, float(b[k]) if k in ("riskUsd", "riskPct", "maxPosPct", "openRiskPct", "dayLossPct") else int(float(b[k]))))
    if b.get("riskMode") in ("pct", "usd"):
        cur["riskMode"] = b["riskMode"]
    if "flattenOnLimit" in b:
        cur["flattenOnLimit"] = bool(b["flattenOnLimit"])
    if isinstance(b.get("cfg"), dict):                 # a new strategy saved from the page
        cur["cfg"] = smc.clean(b["cfg"])
        cur["name"] = str(b.get("name") or "")[:60]
        cur["savedAt"] = iso(now_ny())
        cur["source"] = b.get("source") if isinstance(b.get("source"), dict) else None
    db.put({"PK": db.upk(sub), "SK": SK, **cur})
    return view(sub)


def view(sub):
    import daytrade
    c = config(sub)
    acct = db.get(db.upk(sub), daytrade.CRED_SK)
    out = {"config": c, "connected": bool(acct), "feed": (acct or {}).get("feed")}
    dt = daytrade.config(sub)
    if dt.get("enabled") and dt.get("trade") and set(dt.get("tickers") or []) & set(c["tickers"]) and not dt.get("inplay"):
        out["warn"] = "The day-trading bot also trades " + ", ".join(sorted(set(dt["tickers"]) & set(c["tickers"]))) + \
                      " on this account: one position per ticker, whoever is first."
    today = now_ny().strftime("%Y-%m-%d")
    recs = sorted(db.q_prefix(db.upk(sub), POS), key=lambda r: r.get("t", ""))[-80:]
    out["trades"] = [{k: v for k, v in r.items() if k != "PK"} for r in recs]
    st = db.get(db.upk(sub), SK + "#TICK") or {}
    if acct:
        try:
            a = account(daytrade.creds(sub))
            if a:
                out["account"] = a
                recs_today = [r for r in recs if (r.get("t") or "")[:10] == today]
                rs = risk_state(c, a, recs_today)
                out["risk"] = {**rs, "openRisk": round(_open_risk(recs_today), 2), "perTrade": round(a["equity"] * c["riskPct"] / 100, 2) if c["riskMode"] == "pct" else c["riskUsd"],
                               "maxOpen": round(a["equity"] * c["openRiskPct"] / 100, 2), "dayLimit": round(a["start"] * c["dayLossPct"] / 100, 2)}
        except Exception as e:
            out["accountError"] = str(e)[:120]
    out["lastTick"] = st.get("last")
    out["ticks"] = (st.get("log") or [])[-15:]
    out["today"] = today
    return out


# ---------------------------------------------------------------- broker helpers
def _alp(c, method, path, body=None):
    import autotrader
    return autotrader._alp(c, method, path, body)


def _px(v):
    return round(float(v), 2)


def account(c):
    """{equity, start (yesterday's close equity), dayPct, buyingPower} of the paper account, or None."""
    a = _alp(c, "GET", "/v2/account") or {}
    try:
        eq = float(a["equity"])
    except (KeyError, TypeError, ValueError):
        return None
    start = float(a.get("last_equity") or eq) or eq
    return {"equity": eq, "start": start, "dayPct": round((eq - start) / start * 100, 3) if start else 0.0,
            "buyingPower": float(a.get("buying_power") or 0) or None}


def _open_risk(recs):
    """$ at risk right now: what each pending / open trade loses if its stop is hit (after breakeven: 0)."""
    out = 0.0
    for r in recs:
        if r.get("status") not in ("pending", "open"):
            continue
        q = float(r.get("filledQty") or r.get("qty") or 0)
        px = float(r.get("fill") or r.get("entry"))
        out += max(0.0, (px - float(r.get("stopNow") or r["stop"])) * r["dir"]) * q
    return out


def risk_state(cfg, acct, day_recs):
    """The day's limits: -> {"ok": bool, "why": reason when new trades are blocked, "flatten": bool, ...}."""
    st = {"ok": True, "why": None, "flatten": False}
    done = [r for r in day_recs if r.get("status") not in ("canceled",) and (r.get("filledQty") or r.get("status") in ("open", "closing", "closed"))]
    st["tradesToday"] = len(done)
    closed = sorted((r for r in day_recs if r.get("status") == "closed" and r.get("R") is not None), key=lambda r: r.get("closedAt") or "")
    streak = 0
    for r in reversed(closed):
        if r["R"] < 0:
            streak += 1
        else:
            break
    st["lossStreak"] = streak
    if cfg["riskMode"] == "pct" and not acct:
        st.update(ok=False, why="account value not available")
        return st
    if acct:
        st["dayPct"] = acct["dayPct"]
        if cfg["dayLossPct"] and acct["dayPct"] <= -cfg["dayLossPct"]:
            st.update(ok=False, why=f"daily loss limit: {acct['dayPct']:.2f}% today (limit -{cfg['dayLossPct']:g}%)",
                      flatten=bool(cfg.get("flattenOnLimit")))
            return st
    if cfg["maxTrades"] and len(done) >= cfg["maxTrades"]:
        st.update(ok=False, why=f"{len(done)} trades today (max {cfg['maxTrades']})")
    elif cfg["lossStreak"] and streak >= cfg["lossStreak"]:
        st.update(ok=False, why=f"{streak} losses in a row today (stop after {cfg['lossStreak']})")
    return st


def size(cfg, s, acct, open_risk):
    """Shares for a setup: risk per trade (% of the account value, or a fixed $) / the risk per share, then capped by
    the position size (% of the account), the buying power, the total open risk and the max quantity.
    -> (qty, note): qty 0 = not taken."""
    per = abs(s["entry"] - s["stop"])
    if per <= 0:
        return 0, "no risk per share"
    eq = (acct or {}).get("equity")
    if cfg["riskMode"] == "pct":
        if not eq:
            return 0, "account value not available"
        budget = eq * cfg["riskPct"] / 100
    else:
        budget = float(cfg["riskUsd"] or 0) or per * float(cfg["qty"] or 1)
    notes = [f"risk ${budget:,.0f}" + (f" ({cfg['riskPct']:g}% of ${eq:,.0f})" if cfg["riskMode"] == "pct" else "")]
    q = int(budget // per)
    caps = []
    if eq:
        caps.append((int(eq * cfg["maxPosPct"] / 100 // s["entry"]), f"position ≤ {cfg['maxPosPct']:g}% of the account"))
        room = eq * cfg["openRiskPct"] / 100 - open_risk
        caps.append((int(max(0.0, room) // per), f"open risk ≤ {cfg['openRiskPct']:g}% of the account (${open_risk:,.0f} already at risk)"))
    if acct and acct.get("buyingPower"):
        caps.append((int(acct["buyingPower"] * 0.95 // s["entry"]), "buying power"))
    caps.append((int(cfg["maxQty"]), f"max qty {cfg['maxQty']}"))
    for cap, why in caps:
        if cap < q:
            q = cap
            notes.append(f"capped by {why}")
    return max(0, q), "; ".join(notes)


def _oco(c, sym, side_exit, qty, tp, stop):
    return _alp(c, "POST", "/v2/orders", {"symbol": sym, "qty": str(qty), "side": side_exit, "type": "limit", "time_in_force": "day",
                                          "order_class": "oco", "take_profit": {"limit_price": str(_px(tp))},
                                          "stop_loss": {"stop_price": str(_px(stop))}}) or {}


def _cancel(c, oid):
    if oid:
        try:
            _alp(c, "DELETE", f"/v2/orders/{oid}")
        except Exception as e:
            print("cancel failed", oid, e)


def _place_exits(c, r, stop):
    """OCO exit orders for what is still held: the partial at TP1 (until it is taken), the rest at TP2."""
    sym, qty, up = r["symbol"], int(r["filledQty"]), r["dir"] > 0
    side_exit = "sell" if up else "buy"
    ids = []
    part_q = int(qty * float(r.get("part") or 0)) if not r.get("tp1Done") else 0
    rest = qty - part_q
    if part_q > 0:
        ids.append(_oco(c, sym, side_exit, part_q, r["tp1"], stop).get("id"))
    if rest > 0:
        ids.append(_oco(c, sym, side_exit, rest, r["tp2"] if float(r.get("part") or 0) else r["tp1"], stop).get("id"))
    return [i for i in ids if i]


def _held(c, sym):
    p = _alp(c, "GET", f"/v2/positions/{sym}")
    return int(float(p["qty"])) if p and p.get("qty") else 0


# ---------------------------------------------------------------- the minute
def _bars_now(sub, sym, now, tf):
    """Today's 1-minute candles up to the one that closed at `now` (waits a little for it to be published)."""
    import daytrade
    cut = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    waited = 0.0
    for k in range(5):
        bs = [b for b in daytrade.minute_bars(sub, sym, 3, now) if b["t"] <= cut]
        if bs and bs[-1]["t"] == cut:
            return bs, waited, True
        if k < 4:
            time.sleep(3)
            waited += 3
    return bs, waited, False                            # no candle for that minute (no trade on the feed, or late)


def _close_t(t, tf):
    d = datetime.strptime(t[:16], "%Y-%m-%dT%H:%M") + timedelta(minutes=tf)
    return d


def tick(sub, now=None):
    cfg = config(sub)
    if not cfg["enabled"]:
        return {"skipped": "off"}
    import daytrade
    c = daytrade.creds(sub)
    if not c:
        return {"skipped": "no day-trading account"}
    real = now is None
    now = now or now_ny().replace(tzinfo=None, second=0, microsecond=0)
    t_start = now_ny().replace(tzinfo=None)
    today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    if now.weekday() >= 5 or not ("09:31" <= hm <= "16:00"):
        return {"skipped": "market closed"}
    P = cfg["cfg"]
    tf = P["tf"]
    pk = db.upk(sub)
    acts, log = [], []
    try:
        acct = account(c)
    except Exception as e:
        acct = None
        acts.append(f"account: {str(e)[:100]}")
    day_recs = [r for r in db.q_prefix(pk, POS) if (r.get("t") or "")[:10] == today]
    risk = risk_state(cfg, acct, day_recs)
    if risk["flatten"]:                                # daily loss limit hit: everything of the strategy closed
        for r in [r for r in day_recs if r.get("status") in ("pending", "open")]:
            for oid in [r.get("orderId")] + list(r.get("exitIds") or []):
                _cancel(c, oid)
            if r["status"] == "open":
                time.sleep(0.5)
                _alp(c, "DELETE", f"/v2/positions/{r['symbol']}")
            db.update(pk, r["SK"], {"status": "closing" if r["status"] == "open" else "canceled", "exitWhy": risk["why"],
                                    "why": risk["why"], "events": (r.get("events") or []) + [f"closed: {risk['why']}"]})
            acts.append(f"{r['symbol']} closed: {risk['why']}")
    for sym in cfg["tickers"]:
        try:
            bs, waited, fresh = _bars_now(sub, sym, now, tf)
            if not bs:
                acts.append(f"{sym}: no candles")
                continue
            out = smc.detect(bs, P, [today])
            bars = out.get("bars") or []
            last = bars[-1]["t"] if bars else None
            if not last:
                continue
            new_candle = last != (db.get(pk, f"{SK}#LAST#{sym}") or {}).get("t")
            db.put({"PK": pk, "SK": f"{SK}#LAST#{sym}", "t": last})
            lag = lambda: round((now_ny().replace(tzinfo=None) - _close_t(last, tf)).total_seconds(), 1)
            log.append({"sym": sym, "candle": last, "closedAt": _close_t(last, tf).strftime("%H:%M:%S"), "fresh": fresh, "waited": waited,
                        "newCandle": new_candle})
            recs = {r["t"]: r for r in db.q_prefix(pk, f"{POS}{sym}#{today}")}
            active = [r for r in recs.values() if r.get("status") in ("pending", "open")]
            flat = hm >= P["flatT"]
            setups = {s["t"]: s for s in out["setups"]}
            # 1. what is already at the broker
            for r in active:
                s = setups.get(r["t"])
                acts += _manage(c, pk, r, s, last, tf, flat, P, lag)
            active = [r for r in db.q_prefix(pk, f"{POS}{sym}#{today}") if r.get("status") in ("pending", "open")]
            last_end = max(P["endT"], P["endT2"]) if P.get("win2On") else P["endT"]
            if active or flat or not cfg["trade"] or hm >= last_end:
                continue
            if not risk["ok"]:
                if any(s["t"] not in recs and _waiting(s, last, tf, P) for s in out["setups"]):
                    acts.append(f"{sym}: setup not taken: {risk['why']}")
                continue
            # 2. a new setup on the candle that just closed
            for s in out["setups"]:
                if s["t"] in recs:
                    continue
                pending = _waiting(s, last, tf, P)
                market_now = P["entryAt"] == "market" and s.get("fillT") == last and s.get("armedT") == last
                if not (pending or market_now):
                    continue
                if _held(c, sym):
                    acts.append(f"{sym}: setup {s['t'][11:16]} skipped (a position is already held)")
                    break
                all_recs = [r for r in db.q_prefix(pk, POS) if (r.get("t") or "")[:10] == today]
                q, size_note = size(cfg, s, acct, _open_risk(all_recs))
                if q < 1:
                    acts.append(f"{sym}: setup {s['t'][11:16]} not taken: {size_note}")
                    db.put({"PK": pk, "SK": f"{POS}{sym}#{s['t']}", "symbol": sym, "t": s["t"], "dir": s["dir"], "qty": 0, "entry": s["entry"],
                            "stop": s["stop"], "tp1": s["tp1"], "tp2": s["tp2"], "status": "canceled", "why": f"size 0: {size_note}",
                            "candle": last, "sentAt": iso(now_ny()), "events": [f"not taken: {size_note}"]})
                    break
                side = "buy" if s["dir"] > 0 else "sell"
                if market_now:
                    o = _alp(c, "POST", "/v2/orders", {"symbol": sym, "qty": str(q), "side": side, "type": "market", "time_in_force": "day"}) or {}
                else:
                    o = _alp(c, "POST", "/v2/orders", {"symbol": sym, "qty": str(q), "side": side, "type": "limit", "time_in_force": "day",
                                                       "limit_price": str(_px(s["entry"]))}) or {}
                rec = {"PK": pk, "SK": f"{POS}{sym}#{s['t']}", "symbol": sym, "t": s["t"], "dir": s["dir"], "qty": q,
                       "entry": s["entry"], "stop": s["stop"], "tp1": s["tp1"], "tp2": s["tp2"], "tp1Name": s.get("tp1Name"),
                       "tp2Name": s.get("tp2Name"), "part": s.get("part", P["partial"] if P["partialOn"] else 0), "pool": s.get("pool"),
                       "kind": "market" if market_now else "limit", "orderId": o.get("id"), "status": "pending", "candle": last,
                       "sentAt": iso(now_ny()), "lagS": lag(), "sizing": size_note, "equity": (acct or {}).get("equity"),
                       "events": [f"size {q}: {size_note}", f"{'market' if market_now else 'limit'} {side} {q} @ {s['entry']} "
                                                                           f"(candle {last[11:16]}, sent {lag()}s after its close)"]}
                if P["partialOn"] is False:
                    rec["part"] = 0
                db.put(rec)
                acts.append(f"{sym} {'LONG' if s['dir'] > 0 else 'SHORT'} {q} {rec['kind']} @ {s['entry']} · stop {s['stop']} · lag {rec['lagS']}s")
                break
        except Exception as e:
            acts.append(f"{sym}: {str(e)[:160]}")
    if real:
        st = db.get(pk, SK + "#TICK") or {}
        entry = {"at": iso(now_ny()), "minute": hm, "equity": (acct or {}).get("equity"), "dayPct": (acct or {}).get("dayPct"), "blocked": risk["why"], "took": round((now_ny().replace(tzinfo=None) - t_start).total_seconds(), 1), "syms": log, "actions": acts}
        db.put({"PK": pk, "SK": SK + "#TICK", "last": entry, "log": ((st.get("log") or []) + [entry])[-60:] if acts or hm[-1] == "0" else (st.get("log") or [])[-60:]})
    return {"actions": acts}


def _waiting(s, last, tf, P):
    """The setup is armed and still waiting for its retracement (the window of entryBars candles not over)."""
    if s.get("fillT") or s.get("status") != "no retrace" or not s.get("armedT") or P["entryAt"] == "market":
        return False
    armed = datetime.strptime(s["armedT"][:16], "%Y-%m-%dT%H:%M")
    return last >= s["armedT"] and datetime.strptime(last[:16], "%Y-%m-%dT%H:%M") < armed + timedelta(minutes=tf * P["entryBars"]) \
        and _hm(last) < (smc.window_end(P, _hm(s["t"])) or P["endT"])


def _manage(c, pk, r, s, last, tf, flat, P, lag):
    acts = []
    sym = r["symbol"]
    upd = {}
    ev = list(r.get("events") or [])
    if r["status"] == "pending":
        o = _alp(c, "GET", f"/v2/orders/{r['orderId']}") if r.get("orderId") else None
        filled = int(float((o or {}).get("filled_qty") or 0))
        st = (o or {}).get("status")
        if filled and st == "filled" or (filled and st in ("canceled", "expired")):
            px = float(o.get("filled_avg_price") or r["entry"])
            upd.update(status="open", filledQty=filled, fill=px, filledAt=o.get("filled_at"), slip=round((px - r["entry"]) * r["dir"], 4))
            ev.append(f"filled {filled} @ {px:.2f} ({'+' if upd['slip'] <= 0 else ''}{-upd['slip']:.2f} vs the engine's {r['entry']})")
            r2 = {**r, **upd}
            stop = (s or {}).get("stopNow") or r["stop"]
            upd["exitIds"] = _place_exits(c, r2, stop)
            upd["stopNow"] = stop
            ev.append(f"exits placed: stop {stop} · TP1 {r['tp1']}" + (f" · TP2 {r['tp2']}" if float(r.get("part") or 0) else ""))
            acts.append(f"{sym} filled {filled} @ {px:.2f}")
        elif st in ("canceled", "expired", "rejected"):
            upd.update(status="canceled", why=f"order {st} at the broker")
        else:
            gone = None
            if flat or _hm(last) >= (smc.window_end(P, _hm(r["t"])) or P["endT"]):
                gone = "past the entry time window"
            elif s is None:
                gone = "the engine no longer sees the setup"
            elif not s.get("fillT") and not _waiting(s, last, tf, P):
                gone = s.get("status") or "given up"
            elif s.get("fillT") and s.get("exitT") and s.get("exitWhy") != "end of day":
                gone = f"the engine filled and exited already ({s['exitWhy']}) without a broker fill"
            if gone:
                _cancel(c, r.get("orderId"))
                o2 = _alp(c, "GET", f"/v2/orders/{r['orderId']}") if r.get("orderId") else None
                f2 = int(float((o2 or {}).get("filled_qty") or 0))
                if f2:                                 # filled in the meantime: keep it as a trade
                    upd.update(status="open", filledQty=f2, fill=float(o2.get("filled_avg_price") or r["entry"]))
                    upd["exitIds"] = _place_exits(c, {**r, **upd}, r["stop"])
                    upd["stopNow"] = r["stop"]
                else:
                    upd.update(status="canceled", why=gone)
                    ev.append(f"limit cancelled: {gone}")
                    acts.append(f"{sym} limit cancelled: {gone}")
    elif r["status"] == "open":
        held = _held(c, sym)
        if not held:
            px, why = _last_exit(c, sym, r)
            upd.update(status="closed", exitPx=px, exitWhy=why or "exit order filled", closedAt=iso(now_ny()))
            if px is not None and r.get("fill"):
                risk = abs(r["entry"] - r["stop"]) or 1
                upd["R"] = round(_realized_R(c, sym, r, risk), 3)
            ev.append(f"closed: {upd['exitWhy']}" + (f" @ {px:.2f}" if px else ""))
            acts.append(f"{sym} closed ({upd['exitWhy']})")
        else:
            early = None
            if flat:
                early = f"flat at {P['flatT']}"
            elif s and s.get("exitT") and s.get("exitWhy") not in ("end of day", "stop", "breakeven stop", "trailing stop", "TP1", "TP2"):
                early = s["exitWhy"]
            if early:
                for oid in r.get("exitIds") or []:
                    _cancel(c, oid)
                time.sleep(0.5)
                o = _alp(c, "DELETE", f"/v2/positions/{sym}") or {}
                upd.update(status="closing", exitWhy=early, exitOrder=o.get("id"), closedAt=iso(now_ny()))
                ev.append(f"early exit at market: {early} (candle {last[11:16]}, {lag()}s after its close)")
                acts.append(f"{sym} closed at market: {early}")
            else:
                if s and s.get("tp1Hit") and not r.get("tp1Done") and float(r.get("part") or 0):
                    upd["tp1Done"] = True
                stop = (s or {}).get("stopNow")
                if stop and abs(float(stop) - float(r.get("stopNow") or r["stop"])) >= 0.01:
                    for oid in r.get("exitIds") or []:
                        _cancel(c, oid)
                    time.sleep(0.5)
                    r2 = {**r, **upd, "filledQty": held}
                    upd["exitIds"] = _place_exits(c, r2, stop)
                    upd["stopNow"] = stop
                    ev.append(f"stop moved to {stop} (candle {last[11:16]})")
                    acts.append(f"{sym} stop → {stop}")
    elif r["status"] == "closing" and not _held(c, sym):
        px, _ = _last_exit(c, sym, r)
        upd.update(status="closed", exitPx=px)
        if px is not None and r.get("fill"):
            upd["R"] = round(_realized_R(c, sym, r, abs(r["entry"] - r["stop"]) or 1), 3)
    if s and s.get("R") is not None and s.get("exitWhy") != "end of day":
        upd["engineR"] = s["R"]                        # what the engine says the trade made (vs the broker)
    if upd:
        upd["events"] = ev[-20:]
        db.update(pk, r["SK"], upd)
    return acts


def _hm(t):
    return t[11:16]


def _orders_after(c, sym, r):
    after = (r.get("filledAt") or r.get("sentAt") or "")[:19]
    import urllib.parse
    q = urllib.parse.urlencode({"status": "closed", "symbols": sym, "after": after + "Z" if after and "Z" not in after and "+" not in after else after,
                                "direction": "asc", "limit": 100, "nested": "true"})
    os_ = _alp(c, "GET", f"/v2/orders?{q}") or []
    flat = []
    for o in os_:
        flat.append(o)
        flat += o.get("legs") or []
    side_exit = "sell" if r["dir"] > 0 else "buy"
    return [o for o in flat if o.get("side") == side_exit and float(o.get("filled_qty") or 0) > 0]


def _last_exit(c, sym, r):
    try:
        xs = _orders_after(c, sym, r)
    except Exception:
        return None, None
    if not xs:
        return None, None
    x = sorted(xs, key=lambda o: o.get("filled_at") or "")[-1]
    why = "stop" if x.get("type") in ("stop", "stop_limit") else "target" if x.get("type") == "limit" else "market"
    return float(x.get("filled_avg_price") or 0), why


def _realized_R(c, sym, r, risk):
    try:
        xs = _orders_after(c, sym, r)
    except Exception:
        xs = []
    q_all = sum(float(o["filled_qty"]) for o in xs) or float(r.get("filledQty") or 1)
    pnl = sum((float(o["filled_avg_price"]) - float(r["fill"])) * r["dir"] * float(o["filled_qty"]) for o in xs)
    return pnl / q_all / risk if q_all else 0.0


def users():
    return [p["PK"][5:] for p in db.scan_sk(SK) if p.get("enabled")]
