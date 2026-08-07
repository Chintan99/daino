"""Turn before/after file contents into a renderable diff.

The chat agent edits files directly, so the useful thing to show afterwards is
what changed, not the whole file. This produces line-numbered hunks with a small
amount of surrounding context, which is what makes an edit reviewable at a
glance.
"""

from __future__ import annotations

import difflib

from vasuki.schemas import DiffLine, FileDiff

#: Unchanged lines kept either side of a change. Enough to locate the edit,
#: short enough that a one-line change does not fill the transcript.
CONTEXT_LINES = 3

#: Cap on rendered lines per file. A whole-file rewrite is reported by its
#: counts rather than pasted back in full.
MAX_LINES = 120

#: A newly created file has no "before", so every line is an addition and a full
#: render is just the file pasted back in green. A short head is enough to show
#: what landed; the file itself is on disk.
MAX_CREATED_LINES = 20


def build_file_diff(path: str, before: str | None, after: str | None) -> FileDiff:
    """Diff one file. ``None`` means the file did not exist on that side."""
    if before is None and after is None:
        return FileDiff(path=path, change="modified", note="File is unreadable or binary.")
    change = "created" if before is None else "deleted" if after is None else "modified"
    old = (before or "").splitlines()
    new = (after or "").splitlines()
    if old == new:
        return FileDiff(path=path, change=change, note="No textual change.")

    lines: list[DiffLine] = []
    added = removed = 0
    for group in difflib.SequenceMatcher(None, old, new).get_grouped_opcodes(CONTEXT_LINES):
        for tag, old_start, old_end, new_start, new_end in group:
            if tag == "equal":
                for offset in range(old_end - old_start):
                    lines.append(
                        DiffLine(
                            marker=" ",
                            number=new_start + offset + 1,
                            text=new[new_start + offset],
                        )
                    )
                continue
            if tag in {"replace", "delete"}:
                for offset in range(old_end - old_start):
                    removed += 1
                    lines.append(
                        DiffLine(
                            marker="-",
                            number=old_start + offset + 1,
                            text=old[old_start + offset],
                        )
                    )
            if tag in {"replace", "insert"}:
                for offset in range(new_end - new_start):
                    added += 1
                    lines.append(
                        DiffLine(
                            marker="+",
                            number=new_start + offset + 1,
                            text=new[new_start + offset],
                        )
                    )

    note = ""
    cap = MAX_CREATED_LINES if change == "created" else MAX_LINES
    if len(lines) > cap:
        note = f"Showing the first {cap} of {len(lines)} lines."
        lines = lines[:cap]
    return FileDiff(path=path, change=change, added=added, removed=removed, lines=lines, note=note)


def summarize(diff: FileDiff) -> str:
    """One-line count, in the form the transcript header uses."""
    if diff.change == "created":
        return f"Created with {diff.added} lines"
    if diff.change == "deleted":
        return f"Deleted {diff.removed} lines"
    parts = []
    if diff.added:
        parts.append(f"Added {diff.added} line{'s' if diff.added != 1 else ''}")
    if diff.removed:
        parts.append(f"removed {diff.removed} line{'s' if diff.removed != 1 else ''}")
    return ", ".join(parts) or diff.note or "No change"


def render(diff: FileDiff) -> str:
    """Render a diff as the plain text stored in the conversation transcript."""
    width = max((len(str(line.number)) for line in diff.lines), default=3)
    body = [f"{line.number:>{width}} {line.marker} {line.text}" for line in diff.lines]
    header = f"{diff.path}\n{summarize(diff)}"
    if diff.note and diff.lines:
        body.append(f"… {diff.note}")
    elif diff.note:
        return f"{header}\n{diff.note}"
    return "\n".join([header, *body])
