"""Chart studies for the day-trading chart (intraday): EMA bands with candle formations, the line in the sand
(previous regular-session close) with its 5-minute formations, the pre-market zone, the daily auto fibs, and the
squeeze turn signals. Bars: [{t "YYYY-MM-DDTHH:MM", o, h, l, c}] in NY time."""


def ema(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def agg(bars, m=5):
    """Candles -> m-minute candles on the clock (pre-market included)."""
    out, key = [], None
    for b in bars:
        hm = int(b["t"][11:13]) * 60 + int(b["t"][14:16])
        k0 = hm // m * m
        k = f"{b['t'][:10]}T{k0 // 60:02d}:{k0 % 60:02d}"
        if k != key:
            key = k
            out.append({"t": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
        else:
            x = out[-1]
            x.update(h=max(x["h"], b["h"]), l=min(x["l"], b["l"]), c=b["c"])
    return out


def _inside_runs(bars, ref):
    """X / XX / XXX of the formations: the candle stays inside the previous one (X), and the next ones stay inside
    the candles 2 / 3 back. ref(j, i): the reference test for X at candle i against candle j = i - 1."""
    n = len(bars)
    X, XX, XXX = [False] * n, [False] * n, [False] * n
    for i in range(1, n):
        X[i] = ref(i - 1, i)
        if i >= 2:
            XX[i] = X[i - 1] and bars[i - 2]["h"] >= bars[i]["c"] and bars[i - 2]["l"] <= bars[i]["c"]
        if i >= 3:
            XXX[i] = XX[i - 1] and bars[i - 3]["h"] >= bars[i]["c"] and bars[i - 3]["l"] <= bars[i]["c"]
    return X, XX, XXX


def bands(bars, lens=(9, 13, 22, 44)):
    """EMA bands: superfast / fast / slow / trend EMAs, the state (1 = superfast > fast > slow, 2 = the reverse, 3 =
    mixed) and the candle formations: a breakout candle closing past the high (low) of a same-color candle 2-4 back
    after inside candles, kept when the state agrees. Returns (series, formations)."""
    cl = [b["c"] for b in bars]
    sf, f, s, tr = (ema(cl, n) for n in lens)
    st = [1 if a > b and b > c else 2 if a < b and b < c else 3 for a, b, c in zip(sf, f, s)]
    th = [max(b["o"], b["c"]) for b in bars]
    tl = [min(b["o"], b["c"]) for b in bars]
    X, XX, XXX = _inside_runs(bars, lambda j, i: bars[j]["h"] >= bars[i]["o"] and th[j] >= bars[i]["c"] and tl[j] <= bars[i]["c"] and bars[j]["l"] <= bars[i]["o"])
    G = [b["o"] < b["c"] for b in bars]
    R = [b["o"] > b["c"] for b in bars]
    out = []
    for i in range(4, len(bars)):
        b = bars[i]
        bull = (G[i - 2] and X[i - 1] and b["c"] > bars[i - 2]["h"]) or (G[i - 3] and XX[i - 1] and b["c"] > bars[i - 3]["h"]) or (G[i - 4] and XXX[i - 1] and b["c"] > bars[i - 4]["h"])
        bear = (R[i - 2] and X[i - 1] and b["c"] < bars[i - 2]["l"]) or (R[i - 3] and XX[i - 1] and b["c"] < bars[i - 3]["l"]) or (R[i - 4] and XXX[i - 1] and b["c"] < bars[i - 4]["l"])
        if bull and b["c"] > b["o"] and st[i] == 1:
            out.append({"t": b["t"], "dir": 1, "p": b["l"]})
        elif bear and b["c"] < b["o"] and st[i] == 2:
            out.append({"t": b["t"], "dir": -1, "p": b["h"]})
    ser = [{"t": b["t"], "sf": round(a, 4), "sl": round(c, 4), "tr": round(d, 4), "st": e} for b, a, c, d, e in zip(bars, sf, s, tr, st)]
    return ser, out


def lis(bars5):
    """Line in the sand = the previous regular-session close (the 16:00 close). 5-minute formations off it:
    a test (low under the line, open / close above it), a drop through, a test from under, a push through; then
    inside candles and a breakout candle closing past the test candle, confirmed by the next close. Returns
    ({day: line}, formations, {day: sentiment at the end of the day})."""
    reg = [b for b in bars5 if "09:30" <= b["t"][11:16] < "16:00"]
    lines, last_close, day = {}, None, None
    for b in reg:
        d = b["t"][:10]
        if d != day:
            if day is not None and last_close is not None:
                lines[d] = last_close
            day = d
        last_close = b["c"]
    sf, fs, sl = ema([b["c"] for b in reg], 9), ema([b["c"] for b in reg], 22), ema([b["c"] for b in reg], 44)
    n = len(reg)
    L = [lines.get(b["t"][:10]) for b in reg]
    ok = [L[i] is not None and not (b["t"][11:16] >= "15:55") for i, b in enumerate(reg)]
    DT = [ok[i] and b["l"] <= L[i] and b["o"] >= L[i] and b["c"] >= L[i] for i, b in enumerate(reg)]
    DP = [ok[i] and b["l"] <= L[i] and b["o"] > L[i] and b["c"] < L[i] for i, b in enumerate(reg)]
    MU = [ok[i] and b["h"] >= L[i] and b["o"] <= L[i] and b["c"] <= L[i] for i, b in enumerate(reg)]
    NS = [ok[i] and b["h"] >= L[i] and b["o"] < L[i] and b["c"] > L[i] for i, b in enumerate(reg)]
    anyt = [a or b or c or d for a, b, c, d in zip(DT, DP, MU, NS)]
    X, XX, XXX = _inside_runs(reg, lambda j, i: anyt[j] and reg[j]["h"] >= reg[i]["o"] and reg[j]["h"] >= reg[i]["c"] and reg[j]["l"] <= reg[i]["c"] and reg[j]["l"] <= reg[i]["o"])
    out, sent, sent_d = [], 0, {}
    for i in range(4, n - 1):
        b, nx = reg[i], reg[i + 1]
        if b["t"][:10] != nx["t"][:10]:
            continue
        def form(T, up):
            for k, run in ((2, X), (3, XX), (4, XXX)):
                if T[i - k] and run[i - 1]:
                    if up and b["c"] > reg[i - k]["h"] and b["c"] > b["o"] and nx["c"] >= reg[i - k]["c"]:
                        return True
                    if not up and b["c"] < reg[i - k]["l"] and b["c"] < b["o"] and nx["c"] <= reg[i - k]["c"]:
                        return True
            return False
        bull = form(DT, True) or form(NS, True)
        bear = form(MU, False) or form(DP, False)
        if bull or bear:
            strong = (sf[i] > fs[i] > sl[i]) if bull else (sf[i] < fs[i] < sl[i])
            out.append({"t": b["t"], "dir": 1 if bull else -1, "p": b["l"] if bull else b["h"], "strong": bool(strong)})
            sent = 1 if bull else -1
        sent_d[b["t"][:10]] = sent
    return lines, out, sent_d


def pmz(bars5, lines):
    """Pre-market zone of each day: the 07:25-09:30 high / low (5-minute candles); a gap up (the last pre-market open
    at or above the line in the sand) puts the zone 20-40% under the high, a gap down 20-40% above the low."""
    out = {}
    by = {}
    for b in bars5:
        if "07:25" < b["t"][11:16] < "09:30" or b["t"][11:16] == "07:25":
            by.setdefault(b["t"][:10], []).append(b)
    for d, bs in by.items():
        h, l = max(b["h"] for b in bs), min(b["l"] for b in bs)
        r = h - l
        line = lines.get(d)
        gap = 1 if line is None or bs[-1]["o"] >= line else 0
        up, lo = (h - r * 0.2, h - r * 0.4) if gap else (l + r * 0.4, l + r * 0.2)
        out[d] = {"up": round(up, 4), "lo": round(lo, 4), "gap": gap, "h": round(h, 4), "l": round(l, 4)}
    return out


def fibs(bars, lines):
    """Daily auto fibs: the day's high / low (the previous close included), and 127.2% / 61.8% / 50% / 38.2% /
    -27.2% levels."""
    out, by = {}, {}
    for b in bars:
        if "09:30" <= b["t"][11:16] < "16:00":
            by.setdefault(b["t"][:10], []).append(b)
    for d, bs in by.items():
        h, l = max(b["h"] for b in bs), min(b["l"] for b in bs)
        if lines.get(d) is not None:
            h, l = max(h, lines[d]), min(l, lines[d])
        r = h - l
        out[d] = [["127.2%", round(h + r * 0.272, 4)], ["high", round(h, 4)], ["38.2%", round(h - r * 0.382, 4)],
                  ["50%", round(h - r * 0.5, 4)], ["61.8%", round(h - r * 0.618, 4)], ["low", round(l, 4)],
                  ["-27.2%", round(h - r * 1.272, 4)]]
    return out


def turns(sq):
    """Squeeze turn signals: momentum above 0 that peaks (turn down), or below 0 that bottoms (turn up).
    sq: [{"t", "m"}]. Returns [{"t", "dir"}]."""
    out = []
    for i in range(2, len(sq)):
        a, b, c = sq[i - 2]["m"], sq[i - 1]["m"], sq[i]["m"]
        if c > 0 and c < b and a < b:
            out.append({"t": sq[i]["t"], "dir": -1})
        elif c < 0 and c > b and a > b:
            out.append({"t": sq[i]["t"], "dir": 1})
    return out
