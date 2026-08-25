"""Presentation-neutral, privacy-safe execution maps for completed and live missions."""

from __future__ import annotations

import re
import shlex
from collections import defaultdict
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select

from vasuki.application.context import ProjectContext
from vasuki.application.view_models import (
    ExecutionPrompt,
    ExecutionTrace,
    ExecutionTraceStep,
    ModelUsage,
)
from vasuki.persistence.models import Mission, MissionEventRecord, ModelCall, ToolCall
from vasuki.security import redact

# Event payloads are intentionally handled through an allowlist below. In
# particular, never pass arbitrary ``payload``, ``details``, errors, diffs,
# command output, or model-generated summaries into a presentation model.
_KEY_EVENT_KINDS = frozenset(
    {
        "MissionCreated",
        "MissionStarted",
        "MissionPaused",
        "MissionCompleted",
        "MissionFailed",
        "TaskStarted",
        "TaskCompleted",
        "TodoUpdated",
        "ContextCompacted",
        "AgentRoleChanged",
        "ModelSelected",
        "ModelEscalationRequested",
        "TeamPlanned",
        "TeamMemberStarted",
        "TeamMemberCompleted",
        "ToolStarted",
        "ToolProgress",
        "ToolCompleted",
        "ToolFailed",
        "FileChanged",
        "TestsStarted",
        "TestsCompleted",
        "ApprovalRequested",
        "ApprovalResolved",
        "CheckpointCreated",
        "DeploymentStarted",
        "DeploymentProgress",
        "DeploymentVerified",
        "DeploymentFailed",
        "RollbackStarted",
        "RollbackCompleted",
    }
)

_EVENT_CATEGORY = {
    "MissionCreated": "mission",
    "MissionStarted": "mission",
    "MissionPaused": "mission",
    "MissionCompleted": "mission",
    "MissionFailed": "mission",
    "TaskStarted": "task",
    "TaskCompleted": "task",
    "TodoUpdated": "task",
    "ContextCompacted": "context",
    "AgentRoleChanged": "agent",
    "ModelSelected": "model",
    "ModelEscalationRequested": "model",
    "TeamPlanned": "team",
    "TeamMemberStarted": "team",
    "TeamMemberCompleted": "team",
    "ToolStarted": "tool",
    "ToolProgress": "tool",
    "ToolCompleted": "tool",
    "ToolFailed": "tool",
    "FileChanged": "file",
    "TestsStarted": "tests",
    "TestsCompleted": "tests",
    "ApprovalRequested": "approval",
    "ApprovalResolved": "approval",
    "CheckpointCreated": "checkpoint",
    "DeploymentStarted": "deployment",
    "DeploymentProgress": "deployment",
    "DeploymentVerified": "deployment",
    "DeploymentFailed": "deployment",
    "RollbackStarted": "deployment",
    "RollbackCompleted": "deployment",
}

_EVENT_STATUS = {
    "MissionCreated": "pending",
    "MissionStarted": "running",
    "MissionPaused": "paused",
    "MissionCompleted": "completed",
    "MissionFailed": "failed",
    "TaskStarted": "running",
    "TaskCompleted": "completed",
    "TeamMemberStarted": "running",
    "ToolStarted": "running",
    "ToolProgress": "running",
    "ToolCompleted": "completed",
    "ToolFailed": "failed",
    "TestsStarted": "running",
    "DeploymentStarted": "running",
    "DeploymentProgress": "running",
    "DeploymentFailed": "failed",
    "RollbackStarted": "running",
    "RollbackCompleted": "completed",
}

_TOOL_LABELS = {
    "read_file": "Read file",
    "search_text": "Search text",
    "list_directory": "List directory",
    "replace": "Edit file",
    "write": "Write file",
    "delete": "Delete file",
    "multi_edit": "Edit files",
    "run_command": "Run command",
    "resolve_command_failure": "Resolve command failure",
    "web_search": "Search web",
    "fetch_url": "Fetch web page",
    "glob": "Find files",
    "grep": "Search files",
    "todo": "Update task list",
    "memory_search": "Search memory",
    "memory_save": "Save memory",
    "memory_update": "Update memory",
    "memory_forget": "Forget memory",
    "memory_list": "List memory",
    "memory_verify": "Verify memory",
    "respond": "Prepare response",
    "finish": "Finish work",
    "command": "Run verification command",
}

_PATH_ACTIONS = frozenset(
    {"read_file", "list_directory", "replace", "write", "delete", "multi_edit"}
)
_QUERY_ACTIONS = frozenset({"search_text", "grep", "web_search", "memory_search"})
_COMMAND_ACTIONS = frozenset({"run_command", "resolve_command_failure", "command"})


class ExecutionMapApplicationService:
    """Build query-only execution maps without leaking model-private data.

    The persisted ``ToolCall.arguments`` can contain an entire ``AgentAction``,
    including ``thought``, file contents, edit spans, commands, URLs, and user
    data. This service copies only an action-specific, redacted target into its
    view models. Tool nodes otherwise derive from Vasuki's bounded identifier,
    outcome, and duration; result data is never copied.
    """

    def __init__(self, context: ProjectContext) -> None:
        self.context = context

    def prompts(self, limit: int | None = None) -> list[ExecutionPrompt]:
        """List newest mission-backed prompts with aggregate execution usage."""
        if limit is not None and limit <= 0:
            return []
        project_id = self.context.database.project().id
        with self.context.database.session() as session:
            mission_query = (
                select(Mission)
                .where(Mission.project_id == project_id)
                .order_by(Mission.created_at.desc(), Mission.id.desc())
            )
            if limit is not None:
                mission_query = mission_query.limit(limit)
            missions = list(session.scalars(mission_query))
            mission_ids = [mission.id for mission in missions]
            model_calls = (
                list(
                    session.scalars(
                        select(ModelCall).where(ModelCall.mission_id.in_(mission_ids))
                    )
                )
                if mission_ids
                else []
            )
            tool_calls = (
                list(
                    session.scalars(select(ToolCall).where(ToolCall.mission_id.in_(mission_ids)))
                )
                if mission_ids
                else []
            )
            events = (
                list(
                    session.scalars(
                        select(MissionEventRecord).where(
                            MissionEventRecord.mission_id.in_(mission_ids),
                            MissionEventRecord.kind.in_(_KEY_EVENT_KINDS),
                        )
                    )
                )
                if mission_ids
                else []
            )

        calls_by_mission = _group_by_mission(model_calls)
        tools_by_mission = _group_by_mission(tool_calls)
        events_by_mission = _group_by_mission(events)
        return [
            _prompt_summary(
                mission,
                calls_by_mission[mission.id],
                tools_by_mission[mission.id],
                events_by_mission[mission.id],
            )
            for mission in missions
        ]

    def trace(self, mission_id: str) -> ExecutionTrace:
        """Return the chronological, sanitized trace for one project mission."""
        project_id = self.context.database.project().id
        with self.context.database.session() as session:
            mission = session.get(Mission, mission_id)
            if mission is None or mission.project_id != project_id:
                raise ValueError(f"Unknown mission {mission_id}")
            model_calls = list(
                session.scalars(
                    select(ModelCall)
                    .where(ModelCall.mission_id == mission_id)
                    .order_by(ModelCall.created_at, ModelCall.id)
                )
            )
            tool_calls = list(
                session.scalars(
                    select(ToolCall)
                    .where(ToolCall.mission_id == mission_id)
                    .order_by(ToolCall.created_at, ToolCall.id)
                )
            )
            events = list(
                session.scalars(
                    select(MissionEventRecord)
                    .where(
                        MissionEventRecord.mission_id == mission_id,
                        MissionEventRecord.kind.in_(_KEY_EVENT_KINDS),
                    )
                    .order_by(MissionEventRecord.created_at, MissionEventRecord.id)
                )
            )

        visible_events = _visible_events(events, model_calls, tool_calls)
        steps = [*(_event_step(item) for item in visible_events)]
        steps.extend(_model_step(item) for item in model_calls)
        steps.extend(_tool_step(item) for item in tool_calls)
        steps.sort(key=lambda item: (item.timestamp, item.id))
        return ExecutionTrace(
            mission_id=mission.id,
            request=_safe_text(mission.request, limit=20_000),
            status=_safe_identifier(mission.status),
            created_at=mission.created_at,
            steps=tuple(steps),
            total_input_tokens=sum(item.input_tokens for item in model_calls),
            total_output_tokens=sum(item.output_tokens for item in model_calls),
            estimated_cost=sum(item.estimated_cost for item in model_calls),
            total_model_latency_ms=sum(item.latency_ms for item in model_calls),
            total_tool_duration_seconds=sum(item.duration_seconds for item in tool_calls),
            model_call_count=len(model_calls),
            tool_count=len(tool_calls),
        )

    # Verbose aliases make the service self-documenting to non-TUI clients.
    def list_prompts(self, limit: int | None = None) -> list[ExecutionPrompt]:
        return self.prompts(limit)

    def execution_trace(self, mission_id: str) -> ExecutionTrace:
        return self.trace(mission_id)


def _group_by_mission(items: Iterable[Any]) -> defaultdict[str, list[Any]]:
    grouped: defaultdict[str, list[Any]] = defaultdict(list)
    for item in items:
        if item.mission_id:
            grouped[item.mission_id].append(item)
    return grouped


def _visible_events(
    events: list[MissionEventRecord],
    model_calls: list[ModelCall],
    tool_calls: list[ToolCall],
) -> list[MissionEventRecord]:
    """Collapse lifecycle rows once their more useful durable call exists.

    A selection/start row remains visible while an invocation is still in
    flight or crashed before producing a durable call. Once a ModelCall or
    ToolCall exists, showing both lifecycle rows and the call creates several
    graph nodes for one action, so matched lifecycle rows are suppressed.
    """
    ordered = sorted(events, key=lambda item: (item.created_at, item.id))
    hidden: set[str] = set()

    selections = [item for item in ordered if item.kind == "ModelSelected"]
    for call in sorted(model_calls, key=lambda item: (item.created_at, item.id)):
        matches = [
            item
            for item in selections
            if item.id not in hidden
            and item.created_at <= call.created_at
            and _same_model_selection(item.payload, call)
        ]
        if matches:
            hidden.add(matches[-1].id)

    groups: list[tuple[str, list[MissionEventRecord]]] = []
    active: defaultdict[str, list[list[MissionEventRecord]]] = defaultdict(list)
    for item in ordered:
        if item.kind not in {"ToolStarted", "ToolProgress", "ToolCompleted", "ToolFailed"}:
            continue
        tool = _event_tool_name(item)
        if item.kind == "ToolStarted":
            group = [item]
            groups.append((tool, group))
            active[tool].append(group)
        elif item.kind == "ToolProgress":
            if active[tool]:
                active[tool][-1].append(item)
            else:
                groups.append((tool, [item]))
        elif active[tool]:
            active[tool].pop(0).append(item)
        else:
            groups.append((tool, [item]))

    matched_groups: set[int] = set()
    for call in sorted(tool_calls, key=lambda item: (item.created_at, item.id)):
        matches = [
            (index, group)
            for index, (tool, group) in enumerate(groups)
            if index not in matched_groups
            and tool == call.tool
            and group[-1].kind in {"ToolCompleted", "ToolFailed"}
            and group[-1].created_at <= call.created_at
        ]
        if not matches:
            continue
        index, group = matches[-1]
        matched_groups.add(index)
        hidden.update(item.id for item in group)

    return [item for item in ordered if item.id not in hidden]


def _same_model_selection(payload: dict[str, Any], call: ModelCall) -> bool:
    if not isinstance(payload, dict):
        return False
    return payload.get("provider") == call.provider and payload.get("model") == call.model


def _event_tool_name(record: MissionEventRecord) -> str:
    payload = record.payload if isinstance(record.payload, dict) else {}
    value = payload.get("tool")
    return value if isinstance(value, str) else ""


def _prompt_summary(
    mission: Mission,
    model_calls: list[ModelCall],
    tool_calls: list[ToolCall],
    events: list[MissionEventRecord],
) -> ExecutionPrompt:
    visible_events = _visible_events(events, model_calls, tool_calls)
    return ExecutionPrompt(
        mission_id=mission.id,
        request=_safe_text(mission.request, limit=20_000),
        status=_safe_identifier(mission.status),
        created_at=mission.created_at,
        total_tokens=sum(item.input_tokens + item.output_tokens for item in model_calls),
        estimated_cost=sum(item.estimated_cost for item in model_calls),
        step_count=len(model_calls) + len(tool_calls) + len(visible_events),
        tool_count=len(tool_calls),
        model_call_count=len(model_calls),
    )


def _model_step(call: ModelCall) -> ExecutionTraceStep:
    provider = _safe_text(call.provider, limit=120)
    model = _safe_text(call.model, limit=160)
    role = _safe_identifier(call.role).replace("_", " ").title()
    usage = ModelUsage(
        provider=provider,
        model=model,
        role=_safe_identifier(call.role),
        input_tokens=max(0, call.input_tokens),
        output_tokens=max(0, call.output_tokens),
        estimated_cost=max(0.0, call.estimated_cost),
        latency_ms=max(0.0, call.latency_ms),
        success=call.success,
    )
    detail = f"{role or 'Agent'} via {provider or 'configured provider'}"
    return ExecutionTraceStep(
        id=f"model:{call.id}",
        kind="model",
        title=f"Model · {model or 'configured model'}",
        detail=detail,
        status="completed" if call.success else "failed",
        timestamp=call.created_at,
        target=model,
        duration_seconds=usage.latency_ms / 1000,
        model_usage=usage,
    )


def _tool_step(call: ToolCall) -> ExecutionTraceStep:
    # Read only a tool-specific allowlisted target. Never copy arbitrary
    # arguments or call.result_summary: those may contain thoughts, file text,
    # edit spans, command arguments/output, credentialed URLs, or other secrets.
    tool = _safe_identifier(call.tool, fallback="tool")
    return ExecutionTraceStep(
        id=f"tool:{call.id}",
        kind="tool",
        title=f"Tool · {_tool_label(tool)}",
        detail=tool,
        status="completed" if call.success else "failed",
        timestamp=call.created_at,
        target=_tool_target(tool, call.arguments),
        duration_seconds=max(0.0, call.duration_seconds),
    )


def _event_step(record: MissionEventRecord) -> ExecutionTraceStep:
    payload = record.payload if isinstance(record.payload, dict) else {}
    title, detail = _event_description(record.kind, payload)
    return ExecutionTraceStep(
        id=f"event:{record.id}",
        kind=_EVENT_CATEGORY.get(record.kind, "event"),
        title=title,
        detail=detail,
        status=_event_status(record.kind, payload),
        timestamp=record.created_at,
        target=_event_target(record.kind, payload),
        duration_seconds=_event_duration(record.kind, payload),
    )


def _event_description(kind: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Describe only explicitly safe fields; arbitrary payload text stays private."""
    if kind == "MissionCreated":
        return "Prompt accepted", _safe_identifier(payload.get("mode"), fallback="mission")
    if kind == "MissionStarted":
        return "Mission started", "Workspace prepared"
    if kind == "MissionPaused":
        return "Mission paused", "Waiting to continue"
    if kind == "MissionCompleted":
        return "Mission completed", "Execution finished"
    if kind == "MissionFailed":
        return "Mission failed", "Failure details are available in Logs"
    if kind in {"TaskStarted", "TaskCompleted"}:
        state = "started" if kind == "TaskStarted" else "completed"
        title = _safe_text(payload.get("title"), limit=240) or "Task"
        return f"Task {state}", title
    if kind == "TodoUpdated":
        todos = payload.get("todos")
        count = len(todos) if isinstance(todos, list) else 0
        return "Task map updated", f"{count} task(s)"
    if kind == "ContextCompacted":
        before = _safe_int(payload.get("before_tokens"))
        after = _safe_int(payload.get("after_tokens"))
        return "Context compacted", f"{before:,} → {after:,} tokens"
    if kind == "AgentRoleChanged":
        role = _safe_identifier(payload.get("role"), fallback="agent").replace("_", " ")
        return "Agent role changed", role.title()
    if kind == "ModelSelected":
        model = _safe_text(payload.get("model"), limit=160) or "configured model"
        provider = _safe_text(payload.get("provider"), limit=120) or "configured provider"
        return "Model selected", f"{model} via {provider}"
    if kind == "ModelEscalationRequested":
        role = _safe_identifier(payload.get("role"), fallback="agent").replace("_", " ")
        return "Model escalation requested", role.title()
    if kind == "TeamPlanned":
        members = payload.get("members")
        count = len(members) if isinstance(members, list) else 0
        return "Agent team planned", f"{count} member(s)"
    if kind in {"TeamMemberStarted", "TeamMemberCompleted"}:
        member = _safe_identifier(payload.get("member"), fallback="member")
        role = _safe_identifier(payload.get("role"), fallback="agent").replace("_", " ")
        state = "started" if kind == "TeamMemberStarted" else "completed"
        detail = f"{member} · {role}"
        if kind == "TeamMemberCompleted":
            detail += f" · {_safe_int(payload.get('steps'))} step(s)"
        return f"Team member {state}", detail
    if kind in {"ToolStarted", "ToolProgress", "ToolCompleted", "ToolFailed"}:
        tool = _safe_identifier(payload.get("tool"), fallback="tool")
        state = {
            "ToolStarted": "started",
            "ToolProgress": "working",
            "ToolCompleted": "completed",
            "ToolFailed": "failed",
        }[kind]
        return f"Tool {state} · {_tool_label(tool)}", tool
    if kind == "FileChanged":
        action = _safe_identifier(payload.get("action"), fallback="changed").replace("_", " ")
        path = _safe_text(payload.get("path"), limit=400) or "project file"
        added = _safe_int(payload.get("added"))
        removed = _safe_int(payload.get("removed"))
        counts = f"+{added} /−{removed}" if added or removed else action
        return "File changed", f"{path} · {counts}"
    if kind == "TestsStarted":
        commands = payload.get("commands")
        count = len(commands) if isinstance(commands, list) else 0
        return "Verification started", f"{count} check(s); commands withheld"
    if kind == "TestsCompleted":
        passed = bool(payload.get("passed"))
        passed_count = _safe_int(payload.get("passed_count"))
        failed_count = _safe_int(payload.get("failed_count"))
        return (
            "Verification passed" if passed else "Verification failed",
            f"{passed_count} passed · {failed_count} failed",
        )
    if kind == "ApprovalRequested":
        category = _safe_identifier(payload.get("category"), fallback="action")
        risk = _safe_identifier(payload.get("risk"), fallback="medium")
        return "Approval requested", f"{category} · {risk} risk"
    if kind == "ApprovalResolved":
        category = _safe_identifier(payload.get("category"), fallback="action")
        outcome = "approved" if bool(payload.get("approved")) else "rejected"
        return "Approval resolved", f"{category} · {outcome}"
    if kind == "CheckpointCreated":
        checkpoint = _safe_identifier(payload.get("checkpoint_id"), fallback="checkpoint")
        return "Checkpoint created", checkpoint
    if kind in {"DeploymentStarted", "DeploymentProgress", "DeploymentVerified"}:
        target = _safe_identifier(payload.get("target"), fallback="target")
        if kind == "DeploymentStarted":
            return "Deployment started", target
        if kind == "DeploymentProgress":
            stage = _safe_identifier(payload.get("stage"), fallback="working")
            return "Deployment progress", f"{target} · {stage}"
        healthy = "healthy" if bool(payload.get("healthy")) else "unhealthy"
        return "Deployment verified", f"{target} · {healthy}"
    if kind == "DeploymentFailed":
        target = _safe_identifier(payload.get("target"), fallback="target")
        return "Deployment failed", f"{target}; failure details are available in Logs"
    if kind in {"RollbackStarted", "RollbackCompleted"}:
        target = _safe_identifier(payload.get("target"), fallback="target")
        state = "started" if kind == "RollbackStarted" else "completed"
        return f"Rollback {state}", target
    return "Execution event", _safe_identifier(kind, fallback="event")


def _event_status(kind: str, payload: dict[str, Any]) -> str:
    if kind == "TeamMemberCompleted":
        return "completed" if bool(payload.get("success", True)) else "failed"
    if kind == "TestsCompleted":
        return "completed" if bool(payload.get("passed")) else "failed"
    if kind == "ApprovalResolved":
        return "completed" if bool(payload.get("approved")) else "failed"
    if kind == "DeploymentVerified":
        return "completed" if bool(payload.get("healthy")) else "failed"
    return _EVENT_STATUS.get(kind, "info")


def _event_duration(kind: str, payload: dict[str, Any]) -> float:
    if kind in {"ToolCompleted", "TestsCompleted"}:
        value = payload.get("duration_seconds", 0)
        if isinstance(value, int | float):
            return max(0.0, float(value))
    return 0.0


def _event_target(kind: str, payload: dict[str, Any]) -> str:
    if kind == "ModelSelected":
        return _safe_target(payload.get("model"), limit=160)
    if kind in {"ToolStarted", "ToolProgress", "ToolCompleted", "ToolFailed"}:
        tool = _safe_identifier(payload.get("tool"), fallback="tool")
        summary = payload.get("summary")
        return _tool_target_from_value(tool, summary)
    if kind == "FileChanged":
        return _safe_target(payload.get("path"), limit=400)
    if kind in {"TaskStarted", "TaskCompleted"}:
        return _safe_target(payload.get("title"), limit=240)
    if kind in {"TeamMemberStarted", "TeamMemberCompleted"}:
        return _safe_identifier(payload.get("member"), fallback="member")
    if kind == "CheckpointCreated":
        return _safe_identifier(payload.get("checkpoint_id"), fallback="checkpoint")
    if kind.startswith("Deployment") or kind.startswith("Rollback"):
        return _safe_identifier(payload.get("target"), fallback="target")
    return ""


def _tool_target(tool: str, arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    action = tool.rsplit(".", 1)[-1].casefold()
    if action in _PATH_ACTIONS:
        return _safe_target(arguments.get("path"), limit=400)
    if action in _QUERY_ACTIONS:
        return _safe_target(arguments.get("query"), limit=240)
    if action == "glob":
        return _safe_target(arguments.get("pattern"), limit=240)
    if action in _COMMAND_ACTIONS:
        return _command_executable(arguments.get("command"))
    if action == "fetch_url":
        return _safe_url(arguments.get("url"))
    return ""


def _tool_target_from_value(tool: str, value: Any) -> str:
    """Sanitize the pre-execution event summary according to its tool action."""
    action = tool.rsplit(".", 1)[-1].casefold()
    if action in _COMMAND_ACTIONS:
        return _command_executable(value)
    if action == "fetch_url":
        return _safe_url(value)
    if action in _PATH_ACTIONS | _QUERY_ACTIONS | {"glob"}:
        return _safe_target(value, limit=400 if action in _PATH_ACTIONS else 240)
    return ""


def _command_executable(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parts = shlex.split(value)
    except ValueError:
        return "command"
    executable = ""
    for part in parts:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", part):
            continue
        executable = part
        break
    if not executable:
        return "command"
    executable = executable.replace("\\", "/").rsplit("/", 1)[-1]
    return _safe_identifier(executable, fallback="command")


def _safe_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "web-page"
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        return "web-page"
    # User information, query parameters, and fragments are intentionally
    # discarded because all three routinely carry credentials.
    path = _safe_target(parsed.path, limit=240)
    return f"{parsed.scheme}://{parsed.hostname}{port}{path}"


def _tool_label(tool: str) -> str:
    action = tool.rsplit(".", 1)[-1].casefold()
    if action in _TOOL_LABELS:
        return _TOOL_LABELS[action]
    return action.replace("_", " ").replace("-", " ").title() or "Tool"


def _safe_text(value: Any, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    clean = redact(value).replace("\x00", "")
    clean = "".join(character for character in clean if character >= " " or character in "\n\t")
    return clean[:limit]


def _safe_identifier(value: Any, *, fallback: str = "") -> str:
    clean = _safe_text(value, limit=240).strip()
    clean = re.sub(r"[^A-Za-z0-9_.:/@+-]+", "-", clean).strip("-")
    return clean or fallback


def _safe_target(value: Any, *, limit: int) -> str:
    clean = _safe_text(value, limit=limit).replace("\n", " ").replace("\t", " ")
    return " ".join(clean.split())


def _safe_int(value: Any) -> int:
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0
