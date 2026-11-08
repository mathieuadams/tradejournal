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


def test_clusters_and_odds():
    waves = [{"side": "H", "move": m, "count": c} for m, c in ((0.01, 1), (0.02, 2), (0.03, 2), (0.04, 5))] + \
            [{"side": "L", "move": m, "count": c} for m, c in ((0.01, 1), (0.05, 3))]
    cl = fractal.clusters(waves, 100.0)
    bull, bear = {x["p"]: x["price"] for x in cl["bull"]}, {x["p"]: x["price"] for x in cl["bear"]}
    assert abs(bull[0.50] - 103.0) < 1e-9 and abs(bear[0.50] - 99.0) < 1e-9 and abs(bull[0.05] - 104.0) < 1e-9
    o = {r["count"]: r for r in fractal.odds(waves, "H")}
    assert o[2]["n"] == 2 and abs(o[2]["atLeast"] - 0.75) < 1e-9 and abs(o[5]["atLeast"] - 0.25) < 1e-9


def _check_view(r, wave):
    assert r["waves"] and r["pivot"] and r["clusters"]["bull"] and r["clusters"]["bear"], r.keys()
    bull = {x["p"]: x["price"] for x in r["clusters"]["bull"]}
    bear = {x["p"]: x["price"] for x in r["clusters"]["bear"]}
    assert all(bear[p] <= r["pivot"] <= bull[p] for p in (0.80, 0.50, 0.20, 0.10))
    assert r["oddsH"][0]["atLeast"] == 1.0 and r["oddsL"][0]["atLeast"] == 1.0
    assert r["params"]["wave"] == wave


def test_alpaca_qqq():
    st, r = call("PUT", "/alpaca", {"keyId": "XX1234567890", "secret": "s" * 40})
    assert st == 400, r                                               # refused by (fake) Alpaca
    st, r = call("PUT", "/alpaca", {"keyId": "PK1234567890AB", "secret": "s" * 40})
    assert st == 200 and r["connected"] and r["last4"] == "90AB" and r["feed"] == "sip", r
    st, r = call("GET", "/fractal", q={"sym": "QQQ", "wave": "10", "days": "10", "agg": "15"})
    assert st == 200, r
    assert len({b[0][:10] for b in r["bars"]}) == 10 and all("09:30" <= b[0][11:16] < "16:00" for b in r["bars"])
    _check_view(r, 10)
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
    test_clusters_and_odds()
    test_alpaca_qqq()
    test_databento_nq()
    print("fractal: all tests passed")
