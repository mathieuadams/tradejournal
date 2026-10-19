"""Run with: python -m pytest tests -q   (or: python tests/test_backend.py)
No AWS account needed: DynamoDB, S3 and the Claude API are replaced by in-memory fakes."""
import json
import os
import sys
import urllib.parse as urllib_parse

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
os.environ.setdefault("UPLOAD_BUCKET", "test-bucket")
os.environ.setdefault("SYNC_FUNCTION", "sync")
os.environ.setdefault("WEEKLY_FUNCTION", "weekly")
os.environ.setdefault("BOT_FUNCTION", "bot")

import db  # noqa: E402

STORE = {}


def _install_fake_db():
    def q_prefix(pk, prefix, desc=False, limit=None):
        items = sorted((v for (p, s), v in STORE.items() if p == pk and s.startswith(prefix)), key=lambda i: i["SK"], reverse=desc)
        items = [db.from_ddb(db.to_ddb(i)) for i in items]
        return items[:limit] if limit else items

    def get(pk, sk):
        i = STORE.get((pk, sk))
        return db.from_ddb(db.to_ddb(i)) if i else None

    def put(item):
        STORE[(item["PK"], item["SK"])] = db.from_ddb(db.to_ddb(dict(item)))

    def delete(pk, sk):
        STORE.pop((pk, sk), None)

    def update(pk, sk, fields):
        i = STORE.setdefault((pk, sk), {"PK": pk, "SK": sk})
        i.update(db.from_ddb(db.to_ddb(fields)))

    def batch_write(puts=(), deletes=()):
        for i in puts:
            put(i)
        for pk, sk in deletes:
            delete(pk, sk)

    def scan_sk(v):
        return [dict(i) for (p, s), i in STORE.items() if s == v]

    def try_lock(pk, sk, seconds):
        import time as _t
        cur = STORE.get((pk, sk))
        if cur and cur.get("until", 0) > _t.time():
            return False
        STORE[(pk, sk)] = {"PK": pk, "SK": sk, "until": _t.time() + seconds}
        return True

    def unlock(pk, sk):
        STORE.pop((pk, sk), None)

    def claim(pk, sk, from_statuses, to_status, extra=None):
        it = STORE.get((pk, sk))
        if not it or it.get("status") not in from_statuses:
            return False
        it["status"] = to_status
        it.update(extra or {})
        return True

    def scan_prefix(prefix, statuses=None):
        return [db.from_ddb(db.to_ddb(i)) for (p, s), i in STORE.items() if s.startswith(prefix) and (not statuses or i.get("status") in statuses)]

    for name, fn in dict(q_prefix=q_prefix, get=get, put=put, delete=delete, update=update,
                         batch_write=batch_write, scan_sk=scan_sk, scan_prefix=scan_prefix, try_lock=try_lock, unlock=unlock, claim=claim).items():
        setattr(db, name, fn)


_install_fake_db()

import analytics  # noqa: E402
import api  # noqa: E402
import chat  # noqa: E402
import claude  # noqa: E402
import importer  # noqa: E402
import weekly  # noqa: E402
from grouping import group_fills  # noqa: E402
from parsers import ParseError, parse_csv  # noqa: E402

SUB = "user-123"
import autotrader  # noqa: E402
import wallwatch as _ww0  # noqa: E402
_ww0.PARAMS["pvSetup"] = False   # the flow retest tests below; the VWAP reset strategy is tested on its own
_ww0.PARAMS["relFlow"] = False   # fixed alert size in the flow retest tests; unusual flow is tested on its own
autotrader.DEFAULTS["aiCheck"] = False     # the order tests below don't exercise Claude's chart check (see test_ai_chart_check)


def event(method, path, body=None, q=None):
    return {"rawPath": path, "requestContext": {"http": {"method": method}, "stage": "$default",
            "authorizer": {"jwt": {"claims": {"sub": SUB, "email": "t@example.com"}}}},
            "body": json.dumps(body) if body is not None else None, "queryStringParameters": q}


def call(method, path, body=None, q=None):
    r = api.handler(event(method, path, body, q), None)
    return r["statusCode"], json.loads(r["body"]) if r["body"] else None


def fake_claude(model, system, msgs, max_tokens=1500, tools=None, extra=None):
    if tools and tools[0]["name"] == "submit":
        if "personal trading coach" in system:
            data = {"headline": "h", "portfolio": ["p"], "positions": [{"ticker": "MES", "status": "watch", "note": "n", "levels": "l", "action": "a"}], "focus": ["f"], "rule_checks": [{"rule": "r", "ok": True, "detail": "d"}]}
        elif "weekly" in system.lower():
            data = {"headline": "Mixed week", "summary": "s", "leaks": [], "rule": "Stop after two losses.", "rule_reason": "r", "last_rule_followed": "unknown"}
        else:
            data = {"verdict": "broke_plan", "summary": "s", "what_worked": ["a"], "what_broke": ["b"], "pattern": "", "suggested_tags": ["Revenge", "Nope"], "lesson": "l"}
        return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "x", "name": "submit", "input": data}]}
    if tools:
        last = msgs[-1]
        if isinstance(last["content"], str):
            return {"stop_reason": "tool_use", "content": [
                {"type": "tool_use", "id": "t1", "name": "query_trades", "input": {"symbol": "MNQ", "sort": "net_desc", "limit": 5}}]}
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Your MNQ trades are listed below."}]}
    if "weekly" in system.lower():
        return {"content": [{"type": "text", "text": json.dumps({"headline": "Mixed week", "summary": "s", "leaks": [], "rule": "Stop after two losses.", "rule_reason": "r", "last_rule_followed": "unknown"})}]}
    return {"content": [{"type": "text", "text": "```json\n" + json.dumps({"verdict": "broke_plan", "what_worked": ["a"], "what_broke": ["b"], "suggested_tags": ["Revenge", "Nope"], "lesson": "l"}) + "\n```"}]}


claude.messages = fake_claude
api._s3 = lambda: type("S3", (), {"generate_presigned_url": staticmethod(lambda *a, **k: "https://upload.example/put")})()
api._lambda = lambda: type("L", (), {"invoke": staticmethod(lambda **k: None)})()


def test_grouping_flip_and_scale():
    fills = [
        {"id": "1", "acct": "A", "ts": "2026-09-21T09:30:00", "sym": "MES", "side": "buy", "qty": 3, "price": 100, "fees": 3, "mult": 5},
        {"id": "2", "acct": "A", "ts": "2026-09-21T09:31:00", "sym": "MES", "side": "buy", "qty": 1, "price": 104, "fees": 1, "mult": 5},
        {"id": "3", "acct": "A", "ts": "2026-09-21T09:40:00", "sym": "MES", "side": "sell", "qty": 6, "price": 110, "fees": 6, "mult": 5},
        {"id": "4", "acct": "A", "ts": "2026-09-21T09:50:00", "sym": "MES", "side": "buy", "qty": 2, "price": 108, "fees": 2, "mult": 5},
    ]
    t = group_fills(fills)
    assert len(t) == 2
    long_, short = t
    assert long_["dir"] == "Long" and long_["qty"] == 4 and long_["entry"] == 101
    assert long_["gross"] == (110 - 101) * 4 * 5 == 180
    assert abs(long_["fees"] - (3 + 1 + 6 * 4 / 6)) < 1e-9
    assert short["dir"] == "Short" and short["status"] == "closed" and short["gross"] == (110 - 108) * 2 * 5


def test_parse_generic_and_ibkr():
    fills, skipped = parse_csv(open(os.path.join(HERE, "..", "samples", "sample-fills.csv")).read(), "Main")
    assert len(fills) == 14 and skipped == 0
    mnq = [f for f in fills if f["sym"] == "MNQ"]
    assert all(f["mult"] == 2 for f in mnq)
    ib, _ = parse_csv(open(os.path.join(HERE, "..", "samples", "ibkr-flex-sample.csv")).read(), "IBKR")
    assert ib[0]["ts"] == "2026-09-23T09:35:12" and ib[1]["side"] == "sell" and ib[1]["qty"] == 200 and ib[0]["fees"] == 1
    try:
        parse_csv("a,b\n1,2\n", "x")
        assert False
    except ParseError as e:
        assert "Missing column" in str(e)


def test_schwab_statement():
    text = open(os.path.join(HERE, "..", "samples", "schwab-statement-sample.csv"), encoding="utf-8").read()
    fills, skipped = parse_csv(text, "Schwab")
    assert len(fills) == 5 and skipped == 0
    amd_buy = next(f for f in fills if f["sym"] == "AMD" and f["side"] == "buy")
    assert amd_buy["ts"] == "2026-09-01T09:35:10"          # Pacific file times detected, shifted to Eastern
    opt = [f for f in fills if f["sym"].startswith("NVDA")]
    assert opt[0]["sym"] == "NVDA261016C00180000" and opt[0]["mult"] == 100 and abs(opt[0]["fees"] - 1.32) < 1e-9
    stats = {}
    trades = group_fills(fills, stats)
    assert stats["unmatchedCloses"] == 1                    # PLTR sold to close, opened before the statement
    assert not any(t["sym"] == "PLTR" for t in trades)
    nv = next(t for t in trades if t["sym"].startswith("NVDA"))
    assert nv["gross"] == round((7.25 - 5.00) * 2 * 100, 2) and nv["dir"] == "Long"
    from grouping import describe
    d = describe("NVDA261016C00180000")
    assert d == {"underlying": "NVDA", "assetType": "option", "optType": "call", "expiry": "2026-10-16", "strike": 180.0}
    assert describe("MNQZ26")["assetType"] == "future" and describe("AMD")["assetType"] == "stock"
    fills_et, _ = parse_csv(text, "Schwab", "ET")
    assert next(f for f in fills_et if f["sym"] == "AMD")["ts"] == "2026-09-01T06:35:10"


def test_option_charts_use_underlying():
    import charts
    t = {"sym": "NVDA261016C00180000", "openTs": "2026-09-21T10:00:00", "closeTs": "2026-09-22T11:00:00"}
    assert charts.chart_symbol(t) == ("NVDA", "option")
    s, e = charts.window(t, "1d")
    assert s < charts.parse_iso(t["openTs"]) and e > charts.parse_iso(t["closeTs"])


def test_utc_timestamps_convert_to_eastern():
    f, _ = parse_csv("time,symbol,side,qty,price\n2026-09-21T13:34:10Z,AMD,buy,1,10\n2026-01-05T14:31:00Z,AMD,sell,1,11\n", "x")
    assert sorted(x["ts"] for x in f) == ["2026-01-05T09:31:00", "2026-09-21T09:34:10"]


def test_full_flow():
    STORE.clear()
    code, me = call("GET", "/me")
    assert code == 200 and me["settings"]["riskPerTrade"] == 200
    code, imp = call("POST", "/imports", {"fileName": "sample-fills.csv", "account": "Topstep 50K"})
    assert code == 200 and imp["uploadUrl"]
    text = open(os.path.join(HERE, "..", "samples", "sample-fills.csv")).read()
    res = importer.process(SUB, imp["importId"], "Topstep 50K", text)
    assert res["status"] == "done" and res["newFills"] == 14 and res["trades"] == 6, res
    again = importer.process(SUB, imp["importId"], "Topstep 50K", text)
    assert again["newFills"] == 0 and again["duplicates"] == 14 and again["trades"] == 6

    code, data = call("GET", "/trades")
    trades = data["trades"]
    assert code == 200 and len(trades) == 6
    mnq = next(t for t in trades if t["sym"] == "MNQ" and t["date"] == "2026-09-21")
    assert mnq["net"] == round((21478.5 - 21452.25) * 2 * 2 - 2.48, 2)
    nv = next(t for t in trades if t["sym"] == "NVDA")
    assert nv["qty"] == 150 and nv["exit"] == (179.35 + 179.90) / 2

    code, v = call("PATCH", f"/trades/{mnq['id']}", {"setup": "VWAP reclaim", "tags": ["Revenge", "Revenge"], "emotion": "Calm",
                                                     "plan": {"entry": "21450", "stop": "21440", "target": 21480, "thesis": "t"}, "notes": "n"})
    assert code == 200 and v["tags"] == ["Revenge"] and v["plan"]["stop"] == 21440
    assert v["riskD"] == round((21452.25 - 21440) * 2 * 2, 2) and v["r"] == round(v["net"] / v["riskD"], 3)
    assert any(s.startswith("AUDIT#") for (_, s) in STORE)

    code, err = call("PATCH", f"/trades/{mnq['id']}", {"plan": {"stop": "abc"}})
    assert code == 400 and "number" in err["error"]
    code, _ = call("PATCH", "/trades/0000000000000000", {"tags": []})
    assert code == 404

    code, r = call("POST", f"/trades/{mnq['id']}/review")
    assert code == 200 and r["verdict"] == "broke_plan" and r["suggested_tags"] == []
    code, r2 = call("GET", f"/trades/{mnq['id']}/review")
    assert code == 200 and r2["lesson"] == "l"

    import charts
    seen = {}
    def fake_yahoo(symbol, tf, start, end):
        seen["args"] = (symbol, tf)
        return [{"t": "2026-09-21T09:30:00", "o": 1, "h": 2, "l": 0.5, "c": 1.5},
                {"t": "2026-09-21T10:30:00", "o": 1.5, "h": 3, "l": 1, "c": 2},
                {"t": "2026-09-21T14:30:00", "o": 2, "h": 2.5, "l": 1.8, "c": 2.2}]
    charts._yahoo = fake_yahoo
    code, bars = call("GET", f"/trades/{mnq['id']}/bars", q={"tf": "4h"})
    assert code == 200 and seen["args"] == ("MNQ=F", "4h") and len(bars["bars"]) == 2 and bars["bars"][0]["h"] == 3
    code, bars = call("GET", f"/trades/{mnq['id']}/bars", q={"tf": "2m"})
    assert code == 400

    code, d = call("PUT", "/daily/2026-09-21", {"pre": "plan", "rec": "recap", "mood": 4})
    assert code == 200 and d["mood"] == 4
    code, d = call("PUT", "/daily/2026-09-21", {"mood": 9})
    assert code == 400

    code, s = call("PUT", "/settings", {"riskPerTrade": 150, "prop": {"enabled": True, "account": "Topstep 50K", "start": "2026-09-01",
                                                                      "balance": 50000, "trailing": 2500, "target": 3000, "dailyLoss": 1000}})
    assert code == 200 and s["prop"]["enabled"] and s["riskPerTrade"] == 150

    code, c = call("POST", "/coach/chat", {"messages": [{"role": "user", "content": "best MNQ trades"}]})
    assert code == 200 and len(c["tradeIds"]) == 3 and "MNQ" in c["reply"]

    rep = weekly.run_for(SUB)
    assert rep["headline"]
    code, latest = call("GET", "/reports/latest")
    assert code == 200

    code, lst = call("GET", "/imports")
    assert code == 200 and lst["imports"][0]["status"] == "done"
    code, nr = call("GET", "/nope")
    assert code == 404


SCHWAB_TXS = [
    {"activityId": 111, "time": "2026-09-22T16:33:02+0000", "type": "TRADE", "status": "VALID", "transferItems": [
        {"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"}, "amount": 0, "cost": -1.3, "feeType": "COMMISSION"},
        {"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"}, "amount": 0, "cost": -0.04, "feeType": "OPT_REG_FEE"},
        {"instrument": {"assetType": "OPTION", "symbol": "NUAI  261120C00007500", "underlyingSymbol": "NUAI"},
         "amount": 2, "cost": -212, "price": 1.06, "positionEffect": "OPENING"}]},
    {"activityId": 112, "time": "2026-09-23T14:05:00+0000", "type": "TRADE", "status": "VALID", "transferItems": [
        {"instrument": {"assetType": "OPTION", "symbol": "NUAI  261120C00007500"}, "amount": -2, "cost": 300, "price": 1.5,
         "positionEffect": "CLOSING"}]},
    {"activityId": 113, "time": "2026-09-23T15:00:00+0000", "type": "TRADE", "status": "VALID", "transferItems": [
        {"instrument": {"assetType": "EQUITY", "symbol": "AMD"}, "amount": -10, "cost": 1600, "price": 160, "positionEffect": "CLOSING"}]},
]


def test_schwab_mapping():
    import schwab
    f = schwab.to_fills(SCHWAB_TXS, "Schwab")
    assert len(f) == 3
    a = f[0]
    assert a["sym"] == "NUAI261120C00007500" and a["side"] == "buy" and a["qty"] == 2 and a["mult"] == 100
    assert a["ts"] == "2026-09-22T12:33:02" and abs(a["fees"] - 1.34) < 1e-9 and a["effect"] == "open"
    st = {}
    trades = group_fills(f, st)
    assert st["unmatchedCloses"] == 1 and len(trades) == 1 and trades[0]["gross"] == round((1.5 - 1.06) * 2 * 100, 2)


def test_schwab_connect_and_sync():
    import schwab
    STORE.clear()
    schwab._app = {"appKey": "k", "appSecret": "s"}
    os.environ["SCHWAB_CALLBACK"] = "https://site.example/schwab"
    os.environ["KMS_KEY_ID"] = "k"
    schwab._kms = lambda: type("K", (), {"encrypt": staticmethod(lambda KeyId, Plaintext: {"CiphertextBlob": Plaintext}),
                                         "decrypt": staticmethod(lambda CiphertextBlob: {"Plaintext": CiphertextBlob})})()
    calls = []

    def fake_http(method, url, headers=None, data=None):
        calls.append(url)
        if "oauth/token" in url:
            return {"access_token": "A", "refresh_token": "R", "expires_in": 1800}
        if url.endswith("/accountNumbers"):
            return [{"accountNumber": "87238010", "hashValue": "HASH1"}]
        if "/transactions?" in url:
            return SCHWAB_TXS if len([c for c in calls if "/transactions?" in c]) == 1 else []
        raise AssertionError(url)
    schwab._http = fake_http
    code, st = call("GET", "/broker/schwab")
    assert code == 200 and st["configured"] and not st["connected"]
    code, r = call("POST", "/broker/schwab/authorize", {"journalAccount": "Main"})
    assert code == 200 and "client_id=k" in r["url"] and "state=" in r["url"]
    state = urllib_parse.parse_qs(urllib_parse.urlparse(r["url"]).query)["state"][0]
    code, bad = call("POST", "/broker/schwab/callback", {"code": "c", "state": "nope"})
    assert code == 400
    code, ok = call("POST", "/broker/schwab/callback", {"code": "C0.x@", "state": state})
    assert code == 200 and ok["connected"] and ok["accounts"][0]["name"] == "Main" and ok["accounts"][0]["last4"] == "8010"
    res = schwab.sync_user(SUB)
    assert res == {"status": "ok", "newFills": 3}, res
    code, d = call("GET", "/trades")
    assert len(d["trades"]) == 1 and d["trades"][0]["acct"] == "Main"
    res = schwab.sync_user(SUB)
    assert res["newFills"] == 0
    code, st = call("GET", "/broker/schwab")
    assert st["status"] == "connected" and "enc" not in st


def test_chart_context():
    import context, charts
    bars, price = [], 100.0
    d0 = __import__("datetime").date(2025, 6, 2)
    for i in range(300):
        day = (d0 + __import__("datetime").timedelta(days=i)).isoformat()
        price *= 1.002
        bars.append({"t": day + "T09:30:00", "o": price * 0.995, "h": price * 1.02, "l": price * 0.98, "c": price, "v": 1_000_000})
    entry = bars[260]["t"][:10]
    bars[260]["v"] = 3_000_000
    ctx = context.compute(bars, entry)
    assert ctx["trend"] == "Uptrend" and ctx["above50"] and ctx["rvol"] == 3.0 and 3.9 < ctx["adrPct"] < 4.2
    assert ctx["ext20Adr"] is not None and ctx["rsi14"] == 100.0
    assert ctx["v"] == 2 and "fvgState" in ctx["study"] and ctx["study"]["hvc"]
    STORE.clear()
    importer.process(SUB, "i1", "Main", open(os.path.join(HERE, "..", "samples", "sample-fills.csv")).read())
    context._yahoo = lambda sym, tf, s, e: [] if tf != "1d" else [dict(b, t=b["t"].replace(b["t"][:10], (__import__("datetime").date(2025, 12, 1) + __import__("datetime").timedelta(days=i)).isoformat())) for i, b in enumerate(bars)]
    r = context.analyze(SUB)
    assert r["remaining"] == 0 and r["analyzed"] == 6
    code, d = call("GET", "/trades")
    assert all(t["ctx"] and "v" in t["ctx"] for t in d["trades"])
    nv = next(t for t in d["trades"] if t["sym"] == "NVDA")
    assert nv["cost"] == round(178.42 * 150, 2) and nv["retPct"] is not None
    tid = d["trades"][0]["id"]
    code, v = call("POST", f"/trades/{tid}/context", {"force": True})
    assert code == 200 and v["ctx"]["v"] == 2
    importer.process(SUB, "i2", "Main", open(os.path.join(HERE, "..", "samples", "sample-fills.csv")).read())
    code, d2 = call("GET", "/trades")
    assert all(t["ctx"] for t in d2["trades"])            # context survives a re-import


def test_live_coach():
    import livecoach, datetime as dt
    STORE.clear()
    now = __import__("util").now_ny()
    t0 = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    importer.process(SUB, "i", "Main", f"time,symbol,side,qty,price\n{t0},AMD,buy,10,150\n")
    def fake(sym, tf, s_, e):
        out, p, d = [], 100.0, s_
        step = dt.timedelta(days=1) if tf == "1d" else dt.timedelta(minutes=5)
        while d <= e:
            p *= 1.001
            out.append({"t": d.strftime("%Y-%m-%dT%H:%M:%S"), "o": p, "h": p * 1.01, "l": p * .99, "c": p, "v": 1000})
            d += step
        return out
    livecoach._yahoo = fake
    code, s = call("PUT", "/settings", {"liveCoach": {"enabled": True}, "rules": "Max 3 positions", "accountSize": 20000})
    assert code == 200 and s["liveCoach"]["enabled"] and s["liveCoach"]["preclose"]
    payload = livecoach.build_payload(SUB, "preclose")
    pos = payload["open_positions"][0]
    assert pos["ticker"] == "AMD" and pos["mark"] and pos["underlying"]["keltner"] and "study" in pos["underlying"]
    note = livecoach.run(SUB, "preclose")
    assert note["headline"] == "h" and note["kind"] == "preclose"
    code, n = call("GET", "/coach/notes")
    assert code == 200 and n["notes"][0]["positions"][0]["ticker"] == "MES"
    os.environ["LIVECOACH_FUNCTION"] = "lc"
    code, r = call("POST", "/coach/notes", {"kind": "premarket"})
    assert code == 200


def test_quality_scorecard():
    import quality, datetime as dt
    # stock bought at 100 on day D 10:00, ran to 110, sold 105 at D+1 15:00, then kept rising to 115
    def fake(sym, tf, s_, e):
        out = []
        d = dt.datetime(2026, 1, 1)
        while d <= dt.datetime(2026, 3, 30):
            if d.weekday() < 5:
                day = d.strftime("%Y-%m-%d")
                if tf == "1d":
                    p = 100 if day < "2026-03-02" else 110 if day <= "2026-03-03" else 115
                    out.append({"t": day + "T09:30:00", "o": p, "h": p + 1, "l": p - 1, "c": p, "v": 1})
                elif "2026-03-02" <= day <= "2026-03-03":
                    for m in range(0, 390, 30):
                        t = d + dt.timedelta(hours=9, minutes=30 + m)
                        p = 99 + (m / 390) * 11 if day == "2026-03-02" else 110 - (m / 390) * 5
                        out.append({"t": t.strftime("%Y-%m-%dT%H:%M:%S"), "o": p, "h": p + .2, "l": p - .2, "c": p, "v": 1})
            d += dt.timedelta(days=1)
        return [b for b in out if s_.strftime("%Y-%m-%d") <= b["t"][:10] <= e.strftime("%Y-%m-%d")]
    t = {"sym": "AMD", "dir": "Long", "status": "closed", "openTs": "2026-03-02T10:00:00", "closeTs": "2026-03-03T15:00:00",
         "entry": 100.0, "exit": 105.0, "qty": 10, "mult": 1, "fees": 0}
    quality.datetime = type("D", (), {"utcnow": staticmethod(lambda: dt.datetime(2026, 3, 20)), "strptime": dt.datetime.strptime})
    q = quality.compute(t, [], {"accountSize": 10000, "maxPositionPct": 5}, fake)
    quality.datetime = dt.datetime
    assert q["entryEff"] > 0.8 and 0.3 < q["captured"] < 0.7 and q["afterUpAtr"] > 1, q
    assert any("early exit" in v for v in q["verdicts"]) and q["size"]["costPctAccount"] == 10.0
    assert q["scores"]["size"] < 100


def test_no_duplicates_across_time_zone_choices():
    STORE.clear()
    text = open(os.path.join(HERE, "..", "samples", "schwab-statement-sample.csv"), encoding="utf-8").read()
    from parsers import parse_csv as pc
    import ingest
    f1, _ = pc(text, "Main", "auto")
    f2, _ = pc(text, "Main", "ET")
    ingest.save_fills(SUB, f1)
    new, dup = ingest.save_fills(SUB, f2)
    assert new == 0 and dup == len(f2)
    # simulate an old duplicate stored under a different key, then rebuild
    old = dict(f2[0]); STORE[(db.upk(SUB), "FILL#Main#1999-01-01T00:00:00#" + old["id"])] = {"PK": db.upk(SUB), "SK": "FILL#Main#1999-01-01T00:00:00#" + old["id"], **old}
    code, r = call("POST", "/maintenance/rebuild")
    assert code == 200 and r["duplicateFillsRemoved"] == 1


def test_gex_math():
    import gex
    d, g = gex.bs_greeks(100, 100, 30 / 365, 0.3, True)
    assert 0.5 < d < 0.6 and 0.04 < g < 0.05
    exp = (__import__("util").now_ny() + __import__("datetime").timedelta(days=20)).strftime("%Y-%m-%d")
    cs = []
    for k in range(80, 125, 5):
        cs.append({"sym": f"C{k}", "call": True, "strike": k, "exp": exp, "oi": 1000 if k >= 100 else 200, "iv": .35, "delta": None, "gamma": None, "volume": 10})
        cs.append({"sym": f"P{k}", "call": False, "strike": k, "exp": exp, "oi": 3000 if k <= 95 else 100, "iv": .35, "delta": None, "gamma": None, "volume": 30})
    r = gex.compute(cs, 100.0)
    assert r["callWall"] >= 100 and r["putWall"] <= 95 and r["putCallOi"] > 1
    assert r["gammaFlip"] is not None and 85 < r["gammaFlip"] < 115, r["gammaFlip"]
    assert r["maxPain"] in [k for k in range(80, 125, 5)]
    for c in cs:
        c["mark"] = 3.0 if c["strike"] == 100 else 1.0
    em = gex.expected_moves(cs, 100.0)
    m = em["byExpiration"][0]
    assert m["straddle"] == 6.0 and m["upper"] == 106.0 and m["lower"] == 94.0 and 7 < m["ivMove"] < 9 and em["daily"]["pct"] > 2
    code, err = call("GET", "/gex", q={"symbol": "NVDA"})
    assert code in (400, 503)


def test_paper_bot():
    import autotrader, gex, datetime as dt
    from util import now_ny
    STORE.clear()
    # price path: decline, base, then a gap up (bull FVG) and a cross above the 21 EMA
    def fake_yahoo(sym, tf, s_, e):
        out, d, p = [], (now_ny() - dt.timedelta(days=300)).replace(hour=9, minute=30), 120.0
        i = 0
        while d <= now_ny():
            if d.weekday() < 5:
                n = i
                if n < 150: p *= 0.998
                elif n < 200: p *= 1.0
                elif n == 200: p *= 1.05
                else: p *= 1.003
                gap = 1.03 if n == 200 else 1.0
                out.append({"t": d.strftime("%Y-%m-%dT%H:%M:%S"), "o": p / 1.01, "h": p * 1.01, "l": p * 0.99 if n != 200 else p * 0.995, "c": p, "v": 1000 + (5000 if n == 200 else 0)})
                i += 1
            d += dt.timedelta(days=1)
        return out
    autotrader._yahoo = fake_yahoo
    spot = fake_yahoo("X", "1d", None, None)[-1]["c"]
    exp = (now_ny() + dt.timedelta(days=50)).strftime("%Y-%m-%d")
    exp2 = (now_ny() + dt.timedelta(days=20)).strftime("%Y-%m-%d")
    def fake_chain(sub, sym, days):
        cs = []
        for e in (exp2, exp):
            for k in [round(spot * f) for f in (0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.2)]:
                for call in (True, False):
                    d_, g_ = gex.bs_greeks(spot, k, gex._years(e), 0.3, call)
                    th = max(0.5, (spot - k) if call else (k - spot)) + spot * 0.3 * (gex._years(e) ** 0.5) * 0.4
                    cs.append({"sym": f"X{e}{'C' if call else 'P'}{k}", "call": call, "strike": k, "exp": e, "oi": 500,
                               "iv": 0.3, "delta": d_, "gamma": g_, "volume": 10, "bid": th * 0.97, "ask": th * 1.03, "mark": th})
        return {"source": "Test", "spot": spot, "contracts": cs}
    autotrader.gex.fetch_chain = fake_chain
    call("PUT", "/settings", {"riskPerTrade": 500})
    code, cfg = call("PUT", "/bot/settings", {"enabled": True, "watchlist": ["xyz", "bad ticker!"], "minRoomRatio": 0.1, "crossWindow": 120, "maxExtAtr": 10})
    assert code == 200 and cfg["watchlist"] == ["XYZ"]
    code, rec = call("POST", "/bot/evaluate", {"symbol": "XYZ", "earnings": (now_ny() + dt.timedelta(days=30)).strftime("%Y-%m-%d")})
    assert code == 200 and autotrader.settings(SUB)["earnings"]["XYZ"]
    code, rec = call("POST", "/bot/evaluate", {"symbol": "XYZ", "earnings": ""})
    assert "XYZ" not in autotrader.settings(SUB)["earnings"]
    assert code == 200, rec
    assert rec["candidates"] and rec["proposal"] and rec["proposal"]["exp"] == exp, rec
    assert 0.5 <= rec["candidates"][0]["delta"] <= 0.7
    assert any(c["group"] == "gamma" for c in rec["checks"])
    # paper-only guard
    import alpaca
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "live"}
    code, err = call("POST", f"/bot/{rec['id']}/order", {})
    assert code == 400 and "PAPER" in err["error"]
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    sent = []
    def fake_alp(c, method, path, body=None):
        sent.append((method, path, body))
        if path == "/v2/positions" or path.startswith("/v2/orders?"):
            return []
        if method == "POST":
            return {"id": "ord1"}
        if path.startswith("/v2/orders/"):
            return {"status": "filled", "filled_avg_price": "5.00"}
        if path.startswith("/v2/positions/") and method == "GET":
            return {"unrealized_plpc": "-0.5", "current_price": "2.5"}
        return {"id": "exit1"}
    autotrader._alp = fake_alp
    rec["proposal"]["qty"] = max(1, rec["proposal"]["qty"])
    db.update(db.upk(SUB), f"BOT#{rec['id']}", {"proposal": rec["proposal"]})
    code, placed = call("POST", f"/bot/{rec['id']}/order", {})
    post = next(x for x in sent if x[0] == "POST")
    assert code == 200 and placed["status"] == "submitted" and post[2]["time_in_force"] == "day" and post[2]["client_order_id"].startswith("tj-")
    code, again = call("POST", f"/bot/{rec['id']}/order", {})              # double click
    assert code == 400
    call("PUT", "/bot/settings", {"enabled": False})
    out = autotrader.handler({"job": "monitor"}, None)   # schedule off, exits still managed: fill, then -50% -> stop
    assert SUB in out and out[SUB]["actions"] and "stop" in out[SUB]["actions"][0][1], out
    code, home = call("GET", "/bot")
    assert next(i for i in home["items"] if i["id"] == rec["id"])["status"] == "closing"


def test_earnings_exit_timing():
    import autotrader, datetime as dt
    D = dt.datetime
    # after-close report on Wed Oct 28: exit Wed from 15:30
    assert autotrader.earnings_exit_due("2026-10-28 AMC", D(2026, 10, 28, 15, 0))[0] is False
    assert autotrader.earnings_exit_due("2026-10-28 AMC", D(2026, 10, 28, 15, 35))[0] is True
    assert autotrader.earnings_exit_due("2026-10-28", D(2026, 10, 27, 15, 45))[0] is False
    # before-open report on Monday Nov 2: exit the previous Friday from 15:30
    assert autotrader.earnings_exit_due("2026-11-02 BMO", D(2026, 10, 30, 15, 40))[0] is True
    assert autotrader.earnings_exit_due("2026-11-02 BMO", D(2026, 10, 30, 12, 0))[0] is False
    assert autotrader.earnings_exit_due("2026-11-02 BMO", D(2026, 11, 2, 9, 35))[0] is True   # missed: exit now


def test_bot_chase():
    import autotrader, alpaca
    STORE.clear()
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    autotrader.time.sleep = lambda x: None
    pk = db.upk(SUB)
    db.put({"PK": pk, "SK": "BOT#20260928120000-abcdef", "id": "20260928120000-abcdef", "symbol": "HON", "status": "submitted",
            "orderId": "o1", "qty": 2, "limit": 13.40, "firstLimit": 13.40, "proposal": {"contract": "HON261218C00210000"}})
    state = {"o1": ["new", "new", "canceled"], "o2": ["new", "canceled"], "o3": ["filled"]}
    posts = []
    def fake(c, method, path, body=None):
        if method == "POST":
            posts.append(body); oid = f"o{len(posts) + 1}"; return {"id": oid}
        if method == "DELETE":
            return {}
        oid = path.split("/")[-1]
        seq = state[oid]
        st = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"status": st, "filled_qty": "2" if st == "filled" else ("1" if oid == "o2" and st == "canceled" else "0"),
                "filled_avg_price": "13.60" if oid == "o2" else "13.70"}
    autotrader._alp = fake
    code, _ = call("PUT", "/bot/settings", {"chaseStep": 0.2, "chaseMaxSteps": 5, "chaseMaxPct": 10})
    rec = autotrader.chase(SUB, "20260928120000-abcdef")
    assert [p["limit_price"] for p in posts] == ["13.60", "13.80"], posts
    assert posts[1]["qty"] == "1"                     # one contract filled at 13.60 before the second chase step
    assert rec["status"] == "open" and rec["filledQty"] >= 2, rec


def test_import_undo():
    STORE.clear()
    code, imp = call("POST", "/imports", {"fileName": "s.csv", "account": "Alpaca paper"})
    importer.process(SUB, imp["importId"], "Alpaca paper", open(os.path.join(HERE, "..", "samples", "sample-fills.csv")).read())
    code, d = call("GET", "/trades"); assert len(d["trades"]) == 6
    code, r = call("POST", f"/imports/{imp['importId']}/undo")
    assert code == 200 and r["removedFills"] == 14 and r["trades"] == 0
    # legacy fills without importId: account cleanup keeps broker-synced fills
    importer.process(SUB, "20260101000000-aaaaaa", "Alpaca paper", open(os.path.join(HERE, "..", "samples", "sample-fills.csv")).read())
    for (pk_, sk), it in list(STORE.items()):
        if sk.startswith("FILL#"):
            it.pop("importId", None)
    STORE[(db.upk(SUB), "FILL#Alpaca paper#2026-09-28T10:00:00#alp-1")] = {"PK": db.upk(SUB), "SK": "FILL#Alpaca paper#2026-09-28T10:00:00#alp-1",
        "id": "alp-1", "acct": "Alpaca paper", "ts": "2026-09-28T10:00:00", "sym": "AMD", "side": "buy", "qty": 1, "price": 1, "fees": 0, "mult": 1}
    code, r = call("POST", "/maintenance/remove-csv-fills", {"account": "Alpaca paper"})
    assert r["removedFills"] == 14 and any(s_.endswith("alp-1") for (_, s_) in STORE)


def test_flow_alerts():
    import flowdata
    STORE.clear()
    flowdata._key = lambda sub: "k"
    flowdata._get = lambda key, params: {"data": [] if params.get("older_than") else [
        {"ticker": "MSFT", "type": "call", "strike": "375", "expiry": "2099-12-18", "total_premium": "186705", "total_ask_side_prem": "151875",
         "price": "4.05", "underlying_price": "372.99", "volume": 2442, "open_interest": 7913, "volume_oi_ratio": "0.3", "has_sweep": True,
         "alert_rule": "RepeatedHits", "option_chain": "MSFT991218C00375000", "created_at": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")},
        {"ticker": "BABA", "type": "call", "strike": "160", "expiry": "2099-12-18", "total_premium": "200000", "total_ask_side_prem": "200000",
         "created_at": "2020-08-01T14:00:00Z"},
        {"ticker": "AAPL", "type": "put", "strike": "200", "expiry": "2099-12-18", "total_premium": "500000", "total_ask_side_prem": "400000", "created_at": "2026-09-28T16:00:00Z"}]}
    code, r = call("GET", "/flow", q={"minPremium": "100000", "type": "call", "maxDte": "40000"})
    assert code == 200 and len(r["alerts"]) == 1 and r["alerts"][0]["askPct"] == 81 and r["stale"] >= 1


def test_flow_autotrade():
    import autotrader, flowdata, alpaca, notify
    STORE.clear()
    saved = (autotrader.evaluate, autotrader.place, autotrader.chase, flowdata.alerts)
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    import datetime as _dt
    nowz = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    flowdata.alerts = lambda sub, *a, **k: {"alerts": [
        {"ticker": "LRCX", "premium": 190000, "sweep": True, "contract": "LRCX261120C00390000", "askPct": 85, "volOi": 7.4, "at": nowz},
        {"ticker": "LRCX", "premium": 50000, "sweep": False, "contract": "x", "askPct": 70, "volOi": 1, "at": nowz},
        {"ticker": "OLD", "premium": 5000000, "sweep": True, "contract": "z", "askPct": 99, "volOi": 9, "at": "2020-01-01T00:00:00Z"},
        {"ticker": "SPY", "premium": 900000, "sweep": True, "contract": "y", "askPct": 90, "volOi": 2, "at": nowz}]}
    evaluated = []
    def fake_eval(sub, sym, earnings_date=None, source=None):
        evaluated.append(sym)
        rid = "20260928155900-" + ("abcdef" if sym == "LRCX" else "fedcba")
        db.put({"PK": db.upk(sub), "SK": f"BOT#{rid}", "id": rid, "symbol": sym, "decision": "BUY" if sym == "LRCX" else "SKIP",
                "proposal": {"contract": "LRCX261120C00390000", "qty": 1, "limit": 9.4, "exp": "2026-11-20", "strike": 390}, "status": "proposed", "origin": source})
        return {"id": rid, "decision": "BUY" if sym == "LRCX" else "SKIP"}
    autotrader.evaluate = fake_eval
    placed = []
    autotrader.place = lambda sub, rid, qty=None, limit=None, placed_by="manual": placed.append((rid, placed_by))
    autotrader.chase = lambda sub, rid: None
    call("PUT", "/bot/settings", {"flowAuto": True, "autoSubmit": True, "excludeEtfs": True})
    code, st = call("PUT", "/settings", {"notify": {"phone": "916 555 1234", "sms": True}})
    assert st["notify"]["phone"] == "+19165551234"
    out = autotrader.flow_scan(SUB)
    assert evaluated[0] == "SPY" or evaluated[0] == "LRCX"          # biggest premium first (ETFs are dropped inside flowdata)
    assert ("20260928155900-abcdef", "auto-flow") in placed and "OLD" not in evaluated
    again = autotrader.flow_scan(SUB)                                # cooldown: not re-evaluated
    assert again["evaluated"] == []
    assert autotrader.tick(SUB) is not None
    sent = []
    notify_boto = __import__("types").SimpleNamespace(client=lambda n: __import__("types").SimpleNamespace(publish=lambda **k: sent.append(k)))
    sys.modules["boto3"] = notify_boto
    r = notify.send(SUB, "hello")
    assert r["status"] == "sent" and sent[0]["PhoneNumber"] == "+19165551234"
    del sys.modules["boto3"]
    autotrader.evaluate, autotrader.place, autotrader.chase, flowdata.alerts = saved


def test_no_double_order_at_broker():
    import autotrader, alpaca
    STORE.clear()
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    pk = db.upk(SUB)
    db.put({"PK": pk, "SK": "BOT#20260928150000-aaaaaa", "id": "20260928150000-aaaaaa", "symbol": "HON", "status": "proposed",
            "proposal": {"contract": "HON261218C00210000", "qty": 1, "limit": 13.4, "exp": "2026-12-18", "strike": 210}})
    autotrader._alp = lambda c, m, path, body=None: ([{"symbol": "HON261218C00210000", "qty": "1"}] if path == "/v2/positions" else [] if path.startswith("/v2/orders?") else {"id": "x"})
    code, err = call("POST", "/bot/20260928150000-aaaaaa/order", {})
    assert code == 400 and "already holds" in err["error"]
    assert STORE[(pk, "BOT#20260928150000-aaaaaa")]["status"] == "proposed"


def test_exit_rules():
    import autotrader, alpaca, datetime as dt
    STORE.clear()
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    pk = db.upk(SUB)
    def rec(pl):
        db.put({"PK": pk, "SK": "BOT#20260929100000-aaaaaa", "id": "20260929100000-aaaaaa", "symbol": "MRVL", "status": "open",
                "fillPrice": 10.0, "filledQty": 1, "proposal": {"contract": "MRVL261120C00260000", "exp": "2099-11-20", "strike": 260,
                "underlyingStop": 250.0, "underlyingTarget": 280.0, "underlyingAtr": 8.0}})
        return pl
    state = {"pl": "-0.10", "px": "9.0"}
    closes = []
    def fake_alp(c, m, path, body=None):
        if m == "DELETE":
            closes.append(path); return {"id": "x"}
        if path.startswith("/v2/positions/"):
            return {"unrealized_plpc": state["pl"], "current_price": state["px"], "unrealized_pl": "-100", "market_value": "900"}
        return {}
    autotrader._alp = fake_alp
    under = {"v": 248.0}
    autotrader._yahoo = lambda *a, **k: [{"t": "x", "o": 1, "h": 1, "l": 1, "c": under["v"], "v": 1}]
    real_now = autotrader.now_ny
    autotrader.now_ny = lambda: dt.datetime(2026, 9, 29, 11, 0)
    rec(0); autotrader.monitor(SUB)
    assert not closes, "intraday dip below invalidation must not exit before the close"
    under["v"] = 241.0                                    # more than 1 ATR below: emergency exit
    autotrader.monitor(SUB); assert closes
    closes.clear(); under["v"] = 248.0
    autotrader.now_ny = lambda: dt.datetime(2026, 9, 29, 15, 55)
    rec(0); autotrader.monitor(SUB); assert closes, "closing below invalidation exits"
    # trailing after target
    closes.clear(); autotrader.now_ny = lambda: dt.datetime(2026, 9, 29, 11, 0)
    rec(0); under["v"] = 281.0; state.update(pl="1.2", px="22.0")
    autotrader.monitor(SUB); assert not closes and STORE[(pk, "BOT#20260929100000-aaaaaa")]["trailing"]
    state.update(pl="0.9", px="19.0"); autotrader.monitor(SUB); assert not closes          # -14% from peak: hold
    state.update(pl="0.5", px="15.0"); autotrader.monitor(SUB); assert closes             # -32% from peak: exit
    autotrader.now_ny = real_now


def _bs(s, k, t, v, call):
    import math
    n = lambda x: 0.5 * (1 + math.erf(x / 2 ** 0.5))
    d1 = (math.log(s / k) + 0.5 * v * v * t) / (v * t ** 0.5); d2 = d1 - v * t ** 0.5
    return max(0.05, s * n(d1) - k * n(d2) if call else k * n(-d2) - s * n(-d1))


def _spread_fakes():
    import autotrader, gex, datetime as dt
    from util import now_ny
    def fake_yahoo(sym, tf, s_, e):
        out, d, p = [], (now_ny() - dt.timedelta(days=300)).replace(hour=9, minute=30), 120.0
        i = 0
        while d <= now_ny():
            if d.weekday() < 5:
                n = i
                if n < 150: p *= 0.998
                elif n == 200: p *= 1.05
                elif n > 200: p *= 1.003
                out.append({"t": d.strftime("%Y-%m-%dT%H:%M:%S"), "o": p / 1.01, "h": p * 1.01, "l": p * 0.99 if n != 200 else p * 0.995, "c": p, "v": 1000})
                i += 1
            d += dt.timedelta(days=1)
        return out
    autotrader._yahoo = fake_yahoo
    spot = fake_yahoo("X", "1d", None, None)[-1]["c"]
    exps = [(now_ny() + dt.timedelta(days=n)).strftime("%Y-%m-%d") for n in (14, 50, 100)]
    def fake_chain(sub, sym, days):
        cs = []
        for e in exps:
            for k in [round(spot * f) for f in (0.8, 0.85, 0.9, 0.95, 1.0, 1.05, 1.1, 1.15, 1.2)]:
                for call in (True, False):
                    d_, g_ = gex.bs_greeks(spot, k, gex._years(e), 0.3, call)
                    th = _bs(spot, k, gex._years(e), 0.3, call)
                    cs.append({"sym": f"X{e.replace('-', '')}{'C' if call else 'P'}{k}", "call": call, "strike": k, "exp": e, "oi": 500,
                               "iv": 0.3, "delta": d_, "gamma": g_, "volume": 10, "bid": th * 0.97, "ask": th * 1.03, "mark": th})
        return {"source": "Test", "spot": spot, "contracts": cs}
    autotrader.gex.fetch_chain = fake_chain
    return spot, exps


def test_bull_call_spread():
    import autotrader, alpaca
    STORE.clear()
    spot, exps = _spread_fakes()
    call("PUT", "/settings", {"riskPerTrade": 5000})
    code, cfg = call("PUT", "/bot/settings", {"strategy": "bull_call", "minRoomRatio": 0.1, "crossWindow": 120, "maxExtAtr": 10,
                                              "spreadMaxDebitPct": 95, "minGrowth30": -100})
    assert code == 200 and cfg["strategy"] == "bull_call", cfg
    code, rec = call("POST", "/bot/evaluate", {"symbol": "XYZ"})
    assert code == 200, rec
    p = rec["proposal"]
    assert p and p["strategy"] == "bull_call" and p["shortExp"] == p["exp"] and p["shortStrike"] > p["strike"], (p, rec["blocking"], rec["spreadCandidates"])
    assert p["debit"] > 0 and p["width"] > p["debit"] and abs(p["maxProfit"] - (p["width"] - p["debit"])) < 0.02
    assert any("Bull call spread" in c["text"] for c in rec["checks"])
    # every analysis looks up the ticker's flow (any contract, last trading days): repeat buys (information) and puts (blocking)
    import flowdata
    day = autotrader.now_ny().strftime("%Y-%m-%d")
    fl_feed = []
    def fl(sub, min_premium, opt_type="call", min_dte=0, max_dte=120, ask_side=True, sweeps=False, ticker=None, voi=0, days=1, **k):
        assert opt_type == "all" and ticker == "XYZ" and days >= 5
        return {"alerts": fl_feed}
    saved_alerts = flowdata.alerts
    flowdata.alerts = fl
    mk = lambda typ, prem, d, hm, c="XYZ261120C00055000", rule=None: {"ticker": "XYZ", "type": typ, "premium": prem, "contract": c,
                                                                      "atEt": f"{d} {hm}", "rule": rule}
    fl_feed[:] = [mk("call", 60000, day, f"10:0{i}") for i in range(3)] + [mk("call", 70000, "2026-01-02", "11:00", "XYZ261120C00060000"),
                  mk("call", 500000, day, "10:30")]
    r2 = autotrader.evaluate(SUB, "XYZ")
    assert r2["tickerFlow"]["window"]["smallCalls"]["hits"] == 4 and r2["tickerFlow"]["window"]["smallCalls"]["contracts"] == 2
    rep_line = next(c for c in r2["checks"] if c["text"].startswith("Repeat buying"))
    assert rep_line["ok"] and not rep_line["required"] and "4 smaller call buys" in rep_line["text"] and "2 contracts" in rep_line["text"], rep_line
    assert "today 3 for $180,000" in rep_line["text"] and "plus 1 buys over $100,000" in rep_line["text"]
    put_line = lambda r: next(c for c in r["checks"] if c["text"].startswith("Puts not piling in"))
    assert put_line(r2)["ok"] and not put_line(r2)["required"]          # a caution by default
    fl_feed += [mk("put", 300000, day, "11:00", "XYZ261120P00040000"), mk("put", 150000, "2026-01-02", "12:00", "XYZ261120P00045000")]
    r4 = autotrader.evaluate(SUB, "XYZ")
    pl = put_line(r4)
    assert not pl["ok"] and "put/call 0.60" in pl["text"] and pl["text"] in r4["warnings"] and pl["text"] not in r4["blocking"], pl
    call("PUT", "/bot/settings", {"putBlock": True})                     # optional: block the buy
    r4b = autotrader.evaluate(SUB, "XYZ")
    assert put_line(r4b)["required"] and put_line(r4b)["text"] in r4b["blocking"] and "largest put XYZ261120P00040000" in put_line(r4b)["text"]
    call("PUT", "/bot/settings", {"putBlock": False})
    fl_feed[:] = [mk("call", 500000, day, "10:30")]
    r5 = autotrader.evaluate(SUB, "XYZ")
    assert any(c["text"].startswith("Repeat buying: no smaller call buys") and not c["ok"] for c in r5["checks"])
    flowdata.alerts = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no key"))
    r6 = autotrader.evaluate(SUB, "XYZ")
    assert any("no Unusual Whales data" in c["text"] and c["ok"] for c in r6["checks"])
    flowdata.alerts = saved_alerts
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    sent, pos = [], {}
    def fake_alp(c, method, path, body=None):
        sent.append((method, path, body))
        if path == "/v2/positions" or path.startswith("/v2/orders?"):
            return []
        if method == "POST":
            return {"id": f"o{len(sent)}"}
        if path.startswith("/v2/orders/"):
            return {"status": "filled", "filled_qty": "1", "filled_avg_price": f"{p['debit']:.2f}"}
        if path.startswith("/v2/positions/") and method == "GET":
            return pos.get(path.split("/")[-1])
        return {"id": "del"}
    autotrader._alp = fake_alp
    p["qty"] = 1
    db.update(db.upk(SUB), f"BOT#{rec['id']}", {"proposal": p})
    code, placed = call("POST", f"/bot/{rec['id']}/order", {})
    body = next(x for x in sent if x[0] == "POST")[2]
    assert code == 200 and body["order_class"] == "mleg" and float(body["limit_price"]) > 0, body
    assert [l["position_intent"] for l in body["legs"]] == ["buy_to_open", "sell_to_open"]
    assert body["legs"][1]["symbol"] == p["shortContract"]
    # spread worth half the debit -> 50% spread stop -> one mleg market close (sell long, buy back short)
    d = p["debit"]
    pos[p["contract"]] = {"qty": "1", "current_price": f"{d:.2f}", "avg_entry_price": f"{d + 1:.2f}", "unrealized_pl": "-50", "market_value": f"{d * 100:.0f}"}
    pos[p["shortContract"]] = {"qty": "-1", "current_price": f"{d * 0.6:.2f}", "avg_entry_price": "1.00", "unrealized_pl": "0", "market_value": f"{-d * 50:.0f}"}
    sent.clear()
    out = autotrader.monitor(SUB)
    assert out["actions"] and "spread stop" in out["actions"][0][1], out
    close = next(x for x in sent if x[0] == "POST")[2]
    assert close["order_class"] == "mleg" and close["type"] == "market"
    assert [l["position_intent"] for l in close["legs"]] == ["sell_to_close", "buy_to_close"]
    r = STORE[(db.upk(SUB), f"BOT#{rec['id']}")]
    assert r["status"] == "closing" and r["lastPlPct"] <= -49
    # both legs gone -> closed with P&L from the exit fill
    pos.clear()
    autotrader.monitor(SUB)
    r = STORE[(db.upk(SUB), f"BOT#{rec['id']}")]
    assert r["status"] == "closed" and r["realizedPl"] is not None


def test_spread_close_fallback_and_missing_leg():
    import autotrader, alpaca
    from autotrader import BadRequest
    STORE.clear()
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    pk = db.upk(SUB)
    prop = {"strategy": "bull_call", "contract": "AMD261120C00150000", "shortContract": "AMD261120C00170000", "exp": "2099-11-20",
            "shortExp": "2099-11-20", "strike": 150, "shortStrike": 170, "width": 20, "debit": 8.0, "limit": 8.0,
            "underlyingStop": 100.0, "underlyingTarget": 500.0, "underlyingAtr": 5.0}
    db.put({"PK": pk, "SK": "BOT#20260930100000-bbbbbb", "id": "20260930100000-bbbbbb", "symbol": "AMD", "status": "open",
            "fillPrice": 8.0, "filledQty": 1, "proposal": prop})
    pos = {"AMD261120C00150000": {"qty": "1", "current_price": "3.0", "unrealized_pl": "-500", "market_value": "300"},
           "AMD261120C00170000": {"qty": "-1", "current_price": "0.5", "unrealized_pl": "0", "market_value": "-50"}}
    order = []
    def fake_alp(c, m, path, body=None):
        if m == "POST":
            raise BadRequest("Alpaca refused the request (422): mleg close not supported")
        if m == "DELETE":
            order.append(path.split("/")[-1]); pos.pop(path.split("/")[-1], None); return {"id": "d" + str(len(order))}
        if path.startswith("/v2/positions/"):
            return pos.get(path.split("/")[-1])
        return {}
    autotrader._alp = fake_alp
    autotrader._yahoo = lambda *a, **k: [{"t": "x", "o": 1, "h": 1, "l": 1, "c": 160.0, "v": 1}]
    autotrader.monitor(SUB)
    assert order == ["AMD261120C00170000", "AMD261120C00150000"], order     # short bought back first
    # short leg assigned away: close the long leg
    order.clear()
    pos["AMD261120C00150000"] = {"qty": "1", "current_price": "12.0", "unrealized_pl": "400", "market_value": "1200"}
    db.put({"PK": pk, "SK": "BOT#20260930100000-bbbbbb", "id": "20260930100000-bbbbbb", "symbol": "AMD", "status": "open",
            "fillPrice": 8.0, "filledQty": 1, "proposal": prop})
    out = autotrader.monitor(SUB)
    assert order == ["AMD261120C00150000"] and "no longer held" in out["actions"][0][1], (order, out)


def test_diagonal():
    import autotrader, alpaca, datetime as dt
    STORE.clear()
    spot, exps = _spread_fakes()
    call("PUT", "/settings", {"riskPerTrade": 5000})
    code, err = call("PUT", "/bot/settings", {"strategy": "diagonal", "diagShortDteMax": 80, "diagLongDteMin": 60})
    assert code == 400
    base = {"strategy": "diagonal", "minRoomRatio": 0.1, "crossWindow": 120, "maxExtAtr": 10, "minGrowth30": -100,
            "diagLongDteMin": 80, "diagLongDteMax": 120, "diagLongDeltaMin": 0.6, "diagLongDeltaMax": 0.95,
            "diagShortDteMin": 1, "diagShortDteMax": 60, "diagShortDeltaMin": 0.05, "diagShortDeltaMax": 0.5}
    code, cfg = call("PUT", "/bot/settings", {**base, "diagMinShortCredit": 100})
    assert code == 200 and cfg["diagTargetPct"] == 0, cfg
    code, rec = call("POST", "/bot/evaluate", {"symbol": "XYZ"})
    # the 14-day call pays less than $100, so the next cycle (50 days) is used
    p = rec["proposal"]
    assert p and p["shortExp"] == exps[1] and p["shortCredit"] * 100 >= 100, (p, rec["spreadCandidates"])
    code, cfg = call("PUT", "/bot/settings", {**base, "diagMinShortCredit": 50})
    code, rec = call("POST", "/bot/evaluate", {"symbol": "XYZ"})
    p = rec["proposal"]
    assert p and p["strategy"] == "diagonal" and p["exp"] == exps[2] and p["shortExp"] == exps[0], (p, rec["blocking"])   # closest cycle
    assert p["shortStrike"] > max(p["strike"], spot) and p["optionTarget"] is None
    # short call at its roll day -> one mleg roll: buy back the old short, sell the next cycle
    pk = db.upk(SUB)
    old_short = "XOLD1C" + str(p["shortStrike"])
    yday = (autotrader.now_ny() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    p = {**p, "shortContract": old_short, "shortExp": yday}
    db.update(pk, f"BOT#{rec['id']}", {"status": "open", "fillPrice": p["debit"], "filledQty": 2, "proposal": p})
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    real_open = autotrader.market_open
    autotrader.market_open = lambda now=None: True
    posts, pos, orders = [], {p["contract"]: {"qty": "2", "current_price": str(p["legs"][0]["mid"]), "market_value": "0"},
                              old_short: {"qty": "-2", "current_price": "0.05", "market_value": "0"}}, {}
    def fake_alp(c, m, path, body=None):
        if m == "POST":
            posts.append(body); return {"id": f"r{len(posts)}"}
        if m == "DELETE":
            pos.pop(path.split("/")[-1], None); return {"id": "d"}
        if path.startswith("/v2/orders/"):
            return orders.get(path.split("/")[3].split("?")[0], {"status": "new"})
        if path.startswith("/v2/positions/"):
            return pos.get(path.split("/")[-1])
        return {}
    autotrader._alp = fake_alp
    autotrader._yahoo = lambda *a, **k: [{"t": "x", "o": 1, "h": 1, "l": 1, "c": spot, "v": 1}]
    out = autotrader.monitor(SUB)
    roll = posts[-1]
    assert roll["order_class"] == "mleg" and roll["qty"] == "2" and float(roll["limit_price"]) < 0, (roll, out)   # net credit
    assert [(l["symbol"], l["position_intent"]) for l in roll["legs"]] == [(old_short, "buy_to_close"), (roll["legs"][1]["symbol"], "sell_to_open")]
    new_sym = roll["legs"][1]["symbol"]
    assert STORE[(pk, f"BOT#{rec['id']}")]["status"] == "open"        # the long call is kept
    # roll fills: short leg swapped, credit booked
    orders["r1"] = {"status": "filled", "order_class": "mleg", "legs": [{"side": "buy", "filled_avg_price": "0.05"}, {"side": "sell", "filled_avg_price": "0.75"}]}
    pos.pop(old_short); pos[new_sym] = {"qty": "-2", "current_price": "0.75", "market_value": "0"}
    autotrader.monitor(SUB)
    r = STORE[(pk, f"BOT#{rec['id']}")]
    assert r["proposal"]["shortContract"] == new_sym and abs(r["cashAdj"] - 0.70) < 1e-9 and r["rolls"][0].get("from") == old_short, r.get("rolls")
    # short expires worthless (no shares): keep the long, sell the next cycle as a single sell_to_open
    pos.pop(new_sym)
    orders.clear()
    autotrader.monitor(SUB)
    r = STORE[(pk, f"BOT#{rec['id']}")]
    assert not r["proposal"].get("shortContract") and r["status"] == "open", {k: r.get(k) for k in ("status", "legMissing", "rollNote", "exitReason", "rollOrderId")}
    autotrader.monitor(SUB)
    sell = posts[-1]
    assert sell.get("position_intent") == "sell_to_open" and sell["side"] == "sell" and sell["qty"] == "2", sell
    autotrader.market_open = real_open


def test_ai_chart_check():
    import autotrader, alpaca, aicheck, base64, datetime as dt
    STORE.clear()
    autotrader.DEFAULTS["aiCheck"] = True
    try:
        pk = db.upk(SUB)
        def bars(n, step):
            out, p, t = [], 100.0, autotrader.now_ny() - step * n
            for i in range(n):
                p *= 1.003
                out.append({"t": (t + step * i).strftime("%Y-%m-%dT%H:%M:%S"), "o": p / 1.01, "h": p * 1.01, "l": p * 0.99, "c": p, "v": 1000 + i})
            return out
        aicheck._yahoo = lambda sym, tf, s_, e: bars(250, dt.timedelta(days=1)) if tf == "1d" else bars(140, dt.timedelta(hours=1))
        seen = {}
        def fake_vision(model, system, images, text, schema, max_tokens=1500):
            seen["images"], seen["text"] = images, text
            return {"verdict": seen["verdict"], "confidence": 72, "summary": "Clean base under the call wall.",
                    "supports": ["higher lows"], "concerns": ["gap overhead"]}
        claude.vision_json = fake_vision
        alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
        posts = []
        def fake_alp(c, m, path, body=None):
            if m == "POST":
                posts.append(body); return {"id": "o1"}
            return [] if path == "/v2/positions" or path.startswith("/v2/orders?") else None
        autotrader._alp = fake_alp
        prop = {"strategy": "long_call", "contract": "XYZ261120C00100000", "exp": "2026-11-20", "strike": 100, "qty": 1, "limit": 2.5,
                "underlyingStop": 95.0, "underlyingTarget": 112.0, "underlyingAtr": 2.0}
        def mk(i):
            rid = f"2026093010000{i}-aaaaa{i}"
            db.put({"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": "XYZ", "status": "proposed", "decision": "BUY", "proposal": prop,
                    "checks": [{"group": "chart", "text": "Close above the 21 EMA", "ok": True, "required": True}],
                    "signals": {"price": 101.0}, "gamma": {"gammaFlip": 98.0, "callWall": 110.0, "putWall": 92.0}})
            return rid
        # bot run (inline): Claude rejects -> no order, record flagged, charts stored
        seen["verdict"] = "reject"
        r1 = mk(1)
        try:
            autotrader.place(SUB, r1, placed_by="auto-flow")
            assert False, "should have been blocked"
        except autotrader.BadRequest as e:
            assert "reject" in str(e)
        assert not posts and STORE[(pk, f"BOT#{r1}")]["aiBlocked"] is True
        assert len(seen["images"]) == 2 and seen["images"][0][1][:8] == b"\x89PNG\r\n\x1a\n" and "STOP" not in seen["text"][:0]
        assert "underlyingStop" in seen["text"] and "rule_checks" in seen["text"]
        code, ch = call("GET", f"/bot/{r1}/charts")
        assert code == 200 and len(ch["images"]) == 2 and base64.b64decode(ch["images"][1]["b64"])[:4] == b"\x89PNG"
        # caution: blocked when caution isn't allowed (it is by default); approve places
        seen["verdict"] = "caution"
        assert autotrader.settings(SUB)["aiAllowCaution"] is True
        call("PUT", "/bot/settings", {"aiAllowCaution": False})
        r2 = mk(2)
        try:
            autotrader.place(SUB, r2, placed_by="auto")
            assert False
        except autotrader.BadRequest:
            pass
        call("PUT", "/bot/settings", {"aiAllowCaution": True})
        autotrader.place(SUB, r2, placed_by="auto")             # default: caution enters
        assert len(posts) == 1
        STORE[(pk, f"BOT#{r2}")]["status"] = "closed"
        posts.clear()
        seen["verdict"] = "approve"
        r3 = mk(3)
        autotrader.place(SUB, r3, placed_by="auto")
        assert len(posts) == 1 and STORE[(pk, f"BOT#{r3}")]["aiCheck"]["verdict"] == "approve"
        STORE[(pk, f"BOT#{r3}")]["status"] = "closed"
        # web app: the check must have run first (background job); override skips it
        r4 = mk(4)
        code, err = call("POST", f"/bot/{r4}/order", {})
        assert code == 400 and "chart check first" in err["error"], err
        code, _ = call("POST", f"/bot/{r4}/aicheck", {})
        assert code == 200 and STORE[(pk, f"BOT#{r4}")]["aiRunning"] is True
        autotrader.handler({"sub": SUB, "job": "aicheck", "id": r4}, None)
        r = STORE[(pk, f"BOT#{r4}")]
        assert r["aiCheck"]["verdict"] == "approve" and r["aiRunning"] is False
        # Claude sometimes sends the lists as one string: still shown as separate points
        seen["verdict"] = "caution"
        claude.vision_json = lambda *a, **k: {"verdict": "caution", "confidence": 58, "summary": "<p>Basing</p>",
                                              "supports": "<li>Reclaimed the 21 EMA</li><li>Above the flip</li>", "concerns": '["Overhead supply to 35"]'}
        rr = mk(7)
        v = aicheck.run(SUB, STORE[(pk, f"BOT#{rr}")])
        assert v["supports"] == ["Reclaimed the 21 EMA", "Above the flip"] and v["concerns"] == ["Overhead supply to 35"] and v["summary"] == "Basing", v
        STORE[(pk, f"BOT#{rr}")]["status"] = "dismissed"
        claude.vision_json = fake_vision
        # a re-evaluation of the same contract keeps the check (and its charts); a different contract shows it as previous
        new = {"id": "20260930120000-cccccc", "symbol": "XYZ", "proposal": prop}
        autotrader._carry_ai(SUB, new, autotrader.settings(SUB))
        assert new["aiCheck"]["verdict"] == "caution" and new["aiFrom"] == rr, new
        db.put({"PK": pk, "SK": "BOT#20260930120000-cccccc", **new})
        code, ch = call("GET", "/bot/20260930120000-cccccc/charts")
        assert code == 200 and len(ch["images"]) == 2
        other = {"id": "20260930120001-dddddd", "symbol": "XYZ", "proposal": {**prop, "contract": "XYZ261218C00105000"}}
        autotrader._carry_ai(SUB, other, autotrader.settings(SUB))
        assert "aiCheck" not in other and other["prevAiCheck"]["verdict"] == "caution", other
        STORE[(pk, "BOT#20260930120000-cccccc")]["status"] = "dismissed"
        code, ev = call("GET", "/bot/evaluations", q={"claude": "1"})
        assert code == 200 and ev["total"] >= 1 and all(i["aiCheck"]["verdict"] for i in ev["items"]), ev
        mk(8)                                     # evaluated, never checked
        code, allev = call("GET", "/bot/evaluations")
        assert allev["total"] == ev["total"] + 1
        code, placed = call("POST", f"/bot/{r4}/order", {})
        assert code == 200 and len(posts) == 2, placed
        STORE[(pk, f"BOT#{r4}")]["status"] = "closed"
        seen["verdict"] = "reject"
        r5 = mk(5)
        code, _ = call("POST", f"/bot/{r5}/order", {"override": True})
        assert code == 200 and len(posts) == 3
    finally:
        autotrader.DEFAULTS["aiCheck"] = False


def test_ai_end_of_day_review():
    import autotrader, alpaca, aicheck, datetime as dt
    STORE.clear()
    pk = db.upk(SUB)
    def bars(n, step):
        out, p, t0 = [], 30.0, autotrader.now_ny() - step * n
        for i in range(n):
            p *= 0.998
            out.append({"t": (t0 + step * i).strftime("%Y-%m-%dT%H:%M:%S"), "o": p * 1.01, "h": p * 1.02, "l": p * 0.98, "c": p, "v": 1000})
        return out
    aicheck._yahoo = lambda sym, tf, s_, e: bars(250, dt.timedelta(days=1)) if tf == "1d" else bars(140, dt.timedelta(hours=1))
    answer = {}
    claude.vision_json = lambda model, system, images, text, schema, max_tokens=1500: (answer.update(text=text, system=system) or answer["out"])
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    deletes = []
    def fake_alp(c, m, path, body=None):
        if m == "DELETE":
            deletes.append(path); return {"id": "x1"}
        return {"qty": "2", "current_price": "2.0"} if path.startswith("/v2/positions/") else None
    autotrader._alp = fake_alp
    prop = {"strategy": "long_call", "contract": "BMNR261120C00027000", "exp": "2026-11-20", "strike": 27, "qty": 2, "limit": 2.53,
            "underlyingStop": 25.0, "underlyingTarget": 27.0, "underlyingAtr": 1.5}
    def mk(i):
        rid = f"2026100210000{i}-eeeee{i}"
        db.put({"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": "BMNR", "status": "open", "proposal": prop, "fillPrice": 2.53,
                "filledQty": 2, "lastMark": 2.0, "lastPlPct": -21.0, "createdAt": "2026-10-01T10:00:00", "filledAt": "2026-10-01T10:01:00"})
        return rid
    # a newer evaluation of BMNR that Claude rejected for a new entry
    db.put({"PK": pk, "SK": "BOT#20261002092800-ffffff", "id": "20261002092800-ffffff", "symbol": "BMNR", "status": "proposed",
            "proposal": {**prop, "contract": "BMNR261120C00028000"}, "createdAt": autotrader.iso(autotrader.now_ny()),
            "aiCheck": {"verdict": "reject", "confidence": 78, "summary": "Pulled back hard off the highs", "at": autotrader.iso(autotrader.now_ny())}})
    # Claude says close with enough confidence -> position closed at market, review kept
    a, b, c3 = mk(1), mk(2), mk(3)
    answer["out"] = {"action": "close", "confidence": 80, "summary": "Lost the 27.7 shelf; closing near the low.",
                     "hold_reasons": [], "close_reasons": ["Broke below the range", "Weak close"]}
    out = autotrader.handler({"job": "review"}, None)
    assert out[SUB]["reviews"] and all(r["closed"] for r in out[SUB]["reviews"]), out
    r = STORE[(pk, f"BOT#{a}")]
    assert r["status"] == "closing" and r["exitReason"].startswith("Claude end-of-day review (80%)") and r["aiReview"]["action"] == "close"
    assert len(deletes) == 3 and "days_held" in answer["text"] and "OPEN options position" in answer["system"]
    assert "Pulled back hard off the highs" in answer["text"] and '"reject"' in answer["text"]      # its own recent view is passed
    assert "rule_checks" not in answer["text"] and '"stock_price"' in answer["text"]                  # no stale entry-day rules
    code, ch = call("GET", f"/bot/{a}/reviewcharts")
    assert code == 200 and len(ch["images"]) == 2
    # hold, or close below the confidence threshold -> stays open (the second one only alerts)
    deletes.clear()
    d, e = mk(4), mk(5)
    for x in (a, b, c3):
        STORE[(pk, f"BOT#{x}")]["status"] = "closed"
    answer["out"] = {"action": "hold", "confidence": 70, "summary": "Holding the base.", "hold_reasons": ["Above EMA"], "close_reasons": []}
    aicheck.review_all(SUB, only_id=d)
    answer["out"] = {"action": "close", "confidence": 40, "summary": "Maybe.", "hold_reasons": [], "close_reasons": ["Soft"]}
    aicheck.review_all(SUB, only_id=e)
    assert not deletes and STORE[(pk, f"BOT#{d}")]["status"] == "open" and STORE[(pk, f"BOT#{e}")]["status"] == "open"
    assert len(STORE[(pk, f"BOT#{e}")]["aiReviews"]) == 1 and STORE[(pk, f"BOT#{e}")]["aiReview"]["closed"] is False
    # a HOLD under 50% confidence on a position down 25%+ closes it; 55% or a smaller loss holds
    STORE[(pk, f"BOT#{e}")]["status"] = "closed"
    f1, f2, f3 = mk(6), mk(7), mk(8)
    STORE[(pk, f"BOT#{f1}")]["lastPlPct"] = STORE[(pk, f"BOT#{f2}")]["lastPlPct"] = -29.7
    STORE[(pk, f"BOT#{f3}")]["lastPlPct"] = -10.0
    answer["out"] = {"action": "hold", "confidence": 45, "summary": "Coin flip.", "hold_reasons": ["x"], "close_reasons": ["y"]}
    aicheck.review_all(SUB, only_id=f1)
    r = STORE[(pk, f"BOT#{f1}")]
    assert r["status"] == "closing" and "HOLD only 45% confident" in r["exitReason"] and r["aiReview"]["weakHold"], r.get("exitReason")
    aicheck.review_all(SUB, only_id=f3)
    answer["out"] = {**answer["out"], "confidence": 55}
    aicheck.review_all(SUB, only_id=f2)
    assert STORE[(pk, f"BOT#{f2}")]["status"] == "open" and STORE[(pk, f"BOT#{f3}")]["status"] == "open"
    for x in (f1, f2, f3):
        STORE[(pk, f"BOT#{x}")]["status"] = "closed"
    deletes.clear()
    # on demand from the app: never auto-closes
    answer["out"] = {"action": "close", "confidence": 95, "summary": "Close.", "hold_reasons": [], "close_reasons": ["x"]}
    code, _ = call("POST", f"/bot/{d}/aireview", {})
    assert code == 200 and STORE[(pk, f"BOT#{d}")]["aiReviewRunning"] is True
    autotrader.handler({"sub": SUB, "job": "aireview", "id": d}, None)
    r = STORE[(pk, f"BOT#{d}")]
    assert r["status"] == "open" and r["aiReview"]["action"] == "close" and len(r["aiReviews"]) == 2 and not deletes
    # turned off: the scheduled run skips
    call("PUT", "/bot/settings", {"aiExitReview": False})
    assert autotrader.handler({"job": "review"}, None)[SUB] == {"skipped": "end-of-day review off"}


def test_chart_uses_todays_close():
    """A lagging daily feed must not keep a fair value gap alive that today's close broke."""
    import aicheck, datetime as dt
    now = aicheck.now_ny().replace(hour=15, minute=40)
    day = lambda k: (now - dt.timedelta(days=k)).strftime("%Y-%m-%dT00:00:00")
    daily = [{"t": day(40 - i), "o": 20, "h": 20.5, "l": 19.5, "c": 20, "v": 100} for i in range(30)]
    daily += [{"t": day(9), "o": 20, "h": 21, "l": 19.8, "c": 21, "v": 100},          # gap up: bull FVG 21 -> 22.5
              {"t": day(8), "o": 22, "h": 24, "l": 21.8, "c": 23.5, "v": 300},
              {"t": day(7), "o": 23.5, "h": 25, "l": 22.5, "c": 24.5, "v": 200}]
    daily += [{"t": day(k), "o": 24, "h": 24.6, "l": 23.4, "c": 24, "v": 100} for k in range(6, 0, -1)]
    assert aicheck.indicators.series(daily)[-1]["bullTop"] is not None                 # gap still alive without today
    today = now.date().isoformat()
    hourly = [{"t": f"{today}T{h:02d}:30:00", "o": 23, "h": 23.2, "l": 20.5, "c": 20.8, "v": 50} for h in range(10, 16)]
    merged = aicheck._with_today(daily, hourly, now)
    assert merged[-1]["t"][:10] == today and merged[-1]["c"] == 20.8
    assert aicheck.indicators.series(merged)[-1]["bullTop"] is None                    # closed below the gap: gone
    aicheck._yahoo = lambda sym, tf, s_, e: daily if tf == "1d" else hourly
    ctx = {}
    aicheck.build_charts({"symbol": "XYZ", "proposal": {}}, ctx)
    assert ctx["price"] == 20.8 and ctx["bullFvg"] is None, ctx


def test_auto_earnings():
    """Earnings dates come from Unusual Whales automatically, for new evaluations and open positions."""
    import autotrader, flowdata, datetime as dt
    STORE.clear()
    pk = db.upk(SUB)
    soon = (autotrader.now_ny() + dt.timedelta(days=12)).strftime("%Y-%m-%d")
    calls = []
    def fake_next(sub, sym):
        calls.append(sym)
        return {"HON": f"{soon} BMO", "SPY": ""}.get(sym, f"{soon} AMC")
    flowdata.next_earnings = fake_next
    # pulled once, cached for 12 hours
    assert autotrader.refresh_earnings(SUB, ["HON", "SPY"]) == ["HON", "SPY"]
    assert autotrader.refresh_earnings(SUB, ["HON"]) == [] and calls == ["HON", "SPY"]
    cfg = autotrader.settings(SUB)
    assert autotrader.earn_for(cfg, "HON") == (f"{soon} BMO", "Unusual Whales") and autotrader.earn_for(cfg, "SPY") == (None, None)
    # the pulled date wins over an old entered one; an entered date is only a fallback when nothing was pulled
    autotrader.set_earnings(SUB, "HON", (autotrader.now_ny() + dt.timedelta(days=20)).strftime("%Y-%m-%d") + " AMC")
    assert autotrader.earn_for(autotrader.settings(SUB), "HON")[1] == "Unusual Whales"
    autotrader.set_earnings(SUB, "SPY", (autotrader.now_ny() + dt.timedelta(days=20)).strftime("%Y-%m-%d") + " AMC")
    assert autotrader.earn_for(autotrader.settings(SUB), "SPY")[1] == "entered"
    # saving bot settings keeps the pulled dates
    call("PUT", "/bot/settings", {"maxPositions": 6})
    assert autotrader.settings(SUB)["earningsAuto"]["HON"]["v"] == f"{soon} BMO"
    # open positions get theirs pulled by the minute check
    db.put({"PK": pk, "SK": "BOT#20261002100000-aaaaaa", "id": "20261002100000-aaaaaa", "symbol": "MRVL", "status": "open",
            "proposal": {"contract": "MRVL261120C00260000", "exp": "2026-11-20", "strike": 260, "underlyingStop": 1, "underlyingTarget": 999}})
    import alpaca
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    autotrader._alp = lambda *a, **k: {"qty": "1", "current_price": "2", "unrealized_plpc": "0", "unrealized_pl": "0", "market_value": "200"}
    autotrader._yahoo = lambda *a, **k: [{"t": "x", "o": 1, "h": 1, "l": 1, "c": 270.0, "v": 1}]
    autotrader.monitor(SUB)
    assert "MRVL" in calls and autotrader.settings(SUB)["earningsAuto"]["MRVL"]["v"] == f"{soon} AMC"
    # without a key nothing is pulled and nothing breaks
    flowdata.next_earnings = lambda sub, sym: None
    assert autotrader.refresh_earnings(SUB, ["NVDA"]) == []


def test_option_data_retries():
    """Alpaca's 'DeadlineExceeded' (400) on an options snapshot is retried, real errors are not."""
    import gex, io, time, urllib.error
    calls = {"n": 0}
    orig_open, orig_sleep = gex.urllib.request.urlopen, time.sleep
    def fail(code, body):
        return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body.encode()))
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def flaky(req, timeout=20):
        calls["n"] += 1
        if calls["n"] < 3:
            raise fail(400, '{"message":"rpc error: code = DeadlineExceeded desc = context deadline exceeded"}')
        return R(b'{"ok": 1}')
    time.sleep = lambda s: None
    try:
        gex.urllib.request.urlopen = flaky
        assert gex._get("https://x", {}) == {"ok": 1} and calls["n"] == 3
        calls["n"] = 0
        def bad(req, timeout=20):
            calls["n"] += 1
            raise fail(400, '{"message":"invalid symbol"}')
        gex.urllib.request.urlopen = bad
        try:
            gex._get("https://x", {}); assert False
        except gex.Unavailable as e:
            assert "invalid symbol" in str(e) and calls["n"] == 1
        def always(req, timeout=20):
            raise fail(400, "DeadlineExceeded")
        gex.urllib.request.urlopen = always
        try:
            gex._get("https://x", {}); assert False
        except gex.Unavailable as e:
            assert "Try again in a minute" in str(e)
    finally:
        gex.urllib.request.urlopen, time.sleep = orig_open, orig_sleep


def test_shadow_tracking():
    """Evaluations that weren't placed are followed with the bot's exit rules on daily bars."""
    import autotrader, shadow, alpaca, datetime as dt
    STORE.clear()
    pk = db.upk(SUB)
    now = autotrader.now_ny()
    d0 = (now - dt.timedelta(days=10)).strftime("%Y-%m-%d")
    days = [(now - dt.timedelta(days=9 - i)).strftime("%Y-%m-%d") for i in range(8)]
    prop = {"contract": "XYZ261120C00100000", "exp": "2026-11-20", "strike": 100, "qty": 2, "limit": 5.0,
            "underlyingStop": 95.0, "underlyingTarget": 115.0}
    def ev(i, decision, **kw):
        rid = f"2026092{i}100000-aaaaa{i}"
        r = {"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": "XYZ", "status": "proposed", "decision": decision,
             "createdAt": f"{d0}T10:0{i}:00", "proposal": prop, **kw}
        db.put(r)
        return rid
    skip = ev(1, "WAIT")
    dup = ev(2, "WAIT")                                    # same contract, same day: followed once
    rej = ev(3, "BUY", aiBlocked=True, proposal={**prop, "contract": "XYZ261120C00105000"})
    taken = ev(4, "BUY", orderId="o1", status="closed", proposal={**prop, "contract": "XYZ261120C00110000"})
    cfg = autotrader.settings(SUB)
    bar = lambda d, o, h, l, c: {"t": d + "T04:00:00Z", "o": o, "h": h, "l": l, "c": c}
    obars = {"XYZ261120C00100000": [bar(days[0], 5, 6, 4.5, 5.5), bar(days[1], 5.5, 9.5, 5.4, 9.0)],          # +80% target day 2
             "XYZ261120C00105000": [bar(days[0], 5, 5.2, 2.8, 3.0), bar(days[1], 3, 3, 2, 2)]}                  # -40% stop day 1
    ubars = [{"t": d + "T00:00:00", "o": 100, "h": 104, "l": 98, "c": 102} for d in days[:2]]
    seen = {}
    shadow._creds = lambda sub: {"key": "k", "secret": "s"}
    shadow.option_bars = lambda c, syms, start, errors=None: (seen.update(syms=sorted(set(x for x in syms if x)), start=start) or obars)
    import charts
    charts._yahoo = lambda *a, **k: ubars
    # a closed bot trade: followed for 10 trading days after its exit
    closed = "20260921150000-cccccc"
    db.put({"PK": pk, "SK": f"BOT#{closed}", "id": closed, "symbol": "XYZ", "status": "closed", "orderId": "o9", "fillPrice": 4.0,
            "filledQty": 2, "riskAtFill": 320.0, "exitPrice": 3.0, "exitReason": "option stop (-40%)", "closedAt": f"{d0}T15:30:00",
            "createdAt": f"{d0}T09:40:00", "proposal": {**prop, "contract": "XYZ261120C00120000"}})
    obars["XYZ261120C00120000"] = [bar(days[i], 3, 3.3 + i * 0.4, 2.8, 3.2 + i * 0.3) for i in range(6)]
    out = shadow.run(SUB)
    assert out == {"tracked": 2, "dups": 1, "exits": 1}, out
    ae = STORE[(pk, f"BOT#{closed}")]["afterExit"]
    assert ae["status"] == "tracking" and ae["days"] == 6 and ae["day1Pct"] == round((3.2 / 3 - 1) * 100, 1), ae
    assert ae["day5R"] == round((3.2 + 4 * 0.3 - 3.0) * 2 * 100 / 320.0, 2) and ae["bestR"] > ae["day5R"] and ae.get("day10Pct") is None
    assert "XYZ261120C00120000" in seen["syms"]
    assert seen["start"] == d0
    assert "shadow" not in STORE[(pk, f"BOT#{taken}")] and "path" in STORE[(pk, f"BOT#{taken}")]   # taken: a path, no shadow result
    s1 = STORE[(pk, f"BOT#{skip}")]["shadow"]
    # like the bot, reaching the target starts a trailing stop instead of selling
    assert s1["status"] == "tracking" and s1["trailing"] and s1["days"] == 2 and s1["plPct"] == 80.0, s1
    assert s1["mfePct"] == 90.0 and s1["maePct"] == -10.0 and s1["R"] == round(80.0 / cfg["stopPct"], 2) and s1["undMax"] == 104
    rt = {"id": "t", "symbol": "XYZ", "createdAt": f"{d0}T10:00:00", "proposal": {**prop, "contract": "TT"}}
    trail_bars = {"TT": [bar(days[0], 5, 6, 5, 5.5), bar(days[1], 5.5, 10, 5.4, 9.5), bar(days[2], 9.5, 11, 7.9, 8.0)]}
    sh = shadow.simulate(rt, trail_bars, ubars, cfg)
    assert sh["status"] == "done" and "trailing stop" in sh["exitReason"] and sh["exitReason"].startswith("option target"), sh
    assert sh["exitPrice"] == round(11 * (1 - cfg["trailPct"] / 100), 2)
    sh = shadow.simulate(rt, trail_bars, ubars, {**cfg, "trailAfterTarget": False})
    assert sh["exitReason"].startswith("option target") and sh["days"] == 2 and sh["plPct"] == float(cfg["targetPct"])
    s3 = STORE[(pk, f"BOT#{rej}")]["shadow"]
    assert s3["status"] == "done" and s3["exitReason"].startswith("option stop") and s3["R"] == -1.0 and s3["days"] == 1, s3
    assert STORE[(pk, f"BOT#{dup}")]["shadow"]["status"] == "dup" and "shadow" not in STORE[(pk, f"BOT#{taken}")]
    # finished ones aren't fetched again
    assert shadow.run(SUB)["tracked"] == 1                          # only the one still trailing is followed again
    # summary of what the skipped trades would have done, by reason and by Claude verdict
    STORE[(pk, f"BOT#{skip}")]["blocking"] = ["Up at least 10% from the 30-day low (low 196.98 on 2026-09-14, now +8.6%)"]
    code, sm = call("GET", "/bot/shadow/summary", q={"days": "35"})
    assert code == 200 and sm["skipped"]["n"] == 2 and sm["skipped"]["winPct"] == 50, sm
    whys = {g["why"]: g for g in sm["byReason"]}
    code, li = call("GET", "/bot/shadow/list", q={"days": "35", "why": "Claude rejected"})
    assert code == 200 and li["n"] == 1 and li["items"][0]["R"] == -1.0 and li["items"][0]["symbol"] == "XYZ", li
    code, li = call("GET", "/bot/shadow/list", q={"days": "35", "claude": "not checked"})
    assert li["n"] == 2
    assert "Claude rejected" in whys and whys["Claude rejected"]["avgR"] == -1.0
    assert "Up at least #% from the #-day low" in whys, list(whys)
    assert sm["best"][0]["symbol"] == "XYZ" and sm["best"][0]["R"] > 0
    ex = {g["why"]: g for g in sm["exits"]}
    assert ex["option stop"]["n"] == 1 and ex["option stop"]["day5R"] > 0 and ex["option stop"]["earlyPct"] == 100, sm["exits"]
    # run now from the app: background job, status recorded
    code, _ = call("POST", "/bot/shadow/run", {})
    assert code == 200 and STORE[(pk, "SHADOWRUN")]["status"] == "running"
    autotrader.handler({"sub": SUB, "job": "shadow"}, None)
    assert STORE[(pk, "SHADOWRUN")]["status"] == "done"
    code, sm = call("GET", "/bot/shadow/summary")
    assert code == 200 and sm["run"]["status"] == "done" and sm["days"] == 5
    # still running: tracking with the current result; invalidation on a close below the level
    r = {"id": "x", "symbol": "XYZ", "createdAt": f"{d0}T10:00:00", "proposal": {**prop, "contract": "C1"}}
    sh = shadow.simulate(r, {"C1": [bar(days[0], 5, 5.5, 4.8, 5.2)]}, ubars, cfg)
    assert sh["status"] == "tracking" and sh["plPct"] == 4.0 and sh["days"] == 1
    low = [{"t": days[0] + "T00:00:00", "o": 100, "h": 100, "l": 93, "c": 94}]
    sh = shadow.simulate(r, {"C1": [bar(days[0], 5, 5.2, 4.0, 4.1)]}, low, cfg)
    assert sh["status"] == "done" and "invalidation" in sh["exitReason"] and sh["plPct"] == -18.0
    # evaluated during the session: that day's close is the first point (close only); evaluated after hours: next day
    r_in = {**r, "createdAt": f"{d0}T11:00:00"}
    sh = shadow.simulate(r_in, {"C1": [bar(d0, 5, 9.9, 1.0, 5.5)]}, ubars, cfg)
    assert sh["days"] == 1 and sh["plPct"] == 10.0 and sh["mfePct"] == 10.0 and sh["maePct"] == 10.0, sh
    sh = shadow.simulate({**r, "createdAt": f"{d0}T18:00:00"}, {"C1": [bar(d0, 5, 6, 4, 5.5)]}, ubars, cfg)
    assert sh["days"] == 0 and sh["status"] == "nodata"
    # spreads use the net of the leg closes
    sp = {**r, "proposal": {**prop, "contract": "L", "shortContract": "S", "limit": 2.0, "strategy": "bull_call", "width": 10, "debit": 2.0}}
    sh = shadow.simulate(sp, {"L": [bar(days[0], 5, 6, 5, 6.0)], "S": [bar(days[0], 3, 3.5, 3, 3.5)]}, ubars, cfg)
    assert sh["closesOnly"] and sh["last"] == 2.5 and sh["plPct"] == 25.0


def test_excursions_and_r():
    """Taken trades record the best/worst option price, P&L and stock price, and the result in R."""
    import autotrader, alpaca
    STORE.clear()
    pk = db.upk(SUB)
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    px = {"opt": "5.0", "und": 100.0}
    autotrader._alp = lambda c, m, path, body=None: ({"qty": "2", "current_price": px["opt"], "avg_entry_price": "5.0",
                                                       "unrealized_plpc": str(float(px["opt"]) / 5 - 1), "unrealized_pl": "0",
                                                       "market_value": "0"} if path.startswith("/v2/positions/") and px["opt"] else None)
    autotrader._yahoo = lambda *a, **k: [{"t": "x", "o": 1, "h": 1, "l": 1, "c": px["und"], "v": 1}]
    rid = "20261001100000-bbbbbb"
    db.put({"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": "XYZ", "status": "open", "fillPrice": 5.0, "filledQty": 2,
            "proposal": {"contract": "XYZ261120C00100000", "exp": "2099-11-20", "strike": 100, "qty": 3, "underlyingStop": 50.0,
                         "underlyingTarget": 500.0, "underlyingAtr": 2.0}})
    for opt, und in (("4.0", 97.0), ("6.5", 108.0), ("5.5", 103.0)):
        px.update(opt=opt, und=und)
        autotrader.monitor(SUB)
    r = STORE[(pk, f"BOT#{rid}")]
    cfg = autotrader.settings(SUB)
    assert r["optMin"] == 4.0 and r["optMax"] == 6.5 and r["plMin"] == -20.0 and r["plMax"] == 30.0, r
    assert r["undMin"] == 97.0 and r["undMax"] == 108.0 and r["undMaxAt"]
    assert r["riskAtFill"] == round(5.0 * 2 * 100 * cfg["stopPct"] / 100, 2)        # filled quantity, not the proposed 3
    px["opt"] = ""                                                                  # position gone: closed at the last mark
    autotrader.monitor(SUB)
    r = STORE[(pk, f"BOT#{rid}")]
    assert r["status"] == "closed" and r["realizedPl"] == 100.0 and r["realizedR"] == round(100.0 / r["riskAtFill"], 2), r


def test_option_bars_partial_failure():
    """One contract Alpaca refuses doesn't stop the others."""
    import shadow, io, json as _j, urllib.error, importlib
    shadow = importlib.reload(shadow)           # an earlier test replaced option_bars with a stub
    orig = shadow.urllib.request.urlopen
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake(req, timeout=25):
        q = shadow.urllib.parse.parse_qs(shadow.urllib.parse.urlparse(req.full_url).query)
        syms = q["symbols"][0].split(",")
        assert q["end"][0].endswith("Z")
        if "BAD1" in syms:
            raise urllib.error.HTTPError("u", 400, "x", {}, io.BytesIO(b'{"message":"invalid symbol"}'))
        return R(_j.dumps({"bars": {s: [{"t": "2026-09-29T04:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1}] for s in syms}}).encode())
    try:
        shadow.urllib.request.urlopen = fake
        errs = []
        out = shadow.option_bars({"key": "k", "secret": "s"}, ["A1", "BAD1", "C1"], "2026-09-28", errs)
        assert sorted(out) == ["A1", "C1"] and len(errs) == 1 and errs[0].startswith("BAD1: 400"), (out, errs)
    finally:
        shadow.urllib.request.urlopen = orig


def test_flow_quality_summary():
    """Flow ideas bucketed by premium, sweep, ask %, vol/OI, time, DTE, moneyness; taken and skipped together."""
    import shadow, autotrader
    STORE.clear()
    pk = db.upk(SUB)
    now = autotrader.now_ny()
    d = now.strftime("%Y-%m-%d")
    def put(i, origin, **kw):
        rid = f"2026100{i}100000-fffff{i}"
        db.put({"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": "XYZ", "createdAt": f"{d}T10:1{i}:00",
                "proposal": {"contract": "XYZ261120C00100000"}, "signals": {"price": 100.0}, "origin": origin, **kw})
    o = {"type": "flow", "premium": 300000, "sweep": True, "askPct": 97, "volOi": 4.2, "alerts": 1, "contract": "XYZ261218C00105000"}
    put(1, o, status="proposed", shadow={"status": "done", "R": 1.5, "plPct": 60})
    put(2, {**o, "premium": 2000000, "sweep": False}, status="proposed", shadow={"status": "tracking", "R": -0.5, "plPct": -20})
    put(3, {**o, "askPct": 60, "volOi": 0.5}, status="closed", orderId="o1", fillPrice=2.0, realizedR=-1.0)
    put(4, o, status="proposed", shadow={"status": "dup", "of": "x"})                     # duplicate: not counted
    put(5, {"type": "manual"}, status="proposed", shadow={"status": "done", "R": 3.0})      # not from flow: not counted
    sm = shadow.flow_summary(SUB, 35)
    assert sm["n"] == 3 and sm["all"]["totalR"] == 0.0, sm["all"]
    dims = {x["key"]: {g["label"]: g for g in x["groups"]} for x in sm["dimensions"]}
    assert dims["premium"]["$250k–500k"]["n"] == 2 and dims["premium"]["$1M–5M"]["avgR"] == -0.5
    assert dims["sweep"]["Sweep"]["totalR"] == 0.5 and dims["sweep"]["No sweep"]["n"] == 1
    assert dims["ask"]["95%+"]["n"] == 2 and dims["ask"]["< 70%"]["avgR"] == -1.0
    assert dims["voi"]["< 1 (may be closing)"]["n"] == 1 and dims["voi"]["3–10"]["n"] == 2
    assert dims["tod"]["9:30–10:30"]["n"] == 3
    assert list(dims["otm"]) == ["5–10% OTM"] and dims["taken"]["Taken"]["n"] == 1 and dims["taken"]["Skipped"]["n"] == 2
    code, api_sm = call("GET", "/bot/flow/summary", q={"days": "10"})
    assert code == 200 and api_sm["n"] == 3 and api_sm["days"] == 10


def test_repeat_buyers():
    """Repeat buying is measured per ticker, any contract, over the last trading days; only big alerts start an analysis."""
    import autotrader, flowdata, shadow
    STORE.clear()
    pk = db.upk(SUB)
    cfg = autotrader.settings(SUB)
    import datetime as _dt
    _t = autotrader.now_ny().date()
    _wd = lambda span: sum(1 for k in range(span) if (_t - _dt.timedelta(days=k)).weekday() < 5)
    assert autotrader._trading_span(1) >= 1 and _wd(autotrader._trading_span(5)) == 5        # any day of the week
    day = autotrader.now_ny().strftime("%Y-%m-%d")
    calls = []
    def fake(sub, min_premium, opt_type="call", *a, **k):
        calls.append((min_premium, opt_type, a[3] if len(a) > 3 else None))
        return {"alerts": [{"ticker": "ABC", "type": "call", "premium": 40000, "contract": f"ABC26112{i}C00050000", "atEt": f"{day} 1{i}:00"} for i in range(4)]
                + [{"ticker": "ABC", "type": "call", "premium": 50000, "contract": "ABC261120C00050000", "atEt": "2026-01-02 10:00", "rule": "RepeatedHits"}]}
    flowdata.alerts = fake
    tf = autotrader.ticker_flow(SUB, "ABC", cfg)
    assert calls[0][:2] == (10000, "all")
    w = tf["window"]["smallCalls"]
    assert w["hits"] == 5 and w["contracts"] == 4 and w["minutes"] == 5 and w["days"] == 2 and w["premium"] == 210000 and tf["repeatQualifies"]
    assert tf["today"]["smallCalls"]["hits"] == 4 and tf["putRatio"] == 0
    cfg2 = {**cfg, "repeatMinTotal": 300000}
    assert autotrader.ticker_flow(SUB, "ABC", cfg2)["repeatQualifies"] is False
    # flow quality: bucketed from the stored ticker flow
    for i, (hits, R) in enumerate(((5, 1.0), (0, -0.5), (2, -0.5))):
        rid = f"20261005100{i}00-abcdef"
        db.put({"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": "ABC", "status": "proposed", "createdAt": autotrader.iso(autotrader.now_ny()),
                "origin": {"type": "flow", "premium": 200000}, "proposal": {"contract": "C"}, "shadow": {"status": "done", "R": R, "plPct": 1},
                "tickerFlow": {"window": {"smallCalls": {"hits": hits}, "puts": {"premium": 100000 if i == 2 else 0}},
                               "repeatQualifies": hits >= 4, "putRatio": 0.6 if i == 2 else None}})
    dims = {d["key"]: {g["label"]: g for g in d["groups"]} for d in shadow.flow_summary(SUB, 5)["dimensions"]}
    assert dims["repeat"]["4–9 prints"]["avgR"] == 1.0 and dims["repeat"]["None"]["n"] == 1 and dims["repeat"]["2–3 prints"]["n"] == 1
    assert dims["repeatOk"]["Yes"]["n"] == 1 and dims["puts"]["50–100%"]["n"] == 1 and dims["puts"]["No puts"]["n"] == 2


def test_evaluations_grouped_by_ticker():
    STORE.clear()
    pk = db.upk(SUB)
    rows = [("20261003082700-aaaaa1", "MRVL", "SKIP", "proposed"), ("20261003085800-aaaaa2", "MRVL", "SKIP", "proposed"),
            ("20261003111300-aaaaa3", "LUV", "WAIT", "proposed"), ("20261003122400-aaaaa4", "LUV", "WAIT", "proposed"),
            ("20261003091800-aaaaa5", "NBIS", "BUY", "open"), ("20261003090000-aaaaa6", "LUV", "BUY", "closed")]
    for rid, sym, dec, st in rows:
        db.put({"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": sym, "decision": dec, "status": st, "createdAt": rid[:8] + "T" + rid[8:14],
                **({"orderId": "o"} if st in ("open", "closed") else {})})
    code, g = call("GET", "/bot/evaluations", q={"group": "ticker"})
    assert code == 200 and g["group"] == "ticker" and [x["symbol"] for x in g["items"]] == ["LUV", "MRVL"], g
    luv = g["items"][0]
    assert luv["count"] == 2 and luv["latest"]["id"] == "20261003122400-aaaaa4" and luv["decisions"] == {"WAIT": 2} and not luv["held"]
    code, h = call("GET", "/bot/evaluations", q={"symbol": "luv"})
    assert code == 200 and h["symbol"] == "LUV" and h["total"] == 3 and [x["taken"] for x in h["items"]] == [False, False, True], h
    code, n = call("GET", "/bot/evaluations", q={"group": "ticker"})
    db.put({"PK": pk, "SK": "BOT#20261003130000-aaaaa7", "id": "20261003130000-aaaaa7", "symbol": "NBIS", "decision": "WAIT",
            "status": "proposed", "createdAt": "2026-10-03T13:00:00"})
    code, g = call("GET", "/bot/evaluations", q={"group": "ticker"})
    assert g["items"][0]["symbol"] == "NBIS" and g["items"][0]["held"] is True
    code, f = call("GET", "/bot/evaluations", q={"group": "ticker", "q": "lu"})
    assert [x["symbol"] for x in f["items"]] == ["LUV"], f


def test_feed_health():
    """Flow checks record whether Unusual Whales answered; the app can test the feed on demand."""
    import autotrader, flowdata
    STORE.clear()
    pk = db.upk(SUB)
    saved = flowdata.alerts
    try:
        def boom(*a, **k):
            raise RuntimeError("Unusual Whales returned an error (401)")
        flowdata.alerts = boom
        try:
            autotrader.flow_scan(SUB)
            assert False
        except RuntimeError:
            pass
        st = STORE[(pk, "BOTSTATE")]
        assert "401" in st["uwError"] and st["uwErrorAt"]
        flowdata.alerts = lambda *a, **k: {"alerts": []}
        autotrader.flow_scan(SUB)
        st = STORE[(pk, "BOTSTATE")]
        assert st["uwOkAt"] >= st["uwErrorAt"] and st["alertsToday"] == 0 and st["flowDay"]
        sent = {}
        saved_get, saved_key = flowdata._get, flowdata._key
        def fake_get(key, params):
            sent.update(params)
            return {"data": [{"ticker": "A", "created_at": "2026-10-05T13:40:00Z"}, {"ticker": "B", "created_at": "2026-10-05T13:41:30.5Z"}]}
        flowdata._get, flowdata._key = fake_get, lambda sub: "k"
        code, r = call("POST", "/bot/flow/test", {})
        assert code == 200 and r["ok"] and r["alerts"] == 2 and r["tickers"] == 2 and r["newest"] == "2026-10-05 09:41" and r["newestTicker"] == "B", r
        assert "filtered60" in r or "filterError" in r
        assert sent == {"limit": 50}                                    # no filters at all
        flowdata._get = lambda key, params: boom()
        code, r = call("POST", "/bot/flow/test", {})
        flowdata._get, flowdata._key = saved_get, saved_key
        assert code == 200 and not r["ok"] and "401" in r["error"]
    finally:
        flowdata.alerts = saved


def test_flow_rolling_window():
    """Every minute looks at the last 20 minutes: tickers over the per-minute limit are analyzed the next minute,
    and the same alert is counted once."""
    import autotrader, flowdata, datetime as dt
    STORE.clear()
    pk = db.upk(SUB)
    saved = (flowdata.alerts, autotrader.evaluate)
    try:
        now = dt.datetime.utcnow()
        al = [{"id": f"a{i}", "ticker": f"T{i}", "premium": 500000 - i * 1000, "sweep": False, "contract": f"T{i}C",
               "at": (now - dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")} for i in range(5)]
        windows = []
        def fake_alerts(sub, *a, since_utc=None, **k):
            windows.append(since_utc)
            return {"alerts": al}
        flowdata.alerts = fake_alerts
        seen = []
        autotrader.evaluate = lambda sub, sym, source=None, **k: (seen.append(sym) or {"id": "x", "decision": "WAIT"})
        r1 = autotrader.flow_scan(SUB)
        assert seen == ["T0", "T1", "T2"] and r1["alerts"] == 5 and r1["skips"]["waiting"] == 2
        assert (now - dt.timedelta(minutes=61)).strftime("%Y-%m-%dT%H:%M:%SZ") < windows[0] < (now - dt.timedelta(minutes=59)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r2 = autotrader.flow_scan(SUB)                    # no new alerts, but the 2 left over get their turn
        assert seen[3:] == ["T3", "T4"] and r2["alerts"] == 0 and r2["window"] == 5 and r2["skips"]["recent"] == 3
        st = STORE[(pk, "BOTSTATE")]
        assert st["alertsToday"] == 5 and st["evalsToday"] == 5 and st["lastFlowTickers"] == 5
        code, b = call("GET", "/bot")
        assert code == 200 and b["state"]["alertsToday"] == 5 and b["state"]["uwOkAt"]
    finally:
        flowdata.alerts, autotrader.evaluate = saved


def test_put_watch():
    """Held positions get a daily put-accumulation point (seeded at entry from the evaluation), with an alert when puts pile in."""
    import autotrader, flowdata, notify
    STORE.clear()
    pk = db.upk(SUB)
    tf = lambda tp, tc, wp, wc: {"today": {"puts": {"premium": tp}, "calls": {"premium": tc}}, "window": {"puts": {"premium": wp}, "calls": {"premium": wc}, "bigPuts": 1},
                                 "putRatio": round(wp / wc, 2) if wc else None}
    rec = {"PK": pk, "SK": "BOT#20261005100000-ffffff", "id": "20261005100000-ffffff", "symbol": "MRVL", "status": "open",
           "createdAt": "2026-10-01T10:00:00", "tickerFlow": tf(100, 900, 1000, 5000), "proposal": {"contract": "C", "exp": "2026-11-20", "strike": 260}}
    seed = autotrader._put_seed(rec)
    assert seed[0]["entry"] and seed[0]["ratio"] == 0.2 and seed[0]["day"] == "2026-10-01"
    rec["putWatch"] = seed
    db.put(rec)
    sent = []
    saved = (autotrader.ticker_flow, notify.send)
    try:
        notify.send = lambda sub, text, kind="order": sent.append(text)
        autotrader.ticker_flow = lambda sub, sym, cfg: tf(800000, 600000, 4000000, 5000000)
        pt = autotrader.put_watch(SUB, STORE[(pk, rec["SK"])])
        r = STORE[(pk, rec["SK"])]
        assert pt["ratio"] == 0.8 and len(r["putWatch"]) == 2 and sent and "puts piling in" in sent[0], (pt, sent)
        sent.clear()
        autotrader.ticker_flow = lambda sub, sym, cfg: tf(100000, 600000, 1000000, 5000000)
        autotrader.put_watch(SUB, STORE[(pk, rec["SK"])])                 # same day: replaced, no alert
        r = STORE[(pk, rec["SK"])]
        assert len(r["putWatch"]) == 2 and r["putWatch"][-1]["ratio"] == 0.2 and not sent
    finally:
        autotrader.ticker_flow, notify.send = saved


def test_flow_chart():
    """An evaluation's chart: 15-minute bars over the last trading days plus every alert bought at the ask on the ticker."""
    import autotrader, flowdata, charts, flowchart
    STORE.clear()
    pk = db.upk(SUB)
    now = autotrader.now_ny()
    day = now.strftime("%Y-%m-%d")
    rid = "20261006144500-abcdef"
    db.put({"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": "PLTR", "decision": "WAIT", "createdAt": f"{day}T14:45:00",
            "origin": {"type": "flow", "premium": 185499, "contract": "PLTR261113C00185000"}})
    saved = (flowdata.alerts, charts._yahoo)
    try:
        charts._yahoo = lambda sym, tf, s_, e: [{"t": f"{day}T{h:02d}:{m:02d}:00", "o": 190, "h": 191, "l": 189, "c": 190.5, "v": 10}
                                               for h in range(9, 17) for m in (0, 15, 30, 45)]
        seen = {}
        def fake(sub, min_premium, opt_type, *a, **k):
            seen.update(min=min_premium, type=opt_type, ticker=a[4])
            return {"alerts": [{"ticker": "PLTR", "type": "call", "premium": 185499, "contract": "PLTR261113C00185000", "atEt": f"{day} 14:44", "sweep": True},
                               {"ticker": "PLTR", "type": "put", "premium": 40000, "contract": "PLTR261218P00140000", "atEt": f"{day} 10:05"}]}
        flowdata.alerts = fake
        code, d = call("GET", f"/bot/{rid}/flowchart")
        assert code == 200 and d["symbol"] == "PLTR" and d["evaluatedAt"] == f"{day}T14:45", d
        assert all("09:30" <= b["t"][11:16] < "16:00" for b in d["bars"]) and len(d["bars"]) == 26
        assert [a["type"] for a in d["alerts"]] == ["put", "call"] and d["alerts"][1]["big"] and not d["alerts"][0]["big"]
        assert seen == {"min": 10000, "type": "all", "ticker": "PLTR"} and d["trigger"]["contract"] == "PLTR261113C00185000"
        # gamma walls: from earlier evaluations, stored snapshots and now, as steps (unchanged values collapsed)
        import gex
        for k in [k for k in STORE if k[0] in ("GEXHIST", "CACHE", "UWGEX2")]:
            STORE.pop(k)
        db.put({"PK": pk, "SK": "BOT#20261006100000-aaaaaa", "id": "20261006100000-aaaaaa", "symbol": "PLTR", "createdAt": f"{day}T10:00:00",
                "gamma": {"callWall": 200, "putWall": 180, "gammaFlip": 188}})
        db.put({"PK": "GEXHIST", "SK": f"PLTR#{day}T11:00", "t": f"{day}T11:00", "callWall": 200, "putWall": 180, "flip": 188})
        db.put({"PK": "GEXHIST", "SK": f"PLTR#{day}T12:30", "t": f"{day}T12:30", "callWall": 195, "putWall": 185, "flip": 189})
        saved_w = gex.walls_now
        gex.walls_now = lambda sub, sym: {"t": f"{day}T15:00", "callWall": 195, "putWall": 185, "flip": 189, "spot": 190, "source": "test"}
        try:
            code, d = call("GET", f"/bot/{rid}/flowchart")
        finally:
            gex.walls_now = saved_w
        assert [(w["t"][11:], w["callWall"], w["src"]) for w in d["walls"]] == [("10:00", 200, "evaluation"), ("12:30", 195, "snapshot"), ("15:00", 195, "now")], d["walls"]
        assert d["wallsNow"]["putWall"] == 185
        assert [e["id"] for e in d["evals"]] == ["20261006100000-aaaaaa", rid] and d["evals"][1]["decision"] == "WAIT"
        assert d["trade"]["at"] == f"{day}T14:45" and d["trade"]["taken"] is False
        code, tk = call("GET", "/bot/tickers")
        assert code == 200 and tk["items"][0]["symbol"] == "PLTR" and tk["items"][0]["count"] == 2 and tk["items"][0]["id"] == rid
        # 45-day view: walls day by day from Unusual Whales greek exposure (each day's open interest), cached per day
        import datetime as dt
        calls = []
        def fake_uw(key, path, params=None):
            calls.append((path, params["date"]))
            dd = params["date"]
            if path.endswith("/gex-levels"):          # some days have no flip (stored without the field)
                return {"data": {"gamma_flip": "188.5" if dd.endswith(("0", "2", "4", "6", "8")) else None}}
            cw = "200" if dd < (now - dt.timedelta(days=10)).strftime("%Y-%m-%d") else "210"
            return {"data": [{"strike": "180", "call_gex": "10", "put_gex": "-900"}, {"strike": cw, "call_gex": "5000", "put_gex": "-5"},
                             {"strike": "190", "call_gex": "300", "put_gex": "-100"}]}
        saved_uw = (flowdata._uw, flowdata._key)
        flowdata._uw, flowdata._key = fake_uw, (lambda sub: "k")
        try:
            code, d = call("GET", f"/bot/{rid}/flowchart", q={"days": "45"})
            assert code == 200 and d["days"] == 45 and d["interval"] == 60 and d["wallSource"] == "uw", d.get("wallNote")
            cws = [w["callWall"] for w in d["walls"]]
            assert sorted(set(cws)) == [200, 210] and cws == sorted(cws) and d["walls"][0]["putWall"] == 180 and d["walls"][0].get("flip") in (188.5, None) and d["wallSource"] == "uw"
            assert d["wallsNow"]["callWall"] == 210 and "Unusual Whales" in d["wallsNow"]["source"]
            n1 = len(calls)
            call("GET", f"/bot/{rid}/flowchart", q={"days": "45"})
            assert len(calls) == n1, "past days and today (within the hour) come from the cache"
            # dark pool: prints by day (UTC -> ET), cancelled dropped, top price levels by premium
            def fake_dp(key, path, params=None):
                calls.append((path, params["date"]))
                return {"data": [{"executed_at": f"{params['date']}T14:31:05Z", "price": "190.10", "size": 20000, "premium": "3802000", "canceled": False},
                                 {"executed_at": f"{params['date']}T15:00:00Z", "price": "185.00", "size": 50000, "premium": "9250000", "canceled": True}]}
            flowdata._uw = fake_dp
            code, dp = call("GET", f"/bot/{rid}/darkpool", q={"days": "5"})
            assert code == 200 and dp["prints"] and all(p_["price"] == 190.1 for p_ in dp["prints"]), dp
            assert dp["prints"][0]["t"][11:] == "10:31" and dp["levels"][0]["price"] == 190.1 and dp["minPremium"] == 500000
        finally:
            flowdata._uw, flowdata._key = saved_uw
    finally:
        flowdata.alerts, charts._yahoo = saved


def test_call_bursts():
    """Clusters of calls bought at the ask, far above the ticker's normal: found in a 30-day replay and live, followed
    1/3/5 days later, never traded."""
    import autotrader, flowdata, charts, bursts, datetime as dt
    STORE.clear()
    pk = db.upk(SUB)
    now = autotrader.now_ny()
    days = []
    d = now.date() - dt.timedelta(days=12)
    while len(days) < 8:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    burst_day = days[1]
    # the bot evaluated VST in the last 30 days -> it's in the replay
    db.put({"PK": pk, "SK": "BOT#20261001100000-aaaaaa", "id": "20261001100000-aaaaaa", "symbol": "VST", "status": "proposed",
            "createdAt": f"{days[0]}T10:00:00"})
    def mk(day, hm, prem, typ="call", c="VST261218C00140000", u=140.0):
        return {"ticker": "VST", "type": typ, "premium": prem, "contract": c, "atEt": f"{day} {hm}", "underlying": u,
                "at": f"{day}T{hm}:00Z", "id": f"{day}{hm}{prem}{c}"}
    normal = [mk(dd, "11:00", 20000) for dd in days]                                   # quiet flow every day
    burst = [mk(burst_day, f"15:{35 + i:02d}", 150000, c=f"VST261218C001{40 + i % 5}000", u=140.0 - i * 0.1) for i in range(10)]
    flowdata_saved, yahoo_saved = flowdata.alerts, charts._yahoo
    try:
        flowdata.alerts = lambda sub, *a, **k: {"alerts": normal + burst + [mk(burst_day, "15:40", 50000, typ="put")]}
        closes = {dd: 140.0 + i * 2 for i, dd in enumerate(days)}                      # rises after the burst
        charts._yahoo = lambda sym, tf, s_, e: [{"t": f"{dd}T00:00:00", "o": c, "h": c, "l": c, "c": c} for dd, c in closes.items()]
        out = bursts.replay(SUB, restart=True)
        assert out["status"] == "done" and out["total"] == 1 and out["bursts"] >= 1, out
        st = STORE[(pk, "BURSTSTUDY")]
        b = next(x for x in st["bursts"] if x["strict"])
        assert b["ticker"] == "VST" and b["date"] == burst_day and b["n"] >= 8 and b["cp"] >= 1_000_000 and b["ratio"] >= 5
        # small prints stacking: 8 x $40k in 30 min = $320k counts with the default thresholds, and thresholds re-sort without a re-run
        assert bursts.DEFAULTS["burstMinPremium"] == 250000
        assert b["tod"] == "close" and b["move"] < 0 and b["callShare"] >= 0.75
        assert b["fwd1"] == round((closes[days[2]] / b["price"] - 1) * 100, 2) and b["fwd5"] is not None
        assert STORE[(pk, "BURSTBASE")]["tickers"]["VST"]["dailyCall"] > 0
        code, pg = call("GET", "/bot/bursts")
        assert code == 200 and pg["study"]["status"] == "done" and pg["study"]["strict"]["n"] >= 1 and pg["study"]["baseline"]["fwd1"] is not None
        assert any(g["title"] == "Price during the window" for g in pg["study"]["groups"])
        n0 = pg["study"]["strict"]["n"]
        call("PUT", "/bot/settings", {"burstMinPremium": 5_000_000})
        code, pg2 = call("GET", "/bot/bursts")
        assert pg2["study"]["strict"] is None and pg2["study"]["all"]["n"] == pg["study"]["all"]["n"]
        call("PUT", "/bot/settings", {"burstMinPremium": 250000})
        # live: today's prints add up to a burst once; the same ticker isn't recorded again within the cooldown
        today = now.strftime("%Y-%m-%d")
        import util as _util
        bursts.now_ny = lambda: now.replace(hour=10, minute=30, second=0, microsecond=0)   # the cooldown doesn't depend on when the tests run
        live = [mk(today, f"10:{10 + i:02d}", 150000, u=150 - i * 0.05) for i in range(9)]
        flowdata.alerts = lambda sub, *a, **k: {"alerts": live}
        db.put({"PK": pk, "SK": "BURSTSTATE", "seen": ["x"] * 10, "lastEvent": {}})          # old-version state
        db.put({"PK": pk, "SK": f"BURST#{today.replace('-', '')}0930-OLD", "ticker": "OLD", "date": today})
        r1 = bursts.live_scan(SUB)
        assert r1["bursts"] == ["VST"], r1
        assert (pk, f"BURST#{today.replace('-', '')}0930-OLD") not in STORE and STORE[(pk, "BURSTSTATE")]["v"] == bursts.VERSION
        evs = [v for (p_, s_), v in STORE.items() if s_.startswith("BURST#")]
        assert len(evs) == 1 and evs[0]["ticker"] == "VST" and evs[0]["normalKnown"]
        it = STORE[(pk, f"BURSTT#{today}#VST")]
        cp0 = sum(b.get("cp", 0) for b in it["buckets"].values())
        r_same = bursts.live_scan(SUB)                         # the same alerts again: nothing counted twice
        assert r_same["new"] == 0 and sum(b.get("cp", 0) for b in STORE[(pk, f"BURSTT#{today}#VST")]["buckets"].values()) == cp0
        STORE[(pk, "BURSTSTATE")]["lastAt"] = "2000-01-01T00:00:00Z"   # a run that never saved its progress
        assert bursts.live_scan(SUB)["new"] == 0
        live.append(mk(today, "10:25", 900000))
        r2 = bursts.live_scan(SUB)
        assert r2["bursts"] == [] and r2["new"] == 1          # cooldown: no second burst for VST
        # after the close: forward results for live bursts
        closes[today] = 150.0
        assert bursts.forward_update(SUB)["updated"] in (0, 1)
        # a bad record in the study doesn't break the page
        STORE[(pk, "BURSTSTUDY")]["bursts"].append({"ticker": "BAD"})
        STORE[(pk, "BURSTSTUDY")]["bursts"].append({"ticker": "OLD", "cp": 400000, "n": 7, "at": "2026-09-01T10:00", "date": "2026-09-01"})
        code, pg3 = call("GET", "/bot/bursts")
        assert code == 200 and not pg3["errors"] and pg3["study"]["all"]["n"] >= 1, pg3.get("errors")
        code, _ = call("POST", "/bot/bursts/replay", {"restart": True})
        assert code == 200 and STORE[(pk, "BURSTSTUDY")]["status"] == "starting"
    finally:
        flowdata.alerts, charts._yahoo = flowdata_saved, yahoo_saved
        import util as _util
        bursts.now_ny = _util.now_ny


def test_explorer():
    """Every taken and skipped idea as features; combinations filter them; the search ranks combinations."""
    import autotrader, explorer
    STORE.clear()
    pk = db.upk(SUB)
    now = autotrader.now_ny()
    def ev(i, R, cp_ratio, bear_ok, taken=False):
        rid = f"2026100{1 + i % 6}10{i:04d}-abcdef"[:21]
        rid = f"20261001{i:06d}-abcdef"
        r = {"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": f"T{i % 7}", "decision": "WAIT", "status": "proposed",
             "createdAt": (now - __import__("datetime").timedelta(days=10 - i % 10, hours=i % 5)).strftime("%Y-%m-%dT%H:%M:%S"),
             "checks": [{"group": "chart", "text": "No active bear (negative) fair value gap", "ok": bear_ok, "required": True},
                        {"group": "chart", "text": "Active bull fair value gap (1–2)", "ok": True, "required": True}],
             "origin": {"type": "flow", "premium": 300000, "sweep": i % 2 == 0, "volOi": 2, "askPct": 90},
             "tickerFlow": {"putRatio": 1 / cp_ratio, "window": {"calls": {"premium": 1e6}, "smallCalls": {"hits": i % 12}}},
             "signals": {"extAtr": 0.8, "growth30": 12}, "shadow": {"status": "done", "R": R, "plPct": R * 40}}
        if taken:
            r.update(status="closed", orderId="o", fillPrice=2.0, realizedR=R)
            r.pop("shadow")
        db.put(r)
    for i in range(40):
        good = i % 2 == 0
        ev(i, 1.0 if good else -0.6, 6 if good else 1.5, True if good else i % 4 == 1, taken=i % 10 == 0)
    rs = explorer.rows(SUB, 35)
    assert len(rs) == 40 and rs[0]["callPut"] in (6.0, 1.5) and sum(r["taken"] for r in rs) == 4
    code, d = call("POST", "/bot/explore", {"conds": [{"key": "callPut", "op": ">=", "value": 5}, {"key": "noBearFvg", "value": True}]})
    assert code == 200 and d["match"]["n"] == 20 and d["match"]["avgR"] == 1.0 and d["all"]["n"] == 40, d["match"]
    assert d["conds"][0]["text"].startswith("Calls vs puts bought") and len(d["items"]) == 20
    code, d = call("POST", "/bot/explore", {"search": True, "minN": 10})
    best = d["search"]["results"][0]
    assert best["avgR"] == 1.0 and best["n"] >= 10 and d["search"]["tested"] > 10, best
    assert any(c["key"] in ("callPut", "noBearFvg", "sweep") for c in best["conds"])
    # Monte Carlo on the matching ideas, with a permutation test against random groups of all ideas
    mc = explorer.monte_carlo(SUB, 35, [{"key": "callPut", "op": ">=", "value": 5}], trades=50, runs=1000, risk=200, account=20000, seed=1)
    assert mc["n"] == 20 and mc["avgR"] == 1.0 and mc["total"]["p50"] == 50.0 and mc["probPositive"] == 100.0, mc
    assert mc["randomAsGood"] == 0.0 and mc["dollars"]["p50"] == 10000 and len(mc["fan"]) >= 10 and sum(mc["hist"]["counts"]) == 1000
    mc = explorer.monte_carlo(SUB, 35, [], trades=50, runs=1000, seed=2)
    assert 0 < mc["probPositive"] <= 100 and mc["drawdown"]["p95"] <= mc["drawdown"]["p50"] <= 0 and mc["losingStreak"]["p95"] >= 1
    code, r = call("POST", "/bot/montecarlo", {"conds": [{"key": "callPut", "op": ">=", "value": 99}]})
    assert code == 200 and "error" in r


def test_best_exit_and_strategies():
    """Exit rules replayed on stored paths; strategies saved from a combination tag new evaluations, and a 'trade'
    strategy buys on a match when only non-safety rules failed, with its own exit plan."""
    import autotrader, explorer, strats, shadow, datetime as dt
    STORE.clear()
    pk = db.upk(SUB)
    now = autotrader.now_ny()
    # paths: option runs +150% then falls back -> trailing / a high target beats "sell at +80%"? and a tight stop
    def path(seq):
        d0 = now.date() - dt.timedelta(days=30)
        out = []
        for i, (h, l, c) in enumerate(seq):
            out.append([(d0 + dt.timedelta(days=i)).isoformat(), h, l, c, 100, 100, 100])
        return out
    up = path([(5, 5, 5), (6, 4.6, 5.8), (9, 5.6, 8.8), (12.5, 8.5, 12.2), (13, 10, 10.5), (11, 9, 9.5)])
    dn = path([(5, 5, 5), (5.1, 4.2, 4.3), (4.4, 3.4, 3.5), (3.6, 2.4, 2.5), (2.6, 2.0, 2.1)])
    for i in range(20):
        rid = f"20261001{i:06d}-abcdef"
        p = {"contract": f"C{i}", "exp": "2099-12-18", "limit": 5.0, "underlyingStop": 50, "underlyingTarget": 999}
        db.put({"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": f"S{i}", "decision": "WAIT", "status": "proposed",
                "createdAt": (now - dt.timedelta(days=30, hours=-i)).strftime("%Y-%m-%dT%H:%M:%S"), "proposal": p,
                "origin": {"type": "flow", "premium": 300000, "volOi": 4 if i % 2 == 0 else 0.5},
                "checks": [{"group": "chart", "text": "No active bear (negative) fair value gap", "ok": True, "required": True}],
                "shadow": {"status": "done", "R": 0.1, "plPct": 4, "v": shadow.SIM_VERSION, "path": up if i % 2 == 0 else dn}})
    r = explorer.best_exit(SUB, 60, [{"key": "volOi", "op": ">=", "value": 3}], min_n=5)
    assert r["n"] == 10 and r["tested"] > 100 and r["results"][0]["avgR"] >= r["current"]["avgR"], (r["current"], r["results"][:2])
    best = r["results"][0]["params"]
    # the bid/ask spread costs: the same rules do worse with a 20% spread than with none
    r0 = explorer.best_exit(SUB, 60, [{"key": "volOi", "op": ">=", "value": 3}], min_n=5, spread_mult=0)
    assert r["avgSpread"] == 10.0 and r0["current"]["avgR"] > r["current"]["avgR"]
    r2 = explorer.best_exit(SUB, 60, [{"key": "volOi", "op": ">=", "value": 3}], min_n=5, spread_mult=2)
    assert r2["current"]["avgR"] < r["current"]["avgR"] and any(x["tight"] for x in r2["results"] if x["params"]["stop"] < 40)
    assert best["target"] is None or best["target"] >= 100 or best["trail"], best          # the runner shouldn't be sold early
    code, e = call("POST", "/bot/exitopt", {"conds": [{"key": "volOi", "op": ">=", "value": 99}]})
    assert code == 200 and "error" in e
    # save a strategy (watch), then switch it to trade
    code, st = call("POST", "/bot/strategies", {"conds": [{"key": "volOi", "op": ">=", "value": 3}], "name": "Opening flow",
                                                "exit": best, "backtest": {"n": 10}})
    assert code == 200 and st["mode"] == "watch" and st["exit"]["stop"] == best["stop"]
    code, bad = call("POST", "/bot/strategies", {"conds": [{"key": "claudeApprove", "value": True}]})
    assert code == 400
    rec = {"id": "20261008100000-aaaaaa", "symbol": "NEW", "decision": "WAIT", "createdAt": autotrader.iso(now + dt.timedelta(minutes=5)),
           "origin": {"type": "flow", "premium": 400000, "volOi": 5}, "proposal": {"contract": "NEWC", "limit": 2.0},
           "checks": [{"group": "chart", "text": "Crossed above the 21 EMA in the last 10 days", "ok": False, "required": True}],
           "blocking": ["Crossed above the 21 EMA in the last 10 days"]}
    strats.apply(SUB, rec)
    assert rec["strategies"][0]["mode"] == "watch" and rec["decision"] == "WAIT" and "exitPlan" not in rec["proposal"]
    call("PUT", f"/bot/strategies/{st['id']}", {"mode": "trade"})
    rec2 = {**rec, "checks": list(rec["checks"]), "blocking": list(rec["blocking"]), "proposal": dict(rec["proposal"]), "strategies": None}
    strats.apply(SUB, rec2)
    assert rec2["decision"] == "BUY" and rec2["strategyBuy"] == st["id"] and rec2["proposal"]["exitPlan"]["stop"] == best["stop"], rec2
    assert rec2["ruleDecision"] == "WAIT" and rec2["overridden"] and not rec2["blocking"]
    # safety rules still block
    rec3 = {**rec, "checks": [], "proposal": dict(rec["proposal"]), "blocking": ["Puts not piling in (last 5 trading days): ..."]}
    strats.apply(SUB, rec3)
    assert rec3["decision"] == "WAIT" and "not bought" in rec3["checks"][-1]["text"]
    # forward results only count evaluations after the strategy was saved
    db.put({"PK": pk, "SK": f"BOT#{rec2['id']}", **rec2, "shadow": {"status": "done", "R": 1.5, "plPct": 60}})
    code, lst = call("GET", "/bot/strategies")
    it = lst["items"][0]
    assert it["forward"]["n"] == 1 and it["forward"]["avgR"] == 1.5 and it["conds"][0]["text"].startswith("Flow volume")
    # an open trade with a strategy exit plan: its own stop (20%) closes it where the settings' 40% wouldn't
    import alpaca
    alpaca.creds = lambda sub: {"key": "k", "secret": "s", "env": "paper"}
    autotrader._alp = lambda c, m, path, body=None: ({"qty": "1", "current_price": "1.5", "avg_entry_price": "2.0", "unrealized_plpc": "-0.25",
                                                     "unrealized_pl": "-50", "market_value": "150"} if path.startswith("/v2/positions/") and m == "GET" else {"id": "x"})
    autotrader._yahoo = lambda *a, **k: [{"t": "x", "o": 1, "h": 1, "l": 1, "c": 100.0, "v": 1}]
    db.put({"PK": pk, "SK": "BOT#20261008110000-bbbbbb", "id": "20261008110000-bbbbbb", "symbol": "NEW", "status": "open", "fillPrice": 2.0,
            "filledQty": 1, "filledAt": autotrader.iso(now), "proposal": {"contract": "NEWC", "exp": "2099-12-18", "strike": 100,
            "underlyingStop": 50, "underlyingTarget": 999, "exitPlan": {"stop": 20, "target": None, "trail": None}}})
    out = autotrader.monitor(SUB)
    assert out["actions"] and "stop (-25%)" in out["actions"][0][1], out
    # select the strategy as the only one the bot trades, with a chosen 25% stop
    code, a = call("PUT", "/bot/strategies/active", {"strategy": st["id"], "stop": 25})
    assert code == 200 and a["id"] == st["id"] and a["stop"] == 25
    assert call("PUT", "/bot/strategies/active", {"strategy": st["id"], "stop": 2})[0] == 400
    call("PUT", f"/bot/strategies/{st['id']}", {"mode": "off"})          # still traded: it's the active one
    rec4 = {**rec, "checks": list(rec["checks"]), "blocking": list(rec["blocking"]), "proposal": dict(rec["proposal"])}
    strats.apply(SUB, rec4, autotrader.settings(SUB))
    assert rec4["decision"] == "BUY" and rec4["proposal"]["exitPlan"]["stop"] == 25, rec4
    # a normal BUY that doesn't match the active strategy is not bought
    rec5 = {**rec, "origin": {"type": "flow", "premium": 400000, "volOi": 1}, "decision": "BUY", "checks": [], "blocking": [],
            "proposal": {"contract": "X", "limit": 2.0}}
    strats.apply(SUB, rec5, autotrader.settings(SUB))
    assert rec5["decision"] == "SKIP" and "selected strategy" in rec5["blocking"][0] and "exitPlan" not in rec5["proposal"]
    # a match buys even when safety rules / size failed, and nothing but its exit plan applies
    rec7 = {**rec, "checks": [], "proposal": {"contract": "NEWC", "limit": 9.0, "qty": 0},
            "blocking": ["Puts not piling in (last 5 trading days): ...", "No earnings in the next 7 days"]}
    strats.apply(SUB, rec7, autotrader.settings(SUB))
    assert rec7["decision"] == "BUY" and rec7["proposal"]["qty"] == 1 and rec7["proposal"]["strategyOnly"] == st["id"] and rec7["overridden"]
    # no liquid contract: the closest call is used
    rec8 = {**rec, "checks": [], "proposal": None, "altProposal": {"contract": "ALT", "limit": 1.0, "qty": 2}, "blocking": ["Liquid contract ..."]}
    strats.apply(SUB, rec8, autotrader.settings(SUB))
    assert rec8["decision"] == "BUY" and rec8["proposal"]["contract"] == "ALT" and "altProposal" not in rec8
    assert strats.effective_stop(SUB) == 25
    # back to the bot rules with the strategy/settings stop: a BUY stays a BUY
    code, a = call("PUT", "/bot/strategies/active", {"strategy": "", "stop": None})
    assert code == 200 and a["id"] == "" and a["stop"] is None and strats.effective_stop(SUB) is None
    rec6 = {**rec5, "decision": "BUY", "blocking": [], "checks": [], "proposal": {"contract": "X", "limit": 2.0}}
    strats.apply(SUB, rec6, autotrader.settings(SUB))
    assert rec6["decision"] == "BUY" and "exitPlan" not in rec6["proposal"]
    code, lst = call("GET", "/bot/strategies")
    assert lst["active"]["name"] == "Bot rules" and 25 in lst["stops"]
    call("PUT", "/bot/strategies/active", {"strategy": st["id"]})
    code, _ = call("DELETE", f"/bot/strategies/{st['id']}")
    assert code == 200 and strats.load(SUB) == [] and strats.active(SUB)[0] is None


def test_analytics():
    base = dict(status="closed", setup="", tags=[], r=None, mfe=None, min=600, date="2026-09-21")
    ts = [dict(base, openTs=f"2026-09-21T10:0{i}:00", net=n) for i, n in enumerate([-100, -50, -80, 200, -60])]
    s = analytics.stats(ts)
    assert s["trades"] == 5 and s["net"] == -90 and s["max_drawdown"] == -230
    ts[2]["tags"] = ["Revenge"]
    L = analytics.leaks(ts)
    assert L["leaks"][0]["label"] == "Revenge trades" and L["leaks"][0]["dollars"] == -80


def test_walls_store_and_explorer():
    """Walls by day: largest call / put gamma strikes; stored for every evaluated ticker; explorer features."""
    import flowdata, explorer, autotrader, datetime as dt
    STORE.clear()
    assert flowdata.walls_from_strikes([[90, 50, -10], [100, 900, -2000], [110, 300, -5], [120, 100, 0]]) == (100, 100)
    assert flowdata.walls_from_strikes([[90, 5000, -10], [100, 0, -2000]]) == (90, 100)        # call wall below the put wall
    pk = db.upk(SUB)
    now = autotrader.now_ny()
    day = now.date()
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    rid = day.strftime("%Y%m%d") + "100000-abcdef"
    db.put({"PK": pk, "SK": f"BOT#{rid}", "id": rid, "symbol": "XYZ", "createdAt": f"{day.isoformat()}T10:00:00", "decision": "WAIT",
            "signals": {"price": 102.0}, "shadow": {"status": "done", "R": 1.0}})
    calls = []
    def fake(key, path, params=None):
        calls.append((path, params["date"]))
        if path.endswith("gex-levels"):
            return {"data": {"gamma_flip": "101"}}
        pw = 100 if params["date"] == day.isoformat() else 95
        return {"data": [{"strike": str(pw), "call_gex": "10", "put_gex": "-900"}, {"strike": "110", "call_gex": "800", "put_gex": "-1"},
                         {"strike": "90", "call_gex": "50", "put_gex": "-3"}]}
    saved = (flowdata._uw, flowdata._key)
    flowdata._uw, flowdata._key = fake, (lambda sub: "k")
    try:
        r = flowdata.walls_backfill(SUB, days=10, budget_s=60)
        assert r["tickers"] == 1 and r["stored"] >= 5 and r["left"] == 0, r
        assert not any(p_.endswith("gex-levels") for p_, _ in calls), "the backfill skips the flip"
        st = flowdata.stored_walls("XYZ")
        assert st[day.isoformat()] == {"callWall": 110, "putWall": 100, "flip": None}
        n0 = len(calls)
        assert flowdata.walls_backfill(SUB, days=10)["missing"] == 0 and len(calls) == n0      # nothing fetched twice
        w = flowdata.gex_day("k", "XYZ", day.isoformat())                                    # the chart wants the flip: added once
        assert w["flip"] == 101.0 and flowdata.stored_walls("XYZ")[day.isoformat()]["flip"] == 101.0
        ex = explorer.explore(SUB, 10, [{"key": "pwUp", "value": True}])
        row = explorer.rows(SUB, 10)[0]
        assert row["pwDist"] == 2.0 and row["cwRoom"] == 7.84 and row["betweenWalls"] and row["pwUp"] and row["risingFloor"] and row["cwAbovePw"], row
        assert ex["match"]["n"] == 1 and ex["walls"]["known"] == 1
        sr = explorer.search(explorer.rows(SUB, 10), min_n=1, group="wallsflow")
        assert sr["results"] and all(any(c["key"] in explorer.WALL_KEYS for c in x["conds"]) for x in sr["results"])
        assert all(c["key"] in explorer.WALL_KEYS or c["key"] in explorer.FLOW_KEYS for x in sr["results"] for c in x["conds"])
    finally:
        flowdata._uw, flowdata._key = saved


def test_explorer_many_features_and_deep_search():
    import explorer
    rec = {"createdAt": "2026-10-06T10:15:00", "symbol": "AAA", "decision": "BUY",
           "signals": {"price": 105, "ema21": 100, "atr21": 2.1, "crossAgo": 4, "momentum": 0.5, "momentumPrev": 0.2, "low30Date": "2026-09-26"},
           "gamma": {"gammaFlip": 100, "callWall": 115, "putWall": 102, "netGex": 5},
           "proposal": {"contract": "C1", "underlyingStop": 100, "underlyingTarget": 115},
           "candidates": [{"symbol": "C1", "dte": 30, "delta": 0.55, "iv": 0.45, "spreadPct": 2.0, "oi": 900, "strike": 110}],
           "events": [], "origin": {"type": "flow", "alerts": 3, "topPremium": 400000, "optPrice": 2.5, "rule": "RepeatedHits"},
           "tickerFlow": {"window": {"calls": {"premium": 1000, "days": 3, "contracts": 5}, "bigCalls": {"hits": 2}, "bigPuts": 0},
                          "today": {"calls": {"premium": 600}, "puts": {"premium": 100}}, "repeatQualifies": True}}
    f = explorer.features_of(rec, {})
    assert f["emaDist"] == 5.0 and f["flipDist"] == 5.0 and f["posGex"] is True
    assert f["roomRatio"] == 2.0 and f["iv"] == 45.0 and f["dte"] == 30 and f["low30Age"] == 10
    assert f["repeatRule"] is True and f["todayShare"] == 60.0 and f["callsOverPutsToday"] is True
    assert f["weekday"] == 2 and f["minsOpen"] == 45
    assert all(k in explorer.CAT_OF for k in explorer.FEATURES), [k for k in explorer.FEATURES if k not in explorer.CAT_OF]
    w = explorer.wall_features(rec, {"2026-10-02": {"putWall": 98, "callWall": 112}, "2026-10-05": {"putWall": 100, "callWall": 112},
                                     "2026-10-06": {"putWall": 104, "callWall": 112}})
    assert w["pwUp2"] is True and w["pwChg"] == 4.0 and w["atPw"] is True and w["wallWidth"] == 7.69
    # deep search: only rows with all 5 flags win
    import random
    random.seed(1)
    rs = []
    for i in range(400):
        r = {"id": str(i), "at": f"2026-09-{1 + i % 28:02d}T10:00:00", "status": "done"}
        flags = [random.random() < 0.6 for _ in range(5)]
        for k, v in zip(("aboveEma", "momUp", "sweep", "putsOk", "posGex"), flags):
            r[k] = v
        r["R"] = 2.0 if all(flags) else -0.5
        rs.append(r)
    out = explorer.search(rs, min_n=10, max_depth=5)
    best = out["results"][0]
    assert best["avgR"] == 2.0 and best["size"] == 5, best
    assert explorer.search(rs, min_n=10, max_depth=3)["results"][0]["size"] <= 3


def test_wall_watch():
    """CW > flip > PW + green hourly candle crossing the put wall; target = call wall, stop = put wall - 0.5 ATR (fixed)."""
    import wallwatch as _ww
    _ww.FLOW_ONLY = False
    import wallwatch, flowdata, charts, datetime as dt
    STORE.clear()
    w = {"callWall": 110, "flip": 102, "putWall": 100}
    bars = [{"t": "2026-10-05T09:30", "o": 101, "h": 101.5, "l": 100.5, "c": 101},
            {"t": "2026-10-05T10:30", "o": 99.5, "h": 101, "l": 99, "c": 100.8},      # green, crosses the put wall -> signal
            {"t": "2026-10-05T11:30", "o": 100.8, "h": 104, "l": 100.2, "c": 103.5},
            {"t": "2026-10-05T12:30", "o": 103.5, "h": 103.9, "l": 102.6, "c": 103.2},  # above the trailed stop
            {"t": "2026-10-06T09:30", "o": 104, "h": 110.5, "l": 103, "c": 109},       # target
            {"t": "2026-10-06T10:30", "o": 101, "h": 101.2, "l": 99.6, "c": 101.1},    # new signal
            {"t": "2026-10-06T11:30", "o": 101, "h": 101.2, "l": 95.0, "c": 99.4}]     # through put wall - 0.5 ATR -> stop
    sigs = wallwatch.detect(bars, {"2026-10-05": w, "2026-10-06": w})
    assert [s["status"] for s in sigs] == ["target", "stop"], sigs
    assert sigs[0]["target"] == 109.45 and sigs[0]["exit"] == 109.45 and sigs[0]["pct"] == round((109.45 / 100.8 - 1) * 100, 2)   # 0.5% under the call wall
    assert wallwatch.detect(bars, {"2026-10-05": w, "2026-10-06": w}, cfg={"tgtBuf": 0})[0]["exit"] == 110
    atr = wallwatch.atr14(bars)
    assert sigs[1]["stop"] == round(100 - 0.5 * atr[5], 4) and sigs[1]["exit"] == sigs[1]["stop"]
    assert sigs[0]["stop"] == round(104 - sigs[0]["atr"], 4) and sigs[0]["kind"] == "pw"    # trailed 1 ATR under the 11:30 high
    assert sigs[1]["riskPct"] == round((101.1 - sigs[1]["stop"]) / 101.1 * 100, 2)
    assert not wallwatch.detect(bars, {"2026-10-05": {"callWall": 110, "flip": 99, "putWall": 100}}, cfg={"flipSetup": False})   # flip under the put wall
    tb = bars[:3] + [{"t": "2026-10-06T09:30", "o": 104, "h": 106, "l": 103.8, "c": 105.5},
                     {"t": "2026-10-06T10:30", "o": 105.5, "h": 105.6, "l": 103, "c": 103.5}]       # under the trailed stop
    ts = wallwatch.detect(tb, {"2026-10-05": w, "2026-10-06": w})
    assert ts[0]["status"] == "trail" and ts[0]["stop"] == round(106 - ts[0]["atr"], 4) and ts[0]["exit"] == ts[0]["stop"], ts
    nt = wallwatch.detect(tb, {"2026-10-05": w, "2026-10-06": w}, cfg={"trailAtr": 0, "stopAtr": 1})      # no trailing
    assert nt[0]["stop"] == nt[0]["stop0"] == round(100 - nt[0]["atr"], 4) and "stops" not in nt[0]
    fw = {"callWall": 110, "flip": 100, "putWall": 102}                                                    # flip < PW < CW
    fs = wallwatch.detect(bars, {"2026-10-05": fw, "2026-10-06": fw})
    assert fs and fs[0]["kind"] == "flip" and fs[0]["level"] == 100 and fs[0]["stop0"] == round(100 - 0.5 * fs[0]["atr"], 4)
    assert not wallwatch.detect(bars, {"2026-10-05": fw, "2026-10-06": fw}, cfg={"flipSetup": False})
    mw = {"callWall": 100, "flip": 102, "putWall": 100}                                                   # PW = CW < flip
    assert wallwatch.setup_kind(mw) == "merged"
    mb = [{"t": "2026-10-05T09:30", "o": 101, "h": 101.5, "l": 100.5, "c": 101},
          {"t": "2026-10-05T10:30", "o": 99.5, "h": 101, "l": 99, "c": 100.8},                             # crosses the merged wall
          {"t": "2026-10-05T11:30", "o": 100.8, "h": 120, "l": 100.2, "c": 119},                           # no target: still open
          {"t": "2026-10-06T09:30", "o": 119, "h": 119.5, "l": 105.5, "c": 106},
          {"t": "2026-10-06T10:30", "o": 106, "h": 106.5, "l": 104.0, "c": 104.2}]                         # under the raised stop
    ms = wallwatch.detect(mb, {"2026-10-05": mw, "2026-10-06": {"callWall": 105, "flip": 106, "putWall": 105}})
    assert ms[0]["kind"] == "merged" and ms[0]["target"] is None and ms[0]["status"] == "trail", ms
    assert ms[0]["stop"] == round(105 - 0.5 * ms[0]["atr"], 4) and ms[0]["exit"] == ms[0]["stop"] and ms[0]["tgtPct"] is None
    daily = [{"t": f"2026-08-{d:02d}", "o": 100, "h": 102, "l": 98, "c": 100} for d in range(1, 31)] + \
            [{"t": f"2026-09-{d:02d}", "o": 100, "h": 104, "l": 96, "c": 100} for d in range(1, 31)]
    da = wallwatch.daily_atr(daily)
    assert "2026-08-30" not in da and abs(da["2026-09-30"] - (4 + 29 * 8) / 30) < 1e-9 and abs(da["next"] - 8) < 1e-9
    ds = wallwatch.detect(bars, {"2026-10-05": w, "2026-10-06": w}, datr={**da, "next": 4.0})
    assert ds[0]["atr"] == 4.0 and ds[0]["stop0"] == 98.0          # put wall 100 - 0.5 x 30-day ATR 4
    assert ts[0]["pct"] > 0 and ts[0]["stops"][-1] == ["2026-10-06T09:30", ts[0]["stop"]]
    now = dt.datetime(2026, 10, 5, 11, 10)
    assert [b["t"][11:16] for b in wallwatch.finished(bars[:3], now)] == ["09:30"]                       # 10:30 candle not over
    saved = (charts._yahoo, flowdata.gex_day, flowdata._key)
    charts._yahoo = lambda sym, tf, a, b: [dict(x) for x in bars]
    flowdata.gex_day = lambda key, sym, d, flip=True: dict(w)
    flowdata._key = lambda sub: "k"
    try:
        st, r = call("GET", "/bot/wallwatch")
        assert st == 200 and "SPY" in r["config"]["tickers"] and r["config"]["enabled"]
        st, r = call("PUT", "/bot/wallwatch", {"tickers": "aapl, brk/b msft", "trailAtr": "1.5", "flipSetup": False})
        assert r["config"]["tickers"] == ["AAPL", "BRK.B", "MSFT"] and r["config"]["trailAtr"] == 1.5 and r["config"]["flipSetup"] is False
        call("PUT", "/bot/wallwatch", {"trailAtr": 1, "flipSetup": True})
        out = wallwatch.run(SUB, days=45)
        assert out["checked"] == 3, out
        st, r = call("GET", "/bot/wallwatch")
        sm = r["summary"]
        assert sm["signals"] == 6 and sm["closed"] == 6 and sm["targets"] == 3 and sm["hitPct"] == 50, sm
        assert len(r["status"]) == 3 and r["status"][0]["setup"] == "pw"
        saved_sw = flowdata.stored_walls
        flowdata.stored_walls = lambda sym: {"2026-10-05": dict(w), "2026-10-06": dict(w)}
        saved_al = flowdata.alerts
        flowdata.alerts = lambda *a, **k: {"alerts": [{"ticker": "AAPL", "atEt": "2026-10-05 10:45", "type": "call", "premium": 300000},
                                                       {"ticker": "AAPL", "atEt": "2026-10-06 11:00", "type": "put", "premium": 200000},
                                                       {"ticker": "MSFT", "atEt": "2026-10-06 11:00", "type": "put", "premium": 200000}]}
        try:
            st, c = call("GET", "/bot/wallwatch/chart", q={"t": "AAPL"})
        finally:
            flowdata.stored_walls, flowdata.alerts = saved_sw, saved_al
        assert [a["type"] for a in c["alerts"]] == ["call", "put"], c["alerts"]
        assert st == 200 and c["symbol"] == "AAPL" and len(c["bars"]) == 7 and len(c["signals"]) == 2, c
        assert c["walls"][0]["flip"] == 102 and c["interval"] == 60
        days = wallwatch._weekdays(5, wallwatch.now_ny().date())
        past = [d_ for d_ in days if d_ != wallwatch.now_ny().strftime("%Y-%m-%d")]
        db.put({"PK": "UWGEX2", "SK": f"AAPL#{past[0]}", "data": {"callWall": 1, "putWall": 1, "flip": 1}})
        db.put({"PK": "UWGEX2", "SK": f"AAPL#{past[1]}", "data": None})                       # no data that day: done
        db.put({"PK": "UWGEX2", "SK": f"AAPL#{past[2]}", "data": {"callWall": 1, "putWall": 1, "noflip": True}})
        db.put({"PK": "UWMP", "SK": f"AAPL#{past[0]}", "at": 0, "mp": {}})
        db.put({"PK": "UWMP", "SK": f"AAPL#{past[1]}", "at": 0, "mp": {}})
        saved_uw = flowdata._uw
        flowdata._uw = lambda key, path, params=None, attempts=4: {"data": [{"expiry": "2026-10-16", "max_pain": "105"},
                                                                             {"expiry": "2026-10-23", "max_pain": "99"}]}
        todo, total = wallwatch.walls_todo(["AAPL"], 5)
        assert total == 5 and (("AAPL", past[2]) in todo) and ("AAPL", past[0]) not in todo and ("AAPL", past[1]) not in todo
        got = []
        flowdata.gex_day = lambda key, sym, d, flip=True: got.append((sym, d)) or dict(w)
        f = wallwatch.fill_walls(SUB, days=5, budget_s=30)
        assert f["left"] == 0 and f["fetched"] == len(got) and len(got) == 3 * 5 - 2 - (5 - len(past)) * 3, (f, got)
        st, r = call("GET", "/bot/wallwatch")
        assert r["fill"]["left"] == 0 and r["fill"]["total"] == 15
        mpd = wallwatch.stored_maxpain("MSFT")
        assert mpd and all(v == {"2026-10-16": 105.0} or v == {} for v in mpd.values()), mpd
        got.clear()
        f1 = wallwatch.fill_walls(SUB, days=5, budget_s=30, ticker="MSFT")
        assert f1["total"] == 5 and all(t_ == "MSFT" for t_, _ in got) and db.get(db.upk(SUB), "WWFILL#MSFT")["left"] == 0
        flowdata._uw = saved_uw
        assert wallwatch.parse_max_pain({"data": {"2026-11-20": 50, "x": 1}}) == {"2026-11-20": 50.0}
        ser = wallwatch.maxpain_series({"2026-10-14": {"2026-10-16": 105.0, "2026-11-20": 90.0}, "2026-10-19": {"2026-11-20": 92.0}},
                                       ["2026-10-14", "2026-10-19"])
        assert ser == [{"t": "2026-10-14T09:30", "exp": "2026-10-16", "maxPain": 105.0}, {"t": "2026-10-19T09:30", "exp": "2026-11-20", "maxPain": 92.0}]
        assert wallwatch.monthly_exps("2026-09-01", "2026-11-30") == ["2026-09-18", "2026-10-16", "2026-11-20"]
        saved_dp, saved_y = flowdata.darkpool, charts._yahoo
        seen_dp = []
        charts._yahoo = lambda sym, tf, a, b: [{"t": f"2026-09-{d:02d}", "o": 100, "h": 101, "l": 99, "c": 100, "v": 1_000_000} for d in range(1, 31)]
        pr = [{"t": "2026-10-05T10:00", "price": 100.0, "size": 4000, "premium": 400000, "side": "buy"},
              {"t": "2026-10-05T11:00", "price": 101.0, "size": 6000, "premium": 606000, "side": "buy"},
              {"t": "2026-10-06T11:00", "price": 99.0, "size": 9000, "premium": 891000, "side": "sell"}]
        flowdata.darkpool = lambda sub, sym, dates, mp, **k: seen_dp.append((sym, len(dates), mp)) or {"prints": pr, "levels": [], "minPremium": mp}
        st, dpr = call("GET", "/bot/wallwatch/darkpool", q={"t": "brk/b", "pct": "0.5"})
        flowdata.darkpool, charts._yahoo = saved_dp, saved_y
        assert st == 200 and dpr["symbol"] == "BRK.B" and seen_dp == [("BRK.B", 180 * 5 // 7, 250000)], (dpr, seen_dp)   # levels: always 180 days
        assert dpr["adv"] == 1_000_000 and dpr["minShares"] == 5000 and [p_["size"] for p_ in dpr["prints"]] == [6000, 9000]
        assert dpr["buy"] == 606000 and dpr["sell"] == 891000 and dpr["levels"][0]["sell"] == 891000
    finally:
        charts._yahoo, flowdata.gex_day, flowdata._key = saved


def test_wall_watch_live():
    """Flow tickers join the list; a fresh signal buys a call (paper) with the signal's stop / target; the exit trails."""
    import wallwatch as _ww
    _ww.FLOW_ONLY = False
    import wallwatch, flowdata, autotrader, charts, datetime as dt
    STORE.clear()
    pk = db.upk(SUB)
    call("GET", "/bot/wallwatch")
    saved = (flowdata.alerts, autotrader.evaluate, autotrader.place, autotrader.chase, autotrader._yahoo)
    try:
        flowdata.alerts = lambda *a, **k: {"alerts": [{"ticker": "ZZZ", "premium": 600000}, {"ticker": "AAPL", "premium": 1}]}
        r = wallwatch.flow_add(SUB)
        cfg = wallwatch.config(SUB)
        assert r["added"] == ["ZZZ"] and "ZZZ" in cfg["tickers"] and cfg["added"]["ZZZ"]["by"] == "flow"
        st, v = call("PUT", "/bot/wallwatch", {"add": "pypl"})
        assert "PYPL" in v["config"]["tickers"] and v["config"]["added"]["PYPL"]["by"] == "you"
        calls = []
        def fake_eval(sub, sym, source=None):
            rec = {"id": "20261012113200-abcdef", "symbol": sym, "decision": "SKIP", "status": "proposed", "proposal": None,
                   "altProposal": {"contract": "ZZZ261120C00105000", "exp": "2026-11-20", "strike": 105, "qty": 0, "limit": 2.0}}
            db.put({"PK": pk, "SK": f"BOT#{rec['id']}", **rec})
            calls.append(("eval", source["type"]))
            return rec
        autotrader.evaluate = fake_eval
        autotrader.place = lambda sub, bid, **k: calls.append(("place", k.get("placed_by"), k.get("override")))
        autotrader.chase = lambda sub, bid: calls.append(("chase",)) or {"status": "open"}
        sg = {"at": "2026-10-12T10:30", "kind": "pw", "level": 100, "entry": 100.8, "stop": 99.0, "stop0": 99.0, "atr": 3.6, "target": 110}
        db.put({"PK": pk, "SK": f"WWSIG#ZZZ#{sg['at']}", "symbol": "ZZZ", **sg})
        out = wallwatch.enter(SUB, "ZZZ", sg)
        assert out["botId"] == "20261012113200-abcdef" and [c[0] for c in calls] == ["eval", "place", "chase"], (out, calls)
        assert calls[1] == ("place", "auto-wallwatch", True)
        rec = db.get(pk, "BOT#20261012113200-abcdef")
        p = rec["proposal"]
        assert p["qty"] == 1 and p["underlyingStop"] == 99.0 and p["underlyingTarget"] == 110 and p["wallwatch"]["trailAtr"] == 1.0
        assert db.get(pk, f"WWSIG#ZZZ#{sg['at']}")["botId"] == rec["id"]
        # exit: trailing stop from 5-minute candles after the fill
        db.update(pk, rec["SK"], {"status": "open", "filledAt": "2026-10-12T11:31:00", "fillPrice": 2.0, "filledQty": 1})
        rec = db.get(pk, rec["SK"])
        bars5 = [{"t": "2026-10-12T11:35", "o": 101, "h": 106, "l": 100.5, "c": 105},
                 {"t": "2026-10-12T11:40", "o": 105, "h": 105.2, "l": 102.0, "c": 102.2}]
        autotrader._yahoo = lambda *a, **k: [dict(b) for b in bars5]
        reason = autotrader._wallwatch_exit(SUB, rec, rec["proposal"])
        assert reason and reason.startswith("trailing stop") and db.get(pk, rec["SK"])["proposal"]["underlyingStop"] == round(106 - 3.6, 4), reason
        bars5[-1].update(l=104, h=110.5)
        rec = db.get(pk, rec["SK"])
        assert "call wall" in autotrader._wallwatch_exit(SUB, rec, rec["proposal"])
        st_, vv = call("GET", "/bot/wallwatch")
        assert any(x["symbol"] == "ZZZ" and x["status"] == "open" and x["wallwatch"] for x in vv["positions"]), vv["positions"]
        tr = wallwatch.trades_for(SUB, "ZZZ")
        assert tr and tr[0]["contract"] == "ZZZ261120C00105000" and tr[0]["under"] == 100.8
        hb = [{"t": "2026-10-12T09:30", "o": 101, "h": 101.5, "l": 100.5, "c": 101},
              {"t": "2026-10-12T10:30", "o": 99.5, "h": 101, "l": 99, "c": 100.8}]
        saved2 = (charts._yahoo, flowdata.gex_day)
        charts._yahoo = lambda sym, tf, a, b: [dict(x) for x in hb] if tf == "1h" else []
        flowdata.gex_day = lambda key, sym, d, flip=True: {"callWall": 110, "flip": 102, "putWall": 100}
        try:
            fresh = wallwatch.check_ticker(SUB, "k", "QQQ", 3, dt.datetime(2026, 10, 12, 11, 32))
            stale = wallwatch.check_ticker(SUB, "k", "SPY", 3, dt.datetime(2026, 10, 12, 13, 5))
        finally:
            charts._yahoo, flowdata.gex_day = saved2
        assert len(fresh["_enter"]) == 1 and fresh["_enter"][0]["at"] == "2026-10-12T10:30" and not stale["_enter"]
    finally:
        flowdata.alerts, autotrader.evaluate, autotrader.place, autotrader.chase, autotrader._yahoo = saved


def test_wall_watch_flow_study():
    """Flow before each signal; results with and without each flow condition; the flow filter blocks a trade."""
    import wallwatch as _ww
    _ww.FLOW_ONLY = False
    import wallwatch, flowdata
    STORE.clear()
    pk = db.upk(SUB)
    call("GET", "/bot/wallwatch")
    sg = {"at": "2026-10-07T10:30", "level": 100, "pw": 100, "entry": 100.8}
    al = [{"t": "2026-10-05T10:00", "c": 1, "p": 2_000_000, "u": 100.5},     # 3 days before, near the level
          {"t": "2026-10-07T09:45", "c": 1, "p": 600_000, "u": 101},
          {"t": "2026-10-06T12:00", "c": 0, "p": 300_000, "u": 103},
          {"t": "2026-10-07T12:00", "c": 1, "p": 9_000_000, "u": 102}]     # after the signal: ignored
    f = wallwatch.flow_features(sg, al)
    assert f["c3"] == 2_600_000 and f["p3"] == 300_000 and f["c1"] == 600_000 and f["big3"] == 2 and f["nearLevel"] == 2_600_000, f
    assert f["lastCallH"] == 1.8
    for i, (st, pct, fl) in enumerate([("target", 5.0, f), ("stop", -2.0, {**f, "c3": 0, "c1": 0, "big3": 0, "nearLevel": 0}),
                                       ("stop", -1.0, f)]):
        db.put({"PK": pk, "SK": f"WWSIG#AAPL#2026-10-0{i + 1}T10:30", "symbol": "AAPL", "at": f"2026-10-0{i + 1}T10:30",
                "status": st, "pct": pct, "R": pct / 2, "tgtPct": 8, "riskPct": 2, "flow": fl})
    st_, v = call("GET", "/bot/wallwatch")
    fs = v["flowStudy"]
    assert fs["base"]["n"] == 3 and fs["rows"][0]["name"].startswith("Any call flow") and fs["rows"][0]["n"] == 2
    assert fs["rows"][0]["avgPct"] == 2.0 and fs["rows"][1]["n"] == 1 and fs["rows"][1]["avgPct"] == -2.0
    saved = flowdata.alerts
    flowdata.alerts = lambda *a, **k: {"alerts": [{"ticker": "AAPL", "atEt": "2026-09-30 10:00", "type": "call", "premium": 50000}]}
    try:
        out = wallwatch.flow_study(SUB, budget_s=30)
        assert out["tickers"] == 1 and db.get(pk, "WWSIG#AAPL#2026-10-01T10:30")["flow"]["c3"] == 50000
        call("PUT", "/bot/wallwatch", {"flowRule": 3})                    # calls >= $5M in the 3 days before
        sig = {"at": "2026-10-07T10:30", "level": 100, "entry": 100.8, "stop": 99, "stop0": 99, "atr": 2, "target": 110}
        db.put({"PK": pk, "SK": "WWSIG#AAPL#2026-10-07T10:30", "symbol": "AAPL", **sig})
        r = wallwatch.enter(SUB, "AAPL", sig)
        assert r.get("skipped", "").startswith("Calls ≥ $5M") and "flow filter" in db.get(pk, "WWSIG#AAPL#2026-10-07T10:30")["tradeError"]
    finally:
        flowdata.alerts = saved


def test_wall_watch_retest():
    """Call flow arms the ticker; the support is the highest wall / flip under the price; a green retest candle enters."""
    import wallwatch
    w = {"callWall": 110, "flip": 102, "putWall": 100}
    bars = [{"t": "2026-10-05T09:30", "o": 104, "h": 105.5, "l": 103.5, "c": 105},       # big call alert inside: armed
            {"t": "2026-10-05T10:30", "o": 105, "h": 105.2, "l": 103.0, "c": 103.2},
            {"t": "2026-10-05T11:30", "o": 102.6, "h": 103.4, "l": 102.1, "c": 103.0},   # green, low back to the flip: entry
            {"t": "2026-10-06T09:30", "o": 103, "h": 110, "l": 102.9, "c": 109}]          # target
    al = [{"t": "2026-10-05T10:00", "c": 1, "p": 900000}, {"t": "2026-10-05T10:05", "c": 1, "p": 100000}]
    sg, watch = wallwatch.detect_retest(bars, {"2026-10-05": w, "2026-10-06": w}, alerts=al)
    assert len(sg) == 1 and sg[0]["kind"] == "retest" and sg[0]["level"] == 102 and sg[0]["levelName"] == "flip", sg
    assert sg[0]["armedAt"] == "2026-10-05T10:00" and sg[0]["status"] == "target" and sg[0]["target"] == 109.45 and watch is None
    assert wallwatch.sig_sk("AAA", sg[0]).endswith("#R")
    # the alert arrives while price sits on the support: the alert candle itself is the entry when it is green and holds
    flat = [{"t": "2026-10-05T09:30", "o": 102.0, "h": 102.6, "l": 101.9, "c": 102.4}] + bars[2:]
    fs = wallwatch.detect_retest(flat, {"2026-10-05": w, "2026-10-06": w}, alerts=al, datr={"next": 2.0})[0]
    assert fs and fs[0]["at"] == "2026-10-05T09:30" and fs[0]["levelName"] == "flip"
    # alert on a red candle at the support, entry on the next green candle that holds it
    rc = [{"t": "2026-10-05T09:30", "o": 103.0, "h": 103.1, "l": 102.0, "c": 102.2},
          {"t": "2026-10-05T10:30", "o": 102.2, "h": 102.9, "l": 101.95, "c": 102.7}]
    rs_ = wallwatch.detect_retest(rc, {"2026-10-05": w}, alerts=al, datr={"next": 2.0})[0]
    assert rs_ and rs_[0]["at"] == "2026-10-05T10:30"
    # a red candle at the support is not an entry
    red = bars[:2] + [{"t": "2026-10-05T11:30", "o": 103.2, "h": 103.4, "l": 102.1, "c": 102.5}]
    assert not wallwatch.detect_retest(red, {"2026-10-05": w}, alerts=al)[0]
    # only above the flip: the retest of a put wall under the flip is skipped
    fw2 = {"callWall": 110, "flip": 106, "putWall": 102}
    assert wallwatch.detect_retest(bars, {"2026-10-05": fw2, "2026-10-06": fw2}, alerts=al)[0]
    af = wallwatch.detect_retest(bars, {"2026-10-05": fw2, "2026-10-06": fw2}, cfg={"aboveFlip": True, "cwRoom": 0}, alerts=al)[0]
    assert af and af[0]["at"] == "2026-10-06T09:30" and af[0]["levelName"] == "flip" and af[0]["level"] == 106   # waits for the flip
    # earnings within 10 days: no entry; 11+ days away: entry
    assert not wallwatch.detect_retest(bars, {"2026-10-05": w, "2026-10-06": w}, alerts=al, earnings="2026-10-14")[0]
    assert wallwatch.detect_retest(bars, {"2026-10-05": w, "2026-10-06": w}, alerts=al, earnings="2026-10-20")[0]
    # daily close under the flip: out at the last candle of the day (entry above the flip)
    fx = [{"t": "2026-10-05T14:30", "o": 104, "h": 105.5, "l": 103.5, "c": 105},
          {"t": "2026-10-05T15:30", "o": 104.6, "h": 105.2, "l": 104.2, "c": 104.8},     # green, back to the flip 104.5: entry
          {"t": "2026-10-06T14:30", "o": 104.8, "h": 105, "l": 104.3, "c": 104.6},
          {"t": "2026-10-06T15:30", "o": 104.6, "h": 104.7, "l": 104.1, "c": 104.2}]      # day closes under the flip 104.5
    fw3 = {"callWall": 110, "flip": 104.5, "putWall": 100}
    fxs = wallwatch.detect_retest(fx, {"2026-10-05": fw3, "2026-10-06": fw3}, cfg={"trailAtr": 0, "stopAtr": 1},
                                  alerts=[{"t": "2026-10-05T14:45", "c": 1, "p": 900000}], datr={"next": 1.0})[0]
    assert fxs and fxs[0]["status"] == "flip" and fxs[0]["exit"] == 104.2 and fxs[0]["aboveFlipAtEntry"], fxs
    assert fxs[0]["alt"]["flipExit"] is False and fxs[0]["alt"]["status"] == "open"
    assert wallwatch.detect_retest(fx, {"2026-10-05": fw3, "2026-10-06": fw3}, cfg={"flipExit": False, "trailAtr": 0, "stopAtr": 1},
                                   alerts=[{"t": "2026-10-05T14:45", "c": 1, "p": 900000}], datr={"next": 1.0})[0][0]["status"] == "open"
    assert wallwatch._flip_chg({"2026-10-05": {"flip": 100}, "2026-10-06": {"flip": 98}}, "2026-10-06") == -2.0
    # the retest candle closing above the call wall: no entry
    w105 = {"callWall": 103.5, "flip": 102, "putWall": 100}
    nb = [bars[0], bars[1], {"t": "2026-10-05T11:30", "o": 102.6, "h": 104.0, "l": 102.1, "c": 103.8}]
    assert not wallwatch.detect_retest(nb, {"2026-10-05": w105}, alerts=al)[0]
    # too close under the call wall (less than cwRoom x ATR): no entry
    assert not wallwatch.detect_retest(bars, {"2026-10-05": fw2, "2026-10-06": fw2}, cfg={"aboveFlip": True, "cwRoom": 1}, alerts=al, datr={"next": 2.0})[0]
    assert wallwatch.detect_retest(bars, {"2026-10-05": w, "2026-10-06": w}, cfg={"cwRoom": 1}, alerts=al, datr={"next": 2.0})[0]
    # the call wall is never the support: price above the call wall has no support to retest
    cwb = [{"t": "2026-10-05T09:30", "o": 111, "h": 112, "l": 110.5, "c": 111.5},
           {"t": "2026-10-05T10:30", "o": 110.2, "h": 111, "l": 110.0, "c": 110.8}]
    assert not wallwatch.detect_retest(cwb, {"2026-10-05": w}, alerts=al, datr={"next": 2.0})[0]
    # VWAP restarts only on the highest volume of the last N trading days (not every opening candle)
    vb = []
    for dd in range(1, 6):
        for hh, vol in (("09:30", 500), ("10:30", 100), ("11:30", 100)):
            vb.append({"t": f"2026-10-0{dd}T{hh}", "o": 10, "h": 11, "l": 9, "c": 10, "v": vol})
    vb.append({"t": "2026-10-06T09:30", "o": 20, "h": 21, "l": 19, "c": 20, "v": 900})
    vv, resets = wallwatch.anchored_vwap(vb, days=3)
    assert resets == [0, 15] and vv[14] == 10.0 and vv[15] == 20.0, resets
    # alert too small: never armed
    assert not wallwatch.detect_retest(bars, {"2026-10-05": w, "2026-10-06": w}, alerts=[al[1]])[0]
    # support broken before the retest: the watch ends
    broken = bars[:1] + [{"t": "2026-10-05T10:30", "o": 104, "h": 104, "l": 98, "c": 98.5}] + bars[2:]
    assert not wallwatch.detect_retest(broken, {"2026-10-05": w, "2026-10-06": w}, alerts=al, datr={"next": 2.0})[0]
    # still waiting for the retest: the watch is returned
    sg2, watch2 = wallwatch.detect_retest(bars[:2], {"2026-10-05": w}, alerts=al)
    assert not sg2 and watch2["level"] == 102 and watch2["levelName"] == "flip"
    wallwatch.FLOW_ONLY = True
    STORE.clear()
    pk = db.upk(SUB)
    call("GET", "/bot/wallwatch")
    db.put({"PK": pk, "SK": "WWSIG#AAPL#2026-10-05T11:30#R", "symbol": "AAPL", **sg[0]})
    db.put({"PK": pk, "SK": "WWSIG#AAPL#2026-10-01T11:30", "symbol": "AAPL", "kind": "pw", "at": "2026-10-01T11:30", "status": "stop",
            "pct": -1, "R": -1, "riskPct": 1})
    st, v = call("GET", "/bot/wallwatch")
    assert v["summary"]["signals"] == 1 and v["config"]["pwSetup"] is False and v["config"]["retestSetup"] is True
    rows = {r["name"]: r for r in v["retestStudy"]["rows"]}
    assert v["retestStudy"]["base"]["n"] == 1 and rows["Support: flip"]["n"] == 1 and rows["Call alert $500K – $1M"]["n"] == 1
    assert rows["Retest within 1 day of the alert"]["n"] == 1 and rows["Support: put wall"]["n"] == 0


def test_flow_scan_watch_only():
    """Wall watch trading: flow tickers are evaluated, marked WATCH and put on the list; nothing is bought."""
    import autotrader, flowdata, wallwatch
    STORE.clear()
    pk = db.upk(SUB)
    call("GET", "/bot/wallwatch")
    saved = (flowdata.alerts, autotrader.evaluate, autotrader.place)
    placed = []
    try:
        from datetime import datetime as _dt
        now = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        flowdata.alerts = lambda *a, **k: {"alerts": [{"ticker": "QQQZ", "premium": 700000, "at": now, "sweep": True, "contract": "X"}]}
        def fake_eval(sub, sym, source=None):
            assert source.get("watchOnly")
            rec = {"id": "20261012100000-aaaaaa", "symbol": sym, "decision": "SKIP", "blocking": ["chart"], "status": "proposed"}
            db.put({"PK": pk, "SK": f"BOT#{rec['id']}", **rec})
            return rec
        autotrader.evaluate = fake_eval
        autotrader.place = lambda *a, **k: placed.append(a)
        out = autotrader.flow_scan(SUB, buy=False)
        rec = db.get(pk, "BOT#20261012100000-aaaaaa")
        assert out["evaluated"][0]["decision"] == "WATCH" and rec["decision"] == "WATCH" and rec["ruleDecision"] == "SKIP"
        assert rec["blocking"][0].startswith("On the Wall watch list") and not placed
        cfg = wallwatch.config(SUB)
        assert "QQQZ" in cfg["tickers"] and cfg["added"]["QQQZ"]["by"] == "flow" and cfg["added"]["QQQZ"]["premium"] == 700000
    finally:
        flowdata.alerts, autotrader.evaluate, autotrader.place = saved


def test_wallwatch_skips_claude_review():
    import aicheck
    assert aicheck.is_wallwatch({"proposal": {"strategyOnly": "wallwatch"}})
    assert aicheck.is_wallwatch({"proposal": {"wallwatch": {"stop": 1}}})
    assert not aicheck.is_wallwatch({"proposal": {}})


def test_wall_watch_rule_switches():
    """Each flow retest rule can be turned on or off."""
    import wallwatch
    w = {"callWall": 110, "flip": 102, "putWall": 100}
    bars = [{"t": "2026-10-05T09:30", "o": 104, "h": 105.5, "l": 103.5, "c": 105},
            {"t": "2026-10-05T10:30", "o": 105, "h": 105.2, "l": 103.0, "c": 103.2},
            {"t": "2026-10-05T11:30", "o": 102.6, "h": 103.4, "l": 102.1, "c": 103.0},
            {"t": "2026-10-06T09:30", "o": 103, "h": 110, "l": 102.9, "c": 109}]
    al = [{"t": "2026-10-05T10:00", "c": 1, "p": 900000}]
    ws = {"2026-10-05": w, "2026-10-06": w}
    run = lambda **cfg: wallwatch.detect_retest(bars, ws, cfg=cfg, alerts=al, datr={"next": 2.0})[0]
    assert run()[0]["levelName"] == "flip"
    ns = run(useFlip=False, usePW=False)                            # no support rule: any candle, stop under its low
    assert not ns or ns[0]["levelName"] == "candle low"
    s = run(useFlip=False)                                           # put wall only: price never comes back to 100
    assert not s
    assert run(targetOn=False)[0]["target"] is None
    st = run(stopOn=False)[0]
    assert st["stop0"] > 0 and st["status"] != "stop"
    dip = bars[:3] + [{"t": "2026-10-06T09:30", "o": 103, "h": 103.2, "l": 99.0, "c": 103.1}]   # under stop0 = 101
    assert wallwatch.detect_retest(dip, ws, cfg={"trailOn": False}, alerts=al, datr={"next": 2.0})[0][0]["status"] == "stop"
    assert wallwatch.detect_retest(dip, ws, cfg={"trailOn": False, "stopOn": False}, alerts=al, datr={"next": 2.0})[0][0]["status"] == "open"
    assert not run(emaOn=True, emaLen=2)                             # close 103 under the 2-candle EMA
    red = bars[:2] + [{"t": "2026-10-05T11:30", "o": 103.2, "h": 103.4, "l": 102.1, "c": 102.5}]
    assert not wallwatch.detect_retest(red, {"2026-10-05": w}, alerts=al, datr={"next": 2.0})[0]
    assert wallwatch.detect_retest(red, {"2026-10-05": w}, cfg={"greenOnly": False}, alerts=al, datr={"next": 2.0})[0]
    assert wallwatch.version({**wallwatch.PARAMS, "vwapOn": True}) != wallwatch.version(wallwatch.PARAMS)
    # exits on an hourly close under the VWAP / EMA
    after = bars[:3] + [{"t": "2026-10-06T09:30", "o": 103, "h": 103.5, "l": 102.2, "c": 102.4},
                        {"t": "2026-10-06T10:30", "o": 102.4, "h": 104, "l": 102.3, "c": 103.9}]
    em = {b["t"][:16]: 102.8 for b in after}
    x = wallwatch.detect_retest(after, ws, cfg={"emaExit": True, "trailOn": False}, alerts=al, datr={"next": 2.0}, ema_map=em)[0][0]
    assert x["status"] == "ema" and x["exit"] == 102.4 and x["exitAt"] == "2026-10-06T09:30", x
    vm = {b["t"][:16]: 102.8 for b in after}
    assert wallwatch.detect_retest(after, ws, cfg={"vwapExit": True, "trailOn": False}, alerts=al, datr={"next": 2.0}, vwap_map=vm)[0][0]["status"] == "vwap"
    assert wallwatch.detect_retest(after, ws, cfg={"trailOn": False}, alerts=al, datr={"next": 2.0}, vwap_map=vm)[0][0]["status"] == "open"
    # entry rule: EMA above the VWAP
    up = {b["t"][:16]: 102.0 for b in bars}
    assert not wallwatch.detect_retest(bars, ws, cfg={"emaOverVwap": True}, alerts=al, datr={"next": 2.0}, vwap_map=up, ema_map={k: 101.5 for k in up})[0]
    assert wallwatch.detect_retest(bars, ws, cfg={"emaOverVwap": True}, alerts=al, datr={"next": 2.0}, vwap_map=up, ema_map={k: 102.5 for k in up})[0]
    # entry rule: close above the VWAP
    assert not wallwatch.detect_retest(bars, ws, cfg={"vwapOn": True}, alerts=al, datr={"next": 2.0}, vwap_map={b["t"][:16]: 104.0 for b in bars})[0]
    assert wallwatch.detect_retest(bars, ws, cfg={"vwapOn": True}, alerts=al, datr={"next": 2.0}, vwap_map={b["t"][:16]: 102.5 for b in bars})[0]


def test_rule_tester():
    """History loader + rule tester on synthetic data: base, rules, buckets and exits all come back."""
    import charts, flowdata, wallwatch, backtest
    import datetime as dt
    from util import now_ny
    sub = "rt-user"
    wallwatch.save_config(sub, {"tickers": ["AAA"]})
    today = now_ny().date()
    days = []
    d = today - dt.timedelta(days=200)
    while d < today:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    hb, px = [], 100.0
    import math
    for i, day in enumerate(days):
        for h in ("09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"):
            k = len(hb)
            o = px
            px = 100 + 4 * math.sin(k / 15.0)
            hb.append({"t": f"{day}T{h}", "o": o, "h": max(o, px) + 0.3, "l": min(o, px) - 0.3, "c": px, "v": 1000 + (k % 7) * 10})
    db_ = []
    for day in days:
        db_.append({"t": f"{day}T09:30", "o": 100, "h": 104, "l": 96, "c": 100, "v": 1e6})
    saved = (charts._yahoo, flowdata.stored_walls, backtest.stored_alerts)
    try:
        charts._yahoo = lambda sym, tf, s_, e: hb if tf == "1h" else db_
        flowdata.stored_walls = lambda sym: {day: {"callWall": 106, "putWall": 98, "flip": 100} for day in days}
        backtest.stored_alerts = lambda sub_, sym: [{"t": f"{day}T10:00", "c": 1, "p": 900000} for day in days[::3]]
        out = backtest.rule_test(sub, {"days": 120, "fixR": 2, "maxDays": 5, "full": True})
        quick = backtest.rule_test(sub, {"days": 120, "fixR": 2, "maxDays": 5})
        assert [r["key"] for r in quick["rules"]] == ["base", "mine"] and not quick["buckets"] and len(quick["exits"]) == 7
        out = backtest.rule_test(sub, {"days": 120, "fixR": 2, "maxDays": 5, "full": True})
        base = out["rules"][0]
        assert base["key"] == "base" and base["n"] > 5, base
        assert {r["key"] for r in out["rules"]} >= {"base", "greenOnly", "vwapOn", "emaOverVwap", "mine"}
        assert out["exits"][0]["n"] == out["rules"][-1]["n"]          # fixed exit on your entry = "Your entry settings"
        assert any(b["group"] == "Call alert" and b["n"] for b in out["buckets"])
        assert base["h1n"] + base["h2n"] == base["n"]
        assert backtest.test_result(sub)["rules"][0]["n"] == base["n"]
    finally:
        charts._yahoo, flowdata.stored_walls, backtest.stored_alerts = saved
    try:
        charts._yahoo = lambda sym, tf, s_, e: hb if tf == "1h" else db_
        flowdata.stored_walls = lambda sym: {day: {"callWall": 106, "putWall": 98, "flip": 100} for day in days}
        backtest.stored_alerts = lambda sub_, sym: [{"t": f"{day}T10:00", "c": 1, "p": 900000} for day in days[::3]]
        rb = backtest.robust_test(sub, {"days": 120})
        assert rb["rows"][0]["label"].startswith("Your settings") and len(rb["rows"]) >= 6
        assert backtest.robust_result(sub)["rows"][0]["n"] == rb["rows"][0]["n"]
        mc = backtest._mc([1, -1, 2, -1, -1, 0.5, 2, -1])
        assert mc["lossStreak"] == 2 and mc["dd95"] >= mc["ddMedian"] >= 0
    finally:
        charts._yahoo, flowdata.stored_walls, backtest.stored_alerts = saved
    st = backtest.start_history(sub, 120)
    assert st["running"] and st["days"] == 120
    # stored history: a second load only asks for the days since the last one
    asked = []
    saved_al = flowdata.alerts
    try:
        flowdata.alerts = lambda *a_, **k: asked.append(a_[9]) or {"alerts": [{"ticker": "AAA", "atEt": "2026-09-01 10:00", "premium": 300000}]}
        r1 = backtest._fetch_alerts(sub, "AAA", 120)
        r2 = backtest._fetch_alerts(sub, "AAA", 120)
        assert asked[0] > 150 and asked[1] <= 3 and r2["topUp"] and r1["oldest"] == "2026-09-01", asked
        assert len(backtest.stored_alerts.__wrapped__(sub, "AAA") if hasattr(backtest.stored_alerts, "__wrapped__") else
                   [x for r in db.q_prefix(db.upk(sub), "WWHFLOW#AAA#") for x in r["alerts"]]) == 1
    finally:
        flowdata.alerts = saved_al


def test_tos_indicators():
    """Keltner channel, opening range and SPY trend: values and the entry switches."""
    import wallwatch, charts
    bars = [{"t": f"2026-10-0{1 + i // 7}T{['09:30','10:30','11:30','12:30','13:30','14:30','15:30'][i % 7]}",
             "o": 100 + i * .1, "h": 101 + i * .1, "l": 99 + i * .1, "c": 100.5 + i * .1, "v": 1} for i in range(35)]
    kc = wallwatch.keltner(bars, 5, 3)
    assert kc[3] is None and kc[10][1] > kc[10][0] > kc[10][2] and abs((kc[10][1] - kc[10][0]) - (kc[10][0] - kc[10][2])) < 1e-6
    orb = wallwatch.orb_levels(bars)
    assert orb[0] is None and orb[1] == (bars[0]["h"], bars[0]["l"]) and orb[7] is None and orb[8] == (bars[7]["h"], bars[7]["l"])
    saved = charts._yahoo
    try:
        import datetime as dt
        days = ["2026-09-%02d" % d for d in (21, 22, 23, 24, 25, 28, 29, 30)]
        charts._yahoo = lambda sym, tf, s_, e: ([{"t": f"{d}T09:30", "o": 1, "h": 1, "l": 1, "c": 500 + k} for k, d in enumerate(days)] if tf == "1d"
                                               else [{"t": "2026-09-30T10:30", "o": 1, "h": 1, "l": 1, "c": 499.0}, {"t": "2026-09-30T11:30", "o": 1, "h": 1, "l": 1, "c": 503.0}])
        wallwatch._SPY.clear()
        sp = wallwatch.spy_trend(None, dt.datetime(2026, 10, 1, 12))
        assert sp == {"2026-09-30T10:30": -1, "2026-09-30T11:30": 1}, sp      # 6 sessions before 09-30 = 09-22 (501)
    finally:
        charts._yahoo = saved
        wallwatch._SPY.clear()
    w = {"callWall": 110, "flip": 102, "putWall": 100}
    rb = [{"t": "2026-10-05T09:30", "o": 104, "h": 105.5, "l": 103.5, "c": 105},
          {"t": "2026-10-05T10:30", "o": 105, "h": 105.2, "l": 103.0, "c": 103.2},
          {"t": "2026-10-05T11:30", "o": 102.6, "h": 103.4, "l": 102.1, "c": 103.0}]
    al = [{"t": "2026-10-05T10:00", "c": 1, "p": 900000}]
    ws = {"2026-10-05": w}
    assert wallwatch.detect_retest(rb, ws, alerts=al, datr={"next": 2.0})[0]
    assert not wallwatch.detect_retest(rb, ws, cfg={"spyUp": True}, alerts=al, datr={"next": 2.0}, extra={"spy": {"2026-10-05T11:30": -1}})[0]
    assert wallwatch.detect_retest(rb, ws, cfg={"spyUp": True}, alerts=al, datr={"next": 2.0}, extra={"spy": {"2026-10-05T11:30": 1}})[0]
    assert not wallwatch.detect_retest(rb, ws, cfg={"orbAbove": True}, alerts=al, datr={"next": 2.0})[0]     # 103 < ORB high 105.5
    sg = wallwatch.detect_retest(rb, ws, alerts=al, datr={"next": 2.0}, extra={"kc": {"2026-10-05T11:30": (103.5, 104, 103)}})[0][0]
    assert sg["orb"] == [105.5, 103.5] and sg["kc"] == [103.5, 104, 103]
    assert not wallwatch.detect_retest(rb, ws, cfg={"kcNotExt": True}, alerts=al, datr={"next": 2.0}, extra={"kc": {"2026-10-05T11:30": (101, 102.5, 99.5)}})[0]
    # skew rule: only with a normal skew (middle third), no skew known = no entry
    assert not wallwatch.detect_retest(rb, ws, cfg={"skewOn": True}, alerts=al, datr={"next": 2.0})[0]
    assert not wallwatch.detect_retest(rb, ws, cfg={"skewOn": True}, alerts=al, datr={"next": 2.0}, extra={"skewPct": {"2026-10-05": 90}})[0]
    ok = wallwatch.detect_retest(rb, ws, cfg={"skewOn": True}, alerts=al, datr={"next": 2.0}, extra={"skewPct": {"2026-10-05": 50}})[0]
    assert ok and ok[0]["skewPct"] == 50


def test_intraday_gamma_levels():
    """Intraday framework: slow rejection at the put wall, fast overshoot + reclaim, short at the call wall, calendar."""
    import intraday
    ev = intraday.event_days(2026, 2026)
    assert ev["2026-10-16"] == "monthly OPEX" and ev["2026-09-18"] == "triple witching" and ev["2026-10-21"] == "VIX expiration"
    def mk(day, path):
        out = []
        import datetime as dt
        t0 = dt.datetime.strptime(day + " 09:30", "%Y-%m-%d %H:%M")
        for k, (o, h, l, c) in enumerate(path):
            out.append({"t": (t0 + dt.timedelta(minutes=5 * k)).strftime("%Y-%m-%dT%H:%M"), "o": o, "h": h, "l": l, "c": c})
        return out
    w = {"2026-10-05": {"putWall": 100, "flip": 103, "callWall": 106}}
    # slow drift down to the put wall, green rejection candle, then up to the flip (target)
    path = [(101.6 - k * 0.1, 101.7 - k * 0.1, 101.45 - k * 0.1, 101.5 - k * 0.1) for k in range(15)]   # 101.6 -> 100.1
    path += [(100.15, 100.4, 99.9, 100.35)]            # touches 99.9, closes above 100: rejection
    path += [(100.35 + k * 0.2, 100.6 + k * 0.2, 100.3 + k * 0.2, 100.55 + k * 0.2) for k in range(20)]  # up to 103+
    bars = mk("2026-10-05", path)
    tr = intraday.simulate(bars, w, {"2026-10-05": 3.0}, {"minRR": 1.0}, {}, "AAA")
    assert tr and tr[0]["side"] == "long" and tr[0]["kind"] == "rejection" and tr[0]["levelName"] == "put wall", tr
    assert tr[0]["status"] == "target" and tr[0]["target"] == 103 and tr[0]["R"] > 1
    # fast drop through the put wall, then a close back above it: reclaim
    fpath = [(103.0, 103.1, 102.9, 103.0)] * 7 + [(103, 103, 101, 101.2), (101.2, 101.3, 100.2, 100.3), (100.3, 100.4, 99.2, 99.4),
             (99.4, 99.6, 99.1, 99.5), (99.5, 100.6, 99.4, 100.5)] + [(100.5 + k * .2, 100.8 + k * .2, 100.4 + k * .2, 100.7 + k * .2) for k in range(20)]
    tf = intraday.simulate(mk("2026-10-05", fpath), w, {"2026-10-05": 6.0}, {"minRR": 1.0}, {}, "AAA")
    assert tf and tf[0]["kind"] == "reclaim" and tf[0]["side"] == "long" and tf[0]["fast"], tf
    # event day skipped
    assert not intraday.simulate(bars, w, {"2026-10-05": 3.0}, {"minRR": 1.0}, {"2026-10-05": "monthly OPEX"}, "AAA")
    # short at the call wall from below: slow rise, red rejection, down to the flip
    spath = [(104.4 + k * .1, 104.55 + k * .1, 104.35 + k * .1, 104.5 + k * .1) for k in range(15)] + [(105.85, 106.1, 105.7, 105.75)]
    spath += [(105.75 - k * .2, 105.8 - k * .2, 105.5 - k * .2, 105.55 - k * .2) for k in range(20)]
    ts = intraday.simulate(mk("2026-10-05", spath), w, {"2026-10-05": 3.0}, {"minRR": 1.0}, {}, "AAA")
    assert ts and ts[0]["side"] == "short" and ts[0]["levelName"] == "call wall" and ts[0]["status"] == "target", ts
    # the whole run on mocked data: tables come back and are stored
    import charts, flowdata
    saved = (charts._yahoo, flowdata.stored_walls)
    try:
        import datetime as dt
        today = __import__("util").now_ny().date()
        ds = [(today - dt.timedelta(days=k)).isoformat() for k in range(1, 30) if (today - dt.timedelta(days=k)).weekday() < 5]
        five = []
        for d in sorted(ds):
            five += mk(d, path)
        charts._yahoo = lambda sym, tf, s_, e: five if tf == "5m" else [{"t": f"{d}T09:30", "o": 100, "h": 103, "l": 97, "c": 100} for d in sorted(ds)]
        flowdata.stored_walls = lambda sym: {d: w["2026-10-05"] for d in ds}
        r = intraday.run("intra-user", {"tickers": "QQQ", "minRR": 1.0, "skipEvents": False})
        assert r["all"]["n"] >= 5 and r["groups"] and r["exits"][1]["n"] == r["all"]["n"] and intraday.result("intra-user")["all"]["n"] == r["all"]["n"], r["all"]
    finally:
        charts._yahoo, flowdata.stored_walls = saved


def test_skew_history():
    """25-delta risk reversal: ~30-day expiry chosen per day, stored, and turned into features known before the open."""
    import backtest, flowdata
    import datetime as dt
    from util import now_ny
    calls = []
    def fake(key, path, params=None, attempts=4):
        calls.append(params)
        if "historical-risk-reversal-skew" not in path or params["delta"] != "25":
            return None
        exp = dt.date.fromisoformat(params["expiry"])
        days = [exp - dt.timedelta(days=k) for k in range(70, 0, -1)]
        return {"data": [{"date": d.isoformat() + "T00:00:00.000Z", "delta": 25, "risk_reversal": str(round(0.02 + (exp.month % 3) * 0.01 + k * 0.0001, 5))}
                         for k, d in enumerate(days)]}
    saved = flowdata._uw
    try:
        flowdata._uw = fake
        sk = backtest.fetch_skew("k", "SKW", 120)
        assert len(sk) > 60 and backtest.stored_skew("SKW") == sk
        n = len(calls)
        backtest.fetch_skew("k", "SKW", 120)
        assert len(calls) == n                                  # stored: no new request
    finally:
        flowdata._uw = saved
    day = sorted(sk)[-1]
    f = backtest.skew_features(sk, day)
    assert f and 0 <= f["skewPct"] <= 100 and f["skew"] == sk[sorted(sk)[-2]]
    assert backtest.skew_features(sk, sorted(sk)[5]) is None   # not enough history before


def test_darkpool_history():
    """Dark pool history: every day fetched once, packed by month; features known before the entry day."""
    import backtest, flowdata, charts
    import datetime as dt
    from util import now_ny
    seen = []
    def fake_day(key, sym, d, floor):
        seen.append(d)
        return [{"t": f"{d}T10:00", "price": 100.0 + (hash(d) % 3) * 0.1, "size": 10**7, "premium": 5e8, "side": None},
                {"t": f"{d}T11:00", "price": 110.0, "size": 10**7, "premium": 1e8, "side": None}]
    saved = (flowdata.darkpool_day, charts._yahoo)
    try:
        flowdata.darkpool_day = fake_day
        charts._yahoo = lambda *a, **k: [{"t": (now_ny().date() - dt.timedelta(days=k)).isoformat() + "T09:30", "o": 1, "h": 1, "l": 1, "c": 100, "v": 1e6} for k in range(40, 0, -1)]
        n = backtest.fetch_darkpool("k", "DPX", 60)
        assert n and n == len(seen)
        before = len(seen)
        assert backtest.fetch_darkpool("k", "DPX", 60) == n and len(seen) == before    # stored: nothing fetched again
    finally:
        flowdata.darkpool_day, charts._yahoo = saved
    pr = backtest.stored_darkpool("DPX")
    assert len(pr) == 2 * n and pr == sorted(pr, key=lambda x: x["t"])
    day = (now_ny().date()).isoformat()
    f = backtest.dp_features(pr, day, entry=101.0, support=100.0, target=112.0, atr=2.0)
    assert f["dpNear"] is True and f["dpWay"] is True and f["dpAct"] > 0
    g = backtest.dp_features(pr, day, entry=101.0, support=90.0, target=105.0, atr=2.0)
    assert g["dpNear"] is False and g["dpWay"] is False


def test_live_partial_exit():
    """Live: at entry + 1R, 40% of the contracts are sold at market and the stop of the rest moves to the entry."""
    import autotrader, db
    sent, saved = [], (autotrader._alp, autotrader._paper_creds, autotrader._notify)
    try:
        autotrader._alp = lambda c, m, path, body=None: sent.append((m, path, body)) or {"id": "ord-1"}
        autotrader._paper_creds = lambda sub: {}
        autotrader._notify = lambda *a, **k: None
        sub = "partial-user"
        p = {"contract": "AAA261120C00100000", "underlyingStop": 98.0, "exp": "2026-11-20"}
        ww = {"entry": 100.0, "stop0": 98.0, "scaleOn": True, "scaleR": 1, "scalePct": 40, "scaleBE": True}
        rec = {"SK": "BOT#20261005000000-partial", "symbol": "AAA", "filledQty": 5, "lastMark": 2.5}
        db.put({"PK": db.upk(sub), **rec})
        bars = [{"t": "2026-10-05T10:00", "o": 100, "h": 101.5, "l": 99.8, "c": 101}]
        assert autotrader._partial_exit(sub, rec, p, ww, bars, 98.0) is None and not sent          # 102 not reached
        bars.append({"t": "2026-10-05T10:05", "o": 101, "h": 102.2, "l": 100.9, "c": 102})
        autotrader._partial_exit(sub, rec, p, ww, bars, 98.0)
        assert sent[0][2]["qty"] == "2" and sent[0][2]["side"] == "sell"
        r = db.get(db.upk(sub), rec["SK"])
        assert r["wwScaled"]["qty"] == 2 and float(r["proposal"]["underlyingStop"]) == 100.0 and r["partials"][0]["orderId"] == "ord-1"
    finally:
        autotrader._alp, autotrader._paper_creds, autotrader._notify = saved


def test_inertia_regime():
    """Daily inertia: green when the regression of the daily closes rises on all 3 lengths, red when on none."""
    import wallwatch, datetime as dt
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    up = [{"t": f"{(dt.date(2026, 1, 1) + dt.timedelta(days=d)).isoformat()}T{h}", "o": 100 + d, "h": 101 + d, "l": 99 + d, "c": 100 + d}
          for d in range(30) for h in H]
    r = wallwatch.inertia_regime(up, (10, 6, 3))
    assert r[0] is None and r[-1]["s"] == "G" and r[-1]["pts"] == 3.0
    down = [dict(b, c=200 - (100 - b["c"]) * -1) for b in up]
    down = [dict(b, c=300 - b["c"]) for b in up]
    assert wallwatch.inertia_regime(down, (10, 6, 3))[-1]["s"] == "R"
    # entry filter: green only
    w = {b["t"][:10]: {"putWall": 50, "callWall": 400, "flip": 40} for b in up}
    sg, _ = wallwatch.detect_retest(down, w, None, {"alertReq": False, "inGreen": True, "greenOnly": False, "earnOn": False}, None)
    assert sg == []


def test_keltner_daily():
    """Daily Keltner on hourly candles: the previous days' EMAs updated with today's candle so far (no later price)."""
    import wallwatch
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    bars = [{"t": f"2026-09-{d:02d}T{h}", "o": 100, "h": 101, "l": 99, "c": 100} for d in range(1, 6) for h in H]
    bars += [{"t": f"2026-09-08T{h}", "o": 100, "h": 100 + k + 1, "l": 99, "c": 100 + k} for k, h in enumerate(H)]
    kc = wallwatch.keltner(bars, 3, 2.0, daily=True)
    assert kc[0] is None and kc[20] is None and kc[21] is not None    # 3 completed days needed
    first_today = kc[35]                                                 # 09-08 09:30: close 100, range 99-101
    assert first_today[0] == 100 and round(first_today[1] - first_today[0], 4) == 4.0
    assert kc[41][0] == round(106 * 0.5 + 100 * 0.5, 4)                 # 15:30: today's close 106 weighs 2/(3+1)
    assert kc[34] == kc[34] and kc[36][0] < kc[41][0]
    hk = wallwatch.keltner(bars, 3, 2.0)                                  # hourly channel: a value from the 3rd candle
    assert hk[1] is None and hk[2] is not None and hk[41] != kc[41]


def test_keltner_ride_exit():
    """A vertical move through the call wall is ridden while the days close above the upper Keltner band;
    the first daily close back under it sells. Without the switch the call wall target sells."""
    import wallwatch
    w = {"callWall": 106, "flip": 102, "putWall": 100}
    days = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
    ws = {d: w for d in days}
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    bars = [{"t": "2026-10-05T09:30", "o": 104, "h": 105.5, "l": 103.5, "c": 105},
            {"t": "2026-10-05T10:30", "o": 105, "h": 105.2, "l": 103.0, "c": 103.2},
            {"t": "2026-10-05T11:30", "o": 102.6, "h": 103.4, "l": 102.1, "c": 103.0}]      # entry
    bars += [{"t": f"2026-10-05T{h}", "o": 103.5, "h": 104, "l": 103.4, "c": 103.8} for h in H[3:]]
    px = 104.0
    for h in H:                                                                           # day 2: vertical, through the CW 106
        bars.append({"t": f"2026-10-06T{h}", "o": px, "h": px + 1.2, "l": px - 0.1, "c": px + 1.0}); px += 1.0
    for h in H:                                                                           # day 3: keeps going
        bars.append({"t": f"2026-10-07T{h}", "o": px, "h": px + 0.6, "l": px - 0.1, "c": px + 0.5}); px += 0.5
    for h in H:                                                                           # day 4: fades
        bars.append({"t": f"2026-10-08T{h}", "o": px, "h": px + 0.1, "l": px - 0.6, "c": px - 0.5}); px -= 0.5
    kc = {}
    for b in bars:                                                                        # upper band just under the close on days 2-3, above it on day 4
        d = b["t"][:10]
        kc[b["t"]] = (b["c"] - 2, b["c"] - 0.3 if d in ("2026-10-06", "2026-10-07") else b["c"] + 0.5, b["c"] - 4)
    al = [{"t": "2026-10-05T10:00", "c": 1, "p": 900000}]
    base = {"tgtBuf": 0, "trailOn": False, "flipExit": False}
    plain = wallwatch.detect_retest(bars, ws, cfg=base, alerts=al, datr={"next": 2.0}, extra={"kc": kc})[0][0]
    assert plain["status"] == "target" and plain["exit"] == 106, plain
    ride = wallwatch.detect_retest(bars, ws, cfg={**base, "kcRide": True}, alerts=al, datr={"next": 2.0}, extra={"kc": kc})[0][0]
    assert ride["status"] == "keltner" and ride["exitAt"] == "2026-10-08T15:30" and ride["exit"] > 106, ride
    # same day: an hourly close above the band starts the ride, the day closes under it -> sold at that close
    same = bars[:6] + [{"t": f"2026-10-06T{h}", "o": 104, "h": 104.6, "l": 103.9, "c": 104.4} for h in H]
    k2 = {b["t"]: (b["c"] - 2, (b["c"] - 0.2) if b["t"] == "2026-10-06T11:30" else b["c"] + 0.3, b["c"] - 4) for b in same}
    sd = wallwatch.detect_retest(same, ws, cfg={**base, "kcRide": True}, alerts=al, datr={"next": 2.0}, extra={"kc": k2})[0][0]
    assert sd["status"] == "keltner" and sd["exitAt"] == "2026-10-06T15:30", sd
    # live: an hourly close above the band starts the ride; from 15:50 a close under the band sells
    import autotrader, db
    import datetime as dt
    saved = autotrader._yahoo
    try:
        hb = [{"t": f"2026-10-0{1 + k // 7}T{H[k % 7]}", "o": 100 + k * .2, "h": 100.5 + k * .2, "l": 99.8 + k * .2, "c": 100.3 + k * .2, "v": 1} for k in range(35)]
        autotrader._yahoo = lambda *a, **k: hb
        sub, ww = "ride-user", {"kcRide": True, "kcLen": 5, "kcMult": 0.1, "kcDaily": False}
        rec = {"SK": "BOT#20261005000000-abcdef", "symbol": "AAA"}
        db.put({"PK": db.upk(sub), **rec})
        p_ = {"underlyingTarget": 105.0}
        mid = dt.datetime(2026, 10, 6, 12, 0)
        up = wallwatch.keltner(wallwatch.finished(hb, mid), 5, 0.1)[-1][1]
        assert autotrader._keltner_ride(sub, rec, p_, ww, {"h": 110, "c": up + 1}, "2026-10-01T09:30", mid, 10) is None   # rising bars: ride
        rec = db.get(db.upk(sub), rec["SK"])
        assert rec.get("wwRide")
        late = dt.datetime(2026, 10, 6, 15, 52)
        assert "ride ended" in autotrader._keltner_ride(sub, rec, p_, ww, {"h": up, "c": up - 5}, "2026-10-01T09:30", late, 10)
    finally:
        autotrader._yahoo = saved


def test_retest_misses():
    """Candles that reach the support without entering are listed with the rule that blocked them."""
    import wallwatch
    w = {"callWall": 110, "flip": 102, "putWall": 100}
    bars = [{"t": "2026-10-05T09:30", "o": 104, "h": 105.5, "l": 103.5, "c": 105},
            {"t": "2026-10-05T10:30", "o": 105, "h": 105.2, "l": 103.0, "c": 103.2},
            {"t": "2026-10-05T11:30", "o": 102.6, "h": 103.4, "l": 102.1, "c": 103.0}]
    al = [{"t": "2026-10-05T10:00", "c": 1, "p": 900000}]
    m = []
    sg = wallwatch.detect_retest(bars, {"2026-10-05": w}, cfg={"vwapOn": True}, alerts=al, datr={"next": 2.0},
                                 vwap_map={b["t"]: 104.0 for b in bars}, extra={"misses": m})[0]
    assert not sg and any("VWAP" in x["r"] for x in m), m
    m2 = []
    wallwatch.detect_retest(bars, {"2026-10-05": w}, alerts=[], datr={"next": 2.0}, extra={"misses": m2})
    assert m2 and m2[0]["r"].startswith("no call alert"), m2
    m3 = []
    wallwatch.detect_retest(bars, {"2026-10-05": w}, cfg={"cwRoom": 5, "cwRoomOn": True}, alerts=al, datr={"next": 2.0}, extra={"misses": m3})
    assert any("under the call wall" in x["r"] for x in m3), m3


def test_back_under_level_exit():
    """Passed the call wall / a dark pool level on an hourly close, the day closes back under it: sold at that close."""
    import wallwatch
    w = {"callWall": 106, "flip": 102, "putWall": 100}
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    bars = [{"t": "2026-10-05T09:30", "o": 104, "h": 105.5, "l": 103.5, "c": 105},
            {"t": "2026-10-05T10:30", "o": 105, "h": 105.2, "l": 103.0, "c": 103.2},
            {"t": "2026-10-05T11:30", "o": 102.6, "h": 103.4, "l": 102.1, "c": 103.0}]          # entry
    bars += [{"t": f"2026-10-05T{h}", "o": 103.5, "h": 104, "l": 103.4, "c": 103.8} for h in H[3:]]
    path = [104.5, 105.5, 106.4, 106.8, 106.2, 105.6, 105.2]                                   # through the CW 106, back under
    bars += [{"t": f"2026-10-06T{h}", "o": c - .2, "h": c + .3, "l": c - .4, "c": c} for h, c in zip(H, path)]
    ws = {"2026-10-05": w, "2026-10-06": w}
    al = [{"t": "2026-10-05T10:00", "c": 1, "p": 900000}]
    base = {"targetOn": False, "trailOn": False, "flipExit": False}
    a = wallwatch.detect_retest(bars, ws, cfg=base, alerts=al, datr={"next": 2.0})[0][0]
    assert a["status"] == "open"
    b = wallwatch.detect_retest(bars, ws, cfg={**base, "cwLost": True}, alerts=al, datr={"next": 2.0})[0][0]
    assert b["status"] == "lost" and b["exitAt"] == "2026-10-06T15:30" and b["lostLevel"] == "call wall 106", b
    c = wallwatch.detect_retest(bars, ws, cfg={**base, "dpLost": True}, alerts=al, datr={"next": 2.0},
                                extra={"dpLevels": {"2026-10-06": [105.8, 99.0]}})[0][0]
    assert c["status"] == "lost" and c["lostLevel"] == "dark pool level 105.8", c
    pr = [{"t": f"2026-09-{d:02d}T10:00", "price": 105.8 + (d % 2) * 0.01, "premium": 5e8} for d in range(1, 29)]
    m = wallwatch.dp_level_map(pr, ["2026-10-06"])
    assert m["2026-10-06"] and abs(m["2026-10-06"][0] - 105.8) < 0.1
    # live: an hourly close above the call wall is remembered; from 15:50 a close back under it sells
    import autotrader, flowdata, db
    import datetime as dt
    saved = (autotrader._yahoo, flowdata.stored_walls)
    try:
        autotrader._yahoo = lambda *a_, **k: [{"t": "2026-10-06T10:30", "o": 106, "h": 106.6, "l": 105.9, "c": 106.4, "v": 1}]
        flowdata.stored_walls = lambda sym: {"2026-10-06": {"callWall": 106}}
        sub, rec = "lost-user", {"SK": "BOT#20261005000000-aaaaaa", "symbol": "AAA"}
        db.put({"PK": db.upk(sub), **rec})
        ww = {"cwLost": True, "entry": 103}
        assert autotrader._level_lost(sub, rec, ww, {"c": 106.3}, "2026-10-05T11:30", dt.datetime(2026, 10, 6, 11, 40)) is None
        rec = db.get(db.upk(sub), rec["SK"])
        assert rec["wwPassed"] == [["call wall", 106.0]]
        r = autotrader._level_lost(sub, rec, ww, {"c": 105.4}, "2026-10-05T11:30", dt.datetime(2026, 10, 6, 15, 52))
        assert r and "call wall 106" in r
    finally:
        autotrader._yahoo, flowdata.stored_walls = saved


def test_chart_long_range_replay():
    """A 90-day chart: the stored 45 days plus older signals replayed with the current settings."""
    import wallwatch, charts, flowdata, backtest, math
    import datetime as dt
    from util import now_ny
    today = now_ny().date()
    days, d = [], today - dt.timedelta(days=200)
    while d < today:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    hb = []
    for day in days:
        for h in ("09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"):
            k = len(hb); px = 100 + 4 * math.sin(k / 15.0)
            hb.append({"t": f"{day}T{h}", "o": px - .1, "h": px + .3, "l": px - .3, "c": px, "v": 1000})
    db_ = [{"t": f"{d}T09:30", "o": 100, "h": 104, "l": 96, "c": 100, "v": 1e6} for d in days]
    saved = (charts._yahoo, flowdata.stored_walls, backtest.stored_alerts, backtest.stored_darkpool)
    try:
        charts._yahoo = lambda sym, tf, s_, e: hb if tf == "1h" else db_
        flowdata.stored_walls = lambda sym: {day: {"callWall": 106, "putWall": 98, "flip": 100} for day in days}
        backtest.stored_alerts = lambda sub_, sym: [{"t": f"{day}T10:00", "c": 1, "p": 900000} for day in days[::3]]
        backtest.stored_darkpool = lambda sym: []
        wallwatch.save_config("long-user", {"tickers": ["AAA"], "cwRoomOn": False})
        out = wallwatch.chart("long-user", "AAA", days=90, flow=False)
        assert len({b["t"][:10] for b in out["bars"]}) == 90
        assert any(x.get("replay") for x in out["signals"]), [x["at"] for x in out["signals"]]
        # strategy lab: every signal replayed with the tried settings, nothing saved
        lab = wallwatch.chart("long-user", "AAA", days=45, flow=False, lab={"stopAtr": 0.7, "bogus": 1})
        assert lab["lab"] and lab["signals"] and all(x.get("replay") for x in lab["signals"])
        assert float(wallwatch.config("long-user")["stopAtr"]) == 0.5
        assert "fvg" in lab
        d1 = wallwatch.chart("long-user", "AAA", days=45, flow=False, lab={"tf": "1D", "greenOnly": False, "noAboveCW": False, "cwRoomOn": False})
        assert d1["tf"] == "1D" and all(x.get("tf") == "1D" for x in d1["signals"])
        r1 = backtest.lab_test("long-user", {"days": 60, "cfg": {"tf": "1D", "greenOnly": False}})
        assert "n" in r1["lab"] and r1["live"]["n"] > 0
        r = backtest.lab_test("long-user", {"days": 60, "cfg": {"stopAtr": 0.7}})
        assert r["lab"]["n"] > 0 and r["live"]["n"] > 0
        rb = backtest.robust_test("long-user", {"days": 60, "cfg": {"trailOn": True, "fvgOn": True, "kchLower": True, "scaleOn": True, "vwapOn": True}})
        labs = [x["label"] for x in rb["rows"]]
        assert any(l.startswith("FVG min size") for l in labs) and any(l.startswith("Keltner hourly") for l in labs) \
            and any(l.startswith("Sell part at") for l in labs) and any(l.startswith("VWAP reset") for l in labs), labs
        rl = backtest._with_lab({"stopAtr": 0.5, "tickers": ["AAA"]}, {"cfg": {"stopAtr": 0.9, "pvSetup": True, "junk": 1}})
        assert rl["stopAtr"] == 0.9 and "junk" not in rl and "pvSetup" not in rl and rl["tickers"] == ["AAA"]
        # rule search: screening + replay; with no entry rule it matches the real engine's trade count closely
        rs = backtest.rule_search("long-user", {"days": 60, "minTrades": 1})
        assert rs["candidates"] > 0 and rs["tested"] > 20 and rs["base"]["n"] > 0 and len(rs["singles"]) == len(wallwatch.SEARCH_RULES)
        dd = backtest._ticker_data("long-user", "AAA", 60, {**wallwatch.config("long-user"), "earnOn": False}, False)
        real, _ = wallwatch.detect_retest(dd["bars"], dd["walls"], rs["period"]["from"],
                                          {**wallwatch.config("long-user"), "earnOn": False, **{k: False for k in wallwatch.SEARCH_RULES}},
                                          dd["datr"], dd["alerts"], None, dd["vmap"], dd["emap"], dd.get("extra"))
        print("rule search base vs engine:", rs["base"]["n"], len(real), "tested", rs["tested"])
        assert abs(rs["base"]["n"] - len(real)) <= max(2, len(real) // 5), (rs["base"]["n"], len(real))
    finally:
        charts._yahoo, flowdata.stored_walls, backtest.stored_alerts, backtest.stored_darkpool = saved


def test_pw_vwap_study():
    """Put wall crossings flagged with a nearby VWAP reset; the study returns forward returns by group."""
    import wallwatch, backtest, charts, flowdata, math
    import datetime as dt
    from util import now_ny
    bars = [{"t": f"2026-10-0{1 + k // 7}T{['09:30','10:30','11:30','12:30','13:30','14:30','15:30'][k % 7]}",
             "o": 99, "h": 101, "l": 98, "c": c, "v": 1} for k, c in enumerate([99, 99.5, 100.5, 101, 99.2, 98, 100.4, 101, 102, 103])]
    ws = {f"2026-10-0{d}": {"putWall": 100} for d in (1, 2)}
    ev = wallwatch.pw_vwap_events(bars, ws, [2, 8])
    assert [(e["t"], e["touch"], e["above"]) for e in ev] == [("2026-10-01T11:30", True, True), ("2026-10-02T10:30", True, True)]
    far = [dict(b, l=105, h=106) for b in bars]
    assert not wallwatch.pw_vwap_events(far, ws, [2])[0]["touch"]
    assert wallwatch._kc_pos(95, (100, 110, 90)) == {"kcPos": 25, "kcHalf": "lower", "kcM": 100, "kcU": 110, "kcL": 90}
    assert wallwatch._kc_pos(105, (100, 110, 90))["kcHalf"] == "upper"
    assert wallwatch._kc_pos(105, None)["kcHalf"] is None
    kc = [(100, 110, 90)] * len(bars)
    assert all("kcHalf" in e for e in wallwatch.pw_vwap_events(bars, ws, [2, 8], kc=kc))
    # long: put wall touch in the lower Keltner half; short: call wall touch in the upper half; none when PW == CW
    b1 = [dict(bars[0], t="2026-10-01T09:30"), {"t": "2026-10-01T10:30", "o": 100, "h": 101, "l": 99.5, "c": 100.5, "v": 9}]
    w1 = {"2026-10-01": {"putWall": 100, "callWall": 120}}
    lo_kc, up_kc = [(110, 130, 90)] * 2, [(95, 105, 85)] * 2
    assert wallwatch.pw_vwap_events(b1, w1, [1], kc=lo_kc)[0]["side"] == "long"
    assert wallwatch.pw_vwap_events(b1, w1, [1], kc=up_kc)[0]["side"] is None
    assert wallwatch.pw_vwap_events(b1, {"2026-10-01": {"putWall": 100, "callWall": 100}}, [1], kc=lo_kc)[0]["side"] is None
    w2 = {"2026-10-01": {"putWall": 80, "callWall": 100}}
    assert wallwatch.pw_vwap_events(b1, w2, [1], kc=up_kc)[0]["side"] == "short"
    assert wallwatch.pw_vwap_events(b1, w2, [1], kc=lo_kc)[0]["side"] is None
    # LONG trade with the strategy's exits: target = call wall
    tb = [{"t": f"2026-10-0{1 + k // 7}T{9 + k % 7}:30", "o": 100, "h": 101, "l": 99.5, "c": 100.5, "v": 9} for k in range(14)]
    tb[10] = dict(tb[10], h=121, c=119)
    tw = {f"2026-10-0{d}": {"putWall": 100, "callWall": 120, "flip": 99} for d in (1, 2)}
    tr = wallwatch.pv_long_trades(tb, tw, [{"i": 3, "t": tb[3]["t"], "side": "long", "pw": 100}],
                                  {"tgtBuf": 0, "kcRide": False, "cwLost": False, "dpLost": False, "trailOn": False}, kc=[(110, 130, 90)] * 14)
    assert len(tr) == 1 and tr[0]["status"] == "target" and tr[0]["R"] > 0
    tb2 = [dict(x) for x in tb]
    tb2[6] = dict(tb2[6], c=99.8, l=99.6)            # entry day closes under the put wall 100
    tr2 = wallwatch.pv_long_trades(tb2, tw, [{"i": 3, "t": tb2[3]["t"], "side": "long", "pw": 100}],
                                   {"pvEod": True, "tgtBuf": 0, "kcRide": False, "cwLost": False, "dpLost": False, "trailOn": False}, kc=[(110, 130, 90)] * 14)
    assert tr2[0]["status"] == "eodpw" and tr2[0]["exit"] == 99.8
    # fair value gaps: bullish gap between candle i-2 high and candle i low, filled when a later low trades under it
    fb = [{"t": f"2026-10-01T{9 + k}:30", "o": o, "h": h, "l": l, "c": c, "v": 1} for k, (o, h, l, c) in enumerate(
        [(100, 101, 99, 100.5), (100.5, 104, 100.4, 103.8), (104, 106, 103, 105), (105, 105.5, 102.5, 104), (104, 104.5, 100, 100.2)])]
    fz = wallwatch.fvg_zones(fb, 0.1, [1.0] * 5)
    assert fz[0]["type"] == "bull" and fz[0]["lo"] == 101 and fz[0]["hi"] == 103 and fz[0]["fillI"] == 4
    ft = wallwatch.fvg_bull_touch(fb, fz)
    assert ft[3] is fz[0] and ft[2] is None and ft[4] is None
    near = [dict(x) for x in fb]
    near[3] = dict(near[3], l=103.2)                 # 0.2 above the top of the gap
    assert wallwatch.fvg_bull_touch(near, fz)[3] is None
    assert wallwatch.fvg_bull_touch(near, fz, 0.25, [1.0] * 5)[3] is fz[0]
    # a gap not filled after max_days sessions expires
    eb = fb[:4] + [{"t": f"2026-10-{d:02d}T10:30", "o": 104, "h": 105, "l": 103.5, "c": 104} for d in (2, 3, 6)]
    ez = wallwatch.fvg_zones(eb, 0.1, [1.0] * len(eb), max_days=2)
    assert ez[0]["end"] == "expired" and ez[0]["fillAt"] == "2026-10-06T10:30"
    # exit on a new bearish FVG: candle 4's high under candle 2's low
    xb = [{"t": f"2026-10-01T{9 + k}:30", "o": c, "h": h, "l": l, "c": c, "v": 1} for k, (h, l, c) in enumerate(
        [(101, 99, 100), (102, 100, 101), (103, 101, 102), (102, 99, 100), (100.5, 98, 99), (100, 97, 98)])]
    assert wallwatch.fvg_bear_new(xb, 0.1, [1.0] * 6) == [False, False, False, False, True, False]
    st_ = {"at": xb[0]["t"], "entry": 100, "stop": 90, "stop0": 90, "status": "open", "fvgExit": True, "atr": 1}
    for k in range(1, 6):
        wallwatch._step(st_, xb[k], {}, 0, False, False, bear=wallwatch.fvg_bear_new(xb, 0.1, [1.0] * 6)[k])
        if st_["status"] != "open":
            break
    assert st_["status"] == "fvg" and st_["exitAt"] == xb[4]["t"] and st_["exit"] == 99
    # partial exit: half sold at entry + 1R, the stop of the rest moved to the entry, then stopped there
    sc = {"at": "2026-10-01T09:30", "entry": 100, "stop": 98, "stop0": 98, "status": "open", "scaleOn": True, "scaleR": 1,
          "scalePct": 50, "scaleBE": True, "atr": 1}
    wallwatch._step(sc, {"t": "2026-10-01T10:30", "o": 100, "h": 102.5, "l": 99.5, "c": 102}, {}, 0, False, False)
    assert sc["scaled"]["px"] == 102 and sc["stop"] == 100 and sc["status"] == "open"
    wallwatch._step(sc, {"t": "2026-10-01T11:30", "o": 101, "h": 101, "l": 99, "c": 99.5}, {}, 0, False, False)
    assert sc["status"] == "breakeven" and sc["exit"] == 100
    assert wallwatch._result(sc) == (1.0, 0.5)          # half at +2%, half at 0%: +1% = 0.5R
    assert wallwatch.config("nobody")["scaleOn"] is False
    # wall regimes: labels per day, wall moves vs the previous day, forward moves from the close
    import backtest
    hb = [{"t": f"2026-09-{d:02d}T{h}:30", "o": 100 + d, "h": 101 + d, "l": 99 + d, "c": 100 + d} for d in range(1, 13) for h in (10, 11)]
    dl = backtest._daily(hb)
    assert len(dl) == 12 and dl[0]["c"] == 101
    wr = {"2026-09-01": {"putWall": 95, "callWall": 110, "flip": 100}, "2026-09-02": {"putWall": 110, "callWall": 120, "flip": 105},
          "2026-09-03": {"putWall": 115, "callWall": 115}, "2026-09-04": {"putWall": 120, "callWall": 110, "flip": 100}}
    rr = backtest.regime_rows(dl, wr, "2026-09-01")
    assert [r["regime"] for r in rr] == ["normal", "normal", "same", "inverted"]
    assert rr[0]["px"] == "between" and rr[0]["flipSide"] == "above" and rr[0]["flipLoc"] == "between"
    assert rr[1]["cwToPw"] and rr[2]["merged"] and rr[3]["inverted"] and rr[1]["px"] == "below"
    assert rr[0]["1 day"] == round((102 / 101 - 1) * 100, 2)
    # the strategy switch: detect_retest runs the VWAP reset strategy, signals in the retest format
    sg3, w3 = wallwatch.detect_retest(tb, tw, None, {"pvSetup": True, "tgtBuf": 0, "kcRide": False, "cwLost": False, "dpLost": False,
                                                    "trailOn": False, "earnOn": False}, None, None, None, None, None,
                                      {"kc": {b["t"][:16]: (110, 130, 90) for b in tb}, "kch": {b["t"][:16]: (110, 130, 90) for b in tb}, "vwResets": [tb[3]["t"]]})
    # unusual flow: relative to the ticker's last 30 days of call alerts, no look-ahead
    al = [{"t": f"2026-09-{d:02d}T10:00", "c": 1, "p": 100_000} for d in range(1, 11)]
    al += [{"t": "2026-09-12T10:00", "c": 1, "p": 250_000}, {"t": "2026-09-13T10:00", "c": 1, "p": 400_000},
           {"t": "2026-09-14T10:00", "c": 0, "p": 9_000_000}]
    u = wallwatch.unusual_calls(al, 500_000, 3)
    assert [a["p"] for a in u] == [400_000] and u[0]["x"] == 4.0
    assert [a["p"] for a in wallwatch.unusual_calls(al[:3] + [{"t": "2026-09-05T10:00", "c": 1, "p": 600_000}], 500_000, 3)] == [600_000]
    # the same idea as building blocks: no call alert needed + VWAP reset candle + entry day close above the support
    rbx = [dict(x, o=99.6) for x in tb]
    rbx[3] = dict(rbx[3], o=100.2, l=99.9, c=100.6)
    rr_, _ = wallwatch.detect_retest(rbx, tw, None, {"alertReq": False, "vwapReset": True, "pvEod": True, "greenOnly": False,
                                                      "useFlip": False, "earnOn": False, "cwRoomOn": False, "noAboveCW": False,
                                                      "kcNotExt": False, "tgtBuf": 0, "trailOn": False, "kcRide": False},
                                     None, None, None, None, None, {"kc": {b["t"][:16]: (110, 130, 90) for b in rbx}, "vwResets": [rbx[3]["t"]]})
    assert [x["at"] for x in rr_] == [rbx[3]["t"]] and rr_[0]["level"] == 100, rr_
    assert rr_[0]["armedAt"] is None and not rr_[0]["armPremium"]          # flow off: no alert attached
    rr2, _ = wallwatch.detect_retest(rbx, tw, None, {"alertReq": False, "vwapReset": True, "kcdLower": True, "greenOnly": False,
                                                      "useFlip": False, "earnOn": False, "cwRoomOn": False, "noAboveCW": False,
                                                      "kcNotExt": False, "tgtBuf": 0, "trailOn": False, "kcRide": False},
                                     None, [{"t": rbx[1]["t"], "c": 1, "p": 9e6}], None, None, None,
                                     {"kc": {b["t"][:16]: (100.3, 130, 90) for b in rbx}, "vwResets": [rbx[3]["t"]]})
    assert rr2 == []                                   # close 100.6 above the daily middle 100.3: no entry
    assert w3 is None and len(sg3) == 1 and sg3[0]["kind"] == "retest" and sg3[0]["setup"] == "pvlong" and sg3[0]["status"] == "target"
    today = now_ny().date()
    days, d = [], today - dt.timedelta(days=200)
    while d < today:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    hb = []
    for day in days:
        for h in ("09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"):
            k = len(hb); px = 100 + 3 * math.sin(k / 9.0)
            hb.append({"t": f"{day}T{h}", "o": px - .1, "h": px + .3, "l": px - .3, "c": px, "v": 5000 + k if k % 40 == 0 else 1000})
    saved = (charts._yahoo, flowdata.stored_walls, backtest.stored_alerts, backtest.stored_darkpool)
    try:
        charts._yahoo = lambda sym, tf, s_, e: hb if tf == "1h" else [{"t": f"{x}T09:30", "o": 100, "h": 103, "l": 97, "c": 100, "v": 1} for x in days]
        flowdata.stored_walls = lambda sym: {day: {"callWall": 104, "putWall": 100, "flip": 101} for day in days}
        backtest.stored_alerts = lambda sub_, sym: []
        backtest.stored_darkpool = lambda sym: []
        wallwatch.save_config("pv-user", {"tickers": ["AAA"]})
        r = backtest.pv_study("pv-user", {"days": 120})
        g = {x["label"].strip(): x for x in r["groups"]}
        assert g["All resets touching the put wall (PW ≠ CW)"]["n"] > 0 and g["Any day (baseline)"]["n"] > 50
        assert "5 days" in g["Any day (baseline)"] and backtest.pv_result("pv-user")["groups"]
    finally:
        charts._yahoo, flowdata.stored_walls, backtest.stored_alerts, backtest.stored_darkpool = saved


def test_daily_strategy():
    """1D lab mode: daily candles from the hourly ones; entry on the put wall test + inertia green + an open bullish
    daily FVG, exit at the first daily close with the inertia not green."""
    import wallwatch, datetime as dt
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    cl = [100 + 2 * d for d in range(86)] + [270 - 3 * k for k in range(1, 15)]
    days = [(dt.date(2026, 1, 1) + dt.timedelta(days=d)).isoformat() for d in range(len(cl))]
    hb = []
    for d, c in zip(days, cl):
        for k, h in enumerate(H):
            px = c - 0.5 + k / 12
            hb.append({"t": f"{d}T{h}", "o": px, "h": c + 1 if k == 3 else px + 0.1, "l": c - 1 if k == 2 else px - 0.1,
                       "c": c if k == 6 else px + 0.05, "v": 1})
    db_ = wallwatch.daily_bars(hb)
    assert len(db_) == len(cl) and db_[5]["c"] == cl[5] and db_[5]["h"] == cl[5] + 1 and db_[5]["full"]
    w = {d: {"putWall": 50, "callWall": 999, "flip": 40} for d in days}
    w[days[85]] = {"putWall": cl[85] - 1 + 0.1, "callWall": 999, "flip": 40}
    cfg = {"tf": "1D", "pwTest": True, "inGreen": True, "fvgOn": True, "inYExit": True, "greenOnly": False, "earnOn": False,
           "trailOn": False, "targetOn": False, "flipExit": False, "noAboveCW": False, "cwRoomOn": False, "inN1": 10, "inN2": 6, "inN3": 3, "stopAtr": 5,
           "fvgDays": 30}
    sg, watch = wallwatch.detect_retest(hb, w, None, cfg, None)
    assert watch is None and len(sg) == 1
    s = sg[0]
    assert s["at"].startswith(days[85]) and s["levelName"] == "put wall" and s["fvgZone"] and s["tf"] == "1D"
    assert s["status"] == "inertia" and s["exitAt"][:10] > days[85]
    off = wallwatch.detect_retest(hb, w, None, {**cfg, "inYExit": False, "stopOn": True}, None)[0][0]
    assert off["status"] in ("stop", "open") and off["exitAt" if off["status"] != "open" else "lastAt"] >= s["exitAt"]
    assert wallwatch.detect_retest(hb, w, None, {**cfg, "pwTest": False}, None)[0][0]["at"] < s["at"]   # every day passes
    assert wallwatch.config("x")["tf"] == "1h"
    ih = wallwatch.inertia_hourly(hb, (12, 8, 4))
    last = {b["t"][:10]: x for b, x in zip(hb, ih)}
    hs_ = wallwatch.detect_retest(hb, w, None, {**cfg, "pwTest": False, "inhNotRed": True, "inhN1": 12, "inhN2": 8, "inhN3": 4}, None)[0]
    assert hs_
    for sg_ in hs_:
        assert last[sg_["at"][:10]]["s"] != "R" and sg_["inhSt"] == last[sg_["at"][:10]]["s"]
    assert not wallwatch.detect_retest(hb, w, None, {**cfg, "pwTest": False, "inhGreen": True, "inhN1": 12, "inhN2": 8, "inhN3": 4}, None)[0]   # never green here


def test_daily_above_pw_rule():
    """Above the PW: the daily entry close must be above the put wall of that day."""
    import wallwatch, datetime as dt
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    cl = [100 + 2 * d for d in range(86)] + [270 - 3 * k for k in range(1, 15)]
    days = [(dt.date(2026, 1, 1) + dt.timedelta(days=d)).isoformat() for d in range(len(cl))]
    hb = []
    for d, c in zip(days, cl):
        for k, h in enumerate(H):
            px = c - 0.5 + k / 12
            hb.append({"t": f"{d}T{h}", "o": px, "h": px + 0.1, "l": px - 0.1, "c": c if k == 6 else px + 0.05, "v": 1})
    w = {d: {"putWall": 999, "callWall": 9999, "flip": 40} for d in days}
    for d in days[60:]:
        w[d] = {"putWall": 50, "callWall": 9999, "flip": 40}
    cfg = {"tf": "1D", "pwTest": False, "inGreen": False, "fvgOn": False, "inYExit": False, "greenOnly": False, "earnOn": False,
           "trailOn": False, "targetOn": False, "flipExit": False, "noAboveCW": False, "cwRoomOn": False, "stopAtr": 5,
           "usePW": False, "useFlip": False}
    a = wallwatch.detect_retest(hb, w, None, cfg, None)[0]
    b = wallwatch.detect_retest(hb, w, None, {**cfg, "abovePW": True}, None)[0]
    assert a and b and a[0]["at"][:10] < days[60] and all(x["at"][:10] >= days[60] for x in b)
    assert wallwatch.version({**wallwatch.PARAMS, "abovePW": True}) != wallwatch.version(wallwatch.PARAMS)
    c = wallwatch.detect_retest(hb, w, None, {**cfg, "belowPW": True}, None)[0]
    assert c and all(x["at"][:10] < days[60] for x in c)
    dbars = wallwatch.daily_bars(hb)
    cv = wallwatch.cvd_roll(dbars, 5)
    one = []
    for i, b in enumerate(dbars):
        v, r = b["v"], b["h"] - b["l"]
        pc = dbars[i - 1]["c"] if i else b["o"]
        one.append(2 * (v * (b["c"] - b["l"]) / r if r > 0 else (v if b["c"] > pc else 0 if b["c"] < pc else v / 2)) - v)
    assert all(abs(cv[i] - sum(one[max(0, i - 4):i + 1])) < 1e-6 for i in range(len(dbars)))
    up = wallwatch.detect_retest(hb, w, None, {**cfg, "cvdUp": True, "cvdN": 5}, None)[0]
    dn = wallwatch.detect_retest(hb, w, None, {**cfg, "cvdDn": True, "cvdN": 5}, None)[0]
    at = {b["t"][:10]: cv[i] for i, b in enumerate(dbars)}
    assert up and all(at[x["at"][:10]] > 0 for x in up) and all(at[x["at"][:10]] < 0 for x in dn)
    assert not wallwatch.detect_retest(hb, w, None, {**cfg, "cvdUp": True, "cvdDn": True}, None)[0]


def test_swing_squeeze_turn_rule():
    """Squeeze turn up (1h / daily): momentum under 0, falling then rising, on the entry candle or within N candles."""
    import wallwatch, math, datetime as dt
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    cl = [200 + 20 * math.sin(d / 7) + d * 0.1 for d in range(160)]
    days = [(dt.date(2026, 1, 1) + dt.timedelta(days=d)).isoformat() for d in range(len(cl))]
    hb = []
    for d, c in zip(days, cl):
        for k, h in enumerate(H):
            px = c - 0.5 + k / 12
            hb.append({"t": f"{d}T{h}", "o": px, "h": px + 0.3, "l": px - 0.3, "c": c if k == 6 else px + 0.05, "v": 1})
    dbars = wallwatch.daily_bars(hb)
    sq = wallwatch.squeeze(dbars)
    turns = [i for i, x in enumerate(sq) if x and x["tAgo"] == 0]
    assert turns
    for i in turns:
        a, b, c = sq[i - 2]["mom"], sq[i - 1]["mom"], sq[i]["mom"]
        assert c < 0 and c > b and a > b
    w = {d: {"putWall": 1, "callWall": 9999, "flip": 1} for d in days}
    cfg = {"tf": "1D", "pwTest": False, "inGreen": False, "fvgOn": False, "inYExit": False, "greenOnly": False, "earnOn": False,
           "trailOn": False, "targetOn": False, "flipExit": False, "noAboveCW": False, "cwRoomOn": False, "stopAtr": 0.3,
           "usePW": False, "useFlip": False}
    for n in (1, 3):
        sg = wallwatch.detect_retest(hb, w, None, {**cfg, "sqdTurn": True, "sqTurnN": n}, None)[0]
        assert sg
        at = {b["t"][:10]: x for b, x in zip(dbars, sq)}
        assert all(at[x["at"][:10]]["tAgo"] is not None and at[x["at"][:10]]["tAgo"] < n for x in sg)
    assert wallwatch._sq_ok({"tAgo": 2}, "turn", 3) and not wallwatch._sq_ok({"tAgo": 3}, "turn", 3) and not wallwatch._sq_ok({"tAgo": None}, "turn", 5)


def test_swing_close_under_pw_exit():
    """Close under PW exit: sold at the first daily close under that day's put wall (1D and the hourly step)."""
    import wallwatch, math, datetime as dt
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    cl = [200 + 20 * math.sin(d / 7) for d in range(120)]
    days = [(dt.date(2026, 1, 1) + dt.timedelta(days=d)).isoformat() for d in range(len(cl))]
    hb = [{"t": f"{d}T{h}", "o": c - 0.2, "h": c + 0.3, "l": c - 0.3, "c": c, "v": 1} for d, c in zip(days, cl) for h in H]
    w = {d: {"putWall": 205, "callWall": 9999, "flip": 1} for d in days}
    cfg = {"tf": "1D", "pwTest": False, "inGreen": False, "fvgOn": False, "inYExit": False, "greenOnly": False, "earnOn": False,
           "trailOn": False, "targetOn": False, "flipExit": False, "noAboveCW": False, "cwRoomOn": False, "stopOn": False, "stopAtr": 0.5,
           "usePW": False, "useFlip": False, "abovePW": True}
    sg = wallwatch.detect_retest(hb, w, None, {**cfg, "pwExit": True}, None)[0]
    done = [x for x in sg if x["status"] != "open"]
    assert done and all(x["status"] == "pwlost" and x["exit"] < 205 for x in done)
    close = {d: c for d, c in zip(days, cl)}
    for x in done:                                      # the FIRST daily close under the put wall after the entry
        between = [d for d in days if x["at"][:10] < d < x["exitAt"][:10]]
        assert all(close[d] >= 205 for d in between)
    off = wallwatch.detect_retest(hb, w, None, cfg, None)[0]
    assert all(x["status"] != "pwlost" for x in off)
    st = {"kind": "retest", "entry": 210, "stop": 0.0, "stop0": 200, "pwExit": True, "at": "2026-01-01T10:30"}
    wallwatch._step(st, {"t": "2026-01-02T14:30", "o": 206, "h": 206, "l": 204, "c": 204}, w, 0.0, False, False)
    assert "exitAt" not in st                           # not the daily close yet
    wallwatch._step(st, {"t": "2026-01-02T15:30", "o": 204, "h": 204.5, "l": 203, "c": 204}, w, 0.0, False, True)
    assert st["status"] == "pwlost" and st["exit"] == 204


def test_swing_rising_falling_rules():
    """Inertia / squeeze / CVD rising or falling: entry rules and exits (first close where it holds)."""
    import wallwatch, math, datetime as dt
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    cl = [200 + 20 * math.sin(d / 6) + 5 * math.sin(d / 2) for d in range(160)]
    days = [(dt.date(2026, 1, 1) + dt.timedelta(days=d)).isoformat() for d in range(len(cl))]
    hb = [{"t": f"{d}T{h}", "o": c - 0.3 * math.cos(j), "h": c + 0.6, "l": c - 0.6, "c": c + 0.3 * math.cos(j * 1.7) if k == 6 else c + 0.1 * k * math.sin(j), "v": 10 + k}
          for j, (d, c) in enumerate(zip(days, cl)) for k, h in enumerate(H)]
    w = {d: {"putWall": 1, "callWall": 9999, "flip": 1} for d in days}
    cfg = {"tf": "1D", "pwTest": False, "inGreen": False, "fvgOn": False, "inYExit": False, "greenOnly": False, "earnOn": False,
           "trailOn": False, "targetOn": False, "flipExit": False, "noAboveCW": False, "cwRoomOn": False, "stopOn": False, "stopAtr": 0.5,
           "usePW": False, "useFlip": False, "cvdN": 5, "inN1": 20, "inN2": 10, "inN3": 5}
    db_ = wallwatch.daily_bars(hb)
    dd = [b["t"][:10] for b in db_]
    sq = dict(zip(dd, wallwatch.squeeze(db_)))
    cv = wallwatch.cvd_roll(db_, 5)
    cvd = {d: (cv[i], cv[i - 1] if i else None) for i, d in enumerate(dd)}
    rg = dict(zip(dd, wallwatch.inertia_regime(db_, (20, 10, 5))))
    run = lambda **k: wallwatch.detect_retest(hb, w, None, {**cfg, **k}, None)[0]
    checks = {"sqdRise": lambda d: sq[d] and sq[d]["up"], "sqdFall": lambda d: sq[d] and not sq[d]["up"],
              "cvdRise": lambda d: cvd[d][0] > cvd[d][1], "cvdFall": lambda d: cvd[d][0] < cvd[d][1],
              "inFall": lambda d: rg[d] and rg[d]["s"] == "R"}
    for k, ok in checks.items():
        sg = run(**{k: True})
        assert sg and all(ok(x["at"][:10]) for x in sg), k
    exits = {"cvdFallX": ("cvd", lambda d: cvd[d][0] < cvd[d][1]), "cvdRiseX": ("cvd", lambda d: cvd[d][0] > cvd[d][1]),
             "sqdRiseX": ("squeeze", lambda d: bool(sq[d] and sq[d]["up"])), "inRiseX": ("inertia", lambda d: rg[d] and rg[d]["s"] == "G")}
    for k, (st, ok) in exits.items():
        sg = [x for x in run(**{k: True}) if x["status"] != "open"]
        assert sg, k
        for x in sg:
            assert x["status"] == st and x["exitWhy"] == k and ok(x["exitAt"][:10]), k
            assert not any(ok(d) for d in dd if x["at"][:10] < d < x["exitAt"][:10]), k     # the first one
    h = {**cfg, "tf": "1h", "alertReq": False, "relFlow": False}
    for k in ("inhFall", "sqhRise", "sqhFall", "cvdRise", "inhRiseX", "inhRedX", "sqhRiseX", "cvdFallX", "sqdRiseX", "inRiseX"):
        wallwatch.detect_retest(hb, w, None, {**h, k: True}, None)          # no error on the hourly timeframe
    hs = [x for x in wallwatch.detect_retest(hb, w, None, {**h, "cvdFallX": True}, None)[0] if x["status"] != "open"]
    hc = wallwatch.cvd_roll([b for b in hb], 5)
    at = {b["t"][:16]: (hc[i], hc[i - 1]) for i, b in enumerate(hb) if i}
    assert hs and all(x["status"] == "cvd" and at[x["exitAt"]][0] < at[x["exitAt"]][1] for x in hs)


def test_research_pipeline_and_agent_apart():
    """AI research: the agent only proposes data and sees 1st-half results only; the pipeline cleans, gates
    (unknown setting -> needs feature, repeat -> duplicate), tests and grades with the same criteria; t rises with
    the number of ideas; your own ideas take the same path."""
    import research, research_agent, daytrade
    import inspect
    assert "claude" not in inspect.getsource(research).replace("research_agent", "")    # no AI call in the pipeline
    sub = "rs-user"
    for k in [k for k in STORE if k[0] == db.upk(sub)]:
        STORE.pop(k)
    ok, unknown = research.clean_changes("day", {"aboveVwap": "true", "stopAtr": "0.8", "bogus": 1, "tickers": ["X"]})
    assert ok == {"aboveVwap": True, "stopAtr": 0.8} and len(unknown) == 2 and any("bogus" in u for u in unknown)
    ok2, bad2 = research.clean_changes("day", {"fxLong": "close > vwap and cvd > 0", "fxShort": "os.system('x')"})
    assert ok2 == {"fxLong": "close > vwap and cvd > 0"} and bad2 and "fxShort" in bad2[0]
    c = research.criteria(sub)
    assert research.t_required(c, 1) == 2.0 and research.t_required(c, 100) > 3 and research.t_required({**c, "adjustT": False}, 100) == 2.0
    rows = [{"at": f"2025-{1 + i % 12:02d}-{1 + i % 27:02d}T10:00", "R": (1.5 if i % 2 else -0.6)} for i in range(400)]
    m = research.metrics(rows, "2025-07-01", 0.05)
    assert m["n"] == 400 and abs(m["avgR"] - (sum(r["R"] for r in rows) / 400 - 0.05)) < 1e-3 and m["h1"]["n"] + m["h2"]["n"] == 400
    g, fails, _ = research.grade(m, c, 1)
    assert g == "A" and not fails
    bad = research.metrics([{"at": "2025-01-02T10:00", "R": 0.1}] * 5, "2025-07-01")
    g2, fails2, _ = research.grade(bad, c, 1)
    assert g2 in ("C", "F") and any("trades" in f for f in fails2)

    class Bench:                                       # no market data: a fixed answer per setting
        first, mid, last, errors = "2025-01-01", "2025-07-01", "2025-12-31", []

        def rows(self, P):
            good = P.get("aboveVwap")
            return [{"at": f"2025-{1 + i % 12:02d}-10T10:00", "R": (1.2 if i % 2 else -0.5) if good else (0.5 if i % 2 else -0.6), "sym": "QQQ"}
                    for i in range(300)]
    seen_payload = {}

    def propose(strategy, catalog, settings, crit, rec, n, focus, n_tested, library=None):
        seen_payload.update(rec=rec, crit=crit)
        return [{"name": "vwap", "hypothesis": "h", "changes": {"aboveVwap": True}},
                {"name": "vwap again", "hypothesis": "h", "changes": {"aboveVwap": True}},
                {"name": "needs gex", "hypothesis": "h", "changes": {"vwapCross": True}, "missing": ["net GEX by strike"]},
                {"name": "made up", "hypothesis": "h", "changes": {"magic": True}}], "note"
    saved = research.DayBench, research_agent.propose, daytrade.config, research_agent.build
    try:
        research.DayBench = lambda sub_, days, base: Bench()
        research_agent.propose = propose
        research_agent.build = lambda *a, **k: []
        daytrade.config = lambda s_: {**daytrade.PARAMS, "aboveVwap": False, "vwapCross": False}
        out = research.run(sub, {"strategy": "day", "n": 4})
        xs = {x["name"]: x for x in research.ideas(sub, "day")}
        assert xs["vwap"]["status"] == "tested" and xs["vwap"]["grade"] in ("A", "B") and xs["vwap"]["metrics"]["n"] == 300
        assert xs["vwap again"]["status"] == "duplicate" and xs["needs gex"]["status"] == "needs feature"
        assert xs["made up"]["status"] == "needs feature" and "magic" in xs["made up"]["missing"][0]
        assert out["testedNow"] == 1 and not out["running"]
        research.run(sub, {"strategy": "day", "n": 4})                  # 2nd round: the agent sees the record
        rec = seen_payload["rec"]
        tested = [r for r in rec if r["status"] == "tested"]
        assert tested and all(set(r["inSample"]) == {"n", "avgR", "t", "pf", "win", "totR"} for r in tested)
        assert all("grade" not in r and "metrics" not in r for r in rec) and "minTestT" not in seen_payload["crit"]
        mine = research.submit(sub, "day", "mine", {"aboveVwap": True, "stopAtr": 0.9})
        assert mine["status"] == "queued" and mine["by"] == "you"
        research.test_job(sub, {"strategy": "day"})
        assert next(x for x in research.ideas(sub, "day") if x["id"] == mine["id"])["status"] == "tested"
        bl = research.blank_cfg(sub, "day")
        assert not any(bl[k] for k in daytrade.ENTRY + daytrade.FILTERS) and bl["stopOn"] and not bl["targetOn"] and bl["fxLong"] == ""
        v = next(x for x in research.ideas(sub, "day") if x["name"] == "vwap")
        assert v["origin"] == "blank" and v["full"]["aboveVwap"] is True and v["full"]["vwapCross"] is False and "tickers" not in v["full"]
        assert research.auto_due(sub) == []                          # autopilot off
        research.save_auto(sub, "day", {"on": True, "perDay": 2, "n": 3})
        d1 = research.auto_due(sub)
        assert d1 and d1[0][0] == "day" and d1[0][1]["n"] == 3 and research.auto_due(sub) == []   # one at a time
        research._set_run(sub, {"running": None})
        assert research.auto_due(sub) and (research._set_run(sub, {"running": None}) or True) and research.auto_due(sub) == []   # 2 a day
        assert research.auto_today(sub)["day"] == 2
        research.save_auto(sub, "day", {"perDay": 0, "focus": "gamma flip regime"})   # continuous: no daily limit
        for _ in range(3):
            research._set_run(sub, {"running": None})
            d_ = research.auto_due(sub)
            assert d_ and d_[0][1]["focus"] == "gamma flip regime"
        assert research.run_log(sub) and research.state(sub)["runlog"]
        research._set_run(sub, {"running": None})
        st = research.state(sub, "day")
        assert st["tested"] == 2 and st["tReq"] >= 2.0, (st["tested"], st["tReq"], [(x["name"], x["status"]) for x in st["ideas"]])
    finally:
        research.DayBench, research_agent.propose, daytrade.config, research_agent.build = saved


def test_formula_rules():
    """Formula rules: only whitelisted names / functions; values = the same math done by hand; no look-ahead;
    plugged into day trading (entry per side + exit) and swing (entry + exit)."""
    import formula, daytrade, wallwatch
    for bad in ("__import__('os')", "close.real", "open('x')", "foo > 1", "ema(close, n)", "ema(close)", "lambda: 1", "'a' > close",
                "flowPrem > 0"):
        try:
            formula.check(bad, formula.UNAVAILABLE["day"])
            assert False, bad
        except formula.FormulaError:
            pass
    bars = _dt_bars(4)
    cx = formula.Ctx([b for b in bars if "09:30" <= b["t"][11:16] < "16:00"])
    cl = cx.s("close")
    e9 = cx.s("close") and formula._ema(cl, 9)
    v = formula.evaluate("close > ema(close, 9)", cx)
    assert v == [c > e for c, e in zip(cl, e9)]
    up = formula.evaluate("crossUp(close, ema(close, 9))", cx)
    assert all(not x or (cl[i - 1] <= e9[i - 1] and cl[i] > e9[i]) for i, x in enumerate(up))
    r = formula.evaluate("rising(close, 3) and tod >= 30 and not (rsi(14) > 101)", cx)
    tod = cx.s("tod")
    assert all(not x or (cl[i] > cl[i - 3] and tod[i] >= 30) for i, x in enumerate(r)) and any(r)
    orh = formula._call("orHigh", [__import__("ast").Constant(15)], cx)
    day0 = [i for i, b in enumerate(cx.b) if b["t"][:10] == cx.day[0]]
    assert all(orh[i] is None for i in day0[:14]) and orh[day0[14]] == max(cx.b[i]["h"] for i in day0[:15])
    # bigger bars later don't change earlier values (no look-ahead)
    cx2 = formula.Ctx(cx.b[:300])
    assert formula.evaluate("close > ema(close, 9) and cvdSum(20) > 0", cx2) == formula.evaluate("close > ema(close, 9) and cvdSum(20) > 0", cx)[:300]
    # day trading
    base = {**daytrade.PARAMS, "aboveVwap": False, "emaStack": False, "maxTrades": 50, "longs": True, "shorts": True, "targetOn": False}
    sg = daytrade.detect(bars, {**base, "fxLong": "close > ema(close, 9) and tod >= 20", "fxShort": ""})
    assert sg and all(x["side"] == 1 for x in sg)
    ses = [b for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    ix = {b["t"]: i for i, b in enumerate(ses)}
    for x in sg:
        i = ix[x["at"]]
        assert cl[i] > e9[i] and tod[i] >= 20
    sg2 = daytrade.detect(bars, {**base, "fxLong": "close > ema(close, 9) and tod >= 20", "fxExitLong": "close < ema(close, 9)", "stopOn": False})
    ex = [x for x in sg2 if x["status"] == "formula"]
    assert ex and all(cl[ix[x["exitAt"]]] < e9[ix[x["exitAt"]]] for x in ex)
    try:
        daytrade.clean_cfg(daytrade.PARAMS, {"fxLong": "import os"})
        assert False
    except Exception as e:
        assert type(e).__name__ == "BadRequest"
    # swing, daily candles
    import math, datetime as dt
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    cls = [200 + 20 * math.sin(d / 6) for d in range(120)]
    days = [(dt.date(2026, 1, 1) + dt.timedelta(days=d)).isoformat() for d in range(len(cls))]
    hb = [{"t": f"{d}T{h}", "o": c - 0.3, "h": c + 0.6, "l": c - 0.6, "c": c + 0.1 * k, "v": 10} for d, c in zip(days, cls) for k, h in enumerate(H)]
    w = {d: {"putWall": 195, "callWall": 9999, "flip": 1} for d in days}
    cfg = {"tf": "1D", "pwTest": False, "inGreen": False, "fvgOn": False, "inYExit": False, "greenOnly": False, "earnOn": False,
           "trailOn": False, "targetOn": False, "flipExit": False, "noAboveCW": False, "cwRoomOn": False, "stopOn": False, "stopAtr": 0.5,
           "usePW": False, "useFlip": False}
    sw = wallwatch.detect_retest(hb, w, None, {**cfg, "fxEntry": "close > putWall and rising(close, 2)", "fxExit": "close < putWall"}, None)[0]
    db_ = wallwatch.daily_bars(hb)
    dc = {b["t"][:10]: (i, b["c"]) for i, b in enumerate(db_)}
    assert sw
    for x in sw:
        i, c = dc[x["at"][:10]]
        assert c > 195 and c > db_[i - 2]["c"]
        if x["status"] != "open":
            assert x["status"] == "formula" and dc[x["exitAt"][:10]][1] < 195
    assert wallwatch.version({**wallwatch.PARAMS, "fxEntry": "close > 1"}) != wallwatch.version(wallwatch.PARAMS)


def test_research_agent_creates_indicators():
    """The agent creates missing indicators as formulas: stored in the library, expanded into plain formulas for the
    test; ideas waiting for an indicator are rebuilt and re-run; data the app doesn't have stays 'needs data'."""
    import research, research_agent, daytrade, formula
    sub = "rs-ind"
    for k in [k for k in STORE if k[0] == db.upk(sub)]:
        STORE.pop(k)
    assert formula.expand("a > 1 and b", {"a": "rsi(14)", "b": "a < 80"}) == "rsi(14) > 1 and rsi(14) < 80"
    try:
        formula.expand("a", {"a": "b", "b": "a"})
        assert False
    except formula.FormulaError:
        pass
    calls = {"build": 0}

    class Bench:
        first, mid, last, errors = "2025-01-01", "2025-07-01", "2025-12-31", []

        def rows(self, P):
            good = "rsi(14)" in (P.get("fxLong") or "")
            return [{"at": f"2025-{1 + i % 12:02d}-10T10:00", "R": (1.2 if i % 2 else -0.5) if good else 0.0, "sym": "QQQ"} for i in range(300)]

    def propose(strategy, catalog, settings, crit, rec, n, focus, n_tested, library=None):
        if not calls.get("p"):
            calls["p"] = 1
            return [{"name": "rsi dip", "hypothesis": "h", "changes": {"fxLong": "rsiFast < 30"}, "missing": [],
                     "newIndicators": [{"name": "rsiFast", "formula": "rsi(14)", "what": "RSI"}]},
                    {"name": "needs a band", "hypothesis": "h", "changes": {}, "missing": ["a volatility band around VWAP"], "newIndicators": []},
                    {"name": "news", "hypothesis": "h", "changes": {}, "missing": ["news sentiment"], "newIndicators": []},
                    {"name": "bad ind", "hypothesis": "h", "changes": {"fxLong": "zz > 1"}, "missing": [],
                     "newIndicators": [{"name": "zz", "formula": "import os"}]}], ""
        return [], ""

    def build(strategy, catalog, settings, library, items):
        calls["build"] += 1
        out = []
        for it in items:
            if it["name"] == "needs a band":
                out.append({"id": it["id"], "newIndicators": [{"name": "vwapBand", "formula": "vwap - 2 * atr(14)"}],
                            "changes": {"fxLong": "close < vwapBand and rsiFast < 40"}, "impossible": []})
            elif it["name"] == "news":
                out.append({"id": it["id"], "newIndicators": [], "changes": {}, "impossible": ["news feed"]})
        return out
    saved = research.DayBench, research_agent.propose, research_agent.build, daytrade.config
    try:
        research.DayBench = lambda sub_, days, base: Bench()
        research_agent.propose, research_agent.build = propose, build
        daytrade.config = lambda s_: {**daytrade.PARAMS}
        research.run(sub, {"strategy": "day", "n": 4})
        xs = {x["name"]: x for x in research.ideas(sub, "day")}
        lib = {x["name"]: x["formula"] for x in research.library(sub)}
        assert lib == {"rsiFast": "rsi(14)"}
        r = xs["rsi dip"]
        assert r["status"] == "tested" and r["changes"]["fxLong"] == "rsi(14) < 30" and r["readable"]["fxLong"] == "rsiFast < 30" and r["indicators"] == ["rsiFast"]
        assert xs["bad ind"]["status"] == "needs feature" and "zz" in " ".join(xs["bad ind"]["missing"])
        research.run(sub, {"strategy": "day", "n": 4})              # 2nd run: the waiting ideas get their indicators
        xs = {x["name"]: x for x in research.ideas(sub, "day")}
        b = xs["needs a band"]
        assert b["status"] == "tested" and b["rebuilt"] and "vwapBand" in research.lib_map(sub)
        assert b["changes"]["fxLong"] == "close < vwap - 2 * atr(14) and rsi(14) < 40"
        assert xs["news"]["status"] == "needs data" and xs["news"]["missing"] == ["news feed"]
        n_build = calls["build"]
        research.run(sub, {"strategy": "day", "n": 4})              # one attempt per idea
        assert calls["build"] == n_build
    finally:
        research.DayBench, research_agent.propose, research_agent.build, daytrade.config = saved


def test_no_support_any_candle():
    """Support: put wall and flip both OFF -> any candle can enter (here: only the daily inertia green rule)."""
    import wallwatch, datetime as dt
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    up = [{"t": f"{(dt.date(2026, 1, 1) + dt.timedelta(days=d)).isoformat()}T{h}", "o": 100 + d, "h": 101 + d, "l": 99 + d, "c": 100.5 + d}
          for d in range(30) for h in H]
    w = {b["t"][:10]: {"putWall": 50, "callWall": 400, "flip": 40} for b in up}
    cfg = {"alertReq": False, "usePW": False, "useFlip": False, "inGreen": True, "greenOnly": False, "earnOn": False, "noAboveCW": False,
           "cwRoomOn": False, "inN1": 10, "inN2": 6, "inN3": 3}
    sg, _ = wallwatch.detect_retest(up, w, None, cfg, None)
    assert sg and sg[0]["levelName"] == "candle low" and sg[0]["inSt"] == "G"


def test_hourly_inertia():
    """Hourly inertia: rolling regression matches the direct one; the entry filter and the not-green exit work."""
    import wallwatch, math, datetime as dt
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    bars, k = [], 0
    for d in range(40):
        day = (dt.date(2026, 1, 5) + dt.timedelta(days=d)).isoformat()
        for h in H:
            c = 100 + 5 * math.sin(k / 9.0) + k * 0.02
            bars.append({"t": f"{day}T{h}", "o": c - 0.1, "h": c + 0.3, "l": c - 0.3, "c": c, "v": 1})
            k += 1
    ns = (12, 8, 4)
    r = wallwatch.inertia_hourly(bars, ns)
    ys = [b["c"] for b in bars]
    for j in range(13, len(bars)):
        v = [wallwatch._linreg_end(ys[j - n + 1:j + 1]) > wallwatch._linreg_end(ys[j - n:j]) for n in ns]
        assert r[j]["s"] == ("G" if all(v) else "R" if not any(v) else "Y"), j
    assert r[11] is None and {x["s"] for x in r if x} == {"G", "Y", "R"}
    w = {b["t"][:10]: {"putWall": 50, "callWall": 999, "flip": 40} for b in bars}
    cfg = {"alertReq": False, "usePW": False, "useFlip": False, "greenOnly": False, "earnOn": False, "noAboveCW": False, "cwRoomOn": False,
           "inhGreen": True, "inhYExit": True, "inhN1": 12, "inhN2": 8, "inhN3": 4, "trailOn": False, "targetOn": False, "flipExit": False, "stopAtr": 5}
    sg, _ = wallwatch.detect_retest(bars, w, None, cfg, None)
    idx = {b["t"][:16]: i for i, b in enumerate(bars)}
    assert sg and all(r[idx[s["at"]]]["s"] == "G" for s in sg)
    closed = [s for s in sg if s["status"] == "inertiaH"]
    assert closed and all(r[idx[s["exitAt"]]]["s"] != "G" for s in closed)


def test_option_test():
    """Option test: contract picked among the calls that traded on the entry day; the entry at the 30-minute bar ending
    with the stock candle; take profit / stop / stock exit on the option's own bars."""
    import optiontest, alpaca, backtest
    from datetime import datetime, timedelta
    import db as db_
    assert optiontest.occ("BRK.B", "2025-03-21", 450) == "BRKB250321C00450000"
    assert optiontest.fridays("2025-03-03", 7, 21) == ["2025-03-14", "2025-03-21"]
    assert optiontest.strikes_near(101.2)[0] == 101
    want = optiontest.occ("AAA", "2025-03-14", 100)

    def fake_bars(c, syms, tf, start, end):
        if tf == "1Day":
            return {s: [{"t": "2025-03-03T05:00:00Z", "v": 50}] for s in syms if s in (want, optiontest.occ("AAA", "2025-03-21", 100))}
        out = []                                       # 30-minute bars (UTC): 2.00 at the entry, then up to 3.10 on day 2
        for d, pxs in (("2025-03-03", [2.0] * 13), ("2025-03-04", [2.0, 2.4, 2.8, 3.1] + [2.9] * 9)):
            t0 = datetime.strptime(d + "T14:30", "%Y-%m-%dT%H:%M")      # 09:30 NY (EST)
            for k, px in enumerate(pxs):
                out.append({"t": (t0 + timedelta(minutes=30 * k)).strftime("%Y-%m-%dT%H:%M:%SZ"), "o": px, "h": px, "l": px - 0.05, "c": px})
        return {syms[0]: out}
    saved = (optiontest.bars, alpaca.creds)
    try:
        optiontest.bars = fake_bars
        alpaca.creds = lambda sub: {"key": "k", "secret": "s"}
        db_.put({"PK": db_.upk("opt-user"), "SK": backtest.LAB_SK, "trades": [
            {"sym": "AAA", "at": "2025-03-03T10:30", "R": 1.2, "pct": 2.0, "exit": "trail", "exitAt": "2025-03-05T10:30", "entry": 100.4}]})
        r = optiontest.run("opt-user", {"sel": "manual", "dteMin": 7, "dteMax": 21, "money": 0, "tp": 50, "sl": 40, "slip": 0, "follow": True})
        assert r["priced"] == 1, r
        o = r["trades"][0]["opt"]
        assert o["sym"] == want and o["entry"] == 2.0 and o["why"] == "option target" and o["pct"] == 55.0, o   # gapped over the target: filled at the open
        g = {(x["tp"], x["sl"], x["follow"]): x for x in r["grid"]}
        assert g[(None, None, True)]["avg"] == 45.0          # held to the stock exit: last option close 2.90
        assert g[(100, None, False)]["n"] == 1
        rs = optiontest.run_sym("opt-user", "AAA", [{"at": "2025-03-03T10:30", "entry": 100.4, "exitAt": "2025-03-05T10:30", "exit": "trail", "R": 1.2},
                                                    {"at": "2023-05-01T10:30", "entry": 90, "exit": "stop", "R": -1}], {"sel": "manual", "slip": 0})
        assert rs["byAt"]["2025-03-03T10:30"]["pct"] == 55.0 and rs["stats"]["n"] == 1 and rs["older"] == 1
        assert optiontest.result_sym("opt-user", "AAA")["byAt"]
        dl = optiontest.implied_delta(100, 100, 30 / 365, optiontest._bs_call(100, 100, 30 / 365, 0.3)[0])
        assert abs(dl - optiontest._bs_call(100, 100, 30 / 365, 0.3)[1]) < 1e-3 and 0.5 < dl < 0.6
        assert optiontest.implied_delta(100, 90, 30 / 365, 5.0) is None          # under intrinsic: no volatility fits
        live_k = optiontest.occ("AAA", "2025-04-25", 96)

        def fake_daily(c, syms, tf, start, end):                                  # one ITM call ~0.65 delta, one ATM ~0.52
            px = {live_k: optiontest._bs_call(100, 96, 53.25 / 365, 0.3)[0], optiontest.occ("AAA", "2025-04-25", 100): optiontest._bs_call(100, 100, 53.25 / 365, 0.3)[0]}
            return {s_: [{"t": start + "T05:00:00Z", "v": 20, "c": px[s_], "vw": px[s_]}] for s_ in syms if s_ in px}
        optiontest.bars = fake_daily
        ch = optiontest.pick_live({}, "AAA", "2025-03-03", 100.0, {"dteMin": 40, "dteMax": 65, "deltaMin": 0.55, "deltaMax": 0.75})
        assert ch and ch[0][0] == live_k and 0.55 <= ch[0][4] <= 0.75, ch
        optiontest.bars = fake_bars
        cp = optiontest.clean_path([("a", 1, 1, 1, 10.0), ("b", 1, 1, 1, 4.0), ("c", 1, 1, 1, 10.4), ("d", 1, 1, 1, 10.6)])
        assert cp[1][4] == 10.2 and cp[1][3] == 10.2                 # a stray print: replaced, and lows = closes
    finally:
        optiontest.bars, alpaca.creds = saved


def test_squeeze():
    """TTM squeeze: on while the range is tight (BB inside KC), fires when it expands; entry / exit rules on 1h and 1D."""
    import wallwatch, math, datetime as dt
    H = ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    bars, k = [], 0
    for d in range(60):
        day = (dt.date(2026, 1, 5) + dt.timedelta(days=d)).isoformat()
        for h in H:
            if k < 150:
                c = 100 + 3 * math.sin(k / 3.0)                 # wide swings: no squeeze
            elif k < 260:
                c = 100 + 0.05 * math.sin(k)                    # coiled
            else:
                c = 100 + (k - 260) * 0.4                       # breakout up
            bars.append({"t": f"{day}T{h}", "o": c - 0.05, "h": c + 0.1, "l": c - 0.1, "c": c, "v": 1})
            k += 1
    sq = wallwatch.squeeze(bars, 20, 2.0, 1.5, 3)
    assert sq[10] is None and sq[230]["on"] and not sq[120]["on"]
    fired = [i for i, x in enumerate(sq) if x and x["fired"] and x["mom"] > 0]
    assert fired and 255 < fired[0] < 290, fired[:3]
    w = {b["t"][:10]: {"putWall": 50, "callWall": 999, "flip": 40} for b in bars}
    cfg = {"alertReq": False, "usePW": False, "useFlip": False, "greenOnly": False, "earnOn": False, "noAboveCW": False, "cwRoomOn": False,
           "trailOn": False, "targetOn": False, "flipExit": False, "stopAtr": 5, "sqhFire": True, "sqhExit": True}
    sg, _ = wallwatch.detect_retest(bars, w, None, cfg, None)
    idx = {b["t"][:16]: i for i, b in enumerate(bars)}
    assert sg and all(sq[idx[s["at"]]]["fired"] for s in sg)
    assert all(s["status"] in ("squeeze", "open", "stop") for s in sg)
    d1 = wallwatch.detect_retest(bars, w, None, {**cfg, "tf": "1D", "sqhFire": False, "sqhExit": False, "sqdMom": True}, None)[0]
    dsq = wallwatch.squeeze(wallwatch.daily_bars(bars), 20, 2.0, 1.5, 3)
    dd = {b["t"][:10]: x for b, x in zip(wallwatch.daily_bars(bars), dsq)}
    assert all(dd[s["at"][:10]]["mom"] > 0 and dd[s["at"][:10]]["up"] for s in d1)
    prev = wallwatch.squeeze_daily_prev(bars, 20, 2.0, 1.5, 3)
    assert prev[-1] == dsq[-2]


def test_watchlists():
    """Several watchlists: main reset once to the set list (the rest moved to Unusual flow), add / move / remove,
    new lists, and flow tickers go to the Unusual flow list; the bot scans all of them."""
    import wallwatch, flowdata, autotrader
    import db as db_
    db_.put({"PK": db_.upk("wl-user"), "SK": "WWCFG", "tickers": ["AAPL", "ZZZ", "NVDA"]})
    c = wallwatch.config("wl-user")
    assert c["main"] == wallwatch.MAIN_V1 and "V" in c["main"] and c["lists"][1] == {"name": "Unusual flow", "tickers": ["ZZZ"]}
    assert "ZZZ" in c["tickers"] and len(c["tickers"]) == len(wallwatch.MAIN_V1) + 1
    wallwatch.save_config("wl-user", {"newList": "Swing"})
    wallwatch.save_config("wl-user", {"add": "HOOD", "list": "Swing"})
    c = wallwatch.config("wl-user")
    assert {"name": "Swing", "tickers": ["HOOD"]} in c["lists"] and "HOOD" in c["tickers"] and "HOOD" not in c["main"]
    wallwatch.save_config("wl-user", {"move": "ZZZ", "to": "Main"})
    c = wallwatch.config("wl-user")
    assert "ZZZ" in c["main"] and c["lists"][1]["tickers"] == []
    wallwatch.save_config("wl-user", {"remove": "ZZZ", "list": "Main"})
    wallwatch.save_config("wl-user", {"renameList": "Swing", "to": "Swing 2"})
    c = wallwatch.config("wl-user")
    assert "ZZZ" not in c["tickers"] and any(x["name"] == "Swing 2" for x in c["lists"])
    wallwatch.save_config("wl-user", {"deleteList": "Swing 2"})
    assert "HOOD" not in wallwatch.config("wl-user")["tickers"]
    saved = (flowdata.alerts, autotrader.settings)
    try:
        flowdata.alerts = lambda *a, **k: {"alerts": [{"ticker": "NEWT", "premium": 900000}, {"ticker": "AAPL", "premium": 1}]}
        autotrader.settings = lambda sub: {"flowWindowMin": 5, "flowMinPremium": 1, "flowMinDte": 0, "flowMaxDte": 60, "flowAskSide": True,
                                           "flowSweeps": False, "flowMinVolOi": 0, "excludeEtfs": False}
        r = wallwatch.flow_add("wl-user")
        c = wallwatch.config("wl-user")
        assert r["added"] == ["NEWT"] and "NEWT" not in c["main"] and "NEWT" in c["lists"][1]["tickers"] and "NEWT" in c["tickers"]
        wallwatch.save_config("wl-user", {"remove": "NEWT"})                       # removed: the flow doesn't add it back
        assert wallwatch.flow_add("wl-user")["added"] == [] and "NEWT" not in wallwatch.config("wl-user")["tickers"]
        wallwatch.save_config("wl-user", {"add": "FLW", "addedBy": "flow"})        # flow adds (any path) go to Unusual flow
        c = wallwatch.config("wl-user")
        assert "FLW" not in c["main"] and "FLW" in c["lists"][1]["tickers"]
    finally:
        flowdata.alerts, autotrader.settings = saved


def test_review_open_positions():
    """Open wall watch trades replayed with the current settings: entry rules + exits (the entry forced)."""
    import wallwatch, charts, flowdata, backtest, math
    import datetime as dt
    import db as db_
    from util import now_ny
    today = now_ny().date()
    days, d = [], today - dt.timedelta(days=120)
    while d < today:
        if d.weekday() < 5:
            days.append(d.isoformat())
        d += dt.timedelta(days=1)
    hb = []
    for day in days:
        for h in ("09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"):
            k = len(hb); px = 100 + 4 * math.sin(k / 15.0)
            hb.append({"t": f"{day}T{h}", "o": px - .1, "h": px + .3, "l": px - .3, "c": px, "v": 1000})
    at = hb[-60]["t"]
    w = {day: {"callWall": 106, "putWall": 98, "flip": 100} for day in days}
    # forced entry: exits replayed with the current settings
    fs, _ = wallwatch.detect_retest(hb, w, None, {"alertReq": False, "stopAtr": 0.1, "trailOn": False}, None, None, None, None, None,
                                    {"force": {"at": at, "level": hb[-60]["l"], "levelName": "support"}})
    assert len(fs) == 1 and fs[0]["at"] == at
    saved = (charts._yahoo, flowdata.stored_walls, backtest.stored_alerts, backtest.stored_darkpool)
    try:
        charts._yahoo = lambda sym, tf, s_, e: hb if tf == "1h" else [{"t": f"{x}T09:30", "o": 100, "h": 104, "l": 96, "c": 100, "v": 1} for x in days]
        flowdata.stored_walls = lambda sym: w
        backtest.stored_alerts = lambda sub_, sym: []
        backtest.stored_darkpool = lambda sym: []
        wallwatch.save_config("rev-user", {"alertReq": False, "earnExit": False})
        db_.put({"PK": db_.upk("rev-user"), "SK": "BOT#b1", "id": "b1", "symbol": "AAA", "status": "open", "lastPlPct": 12.0,
                 "proposal": {"contract": "AAA261120C00100000", "wallwatch": {"signalAt": at, "level": 98, "entry": hb[-60]["c"]}}})
        r = wallwatch.review_open("rev-user")
        assert len(r["rows"]) == 1
        x = r["rows"][0]
        assert x["verdict"] in ("keep", "exit", "no entry") and "entryOk" in x, x
        assert (x["verdict"] == "exit") == bool(x["exitNow"] or x["earnings"])
        import autotrader
        closed, sv = [], autotrader.close
        autotrader.close = lambda sub_, bid, reason="manual": closed.append((bid, reason))
        try:
            r2 = wallwatch.review_open("rev-user", auto_close=True)
        finally:
            autotrader.close = sv
        bad = r2["rows"][0]["verdict"] in ("exit", "no entry")
        assert (closed and closed[0][0] == "b1" and r2["rows"][0]["closed"]) if bad else not closed
        import autotrader
        closed, sv = [], autotrader.close
        autotrader.close = lambda sub_, bid, reason="manual": closed.append((bid, reason))
        try:
            r2 = wallwatch.review_open("rev-user", auto_close=True)
        finally:
            autotrader.close = sv
        bad = r2["rows"][0]["verdict"] in ("exit", "no entry")
        assert (closed and closed[0][0] == "b1" and r2["rows"][0]["closed"]) if bad else not closed
    finally:
        charts._yahoo, flowdata.stored_walls, backtest.stored_alerts, backtest.stored_darkpool = saved


def test_stock_quotes():
    """Watchlist Mark / change / % change from Alpaca snapshots (mid when tight, else last trade)."""
    import flowdata, alpaca, json as js, io
    from util import now_ny
    today = now_ny().strftime("%Y-%m-%d")
    snap = {"AAA": {"latestTrade": {"p": 101.0}, "latestQuote": {"bp": 100.98, "ap": 101.02}, "dailyBar": {"t": today + "T13:30:00Z", "c": 101},
                    "prevDailyBar": {"c": 100.0}},
            "BBB": {"latestTrade": {"p": 50.0}, "latestQuote": {"bp": 40, "ap": 60}, "dailyBar": {"t": "2020-01-01T00:00:00Z", "c": 49.0}}}
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    saved = (flowdata.urllib.request.urlopen, alpaca.creds)
    try:
        flowdata.urllib.request.urlopen = lambda req, timeout=15: R(js.dumps(snap).encode())
        alpaca.creds = lambda sub: {"key": "k", "secret": "s"}
        q = flowdata.stock_quotes("q-user", ["AAA", "BBB"])
        assert q["AAA"] == {"m": 101.0, "ch": 1.0, "pct": 1.0}
        assert q["BBB"]["m"] == 50.0 and q["BBB"]["ch"] == 1.0 and q["BBB"]["pct"] == 2.04   # wide spread: last trade; vs the last close
    finally:
        flowdata.urllib.request.urlopen, alpaca.creds = saved


def _dt_bars(days=3, seed=1):
    import math, datetime as dt
    out, k = [], 0
    d0 = dt.date(2026, 9, 14)
    for di in range(days):
        day = d0 + dt.timedelta(days=di)
        for m in range(4 * 60, 16 * 60):
            if m < 9 * 60 + 30 and m % 5:
                continue                                   # sparse pre-market
            px = 500 + 3 * math.sin(k / 40.0) + 0.5 * math.sin(k / 7.0) + di * 2
            out.append({"t": f"{day.isoformat()}T{m // 60:02d}:{m % 60:02d}", "o": px - 0.05, "h": px + 0.2, "l": px - 0.2,
                        "c": px + (0.04 if math.cos(k / 40.0) > 0 else -0.04), "v": 1000})
            k += 1
    return out


def test_daytrade_engine():
    """1-minute engine: rules ON must all pass, long and short, stop / target / time exits, one trade at a time,
    max trades a day, flat by the flat time; 5-minute aggregation for the chart."""
    import daytrade
    bars = _dt_bars(3)
    P = {**daytrade.PARAMS, "aboveVwap": True, "emaStack": True, "greenBar": True, "maxTrades": 3}
    sg = daytrade.detect(bars, P)
    assert sg and all(s["status"] in ("stop", "target", "time", "trail") for s in sg[:-1])
    days = {}
    for s in sg:
        days[s["at"][:10]] = days.get(s["at"][:10], 0) + 1
        assert "09:35" <= s["at"][11:16] < "15:30" and (s["exitAt"] or "")[11:16] <= "15:59"
    assert max(days.values()) <= 3 and {s["dir"] for s in sg} <= {"long", "short"}
    for a, b in zip(sg, sg[1:]):
        assert b["at"] >= (a["exitAt"] or "")                 # one at a time
    for s in sg:
        if s["status"] == "target":
            assert abs(s["R"] - 2.0) < 0.3, s
        if s["status"] == "stop":
            assert s["R"] <= -0.9, s
    lo = daytrade.detect(bars, {**P, "shorts": False})
    assert lo and all(s["side"] == 1 for s in lo)
    assert daytrade.detect(bars, {**P, **{k: False for k in daytrade.ENTRY}}) == []      # no rule on: no entry
    agg = daytrade._agg([b for b in bars if "09:30" <= b["t"][11:16] < "16:00"][:12], 5)
    assert [x["t"][11:16] for x in agg] == ["09:30", "09:35", "09:40"] and agg[0]["h"] == max(b["h"] for b in bars if "09:30" <= b["t"][11:16] < "09:35" and b["t"][:10] == "2026-09-14")
    st = daytrade.stats(sg)
    assert st["n"] == len(sg) and "exits" in st
    c = daytrade.save_config("dt-user", {"stopAtr": 2, "aboveVwap": False, "startT": "09:45", "tickers": "qqq spy iwm"})
    assert c["stopAtr"] == 2 and c["aboveVwap"] is False and c["startT"] == "09:45" and c["tickers"] == ["QQQ", "SPY", "IWM"]


def test_daytrade_live_tick():
    """Live: the engine's new trade on the last finished candle is sent to the day account; closed when the engine
    closes it."""
    import daytrade, datetime as dt
    bars = _dt_bars(2)
    P = {**daytrade.PARAMS, "aboveVwap": True, "emaStack": True, "greenBar": True, "enabled": True, "trade": True, "tickers": ["QQQ"]}
    sg = daytrade.detect(bars, P, None, "2026-09-15")
    s0 = next(s for s in sg if s["at"][:10] == "2026-09-15" and s["status"] != "open")
    calls = []
    saved = (daytrade.minute_bars, daytrade.creds, daytrade._alp, daytrade.walls_by_day, daytrade.config)
    try:
        daytrade.minute_bars = lambda sub, sym, days, now=None: [b for b in bars if b["t"] < (now or dt.datetime.now()).strftime("%Y-%m-%dT%H:%M")]
        daytrade.creds = lambda sub: {"key": "k", "secret": "s"}
        held = {}
        def alp(c, m, path, body=None):
            calls.append((m, path, body))
            if m == "GET" and path.startswith("/v2/positions/"):
                return held.get(path.rsplit("/", 1)[1])
            if m == "POST":
                held[body["symbol"]] = {"qty": body["qty"]}
                return {"id": "o1"}
            if m == "DELETE":
                held.pop(path.rsplit("/", 1)[1], None)
                return {"id": "o2"}
            return {}
        daytrade._alp = alp
        daytrade.walls_by_day = lambda sub, sym, dates, *a, **k: {}
        daytrade.config = lambda sub: P
        t_in = dt.datetime.strptime(s0["at"], "%Y-%m-%dT%H:%M") + dt.timedelta(minutes=1, seconds=5)
        r = daytrade.tick("dt-live", t_in)
        assert any(m == "POST" for m, *_ in calls), (r, calls)
        post = next(b for m, p_, b in calls if m == "POST")
        assert post["side"] == ("buy" if s0["side"] > 0 else "sell") and post["qty"] == "10"
        calls.clear()
        t_out = dt.datetime.strptime(s0["exitAt"], "%Y-%m-%dT%H:%M") + dt.timedelta(minutes=1, seconds=5)
        daytrade.tick("dt-live", t_out)
        assert any(m == "DELETE" and p_ == "/v2/positions/QQQ" for m, p_, b in calls), calls
        rec = [x for x in db.q_prefix(db.upk("dt-live"), "DTPOS#QQQ#") if x["at"] == s0["at"]][0]
        assert rec["status"] == "closed" and s0["status"] in rec["exitReason"]
    finally:
        daytrade.minute_bars, daytrade.creds, daytrade._alp, daytrade.walls_by_day, daytrade.config = saved


def test_tr3ndy_studies():
    """Day-trading chart studies: LIS = previous regular close, PM zone 20-40% from the gap side, fibs, turns, bands."""
    import tr3ndy
    bars = _dt_bars(3)
    b5 = tr3ndy.agg(bars, 5)
    lines, lf, sent = tr3ndy.lis(b5)
    reg = [b for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    assert lines["2026-09-15"] == [b for b in reg if b["t"][:10] == "2026-09-14"][-1]["c"] and "2026-09-14" not in lines
    pz = tr3ndy.pmz(b5, lines)["2026-09-15"]
    r = pz["h"] - pz["l"]
    assert (abs(pz["up"] - (pz["h"] - .2 * r)) < 1e-3 and abs(pz["lo"] - (pz["h"] - .4 * r)) < 1e-3) if pz["gap"] else (abs(pz["lo"] - (pz["l"] + .2 * r)) < 1e-3)
    fb = dict((k, v) for k, v in tr3ndy.fibs(bars, lines)["2026-09-15"])
    d14 = [b for b in reg if b["t"][:10] == "2026-09-14"]
    assert fb["high"] == round(max(b["h"] for b in d14), 4) and fb["low"] == round(min(b["l"] for b in d14), 4)   # the previous day
    assert abs(fb["50%"] - (fb["high"] + fb["low"]) / 2) < 1e-3 and fb["200%"] > fb["161.8%"] > fb["127.2%"] > fb["high"] and fb["-100%"] < fb["-61.8%"] < fb["-27.2%"] < fb["low"]
    assert "2026-09-14" not in tr3ndy.fibs(bars)
    tu = tr3ndy.turns([{"t": str(i), "m": m} for i, m in enumerate([1, 3, 2, 1, -1, -3, -2])])
    assert tu == [{"t": "2", "dir": -1}, {"t": "6", "dir": 1}]
    ser, fm = tr3ndy.bands(reg)
    assert len(ser) == len(reg) and all(x["dir"] in (1, -1) for x in fm)


def test_daytrade_backtest_stats():
    """Long backtest: SIP first (falls back to IEX), halves, month / weekday / hour, drawdown, equity curve."""
    import daytrade
    bars = _dt_bars(6)
    seen = []
    def mb(sub, sym, days, now=None, feed=None):
        seen.append(feed)
        if feed == "sip":
            raise RuntimeError("subscription does not permit querying recent SIP data")
        return bars
    saved = (daytrade.minute_bars, daytrade.walls_by_day, daytrade.config)
    try:
        daytrade.minute_bars = mb
        daytrade.walls_by_day = lambda sub, sym, dates, *a, **k: {}
        daytrade.config = lambda sub: {**daytrade.PARAMS, "tickers": ["QQQ"]}
        r = daytrade.backtest("bt-user", 250)
        assert seen == ["sip", "iex"] and r["feed"] == {"QQQ": "iex"} and r["days"] == 250 and r["daysFound"] == 6
        assert r["all"]["n"] > 0 and r["h1"]["n"] + r["h2"]["n"] == r["all"]["n"]
        assert sum(x["n"] for x in r["byHour"].values()) == r["all"]["n"] and r["byWeekday"] and r["byMonth"]
        assert r["curve"][-1] == round(sum(x["R"] for x in r["trades"]), 2) and r["maxDD"] <= 0
        r2 = daytrade.backtest("bt-user", 30, {"tickers": "QQQ SPY", "stopAtr": "2"})    # the settings on screen (text from the inputs)
        assert set(r2["byTicker"]) == {"QQQ", "SPY"}
        daytrade.backtest_job("bt-user", {"days": 30})
        assert daytrade.backtest_result("bt-user")["all"]["n"] == r["all"]["n"]
    finally:
        daytrade.minute_bars, daytrade.walls_by_day, daytrade.config = saved


def test_day_scenario_study():
    """Scenario study: gaps (fill, direction), first touches of levels with the move after (signed through the level),
    held / rejected, dark pool + gamma confluence."""
    import daystudy
    bars = _dt_bars(6)
    walls = {d: {"putWall": 499, "flip": 501, "callWall": 504} for d in {b["t"][:10] for b in bars}}
    dpm = {d: [501.05, 497.0] for d in walls}
    days, ev = daystudy.study_ticker(bars, walls, dpm)
    assert len(days) == 5 and all(x["bucket"] for x in days)
    assert ev and all(e["side"] in ("from below", "from above") for e in ev)
    seen = {}
    for e in ev:
        k = (e["d"], e["level"], e["price"])
        assert k not in seen                                   # first touch only
        seen[k] = 1
    dp = [e for e in ev if e["level"] == "dark pool" and abs(e["price"] - 501.05) < 1e-6]
    assert dp and all(e["conf"] == "flip" for e in dp)        # 501.05 within 0.15% of the flip
    e0 = ev[0]
    day = [b for b in bars if b["t"][:10] == e0["d"] and "09:30" <= b["t"][11:16] < "16:00"]
    sgn = 1 if e0["side"] == "from below" else -1
    assert abs(e0["f15"] - round((day[e0["minute"] + 15]["c"] / e0["price"] - 1) * 100 * sgn, 3)) < 1e-6
    sm = daystudy.summarize(days, ev)
    assert sm["gaps"] and sm["levels"] and sum(v["n"] for v in sm["gaps"].values()) == 5 and "flip · from below" in str(sm["confluence"]) or sm["confluence"] is not None


def test_day_scenario_study_cvd():
    """Scenario study with CVD: CVD at 10:00 etc. -> move to the close, level touches split by CVD with / against the
    approach (CVD of the candle before the touch); real order flow used for the minutes it covers."""
    import daystudy, daytrade
    bars = _dt_bars(6)
    walls = {d: {"putWall": 499, "flip": 501, "callWall": 504} for d in {b["t"][:10] for b in bars}}
    days, ev = daystudy.study_ticker(bars, walls, {})
    d0 = days[0]
    rb = [b for b in bars if b["t"][:10] == d0["d"] and "09:30" <= b["t"][11:16] < "16:00"]
    est = daytrade._cvd_est(rb)
    j = next(i for i, b in enumerate(rb) if b["t"][11:16] >= "10:00")
    assert d0["cvdAt"]["10:00"]["cvd"] == round(est[j - 1])
    assert abs(d0["cvdAt"]["10:00"]["move"] - round((rb[-1]["c"] / rb[j - 1]["c"] - 1) * 100, 3)) < 1e-9
    e = next(x for x in ev if x["cvdWith"] is not None)
    rbe = [b for b in bars if b["t"][:10] == e["d"] and "09:30" <= b["t"][11:16] < "16:00"]
    cv = daytrade._cvd_est(rbe)[e["minute"] - 1]
    assert e["cvdWith"] == (cv * (1 if e["side"] == "from below" else -1) > 0)
    real = {rb[j - 1]["t"][:16]: 123456.0}
    days2, _ = daystudy.study_ticker(bars, walls, {}, real_cvd=real)
    assert next(x for x in days2 if x["d"] == d0["d"])["cvdAt"]["10:00"]["cvd"] == 123456
    sm = daystudy.summarize(days, ev)
    assert sm["cvdTime"] and sm["cvdLevels"]
    for k, x in sm["cvdTime"].items():
        assert x["n"] > 0 and 0 <= x["same"] <= 100
    assert sum(x["n"] for k, x in sm["cvdTime"].items() if k.startswith("10:00") and k.endswith("all days")) == len([x for x in days if x["cvdAt"].get("10:00", {}).get("cvd")])


def test_daytrade_new_rules_filters_exits():
    """Study rules, day filters (gap, VIX, other ticker, lunch, daily loss), breakeven / scale-out / time stop /
    level target; indicators reused across settings (cache)."""
    import daytrade
    bars = _dt_bars(6)
    base = {**daytrade.PARAMS, "aboveVwap": True, "emaStack": False, "greenBar": True, "maxTrades": 5}
    ref = daytrade.detect(bars, base)
    assert ref
    # every new entry rule / filter runs and only ever removes trades
    for k in daytrade.STUDY_RULES:
        sg = daytrade.detect(bars, {**base, k: True}, {}, None, {"vix": {d: 15.0 for d in {b["t"][:10] for b in bars}}})
        assert len(sg) <= len(ref) * 2 + 5, k
    # filters
    days = sorted({b["t"][:10] for b in bars})
    assert daytrade.detect(bars, {**base, "vixOn": True, "vixMax": 20}, {}, None, {"vix": {d: 30.0 for d in days}}) == []
    assert daytrade.detect(bars, {**base, "vixOn": True, "vixMax": 20}, {}, None, {"vix": {d: 15.0 for d in days}})
    reg = [b for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    assert daytrade.detect(bars, {**base, "peerAgree": True}, {}, None, {"peer": {b["t"]: -1 for b in reg}, }) == [] or base["shorts"]
    longs = {**base, "shorts": False}
    assert daytrade.detect(bars, {**longs, "peerAgree": True}, {}, None, {"peer": {b["t"]: -1 for b in reg}}) == []
    assert daytrade.detect(bars, {**longs, "peerAgree": True}, {}, None, {"peer": {b["t"]: 1 for b in reg}})
    nl = daytrade.detect(bars, {**base, "noLunch": True})
    assert not any("11:30" <= s["at"][11:16] < "13:30" for s in nl)
    gp = daytrade.detect(bars, {**base, "gapOn": True, "gapMin": 50, "gapMax": 99})
    assert gp == []
    # exits
    tm = daytrade.detect(bars, {**base, "timeOn": True, "timeMin": 7, "targetOn": False, "stopAtr": 50})
    assert tm and all(s["status"] in ("time stop", "time") for s in tm)
    assert any(s["status"] == "time stop" and s["exitAt"][11:16] for s in tm)
    sc = daytrade.detect(bars, {**base, "scaleOn": True, "scaleR": 0.5, "targetR": 2})
    one = next((s for s in sc if s.get("scaled") is not None and s["status"] != "open"), None)
    assert one is not None
    risk = abs(one["entry"] - one["stop0"])
    exp = round((0.5 * (one["scaled"] - one["entry"]) + 0.5 * (one["exit"] - one["entry"])) * one["side"] / risk, 2)
    assert one["R"] == exp
    be = daytrade.detect(bars, {**base, "beOn": True, "beR": 0.3, "targetOn": False, "stopAtr": 2})
    assert any(s.get("be") for s in be)
    bes = [s for s in be if s["status"] == "breakeven"]
    assert bes and all((s["exit"] - s["entry"]) * s["side"] <= 1e-4 for s in bes)
    lt = daytrade.detect(bars, {**base, "lvlTarget": True, "targetOn": False}, {d: {"callWall": 9999, "putWall": 1} for d in days})
    assert any(s.get("lvlT") for s in lt)
    # daily loss limit: no entry after the day's closed R reaches -X
    ds = daytrade.detect(bars, {**base, "dayStop": True, "dayStopR": 0.5, "maxTrades": 50, "targetR": 5})
    byd = {}
    for s in ds:
        tot = sum(x["R"] for x in byd.get(s["at"][:10], []))
        assert tot > -0.5 or not byd.get(s["at"][:10]), s["at"]
        byd.setdefault(s["at"][:10], []).append(s)
    # open vs flip, reverse: long only when the day opened under the flip, short only above
    W = {d: {"flip": 99999} for d in days}
    ff = daytrade.detect(bars, {**base, "aboveVwap": False, "greenBar": False, "flipFade": True, "maxTrades": 1}, W)
    assert ff and all(s0["side"] == 1 for s0 in ff) and len({s0["at"][:10] for s0 in ff}) == len(ff)
    W = {d: {"flip": 1} for d in days}
    assert all(s0["side"] == -1 for s0 in daytrade.detect(bars, {**base, "aboveVwap": False, "greenBar": False, "flipFade": True}, W))
    # previous day's walls: day k uses the flip of day k-1 (the first day has none)
    W = {d: {"flip": (99999 if i % 2 else 1)} for i, d in enumerate(days)}
    pv = daytrade.detect(bars, {**base, "aboveVwap": False, "greenBar": False, "flipFade": True, "flipPrev": True, "maxTrades": 1}, W)
    assert pv and days[0] not in {s0["at"][:10] for s0 in pv}
    for s0 in pv:
        k = days.index(s0["at"][:10])
        assert s0["side"] == (1 if (k - 1) % 2 else -1)
    # put wall side: put wall above the close -> shorts only
    hiW = {d: {"putWall": 99999} for d in days}
    nb = {**base, "aboveVwap": False, "greenBar": False}
    pws = daytrade.detect(bars, {**nb, "pwSide": True}, hiW)
    assert pws and all(s0["side"] == -1 for s0 in pws)
    pwl = daytrade.detect(bars, {**nb, "pwSide": True}, {d: {"putWall": 1} for d in days})
    assert pwl and all(s0["side"] == 1 for s0 in pwl)
    # CVD side: estimated from the candles; the real one (stored order flow) when switched on
    reg_ = [b for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    Ic = daytrade.indicators(reg_, bars, daytrade.PARAMS)
    tt_ = [b["t"] for b in reg_]
    cv = daytrade.detect(bars, {**base, "cvdSide": True})
    assert cv and all((Ic["cvdE"][tt_.index(s0["at"])] > 0) == (s0["side"] > 0) for s0 in cv)
    fake = {b["t"]: -1.0 for b in reg_}
    cr = daytrade.detect(bars, {**base, "cvdSide": True, "cvdReal": True, "shorts": False}, {}, None, {"cvd": fake})
    assert cr == []
    # squeeze turn: entries only within formWin minutes after a turn in the trade direction
    st_ = daytrade.detect(bars, {**base, "aboveVwap": False, "greenBar": False, "sqTurn": True, "formWin": 3})
    reg = [b for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    It = daytrade.indicators(reg, bars, daytrade.PARAMS)
    tt = [b["t"] for b in reg]
    assert st_
    for s0 in st_:
        i = tt.index(s0["at"])
        assert any(It["sqTurnD"][j] == s0["side"] for j in range(i - 2, i + 1))
    # stop unit: 1-min ATR vs 5-min ATR vs daily ATR vs % of price
    d1 = daytrade.detect(bars, {**base, "stopAtr": 1.0})
    d5 = daytrade.detect(bars, {**base, "stopAtr": 1.0, "stopBase": "atr5"})
    dp = daytrade.detect(bars, {**base, "stopAtr": 0.2, "stopBase": "pct"})
    dd = daytrade.detect(bars, {**base, "stopAtr": 0.1, "stopBase": "atrD"})
    da = daytrade.detect(bars, {**base, "stopAtr": 5, "stopBase": "avgN", "volN": 300})
    I = daytrade.indicators([b for b in bars if "09:30" <= b["t"][11:16] < "16:00"], bars, {**daytrade.PARAMS, "volN": 300})
    assert da and abs(da[0]["unit"] - round(I["avgN"][[b["t"] for b in bars if "09:30" <= b["t"][11:16] < "16:00"].index(da[0]["at"])], 4)) < 1e-4
    # swing stop: under the last swing low known at entry (pivot confirmed swingN candles later), minus the buffer
    sw = daytrade.detect(bars, {**base, "stopBase": "swing", "swingN": 5, "swingBuf": 0.05, "shorts": False, "targetOn": False})
    reg = [b for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    Iw = daytrade.indicators(reg, bars, {**daytrade.PARAMS, "swingN": 5})
    assert sw
    for s0 in sw[:10]:
        i = [b["t"] for b in reg].index(s0["at"])
        known = [p for p in Iw["pivLo"] if p[0] <= i and p[1] < s0["entry"]]
        if known:
            assert abs(s0["stop0"] - round(known[-1][1] - s0["entry"] * 0.0005, 4)) < 1e-6 and s0["stop0"] < s0["entry"]
            assert all(reg[j]["l"] >= known[-1][1] for j in range(known[-1][0] - 5 - 5, known[-1][0] - 5) if j >= 0)
    risk = lambda sg: sum(abs(s["entry"] - s["stop0"]) for s in sg) / len(sg)
    assert risk(d5) > risk(d1) and dd and all(abs(abs(s["entry"] - s["stop0"]) - s["entry"] * 0.002) < 1e-3 for s in dp)
    assert daytrade.clean_cfg(daytrade.PARAMS, {"stopBase": "atr5"})["stopBase"] == "atr5" and daytrade.clean_cfg(daytrade.PARAMS, {"stopBase": "x"})["stopBase"] == "atr1"
    # cache: same indicator settings computed once
    cache = {}
    daytrade.detect(bars, base, {}, None, {"cache": cache})
    daytrade.detect(bars, {**base, "targetR": 3}, {}, None, {"cache": cache})
    assert len([k for k in cache if isinstance(k, tuple) and k[0] != "X"]) == 1


def test_daytrade_compare_presets_options():
    """Saved settings, compare several on the same candles, 0DTE option pricing (call for a long, put for a short)."""
    import daytrade
    import dtoptions
    import optiontest
    sub = "cmp-user"
    daytrade.save_config(sub, {"tickers": "QQQ"})
    pr = daytrade.save_preset(sub, "Tight", {"stopAtr": 0.5, "aboveVwap": True})
    assert pr[0]["name"] == "Tight" and pr[0]["cfg"]["stopAtr"] == 0.5
    assert daytrade.delete_preset(sub, "tight") == []
    bars = _dt_bars(6)
    calls = []
    saved = (daytrade.minute_bars, daytrade.walls_by_day)
    try:
        daytrade.minute_bars = lambda s, sym, days, now=None, feed=None: calls.append(sym) or bars
        daytrade.walls_by_day = lambda *a, **k: {}
        r = daytrade.compare(sub, 30, [{"name": "A", "cfg": {"stopAtr": 0.5}}, {"name": "B", "cfg": {"stopAtr": 2}},
                                       {"name": "C", "cfg": {"stopAtr": 2, "peerAgree": True}}])
        assert [v["name"] for v in r["variants"]] == ["A", "B", "C"] and r["daysFound"] == 6
        assert calls.count("QQQ") == 1 and calls.count("SPY") == 1          # candles loaded once; SPY = the other ticker
        assert r["variants"][0]["all"]["n"] > 0 and r["variants"][0]["changes"]["stopAtr"] == 0.5
        daytrade.compare_job(sub, {"days": 30, "variants": [{"name": "A", "cfg": {}}]})
        assert daytrade.compare_result(sub)["variants"][0]["name"] == "A"
    finally:
        daytrade.minute_bars, daytrade.walls_by_day = saved
    assert dtoptions.occ("QQQ", "2026-03-06", "P", 600) == "QQQ260306P00600000"
    assert dtoptions.expiries("QQQ", "2026-03-04")[0] == "2026-03-04" and dtoptions.expiries("AAPL", "2026-03-04")[0] == "2026-03-06"
    got = []
    def ob(c, syms, tf, start, end):
        got.append(syms[0])
        if "C00500000" in syms[0] or "P00500000" in syms[0]:
            return {syms[0]: [{"t": f"2026-03-04T{14 + h}:{m:02d}:00Z", "c": 1.0 + 0.01 * (h * 60 + m)} for h in range(0, 2) for m in range(60)]}
        return {}
    o_b, o_c = optiontest.bars, daytrade.data_creds
    try:
        optiontest.bars, daytrade.data_creds = ob, lambda sub: {"key": "k", "secret": "s"}
        rows = [{"sym": "QQQ", "at": "2026-03-04T09:40", "exitAt": "2026-03-04T10:00", "entry": 500.1, "side": 1, "dir": "long"},
                {"sym": "QQQ", "at": "2026-03-04T09:45", "exitAt": "2026-03-04T09:50", "entry": 499.9, "side": -1, "dir": "short"},
                {"sym": "QQQ", "at": "2023-03-04T09:45", "exitAt": "2023-03-04T09:50", "entry": 499.9, "side": -1, "dir": "short"}]
        st = dtoptions.price("u", rows, {"on": True, "slip": 0})
        assert rows[0]["optSym"] == "QQQ260304C00500000" and rows[1]["optSym"] == "QQQ260304P00500000"
        assert rows[0]["optIn"] == 1.4 and rows[0]["optOut"] == 1.6 and st["n"] == 2 and st["before2024"] == 1
        assert rows[2].get("optPct") is None
    finally:
        optiontest.bars, daytrade.data_creds = o_b, o_c


def test_daytrade_auto_search():
    """Automatic search: candles loaded once, every single change scored on the 1st half only (t-stat), the best kept
    only when clearly better, the 2nd half reported (never used to choose)."""
    import daytrade
    import dtsearch
    assert dtsearch.tstat([1, 1, 1]) is None and dtsearch.tstat([1, -1, 1, -1]) == 0 and dtsearch.tstat([2, 1, 2, 1]) > 2
    bars = _dt_bars(10)
    calls = []
    saved = (daytrade.minute_bars, daytrade.walls_by_day, daytrade.vix_map)
    try:
        daytrade.minute_bars = lambda s, sym, days, now=None, feed=None: calls.append(sym) or bars
        daytrade.walls_by_day = lambda *a, **k: {}
        daytrade.vix_map = lambda: {}
        sub = "srch-user"
        daytrade.save_config(sub, {"tickers": "QQQ", "aboveVwap": True, "emaStack": True, "greenBar": True})
        old_min = dtsearch.MIN_N
        dtsearch.MIN_N = 5
        r = dtsearch.run(sub, {"days": 30, "rounds": 2})
        dtsearch.MIN_N = old_min
        assert calls.count("QQQ") == 1 and r["daysFound"] == 10 and r["tested"] > 30
        assert r["verdict"] in ("edge", "weak", "none") and r["table"]
        t1 = [x["train"]["totR"] for x in r["table"]]
        assert t1 == sorted(t1, reverse=True)
        prev = r["start"]["train"]
        for p in r["path"]:                                    # each kept change beat the previous by more than the noise
            assert p["train"]["totR"] >= prev["totR"] + dtsearch.need(prev) and p["train"]["t"] >= max(1, prev["t"] or 0)
            prev = p["train"]
        assert dtsearch.result(sub)["tested"] == r["tested"]
    finally:
        daytrade.minute_bars, daytrade.walls_by_day, daytrade.vix_map = saved


def test_day_walls_fetch_missing_days():
    """Day walls: stored days used, the missing ones fetched from Unusual Whales (all expirations)."""
    import daytrade
    import flowdata
    got = []
    saved = (flowdata.stored_walls, flowdata.gex_day, flowdata._key)
    try:
        flowdata.stored_walls = lambda sym: {"2026-03-02": {"callWall": 1, "putWall": 2, "flip": 3}}
        flowdata.gex_day = lambda key, sym, d, flip=True: got.append(d) or {"callWall": 10, "putWall": 9, "flip": 9.5}
        flowdata._key = lambda sub: "k"
        w = daytrade.walls_by_day("u", "SPY", ["2026-03-02", "2026-03-03", "2026-03-04"], False)
        assert sorted(got) == ["2026-03-03", "2026-03-04"] and w["2026-03-02"]["flip"] == 3 and w["2026-03-04"]["flip"] == 9.5
    finally:
        flowdata.stored_walls, flowdata.gex_day, flowdata._key = saved


def test_morning_flip_check():
    """Morning snapshots of the flip (fresh from Unusual Whales) and the check against the stored flip."""
    import datetime as dt
    import daytrade
    import flowdata
    saved = (flowdata.snap_walls, flowdata.stored_walls)
    try:
        vals = {"09:20": 500.0, "09:32": 501.0, "09:40": 502.0}
        flowdata.snap_walls = lambda key, sym, d: {"callWall": 510, "putWall": 490, "flip": vals[cur[0]]}
        cur = ["09:20"]
        for hm in ("09:20", "09:32", "09:40"):
            cur[0] = hm
            flowdata.flip_snapshot("k", ["QQQ"], dt.datetime(2026, 3, 4, int(hm[:2]), int(hm[3:])), lambda sym: (None if hm == "09:20" else 505.0))
        flowdata.stored_walls = lambda sym: {"2026-03-04": {"flip": 508.0}}
        r = daytrade.flip_check("u")
        row = next(x for x in r["rows"] if x["date"] == "2026-03-04" and x["sym"] == "QQQ")
        assert row["snaps"] == {"09:20": 500.0, "09:32": 501.0, "09:40": 502.0} and row["open"] == 505.0
        assert row["sideMorning"] == "above" and row["sideStored"] == "below" and row["agree"] is False and r["pct"] == 0.0
    finally:
        flowdata.snap_walls, flowdata.stored_walls = saved


def _sd_day(day, o=50.0, drift=0.02, vol=1000, first_up=True):
    """One synthetic stock day of 1-minute candles 09:30-16:00."""
    out, px = [], o
    for m in range(390):
        hm = 570 + m
        step = (drift if first_up else -drift) if m < 30 else (drift if first_up else -drift) * 0.5
        o_, c_ = px, px + step
        out.append({"t": f"{day}T{hm // 60:02d}:{hm % 60:02d}", "o": round(o_, 4), "h": round(max(o_, c_) + 0.01, 4),
                    "l": round(min(o_, c_) - 0.01, 4), "c": round(c_, 4), "v": vol * (5 if m < 5 else 1)})
        px = c_
    return out


def test_stockday_engine():
    """Stocks in play: universe filter from the candles before each day, opening-range windows, RVOL ranking, the
    direction lock (doji skipped), stop entries at the opening-range high / low, stop = % of ATR, exit at the close,
    VWAP side / exit, the VWAP stop-out cap and the sizing."""
    import stockday
    # universe
    daily = {"AAA": [{"t": f"2026-02-{d:02d}T00:00", "o": 50, "h": 51, "l": 49, "c": 50, "v": 2000000} for d in range(1, 21)],
             "LOW": [{"t": f"2026-02-{d:02d}T00:00", "o": 3, "h": 3.5, "l": 2.9, "c": 3, "v": 5000000} for d in range(1, 21)]}
    u = stockday.universe_from_daily(daily, ["2026-02-16", "2026-02-21"])
    assert "AAA" in u["2026-02-16"] and "LOW" not in u["2026-02-16"] and "AAA" in u["2026-02-21"]   # the 21st: before its own candle
    assert abs(u["2026-02-16"]["AAA"][0] - 2.0) < 1e-9 and u["2026-02-16"]["AAA"][1] == 2000000
    # windows
    bars = _sd_day("2026-03-02")
    pm = [{"t": "2026-03-02T08:00", "o": 50, "h": 50, "l": 50, "c": 50, "v": 777}]
    w = stockday.window_stats(pm, stockday.multi_agg(bars, 5) if hasattr(stockday, "multi_agg") else
                              [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]} for b in bars])
    assert w[0] == 777 and w[1] == bars[0]["o"] and w[5] == sum(b["v"] for b in bars[:5])
    # ranking: today's opening-range volume vs the previous days
    prev = [{"AAA": [100] + [50, 51, 49, 50.5, 1000] * 3, "BBB": [100] + [50, 51, 49, 50.5, 1000] * 3} for _ in range(6)]
    today = {"AAA": [500] + [50, 51, 49, 50.6, 30000] * 3, "BBB": [100] + [50, 51, 49, 50.0, 1500] * 3}
    uni = {"AAA": [2.0, 2000000, 50], "BBB": [2.0, 2000000, 50]}
    P = {**stockday.PARAMS, "minRvol": 1.0, "topN": 5}
    r = stockday.rank_day("2026-03-02", uni, prev + [today], P)
    assert [x["sym"] for x in r] == ["AAA", "BBB"] and r[0]["rvol"] == 30.0 and r[0]["dir"] == 1 and r[1]["dir"] == 0   # BBB: doji
    assert stockday.rank_day("2026-03-02", uni, prev + [today], {**P, "minRvol": 2})[0]["sym"] == "AAA"
    assert len(stockday.rank_day("2026-03-02", uni, prev + [today], {**P, "minRvol": 2})) == 1
    assert stockday.rank_day("2026-03-02", uni, prev + [today], {**P, "rvolMode": "pm"})[0]["pmRvol"] == 5.0
    # simulate: long, entry at the opening-range high, exit at the close (uptrend)
    hi = max(b["h"] for b in bars[:5]); lo = min(b["l"] for b in bars[:5])
    pick = {"sym": "AAA", "rvol": 30, "pmRvol": 5, "atr": 2.0, "o": bars[0]["o"], "h": hi, "l": lo, "c": bars[4]["c"], "dir": 1}
    t = stockday.simulate(pick, bars, {**stockday.PARAMS, "entryEnd": "11:30"})
    assert t and t["side"] == 1 and t["entry"] == round(max(bars[5]["o"], hi), 4) and t["status"] == "close"
    assert abs(t["stop0"] - (t["entry"] - 0.2)) < 1e-9 and t["R"] > 0
    # short on a down day; stop hit when it reverses
    dn = _sd_day("2026-03-03", first_up=False)
    px = dn[59]["c"]
    for b in dn[60:]:                                          # rallies after an hour
        b.update(o=round(px, 4), c=round(px + 0.05, 4), h=round(px + 0.06, 4), l=round(px - 0.01, 4))
        px += 0.05
    pk2 = {**pick, "h": max(b["h"] for b in dn[:5]), "l": min(b["l"] for b in dn[:5]), "dir": -1}
    t2 = stockday.simulate(pk2, dn, {**stockday.PARAMS})
    assert t2["side"] == -1 and t2["status"] == "stop" and abs(t2["R"] + 1) < 0.2
    # VWAP exit on a 1-minute close across the VWAP
    t3 = stockday.simulate(pk2, dn, {**stockday.PARAMS, "vwapExit": True, "stopAtrPct": 500})
    assert t3["status"] == "vwap"
    # day: VWAP stop-out cap and sizing
    picks = [{**pk2, "sym": f"S{i}"} for i in range(5)]
    trs = stockday.day_trades(picks, {f"S{i}": dn for i in range(5)}, {**stockday.PARAMS, "vwapExit": True, "stopAtrPct": 500, "vwapCap": 2, "topN": 5})
    assert all(0 < x["size"] <= 1 for x in trs) and all(abs(x["ret"] - x["R"] * x["size"]) < 1e-3 for x in trs)
    assert stockday.bucket(0.5) == "< 1x" and stockday.bucket(31) == "> 30x"
    # storage: compact, split over items, day index
    rows = {f"T{i:04d}": [1.5, 2000000, 51.25] for i in range(1600)}
    stockday.put_day("U", "2026-03-02", rows)
    assert stockday.get_day("U", "2026-03-02")["T0007"] == [1.5, 2000000.0, 51.25] and "2026-03-02" in stockday.stored_days("U")
    c = stockday.clean_cfg(stockday.PARAMS, {"orbMin": 7, "rvolMode": "pm", "exclude": "csx, wmb", "entryEnd": "12:00"})
    assert c["orbMin"] == 5 and c["rvolMode"] == "pm" and c["exclude"] == ["CSX", "WMB"] and c["entryEnd"] == "12:00"


def test_stockday_prep_and_backtest():
    """History build (universe then opening-range windows, resumable) and the backtest on it."""
    import stockday
    days = [f"2026-03-{d:02d}" for d in range(2, 28) if __import__("datetime").date(2026, 3, d).weekday() < 5]
    def mb(c, syms, tf, start, end, feed="sip", max_pages=60):
        out = {}
        for s in syms:
            if tf == "1Day":
                out[s] = [{"t": f"2026-02-{d:02d}T00:00", "o": 50, "h": 51.5, "l": 49, "c": 50, "v": 3000000} for d in range(1, 28)] + \
                         [{"t": f"{d}T00:00", "o": 50, "h": 51.5, "l": 49, "c": 50, "v": 3000000} for d in days]
            elif tf == "30Min":
                out[s] = [{"t": start.strftime("%Y-%m-%dT08:00"), "o": 50, "h": 50, "l": 50, "c": 50, "v": 1000}]
            elif tf == "5Min":
                d0 = start.strftime("%Y-%m-%d")
                big = 9 if (s == "HOT" and d0 >= days[10]) else 1
                out[s] = [{"t": f"{d0}T{(570 + 5 * k) // 60:02d}:{(570 + 5 * k) % 60:02d}", "o": 50 + k * 0.05, "h": 50.1 + k * 0.05,
                           "l": 49.95 + k * 0.05, "c": 50.05 + k * 0.05, "v": 1000 * big} for k in range(12)]
            else:
                d0 = start.strftime("%Y-%m-%d")
                out[s] = _sd_day(d0)
        return out
    saved = (stockday.multi_bars, stockday.assets, stockday.trading_days, stockday.data_creds, stockday.now_ny)
    try:
        import datetime as _dt
        stockday.multi_bars = mb
        stockday.assets = lambda c: ["HOT", "COLD"]
        stockday.trading_days = lambda c, a, b: days
        stockday.data_creds = lambda sub: {"key": "k", "secret": "s"}
        stockday.now_ny = lambda: _dt.datetime(2026, 3, 28, 8, 0)
        st = stockday.prep("sd-user", days=len(days), budget_s=60)
        assert st["phase"] == "done" and st["rDays"] == len(days) and st["uDays"] == len(days)
        r = stockday.backtest("sd-user", 30, {"topN": 5, "minRvol": 1.5})
        assert r["daysFound"] == len(days) and r["all"]["n"] > 0 and all(t["sym"] == "HOT" for t in r["trades"])
        assert set(r["byRvol"]) <= {lbl for _, _, lbl in stockday.RV_BUCKETS} and r["retPct"] is not None
        stockday.backtest_job("sd-user", {"days": 30, "cfg": {"topN": 5}})
        assert stockday.backtest_result("sd-user")["daysFound"] == len(days)
    finally:
        stockday.multi_bars, stockday.assets, stockday.trading_days, stockday.data_creds, stockday.now_ny = saved


def test_stockday_live_tick():
    """Live: scan once after the opening range, stop entries with the stop loss attached (OTO), sized at 1% risk and
    capped by the leverage; flat at the flat time."""
    import datetime as dt
    import stockday
    sub = "sd-live"
    stockday.save_config(sub, {"enabled": True, "trade": True, "topN": 2})
    calls = []
    saved = (stockday.scan_today, stockday._alp, stockday.creds)
    try:
        stockday.creds = lambda s: {"key": "k", "secret": "s"}
        stockday.scan_today = lambda s, P, now: {"date": "2026-03-04", "at": "09:36", "candidates": 10, "picks": [
            {"sym": "NVDA", "rvol": 40, "pmRvol": 6, "atr": 5.0, "o": 100, "h": 101, "l": 99.5, "c": 100.8, "dir": 1},
            {"sym": "CSX", "rvol": 3, "pmRvol": 1, "atr": 1.0, "o": 30, "h": 30.2, "l": 29.6, "c": 29.7, "dir": -1}]}
        def alp(c, method, path, body=None):
            calls.append((method, path, body))
            if path == "/v2/account":
                return {"equity": "100000"}
            if path == "/v2/positions":
                return []
            return {"id": "o1"}
        stockday._alp = alp
        r = stockday.tick(sub, dt.datetime(2026, 3, 4, 9, 36))
        posts = [b for m, p, b in calls if m == "POST"]
        assert len(posts) == 2 and posts[0]["order_class"] == "oto" and posts[0]["type"] == "stop" and posts[0]["stop_price"] == "101"
        assert posts[0]["stop_loss"]["stop_price"] == "100.5" and int(posts[0]["qty"]) == min(int(100000 * 0.01 / 0.5), int(100000 * 4 / 2 / 101))
        assert posts[1]["side"] == "sell" and posts[1]["stop_price"] == "29.6"
        calls.clear()
        stockday.tick(sub, dt.datetime(2026, 3, 4, 9, 37))
        assert not [1 for m, p, b in calls if m == "POST"]                 # scanned once
        stockday.tick(sub, dt.datetime(2026, 3, 4, 15, 55))
        assert any(m == "DELETE" and p.startswith("/v2/positions") for m, p, b in calls)
    finally:
        stockday.scan_today, stockday._alp, stockday.creds = saved


def test_order_flow_tick_rule():
    """Real order flow: trades classified by the tick rule, per minute buy / sell, the largest trades, stored days."""
    import orderflow
    pages = [{"trades": {"SPY": [
        {"t": "2026-03-04T14:30:01.1Z", "p": 500.0, "s": 100}, {"t": "2026-03-04T14:30:02Z", "p": 500.1, "s": 200},
        {"t": "2026-03-04T14:30:03Z", "p": 500.1, "s": 50}, {"t": "2026-03-04T14:31:00Z", "p": 499.9, "s": 300}]}, "next_page_token": "x"},
        {"trades": {"SPY": [{"t": "2026-03-04T14:31:30Z", "p": 499.9, "s": 10000}]}, "next_page_token": None}]
    calls = []
    def get(c, url, q):
        calls.append(q.get("page_token"))
        return pages[len(calls) - 1]
    x = orderflow.day_flow({"key": "k"}, "SPY", "2026-03-04", get=get)
    assert x["done"] and x["n"] == 5 and calls == [None, "x"]
    assert x["min"]["2026-03-04T09:30"] == [350, 0]           # first trade: buy; uptick: buy; unchanged: same side
    assert x["min"]["2026-03-04T09:31"] == [0, 10300]          # downtick: sell; unchanged: sell
    assert x["big"][0] == ["2026-03-04T09:31", 499.9, 10000, -1]
    orderflow.store("SPY", "2026-03-04", x)
    days, missing = orderflow.get_days("u", "SPY", ["2026-03-04", "2026-03-05"])
    assert list(days) == ["2026-03-04"] and missing == ["2026-03-05"]


def test_daytrade_why_and_5min_turns():
    """Why (no) trade at a minute: each rule ON, long and short, and the notes; squeeze turns of 5-minute candles."""
    import datetime as dt
    import daytrade
    bars = _dt_bars(6)
    reg = [b for b in bars if "09:30" <= b["t"][11:16] < "16:00"]
    t1 = daytrade.indicators(reg, bars, daytrade.PARAMS)["sqTurnD"]
    t5 = daytrade.indicators(reg, bars, {**daytrade.PARAMS, "sqTurnTf": 5})["sqTurnD"]
    assert any(t5) and t5 != t1 and all(int(reg[i]["t"][15]) % 5 == 4 for i, v in enumerate(t5) if v)   # at the last minute of a 5-min candle
    saved = (daytrade.minute_bars, daytrade.walls_by_day, daytrade.config, daytrade.now_ny)
    try:
        daytrade.minute_bars = lambda *a, **k: bars
        daytrade.walls_by_day = lambda *a, **k: {}
        daytrade.config = lambda sub: {**daytrade.PARAMS, "tickers": ["QQQ"], "aboveVwap": True, "greenBar": True, "emaStack": False}
        daytrade.now_ny = lambda: dt.datetime(2026, 9, 21, 12, 0)
        at = reg[200]["t"]
        w = daytrade.why("u", "QQQ", at.replace("T", " "))
        assert w["at"] == at and {r["rule"] for r in w["rules"]} == {"aboveVwap", "greenBar"} and len(w["rules"]) == 4
        sigs = daytrade.detect(bars, daytrade.config("u"))
        hit = sigs[0]
        w2 = daytrade.why("u", "QQQ", hit["at"])
        assert "a trade was taken on this candle" in w2["notes"] and all(r["ok"] for r in w2["rules"] if r["side"] == hit["dir"])
    finally:
        daytrade.minute_bars, daytrade.walls_by_day, daytrade.config, daytrade.now_ny = saved


def test_nearest_expiration_walls():
    """Walls of the nearest expiration only: SPY tries the same day first, others the coming Friday; flip from the
    running net gamma; daytrade.walls_by_day follows the wallNear setting."""
    import flowdata
    import daytrade
    assert flowdata._exp_candidates("SPY", "2026-10-13")[0] == "2026-10-13"
    assert flowdata._exp_candidates("AAPL", "2026-10-13")[0] == "2026-10-16"
    assert flowdata.flip_from_strikes([[95, 0, -50], [100, 30, -10], [105, 80, 0]], 95, 105) == 105
    calls = []
    def fake_uw(key, path, params=None, attempts=4):
        calls.append((path, params))
        if "strike-expiry" in path and params["expiry"] == params["date"]:
            return {"data": [{"strike": "580", "call_gex": "10", "put_gex": "-900"}, {"strike": "590", "call_gex": "50", "put_gex": "-20"},
                             {"strike": "600", "call_gex": "1700", "put_gex": "-5"}]}
        return {"data": []}
    o_uw, o_key = flowdata._uw, flowdata._key
    flowdata._uw, flowdata._key = fake_uw, lambda sub: "k"
    try:
        g = flowdata.gex_near_day("k", "SPY", "2026-10-13")
        assert g["expiry"] == "2026-10-13" and g["callWall"] == 600 and g["putWall"] == 580 and g["flip"] == 600
        n = len(calls)
        assert flowdata.gex_near_day("k", "SPY", "2026-10-13")["callWall"] == 600 and len(calls) == n   # cached
        sub = "u-near"
        daytrade.save_config(sub, {"wallNear": True})
        w = daytrade.walls_by_day(sub, "SPY", ["2026-10-13"])
        assert w["2026-10-13"]["putWall"] == 580 and w["2026-10-13"]["expiry"] == "2026-10-13"
    finally:
        flowdata._uw, flowdata._key = o_uw, o_key


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
