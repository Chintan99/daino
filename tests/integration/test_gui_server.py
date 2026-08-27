"""End-to-end tests for the Daino GUI FastAPI backend.

These exercise the real routes and WebSocket transport against a real project
context (no mocked services), covering the Definition-of-Done backend surface:
sessions, file read/write + conflict handling, Git status, design persistence
and mutation, preview detection, terminal lifecycle, and event streaming.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from daino.application import initialize_project, open_project
from daino.events import GitChanged
from daino.server.app import create_app
from daino.services.terminal import MAX_SESSIONS

#: What a browser on the loopback listener sends. Starlette's test client
#: hard-codes ``ws://testserver`` for WebSockets, so the Host has to be supplied
#: explicitly — and the origin policy rightly refuses anything else.
LOCAL_HOST = "127.0.0.1:4173"
LOCAL_ORIGIN = f"http://{LOCAL_HOST}"


@pytest.fixture
def client(git_repo: Path) -> Iterator[TestClient]:
    initialize_project(git_repo)
    context = open_project(git_repo)
    app = create_app(context)
    # A real browser addresses the loopback listener, and the origin policy
    # checks the Host header, so the test client must look like one.
    with TestClient(app, base_url="http://127.0.0.1:4173") as test_client:
        test_client.app_root = git_repo  # type: ignore[attr-defined]
        yield test_client
    context.close()


def test_health_and_workspace(client: TestClient) -> None:
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    workspace = client.get("/api/workspace").json()
    assert Path(workspace["root"]).name == Path(client.app_root).name  # type: ignore[attr-defined]


def test_sessions_lifecycle(client: TestClient) -> None:
    created = client.post("/api/sessions", json={"title": "GUI session"}).json()
    assert created["id"]
    listing = client.get("/api/sessions").json()["sessions"]
    assert any(item["id"] == created["id"] for item in listing)
    messages = client.get(f"/api/sessions/{created['id']}/messages").json()
    assert messages["messages"] == []


def test_file_tree_read_write_and_conflict(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "sample.py").write_text("x = 1\n", encoding="utf-8")

    tree = client.get("/api/files/tree", params={"path": ""}).json()
    assert any(entry["name"] == "sample.py" for entry in tree["entries"])

    read = client.get("/api/files/read", params={"path": "sample.py"}).json()
    assert read["content"] == "x = 1\n"
    assert read["language"] == "python"
    base_hash = read["hash"]

    ok = client.put(
        "/api/files/write",
        json={"path": "sample.py", "content": "x = 2\n", "base_hash": base_hash},
    )
    assert ok.status_code == 200
    assert (root / "sample.py").read_text(encoding="utf-8") == "x = 2\n"

    # A stale base_hash must be rejected rather than clobbering the newer content.
    conflict = client.put(
        "/api/files/write",
        json={"path": "sample.py", "content": "x = 3\n", "base_hash": base_hash},
    )
    assert conflict.status_code == 409

    created = client.post("/api/files/create", json={"path": "pkg/new.txt", "is_dir": False})
    assert created.status_code == 200
    assert (root / "pkg" / "new.txt").exists()

    deleted = client.request("DELETE", "/api/files/delete", params={"path": "pkg/new.txt"})
    assert deleted.status_code == 200
    assert not (root / "pkg" / "new.txt").exists()


def test_file_path_escape_is_rejected(client: TestClient) -> None:
    response = client.get("/api/files/read", params={"path": "../../etc/passwd"})
    assert response.status_code in (400, 404)


def test_file_search(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "needle.py").write_text("MAGIC_TOKEN = 1\n", encoding="utf-8")
    result = client.get("/api/files/search", params={"q": "MAGIC_TOKEN"}).json()
    assert any(match["path"] == "needle.py" for match in result["matches"])


def test_git_status(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "changed.txt").write_text("hi\n", encoding="utf-8")
    status = client.get("/api/git/status").json()
    assert status["repository"] is True
    tracked = {item["path"] for item in status["untracked"]}
    assert "changed.txt" in tracked


def test_design_create_mutate_persist(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    design = client.post(
        "/api/designs", json={"name": "Checkout Architecture", "type": "architecture"}
    ).json()
    design_id = design["id"]
    assert design_id == "checkout-architecture"

    client.post(
        f"/api/designs/{design_id}/nodes",
        json={"label": "Payment Service", "node_type": "service", "node_id": "payment"},
    )
    client.post(
        f"/api/designs/{design_id}/nodes",
        json={"label": "Postgres", "node_type": "database", "node_id": "db"},
    )
    connected = client.post(
        f"/api/designs/{design_id}/edges", json={"source": "payment", "target": "db"}
    ).json()
    assert len(connected["nodes"]) == 2
    assert len(connected["edges"]) == 1

    # Persisted to disk under the project state directory.
    assert (root / ".daino" / "designs" / design_id / "design.json").is_file()

    fetched = client.get(f"/api/designs/{design_id}").json()
    assert fetched["version"] >= 3

    updated = client.patch(
        f"/api/designs/{design_id}/nodes/payment", json={"x": 120, "y": 40}
    ).json()
    payment = next(node for node in updated["nodes"] if node["id"] == "payment")
    assert payment["position"]["x"] == 120


def test_generate_design_from_code(client: TestClient) -> None:
    generated = client.post("/api/designs/generate-from-code").json()
    assert generated["type"] == "architecture"
    assert len(generated["nodes"]) >= 1


def test_preview_detection(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "package.json").write_text('{"scripts": {"dev": "vite"}}', encoding="utf-8")
    detected = client.get("/api/preview/detect").json()["commands"]
    assert any("dev" in command["command"] for command in detected)
    status = client.get("/api/preview/status").json()
    assert status["running"] is False


def test_terminal_lifecycle(client: TestClient) -> None:
    created = client.post("/api/terminals").json()
    terminal_id = created["id"]
    assert terminal_id in client.get("/api/terminals").json()["terminals"]
    closed = client.request("DELETE", f"/api/terminals/{terminal_id}").json()
    assert closed["closed"] is True


def test_websocket_session_stream_and_events(client: TestClient) -> None:
    with client.websocket_connect(
        "/ws/session/latest", headers={"host": LOCAL_HOST, "origin": LOCAL_ORIGIN}
    ) as ws:
        first = ws.receive_json()
        assert first["type"] == "session"
        assert first["session_id"]

        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"

        # An event published on the shared bus is relayed to the socket.
        client.app.state.gui.context.events.publish(GitChanged(paths=["a.py"]))
        message = ws.receive_json()
        assert message["type"] == "event"
        assert message["event"]["kind"] == "GitChanged"


def test_git_file_diff_and_staging(client: TestClient) -> None:
    """The diff view needs whole-file before/after, and staging must round-trip."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    tracked = root / "module.py"
    tracked.write_text("first = 1\n", encoding="utf-8")
    from daino.git import GitClient

    git = GitClient(root)
    git.run("add", "--", "module.py")
    git.commit("add module")

    tracked.write_text("first = 1\nsecond = 2\n", encoding="utf-8")

    working = client.get("/api/git/file", params={"path": "module.py"}).json()
    assert working["repository"] is True
    assert working["original"] == "first = 1\n"
    assert working["modified"] == "first = 1\nsecond = 2\n"
    assert working["language"] == "python"
    assert working["binary"] is False

    assert client.post("/api/git/stage", json={"paths": ["module.py"]}).status_code == 200
    staged = client.get("/api/git/file", params={"path": "module.py", "staged": True}).json()
    assert staged["original"] == "first = 1\n"
    assert staged["modified"] == "first = 1\nsecond = 2\n"
    assert any(
        item["path"] == "module.py" for item in client.get("/api/git/status").json()["staged"]
    )

    assert client.post("/api/git/unstage", json={"paths": ["module.py"]}).status_code == 200
    assert client.get("/api/git/status").json()["staged"] == []

    client.post("/api/git/discard", json={"paths": ["module.py"]})
    assert tracked.read_text(encoding="utf-8") == "first = 1\n"


def test_git_file_diff_rejects_escaping_paths(client: TestClient) -> None:
    assert client.get("/api/git/file", params={"path": "../outside.txt"}).status_code == 400


def test_insight_views_are_served(client: TestClient) -> None:
    """Every workspace view the TUI offers has a browser endpoint behind it."""
    assert client.get("/api/logs").json() == {"total": 0, "matched": 0, "events": []}
    assert client.get("/api/map/prompts").json() == {"prompts": []}
    assert client.get("/api/missions").json() == {"missions": []}
    assert client.get("/api/checkpoints").json() == {"checkpoints": []}
    assert client.get("/api/approvals").json() == {"approvals": []}

    latest = client.get("/api/qa/latest").json()
    assert latest == {"running": False, "report": None}
    assert client.get("/api/qa/history").json() == {"running": False, "reports": []}
    assert client.get("/api/qa/reports/qa-nope").status_code == 404
    assert client.post("/api/qa/cancel").json() == {"cancelled": False}


def test_execution_map_records_a_prompt(client: TestClient) -> None:
    """A mission shows up in the map index and resolves to a trace."""
    from daino.schemas import ProjectMode

    state = client.app.state.gui
    mission = state.missions.core.create("Add a health endpoint", ProjectMode.DIRECT)

    prompts = client.get("/api/map/prompts").json()["prompts"]
    assert [item["mission_id"] for item in prompts] == [mission.id]
    assert prompts[0]["request"] == "Add a health endpoint"

    trace = client.get(f"/api/map/prompts/{mission.id}").json()
    assert trace["mission_id"] == mission.id
    assert isinstance(trace["steps"], list)
    assert client.get("/api/map/prompts/does-not-exist").status_code == 404

    missions = client.get("/api/missions").json()["missions"]
    assert [item["id"] for item in missions] == [mission.id]
    assert client.get(f"/api/missions/{mission.id}").json()["mission"]["id"] == mission.id
    assert client.get("/api/missions/does-not-exist").status_code == 404


def test_audit_log_filtering(client: TestClient) -> None:
    state = client.app.state.gui
    state.audit.emit("MissionCreated", mission_id="m-1")
    state.audit.emit("ToolCompleted", mission_id="m-2", tool="filesystem.read")

    everything = client.get("/api/logs").json()
    assert everything["total"] == 2
    assert [item["event"] for item in everything["events"]] == [
        "MissionCreated",
        "ToolCompleted",
    ]

    filtered = client.get("/api/logs", params={"q": "filesystem"}).json()
    assert filtered["matched"] == 1
    assert filtered["events"][0]["mission_id"] == "m-2"


def test_repository_index_endpoint(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "app.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")

    built = client.post("/api/repository/index").json()
    assert built["file_count"] >= 1

    info = client.get("/api/repository").json()
    assert info["file_count"] >= 1
    assert "python" in {key.lower() for key in info["languages"]}
    assert isinstance(info["summary"], str)


def test_design_holds_a_dropped_html_artifact(client: TestClient) -> None:
    """An HTML file dropped on the canvas persists verbatim, with its geometry."""
    design = client.post("/api/designs", json={"name": "Canvas", "type": "prototype"}).json()
    updated = client.post(
        f"/api/designs/{design['id']}/nodes",
        json={
            "label": "landing.html",
            "node_type": "artifact",
            "x": 40,
            "y": 60,
            "data": {
                "kind": "html",
                "content": "<h1>Landing</h1>",
                "filename": "landing.html",
                "width": 460,
                "height": 320,
            },
        },
    ).json()
    node = updated["nodes"][-1]
    assert node["type"] == "artifact"
    assert node["data"]["content"] == "<h1>Landing</h1>"

    resized = client.patch(
        f"/api/designs/{design['id']}/nodes/{node['id']}",
        json={"data": {**node["data"], "width": 600, "height": 400}},
    ).json()
    assert resized["nodes"][-1]["data"]["width"] == 600

    reloaded = client.get(f"/api/designs/{design['id']}").json()
    assert reloaded["nodes"][-1]["data"]["content"] == "<h1>Landing</h1>"


def test_documentation_is_served_to_the_gui(client: TestClient) -> None:
    """`/docs` is Daino's usage documentation; the API reference moved aside."""
    index = client.get("/api/docs").json()
    assert index["available"] is True
    slugs = {item["slug"] for item in index["pages"]}
    assert {"installation", "gui", "configuration"} <= slugs
    # Getting-started pages lead, and every page is grouped for the sidebar.
    assert index["pages"][0]["slug"] == "installation"
    assert all(item["section"] and item["title"] for item in index["pages"])

    page = client.get("/api/docs/gui").json()
    assert page["slug"] == "gui"
    assert page["markdown"].startswith("# ")
    assert page["title"] == "Browser IDE (GUI)"

    assert client.get("/api/docs/no-such-page").status_code == 404
    assert client.get("/api/docs/UPPER").status_code == 400

    # A slug is never allowed to address anything outside the docs directory.
    # Asserted against the handler: an HTTP client normalises "../" away before
    # the request is even sent, so going through the wire proves nothing here.
    from fastapi import HTTPException

    from daino.server.routes.docs import read_page

    for bad in ("../pyproject", "a/b", ".hidden", "", "UPPER"):
        with pytest.raises(HTTPException) as caught:
            read_page(bad)
        assert caught.value.status_code == 400

    # The generated reference is still available, just not at /docs.
    assert client.get("/api-docs").status_code == 200


def test_settings_read_and_patch(client: TestClient) -> None:
    """The Settings menu's server half: read, route, and persist."""
    from daino.config import load_settings
    from daino.config.models import ModelProfileConfig, ProviderConfig

    root: Path = client.app_root  # type: ignore[attr-defined]
    settings = client.app.state.gui.context.settings  # type: ignore[attr-defined]
    settings.providers["local-ollama"] = ProviderConfig(
        type="ollama", base_url="http://127.0.0.1:11434/v1", model="qwen2.5-coder:7b"
    )
    settings.models["local-ollama"] = ModelProfileConfig(
        provider="local-ollama", model="qwen2.5-coder:7b", local=True
    )

    payload = client.get("/api/settings").json()
    assert "debugger" in payload["roles"]
    assert [item["name"] for item in payload["providers"]] == ["local-ollama"]
    # An unsaved in-memory provider reads as project scope, not inherited.
    assert payload["providers"][0]["scope"] == "project"
    assert payload["runtime"]["default"] in {"local", "docker", "ssh"}

    # A provider choice routes every agent role, including the debugger.
    routed = client.patch("/api/settings", json={"default_provider": "local-ollama"}).json()
    assert set(routed["routing"].values()) == {"local-ollama"}

    # One role can then be pointed somewhere else on its own.
    settings.models["strong-cloud"] = ModelProfileConfig(
        provider="local-ollama", model="anthropic/claude-sonnet-4"
    )
    single = client.patch(
        "/api/settings", json={"routing": {"debugger": "strong-cloud"}}
    ).json()
    assert single["routing"]["debugger"] == "strong-cloud"
    assert single["routing"]["builder"] == "local-ollama"

    # Runtime, network access, approvals, and log level are all persisted.
    updated = client.patch(
        "/api/settings",
        json={
            "runtime": "local",
            "network_access": "allowed",
            "log_level": "DEBUG",
            "require_approval_for_install": False,
        },
    ).json()
    assert updated["runtime"] == {
        **updated["runtime"],
        "default": "local",
        "network_access": "allowed",
    }
    assert updated["observability"]["log_level"] == "DEBUG"
    assert updated["security"]["require_approval_for_install"] is False

    on_disk = load_settings(root)
    assert on_disk.runtime.default == "local"
    assert on_disk.routing["debugger"] == "strong-cloud"

    # Unknown names are rejected rather than written.
    assert client.patch("/api/settings", json={"default_provider": "nope"}).status_code == 400
    assert (
        client.patch("/api/settings", json={"routing": {"architect": "nope"}}).status_code
        == 400
    )
    assert (
        client.patch("/api/settings", json={"routing": {"nope": "local-ollama"}}).status_code
        == 400
    )
    assert client.patch("/api/settings", json={"runtime": "vm"}).status_code == 422

    # Reloading re-reads the file that was just written.
    reloaded = client.post("/api/settings/reload").json()
    assert reloaded["runtime"]["default"] == "local"


def test_foreign_origin_is_refused_everywhere(client: TestClient) -> None:
    """A page on another site must not be able to drive this local API.

    WebSockets are exempt from CORS, so without an Origin check any site the
    user has open can run shell commands and answer the agent's own approval
    prompts. DNS rebinding is the same attack with a forged Host.
    """
    evil = {"origin": "https://evil.example"}

    # REST: a "simple" cross-origin POST reaches the server even under CORS.
    assert client.get("/api/workspace", headers=evil).status_code == 403
    assert client.post("/api/terminals", json={}, headers=evil).status_code == 403
    assert client.patch("/api/settings", json={"runtime": "local"}, headers=evil).status_code == 403

    # DNS rebinding: loopback listener, attacker-controlled hostname.
    assert client.get("/api/workspace", headers={"host": "evil.example"}).status_code == 403

    # WebSockets: the session socket drives the agent, the terminal socket a shell.
    created = client.post("/api/terminals", json={}).json()
    for path in ("/ws/session/latest", f"/ws/terminal/{created['id']}"):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                path, headers={"host": LOCAL_HOST, **evil}
            ):
                pass  # pragma: no cover - the handshake never completes

    # The IDE's own page, and non-browser clients, are unaffected.
    assert client.get("/api/workspace", headers={"origin": LOCAL_ORIGIN}).status_code == 200
    assert client.get("/api/workspace").status_code == 200
    # The Vite dev server is allowed, so `npm run dev` still works.
    assert (
        client.get("/api/workspace", headers={"origin": "http://localhost:5173"}).status_code
        == 200
    )


def test_only_one_agent_turn_runs_at_a_time(client: TestClient) -> None:
    """A second tab must wait rather than interleave edits with the first."""
    state = client.app.state.gui  # type: ignore[attr-defined]
    assert not state.turn_lock.locked()

    # Stand in for a turn started by another connection: the handler only reads
    # `locked()`, and an asyncio.Lock is not bound to a loop until it is awaited.
    asyncio.run(state.turn_lock.acquire())

    with client.websocket_connect(
        "/ws/session/latest", headers={"host": LOCAL_HOST, "origin": LOCAL_ORIGIN}
    ) as ws:
        ws.receive_json()  # session id
        ws.send_json({"type": "user_message", "text": "hello"})
        message = ws.receive_json()
    assert message["type"] == "error"
    assert "already running" in message["message"]
    state.turn_lock.release()


def test_terminals_are_reaped_when_no_client_returns(client: TestClient) -> None:
    """Every page load opens a shell; closed tabs must not leak PTYs forever."""
    terminals = client.app.state.gui.terminals  # type: ignore[attr-defined]
    first = client.post("/api/terminals", json={}).json()["id"]

    # A shell with a live client is never reaped, however long it idles.
    terminals.attach(first)
    assert terminals.prune(idle_seconds=0) == []
    assert first in terminals.list_ids()

    # Once the client goes away, the idle countdown starts.
    terminals.detach(first)
    assert terminals.prune(idle_seconds=3600) == []
    assert terminals.prune(idle_seconds=0) == [first]
    assert terminals.list_ids() == []

    # And a project cannot accumulate shells without bound.
    for _ in range(MAX_SESSIONS):
        client.post("/api/terminals", json={})
    for terminal_id in terminals.list_ids():
        terminals.attach(terminal_id)
    assert client.post("/api/terminals", json={}).status_code == 429


def test_provider_form_test_and_save(client: TestClient) -> None:
    """The agent panel's provider form: probe, save, and re-route."""
    unreachable = {
        "name": "local-ollama",
        "type": "ollama",
        # Port 1 is never a model server, so this exercises the failure path.
        "base_url": "http://127.0.0.1:1/v1",
        "model": "qwen2.5-coder:7b",
    }

    # Testing reports a verdict instead of raising, and writes nothing.
    probed = client.post("/api/settings/providers/test", json=unreachable).json()
    assert probed["provider"]["connected"] is False
    assert probed["provider"]["detail"]
    assert client.get("/api/settings").json()["providers"] == []

    # Every step is reported separately: a single green tick for "the port
    # answered" is exactly the false confidence this is meant to avoid.
    steps = {check["name"]: check for check in probed["checks"]}
    assert list(steps) == ["endpoint", "credentials", "model", "generation"]
    assert steps["endpoint"]["status"] == "fail"
    assert steps["endpoint"]["detail"]
    # Nothing downstream of an unreachable endpoint is claimed as passing.
    assert [steps[name]["status"] for name in ("credentials", "model", "generation")] == [
        "skip",
        "skip",
        "skip",
    ]

    # A catalog request against the same dead endpoint is a client error.
    assert client.post("/api/settings/providers/catalog", json=unreachable).status_code == 400

    # Saving a self-hosted provider works even while it is down, and routes to it.
    saved = client.post("/api/settings/providers", json=unreachable).json()
    assert saved["provider"]["connected"] is False
    assert set(saved["settings"]["routing"].values()) == {"local-ollama"}
    assert [item["name"] for item in saved["settings"]["providers"]] == ["local-ollama"]
    assert saved["settings"]["providers"][0]["scope"] == "project"

    # Editing keeps one entry rather than adding a second.
    edited = client.post(
        "/api/settings/providers",
        json={**unreachable, "model": "llama3.2"},
    ).json()
    assert [item["model"] for item in edited["settings"]["providers"]] == ["llama3.2"]

    # A nameless provider is refused before anything is written.
    assert (
        client.post("/api/settings/providers", json={**unreachable, "name": " "}).status_code
        == 400
    )


def test_notification_and_keep_awake_settings(client: TestClient) -> None:
    """The browser can turn both attention features on and off."""
    from daino.config import load_settings

    root: Path = client.app_root  # type: ignore[attr-defined]
    payload = client.get("/api/settings").json()
    assert payload["keep_awake"] is True
    assert payload["notifications"] == {
        "enabled": True,
        "desktop": True,
        "terminal_bell": True,
        "on_completed": True,
        "on_failed": True,
        "on_approval": True,
    }

    updated = client.patch(
        "/api/settings",
        json={
            "keep_awake": False,
            "notify_on_completed": False,
            "notify_terminal_bell": False,
        },
    ).json()
    assert updated["keep_awake"] is False
    assert updated["notifications"]["on_completed"] is False
    assert updated["notifications"]["terminal_bell"] is False
    assert updated["notifications"]["on_failed"] is True

    # The live services honour the change without a restart, and the running
    # inhibitor is dropped rather than held until the process exits.
    attention = client.app.state.gui.missions.attention  # type: ignore[attr-defined]
    assert attention.keep_awake.enabled is False
    assert attention.notifications.config.on_completed is False

    on_disk = load_settings(root)
    assert on_disk.keep_awake is False
    assert on_disk.notifications.on_completed is False


def test_attachments_are_stored_inside_the_state_directory(client: TestClient) -> None:
    """A file dropped on the chat box becomes a path the agent can open."""
    import base64

    root: Path = client.app_root  # type: ignore[attr-defined]
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()

    first = client.post(
        "/api/files/attach", json={"name": "Screen Shot 2026.png", "content_base64": png}
    ).json()
    assert first["path"] == ".daino/attachments/Screen-Shot-2026.png"
    assert (root / first["path"]).read_bytes().startswith(b"\x89PNG")

    # Two screenshots pasted in a row are two attachments, not one overwritten.
    second = client.post(
        "/api/files/attach", json={"name": "Screen Shot 2026.png", "content_base64": png}
    ).json()
    assert second["path"] == ".daino/attachments/Screen-Shot-2026-1.png"

    # A crafted name cannot escape the attachment directory.
    escaped = client.post(
        "/api/files/attach", json={"name": "../../etc/passwd", "content_base64": png}
    ).json()
    assert escaped["path"] == ".daino/attachments/etc-passwd"
    assert (root / ".daino" / "attachments" / "etc-passwd").is_file()

    assert (
        client.post(
            "/api/files/attach", json={"name": "x.png", "content_base64": "not base64!"}
        ).status_code
        == 400
    )
    oversized = base64.b64encode(b"x" * 9_000_000).decode()
    assert (
        client.post(
            "/api/files/attach", json={"name": "big.bin", "content_base64": oversized}
        ).status_code
        == 413
    )
    # Attachments are conversation material, so they stay out of the working tree
    # the user is about to review.
    assert client.get("/api/git/status").json()["untracked"] == []


def test_the_browser_can_leave_the_session_unpinned(client: TestClient) -> None:
    """Auto must be reachable, or the browser can never escalate.

    A pinned session is deliberately excluded from escalation to a stronger
    model. The browser used to send a concrete profile with every message, so it
    was always pinned and a stalled turn could never recover — unlike the
    terminal client, which is unpinned until the user picks a model.
    """
    from daino.config.models import ModelProfileConfig, ProviderConfig

    settings = client.app.state.gui.context.settings  # type: ignore[attr-defined]
    settings.providers["local"] = ProviderConfig(
        type="ollama", base_url="http://127.0.0.1:11434/v1", model="qwen"
    )
    settings.models["local"] = ModelProfileConfig(provider="local", model="qwen")
    providers = client.app.state.gui.providers  # type: ignore[attr-defined]
    session = client.post("/api/sessions", json={"title": "pinning"}).json()["id"]

    assert providers.session_profile(session) == ""

    pinned = client.post(f"/api/sessions/{session}/model", json={"profile": "local"})
    assert pinned.status_code == 200
    assert providers.session_profile(session) == "local"

    # An empty profile means auto, and clears the pin rather than erroring.
    unpinned = client.post(f"/api/sessions/{session}/model", json={"profile": ""})
    assert unpinned.status_code == 200
    assert unpinned.json()["profile"] == ""
    assert providers.session_profile(session) == ""

    assert (
        client.post(f"/api/sessions/{session}/model", json={"profile": "nope"}).status_code
        == 400
    )


def test_a_refresh_does_not_kill_a_running_turn(client: TestClient) -> None:
    """The regression from ~/vasukitest/project4: work died on page reload.

    Closing the socket used to cancel the turn task. CancelledError is not an
    Exception, so nothing reported it: the log stopped mid-action and the mission
    was left orphaned at status "created".
    """
    import asyncio

    state = client.app.state.gui  # type: ignore[attr-defined]
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow_chat(text: str, session_id: str, **_: object) -> object:
        started.set()
        await release.wait()
        finished.set()
        raise RuntimeError("finished after the client left")

    state.missions.chat = slow_chat  # type: ignore[method-assign]

    with client.websocket_connect(
        "/ws/session/latest", headers={"host": LOCAL_HOST, "origin": LOCAL_ORIGIN}
    ) as ws:
        hello = ws.receive_json()
        assert hello["turn_running"] is False
        ws.send_json({"type": "user_message", "text": "long job"})
        # Wait for the turn to actually be in flight before "refreshing".
        for _ in range(200):
            if state.turn_lock.locked():
                break
            ws.send_json({"type": "ping"})
            ws.receive_json()
        assert state.turn_lock.locked(), "the turn never started"

    # The tab is gone. The turn must still be running, and the lock still held.
    assert state.turn_lock.locked()
    task = state.active_turn
    assert task is not None and not task.done()

    # A reconnecting client is told the work is still in flight.
    with client.websocket_connect(
        "/ws/session/latest", headers={"host": LOCAL_HOST, "origin": LOCAL_ORIGIN}
    ) as ws:
        assert ws.receive_json()["turn_running"] is True

        # And can stop it, even though another connection started it.
        release.set()
        ws.send_json({"type": "cancel"})

    for _ in range(200):
        if not state.turn_lock.locked():
            break
    assert not state.turn_lock.locked(), "the turn lock was never released"


def test_an_abandoned_approval_is_denied_rather_than_left_hanging(
    client: TestClient,
) -> None:
    """Keeping the turn alive must not let it wait forever for a gone client.

    An approval is answered by the browser. With no browser, the future would
    never resolve, holding the turn — and the project's turn lock — open for the
    life of the process.
    """
    import asyncio

    state = client.app.state.gui  # type: ignore[attr-defined]
    answered: list[tuple[bool, bool]] = []
    asked = asyncio.Event()

    async def chat_needing_approval(
        text: str, session_id: str, *, approve=None, **_: object
    ) -> object:
        asked.set()
        answered.append(await approve("rm -rf build/", "writes outside the workspace"))
        raise RuntimeError("done")

    state.missions.chat = chat_needing_approval  # type: ignore[method-assign]

    with client.websocket_connect(
        "/ws/session/latest", headers={"host": LOCAL_HOST, "origin": LOCAL_ORIGIN}
    ) as ws:
        ws.receive_json()
        ws.send_json({"type": "user_message", "text": "clean up"})
        # The approval request reaches the client…
        request = ws.receive_json()
        while request.get("type") != "approval_request":
            request = ws.receive_json()
        assert request["command"] == "rm -rf build/"
    # …and then the tab closes without answering.

    for _ in range(200):
        if answered:
            break
    assert answered == [(False, False)], "the agent was never told the answer"
    for _ in range(200):
        if not state.turn_lock.locked():
            break
    assert not state.turn_lock.locked(), "the turn lock was held by a dead approval"


def test_sessions_are_named_and_listed_so_they_can_be_switched(client: TestClient) -> None:
    """One project, several conversations — legible enough to choose between.

    Every session used to keep the placeholder title, so a session list was
    three identical rows and the browser simply stayed in the newest one
    forever, carrying its whole history into every prompt.
    """
    first = client.post("/api/sessions", json={}).json()["id"]
    second = client.post("/api/sessions", json={}).json()["id"]

    state = client.app.state.gui  # type: ignore[attr-defined]
    state.missions.add_message(
        first, kind="user", role="user", content="add a dark theme to test.html"
    )
    state.missions.add_message(first, kind="agent", role="builder", content="done")
    state.missions.add_message(second, kind="user", role="user", content="why is the build slow?")

    listing = {item["id"]: item for item in client.get("/api/sessions").json()["sessions"]}
    # The first request names the session; the placeholder is gone.
    assert listing[first]["title"] == "add a dark theme to test.html"
    assert listing[second]["title"] == "why is the build slow?"
    # And the size of each conversation is visible, since it is prompt weight.
    assert listing[first]["message_count"] == 2
    assert listing[second]["message_count"] == 1

    # A later request does not rename an already-named session.
    state.missions.add_message(second, kind="user", role="user", content="never mind")
    renamed = {i["id"]: i["title"] for i in client.get("/api/sessions").json()["sessions"]}
    assert renamed[second] == "why is the build slow?"

    # An explicit title survives too.
    titled = client.post("/api/sessions", json={"title": "Release checks"}).json()["id"]
    state.missions.add_message(titled, kind="user", role="user", content="run the tests")
    kept = {i["id"]: i["title"] for i in client.get("/api/sessions").json()["sessions"]}
    assert kept[titled] == "Release checks"

    # Each session's transcript is its own; switching is just choosing an id.
    assert len(client.get(f"/api/sessions/{first}/messages").json()["messages"]) == 2
    assert len(client.get(f"/api/sessions/{titled}/messages").json()["messages"]) == 1


def test_a_new_session_starts_without_the_old_one_s_history(client: TestClient) -> None:
    """The reason to start one: history is what each turn sends as context."""
    state = client.app.state.gui  # type: ignore[attr-defined]
    old = client.post("/api/sessions", json={}).json()["id"]
    for index in range(6):
        state.missions.add_message(old, kind="user", role="user", content=f"request {index}")
        state.missions.add_message(old, kind="agent", role="builder", content=f"answer {index}")

    assert len(state.missions.conversation_history(old)) == 12

    fresh = client.post("/api/sessions", json={}).json()["id"]
    assert state.missions.conversation_history(fresh) == []
    # Todos and interaction mode start clean as well.
    assert client.get(f"/api/sessions/{fresh}/todos").json()["todos"] == []
    assert (
        client.get("/api/agent/config", params={"session_id": fresh}).json()["autonomy"]["mode"]
        == "ask"
    )
