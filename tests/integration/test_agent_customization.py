"""The browser's half of `/mode`, `/effort`, `/verbose`, `/memory`, `/playbooks`.

Each of these already worked in the terminal client; the point of these tests is
that the GUI drives the *same* services, so a session configured in one client is
configured in the other.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from daino.application import initialize_project, open_project
from daino.server.app import create_app
from daino.tui.keybindings import SLASH_COMMANDS

LOCAL_HOST = "127.0.0.1:4173"


@pytest.fixture
def client(git_repo: Path) -> Iterator[TestClient]:
    initialize_project(git_repo)
    context = open_project(git_repo)
    app = create_app(context)
    with TestClient(app, base_url=f"http://{LOCAL_HOST}") as test_client:
        test_client.app_root = git_repo  # type: ignore[attr-defined]
        yield test_client
    context.close()


@pytest.fixture
def session(client: TestClient) -> str:
    return client.post("/api/sessions", json={"title": "config"}).json()["id"]


def test_config_reports_session_policy_and_customizations(client: TestClient, session: str) -> None:
    payload = client.get("/api/agent/config", params={"session_id": session}).json()

    # Autonomy options carry the same descriptions the TUI shows.
    assert payload["autonomy"]["mode"] == "ask"
    assert [item["id"] for item in payload["autonomy"]["options"]] == [
        "plan",
        "ask",
        "session",
        "full",
    ]
    assert "read-only planning" in payload["autonomy"]["options"][0]["hint"]

    assert payload["effort"]["value"] == "auto"
    assert "xhigh" in payload["effort"]["options"]
    assert payload["verbose"] is True

    # Every agent role is listed, including the debugger.
    roles = {entry["role"] for entry in payload["roles"]}
    assert {"architect", "builder", "reviewer", "debugger", "deployer"} <= roles

    # Built-in playbooks are discovered and fully described.
    names = {item["name"] for item in payload["playbooks"]}
    assert "fix-failing-test" in names
    playbook = next(item for item in payload["playbooks"] if item["name"] == "fix-failing-test")
    assert playbook["builtin"] is True
    assert playbook["stages"] and playbook["allowed_tools"]

    assert payload["memory"]["total"] == 0
    # The repository file is offered even before it exists, so it can be created.
    scopes = {file["scope"]: file for file in payload["instructions"]["files"]}
    assert scopes["repository"]["exists"] is False
    assert scopes["repository"]["relative_path"] == "DAINO.md"


def test_autonomy_effort_and_verbose_persist_to_the_session(
    client: TestClient, session: str
) -> None:
    assert (
        client.post("/api/agent/autonomy", json={"session_id": session, "mode": "plan"}).json()[
            "mode"
        ]
        == "plan"
    )
    assert (
        client.post("/api/agent/verbose", json={"session_id": session, "enabled": False}).json()[
            "verbose"
        ]
        is False
    )

    payload = client.get("/api/agent/config", params={"session_id": session}).json()
    assert payload["autonomy"]["mode"] == "plan"
    assert payload["verbose"] is False

    # The same values are what the TUI's service reads back.
    state = client.app.state.gui  # type: ignore[attr-defined]
    assert state.missions.interaction_mode(session).value == "plan"
    assert state.missions.verbose_enabled(session) is False

    # Unknown modes and sessions are refused, not silently accepted.
    assert (
        client.post("/api/agent/autonomy", json={"session_id": session, "mode": "yolo"}).status_code
        == 422
    )
    assert (
        client.post("/api/agent/autonomy", json={"session_id": "nope", "mode": "plan"}).status_code
        == 400
    )
    # Effort needs a configured model profile; without one it says so.
    assert (
        client.post("/api/agent/effort", json={"session_id": session, "effort": "high"}).status_code
        == 400
    )


def test_instruction_layers_are_reported_with_precedence(client: TestClient, session: str) -> None:
    root: Path = client.app_root  # type: ignore[attr-defined]
    (root / "DAINO.md").write_text("style: terse\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "DAINO.md").write_text("style: verbose\n", encoding="utf-8")

    files = client.get("/api/agent/config", params={"session_id": session}).json()["instructions"][
        "files"
    ]
    by_path = {item["relative_path"]: item for item in files}
    assert by_path["DAINO.md"]["exists"] is True
    assert by_path["src/DAINO.md"]["scope"] == "scoped"
    assert by_path["src/DAINO.md"]["editable_in_editor"] is True

    # The closer layer wins on a conflicting key — the resolver's own behaviour,
    # which is exactly why the panel shows the resolved text rather than a list.
    resolved = client.get("/api/agent/instructions/effective", params={"path": "src/app.py"}).json()
    assert "style: verbose" in resolved["text"]
    assert "style: terse" not in resolved["text"]
    assert len(resolved["sources"]) == 2

    # The user-level file is read and written through the API, since it lives
    # outside the repository the file endpoints are scoped to.
    assert client.get("/api/agent/instructions/global").json()["exists"] is False
    client.put("/api/agent/instructions/global", json={"content": "always run tests\n"})
    written = client.get("/api/agent/instructions/global").json()
    assert written["exists"] is True
    assert written["content"] == "always run tests\n"
    assert "always run tests" in client.get("/api/agent/instructions/effective").json()["text"]


def test_memory_can_be_added_inspected_and_forgotten(client: TestClient, session: str) -> None:
    created = client.post(
        "/api/agent/memory",
        json={"content": "Deploys go out on Thursdays", "summary": "release day"},
    ).json()
    assert created["id"]

    items = client.get("/api/agent/memory").json()["items"]
    assert len(items) == 1
    item = items[0]
    # A fact the user states is authoritative, which the agent cannot self-grant.
    assert (item["type"], item["scope"], item["source_type"]) == ("user", "project", "user")
    assert item["content"] == "Deploys go out on Thursdays"

    assert client.get("/api/agent/memory", params={"q": "thursdays"}).json()["items"]
    assert client.get("/api/agent/memory", params={"type": "decision"}).json()["items"] == []
    assert client.post(f"/api/agent/memory/{created['id']}/verify").json()["verified"] is True

    assert (
        client.get("/api/agent/config", params={"session_id": session}).json()["memory"]["total"]
        == 1
    )

    assert client.delete(f"/api/agent/memory/{created['id']}").json()["forgotten"] is True
    assert client.get("/api/agent/memory").json()["items"] == []
    assert client.delete("/api/agent/memory/memory-missing").status_code == 404

    # Empty content is rejected rather than stored as a blank memory.
    assert client.post("/api/agent/memory", json={"content": "   "}).status_code == 400


def test_every_customization_slash_command_has_a_browser_route(client: TestClient) -> None:
    """The GUI must not silently lack a customization the TUI advertises."""
    covered = {
        "/mode": ("POST", "/api/agent/autonomy"),
        "/effort": ("POST", "/api/agent/effort"),
        "/verbose": ("POST", "/api/agent/verbose"),
        "/memory": ("GET", "/api/agent/memory"),
        "/playbooks": ("GET", "/api/agent/config"),
        "/provider": ("GET", "/api/settings"),
        "/model": ("POST", "/api/sessions/{session_id}/model"),
        "/runtime": ("PATCH", "/api/settings"),
        "/index": ("POST", "/api/repository/index"),
        "/settings": ("GET", "/api/settings"),
    }
    advertised = {command.name for command in SLASH_COMMANDS}
    assert set(covered) <= advertised, "the TUI renamed or dropped a command"

    # The OpenAPI document is the authoritative surface — walking `app.routes`
    # misses anything nested inside an included router.
    schema = client.get("/openapi.json").json()["paths"]
    routes = {
        (method.upper(), path) for path, operations in schema.items() for method in operations
    }
    for command, endpoint in covered.items():
        assert endpoint in routes, f"{command} has no browser equivalent at {endpoint}"
