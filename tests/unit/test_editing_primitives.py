"""The edit primitives an agent actually drives.

``replace`` is the workhorse: an exact anchor either matches the file or it does
not, with no line numbers to miscount and no context lines to drift, which is
where unified diffs fail. Its guarantees are worth pinning down precisely,
because the agent's correctness rests on them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from daino.schemas import AgentAction, FileModification
from daino.tools import ActionExecutor, EditTools

SOURCE = """def greet(name):
    return f"hello {name}"


def farewell(name):
    return f"bye {name}"
"""


@pytest.fixture
def module(tmp_path: Path) -> Path:
    path = tmp_path / "greet.py"
    path.write_text(SOURCE, encoding="utf-8")
    return path


def tools(root: Path, **kwargs: object) -> EditTools:
    return EditTools(root, ["greet.py", "page.html", "new.py"], **kwargs)  # type: ignore[arg-type]


def test_a_unique_anchor_is_replaced(module: Path, tmp_path: Path) -> None:
    result = tools(tmp_path).replace_in_file(
        "greet.py", 'return f"hello {name}"', 'return f"hi {name}"'
    )

    assert result.success, result.error
    assert result.data["replacements"] == 1
    assert 'return f"hi {name}"' in module.read_text(encoding="utf-8")
    assert 'return f"bye {name}"' in module.read_text(encoding="utf-8")


def test_an_ambiguous_anchor_is_refused_rather_than_guessed(module: Path, tmp_path: Path) -> None:
    """Editing the first of several matches is wrong as often as it is right."""
    result = tools(tmp_path).replace_in_file("greet.py", "    return f", "    return F")

    assert not result.success
    assert "matches 2 places" in (result.error or "")
    assert "replace_all" in (result.error or "")
    assert module.read_text(encoding="utf-8") == SOURCE


def test_replace_all_changes_every_occurrence(module: Path, tmp_path: Path) -> None:
    result = tools(tmp_path).replace_in_file(
        "greet.py", "    return f", "    return F", replace_all=True
    )

    assert result.success, result.error
    assert result.data["replacements"] == 2
    assert module.read_text(encoding="utf-8").count("    return F") == 2


def test_a_missing_anchor_tells_the_agent_how_to_recover(module: Path, tmp_path: Path) -> None:
    result = tools(tmp_path).replace_in_file("greet.py", "return 'nope'", "x")

    assert not result.success
    assert "was not found" in (result.error or "")
    assert "exactly, including indentation" in (result.error or "")
    assert module.read_text(encoding="utf-8") == SOURCE


def test_replace_rolls_back_when_it_breaks_python(module: Path, tmp_path: Path) -> None:
    result = tools(tmp_path).replace_in_file("greet.py", "def greet(name):", "def greet(:")

    assert not result.success
    assert "syntax" in (result.error or "").casefold()
    assert module.read_text(encoding="utf-8") == SOURCE


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("", "x", "old_string is empty"),
        ("same", "same", "identical"),
    ],
)
def test_degenerate_replacements_are_refused(
    module: Path, tmp_path: Path, old: str, new: str, expected: str
) -> None:
    result = tools(tmp_path).replace_in_file("greet.py", old, new)

    assert not result.success
    assert expected in (result.error or "")


def test_replace_on_a_missing_file_points_at_write(tmp_path: Path) -> None:
    result = tools(tmp_path).replace_in_file("new.py", "a", "b")

    assert not result.success
    assert "does not exist" in (result.error or "")
    assert "write" in (result.error or "")


def test_replace_reaches_the_same_code_through_a_modification(module: Path, tmp_path: Path) -> None:
    result = tools(tmp_path).apply_modification(
        FileModification(
            path="./greet.py",
            action="replace",
            old_string='return f"bye {name}"',
            new_string='return f"later {name}"',
            reason="soften the farewell",
        )
    )

    assert result.success, result.error
    assert "later" in module.read_text(encoding="utf-8")


class TestReadBeforeWrite:
    """A blind whole-file write discards content the agent never saw."""

    def test_writing_over_an_unread_file_is_refused(self, module: Path, tmp_path: Path) -> None:
        editor = tools(tmp_path, require_read_before_write=True)

        result = editor.apply_modification(
            FileModification(path="greet.py", action="create", content="x = 1\n", reason="clobber")
        )

        assert not result.success
        assert "has not been read in this task" in (result.error or "")
        assert module.read_text(encoding="utf-8") == SOURCE

    @pytest.mark.asyncio
    async def test_reading_the_file_first_permits_the_write(
        self, module: Path, tmp_path: Path
    ) -> None:
        editor = tools(tmp_path, require_read_before_write=True)
        executor = ActionExecutor(editor)

        read = await executor.execute(
            AgentAction(thought="look", action="read_file", path="greet.py")
        )
        assert read[0].success
        result = editor.apply_modification(
            FileModification(path="greet.py", action="create", content="x = 1\n", reason="rewrite")
        )

        assert result.success, result.error
        assert module.read_text(encoding="utf-8") == "x = 1\n"

    def test_context_supplied_files_count_as_read(self, module: Path, tmp_path: Path) -> None:
        """Files in the compiled task context were already shown to the agent."""
        editor = tools(tmp_path, require_read_before_write=True, seen_files={"greet.py"})

        result = editor.apply_modification(
            FileModification(path="greet.py", action="create", content="x = 1\n", reason="rewrite")
        )

        assert result.success, result.error

    def test_creating_a_new_file_needs_no_read(self, tmp_path: Path) -> None:
        editor = tools(tmp_path, require_read_before_write=True)

        result = editor.apply_modification(
            FileModification(path="new.py", action="create", content="x = 1\n", reason="create")
        )

        assert result.success, result.error

    def test_deleting_an_unread_file_is_refused(self, module: Path, tmp_path: Path) -> None:
        editor = tools(tmp_path, require_read_before_write=True)

        result = editor.delete_file("greet.py")

        assert not result.success
        assert "has not been read in this task" in (result.error or "")
        assert module.exists()

    def test_replace_is_gated_too(self, module: Path, tmp_path: Path) -> None:
        """The rule is uniform: read before touching an existing file at all.

        An anchor can be produced from a stale read or guessed from the task
        description, so a match is not proof the agent knows the file's current
        state. One rule with no exceptions is also easier for a model to follow.
        """
        editor = tools(tmp_path, require_read_before_write=True)

        blind = editor.replace_in_file("greet.py", 'f"hello {name}"', 'f"hi {name}"')
        assert not blind.success
        assert "has not been read in this task" in (blind.error or "")
        assert module.read_text(encoding="utf-8") == SOURCE

        editor.mark_seen("greet.py")
        assert editor.replace_in_file("greet.py", 'f"hello {name}"', 'f"hi {name}"').success
