"""Option test for the Strategy lab: the stock trades of the last lab test, each one traded with a real call contract.

For every trade: a call expiring in the DTE window, strike nearest to entry price x (1 + moneyness %), picked among the
contracts that traded on the entry day (Alpaca daily option bars). Its 30-minute bars (Alpaca, history from Feb 2024)
price the entry at the close of the 30-minute bar that ends with the stock entry candle, then the trade is followed
bar by bar: option take profit / option stop (intrabar, the stop first when both are touched in the same bar), the stock
signal's own exit (optional), and the day before expiration. Slippage is taken on both sides.
A grid of take profit x stop is computed on the same option prices.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import db
from util import iso, now_ny, utc_to_ny

OPT_SK = "WWOPTTEST"
URL = "https://data.alpaca.markets/v1beta1/options/bars"
TPS = (None, 25, 50, 75, 100, 150, 200)
SLS = (None, 30, 50, 70)


def occ(sym, exp, strike):
    root = sym.replace(".", "").replace("-", "").upper()
    return f"{root}{exp[2:4]}{exp[5:7]}{exp[8:10]}C{int(round(strike * 1000)):08d}"


def fridays(day, dmin, dmax):
    d0 = datetime.strptime(day, "%Y-%m-%d").date()
    out = []
    for k in range(dmin, dmax + 1):
        d = d0 + timedelta(days=k)
        if d.weekday() == 4:
            out.append(d.isoformat())
    return out


def strikes_near(k):
    incs = (0.5, 1) if k < 25 else (1, 2.5, 5) if k < 200 else (2.5, 5, 10) if k < 600 else (5, 10, 25)
    out = set()
    for inc in incs:
        base = round(k / inc) * inc
        for j in (-1, 0, 1):
            v = round(base + j * inc, 2)
            if v > 0:
                out.add(v)
    return sorted(out, key=lambda x: abs(x - k))


def _get(c, q):
    req = urllib.request.Request(f"{URL}?{urllib.parse.urlencode(q)}",
                                 headers={"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"]})
    for n in range(4):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and n < 3:                 # rate limit: back off
                time.sleep(3 * (n + 1))
                continue
            raise


def bars(c, symbols, tf, start, end):
    out, token = {}, None
    for _ in range(30):
        q = {"symbols": ",".join(symbols), "timeframe": tf, "start": start, "end": end, "limit": 10000}
        if token:
            q["page_token"] = token
        d = _get(c, q) or {}
        for s, bs in (d.get("bars") or {}).items():
            out.setdefault(s, []).extend(bs)
        token = d.get("next_page_token")
        if not token:
            break
    return out


def _ny(t):
    """Alpaca bar time (UTC, bar start) -> NY bar start 'YYYY-MM-DDTHH:MM'."""
    return utc_to_ny(datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")).strftime("%Y-%m-%dT%H:%M")


def pick(c, sym, day, price, P):
    """The contract for a trade entered on `day` at `price`: [(occ, exp, strike, volume)] best first, or []."""
    k = price * (1 + float(P["money"]) / 100)
    exps = fridays(day, int(P["dteMin"]), int(P["dteMax"]))
    if not exps:
        return []
    cands = {occ(sym, e, s): (e, s) for e in exps for s in strikes_near(k)[:5]}
    nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    got = {}
    syms = list(cands)
    for i in range(0, len(syms), 50):
        got.update(bars(c, syms[i:i + 50], "1Day", day, nxt))
    rows = []
    for s, bs in got.items():
        b = next((x for x in bs if x["t"][:10] == day), None)
        if b and b.get("v"):
            e, st = cands[s]
            rows.append((abs(st - k), e, s, st, b["v"]))
    rows.sort(key=lambda r: (round(r[0], 2), r[1]))   # nearest strike, then the nearest expiration
    return [(r[2], r[1], r[3], r[4]) for r in rows]


def clean_path(path):
    """Option trade prints include stray ones (legs of spreads, odd lots) far from the real market: a 30-minute close
    more than 25% under / 33% over BOTH neighbours is replaced by their middle. The bars are then reduced to their
    close, and the target / stop are checked on those closes (a single odd high / low print can't trigger them)."""
    cs = [p[4] for p in path]
    for i in range(1, len(cs) - 1):
        a, b = cs[i - 1], cs[i + 1]
        if cs[i] < 0.75 * min(a, b) or cs[i] > 1.33 * max(a, b):
            cs[i] = (a + b) / 2
    return [(p[0], c, c, c, c) for p, c in zip(path, cs)]


def simulate(path, entry, entry_end, stock_exit_end, exp, tp, sl, follow, slip):
    """path: [(bar end 'YYYY-MM-DDTHH:MM', o, h, l, c)] after the entry bar (cleaned: o = h = l = c).
    Returns (pct, reason, at, exit price)."""
    e = entry * (1 + slip)
    last_day = (datetime.strptime(exp, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    out = None
    for t, o, h, l, cl in path:
        if sl is not None and l <= e * (1 - sl / 100):
            out = (l, "option stop", t)
            break
        if tp is not None and h >= e * (1 + tp / 100):
            out = (h, "option target", t)
            break
        if follow and stock_exit_end and t >= stock_exit_end:
            out = (cl, "stock exit", t)
            break
        if t[:10] >= last_day and t[11:16] >= "16:00":
            out = (cl, "expiration", t)
            break
    if not out:
        if not path:
            return None
        t, o, h, l, cl = path[-1]
        out = (cl, "open", t)
    px = out[0] * (1 - slip)
    return round((px / e - 1) * 100, 1), out[1], out[2], round(out[0], 2)


def _stats(pcts):
    n = len(pcts)
    if not n:
        return {"n": 0}
    w = [x for x in pcts if x > 0]
    lo = [x for x in pcts if x <= 0]
    s = sorted(pcts)
    return {"n": n, "win": round(len(w) / n * 100, 1), "avg": round(sum(pcts) / n, 1), "med": s[n // 2],
            "tot": round(sum(pcts)), "pf": round(sum(w) / -sum(lo), 2) if lo and sum(lo) < 0 else None,
            "avgWin": round(sum(w) / len(w), 1) if w else None, "avgLoss": round(sum(lo) / len(lo), 1) if lo else None}


def price_one(c, t, P):
    """One stock trade {sym, at, entry, exitAt, exit} bought as a call: {**t, opt: {...} | None, why, _grid}."""
    import wallwatch
    slip = float(P["slip"]) / 100
    day = t["at"][:10]
    entry_end = wallwatch._bar_end(t["at"]).strftime("%Y-%m-%dT%H:%M")
    stock_exit_end = wallwatch._bar_end(t["exitAt"]).strftime("%Y-%m-%dT%H:%M") if t.get("exitAt") and t.get("exit") != "open" else None
    ch = pick(c, t["sym"], day, float(t["entry"]), P)
    if not ch:
        return {**t, "opt": None, "why": "no call traded that day in the window"}
    sym_, exp, strike, vol = ch[0]
    end_d = min(exp, (datetime.strptime((t.get("exitAt") or day)[:10], "%Y-%m-%d") + timedelta(days=45)).strftime("%Y-%m-%d"))
    end = min(datetime.utcnow() - timedelta(minutes=20), datetime.strptime(end_d, "%Y-%m-%d") + timedelta(days=1))
    bs = bars(c, [sym_], "30Min", day, end.strftime("%Y-%m-%dT%H:%M:%SZ")).get(sym_) or []
    path = []
    for b in bs:
        st = _ny(b["t"])
        en = (datetime.strptime(st, "%Y-%m-%dT%H:%M") + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M")
        if "09:30" <= st[11:16] < "16:00":
            path.append((en, b["o"], b["h"], b["l"], b["c"]))
    path = clean_path(path)
    before = [p for p in path if p[0] <= entry_end and p[0][:10] == day]
    if not before:
        return {**t, "opt": None, "why": "no option trade before the entry"}
    entry = before[-1][4]
    after = [p for p in path if p[0] > entry_end]
    res = {}
    for tp in TPS:
        for sl in SLS:
            for fo in (True, False):
                r = simulate(after, entry, entry_end, stock_exit_end, exp, tp, sl, fo, slip)
                if r:
                    res[f"{tp}|{sl}|{int(fo)}"] = r
    mine = simulate(after, entry, entry_end, stock_exit_end, exp, None if P.get("tp") in (None, "", 0) else float(P["tp"]),
                    None if P.get("sl") in (None, "", 0) else float(P["sl"]), bool(P.get("follow", True)), slip)
    mfe = max([p[2] for p in after] or [entry])
    return {**t, "opt": {"sym": sym_, "exp": exp, "strike": strike, "vol": vol, "entry": round(entry, 2),
                         "mfe": round((mfe / (entry * (1 + slip)) - 1) * 100, 1),
                         "pct": mine[0] if mine else None, "why": mine[1] if mine else None, "exitAt": mine[2] if mine else None,
                         "exitPx": mine[3] if mine else None, "entryAt": before[-1][0]},
            "_grid": res}


DEF = {"dteMin": 7, "dteMax": 21, "money": 0.0, "tp": 50, "sl": 40, "follow": True, "slip": 2.0, "maxTrades": 300}


def run_sym(sub, sym, trades, params=None, budget_s=270):
    """The Strategy lab chart: this ticker's trades bought as calls (stored per ticker)."""
    import alpaca
    t0 = time.time()
    pk, sk = db.upk(sub), f"WWOPTSYM#{sym}"
    P = {**DEF, **(params or {})}
    c = alpaca.creds(sub)
    if not c:
        out = {"PK": pk, "SK": sk, "running": None, "at": iso(now_ny()), "errors": ["Connect Alpaca (Settings) to load option prices."]}
        db.put(out)
        return out
    tr = [{"sym": sym, **{k: t.get(k) for k in ("at", "entry", "exitAt", "exit", "R", "pct")}} for t in trades
          if (t.get("at") or "") >= "2024-02-05" and t.get("entry")][-60:]
    rows, errors = [], []
    pool = ThreadPoolExecutor(max_workers=4)
    for f in [pool.submit(price_one, c, t, P) for t in tr]:
        try:
            rows.append(f.result(timeout=max(1, budget_s - (time.time() - t0))))
        except Exception as e:
            errors.append(str(e)[:120] or "time limit")
    pool.shutdown(wait=False, cancel_futures=True)
    by = {r["at"]: {**(r.get("opt") or {}), **({} if r.get("opt") else {"none": r.get("why")})} for r in rows}
    ok = [r["opt"]["pct"] for r in rows if r.get("opt") and r["opt"].get("pct") is not None]
    out = {"PK": pk, "SK": sk, "running": None, "at": iso(now_ny()), "params": P, "byAt": by, "v": 2, "stats": _stats(ok),
           "errors": errors[:3], "older": sum(1 for t in trades if (t.get("at") or "") < "2024-02-05")}
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


def result_sym(sub, sym):
    import backtest
    r = backtest._unstick(db.get(db.upk(sub), f"WWOPTSYM#{sym}") or {})
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


def run(sub, params=None, budget_s=540):
    import alpaca
    import backtest
    import wallwatch
    t0 = time.time()
    pk = db.upk(sub)
    P = {**DEF, **(params or {})}
    db.put({"PK": pk, "SK": OPT_SK, **(db.get(pk, OPT_SK) or {}), "running": iso(now_ny()), "params": P})
    c = alpaca.creds(sub)
    if not c:
        out = {"PK": pk, "SK": OPT_SK, "running": None, "at": iso(now_ny()), "errors": ["Connect Alpaca (Settings) to load option prices."]}
        db.put(out)
        return out
    lab = db.get(pk, backtest.LAB_SK) or {}
    trades = [t for t in (lab.get("trades") or []) if t.get("at", "") >= "2024-02-05" and t.get("entry")]
    trades = trades[-int(P["maxTrades"]):]
    errors = []

    def one(t):
        return price_one(c, t, P)

    rows = []
    pool = ThreadPoolExecutor(max_workers=4)
    futs = [pool.submit(one, t) for t in trades]
    for f in futs:
        try:
            rows.append(f.result(timeout=max(1, budget_s - (time.time() - t0))))
        except Exception as e:
            errors.append(str(e)[:120] or "time limit")
    pool.shutdown(wait=False, cancel_futures=True)
    ok = [r for r in rows if r.get("opt") and r["opt"].get("pct") is not None]
    grid = []
    for tp in TPS:
        for sl in SLS:
            for fo in (True, False):
                k = f"{tp}|{sl}|{int(fo)}"
                st = _stats([r["_grid"][k][0] for r in ok if k in r["_grid"]])
                grid.append({"tp": tp, "sl": sl, "follow": fo, **st})
    grid.sort(key=lambda g: -(g.get("avg") or -1e9))
    mine = _stats([r["opt"]["pct"] for r in ok])
    mine["exits"] = {}
    for r in ok:
        mine["exits"][r["opt"]["why"]] = mine["exits"].get(r["opt"]["why"], 0) + 1
    stock = backtest._stats([{"at": r["at"], "R": r["R"]} for r in ok], "9999") if ok else {"n": 0}
    lst = [{k: r.get(k) for k in ("sym", "at", "R", "pct", "exit", "opt")} for r in ok][-200:]
    out = {"PK": pk, "SK": OPT_SK, "running": None, "at": iso(now_ny()), "secs": round(time.time() - t0), "params": P,
           "tested": len(trades), "priced": len(ok), "skipped": len(rows) - len(ok), "mine": mine, "stock": stock,
           "grid": grid[:60], "trades": lst, "labAt": lab.get("at"),
           "errors": (errors[:3] + ([f"{len(errors)} trades failed"] if len(errors) > 3 else []))}
    db.put(out)
    return {k: v for k, v in out.items() if k not in ("PK", "SK")}


def result(sub):
    import backtest
    r = backtest._unstick(db.get(db.upk(sub), OPT_SK) or {})
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}
