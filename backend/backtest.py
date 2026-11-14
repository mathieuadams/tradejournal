"""Earnings strategy backtest and optimizer: what the event study's rules would have made, in dollars.

Four legs, each optional, each with its own settings and a fixed stake per trade (default $1,000):
  A  After the call, buy at the open: the "after the call" model's out-of-sample prediction is in the top `cut` of the
     previous year's predictions, and the release isn't "bad news + gap up". Buy at the day-0 open, sell at the close.
  B  Fade "bad news + gap up": post-release score in the bottom `band` and a gap above `gap`. Short at the open, cover at
     the close.
  C  Post-release long: score in the top `band`. Buy at the day-0 close, hold up to `hold` sessions (5, 20, 40 or 60). Filters:
     the market weak going in (below its 200-day average or down over 3 months), day 0 up >2% on heavy volume (≥2.5×),
     the stock more than 25% below its 52-week high. Hedge: none, SPY 1:1 or beta × SPY.
  D  Post-release short: score in the bottom `band`. Short at the day-0 close, hold up to `hold` sessions. Filter: the
     market strong going in (above its 200-day average and up over 3 months). Hedge as C.

Entry for C and D: the day-0 close (default) or the day-0 open (`entry`; then day 0's own range counts for the stops).
Exits for C and D (on the stock's daily bars after day 0, from the study's price files): a stop `stop` below the entry,
a trailing stop `trail` below the best price since the entry (above, for a short), a profit target `target`; whichever
comes first, else the close of the last session. A stop is filled at the stop price, or at the open when the stock gaps
through it; on a day that touches both, the stop counts (the cautious reading).

Option equivalent (C: a call, D: a put): `dte` calendar days to expiry, strike `mny` out of the money (negative = in the
money), bought at the day-0 close for the stake and sold at the stock exit. There are no historical option quotes here:
the option is PRICED with Black-Scholes at the stock's own volatility (20-day, annualized) × `ivMult`, with a round-trip
spread of `optSpreadPct` % of the premium. It shows the leverage and the time decay, not real fills.

No look-ahead: the score uses signals ranked against earlier releases only, the "after the call" prediction is the
walk-forward (out-of-sample) one, and "top / bottom" is judged against the previous 365 days of releases.

The optimizer works like the Research one: every setting of a leg's grid is traded over the whole period, the trades
are split at a date (the last 5 years = test, the years before = train), a setting is ranked on the TRAIN trades
only (the t of the P/L per trade, at least MIN_TRADES trades), and the test columns show whether the ranking holds.
Costs: a round trip in basis points of the stake (default 10 = $1 per $1,000; doubled when hedged: the hedge is a second
trade), plus, for shorts, a borrow fee per year."""
import array
import bisect
import gzip
import itertools
import math
from datetime import date, timedelta

WINDOW = 365
MIN_PRIOR = 200
TEST_YEARS = 5                                        # the last 5 years = test, everything before = train
MIN_TRADES = 60


def _d(s):
    return date.fromisoformat(s[:10])


def trailing_pct(rows, key, when="day0"):
    """{index: percentile (0..1) of rows[i][key] among the previous WINDOW days' values}; None while fewer than
    MIN_PRIOR earlier values exist."""
    order = sorted((i for i, r in enumerate(rows) if r.get(key) is not None and r.get(when)), key=lambda i: rows[i][when])
    win, q, qi, out = [], [], 0, {}
    for i in order:
        w = rows[i][when]
        cut = (_d(w) - timedelta(days=WINDOW)).isoformat()
        while qi < len(q) and q[qi][0] < cut:
            v = q[qi][1]
            j = bisect.bisect_left(win, v)
            if j < len(win) and win[j] == v:
                win.pop(j)
            qi += 1
        v = rows[i][key]
        if len(win) >= MIN_PRIOR:
            lo, hi = bisect.bisect_left(win, v), bisect.bisect_right(win, v)
            out[i] = (lo + hi) / 2 / len(win)
        q.append((w, v))
        bisect.insort(win, v)
    return out


LEG_DEFAULT = {"A": {"on": True, "cut": 0.2},
               "B": {"on": True, "band": 0.2, "gap": 0.02},
               "C": {"on": True, "band": 0.2, "hold": 40, "hedge": "spy", "weak": True, "heavy": False, "far": False,
                     "entry": "close", "stop": 0.0, "trail": 0.0, "target": 0.0, "inst": "stock", "dte": 60, "mny": 0.0},
               "D": {"on": True, "band": 0.2, "hold": 40, "hedge": "spy", "strong": True,
                     "entry": "close", "stop": 0.0, "trail": 0.0, "target": 0.0, "inst": "stock", "dte": 60, "mny": 0.0}}
DEFAULT = {"stake": 1000.0, "costBps": 10.0, "borrowPct": 1.0, "ivMult": 1.0, "optSpreadPct": 5.0, "from": None, "to": None}
HOLDS = (5, 20, 40, 60)
HEDGES = ("none", "spy", "beta")
DTES = (30, 60, 90)


def clean(cfg):
    cfg = cfg or {}
    c = {**DEFAULT, **{k: v for k, v in cfg.items() if k in DEFAULT}}
    c["stake"] = max(10.0, min(1e7, float(c["stake"])))
    c["costBps"] = max(0.0, min(500.0, float(c["costBps"])))
    c["borrowPct"] = max(0.0, min(50.0, float(c["borrowPct"])))
    c["ivMult"] = max(0.3, min(3.0, float(c["ivMult"])))
    c["optSpreadPct"] = max(0.0, min(50.0, float(c["optSpreadPct"])))
    legs = {}
    old_legs, old_f = cfg.get("legs") or {}, cfg.get("filters") or {}
    for k, d in LEG_DEFAULT.items():
        v = old_legs.get(k)
        leg = {**d, **(v if isinstance(v, dict) else {"on": v} if isinstance(v, bool) else {})}
        if k in "CD":                                  # the earlier, shared settings
            if "hold" in cfg and not isinstance(v, dict):
                leg["hold"] = cfg["hold"]
            if "hedge" in cfg and not isinstance(v, dict):
                leg["hedge"] = cfg["hedge"]
        if k == "C" and old_f and not isinstance(v, dict):
            leg.update(weak=bool(old_f.get("cWeakMarket")), heavy=bool(old_f.get("cHeavyUp")), far=bool(old_f.get("cFarFromHigh")))
        if k == "D" and old_f and not isinstance(v, dict):
            leg["strong"] = bool(old_f.get("dStrongMarket"))
        leg["on"] = bool(leg["on"])
        for f in ("band", "cut"):
            if f in leg:
                leg[f] = max(0.05, min(0.5, float(leg[f])))
        if "gap" in leg:
            leg["gap"] = max(0.0, min(0.2, float(leg["gap"])))
        if "hold" in leg:
            leg["hold"] = int(leg["hold"]) if int(leg["hold"]) in HOLDS else 40
        if "hedge" in leg and leg["hedge"] not in HEDGES:
            leg["hedge"] = "spy"
        if k in "CD":
            for f in ("stop", "trail", "target"):
                leg[f] = max(0.0, min(0.9, float(leg.get(f) or 0)))
            leg["inst"] = "option" if leg.get("inst") == "option" else "stock"
            leg["entry"] = "open" if leg.get("entry") == "open" else "close"
            leg["dte"] = int(leg["dte"]) if int(leg.get("dte") or 0) in DTES else 60
            leg["mny"] = max(-0.3, min(0.3, float(leg.get("mny") or 0)))
        legs[k] = leg
    c["legs"] = legs
    return c


def _f(v):
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def _o2c(r):
    """Day-0 open → close of the stock alone (from the day-0 return and the gap)."""
    r1, gap = _f(r.get("r1")), _f(r.get("gap"))
    if r1 is None or gap is None or gap <= -1:
        return None
    return (1 + r1) / (1 + gap) - 1


def _hold(r, n, hedge):
    """Day-0 close → n sessions later: the stock alone, minus SPY, or minus beta × SPY."""
    r1, rn = _f(r.get("r1")), _f(r.get(f"r{n}"))
    if r1 is None or rn is None or r1 <= -1:
        return None
    raw = (1 + rn) / (1 + r1) - 1
    if hedge == "none":
        return raw
    ab = _f(r.get(f"drift{n}"))
    if hedge == "spy" or ab is None:
        return ab
    b = _f(r.get("beta"))
    return raw - (b if b is not None else 1.0) * (raw - ab)


# ---------------------------------------------------------------- daily paths after day 0 (for the exits)

STEPS = 61                                            # day 0, then 60 sessions after it
W = 6                                                 # per session: calendar days since day 0, open, high, low, close, SPY close
PATH_V = 2                                            # (day 0's slot holds SPY's day-0 open instead of its close)
MISSING = -32768


def _bp(x):
    return max(-32767, min(32767, int(round(x * 1e4))))


def path_of(bars, idx, d0, bmap, bopen=None):
    """One release's day 0 and the sessions after it as int16 values: [days, open, high, low, close, bench close] per
    session; prices in basis points from the day-0 close. Day 0's bench slot holds the bench's day-0 OPEN (its close
    is 0 by definition), for an entry at the open. None when day 0 isn't in the bars."""
    k0 = idx.get(d0)
    if k0 is None or not bars[k0][4]:
        return None
    c0, s0, t0 = bars[k0][4], bmap.get(d0), _d(d0)
    out = [MISSING] * (STEPS * W)
    for j in range(STEPS):
        k = k0 + j
        if k >= len(bars):
            break
        b = bars[k]
        s = (bopen or {}).get(b[0]) if j == 0 else bmap.get(b[0])
        out[j * W:(j + 1) * W] = [min(32767, (_d(b[0]) - t0).days), _bp(b[1] / c0 - 1), _bp(b[2] / c0 - 1), _bp(b[3] / c0 - 1), _bp(b[4] / c0 - 1),
                                  _bp(s / s0 - 1) if s and s0 else MISSING]
    return out


def path_key(r):
    return f"{r['sym']}|{r['day0']}"


def walk(P, off, short, n, stop, trail, target, max_days=None, at_open=False):
    """The exit of one position from its path: (session index, fill as a multiple of the ENTRY price, calendar days, bench
    move since the entry in bp or None, reason). Entry: the day-0 close, or the day-0 open (`at_open`: day 0's range then
    counts too). `n`: the sessions after day 0 (hold n = the close n-1 sessions after day 0, like the study's t<n>).
    None without data."""
    if P[off] == MISSING:
        return None
    e = 1 + P[off + 1] / 1e4 if at_open else 1.0
    se = (P[off + 5] if P[off + 5] != MISSING else None) if at_open else 0
    best, last, end = 1.0, None, "time"
    for j in range(0 if at_open else 1, n + 1):
        b = off + j * W
        dd = P[b]
        if dd == MISSING:
            end = "data"
            break
        if max_days is not None and dd > max_days:
            end = "expiry"
            break
        o, h, l, c = ((1 + P[b + 1] / 1e4) / e, (1 + P[b + 2] / 1e4) / e, (1 + P[b + 3] / 1e4) / e, (1 + P[b + 4] / 1e4) / e)
        if j == 0:                                     # entered at this open; SPY's day-0 close is 0 by definition
            o, s = 1.0, (-se if se is not None else None)
        else:
            s = P[b + 5] - se if P[b + 5] != MISSING and se is not None else None
        if not short:
            lvl, why = (1 - stop, "stop") if stop else (None, None)
            if trail and (lvl is None or best * (1 - trail) > lvl):
                lvl, why = best * (1 - trail), "trailing stop"
            if lvl is not None and l <= lvl:
                return j, min(o, lvl), dd, s, why
            if target and h >= 1 + target:
                return j, max(o, 1 + target), dd, s, "target"
            best = max(best, h)
        else:
            lvl, why = (1 + stop, "stop") if stop else (None, None)
            if trail and (lvl is None or best * (1 + trail) < lvl):
                lvl, why = best * (1 + trail), "trailing stop"
            if lvl is not None and h >= lvl:
                return j, max(o, lvl), dd, s, why
            if target and l <= 1 - target:
                return j, min(o, 1 - target), dd, s, "target"
            best = min(best, l)
        last = (j, c, dd, s, "time")
    return last if last is None or end == "time" else last[:4] + (end,)


R_FREE = 0.04


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, vol, call):
    """Black-Scholes price (no dividends) and delta."""
    if T <= 1e-6:
        return (max(0.0, S - K) if call else max(0.0, K - S)), ((1.0 if S > K else 0.0) if call else (-1.0 if S < K else 0.0))
    v = vol * math.sqrt(T)
    d1 = (math.log(S / K) + (R_FREE + vol * vol / 2) * T) / v
    d2 = d1 - v
    df = math.exp(-R_FREE * T)
    if call:
        return S * _ncdf(d1) - K * df * _ncdf(d2), _ncdf(d1)
    return K * df * _ncdf(-d2) - S * _ncdf(-d1), _ncdf(d1) - 1


def opt_vol(r, mult):
    v = _f(r.get("vol20"))
    return max(0.12, min(2.5, (v * math.sqrt(252) if v else 0.35) * mult))


def opt_label(L):
    m = L["mny"]
    k = "ATM" if abs(m) < 1e-9 else f"{abs(m) * 100:.0f}% {'OTM' if m > 0 else 'ITM'}"
    return f"{'call' if L.get('_leg') == 'C' else 'put'} {L['dte']}d {k}"


_PATHS = {}


def load_paths(sub):
    """The saved paths: (index {key: offset}, int16 array) or None. Kept in the container between calls."""
    import store
    meta = store.get_json(f"fund/u/{sub}/paths_meta")
    if not meta:
        return None
    hit = _PATHS.get(sub)
    if hit and hit[0] == meta.get("at"):
        return hit[1]
    raw = store.get_raw(f"fund/u/{sub}/paths.bin.gz")
    if not raw:
        return None
    a = array.array("h")
    a.frombytes(gzip.decompress(raw))
    out = ({k: i * STEPS * W for i, k in enumerate(meta["keys"])}, a)
    _PATHS.clear()
    _PATHS[sub] = (meta.get("at"), out)
    return out


def paths_ready(sub, rows):
    import store
    meta = store.get_json(f"fund/u/{sub}/paths_meta") or {}
    return bool(rows) and meta.get("n") == len(rows) and meta.get("last") == path_key(rows[-1]) and meta.get("v") == PATH_V


def build_paths(sub, rows, prog=None):
    """Every release's sessions after day 0, from the study's price files (fund/px/<SYM>, already downloaded)."""
    import time
    import fundamentals as F
    import store
    from util import iso, now_ny
    st = F.settings(sub)
    first = min(r["day0"] for r in rows if r.get("day0"))
    need = (_d(first) - timedelta(days=10)).isoformat()
    bench = F.prices(sub, st.get("bench") or "SPY", need, max_age_h=72)
    bmap = {b[0]: b[4] for b in bench}
    bopen = {b[0]: b[1] for b in bench}
    by_sym = {}
    for i, r in enumerate(rows):
        if r.get("day0"):
            by_sym.setdefault(r["sym"], []).append(i)
    keys, a, done, t_last = [], array.array("h"), set(), 0.0
    for n, (sym, ix) in enumerate(sorted(by_sym.items())):
        if prog and time.time() - t_last > 3:
            prog(f"Daily paths for the exits: {n + 1}/{len(by_sym)} {sym}")
            t_last = time.time()
        bars = (store.get_json("fund/px/" + sym) or {}).get("bars")
        if not bars:
            try:
                bars = F.prices(sub, sym, need, max_age_h=24 * 365)
            except Exception:
                continue
        idx = {b[0]: k for k, b in enumerate(bars)}
        for i in ix:
            k = path_key(rows[i])
            if k in done:
                continue
            p = path_of(bars, idx, rows[i]["day0"], bmap, bopen)
            if p:
                done.add(k)
                keys.append(k)
                a.extend(p)
    store.put_raw(f"fund/u/{sub}/paths.bin.gz", gzip.compress(a.tobytes(), 6), "application/gzip")
    store.put_json(f"fund/u/{sub}/paths_meta", {"v": PATH_V, "at": iso(now_ny()), "n": len(rows), "last": path_key(rows[-1]), "keys": keys})
    _PATHS.clear()
    return len(keys)


def needs_paths(c):
    return any(c["legs"][k]["on"] and (c["legs"][k]["stop"] or c["legs"][k]["trail"] or c["legs"][k]["target"] or
                                       c["legs"][k]["inst"] == "option" or c["legs"][k]["hold"] == 60 or c["legs"][k]["entry"] == "open") for k in "CD")


def context(rows):
    """What every setting shares: the trailing percentiles of the score and of the model's prediction."""
    last = max((r["day0"] for r in rows if r.get("day0")), default=None)
    return {"score": trailing_pct(rows, "postScore"), "pred": trailing_pct(rows, "oosO2c"),
            "openAfter": (_d(last) - timedelta(days=7)).isoformat() if last else None}


def trades_for(rows, ctx, c, only=None, paths=None, keep_open=False):
    """The trades of the enabled legs (or of one leg, `only`), oldest first. `paths`: load_paths(), for the exits.
    A position whose planned exit is after the last price (still open today) is marked "open" and left out unless
    `keep_open`: it isn't a result yet."""
    stake, legs = c["stake"], c["legs"]
    base_cost, borrow_y = c["costBps"] / 1e4, c["borrowPct"] / 100
    score_p, pred_p = ctx["score"], ctx["pred"]
    out = []
    want = [only] if only else [k for k in "ABCD" if legs[k]["on"]]
    for i, r in enumerate(rows):
        d0 = r.get("day0")
        if not d0 or (c["from"] and d0 < c["from"]) or (c["to"] and d0 > c["to"]):
            continue
        sp, pp = score_p.get(i), pred_p.get(i)
        gap = _f(r.get("gap"))

        def add(leg, side, ret, exit_d, days, cost, why, xret=None):
            if ret is None or ret <= -1:
                return
            borrow = borrow_y * days / 365 if side == "short" else 0.0
            sg = 1 if side == "long" else -1
            t = {"leg": leg, "sym": r["sym"], "release": r["date"], "entry": d0, "exit": exit_d, "side": side, "ret": round(ret, 6),
                 "res": round(sg * ret, 6), "pnl": round(stake * (sg * ret - cost - borrow), 2), "why": why}
            if xret is not None:                       # the same trade's result beyond the market (vs SPY, as if hedged)
                xcost = base_cost * (2 if leg in "CD" else 1)
                t["alpha"] = round(stake * (sg * xret - xcost - borrow), 2)
            out.append(t)

        for k in want:
            L = legs[k]
            if k == "A":
                bad = sp is not None and sp <= legs["B"]["band"] and gap is not None and gap > legs["B"]["gap"]
                if pp is not None and pp >= 1 - L["cut"] and not bad:
                    add("A", "long", _o2c(r), d0, 1, base_cost, f"after-call model top {100 - pp * 100:.0f}%", _f(r.get("o2c")))
            elif k == "B":
                if sp is not None and sp <= L["band"] and gap is not None and gap > L["gap"]:
                    add("B", "short", _o2c(r), d0, 1, base_cost, f"score bottom {sp * 100:.0f}%, gap {gap * 100:+.1f}%", _f(r.get("o2c")))
            else:
                if sp is None:
                    continue
                if k == "C":
                    if sp < 1 - L["band"]:
                        continue
                    ok = True
                    if L["weak"]:
                        ok &= (_f(r.get("spyVs200")) or 0) < 0 or (_f(r.get("spy63")) or 0) < 0
                    if L["heavy"]:
                        ok &= (_f(r.get("r1ab")) or 0) > 0.02 and (_f(r.get("vol0")) or 0) >= 2.5
                    if L["far"]:
                        ok &= (_f(r.get("offHigh")) or 0) < -0.25
                    why = f"score top {100 - sp * 100:.0f}%"
                else:
                    if sp > L["band"]:
                        continue
                    ok = not L["strong"] or ((_f(r.get("spyVs200")) or 0) > 0 and (_f(r.get("spy63")) or 0) > 0)
                    why = f"score bottom {sp * 100:.0f}%"
                if ok:
                    hold_trade(k, L, r, d0, why, add, paths, c, out, ctx.get("openAfter"))
    return out if keep_open else [t for t in out if not t.get("open")]


def hold_trade(k, L, r, d0, why, add, paths, c, out, open_after=None):
    """C or D: from the day-0 close to the first exit (stop, trailing stop, target, the last session; an option's expiry)."""
    n, short = L["hold"], k == "D"
    side = "short" if short else "long"
    base_cost = c["costBps"] / 1e4
    at_open = L["entry"] == "open"
    rules = L["stop"] or L["trail"] or L["target"] or L["inst"] == "option" or n == 60 or at_open
    off = paths[0].get(path_key(r)) if paths else None
    if off is None:
        if rules:                                      # needs the daily path, and this release has none
            return
        exit_d = r.get(f"t{n}")
        if not exit_d:
            return
        cost = base_cost * (2 if L["hedge"] != "none" else 1)
        add(k, side, _hold(r, n, L["hedge"]), exit_d, (_d(exit_d) - _d(d0)).days, cost, why, _hold(r, n, "spy"))
        return
    P = paths[1]
    opt = L["inst"] == "option"
    w = walk(P, off, short, max(1, n - 1), L["stop"], L["trail"], L["target"], L["dte"] if opt else None, at_open)
    if not w:
        return
    j, fill, dd, s, reason = w
    exit_d = (_d(d0) + timedelta(days=dd)).isoformat()
    still_open = reason == "data" and open_after is not None and exit_d >= open_after   # the data ends: today, not an exit
    raw, spy = fill - 1, (s / 1e4 if s is not None else None)
    tag = (" · in at the open" if at_open else "") + ("" if reason in ("time", "data") else f" · {reason}")
    if not opt:
        if L["hedge"] == "none":
            ret = raw
        elif spy is None:
            return
        elif L["hedge"] == "spy":
            ret = raw - spy
        else:
            b = _f(r.get("beta"))
            ret = raw - (b if b is not None else 1.0) * spy
        cost = base_cost * (2 if L["hedge"] != "none" else 1)
        n0 = len(out)
        add(k, side, ret, exit_d, dd, cost, why + tag, raw - spy if spy is not None else None)
        if still_open and len(out) > n0:
            out[-1]["open"] = True
        return
    vol, call = opt_vol(r, c["ivMult"]), not short
    K = 1 + L["mny"] if call else 1 - L["mny"]
    p0, delta = bs(1.0, K, L["dte"] / 365, vol, call)
    if p0 <= 1e-4:
        return
    p1, _ = bs(fill, K, max(0.0, (L["dte"] - dd) / 365), vol, call)
    ret = p1 / p0 - 1
    spread = c["optSpreadPct"] / 100
    lab = opt_label({**L, "_leg": k})                 # strike and premium relative to the entry price (fill is too)
    t = {"leg": k, "sym": r["sym"], "release": r["date"], "entry": d0, "exit": exit_d, "side": lab, "ret": round(raw, 6), "res": round(ret, 6),
         "pnl": round(c["stake"] * (ret - spread), 2), "why": f"{why} · {lab} at {vol * 100:.0f}% vol" + tag}
    if spy is not None:                                # beyond the market: minus what the option's delta made on SPY's move
        t["alpha"] = round(c["stake"] * (ret - delta / p0 * spy - spread), 2)
    if still_open:
        t["open"] = True
    out.append(t)


def split_date(rows):
    """The start of the test period: TEST_YEARS before the last release (60% of the period if it's shorter than 7 years)."""
    ds = sorted(r["day0"] for r in rows if r.get("day0"))
    if not ds:
        return None
    a, b = _d(ds[0]), _d(ds[-1])
    if (b - a).days >= 365.25 * (TEST_YEARS + 2):
        return b.replace(year=b.year - TEST_YEARS).isoformat()
    return (a + timedelta(days=int((b - a).days * 0.6))).isoformat()


def run(rows, cfg, ctx=None, paths=None):
    c = clean(cfg)
    ctx = ctx or context(rows)
    allt = trades_for(rows, ctx, c, paths=paths, keep_open=True)
    opens = [t for t in allt if t.get("open")]
    out = summarize([t for t in allt if not t.get("open")], c)
    out["open"] = {"n": len(opens), "mtm": round(sum(t["pnl"] for t in opens), 2),
                   "trades": sorted(opens, key=lambda t: t["entry"])}
    out["alpha"] = _stats(out["_all"], "alpha")
    sd = split_date(rows)
    if sd:
        tr = [t for t in out["_all"] if t["entry"] < sd]
        te = [t for t in out["_all"] if t["entry"] >= sd]
        out["split"] = {"date": sd, "train": _stats(tr), "test": _stats(te), "trainX": _stats(tr, "alpha"), "testX": _stats(te, "alpha")}
    out.pop("_all", None)
    return out


def _stats(ts, key="pnl"):
    if not ts:
        return {"n": 0}
    p = [t[key] for t in ts if t.get(key) is not None]
    if not p:
        return {"n": 0}
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]
    m = sum(p) / len(p)
    sd = math.sqrt(sum((x - m) ** 2 for x in p) / (len(p) - 1)) if len(p) > 1 else 0
    cum = peak = dd = 0.0
    for x in sorted(ts, key=lambda t: t["exit"]):
        cum += x.get(key) or 0
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": len(p), "total": round(sum(p), 2), "avg": round(m, 2), "win": len(wins) / len(p),
            "avgWin": round(sum(wins) / len(wins), 2) if wins else None, "avgLoss": round(sum(losses) / len(losses), 2) if losses else None,
            "profitFactor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "best": round(max(p), 2), "worst": round(min(p), 2), "t": round(m / (sd / math.sqrt(len(p))), 2) if sd else None,
            "dd": round(dd, 2)}


def summarize(trades, c):
    trades.sort(key=lambda t: (t["exit"], t["entry"], t["sym"]))
    eq, cum, peak, dd, dd_at = [], 0.0, 0.0, 0.0, None
    by_day = {}
    for t in trades:
        by_day[t["exit"]] = by_day.get(t["exit"], 0.0) + t["pnl"]
    for d in sorted(by_day):
        cum += by_day[d]
        peak = max(peak, cum)
        if peak - cum > dd:
            dd, dd_at = peak - cum, d
        eq.append([d, round(cum, 2)])
    ev = []                                            # capital in use: open positions over time
    for t in trades:
        ev.append((t["entry"], 0, 1))
        ev.append(((_d(t["exit"]) + timedelta(days=1)).isoformat(), 0, -1))     # held through the exit day's close
    ev.sort(key=lambda e: (e[0], e[2]))                # same day: positions closed before new ones count
    open_n, max_open, max_at, area, last_d = 0, 0, None, 0.0, None
    for d, _, k in ev:
        if last_d is not None:
            area += open_n * (_d(d) - _d(last_d)).days
        last_d = d
        open_n += k
        if open_n > max_open:
            max_open, max_at = open_n, d
    span = (_d(ev[-1][0]) - _d(ev[0][0])).days if ev else 0
    avg_open = area / span if span else 0
    years, legs = {}, {}
    for t in trades:
        years.setdefault(t["exit"][:4], []).append(t)
        legs.setdefault(t["leg"], []).append(t)
    st = _stats(trades)
    first, last = (eq[0][0], eq[-1][0]) if eq else (None, None)
    yrs = max(1e-9, ((_d(last) - _d(first)).days / 365.25)) if eq else None
    return {"cfg": c, **st, "maxDrawdown": round(dd, 2), "maxDrawdownAt": dd_at, "maxOpen": max_open, "maxOpenAt": max_at,
            "capitalNeeded": round(max_open * c["stake"], 2), "avgOpen": round(avg_open, 1), "avgCapital": round(avg_open * c["stake"], 2),
            "from": first, "to": last,
            "perYear": round(st.get("total", 0) / yrs, 2) if yrs else None,
            "returnOnCapital": round(st.get("total", 0) / (max_open * c["stake"]), 4) if max_open else None,
            "returnOnAvgCapitalPerYear": round(st.get("total", 0) / yrs / (avg_open * c["stake"]), 4) if yrs and avg_open else None,
            "years": [{"year": y, **_stats(ts)} for y, ts in sorted(years.items())],
            "legs": [{"leg": k, **_stats(v)} for k, v in sorted(legs.items())],
            "equity": eq[::max(1, len(eq) // 600)] + ([eq[-1]] if eq and len(eq) > 600 else []),
            "trades": trades[-3000:], "nTrades": len(trades), "_all": trades}


# ---------------------------------------------------------------- the optimizer

GRID = {                                              # stage 1: the entries (exits: hold only, the stock)
    "A": {"cut": [0.1, 0.2, 0.3]},
    "B": {"band": [0.1, 0.2, 0.3], "gap": [0.01, 0.02, 0.04]},
    "C": {"band": [0.1, 0.2, 0.3], "hold": [5, 20, 40], "hedge": ["none", "spy", "beta"], "weak": [False, True], "heavy": [False, True],
          "far": [False, True]},
    "D": {"band": [0.1, 0.2, 0.3], "hold": [5, 20, 40], "hedge": ["none", "spy", "beta"], "strong": [False, True]},
}
EXIT_GRID = {"entry": ["close", "open"], "hold": [20, 40, 60], "stop": [0.0, 0.05, 0.1, 0.15, 0.2], "trail": [0.0, 0.05, 0.1, 0.15, 0.2],
             "target": [0.0, 0.1, 0.2, 0.3]}            # stage 2: the exits, on stage 1's best entry
OPT_GRID = {"dte": [30, 60, 90], "mny": [-0.05, 0.0, 0.05]}   # stage 3: the option, on stage 2's best exits
STAGES = {1: "entry", 2: "exits", 3: "option"}


def _grid(g):
    keys = list(g)
    return [dict(zip(keys, v)) for v in itertools.product(*[g[k] for k in keys])]


def combos(leg):
    return _grid(GRID[leg])


def n_combos():
    return sum(len(combos(k)) for k in GRID) + 2 * (len(_grid(EXIT_GRID)) + len(_grid(OPT_GRID)))


def _sig(leg, stage, p):
    return (leg, stage, tuple(sorted(p.items())))


def _best(res, leg, stage, on="alpha"):
    xs = ranked([x for x in res if x.get("stage", 1) == stage], on).get(leg) or []
    return xs[0]["p"] if xs else None


def optimize(rows, cfg, prog=None, deadline=None, done=None, paths=None):
    """Stage 1: every entry setting of every leg; stage 2 (C, D): every exit (hold, stop, trailing stop, target) on stage
    1's best entry; stage 3: the option equivalent of stage 2's best. Train / test split at split_date(), each stage
    ranked on train only. `done`: rows already computed (a resumed run). Returns (rows, complete)."""
    import time
    c = clean(cfg)
    ctx = context(rows)
    sd = split_date(rows)
    res = list(done or [])
    seen = {_sig(x["leg"], x.get("stage", 1), x["p"]) for x in res}
    total, count = n_combos(), [len(res)]

    def test(leg, stage, p):
        if _sig(leg, stage, p) in seen:
            return True
        if deadline and time.time() > deadline:
            return False
        if prog and count[0] % 10 == 0:
            prog(f"Optimizing ({STAGES[stage]}): setting {count[0] + 1} of {total}")
        trial = {**c, "legs": {**c["legs"], leg: {**c["legs"][leg], **p, "on": True}}}
        ts = trades_for(rows, ctx, trial, only=leg, paths=paths)
        tr, te = [t for t in ts if t["entry"] < sd], [t for t in ts if t["entry"] >= sd]
        res.append({"leg": leg, "stage": stage, "p": p, "train": _stats(tr), "test": _stats(te), "trainX": _stats(tr, "alpha"),
                    "testX": _stats(te, "alpha")})
        seen.add(_sig(leg, stage, p))
        count[0] += 1
        return True

    for leg in "ABCD":
        for p in combos(leg):
            if not test(leg, 1, p):
                return res, False
    if not paths:
        return res, True
    for leg in "CD":
        base = {**LEG_DEFAULT[leg], **(_best(res, leg, 1) or {})}
        base.pop("on", None)
        for e in _grid(EXIT_GRID):
            if not test(leg, 2, {**base, **e, "inst": "stock"}):
                return res, False
    for leg in "CD":
        base = _best(res, leg, 2)
        if not base:
            continue
        for o in _grid(OPT_GRID):
            if not test(leg, 3, {**base, **o, "inst": "option"}):
                return res, False
    return res, True


def ranked(res, on="alpha"):
    """Per leg, best first on the train t (settings with fewer than MIN_TRADES train trades last). on="alpha": the profit
    beyond the market (the same trades vs SPY) — an unhedged long in a rising market otherwise wins on the market alone;
    on="pnl": the profit as traded."""
    k = "trainX" if on == "alpha" else "train"
    out = {}
    for leg in "ABCD":
        xs = [x for x in res if x["leg"] == leg]
        xs.sort(key=lambda x: ((x["train"].get("n") or 0) >= MIN_TRADES, round((x.get(k) or {}).get("t") or -1e9, 2),
                              x["train"].get("t") or -1e9), reverse=True)   # ties (hedge choices): the better as traded
        out[leg] = xs
    return out


def ready(rows):
    return bool(rows) and any("postScore" in r for r in rows[-500:])


def prepare(sub, prog=None, paths=True):
    """A study run before the backtest existed: score its releases now (the walk-forward model fills the post-release
    score and the after-the-call predictions) and save them; then the daily paths after day 0 (for the exits)."""
    import model
    import store
    from util import BadRequest
    rows = (store.get_json(f"fund/u/{sub}/study_rows") or {}).get("rows") or []
    if not rows:
        raise BadRequest("Run the event study first.")
    if not ready(rows):
        if prog:
            prog(f"Scoring {len(rows)} releases for the backtest…")
        mdl = model.evaluate(rows, prog)
        store.put_json(f"fund/u/{sub}/study_rows", {"rows": rows})
        sd = store.get_json(f"fund/u/{sub}/study")
        if sd:
            sd["model"] = mdl
            store.put_json(f"fund/u/{sub}/study", sd)
    if paths and not paths_ready(sub, rows):
        build_paths(sub, rows, prog)
    return rows


def opt_job(sub, prog=None, deadline=None):
    """The optimizer over the last study's releases (a background job); results in fund/u/<sub>/btopt."""
    import store
    from util import iso, now_ny
    rows = prepare(sub, prog)
    prev = store.get_json(f"fund/u/{sub}/btopt") or {}
    cfg = prev.get("cfg") or {}
    res, ok = optimize(rows, cfg, prog, deadline, None if prev.get("complete") else prev.get("res"), load_paths(sub))
    store.put_json(f"fund/u/{sub}/btopt", {"at": iso(now_ny()), "cfg": clean(cfg), "split": split_date(rows), "res": res, "complete": ok,
                                           "grid": GRID, "exitGrid": EXIT_GRID, "optGrid": OPT_GRID, "total": n_combos()})
    return ok


def opt_view(sub, on="alpha", top=12):
    import store
    o = store.get_json(f"fund/u/{sub}/btopt")
    if not o:
        return None
    res = o["res"]
    legs, best = {}, {}
    for st in STAGES:
        rk = ranked([x for x in res if x.get("stage", 1) == st], on)
        for k, v in rk.items():
            if v:
                legs[k if st == 1 else f"{k}{st}"] = v[:top]
    key = "trainX" if on == "alpha" else "train"
    for k in "ABCD":                                   # the best of each leg: a later stage only if it beats the earlier on train
        cand = [legs[x][0] for x in (k, f"{k}2", f"{k}3") if legs.get(x)]
        cand = [x for x in cand if (x["train"].get("n") or 0) >= MIN_TRADES] or cand
        if cand:
            best[k] = max(cand, key=lambda x: (x.get(key) or {}).get("t") or -1e9)["p"]
    return {k: v for k, v in o.items() if k != "res"} | {"on": on, "minTrades": MIN_TRADES, "legs": legs, "best": best, "done": len(res)}
