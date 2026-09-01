"""Work types: what a new workspace starts with, and how the agent behaves in it.

Mirrors :class:`daino.playbooks.PlaybookLoader`'s two-directory override —
built-ins ship with the package, and a project can shadow any of them by name
from its own state directory. A malformed template raises rather than being
skipped, so a typo is visible immediately instead of silently reverting to the
built-in.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from daino.config import paths
from daino.workbench.models import WorkspaceTemplate

#: Last resort when even the built-in "general" template is missing — an
#: install so broken that refusing to open a workspace would help nobody.
FALLBACK = WorkspaceTemplate(
    name="general",
    title="General",
    purpose="Open-ended work with no fixed shape.",
)


class TemplateError(ValueError):
    """Raised when a template file cannot be read or does not validate."""


class TemplateLoader:
    def __init__(self, project_root: Path) -> None:
        self.directories = [
            Path(__file__).parent / "templates",
            paths.state_dir(project_root) / "workbench-templates",
        ]

    def list(self) -> list[WorkspaceTemplate]:
        """Every template, with later directories overriding earlier by name."""
        items: dict[str, WorkspaceTemplate] = {}
        for directory in self.directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.yaml")):
                items[_load(path).name] = _load(path)
        ordered = sorted(items.values(), key=lambda item: item.name)
        # "general" is the default, so it leads the picker regardless of name.
        return sorted(ordered, key=lambda item: item.name != "general")

    def get(self, name: str) -> WorkspaceTemplate:
        """Return one template, falling back rather than failing.

        A workspace stores its template name, so a template that has been
        deleted or renamed must not make the workspace unopenable — the goal,
        tasks and documents are all still there.
        """
        available = self.list()
        for candidate in (name, FALLBACK.name):
            found = next((item for item in available if item.name == candidate), None)
            if found is not None:
                return found
        return FALLBACK


def _load(path: Path) -> WorkspaceTemplate:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TemplateError(f"{path.name} could not be read: {exc}") from exc
    try:
        return WorkspaceTemplate.model_validate(data)
    except ValidationError as exc:
        raise TemplateError(f"{path.name} is not a valid workspace template: {exc}") from exc
