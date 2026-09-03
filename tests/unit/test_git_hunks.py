"""Partial staging: splitting a diff into hunks and rebuilding a valid patch.

The interesting cases are all about hunk headers. Selecting a subset of hunks
invalidates the `+start` of every hunk after the first gap, because those
numbers assumed the earlier hunks had already been applied. Carrying them over
is the classic partial-staging bug — Git either refuses the patch or applies it
somewhere else — so most of these tests are about arithmetic, and the end-to-end
ones prove Git itself accepts the result.
"""

from __future__ import annotations

import subprocess
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.git import GitClient, hunks

FIVE_HUNK_DIFF = textwrap.dedent(
    """\
    diff --git a/app.py b/app.py
    index 1111111..2222222 100644
    --- a/app.py
    +++ b/app.py
    @@ -1,3 +1,4 @@
     one
    +inserted after one
     two
     three
    @@ -10,4 +11,3 @@ def middle():
     ten
    -eleven
     twelve
     thirteen
    @@ -20,3 +20,4 @@ def tail():
     twenty
     twentyone
    +inserted at the end
     twentytwo
    """
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[Path]:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "app.py").write_text(
        "\n".join(f"line{index}" for index in range(1, 31)) + "\n", encoding="utf-8"
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial")
    yield tmp_path


# ------------------------------------------------------------------ parsing


def test_a_diff_splits_into_files_and_hunks() -> None:
    files = hunks.split(FIVE_HUNK_DIFF)

    assert len(files) == 1
    assert files[0].path == "app.py"
    assert len(files[0].hunks) == 3
    assert [hunk.index for hunk in files[0].hunks] == [0, 1, 2]
    assert files[0].hunks[1].heading.strip() == "def middle():"
    assert (files[0].added, files[0].removed) == (2, 1)


def test_a_hunk_counts_its_own_added_and_removed_lines() -> None:
    first, second, third = hunks.split(FIVE_HUNK_DIFF)[0].hunks

    assert (first.added, first.removed) == (1, 0)
    assert (second.added, second.removed) == (0, 1)
    assert (third.added, third.removed) == (1, 0)


def test_a_header_without_counts_is_read_as_one_line() -> None:
    """`@@ -5 +5 @@` is legal and means a count of 1 on both sides."""
    patch = textwrap.dedent(
        """\
        diff --git a/x.txt b/x.txt
        --- a/x.txt
        +++ b/x.txt
        @@ -5 +5 @@
        -old
        +new
        """
    )

    hunk = hunks.split(patch)[0].hunks[0]

    assert (hunk.old_start, hunk.old_count) == (5, 1)
    assert (hunk.new_start, hunk.new_count) == (5, 1)


def test_a_rename_keeps_its_header_verbatim() -> None:
    """Rebuilding the header would lose the rename; it is reused as-is."""
    patch = textwrap.dedent(
        """\
        diff --git a/old.py b/new.py
        similarity index 90%
        rename from old.py
        rename to new.py
        --- a/old.py
        +++ b/new.py
        @@ -1,2 +1,2 @@
        -before
        +after
         same
        """
    )

    file = hunks.split(patch)[0]

    assert file.path == "new.py"
    assert file.old_path == "old.py"
    rebuilt = hunks.rebuild(file, [0])
    assert "rename from old.py" in rebuilt
    assert "rename to new.py" in rebuilt


def test_a_binary_change_is_marked_rather_than_parsed() -> None:
    patch = (
        "diff --git a/logo.png b/logo.png\n"
        "index 1111111..2222222 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )

    file = hunks.split(patch)[0]

    assert file.binary is True
    assert file.hunks == []


# --------------------------------------------------------------- rebuilding


def test_selecting_the_first_hunk_leaves_its_header_alone() -> None:
    """Nothing precedes it, so there is no drift to correct for."""
    file = hunks.split(FIVE_HUNK_DIFF)[0]

    rebuilt = hunks.rebuild(file, [0])

    assert "@@ -1,3 +1,4 @@" in rebuilt
    assert "inserted after one" in rebuilt
    assert "inserted at the end" not in rebuilt


def test_skipping_a_hunk_recomputes_the_ones_after_it() -> None:
    """The bug this exists to prevent.

    In the original diff the third hunk is `@@ -20,3 +20,4 @@` — a +start of 20
    that already accounts for hunk 1 adding a line and hunk 2 removing one. Take
    hunks 1 and 3 without hunk 2 and that +start is off by one, because the
    removal it assumed is no longer in the patch.
    """
    file = hunks.split(FIVE_HUNK_DIFF)[0]

    rebuilt = hunks.rebuild(file, [0, 2])

    # Hunk 1 adds a line, so everything after it shifts by +1.
    assert "@@ -1,3 +1,4 @@" in rebuilt
    assert "@@ -20,3 +21,4 @@" in rebuilt
    # And the skipped hunk's content is genuinely absent.
    assert "eleven" not in rebuilt


def test_selecting_only_a_later_hunk_starts_from_no_drift() -> None:
    file = hunks.split(FIVE_HUNK_DIFF)[0]

    rebuilt = hunks.rebuild(file, [2])

    assert "@@ -20,3 +20,4 @@" in rebuilt
    assert "inserted after one" not in rebuilt


def test_a_rebuilt_patch_always_ends_with_a_newline() -> None:
    """``git apply`` rejects one that does not."""
    file = hunks.split(FIVE_HUNK_DIFF)[0]

    assert hunks.rebuild(file, [0]).endswith("\n")


def test_selecting_nothing_produces_nothing() -> None:
    file = hunks.split(FIVE_HUNK_DIFF)[0]

    assert hunks.rebuild(file, []) == ""
    assert hunks.rebuild(file, [99]) == ""


def test_no_newline_markers_are_not_counted_as_lines() -> None:
    """ "\\ No newline at end of file" is a note about a line, not a line."""
    patch = textwrap.dedent(
        """\
        diff --git a/x.txt b/x.txt
        --- a/x.txt
        +++ b/x.txt
        @@ -1,2 +1,2 @@
         keep
        -old
        \\ No newline at end of file
        +new
        \\ No newline at end of file
        """
    )
    file = hunks.split(patch)[0]

    rebuilt = hunks.rebuild(file, [0])

    # Two lines on each side, not three or four.
    assert "@@ -1,2 +1,2 @@" in rebuilt
    assert "\\ No newline at end of file" in rebuilt


def test_hunks_are_described_line_by_line_for_the_ui() -> None:
    file = hunks.split(FIVE_HUNK_DIFF)[0]

    described = hunks.describe(file)

    assert described[0]["index"] == 0
    assert described[0]["heading"] == ""
    assert described[1]["heading"] == "def middle():"
    kinds = [line["kind"] for line in described[0]["lines"]]
    assert kinds == ["context", "added", "context", "context"]
    # The marker character is stripped; the text is what the editor shows.
    assert described[0]["lines"][1]["text"] == "inserted after one"


# ------------------------------------------------------- against real Git


def test_git_accepts_a_partially_selected_patch(repo: Path) -> None:
    """The proof that the arithmetic is right: Git itself applies it."""
    target = repo / "app.py"
    lines = target.read_text(encoding="utf-8").splitlines()
    lines.insert(1, "FIRST CHANGE")
    lines[15] = "MIDDLE CHANGE"
    lines.append("LAST CHANGE")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    client = GitClient(repo)
    file = hunks.find(hunks.split(client.diff("--", "app.py")), "app.py")
    assert file is not None
    assert len(file.hunks) == 3

    # Stage the first and last, skipping the middle — the arrangement that
    # exposes a bad +start.
    patch = hunks.rebuild(file, [0, 2])
    result = client.apply_patch(patch, cached=True)

    assert result.succeeded, result.stderr
    staged = client.diff(staged=True)
    assert "FIRST CHANGE" in staged
    assert "LAST CHANGE" in staged
    assert "MIDDLE CHANGE" not in staged
    # And the middle change is still in the working tree, unstaged.
    assert "MIDDLE CHANGE" in client.diff()


def test_unstaging_a_hunk_is_the_exact_inverse(repo: Path) -> None:
    target = repo / "app.py"
    lines = target.read_text(encoding="utf-8").splitlines()
    lines.insert(1, "FIRST CHANGE")
    lines.append("LAST CHANGE")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    client = GitClient(repo)
    client.run("add", "--", "app.py")

    file = hunks.find(hunks.split(client.diff(staged=True)), "app.py")
    assert file is not None
    patch = hunks.rebuild(file, [0])

    result = client.apply_patch(patch, cached=True, reverse=True)

    assert result.succeeded, result.stderr
    staged = client.diff(staged=True)
    assert "FIRST CHANGE" not in staged
    assert "LAST CHANGE" in staged
    # Reversing out of the index must not touch the working tree.
    assert "FIRST CHANGE" in target.read_text(encoding="utf-8")


def test_staging_a_hunk_from_a_new_file_works(repo: Path) -> None:
    """A file with no index entry has an empty old side, which numbers from 0."""
    (repo / "fresh.py").write_text("alpha\nbeta\n", encoding="utf-8")
    client = GitClient(repo)
    # An untracked file has no diff at all until Git knows about it.
    client.run("add", "-N", "--", "fresh.py")

    file = hunks.find(hunks.split(client.diff("--", "fresh.py")), "fresh.py")
    assert file is not None
    result = client.apply_patch(hunks.rebuild(file, [0]), cached=True)

    assert result.succeeded, result.stderr
    assert "alpha" in client.diff(staged=True)
