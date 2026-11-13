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
    assert {"Guidance", "Beat / raise", "pre_signal", "Revenue YoY (reported)", "Accumulation (Chaikin money flow 20d)"} <= labels, labels
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
    assert s["sources"]["fmp"] > 30 and s["withEst"] > 30 and any("DDD" in x for x in s["skipped"]), (s["sources"], s["skipped"][:3])
    row = next(x for x in s["rows"] if x.get("epsSurprise") is not None and x.get("ownAbs") is not None)
    for k in ("pre1ab", "pre5ab", "pre21ab", "pre63ab", "relVol5", "cmf20", "obv20", "sma50", "estEpsG", "epsSurprise", "drift5"):
        assert k in row, k
    assert {t["key"] for t in s["model"]["targets"]} >= {"r1ab", "abs1", "r40ab", "drift20"}
    st, pg = call("GET", "/fund/study/rows", None, {"page": "0"})
    assert st == 200 and len(pg["rows"]) == s["n"] and pg["pages"] == 1
    st, p = call("GET", "/fund/project", None, {"sym": "AAA"})
    assert st == 200 and p["next"]["source"] == "FMP" and p["next"]["epsEst"] is not None and "relVol5" in p["now"], p["next"]
    st, r = call("POST", "/fund/universe", {"tickers": []})
    assert r["universe"] == []


def _synth(n=3000, seed=7):
    """Releases with a planted pre-release signal: day-0 return = 0.02 × cmf20 + noise; size = 0.5 × vol20 + noise."""
    import random
    from datetime import date, timedelta
    rnd = random.Random(seed)
    rows, d0 = [], date(2015, 1, 2)
    for i in range(n):
        d = d0 + timedelta(days=i * 3650 // n)
        cmf, vol = rnd.uniform(-0.5, 0.5), rnd.uniform(0.01, 0.05)
        r1 = 0.02 * cmf / 0.29 + rnd.gauss(0, 0.04)
        r = {"sym": f"S{i % 300}", "date": d.isoformat(), "base": d.isoformat(), "day0": (d + timedelta(days=1)).isoformat(),
             "t1": (d + timedelta(days=1)).isoformat(), "t5": (d + timedelta(days=7)).isoformat(), "t20": (d + timedelta(days=28)).isoformat(),
             "t40": (d + timedelta(days=56)).isoformat(), "cmf20": cmf, "vol20": vol, "pre21ab": rnd.gauss(0, 0.05),
             "r1ab": r1 + (abs(rnd.gauss(0, vol * 2)) * (1 if r1 > 0 else -1)), "r5ab": rnd.gauss(0, 0.05), "r20ab": rnd.gauss(0, 0.08),
             "r40ab": rnd.gauss(0, 0.1), "drift5": rnd.gauss(0, 0.03), "drift20": rnd.gauss(0, 0.05), "drift40": rnd.gauss(0, 0.07)}
        rows.append(r)
    return rows


def test_model_finds_planted_signal():
    import model
    m = model.evaluate(_synth(), min_train=300)
    t = {x["key"]: x for x in m["targets"]}
    assert t["r1ab"]["verdict"] == "edge" and t["r1ab"]["hit"] > t["r1ab"]["hitBase"] and t["r1ab"]["icQ"]["t"] > 3, t["r1ab"]
    assert t["r1ab"]["coef"][0]["key"] == "cmf20" and t["r1ab"]["spread"]["mean"] > 0.01
    assert t["abs1"]["ic"] > 0.1                        # size follows volatility
    assert all(t[k]["verdict"] != "edge" for k in ("r20ab", "r40ab", "drift20")), [(k, t[k]["verdict"], t[k]["icQ"]) for k in ("r20ab", "r40ab", "drift20")]
    pr = model.apply(m, {"cmf20": 0.4, "vol20": 0.02})
    assert next(x for x in pr if x["key"] == "r1ab")["pred"] > 0.01


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
