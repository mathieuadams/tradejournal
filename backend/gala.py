"""Gala levels: the stocks-in-play day trade on HOURLY trend + HOURLY levels, entered on 5-minute candles.

  trend      1-hour market structure: the last two pivot highs and lows (pivot = higher / lower than gPivot candles on
             each side). Higher high + higher low -> up (longs only), lower high + lower low -> down (shorts only),
             anything else -> unclear, no trade (gTrend off: both sides).
  levels     hourly pivots taken at the candle BODY (pivot high -> top of the body, pivot low -> bottom), plus the open
             of strong impulsive hourly candles (body >= gImpulse x the average hourly range), over the last gLvlDays
             days; a level the hourly bodies went through more than gMaxCross times is dropped; levels closer than the
             tolerance are merged (the newest is kept).
  timing     5-minute candles (built from the 1-minute ones), the first 5-minute candle is skipped (no entry before
             09:35 + the opening range of the ranking); entry at the open of the next minute after the signal candle.
  setups     breaking retest: a 5-min close beyond a level, then a candle wicks back to it (within the tolerance) and
                closes on the right side -> stop beyond the most extreme wick since the break (gRetestMax candles max);
             bounce (uptrend): price above a level pulls back; gTestBars candles in a row test it (low within the
                tolerance) with the bodies holding above -> stop under the lowest wick;
             rejection (downtrend): the mirror under resistance -> stop above the highest wick.
  exits      first target gTargetR x R, or the next hourly level when it comes first (but at least 1R away, gLvlTarget);
             gPartial % out there, the stop of the runner to break-even, then trailed behind the low (short: high) of
             the last gTrailBars 5-minute candles; flat at flatT.
  limits     at most gMaxTrades trades a day, gMaxTries attempts per level, optional one-and-done after a winner of
             gOneDoneR R or more; optional index alignment (SPY and QQQ on the same side of their VWAP), relative strength
             vs SPY since the open, and skipping FOMC / CPI days.
"""
SETUPS = ("retest", "bounce", "reject")
INDEX = ("SPY", "QQQ")


def _hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _mins(hm):
    return int(hm[:2]) * 60 + int(hm[3:5])


def agg(bars, minutes):
    """Regular-session candles (09:30-16:00) of one or more days -> candles of `minutes`, anchored at 09:30 each day.
    Each candle: t (start), end (HH:MM), o h l c v, n (number of source candles)."""
    out, cur, key = [], None, None
    for b in bars:
        hm = b["t"][11:16]
        if not ("09:30" <= hm < "16:00"):
            continue
        k = (b["t"][:10], (_mins(hm) - 570) // minutes)
        if k != key:
            if cur:
                out.append(cur)
            key = k
            s = 570 + k[1] * minutes
            cur = {"t": f"{k[0]}T{_hm(s)}", "end": _hm(min(960, s + minutes)), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                   "v": b.get("v") or 0, "n": 1}
        else:
            cur["h"] = max(cur["h"], b["h"])
            cur["l"] = min(cur["l"], b["l"])
            cur["c"] = b["c"]
            cur["v"] += b.get("v") or 0
            cur["n"] += 1
    if cur:
        out.append(cur)
    return out


def pivots(H, k):
    """([(i, high)], [(i, low)]) confirmed pivots of the hourly candles (k candles on each side)."""
    ph, pl = [], []
    for i in range(k, len(H) - k):
        hs = [H[j]["h"] for j in range(i - k, i + k + 1) if j != i]
        ls = [H[j]["l"] for j in range(i - k, i + k + 1) if j != i]
        if H[i]["h"] > max(hs):
            ph.append(i)
        if H[i]["l"] < min(ls):
            pl.append(i)
    return ph, pl


def trend(H, k):
    ph, pl = pivots(H, k)
    if len(ph) < 2 or len(pl) < 2:
        return 0
    h1, h0, l1, l0 = H[ph[-1]]["h"], H[ph[-2]]["h"], H[pl[-1]]["l"], H[pl[-2]]["l"]
    if h1 > h0 and l1 > l0:
        return 1                                        # higher high + higher low
    if h1 < h0 and l1 < l0:
        return -1                                       # lower high + lower low
    return 0


def levels(H, P, tol):
    """[{"p": price, "src": "pivot high" | "pivot low" | "impulse", "i": candle index}] of the hourly candles."""
    k = int(P.get("gPivot") or 2)
    ph, pl = pivots(H, k)
    cand = [(i, max(H[i]["o"], H[i]["c"]), "pivot high") for i in ph] + [(i, min(H[i]["o"], H[i]["c"]), "pivot low") for i in pl]
    imp = float(P.get("gImpulse") or 0)
    if imp > 0 and H:
        avg = sum(b["h"] - b["l"] for b in H) / len(H)
        cand += [(i, b["o"], "impulse") for i, b in enumerate(H) if avg > 0 and abs(b["c"] - b["o"]) >= imp * avg]
    mx = int(P.get("gMaxCross") or 0)
    keep = []
    for i, p, src in cand:
        if mx:
            cross = sum(1 for b in H[i + 1:] if min(b["o"], b["c"]) < p < max(b["o"], b["c"]))
            if cross > mx:
                continue
        keep.append({"p": round(p, 4), "src": src, "i": i})
    keep.sort(key=lambda x: -x["i"])                    # newest first: it wins the merge
    out = []
    for x in keep:
        if all(abs(x["p"] - y["p"]) > tol for y in out):
            out.append(x)
    out.sort(key=lambda x: x["p"])
    return out


def _vwap_side(bars, upto):
    """+1 / -1 / 0: the last close (time < upto) above / below the session VWAP."""
    pv = vv = 0.0
    last = None
    for b in bars:
        if b["t"][11:16] >= upto:
            break
        if b["t"][11:16] < "09:30":
            continue
        v = float(b.get("v") or 0) or 1.0
        pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        vv += v
        last = b
    if not last or not vv:
        return 0
    w = pv / vv
    return 1 if last["c"] > w else -1 if last["c"] < w else 0


def _chg(bars, upto):
    reg = [b for b in bars if "09:30" <= b["t"][11:16] < upto]
    return (reg[-1]["c"] / reg[0]["o"] - 1) if reg and reg[0]["o"] else 0.0


def market_side(mkt, upto):
    """+1 when SPY and QQQ are both above their VWAP, -1 both below, 0 otherwise (or without index candles)."""
    s = [_vwap_side(mkt.get(x) or [], upto) for x in INDEX]
    return 1 if all(v > 0 for v in s) else -1 if all(v < 0 for v in s) else 0


def simulate(pick, bars, hist, P, mkt=None, end_or="09:35"):
    """One stock, one day -> (trades, pending). bars: the 1-minute candles of the day; hist: 30-minute (or hourly)
    candles of the previous days (regular session used). pending: the signal of the last 5-minute candle when no
    minute follows it yet (live)."""
    mkt = mkt or {}
    bars = [b for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    if not bars:
        return [], None
    atr = float(pick.get("atr") or 0)
    if atr <= 0:
        return [], None
    tol = float(P.get("gTolAtr", 5)) / 100 * atr
    buf = float(P.get("gStopBuf", 2)) / 100 * atr
    k = int(P.get("gPivot") or 2)
    days = sorted({b["t"][:10] for b in hist if b["t"][:10] < bars[0]["t"][:10]})[-int(P.get("gLvlDays") or 5):]
    hprev = agg([b for b in hist if b["t"][:10] in days], 60)
    htoday = agg(bars, 60)
    b5 = agg(bars, 5)
    sides = {1, -1}
    if not P.get("longs", True):
        sides.discard(1)
    if not P.get("shorts", True):
        sides.discard(-1)
    on = {s: P.get({"retest": "gRetest", "bounce": "gBounce", "reject": "gReject"}[s], True) for s in SETUPS}
    g_end = P.get("gEnd") or "15:30"
    flat = P.get("flatT") or "15:55"
    test_n = int(P.get("gTestBars") or 2)
    rmax = int(P.get("gRetestMax") or 12)
    tries_max = int(P.get("gMaxTries") or 2)
    part = max(0.0, min(1.0, float(P.get("gPartial", 50)) / 100))
    trail_n = int(P.get("gTrailBars") or 3)
    tr_r = float(P.get("gTargetR") or 2.0)
    first_ok = _hm(max(_mins(end_or), 575))            # never before 09:35 (the first 5-minute candle is skipped)
    cache = {}

    def ctx(close_hm):
        H = hprev + [h for h in htoday if h["end"] <= close_hm and h["n"] >= 1]
        n = len(H)
        if n not in cache:
            cache[n] = (trend(H, k), levels(H, P, tol))
        return cache[n]

    broken = {}                                         # level -> (side, index of the 5-minute break candle)
    tries = {}
    trades, pending, pos = [], None, None
    j5 = -1                                             # index of the last closed 5-minute candle
    prev_close = hprev[-1]["c"] if hprev else b5[0]["o"]

    def close5(j):
        """5-minute candle j has closed: break states, then a signal (flat) or the trail (in a trade)."""
        nonlocal prev_close
        c = b5[j]
        pc = b5[j - 1]["c"] if j else prev_close
        tdir, lv = ctx(c["end"])
        for x in lv:
            L = x["p"]
            if pc <= L < c["c"]:
                broken[L] = (1, j)
            elif pc >= L > c["c"]:
                broken[L] = (-1, j)
            elif L in broken and (broken[L][0] > 0 and c["c"] < L or broken[L][0] < 0 and c["c"] > L):
                del broken[L]                           # back through: the break failed
        if pos is not None:
            if pos["partial"] and j >= trail_n - 1:
                w = b5[j - trail_n + 1: j + 1]
                if pos["side"] > 0:
                    pos["stop"] = max(pos["stop"], min(b["l"] for b in w))
                else:
                    pos["stop"] = min(pos["stop"], max(b["h"] for b in w))
            return None
        if c["t"][11:16] < "09:35" or c["end"] < first_ok or c["end"] >= g_end or c["end"] >= flat:
            return None
        allowed = set(sides)
        if P.get("gTrend", True):
            allowed &= {tdir} if tdir else set()
        if P.get("gMktAlign") and mkt:
            ms = market_side(mkt, c["end"])
            allowed &= {ms} if ms else set()
        if P.get("gRelStr") and mkt.get("SPY"):
            rs = _chg(bars, c["end"]) - _chg(mkt["SPY"], c["end"])
            allowed &= {1} if rs > 0 else {-1} if rs < 0 else set()
        if not allowed:
            return None
        best = None
        for x in sorted(lv, key=lambda x: abs(x["p"] - c["c"])):
            L = x["p"]
            if tries.get(L, 0) >= tries_max:
                continue
            sig = None
            if L in broken and broken[L][1] < j and j - broken[L][1] <= rmax and on["retest"]:
                s, bj = broken[L]
                if s in allowed:
                    if s > 0 and c["l"] <= L + tol and c["c"] > L:
                        sig = (1, "retest", min(b["l"] for b in b5[bj + 1: j + 1]) - buf)
                    elif s < 0 and c["h"] >= L - tol and c["c"] < L:
                        sig = (-1, "retest", max(b["h"] for b in b5[bj + 1: j + 1]) + buf)
            elif L not in broken and j >= test_n:
                w = b5[j - test_n + 1: j + 1]
                before = b5[j - test_n]["c"]
                if on["bounce"] and 1 in allowed and before > L and all(min(b["o"], b["c"]) > L and L - 2 * tol <= b["l"] <= L + tol for b in w):
                    sig = (1, "bounce", min(b["l"] for b in w) - buf)
                elif on["reject"] and -1 in allowed and before < L and all(max(b["o"], b["c"]) < L and L - tol <= b["h"] <= L + 2 * tol for b in w):
                    sig = (-1, "reject", max(b["h"] for b in w) + buf)
            if sig:
                best = (L, x["src"], *sig)
                break
        if not best:
            return None
        L, src, s, setup, stop = best
        risk = (c["c"] - stop) * s
        if risk < 0.03 * atr or risk > 0.6 * atr:       # a stop too tight to survive noise, or far too wide
            return None
        tgt = c["c"] + s * tr_r * risk
        if P.get("gLvlTarget", True):
            nxt = [y["p"] for y in lv if (y["p"] - c["c"]) * s >= risk and (y["p"] - tgt) * s < 0]
            if nxt:
                tgt = min(nxt) if s > 0 else max(nxt)
        tries[L] = tries.get(L, 0) + 1
        return {"side": s, "setup": setup, "level": L, "levelSrc": src, "stop": round(stop, 4), "target": round(tgt, 4),
                "signalAt": c["t"], "trend": tdir}

    sig = None
    for i, b in enumerate(bars):
        hm = b["t"][11:16]
        j = (_mins(hm) - 570) // 5
        if sig and pos is None:                         # the entry: the open of the minute after the signal candle
            e = b["o"]
            risk = (e - sig["stop"]) * sig["side"]
            if risk > 0:
                pos = {**sig, "entry": e, "at": b["t"], "risk": risk, "stop0": sig["stop"], "partial": False, "done": 0.0}
            sig = None
        if pos is not None:
            s = pos["side"]
            if (b["l"] <= pos["stop"]) if s > 0 else (b["h"] >= pos["stop"]):
                px = min(b["o"], pos["stop"]) if s > 0 else max(b["o"], pos["stop"])
                trades.append(_close(pick, pos, px, b["t"], "trail" if pos["partial"] and pos["stop"] != pos["entry"] else "stop", part))
                pos = None
            elif not pos["partial"] and ((b["h"] >= pos["target"]) if s > 0 else (b["l"] <= pos["target"])):
                pos["partial"] = True
                pos["partAt"] = b["t"]
                if part >= 1:
                    trades.append(_close(pick, pos, pos["target"], b["t"], "target", part))
                    pos = None
                else:
                    pos["stop"] = max(pos["stop"], pos["entry"]) if s > 0 else min(pos["stop"], pos["entry"])
            if pos is not None and (hm >= flat or i == len(bars) - 1):
                trades.append(_close(pick, pos, b["c"], b["t"], "target+close" if pos["partial"] else "close", part))
                pos = None
        last_of_5 = i == len(bars) - 1 or (_mins(bars[i + 1]["t"][11:16]) - 570) // 5 != j
        if last_of_5 and j > j5:
            jj = next((q for q in range(len(b5)) if b5[q]["t"][11:16] == _hm(570 + j * 5)), None)
            if jj is not None:
                j5 = j
                sg = close5(jj)
                if sg and hm < flat:
                    if i == len(bars) - 1:
                        pending = sg
                    else:
                        sig = sg
    return trades, pending


def _close(pick, pos, px, at, status, part):
    s, e, r0 = pos["side"], pos["entry"], pos["risk"]
    if pos["partial"]:
        R = part * (pos["target"] - e) * s / r0 + (1 - part) * (px - e) * s / r0
        if status in ("trail", "stop"):
            status = "target+trail"
    else:
        R = (px - e) * s / r0
    return {"sym": pick["sym"], "at": pos["at"], "side": s, "dir": "long" if s > 0 else "short", "entry": round(e, 4),
            "stop0": round(pos["stop0"], 4), "target": pos["target"], "exit": round(px, 4), "exitAt": at, "status": status,
            "setup": pos["setup"], "level": pos["level"], "levelSrc": pos["levelSrc"], "R": round(R, 3),
            "pct": round((px / e - 1) * 100 * s, 3), "rvol": pick.get("rvol"), "pmRvol": pick.get("pmRvol"), "atr": pick.get("atr")}


def day_filter(trs, P):
    """Trades of every stock of one day (time order) -> the ones taken under the daily limits."""
    trs = sorted(trs, key=lambda t: t["at"])
    mx = int(P.get("gMaxTrades") or 3)
    one = P.get("gOneDone")
    oner = float(P.get("gOneDoneR") or 1.5)
    out = []
    for t in trs:
        if len(out) >= mx:
            break
        if one and any(x["R"] >= oner and x["exitAt"] <= t["at"] for x in out):
            break
        if any(x["sym"] == t["sym"] and x["exitAt"] > t["at"] for x in out):
            continue
        out.append(t)
    return out


def day_trades(picks, bars_by_sym, hist_by_sym, P, mkt=None, end_or="09:35"):
    trs = []
    for p in picks:
        t, _ = simulate(p, bars_by_sym.get(p["sym"]) or [], hist_by_sym.get(p["sym"]) or [], P, mkt, end_or)
        trs += t
    out = day_filter(trs, P)
    n = max(1, int(P.get("gMaxTrades") or 3))
    risk_frac = float(P["riskPct"]) / 100
    for t in out:
        pos_frac = risk_frac * t["entry"] / max(1e-9, abs(t["entry"] - t["stop0"]))
        f = min(1.0, (float(P["maxLev"]) / n) / pos_frac) if pos_frac > 0 else 1.0
        t["size"] = round(f, 4)
        t["ret"] = round(risk_frac * t["R"] * f * 100, 4)
    return out


def event_skip(d, P):
    if not P.get("gSkipEvents"):
        return False
    try:
        import marketctx
        f = marketctx.event_flags(d)
        return bool(f.get("fomc") or f.get("cpi"))
    except Exception:
        return False


def qty(P, equity, entry, stop):
    risk = abs(entry - stop)
    if risk <= 0 or entry <= 0:
        return 0
    q = int(equity * float(P["riskPct"]) / 100 / risk)
    q_lev = int(equity * float(P["maxLev"]) / max(1, int(P.get("gMaxTrades") or 3)) / entry)
    return max(0, min(q, q_lev))


def round_px(x):
    return round(x, 2) if x >= 1 else round(x, 4)


def hist_start(day, P):
    """Start of the 30-minute history to load before `day` (enough calendar days for gLvlDays trading days)."""
    from datetime import timedelta
    return (day - timedelta(days=int(int(P.get("gLvlDays") or 5) * 1.5) + 5)).replace(hour=9, minute=30)


# ---------------- live (the stock account, called by stockday.tick every minute) ----------------

def live(sub, P, c, now, picks, pk, d, hm):
    """At each 5-minute close: replay the day so far on every stock in play; a signal on the candle that just closed
    -> market entry, half with a take-profit at the target + the stop, the runner with the stop only. Runner stop to
    break-even then trailed once the first half is out. The daily limits come from the replay of the day."""
    import json
    from datetime import timedelta
    from decimal import Decimal
    import db
    import stockday as sd
    acts = []
    if not picks or hm < "09:35" or hm >= P["flatT"]:
        return acts
    st = db.get(pk, f"SDGALA#{d}") or {}
    slot = _hm((_mins(hm) - 570) // 5 * 5 + 570)        # start of the 5-minute candle now forming
    if st.get("slot") == slot:
        return acts
    db.put({"PK": pk, "SK": f"SDGALA#{d}", "slot": slot})
    day = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    end = now.replace(second=0, microsecond=0, tzinfo=None) - timedelta(minutes=1)
    syms = [p["sym"] for p in picks]
    bb = sd.multi_bars(c, syms + list(INDEX), "1Min", day.replace(hour=9, minute=30), end, max_pages=5)
    hist = sd.multi_bars(c, syms, "30Min", hist_start(day, P), day.replace(hour=9, minute=29), max_pages=5)
    for s in syms:                                      # the minute just closed belongs to the candle that closed
        bb[s] = [b for b in bb.get(s) or [] if b["t"][11:16] < slot]
    mkt = {x: [b for b in bb.get(x) or [] if b["t"][11:16] < slot] for x in INDEX}
    m = int(P["orbMin"])
    end_or = _hm(570 + m)
    done, pend = [], {}
    for p in picks:
        t, pg = simulate(p, bb.get(p["sym"]) or [], hist.get(p["sym"]) or [], P, mkt, end_or)
        done += t
        if pg:
            pend[p["sym"]] = (p, pg)
    taken = day_filter(done, P)
    recs = [r for r in db.q_prefix(pk, f"SDPOS#{d}#") if r.get("gala")]
    try:
        pos = {x["symbol"]: x for x in (sd._alp(c, "GET", "/v2/positions") or [])}
    except Exception:
        pos = {}
    acts += _trail(c, P, recs, pos, bb, pk)
    if not P.get("trade") or hm >= entry_end(P):
        return acts
    n_today = max(len(taken), len(recs))
    if n_today >= int(P.get("gMaxTrades") or 3):
        return acts
    if P.get("gOneDone") and any(x["R"] >= float(P.get("gOneDoneR") or 1.5) for x in taken):
        return acts
    try:
        eq = float((sd._alp(c, "GET", "/v2/account") or {}).get("equity") or 0)
    except Exception:
        eq = 0
    for sym, (p, g) in pend.items():
        if n_today >= int(P.get("gMaxTrades") or 3):
            break
        if sym in pos:
            continue
        px = (bb.get(sym) or [{}])[-1].get("c") or 0
        q = qty(P, eq, px, g["stop"])
        if q < 1:
            continue
        s = g["side"]
        stop, tgt = round_px(g["stop"]), round_px(g["target"])
        side = "buy" if s > 0 else "sell"
        part = max(0.0, min(1.0, float(P.get("gPartial", 50)) / 100))
        q1 = int(round(q * part)) if q > 1 else (q if part >= 0.5 else 0)
        q2 = q - q1
        ids = {}
        try:
            if q1:
                o = sd._alp(c, "POST", "/v2/orders", {"symbol": sym, "qty": str(q1), "side": side, "type": "market", "time_in_force": "day",
                                                      "order_class": "bracket", "take_profit": {"limit_price": str(tgt)},
                                                      "stop_loss": {"stop_price": str(stop)}}) or {}
                ids["orderId"] = o.get("id")
            if q2:
                o = sd._alp(c, "POST", "/v2/orders", {"symbol": sym, "qty": str(q2), "side": side, "type": "market", "time_in_force": "day",
                                                      "order_class": "oto", "stop_loss": {"stop_price": str(stop)}}) or {}
                ids["runnerId"] = o.get("id")
            db.put({"PK": pk, "SK": f"SDPOS#{d}#{sym}#{hm}", "symbol": sym, "at": f"{d}T{hm}", "dir": "long" if s > 0 else "short",
                    "qty": q, "level": Decimal(str(round_px(g["level"]))), "stop": Decimal(str(stop)), "target": Decimal(str(tgt)),
                    "entry": Decimal(str(round_px(px))), "setup": g["setup"], "gala": True, "rvol": Decimal(str(p.get("rvol") or 0)),
                    "status": "open", **{k: v for k, v in ids.items() if v}})
            acts.append(f"{sym} {g['setup']} {side} {q} (stop {stop}, target {tgt}, level {round_px(g['level'])})")
            n_today += 1
        except Exception as e:
            acts.append(f"{sym}: {str(e)[:100]}")
    return acts


def _trail(c, P, recs, pos, bb, pk):
    """Runner: once the first half is out (take-profit filled), its stop goes to break-even, then behind the last
    gTrailBars 5-minute candles."""
    import db
    import stockday as sd
    acts = []
    n = int(P.get("gTrailBars") or 3)
    for r in recs:
        if r.get("status") != "open" or not r.get("runnerId") or r["symbol"] not in pos:
            continue
        try:
            if r.get("orderId"):
                o = sd._alp(c, "GET", f"/v2/orders/{r['orderId']}?nested=true") or {}
                tp = [x for x in o.get("legs") or [] if x.get("type") == "limit"]
                if not (tp and tp[0].get("status") == "filled"):
                    continue
            ro = sd._alp(c, "GET", f"/v2/orders/{r['runnerId']}?nested=true") or {}
            leg = next((x for x in ro.get("legs") or [] if x.get("type") in ("stop", "stop_limit") and x.get("status") in ("new", "accepted", "held")), None)
            if not leg:
                continue
            b5 = agg(bb.get(r["symbol"]) or [], 5)[-n:]
            if not b5:
                continue
            long_ = r["dir"] == "long"
            e = float(r.get("entry") or 0)
            cur = float(leg.get("stop_price") or 0)
            new = max(e, min(b["l"] for b in b5), cur) if long_ else min(e, max(b["h"] for b in b5), cur or 1e12)
            new = round_px(new)
            if abs(new - cur) >= 0.01:
                sd._alp(c, "PATCH", f"/v2/orders/{leg['id']}", {"stop_price": str(new)})
                db.update(pk, r["SK"], {"runnerStop": str(new)})
                acts.append(f"{r['symbol']} runner stop → {new}")
        except Exception as e:
            acts.append(f"{r['symbol']} trail: {str(e)[:80]}")
    return acts


# ---------------- the pre-market watchlist (09:15 ET) ----------------

def plan(price, tdir, lv, tol):
    """Direction + the setup to wait for, from the hourly trend and the levels around the pre-market price."""
    above = sorted((x for x in lv if x["p"] > price), key=lambda x: x["p"])
    below = sorted((x for x in lv if x["p"] <= price), key=lambda x: -x["p"])
    if tdir > 0:
        if below and (not above or price - below[0]["p"] <= above[0]["p"] - price):
            return "long", f"bounce at {below[0]['p']:.2f} (support)", below[0]["p"]
        if above:
            return "long", f"breaking retest above {above[0]['p']:.2f}", above[0]["p"]
        return "long", "bounce at the next pullback (no level below)", None
    if tdir < 0:
        if above and (not below or above[0]["p"] - price <= price - below[0]["p"]):
            return "short", f"rejection at {above[0]['p']:.2f} (resistance)", above[0]["p"]
        if below:
            return "short", f"breaking retest below {below[0]['p']:.2f}", below[0]["p"]
        return "short", "rejection at the next rally (no level above)", None
    return "skip", "hourly trend unclear", None


def pick_watch(cands, hist, P, d):
    """cands: [{sym, atr, pmRvol, price, ...}] best first -> the top gWatchN with a clear hourly trend (when gTrend),
    each with its trend, levels, direction and setup."""
    out = []
    n = int(P.get("gWatchN") or 5)
    for x in cands:
        hs = hist.get(x["sym"]) or []
        days = sorted({b["t"][:10] for b in hs if b["t"][:10] < d})[-int(P.get("gLvlDays") or 5):]
        H = agg([b for b in hs if b["t"][:10] in days], 60)
        if len(H) < 6:
            continue
        tol = float(P.get("gTolAtr", 5)) / 100 * float(x["atr"])
        tdir = trend(H, int(P.get("gPivot") or 2))
        if P.get("gTrend", True) and not tdir:
            continue
        if tdir > 0 and not P.get("longs", True) or tdir < 0 and not P.get("shorts", True):
            continue
        lv = levels(H, P, tol)
        price = x.get("price") or H[-1]["c"]
        direc, setup, key = plan(price, tdir, lv, tol)
        near = sorted(lv, key=lambda y: abs(y["p"] - price))[:4]
        out.append({**x, "trend": "up" if tdir > 0 else "down" if tdir < 0 else "unclear", "direction": direc, "setup": setup,
                    "keyLevel": key, "levels": sorted(({"p": y["p"], "src": y["src"]} for y in near), key=lambda y: -y["p"])})
        if len(out) >= n:
            break
    return out


def watchlist(sub, now=None):
    """09:15 ET: today's universe ranked by pre-market RVOL (pre-market volume so far vs its 14-day average), then the
    top gWatchN with a clear hourly trend, each with its levels and the setup to wait for. Stored as SDWATCH#day."""
    import json
    from datetime import timedelta
    from decimal import Decimal
    import db
    import stockday as sd
    from util import iso, now_ny
    P = sd.config(sub)
    now = now or now_ny()
    d = now.strftime("%Y-%m-%d")
    pk = db.upk(sub)
    c = sd.data_creds(sub)
    if not c:
        return {"error": "Connect an Alpaca account to load the stock data."}
    uni = sd.get_day("U", d)
    if not uni:
        out = {"date": d, "at": now.strftime("%H:%M"), "error": "no universe for today yet (the morning prep runs at 09:05)"}
        db.put({"PK": pk, "SK": f"SDWATCH#{d}", **out})
        return out
    ex = set(P.get("exclude") or [])
    syms = sorted(s for s in uni if s not in ex)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    cut = min(now.replace(tzinfo=None, second=0, microsecond=0), day.replace(hour=9, minute=29))
    pm = {}
    for i in range(0, len(syms), 200):
        pm.update(sd.multi_bars(c, syms[i:i + 200], "30Min", day.replace(hour=4), cut))
    rdays = [x for x in sd.stored_days("R") if x < d][-14:]
    avg = {}
    for x in rdays:
        for s, w in sd.get_day("R", x).items():
            avg.setdefault(s, []).append(float(w[0]))
    min_v = float(P.get("gWatchMinPmVol") or 0)
    cands = []
    for s in syms:
        bs = [b for b in pm.get(s) or [] if "04:00" <= b["t"][11:16] < "09:30"]
        v = sum(b["v"] or 0 for b in bs)
        h = avg.get(s) or []
        if not bs or len(h) < 5 or v < min_v:
            continue
        a = sum(h) / len(h)
        if a <= 0:
            continue
        atr, _, pc = (float(z) for z in uni[s][:3])
        price = bs[-1]["c"]
        cands.append({"sym": s, "pmRvol": round(v / a, 2), "pmVol": int(v), "price": round(price, 4), "prevClose": pc,
                      "gapPct": round((price / pc - 1) * 100, 2) if pc else None, "atr": atr, "rvol": round(v / a, 2)})
    cands.sort(key=lambda x: -x["pmRvol"])
    top = cands[: max(15, 3 * int(P.get("gWatchN") or 5))]
    hist = sd.multi_bars(c, [x["sym"] for x in top], "30Min", hist_start(day, P), day.replace(hour=9, minute=29), max_pages=8) if top else {}
    picks = pick_watch(top, hist, P, d)
    ev = {}
    try:
        import marketctx
        ev = {k: v for k, v in marketctx.event_flags(d).items() if k in ("fomc", "cpi", "nfp", "opex") and v}
    except Exception:
        pass
    out = {"date": d, "at": now.strftime("%H:%M"), "picks": picks, "candidates": len(cands), "universe": len(uni),
           "events": sorted(ev), "note": "pre-market volume 04:00 to now vs the 14-day average of the full pre-market"}
    item = {"PK": pk, "SK": f"SDWATCH#{d}", **json.loads(json.dumps(out, default=str), parse_float=Decimal), "built": iso(now_ny())}
    db.put(item)
    db.put({**item, "SK": "SDWATCH#last"})
    return out


def watch_today(sub, d):
    import db
    r = db.get(db.upk(sub), f"SDWATCH#{d}") or {}
    return [{k: (float(v) if k not in ("sym", "trend", "direction", "setup", "levels") and v is not None and not isinstance(v, (str, list)) else v)
             for k, v in p.items()} for p in r.get("picks") or []]
