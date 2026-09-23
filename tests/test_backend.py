"""Run with: python -m pytest tests -q   (or: python tests/test_backend.py)
No AWS account needed: DynamoDB, S3 and the Claude API are replaced by in-memory fakes."""
import json
import os
import sys

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


def fake_claude(model, system, msgs, max_tokens=1500, tools=None):
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
