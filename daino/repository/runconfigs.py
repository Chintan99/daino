"""Run configurations and tasks: the commands a project already knows about.

Rather than inventing a launch-configuration format nobody has written yet,
these are *discovered* from what the project already declares — npm scripts,
Makefile targets, pyproject entry points, docker-compose services, justfile
recipes. A project's `package.json` scripts are its run configurations; asking
someone to restate them in a Daino-specific file would be asking them to keep
two lists in step.

The user's own additions live in ``.daino/tasks.json``, which is read after the
discovered ones and can override any of them by id. That file is the escape
hatch for the command that is genuinely project-specific, not the primary
mechanism.

Nothing here executes anything: a run config is a command plus a working
directory, and the terminal service runs it. That separation is deliberate —
these commands come from files in the repository, and treating them as data
right up to the moment a person presses Run keeps the trust boundary visible.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daino.config import paths

#: Where a project's own extra tasks live.
TASKS_FILE = "tasks.json"

#: Makefile targets that are conventions rather than things to run.
_MAKE_SKIP = frozenset({".PHONY", ".DEFAULT", ".SUFFIXES", ".PRECIOUS"})
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][\w.\-/]*)\s*:(?!=)")
_JUST_RECIPE = re.compile(r"^([a-zA-Z0-9][\w-]*)(?:\s+[^:]*)?:(?!=)")


@dataclass(frozen=True, slots=True)
class RunConfig:
    """One runnable command, and where it came from."""

    id: str
    label: str
    command: str
    #: "npm", "make", "just", "compose", "python", "user" — shown so a reader
    #: knows which file to edit to change it.
    source: str
    #: Relative to the project root. Empty means the root itself.
    cwd: str = ""
    #: What this is for, when the source says: an npm script's own name, a
    #: compose service's image.
    detail: str = ""
    #: Categorised so the UI can group: "run", "build", "test", "lint", "other".
    kind: str = "other"


def _package_json(root: Path) -> dict[str, Any]:
    path = root / "package.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _classify(name: str) -> str:
    """Group a command by what it is for, from its name.

    Name-based because that is the only signal available, and conventional
    enough to be right nearly always: `dev`, `start`, `serve` are things you
    run; `test`, `lint`, `typecheck` are things you check with.
    """
    lowered = name.casefold()
    if any(word in lowered for word in ("test", "spec", "e2e", "check:test")):
        return "test"
    if any(word in lowered for word in ("lint", "format", "fmt", "typecheck", "types")):
        return "lint"
    if any(word in lowered for word in ("build", "compile", "bundle", "dist")):
        return "build"
    if any(word in lowered for word in ("dev", "start", "serve", "watch", "run")):
        return "run"
    return "other"


def _node_manager(root: Path) -> str:
    """The package manager this project actually uses, from its lockfile.

    Running `npm run dev` in a pnpm project works often enough to be tempting
    and fails confusingly when it does not, so the lockfile decides.
    """
    for lockfile, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lockb", "bun"),
        ("package-lock.json", "npm"),
    ):
        if (root / lockfile).is_file():
            return manager
    return "npm"


def _npm_configs(root: Path) -> list[RunConfig]:
    package = _package_json(root)
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return []
    manager = _node_manager(root)
    # yarn and bun take `yarn dev`; npm and pnpm want `run`.
    prefix = f"{manager} " if manager in {"yarn", "bun"} else f"{manager} run "
    return [
        RunConfig(
            id=f"npm:{name}",
            label=name,
            command=f"{prefix}{name}",
            source=manager,
            detail=str(body)[:200],
            kind=_classify(name),
        )
        for name, body in sorted(scripts.items())
        if isinstance(name, str) and name
    ]


def _make_configs(root: Path) -> list[RunConfig]:
    for candidate in ("Makefile", "makefile", "GNUmakefile"):
        path = root / candidate
        if path.is_file():
            break
    else:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[RunConfig] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if line.startswith(("\t", " ")):
            continue  # a recipe body, not a target
        match = _MAKE_TARGET.match(line)
        if match is None:
            continue
        target = match.group(1)
        if target in _MAKE_SKIP or target in seen or "$" in target:
            continue
        seen.add(target)
        found.append(
            RunConfig(
                id=f"make:{target}",
                label=target,
                command=f"make {target}",
                source="make",
                kind=_classify(target),
            )
        )
    return found


def _just_configs(root: Path) -> list[RunConfig]:
    for candidate in ("justfile", "Justfile", ".justfile"):
        path = root / candidate
        if path.is_file():
            break
    else:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found: list[RunConfig] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if line.startswith((" ", "\t", "#")) or not line.strip():
            continue
        match = _JUST_RECIPE.match(line)
        if match is None:
            continue
        recipe = match.group(1)
        if recipe in seen:
            continue
        seen.add(recipe)
        found.append(
            RunConfig(
                id=f"just:{recipe}",
                label=recipe,
                command=f"just {recipe}",
                source="just",
                kind=_classify(recipe),
            )
        )
    return found


def _compose_configs(root: Path) -> list[RunConfig]:
    for candidate in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
        path = root / candidate
        if path.is_file():
            break
    else:
        return []
    # Parsed with a deliberately shallow reader rather than a YAML dependency:
    # all that is wanted is the service names, which are the keys indented one
    # level under `services:`.
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found: list[RunConfig] = []
    inside = False
    indent = 0
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not inside:
            if stripped.rstrip(":") == "services" and stripped.endswith(":"):
                inside = True
                indent = len(line) - len(line.lstrip())
            continue
        current = len(line) - len(line.lstrip())
        if current <= indent:
            break  # left the services block
        if current == indent + 2 and stripped.endswith(":"):
            service = stripped.rstrip(":").strip("'\"")
            if service:
                found.append(
                    RunConfig(
                        id=f"compose:{service}",
                        label=service,
                        command=f"docker compose up {service}",
                        source="compose",
                        detail=f"from {path.name}",
                        kind="run",
                    )
                )
    return found


def _python_configs(root: Path) -> list[RunConfig]:
    """Entry points and the obvious module invocation for a Python project."""
    found: list[RunConfig] = []
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return found
    try:
        import tomllib  # noqa: PLC0415 - stdlib, only needed here

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return found
    scripts = ((data.get("project") or {}).get("scripts")) or {}
    interpreter = _python_executable(root)
    for name, target in sorted(scripts.items()):
        if not isinstance(name, str):
            continue
        binary = root / ".venv" / "bin" / name
        found.append(
            RunConfig(
                id=f"python:{name}",
                label=name,
                command=str(binary) if binary.is_file() else name,
                source="python",
                detail=str(target),
                kind=_classify(name),
            )
        )
    if not found:
        package = (data.get("project") or {}).get("name")
        if isinstance(package, str) and (root / package.replace("-", "_")).is_dir():
            module = package.replace("-", "_")
            found.append(
                RunConfig(
                    id=f"python:{module}",
                    label=module,
                    command=f"{interpreter} -m {module}",
                    source="python",
                    kind="run",
                )
            )
    return found


def _python_executable(root: Path) -> str:
    """The project's interpreter, never the bare name ``python``."""
    import sys  # noqa: PLC0415

    for relative in (Path(".venv/bin/python"), Path(".venv/Scripts/python.exe")):
        if (root / relative).is_file():
            return str(root / relative)
    return sys.executable or "python3"


def _cargo_configs(root: Path) -> list[RunConfig]:
    if not (root / "Cargo.toml").is_file() or not shutil.which("cargo"):
        return []
    return [
        RunConfig(
            id="cargo:run",
            label="cargo run",
            command="cargo run",
            source="cargo",
            kind="run",
        ),
        RunConfig(
            id="cargo:build",
            label="cargo build",
            command="cargo build",
            source="cargo",
            kind="build",
        ),
    ]


def _go_configs(root: Path) -> list[RunConfig]:
    if not (root / "go.mod").is_file() or not shutil.which("go"):
        return []
    return [
        RunConfig(id="go:run", label="go run .", command="go run .", source="go", kind="run"),
        RunConfig(
            id="go:build",
            label="go build ./...",
            command="go build ./...",
            source="go",
            kind="build",
        ),
    ]


def user_tasks(root: Path) -> list[RunConfig]:
    """Commands the user added in ``.daino/tasks.json``.

    Read last so a hand-written entry can override a discovered one by reusing
    its id — which is the point: the discovered command is a good default until
    it is not.
    """
    path = paths.state_dir(root, create=False) / TASKS_FILE
    if not path.is_file():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = loaded.get("tasks") if isinstance(loaded, dict) else loaded
    if not isinstance(entries, list):
        return []
    found: list[RunConfig] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        command = str(entry.get("command") or "").strip()
        if not command:
            continue
        label = str(entry.get("label") or command)[:120]
        found.append(
            RunConfig(
                id=str(entry.get("id") or f"user:{label}"),
                label=label,
                command=command,
                source="user",
                cwd=str(entry.get("cwd") or ""),
                detail=str(entry.get("detail") or ""),
                kind=str(entry.get("kind") or _classify(label)),
            )
        )
    return found


def discover(root: Path) -> list[RunConfig]:
    """Every runnable command this project declares, user overrides last."""
    discovered: list[RunConfig] = [
        *_npm_configs(root),
        *_python_configs(root),
        *_make_configs(root),
        *_just_configs(root),
        *_compose_configs(root),
        *_cargo_configs(root),
        *_go_configs(root),
    ]
    merged: dict[str, RunConfig] = {item.id: item for item in discovered}
    for item in user_tasks(root):
        merged[item.id] = item
    order = {"run": 0, "build": 1, "test": 2, "lint": 3, "other": 4}
    return sorted(
        merged.values(), key=lambda item: (order.get(item.kind, 9), item.source, item.label)
    )


def by_id(root: Path, identifier: str) -> RunConfig | None:
    return next((item for item in discover(root) if item.id == identifier), None)


def save_user_tasks(root: Path, tasks: list[dict[str, Any]]) -> Path:
    """Write ``.daino/tasks.json``. Returns where it went."""
    directory = paths.state_dir(root, create=True)
    path = directory / TASKS_FILE
    path.write_text(json.dumps({"tasks": tasks}, indent=2) + "\n", encoding="utf-8")
    return path
