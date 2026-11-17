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
