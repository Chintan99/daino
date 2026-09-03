"""Language-server feedback reaching the agent instead of only the IDE."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from daino.agents.loop import _detail
from daino.repository.code_intel import CodeIntelligence, edit_feedback, render_locations
from daino.schemas import AgentAction, ToolResult
from daino.tools import ActionExecutor, EditTools


class FakeAdapter:
    """A language server that answers instantly, or however the test wants."""

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        delay: float = 0.0,
        fail: Exception | None = None,
    ) -> None:
        self.rows = rows or []
        self.delay = delay
        self.fail = fail
        self.calls = 0
        self.closed = False

    async def start(self, root: Path) -> None:
        return None

    async def diagnostics(self, path: Path, *args: object, **kwargs: object) -> list[dict]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return self.rows

    async def definition(self, path: Path, line: int, column: int) -> list[dict]:
        self.calls += 1
        return [{"path": "src/helper.py", "line": line + 1, "column": column + 1}]

    async def references(self, path: Path, line: int, column: int) -> list[dict]:
        self.calls += 1
        return [
            {"path": "a.py", "line": 3, "column": 5},
            {"path": "b.py", "line": 9, "column": 1},
        ]

    async def hover(self, path: Path, line: int, column: int) -> str:
        return "def helper(value: int) -> str"

    async def close(self) -> None:
        self.closed = True


def intel(tmp_path: Path, adapter: FakeAdapter, **kwargs: Any) -> CodeIntelligence:
    """A CodeIntelligence whose language support is forced on for the test."""
    instance = CodeIntelligence(tmp_path, adapter=adapter, **kwargs)
    instance._supported["python"] = True  # noqa: SLF001 - bypassing the PATH probe
    return instance


ERROR_ROW = {
    "path": "a.py",
    "line": 4,
    "column": 1,
    "severity": "error",
    "message": "Undefined name 'helpr'",
    "source": "pyright",
}
HINT_ROW = {
    "path": "a.py",
    "line": 9,
    "column": 1,
    "severity": "hint",
    "message": "Consider a docstring",
    "source": "",
}


@pytest.mark.asyncio
async def test_an_edit_reports_what_it_broke(tmp_path: Path) -> None:
    adapter = FakeAdapter([ERROR_ROW])
    executor = ActionExecutor(
        EditTools(tmp_path, require_read_before_write=False),
        code_intel=intel(tmp_path, adapter),
    )
    result, paths = await executor.execute(
        AgentAction(thought="write it", action="write", path="a.py", content="x = helpr()\n")
    )
    assert result.success
    assert paths == ["a.py"]
    feedback = result.data["diagnostics_feedback"]
    assert "Undefined name 'helpr'" in feedback
    assert "1 error(s)" in feedback
    # And it reaches the model, ahead of the rest of the observation.
    rendered = _detail(
        AgentAction(thought="t", action="write", path="a.py"), result
    )
    assert rendered.startswith("LANGUAGE SERVER")


@pytest.mark.asyncio
async def test_hints_are_not_worth_interrupting_for(tmp_path: Path) -> None:
    """Feedback on every edit only stays useful while it is all signal."""
    executor = ActionExecutor(
        EditTools(tmp_path, require_read_before_write=False),
        code_intel=intel(tmp_path, FakeAdapter([HINT_ROW])),
    )
    result, _ = await executor.execute(
        AgentAction(thought="t", action="write", path="a.py", content="x = 1\n")
    )
    assert "diagnostics_feedback" not in result.data


@pytest.mark.asyncio
async def test_a_rejected_edit_reports_no_diagnostics(tmp_path: Path) -> None:
    """Pre-existing warnings must not be attributed to a change that never landed."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    adapter = FakeAdapter([ERROR_ROW])
    # require_read_before_write rejects a blind overwrite of an existing file.
    executor = ActionExecutor(
        EditTools(tmp_path, require_read_before_write=True),
        code_intel=intel(tmp_path, adapter),
    )
    result, paths = await executor.execute(
        AgentAction(thought="t", action="write", path="a.py", content="x = 2\n")
    )
    assert not result.success
    assert paths == []
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_a_slow_server_does_not_hold_up_the_edit(tmp_path: Path) -> None:
    adapter = FakeAdapter([ERROR_ROW], delay=5.0)
    executor = ActionExecutor(
        EditTools(tmp_path, require_read_before_write=False),
        code_intel=intel(tmp_path, adapter, diagnostics_timeout=0.05),
    )
    result, _ = await executor.execute(
        AgentAction(thought="t", action="write", path="a.py", content="x = 1\n")
    )
    assert result.success
    assert "diagnostics_feedback" not in result.data


@pytest.mark.asyncio
async def test_a_broken_server_is_not_retried_after_every_edit(tmp_path: Path) -> None:
    from daino.repository.lsp import LSPError

    adapter = FakeAdapter(fail=LSPError("pyright exited"))
    executor = ActionExecutor(
        EditTools(tmp_path, require_read_before_write=False),
        code_intel=intel(tmp_path, adapter),
    )
    for index in range(3):
        result, _ = await executor.execute(
            AgentAction(
                thought="t", action="write", path=f"a{index}.py", content="x = 1\n"
            )
        )
        assert result.success
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_an_unsupported_language_is_silent(tmp_path: Path) -> None:
    adapter = FakeAdapter([ERROR_ROW])
    instance = CodeIntelligence(tmp_path, adapter=adapter)
    instance._supported["python"] = False  # noqa: SLF001
    executor = ActionExecutor(
        EditTools(tmp_path, require_read_before_write=False), code_intel=instance
    )
    result, _ = await executor.execute(
        AgentAction(thought="t", action="write", path="a.py", content="x = 1\n")
    )
    assert result.success
    assert "diagnostics_feedback" not in result.data
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_find_references_resolves_a_name_not_a_coordinate(tmp_path: Path) -> None:
    """The interface a model can actually use: a symbol, not a zero-based offset."""
    (tmp_path / "a.py").write_text(
        "import os\n\n\ndef helper(value):\n    return value\n", encoding="utf-8"
    )
    adapter = FakeAdapter()
    executor = ActionExecutor(EditTools(tmp_path), code_intel=intel(tmp_path, adapter))
    result, _ = await executor.execute(
        AgentAction(
            thought="who calls it", action="find_references", path="a.py", symbol="helper"
        )
    )
    assert result.success
    assert [item["path"] for item in result.data["locations"]] == ["a.py", "b.py"]
    rendered = _detail(
        AgentAction(thought="t", action="find_references", path="a.py"), result
    )
    assert "a.py:3:5" in rendered


@pytest.mark.asyncio
async def test_a_symbol_that_is_not_in_the_file_says_so(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path), code_intel=intel(tmp_path, FakeAdapter()))
    result, _ = await executor.execute(
        AgentAction(thought="t", action="find_definition", path="a.py", symbol="missing")
    )
    assert not result.success
    assert "does not appear in" in (result.error or "")


@pytest.mark.asyncio
async def test_definition_includes_the_server_s_own_summary(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def helper(value):\n    return value\n", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path), code_intel=intel(tmp_path, FakeAdapter()))
    result, _ = await executor.execute(
        AgentAction(thought="t", action="find_definition", path="a.py", symbol="helper")
    )
    assert result.success
    assert "def helper(value: int) -> str" in render_locations(
        result.data, label="definition"
    )


@pytest.mark.asyncio
async def test_diagnostics_without_a_server_is_not_a_clean_bill_of_health(
    tmp_path: Path,
) -> None:
    """An empty list must not read as 'this file has no problems'."""
    instance = CodeIntelligence(tmp_path, adapter=FakeAdapter())
    instance._supported["python"] = False  # noqa: SLF001
    executor = ActionExecutor(EditTools(tmp_path), code_intel=instance)
    result, _ = await executor.execute(
        AgentAction(thought="t", action="diagnostics", path="a.py")
    )
    assert not result.success
    assert "No language server is installed" in (result.error or "")


@pytest.mark.asyncio
async def test_lookups_say_so_when_no_intelligence_is_attached(tmp_path: Path) -> None:
    executor = ActionExecutor(EditTools(tmp_path))
    result, _ = await executor.execute(
        AgentAction(thought="t", action="find_definition", path="a.py", symbol="x")
    )
    assert not result.success
    assert "Use grep" in (result.error or "")


def test_feedback_is_capped_and_errors_come_first() -> None:
    rows = [
        {"line": index, "column": 1, "severity": "warning", "message": f"w{index}"}
        for index in range(50)
    ]
    rows.append({"line": 99, "column": 1, "severity": "error", "message": "the real one"})
    feedback = edit_feedback("a.py", rows)
    assert feedback.splitlines()[1].endswith("error: the real one")
    assert "and 31 more" in feedback


def test_a_clean_file_produces_no_feedback_at_all() -> None:
    assert edit_feedback("a.py", []) == ""
    assert edit_feedback("a.py", [HINT_ROW]) == ""


def test_render_locations_reports_an_empty_answer_honestly() -> None:
    assert "No references found for helper." == render_locations(
        {"symbol": "helper", "locations": []}, label="references"
    )


def test_diagnostics_observation_states_a_clean_file(tmp_path: Path) -> None:
    rendered = _detail(
        AgentAction(thought="t", action="diagnostics", path="a.py"),
        ToolResult(tool="diagnostics", success=True, data={"diagnostics": []}),
    )
    assert rendered == "a.py has no errors or warnings."
