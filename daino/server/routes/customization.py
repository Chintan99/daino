"""Agent customization: autonomy, effort, instructions, memory, playbooks, roles.

Everything here already existed as a slash command in the terminal client —
``/mode``, ``/effort``, ``/verbose``, ``/memory``, ``/playbooks`` — and went
through the same application services these endpoints call. The browser had no
way to reach any of it, so a session started in the GUI was stuck on the default
autonomy policy with no view of the instructions or memory that were shaping its
answers.

Extensibility now has a card of its own. Hooks, MCP servers, skills, and
user-defined slash commands are all real, all configured on disk, and all
capable of failing quietly — a hook whose matcher does not compile, a server
whose command is not installed, a skill with no description. ``/extensions``
reports what loaded and what did not, because the alternative is a user
wondering why the thing they configured is not happening.

Read-only on purpose. A hook command runs through a shell and an MCP stdio server
launches a process, so their files live in the state directory, which nothing
running as the agent may write to. Editing them from a browser panel would
reopen exactly the hole that placement closes.

Plugins remain absent: a plugin is a bundle of the four things below, and a
bundle format is only worth having once people are asking to share the parts.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from daino.hooks import HookEvent
from daino.memory.instructions import (
    MAX_INSTRUCTION_BYTES,
    InstructionResolver,
    global_instruction_path,
)
from daino.memory.types import MemoryScope, MemoryType
from daino.model_router import ModelRole
from daino.playbooks.loader import PlaybookLoader
from daino.schemas.core import InteractionMode
from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api/agent", tags=["agent-config"])

#: The same wording the TUI shows when the mode changes, so the two clients
#: describe the policy identically.
MODE_HINTS: dict[str, str] = {
    "plan": "read-only planning; no implementation or deployment",
    "ask": "routine repository work is allowed; risky commands ask first",
    "session": "approval-gated agent commands are allowed for this session",
    "full": "normal in-scope work and mission gates run without prompts",
}

EFFORT_LEVELS = ("auto", "none", "minimal", "low", "medium", "high", "xhigh", "max")

#: Directories never worth walking when looking for scoped instruction files.
_SKIP_DIRS = {
    ".git",
    ".daino",
    ".vasuki",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
#: Bound the walk so a huge checkout cannot stall the panel.
_MAX_SCOPED_FILES = 100


class AutonomyRequest(BaseModel):
    session_id: str
    mode: Literal["plan", "ask", "session", "full"]


class EffortRequest(BaseModel):
    session_id: str
    effort: Literal["auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"]


class VerboseRequest(BaseModel):
    session_id: str
    enabled: bool


class InstructionBody(BaseModel):
    content: str = Field(max_length=MAX_INSTRUCTION_BYTES)


class RememberRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8_000)
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    scope: Literal["project", "global"] = "project"


class ClearMemoryRequest(BaseModel):
    scope: Literal["session", "project"]
    session_id: str = ""


def _instruction_files(root: Path) -> list[dict]:
    """Every instruction file the resolver can pick up, with its scope."""
    from daino.config import paths

    names = (paths.INSTRUCTION_FILENAME, paths.LEGACY_INSTRUCTION_FILENAME)
    items: list[dict] = []

    global_path = global_instruction_path()
    items.append(
        {
            "scope": "global",
            "label": "Every project (user)",
            "path": str(global_path),
            "relative_path": "",
            "exists": global_path.is_file(),
            "bytes": global_path.stat().st_size if global_path.is_file() else 0,
            # Outside the repository, so it is edited through this API rather
            # than the workspace file endpoints.
            "editable_in_editor": False,
        }
    )

    for name in names:
        candidate = root / name
        if candidate.is_file() or name == paths.INSTRUCTION_FILENAME:
            items.append(
                {
                    "scope": "repository",
                    "label": "This repository",
                    "path": str(candidate),
                    "relative_path": name,
                    "exists": candidate.is_file(),
                    "bytes": candidate.stat().st_size if candidate.is_file() else 0,
                    "editable_in_editor": True,
                }
            )
            if candidate.is_file():
                break

    # os.walk with in-place pruning: rglob would descend into node_modules and
    # .venv in full before anything could filter them out, which on a real
    # checkout means walking tens of thousands of paths to find a few files.
    found = 0
    for current, directories, files in os.walk(root):
        directories[:] = sorted(
            name for name in directories if name not in _SKIP_DIRS and not name.startswith(".")
        )
        directory = Path(current)
        if directory == root:
            continue  # the repository-level file is handled above
        if found >= _MAX_SCOPED_FILES:
            break
        for name in names:
            if name not in files:
                continue
            candidate = directory / name
            relative = candidate.relative_to(root).as_posix()
            items.append(
                {
                    "scope": "scoped",
                    "label": f"Files under {directory.relative_to(root).as_posix()}/",
                    "path": str(candidate),
                    "relative_path": relative,
                    "exists": True,
                    "bytes": candidate.stat().st_size,
                    "editable_in_editor": True,
                }
            )
            found += 1
            break
    return items


def _playbooks(root: Path) -> list[dict]:
    loader = PlaybookLoader(root)
    builtin_dir, project_dir = loader.directories
    items: list[dict] = []
    for playbook in loader.list():
        project_file = project_dir / f"{playbook.name}.yaml"
        builtin_file = builtin_dir / f"{playbook.name}.yaml"
        source = project_file if project_file.is_file() else builtin_file
        items.append(
            {
                "name": playbook.name,
                "version": playbook.version,
                "purpose": playbook.purpose,
                "stages": list(playbook.execution_stages),
                "allowed_tools": list(playbook.allowed_tools),
                "approval_points": list(playbook.approval_points),
                "builtin": not project_file.is_file(),
                "relative_path": (
                    source.relative_to(root).as_posix()
                    if source.is_file() and source.is_relative_to(root)
                    else ""
                ),
            }
        )
    return items


def _memory_counts(state: GuiState) -> dict:
    memory = state.missions.memory
    counts: dict[str, int] = {}
    for memory_type in MemoryType:
        try:
            counts[memory_type.value] = len(memory.list(memory_type=memory_type, limit=500))
        except Exception:  # noqa: BLE001 - a count is never worth failing the panel
            counts[memory_type.value] = 0
    return {
        "enabled": state.context.settings.memory.enabled,
        "counts": counts,
        "total": sum(counts.values()),
    }


def _match(item: object) -> dict:
    """Shape one memory record for the browser, without provider internals."""
    return {
        "id": item.id,  # type: ignore[attr-defined]
        "type": item.type.value,  # type: ignore[attr-defined]
        "scope": item.scope.value,  # type: ignore[attr-defined]
        "status": item.status.value,  # type: ignore[attr-defined]
        "content": item.content,  # type: ignore[attr-defined]
        "summary": item.summary,  # type: ignore[attr-defined]
        "source": item.source,  # type: ignore[attr-defined]
        "source_type": item.source_type,  # type: ignore[attr-defined]
        "confidence": item.confidence,  # type: ignore[attr-defined]
        "tags": list(item.tags),  # type: ignore[attr-defined]
        "why": list(item.why),  # type: ignore[attr-defined]
        "created_at": item.created_at.isoformat() if item.created_at else None,  # type: ignore[attr-defined]
    }


@router.get("/config")
def read_config(
    state: Annotated[GuiState, Depends(get_state)],
    session_id: str = Query(default=""),
) -> dict:
    """Everything the customization panel shows, in one round trip."""
    settings = state.context.settings
    resolved = session_id or state.missions.latest_session()
    try:
        mode = state.missions.interaction_mode(resolved).value
        verbose = state.missions.verbose_enabled(resolved)
    except Exception:  # noqa: BLE001 - an unknown session still gets defaults
        mode, verbose = InteractionMode.ASK.value, True
    try:
        effort = state.providers.session_effort(resolved)
    except Exception:  # noqa: BLE001 - no provider configured yet
        effort = "auto"

    return {
        "session_id": resolved,
        "autonomy": {
            "mode": mode,
            "options": [
                {"id": item.value, "label": item.value.title(), "hint": MODE_HINTS[item.value]}
                for item in InteractionMode
            ],
        },
        "effort": {"value": effort, "options": list(EFFORT_LEVELS)},
        "verbose": verbose,
        "roles": [
            {
                "role": role.value,
                "profile": settings.routing.get(role.value, ""),
            }
            for role in ModelRole
        ],
        "profiles": [name for name in settings.models],
        "instructions": {
            "files": _instruction_files(state.root),
            "max_bytes": MAX_INSTRUCTION_BYTES,
        },
        "playbooks": _playbooks(state.root),
        "memory": _memory_counts(state),
    }


@router.get("/extensions")
def read_extensions(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    """What the project extends the agent with, and what failed to load.

    The problems list matters more than the inventory. Everything here fails
    silently by design — a broken hook is skipped so it cannot block every edit,
    an unreachable MCP server is dropped so it cannot stop a session — and
    without somewhere to report that, "silently" would mean "invisibly".
    """
    missions = state.missions
    extensions = missions.extensions(reload=True)
    servers = missions.mcp_servers
    registry = missions._mcp  # noqa: SLF001 - reporting on it, not driving it
    connected = {status.name: status for status in (registry.statuses if registry else [])}
    return {
        "hooks": {
            "sources": [str(path) for path in missions.hooks.sources],
            "events": {
                event.value: [
                    {
                        "name": item.label,
                        "command": item.command,
                        "matcher": item.matcher,
                        "timeout": item.timeout,
                        "enabled": item.enabled,
                    }
                    for item in missions.hooks.hooks.for_event(event)
                ]
                for event in HookEvent
                if missions.hooks.hooks.for_event(event)
            },
            "problems": list(missions.hooks.problems),
        },
        "mcp": {
            "sources": [str(path) for path in servers.sources],
            "servers": [
                {
                    "name": name,
                    "transport": config.transport,
                    "target": config.command or config.url,
                    "enabled": config.enabled,
                    # Absent until the first turn connects them, which is why
                    # this is three states rather than a boolean.
                    "connected": (None if name not in connected else connected[name].connected),
                    "tool_count": connected[name].tool_count if name in connected else 0,
                    "error": connected[name].error if name in connected else "",
                }
                for name, config in sorted(servers.servers.items())
            ],
            "problems": list(servers.problems),
        },
        "skills": [
            {
                "name": skill.name,
                "description": skill.description,
                "scope": "global" if skill.global_scope else "project",
                "resources": list(skill.resources),
            }
            for skill in sorted(extensions.skills.values(), key=lambda item: item.name)
        ],
        "commands": [
            {
                "name": command.invocation,
                "description": command.description,
                "argument_hint": command.argument_hint,
                "scope": "global" if command.global_scope else "project",
            }
            for command in sorted(extensions.commands.values(), key=lambda item: item.name)
        ],
        "problems": list(extensions.problems),
    }


@router.post("/autonomy")
def set_autonomy(state: Annotated[GuiState, Depends(get_state)], body: AutonomyRequest) -> dict:
    """Set the session's autonomy policy — the browser's `/mode`."""
    try:
        state.missions.set_interaction_mode(body.session_id, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"session_id": body.session_id, "mode": body.mode, "hint": MODE_HINTS[body.mode]}


@router.post("/effort")
def set_effort(state: Annotated[GuiState, Depends(get_state)], body: EffortRequest) -> dict:
    """Set reasoning effort for the session's model profile — the browser's `/effort`."""
    try:
        profile, effort = state.providers.set_session_effort(body.session_id, body.effort)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"profile": profile, "effort": effort}


@router.post("/verbose")
def set_verbose(state: Annotated[GuiState, Depends(get_state)], body: VerboseRequest) -> dict:
    """Show or hide detailed live progress for this session — `/verbose`."""
    try:
        state.missions.set_verbose(body.session_id, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"session_id": body.session_id, "verbose": body.enabled}


@router.get("/instructions/global")
def read_global_instructions() -> dict:
    """The user-level DAINO.md, which lives outside any repository."""
    path = global_instruction_path()
    content = ""
    if path.is_file():
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": str(path), "exists": path.is_file(), "content": content}


@router.put("/instructions/global")
def write_global_instructions(body: InstructionBody) -> dict:
    path = global_instruction_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"path": str(path), "exists": True, "bytes": len(body.content.encode("utf-8"))}


@router.get("/instructions/effective")
def effective_instructions(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(default=""),
) -> dict:
    """What the agent would actually load for a path, layer by layer.

    Instructions are precedence layers, so "which file wins here?" is a real
    question — and the only honest answer is the resolver's own.
    """
    resolver = InstructionResolver(state.root)
    resolved = resolver.resolve([path] if path.strip() else None)
    return {
        "target": path,
        "text": resolved.text,
        "sources": list(resolved.sources),
        "scopes": {key: list(value) for key, value in resolved.scopes.items()},
    }


@router.get("/memory")
def list_memory(
    state: Annotated[GuiState, Depends(get_state)],
    q: str = Query(default=""),
    memory_type: str = Query(default="", alias="type"),
    scope: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    """Inspect durable memory — the browser's `/memory` and `/memory search`."""
    memory = state.missions.memory
    try:
        if q.strip():
            items = memory.search(q.strip(), include_stale=True, debug=True)[:limit]
        else:
            items = memory.list(
                memory_type=MemoryType(memory_type) if memory_type else None,
                scope=MemoryScope(scope) if scope else None,
                limit=limit,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"items": [_match(item) for item in items]}


@router.post("/memory")
def remember(state: Annotated[GuiState, Depends(get_state)], body: RememberRequest) -> dict:
    """Record a fact the user states themselves.

    ``source_type="user"`` is what makes it authoritative and pre-approved, the
    same classification the agent's own extraction cannot grant itself.
    """
    try:
        memory_id = state.missions.memory.remember(
            body.content,
            memory_type=MemoryType.USER,
            scope=MemoryScope(body.scope),
            summary=body.summary,
            source="browser",
            source_type="user",
            tags=body.tags,
            importance=0.8,
            confidence=0.9,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": memory_id}


@router.delete("/memory/{memory_id}")
def forget(state: Annotated[GuiState, Depends(get_state)], memory_id: str) -> dict:
    try:
        state.missions.memory.forget(memory_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": memory_id, "forgotten": True}


@router.post("/memory/{memory_id}/verify")
def verify(state: Annotated[GuiState, Depends(get_state)], memory_id: str) -> dict:
    """Re-check a memory against its current source, clearing a stale flag."""
    try:
        state.missions.memory.verify(memory_id)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"id": memory_id, "verified": True}


@router.post("/memory/clear")
def clear_memory(state: Annotated[GuiState, Depends(get_state)], body: ClearMemoryRequest) -> dict:
    scope = MemoryScope(body.scope)
    count = state.missions.memory.clear(
        scope=scope,
        session_id=body.session_id or None,
    )
    return {"scope": body.scope, "cleared": count}
