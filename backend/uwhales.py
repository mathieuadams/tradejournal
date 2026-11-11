"""Unusual Whales API (https://api.unusualwhales.com), with the user's own API token (KMS-encrypted).

Gamma for a stock / ETF (QQQ, SPY):
  GET /api/stock/{ticker}/gex-levels?source=oi|vol      call_wall, put_wall, gamma_magnet, gamma_flip, nearby_flips
  GET /api/stock/{ticker}/spot-exposures/strike         per strike: call_gamma_oi / put_gamma_oi (and _vol, _ask, _bid),
                                                         price, time; $ gamma per 1% move of the underlying
The app also works the walls out itself from the per-strike rows (same definitions), so the numbers can later be
compared with a Schwab option chain computed the same way."""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

import db
from util import BadRequest, Unavailable, iso, now_ny

SK = "DATASOURCE#uw"
BASE = "https://api.unusualwhales.com/api/"
SOURCES = ("oi", "vol")


def _kms():
    import boto3
    return boto3.client("kms")


def _err(e):
    try:
        b = json.loads(e.read() or b"{}")
        msg = b.get("message") or b.get("msg") or b.get("error") or ""
    except Exception:
        msg = ""
    if e.code in (401, 403):
        return BadRequest(f"Unusual Whales refused the token ({e.code}){': ' + msg if msg else ''}. API access is set up at unusualwhales.com/dashboard/api.")
    if e.code == 422:
        return BadRequest(f"Unusual Whales: {msg or 'bad request'} (422).")
    if e.code in (429, 503):
        return Unavailable("Unusual Whales rate limit reached. Try again in a minute.")
    return Unavailable(f"Unusual Whales error {e.code}{': ' + msg if msg else ''}.")


def _get(token, path, params=None, timeout=60):
    url = BASE + path + ("?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")}) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                                               "User-Agent": "tradejournal-futures/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise _err(e)
    except urllib.error.URLError as e:
        raise Unavailable(f"Unusual Whales couldn't be reached: {e.reason}")


# ---------------------------------------------------------------- token storage

def save_key(sub, token):
    token = (token or "").strip()
    if not (20 <= len(token) <= 200) or any(ch.isspace() for ch in token):
        raise BadRequest("Paste the Unusual Whales API token (unusualwhales.com/dashboard/api).")
    _get(token, "stock/SPY/gex-levels")               # a refused token stops here
    blob = _kms().encrypt(KeyId=os.environ.get("KMS_KEY_ID", ""), Plaintext=token.encode())["CiphertextBlob"]
    db.put({"PK": db.upk(sub), "SK": SK, "enc": base64.b64encode(blob).decode(), "last4": token[-4:],
            "savedAt": iso(now_ny()), "testedAt": iso(now_ny())})
    return status(sub)


def token(sub):
    it = db.get(db.upk(sub), SK)
    if not it or not it.get("enc"):
        raise BadRequest("Connect Unusual Whales first (Data → Unusual Whales).")
    return _kms().decrypt(CiphertextBlob=base64.b64decode(it["enc"]))["Plaintext"].decode()


def status(sub):
    it = db.get(db.upk(sub), SK) or {}
    return {"connected": bool(it.get("enc")), "last4": it.get("last4"), "savedAt": it.get("savedAt"), "testedAt": it.get("testedAt")}


def delete_key(sub):
    db.delete(db.upk(sub), SK)
    return status(sub)


def test(sub):
    it = db.get(db.upk(sub), SK) or {}
    _get(token(sub), "stock/SPY/gex-levels")
    it["testedAt"] = iso(now_ny())
    db.put(it)
    return status(sub)


# ---------------------------------------------------------------- gamma

def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def check_sym(sym):
    s = (sym or "").strip().upper()
    if not s or len(s) > 10 or not all(ch.isalnum() or ch in ".-" for ch in s):
        raise BadRequest("Enter a stock or ETF symbol, e.g. QQQ.")
    return s


def walls(rows, price):
    """The levels from per-strike gamma, with Unusual Whales' definitions:
      call wall    = strike above the price with the largest positive net gamma (resistance)
      put wall     = strike below the price with the largest positive net gamma (support)
      gamma magnet = strike with the largest net gamma by size (the strongest pin)
      gamma flips  = where net gamma changes sign between strikes, nearest the price first (linear in between)
    rows: [{"strike", "net"}] sorted by strike."""
    above = [r for r in rows if r["strike"] > price and r["net"] > 0]
    below = [r for r in rows if r["strike"] < price and r["net"] > 0]
    out = {"call_wall": max(above, key=lambda r: r["net"])["strike"] if above else None,
           "put_wall": max(below, key=lambda r: r["net"])["strike"] if below else None,
           "gamma_magnet": max(rows, key=lambda r: abs(r["net"]))["strike"] if rows else None}
    flips = []
    for a, b in zip(rows, rows[1:]):
        if a["net"] == 0 or (a["net"] > 0) != (b["net"] > 0):
            x = a["strike"] if a["net"] == b["net"] else a["strike"] + (b["strike"] - a["strike"]) * (-a["net"]) / (b["net"] - a["net"])
            flips.append(round(x, 2))
    flips.sort(key=lambda x: abs(x - price))
    out["nearby_flips"] = flips[:5]
    out["gamma_flip"] = flips[0] if flips else None
    return out


def gamma(sub, sym, source="oi", date=None, band=0.25):
    """Today's (or `date`'s) gamma for `sym`: Unusual Whales' own levels, the per-strike profile within +/- band of the
    price, and the same levels worked out here from that profile."""
    sym, source = check_sym(sym), source if source in SOURCES else "oi"
    tok = token(sub)
    lv = _get(tok, f"stock/{sym}/gex-levels", {"source": source, "date": date})
    lv = lv.get("data", lv) if isinstance(lv, dict) else {}
    first = _get(tok, f"stock/{sym}/spot-exposures/strike", {"date": date, "limit": 1})
    price = _num(((first.get("data") or [{}])[0]).get("price"))
    if not price:
        raise Unavailable(f"Unusual Whales has no gamma data for {sym}{' on ' + date if date else ''}.")
    rows, page = [], 0
    while page < 10:
        r = _get(tok, f"stock/{sym}/spot-exposures/strike", {"date": date, "min_strike": round(price * (1 - band), 2),
                                                               "max_strike": round(price * (1 + band), 2), "limit": 500, "page": page})
        data = r.get("data") or []
        rows += data
        if len(data) < 500:
            break
        page += 1
    prof = []
    for x in rows:
        k = _num(x.get("strike"))
        c, p = _num(x.get(f"call_gamma_{source}")) or 0.0, _num(x.get(f"put_gamma_{source}")) or 0.0
        if k is not None:
            prof.append({"strike": k, "call": c, "put": p, "net": c + p})
    prof.sort(key=lambda r: r["strike"])
    try:                                                # open interest per strike (all expiries), on the same rows
        oi = {}
        for x in (_get(tok, f"stock/{sym}/oi-per-strike", {"date": date}).get("data") or []):
            k = _num(x.get("strike"))
            if k is not None:
                oi[k] = (int(_num(x.get("call_oi")) or 0), int(_num(x.get("put_oi")) or 0))
        for r in prof:
            r["callOi"], r["putOi"] = oi.get(r["strike"], (0, 0))
    except Exception as e:
        print("oi-per-strike failed", e)
    uw = {k: _num(lv.get(k)) for k in ("call_wall", "put_wall", "gamma_magnet", "gamma_flip")}
    uw["nearby_flips"] = [v for v in (_num(f) for f in lv.get("nearby_flips") or []) if v is not None]
    t = ((rows or [{}])[0]).get("time") or lv.get("time")
    return {"sym": sym, "source": source, "price": price, "time": t, "date": lv.get("date"),
            "uw": uw, "computed": walls(prof, price), "strikes": prof, "netTotal": sum(r["net"] for r in prof)}


# ---------------------------------------------------------------- market maker landscape (TradeWellX "Calculate MMM")

def _occ(sym):
    """QQQ261109C00651000 -> ("2026-11-09", "C", 651.0)."""
    import re
    m = re.match(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$", sym or "")
    if not m:
        return None
    d = m.group(2)
    return f"20{d[:2]}-{d[2:4]}-{d[4:6]}", m.group(3), int(m.group(4)) / 1000


def expiries(sub, sym):
    tok = token(sub)
    r = _get(tok, f"stock/{check_sym(sym)}/expiry-breakdown")
    today = now_ny().strftime("%Y-%m-%d")
    out = [{"expiry": str(x.get("expires", ""))[:10], "oi": int(x.get("open_interest") or 0), "volume": int(x.get("volume") or 0)}
           for x in r.get("data") or []]
    return sorted([e for e in out if e["expiry"] >= today and e["oi"] > 0], key=lambda e: e["expiry"])


def landscape(sub, sym, expiry=None, step=0.001, span=0.03):
    """What the option holders of one expiry make or lose at expiry for each underlying price, if market makers are on
    the other side of all the open interest (TradeWellX's Market Maker Landscape):
        holders(P) = sum over contracts of open interest x 100 x (value at expiry at P - premium paid)
    premium = the NBBO mid (ask when there is no bid). Its lowest point is the Peak Money Line (PML: the price where the
    market makers make the most); where it crosses zero below / above that are the Floor / Ceiling (wash-out).
    Targets: the price +/- 0.1% steps; for each, the call (above) or put (below) with the best % gain if bought at the ask
    now and sold at its expiry value at the target."""
    sym = check_sym(sym)
    tok = token(sub)
    exps = expiries(sub, sym)
    if not exps:
        raise Unavailable(f"Unusual Whales lists no open {sym} expirations.")
    ex = expiry if expiry and any(e["expiry"] == expiry for e in exps) else exps[0]["expiry"]
    first = _get(tok, f"stock/{sym}/spot-exposures/strike", {"limit": 1})
    price = _num(((first.get("data") or [{}])[0]).get("price"))
    rows, page = [], 0
    while page < 20:
        r = _get(tok, f"stock/{sym}/option-contracts", {"expiry": ex, "exclude_zero_oi_chains": "true", "limit": 500, "page": page})
        data = r.get("data") or []
        rows += data
        if len(data) < 500:
            break
        page += 1
    cons = []
    for x in rows:
        o = _occ(x.get("option_symbol"))
        if not o or o[0] != ex:
            continue
        bid, ask = _num(x.get("nbbo_bid")) or 0.0, _num(x.get("nbbo_ask")) or 0.0
        prem = (bid + ask) / 2 if bid > 0 and ask > 0 else (ask or _num(x.get("last_price")) or 0.0)
        cons.append({"sym": x.get("option_symbol"), "type": o[1], "strike": o[2], "oi": int(x.get("open_interest") or 0),
                     "vol": int(x.get("volume") or 0), "bid": bid, "ask": ask, "prem": prem})
    if not cons:
        raise Unavailable(f"No {sym} contracts with open interest for {ex}.")
    if not price:
        raise Unavailable(f"Unusual Whales has no {sym} price right now.")
    val = lambda c, p: max(0.0, p - c["strike"]) if c["type"] == "C" else max(0.0, c["strike"] - p)
    holders = lambda p: sum(weight(c) * 100 * (val(c, p) - c["prem"]) for c in cons)
    ks = sorted({c["strike"] for c in cons})
    lo, hi = min(ks[0], price * 0.85), max(ks[-1], price * 1.15)
    grid = [round(lo + (hi - lo) * i / 600, 2) for i in range(601)] + ks
    grid = sorted(set(round(g, 2) for g in grid))
    curve = [{"price": g, "holders": holders(g)} for g in grid]
    pml = min(curve, key=lambda z: z["holders"])
    def cross(seq):                          # first zero crossing walking away from the PML
        for a, b in zip(seq, seq[1:]):
            if (a["holders"] < 0) != (b["holders"] < 0):
                return a["price"] + (b["price"] - a["price"]) * (-a["holders"]) / (b["holders"] - a["holders"])
        return None
    i = curve.index(pml)
    ceiling, floor = cross(curve[i:]), cross(curve[:i + 1][::-1])
    targets = []
    n = int(round(span / step))
    for j in range(n, -n - 1, -1):
        t = round(price * (1 + j * step), 2)
        side = "C" if j > 0 else "P" if j < 0 else None
        best = None
        if side:
            for c in cons:
                if c["type"] != side or c["ask"] < 0.05 or c["oi"] < 10:
                    continue
                stc = val(c, t)
                g = (stc - c["ask"]) / c["ask"]
                if best is None or g > best["gain"]:
                    best = {"contract": c["sym"], "strike": c["strike"], "bto": c["ask"], "stc": stc, "gain": g}
        targets.append({"target": t, "move": j * step, "holders": holders(t), "best": best})
    above = lambda typ, ref: sum(c["vol"] for c in cons if c["type"] == typ and c["strike"] >= ref)
    below = lambda typ, ref: sum(c["vol"] for c in cons if c["type"] == typ and c["strike"] < ref)
    return {"sym": sym, "expiry": ex, "expiries": exps[:12], "price": price, "contracts": len(cons),
            "oi": {"calls": sum(c["oi"] for c in cons if c["type"] == "C"), "puts": sum(c["oi"] for c in cons if c["type"] == "P")},
            "pml": pml["price"], "pmlValue": pml["holders"], "ceiling": ceiling, "floor": floor,
            "aggregate": holders(price),
            "volumes": {"pml": {"aboveCalls": above("C", pml["price"]), "abovePuts": above("P", pml["price"]),
                                "belowCalls": below("C", pml["price"]), "belowPuts": below("P", pml["price"])},
                        "price": {"aboveCalls": above("C", price), "abovePuts": above("P", price),
                                  "belowCalls": below("C", price), "belowPuts": below("P", price)}},
            "curve": [z for z in curve if abs(z["price"] / price - 1) <= 0.06], "targets": targets}


# ---------------------------------------------------------------- intraday market maker data (TradeWellX "MM Data")

SNAP_SYMS = ("QQQ", "SPY")


def stock_session(t=None):
    """US stock regular session, plus 5 minutes for the close (weekdays 09:30-16:05 ET)."""
    t = t or now_ny()
    return t.weekday() < 5 and "09:30" <= t.strftime("%H:%M") <= "16:05"


def snapshot(sub, sym):
    """Record the landscape now: price (TradeWellX's GreenLine), PML, Ceiling, Floor and the day's volume of puts / calls
    at or above vs below the price. One row per minute in MM#<sym>#<date>."""
    d = landscape(sub, sym)
    t = now_ny()
    v = d["volumes"]["price"]
    row = {"t": t.strftime("%H:%M"), "price": d["price"], "pml": d["pml"], "ceiling": d["ceiling"], "floor": d["floor"],
           "agg": d["aggregate"], "pmlValue": d["pmlValue"], "expiry": d["expiry"],
           "pa": v["abovePuts"], "pb": v["belowPuts"], "ca": v["aboveCalls"], "cb": v["belowCalls"]}
    try:                                                # the gamma walls at that time (Unusual Whales' own levels)
        g = gamma(sub, sym)
        row["cw"], row["pw"] = g["uw"].get("call_wall"), g["uw"].get("put_wall")
    except Exception as e:
        print("snapshot walls failed", e)
    sk = f"MM#{d['sym']}#{t.strftime('%Y-%m-%d')}"
    it = db.get(db.upk(sub), sk) or {"PK": db.upk(sub), "SK": sk, "rows": []}
    rows = [r for r in it.get("rows") or [] if r.get("t") != row["t"]] + [row]
    it["rows"] = sorted(rows, key=lambda r: r["t"])[-400:]
    db.put(it)
    return row


def history(sub, sym, days=3):
    """The recorded snapshots of the last `days` dates that have any, oldest first: [{"date", "rows"}]."""
    sym = check_sym(sym)
    items = db.q_prefix(db.upk(sub), f"MM#{sym}#", desc=True, limit=days)
    return sorted(({"date": it["SK"].rsplit("#", 1)[1], "rows": it.get("rows") or [], "backfilled": bool(it.get("backfilled"))}
                   for it in items), key=lambda x: x["date"])


def scheduled_snapshots(sub):
    if not status(sub)["connected"] or not stock_session():
        return None
    out = {}
    for s in SNAP_SYMS:
        try:
            out[s] = snapshot(sub, s)["t"]
        except Exception as e:
            out[s] = "error: " + str(e)[:120]
    return out


# ---------------------------------------------------------------- backfill: rebuild past days of MM data

def _get_retry(tok, path, params=None, tries=6):
    import time
    for k in range(tries):
        try:
            return _get(tok, path, params)
        except Unavailable as e:
            if "rate limit" not in str(e) or k == tries - 1:
                raise
            time.sleep(2 + 3 * k)


def weight(c):
    """Contracts held = open interest (yesterday's close) + today's traded volume so far. Open interest only changes
    overnight, so with it alone the PML can't move during the day; the volume makes it follow today's flow (like
    TradeWellX's "OI / Traded Volume" and MoobeBot's intraday PML)."""
    return (c.get("oi") or 0) + (c.get("vol") or 0)


def mm_levels(cons, price, span=0.15, n=600):
    """PML / Ceiling / Floor / aggregate from contracts [{"type", "strike", "oi", "vol", "prem"}] (same model as landscape)."""
    cons = [c for c in cons if c["prem"] is not None and weight(c) > 0]
    if not cons:
        return None
    val = lambda c, p: max(0.0, p - c["strike"]) if c["type"] == "C" else max(0.0, c["strike"] - p)
    holders = lambda p: sum(weight(c) * 100 * (val(c, p) - c["prem"]) for c in cons)
    ks = sorted({c["strike"] for c in cons})
    lo, hi = min(ks[0], price * (1 - span)), max(ks[-1], price * (1 + span))
    grid = sorted({round(lo + (hi - lo) * i / n, 2) for i in range(n + 1)} | set(ks))
    curve = [(g, holders(g)) for g in grid]
    i = min(range(len(curve)), key=lambda j: curve[j][1])

    def cross(seq):
        for (pa, a), (pb, b) in zip(seq, seq[1:]):
            if (a < 0) != (b < 0):
                return pa + (pb - pa) * (-a) / (b - a)
        return None
    return {"pml": curve[i][0], "pmlValue": curve[i][1], "ceiling": cross(curve[i:]), "floor": cross(curve[:i + 1][::-1]),
            "agg": holders(price)}


def chain_walls(chain, day):
    """Gamma by strike from a dated option chain (all expiries, gamma x open interest x 100): {strike: call gamma},
    {strike: put gamma}. The walls of a past day, when Unusual Whales' own levels for it are not stored."""
    calls, puts = {}, {}
    for x in chain:
        o = _occ(x.get("option_symbol"))
        g, oi = _num(x.get("gamma")), x.get("open_interest") or 0
        if not o or not g or not oi or str(x.get("expires"))[:10] < day:
            continue
        b = calls if o[1] == "C" else puts
        b[o[2]] = b.get(o[2], 0.0) + abs(g) * oi * 100
    return calls, puts


def _side_wall(prof, price, side):
    """Call wall: the strike above the price with the most call gamma; put wall: below, the most put gamma."""
    ks = [k for k in prof if (k > price if side > 0 else k < price)]
    return max(ks, key=lambda k: prof[k]) if ks else None


def backfill_day(sub, sym, day, prog=None, step=5):
    """Rebuild `day`'s MM data every `step` minutes, as the live snapshots record it: the nearest expiry's contracts
    (option-chains for that date, with open interest), each contract's minute prices and volume (option-contract
    intraday) and the stock's bars (Alpaca). A contract's premium at a time = its last trade so far (its first trade of
    the day before it has traded). Stored in MM#<sym>#<day> with backfilled=True."""
    import alpaca
    sym = check_sym(sym)
    tok = token(sub)
    chain, page = [], 0
    while page < 40:
        r = _get_retry(tok, f"stock/{sym}/option-chains", {"date": day, "greeks": "true", "limit": 500, "page": page})
        data = r.get("data") if isinstance(r, dict) else r
        data = data or []
        chain += [x for x in data if isinstance(x, dict)]
        if len(data) < 500:
            break
        page += 1
    exps = sorted({str(x.get("expires"))[:10] for x in chain if str(x.get("expires"))[:10] >= day and (x.get("open_interest") or 0) > 0})
    if not exps:
        raise Unavailable(f"No {sym} contracts with open interest on {day}.")
    ex = exps[0]
    cw, pw = chain_walls(chain, day)
    cons = []
    for x in chain:
        if str(x.get("expires"))[:10] != ex or not (x.get("open_interest") or 0) > 0:
            continue
        o = _occ(x.get("option_symbol"))
        if o:
            cons.append({"sym": x["option_symbol"], "type": o[1], "strike": o[2], "oi": int(x["open_interest"])})
    bars = alpaca.day_bars(sub, sym, day, step)
    if not bars:
        raise Unavailable(f"No {sym} bars from Alpaca for {day}.")
    ticks = {}
    from util import utc_to_ny
    from datetime import datetime
    for j, c in enumerate(cons):
        if prog and j % 25 == 0:
            prog(f"MM backfill {sym} {day}: contract {j + 1}/{len(cons)}")
        r = _get_retry(tok, f"option-contract/{c['sym']}/intraday", {"date": day})
        tl = []
        for m in r.get("data") or []:
            t = utc_to_ny(datetime.strptime(str(m["start_time"])[:19], "%Y-%m-%dT%H:%M:%S")).strftime("%H:%M")
            vol = sum(int(m.get(k) or 0) for k in ("volume_bid_side", "volume_ask_side", "volume_mid_side", "volume_no_side"))
            tl.append((t, _num(m.get("close")), vol))
        tl.sort()
        ticks[c["sym"]] = tl
    rows = []
    for hm, px in bars:
        h, mi = int(hm[:2]), int(hm[3:]) + step
        end = f"{h + mi // 60:02d}:{mi % 60:02d}"                  # the snapshot is at the end of the bar
        snap = []
        pa = pb = ca = cb = 0
        for c in cons:
            tl = ticks.get(c["sym"]) or []
            done = [x for x in tl if x[0] < end]
            prem = done[-1][1] if done else (tl[0][1] if tl else None)
            vol = sum(x[2] for x in done)
            snap.append({**c, "prem": prem, "vol": vol})          # the volume traded so far today
            if c["type"] == "P":
                if c["strike"] >= px:
                    pa += vol
                else:
                    pb += vol
            else:
                if c["strike"] >= px:
                    ca += vol
                else:
                    cb += vol
        lv = mm_levels(snap, px)
        if not lv:
            continue
        rows.append({"t": end, "price": px, "pml": lv["pml"], "ceiling": lv["ceiling"], "floor": lv["floor"], "agg": lv["agg"],
                     "pmlValue": lv["pmlValue"], "expiry": ex, "pa": pa, "pb": pb, "ca": ca, "cb": cb,
                     "cw": _side_wall(cw, px, 1), "pw": _side_wall(pw, px, -1)})
    sk = f"MM#{sym}#{day}"
    db.put({"PK": db.upk(sub), "SK": sk, "rows": rows, "backfilled": True, "contracts": len(cons), "expiry": ex})
    return len(rows)
