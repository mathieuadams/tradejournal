"""Beat the Market (Zarattini, Aziz, Barbon, 2024–25): intraday momentum on SPY.

Noise area of day t, for each time of day HH:MM: σ = the average over the previous 14 days of |close(HH:MM) / open(9:30)
− 1|. Upper bound = max(open, yesterday's close) × (1 + VM × σ), lower bound = min(open, yesterday's close) × (1 − VM × σ).
Decisions only every 30 minutes (10:00, 10:30, … 15:30; 15 / 60 as options), at that minute's close:
  above the upper bound → long, below the lower bound → short;
  stop (checked at the same times): "opp" — the opposite bound (then reverse); "band" — the current bound;
  "vwap" — the current bound or the session VWAP, whichever is tighter (the paper's final model: long only while above
  both, short only while below both). Everything is closed at 16:00 (the last minute's close).
Size: 100% of the account at the open, or the paper's volatility target: account × min(4, 2% ÷ σ of SPY's last 14
daily returns) ÷ open. Costs: $0.0035 commission + $0.001 slippage a share, every trade. VIX (FMP, at the open):
the paper's regime table (Sharpe on days the VIX opens above a level) and an optional filter.
Data: SPY 1-minute bars from Alpaca (SIP since 2016 — the paper ran 2007–2024 on IQFeed), stored by month."""
import itertools
import math
import time
from datetime import date, datetime, timedelta

import store
from util import BadRequest, iso, now_ny, utc_to_ny, ny_to_utc

SYM = "SPY"
FROM = "2016-01"
LOOK = 14
MARKS = 26                                  # 15-minute marks: 9:45 … 16:00
TEST_YEARS = 5
DEFAULT = {"vm": 1.0, "stop": "vwap", "sizing": "dyn", "target": 0.02, "maxLev": 4.0, "step": 30, "vixMin": 0.0, "vixMax": 0.0,
           "acct": 100000.0, "comm": 0.0035, "slip": 0.001, "sides": "both"}
GRID = {"vm": [1.0, 1.25, 1.5, 2.0], "stop": ["opp", "band", "vwap"], "sizing": ["full", "dyn"], "step": [15, 30, 60], "vixMin": [0.0, 15.0, 20.0]}


def clean(cfg):
    c = {**DEFAULT, **{k: v for k, v in (cfg or {}).items() if k in DEFAULT}}
    c["vm"] = max(0.25, min(4.0, float(c["vm"])))
    c["stop"] = c["stop"] if c["stop"] in ("opp", "band", "vwap") else "vwap"
    c["sizing"] = "full" if c["sizing"] == "full" else "dyn"
    c["target"] = max(0.002, min(0.1, float(c["target"])))
    c["maxLev"] = max(0.5, min(8.0, float(c["maxLev"])))
    c["step"] = int(c["step"]) if int(c["step"]) in (15, 30, 60) else 30
    c["vixMin"], c["vixMax"] = max(0.0, float(c["vixMin"])), max(0.0, float(c["vixMax"]))
    c["acct"] = max(1000.0, float(c["acct"]))
    c["comm"], c["slip"] = max(0.0, float(c["comm"])), max(0.0, float(c["slip"]))
    c["sides"] = c["sides"] if c["sides"] in ("both", "long", "short") else "both"
    return c


# ---------------------------------------------------------------- data

def _months(a, b):
    y, m = map(int, a.split("-"))
    while f"{y:04d}-{m:02d}" <= b:
        yield f"{y:04d}-{m:02d}"
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def _fetch_month(sub, ym):
    """{date: [[minute from 9:30, o, h, l, c, v], …]} of the regular session (Alpaca 1Min, split-adjusted)."""
    import alpaca
    c, feed = alpaca.creds(sub), alpaca.status(sub)["feed"]
    y, m = map(int, ym.split("-"))
    t0 = ny_to_utc(datetime(y, m, 1, 9, 30))
    t1 = ny_to_utc(datetime(y + (m == 12), m % 12 + 1, 1))
    t1 = min(t1, datetime.utcnow() - timedelta(minutes=16 if feed == "sip" else 0))
    out, token = {}, None
    for _ in range(200):
        r = alpaca._get(c, f"stocks/{SYM}/bars", {"timeframe": "1Min", "start": t0.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": t1.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                   "limit": 10000, "adjustment": "split", "feed": feed, "sort": "asc", "page_token": token})
        for b in r.get("bars") or []:
            t = utc_to_ny(datetime.strptime(b["t"][:19], "%Y-%m-%dT%H:%M:%S"))
            k = (t.hour - 9) * 60 + t.minute - 30
            if 0 <= k < 390:
                out.setdefault(t.strftime("%Y-%m-%d"), []).append([k, b["o"], b["h"], b["l"], b["c"], b.get("v", 0)])
        token = r.get("next_page_token")
        if not token:
            break
    return out


def _vix(sub):
    """Daily VIX [date, open, close] from FMP (^VIX); [] without FMP."""
    import fmp
    try:
        raw = fmp._get(fmp._key(sub), "historical-price-eod/full", {"symbol": "^VIX", "from": "2015-11-01"}) or []
    except BadRequest:
        return []
    rows = raw if isinstance(raw, list) else (raw.get("historical") or [])
    out = sorted([str(r.get("date"))[:10], float(r.get("open") or r.get("close")), float(r.get("close"))] for r in rows if r.get("date") and (r.get("open") or r.get("close")))
    return out


def download(sub, prog=None, deadline=None):
    """Every month since 2016 not stored yet (the last two are always fetched again), then VIX and the day table.
    Returns False when out of time (call again)."""
    now = now_ny().strftime("%Y-%m")
    months = list(_months(FROM, now))
    have = (store.get_json(f"im/{SYM}/months") or {}).get("m") or {}
    for ym in months:
        if ym in have and ym < months[-2]:
            continue
        if deadline and time.time() > deadline:
            store.put_json(f"im/{SYM}/months", {"m": have})
            return False
        if prog:
            prog(f"SPY 1-minute bars: {ym}")
        d = _fetch_month(sub, ym)
        store.put_json(f"im/{SYM}/1m/{ym}", d)
        have[ym] = len(d)
    store.put_json(f"im/{SYM}/months", {"m": have})
    if prog:
        prog("VIX (FMP) and the day table…")
    store.put_json("im/VIX", {"rows": _vix(sub), "at": iso(now_ny())})
    prep(have)
    return True


def prep(have=None):
    """The day table: [date, open, last close, [close at each 15-minute mark], [VWAP at each mark], last minute]."""
    have = have or (store.get_json(f"im/{SYM}/months") or {}).get("m") or {}
    days = []
    for ym in sorted(have):
        d = store.get_json(f"im/{SYM}/1m/{ym}") or {}
        for day in sorted(d):
            bars = sorted(d[day])
            if len(bars) < 60 or bars[0][0] > 5:
                continue                                # a broken day
            by = {b[0]: b for b in bars}
            cl, vw, pv, vv, last = [None] * MARKS, [None] * MARKS, 0.0, 0.0, None
            for k in range(390):
                b = by.get(k)
                if b:
                    tp = (b[2] + b[3] + b[4]) / 3
                    pv += tp * b[5]
                    vv += b[5]
                    last = b
                if (k + 1) % 15 == 0 and last and last[0] >= k - 14:
                    j = (k + 1) // 15 - 1
                    cl[j] = last[4]
                    vw[j] = pv / vv if vv else last[4]
            days.append([day, bars[0][1], bars[-1][4], cl, vw, bars[-1][0]])
    store.put_json(f"im/{SYM}/prep", {"days": days, "at": iso(now_ny())})
    return days


def status():
    p = store.get_json(f"im/{SYM}/prep") or {}
    d = p.get("days") or []
    v = (store.get_json("im/VIX") or {}).get("rows") or []
    return {"days": len(d), "first": d[0][0] if d else None, "last": d[-1][0] if d else None, "at": p.get("at"),
            "vix": len(v), "vixFirst": v[0][0] if v else None, "vixLast": v[-1][0] if v else None}


# ---------------------------------------------------------------- the simulation

def context(days, vix_rows):
    """Per day: σ at each mark (the 14 days before), σ of SPY's daily returns, the VIX open, the 5-day RSI before."""
    vix = {r[0]: r[1] for r in vix_rows}
    sig, vol, rsi = [None] * len(days), [None] * len(days), [None] * len(days)
    rets = [None] + [days[i][2] / days[i - 1][2] - 1 for i in range(1, len(days))]
    for i in range(LOOK, len(days)):
        o = days[i][1]
        s = []
        for j in range(MARKS):
            xs = [abs(days[i - k][3][j] / days[i - k][1] - 1) for k in range(1, LOOK + 1) if days[i - k][3][j] is not None]
            s.append(sum(xs) / len(xs) if xs else None)
        sig[i] = s
        r = [x for x in rets[i - LOOK:i] if x is not None]
        if len(r) > 2:
            m = sum(r) / len(r)
            vol[i] = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
        up = sum(max(0, days[k][2] - days[k - 1][2]) for k in range(i - 5, i))
        dn = sum(max(0, days[k - 1][2] - days[k][2]) for k in range(i - 5, i))
        rsi[i] = 100 if dn == 0 else 100 - 100 / (1 + up / dn)
    return {"sig": sig, "vol": vol, "vix": [vix.get(d[0]) for d in days], "rsi": rsi}


def simulate(days, ctx, c, keep_trades=False):
    aum = c["acct"]
    daily, trades = [], []
    for i in range(LOOK, len(days)):
        day, o, last, cl, vw, lastk = days[i]
        prev, s, vx = days[i - 1][2], ctx["sig"][i], ctx["vix"][i]
        if (c["vixMin"] and (vx is None or vx < c["vixMin"])) or (c["vixMax"] and (vx is None or vx > c["vixMax"])):
            daily.append([day, 0.0, aum, 0, vx])
            continue
        lev = 1.0 if c["sizing"] == "full" else (min(c["maxLev"], c["target"] / ctx["vol"][i]) if ctx["vol"][i] else 1.0)
        sh = math.floor(aum * lev / o)
        hi_ref, lo_ref = max(o, prev), min(o, prev)
        pos, entry, et, pnl, n = 0, 0.0, None, 0.0, 0
        cost = c["comm"] + c["slip"]
        every = c["step"] // 15
        for j in range(every - 1, MARKS - 1, every):  # the checkpoints before 16:00
            p = cl[j]
            if p is None or s[j] is None:
                continue
            ub, lb = hi_ref * (1 + c["vm"] * s[j]), lo_ref * (1 - c["vm"] * s[j])
            if c["stop"] == "vwap":
                want = 1 if p > max(ub, vw[j]) else -1 if p < min(lb, vw[j]) else 0
            elif c["stop"] == "band":
                want = 1 if p > ub else -1 if p < lb else 0
            else:
                want = pos if pos else (1 if p > ub else -1 if p < lb else 0)
                if pos == 1 and p < lb:
                    want = -1
                elif pos == -1 and p > ub:
                    want = 1
            if (want == 1 and c["sides"] == "short") or (want == -1 and c["sides"] == "long"):
                want = 0
            if want != pos:
                if pos:
                    g = pos * sh * (p - entry) - sh * cost
                    pnl += g
                    if keep_trades:
                        trades.append([day, et, _hm(j), pos, round(entry, 2), round(p, 2), sh, round(g, 2)])
                if want:
                    pnl -= sh * cost
                    entry, et = p, _hm(j)
                    n += 1
                pos = want
        if pos:                                         # out at the close
            g = pos * sh * (last - entry) - sh * cost
            pnl += g
            if keep_trades:
                trades.append([day, et, "16:00" if lastk >= 389 else _hm2(lastk), pos, round(entry, 2), round(last, 2), sh, round(g, 2)])
        ret = pnl / aum if aum else 0.0
        aum += pnl
        daily.append([day, ret, round(aum, 2), n, vx])
    return daily, trades


def _hm(j):
    m = 9 * 60 + 30 + 15 * (j + 1)
    return f"{m // 60:02d}:{m % 60:02d}"


def _hm2(k):
    m = 9 * 60 + 30 + k + 1
    return f"{m // 60:02d}:{m % 60:02d}"


def stats(daily, start=None, end=None):
    rs = [d for d in daily if (not start or d[0] >= start) and (not end or d[0] < end)]
    if len(rs) < 5:
        return {"days": len(rs)}
    r = [d[1] for d in rs]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for x in r:
        eq *= 1 + x
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    m = sum(r) / len(r)
    sd = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
    yrs = len(r) / 252
    traded = [x for d, x in zip(rs, r) if d[3]]
    return {"days": len(r), "from": rs[0][0], "to": rs[-1][0], "total": round(eq - 1, 4), "cagr": round(eq ** (1 / yrs) - 1, 4) if yrs > 0.3 and eq > 0 else None,
            "vol": round(sd * math.sqrt(252), 4), "sharpe": round(m / sd * math.sqrt(252), 2) if sd else None, "mdd": round(mdd, 4),
            "hit": round(sum(1 for x in traded if x > 0) / len(traded), 3) if traded else None, "trades": sum(d[3] for d in rs),
            "best": round(max(r), 4), "worst": round(min(r), 4), "t": round(m / (sd / math.sqrt(len(r))), 2) if sd else None}


def buy_hold(days, start=None, end=None):
    rs = [[days[i][0], days[i][2] / days[i - 1][2] - 1, 0, 1, None] for i in range(LOOK, len(days))]
    return stats(rs, start, end)


def _split(days):
    d = date.fromisoformat(days[-1][0])
    return d.replace(year=d.year - TEST_YEARS).isoformat()


def load():
    days = (store.get_json(f"im/{SYM}/prep") or {}).get("days") or []
    if len(days) < LOOK + 30:
        raise BadRequest("No SPY 1-minute data yet: press “Download SPY 1-minute + VIX”.")
    vix = (store.get_json("im/VIX") or {}).get("rows") or []
    return days, ctx_cached(days, vix)


_CTX = {}


def ctx_cached(days, vix):
    k = (len(days), days[-1][0], len(vix))
    if k not in _CTX:
        _CTX.clear()
        _CTX[k] = context(days, vix)
    return _CTX[k]


def run(sub, cfg):
    c = clean(cfg)
    days, ctx = load()
    daily, trades = simulate(days, ctx, c, keep_trades=True)
    sd = _split(days)
    years = sorted({d[0][:4] for d in daily})
    by_year = [{"year": y, **stats(daily, f"{y}-01-01", f"{int(y) + 1}-01-01"), "spy": buy_hold(days, f"{y}-01-01", f"{int(y) + 1}-01-01").get("total")} for y in years]
    vix_tab = []
    if any(d[4] is not None for d in daily):
        for lvl in (10, 15, 20, 25, 30, 40):
            sub_d = [d for d in daily if d[4] is not None and d[4] >= lvl]
            vix_tab.append({"vix": lvl, **stats(sub_d)})
    dow = []
    for k, name in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")):
        dow.append({"day": name, **stats([d for d in daily if date.fromisoformat(d[0]).weekday() == k])})
    rsi_tab = []
    rs = sorted(((ctx["rsi"][i], daily[i - LOOK]) for i in range(LOOK, len(days)) if ctx["rsi"][i] is not None), key=lambda z: z[0])
    if len(rs) > 30:
        third = len(rs) // 3
        for lab, part in (("low (recent drop)", rs[:third]), ("middle", rs[third:2 * third]), ("high (recent rise)", rs[2 * third:])):
            rsi_tab.append({"rsi": lab, "range": f"{part[0][0]:.0f}–{part[-1][0]:.0f}", **stats(sorted([p[1] for p in part]))})
    longs = [t for t in trades if t[3] == 1]
    shorts = [t for t in trades if t[3] == -1]
    side = [{"side": lab, "n": len(ts), "pnl": round(sum(t[7] for t in ts), 2), "win": round(sum(1 for t in ts if t[7] > 0) / len(ts), 3) if ts else None}
            for lab, ts in (("long", longs), ("short", shorts))]
    curve = [[d[0], d[2]] for d in daily]
    step = max(1, len(curve) // 1200)
    spy = [[days[i][0], round(c["acct"] * days[i][2] / days[LOOK - 1][2], 2)] for i in range(LOOK, len(days))]
    res = {"cfg": c, "at": iso(now_ny()), "split": sd, "all": stats(daily), "train": stats(daily, end=sd), "test": stats(daily, sd),
           "spy": {"all": buy_hold(days), "train": buy_hold(days, end=sd), "test": buy_hold(days, sd)}, "years": by_year, "vix": vix_tab, "dow": dow,
           "rsi": rsi_tab, "sides": side, "curve": curve[::step] + ([curve[-1]] if (len(curve) - 1) % step else []),
           "spyCurve": spy[::step] + ([spy[-1]] if (len(spy) - 1) % step else []), "final": daily[-1][2] if daily else None,
           "trades": [dict(zip(("day", "in", "out", "side", "entry", "exit", "shares", "pnl"), t)) for t in trades[-500:]], "nTrades": len(trades),
           "vixData": any(d[4] is not None for d in daily)}
    st = store.get_json(f"im/u/{sub}") or {}
    store.put_json(f"im/u/{sub}", {**st, "cfg": c, "res": res})
    return res


def view(sub):
    import jobs
    st = store.get_json(f"im/u/{sub}") or {}
    o = store.get_json(f"im/u/{sub}/opt")
    if o:
        o = {**o, "top": sorted(o.get("res") or [], key=lambda x: x["train"].get("sharpe") or -9, reverse=True)[:20]}
        o.pop("res", None)
    j = jobs.status(sub, "intramom")
    if j.get("status") == "running" and j.get("at", "") < iso(now_ny() - timedelta(minutes=20)):
        j["stale"] = True
    return {"cfg": st.get("cfg") or clean({}), "res": st.get("res"), "data": status(), "opt": o, "job": j, "grid": len(grid())}


def data_start(sub):
    import alpaca
    import jobs
    alpaca.creds(sub)                                  # Alpaca connected (else the error shows on the page)
    return jobs.start(sub, "intramom", "imdata", {})


def grid():
    keys = list(GRID)
    return [dict(zip(keys, v)) for v in itertools.product(*[GRID[k] for k in keys])]


def opt_start(sub, cfg):
    import jobs
    load()
    store.put_json(f"im/u/{sub}/opt", {"cfg": clean(cfg), "res": []})
    return jobs.start(sub, "intramom", "imopt", {})


def opt_job(sub, prog=None, deadline=None):
    st = store.get_json(f"im/u/{sub}/opt") or {}
    base = clean(st.get("cfg"))
    days, ctx = load()
    sd = _split(days)
    done = st.get("res") or []
    g = grid()
    t_last = 0.0
    for i in range(len(done), len(g)):
        if deadline and time.time() > deadline:
            store.put_json(f"im/u/{sub}/opt", {**st, "res": done})
            return False
        if prog and time.time() - t_last > 3:
            prog(f"Optimizer: {i + 1}/{len(g)}")
            t_last = time.time()
        daily, _ = simulate(days, ctx, clean({**base, **g[i]}))
        done.append({"p": g[i], "train": stats(daily, end=sd), "test": stats(daily, sd)})
    store.put_json(f"im/u/{sub}/opt", {**st, "res": done, "split": sd, "total": len(g), "at": iso(now_ny()), "complete": True})
    return True
