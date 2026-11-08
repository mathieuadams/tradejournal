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
    assert abs(fractal.stdev_pct([1, 3], 2)[1] - 50.0) < 1e-9 and fractal.macro_trend([1, 1, 5], 2)[2] == 1


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
    st, r = call("GET", "/fractal", q={"sym": "QQQ", "wave": "10", "days": "10", "agg": "15"})
    assert st == 200, r
    assert len({b[0][:10] for b in r["bars"]}) == 10 and all("09:30" <= b[0][11:16] < "16:00" for b in r["bars"])
    _check_view(r, 10)
    st, r = call("GET", "/fractal", q={"sym": "QQQ", "wave": "10", "days": "5", "stats": "40", "agg": "15"})
    assert st == 200, r
    assert len({b[0][:10] for b in r["bars"]}) == 5 and len(r["series"]) == len(r["bars"])
    assert r["params"]["statsFrom"] < r["bars"][0][0] and r["waves"][0]["start"] < r["bars"][0][0]   # waves from before the window
    _check_view(r, 10)
    st, r = call("GET", "/fractal", q={"sym": "QQQ", "wave": "7", "days": "50", "agg": "60"})
    assert st == 200, r
    hrs = sorted({b[0][11:16] for b in r["bars"]})
    assert hrs == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"], hrs
    assert len({b[0][:10] for b in r["bars"]}) == 50 and len(r["bars"]) == 350, len(r["bars"])
    _check_view(r, 7)
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


if __name__ == "__main__":
    test_against_tradewellx()
    test_levels_match_tradewellx_clusternode()
    test_macro_trend_and_stdev()
    test_frozen_side()
    test_bases_in_a_drop()
    test_clusters_and_odds()
    test_alpaca_qqq()
    test_databento_nq()
    print("fractal: all tests passed")
