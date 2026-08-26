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
