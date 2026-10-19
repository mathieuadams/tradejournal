"""Day-trading trades priced on real options: each trade (long -> call, short -> put) on the nearest expiration (0DTE on
SPY / QQQ, the coming Friday for the others), strike nearest to the entry price x (1 ± moneyness %, out of the money
when > 0). Alpaca 1-minute option bars (history from Feb 2024): bought at the close of the option bar of the entry
minute, sold at the close of the option bar of the exit minute (the last bar at or before it). Stray prints (far from
both neighbours) are smoothed. Slippage taken on both sides. A contract's day of bars is fetched once and reused."""
import time
from datetime import datetime, timedelta

from util import ny_to_utc

DAILY = {"SPY", "QQQ", "IWM", "SPX", "SPXW", "XSP", "NDX"}
FIRST = "2024-02-01"


def occ(sym, exp, cp, strike):
    root = sym.replace(".", "").replace("-", "").upper()
    return f"{root}{exp[2:4]}{exp[5:7]}{exp[8:10]}{cp}{int(round(strike * 1000)):08d}"


def expiries(sym, day):
    d0 = datetime.strptime(day, "%Y-%m-%d").date()
    ds = [d0 + timedelta(days=i) for i in range(0, 8)]
    ds = [d for d in ds if d.weekday() < 5]
    if sym.upper() not in DAILY:
        ds.sort(key=lambda d: (d.weekday() != 4, d))
    return [d.isoformat() for d in ds[:3]]


def _utc(t):
    return ny_to_utc(datetime.strptime(t, "%Y-%m-%dT%H:%M")).strftime("%Y-%m-%dT%H:%M:%SZ")


def _smooth(path):
    cs = [c for _, c in path]
    for i in range(1, len(cs) - 1):
        a, b = cs[i - 1], cs[i + 1]
        if cs[i] < 0.75 * min(a, b) or cs[i] > 1.33 * max(a, b):
            cs[i] = (a + b) / 2
    return [(t, c) for (t, _), c in zip(path, cs)]


def _at(path, t, before=5):
    """Close of the last bar at or before minute t (at most `before` minutes earlier)."""
    best = None
    for tt, c in path:
        if tt <= t:
            best = (tt, c)
        else:
            break
    if not best:
        return None
    gap = (datetime.strptime(t, "%Y-%m-%dT%H:%M") - datetime.strptime(best[0], "%Y-%m-%dT%H:%M")).seconds // 60
    return best[1] if gap <= before else None


def price(sub, rows, opt=None, budget_s=150, cache=None):
    """Adds optPct / optSym / optIn / optOut to the trades (most recent first, within the time budget). Returns the
    summary statistics in %."""
    import daytrade
    import optiontest
    t0 = time.time()
    o = opt or {}
    money = float(o.get("money") or 0)
    slip = float(o.get("slip") if o.get("slip") is not None else 1) / 100
    c = daytrade.data_creds(sub)
    if not c:
        return {"n": 0, "error": "No Alpaca account for option data."}
    cache = {} if cache is None else cache                 # (contract, day) -> [(minute, close)], shareable

    def path(sym_o, day):
        k = (sym_o, day)
        if k not in cache:
            got = optiontest.bars(c, [sym_o], "1Min", _utc(f"{day}T09:30"), _utc(f"{day}T16:00"))
            cache[k] = _smooth(sorted((optiontest._ny(b["t"]), float(b["c"])) for b in got.get(sym_o) or []))
        return cache[k]

    todo = [r for r in rows if r.get("exitAt") and r["at"][:10] >= FIRST]
    skipped = len([r for r in rows if r["at"][:10] < FIRST])
    done = 0
    for r in reversed(todo):
        if time.time() - t0 > budget_s:
            break
        day, side = r["at"][:10], r.get("side") or (1 if r.get("dir") != "short" else -1)
        cp = "C" if side > 0 else "P"
        k = r["entry"] * (1 + side * money / 100)
        try:
            for exp in expiries(r["sym"], day):
                hit = None
                for st in optiontest.strikes_near(k)[:3]:
                    s_ = occ(r["sym"], exp, cp, st)
                    pth = path(s_, day)
                    pin = _at(pth, r["at"])
                    if pin and pin > 0:
                        hit = (s_, pth, pin)
                        break
                if hit:
                    s_, pth, pin = hit
                    pout = _at(pth, r["exitAt"], before=400) or pin
                    bi, so = pin * (1 + slip), pout * (1 - slip)
                    r.update(optSym=s_, optIn=round(bi, 3), optOut=round(so, 3), optPct=round((so / bi - 1) * 100, 2))
                    done += 1
                    break
        except Exception as e:
            print("option price failed", r.get("sym"), r.get("at"), e)
    ps = [r["optPct"] for r in rows if r.get("optPct") is not None]
    return {**summary(ps), "priced": done, "of": len(rows), "before2024": skipped, "money": money, "slip": round(slip * 100, 2)}


def summary(ps):
    n = len(ps)
    if not n:
        return {"n": 0}
    w = [x for x in ps if x > 0]
    lo = [x for x in ps if x <= 0]
    srt = sorted(ps)
    return {"n": n, "win": round(len(w) / n * 100, 1), "avg": round(sum(ps) / n, 2), "med": srt[n // 2],
            "tot": round(sum(ps), 1), "pf": round(sum(w) / -sum(lo), 2) if lo and sum(lo) < 0 else None,
            "best": srt[-1], "worst": srt[0]}
