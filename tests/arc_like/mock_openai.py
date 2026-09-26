#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("ARC_MOCK_PORT", "19091"))
REQUIRE_RECOVERY = os.environ.get("ARC_MOCK_REQUIRE_RECOVERY", "0") == "1"
RECOVERY_SEEN = False


def dump(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[mock]", fmt % args, flush=True)

    def send_json(self, code: int, obj) -> None:
        raw = dump(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/health":
            return self.send_json(200, {"ok": True})
        if self.path == "/v1/models":
            return self.send_json(200, {"object": "list", "data": [{"id": "mock-model"}]})
        return self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            return self.send_json(404, {"error": {"message": self.path}})

        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        tools = body.get("tools") or []
        messages = body.get("messages") or []
        global RECOVERY_SEEN
        serialized_messages = json.dumps(messages, ensure_ascii=False)
        if REQUIRE_RECOVERY and not RECOVERY_SEEN:
            if "RECOVERY ATTEMPT" in serialized_messages:
                RECOVERY_SEEN = True
                print("[mock] recovery prompt observed; upstream recovers", flush=True)
            else:
                print("[mock] injecting upstream connection reset", flush=True)
                return self.send_json(400, {
                    "error": {
                        "message": 'upstream: Post "https://api.taotoken.net/v1/chat/completions": read tcp: connection reset by peer',
                        "type": "api_error",
                    }
                })
        names = [((tool.get("function") or {}).get("name")) for tool in tools]
        print("[mock] tool_count:", len(names), flush=True)

        has_tool_result = any(message.get("role") == "tool" for message in messages)
        stream = bool(body.get("stream"))
        if has_tool_result:
            return self.respond_text(stream, "Done. The requested file has been created.")

        # Prefer Bash so smoke can also plant a minimal frontend test tree for the
        # harness validation gate (Claude success alone must not mark_test_passed).
        tool_name = next((name for name in names if name == "Bash"), None)
        arguments = None
        smoke_setup = (
            "mkdir -p /workspace/output/frontend/tests && "
            "printf 'ARC_CLAUDE_GSC_OK\\n' > /workspace/output/ARC_SMOKE.txt && "
            "printf '%s\\n' "
            "'{\"name\":\"arc-smoke-frontend\",\"private\":true,"
            "\"scripts\":{\"test\":\"node ./tests/smoke.test.js\"}}' "
            "> /workspace/output/frontend/package.json && "
            "printf '%s\\n' "
            "'const fs=require(\"fs\");"
            "const p=require(\"path\").resolve(__dirname,\"..\",\"..\",\"ARC_SMOKE.txt\");"
            "if(!fs.existsSync(p)||fs.readFileSync(p,\"utf8\").trim()!==\"ARC_CLAUDE_GSC_OK\")"
            "{console.error(\"bad marker\");process.exit(1)}"
            "console.log(\"smoke harness validation ok\");' "
            "> /workspace/output/frontend/tests/smoke.test.js"
        )
        if tool_name:
            arguments = {"command": smoke_setup}
        else:
            tool_name = next((name for name in names if name == "Write"), None)
            if tool_name:
                arguments = {
                    "file_path": "/workspace/output/ARC_SMOKE.txt",
                    "content": "ARC_CLAUDE_GSC_OK\n",
                }

        if not tool_name:
            return self.respond_text(stream, "No writable tool was exposed.")
        return self.respond_tool(stream, tool_name, arguments)

    def sse_headers(self):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()

    def sse(self, obj):
        self.wfile.write(("data: " + dump(obj) + "\n\n").encode())
        self.wfile.flush()

    def respond_tool(self, stream: bool, name: str, arguments: dict):
        now = int(time.time())
        args = dump(arguments)
        if not stream:
            return self.send_json(200, {
                "id": "chatcmpl-tool",
                "object": "chat.completion",
                "created": now,
                "model": "mock-model",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_arc_smoke",
                            "type": "function",
                            "function": {"name": name, "arguments": args},
                        }],
                    },
                    "finish_reason": "tool_calls",
                }],
            })

        self.sse_headers()
        base = {"id": "chatcmpl-tool", "object": "chat.completion.chunk", "created": now, "model": "mock-model"}
        self.sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        self.sse({**base, "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0,
            "id": "call_arc_smoke",
            "type": "function",
            "function": {"name": name, "arguments": args},
        }]}, "finish_reason": None}]})
        self.sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def respond_text(self, stream: bool, text: str):
        now = int(time.time())
        if not stream:
            return self.send_json(200, {
                "id": "chatcmpl-final",
                "object": "chat.completion",
                "created": now,
                "model": "mock-model",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
            })

        self.sse_headers()
        base = {"id": "chatcmpl-final", "object": "chat.completion.chunk", "created": now, "model": "mock-model"}
        self.sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})
        self.sse({**base, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
        self.sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


if __name__ == "__main__":
    print(f"[mock] listening on 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
