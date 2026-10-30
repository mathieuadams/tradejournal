"""Run the whole app on your machine: web app + API + background jobs, no AWS account needed.

    npm start            (or: python dev/local_server.py)
    open http://localhost:5173

Sign-in is skipped. Data lives in dev/.localdb.json and dev/.localdata/. With FAKE_DATABENTO=1 any key that
starts with db- works and the candles / trades are synthetic; otherwise your real Databento key is used.
"""
import mimetypes
import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FRONT = os.path.join(ROOT, "frontend")
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, HERE)
PORT = int(os.environ.get("PORT", "5173"))
os.environ.update({"KMS_KEY_ID": "local", "TABLE": "local", "DATA_BUCKET": "local"})

import fakes  # noqa: E402

fakes.install(fake_databento=os.environ.get("FAKE_DATABENTO") == "1")

import api  # noqa: E402
import jobs  # noqa: E402

SUB = "local-user"


def _bg(payload):
    def run():
        try:
            jobs.handler(payload, None)
        except Exception as e:
            print("background job failed:", e)
    threading.Thread(target=run, daemon=True).start()


jobs.INVOKE = _bg

CONFIG_JS = f"""window.TJ_CONFIG = {{ local: true, apiUrl: "http://localhost:{PORT}/api", region: "local",
  userPoolId: "local", clientId: "local", cognitoDomain: "localhost" }};"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("  %s %s\n" % (self.command, self.path))

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        url = urllib.parse.urlparse(self.path)
        if url.path.startswith("/api/"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            n = int(self.headers.get("Content-Length") or 0)
            ev = {"rawPath": url.path[4:], "queryStringParameters": q, "body": (self.rfile.read(n).decode() if n else None),
                  "requestContext": {"http": {"method": self.command}, "stage": "$default",
                                     "authorizer": {"jwt": {"claims": {"sub": SUB, "email": "local@dev"}}}}}
            r = api.handler(ev, None)
            return self._send(r["statusCode"], r["body"].encode())
        if self.command != "GET":
            return self._send(405)
        if url.path == "/config.js":
            return self._send(200, CONFIG_JS.encode(), "application/javascript")
        path = os.path.normpath(os.path.join(FRONT, url.path.lstrip("/") or "index.html"))
        if not path.startswith(FRONT) or not os.path.isfile(path):
            path = os.path.join(FRONT, "index.html")
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, mimetypes.guess_type(path)[0] or "application/octet-stream")

    do_GET = do_POST = do_PUT = do_DELETE = _handle


if __name__ == "__main__":
    print(f"Tradejournal Futures at http://localhost:{PORT}  |  Databento: {'fake' if os.environ.get('FAKE_DATABENTO') == '1' else 'real key'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
