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
        # caution blocks unless allowed; approve places
        seen["verdict"] = "caution"
        r2 = mk(2)
        try:
            autotrader.place(SUB, r2, placed_by="auto")
            assert False
        except autotrader.BadRequest:
            pass
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
