"""Credential-free OpenAI-compatible smoke-test server."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "mock-coder", "object": "model"}]})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        size = int(self.headers.get("content-length", "0"))
        request = json.loads(self.rfile.read(size))
        content = "ok"
        if request.get("response_format"):
            content = json.dumps(
                {
                    "approved": True,
                    "summary": "Mock review approved",
                    "findings": [],
                    "missing_tests": [],
                }
            )
        self._send(
            {
                "id": "mock",
                "model": request.get("model", "mock-coder"),
                "choices": [
                    {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        )

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, data: object) -> None:
        encoded = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8001), Handler).serve_forever()  # noqa: S104
