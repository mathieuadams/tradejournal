"""Smart-money day model (ICT style), one ticker, intraday candles, every step checked and written down:

  1. Liquidity  : the pools resting outside the price: previous day high / low, pre-market high / low, opening range
                  high / low (once the range is done) and the day's confirmed swing highs / lows (two within 0.1 ATR =
                  equal highs / lows, a stronger pool).
  2. Sweep      : a candle trades through a pool and closes back on the other side (or the next candle does): the
                  liquidity is taken (sell-side under the price for a long, buy-side above for a short).
  3. MSS        : within `mssBars` candles, a close beyond the last swing high (long) / low (short) that formed before
                  the sweep: the market structure shifts. A new extreme past the sweep first cancels it.
  4. FVG        : the displacement leg leaves a fair value gap of at least `fvgMin` x ATR (3-candle gap). Without one
                  the move is not displacement: no trade.
  5. Retrace    : within `entryBars` candles after the MSS, price comes back into the FVG: the limit entry fills at its
                  edge (or its middle, entryAt = "ce"). Price reaching the first target first, or a close through the
                  far side of the gap, cancels it.
  6. Risk       : stop beyond the sweep extreme (+ stopBuf x ATR); TP1 = the next opposite pool at least 1R away
                  (partial there, the rest to breakeven), TP2 = the pool after it (else 2R / 3R); out at flatT.

detect() returns every candidate with the step where it stopped, so the funnel (how many sweeps make an MSS, an FVG,
a fill, a winner) shows whether the rules read the chart the way a trader would."""

PARAMS = {"tf": 5, "orMin": 15, "pivot": 2, "fvgMin": 0.3, "mssBars": 12, "entryBars": 24, "entryAt": "edge",
          "stopBuf": 0.1, "partial": 0.5, "startT": "09:45", "endT": "15:00", "flatT": "15:55", "eqTol": 0.1,
          "longs": True, "shorts": True}
INTS = ("tf", "orMin", "pivot", "mssBars", "entryBars")
FLOATS = ("fvgMin", "stopBuf", "partial", "eqTol")


def clean(cfg):
    P = dict(PARAMS)
    for k, v in (cfg or {}).items():
        if k not in PARAMS:
            continue
        try:
            P[k] = int(v) if k in INTS else float(v) if k in FLOATS else bool(v) if k in ("longs", "shorts") else str(v)
        except (TypeError, ValueError):
            pass
    P["tf"] = 1 if P["tf"] <= 1 else 5 if P["tf"] < 15 else 15
    P["pivot"] = max(1, min(5, P["pivot"]))
    return P


def _atr(bars, n=14):
    out, a = [], None
    for i, b in enumerate(bars):
        tr = b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"]))
        a = tr if a is None else a + (tr - a) / n
        out.append(a)
    return out


def _hm(t):
    return t[11:16]


def day_pools(prev_reg, pm):
    """The day's starting pools: previous day high / low, pre-market high / low."""
    out = []
    if prev_reg:
        out += [{"name": "PDH", "p": max(b["h"] for b in prev_reg), "side": "buy"}, {"name": "PDL", "p": min(b["l"] for b in prev_reg), "side": "sell"}]
    if pm:
        out += [{"name": "PM high", "p": max(b["h"] for b in pm), "side": "buy"}, {"name": "PM low", "p": min(b["l"] for b in pm), "side": "sell"}]
    return out


def detect(minute_bars, P=None, show=None):
    """minute_bars: 1-minute candles (pre-market included) of several days. -> {"bars", "levels", "sweeps", "mss",
    "fvgs", "obs", "setups", "funnel"} for the days in `show` (default: all)."""
    import daytrade
    P = clean(P)
    tf = P["tf"]
    allb = daytrade.agg_bars(minute_bars, tf) if tf > 1 else list(minute_bars)
    reg = [b for b in allb if "09:30" <= _hm(b["t"]) < "16:00"]
    if not reg:
        return {"bars": [], "levels": [], "sweeps": [], "mss": [], "fvgs": [], "obs": [], "setups": [], "funnel": {}}
    atr = _atr(reg)
    days = sorted({b["t"][:10] for b in reg})
    show = [d for d in days if not show or d in show]
    pm_by = {}
    for b in minute_bars:
        if "04:00" <= _hm(b["t"]) < "09:30":
            pm_by.setdefault(b["t"][:10], []).append(b)
    by = {}
    for i, b in enumerate(reg):
        by.setdefault(b["t"][:10], []).append(i)
    out = {"levels": [], "sweeps": [], "mss": [], "fvgs": [], "obs": [], "setups": []}
    for di, d in enumerate(days):
        if d not in show:
            continue
        ix = by[d]
        prev = [reg[i] for i in by.get(days[di - 1], [])] if di else []
        _day(reg, atr, ix, day_pools(prev, pm_by.get(d) or []), P, out)
    keep = set(show)
    out["bars"] = [{k: b[k] for k in ("t", "o", "h", "l", "c", "v")} for b in reg if b["t"][:10] in keep]
    out["interval"] = tf
    out["funnel"] = funnel(out["setups"])
    out["stats"] = stats(out["setups"])
    return out


def _pivots(bars, ix, k):
    """Confirmed swings of the day: [(index, price, "high" / "low", confirmed at index)]."""
    out = []
    for n_, i in enumerate(ix):
        if n_ < k or n_ + k >= len(ix):
            continue
        win = [bars[j] for j in ix[n_ - k:n_ + k + 1]]
        b = bars[i]
        if b["h"] >= max(x["h"] for x in win) and sum(1 for x in win if x["h"] == b["h"]) == 1:
            out.append((i, b["h"], "high", ix[n_ + k]))
        if b["l"] <= min(x["l"] for x in win) and sum(1 for x in win if x["l"] == b["l"]) == 1:
            out.append((i, b["l"], "low", ix[n_ + k]))
    return out


def _day(bars, atr, ix, pools0, P, out):
    k = P["pivot"]
    t0, t_end = bars[ix[0]]["t"], bars[ix[-1]]["t"]
    piv = _pivots(bars, ix, k)
    or_end = ix[0] + max(1, P["orMin"] // P["tf"]) - 1
    pools = [dict(p, t0=t0, born=ix[0], swept=None) for p in pools0]
    if or_end in ix:
        seg = [bars[j] for j in ix if j <= or_end]
        pools += [{"name": "ORH", "p": max(b["h"] for b in seg), "side": "buy", "t0": bars[or_end]["t"], "born": or_end + 1, "swept": None},
                  {"name": "ORL", "p": min(b["l"] for b in seg), "side": "sell", "t0": bars[or_end]["t"], "born": or_end + 1, "swept": None}]
    for i, p, kind, conf in piv:                       # the day's swings become pools once confirmed
        tol = P["eqTol"] * (atr[i] or 0)
        eq = any(q["name"].startswith("swing") and q["side"] == ("buy" if kind == "high" else "sell") and abs(q["p"] - p) <= tol and q["born"] <= conf
                 for q in pools)
        pools.append({"name": ("EQH" if eq else "swing high") if kind == "high" else ("EQL" if eq else "swing low"), "p": p,
                      "side": "buy" if kind == "high" else "sell", "t0": bars[i]["t"], "born": conf + 1, "swept": None, "at": i})
    busy_until = {1: -1, -1: -1}                       # one setup at a time per side
    n = len(ix)
    for pos in range(n):
        i = ix[pos]
        b = bars[i]
        hm = _hm(b["t"])
        for p in pools:
            if p["swept"] is not None or p["born"] > i:
                continue
            side = 1 if p["side"] == "sell" else -1     # sell-side taken -> a long; buy-side taken -> a short
            through = b["l"] < p["p"] if side > 0 else b["h"] > p["p"]
            if not through:
                continue
            p["swept"] = i
            back = (b["c"] > p["p"]) if side > 0 else (b["c"] < p["p"])
            sw_i = i
            if not back and pos + 1 < n:              # the next candle closes back: a 2-candle sweep
                nb = bars[ix[pos + 1]]
                back = (nb["c"] > p["p"]) if side > 0 else (nb["c"] < p["p"])
                sw_i = ix[pos + 1]
            if not back:
                continue                               # broke through and stayed: not a sweep
            if (side > 0 and not P["longs"]) or (side < 0 and not P["shorts"]) or i <= busy_until[side]:
                continue
            if not (P["startT"] <= hm < P["endT"]):
                continue
            s = _setup(bars, atr, ix, pos, sw_i, p, side, piv, pools, P, t_end)
            out["sweeps"].append({"t": bars[sw_i]["t"], "p": p["p"], "name": p["name"], "dir": side})
            if s.get("mss"):
                out["mss"].append(s["mss"])
            if s.get("fvg"):
                out["fvgs"].append(s["fvg"])
            if s.get("ob"):
                out["obs"].append(s["ob"])
            out["setups"].append(s)
            busy_until[side] = s.get("_end", sw_i)
    for p in pools:
        out["levels"].append({"name": p["name"], "p": round(p["p"], 4), "side": p["side"], "t0": p["t0"],
                              "t1": bars[p["swept"]]["t"] if p["swept"] is not None else t_end, "swept": p["swept"] is not None})


def _setup(bars, atr, ix, pos, sw_i, pool, side, piv, pools, P, t_end):
    f = lambda v: round(v, 4)
    sweep_bars = [bars[j] for j in ix if bars[ix[pos]]["t"] <= bars[j]["t"] <= bars[sw_i]["t"]]
    ext = min(x["l"] for x in sweep_bars) if side > 0 else max(x["h"] for x in sweep_bars)
    a = atr[sw_i] or 0
    word = "long" if side > 0 else "short"
    s = {"dir": side, "side": word, "t": bars[sw_i]["t"], "pool": pool["name"], "poolP": f(pool["p"]), "ext": f(ext), "steps": []}
    s["steps"].append({"n": 1, "ok": True, "name": "Liquidity swept",
                       "text": f"{pool['name']} {pool['p']:.2f} taken at {bars[ix[pos]]['t'][11:16]} ({'low' if side > 0 else 'high'} {ext:.2f}), "
                               f"closed back {'above' if side > 0 else 'under'} it"})
    # 3. MSS: the last swing opposite to the sweep, formed before it
    want = "high" if side > 0 else "low"
    cand = [x for x in piv if x[2] == want and x[3] <= sw_i and x[0] < ix[pos]]
    if not cand:
        s["status"] = "no structure"
        s["steps"].append({"n": 2, "ok": False, "name": "Market structure shift", "text": f"no swing {want} before the sweep to break"})
        return s
    lvl_i, lvl, _, _ = cand[-1]
    k0 = ix.index(sw_i)
    mss_j = None
    for q in range(k0 + 1, min(len(ix), k0 + 1 + P["mssBars"])):
        j = ix[q]
        c = bars[j]
        if (c["l"] < ext) if side > 0 else (c["h"] > ext):
            s["status"] = "new extreme"
            s["steps"].append({"n": 2, "ok": False, "name": "Market structure shift",
                               "text": f"price made a new {'low' if side > 0 else 'high'} at {c['t'][11:16]} before breaking the swing {want} {lvl:.2f}"})
            s["_end"] = j
            return s
        if (c["c"] > lvl) if side > 0 else (c["c"] < lvl):
            mss_j = j
            break
    if mss_j is None:
        s["status"] = "no MSS"
        s["steps"].append({"n": 2, "ok": False, "name": "Market structure shift",
                           "text": f"no close {'above' if side > 0 else 'under'} the swing {want} {lvl:.2f} within {P['mssBars']} candles"})
        s["_end"] = ix[min(len(ix) - 1, k0 + P["mssBars"])]
        return s
    s["mss"] = {"t": bars[mss_j]["t"], "p": f(lvl), "t0": bars[lvl_i]["t"], "dir": side}
    s["steps"].append({"n": 2, "ok": True, "name": "Market structure shift",
                       "text": f"close {bars[mss_j]['c']:.2f} {'above' if side > 0 else 'under'} the swing {want} {lvl:.2f} at {bars[mss_j]['t'][11:16]} "
                               f"({ix.index(mss_j) - k0} candles after the sweep)"})
    # 4. FVG of the displacement leg (sweep -> MSS, and the candle after)
    best = None
    q_end = min(len(ix) - 1, ix.index(mss_j) + 1)
    for q in range(k0 + 2, q_end + 1):
        j, j2 = ix[q], ix[q - 2]
        lo_, hi_ = (bars[j2]["h"], bars[j]["l"]) if side > 0 else (bars[j]["h"], bars[j2]["l"])
        size = hi_ - lo_
        if size > 0 and size >= P["fvgMin"] * (atr[ix[q - 1]] or 0) and (best is None or size > best[2]):
            best = (j2, j, size, lo_, hi_)
    if not best:
        s["status"] = "no FVG"
        s["steps"].append({"n": 3, "ok": False, "name": "Fair value gap",
                           "text": f"the move to the MSS left no gap of {P['fvgMin']:g} ATR or more: no displacement"})
        s["_end"] = mss_j
        return s
    j2, j, size, glo, ghi = best
    s["fvg"] = {"t0": bars[j2]["t"], "t1": t_end, "lo": f(glo), "hi": f(ghi), "dir": side}
    s["steps"].append({"n": 3, "ok": True, "name": "Fair value gap",
                       "text": f"{'bullish' if side > 0 else 'bearish'} FVG {glo:.2f}–{ghi:.2f} ({size / a:.2f} ATR) at {bars[ix[ix.index(j) - 1]]['t'][11:16]}" if a else
                               f"FVG {glo:.2f}–{ghi:.2f}"})
    # order block: the last opposite candle before the leg (shown, not required)
    for q in range(ix.index(j2), k0 - 1, -1):
        c = bars[ix[q]]
        if (c["c"] < c["o"]) if side > 0 else (c["c"] > c["o"]):
            s["ob"] = {"t0": c["t"], "lo": f(c["l"]), "hi": f(c["h"]), "dir": side}
            break
    # 6 (needed for 5): risk and the targets
    entry = (ghi if side > 0 else glo) if P["entryAt"] != "ce" else (glo + ghi) / 2
    stop = ext - side * P["stopBuf"] * a
    risk = (entry - stop) * side
    if risk <= 0:
        s["status"] = "bad risk"
        s["steps"].append({"n": 4, "ok": False, "name": "Retracement", "text": "the stop would be on the wrong side of the entry"})
        s["_end"] = mss_j
        return s
    opp = "buy" if side > 0 else "sell"
    targets = sorted((p for p in pools if p["side"] == opp and p["swept"] is None and (p["p"] - entry) * side >= risk and p["born"] <= mss_j),
                     key=lambda p: (p["p"] - entry) * side)
    if targets:
        tp1 = (targets[0]["p"], targets[0]["name"])
        tp2 = (targets[1]["p"], targets[1]["name"]) if len(targets) > 1 else (entry + side * max(2 * risk, 1.5 * (targets[0]["p"] - entry) * side), "extension")
    else:
        tp1, tp2 = (entry + side * 2 * risk, "2R (no liquidity)"), (entry + side * 3 * risk, "3R")
    s.update(entry=f(entry), stop=f(stop), tp1=f(tp1[0]), tp1Name=tp1[1], tp2=f(tp2[0]), tp2Name=tp2[1])
    r_ = lambda px: (px - entry) * side / risk
    # 5. the retracement into the gap
    m_q = ix.index(mss_j)
    fill = None
    for q in range(m_q + 1, min(len(ix), m_q + 1 + P["entryBars"])):
        c = bars[ix[q]]
        if _hm(c["t"]) >= P["endT"]:
            break
        if (c["h"] >= tp1[0]) if side > 0 else (c["l"] <= tp1[0]):
            s["status"] = "ran away"
            s["steps"].append({"n": 4, "ok": False, "name": "Retracement", "text": f"price reached {tp1[1]} at {c['t'][11:16]} without coming back to the FVG"})
            s["_end"] = ix[q]
            return s
        if (c["l"] <= entry) if side > 0 else (c["h"] >= entry):
            fill = q
            break
    if fill is None:
        s["status"] = "no retrace"
        s["steps"].append({"n": 4, "ok": False, "name": "Retracement", "text": f"no return to the FVG ({entry:.2f}) within {P['entryBars']} candles"})
        s["_end"] = ix[min(len(ix) - 1, m_q + P["entryBars"])]
        return s
    c = bars[ix[fill]]
    s["fillT"] = c["t"]
    s["steps"].append({"n": 4, "ok": True, "name": "Retracement", "text": f"back into the FVG at {c['t'][11:16]}: filled at {entry:.2f}"})
    s["steps"].append({"n": 5, "ok": True, "name": "Risk & targets",
                       "text": f"stop {stop:.2f} beyond the sweep ({risk:.2f} = 1R) · TP1 {tp1[1]} {tp1[0]:.2f} ({r_(tp1[0]):.1f}R, {round(P['partial'] * 100)}%) · "
                               f"TP2 {tp2[1]} {tp2[0]:.2f} ({r_(tp2[0]):.1f}R)"})
    # the trade
    st, half, res, ex_t = stop, None, None, None
    for q in range(fill, len(ix)):
        c = bars[ix[q]]
        if (c["l"] <= st) if side > 0 else (c["h"] >= st):
            res, ex_t = (P["partial"] * half + (1 - P["partial"]) * r_(st)) if half is not None else r_(st), c["t"]
            break
        if half is None and ((c["h"] >= tp1[0]) if side > 0 else (c["l"] <= tp1[0])):
            half, st = r_(tp1[0]), entry                # partial at TP1, the rest to breakeven
        if half is not None and ((c["h"] >= tp2[0]) if side > 0 else (c["l"] <= tp2[0])):
            res, ex_t = P["partial"] * half + (1 - P["partial"]) * r_(tp2[0]), c["t"]
            break
        if _hm(c["t"]) >= P["flatT"] or q == len(ix) - 1:
            res, ex_t = (P["partial"] * half + (1 - P["partial"]) * r_(c["c"])) if half is not None else r_(c["c"]), c["t"]
            break
    s["R"], s["exitT"] = round(res, 3), ex_t
    s["status"] = "win" if res > 0.05 else "loss" if res < -0.05 else "flat"
    s["steps"].append({"n": 6, "ok": res > 0, "name": "Result",
                       "text": f"{'TP1 hit (partial), ' if half is not None else ''}out at {ex_t[11:16]}: {res:+.2f}R"})
    s["_end"] = next((j for j in ix if bars[j]["t"] == ex_t), ix[-1])
    return s


def funnel(setups):
    n = len(setups)
    mss = sum(1 for s in setups if s.get("mss"))
    fvg = sum(1 for s in setups if s.get("fvg"))
    fill = sum(1 for s in setups if s.get("fillT"))
    win = sum(1 for s in setups if s.get("status") == "win")
    return {"sweeps": n, "mss": mss, "fvg": fvg, "filled": fill, "wins": win,
            "why": {k: sum(1 for s in setups if s.get("status") == k) for k in ("no structure", "new extreme", "no MSS", "no FVG", "ran away", "no retrace", "bad risk")}}


def stats(setups):
    rs = [s["R"] for s in setups if s.get("R") is not None]
    if not rs:
        return {"n": 0}
    w = [r for r in rs if r > 0]
    l_ = [r for r in rs if r <= 0]
    return {"n": len(rs), "win": round(len(w) / len(rs) * 100), "avgR": round(sum(rs) / len(rs), 3), "totR": round(sum(rs), 2),
            "pf": round(sum(w) / -sum(l_), 2) if l_ and sum(l_) < 0 else None}


def run(sub, sym, days=5, cfg=None):
    import daytrade
    sym = (sym or "QQQ").upper()
    P = clean(cfg)
    days = max(1, min(30, int(days or 5)))
    mb = daytrade.minute_bars(sub, sym, days + 2)
    if not mb:
        return {"symbol": sym, "bars": [], "error": "No minute candles."}
    dates = sorted({b["t"][:10] for b in mb if "09:30" <= b["t"][11:16] < "16:00"})
    out = detect(mb, P, dates[-days:])
    for s in out["setups"]:
        s.pop("_end", None)
    return {"symbol": sym, "params": P, **out}
