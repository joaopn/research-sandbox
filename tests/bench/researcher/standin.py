#!/usr/bin/env python3
"""A stand-in model server for the benchmark's plumbing run (zero tokens).

Anthropic Messages (Claude) and OpenAI chat completions (pi) on one port. It
answers every request with a short text, except two kinds:
- a consolidation run's brief ("Read the file <brief> and do exactly"): it
  reads the brief, writes a delta adding one belief, and calls
  `rs-memory consolidate --apply` through the agent's bash tool;
- the grader's prompt (it carries GRADE-AS-JSON): a JSON score of 0;
- any other prompt where the agent has a bash tool: it first journals the
  prompt with `rs-memory note` (as a researcher agent is told to; a no-op
  where the command is missing), then answers.
Every request body is logged to /tmp/bench-standin.log.

    python3 standin.py <port>
"""
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = "/tmp/bench-standin.log"
# The interrupted session's message is answered at once with a tool call that
# runs this long, so the plumbing run's kill (5 s in) lands mid-turn, after
# one model call whose tokens are already in the transcript, while the run's
# turn timeout (300 s) is never near.
SLOW_S = 30
SLOW_TEXT = "Continue the per-wave check"


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def delta_cmd(t):
    m = re.search(r"Read the file (\S+) and do exactly", t)
    if not m:
        return None
    brief = open(m.group(1)).read()
    dp = re.search(r"Write the delta as JSON to (\S+) and then run:", brief).group(1)
    page = int(re.search(r'"page": (\d+),', brief).group(1))
    thr = re.search(r'"through": "(J-\d+)",', brief).group(1)
    json.dump({"page": page, "through": thr, "add": [{"part": "beliefs", "text": "stand-in belief"}]}, open(dp, "w"))
    return "rs-memory consolidate --apply " + dp


def slow_cmd(t):
    # not a bare `sleep`: Claude Code blocks a foreground sleep and ends the turn
    return f"python3 -c 'import time; time.sleep({SLOW_S})'" if SLOW_TEXT in t else None


def note_cmd(t):
    if not t.strip() or "GRADE-AS-JSON" in t or "Read the file" in t:
        return None
    head = " ".join(t.split())[:60].replace("'", "")
    return f"rs-memory note 'worked on: {head}' --kind did >/dev/null 2>&1 || true"


def reply_text(t):
    return '{"score": 0, "why": "stand-in"}' if "GRADE-AS-JSON" in t else "STUBOK"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[]}')

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        with open(LOG, "a") as f:
            f.write(json.dumps({"path": self.path, "body": raw.decode("utf-8", "replace")}) + "\n")
        if "count_tokens" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"input_tokens":1}')
            return
        req = json.loads(raw or b"{}")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if self.path.startswith("/v1/messages"):
            us = [m for m in req.get("messages", []) if m.get("role") == "user"]
            last = us[-1].get("content") if us else ""
            tr = isinstance(last, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in last)
            tools = [x.get("name") for x in req.get("tools", []) if isinstance(x, dict)]
            t = text_of(last)
            cmd = None if tr or "Bash" not in tools else (delta_cmd(t) or slow_cmd(t) or note_cmd(t))

            def ev(k, d):
                self.wfile.write(f"event: {k}\ndata: {json.dumps(d)}\n\n".encode())
                self.wfile.flush()
            ev("message_start", {"type": "message_start", "message": {"id": "m1", "type": "message", "role": "assistant",
               "model": req.get("model", "x"), "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 1, "output_tokens": 1}}})
            if cmd:
                ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_%d" % int(time.time() * 1000), "name": "Bash", "input": {}}})
                ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps({"command": cmd, "description": "apply"})}})
            else:
                ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
                ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": reply_text(t)}})
            ev("content_block_stop", {"type": "content_block_stop", "index": 0})
            ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use" if cmd else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}})
            ev("message_stop", {"type": "message_stop"})
            return
        msgs = req.get("messages", [])
        lastm = msgs[-1] if msgs else {}
        t = ""
        if lastm.get("role") == "user":
            # pi injects the front page as its own message after the prompt:
            # the prompt is the last user message that is not the front page
            for m in reversed(msgs):
                if m.get("role") != "user":
                    break
                mt = text_of(m.get("content"))
                if not mt.startswith("rs-memory open (start of session)"):
                    t = mt
                    break
        cmd = (delta_cmd(t) or slow_cmd(t) or note_cmd(t)) if t and req.get("tools") else None

        def chunk(delta, fin=None):
            d = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "stub-1",
                 "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
            self.wfile.write(f"data: {json.dumps(d)}\n\n".encode())
            self.wfile.flush()
        chunk({"role": "assistant", "content": ""})
        if cmd:
            chunk({"tool_calls": [{"index": 0, "id": "call_%d" % int(time.time() * 1000), "type": "function",
                                   "function": {"name": "bash", "arguments": json.dumps({"command": cmd})}}]})
            chunk({}, "tool_calls")
        else:
            chunk({"content": reply_text(t)})
            chunk({}, "stop")
        d = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": "stub-1", "choices": [],
             "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
        self.wfile.write(f"data: {json.dumps(d)}\n\ndata: [DONE]\n\n".encode())
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
