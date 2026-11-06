"""Cycles: the vector, HL values, probability bands and the Average Mean Wave (AMW).

The vector: a moving average of the closes (`kind` ema / sma, `length` candles). A close crossing above it starts an up
cycle (below: a down cycle); the cycle ends at the next cross back. Cycles shorter than `minLen` candles are noise:
left out of the labels and the statistics.
  Base (the reversion point): the extreme the cycle started from (the low of the down cycle before an up cycle).
  HL value: the number of candles of the cycle (effort).
  Extension: from the base to the cycle's extreme (points and %), and the candles it took to get there.
From the completed cycles of the loaded history:
  Probability bands: from the current cycle's base, the levels that 80 / 50 / 20 / 10 / 5 % of past cycles (of the same
  direction) extended to or beyond.
  AMW: a parabola from the current base, peaking at the average extension after the average number of candles to the
  peak, coming back down over the same span (drawn forward past the last candle)."""
import futdata

BANDS = (80, 50, 20, 10, 5)


def params(q):
    def f(k, lo, hi, d, typ=int):
        try:
            return max(lo, min(hi, typ(q.get(k))))
        except (TypeError, ValueError):
            return d
    tf = q.get("tf") if q.get("tf") in ("5", "15", "60", "1D") else "60"
    return {"tf": tf, "days": f("days", 5, 2000, {"5": 10, "15": 30, "60": 50, "1D": 500}[tf]), "length": f("length", 2, 400, 20),
            "kind": q.get("kind") if q.get("kind") in ("ema", "sma") else "ema", "show": f("show", 20, 3000, 300),
            "sdLen": f("sdLen", 3, 100, 10), "macroLen": f("macroLen", 5, 400, 50), "minLen": f("minLen", 1, 50, 3)}


def candles(sym, p):
    if p["tf"] == "1D":
        D = futdata.daily(sym)[-p["days"]:]
        return [(d[0] + "T09:30", d[1], d[2], d[3], d[4]) for d in D]
    by = futdata.sessions(sym, p["days"], rth=False)
    out = []
    for d in sorted(by):
        out += [tuple(b[:5]) for b in futdata.agg(by[d], int(p["tf"]))]
    return out


def stddev(C, p):
    """The volatility panel (an approximation of the Fractal's standard-deviation tool, from the candles alone):
    v = the standard deviation of the last `sdLen` closes, relative to its median over the loaded history, scaled so the
    median reads 0.35 (squeeze under 0.1, compressed 0.1-0.5, expanding 0.5-0.8+, aggressive 1.6+). Each point also has
    the price direction over those candles (+1 green / -1 red), a letter where the volatility turns up with direction
    ("k" up, "m" down), and the macro trend (the close vs its `macroLen`-candle EMA: +1 / -1)."""
    import statistics
    n = p["sdLen"]
    raw = []
    for i in range(len(C)):
        w = [b[4] for b in C[max(0, i - n + 1):i + 1]]
        raw.append(statistics.pstdev(w) if len(w) >= 3 else None)
    vals = sorted(x for x in raw if x)
    med = vals[len(vals) // 2] if vals else 1
    e, out = None, []
    m = 2 / (p["macroLen"] + 1)
    for i, b in enumerate(C):
        e = b[4] if e is None else e + m * (b[4] - e)
        v = raw[i] / med * 0.35 if raw[i] and med else None
        d = 1 if i >= n and b[4] > C[i - n + 1][4] else -1 if i >= n and b[4] < C[i - n + 1][4] else 0
        out.append([round(v, 3) if v is not None else None, d, 1 if b[4] >= e else -1])
    for i in range(2, len(out)):                           # letters: the volatility turning up from a low (< 0.5) with a direction
        v0, v1, v2 = out[i - 2][0], out[i - 1][0], out[i][0]
        if None not in (v0, v1, v2) and v1 <= v0 and v2 > v1 * 1.15 and v1 < 0.5 and out[i][1]:
            out[i].append("k" if out[i][1] > 0 else "m")
    return out


def vector(C, p):
    out, e = [], None
    n = p["length"]
    for i, b in enumerate(C):
        if p["kind"] == "ema":
            e = b[4] if e is None else e + 2 / (n + 1) * (b[4] - e)
            out.append(e)
        else:
            w = C[max(0, i - n + 1):i + 1]
            out.append(sum(x[4] for x in w) / len(w))
    return out


def build(sym, q):
    p = params(q or {})
    C = candles(sym, p)
    if len(C) < p["length"] + 10:
        return None
    V = vector(C, p)
    cyc, side, start = [], 0, None
    for i in range(p["length"], len(C)):
        s = 1 if C[i][4] > V[i] else -1 if C[i][4] < V[i] else side
        if s != side:
            if side and start is not None:
                cyc.append((side, start, i - 1))
            side, start = s, i
    cur = (side, start, len(C) - 1) if side else None
    stats = {1: [], -1: []}
    marks, done = [], []
    prev_ext = None
    for k, (sd, a, b) in enumerate(cyc + ([cur] if cur else [])):
        seg = C[a:b + 1]
        # the base: the extreme of the cycle before (the reversion point), else this cycle's own start
        if k > 0:
            pa, pb = (cyc + [cur])[k - 1][1], (cyc + [cur])[k - 1][2]
            prev = C[pa:pb + 1]
            bi = (pa + min(range(len(prev)), key=lambda j: prev[j][3])) if sd > 0 else (pa + max(range(len(prev)), key=lambda j: prev[j][2]))
        else:
            bi = a
        base = C[bi][3] if sd > 0 else C[bi][2]
        ei = (a + max(range(len(seg)), key=lambda j: seg[j][2])) if sd > 0 else (a + min(range(len(seg)), key=lambda j: seg[j][3]))
        ext = (C[ei][2] - base) if sd > 0 else (base - C[ei][3])
        item = {"side": sd, "a": a, "b": b, "base": base, "bi": bi, "ei": ei, "ext": ext, "pct": ext / base * 100, "bars": b - a + 1, "toPeak": ei - bi}
        if k < len(cyc):
            if item["bars"] < p["minLen"]:
                continue                                # a cross back within minLen candles: noise, not a cycle
            stats[sd].append(item)
            marks.append({"t": C[ei][0], "p": C[ei][2] if sd > 0 else C[ei][3], "text": f"{'H' if sd > 0 else 'L'} {item['bars']}", "side": sd})
        else:
            cur = item
    out = {"sym": sym, "params": p, "candles": C[-p["show"]:], "vector": [round(v, 2) for v in V[-p["show"]:]], "off": max(0, len(C) - p["show"]),
           "marks": [m for m in marks if m["t"] >= C[max(0, len(C) - p["show"])][0]], "cycles": {}}
    for sd, key in ((1, "up"), (-1, "down")):
        xs = stats[sd]
        if not xs:
            continue
        exts = sorted(x["pct"] for x in xs)
        n = len(xs)
        out["cycles"][key] = {"n": n, "avgPct": sum(exts) / n, "avgBars": sum(x["bars"] for x in xs) / n, "longest": max(x["bars"] for x in xs),
                              "avgToPeak": sum(x["toPeak"] for x in xs) / n,
                              "bands": [{"p": b, "pct": exts[max(0, min(n - 1, int(round(n * (1 - b / 100)))))]} for b in BANDS]}
    if cur and out["cycles"].get("up" if cur["side"] > 0 else "down"):
        s = out["cycles"]["up" if cur["side"] > 0 else "down"]
        sd, base = cur["side"], cur["base"]
        out["current"] = {"side": sd, "base": round(base, 2), "baseT": C[cur["bi"]][0], "barsIn": cur["bars"], "extPct": round(cur["pct"], 3),
                          "bands": [{"p": b["p"], "price": round(base * (1 + sd * b["pct"] / 100), 2), "pct": round(b["pct"], 3)} for b in s["bands"]],
                          "amw": {"peak": round(base * (1 + sd * s["avgPct"] / 100), 2), "pct": round(s["avgPct"], 3), "bars": round(s["avgToPeak"], 1),
                                  "fromIdx": cur["bi"] - out["off"]}}
    out["sd"] = stddev(C, p)[-p["show"]:]
    return out
