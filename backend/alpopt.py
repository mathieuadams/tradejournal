"""Same-day options on the user's Alpaca PAPER account, following a paper strategy's NQ / ES trades.

When a paper trade opens (strategy.paper_one marks it "open"), 1 contract (or `qty`) is bought at each target delta:
a call for a long, a put for a short, on QQQ (NQ) or SPY (ES), expiring today. Exit ("exitOn"):
  mid (default)  every leg is sold only when a 1-minute futures close crosses back over the trade's stop level (the
                 overnight midpoint for the midpoint strategies), recorded when the legs were bought. The futures
                 trade's target or time exit (e.g. "Out at 11:30") and a re-run that drops the trade do not sell.
  trade          every leg is sold when the futures trade closes (stop, target or time exit).
Anything still open is sold at 15:50: same-day options can't be held through the close.
The simulation is re-run on every check: a trade whose entry moved by a bar is still the same trade (SHIFT_MIN), and a
trade missing from the re-run is only sold after GONE_CHECKS checks in a row, with the reason logged.

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
DEFAULT = {"on": False, "deltas": [0.3, 0.5, 0.7], "qty": 1, "exitOn": "mid"}
EXITS = ("mid", "trade")       # mid: sell only when a futures close crosses back over the trade's stop (the midpoint);
                               # trade: sell on any exit of the futures trade (stop, target, time)
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
    ex = b.get("exitOn") or DEFAULT["exitOn"]
    if ex not in EXITS:
        raise BadRequest("Exit: mid or trade.")
    return {"on": bool(b.get("on")), "deltas": ds, "qty": qty, "exitOn": ex}


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


SHIFT_MIN = 5                                         # a recalculated trade's entry may move one 5-minute bar and still be the same trade
GONE_CHECKS = 2                                       # "trade gone" must be seen on this many checks in a row before selling


def _mins(hhmm):
    try:
        return int(hhmm[-5:-3]) * 60 + int(hhmm[-2:])
    except (TypeError, ValueError):
        return None


def _find(k, by, claimed, pool=None):
    """The trade behind a set of legs: the same key, else the same symbol / day / side with the entry moved by at most
    SHIFT_MIN minutes (a re-run of the simulation can move the entry by a bar). Returns (key, trade) or (None, None)."""
    if k in by:
        return k, by[k]
    s_, d_, in_, side = k.split("|")
    m0, best = _mins(in_), (None, None, 10 ** 9)
    for k2, t in (pool if pool is not None else by).items():
        if k2 in claimed or t["s"] != s_ or t["d"] != d_ or t["side"] != side:
            continue
        m = _mins(t["in"])
        if m0 is not None and m is not None and abs(m - m0) <= SHIFT_MIN and abs(m - m0) < best[2]:
            best = (k2, t, abs(m - m0))
    return best[0], best[1]


def _crossed(meta, today):
    """The first 1-minute futures close since the entry that is back across the stop level: ('HH:MM', price) or None."""
    import futdata
    lvl, side, sym, t_in = meta.get("stop"), meta.get("side"), meta.get("s"), (meta.get("in") or "")[-5:]
    if lvl is None or not sym:
        return None
    try:
        bars = futdata.bars_between(sym, today, today)
    except Exception as e:
        print("alpopt bars", e)
        return None
    for b in bars:
        hm = b[0][11:16]
        if hm <= t_in:
            continue
        if (side == "long" and b[4] < lvl) or (side == "short" and b[4] > lvl):
            return hm, b[4]
    return None


def tick(sub, s, trades, dropped=None):
    """Called after paper_one: opens legs for new open trades today, closes legs whose trade closed.
    `dropped`: [(trade, reason)] the simulation found but the paper filters removed (market condition, daily loss)."""
    cfg = config(s)
    st = store.get_json(_path(sub, s["id"]), {}) or {}
    legs, log, gone, meta = st.get("legs") or {}, st.get("log") or [], st.get("gone") or {}, st.get("meta") or {}
    if not cfg["on"] and not any(not l.get("sell") and (l.get("buy") or {}).get("status") == "filled" for ls in legs.values() for l in ls):
        return st
    c = alpaca.creds(sub)
    now = now_ny()
    today, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    by = {_key(t): t for t in trades}
    drop = {_key(t): (t, why) for t, why in (dropped or [])}
    for k, ls in legs.items():                       # fills first, then exits
        for l in ls:
            _update(c, l)
    claimed = set()
    for k in legs:                                   # exact matches first, so a shifted match never steals another's trade
        if k in by:
            claimed.add(k)
    for k, ls in legs.items():
        done = all(l.get("sell") or (l.get("buy") or {}).get("status") in ("error", "canceled", "rejected", "expired") for l in ls)
        k2, t = _find(k, by, claimed - {k}) if not done or k in by else (None, None)
        if k2:
            claimed.add(k2)
            if k2 != k and not any(l.get("shifted") == k2 for l in ls):
                for l in ls:
                    l["shifted"] = k2
                log.append(f"{hm} {k}: the recalculated trade now enters at {t['in'][-5:]}; the legs follow it")
        if k not in meta and t is not None:          # the stop level the legs were bought against
            meta[k] = {"stop": t.get("stop"), "side": t["side"], "s": t["s"], "in": t["in"]}
        open_legs = any(not l.get("sell") and (l.get("buy") or {}).get("status") in ("filled", "new", "accepted", "pending_new", "partially_filled") for l in ls)
        why = None
        if k.split("|")[1] < today:
            why = "expired"
        elif hm >= FLAT_AT:
            why = "15:50 flat"
        elif cfg["exitOn"] == "mid":
            m = meta.get(k) or {}
            hit = _crossed(m, today) if open_legs else None
            if hit:
                why = f"midpoint crossed {hit[1]:,.2f} at {hit[0]}"
            elif open_legs and m.get("stop") is None and t is not None and t.get("status") == "closed" and t.get("why") == "stop":
                why = f"trade stop {t.get('out', '')[-5:]}"           # no level recorded: the futures stop itself
        elif t is None:
            if open_legs:
                _, dt_ = _find(k, {kk: v[0] for kk, v in drop.items()}, set())
                reason = next((v[1] for kk, v in drop.items() if v[0] is dt_), None) if dt_ else None
                g = gone.get(k) or {"n": 0, "since": hm}
                g["n"] += 1
                g["reason"] = reason or "no longer in the simulation's trades"
                gone[k] = g
                if g["n"] >= GONE_CHECKS:
                    why = f"trade gone ({g['reason']}, since {g['since']})"
                else:
                    log.append(f"{hm} {k}: missing from the recalculation ({g['reason']}); legs kept, checking again")
        elif t.get("status") == "closed":
            why = f"trade {t.get('why')} {t.get('out', '')[-5:]}"
        if t is not None:
            gone.pop(k, None)
        if why:
            for l in ls:
                if why == "expired":
                    if not l.get("sell") and (l.get("buy") or {}).get("status") == "filled":
                        l["sell"] = {"status": "expired", "why": "expired"}
                    continue
                _close_leg(c, l, why, log)
    if cfg["on"] and "09:30" <= hm < LAST_ENTRY:
        for k, t in by.items():
            if t["d"] == today and t.get("status") == "open" and k not in legs and k not in claimed and t["s"] in UND:
                legs[k] = _open_legs(c, t, cfg, log)
                meta[k] = {"stop": t.get("stop"), "side": t["side"], "s": t["s"], "in": t["in"]}
                log.append(f"{hm} opened {t['side']} {t['s']} {t['in']}: {len(legs[k])} legs")
    for ls in legs.values():
        for l in ls:
            _update(c, l)
    st = {"legs": legs, "log": log[-50:], "at": iso(now), "gone": {k: v for k, v in gone.items() if k.split("|")[1] >= today},
          "meta": {k: v for k, v in meta.items() if k.split("|")[1] >= today}}
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
            "summary": [by[k] for k in sorted(by)], "log": (st.get("log") or [])[-15:], "at": st.get("at"), "account": acct}
