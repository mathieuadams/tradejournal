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
           "acceptanceDateTime": ["2025-01-28T16:05:00.000Z", "", "", "2025-04-29T06:59:00.000Z", "2025-04-29T10:00:00.000Z", "2025-07-29T18:01:00.000Z"],
           "accessionNumber": ["a", "b", "c", "d", "e", "f"]}
    r = F.earnings_filings({"filings": {"recent": rec}})
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
    days = [f"2025-01-{d:02d}" for d in range(1, 32)] + [f"2025-02-{d:02d}" for d in range(1, 29)] + [f"2025-03-{d:02d}" for d in range(1, 32)]
    bars = [[d, 100 + i, 101 + i, 99 + i, 100 + i, 1] for i, d in enumerate(days)]
    bench = [[d, 50, 50, 50, 50, 1] for d in days]
    amc = F.reaction({"date": "2025-01-30", "timing": "amc"}, bars, bench)
    assert amc["base"] == "2025-01-30" and amc["day0"] == "2025-01-31" and abs(amc["r1"] - (130 / 129 - 1)) < 1e-12 and amc["r1ab"] == amc["r1"]
    bmo = F.reaction({"date": "2025-01-30", "timing": "bmo"}, bars, bench)
    assert bmo["base"] == "2025-01-29" and bmo["day0"] == "2025-01-30" and abs(bmo["gap"] - (129 / 128 - 1)) < 1e-12
    assert abs(bmo["drift20"] - (bars[48][4] / bars[29][4] - 1)) < 1e-12
    assert F.reaction({"date": "2025-01-05", "timing": "bmo"}, bars, bench) is None        # not enough history before


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
    assert {"Guidance", "Beat / raise", "pre_signal", "Revenue YoY"} <= labels, labels
    assert all(x["timing"] != "unknown" for x in s["rows"] if x["src"] == "csv")       # timing filled from the same-day 8-K
    st, p = call("GET", "/fund/project", None, {"sym": "BBB"})
    assert st == 200 and [h["label"] for h in p["horizons"]] == ["1 day", "5 days", "1 month", "2 months"], p
    assert p["next"]["date"] >= p["now"]["asOf"] and p["neighbours"] and p["signals"] and p["ownN"] > 10
    h = p["horizons"][3]
    assert h["similar"]["ab"]["n"] > 0 and h["own"]["ab"]["p25"] <= h["own"]["ab"]["p75"] and h["volRange"] > 0
    assert any(r["r40ab"] is not None for r in s["rows"])
    st, r = call("POST", "/fund/events", {"csv": "foo,bar\n1,2"})
    assert st == 400
    st, r = call("DELETE", "/fund/events")
    assert r["n"] == 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
