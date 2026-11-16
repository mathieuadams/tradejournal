"""ETF correlation matrix: how the daily returns of a list of ETFs move together over a window.

Default list: sector, industry, commodity and country ETFs — no broad index (SPY, DIA, IWM, QQQ) and no bond fund.
Prices: the same Alpaca daily bars (split-adjusted) as the earnings study, kept in fund/px/<SYM>.
Correlation: Pearson, on daily close-to-close log returns, over the dates every ETF traded."""
import math
from datetime import timedelta

import db
from util import BadRequest, iso, now_ny

DEFAULT = ["XLK", "SMH", "XLC", "XLY", "XLF", "XLI", "XLB", "XLE", "XLV", "XBI", "XLP", "XLU", "XLRE", "GLD", "SLV", "EEM", "FXI"]
SK = "ETF#settings"
WINDOWS = (20, 60, 120, 250)
MAX = 40


def settings(sub):
    it = db.get(db.upk(sub), SK) or {}
    return {"syms": it.get("syms") or DEFAULT}


def save(sub, body):
    syms = []
    for s in body.get("syms") or []:
        s = str(s).strip().upper()
        if s and s.replace(".", "").replace("-", "").isalnum() and s not in syms:
            syms.append(s)
    if len(syms) < 2:
        raise BadRequest("At least two ETFs.")
    syms = syms[:MAX]
    db.put({"PK": db.upk(sub), "SK": SK, "syms": syms, "at": iso(now_ny())})
    return {"syms": syms}


def _corr(a, b):
    n = len(a)
    if n < 3:
        return None
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    saa = sum((x - ma) ** 2 for x in a)
    sbb = sum((y - mb) ** 2 for y in b)
    return sab / math.sqrt(saa * sbb) if saa > 0 and sbb > 0 else None


def order(syms, m):
    """Similar ETFs next to each other: start from the one most correlated with the rest, then always the nearest."""
    n = len(syms)
    if n < 3:
        return list(range(n))
    avg = [sum(m[i][j] or 0 for j in range(n) if j != i) / (n - 1) for i in range(n)]
    left = set(range(n))
    cur = max(left, key=lambda i: avg[i])
    out = [cur]
    left.remove(cur)
    while left:
        cur = max(left, key=lambda j: (m[cur][j] if m[cur][j] is not None else -2))
        out.append(cur)
        left.remove(cur)
    return out


def matrix(sub, days=60, syms=None):
    import fundamentals as F
    days = int(days) if int(days) in WINDOWS else 60
    syms = syms or settings(sub)["syms"]
    need = (now_ny().date() - timedelta(days=int(days * 1.6) + 30)).isoformat()
    closes, missing = {}, []
    for s in syms:
        try:
            bars = F.prices(sub, s, need, max_age_h=12)
        except Exception as ex:
            missing.append(f"{s}: {str(ex)[:80]}")
            continue
        if bars:
            closes[s] = {b[0]: b[4] for b in bars if b[4]}
        else:
            missing.append(f"{s}: no prices")
    ok = [s for s in syms if s in closes]
    if len(ok) < 2:
        raise BadRequest("Not enough ETFs with prices. " + "; ".join(missing))
    dates = sorted(set.intersection(*[set(closes[s]) for s in ok]))[-(days + 1):]
    rets = {s: [math.log(closes[s][dates[i]] / closes[s][dates[i - 1]]) for i in range(1, len(dates))] for s in ok}
    n = len(ok)
    m = [[1.0 if i == j else None for j in range(n)] for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            c = _corr(rets[ok[i]], rets[ok[j]])
            m[i][j] = m[j][i] = round(c, 4) if c is not None else None
    perf = {s: closes[s][dates[-1]] / closes[s][dates[0]] - 1 for s in ok}
    vol = {s: (sum(r * r for r in rets[s]) / max(1, len(rets[s]) - 1)) ** 0.5 * math.sqrt(252) for s in ok}
    avg = {ok[i]: round(sum(m[i][j] or 0 for j in range(n) if j != i) / (n - 1), 4) for i in range(n)}
    return {"syms": ok, "order": order(ok, m), "matrix": m, "days": len(dates) - 1, "window": days, "from": dates[0], "to": dates[-1],
            "perf": perf, "vol": vol, "avg": avg, "missing": missing, "windows": WINDOWS}
