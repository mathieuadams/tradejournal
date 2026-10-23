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
from util import BadRequest, iso, now_ny

SK = "SMCLIVE"
POS = "SMCPOS#"
DEFAULT = {"enabled": False, "trade": False, "tickers": ["QQQ", "SPY"], "riskUsd": 100.0, "qty": 0, "maxQty": 5000,
           # risk management from the account value (equity of the paper account, read every minute)
           "riskMode": "pct", "riskPct": 0.5, "maxPosPct": 50.0, "openRiskPct": 1.0, "dayLossPct": 2.0, "maxTrades": 4,
           "lossStreak": 3, "flattenOnLimit": True,
           # "market": every entry / exit is a market order sent right after the candle where the strategy enters / exits
           # (the exits come only from the strategy); "limit": limit entry + OCO exits at the broker
           "orderMode": "market",
           "restStop": True,
           # what is bought: "shares" or "opt0dte" (same-day call for a long, put for a short; the strategy reads the
           # chart of the stock, the option is bought / sold at market)
           "instrument": "shares", "optPick": "atm", "optRiskPct": 0.5, "optMaxContracts": 50, "optFlatT": "15:25",                           # market mode: a stop order resting at Alpaca at the strategy's stop
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
    if b.get("instrument") in ("shares", "opt0dte"):
        cur["instrument"] = b["instrument"]
    if b.get("optPick") in ("atm", "otm1", "otm2", "itm1"):
        cur["optPick"] = b["optPick"]
    for k, lo, hi in (("optRiskPct", 0.01, 10), ("optMaxContracts", 1, 1000)):
        if b.get(k) not in (None, ""):
            cur[k] = max(lo, min(hi, float(b[k]) if k == "optRiskPct" else int(float(b[k]))))
    if b.get("optFlatT") and "09:30" < str(b["optFlatT"]) <= "15:29":
        cur["optFlatT"] = str(b["optFlatT"])[:5]
    if "restStop" in b:
        cur["restStop"] = bool(b["restStop"])
    if b.get("orderMode") in ("market", "limit"):
        cur["orderMode"] = b["orderMode"]
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
    out["speed"] = {k: v for k, v in (db.get(db.upk(sub), SK + "#SPEED") or {}).items() if k not in ("PK", "SK")} or None
    out["lastTick"] = st.get("last")
    out["ticks"] = (st.get("log") or [])[-60:]
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
        if r.get("kind") == "option":                  # an option bought: the premium is what can be lost
            out += float(r.get("fill") or r.get("premium") or 0) * 100 * q
            continue
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


def _send(c, body, tries=3):
    """An order, retried on network errors, rate limits (429) and Alpaca server errors (5xx); a refusal (4xx: not
    enough shares, market closed, ...) is not retried. -> (order or None, [notes])"""
    notes = []
    for k in range(tries):
        try:
            return (_alp(c, "POST", "/v2/orders", body) or {}), notes
        except Exception as e:
            m = str(e)
            notes.append(f"try {k + 1}: {m[:120]}")
            if "(4" in m and "(429)" not in m:
                break
            time.sleep(0.6 * (k + 1))
    return None, notes


def _close_pos(c, sym, tries=3):
    """Close the whole position at market, retried. -> (order id or None, notes)"""
    notes = []
    for k in range(tries):
        try:
            o = _alp(c, "DELETE", f"/v2/positions/{sym}") or {}
            return o.get("id") or "none", notes
        except Exception as e:
            notes.append(f"close try {k + 1}: {str(e)[:120]}")
            time.sleep(0.6 * (k + 1))
    return None, notes


def _cancel_all(c, ids):
    for oid in ids or []:
        _cancel(c, oid)
    if ids:
        time.sleep(0.4)                                # Alpaca frees the shares


def _oco_exits(c, r, held, stop, tp1_done):
    """Stop + target resting at Alpaca as OCO orders (one fills, Alpaca cancels the other): the TP1 part and the rest
    at TP2 (or all at TP1 without a partial). Prices on the strategy's side of the cent: they fill when the strategy's
    would. -> (ids, notes, description)"""
    import math
    up = r["dir"] > 0
    rs = lambda v: math.ceil(float(v) * 100 - 1e-6) / 100 if up else math.floor(float(v) * 100 + 1e-6) / 100     # stop
    rt = lambda v: math.floor(float(v) * 100 + 1e-6) / 100 if up else math.ceil(float(v) * 100 - 1e-6) / 100     # target
    part = float(r.get("part") or 0)
    legs = []
    if part and not tp1_done:
        q1 = min(int(int(r.get("filledQty") or held) * part), held - 1)
        if q1 > 0:
            legs.append((q1, r["tp1"]))
        legs.append((held - max(q1, 0), r["tp2"]))
    else:
        legs.append((held, r["tp2"] if part else r["tp1"]))
    ids, notes, desc = [], [], []
    for q, tp in legs:
        if q <= 0:
            continue
        o, n = _send(c, {"symbol": r["symbol"], "qty": str(int(q)), "side": "sell" if up else "buy", "type": "limit", "time_in_force": "day",
                         "order_class": "oco", "take_profit": {"limit_price": f"{rt(tp):.2f}"}, "stop_loss": {"stop_price": f"{rs(stop):.2f}"}})
        notes += n
        if o and o.get("id"):
            ids.append(o["id"])
            desc.append(f"{int(q)} @ target {rt(tp):.2f} / stop {rs(stop):.2f}")
    return ids, notes, " + ".join(desc)


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


_LOADED = time.time()                                  # when this Lambda container loaded the code (cold start)
_RUNS = [0]


class Timer:
    """Seconds since the scheduled minute (the candle close) at each step of a run: where the time goes."""
    def __init__(self, base):
        self.base, self.marks = base, []

    def mark(self, name):
        self.marks.append([name, round(time.time() - self.base, 2)])

    def line(self):
        return " ".join(f"{n}@{t:+.2f}s" for n, t in self.marks)


_TM = [None]


def _mark(name):
    if _TM[0]:
        _TM[0].mark(name)


def job(at=None, t0=None, loaded=None):
    """The scheduled minute: `at` = the time it was scheduled for (UTC, from EventBridge Scheduler), so a run that
    starts late still works on its own minute (and never skips one). Every user with auto-trading on, in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    from util import utc_to_ny
    import calendar
    minute, base = None, None
    if at:
        try:
            u = datetime.strptime(str(at)[:19], "%Y-%m-%dT%H:%M:%S")
            minute = utc_to_ny(u).replace(tzinfo=None, second=0, microsecond=0)
            base = calendar.timegm(u.replace(second=0).timetuple())
        except (TypeError, ValueError):
            minute = None
    base = base or (time.time() // 60 * 60)
    tm = _TM[0] = Timer(base)
    cold = _RUNS[0] == 0
    _RUNS[0] += 1
    if cold:
        tm.marks.append(["code loaded (cold start)", round((loaded or _LOADED) - base, 2)])
    if t0:
        tm.marks.append(["Lambda handler start", round(t0 - base, 2)])
    tm.mark("job start")
    subs = users()
    tm.mark("users read")
    def one(sub):
        pk = db.upk(sub)
        for _ in range(25):                            # a previous minute still running: wait for it (never two at once)
            if db.try_lock(pk, SK + "#LOCK", 50):
                break
            time.sleep(1)
        else:
            return sub, {"skipped": "the previous minute run is still going"}
        _mark("lock taken")
        try:
            return sub, tick(sub, minute=minute)
        except Exception as e:
            return sub, {"error": str(e)[:200]}
        finally:
            db.unlock(pk, SK + "#LOCK")
    with ThreadPoolExecutor(max_workers=8) as ex:
        out = dict(ex.map(one, subs))
    tm.mark("done")
    print(f"SMC {minute.strftime('%H:%M') if minute else '?'} {'COLD ' if cold else ''}{tm.line()}")   # CloudWatch: filter @message like /^SMC /
    return out


def tick(sub, now=None, minute=None):
    """The minute run; a crash is written to the log the page shows (else it would only be in CloudWatch).
    now: tests (no log); minute: the scheduled minute of a real run."""
    try:
        return _tick(sub, now, minute)
    except Exception as e:
        import traceback
        if now is None:
            st = db.get(db.upk(sub), SK + "#TICK") or {}
            entry = {"at": iso(now_ny()), "minute": now_ny().strftime("%H:%M"), "error": f"{type(e).__name__}: {str(e)[:200]}",
                     "where": traceback.format_exc().strip().splitlines()[-3][:200]}
            db.put({"PK": db.upk(sub), "SK": SK + "#TICK", "last": entry, "log": ((st.get("log") or []) + [entry])[-60:]})
        raise


def check(sub):
    """Right now, without sending anything: is it on, the account, the limits, and per ticker the last candle and
    every setup of today with what live would do with it (and why not)."""
    import daytrade
    cfg = config(sub)
    now = now_ny().replace(tzinfo=None, second=0, microsecond=0)
    today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    out = {"now": now.strftime("%Y-%m-%d %H:%M"), "enabled": cfg["enabled"], "trade": cfg["trade"], "problems": []}
    c = daytrade.creds(sub)
    if not cfg["enabled"]:
        out["problems"].append("Auto-trading is off: the minute run skips this strategy.")
    if not cfg["trade"]:
        out["problems"].append("Auto-trading is off: no order goes out.")
    if not c:
        out["problems"].append("No day-trading paper account connected.")
        return out
    if now.weekday() >= 5 or not ("09:31" <= hm <= "16:00"):
        out["problems"].append(f"Market closed now ({hm} ET): the minute run works 09:31-16:00 ET.")
    try:
        acct = account(c)
    except Exception as e:
        acct = None
        out["problems"].append(f"Account not readable: {str(e)[:120]}")
    out["account"] = acct
    pk = db.upk(sub)
    day_recs = [r for r in db.q_prefix(pk, POS) if (r.get("t") or "")[:10] == today]
    risk = risk_state(cfg, acct, day_recs)
    out["risk"] = risk
    if not risk["ok"]:
        out["problems"].append(f"New trades blocked: {risk['why']}")
    st = db.get(pk, SK + "#TICK") or {}
    last_run = (st.get("last") or {}).get("at")
    out["lastRun"] = st.get("last")
    if cfg["enabled"] and (not last_run or last_run < iso(now_ny() - timedelta(minutes=3))) and "09:33" <= hm <= "16:00" and now.weekday() < 5:
        out["problems"].append(f"The minute run hasn't run since {last_run or 'ever'}: is the latest version deployed (sam build / deploy)?")
    P = cfg["cfg"]
    out["tf"] = P["tf"]
    out["syms"] = []
    for sym in cfg["tickers"]:
        x = {"sym": sym}
        try:
            cut = (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
            bs = [b for b in daytrade.minute_bars(sub, sym, 3, now) if b["t"] <= cut]
            o = smc.detect(bs, P, [today])
            bars = o.get("bars") or []
            last = bars[-1]["t"] if bars else None
            x["lastCandle"] = last
            recs = {r["t"]: r for r in day_recs if r.get("symbol") == sym}
            x["setups"] = []
            for s_ in o["setups"]:
                r = recs.get(s_["t"])
                wait = bool(last) and _waiting(s_, last, P["tf"], P)
                what = (f"live record: {r.get('status')}" + (f" ({r.get('why') or r.get('exitWhy')})" if r.get("why") or r.get("exitWhy") else "")) if r else \
                    "waiting for the retracement: a limit order should be resting now" if wait else \
                    ("filled in the engine but no live record: the setup was armed and filled between two minute runs, or before auto-trading was on" if s_.get("fillT") else s_.get("status") or "")
                x["setups"].append({"t": s_["t"], "dir": s_["dir"], "status": s_.get("status"), "armedT": s_.get("armedT"), "entry": s_.get("entry"),
                                    "fillT": s_.get("fillT"), "R": s_.get("R"), "exitWhy": s_.get("exitWhy"), "live": what})
        except Exception as e:
            x["error"] = str(e)[:160]
        out["syms"].append(x)
    return out


def start_speed(sub):
    """The crypto speed test, any hour: the page asks, the trading function (the same one as the minute run) does it."""
    import daytrade
    import research
    if not daytrade.creds(sub):
        raise BadRequest("Connect the day-trading paper account first.")
    t = time.time()
    db.put({"PK": db.upk(sub), "SK": SK + "#SPEED", "status": "starting", "askedAt": t, "at": iso(now_ny())})
    research._invoke(sub, "smcspeed", {"askedAt": t})
    return {"status": "starting"}


def speed_test(sub, asked_at=None, t0=None, loaded=None, cold=False, sym="BTC/USD", notional=10):
    """Crypto round trip on the day-trading paper account (24/7, market orders): $10 bought at market, the fill read,
    sold back at market. Every step timed from the click on the page."""
    import daytrade
    c = daytrade.creds(sub)
    base = asked_at or time.time()
    tm = Timer(base)
    if cold and loaded:
        tm.marks.append(["code loaded (cold start)", round(loaded - base, 2)])
    if t0:
        tm.marks.append(["Lambda handler start", round(t0 - base, 2)])
    out = {"status": "done", "sym": sym, "at": iso(now_ny()), "askedAt": asked_at}
    try:
        tm.mark("job start")
        account(c)
        tm.mark("account read")
        t1 = time.time()
        o, notes = _send(c, {"symbol": sym, "notional": str(notional), "side": "buy", "type": "market", "time_in_force": "gtc"})
        tm.mark("buy accepted by Alpaca" if o else "buy refused")
        out["buyAcceptMs"] = round((time.time() - t1) * 1000)
        if not o:
            raise ValueError("; ".join(notes) or "buy refused")
        x = {}
        for _ in range(40):
            x = _alp(c, "GET", f"/v2/orders/{o['id']}") or {}
            if x.get("status") == "filled":
                break
            time.sleep(0.25)
        if x.get("status") != "filled":
            raise ValueError(f"buy not filled after 10 s (status {x.get('status')})")
        tm.mark("buy filled (seen)")
        out["buyFillMs"] = round((time.time() - t1) * 1000)
        out["buyPx"], qty = float(x["filled_avg_price"]), x["filled_qty"]
        out["buyAlpacaMs"] = _alpaca_ms(x)
        t2 = time.time()
        try:                                           # close the position (the crypto fee leaves a bit less than the fill)
            o2 = _alp(c, "DELETE", f"/v2/positions/{sym.replace('/', '')}") or None
        except Exception as e:
            o2, out["sellError"] = None, str(e)[:160]
        tm.mark("sell accepted by Alpaca" if o2 else "sell refused")
        out["sellAcceptMs"] = round((time.time() - t2) * 1000)
        if o2:
            for _ in range(40):
                y = _alp(c, "GET", f"/v2/orders/{o2['id']}") or {}
                if y.get("status") == "filled":
                    out["sellPx"], out["sellFillMs"], out["sellAlpacaMs"] = float(y["filled_avg_price"]), round((time.time() - t2) * 1000), _alpaca_ms(y)
                    tm.mark("sell filled (seen)")
                    break
                time.sleep(0.25)
    except Exception as e:
        out.update(status="failed", error=str(e)[:200])
    tm.mark("done")
    out["timing"] = tm.marks
    print(f"SMC SPEED {'COLD ' if cold else ''}{tm.line()}")
    db.put({"PK": db.upk(sub), "SK": SK + "#SPEED", **out})
    return out


def _alpaca_ms(o):
    """Alpaca's own time between receiving the order and filling it (from its timestamps), in ms."""
    try:
        f = lambda v: datetime.strptime(v[:26].replace("Z", ""), "%Y-%m-%dT%H:%M:%S.%f")
        return round((f(o["filled_at"]) - f(o["submitted_at"] or o["created_at"])).total_seconds() * 1000)
    except Exception:
        return None


def arm_test(sub, sym="SPY"):
    """The loop test, automatic: the next minute run (the scheduled job, as for a real setup) buys 1 share at market,
    puts the exits at the broker, and closes it 2 minutes later."""
    import daytrade
    cfg = config(sub)
    if not daytrade.creds(sub):
        raise BadRequest("Connect the day-trading paper account first.")
    if not (cfg["enabled"] and cfg["trade"]):
        raise BadRequest("Turn auto-trading on first: the minute run does the test.")
    db.update(db.upk(sub), SK, {"testReq": sym, "testAt": iso(now_ny())})
    return {"steps": [f"armed: the next minute run buys 1 {sym} at market, puts the stop / target at the broker and closes it 2 minutes later "
                      "(watch the Today table)"]}


def test_loop(sub, sym="SPY"):
    """The whole loop with 1 share, right now: market order -> fill read -> OCO stop / target at the broker; the minute
    run then manages it and closes it at market after 2 minutes. -> the steps done in this call."""
    import daytrade
    cfg = config(sub)
    c = daytrade.creds(sub)
    if not c:
        raise BadRequest("Connect the day-trading paper account first.")
    if not (cfg["enabled"] and cfg["trade"]):
        raise BadRequest("Turn auto-trading on first: the minute run manages and closes the test trade.")
    now = now_ny().replace(tzinfo=None)
    if now.weekday() >= 5 or not ("09:30" <= now.strftime("%H:%M") < "15:50"):
        raise BadRequest("The market is closed: the loop test runs 09:30-15:50 ET.")
    if _held(c, sym):
        raise BadRequest(f"A {sym} position is already held on this account.")
    steps = []
    t0 = time.time()
    o = _alp(c, "POST", "/v2/orders", {"symbol": sym, "qty": "1", "side": "buy", "type": "market", "time_in_force": "day"}) or {}
    steps.append(f"market buy 1 {sym} sent ({time.time() - t0:.2f}s)")
    px = None
    for _ in range(10):
        time.sleep(0.5)
        x = _alp(c, "GET", f"/v2/orders/{o.get('id')}") or {}
        if x.get("status") == "filled":
            px = float(x["filled_avg_price"])
            break
    if px is None:
        steps.append("not filled within 5 s: the minute run keeps checking it")
        px = None
    else:
        steps.append(f"filled @ {px:.2f} ({time.time() - t0:.2f}s after sending)")
    ref = px or float((_alp(c, "GET", f"/v2/positions/{sym}") or {}).get("avg_entry_price") or 0) or None
    tkey = now.strftime("%Y-%m-%dT%H:%M") + "~test"
    rec = {"PK": db.upk(sub), "SK": f"{POS}{sym}#{tkey}", "symbol": sym, "t": tkey, "dir": 1, "qty": 1, "test": True, "kind": "market",
           "entry": ref or 0, "stop": round((ref or 0) - 1.0, 2), "tp1": round((ref or 0) + 1.0, 2), "tp2": round((ref or 0) + 1.0, 2), "part": 0,
           "orderId": o.get("id"), "status": "pending", "candle": tkey, "sentAt": iso(now_ny()), "lagS": 0,
           "events": ["loop test: market buy 1 share"] + steps}
    if px is not None:
        rec.update(status="open", filledQty=1, fill=px, slip=0.0)
        rec["exitIds"] = _place_exits(c, rec, rec["stop"])
        rec["stopNow"] = rec["stop"]
        steps.append(f"OCO exit at the broker: stop {rec['stop']} / target {rec['tp1']} ({len(rec['exitIds'])} order)")
        rec["events"] = rec["events"][:1] + steps
    db.put(rec)
    steps.append("the minute run closes it at market in about 2 minutes: watch the Today table")
    return {"steps": steps}


def _tick(sub, now=None, minute=None):
    cfg = config(sub)
    if not cfg["enabled"]:
        return {"skipped": "off"}
    import daytrade
    c = daytrade.creds(sub)
    if not c:
        return {"skipped": "no day-trading account"}
    real = now is None
    now = now or minute or now_ny().replace(tzinfo=None, second=0, microsecond=0)
    t_start = now_ny().replace(tzinfo=None)
    today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    if now.weekday() >= 5 or not ("09:31" <= hm <= "16:00"):
        return {"skipped": "market closed"}
    P = cfg["cfg"]
    tf = P["tf"]
    pk = db.upk(sub)
    config_cache[pk] = cfg
    acts, log = [], []
    try:
        acct = account(c)
    except Exception as e:
        acct = None
        acts.append(f"account: {str(e)[:100]}")
    _mark("account read")
    day_recs = [r for r in db.q_prefix(pk, POS) if (r.get("t") or "")[:10] == today]
    risk = risk_state(cfg, acct, day_recs)
    if risk["flatten"]:                                # daily loss limit hit: everything of the strategy closed
        for r in [r for r in day_recs if r.get("status") in ("pending", "open")]:
            for oid in [r.get("orderId"), r.get("stopId")] + list(r.get("exitIds") or []):
                _cancel(c, oid)
            if r["status"] == "open":
                time.sleep(0.5)
                _alp(c, "DELETE", f"/v2/positions/{r.get('optSym') or r['symbol']}")
            db.update(pk, r["SK"], {"status": "closing" if r["status"] == "open" else "canceled", "exitWhy": risk["why"],
                                    "why": risk["why"], "events": (r.get("events") or []) + [f"closed: {risk['why']}"]})
            acts.append(f"{r['symbol']} closed: {risk['why']}")
    req = (db.get(pk, SK) or {}).get("testReq")
    if req and cfg["trade"]:                           # the loop test armed from the page
        db.update(pk, SK, {"testReq": ""})
        try:
            acts += [f"loop test: {x}" for x in test_loop(sub, req)["steps"][:-1]]
        except Exception as e:
            acts.append(f"loop test failed: {str(e)[:160]}")
    for sym in cfg["tickers"]:
        try:
            _mark(f"{sym} candles asked")
            bs, waited, fresh = _bars_now(sub, sym, now, tf)
            _mark(f"{sym} candles in" + (f" (waited {waited:g}s)" if waited else ""))
            out = smc.detect(bs, P, [today]) if bs else {"setups": [], "bars": []}
            _mark(f"{sym} strategy run")
            bars = out.get("bars") or []
            last = bars[-1]["t"] if bars else None
            if not last:                               # no candle: what is open is still managed (no new setup)
                acts.append(f"{sym}: no candles")
                last = (now - timedelta(minutes=tf)).strftime("%Y-%m-%dT%H:%M")
                out = {"setups": [], "bars": []}
            new_candle = last != (db.get(pk, f"{SK}#LAST#{sym}") or {}).get("t")
            db.put({"PK": pk, "SK": f"{SK}#LAST#{sym}", "t": last})
            lag = lambda: round((now_ny().replace(tzinfo=None) - _close_t(last, tf)).total_seconds(), 1)
            log.append({"sym": sym, "candle": last, "closedAt": _close_t(last, tf).strftime("%H:%M:%S"), "fresh": fresh, "waited": waited,
                        "newCandle": new_candle,
                        "engine": [f"{x['t'][11:16]} {'L' if x['dir'] > 0 else 'S'} {x.get('status')}" + (f" fill {x['fillT'][11:16]}" if x.get("fillT") else "")
                                   + (f" out {x['exitT'][11:16]} {x.get('exitWhy')}" if x.get("exitT") and x.get("exitWhy") != "end of day" else "")
                                   for x in out["setups"] if (x.get("fillT") or x.get("armedT") or x["t"]) >= (now - timedelta(minutes=6 * tf)).strftime("%Y-%m-%dT%H:%M")]})
            recs = {r["t"]: r for r in db.q_prefix(pk, f"{POS}{sym}#{today}")}
            active = [r for r in recs.values() if r.get("status") in ("pending", "open", "closing")]
            flat = hm >= P["flatT"]
            setups = {s["t"]: s for s in out["setups"]}
            # 1. what is already at the broker
            for r in active:
                s = setups.get(r["t"])
                acts += _manage(c, pk, r, s, last, tf, flat, P, lag)
                _mark(f"{sym} open trade managed")
            active = [r for r in db.q_prefix(pk, f"{POS}{sym}#{today}") if r.get("status") in ("pending", "open", "closing")]
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
                notes_late = None
                mkt_mode = cfg.get("orderMode", "market") == "market"
                done_now = s.get("exitT") == last and s.get("exitWhy") != "end of day"     # in and out in the same candle
                if mkt_mode:                           # market: in right after the candle where the engine fills
                    in_trade = bool(s.get("fillT")) and (not s.get("exitT") or s.get("exitWhy") == "end of day")
                    late = _candles_between(s.get("fillT"), last, tf) if s.get("fillT") else 99
                    pending, market_now = False, bool(in_trade and late <= CATCHUP)   # a missed minute (late candle, slow run): caught up
                    if market_now and late:
                        notes_late = f"{late} candle(s) late (the minute run missed the fill candle {s['fillT'][11:16]})"
                        acts.append(f"{sym}: entry caught up: {notes_late}")
                else:
                    pending = _waiting(s, last, tf, P)
                    market_now = P["entryAt"] == "market" and s.get("fillT") == last and s.get("armedT") == last
                if not (pending or market_now):
                    continue
                if cfg.get("instrument") == "opt0dte" and mkt_mode:
                    if market_now:
                        acts += _enter_option(c, pk, cfg, sym, s, last, acct, today, lag, notes_late)
                    break
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
                    _mark(f"{sym} entry sending")
                    o, notes = _send(c, {"symbol": sym, "qty": str(q), "side": side, "type": "market", "time_in_force": "day"})
                    _mark(f"{sym} entry accepted by Alpaca" if o else f"{sym} entry refused")
                else:
                    o, notes = _send(c, {"symbol": sym, "qty": str(q), "side": side, "type": "limit", "time_in_force": "day",
                                         "limit_price": str(_px(s["entry"]))})
                o = o or {}
                rec = {"PK": pk, "SK": f"{POS}{sym}#{s['t']}", "symbol": sym, "t": s["t"], "dir": s["dir"], "qty": q,
                       "entry": s["entry"], "stop": s["stop"], "tp1": s["tp1"], "tp2": s["tp2"], "tp1Name": s.get("tp1Name"),
                       "tp2Name": s.get("tp2Name"), "part": s.get("part", P["partial"] if P["partialOn"] else 0), "pool": s.get("pool"),
                       "kind": "market" if market_now else "limit", "mode": cfg.get("orderMode", "market"), "orderId": o.get("id"),
                       "restStop": bool(cfg.get("restStop", True)),
                       "status": "pending", "candle": last, "tries": 1,
                       "sentAt": iso(now_ny()), "lagS": lag(), "sizing": size_note, "equity": (acct or {}).get("equity"),
                       "events": [f"size {q}: {size_note}", f"{'market' if market_now else 'limit'} {side} {q} @ {s['entry']} "
                                                                           f"(candle {last[11:16]}, sent {lag()}s after its close)"] + notes + ([notes_late] if notes_late else [])}
                if not o.get("id"):
                    rec["events"].append("order not accepted after the retries: tried again next minute")
                if P["partialOn"] is False:
                    rec["part"] = 0
                db.put(rec)
                acts.append(f"{sym} {'LONG' if s['dir'] > 0 else 'SHORT'} {q} {rec['kind']} @ {s['entry']} · stop {s['stop']} · lag {rec['lagS']}s")
                break
        except Exception as e:
            acts.append(f"{sym}: {str(e)[:160]}")
    if real:
        st = db.get(pk, SK + "#TICK") or {}
        _mark("logged")
        entry = {"at": iso(now_ny()), "minute": hm, "timing": list(_TM[0].marks) if _TM[0] else None, "startS": round((t_start - now).total_seconds(), 1), "equity": (acct or {}).get("equity"), "dayPct": (acct or {}).get("dayPct"), "blocked": risk["why"], "took": round((now_ny().replace(tzinfo=None) - t_start).total_seconds(), 1), "syms": log, "actions": acts}
        db.put({"PK": pk, "SK": SK + "#TICK", "last": entry, "log": ((st.get("log") or []) + [entry])[-120:]})
    return {"actions": acts}


# ---------------------------------------------------------------- 0DTE options
def pick_0dte(c, sym, up, price, pick="atm", day=None):
    """Today's call (long) / put (short) of `sym`: at the money, 1-2 strikes out of the money, or 1 in the money.
    -> {"symbol": OCC, "strike", "exp"} or None"""
    import urllib.parse
    day = day or now_ny().strftime("%Y-%m-%d")
    q = urllib.parse.urlencode({"underlying_symbols": sym, "expiration_date": day, "type": "call" if up else "put",
                                "strike_price_gte": f"{price * 0.97:.2f}", "strike_price_lte": f"{price * 1.03:.2f}", "limit": 500, "status": "active"})
    cs = (_alp(c, "GET", f"/v2/options/contracts?{q}") or {}).get("option_contracts") or []
    cs = sorted((x for x in cs if x.get("tradable", True)), key=lambda x: float(x["strike_price"]))
    if not cs:
        return None
    ks = [float(x["strike_price"]) for x in cs]
    i = min(range(len(ks)), key=lambda k: abs(ks[k] - price))
    step = {"atm": 0, "otm1": 1, "otm2": 2, "itm1": -1}.get(pick, 0) * (1 if up else -1)   # out of the money: up for a call
    i = max(0, min(len(cs) - 1, i + step))
    return {"symbol": cs[i]["symbol"], "strike": ks[i], "exp": cs[i].get("expiration_date") or day}


def _opt_quote(c, occ):
    import daytrade
    import optiondata
    feed = "opra" if (daytrade.data_creds_feed(c) == "sip") else "indicative"
    q = optiondata.snapshots(c, [occ], feed=feed).get(occ) or {}
    if not q.get("ask") and feed == "opra":
        q = optiondata.snapshots(c, [occ]).get(occ) or {}
    return q


def _enter_option(c, pk, cfg, sym, s, last, acct, today, lag, notes_late=None):
    """The strategy filled on the stock: buy today's call (long) / put (short) at market, sized by the premium."""
    P = cfg["cfg"]
    hm = last[11:16]
    cut = min(P["endT"] if not P.get("win2On") else max(P["endT"], P["endT2"]), "15:10")
    key = f"{POS}{sym}#{s['t']}"
    base = {"PK": pk, "SK": key, "symbol": sym, "t": s["t"], "dir": s["dir"], "entry": s["entry"], "stop": s["stop"], "tp1": s["tp1"],
            "tp2": s["tp2"], "tp1Name": s.get("tp1Name"), "tp2Name": s.get("tp2Name"), "kind": "option", "mode": "market", "candle": last,
            "part": s.get("part", P["partial"] if P["partialOn"] else 0) if P["partialOn"] else 0, "sentAt": iso(now_ny())}
    if hm >= cut:
        db.put({**base, "status": "canceled", "why": f"0DTE: no new option after {cut} (Alpaca cutoff 15:30)", "events": [f"not taken: after {cut}"]})
        return [f"{sym}: option not bought after {cut}"]
    if not acct:
        db.put({**base, "status": "canceled", "why": "account value not available", "events": ["not taken: account value not available"]})
        return [f"{sym}: option not bought: account value not available"]
    px = float(s["entry"])
    try:
        ct = pick_0dte(c, sym, s["dir"] > 0, px, cfg.get("optPick", "atm"), today)
    except Exception as e:
        ct, err = None, str(e)[:120]
    else:
        err = None
    if not ct:
        db.put({**base, "status": "canceled", "why": f"no 0DTE contract found{': ' + err if err else ''}", "events": ["not taken: no contract"]})
        return [f"{sym}: no 0DTE contract{': ' + err if err else ''}"]
    qt = _opt_quote(c, ct["symbol"])
    ask = qt.get("ask") or qt.get("mark")
    if not ask:
        db.put({**base, "optSym": ct["symbol"], "status": "canceled", "why": "no option quote", "events": [f"{ct['symbol']}: no quote"]})
        return [f"{sym}: {ct['symbol']} has no quote"]
    budget = acct["equity"] * float(cfg.get("optRiskPct") or 0.5) / 100
    n = min(int(budget // (float(ask) * 100)), int(cfg.get("optMaxContracts") or 50))
    note = f"premium budget ${budget:,.0f} ({cfg.get('optRiskPct')}% of ${acct['equity']:,.0f}) / ask {float(ask):.2f} x 100"
    if n < 1:
        db.put({**base, "optSym": ct["symbol"], "status": "canceled", "why": f"size 0: {note}", "events": [f"not taken: {note}"]})
        return [f"{sym}: option not bought: {note}"]
    _mark(f"{sym} option entry sending")
    o, notes = _send(c, {"symbol": ct["symbol"], "qty": str(n), "side": "buy", "type": "market", "time_in_force": "day"})
    _mark(f"{sym} option entry accepted by Alpaca" if o else f"{sym} option entry refused")
    o = o or {}
    rec = {**base, "optSym": ct["symbol"], "strike": ct["strike"], "qty": n, "premium": float(ask), "bid": qt.get("bid"), "delta": qt.get("delta"),
           "orderId": o.get("id"), "status": "pending", "tries": 1, "lagS": lag(), "sizing": note, "equity": acct.get("equity"),
           "events": [f"{'CALL' if s['dir'] > 0 else 'PUT'} {ct['symbol']} (strike {ct['strike']:g}, {cfg.get('optPick', 'atm')}) · bid {qt.get('bid')} / ask {ask}",
                      f"size {n}: {note}", f"market buy {n} (candle {last[11:16]}, sent {lag()}s after its close)"] + notes + ([notes_late] if notes_late else [])}
    if not o.get("id"):
        rec["events"].append("order not accepted after the retries")
    db.put(rec)
    return [f"{sym} {'CALL' if s['dir'] > 0 else 'PUT'} {n}x {ct['symbol']} market (ask {ask}) · lag {rec['lagS']}s"]


def _manage_opt(c, pk, r, s, last, flat, P, lag):
    """An option bought on the strategy: exits from the strategy on the stock (TP1 partial, target, stop, breakeven,
    trail, early exit) at market after the candle, and always out before the 0DTE cutoff (optFlatT)."""
    acts, upd, ev = [], {}, list(r.get("events") or [])
    occ = r["optSym"]
    cfg_flat = (config_cache.get(pk) or {}).get("optFlatT", "15:25")
    flat = flat or last[11:16] >= min(cfg_flat, "15:28")
    eng_out = bool(s and s.get("exitT") and s.get("exitWhy") != "end of day")
    st = r["status"]
    if st == "pending":
        o = _alp(c, "GET", f"/v2/orders/{r['orderId']}") if r.get("orderId") else None
        filled = int(float((o or {}).get("filled_qty") or 0))
        ost = (o or {}).get("status")
        if filled and ost in ("filled", "canceled", "expired", "partially_filled"):
            if ost == "partially_filled":
                _cancel(c, r["orderId"])
            px = float(o.get("filled_avg_price") or r.get("premium") or 0)
            upd.update(status="open", filledQty=filled, fill=px, filledAt=o.get("filled_at"), slip=round(px - float(r.get("premium") or px), 4))
            ev.append(f"filled {filled} @ {px:.2f} (ask was {r.get('premium')}; seen {lag()}s after candle {last[11:16]})")
            acts.append(f"{r['symbol']} option filled {filled} @ {px:.2f}")
        elif not r.get("orderId") or ost in ("canceled", "expired", "rejected") or \
                (ost in ("new", "accepted", "pending_new") and (r.get("sentAt") or "") < iso(now_ny() - timedelta(minutes=2))):
            if r.get("orderId") and ost not in ("canceled", "expired", "rejected"):
                _cancel(c, r["orderId"])
            upd.update(status="canceled", why=f"option entry not filled ({ost or 'not accepted by Alpaca'})")
            ev.append(upd["why"])
    elif st in ("open", "closing"):
        held = abs(_held(c, occ))
        if not held:
            xs = _orders_after(c, occ, {**r, "dir": 1})
            q_all = sum(float(x["filled_qty"]) for x in xs)
            avg = sum(float(x["filled_avg_price"]) * float(x["filled_qty"]) for x in xs) / q_all if q_all else None
            upd.update(status="closed", exitPx=round(avg, 4) if avg else None, closedAt=iso(now_ny()), exitWhy=r.get("exitWhy") or "closed outside the strategy")
            if avg and r.get("fill"):
                upd["R"] = round((avg - float(r["fill"])) / float(r["fill"]), 3)      # 1R = the premium paid
            ev.append(f"closed: {upd['exitWhy']}" + (f" @ {avg:.2f} avg" if avg else ""))
            acts.append(f"{r['symbol']} option closed ({upd['exitWhy']})")
        elif st == "closing" or flat or eng_out:
            why = r.get("exitWhy") if st == "closing" else (f"0DTE flat at {cfg_flat}" if flat else s["exitWhy"])
            oid, notes = _close_pos(c, occ)
            upd.update(status="closing", exitWhy=why, exitOrder=oid, closeTries=(r.get("closeTries") or 0) + 1)
            ev += notes + [f"option sold at market: {why} (candle {last[11:16]}, {lag()}s after its close)"]
            acts.append(f"{r['symbol']} option sold: {why}")
        elif s and s.get("tp1Hit") and not r.get("tp1Done") and float(r.get("part") or 0):
            q = int(int(r.get("filledQty") or held) * float(r["part"]))
            if 0 < q < held:
                o, notes = _send(c, {"symbol": occ, "qty": str(q), "side": "sell", "type": "market", "time_in_force": "day"})
                ev += notes + [f"TP1: {q} contracts sold at market (candle {last[11:16]}, {lag()}s after its close)" if o else "TP1 partial NOT sent (tried again next minute)"]
                if o:
                    upd["tp1Done"] = True
                    acts.append(f"{r['symbol']} option TP1 {q}")
            else:
                upd["tp1Done"] = True                  # 1 contract: no partial, the rest rides to TP2
    if s and s.get("R") is not None and s.get("exitWhy") != "end of day":
        upd["engineR"] = s["R"]
    if upd:
        upd["events"] = ev[-25:]
        db.update(pk, r["SK"], upd)
    return acts


config_cache = {}


CATCHUP = 3                                            # market mode: an entry missed by up to 3 candles is still taken


def _candles_between(t0, t1, tf):
    try:
        d = (datetime.strptime(t1[:16], "%Y-%m-%dT%H:%M") - datetime.strptime(t0[:16], "%Y-%m-%dT%H:%M")).total_seconds() / 60 / tf
    except (TypeError, ValueError):
        return 99
    return int(round(d)) if t0[:10] == t1[:10] else 99


def _waiting(s, last, tf, P):
    """The setup is armed and still waiting for its retracement (the window of entryBars candles not over)."""
    if s.get("fillT") or s.get("status") != "no retrace" or not s.get("armedT") or P["entryAt"] == "market":
        return False
    armed = datetime.strptime(s["armedT"][:16], "%Y-%m-%dT%H:%M")
    return last >= s["armedT"] and datetime.strptime(last[:16], "%Y-%m-%dT%H:%M") < armed + timedelta(minutes=tf * P["entryBars"]) \
        and _hm(last) < (smc.window_end(P, _hm(s["t"])) or P["endT"])


def _manage(c, pk, r, s, last, tf, flat, P, lag):
    if r.get("kind") == "option":
        return _manage_opt(c, pk, r, s, last, flat, P, lag)
    if r.get("mode") == "market" and not r.get("test"):
        return _manage_mkt(c, pk, r, s, last, flat, P, lag, r.get("restStop"))
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
            elif r.get("test"):
                gone = None                            # the loop test: not a setup of the engine
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
            elif r.get("test") and (r.get("sentAt") or "") < iso(now_ny() - timedelta(minutes=2)):
                early = "loop test done (closed after 2 minutes)"
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


def _manage_mkt(c, pk, r, s, last, flat, P, lag, rest=None):
    """Market mode: market entries; with restStop the strategy's stop and targets rest at Alpaca as OCO orders (filled
    the moment they are touched), else every exit is a market order after the candle. The strategy takes TP1 -> the partial sold at market; the strategy exits (target, stop, breakeven,
    trail, early exit) or the flat time -> closed at market right after that candle. A close that didn't go through is
    sent again every minute until the position is gone."""
    acts, upd, ev = [], {}, list(r.get("events") or [])
    sym, side_exit = r["symbol"], "sell" if r["dir"] > 0 else "buy"
    eng_out = bool(s and s.get("exitT") and s.get("exitWhy") != "end of day")
    st = r["status"]
    if st == "pending":
        o = _alp(c, "GET", f"/v2/orders/{r['orderId']}") if r.get("orderId") else None
        filled = int(float((o or {}).get("filled_qty") or 0))
        ost = (o or {}).get("status")
        if filled and ost in ("filled", "canceled", "expired", "partially_filled"):
            if ost == "partially_filled":
                _cancel(c, r["orderId"])               # what is filled is the trade
            px = float(o.get("filled_avg_price") or r["entry"])
            upd.update(status="open", filledQty=filled, fill=px, filledAt=o.get("filled_at"), slip=round((px - r["entry"]) * r["dir"], 4),
                       stopNow=(s or {}).get("stopNow") or r["stop"])
            ev.append(f"filled {filled} @ {px:.2f} (seen {lag()}s after the close of candle {last[11:16]}; strategy price {r['entry']})")
            acts.append(f"{sym} filled {filled} @ {px:.2f}")
            if rest:
                ids, notes, desc = _oco_exits(c, {**r, **upd}, filled, upd["stopNow"], False)
                upd["exitIds"] = ids
                ev += notes + [f"at Alpaca: {desc}" if ids else "exit orders NOT placed (tried again next minute)"]
        elif not r.get("orderId") or ost in ("canceled", "expired", "rejected") or \
                (ost in ("new", "accepted", "pending_new") and (r.get("sentAt") or "") < iso(now_ny() - timedelta(minutes=2))):
            if r.get("orderId") and ost not in ("canceled", "expired", "rejected"):
                _cancel(c, r["orderId"])
            upd.update(status="canceled", why=f"entry not filled ({ost or 'not accepted by Alpaca'})")
            ev.append(upd["why"])
            acts.append(f"{sym} entry not filled: {ost or 'not accepted'}")
    elif st in ("open", "closing"):
        held = abs(_held(c, sym))
        ids = [x for x in list(r.get("exitIds") or []) + [r.get("stopId")] if x]
        if not held:
            px, why = _last_exit(c, sym, r)
            if st == "closing" and r.get("exitWhy"):
                why = r["exitWhy"]
            elif why == "stop":
                why = f"stop order at Alpaca filled @ {px:.2f}" if px else "stop order at Alpaca filled"
            elif why == "target":
                why = f"target order at Alpaca filled @ {px:.2f}" if px else "target order at Alpaca filled"
            else:
                why = r.get("exitWhy") or "closed outside the strategy"
            _cancel_all(c, ids)                        # what is left of an OCO (normally cancelled by Alpaca)
            upd.update(status="closed", exitPx=px, closedAt=iso(now_ny()), exitWhy=why, exitIds=[], stopId=None)
            if px is not None and r.get("fill"):
                upd["R"] = round(_realized_R(c, sym, r, abs(r["entry"] - r["stop"]) or 1), 3)
            ev.append(f"closed: {why} (seen {lag()}s after candle {last[11:16]})")
            acts.append(f"{sym} closed ({why})")
        elif st == "closing" or flat or eng_out:
            why = r.get("exitWhy") if st == "closing" else (f"flat at {P['flatT']}" if flat else s["exitWhy"])
            _cancel_all(c, ids)                        # the resting orders out first (they hold the shares)
            oid, notes = _close_pos(c, sym)
            upd.update(status="closing", exitWhy=why, exitOrder=oid, closeTries=(r.get("closeTries") or 0) + 1, exitIds=[], stopId=None)
            ev += notes + [f"close at market: {why} (candle {last[11:16]}, {lag()}s after its close" + (f", try {upd['closeTries']})" if upd["closeTries"] > 1 else ")")]
            acts.append(f"{sym} close at market: {why}")
        else:
            part = float(r.get("part") or 0)
            filled = int(r.get("filledQty") or held)
            tp1_done = bool(r.get("tp1Done"))
            if part and not tp1_done and held < filled:  # the TP1 order at Alpaca filled
                tp1_done = upd["tp1Done"] = True
                ev.append(f"TP1 order at Alpaca filled: {filled - held} sold, {held} left (seen {lag()}s after candle {last[11:16]})")
                acts.append(f"{sym} TP1 filled at Alpaca")
            if s and s.get("tp1Hit") and part and not tp1_done:   # the strategy took TP1, the order didn't fill: at market
                q = int(filled * part)
                if 0 < q < held:
                    _cancel_all(c, ids)
                    ids = []
                    o, notes = _send(c, {"symbol": sym, "qty": str(q), "side": side_exit, "type": "market", "time_in_force": "day"})
                    ev += notes + [f"TP1: {q} sold at market (the TP1 order didn't fill; candle {last[11:16]}, {lag()}s after its close)" if o else "TP1 partial NOT sent (tried again next minute)"]
                    if o:
                        tp1_done = upd["tp1Done"] = True
                        held -= q                      # the sale may not be filled yet: the new orders are for what is left
                        upd["exitIds"], upd["stopId"] = [], None
                        acts.append(f"{sym} TP1 partial {q} at market")
            stop = (s or {}).get("stopNow") or r.get("stopNow") or r["stop"]
            moved = abs(float(stop) - float(r.get("stopNow") or r["stop"])) >= 0.01
            if moved:
                upd["stopNow"] = stop
                ev.append(f"strategy stop now {stop} (candle {last[11:16]})")
            if rest and (moved or upd.get("tp1Done") or not ids):
                _cancel_all(c, ids)
                new_ids, notes, desc = _oco_exits(c, r, held, stop, tp1_done)
                upd.update(exitIds=new_ids, stopId=None)
                ev += notes + [f"at Alpaca: {desc}" if new_ids else "exit orders NOT placed (tried again next minute)"]
    if s and s.get("R") is not None and s.get("exitWhy") != "end of day":
        upd["engineR"] = s["R"]
    if upd:
        upd["events"] = ev[-25:]
        db.update(pk, r["SK"], upd)
    return acts

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
    return [p["PK"][5:] for p in db.scan_sk(SK) if p.get("enabled")]      # the owner's own item: one read
