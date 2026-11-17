"""The whole US stock market for the swing scans (Qullamaggie scans everything, not an index).

Symbols: FMP company screener (NYSE, NASDAQ, AMEX; no ETF, no fund; actively trading) plus FMP's delisted companies
since 2016 (to limit survivorship bias; their bars stop at the delisting date). Daily bars: Alpaca, split-adjusted,
since 2016, 50 symbols per request. Kept only if the stock was ever liquid enough to matter: a 20-day average $ volume
of $5M+ at a price of $3+ at some point. Stored in shards of 100 symbols (store "all/shard/NNN") with an index."""
import time
from datetime import datetime, timedelta

import store
from util import BadRequest, Unavailable, iso, now_ny, utc_to_ny

FROM = "2016-01-01"
BATCH = 50
SHARD = 100
EXCH = ("NYSE", "NASDAQ", "AMEX")
MIN_DV, MIN_PX = 5e6, 3.0


def _alp(sym):
    """FMP's symbol as Alpaca writes it, or None: BRK-B → BRK.B (a share class); preferreds (AKO-A is a class, but
    XYZ-PA / XYZ^A are preferreds), units, warrants and rights left out."""
    s = str(sym or "").upper().strip()
    if not s or len(s) > 7:
        return None
    if "-" in s or "." in s:
        base, _, cls = s.replace(".", "-").partition("-")
        if not (base.isalpha() and len(cls) == 1 and cls.isalpha() and len(base) <= 5):
            return None
        return f"{base}.{cls}"
    return s if s.isalpha() and len(s) <= 5 else None


def symbols(sub):
    """[(symbol, delisted date or None)]."""
    import fmp
    key = fmp._key(sub)
    act = fmp._get(key, "company-screener", {"exchange": ",".join(EXCH), "isEtf": "false", "isFund": "false", "isActivelyTrading": "true",
                                             "limit": 10000}) or []
    out = {}
    for r in act if isinstance(act, list) else []:
        s = _alp(r.get("symbol"))
        if s and not r.get("isEtf") and not r.get("isFund"):
            out[s] = None
    for page in range(200):
        raw = fmp._get(key, "delisted-companies", {"page": page, "limit": 100}) or []
        if not isinstance(raw, list) or not raw:
            break
        old = False
        for r in raw:
            s, d = _alp(r.get("symbol")), str(r.get("delistedDate") or "")[:10]
            if d and d < FROM:
                old = True
                continue
            if s and str(r.get("exchange") or "").upper() in EXCH and s not in out:
                out[s] = d or None
        if old and page > 2:                           # newest first: past 2016 nothing more to take
            break
    return sorted(out.items())


def _fetch(sub, syms, delisted):
    import alpaca
    c, feed = alpaca.creds(sub), alpaca.status(sub)["feed"]
    end = datetime.utcnow() - timedelta(minutes=16 if feed == "sip" else 0)
    out, token = {}, None
    for _ in range(400):
        for attempt in range(40):
            try:
                r = alpaca._get(c, "stocks/bars", {"symbols": ",".join(syms), "timeframe": "1Day", "start": FROM + "T05:00:00Z",
                                                   "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 10000, "adjustment": "split",
                                                   "feed": feed, "sort": "asc", "page_token": token})
                break
            except BadRequest as e:                    # one symbol Alpaca doesn't know spoils the batch: drop it, ask again
                import re
                m = re.search(r"invalid symbol:\s*([A-Z0-9.\-^/]+)", str(e), re.I)
                if not m or m.group(1).upper() not in syms or len(syms) == 1:
                    if len(syms) == 1:
                        return out
                    raise
                syms = [x for x in syms if x != m.group(1).upper()]
                token = None
                out = {}
            except Unavailable as e:
                if "rate limit" not in str(e) or attempt == 39:
                    raise
                time.sleep(15)
        for s, bs in (r.get("bars") or {}).items():
            cut = delisted.get(s)
            for b in bs:
                d = utc_to_ny(datetime.strptime(b["t"][:19], "%Y-%m-%dT%H:%M:%S")).strftime("%Y-%m-%d")
                if cut and d > cut:
                    continue
                out.setdefault(s, []).append([d, round(b["o"], 4), round(b["h"], 4), round(b["l"], 4), round(b["c"], 4), int(b.get("v", 0))])
        token = r.get("next_page_token")
        if not token:
            break
    return out


def liquid(b):
    """Ever a 20-day average $ volume of MIN_DV+ at MIN_PX+?"""
    if len(b) < 60:
        return False
    s = 0.0
    for i, x in enumerate(b):
        s += x[4] * x[5]
        if i >= 20:
            s -= b[i - 20][4] * b[i - 20][5]
        if i >= 19 and s / 20 >= MIN_DV and x[4] >= MIN_PX:
            return True
    return False


def download(sub, prog=None, deadline=None):
    """Resumable: returns False when out of time."""
    st = store.get_json("all/state") or {}
    if not st.get("syms") or st.get("done"):
        if prog:
            prog("Whole market: the symbol list (FMP)…")
        st = {"syms": symbols(sub), "i": 0, "kept": {}, "shard": [], "n": 0, "started": iso(now_ny())}
        store.put_json("all/state", st)
    syms = st["syms"]
    delisted = {s: d for s, d in syms if d}
    kept, cur, n = st["kept"], st.get("cur") or {}, st["n"]
    i = st["i"]
    while i < len(syms):
        if deadline and time.time() > deadline:
            store.put_json("all/state", {**st, "i": i, "kept": kept, "cur": cur, "n": n})
            return False
        part = [s for s, _ in syms[i:i + BATCH]]
        if prog:
            prog(f"Whole market: daily bars {i + 1}–{i + len(part)} of {len(syms)} ({len(kept)} liquid so far)")
        got = _fetch(sub, part, delisted)
        for s in part:
            b = got.get(s)
            if b and liquid(b):
                cur[s] = b
                if len(cur) >= SHARD:
                    store.put_json(f"all/shard/{n:03d}", cur)
                    kept.update({k: n for k in cur})
                    cur, n = {}, n + 1
        i += len(part)
    if cur:
        store.put_json(f"all/shard/{n:03d}", cur)
        kept.update({k: n for k in cur})
        n += 1
    store.put_json("all/index", {"syms": kept, "shards": n, "at": iso(now_ny()), "listed": len(syms)})
    store.put_json("all/state", {"done": True, "at": iso(now_ny())})
    return True


def index():
    return store.get_json("all/index") or {}


def status():
    ix = index()
    st = store.get_json("all/state") or {}
    return {"n": len(ix.get("syms") or {}), "listed": ix.get("listed"), "at": ix.get("at"),
            "partial": None if st.get("done") or not st.get("syms") else {"i": st.get("i"), "of": len(st["syms"])}}


def universe():
    ix = index()
    syms = ix.get("syms") or {}
    if not syms:
        raise BadRequest("The whole market isn't downloaded yet: press “Download the whole market”.")
    return sorted(syms, key=lambda s: (syms[s], s))      # shard order: one shard read at a time


_CACHE = {"n": None, "d": {}}


def bars(sym):
    ix = index() if _CACHE.get("ix") is None else _CACHE["ix"]
    _CACHE["ix"] = ix
    n = (ix.get("syms") or {}).get(sym)
    if n is None:
        return None
    if _CACHE["n"] != n:
        _CACHE["n"], _CACHE["d"] = n, store.get_json(f"all/shard/{n:03d}") or {}
    return _CACHE["d"].get(sym)


def reset_cache():
    _CACHE.update({"n": None, "d": {}, "ix": None})


def start(sub):
    import alpaca
    import fmp
    import jobs
    fmp._key(sub)
    alpaca.creds(sub)
    st = store.get_json("all/state") or {}
    if st.get("done") or not st.get("i"):
        store.put_json("all/state", {})                # a fresh list and fresh bars (also after a failure at the start)
    return jobs.start(sub, "swingall", "alldl", {})
