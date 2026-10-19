"""Formula rules: new entry / exit conditions written as expressions over the data the app has (Alpaca candles and
trades, Unusual Whales gamma walls / flip / dark pool / options flow), e.g.

    close > vwap and crossUp(ema(close, 9), ema(close, 21)) and cvd > 0 and tod >= 15
    rsi(14) < 30 and close > putWall and rising(sqMom(20), 1)

The AI research agent (or you) writes them; this module checks and computes them with plain code. An expression is
parsed with Python's ast and only the names / functions / operators below are allowed: no attribute access, no
imports, no calls to anything else, so a formula can't do anything but arithmetic on these series.

Every series is aligned with the candles. A value that isn't known yet (warm-up, no walls that day) is None and makes
a comparison false. Everything at candle i uses candle i and before (no look-ahead): levels of the day (walls, flip,
dark pool) are those the app already uses for that day; opening range values exist only once the range is complete."""
import ast
import math
from datetime import datetime

SERIES = {
    "open": "candle open", "high": "candle high", "low": "candle low", "close": "candle close", "volume": "candle volume",
    "vwap": "VWAP (session VWAP intraday; anchored VWAP on swing hourly candles when loaded)",
    "delta": "estimated buy − sell volume of the candle (volume split by where it closes in its range)",
    "cvd": "cumulative delta from the day's open (intraday) / from the first candle (daily)",
    "tod": "minutes since 09:30 (0 = the 09:30 candle)", "dow": "weekday, 0 = Monday",
    "dayOpen": "open of the day", "dayHigh": "high of the day so far", "dayLow": "low of the day so far",
    "prevClose": "previous session close", "prevHigh": "previous session high", "prevLow": "previous session low",
    "gap": "today's open vs the previous close, %",
    "pmHigh": "pre-market high (1-minute day trading only)", "pmLow": "pre-market low (1-minute day trading only)",
    "putWall": "put wall of the day (Unusual Whales gamma)", "callWall": "call wall of the day", "flip": "gamma flip of the day",
    "dpAbove": "nearest dark pool level above the close", "dpBelow": "nearest dark pool level below the close",
    "flowPrem": "call alert premium ($) of the last 5 days, before this candle (swing only, Unusual Whales flow)",
    "vix": "previous VIX close (day trading)", "peer": "the other ticker vs its VWAP: +1 above, −1 below (day trading)",
}
FUNCS = {
    "ema": ("ema(x, n)", "exponential average of x over n candles"), "sma": ("sma(x, n)", "simple average"),
    "highest": ("highest(x, n)", "highest x of the last n candles (this one included)"),
    "lowest": ("lowest(x, n)", "lowest x of the last n candles"), "ref": ("ref(x, k)", "x k candles ago"),
    "change": ("change(x, k)", "x − x k candles ago"), "pct": ("pct(x, k)", "% change of x over k candles"),
    "stdev": ("stdev(x, n)", "standard deviation of x over n candles"), "rsi": ("rsi(n)", "RSI of the close"),
    "atr": ("atr(n)", "average true range"), "crossUp": ("crossUp(a, b)", "a crosses above b on this candle"),
    "crossDown": ("crossDown(a, b)", "a crosses below b on this candle"), "rising": ("rising(x, k)", "x > x k candles ago"),
    "falling": ("falling(x, k)", "x < x k candles ago"), "abs": ("abs(x)", "absolute value"), "min": ("min(a, b)", "smaller"),
    "max": ("max(a, b)", "larger"), "between": ("between(x, a, b)", "a <= x <= b"),
    "sqMom": ("sqMom(n)", "TTM squeeze momentum (n candles)"), "sqOn": ("sqOn(n)", "1 when the TTM squeeze is on (BB inside KC)"),
    "rvol": ("rvol(n)", "volume / its average of the last n candles"), "cvdSum": ("cvdSum(n)", "delta summed over the last n candles"),
    "orHigh": ("orHigh(n)", "high of the first n candles of the day (once they are done)"), "orLow": ("orLow(n)", "low of the first n candles"),
}
NUM_ARG = {"ema": 1, "sma": 1, "highest": 1, "lowest": 1, "ref": 1, "change": 1, "pct": 1, "stdev": 1, "rising": 1,
           "falling": 1, "rsi": 0, "atr": 0, "sqMom": 0, "sqOn": 0, "rvol": 0, "cvdSum": 0, "orHigh": 0, "orLow": 0}
ARITY = {"ema": 2, "sma": 2, "highest": 2, "lowest": 2, "ref": 2, "change": 2, "pct": 2, "stdev": 2, "rsi": 1, "atr": 1,
         "crossUp": 2, "crossDown": 2, "rising": 2, "falling": 2, "abs": 1, "min": 2, "max": 2, "between": 3, "sqMom": 1,
         "sqOn": 1, "rvol": 1, "cvdSum": 1, "orHigh": 1, "orLow": 1}
MAX_LEN = 400


class FormulaError(ValueError):
    pass


def spec():
    """What a formula may use (shown to the AI agent and in the app)."""
    return {"series": SERIES, "functions": {k: f"{a}: {b}" for k, (a, b) in FUNCS.items()},
            "operators": "+ - * /, < <= > >= == !=, and / or / not, numbers, parentheses",
            "examples": ["close > vwap and cvd > 0", "crossUp(ema(close, 9), ema(close, 21)) and tod >= 15",
                         "rsi(14) < 30 and close > putWall", "rising(sqMom(20), 1) and sqMom(20) < 0"]}


def check(expr, unavailable=()):
    """The parsed formula, or FormulaError saying what is wrong. unavailable: names this strategy has no data for."""
    if not isinstance(expr, str) or not expr.strip():
        raise FormulaError("empty formula")
    if len(expr) > MAX_LEN:
        raise FormulaError(f"formula longer than {MAX_LEN} characters")
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as e:
        raise FormulaError(f"not a valid formula: {e.msg}")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
                             ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Compare, ast.Lt, ast.LtE, ast.Gt,
                             ast.GtE, ast.Eq, ast.NotEq, ast.Load)):
            continue
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or isinstance(node.value, (int, float)):
                continue
            raise FormulaError("only numbers are allowed as constants")
        if isinstance(node, ast.Name):
            if node.id in ("True", "False"):
                continue
            if node.id in FUNCS and node.id not in SERIES:
                continue                               # checked with its call
            if node.id not in SERIES:
                raise FormulaError(f"unknown name “{node.id}”")
            if node.id in unavailable:
                raise FormulaError(f"no data for “{node.id}” in this strategy")
            continue
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCS:
                raise FormulaError(f"unknown function “{getattr(node.func, 'id', '?')}”")
            f = node.func.id
            if node.keywords or len(node.args) != ARITY[f]:
                raise FormulaError(f"{FUNCS[f][0]} takes {ARITY[f]} argument{'s' if ARITY[f] > 1 else ''}")
            if f in NUM_ARG:
                a = node.args[NUM_ARG[f]]
                v = a.value if isinstance(a, ast.Constant) else -a.operand.value if (isinstance(a, ast.UnaryOp) and isinstance(a.operand, ast.Constant)) else None
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not (1 <= v <= 500) or int(v) != v:
                    raise FormulaError(f"{FUNCS[f][0]}: the length must be a whole number 1–500")
            continue
        raise FormulaError(f"“{type(node).__name__}” is not allowed in a formula")
    return tree


# ---------------------------------------------------------------- series
def _ema(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        if x is None:
            out.append(e)
            continue
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def _roll(xs, n, f):
    out = []
    for i in range(len(xs)):
        w = [x for x in xs[max(0, i - n + 1):i + 1] if x is not None]
        out.append(f(w) if len(w) == min(n, i + 1) and len(w) == n else None)
    return out


def _rollsum(xs, n):
    out, s, cnt = [], 0.0, 0
    for i, x in enumerate(xs):
        s += x or 0
        if i >= n:
            s -= xs[i - n] or 0
        out.append(s if i >= n - 1 else None)
    return out


class Ctx:
    """The series of one set of candles, computed when a formula asks for them (and kept)."""

    def __init__(self, bars, intraday=True, walls=None, pre=None, dp=None, vix=None, peer=None, vwap=None, alerts=None):
        self.b, self.intraday = bars, intraday
        self.walls, self.pre, self.dpm = walls or {}, pre or {}, dp or {}
        self.vixm, self.peerm, self.vw, self.alerts = vix or {}, peer or {}, vwap, alerts
        self.memo = {}
        self.day = [b["t"][:10] for b in bars]

    def s(self, name):
        if name in self.memo:
            return self.memo[name]
        v = getattr(self, "_" + name)()
        self.memo[name] = v
        return v

    def _open(self):
        return [b["o"] for b in self.b]

    def _high(self):
        return [b["h"] for b in self.b]

    def _low(self):
        return [b["l"] for b in self.b]

    def _close(self):
        return [b["c"] for b in self.b]

    def _volume(self):
        return [float(b.get("v") or 0) for b in self.b]

    def _delta(self):
        out = []
        for i, b in enumerate(self.b):
            v, r = float(b.get("v") or 0), b["h"] - b["l"]
            pc = self.b[i - 1]["c"] if i else b["o"]
            buy = v * (b["c"] - b["l"]) / r if r > 0 else (v if b["c"] > pc else 0 if b["c"] < pc else v / 2)
            out.append(2 * buy - v)
        return out

    def _cvd(self):
        d, out, cum, cur = self.s("delta"), [], 0.0, None
        for i, x in enumerate(d):
            if self.intraday and self.day[i] != cur:
                cur, cum = self.day[i], 0.0
            cum += x
            out.append(cum)
        return out

    def _vwap(self):
        if self.vw is not None:
            return list(self.vw)
        out, pv, vv, cur = [], 0.0, 0.0, None
        for i, b in enumerate(self.b):
            if self.intraday and self.day[i] != cur:
                cur, pv, vv = self.day[i], 0.0, 0.0
            v = float(b.get("v") or 0)
            pv += (b["h"] + b["l"] + b["c"]) / 3 * v
            vv += v
            out.append(pv / vv if vv else None)
        return out

    def _tod(self):
        return [(int(b["t"][11:13]) * 60 + int(b["t"][14:16])) - 570 if len(b["t"]) >= 16 else 0 for b in self.b]

    def _dow(self):
        return [datetime.strptime(d, "%Y-%m-%d").weekday() for d in self.day]

    def _days(self):
        """{day: (open, high, low, close)} of each session in the candles."""
        if "_dd" not in self.memo:
            dd = {}
            for b in self.b:
                x = dd.get(b["t"][:10])
                dd[b["t"][:10]] = [b["o"], b["h"], b["l"], b["c"]] if not x else [x[0], max(x[1], b["h"]), min(x[2], b["l"]), b["c"]]
            self.memo["_dd"] = dd
        return self.memo["_dd"]

    def _so_far(self, k):
        out, cur, v = [], None, None
        for i, b in enumerate(self.b):
            if self.day[i] != cur:
                cur, v = self.day[i], (b["o"] if k == "o" else b["h"] if k == "h" else b["l"])
            v = v if k == "o" else max(v, b["h"]) if k == "h" else min(v, b["l"])
            out.append(v)
        return out

    def _dayOpen(self):
        return self._so_far("o")

    def _dayHigh(self):
        return self._so_far("h")

    def _dayLow(self):
        return self._so_far("l")

    def _prev(self, j):
        dd, ds = self._days(), sorted(self._days())
        pv = {ds[k]: dd[ds[k - 1]][j] for k in range(1, len(ds))}
        if not self.intraday:                          # daily candles: the previous candle
            return [None] + [self.b[i - 1][("o", "h", "l", "c")[j]] for i in range(1, len(self.b))]
        return [pv.get(d) for d in self.day]

    def _prevClose(self):
        return self._prev(3)

    def _prevHigh(self):
        return self._prev(1)

    def _prevLow(self):
        return self._prev(2)

    def _gap(self):
        po, pc = self.s("dayOpen"), self.s("prevClose")
        return [(o / c - 1) * 100 if c else None for o, c in zip(po, pc)]

    def _pmHigh(self):
        return [(self.pre.get(d) or (None, None))[0] for d in self.day]

    def _pmLow(self):
        return [(self.pre.get(d) or (None, None))[1] for d in self.day]

    def _wall(self, k):
        return [float(self.walls[d][k]) if (self.walls.get(d) or {}).get(k) else None for d in self.day]

    def _putWall(self):
        return self._wall("putWall")

    def _callWall(self):
        return self._wall("callWall")

    def _flip(self):
        return self._wall("flip")

    def _dpAbove(self):
        return [min((float(x) for x in self.dpm.get(d) or [] if float(x) > b["c"]), default=None) for d, b in zip(self.day, self.b)]

    def _dpBelow(self):
        return [max((float(x) for x in self.dpm.get(d) or [] if float(x) < b["c"]), default=None) for d, b in zip(self.day, self.b)]

    def _vix(self):
        return [self.vixm.get(d) for d in self.day]

    def _peer(self):
        return [self.peerm.get(b["t"]) for b in self.b]

    def _flowPrem(self):
        if self.alerts is None:
            return [None] * len(self.b)
        from datetime import timedelta
        al = sorted((str(a.get("t") or "")[:16], float(a.get("p") or 0)) for a in self.alerts if a.get("c"))
        ts = [a[0] for a in al]
        import bisect
        cum = [0.0]
        for _, p in al:
            cum.append(cum[-1] + p)
        out = []
        for b in self.b:
            t = b["t"][:16]
            t0 = (datetime.strptime(t[:10], "%Y-%m-%d") - timedelta(days=5)).strftime("%Y-%m-%d")
            i0, i1 = bisect.bisect_left(ts, t0), bisect.bisect_left(ts, t)
            out.append(cum[i1] - cum[i0])
        return out


# ---------------------------------------------------------------- evaluation
def _num(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


def _vec(x, n):
    return x if isinstance(x, list) else [x] * n


def _bin(a, b, n, f):
    a, b = _vec(a, n), _vec(b, n)
    out = []
    for x, y in zip(a, b):
        if x is None or y is None:
            out.append(None)
            continue
        try:
            out.append(f(x, y))
        except ZeroDivisionError:
            out.append(None)
    return out


def _ev(node, cx):
    n = len(cx.b)
    if isinstance(node, ast.Expression):
        return _ev(node.body, cx)
    if isinstance(node, ast.Constant):
        return float(node.value) if not isinstance(node.value, bool) else node.value
    if isinstance(node, ast.Name):
        if node.id == "True":
            return True
        if node.id == "False":
            return False
        return cx.s(node.id)
    if isinstance(node, ast.UnaryOp):
        v = _ev(node.operand, cx)
        if isinstance(node.op, ast.Not):
            return [None if x is None else not x for x in _vec(v, n)]
        if isinstance(node.op, ast.USub):
            return [None if x is None else -x for x in v] if isinstance(v, list) else -v
        return v
    if isinstance(node, ast.BinOp):
        f = {ast.Add: lambda x, y: x + y, ast.Sub: lambda x, y: x - y, ast.Mult: lambda x, y: x * y,
             ast.Div: lambda x, y: x / y}[type(node.op)]
        a, b = _ev(node.left, cx), _ev(node.right, cx)
        if not isinstance(a, list) and not isinstance(b, list):
            return f(a, b)
        return _bin(a, b, n, f)
    if isinstance(node, ast.BoolOp):
        vals = [_vec(_ev(v, cx), n) for v in node.values]
        out = []
        for i in range(n):
            xs = [v[i] for v in vals]
            if isinstance(node.op, ast.And):
                out.append(False if any(x is not None and not x for x in xs) else None if any(x is None for x in xs) else True)
            else:
                out.append(True if any(x for x in xs if x is not None) else None if any(x is None for x in xs) else False)
        return out
    if isinstance(node, ast.Compare):
        ops = {ast.Lt: lambda x, y: x < y, ast.LtE: lambda x, y: x <= y, ast.Gt: lambda x, y: x > y,
               ast.GtE: lambda x, y: x >= y, ast.Eq: lambda x, y: x == y, ast.NotEq: lambda x, y: x != y}
        left, res = _ev(node.left, cx), [True] * n
        for op, rt in zip(node.ops, node.comparators):
            right = _ev(rt, cx)
            r = _bin(left, right, n, ops[type(op)])
            res = [None if (p is None or q is None) else (p and q) for p, q in zip(res, r)]
            left = right
        return res
    if isinstance(node, ast.Call):
        return _call(node.func.id, node.args, cx)
    raise FormulaError("unsupported")


def _intarg(a):
    return int(a.value)


def _call(f, args, cx):
    n = len(cx.b)
    key = ("call", ast.dump(ast.Call(func=ast.Name(id=f), args=args, keywords=[])))
    if key in cx.memo:
        return cx.memo[key]
    if f in ("rsi", "atr", "sqMom", "sqOn", "rvol", "cvdSum", "orHigh", "orLow"):
        k = _intarg(args[0])
        if f == "rsi":
            cl = cx.s("close")
            up = [None] + [max(0.0, cl[i] - cl[i - 1]) for i in range(1, n)]
            dn = [None] + [max(0.0, cl[i - 1] - cl[i]) for i in range(1, n)]
            a, d, out = None, None, []
            for i in range(n):                         # Wilder's smoothing
                if i == 0:
                    out.append(None)
                    continue
                a = up[i] if a is None else (a * (k - 1) + up[i]) / k
                d = dn[i] if d is None else (d * (k - 1) + dn[i]) / k
                out.append(None if i < k else (100.0 if d == 0 else 100 - 100 / (1 + a / d)))
            v = out
        elif f == "atr":
            b = cx.b
            tr = [b[0]["h"] - b[0]["l"]] + [max(b[i]["h"] - b[i]["l"], abs(b[i]["h"] - b[i - 1]["c"]), abs(b[i]["l"] - b[i - 1]["c"])) for i in range(1, n)]
            v = _roll(tr, k, lambda w: sum(w) / len(w))
        elif f in ("sqMom", "sqOn"):
            import wallwatch
            sq = wallwatch.squeeze(cx.b, k)
            v = [(x["mom"] if f == "sqMom" else (1.0 if x["on"] else 0.0)) if x else None for x in sq]
        elif f == "rvol":
            vol = cx.s("volume")
            av = _roll([None] + vol[:-1], k, lambda w: sum(w) / len(w))
            v = [x / a if a else None for x, a in zip(vol, av)]
        elif f == "cvdSum":
            v = _rollsum(cx.s("delta"), k)
        else:                                          # opening range of the first k candles, once they are done
            v, cur, cnt, hi, lo = [], None, 0, None, None
            for i, b in enumerate(cx.b):
                if cx.day[i] != cur:
                    cur, cnt, hi, lo = cx.day[i], 0, b["h"], b["l"]
                cnt += 1
                if cnt <= k:
                    hi, lo = max(hi, b["h"]), min(lo, b["l"])
                    v.append(None if cnt < k else (hi if f == "orHigh" else lo))
                else:
                    v.append(hi if f == "orHigh" else lo)
    elif f in ("ema", "sma", "highest", "lowest", "ref", "change", "pct", "stdev", "rising", "falling"):
        x, k = _vec(_ev(args[0], cx), n), _intarg(args[1])
        if f == "ema":
            v = _ema(x, k)
        elif f == "sma":
            v = _roll(x, k, lambda w: sum(w) / len(w))
        elif f == "highest":
            v = _roll(x, k, max)
        elif f == "lowest":
            v = _roll(x, k, min)
        elif f == "stdev":
            v = _roll(x, k, lambda w: (sum((y - sum(w) / len(w)) ** 2 for y in w) / len(w)) ** 0.5)
        else:
            p = [None] * k + x[:-k] if k < n else [None] * n
            if f == "ref":
                v = p
            elif f == "change":
                v = _bin(x, p, n, lambda a, b: a - b)
            elif f == "pct":
                v = _bin(x, p, n, lambda a, b: (a / b - 1) * 100)
            elif f == "rising":
                v = _bin(x, p, n, lambda a, b: a > b)
            else:
                v = _bin(x, p, n, lambda a, b: a < b)
    elif f in ("crossUp", "crossDown"):
        a, b = _vec(_ev(args[0], cx), n), _vec(_ev(args[1], cx), n)
        v = [None]
        for i in range(1, n):
            if None in (a[i], b[i], a[i - 1], b[i - 1]):
                v.append(None)
            else:
                v.append(a[i - 1] <= b[i - 1] and a[i] > b[i] if f == "crossUp" else a[i - 1] >= b[i - 1] and a[i] < b[i])
    elif f == "abs":
        x = _ev(args[0], cx)
        v = [None if y is None else abs(y) for y in x] if isinstance(x, list) else abs(x)
    elif f in ("min", "max"):
        v = _bin(_ev(args[0], cx), _ev(args[1], cx), n, min if f == "min" else max)
    elif f == "between":
        x, a, b = _ev(args[0], cx), _ev(args[1], cx), _ev(args[2], cx)
        lo = _bin(x, a, n, lambda p, q: p >= q)
        hi = _bin(x, b, n, lambda p, q: p <= q)
        v = [None if p is None or q is None else (p and q) for p, q in zip(lo, hi)]
    else:
        raise FormulaError(f"unknown function {f}")
    cx.memo[key] = v
    return v


def evaluate(expr, cx, unavailable=()):
    """[True / False] per candle (None = not known -> False)."""
    v = _ev(check(expr, unavailable), cx)
    v = _vec(v, len(cx.b))
    return [bool(x) if x is not None else False for x in v]


UNAVAILABLE = {"day": ("flowPrem",), "swing": ("pmHigh", "pmLow", "vix", "peer")}
