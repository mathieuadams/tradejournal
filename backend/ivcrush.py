"""IV crush on the S&P 500: sell out-of-the-money puts when a stock's implied volatility is high (IV rank), buy them back
at 50% of the credit.

Universe: the current S&P 500 (FMP sp500-constituent), saved when the data is downloaded.

Data: Databento OPRA.PILLAR daily bars (ohlcv-1d: the day's trades in each option) for every option of each stock
(stype parent "AAPL.OPT"), 10 stocks and one year per request, the cost checked first against an amount the user
accepts. The stocks' prices: Alpaca daily closes, unadjusted (strikes are in unadjusted dollars). Kept per stock and day:
  - the close,
  - an IV index: the median implied volatility of the out-of-the-money options within 3% of the money, 20-60 days to
    expiry, that traded that day,
  - the puts of the standard monthly expiries (third Friday; Saturday before 2015) up to 75 days out, strikes 70-105% of
    the price, that traded that day: [expiry, strike, close, volume, IV].
IV from Black-Scholes on the option's daily close (the year's average 3-month T-bill rate, no dividends).

Rules (per stock, one put at a time on a stock):
  - IV rank = today's IV index within its last `look` sessions: (IV - low) / (high - low) x 100 (none before 120 values).
  - Entry: IV rank >= `rank` → sell the monthly put nearest `dte` days (within dteMin-dteMax) whose delta is nearest
    `delta` (within 0.10), price >= minPx, at its close - slip.
  - Exit: its close <= (1 - take) x the credit → buy back at close + slip; loss >= stop x the credit (0 = off); at
    `manage` days to expiry (0 = hold); else it expires (strike - price, or 0). A day the put didn't trade: no
    profit / stop check; the manage exit and the marks use Black-Scholes with the put's last IV.
  - Commission `comm` a contract each side (none when it expires worthless).
Portfolio: at most `maxPos` puts open; the day's candidates taken in IV-rank order while there's room. Each position:
`n` contracts = (equity x alloc / maxPos) / collateral (cash-secured: strike x 100; margin: Reg T ≈ max(20% of the price
- the OTM amount, 10% of the strike) x 100); fewer than 1: skipped. Equity marked to market every day. The same rules
without the IV-rank filter are the baseline."""
import csv
import io
import json
import math
import time
from datetime import date, timedelta

import store
from util import BadRequest, iso, now_ny

DATASET = "OPRA.PILLAR"
GROUP = 10
DEFAULT = {"rank": 30.0, "look": 252, "dte": 45, "dteMin": 30, "dteMax": 60, "delta": 0.20, "take": 0.50, "manage": 21, "stop": 0.0,
           "maxPos": 20, "minPx": 0.20, "slip": 0.03, "comm": 0.65, "acct": 500000.0, "alloc": 1.0, "coll": "cash", "split": 0.6, "from": 2016}
RATES = {2013: .0006, 2014: .0003, 2015: .0005, 2016: .0032, 2017: .0093, 2018: .0194, 2019: .0206, 2020: .0037, 2021: .0005,
         2022: .0202, 2023: .0507, 2024: .0497, 2025: .0420, 2026: .0370, 2027: .0350}


def clean(cfg):
    c = {**DEFAULT, **{k: v for k, v in (cfg or {}).items() if k in DEFAULT}}
    for k, lo, hi in (("rank", 0, 100), ("look", 60, 504), ("dte", 7, 75), ("dteMin", 5, 75), ("dteMax", 7, 75), ("delta", 0.03, 0.5),
                      ("take", 0.1, 0.95), ("manage", 0, 60), ("stop", 0, 10), ("maxPos", 1, 200), ("minPx", 0.01, 5), ("slip", 0, 1),
                      ("comm", 0, 5), ("acct", 1000, 1e9), ("alloc", 0.05, 5), ("split", 0.3, 0.9), ("from", 2013, 2026)):
        try:
            c[k] = max(lo, min(hi, float(c[k])))
        except (TypeError, ValueError):
            c[k] = DEFAULT[k]
    for k in ("look", "dte", "dteMin", "dteMax", "manage", "maxPos", "from"):
        c[k] = int(c[k])
    c["coll"] = "margin" if c["coll"] == "margin" else "cash"
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


def implied_vol(price, S, K, T, r, call):
    """Bisection between 1% and 500%; None when no volatility gives the price."""
    if price <= 0 or T <= 0 or S <= 0:
        return None
    if price < bs(S, K, T, 0.01, r, call) - 1e-9 or price > bs(S, K, T, 5.0, r, call):
        return None
    lo, hi = 0.01, 5.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if bs(S, K, T, mid, r, call) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def put_delta(S, K, T, v, r):
    if T <= 0 or v <= 0:
        return -1.0 if K > S else 0.0
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / (v * math.sqrt(T))
    return _ncdf(d1) - 1


def rate(day):
    return RATES.get(int(day[:4]), 0.03)


# ---------------------------------------------------------------- contracts

def parse_occ(sym):
    """'AAPL  250117P00200000' -> ('AAPL', '2025-01-17', 'P', 200.0); None when not an option symbol."""
    s = (sym or "").strip()
    if len(s) < 16:
        return None
    t = s[-15:]
    if t[6] not in "CP" or not t[:6].isdigit() or not t[7:].isdigit():
        return None
    return s[:-15].strip(), f"20{t[0:2]}-{t[2:4]}-{t[4:6]}", t[6], int(t[7:]) / 1000.0


_MONTHLY = {}


def is_monthly(exp):
    """The standard monthly expiry: the third Friday (the Thursday before when that Friday is a market holiday; the
    Saturday after, the old convention, before 2015)."""
    v = _MONTHLY.get(exp)
    if v is None:
        import holistudy
        d = date.fromisoformat(exp)
        wd = d.weekday()
        v = (wd == 4 and 15 <= d.day <= 21) or (wd == 5 and 16 <= d.day <= 22) or \
            (wd == 3 and 14 <= d.day <= 20 and holistudy.holiday_name(d + timedelta(days=1)) == "Good Friday")
        _MONTHLY[exp] = v
    return v


def root(sym):
    """The option root of a stock: BRK.B / BRK-B -> BRKB."""
    return "".join(ch for ch in sym.upper() if ch.isalnum())


# ---------------------------------------------------------------- the universe and the data index

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


def _index():
    return store.get_json("ivc/have") or {"y": {}, "packed": {}}


def status():
    ix, u = _index(), universe()
    syms = u["syms"]
    years = sorted({int(y) for s in syms for y in (ix["y"].get(s) or {})})
    full = sum(1 for s in syms if s in ix["packed"])
    return {"syms": len(syms), "uniAt": u["at"], "years": [years[0], years[-1]] if years else None, "packed": full,
            "at": ix.get("at")}


def _db_key(sub):
    import databento
    k = databento.key(sub)
    if DATASET not in databento.list_datasets(k):
        raise BadRequest(f"Your Databento key has no access to {DATASET} (US equity options). Add it in the Databento portal.")
    return k


def _todo(syms, y0):
    """(symbol, year) pairs not downloaded; the current year is always fetched again (new days)."""
    ix = _index()
    y1 = now_ny().year
    return [(s, y) for y in range(y0, y1 + 1) for s in syms if y == y1 or str(y) not in (ix["y"].get(s) or {})]


def _yesterday():
    return (now_ny() - timedelta(days=1)).strftime("%Y-%m-%d")


def cost(sub, y0):
    """US dollars Databento would bill for what is not downloaded yet: one estimate per year."""
    import databento
    k = _db_key(sub)
    syms = universe(sub)["syms"]
    todo = _todo(syms, int(y0))
    by = {}
    for s, y in todo:
        by.setdefault(y, []).append(s)
    total = 0.0
    for y, ss in sorted(by.items()):
        a, b = f"{y}-01-01", min(f"{y + 1}-01-01", _yesterday())
        if b <= a:
            continue
        r = databento._retry_end(lambda e: databento._json(k, "metadata.get_cost", {"dataset": DATASET, "symbols": ",".join(root(s) + ".OPT" for s in ss),
                                                                                    "schema": "ohlcv-1d", "start": a, "end": e, "stype_in": "parent"}), b + "T00:00:00")
        try:
            total += float(r)
        except (TypeError, ValueError):
            total += float((r or {}).get("cost") or 0) if isinstance(r, dict) else 0.0
    return {"syms": len(syms), "pairs": len(todo), "from": int(y0), "cost": round(total, 2)}


# ---------------------------------------------------------------- download

def _closes(sub, sym, y0):
    """{date: close} unadjusted (Alpaca), cached; fetched again when older than 12 hours."""
    import alpaca
    key = f"ivc/c/{root(sym)}"
    c = store.get_json(key, max_age=12 * 3600)
    if c and c.get("from", 9999) <= y0:
        return c["d"]
    try:
        rows = alpaca.daily_range(sub, sym, f"{y0 - 1}-12-01", adjustment="raw")
    except Exception as e:                              # a ticker Alpaca doesn't know
        print("ivcrush closes", sym, e)
        rows = []
    d = {r[0]: float(r[4]) for r in rows}
    store.put_json(key, {"from": y0, "d": d, "at": iso(now_ny())})
    return d


def _fetch(key, syms, y, closes):
    """One year of OPRA daily bars of `syms` -> {sym: [[day, S, iv, puts], ...]}."""
    import databento
    a, b = f"{y}-01-01", min(f"{y + 1}-01-01", _yesterday())
    if b <= a:
        return {}
    roots = {root(s): s for s in syms}
    params = {"dataset": DATASET, "symbols": ",".join(r + ".OPT" for r in roots), "schema": "ohlcv-1d", "start": a, "stype_in": "parent",
              "encoding": "csv", "compression": "none", "pretty_px": "true", "pretty_ts": "true", "map_symbols": "true"}
    resp = databento._retry_end(lambda e: databento._open(key, "POST", "timeseries.get_range", {**params, "end": e}, timeout=900), b + "T00:00:00")
    acc = {}                                            # (sym, day) -> [ivs, puts]
    try:
        rd = csv.reader(io.TextIOWrapper(resp, encoding="utf-8", newline=""))
        head = next(rd, None) or []
        try:
            i_ts, i_c, i_v, i_s = head.index("ts_event"), head.index("close"), head.index("volume"), head.index("symbol")
        except ValueError:
            return {}
        for x in rd:
            p = parse_occ(x[i_s])
            if not p or p[0] not in roots:
                continue
            sym = roots[p[0]]
            _, exp, cp, k = p
            day = x[i_ts][:10]
            S = closes[sym].get(day)
            if not S:
                continue
            dte = (date.fromisoformat(exp) - date.fromisoformat(day)).days
            if dte < 0 or dte > 75:
                continue
            m = k / S
            if m < 0.70 or m > 1.05:
                continue
            try:
                px, vol = float(x[i_c]), int(float(x[i_v]))
            except ValueError:
                continue
            if px <= 0:
                continue
            T = max(dte, 0.5) / 365
            ivs_puts = acc.get((sym, day))
            if ivs_puts is None:
                ivs_puts = acc[(sym, day)] = [[], []]
            if 20 <= dte <= 60 and ((cp == "P" and 0.97 <= m <= 1.0) or (cp == "C" and 1.0 <= m <= 1.03)):
                iv = implied_vol(px, S, k, T, rate(day), cp == "C")
                if iv:
                    ivs_puts[0].append(iv)
            if cp == "P" and is_monthly(exp):
                iv = implied_vol(px, S, k, T, rate(day), False)
                ivs_puts[1].append([exp, k, px, vol, round(iv, 4) if iv else None])
    finally:
        resp.close()
    out = {}
    for (sym, day), (ivs, puts) in acc.items():
        ivs.sort()
        out.setdefault(sym, []).append([day, closes[sym][day], round(ivs[len(ivs) // 2], 4) if ivs else None, puts])
    for v in out.values():
        v.sort(key=lambda r: r[0])
    return out


def download(sub, p, prog, deadline):
    """The job: the universe, then every missing (stock, year), then one file per stock. Returns the params to carry on
    with (out of time), or None when done."""
    y0 = int(p.get("from") or DEFAULT["from"])
    k = _db_key(sub)
    syms = universe(sub, refresh=not p.get("uni"))["syms"]
    p = {**p, "uni": True}
    done = set(p.get("done") or [])
    todo = [(s, y) for s, y in _todo(syms, y0) if f"{s}:{y}" not in done]
    years = sorted({y for _, y in todo})
    for y in years:
        ss = [s for s, yy in todo if yy == y]
        for i in range(0, len(ss), GROUP):
            if time.time() > deadline - 240:
                return {**p, "done": sorted(done)}
            grp = ss[i:i + GROUP]
            prog(f"Options {y}: {grp[0]}…{grp[-1]} ({len(done) + 1}–{len(done) + len(grp)} of {len(done) + len(todo) - len([1 for s, yy in todo if f'{s}:{yy}' in done])})")
            closes = {s: _closes(sub, s, y0) for s in grp}
            try:
                got = _fetch(k, [s for s in grp if closes[s]], y, closes)
            except BadRequest as e:                     # one bad symbol spoils a request: one at a time
                print("ivcrush group", y, grp, e)
                got = {}
                for s in grp:
                    try:
                        got.update(_fetch(k, [s], y, closes) if closes[s] else {})
                    except BadRequest as e2:
                        print("ivcrush sym", y, s, e2)
            ix = _index()
            for s in grp:
                rows = got.get(s) or []
                store.put_json(f"ivc/y/{root(s)}/{y}", rows)
                ix["y"].setdefault(s, {})[str(y)] = len(rows)
                ix["packed"].pop(s, None)
                done.add(f"{s}:{y}")
            ix["at"] = iso(now_ny())
            store.put_json("ivc/have", ix)
    # one file per stock (what the backtest reads)
    ix = _index()
    for s in syms:
        if s in ix["packed"]:
            continue
        if time.time() > deadline - 60:
            store.put_json("ivc/have", ix)
            return {**p, "done": sorted(done)}
        prog(f"Packing {s}…")
        rows = []
        for y in sorted(ix["y"].get(s) or {}):
            rows += store.get_json(f"ivc/y/{root(s)}/{y}") or []
        store.put_json(f"ivc/s/{root(s)}", rows)
        ix["packed"][s] = len(rows)
    store.put_json("ivc/have", ix)
    return None


# ---------------------------------------------------------------- the backtest

def ranks(iv, look):
    """IV rank (0-100) of each day within its last `look` values (today included); None until 120 values."""
    out = []
    for i in range(len(iv)):
        if iv[i] is None:
            out.append(None)
            continue
        w = [x for x in iv[max(0, i - look + 1):i + 1] if x is not None]
        if len(w) < min(120, look):
            out.append(None)
            continue
        lo, hi = min(w), max(w)
        out.append(100 * (iv[i] - lo) / (hi - lo) if hi > lo else 50.0)
    return out


def _pick(day, S, puts, c):
    """The put to sell: the monthly expiry nearest `dte` days (dteMin-dteMax), the strike whose delta is nearest."""
    d0 = date.fromisoformat(day)
    exps = {}
    for x in puts:
        if x[4] is None or x[2] < c["minPx"] or x[1] >= S:
            continue
        dte = (date.fromisoformat(x[0]) - d0).days
        if c["dteMin"] <= dte <= c["dteMax"]:
            exps.setdefault(x[0], []).append(x)
    if not exps:
        return None
    e = min(exps, key=lambda e: abs((date.fromisoformat(e) - d0).days - c["dte"]))
    T = (date.fromisoformat(e) - d0).days / 365
    best = None
    for x in exps[e]:
        dl = abs(put_delta(S, x[1], T, x[4], rate(day)))
        if best is None or abs(dl - c["delta"]) < abs(best[1] - c["delta"]):
            best = (x, dl)
    return best if best and abs(best[1] - c["delta"]) <= 0.10 else None


def _coll(c, S, K, credit):
    if c["coll"] == "cash":
        return K * 100
    return (max(0.2 * S - max(0.0, S - K), 0.1 * K) + credit) * 100


def sim_symbol(sym, rows, c, cal):
    """Candidate trades of one stock, with and without the IV-rank filter: [filtered, baseline]. Each: entry / exit,
    the P&L of one contract, its daily marks (one contract's value to buy back) from the day after entry, indexed on
    the calendar `cal` ({day: i})."""
    rk = ranks([r[2] for r in rows], c["look"])
    out = [[], []]
    for which in (0, 1):
        pos = None
        for i, (day, S, iv, puts) in enumerate(rows):
            if day not in cal:
                continue
            if pos:
                dte = (date.fromisoformat(pos["exp"]) - date.fromisoformat(day)).days
                row = next((x for x in puts if x[0] == pos["exp"] and x[1] == pos["K"]), None)
                if row and row[4]:
                    pos["iv"] = row[4]
                est = row[2] if row else bs(S, pos["K"], max(dte, 0) / 365, pos["iv"] or iv or 0.3, rate(day), False)
                why, px = None, None
                if dte <= 0:
                    why, px = "expired", max(0.0, pos["K"] - S)
                elif row and row[2] <= pos["credit"] * (1 - c["take"]):
                    why, px = "take", row[2] + c["slip"]
                elif row and c["stop"] > 0 and row[2] >= pos["credit"] * (1 + c["stop"]):
                    why, px = "stop", row[2] + c["slip"]
                elif c["manage"] > 0 and dte <= c["manage"]:
                    why, px = "manage", est + c["slip"]
                ci = cal[day]
                while len(pos["marks"]) < ci - pos["i"] - 1:        # days the stock had no row: carry the last mark
                    pos["marks"].append(pos["marks"][-1] if pos["marks"] else pos["credit"])
                if why:
                    fee = c["comm"] * (2 if (why != "expired" or px > 0) else 1)
                    pnl = (pos["credit"] - px) * 100 - fee
                    pos["marks"].append(px)
                    out[which].append({"sym": sym, "in": pos["in"], "out": day, "exp": pos["exp"], "K": pos["K"], "S": pos["S"], "credit": pos["credit"],
                                       "exitPx": round(px, 2), "why": why, "pnl": round(pnl, 2), "rank": pos["rank"], "iv0": pos["iv0"], "delta": pos["delta"],
                                       "coll": pos["coll"], "i": pos["i"], "j": ci, "marks": [round(m, 3) for m in pos["marks"]]})
                    pos = None
                else:
                    pos["marks"].append(est)
                    continue
            if pos is None and rk[i] is not None and (which == 1 or rk[i] >= c["rank"]):
                pk = _pick(day, S, puts, c)
                if pk:
                    x, dl = pk
                    credit = max(0.01, x[2] - c["slip"])
                    pos = {"in": day, "exp": x[0], "K": x[1], "S": S, "credit": round(credit, 3), "iv": x[4], "iv0": x[4], "rank": round(rk[i], 1),
                           "delta": round(dl, 3), "coll": round(_coll(c, S, x[1], credit), 2), "i": cal[day], "marks": []}
    today = None
    if rows and rk[-1] is not None:
        day, S, iv, puts = rows[-1]
        pk = _pick(day, S, puts, c)
        today = {"sym": sym, "day": day, "S": S, "iv": iv, "rank": round(rk[-1], 1),
                 "put": {"exp": pk[0][0], "K": pk[0][1], "px": pk[0][2], "iv": pk[0][4], "delta": round(pk[1], 3)} if pk else None}
    return out, today


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


def run_job(sub, prog, deadline):
    """The backtest over every packed stock; partial candidates kept between invocations. True when done."""
    st = store.get_json(f"ivc/run/{sub}") or {}
    c = clean(st.get("cfg"))
    ix = _index()
    syms = [s for s in universe()["syms"] if s in ix["packed"]]
    if not syms:
        raise BadRequest("No option data yet: download it first.")
    import alpaca
    if not st.get("cal"):
        cal = [r[0] for r in alpaca.daily_range(sub, "SPY", f"{c['from']}-01-01", adjustment="raw")]
        st.update(cal=cal, done=[], parts=0)
        store.put_json(f"ivc/run/{sub}", st)
    cal = st["cal"]
    ci = {d: i for i, d in enumerate(cal)}
    done = set(st["done"])
    part = {"f": [], "b": [], "today": []}
    for k, s in enumerate(syms):
        if s in done:
            continue
        if time.time() > deadline:
            break
        prog(f"Backtest {len(done) + 1} / {len(syms)}: {s}")
        rows = [r for r in (store.get_json(f"ivc/s/{root(s)}") or []) if r[0] >= cal[0]]
        (f, b), today = sim_symbol(s, rows, c, ci)
        part["f"] += f
        part["b"] += b
        if today:
            part["today"].append(today)
        done.add(s)
    store.put_json(f"ivc/run/{sub}/p{st['parts']}", part)
    st.update(done=sorted(done), parts=st["parts"] + 1)
    store.put_json(f"ivc/run/{sub}", st)
    if len(done) < len(syms):
        return False
    prog("Portfolio…")
    f, b, today = [], [], []
    for k in range(st["parts"]):
        p = store.get_json(f"ivc/run/{sub}/p{k}") or {}
        f += p.get("f") or []
        b += p.get("b") or []
        today += p.get("today") or []
        store.delete(f"ivc/run/{sub}/p{k}")
    res = _result(c, cal, f, b, today, len(syms))
    store.put_json(f"ivc/res/{sub}", res)
    store.put_json(f"ivc/run/{sub}", {"cfg": c})
    return True


def _result(c, cal, f, b, today, nsym):
    first = min((t["in"] for t in b), default=cal[0])
    days = [d for d in cal if d >= first]
    i0 = cal.index(days[0]) if days else 0
    shift = lambda ts: [{**t, "i": t["i"] - i0, "j": t["j"] - i0} for t in ts if t["i"] >= i0]
    trades, curve = portfolio(shift(f), days, c)
    btr, bcv = portfolio(shift(b), days, c)
    split = days[int(len(days) * c["split"])] if days else None
    buckets = []
    for lo, hi in ((0, 20), (20, 30), (30, 50), (50, 70), (70, 101)):
        ts = [dict(t, ret=t["pnl"] / t["coll"], days=(date.fromisoformat(t["out"]) - date.fromisoformat(t["in"])).days) for t in b if lo <= t["rank"] < hi]
        buckets.append({"range": f"{lo}–{min(hi, 100)}", **_stats(ts, [])})
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
    today.sort(key=lambda x: -x["rank"])
    return {"cfg": c, "at": iso(now_ny()), "syms": nsym, "from": days[0] if days else None, "to": days[-1] if days else None, "split": split,
            "all": _stats(trades, curve), "train": _stats(trades, curve, None, split), "test": _stats(trades, curve, split),
            "base": {"all": _stats(btr, bcv), "train": _stats(btr, bcv, None, split), "test": _stats(btr, bcv, split)},
            "byRank": buckets, "worstSyms": syms[:15], "bestSyms": syms[-15:][::-1],
            "curve": [x[:2] for x in curve[::step]], "baseCurve": [x[:2] for x in bcv[::step]],
            "trades": trades[-20000:], "nTrades": len(trades),
            "today": [x for x in today if x["rank"] >= c["rank"]][:60], "todayN": sum(1 for x in today if x["rank"] >= c["rank"])}


# ---------------------------------------------------------------- API

def view(sub):
    import jobs
    st = store.get_json(f"ivc/run/{sub}") or {}
    j = jobs.status(sub, "ivcrush")
    if j.get("status") == "running" and j.get("at", "") < iso(now_ny() - timedelta(minutes=20)):
        j["stale"] = True
    return {"cfg": clean(st.get("cfg")), "res": store.get_json(f"ivc/res/{sub}"), "data": status(), "job": j}


def data_start(sub, y0, max_cost):
    import jobs
    y0 = clean({"from": y0})["from"]
    est = cost(sub, y0)
    if est["cost"] > float(max_cost or 0) + 0.005:
        raise BadRequest(f"Databento would bill about ${est['cost']:,.2f} for the S&P 500 options ({est['pairs']} stock-years from {y0}), "
                         f"more than the ${float(max_cost or 0):,.2f} you accepted.")
    return jobs.start(sub, "ivcrush", "ivcdata", {"from": y0})


def run_start(sub, cfg):
    import jobs
    c = clean(cfg)
    if not _index()["packed"]:
        raise BadRequest("No option data yet: check the cost and download it first.")
    store.put_json(f"ivc/run/{sub}", {"cfg": c})
    return jobs.start(sub, "ivcrush", "ivcrun", {})
