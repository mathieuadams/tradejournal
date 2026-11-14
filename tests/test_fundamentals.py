"""Earnings page tests: XBRL quarters, growth / inflection / quality, 8-K timing, CSV import, reactions, the study.
No network (fake SEC EDGAR and Alpaca from dev/fakes.py).

    python tests/test_fundamentals.py"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.path.insert(0, os.path.join(HERE, "..", "dev"))
os.environ.update({"TABLE": "local", "KMS_KEY_ID": "local", "DATA_BUCKET": "local"})

import fakes  # noqa: E402

fakes.install(fake_databento=True, data_dir=tempfile.mkdtemp(), persist=False)

import api  # noqa: E402
import fundamentals as F  # noqa: E402
import jobs  # noqa: E402

SUB = "f1"
jobs.INVOKE = lambda payload: jobs.handler(payload, None)


def call(method, path, body=None, q=None):
    ev = {"rawPath": path, "queryStringParameters": q or {}, "body": json.dumps(body) if body is not None else None,
          "requestContext": {"http": {"method": method}, "stage": "$default", "authorizer": {"jwt": {"claims": {"sub": SUB, "email": "a@b.c"}}}}}
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"] or "null")


def _f(start, end, val, form="10-Q", filed="2025-01-01"):
    return {"start": start, "end": end, "val": val, "form": form, "filed": filed}


def test_quarters_derived():
    rev = [_f("2024-01-01", "2024-03-31", 100), _f("2024-04-01", "2024-06-30", 110), _f("2024-07-01", "2024-09-30", 120),
           _f("2024-01-01", "2024-12-31", 470, "10-K")]
    cfo = [_f("2024-01-01", "2024-03-31", 10), _f("2024-01-01", "2024-06-30", 25), _f("2024-01-01", "2024-09-30", 45),
           _f("2024-01-01", "2024-12-31", 70, "10-K")]
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": rev}}, "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": cfo}}}}}
    qs = F.build_quarters(facts)
    assert [q["rev"] for q in qs] == [100, 110, 120, 140], qs
    assert [q["cfo"] for q in qs] == [10, 15, 20, 25], qs
    assert qs[3]["d"] == ["rev", "cfo"] and qs[0]["d"] == []
    # a restated value (later filing) wins; the first tag in the priority list wins per period
    facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(_f("2024-01-01", "2024-03-31", 101, filed="2025-06-01"))
    assert F.build_quarters(facts)[0]["rev"] == 101


def _series(revs, eps, ni=None, cfo=None, assets=None, y0=2020):
    qs, ends = [], ["03-31", "06-30", "09-30", "12-31"]
    for i, r in enumerate(revs):
        q = {"end": f"{y0 + i // 4}-{ends[i % 4]}", "rev": r, "eps": eps[i], "d": []}
        if ni:
            q["ni"] = ni[i]
        if cfo:
            q["cfo"] = cfo[i]
        if assets:
            q["assets"] = assets[i]
        qs.append(q)
    return F.metrics(qs)


def test_growth_metrics():
    revs = [100 * 1.05 ** i for i in range(16)]
    qs = _series(revs, [0.1] * 16)
    q = qs[-1]
    assert abs(q["revQoQ"] - 0.05) < 1e-9 and abs(q["revYoY"] - (1.05 ** 4 - 1)) < 1e-9
    assert abs(q["revTTMYoY"] - (1.05 ** 4 - 1)) < 1e-9
    assert abs(q["rev3y"] - (1.05 ** 4 - 1)) < 1e-9          # 3-year CAGR of TTM revenue
    assert abs(q["revAccel"]) < 1e-9 and qs[2]["revTTM"] is None and qs[3]["revTTM"] is not None
    assert qs[11]["rev3y"] is None and qs[15]["rev3y"] is not None


def test_inflection_and_quality():
    eps = [-0.3, -0.25, -0.2, -0.1, -0.05, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4]
    ni = [e * 100 for e in eps]
    cfo = [n * 1.2 for n in ni]
    qs = _series([100 + i for i in range(12)], eps, ni, cfo, [1000] * 12)
    assert "Quarter EPS turned positive" in qs[5]["flags"], qs[5]["flags"]
    turned = [i for i, q in enumerate(qs) if "TTM EPS turned positive" in q["flags"]]
    assert turned and qs[turned[0]]["epsTTM"] > 0
    assert any("EPS growth" in f for q in qs[8:] for f in q["flags"])
    assert qs[-1]["quality"] == "strong" and qs[-1]["cashConv"] > 1
    bad = _series([100] * 8, [0.5] * 8, [50] * 8, [5] * 8, [100] * 8)
    assert bad[-1]["quality"] == "weak" and bad[-1]["accruals"] > 0.1
    assert F._g(1, -2) == 1.5 and F._g(1, 0) is None          # growth off a negative base: change over |base|


def test_8k_timing():
    rec = {"form": ["8-K", "10-Q", "8-K", "8-K", "8-K/A", "8-K"], "items": ["2.02,9.01", "", "5.02", "2.02", "2.02", "2.02"],
           "filingDate": ["2025-01-28", "2025-02-01", "2025-03-01", "2025-04-29", "2025-04-29", "2025-07-30"],
           "acceptanceDateTime": ["2025-01-28T21:05:00.000Z", "", "", "2025-04-29T10:59:00.000Z", "2025-04-29T14:00:00.000Z", "2025-07-29T22:01:00.000Z"],
           "accessionNumber": ["a", "b", "c", "d", "e", "f"]}
    r = F.earnings_filings({"filings": {"recent": rec}})
    assert [x["time"] for x in r] == ["16:05", "06:59", "18:01"], r
    assert [(x["date"], F.timing_of(x["time"])) for x in r] == [("2025-01-28", "amc"), ("2025-04-29", "bmo"), ("2025-07-29", "amc")], r


def test_csv_import():
    text = "Symbol;Earnings Date;Time;EPS Estimate;EPS Actual;Revenue Surprise;Guidance;Short interest;Sector\n" \
           "nvda;08/27/2025;AMC;1.00;1.05;1.2%;Raised;3.1;Tech\nXX!;2025-01-01;;;;;;;\nmsft;2025-07-30;after;3.37;3.65;(0.5);reaffirmed;0.8;Tech\n"
    evs, cols, probs = F.parse_csv(text)
    assert [e["sym"] for e in evs] == ["NVDA", "MSFT"] and len(probs) == 1
    n = evs[0]
    assert n["date"] == "2025-08-27" and n["timing"] == "amc" and abs(n["epsSurprise"] - 5.0) < 1e-9 and n["revSurprise"] == 1.2 and n["guide"] == "raise"
    assert n["x"] == {"Short interest": 3.1, "Sector": "Tech"} and evs[1]["revSurprise"] == -0.5 and evs[1]["guide"] == "maintain"
    assert F.setup_of(n) == "Beat & raise" and F.setup_of(evs[1]) == "Miss" and F.setup_of({"guide": "lower", "epsSurprise": 3}) == "Lowered guidance"


def test_reaction_windows():
    from datetime import date, timedelta
    days, d = [], date(2024, 9, 2)
    while len(days) < 160:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    bars = [[x, 100 + i, 101 + i, 99 + i, 100 + i, 1000 + (500 if i == 99 else 0)] for i, x in enumerate(days)]
    bench = [[x, 50, 50, 50, 50, 1] for x in days]
    ev = days[100]
    amc = F.reaction({"date": ev, "timing": "amc"}, bars, bench)
    assert amc["base"] == days[100] and amc["day0"] == days[101] and abs(amc["r1"] - (201 / 200 - 1)) < 1e-12 and amc["r1ab"] == amc["r1"]
    bmo = F.reaction({"date": ev, "timing": "bmo"}, bars, bench)
    assert bmo["base"] == days[99] and bmo["day0"] == days[100] and abs(bmo["gap"] - (200 / 199 - 1)) < 1e-12
    assert abs(bmo["drift20"] - (bars[119][4] / bars[100][4] - 1)) < 1e-12 and bmo["t40"] == days[139]
    # what the stock did before: only bars up to the base close
    assert abs(bmo["pre5"] - (199 / 194 - 1)) < 1e-12 and abs(bmo["pre21ab"] - (199 / 178 - 1)) < 1e-12
    assert abs(bmo["relVol1"] - 1500 / (1000 + 500 / 50)) < 1e-9 and bmo["cmf20"] == 0 and bmo["obv20"] == 1
    later = [[x, 1, 1, 1, 1, 1] if i > 99 else b for i, (x, b) in enumerate(zip(days, bars))]
    assert F.reaction({"date": ev, "timing": "bmo"}, later, bench)["pre21"] == bmo["pre21"]   # no look-ahead
    assert F.reaction({"date": days[30], "timing": "bmo"}, bars, bench) is None        # not enough history before


def test_range_beta_market():
    import random
    from datetime import date, timedelta
    rnd = random.Random(1)
    days, d = [], date(2023, 1, 2)
    while len(days) < 420:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += timedelta(days=1)
    m, st, bench, bars = 100.0, 50.0, [], []
    for x in days:
        r = rnd.gauss(0, 0.01)
        m *= 1 + r
        st *= 1 + 2 * r                                 # beta 2 exactly
        bench.append([x, m, m, m, m, 1])
        bars.append([x, st, st * 1.01, st * 0.99, st, 1000])
    out = F.reaction({"date": days[350], "timing": "bmo"}, bars, bench, None, None, F.market_ctx(bench))
    assert abs(out["beta"] - 2) < 1e-6, out["beta"]
    k = 349
    hi = max(b[2] for b in bars[k - 251:k + 1]); lo = min(b[3] for b in bars[k - 251:k + 1])
    assert abs(out["rangePos"] - (bars[k][4] - lo) / (hi - lo)) < 1e-12 and abs(out["offLow"] - (bars[k][4] / lo - 1)) < 1e-12
    assert abs(out["spy63"] - (bench[k][4] / bench[k - 63][4] - 1)) < 1e-12 and out["regime"]
    assert abs(out["r40b"]) < 0.02 and abs(out["drift40b"]) < 0.02 and abs(out["r40ab"]) > abs(out["r40b"])   # beta 2 explains the moves (up to compounding)
    assert abs(out["volTrend126"] - 1) < 1e-9 and 0 < out["udVol126"] < 1 and abs(out["cmf126"]) < 1e-9      # flat volume, closes mid-range
    up = sum(1 for i in range(k - 125, k + 1) if bars[i][4] > bars[i - 1][4])
    assert abs(out["udVol126"] - up / 126) < 1e-12
    vb = [b[:5] + [3000 if i > k - 42 else 1000] for i, b in enumerate(bars)]
    o2 = F.reaction({"date": days[350], "timing": "bmo"}, vb, bench)
    assert abs(o2["volTrend42"] - 3000 / ((42 * 3000 + 210 * 1000) / 252)) < 1e-9 and F.volume_patterns(o2)["rangeVol"].endswith("rising")
    late = [b if i <= k else [b[0], 1, 1, 1, 1, 1] for i, b in enumerate(bench)]
    assert F.market_ctx(late)[days[k]] == F.market_ctx(bench)[days[k]]       # the market context uses closes up to that day only


def test_backtest_math():
    import backtest
    from datetime import date, timedelta
    rows = []
    for i in range(600):                                # 600 days of releases, one per day
        d = (date(2020, 1, 1) + timedelta(days=i)).isoformat()
        sc = (i % 10) / 10                              # scores cycle 0.0 … 0.9
        rows.append({"sym": f"S{i}", "date": d, "day0": d, "t40": (date(2020, 1, 1) + timedelta(days=i + 56)).isoformat(),
                     "postScore": sc, "oosO2c": None, "r1": 0.0, "gap": 0.0, "o2c": 0.0, "r40": 0.10, "drift40": 0.05, "drift40b": 0.04,
                     "spyVs200": 0.01, "spy63": 0.02})
    p = backtest.trailing_pct(rows, "postScore")
    assert 0 not in p and 199 not in p and 200 in p                        # needs 200 earlier releases
    r = backtest.run(rows, {"stake": 1000, "costBps": 10, "borrowPct": 0, "hedge": "none", "legs": {"A": False, "B": False, "C": True, "D": True}})
    c = next(x for x in r["legs"] if x["leg"] == "C")
    d = next(x for x in r["legs"] if x["leg"] == "D")
    assert c["avg"] == 99.0 and d["avg"] == -101.0, (c, d)                  # +10% − 0.1% cost; short loses 10% + cost
    r = backtest.run(rows, {"hedge": "spy", "costBps": 10, "borrowPct": 0, "legs": {"A": False, "B": False, "C": True, "D": False}})
    assert r["legs"][0]["avg"] == 48.0                                     # +5% vs SPY − 2 × 0.1% (the hedge is a trade)
    r = backtest.run(rows, {"hedge": "beta", "costBps": 0, "borrowPct": 0, "legs": {"A": False, "B": False, "C": False, "D": True},
                            "filters": {"dStrongMarket": True}})
    assert r["legs"][0]["avg"] == -40.0 and r["maxOpen"] >= 5
    rows2 = [dict(x) for x in rows]
    rows2[-1]["postScore"] = -99.0                                         # a later value changes no earlier percentile
    assert all(backtest.trailing_pct(rows2, "postScore")[i] == p[i] for i in p if i < 599)


def test_spearman():
    assert abs(F.spearman(list(range(10)), [x * x for x in range(10)])[0] - 1) < 1e-12
    assert abs(F.spearman(list(range(10)), [-x for x in range(10)])[0] + 1) < 1e-12
    assert F.spearman([1, 2], [1, 2])[0] is None


def test_api_flow():
    st, _ = call("PUT", "/alpaca", {"keyId": "PK" + "A" * 18, "secret": "s" * 30})
    assert st == 200
    st, r = call("PUT", "/fund/settings", {"tickers": "aaa, BBB ccc AAA"})
    assert r["tickers"] == ["AAA", "BBB", "CCC"]
    st, r = call("POST", "/fund/refresh", {})
    assert r["status"] == "done", r
    st, v = call("GET", "/fund")
    assert st == 200 and all(row.get("revTTM") for row in v["rows"]) and v["rows"][0]["rev3y"] is not None
    st, c = call("GET", "/fund/company", None, {"sym": "AAA"})
    assert st == 200 and c["quarters"] and c["releases"] and any(x.get("r1ab") is not None for x in c["reactions"])
    assert {x["timing"] for x in c["reactions"]} <= {"amc", "bmo"}
    st, r = call("POST", "/fund/study", {})
    assert st == 200 and r["status"] == "done", r
    csv = "ticker,date,eps_est,eps_actual,guidance,pre_signal\n" + "\n".join(
        f"{s},{y}-{m:02d}-28,1.0,{1.0 + ((y + m + i) % 3 - 1) * 0.1},{['raise', 'maintain', 'lower'][(y * m + i) % 3]},{(y + m * i) % 7}"
        for i, s in enumerate(["AAA", "BBB", "CCC"]) for y in (2023, 2024, 2025) for m in (1, 4, 7, 10))
    st, r = call("POST", "/fund/events", {"csv": csv})
    assert st == 200 and r["n"] == 36 and "pre_signal" in r["cols"]
    st, r = call("POST", "/fund/study", {"bench": "QQQ", "years": 3, "useSec": True})
    assert r["status"] == "done", r
    st, s = call("GET", "/fund/study")
    assert s["bench"] == "QQQ" and s["n"] >= 36 and s["rows"][0]["timing"] in ("amc", "bmo")
    labels = {g["label"] for g in s["groups"]}
    assert {"Guidance (CSV, else from the call)", "Beat / raise", "pre_signal", "Revenue YoY (reported)", "Accumulation (Chaikin money flow 20d)"} <= labels, labels
    assert all(x["timing"] != "unknown" for x in s["rows"] if x["src"] == "csv")       # timing filled from the same-day 8-K
    st, p = call("GET", "/fund/project", None, {"sym": "BBB"})
    assert st == 200 and [h["label"] for h in p["horizons"]] == ["1 day", "5 days", "1 month", "2 months"], p
    assert p["next"]["date"] >= p["now"]["asOf"] and p["neighbours"] and p["signals"] and p["ownN"] > 10
    h = p["horizons"][3]
    assert h["similar"]["ab"]["n"] > 0 and h["own"]["ab"]["p25"] <= h["own"]["ab"]["p75"] and h["volRange"] > 0
    assert any(r.get("r40ab") is not None for r in s["rows"])
    st, r = call("POST", "/fund/events", {"csv": "foo,bar\n1,2"})
    assert st == 400
    st, r = call("DELETE", "/fund/events")
    assert r["n"] == 0
    # FMP: consensus / actuals / timing, the S&P 500 universe, the model and the paged rows
    st, r = call("PUT", "/fmp", {"key": "k" * 32})
    assert st == 200 and r["connected"], r
    st, r = call("POST", "/fund/universe", {"source": "sp500", "years": 6})
    assert st == 200 and r["universe"][:3] == ["AAA", "BBB", "CCC"] and "DDD" in r["universe"] and "S&P 500" in r["uniName"], r
    st, r = call("POST", "/fund/study", {"years": 6, "useSec": True})
    assert r["status"] == "done", r
    st, s = call("GET", "/fund/study")
    assert s["sources"]["fmp"] > 30 and s["withEst"] > 30, (s["sources"], s["skipped"][:3])
    assert not any(x.startswith("DDD:") for x in s["skipped"]), s["skipped"]      # removed ticker: SEC number from FMP's profile
    row = next(x for x in s["rows"] if x.get("epsSurprise") is not None and x.get("ownAbs") is not None)
    for k in ("pre1ab", "pre5ab", "pre21ab", "pre63ab", "relVol5", "cmf20", "obv20", "sma50", "estEpsG", "epsSurprise", "drift5"):
        assert k in row, k
    assert {t["key"] for t in s["model"]["targets"]} >= {"r1ab", "abs1", "r40ab", "drift20", "score_drift40"}
    assert s["model"]["scale"] == "trailing percentiles"
    tx = [x for x in s["rows"] if x.get("txTone") is not None]
    assert len(tx) > 20 and any(x.get("guideSrc") == "call" for x in tx) and any(x.get("txToneChg") is not None for x in tx)
    assert all("gradeNet90" in x for x in s["rows"] if x["src"] == "fmp")
    assert {"Call tone", "Analyst upgrades − downgrades, 90 days before"} <= {g["label"] for g in s["groups"]}
    st, t = call("GET", "/fund/transcript", None, {"sym": "AAA", "date": tx[0]["txDate"]})
    assert st == 200 and "question-and-answer" in t["content"] and t["scores"]["tone"] is not None and t["guideSents"]
    st, pr = call("POST", "/fmp/test", {})
    assert pr["plan"]["transcripts"] and pr["plan"]["transcriptText"] and pr["plan"]["grades"]
    st, pg = call("GET", "/fund/study/rows", None, {"page": "0"})
    assert st == 200 and len(pg["rows"]) == min(s["n"], F.ROW_PAGE)
    assert s["withTx"] > 20 and s["withGrades"] > 20 and s["txStatus"]["planOk"]
    import base64
    import gzip as _gz
    st, ex = call("GET", "/fund/study/export", None, {"kind": "review"})
    rv = json.loads(_gz.decompress(base64.b64decode(ex["data"])))
    assert ex["name"].endswith(".json.gz") and len(rv["releases"]["data"]) == s["n"] and "txTone" in rv["releases"]["cols"] and rv["summary"]["model"]
    st, ex = call("GET", "/fund/study/export", None, {"kind": "csv"})
    assert _gz.decompress(base64.b64decode(ex["data"])).decode().splitlines()[0].startswith("sym,date,timing")
    st, p = call("GET", "/fund/project", None, {"sym": "AAA"})
    assert p["now"].get("regime") and p["now"].get("rangePos") is not None
    assert p["priceTarget"]["upside"] is not None and "gradeNet90" in p["now"] and p["now"]["lastCall"]["tone"] is not None
    assert st == 200 and p["next"]["source"] == "FMP" and p["next"]["epsEst"] is not None and "relVol5" in p["now"], p["next"]
    assert {"o2c", "after5"} <= {t["key"] for t in s["model"]["targets"]} and any(x.get("o2c") is not None for x in s["rows"])
    assert any(x.get("regime") and x.get("range52") and x.get("beta") is not None and x.get("drift40b") is not None for x in s["rows"])
    assert any(x.get("rangeVol") and x.get("udVol126") is not None for x in s["rows"])
    assert {"Market regime", "52-week range"} <= {g["label"] for g in s["groups"]} and "score_drift40b" in {t["key"] for t in s["model"]["targets"]}
    from datetime import date as _date, timedelta as _td
    d = _date(_date.today().year, 7, 28)
    while d.weekday() >= 5:
        d += _td(days=1)
    st, r = call("POST", "/fund/today", {"day": d.isoformat()})
    assert st == 200 and r["status"] == "done", r
    st, t = call("GET", "/fund/today", None, {"day": d.isoformat()})
    assert t["rows"] and all(x["sym"] in ("AAA", "BBB", "CCC") for x in t["rows"]), t
    assert t["market"]["regime"] and all("rangePos" in x for x in t["rows"])
    for x in t["rows"]:
        assert x["timing"] in ("bmo", "amc") and "epsSurprise" in x and x.get("gap") is not None and "scoreBand" not in x or x["scoreBand"] in ("top", "middle", "bottom")
    st, bt = call("POST", "/fund/backtest", {"cfg": {"stake": 1000}})
    assert st == 200 and "legs" in bt and bt["cfg"]["stake"] == 1000, bt
    st, r = call("POST", "/fund/universe", {"tickers": []})
    assert r["universe"] == []


def _synth(n=3000, seed=7):
    """Releases with a planted pre-release signal: day-0 return = 0.02 × cmf126 + noise; size = 0.5 × vol20 + noise."""
    import random
    from datetime import date, timedelta
    rnd = random.Random(seed)
    rows, d0 = [], date(2015, 1, 2)
    for i in range(n):
        d = d0 + timedelta(days=i * 3650 // n)
        cmf, vol = rnd.uniform(-0.5, 0.5), rnd.uniform(0.01, 0.05)   # planted in cmf126 (a direction signal) and vol20 (size)
        r1 = 0.02 * cmf / 0.29 + rnd.gauss(0, 0.04)
        r = {"sym": f"S{i % 300}", "date": d.isoformat(), "base": d.isoformat(), "day0": (d + timedelta(days=1)).isoformat(),
             "t1": (d + timedelta(days=1)).isoformat(), "t5": (d + timedelta(days=7)).isoformat(), "t20": (d + timedelta(days=28)).isoformat(),
             "t40": (d + timedelta(days=56)).isoformat(), "cmf126": cmf, "vol20": vol, "pre21ab": rnd.gauss(0, 0.05),
             "r1ab": r1 + (abs(rnd.gauss(0, vol * 2)) * (1 if r1 > 0 else -1)), "r5ab": rnd.gauss(0, 0.05), "r20ab": rnd.gauss(0, 0.08),
             "r40ab": rnd.gauss(0, 0.1), "drift5": rnd.gauss(0, 0.03), "drift20": rnd.gauss(0, 0.05), "drift40": rnd.gauss(0, 0.07)}
        rows.append(r)
    return rows


def test_sentiment_scores():
    import sentiment
    up = sentiment.score("CEO: Record growth, strong demand and robust margins. We are raising our full-year guidance. "
                         "We now expect revenue at the upper end of the range. Operator: We will now begin the question-and-answer session. "
                         "Analyst: Great quarter. CEO: Momentum is strong.")
    dn = sentiment.score("CEO: Demand was weak and orders declined. We are lowering our outlook for the full year. "
                         "Operator: Our first question comes from Analyst. CEO: It may remain uncertain and difficult.")
    assert up["tone"] > 0.5 and dn["tone"] < -0.5 and up["guideDir"] == "raise" and dn["guideDir"] == "lower"
    assert up["toneQA"] is not None and dn["uncert"] > up["uncert"]
    assert sentiment.score("Results were not weak. We reaffirm our full-year guidance.")["guideDir"] == "maintain"
    assert sentiment.score("Results were not weak.")["tone"] > 0           # negation flips


def test_grades_before_base_only():
    row = {}
    grades = [{"date": "2025-01-10", "action": "up"}, {"date": "2025-03-01", "action": "down"}, {"date": "2025-03-02", "action": "up"},
              {"date": "2025-03-03", "action": "up"}]
    F._grade_fields(row, grades, "2025-03-03")          # the action on the base day itself is excluded
    assert row == {"gradeNet30": 0, "gradeNet90": 1, "gradeN90": 3}, row


def test_model_finds_planted_signal():
    import model
    m = model.evaluate(_synth(), min_train=300)
    t = {x["key"]: x for x in m["targets"]}
    assert t["r1ab"]["verdict"] == "edge" and t["r1ab"]["hit"] > t["r1ab"]["hitBase"] and t["r1ab"]["icQ"]["t"] > 3, t["r1ab"]
    assert t["r1ab"]["coef"][0]["key"] == "cmf126" and t["r1ab"]["spread"]["mean"] > 0.01
    assert t["abs1"]["ic"] > 0.1                        # size follows volatility
    assert all(t[k]["verdict"] != "edge" for k in ("r20ab", "r40ab", "drift20")), [(k, t[k]["verdict"], t[k]["icQ"]) for k in ("r20ab", "r40ab", "drift20")]
    pr = model.apply(m, {"cmf126": 0.4, "vol20": 0.02})
    assert next(x for x in pr if x["key"] == "r1ab")["pred"] > 0.01


def test_ranked_uses_earlier_releases_only():
    import model
    rows = [{"sym": "A", "date": f"2020-01-{d:02d}", "base": f"2020-01-{d:02d}", "cmf20": float(d)} for d in range(1, 31)]
    model.RANK_MIN, old = 5, model.RANK_MIN
    try:
        rk, ref = model.ranked(rows, ["cmf20"])
    finally:
        model.RANK_MIN = old
    assert rk[3]["cmf20"] is None and rk[10]["cmf20"] == 0.5        # above every earlier value: top percentile
    rows[29]["cmf20"] = -100.0                                       # changing the last release changes no earlier rank
    model.RANK_MIN = 5
    try:
        rk2, _ = model.ranked(rows, ["cmf20"])
    finally:
        model.RANK_MIN = old
    assert [r["cmf20"] for r in rk[:29]] == [r["cmf20"] for r in rk2[:29]]
    assert model.post_score({"epsSurprise": 0.4, "accruals": 0.4}) == 0.0


def test_implied_move():
    import alpopt
    snaps = {"call": {"QQQ261120C00500000": {"latestQuote": {"bp": 9.8, "ap": 10.2}}, "QQQ261127C00500000": {"latestQuote": {"bp": 15, "ap": 15}},
                      "QQQ261120C00510000": {"latestQuote": {"bp": 5, "ap": 5.2}}},
             "put": {"QQQ261120P00500000": {"latestQuote": {"bp": 9.9, "ap": 10.1}}, "QQQ261120P00510000": {"latestQuote": {"bp": 14, "ap": 14.4}}}}
    old = alpopt._req
    alpopt._req = lambda c, m, url, body=None, timeout=30: {"snapshots": snaps["call" if "type=call" in url else "put"]}
    try:
        call("PUT", "/alpaca", {"keyId": "PK" + "A" * 18, "secret": "s" * 30})
        im = F.implied_move(SUB, "QQQ", "2026-11-19", 501.0)
    finally:
        alpopt._req = old
    assert im["expiry"] == "2026-11-20" and im["strike"] == 500 and abs(im["move"] - 20 / 501) < 1e-9, im


def test_model_no_lookahead():
    """An outcome that becomes known only after the prediction time must not be in the training set: a feature that
    copies the target is useless if each release's own outcome were the only one 'leaking'."""
    import model
    rows = _synth(800, seed=3)
    for r in rows:
        r["t1"] = "2099-01-01"                          # no outcome is ever known before any prediction
    assert model.walk_forward(rows, model.PRE, lambda r: r.get("r1ab"), "t1", "base", 50) == []
    rows = _synth(800, seed=3)
    preds = model.walk_forward(rows, model.PRE, lambda r: r.get("r1ab"), "t1", "base", 50)
    seen = {}
    for r, p, base, y, when in preds:
        seen[when] = True
    assert preds and all(r["t1"] > r["base"] for r, *_ in preds)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
