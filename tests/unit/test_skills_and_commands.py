"""Lightweight prompt extensions: discovery, expansion, and progressive loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from daino.schemas import AgentAction
from daino.skills import (
    SlashCommand,
    load_extensions,
    project_commands_dir,
    project_skills_dir,
    split_frontmatter,
)
from daino.tools import ActionExecutor, EditTools


def write_command(root: Path, name: str, body: str) -> Path:
    path = project_commands_dir(root) / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def write_skill(root: Path, name: str, body: str) -> Path:
    path = project_skills_dir(root) / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_a_bare_markdown_file_is_a_valid_command(tmp_path: Path) -> None:
    """No frontmatter required: the simplest useful case must need no schema."""
    write_command(tmp_path, "standup", "Summarise what changed in the last day.")
    loaded = load_extensions(tmp_path)
    assert not loaded.problems
    assert loaded.commands["standup"].body.strip().startswith("Summarise")
    assert loaded.commands["standup"].invocation == "/standup"


def test_frontmatter_supplies_the_description_and_hint(tmp_path: Path) -> None:
    write_command(
        tmp_path,
        "review-pr",
        "---\ndescription: Review a pull request our way\nargument-hint: <pr-number>\n---\n"
        "Review PR $ARGUMENTS against our checklist.",
    )
    command = load_extensions(tmp_path).commands["review-pr"]
    assert command.description == "Review a pull request our way"
    assert command.argument_hint == "<pr-number>"
    assert command.expand("481") == "Review PR 481 against our checklist."


def test_positional_arguments_substitute(tmp_path: Path) -> None:
    write_command(tmp_path, "compare", "Compare $1 with $2 and report the differences.")
    command = load_extensions(tmp_path).commands["compare"]
    assert command.expand("alpha beta") == "Compare alpha with beta and report the differences."


def test_a_missing_positional_leaves_no_placeholder(tmp_path: Path) -> None:
    write_command(tmp_path, "compare", "Compare $1 with $2.")
    command = load_extensions(tmp_path).commands["compare"]
    assert command.expand("alpha") == "Compare alpha with ."


def test_arguments_are_appended_when_the_template_ignores_them() -> None:
    """Dropping what the user typed is the worse failure."""
    command = SlashCommand(name="audit", body="Audit the codebase for dead code.")
    assert command.expand("focus on the API layer").endswith("focus on the API layer")


def test_a_subdirectory_becomes_a_namespaced_name(tmp_path: Path) -> None:
    path = project_commands_dir(tmp_path) / "db" / "migrate.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Write a migration for $ARGUMENTS.", encoding="utf-8")
    assert "db:migrate" in load_extensions(tmp_path).commands


def test_an_empty_command_file_is_reported(tmp_path: Path) -> None:
    write_command(tmp_path, "blank", "---\ndescription: nothing\n---\n")
    loaded = load_extensions(tmp_path)
    assert any("no prompt text" in item for item in loaded.problems)
    assert "blank" not in loaded.commands


def test_a_skill_needs_a_description_to_be_choosable(tmp_path: Path) -> None:
    """Without one the model has no basis for picking it, so it is dead weight."""
    write_skill(tmp_path, "silent", "Some instructions with no frontmatter.")
    loaded = load_extensions(tmp_path)
    assert any("description" in item for item in loaded.problems)
    assert not loaded.skills


def test_a_skill_lists_its_bundled_files_without_inlining_them(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "api-conventions",
        "---\nname: api-conventions\ndescription: Use when adding an HTTP endpoint\n---\n"
        "Endpoints go in routes/, one router per resource.",
    )
    (project_skills_dir(tmp_path) / "api-conventions" / "checklist.md").write_text(
        "A" * 5_000, encoding="utf-8"
    )
    skill = load_extensions(tmp_path).skills["api-conventions"]
    assert skill.resources == ("checklist.md",)
    rendered = skill.render()
    assert "one router per resource" in rendered
    # The bundled file is pointed at, not pasted in.
    assert "AAAA" not in rendered
    assert "checklist.md" in rendered
    assert skill.summary_line() == "- api-conventions: Use when adding an HTTP endpoint"


@pytest.mark.asyncio
async def test_the_skill_tool_returns_the_body_on_request(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "migrations",
        "---\ndescription: Use when changing the database schema\n---\nAlways add a down step.",
    )
    skills = load_extensions(tmp_path).skills
    executor = ActionExecutor(EditTools(tmp_path), skills=skills)
    result, _ = await executor.execute(
        AgentAction(thought="check practice", action="skill", skill_name="migrations")
    )
    assert result.success
    assert "Always add a down step." in result.data["instructions"]


@pytest.mark.asyncio
async def test_a_wrong_skill_name_is_answered_with_the_real_list(tmp_path: Path) -> None:
    write_skill(tmp_path, "migrations", "---\ndescription: schema changes\n---\nAdd a down step.")
    executor = ActionExecutor(EditTools(tmp_path), skills=load_extensions(tmp_path).skills)
    result, _ = await executor.execute(
        AgentAction(thought="t", action="skill", skill_name="migration")
    )
    assert not result.success
    assert "migrations" in (result.error or "")


@pytest.mark.asyncio
async def test_the_skill_tool_says_so_when_a_project_has_none(tmp_path: Path) -> None:
    executor = ActionExecutor(EditTools(tmp_path))
    result, _ = await executor.execute(
        AgentAction(thought="t", action="skill", skill_name="anything")
    )
    assert not result.success
    assert "no skills" in (result.error or "")


def test_frontmatter_parsing_tolerates_a_malformed_header() -> None:
    """A stray colon must not throw away the instructions underneath it."""
    body, metadata = split_frontmatter("---\ndescription: a: b: c\n\t- bad\n---\nThe body.")
    assert metadata == {}
    assert "The body." in body


def test_a_file_without_frontmatter_is_all_body() -> None:
    body, metadata = split_frontmatter("Just instructions.\n")
    assert metadata == {}
    assert body == "Just instructions.\n"
