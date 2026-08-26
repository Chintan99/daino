"""End-to-end tests for the Daino GUI FastAPI backend.

These exercise the real routes and WebSocket transport against a real project
context (no mocked services), covering the Definition-of-Done backend surface:
sessions, file read/write + conflict handling, Git status, design persistence
and mutation, preview detection, terminal lifecycle, and event streaming.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from daino.application import initialize_project, open_project
from daino.events import GitChanged
from daino.server.app import create_app


@pytest.fixture
def client(git_repo: Path) -> Iterator[TestClient]:
    initialize_project(git_repo)
    context = open_project(git_repo)
    app = create_app(context)
    with TestClient(app) as test_client:
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
    with client.websocket_connect("/ws/session/latest") as ws:
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
