"""Parse a unified diff into the shape a reviewer actually needs.

The mechanical checks in :mod:`daino.review.checks` only ever want to look at
what a change *introduced*. Scanning a whole modified file instead would report
every pre-existing problem in it as if the author had just written it, which is
the fastest way to make a review tool worth ignoring.

So the unit here is the added line, with its line number in the new file, and
everything else — removed lines, counts, rename detection, binary detection —
falls out of the same single pass over the patch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

#: How a file was touched. ``renamed`` keeps both paths because a review wants
#: to say "moved and edited" rather than "deleted and added".
ChangeKind = Literal["added", "modified", "deleted", "renamed", "binary"]

_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
_HUNK = re.compile(r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? \+(?P<new>\d+)(?:,\d+)? @@")


@dataclass(slots=True)
class Line:
    """One line the change introduced or removed, with where it sits."""

    number: int
    text: str


@dataclass(slots=True)
class FileChange:
    """One file's part of a diff."""

    path: str
    kind: ChangeKind = "modified"
    #: Set only for a rename, so the reviewer can name the move.
    previous_path: str = ""
    #: Lines the change introduced, numbered in the new file.
    added: list[Line] = field(default_factory=list)
    #: Lines the change removed, numbered in the old file.
    removed: list[Line] = field(default_factory=list)
    #: True when git reported the content as binary rather than emitting a patch.
    binary: bool = False

    @property
    def insertions(self) -> int:
        return len(self.added)

    @property
    def deletions(self) -> int:
        return len(self.removed)

    @property
    def suffix(self) -> str:
        _, _, suffix = self.path.rpartition(".")
        return f".{suffix.casefold()}" if suffix and suffix != self.path else ""

    def added_text(self) -> str:
        """The introduced lines as one blob, for a whole-text scan."""
        return "\n".join(line.text for line in self.added)


def parse_diff(patch: str) -> list[FileChange]:
    """Split a ``git diff`` patch into per-file changes.

    Tolerant by design: an unfamiliar extended header line is skipped rather
    than raising, because a review that refuses to run on an unusual diff is
    worse than one that reports slightly less about it.
    """
    changes: list[FileChange] = []
    current: FileChange | None = None
    old_number = 0
    new_number = 0

    for raw in patch.splitlines():
        header = _DIFF_HEADER.match(raw)
        if header is not None:
            current = FileChange(path=_unquote(header.group("b")))
            changes.append(current)
            old_number = new_number = 0
            continue
        if current is None:
            continue

        if raw.startswith("new file mode"):
            current.kind = "added"
            continue
        if raw.startswith("deleted file mode"):
            current.kind = "deleted"
            continue
        if raw.startswith("rename from "):
            current.kind = "renamed"
            current.previous_path = _unquote(raw.removeprefix("rename from "))
            continue
        if raw.startswith("rename to "):
            current.path = _unquote(raw.removeprefix("rename to "))
            continue
        if raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            current.binary = True
            if current.kind == "modified":
                current.kind = "binary"
            continue

        hunk = _HUNK.match(raw)
        if hunk is not None:
            old_number = int(hunk.group("old"))
            new_number = int(hunk.group("new"))
            continue
        if not old_number and not new_number:
            # Still in the extended header (index, mode, ---/+++ lines).
            continue

        if raw.startswith("+"):
            current.added.append(Line(new_number, raw[1:]))
            new_number += 1
        elif raw.startswith("-"):
            current.removed.append(Line(old_number, raw[1:]))
            old_number += 1
        elif raw.startswith("\\"):
            # "\ No newline at end of file" belongs to the line before it.
            continue
        else:
            # Context, including the empty line git writes as "".
            old_number += 1
            new_number += 1

    return changes


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of paths containing unusual characters."""
    path = path.strip()
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        try:
            return path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            return path[1:-1]
    return path


#: A new file bigger than this is not read line by line by anyone.
MAX_NEW_FILE_LINES = 5_000


def whole_file_change(path: str, content: str) -> FileChange:
    """Treat an entire file as introduced, for something git will not diff.

    An untracked file has no diff — git does not know about it yet — but for a
    review it is the most interesting kind of change there is, so every line
    counts as added.
    """
    lines = content.splitlines()
    truncated = lines[:MAX_NEW_FILE_LINES]
    return FileChange(
        path=path,
        kind="added",
        added=[Line(number, text) for number, text in enumerate(truncated, start=1)],
    )


def binary_change(path: str) -> FileChange:
    """A new file that is not text, so there is nothing to read."""
    return FileChange(path=path, kind="added", binary=True)
