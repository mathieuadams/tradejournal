"""Trading workspace server: web app, market data (history + live), paper broker, Claude proxy. Standard library only.

    python workspace/server/server.py            # http://localhost:8765
    DATA=fake | alpaca      (default: alpaca when ALPACA_KEY_ID / ALPACA_SECRET_KEY are set, else fake)
    ALPACA_FEED=iex | sip   ANTHROPIC_API_KEY=...   CLAUDE_MODEL=claude-sonnet-5-5
    WORKSPACE_PASSWORD=...  (HTTP basic auth for everything; set it when the server is reachable from the internet)
    PORT=8765  HOST=127.0.0.1  DATA_DIR=workspace/.data
"""
import base64
import hmac
import json
import mimetypes
import os
import queue
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")
sys.path.insert(0, HERE)

import llm  # noqa: E402
import market  # noqa: E402
from paper import Paper, PaperError  # noqa: E402

PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "127.0.0.1")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(HERE), ".data"))
PASSWORD = os.environ.get("WORKSPACE_PASSWORD", "")
ORIGIN_SECRET = os.environ.get("ORIGIN_SECRET", "")  # set when CloudFront fronts the server: requests without it are refused
os.makedirs(DATA_DIR, exist_ok=True)


def make_market():
    mode = os.environ.get("DATA")
    key, secret = os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    if mode == "alpaca" or (mode is None and key and secret):
        if not (key and secret):
            print("DATA=alpaca needs ALPACA_KEY_ID and ALPACA_SECRET_KEY; using fake data")
            return market.FakeMarket()
        return market.AlpacaMarket(key, secret, os.environ.get("ALPACA_FEED", "iex"))
    return market.FakeMarket()


class Hub:
    """Fans live minute bars and paper events out to the browsers' event streams."""

    def __init__(self, mkt):
        self.mkt, self.clients, self.lock = mkt, {}, threading.Lock()
        mkt.on_bar = self.on_bar
        self.paper = Paper(os.path.join(DATA_DIR, "paper.json"), mkt.last, self.on_paper)

    def add(self, symbols):
        q = queue.Queue(maxsize=5000)
        with self.lock:
            self.clients[id(q)] = (q, set(symbols))
            self._resub()
        return q

    def remove(self, q):
        with self.lock:
            self.clients.pop(id(q), None)
            self._resub()

    def _resub(self):
        want = set()
        for _, syms in self.clients.values():
            want |= syms
        want |= set(self.paper.s["positions"].keys())
        want |= {o["symbol"] for o in self.paper.s["orders"] if o["status"] == "working"}
        self.mkt.subscribe(want)

    def on_bar(self, sym, bar):
        try:
            self.paper.on_price(sym, bar[4])
        except Exception as e:  # pragma: no cover
            print("paper on_price failed:", e)
        msg = ("bar", {"s": sym, "b": bar})
        with self.lock:
            for q, syms in self.clients.values():
                if sym in syms:
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass

    def on_paper(self, event):
        snap = self.paper.snapshot()
        with self.lock:
            for q, _ in self.clients.values():
                try:
                    q.put_nowait(("paper", {"event": event, "account": snap}))
                except queue.Full:
                    pass
        threading.Thread(target=lambda: (time.sleep(0.05), self._locked_resub()), daemon=True).start()

    def _locked_resub(self):
        with self.lock:
            self._resub()


MKT = make_market()
HUB = Hub(MKT)
STATE_PATH = os.path.join(DATA_DIR, "workspace.json")
STATE_LOCK = threading.Lock()


class BadRequest(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "TradeWorkspace/1.0"

    def log_message(self, fmt, *args):
        if "/api/stream" not in self.path:
            sys.stderr.write("  %s %s\n" % (self.command, self.path[:120]))

    def _authed(self):
        if not PASSWORD:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, pw = base64.b64decode(h[6:]).decode().split(":", 1)
                return hmac.compare_digest(pw, PASSWORD)
            except Exception:
                return False
        return False

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":")).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 20_000_000:
            raise BadRequest("Request too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise BadRequest("Body is not JSON")

    def do_GET(self):
        self._route()

    def do_POST(self):
        self._route()

    def do_PUT(self):
        self._route()

    def _route(self):
        if ORIGIN_SECRET and not hmac.compare_digest(self.headers.get("X-Origin-Secret", ""), ORIGIN_SECRET):
            return self._send(403, b"Forbidden", "text/plain")
        if not self._authed():
            return self._send(401, b"Sign in", "text/plain", {"WWW-Authenticate": 'Basic realm="Trading workspace"'})
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        path = url.path
        try:
            if path.startswith("/api/"):
                return self._api(path[5:], q)
            return self._static(path)
        except (BadRequest, PaperError, ValueError) as e:
            return self._json({"error": str(e)}, 400)
        except market.AlpacaError as e:
            return self._json({"error": str(e)}, 502)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # pragma: no cover
            import traceback
            traceback.print_exc()
            return self._json({"error": f"Server error: {e}"}, 500)

    def _static(self, path):
        if self.command != "GET":
            return self._send(405)
        rel = path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(WEB, rel))
        if not full.startswith(WEB) or not os.path.isfile(full):
            full = os.path.join(WEB, "index.html")
        with open(full, "rb") as f:
            data = f.read()
        ctype = "text/javascript" if full.endswith((".js", ".mjs")) else (mimetypes.guess_type(full)[0] or "application/octet-stream")
        self._send(200, data, ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))

    def _api(self, p, q):
        m = self.command
        if p == "config":
            return self._json({"data": MKT.name, "llm": "anthropic" if llm.configured() else "demo", "model": llm.MODEL, "feed": os.environ.get("ALPACA_FEED", "iex") if MKT.name == "alpaca" else None})
        if p == "symbols":
            return self._json({"results": MKT.search(q.get("q", ""))})
        if p == "bars":
            sym = (q.get("symbol") or "").upper()
            if not sym:
                raise BadRequest("symbol is required")
            limit = max(10, min(5000, int(q.get("limit", 1500))))
            before = int(q["before"]) if q.get("before") else None
            bars, more = MKT.bars(sym, q.get("tf", "1h"), limit, before)
            return self._json({"symbol": sym, "tf": q.get("tf", "1h"), "bars": bars, "more": more and len(bars) > 0, "name": market.NAMES.get(sym, sym)})
        if p == "quotes":
            syms = [s for s in (q.get("symbols") or "").upper().split(",") if s][:60]
            return self._json({"quotes": MKT.quotes(syms) if syms else {}})
        if p == "stream":
            return self._stream([s for s in (q.get("symbols") or "").upper().split(",") if s][:60])
        if p == "chat" and m == "POST":
            return self._chat(self._body())
        if p == "state":
            if m == "GET":
                with STATE_LOCK:
                    try:
                        with open(STATE_PATH) as f:
                            return self._send(200, f.read().encode())
                    except FileNotFoundError:
                        return self._json({})
            body = self._body()
            with STATE_LOCK:
                tmp = STATE_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(body, f)
                os.replace(tmp, STATE_PATH)
            return self._json({"ok": True})
        if p.startswith("paper"):
            return self._paper(p[5:].strip("/"), m)
        return self._json({"error": "Not found"}, 404)

    def _paper(self, action, m):
        P = HUB.paper
        if action == "" and m == "GET":
            return self._json(P.snapshot())
        if m != "POST":
            return self._json({"error": "Use POST"}, 405)
        b = self._body()
        if action == "order":
            HUB.mkt.subscribe(HUB.mkt.subs | {str(b.get("symbol", "")).upper()})
            o = P.place(b)
        elif action == "cancel":
            o = P.cancel(b.get("id"))
        elif action == "modify":
            o = P.modify(b.get("id"), b.get("price"), b.get("sl"), b.get("tp"))
        elif action == "close":
            o = P.close(str(b.get("symbol", "")).upper(), b.get("source", "manual"))
        elif action == "flatten":
            P.flatten(); o = None
        elif action == "reset":
            P.reset(b.get("balance") or 100000); o = None
        elif action == "settings":
            P.settings(**b); o = None
        else:
            return self._json({"error": "Not found"}, 404)
        return self._json({"order": o, "account": P.snapshot()})

    def _stream(self, symbols):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = HUB.add(symbols)
        try:
            self.wfile.write(b"retry: 2000\n\n")
            for s in symbols:  # current minute bar right away
                b = MKT.cur.get(s)
                if b:
                    self.wfile.write(f"event: bar\ndata: {json.dumps({'s': s, 'b': b})}\n\n".encode())
            self.wfile.write(f"event: paper\ndata: {json.dumps({'event': None, 'account': HUB.paper.snapshot()})}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    kind, data = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                chunk = [f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"]
                while not q.empty() and len(chunk) < 200:
                    kind, data = q.get_nowait()
                    chunk.append(f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n")
                self.wfile.write("".join(chunk).encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            HUB.remove(q)

    def _chat(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def write(b):
            self.wfile.write(b)
            self.wfile.flush()
        try:
            llm.stream(body, write)
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    print(f"Trading workspace at http://{HOST}:{PORT}  |  data: {MKT.name}  |  AI: {'Claude ' + llm.MODEL if llm.configured() else 'demo (set ANTHROPIC_API_KEY)'}"
          f"{'  |  password on' if PASSWORD else ''}")
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
