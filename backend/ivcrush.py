"""IV crush on the S&P 500: sell out-of-the-money puts when a stock's IV rank is high, buy them back at 50% of the
credit. Data: Unusual Whales (the user's token; history from 2022).

Universe: the current S&P 500 (FMP sp500-constituent), saved when the IV history is downloaded.

Data:
  - IV rank: GET /api/stock/{T}/iv-rank?date=<end>&timespan=1y (date, close, iv_rank_1y, volatility), a year per call
    walking back to 2022, per stock, kept
    in store ivc/uw/<T> (job "ivcdata", ~500 calls).
  - The puts, fetched by the backtest when it needs them (cached, so a second run is fast): the contracts listed on the
    entry day (GET /api/stock/{T}/option-contracts?date=…&option_type=put, via optreal.chain_on), then each candidate
    contract's daily history (GET /api/option-contract/{OCC}/historic: NBBO bid / ask, last, implied volatility).

Rules (per stock, one put at a time on a stock):
  - Entry: IV rank (Unusual Whales, 1 year) >= `rank` → the expiry nearest `dte` days (within dteMin-dteMax); the strike
    for the target `delta` from the stock's IV (Black-Scholes), then of the 3 listed strikes nearest it, the one whose
    delta that day (its own IV) is nearest the target (within 0.10), premium >= minPx. Sold at the day's NBBO bid
    (no NBBO: last - slip).
  - Exit, checked on each daily row of the contract: the NBBO ask (no NBBO: last + slip) <= (1 - take) x the credit →
    bought back there; loss >= stop x the credit (0 = off); `manage` days to expiry (0 = hold); else it expires (strike
    - the stock's close, or 0). A day the contract has no row is skipped.
  - Roll (option): from `rollDte` days to expiry, when the put is tested (its delta >= rollDelta), buy it back and sell
    a put of a later expiry (nearest `dte`, within dteMin-dteMax), only for a net credit >= rollMin a share: rollMode
    "out" = in time at the same strike, "down" = also down to a lower strike, "both" = the same strike or lower (the
    lowest that gives the credit); at most rollMax rolls. No
    credit roll: closed. Take profit / stop then measured on the net credit received over all the legs.
  - Commission `comm` a contract each side (none when it expires worthless).
Portfolio: at most `maxPos` puts open; the day's candidates taken in IV-rank order while there's room. Each position:
`n` contracts = (equity x alloc / maxPos) / collateral (cash-secured: strike x 100; margin: Reg T ≈ max(20% of the price
- the OTM amount, 10% of the strike) x 100); fewer than 1: skipped. Equity marked to market every day (mid)."""
import math
import time
from datetime import date, timedelta
from statistics import NormalDist

import store
from util import BadRequest, iso, now_ny

START = "2022-01-01"
DEFAULT = {"rank": 30.0, "dte": 45, "dteMin": 30, "dteMax": 60, "delta": 0.20, "take": 0.50, "manage": 21, "stop": 0.0,
           "maxPos": 20, "minPx": 0.20, "slip": 0.03, "comm": 0.65, "acct": 500000.0, "alloc": 1.0, "coll": "cash", "split": 0.6,
           "from": "2022-03-01", "fill": "nbbo", "roll": 0, "rollDte": 21, "rollDelta": 0.40, "rollMin": 0.10, "rollMax": 3, "rollMode": "both"}
RATES = {2021: .0005, 2022: .0202, 2023: .0507, 2024: .0497, 2025: .0420, 2026: .0370, 2027: .0350}


def clean(cfg):
    c = {**DEFAULT, **{k: v for k, v in (cfg or {}).items() if k in DEFAULT}}
    for k, lo, hi in (("rank", 0, 100), ("dte", 7, 120), ("dteMin", 1, 120), ("dteMax", 7, 180), ("delta", 0.03, 0.5),
                      ("take", 0.1, 0.95), ("manage", 0, 60), ("stop", 0, 10), ("maxPos", 1, 200), ("minPx", 0.01, 5), ("slip", 0, 1),
                      ("comm", 0, 5), ("acct", 1000, 1e9), ("alloc", 0.05, 5), ("split", 0.3, 0.9),
                      ("roll", 0, 1), ("rollDte", 1, 60), ("rollDelta", 0.1, 0.9), ("rollMin", 0, 5), ("rollMax", 1, 10)):
        try:
            c[k] = max(lo, min(hi, float(c[k])))
        except (TypeError, ValueError):
            c[k] = DEFAULT[k]
    for k in ("dte", "dteMin", "dteMax", "manage", "maxPos", "roll", "rollDte", "rollMax"):
        c[k] = int(c[k])
    c["coll"] = "margin" if c["coll"] == "margin" else "cash"
    c["fill"] = "mid" if c["fill"] == "mid" else "nbbo"
    c["rollMode"] = c["rollMode"] if c["rollMode"] in ("out", "down", "both") else "both"
    f = str(c["from"])[:10]
    c["from"] = f if len(f) == 10 and f >= START else DEFAULT["from"]
    return c


# ---------------------------------------------------------------- Black-Scholes

def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, v, r, call):
    if T <= 0 or v <= 0:
        return max(0.0, (S - K) if call else (K - S))
    sq = v * math.sqrt(T)
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / sq
    d2 = d1 - sq
    if call:
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def put_delta(S, K, T, v, r):
    if T <= 0 or v <= 0:
        return -1.0 if K > S else 0.0
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / (v * math.sqrt(T))
    return _ncdf(d1) - 1


def strike_for(S, T, v, r, delta):
    """The strike of a put with delta -`delta`."""
    d1 = NormalDist().inv_cdf(1 - delta)
    return S * math.exp(-d1 * v * math.sqrt(T) + (r + v * v / 2) * T)


def rate(day):
    return RATES.get(int(day[:4]), 0.03)


def _n(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def uw_sym(sym):
    return sym.replace("-", ".")


# ---------------------------------------------------------------- the universe and the IV history

def universe(sub=None, refresh=False):
    u = store.get_json("ivc/uni")
    if (refresh or not u) and sub:
        import fmp
        syms = sorted(set(fmp.sp500(sub, 0)["current"]))
        if not syms:
            raise BadRequest("FMP returned no S&P 500 list (connect FMP on the Data page).")
        u = {"syms": syms, "at": iso(now_ny())}
        store.put_json("ivc/uni", u)
    return u or {"syms": [], "at": None}


def _ixd():
    return store.get_json("ivc/uwix") or {"s": {}}


def status():
    ix, u = _ixd(), universe()
    have = [ix["s"][s] for s in u["syms"] if s in ix["s"]]
    return {"syms": len(u["syms"]), "uniAt": u["at"], "have": len(have), "first": min((h["first"] for h in have if h.get("first")), default=None),
            "last": max((h["last"] for h in have if h.get("last")), default=None), "at": ix.get("at"), "cache": ix.get("cache", 0)}


def iv_series(tok, sym):
    """[[date, close, IV rank 0-100, IV or None], ...] from 2022 (Unusual Whales iv-rank)."""
    import uwhales
    rows = {}

    def take(r):
        for x in (r.get("data") if isinstance(r, dict) else r) or []:
            if not isinstance(x, dict):
                continue
            d = str(x.get("date") or "")[:10]
            rk, c = _n(x.get("iv_rank_1y")), _n(x.get("close"))
            if d and rk is not None and c:
                rows[d] = [d, c, rk, _n(x.get("volatility") or x.get("iv") or x.get("implied_volatility"))]
    d = now_ny().date()                                 # a year up to `date` per call, walking back to 2022
    miss = 0
    for _ in range(80):
        n0 = len(rows)
        try:
            take(uwhales._get_retry(tok, f"stock/{uw_sym(sym)}/iv-rank", {"date": d.isoformat(), "timespan": "1y"}))
        except BadRequest:                              # not a trading day / nothing that day: a day earlier
            miss += 1
            if miss > 4:
                break
            d -= timedelta(days=1)
            continue
        if len(rows) == n0 or min(rows) <= START:
            break
        d = date.fromisoformat(min(rows)) - timedelta(days=1)
    out = sorted(rows.values())
    if out and max(r[2] for r in out) <= 1.5:          # a 0-1 scale
        for r in out:
            r[2] = round(r[2] * 100, 2)
    return out


def download(sub, p, prog, deadline):
    """The job: the universe, then every stock's IV rank history. Params to carry on with, or None when done."""
    import uwhales
    tok = uwhales.token(sub)
    syms = universe(sub, refresh=not p.get("uni"))["syms"]
    p = {**p, "uni": True}
    done = set(p.get("done") or [])
    ix = _ixd()
    for k, s in enumerate(syms):
        if s in done:
            continue
        if time.time() > deadline:
            store.put_json("ivc/uwix", ix)
            return {**p, "done": sorted(done)}
        prog(f"IV rank history {len(done) + 1} / {len(syms)}: {s}")
        try:
            rows = iv_series(tok, s)
        except Exception as e:                         # a ticker Unusual Whales doesn't know: noted, go on
            print("ivcrush iv", s, e)
            rows = []
        store.put_json(f"ivc/uw/{s}", rows)
        ix["s"][s] = {"n": len(rows), "first": rows[0][0] if rows else None, "last": rows[-1][0] if rows else None}
        done.add(s)
        if len(done) % 25 == 0:
            ix["at"] = iso(now_ny())
            store.put_json("ivc/uwix", ix)
    ix["at"] = iso(now_ny())
    store.put_json("ivc/uwix", ix)
    return None


# ---------------------------------------------------------------- one stock

def _px(row, side, c):
    """What we get selling (side "bid") / pay buying back ("ask") on a daily row; None without a usable price."""
    b, a, last = row.get("nbbo_bid"), row.get("nbbo_ask"), row.get("last_price")
    if b and a and a >= b > 0:
        if c["fill"] == "mid":
            m = (a + b) / 2
            return m - c["slip"] if side == "bid" else m + c["slip"]
        return b if side == "bid" else a
    if last and last > 0:
        return last - c["slip"] if side == "bid" else last + c["slip"]
    return None


def _mid(row):
    b, a, last = row.get("nbbo_bid"), row.get("nbbo_ask"), row.get("last_price")
    return (a + b) / 2 if b and a and a >= b else last


_MEMO = {}                                            # one stock's chains / contract histories (cleared per stock)


def _chain(tok, sym, day, dte):
    import optreal
    k = ("c", sym, day)
    if k not in _MEMO:
        _MEMO[k] = optreal.chain_on(tok, sym, day, False, max(dte, 42))
    return _MEMO[k]


def _hist(tok, occ):
    import optreal
    k = ("h", occ)
    if k not in _MEMO:
        _MEMO[k] = optreal.history(tok, occ)
    return _MEMO[k]


def _entry(tok, sym, day, S, iv, c):
    """(occ, expiry, strike, credit, delta, its IV, history) of the put to sell on `day`, or None."""
    ch = [x for x in _chain(tok, sym, day, c["dte"]) if x[2] < S]
    d0 = date.fromisoformat(day)
    exps = sorted({x[1] for x in ch if c["dteMin"] <= (date.fromisoformat(x[1]) - d0).days <= c["dteMax"]},
                  key=lambda e: abs((date.fromisoformat(e) - d0).days - c["dte"]))
    if not exps:
        return None
    ex = exps[0]
    T = (date.fromisoformat(ex) - d0).days / 365
    k0 = strike_for(S, T, iv or 0.35, rate(day), c["delta"])
    near = sorted((x for x in ch if x[1] == ex), key=lambda x: abs(x[2] - k0))[:3]
    best = None
    for occ, _, k, _ in near:
        h = _hist(tok, occ)
        row = h.get(day)
        if not row:
            continue
        credit = _px(row, "bid", c)
        v = row.get("implied_volatility")
        if not credit or credit < c["minPx"] or not v:
            continue
        dl = abs(put_delta(S, k, T, v, rate(day)))
        if best is None or abs(dl - c["delta"]) < abs(best[4] - c["delta"]):
            best = (occ, ex, k, round(credit, 3), round(dl, 3), v, h)
    if best and abs(best[4] - c["delta"]) <= 0.10:
        return best
    return None


def _coll(c, S, K, credit):
    if c["coll"] == "cash":
        return K * 100
    return (max(0.2 * S - max(0.0, S - K), 0.1 * K) + credit) * 100


def _tested(S, K, T, row, day, c):
    """The put is "tested": its delta (its own IV that day) >= rollDelta; without an IV, the price within 2% of the strike."""
    v = row.get("implied_volatility") if row else None
    if v:
        return abs(put_delta(S, K, T, v, rate(day))) >= c["rollDelta"]
    return S <= K * 1.02


def _roll(tok, sym, day, S, pos, cost, c):
    """Roll to a later expiry (nearest `dte` within dteMin-dteMax) for a net credit of at least rollMin a share. rollMode
    "out": the same strike; "down": a lower strike (the lowest of the 4 listed below that gives the credit); "both":
    the same strike or one of the 3 below (the lowest that gives the credit).
    (occ, expiry, strike, credit, IV, history) or None."""
    d0 = date.fromisoformat(day)
    ch = [x for x in _chain(tok, sym, day, c["dte"]) if x[1] > pos["exp"] and x[2] <= pos["K"]]
    exps = sorted({x[1] for x in ch if c["dteMin"] <= (date.fromisoformat(x[1]) - d0).days <= c["dteMax"]},
                  key=lambda e: abs((date.fromisoformat(e) - d0).days - c["dte"]))
    if not exps:
        return None
    ex = exps[0]
    ks = sorted((x for x in ch if x[1] == ex), key=lambda x: -x[2])
    if c["rollMode"] == "out":                          # in time only: the same strike
        ks = [x for x in ks if x[2] == pos["K"]]
    elif c["rollMode"] == "down":                       # down in price: a lower strike (the 4 listed below)
        ks = [x for x in ks if x[2] < pos["K"]][:4]
    else:                                               # either: the same strike or one of the 3 below
        ks = ks[:4]
    for occ, _, k, _ in sorted(ks, key=lambda x: x[2]):
        h = _hist(tok, occ)
        row = h.get(day)
        if not row:
            continue
        bid = _px(row, "bid", c)
        if bid is not None and bid - cost >= c["rollMin"]:
            return occ, ex, k, round(bid, 3), row.get("implied_volatility"), h
    return None


def sim_symbol(tok, sym, rows, c, cal):
    """Candidate trades of one stock: entry / exit (rolls included), the P&L of one contract and its daily marks
    (indexed on the calendar `cal` ({day: i}), from the day after entry) in the form the portfolio uses: first credit -
    mark = the position's value so far (net credit received - the open put's mid)."""
    out, pos = [], None
    close = {r[0]: r[1] for r in rows}
    for day, S, rk, iv in rows:
        if day not in cal or day < c["from"]:
            continue
        ci = cal[day]
        if pos:
            dte = (date.fromisoformat(pos["exp"]) - date.fromisoformat(day)).days
            row = pos["h"].get(day)
            why, px = None, None
            if dte <= 0:
                why, px = "expired", max(0.0, pos["K"] - (close.get(pos["exp"]) or S))
            elif row:
                ask = _px(row, "ask", c)
                m = _mid(row)
                if m is not None:
                    pos["last"] = m
                if ask is not None and ask <= pos["net"] * (1 - c["take"]):
                    why, px = "take", ask
                elif ask is not None and c["stop"] > 0 and ask >= pos["net"] * (1 + c["stop"]):
                    why, px = "stop", ask
                elif ask is not None and c["roll"] and dte <= c["rollDte"] and _tested(S, pos["K"], dte / 365, row, day, c):
                    r = _roll(tok, sym, day, S, pos, ask, c) if len(pos["legs"]) <= c["rollMax"] else None
                    if r:
                        occ, ex, k, bid, v, h = r
                        pos["legs"].append({"day": day, "occ": occ, "exp": ex, "K": k, "credit": bid, "debit": round(ask, 3)})
                        pos.update(occ=occ, exp=ex, K=k, h=h, net=round(pos["net"] - ask + bid, 3), fees=pos["fees"] + 2 * c["comm"],
                                   last=(_mid(h.get(day) or {}) or bid))
                    else:
                        why, px = ("tested: no credit roll" if len(pos["legs"]) <= c["rollMax"] else "tested: max rolls"), ask
                elif ask is not None and c["manage"] > 0 and dte <= c["manage"]:
                    why, px = "manage", ask
            while len(pos["marks"]) < ci - pos["i"] - 1:
                pos["marks"].append(pos["marks"][-1] if pos["marks"] else pos["credit"])
            adj = pos["credit"] - pos["net"]
            if why:
                fee = pos["fees"] + (c["comm"] if (why != "expired" or px > 0) else 0.0)
                pnl = (pos["net"] - px) * 100 - fee
                pos["marks"].append(px + adj)
                out.append({"sym": sym, "in": pos["in"], "out": day, "exp": pos["legs"][0]["exp"], "K": pos["legs"][0]["K"], "S": pos["S"],
                            "occ": pos["legs"][0]["occ"], "credit": pos["credit"], "net": pos["net"], "exitPx": round(px, 2), "why": why,
                            "pnl": round(pnl, 2), "rank": pos["rank"], "iv0": pos["iv0"], "delta": pos["delta"], "coll": pos["coll"],
                            "rolls": len(pos["legs"]) - 1, "legs": pos["legs"], "i": pos["i"], "j": ci, "marks": [round(x, 3) for x in pos["marks"]]})
                pos = None
            else:
                pos["marks"].append(pos["last"] + adj)
                continue
        if pos is None and rk >= c["rank"]:
            e = _entry(tok, sym, day, S, iv, c)
            if e:
                occ, ex, k, credit, dl, v, h = e
                pos = {"occ": occ, "in": day, "exp": ex, "K": k, "S": S, "credit": credit, "net": credit, "fees": c["comm"], "iv0": round(v, 4),
                       "rank": round(rk, 1), "delta": dl, "coll": round(_coll(c, S, k, credit), 2), "i": ci, "marks": [], "last": credit, "h": h,
                       "legs": [{"day": day, "occ": occ, "exp": ex, "K": k, "credit": credit, "debit": None}]}
    return out


# ---------------------------------------------------------------- portfolio and stats

def portfolio(cands, days, c):
    """Candidates -> accepted trades and the daily equity: the day's candidates in IV-rank order while there's room."""
    by_in = {}
    for t in cands:
        by_in.setdefault(t["i"], []).append(t)
    eq = c["acct"]
    open_, done, curve = [], [], []
    for i, day in enumerate(days):
        keep = []
        for t, n in open_:
            if t["j"] == i:
                eq += t["pnl"] * n
                done.append({**{k: v for k, v in t.items() if k not in ("marks", "i", "j")}, "n": n, "pnl": round(t["pnl"] * n, 2),
                             "ret": round(t["pnl"] / t["coll"], 5), "days": (date.fromisoformat(t["out"]) - date.fromisoformat(t["in"])).days})
            else:
                keep.append((t, n))
        open_ = keep
        held = {t["sym"] for t, _ in open_}
        for t in sorted(by_in.get(i, []), key=lambda t: -t["rank"]):
            if len(open_) >= c["maxPos"]:
                break
            if t["sym"] in held:
                continue
            n = int(max(0.0, eq) * c["alloc"] / c["maxPos"] // t["coll"])
            if n >= 1:
                open_.append((t, n))
                held.add(t["sym"])
        mtm = 0.0
        for t, n in open_:
            k = i - t["i"] - 1
            m = t["marks"][k] if 0 <= k < len(t["marks"]) else t["credit"]
            mtm += (t["credit"] - m) * 100 * n
        curve.append([day, round(eq + mtm, 2), len(open_)])
    return done, curve


def _stats(trades, curve, a=None, b=None):
    ts = [t for t in trades if (a is None or t["in"] >= a) and (b is None or t["in"] < b)]
    cv = [x for x in curve if (a is None or x[0] >= a) and (b is None or x[0] < b)]
    if not ts:
        return {"n": 0}
    pn = [t["pnl"] for t in ts]
    rs = [t["ret"] for t in ts]
    m = sum(rs) / len(rs)
    sd = math.sqrt(sum((x - m) ** 2 for x in rs) / (len(rs) - 1)) if len(rs) > 1 else 0
    out = {"n": len(ts), "win": round(sum(1 for x in pn if x > 0) / len(ts), 3), "pnl": round(sum(pn), 2), "avg": round(sum(pn) / len(ts), 2),
           "avgRet": round(m, 5), "t": round(m / (sd / math.sqrt(len(rs))), 2) if sd else None, "worst": round(min(pn), 2), "best": round(max(pn), 2),
           "days": round(sum(t["days"] for t in ts) / len(ts), 1), "why": {}}
    for t in ts:
        out["why"][t["why"]] = out["why"].get(t["why"], 0) + 1
    if len(cv) > 1 and cv[0][1] > 0:
        peak, mdd = cv[0][1], 0.0
        dr = []
        for k, (_, v, _) in enumerate(cv):
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
            if k and cv[k - 1][1] > 0:
                dr.append(v / cv[k - 1][1] - 1)
        tot = cv[-1][1] / cv[0][1] - 1
        yrs = max(0.25, (date.fromisoformat(cv[-1][0]) - date.fromisoformat(cv[0][0])).days / 365.25)
        mu = sum(dr) / len(dr) if dr else 0
        s2 = math.sqrt(sum((x - mu) ** 2 for x in dr) / (len(dr) - 1)) if len(dr) > 1 else 0
        out.update(total=round(tot, 4), cagr=round((1 + tot) ** (1 / yrs) - 1, 4) if tot > -1 else None, mdd=round(mdd, 4),
                   sharpe=round(mu / s2 * math.sqrt(252), 2) if s2 else None, avgOpen=round(sum(x[2] for x in cv) / len(cv), 1))
    return out


# ---------------------------------------------------------------- the backtest job

def run_job(sub, prog, deadline):
    """The backtest over every stock with IV history; partial candidates kept between invocations. True when done."""
    import uwhales
    tok = uwhales.token(sub)
    st = store.get_json(f"ivc/run/{sub}") or {}
    c = clean(st.get("cfg"))
    ix = _ixd()
    syms = [s for s in universe()["syms"] if (ix["s"].get(s) or {}).get("n")]
    if not syms:
        raise BadRequest("No IV history yet: download it first.")
    if not st.get("cal"):
        cal = set()
        for s in syms[:40]:
            cal |= {r[0] for r in store.get_json(f"ivc/uw/{s}") or []}
        st.update(cal=sorted(d for d in cal if d >= c["from"]), done=[], parts=0)
        store.put_json(f"ivc/run/{sub}", st)
    cal = st["cal"]
    ci = {d: i for i, d in enumerate(cal)}
    done = set(st["done"])
    part = {"f": [], "today": []}
    for s in syms:
        if s in done:
            continue
        if time.time() > deadline:
            break
        prog(f"Backtest {len(done) + 1} / {len(syms)}: {s} (option prices from Unusual Whales, cached)")
        rows = store.get_json(f"ivc/uw/{s}") or []
        _MEMO.clear()
        try:
            part["f"] += sim_symbol(tok, s, rows, c, ci)
        except BadRequest as e:                         # a ticker without option data: go on
            print("ivcrush sim", s, e)
        if rows:
            part["today"].append({"sym": s, "day": rows[-1][0], "S": rows[-1][1], "rank": rows[-1][2], "iv": rows[-1][3]})
        done.add(s)
    store.put_json(f"ivc/run/{sub}/p{st['parts']}", part)
    st.update(done=sorted(done), parts=st["parts"] + 1)
    store.put_json(f"ivc/run/{sub}", st)
    if len(done) < len(syms):
        return False
    prog("Portfolio…")
    f, today = [], []
    for k in range(st["parts"]):
        p = store.get_json(f"ivc/run/{sub}/p{k}") or {}
        f += p.get("f") or []
        today += p.get("today") or []
        store.delete(f"ivc/run/{sub}/p{k}")
    store.put_json(f"ivc/res/{sub}", _result(c, cal, f, today, len(syms)))
    store.put_json(f"ivc/run/{sub}", {"cfg": c})
    return True


def _result(c, cal, f, today, nsym):
    trades, curve = portfolio(f, cal, c)
    split = cal[int(len(cal) * c["split"])] if cal else None
    buckets = []
    edges = sorted({c["rank"], 50.0, 70.0, 90.0, 100.01})
    for lo, hi in zip(edges, edges[1:]):
        if lo < c["rank"]:
            continue
        ts = [dict(t, ret=t["pnl"] / t["coll"], days=(date.fromisoformat(t["out"]) - date.fromisoformat(t["in"])).days) for t in f if lo <= t["rank"] < hi]
        buckets.append({"range": f"{lo:g}–{min(hi, 100):g}", **_stats(ts, [])})
    bysym = {}
    for t in trades:
        g = bysym.setdefault(t["sym"], {"sym": t["sym"], "n": 0, "pnl": 0.0, "win": 0})
        g["n"] += 1
        g["pnl"] += t["pnl"]
        g["win"] += t["pnl"] > 0
    syms = sorted(bysym.values(), key=lambda g: g["pnl"])
    for g in syms:
        g["pnl"] = round(g["pnl"], 2)
    step = max(1, len(curve) // 1200)
    hot = sorted([x for x in today if x["rank"] >= c["rank"]], key=lambda x: -x["rank"])
    return {"cfg": c, "at": iso(now_ny()), "syms": nsym, "cands": len(f), "from": cal[0] if cal else None, "to": cal[-1] if cal else None, "split": split,
            "all": _stats(trades, curve), "train": _stats(trades, curve, None, split), "test": _stats(trades, curve, split),
            "byRank": buckets, "worstSyms": syms[:15], "bestSyms": syms[-15:][::-1],
            "curve": [x[:2] for x in curve[::step]], "trades": trades[-20000:], "nTrades": len(trades),
            "today": hot[:80], "todayN": len(hot)}


# ---------------------------------------------------------------- the optimizer

DTE_WIN = {30: (21, 40), 45: (35, 60)}


def grid(base, wide=False):
    """The settings tried: IV rank x take profit x DTE exit x stop at the current delta / DTE; `wide`: fewer of those, but
    delta x DTE too (more option data to download)."""
    import itertools
    if wide:
        g = {"rank": [30, 50, 70], "take": [0.5, 0.75], "manage": [0, 21], "stop": [0, 2], "delta": [0.15, 0.2, 0.25, 0.3], "dte": [30, 45]}
    else:
        g = {"rank": sorted({30, 40, 50, 60, 70, base["rank"]}), "take": [0.25, 0.5, 0.75], "manage": [0, 14, 21], "stop": [0, 1, 2], "roll": [0, 1]}
    out = []
    for v in itertools.product(*g.values()):
        p = dict(zip(g, v))
        if "dte" in p:
            p["dteMin"], p["dteMax"] = DTE_WIN[p["dte"]]
        out.append(p)
    return out


def _light(cands, n, c):
    """The portfolio on closed trades only (fast): (accepted [(in, pnl, ret)], equity per day)."""
    by_in = {}
    for t in cands:
        by_in.setdefault(t[0], []).append(t)
    eq, open_, done, curve = c["acct"], [], [], []
    for i in range(n):
        keep = []
        for t, k in open_:
            if t[1] == i:
                eq += t[2] * k
                done.append((t[0], t[2] * k, t[2] / t[3]))
            else:
                keep.append((t, k))
        open_ = keep
        held = {t[5] for t, _ in open_}
        for t in sorted(by_in.get(i, []), key=lambda t: -t[4]):
            if len(open_) >= c["maxPos"]:
                break
            if t[5] in held:
                continue
            k = int(max(0.0, eq) * c["alloc"] / c["maxPos"] // t[3])
            if k >= 1:
                open_.append((t, k))
                held.add(t[5])
        curve.append(eq)
    return done, curve


def _ostats(done, curve, a, b, ndays):
    ts = [x for x in done if a <= x[0] < b]
    cv = curve[a:b]
    if len(cv) < 2 or not ts:
        return {"n": len(ts)}
    peak, mdd, dr = cv[0], 0.0, []
    for k, v in enumerate(cv):
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1 if peak > 0 else -1)
        if k and cv[k - 1] > 0:
            dr.append(v / cv[k - 1] - 1)
    mu = sum(dr) / len(dr)
    sd = math.sqrt(sum((x - mu) ** 2 for x in dr) / (len(dr) - 1)) if len(dr) > 1 else 0
    tot = cv[-1] / cv[0] - 1
    return {"n": len(ts), "win": round(sum(1 for x in ts if x[1] > 0) / len(ts), 3), "total": round(tot, 4),
            "cagr": round((1 + tot) ** (252 / len(cv)) - 1, 4) if tot > -1 else None, "mdd": round(mdd, 4),
            "sharpe": round(mu / sd * math.sqrt(252), 2) if sd else None, "avgRet": round(sum(x[2] for x in ts) / len(ts), 5)}


def opt_job(sub, prog, deadline):
    """Every setting of the grid on every stock (each stock's option data downloaded once, then reused for all the
    settings), then the portfolio of each; ranked on the train period only. True when done."""
    import uwhales
    tok = uwhales.token(sub)
    st = store.get_json(f"ivc/opt/{sub}") or {}
    base = clean(st.get("cfg"))
    g = grid(base, st.get("wide"))
    ix = _ixd()
    syms = [s for s in universe()["syms"] if (ix["s"].get(s) or {}).get("n")]
    if not syms:
        raise BadRequest("No IV history yet: download it first.")
    if not st.get("cal"):
        cal = set()
        for s in syms[:40]:
            cal |= {r[0] for r in store.get_json(f"ivc/uw/{s}") or []}
        st.update(cal=sorted(d for d in cal if d >= base["from"]), done=[], parts=0, res=None)
        store.put_json(f"ivc/opt/{sub}", st)
    cal = st["cal"]
    ci = {d: i for i, d in enumerate(cal)}
    done = set(st["done"])
    part = [[] for _ in g]
    for s in syms:
        if s in done:
            continue
        if time.time() > deadline:
            break
        prog(f"Optimizer {len(done) + 1} / {len(syms)}: {s} · {len(g)} settings")
        rows = store.get_json(f"ivc/uw/{s}") or []
        _MEMO.clear()
        for k, p in enumerate(g):
            try:
                ts = sim_symbol(tok, s, rows, {**base, **p}, ci)
            except BadRequest as e:
                print("ivcrush opt", s, e)
                break
            part[k] += [[t["i"], t["j"], t["pnl"], t["coll"], t["rank"], s] for t in ts]
        done.add(s)
    _MEMO.clear()
    store.put_json(f"ivc/opt/{sub}/p{st['parts']}", part)
    st.update(done=sorted(done), parts=st["parts"] + 1)
    store.put_json(f"ivc/opt/{sub}", st)
    if len(done) < len(syms):
        return False
    prog("Optimizer: portfolios…")
    allc = [[] for _ in g]
    for k in range(st["parts"]):
        for i, x in enumerate(store.get_json(f"ivc/opt/{sub}/p{k}") or []):
            allc[i] += x
        store.delete(f"ivc/opt/{sub}/p{k}")
    n = len(cal)
    sp = int(n * base["split"])
    res = []
    for p, cands in zip(g, allc):
        dn, cv = _light(cands, n, {**base, **p})
        res.append({"p": p, "train": _ostats(dn, cv, 0, sp, n), "test": _ostats(dn, cv, sp, n, n), "all": _ostats(dn, cv, 0, n, n)})
    res.sort(key=lambda x: x["train"].get("sharpe") or -99, reverse=True)
    store.put_json(f"ivc/opt/{sub}", {"cfg": base, "wide": st.get("wide"), "res": res, "split": cal[sp] if cal else None, "syms": len(syms),
                                      "at": iso(now_ny()), "total": len(g)})
    return True


def opt_start(sub, cfg, wide=False):
    import jobs
    import uwhales
    uwhales.token(sub)
    if not status()["have"]:
        raise BadRequest("No IV history yet: download it first.")
    store.put_json(f"ivc/opt/{sub}", {"cfg": clean(cfg), "wide": bool(wide)})
    return jobs.start(sub, "ivcrush", "ivcopt", {})


# ---------------------------------------------------------------- API

def view(sub):
    import jobs
    st = store.get_json(f"ivc/run/{sub}") or {}
    j = jobs.status(sub, "ivcrush")
    if j.get("status") == "running" and j.get("at", "") < iso(now_ny() - timedelta(minutes=20)):
        j["stale"] = True
    o = store.get_json(f"ivc/opt/{sub}") or {}
    opt = {k: o.get(k) for k in ("wide", "split", "syms", "at", "total")} if o else None
    if opt is not None:
        opt["top"] = (o.get("res") or [])[:25]
        opt["running"] = o.get("res") is None
        opt["cfg"] = o.get("cfg")
    return {"cfg": clean(st.get("cfg")), "res": store.get_json(f"ivc/res/{sub}"), "data": status(), "job": j, "opt": opt}


def data_start(sub):
    import jobs
    import uwhales
    uwhales.token(sub)                                  # connected?
    return jobs.start(sub, "ivcrush", "ivcdata", {})


def run_start(sub, cfg):
    import jobs
    import uwhales
    uwhales.token(sub)
    c = clean(cfg)
    if not status()["have"]:
        raise BadRequest("No IV history yet: download it first.")
    store.put_json(f"ivc/run/{sub}", {"cfg": c})
    return jobs.start(sub, "ivcrush", "ivcrun", {})
