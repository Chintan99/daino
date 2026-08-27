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


def test_a_missing_anchor_in_a_large_file_points_at_paging(tmp_path: Path) -> None:
    """On a big file, unmatched lines are usually just outside the seen view.

    The field failure was a local model editing a 480-line page: after context
    compaction it invented lines that were not in the file, ``replace`` reported
    "None of its lines appear", and it re-read the same truncated view forever.
    The recovery hint must send it to page through the file, not rewrite it all.
    """
    body = "".join(f"<div>row {number}</div>\n" for number in range(1, 400))
    (tmp_path / "page.html").write_text(body, encoding="utf-8")

    result = tools(tmp_path).replace_in_file("page.html", "<div>totally invented</div>", "x")

    assert not result.success
    error = result.error or ""
    assert "offset/limit" in error
    assert "400-line file" in error
    # It must NOT tell the model to rewrite a 400-line file wholesale.
    assert "replace the whole file" not in error


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


# ---- whitespace-tolerant anchors -------------------------------------------
#
# Observed in ~/vasukitest/project4: a 27B local model read an 18 KB HTML file
# six times and then failed four `replace` calls in a row with "old_string was
# not found", until the no-progress guard stopped the whole turn. It reproduces
# the lines and drifts on indentation, which byte-exact matching cannot forgive.

HTML = (
    "<div class=\"panel\">\n"
    "    <label class=\"toggle\">\n"
    "        <input type=\"checkbox\" id=\"darkToggle\">\n"
    "        <span>Dark</span>\n"
    "    </label>\n"
    "</div>\n"
)


def _html_tools(tmp_path: Path):
    """page.html is already in the helper's seen-files list, so no read gate."""
    (tmp_path / "page.html").write_text(HTML, encoding="utf-8")
    return tools(tmp_path)


def test_an_anchor_with_the_wrong_indentation_still_applies(tmp_path: Path) -> None:
    editor = _html_tools(tmp_path)
    # The model dropped the leading indentation on both lines.
    result = editor.replace_in_file(
        "page.html",
        '<label class="toggle">\n<input type="checkbox" id="darkToggle">',
        '<div class="mode">\n<button>Light</button>',
    )
    assert result.success, result.error
    # Recorded, so a relaxed match is auditable rather than silent.
    assert result.data["matched"] == "whitespace-insensitive"

    written = (tmp_path / "page.html").read_text(encoding="utf-8")
    assert "darkToggle" not in written
    # The block is shifted onto the indentation the file actually uses. The
    # model gave its replacement no relative indentation, so none is invented.
    assert '    <div class="mode">\n    <button>Light</button>\n' in written
    # And the untouched lines are intact.
    assert written.startswith('<div class="panel">\n')
    assert written.endswith("</div>\n")


def test_relative_indentation_inside_the_replacement_is_preserved(
    tmp_path: Path,
) -> None:
    """Shift the block, keep its internal shape."""
    editor = _html_tools(tmp_path)
    result = editor.replace_in_file(
        "page.html",
        '<label class="toggle">\n<input type="checkbox" id="darkToggle">',
        '<div class="mode">\n  <button>Light</button>',
    )
    assert result.success, result.error
    written = (tmp_path / "page.html").read_text(encoding="utf-8")
    # First line onto the file's 4 spaces; the nested line keeps its extra two.
    assert '    <div class="mode">\n      <button>Light</button>\n' in written


def test_an_exact_anchor_is_still_matched_exactly(tmp_path: Path) -> None:
    """The relaxed path is a fallback, never the first attempt."""
    editor = _html_tools(tmp_path)
    result = editor.replace_in_file("page.html", "<span>Dark</span>", "<span>Theme</span>")
    assert result.success
    assert "matched" not in result.data


def test_a_relaxed_anchor_that_matches_twice_is_refused(tmp_path: Path) -> None:
    (tmp_path / "page.html").write_text(
        "<p>one</p>\n    <p>one</p>\n", encoding="utf-8"
    )
    editor = tools(tmp_path)
    result = editor.replace_in_file("page.html", "<p>one</p>", "<p>two</p>")
    # Exact matching finds two, so it is refused before the relaxed path runs.
    assert not result.success
    assert "matches" in (result.error or "")

    ambiguous = editor.replace_in_file("page.html", "  <p>one</p>  ", "<p>two</p>")
    assert not ambiguous.success
    assert "several places" in (ambiguous.error or "")
    assert (tmp_path / "page.html").read_text(encoding="utf-8") == (
        "<p>one</p>\n    <p>one</p>\n"
    )


def test_a_near_miss_is_told_where_to_look(tmp_path: Path) -> None:
    """"Read the file again" is advice a weak model follows by repeating itself."""
    editor = _html_tools(tmp_path)
    result = editor.replace_in_file(
        "page.html",
        '<span>Dark</span>\n<span>Nonexistent</span>',
        "<span>Theme</span>",
    )
    assert not result.success
    # Line 4 holds the first anchor line, so the rest is what differs.
    assert "matches line(s) 4" in (result.error or "")


def test_relaxed_matching_still_rolls_back_broken_python(
    module: Path, tmp_path: Path
) -> None:
    """A relaxed match must not bypass the syntax guard."""
    # Anchor with the indentation stripped, so only the relaxed path can match.
    result = tools(tmp_path).replace_in_file(
        "greet.py", "return f\"hello {name}\"", "return f\"hello {name}"
    )
    assert not result.success
    assert module.read_text(encoding="utf-8") == SOURCE
