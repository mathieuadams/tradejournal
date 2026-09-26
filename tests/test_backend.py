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

    for name, fn in dict(q_prefix=q_prefix, get=get, put=put, delete=delete, update=update,
                         batch_write=batch_write, scan_sk=scan_sk).items():
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


def test_analytics():
    base = dict(status="closed", setup="", tags=[], r=None, mfe=None, min=600, date="2026-09-21")
    ts = [dict(base, openTs=f"2026-09-21T10:0{i}:00", net=n) for i, n in enumerate([-100, -50, -80, 200, -60])]
    s = analytics.stats(ts)
    assert s["trades"] == 5 and s["net"] == -90 and s["max_drawdown"] == -230
    ts[2]["tags"] = ["Revenge"]
    L = analytics.leaks(ts)
    assert L["leaks"][0]["label"] == "Revenge trades" and L["leaks"][0]["dollars"] == -80


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
