"""The language-server client, exercised against a real LSP server.

The server here is a stub, but the protocol is not: it frames JSON-RPC over
stdio, completes an initialize handshake, publishes diagnostics unsolicited, and
answers definition/references/symbol/rename requests. That is the whole contract
the client depends on, so testing against it catches the things that actually
break — framing, request correlation, the two shapes LSP allows for a location,
and the fact that diagnostics arrive when the server is ready rather than when
anybody asked.

Using a stub rather than pyright is deliberate: the suite must pass on a machine
with no language servers installed, which is also the machine the "no server"
path has to be correct on.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.repository.lsp import (
    LanguageServer,
    LSPError,
    PooledLSPAdapter,
    ServerSpec,
    available_servers,
    from_uri,
    language_id_for,
    resolve_executable,
    to_uri,
)

#: A minimal but genuine LSP server. Reports one error on line 2 of any file it
#: is shown, and answers navigation from fixed positions.
STUB_SERVER = textwrap.dedent(
    """
    import json, sys

    def read():
        length = 0
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            text = line.decode().strip()
            if not text:
                break
            name, _, value = text.partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        if length <= 0:
            return None
        return json.loads(sys.stdin.buffer.read(length).decode())

    def write(payload):
        body = json.dumps(payload).encode()
        sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
        sys.stdout.buffer.flush()

    def reply(identifier, result):
        write({"jsonrpc": "2.0", "id": identifier, "result": result})

    while True:
        message = read()
        if message is None:
            break
        method = message.get("method")
        identifier = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            reply(identifier, {"capabilities": {"referencesProvider": True}})
        elif method == "textDocument/didOpen":
            uri = params["textDocument"]["uri"]
            write({
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": uri,
                    "diagnostics": [{
                        "range": {
                            "start": {"line": 1, "character": 4},
                            "end": {"line": 1, "character": 9},
                        },
                        "severity": 1,
                        "message": "undefined name 'oops'",
                        "source": "stub",
                        "code": "F821",
                    }],
                },
            })
        elif method == "textDocument/didChange":
            uri = params["textDocument"]["uri"]
            # A changed document re-publishes; an empty list is how a server
            # says "you fixed it", which the client must reflect.
            write({
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {"uri": uri, "diagnostics": []},
            })
        elif method == "textDocument/definition":
            uri = params["textDocument"]["uri"]
            # LocationLink shape, to prove the client normalises both.
            reply(identifier, [{
                "targetUri": uri,
                "targetSelectionRange": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 5},
                },
            }])
        elif method == "textDocument/references":
            uri = params["textDocument"]["uri"]
            reply(identifier, [
                {"uri": uri, "range": {
                    "start": {"line": 0, "character": 0},
                    "end": {"line": 0, "character": 4}}},
                {"uri": uri, "range": {
                    "start": {"line": 3, "character": 2},
                    "end": {"line": 3, "character": 6}}},
            ])
        elif method == "textDocument/implementation":
            reply(identifier, None)
        elif method == "textDocument/documentSymbol":
            reply(identifier, [{
                "name": "Widget",
                "kind": 5,
                "detail": "class Widget",
                "selectionRange": {
                    "start": {"line": 0, "character": 6},
                    "end": {"line": 0, "character": 12}},
                "children": [{
                    "name": "render",
                    "kind": 6,
                    "selectionRange": {
                        "start": {"line": 1, "character": 8},
                        "end": {"line": 1, "character": 14}},
                }],
            }])
        elif method == "workspace/symbol":
            uri = params.get("__uri") or "file:///nowhere"
            reply(identifier, [])
        elif method == "textDocument/hover":
            reply(identifier, {"contents": {"kind": "markdown", "value": "**Widget**"}})
        elif method == "textDocument/rename":
            uri = params["textDocument"]["uri"]
            reply(identifier, {"changes": {uri: [
                {"range": {"start": {"line": 0, "character": 6},
                           "end": {"line": 0, "character": 12}},
                 "newText": params["newName"]},
            ]}})
        elif method == "shutdown":
            reply(identifier, None)
        elif identifier is not None:
            reply(identifier, None)
    """
).strip()


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "widget.py").write_text(
        "class Widget:\n    oops = 1\n\n\nWidget()\n", encoding="utf-8"
    )
    (tmp_path / "_stub_lsp.py").write_text(STUB_SERVER, encoding="utf-8")
    yield tmp_path


def _stub_spec() -> ServerSpec:
    return ServerSpec(id="stub", label="Stub", executable="stub", languages=("python",))


async def _stub_server(project: Path) -> LanguageServer:
    server = LanguageServer(
        project,
        _stub_spec(),
        [sys.executable, str(project / "_stub_lsp.py")],
    )
    await server.start()
    return server


# ----------------------------------------------------------------- protocol


async def test_diagnostics_arrive_when_the_server_publishes_them(project: Path) -> None:
    """A server publishes when it has finished analysing, not when asked.

    Returning immediately after didOpen would report "no problems" for every
    file the editor has only just opened — the exact false clean the Problems
    panel must never show.
    """
    server = await _stub_server(project)
    try:
        uri = await server.open_document(project / "widget.py")
        found = await server.wait_for_diagnostics(uri, timeout=5.0)

        assert len(found) == 1
        assert found[0]["message"] == "undefined name 'oops'"
        assert found[0]["range"]["start"]["line"] == 1
    finally:
        await server.close()


async def test_an_edited_document_replaces_its_diagnostics(project: Path) -> None:
    """An empty publish is how a server says the problem is gone."""
    server = await _stub_server(project)
    try:
        uri = await server.open_document(project / "widget.py")
        assert await server.wait_for_diagnostics(uri, timeout=5.0)

        # Second open of the same path is a didChange, and the stub answers
        # with an empty list.
        await server.open_document(project / "widget.py", "class Widget:\n    fine = 1\n")
        assert await server.wait_for_diagnostics(uri, timeout=5.0) == []
    finally:
        await server.close()


async def test_concurrent_requests_get_their_own_answers(project: Path) -> None:
    """Request correlation by id: the thing framing bugs break first."""
    server = await _stub_server(project)
    try:
        await server.open_document(project / "widget.py")
        uri = to_uri(project / "widget.py")
        results = await asyncio.gather(
            server.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}}),
            server.request(
                "textDocument/references",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": 0, "character": 6},
                    "context": {"includeDeclaration": True},
                },
            ),
            server.request(
                "textDocument/hover",
                {"textDocument": {"uri": uri}, "position": {"line": 0, "character": 6}},
            ),
        )
        symbols, references, hover = results
        assert symbols[0]["name"] == "Widget"
        assert len(references) == 2
        assert hover["contents"]["value"] == "**Widget**"
    finally:
        await server.close()


async def test_a_server_that_will_not_start_is_reported_not_raised(project: Path) -> None:
    server = LanguageServer(project, _stub_spec(), [sys.executable, "-c", "raise SystemExit(3)"])
    with pytest.raises(LSPError):
        await server.start()
    await server.close()


# ------------------------------------------------------------------ adapter


async def test_the_adapter_speaks_editor_coordinates(project: Path) -> None:
    """One-based on the wire, zero-based inside. Off-by-one lives here or nowhere."""
    adapter = _adapter(project)
    try:
        found = await adapter.diagnostics(project / "widget.py", timeout=5.0)

        assert len(found) == 1
        problem = found[0]
        # The stub reported LSP line 1, character 4 — the editor's line 2,
        # column 5.
        assert (problem["line"], problem["column"]) == (2, 5)
        assert problem["severity"] == "error"
        assert problem["source"] == "stub"
        assert problem["code"] == "F821"
    finally:
        await adapter.close()


async def test_definitions_and_references_come_back_repository_relative(
    project: Path,
) -> None:
    """Both LSP location shapes, normalised to a path the editor can open."""
    adapter = _adapter(project)
    try:
        definitions = await adapter.definition(project / "widget.py", 0, 6)
        references = await adapter.references(project / "widget.py", 0, 6)

        # LocationLink (targetUri/targetSelectionRange) resolved correctly.
        assert definitions == [{"path": "widget.py", "line": 1, "column": 1}]
        assert [item["line"] for item in references] == [1, 4]
        assert all(item["path"] == "widget.py" for item in references)
    finally:
        await adapter.close()


async def test_nested_document_symbols_are_flattened_with_their_children(
    project: Path,
) -> None:
    adapter = _adapter(project)
    try:
        symbols = await adapter.symbols(project / "widget.py")

        assert [(item.name, item.kind, item.line) for item in symbols] == [
            ("Widget", "class", 1),
            ("render", "method", 2),
        ]
        assert symbols[0].signature == "class Widget"
    finally:
        await adapter.close()


async def test_a_rename_is_returned_as_edits_rather_than_applied(project: Path) -> None:
    """A cross-file refactor should be seen before it happens."""
    adapter = _adapter(project)
    original = (project / "widget.py").read_text(encoding="utf-8")
    try:
        edits = await adapter.rename_edits(project / "widget.py", 0, 6, "Gadget")

        assert edits == {
            "widget.py": [
                {
                    "start_line": 1,
                    "start_column": 7,
                    "end_line": 1,
                    "end_column": 13,
                    "text": "Gadget",
                }
            ]
        }
        # Nothing was written.
        assert (project / "widget.py").read_text(encoding="utf-8") == original
    finally:
        await adapter.close()


async def test_a_language_with_no_server_says_so(project: Path) -> None:
    """ "Nothing installed" and "nothing wrong" must never look the same."""
    adapter = PooledLSPAdapter(project)
    try:
        with pytest.raises(LSPError) as caught:
            await adapter.diagnostics(project / "widget.py", timeout=1.0)
        assert "No language server available" in str(caught.value)
    finally:
        await adapter.close()


async def test_a_file_type_no_server_covers_is_refused_early(project: Path) -> None:
    (project / "notes.txt").write_text("plain\n", encoding="utf-8")
    adapter = PooledLSPAdapter(project)
    try:
        with pytest.raises(LSPError):
            await adapter.diagnostics(project / "notes.txt", timeout=1.0)
    finally:
        await adapter.close()


# ---------------------------------------------------------------- detection


def test_language_ids_cover_the_servers_that_exist() -> None:
    assert language_id_for("a.py") == "python"
    assert language_id_for("a.tsx") == "typescriptreact"
    assert language_id_for("a.rs") == "rust"
    # A file no server here can analyse gets no id, which is what makes the
    # route answer "unsupported" instead of "no problems".
    assert language_id_for("a.txt") == ""


def test_a_project_pinned_server_beats_a_global_one(tmp_path: Path) -> None:
    """A project's own tool is the one that matches its config and version."""
    local = tmp_path / "node_modules" / ".bin"
    local.mkdir(parents=True)
    binary = local / "typescript-language-server"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    spec = ServerSpec(
        id="typescript-language-server",
        label="TypeScript",
        executable="typescript-language-server",
        arguments=("--stdio",),
    )

    resolved = resolve_executable(tmp_path, spec)

    assert resolved is not None
    assert resolved[0] == str(binary)
    assert resolved[1] == "--stdio"


def test_missing_servers_are_listed_with_how_to_install_them(tmp_path: Path) -> None:
    """ "No diagnostics" has to be actionable, not just true."""
    rows = available_servers(tmp_path)

    assert rows
    pyright = next(row for row in rows if row["id"] == "pyright")
    assert pyright["available"] is False
    assert "pyright" in pyright["install"]
    assert "python" in pyright["languages"]


def test_uris_round_trip_through_paths(tmp_path: Path) -> None:
    target = tmp_path / "some dir" / "file name.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")

    assert from_uri(to_uri(target)) == target.resolve()


def _adapter(project: Path) -> PooledLSPAdapter:
    """An adapter whose python server is the stub."""
    adapter = PooledLSPAdapter(project)
    slot = adapter.pool._slot("python")
    server = LanguageServer(project, _stub_spec(), [sys.executable, str(project / "_stub_lsp.py")])
    slot.server = server

    async def ensure() -> LanguageServer:
        if not server.alive:
            await server.start()
        return server

    # The pool would otherwise try the real specs and find none installed.
    adapter.pool.server_for = lambda language: ensure()  # type: ignore[method-assign]
    return adapter
