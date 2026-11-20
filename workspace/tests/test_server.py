import math
"""python -I workspace/tests/test_server.py — market aggregation, fake data, paper broker, websocket client, HTTP API."""
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "server"))
os.environ["DATA"] = "fake"
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ["DATA_DIR"] = tempfile.mkdtemp()

import market  # noqa: E402
from paper import Paper, PaperError  # noqa: E402
from wsclient import WebSocket  # noqa: E402

ok = 0


def check(cond, msg):
    global ok
    if not cond:
        raise AssertionError(msg)
    ok += 1


# ---- timeframes & aggregation
check(market.tf_seconds("5m") == 300 and market.tf_seconds("4h") == 14400 and market.tf_seconds("1D") == 86400, "tf seconds")
check(market.parse_tf("22m") == (22, "m"), "custom tf")
try:
    market.parse_tf("abc"); check(False, "bad tf")
except ValueError:
    check(True, "bad tf raises")
m1 = [[1_700_000_040 + 60 * i, 10 + i, 11 + i, 9 + i, 10.5 + i, 100] for i in range(30)]
agg = market.aggregate(m1, 5, "m")
check(all(b[0] % 300 == 0 for b in agg), "5m buckets aligned")
check(sum(b[5] for b in agg) == 3000, "volume preserved")
check(agg[1][1] == next(b for b in m1 if b[0] == agg[1][0])[1], "open is first minute's open")
# daily bucket = New York midnight
t = 1_763_394_000  # 2025-11-17 15:40 UTC = 10:40 NY
k = market.bucket_start(t, 1, "D")
check(market.datetime.fromtimestamp(k, market.NY).hour == 0, "daily bucket at NY midnight")
check(market.datetime.fromtimestamp(market.bucket_start(t, 1, "W"), market.NY).weekday() == 0, "weekly bucket on Monday")
check(market.datetime.fromtimestamp(market.bucket_start(t, 1, "M"), market.NY).day == 1, "monthly bucket on the 1st")
check(market.AlpacaMarket._plan(90, "m") == ("30Min", (90, "m")) and market.AlpacaMarket._plan(5, "m") == ("5Min", None) and market.AlpacaMarket._plan(2, "D") == ("1Day", (2, "D")), "alpaca plan")

# ---- fake market
fm = market.FakeMarket(tick=0.05)
bars, more = fm.bars("PLTR", "1h", 500)
check(len(bars) == 500 and more, "fake 1h history")
check(all(bars[i][0] < bars[i + 1][0] for i in range(len(bars) - 1)), "sorted")
check(all(b[2] >= max(b[1], b[4]) - 1e-9 and b[3] <= min(b[1], b[4]) + 1e-9 for b in bars), "ohlc consistent")
older, _ = fm.bars("PLTR", "1h", 200, before=bars[0][0])
check(older and older[-1][0] < bars[0][0], "paging before")
d, _ = fm.bars("PLTR", "1D", 1000)
check(len(d) == 1000, "fake daily")
w, _ = fm.bars("BTC/USD", "1W", 100)
check(len(w) == 100, "fake weekly crypto")
c22, _ = fm.bars("AAPL", "22m", 300)
check(len(c22) == 300, "22 minute bars")
got = []
fm.on_bar = lambda s, b: got.append((s, b))
fm.subscribe({"PLTR"})
time.sleep(0.4)
check(got and got[-1][0] == "PLTR", "live ticks")
q = fm.quotes(["PLTR", "AAPL"])
check(q["PLTR"]["last"] > 0 and q["AAPL"]["prev"] > 0, "quotes")

# ---- paper broker
prices = {"XYZ": 100.0}
pp = Paper(os.path.join(os.environ["DATA_DIR"], "p.json"), lambda s: prices.get(s))
o = pp.place({"symbol": "XYZ", "side": "buy", "qty": 10, "type": "market", "sl": 95, "tp": 110})
snap = pp.snapshot()
check(o["status"] == "filled" and snap["positions"][0]["qty"] == 10, "market buy fills")
check(len(snap["orders"]) == 2, "bracket children created")
prices["XYZ"] = 111; pp.on_price("XYZ", 111)
snap = pp.snapshot()
check(not snap["positions"] and not snap["orders"], "take profit closes, stop canceled (OCO)")
check(abs(snap["trades"][0]["pnl"] - 110) < 1e-6 and abs(snap["equity"] - 100110) < 1e-6, f"realized pnl {snap['trades'][0]['pnl']}")
lim = pp.place({"symbol": "XYZ", "side": "sell", "qty": 5, "type": "limit", "price": 115})
check(lim["status"] == "working", "limit rests")
pp.modify(lim["id"], price=112)
prices["XYZ"] = 112.5; pp.on_price("XYZ", 112.5)
snap = pp.snapshot()
check(snap["positions"][0]["qty"] == -5, "short via limit")
pp.place({"symbol": "XYZ", "side": "buy", "qty": 8, "type": "market"})
snap = pp.snapshot()
check(snap["positions"][0]["qty"] == 3 and abs(snap["positions"][0]["avg"] - 112.5) < 1e-9, "flip short to long")
try:
    pp.place({"symbol": "XYZ", "side": "buy", "qty": 100000, "type": "market"}); check(False, "margin")
except PaperError:
    check(True, "margin check")
pp.settings(kill_switch=True)
try:
    pp.place({"symbol": "XYZ", "side": "buy", "qty": 1}); check(False, "kill")
except PaperError:
    check(True, "kill switch blocks")
pp.settings(kill_switch=False)
pp.flatten()
check(not pp.snapshot()["positions"], "flatten")
pp2 = Paper(os.path.join(os.environ["DATA_DIR"], "p.json"), lambda s: prices.get(s))
check(len(pp2.snapshot()["trades"]) == len(pp.snapshot()["trades"]), "persisted")


# ---- websocket client against a tiny local server
def ws_server(sock):
    c, _ = sock.accept()
    req = c.recv(4096).decode()
    key = [ln.split(": ")[1] for ln in req.split("\r\n") if ln.lower().startswith("sec-websocket-key")][0]
    acc = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    c.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n".encode())
    h = c.recv(2); n = h[1] & 0x7F; mask = c.recv(4); data = bytes(b ^ mask[i % 4] for i, b in enumerate(c.recv(n)))
    c.sendall(bytes([0x89, 0]))  # ping
    big = json.dumps([{"T": "t", "echo": data.decode(), "pad": "x" * 300}]).encode()
    c.sendall(bytes([0x81, 126]) + struct.pack(">H", len(big)) + big)
    c.recv(16)  # pong
    c.sendall(bytes([0x88, 0]))
    c.close()


srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
threading.Thread(target=ws_server, args=(srv,), daemon=True).start()
ws = WebSocket(f"ws://127.0.0.1:{srv.getsockname()[1]}/v2/iex")
ws.send('{"action":"auth"}')
msg = json.loads(ws.recv())
check(msg[0]["echo"] == '{"action":"auth"}', "ws echo through ping + 16-bit length")
try:
    ws.recv(); check(False, "close")
except Exception as e:
    check("closed" in str(e), "ws close")

# ---- HTTP API
import server  # noqa: E402

httpd = server.Server(("127.0.0.1", 0), server.Handler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"


def get(p):
    with urllib.request.urlopen(base + p, timeout=20) as r:
        return json.loads(r.read())


def post(p, body):
    req = urllib.request.Request(base + p, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


import urllib.error  # noqa: E402

check(get("/api/config")["data"] == "fake", "config")
b = get("/api/bars?symbol=TSLA&tf=15m&limit=300")
check(len(b["bars"]) == 300, "bars endpoint")
check(any(r["symbol"] == "TSLA" for r in get("/api/symbols?q=tes")["results"]), "search")
code, _ = post("/api/paper/order", {"symbol": "TSLA", "side": "buy", "qty": 2, "type": "market"})
check(code == 200 and get("/api/paper")["positions"][0]["symbol"] == "TSLA", "paper order via api")
code, body = post("/api/paper/order", {"symbol": "TSLA", "side": "buy", "qty": 0})
check(code == 400 and b"Quantity" in body, "api validation error")
code, body = post("/api/chat", {"messages": [{"role": "user", "content": "mark up my chart"}]})
check(code == 200 and b"get_chart_state" in body and b"message_stop" in body, "demo chat stream")
with urllib.request.urlopen(base + "/api/stream?symbols=TSLA", timeout=30) as r:
    seen = b""
    t0 = time.time()
    while not (b"event: bar" in seen and b"event: paper" in seen) and time.time() - t0 < 20:
        seen += r.read1(4096)
check(b"event: bar" in seen and b"event: paper" in seen, "event stream: " + repr(seen[:300]))
with urllib.request.urlopen(base + "/", timeout=10) as r:
    check(b"<!doctype html>" in r.read().lower(), "index served")
httpd.shutdown()
print(f"all {ok} server checks passed")


# ---- journal sign-in (Cognito ID token, RS256) and the /ws/ prefix
import hashlib  # noqa: E402
import random  # noqa: E402

import cognito  # noqa: E402


def _prime(bits, rnd):
    while True:
        n = rnd.getrandbits(bits) | (1 << (bits - 1)) | 1
        if all(pow(a, n - 1, n) == 1 for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37)):
            return n


rnd = random.Random(42)
p_, q_ = _prime(512, rnd), _prime(512, rnd)
N, E = p_ * q_, 65537
D = pow(E, -1, (p_ - 1) * (q_ - 1))
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
iint = lambda i: b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))


def jwt(claims, kid="k1"):
    h = b64(json.dumps({"alg": "RS256", "kid": kid}).encode()); pl = b64(json.dumps(claims).encode())
    t = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(f"{h}.{pl}".encode()).digest()
    k = (N.bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return f"{h}.{pl}.{iint(pow(int.from_bytes(em, 'big'), D, N))}"


jwks = {"keys": [{"kid": "k1", "kty": "RSA", "n": iint(N), "e": iint(E)}]}
V = cognito.Verifier("us-east-1", "us-east-1_POOL", "client1", fetch=lambda url: jwks)
good = {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_POOL", "aud": "client1", "token_use": "id", "exp": time.time() + 600, "email": "a@b.c"}
check(V.verify(jwt(good))["email"] == "a@b.c", "valid token")
check(V.verify(jwt({**good, "aud": "other"})) is None, "wrong audience")
check(V.verify(jwt({**good, "exp": time.time() - 100})) is None, "expired")
check(V.verify(jwt(good)[:-4] + "AAAA") is None, "bad signature")
check(V.verify(jwt({**good, "token_use": "access"})) is None, "access token refused")

server.COGNITO = V
httpd = server.Server(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"
with urllib.request.urlopen(base + "/ws/", timeout=10) as r:
    check(b"app.js" in r.read(), "page served under /ws/ without a token")
try:
    urllib.request.urlopen(base + "/ws/api/config", timeout=10); check(False, "api needs token")
except urllib.error.HTTPError as e:
    check(e.code == 401 and b"signin" in e.read(), "api refused without the journal token")
req = urllib.request.Request(base + "/ws/api/config", headers={"Authorization": "Bearer " + jwt(good)})
with urllib.request.urlopen(req, timeout=10) as r:
    check(json.loads(r.read())["data"] == "fake", "api with the journal token under /ws/")
with urllib.request.urlopen(base + "/ws/api/stream?symbols=TSLA&token=" + jwt(good), timeout=30) as r:
    check(b"retry" in r.read1(4096), "stream with token in the query")
server.ORIGIN_SECRET = "s3cret"
try:
    urllib.request.urlopen(req, timeout=10); check(False, "origin secret")
except urllib.error.HTTPError as e:
    check(e.code == 403, "requests that skip CloudFront are refused")
server.ORIGIN_SECRET = ""
httpd.shutdown()
print(f"all {ok} server checks passed (with sign-in)")

# ---- saved conversations
server.COGNITO = None
httpd = server.Server(("127.0.0.1", 0), server.Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{httpd.server_address[1]}"
req = urllib.request.Request(base + "/api/chats/cabc12345", data=json.dumps({"title": "QQQ study", "symbol": "QQQ", "messages": [{"role": "user", "content": "hi"}], "ui": [{"k": "user", "text": "hi"}]}).encode(), method="PUT", headers={"Content-Type": "application/json"})
urllib.request.urlopen(req, timeout=10).read()
lst = get("/api/chats") if False else json.loads(urllib.request.urlopen(base + "/api/chats", timeout=10).read())
check(lst["chats"][0]["title"] == "QQQ study" and lst["chats"][0]["n"] == 1, "chat saved and listed")
check(json.loads(urllib.request.urlopen(base + "/api/chats/cabc12345", timeout=10).read())["messages"][0]["content"] == "hi", "chat loaded")
urllib.request.urlopen(urllib.request.Request(base + "/api/chats/cabc12345", method="DELETE"), timeout=10).read()
check(not json.loads(urllib.request.urlopen(base + "/api/chats", timeout=10).read())["chats"], "chat deleted")
try:
    urllib.request.urlopen(base + "/api/chats/..%2Fpaper", timeout=10); check(False, "bad id")
except urllib.error.HTTPError as e:
    check(e.code in (400, 404), "bad conversation id refused")
b = json.loads(urllib.request.urlopen(base + "/api/bars?symbol=QQQ&tf=5m&days=30&feed=sip", timeout=30).read())
check(len(b["bars"]) > 2000, f"days-based history ({len(b['bars'])} bars)")
httpd.shutdown()

# ---- NQ / ES from the journal's Databento month files, frozen strategies (Node runs the Pine strategy)
import gzip  # noqa: E402
from datetime import datetime as _dt, timedelta as _td  # noqa: E402
fdir = tempfile.mkdtemp()
now_ny = _dt.now(market.NY).replace(second=0, microsecond=0, tzinfo=None)
rows, t, px = {}, now_ny - _td(days=4), 20000.0
while t <= now_ny:
    rows.setdefault(t.strftime("%Y-%m"), []).append([t.strftime("%Y-%m-%dT%H:%M"), px, px + 2, px - 2, px + 0.5, 10])
    px += 0.5 if t.minute % 7 else -1.5
    t += _td(minutes=1)
os.makedirs(os.path.join(fdir, "NQ", "1m"))
for ym, b in rows.items():
    with open(os.path.join(fdir, "NQ", "1m", ym + ".json.gz"), "wb") as fh:
        fh.write(gzip.compress(json.dumps({"b": b}).encode()))
fut = market.FuturesData("", fdir)
check(fut._epoch("2026-11-02T09:30") == 1793629800 and fut._epoch("2026-07-01T09:30") == 1782912600, "NY time -> epoch (EST / EDT)")
nb, _ = fut.bars("NQ", "5m", 300)
check(len(nb) == 300 and all(b[0] % 300 == 0 for b in nb) and nb[-1][0] > time.time() - 600, f"NQ 5m from the month files ({len(nb)})")
wf = market.WithFutures(market.FakeMarket(), fut)
check(wf.search("nq")[0]["symbol"] == "NQ" and wf.bars("QQQ", "5m", 50)[0], "futures merged with the base market")
check(wf.quotes(["NQ"])["NQ"]["last"] == nb[-1][4], "futures quote")

import frozen  # noqa: E402
SRC = '//@version=6\nstrategy("every 6", overlay = true)\nn = request.security("NQ", timeframe.period, close)\nif bar_index % 6 == 0 and strategy.position_size == 0 and n > 0\n    strategy.entry("L", strategy.long, 2)\nif bar_index % 6 == 3\n    strategy.close_all()'
if frozen.node_bin():
    pp = Paper(os.path.join(tempfile.mkdtemp(), "p.json"), wf.last)
    wf.cur["QQQ"] = [0, 600, 600, 600, 600, 1]
    fz = frozen.Frozen(os.path.join(tempfile.mkdtemp(), "fz.json"), wf, pp, interval=0)
    v = fz.create({"name": "t", "src": SRC, "symbol": "QQQ", "tf": "5m", "sec": [{"sym": "NQ", "tf": "5m"}], "account": "paper"})
    time.sleep(0.2)
    while v["id"] in fz.busy:
        time.sleep(0.2)
    fz.s["items"][v["id"]]["frozenAt"] = int(time.time()) - 86400 * 2      # as if frozen two days ago
    fz.run_one(v["id"])
    g = fz.get(v["id"])
    check(not g["error"], f"frozen run: {g['error']}")
    check(g["metrics"]["n"] > 10 and all(t["entryTime"] >= g["frozenAt"] for t in g["trades"]), f"forward trades only ({g['metrics']['n']})")
    want = sum(o["qty"] * o["dir"] for o in g["open"])
    check(g["mirror"] == want and (pp.s["positions"].get("QQQ", {}).get("qty", 0) == want), "position mirrored in the paper account")
    fz.action(v["id"], "stop")
    check(not pp.s["positions"].get("QQQ") and fz.get(v["id"])["status"] == "off", "stop flattens the paper position")
    bad = fz.create({"name": "x", "src": '//@version=6\nstrategy("x")\nplot(nope)', "symbol": "QQQ", "tf": "5m"})
    while bad["id"] in fz.busy or not fz.get(bad["id"])["lastRun"]:
        time.sleep(0.2)
    check("nope" in (fz.get(bad["id"])["error"] or ""), "a script error is shown")
    try:
        fz.create({"src": '//@version=6\nindicator("x")', "symbol": "QQQ", "tf": "5m"}); check(False, "indicator frozen")
    except frozen.FrozenError:
        check(True, "only strategies freeze")
else:
    print("(node not found: frozen strategy checks skipped)")

# ---- Alpaca paper mirror (a fake Alpaca: positions, orders, OCO)
import alpaca_paper  # noqa: E402


class FakeAlp(alpaca_paper.AlpacaPaper):
    def __init__(self):
        super().__init__("k", "s")
        self.pos, self.ords, self.n, self.sent = {}, [], 0, []

    def req(self, method, path, body=None, timeout=20):
        if method == "GET" and path.startswith("positions/"):
            q = self.pos.get(path.split("/")[1], 0)
            return {"qty": str(q)} if q else None
        if method == "GET" and path.startswith("orders?"):
            return [o for o in self.ords if o["status"] == "new"]
        if method == "GET" and path.startswith("orders/"):
            return next(o for o in self.ords if o["id"] == path.split("/")[1])
        if method == "DELETE":
            for o in self.ords:
                if o["id"] == path.split("/")[1]:
                    o["status"] = "canceled"
            return None
        if method == "POST":
            self.n += 1
            self.sent.append(body)
            o = dict(body, id=str(self.n), status="new")
            if body.get("order_class") == "oco":
                o["legs"] = [{"type": "stop", "stop_price": body["stop_loss"]["stop_price"]}]
                o["limit_price"] = body["take_profit"]["limit_price"]
            if body["type"] == "market":
                o["status"] = "filled"
                self.pos[body["symbol"]] = self.pos.get(body["symbol"], 0) + float(body["qty"]) * (1 if body["side"] == "buy" else -1)
            self.ords.append(o)
            return o
        raise AssertionError(path)


alp = FakeAlp()
fx = {"id": "fabc1234567", "symbol": "QQQ"}
alpaca_paper.AlpacaPaper.rth = staticmethod(lambda now=None: True)
log = alpaca_paper.sync(alp, fx, [{"qty": 100, "dir": 1}], [{"dir": 1, "stop": 600.123, "limit": 612.5}], 605)
check(alp.pos["QQQ"] == 100 and alp.sent[0]["type"] == "market" and alp.sent[0]["client_order_id"].startswith("fz-fabc1234567"), f"entry mirrored on Alpaca ({log})")
check(alp.sent[1]["order_class"] == "oco" and alp.sent[1]["stop_loss"]["stop_price"] == "600.12" and alp.sent[1]["take_profit"]["limit_price"] == "612.50" and alp.sent[1]["side"] == "sell", "stop + target placed as an OCO")
check(alpaca_paper.sync(alp, fx, [{"qty": 100, "dir": 1}], [{"dir": 1, "stop": 600.12, "limit": 612.5}], 606) is None and len(alp.sent) == 2, "nothing resent when unchanged")
alpaca_paper.sync(alp, fx, [{"qty": 100, "dir": 1}], [{"dir": 1, "stop": 605, "limit": 612.5}], 607)
check(alp.sent[-1]["stop_loss"]["stop_price"] == "605.00" and sum(o["status"] == "new" for o in alp.ords) == 1, "moved stop: old OCO canceled, new one placed")
alpaca_paper.sync(alp, fx, [], [], 608)
check(alp.pos["QQQ"] == 0 and not [o for o in alp.ords if o["status"] == "new"], "strategy flat: exits canceled, position sold")
alpaca_paper.AlpacaPaper.rth = staticmethod(lambda now=None: False)
alpaca_paper.sync(alp, fx, [{"qty": 10, "dir": -1}], [], 600)
check(alp.sent[-1]["type"] == "limit" and alp.sent[-1]["extended_hours"] and alp.sent[-1]["limit_price"] == "598.80", "outside regular hours: marketable limit, extended hours")

# ---- options: Black-Scholes, OCC symbols, simulated and Alpaca chains, the API
import options  # noqa: E402
EXP = (_dt.now(market.NY) + _td(days=30)).strftime('%Y-%m-%d')
YY = EXP[2:4] + EXP[5:7] + EXP[8:10]
pc = options.bs(100, 100, 0.25, 0.2, True)[0]
pp = options.bs(100, 100, 0.25, 0.2, False)[0]
check(abs(pc - pp - (100 - 100 * math.exp(-options.R * 0.25))) < 1e-9 and abs(pc - 4.4793) < 0.01, f"Black-Scholes + put-call parity ({pc:.4f})")
check(abs(options.implied_vol(pc, 100, 100, 0.25, True) - 0.2) < 1e-4, "implied volatility")
check(options.parse_occ("QQQ261120C00650000") == ("QQQ", "2026-11-20", True, 650.0) and options.occ("QQQ", "2026-11-20", False, 647.5) == "QQQ261120P00647500", "OCC symbols")


class FakeOptAlp:
    def configured(self):
        return True

    def req(self, method, path, body=None, timeout=20, base=None):
        if path.startswith("options/contracts"):
            return {"option_contracts": [{"symbol": f"QQQ{YY}C00600000", "expiration_date": EXP, "strike_price": "600", "type": "call", "open_interest": "1200"},
                                         {"symbol": f"QQQ{YY}P00600000", "expiration_date": EXP, "strike_price": "600", "type": "put", "open_interest": "800"}]}
        if path.startswith("options/snapshots/QQQ"):
            assert base == options.DATA
            return {"snapshots": {f"QQQ{YY}C00600000": {"latestQuote": {"bp": 300, "ap": 302}, "greeks": {"delta": 0.7}, "impliedVolatility": 0.25},
                                  f"QQQ{YY}P00600000": {"latestQuote": {"bp": 60, "ap": 62}}}}
        raise AssertionError(path)


op = options.Options(FakeOptAlp(), lambda s: 650.0)
check(op.expirations("QQQ") == [EXP], "expirations from the contracts")
ch = op.chain("QQQ", EXP)
r0 = ch["rows"][0]
check(r0["strike"] == 600 and r0["call"]["delta"] == 0.7 and r0["call"]["oi"] == 1200 and r0["put"]["model"] and -1 < r0["put"]["delta"] < 0, "Alpaca chain: greeks kept, missing ones filled")
fo = options.Options(None, lambda s: 600.0)
fc = fo.chain("QQQ", fo.expirations("QQQ")[0])
check(fc["source"] == "simulated" and len(fc["rows"]) > 40 and all(0 <= r["call"]["delta"] <= 1 for r in fc["rows"]), "simulated chain")
print(f"all {ok} server checks passed (chats, research history, futures, frozen, alpaca, options)")
