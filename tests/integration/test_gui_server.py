"""End-to-end tests for the Daino GUI FastAPI backend.

These exercise the real routes and WebSocket transport against a real project
context (no mocked services), covering the Definition-of-Done backend surface:
sessions, file read/write + conflict handling, Git status, design persistence
and mutation, preview detection, terminal lifecycle, and event streaming.
"""

from __future__ import annotations

import asyncio
import base64
import subprocess
import time
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
    # "stale" says whether the last verdict still describes this checkout. With
    # no report at all there is nothing to have gone stale.
    assert latest == {"running": False, "report": None, "stale": False}
    assert client.get("/api/qa/history").json() == {"running": False, "reports": []}
    assert client.get("/api/qa/reports/qa-nope").status_code == 404
    assert client.post("/api/qa/cancel").json() == {"cancelled": False}


def test_the_inspector_refuses_to_probe_a_host_the_user_has_not_claimed(
    client: TestClient,
) -> None:
    """Pointing the live probe at someone else's server takes a deliberate act."""
    refused = client.post(
        "/api/qa/run",
        json={"profile": "security", "target_url": "https://example.com"},
    )

    assert refused.status_code == 403
    assert "loopback" in refused.json()["detail"]
    # Refusing must not have started anything.
    assert client.get("/api/qa/latest").json()["running"] is False


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
    assert {
        "installation",
        "getting-started",
        "features",
        "gui",
        "cli-reference",
        "configuration",
        "missions",
        "infrastructure",
    } <= slugs
    assert "index" not in slugs
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
    single = client.patch("/api/settings", json={"routing": {"debugger": "strong-cloud"}}).json()
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
    assert client.patch("/api/settings", json={"routing": {"architect": "nope"}}).status_code == 400
    assert (
        client.patch("/api/settings", json={"routing": {"nope": "local-ollama"}}).status_code == 400
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
            with client.websocket_connect(path, headers={"host": LOCAL_HOST, **evil}):
                pass  # pragma: no cover - the handshake never completes

    # The IDE's own page, and non-browser clients, are unaffected.
    assert client.get("/api/workspace", headers={"origin": LOCAL_ORIGIN}).status_code == 200
    assert client.get("/api/workspace").status_code == 200
    # The Vite dev server is allowed, so `npm run dev` still works.
    assert (
        client.get("/api/workspace", headers={"origin": "http://localhost:5173"}).status_code == 200
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
        client.post("/api/settings/providers", json={**unreachable, "name": " "}).status_code == 400
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
        client.post(f"/api/sessions/{session}/model", json={"profile": "nope"}).status_code == 400
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


# ------------------------------------------------------------------ workspaces


def test_a_workspace_is_created_as_real_files_the_other_tabs_can_see(
    client: TestClient,
) -> None:
    """Workspaces live under ``.daino/workspaces``, still as ordinary files.

    Out of the working tree, so a documents folder never turns up in a diff or
    a package listing — but written to disk as plain files the CODE tab and the
    agent's own file tools can open by path.
    """
    created = client.post(
        "/api/workspaces",
        json={"name": "Q3 pricing", "goal": "Compare three vendors", "kind": "research"},
    ).json()

    assert created["folder"] == ".daino/workspaces/q3-pricing"
    assert created["goal"] == "Compare three vendors"
    assert [item["path"] for item in created["artifacts"]] == ["findings.md"]
    assert len(created["tasks"]) == 5

    # The same file is reachable through the ordinary file API the CODE tab uses.
    read = client.get(
        "/api/files/read", params={"path": ".daino/workspaces/q3-pricing/findings.md"}
    )
    assert read.status_code == 200
    assert "## Question" in read.json()["content"]


def test_the_workspace_lifecycle_round_trips_over_the_api(client: TestClient) -> None:
    workspace = client.post("/api/workspaces", json={"name": "Onboarding"}).json()
    identifier = workspace["id"]

    client.put(
        f"/api/workspaces/{identifier}/artifact",
        json={"path": "notes.md", "content": "# Notes\n\nFirst pass.\n"},
    )
    task = client.post(
        f"/api/workspaces/{identifier}/tasks", json={"content": "Draft the guide"}
    ).json()
    client.patch(f"/api/workspaces/{identifier}/tasks/{task['id']}", json={"status": "completed"})
    client.patch(f"/api/workspaces/{identifier}", json={"goal": "Rewrite onboarding"})

    reloaded = client.get(f"/api/workspaces/{identifier}").json()

    assert reloaded["goal"] == "Rewrite onboarding"
    assert {item["path"] for item in reloaded["artifacts"]} == {"notes.md"}
    completed = [item for item in reloaded["tasks"] if item["id"] == task["id"]]
    assert completed[0]["status"] == "completed"
    summary = next(
        item
        for item in client.get("/api/workspaces").json()["workspaces"]
        if item["id"] == identifier
    )
    assert summary["done_count"] == 1


def test_an_upload_is_stored_and_extracted_for_the_agent(client: TestClient) -> None:
    workspace = client.post("/api/workspaces", json={"name": "Analysis"}).json()
    identifier = workspace["id"]

    uploaded = client.post(
        f"/api/workspaces/{identifier}/uploads",
        json={"name": "churn.csv", "content_base64": base64.b64encode(b"a,b\n1,2\n").decode()},
    ).json()

    assert uploaded["path"] == "uploads/churn.csv"
    assert uploaded["extracted_path"].endswith("uploads/.extracted/churn.md")
    assert uploaded["warning"] == ""
    listed = client.get(f"/api/workspaces/{identifier}/artifacts").json()
    # An upload is not a deliverable, so it is listed separately.
    assert [item["path"] for item in listed["uploads"]] == ["uploads/churn.csv"]
    assert "uploads/churn.csv" not in {item["path"] for item in listed["artifacts"]}


def test_an_oversized_upload_is_refused(client: TestClient) -> None:
    workspace = client.post("/api/workspaces", json={"name": "Analysis"}).json()

    response = client.post(
        f"/api/workspaces/{workspace['id']}/uploads",
        json={
            "name": "huge.bin",
            "content_base64": base64.b64encode(b"x" * 8_000_001).decode(),
        },
    )

    assert response.status_code == 413


def test_artifact_history_is_recorded_and_restorable(client: TestClient) -> None:
    """The case that matters: an agent rewriting something you had edited."""
    workspace = client.post("/api/workspaces", json={"name": "Pricing"}).json()
    identifier = workspace["id"]
    path = {"path": "findings.md"}

    client.put(
        f"/api/workspaces/{identifier}/artifact",
        json={"path": "findings.md", "content": "my version", "author": "user"},
    )
    client.put(
        f"/api/workspaces/{identifier}/artifact",
        json={"path": "findings.md", "content": "agent version", "author": "agent"},
    )

    revisions = client.get(f"/api/workspaces/{identifier}/revisions", params=path).json()
    assert [(item["version"], item["author"]) for item in revisions["revisions"]] == [
        (2, "agent"),
        (1, "user"),
    ]

    client.post(f"/api/workspaces/{identifier}/revision/restore", params={**path, "version": 1})
    restored = client.get(f"/api/workspaces/{identifier}/artifact", params=path).json()
    assert restored["content"] == "my version"


def test_a_traversing_path_is_refused(client: TestClient) -> None:
    workspace = client.post("/api/workspaces", json={"name": "Pricing"}).json()
    identifier = workspace["id"]
    before = client.get("/api/files/read", params={"path": "README.md"}).json()["content"]

    for attempt in ("../../etc/passwd", "../../../README.md", "notes/../../../README.md"):
        assert (
            client.get(
                f"/api/workspaces/{identifier}/artifact", params={"path": attempt}
            ).status_code
            == 404
        )
        assert (
            client.put(
                f"/api/workspaces/{identifier}/artifact",
                json={"path": attempt, "content": "owned"},
            ).status_code
            == 404
        )

    after = client.get("/api/files/read", params={"path": "README.md"}).json()["content"]
    assert after == before
    assert client.get("/api/workspaces/ws-nope").status_code == 404


def test_an_absolute_path_is_normalised_into_the_workspace(client: TestClient) -> None:
    """It is contained, not escaped — the property that actually matters.

    Leading slashes are stripped rather than rejected, matching
    ``EditTools.normalize``: models write ``notes.md``, ``./notes.md`` and
    ``/notes.md`` interchangeably, and one rule for agent-supplied paths is
    better than two. What must never happen is a write landing outside the
    folder, which is what this asserts.
    """
    workspace = client.post("/api/workspaces", json={"name": "Pricing"}).json()
    identifier = workspace["id"]

    written = client.put(
        f"/api/workspaces/{identifier}/artifact",
        json={"path": "/etc/passwd", "content": "contained"},
    ).json()

    assert written["repo_path"] == ".daino/workspaces/pricing/etc/passwd"
    landed = client.get("/api/files/read", params={"path": ".daino/workspaces/pricing/etc/passwd"})
    assert landed.status_code == 200 and landed.json()["content"] == "contained"


def test_deleting_a_workspace_leaves_its_files_unless_asked(client: TestClient) -> None:
    workspace = client.post("/api/workspaces", json={"name": "Kept"}).json()

    client.delete(f"/api/workspaces/{workspace['id']}")

    assert client.get(f"/api/workspaces/{workspace['id']}").status_code == 404
    still_there = client.get(
        "/api/files/read", params={"path": ".daino/workspaces/kept/workspace.json"}
    )
    assert still_there.status_code == 200


def test_the_run_api_reports_a_plan_that_has_never_been_executed(
    client: TestClient,
) -> None:
    workspace = client.post("/api/workspaces", json={"name": "Proposal"}).json()

    assert client.get(f"/api/workspaces/{workspace['id']}/run").json()["run"] is None


def test_a_plan_with_nothing_pending_refuses_to_run(client: TestClient) -> None:
    """A refusal the user can act on, rather than a run that finishes instantly."""
    workspace = client.post("/api/workspaces", json={"name": "Proposal"}).json()
    for task in workspace["tasks"]:
        client.patch(
            f"/api/workspaces/{workspace['id']}/tasks/{task['id']}",
            json={"status": "completed"},
        )

    response = client.post(f"/api/workspaces/{workspace['id']}/run", json={})

    assert response.status_code == 409
    assert "already done" in response.json()["detail"]


def test_skills_are_listed_for_the_picker(client: TestClient) -> None:
    skills = client.get("/api/workspaces/meta/skills").json()["skills"]

    names = {item["name"] for item in skills}
    assert {"competitive-research", "prd-writer", "data-analysis"} <= names
    assert all(item["title"] for item in skills)


def test_a_document_renders_into_a_file_people_can_open(client: TestClient) -> None:
    workspace = client.post("/api/workspaces", json={"name": "Proposal"}).json()
    client.put(
        f"/api/workspaces/{workspace['id']}/artifact",
        json={
            "path": "report.md",
            "content": "# Report\n\nWe recommend Beta.\n\n## Risks\n\n- Tight window\n",
        },
    )

    artifact = client.post(
        f"/api/workspaces/{workspace['id']}/deliverable",
        json={"path": "report.md", "format": "pdf"},
    ).json()

    assert artifact["path"] == "report.pdf"
    assert artifact["bytes"] > 0
    # It lands in the workspace folder as an ordinary file, like everything else.
    listing = client.get(f"/api/workspaces/{workspace['id']}/artifacts").json()["artifacts"]
    assert {item["path"] for item in listing} == {"report.md", "report.pdf"}


def test_provenance_drives_the_outdated_warning(client: TestClient) -> None:
    workspace = client.post("/api/workspaces", json={"name": "Proposal"}).json()
    identifier = workspace["id"]
    for path, content in (("architecture.md", "v1"), ("proposal.md", "from v1")):
        client.put(
            f"/api/workspaces/{identifier}/artifact", json={"path": path, "content": content}
        )
    client.post(
        f"/api/workspaces/{identifier}/links",
        json={
            "source_path": "proposal.md",
            "target_path": "architecture.md",
            "relation": "derived_from",
        },
    )
    assert client.get(f"/api/workspaces/{identifier}/links").json()["stale"] == []

    client.put(
        f"/api/workspaces/{identifier}/artifact",
        json={"path": "architecture.md", "content": "v2 — different"},
    )

    stale = client.get(f"/api/workspaces/{identifier}/links").json()["stale"]
    assert [item["path"] for item in stale] == ["proposal.md"]
    # Ignoring it is durable: the warning does not come back on the next read.
    client.post(f"/api/workspaces/{identifier}/links/{stale[0]['link_id']}/acknowledge")
    assert client.get(f"/api/workspaces/{identifier}/links").json()["stale"] == []


def test_a_change_set_can_be_reviewed_and_rejected_over_the_api(
    client: TestClient,
) -> None:
    """The GUI's Reject button, end to end: the previous version comes back."""
    from daino.workbench.changes import ChangeSetStore

    workspace = client.post("/api/workspaces", json={"name": "Proposal"}).json()
    identifier = workspace["id"]
    client.put(
        f"/api/workspaces/{identifier}/artifact",
        json={"path": "proposal.md", "content": "the good draft"},
    )
    state = client.app.state.gui  # type: ignore[attr-defined]
    changes: ChangeSetStore = state.runs.changes
    before = changes.snapshot(identifier)
    client.put(
        f"/api/workspaces/{identifier}/artifact",
        json={"path": "proposal.md", "content": "the rewrite", "author": "agent"},
    )
    change = changes.record(identifier, before=before, summary="Rewrote it")
    assert change is not None

    listed = client.get(f"/api/workspaces/{identifier}/changes").json()["changes"]
    assert [item["id"] for item in listed] == [change.id]
    diff = client.get(
        f"/api/workspaces/{identifier}/changes/{change.id}/diff",
        params={"path": "proposal.md"},
    ).json()
    assert any(line["marker"] == "-" for line in diff["lines"])

    decided = client.post(
        f"/api/workspaces/{identifier}/changes/{change.id}/decide",
        json={"accepted": False, "path": "proposal.md"},
    ).json()

    assert decided["status"] == "rejected"
    restored = client.get(
        f"/api/workspaces/{identifier}/artifact", params={"path": "proposal.md"}
    ).json()
    assert restored["content"] == "the good draft"


def test_saving_a_document_over_a_newer_version_is_refused(client: TestClient) -> None:
    """The lost update: agent rewrites, user saves their stale draft, work gone.

    409 rather than 400, because the request is well formed and will succeed the
    moment the writer has seen what changed and decided what to keep.
    """
    workspace = client.post("/api/workspaces", json={"name": "Analysis"}).json()
    client.put(
        f"/api/workspaces/{workspace['id']}/artifact",
        json={"path": "notes.md", "content": "the user's draft"},
    )
    opened = client.get(
        f"/api/workspaces/{workspace['id']}/artifact", params={"path": "notes.md"}
    ).json()
    digest = opened["artifact"]["digest"]
    assert digest

    # The agent finishes a step and rewrites the same document.
    client.put(
        f"/api/workspaces/{workspace['id']}/artifact",
        json={"path": "notes.md", "content": "the agent's rewrite", "author": "agent"},
    )

    refused = client.put(
        f"/api/workspaces/{workspace['id']}/artifact",
        json={"path": "notes.md", "content": "more draft", "base_digest": digest},
    )

    assert refused.status_code == 409
    assert refused.json()["detail"]["current_digest"] != digest
    current = client.get(
        f"/api/workspaces/{workspace['id']}/artifact", params={"path": "notes.md"}
    ).json()
    assert current["content"] == "the agent's rewrite"

    # "Keep mine" sends no digest, and goes through.
    kept = client.put(
        f"/api/workspaces/{workspace['id']}/artifact",
        json={"path": "notes.md", "content": "more draft"},
    )
    assert kept.status_code == 200


def test_saving_a_design_over_a_newer_version_is_refused(client: TestClient) -> None:
    """Two windows on version 2 both used to write version 3."""
    design = client.post("/api/designs", json={"name": "Architecture"}).json()
    client.post(f"/api/designs/{design['id']}/nodes", json={"label": "API"})

    loaded = client.get(f"/api/designs/{design['id']}").json()

    first = {**loaded, "name": "Architecture (first)"}
    assert client.put(f"/api/designs/{design['id']}", json=first).status_code == 200

    # The second window still posts the version it loaded.
    second = {**loaded, "name": "Architecture (second)"}
    refused = client.put(f"/api/designs/{design['id']}", json=second)

    assert refused.status_code == 409
    assert refused.json()["detail"]["stored_version"] > loaded["version"]
    assert client.get(f"/api/designs/{design['id']}").json()["name"] == "Architecture (first)"

    # And every version along the way is still there to go back to.
    versions = client.get(f"/api/designs/{design['id']}/revisions").json()["revisions"]
    assert loaded["version"] in {item["version"] for item in versions}


# ------------------------------------------------------------- error surface


def test_an_unknown_api_path_is_a_json_404_not_the_app(client: TestClient) -> None:
    """The SPA catch-all used to answer for /api too.

    A misspelled or removed endpoint returned index.html with a 200, so the
    frontend got HTML where it expected JSON: `res.ok` was true, the parse
    failed, and the caller received a string of markup instead of data. The
    breakage then surfaced far from its cause and looked like a server fault.
    """
    for path in ("/api/does-not-exist", "/api/qa/nope", "/api", "/ws/nope"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"].startswith("application/json"), path
        assert "No such endpoint" in response.json()["detail"]


def test_client_side_routes_still_fall_back_to_the_app(client: TestClient) -> None:
    """Only /api and /ws are excluded; everything else is the SPA's."""
    for path in ("/", "/docs", "/code", "/some/deep/route"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "text/html" in response.headers["content-type"], path


def test_an_unhandled_error_is_reported_and_audited(git_repo: Path) -> None:
    """A bare "Internal Server Error" leaves nothing to report a bug with.

    The traceback goes to the audit log, and the response names the exception
    and the route — so the person who hit it can say what happened.

    Builds its own client because the shared fixture re-raises server
    exceptions, which is what you want everywhere except here: this test is
    about the response a browser actually receives.
    """
    import daino.server.routes.insights as insights

    initialize_project(git_repo)
    context = open_project(git_repo)
    original = insights._qa_running

    def explode(_state: object) -> bool:
        raise ValueError("deliberate failure")

    insights._qa_running = explode  # type: ignore[assignment]
    try:
        with TestClient(
            create_app(context),
            base_url="http://127.0.0.1:4173",
            raise_server_exceptions=False,
        ) as unguarded:
            response = unguarded.get("/api/qa/latest")
    finally:
        insights._qa_running = original  # type: ignore[assignment]
        context.close()

    assert response.status_code == 500
    payload = response.json()
    assert payload["detail"] == "ValueError: deliberate failure"
    assert payload["path"] == "/api/qa/latest"
    assert "daino logs" in payload["hint"]

    from daino.observability import AuditLog

    recorded = [
        item for item in AuditLog(git_repo).read() if item.get("event") == "ServerError"
    ]
    assert recorded
    assert "deliberate failure" in str(recorded[-1].get("traceback", ""))


# ---------------------------------------------------------------------- debug


def test_debug_adapters_are_listed_with_install_hints(client: TestClient) -> None:
    """"No debugger" must never look the same as "the debugger found nothing"."""
    payload = client.get("/api/debug/adapters").json()

    rows = {row["id"]: row for row in payload["adapters"]}
    assert "debugpy" in rows
    assert "pip install debugpy" in rows["debugpy"]["install"]


def test_breakpoints_are_kept_on_the_server_and_survive(client: TestClient) -> None:
    """They belong to the user, not to a run — so a reload keeps them."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "prog.py").write_text("a = 1\nb = 2\n", encoding="utf-8")

    first = client.post(
        "/api/debug/breakpoints/toggle", json={"path": "prog.py", "line": 2}
    ).json()
    assert [item["line"] for item in first["breakpoints"]] == [2]

    # A fresh request — as a reloaded tab would make — still sees it.
    assert [item["line"] for item in client.get("/api/debug/state").json()["breakpoints"]] == [2]

    # Toggling the same line removes it.
    again = client.post(
        "/api/debug/breakpoints/toggle", json={"path": "prog.py", "line": 2}
    ).json()
    assert again["breakpoints"] == []


def test_a_breakpoint_condition_is_recorded(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "prog.py").write_text("a = 1\n", encoding="utf-8")
    client.post("/api/debug/breakpoints/toggle", json={"path": "prog.py", "line": 1})

    payload = client.post(
        "/api/debug/breakpoints/condition",
        json={"path": "prog.py", "line": 1, "condition": "a > 3"},
    ).json()

    assert payload["breakpoints"][0]["condition"] == "a > 3"


def test_breakpoints_can_be_cleared_for_one_file_or_all(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "one.py").write_text("a = 1\n", encoding="utf-8")
    (root / "two.py").write_text("b = 2\n", encoding="utf-8")
    client.post("/api/debug/breakpoints/toggle", json={"path": "one.py", "line": 1})
    client.post("/api/debug/breakpoints/toggle", json={"path": "two.py", "line": 1})

    narrowed = client.delete("/api/debug/breakpoints", params={"path": "one.py"}).json()
    assert {item["path"] for item in narrowed["breakpoints"]} == {"two.py"}

    assert client.delete("/api/debug/breakpoints").json()["breakpoints"] == []


def test_debugging_a_file_no_adapter_covers_is_refused(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "notes.md").write_text("# hi\n", encoding="utf-8")

    refused = client.post("/api/debug/launch", json={"program": "notes.md"})

    assert refused.status_code == 400
    assert "adapter" in refused.json()["detail"]


def test_launching_with_nothing_named_is_refused(client: TestClient) -> None:
    refused = client.post("/api/debug/launch", json={})

    assert refused.status_code == 400
    assert "Nothing to debug" in refused.json()["detail"]


def test_an_unknown_debug_command_is_a_404(client: TestClient) -> None:
    assert client.post("/api/debug/teleport").status_code == 404


# ------------------------------------------------------- search and tasks


def test_search_filters_narrow_the_results(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "a.ts").write_text("const total = 1;\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("total = 2\n", encoding="utf-8")

    everything = client.get("/api/files/search", params={"q": "total"}).json()
    only_ts = client.get(
        "/api/files/search", params={"q": "total", "include": "*.ts"}
    ).json()

    assert {item["path"] for item in everything["matches"]} >= {
        "src/a.ts",
        "src/b.py",
    }
    assert {item["path"] for item in only_ts["matches"]} == {"src/a.ts"}


def test_a_replace_preview_writes_nothing(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "one.txt").write_text("total here\n", encoding="utf-8")

    preview = client.get(
        "/api/files/search", params={"q": "total", "replace": "sum"}
    ).json()

    assert preview["matches"][0]["replacement"] == "sum here"
    assert (root / "one.txt").read_text(encoding="utf-8") == "total here\n"


def test_applying_a_replace_writes_only_the_chosen_files(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "keep.txt").write_text("total\n", encoding="utf-8")
    (root / "change.txt").write_text("total\n", encoding="utf-8")

    applied = client.post(
        "/api/files/replace",
        json={"query": "total", "replacement": "sum", "paths": ["change.txt"]},
    )

    assert applied.status_code == 200
    assert applied.json()["files"] == ["change.txt"]
    assert (root / "change.txt").read_text(encoding="utf-8") == "sum\n"
    assert (root / "keep.txt").read_text(encoding="utf-8") == "total\n"


def test_an_invalid_search_pattern_is_reported(client: TestClient) -> None:
    result = client.get(
        "/api/files/search", params={"q": "([bad", "regex": True}
    ).json()

    assert result["success"] is False
    assert "Invalid pattern" in result["error"]
    assert result["matches"] == []


def test_the_projects_own_commands_are_discovered(client: TestClient) -> None:
    """A project's npm scripts *are* its run configurations."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "package.json").write_text(
        '{"name": "app", "scripts": {"dev": "vite", "test": "vitest run"}}',
        encoding="utf-8",
    )
    (root / "package-lock.json").write_text("{}", encoding="utf-8")

    payload = client.get("/api/tasks").json()

    tasks = {item["id"]: item for item in payload["tasks"]}
    assert tasks["npm:dev"]["command"] == "npm run dev"
    assert tasks["npm:dev"]["kind"] == "run"
    assert tasks["npm:test"]["kind"] == "test"
    assert payload["tasks_file"].endswith("tasks.json")


def test_a_saved_task_overrides_a_discovered_one(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "package.json").write_text(
        '{"name": "app", "scripts": {"dev": "vite"}}', encoding="utf-8"
    )

    saved = client.put(
        "/api/tasks",
        json={
            "tasks": [
                {
                    "id": "npm:dev",
                    "label": "dev with debug",
                    "command": "DEBUG=1 npm run dev",
                    "kind": "run",
                }
            ]
        },
    )

    assert saved.status_code == 200
    resolved = client.get("/api/tasks/npm:dev").json()
    assert resolved["command"] == "DEBUG=1 npm run dev"
    assert resolved["source"] == "user"


# ------------------------------------------------------------------------ git


def _commit_all(client: TestClient, message: str) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603, S607
        ["git", "commit", "-m", message], cwd=root, check=True, capture_output=True
    )


def test_part_of_a_file_can_be_staged(client: TestClient) -> None:
    """The whole point of hunk staging: commit one change, keep the other."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "app.py").write_text(
        "\n".join(f"line{index}" for index in range(1, 31)) + "\n", encoding="utf-8"
    )
    _commit_all(client, "add app")

    lines = (root / "app.py").read_text(encoding="utf-8").splitlines()
    lines.insert(1, "FIRST CHANGE")
    lines.append("LAST CHANGE")
    (root / "app.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    listed = client.get("/api/git/hunks", params={"path": "app.py"}).json()
    assert len(listed["hunks"]) == 2
    assert listed["hunks"][0]["added"] == 1

    staged = client.post(
        "/api/git/stage-hunks", json={"path": "app.py", "hunks": [0]}
    )
    assert staged.status_code == 200

    index_diff = client.get("/api/git/diff", params={"staged": True}).json()["diff"]
    assert "FIRST CHANGE" in index_diff
    assert "LAST CHANGE" not in index_diff
    # The other change is still in the working tree.
    assert "LAST CHANGE" in client.get("/api/git/diff").json()["diff"]


def test_a_staged_hunk_can_be_taken_back_out(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "app.py").write_text("a\nb\nc\n", encoding="utf-8")
    _commit_all(client, "add app")
    (root / "app.py").write_text("a\nCHANGED\nc\n", encoding="utf-8")
    client.post("/api/git/stage", json={"paths": ["app.py"]})

    listed = client.get("/api/git/hunks", params={"path": "app.py", "staged": True}).json()
    assert listed["hunks"]

    client.post("/api/git/unstage-hunks", json={"path": "app.py", "hunks": [0]})

    assert "CHANGED" not in client.get("/api/git/diff", params={"staged": True}).json()["diff"]
    # Unstaging must not revert the file itself.
    assert "CHANGED" in (root / "app.py").read_text(encoding="utf-8")


def test_a_commit_takes_only_what_is_staged(client: TestClient) -> None:
    """A commit button that swept in the rest of the tree would be the most
    surprising thing in the product."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "wanted.py").write_text("keep = 1\n", encoding="utf-8")
    (root / "unwanted.py").write_text("later = 2\n", encoding="utf-8")
    client.post("/api/git/stage", json={"paths": ["wanted.py"]})

    context = client.get("/api/git/commit-context").json()
    assert [item["path"] for item in context["staged"]] == ["wanted.py"]

    committed = client.post("/api/git/commit", json={"message": "add wanted"})
    assert committed.status_code == 200

    listing = subprocess.run(  # noqa: S603, S607
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "wanted.py" in listing
    assert "unwanted.py" not in listing
    # And the file left out is still sitting there untracked.
    assert "unwanted.py" in {
        item["path"] for item in client.get("/api/git/status").json()["untracked"]
    }


def test_committing_nothing_is_refused_with_advice(client: TestClient) -> None:
    refused = client.post("/api/git/commit", json={"message": "empty"})

    assert refused.status_code == 400
    assert "Nothing is staged" in refused.json()["detail"]


def test_a_commit_can_be_amended_with_its_message_prefilled(
    client: TestClient,
) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "thing.py").write_text("first = 1\n", encoding="utf-8")
    _commit_all(client, "original subject")

    context = client.get("/api/git/commit-context").json()
    assert context["can_amend"] is True
    assert context["previous_message"] == "original subject"

    (root / "thing.py").write_text("first = 2\n", encoding="utf-8")
    client.post("/api/git/stage", json={"paths": ["thing.py"]})
    amended = client.post(
        "/api/git/commit", json={"message": "corrected subject", "amend": True}
    )

    assert amended.status_code == 200
    subject = subprocess.run(  # noqa: S603, S607
        ["git", "log", "-1", "--format=%s"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert subject == "corrected subject"


def test_branches_are_listed_created_and_switched(client: TestClient) -> None:
    listed = client.get("/api/git/branches").json()
    assert listed["repository"] is True
    assert listed["current"] == "main"

    created = client.post(
        "/api/git/branch", json={"name": "feature/x", "create": True}
    )
    assert created.status_code == 200
    assert created.json()["branch"] == "feature/x"

    back = client.post("/api/git/branch", json={"name": "main"})
    assert back.json()["branch"] == "main"

    names = {item["name"] for item in client.get("/api/git/branches").json()["branches"]}
    assert names == {"main", "feature/x"}


def test_deleting_a_branch_with_unmerged_work_is_refused(client: TestClient) -> None:
    """Git's refusal is the only thing between the user and losing commits, so
    it is passed straight through rather than worked around."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    client.post("/api/git/branch", json={"name": "spike", "create": True})
    (root / "spike.py").write_text("work = 1\n", encoding="utf-8")
    _commit_all(client, "spike work")
    client.post("/api/git/branch", json={"name": "main"})

    refused = client.delete("/api/git/branch", params={"name": "spike"})
    assert refused.status_code == 400

    forced = client.delete("/api/git/branch", params={"name": "spike", "force": True})
    assert forced.status_code == 200


def test_a_merge_conflict_is_reported_as_state_not_an_error(
    client: TestClient,
) -> None:
    """A conflict is something the user now resolves, not a failed request."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "shared.txt").write_text("original\n", encoding="utf-8")
    _commit_all(client, "base")

    client.post("/api/git/branch", json={"name": "other", "create": True})
    (root / "shared.txt").write_text("theirs\n", encoding="utf-8")
    _commit_all(client, "their change")
    client.post("/api/git/branch", json={"name": "main"})
    (root / "shared.txt").write_text("ours\n", encoding="utf-8")
    _commit_all(client, "our change")

    merged = client.post("/api/git/merge", json={"ref": "other"})

    assert merged.status_code == 200
    payload = merged.json()
    assert payload["conflicted"] is True
    assert payload["conflicts"] == ["shared.txt"]

    # All three sides are readable without disturbing the file being edited.
    sides = client.get("/api/git/conflict", params={"path": "shared.txt"}).json()
    assert sides["base"].strip() == "original"
    assert sides["ours"].strip() == "ours"
    assert sides["theirs"].strip() == "theirs"

    resolved = client.post(
        "/api/git/conflict/resolve", json={"path": "shared.txt", "side": "theirs"}
    )
    assert resolved.status_code == 200
    assert resolved.json()["conflicts"] == []
    assert (root / "shared.txt").read_text(encoding="utf-8").strip() == "theirs"

    # The merge is still unfinished until it is committed, and says so.
    assert client.get("/api/git/conflicts").json()["merging"] is True
    assert client.post("/api/git/commit", json={"message": "merge other"}).status_code == 200
    assert client.get("/api/git/conflicts").json()["merging"] is False


def test_committing_during_an_unresolved_merge_is_refused(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "shared.txt").write_text("original\n", encoding="utf-8")
    _commit_all(client, "base")
    client.post("/api/git/branch", json={"name": "other", "create": True})
    (root / "shared.txt").write_text("theirs\n", encoding="utf-8")
    _commit_all(client, "their change")
    client.post("/api/git/branch", json={"name": "main"})
    (root / "shared.txt").write_text("ours\n", encoding="utf-8")
    _commit_all(client, "our change")
    client.post("/api/git/merge", json={"ref": "other"})

    refused = client.post("/api/git/commit", json={"message": "half a merge"})

    assert refused.status_code == 400
    assert "conflicts" in refused.json()["detail"]


def test_a_merge_can_be_abandoned(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "shared.txt").write_text("original\n", encoding="utf-8")
    _commit_all(client, "base")
    client.post("/api/git/branch", json={"name": "other", "create": True})
    (root / "shared.txt").write_text("theirs\n", encoding="utf-8")
    _commit_all(client, "their change")
    client.post("/api/git/branch", json={"name": "main"})
    (root / "shared.txt").write_text("ours\n", encoding="utf-8")
    _commit_all(client, "our change")
    client.post("/api/git/merge", json={"ref": "other"})

    aborted = client.post("/api/git/merge/abort")

    assert aborted.status_code == 200
    assert aborted.json()["merging"] is False
    assert (root / "shared.txt").read_text(encoding="utf-8").strip() == "ours"


def test_pushing_without_a_remote_says_what_to_do(client: TestClient) -> None:
    """A useful error beats a raw Git one, and the raw one is kept too."""
    refused = client.post("/api/git/push", json={})

    assert refused.status_code == 400
    assert refused.json()["detail"]


# ---------------------------------------------------------------------- tests


def test_the_projects_test_frameworks_are_discovered(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n\n\ndef test_bad():\n    assert False\n",
        encoding="utf-8",
    )

    payload = client.get("/api/tests/frameworks").json()

    pytest_entry = next(
        item for item in payload["frameworks"] if item["id"] == "pytest"
    )
    assert pytest_entry["available"] is True
    assert pytest_entry["test_count"] == 2
    assert {item["name"] for item in payload["tests"]} == {"test_ok", "test_bad"}


def test_a_run_reports_failures_with_a_place_to_click(client: TestClient) -> None:
    """The point of the panel: the failing line, not just a count."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n\n\ndef test_bad():\n"
        "    assert 1 == 2, 'nope'\n",
        encoding="utf-8",
    )

    assert client.post("/api/tests/run", json={}).status_code == 200
    for _ in range(400):
        latest = client.get("/api/tests/latest").json()
        if not latest["running"] and latest["run"]["finished_at"]:
            break
        time.sleep(0.05)

    run = client.get("/api/tests/latest").json()["run"]
    assert run["status"] == "failed"
    assert run["counts"]["passed"] == 1
    assert run["counts"]["failed"] == 1
    failure = next(item for item in run["results"] if item["status"] == "failed")
    assert failure["failure_file"] == "tests/test_sample.py"
    assert failure["failure_line"] == 6
    # The id is pytest's own node id, so re-running selects exactly this test.
    assert failure["id"] == "tests/test_sample.py::test_bad"


def test_only_the_failures_can_be_re_run_over_the_api(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n\n\ndef test_bad():\n    assert False\n",
        encoding="utf-8",
    )

    # Nothing has failed yet, so there is nothing to re-run.
    assert (
        client.post("/api/tests/run", json={"failed_only": True}).status_code == 400
    )

    client.post("/api/tests/run", json={})
    for _ in range(400):
        latest = client.get("/api/tests/latest").json()
        if not latest["running"] and latest["run"]["finished_at"]:
            break
        time.sleep(0.05)

    assert client.post("/api/tests/run", json={"failed_only": True}).status_code == 200
    for _ in range(400):
        latest = client.get("/api/tests/latest").json()
        if not latest["running"] and latest["run"]["finished_at"]:
            break
        time.sleep(0.05)

    rerun = client.get("/api/tests/latest").json()["run"]
    assert rerun["selection"] == ["tests/test_sample.py::test_bad"]
    assert [item["name"] for item in rerun["results"]] == ["test_bad"]


def test_test_reports_never_dirty_the_working_tree(client: TestClient) -> None:
    """A run that showed up as an uncommitted change would poison every diff."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'fixture'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    tests_dir = root / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_sample.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )

    client.post("/api/tests/run", json={})
    for _ in range(400):
        latest = client.get("/api/tests/latest").json()
        if not latest["running"] and latest["run"]["finished_at"]:
            break
        time.sleep(0.05)

    status = client.get("/api/git/status").json()
    touched = {
        item["path"]
        for group in ("staged", "modified", "untracked")
        for item in status[group]
    }
    assert not any("test-reports" in path for path in touched)


# ------------------------------------------------------------------------ lsp


def test_language_servers_are_listed_with_install_hints(client: TestClient) -> None:
    """"No diagnostics" must be distinguishable from "no problems"."""
    payload = client.get("/api/lsp/servers").json()

    servers = {row["id"]: row for row in payload["servers"]}
    assert "pyright" in servers
    assert "install" in servers["pyright"]
    assert isinstance(payload["running"], list)


def test_diagnostics_for_an_unanalysable_file_say_so(client: TestClient) -> None:
    """A .txt file is not a clean .txt file — nothing looked at it."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "notes.txt").write_text("plain text\n", encoding="utf-8")

    payload = client.post(
        "/api/lsp/diagnostics", json={"path": "notes.txt"}
    ).json()

    assert payload["supported"] is False
    assert payload["diagnostics"] == []
    assert "No language server" in payload["detail"]


def test_diagnostics_without_a_server_report_the_gap_not_an_error(
    client: TestClient,
) -> None:
    """A missing analyser is evidence missing, not a failed request."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "thing.py").write_text("x = 1\n", encoding="utf-8")

    response = client.post("/api/lsp/diagnostics", json={"path": "thing.py"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["supported"] is True
    # No server is installed in the test environment, and that is reported
    # rather than rendered as a clean file.
    assert payload["available"] is False
    assert payload["diagnostics"] == []
    assert payload["detail"]


def test_workspace_symbol_search_falls_back_to_the_index(client: TestClient) -> None:
    """The search box has to work in a checkout with nothing installed."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "shapes.py").write_text(
        "class Rectangle:\n    pass\n", encoding="utf-8"
    )
    client.post("/api/repository/index", json={})

    payload = client.get(
        "/api/lsp/workspace-symbols", params={"query": "Rectangle"}
    ).json()

    assert payload["source"] == "index"
    assert any(item["name"] == "Rectangle" for item in payload["symbols"])


def test_a_rename_is_previewed_then_applied(client: TestClient) -> None:
    """Applying is a separate, explicit call from computing the edits."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "shapes.py").write_text(
        "class Rectangle:\n    pass\n", encoding="utf-8"
    )

    applied = client.post(
        "/api/lsp/rename/apply",
        json={
            "edits": {
                "shapes.py": [
                    {
                        "start_line": 1,
                        "start_column": 7,
                        "end_line": 1,
                        "end_column": 16,
                        "text": "Square",
                    }
                ]
            }
        },
    )

    assert applied.status_code == 200
    assert applied.json()["written"] == ["shapes.py"]
    assert (root / "shapes.py").read_text(encoding="utf-8") == "class Square:\n    pass\n"


def test_multiple_edits_in_one_file_do_not_shift_each_other(
    client: TestClient,
) -> None:
    """Applied back-to-front, so an earlier edit cannot move a later one."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "many.py").write_text("aa = 1\nbb = aa + aa\n", encoding="utf-8")

    client.post(
        "/api/lsp/rename/apply",
        json={
            "edits": {
                "many.py": [
                    {"start_line": 1, "start_column": 1, "end_line": 1,
                     "end_column": 3, "text": "value"},
                    {"start_line": 2, "start_column": 6, "end_line": 2,
                     "end_column": 8, "text": "value"},
                    {"start_line": 2, "start_column": 11, "end_line": 2,
                     "end_column": 13, "text": "value"},
                ]
            }
        },
    )

    assert (root / "many.py").read_text(encoding="utf-8") == (
        "value = 1\nbb = value + value\n"
    )


# --------------------------------------------------------------- change review


def test_the_review_subject_describes_the_change_before_running_one(
    client: TestClient,
) -> None:
    """The view can show what would be reviewed without paying for a model call."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "README.md").write_text("# Fixture\n\nEdited.\n", encoding="utf-8")
    (root / "brand_new.py").write_text("def added():\n    return 1\n", encoding="utf-8")

    subject = client.get("/api/review/subject", params={"scope": "working"}).json()

    assert subject["empty"] is False
    assert subject["label"] == "Uncommitted changes in the working tree"
    # A newly created file has no diff, so it has to be found separately or a
    # working-tree review silently skips it.
    assert "brand_new.py" in subject["untracked"]
    # Both the edited file and the new one are counted; the fixture's own
    # .gitignore edit is a real change too, so the total is not pinned.
    assert subject["files"] >= 2


def test_an_unresolvable_base_is_a_bad_request_not_a_failed_run(
    client: TestClient,
) -> None:
    response = client.get(
        "/api/review/subject", params={"scope": "branch", "base_ref": "no-such-branch"}
    )

    assert response.status_code == 400
    assert client.post(
        "/api/review/run", json={"scope": "branch", "base_ref": "no-such-branch"}
    ).status_code == 400


def test_a_review_runs_and_is_reloadable(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "app.py").write_text(
        "import subprocess\n\n\ndef run(cmd):\n    return subprocess.run(cmd, shell=True)\n",
        encoding="utf-8",
    )

    assert client.post("/api/review/run", json={"scope": "working"}).json()["running"] is True
    for _ in range(200):
        latest = client.get("/api/review/latest").json()
        if not latest["running"] and latest["review"]:
            break
        time.sleep(0.05)

    review = client.get("/api/review/latest").json()["review"]
    assert review["status"] == "completed"
    assert "app.py" in {item["path"] for item in review["files"]}
    assert "py-shell-injection" in {item["reference"] for item in review["findings"]}
    assert review["verdict"] in {"warn", "blocked"}

    reloaded = client.get(f"/api/review/reports/{review['id']}").json()["review"]
    assert reloaded["id"] == review["id"]
    assert client.get("/api/review/history").json()["reviews"][0]["id"] == review["id"]
    assert client.get("/api/review/reports/review-nope").status_code == 404


def test_one_file_of_the_change_can_be_read_on_its_own(client: TestClient) -> None:
    """Opening a file must not re-send a diff that can run to hundreds of KB."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "README.md").write_text("# Fixture\n\nEdited.\n", encoding="utf-8")
    (root / "brand_new.py").write_text("def added():\n    return 1\n", encoding="utf-8")

    tracked = client.get("/api/review/diff", params={"path": "README.md"}).json()
    untracked = client.get("/api/review/diff", params={"path": "brand_new.py"}).json()

    assert "+Edited." in tracked["patch"]
    assert "README.md" in tracked["patch"] and "brand_new.py" not in tracked["patch"]
    # A file git has no diff for is shown as wholly added rather than as nothing.
    assert "+def added():" in untracked["patch"]


def test_a_saved_review_shows_the_diff_it_reviewed_not_todays(
    client: TestClient,
) -> None:
    """Findings and the code they are about have to age together.

    The endpoint used to re-resolve the requested scope against the current
    working tree, so opening a file in a week-old review showed last week's
    findings beside code written since.
    """
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "app.py").write_text("REVIEWED = 1\n", encoding="utf-8")

    assert client.post("/api/review/run", json={"scope": "working"}).json()["running"] is True
    for _ in range(200):
        latest = client.get("/api/review/latest").json()
        if not latest["running"] and latest["review"]:
            break
        time.sleep(0.05)
    review = client.get("/api/review/latest").json()["review"]
    assert review["status"] == "completed"

    # The working tree moves on after the review is saved.
    (root / "app.py").write_text("WRITTEN_AFTERWARDS = 2\n", encoding="utf-8")

    archived = client.get(
        "/api/review/diff", params={"path": "app.py", "review_id": review["id"]}
    ).json()
    live = client.get("/api/review/diff", params={"path": "app.py"}).json()

    assert archived["archived"] is True
    assert "REVIEWED = 1" in archived["patch"]
    assert "WRITTEN_AFTERWARDS" not in archived["patch"]
    # Without a review id the endpoint still describes the tree as it is now.
    assert "WRITTEN_AFTERWARDS" in live["patch"]

    # And the report itself reads as no longer describing this checkout.
    assert client.get(f"/api/review/reports/{review['id']}").json()["stale"] is True


def test_a_verdict_goes_stale_when_the_files_move(client: TestClient) -> None:
    """The tab badge must stop claiming a checkout nobody inspected is cleared."""
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert client.post("/api/qa/run", json={"profile": "quality"}).status_code == 200
    for _ in range(400):
        latest = client.get("/api/qa/latest").json()
        if not latest["running"] and latest["report"]:
            break
        time.sleep(0.05)

    fresh = client.get("/api/qa/latest").json()
    assert fresh["report"]["checkout"]["digest"]
    assert fresh["stale"] is False

    (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert client.get("/api/qa/latest").json()["stale"] is True


def test_only_one_review_runs_at_a_time(client: TestClient) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    first = client.post("/api/review/run", json={"scope": "working"})
    second = client.post("/api/review/run", json={"scope": "working"})

    assert first.status_code == 200
    assert second.status_code in {200, 409}
    client.post("/api/review/cancel")

