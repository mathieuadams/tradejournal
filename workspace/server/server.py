"""Trading workspace server: web app, market data (history + live), paper broker, Claude proxy. Standard library only.

    python workspace/server/server.py            # http://localhost:8765
    DATA=fake | alpaca      (default: alpaca when ALPACA_KEY_ID / ALPACA_SECRET_KEY are set, else fake)
    ALPACA_FEED=iex | sip   ANTHROPIC_API_KEY=...   CLAUDE_MODEL=claude-sonnet-5-5
    COGNITO_USER_POOL_ID / COGNITO_CLIENT_ID / COGNITO_REGION: sign in with the journal app (its ID token is required on /api)
    WORKSPACE_PASSWORD=...  (HTTP basic auth instead, for a server without the journal)
    Served at / and at /ws/ (the journal's CloudFront sends /ws* here).
    PORT=8765  HOST=127.0.0.1  DATA_DIR=workspace/.data
"""
import base64
import hmac
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")
sys.path.insert(0, HERE)

import cognito  # noqa: E402
import llm  # noqa: E402
import market  # noqa: E402
from paper import Paper, PaperError  # noqa: E402
from frozen import Frozen, FrozenError  # noqa: E402
import alpaca_paper  # noqa: E402
import options  # noqa: E402

PORT = int(os.environ.get("PORT", "8765"))
HOST = os.environ.get("HOST", "127.0.0.1")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(HERE), ".data"))
PASSWORD = os.environ.get("WORKSPACE_PASSWORD", "")
ORIGIN_SECRET = os.environ.get("ORIGIN_SECRET", "")  # set when CloudFront fronts the server: requests without it are refused
os.makedirs(DATA_DIR, exist_ok=True)
COGNITO = cognito.Verifier(os.environ.get("COGNITO_REGION", "us-east-1"), os.environ["COGNITO_USER_POOL_ID"], os.environ.get("COGNITO_CLIENT_ID", "")) \
    if os.environ.get("COGNITO_USER_POOL_ID") else None
ROOT = os.path.dirname(os.path.dirname(HERE))


def make_market():
    mode = os.environ.get("DATA")
    key, secret = os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    if mode == "alpaca" or (mode is None and key and secret):
        if not (key and secret):
            print("DATA=alpaca needs ALPACA_KEY_ID and ALPACA_SECRET_KEY; using fake data")
            return with_futures(market.FakeMarket())
        return with_futures(market.AlpacaMarket(key, secret, os.environ.get("ALPACA_FEED", "iex")))
    return with_futures(market.FakeMarket())


def with_futures(base):
    """NQ / ES from the journal's Databento candles (FUT_BUCKET on the server, FUT_DIR locally) when configured."""
    fut = market.FuturesData(os.environ.get("FUT_BUCKET", ""), os.environ.get("FUT_DIR", ""))
    return market.WithFutures(base, fut) if fut.available() else base


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
        fz = globals().get("FROZEN")
        if fz:
            want |= {f["symbol"] for f in fz.s["items"].values() if f["status"] == "on" and f["account"] == "paper"}
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
ALPACA = alpaca_paper.AlpacaPaper(os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", ""))
OPTIONS = options.Options(ALPACA, MKT.last)
FROZEN = Frozen(os.path.join(DATA_DIR, "frozen.json"), MKT, HUB.paper, interval=0 if os.environ.get("FROZEN_OFF") else 20, alpaca=ALPACA)
STATE_PATH = os.path.join(DATA_DIR, "workspace.json")
STATE_LOCK = threading.Lock()


class BadRequest(Exception):
    pass


class Handler(BaseHTTPRequestHandler):
    server_version = "TradeWorkspace/1.0"

    def log_message(self, fmt, *args):
        if "/api/stream" not in self.path:
            sys.stderr.write("  %s %s\n" % (self.command, self.path[:120]))

    def _authed(self, api, q):
        if COGNITO:
            if not api:
                return True                                   # the page itself; every /api call needs the token
            h = self.headers.get("Authorization", "")
            tok = h[7:] if h.startswith("Bearer ") else q.get("token", "")
            return COGNITO.verify(tok) is not None
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

    def do_DELETE(self):
        self._route()

    def _route(self):
        if ORIGIN_SECRET and not hmac.compare_digest(self.headers.get("X-Origin-Secret", ""), ORIGIN_SECRET):
            return self._send(403, b"Forbidden", "text/plain")
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        path = url.path
        if path == "/ws":
            return self._send(301, b"", "text/plain", {"Location": "/ws/"})
        if path.startswith("/ws/"):
            path = path[3:]
        if not self._authed(path.startswith("/api/"), q):
            if COGNITO:
                return self._json({"error": "Sign in to the journal first.", "signin": True}, 401)
            return self._send(401, b"Sign in", "text/plain", {"WWW-Authenticate": 'Basic realm="Trading workspace"'})
        try:
            if path.startswith("/api/"):
                return self._api(path[5:], q)
            return self._static(path)
        except (BadRequest, PaperError, FrozenError, alpaca_paper.AlpacaError, ValueError) as e:
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
        if path in ("/config.js", "/auth.js"):                # the journal's sign-in, when this server runs without the journal in front
            src = os.path.join(ROOT, "frontend", "auth.js")
            body = b"window.TJ_CONFIG = window.TJ_CONFIG || { local: true };" if path == "/config.js" else (open(src, "rb").read() if os.path.isfile(src) else b"")
            return self._send(200, body, "text/javascript; charset=utf-8")
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
            tf = q.get("tf", "1h")
            if q.get("days"):        # research: N trading days of history (extended hours included for stocks)
                days, sec = max(1, min(2000, int(q["days"]))), market.tf_seconds(tf)
                per_day = (86400 if market.is_crypto(sym) else 82800 if sym in market.FUTURES else 57600) if sec < 86400 else 86400
                limit = max(10, min(150000, -(-days * per_day // sec)))
            else:
                limit = max(10, min(5000, int(q.get("limit", 1500))))
            before = int(q["before"]) if q.get("before") else None
            bars, more = MKT.bars(sym, tf, limit, before, feed=q.get("feed"))
            return self._json({"symbol": sym, "tf": q.get("tf", "1h"), "bars": bars, "more": more and len(bars) > 0, "name": market.NAMES.get(sym) or market.FUTURES.get(sym, sym)})
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
        if p.startswith("options/"):
            return self._options(p[8:], q, m)
        if p == "alpaca" and m == "GET":
            # the Alpaca paper account the frozen strategies trade in
            if not ALPACA.configured():
                return self._json({"configured": False})
            orders = [o for o in ALPACA.orders("all", 30) if (o.get("client_order_id") or "").startswith("fz-")]
            names = {f["id"]: f["name"] for f in FROZEN.s["items"].values()}
            return self._json({"configured": True, "account": ALPACA.account(), "positions": ALPACA.positions(),
                               "orders": [{"symbol": o["symbol"], "side": o["side"], "qty": o.get("qty"), "type": o.get("type"), "status": o.get("status"),
                                           "filled_avg_price": o.get("filled_avg_price"), "stop": o.get("stop_price"), "limit": o.get("limit_price"),
                                           "at": o.get("filled_at") or o.get("submitted_at"), "strategy": names.get((o.get("client_order_id") or "")[3:14], "")} for o in orders[:20]]})
        if p == "frozen" or p.startswith("frozen/"):
            return self._frozen(p[7:], m)
        if p == "chats" or p.startswith("chats/"):
            return self._chats(p[6:], m)
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

    def _options(self, what, q, m):
        """Option chains (Alpaca, or Black-Scholes on simulated data) and option orders on the Alpaca paper account."""
        sym = re.sub(r"[^A-Z.]", "", (q.get("symbol") or "").upper())[:10]
        if what == "expirations" and m == "GET":
            return self._json({"symbol": sym, "expirations": OPTIONS.expirations(sym), "source": "alpaca" if OPTIONS.live() else "simulated",
                               "spot": MKT.last(sym)})
        if what == "chain" and m == "GET":
            exp = q.get("exp") or ""
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp):
                raise BadRequest("exp=YYYY-MM-DD")
            return self._json(OPTIONS.chain(sym, exp))
        if what == "positions" and m == "GET":
            if not ALPACA.configured():
                return self._json({"configured": False, "positions": []})
            ps = [p for p in ALPACA.req("GET", "positions") or [] if p.get("asset_class") == "us_option"]
            return self._json({"configured": True, "positions": [{"symbol": p["symbol"], "qty": float(p["qty"]), "avg": float(p["avg_entry_price"]),
                               "price": float(p.get("current_price") or 0), "upl": float(p.get("unrealized_pl") or 0), "value": float(p.get("market_value") or 0)} for p in ps]})
        if what == "order" and m == "POST":
            b = self._body()
            osym = str(b.get("symbol") or "").upper()
            if not re.fullmatch(r"[A-Z.]{1,6}\d{6}[CP]\d{8}", osym):
                raise BadRequest("Bad option symbol")
            if not ALPACA.configured():
                raise BadRequest("Option orders go to your Alpaca paper account: no Alpaca key is loaded on the workspace server.")
            qty, side = int(b.get("qty") or 0), b.get("side")
            if qty < 1 or side not in ("buy", "sell"):
                raise BadRequest("qty >= 1 and side buy / sell")
            body = {"symbol": osym, "qty": str(qty), "side": side, "time_in_force": "day"}
            if b.get("type") == "market":
                body["type"] = "market"
            else:
                lp = float(b.get("limit") or 0)
                if lp <= 0:
                    raise BadRequest("limit price required")
                body.update(type="limit", limit_price=f"{lp:.2f}")
            o = ALPACA.req("POST", "orders", body)
            return self._json({"order": {"id": o.get("id"), "status": o.get("status"), "symbol": osym, "side": side, "qty": qty, "type": body["type"], "limit": body.get("limit_price")}})
        return self._json({"error": "Unknown"}, 404)

    def _frozen(self, fid, m):
        """Frozen strategies (paper traded forward from the freeze): GET list · POST create · GET <id> · POST <id> {action}."""
        if not fid:
            if m == "GET":
                return self._json({"frozen": FROZEN.list()})
            if m == "POST":
                b = self._body()
                if b.get("account") == "paper":
                    HUB.mkt.subscribe(HUB.mkt.subs | {str(b.get("symbol", "")).upper()})
                return self._json({"frozen": FROZEN.create(b)})
            return self._json({"error": "Use GET or POST"}, 405)
        if not re.fullmatch(r"f[a-f0-9]{10}", fid):
            raise BadRequest("Bad id")
        if m == "GET":
            return self._json({"frozen": FROZEN.get(fid)})
        if m == "POST":
            return self._json({"frozen": FROZEN.action(fid, self._body().get("action"))})
        if m == "DELETE":
            FROZEN.action(fid, "delete")
            return self._json({"ok": True})
        return self._json({"error": "Use GET or POST"}, 405)

    def _chats(self, cid, m):
        """Saved Quant conversations: one JSON file each under DATA_DIR/chats."""
        d = os.path.join(DATA_DIR, "chats")
        os.makedirs(d, exist_ok=True)
        if not cid:
            out = []
            for f in os.listdir(d):
                if f.endswith(".json"):
                    try:
                        with open(os.path.join(d, f)) as fh:
                            c = json.load(fh)
                        out.append({"id": c["id"], "title": c.get("title"), "symbol": c.get("symbol"), "updated": c.get("updated", 0), "n": len(c.get("ui", []))})
                    except Exception:
                        pass
            return self._json({"chats": sorted(out, key=lambda c: -c["updated"])[:200]})
        if not re.fullmatch(r"c[a-z0-9]{6,40}", cid):
            raise BadRequest("Bad conversation id")
        path = os.path.join(d, cid + ".json")
        if m == "GET":
            if not os.path.isfile(path):
                return self._json({"error": "Not found"}, 404)
            with open(path, "rb") as fh:
                return self._send(200, fh.read())
        if m == "DELETE":
            if os.path.isfile(path):
                os.remove(path)
            return self._json({"ok": True})
        b = self._body()
        c = {"id": cid, "title": str(b.get("title") or "Chat")[:120], "symbol": str(b.get("symbol") or "")[:20], "updated": int(time.time()),
             "messages": b.get("messages") or [], "ui": b.get("ui") or []}
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(c, fh)
        os.replace(tmp, path)
        return self._json({"ok": True, "id": cid})

    def _stream(self, symbols):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = HUB.add(symbols)
        try:
            first = ["retry: 2000\n\n"]
            for s in symbols:  # current minute bar right away
                b = MKT.cur.get(s)
                if b:
                    first.append(f"event: bar\ndata: {json.dumps({'s': s, 'b': b})}\n\n")
            first.append(f"event: paper\ndata: {json.dumps({'event': None, 'account': HUB.paper.snapshot()})}\n\n")
            self.wfile.write("".join(first).encode())
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
          f"{'  |  journal sign-in' if COGNITO else '  |  password on' if PASSWORD else ''}")
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
