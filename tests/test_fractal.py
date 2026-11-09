"""Fractal engine vs the TradeWellX Data tab (QQQ, Wave 10, 15-minute bars).

    python tests/test_fractal.py"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.path.insert(0, os.path.join(HERE, "fixtures"))

sys.path.insert(0, os.path.join(HERE, "..", "dev"))
import json  # noqa: E402
import tempfile  # noqa: E402
from datetime import timedelta  # noqa: E402

os.environ.update({"TABLE": "local", "KMS_KEY_ID": "local", "DATA_BUCKET": "local"})
import fakes  # noqa: E402

fakes.install(fake_databento=True, data_dir=tempfile.mkdtemp(), persist=False)

import api  # noqa: E402
import fractal  # noqa: E402
import futdata  # noqa: E402
import qqq_closes  # noqa: E402
import util  # noqa: E402

SUB = "u1"

# Alpaca's clock for these tests: Saturday 2026-11-07 (the last session is over), so the results don't depend on
# whether the tests run during market hours (the live IEX top-up has its own test with its own clock)
import alpaca as _alpaca  # noqa: E402
from datetime import datetime as _DT  # noqa: E402


class _Sat(_DT):
    @classmethod
    def utcnow(cls):
        return _DT(2026, 11, 7, 18, 0)


_alpaca.datetime = _Sat


def call(method, path, body=None, q=None):
    ev = {"rawPath": path, "queryStringParameters": q or {}, "body": json.dumps(body) if body is not None else None,
          "requestContext": {"http": {"method": method}, "stage": "$default", "authorizer": {"jwt": {"claims": {"sub": SUB}}}}}
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"] or "null")

EXPECTED = [  # values read off TradeWellX
    ("ma", "2026-10-27 09:30", 714.705), ("ma", "2026-10-27 11:45", 718.0924), ("ma", "2026-10-27 14:00", 721.34),
    ("ma", "2026-10-27 14:45", 720.8), ("ma", "2026-10-28 10:30", 722.0125), ("ma", "2026-10-28 12:00", 721.8425),
    ("ma", "2026-10-28 15:45", 723.505), ("ma", "2026-10-29 09:30", 720.295), ("ma", "2026-10-29 11:45", 716.045),
    ("ma", "2026-10-29 15:15", 713.4875), ("ma", "2026-10-30 09:30", 705.595), ("ma", "2026-10-30 11:30", 704.13),
    ("slowMa", "2026-10-29 14:15", 715.28), ("slowMa", "2026-10-29 14:30", 715.0125), ("slowMa", "2026-10-30 09:30", 706.755),
    ("pivot", "2026-10-27 09:30", 713.11), ("pivot", "2026-10-27 15:00", 720.8), ("pivot", "2026-10-28 12:00", 722.0625),
    ("pivot", "2026-10-28 12:45", 721.8314), ("pivot", "2026-10-29 09:30", 723.505), ("pivot", "2026-10-29 15:45", 713.4875),
    ("pivot", "2026-10-30 09:30", 713.8525),
    ("direction", "2026-10-28 12:00", 1), ("direction", "2026-10-29 09:30", -1), ("direction", "2026-10-29 15:45", -1),
    ("barsElapsed", "2026-10-28 13:15", 0), ("barsElapsed", "2026-10-28 13:45", 1), ("barsElapsed", "2026-10-29 09:30", 0),
]
# Known gap: TradeWellX moved the pivot to 721.34 at 2026-10-27 14:00, when the MA went flat (it turned down at 14:15).


def test_against_tradewellx():
    s = {x["t"]: x for x in fractal.compute(qqq_closes.bars(), 10)["series"]}
    bad = [(k, t, v, s[t][k]) for k, t, v in EXPECTED if s[t][k] is None or abs(s[t][k] - v) > 0.006]
    assert not bad, bad


def test_levels_match_tradewellx_clusternode():
    # TradeWellX ClusterWave marks (QQQ Wave 10, 15 min, 10 days, 11/06/2026, pivot 658.06): the 14 L waves' extremes
    # at today's pivot, and what ClusterNode(-p) returned for each level
    piv = 658.06
    marks = [647.32, 657.48, 657.48, 657.62, 655.74, 656.32, 657.04, 651.82, 656.46, 651.09, 654.72, 613.21, 642.82, 630.05]
    want = {0.80: 657.48, 0.50: 655.74, 0.20: 647.32, 0.10: 630.05, 0.02: 613.21}
    got = fractal.levels([1 - m / piv for m in marks], fractal.BEAR_P)
    for p, m in zip(fractal.BEAR_P, got):
        assert abs(piv * (1 - m) - want[p]) < 0.006, (p, piv * (1 - m), want[p])
    up = [0.001 * k for k in range(1, 15)]                          # 14 H waves: 10% and 5% land on the same mark
    lv = fractal.levels(up, fractal.BULL_P)
    assert lv[3] == lv[4] == up[-2] and lv == sorted(lv)


def test_daily_pivots_match_tradewellx_warehouse():
    # TradeWellX DataWarehouse table Daily_QQQ (Wave 1, 300 days to 11/06/2026): the vector on every day, and the pivot on
    # every pivot change where the rows involved were stored at the 4:00 PM close (stale intraday rows are skipped)
    import csv
    from datetime import datetime
    rows = list(csv.DictReader(open(os.path.join(HERE, "fixtures", "tradewellx_daily_qqq.csv"))))
    dt = lambda r: datetime.strptime(r["LastTradeDateTime"], "%m/%d/%Y %I:%M:%S %p")
    rows.sort(key=dt)
    final = [r["LastTradeDateTime"].endswith("4:00:00 PM") for r in rows]
    bars = [[dt(r).strftime("%Y-%m-%d"), float(r["OpenPrice"]), float(r["HighPrice"]), float(r["LowPrice"]), float(r["ClosePrice"]), 0] for r in rows]
    S = fractal.compute(bars, wave=1)["series"]
    assert all(abs(S[i]["ma"] - float(rows[i]["Vector"])) < 0.006 for i in range(len(rows)))
    checked = 0
    for i in range(3, len(rows)):
        if rows[i]["PivotValue"] == rows[i - 1]["PivotValue"] or not (final[i] and final[i - 1] and final[i - 2]):
            continue
        assert abs(S[i]["pivot"] - float(rows[i]["PivotValue"])) < 0.006, (bars[i][0], rows[i]["PivotValue"], S[i]["pivot"])
        checked += 1
    assert checked >= 50, checked


def test_daily_macro_matches_tradewellx():
    # warehouse SIGNALS_General at 11/08/2026: Daily Wave 1 and Wave 7 both Macro S018
    import csv
    from datetime import datetime
    rows = list(csv.DictReader(open(os.path.join(HERE, "fixtures", "tradewellx_daily_qqq.csv"))))
    rows.sort(key=lambda r: datetime.strptime(r["LastTradeDateTime"], "%m/%d/%Y %I:%M:%S %p"))
    closes = [float(r["ClosePrice"]) for r in rows]
    for i in range(1, len(rows)):          # stale rows: the true close is the pivot set from it the next day
        if rows[i]["PivotValue"] != rows[i - 1]["PivotValue"] and not rows[i - 1]["LastTradeDateTime"].endswith("4:00:00 PM") \
                and abs(float(rows[i]["PivotValue"]) - closes[i - 1]) < 1.5:
            closes[i - 1] = float(rows[i]["PivotValue"])
    bars = [[str(i), c, c, c, c, 0] for i, c in enumerate(closes)]
    for wave in (1, 7):
        assert fractal.compute(bars, wave)["series"][-1]["macroTrend"] == "S018"


def test_cold_midline():
    m = fractal.midline([1, 5, 3], 10, partial=True)
    assert m == [1, 3, 3] and fractal.midline([1, 5, 3], 10) == [None, None, None]


def test_macro_trend_and_stdev():
    # MacroTrend vs TradeWellX: L from 10/27 15:30 through 10/28, S from 10/29 09:30 (fixture starts 10/26 12:30)
    s = {x["t"]: x for x in fractal.compute(qqq_closes.bars(), 10)["series"]}
    for t, x in s.items():
        if x["macro"] is None:
            continue
        if "2026-10-27 15:30" <= t <= "2026-10-28 15:45":
            assert x["macro"] == 1, t
        if t >= "2026-10-29 09:30":
            assert x["macro"] == -1 and x["macroTrend"].startswith("S"), t
    assert s["2026-10-29 09:45"]["macroTrend"] == "S002"
    assert s["2026-10-28 13:00"]["stdev"] < 0.1 < s["2026-10-29 10:00"]["stdev"] < s["2026-10-30 10:45"]["stdev"]
    assert abs(fractal.stdev_pct([1, 3], 2)[1] - 50.0) < 1e-9 and fractal.macro_trend([1, 1, 5], 2, "sma")[2] == 1
    assert fractal.macro_trend([10, 9, 8], 30) == [1, -1, -1]


def test_frozen_side():
    # a run below the pivot: red must not change from bar to bar while green follows the vector
    import random
    rnd, px, bars = random.Random(7), 600.0, []
    for i in range(500):
        o = px
        px *= 1 + rnd.gauss(0, 0.002) + 0.0006 * __import__("math").sin(i / 9)
        bars.append([f"t{i:04d}", o, max(o, px) * 1.0005, min(o, px) * 0.9995, px, 1])
    r = fractal.compute(bars, 10, 40)
    r["close_at"] = [b[4] for b in bars[40:]]
    S = r["series"]
    checked = 0
    for k in range(60, len(S) - 1):
        if S[k]["pivot"] and S[k + 1]["pivot"] and r["close_at"][k] < S[k]["pivot"] and r["close_at"][k + 1] < S[k + 1]["pivot"]:
            a, b = fractal.levels_at(r, k), fractal.levels_at(r, k + 1)
            if a["bear"] and b["bear"]:
                assert [x["price"] for x in a["bear"]] == [x["price"] for x in b["bear"]], k
                checked += 1
    assert checked > 20, checked


def test_bases_in_a_drop():
    waves = [{"side": "H", "move": 0.01 * k, "count": 1} for k in range(1, 6)] + [{"side": "L", "move": 0.01 * k, "count": 1} for k in range(1, 6)]
    cl = fractal.clusters(waves, 100.0, ma=95.0)                     # vector below the pivot: green starts from it
    assert cl["bullBase"] == 95.0 and cl["bearBase"] == 100.0
    assert abs(cl["bull"][0]["price"] - 95.0 * (1 + cl["bull"][0]["move"])) < 1e-9 and cl["bull"][0]["price"] < 100.0


def test_clusters_and_odds():
    waves = [{"side": "H", "move": m, "count": c} for m, c in ((0.01, 1), (0.02, 2), (0.03, 2), (0.04, 5))] + \
            [{"side": "L", "move": m, "count": c} for m, c in ((0.01, 1), (0.05, 3))]
    cl = fractal.clusters(waves, 100.0)
    bull, bear = {x["p"]: x["price"] for x in cl["bull"]}, {x["p"]: x["price"] for x in cl["bear"]}
    assert abs(bull[0.50] - 102.0) < 1e-9 and abs(bear[0.50] - 99.0) < 1e-9 and abs(bull[0.05] - 104.0) < 1e-9
    o = {r["count"]: r for r in fractal.odds(waves, "H")}
    assert o[2]["n"] == 2 and abs(o[2]["atLeast"] - 0.75) < 1e-9 and abs(o[5]["atLeast"] - 0.25) < 1e-9


def _check_view(r, wave):
    assert r["waves"] and r["pivot"] and r["clusters"]["bull"] and r["clusters"]["bear"], r.keys()
    bull = {x["p"]: x["price"] for x in r["clusters"]["bull"]}
    bear = {x["p"]: x["price"] for x in r["clusters"]["bear"]}
    c = r["clusters"]                       # each side from its own base; one side frozen from the last cross
    assert c["frozen"] in ("bull", "bear")
    assert all(bear[p] <= c["bearBase"] and bull[p] >= c["bullBase"] for p in (0.80, 0.50, 0.20, 0.10))
    assert [bull[p] for p in (0.80, 0.50, 0.20, 0.10, 0.05)] == sorted(bull[p] for p in (0.80, 0.50, 0.20, 0.10, 0.05))
    assert [bear[p] for p in (0.80, 0.50, 0.20, 0.10, 0.02)] == sorted((bear[p] for p in (0.80, 0.50, 0.20, 0.10, 0.02)), reverse=True)
    st = r["steps"]
    assert st and all(a["i1"] < b["i0"] for a, b in zip(st, st[1:])) and st[-1]["i1"] == len(r["bars"]) - 1
    assert all(x["bull"][0]["price"] >= x["pivot"] >= x["bear"][0]["price"] for x in st)


def test_alpaca_qqq():
    st, r = call("PUT", "/alpaca", {"keyId": "XX1234567890", "secret": "s" * 40})
    assert st == 400, r                                               # refused by (fake) Alpaca
    st, r = call("PUT", "/alpaca", {"keyId": "PK1234567890AB", "secret": "s" * 40})
    assert st == 200 and r["connected"] and r["last4"] == "90AB" and r["feed"] == "sip", r
    import alpaca
    from unittest import mock
    from datetime import datetime as _dt

    class Sat(_dt):                                                   # a fixed clock: the last session is over
        @classmethod
        def utcnow(cls):
            return _dt(2026, 11, 7, 18, 0)
    with mock.patch.object(alpaca, "datetime", Sat):
        st, r = call("GET", "/fractal", q={"sym": "QQQ", "wave": "10", "days": "10", "agg": "15"})
        st2, r2 = call("GET", "/fractal", q={"sym": "QQQ", "wave": "10", "days": "10", "agg": "15", "close16": "0"})
    assert st == 200, r
    assert len({b[0][:10] for b in r["bars"]}) == 10 and all("09:30" <= b[0][11:16] < "16:00" for b in r["bars"][:-1])
    assert r["bars"][-1][0].endswith("16:00") and len(r["bars"]) == 261, len(r["bars"])   # + the closing print, like TradeWellX
    assert st2 == 200 and len(r2["bars"]) == 260 and not r2["bars"][-1][0].endswith("16:00")
    _check_view(r, 10)
    st, r = call("GET", "/fractal", q={"sym": "QQQ", "wave": "10", "days": "5", "stats": "40", "agg": "15"})
    assert st == 200, r
    assert len({b[0][:10] for b in r["bars"]}) == 5 and len(r["series"]) == len(r["bars"])
    assert r["params"]["statsFrom"] < r["bars"][0][0] and r["waves"][0]["start"] < r["bars"][0][0]   # waves from before the window
    _check_view(r, 10)
    st, r = call("GET", "/fractal", q={"sym": "QQQ", "wave": "7", "days": "50", "agg": "60"})
    assert st == 200, r
    hrs = sorted({b[0][11:16] for b in r["bars"]})
    assert hrs == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30", "16:00"], hrs
    assert len({b[0][:10] for b in r["bars"]}) == 50 and len(r["bars"]) == 351, len(r["bars"])   # 50 x 7 + the 16:00 bar
    _check_view(r, 7)
    st, r = call("GET", "/fractal", q={"sym": "QQQ", "wave": "10", "days": "10", "agg": "15", "cold": "1"})
    assert st == 200 and r["params"]["cold"], r
    assert r["series"][0]["ma"] is not None and r["waves"][0]["start"] == r["bars"][0][0]   # MA from bar 1, first wave on bar 1
    st, r = call("GET", "/fractal", q={"sym": "QQQ", "wave": "500"})
    assert st == 400


def test_databento_nq():
    st, r = call("PUT", "/databento", {"key": "db-" + "A" * 29})
    assert st == 200, r
    today = util.now_ny().date()
    futdata.download_candles(SUB, "NQ", (today - timedelta(days=30)).isoformat(), today.isoformat())
    st, r = call("GET", "/fractal", q={"sym": "NQ", "wave": "10", "days": "10", "agg": "15"})
    assert st == 200, r
    assert r["params"]["source"] == "databento" and len({b[0][:10] for b in r["bars"]}) == 10
    assert any(b[0][11:16] < "09:30" for b in r["bars"])              # overnight included, like NQ=F
    _check_view(r, 10)


def test_uw_gamma():
    import uwhales
    st, r = call("PUT", "/uw", {"token": "bad_token_" + "x" * 20})
    assert st == 400, r
    st, r = call("PUT", "/uw", {"token": "uw_" + "k" * 30})
    assert st == 200 and r["connected"] and r["last4"] == "kkkk", r
    st, r = call("GET", "/gamma", q={"syms": "QQQ,SPY", "source": "oi"})
    assert st == 200, r
    for g in r["items"]:
        c, u, px = g["computed"], g["uw"], g["price"]
        assert c["call_wall"] > px > c["put_wall"] and g["strikes"]
        assert c["call_wall"] == u["call_wall"] and c["put_wall"] == u["put_wall"] and c["gamma_magnet"] == u["gamma_magnet"]
        assert abs(c["gamma_flip"] - u["gamma_flip"]) < 0.01
        if g["sym"] == "QQQ":                     # NQ candles are stored (test_databento_nq): levels in NQ points
            f = g["fut"]
            assert f["sym"] == "NQ" and abs(f["computed"]["call_wall"] - c["call_wall"] * f["ratio"]) <= 0.125
    # the wall rules on a hand-made profile
    rows = [{"strike": k, "net": n} for k, n in ((95, 5), (98, -2), (99, 1), (101, 3), (103, 9), (104, -1))]
    w = uwhales.walls(rows, 100)
    assert w["call_wall"] == 103 and w["put_wall"] == 95 and w["gamma_magnet"] == 103 and w["gamma_flip"] == 98.67


def test_landscape():
    import uwhales
    st, r = call("GET", "/landscape", q={"sym": "QQQ"})
    assert st == 200, r
    assert r["floor"] < r["pml"] < r["ceiling"] and r["pmlValue"] < 0 and r["contracts"] > 50
    assert all(z["holders"] >= r["pmlValue"] - 1e-6 for z in r["curve"])
    t = {x["move"]: x for x in r["targets"]}
    assert t[0.0]["best"] is None and t[0.01]["best"]["contract"][9] == "C" and t[-0.01]["best"]["contract"][9] == "P"
    st, r2 = call("GET", "/landscape", q={"sym": "QQQ", "expiry": r["expiries"][1]["expiry"]})
    assert st == 200 and r2["expiry"] == r["expiries"][1]["expiry"]
    # TradeWellX's own row: C651 bought at the 9.93 ask, worth 665.94 - 651 = 14.94 at expiry -> 50.5%
    assert uwhales._occ("QQQ261109C00651000") == ("2026-11-09", "C", 651.0)
    assert round((max(0, 665.94 - 651) - 9.93) / 9.93 * 100, 1) == 50.5


def test_mm_history():
    import uwhales
    st, a = call("POST", "/mmhistory/snapshot", {"sym": "QQQ"})
    assert st == 200 and {"price", "pml", "ceiling", "floor", "pa", "pb", "ca", "cb"} <= set(a), a
    st, b = call("POST", "/mmhistory/snapshot", {"sym": "NQ"})          # NQ records QQQ; same minute replaces the row
    st, h = call("GET", "/mmhistory", q={"sym": "QQQ", "days": "3"})
    assert st == 200 and h["days"] and len(h["days"][-1]["rows"]) == 1, h
    assert uwhales.stock_session(__import__("datetime").datetime(2026, 11, 6, 10, 0)) and not uwhales.stock_session(__import__("datetime").datetime(2026, 11, 7, 10, 0))


def test_mm_backfill():
    import jobs
    import uwhales
    n = uwhales.backfill_day(SUB, "QQQ", "2026-11-06")
    assert n > 60, n                                  # 78 five-minute snapshots in a session
    h = uwhales.history(SUB, "QQQ", 5)
    day = next(d for d in h if d["date"] == "2026-11-06")
    r0, r1 = day["rows"][0], day["rows"][-1]
    assert r0["t"] == "09:35" and r1["t"] == "16:00" and r1["floor"] < r1["pml"] < r1["ceiling"]
    assert r1["pa"] + r1["pb"] + r1["ca"] + r1["cb"] > r0["pa"] + r0["pb"] + r0["ca"] + r0["cb"]   # volume adds up
    jobs.INVOKE = lambda payload: jobs.handler(payload)            # the job, inline
    st, j = call("POST", "/mmhistory/backfill", {"sym": "QQQ", "days": 2, "only": True})
    st, j = call("GET", "/mmhistory/status")
    assert j["status"] == "done", j


def test_volproj():
    import volproj
    # TradeWellX's daily table: 11/06/2026 is 1H on Wave 1 (11/05 closed below its pivot, 11/06 above)
    import csv
    from datetime import datetime
    rows = list(csv.DictReader(open(os.path.join(HERE, "fixtures", "tradewellx_daily_qqq.csv"))))
    rows.sort(key=lambda r: datetime.strptime(r["LastTradeDateTime"], "%m/%d/%Y %I:%M:%S %p"))
    bars = [[datetime.strptime(r["LastTradeDateTime"], "%m/%d/%Y %I:%M:%S %p").strftime("%Y-%m-%d"), float(r["OpenPrice"]),
             float(r["HighPrice"]), float(r["LowPrice"]), float(r["ClosePrice"]), 0] for r in rows]
    for fwd in (1, 2, 3):
        p = volproj.project(bars[-200:], 1, fwd)
        b, d = p["bull"], p["bear"]
        assert p["state"] == "1H" and p["matches"] > 20, p["matches"]
        assert b["max"] >= b["maxAvg"] >= b["avg"] >= b["minAvg"] and d["max"] <= d["maxAvg"] <= d["avg"] <= d["minAvg"]
        assert abs(b["levels"]["max"] - p["price"] * (1 + b["max"])) < 1e-9
    p1, p3 = volproj.project(bars[-200:], 1, 1), volproj.project(bars[-200:], 1, 3)
    assert p3["bull"]["avg"] > p1["bull"]["avg"] and p3["bear"]["avg"] < p1["bear"]["avg"]    # more days, wider range
    r = volproj.project(bars[-200:], 1, 2, asof="2026-10-29")
    assert r["date"] == "2026-10-29" and r["unfold"]["complete"] and r["unfold"]["days"] == ["2026-10-30", "2026-11-02"]
    w = volproj.project(bars[-200:], 1, 1, match="weekday")
    assert w["matches"] >= 30
    # a rare state with no earlier exact match widens to the same side (7L -> 7L or longer, then any L)
    r = volproj.project(bars[-200:], 1, 1)
    st0 = volproj.fractal.compute(bars[-200:], 1)
    assert r["matchedBy"] == "state"
    for asof in [b[0] for b in bars[-60:]]:
        q = volproj.project(bars[-200:], 1, 2, asof=asof)
        assert q["matches"] > 0 and q["matchedBy"] in ("state", "side, count >= %s" % q["state"][:-1], "side")
    st, a = call("GET", "/volproj", q={"sym": "QQQ", "fwd": "2"})
    assert st == 200 and a["fwd"] == 2 and a["bull"]["levels"]["maxAvg"] > a["price"] > a["bear"]["levels"]["maxAvg"], a


def test_live_top_up():
    # SIP is 15 min late: at 09:40 ET the 09:30 bar comes from IEX
    import alpaca
    from unittest import mock
    from datetime import datetime as _dt

    class D(_dt):
        @classmethod
        def utcnow(cls):
            return _dt(2026, 11, 9, 14, 40)
    with mock.patch.object(alpaca, "datetime", D), mock.patch.object(alpaca, "status", lambda sub: {"feed": "sip", "connected": True}):
        b = alpaca.bars(SUB, "QQQ", 3, 15)
    ts = [x[0] for x in b]
    assert ts[-1] == "2026-11-09 09:30" and len(ts) == len(set(ts)) and ts == sorted(ts), ts[-3:]


if __name__ == "__main__":
    test_against_tradewellx()
    test_levels_match_tradewellx_clusternode()
    test_daily_pivots_match_tradewellx_warehouse()
    test_daily_macro_matches_tradewellx()
    test_cold_midline()
    test_macro_trend_and_stdev()
    test_frozen_side()
    test_bases_in_a_drop()
    test_clusters_and_odds()
    test_alpaca_qqq()
    test_databento_nq()
    test_uw_gamma()
    test_landscape()
    test_mm_history()
    test_mm_backfill()
    test_volproj()
    test_live_top_up()
    print("fractal: all tests passed")
