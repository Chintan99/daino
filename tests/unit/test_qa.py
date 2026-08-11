from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vasuki.agents.team import validate_team_plan
from vasuki.agents.tool_schemas import QA_TOOL_SPECS
from vasuki.application.qa_service import (
    _dependency_summary,
    discover_checks,
    inspect_project,
    specialist_plan,
)
from vasuki.schemas import QAAgentAction


def _tool_names() -> set[str]:
    return {str(spec["function"]["name"]) for spec in QA_TOOL_SPECS}


def test_qa_tool_surface_is_read_only() -> None:
    assert _tool_names() == {
        "read_file",
        "search_text",
        "glob",
        "grep",
        "list_directory",
        "finish",
    }
    with pytest.raises(ValidationError):
        QAAgentAction(
            thought="change the target",
            action="write",
            path="app.py",
            content="unsafe",
        )


def test_project_detection_and_checks_cover_full_stack_playwright_and_audits(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "lint": "eslint .",
                    "test": "vitest run",
                    "build": "vite build",
                    "test:e2e": "playwright test",
                },
                "dependencies": {"react": "19.0.0", "express": "5.0.0"},
                "devDependencies": {"@playwright/test": "1.52.0"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname='sample'\n[tool.ruff]\nline-length=100\n",
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(
        "vasuki.application.qa_service.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )

    profile = inspect_project(tmp_path)
    checks = {item.id: item for item in discover_checks(tmp_path, profile)}

    assert profile.frontend and profile.backend and profile.playwright
    assert {"frontend", "backend", "npm", "python", "playwright"} <= set(profile.labels)
    assert checks["playwright"].command == "npm run test:e2e"
    assert checks["js-audit"].command == "npm audit --json"
    assert checks["js-audit"].network_required
    assert checks["python-audit"].command.endswith("pip-audit --format json .")
    assert {"python-lint", "python-tests", "js-lint", "js-test", "js-build"} <= checks.keys()


def test_playwright_is_transparently_skipped_when_not_applicable(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# no browser app\n", encoding="utf-8")

    profile = inspect_project(tmp_path)
    check = next(item for item in discover_checks(tmp_path, profile) if item.id == "playwright")

    assert not profile.playwright
    assert check.status == "skipped"
    assert "Not applicable" in check.summary


def test_specialist_roster_fans_out_then_synthesizes() -> None:
    profile = inspect_project(Path(__file__).parents[2])
    profile = profile.__class__(
        labels=("frontend", "backend", "playwright"),
        frontend=True,
        backend=True,
        playwright=True,
        package_manager="npm",
    )

    plan = specialist_plan(profile)
    waves = validate_team_plan(plan)

    assert [[item.id for item in wave] for wave in waves] == [
        ["architecture", "backend", "code-quality", "frontend", "security", "ui-browser"],
        ["qa-summary"],
    ]
    assert all(member.read_only and member.scope == [] for member in plan.members)
    assert set(plan.members[-1].dependencies) == {
        "architecture",
        "backend",
        "code-quality",
        "frontend",
        "security",
        "ui-browser",
    }


def test_dependency_outputs_are_summarized_for_the_report() -> None:
    assert (
        _dependency_summary(
            json.dumps(
                {
                    "metadata": {
                        "vulnerabilities": {
                            "info": 0,
                            "low": 1,
                            "moderate": 2,
                            "high": 1,
                            "critical": 0,
                            "total": 4,
                        }
                    }
                }
            )
        )
        == "4 JavaScript vulnerability finding(s) reported."
    )
    assert (
        _dependency_summary(
            json.dumps(
                [
                    {"name": "safe", "vulns": []},
                    {"name": "affected", "vulns": [{"id": "PYSEC-1"}]},
                ]
            )
        )
        == "1 vulnerable Python package(s) reported."
    )
