"""US market holidays and early closes: what SPY (or QQQ / IWM / DIA) does before and after them.

Daily bars from FMP (dividend-adjusted, since 1993) or, without FMP, from Alpaca (since 2016). A holiday is a weekday
with no session (found in the data, so special closures such as 9/11 or a national day of mourning are included),
named by the NYSE rules. Early closes (13:00) are the day after Thanksgiving and Jul 3 / Dec 24 when they fall
Monday-Thursday.

For each event: the N sessions before (N = 5 or 10), the last session before, the gap at the reopen, and 1 / 3 / 5 / 10
sessions after (from the last close before the holiday, or from the half day's close). Compared with every day of the
same data (the baseline), and split by whether the N sessions before were down or up."""
import math
import time
from datetime import date, timedelta

import store
from util import BadRequest, iso, now_ny

SYMS = ("SPY", "QQQ", "IWM", "DIA")
POST = (1, 3, 5, 10)
HOLIDAY_ORDER = ["New Year's Day", "Martin Luther King Jr. Day", "Presidents' Day", "Good Friday", "Memorial Day", "Juneteenth",
                 "Independence Day", "Labor Day", "Thanksgiving", "Christmas", "Special closure"]


# ---------------------------------------------------------------- the calendar

def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mo = (h + l - 7 * m + 114) // 31
    return date(y, mo, (h + l - 7 * m + 114) % 31 + 1)


def _nth(y, m, wd, n):
    """The n-th weekday wd (0 = Monday) of the month; n = -1: the last one."""
    if n > 0:
        d = date(y, m, 1)
        d += timedelta(days=(wd - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _observed(d):
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def holiday_name(d):
    y = d.year
    if d == _observed(date(y, 1, 1)) or (d.month == 1 and d.day == 2 and d.weekday() == 0):
        return "New Year's Day"
    if y >= 1998 and d == _nth(y, 1, 0, 3):
        return "Martin Luther King Jr. Day"
    if d == _nth(y, 2, 0, 3):
        return "Presidents' Day"
    if d == _easter(y) - timedelta(days=2):
        return "Good Friday"
    if d == _nth(y, 5, 0, -1):
        return "Memorial Day"
    if y >= 2022 and d == _observed(date(y, 6, 19)):
        return "Juneteenth"
    if d == _observed(date(y, 7, 4)):
        return "Independence Day"
    if d == _nth(y, 9, 0, 1):
        return "Labor Day"
    if d == _nth(y, 11, 3, 4):
        return "Thanksgiving"
    if d == _observed(date(y, 12, 25)):
        return "Christmas"
    return "Special closure"


def early_close_name(d):
    if d.month == 11 and d.weekday() == 4 and 23 <= d.day <= 29:
        return "Day after Thanksgiving"
    if d.weekday() <= 3 and (d.month, d.day) == (7, 3):
        return "July 3"
    if d.weekday() <= 3 and (d.month, d.day) == (12, 24):
        return "Christmas Eve"
    return None


# ---------------------------------------------------------------- Jewish holidays (the market is open on them)
# Hebrew calendar arithmetic (Dershowitz & Reingold, "Calendrical Calculations"): Rosh Hashanah of each Hebrew year,
# then the other holidays by their fixed distance from it.
_HEBREW_EPOCH = -1373427                                   # R.D. of 1 Tishri AM 1


def _elapsed(hy):
    months = (235 * hy - 234) // 19
    parts = 12084 + 13753 * months
    day = months * 29 + parts // 25920
    return day + 1 if (3 * (day + 1)) % 7 < 3 else day


def _correction(hy):
    ny0, ny1, ny2 = _elapsed(hy - 1), _elapsed(hy), _elapsed(hy + 1)
    return 2 if ny2 - ny1 == 356 else 1 if ny1 - ny0 == 382 else 0


def rosh_hashanah(gy):
    """1 Tishri falling in the autumn of Gregorian year gy."""
    hy = gy + 3761
    return date.fromordinal(_HEBREW_EPOCH + _elapsed(hy) + _correction(hy))


def jewish_holidays(gy):
    """{name: date} of the holidays in Gregorian year gy (the first day, the evening before is when it begins)."""
    rh = rosh_hashanah(gy)
    pesach = rosh_hashanah(gy) - timedelta(days=163)      # 15 Nisan is always 163 days before the next 1 Tishri
    # 25 Kislev: Tishri (30) + Heshvan (29, or 30 in a "complete" year of 355 / 385 days) + 24
    kislev25 = rh + timedelta(days=30 + 29 + (1 if (rosh_hashanah(gy + 1) - rh).days % 10 == 5 else 0) + 24)
    return {"Purim": pesach - timedelta(days=30), "Passover": pesach, "Shavuot": pesach + timedelta(days=50),
            "Rosh Hashanah": rh, "Yom Kippur": rh + timedelta(days=9), "Sukkot": rh + timedelta(days=14), "Hanukkah": kislev25}


JEWISH_ORDER = ["Purim", "Passover", "Shavuot", "Rosh Hashanah", "Yom Kippur", "Sukkot", "Hanukkah"]


# ---------------------------------------------------------------- data

def _daily(sub, sym):
    """[[date, open, high, low, close], ...] oldest first, kept a day."""
    k = f"hol/{sym}"
    c = store.get_json(k) or {}
    if c.get("rows") and c.get("at", "") >= iso(now_ny() - timedelta(hours=20)):
        return c["rows"], c.get("src")
    rows, src = [], None
    try:
        import fmp
        key = fmp._key(sub)
        raw = fmp._get(key, "historical-price-eod/dividend-adjusted", {"symbol": sym, "from": "1993-01-01"}) or []
        raw = raw if isinstance(raw, list) else (raw.get("historical") or [])
        rows = sorted([str(r["date"])[:10], float(r.get("adjOpen") or r.get("open")), float(r.get("adjHigh") or r.get("high")),
                       float(r.get("adjLow") or r.get("low")), float(r.get("adjClose") or r.get("close"))] for r in raw if r.get("date") and (r.get("adjClose") or r.get("close")))
        src = "FMP (dividend-adjusted)"
    except BadRequest:
        rows = []
    if len(rows) < 300:
        import alpaca
        cr, feed = alpaca.creds(sub), alpaca.status(sub)["feed"]
        out, token = [], None
        for _ in range(20):
            r = alpaca._get(cr, f"stocks/{sym}/bars", {"timeframe": "1Day", "start": "2016-01-01T00:00:00Z", "limit": 10000, "adjustment": "all",
                                                      "feed": feed, "sort": "asc", "page_token": token})
            out += r.get("bars") or []
            token = r.get("next_page_token")
            if not token:
                break
        rows = [[b["t"][:10], b["o"], b["h"], b["l"], b["c"]] for b in out]
        src = "Alpaca (adjusted, since 2016)"
    if len(rows) < 300:
        raise BadRequest(f"Not enough daily data for {sym}: connect FMP (Data page) or Alpaca.")
    store.put_json(k, {"rows": rows, "src": src, "at": iso(now_ny())})
    return rows, src


# ---------------------------------------------------------------- the study

def _st(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    n, m = len(xs), sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
    return {"n": n, "avg": round(m, 5), "med": round(sorted(xs)[n // 2], 5), "up": round(sum(1 for x in xs if x > 0) / n, 3),
            "t": round(m / (sd / math.sqrt(n)), 2) if sd > 0 and n > 2 else None}


def _ret(c, a, b):
    return c[b] / c[a] - 1 if 0 <= a < len(c) and 0 <= b < len(c) and c[a] else None


def study(sub, sym="SPY", n=5, start_year=1993):
    sym = sym if sym in SYMS else "SPY"
    n = 10 if int(n) == 10 else 5
    rows, src = _daily(sub, sym)
    rows = [r for r in rows if r[0] >= f"{int(start_year)}-01-01"] or rows
    ds = [date.fromisoformat(r[0]) for r in rows]
    o = [r[1] for r in rows]
    c = [r[4] for r in rows]
    idx = {d: i for i, d in enumerate(ds)}

    # holidays: weekdays without a session (consecutive missing weekdays are one event, named by its first day)
    import bisect
    events = []
    d, last, prev_missing = ds[0], ds[-1], None
    while d < last:
        d += timedelta(days=1)
        if d.weekday() >= 5:
            continue
        if d in idx:
            prev_missing = None
            continue
        if prev_missing is None:                           # the first day of a closure
            events.append({"kind": "holiday", "name": holiday_name(d), "date": d.isoformat(), "i": bisect.bisect_left(ds, d) - 1})
        prev_missing = d
    # early closes (13:00): the half day itself is the event
    for i, dd in enumerate(ds):
        nm = early_close_name(dd)
        if nm:
            events.append({"kind": "early", "name": nm, "date": dd.isoformat(), "i": i})

    # Jewish holidays: the market is open; the event is the first session on or after the holiday's first day
    for gy in range(ds[0].year, ds[-1].year + 1):
        for nm, hd in jewish_holidays(gy).items():
            i = bisect.bisect_left(ds, hd)
            if i < len(ds) and (ds[i] - hd).days <= 3:
                events.append({"kind": "jewish", "name": nm, "date": hd.isoformat(), "i": i})

    out = []
    for e in events:
        i = e["i"]
        if i is None or i < n + 1:
            continue
        if e["kind"] == "holiday":                          # i = the last session before the holiday
            pre = _ret(c, i - n, i)
            day0 = _ret(c, i - 1, i)                        # the session before the holiday
            gap = o[i + 1] / c[i] - 1 if i + 1 < len(c) else None
        else:                                               # i = the half day / the Jewish holiday session
            pre = _ret(c, i - 1 - n, i - 1)
            day0 = _ret(c, i - 1, i)                        # the half day itself
            gap = o[i + 1] / c[i] - 1 if i + 1 < len(c) else None
        post = {str(k): _ret(c, i, i + k) for k in POST}
        out.append({**{k: v for k, v in e.items() if k != "i"}, "pre": pre, "day0": day0, "gap": gap, "post": post, "last": ds[i].isoformat()})

    def group(evs):
        return {"n": len(evs), "pre": _st([e["pre"] for e in evs]), "day0": _st([e["day0"] for e in evs]), "gap": _st([e["gap"] for e in evs]),
                **{f"p{k}": _st([e["post"][str(k)] for e in evs]) for k in POST}}

    hol = [e for e in out if e["kind"] == "holiday"]
    early = [e for e in out if e["kind"] == "early"]
    by_name = [{"name": nm, **group([e for e in hol if e["name"] == nm])} for nm in HOLIDAY_ORDER if any(e["name"] == nm for e in hol)]
    jew = [e for e in out if e["kind"] == "jewish"]
    by_jewish = [{"name": nm, **group([e for e in jew if e["name"] == nm])} for nm in JEWISH_ORDER if any(e["name"] == nm for e in jew)]
    # "sell Rosh Hashanah, buy Yom Kippur": the close before Rosh Hashanah to the close of Yom Kippur, then Yom Kippur onward
    rhyk, ykon = [], []
    for gy in range(ds[0].year, ds[-1].year + 1):
        jh = jewish_holidays(gy)
        a, b = bisect.bisect_left(ds, jh["Rosh Hashanah"]) - 1, bisect.bisect_left(ds, jh["Yom Kippur"])
        if a >= 0 and b < len(ds) and (ds[b] - jh["Yom Kippur"]).days <= 3:
            rhyk.append(_ret(c, a, b))
            ykon.append(_ret(c, b, b + 10))
    by_early = [{"name": nm, **group([e for e in early if e["name"] == nm])} for nm in ("Day after Thanksgiving", "July 3", "Christmas Eve") if any(e["name"] == nm for e in early)]

    # the baseline: every day of the same data, and split by the N sessions before
    base_all = {f"p{k}": _st([_ret(c, t, t + k) for t in range(n, len(c) - k)]) for k in POST}
    base_dn = {f"p{k}": _st([_ret(c, t, t + k) for t in range(n, len(c) - k) if _ret(c, t - n, t) < 0]) for k in POST}
    base_up = {f"p{k}": _st([_ret(c, t, t + k) for t in range(n, len(c) - k) if _ret(c, t - n, t) > 0]) for k in POST}
    base_day = _st([_ret(c, t - 1, t) for t in range(1, len(c))])

    def split(evs):
        return {"down": group([e for e in evs if e["pre"] is not None and e["pre"] < 0]), "up": group([e for e in evs if e["pre"] is not None and e["pre"] > 0])}

    return {"sym": sym, "n": n, "src": src, "from": rows[0][0], "to": rows[-1][0], "days": len(rows), "at": iso(now_ny()),
            "holidays": group(hol), "early": group(early), "jewish": group(jew), "byName": by_name, "byEarly": by_early, "byJewish": by_jewish,
            "rhyk": _st(rhyk), "ykon": _st(ykon), "base10": base_all["p10"],
            "split": {"holidays": split(hol), "early": split(early), "jewish": split(jew)},
            "base": {"all": base_all, "down": base_dn, "up": base_up, "day": base_day},
            "events": sorted(out, key=lambda e: e["date"], reverse=True)[:120]}
