"""The live half of the Inspector: what it probes, and where it refuses to go."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from daino.security.probe import probe_target, target_is_local

#: What the fake app answers with when a test does not override the path.
Responder = Callable[[str, str, dict[str, str]], tuple[int, dict[str, str], str]]


def _render(status: int, headers: dict[str, str], body: str) -> bytes:
    payload = body.encode()
    lines = [f"HTTP/1.1 {status} X", f"Content-Length: {len(payload)}", "Connection: close"]
    lines.extend(f"{key}: {value}" for key, value in headers.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + payload


class FakeApp:
    """A loopback HTTP listener that answers exactly what a test dictates."""

    def __init__(self, responder: Responder) -> None:
        self.responder = responder
        self.seen: list[tuple[str, str]] = []
        self._server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}/"

    async def __aenter__(self) -> FakeApp:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except (TimeoutError, asyncio.IncompleteReadError):
            writer.close()
            return
        head = raw.decode("latin-1").split("\r\n")
        method, path, *_ = head[0].split(" ")
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in head[1:])
            if name.strip()
        }
        self.seen.append((method, path))
        writer.write(_render(*self.responder(method, path, headers)))
        await writer.drain()
        writer.close()


@pytest.fixture
async def careless_app() -> AsyncIterator[FakeApp]:
    """An app deployed the way the Inspector exists to catch."""

    def respond(method: str, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        if path == "/.env":
            return 200, {}, "DATABASE_URL=postgres://u:p@db/app\nAPI_KEY=live\n"
        if path.startswith("/daino-inspector-"):
            return 500, {}, "Traceback (most recent call last):\n  File 'app.py', line 3\n"
        if path == "/":
            reflected = headers.get("origin", "")
            extra = (
                {
                    "Access-Control-Allow-Origin": reflected,
                    "Access-Control-Allow-Credentials": "true",
                }
                if reflected
                else {}
            )
            return (
                200,
                {
                    "Server": "Werkzeug/3.0.1 Python/3.12.1",
                    "Set-Cookie": "session=abc; Path=/",
                    **extra,
                },
                "<html><body>hello</body></html>",
            )
        return 404, {}, "not found"

    async with FakeApp(respond) as app:
        yield app


@pytest.fixture
async def careful_app() -> AsyncIterator[FakeApp]:
    """The same app, deployed the way the report asks for."""

    def respond(method: str, path: str, headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        if path != "/":
            return 404, {}, "not found"
        return (
            200,
            {
                "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "Set-Cookie": "session=abc; Path=/; HttpOnly; SameSite=Lax",
            },
            "<html><body>hello</body></html>",
        )

    async with FakeApp(respond) as app:
        yield app


def test_loopback_and_private_targets_are_local_and_public_ones_are_not() -> None:
    assert target_is_local("http://localhost:3000")
    assert target_is_local("http://127.0.0.1:8000/app")
    assert target_is_local("http://10.0.0.4")
    assert not target_is_local("https://example.com")
    assert not target_is_local("http://8.8.8.8")
    # A name that does not resolve must fail closed, not open.
    assert not target_is_local("http://inspector-probe.invalid")


async def test_a_remote_target_is_refused_until_the_user_confirms_ownership() -> None:
    findings, evidence = await probe_target("https://example.com")

    assert findings == []
    assert "Refused to probe" in evidence


async def test_an_unreachable_target_is_reported_rather_than_failing_the_scan() -> None:
    # Port 1 on loopback: local, so allowed, and reliably not listening.
    findings, evidence = await probe_target("http://127.0.0.1:1/")

    assert findings == []
    assert "did not answer" in evidence


async def test_a_careless_deployment_is_described_by_its_own_responses(
    careless_app: FakeApp,
) -> None:
    findings, evidence = await probe_target(careless_app.url)
    found = {item.reference: item for item in findings}

    assert found["live-exposed-.env"].severity == "critical"
    assert found["live-stack-trace-404"].severity == "high"
    assert found["live-cors-reflected-credentials"].severity == "high"
    assert "HttpOnly" in found["live-cookie-session"].title
    assert found["live-header-content-security-policy"].cwe == "CWE-693"
    assert found["live-header-frame-options"].cwe == "CWE-1021"
    assert "Werkzeug" in found["live-header-version-disclosure"].detail
    # The probe only ever reads.
    assert {method for method, _ in careless_app.seen} <= {"GET", "OPTIONS"}
    assert careless_app.url in evidence


async def test_a_hardened_deployment_produces_nothing_to_act_on(
    careful_app: FakeApp,
) -> None:
    findings, _ = await probe_target(careful_app.url)

    assert [item.title for item in findings] == []
