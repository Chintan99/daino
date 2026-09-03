"""Read ``.daino/mcp.json``, the file everyone already has.

The format is the one every MCP client uses — a top-level ``mcpServers`` object
keyed by server name — so a configuration copied from another tool works here
unchanged. That compatibility is the whole point: nobody wants to write their
Postgres server definition a second time in a bespoke schema.

Placement follows the hooks file for the same reason. A stdio server is a process
Daino launches with the user's environment, so the file that defines it lives in
the state directory, which ``EditTools`` refuses to write to. An agent that could
add an MCP server could launch an arbitrary process on its next action.

Two layers load, global then project. Both are merged, with the project winning a
name collision — an organisation's shared server list is a floor, and a project
overriding one entry of it should not have to restate the rest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from daino.config import paths
from daino.mcp.models import MCPServerConfig

#: Filename in the project state directory and the global memory directory.
MCP_FILENAME = "mcp.json"

#: Keys accepted at the top level. ``mcpServers`` is the near-universal spelling;
#: ``servers`` is accepted because it is the obvious guess.
_ROOT_KEYS = ("mcpServers", "servers")


@dataclass(frozen=True, slots=True)
class LoadedServers:
    servers: dict[str, MCPServerConfig] = field(default_factory=dict)
    #: Problems that did not stop the rest of the file loading.
    problems: tuple[str, ...] = ()
    sources: tuple[Path, ...] = ()


def project_mcp_path(root: Path) -> Path:
    return paths.state_path(root, MCP_FILENAME)


def global_mcp_path() -> Path:
    return paths.global_memory_dir() / MCP_FILENAME


def load_servers(root: Path) -> LoadedServers:
    """Load global then project MCP configuration, reporting rather than raising."""
    servers: dict[str, MCPServerConfig] = {}
    problems: list[str] = []
    sources: list[Path] = []
    for path in (global_mcp_path(), project_mcp_path(root)):
        if not path.is_file():
            continue
        parsed, issues = _read(path)
        problems.extend(issues)
        if parsed:
            servers.update(parsed)
            sources.append(path)
    return LoadedServers(servers=servers, problems=tuple(problems), sources=tuple(sources))


def _read(path: Path) -> tuple[dict[str, MCPServerConfig], list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{path}: could not be read ({exc})"]
    if not isinstance(raw, dict):
        return {}, [f"{path}: expected a JSON object"]
    body = next(
        (raw[key] for key in _ROOT_KEYS if isinstance(raw.get(key), dict)),
        None,
    )
    if body is None:
        # A bare mapping of name -> definition is also accepted, since that is
        # what someone writing the file from scratch tends to produce.
        body = raw if all(isinstance(value, dict) for value in raw.values()) else None
    if body is None:
        return {}, [f"{path}: expected an 'mcpServers' object"]
    servers: dict[str, MCPServerConfig] = {}
    problems: list[str] = []
    for name, definition in body.items():
        if not isinstance(definition, dict):
            problems.append(f"{path}: server {name!r} is not an object")
            continue
        try:
            servers[str(name)] = _coerce(definition)
        except (ValidationError, ValueError) as exc:
            problems.append(f"{path}: server {name!r} is not usable ({_summarize(exc)})")
    return servers, problems


def _coerce(definition: dict[str, object]) -> MCPServerConfig:
    """Normalise the shapes other clients write into this one's model.

    The ecosystem's files omit ``transport`` and let the presence of ``command``
    or ``url`` imply it, and some spell the HTTP transport ``sse`` or
    ``streamable-http``. Rejecting those would defeat the point of using the
    common filename.
    """
    payload = dict(definition)
    declared = str(payload.get("transport") or payload.get("type") or "").casefold()
    payload.pop("type", None)
    if declared in {"http", "sse", "streamable-http", "streamablehttp"}:
        payload["transport"] = "http"
    elif declared == "stdio":
        payload["transport"] = "stdio"
    else:
        payload["transport"] = "http" if payload.get("url") else "stdio"
    payload["env"] = _resolved_env(payload.get("env"))
    return MCPServerConfig.model_validate(payload)


def _resolved_env(raw: object) -> dict[str, str]:
    """Resolve ``env://``-style references so a token never sits in the file.

    A literal value is passed through: the ecosystem's files routinely contain
    plain strings, and refusing them would make a copied configuration fail for
    a reason the user did not ask about. What this adds is the *option* of a
    reference, which is what a checked-in file should use.
    """
    if not isinstance(raw, dict):
        return {}
    from daino.security.secrets import resolve_secret

    resolved: dict[str, str] = {}
    for key, value in raw.items():
        text = str(value)
        if text.startswith(("env://", "keyring://", "file://")):
            with_fallback = resolve_secret(text)
            resolved[str(key)] = with_fallback
        else:
            resolved[str(key)] = text
    return resolved


def _summarize(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        error = exc.errors()[0]
        location = ".".join(str(part) for part in error["loc"]) or "server"
        return f"{location}: {error['msg']}"
    return str(exc)
