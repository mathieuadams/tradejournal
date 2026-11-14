"""Earnings page: fundamentals screen (SEC EDGAR XBRL) and the earnings event study (Alpaca daily prices).

Sources
  SEC EDGAR (free, no key; a User-Agent with a contact e-mail is required, 10 requests / second at most):
    www.sec.gov/files/company_tickers.json                ticker -> CIK
    data.sec.gov/api/xbrl/companyfacts/CIK##########.json  every XBRL fact the company filed (10-Q / 10-K)
    data.sec.gov/submissions/CIK##########.json            filings; 8-K with item 2.02 = the earnings release
  Alpaca daily bars, split-adjusted: the price reaction around each release, and the benchmark (SPY / QQQ).
  The user's own CSV of releases: consensus, actuals, guidance and any other pre-earnings signal columns.

Storage
  DynamoDB  USER#<sub> / FUND#watch       the screen's tickers and the event-study options
  S3        fund/tickers                  SEC ticker -> CIK map (refreshed weekly)
            fund/co/<SYM>                 the company's quarters (derived from XBRL) and its 8-K 2.02 dates
            fund/px/<SYM>                 daily bars (split-adjusted)
            fund/u/<sub>/events           the imported releases
            fund/u/<sub>/study            the last event study

Quarters: 10-Qs report 3-month figures (and year-to-date ones); Q4 is only in the 10-K as the full year. Missing
quarters are derived: year-to-date differences (cash flow is usually only reported year to date), then full year minus
the three other quarters. Derived values are marked "d" (Q4 EPS derived this way ignores share count changes).

Acceptance times in EDGAR's submissions file are UTC; they are shown and classified in New York time."""
import csv
import io
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import db
import store
from util import BadRequest, Unavailable, iso, now_ny, utc_to_ny

SK_WATCH = "FUND#watch"
SEC_WWW = "https://www.sec.gov/"
SEC_DATA = "https://data.sec.gov/"
PX_FROM = "2015-01-01"
BENCHES = ("SPY", "QQQ", "IWM", "DIA")
MAX_WATCH = 150
MAX_UNIVERSE = 700
SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

REV = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
       "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueGoodsNet", "RevenuesNetOfInterestExpense"]
EPS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"]
NI = ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"]
CFO = ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
ASSETS = ["Assets"]

# ---------------------------------------------------------------- SEC access

_last_call = [0.0]


def _ua(sub):
    if os.environ.get("SEC_USER_AGENT"):
        return os.environ["SEC_USER_AGENT"]
    p = db.get(db.upk(sub), "PROFILE") or {}
    return f"Tradejournal research {p.get('email') or 'research@example.com'}"


def _http(url, ua, timeout=40):
    """GET a SEC JSON file (patched in the tests / dev server). None when the file doesn't exist."""
    wait = 0.12 - (time.time() - _last_call[0])         # SEC asks for 10 requests / second at most
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if e.code in (403, 429):
            raise Unavailable(f"SEC EDGAR refused the request ({e.code}): too many requests or the User-Agent was rejected.")
        raise Unavailable(f"SEC EDGAR error {e.code}.")
    except urllib.error.URLError as e:
        raise Unavailable(f"SEC EDGAR couldn't be reached: {e.reason}")


def check_sym(s):
    s = (s or "").strip().upper().replace("/", ".")
    if not s or len(s) > 10 or not all(ch.isalnum() or ch in ".-" for ch in s):
        raise BadRequest(f"“{s}” is not a ticker.")
    return s


def cik_map(sub):
    m = store.get_json("fund/tickers", max_age=3600)
    if m and m.get("at", "") > iso(now_ny() - timedelta(days=7)):
        return m["map"]
    raw = _http(SEC_WWW + "files/company_tickers.json", _ua(sub)) or {}
    mp = {}
    for v in raw.values():
        t = str(v.get("ticker", "")).upper()
        if t and t not in mp:
            mp[t] = [int(v["cik_str"]), v.get("title", "")]
    if not mp:
        if m:
            return m["map"]
        raise Unavailable("SEC EDGAR returned no ticker list.")
    store.put_json("fund/tickers", {"at": iso(now_ny()), "map": mp})
    return mp


def _cik_from_fmp(sub, sym):
    """A former ticker's SEC company number from FMP's profile (cached; the filings stay on EDGAR after a delisting)."""
    import fmp
    key = "fund/cik/" + sym
    c = store.get_json(key)
    if c is not None:
        return c.get("hit")
    hit = None
    try:
        if fmp.status(sub)["connected"]:
            raw = fmp._get(fmp._key(sub), "profile", {"symbol": fmp._sym(sym)}) or []
            p = raw[0] if isinstance(raw, list) and raw else {}
            if p.get("cik"):
                hit = [int(str(p["cik"]).lstrip("0") or 0), p.get("companyName") or sym]
            if not hit:                                # FMP's search covers delisted symbols too
                raw = fmp._get(fmp._key(sub), "search-symbol", {"query": fmp._sym(sym), "limit": 5}) or []
                for r in raw if isinstance(raw, list) else []:
                    if str(r.get("symbol", "")).upper() == fmp._sym(sym) and r.get("cik"):
                        hit = [int(str(r["cik"]).lstrip("0") or 0), r.get("name") or sym]
                        break
    except Exception as e:
        print("cik fmp", sym, e)
        return None                                    # not cached: try again next time
    if hit and not hit[0]:
        hit = None
    store.put_json(key, {"hit": hit})
    return hit


def lookup(sub, sym):
    mp = cik_map(sub)
    hit = mp.get(sym) or mp.get(sym.replace(".", "-")) or mp.get(sym.replace("-", "."))
    if not hit:                                        # acquired / delisted: SEC's list only has today's tickers
        hit = _cik_from_fmp(sub, sym)
    if not hit:
        raise BadRequest(f"{sym} isn't in SEC EDGAR's ticker list (US filers only; foreign 20-F filers have no quarterly XBRL).")
    return hit


# ---------------------------------------------------------------- XBRL -> quarters

def _d(s):
    return date.fromisoformat(s)


def _vals(facts, tags, unit):
    """{(start, end): val} for flow concepts (start None for instants); for each period the first tag in `tags` that has
    it, the latest filing of that period (restatements win)."""
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    out = {}
    for tag in tags:
        units = (gaap.get(tag) or {}).get("units") or {}
        rows = units.get(unit) or []
        best = {}
        for f in rows:
            if f.get("val") is None or not f.get("end"):
                continue
            if str(f.get("form", "")).split("/")[0] not in ("10-Q", "10-K", "10-KT", "10-QT", "8-K"):
                continue
            k = (f.get("start"), f["end"])
            if k not in best or f.get("filed", "") >= best[k][0]:
                best[k] = (f.get("filed", ""), float(f["val"]))
        for k, (_, v) in best.items():
            out.setdefault(k, v)
    return out


def _quarters(vals):
    """{end: (start, value, derived)} of 3-month periods, derived from year-to-date and full-year figures when needed."""
    q, spans = {}, []
    for (s, e), v in vals.items():
        if not s:
            continue
        s, e = _d(s), _d(e)
        n = (e - s).days
        if 80 <= n <= 100:
            q.setdefault(e, (s, v, False))
        if 80 <= n <= 380:
            spans.append((s, e, v))
    by_start = {}
    for s, e, v in spans:
        by_start.setdefault(s, {})[e] = v
    for s, ends in by_start.items():                   # year to date: (start..e2) - (start..e1) = the quarter after e1
        es = sorted(ends)
        for e1, e2 in zip(es, es[1:]):
            if 80 <= (e2 - e1).days <= 100 and e2 not in q:
                q[e2] = (e1 + timedelta(days=1), ends[e2] - ends[e1], True)
    for s, e, v in spans:                              # full year - the three other quarters
        if 340 <= (e - s).days <= 380 and e not in q:
            inside = sorted(k for k, x in q.items() if s - timedelta(days=5) <= x[0] and k < e)
            if len(inside) == 3:
                q[e] = (inside[-1] + timedelta(days=1), v - sum(q[k][1] for k in inside), True)
    return q


def _instants(vals):
    return {_d(e): v for (s, e), v in vals.items() if not s}


def build_quarters(facts):
    """The company's quarters, oldest first: [{end, start, rev, eps, ni, cfo, assets, d: [derived fields]}]."""
    series = {"rev": _quarters(_vals(facts, REV, "USD")), "eps": _quarters(_vals(facts, EPS, "USD/shares")),
              "ni": _quarters(_vals(facts, NI, "USD")), "cfo": _quarters(_vals(facts, CFO, "USD"))}
    assets = _instants(_vals(facts, ASSETS, "USD"))
    ends = sorted(set(series["rev"]) | set(series["eps"]) | set(series["ni"]))
    rows = []
    for e in ends:
        r = {"end": e.isoformat(), "d": []}
        for k, s in series.items():
            hit = s.get(e)
            if not hit:                                # 52/53-week calendars: a few days apart in different concepts
                near = [x for x in s if abs((x - e).days) <= 6]
                hit = s[near[0]] if near else None
            if hit:
                r[k] = round(hit[1], 6 if k == "eps" else 0)
                r.setdefault("start", hit[0].isoformat())
                if hit[2]:
                    r["d"].append(k)
        a = [v for x, v in assets.items() if abs((x - e).days) <= 6]
        if a:
            r["assets"] = a[0]
        rows.append(r)
    out = []
    for r in rows:                                     # the same quarter found twice a few days apart: keep one
        if out and (_d(r["end"]) - _d(out[-1]["end"])).days < 45:
            out[-1] = {**r, **out[-1]} if len(out[-1]) >= len(r) else {**out[-1], **r}
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- metrics

def _g(a, b):
    """Growth from b to a; with a negative or zero base, the change over |base| (None when there's no base)."""
    if a is None or b is None or b == 0:
        return None
    return (a - b) / abs(b)


def _back(qs, i, days, tol=25):
    """The index of the quarter `days` before quarter i (None if missing)."""
    want = _d(qs[i]["end"]) - timedelta(days=days)
    for j in range(i - 1, -1, -1):
        dd = (_d(qs[j]["end"]) - want).days
        if abs(dd) <= tol:
            return j
        if dd < -tol:
            return None
    return None


def _ttm(qs, i, k):
    idx = [i]
    for _ in range(3):
        j = _back(qs, idx[-1], 91, 20)
        if j is None:
            return None
        idx.append(j)
    vs = [qs[j].get(k) for j in idx]
    return None if any(v is None for v in vs) else sum(vs)


def metrics(qs):
    """Growth, EPS inflection and earnings quality for every quarter (adds fields in place); returns qs."""
    for i, q in enumerate(qs):
        p, y, y3 = _back(qs, i, 91, 20), _back(qs, i, 364), _back(qs, i, 364 * 3, 35)
        q["revQoQ"] = _g(q.get("rev"), qs[p].get("rev")) if p is not None else None
        q["revYoY"] = _g(q.get("rev"), qs[y].get("rev")) if y is not None else None
        q["epsYoY"] = _g(q.get("eps"), qs[y].get("eps")) if y is not None else None
        for k in ("rev", "eps", "ni", "cfo"):
            q[k + "TTM"] = _ttm(qs, i, k)
        ly = qs[y] if y is not None else {}
        q["revTTMYoY"] = _g(q["revTTM"], ly.get("revTTM"))
        q["epsTTMYoY"] = _g(q["epsTTM"], ly.get("epsTTM"))
        t3 = qs[y3].get("revTTM") if y3 is not None else None
        q["rev3y"] = (q["revTTM"] / t3) ** (1 / 3) - 1 if q["revTTM"] and t3 and q["revTTM"] > 0 and t3 > 0 else None
        prev = qs[p] if p is not None else {}
        q["revAccel"] = q["revYoY"] - prev["revYoY"] if q["revYoY"] is not None and prev.get("revYoY") is not None else None
        q["epsAccel"] = q["epsYoY"] - prev["epsYoY"] if q["epsYoY"] is not None and prev.get("epsYoY") is not None else None
        # earnings quality: cash conversion (CFO / net income) and the Sloan accrual ratio ((NI - CFO) / average assets)
        ni, cfo = q["niTTM"], q["cfoTTM"]
        a0, a1 = q.get("assets"), ly.get("assets")
        avg_a = (a0 + a1) / 2 if a0 and a1 else a0
        q["cashConv"] = cfo / ni if ni and cfo is not None and ni > 0 else None
        q["accruals"] = (ni - cfo) / avg_a if ni is not None and cfo is not None and avg_a else None
        q["quality"] = _quality(q["cashConv"], q["accruals"], ni)
        q["flags"] = _flags(qs, i, p, y)
    return qs


def _quality(conv, acc, ni):
    if acc is None:
        return None
    if acc > 0.10 or (ni and ni > 0 and conv is not None and conv < 0.6):
        return "weak"
    if acc < 0.05 and (conv is None or conv >= 0.9):
        return "strong"
    return "ok"


def _flags(qs, i, p, y):
    q, f = qs[i], []
    prev = qs[p] if p is not None else {}
    pp = qs[_back(qs, p, 91, 20)] if p is not None and _back(qs, p, 91, 20) is not None else {}
    ly = qs[y] if y is not None else {}
    if q.get("epsTTM") is not None and ly.get("epsTTM") is not None and q["epsTTM"] > 0 >= ly["epsTTM"]:
        f.append("TTM EPS turned positive")
    elif q.get("eps") is not None and q["eps"] > 0 and prev.get("eps") is not None and prev["eps"] <= 0:
        f.append("Quarter EPS turned positive")
    if q.get("epsYoY") is not None and prev.get("epsYoY") is not None:
        if q["epsYoY"] > 0 >= prev["epsYoY"]:
            f.append("EPS growth turned up")
        elif pp.get("epsYoY") is not None and q["epsYoY"] > prev["epsYoY"] > pp["epsYoY"] and q["epsYoY"] > 0:
            f.append("EPS growth accelerating")
    if q.get("revYoY") is not None and prev.get("revYoY") is not None and pp.get("revYoY") is not None \
            and q["revYoY"] > prev["revYoY"] > pp["revYoY"]:
        f.append("Revenue accelerating")
    return f


# ---------------------------------------------------------------- 8-K item 2.02 (earnings release) dates

def earnings_filings(subs_json, pages=()):
    """[{date, time, accn}] of 8-K filings with item 2.02 (results of operations), oldest first, one per day."""
    out, seen = [], set()
    blocks = [((subs_json or {}).get("filings") or {}).get("recent") or {}] + [p or {} for p in pages]
    for b in blocks:
        forms = b.get("form") or []
        for k, form in enumerate(forms):
            if form not in ("8-K", "8-K/A") or "2.02" not in str((b.get("items") or [""] * len(forms))[k]):
                continue
            fd = b["filingDate"][k]
            acc = (b.get("acceptanceDateTime") or [""] * len(forms))[k] or ""
            t = ""
            if len(acc) >= 16:                         # acceptance time is UTC: convert to New York time
                et = utc_to_ny(datetime.strptime(acc[:16], "%Y-%m-%dT%H:%M"))
                t, ad = et.strftime("%H:%M"), et.strftime("%Y-%m-%d")
                if ad < fd:                            # accepted after 17:30 ET: dated the next business day, released that evening
                    fd = ad
            if fd in seen or form == "8-K/A":
                continue
            seen.add(fd)
            out.append({"date": fd, "time": t, "accn": (b.get("accessionNumber") or [""] * len(forms))[k]})
    return sorted(out, key=lambda x: x["date"])


def timing_of(t):
    """bmo (before 09:30 ET) / intraday / amc (16:00 ET or later) from an HH:MM release time."""
    if not t:
        return "unknown"
    return "bmo" if t < "09:30" else "amc" if t >= "16:00" else "intraday"


# ---------------------------------------------------------------- company cache

def company(sub, sym, max_age_h=20, force=False):
    """The company's quarters with metrics, and its earnings release dates (cached per ticker)."""
    sym = check_sym(sym)
    key = "fund/co/" + sym
    c = store.get_json(key)
    if c and not force and c.get("at", "") > iso(now_ny() - timedelta(hours=max_age_h)):
        return c
    cik, name = lookup(sub, sym)
    ua, ck = _ua(sub), f"CIK{cik:010d}"
    facts = _http(SEC_DATA + f"api/xbrl/companyfacts/{ck}.json", ua) or {}
    subs = _http(SEC_DATA + f"submissions/{ck}.json", ua) or {}
    pages, since = [], (now_ny().date() - timedelta(days=365 * 11)).isoformat()
    for f in ((subs.get("filings") or {}).get("files") or [])[:12]:     # older filings (heavy filers fill "recent" fast)
        if f.get("filingTo") and f["filingTo"] < since:
            continue
        try:
            pages.append(_http(SEC_DATA + "submissions/" + f["name"], ua))
        except Exception as e:
            print("sec page failed", sym, e)
    qs = metrics(build_quarters(facts))
    c = {"sym": sym, "cik": cik, "name": subs.get("name") or name, "at": iso(now_ny()), "fiscalYearEnd": subs.get("fiscalYearEnd"),
         "quarters": qs[-48:], "releases": earnings_filings(subs, pages),
         "sic": subs.get("sic"), "industry": subs.get("sicDescription")}
    store.put_json(key, c)
    return c


def cached(sym):
    return store.get_json("fund/co/" + sym)


def screen_row(c):
    qs = c.get("quarters") or []
    if not qs:
        return {"sym": c["sym"], "name": c.get("name"), "at": c.get("at"), "empty": True}
    q = qs[-1]
    keep = ("end", "rev", "revTTM", "rev3y", "revTTMYoY", "revYoY", "revQoQ", "revAccel", "eps", "epsTTM", "epsYoY", "epsTTMYoY",
            "epsAccel", "cashConv", "accruals", "quality", "flags", "d")
    rel = c.get("releases") or []
    return {"sym": c["sym"], "name": c.get("name"), "at": c.get("at"), **{k: q.get(k) for k in keep},
            "lastRelease": rel[-1]["date"] if rel else None, "yoyTrail": [x.get("revYoY") for x in qs[-8:]]}


# ---------------------------------------------------------------- watchlist / settings

def settings(sub):
    it = db.get(db.upk(sub), SK_WATCH) or {}
    return {"tickers": it.get("tickers") or [], "useSec": it.get("useSec", True), "years": it.get("years", 5),
            "bench": it.get("bench", "SPY"), "universe": it.get("universe") or [], "uniName": it.get("uniName") or ""}


def study_tickers(st):
    """The tickers whose SEC 8-K release dates go into the study: the study universe, or the screen's tickers."""
    return st["universe"] or st["tickers"]


def save_settings(sub, b):
    cur = settings(sub)
    if "tickers" in b:
        raw = b["tickers"] if isinstance(b["tickers"], list) else str(b["tickers"]).replace(",", " ").split()
        out = []
        for s in raw:
            s = check_sym(s)
            if s not in out:
                out.append(s)
        if len(out) > MAX_WATCH:
            raise BadRequest(f"{MAX_WATCH} tickers at most.")
        cur["tickers"] = out
    if "universe" in b:
        raw = b["universe"] if isinstance(b["universe"], list) else str(b["universe"]).replace(",", " ").split()
        out = []
        for s in raw:
            s = check_sym(s)
            if s not in out:
                out.append(s)
        if len(out) > MAX_UNIVERSE:
            raise BadRequest(f"{MAX_UNIVERSE} tickers at most in the study universe.")
        cur["universe"] = out
        cur["uniName"] = str(b.get("uniName") or ("Custom" if out else ""))[:60]
    if "useSec" in b:
        cur["useSec"] = bool(b["useSec"])
    if "years" in b:
        try:
            cur["years"] = max(1, min(11, int(b["years"])))
        except (TypeError, ValueError):
            raise BadRequest("Years must be a whole number.")
    if "bench" in b:
        if b["bench"] not in BENCHES:
            raise BadRequest("Benchmark: " + ", ".join(BENCHES) + ".")
        cur["bench"] = b["bench"]
    db.put({"PK": db.upk(sub), "SK": SK_WATCH, **cur})
    return cur


# ---------------------------------------------------------------- imported releases (CSV)

ALIASES = {
    "sym": ("ticker", "symbol", "sym", "stock"),
    "date": ("date", "earnings_date", "report_date", "announce_date", "release_date", "event_date"),
    "timing": ("timing", "time", "session", "when", "hour"),
    "epsEst": ("eps_est", "eps_estimate", "consensus_eps", "eps_consensus", "est_eps", "epsestimate"),
    "epsAct": ("eps_act", "eps_actual", "actual_eps", "reported_eps", "eps"),
    "revEst": ("rev_est", "revenue_estimate", "rev_estimate", "consensus_revenue", "revenue_consensus", "est_revenue"),
    "revAct": ("rev_act", "revenue_actual", "rev_actual", "actual_revenue", "reported_revenue", "revenue"),
    "epsSurprise": ("eps_surprise", "eps_surprise_pct", "eps_surprise_%", "surprise_eps"),
    "revSurprise": ("rev_surprise", "revenue_surprise", "rev_surprise_pct", "revenue_surprise_pct", "surprise_revenue"),
    "guide": ("guidance", "guide", "guidance_change", "guide_change", "outlook"),
}
GUIDE = {"raise": "raise", "raised": "raise", "up": "raise", "higher": "raise", "+": "raise", "beat_raise": "raise",
         "lower": "lower", "lowered": "lower", "cut": "lower", "down": "lower", "-": "lower",
         "maintain": "maintain", "maintained": "maintain", "reaffirm": "maintain", "reaffirmed": "maintain", "inline": "maintain",
         "in-line": "maintain", "in line": "maintain", "same": "maintain", "initiate": "initiate", "initiated": "initiate",
         "none": "none", "no": "none", "n/a": "none", "": "none"}
TIMING = {"bmo": "bmo", "pre": "bmo", "premarket": "bmo", "pre-market": "bmo", "before": "bmo", "before open": "bmo", "am": "bmo",
          "amc": "amc", "post": "amc", "postmarket": "amc", "post-market": "amc", "after": "amc", "after close": "amc", "pm": "amc",
          "intraday": "intraday", "dmh": "intraday", "during": "intraday"}


def _num(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("$", "").replace("%", "")
    if s in ("", "-", "—", "n/a", "na", "NA", "N/A", "null", "None"):
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    mult = 1
    if s[-1:].upper() in ("K", "M", "B"):
        mult = {"K": 1e3, "M": 1e6, "B": 1e9}[s[-1].upper()]
        s = s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def _date(s):
    s = (s or "").strip()[:10]
    for f in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%d-%b-%Y", "%b %d %Y"):
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    return None


def parse_csv(text):
    """Releases from the user's CSV: ticker and date required; known columns mapped (see ALIASES), every other column
    kept as a custom signal. Returns (events, columns, problems)."""
    text = (text or "").lstrip("﻿")
    if not text.strip():
        raise BadRequest("The CSV is empty.")
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if len(rows) < 2:
        raise BadRequest("The CSV needs a header row and at least one release.")
    hdr = [h.strip() for h in rows[0]]
    norm = [h.lower().replace(" ", "_") for h in hdr]
    col = {}
    for k, names in ALIASES.items():
        for j, h in enumerate(norm):
            if h in names and j not in col.values():
                col[k] = j
                break
    if "sym" not in col or "date" not in col:
        raise BadRequest("The CSV needs a ticker column (ticker / symbol) and a date column (date / earnings_date).")
    extra = [j for j in range(len(hdr)) if j not in col.values() and hdr[j]]
    events, problems = [], []
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        g = lambda k: r[col[k]].strip() if k in col and col[k] < len(r) else ""
        d = _date(g("date"))
        try:
            sym = check_sym(g("sym"))
        except BadRequest:
            sym = None
        if not sym or not d:
            problems.append(f"Row {n}: {'ticker' if not sym else 'date'} not readable.")
            continue
        e = {"sym": sym, "date": d, "src": "csv"}
        tm = TIMING.get(g("timing").lower())
        if tm:
            e["timing"] = tm
        for k in ("epsEst", "epsAct", "revEst", "revAct", "epsSurprise", "revSurprise"):
            v = _num(g(k))
            if v is not None:
                e[k] = v
        if "epsSurprise" not in e and e.get("epsEst") is not None and e.get("epsAct") is not None and e["epsEst"] != 0:
            e["epsSurprise"] = round((e["epsAct"] - e["epsEst"]) / abs(e["epsEst"]) * 100, 3)
        if "revSurprise" not in e and e.get("revEst") and e.get("revAct") is not None:
            e["revSurprise"] = round((e["revAct"] - e["revEst"]) / abs(e["revEst"]) * 100, 3)
        if "guide" in col:
            gv = GUIDE.get(g("guide").lower())
            if gv is None:
                problems.append(f"Row {n}: guidance “{g('guide')}” not recognised (raise / maintain / lower / none).")
            else:
                e["guide"] = gv
        cx = {}
        for j in extra:
            v = r[j].strip() if j < len(r) else ""
            if v != "":
                cx[hdr[j]] = _num(v) if _num(v) is not None else v
        if cx:
            e["x"] = cx
        events.append(e)
    if not events:
        raise BadRequest("No readable releases. " + " ".join(problems[:3]))
    cols = sorted(set(k for k in col if k not in ("sym", "date"))) + [hdr[j] for j in extra]
    return events, cols, problems[:20]


def events(sub):
    return store.get_json(f"fund/u/{sub}/events") or {"events": [], "cols": [], "at": None}


def import_events(sub, text, mode="replace"):
    evs, cols, problems = parse_csv(text)
    cur = events(sub)
    if mode == "merge":
        have = {(e["sym"], e["date"]): e for e in cur["events"]}
        for e in evs:
            have[(e["sym"], e["date"])] = e
        evs, cols = sorted(have.values(), key=lambda e: (e["date"], e["sym"])), sorted(set(cur.get("cols") or []) | set(cols))
    else:
        evs = sorted(evs, key=lambda e: (e["date"], e["sym"]))
    store.put_json(f"fund/u/{sub}/events", {"events": evs, "cols": cols, "at": iso(now_ny())})
    return {"n": len(evs), "tickers": len({e["sym"] for e in evs}), "cols": cols, "problems": problems}


def clear_events(sub):
    store.delete(f"fund/u/{sub}/events")
    return {"n": 0}


# ---------------------------------------------------------------- prices

def prices(sub, sym, need_from=PX_FROM, max_age_h=18):
    import alpaca
    key = "fund/px/" + sym
    p = store.get_json(key)
    if p and p.get("from", "9") <= need_from and p.get("at", "") > iso(now_ny() - timedelta(hours=max_age_h)):
        return p["bars"]
    for attempt in range(5):                          # a 500-ticker study hits Alpaca's 200 requests / minute
        try:
            bars = alpaca.daily_range(sub, sym, min(need_from, PX_FROM))
            break
        except Unavailable as e:
            if "rate limit" not in str(e) or attempt == 4:
                raise
            time.sleep(15)
    if not bars:
        raise BadRequest(f"Alpaca returned no daily bars for {sym}.")
    store.put_json(key, {"at": iso(now_ny()), "from": min(need_from, PX_FROM), "bars": bars})
    return bars


# ---------------------------------------------------------------- one release: signals and reaction

WIN = (("r1", 1), ("r5", 5), ("r20", 20), ("r40", 40), ("r60", 60))     # sessions after the base close; 40 ≈ 2 months
FMP_SIGNALS = ("epsEst", "revEst", "epsAct", "revAct")


def _idx_on_or_after(dates, d):
    import bisect
    i = bisect.bisect_left(dates, d)
    return i if i < len(dates) else None


def pre_signals(bars, bmap, k):
    """What the stock did into the close of session k (the last close before the release could be priced):
    returns over 1 / 5 / 21 / 63 sessions vs the benchmark, trend, volume and accumulation. Uses bars[..k] only."""
    if k < 64:
        return {}
    c = lambda i: bars[i][4]
    v = lambda i: bars[i][5] or 0
    dates = lambda i: bars[i][0]

    def ab(n):
        raw = c(k) / c(k - n) - 1
        a, b = bmap.get(dates(k - n)), bmap.get(dates(k))
        return raw, (raw - (b / a - 1)) if a and b else None

    out = {}
    for n, key in ((1, "pre1"), (5, "pre5"), (21, "pre21"), (63, "pre63")):
        out[key], out[key + "ab"] = ab(n)
    out["runup20"] = ab(20)[1] if ab(20)[1] is not None else ab(20)[0]
    rets = [c(i) / c(i - 1) - 1 for i in range(k - 19, k + 1)]
    m = sum(rets) / len(rets)
    out["vol20"] = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))
    hi52, lo52 = max(b[2] for b in bars[max(0, k - 251):k + 1]), min(b[3] for b in bars[max(0, k - 251):k + 1])
    out["offHigh"] = c(k) / hi52 - 1
    out["offLow"] = c(k) / lo52 - 1 if lo52 > 0 else None
    out["rangePos"] = (c(k) - lo52) / (hi52 - lo52) if hi52 > lo52 else None     # 0 = at the 52-week low, 1 = at the high
    if k >= 252:                                       # volume over 2 and 6 months: rising / falling, and who did it
        v1y = sum(v(i) for i in range(k - 251, k + 1)) / 252
        for n, tag in ((42, "42"), (126, "126")):
            win = range(k - n + 1, k + 1)
            tv_ = sum(v(i) for i in win)
            if v1y > 0:
                out["volTrend" + tag] = tv_ / n / v1y      # the window's average volume vs the year's
            if tv_ > 0:
                out["udVol" + tag] = sum(v(i) for i in win if c(i) > c(i - 1)) / tv_    # share of volume on up days
                mf = 0.0
                for i in win:
                    h, l_, cl = bars[i][2], bars[i][3], bars[i][4]
                    mf += (((cl - l_) - (h - cl)) / (h - l_) if h > l_ else 0) * v(i)
                out["cmf" + tag] = mf / tv_
        out["pre126"], out["pre126ab"] = ab(126)
    rs, ms = [], []                                    # beta vs the benchmark over the year before
    for i in range(max(1, k - 251), k + 1):
        a0, a1 = bmap.get(dates(i - 1)), bmap.get(dates(i))
        if a0 and a1:
            rs.append(c(i) / c(i - 1) - 1)
            ms.append(a1 / a0 - 1)
    if len(rs) >= 120:
        mr, mm = sum(rs) / len(rs), sum(ms) / len(ms)
        vm = sum((x - mm) ** 2 for x in ms)
        if vm > 0:
            out["beta"] = max(-1.0, min(4.0, sum((x - mr) * (y - mm) for x, y in zip(rs, ms)) / vm))
    out["sma50"] = c(k) / (sum(c(i) for i in range(k - 49, k + 1)) / 50) - 1
    if k >= 200:
        out["sma200"] = c(k) / (sum(c(i) for i in range(k - 199, k + 1)) / 200) - 1
    v50 = sum(v(i) for i in range(k - 49, k + 1)) / 50
    if v50 > 0:
        out["relVol1"] = v(k) / v50                    # the last session's volume vs its 50-day average
        out["relVol5"] = sum(v(i) for i in range(k - 4, k + 1)) / 5 / v50
        out["relVol20"] = sum(v(i) for i in range(k - 19, k + 1)) / 20 / v50
    tv = sum(v(i) for i in range(k - 19, k + 1))
    if tv > 0:                                         # accumulation over 20 sessions
        mf = 0.0
        for i in range(k - 19, k + 1):
            h, l_, cl = bars[i][2], bars[i][3], bars[i][4]
            mf += (((cl - l_) - (h - cl)) / (h - l_) if h > l_ else 0) * v(i)
        out["cmf20"] = mf / tv                         # Chaikin money flow: closes near the high on volume
        out["obv20"] = sum((1 if c(i) > c(i - 1) else -1 if c(i) < c(i - 1) else 0) * v(i) for i in range(k - 19, k + 1)) / tv
    return out


def volume_patterns(r):
    """Range position × 6-month volume: rising volume near the lows with buying (accumulation) vs selling, light or heavy
    volume near the highs (distribution)."""
    rp, vt, ud = r.get("rangePos"), r.get("volTrend126"), r.get("udVol126")
    out = {}
    if rp is None:
        return out
    zone = "Near the low" if rp < 0.3 else "Near the high" if rp > 0.7 else "Mid-range"
    if vt is not None:
        out["rangeVol"] = f"{zone} · 6m volume " + ("rising" if vt > 1.15 else "falling" if vt < 0.87 else "flat")
    if ud is not None:
        out["rangeFlow"] = f"{zone} · 6m " + ("buying (up-day volume > 52%)" if ud > 0.52 else "selling (up-day volume < 48%)" if ud < 0.48 else "balanced")
    return out


def market_ctx(bench):
    """The market (benchmark) state at each date's close: 1- and 3-month returns, distance from its 200-day average,
    20-day daily volatility. {date: {...}} computed once per study."""
    out, cl = {}, [b[4] for b in bench]
    for k in range(200, len(bench)):
        rets = [cl[i] / cl[i - 1] - 1 for i in range(k - 19, k + 1)]
        m = sum(rets) / 20
        out[bench[k][0]] = {"spy21": cl[k] / cl[k - 21] - 1, "spy63": cl[k] / cl[k - 63] - 1,
                            "spyVs200": cl[k] / (sum(cl[k - 199:k + 1]) / 200) - 1,
                            "spyVol20": math.sqrt(sum((x - m) ** 2 for x in rets) / 19)}
    return out


def regime_of(ctx):
    if not ctx or ctx.get("spy63") is None:
        return None
    up = ctx.get("spyVs200", 0) >= 0
    return ("Above 200d" if up else "Below 200d") + (", 3m up" if ctx["spy63"] >= 0 else ", 3m down")


def reaction(ev, bars, bench, bmap=None, bopen=None, mctx=None):
    """Price reaction around one release. Base = the last close before the market could react (the release day's close
    after an after-close release, the previous close otherwise); day 0 = the first session that could react."""
    dates = [b[0] for b in bars]
    i = _idx_on_or_after(dates, ev["date"])
    if i is None:
        return None
    base = (i if dates[i] == ev["date"] else i - 1) if ev.get("timing") == "amc" else i - 1
    d0 = base + 1
    if base < 64 or d0 >= len(bars):
        return None
    c = lambda k: bars[k][4]
    bd = bmap if bmap is not None else {b[0]: b[4] for b in bench}

    def bret(k0, k1):
        a, b = bd.get(dates[k0]), bd.get(dates[k1])
        return b / a - 1 if a and b else None

    out = {"base": dates[base], "day0": dates[d0], "gap": bars[d0][1] / c(base) - 1}
    for name, n in WIN:
        k = base + n
        if k < len(bars):
            out["t" + name[1:]] = dates[k]
            out[name] = c(k) / c(base) - 1
            br = bret(base, k)
            out[name + "ab"] = out[name] - br if br is not None else None
    for name, n in (("drift5", 5), ("drift20", 20), ("drift40", 40), ("drift60", 60)):   # after day 0, vs the benchmark
        k = base + n
        if k < len(bars):
            br = bret(d0, k)
            out[name] = c(k) / c(d0) - 1 - br if br is not None else None
    bo = bopen if bopen is not None else {b[0]: b[1] for b in bench}
    o0, so, sc0 = bars[d0][1], bo.get(dates[d0]), bd.get(dates[d0])
    if o0 and so and sc0:                              # after the call: from the day-0 open, vs the benchmark's own open
        out["o2c"] = c(d0) / o0 - 1 - (sc0 / so - 1)
        k = base + 5
        if k < len(bars) and bd.get(dates[k]):
            out["after5"] = c(k) / o0 - 1 - (bd[dates[k]] / so - 1)
    out.update(pre_signals(bars, bd, base))
    if mctx is not None:
        ctx = mctx.get(dates[base])
        if ctx:
            out.update(ctx)
            out["regime"] = regime_of(ctx)
    b_ = out.get("beta")
    if b_ is not None:                                 # beta-adjusted: the stock minus beta × the benchmark
        for name, n in (("r1", 1), ("r20", 20), ("r40", 40)):
            k = base + n
            if out.get(name) is not None:
                br = bret(base, k)
                if br is not None:
                    out[name + "b"] = out[name] - b_ * br
        for name, n in (("drift20", 20), ("drift40", 40)):
            k = base + n
            if k < len(bars):
                br = bret(d0, k)
                if br is not None:
                    out[name + "b"] = c(k) / c(d0) - 1 - b_ * br
    if out.get("r1ab") is not None and out.get("vol20"):
        out["z1"] = out["r1ab"] / out["vol20"]         # day-0 abnormal move in daily standard deviations
    if bars[d0][5] and out.get("relVol1") is not None:
        v50 = sum(bars[x][5] or 0 for x in range(base - 49, base + 1)) / 50
        out["vol0"] = bars[d0][5] / v50 if v50 else None   # reaction-day volume vs normal
    return out


def _quarter_for(c, d):
    """The quarter a release on `d` reports: the latest quarter that ended 5-120 days before."""
    best = None
    for q in (c or {}).get("quarters") or []:
        n = (_d(d) - _d(q["end"])).days
        if 5 <= n <= 120:
            best = q
    return best


def setup_of(e):
    s, r, g = e.get("epsSurprise"), e.get("revSurprise"), e.get("guide")
    if s is None and r is None and g is None:
        return None
    beat = (s is None or s > 0) and (r is None or r > 0) and (s is not None or r is not None)
    miss = (s is not None and s < 0) or (r is not None and r < 0)
    if g == "lower":
        return "Lowered guidance"
    if beat and g == "raise":
        return "Beat & raise"
    if beat:
        return "Beat, no raise"
    if miss and g == "raise":
        return "Miss & raise"
    if miss:
        return "Miss"
    return "In line"


# ---------------------------------------------------------------- the study

OUTCOMES = [("gap", "Gap"), ("o2c", "Day 0 open → close vs bench"), ("r1ab", "Day 0 vs bench"), ("r1b", "Day 0 beta-adjusted"), ("z1", "Day 0 (σ)"), ("r5ab", "5 days vs bench"),
            ("r20ab", "1 month vs bench"), ("r40ab", "2 months vs bench"), ("drift5", "Drift d1→d5"), ("drift20", "Drift d1→d20"),
            ("drift40", "Drift d1→d40"), ("drift40b", "Drift d1→d40 beta-adjusted")]
NUM_SIGNALS = [("pre1ab", "Day before vs bench"), ("pre5ab", "5 days before vs bench"), ("pre21ab", "1 month before vs bench"),
               ("pre63ab", "3 months before vs bench"), ("offHigh", "Distance from 52w high"), ("sma50", "vs 50-day average"),
               ("sma200", "vs 200-day average"), ("vol20", "20-day volatility"), ("relVol1", "Volume, day before (× 50d avg)"),
               ("relVol5", "Volume, 5 days before (× 50d avg)"), ("relVol20", "Volume, 20 days before (× 50d avg)"),
               ("cmf20", "Accumulation (Chaikin money flow 20d)"), ("obv20", "On-balance volume 20d (share of volume)"),
               ("estEpsG", "Expected EPS growth (consensus vs year ago)"), ("estRevG", "Expected revenue growth (consensus)"),
               ("epsSurprise", "EPS surprise %"), ("revSurprise", "Revenue surprise %"),
               ("revYoY", "Revenue YoY (reported)"), ("revAccel", "Revenue YoY change (reported)"), ("revQoQ", "Revenue QoQ"),
               ("rev3y", "Revenue 3Y CAGR"), ("epsYoY", "EPS YoY (reported)"), ("epsAccel", "EPS acceleration"),
               ("accruals", "Accrual ratio"), ("cashConv", "Cash conversion"), ("prevRevYoY", "Revenue YoY, prior quarter"),
               ("ownAbs", "Own average |day-0 move|, past releases"), ("offLow", "Distance above the 52w low"),
               ("rangePos", "Position in the 52-week range (0 low … 1 high)"),
               ("volTrend42", "Volume, last 2 months vs the year"), ("volTrend126", "Volume, last 6 months vs the year"),
               ("udVol42", "Up-day share of volume, 2 months"), ("udVol126", "Up-day share of volume, 6 months"),
               ("cmf42", "Accumulation (CMF), 2 months"), ("cmf126", "Accumulation (CMF), 6 months"), ("pre126ab", "6 months before vs bench"), ("beta", "Beta vs the benchmark (1 year)"),
               ("spy21", "Market (benchmark) 1 month before"), ("spy63", "Market 3 months before"), ("spyVs200", "Market vs its 200-day average"),
               ("spyVol20", "Market volatility (20d)"), ("gradeNet90", "Analyst upgrades − downgrades, 90 days before"),
               ("gradeNet30", "Analyst upgrades − downgrades, 30 days before"), ("txTone", "Call tone"), ("txToneQA", "Call tone, Q&A"),
               ("txToneChg", "Call tone change vs previous call"), ("txUncert", "Call uncertainty (per 1,000 words)"),
               ("txGuide", "Call guidance score (raise − lower)"), ("prevTxTone", "Previous call tone (known before)")]
CAT_SIGNALS = [("regime", "Market regime"), ("range52", "52-week range"), ("rangeVol", "52-week range × 6-month volume trend"),
               ("rangeFlow", "52-week range × 6-month buying / selling"), ("setup", "Beat / raise"), ("guide", "Guidance (CSV, else from the call)"), ("timing", "Release timing"), ("quality", "Earnings quality"),
               ("inflection", "EPS inflection"), ("revDir", "Revenue growth direction"), ("sector", "Sector (SIC)")]
SIC_DIV = [(100, "Agriculture"), (1000, "Mining & energy"), (1500, "Construction"), (2000, "Manufacturing"), (4000, "Transport, utilities, telecom"),
           (5000, "Wholesale"), (5200, "Retail"), (6000, "Finance & real estate"), (7000, "Services"), (7370, "Software & IT services"),
           (7380, "Services"), (8000, "Health & other services"), (9100, "Public administration")]


def sector_of(sic):
    try:
        n = int(sic)
    except (TypeError, ValueError):
        return None
    if 2830 <= n <= 2836 or 3840 <= n <= 3851:
        return "Pharma & medical devices"
    if 3570 <= n <= 3579 or 3660 <= n <= 3679:
        return "Hardware & semiconductors"
    name = None
    for lo, nm in SIC_DIV:
        if n >= lo:
            name = nm
    return name


# ---------------------------------------------------------------- transcripts and analyst actions (FMP)

class NoPlan(Exception):
    """The FMP plan doesn't include this dataset: stop asking for it in this run."""


def tx_index(sub, sym, since, fetch=True, deadline=None, force=False):
    """The company's scored call transcripts (texts cached for good in S3: they stay after a plan downgrade)."""
    import fmp
    import sentiment
    key = "fund/txi/" + sym
    idx = store.get_json(key) or {"at": "", "items": []}
    if not fetch or (not force and idx.get("at", "") > iso(now_ny() - timedelta(days=5))):
        return idx["items"]
    try:
        dates = fmp.transcript_dates(sub, sym)
    except BadRequest as e:
        if "isn't in your plan" in str(e) or "402" in str(e):
            raise NoPlan(str(e))
        raise
    have = {i["date"] for i in idx["items"]}
    changed = False
    for d in dates:
        if d["date"] < since or d["date"] in have:
            continue
        if deadline and time.time() > deadline:
            break
        t = fmp.transcript(sub, sym, d["year"], d["quarter"])
        if not t:
            continue
        sc = sentiment.score(t["content"])
        store.put_json(f"fund/tx/{sym}/{d['date']}", {"sym": sym, **d, "content": t["content"], "guideSents": sc["guideSents"]})
        idx["items"].append({**d, **{k: v for k, v in sc.items() if k != "guideSents"}})
        have.add(d["date"])
        changed = True
    else:
        idx["at"] = iso(now_ny())
        changed = True
    if changed:
        idx["items"].sort(key=lambda x: x["date"])
        store.put_json(key, idx)
    if deadline and time.time() > deadline:
        raise TimeoutError("out of time")
    return idx["items"]


def transcript_view(sub, sym, date_):
    sym = check_sym(sym)
    t = store.get_json(f"fund/tx/{sym}/{date_}")
    if not t:
        raise BadRequest("No stored transcript for that call: run the Event study with FMP Ultimate connected to download them.")
    item = next((i for i in (store.get_json("fund/txi/" + sym) or {}).get("items") or [] if i["date"] == date_), {})
    return {**t, "scores": item}


def _tx_fields(row, items):
    """The call that goes with this release (0-3 days after it) and the one before it."""
    d = _d(row["date"])
    cur = next((i for i in items if -1 <= (_d(i["date"]) - d).days <= 3), None)
    prev = [i for i in items if (_d(i["date"]) - d).days < -20]
    prev = prev[-1] if prev else None
    if cur:
        row.update({"txDate": cur["date"], "txTone": cur.get("tone"), "txTonePrep": cur.get("tonePrep"), "txToneQA": cur.get("toneQA"),
                    "txUncert": cur.get("uncert"), "txNeg": cur.get("neg"), "txGuide": cur.get("guide"), "txGuideDir": cur.get("guideDir")})
    if prev:
        row.update({"prevTxTone": prev.get("tone"), "prevTxGuide": prev.get("guide"), "prevTxUncert": prev.get("uncert")})
        if cur and cur.get("tone") is not None and prev.get("tone") is not None:
            row["txToneChg"] = cur["tone"] - prev["tone"]
    if not row.get("guide") and row.get("txGuideDir"):
        row["guide"], row["guideSrc"] = row["txGuideDir"], "call"


def _grade_fields(row, grades, base):
    """Analyst upgrades minus downgrades before the base close (dated actions strictly before it: no look-ahead)."""
    b = _d(base)
    for n in (30, 90):
        lo = (b - timedelta(days=n)).isoformat()
        sel = [g for g in grades if lo <= g["date"] < base]
        row[f"gradeNet{n}"] = sum(1 for g in sel if g["action"] == "up") - sum(1 for g in sel if g["action"] == "down")
        if n == 90:
            row["gradeN90"] = len(sel)


def _merge_fmp(evs, s, frows, since):
    """FMP releases: the announcement date, consensus and actuals; they override the 8-K date within 3 days."""
    for r in frows:
        if r["date"] < since or r["epsAct"] is None and r["revAct"] is None:
            continue                                   # upcoming: no reaction yet
        near = [k for k in evs if k[0] == s and abs((_d(k[1]) - _d(r["date"])).days) <= 3]
        e = evs.pop(near[0]) if near else {"sym": s, "src": "fmp"}
        if e.get("src") == "csv":                      # your CSV wins; FMP only fills what it lacks
            for a, b in (("epsEst", "epsEst"), ("epsAct", "epsAct"), ("revEst", "revEst"), ("revAct", "revAct")):
                if e.get(a) is None and r[b] is not None:
                    e[a] = r[b]
            evs[(s, e["date"])] = e
            continue
        if e.get("src") == "sec":
            e["sec"] = True
        e.update({"date": r["date"], "src": "fmp" if e.get("src") != "csv" else "csv", "epsEst": r["epsEst"], "epsAct": r["epsAct"],
                  "revEst": r["revEst"], "revAct": r["revAct"]})
        if r["timing"]:
            e["timing"] = r["timing"]
        evs[(s, e["date"])] = e


def _surprises(e):
    if e.get("epsSurprise") is None and e.get("epsEst") not in (None, 0) and e.get("epsAct") is not None:
        e["epsSurprise"] = round((e["epsAct"] - e["epsEst"]) / abs(e["epsEst"]) * 100, 3)
    if e.get("revSurprise") is None and e.get("revEst") and e.get("revAct") is not None:
        e["revSurprise"] = round((e["revAct"] - e["revEst"]) / abs(e["revEst"]) * 100, 3)
    if e.get("revSurprise") is not None and abs(e["revSurprise"]) > 50:
        e["revSurpriseOff"] = e.pop("revSurprise")     # the estimate is on another basis (REITs, insurers): not a surprise


def _prev_quarter(c, q):
    qs = (c or {}).get("quarters") or []
    for k, x in enumerate(qs):
        if x is q:
            return qs[k - 1] if k > 0 else None
    return None


def build_rows(sub, opts, prog=None, deadline=None):
    """Every release with what the stock did before it, the fundamentals and consensus, and the reaction.
    Sources: your CSV, FMP releases (when connected) and SEC 8-K 2.02 dates of the study universe (or the screen)."""
    import fmp
    imp = events(sub)["events"]
    st = settings(sub)
    bench_sym = opts.get("bench") or st["bench"]
    use_sec = opts.get("useSec", st["useSec"])
    since = (now_ny().date() - timedelta(days=365 * int(opts.get("years") or st["years"]))).isoformat()
    uni = study_tickers(st) if use_sec else []
    has_fmp = fmp.status(sub)["connected"]
    by_sym = {}
    for e in imp:
        by_sym.setdefault(e["sym"], {})[(e["sym"], e["date"])] = dict(e)
    syms = sorted(set(by_sym) | set(uni))
    need = (_d(since) - timedelta(days=400)).isoformat()
    first_imp = min((e["date"] for e in imp), default=since)
    need = min(need, (_d(first_imp) - timedelta(days=400)).isoformat())
    if prog:
        prog(f"Prices: {bench_sym}")
    bench = prices(sub, bench_sym, need, max_age_h=72)
    bmap = {b[0]: b[4] for b in bench}
    bopen = {b[0]: b[1] for b in bench}
    mctx = market_ctx(bench)
    rows, skipped, t_last = [], [], 0.0
    uni_set = set(uni)
    plan_ok = {"grades": True, "tx": True}
    tx_note, tx_fail = [], [0]
    for n, s in enumerate(syms):
        if deadline and time.time() > deadline:
            raise TimeoutError("out of time")
        if prog and time.time() - t_last > 3:
            prog(f"{n + 1}/{len(syms)}: {s} · {len(rows)} releases so far")
            t_last = time.time()
        evs = by_sym.get(s, {})
        try:
            c = company(sub, s, max_age_h=24 * 14)
        except Exception as ex:
            skipped.append(f"{s}: {str(ex)[:100]}")
            c = None
        if s in uni_set:
            for r in ((c or {}).get("releases") or []):
                if r["date"] < since:
                    continue
                near = [k for k in evs if abs((_d(k[1]) - _d(r["date"])).days) <= 3]
                if near:
                    evs[near[0]].setdefault("timing", timing_of(r["time"]))
                    evs[near[0]]["sec"] = True
                else:
                    evs[(s, r["date"])] = {"sym": s, "date": r["date"], "timing": timing_of(r["time"]), "src": "sec", "time": r["time"]}
        frows = []
        if has_fmp and s in uni_set | set(by_sym):
            try:
                frows = fmp.earnings(sub, s, max_age_h=24 * 7)
                _merge_fmp(evs, s, frows, min(since, first_imp))
            except Exception as ex:
                skipped.append(f"{s} FMP: {str(ex)[:100]}")
        for e in evs.values():
            if not e.get("timing"):
                rel = [r for r in ((c or {}).get("releases") or []) if abs((_d(r["date"]) - _d(e["date"])).days) <= 3]
                e["timing"] = timing_of(rel[0]["time"]) if rel else "unknown"
            _surprises(e)
        if not evs:
            continue
        try:
            bars = prices(sub, s, need, max_age_h=72)
        except Exception as ex:
            skipped.append(f"{s} prices: {str(ex)[:100]}")
            continue
        grades, txs = None, []
        if has_fmp and plan_ok["grades"]:
            try:
                grades = fmp.grades(sub, s)
            except BadRequest as ex:
                plan_ok["grades"] = False
                skipped.append(f"Analyst grades not in the FMP plan: {str(ex)[:100]}")
            except Exception as ex:
                skipped.append(f"{s} grades: {str(ex)[:80]}")
        if has_fmp and plan_ok["tx"] and opts.get("transcripts", True):
            try:
                txs = tx_index(sub, s, min(since, first_imp), True, deadline)
            except NoPlan as ex:
                plan_ok["tx"] = False
                tx_note.append(f"Transcripts not in the FMP plan: {str(ex)[:160]}")
                skipped.append(f"Transcripts not in the FMP plan (Ultimate): {str(ex)[:100]}")
            except TimeoutError:
                raise
            except Exception as ex:
                tx_fail[0] += 1
                if len(tx_note) < 5:
                    tx_note.append(f"{s}: {str(ex)[:160]}")
                skipped.append(f"{s} transcripts: {str(ex)[:80]}")
        else:
            txs = (store.get_json("fund/txi/" + s) or {}).get("items") or []
        fact = {r["date"]: r for r in frows if r["epsAct"] is not None or r["revAct"] is not None}
        fdates = sorted(fact)
        sec = sector_of((c or {}).get("sic"))
        mine = []
        for e in sorted(evs.values(), key=lambda x: x["date"]):
            re_ = reaction(e, bars, bench, bmap, bopen, mctx)
            if not re_:
                continue
            row = {**{k2: v for k2, v in e.items() if k2 != "x"}, **re_, "sector": sec}
            rp = row.get("rangePos")
            row["range52"] = None if rp is None else "Near the low (bottom 20%)" if rp < 0.2 else "Near the high (top 20%)" if rp > 0.8 else "Middle of the range"
            row.update(volume_patterns(row))
            q = _quarter_for(c, e["date"])
            if q:
                row["qEnd"] = q["end"]
                for k2 in ("revYoY", "revAccel", "revQoQ", "rev3y", "epsYoY", "epsAccel", "accruals", "cashConv", "quality"):
                    row[k2] = q.get(k2)
                fl = q.get("flags") or []
                row["inflection"] = next((f for f in fl if "EPS" in f), "None")
                row["revDir"] = None if q.get("revAccel") is None else "Accelerating" if q["revAccel"] > 0.005 else "Decelerating" if q["revAccel"] < -0.005 else "Flat"
                pq = _prev_quarter(c, q)                # known before this release: the previous quarter
                if pq:
                    for k2 in ("revYoY", "revAccel", "epsYoY", "accruals"):
                        row["prev" + k2[0].upper() + k2[1:]] = pq.get(k2)
            # consensus growth expected going in: estimate vs the actual four releases earlier (FMP's own basis)
            if e.get("epsEst") is not None or e.get("revEst") is not None:
                before = [x for x in fdates if x < (_d(e["date"]) - timedelta(days=300)).isoformat()]
                ya = fact[before[-1]] if before and (_d(e["date"]) - _d(before[-1])).days <= 430 else None
                if ya:
                    row["estEpsG"] = _g(e.get("epsEst"), ya["epsAct"])
                    row["estRevG"] = _g(e.get("revEst"), ya["revAct"])
            if grades is not None:
                _grade_fields(row, grades, row["base"])
            if txs:
                _tx_fields(row, txs)
            row["setup"] = setup_of(row)
            if e.get("x"):
                row["x"] = e["x"]
            # this stock's own history of reactions, releases before this one only
            prior = [m for m in mine if m.get("r1ab") is not None and m.get("t1", "9") < row["base"]][-12:]
            if prior:
                row["ownAbs"] = sum(abs(m["r1ab"]) for m in prior) / len(prior)
                row["ownMean"] = sum(m["r1ab"] for m in prior) / (len(prior) + 4)     # shrunk toward zero
                row["prevR1ab"] = prior[-1]["r1ab"]
                row["ownN"] = len(prior)
            mine.append(row)
        rows.extend(mine)
    rows.sort(key=lambda r: (r["date"], r["sym"]))
    TX_NOTE[0] = {"on": bool(has_fmp and opts.get("transcripts", True)), "planOk": plan_ok["tx"], "failed": tx_fail[0], "notes": tx_note}
    return rows, bench_sym, skipped


def _stats(xs):
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    n = len(xs)
    if not n:
        return {"n": 0}
    m = sum(xs) / n
    s = sorted(xs)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else None
    return {"n": n, "mean": m, "median": med, "hit": sum(1 for x in xs if x > 0) / n, "t": m / (sd / math.sqrt(n)) if sd else None}


def _rank(xs):
    o = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(o):
        j = i
        while j + 1 < len(o) and xs[o[j + 1]] == xs[o[i]]:
            j += 1
        for k in range(i, j + 1):
            r[o[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b):
    pr = [(x, y) for x, y in zip(a, b) if x is not None and y is not None and math.isfinite(x) and math.isfinite(y)]
    if len(pr) < 8:
        return None, len(pr)
    ra, rb = _rank([p[0] for p in pr]), _rank([p[1] for p in pr])
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return (cov / math.sqrt(va * vb) if va and vb else None), len(pr)


def _terciles(vals):
    xs = sorted(v for v in vals if v is not None and math.isfinite(v))
    if len(xs) < 9:
        return None
    return xs[len(xs) // 3], xs[2 * len(xs) // 3]


def aggregate(rows):
    sigs = list(NUM_SIGNALS) + [("x:" + k, k) for k in sorted({k for r in rows for k, v in (r.get("x") or {}).items() if isinstance(v, (int, float))})]
    cats = list(CAT_SIGNALS) + [("x:" + k, k) for k in sorted({k for r in rows for k, v in (r.get("x") or {}).items() if isinstance(v, str)})]
    val = lambda r, k: (r.get("x") or {}).get(k[2:]) if k.startswith("x:") else r.get(k)
    out = {"all": {o: _stats([r.get(o) for r in rows]) for o, _ in OUTCOMES}, "groups": [], "corr": []}
    for k, label in sigs:
        vs = [val(r, k) for r in rows]
        vs = [v if isinstance(v, (int, float)) else None for v in vs]
        cut = _terciles(vs)
        if cut:
            lo, hi = cut
            buckets = [("Low", lambda v: v < lo), ("Middle", lambda v: lo <= v < hi), ("High", lambda v: v >= hi)]
            gs = []
            for name, f in buckets:
                sel = [r for r, v in zip(rows, vs) if v is not None and f(v)]
                gs.append({"name": name, "range": [min(v for v in vs if v is not None and f(v)), max(v for v in vs if v is not None and f(v))] if sel else None,
                           **{o: _stats([r.get(o) for r in sel]) for o, _ in OUTCOMES}})
            out["groups"].append({"key": k, "label": label, "kind": "num", "groups": gs})
        cr = {"key": k, "label": label}
        for o, _ in OUTCOMES:
            rho, n = spearman(vs, [r.get(o) for r in rows])
            cr[o] = {"rho": rho, "n": n}
        if any(cr[o]["rho"] is not None for o, _ in OUTCOMES):
            out["corr"].append(cr)
    for k, label in cats:
        vs = [val(r, k) for r in rows]
        names = sorted({v for v in vs if isinstance(v, str) and v}, key=lambda s: -sum(1 for v in vs if v == s))[:12]
        if len(names) < 2 and not (names and len([v for v in vs if v]) < len(vs)):
            continue
        gs = [{"name": nm, **{o: _stats([r.get(o) for r, v in zip(rows, vs) if v == nm]) for o, _ in OUTCOMES}} for nm in names]
        out["groups"].append({"key": k, "label": label, "kind": "cat", "groups": gs})
    return out


TX_NOTE = [None]
ROW_PAGE = 1200                                       # an API response must stay under 6 MB

EXPORT_KEYS = ["sym", "date", "timing", "src", "sector", "base", "day0", "qEnd", "prevRevYoY", "prevRevAccel", "prevEpsYoY", "revYoY", "revAccel",
               "revQoQ", "rev3y", "epsYoY", "epsAccel", "accruals", "cashConv", "quality", "inflection", "revDir", "epsEst", "epsAct", "epsSurprise",
               "revEst", "revAct", "revSurprise", "estEpsG", "estRevG", "guide", "guideSrc", "setup", "gradeNet30", "gradeNet90", "gradeN90",
               "txDate", "txTone", "txTonePrep", "txToneQA", "txUncert", "txNeg", "txGuide", "txGuideDir", "txToneChg", "prevTxTone", "prevTxGuide",
               "pre1", "pre1ab", "pre5", "pre5ab", "pre21", "pre21ab", "pre63", "pre63ab", "offHigh", "sma50", "sma200", "vol20", "relVol1", "relVol5",
               "relVol20", "cmf20", "obv20", "ownAbs", "ownMean", "prevR1ab", "gap", "vol0", "r1", "r1ab", "z1", "r5", "r5ab", "r20", "r20ab", "r40",
               "r40ab", "r60", "r60ab", "drift5", "drift20", "drift40", "drift60", "o2c", "after5", "t1", "t5", "t20", "t40", "t60",
               "offLow", "rangePos", "range52", "rangeVol", "rangeFlow", "volTrend42", "volTrend126", "udVol42", "udVol126", "cmf42", "cmf126",
               "pre126", "pre126ab", "beta", "r1b", "r20b", "r40b", "drift20b", "drift40b", "spy21", "spy63", "spyVs200", "spyVol20", "regime", "postScore", "oosO2c"]


def _r6(v):
    return float(f"{v:.6g}") if isinstance(v, float) else v


def build_exports(sub, res=None, rows=None):
    """The two downloads, written next to the study: the review file (summary + every release, columnar JSON, gzip)
    and the CSV of every release (gzip)."""
    import csv as _csv
    import gzip
    res = res or study(sub)
    if not res:
        raise BadRequest("Run the event study first.")
    if rows is None:
        rows = (store.get_json(f"fund/u/{sub}/study_rows") or {}).get("rows") or []
    xk = sorted({k for r in rows for k in (r.get("x") or {})})
    cols = EXPORT_KEYS + ["x:" + k for k in xk]
    summary = {k: v for k, v in res.items() if k != "rows"}
    review = {"kind": "tradejournal-earnings-study", "exportedAt": iso(now_ny()), "summary": summary,
              "releases": {"cols": cols, "data": [[_r6(r.get(k)) for k in EXPORT_KEYS] + [_r6((r.get("x") or {}).get(k)) for k in xk] for r in rows]}}
    store.put_raw(f"fund/u/{sub}/export/review.json.gz", gzip.compress(json.dumps(review, separators=(",", ":"), default=str).encode()), "application/gzip")
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(cols)
    for r in review["releases"]["data"]:
        w.writerow(["" if v is None else v for v in r])
    store.put_raw(f"fund/u/{sub}/export/releases.csv.gz", gzip.compress(buf.getvalue().encode()), "application/gzip")
    store.put_json(f"fund/u/{sub}/export/meta", {"studyAt": res.get("at"), "at": iso(now_ny()), "n": len(rows)})


def export_link(sub, kind):
    """The download link of the review file or the CSV; None while it's being written (a job builds it)."""
    res = study(sub)
    if not res:
        raise BadRequest("Run the event study first.")
    meta = store.get_json(f"fund/u/{sub}/export/meta") or {}
    if meta.get("studyAt") != res.get("at"):
        return None
    day = (res.get("at") or "")[:10]
    key, name = {"review": ("review.json.gz", f"earnings_study_review_{res.get('bench')}_{day}.json.gz"),
                 "csv": ("releases.csv.gz", f"earnings_study_releases_{res.get('bench')}_{day}.csv.gz")}[kind]
    u = store.url(f"fund/u/{sub}/export/{key}", name)
    if u:
        return {"url": u, "name": name}
    import base64
    return {"name": name, "data": base64.b64encode(store.get_raw(f"fund/u/{sub}/export/{key}") or b"").decode()}


def run_study(sub, opts, prog=None, deadline=None):
    import model
    rows, bench, skipped = build_rows(sub, opts, prog, deadline)
    if prog:
        prog(f"Statistics over {len(rows)} releases…")
    if deadline and time.time() > deadline - 240:     # leave the model a full run: rows and prices are cached by now
        raise TimeoutError("out of time")
    agg = aggregate(rows)
    if prog:
        prog(f"Walk-forward model over {len(rows)} releases…")
    mdl = model.evaluate(rows, prog)
    store.put_json(f"fund/u/{sub}/study_rows", {"rows": rows})
    res = {"at": iso(now_ny()), "bench": bench, "n": len(rows), "tickers": len({r["sym"] for r in rows}),
           "from": rows[0]["date"] if rows else None, "to": rows[-1]["date"] if rows else None,
           "sources": {k: sum(1 for r in rows if r.get("src") == k) for k in ("csv", "fmp", "sec")},
           "withEst": sum(1 for r in rows if r.get("epsSurprise") is not None),
           "outcomes": OUTCOMES, "skipped": skipped[:60], "nSkipped": len(skipped), **agg, "model": mdl,
           "rows": rows[-300:][::-1], "rowPages": max(1, math.ceil(len(rows) / ROW_PAGE))}
    res["withTx"] = sum(1 for r in rows if r.get("txTone") is not None)
    res["txStatus"] = TX_NOTE[0]
    res["withGrades"] = sum(1 for r in rows if r.get("gradeN90"))
    store.put_json(f"fund/u/{sub}/study", res)
    try:
        build_exports(sub, res, rows)
    except Exception as e:                            # the study stands without its downloads (rebuilt on request)
        print("exports failed", e)
    return res


def study(sub):
    return store.get_json(f"fund/u/{sub}/study")


def study_rows(sub, page=0):
    r = store.get_json(f"fund/u/{sub}/study_rows", max_age=600) or {"rows": []}
    rows = r["rows"]
    return {"page": page, "pages": max(1, math.ceil(len(rows) / ROW_PAGE)), "rows": rows[page * ROW_PAGE:(page + 1) * ROW_PAGE]}


def load_universe(sub, source, years=10, removed=True):
    """The study universe: the S&P 500 from FMP (with the companies removed in the period: less survivorship bias)."""
    import fmp
    if source != "sp500":
        raise BadRequest("Unknown universe.")
    r = fmp.sp500(sub, years)
    syms = r["current"] + (r["removed"] if removed else [])
    ok = []
    for x in syms:
        try:
            x = check_sym(x)
        except BadRequest:
            continue
        if x not in ok:
            ok.append(x)
    if not ok:
        raise BadRequest("FMP returned no S&P 500 list for this plan: paste the tickers instead.")
    cur = save_settings(sub, {"universe": ok[:MAX_UNIVERSE], "uniName": f"S&P 500 ({len(r['current'])} current" + (f" + {len(r['removed'])} removed" if removed and r["removed"] else "") + ")"})
    return {**cur, "note": r["note"]}


def company_view(sub, sym, bench=None):
    """One company: quarters with metrics, and each release's reaction (prices fetched on demand)."""
    c = company(sub, sym)
    out = {k: v for k, v in c.items()}
    bench = bench or settings(sub)["bench"]
    try:
        rel = c.get("releases") or []
        need = (_d(rel[0]["date"]) - timedelta(days=400)).isoformat() if rel else PX_FROM
        bars, bb = prices(sub, c["sym"], need), prices(sub, bench, need)
        rs = []
        for r in rel:
            e = {"sym": c["sym"], "date": r["date"], "timing": timing_of(r["time"]), "time": r["time"]}
            x = reaction(e, bars, bb)
            q = _quarter_for(c, r["date"])
            rs.append({**e, **(x or {}), "qEnd": q["end"] if q else None,
                       **({k: q.get(k) for k in ("revYoY", "revAccel", "epsYoY", "quality")} if q else {})})
        items = (store.get_json("fund/txi/" + c["sym"]) or {}).get("items") or []
        for x in rs:
            _tx_fields(x, items)
        out["reactions"] = rs[::-1]
        out["bench"] = bench
    except Exception as e:                             # Alpaca not connected: the fundamentals alone
        out["reactionsError"] = str(e)
    return out


# ---------------------------------------------------------------- projection for the next release

HORIZONS = (("r1", "1 day"), ("r5", "5 days"), ("r20", "1 month"), ("r40", "2 months"))
# known before a release: the last reported quarter (prev*), consensus growth, price / volume into the release, own history
NN_SIGNALS = ("prevRevYoY", "prevRevAccel", "prevEpsYoY", "estEpsG", "pre5ab", "pre21ab", "pre63ab", "offHigh", "vol20",
              "relVol5", "cmf20")


def _pctl(xs, p):
    s = sorted(xs)
    if not s:
        return None
    k = (len(s) - 1) * p
    f = int(k)
    return s[f] + (s[min(f + 1, len(s) - 1)] - s[f]) * (k - f)


def _dist(xs):
    xs = [x for x in xs if x is not None and math.isfinite(x)]
    if not xs:
        return {"n": 0}
    st = _stats(xs)
    return {**st, "p10": _pctl(xs, 0.1), "p25": _pctl(xs, 0.25), "p75": _pctl(xs, 0.75), "p90": _pctl(xs, 0.9),
            "absMed": _pctl([abs(x) for x in xs], 0.5)}


def implied_move(sub, und, day0, spot=None):
    """The move the options price in through the release: the at-the-money straddle (call + put mid) of the first
    expiry on or after the reaction day, as a share of the stock price. Includes the ordinary days to that expiry."""
    import alpaca
    import alpopt
    import urllib.parse
    c = alpaca.creds(sub)
    spot = spot or alpopt._spot(c, und.replace("-", "."))
    if not spot:
        return None
    hi = (_d(day0) + timedelta(days=10)).isoformat()
    by = {}                                            # expiry -> strike -> {"C": mid, "P": mid}
    for typ in ("call", "put"):
        tok = None
        for _ in range(5):
            q = {"feed": "indicative", "type": typ, "expiration_date_gte": day0, "expiration_date_lte": hi,
                 "strike_price_gte": round(spot * 0.85, 2), "strike_price_lte": round(spot * 1.15, 2), "limit": 1000, "page_token": tok}
            r = alpopt._req(c, "GET", alpopt.DATA + f"options/snapshots/{und.replace('-', '.')}?" + urllib.parse.urlencode({k: v for k, v in q.items() if v}))
            for osym, x in (r.get("snapshots") or {}).items():
                qt = x.get("latestQuote") or {}
                bid, ask = float(qt.get("bp") or 0), float(qt.get("ap") or 0)
                if ask <= 0:
                    continue
                exp = "20" + osym[-15:-13] + "-" + osym[-13:-11] + "-" + osym[-11:-9]
                k = float(osym[-8:]) / 1000
                by.setdefault(exp, {}).setdefault(k, {})[osym[-9]] = (bid + ask) / 2
            tok = r.get("next_page_token")
            if not tok:
                break
    for exp in sorted(by):
        both = [(abs(k - spot), k, v) for k, v in by[exp].items() if "C" in v and "P" in v]
        if both:
            _, k, v = min(both)
            return {"expiry": exp, "strike": k, "call": round(v["C"], 2), "put": round(v["P"], 2), "spot": spot,
                    "move": (v["C"] + v["P"]) / spot}
    return None


def project(sub, sym, k_nn=None):
    """The next release: (1) the walk-forward model's expectation for this setup, with its tested accuracy; (2) what the
    most similar past releases did; (3) this company's own history. All from signals known before the release."""
    import fmp
    import model
    c = company(sub, sym)
    st = settings(sub)
    bench_sym = st["bench"]
    rel = c.get("releases") or []
    frows = []
    if fmp.status(sub)["connected"]:
        try:
            frows = fmp.earnings(sub, c["sym"])
        except Exception as e:
            print("fmp", e)
    need = (_d(min([rel[0]["date"]] if rel else [PX_FROM])) - timedelta(days=400)).isoformat()
    bars, bench = prices(sub, c["sym"], need), prices(sub, bench_sym, need)
    bmap = {b[0]: b[4] for b in bench}
    own = []
    for r in rel:
        e = {"sym": c["sym"], "date": r["date"], "timing": timing_of(r["time"])}
        x = reaction(e, bars, bench, bmap)
        if x:
            own.append({**e, **x})
    sd = study(sub) or {}
    ref = {}
    if sd.get("bench") == bench_sym:
        for r in (store.get_json(f"fund/u/{sub}/study_rows") or {}).get("rows") or []:
            ref[(r["sym"], r["date"])] = r
    for r in own:
        ref.setdefault((r["sym"], r["date"]), r)
    ref = list(ref.values())
    # this company going into its next release
    qs = c.get("quarters") or []
    q = qs[-1] if qs else {}
    now = {"prevRevYoY": q.get("revYoY"), "prevRevAccel": q.get("revAccel"), "prevEpsYoY": q.get("epsYoY"), "prevAccruals": q.get("accruals"),
           "accruals": q.get("accruals")}
    now.update(pre_signals(bars, bmap, len(bars) - 1))
    now.update(volume_patterns(now))
    now["asOf"], now["close"] = bars[-1][0], bars[-1][4]
    ctx = market_ctx(bench[-260:]).get(bars[-1][0])
    if ctx:
        now.update(ctx)
        now["regime"] = regime_of(ctx)
    past = [r for r in own if r.get("r1ab") is not None][-12:]
    if past:
        now.update({"ownAbs": sum(abs(r["r1ab"]) for r in past) / len(past), "ownMean": sum(r["r1ab"] for r in past) / (len(past) + 4),
                    "prevR1ab": past[-1]["r1ab"], "ownN": len(past)})
    nxt = None
    up = [r for r in frows if r["date"] >= now_ny().date().isoformat() and r["epsAct"] is None]
    if up:                                             # FMP's calendar: the scheduled date and the consensus
        u = up[0]
        nxt = {"date": u["date"], "timing": u["timing"] or (timing_of(rel[-1]["time"]) if rel else "unknown"), "estimated": False,
               "epsEst": u["epsEst"], "revEst": u["revEst"], "source": "FMP"}
        done = [r for r in frows if r["epsAct"] is not None and r["date"] < (_d(u["date"]) - timedelta(days=300)).isoformat()]
        if done and (_d(u["date"]) - _d(done[-1]["date"])).days <= 430:
            now["estEpsG"], now["estRevG"] = _g(u["epsEst"], done[-1]["epsAct"]), _g(u["revEst"], done[-1]["revAct"])
    elif rel:
        gaps = sorted((_d(b["date"]) - _d(a["date"])).days for a, b in zip(rel, rel[1:]) if 60 <= (_d(b["date"]) - _d(a["date"])).days <= 130)
        step = gaps[len(gaps) // 2] if gaps else 91
        nd = _d(rel[-1]["date"]) + timedelta(days=step)
        while nd < now_ny().date():
            nd += timedelta(days=step)
        nxt = {"date": nd.isoformat(), "timing": timing_of(rel[-1]["time"]), "estimated": True, "last": rel[-1]["date"], "spacing": step}
    now["amc"] = 1.0 if nxt and nxt.get("timing") == "amc" else 0.0
    pt = None
    if frows:                                          # FMP connected: analysts and the last call
        try:
            _grade_fields(now, fmp.grades(sub, c["sym"]), (_d(now["asOf"]) + timedelta(days=1)).isoformat())
        except Exception as e:
            print("grades", e)
        try:
            items = tx_index(sub, c["sym"], (now_ny().date() - timedelta(days=400)).isoformat(), True)
        except Exception as e:
            items = (store.get_json("fund/txi/" + c["sym"]) or {}).get("items") or []
            print("tx", e)
        if items:
            last = items[-1]
            now.update({"prevTxTone": last.get("tone"), "prevTxGuide": last.get("guide"), "prevTxUncert": last.get("uncert"),
                        "lastCall": {k: last.get(k) for k in ("date", "tone", "toneQA", "uncert", "guide", "guideDir")}})
        try:
            pt = fmp.price_target(sub, c["sym"])
            if pt and pt.get("targetConsensus"):
                pt["upside"] = pt["targetConsensus"] / now["close"] - 1
        except Exception as e:
            print("pt", e)
    # nearest neighbours on standardised pre-release signals
    use = [k for k in NN_SIGNALS if now.get(k) is not None and sum(1 for r in ref if r.get(k) is not None) >= 10]
    neigh = []
    if use:
        sc = {}
        for k in use:
            xs = [r[k] for r in ref if r.get(k) is not None]
            m = sum(xs) / len(xs)
            sc[k] = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)) or 1
        scored = []
        for r in ref:
            have = [k for k in use if r.get(k) is not None]
            if len(have) < max(2, len(use) // 2):
                continue
            scored.append((math.sqrt(sum(((r[k] - now[k]) / sc[k]) ** 2 for k in have) / len(have)), r))
        scored.sort(key=lambda t: t[0])
        neigh = scored[:k_nn or max(10, min(50, len(scored) // 20 or 10))]
    hz = []
    for key, label in HORIZONS:
        hz.append({"key": key, "label": label,
                   "similar": {"raw": _dist([r.get(key) for _, r in neigh]), "ab": _dist([r.get(key + "ab") for _, r in neigh])},
                   "own": {"raw": _dist([r.get(key) for r in own]), "ab": _dist([r.get(key + "ab") for r in own])},
                   "all": {"raw": _dist([r.get(key) for r in ref]), "ab": _dist([r.get(key + "ab") for r in ref])},
                   "volRange": now.get("vol20") * math.sqrt(int(key[1:])) if now.get("vol20") else None})
    keep = ("sym", "date", "timing", "prevRevYoY", "estEpsG", "pre21ab", "relVol5", "cmf20", "r1ab", "r5ab", "r20ab", "r40ab")
    implied = None
    if nxt:
        d0 = _d(nxt["date"])
        if nxt.get("timing") == "amc":
            d0 += timedelta(days=1)
        while d0.weekday() >= 5:
            d0 += timedelta(days=1)
        try:
            implied = implied_move(sub, c["sym"], d0.isoformat(), now.get("close"))
            if implied:
                implied["reactionDay"] = d0.isoformat()
        except Exception as e:
            implied = {"error": str(e)[:160]}
    return {"sym": c["sym"], "name": c.get("name"), "bench": bench_sym, "next": nxt, "now": now, "signals": use, "priceTarget": pt,
            "implied": implied,
            "refN": len(ref), "ownN": len(own), "studyAt": sd.get("at"), "horizons": hz,
            "model": model.apply(sd.get("model"), now),
            "neighbours": [{"dist": round(dd, 3), **{k: r.get(k) for k in keep}} for dd, r in neigh[:25]]}


# ---------------------------------------------------------------- today's list: releases reacting at today's open

def _bday(d, step):
    d = d + timedelta(days=step)
    while d.weekday() >= 5:
        d += timedelta(days=step)
    return d


def _fmp_growth(frows, r):
    """Year-over-year growth of the release's actuals from FMP's own history (the 10-Q isn't filed yet on release day)."""
    done = [x for x in frows if x["date"] < r["date"] and (x["epsAct"] is not None or x["revAct"] is not None)]
    out = {}

    def ago(days_lo, days_hi, ref_date):
        for x in reversed(done):
            n = (_d(ref_date) - _d(x["date"])).days
            if days_lo <= n <= days_hi:
                return x
        return None
    ya = ago(300, 430, r["date"])
    if ya:
        out["revYoY"], out["epsYoY"] = _g(r.get("revAct"), ya["revAct"]), _g(r.get("epsAct"), ya["epsAct"])
    prev = done[-1] if done else None
    if prev:
        out["revQoQ"] = _g(r.get("revAct"), prev["revAct"])
        pya = ago(300, 430, prev["date"]) if prev else None
        if pya and out.get("revYoY") is not None:
            py = _g(prev["revAct"], pya["revAct"])
            if py is not None:
                out["revAccel"] = out["revYoY"] - py
        if pya and out.get("epsYoY") is not None:
            pe = _g(prev["epsAct"], pya["epsAct"])
            if pe is not None:
                out["epsAccel"] = out["epsYoY"] - pe
    return out


def today_list(sub, day, prog=None, deadline=None):
    """The study universe's releases whose first reaction is `day` (after-close releases of the previous business day,
    before-open releases of the day): surprises, the call, the gap at the open, the post-release score and its flags, the
    'after the call' model's expectation. Stored per day."""
    import alpaca
    import alpopt
    import fmp
    import model
    st = settings(sub)
    uni = set(study_tickers(st))
    if not uni:
        raise BadRequest("Set the study universe first (Event study → Load the S&P 500).")
    sd = study(sub) or {}
    mdl = sd.get("model") or {}
    cuts = mdl.get("scoreCuts")
    D = _d(day)
    prev = _bday(D, -1)
    cal = [r for r in fmp.calendar(sub, prev.isoformat(), day) if r["symbol"].replace(".", "-") in uni or r["symbol"] in uni]
    bench_sym = st["bench"]
    start = (D - timedelta(days=420)).isoformat()
    bench = alpaca.daily_range(sub, bench_sym, start)
    bmap, bopen = {b[0]: b[4] for b in bench}, {b[0]: b[1] for b in bench}
    mctx = market_ctx(bench[-300:])
    pdays = [d for d in sorted(mctx) if d < day]
    market = {**mctx[pdays[-1]], "asOf": pdays[-1], "regime": regime_of(mctx[pdays[-1]])} if pdays else None
    c_alp = alpaca.creds(sub)
    rows, notes = [], []
    seen = set()
    for n, r in enumerate(cal):
        if deadline and time.time() > deadline:
            notes.append("Out of time: the list is partial.")
            break
        s = check_sym(r["symbol"].replace(".", "-"))
        if s in seen:
            continue
        if prog:
            prog(f"{n + 1}/{len(cal)}: {s}")
        try:
            frows = fmp.earnings(sub, s, max_age_h=0)
            fr = next((x for x in frows if x["date"] == r["date"]), None) or {**r, "timing": r.get("timing")}
            timing = fr.get("timing") or r.get("timing") or "unknown"
            d0 = _bday(_d(r["date"]), 1) if timing == "amc" else _d(r["date"])
            if d0.weekday() >= 5:
                d0 = _bday(d0, 1)
            if d0 != D:
                continue
            seen.add(s)
            e = {"sym": s, "date": r["date"], "timing": timing, "epsEst": fr.get("epsEst"), "epsAct": fr.get("epsAct"),
                 "revEst": fr.get("revEst"), "revAct": fr.get("revAct")}
            _surprises(e)
            if e.get("epsAct") is None and e.get("revAct") is None:
                notes.append(f"{s}: no actuals yet")
            e.update(_fmp_growth(frows, e))
            bars = alpaca.daily_range(sub, s, start)
            ix = [i for i, b in enumerate(bars) if b[0] < day]
            if not ix:
                continue
            base = ix[-1]
            e.update(pre_signals(bars, bmap, base))
            e.update(volume_patterns(e))
            if mctx.get(bars[base][0]):
                e.update(mctx[bars[base][0]])
            op, indicated = None, False
            if base + 1 < len(bars) and bars[base + 1][0] == day:
                op = bars[base + 1][1]
                e["last"] = bars[base + 1][4]
            else:
                op, indicated = alpopt._spot(c_alp, s.replace("-", ".")), True      # before the open: the latest trade
            if op:
                e["gap"] = op / bars[base][4] - 1
                e["open"], e["indicated"] = op, indicated
                if not indicated and e.get("last") and bopen.get(day) and bmap.get(day):
                    e["o2cSoFar"] = e["last"] / op - 1 - (bmap[day] / bopen[day] - 1)
            try:
                items = tx_index(sub, s, (D - timedelta(days=400)).isoformat(), True, force=True)
            except Exception as ex:
                items = (store.get_json("fund/txi/" + s) or {}).get("items") or []
                notes.append(f"{s} call: {str(ex)[:80]}")
            _tx_fields(e, [i for i in items if i["date"] <= day])
            e["callBeforeOpen"] = timing == "amc"
            e["setup"] = setup_of(e)
            if mdl.get("ref"):
                snap = model.rank_snapshot(e, mdl["ref"])
                e["score"] = model.post_score(snap)
                if cuts and e["score"] is not None:
                    e["scoreBand"] = "top" if e["score"] >= cuts["p80"] else "bottom" if e["score"] <= cuts["p20"] else "middle"
                for p_ in model.apply(mdl, e, "call") or []:
                    e["pred_" + p_["key"]] = p_["pred"]
            g = e.get("gap")
            if e.get("scoreBand") == "bottom" and g is not None and g > 0.02:
                e["flag"] = "Bad news + gap up"
            elif e.get("scoreBand") == "top" and g is not None and g < -0.02:
                e["flag"] = "Good news + gap down"
            rows.append(e)
        except Exception as ex:
            notes.append(f"{s}: {str(ex)[:100]}")
    rows.sort(key=lambda x: (x.get("flag") is None, -(abs(x.get("gap") or 0))))
    calls = {t["key"]: {k: t.get(k) for k in ("verdict", "hit", "hitBase", "icQ", "spread", "n")} for t in mdl.get("targets") or [] if t.get("kind") == "call"}
    out = {"day": day, "at": iso(now_ny()), "bench": bench_sym, "rows": rows, "notes": notes[:30], "cuts": cuts, "models": calls, "market": market,
           "studyAt": sd.get("at")}
    store.put_json(f"fund/u/{sub}/today/{day}", out)
    return out


def today_view(sub, day):
    return store.get_json(f"fund/u/{sub}/today/{day}") or {"day": day, "rows": None}
