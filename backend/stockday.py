"""Day trading stocks in play: the opening-range breakout on the top stocks by relative volume (RVOL), with the VWAP
side and the VWAP exit as options. On a THIRD Alpaca paper account.

Rules (ORB / stocks-in-play and VWAP papers):
  universe   price > minPrice, 14-day average volume > minAvgVol, 14-day ATR > minAtr (known the evening before)
  RVOL       volume of the opening range (first orbMin minutes) / its average over the previous 14 days
             (or the pre-market volume 04:00-09:30 / its 14-day average), >= minRvol, then the top N
  direction  the opening-range candle green -> longs only, red -> shorts only, doji (body < dojiPct of the range) -> skip
  entry      stop order at the opening-range high (short: low), until entryEnd
  stop       stopAtrPct % of the 14-day ATR; exit at the close (flatT), no target
  options    VWAP side (long only above the session VWAP, short only below), exit on a 1-minute CLOSE across the
             VWAP, no entries 12:00-15:00, done for the day after N VWAP stop-outs
  sizing     riskPct % of the capital at risk per trade, at most maxLev x leverage across the topN positions

Data (Alpaca SIP, history from 2016): daily candles of every active US stock (universe), 30-minute candles
04:00-09:30 (pre-market volume) and 5-minute candles 09:30-10:30 (opening ranges) of the candidates of each day,
stored once per day ("SDDATA" U#day / R#day); 1-minute candles of the top N of each day for the trades.
"""
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import db
from util import BadRequest, iso, now_ny, ny_to_utc, utc_to_ny

SK = "SDCFG"
CRED_SK = "BROKER#alpacastock"
DATA_PK = "SDDATA"
PREP_SK = "SDPREP"
BT_SK = "SDBT"
DATA = "https://data.alpaca.markets"
TRADING = "https://paper-api.alpaca.markets"

PARAMS = {"enabled": False, "trade": False, "longs": True, "shorts": True,
          "minPrice": 5.0, "minAvgVol": 1000000, "minAtr": 0.5, "minRvol": 1.0, "topN": 20, "rvolMode": "orb",
          "orbMin": 5, "dirLock": True, "dojiPct": 10.0, "stopAtrPct": 10.0, "entryEnd": "11:30", "flatT": "15:55",
          "vwapSide": False, "cvdSide": False, "vwapExit": False, "skipMid": False, "vwapCap": 0, "riskPct": 1.0, "maxLev": 4.0,
          "exclude": []}
INTS = {"minAvgVol": (0, 10 ** 10), "topN": (1, 100), "orbMin": (5, 30), "vwapCap": (0, 50)}
FLOATS = {"minPrice": (0, 10000), "minAtr": (0, 1000), "minRvol": (0, 1000), "dojiPct": (0, 100), "stopAtrPct": (0.5, 500),
          "riskPct": (0.05, 10), "maxLev": (0.1, 10)}
BOOLS = ("enabled", "trade", "longs", "shorts", "dirLock", "vwapSide", "cvdSide", "vwapExit", "skipMid")
ORBS = (5, 15, 30)
RV_BUCKETS = ((0, 1, "< 1x"), (1, 2, "1-2x"), (2, 5, "2-5x"), (5, 10, "5-10x"), (10, 30, "10-30x"), (30, 1e12, "> 30x"))


# ---------------- settings ----------------

def config(sub):
    c = db.get(db.upk(sub), SK) or {}
    out = {k: c.get(k, v) for k, v in PARAMS.items()}
    for k in INTS:
        out[k] = int(out[k])
    for k in FLOATS:
        out[k] = float(out[k])
    return out


def clean_cfg(cur, body):
    cur = dict(cur)
    for k, (lo, hi) in INTS.items():
        if k in body:
            try:
                cur[k] = int(max(lo, min(hi, float(body[k]))))
            except (TypeError, ValueError):
                pass
    if cur["orbMin"] not in ORBS:
        cur["orbMin"] = min(ORBS, key=lambda x: abs(x - cur["orbMin"]))
    for k, (lo, hi) in FLOATS.items():
        if k in body:
            try:
                cur[k] = max(lo, min(hi, round(float(body[k]), 4)))
            except (TypeError, ValueError):
                pass
    for k in BOOLS:
        if k in body:
            cur[k] = bool(body[k])
    if body.get("rvolMode") in ("orb", "pm"):
        cur["rvolMode"] = body["rvolMode"]
    for k in ("entryEnd", "flatT"):
        if k in body:
            v = str(body[k] or "").strip()
            try:
                h, m = v.split(":")
                if 9 <= int(h) <= 16 and 0 <= int(m) < 60:
                    cur[k] = f"{int(h):02d}:{int(m):02d}"
            except ValueError:
                pass
    if "exclude" in body:
        ex = body["exclude"]
        ex = ex.replace(",", " ").split() if isinstance(ex, str) else ex
        cur["exclude"] = sorted({str(x).strip().upper()[:10] for x in ex or [] if str(x).strip()})[:200]
    return cur


def save_config(sub, body):
    from decimal import Decimal
    cur = clean_cfg(config(sub), body or {})
    db.put({"PK": db.upk(sub), "SK": SK, **{k: Decimal(str(v)) if isinstance(v, float) else v for k, v in cur.items()}})
    return cur


def creds(sub):
    import alpaca
    return alpaca.creds(sub, CRED_SK)


def data_creds(sub):
    import alpaca
    import daytrade
    return creds(sub) or daytrade.creds(sub) or alpaca.creds(sub)


def connect(sub, key, secret):
    import alpaca
    key, secret = (key or "").strip(), (secret or "").strip()
    if not key or not secret:
        raise BadRequest("Enter the API key and secret of the paper account.")
    for sk in ("BROKER#alpaca", "BROKER#alpacaday"):
        it = db.get(db.upk(sub), sk) or {}
        if it.get("keyHint") and it["keyHint"] == key[-4:]:
            raise BadRequest("These look like the keys of another account here: use a THIRD Alpaca paper account for the stocks.")
    alpaca.save_creds(sub, key, secret, "paper", "sip", sk=CRED_SK)
    return view(sub)


# ---------------- Alpaca data ----------------

def _get(c, url, q):
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(q)}",
                                 headers={"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"]})
    for n in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and n < 5:                 # rate limit (200 / minute on the free data plan)
                time.sleep(4 * (n + 1))
                continue
            if e.code in (401, 403):
                raise BadRequest("Alpaca rejected the data request (keys or data plan).")
            raise BadRequest(f"Alpaca returned an error ({e.code}): {e.read().decode(errors='replace')[:160]}")
        except urllib.error.URLError as e:
            if n < 2:
                time.sleep(2)
                continue
            raise BadRequest(f"Alpaca couldn't be reached: {e.reason}")


def _utc(local):
    return ny_to_utc(local).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ny(t):
    return utc_to_ny(datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")).strftime("%Y-%m-%dT%H:%M")


def multi_bars(c, symbols, tf, start_local, end_local, feed="sip", max_pages=60):
    """Bars of many symbols at once: {sym: [{t (NY), o, h, l, c, v}]}."""
    out, token = {}, None
    for _ in range(max_pages):
        q = {"symbols": ",".join(symbols), "timeframe": tf, "start": _utc(start_local), "end": _utc(end_local),
             "limit": 10000, "adjustment": "split" if tf == "1Day" else "raw", "feed": feed}
        if token:
            q["page_token"] = token
        d = _get(c, f"{DATA}/v2/stocks/bars", q) or {}
        for s, bs in (d.get("bars") or {}).items():
            out.setdefault(s, []).extend({"t": _ny(b["t"]), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v") or 0} for b in bs)
        token = d.get("next_page_token")
        if not token:
            break
    return out


def assets(c):
    """Active, tradable US stocks (no warrants / units / rights / preferreds: plain 1-5 letter symbols)."""
    hit = db.get(DATA_PK, "ASSETS")
    if hit and time.time() - hit.get("at", 0) < 7 * 86400 and hit.get("syms"):
        return hit["syms"]
    req = urllib.request.Request(f"{TRADING}/v2/assets?status=active&asset_class=us_equity",
                                 headers={"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"]})
    with urllib.request.urlopen(req, timeout=60) as r:
        rows = json.loads(r.read())
    syms = sorted(a["symbol"] for a in rows if a.get("tradable") and a.get("exchange") in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS")
                  and a["symbol"].isalpha() and 1 <= len(a["symbol"]) <= 5)
    db.put({"PK": DATA_PK, "SK": "ASSETS", "at": int(time.time()), "syms": syms})
    return syms


def trading_days(c, start, end):
    """Market days from Alpaca's calendar (YYYY-MM-DD)."""
    req = urllib.request.Request(f"{TRADING}/v2/calendar?start={start}&end={end}",
                                 headers={"APCA-API-KEY-ID": c["key"], "APCA-API-SECRET-KEY": c["secret"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return [d["date"] for d in json.loads(r.read())]


# ---------------- universe + RVOL history ----------------

def universe_from_daily(daily, days, P=None):
    """daily: {sym: [daily candles]} -> {day: {sym: [atr14, avgVol14, prevClose]}} for the symbols that pass the
    universe filter, using only the candles BEFORE each day (today included, before its own daily candle exists)."""
    import bisect
    P = {**PARAMS, **(P or {})}
    out = {d: {} for d in days}
    for sym, bs in daily.items():
        bs = sorted(bs, key=lambda b: b["t"])
        ts = [b["t"][:10] for b in bs]
        trs, vols = [], []
        for i, b in enumerate(bs):
            pc0 = bs[i - 1]["c"] if i else b["o"]
            trs.append(max(b["h"] - b["l"], abs(b["h"] - pc0), abs(b["l"] - pc0)))
            vols.append(b["v"] or 0)
        for d in days:
            k = bisect.bisect_left(ts, d)              # candles strictly before d
            if k < 15:
                continue
            atr, av, pc = sum(trs[k - 14:k]) / 14, sum(vols[k - 14:k]) / 14, bs[k - 1]["c"]
            if pc > P["minPrice"] and av > P["minAvgVol"] and atr > P["minAtr"]:
                out[d][sym] = [round(atr, 4), int(av), round(pc, 4)]
    return out


# compact storage: one string per symbol, split over several items (DynamoDB items are limited to 400 KB)
PART = 700


def _enc(v):
    return ",".join(str(int(x)) if float(x).is_integer() else "%.6g" % x for x in v)


def _dec(sv):
    return [float(x) for x in sv.split(",")]


def put_day(kind, d, rows):
    syms = sorted(rows)
    parts = [syms[i:i + PART] for i in range(0, len(syms), PART)] or [[]]
    for k, ps in enumerate(parts):
        db.put({"PK": DATA_PK, "SK": f"{kind}#{d}#{k}", "n": len(parts), "m": {s: _enc(rows[s]) for s in ps}})
    if kind in ("U", "R"):                              # the list of stored days (read without loading the days)
        idx = db.get(DATA_PK, f"DAYS#{kind}") or {}
        days = sorted(set(idx.get("days") or []) | {d})
        db.put({"PK": DATA_PK, "SK": f"DAYS#{kind}", "days": days})


def get_day(kind, d):
    out = {}
    for r in db.q_prefix(DATA_PK, f"{kind}#{d}#"):
        out.update({s: _dec(v) for s, v in (r.get("m") or {}).items()})
    return out


def stored_days(kind):
    return sorted((db.get(DATA_PK, f"DAYS#{kind}") or {}).get("days") or [])


def window_stats(pm_bars, b5):
    """One day of one symbol: [pre-market volume, then o,h,l,c,v of the first 5 / 15 / 30 minutes]."""
    pmv = int(sum(b["v"] or 0 for b in pm_bars if "04:00" <= b["t"][11:16] < "09:30"))
    reg = sorted((b for b in b5 if "09:30" <= b["t"][11:16] < "10:30"), key=lambda b: b["t"])
    out = [pmv]
    for m in ORBS:
        w = [b for b in reg if b["t"][11:16] < f"{9 + (30 + m) // 60:02d}:{(30 + m) % 60:02d}"]
        if not w or w[0]["t"][11:16] != "09:30":
            return None
        out += [w[0]["o"], max(b["h"] for b in w), min(b["l"] for b in w), w[-1]["c"], int(sum(b["v"] or 0 for b in w))]
    return out


def prep_status(sub):
    st = db.get(db.upk(sub), PREP_SK) or {}
    return {k: v for k, v in st.items() if k not in ("PK", "SK")}


def prep(sub, days=500, budget_s=480, invoke_next=None):
    """Builds (or extends) the stored history: universe for every day, then the RVOL windows of the candidates of every
    day. Resumes where it stopped; calls invoke_next() when the time budget ends before it is done."""
    from decimal import Decimal
    t0 = time.time()
    c = data_creds(sub)
    if not c:
        raise BadRequest("Connect an Alpaca account to load the stock data.")
    pk = db.upk(sub)
    st = db.get(pk, PREP_SK) or {}
    today = now_ny().strftime("%Y-%m-%d")
    end = (now_ny() - timedelta(days=1)).strftime("%Y-%m-%d")
    cal = trading_days(c, (now_ny() - timedelta(days=int(days * 1.5) + 10)).strftime("%Y-%m-%d"), today)
    want = [d for d in cal if d <= today][-days:]
    have_u = set(stored_days("U"))
    need_u = [d for d in want if d not in have_u]
    st.update({"PK": pk, "SK": PREP_SK, "running": iso(now_ny()), "days": days, "phase": "universe", "error": None})
    db.put(st)
    try:
        if need_u:                                      # daily candles of every stock -> the universe of the missing days
            syms = assets(c)
            first = (datetime.strptime(need_u[0], "%Y-%m-%d") - timedelta(days=40)).strftime("%Y-%m-%d")
            uni = {d: {} for d in need_u}
            done = int(st.get("uChunk") or 0) if st.get("uFor") == need_u[0] else 0
            chunks = [syms[i:i + 100] for i in range(0, len(syms), 100)]
            for ci in range(done, len(chunks)):
                if time.time() - t0 > budget_s:
                    break
                daily = multi_bars(c, chunks[ci], "1Day", datetime.strptime(first, "%Y-%m-%d"),
                                   min(datetime.strptime(need_u[-1], "%Y-%m-%d") + timedelta(hours=20),
                                       now_ny().replace(tzinfo=None) - timedelta(minutes=16)))
                for d, m in universe_from_daily(daily, need_u).items():
                    uni[d].update(m)
                done = ci + 1
                st.update(uChunk=done, uFor=need_u[0], uOf=len(chunks))
            # merge into the stored partial days (several runs)
            for d in need_u:
                part = get_day("UP", d)
                merged = {**part, **uni[d]}
                if done >= len(chunks):
                    put_day("U", d, merged)
                    for r in db.q_prefix(DATA_PK, f"UP#{d}#"):
                        db.delete(DATA_PK, r["SK"])
                else:
                    put_day("UP", d, merged)
            if done < len(chunks):
                st.update(phase="universe", progress=f"universe {done}/{len(chunks)} groups of stocks", running=None)
                db.put(st)
                if invoke_next:
                    invoke_next()
                return prep_status(sub)
            st.pop("uChunk", None)
        st["phase"] = "rvol"
        have_r = set(stored_days("R"))
        need_r = [d for d in want if d not in have_r and d <= end]
        k = 0
        for d in need_r:
            if time.time() - t0 > budget_s:
                break
            syms = sorted(get_day("U", d))
            day = datetime.strptime(d, "%Y-%m-%d")
            rows = {}

            def chunk(cs):
                pm = multi_bars(c, cs, "30Min", day.replace(hour=4), day.replace(hour=9, minute=30))
                b5 = multi_bars(c, cs, "5Min", day.replace(hour=9, minute=30), day.replace(hour=10, minute=30))
                res = {}
                for s in cs:
                    w = window_stats(pm.get(s) or [], b5.get(s) or [])
                    if w:
                        res[s] = w
                return res
            with ThreadPoolExecutor(max_workers=3) as ex:
                for res in ex.map(chunk, [syms[i:i + 200] for i in range(0, len(syms), 200)]):
                    rows.update(res)
            put_day("R", d, rows)
            k += 1
        left = len(need_r) - k
        st.update(phase="done" if not left else "rvol", progress=(f"opening ranges: {left} days left" if left else "ready"),
                  running=None, uDays=len(stored_days("U")), rDays=len(stored_days("R")), at=iso(now_ny()))
        db.put(st)
        if left and invoke_next:
            invoke_next()
        return prep_status(sub)
    except Exception as e:
        st.update(running=None, error=str(e)[:200])
        db.put(st)
        raise


def coverage(want=None):
    u, r = stored_days("U"), stored_days("R")
    return {"uDays": len(u), "rDays": len(r), "first": r[0] if r else None, "last": r[-1] if r else None}


# ---------------- selection + engine ----------------

def _f(x):
    return float(x) if x is not None else None


def rank_day(d, uni, rv_hist, P):
    """The stocks in play of day d: [{sym, rvol, pmRvol, atr, o,h,l,c (opening range), dir}] best first.
    rv_hist: the window rows of the previous days, today last."""
    base = 1 + ORBS.index(int(P["orbMin"])) * 5
    today = rv_hist[-1] if rv_hist else {}
    roll = {}
    for p in rv_hist[:-1][-14:]:
        for s, w in p.items():
            roll.setdefault(s, []).append((float(w[base + 4]), float(w[0])))
    return _rank(uni, today, roll, P)


def _rank(uni, today, roll, P):
    base = 1 + ORBS.index(int(P["orbMin"])) * 5
    out = []
    ex = set(P.get("exclude") or [])
    for s, w in today.items():
        if s not in uni or s in ex:
            continue
        h = roll.get(s) or []
        if len(h) < 5:
            continue
        av = sum(x[0] for x in h) / len(h)
        ap = sum(x[1] for x in h) / len(h)
        w = [float(x) for x in w]
        rv = w[base + 4] / av if av > 0 else 0
        prv = w[0] / ap if ap > 0 else 0
        o, hh, l, cl = w[base], w[base + 1], w[base + 2], w[base + 3]
        body, rng = cl - o, hh - l
        direc = 0 if rng <= 0 or abs(body) < float(P["dojiPct"]) / 100 * rng else (1 if body > 0 else -1)
        out.append({"sym": s, "rvol": round(rv, 2), "pmRvol": round(prv, 2), "atr": float(uni[s][0]), "o": o, "h": hh, "l": l, "c": cl, "dir": direc})
    key = "pmRvol" if P.get("rvolMode") == "pm" else "rvol"
    out = [x for x in out if x[key] >= float(P["minRvol"])]
    out.sort(key=lambda x: -x[key])
    return out[: int(P["topN"])]


def vwap_series(bars):
    pv = vv = 0.0
    out = []
    for b in bars:
        v = float(b.get("v") or 0) or 1.0
        pv += (b["h"] + b["l"] + b["c"]) / 3 * v
        vv += v
        out.append(pv / vv)
    return out


def simulate(pick, bars, P):
    """One stock, one day: the trade (or None). bars: 1-minute regular-session candles of the day."""
    m = int(P["orbMin"])
    end_or = f"{9 + (30 + m) // 60:02d}:{(30 + m) % 60:02d}"
    side = pick["dir"]
    if P.get("dirLock", True):
        if side == 0:
            return None
    if side > 0 and not P.get("longs", True) or side < 0 and not P.get("shorts", True):
        return None
    if not P.get("dirLock", True):
        side = side or 1
    bars = [b for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    if not bars:
        return None
    vw = vwap_series(bars)
    import daytrade
    cvd = daytrade._cvd_est(bars)
    lvl = pick["h"] if side > 0 else pick["l"]
    risk = float(P["stopAtrPct"]) / 100 * pick["atr"]
    if risk <= 0:
        return None
    tr = None
    for i, b in enumerate(bars):
        hm = b["t"][11:16]
        if tr is None:
            if hm < end_or:
                continue
            if hm >= P["entryEnd"] or hm >= P["flatT"]:
                return None
            if P.get("skipMid") and "12:00" <= hm < "15:00":
                continue
            hit = b["h"] >= lvl if side > 0 else b["l"] <= lvl
            if not hit:
                continue
            px = max(b["o"], lvl) if side > 0 else min(b["o"], lvl)
            if P.get("vwapSide") and ((px < vw[i - 1] if i else False) if side > 0 else (px > vw[i - 1] if i else False)):
                return None                             # the breakout is on the wrong side of the VWAP
            if P.get("cvdSide") and i and (cvd[i - 1] * side <= 0):
                return None                             # the cumulative volume delta is against the breakout
            tr = {"sym": pick["sym"], "at": b["t"], "side": side, "dir": "long" if side > 0 else "short", "entry": round(px, 4),
                  "stop0": round(px - side * risk, 4), "rvol": pick["rvol"], "pmRvol": pick["pmRvol"], "atr": pick["atr"]}
            stop = tr["stop0"]
            if (b["l"] <= stop) if side > 0 else (b["h"] >= stop):  # stopped in the entry candle
                tr.update(exit=stop, exitAt=b["t"], status="stop")
                break
            continue
        stop = tr["stop0"]
        if (b["l"] <= stop) if side > 0 else (b["h"] >= stop):
            tr.update(exit=round(min(b["o"], stop) if side > 0 else max(b["o"], stop), 4), exitAt=b["t"], status="stop")
            break
        if P.get("vwapExit") and ((b["c"] < vw[i]) if side > 0 else (b["c"] > vw[i])):
            tr.update(exit=b["c"], exitAt=b["t"], status="vwap")
            break
        if hm >= P["flatT"] or i == len(bars) - 1:
            tr.update(exit=b["c"], exitAt=b["t"], status="close")
            break
    if tr is None:
        return None
    if "exit" not in tr:
        tr.update(exit=bars[-1]["c"], exitAt=bars[-1]["t"], status="close")
    tr["R"] = round((tr["exit"] - tr["entry"]) * side / risk, 3)
    tr["pct"] = round((tr["exit"] / tr["entry"] - 1) * 100 * side, 3)
    return tr


def day_trades(picks, bars_by_sym, P):
    """Every pick of one day, in time order, with the VWAP stop-out cap and the sizing (fraction of capital)."""
    trs = [t for t in (simulate(p, bars_by_sym.get(p["sym"]) or [], P) for p in picks) if t]
    trs.sort(key=lambda t: t["at"])
    cap = int(P.get("vwapCap") or 0)
    out, outs = [], []
    for t in trs:
        if cap and sum(1 for x in outs if x["exitAt"] < t["at"]) >= cap:
            continue
        out.append(t)
        if t["status"] == "vwap" and t["R"] <= 0:
            outs.append(t)
    n = max(1, int(P["topN"]))
    for t in out:                                       # 1% at risk, at most maxLev x capital over the topN slots
        risk_frac = float(P["riskPct"]) / 100
        pos_frac = risk_frac * t["entry"] / max(1e-9, abs(t["entry"] - t["stop0"]))
        f = min(1.0, (float(P["maxLev"]) / n) / pos_frac) if pos_frac > 0 else 1.0
        t["size"] = round(f, 4)
        t["ret"] = round(risk_frac * t["R"] * f * 100, 4)   # % of the capital
    return out


def bucket(rv):
    return next(lbl for lo, hi, lbl in RV_BUCKETS if lo <= rv < hi)


def backtest(sub, days=250, cfg=None, budget_s=520):
    import daytrade
    t0 = time.time()
    P = clean_cfg(config(sub), cfg or {})
    days = max(10, min(2000, int(days or 250)))
    c = data_creds(sub)
    if not c:
        raise BadRequest("Connect an Alpaca account to load the stock data.")
    rdays = stored_days("R")
    if len(rdays) < 20:
        raise BadRequest("Build the stock history first (Day trading stocks → Build history).")
    run = rdays[-days:]
    keep = rdays[max(0, len(rdays) - days - 14):]
    rows, errors = [], []
    picks_by_day = {}
    base = 1 + ORBS.index(int(P["orbMin"])) * 5
    roll = {}                                           # sym -> the last 14 (opening-range volume, pre-market volume)
    runset = set(run)
    def load(d):
        return d, get_day("R", d), (get_day("U", d) if d in runset else None)

    def days_in_order():
        with ThreadPoolExecutor(max_workers=6) as ex:   # a few days read ahead, never the whole history in memory
            for i in range(0, len(keep), 12):
                for x in ex.map(load, keep[i:i + 12]):
                    yield x
    for d, today, uni in days_in_order():
        if d in runset:
            picks_by_day[d] = _rank(uni, today, roll, P)
        for s, w in today.items():
            h = roll.setdefault(s, [])
            h.append((w[base + 4], w[0]))
            if len(h) > 14:
                del h[0]
        if time.time() - t0 > budget_s * 0.4:
            errors.append("time limit while reading the history")
            break
    run = [d for d in run if d in picks_by_day]

    def one(d):
        pk = picks_by_day[d]
        if not pk:
            return []
        day = datetime.strptime(d, "%Y-%m-%d")
        bb = multi_bars(c, [p["sym"] for p in pk], "1Min", day.replace(hour=9, minute=30), day.replace(hour=16), max_pages=5)
        return day_trades(pk, bb, P)
    done_days = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {d: ex.submit(one, d) for d in run}
        for d in run:
            if time.time() - t0 > budget_s:
                errors.append(f"time limit: {len(done_days)} of {len(run)} days")
                break
            try:
                rows += futs[d].result(timeout=max(5, budget_s - (time.time() - t0)))
                done_days.append(d)
            except Exception as e:
                errors.append(f"{d}: {str(e)[:100]}")
    rows.sort(key=lambda t: t["at"])
    ds = daytrade.deep_stats(rows, done_days)
    by_rv = {}
    for lo, hi, lbl in RV_BUCKETS:
        rs = [t for t in rows if lo <= t["rvol"] < hi]
        if rs:
            by_rv[lbl] = daytrade.stats(rs)
    by_t = {}
    for t in rows:
        by_t.setdefault(t["sym"], []).append(t)
    tick = sorted(({"sym": s, **daytrade.stats(v)} for s, v in by_t.items() if len(v) >= 3), key=lambda x: -(x.get("totR") or 0))
    eq, peak, dd, curve = 1.0, 1.0, 0.0, []
    byday = {}
    for t in rows:
        byday[t["at"][:10]] = byday.get(t["at"][:10], 0) + t["ret"]
    for d in done_days:
        eq *= 1 + byday.get(d, 0) / 100
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
        curve.append(round((eq - 1) * 100, 2))
    yrs = len(done_days) / 252 if done_days else 0
    import dtsearch
    out = {"at": iso(now_ny()), "days": days, "daysFound": len(done_days), "first": done_days[0] if done_days else None,
           "all": ds.pop("all"), **ds, "t": dtsearch.tstat([t["R"] for t in rows]), "byRvol": by_rv,
           "best": tick[:12], "worst": tick[-12:][::-1], "retPct": round((eq - 1) * 100, 1),
           "annPct": round((eq ** (1 / yrs) - 1) * 100, 1) if yrs > 0.2 and eq > 0 else None, "ddPct": round(dd * 100, 1),
           "curvePct": curve[::max(1, len(curve) // 300)], "picksPerDay": round(sum(len(v) for v in picks_by_day.values()) / max(1, len(run)), 1),
           "trades": [{k: t.get(k) for k in ("sym", "at", "dir", "entry", "stop0", "exit", "exitAt", "status", "R", "rvol", "ret")} for t in rows][-300:],
           "secs": round(time.time() - t0), "errors": errors}
    return out


def backtest_job(sub, params=None):
    p = params or {}
    try:
        out = backtest(sub, p.get("days") or 250, p.get("cfg"))
    except Exception as e:
        out = {"at": iso(now_ny()), "errors": [str(e)[:200]]}
    from decimal import Decimal
    db.put({"PK": db.upk(sub), "SK": BT_SK, **json.loads(json.dumps(out, default=str), parse_float=Decimal), "running": None})
    return out


def backtest_result(sub):
    r = db.get(db.upk(sub), BT_SK) or {}
    if r.get("running") and r["running"] < iso(now_ny() - timedelta(minutes=12)):
        r = {**r, "running": None, "errors": ["The last run hit the 10-minute limit: try fewer days."]}
    return {k: v for k, v in r.items() if k not in ("PK", "SK")}


# ---------------- live (third Alpaca paper account) ----------------

def _alp(c, method, path, body=None):
    import autotrader
    return autotrader._alp(c, method, path, body)


def view(sub):
    cfg = config(sub)
    acct = db.get(db.upk(sub), CRED_SK)
    info = {"connected": bool(acct), **({k: acct.get(k) for k in ("env", "keyHint")} if acct else {})}
    if acct:
        try:
            c = creds(sub)
            a = _alp(c, "GET", "/v2/account") or {}
            info.update(equity=a.get("equity"), buyingPower=a.get("buying_power"))
            info["positions"] = [{"symbol": p.get("symbol"), "qty": p.get("qty"), "side": p.get("side"), "entry": p.get("avg_entry_price"),
                                  "pl": p.get("unrealized_pl")} for p in (_alp(c, "GET", "/v2/positions") or [])]
        except Exception as e:
            info["error"] = str(e)[:160]
    today = now_ny().strftime("%Y-%m-%d")
    scan = db.get(db.upk(sub), f"SDSCAN#{today}") or db.get(db.upk(sub), "SDSCAN#last") or {}
    recs = sorted(db.q_prefix(db.upk(sub), "SDPOS#"), key=lambda r: r.get("at", ""))[-80:]
    return {"config": cfg, "account": info, "prep": prep_status(sub), "coverage": coverage(),
            "scan": {k: v for k, v in scan.items() if k not in ("PK", "SK")},
            "trades": [{k: v for k, v in r.items() if k != "PK"} for r in recs]}


def _windows(c, syms, day, feed="sip", end=None):
    """{sym: window_stats} of one day for many symbols (pre-market 30-min + opening 5-min candles)."""
    out = {}
    e5 = min(day.replace(hour=10, minute=30), end) if end else day.replace(hour=10, minute=30)

    def chunk(cs):
        pm = multi_bars(c, cs, "30Min", day.replace(hour=4), day.replace(hour=9, minute=30), feed=feed)
        b5 = multi_bars(c, cs, "5Min", day.replace(hour=9, minute=30), e5, feed=feed)
        return {s: w for s in cs for w in [window_stats(pm.get(s) or [], b5.get(s) or [])] if w}
    with ThreadPoolExecutor(max_workers=3) as ex:
        for res in ex.map(chunk, [syms[i:i + 200] for i in range(0, len(syms), 200)]):
            out.update(res)
    return out


def iex_history(c, syms, days):
    """For an account without real-time SIP data: the same windows on the IEX feed over the previous days (so that
    today's IEX volume is compared with IEX volume)."""
    return [_windows(c, syms, datetime.strptime(d, "%Y-%m-%d"), feed="iex") for d in days]


def scan_today(sub, P, now):
    """Today's stocks in play once the opening range is complete: universe of today (stored by the morning prep),
    today's pre-market + opening-range windows, RVOL vs the stored 14 previous days."""
    c = data_creds(sub)
    d = now.strftime("%Y-%m-%d")
    uni = get_day("U", d)
    if not uni:
        return {"error": "no universe for today (the morning prep runs at 09:05)"}
    rdays = [x for x in stored_days("R") if x < d][-14:]
    hist = [get_day("R", x) for x in rdays]
    syms = sorted(uni)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    feed = "sip"
    try:
        today = _windows(c, syms, day, "sip", now.replace(tzinfo=None))
    except BadRequest:                                   # no real-time SIP on this data plan: IEX vs IEX history
        feed = "iex"
        today = _windows(c, syms, day, "iex", now.replace(tzinfo=None))
        ih = db.get(db.upk(sub), f"SDIEX#{d}")
        if ih and ih.get("h"):
            hist = [{s: _dec(v) for s, v in h.items()} for h in json.loads(ih["h"])]
        else:
            hist = iex_history(c, sorted(today), rdays[-10:])
    picks = rank_day(d, uni, hist + [today], P)
    return {"date": d, "at": now.strftime("%H:%M"), "picks": picks, "candidates": len(uni), "feed": feed}


def _qty(P, equity, pick, n):
    risk = float(P["stopAtrPct"]) / 100 * pick["atr"]
    lvl = pick["h"] if pick["dir"] > 0 else pick["l"]
    if risk <= 0 or lvl <= 0:
        return 0
    q = int(equity * float(P["riskPct"]) / 100 / risk)
    q_lev = int(equity * float(P["maxLev"]) / max(1, n) / lvl)
    return max(0, min(q, q_lev))


def tick(sub, now=None):
    """Every minute 09:30-16:00: after the opening range, scan and place the stop entries (OTO with the stop loss);
    then the VWAP exit, the entry deadline and the flat time."""
    from decimal import Decimal
    P = config(sub)
    if not P.get("enabled"):
        return {"skipped": "off"}
    c = creds(sub)
    if not c:
        return {"skipped": "no stock account"}
    now = now or now_ny()
    d, hm = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    if now.weekday() >= 5 or not ("09:30" <= hm < "16:00"):
        return {"skipped": "market closed"}
    pk = db.upk(sub)
    acts = []
    m = int(P["orbMin"])
    ready = f"{9 + (31 + m) // 60:02d}:{(31 + m) % 60:02d}"
    scan = db.get(pk, f"SDSCAN#{d}")
    busy = scan and scan.get("scanning") and scan["scanning"] > iso(now_ny() - timedelta(minutes=5))
    if (not scan or scan.get("error") or scan.get("scanning")) and not busy and hm >= ready and hm < P["entryEnd"]:
        db.put({"PK": pk, "SK": f"SDSCAN#{d}", "scanning": iso(now_ny())})     # the next minute's run waits
        try:
            sc = scan_today(sub, P, now)
        except Exception as e:
            sc = {"error": str(e)[:160]}
        scan = {"PK": pk, "SK": f"SDSCAN#{d}", **json.loads(json.dumps(sc), parse_float=Decimal)}
        db.put(scan)
        db.put({**scan, "SK": "SDSCAN#last"})
        if P.get("trade") and sc.get("picks"):
            eq = float((_alp(c, "GET", "/v2/account") or {}).get("equity") or 0)
            for p in sc["picks"]:
                if p["dir"] == 0 or (p["dir"] > 0 and not P["longs"]) or (p["dir"] < 0 and not P["shorts"]):
                    continue
                q = _qty(P, eq, p, len(sc["picks"]))
                if q < 1:
                    continue
                lvl = round(p["h"] if p["dir"] > 0 else p["l"], 2)
                stop = round(lvl - p["dir"] * float(P["stopAtrPct"]) / 100 * p["atr"], 2)
                try:
                    o = _alp(c, "POST", "/v2/orders", {"symbol": p["sym"], "qty": str(q), "side": "buy" if p["dir"] > 0 else "sell",
                                                       "type": "stop", "stop_price": str(lvl), "time_in_force": "day",
                                                       "order_class": "oto", "stop_loss": {"stop_price": str(stop)}}) or {}
                    db.put({"PK": pk, "SK": f"SDPOS#{d}#{p['sym']}", "symbol": p["sym"], "at": f"{d}T{hm}", "dir": "long" if p["dir"] > 0 else "short",
                            "qty": q, "level": Decimal(str(lvl)), "stop": Decimal(str(stop)), "rvol": Decimal(str(p["rvol"])),
                            "orderId": o.get("id"), "status": "ordered"})
                    acts.append(f"{p['sym']} {'buy' if p['dir'] > 0 else 'sell'} stop {q} @ {lvl}")
                except Exception as e:
                    acts.append(f"{p['sym']}: {str(e)[:100]}")
    recs = [r for r in db.q_prefix(pk, f"SDPOS#{d}#")]
    if not recs:
        return {"actions": acts}
    try:
        pos = {p["symbol"]: p for p in (_alp(c, "GET", "/v2/positions") or [])}
    except Exception:
        pos = {}
    if hm >= P["entryEnd"]:                             # entries not triggered by the deadline: cancelled
        for r in recs:
            if r.get("status") == "ordered" and r["symbol"] not in pos and r.get("orderId"):
                try:
                    _alp(c, "DELETE", f"/v2/orders/{r['orderId']}")
                except Exception:
                    pass
                db.update(pk, r["SK"], {"status": "expired"})
    if P.get("vwapExit") and pos:
        held = [r["symbol"] for r in recs if r["symbol"] in pos]
        try:
            day = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
            bb = multi_bars(c, held, "1Min", day.replace(hour=9, minute=30), now.replace(tzinfo=None) - timedelta(minutes=1), max_pages=5)
            for s in held:
                bs = bb.get(s) or []
                if not bs:
                    continue
                vw = vwap_series(bs)[-1]
                long_ = pos[s].get("side") == "long"
                if (bs[-1]["c"] < vw) if long_ else (bs[-1]["c"] > vw):
                    _alp(c, "DELETE", f"/v2/positions/{s}?cancel_orders=true")
                    db.update(pk, f"SDPOS#{d}#{s}", {"status": "closed", "exitReason": f"VWAP close {bs[-1]['t'][11:16]}"})
                    acts.append(f"{s} closed: 1-min close across the VWAP")
        except Exception as e:
            acts.append(f"vwap exit: {str(e)[:100]}")
    if hm >= P["flatT"]:
        try:
            _alp(c, "DELETE", "/v2/positions?cancel_orders=true")
        except Exception:
            pass
        for r in recs:
            if r.get("status") in ("ordered", "open"):
                db.update(pk, r["SK"], {"status": "closed", "exitReason": f"flat at {P['flatT']}"})
        acts.append("flat")
    else:
        for r in recs:
            if r.get("status") == "ordered" and r["symbol"] in pos:
                db.update(pk, r["SK"], {"status": "open"})
    return {"actions": acts}


def continue_prep(sub, days):
    import os
    import boto3
    fn = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if fn:
        boto3.client("lambda").invoke(FunctionName=fn, InvocationType="Event",
                                      Payload=json.dumps({"sub": sub, "job": "sdprep", "params": {"days": days}}))


def prep_job(sub, params=None):
    days = max(30, min(1500, int((params or {}).get("days") or 500)))
    return prep(sub, days, budget_s=470, invoke_next=lambda: continue_prep(sub, days))


def morning(sub):
    """09:05 ET: today's universe (daily candles up to yesterday) and yesterday's opening ranges."""
    days = int((prep_status(sub) or {}).get("days") or 500)
    return prep(sub, days=days, budget_s=470, invoke_next=lambda: continue_prep(sub, days))


# ---------------- chart of one stock-day ----------------

def chart(sub, sym, date=None):
    P = config(sub)
    c = data_creds(sub)
    if not c:
        raise BadRequest("Connect an Alpaca account to load the stock data.")
    sym = (sym or "").upper().strip()
    d = date or now_ny().strftime("%Y-%m-%d")
    day = datetime.strptime(d, "%Y-%m-%d")
    bb = multi_bars(c, [sym], "1Min", day.replace(hour=4), min(day.replace(hour=16), now_ny().replace(tzinfo=None) - timedelta(minutes=16)), max_pages=5).get(sym) or []
    reg = [b for b in bb if "09:30" <= b["t"][11:16] < "16:00"]
    vw = vwap_series(reg)
    m = int(P["orbMin"])
    end_or = f"{9 + (30 + m) // 60:02d}:{(30 + m) % 60:02d}"
    w = [b for b in reg if b["t"][11:16] < end_or]
    orb = {"h": max(b["h"] for b in w), "l": min(b["l"] for b in w), "o": w[0]["o"], "c": w[-1]["c"]} if w else None
    pre = [b for b in bb if b["t"][11:16] < "09:30"]
    sig = []
    if orb and len(reg) > m:
        pick = {"sym": sym, "rvol": None, "pmRvol": None, "atr": float((get_day("U", d).get(sym) or [0])[0] or 0), **orb,
                "dir": 0 if (orb["h"] - orb["l"]) <= 0 or abs(orb["c"] - orb["o"]) < float(P["dojiPct"]) / 100 * (orb["h"] - orb["l"]) else (1 if orb["c"] > orb["o"] else -1)}
        if pick["atr"] > 0:
            t = simulate(pick, reg, P)
            if t:
                t.update(kind="day", status=t["status"], level=t["stop0"], levelName="stop", stop=t["stop0"], target=None,
                         last=t["exit"], lastAt=t["exitAt"], o=0, h=0, l=0)
                sig = [t]
    return {"symbol": sym, "date": d, "bars": reg, "interval": 1, "days": 1, "dt": True,
            "vwap": [{"t": b["t"], "v": round(v, 4)} for b, v in zip(reg, vw)], "orb": orb,
            "orbLv": {d: {"h": orb["h"], "l": orb["l"]}} if orb else {},
            "pm": {d: {"h": max(b["h"] for b in pre), "l": min(b["l"] for b in pre)}} if pre else {},
            "pmVol": int(sum(b["v"] or 0 for b in pre)), "signals": sig, "stats": {}, "walls": [], "alerts": [], "evals": [],
            "maxPain": [], "expirations": [], "trades": [], "ema": [], "ema2": [], "kch": [], "inertia": [], "sqzH": []}
