"""Call bursts: clusters of calls bought at the ask on one ticker, much bigger than its usual flow, in a short window.
Tracking only: bursts are recorded and followed, never traded.

A 30-minute window (two 15-minute buckets) on a ticker is a burst when
  - call premium bought at the ask >= burstMinPremium and >= burstMinRatio x the ticker's normal 30-minute call premium
  - at least burstMinPrints call prints
  - calls are >= burstMinCallShare of calls + puts bought in the window
Recorded with what may matter: bought into weakness (price flat or down during the window), open / close of the
session, how many contracts, size vs normal. Followed 1, 3 and 5 trading days later (stock close vs price at the burst).

Live: every minute (with flow trading on), new alerts >= $10k on all tickers (one incremental Unusual Whales request).
Replay: the last 30 days on the tickers the bot has evaluated, to measure it now instead of in weeks.
"""
import json
import time
from datetime import datetime, timedelta

import db
from util import iso, now_ny

DEFAULTS = {"burstMinPremium": 250_000, "burstMinRatio": 4.0, "burstMinPrints": 6, "burstMinCallShare": 0.75, "burstCooldownMin": 120}
SLOTS_PER_DAY = 26                                   # 15-minute buckets 09:30-16:00


def cfg_of(sub):
    import autotrader
    c = autotrader.settings(sub)
    return {**DEFAULTS, **{k: c[k] for k in DEFAULTS if k in c}}


def _bucket(at_et):
    """'YYYY-MM-DD HH:MM' (ET) -> 15-minute bucket 'YYYY-MM-DDTHH:MM', or None outside the session."""
    if not at_et or len(at_et) < 16:
        return None
    d, hm = at_et[:10], at_et[11:16]
    if not ("09:30" <= hm < "16:00"):
        return None
    m = int(hm[:2]) * 60 + int(hm[3:5])
    m = 570 + (m - 570) // 15 * 15
    return f"{d}T{m // 60:02d}:{m % 60:02d}"


def _add(b, a):
    """Add one alert to a bucket dict."""
    prem = a.get("premium") or 0
    if a.get("type") == "put":
        b["pp"] = b.get("pp", 0) + prem
    else:
        b["cp"] = b.get("cp", 0) + prem
        b["n"] = b.get("n", 0) + 1
        cs = set(b.get("cs") or [])
        cs.add(a.get("contract"))
        b["cs"] = sorted(c for c in cs if c)[:40]
        if prem > b.get("mx", 0):
            b["mx"] = prem
    u, t = a.get("underlying"), a.get("atEt") or ""
    if u:                                    # first and last stock price seen in the bucket, by time (feeds come newest first)
        if not b.get("t0") or t < b["t0"]:
            b["t0"], b["u0"] = t, u
        if not b.get("t1") or t >= b["t1"]:
            b["t1"], b["u1"] = t, u
    return b


def aggregate(alerts):
    """{ticker: {bucket: {...}}} from flow alerts (bought at the ask)."""
    out = {}
    for a in alerts:
        k = _bucket(a.get("atEt"))
        if not k or not a.get("ticker"):
            continue
        _add(out.setdefault(a["ticker"], {}).setdefault(k, {}), a)
    return out


def _consecutive(k1, k2):
    return k1[:10] == k2[:10] and (datetime.strptime(k2, "%Y-%m-%dT%H:%M") - datetime.strptime(k1, "%Y-%m-%dT%H:%M")).seconds == 900


def windows(buckets, base30):
    """Every 30-minute window (a bucket and the one before it) with its features."""
    keys = sorted(buckets)
    out = []
    for i, k in enumerate(keys):
        pair = [buckets[k]]
        if i and _consecutive(keys[i - 1], k):
            pair.insert(0, buckets[keys[i - 1]])
        cp = sum(b.get("cp", 0) for b in pair)
        if cp <= 0:
            continue
        pp = sum(b.get("pp", 0) for b in pair)
        n = sum(b.get("n", 0) for b in pair)
        cs = set()
        for b in pair:
            cs.update(b.get("cs") or [])
        u0 = next((b["u0"] for b in pair if b.get("u0")), None)
        u1 = next((b["u1"] for b in reversed(pair) if b.get("u1")), None)
        start = (datetime.strptime(k, "%Y-%m-%dT%H:%M") - timedelta(minutes=15 if len(pair) == 2 else 0)).strftime("%H:%M")
        end = (datetime.strptime(k, "%Y-%m-%dT%H:%M") + timedelta(minutes=15)).strftime("%H:%M")
        out.append({"at": k, "date": k[:10], "start": start, "end": end, "cp": round(cp), "pp": round(pp), "n": n,
                    "contracts": len(cs), "maxPrint": round(max(b.get("mx", 0) for b in pair)),
                    "callShare": round(cp / (cp + pp), 3), "ratio": round(cp / base30, 1) if base30 else None,
                    "move": round((u1 / u0 - 1) * 100, 2) if u0 and u1 else None, "price": u1,
                    "tod": "open" if start <= "10:00" else "close" if end >= "15:30" else "midday"})
    return out


def strict(w, c):
    ratio_ok = (w["ratio"] or 0) >= c["burstMinRatio"] if w["ratio"] is not None else w["cp"] >= 2 * c["burstMinPremium"]
    return (w["cp"] >= c["burstMinPremium"] and ratio_ok and w["n"] >= c["burstMinPrints"]
            and w["callShare"] >= c["burstMinCallShare"])


def loose(w):
    """Wider net for the study, so the thresholds themselves can be judged (and changed without re-running)."""
    return w["cp"] >= 100_000 and w["n"] >= 3 and (w["ratio"] or 2) >= 1.5 and w["callShare"] >= 0.6


def _forward(closes, date, price):
    """Stock change 1, 3, 5 trading days after `date`, from `price` at the burst. closes: [(date, close)] sorted."""
    after = [c for d, c in closes if d > date]
    f = lambda n: round((after[n - 1] / price - 1) * 100, 2) if price and len(after) >= n else None
    return {"fwd1": f(1), "fwd3": f(3), "fwd5": f(5)}


def _dedupe(ws, cooldown_min):
    """One burst per ticker per cooldown: keep the first window, skip overlapping ones right after it."""
    out, last = [], None
    for w in sorted(ws, key=lambda x: x["at"]):
        t = datetime.strptime(w["at"], "%Y-%m-%dT%H:%M")
        if last and t - last < timedelta(minutes=cooldown_min):
            continue
        out.append(w)
        last = t
    return out


# ---------------- replay: the last 30 days ----------------

def replay(sub, restart=False, budget_s=420):
    """Run (or continue) the 30-day study. Processes tickers until the time budget is used, saves progress."""
    import flowdata
    from charts import _yahoo
    pk = db.upk(sub)
    st = db.get(pk, "BURSTSTUDY") or {}
    c = cfg_of(sub)
    if restart or st.get("status") not in ("partial", "running"):
        cut = (now_ny() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        uni = sorted({r["symbol"] for r in db.q_prefix(pk, "BOT#") if r.get("symbol") and (r.get("createdAt") or "") >= cut})
        st = {"PK": pk, "SK": "BURSTSTUDY", "status": "running", "universe": uni, "done": 0, "bursts": [], "base": {},
              "startedAt": iso(now_ny()), "errors": 0, "cfg": c}
    base_acc = st.get("base") or {}
    bursts = st.get("bursts") or []
    t0 = time.time()
    uni = st["universe"]
    i = st.get("done", 0)
    norm = db.get(pk, "BURSTBASE") or {"PK": pk, "SK": "BURSTBASE", "tickers": {}}
    while i < len(uni) and time.time() - t0 < budget_s:
        sym = uni[i]
        i += 1
        try:
            res = flowdata.alerts(sub, 10000, "all", 0, 400, True, False, sym, 0, 31, exclude_etfs=False)
            al = [a for a in res.get("alerts") or [] if (a.get("ticker") or "").upper() == sym]
            bk = aggregate(al).get(sym, {})
            days = sorted({k[:10] for k in bk})
            if not days:
                continue
            total_cp = sum(b.get("cp", 0) for b in bk.values())
            base30 = total_cp / max(1, len(days)) / SLOTS_PER_DAY * 2
            norm["tickers"][sym] = {"dailyCall": round(total_cp / max(1, len(days))), "days": len(days), "at": iso(now_ny())}
            daily = _yahoo(sym.replace(".", "-"), "1d", now_ny() - timedelta(days=50), now_ny() + timedelta(hours=1)) or []
            closes = [(b["t"][:10], b["c"]) for b in daily]
            # baseline: every trading day in the window, close to close
            bl = base_acc.setdefault("all", {"n": 0, "s1": 0, "s3": 0, "s5": 0, "n1": 0, "n3": 0, "n5": 0})
            for d, cl in closes:
                if d < days[0]:
                    continue
                f = _forward(closes, d, cl)
                for h in (1, 3, 5):
                    if f[f"fwd{h}"] is not None:
                        bl[f"s{h}"] += f[f"fwd{h}"]
                        bl[f"n{h}"] += 1
            ws = [w for w in windows(bk, base30) if loose(w)]
            for w in _dedupe(ws, c["burstCooldownMin"]):
                bursts.append({"ticker": sym, **w, "strict": strict(w, c), **_forward(closes, w["date"], w["price"])})
        except Exception as e:
            st["errors"] = st.get("errors", 0) + 1
            st["lastError"] = f"{sym}: {str(e)[:160]}"
    bursts.sort(key=lambda b: (-(b["ratio"] or 0)))
    st.update(done=i, bursts=bursts[:800], base=base_acc, updatedAt=iso(now_ny()),
              status="done" if i >= len(uni) else "partial")
    for cap in (800, 500, 300, 150):          # a DynamoDB item holds 400 KB: keep the biggest clusters if it's too large
        st["bursts"] = bursts[:cap]
        try:
            db.put(st)
            break
        except Exception as e:
            if "size" not in str(e).lower():
                raise
    db.put(norm)
    return {"status": st["status"], "done": i, "total": len(uni), "bursts": len(bursts)}


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return (round(sum(xs) / len(xs), 2), len(xs)) if xs else (None, 0)


def _group(rows, key):
    g = {}
    for r in rows:
        g.setdefault(key(r), []).append(r)
    out = []
    for k, v in g.items():
        a1, _ = _avg([r["fwd1"] for r in v])
        a3, _ = _avg([r["fwd3"] for r in v])
        a5, n5 = _avg([r["fwd5"] for r in v])
        out.append({"label": k, "n": len(v), "fwd1": a1, "fwd3": a3, "fwd5": a5, "n5": n5,
                    "win5": round(sum(1 for r in v if (r["fwd5"] or 0) > 0) / n5 * 100) if n5 else None})
    return out


def study_summary(st, c=None):
    if not st:
        return None
    rows = [r for r in (st.get("bursts") or []) if isinstance(r, dict) and r.get("cp") is not None and r.get("n") is not None]
    for r in rows:
        for k in ("ratio", "move", "fwd1", "fwd3", "fwd5", "price", "callShare", "contracts", "tod", "maxPrint"):
            r.setdefault(k, None)
        r["callShare"] = r["callShare"] if r["callShare"] is not None else 1.0
        r["contracts"] = r["contracts"] or 1
        r["tod"] = r["tod"] or "midday"
    if c:                                     # re-sort with the current thresholds: no need to re-run the replay
        for r in rows:
            r["strict"] = strict(r, c)
        rows.sort(key=lambda b: (not b["strict"], -(b["ratio"] or 0)))
    strict_rows = [r for r in rows if r["strict"]]
    bl = (st.get("base") or {}).get("all") or {}
    base = {f"fwd{h}": round(bl[f"s{h}"] / bl[f"n{h}"], 2) if bl.get(f"n{h}") else None for h in (1, 3, 5)}
    order = lambda labels: (lambda g: sorted(g, key=lambda x: labels.index(x["label"]) if x["label"] in labels else 99))
    ratio_b = lambda r: "unknown" if r["ratio"] is None else "2–5× normal" if r["ratio"] < 5 else "5–10× normal" if r["ratio"] < 10 else "10×+ normal"
    prem_b = lambda r: "< $250k" if r["cp"] < 2.5e5 else "$250k–1M" if r["cp"] < 1e6 else "$1–3M" if r["cp"] < 3e6 else "$3M+"
    move_b = lambda r: "unknown" if r["move"] is None else "Into weakness (flat or down)" if r["move"] <= 0 else "Up < 1%" if r["move"] < 1 else "Chasing (up 1%+)"
    con_b = lambda r: "1 contract" if r["contracts"] <= 1 else "2–4 contracts" if r["contracts"] <= 4 else "5+ contracts"
    share_b = lambda r: "60–75% calls" if r["callShare"] < 0.75 else "75–90% calls" if r["callShare"] < 0.9 else "90%+ calls"
    return {
        "status": st.get("status"), "done": st.get("done"), "total": len(st.get("universe") or []), "updatedAt": st.get("updatedAt"),
        "startedAt": st.get("startedAt"), "errors": st.get("errors"), "lastError": st.get("lastError"), "cfg": st.get("cfg"),
        "baseline": base, "all": _group(rows, lambda r: "All clusters (wide net)")[0] if rows else None,
        "strict": _group(strict_rows, lambda r: "Bursts (your thresholds)")[0] if strict_rows else None,
        "groups": [
            {"title": "Price during the window", "rows": order(["Into weakness (flat or down)", "Up < 1%", "Chasing (up 1%+)", "unknown"])(_group(rows, move_b))},
            {"title": "Time of day", "rows": order(["open", "midday", "close"])(_group(rows, lambda r: r["tod"]))},
            {"title": "Size vs the ticker's normal", "rows": order(["2–5× normal", "5–10× normal", "10×+ normal", "unknown"])(_group(rows, ratio_b))},
            {"title": "Call premium in 30 minutes", "rows": order(["< $250k", "$250k–1M", "$1–3M", "$3M+"])(_group(rows, prem_b))},
            {"title": "Call prints in 30 minutes", "rows": order(["3–5 prints", "6–9 prints", "10–19 prints", "20+ prints"])(_group(rows, lambda r: "3–5 prints" if r["n"] < 6 else "6–9 prints" if r["n"] < 10 else "10–19 prints" if r["n"] < 20 else "20+ prints"))},
            {"title": "Contracts bought", "rows": order(["1 contract", "2–4 contracts", "5+ contracts"])(_group(rows, con_b))},
            {"title": "Calls vs puts in the window", "rows": order(["60–75% calls", "75–90% calls", "90%+ calls"])(_group(rows, share_b))},
        ],
        "top": [r for r in rows if r["strict"]][:40],
    }


# ---------------- live tracking ----------------

def live_scan(sub, c=None):
    """Every minute: add new alerts to today's buckets, record a burst the first time a ticker qualifies."""
    import flowdata
    pk = db.upk(sub)
    c = c or cfg_of(sub)
    st = db.get(pk, "BURSTSTATE") or {"PK": pk, "SK": "BURSTSTATE"}
    since = st.get("lastAt") or (datetime.utcnow() - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    since = min(since, (datetime.utcnow() - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    res = flowdata.alerts(sub, 10000, "all", 0, 400, True, False, None, 0, 1, since_utc=since, exclude_etfs=True)
    seen = list(st.get("seen") or [])
    seen_set = set(seen)
    key = lambda a: str(a.get("id") or f"{a.get('contract')}|{a.get('at')}|{a.get('premium')}")
    new = [a for a in res.get("alerts") or [] if key(a) not in seen_set]
    newest = max([a.get("at") or "" for a in new] + [st.get("lastAt") or ""])
    today = now_ny().strftime("%Y-%m-%d")
    by_t = {}
    for a in new:
        if (a.get("atEt") or "")[:10] == today:
            by_t.setdefault(a["ticker"], []).append(a)
    norm = (db.get(pk, "BURSTBASE") or {}).get("tickers") or {}
    last_ev = dict(st.get("lastEvent") or {})
    found = []
    for sym, al in by_t.items():
        it = db.get(pk, f"BURSTT#{today}#{sym}") or {"PK": pk, "SK": f"BURSTT#{today}#{sym}", "buckets": {}, "ttl": int(time.time()) + 3 * 86400}
        bk = it["buckets"]
        for a in al:
            k = _bucket(a.get("atEt"))
            if k:
                _add(bk.setdefault(k, {}), a)
        db.put(it)
        base30 = (norm.get(sym) or {}).get("dailyCall", 0) / SLOTS_PER_DAY * 2 or None
        w = windows(bk, base30)[-1] if bk else None
        if not w or not strict(w, c):
            continue
        if last_ev.get(sym) and last_ev[sym] > (now_ny() - timedelta(minutes=c["burstCooldownMin"])).strftime("%Y-%m-%dT%H:%M"):
            continue
        ev = {"PK": pk, "SK": f"BURST#{w['at'].replace('-', '').replace(':', '').replace('T', '')}-{sym}", "ticker": sym, **w,
              "detectedAt": iso(now_ny()), "normalKnown": base30 is not None}
        db.put(ev)
        last_ev[sym] = w["at"]
        found.append(sym)
    st.update(lastAt=newest or since, seen=(seen + [key(a) for a in new])[-5000:], lastEvent=last_ev, lastRun=iso(now_ny()),
              lastNew=len(new), today=today, burstsToday=(st.get("burstsToday", 0) if st.get("today") == today else 0) + len(found))
    db.put(st)
    return {"new": len(new), "bursts": found}


def forward_update(sub):
    """After the close: 1/3/5-day results for live bursts of the last 3 weeks."""
    from charts import _yahoo
    pk = db.upk(sub)
    cut = (now_ny() - timedelta(days=21)).strftime("%Y%m%d")
    evs = [e for e in db.q_prefix(pk, "BURST#") if e["SK"][6:14] >= cut and e.get("fwd5") is None]
    cache, n = {}, 0
    for e in evs:
        sym = e["ticker"]
        if sym not in cache:
            try:
                cache[sym] = [(b["t"][:10], b["c"]) for b in _yahoo(sym.replace(".", "-"), "1d", now_ny() - timedelta(days=35),
                                                                     now_ny() + timedelta(hours=1)) or []]
            except Exception:
                cache[sym] = []
        f = _forward(cache[sym], e["date"], e.get("price"))
        if any(v is not None for v in f.values()):
            db.update(pk, e["SK"], f)
            n += 1
    return {"updated": n}


def page(sub, days=10):
    """Everything the Bursts page shows. Each part is computed on its own so one bad record can't break the page:
    a failing part comes back with an error message instead."""
    import traceback
    pk = db.upk(sub)
    out = {"days": days, "events": [], "cfg": DEFAULTS, "live": {}, "study": None, "errors": []}
    try:
        out["cfg"] = cfg_of(sub)
    except Exception as e:
        out["errors"].append(f"settings: {e}")
    try:
        cut = (now_ny() - timedelta(days=days)).strftime("%Y%m%d")
        evs = [{k: v for k, v in e.items() if k != "PK"} for e in db.q_prefix(pk, "BURST#") if (e.get("SK") or "")[6:14] >= cut]
        out["events"] = sorted(evs, key=lambda e: e["SK"], reverse=True)[:300]
    except Exception as e:
        traceback.print_exc()
        out["errors"].append(f"live bursts: {str(e)[:200]}")
    try:
        st = db.get(pk, "BURSTSTATE") or {}
        out["live"] = {k: st.get(k) for k in ("lastRun", "lastNew", "today", "burstsToday")}
    except Exception as e:
        out["errors"].append(f"live status: {str(e)[:200]}")
    try:
        out["study"] = study_summary(db.get(pk, "BURSTSTUDY"), out["cfg"])
    except Exception as e:
        traceback.print_exc()
        out["errors"].append(f"replay results: {type(e).__name__}: {str(e)[:200]}")
    return out
