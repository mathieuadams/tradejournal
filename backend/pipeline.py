"""Strategy pipeline: every strategy goes through the same protocol, then gets its own paper account.

  1. Data download  - the history the rule needs (flow: the unusual-options alert history; daily: the daily candles)
  2. Study          - every signal of the rule, traded the way paper will trade it (buy the next open, sell the open
                      `hold` days later, optional k x ATR stop), vs the baseline (every ticker on every day where the
                      same MARKET conditions hold, same trade): by holding time, by market condition (the conditions
                      map: SPY trend x VIX), by each condition of the stock / the alert, train (first 60%) vs test.
  3. Validation     - hold x stop grid, cost stress, months, tickers, the hard gates, then Claude reviews it all
                      (the risk committee) and answers pass / fail / more data.
  4. Promote        - all gates + Claude pass -> paper on an account of its own; it only trades in the approved cells
                      of the conditions map. Kill switch: paper drifting below the backtest (z < -2) or a drawdown
                      over 2x the backtest's -> paused. Enough paper trades in line -> ready for live.

Items: PIPE#<id> (the strategy), PIPER#<id> (protocol results), PIPET#<id> (backtest trades, for the chart),
PIPEPOS#<id>#<day>#<sym> (paper positions), PIPEMKT (today's market cell), BROKER#alpacapipe#<n> (extra accounts)."""
import json
import math
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import db
from util import BadRequest, NotFound, iso, now_ny

SP = "PIPE#"
RES = "PIPER#"
TRD = "PIPET#"
POS = "PIPEPOS#"
MKT = "PIPEMKT"
ACC = "BROKER#alpacapipe#"
TRAIN = 0.6
HZ = (1, 3, 5, 10, 20)
HOLDS = (3, 5, 10, 20)
STOPS = (0, 2.0, 3.0)
COSTS = (5, 10, 20, 40)
COST_BP = 10
STAGES = ("research", "paper", "live", "retired")
AI_REQUIRED = False                                        # the AI review is optional for now: promote = data + validation passed
MARKET_KEYS = ("phase", "vix", "dial", "spy50", "clear")
PHASES = ("bull", "sideways", "bear")
VOLS = ("calm", "volatile")                               # VIX under / over 20

BOT_ACCOUNTS = [("BROKER#alpaca", "Options bot account"), ("BROKER#alpacaday", "Day-trading account (QQQ / SPY)"),
                ("BROKER#alpacastock", "Stocks-in-play account")]

DAILY_FACTORS = [
    ("phase", "Market phase (SPY trend)"), ("vix", "VIX"), ("dial", "Risk dial"), ("spy50", "SPY vs its 50-day"), ("clear", "VIX all-clear day"),
    ("ws", "Stock's weekly EMA score"), ("e21", "Stock vs its 21-day"), ("e50", "Stock vs its 50-day"), ("e200", "Stock vs its 200-day"),
    ("mom", "Stock's last 20 days"), ("drop5", "Stock's last 5 days"),
    ("hi52", "Stock vs its 52-week high"), ("ath", "Stock vs its all-time high (in the data)"), ("base", "High base (last 15 days)"), ("sqz", "Daily squeeze (Bollinger inside Keltner)"),
    ("sqz5", "Daily squeeze in the last 5 days"),
]
VALUES = {
    "phase": ["bull", "sideways", "bear"], "vix": ["under 15", "15-20", "20-30", "30+"], "dial": ["normal", "caution", "alarm"],
    "spy50": ["above", "under"], "clear": ["yes", "no"], "ws": ["+2", "+1", "0", "-1", "-2"], "e21": ["above", "under"],
    "e50": ["above", "under"], "e200": ["above", "under"], "mom": ["down 5%+", "down 0-5%", "up 0-5%", "up 5%+"],
    "drop5": ["down 5%+", "down 2-5%", "flat / up"],
    "ath": ["within 10%", "10-25% below", "25%+ below"], "hi52": ["within 10%", "10-25% below", "25%+ below"], "base": ["breakout of the base", "in the base", "no base"],
    "sqz": ["on", "fired today", "off"], "sqz5": ["yes", "no"],
    "prem": ["100-250K", "250K-1M", "1-5M", "5M+"], "rel": ["3x+ its median", "normal size"], "dte": ["0-7", "8-30", "31-90", "91+"],
    "mny": ["in the money", "at the money", "OTM 2-10%", "OTM 10%+"], "sweep": ["sweep", "no sweep"], "open": ["opening (vol > OI)", "vol <= OI"],
    "tod": ["before 10:30", "10:30-15:00", "last hour"], "burst": ["1", "2-4", "5+"],
    "cluster": ["single (none in 5 days)", "first day of a burst", "cluster 1-2 days", "cluster 3+ days"],
    "repeat": ["same contract 2x+", "different contracts"], "total": ["under 500K", "500K-2M", "2-10M", "10M+"], "mixed": ["other side too", "this side only"],
}

SEED_V = 3                                                 # new candidates are added once each time this goes up
SEEDS = [
    {"seed": "range50", "v": 3, "name": "Range 50% retrace (NQ scalper setup)", "kind": "intraday",
     "ip": {"tickers": ["QQQ", "SPY"], "tf": 1, "legPct": 0.35, "stopBuf": 0, "side": "both", "days": 250},
     "desc": "A leg (a high to the low under it), the retrace to its 50%, then back to 25% of the range: buy there, target the 50%, stop under the low. Mirror for the short."},
    {"seed": "highBase", "v": 2, "name": "High base breakout + squeeze", "kind": "daily", "side": "long", "universe": {"list": "Main"},
     "cond": {"base": "breakout of the base", "hi52": "within 10%", "sqz5": "yes"}, "hold": 5, "stop": 0, "exitRange": True,
     "desc": "A tight 15-day base within 10% of the 52-week high, a daily squeeze in the last 5 days, today's close breaks above the base: "
             "buy the next open, out after 5 days, or the open after a close back below the base's low."},
    {"seed": "flowSpyWeak", "name": "Flow: calls while SPY < 50-day", "kind": "flow", "side": "long", "flowSide": "call", "cond": {"spy50": "under"},
     "hold": 5, "stop": 0, "desc": "Buy the stock the open after unusual call buying (bought at the ask) while SPY is under its 50-day. Out after 5 days."},
    {"seed": "bearDip", "name": "Bear-phase dip buy", "kind": "daily", "side": "long", "universe": {"list": "__etf"}, "cond": {"phase": "bear", "drop5": "down 5%+"},
     "hold": 10, "stop": 0, "desc": "Index / sector ETFs down 5%+ in 5 days while the market phase is bear: buy the next open, out after 10 days."},
    {"seed": "vixClear", "name": "VIX all-clear buy", "kind": "daily", "side": "long", "universe": {"tickers": ["SPY", "QQQ"]}, "cond": {"clear": "yes"},
     "hold": 20, "stop": 0, "desc": "VIX back under VIX3M 3 days in a row after a stress alarm: buy SPY / QQQ the next open, out after 20 days."},
]


# ---------------------------------------------------------------- storage
def _pk(sub):
    return db.upk(sub)


def _load_json(it, k):
    return json.loads(it[k]) if it and it.get(k) else {}


def _strip(it):
    return {k: v for k, v in (it or {}).items() if k not in ("PK", "SK")}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get(sub, pid):
    it = db.get(_pk(sub), SP + pid)
    if not it:
        raise NotFound("No such strategy.")
    return _norm(it)


def _norm(it):
    s = _strip(it)
    for k in ("cond", "universe", "job", "kill", "signals", "plan", "ip"):
        if isinstance(s.get(k), str):
            try:
                s[k] = json.loads(s[k])
            except ValueError:
                s[k] = {}
    for k in ("hold", "stop", "allocPct", "maxPos"):
        if s.get(k) is not None:
            s[k] = float(s[k]) if k in ("stop", "allocPct") else int(s[k])
    s.setdefault("cells", None)
    return s


def _save(sub, s):
    item = {"PK": _pk(sub), "SK": SP + s["id"]}
    for k, v in s.items():
        item[k] = json.dumps(v, default=str) if k in ("cond", "universe", "job", "kill", "signals", "plan", "ip") else v
    for k in ("stop", "allocPct"):
        if item.get(k) is not None:
            item[k] = str(item[k])
    db.put(item)


def results(sub, pid):
    return _load_json(db.get(_pk(sub), RES + pid), "data")


def _save_results(sub, pid, r):
    db.put({"PK": _pk(sub), "SK": RES + pid, "data": json.dumps(r, default=str, separators=(",", ":"))})


def strategies(sub):
    items = [_norm(i) for i in db.q_prefix(_pk(sub), SP)]
    have = {s.get("seed") for s in items}
    for x in items:
        if x.get("seed") == "highBase" and x.get("stage") == "research" and x.get("cond", {}).get("ath") == "within 10%" and "hi52" not in x["cond"]:
            x["cond"] = {**{k: v for k, v in x["cond"].items() if k != "ath"}, "hi52": "within 10%"}
            x["desc"] = x.get("desc", "").replace("all-time high", "52-week high")
            x.setdefault("history", []).append({"at": iso(now_ny()), "what": "rule changed: near the 52-week high (not the all-time high)"})
            _save(sub, x)
    sd = db.get(_pk(sub), "PIPESEEDED")
    v0 = int((sd or {}).get("v") or (1 if sd else 0))
    if v0 < SEED_V:                                    # the candidates found so far, each added once
        for x in SEEDS:
            if x["seed"] not in have and int(x.get("v") or 1) > v0:
                items.append(create(sub, x, seed=True))
        db.put({"PK": _pk(sub), "SK": "PIPESEEDED", "at": iso(now_ny()), "v": SEED_V})
    return sorted(items, key=lambda s: s.get("createdAt") or "")


def create(sub, b, seed=False):
    b = b or {}
    name = str(b.get("name") or "").strip()[:80]
    if not name:
        raise BadRequest("Give the strategy a name.")
    kind = b.get("kind") if b.get("kind") in ("flow", "daily", "intraday") else "daily"
    cond = clean_cond(b.get("cond") or {}, kind)
    if kind == "daily" and not cond:
        raise BadRequest("Add at least one entry condition.")
    s = {"id": uuid.uuid4().hex[:10], "name": name, "desc": str(b.get("desc") or "")[:400], "kind": kind,
         "side": "short" if b.get("side") == "short" else "long", "flowSide": "put" if b.get("flowSide") == "put" else "call",
         "cond": cond, "universe": _universe(b.get("universe")) if kind == "daily" else {"list": "__watch"},
         "hold": int(_clip(b.get("hold"), 1, 60, 5)), "stop": float(_clip(b.get("stop"), 0, 10, 0)),
         "allocPct": float(_clip(b.get("allocPct"), 1, 50, 10)), "maxPos": int(_clip(b.get("maxPos"), 1, 30, 5)),
         "exitRange": bool(b.get("exitRange")) and kind == "daily",
         "ip": _ip(b.get("ip")) if kind == "intraday" else None,
         "stage": "research", "account": None, "createdAt": iso(now_ny()), "history": [{"at": iso(now_ny()), "what": "created"}]}
    if kind == "intraday":
        s["universe"] = {"tickers": s["ip"]["tickers"]}
        s["cond"], s["hold"], s["stop"] = {}, 1, 0.0
        s["side"] = "short" if s["ip"]["side"] == "short" else "long"
    if seed:
        s["seed"] = b["seed"]
    _save(sub, s)
    return s


def _ip(p):
    import range50
    return range50.clean(p)


def _clip(v, lo, hi, dflt):
    v = _num(v)
    return dflt if v is None else max(lo, min(hi, v))


def _universe(u):
    u = u or {}
    if u.get("tickers"):
        tk = u["tickers"].replace(",", " ").split() if isinstance(u["tickers"], str) else u["tickers"]
        tk = [str(t).strip().upper()[:10] for t in tk if str(t).strip()][:80]
        if tk:
            return {"tickers": tk}
    return {"list": str(u.get("list") or "__etf")[:60]}


def clean_cond(c, kind):
    if kind == "intraday":
        return {}
    keys = {k for k, _ in DAILY_FACTORS} if kind == "daily" else {k for k, _ in _flow_factors()}
    out = {}
    for k, v in (c or {}).items():
        if k in keys and str(v) in VALUES.get(k, []):
            out[k] = str(v)
    return out


def _flow_factors():
    import uoa
    return uoa.FACTORS + [("clear", "VIX all-clear day")]


INTRA_FACTORS = [("side", "Side"), ("tod", "Time of the entry"), ("s", "Ticker"), ("wd", "Weekday"), ("wait", "Minutes from the 50% to the entry"),
                 ("size", "Leg size vs the rule's minimum")]


def factor_names(kind):
    if kind == "intraday":
        return dict(INTRA_FACTORS)
    return dict(DAILY_FACTORS if kind == "daily" else _flow_factors())


def describe(s):
    if s["kind"] == "intraday":
        p = s.get("ip") or {}
        return (f"{', '.join(p.get('tickers') or [])} {p.get('tf', 1)}-minute: after a leg of at least {p.get('legPct')}% "
                f"({'down legs → long' if p.get('side') == 'long' else 'up legs → short' if p.get('side') == 'short' else 'both sides'}), "
                f"a retrace to the range's 50%, then back to {round(p.get('entryLvl', 0.25) * 100)}%: enter there, target the 50%, "
                f"stop {'at the range end' if not p.get('stopBuf') else f'{round(p.get('stopBuf') * 100)}% of the range past its end'}, flat at 15:55")
    names = factor_names(s["kind"])
    c = " and ".join(f"{names.get(k, k)} = {v}" for k, v in (s.get("cond") or {}).items())
    who = f"unusual {s.get('flowSide', 'call')} buying" if s["kind"] == "flow" else "signal"
    return (f"{'Short' if s['side'] == 'short' else 'Buy'} the next open after {who}" + (f" when {c}" if c else "") +
            f"; out at the open {s['hold']} trading days later" + (f", stop {s['stop']:g} x ATR" if s.get("stop") else ", no stop") +
            (" or the open after a close below the base's low" if s.get("exitRange") else ""))


# ---------------------------------------------------------------- the trade (exactly how paper trades it)
def trade(A, i, d, hold, stop, cost_bp, lvl=None):
    """Signal known at day i's close. Buy (d=1) / short (d=-1) at the open of i+1, out at the open of i+1+hold,
    or at the stop (k x ATR of day i, checked on the daily lows / highs while held). % net of the cost."""
    r, atr = A["r"], A["atr"]
    n = len(r)
    j0, jx = i + 1, i + 1 + hold
    if jx >= n or not r[j0] or not r[jx] or not atr[i]:
        return None
    px = r[j0][0]
    if not px:
        return None
    st = px - d * stop * atr[i] if stop else None
    for k in range(j0, jx):
        if not r[k]:
            continue
        o, h, l = r[k][:3]
        if st is not None and ((d > 0 and l <= st) or (d < 0 and h >= st)):
            ex = st if k == j0 else (min(o, st) if d > 0 else max(o, st))
            return d * (ex / px - 1) * 100 - cost_bp / 100, k
        if lvl and k + 1 < jx and r[k + 1] and ((d > 0 and r[k][3] < lvl) or (d < 0 and r[k][3] > lvl)):   # closed back in / below the range
            return d * (r[k + 1][0] / px - 1) * 100 - cost_bp / 100, k + 1
    return d * (r[jx][0] / px - 1) * 100 - cost_bp / 100, jx


def _tr(s, A, r, d, hold, stop, cost_bp):
    """The strategy's trade of one signal row (with its range exit when the strategy has one)."""
    return trade(A, r["i"], d, hold, stop, cost_bp, r.get("lvl") if s.get("exitRange") else None)


def _agg(xs):
    xs = [x for x in xs if x is not None]
    n = len(xs)
    if not n:
        return {"n": 0}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, n - 1))
    return {"n": n, "avg": round(m, 3), "win": round(sum(1 for x in xs if x > 0) / n * 100), "t": round(m / sd * math.sqrt(n), 2) if sd > 0 else None,
            "sd": round(sd, 3), "tot": round(sum(xs), 2)}


def _edge(a, b):
    return round(a["avg"] - b["avg"], 3) if a.get("n") and b.get("n") else None


def _dd(xs):
    peak = cur = dd = 0.0
    for x in xs:
        cur += x
        peak = max(peak, cur)
        dd = max(dd, peak - cur)
    return round(dd, 2)


# ---------------------------------------------------------------- the data every step reads
def _mkt_f(ctx0, X, i):
    v = ctx0["vix"][i]
    import uoa
    return {"phase": ctx0["phase"][i], "vix": uoa._bucket(v, (15, 20, 30), ("under 15", "15-20", "20-30", "30+")),
            "dial": "alarm" if ctx0["alarm"][i] else "caution" if ctx0["caution"][i] else "normal",
            "spy50": None if ctx0["spy"][i] is None or ctx0["spy50"][i] is None else ("above" if ctx0["spy"][i] > ctx0["spy50"][i] else "under"),
            "clear": "yes" if X["clear"][i] else "no"}


def _cell(ctx0, i):
    p, v = ctx0["phase"][i], ctx0["vix"][i]
    return f"{p}|{'calm' if v < 20 else 'volatile'}" if p and v is not None else None


def _stock_f(A, ctx, i, ws):
    import uoa
    c = A["c"][i]
    f = {}
    w = ws[i] if ws else None
    f["ws"] = None if w is None else ("+2" if w == 2 else "+1" if w == 1 else "0" if w == 0 else "-1" if w == -1 else "-2")
    for k, arr in (("e21", A["e21"]), ("e50", A["e50"]), ("e200", ctx["e200"])):
        f[k] = None if c is None or arr[i] is None else ("above" if c > arr[i] else "under")
    m = c / A["c"][i - 20] - 1 if i >= 20 and c and A["c"][i - 20] else None
    f["mom"] = uoa._bucket(None if m is None else m * 100, (-5, 0, 5), ("down 5%+", "down 0-5%", "up 0-5%", "up 5%+"))
    m5 = c / A["c"][i - 5] - 1 if i >= 5 and c and A["c"][i - 5] else None
    f["drop5"] = None if m5 is None else ("down 5%+" if m5 <= -0.05 else "down 2-5%" if m5 <= -0.02 else "flat / up")
    if "ath" in ctx:
        hi = ctx["ath"][i]
        f["ath"] = None if not hi or c is None else ("within 10%" if c >= hi * 0.9 else "10-25% below" if c >= hi * 0.75 else "25%+ below")
        h52 = ctx["hi52"][i]
        f["hi52"] = None if not h52 or c is None else ("within 10%" if c >= h52 * 0.9 else "10-25% below" if c >= h52 * 0.75 else "25%+ below")
        bh, bl = ctx["bh"][i], ctx["bl"][i]
        if bh is None or c is None:
            f["base"] = None
        elif (bh - bl) / bh <= BASE_RANGE:
            f["base"] = "breakout of the base" if c > bh else "in the base"
        else:
            f["base"] = "no base"
        sq = ctx["sq"]
        f["sqz"] = None if sq[i] is None else "on" if sq[i] else "fired today" if i and sq[i - 1] else "off"
        f["sqz5"] = None if sq[i] is None else ("yes" if any(sq[j] for j in range(max(0, i - 4), i + 1)) else "no")
    return f


BASE_N, BASE_RANGE = 15, 0.15                          # a base: the 15 days before, high-to-low range at most 15% of the high


def _stock_ctx(A, clf):
    """Per ticker, once: EMA 200, the running high, the base before each day, the daily squeeze."""
    import macro
    r, c = A["r"], A["c"]
    n = len(c)
    ctx = {"e200": macro.ema(clf, 200)}
    ath, hi = [], None
    for i in range(n):
        if r[i]:
            hi = r[i][1] if hi is None else max(hi, r[i][1])
        ath.append(hi)
    ctx["ath"] = ath
    from collections import deque
    h52, dq = [None] * n, deque()                       # the highest high of the last 252 trading days (sliding max)
    for i in range(n):
        if r[i]:
            while dq and dq[-1][1] <= r[i][1]:
                dq.pop()
            dq.append((i, r[i][1]))
        while dq and dq[0][0] <= i - 252:
            dq.popleft()
        h52[i] = dq[0][1] if dq and i >= 200 else None
    ctx["hi52"] = h52
    bh, bl = [None] * n, [None] * n
    for i in range(BASE_N, n):
        w = [x for x in r[i - BASE_N:i] if x]
        if len(w) >= BASE_N - 2:
            bh[i], bl[i] = max(x[1] for x in w), min(x[2] for x in w)
    ctx["bh"], ctx["bl"] = bh, bl
    e20 = macro.ema(clf, 20)
    sq = [None] * n
    for i in range(20, n):                              # Bollinger (20, 2 sd) inside Keltner (EMA 20 +- 1.5 x ATR)
        xs = [x for x in clf[i - 19:i + 1] if x is not None]
        if len(xs) < 18 or e20[i] is None or not A["atr"][i]:
            continue
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))
        sq[i] = (m + 2 * sd) < (e20[i] + 1.5 * A["atr"][i]) and (m - 2 * sd) > (e20[i] - 1.5 * A["atr"][i])
    ctx["sq"] = sq
    return ctx


def _universe_tickers(sub, s):
    import regime
    import wallwatch
    u = s.get("universe") or {}
    if u.get("tickers"):
        return list(u["tickers"])
    etfs = [m for m, _ in regime.MARKET + regime.SECTORS]
    if u.get("list") == "__etf":
        return etfs
    cfg = wallwatch.config(sub)
    lst = next((L for L in cfg["lists"] if L["name"] == u.get("list")), None) or cfg["lists"][0]
    return list(lst["tickers"])


def build(sub, s, prog=None):
    """{rows: [{s, d, i, f, cell}], X, days, ctx0, tick} - the signals of the strategy with their conditions."""
    import macro
    import plab
    import regime
    import uoa
    if s["kind"] == "flow":
        if prog is not None:
            prog("loading the alert history and the daily candles")
        B = uoa.build(sub)
        X, days, ctx0 = B["X"], B["days"], B["ctx0"]
        rows = []
        for r in B["rows"]:
            if r["side"] != s.get("flowSide", "call"):
                continue
            f = {**r["f"], "clear": "yes" if X["clear"][r["i"]] else "no"}
            if all(f.get(k) == v for k, v in s["cond"].items()):
                rows.append({"s": r["s"], "d": r["d"], "i": r["i"], "f": f, "cell": _cell(ctx0, r["i"])})
        B["first"] = B["rows"][0]["d"] if B["rows"] else (days[0] if days else None)
        B.update(rows=rows, names=dict(_flow_factors()))
        return B
    tick = _universe_tickers(sub, s)
    years = 15 if (s.get("universe") or {}).get("list") == "__etf" or (s.get("universe") or {}).get("tickers") else 8
    syms = list(dict.fromkeys(tick + ["SPY", "^VIX", "^VIX3M", "HYG", "IEF"]))
    if prog is not None:
        prog(f"loading {len(syms)} daily histories")
    data = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for sy, f in [(sy, ex.submit(plab._bars, sy, years)) for sy in syms]:
            try:
                d = f.result(timeout=300)
                if d:
                    data[sy] = d
            except Exception:
                pass
    if "SPY" not in data or "^VIX" not in data:
        raise ValueError("The SPY / VIX history couldn't be loaded. Try again in a minute.")
    days = sorted(data["SPY"])
    tick = [t for t in tick if t in data]
    X = plab.prepare(data, days, tick, [], years)
    lab = regime.label_days({d: v[3] for d, v in data["SPY"].items()}, {d: v[3] for d, v in data["^VIX"].items()},
                            {d: v[3] for d, v in data.get("^VIX3M", {}).items()})
    ctx0 = {"phase": [{"Up": "bull", "Range": "sideways", "Down": "bear"}.get((lab.get(d) or {}).get("trend")) for d in days],
            "vix": macro.align({d: v[3] for d, v in data["^VIX"].items()}, days), "alarm": X["alarm"], "caution": X["caution"],
            "spy": X["A"]["SPY"]["c"], "spy50": X["A"]["SPY"]["e50"]}
    if prog is not None:
        prog(f"scanning {len(tick)} tickers for the signals")
    rows, ctxs = [], {}
    for t in tick:
        A = X["A"][t]
        clf = macro.align({d: v[3] for d, v in data[t].items()}, days)
        ctx = _stock_ctx(A, clf)
        ctxs[t] = ctx
        nxt = 0
        for i in range(200, len(days)):
            if A["c"][i] is None or i < nxt:
                continue
            f = {**_mkt_f(ctx0, X, i), **_stock_f(A, ctx, i, X["w"].get(t))}
            if all(f.get(k) == v for k, v in s["cond"].items()):
                rows.append({"s": t, "d": days[i], "i": i, "f": f, "cell": _cell(ctx0, i), "lvl": ctx["bl"][i]})
                nxt = i + s["hold"] + 1                 # one position per ticker at a time, like paper
    rows.sort(key=lambda r: r["d"])
    return {"rows": rows, "X": X, "days": days, "ctx0": ctx0, "tick": tick, "data": data, "names": dict(DAILY_FACTORS),
            "first": days[200] if len(days) > 200 else days[0], "ctxs": ctxs}


def _baseline(B, s, hold, stop, cost_bp, cut, cache, cells=None):
    """Every ticker, every 4th day, where the MARKET conditions of the rule hold (and, given cells, only in those
    market cells): same trade."""
    mk = {k: v for k, v in s["cond"].items() if k in MARKET_KEYS}
    key = (hold, stop, cost_bp, tuple(sorted(cells or [])))
    if key in cache:
        return cache[key]
    X, days, ctx0 = B["X"], B["days"], B["ctx0"]
    d = -1 if s["side"] == "short" else 1
    tr, te, by_cell = [], [], {}
    for t in B["tick"]:
        A = X["A"].get(t)
        if not A:
            continue
        for i in range(0, len(days), 4):
            if days[i] < B["first"] or A["c"][i] is None:
                continue
            if mk and not all(_mkt_f(ctx0, X, i).get(k) == v for k, v in mk.items()):
                continue
            if cells and _cell(ctx0, i) not in cells:
                continue
            x = trade(A, i, d, hold, stop, cost_bp)
            if x is None:
                continue
            (tr if days[i] < cut else te).append(x[0])
            by_cell.setdefault((_cell(ctx0, i), days[i] < cut), []).append(x[0])
    cache[key] = (_agg(tr), _agg(te), {k: _agg(v) for k, v in by_cell.items()})
    return cache[key]


# ---------------------------------------------------------------- 1. data
def step_data(sub, s, prog):
    import uoa
    if s["kind"] == "intraday":
        return intra_data(sub, s, prog)
    out = {"at": iso(now_ny()), "kind": s["kind"]}
    if s["kind"] == "flow":
        h = uoa.hist_public(sub)
        import wallwatch
        tick = wallwatch.config(sub)["tickers"]
        by_y, n = {}, 0
        for t in tick:
            al = uoa.stored(sub, t)
            for a in al.get(s.get("flowSide", "call")) or []:
                y = a["t"][:4]
                b = by_y.setdefault(y, {"y": y, "n": 0, "leaps": 0})
                b["n"] += 1
                n += 1
                try:
                    from datetime import datetime
                    if (datetime.strptime(a["x"], "%Y-%m-%d") - datetime.strptime(a["t"][:10], "%Y-%m-%d")).days > 90:
                        b["leaps"] += 1
                except (TypeError, ValueError, KeyError):
                    pass
        out.update(source="Unusual Whales flow alerts (bought at the ask)", alerts=n, tickers=len(tick), loaded=h.get("done"),
                   oldest=h.get("oldest"), minPrem=h.get("minPrem"), running=bool(h.get("running")),
                   byYear=[{**b, "leapsPct": round(b["leaps"] / b["n"] * 100) if b["n"] else 0} for b in sorted(by_y.values(), key=lambda b: b["y"])],
                   topup="every trading day at 16:30 ET")
        notes = []
        old = [b for b in out["byYear"] if b["leapsPct"] >= 50]
        if old:
            notes.append(f"{', '.join(b['y'] for b in old)}: mostly LEAPS (90+ days) - the older history only keeps contracts not expired yet. It gets cleaner as the daily top-up adds up.")
        if not n:
            notes.append("No alert history yet: get it below (Unusual Whales).")
        out["notes"] = notes
        out["ok"] = n > 0
        if h.get("running"):
            out["ok"] = n > 0
        return out
    B = build(sub, s, prog)
    cov = []
    for t in B["tick"]:
        ds = sorted(B["data"][t])
        cov.append({"s": t, "from": ds[0], "to": ds[-1], "days": len(ds)})
    want = _universe_tickers(sub, s)
    out.update(source="Daily candles (Yahoo) + SPY, VIX, VIX3M, HYG / IEF", tickers=len(B["tick"]), coverage=cov,
               missing=[t for t in want if t not in B["tick"]], days=len(B["days"]), first=B["days"][0], last=B["days"][-1],
               notes=([f"No data for {', '.join(t for t in want if t not in B['tick'])}."] if any(t not in B["tick"] for t in want) else []), ok=bool(B["tick"]))
    return out


# ---------------------------------------------------------------- 2. study
def step_study(sub, s, prog):
    if s["kind"] == "intraday":
        return intra_study(sub, s, prog)
    B = build(sub, s, prog)
    rows, X = B["rows"], B["X"]
    if not rows:
        return {"at": iso(now_ny()), "events": 0, "error": "No signal in the history: loosen the conditions."}
    cut = rows[int(len(rows) * TRAIN)]["d"] if len(rows) > 1 else rows[0]["d"]
    d = -1 if s["side"] == "short" else 1
    spyA = X["A"]["SPY"]
    prog(f"{len(rows)} signals: by holding time, market condition and each condition")
    cache = {}
    summ = {}
    for h in HZ:
        P = {"train": [], "test": [], "all": []}
        V = {"train": [], "test": [], "all": []}
        for r in rows:
            x = _tr(s, X["A"][r["s"]], r, d, h, 0, COST_BP)
            if x is None:
                continue
            sx = trade(spyA, r["i"], d, h, 0, 0)
            p = "train" if r["d"] < cut else "test"
            for k in (p, "all"):
                P[k].append(x[0])
                if sx:
                    V[k].append(x[0] - sx[0])
        btr, bte, _ = _baseline(B, s, h, 0, COST_BP, cut, cache)
        a = {k: _agg(v) for k, v in P.items()}
        summ[str(h)] = {"train": a["train"], "test": a["test"], "all": a["all"], "vsSpy": {k: _agg(v) for k, v in V.items()},
                        "baseTrain": btr, "baseTest": bte, "edgeTrain": _edge(a["train"], btr), "edgeTest": _edge(a["test"], bte)}
    hold, stop = s["hold"], s["stop"]
    _, _, bcell = _baseline(B, s, hold, stop, COST_BP, cut, cache)
    cells = []
    for ph in PHASES:
        for vo in VOLS:
            ck = f"{ph}|{vo}"
            tr, te = [], []
            for r in rows:
                if r["cell"] != ck:
                    continue
                x = _tr(s, X["A"][r["s"]], r, d, hold, stop, COST_BP)
                if x is not None:
                    (tr if r["d"] < cut else te).append(x[0])
            a_tr, a_te = _agg(tr), _agg(te)
            b_tr, b_te = bcell.get((ck, True), {"n": 0}), bcell.get((ck, False), {"n": 0})
            e_tr, e_te = _edge(a_tr, b_tr), _edge(a_te, b_te)
            n = a_tr.get("n", 0) + a_te.get("n", 0)
            if n < 20 or a_tr.get("n", 0) < 8 or a_te.get("n", 0) < 5:
                status = "thin"
            elif (e_tr or 0) > 0 and (e_te or 0) > 0 and a_tr["avg"] > 0 and a_te["avg"] > 0:
                status = "edge"
            else:
                status = "no edge"
            cells.append({"cell": ck, "phase": ph, "vol": vo, "train": a_tr, "test": a_te, "baseTrain": b_tr, "baseTest": b_te,
                          "edgeTrain": e_tr, "edgeTest": e_te, "n": n, "status": status})
    facs = {}
    for fk in B["names"]:
        vals = {}
        for r in rows:
            v = r["f"].get(fk)
            if v is None:
                continue
            x = _tr(s, X["A"][r["s"]], r, d, hold, stop, COST_BP)
            if x is None:
                continue
            vals.setdefault(v, {"train": [], "test": []})["train" if r["d"] < cut else "test"].append(x[0])
        if len(vals) > 1:
            facs[fk] = {v: {"train": _agg(xs["train"]), "test": _agg(xs["test"])} for v, xs in sorted(vals.items())}
    last = {}
    for r in rows:
        last[r["s"]] = r["d"]
    return {"at": iso(now_ny()), "events": len(rows), "trainEvents": sum(1 for r in rows if r["d"] < cut),
            "testEvents": sum(1 for r in rows if r["d"] >= cut), "split": {"from": rows[0]["d"], "cut": cut, "to": rows[-1]["d"]},
            "hz": list(HZ), "sum": summ, "map": cells, "factors": facs, "factorNames": B["names"], "hold": hold, "stop": stop,
            "lastSignal": last, "tickers": B["tick"], "costBp": COST_BP}


# ---------------------------------------------------------------- 3. validation
GATES = [("trainEdge", "Train edge vs baseline > 0", True), ("testEdge", "Test edge vs baseline > 0", True),
         ("trades", "At least 30 trades", True), ("cost20", "Still positive at 20 bp cost", True),
         ("conc", "No single ticker over 35% of the trades", True), ("testT", "Test t-stat at least 1.5", False),
         ("months", "Best 3 months under 60% of the profit", False), ("dd", "Test drawdown under 3x the average year", False)]


def step_validate(sub, s, prog):
    if s["kind"] == "intraday":
        return intra_validate(sub, s, prog)
    B = build(sub, s, prog)
    rows, X = B["rows"], B["X"]
    if len(rows) < 2:
        return {"at": iso(now_ny()), "error": "Not enough signals to validate.", "gates": [], "passed": False}
    cut = rows[int(len(rows) * TRAIN)]["d"]                 # the same split as the study
    cells = sorted(s.get("cells") or [])
    if cells:                                          # validated as it will trade: only in the ticked market cells
        rows = [r for r in rows if r.get("cell") in cells]
        if len(rows) < 2:
            return {"at": iso(now_ny()), "error": "Not enough signals in the ticked market conditions.", "gates": [], "passed": False, "cells": cells}
    d = -1 if s["side"] == "short" else 1
    cache = {}
    prog(f"{len(rows)} signals: hold x stop grid vs the baseline")
    grid = []
    for hold in sorted(set(HOLDS) | {s["hold"]}):
        for stop in sorted(set(STOPS) | {s["stop"]}):
            tr, te = [], []
            for r in rows:
                x = _tr(s, X["A"][r["s"]], r, d, hold, stop, COST_BP)
                if x is not None:
                    (tr if r["d"] < cut else te).append(x[0])
            btr, bte, _ = _baseline(B, s, hold, stop, COST_BP, cut, cache, cells)
            a_tr, a_te = _agg(tr), _agg(te)
            grid.append({"hold": hold, "stop": stop, "train": a_tr, "test": a_te, "baseTrain": btr, "baseTest": bte,
                         "edgeTrain": _edge(a_tr, btr), "edgeTest": _edge(a_te, bte), "chosen": hold == s["hold"] and stop == s["stop"]})
    prog("costs, months, tickers, drawdown")
    trades, te_x = [], []
    for r in rows:
        x = _tr(s, X["A"][r["s"]], r, d, s["hold"], s["stop"], COST_BP)
        if x is None:
            continue
        trades.append({"s": r["s"], "d": r["d"], "in": B["days"][r["i"] + 1], "out": B["days"][x[1]], "pct": round(x[0], 2), "cell": r["cell"],
                       "test": r["d"] >= cut})
        if r["d"] >= cut:
            te_x.append(x[0])
    costs = []
    for c_ in COSTS:
        costs.append({"bp": c_, "all": _agg([t["pct"] + COST_BP / 100 - c_ / 100 for t in trades])})
    months = {}
    for t in trades:
        months.setdefault(t["d"][:7], []).append(t["pct"])
    mlist = [{"m": m, **_agg(v)} for m, v in sorted(months.items())]
    by_t = {}
    for t in trades:
        by_t.setdefault(t["s"], []).append(t["pct"])
    tick = sorted(({"s": k, **_agg(v)} for k, v in by_t.items()), key=lambda z: -z["n"])
    ch = next(g for g in grid if g["chosen"])
    tot = sum(t["pct"] for t in trades)
    pos_m = sorted((m["tot"] for m in mlist if m.get("tot", 0) > 0), reverse=True)
    top3 = round(sum(pos_m[:3]) / sum(pos_m) * 100) if pos_m and sum(pos_m) > 0 else None
    top1 = round(tick[0]["n"] / len(trades) * 100) if trades and tick else None
    yrs = max(0.5, (len(B["days"]) - B["days"].index(cut) if cut in B["days"] else 250) / 252)
    dd_te = _dd(te_x)
    avg_year = (sum(te_x) / yrs) if te_x else 0
    c20 = next((c for c in costs if c["bp"] == 20), {}).get("all", {})
    gv = {"trainEdge": (ch["edgeTrain"], "> 0"), "testEdge": (ch["edgeTest"], "> 0"), "trades": (len(trades), "≥ 30"),
          "cost20": (c20.get("avg"), "> 0"), "conc": (top1, "≤ 35%" if len(B["tick"]) >= 8 else "n/a (few tickers)"),
          "testT": (ch["test"].get("t"), "≥ 1.5"), "months": (top3, "< 60%"), "dd": (dd_te, f"< {round(3 * avg_year, 1)}" if avg_year > 0 else "")}
    ok = {"trainEdge": (ch["edgeTrain"] or 0) > 0, "testEdge": (ch["edgeTest"] or 0) > 0, "trades": len(trades) >= 30,
          "cost20": (c20.get("avg") or 0) > 0, "conc": len(B["tick"]) < 8 or (top1 or 100) <= 35,
          "testT": (ch["test"].get("t") or 0) >= 1.5, "months": top3 is not None and top3 < 60, "dd": avg_year > 0 and dd_te < 3 * avg_year}
    gates = [{"key": k, "name": n, "hard": hard, "ok": bool(ok[k]), "value": gv[k][0], "need": gv[k][1]} for k, n, hard in GATES]
    eq, cur = [], 0.0
    for t in trades:
        cur += t["pct"]
        eq.append([t["d"], round(cur, 2)])
    return {"at": iso(now_ny()), "hold": s["hold"], "stop": s["stop"], "grid": grid, "costs": costs, "months": mlist, "tickers": tick[:15],
            "top1Share": top1, "top3Months": top3, "trades": len(trades), "chosen": ch, "testDD": dd_te, "allDD": _dd([t["pct"] for t in trades]),
            "gates": gates, "passed": all(g["ok"] for g in gates if g["hard"]), "equity": eq[-400:], "split": {"cut": cut},
            "cells": cells, "holds": sorted(set(HOLDS) | {s["hold"]}), "stops": sorted(set(STOPS) | {s["stop"]}), "costBp": COST_BP,
            "_trades": trades}


AI_SYSTEM = """You are the risk committee of a small quant trading firm. A researcher submits a strategy that went through
the firm's protocol (data, study vs a baseline, train / test split, hold x stop grid, cost stress, months, tickers, gates).
Decide if it goes to a paper account. Be strict and concrete: an edge that is only in a few months, a few tickers, one market
condition, or that vanishes with costs or on the test period is not an edge. The baseline is every ticker on every day with
the same market conditions and the same trade, so the edge column is what the signal adds. Say which conditions-map cells
(phase|calm or phase|volatile) it should be allowed to trade in (only cells with an edge in train AND test, or none).
Plain words, no jargon, short sentences. Numbers are % per trade net of costs."""
AI_SCHEMA = {"type": "object", "properties": {
    "verdict": {"type": "string", "enum": ["pass", "fail", "more data"]},
    "summary": {"type": "string", "description": "3-5 sentences: the verdict and why"},
    "checks": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "ok": {"type": "boolean"}, "note": {"type": "string"}},
                                          "required": ["name", "ok", "note"]}},
    "concerns": {"type": "array", "items": {"type": "string"}},
    "approvedCells": {"type": "array", "items": {"type": "string"}},
    "paperPlan": {"type": "object", "properties": {"minTrades": {"type": "integer"}, "expectAvgPct": {"type": "number"},
                                                   "maxDrawdownPct": {"type": "number"}, "watch": {"type": "string"}},
                  "required": ["minTrades", "expectAvgPct", "maxDrawdownPct", "watch"]}},
    "required": ["verdict", "summary", "checks", "concerns", "approvedCells", "paperPlan"]}


def step_ai(sub, s, res):
    import os
    import claude
    v, st, dt = res.get("validation") or {}, res.get("study") or {}, res.get("data") or {}
    if not v.get("grid"):
        raise BadRequest("Run the validation first.")
    payload = {"strategy": {"name": s["name"], "rule": describe(s), "kind": s["kind"], "side": s["side"], "universe": st.get("tickers")},
               "data": {k: dt.get(k) for k in ("source", "alerts", "tickers", "oldest", "first", "last", "notes", "byYear")},
               "study": {"events": st.get("events"), "split": st.get("split"), "byHold": st.get("sum"), "conditionsMap": st.get("map")},
               "validation": {k: v.get(k) for k in ("hold", "stop", "chosen", "grid", "costs", "months", "tickers", "top1Share", "top3Months",
                                                    "trades", "testDD", "allDD", "gates")}}
    out = claude.json_call(os.environ.get("COACH_MODEL", "claude-sonnet-5"), AI_SYSTEM, payload, 3000, AI_SCHEMA)
    out["approvedCells"] = [c for c in (out.get("approvedCells") or []) if c in {f"{p}|{q}" for p in PHASES for q in VOLS}]
    out["at"] = iso(now_ny())
    return out


IMPROVE_SYSTEM = """You are the head of research of a small quant trading firm. A strategy went through the firm's protocol and
the risk committee did not pass it (or passed it with doubts). From its study (results by holding time, by market
condition, and by each condition of the stock / the alert, train vs test) and its validation (hold x stop grid, costs,
months, tickers, gates) and the committee's concerns, design up to 3 NEW versions that fix the problems: add or change
conditions that were positive in BOTH train and test with enough trades, pick the hold / stop that held in both, drop what
only worked in train. Every version is tested again through the whole protocol, so prefer few, simple, distinct changes
over curve fitting (at most 2 conditions added each). Use only the condition keys and values listed in `allowed`.
Explain each version in one plain sentence."""
IMPROVE_SCHEMA = {"type": "object", "properties": {
    "diagnosis": {"type": "string", "description": "2-3 sentences: what is wrong with the current version"},
    "variants": {"type": "array", "maxItems": 3, "items": {"type": "object", "properties": {
        "name": {"type": "string"}, "cond": {"type": "object", "description": "the FULL entry conditions {key: value}"},
        "hold": {"type": "integer"}, "stop": {"type": "number"}, "why": {"type": "string"}}, "required": ["name", "cond", "hold", "stop", "why"]}}},
    "required": ["diagnosis", "variants"]}


def step_improve(sub, s, res):
    import os
    import claude
    st, v, ai = res.get("study") or {}, res.get("validation") or {}, res.get("ai") or {}
    if s["kind"] == "intraday":
        raise BadRequest("Claude's versions aren't available for the intraday strategies yet: change the leg size / stop yourself (Validation grid).")
    if not st.get("factors") or not v.get("grid"):
        raise BadRequest("Run the study and the validation first.")
    allowed = {k: VALUES.get(k, []) for k, _ in (DAILY_FACTORS if s["kind"] == "daily" else _flow_factors())}
    payload = {"strategy": {"name": s["name"], "rule": describe(s), "cond": s["cond"], "hold": s["hold"], "stop": s["stop"], "kind": s["kind"]},
               "study": {"events": st.get("events"), "split": st.get("split"), "byHold": st.get("sum"), "conditionsMap": st.get("map"),
                         "byCondition": st.get("factors"), "names": st.get("factorNames")},
               "validation": {k: v.get(k) for k in ("grid", "costs", "months", "tickers", "gates", "top3Months", "trades")},
               "committee": {k: ai.get(k) for k in ("verdict", "summary", "checks", "concerns")}, "allowed": allowed}
    out = claude.json_call(os.environ.get("COACH_MODEL", "claude-sonnet-5"), IMPROVE_SYSTEM, payload, 4000, IMPROVE_SCHEMA)
    have = {_cfg_key(x) for x in strategies(sub)}
    made = []
    for x in (out.get("variants") or [])[:3]:
        cond = clean_cond(x.get("cond") or {}, s["kind"])
        if not cond and s["kind"] == "daily":
            continue
        b = {"name": str(x.get("name") or "")[:60] or s["name"] + " v2", "kind": s["kind"], "side": s["side"], "flowSide": s.get("flowSide"),
             "universe": s.get("universe"), "cond": cond, "hold": x.get("hold") or s["hold"], "stop": x.get("stop") if x.get("stop") is not None else s["stop"],
             "desc": str(x.get("why") or "")[:400], "allocPct": s["allocPct"], "maxPos": s["maxPos"], "exitRange": s.get("exitRange")}
        tmp = {**b, "exitRange": bool(b.get("exitRange")) and s["kind"] == "daily", "hold": int(_clip(b["hold"], 1, 60, 5)), "stop": float(_clip(b["stop"], 0, 10, 0)), "universe": _universe(b["universe"]) if s["kind"] == "daily" else {"list": "__watch"}}
        if _cfg_key(tmp) in have:
            continue
        n = create(sub, b)
        n["parent"] = s["id"]
        n["history"].append({"at": iso(now_ny()), "what": f"designed by Claude from {s['name']}"})
        _save(sub, n)
        have.add(_cfg_key(n))
        try:
            run_start(sub, n["id"], "all")
        except Exception as e:
            print("variant start failed", e)
        made.append({"id": n["id"], "name": n["name"], "rule": describe(n), "why": b["desc"]})
    return {"at": iso(now_ny()), "diagnosis": out.get("diagnosis") or "", "variants": made}


# ---------------------------------------------------------------- the protocol job
def protocol(sub, s, res):
    """Where the strategy is in the protocol: each step done / failed / to do, and whether it can be promoted."""
    dt, st, v, ai = res.get("data") or {}, res.get("study") or {}, res.get("validation") or {}, res.get("ai") or {}
    stale = lambda r, k: bool(r.get("at")) and r.get("cfg") not in (None, k)
    e_te = (((st.get("sum") or {}).get(st.get("ruleRow") or str(s["hold"])) or {}).get("edgeTest") or 0)
    steps = [
        {"key": "data", "name": "Data download", "status": "done" if dt.get("ok") else "failed" if dt.get("at") else "todo"},
        {"key": "study", "name": "Study", "status": "todo" if not st.get("at") else "failed" if st.get("error") or not st.get("events") else
         ("done" if any(c["status"] == "edge" for c in st.get("map") or []) or e_te > 0 else "weak")},
        {"key": "validate", "name": "Validation", "status": "todo" if not v.get("at") else "done" if v.get("passed") else "failed"},
        {"key": "ai", "name": "AI review", "status": "todo" if not ai.get("at") else "done" if ai.get("verdict") == "pass" else "failed"},
    ]
    for x, r, k in zip(steps, (dt, st, v, ai), (_data_key(s), _cfg_key(s), _val_key(s), _val_key(s))):
        if stale(r, k):
            x["status"] = "stale"
    if not AI_REQUIRED:
        steps[3]["optional"] = True
    ready = steps[0]["status"] == "done" and steps[2]["status"] == "done" and (steps[3]["status"] == "done" or not AI_REQUIRED)
    return {"steps": steps, "ready": ready}


def _data_key(s):
    return json.dumps([s["kind"], s.get("universe")] + ([(s.get("ip") or {}).get("tf"), (s.get("ip") or {}).get("days")] if s["kind"] == "intraday" else []), sort_keys=True)


def _val_key(s):
    return json.dumps([_cfg_key(s), sorted(s.get("cells") or [])])


def _cfg_key(s):
    k = [s["kind"], s["side"], s.get("flowSide"), s["cond"], s.get("universe"), s["hold"], s["stop"]]
    return json.dumps(k + (["range"] if s.get("exitRange") else []) + ([s.get("ip")] if s["kind"] == "intraday" else []), sort_keys=True)


def run_start(sub, pid, step):
    import research
    s = get(sub, pid)
    if step not in ("data", "study", "validate", "ai", "all", "improve"):
        raise BadRequest("Unknown step.")
    j = s.get("job") or {}
    if j.get("running") and j["running"] > iso(now_ny() - timedelta(minutes=14)):
        return s
    if step == "data" and s["kind"] == "flow":
        import uoa
        uoa.hist_start(sub, {})
    s["job"] = {"running": iso(now_ny()), "step": step, "msg": "starting", "error": None}
    _save(sub, s)
    research._invoke(sub, "pipe", {"id": pid, "step": step})
    return s


def job(sub, params):
    pid, step = params.get("id"), params.get("step") or "all"
    s = get(sub, pid)

    def prog(msg):
        s["job"] = {**(s.get("job") or {}), "running": iso(now_ny()), "msg": msg}
        _save(sub, s)
    res = results(sub, pid)
    steps = (["data", "study", "validate", "ai"] if AI_REQUIRED else ["data", "study", "validate"]) if step == "all" else [step]
    try:
        for k in steps:
            s["job"] = {"running": iso(now_ny()), "step": k, "msg": {"data": "downloading the data", "study": "running the study",
                                                                     "validate": "validating", "ai": "Claude is reviewing",
                                                                     "improve": "Claude is designing better versions"}[k], "error": None}
            _save(sub, s)
            if k == "data":
                res["data"] = step_data(sub, s, prog)
            elif k == "study":
                res["study"] = step_study(sub, s, prog)
            elif k == "validate":
                v = step_validate(sub, s, prog)
                tr = v.pop("_trades", [])
                by = {}
                for t in tr:
                    by.setdefault(t["s"], []).append([t["in"], t["out"], t["pct"]])
                db.put({"PK": _pk(sub), "SK": TRD + pid, "data": json.dumps({k2: v2[-120:] for k2, v2 in by.items()}, separators=(",", ":"))})
                res["validation"] = v
                if (res.get("ai") or {}).get("at"):
                    res["ai"]["stale"] = True
            elif k == "ai":
                if step == "all" and not (res.get("validation") or {}).get("passed"):
                    s.setdefault("history", []).append({"at": iso(now_ny()), "what": "AI review skipped: the validation failed (ask Claude from the AI review tab anyway)"})
                    break
                res["ai"] = step_ai(sub, s, res)
                if s.get("cells") is None:
                    s["cells"] = res["ai"]["approvedCells"]
            elif k == "improve":
                res["improve"] = step_improve(sub, s, res)
            res[k if k != "validate" else "validation"]["cfg"] = _data_key(s) if k == "data" else _cfg_key(s) if k == "study" else _val_key(s)
            _save_results(sub, pid, res)
            s.setdefault("history", []).append({"at": iso(now_ny()), "what": f"{k}: " + _summary_line(k, res)})
            s["history"] = s["history"][-40:]
        s["job"] = {"running": None, "step": None, "msg": None, "error": None, "at": iso(now_ny())}
    except Exception as e:
        print("pipeline job failed", pid, step, repr(e))
        s["job"] = {"running": None, "step": None, "msg": None, "error": str(e)[:200], "at": iso(now_ny())}
    _save(sub, s)
    return {"id": pid, "error": s["job"].get("error")}


def _summary_line(k, res):
    if k == "data":
        d = res.get("data") or {}
        return f"{d.get('alerts') or d.get('days') or 0} {'alerts' if d.get('kind') == 'flow' else 'days'}, {d.get('tickers') or 0} tickers"
    if k == "study":
        st = res.get("study") or {}
        return f"{st.get('events', 0)} signals, {sum(1 for c in st.get('map') or [] if c['status'] == 'edge')} cells with an edge"
    if k == "validate":
        v = res.get("validation") or {}
        return ("passed" if v.get("passed") else "failed") + f" ({sum(1 for g in v.get('gates') or [] if g['ok'])}/{len(v.get('gates') or [])} checks)"
    if k == "improve":
        return f"{len((res.get('improve') or {}).get('variants') or [])} new versions from Claude, each running the protocol"
    return (res.get("ai") or {}).get("verdict") or ""


# ---------------------------------------------------------------- accounts
def accounts(sub, strats=None):
    strats = strats if strats is not None else strategies(sub)
    out = []
    used = {}                                          # an account can carry several strategies: each one tracks its own trades
    for x in strats:
        if x.get("account") and x["stage"] in ("paper", "live"):
            used.setdefault(x["account"], []).append(x)
    on = _bots_on(sub)
    for sk, name in BOT_ACCOUNTS:
        it = db.get(_pk(sub), sk)
        u = used.get(sk) or []
        out.append({"sk": sk, "name": name, "connected": bool(it), "env": (it or {}).get("env"), "keyHint": (it or {}).get("keyHint"),
                    "bot": True, "botOn": on.get(sk) or None, "usedBy": ", ".join(x["name"] for x in u) or None, "usedByIds": [x["id"] for x in u]})
    for it in db.q_prefix(_pk(sub), ACC):
        u = used.get(it["SK"]) or []
        out.append({"sk": it["SK"], "name": it.get("name") or it["SK"][len(ACC):], "connected": True, "env": it.get("env"), "keyHint": it.get("keyHint"),
                    "bot": False, "usedBy": ", ".join(x["name"] for x in u) or None, "usedByIds": [x["id"] for x in u]})
    return out


def _bots_on(sub):
    """{account: 'the bot trading on it'} for the older bots that are switched on."""
    on = {}
    try:
        import autotrader
        c = autotrader.settings(sub)
        if c.get("flowAuto") and c.get("autoSubmit"):
            on["BROKER#alpaca"] = "Options flow bot"
    except Exception as e:
        print("bots on: flow", e)
    for mod, sk, name in (("smclive", "BROKER#alpacaday", "Smart money QQQ / SPY"), ("daytrade", "BROKER#alpacaday", "Day trading QQQ / SPY"),
                          ("stockday", "BROKER#alpacastock", "Stocks in play")):
        try:
            c = __import__(mod).config(sub)
            if c.get("enabled") and c.get("trade"):
                on[sk] = (on[sk] + " + " if sk in on else "") + name
        except Exception as e:
            print("bots on:", mod, e)
    return on


def connect(sub, b):
    import alpaca
    name = str((b or {}).get("name") or "").strip()[:40]
    key, secret = str(b.get("key") or "").strip(), str(b.get("secret") or "").strip()
    env = "live" if b.get("env") == "live" else "paper"
    if not name or not key or not secret:
        raise BadRequest("Enter a name, the API key and the secret.")
    for a in accounts(sub):
        if a.get("keyHint") and a["keyHint"] == key[-4:]:
            raise BadRequest(f"These look like the keys of {a['name']}: each strategy needs an account of its own.")
    n = len(db.q_prefix(_pk(sub), ACC)) + 1
    sk = f"{ACC}{n:02d}"
    alpaca.save_creds(sub, key, secret, env, "iex", sk=sk)
    it = db.get(_pk(sub), sk) or {}
    db.put({**it, "name": name})
    return accounts(sub)


def _creds(sub, sk):
    import alpaca
    return alpaca.creds(sub, sk)


def _alp(c, method, path, body=None):
    base = "https://api.alpaca.markets" if c.get("env") == "live" else "https://paper-api.alpaca.markets"
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise BadRequest(f"Alpaca refused the request ({e.code}): {e.read().decode(errors='replace')[:200]}")


# ---------------------------------------------------------------- stage changes
def action(sub, pid, b):
    s = get(sub, pid)
    a = (b or {}).get("action")
    res = results(sub, pid)
    now = iso(now_ny())
    if a == "update":
        before = _cfg_key(s)
        if s["stage"] in ("paper", "live"):
            raise BadRequest("Pause it and send it back to research before changing the rule.")
        for k in ("name", "desc"):
            if b.get(k) is not None:
                s[k] = str(b[k]).strip()[:400 if k == "desc" else 80] or s[k]
        if b.get("cond") is not None:
            s["cond"] = clean_cond(b["cond"], s["kind"])
        for k, lo, hi in (("hold", 1, 60), ("stop", 0, 10), ("allocPct", 1, 50), ("maxPos", 1, 30)):
            if b.get(k) is not None:
                s[k] = type(s[k])(_clip(b[k], lo, hi, s[k]))
        if b.get("side") in ("long", "short"):
            s["side"] = b["side"]
        if b.get("ip") is not None and s["kind"] == "intraday":
            s["ip"] = _ip({**(s.get("ip") or {}), **b["ip"]})
            s["universe"] = {"tickers": s["ip"]["tickers"]}
        if "exitRange" in b and s["kind"] == "daily":
            s["exitRange"] = bool(b["exitRange"])
        if b.get("flowSide") in ("call", "put"):
            s["flowSide"] = b["flowSide"]
        if b.get("universe") is not None and s["kind"] == "daily":
            s["universe"] = _universe(b["universe"])
        if _cfg_key(s) != before:
            s["history"].append({"at": now, "what": "rule changed: the protocol starts again"})
    elif a == "cells":
        ok = {f"{p}|{q}" for p in PHASES for q in VOLS}
        s["cells"] = [c for c in (b.get("cells") or []) if c in ok]
        s["history"].append({"at": now, "what": "approved conditions: " + (", ".join(s["cells"]) or "none") + " (validate again)"})
    elif a in ("paper", "live"):
        pr = protocol(sub, s, res)
        if s["kind"] == "intraday":
            raise BadRequest("Paper trading of the intraday strategies isn't wired yet: the research protocol works, the minute-by-minute paper engine comes next.")
        if a == "paper" and not pr["ready"]:
            raise BadRequest("The protocol must pass first (data, validation" + (", AI review)." if AI_REQUIRED else ")."))
        if a == "live" and s["stage"] != "paper":
            raise BadRequest("Only a paper strategy goes live.")
        sk = b.get("account")
        acc = next((x for x in accounts(sub) if x["sk"] == sk), None)
        if not acc or not acc["connected"]:
            raise BadRequest("Pick a connected account.")
        if acc.get("botOn") and not b.get("share"):
            raise BadRequest(f"The {acc['botOn']} is trading on {acc['name']}: confirm sharing the account (its P&L mixes with the bot's).")
        if a == "live" and acc.get("env") != "live":
            raise BadRequest("Pick an account connected with LIVE keys.")
        if a == "paper" and acc.get("env") == "live":
            raise BadRequest("That account has live keys: paper needs a paper account.")
        if not s.get("cells"):
            s["cells"] = (res.get("ai") or {}).get("approvedCells") or [c["cell"] for c in (res.get("study") or {}).get("map") or [] if c["status"] == "edge"]
        if not s.get("cells"):
            raise BadRequest("No market condition is approved: tick at least one cell of the conditions map (2 · Study).")
        s.update(stage=a, account=sk, since=now, kill={"status": "armed"})
        s["plan"] = (res.get("ai") or {}).get("paperPlan") or {}
        s["history"].append({"at": now, "what": f"promoted to {a} on {acc['name']}"})
    elif a == "pause":
        s["kill"] = {"status": "paused", "why": "paused by you", "at": now}
        s["history"].append({"at": now, "what": "paused"})
    elif a == "resume":
        s["kill"] = {"status": "armed"}
        s["history"].append({"at": now, "what": "resumed"})
    elif a == "research":
        s.update(stage="research", account=None, kill={})
        s["history"].append({"at": now, "what": "back to research (open positions still exit by their rule)"})
    elif a == "retire":
        s.update(stage="retired", account=None, kill={})
        s["history"].append({"at": now, "what": "retired"})
    elif a == "unretire":
        s.update(stage="research")
        s["history"].append({"at": now, "what": "back to research"})
    else:
        raise BadRequest("Unknown action.")
    s["history"] = s["history"][-40:]
    _save(sub, s)
    return detail(sub, pid)


def delete(sub, pid):
    s = get(sub, pid)
    if s["stage"] in ("paper", "live"):
        raise BadRequest("Send it back to research first.")
    for sk in (SP + pid, RES + pid, TRD + pid):
        db.delete(_pk(sub), sk)
    return {"ok": True}


# ---------------------------------------------------------------- views
def positions(sub, pid):
    return sorted((_strip(p) for p in db.q_prefix(_pk(sub), POS + pid + "#")), key=lambda p: p.get("signalDay") or "")


def paper_stats(s, pos, res):
    closed = [p for p in pos if p.get("status") == "closed" and p.get("pct") is not None]
    xs = [float(p["pct"]) for p in closed]
    a = _agg(xs)
    v = (res.get("validation") or {}).get("chosen") or {}
    bt = v.get("test") or {}
    z = None
    if a.get("n", 0) >= 5 and bt.get("sd"):
        z = round((a["avg"] - bt["avg"]) / (bt["sd"] / math.sqrt(a["n"])), 2)
    return {"closed": a, "open": sum(1 for p in pos if p.get("status") in ("pending", "open", "exiting")),
            "pl": round(sum(float(p.get("pl") or 0) for p in closed), 2), "dd": _dd(xs), "z": z, "bt": bt,
            "btDD": (res.get("validation") or {}).get("testDD")}


def market(sub, fresh=False):
    it = db.get(_pk(sub), MKT) or {}
    today = now_ny().strftime("%Y-%m-%d")
    if it.get("at", "")[:10] == today and not fresh:
        return _strip(it)
    try:
        import macro
        import plab
        import regime
        spy, vix, v3 = plab._bars("SPY", 2), plab._bars("^VIX", 2), plab._bars("^VIX3M", 2)
        days = sorted(spy)
        lab = regime.label_days({d: v[3] for d, v in spy.items()}, {d: v[3] for d, v in vix.items()}, {d: v[3] for d, v in v3.items()})
        d = days[-1]
        ph = {"Up": "bull", "Range": "sideways", "Down": "bear"}.get((lab.get(d) or {}).get("trend"))
        vv = macro.align({x: v[3] for x, v in vix.items()}, days)[-1]
        out = {"d": d, "phase": ph, "vix": round(vv, 2) if vv else None, "cell": f"{ph}|{'calm' if vv < 20 else 'volatile'}" if ph and vv else None,
               "label": (lab.get(d) or {}).get("label"), "at": iso(now_ny())}
        db.put({"PK": _pk(sub), "SK": MKT, **out})
        return out
    except Exception as e:
        print("pipeline market failed", e)
        return _strip(it)


def bots(sub):
    """The bots that ran before the pipeline existed: shown on the Running page as they are."""
    out = []
    try:
        import autotrader
        cfg = autotrader.settings(sub)
        items = [i for i in db.q_prefix(_pk(sub), "BOT#") if i.get("status") == "closed" and i.get("realizedPl") is not None]
        out.append({"key": "flowbot", "name": "Options flow bot", "desc": "Calls bought from unusual options flow, exits managed by the bot",
                    "on": bool(cfg.get("flowAuto")), "orders": bool(cfg.get("autoSubmit")), "account": "Options bot account", "stage": "paper",
                    "closed": len(items), "pl": round(sum(float(i["realizedPl"]) for i in items), 2), "open": "#botflow"})
    except Exception as e:
        print("flow bot view failed", e)
    try:
        import smclive
        c = smclive.config(sub)
        recs = [r for r in db.q_prefix(_pk(sub), smclive.POS) if r.get("R") is not None]
        rs = [float(r["R"]) for r in recs]
        out.append({"key": "smc", "name": "Smart money QQQ / SPY", "desc": "1-minute smart money setups on QQQ / SPY", "on": bool(c.get("enabled")),
                    "orders": bool(c.get("trade")), "account": "Day-trading account (QQQ / SPY)", "stage": "paper", "closed": len(rs),
                    "avgR": round(sum(rs) / len(rs), 2) if rs else None, "totR": round(sum(rs), 1) if rs else None, "mode": "smc"})
    except Exception as e:
        print("smc view failed", e)
    for mod, key, name, desc, mode, acc in (("daytrade", "dt", "Day trading QQQ / SPY", "1-minute day-trading strategy", "dt", "Day-trading account (QQQ / SPY)"),
                                            ("stockday", "sd", "Stocks in play", "Opening-range breakout on the top relative-volume stocks", "sd", "Stocks-in-play account")):
        try:
            m = __import__(mod)
            c = m.config(sub)
            if c.get("enabled") or c.get("trade"):
                out.append({"key": key, "name": name, "desc": desc, "on": bool(c.get("enabled")), "orders": bool(c.get("trade")), "account": acc,
                            "stage": "paper", "mode": mode})
        except Exception as e:
            print(mod, "view failed", e)
    return out


def _summary(sub, s, res=None):
    res = res if res is not None else results(sub, s["id"])
    pr = protocol(sub, s, res)
    st, v, ai = res.get("study") or {}, res.get("validation") or {}, res.get("ai") or {}
    ch = v.get("chosen") or {}
    out = {**{k: s.get(k) for k in ("id", "name", "desc", "kind", "side", "flowSide", "cond", "universe", "hold", "stop", "allocPct", "maxPos",
                                    "stage", "account", "since", "kill", "cells", "job", "signals", "plan")},
           "rule": describe(s), "protocol": pr, "events": st.get("events"),
           "bt": {"testAvg": (ch.get("test") or {}).get("avg"), "testEdge": ch.get("edgeTest"), "trainEdge": ch.get("edgeTrain"), "n": v.get("trades")},
           "verdict": ai.get("verdict")}
    if s["stage"] in ("paper", "live"):
        out["paper"] = paper_stats(s, positions(sub, s["id"]), res)
    return out


def overview(sub):
    ss = strategies(sub)
    return {"strategies": [_summary(sub, s) for s in ss], "bots": bots(sub), "accounts": accounts(sub, ss), "market": market(sub),
            "catalog": {"daily": DAILY_FACTORS, "flow": _flow_factors(), "values": VALUES}}


def detail(sub, pid):
    s = get(sub, pid)
    res = results(sub, pid)
    return {"strategy": _summary(sub, s, res), "history": s.get("history") or [], "results": res, "positions": positions(sub, pid),
            "accounts": accounts(sub), "market": market(sub), "factorNames": factor_names(s["kind"]),
            "catalog": {"factors": DAILY_FACTORS if s["kind"] == "daily" else _flow_factors(), "values": VALUES}}


def chart(sub, pid, sym):
    import plab
    s = get(sub, pid)
    sym = (sym or "").strip().upper()[:10]
    if not sym:
        raise BadRequest("Pick a ticker.")
    bars = plab._bars(sym, 2)
    days = sorted(bars)[-260:]
    tr = _load_json(db.get(_pk(sub), TRD + pid), "data").get(sym, [])
    pos = [p for p in positions(sub, pid) if p.get("symbol") == sym]
    return {"symbol": sym, "bars": [[d, *bars[d][:4]] for d in days], "trades": [t for t in tr if t[1] >= days[0]] if days else [],
            "paper": [{k: p.get(k) for k in ("status", "fillDay", "fillPrice", "exitDay", "exitPrice", "pct", "side")} for p in pos], "side": s["side"]}


# ---------------------------------------------------------------- paper / live: every trading day after the close
def daily_kick():
    now = now_ny()
    if now.weekday() >= 5 or now.strftime("%H:%M") < "18:00":
        return 0
    import research
    n = 0
    for sub in db.owners():
        try:
            if not any(s.get("stage") in ("paper", "live") for s in (_norm(i) for i in db.q_prefix(_pk(sub), SP))):
                continue
            it = db.get(_pk(sub), "PIPETICK") or {}
            if (it.get("day") or "") >= now.strftime("%Y-%m-%d"):
                continue
            db.put({"PK": _pk(sub), "SK": "PIPETICK", **{k: v for k, v in it.items() if k not in ("PK", "SK")}, "day": now.strftime("%Y-%m-%d")})
            research._invoke(sub, "pipetick", {})
            n += 1
        except Exception as e:
            print("pipeline kick failed", e)
    return n


def tick(sub, today=None):
    """Sync the paper positions, exits due, kill switch, then today's signals -> orders at the next open."""
    log = []
    mk = market(sub, fresh=True)
    for s in strategies(sub):
        if s["stage"] not in ("paper", "live") or not s.get("account"):
            continue
        try:
            log.append(_tick_one(sub, s, mk, today))
        except Exception as e:
            print("pipeline tick failed", s["id"], repr(e))
            log.append({"id": s["id"], "error": str(e)[:200]})
    it = db.get(_pk(sub), "PIPETICK") or {}
    db.put({"PK": _pk(sub), "SK": "PIPETICK", "day": it.get("day"), "at": iso(now_ny()), "log": json.dumps(log, default=str)[:30000]})
    return {"strategies": len(log)}


def _tick_one(sub, s, mk, today=None):
    c = _creds(sub, s["account"])
    if not c:
        raise ValueError("The account isn't connected any more.")
    B = build(sub, s)
    days, X = B["days"], B["X"]
    today = today or now_ny().strftime("%Y-%m-%d")
    ti = len(days) - 1
    out = {"id": s["id"], "name": s["name"], "day": days[-1], "actions": []}
    d = -1 if s["side"] == "short" else 1
    pos = positions(sub, s["id"])
    for p in pos:                                      # 1. sync
        st = p.get("status")
        key = {"PK": _pk(sub), "SK": f"{POS}{s['id']}#{p['signalDay']}#{p['symbol']}"}
        if st == "pending":
            o = _alp(c, "GET", f"/v2/orders/{p['orderId']}") or {}
            if o.get("status") == "filled":
                p.update(status="open", fillPrice=float(o["filled_avg_price"]), fillDay=(o.get("filled_at") or today)[:10], qty=int(float(o["filled_qty"])))
                A = X["A"].get(p["symbol"])
                si = days.index(p["signalDay"]) if p["signalDay"] in days else None
                if s.get("stop") and A and si is not None and A["atr"][si]:
                    sp = round(p["fillPrice"] - d * s["stop"] * A["atr"][si], 2)
                    so = _alp(c, "POST", "/v2/orders", {"symbol": p["symbol"], "qty": str(p["qty"]), "side": "sell" if d > 0 else "buy", "type": "stop",
                                                         "stop_price": str(sp), "time_in_force": "gtc", "client_order_id": f"pp-{s['id']}-{p['symbol']}-{p['signalDay']}-s"})
                    p.update(stopPx=sp, stopOrderId=(so or {}).get("id"))
                out["actions"].append(f"{p['symbol']}: filled {p['fillPrice']}")
                st = "open"
            elif o.get("status") in ("canceled", "expired", "rejected"):
                p.update(status="canceled", why=o.get("status"))
                out["actions"].append(f"{p['symbol']}: entry {o.get('status')}")
        if st == "open":
            if p.get("stopOrderId"):
                so = _alp(c, "GET", f"/v2/orders/{p['stopOrderId']}") or {}
                if so.get("status") == "filled":
                    _close(p, float(so["filled_avg_price"]), (so.get("filled_at") or today)[:10], d, "stop")
                    out["actions"].append(f"{p['symbol']}: stopped {p['exitPrice']}")
                    db.put({**key, **p})
                    continue
            j0 = days.index(p["fillDay"]) if p.get("fillDay") in days else None
            A_ = X["A"].get(p["symbol"])
            back = bool(s.get("exitRange") and p.get("lvl") and A_ and A_["c"][ti] and j0 is not None and ti >= j0 and
                        ((d > 0 and A_["c"][ti] < float(p["lvl"])) or (d < 0 and A_["c"][ti] > float(p["lvl"]))))
            if back or (j0 is not None and ti - j0 >= s["hold"] - 1):
                if p.get("stopOrderId"):
                    _alp(c, "DELETE", f"/v2/orders/{p['stopOrderId']}")
                eo = _alp(c, "POST", "/v2/orders", {"symbol": p["symbol"], "qty": str(p["qty"]), "side": "sell" if d > 0 else "buy", "type": "market",
                                                     "time_in_force": "opg", "client_order_id": f"pp-{s['id']}-{p['symbol']}-{p['signalDay']}-x"})
                p.update(status="exiting", exitOrderId=(eo or {}).get("id"), exitSent=today, exitWhyNext="range" if back else "time")
                out["actions"].append(f"{p['symbol']}: exit at the next open " + ("(closed below the base)" if back else f"(held {s['hold']} days)"))
        elif st == "exiting":
            o = _alp(c, "GET", f"/v2/orders/{p['exitOrderId']}") or {}
            if o.get("status") == "filled":
                _close(p, float(o["filled_avg_price"]), (o.get("filled_at") or today)[:10], d, p.get("exitWhyNext") or "time")
                out["actions"].append(f"{p['symbol']}: closed {p['exitPrice']} ({p['pct']:+.2f}%)")
        db.put({**key, **p})
    res = results(sub, s["id"])                        # 2. kill switch
    ps = paper_stats(s, positions(sub, s["id"]), res)
    kill = s.get("kill") or {"status": "armed"}
    n = ps["closed"].get("n", 0)
    why = None
    if kill.get("status") == "armed":
        if ps["z"] is not None and n >= 10 and ps["z"] < -2:
            why = f"paper is drifting below the backtest (z {ps['z']} over {n} trades)"
        elif ps["btDD"] and ps["dd"] > 2 * float(ps["btDD"]) and n >= 5:
            why = f"paper drawdown {ps['dd']}% is over 2x the backtest's ({ps['btDD']}%)"
        if why:
            kill = {"status": "paused", "why": why, "at": iso(now_ny())}
            s["history"].append({"at": iso(now_ny()), "what": "kill switch: " + why})
            out["actions"].append("kill switch: " + why)
    plan = s.get("plan") or {}
    s["ready"] = s["stage"] == "paper" and n >= int(plan.get("minTrades") or 30) and (ps["z"] is None or ps["z"] >= -1)
    s["kill"] = kill
    sig = []                                           # 3. today's signals
    if days[-1] == today:
        cell = _cell(B["ctx0"], ti)
        allowed = cell in (s.get("cells") or [])
        held = {p["symbol"] for p in positions(sub, s["id"]) if p.get("status") in ("pending", "open", "exiting")}
        others = {}                                    # the other strategies on the same account: one position per ticker per account
        for o2 in strategies(sub):                     # (Alpaca nets a ticker's shares, so two strategies can't both hold it)
            if o2["id"] != s["id"] and o2.get("account") == s["account"] and o2["stage"] in ("paper", "live"):
                for p in positions(sub, o2["id"]):
                    if p.get("status") in ("pending", "open", "exiting"):
                        others[p["symbol"]] = o2["name"]
        todays = [r for r in B["rows"] if r["d"] == today] if s["kind"] == "flow" else _today_rows(B, s, ti)
        acct = _alp(c, "GET", "/v2/account") or {}
        eq = float(acct.get("equity") or 0)
        room = s["maxPos"] - len(held)
        for r in todays:
            act = None
            if kill.get("status") != "armed":
                act = "skipped: kill switch paused"
            elif not allowed:
                act = f"skipped: market {_cell_name(cell)} isn't approved"
            elif r["s"] in held:
                act = "skipped: already held"
            elif r["s"] in others:
                act = f"skipped: {others[r['s']]} holds it on this account"
            elif room <= 0:
                act = f"skipped: {s['maxPos']} positions max"
            else:
                px = X["A"][r["s"]]["c"][ti]
                qty = int(eq * s["allocPct"] / 100 // px) if px and eq else 0
                if qty < 1:
                    act = "skipped: size under 1 share"
                else:
                    o = _alp(c, "POST", "/v2/orders", {"symbol": r["s"], "qty": str(qty), "side": "buy" if d > 0 else "sell", "type": "market",
                                                        "time_in_force": "opg", "client_order_id": f"pp-{s['id']}-{r['s']}-{today}"})
                    db.put({"PK": _pk(sub), "SK": f"{POS}{s['id']}#{today}#{r['s']}", "symbol": r["s"], "signalDay": today, "side": s["side"],
                            "qty": qty, "status": "pending", "orderId": (o or {}).get("id"), "cell": cell, "at": iso(now_ny()), "lvl": r.get("lvl")})
                    room -= 1
                    held.add(r["s"])
                    act = f"order: {'buy' if d > 0 else 'short'} {qty} at the next open"
            sig.append({"s": r["s"], "action": act})
        out["cell"], out["allowed"] = cell, allowed
    s["signals"] = {"d": days[-1], "list": sig, "at": iso(now_ny())}
    out["signals"] = sig
    _save(sub, s)
    return out


def _cell_name(c):
    p, v = (c or "|").split("|")
    return f"{ {'bull': 'Up', 'sideways': 'Range', 'bear': 'Down'}.get(p, p)} · {'Calm' if v == 'calm' else 'Volatile'}" if p else "unknown"


def _today_rows(B, s, ti):
    X, ctx0 = B["X"], B["ctx0"]
    rows = []
    for t in B["tick"]:
        A = X["A"][t]
        if A["c"][ti] is None:
            continue
        f = {**_mkt_f(ctx0, X, ti), **_stock_f(A, B["ctxs"][t], ti, X["w"].get(t))}
        if all(f.get(k) == v for k, v in s["cond"].items()):
            rows.append({"s": t, "d": B["days"][ti], "i": ti, "f": f, "lvl": B["ctxs"][t]["bl"][ti]})
    return rows


def _close(p, px, day, d, why):
    p.update(status="closed", exitPrice=px, exitDay=day, exitWhy=why, pct=round(d * (px / p["fillPrice"] - 1) * 100, 3),
             pl=round(d * (px - p["fillPrice"]) * p["qty"], 2))


# ---------------------------------------------------------------- intraday (range50.py): the same protocol on minute candles
COST_I = 2                                             # bp, round trip: QQQ / SPY spreads + a tick of slippage
COSTS_I = (1, 2, 5, 10)


def _cells_by_day():
    import macro
    import plab
    import regime
    spy, vix, v3 = plab._bars("SPY", 4), plab._bars("^VIX", 4), plab._bars("^VIX3M", 4)
    days = sorted(spy)
    lab = regime.label_days({d: v[3] for d, v in spy.items()}, {d: v[3] for d, v in vix.items()}, {d: v[3] for d, v in v3.items()})
    va = macro.align({d: v[3] for d, v in vix.items()}, days)
    out = {}
    for i, d in enumerate(days):                       # the cell known at the previous close (the session itself is unknown)
        if i == 0:
            continue
        pd_ = days[i - 1]
        ph = {"Up": "bull", "Range": "sideways", "Down": "bear"}.get((lab.get(pd_) or {}).get("trend"))
        if ph and va[i - 1] is not None:
            out[d] = f"{ph}|{'calm' if va[i - 1] < 20 else 'volatile'}"
    return out


def _intra_all(sub, s, prog, p=None):
    import range50
    p = p or s["ip"]
    try:
        cells = _cells_by_day()
    except Exception as e:
        print("intraday cells failed", e)
        cells = {}
    tr, base = range50.all_trades(sub, p, cells, prog)
    for x, b in zip(tr, base):
        x["base"] = b[2]
        x["tod"] = range50.tod(x["in"])
        x["wd"] = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][__import__("datetime").date.fromisoformat(x["d"]).weekday()]
        m_in = int(x["in"][11:13]) * 60 + int(x["in"][14:16])
        m_t = int(x["touch"][11:13]) * 60 + int(x["touch"][14:16])
        w = m_in - m_t
        x["wait"] = "under 5 min" if w < 5 else "5-15 min" if w < 15 else "15-60 min" if w < 60 else "60 min+"
        rel = x["R"] / (abs(x["e"]) * p["legPct"] / 100) if x["e"] else 1
        x["size"] = "1-1.5x" if rel < 1.5 else "1.5-2x" if rel < 2 else "2x+"
    return tr


def intra_data(sub, s, prog):
    import range50
    p = s["ip"]
    cov = []
    for sym in p["tickers"]:
        prog(f"{sym}: {p['days']} days of minute candles (Alpaca)")
        by = range50.bars(sub, sym, p["days"], p["tf"])
        ds = sorted(by)
        if ds:
            cov.append({"s": sym, "from": ds[0], "to": ds[-1], "days": len(ds), "bars": sum(len(v) for v in by.values())})
    return {"at": iso(now_ny()), "kind": "intraday", "source": f"Alpaca {p['tf']}-minute candles, regular hours (QQQ stands in for NQ: no futures on Alpaca)",
            "tickers": len(cov), "coverage": cov, "days": max([c["days"] for c in cov] or [0]), "first": min([c["from"] for c in cov] or [""]),
            "last": max([c["to"] for c in cov] or [""]), "missing": [t for t in p["tickers"] if t not in [c["s"] for c in cov]], "notes": [], "ok": bool(cov)}


def _net(x, bp):
    return x["pct"] - bp / 100


def _split(tr):
    ds = sorted({x["d"] for x in tr})
    return ds[int(len(ds) * TRAIN)] if ds else None


def _io(tr, cut, bp=COST_I):
    a = [x for x in tr if x["d"] < cut]
    b = [x for x in tr if x["d"] >= cut]
    agg = lambda xs: _agg([_net(x, bp) for x in xs])
    bag = lambda xs: _agg([v - bp / 100 for x in xs for v in x["base"]])
    A, Bt, bA, bB = agg(a), agg(b), bag(a), bag(b)
    return {"train": A, "test": Bt, "all": agg(tr), "baseTrain": bA, "baseTest": bB, "edgeTrain": _edge(A, bA), "edgeTest": _edge(Bt, bB),
            "rTrain": _agg([x["r"] for x in a if x["r"] is not None]), "rTest": _agg([x["r"] for x in b if x["r"] is not None])}


def intra_study(sub, s, prog):
    import range50
    p = s["ip"]
    tr = _intra_all(sub, s, prog)
    if not tr:
        return {"at": iso(now_ny()), "events": 0, "error": "No setup in the history: lower the leg size."}
    cut = _split(tr)
    prog(f"{len(tr)} setups: by leg size, market condition and each condition")
    summ = {}
    for lg in sorted(set(range50.LEGS) | {p["legPct"]}):
        t2 = tr if lg == p["legPct"] else _intra_all(sub, s, None, {**p, "legPct": lg})
        summ[f"{lg:g}"] = _io(t2, _split(t2) or cut)
    cells = []
    for ph in PHASES:
        for vo in VOLS:
            ck = f"{ph}|{vo}"
            x = _io([t for t in tr if t["cell"] == ck], cut)
            n = x["train"].get("n", 0) + x["test"].get("n", 0)
            status = "thin" if n < 20 or x["train"].get("n", 0) < 8 or x["test"].get("n", 0) < 5 else \
                "edge" if (x["edgeTrain"] or 0) > 0 and (x["edgeTest"] or 0) > 0 and x["train"]["avg"] > 0 and x["test"]["avg"] > 0 else "no edge"
            cells.append({"cell": ck, "phase": ph, "vol": vo, **{k: x[k] for k in ("train", "test", "baseTrain", "baseTest", "edgeTrain", "edgeTest")},
                          "n": n, "status": status})
    facs = {}
    for fk, _ in INTRA_FACTORS:
        vals = {}
        for t in tr:
            vals.setdefault(t.get(fk), {"train": [], "test": []})["train" if t["d"] < cut else "test"].append(_net(t, COST_I))
        if len(vals) > 1:
            facs[fk] = {str(v): {"train": _agg(xs["train"]), "test": _agg(xs["test"])} for v, xs in sorted(vals.items(), key=lambda kv: str(kv[0]))}
    last = {}
    for t in tr:
        last[t["s"]] = t["d"]
    why = {}
    for t in tr:
        why[t["why"]] = why.get(t["why"], 0) + 1
    return {"at": iso(now_ny()), "events": len(tr), "trainEvents": sum(1 for t in tr if t["d"] < cut), "testEvents": sum(1 for t in tr if t["d"] >= cut),
            "split": {"from": min(t["d"] for t in tr), "cut": cut, "to": max(t["d"] for t in tr)}, "hz": list(summ), "sum": summ, "map": cells,
            "factors": facs, "factorNames": dict(INTRA_FACTORS), "lastSignal": last, "tickers": p["tickers"], "costBp": COST_I,
            "rowLabel": "Leg size", "rowNames": {k: f"{k}% leg" for k in summ}, "ruleRow": f"{p['legPct']:g}", "exits": why,
            "mapNote": "the rule as it will trade; the cell is the market at the previous close", "intraday": True}


def intra_validate(sub, s, prog):
    import range50
    p = s["ip"]
    cells = sorted(s.get("cells") or [])
    tr0 = _intra_all(sub, s, prog)
    if len(tr0) < 2:
        return {"at": iso(now_ny()), "error": "Not enough setups to validate.", "gates": [], "passed": False}
    cut = _split(tr0)
    flt = (lambda xs: [x for x in xs if x["cell"] in cells]) if cells else (lambda xs: xs)
    tr = flt(tr0)
    if len(tr) < 2:
        return {"at": iso(now_ny()), "error": "Not enough setups in the ticked market conditions.", "gates": [], "passed": False, "cells": cells}
    prog(f"{len(tr)} setups: leg size x stop grid vs the random-timing baseline")
    grid = []
    legs, stops = sorted(set(range50.LEGS) | {p["legPct"]}), sorted(set(range50.STOPS) | {p["stopBuf"]})
    for lg in legs:
        for sb in stops:
            t2 = tr if (lg == p["legPct"] and sb == p["stopBuf"]) else flt(_intra_all(sub, s, None, {**p, "legPct": lg, "stopBuf": sb}))
            x = _io(t2, cut)
            grid.append({"hold": lg, "stop": sb, **{k: x[k] for k in ("train", "test", "baseTrain", "baseTest", "edgeTrain", "edgeTest")},
                         "rTest": x["rTest"], "chosen": lg == p["legPct"] and sb == p["stopBuf"]})
    ch = next(g for g in grid if g["chosen"])
    costs = [{"bp": c_, "all": _agg([_net(x, c_) for x in tr])} for c_ in COSTS_I]
    months = {}
    for x in tr:
        months.setdefault(x["d"][:7], []).append(_net(x, COST_I))
    mlist = [{"m": m, **_agg(v)} for m, v in sorted(months.items())]
    by_t = {}
    for x in tr:
        by_t.setdefault(x["s"], []).append(_net(x, COST_I))
    tick = sorted(({"s": k, **_agg(v)} for k, v in by_t.items()), key=lambda z: -z["n"])
    pos_m = sorted((m["tot"] for m in mlist if m.get("tot", 0) > 0), reverse=True)
    top3 = round(sum(pos_m[:3]) / sum(pos_m) * 100) if pos_m and sum(pos_m) > 0 else None
    te_x = [_net(x, COST_I) for x in tr if x["d"] >= cut]
    ndays = len({x["d"] for x in tr0 if x["d"] >= cut}) or 1
    avg_year = sum(te_x) / max(0.2, ndays / 252)
    dd_te = _dd(te_x)
    c5 = next(c for c in costs if c["bp"] == 5)["all"]
    gates = [
        {"key": "trainEdge", "name": "Train edge vs random timing > 0", "hard": True, "ok": (ch["edgeTrain"] or 0) > 0, "value": ch["edgeTrain"], "need": "> 0"},
        {"key": "testEdge", "name": "Test edge vs random timing > 0", "hard": True, "ok": (ch["edgeTest"] or 0) > 0, "value": ch["edgeTest"], "need": "> 0"},
        {"key": "trades", "name": "At least 30 trades", "hard": True, "ok": len(tr) >= 30, "value": len(tr), "need": "≥ 30"},
        {"key": "cost20", "name": "Still positive at 5 bp cost", "hard": True, "ok": (c5.get("avg") or 0) > 0, "value": c5.get("avg"), "need": "> 0"},
        {"key": "testT", "name": "Test t-stat at least 1.5", "hard": False, "ok": (ch["test"].get("t") or 0) >= 1.5, "value": ch["test"].get("t"), "need": "≥ 1.5"},
        {"key": "months", "name": "Best 3 months under 60% of the profit", "hard": False, "ok": top3 is not None and top3 < 60, "value": top3, "need": "< 60%"},
        {"key": "dd", "name": "Test drawdown under 3x the average year", "hard": False, "ok": avg_year > 0 and dd_te < 3 * avg_year, "value": dd_te,
         "need": f"< {round(3 * avg_year, 1)}" if avg_year > 0 else ""}]
    eq, cur = [], 0.0
    for x in sorted(tr, key=lambda z: z["in"]):
        cur += _net(x, COST_I)
        eq.append([x["d"], round(cur, 2)])
    return {"at": iso(now_ny()), "hold": p["legPct"], "stop": p["stopBuf"], "grid": grid, "costs": costs, "months": mlist, "tickers": tick,
            "top1Share": None, "top3Months": top3, "trades": len(tr), "chosen": ch, "testDD": dd_te, "allDD": _dd([_net(x, COST_I) for x in tr]),
            "gates": gates, "passed": all(g["ok"] for g in gates if g["hard"]), "equity": eq[-400:], "split": {"cut": cut}, "cells": cells,
            "holds": legs, "stops": stops, "costBp": COST_I, "gridRow": "Leg \\ stop", "rowUnit": "% leg", "colUnit": "of the range past the end",
            "rTest": ch.get("rTest"), "intraday": True,
            "_trades": [{"s": x["s"], "in": x["d"], "out": x["d"], "pct": round(_net(x, COST_I), 3)} for x in tr]}
