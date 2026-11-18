"""Claude for the chat panel. The browser runs the agent loop (it owns the chart, so it executes the tools);
the server only adds the API key and streams Anthropic's server-sent events back unchanged.

Without ANTHROPIC_API_KEY a scripted demo answers with the same event format, so the whole loop
(chart state -> drawing tools -> answer) can be tried offline."""
import json
import os
import re
import time
import urllib.error
import urllib.request

API = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")


def configured():
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def stream(body, write):
    """body: {system, messages, tools, max_tokens}; write(bytes) sends to the browser."""
    if not configured():
        return demo(body, write)
    payload = {"model": body.get("model") or MODEL, "max_tokens": min(int(body.get("max_tokens") or 4096), 32000), "stream": True,
               "system": body.get("system") or "", "messages": body.get("messages") or [], "tools": body.get("tools") or []}
    if not payload["tools"]:
        payload.pop("tools")
    req = urllib.request.Request(API, data=json.dumps(payload).encode(), method="POST", headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            while True:
                line = r.readline()
                if not line:
                    break
                write(line)
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read() or b"{}").get("error", {}).get("message", "")
        except Exception:
            msg = ""
        _event(write, "error", {"type": "error", "error": {"type": "api_error", "message": f"Claude API {e.code}: {msg or e.reason}"}})
    except urllib.error.URLError as e:
        _event(write, "error", {"type": "error", "error": {"type": "network", "message": f"Claude API unreachable: {e.reason}"}})


def _event(write, name, data):
    write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())


# ---------------------------------------------------------------------------------------------- demo (no key)
def demo(body, write):
    msgs = body.get("messages") or []
    last = msgs[-1] if msgs else {"content": ""}
    content = last.get("content")
    results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"] if isinstance(content, list) else []
    blocks = []
    if not results:
        text = _text(content)
        text = re.sub(r"<(chart_context|editor_script)[^>]*>.*?</\1>", "", text, flags=re.S).lower()
        if "pine" in text or "script" in text or ("indicator" in text and "write" in text):
            blocks = [("text", "Demo mode (no ANTHROPIC_API_KEY): here's a simple EMA cross script, compiled and added to your chart."),
                      ("tool", "write_pine_script", {"name": "EMA cross (demo)", "source": "//@version=6\nindicator(\"EMA cross (demo)\", overlay = true)\nf = ta.ema(close, 9)\ns = ta.ema(close, 21)\nplot(f, \"Fast\", color.aqua)\nplot(s, \"Slow\", color.orange)\nplotshape(ta.crossover(f, s), \"Up\", shape.triangleup, location.belowbar, color.green, size = size.small)\nplotshape(ta.crossunder(f, s), \"Down\", shape.triangledown, location.abovebar, color.red, size = size.small)\n", "add_to_chart": True})]
        else:
            blocks = [("text", "Looking at your chart first."), ("tool", "get_chart_state", {"bars": 120})]
    else:
        r = results[-1]
        payload = _text(r.get("content"))
        try:
            st = json.loads(payload)
        except Exception:
            st = None
        if st and isinstance(st, dict) and st.get("bars"):
            bars = st["bars"]
            hi = max(b[2] for b in bars); lo = min(b[3] for b in bars); last_c = bars[-1][4]
            first = bars[0][4]
            mid = (hi + lo) / 2
            trend = "up" if last_c > first * 1.01 else "down" if last_c < first * 0.99 else "sideways"
            recent = bars[-30:]
            rh, rl = max(b[2] for b in recent), min(b[3] for b in recent)
            blocks = [("text", f"Demo analysis of {st.get('symbol')} {st.get('timeframe')}: price {last_c:.2f}, the {len(bars)}-bar range is {lo:.2f}–{hi:.2f}, trend {trend}. Marking the range extremes, the midpoint and the recent swing range."),
                      ("tool", "draw_levels", {"clear_previous": True, "levels": [
                          {"price": round(hi, 2), "label": "Range high", "kind": "resistance"},
                          {"price": round(rh, 2), "label": "Recent high", "kind": "resistance"},
                          {"price": round(mid, 2), "label": "Range mid", "kind": "pivot"},
                          {"price": round(rl, 2), "label": "Recent low", "kind": "support"},
                          {"price": round(lo, 2), "label": "Range low", "kind": "support"}]})]
        else:
            blocks = [("text", "Done. This is the demo assistant: set ANTHROPIC_API_KEY on the server to talk to Claude for real (it sees the chart screenshot, reads the bars and can write Pine Script, draw and place paper orders).")]
    _stream_blocks(write, blocks)


def _text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def _stream_blocks(write, blocks):
    _event(write, "message_start", {"type": "message_start", "message": {"id": "demo", "role": "assistant", "model": "demo", "content": [], "usage": {"input_tokens": 0, "output_tokens": 0}}})
    stop = "end_turn"
    for i, b in enumerate(blocks):
        if b[0] == "text":
            _event(write, "content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            words = b[1].split(" ")
            for k in range(0, len(words), 3):
                _event(write, "content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": " ".join(words[k:k + 3]) + (" " if k + 3 < len(words) else "")}})
                time.sleep(0.02)
        else:
            stop = "tool_use"
            _event(write, "content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": f"toolu_demo_{int(time.time() * 1000)}_{i}", "name": b[1], "input": {}}})
            s = json.dumps(b[2])
            for k in range(0, len(s), 40):
                _event(write, "content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": s[k:k + 40]}})
        _event(write, "content_block_stop", {"type": "content_block_stop", "index": i})
    _event(write, "message_delta", {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 0}})
    _event(write, "message_stop", {"type": "message_stop"})
