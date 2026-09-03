"""Splitting a unified diff into hunks, and rebuilding a patch from a subset.

This is what makes partial staging possible. Git has no "stage lines 40-52"
command; the way it is done — by every editor that offers it — is to construct a
patch containing only the chosen hunks and feed it to ``git apply --cached``.

The correctness of that rests on one thing: **a hunk's header must describe the
patch it is in, not the patch it came from.** Take hunks 1 and 3 out of a
five-hunk diff and hunk 3's `@@ -120,6 +118,7 @@` is wrong, because the line
offset it assumed included hunk 2's edits. Git will refuse the patch, or worse
apply it at the wrong place. So headers are recomputed here rather than copied.

Everything is text in, text out. Nothing in this module runs Git.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: `@@ -old_start,old_count +new_start,new_count @@ optional heading`
#: The counts are optional in the format and default to 1 when omitted.
HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<heading>.*)$"
)


@dataclass(slots=True)
class Hunk:
    """One contiguous change within one file."""

    #: Position within its file's hunk list. Stable for one reading of a diff,
    #: which is all a stage/unstage round trip needs.
    index: int
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    #: The function/class context Git puts after the closing `@@`.
    heading: str = ""
    #: Body lines, each still carrying its leading " ", "+", "-", or "\".
    lines: list[str] = field(default_factory=list)

    @property
    def header(self) -> str:
        return (
            f"@@ -{self.old_start},{self.old_count} "
            f"+{self.new_start},{self.new_count} @@{self.heading}"
        )

    @property
    def added(self) -> int:
        return sum(1 for line in self.lines if line.startswith("+"))

    @property
    def removed(self) -> int:
        return sum(1 for line in self.lines if line.startswith("-"))

    @property
    def text(self) -> str:
        """The hunk as it appears in a diff, header included."""
        return "\n".join([self.header, *self.lines])


@dataclass(slots=True)
class FilePatch:
    """One file's section of a unified diff."""

    path: str
    #: Set only for a rename; the path the change came from.
    old_path: str = ""
    #: Everything before the first `@@` — the `diff --git`, mode changes,
    #: index line, and the `---`/`+++` pair. Reused verbatim when rebuilding,
    #: because reconstructing it is how a rename or mode change gets lost.
    header: list[str] = field(default_factory=list)
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False

    @property
    def added(self) -> int:
        return sum(hunk.added for hunk in self.hunks)

    @property
    def removed(self) -> int:
        return sum(hunk.removed for hunk in self.hunks)


def split(patch: str) -> list[FilePatch]:
    """Parse a unified diff into per-file, per-hunk structure."""
    files: list[FilePatch] = []
    current: FilePatch | None = None
    hunk: Hunk | None = None

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            current = FilePatch(path=_path_from_diff_line(line), header=[line])
            hunk = None
            files.append(current)
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            match = HUNK_HEADER.match(line)
            if match is None:
                continue
            hunk = Hunk(
                index=len(current.hunks),
                old_start=int(match.group("old_start")),
                old_count=int(match.group("old_count") or 1),
                new_start=int(match.group("new_start")),
                new_count=int(match.group("new_count") or 1),
                heading=match.group("heading") or "",
            )
            current.hunks.append(hunk)
            continue
        if hunk is not None:
            # Inside a hunk. Blank lines in a diff body are context lines whose
            # single leading space was stripped by a tool along the way; treat
            # them as context rather than dropping them, or the line counts stop
            # matching and Git refuses the patch.
            hunk.lines.append(line if line else " ")
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
            current.header.append(line)
            continue
        if line.startswith("rename from "):
            current.old_path = line[len("rename from ") :].strip()
        elif line.startswith("+++ b/"):
            # Authoritative over the `diff --git` line, which quotes and escapes
            # unusual paths differently.
            current.path = line[len("+++ b/") :].strip()
        current.header.append(line)

    return files


def _path_from_diff_line(line: str) -> str:
    """Best-effort path from `diff --git a/x b/x`, refined later by `+++ b/`."""
    remainder = line[len("diff --git ") :]
    if remainder.startswith("a/"):
        _, _, after = remainder.partition(" b/")
        if after:
            return after.strip()
    return remainder.rsplit(" b/", 1)[-1].strip()


def rebuild(file: FilePatch, indices: list[int] | None = None) -> str:
    """A valid patch containing only the named hunks of one file.

    ``indices`` of None means every hunk. The returned patch always ends with a
    newline, because ``git apply`` rejects one that does not.

    The hunk headers are **recomputed**, not copied. A hunk's `-start` is still
    true — it describes the unchanged side, which selecting a subset does not
    move — but its `+start` is not: it assumed the earlier hunks in the original
    diff had already been applied. Carrying the original value over is the
    classic partial-staging bug, and it either fails loudly or corrupts quietly.
    """
    wanted = (
        file.hunks
        if indices is None
        else [hunk for hunk in file.hunks if hunk.index in set(indices)]
    )
    if not wanted:
        return ""
    lines = list(file.header)
    # Running offset between the two sides, accumulated over the hunks that are
    # actually in this patch.
    drift = 0
    for hunk in wanted:
        old_count = _count(hunk, side="old")
        new_count = _count(hunk, side="new")
        new_start = hunk.old_start + drift
        # An empty original side (a pure addition at the top of a file) starts
        # at 0 in Git's numbering, and its counterpart starts at 1.
        if old_count == 0:
            new_start = max(new_start, 1)
        lines.append(f"@@ -{hunk.old_start},{old_count} +{new_start},{new_count} @@{hunk.heading}")
        lines.extend(hunk.lines)
        drift += new_count - old_count
    return "\n".join(lines) + "\n"


def _count(hunk: Hunk, *, side: str) -> int:
    """Lines this hunk covers on one side, counted from the body.

    Counted rather than taken from the header so a hunk whose body was edited —
    which is exactly what "stage only these lines" would eventually allow —
    still produces a header that matches it.
    """
    total = 0
    for line in hunk.lines:
        if line.startswith("\\"):
            continue  # "\ No newline at end of file" is a marker, not a line
        if line.startswith("+"):
            if side == "new":
                total += 1
        elif line.startswith("-"):
            if side == "old":
                total += 1
        else:
            total += 1
    return total


def find(files: list[FilePatch], path: str) -> FilePatch | None:
    return next((item for item in files if item.path == path), None)


def describe(file: FilePatch) -> list[dict[str, object]]:
    """Hunks in the shape the GUI renders them."""
    return [
        {
            "index": hunk.index,
            "header": hunk.header,
            "heading": hunk.heading.strip(),
            "old_start": hunk.old_start,
            "new_start": hunk.new_start,
            "added": hunk.added,
            "removed": hunk.removed,
            "lines": [
                {
                    "kind": (
                        "added"
                        if line.startswith("+")
                        else "removed"
                        if line.startswith("-")
                        else "marker"
                        if line.startswith("\\")
                        else "context"
                    ),
                    "text": line[1:] if line[:1] in {"+", "-", " "} else line,
                }
                for line in hunk.lines
            ],
        }
        for hunk in file.hunks
    ]
