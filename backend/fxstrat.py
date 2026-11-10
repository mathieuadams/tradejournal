"""Fractal (TradeWellX) strategy: the wave pivot, the percent-probable cluster levels, the Option Analyzer wave-count
odds, the Macro Trend, the StdDev squeeze and the daily wave state, as entries for NQ / ES.

  The fractal runs on the strategy's own `tf`-minute candles (NYSE hours only or overnight + NYSE), `wave` bars.
  Levels and odds use only waves finished before the candle (the last `stats` sessions of them): no look-ahead
  (TradeWellX's replay re-uses every wave, the future ones included, so its levels look better than they were).

  Entries (`entry`):
    cross  the first close across the pivot (a new H / L wave): go with it. Stop = the other side's `stopP` level
           (red level under a long, green over a short); target = this side's `tgtP` level (or `tgtR` x the risk).
    fade   a candle reaching this wave's `fadeP` outlier level (green 10% / 5% over, red 10% / 2% under) and closing
           back inside it: fade it. Stop = the wave's extreme + `bufTicks`; target = the pivot (or `tgtR`).
    count  the wave has run `n` candles and the Option Analyzer gives under `oddsMax` for a wave that long, and the
           candle closes against it: fade it. Stop = the wave's extreme + `bufTicks`; target = the pivot (or `tgtR`).
    vector the first close across the purple vector (the MA): go with it. Stop / target as for cross.
  Targets (`tgt`): level (this side's `tgtP` cluster level for cross / vector, the pivot for fade / count), pivot,
    prev (the previous actual wave's extreme: the last H high for a long, the last L low for a short), day (the
    previous session's open / high / low nearest beyond the entry), wall (the gamma call wall for a long, put wall for a
    short, from the recorded MM data; QQQ / SPY strikes x the NQ / ES ratio at that time), or r (`tgtR` x the risk).
    A target not beyond the entry falls back to `level`.
  MM data (`mm`, recorded / rebuilt Unusual Whales snapshots, NYSE hours): calls (long only when calls at/above the
    price > calls below; short the reverse), puts (short only when puts at/above > puts below: bearish divergence;
    long the reverse), both, or none. Days without MM data take no trade when it is on.
  Option Analyzer (`oaMin` > 0): a cross / vector entry needs at least `oaMin` of past same-side waves to have run
    `oaBars`+ candles.
  Higher timeframe (`htf` 60 / 240 / D, `htfWave`, `htfRule`): the fractal on 60-minute, 4-hour or daily candles,
    from the last finished higher-timeframe candle (no look-ahead): side (close vs its pivot), dir (its direction),
    vector (close vs its MA) or macro (its Macro Trend). Trades only on its side.
  Filters: `macro` with / against / none (close vs EMA 30 on the same candles); `sqz` any / after (a squeeze, StdDev
  under 0.1, in the last `wave` candles) / no (not while squeezed); `daily` with / none (the previous session's
  Wave 1 daily state, H = long side, L = short side). One position at a time, at most `maxDay` a session, flat 15:55."""
import bisect
import fractal
import nightkit as N
import stockdata

TICK = {"QQQ": 0.01, "SPY": 0.01}

DEFAULT = {"tickers": ["NQ"], "tf": 15, "wave": 10, "stats": 10, "hours": "all", "entry": "cross", "stopP": 0.5, "tgtP": 0.5, "fadeP": 0.1,
           "oddsMax": 0.2, "tgt": "level", "mm": "none", "oaMin": 0, "oaBars": 3, "htf": "none", "htfWave": 10, "htfRule": "side",
           "smLevel": "wall", "smTgt": "median", "smMatch": "hl", "smDays": 200, "lateN": 3, "lateStopP": 0.1, "oaExit": 0, "macro": "with", "sqz": "any", "daily": "none", "lastEntry": "15:00", "maxDay": 2,
           "bias": "none", "side": "both", "exit": "target", "tgtR": 0, "bufTicks": 2, "days": 500}
FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("wd", "Weekday"), ("ent", "Entry"), ("mac", "Macro Trend"), ("sq", "StdDev at entry"),
           ("dst", "Daily wave"), ("cnt", "Wave count at entry"), ("ts", "Target used"), ("mmv", "MM option volume"), ("htfs", "Higher-timeframe bias")]
LEVELS = {"bull": fractal.BULL_P, "bear": fractal.BEAR_P}
ENTRIES = ("cross", "vector", "fade", "count", "smfade", "late")


def _pick(v, opts, dflt):
    try:
        v = type(opts[0])(v)
    except (TypeError, ValueError):
        return dflt
    return v if v in opts else dflt


def clean(p):
    p = {**DEFAULT, **(p or {})}
    out = {"setup": "fractal", "tf": _pick(p.get("tf"), (5, 15, 30, 60), 15),
           "hours": p.get("hours") if p.get("hours") in ("rth", "all") else "all",
           "entry": p.get("entry") if p.get("entry") in ENTRIES else "cross",
           "smLevel": p.get("smLevel") if p.get("smLevel") in ("wall", "max", "median") else "wall",
           "smTgt": p.get("smTgt") if p.get("smTgt") in ("median", "close", "pivot") else "median",
           "smMatch": "weekday" if p.get("smMatch") == "weekday" else "hl",
           "lateStopP": _pick(p.get("lateStopP"), (0.2, 0.1, 0.05), 0.1),
           "stopP": _pick(p.get("stopP"), (0.8, 0.5, 0.2, 0.1), 0.5), "tgtP": _pick(p.get("tgtP"), (0.8, 0.5, 0.2, 0.1, 0.05), 0.5),
           "fadeP": _pick(p.get("fadeP"), (0.2, 0.1, 0.05), 0.1), "tgt": p.get("tgt") if p.get("tgt") in ("level", "pivot", "prev", "day", "wall", "r") else "level",
           "mm": p.get("mm") if p.get("mm") in ("none", "calls", "puts", "both") else "none",
           "htf": str(p.get("htf")) if str(p.get("htf")) in ("none", "60", "240", "D") else "none",
           "htfRule": p.get("htfRule") if p.get("htfRule") in ("side", "dir", "vector", "macro") else "side",
           "macro": p.get("macro") if p.get("macro") in ("with", "against", "none") else "with",
           "sqz": p.get("sqz") if p.get("sqz") in ("any", "after", "no") else "any",
           "daily": p.get("daily") if p.get("daily") in ("with", "none") else "none",
           "lastEntry": N.hhmm(p.get("lastEntry"), "15:00", "09:40", "15:50")}
    N.K.num(p, out, (("wave", 2, 50, 10), ("stats", 2, 60, 10), ("oddsMax", 0.01, 0.9, 0.2), ("maxDay", 1, 20, 2), ("oaMin", 0, 1, 0), ("oaBars", 1, 50, 3), ("htfWave", 1, 50, 10), ("smDays", 60, 1000, 200),
                           ("lateN", 1, 50, 3), ("oaExit", 0, 0.9, 0)),
            ("wave", "stats", "maxDay", "oaBars", "htfWave", "smDays", "lateN"))
    if out["entry"] == "late" and out["htf"] == "none":
        out["htf"] = "D"                                   # the late counter-wave needs a higher-timeframe side
    N.clean_common(p, out, bias="none")
    if p.get("bufTicks") in (None, ""):
        out["bufTicks"] = 2
    return out


SM_TXT = {"wall": "wall (MaxAvg, ~25% odds)", "max": "1% outlier (Max)", "median": "50% median"}
HTF_TXT = {"60": "60-minute", "240": "4-hour", "D": "daily"}
HTF_RULE = {"side": "longs above its pivot (H), shorts below (L)", "dir": "longs when its direction is up, shorts down",
            "vector": "longs above its purple vector, shorts below", "macro": "longs when its Macro Trend is up, shorts down"}
TGT_TXT = {"level": None, "pivot": "the pivot", "prev": "the previous wave's extreme", "day": "the previous session's open / high / low",
           "wall": "the gamma wall", "r": None}


def describe(p):
    tg = TGT_TXT[p["tgt"]] or (f"{p['tgtR']:g}R" if p["tgt"] == "r" else None)
    e = {"cross": "the first close across the pivot (a new wave): go with it",
         "vector": "the first close across the purple vector: go with it",
         "fade": f"a candle reaching the wave's {p['fadeP']:.0%} level and closing back inside: fade it",
         "count": f"a wave running longer than {p['oddsMax']:.0%} of past waves did, then a candle against it: fade it",
         "smfade": f"a candle reaching the day's Spread Monster {SM_TXT[p['smLevel']]} (daily bars, {p['smDays']} days, {'weekday' if p['smMatch'] == 'weekday' else 'HL'} matching) and closing back inside: fade it, stop over the day's extreme, target "
                   + {"median": "the 50% median on that side (or the previous close)", "close": "the previous close", "pivot": "the pivot"}[p["smTgt"]],
         "late": f"a counter-wave against the higher timeframe reaching {p['lateN']} candles, then a candle turning back: go with the higher timeframe, stop at the {p['lateStopP']:.0%} level"}[p["entry"]]
    if p["entry"] in ("cross", "vector", "late"):
        if p["entry"] != "late":
            e += f", stop at the other side's {p['stopP']:.0%} level"
        e += ", target " + (tg or f"this side's {p['tgtP']:.0%} level")
        if p["oaExit"]:
            e += f", out early once fewer than {p['oaExit']:.0%} of past waves ran longer (Option Analyzer)"
    elif p["entry"] == "smfade":
        pass
    else:
        e += ", stop over the wave's extreme, target " + (tg or "the pivot")
    f = [x for x in ({"with": "with the Macro Trend", "against": "against the Macro Trend", "none": ""}[p["macro"]],
                     {"after": "after a StdDev squeeze", "no": "not while squeezed", "any": ""}[p["sqz"]],
                     "with the daily wave" if p["daily"] == "with" else "",
                     {"calls": "calls at/above vs below the price agree (MM data)", "puts": "puts at/above vs below agree (MM data)",
                      "both": "calls and puts agree (MM data)", "none": ""}[p["mm"]],
                     f"Option Analyzer: {p['oaMin']:.0%}+ of past waves ran {p['oaBars']}+ candles" if p["oaMin"] and p["entry"] in ("cross", "vector") else "",
                     (f"with the {HTF_TXT[p['htf']]} fractal (Wave {p['htfWave']}): " + HTF_RULE[p["htfRule"]]) if p["htf"] != "none" else "") if x]
    return (f"{', '.join(p['tickers'])} fractal on {p['tf']}-minute candles ({'overnight + NYSE' if p['hours'] == 'all' else 'NYSE session'}), "
            f"Wave {p['wave']}, levels from the last {p['stats']} sessions' waves: {e}" + (f" ({'; '.join(f)})" if f else "")
            + f", at most {p['maxDay']} a session, flat at 15:55")


def tickers(sub, p):
    return list(p["tickers"])


# ---------------------------------------------------------------- the fractal over the whole run, once per setting

_SIG = {}


def _daily_state(sym):
    """{day: +1 / -1}: the Wave 1 daily state at that session's close (H = close over its pivot)."""
    import futdata
    D = stockdata.daily(sym) if stockdata.is_stock(sym) else futdata.daily(sym)
    if len(D) < 5:
        return {}
    r = fractal.compute([[x[0], x[1], x[2], x[3], x[4]] for x in D], 1)
    return {x[0]: (1 if x[4] > s["pivot"] else -1) for x, s in zip(D, r["series"]) if s["pivot"] is not None}


def signals(sym, p, S, days, sday_, pday=None):
    """{candle time: signal} over the continuous candles S (all sessions of the run, oldest first); sday[i] = S[i]'s session."""
    import futdata
    key = (sym, p["tf"], p["hours"], p["wave"], p["stats"], p["entry"], p["stopP"], p["tgtP"], p["fadeP"], p["oddsMax"], p["tgt"],
           p["macro"], p["sqz"], p["daily"], p["oaMin"], p["oaBars"], p["htf"], p["htfWave"], p["htfRule"], p["smLevel"], p["smTgt"], p["smMatch"], p["smDays"],
           p["lateN"], p["lateStopP"], p["oaExit"], len(S), S[-1][0] if S else None, None if stockdata.is_stock(sym) else futdata.last_bar_time(sym))
    if key in _SIG:
        return _SIG[key]
    if len(_SIG) > 40:
        _SIG.pop(next(iter(_SIG)))
    r = fractal.compute([list(b) for b in S], p["wave"])
    ser, W = r["series"], [w for w in r["waves"] if not w.get("open")]
    t = [b[0] for b in S]
    ti = {x: i for i, x in enumerate(t)}
    ends = [ti[w["end"]] for w in W]                     # bar index where each wave finished (waves are in order)
    nd, sday = sorted(set(days)), sday_
    hst = htf_states(sym, p, S, sday) if p["htf"] != "none" else None
    sm = sm_levels(sym, p) if p["entry"] == "smfade" else {}
    cur_day, dhi, dlo, sm_used = None, None, None, set()
    ds = _daily_state(sym) if p["daily"] == "with" else {}
    dk = sorted(ds)
    from collections import deque
    out, win, add, up, dn, cH, cL = {}, deque(), 0, [], [], [], []
    side, wstart, used_wave, ext, vside, lastH, lastL = 0, None, None, None, 0, None, None
    pday = pday or {}
    for i in range(len(S)):
        si = ser[i]
        piv = si["pivot"]
        if piv is None:
            continue
        c, h, lo = S[i][4], S[i][2], S[i][3]
        s = 1 if c > piv else -1
        new = s != side
        if new:
            side, wstart, ext = s, i, (h if s > 0 else lo)
        else:
            ext = max(ext, h) if s > 0 else min(ext, lo)
        # waves finished before this candle, within the last `stats` sessions (sliding window, sorted lists)
        while add < len(W) and ends[add] < i:
            w = W[add]
            win.append(w)
            bisect.insort(up if w["side"] == "H" else dn, w["move"])
            bisect.insort(cH if w["side"] == "H" else cL, w["count"])
            if w["side"] == "H":
                lastH = w["price"]
            else:
                lastL = w["price"]
            add += 1
        first = nd[max(0, bisect.bisect_left(nd, sday[i]) - p["stats"])]
        while win and sday[ti[win[0]["start"]]] < first:
            w = win.popleft()
            (up if w["side"] == "H" else dn).remove(w["move"])
            (cH if w["side"] == "H" else cL).remove(w["count"])
        if sday[i] != cur_day:                          # the session's high / low so far (Spread Monster fade stops)
            cur_day, dhi, dlo = sday[i], h, lo
        else:
            dhi, dlo = max(dhi, h), min(dlo, lo)
        ma0 = si["ma"]
        vnew = False
        if ma0 is not None:
            v = 1 if c > ma0 else -1
            vnew, vside = v != vside and vside != 0, v
        sig = None
        if (len(up) < 4 or len(dn) < 4) and p["entry"] != "smfade":
            continue
        ma = ma0 if ma0 is not None else piv
        lo_b, hi_b = min(piv, ma), max(piv, ma)
        bull = lambda q: lo_b * (1 + up[fractal._rank(len(up), q)]) if up else None
        bear = lambda q: hi_b * (1 - dn[fractal._rank(len(dn), q)]) if dn else None
        cnt = i - wstart + 1
        go = (p["entry"] == "cross" and new and i) or (p["entry"] == "vector" and vnew)
        lv = sm.get(sday[i]) if p["entry"] == "smfade" else None
        if lv:
            buf = p["bufTicks"] * TICK.get(sym, N.K.TICK)
            for side_, L in ((1, lv["bull"][p["smLevel"]]), (-1, lv["bear"][p["smLevel"]])):
                if (sday[i], side_) in sm_used:
                    continue
                hit = (h >= L > c) if side_ > 0 else (lo <= L < c)
                if not hit:
                    continue
                d = -side_                                  # fade the level
                stop = dhi + buf if d < 0 else dlo - buf
                med = lv["bull" if side_ > 0 else "bear"]["median"]
                tgt0 = {"median": med if (med - c) * d > 0 else lv["close"], "close": lv["close"], "pivot": piv}[p["smTgt"]]
                if (c - stop) * d > 0 and (tgt0 - c) * d > 0:
                    sig = (d, stop, tgt0)
                    sm_used.add((sday[i], side_))
                    break
        elif p["entry"] == "late" and hst and hst[i] and used_wave != wstart and s == -hst[i]:
            # a wave against the higher timeframe, `lateN` candles old, then a candle turning back: go with the HTF
            turn = i and ((c > S[i - 1][4]) if s < 0 else (c < S[i - 1][4]))
            if cnt >= p["lateN"] and turn:
                d = hst[i]
                stop = bear(p["lateStopP"]) if d > 0 else bull(p["lateStopP"])
                if (c - stop) * d > 0:
                    sig = (d, stop, piv if (piv - c) * d > 0 else (bull(p["tgtP"]) if d > 0 else bear(p["tgtP"])))
                    used_wave = wstart
        elif go:
            d = s if p["entry"] == "cross" else vside
            stop = bear(p["stopP"]) if d > 0 else bull(p["stopP"])
            if p["oaMin"]:                                  # Option Analyzer: enough past waves on this side ran oaBars+ candles
                cs = cH if d > 0 else cL
                if not cs or sum(1 for x in cs if x >= p["oaBars"]) / len(cs) < p["oaMin"]:
                    continue
            if (c - stop) * d > 0:
                sig = (d, stop, bull(p["tgtP"]) if d > 0 else bear(p["tgtP"]))
        elif p["entry"] in ("fade", "count") and used_wave != wstart:
            hit = False
            if p["entry"] == "fade":
                fp = p["fadeP"]
                lvl = bull(fp) if s > 0 else bear(0.02 if fp == 0.05 else fp)   # the red outer level is 2% (TradeWellX's bear set)
                hit = (h >= lvl > c) if s > 0 else (lo <= lvl < c)
            else:
                cs = cH if s > 0 else cL
                odds = sum(1 for x in cs if x >= cnt) / len(cs) if cs else 1
                turn = i and ((c < S[i - 1][4]) if s > 0 else (c > S[i - 1][4]))
                hit = odds <= p["oddsMax"] and turn
            if hit:
                d = -s
                buf = p["bufTicks"] * TICK.get(sym, N.K.TICK)
                stop = ext + buf if d < 0 else ext - buf
                if (c - stop) * d > 0 and (piv - c) * d > 0:
                    sig = (d, stop, piv)
                    used_wave = wstart
        if not sig:
            continue
        d = sig[0]
        mac = si["macro"] or 0
        if p["macro"] == "with" and mac != d or p["macro"] == "against" and mac != -d:
            continue
        sd = si["stdev"]
        recent = [ser[j]["stdev"] for j in range(max(0, i - p["wave"]), i + 1)]
        if p["sqz"] == "after" and not any(v is not None and v < 0.1 for v in recent):
            continue
        if p["sqz"] == "no" and (sd is None or sd < 0.1):
            continue
        dst = 0
        if dk:
            j = bisect.bisect_left(dk, sday[i]) - 1           # the previous session's close: known before this one
            dst = ds[dk[j]] if j >= 0 else 0
            if p["daily"] == "with" and dst != d:
                continue
        hs = hst[i] if hst else None
        if hst is not None and hs != d:
            continue
        # target candidates (the wall is added at the trade, from the MM data); the first one beyond the entry is used
        po = pday.get(sday[i])
        dayl = None
        if po:
            beyond = [v for v in po if (v - c) * d > 0]
            dayl = (min(beyond) if d > 0 else max(beyond)) if beyond else None
        tg = {"level": sig[2], "pivot": piv if (piv - c) * d > 0 else None, "prev": lastH if d > 0 else lastL, "day": dayl}
        oax = None
        if p["oaExit"] and p["entry"] in ("cross", "vector", "late"):
            cs = cH if d > 0 else cL                        # waves on the trade's side
            if cs:
                n0 = cnt if (p["entry"] != "late") else 1   # a late entry rides the new wave from its start
                nstar = n0
                while nstar < 500 and sum(1 for x in cs if x >= nstar + 1) / len(cs) >= p["oaExit"]:
                    nstar += 1
                j = i + max(1, nstar - n0)
                oax = t[j][:16] if j < len(t) else None
        out[t[i]] = {"d": d, "stop": sig[1], "tgt": sig[2], "tg": tg, "oax": oax, "ent": p["entry"], "mac": mac == d, "sq": sd, "dst": dst, "cnt": cnt, "hs": hs}
    _SIG[key] = out
    return out


def _state(r_i, close, rule):
    if r_i is None or r_i["pivot"] is None:
        return None
    if rule == "side":
        return 1 if close > r_i["pivot"] else -1
    if rule == "dir":
        return r_i["direction"] or None
    if rule == "vector":
        return None if r_i["ma"] is None else (1 if close > r_i["ma"] else -1)
    return r_i["macro"] or None


def htf_states(sym, p, S, sday):
    """[+1 / -1 / None] per candle of S: the higher-timeframe fractal state at the last higher-timeframe candle that had
    finished before it (daily: the previous session's close)."""
    import futdata
    rule, wv = p["htfRule"], p["htfWave"]
    if p["htf"] == "D":
        D = stockdata.daily(sym) if stockdata.is_stock(sym) else futdata.daily(sym)
        if len(D) < 3:
            return [None] * len(S)
        ser = fractal.compute([[x[0], x[1], x[2], x[3], x[4]] for x in D], wv)["series"]
        dk = [x[0] for x in D]
        st = {x[0]: _state(ser[k], x[4], rule) for k, x in enumerate(D)}
        out = []
        for d in sday:
            j = bisect.bisect_left(dk, d) - 1             # the previous session
            out.append(st[dk[j]] if j >= 0 else None)
        return out
    tf = int(p["htf"])
    anchor = 570 if (p["hours"] == "rth" or stockdata.is_stock(sym)) else 1080     # 09:30 / 18:00 ET
    bk, H, pos = [], [], []
    for b, d in zip(S, sday):
        m = int(b[0][11:13]) * 60 + int(b[0][14:16])
        k = (d, ((m - anchor) % 1440) // tf)
        if not H or k != bk[-1]:
            H.append([b[0], b[1], b[2], b[3], b[4]])
            bk.append(k)
        else:
            x = H[-1]
            x[2], x[3], x[4] = max(x[2], b[2]), min(x[3], b[3]), b[4]
        pos.append(len(H) - 1)
    ser = fractal.compute(H, wv)["series"]
    st = [_state(ser[j], H[j][4], rule) for j in range(len(H))]
    return [st[j - 1] if j >= 1 else None for j in pos]      # the higher-timeframe candle before this one (finished)


def sm_levels(sym, p):
    """{day: {"close": previous close, "bull": {max, maxAvg -> "wall", median}, "bear": {...}}}: the Spread Monster
    1-day projection made after the previous session (its daily bars only)."""
    import futdata
    import volproj
    D = stockdata.daily(sym) if stockdata.is_stock(sym) else futdata.daily(sym)
    B = [[x[0], x[1], x[2], x[3], x[4]] for x in D]
    out = {}
    for k in range(60, len(B)):
        win = B[max(0, k - p["smDays"]):k]                  # up to the previous session
        try:
            r = volproj.project(win, 1, 1, p["smMatch"])
        except ValueError:
            continue
        pick = lambda side: {"max": side["levels"]["max"], "wall": side["levels"]["maxAvg"], "median": side["levels"]["median"]}
        out[B[k][0]] = {"close": r["price"], "bull": pick(r["bull"]), "bear": pick(r["bear"])}
    return out


def engine_for(sym, p, rows):
    S, sday = [], []
    for d, on, R, F, V in rows:
        xs = list(R) if p["hours"] == "rth" else list(F)
        S += xs
        sday += [d] * len(xs)
    pday, prev = {}, None                                # the previous session's regular-hours open / high / low
    for d, on, R, F, V in rows:
        if prev:
            pday[d] = (prev[0][1], max(b[2] for b in prev), min(b[3] for b in prev))
        prev = R or prev
    return signals(sym, p, S, [r[0] for r in rows], sday, pday)


def all_trades(sub, p, cells=None, prog=None):
    import random
    import ifvg
    trades, base = [], []
    rnd = random.Random(173)
    for sym in p["tickers"]:
        if prog:
            prog(f"{sym}: {p['tf']}-minute candles + the fractal")
        q = {**p, "days": p["days"] + p["stats"] + 5} if not p.get("_start") else p
        if stockdata.is_stock(sym):                     # QQQ / SPY: Alpaca 5-minute candles, regular hours
            rows, Fall, fi, E = stockdata.series(sym, q)
            p = {**p, "hours": "rth"}
        else:
            rows, Fall, fi, E = ifvg._series(sym, q)
        sig = engine_for(sym, p, rows)
        mm = mm_data(sub, sym)                          # also for the factor table (MM option volume at each entry)
        keep = set(d for d, *_ in rows[-p["days"]:]) if not p.get("_start") else set(d for d, *_ in rows if d >= p["_start"])
        for d, on, R, F, V in rows:
            if d not in keep or len(F) < 5:
                continue
            flat, last, rth0 = f"{d}T15:55", f"{d}T{p['lastEntry']}", f"{d}T09:35"
            k, n = 0, 0
            while k < len(F) - 1 and n < p["maxDay"]:
                t = F[k][0]
                x = sig.get(t)
                end = N.K.end_of(t, p["tf"])
                if not x or end[:16] > last or (p["hours"] == "rth" and end[:16] < rth0) or "16:00" <= t[11:16] < "18:00" or not N.ok_side(x["d"], 0, {**p, "bias": "none"}):
                    k += 1
                    continue
                c, dd = F[k][4], x["d"]
                risk = (c - x["stop"]) * dd
                if risk <= 0:
                    k += 1
                    continue
                row = mm_at(mm, d, end[11:16]) if mm else None
                mmv = mm_view(row, dd)
                if p["mm"] != "none" and not mm_ok(row, dd, p["mm"]):
                    k += 1
                    continue
                tg = dict(x["tg"])
                if row and row.get("price"):
                    ratio = c / float(row["price"])                 # QQQ / SPY strikes -> NQ / ES at this time
                    w = row.get("cw") if dd > 0 else row.get("pw")
                    tg["wall"] = float(w) * ratio if w else None
                if p["tgt"] == "r":
                    tgt, src = (None, "r") if p.get("tgtR") else (None, "none")
                else:
                    v = tg.get(p["tgt"])
                    tgt, src = (v, p["tgt"]) if v is not None and (v - c) * dd > 0 else (x["tgt"], "level (fallback)")
                    if tgt is None or (tgt - c) * dd <= 0:          # no target beyond the entry: no trade
                        k += 1
                        continue
                fl = min(flat, x["oax"]) if x.get("oax") and x["oax"] > t[:16] else flat
                tr, j = N.trade(F, k, dd, c, x["stop"], tgt, risk, p, E, fl, ent=x["ent"], mac=x["mac"], sq=x["sq"], dst=x["dst"], cnt=x["cnt"],
                                ts=src, mmv=mmv, htfs=x.get("hs"))
                tr.update(s=sym, d=d, cell=None)
                trades.append(tr)
                lo = 0 if p["hours"] == "all" else next((i for i, b in enumerate(F) if b[0] >= rth0), len(F))
                base.append((d, tr["side"], N.baseline(F, tr, rnd, p, E, flat, lo, last)))
                n += 1
                k = j + 1
    return trades, base


_MM = {}


def mm_data(sub, sym):
    """{day: [rows]} of the recorded / rebuilt MM data for the futures' ETF (NQ <- QQQ, ES <- SPY)."""
    import db
    base = {"NQ": "QQQ", "ES": "SPY"}.get(sym, sym)            # QQQ / SPY: their own data
    try:
        items = db.q_prefix(db.upk(sub), f"MM#{base}#")
    except Exception as e:
        print("mm data failed", e)
        return {}
    return {it["SK"].rsplit("#", 1)[1]: sorted(it.get("rows") or [], key=lambda r: r["t"]) for it in items}


def mm_at(mm, day, hm):
    """The last snapshot at or before hh:mm that day (None before 09:35 or without data)."""
    rows = mm.get(day) or []
    j = bisect.bisect_right([r["t"] for r in rows], hm) - 1
    return rows[j] if j >= 0 else None


def mm_ok(row, d, mode):
    """calls: long when calls at/above the price > calls below (buying pressure), short the reverse; puts: short when
    puts at/above > puts below (bearish divergence), long the reverse; both: both agree."""
    if not row:
        return False
    ca, cb, pa, pb = (float(row.get(k) or 0) for k in ("ca", "cb", "pa", "pb"))
    c_ok = (ca > cb) if d > 0 else (ca < cb)
    p_ok = (pa < pb) if d > 0 else (pa > pb)
    return c_ok if mode == "calls" else p_ok if mode == "puts" else c_ok and p_ok


def mm_view(row, d):
    if not row:
        return "no MM data"
    c, q = mm_ok(row, d, "calls"), mm_ok(row, d, "puts")
    return "calls + puts agree" if c and q else "calls agree" if c else "puts agree" if q else "against"


def decorate(x, p):
    N.decorate_common(x)
    x["ent"] = {"cross": "pivot cross", "vector": "vector cross", "fade": "level fade", "count": "wave count", "smfade": "Spread Monster fade", "late": "late counter-wave"}.get(x.get("ent"), x.get("ent"))
    x["mac"] = "with the Macro Trend" if x.get("mac") else "against it"
    v = x.get("sq")
    x["sq"] = "—" if v is None else "squeeze (< 0.1)" if v < 0.1 else "0.1-0.5" if v < 0.5 else "0.5+"
    x["dst"] = {1: "daily H", -1: "daily L"}.get(x.get("dst"), "—")
    x["htfs"] = {1: "HTF up", -1: "HTF down"}.get(x.get("htfs"), "—")
    c = x.get("cnt") or 0
    x["cnt"] = "1" if c <= 1 else "2-3" if c <= 3 else "4-7" if c <= 7 else "8+"


def overlay(sub, p, sym, start, end):
    return N.overlay(sub, p, sym, start, end)
