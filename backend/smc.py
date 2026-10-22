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
          "longs": True, "shorts": True,
          # building blocks (switches + their numbers)
          "poolPD": True, "poolPM": True, "poolOR": True, "poolSwing": True, "eqOnly": False,       # which liquidity counts
          "mssOn": True, "dispOn": False, "dispMin": 1.0, "fvgOn": True,                            # structure
          "stopAt": "sweep", "vwapOn": False, "pdOn": False, "rrOn": False, "rrMin": 1.5, "maxDay": 0,   # entry filters
          "tpLiq": True, "tpR": 2.0, "partialOn": True, "beTp1": True,                              # targets
          "beOn": False, "beR": 1.0, "trailOn": False, "fvgFail": False, "mssX": False, "vwapX": False,   # early exits
          "timeOn": False, "timeBars": 12}
INTS = ("tf", "orMin", "pivot", "mssBars", "entryBars", "maxDay", "timeBars")
FLOATS = ("fvgMin", "stopBuf", "partial", "eqTol", "dispMin", "rrMin", "tpR", "beR")
BOOLS = ("longs", "shorts", "poolPD", "poolPM", "poolOR", "poolSwing", "eqOnly", "mssOn", "dispOn", "fvgOn", "vwapOn", "pdOn",
         "rrOn", "tpLiq", "partialOn", "beTp1", "beOn", "trailOn", "fvgFail", "mssX", "vwapX", "timeOn")


def clean(cfg):
    P = dict(PARAMS)
    for k, v in (cfg or {}).items():
        if k not in PARAMS:
            continue
        try:
            P[k] = int(v) if k in INTS else float(v) if k in FLOATS else (v in (True, 1, "1", "true", "on")) if k in BOOLS else str(v)
        except (TypeError, ValueError):
            pass
    P["tf"] = 1 if P["tf"] <= 1 else 5 if P["tf"] < 15 else 15
    P["pivot"] = max(1, min(5, P["pivot"]))
    if P["entryAt"] not in ("edge", "ce", "ob", "market"):
        P["entryAt"] = "edge"
    if P["stopAt"] not in ("sweep", "fvg"):
        P["stopAt"] = "sweep"
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


def day_pools(prev_reg, pm, P=None):
    """The day's starting pools: previous day high / low, pre-market high / low."""
    P = P or PARAMS
    out = []
    if prev_reg and P.get("poolPD", True):
        out += [{"name": "PDH", "p": max(b["h"] for b in prev_reg), "side": "buy"}, {"name": "PDL", "p": min(b["l"] for b in prev_reg), "side": "sell"}]
    if pm and P.get("poolPM", True):
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
        return {"bars": [], "levels": [], "sweeps": [], "mss": [], "fvgs": [], "obs": [], "setups": [], "gaps": [], "funnel": {}}
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
    out = {"levels": [], "sweeps": [], "mss": [], "fvgs": [], "obs": [], "setups": [], "gaps": []}
    for di, d in enumerate(days):
        if d not in show:
            continue
        ix = by[d]
        prev = [reg[i] for i in by.get(days[di - 1], [])] if di else []
        _day(reg, atr, ix, day_pools(prev, pm_by.get(d) or [], P), P, out)
        out["gaps"] += _gaps(reg, atr, ix, P)
    keep = set(show)
    out["bars"] = [{k: b[k] for k in ("t", "o", "h", "l", "c", "v")} for b in reg if b["t"][:10] in keep]
    out["interval"] = tf
    out["funnel"] = funnel(out["setups"])
    out["stats"] = stats(out["setups"])
    return out


def _gaps(bars, atr, ix, P):
    """Every fair value gap of the day (3-candle gap of fvgMin x ATR or more), setup or not, drawn until price fills
    it (trades through its far side) or the day ends."""
    out = []
    for q in range(2, len(ix)):
        j, j1, j2 = ix[q], ix[q - 1], ix[q - 2]
        for up in (True, False):
            lo_, hi_ = (bars[j2]["h"], bars[j]["l"]) if up else (bars[j]["h"], bars[j2]["l"])
            if hi_ - lo_ <= 0 or hi_ - lo_ < P["fvgMin"] * (atr[j1] or 0):
                continue
            t1 = bars[ix[-1]]["t"]
            for r in range(q + 1, len(ix)):
                c = bars[ix[r]]
                if (c["l"] <= lo_) if up else (c["h"] >= hi_):
                    t1 = c["t"]
                    break
            out.append({"t0": bars[j2]["t"], "t1": t1, "lo": round(lo_, 4), "hi": round(hi_, 4), "dir": 1 if up else -1})
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
    vw, pv, vv = {}, 0.0, 0.0                          # session VWAP (for the VWAP filter / exit)
    for j in ix:
        b = bars[j]
        v = float(b.get("v") or 0) or 1.0
        pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        vv += v
        vw[j] = pv / vv
    if or_end in ix and P["poolOR"]:
        seg = [bars[j] for j in ix if j <= or_end]
        pools += [{"name": "ORH", "p": max(b["h"] for b in seg), "side": "buy", "t0": bars[or_end]["t"], "born": or_end + 1, "swept": None},
                  {"name": "ORL", "p": min(b["l"] for b in seg), "side": "sell", "t0": bars[or_end]["t"], "born": or_end + 1, "swept": None}]
    swings = []
    for i, p, kind, conf in piv:                       # the day's swings become pools once confirmed
        if not P["poolSwing"]:
            break
        tol = P["eqTol"] * (atr[i] or 0)
        eq = any(q["side"] == ("buy" if kind == "high" else "sell") and abs(q["p"] - p) <= tol and q["born"] <= conf for q in swings)
        if eq:
            for q in swings:                           # both ends of an equal pair are the same pool
                if q["side"] == ("buy" if kind == "high" else "sell") and abs(q["p"] - p) <= tol:
                    q["name"] = "EQH" if kind == "high" else "EQL"
        swings.append({"name": ("EQH" if eq else "swing high") if kind == "high" else ("EQL" if eq else "swing low"), "p": p,
                       "side": "buy" if kind == "high" else "sell", "t0": bars[i]["t"], "born": conf + 1, "swept": None, "at": i})
    pools += [q for q in swings if not P["eqOnly"] or q["name"] in ("EQH", "EQL")]
    busy = [-1]                                        # one setup at a time (either side): nothing new until it is done
    n_day = 0
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
            if (side > 0 and not P["longs"]) or (side < 0 and not P["shorts"]) or i <= busy[0]:
                continue
            if not (P["startT"] <= hm < P["endT"]):
                continue
            if P["maxDay"] and n_day >= P["maxDay"]:
                continue
            s = _setup(bars, atr, ix, pos, sw_i, p, side, piv, pools, P, t_end, vw)
            if s.get("fillT"):
                n_day += 1
            out["sweeps"].append({"t": bars[sw_i]["t"], "p": p["p"], "name": p["name"], "dir": side})
            if s.get("mss"):
                out["mss"].append(s["mss"])
            if s.get("fvg"):
                out["fvgs"].append(s["fvg"])
            if s.get("ob"):
                out["obs"].append(s["ob"])
            out["setups"].append(s)
            busy[0] = s.get("_end", sw_i)
    for p in pools:
        out["levels"].append({"name": p["name"], "p": round(p["p"], 4), "side": p["side"], "t0": p["t0"],
                              "t1": bars[p["swept"]]["t"] if p["swept"] is not None else t_end, "swept": p["swept"] is not None})


def _setup(bars, atr, ix, pos, sw_i, pool, side, piv, pools, P, t_end, vw=None):
    f = lambda v: round(v, 4)
    vw = vw or {}
    up = side > 0
    beyond = lambda x, lvl: x > lvl if up else x < lvl          # past a level in the trade's direction
    sweep_bars = [bars[j] for j in ix if bars[ix[pos]]["t"] <= bars[j]["t"] <= bars[sw_i]["t"]]
    ext = min(x["l"] for x in sweep_bars) if up else max(x["h"] for x in sweep_bars)
    a = atr[sw_i] or 0
    s = {"dir": side, "side": "long" if up else "short", "t": bars[sw_i]["t"], "pool": pool["name"], "poolP": f(pool["p"]), "ext": f(ext), "steps": []}
    step = lambda n, ok, name, text: s["steps"].append({"n": n, "ok": ok, "name": name, "text": text})
    def stop_here(status, n, name, text, end=None):
        s["status"] = status
        step(n, False, name, text)
        if end is not None:
            s["_end"] = end
            if s.get("fvg"):                           # the gap is drawn until the setup gave up
                s["fvg"]["t1"] = bars[end]["t"]
        return s
    step(1, True, "Liquidity swept", f"{pool['name']} {pool['p']:.2f} taken at {bars[ix[pos]]['t'][11:16]} ({'low' if up else 'high'} {ext:.2f}), "
                                     f"closed back {'above' if up else 'under'} it")
    k0 = ix.index(sw_i)
    # 2. market structure shift
    want = "high" if up else "low"
    if P["mssOn"]:
        cand = [x for x in piv if x[2] == want and x[3] <= sw_i and x[0] < ix[pos]]
        if not cand:
            return stop_here("no structure", 2, "Market structure shift", f"no swing {want} before the sweep to break")
        lvl_i, lvl, _, _ = cand[-1]
        mss_j = None
        for q in range(k0 + 1, min(len(ix), k0 + 1 + P["mssBars"])):
            c = bars[ix[q]]
            if (c["l"] < ext) if up else (c["h"] > ext):
                return stop_here("new extreme", 2, "Market structure shift",
                                 f"price made a new {'low' if up else 'high'} at {c['t'][11:16]} before breaking the swing {want} {lvl:.2f}", ix[q])
            if beyond(c["c"], lvl):
                mss_j = ix[q]
                break
        if mss_j is None:
            return stop_here("no MSS", 2, "Market structure shift",
                             f"no close {'above' if up else 'under'} the swing {want} {lvl:.2f} within {P['mssBars']} candles", ix[min(len(ix) - 1, k0 + P["mssBars"])])
        s["mss"] = {"t": bars[mss_j]["t"], "p": f(lvl), "t0": bars[lvl_i]["t"], "dir": side}
        step(2, True, "Market structure shift", f"close {bars[mss_j]['c']:.2f} {'above' if up else 'under'} the swing {want} {lvl:.2f} at "
                                                f"{bars[mss_j]['t'][11:16]} ({ix.index(mss_j) - k0} candles after the sweep)")
        leg_end = ix.index(mss_j) + 1
    else:
        mss_j = sw_i
        leg_end = min(len(ix) - 1, k0 + P["mssBars"])
        step(2, True, "Market structure shift", "not required (block off)")
    # displacement: one candle of the leg with a body of dispMin x ATR in the trade's direction
    if P["dispOn"]:
        big = [ix[q] for q in range(k0 + 1, min(len(ix), leg_end + 1))
               if (bars[ix[q]]["c"] - bars[ix[q]]["o"]) * side >= P["dispMin"] * (atr[ix[q]] or 0)]
        if not big:
            return stop_here("no displacement", 3, "Fair value gap", f"no candle of the move with a body of {P['dispMin']:g} ATR or more", mss_j)
    # 3. FVG of the leg
    best = None
    for q in range(k0 + 2, min(len(ix) - 1, leg_end) + 1):
        j, j2 = ix[q], ix[q - 2]
        lo_, hi_ = (bars[j2]["h"], bars[j]["l"]) if up else (bars[j]["h"], bars[j2]["l"])
        size = hi_ - lo_
        if size > 0 and size >= P["fvgMin"] * (atr[ix[q - 1]] or 0) and (best is None or size > best[2]):
            best = (j2, j, size, lo_, hi_)
    k_q = ix.index(mss_j)                                # the candle where the setup is known (the MSS, or the gap's 3rd candle)
    if best:
        j2, j, size, glo, ghi = best
        k_q = max(k_q, ix.index(j))
        s["fvg"] = {"t0": bars[j2]["t"], "t1": t_end, "lo": f(glo), "hi": f(ghi), "dir": side}
        step(3, True, "Fair value gap", f"{'bullish' if up else 'bearish'} FVG {glo:.2f}–{ghi:.2f}" + (f" ({size / a:.2f} ATR)" if a else "")
                                        + f" at {bars[ix[ix.index(j) - 1]]['t'][11:16]}")
    elif P["fvgOn"]:
        return stop_here("no FVG", 3, "Fair value gap", f"the move left no gap of {P['fvgMin']:g} ATR or more: no displacement", mss_j)
    else:
        glo = ghi = None
        step(3, True, "Fair value gap", "none, not required (block off)")
    # order block: the last opposite candle before the leg
    ob_from = ix.index(best[0]) if best else ix.index(mss_j)
    for q in range(ob_from, k0 - 1, -1):
        c = bars[ix[q]]
        if (c["c"] < c["o"]) if up else (c["c"] > c["o"]):
            s["ob"] = {"t0": c["t"], "lo": f(c["l"]), "hi": f(c["h"]), "dir": side}
            break
    # entry price
    how = P["entryAt"]
    if how in ("edge", "ce") and glo is None:
        how = "ob" if s.get("ob") else "market"
    if how == "ob" and not s.get("ob"):
        how = "edge" if glo is not None else "market"
    if how == "edge":
        entry, where = (ghi if up else glo), "the FVG edge"
    elif how == "ce":
        entry, where = (glo + ghi) / 2, "the middle of the FVG"
    elif how == "ob":
        entry, where = (s["ob"]["hi"] if up else s["ob"]["lo"]), "the order block"
    else:
        entry, where = bars[ix[k_q]]["c"], "the close of the MSS candle" if ix[k_q] == mss_j else "the close of the candle that completed the FVG"
    stop = (ext - side * P["stopBuf"] * a) if P["stopAt"] == "sweep" or glo is None else ((glo if up else ghi) - side * P["stopBuf"] * a)
    risk = (entry - stop) * side
    if risk <= 0:
        return stop_here("bad risk", 4, "Retracement", "the stop would be on the wrong side of the entry", mss_j)
    # targets
    opp = "buy" if up else "sell"
    targets = sorted((p for p in pools if p["side"] == opp and p["swept"] is None and (p["p"] - entry) * side >= risk and p["born"] <= mss_j),
                     key=lambda p: (p["p"] - entry) * side) if P["tpLiq"] else []
    if targets:
        tp1 = (targets[0]["p"], targets[0]["name"])
        tp2 = (targets[1]["p"], targets[1]["name"]) if len(targets) > 1 else (entry + side * max(2 * risk, 1.5 * (targets[0]["p"] - entry) * side), "extension")
    else:
        tp1 = (entry + side * P["tpR"] * risk, f"{P['tpR']:g}R" + (" (no liquidity)" if P["tpLiq"] else ""))
        tp2 = (entry + side * P["tpR"] * 1.5 * risk, f"{P['tpR'] * 1.5:g}R")
    r_ = lambda px: (px - entry) * side / risk
    s.update(entry=f(entry), stop=f(stop), tp1=f(tp1[0]), tp1Name=tp1[1], tp2=f(tp2[0]), tp2Name=tp2[1])
    if P["rrOn"] and r_(tp1[0]) < P["rrMin"]:
        return stop_here("RR too small", 4, "Retracement", f"TP1 {tp1[1]} is only {r_(tp1[0]):.1f}R away (< {P['rrMin']:g}R)", mss_j)
    # 4. the retracement (or the market entry)
    m_q = k_q                                          # no fill before the gap exists (its 3rd candle closed)
    fill = None
    if how == "market":
        fill = m_q
    else:
        for q in range(m_q + 1, min(len(ix), m_q + 1 + P["entryBars"])):
            c = bars[ix[q]]
            if _hm(c["t"]) >= P["endT"]:
                break
            if beyond(c["h"] if up else c["l"], tp1[0]) or (c["h"] if up else c["l"]) == tp1[0]:
                return stop_here("ran away", 4, "Retracement", f"price reached {tp1[1]} at {c['t'][11:16]} without coming back to {where}", ix[q])
            if (c["l"] <= entry) if up else (c["h"] >= entry):
                fill = q
                break
        if fill is None:
            return stop_here("no retrace", 4, "Retracement", f"no return to {where} ({entry:.2f}) within {P['entryBars']} candles",
                             ix[min(len(ix) - 1, m_q + P["entryBars"])])
    fj = ix[fill]
    c = bars[fj]
    # entry filters, checked at the fill
    if P["vwapOn"] and not beyond(entry, vw.get(fj, entry)):
        return stop_here("VWAP side", 4, "Retracement", f"entry {entry:.2f} {'under' if up else 'above'} the session VWAP {vw.get(fj, 0):.2f}", fj)
    if P["pdOn"]:
        seen = [bars[j] for j in ix if j <= fj]
        mid = (max(x["h"] for x in seen) + min(x["l"] for x in seen)) / 2
        if (entry > mid) if up else (entry < mid):
            return stop_here("premium / discount", 4, "Retracement",
                             f"entry {entry:.2f} in the {'premium' if up else 'discount'} half of the day's range (middle {mid:.2f})", fj)
    s["fillT"] = c["t"]
    step(4, True, "Retracement", f"filled at {entry:.2f} ({where}) at {c['t'][11:16]}")
    exits = [n_ for k_, n_ in (("beOn", f"breakeven at {P['beR']:g}R"), ("trailOn", "trail under the new swings"), ("fvgFail", "close through the FVG"),
                               ("mssX", "opposite structure shift"), ("vwapX", "close back across VWAP"), ("timeOn", f"time stop {P['timeBars']} candles")) if P[k_]]
    part = P["partial"] if P["partialOn"] else 0.0
    step(5, True, "Risk & targets", f"stop {stop:.2f} ({'beyond the sweep' if P['stopAt'] == 'sweep' or glo is None else 'beyond the FVG'}, {risk:.2f} = 1R) · "
                                    f"TP1 {tp1[1]} {tp1[0]:.2f} ({r_(tp1[0]):.1f}R{f', {round(part * 100)}%' if part else ', all'})"
                                    + (f" · TP2 {tp2[1]} {tp2[0]:.2f} ({r_(tp2[0]):.1f}R)" if part else "")
                                    + (f" · early exits: {', '.join(exits)}" if exits else ""))
    # 5. the trade
    final = tp2[0] if part else tp1[0]
    st, half, res, ex_t, why = stop, None, None, None, ""
    best_px = entry
    sw_after = lambda q: [x for x in piv if x[2] == ("low" if up else "high") and x[0] > fj and x[3] <= ix[q]]
    for q in range(fill + 1 if how == "market" else fill, len(ix)):   # market: in at the close, the trade starts on the next candle
        c = bars[ix[q]]
        blend = lambda px: (part * half + (1 - part) * r_(px)) if half is not None else r_(px)
        if (c["l"] <= st) if up else (c["h"] >= st):
            res, ex_t, why = blend(st), c["t"], "breakeven stop" if st == entry else "trailing stop" if (st - stop) * side > 0 else "stop"
            break
        if q == fill and how != "market":              # the fill candle: its high (long) may come before the fill -> only its close counts
            best_px = max(best_px, c["c"]) if up else min(best_px, c["c"])
            if _hm(c["t"]) < P["flatT"] and q < len(ix) - 1:
                continue
        best_px = max(best_px, c["h"]) if up else min(best_px, c["l"])
        if half is None and part and beyond(c["h"] if up else c["l"], tp1[0]) or (half is None and part and (c["h"] if up else c["l"]) == tp1[0]):
            half = r_(tp1[0])                          # partial at TP1
            if P["beTp1"]:
                st = entry if (entry - st) * side > 0 else st
        if beyond(c["h"] if up else c["l"], final) or (c["h"] if up else c["l"]) == final:
            res, ex_t, why = blend(final), c["t"], "TP2" if part else "TP1"
            break
        if P["beOn"] and (best_px - entry) * side >= P["beR"] * risk and (entry - st) * side > 0:
            st = entry
        if P["trailOn"] and (half is not None or (best_px - entry) * side >= risk):
            sw = sw_after(q)
            if sw:
                lvl = sw[-1][1] - side * P["stopBuf"] * (atr[ix[q]] or 0)
                if (lvl - st) * side > 0:
                    st = lvl
        early = None                                   # early exits, at the close of the candle
        if P["fvgFail"] and glo is not None and ((c["c"] < glo) if up else (c["c"] > ghi)):
            early = "closed through the FVG"
        elif P["mssX"] and sw_after(q) and ((c["c"] < sw_after(q)[-1][1]) if up else (c["c"] > sw_after(q)[-1][1])):
            early = f"opposite structure shift (close {'under' if up else 'above'} {sw_after(q)[-1][1]:.2f})"
        elif P["vwapX"] and q > fill and not beyond(c["c"], vw.get(ix[q], c["c"])):
            early = "closed back across VWAP"
        elif P["timeOn"] and half is None and q - fill >= P["timeBars"]:
            early = f"time stop ({P['timeBars']} candles without TP1)"
        if early:
            res, ex_t, why = blend(c["c"]), c["t"], early
            break
        if _hm(c["t"]) >= P["flatT"] or q == len(ix) - 1:
            res, ex_t, why = blend(c["c"]), c["t"], "end of day"
            break
    if res is None:                                    # filled on the last candle of the day
        res, ex_t, why = r_(bars[ix[-1]]["c"]), bars[ix[-1]]["t"], "end of day"
    s["R"], s["exitT"], s["exitWhy"] = round(res, 3), ex_t, why
    s["status"] = "win" if res > 0.05 else "loss" if res < -0.05 else "flat"
    step(6, res > 0, "Result", f"{'TP1 hit (partial), ' if half is not None else ''}out at {ex_t[11:16]} ({why}): {res:+.2f}R")
    s["_end"] = next((j for j in ix if bars[j]["t"] == ex_t), ix[-1])
    return s


def funnel(setups):
    n = len(setups)
    mss = sum(1 for s in setups if s.get("mss"))
    fvg = sum(1 for s in setups if s.get("fvg"))
    fill = sum(1 for s in setups if s.get("fillT"))
    win = sum(1 for s in setups if s.get("status") == "win")
    return {"sweeps": n, "mss": mss, "fvg": fvg, "filled": fill, "wins": win,
            "why": {k: sum(1 for s in setups if s.get("status") == k) for k in ("no structure", "new extreme", "no MSS", "no displacement", "no FVG", "RR too small",
                                                                              "ran away", "no retrace", "VWAP side", "premium / discount", "bad risk")}}


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


NUDGE = ("fvgMin", "mssBars", "entryBars", "stopBuf", "partial", "pivot", "orMin", "tpR", "rrMin", "dispMin", "beR", "timeBars")


def variants(P):
    """The stress versions of a setting: each block switched the other way, each number -20% / +20%, the other candle
    size, the other entry / stop. -> [(label, cfg)]"""
    out = []
    for k in BOOLS:
        if k in ("longs", "shorts"):
            continue
        out.append((f"{k} {'off' if P[k] else 'on'}", {**P, k: not P[k]}))
    used_by = {"dispMin": "dispOn", "rrMin": "rrOn", "beR": "beOn", "timeBars": "timeOn", "partial": "partialOn", "orMin": "poolOR"}
    for k in NUDGE:
        if k in used_by and not P[used_by[k]]:
            continue                                   # a number of a block that is off changes nothing
        for f in (0.8, 1.2):
            v = P[k] * f
            v = max(1, int(round(v))) if k in INTS else round(v, 4)
            if v != P[k]:
                out.append((f"{k} {P[k]:g} → {v:g}", {**P, k: v}))
    out.append((f"{'5m' if P['tf'] == 1 else '1m'} candles", {**P, "tf": 5 if P["tf"] == 1 else 1}))
    for e in ("edge", "ce", "ob", "market"):
        if e != P["entryAt"]:
            out.append((f"entry at {e}", {**P, "entryAt": e}))
    out.append((f"stop beyond the {'FVG' if P['stopAt'] == 'sweep' else 'sweep'}", {**P, "stopAt": "fvg" if P["stopAt"] == "sweep" else "sweep"}))
    return out


def _sum(setups, cost):
    rs = [s["R"] - cost for s in setups if s.get("R") is not None]
    return {"n": len(rs), "totR": round(sum(rs), 3), "avgR": round(sum(rs) / len(rs), 4) if rs else None}


def stress(sub, sym, days=30, cfg=None, cost=0.05):
    """One ticker, the last `days` days: the trades of the setting (after the cost) and the stress versions."""
    import daytrade
    sym = (sym or "").upper()
    P = clean(cfg)
    days = max(5, min(30, int(days or 30)))
    mb = daytrade.minute_bars(sub, sym, days + 2)
    dates = sorted({b["t"][:10] for b in mb if "09:30" <= b["t"][11:16] < "16:00"})[-days:]
    if not dates:
        return {"symbol": sym, "error": "No minute candles."}
    base = detect(mb, P, dates)
    tr = [{"t": s["fillT"], "R": round(s["R"] - cost, 3), "dir": s["dir"], "why": s.get("exitWhy")} for s in base["setups"] if s.get("R") is not None]
    vs = []
    for label, c in variants(P):
        try:
            vs.append({"label": label, **_sum(detect(mb, c, dates)["setups"], cost)})
        except Exception as e:
            vs.append({"label": label, "error": str(e)[:80]})
    return {"symbol": sym, "days": dates, "cost": cost, "trades": tr, "base": _sum(base["setups"], cost), "funnel": base["funnel"], "variants": vs}
