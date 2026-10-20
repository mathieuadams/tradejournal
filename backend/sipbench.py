"""Stocks in play as the research universe: every research test (the AI agent's ideas, the evolution, the robustness
and holdout checks) can run on each day's top stocks by relative volume instead of a fixed ticker list.

History (built once, extended every evening), from the stocks-in-play data (stockday.py: the universe and the
opening-range / pre-market volume of every day):
  * the picks of each day: the top SIP_TOP stocks by opening-range RVOL (the first 5 minutes vs their 14-day
    average), with price / volume / ATR filters; known at 09:35, so no entry is counted before 09:35;
  * their 5-minute candles of the day (09:30-16:00), the 30-minute candles of the 5 previous sessions (hourly
    structure, prior-day levels) and of the day's pre-market (pre-market high / low), plus QQQ's 5-minute candles
    (relative strength, the market);
  * stored as text in DynamoDB ("SIPDATA" B#day#part), one day = 1-3 items.

The market context of the formulas (options flow, dealer gamma, sweeps, breadth, VIXY, events) is QQQ's / the
market's: per-stock options flow and gamma walls are not in the history (Unusual Whales per stock, every day, would
be thousands of calls). The universe is built from the stocks active today (delisted ones are missing: results
look slightly better than they were)."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import db
from util import iso, now_ny

PK = "SIPDATA"
STATE_SK = "SIPPREP"
SIP_TOP = 15
PER_ITEM = 6
MIN_ATR = 1.0                                          # $: stocks moving less than this a day are not picked
MIN_HM = "09:35"                                       # the picks are known once the 5-minute opening range is done


# ---------------------------------------------------------------- compact storage
def _enc(bars):
    out = []
    for b in bars:
        t = b["t"].replace("-", "").replace("T", "").replace(":", "")[:12]
        out.append(",".join([t] + ["%.6g" % float(b[k]) for k in ("o", "h", "l", "c")] + [str(int(b.get("v") or 0))]))
    return ";".join(out)


def _dec(s):
    out = []
    for r in (s or "").split(";"):
        if not r:
            continue
        p = r.split(",")
        t = p[0]
        out.append({"t": f"{t[:4]}-{t[4:6]}-{t[6:8]}T{t[8:10]}:{t[10:12]}", "o": float(p[1]), "h": float(p[2]), "l": float(p[3]),
                    "c": float(p[4]), "v": int(p[5])})
    return out


def stored_days():
    return sorted((db.get(PK, "DAYS") or {}).get("days") or [])


def _put_day(d, picks, b5, h30, q5):
    syms = [p["sym"] for p in picks]
    parts = [syms[i:i + PER_ITEM] for i in range(0, len(syms), PER_ITEM)] or [[]]
    for k, ss in enumerate(parts):
        db.put({"PK": PK, "SK": f"B#{d}#{k}", "n": len(parts),
                "picks": json.dumps([p for p in picks if p["sym"] in ss]),
                "b": {s: _enc(b5.get(s) or []) for s in ss}, "h": {s: _enc(h30.get(s) or []) for s in ss},
                **({"q": _enc(q5)} if k == 0 else {})})
    idx = db.get(PK, "DAYS") or {}
    db.put({"PK": PK, "SK": "DAYS", "days": sorted(set(idx.get("days") or []) | {d})})


def get_day(d):
    """{"picks": [...], "b": {sym: 5-min}, "h": {sym: 30-min prior + pre-market}, "q": QQQ 5-min} or None."""
    rows = db.q_prefix(PK, f"B#{d}#")
    if not rows:
        return None
    out = {"picks": [], "b": {}, "h": {}, "q": []}
    for r in rows:
        out["picks"] += json.loads(r.get("picks") or "[]")
        out["b"].update({s: _dec(v) for s, v in (r.get("b") or {}).items()})
        out["h"].update({s: _dec(v) for s, v in (r.get("h") or {}).items()})
        if r.get("q"):
            out["q"] = _dec(r["q"])
    return out


# ---------------------------------------------------------------- picks of every day (as the live scan ranks them)
def picks_history(days_wanted, top=SIP_TOP):
    """{day: [pick]} for the stored stocks-in-play days (the 14 previous days of each give its RVOL)."""
    import stockday
    P = {**stockday.PARAMS, "topN": top, "minRvol": 1.0, "rvolMode": "orb", "orbMin": 5, "minAtr": MIN_ATR}
    rdays = stockday.stored_days("R")
    want = set(days_wanted)
    keep = [d for d in rdays if d <= max(want)] if want else []
    i0 = max(0, (keep.index(min(want)) if min(want) in keep else 0) - 15)
    keep = keep[i0:]
    base = 1 + stockday.ORBS.index(5) * 5
    roll, out = {}, {}
    for d in keep:
        today = stockday.get_day("R", d)
        if d in want:
            uni = stockday.get_day("U", d)
            out[d] = [{k: p[k] for k in ("sym", "rvol", "pmRvol", "atr", "dir")} for p in stockday._rank(uni, today, roll, P)]
        for s, w in today.items():
            h = roll.setdefault(s, [])
            h.append((w[base + 4], w[0]))
            if len(h) > 14:
                del h[0]
    return out


# ---------------------------------------------------------------- build / extend the history
def prep_state(sub=None):
    r = db.get(PK, STATE_SK) or {}
    return {k: r.get(k) for k in ("running", "progress", "error", "days", "at")} | {"stored": len(stored_days())}


def prep(sub, days=500, budget_s=470):
    """Stores the picks + candles of the last `days` stocks-in-play days that aren't stored yet. Resumable: returns
    {"left": n} when the time ran out (the job starts itself again)."""
    import stockday
    t0 = time.time()
    c = stockday.data_creds(sub)
    if not c:
        raise ValueError("Connect an Alpaca account to load the stock data.")
    rdays = stockday.stored_days("R")
    if len(rdays) < 20:
        raise ValueError("The stocks-in-play history (universe + opening ranges) is missing: it builds every morning at 09:05 ET.")
    have = set(stored_days())
    want = [d for d in rdays[-days:] if d not in have]
    db.put({"PK": PK, "SK": STATE_SK, "running": iso(now_ny()), "progress": f"{len(want)} days to load", "days": days, "error": None})
    if not want:
        db.put({"PK": PK, "SK": STATE_SK, "running": None, "progress": "up to date", "days": days, "at": iso(now_ny())})
        return {"left": 0}
    picks = picks_history(want)

    def one(d):
        pk = picks.get(d) or []
        if not pk:
            _put_day(d, [], {}, {}, [])
            return d
        day = datetime.strptime(d, "%Y-%m-%d")
        syms = [p["sym"] for p in pk]
        b5 = stockday.multi_bars(c, syms + ["QQQ"], "5Min", day.replace(hour=9, minute=30), day.replace(hour=16), max_pages=5)
        h = stockday.multi_bars(c, syms, "30Min", (day - timedelta(days=9)).replace(hour=4), day.replace(hour=9, minute=29), max_pages=8)
        h30 = {}
        for s in syms:
            prior = [b for b in h.get(s) or [] if b["t"][:10] < d and "09:30" <= b["t"][11:16] < "16:00"]
            ds = sorted({b["t"][:10] for b in prior})[-5:]
            pm = [b for b in h.get(s) or [] if b["t"][:10] == d and "04:00" <= b["t"][11:16] < "09:30"]
            h30[s] = [b for b in prior if b["t"][:10] in ds] + pm
        reg = {s: [b for b in b5.get(s) or [] if "09:30" <= b["t"][11:16] < "16:00"] for s in syms}
        _put_day(d, pk, reg, h30, [b for b in b5.get("QQQ") or [] if "09:30" <= b["t"][11:16] < "16:00"])
        return d
    done = 0
    with ThreadPoolExecutor(max_workers=3) as ex:
        for i in range(0, len(want), 6):
            if time.time() - t0 > budget_s:
                break
            for _ in ex.map(one, want[i:i + 6]):
                done += 1
            db.put({"PK": PK, "SK": STATE_SK, "running": iso(now_ny()), "progress": f"{done} of {len(want)} days loaded", "days": days, "error": None})
    left = len(want) - done
    db.put({"PK": PK, "SK": STATE_SK, "running": iso(now_ny()) if left else None,
            "progress": f"{len(stored_days())} days stored" + (f", {left} left (continuing)" if left else ""), "days": days,
            "at": iso(now_ny()), "error": None})
    return {"left": left}


def prep_job(sub, params=None):
    import research
    days = int((params or {}).get("days") or 500)
    try:
        r = prep(sub, days)
    except Exception as e:
        db.put({"PK": PK, "SK": STATE_SK, "running": None, "error": str(e)[:200], "at": iso(now_ny())})
        return {"error": str(e)[:200]}
    if r.get("left"):
        research._invoke(sub, "sipprep", {"days": days})
    return r


# ---------------------------------------------------------------- the bench (same interface as research.DayBench)
class SipBench:
    """Each stock-day of the stored picks is one series: 30-minute candles of its 5 previous sessions and of its
    pre-market, then its 5-minute candles of the day (the strategies run with barMin 5). Trades before 09:35 don't
    count (the picks are only known then)."""

    def __init__(self, sub, days, base, holdout=0, top=SIP_TOP):
        import daytrade
        self.errors, self.feed = [], {}
        self.hold = max(0, int(holdout or 0))
        ds = stored_days()[-(days + self.hold):]
        if not ds:
            self.errors.append("No stocks-in-play history yet: build it first (AI tab → criteria → Build stocks-in-play history).")
        try:
            vix = daytrade.vix_map()
        except Exception as e:
            vix = {}
            self.errors.append(f"VIX: {str(e)[:60]}")
        self.data = []
        try:
            import marketctx
            mxall = marketctx.for_symbol("QQQ", ds)        # the market's flow / gamma / breadth, for every pick
        except Exception as e:
            mxall = {}
            self.errors.append(f"market context: {str(e)[:60]}")
        mx = {"mx": mxall, "snaps": {}}
        for d in ds:
            x = get_day(d)
            if not x or not x["picks"]:
                continue
            peer = daytrade.peer_map(x["q"]) if x["q"] else {}
            for p in [q for q in x["picks"] if float(q.get("atr") or 0) >= MIN_ATR][:top]:
                s = p["sym"]
                today = x["b"].get(s) or []
                if len(today) < 20:
                    continue
                self.data.append({"sym": s, "allb": (x["h"].get(s) or []) + today, "show": [d], "walls": {}, "pick": p,
                                  "ctx": {"dp": {}, "vix": vix, "peer": peer, "cache": {}, **mx}})
        days_all = sorted({x["show"][0] for x in self.data})
        self.cut = days_all[-self.hold] if self.hold and len(days_all) > self.hold else None
        rd = [d for d in days_all if not self.cut or d < self.cut]
        self.first, self.mid, self.last = (rd[0], rd[len(rd) // 2], rd[-1]) if rd else (None, None, None)
        self.nDays, self.nSeries = len(rd), len(self.data)

    def _all(self, P):
        import dtsearch
        P = {**P, "barMin": 5}
        return [r for r in dtsearch.evaluate(self.data, P) if r["at"][11:16] >= MIN_HM]

    def rows(self, P):
        return [r for r in self._all(P) if not self.cut or r["at"][:10] < self.cut]

    def hold_rows(self, P):
        return [r for r in self._all(P) if self.cut and r["at"][:10] >= self.cut]
