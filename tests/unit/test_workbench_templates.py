"""Work types: what ships, and how a project overrides it."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from daino.config import paths
from daino.workbench.templates import TemplateError, TemplateLoader

#: Every built-in, so adding one without wiring it up fails here.
BUILT_INS = {"general", "research", "prd", "analysis", "meeting-notes", "incident-review"}


def test_every_built_in_template_validates(tmp_path: Path) -> None:
    templates = TemplateLoader(tmp_path).list()

    assert {item.name for item in templates} == BUILT_INS
    assert all(item.title and item.purpose for item in templates)
    assert all(item.starter_tasks for item in templates)


def test_general_leads_the_list_because_it_is_the_default(tmp_path: Path) -> None:
    assert TemplateLoader(tmp_path).list()[0].name == "general"


def test_a_template_carries_a_preamble_the_agent_can_be_given(tmp_path: Path) -> None:
    """A work type is only useful if it changes how the agent behaves."""
    research = TemplateLoader(tmp_path).get("research")

    assert "footnote" in research.preamble
    assert [item.filename for item in research.starter_artifacts] == ["findings.md"]


def test_a_project_can_override_a_built_in_by_name(tmp_path: Path) -> None:
    directory = paths.state_dir(tmp_path, create=True) / "workbench-templates"
    directory.mkdir(parents=True)
    (directory / "research.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "research",
                "title": "House research",
                "purpose": "Our own way of doing it.",
                "starter_tasks": ["Read the brief"],
            }
        ),
        encoding="utf-8",
    )

    loader = TemplateLoader(tmp_path)

    assert loader.get("research").title == "House research"
    # Overriding replaces one template, not the set.
    assert {item.name for item in loader.list()} == BUILT_INS


def test_a_project_can_add_a_template_of_its_own(tmp_path: Path) -> None:
    directory = paths.state_dir(tmp_path, create=True) / "workbench-templates"
    directory.mkdir(parents=True)
    (directory / "rfc.yaml").write_text(
        yaml.safe_dump({"name": "rfc", "title": "RFC", "starter_tasks": ["Draft"]}),
        encoding="utf-8",
    )

    assert TemplateLoader(tmp_path).get("rfc").title == "RFC"


def test_a_malformed_template_is_reported_rather_than_skipped(tmp_path: Path) -> None:
    """Silently reverting to the built-in would hide the typo indefinitely."""
    directory = paths.state_dir(tmp_path, create=True) / "workbench-templates"
    directory.mkdir(parents=True)
    (directory / "broken.yaml").write_text("title: no name here\n", encoding="utf-8")

    with pytest.raises(TemplateError, match="broken.yaml"):
        TemplateLoader(tmp_path).list()


def test_an_unknown_name_falls_back_to_general(tmp_path: Path) -> None:
    """A deleted template must not strand the workspaces made from it."""
    resolved = TemplateLoader(tmp_path).get("no-such-thing")

    assert resolved.name == "general"
    assert resolved.starter_tasks
