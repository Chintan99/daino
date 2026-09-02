"""What the agent can do inside a workspace, and what it is told about it."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.agents.tool_schemas import CHAT_TOOL_SPECS, WORKSPACE_TOOL_SPECS
from daino.config import default_settings, save_settings
from daino.model_router import ModelRole
from daino.persistence import Database
from daino.prompts import WORKSPACE_AGENT_SYSTEM
from daino.schemas import AgentAction, ToolResult
from daino.tools.editing import ActionExecutor, EditTools
from daino.workbench.research import SourceRecordingWeb
from daino.workbench.service import WorkbenchService


@pytest.fixture
def workbench(tmp_path: Path) -> Iterator[WorkbenchService]:
    settings = default_settings(tmp_path)
    save_settings(settings, tmp_path)
    database = Database(settings, tmp_path)
    database.initialize()
    yield WorkbenchService(tmp_path, database)
    database.engine.dispose()


def _executor(
    tmp_path: Path, workbench: WorkbenchService | None, workspace_id: str = ""
) -> ActionExecutor:
    return ActionExecutor(
        EditTools(tmp_path),
        workbench=workbench,
        workspace_id=workspace_id,
    )


async def test_the_agent_gets_only_the_verbs_a_file_cannot_replace() -> None:
    """Documents are real files, so no new file tools are needed — or wanted.

    The workspace verbs each cover something a file genuinely cannot say: what
    the workspace holds, what the plan is, where a document came from, what a
    rendering of it should be, and what should be built elsewhere. There is
    still no workspace_write or workspace_delete, because write and replace
    already do that.
    """
    names = {spec["function"]["name"] for spec in WORKSPACE_TOOL_SPECS}

    assert {name for name in names if name.startswith("workspace_")} == {
        "workspace_read",
        "workspace_plan",
        "workspace_task",
        "workspace_link",
        "workspace_code",
        "workspace_deliverable",
    }
    assert {"read_file", "write", "replace", "grep"} <= names


async def test_reading_a_workspace_orients_without_dumping_documents(
    tmp_path: Path, workbench: WorkbenchService
) -> None:
    """An orientation call must not spend the context window on full text."""
    workspace = workbench.create("Pricing", goal="Compare vendors", kind="research")
    workbench.write_artifact(workspace.id, "findings.md", "# Findings\n\n" + ("long body. " * 400))
    workbench.save_upload(workspace.id, "vendors.csv", b"vendor,price\nAcme,40\n")
    executor = _executor(tmp_path, workbench, workspace.id)

    result, _ = await executor.execute(AgentAction(thought="orient", action="workspace_read"))

    assert result.success
    data = result.data
    assert data["goal"] == "Compare vendors"
    assert [item["task_id"] for item in data["plan"]]
    findings = next(item for item in data["documents"] if item["title"] == "Findings")
    # The preview is bounded; the full 4 KB body is one read_file away, and the
    # path given is exactly what read_file accepts.
    assert len(findings["preview"]) <= 200
    assert findings["bytes"] > 4_000
    assert len(str(data)) < 2_000
    assert findings["path"] == f"{workspace.folder}/findings.md"
    upload = data["uploads"][0]
    assert upload["read_this_instead"].endswith("uploads/.extracted/vendors.md")


async def test_the_agent_keeps_the_visible_plan_current(
    tmp_path: Path, workbench: WorkbenchService
) -> None:
    workspace = workbench.create("Pricing")
    executor = _executor(tmp_path, workbench, workspace.id)

    planned, _ = await executor.execute(
        AgentAction(
            thought="plan",
            action="workspace_plan",
            plan_steps=["Read the brief", "Draft findings"],
        )
    )
    task_id = planned.data["tasks"][0]["task_id"]
    updated, _ = await executor.execute(
        AgentAction(
            thought="starting",
            action="workspace_task",
            task_id=task_id,
            task_status="in_progress",
        )
    )

    assert [item["content"] for item in planned.data["tasks"]] == [
        "Read the brief",
        "Draft findings",
    ]
    assert updated.data["task"]["status"] == "in_progress"
    assert workbench.get(workspace.id).tasks[0].status == "in_progress"


async def test_workspace_actions_are_refused_where_there_is_no_workspace(
    tmp_path: Path, workbench: WorkbenchService
) -> None:
    """Same contract as the design and command capabilities: refuse, explain."""
    without, _ = await _executor(tmp_path, None).execute(
        AgentAction(thought="try", action="workspace_read")
    )
    unopened, _ = await _executor(tmp_path, workbench).execute(
        AgentAction(thought="try", action="workspace_read")
    )

    assert not without.success
    assert "No workspace is available" in (without.error or "")
    assert not unopened.success
    assert "No workspace is open" in (unopened.error or "")


async def test_a_fetched_page_becomes_a_source_without_the_model_asking(
    workbench: WorkbenchService,
) -> None:
    """Citation that depends on the model remembering is citation with holes."""
    workspace = workbench.create("Pricing", kind="research")

    class FakeWeb:
        async def search(self, query: str, *, max_results: int = 5) -> ToolResult:
            return ToolResult(tool="web_search", success=True, data={"results": []})

        async def fetch(self, url: str, *, max_chars: int = 12_000) -> ToolResult:
            return ToolResult(
                tool="fetch_url",
                success=True,
                data={"url": url, "title": "Acme pricing", "content": "Acme costs $40."},
            )

    web = SourceRecordingWeb(FakeWeb(), workbench=workbench, workspace_id=workspace.id)  # type: ignore[arg-type]
    await web.fetch("https://acme.example/pricing")
    # Searching is not reading, so it files nothing.
    await web.search("acme pricing")

    sources = workbench.get(workspace.id).sources
    assert [item.url for item in sources] == ["https://acme.example/pricing"]
    assert sources[0].title == "Acme pricing"
    assert "Acme costs $40." in sources[0].snippet


async def test_a_failed_fetch_files_nothing(workbench: WorkbenchService) -> None:
    workspace = workbench.create("Pricing")

    class Broken:
        async def search(self, query: str, *, max_results: int = 5) -> ToolResult:
            return ToolResult(tool="web_search", success=False, error="no")

        async def fetch(self, url: str, *, max_chars: int = 12_000) -> ToolResult:
            return ToolResult(tool="fetch_url", success=False, error="refused")

    web = SourceRecordingWeb(Broken(), workbench=workbench, workspace_id=workspace.id)  # type: ignore[arg-type]
    await web.fetch("https://nope.example")

    assert workbench.get(workspace.id).sources == []


def test_the_workspace_prompt_is_not_the_coding_prompt() -> None:
    """Every other prompt says "repository"; this one has a different job."""
    assert "coding agent" not in WORKSPACE_AGENT_SYSTEM
    assert "workspace_read" in WORKSPACE_AGENT_SYSTEM
    # The two instructions that matter most for knowledge work.
    assert "footnote" in WORKSPACE_AGENT_SYSTEM
    assert "Do not propose verification commands" in WORKSPACE_AGENT_SYSTEM


def test_the_researcher_role_is_optional(tmp_path: Path) -> None:
    """Adding a routed role must not break a configuration that predates it."""
    from daino.application import MissionApplicationService
    from daino.application.context import initialize_project, open_project

    initialize_project(tmp_path)
    service = MissionApplicationService(open_project(tmp_path))

    # Nothing configured: falls back rather than raising.
    assert service.workspace_role() == ModelRole.BUILDER

    settings = service.context.settings
    from daino.config.models import ModelProfileConfig, ProviderConfig

    settings.routing["researcher"] = "cheap"
    settings.models["cheap"] = ModelProfileConfig(provider="local", model="m")
    settings.providers["local"] = ProviderConfig(type="ollama", base_url="http://x", model="m")

    assert service.workspace_role() == ModelRole.RESEARCHER
    # A pinned session profile still wins, as everywhere else.
    assert service.workspace_role("cheap") == ModelRole.BUILDER


# ------------------------------------------------------- parallel research


def test_researchers_fan_out_then_one_synthesises() -> None:
    from daino.agents.team import validate_team_plan
    from daino.workbench.investigation import research_plan

    plan = research_plan(
        "Which vendor is cheapest?", ["Acme pricing", "Globex pricing", "Initech pricing"]
    )
    waves = validate_team_plan(plan)

    assert [[member.id for member in wave] for wave in waves] == [
        ["research-1", "research-2", "research-3"],
        ["synthesis"],
    ]
    assert plan.members[-1].dependencies == ["research-1", "research-2", "research-3"]


def test_every_researcher_is_read_only_so_none_can_collide() -> None:
    """What makes the fan-out safe: no write scopes means no overlap to arbitrate."""
    from daino.workbench.investigation import research_plan

    plan = research_plan("q", ["a", "b"])

    assert all(member.read_only for member in plan.members)
    assert all(member.scope == [] for member in plan.members)


def test_the_roster_is_capped_however_many_angles_are_proposed() -> None:
    from daino.agents.team import validate_team_plan
    from daino.workbench.investigation import MAX_RESEARCHERS, research_plan

    plan = research_plan("q", [f"sub {index}" for index in range(50)])

    assert len(plan.members) == MAX_RESEARCHERS + 1
    validate_team_plan(plan)  # still within MAX_TEAM_MEMBERS


def test_a_question_with_no_sub_questions_still_researches_it() -> None:
    from daino.workbench.investigation import research_plan

    plan = research_plan("Why is churn up?", [])

    assert len(plan.members) == 2
    assert "Why is churn up?" in plan.members[0].objective


def test_researchers_can_reach_the_web_but_cannot_edit() -> None:
    from daino.agents.tool_schemas import RESEARCH_TOOL_SPECS

    names = {spec["function"]["name"] for spec in RESEARCH_TOOL_SPECS}

    assert {"web_search", "fetch_url", "read_file", "grep"} <= names
    assert not names & {"write", "replace", "multi_edit", "delete", "run_command"}


def test_a_team_without_a_web_tool_still_refuses_research() -> None:
    """QA and code rosters must not gain outbound access by this change."""
    from pathlib import Path as _Path

    from daino.agents.team import TeamRunner

    runner = TeamRunner(object(), _Path("."))  # type: ignore[arg-type]

    assert runner.web is None


async def test_workspace_tools_stay_out_of_a_repository_chat() -> None:
    """A code session must not be offered verbs it can only be refused for.

    Advertising them anyway is how a build task ended up calling workspace_plan
    and being told "No workspace is open": a wasted turn the model reads as a
    failure, on the way to the no-progress guard.
    """
    code = {spec["function"]["name"] for spec in CHAT_TOOL_SPECS}

    assert not {name for name in code if name.startswith("workspace_")}
    # The repository surface keeps everything a code session actually uses.
    assert {"read_file", "write", "replace", "grep", "run_command"} <= code


async def test_a_workspace_can_draw_but_a_repository_chat_cannot_orchestrate() -> None:
    """The surfaces overlap in one direction only, and deliberately.

    Design was once withheld from workspaces on the grounds that knowledge work
    and repository design are separate activities. A proposal whose architecture
    section is three paragraphs because the agent had no way to draw it says
    otherwise, so a workspace now reaches the same canvas DESIGN edits. The
    other direction stays shut: a repository chat has no workspace open, so
    offering it workspace verbs only earns a refusal.
    """
    workspace = {spec["function"]["name"] for spec in WORKSPACE_TOOL_SPECS}
    code = {spec["function"]["name"] for spec in CHAT_TOOL_SPECS}

    assert {name for name in workspace if "design" in name}
    assert {name for name in code if "design" in name}
    assert not {name for name in code if name.startswith("workspace_")}
    # Both still get the shared file/web/memory surface they are built on.
    assert {"read_file", "write", "replace", "grep"} <= workspace & code


def test_the_session_type_decides_which_tool_surface_the_agent_is_handed(
    tmp_path: Path, workbench: WorkbenchService
) -> None:
    """The wiring, not just the lists: a code session must not be given workspace verbs.

    mission_service already branched on the open workspace for the role, the
    system prompt and require_verified_finish, but passed CHAT_TOOL_SPECS
    unconditionally — so a repository chat was still offered workspace_plan.
    """
    from daino.application.mission_service import MissionApplicationService

    select = MissionApplicationService._chat_tool_specs
    workspace = workbench.create("Research", goal="Compare vendors", kind="research")

    code_tools = {spec["function"]["name"] for spec in select(None)}
    workspace_tools = {spec["function"]["name"] for spec in select(workspace)}

    # The reported bug: a code session offered verbs it can only be refused for.
    assert not {name for name in code_tools if name.startswith("workspace_")}
    assert {"workspace_read", "workspace_plan", "workspace_task"} <= workspace_tools
    assert {"read_file", "write", "replace", "grep"} <= code_tools & workspace_tools
