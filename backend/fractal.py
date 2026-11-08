"""Fractal wave engine: a rebuild of the TradeWellX "Wave [N]" chart from OHLC bars.

Rules worked out from the TradeWellX Data tab (QQQ, Wave 10, 15-minute bars, 10/26-10/30/2026):
  MA        = (highest close + lowest close) / 2 over the last `wave` bars (the purple vector)
  SlowMA    = the same over 2 x `wave` bars
  Pivot     = the MA value at its last turn (top or bottom); updates on the bar the MA reverses
  Direction = +1 / -1, flips only when the MA breaks above its last swing high / below its last swing low
  Wave      = a run of bars whose close stays on one side of the pivot; a close across the pivot starts the next
  Label     = H<n> / L<n>: n = bars from the wave's first bar to its highest high (H) or lowest low (L)
  Move      = how far the wave got from the pivot: highest high / pivot - 1 (H), 1 - lowest low / pivot (L)
  MacroTrend= +1 (L) / -1 (S): the close above / below the average of the 50 closes before it; counted L001, L002...
              like TradeWellX's MacroTrend column (starts on the 51st bar: 10/27 15:30 for a 10/26 09:30 start)
  StdDev    = standard deviation of the last `wave` closes, % of their average (TradeWellX "StdDev" panel:
              < 0.1 squeeze, 0.5 default, > 0.8 expanding)
  Clusters  = pivot x (1 +/- the move of the wave nearest to rank p: the move p of the waves reached or beat), p = 80/50/20/10/5 (bull), 80/50/20/10/2 (bear)

Bars: list of [t, o, h, l, c] or [t, o, h, l, c, v]. Plain Python, no third-party packages."""

BULL_P = (0.80, 0.50, 0.20, 0.10, 0.05)
BEAR_P = (0.80, 0.50, 0.20, 0.10, 0.02)


def midline(close, n):
    """(highest close + lowest close) / 2 over the last n closes; None until n closes exist."""
    out = [None] * len(close)
    for i in range(n - 1, len(close)):
        w = close[i - n + 1:i + 1]
        out[i] = (max(w) + min(w)) / 2
    return out


def macro_trend(close, n=50):
    """+1 / -1: the close above / below the average of the n closes before it; None until n closes exist."""
    out, run = [None] * len(close), 0.0
    for i, c in enumerate(close):
        if i >= n:
            out[i] = 1 if c >= run / n else -1
            run -= close[i - n]
        run += c
    return out


def stdev_pct(close, n):
    """Population standard deviation of the last n closes, in % of their average; None until n closes exist."""
    out = [None] * len(close)
    for i in range(n - 1, len(close)):
        w = close[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = (sum((x - m) ** 2 for x in w) / n) ** 0.5 / m * 100
    return out


def _sign(x, eps=1e-9):
    return 1 if x > eps else -1 if x < -eps else 0


def compute(bars, wave=10, win=0):
    """Per-bar series and the list of waves. Bars before `start` only warm up the MA and the pivot (TradeWellX loads
    history before the window, so its first wave starts on the window's first bar)."""
    t = [b[0] for b in bars]
    h = [float(b[2]) for b in bars]
    lo = [float(b[3]) for b in bars]
    c = [float(b[4]) for b in bars]
    ma, slow = midline(c, wave), midline(c, 2 * wave)
    n = len(bars)
    pivot, direction = [None] * n, [0] * n
    piv = None
    slope = 0                       # last non-flat MA direction
    swing_hi = swing_lo = None
    d = 0
    for i in range(n):
        if ma[i] is not None and i and ma[i - 1] is not None:
            s = _sign(ma[i] - ma[i - 1])
            if s and slope and s != slope:            # the MA reversed: its previous value is the new pivot
                piv = ma[i - 1]
                if slope > 0:
                    swing_hi = piv
                else:
                    swing_lo = piv
            if s:
                slope = s
            if piv is None:
                piv = ma[i - 1]
        if ma[i] is not None:
            if swing_lo is not None and ma[i] < swing_lo:
                d = -1
            elif swing_hi is not None and ma[i] > swing_hi:
                d = 1
        pivot[i], direction[i] = piv, d

    # waves: a close across the pivot starts the next one
    waves, side, start = [], 0, None
    since = [None] * n
    label = [None] * n

    def close_wave(end):
        if side == 0 or start is None:
            return
        idx = range(start, end + 1)
        if side > 0:
            k = max(idx, key=lambda j: h[j])
            move = h[k] / pivot[k] - 1
        else:
            k = min(idx, key=lambda j: lo[j])
            move = 1 - lo[k] / pivot[k]
        cnt = k - start + 1
        label[k] = cnt * side
        waves.append({"side": "H" if side > 0 else "L", "start": t[start], "end": t[end], "bars": end - start + 1,
                      "extreme": t[k], "count": cnt, "price": h[k] if side > 0 else lo[k], "pivot": pivot[k],
                      "move": move})

    for i in range(win, n):
        if pivot[i] is None:
            continue
        s = 1 if c[i] > pivot[i] else -1
        if s != side:
            close_wave(i - 1)
            side, start = s, i
        since[i] = i - start
    open_wave = None
    if side and start is not None:
        close_wave(n - 1)
        open_wave = waves[-1]                         # the wave still running: counted in the statistics, like TradeWellX
        open_wave["open"] = True

    macro, mcount, sd = macro_trend(c), [None] * n, stdev_pct(c, wave)
    run = 0
    for i in range(n):
        if macro[i] is not None:
            run = run + 1 if i and macro[i - 1] == macro[i] else 1
            mcount[i] = ("L" if macro[i] > 0 else "S") + f"{run:03d}"
    series = [{"t": t[i], "ma": ma[i], "slowMa": slow[i], "pivot": pivot[i], "direction": direction[i],
               "barsElapsed": since[i], "peakToTrough": label[i], "macro": macro[i], "macroTrend": mcount[i],
               "stdev": sd[i]} for i in range(n)][win:]
    return {"series": series, "waves": waves, "current": open_wave,
            "pivot": pivot[-1] if n else None, "close": c[-1] if n else None}


def _rank(n, p):
    return min(n - 1, max(0, round((1 - p) * (n - 1))))


def levels(values, ps):
    """For each p (inner to outer: 80, 50, 20, 10, 5 / 2 %), the move that a fraction p of `values` reached or beat:
    the nearest wave's own move, no interpolation (rank (1 - p) x (n - 1), rounded). Two levels never share a wave:
    an outer level that lands on the same wave as the one inside it takes the next wave out.
    Matches TradeWellX on QQQ 10/26-11/06/2026 (Wave 10, 15 min) to within ~0.05%."""
    v = sorted(values)
    out, last = [], -1
    for p in ps:
        i = _rank(len(v), p)
        if i <= last:
            i = min(len(v) - 1, last + 1)
        out.append(v[i])
        last = i
    return out


def survival(values, p):
    v = sorted(values)
    return v[_rank(len(v), p)] if v else None


def clusters(waves, pivot, ma=None):
    """Green (bull) and red (bear) percent-probable levels. Like TradeWellX, green is measured from the lower of the
    pivot and the purple vector (MA), red from the higher: in a drop the targets up start from the vector, not the pivot."""
    ma = pivot if ma is None else ma
    lo_base, hi_base = min(pivot, ma), max(pivot, ma)
    up = [w["move"] for w in waves if w["side"] == "H"]
    dn = [w["move"] for w in waves if w["side"] == "L"]
    bull = [{"p": p, "move": m, "price": lo_base * (1 + m)} for p, m in zip(BULL_P, levels(up, BULL_P))] if up else []
    bear = [{"p": p, "move": -m, "price": hi_base * (1 - m)} for p, m in zip(BEAR_P, levels(dn, BEAR_P))] if dn else []
    return {"bull": bull, "bear": bear, "bullBase": lo_base, "bearBase": hi_base}


def odds(waves, side):
    """Option Analyzer table: for each label count, how many waves, % of waves, and odds of reaching at least it."""
    cnt = sorted(w["count"] for w in waves if w["side"] == side)
    tot = len(cnt)
    out = []
    for k in sorted(set(cnt)):
        out.append({"count": k, "n": cnt.count(k), "pct": cnt.count(k) / tot,
                    "atLeast": sum(1 for x in cnt if x >= k) / tot})
    return out


def steps(r, min_waves=4):
    """The cluster levels as they stood during each pivot: for every run of bars with the same pivot, the levels from
    the waves finished before it started (what was knowable then). i0 / i1: first / last bar of the run in `series`."""
    S, out, i = r["series"], [], 0
    done = [w for w in r["waves"] if not w.get("open")]
    while i < len(S):
        p, j = S[i]["pivot"], i
        while j + 1 < len(S) and S[j + 1]["pivot"] == p:
            j += 1
        prior = [w for w in done if w["end"] < S[i]["t"]]
        nh, nl = sum(w["side"] == "H" for w in prior), sum(w["side"] == "L" for w in prior)
        if p and nh >= min_waves and nl >= min_waves:
            out.append({"i0": i, "i1": j, "pivot": p, "nH": nh, "nL": nl, **clusters(prior, p)})
        i = j + 1
    return out


def analyze(bars, wave=10, win=0):
    r = compute(bars, wave, win)
    ma = r["series"][-1]["ma"] if r["series"] else None
    r["clusters"] = clusters(r["waves"], r["pivot"], ma) if r["pivot"] else {"bull": [], "bear": []}
    r["oddsH"], r["oddsL"] = odds(r["waves"], "H"), odds(r["waves"], "L")
    r["steps"] = steps(r)
    return r
