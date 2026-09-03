"""Discover commands and skills from the project and the user's home.

Layout, chosen to match what people already have from other agents:

    .daino/commands/review-pr.md        ->  /review-pr
    .daino/commands/db/migrate.md       ->  /db:migrate
    .daino/skills/api-conventions/SKILL.md
    ~/.daino/commands/...               ->  the same, for every project

Project definitions win a name collision, because a project that has written its
own ``/review`` meant that one.

Unlike hooks and MCP servers, these files are *not* protected from the agent.
That is deliberate and safe: a command or a skill is text that becomes part of a
prompt. It cannot start a process or reach the network. An agent that writes a
skill has written itself a note, which is a feature — "remember how we do
migrations here" is a reasonable thing to ask for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from daino.config import paths
from daino.skills.models import Skill, SlashCommand

COMMANDS_DIR = "commands"
SKILLS_DIR = "skills"
SKILL_FILENAME = "SKILL.md"

#: Namespace separator for a command in a subdirectory. A colon rather than a
#: slash so the name is one token to a completion list and to ``partition(" ")``.
NAMESPACE_SEPARATOR = ":"

#: Ceiling on one definition. A skill body is prompt text; a megabyte of it is a
#: mistake that would silently eat the window rather than a large skill.
MAX_DEFINITION_BYTES = 256_000


@dataclass(frozen=True, slots=True)
class LoadedExtensions:
    commands: dict[str, SlashCommand] = field(default_factory=dict)
    skills: dict[str, Skill] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.commands and not self.skills


def project_commands_dir(root: Path) -> Path:
    return paths.state_path(root, COMMANDS_DIR)


def project_skills_dir(root: Path) -> Path:
    return paths.state_path(root, SKILLS_DIR)


def global_commands_dir() -> Path:
    return paths.global_memory_dir() / COMMANDS_DIR


def global_skills_dir() -> Path:
    return paths.global_memory_dir() / SKILLS_DIR


def load_extensions(root: Path) -> LoadedExtensions:
    """Load global then project definitions, with the project winning collisions."""
    commands: dict[str, SlashCommand] = {}
    skills: dict[str, Skill] = {}
    problems: list[str] = []
    for directory, is_global in (
        (global_commands_dir(), True),
        (project_commands_dir(root), False),
    ):
        found, issues = _load_commands(directory, is_global)
        commands.update(found)
        problems.extend(issues)
    for directory, is_global in (
        (global_skills_dir(), True),
        (project_skills_dir(root), False),
    ):
        found_skills, issues = _load_skills(directory, is_global)
        skills.update(found_skills)
        problems.extend(issues)
    return LoadedExtensions(commands=commands, skills=skills, problems=tuple(problems))


def _load_commands(
    directory: Path, is_global: bool
) -> tuple[dict[str, SlashCommand], list[str]]:
    if not directory.is_dir():
        return {}, []
    commands: dict[str, SlashCommand] = {}
    problems: list[str] = []
    for path in sorted(directory.rglob("*.md")):
        if not path.is_file():
            continue
        name = _command_name(path, directory)
        if not name:
            continue
        body, metadata, issue = _read_definition(path)
        if issue:
            problems.append(issue)
            continue
        if not body.strip():
            problems.append(f"{path}: has no prompt text")
            continue
        commands[name] = SlashCommand(
            name=name,
            body=body,
            description=str(metadata.get("description") or "").strip(),
            argument_hint=str(
                metadata.get("argument-hint") or metadata.get("argument_hint") or ""
            ).strip(),
            source=path,
            global_scope=is_global,
        )
    return commands, problems


def _command_name(path: Path, directory: Path) -> str:
    relative = path.relative_to(directory).with_suffix("")
    parts = [part for part in relative.parts if part not in {".", ""}]
    return NAMESPACE_SEPARATOR.join(parts)


def _load_skills(directory: Path, is_global: bool) -> tuple[dict[str, Skill], list[str]]:
    if not directory.is_dir():
        return {}, []
    skills: dict[str, Skill] = {}
    problems: list[str] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        manifest = child / SKILL_FILENAME
        if not manifest.is_file():
            problems.append(f"{child}: a skill directory needs a {SKILL_FILENAME}")
            continue
        body, metadata, issue = _read_definition(manifest)
        if issue:
            problems.append(issue)
            continue
        name = str(metadata.get("name") or child.name).strip()
        description = str(metadata.get("description") or "").strip()
        if not description:
            # Without one the model has no basis for choosing the skill, and an
            # unchosen skill is dead weight in the prompt. Better to say so.
            problems.append(
                f"{manifest}: needs a 'description' in its frontmatter saying when to use it"
            )
            continue
        skills[name] = Skill(
            name=name,
            description=description,
            body=body.strip(),
            directory=child,
            global_scope=is_global,
            resources=tuple(
                sorted(
                    item.name
                    for item in child.iterdir()
                    if item.is_file() and item.name != SKILL_FILENAME
                )
            ),
        )
    return skills, problems


def _read_definition(path: Path) -> tuple[str, dict[str, object], str]:
    """Return ``(body, frontmatter, problem)`` for one markdown definition."""
    try:
        if path.stat().st_size > MAX_DEFINITION_BYTES:
            return "", {}, f"{path}: larger than {MAX_DEFINITION_BYTES:,} bytes"
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return "", {}, f"{path}: could not be read ({exc})"
    body, metadata = split_frontmatter(text)
    return body, metadata, ""


def split_frontmatter(text: str) -> tuple[str, dict[str, object]]:
    """Separate optional YAML frontmatter from the markdown body.

    Frontmatter is optional throughout. A command file that is nothing but a
    paragraph of instructions is a perfectly good command, and requiring a header
    on it would put a schema in front of the simplest useful case.
    """
    if not text.startswith("---"):
        return text, {}
    lines = text.splitlines()
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            header = "\n".join(lines[1:index])
            body = "\n".join(lines[index + 1 :])
            try:
                parsed = yaml.safe_load(header) or {}
            except yaml.YAMLError:
                # A malformed header is treated as content rather than dropped:
                # the body is what matters, and refusing the whole file over a
                # stray colon would be the wrong trade.
                return text, {}
            return (body, parsed) if isinstance(parsed, dict) else (body, {})
    return text, {}
