"""Backend tests: no AWS, no network (fake DynamoDB / KMS / S3 folder / Databento from dev/fakes.py).

    python tests/test_backend.py"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
sys.path.insert(0, os.path.join(HERE, "..", "dev"))
os.environ.update({"TABLE": "local", "KMS_KEY_ID": "local", "DATA_BUCKET": "local"})

import fakes  # noqa: E402

TMP = tempfile.mkdtemp()
fakes.install(fake_databento=True, data_dir=TMP, persist=False)

import api  # noqa: E402
import databento  # noqa: E402
import db  # noqa: E402
import futdata  # noqa: E402
import jobs  # noqa: E402
import strategy  # noqa: E402
import util  # noqa: E402

SUB = "u1"
KEY = "db-" + "A" * 29
jobs.INVOKE = lambda payload: jobs.handler(payload, None)          # background jobs run inline
TODAY = util.now_ny().date()
D0 = (TODAY - timedelta(days=260)).isoformat()


def call(method, path, body=None, q=None):
    ev = {"rawPath": path, "queryStringParameters": q or {}, "body": json.dumps(body) if body is not None else None,
          "requestContext": {"http": {"method": method}, "stage": "$default", "authorizer": {"jwt": {"claims": {"sub": SUB, "email": "a@b.c"}}}}}
    r = api.handler(ev, None)
    return r["statusCode"], json.loads(r["body"] or "null")


def _expect(code, fn):
    try:
        fn()
    except Exception as e:
        assert getattr(e, "status", None) == code, (e, code)
        return str(e)
    raise AssertionError("no error")


def test_01_databento_key():
    assert "db-" in _expect(400, lambda: databento.save_key(SUB, "abc"))
    st, r = call("PUT", "/databento", {"key": KEY})
    assert st == 200 and r["connected"] and r["last4"] == "AAAA" and r["range"]["end"], r
    st, r = call("PUT", "/databento", {"maxCost": 0.5, "autoFp": False})
    assert r["maxCost"] == 0.5 and r["autoFp"] is False and r["auto"] is True
    assert databento.key(SUB) == KEY
    st, me = call("GET", "/me")
    assert me["databento"]["connected"] and me["specs"]["NQ"]["pv"] == 20


def test_02_candles_download():
    st, r = call("POST", "/data/estimate", {"kind": "candles", "syms": ["NQ", "ES"], "from": D0, "to": TODAY.isoformat()})
    assert st == 200 and r["usd"] > 0
    st, r = call("POST", "/data/download", {"kind": "candles", "syms": ["NQ", "ES"], "from": D0, "to": TODAY.isoformat()})
    assert st == 200 and r.get("needConfirm") and r["cap"] == 0.5          # over the $0.50 cap: asks first
    st, r = call("POST", "/data/download", {"kind": "candles", "syms": ["NQ", "ES"], "from": D0, "to": TODAY.isoformat(), "confirm": True})
    assert st == 200, r
    st, d = call("GET", "/data")
    assert d["job"]["status"] == "done", d["job"]
    c = d["coverage"]["NQ"]
    assert c["days"] > 150 and c["from"] <= (TODAY - timedelta(days=255)).isoformat() and c["bars"] > 150 * 1300, c
    assert d["coverage"]["ES"]["days"] == c["days"]
    D = futdata.daily("NQ")
    assert D[0][0] >= D0 and all(len(x) == 6 for x in D)
    rth = futdata.rth("NQ", 5, 5)
    day = sorted(rth)[-2]
    assert rth[day][0][0] == f"{day}T09:30" and rth[day][-1][0] == f"{day}T15:55" and len(rth[day]) == 78
    h = futdata.rth("NQ", 3, 60)
    assert [b[0][11:16] for b in h[day]] == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    full = futdata.sessions("NQ", 2, rth=False)
    first = full[sorted(full)[-1]][0][0]
    assert first[11:16] == "18:00" and futdata.session_of(first) == sorted(full)[-1]
    cl = futdata.cells("NQ")
    assert len(cl) > 60 and set(v.split("|")[0] for v in cl.values()) <= {"Bull", "Bear", "Sideways"}


def test_03_chart_and_latest():
    st, ch = call("GET", "/chart", q={"sym": "NQ", "tf": "5", "days": "3"})
    assert st == 200 and len(ch["days"]) == 3 and ch["bars"][0][6] == ch["days"][0] and ch["levels"][ch["days"][-1]].get("pdh")
    assert ch["spec"]["pv"] == 20
    lt = futdata.latest("NQ")
    assert lt["last"] and lt["vwap"] and lt["high"] >= lt["last"] >= lt["low"] and lt["cell"]
    assert call("GET", "/chart", q={"sym": "CL"})[0] == 400


def test_04_footprint():
    days = [d for d in futdata.trading_days("NQ")][-3:-1]
    st, r = call("POST", "/data/download", {"kind": "footprint", "syms": ["NQ"], "from": days[0], "to": days[-1], "confirm": True})
    assert st == 200, r
    st, fp = call("GET", "/footprint", q={"sym": "NQ", "day": days[-1]})
    assert st == 200 and fp["m"][0][0] == "09:30" and fp["m"][-1][0] == "15:59" and len(fp["m"]) == 390
    p, b, a = fp["m"][0][1][0]
    assert b + a > 0 and fp["m"][0][1][0][0] >= fp["m"][0][1][-1][0]        # prices high to low
    assert fp["big"] and all(x[2] >= 20 for x in fp["big"])
    assert futdata.coverage()["NQ"]["fpDays"] == 2
    assert call("GET", "/footprint", q={"sym": "ES", "day": days[-1]})[0] == 404


def test_05_strategy_protocol():
    st, s = call("POST", "/strategies", {"setup": "range50", "params": {"tickers": ["NQ", "XX"], "legPct": 0.2, "days": 200}})
    assert st == 200 and s["params"]["tickers"] == ["NQ"] and s["stage"] == "idea" and s["describe"]
    sid = s["id"]
    st, r = call("POST", f"/strategies/{sid}/action", {"action": "paper"})
    assert st == 400                                   # no validation yet
    st, r = call("POST", f"/strategies/{sid}/run", {"step": "study"})
    assert st == 200 and r["status"] == "done", r
    st, v = call("GET", f"/strategies/{sid}")
    assert v["stage"] == "study" and v["study"]["n"] > 0 and v["study"]["m"]["n"] == v["study"]["n"] - sum(1 for t in strategy.trades(SUB, sid, "study") if t["why"] == "open")
    ts = strategy.trades(SUB, sid, "study")
    t = ts[0]
    assert t["s"] == "NQ" and t["net"] == round(t["gross"] - t["cost"], 2) and t["cost"] == 2 * 2.25 + 2 * 0.25 * 20
    assert t["gross"] == round(t["pts"] * 20, 2) and t["bp"] is not None and t.get("tod")
    st, r = call("POST", f"/strategies/{sid}/run", {"step": "validate"})
    assert st == 200 and r["status"] == "done", r
    st, v = call("GET", f"/strategies/{sid}")
    va = v["valid"]
    assert len(va["gates"]) == 5 and va["train"]["n"] + va["test"]["n"] == v["study"]["m"]["n"]
    assert set(va["grid"]) == {f"{p}|{x}" for p in strategy.PHASES for x in strategy.VOLS} and v["cells"]
    assert va["cost2"]["exp"] < va["test"]["exp"]
    # force a pass to exercise paper trading
    s = strategy.get(SUB, sid)
    s["valid"]["pass"] = True
    s["cells"] = {k: True for k in s["cells"]}
    db.put(s)
    st, r = call("POST", f"/strategies/{sid}/action", {"action": "paper"})
    assert st == 200 and r["stage"] == "paper" and r["paper"]["on"]
    st, r = call("PUT", f"/strategies/{sid}", {"params": {"legPct": 0.5}})
    assert st == 400                                   # rules locked while paper trading
    s = strategy.get(SUB, sid)
    s["paper"]["since"] = futdata.trading_days("NQ")[-15]
    db.put(s)
    strategy.paper_tick(SUB)
    s = strategy.get(SUB, sid)
    pt = strategy.trades(SUB, sid, "paper")
    assert pt and all(t["d"] >= s["paper"]["since"] for t in pt) and s["paper"]["n"] == sum(1 for t in pt if t["status"] == "closed")
    st, run = call("GET", "/running")
    assert st == 200 and [m["sym"] for m in run["markets"]] == ["NQ", "ES"] and run["strategies"][0]["id"] == sid
    st, ch = call("GET", "/chart", q={"sym": "NQ", "tf": "1", "days": "5", "strat": sid})
    assert st == 200 and isinstance(ch["marks"], list)
    assert call("DELETE", f"/strategies/{sid}")[0] == 400
    call("POST", f"/strategies/{sid}/action", {"action": "stop"})
    assert call("DELETE", f"/strategies/{sid}")[0] == 200


def test_06_other_setups():
    for setup, p in (("onsweep", {"days": 60, "minRR": 0}), ("onsweep", {"days": 60, "tgt": "other", "tf": 1}), ("ibbreak", {"days": 60, "filter": "none"}), ("ibbreak", {"days": 60, "ibMin": 30, "stop": "bar", "tf": 15}), ("odpull", {"days": 60, "drivePct": 0.05}), ("vwapret", {"days": 60}), ("vwapret", {"days": 60, "hours": "on", "tf": 1, "trend": 0}), ("tvpoc", {"days": 120}), ("tvpoc", {"days": 60, "entry": "poc", "tf": "5", "maType": "HMA", "stop": "none"}), ("tvpoc", {"tf": "1D", "maType": "VWMA", "days": 250}), ("orbema", {"days": 120, "rangeMin": 30}), ("orbema", {"days": 60, "emaOn": "rth", "entryAt": "open"}), ("orbema", {"days": 120, "tf": 1, "minRR": 0}), ("orb", {"days": 120}), ("silver", {"days": 60, "tf": 5}), ("wt", {"tf": "1D", "side": "both", "days": 250}),
                     ("wt", {"tf": "15", "days": 60}), ("pdbreak", {"days": 80, "bias": "none", "minRR": 0}), ("pdbreak", {"days": 60, "bias": "trend", "brkTf": 5, "tgt": "r"}),
                     ("pdsweep", {"days": 60, "minRR": 0}), ("pdsweep", {"days": 60, "levels": "outer", "tgt": "other"}), ("nrbreak", {"days": 80, "cond": "any", "trend": "none", "minRR": 0}),
                     ("nrbreak", {"days": 60, "cond": "either", "orMin": 30, "stop": "mid"}), ("fpdisp", {"days": 60, "minRR": 0}), ("fpdisp", {"days": 60, "fair": "vwap", "stop": "swing", "tf": 1}),
                     ("ifvg", {"days": 60, "asia": "none", "bias": "h1", "minRR": 0}), ("ifvg", {"days": 60, "tgt": "liq", "stop": "swing"}),
                     ("ifvg", {"days": 60, "hours": "all", "exit": "trail", "trail": "ema15", "gapFrom": "18:00"}),
                     ("biaspull", {"days": 60}), ("biaspull", {"days": 60, "hours": "rth", "exit": "target", "bias": "h1", "asia": "dir"}),
                     ("ondrift", {"days": 60}), ("ondrift", {"days": 60, "bias": "none", "stopMult": 0, "exit": "trail"}), ("ldnbrk", {"days": 60}),
                     ("ldnbrk", {"days": 60, "exit": "trail", "tgtMult": 0, "tf": 15}), ("smt", {"days": 60}), ("smt", {"days": 60, "tradeSym": "ES", "tgt": "r", "hours": "rth"}),
                     ("flatrev", {"days": 60}), ("postclose", {"days": 60, "mode": "fade"}), ("sungap", {"days": 120}), ("sungap", {"days": 60, "which": "all", "mode": "follow"}),
                     ("ifvg", {"days": 60, "volMin": 20, "volMax": 80}), ("insidebar", {"days": 60}), ("insidebar", {"days": 30, "tf": 1, "hours": "all"}),
                     ("insidebar", {"days": 60, "tf": 15, "entry": "close", "exit": "trail"}), ("imom", {"days": 60}), ("onrev", {"days": 60, "minPct": 0.1}),
                     ("gapfill", {"days": 60}), ("tom", {"days": 120}), ("vwapband", {"days": 60}), ("rsi2", {"days": 60}), ("squeeze", {"days": 60}),
                     ("volclimax", {"days": 60, "volMult": 1.5}), ("pwsweep", {"days": 60, "fresh": False}), ("trendday", {"days": 60, "rangeMult": 0.3}),
                     ("onmid", {"days": 60}), ("onmid", {"days": 60, "stop": "opp", "deadPct": 10, "exitAt": "11:30"}),
                     ("onmid", {"days": 60, "stop": "part", "stopPart": 0.15, "minRR": 1}),
                     ("onhold", {"days": 60}), ("onhold", {"days": 60, "exitAt": "11:30", "stop": "opp"}),
                     ("onmid", {"days": 60, "hold": "off"}), ("onmid", {"days": 60, "holdStop": "level", "exitAt": "11:30"}),
                     ("fractal", {"days": 60}), ("fractal", {"days": 60, "entry": "fade", "macro": "none", "tf": 5}),
                     ("fractal", {"days": 60, "entry": "count", "macro": "none", "oddsMax": 0.3, "hours": "rth", "tgt": "r", "tgtR": 2}),
                     ("fractal", {"days": 60, "sqz": "no", "daily": "with", "macro": "against", "tf": 60, "wave": 7}),
                     ("fractal", {"days": 60, "entry": "vector", "macro": "none", "tgt": "prev", "oaMin": 0.3}),
                     ("fractal", {"days": 60, "entry": "vector", "tgt": "day", "hours": "rth"}),
                     ("fractal", {"days": 60, "mm": "both", "tgt": "wall"}),
                     ("fractal", {"days": 60, "macro": "none", "htf": "60", "htfRule": "side"}),
                     ("fractal", {"days": 60, "macro": "none", "htf": "240", "htfRule": "vector", "tf": 5}),
                     ("fractal", {"days": 60, "macro": "none", "htf": "D", "htfRule": "dir", "htfWave": 5}),
                     ("fractal", {"days": 60, "macro": "none", "entry": "smfade", "smDays": 100}),
                     ("fractal", {"days": 60, "macro": "none", "entry": "smfade", "smLevel": "median", "smTgt": "close", "smMatch": "weekday"}),
                     ("fractal", {"days": 60, "macro": "none", "entry": "late", "htf": "60", "lateN": 2}),
                     ("fractal", {"days": 60, "macro": "none", "entry": "cross", "oaExit": 0.3})):
        s = strategy.create(SUB, {"setup": setup, "params": p})
        v = strategy.study(SUB, s["id"])
        assert v["study"]["n"] >= 0, setup
        if v["study"]["n"]:
            strategy.validate(SUB, s["id"])
        ts = strategy.trades(SUB, s["id"], "study")
        assert all(t["e"] > 1000 for t in ts), setup


def test_06b_fractal_mm():
    import fxstrat
    mm = {"2026-01-05": [{"t": "09:35", "price": 500, "ca": 10, "cb": 5, "pa": 3, "pb": 8, "cw": 505, "pw": 495},
                         {"t": "10:00", "price": 501, "ca": 4, "cb": 9, "pa": 9, "pb": 2, "cw": 506, "pw": 496}]}
    assert fxstrat.mm_at(mm, "2026-01-05", "09:30") is None and fxstrat.mm_at(mm, "2026-01-05", "09:50")["t"] == "09:35"
    r1, r2 = mm["2026-01-05"]
    assert fxstrat.mm_ok(r1, 1, "both") and not fxstrat.mm_ok(r1, -1, "calls")
    assert fxstrat.mm_ok(r2, -1, "both") and fxstrat.mm_ok(r2, -1, "puts") and not fxstrat.mm_ok(None, 1, "calls")
    p = fxstrat.clean({"entry": "vector", "tgt": "wall", "mm": "calls", "oaMin": 0.4})
    assert p["entry"] == "vector" and p["tgt"] == "wall" and p["mm"] == "calls" and "gamma wall" in fxstrat.describe(p)


def test_06b2_htf_states():
    import fxstrat
    # 5-minute candles, 2 sessions, 60-minute bias: a candle only sees the 60-minute candle before its own
    S, sday = [], []
    for d in ("2026-01-05", "2026-01-06"):
        for k in range(78):
            m = 570 + 5 * k
            px = 100 + k * 0.1 if d == "2026-01-05" else 108 - k * 0.1
            S.append((f"{d}T{m // 60:02d}:{m % 60:02d}", px, px + 0.05, px - 0.05, px))
            sday.append(d)
    p = fxstrat.clean({"htf": "60", "htfWave": 2, "htfRule": "side", "hours": "rth"})
    st = fxstrat.htf_states("NQ", p, S, sday)
    assert len(st) == len(S) and st[0] is None and all(x is None for x in st[:12])     # nothing finished yet
    assert st[12 * 3] == 1 and st[-1] == -1, (st[36], st[-1])
    assert st[12] == st[23]                                    # same finished 60-minute candle for a whole hour


def test_06b3_fractal_variants():
    import search
    for key, ent in (("smfade", "smfade"), ("oaride", "cross"), ("latewave", "late")):
        s = strategy.create(SUB, {"setup": key, "params": {"days": 60, "entry": "fade"}})
        assert s["setup"] == key and s["params"]["entry"] == ent and s["params"]["setup"] == key, s["params"]
        v = strategy.study(SUB, s["id"])
        assert v["study"]["n"] > 0, (key, v["study"])
        assert search.space(key) and search.key(key, search.from_params(key, s["params"]))
    o = strategy.create(SUB, {"setup": "oaride", "params": {"days": 60, "oaExit": 0}})
    assert o["params"]["oaExit"] == 0.3 and "Option Analyzer" in strategy.describe(o["params"]) if hasattr(strategy, "describe") else True
    lw = strategy.create(SUB, {"setup": "latewave", "params": {"days": 60, "tickers": ["QQQ"]}})
    assert lw["params"]["htf"] == "D" and lw["params"]["tickers"] == ["QQQ"]


def test_06b4_onmid_hold():
    import onmid
    p = onmid.clean({"hold": "close", "holdStop": "be"})
    day = "2026-01-05"
    c = lambda hm, o, h, l, cl: (f"{day}T{hm}", o, h, l, cl)
    # long from 100, target 105, stop 98: 09:35 closes above 105 -> held; 10:00 dips to 101 (above break-even); out 15:55 at 110
    F = [c("09:25", 99, 100, 99, 100), c("09:30", 100, 103, 99.5, 102), c("09:35", 102, 106, 101, 105.5), c("10:00", 105, 106, 101, 104),
         c("15:50", 108, 110, 107, 109), c("15:55", 109, 110.5, 109, 110)]
    x = onmid.hold_trade(F, 0, 1, 100, 98, 105, 2, p, day)
    assert x["held"] and x["x"] == 110 and x["why"].startswith("time"), x
    # a touch without a close beyond takes profit at the target
    F2 = F[:2] + [c("09:35", 102, 106, 101, 104.5)] + F[3:]
    y = onmid.hold_trade(F2, 0, 1, 100, 98, 105, 2, p, day)
    assert not y["held"] and y["x"] == 105 and y["why"] == "target", y
    # held, then back to the entry: break-even
    F3 = F[:3] + [c("10:00", 105, 105, 99, 99.5)] + F[4:]
    z = onmid.hold_trade(F3, 0, 1, 100, 98, 105, 2, p, day)
    assert z["held"] and z["x"] == 100 and "break-even" in z["why"], z


def test_06b5_onhold():
    s = strategy.create(SUB, {"setup": "onhold", "params": {"days": 60, "hold": "close", "exitAt": "12:30"}})
    assert s["params"]["hold"] == "never" and s["params"]["exitAt"] == "12:30"
    strategy.study(SUB, s["id"])
    ts = strategy.trades(SUB, s["id"], "study")
    assert ts and all(t["why"] in ("stop", "time") for t in ts), {t["why"] for t in ts}       # never a target exit
    assert all(t["out"][11:16] <= "12:35" for t in ts)


def test_06b6_opt0dte():
    import time
    import alpaca
    import opt0dte
    import uwhales
    alpaca.save_key(SUB, "PK1234567890AB", "s" * 40)
    uwhales.save_key(SUB, "uw_" + "k" * 30)
    s = strategy.create(SUB, {"setup": "onhold", "params": {"days": 60}})
    strategy.study(SUB, s["id"])
    ts = strategy.trades(SUB, s["id"], "study")
    need = opt0dte.trade_days(ts)
    assert need and all(e == "QQQ" and side in "CP" for e, d, side in need)
    assert opt0dte.job(SUB, ts[-8:], lambda m: None, time.time() + 600, lambda e: None, {})
    st, r = call("GET", f"/strategies/{s['id']}/options")
    assert st == 200 and r["coverage"]["have"] >= 1, r.get("coverage")
    rows = [x for x in r["rows"] if x["n"]]
    assert rows and all(0 <= x["avgDelta"] <= 1 and x["avgPrem"] > 0 for x in rows), r["rows"][:2]
    lo, hi = [x for x in rows if x["delta"] <= 0.3], [x for x in rows if x["delta"] >= 0.7]
    assert not lo or not hi or lo[0]["avgPrem"] < hi[0]["avgPrem"]          # lower delta = cheaper (further out of the money)
    # 5 days out: its own download and pack; the contracts expire 5 weekdays later
    assert opt0dte.expiry("2026-11-06", 5) == "2026-11-13" and opt0dte.expiry("2026-11-06", 0) == "2026-11-06"
    assert opt0dte.job(SUB, ts[-4:], lambda m: None, time.time() + 600, lambda e: None, {}, 5)
    st, r5 = call("GET", f"/strategies/{s['id']}/options", q={"dte": "5"})
    assert st == 200 and r5["dte"] == 5 and r5["coverage"]["have"] >= 1
    assert any(x["n"] for x in r5["rows"]), r5["rows"][:2]
    d0 = opt0dte.pack("QQQ", 5)
    assert d0 and all(v.get("exp", "") > k[:10] for k, v in d0.items() if v.get("strikes"))
    for dte in (0, 5):                                      # some train-period days too (the delta is picked on them)
        assert opt0dte.job(SUB, ts[:6], lambda m: None, time.time() + 600, lambda e: None, {}, dte)
    st, c = call("GET", f"/strategies/{s['id']}/compare", q={"risk": "1000"})
    assert st == 200 and c["rows"][0]["name"].startswith("Futures") and c["cut"], c
    names = [x["name"] for x in c["rows"]]
    assert any("0DTE" in n for n in names) and any("5 days out" in n for n in names), names
    f0 = c["rows"][0]
    assert f0["train"]["n"] + f0["test"]["n"] == len([t for t in ts if t.get("net") is not None])
    assert all(x["test"]["n"] == 0 or x["test2x"] <= x["test"]["total"] + 1e-6 for x in c["rows"])     # 2x costs never helps
    # NDX index options: the level from put-call parity, strikes in 10s
    assert opt0dte.job(SUB, ts[:6] + ts[-4:], lambda m: None, time.time() + 600, lambda e: None, {}, 0, "index")
    pk = opt0dte.pack("NDX", 0)
    day = next(v for v in pk.values() if v.get("strikes"))
    assert day["root"] == "NDXP" and day["S"] and abs(day["open"] / 41.0 / 656.10 - 1) < 0.05, (day["root"], day["open"])
    assert all(int(k) % 10 == 0 for k in day["strikes"])
    st, ri = call("GET", f"/strategies/{s['id']}/options", q={"und": "index"})
    assert st == 200 and ri["und"] == "index" and any(x["n"] for x in ri["rows"]), ri["rows"][:2]
    st, c = call("GET", f"/strategies/{s['id']}/compare")
    assert any(x["name"].startswith("NDX") for x in c["rows"]), [x["name"] for x in c["rows"]]
    st, j = call("POST", f"/strategies/{s['id']}/options", {"dte": 4})
    assert st == 400
    # Black-Scholes sanity: an at-the-money call is about 0.5, deep in / out near 1 / 0
    T = 390 / opt0dte.YEAR_MIN
    assert 0.45 < opt0dte.delta(500, 500, T, opt0dte.bs(500, 500, T, 0.2, True), True) < 0.55
    assert opt0dte.delta(500, 490, T, opt0dte.bs(500, 490, T, 0.2, True), True) > 0.9
    assert opt0dte.delta(500, 510, T, opt0dte.bs(500, 510, T, 0.2, False), False) > 0.9


def test_06c_fractal_on_stocks():
    import alpaca
    import stockdata
    alpaca.save_key(SUB, "PK1234567890AB", "s" * 40)
    s = strategy.create(SUB, {"setup": "fractal", "params": {"days": 80, "tickers": ["QQQ", "NQ"], "hours": "all", "macro": "none"}})
    assert s["params"]["tickers"] == ["QQQ"] and s["params"]["hours"] == "rth"
    v = strategy.study(SUB, s["id"])
    ts = strategy.trades(SUB, s["id"], "study")
    assert v["study"]["n"] > 0 and all(t["s"] == "QQQ" and "09:30" <= t["in"][11:16] <= "16:00" for t in ts), v["study"]
    assert all(t["cost"] == 2.0 for t in ts)                       # 100 shares, 1 cent slippage a side, no commission
    assert stockdata.coverage("QQQ")["days"] >= 80
    import search                                                  # the optimizer on stocks (its day split used the futures)
    search.run(SUB, s["id"], budget_s=30)
    st = search.state(SUB, s["id"])
    assert st.get("split", {}).get("cut") and st["evals"] >= 1 and not st.get("error"), st.get("split")
    assert len(search.space("fractal")) > 15 and search.key("fractal", {**search.from_params("fractal", s["params"]), "fadeP": 0.2}) == \
        search.key("fractal", search.from_params("fractal", s["params"]))      # a fade setting doesn't change a cross
    old = {"entry": "cross", "tf": 15, "wave": 10, "macro": "with", "tgt": "level"}                  # saved by the old grid
    assert search.key("fractal", old) and search.mutate("fractal", old, __import__("random").Random(1))
    b = strategy.create(SUB, {"setup": "squeeze", "params": {"tickers": ["QQQ"]}})
    assert b["params"]["tickers"] == ["NQ"]                        # the futures-only setups stay on NQ / ES
    try:
        strategy.action(SUB, s["id"], "paper_force")
        raise AssertionError("paper on stocks")
    except Exception as e:
        assert "QQQ" in str(e) or "validation" in str(e).lower()


def test_07_job_continuation():
    calls = []
    real = jobs._deadline
    jobs._deadline = lambda ctx: 1                     # already out of time: every step continues in a new call
    jobs.INVOKE = lambda payload: calls.append(payload)
    try:
        jobs.handler({"sub": SUB, "name": "data", "job": "candles", "params": {"syms": ["NQ", "ES"], "from": D0, "to": TODAY.isoformat()}})
        assert calls and calls[0]["params"]["syms"] == ["NQ", "ES"] and calls[0]["params"]["from"] == D0
    finally:
        jobs._deadline = real
        jobs.INVOKE = lambda payload: jobs.handler(payload, None)


def test_08_scheduled_refresh():
    db.put({"PK": db.upk(SUB), "SK": "PROFILE", "email": "a@b.c"})
    out = jobs.scheduled()
    assert out[SUB] == "ok", out


def test_09_orb_ema_rules():
    import orbema
    p = orbema.clean({"tf": 5, "rangeMin": 15, "ema": 3, "bufTicks": 1, "minRR": 0, "side": "short", "entryAt": "open"})
    d = "2026-10-05"
    t = lambda k: f"{d}T{9 + (30 + 5 * k) // 60:02d}:{(30 + 5 * k) % 60:02d}"
    #        range (3 candles)     breakout   falls       retest above EMA   back under      entry  ... target hit
    px = [(100, 101, 99, 100), (100, 100.5, 99.5, 100), (100, 100.5, 99, 99.5), (99.5, 99.5, 97, 97.5), (97.5, 97.5, 95, 95.5),
          (95.5, 99, 95.5, 98.75), (98.75, 99.25, 96, 96.5), (96.5, 97, 96, 96.5), (96.5, 96.5, 94.5, 94.75)] + [(94.75, 95, 94.5, 94.75)] * 30
    B = [(t(k), *x) for k, x in enumerate(px)]
    E = orbema._ema([b[4] for b in B], 3)
    x = orbema.setups(B, E, p)[0]
    assert x["side"] == "short" and x["in"] == t(7) and x["e"] == 96.5 and x["stop"] == 99.5 and x["tgt"] == 95.0, x
    assert x["why"] == "target" and x["x"] == 95.0 and x["bk"] == t(3) and x["rt"] == t(5)
    assert not orbema.setups(B, E, {**p, "minRR": 1})        # target 1.5 points vs 3 points of risk: skipped
    y = orbema.setups(B, E, {**p, "entryAt": "close"})[0]   # in at the confirmation candle's close (its last minute)
    assert y["in"] == f"{d}T10:04" and y["e"] == 96.5 and y["cf"] == t(6) and y["why"] == "target" and y["rlo"] == 99 and y["rhi"] == 101
    ovr = orbema.overlay("u1", orbema.clean({"tickers": ["NQ"], "days": 60, "emaOn": "rth"}), "NQ", futdata.daily("NQ")[-3][0], futdata.daily("NQ")[-1][0])
    assert all("09:30" <= x[0][11:16] < "16:00" for x in ovr["lines"][0]["pts"])
    ov = orbema.overlay("u1", orbema.clean({"tickers": ["NQ"], "days": 60}), "NQ", futdata.daily("NQ")[-3][0], futdata.daily("NQ")[-1][0])
    assert len(ov["ranges"]) == 3 and ov["ranges"][0]["t1"][11:16] == "09:44" and any(x[0][11:16] < "09:30" for x in ov["lines"][0]["pts"])


def test_10_order_book():
    import dbn
    import io as _io
    rec = dbn.pack_mbp10(1_700_000_000_000_000_000, 100.25, 7, "T", "B", [(100.0, 5), (99.75, 9)], [(100.25, 4)])
    assert len(rec) == 368
    rows = list(dbn.records(_io.BytesIO(dbn.header() + rec * 3), chunk=500))      # records split across reads
    assert len(rows) == 3 and rows[0][0] == dbn.MBP10 and dbn.trade(rows[1][1]) == (100.25, 7, "B")
    assert dbn.levels(rows[2][1]) == ([(100.0, 5), (99.75, 9)], [(100.25, 4)])
    day = futdata.trading_days("NQ")[-2]
    st, r = call("POST", "/data/estimate", {"kind": "book", "syms": ["NQ"], "from": day, "to": day, "window": "am"})
    assert st == 200 and r["usd"] > 0, r
    st, r = call("POST", "/data/download", {"kind": "book", "syms": ["NQ"], "from": day, "to": day, "window": "am", "confirm": True})
    assert st == 200, r
    st, hm = call("GET", "/heatmap", q={"sym": "NQ", "day": day})
    assert st == 200 and hm["step"] == 2 and hm["window"] == ["09:30", "11:30"] and len(hm["c"]) >= 3000, len(hm["c"])
    tr = [x for x in hm["c"] if x[3] + x[4]]
    assert tr and all(x[5] is not None and x[6] is not None for x in tr)
    c0 = hm["c"][0]
    assert c0[0] == 9 * 3600 + 30 * 60 and len(c0[1]) == 20 and len(c0[2]) == 20
    assert (hm["base"] + c0[2][0]) * 0.25 > (hm["base"] + c0[1][0]) * 0.25            # best ask above best bid
    st, fp = call("GET", "/footprint", q={"sym": "NQ", "day": day})
    assert st == 200 and fp["m"][0][0] == "09:30" and fp["m"][-1][0] == "11:29"
    assert futdata.coverage()["NQ"]["hmDays"] == 1


def test_11_tvpoc_indicator():
    import tvpoc
    p = tvpoc.clean({"maLen": 5, "atrLen": 5, "atrMult": 1})
    up = [(f"2026-10-05T09:{k:02d}", 100, 100.5, 99.5, 100 + (0.1 if k % 2 else -0.1), 100) for k in range(15)]
    up += [(f"2026-10-05T09:{15 + k:02d}", 100 + 2 * k, 102 + 2 * k, 99.5 + 2 * k, 101.5 + 2 * k, 100) for k in range(15)]
    dn = [(f"2026-10-05T{10 + k // 60:02d}:{k % 60:02d}", 128 - 3 * k, 129 - 3 * k, 125 - 3 * k, 125.5 - 3 * k, 100) for k in range(30)]
    C = up + dn
    ind = tvpoc.indicator(C, p)
    first_bull = ind["flip"].index(True)
    assert ind["dir"][first_bull] == 1 and ind["dir"][-1] == -1 and sum(ind["flip"]) == 2
    k = len(C) - 1
    assert ind["poc"][k] is not None and min(c[3] for c in C[ind["start"][k]:]) <= ind["poc"][k] <= max(c[2] for c in C[ind["start"][k]:])
    tr = tvpoc.run(C, {**p, "entry": "flip", "stop": "none", "flatEod": False, "tf": "15"})
    assert tr[0]["side"] == "long" and tr[0]["why"] == "trend change" and tr[1]["side"] == "short"
    assert tvpoc._sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5] and tvpoc._wma([1, 2, 3], 3)[2] == (1 + 4 + 9) / 6


def test_12_daily_supply_demand():
    mk = lambda t, o, h, l, c: [t, o, h, l, c, 100]
    d1 = [mk(f"2026-10-05T{9 + k // 60:02d}:{k % 60:02d}", 100, 101, 99, 100) for k in range(30)]
    on = [mk(f"2026-10-05T{18 + k // 60:02d}:{k % 60:02d}", 100, 101, 99, 100) for k in range(20)]
    on += [mk("2026-10-05T18:20", 100, 100.5, 99.5, 100.2), mk("2026-10-05T18:21", 100.2, 106, 100.1, 105.8)]      # base, then an impulse up
    on += [mk(f"2026-10-06T{9 + k // 60:02d}:{30 + k % 30:02d}", 105, 106, 104, 105) for k in range(5)]
    on += [mk("2026-10-06T09:35", 105, 105, 98, 98.5)]                                                                  # closes under the zone
    z = futdata.sd_zones({"2026-10-05": d1, "2026-10-06": on}, 1)
    dz = [x for x in z if x["day"] == "2026-10-06"]
    assert len(dz) == 1 and dz[0]["k"] == "demand" and dz[0]["on"] and dz[0]["lo"] == 99.5 and dz[0]["hi"] == 100.5
    assert dz[0]["t0"] == "2026-10-05T18:20" and dz[0]["broken"] and dz[0]["t1"] == "2026-10-06T09:35"
    assert not [x for x in z if x["day"] == "2026-10-05"]                    # nothing carried from day to day
    st, ch = call("GET", "/chart", q={"sym": "NQ", "tf": "5", "days": "3", "rth": "1"})
    assert st == 200 and isinstance(ch["zones"], list) and all(x["day"] in ch["days"] for x in ch["zones"])


def test_13_vwap_retest():
    import vwapret
    p = vwapret.clean({"tf": 5, "trend": 2, "tgtR": 2, "bufTicks": 0, "maxDay": 3, "side": "short"})
    d = "2026-10-06"
    B = [(f"{d}T10:{5 * k:02d}", *x) for k, x in enumerate([
        (95, 96, 94, 95, 100), (95, 96, 93, 94, 100),           # two closes under the VWAP (100)
        (94, 100.5, 93.5, 99, 100),                              # green, opened under, high touches the VWAP
        (99, 99.5, 96, 96.5, 100),                               # red, closes under: sell 96.5, stop 100.5 (risk 4), target 88.5
        (96.5, 97, 92, 92.5, 99), (92.5, 93, 88, 88.25, 98)])] # the target is hit
    t = vwapret.session(B, d, p)[0]
    assert t["side"] == "short" and t["e"] == 96.5 and t["stop"] == 100.5 and t["tgt"] == 88.5 and t["why"] == "target" and t["x"] == 88.5, t
    assert t["in"] == f"{d}T10:19" and t["rt"] == f"{d}T10:10" and t["cf"] == f"{d}T10:15" and t["r"] == 2
    assert not vwapret.session(B, d, {**p, "trend": 3})               # needs 3 closes under first
    assert not vwapret.session(B, d, {**p, "hours": "on"})            # 10:19 is not overnight
    thru = list(B)
    thru[2] = (thru[2][0], 94, 101, 93.5, 100.75, 100)                 # closes through the VWAP: not a test
    assert not vwapret.session(thru, d, p)
    late = B[:3] + [(f"{d}T10:15", 99, 99.75, 97, 99.5, 100)] + [(f"{d}T10:20", 99.5, 100.25, 96, 96.5, 100)] + B[4:]
    u = vwapret.session(late, d, p)[0]                                 # a green inside candle, then the first red one
    assert u["cf"] == f"{d}T10:20" and u["stop"] == 100.5 and u["e"] == 96.5
    assert not vwapret.session(late, d, {**p, "wait": 1})


def test_14_old_rules_still_load():
    s = strategy.create(SUB, {"setup": "vwapret", "params": {"days": 30}})
    it = db.get(db.upk(SUB), strategy.SK + s["id"])
    it["params"].pop("wait")                            # saved before the rule existed
    db.put(it)
    st, r = call("GET", "/strategies")
    assert st == 200 and any(x["id"] == s["id"] and x["params"]["wait"] == 3 for x in r["strategies"])
    assert call("GET", f"/strategies/{s['id']}")[0] == 200


def _rth(day, rows, start="09:30", tf=5):
    out, m = [], int(start[:2]) * 60 + int(start[3:])
    for k, (o, h, l, c) in enumerate(rows):
        mm = m + tf * k
        out.append((f"{day}T{mm // 60:02d}:{mm % 60:02d}", o, h, l, c))
    return out


def test_15_session_strategies():
    import ibbreak
    import odpull
    import onsweep
    d = "2026-10-06"
    on = [["2026-10-05T18:00", 100, 110, 95, 104, 10], ["2026-10-06T08:00", 104, 108, 96, 105, 10]]        # ONH 110, ONL 95
    # overnight sweep: pokes over 110, closes back under on the next candle, VWAP 102 below
    R = _rth(d, [(105, 108, 104, 107), (107, 112, 106, 111), (111, 112.5, 108, 109), (109, 109.5, 101, 101.5), (101.5, 102, 100, 101)] + [(101, 101.5, 100.5, 101)] * 5)
    V = [102.0] * len(R)
    t = onsweep.engine(d, on, R, R, V, onsweep.clean({"tf": 5, "side": "short", "minRR": 0, "bufTicks": 0}))[0]
    assert t["side"] == "short" and t["e"] == 109 and t["stop"] == 112.5 and t["tgt"] == 102 and t["why"] == "target" and t["cf"] == R[2][0], t
    assert not onsweep.engine(d, on, R, R, V, onsweep.clean({"tf": 5, "side": "short", "minRR": 3, "bufTicks": 0}))   # 7 pts vs 3.5 risk
    # initial balance (30 min = 6 candles 98..104), a close at 105 breaks up, target 104 + 6 = 110
    rows = [(100, 101, 98, 100)] * 3 + [(100, 104, 99, 103)] * 3 + [(103, 105.5, 102, 105), (105, 108, 104.5, 107.5), (107.5, 111, 107, 110.5)] + [(110, 110, 109, 109.5)] * 3
    R2 = _rth(d, rows)
    t = ibbreak.engine(d, on, R2, R2, [0] * len(R2), ibbreak.clean({"tf": 5, "ibMin": 30, "filter": "overnight", "stop": "mid", "bufTicks": 0}))[0]
    assert t["side"] == "long" and t["e"] == 105 and t["stop"] == 101 and t["tgt"] == 110 and t["why"] == "target" and t["x"] == 110, t
    down = [["x", 110, 110, 100, 100, 1]]
    assert not ibbreak.engine(d, down, R2, R2, [0] * len(R2), ibbreak.clean({"tf": 5, "ibMin": 30, "filter": "overnight"}))   # overnight down 9%
    assert ibbreak.engine(d, [["x", 100.2, 101, 99, 100, 1]], R2, R2, [0] * len(R2), ibbreak.clean({"tf": 5, "ibMin": 30, "filter": "overnight"}))  # -0.2%: flat
    assert ibbreak.engine(d, down, R2, R2, [100] * len(R2), ibbreak.clean({"tf": 5, "ibMin": 30, "filter": "vwap"}))            # over the VWAP
    assert not ibbreak.engine(d, down, R2, R2, [106] * len(R2), ibbreak.clean({"tf": 5, "ibMin": 30, "filter": "vwap"}))        # under it
    assert ibbreak.clean({"trendFilter": True})["filter"] == "overnight"                                                          # saved before
    # opening drive up 100 -> 104 (4%), two lower closes back to the EMA, then a break of the previous high
    rows = [(100, 102, 99.8, 101.8), (101.8, 103.5, 101.5, 103.4), (103.4, 104.2, 103, 104)]                              # the drive (15 min)
    rows += [(104, 104.1, 102.6, 102.8), (102.8, 103, 101.2, 101.5), (101.5, 102.4, 101.3, 102.2), (102.2, 103.5, 102, 103.3)]
    rows += [(103.3, 107, 103.2, 106.8)] + [(106.8, 107, 106.5, 106.9)] * 5
    R3 = _rth(d, rows)
    t = odpull.engine(d, on, R3, R3, [0] * len(R3), odpull.clean({"tf": 5, "drivePct": 1, "pbMin": 2, "ema": 3, "bufTicks": 0, "tgtR": 2}))[0]
    assert t["side"] == "long" and t["e"] == 102.65 and t["stop"] == 101.2 and t["cf"] == R3[6][0] and t["rt"] == R3[5][0], t
    assert abs(t["tgt"] - (102.65 + 2 * 1.45)) < 1e-9 and t["why"] == "target"


def test_16_optimize():
    import time as _t
    s = strategy.create(SUB, {"setup": "onsweep", "params": {"days": 40, "minRR": 0}})
    assert strategy.optimize(SUB, s["id"], deadline=_t.time() - 1) is False          # out of time at once: nothing done, resumable
    assert strategy.opt_view(SUB, s["id"])["rows"] == []
    st, r = call("POST", f"/strategies/{s['id']}/run", {"step": "optimize"})
    assert st == 200 and r["status"] == "done", r
    st, o = call("GET", f"/strategies/{s['id']}/optimize")
    o = o["opt"]
    assert o["complete"] and o["total"] == 36 and len(o["rows"]) == 36 and o["split"]
    assert set(o["rows"][0]["p"]) == {"bufTicks", "tgt", "lastEntry", "minDepth"} and "edge" in o["rows"][0]["test"]
    top = o["rows"][0]
    st, v = call("PUT", f"/strategies/{s['id']}", {"params": {**strategy.get(SUB, s["id"])["params"], **top["p"]}})
    same = lambda a, b: float(a) == float(b) if isinstance(b, (int, float)) else a == b
    assert st == 200 and all(same(v["params"][k], x) for k, x in top["p"].items())


def test_17_daily_setups():
    import nrbreak
    import pdbreak
    import pdsweep
    import sessionkit as K
    d = "2026-03-10"
    H = [{"d": f"x{i}", "o": 100, "h": 102, "l": 98, "c": 101} for i in range(19)] + [{"d": "y", "o": 100, "h": 101.2, "l": 99.5, "c": 101.15}]
    assert nrbreak.kind(H) == (True, True) and abs(K.atr(H) - (4 * 13 + 1.7) / 14) < 1e-9 and K.sma_close(H) > 100
    rows = [(101, 101.2, 100.6, 101.1)] * 3 + [(101.1, 101.6, 101.0, 101.5)] + [(101.5, 101.6, 101.1, 101.4)] + [(101.4, 102, 101.3, 101.9)] * 2 + [(101.9, 103, 101.8, 102.9)] * 70
    R = _rth(d, rows)
    t = nrbreak.engine(d, [], R, R, [0] * len(R), H, nrbreak.clean({"tf": 5, "orMin": 15, "bufTicks": 0, "minRR": 0}))[0]
    assert t["side"] == "long" and t["e"] == 101.5 and t["stop"] == 100.6 and t["kind"] == [True, True], t
    assert not nrbreak.engine(d, [], R, R, [0] * len(R), H[:-1] + [{"d": "y", "o": 100, "h": 103, "l": 97, "c": 99}], nrbreak.clean({"tf": 5, "cond": "nr7"}))
    # prior-day break: a 5-minute close over yesterday's high (101.2), a retest within 4 ticks that closes back over: long at that close
    t = pdbreak.engine(d, [], R, R, [0] * len(R), H, pdbreak.clean({"tf": 5, "brkTf": 5, "bias": "close", "bufTicks": 0, "minRR": 0, "tgt": "r", "tgtR": 2}))[0]
    assert t["side"] == "long" and t["e"] == 101.4 and t["stop"] == 101.1 and t["bk"] == R[3][0] and t["rt"] == R[4][0], t
    # prior-day sweep: price runs over yesterday's high and closes back under it: short
    rows2 = [(100.5, 100.8, 100.2, 100.6), (100.6, 101.6, 100.5, 101.3), (101.3, 101.4, 100.4, 100.5)] + [(100.5, 100.6, 99.4, 99.6)] * 70
    R2 = _rth(d, rows2)
    t = pdsweep.engine(d, [], R2, R2, [99.8] * len(R2), H, pdsweep.clean({"tf": 5, "bufTicks": 0, "minRR": 0}))[0]
    assert t["side"] == "short" and t["e"] == 100.5 and t["stop"] == 101.6 and t["tgt"] == 99.8, t
    assert not pdsweep.engine(d, [["x", 100, 101.5, 100, 100.5, 1]], R2, R2, [99.8] * len(R2), H, pdsweep.clean({"tf": 5, "fresh": True, "minRR": 0, "side": "short"}))


def test_18_fair_price_displacement():
    import fpdisp
    d = "2026-03-10"
    # opens 100 (fair), runs to 103, a small green candle, then a red one with a bigger body closing under its low: short to 100
    rows = [(100, 101, 99.8, 100.9), (100.9, 102, 100.8, 101.9), (101.9, 103, 101.8, 102.8), (102.8, 103.2, 102.6, 103.0), (103.0, 103.1, 101.9, 102.0)]
    rows += [(102.0, 102.1, 101, 101.2), (101.2, 101.3, 99.9, 100.1)] + [(100.1, 100.3, 99.8, 100)] * 60
    R = _rth(d, rows)
    t = fpdisp.engine(d, [], R, R, [0] * len(R), fpdisp.clean({"tf": 5, "bufTicks": 0, "minRR": 0, "maxDay": 1}))[0]
    assert t["side"] == "short" and t["e"] == 102.0 and t["stop"] == 103.1 and t["tgt"] == 100 and t["why"] == "target" and t["cf"] == R[4][0], t
    assert not fpdisp.engine(d, [], R, R, [0] * len(R), fpdisp.clean({"tf": 5, "bodyMult": 6, "minRR": 0}))          # body only 5x the previous 0.2
    assert not fpdisp.engine(d, [], R, R, [0] * len(R), fpdisp.clean({"tf": 5, "minRR": 3}))                         # 2 pts vs 1.6 risk


def test_19_fvg_inversion():
    import ifvg
    d = "2026-03-10"
    # bias long: 3 EMAs rising under price. A pullback leaves a bearish gap (102.0 -> 101.0), then a close above 102.0: long
    up = [(f"2026-03-09T{18 + i // 12:02d}:{i % 12 * 5:02d}", 0, 0, 0, 90 + i * 0.05) for i in range(60)]
    E = ifvg.ema_track([(t, c, c, c, c) for t, o, h, l, c in up])
    assert all(r for e, r, dn in E[up[-1][0]])
    row = E[up[-1][0]]
    rows = [(100, 101, 99.8, 100.9), (100.9, 103, 100.8, 102.8), (102.8, 103.5, 102.0, 102.2), (102.2, 102.3, 101.4, 101.5), (101.5, 101.6, 100.6, 100.8),
            (100.8, 102.5, 100.7, 102.4)] + [(102.4, 105, 102.3, 104.8)] * 60
    R = _rth(d, rows)
    Eday = {b[0]: row for b in R}
    F = [(f"2026-03-10T08:{i * 5:02d}", 103, 104, 102, 103) for i in range(3)] + [(f"2026-03-10T08:15", 103, 104.5, 102, 103)] + [(f"2026-03-10T08:{20 + i * 5:02d}", 103, 104, 102, 103) for i in range(4)] + R
    fi = {b[0]: i for i, b in enumerate(F)}
    on = [("2026-03-09T18:00", 95, 96, 94, 95, 1), ("2026-03-10T01:55", 96, 97, 95.5, 97, 1)]
    t = ifvg.day_trades(d, on, R, R, F, fi, Eday, ifvg.clean({"tf": 5, "exit": "target", "hours": "rth", "minGap": 1, "minRR": 0, "maxDay": 1}))[0]
    assert t["side"] == "long" and t["e"] == 102.4 and t["fvgTop"] == 102.0 and t["fvgBot"] == 101.6 and t["stop"] == 101.6, t
    assert abs(t["tgt"] - 104) < 1e-6, t                                 # the nearest untaken swing high (104) = 2R here
    t = ifvg.day_trades(d, on, R, R, F, fi, Eday, ifvg.clean({"tf": 5, "exit": "target", "hours": "rth", "minGap": 1, "minRR": 0, "maxDay": 1, "stop": "swing"}))[0]
    assert t["stop"] == 100.6 and abs(t["tgt"] - (102.4 + 2 * 1.8)) < 1e-6 and t["tk"] == "2R", t   # liquidity 104 is under 2R (106.0): 2R
    notes = []
    assert not ifvg.day_trades(d, [("2026-03-09T18:00", 97, 98, 94, 95, 1)], R, R, F, fi, Eday, ifvg.clean({"tf": 5, "exit": "target", "hours": "rth", "minGap": 1, "minRR": 0, "asia": "dir"}), notes)   # Asia closed down
    L = [x for x in notes if x["d"] > 0]
    assert L[0]["text"] == "Long skipped: Asia closed against" and L[0]["t"] == R[5][0] and notes[0]["text"] == "Short skipped: bias against", notes
    notes = []
    ifvg.day_trades(d, on, R, R, F, fi, Eday, ifvg.clean({"tf": 5, "exit": "target", "hours": "rth", "minGap": 1, "minRR": 3}), notes)
    assert [x for x in notes if x["d"] > 0][0]["text"].startswith("Long skipped: target 2.0R"), notes


def test_19b_ifvg_trigger_candle():
    import ifvg
    d = "2026-03-10"
    rows = [(100, 101, 99.8, 100.9), (100.9, 103, 100.8, 102.8), (102.8, 103.5, 102.0, 102.2), (102.2, 102.3, 101.4, 101.5), (101.5, 101.6, 100.6, 100.8),
            (100.8, 102.5, 100.7, 102.4)] + [(102.4, 105, 102.3, 104.8)] * 60
    R = _rth(d, rows)
    up = [(f"2026-03-09T{18 + i // 12:02d}:{i % 12 * 5:02d}", 0, 0, 0, 90 + i * 0.05) for i in range(60)]
    row = ifvg.ema_track([(t, c, c, c, c) for t, o, h, l, c in up])[up[-1][0]]
    E = {b[0]: row for b in R}
    fi = {b[0]: i for i, b in enumerate(R)}
    base = {"tf": 5, "exit": "target", "hours": "rth", "minGap": 1, "minRR": 0, "maxDay": 1}
    run = lambda **kw: ifvg.day_trades(d, [], R, R, R, fi, E, ifvg.clean({**base, **kw}))
    t = run(trigMin=1)[0]                                          # body 1.6 vs the average range 1.36: 1.18x
    assert t["e"] == 102.4 and abs(t["tb"] - 1.6 / 1.36) < 1e-6 and abs(t["bp"] - 1.6 / 1.8 * 100) < 1e-6, t
    assert not run(trigMin=2) and not run(trigSize="range", trigMax=1.2) and not run(bodyPct=90) and run(bodyPct=80)
    # vs the previous candles: bodies 0.6, 1.2 (red, 0.7 / 0.3 / 0.7 before it) -> trigger body 1.6
    assert run(trigN=3, trigPrevMult=1.0) and not run(trigN=3, trigPrevMult=2.5)
    assert not run(trigN=3, trigPrevMax=2.0) and run(trigN=3, trigPrevMax=2.5)    # 1.6 vs 0.7: 2.29x
    assert run(trigN=3, trigEngulf=True) == []                     # 102.4 doesn't close past the 103.5 high 3 candles back
    assert run(trigN=2, trigEngulf=True)                           # past 102.3 and 101.6
    x = dict(t, d=d)
    ifvg.decorate(x, ifvg.clean({}))
    assert x["tb"] == "1-1.5×" and x["bp"] == "80%+ (small wicks)"


def test_20_ifvg_asia_migration():
    import db as _db
    s = strategy.create(SUB, {"setup": "ifvg", "params": {"days": 40, "asia": "dir"}})
    it = _db.get(strategy._pk(SUB), strategy.SK + s["id"])
    it.pop("migAsia", None)
    it.pop("migIfvg", None)
    it["params"].update(hours="rth", exit="target")
    _db.put(it)
    g = strategy.get(SUB, s["id"])["params"]
    assert g["asia"] == "none" and g["hours"] == "all" and g["exit"] == "trail"       # switched once to what was asked
    call("PUT", f"/strategies/{s['id']}", {"params": {**strategy.get(SUB, s["id"])["params"], "asia": "dir"}})
    assert strategy.get(SUB, s["id"])["params"]["asia"] == "dir"                       # a later choice sticks


def test_21_ifvg_managed_exit():
    import ifvg
    p = ifvg.clean({"exit": "trail", "beR": 0.5, "partR": 1, "partPct": 50, "trail": "none"})
    B = [("2026-03-10T10:00", 100, 100.6, 99.8, 100.4), ("2026-03-10T10:05", 100.4, 101.2, 100.3, 101), ("2026-03-10T10:10", 101, 101.1, 99.9, 100)]
    x, t, why, j, part = ifvg.manage(B, 0, 1, 100, 99, 102, 1.0, p, {}, "2026-03-10T15:55")
    assert abs(x - 100.5) < 1e-9 and part == ["2026-03-10T10:05", 101] and why == "break-even (after partial)" and j == 2, (x, why, part)
    # swing trail: the stop follows the last 2 candles' low once at break-even; out on the pullback
    p = ifvg.clean({"exit": "trail", "beR": 0.5, "partPct": 0, "trail": "swing", "trailN": 2})
    B = [("2026-03-10T10:00", 100, 101, 99.9, 100.8), ("2026-03-10T10:05", 100.8, 103, 100.7, 102.8), ("2026-03-10T10:10", 102.8, 104, 102.5, 103.8),
         ("2026-03-10T10:15", 103.8, 103.9, 102.0, 102.2), ("2026-03-10T10:20", 102.2, 102.4, 101.5, 101.6)]
    x, t, why, j, part = ifvg.manage(B, 0, 1, 100, 99, 102, 1.0, p, {}, "2026-03-10T15:55")
    assert x == 102 and why == "trail" and j == 4 and part is None, (x, why)   # the stop trailed up to 102 (low of 10:10-10:15)
    # R trail: 0.5R behind the best high once at break-even
    p = ifvg.clean({"exit": "trail", "beR": 0.5, "partPct": 0, "trail": "r", "trailR": 0.5})
    x, t, why, j, part = ifvg.manage(B, 0, 1, 100, 99, 102, 1.0, p, {}, "2026-03-10T15:55")
    assert x == 102.5 and why == "trail" and j == 2, (x, why, j)           # best 103 (10:05) -> stop 102.5, touched at 10:10
    # fixed target unchanged
    x, t, why, j, part = ifvg.manage(B, 0, 1, 100, 99, 102, 1.0, ifvg.clean({"exit": "target"}), {}, "2026-03-10T15:55")
    assert x == 102 and why == "target"


def test_22_search():
    import search
    search.TEST_BUDGET = 40
    calls = []
    jobs.INVOKE = lambda payload: calls.append(payload)
    s = strategy.create(SUB, {"setup": "ifvg", "params": {"days": 80, "tickers": ["NQ"]}})
    st, r = call("POST", f"/strategies/{s['id']}/search", {"action": "start"})
    assert st == 200 and r["on"] and calls[-1]["job"] == "search"
    out = search.job(SUB, s["id"], calls[-1], lambda ev: calls.append(ev))   # a short run: budget from RUN_S, cut by the stop below
    st, v = call("GET", f"/strategies/{s['id']}/search")
    assert v["tried"] >= 1 and v["log"] and v["split"]["cut"] and out["continued"] and calls[-1]["job"] == "search"
    x = v["top"][0]
    assert x["g"] == search.from_params("ifvg", strategy.get(SUB, s["id"])["params"]) or x["fit"] >= min(y["fit"] for y in v["top"])
    assert search.key("ifvg", {**x["g"], "exit": "trail", "tgtR": 1.0}) == search.key("ifvg", {**x["g"], "exit": "trail", "tgtR": 4.0})   # tgtR unused
    call("POST", f"/strategies/{s['id']}/search", {"action": "stop"})
    assert search.run(SUB, s["id"], budget_s=60) is False                       # stopped: no next run
    st, v = call("POST", f"/strategies/{s['id']}/search", {"action": "restart"})
    assert v["tried"] == 0 and not v["top"]
    assert search.score([]) == {"n": 0} and search.fitness({"n": 10, "t": 3}) < 0       # too few trades
    s2 = strategy.create(SUB, {"setup": "onsweep", "params": {"days": 40, "tickers": ["NQ"]}})
    call("POST", f"/strategies/{s2['id']}/search", {"action": "start"})
    assert search.run(SUB, s2["id"], budget_s=120) is False and search.public(SUB, s2["id"])["done"]   # a 36-setting grid: all tried


def test_23_bias_pullback_control():
    import biaspull
    import ifvg
    d = "2026-03-10"
    rows = [(100, 101, 99.5, 100.8), (100.8, 101.5, 100.5, 101.4), (101.4, 101.5, 100.6, 100.7), (100.7, 100.8, 100.0, 100.2), (100.2, 101.2, 100.1, 101.1)]
    rows += [(101.1, 103, 101, 102.9)] * 40
    F = _rth(d, rows)
    up = [(f"2026-03-09T{18 + i // 12:02d}:{i % 12 * 5:02d}", 0, 0, 0, 90 + i * 0.05) for i in range(60)]
    row = ifvg.ema_track([(t, c, c, c, c) for t, o, h, l, c in up])[up[-1][0]]
    E = {b[0]: row for b in F}
    p = biaspull.clean({"tf": 5, "hours": "rth", "minRisk": 0, "exit": "target", "tgtR": 2, "maxDay": 1})
    p["_dummy"] = 1
    t = biaspull.day_trades(d, [], F, E, {**p, "gapFrom": "08:00", "minGap": 0, "fvgMax": 24, "stop": "swing", "tgt": "r", "minRR": 0, "pivLen": 3, "liqLook": 150})[0]
    assert t["side"] == "long" and t["e"] == 101.1 and t["stop"] == 100.0 and t["pb"] == 2 and t["cf"] == F[4][0], t
    assert biaspull.clean({"exit": "nope"})["exit"] == "trail" and biaspull.clean({})["setup"] == "biaspull"


def test_24_edge_test_choice():
    s = strategy.create(SUB, {"setup": "biaspull", "params": {"days": 120, "tickers": ["NQ"], "minRisk": 0}})
    v = strategy.study(SUB, s["id"])
    assert v["study"]["n"] > 0
    v = strategy.validate(SUB, s["id"])["valid"]
    assert v["edgeTest"] == "timing" and v["gates"][0]["k"] == "edge" and v["info"][0]["k"] == "edge2" and "edge2" in v["test"]
    call("PUT", f"/strategies/{s['id']}", {"edgeTest": "rule"})
    v = strategy.validate(SUB, s["id"])["valid"]
    assert v["edgeTest"] == "rule" and v["gates"][0]["k"] == "edge2" and v["info"][0]["k"] == "edge"
    o = strategy.create(SUB, {"setup": "onsweep", "params": {"days": 60}})          # no random-side baseline: the timing gate, no info
    strategy.study(SUB, o["id"])
    call("PUT", f"/strategies/{o['id']}", {"edgeTest": "rule"})
    if strategy.get(SUB, o["id"])["study"]["n"]:
        v = strategy.validate(SUB, o["id"])["valid"]
        assert v["gates"][0]["k"] == "edge" and v["info"] == []


def test_25_paper_anyway_and_hours():
    import datetime as dt
    s = strategy.create(SUB, {"setup": "biaspull", "params": {"days": 120, "tickers": ["NQ"], "minRisk": 0}})
    strategy.study(SUB, s["id"])
    v = strategy.validate(SUB, s["id"])
    call("PUT", f"/strategies/{s['id']}", {"cells": {k: True for k in v["valid"]["grid"]}})
    st, r = call("POST", f"/strategies/{s['id']}/action", {"action": "paper_force"})
    assert st == 200 and r["paper"]["on"] and r["paper"]["forced"], r
    call("POST", f"/strategies/{s['id']}/action", {"action": "stop"})
    if not v["valid"]["pass"]:
        assert call("POST", f"/strategies/{s['id']}/action", {"action": "paper"})[0] == 400
    ny = dt.timezone(dt.timedelta(hours=-5))
    mo = lambda y, m, d, h, mi: jobs.market_open(dt.datetime(y, m, d, h, mi, tzinfo=ny))
    assert mo(2026, 11, 2, 20, 0) and mo(2026, 11, 3, 3, 0) and not mo(2026, 11, 2, 17, 30) and not mo(2026, 11, 7, 12, 0)
    assert not mo(2026, 11, 8, 17, 0) and mo(2026, 11, 8, 18, 5) and not mo(2026, 11, 6, 17, 5)


def test_26_session_in_progress_and_refresh_status():
    import futdata
    days = futdata.trading_days("NQ")
    last = futdata.last_bar_time("NQ")
    assert days[-1] == futdata.session_of(last) or days[-1] >= futdata.session_of(last)
    jobs.refresh(SUB)
    st = jobs.status(SUB, "refresh")
    assert st["status"] in ("done", "skipped") and "NQ" in st["last"]
    st, r = call("GET", "/running")
    assert st == 200 and "refresh" in r and "lastBar" in r
    s = strategy.create(SUB, {"setup": "ifvg", "params": {"days": 30}})
    st, v = call("PUT", f"/strategies/{s['id']}", {"name": "FVG copy A"})
    assert st == 200 and v["name"] == "FVG copy A"


def test_27_overnight_engines():
    import ldnbrk
    import nightkit as N
    import sungap
    ev, d = "2026-03-09", "2026-03-10"
    mk = lambda day, hm: f"{day}T{hm}"
    F = [(mk(ev, f"{18 + i // 12:02d}:{i % 12 * 5:02d}"), 100, 101, 99, 100) for i in range(72)]          # 18:00-23:55: range 99-101
    F += [(mk(d, f"{i // 12:02d}:{i % 12 * 5:02d}"), 100, 100.8, 99.5, 100.2) for i in range(24)]          # 00:00-01:55 inside
    F += [(mk(d, "02:00"), 100.2, 101.6, 100.1, 101.5)] + [(mk(d, f"02:{5 + i * 5:02d}"), 101.5, 103.5, 101.4, 103.4) for i in range(10)]
    p = ldnbrk.clean({"bias": "none", "stop": "mid", "tgtMult": 1, "bufTicks": 0})
    (x, win), = ldnbrk.engine("NQ", d, [], [], F, [], {}, None, p)
    assert x["side"] == "long" and x["e"] == 101.5 and x["stop"] == 100 and x["tgt"] == 103 and x["why"] == "target" and x["rhi"] == 101, x
    prev = [(mk(ev, f"16:{i * 5:02d}"), 100, 100.5, 99.5, 100) for i in range(12)]
    G = [(mk(ev, "18:00"), 100.1, 100.2, 99.95, 100.15)] + [(mk(ev, f"18:{5 + i * 5:02d}"), 100.1, 100.2, 99.9, 100) for i in range(20)]
    (y, _), = sungap.engine("NQ", "2026-03-09", [], [], G, [], {}, prev, sungap.clean({"smallPct": 0.25, "bigPct": 0.5}))    # a Monday session: +0.1% gap
    assert y["side"] == "short" and y["e"] == 100.15 and y["tgt"] == 100 and y["why"] == "target", y
    assert N.stamp(F, "18:00") == f"{ev}T18:00" and N.stamp(F, "02:00") == f"{d}T02:00"


def test_28_inside_bar():
    import insidebar
    d = "2026-03-10"
    rows = [(100, 100, 100, 100)] * 22 + [(100, 104, 98, 103), (102.5, 103, 100, 101), (101, 103.5, 100.8, 103.3)] + [(103.3, 111, 103.2, 110.5)] * 5
    F = _rth(d, rows)
    p = insidebar.clean({"tf": 5})
    (x, _), = insidebar.engine("NQ", d, [], F, F, [], {}, None, p)[:1]
    assert x["side"] == "long" and x["e"] == 103.25 and x["stop"] == 99.75 and abs(x["tgt"] - (103.25 + 2 * 3.5)) < 1e-9 and x["why"] == "target", x
    F2 = _rth(d, [(100, 100, 100, 100)] * 22 + [(100, 104, 98, 103), (102.5, 104.5, 100, 101)] + [(101, 102, 100.5, 101)] * 5)
    assert insidebar.engine("NQ", d, [], F2, F2, [], {}, None, p) == []          # the high pokes over the mother: not an inside bar
    F3 = _rth(d, [(100, 100, 100, 100)] * 22 + [(100, 104, 98, 103), (102.5, 103, 100, 101), (101, 103.5, 99.5, 101)] + [(101, 102, 100.5, 101)] * 5)
    assert insidebar.engine("NQ", d, [], F3, F3, [], {}, None, p) == []          # both sides broken in one candle: skipped


def test_29_more_ideas():
    import imom
    import onrev
    import tom
    d, ev = "2026-03-10", "2026-03-09"
    prev = [(f"{ev}T15:{i * 5:02d}", 100, 100.5, 99.5, 100) for i in range(12)] + [(f"{ev}T16:{i * 5:02d}", 100, 100.2, 99.8, 100) for i in range(12)]
    F = [(f"{d}T09:{25 + i * 5:02d}", 101, 101.2, 100.8, 101) for i in range(1)] + _rth(d, [(101, 101.5, 100.5, 101.2)] * 6 + [(101.2, 101.4, 101, 101.3)] * 66 + [(101.3, 102, 101.2, 102)] * 6)
    (x, _), = imom.engine("NQ", d, [], [], F, [], {}, prev, imom.clean({}))
    assert x["side"] == "long" and x["e"] == 101.3 and x["x"] == 102 and x["why"] == "time", x          # 10:00 above yesterday's 100 close: long 15:30-16:00
    (y, _), = onrev.engine("NQ", d, [], [], F, [], {}, prev, onrev.clean({"minPct": 0.5, "exitAt": "15:55"}))
    assert y["side"] == "short" and y["e"] == 101 and y["tgt"] == 100 and y["stop"] == 102, y            # +1% gap faded to the close, stop 1x the gap
    D = [("2026-01-%02d" % i, 100, 101, 99, 100 + i * 0.1, 1) for i in range(5, 31)] + [("2026-01-30", 100, 101, 99, 102, 1), ("2026-02-02", 102, 104, 101, 103, 1),
                                                                                       ("2026-02-03", 103, 105, 102, 104, 1), ("2026-02-04", 104, 106, 103, 105, 1)]
    D = sorted({x[0]: x for x in D}.values())
    ts = tom.run(D, tom.clean({"exitDay": 3}), set(x[0] for x in D))
    assert ts and ts[-1]["in"] == "2026-01-30T16:00" and ts[-1]["out"] == "2026-02-04T16:00" and ts[-1]["e"] == 102 and ts[-1]["x"] == 105, ts


def test_30_levels_tools():
    import confluence
    import cycles
    import futdata
    import walls
    day = futdata.daily("NQ")[-1][0]
    w = walls.density("NQ", day, {"lookback": "60"})
    ups = {x["k"]: x["pct"] for x in w["levels"] if x["side"] == "up"}
    assert ups["peak"] < ups["inner"] < ups["true"] < ups["over"] and w["sample"] == 60 and len(w["grid"]) == 78 and w["path"]
    assert w["fan"]["up"][0]["label"] == "Max" and w["odds"]["up"][0] == 100.0 and w["stats"]["lines"]
    assert walls.levels("NQ", day, {"lookback": "60", "match": "weekday"})["sample"] <= 60
    c = cycles.build("NQ", {"tf": "60", "days": "40"})
    assert c["vector"] and c["sd"] and len(c["sd"]) == len(c["candles"]) and all(m["text"][0] in "HL" for m in c["marks"])
    cf = confluence.build("NQ", {"tol": "20"})
    assert cf["levels"] and all(z["lo"] <= z["hi"] for z in cf["clusters"]) and all(z["score"] == len(set(x["src"] for x in z["levels"])) for z in cf["clusters"])
    for path, q in (("/walls", {"sym": "NQ", "day": day}), ("/cycles", {"sym": "ES", "tf": "15"}), ("/confluence", {"sym": "NQ"}), ("/chart", {"sym": "NQ", "walls": "1"})):
        st, r = call("GET", path, None, q)
        assert st == 200, (path, r)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
