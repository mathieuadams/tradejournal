"""Minimal Anthropic Messages API client using only the standard library."""
import json
import os
import time
import urllib.error
import urllib.request

from util import Unavailable

_key = None
URL = "https://api.anthropic.com/v1/messages"


def api_key():
    global _key
    if _key is None:
        import boto3
        v = boto3.client("secretsmanager").get_secret_value(SecretId=os.environ["ANTHROPIC_SECRET_ARN"])
        _key = v["SecretString"].strip()
    if not _key or _key.startswith("REPLACE"):
        raise Unavailable("The AI coach isn't configured yet. Add your Anthropic API key (see README).")
    return _key


def messages(model, system, msgs, max_tokens=1500, tools=None):
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": msgs}
    if tools:
        body["tools"] = tools
    data = json.dumps(body).encode()
    for attempt in range(3):
        req = urllib.request.Request(URL, data=data, method="POST", headers={
            "x-api-key": api_key(), "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            detail = e.read().decode(errors="replace")[:300]
            raise Unavailable(f"The AI service returned an error ({e.code}): {detail}")
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(2)
                continue
            raise Unavailable(f"The AI service couldn't be reached: {e.reason}")


def text_of(resp):
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")


def json_call(model, system, payload, max_tokens=1200):
    r = messages(model, system, [{"role": "user", "content": json.dumps(payload, default=str)}], max_tokens)
    t = text_of(r).strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("{"):]
    s, e = t.find("{"), t.rfind("}")
    if s < 0 or e < 0:
        raise Unavailable("The AI response wasn't valid JSON.")
    return json.loads(t[s:e + 1])
