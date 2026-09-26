"""Minimal Anthropic Messages API client using only the standard library."""
import json
import os
import re
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
        _key = None  # re-read next time, so a key added later works without a redeploy
        raise Unavailable("The AI coach isn't configured yet. Add your Anthropic API key (see README).")
    return _key


def messages(model, system, msgs, max_tokens=1500, tools=None, extra=None):
    body = {"model": model, "max_tokens": max_tokens, "system": system, "messages": msgs}
    if tools:
        body["tools"] = tools
    if extra:
        body.update(extra)
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


def json_call(model, system, payload, max_tokens=1200, schema=None):
    """Structured output. With a schema, Claude must answer by calling a tool whose input is the JSON object,
    so the result is always valid JSON. Without one, falls back to parsing the text."""
    msgs = [{"role": "user", "content": json.dumps(payload, default=str)}]
    if schema:
        tool = {"name": "submit", "description": "Submit the final answer.", "input_schema": schema}
        body_extra = {"tool_choice": {"type": "tool", "name": "submit"}}
        r = messages(model, system, msgs, max_tokens, [tool], extra=body_extra)
        for attempt in range(2):
            out = next((b.get("input") or {} for b in r.get("content", []) if b.get("type") == "tool_use"), None)
            if out is not None:
                fixed, leaked = repair(out, schema)
                if not leaked or attempt == 1:
                    return fixed
                print("AI tool input had leaked markup, retrying")
            else:
                print("AI returned no tool call:", json.dumps(r)[:800])
            r = messages(model, system, msgs, max_tokens, [tool], extra=body_extra)
        raise Unavailable("The AI didn't return a structured answer. Try again.")
    r = messages(model, system, msgs, max_tokens)
    t = text_of(r).strip()
    s, e = t.find("{"), t.rfind("}")
    try:
        return json.loads(t[s:e + 1])
    except ValueError:
        print("AI returned invalid JSON:", t[:800], "stop_reason:", r.get("stop_reason"))
        raise Unavailable("The AI response wasn't valid JSON. Try again.")


_PARAM = re.compile(r'<parameter name="(\w+)">(.*?)(?:</parameter>|$)', re.S)


def repair(obj, schema):
    """Occasionally the model writes later tool parameters inside an earlier string, e.g.
    'summary text</summary> <parameter name="leaks">[...]</parameter>'. Pull them back out."""
    leaked = False
    props = (schema or {}).get("properties", {})
    out = dict(obj)
    for key, val in list(out.items()):
        if not isinstance(val, str) or "<parameter" not in val and not re.search(r"</\w+>", val):
            continue
        leaked = True
        for name, raw in _PARAM.findall(val):
            if name in props and (name not in out or out.get(name) in (None, "", [], {})):
                raw = raw.strip()
                try:
                    out[name] = json.loads(raw) if props[name].get("type") in ("array", "object", "number", "boolean") else raw
                except ValueError:
                    out[name] = raw
        cut = re.search(r"</\w+>|<parameter", val)
        out[key] = val[:cut.start()].strip() if cut else val
    return out, leaked
