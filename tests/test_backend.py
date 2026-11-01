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
                     ("nrbreak", {"days": 60, "cond": "either", "orMin": 30, "stop": "mid"}), ("fpdisp", {"days": 60, "minRR": 0}), ("fpdisp", {"days": 60, "fair": "vwap", "stop": "swing", "tf": 1})):
        s = strategy.create(SUB, {"setup": setup, "params": p})
        v = strategy.study(SUB, s["id"])
        assert v["study"]["n"] >= 0, setup
        if v["study"]["n"]:
            strategy.validate(SUB, s["id"])
        ts = strategy.trades(SUB, s["id"], "study")
        assert all(t["e"] > 1000 for t in ts), setup


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
    ovr = orbema.overlay("u1", orbema.clean({"tickers": ["NQ"], "days": 60, "emaOn": "rth"}), "NQ", futdata.trading_days("NQ")[-3], futdata.trading_days("NQ")[-1])
    assert all("09:30" <= x[0][11:16] < "16:00" for x in ovr["lines"][0]["pts"])
    ov = orbema.overlay("u1", orbema.clean({"tickers": ["NQ"], "days": 60}), "NQ", futdata.trading_days("NQ")[-3], futdata.trading_days("NQ")[-1])
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
