"""Chart context at entry, computed from the underlying's daily bars.

Everything is measured as of the PRIOR day's close (what you could see before the session),
plus the entry day's open (gap) and full-day volume (relative volume).
Options use the underlying stock, futures the continuous front-month contract.
"""
from datetime import datetime, timedelta

import db
from charts import _yahoo  # Yahoo daily bars, no key needed
from grouping import describe
from util import parse_iso

VERSION = 1


def _sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[-n - 1:-1], closes[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = (gains / n) / (losses / n)
    return 100 - 100 / (1 + rs)


def compute(bars, entry_date):
    """bars: daily bars sorted by time [{t,o,h,l,c,v}]. Returns the context dict or None."""
    prior = [b for b in bars if b["t"][:10] < entry_date]
    today = next((b for b in bars if b["t"][:10] == entry_date), None)
    if len(prior) < 21:
        return None
    closes = [b["c"] for b in prior]
    prev = closes[-1]
    ma10, ma20, ma50, ma200 = (_sma(closes, n) for n in (10, 20, 50, 200))
    adr_pct = sum((b["h"] / b["l"] - 1) * 100 for b in prior[-20:] if b["l"]) / 20
    adr_dollars = prev * adr_pct / 100 or None
    hi20 = max(b["h"] for b in prior[-20:])
    hi52 = max(b["h"] for b in prior[-252:])
    lo52 = min(b["l"] for b in prior[-252:])
    vols = [b.get("v") or 0 for b in prior[-20:]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    ctx = {
        "v": VERSION, "prevClose": round(prev, 4),
        "ma10": ma10 and round(ma10, 4), "ma20": ma20 and round(ma20, 4),
        "ma50": ma50 and round(ma50, 4), "ma200": ma200 and round(ma200, 4),
        "above50": None if ma50 is None else prev > ma50,
        "above200": None if ma200 is None else prev > ma200,
        "adrPct": round(adr_pct, 2),
        "ext20Adr": round((prev - ma20) / adr_dollars, 2) if ma20 and adr_dollars else None,
        "fromHigh20Pct": round((prev / hi20 - 1) * 100, 2),
        "fromHigh52Pct": round((prev / hi52 - 1) * 100, 2),
        "range52Pos": round((prev - lo52) / (hi52 - lo52) * 100, 1) if hi52 > lo52 else None,
        "rsi14": (lambda r: r if r is None else round(r, 1))(_rsi(closes)),
        "chg5Pct": round((prev / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else None,
        "chg20Pct": round((prev / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else None,
    }
    if today:
        ctx["gapPct"] = round((today["o"] / prev - 1) * 100, 2)
        ctx["dayChgPct"] = round((today["c"] / prev - 1) * 100, 2)
        ctx["rvol"] = round((today.get("v") or 0) / avg_vol, 2) if avg_vol else None
        ctx["brokeHigh20"] = today["h"] > hi20
    trend = "Uptrend" if ma50 and ma200 and prev > ma50 > ma200 else \
        "Downtrend" if ma50 and ma200 and prev < ma50 < ma200 else "Mixed" if ma50 and ma200 else None
    ctx["trend"] = trend
    return ctx


def analyze(sub, limit_symbols=12):
    """Compute context for trades missing it, a few underlyings per call (one Yahoo request each)."""
    pk = db.upk(sub)
    trades = [t for t in db.q_prefix(pk, "TRADE#") if (t.get("ctx") or {}).get("v") != VERSION]
    by_sym = {}
    for t in trades:
        d = describe(t["sym"])
        key = f"{d['underlying']}=F" if d["assetType"] == "future" else d["underlying"].replace(".", "-")
        by_sym.setdefault(key, []).append(t)
    done, failed = 0, []
    for sym in list(by_sym)[:limit_symbols]:
        ts = by_sym[sym]
        first = min(parse_iso(t["openTs"]) for t in ts)
        last = max(parse_iso(t["openTs"]) for t in ts)
        try:
            bars = _yahoo(sym, "1d", first - timedelta(days=400), min(last + timedelta(days=2), datetime.utcnow()))
        except Exception as e:  # keep going; mark these trades so they aren't retried forever
            failed.append(sym)
            bars = []
        for t in ts:
            ctx = compute(bars, t["openTs"][:10]) if bars else None
            db.update(pk, t["SK"], {"ctx": ctx or {"v": VERSION, "missing": True}})
            done += 1
    remaining = sum(len(v) for k, v in list(by_sym.items())[limit_symbols:])
    return {"analyzed": done, "remaining": remaining, "failedSymbols": failed}
