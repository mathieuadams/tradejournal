import math
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
    r = backtest.run(rows, {"stake": 1000, "costBps": 10, "borrowPct": 0, "legs": {"A": {"on": False}, "B": {"on": False},
                            "C": {"on": True, "hedge": "none", "weak": False}, "D": {"on": True, "hedge": "none", "strong": False}}})
    c = next(x for x in r["legs"] if x["leg"] == "C")
    d = next(x for x in r["legs"] if x["leg"] == "D")
    assert c["avg"] == 99.0 and d["avg"] == -101.0, (c, d)                  # +10% − 0.1% cost; short loses 10% + cost
    r = backtest.run(rows, {"costBps": 10, "borrowPct": 0, "legs": {"A": {"on": False}, "B": {"on": False}, "C": {"on": True, "hedge": "spy", "weak": False},
                            "D": {"on": False}}})
    assert r["legs"][0]["avg"] == 48.0                                     # +5% vs SPY − 2 × 0.1% (the hedge is a trade)
    r = backtest.run(rows, {"costBps": 0, "borrowPct": 0, "legs": {"A": {"on": False}, "B": {"on": False}, "C": {"on": False},
                            "D": {"on": True, "hedge": "beta", "strong": True}}})
    assert r["legs"][0]["avg"] == -50.0 and r["maxOpen"] >= 5                # no beta on the rows: beta 1 = SPY
    res, ok = backtest.optimize(rows, {})
    assert ok and len(res) == sum(len(backtest.combos(k)) for k in "ABCD") + 512 + 16 and res[0]["train"] is not None
    rk = backtest.ranked(res)
    assert rk["C"][0]["train"]["n"] >= rk["C"][-1]["train"]["n"] or (rk["C"][0]["trainX"].get("t") or 0) >= (rk["C"][-1]["trainX"].get("t") or -1e9)
    r = backtest.run(rows, {"legs": {"A": {"on": False}, "B": {"on": False}, "C": {"on": True, "band": 0.3, "hold": 40, "hedge": "none", "weak": False}, "D": {"on": False}}})
    assert r["split"]["train"]["n"] + r["split"]["test"]["n"] == r["n"] and r["alpha"]["avg"] == 48.0     # vs SPY, on the hedged cost basis
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
    import uwhales                                     # the C / D trades with real option prices
    uwhales.save_key(SUB, "uw_test_token_1234567890")
    import optreal
    import backtest
    st, r = call("POST", "/fund/backtest/real", {"cfg": {"legs": {"C": {"band": 0.3, "weak": False}, "D": {"band": 0.3, "strong": False}}}})
    assert st == 200, r
    st, v = call("GET", "/fund/backtest/real")
    assert st == 200 and v["complete"] and v["view"] and "all" in v["view"], v.get("job")
    tok = uwhales.token(SUB)                           # one trade priced with the (fake) contract history
    L = {"mny": 0.0, "dte": 60, "entry": "close"}
    t = {"leg": "C", "sym": "AAA", "entry": "2024-03-04", "exit": "2024-04-01", "pnl": 10.0, "ret": 0.05, "vol20": 0.02}
    r = optreal.price_trade(tok, t, L, 101.0, lambda d: 100.0, 101.0)
    assert r.get("occ", "").startswith("AAA24") and "C00100000" in r["occ"] and r["how"] == "close", r     # the nearest strike: 100
    assert abs((_d := __import__("datetime").date)(*map(int, r["expiry"].split("-"))).toordinal() - _d(2024, 5, 3).toordinal()) <= 3, r
    assert r["buy"] > r["mid0"] and r["sell"] < r["mid1"] and abs(r["ret"] - (r["sell"] / r["buy"] - 1)) < 1e-12, r
    r3 = optreal.price_trade(tok, {**t, "exit": "2024-05-20"}, L, 101.0, lambda d: 100.0, 101.0)
    assert r3["expiry"] >= "2024-05-20" and r3["how"] == "close", r3      # an expiry that outlives the planned exit
    assert optreal.price_trade(tok, t, L, 101.0, lambda d: 100.0, 101.0)["occ"] == r["occ"]
    bars = {"2024-03-04": ["2024-03-04", 100, 102, 99, 101, 1], "2024-03-05": ["2024-03-05", 101, 101, 97, 98, 1]}
    rd = optreal.price_trade(tok, t, L, 101.0, lambda d: 100.0, 101.0, bars)    # closed below the day-0 low (99) on day 1
    assert rd["d0Day"] == "2024-03-05" and rd["d0Ret"] is not None, rd
    assert optreal.price_trade(tok, t, L, 101.0, lambda d: 100.0, 101.0, {**bars, "2024-03-05": ["2024-03-05", 101, 103, 100, 102, 1]})["d0Day"] is None
    sm = optreal.summarize([{**t, "why": "x", "release": "2024-03-03"}], {optreal.tkey(t): rd}, backtest.clean({}))
    assert sm["d0"]["C"]["all"]["hit"] == 1, sm["d0"]
    sk = optreal.price_trade(tok, t, {**L, "ivCap": 1.0}, 101.0, lambda d: 100.0, 101.0)   # fake IV 42% vs 0.02×√252 ≈ 32%
    assert sk["err"].startswith("skipped: implied vol above 1"), sk
    assert "err" not in optreal.price_trade(tok, t, {**L, "ivCap": 1.5}, 101.0, lambda d: 100.0, 101.0)
    t2 = {**t, "exit": "2024-08-01"}                   # past every listed expiry: intrinsic value then
    r2 = optreal.price_trade(tok, t2, L, 101.0, lambda d: 112.0, 101.0)
    assert r2["how"] == "expired" and abs(r2["sell"] - 12.0) < 1e-9, r2
    tr = [{**t, "why": "x", "release": "2024-03-03"}]
    sm = optreal.summarize(tr, {optreal.tkey(t): {**r}}, backtest.clean({}))
    assert sm["all"]["n"] == 1 and sm["all"]["real"] == round(1000 * r["ret"], 2) and sm["legs"]["C"]["model"] == 10.0, sm["all"]
    sr = F.store.get_json(f"fund/u/{SUB}/study_rows")
    F.store.put_json(f"fund/u/{SUB}/study_rows", {"rows": [{k: v for k, v in r.items() if k not in ("postScore", "oosO2c")} for r in sr["rows"]]})
    st, bt = call("POST", "/fund/backtest", {"cfg": {}})             # an older study: scored in the background, then run
    assert bt.get("preparing") or "legs" in bt, bt
    st, bt = call("POST", "/fund/backtest", {"cfg": {}})
    assert st == 200 and "legs" in bt and "split" in bt or bt.get("n") == 0, bt
    st, r = call("POST", "/fund/backtest/opt", {"cfg": {"stake": 500}})
    assert st == 200 and r["status"] == "done", r
    st, o = call("GET", "/fund/backtest/opt", None, {"on": "alpha"})
    assert o["opt"]["complete"] and set("ABCD") <= set(o["opt"]["legs"]) and {"C4", "D4", "C2", "D2", "C3", "D3"} <= set(o["opt"]["legs"]) and o["opt"]["cfg"]["stake"] == 500, o
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


def test_exits_and_option():
    import array
    import backtest as B
    # one release: day-0 close 100; then 101, 104 (high 106), 99 (low 97), 95, 110
    bars = [["2024-01-02", 99, 100, 98, 100, 1], ["2024-01-03", 100, 101.5, 99.5, 101, 1], ["2024-01-04", 101, 106, 100, 104, 1],
            ["2024-01-05", 103, 103.5, 97, 99, 1], ["2024-01-08", 98, 98, 94, 95, 1], ["2024-01-09", 96, 111, 95, 110, 1]]
    bmap = {b[0]: 400 + i for i, b in enumerate(bars)}
    bopen = {"2024-01-02": 398}
    P = array.array("h", B.path_of(bars, {b[0]: i for i, b in enumerate(bars)}, "2024-01-02", bmap, bopen))
    assert P[0] == 0 and P[1] == -100 and P[4] == 0 and P[5] == -50                 # day 0: open 99 vs close 100; SPY opened 398
    assert P[6] == 1 and P[10] == 100 and P[11] == round(1 / 400 * 1e4)
    j, fill, dd, s, why = B.walk(P, 0, False, 5, 0, 0, 0, at_open=True)     # in at the 99 open, out at 110
    assert why == "time" and abs(fill - 110 / 99) < 1e-9 and abs(s - (405 - 398) / 400 * 1e4) < 2, (fill, s)
    j, fill, dd, s, why = B.walk(P, 0, False, 5, 0.01, 0, 0, at_open=True)  # day 0's own low (98 < 98.01) stops it
    assert why == "stop" and j == 0 and dd == 0 and abs(fill - 0.99) < 1e-9
    j, fill, dd, s, why = B.walk(P, 0, False, 5, 0, 0, 0)                    # no rule: the last close
    assert why == "time" and abs(fill - 1.10) < 1e-9 and dd == 7, (j, fill, dd, why)
    j, fill, dd, s, why = B.walk(P, 0, False, 5, 0.04, 0, 0)                 # stop 4%: the 94 low on day 4 → filled at 96
    assert why == "stop" and abs(fill - 0.96) < 1e-9 and j == 4
    j, fill, dd, s, why = B.walk(P, 0, False, 5, 0, 0.05, 0)                 # trailing 5% from the 106 high → 100.7, low 97
    assert why == "trailing stop" and abs(fill - 1.06 * 0.95) < 1e-9 and j == 3, (j, fill, why)
    j, fill, dd, s, why = B.walk(P, 0, False, 5, 0, 0, 0.05)                 # target 5%: the 106 high on day 2
    assert why == "target" and abs(fill - 1.05) < 1e-9 and j == 2
    j, fill, dd, s, why = B.walk(P, 0, True, 5, 0.05, 0, 0)                  # short, stop 5%: the 106 high
    assert why == "stop" and abs(fill - 1.05) < 1e-9 and j == 2
    j, fill, dd, s, why = B.walk(P, 0, True, 5, 0, 0, 0.04)                  # short, target 4%: low 94 → 96 (open 98)
    assert why == "target" and abs(fill - 0.96) < 1e-9
    assert B.walk(P, 0, False, 9, 0, 0, 0)[4] == "data"                      # the prices end before the planned exit
    assert B.walk(P, 0, False, 9, 0, 0, 0, max_days=60)[4] == "data"
    j, fill, dd, s, why = B.walk(P, 0, False, 5, 0, 0, 0, d0=True)           # day-0 low 98: day 4 closes at 95
    assert why == "closed below the day-0 low" and j == 4 and abs(fill - 0.95) < 1e-9, (j, fill, why)
    j, fill, dd, s, why = B.walk(P, 0, True, 5, 0, 0, 0, d0=True)            # day-0 high 100: day 1 closes at 101
    assert why == "closed above the day-0 high" and j == 1 and abs(fill - 1.01) < 1e-9, (j, fill, why)
    j, fill, dd, s, why = B.walk(P, 0, False, 5, 0, 0, 0, max_days=3)        # an option expiring after 3 days
    assert why == "expiry" and dd == 3
    c, d = B.bs(1.0, 1.0, 60 / 365, 0.3, True)                               # an ATM 2-month call ≈ 5% of the stock, delta ≈ 0.55
    assert 0.045 < c < 0.06 and 0.5 < d < 0.6, (c, d)
    p_, d2 = B.bs(1.0, 1.0, 60 / 365, 0.3, False)
    assert abs((c - p_) - (1 - math.exp(-B.R_FREE * 60 / 365))) < 1e-9 and d2 < 0      # put-call parity
    row = {"sym": "X", "date": "2024-01-01", "day0": "2024-01-02", "vol20": 0.3 / math.sqrt(252), "beta": 1.0}
    paths = ({"X|2024-01-02": 0}, P)
    out, cfg = [], B.clean({"legs": {"C": {"hold": 5, "inst": "option", "dte": 60, "hedge": "none"}}})
    B.hold_trade("C", cfg["legs"]["C"], row, "2024-01-02", "test", None, paths, cfg, out)
    t = out[0]
    assert t["side"].startswith("call 60d") and t["exit"] == "2024-01-08" and t["res"] < -0.3 and t["pnl"] == round(1000 * (t["res"] - 0.05), 2), t   # hold 5 = 4 sessions: −5% stock ≈ −50% call
    out, cfg = [], B.clean({"legs": {"C": {"hold": 5, "stop": 0.04, "hedge": "spy"}}})
    adds = []
    B.hold_trade("C", cfg["legs"]["C"], row, "2024-01-02", "test", lambda *a, **k: adds.append(a), paths, cfg, out)
    assert adds and abs(adds[0][2] - (-0.04 - (404 / 400 - 1))) < 1e-3 and "stop" in adds[0][6], adds



def test_setup_filters():
    import backtest as B
    c = B.clean({"legs": {"C": {"rs": 1, "cash": True, "noQ4": True, "noWeakSec": True}, "D": {"gapDown": True, "notRaised": True}}})
    L, D = c["legs"]["C"], c["legs"]["D"]
    assert L["rs"] is True and L["raised"] is False and D["miss"] is False
    good = {"pre21ab": 0.05, "accruals": -0.08, "sector": "Hardware & semiconductors"}
    assert B.setup_ok("C", L, good, "2025-05-01")
    assert not B.setup_ok("C", L, good, "2025-02-01")                        # Q4 reports
    assert not B.setup_ok("C", L, {**good, "pre21ab": -0.01}, "2025-05-01")
    assert not B.setup_ok("C", L, {**good, "accruals": None}, "2025-05-01")  # unknown: filtered out
    assert not B.setup_ok("C", L, {**good, "sector": "Retail"}, "2025-05-01")
    assert B.setup_ok("C", B.clean({})["legs"]["C"], {}, "2025-02-01")      # all off: everything passes
    assert B.setup_ok("D", D, {"gap": -0.05, "txGuide": 0}, "2025-05-01") and not B.setup_ok("D", D, {"gap": -0.05, "txGuide": 1}, "2025-05-01")
    assert len(B.filter_grid("C")) == 512 and len(B.filter_grid("D")) == 16
    L2 = B.clean({"legs": {"C": {"cheap": True, "toneUp": True}}})["legs"]["C"]
    assert B.setup_ok("C", L2, {}, "2025-05-01", {"ey": 0.8, "tone": 0.5}) and not B.setup_ok("C", L2, {}, "2025-05-01", {"ey": 0.5, "tone": 0.5})
    assert not B.setup_ok("C", L2, {}, "2025-05-01", {"ey": 0.9, "tone": 0.2}) and not B.setup_ok("C", L2, {}, "2025-05-01", {})



def test_qqq_universe_is_separate():
    import space
    st, spy = call("GET", "/fund")
    spy_uni = spy["settings"]["universe"]
    Q = {"u": "qqq"}
    st, r = call("POST", "/fund/universe", {"source": "nasdaq100", "years": 5}, Q)
    assert st == 200 and r["uniName"].startswith("Nasdaq-100") and len(r["universe"]) == 2, r
    st, q = call("GET", "/fund", None, Q)
    assert q["settings"]["bench"] == "QQQ" and q["settings"]["space"]["id"] == "qqq" and q["settings"]["universe"] == r["universe"]
    st, b = call("GET", "/fund/backtest/real", None, Q)
    assert st == 200 and b["view"] is None                                  # nothing priced in this universe yet
    space.set("qqq")
    assert space.pre("x") == "fund/u/x/qqq" and space.job("fund") == "fund-qqq"
    space.set("spy")
    st, again = call("GET", "/fund")
    assert again["settings"] == spy["settings"] and again["settings"]["space"]["id"] == "spy"   # the SPY universe untouched



def test_reaction_skips_a_gap_in_prices():
    import fundamentals as F
    from datetime import date, timedelta
    bars, d = [], date(2022, 1, 3)
    while len(bars) < 120:                               # 120 sessions, then nothing for two years, then 30 more
        if d.weekday() < 5:
            bars.append([d.isoformat(), 10, 10.2, 9.8, 10, 1000])
        d += timedelta(days=1)
    d = date(2025, 8, 29)
    for _ in range(30):
        if d.weekday() < 5:
            bars.append([d.isoformat(), 1200, 1300, 1100, 1250, 1000])
        d += timedelta(days=1)
    bench = [[b[0], 400, 401, 399, 400, 1] for b in bars]
    last = bars[119][0]
    ev = {"sym": "BBBY", "date": (date.fromisoformat(last) + timedelta(days=3)).isoformat(), "timing": "bmo"}
    assert F.reaction(ev, bars, bench) is None                       # day 0 would be 2025-08-29
    ok = {"sym": "BBBY", "date": bars[100][0], "timing": "bmo"}
    assert F.reaction(ok, bars, bench) is not None



def test_valuation_and_drivers():
    import fundamentals as F
    import backtest as B
    rs = [{"date": "2024-03-31", "pe": 20.0, "ps": 3.0}, {"date": "2024-06-30", "pe": -10.0, "ps": 2.0}]
    assert F._ratio_fields(rs, "2024-05-01")["pe"] == 20.0 and abs(F._ratio_fields(rs, "2024-05-01")["ey"] - 0.05) < 1e-12
    assert F._ratio_fields(rs, "2024-07-25")["ey"] == -0.1 and F._ratio_fields(rs, "2025-01-30") == {}
    assert any(k == "ey" for k, _ in F.NUM_SIGNALS) and "pe" in F.EXPORT_KEYS
    import random
    rnd = random.Random(3)
    rows, trades = [], []
    for i in range(300):                               # cheap (high earnings yield) wins; the rest is noise
        ey = rnd.uniform(-0.05, 0.1)
        d = f"202{3 + i // 150}-{1 + i % 12:02d}-{1 + i % 27:02d}"
        rows.append({"sym": f"S{i}", "day0": d, "ey": ey, "vol20": rnd.uniform(0.01, 0.04), "sector": "Retail" if i % 2 else "Services"})
        trades.append({"sym": f"S{i}", "entry": d, "pnl": (300 if ey > 0.05 else -100) + rnd.uniform(-50, 50)})
    dv = B.drivers(trades, rows)
    top = dv["num"][0]
    assert top["key"] == "ey" and top["consistent"] and top["spread"] > 300 and top["thirds"][2]["win"] > 0.9, top
    assert any(x["key"] == "sector" for x in dv["cat"]) and B.drivers(trades[:20], rows) is None



def test_etf_matrix():
    import etf
    st, r = call("GET", "/etf", None, {"days": "60"})
    assert st == 200, r
    n = len(r["syms"])
    assert n >= 2 and r["window"] == 60 and sorted(r["order"]) == list(range(n)) and r["settings"]["syms"] == etf.DEFAULT
    m = r["matrix"]
    assert all(m[i][i] == 1.0 for i in range(n)) and all(m[i][j] == m[j][i] for i in range(n) for j in range(n))
    assert all(-1.0001 <= m[i][j] <= 1.0001 for i in range(n) for j in range(n) if m[i][j] is not None)
    assert not {"SPY", "DIA", "IWM", "QQQ", "HYG", "TLT"} & set(etf.DEFAULT)
    st, s2 = call("PUT", "/etf/settings", {"syms": ["xle", "XLK", "xle", "GLD"]})
    assert st == 200 and s2["syms"] == ["XLE", "XLK", "GLD"]
    st, r2 = call("GET", "/etf", None, {"days": "20"})
    assert st == 200 and r2["syms"] == ["XLE", "XLK", "GLD"] and r2["window"] == 20
    st, bad = call("PUT", "/etf/settings", {"syms": ["XLE"]})
    assert st == 400
    assert abs(etf._corr([1, 2, 3, 4], [2, 4, 6, 8]) - 1) < 1e-12 and abs(etf._corr([1, 2, 3, 4], [4, 3, 2, 1]) + 1) < 1e-12



def test_etf_pairs():
    import etfpairs as P
    call("PUT", "/etf/settings", {"syms": ["XLE", "XLK", "GLD", "SMH"]})
    st, r = call("POST", "/etf/pairs", {"cfg": {"entry": 1.5, "minCorr": -1}})
    assert st == 200, r
    assert r["nPairs"] == 6 and r["cfg"]["entry"] == 1.5 and "train" in r["split"] and r["now"], {k: r[k] for k in ("nPairs", "n")}
    for t in r["trades"]:
        assert t["entry"] <= t["exit"] and t["why"] in ("back to the mean", "stop", "time") and t["long"] != t["short"]
    st, o = call("POST", "/etf/pairs/opt", {"cfg": {"stake": 500}})
    assert st == 200, o
    st, v = call("GET", "/etf/pairs/opt")
    assert st == 200 and v["opt"] and v["opt"]["done"] == len(P.combos()) and v["opt"]["top"], v.get("job")
    # the mechanics on a hand-made pair: A jumps 10% above B, then comes back
    ds = [f"2020-01-{d:02d}" for d in range(1, 31)] + [f"2020-02-{d:02d}" for d in range(1, 29)] + [f"2020-03-{d:02d}" for d in range(1, 31)]
    import math
    pa, pb = {}, {}
    for i, d in enumerate(ds):
        base = 100 * (1 + 0.001 * math.sin(i))
        bump = 1.10 if i in (70, 71) else 1.0
        pa[d], pb[d] = (base * bump, base * bump), (base * (1 + 0.0005 * math.cos(i * 1.3)), base * (1 + 0.0005 * math.cos(i * 1.3)))
    p = P.prep_pair(pa, pb, [20])
    c = P.clean({"look": 20, "entry": 2, "exit": 0, "hold": 10, "minCorr": -1, "costBps": 0, "borrowPct": 0})
    ts = [t for t in P.sim_pair("A", "B", p, c) if not t.get("open")]
    t0 = next(t for t in ts if t["entry"] == ds[71])
    assert t0["short"] == "A" and t0["long"] == "B" and t0["pnl"] > 50, t0      # bought back after the jump faded



def test_swing():
    import swing as SW
    for kind in ("qulla", "turtle"):
        st, r = call("POST", f"/swing/{kind}", {"cfg": {"uni": "etf", "minAdr": 0.0, "prior": 0.0, "tight": 0.5}})
        assert st == 200, r
        st, v = call("GET", f"/swing/{kind}")
        assert st == 200 and v["res"] and v["res"]["kind"] == kind and "train" in v["res"]["split"], v.get("job")
        for t in v["res"]["trades"]:
            assert t["entry"] <= t["exit"]
        st, o = call("POST", f"/swing/{kind}/opt", {"cfg": {"uni": "etf"}})
        assert st == 200, o
        st, v = call("GET", f"/swing/{kind}")
        assert v["opt"] and v["opt"]["done"] == len(SW.grid(kind)), v.get("job")
    for kind in ("turtle", "qulla"):                       # the stock universes (both: S&P 500 + Nasdaq-100)
        st, r = call("POST", f"/swing/{kind}", {"cfg": {"uni": "both", "minAdr": 0.0, "prior": 0.0, "tight": 0.5, "minDv": 0}})
        assert st == 200, r
        st, v = call("GET", f"/swing/{kind}")
        assert v["job"]["status"] == "done" and v["res"] and v["res"]["nSyms"] >= 2, (v["res"] or {}).get("nSyms")
    st, v = call("GET", "/swing/qulla")
    A = v["res"]["acct"]
    assert A and A["all"]["start"] == 10000 and A["all"]["taken"] > 0 and A["test"]["from"] == v["res"]["split"]["date"], A and A["all"]
    assert any("size" in t for t in v["res"]["trades"])
    acc = SW.account([{"entry": "2020-01-02", "exit": "2020-01-10", "ret": 0.1, "risk": 0.05}, {"entry": "2020-01-03", "exit": "2020-01-06", "ret": -0.05, "risk": 0.05},
                      {"entry": "2020-01-06", "exit": "2020-02-01", "ret": 0.2, "risk": 0.02}], SW.clean("qulla", {"costBps": 0}))
    assert acc["final"] == 10595 and acc["sizes"][2] == 2475                # 1% of 9,900 ÷ 2% = 4,950, capped at 25% of the account
    # the mechanics: a flat base, a breakout, a run, then a close under the 10-day average
    from datetime import date, timedelta
    b, d, px = [], date(2020, 1, 2), 10.0
    while len(b) < 140:
        if d.weekday() < 5:
            i = len(b)
            if i < 60:
                px *= 1.012                                # the prior move
            elif i < 70:
                px = px                                    # the base
            elif i < 85:
                px *= 1.03                                 # the breakout run
            else:
                px *= 0.97
            b.append([d.isoformat(), px * 0.995, px * 1.02, px * 0.98, px, 5e6])
        d += timedelta(days=1)
    c = SW.clean("qulla", {"minAdr": 0.03, "prior": 0.3, "tight": 0.06, "partialDay": 3, "trail": 10, "costBps": 0})
    ts = SW.qulla("X", b, c, {})
    t0 = ts[0]
    assert t0["entry"] == b[70][0] and t0["pnl"] > 0 and "10-day" in t0["why"], t0
    c2 = SW.clean("turtle", {"costBps": 0})
    tt = SW.turtle("X", b, c2, {})
    assert tt and tt[0]["side"] == "long" and all(x["entry"] <= x["exit"] for x in tt)



def test_swing_futures():
    import futdaily
    import swing as SW
    st, r = call("GET", "/swing/turtle")
    assert st == 200 and SW.clean("turtle", {})["uni"] == "fut" and len(r["fut"]) == len(futdaily.SPEC)
    st, r = call("POST", "/swing/turtle", {"cfg": {"uni": "fut"}})
    assert st == 400 and "Download futures daily bars" in r["error"], r     # said on the page, not lost in the job
    st, r = call("POST", "/swing/futdaily")
    assert st == 400                                      # Databento not connected
    st, r = call("PUT", "/databento", {"key": "db-" + "A" * 29})
    assert st == 200, r
    call("PUT", "/databento", {"maxCost": 1})
    st, r = call("POST", "/swing/futdaily")
    assert st == 400 and "cost limit" in r["error"], r
    call("PUT", "/databento", {"maxCost": 20})
    st, r = call("POST", "/swing/futdaily")
    assert st == 200, r
    st, v = call("GET", "/swing/turtle")
    assert all(x["n"] > 1000 and x["rolls"] > 20 and x["missing"] == 0 for x in v["fut"]), v["fut"][:2]
    # back-adjusted: no jump at the rolls (the fake adds 1.5 points per later contract)
    b = futdaily.bars("ES")
    jumps = max(abs(b[i][4] - b[i - 1][4]) for i in range(1, len(b)))
    assert jumps < 15, jumps
    import databento
    assert databento._avail_from("Schema `ohlcv-1d` for dataset `GLBX.MDP3` is only available between 2010-06-06T00:00:00.000000000Z and "
                                 "2026-11-16T00:00:00.000000000Z. Try requesting a different schema (422).") == "2026-11-16T00:00:00"
    # a market whose request stalls is noted and skipped; the others go on; a resumed download skips what's done today
    import store
    store.put_json("fut/d/NQ", {})
    real = futdaily._parent
    futdaily._parent = lambda key, root, end: (_ for _ in ()).throw(TimeoutError("timed out")) if root == "NQ" else real(key, root, end)
    try:
        assert futdaily.download(SUB) is None
    finally:
        futdaily._parent = real
    # the front month: the spread is dropped, rolls follow the volume forward, no jump at a roll
    by = futdaily._parent(databento.key(SUB), "GC", "2012-01-01T00:00:00")
    assert all("-" not in s for d in by.values() for s in d)
    b, rolls, miss = futdaily.front("GC", by)
    assert rolls >= 5 and miss == 0 and max(abs(b[i][4] - b[i - 1][4]) for i in range(1, len(b))) < 15
    assert futdaily._expiry("ES", "ESZ6", "2026-11-16") == (2026, 12) and futdaily._expiry("ES", "ESH0", "2029-12-01") == (2030, 3)
    # an answer without symbols: the continuous way
    real_open = databento._open
    def no_sym(key, method, path, params, timeout=60):
        if params.get("stype_in") == "parent":
            import io as _io
            class R(_io.BytesIO):
                pass
            return R(b"ts_event,open,high,low,close,volume\n")
        return real_open(key, method, path, params, timeout)
    databento._open = no_sym
    try:
        b2, rolls2, _ = futdaily._market(databento.key(SUB), "ES", futdaily._end(databento.key(SUB)))
    finally:
        databento._open = real_open
    assert len(b2) > 1000 and rolls2 > 20
    import databento
    nq = [x for x in futdaily.status() if x["root"] == "NQ"][0]
    assert nq["error"] == "timed out" and nq["n"] == 0, nq
    assert futdaily.download(SUB) is None and [x for x in futdaily.status() if x["root"] == "NQ"][0]["n"] > 1000
    # the adjustment itself
    v0 = [["d1", 10, 11, 9, 10, 1, 1], ["d2", 12.5, 13, 12, 12.5, 1, 2], ["d3", 13, 14, 12, 13, 1, 2]]
    v1 = [["d1", 12, 13, 11, 12, 1, 2], ["d2", 13, 13, 13, 13, 1, 3]]
    out, rolls, miss = futdaily.adjust(v0, v1)
    assert rolls == 1 and miss == 0 and out[0][4] == 12 and out[1][4] == 12.5
    st, r = call("POST", "/swing/turtle", {"cfg": {"uni": "fut", "risk": 2000, "groups": ["Metals", "Energy"]}})
    assert st == 200, r
    st, v = call("GET", "/swing/turtle")
    res = v["res"]
    assert res and res["nSyms"] == 8 and res["n"] > 0 and {g["group"] for g in res["groups"]} <= {"Metals", "Energy"}, res and res["nSyms"]
    t = res["trades"][-1]
    assert t["contracts"] > 0 and t["ret"] is None and t["R"] is not None
    # 1 N against a unit = the $ risk per unit (fractional contracts)
    spec = futdaily.SPEC["GC"]
    c = SW.clean("turtle", {"uni": "fut", "risk": 1000, "comm": 0, "slip": 0})
    tt = [x for x in SW.turtle("GC", futdaily.bars("GC"), c, {}, spec) if x["units"] == 1 and not x.get("open")][0]
    assert abs(tt["pnl"] - tt["contracts"] * (tt["out"] - tt["in"]) * (1 if tt["side"] == "long" else -1) * spec[2]) < 1
    st, o = call("POST", "/swing/turtle/opt", {"cfg": {"uni": "fut"}})
    assert st == 200, o
    st, v = call("GET", "/swing/turtle")
    assert v["opt"]["done"] == len(SW.grid("turtle", "fut")) == 24
    top = v["opt"]["top"][0]
    assert top["groups"] and set(top["groups"]) <= set(futdaily.GROUPS) and "train" in next(iter(top["groups"].values()))
    # the account: compounding, unit limits, whole contracts
    st, r = call("POST", "/swing/turtle", {"cfg": {"uni": "fut", "sizing": "account", "acct": 1000000, "riskPct": 1, "maxDir": 12, "pyramid": True, "groups": None}})
    assert st == 200, r
    st, v = call("GET", "/swing/turtle")
    A = v["res"]["acct"]
    assert A and A["kind"] == "turtle" and A["all"]["taken"] > 0 and A["test"]["from"] == v["res"]["split"]["date"], A and A["all"]
    assert "ul" not in v["res"]["trades"][0]
    ts = [{"side": "long", "entry": "2020-01-02", "exit": "2020-01-10", "n0": 10.0, "pv": 50, "costC": 0, "ul": [["2020-01-02", 1000.0], ["2020-01-03", 500.0]]},
          {"side": "long", "entry": "2020-01-03", "exit": "2020-01-06", "n0": 10.0, "pv": 50, "costC": 0, "ul": [["2020-01-03", -500.0]]},
          {"side": "long", "entry": "2020-01-07", "exit": "2020-01-08", "n0": 10.0, "pv": 50, "costC": 0, "ul": [["2020-01-07", 100.0]]}]
    a = SW.account_turtle(ts, SW.clean("turtle", {"uni": "fut", "acct": 100000, "riskPct": 1, "maxDir": 2, "whole": True}))
    # trade 0: 1% of 100k ÷ (10 × 50) = 2 contracts; its add on 01-03 and trade 1 compete for the 2-unit limit: add taken first? entries before adds
    assert a["sizes"][0][0] == 2 and a["taken"] == 3 and a["cut"] == 1, a

    st, r = call("POST", "/swing/qulla", {"cfg": {"uni": "fut"}})
    st, v = call("GET", "/swing/qulla")
    assert v["cfg"]["uni"] == "both"



def test_intramom():
    import intramom as IM
    from datetime import date as _d, timedelta as _td
    st, v = call("GET", "/intramom")
    assert st == 200 and v["data"]["days"] == 0 and v["grid"] == 216
    st, r = call("POST", "/intramom", {"cfg": {}})
    assert st == 400 and "Download" in r["error"]
    call("PUT", "/alpaca", {"keyId": "PK" + "A" * 18, "secret": "s" * 30})
    call("PUT", "/fmp", {"key": "k" * 32})
    t = _d.today()
    IM.FROM = f"{(t - _td(days=100)).year}-{(t - _td(days=100)).month:02d}"           # 4 months in the tests (the fake Alpaca is slow)
    st, r = call("POST", "/intramom/data")
    assert st == 200, r
    st, v = call("GET", "/intramom")
    assert v["job"]["status"] == "done" and v["data"]["days"] > 50 and v["data"]["vix"] > 1000, (v["job"], v["data"])
    days = IM.load()[0]
    assert all(len(d[3]) == 26 for d in days) and days[5][3][-1] is not None
    for stop in ("vwap", "band", "opp"):
        st, r = call("POST", "/intramom", {"cfg": {"stop": stop, "vixMin": 0}})
        assert st == 200 and r["all"]["days"] > 30 and r["nTrades"] > 0, r.get("all")
        for x in r["trades"]:
            assert x["in"] < x["out"] and x["side"] in (1, -1)
    assert r["vixData"] and r["vix"] and r["dow"]
    # the mechanics on a hand-made day: σ = 1% at every mark, open 100, yesterday 100
    mk = lambda day, cl, vw: [day, 100.0, cl[-1], cl, vw, 389]
    flat = [100.0 * 1.01] * 26
    hist = [mk(f"2024-01-{i + 1:02d}", flat, [100.0] * 26) for i in range(14)]
    ctx_days = hist + [mk("2024-01-20", [100.5] + [101.5] * 3 + [102.0] * 10 + [100.5] * 12, [100.2] * 26)]
    ctx_days[13][2] = 100.0                               # yesterday's close 100
    ctx = IM.context(ctx_days, [])
    assert abs(ctx["sig"][14][0] - 0.01) < 1e-9
    c = IM.clean({"sizing": "full", "comm": 0, "slip": 0, "acct": 10000, "stop": "band"})
    daily, tr = IM.simulate(ctx_days, ctx, c, keep_trades=True)
    # 10:00: 101.5 > 101 → long; out at the first 30-minute check back inside the band (13:30; 13:15 is not a check)
    assert tr[0][1] == "10:00" and tr[0][3] == 1 and tr[0][2] == "13:30" and abs(tr[0][7] - 100 * (100.5 - 101.5)) < 1e-6, tr
    st, o = call("POST", "/intramom/opt", {"cfg": {}})
    assert st == 200, o
    st, v = call("GET", "/intramom")
    assert v["opt"]["complete"] and len(v["opt"]["top"]) == 20, v["job"]



def test_swing_whole_market():
    import allstocks as AL
    import store
    call("PUT", "/alpaca", {"keyId": "PK" + "A" * 18, "secret": "s" * 30})
    call("PUT", "/fmp", {"key": "k" * 32})
    syms = dict(AL.symbols(SUB))
    assert "AKO.A" in syms and "AKO-A" not in syms and not any(k.startswith(("XYZ", "ABC")) for k in syms)
    assert AL._alp("BRK-B") == "BRK.B" and AL._alp("XYZ-PA") is None and AL._alp("ABCDEFG") is None
    assert "MOMO" in syms and "GONE" in syms and syms["GONE"] == "2022-06-30" and "OLDY" not in syms and "ETFX" not in syms
    st, r = call("POST", "/swing/qulla", {"cfg": {"uni": "all"}})
    assert st == 400 and "whole market" in r["error"]
    AL.MIN_DV, AL.SHARD = 1e5, 2                          # every fake stock counts; 2 per shard to test the shards
    st, r = call("POST", "/swing/all")
    assert st == 200, r
    st, v = call("GET", "/swing/qulla")
    assert v["allJob"]["status"] == "done", v["allJob"]
    ix = AL.index()
    assert set(ix["syms"]) == {"MOMO", "RUNR", "SLOW", "GONE", "AKO.A"} and ix["shards"] == 3, ix          # QQBAD refused by Alpaca: dropped
    gone = AL.bars("GONE")
    assert gone[-1][0] <= "2022-06-30"
    st, r = call("POST", "/swing/qulla", {"cfg": {"uni": "all", "minAdr": 0.0, "prior": 0.0, "tight": 0.5, "minDv": 0}})
    assert st == 200, r
    st, v = call("GET", "/swing/qulla")
    assert v["res"]["nSyms"] == 5 and v["res"]["n"] > 0, v["job"]
    assert SW_clean_default()["uni"] == "all"


def SW_clean_default():
    import swing
    return swing.clean("qulla", {})


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
