"""Same-day options on the user's Alpaca PAPER account, following a paper strategy's NQ / ES trades.

When a paper trade opens (strategy.paper_one marks it "open"), 1 contract (or `qty`) is bought at each target delta:
a call for a long, a put for a short, on QQQ (NQ) or SPY (ES), expiring today. When the trade closes (its stop,
target or time exit on the futures candles), every leg is sold to close; anything still open is sold at 15:50.

Signal: the NQ / ES candles arrive hours late, so for the overnight-midpoint setups (onmid / onhold) the signal is
computed live from the ETF's own Alpaca bars (real time): overnight = the previous session's after-hours 16:00-20:00 plus
today's pre-market 04:00-09:29; the open = the 09:25 candle's close; then the strategy's own rules (stop, target, hold,
out-at time) run on the ETF's 5-minute candles. Other setups follow the futures paper trades (late when the data is).

Trading: https://paper-api.alpaca.markets/v2/ only (orders, account). Contract pick: option snapshots with greeks,
https://data.alpaca.markets/v1beta1/options/snapshots/{underlying}?feed=indicative (Black-Scholes from the quote
when greeks are missing). State: strat/<sub>/<id>/alpopt = {"legs": {trade key: [leg, ...]}, "log": [...]}."""
import json
import urllib.error
import urllib.parse
import urllib.request

import alpaca
import opt0dte
import store
from util import BadRequest, Unavailable, iso, now_ny

PAPER = "https://paper-api.alpaca.markets/v2/"
DATA = "https://data.alpaca.markets/v1beta1/"
UND = {"NQ": "QQQ", "ES": "SPY"}
DEFAULT = {"on": False, "deltas": [0.3, 0.5, 0.7], "qty": 1}
LAST_ENTRY = "15:30"                                  # no new legs after this (0DTE)
FLAT_AT = "15:50"                                     # everything still open is sold here


def _req(creds, method, url, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"APCA-API-KEY-ID": creds[0], "APCA-API-SECRET-KEY": creds[1], "Accept": "application/json",
                                          "Content-Type": "application/json", "User-Agent": "tradejournal-futures/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise alpaca._err(e)
    except urllib.error.URLError as e:
        raise Unavailable(f"Alpaca couldn't be reached: {e.reason}")


def _key(t):
    return f"{t['s']}|{t['d']}|{t['in']}|{t['side']}"


def _path(sub, sid):
    return f"strat/{sub}/{sid}/alpopt"


def config(s):
    return {**DEFAULT, **(s.get("alpOpt") or {})}


def clean(b):
    try:
        ds = sorted({round(float(x), 2) for x in (b.get("deltas") or DEFAULT["deltas"])})
        qty = int(b.get("qty") or 1)
    except (TypeError, ValueError):
        raise BadRequest("Deltas: numbers between 0.05 and 0.95; contracts: a whole number.")
    if not ds or len(ds) > 5 or any(not 0.05 <= d <= 0.95 for d in ds):
        raise BadRequest("Pick 1 to 5 deltas between 0.05 and 0.95.")
    if not 1 <= qty <= 10:
        raise BadRequest("Contracts per delta: 1 to 10.")
    return {"on": bool(b.get("on")), "deltas": ds, "qty": qty}


def account(sub):
    """The paper account: confirms the key is a paper key and options are allowed."""
    a = _req(alpaca.creds(sub), "GET", PAPER + "account")
    return {"number": a.get("account_number"), "status": a.get("status"), "cash": a.get("cash"), "buyingPower": a.get("options_buying_power") or a.get("buying_power"),
            "level": a.get("options_trading_level"), "approved": a.get("options_approved_level")}


# ---------------------------------------------------------------- contracts

def chain(c, und, day, call):
    """{symbol: (|delta|, strike, bid, ask)} for today's expiry on one side."""
    out, tok = {}, None
    for _ in range(10):
        q = {"feed": "indicative", "expiration_date": day, "type": "call" if call else "put", "limit": 1000, "page_token": tok}
        r = _req(c, "GET", DATA + f"options/snapshots/{und}?" + urllib.parse.urlencode({k: v for k, v in q.items() if v}))
        for sym, x in (r.get("snapshots") or {}).items():
            k = float(sym[-8:]) / 1000
            qt = x.get("latestQuote") or {}
            bid, ask = float(qt.get("bp") or 0), float(qt.get("ap") or 0)
            d = (x.get("greeks") or {}).get("delta")
            out[sym] = [abs(float(d)) if d is not None else None, k, bid, ask]
        tok = r.get("next_page_token")
        if not tok:
            break
    return out


def _fill_deltas(rows, spot):
    """Contracts without greeks: delta from the mid quote (implied volatility, time left to 16:00)."""
    t = now_ny()
    T = max(5, (16 * 60) - (t.hour * 60 + t.minute)) / opt0dte.YEAR_MIN
    for sym, r in rows.items():
        if r[0] is None and r[3] > 0 and spot:
            r[0] = opt0dte.delta(spot, r[1], T, (r[2] + r[3]) / 2, "C" in sym[-9:-8])
    return rows


def pick(rows, target):
    best = None
    for sym, (d, k, bid, ask) in rows.items():
        if d is None or ask <= 0:
            continue
        gap = abs(d - target)
        if best is None or gap < best[0]:
            best = (gap, sym, d, k, bid, ask)
    return best


def _spot(c, und):
    try:
        r = alpaca._get(c, f"stocks/{und}/trades/latest", {"feed": "iex"})
        return float((r.get("trade") or {}).get("p") or 0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------- orders

def _order(c, sym, side, qty, intent):
    return _req(c, "POST", PAPER + "orders", {"symbol": sym, "qty": str(qty), "side": side, "type": "market",
                                              "time_in_force": "day", "position_intent": intent})


def _update(c, leg):
    for k in ("buy", "sell"):
        o = leg.get(k)
        if o and o.get("id") and o.get("status") not in ("filled", "canceled", "rejected", "expired"):
            try:
                r = _req(c, "GET", PAPER + f"orders/{o['id']}")
                o.update(status=r.get("status"), px=float(r["filled_avg_price"]) if r.get("filled_avg_price") else None, at=r.get("filled_at") or o.get("at"))
            except Exception as e:
                o["err"] = str(e)[:200]
    b, s = (leg.get("buy") or {}).get("px"), (leg.get("sell") or {}).get("px")
    leg["pnl"] = round((s - b) * 100 * leg["qty"], 2) if b is not None and s is not None else None


def _open_legs(c, t, cfg, log):
    und = UND.get(t["s"])
    if not und:
        return []
    call = t["side"] == "long"
    day = t["d"]
    rows = chain(c, und, day, call)
    if any(r[0] is None for r in rows.values()):
        rows = _fill_deltas(rows, _spot(c, und))
    legs = []
    for d in cfg["deltas"]:
        p = pick(rows, d)
        if not p:
            log.append(f"{t['d']} {t['in']}: no {und} {'call' if call else 'put'} near delta {d}")
            continue
        _, sym, dlt, k, bid, ask = p
        leg = {"target": d, "sym": sym, "und": und, "strike": k, "right": "C" if call else "P", "delta": round(dlt, 3), "bid": bid, "ask": ask,
               "qty": cfg["qty"], "trade": _key(t)}
        try:
            o = _order(c, sym, "buy", cfg["qty"], "buy_to_open")
            leg["buy"] = {"id": o.get("id"), "status": o.get("status"), "at": iso(now_ny())}
        except Exception as e:
            leg["buy"] = {"status": "error", "err": str(e)[:200]}
            log.append(f"buy {sym} failed: {e}")
        legs.append(leg)
    return legs


def _close_leg(c, leg, why, log):
    b = leg.get("buy") or {}
    if leg.get("sell") or b.get("status") in ("error", "canceled", "rejected", "expired"):
        return
    if b.get("status") != "filled":
        try:
            _req(c, "DELETE", PAPER + f"orders/{b['id']}")    # not filled yet: cancel instead
            b["status"] = "canceled"
            return
        except Exception:
            pass
    try:
        o = _order(c, leg["sym"], "sell", leg["qty"], "sell_to_close")
        leg["sell"] = {"id": o.get("id"), "status": o.get("status"), "at": iso(now_ny()), "why": why}
    except Exception as e:
        leg["sell"] = {"status": "error", "err": str(e)[:200], "why": why}
        log.append(f"sell {leg['sym']} failed: {e}")


LIVE_SETUPS = ("onmid", "onhold")


def etf_bars(sub, und):
    """The last two sessions of 5-minute bars, extended hours: [["YYYY-MM-DDTHH:MM", o, h, l, c], ...]."""
    out = []
    for b in alpaca.bars(sub, und, days=2, tf=5, rth=False, close_bar=False):
        out.append([b[0].replace(" ", "T"), float(b[1]), float(b[2]), float(b[3]), float(b[4])])
    return out


def live_signal(sub, s, fut, day, B=None):
    """The overnight-midpoint trade for `day`, run on the ETF's own bars. Returns (trade or None, info)."""
    import nightkit as N
    import onmid
    und = UND[fut]
    B = B if B is not None else etf_bars(sub, und)
    days = sorted({b[0][:10] for b in B})
    prev = [d for d in days if d < day]
    info = {"und": und, "day": day}
    if not prev:
        info["msg"] = f"no {und} bars from the previous session yet"
        return None, info
    pd = prev[-1]
    on = [b for b in B if (b[0][:10] == pd and "16:00" <= b[0][11:16] < "20:00") or (b[0][:10] == day and "04:00" <= b[0][11:16] < "09:30")]
    F = [b for b in B if b[0][:10] == day and b[0][11:16] < "16:00"]
    if not on:
        info["msg"] = f"no {und} after-hours / pre-market bars"
        return None, info
    hi, lo = max(b[2] for b in on), min(b[3] for b in on)
    info.update(onh=hi, onl=lo, mid=round((hi + lo) / 2, 3), n=len(on))
    if N.idx_at(F, f"{day}T09:25") is None:
        pre = [b for b in F if b[0][11:16] < "09:25"]
        if not pre or not any(b[0][11:16] >= "09:30" for b in F):
            info["msg"] = "waiting for the 09:30 open"
            return None, info
        c = pre[-1][4]                                 # no 09:25 print: the last pre-market close stands in
        F = [b for b in F if b[0][11:16] < "09:25"] + [[f"{day}T09:25", c, c, c, c]] + [b for b in F if b[0][11:16] >= "09:30"]
    k = N.idx_at(F, f"{day}T09:25")
    info["open"] = F[k][4]
    if len(F) <= k + 1:
        info["msg"] = "waiting for the 09:30 open"
        return None, info
    p = {**s["params"], "tf": 5}
    r = onmid.engine(und, day, on, None, F, None, onmid.N.ifvg.ema_track(F), None, p)
    if not r:
        info["msg"] = "no trade today (open outside the range, in the dead zone, or the side / R:R filter)"
        return None, info
    x = r[0][0]
    hm = now_ny().strftime("%H:%M")
    t = {"s": fut, "d": day, "in": x["in"][:16], "out": str(x["out"])[:16], "side": x["side"], "why": x["why"], "e": x["e"], "x": x["x"],
         "stop": x.get("stop"), "tgt": x.get("tgt"), "und": und,
         "status": "open" if x["why"] == "close" and hm < "16:00" else "closed"}
    info.update(side=t["side"], stop=t["stop"], tgt=t["tgt"], status=t["status"], why=t["why"])
    return t, info


def tick(sub, s, trades):
    """Called after paper_one: opens legs for new open trades today, closes legs whose trade closed."""
    cfg = config(s)
    st = store.get_json(_path(sub, s["id"]), {}) or {}
    legs, log = st.get("legs") or {}, st.get("log") or []
    if not cfg["on"] and not any(not l.get("sell") and (l.get("buy") or {}).get("status") == "filled" for ls in legs.values() for l in ls):
        return st
    c = alpaca.creds(sub)
    now = now_ny()
    today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    sig = st.get("signal") or {}
    if s.get("setup") in LIVE_SETUPS and now.weekday() < 5 and "09:30" <= hm < "16:30":
        trades, sig = [], {}
        for fut in s["params"]["tickers"]:
            if fut in UND:
                t, info = live_signal(sub, s, fut, today)
                sig[fut] = {**info, "at": hm}
                if t:
                    trades.append(t)
    by = {_key(t): t for t in trades}
    for k, ls in legs.items():                       # fills first, then exits
        for l in ls:
            _update(c, l)
    for k, ls in legs.items():
        t = by.get(k)
        why = None
        if k.split("|")[1] < today:
            why = "expired"
        elif hm >= FLAT_AT:
            why = "15:50 flat"
        elif t is None:
            why = "trade gone"
        elif t.get("status") == "closed":
            why = f"trade {t.get('why')} {t.get('out', '')[-5:]}"
        if why:
            for l in ls:
                if why == "expired":
                    if not l.get("sell") and (l.get("buy") or {}).get("status") == "filled":
                        l["sell"] = {"status": "expired", "why": "expired"}
                    continue
                _close_leg(c, l, why, log)
    if cfg["on"] and "09:30" <= hm < LAST_ENTRY:
        for k, t in by.items():
            if t["d"] == today and t.get("status") == "open" and k not in legs and t["s"] in UND:
                legs[k] = _open_legs(c, t, cfg, log)
                log.append(f"{hm} opened {t['side']} {t['s']} {t['in']}: {len(legs[k])} legs")
    for ls in legs.values():
        for l in ls:
            _update(c, l)
    st = {"legs": legs, "log": log[-50:], "at": iso(now), "signal": sig}
    store.put_json(_path(sub, s["id"]), st)
    return st


def close_all(sub, s):
    """Sell every open leg now (paper stopped or Alpaca options switched off)."""
    st = store.get_json(_path(sub, s["id"]), {}) or {}
    if not st.get("legs"):
        return st
    c, log = alpaca.creds(sub), st.get("log") or []
    for ls in st["legs"].values():
        for l in ls:
            _update(c, l)
            _close_leg(c, l, "stopped", log)
            _update(c, l)
    st["log"] = log[-50:]
    store.put_json(_path(sub, s["id"]), st)
    return st


def view(sub, s):
    st = store.get_json(_path(sub, s["id"]), {}) or {}
    rows = [l for ls in (st.get("legs") or {}).values() for l in ls]
    by = {}
    for l in rows:
        b = by.setdefault(l["target"], {"delta": l["target"], "n": 0, "done": 0, "pnl": 0.0, "win": 0})
        b["n"] += 1
        if l.get("pnl") is not None:
            b["done"] += 1
            b["pnl"] = round(b["pnl"] + l["pnl"], 2)
            b["win"] += l["pnl"] > 0
    acct = None
    try:
        acct = account(sub)
    except Exception as e:
        acct = {"error": str(e)[:200]}
    return {"cfg": config(s), "legs": sorted(rows, key=lambda l: ((l.get("buy") or {}).get("at") or "", l["target"]), reverse=True)[:120],
            "summary": [by[k] for k in sorted(by)], "log": (st.get("log") or [])[-15:], "at": st.get("at"), "account": acct,
            "live": s.get("setup") in LIVE_SETUPS, "signal": st.get("signal") or {}}
