"""Run the whole app on your machine: web app + API + imports, no AWS account needed.

    npm start            (or: python dev/local_server.py)
    open http://localhost:5173

Sign-in is skipped (you are "local@dev"). Data lives in dev/.localdb.json.
The AI coach (reviews, weekly report, chat) needs ANTHROPIC_API_KEY in your environment;
everything else works without it.
"""
import json
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
os.environ.update({"UPLOAD_BUCKET": "local", "SYNC_FUNCTION": "sync", "WEEKLY_FUNCTION": "weekly",
                   "KMS_KEY_ID": "local", "TABLE": "local"})

import fakes  # noqa: E402

fakes.install()

import api  # noqa: E402
import claude  # noqa: E402
import importer  # noqa: E402
import sync  # noqa: E402
import weekly  # noqa: E402

SUB = "local-user"
claude._key = os.environ.get("ANTHROPIC_API_KEY") or "REPLACE_ME"


class FakeS3:
    def generate_presigned_url(self, op, ExpiresIn, HttpMethod, Params):
        return f"http://localhost:{PORT}/_upload/{urllib.parse.quote(Params['Key'])}"


class FakeLambda:
    def invoke(self, FunctionName, InvocationType, Payload):
        fn = {"sync": sync.handler, "weekly": weekly.handler}[FunctionName]
        threading.Thread(target=lambda: _safe(fn, json.loads(Payload)), daemon=True).start()


def _safe(fn, payload):
    try:
        fn(payload, None)
    except Exception as e:
        print("background job failed:", e)


api._s3 = lambda: FakeS3()
api._lambda = lambda: FakeLambda()

CONFIG_JS = f"""window.TJ_CONFIG = {{ local: true, apiUrl: "http://localhost:{PORT}/api", region: "local",
  userPoolId: "local", clientId: "local", cognitoDomain: "localhost" }};"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("  %s %s\n" % (self.command, self.path))

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _handle(self):
        url = urllib.parse.urlparse(self.path)
        if url.path.startswith("/api/"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            ev = {"rawPath": url.path[4:], "queryStringParameters": q, "body": self._body().decode() or None,
                  "requestContext": {"http": {"method": self.command}, "stage": "$default",
                                     "authorizer": {"jwt": {"claims": {"sub": SUB, "email": "local@dev"}}}}}
            r = api.handler(ev, None)
            return self._send(r["statusCode"], r["body"].encode())
        if url.path.startswith("/_upload/") and self.command == "PUT":
            key = urllib.parse.unquote(url.path[len("/_upload/"):])
            data = self._body()
            _, sub, iid, acct_b64, _name = key.split("/", 4)
            import base64
            account = base64.urlsafe_b64decode(acct_b64 + "=" * (-len(acct_b64) % 4)).decode()
            text = data.decode("utf-8", errors="replace")
            threading.Thread(target=lambda: _safe(lambda p, c: importer.process(sub, iid, account, text), None), daemon=True).start()
            return self._send(200)
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

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle


if __name__ == "__main__":
    ai = "on" if os.environ.get("ANTHROPIC_API_KEY") else "off (set ANTHROPIC_API_KEY to turn it on)"
    print(f"Trade Journal running at http://localhost:{PORT}  |  AI coach: {ai}  |  data: dev/.localdb.json")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
