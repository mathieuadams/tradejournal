"""Volatility projection (TradeWellX "Forward Looking Volatility"), from daily bars.

  State today      = the fractal wave state of the last daily bar: H or L and how many bars into the wave (1H, 2L...),
                     with the Wave setting (TradeWellX's default for this view: Wave 1, 200 days)
  Matches          = earlier days in the same state (HL matching) or on the same weekday (weekday matching)
  For each match   = the highest high / lowest low over the next `fwd` days, in % from that day's close
  Per side         = Avg (mean), MinAvg / MaxAvg (mean of the moves below / at or above Avg), Max (the outlier), median
  Projection       = today's close x (1 + each %): wall long / short = the MaxAvg levels, medians = ~50% odds
  Counts           = how many matches reached each level (TradeWellX's "Bull count High$ at or above ... x out of n")
`asof` replays an earlier day: the projection from that day, and what then happened over the next `fwd` days."""
import fractal


def _mean(a):
    return sum(a) / len(a) if a else 0.0


def _median(a):
    s = sorted(a)
    n = len(s)
    return 0.0 if not n else s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def side_stats(moves, sign):
    """moves: % moves (up: >= 0 mostly; down: <= 0 mostly). sign: +1 bullish, -1 bearish (bigger = further from 0)."""
    if not moves:
        return None
    key = lambda m: m * sign
    avg = _mean(moves)
    hi = [m for m in moves if key(m) >= key(avg)]
    lo = [m for m in moves if key(m) < key(avg)]
    return {"n": len(moves), "max": max(moves, key=key), "maxAvg": _mean(hi) if hi else avg, "avg": avg,
            "minAvg": _mean(lo) if lo else avg, "median": _median(moves)}


def project(bars, wave=1, fwd=1, match="hl", asof=None):
    r = fractal.compute(bars, wave)
    S = r["series"]
    n = len(bars)
    idx = n - 1
    if asof:
        idx = max((i for i, b in enumerate(bars) if b[0][:10] <= asof), default=n - 1)
    st = lambda i: None if S[i]["pivot"] is None or S[i]["barsElapsed"] is None else \
        (("H" if bars[i][4] > S[i]["pivot"] else "L"), S[i]["barsElapsed"] + 1)
    now = st(idx)
    if now is None:
        raise ValueError("Not enough bars for a wave state.")
    wd = lambda i: __import__("datetime").date.fromisoformat(bars[i][0][:10]).weekday()
    ups, dns, used, paths = [], [], [], []
    # HL matching: the exact state (e.g. 3L); a rare state with no earlier match widens to the same side with an equal or
    # longer count (7L -> 7L, 8L, ...), then to any day on that side
    rules = [("weekday", lambda i: wd(i) == wd(idx))] if match == "weekday" else [
        ("state", lambda i: st(i) == now),
        ("side, count >= %d" % now[1], lambda i: st(i) is not None and st(i)[0] == now[0] and st(i)[1] >= now[1]),
        ("side", lambda i: st(i) is not None and st(i)[0] == now[0])]
    ok = lambda i: i + fwd <= idx                      # only matches whose next fwd days are known at `idx`
    rule, same = next(((name, fn) for name, fn in rules if any(fn(i) for i in range(idx) if ok(i))), rules[-1])
    for i in range(idx):
        if not ok(i):
            break
        if same(i):
            base = bars[i][4]
            hi = max(b[2] for b in bars[i + 1:i + 1 + fwd])
            lo = min(b[3] for b in bars[i + 1:i + 1 + fwd])
            ups.append(hi / base - 1)
            dns.append(lo / base - 1)
            used.append(bars[i][0][:10])
            paths.append([[b[2] / base - 1, b[3] / base - 1] for b in bars[i + 1:i + 1 + fwd]])   # each day's high / low
    if not used:
        raise ValueError(f"No earlier day in state {now[1]}{now[0]} with {fwd} day(s) after it.")
    px = bars[idx][4]
    bull, bear = side_stats(ups, 1), side_stats(dns, -1)
    lvl = lambda m: px * (1 + m)
    cnt = lambda arr, cond: sum(1 for m in arr if cond(m))
    k = len(used)
    counts = [
        ("Bull: high below the wall long (MaxAvg)", cnt(ups, lambda m: m < bull["maxAvg"])),
        ("Bull: high at or above MaxAvg", cnt(ups, lambda m: m >= bull["maxAvg"])),
        ("Bull: high at or above the median", cnt(ups, lambda m: m >= bull["median"])),
        ("Bull: high at or above Avg", cnt(ups, lambda m: m >= bull["avg"])),
        ("Bull: high at or above MinAvg", cnt(ups, lambda m: m >= bull["minAvg"])),
        ("Bear: low above the wall short (MaxAvg)", cnt(dns, lambda m: m > bear["maxAvg"])),
        ("Bear: low at or below MaxAvg", cnt(dns, lambda m: m <= bear["maxAvg"])),
        ("Bear: low at or below the median", cnt(dns, lambda m: m <= bear["median"])),
        ("Bear: low at or below Avg", cnt(dns, lambda m: m <= bear["avg"])),
        ("Bear: low at or below MinAvg", cnt(dns, lambda m: m <= bear["minAvg"])),
    ]
    after = bars[idx + 1:idx + 1 + fwd]                # what happened next (replay of an earlier day)
    unfold = None
    if after:
        unfold = {"days": [b[0][:10] for b in after], "high": max(b[2] for b in after), "low": min(b[3] for b in after),
                  "close": after[-1][4], "complete": len(after) == fwd}
    return {"date": bars[idx][0][:10], "price": px, "state": f"{now[1]}{now[0]}", "wave": wave, "fwd": fwd, "match": match, "matchedBy": rule,
            "from": bars[0][0][:10], "to": bars[-1][0][:10], "index": idx, "matches": k, "matchDays": used[-60:],
            "bull": {**bull, "levels": {key: lvl(bull[key]) for key in ("max", "maxAvg", "avg", "minAvg", "median")},
                     "outlierOdds": cnt(ups, lambda m: m >= bull["max"]) / k},
            "bear": {**bear, "levels": {key: lvl(bear[key]) for key in ("max", "maxAvg", "avg", "minAvg", "median")},
                     "outlierOdds": cnt(dns, lambda m: m <= bear["max"]) / k},
            "counts": [{"label": t, "n": c, "of": k, "pct": c / k} for t, c in counts],
            "unfold": unfold, "paths": paths, "bars": [[b[0][:10], b[1], b[2], b[3], b[4]] for b in bars[max(0, idx - 39):idx + 1 + fwd]], "dates": [b[0][:10] for b in bars]}
