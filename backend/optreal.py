"""The strategy's option trades (legs C and D) re-priced with REAL option prices from Unusual Whales.

For every C / D trade of the backtest (as a call for C, a put for D, with the leg's expiry and strike settings):
  1. the contracts listed on the entry day: GET /api/stock/{T}/option-contracts?date=… (option_type, min_dte, max_dte,
     open interest > 0), else GET /api/stock/{T}/option-chains?date=… (every contract of that day);
  2. the contract: the expiry nearest to entry + `dte` days, then the strike nearest to the entry price × (1 ± `mny`);
  3. its daily history: GET /api/option-contract/{OCC}/historic (one row per trading day: open, last, NBBO bid / ask,
     implied volatility, underlying close); kept in S3 (an expired contract never changes);
  4. bought at the entry day's NBBO ask (an entry at the open: the day's first trade), sold at the exit day's NBBO bid
     — what you'd really pay and get with market orders; the mid (bid + ask) / 2 is shown as well. A contract that
     expires before the exit is worth its intrinsic value at expiry (the stock's close that day).
The daily rows have no intraday prices, so an exit by a stop / trailing stop / target is priced at that day's close
quotes. How far back it goes depends on the Unusual Whales plan (the API's history starts in 2022); earlier trades are
left on the model price and counted apart.

Results in fund/u/<sub>/optreal; a background job ("fundreal"), resumed across runs (1-3 calls per trade)."""
import time
from datetime import date, timedelta

import store
from util import BadRequest, Unavailable, iso, now_ny

START = "2022-01-01"                                  # the oldest option data Unusual Whales has
V = 3                                                 # 3: as-traded prices (splits); an expiry that outlives the planned exit
MIN_PREMIUM = 0.05                                    # an entry under 5 cents isn't a real fill
KEY = "fund/u/{sub}/optreal"


def _d(s):
    return date.fromisoformat(str(s)[:10])


def _n(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def tkey(t):
    return f"{t['leg']}|{t['sym']}|{t['entry']}|{t['exit']}"


def uw_sym(sym):
    return sym.replace("-", ".")                       # BRK-B → BRK.B


# ---------------------------------------------------------------- Unusual Whales calls (cached)

def chain_on(tok, sym, day, call, dte):
    """[(occ, expiry, strike, open interest)] of one type listed on `day`, expiries within roughly dte/2 .. dte×2."""
    import uwhales
    ck = f"fund/uwchain/{sym}/{day}/{'C' if call else 'P'}"
    hit = store.get_json(ck)
    if hit is not None:
        return [tuple(x) for x in hit["c"]]
    lo, hi = max(1, dte // 2), dte * 2 + 10
    out = []

    def keep(rows):
        for x in rows:
            if not isinstance(x, dict):
                continue
            o = uwhales._occ(x.get("option_symbol"))
            if not o or o[1] != ("C" if call else "P"):
                continue
            n = (_d(o[0]) - _d(day)).days
            if lo <= n <= hi:
                out.append((x["option_symbol"], o[0], o[2], int(_n(x.get("open_interest")) or 0)))
    page = 0
    while page < 20:                                   # the filtered list: one type, the expiries we may use
        r = uwhales._get_retry(tok, f"stock/{uw_sym(sym)}/option-contracts",
                               {"date": day, "option_type": "call" if call else "put", "min_dte": lo, "max_dte": hi,
                                "exclude_zero_oi_chains": "true", "limit": 500, "page": page})
        rows = (r.get("data") if isinstance(r, dict) else r) or []
        keep(rows)
        if len(rows) < 500:
            break
        page += 1
    if not out:                                        # that endpoint ignored the date: the whole chain of the day
        page = 0
        while page < 40:
            r = uwhales._get_retry(tok, f"stock/{uw_sym(sym)}/option-chains", {"date": day, "limit": 500, "page": page})
            rows = (r.get("data") if isinstance(r, dict) else r) or []
            keep([x if isinstance(x, dict) else {"option_symbol": x} for x in rows])
            if len(rows) < 500:
                break
            page += 1
    store.put_json(ck, {"c": out})
    return out


def pick(chain, day, dte, strike, need=None):
    """The expiry nearest to day + dte among those on or after `need` (the planned exit: the option must still be alive
    to sell it; any expiry if none is); then the strike nearest to `strike` (open interest breaks ties)."""
    if not chain:
        return None
    want = _d(day) + timedelta(days=dte)
    alive = {c[1] for c in chain if need and c[1] >= need} or {c[1] for c in chain}
    exps = sorted(alive, key=lambda e: (abs((_d(e) - want).days), -_d(e).toordinal()))
    ex = exps[0]
    cs = [c for c in chain if c[1] == ex]
    return min(cs, key=lambda c: (abs(c[2] - strike), -c[3]))


def history(tok, occ):
    """{date: row} of one contract's daily history (kept: an expired contract is final; a live one for 12 hours)."""
    import uwhales
    ck = f"fund/uwhist/{occ}"
    hit = store.get_json(ck)
    exp = uwhales._occ(occ)[0]
    if hit is not None and (exp < hit.get("at", "")[:10] or hit.get("at", "") > iso(now_ny() - timedelta(hours=12))):
        return hit["rows"]
    r = uwhales._get_retry(tok, f"option-contract/{occ}/historic")
    rows = {}
    for x in (r.get("chains") or r.get("data") or []) if isinstance(r, dict) else []:
        d = str(x.get("date") or "")[:10]
        if d:
            rows[d] = {k: _n(x.get(k)) for k in ("open_price", "last_price", "high_price", "low_price", "nbbo_bid", "nbbo_ask",
                                                   "implied_volatility", "underlying_close", "volume", "open_interest")}
    store.put_json(ck, {"at": iso(now_ny()), "rows": rows})
    return rows


# ---------------------------------------------------------------- one trade

def price_trade(tok, t, L, close_px, close_on, adj_close):
    """The real result of one C / D trade as an option. `close_px`: the stock's day-0 close (or open, for an entry at the
    open), `adj_close`: its day-0 close, `close_on(day)`: its close on a day (for an expiry before the exit) — all from
    split-ADJUSTED bars; the contract's own history gives the price as it traded then, and the strike is re-picked if a
    later split makes them differ."""
    import backtest
    call = t["leg"] == "C"
    ch = chain_on(tok, t["sym"], t["entry"], call, L["dte"])
    f = 1.0                                            # traded price / adjusted price (a later split)
    for _ in range(2):
        strike = close_px * f * (1 + L["mny"] if call else 1 - L["mny"])
        c = pick(ch, t["entry"], L["dte"], strike, t["exit"])
        if not c:
            return {"err": "no listed contract"}
        occ, ex, k, _ = c
        h = history(tok, occ)
        e = h.get(t["entry"])
        if not e:
            return {"err": "no quote on the entry day", "occ": occ}
        u = e.get("underlying_close")
        if not u or not adj_close or abs(u / (adj_close * f) - 1) < 0.03:
            break
        f = u / adj_close
    at_open = L.get("entry") == "open"
    bid0, ask0 = e.get("nbbo_bid"), e.get("nbbo_ask")
    if at_open:
        buy = e.get("open_price") or e.get("last_price")
        mid0 = buy
    else:
        buy = ask0 if ask0 and ask0 > 0 else e.get("last_price")
        mid0 = (bid0 + ask0) / 2 if bid0 is not None and ask0 else buy
    if not buy or buy <= 0:
        return {"err": "no entry price", "occ": occ}
    if buy < MIN_PREMIUM:
        return {"err": "premium under $0.05", "occ": occ}
    if abs(k / strike - 1) > 0.25:
        return {"err": "no strike near the target", "occ": occ}
    xd = t["exit"]
    how = "close"
    if ex < xd:                                        # expired first: intrinsic value at expiry
        u = (h.get(ex) or {}).get("underlying_close") or (close_on(ex) * f if close_on(ex) is not None else None)
        if u is None:
            return {"err": "no stock close at expiry", "occ": occ}
        sell = smid = max(0.0, u - k) if call else max(0.0, k - u)
        xd, how = ex, "expired"
    else:
        days = sorted(d for d in h if t["entry"] < d <= xd)
        if not days:
            return {"err": "no quote after the entry", "occ": occ}
        x = h[days[-1]]
        if days[-1] != xd:
            how = f"last quote {days[-1]}"
        bid, ask = x.get("nbbo_bid"), x.get("nbbo_ask")
        sell = bid if bid is not None and bid >= 0 and ask else x.get("last_price")
        smid = (bid + ask) / 2 if bid is not None and ask else x.get("last_price")
        if sell is None:
            return {"err": "no exit price", "occ": occ}
    vol_model = backtest.opt_vol({"vol20": t.get("vol20")}, 1.0) if t.get("vol20") else None
    return {"occ": occ, "expiry": ex, "strike": k, "buy": round(buy, 4), "sell": round(sell, 4), "mid0": round(mid0, 4), "mid1": round(smid, 4),
            "ret": sell / buy - 1, "retMid": smid / mid0 - 1 if mid0 else None, "iv": e.get("implied_volatility"), "volModel": vol_model,
            "spread": (ask0 - bid0) / ((ask0 + bid0) / 2) if bid0 is not None and ask0 and ask0 + bid0 > 0 else None,
            "exitDay": xd, "how": how}


# ---------------------------------------------------------------- the job and the view

def start(sub, cfg):
    """A new run (the trades priced before with the same settings are kept)."""
    import backtest
    import jobs
    key = KEY.format(sub=sub)
    c = backtest.clean(cfg)
    for k in "CD":
        c["legs"][k]["inst"] = "option"
    if not any(c["legs"][k]["on"] for k in "CD"):
        raise BadRequest("Turn on leg C or D: the real option check prices their trades.")
    import uwhales
    uwhales.token(sub)                                 # connected?
    st = store.get_json(key) or {}
    same = st.get("cfg") and {k: st["cfg"]["legs"][k] for k in "CD"} == {k: c["legs"][k] for k in "CD"} and st["cfg"]["stake"] == c["stake"]
    same = same and st.get("v") == V
    store.put_json(key, {"v": V, "cfg": c, "done": st.get("done") if same else {}, "complete": False})
    return jobs.start(sub, "fund", "fundreal", {})


def run_job(sub, prog=None, deadline=None):
    """Re-price every C / D trade since START (as options, with the legs' expiry / strike) with real prices; resumable."""
    import backtest
    import fundamentals as F
    import uwhales
    key = KEY.format(sub=sub)
    st = store.get_json(key) or {}
    cfg = backtest.clean(st.get("cfg") or {})
    for k in "CD":
        cfg["legs"][k]["inst"] = "option"
    tok = uwhales.token(sub)
    rows = backtest.prepare(sub, prog)
    paths = backtest.load_paths(sub)
    trades = [t for t in backtest.trades_for(rows, backtest.context(rows), cfg, paths=paths) if t["leg"] in "CD"]
    vol_by = {(r["sym"], r.get("day0")): r.get("vol20") for r in rows}
    done = st.get("done") or {}
    todo = [t for t in trades if t["entry"] >= START and tkey(t) not in done]
    px_cache = {}

    def bars_of(sym):
        """The stock's daily bars AS TRADED (not split-adjusted): option strikes are in those prices."""
        if sym not in px_cache:
            px_cache.clear()
            px_cache[sym] = raw_bars(sub, sym)
        return px_cache[sym]
    todo.sort(key=lambda t: (t["sym"], t["entry"]))
    t_last, n0, stopped = 0.0, len(done), False
    for n, t in enumerate(todo):
        if deadline and time.time() > deadline:
            stopped = True
            break
        if prog and time.time() - t_last > 3:
            prog(f"Real option prices: {n0 + n + 1} of {n0 + len(todo)} trades ({t['sym']} {t['entry']})")
            t_last = time.time()
        L = cfg["legs"][t["leg"]]
        b = bars_of(t["sym"])
        d0 = b.get(t["entry"])
        if not d0:
            done[tkey(t)] = {"err": "no stock bar on the entry day"}
            continue
        try:
            res = price_trade(tok, {**t, "vol20": vol_by.get((t["sym"], t["entry"]))}, L, d0[1] if L.get("entry") == "open" else d0[4],
                              lambda day: (b.get(day) or [None] * 5)[4], d0[4])
        except BadRequest as ex:
            msg = str(ex)
            if "historic_data_access_missing" in msg or "403" in msg:
                res = {"err": "before the plan's history"}
            else:
                res = {"err": msg[:120]}
        except Unavailable as ex:
            if "rate limit" in str(ex):                # left for the next run (Run again fills the gaps)
                time.sleep(20)
                continue
            res = {"err": str(ex)[:120]}
        done[tkey(t)] = res
        if n % 50 == 0:
            store.put_json(key, {**st, "cfg": cfg, "done": done, "complete": False})
    st.update(cfg=cfg, done=done, complete=not stopped, at=iso(now_ny()))
    store.put_json(key, st)
    if not stopped:
        store.put_json(key + "_view", summarize(trades, done, cfg))
    return not stopped


def raw_bars(sub, sym):
    """{date: bar} of the unadjusted daily bars since START (Alpaca adjustment=raw), kept a day; the split-adjusted
    study prices otherwise (a stock that never split is the same)."""
    import alpaca
    ck = f"fund/pxraw/{sym}"
    hit = store.get_json(ck)
    if hit and hit.get("at", "") > iso(now_ny() - timedelta(hours=20)):
        return {x[0]: x for x in hit["bars"]}
    try:
        b = alpaca.daily_range(sub, sym, (_d(START) - timedelta(days=5)).isoformat(), adjustment="raw")
        store.put_json(ck, {"at": iso(now_ny()), "bars": b})
    except Exception:
        b = (store.get_json("fund/px/" + sym) or {}).get("bars") or []
    return {x[0]: x for x in b}


def _agg(xs, k):
    v = [x[k] for x in xs if x.get(k) is not None]
    return round(sum(v), 2) if v else 0.0


def summarize(trades, done, cfg):
    """Model vs real, per leg and year, on the same trades; the model's volatility against the real implied one."""
    import statistics
    stake, spread = cfg["stake"], cfg["optSpreadPct"] / 100
    out, ivr, spreads, miss = [], [], [], {}
    for t in trades:
        r = done.get(tkey(t))
        if t["entry"] < START:
            continue
        if not r or r.get("err"):
            miss[(r or {}).get("err") or "not priced yet"] = miss.get((r or {}).get("err") or "not priced yet", 0) + 1
            continue
        real = round(stake * r["ret"], 2)
        out.append({**{k: t[k] for k in ("leg", "sym", "release", "entry", "why")}, "exit": r.get("exitDay") or t["exit"], "plannedExit": t["exit"], "occ": r["occ"], "expiry": r["expiry"], "strike": r["strike"],
                    "buy": r["buy"], "sell": r["sell"], "model": t["pnl"], "real": real,
                    "realMid": round(stake * r["retMid"], 2) if r.get("retMid") is not None else None,
                    "iv": r.get("iv"), "how": r["how"], "stock": t["ret"]})
        if r.get("iv") and r.get("volModel"):
            ivr.append(r["iv"] / r["volModel"])
        if r.get("spread") is not None:
            spreads.append(r["spread"])

    def block(xs):
        if not xs:
            return {"n": 0}
        w = [x for x in xs if x["real"] > 0]
        return {"n": len(xs), "model": _agg(xs, "model"), "real": _agg(xs, "real"), "realMid": _agg(xs, "realMid"),
                "win": len(w) / len(xs), "best": max(x["real"] for x in xs), "worst": min(x["real"] for x in xs)}
    legs = {k: block([x for x in out if x["leg"] == k]) for k in "CD"}
    years = {}
    for x in out:
        years.setdefault(x["exit"][:4], []).append(x)
    top = sorted(out, key=lambda x: -abs(x["real"]))[:20]
    med = lambda v: round(statistics.median(v), 3) if v else None
    return {"at": iso(now_ny()), "from": START, "cfg": cfg, "all": block(out), "legs": legs,
            "years": [{"year": y, **block(v)} for y, v in sorted(years.items())],
            "ivRatio": med(ivr), "spreadMedian": med(spreads), "modelSpread": spread, "missing": miss,
            "top": top, "trades": sorted(out, key=lambda x: x["exit"])[-1500:]}


def view(sub):
    import jobs
    st = store.get_json(KEY.format(sub=sub)) or {}
    v = store.get_json(KEY.format(sub=sub) + "_view")
    return {"view": v, "done": len(st.get("done") or {}), "complete": st.get("complete"), "job": jobs.status(sub, "fund")}
