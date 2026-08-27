"""What "Test connection" actually proves.

A provider test that only checks whether an HTTP endpoint answers gives a green
tick to a configuration that cannot serve a single request — a reachable runtime
with a model that was never pulled, or a rejected key. These tests pin each step
of the diagnosis against a real socket: reachable, credentialed, model present,
and one actual generation.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from daino.application import initialize_project, open_project
from daino.application.provider_service import ProviderApplicationService

#: Models the fake runtime reports as installed.
INSTALLED = ["fake-model"]


class _Handler(BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible runtime: a model list and a completion."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # keep the test output clean
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") == "/v1/models":
            self._send(200, {"data": [{"id": name} for name in INSTALLED]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        if request.get("model") not in INSTALLED:
            # What a real runtime does when asked for a model it does not have.
            self._send(404, {"error": {"message": f"model {request.get('model')!r} not found"}})
            return
        self._send(
            200,
            {
                "model": request.get("model"),
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )


@pytest.fixture
def runtime() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def providers(git_repo: Path) -> Iterator[ProviderApplicationService]:
    initialize_project(git_repo)
    context = open_project(git_repo)
    try:
        yield ProviderApplicationService(context)
    finally:
        context.close()


def _steps(diagnosis: object) -> dict[str, tuple[str, str]]:
    return {check.name: (check.status, check.detail) for check in diagnosis.checks}  # type: ignore[attr-defined]


async def test_a_working_provider_passes_every_step(
    providers: ProviderApplicationService, runtime: str
) -> None:
    diagnosis = await providers.diagnose(
        name="fake",
        provider_type="openai-compatible",
        base_url=runtime,
        model="fake-model",
    )
    steps = _steps(diagnosis)
    assert diagnosis.status.connected is True
    assert steps["endpoint"][0] == "pass"
    assert steps["model"] == ("pass", "fake-model is available")
    # The decisive step: a real request came back.
    assert steps["generation"][0] == "pass"
    assert "replied in" in steps["generation"][1]
    # No key was configured, so authentication is skipped rather than claimed.
    assert steps["credentials"][0] == "skip"


async def test_a_model_that_is_not_installed_fails(
    providers: ProviderApplicationService, runtime: str
) -> None:
    """The exact case a shallow "is the port open?" check reports as healthy."""
    diagnosis = await providers.diagnose(
        name="fake",
        provider_type="openai-compatible",
        base_url=runtime,
        model="never-pulled",
    )
    steps = _steps(diagnosis)
    assert diagnosis.status.connected is False
    assert steps["endpoint"][0] == "pass"  # the runtime is up…
    assert steps["model"][0] == "fail"  # …but it does not have this model
    assert "never-pulled is not among the 1 models offered" in steps["model"][1]
    assert steps["generation"][0] == "fail"
    assert "not found" in steps["generation"][1]


async def test_an_unreachable_endpoint_skips_the_rest(
    providers: ProviderApplicationService,
) -> None:
    diagnosis = await providers.diagnose(
        name="dead",
        provider_type="openai-compatible",
        # Port 1 is never a model server.
        base_url="http://127.0.0.1:1/v1",
        model="anything",
    )
    steps = _steps(diagnosis)
    assert diagnosis.status.connected is False
    assert steps["endpoint"][0] == "fail"
    assert {steps[name][0] for name in ("credentials", "model", "generation")} == {"skip"}


async def test_a_blank_model_is_reported_rather_than_ignored(
    providers: ProviderApplicationService, runtime: str
) -> None:
    diagnosis = await providers.diagnose(
        name="fake", provider_type="openai-compatible", base_url=runtime, model=""
    )
    steps = _steps(diagnosis)
    assert diagnosis.status.connected is False
    assert steps["model"] == ("fail", "no model selected")
    assert steps["generation"][0] == "skip"
