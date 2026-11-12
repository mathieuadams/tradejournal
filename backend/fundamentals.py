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

Acceptance times in EDGAR's submissions file are Eastern time (the trailing Z is not UTC)."""
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
from util import BadRequest, Unavailable, iso, now_ny

SK_WATCH = "FUND#watch"
SEC_WWW = "https://www.sec.gov/"
SEC_DATA = "https://data.sec.gov/"
PX_FROM = "2015-01-01"
BENCHES = ("SPY", "QQQ", "IWM", "DIA")
MAX_WATCH = 150

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


def lookup(sub, sym):
    mp = cik_map(sub)
    hit = mp.get(sym) or mp.get(sym.replace(".", "-")) or mp.get(sym.replace("-", "."))
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
            t = acc[11:16] if len(acc) >= 16 else ""
            if acc[:10] and acc[:10] < fd:             # accepted after 17:30: dated the next business day, released that evening
                fd = acc[:10]
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
    pages = []
    for f in ((subs.get("filings") or {}).get("files") or [])[:3]:      # older filings (heavy filers fill "recent" fast)
        try:
            pages.append(_http(SEC_DATA + "submissions/" + f["name"], ua))
        except Exception as e:
            print("sec page failed", sym, e)
    qs = metrics(build_quarters(facts))
    c = {"sym": sym, "cik": cik, "name": subs.get("name") or name, "at": iso(now_ny()), "fiscalYearEnd": subs.get("fiscalYearEnd"),
         "quarters": qs[-28:], "releases": earnings_filings(subs, pages)}
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
            "bench": it.get("bench", "SPY")}


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
    if "useSec" in b:
        cur["useSec"] = bool(b["useSec"])
    if "years" in b:
        try:
            cur["years"] = max(1, min(10, int(b["years"])))
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
    bars = alpaca.daily_range(sub, sym, min(need_from, PX_FROM))
    if not bars:
        raise BadRequest(f"Alpaca returned no daily bars for {sym}.")
    store.put_json(key, {"at": iso(now_ny()), "from": min(need_from, PX_FROM), "bars": bars})
    return bars


# ---------------------------------------------------------------- one release: signals and reaction

WIN = (("r1", 1), ("r5", 5), ("r20", 20), ("r40", 40), ("r60", 60))     # sessions after the base close; 40 ≈ 2 months


def _idx_on_or_after(dates, d):
    import bisect
    i = bisect.bisect_left(dates, d)
    return i if i < len(dates) else None


def reaction(ev, bars, bench):
    """Price reaction around one release. Base = the last close before the market could react (the release day's close
    after an after-close release, the previous close otherwise); day 0 = the first session that could react."""
    dates = [b[0] for b in bars]
    i = _idx_on_or_after(dates, ev["date"])
    if i is None:
        return None
    if ev.get("timing") == "amc":
        base = i if dates[i] == ev["date"] else i - 1
    else:
        base = i - 1
    d0 = base + 1
    if base < 21 or d0 >= len(bars):
        return None
    c = lambda k: bars[k][4]
    bd = {b[0]: b[4] for b in bench}

    def bret(k0, k1):
        a, b = bd.get(dates[k0]), bd.get(dates[k1])
        return b / a - 1 if a and b else None

    out = {"base": dates[base], "day0": dates[d0], "gap": bars[d0][1] / c(base) - 1}
    for name, n in WIN:
        k = base + n
        if k < len(bars):
            out[name] = c(k) / c(base) - 1
            br = bret(base, k)
            out[name + "ab"] = out[name] - br if br is not None else None
    if out.get("r1") is not None:
        for name, n in (("drift20", 20), ("drift60", 60)):    # after day 0: post-earnings drift
            k = base + n
            if k < len(bars):
                br = bret(d0, k)
                out[name] = c(k) / c(d0) - 1 - (br or 0) if br is not None else None
    rets = [c(k) / c(k - 1) - 1 for k in range(base - 19, base + 1)]
    m = sum(rets) / len(rets)
    vol = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))
    out["vol20"] = vol
    if out.get("r1ab") is not None and vol > 0:
        out["z1"] = out["r1ab"] / vol                  # day-0 abnormal move in daily standard deviations
    run = c(base) / c(base - 20) - 1
    br = bret(base - 20, base)
    out["runup20"] = run - br if br is not None else run
    hi = max(b[2] for b in bars[max(0, base - 251):base + 1])
    out["offHigh"] = c(base) / hi - 1
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

OUTCOMES = [("gap", "Gap"), ("r1ab", "Day 0 vs bench"), ("z1", "Day 0 (σ)"), ("r5ab", "5 days vs bench"),
            ("r20ab", "20 days vs bench"), ("r40ab", "2 months vs bench"), ("drift20", "Drift d1→d20"), ("drift60", "Drift d1→d60")]
NUM_SIGNALS = [("revYoY", "Revenue YoY"), ("revAccel", "Revenue acceleration (YoY change)"), ("revQoQ", "Revenue QoQ"),
               ("rev3y", "Revenue 3Y CAGR"), ("epsYoY", "EPS YoY"), ("epsAccel", "EPS acceleration"),
               ("accruals", "Accrual ratio"), ("cashConv", "Cash conversion"), ("epsSurprise", "EPS surprise %"),
               ("revSurprise", "Revenue surprise %"), ("runup20", "20-day run-up vs bench"), ("offHigh", "Distance from 52w high"),
               ("vol20", "20-day volatility")]
CAT_SIGNALS = [("setup", "Beat / raise"), ("guide", "Guidance"), ("timing", "Release timing"), ("quality", "Earnings quality"),
               ("inflection", "EPS inflection"), ("revDir", "Revenue growth direction")]


def build_rows(sub, opts, prog=None, deadline=None):
    """Every release (imported + SEC 8-K 2.02 dates of the watched tickers) with its signals and reaction."""
    imp = events(sub)["events"]
    st = settings(sub)
    bench_sym = opts.get("bench") or st["bench"]
    since = (now_ny().date() - timedelta(days=365 * int(opts.get("years") or st["years"]))).isoformat()
    evs = {}
    for e in imp:
        evs[(e["sym"], e["date"])] = dict(e)
    syms = sorted({e["sym"] for e in imp} | (set(st["tickers"]) if opts.get("useSec", st["useSec"]) else set()))
    cos, skipped = {}, []
    for n, s in enumerate(syms):
        if deadline and time.time() > deadline:
            raise TimeoutError("out of time")
        if prog and n % 5 == 0:
            prog(f"Fundamentals {n + 1}/{len(syms)}: {s}")
        try:
            cos[s] = company(sub, s, max_age_h=72)
        except Exception as e:
            skipped.append(f"{s}: {e}")
            cos[s] = None
    if opts.get("useSec", st["useSec"]):
        for s in st["tickers"]:
            for r in ((cos.get(s) or {}).get("releases") or []):
                if r["date"] < since:
                    continue
                near = [k for k in evs if k[0] == s and abs((_d(k[1]) - _d(r["date"])).days) <= 3]
                if near:                               # the imported row wins; fill the timing from the 8-K if missing
                    evs[near[0]].setdefault("timing", timing_of(r["time"]))
                    evs[near[0]]["sec"] = True
                else:
                    evs[(s, r["date"])] = {"sym": s, "date": r["date"], "timing": timing_of(r["time"]), "src": "sec", "time": r["time"]}
    for k, e in evs.items():                           # imported rows without a timing: from the 8-K the same day
        if not e.get("timing"):
            rel = [r for r in ((cos.get(e["sym"]) or {}).get("releases") or []) if abs((_d(r["date"]) - _d(e["date"])).days) <= 3]
            e["timing"] = timing_of(rel[0]["time"]) if rel else "unknown"
    first = min((e["date"] for e in evs.values()), default=PX_FROM)
    need = (_d(first) - timedelta(days=400)).isoformat()
    if prog:
        prog(f"Prices: {bench_sym}")
    bench = prices(sub, bench_sym, need)
    px = {}
    rows = []
    for n, (k, e) in enumerate(sorted(evs.items(), key=lambda kv: (kv[0][1], kv[0][0]))):
        s = e["sym"]
        if s not in px:
            if deadline and time.time() > deadline:
                raise TimeoutError("out of time")
            if prog:
                prog(f"Prices {len(px) + 1}: {s}")
            try:
                px[s] = prices(sub, s, need)
            except Exception as ex:
                skipped.append(f"{s} prices: {ex}")
                px[s] = None
        if not px[s]:
            continue
        re_ = reaction(e, px[s], bench)
        if not re_:
            continue
        q = _quarter_for(cos.get(s), e["date"])
        row = {**{k2: v for k2, v in e.items() if k2 != "x"}, **re_}
        if q:
            row["qEnd"] = q["end"]
            for k2 in ("revYoY", "revAccel", "revQoQ", "rev3y", "epsYoY", "epsAccel", "accruals", "cashConv", "quality"):
                row[k2] = q.get(k2)
            fl = q.get("flags") or []
            row["inflection"] = next((f for f in fl if "EPS" in f), "None")
            row["revDir"] = None if q.get("revAccel") is None else "Accelerating" if q["revAccel"] > 0.005 else "Decelerating" if q["revAccel"] < -0.005 else "Flat"
        row["setup"] = setup_of(e)
        if e.get("x"):
            row["x"] = e["x"]
        rows.append(row)
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


def run_study(sub, opts, prog=None, deadline=None):
    rows, bench, skipped = build_rows(sub, opts, prog, deadline)
    if prog:
        prog(f"Statistics over {len(rows)} releases…")
    res = {"at": iso(now_ny()), "bench": bench, "n": len(rows), "tickers": len({r["sym"] for r in rows}),
           "from": rows[0]["date"] if rows else None, "to": rows[-1]["date"] if rows else None,
           "outcomes": OUTCOMES, "skipped": skipped[:40], **aggregate(rows), "rows": rows}
    store.put_json(f"fund/u/{sub}/study", res)
    return res


def study(sub):
    return store.get_json(f"fund/u/{sub}/study")


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
        out["reactions"] = rs[::-1]
        out["bench"] = bench
    except Exception as e:                             # Alpaca not connected: the fundamentals alone
        out["reactionsError"] = str(e)
    return out


# ---------------------------------------------------------------- projection for the next release

HORIZONS = (("r1", "1 day"), ("r5", "5 days"), ("r20", "1 month"), ("r40", "2 months"))
NN_SIGNALS = ("revYoY", "revAccel", "epsYoY", "epsAccel", "accruals", "runup20", "offHigh", "vol20")


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


def _now_signals(bars, bench):
    """The price signals as of the last close (what they will be going into the release if nothing changes)."""
    k = len(bars) - 1
    if k < 21:
        return {}
    c = lambda i: bars[i][4]
    bd = {b[0]: b[4] for b in bench}
    rets = [c(i) / c(i - 1) - 1 for i in range(k - 19, k + 1)]
    m = sum(rets) / len(rets)
    vol = math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1))
    run = c(k) / c(k - 20) - 1
    a, b = bd.get(bars[k - 20][0]), bd.get(bars[k][0])
    return {"asOf": bars[k][0], "close": c(k), "vol20": vol, "runup20": run - (b / a - 1) if a and b else run,
            "offHigh": c(k) / max(x[2] for x in bars[max(0, k - 251):k + 1]) - 1}


def project(sub, sym, k_nn=None):
    """What past releases with signals like this company's current ones did over 1 day, 5 days, 1 and 2 months.

    Reference set: the last event study's releases plus this company's own past releases. Neighbours: the k nearest
    releases on the standardised signals both sides have (revenue / EPS growth and acceleration, accruals, run-up,
    distance from the high, volatility). An empirical distribution, not a model: read N and the spread."""
    c = company(sub, sym)
    st = settings(sub)
    bench_sym = st["bench"]
    rel = c.get("releases") or []
    need = (_d(rel[0]["date"]) - timedelta(days=400)).isoformat() if rel else PX_FROM
    bars, bench = prices(sub, c["sym"], need), prices(sub, bench_sym, need)
    own = []
    for r in rel:
        e = {"sym": c["sym"], "date": r["date"], "timing": timing_of(r["time"])}
        x = reaction(e, bars, bench)
        if not x:
            continue
        q = _quarter_for(c, r["date"])
        own.append({**e, **x, **({k: q.get(k) for k in ("revYoY", "revAccel", "epsYoY", "epsAccel", "accruals")} if q else {})})
    sd = study(sub) or {}
    ref = {(r["sym"], r["date"]): r for r in (sd.get("rows") or []) if sd.get("bench") == bench_sym}
    for r in own:
        ref.setdefault((r["sym"], r["date"]), r)
    ref = list(ref.values())
    # this company now: the latest quarter's fundamentals and today's price signals
    q = (c.get("quarters") or [{}])[-1]
    now = {k: q.get(k) for k in ("revYoY", "revAccel", "epsYoY", "epsAccel", "accruals")}
    now.update(_now_signals(bars, bench))
    gaps = sorted((_d(b["date"]) - _d(a["date"])).days for a, b in zip(rel, rel[1:]) if 60 <= (_d(b["date"]) - _d(a["date"])).days <= 130)
    nxt = None
    if rel:
        step = gaps[len(gaps) // 2] if gaps else 91
        nd = _d(rel[-1]["date"]) + timedelta(days=step)
        while nd < now_ny().date():
            nd += timedelta(days=step)
        nxt = {"date": nd.isoformat(), "timing": timing_of(rel[-1]["time"]), "estimated": True, "last": rel[-1]["date"], "spacing": step}
    # nearest neighbours on standardised signals
    use = [k for k in NN_SIGNALS if now.get(k) is not None and sum(1 for r in ref if r.get(k) is not None) >= 10]
    neigh = []
    if use:
        sc = {}
        for k in use:
            xs = [r[k] for r in ref if r.get(k) is not None]
            m = sum(xs) / len(xs)
            s = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)) or 1
            sc[k] = (m, s)
        scored = []
        for r in ref:
            have = [k for k in use if r.get(k) is not None]
            if len(have) < max(2, len(use) // 2):
                continue
            d2 = sum(((r[k] - now[k]) / sc[k][1]) ** 2 for k in have) / len(have)
            scored.append((math.sqrt(d2), r))
        scored.sort(key=lambda t: t[0])
        kk = k_nn or max(10, min(40, len(scored) // 4))
        neigh = scored[:kk]
    hz = []
    for key, label in HORIZONS:
        hz.append({"key": key, "label": label,
                   "similar": {"raw": _dist([r.get(key) for _, r in neigh]), "ab": _dist([r.get(key + "ab") for _, r in neigh])},
                   "own": {"raw": _dist([r.get(key) for r in own]), "ab": _dist([r.get(key + "ab") for r in own])},
                   "all": {"raw": _dist([r.get(key) for r in ref]), "ab": _dist([r.get(key + "ab") for r in ref])},
                   "volRange": now.get("vol20") * math.sqrt(int(key[1:])) if now.get("vol20") else None})
    keep = ("sym", "date", "timing", "revYoY", "revAccel", "epsYoY", "runup20", "r1ab", "r5ab", "r20ab", "r40ab")
    return {"sym": c["sym"], "name": c.get("name"), "bench": bench_sym, "next": nxt, "now": now, "signals": use,
            "refN": len(ref), "ownN": len(own), "studyAt": sd.get("at"), "horizons": hz,
            "neighbours": [{"dist": round(dd, 3), **{k: r.get(k) for k in keep}} for dd, r in neigh[:25]]}
