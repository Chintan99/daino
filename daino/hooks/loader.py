"""Find and validate the hooks a project has configured.

Hooks live in their own file rather than inside ``config.yaml``, and that is a
security decision rather than a filing one. A hook command runs through a shell
with the user's full environment; ``config.yaml`` is an ordinary project file the
agent is free to edit. Keeping hooks in ``.daino/hooks.yaml`` puts them inside
the state directory, which ``EditTools`` refuses to write to — so the agent
cannot arm a hook, and a hook stays a thing the user configured about the agent
rather than a thing the agent configured about itself.

Two layers load, global first: ``~/.daino/hooks.yaml`` then the project's own.
Both run. A project cannot drop a hook its organisation set globally, which is
the only reason a global layer is worth having.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from daino.config import paths
from daino.hooks.models import HookEvent, HookSet

#: Filename inside the project state directory and the global memory directory.
HOOKS_FILENAME = "hooks.yaml"


@dataclass(frozen=True, slots=True)
class LoadedHooks:
    """The resolved hook set, plus anything wrong with how it was written."""

    hooks: HookSet = field(default_factory=HookSet)
    #: Human-readable problems: a malformed file, an unknown event, a matcher
    #: that will not compile. Surfaced rather than raised, so one bad hook does
    #: not stop a session from opening.
    problems: tuple[str, ...] = ()
    #: Files that were actually read, for diagnostics and for the settings panel.
    sources: tuple[Path, ...] = ()


def project_hooks_path(root: Path) -> Path:
    return paths.state_path(root, HOOKS_FILENAME)


def global_hooks_path() -> Path:
    return paths.global_memory_dir() / HOOKS_FILENAME


def load_hooks(root: Path) -> LoadedHooks:
    """Load global then project hooks, reporting rather than raising on error."""
    problems: list[str] = []
    sources: list[Path] = []
    merged = HookSet()
    for path in (global_hooks_path(), project_hooks_path(root)):
        if not path.is_file():
            continue
        loaded, issues = _read(path)
        problems.extend(issues)
        if loaded is not None:
            merged = merged.merged_with(loaded)
            sources.append(path)
    problems.extend(_matcher_problems(merged))
    return LoadedHooks(hooks=merged, problems=tuple(problems), sources=tuple(sources))


def _read(path: Path) -> tuple[HookSet | None, list[str]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return None, [f"{path}: could not be read ({exc})"]
    if raw is None:
        return HookSet(), []
    if not isinstance(raw, dict):
        return None, [f"{path}: expected a mapping of event name to hook list"]
    # A hooks file may nest under a top-level ``hooks:`` key, which is what
    # someone who has seen the config.yaml layout will write first.
    if "hooks" in raw and isinstance(raw["hooks"], dict):
        raw = raw["hooks"]
    known = {event.value for event in HookEvent}
    unknown = sorted(set(raw) - known)
    problems = [
        f"{path}: unknown hook event {name!r}; expected one of {', '.join(sorted(known))}"
        for name in unknown
    ]
    try:
        return HookSet.model_validate({key: raw[key] for key in raw if key in known}), problems
    except ValidationError as exc:
        problems.append(f"{path}: {_first_error(exc)}")
        return None, problems


def _matcher_problems(hooks: HookSet) -> list[str]:
    """Report matchers that will never compile, instead of failing per action."""
    problems: list[str] = []
    for event in HookEvent:
        for definition in hooks.for_event(event):
            if not definition.matcher:
                continue
            try:
                re.compile(definition.matcher)
            except re.error as exc:
                problems.append(
                    f"{event.value} hook {definition.label!r}: matcher "
                    f"{definition.matcher!r} is not a valid regular expression ({exc}); "
                    "this hook will never run"
                )
    return problems


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"]) or "hooks"
    return f"{location}: {error['msg']}"
