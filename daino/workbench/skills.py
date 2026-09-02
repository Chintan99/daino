"""Skills: how to do a kind of work, as opposed to what to start with.

A :class:`~daino.workbench.models.WorkspaceTemplate` shapes a *new workspace* —
starter steps, starter documents, a standing preamble. A skill shapes *one piece
of work*, wherever it happens: how a competent person approaches a competitive
analysis, what a PRD has to contain, which checks an incident review is not
finished without. A workspace created from the "general" template can still need
the PRD Writer skill for one step of its plan.

The loader mirrors :class:`~daino.workbench.templates.TemplateLoader` exactly —
built-ins ship with the package, a project shadows any of them by name from its
own state directory, and a malformed file raises rather than silently reverting
to the built-in.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError

from daino.config import paths


class Skill(BaseModel):
    """One reusable way of working."""

    name: str
    title: str
    description: str = ""
    #: What the agent should do, in the second person. The body of the skill.
    instructions: str = ""
    #: Tools this kind of work leans on, named so the model is reminded they
    #: exist. Advisory: the tool surface itself is unchanged by a skill.
    preferred_tools: list[str] = Field(default_factory=list)
    #: What finished work looks like, e.g. "a markdown document with sources".
    expected_artifacts: list[str] = Field(default_factory=list)
    #: Questions to answer before calling it done.
    checklist: list[str] = Field(default_factory=list)
    #: Words that suggest this skill. Matched case-insensitively against the
    #: goal; deliberately simple, because a wrong guess is cheap and visible.
    triggers: list[str] = Field(default_factory=list)
    #: Workspace template names this skill is the natural fit for.
    kinds: list[str] = Field(default_factory=list)

    def as_prompt(self) -> str:
        """The skill as a block appended to one turn's instruction."""
        parts = [f"Approach — {self.title}:", self.instructions.strip()]
        if self.preferred_tools:
            parts.append("Lean on: " + ", ".join(self.preferred_tools) + ".")
        if self.expected_artifacts:
            parts.append("Finished work looks like: " + "; ".join(self.expected_artifacts) + ".")
        if self.checklist:
            checks = "\n".join(f"- {item}" for item in self.checklist)
            parts.append(f"Before calling it done, check:\n{checks}")
        return "\n\n".join(part for part in parts if part.strip())


class SkillError(ValueError):
    """Raised when a skill file cannot be read or does not validate."""


class SkillLoader:
    """Every skill available to this project, built-ins first."""

    def __init__(self, project_root: Path) -> None:
        self.directories = [
            Path(__file__).parent / "skills",
            paths.state_dir(project_root) / "workbench-skills",
        ]

    def list(self) -> list[Skill]:
        items: dict[str, Skill] = {}
        for directory in self.directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.yaml")):
                skill = _load(path)
                items[skill.name] = skill
        return sorted(items.values(), key=lambda item: item.title)

    def get(self, name: str) -> Skill | None:
        """One skill by name, or None — a missing skill is never fatal."""
        if not name:
            return None
        return next((item for item in self.list() if item.name == name), None)

    def select(self, goal: str, kind: str = "") -> str:
        """Pick the skill this goal reads like, or nothing.

        Keyword matching rather than a model call: choosing a skill must not
        cost a round trip before the run has even started, and the choice is
        shown in the UI where a person can override it. Nothing is a perfectly
        good answer — a run with no skill behaves exactly as it did before
        skills existed.
        """
        text = f" {goal.casefold()} "
        best: tuple[int, str] = (0, "")
        for skill in self.list():
            # What the goal says outweighs what the workspace was created from:
            # a research workspace can still need a step written as a PRD, and
            # "general" says nothing at all.
            score = 2 * sum(1 for word in skill.triggers if _mentions(text, word))
            if kind and kind != "general" and kind in skill.kinds:
                score += 1
            if score > best[0]:
                best = (score, skill.name)
        return best[1]


def _mentions(haystack: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word.casefold())}\b", haystack) is not None


def _load(path: Path) -> Skill:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SkillError(f"{path.name} could not be read: {exc}") from exc
    try:
        return Skill.model_validate(data)
    except ValidationError as exc:
        raise SkillError(f"{path.name} is not a valid skill: {exc}") from exc
