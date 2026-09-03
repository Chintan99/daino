"""JSON-RPC clients for the two MCP transports.

Deliberately hand-written rather than taking the reference SDK as a dependency.
Three methods are needed — ``initialize``, ``tools/list``, ``tools/call`` — and
all three have been stable across protocol revisions. A dependency would bring
its own async runtime assumptions, its own logging, and a version floor on a
tool that is meant to run offline against a local model on someone's laptop.

What matters here is failure behaviour, not feature coverage. An MCP server is a
third-party process that may hang, die, print garbage on stdout, or return a
1MB blob. None of those may take a mission down with them, so every call is
bounded by a timeout, every response is size-capped, and a dead server is
reported as a failed tool rather than raised as an exception into the loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from abc import ABC, abstractmethod
from typing import Any

import httpx

from daino.mcp.models import PROTOCOL_VERSION, MCPServerConfig, MCPTool

#: Ceiling on one response, before it is truncated. A server that returns a
#: whole table dump must not blow the model's context in a single observation.
MAX_RESPONSE_CHARS = 100_000

#: Ceiling on one line from a stdio server. Larger than the response cap so a
#: legitimate big reply is truncated by the caller rather than corrupted here.
MAX_LINE_BYTES = 8 * 1024 * 1024

CLIENT_INFO = {"name": "daino", "version": "0.4"}


class MCPError(RuntimeError):
    """A server could not be reached, or answered with an error."""


class MCPClient(ABC):
    """One connection to one server."""

    def __init__(self, name: str, config: MCPServerConfig) -> None:
        self.name = name
        self.config = config
        self.server_info: dict[str, Any] = {}
        self._next_id = 0

    def _identifier(self) -> int:
        self._next_id += 1
        return self._next_id

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any: ...

    @abstractmethod
    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    async def handshake(self) -> None:
        """Complete the initialize exchange, so the server will accept calls."""
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": CLIENT_INFO,
            },
        )
        if isinstance(result, dict):
            self.server_info = result
        await self.notify("notifications/initialized")

    async def list_tools(self) -> list[MCPTool]:
        """Every tool this server offers that the configuration lets through."""
        tools: list[MCPTool] = []
        cursor: str | None = None
        # Paginated by the protocol. A server with many tools returns a cursor;
        # stopping at the first page would silently hide the rest.
        for _ in range(20):
            params = {"cursor": cursor} if cursor else {}
            result = await self.request("tools/list", params)
            if not isinstance(result, dict):
                break
            for entry in result.get("tools") or []:
                if not isinstance(entry, dict) or not entry.get("name"):
                    continue
                name = str(entry["name"])
                if not self.config.exposes(name):
                    continue
                tools.append(
                    MCPTool(
                        server=self.name,
                        name=name,
                        description=str(entry.get("description") or ""),
                        input_schema=entry.get("inputSchema") or {},
                    )
                )
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
        """Invoke one tool. Returns ``(ok, rendered_text)``.

        A protocol-level error and a tool that reports failure are both returned
        as ``ok=False`` rather than raised: from the agent's point of view they
        are the same event — the tool did not do the thing — and the loop's
        observation machinery is where that belongs.
        """
        result = await self.request("tools/call", {"name": tool, "arguments": arguments})
        if not isinstance(result, dict):
            return (False, "The server returned an unexpected response shape.")
        rendered = render_content(result.get("content"))
        structured = result.get("structuredContent")
        if not rendered and structured is not None:
            rendered = json.dumps(structured, ensure_ascii=False, indent=2)
        return (not bool(result.get("isError")), _clip(rendered))


class StdioMCPClient(MCPClient):
    """Speaks newline-delimited JSON-RPC to a launched process."""

    def __init__(self, name: str, config: MCPServerConfig) -> None:
        super().__init__(name, config)
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        #: Last lines the server wrote to stderr. An MCP server that fails to
        #: start usually explains why there and nowhere else, and without this
        #: the user gets "the server closed the connection" and no cause.
        self.diagnostics: list[str] = []
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        environment = os.environ.copy()
        environment.update(self.config.env)
        try:
            self._process = await asyncio.create_subprocess_exec(
                self.config.command,
                *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
                limit=MAX_LINE_BYTES,
            )
        except OSError as exc:
            raise MCPError(f"could not start {self.config.command}: {exc}") from exc
        self._reader = asyncio.create_task(self._read_loop())
        self._stderr = asyncio.create_task(self._drain_stderr())

    async def _read_loop(self) -> None:
        if self._process is None or self._process.stdout is None:
            return
        stream = self._process.stdout
        while True:
            try:
                line = await stream.readline()
            except (asyncio.LimitOverrunError, ValueError):
                # An oversized line. Everything after it is unframed, so the
                # connection is finished rather than merely damaged.
                self._fail_pending(MCPError("the server sent an oversized message"))
                return
            if not line:
                break
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                # Servers occasionally log to stdout despite the protocol saying
                # not to. Skipping the line is right; failing the session is not.
                continue
            if isinstance(message, dict):
                self._dispatch(message)
        self._fail_pending(MCPError("the server closed the connection"))

    def _dispatch(self, message: dict[str, Any]) -> None:
        identifier = message.get("id")
        if identifier is None:
            # A notification or a server-initiated request. Nothing here samples
            # the server's own requests, and answering them incorrectly would be
            # worse than not answering.
            return
        pending = self._pending.pop(int(identifier), None)
        if pending is None or pending.done():
            return
        error = message.get("error")
        if error:
            pending.set_exception(MCPError(_error_text(error)))
        else:
            pending.set_result(message.get("result"))

    def _fail_pending(self, exc: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def _drain_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        stderr = self._process.stderr
        while True:
            line = await stderr.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip()
            if text:
                self.diagnostics.append(text)
                del self.diagnostics[:-20]

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise MCPError("the server is not running")
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
        async with self._write_lock:
            try:
                self._process.stdin.write(encoded)
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise MCPError(f"the server stopped accepting input: {exc}") from exc

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        identifier = self._identifier()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[identifier] = future
        await self._send(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params or {}}
        )
        try:
            return await asyncio.wait_for(future, timeout=self.config.timeout)
        except TimeoutError as exc:
            self._pending.pop(identifier, None)
            raise MCPError(
                f"{method} timed out after {self.config.timeout:g}s"
                + (f" ({self.diagnostics[-1]})" if self.diagnostics else "")
            ) from exc

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def close(self) -> None:
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        # Closing stdin is how an MCP server is asked to shut down. The kill is
        # the backstop for one that ignores it.
        with contextlib.suppress(Exception):
            if process.stdin is not None:
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            process.kill()
            with contextlib.suppress(Exception):
                await process.wait()


class HTTPMCPClient(MCPClient):
    """Posts JSON-RPC to a URL, accepting a JSON or an SSE reply."""

    def __init__(
        self,
        name: str,
        config: MCPServerConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(name, config)
        self._client = httpx.AsyncClient(
            timeout=config.timeout,
            transport=transport,
            follow_redirects=False,
            headers={
                "Content-Type": "application/json",
                # Both, because a streamable-HTTP server chooses which to send.
                "Accept": "application/json, text/event-stream",
                **config.headers,
            },
        )
        #: Issued by the server at initialize and echoed on every later request.
        self._session_id = ""

    async def start(self) -> None:
        return None

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        headers = {"Mcp-Session-Id": self._session_id} if self._session_id else {}
        try:
            response = await self._client.post(self.config.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise MCPError(f"could not reach {self.config.url}: {exc}") from exc
        if response.status_code >= 400:
            raise MCPError(f"{self.config.url} answered HTTP {response.status_code}")
        issued = response.headers.get("mcp-session-id")
        if issued:
            self._session_id = issued
        return response

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._identifier(),
                "method": method,
                "params": params or {},
            }
        )
        message = _decode_http_body(response)
        if message is None:
            raise MCPError(f"{method} returned no JSON-RPC message")
        error = message.get("error")
        if error:
            raise MCPError(_error_text(error))
        return message.get("result")

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def close(self) -> None:
        await self._client.aclose()


def build_client(
    name: str,
    config: MCPServerConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MCPClient:
    if config.transport == "http":
        return HTTPMCPClient(name, config, transport=transport)
    return StdioMCPClient(name, config)


def render_content(content: Any) -> str:
    """Flatten an MCP content array into text the model can read.

    Text parts are used as-is. Everything else — images, audio, embedded
    resources — is described rather than inlined: the transcript is text, and a
    base64 blob in it is pure cost with no information for the model.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "resource":
            resource = item.get("resource")
            if isinstance(resource, dict) and resource.get("text"):
                parts.append(str(resource["text"]))
            elif isinstance(resource, dict):
                parts.append(f"[resource {resource.get('uri', 'unknown')}]")
        elif kind in {"image", "audio"}:
            parts.append(f"[{kind} content omitted: {item.get('mimeType', 'unknown type')}]")
        elif kind == "resource_link":
            parts.append(f"[resource link {item.get('uri', '')}]")
    return "\n".join(part for part in parts if part)


def _decode_http_body(response: httpx.Response) -> dict[str, Any] | None:
    media = response.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if media == "text/event-stream":
        return _first_sse_message(response.text)
    if not response.content:
        return None
    try:
        payload = response.json()
    except ValueError as exc:
        raise MCPError(f"the server returned non-JSON: {exc}") from exc
    return payload if isinstance(payload, dict) else None


def _first_sse_message(body: str) -> dict[str, Any] | None:
    """The first JSON-RPC message in an SSE stream.

    One response per request is what the three methods used here produce, so the
    first ``data:`` payload is the answer; anything after it is progress
    notification that nothing consumes.
    """
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk:
            continue
        try:
            payload = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and ("result" in payload or "error" in payload):
            return payload
    return None


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or "unknown error"
        return f"{message} (code {code})" if code is not None else str(message)
    return str(error)


def _clip(text: str) -> str:
    if len(text) <= MAX_RESPONSE_CHARS:
        return text
    return (
        text[:MAX_RESPONSE_CHARS]
        + f"\n… response truncated at {MAX_RESPONSE_CHARS:,} characters …"
    )
