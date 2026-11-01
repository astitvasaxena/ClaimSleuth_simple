"""FAKE Ollama HTTP server — a test fixture, NOT a model and NOT Ollama.

Serves /api/version, /api/tags and an OpenAI-compatible /v1/chat/completions on 127.0.0.1 so offline tests
can drive CrewAI's real Ollama provider (native function calling) without inference. Behaviours:
  "loop"  every completion asks for the first offered tool again (tests iteration bounds)
  "slow"  sleeps `delay` seconds before answering (tests HTTP timeouts)
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self, models: list[dict] | None = None, behaviour: str = "loop", delay: float = 0.0) -> None:
        self.models = models if models is not None else [local_model("qwen2.5:3b")]
        self.behaviour, self.delay = behaviour, delay
        self.chat_requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, body: dict, status: int = 200) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/version":
                    return self._send({"version": "fake-0.0"})
                if self.path == "/api/tags":
                    return self._send({"models": fake.models})
                return self._send({"error": "not found"}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                fake.chat_requests.append(body)
                if fake.behaviour == "slow":
                    time.sleep(fake.delay)
                tools = body.get("tools") or []
                if tools:
                    name = tools[0]["function"]["name"]
                    message = {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"call_{len(fake.chat_requests)}", "type": "function",
                        "function": {"name": name, "arguments": json.dumps({"serial_number": "FRL-RF-77100-A"})}}]}
                    finish = "tool_calls"
                else:
                    message, finish = {"role": "assistant", "content": "not a forensic report"}, "stop"
                try:
                    self._send({"id": "chatcmpl-fake", "object": "chat.completion", "created": int(time.time()),
                                "model": body.get("model"), "choices": [{"index": 0, "message": message,
                                                                         "finish_reason": finish}],
                                "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}})
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def local_model(name: str, **over) -> dict:
    entry = {"name": name, "model": name, "size": 1929912432, "digest": "f" * 64,
             "details": {"parameter_size": "3.1B", "quantization_level": "Q4_K_M", "context_length": 32768},
             "capabilities": ["completion", "tools"]}
    entry.update(over)
    return entry
