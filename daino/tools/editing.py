"""Validated patch and symbol-level editing."""

from __future__ import annotations

import ast
import fnmatch
import os
import re
import subprocess  # nosec B404
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from daino.design import DesignError, DesignService
from daino.memory import InstructionResolver, MemoryManager, MemoryScope, MemoryType
from daino.schemas import AgentAction, EditSpec, FileModification, TodoItem, ToolResult
from daino.tools.commands import CommandRunner
from daino.tools.filesystem import FileTools
from daino.tools.web import WebResearch
from daino.workbench.models import Workspace, WorkspaceTask
from daino.workbench.service import WorkbenchError, WorkbenchService

#: Ordered ``git apply`` strategies. Models routinely emit diffs with miscounted
#: hunk headers or drifted whitespace; each fallback repairs one of those without
#: changing which lines are edited.
_APPLY_STRATEGIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("exact", ()),
    ("recounted", ("--recount",)),
    ("whitespace-fixed", ("--recount", "--whitespace=fix")),
    ("zero-context", ("--recount", "--unidiff-zero")),
    ("three-way", ("--3way",)),
)

_CONFLICT_MARKERS = ("<<<<<<< ", ">>>>>>> ")
_TOOL_INSTRUCTION_CHARS = 12_000

#: Characters that make a scope entry a pattern rather than a literal path.
_GLOB_MAGIC = ("*", "?", "[")


def scope_matches(pattern: str, relative: str) -> bool:
    """Match a repository-relative path against one scope entry.

    A plain path matches only itself, which is what task scopes have always
    meant. An entry may also use ``*`` for part of one segment and ``**`` for any
    number of segments, so a team member can be scoped to ``api/**`` without the
    planner having to enumerate files that do not exist yet.
    """
    return _match_segments(pattern.split("/"), relative.split("/"))


def _match_segments(pattern: list[str], parts: list[str]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        if not rest:
            return True
        # ``**`` absorbs zero or more segments, so try every split point.
        return any(_match_segments(rest, parts[index:]) for index in range(len(parts) + 1))
    if not parts or not fnmatch.fnmatchcase(parts[0], head):
        return False
    return _match_segments(rest, parts[1:])


def literal_prefix(pattern: str) -> str:
    """Return the leading segments of a scope entry that contain no glob magic."""
    kept: list[str] = []
    for segment in pattern.split("/"):
        if any(char in segment for char in _GLOB_MAGIC):
            break
        kept.append(segment)
    return "/".join(kept)


def patterns_overlap(first: str, second: str) -> bool:
    """Report whether two scope entries could ever match the same path.

    Used to keep parallel team members off each other's files. It answers
    conservatively: a pattern whose first segment is a glob has no literal
    prefix and so is treated as covering the whole repository.
    """
    first_prefix, second_prefix = literal_prefix(first), literal_prefix(second)
    if not first_prefix or not second_prefix:
        return True
    return scope_matches(first, second_prefix) or scope_matches(second, first_prefix)


#: Returned by :func:`_tolerant_span` when the relaxed match is not unique.
_AMBIGUOUS = (-1, -1)


def _normalise(line: str) -> str:
    """A line with its horizontal whitespace collapsed, for anchor matching."""
    return " ".join(line.split())


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _tolerant_span(text: str, old_string: str) -> tuple[int, int] | None:
    """Locate ``old_string`` in ``text`` ignoring horizontal whitespace.

    Returns the character span of the match, ``_AMBIGUOUS`` when more than one
    region matches, or ``None`` when nothing does. Line *structure* still has to
    agree — only indentation and runs of spaces inside a line are forgiven — so
    this cannot silently rewrite a different part of the file.
    """
    wanted = [_normalise(line) for line in old_string.splitlines()]
    if not wanted or not any(wanted):
        return None
    lines = text.splitlines(keepends=True)
    normalised = [_normalise(line) for line in lines]
    # Character offset of the start of each line, to convert a line match back
    # into a span the caller can splice.
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)

    matches: list[tuple[int, int]] = []
    for index in range(len(lines) - len(wanted) + 1):
        if normalised[index : index + len(wanted)] != wanted:
            continue
        start = offsets[index]
        last = index + len(wanted) - 1
        end = offsets[last] + len(lines[last])
        # Keep the newline out of the span when the anchor did not include one,
        # so the replacement does not swallow the line break.
        if not old_string.endswith("\n") and lines[last].endswith("\n"):
            end -= len(lines[last]) - len(lines[last].rstrip("\r\n"))
        matches.append((start, end))
        if len(matches) > 1:
            return _AMBIGUOUS
    return matches[0] if matches else None


def _reindented(new_string: str, matched: str) -> str:
    """Shift ``new_string`` onto the indentation the file actually uses.

    The model supplied both the anchor and the replacement with the same wrong
    indentation, so applying the replacement verbatim would leave the file
    misaligned by exactly the amount the anchor was wrong by.
    """
    replacement_lines = new_string.splitlines(keepends=True)
    if not replacement_lines:
        return new_string
    target = _indent(matched.splitlines()[0]) if matched.splitlines() else ""
    source = _indent(replacement_lines[0])
    if target == source:
        return new_string
    shifted: list[str] = []
    for line in replacement_lines:
        stripped = line.lstrip(" \t")
        if not stripped.strip():
            shifted.append(line)
            continue
        existing = _indent(line)
        # Preserve relative indentation inside the block.
        extra = existing[len(source) :] if existing.startswith(source) else ""
        shifted.append(target + extra + stripped)
    return "".join(shifted)


def _recovery_hint(text: str, old_string: str) -> str:
    """Point the agent at where the anchor nearly matched.

    "Read the file again" is advice a weak model follows by reading the same file
    and producing the same near-miss. A line number is something it can act on.
    """
    first = next((line for line in old_string.splitlines() if line.strip()), "")
    needle = _normalise(first)
    if not needle:
        return "Read the file again and copy the text exactly, including indentation."
    hits = [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if _normalise(line) == needle
    ]
    if not hits:
        partial = [
            number
            for number, line in enumerate(text.splitlines(), start=1)
            if needle[:40] and needle[:40] in _normalise(line)
        ][:3]
        if partial:
            return (
                f"Its first line resembles line(s) {', '.join(map(str, partial))}; "
                "read that region and copy the text exactly, including indentation."
            )

        line_count = text.count("\n") + 1
        if line_count > 150:
            return (
                f"None of its lines appear in this {line_count}-line file. The region you want "
                "is probably outside the part you have already seen — read the file in ranges "
                "with offset/limit until you locate the exact lines, then copy them verbatim. "
                "Do not write content from memory."
            )
        return (
            "None of its lines appear in the file. Read the file again and copy the "
            "text exactly, including indentation — or use the write action to "
            "replace the whole file."
        )
    shown = ", ".join(str(number) for number in hits[:3])
    return (
        f"Its first line matches line(s) {shown}, so the rest of the anchor differs. "
        "Re-read that region and copy it exactly, including indentation."
    )


class EditTools:
    def __init__(
        self,
        root: Path,
        allowed_files: list[str] | None = None,
        *,
        require_read_before_write: bool = False,
        seen_files: set[str] | None = None,
        read_only: bool = False,
    ) -> None:
        self.root = root.resolve()
        self.files = FileTools(self.root)
        self.allowed_files = {self.normalize(item) for item in (allowed_files or [])}
        self.require_read_before_write = require_read_before_write
        #: Paths whose current contents the agent has been shown, either by
        #: reading them or through the compiled task context.
        self.seen_files = {self.normalize(item) for item in (seen_files or ())}
        #: Refuse every mutation. An empty ``allowed_files`` means "anything", so
        #: an explorer cannot be confined by scope alone.
        self.read_only = read_only

    def mark_seen(self, relative: str) -> None:
        self.seen_files.add(self.normalize(relative))

    def _readonly_rejection(self, tool: str) -> ToolResult | None:
        if not self.read_only:
            return None
        return ToolResult(
            tool=tool,
            success=False,
            error="This agent is read-only and may not modify the repository.",
        )

    def delete_file(self, relative: str) -> ToolResult:
        """Delete one file, subject to the same scope gate as every other write."""
        rejection = self._readonly_rejection("delete_file")
        if rejection is not None:
            return rejection
        relative = self.normalize(relative)
        if not self._allowed(relative):
            allowed = ", ".join(sorted(self.allowed_files)) or "none"
            return ToolResult(
                tool="delete_file",
                success=False,
                error=f"Path outside allowed task scope: {relative}. This task may edit: {allowed}",
            )
        unseen = self._unseen(relative)
        if unseen:
            return ToolResult(tool="delete_file", success=False, error=unseen)
        return self.files.delete_file(relative)

    def _unseen(self, relative: str) -> str | None:
        """Reject a blind write over a file whose contents were never shown.

        Changing a file the agent has not read risks throwing away work it does
        not know exists. Reading first is cheap; recovering the file is not.

        Applies to every mutation, including ``replace``. An anchor can come
        from a stale read or be guessed from the task description, so a match is
        not proof the agent knows the file's current state — and one rule with
        no exceptions is easier for a model to follow than a nuanced one.
        """
        if not self.require_read_before_write:
            return None
        relative = self.normalize(relative)
        if relative in self.seen_files or not (self.root / relative).is_file():
            return None
        return (
            f"{relative} already exists and has not been read in this task. "
            "Read it first, then edit it."
        )

    @staticmethod
    def normalize(relative: str) -> str:
        """Reduce a model-supplied path to the plain repository-relative form.

        Models write the same target as ``landing.html``, ``./landing.html``, or
        ``/landing.html`` interchangeably; treating those as different paths
        rejects perfectly valid edits.
        """
        text = relative.strip().replace("\\", "/").lstrip("/")
        while text.startswith("./"):
            text = text[2:]
        return PurePosixPath(text).as_posix() if text else text

    def _allowed(self, relative: str) -> bool:
        if not self.allowed_files:
            return True
        candidate = self.normalize(relative)
        return any(scope_matches(pattern, candidate) for pattern in self.allowed_files)

    def _git_apply(self, patch_path: str, flags: tuple[str, ...], *, check: bool) -> str | None:
        """Run ``git apply``; return None on success or the stderr on failure."""
        result = subprocess.run(  # nosec B603, B607
            ["git", "apply", *flags, *(["--check"] if check else []), patch_path],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return None if result.returncode == 0 else (result.stderr.strip() or "git apply failed")

    @staticmethod
    def _hunks(patch: str) -> list[tuple[list[str], list[str]]]:
        """Split a unified diff into (removed, added) line pairs per hunk."""
        hunks: list[tuple[list[str], list[str]]] = []
        removed: list[str] = []
        added: list[str] = []
        for line in patch.splitlines():
            if line.startswith("@@"):
                if removed or added:
                    hunks.append((removed, added))
                removed, added = [], []
            elif line.startswith(("---", "+++", "diff ", "index ")):
                continue
            elif line.startswith("-"):
                removed.append(line[1:])
            elif line.startswith("+"):
                added.append(line[1:])
        if removed or added:
            hunks.append((removed, added))
        return hunks

    def _apply_by_content(self, relative: str, patch: str) -> str | None:
        """Anchor a diff on the text it removes rather than on line numbers.

        Last resort for diffs git rejects because the hunk header miscounts or
        the context lines drift. Safer than ``git apply -C0``, which trusts the
        stated line numbers without checking what is actually there: here the
        removed block must appear exactly once, so a hunk can only land on the
        text it was written against.
        """
        path = (self.root / relative).resolve()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return f"could not read {relative}: {exc}"
        hunks = self._hunks(patch)
        if not hunks:
            return "no hunks found"
        updated = text
        for removed, added in hunks:
            if not removed:
                return "hunk only inserts lines, so it cannot be anchored"
            block = "\n".join(removed)
            if updated.count(block) != 1:
                return f"removed block appears {updated.count(block)} times, expected once"
            updated = updated.replace(block, "\n".join(added), 1)
        path.write_text(updated, encoding="utf-8")
        return None

    def _has_conflict_markers(self, paths: list[str]) -> str | None:
        for relative in paths:
            try:
                text = (self.root / relative).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(marker in text for marker in _CONFLICT_MARKERS):
                return f"Three-way merge left conflict markers in {relative}"
        return None

    def apply_unified_diff(self, patch: str) -> ToolResult:
        rejection = self._readonly_rejection("apply_unified_diff")
        if rejection is not None:
            return rejection
        touched = []
        for line in patch.splitlines():
            if line.startswith("+++ b/") or line.startswith("+++ "):
                relative = line.removeprefix("+++ b/").removeprefix("+++ ").strip()
                if relative in {"/dev/null", ""}:
                    continue
                relative = self.normalize(relative)
                path = (self.root / relative).resolve()
                if not path.is_relative_to(self.root) or not self._allowed(relative):
                    return ToolResult(
                        tool="apply_unified_diff",
                        success=False,
                        error=f"Patch touches disallowed path {relative}",
                    )
                touched.append(relative)
        if not touched:
            return ToolResult(
                tool="apply_unified_diff", success=False, error="Patch has no target files"
            )
        if not patch.endswith("\n"):
            patch += "\n"
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".patch", encoding="utf-8", delete=False
            ) as handle:
                handle.write(patch)
                handle.flush()
                patch_path = handle.name
            try:
                failures: list[str] = []
                for name, flags in _APPLY_STRATEGIES:
                    reason = self._git_apply(patch_path, flags, check=True)
                    if reason is not None:
                        failures.append(f"{name}: {reason.splitlines()[0]}")
                        continue
                    reason = self._git_apply(patch_path, flags, check=False)
                    if reason is not None:
                        failures.append(f"{name}: {reason.splitlines()[0]}")
                        continue
                    problem = self._validate_python(touched) or self._has_conflict_markers(touched)
                    if problem:
                        self._git_apply(patch_path, (*flags, "--reverse"), check=False)
                        return ToolResult(tool="apply_unified_diff", success=False, error=problem)
                    return ToolResult(
                        tool="apply_unified_diff",
                        success=True,
                        data={"files": touched, "strategy": name},
                    )
                target = self.root / touched[0]
                if len(touched) == 1 and target.is_file():
                    original = target.read_text(encoding="utf-8")
                    reason = self._apply_by_content(touched[0], patch)
                    if reason is None:
                        problem = self._validate_python(touched)
                        if problem:
                            (self.root / touched[0]).write_text(original, encoding="utf-8")
                        else:
                            return ToolResult(
                                tool="apply_unified_diff",
                                success=True,
                                data={"files": touched, "strategy": "content-anchored"},
                            )
                        failures.append(f"content-anchored: {problem}")
                    else:
                        failures.append(f"content-anchored: {reason}")
                return ToolResult(
                    tool="apply_unified_diff",
                    success=False,
                    error=(
                        "Could not apply the diff. "
                        + "; ".join(failures)
                        + ". Return the file's complete new content with action "
                        '"create" instead.'
                    ),
                )
            finally:
                os.unlink(patch_path)
        except (OSError, subprocess.SubprocessError) as exc:
            return ToolResult(tool="apply_unified_diff", success=False, error=str(exc))

    def replace_in_file(
        self,
        relative: str,
        old_string: str,
        new_string: str,
        *,
        replace_all: bool = False,
    ) -> ToolResult:
        """Replace an exact span of text.

        The most reliable edit primitive available to a model: an anchor string
        either matches the file or it does not. There are no line numbers to
        miscount and no context lines to drift, which is where unified diffs
        fail. Ambiguity is an error rather than a guess — a match count other
        than one means the anchor was not specific enough, and silently editing
        the first occurrence would be the wrong one as often as not.
        """
        rejection = self._readonly_rejection("replace_in_file")
        if rejection is not None:
            return rejection
        relative = self.normalize(relative)
        if not self._allowed(relative):
            allowed = ", ".join(sorted(self.allowed_files)) or "none"
            return ToolResult(
                tool="replace_in_file",
                success=False,
                error=f"Path outside allowed task scope: {relative}. This task may edit: {allowed}",
            )
        if not old_string:
            return ToolResult(
                tool="replace_in_file",
                success=False,
                error="old_string is empty; use the write action to create or replace a whole file",
            )
        if old_string == new_string:
            return ToolResult(
                tool="replace_in_file",
                success=False,
                error="old_string and new_string are identical, so there is nothing to change",
            )
        blind = self._unseen(relative)
        if blind:
            return ToolResult(tool="replace_in_file", success=False, error=blind)
        path = (self.root / relative).resolve()
        if not path.is_file():
            return ToolResult(
                tool="replace_in_file",
                success=False,
                error=f"{relative} does not exist; use the write action to create it",
            )
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(tool="replace_in_file", success=False, error=str(exc))
        occurrences = text.count(old_string)
        if occurrences == 0:
            # Exact matching is right, and byte-exactness is also the single
            # thing weaker models cannot deliver: they reproduce the lines but
            # drift on indentation or a trailing space, and then retry the same
            # edit until the no-progress guard stops the whole turn. So before
            # giving up, look for the same lines ignoring horizontal whitespace.
            span = _tolerant_span(text, old_string)
            if span is None:
                return ToolResult(
                    tool="replace_in_file",
                    success=False,
                    error=(
                        f"old_string was not found in {relative}. "
                        f"{_recovery_hint(text, old_string)}"
                    ),
                )
            if span == _AMBIGUOUS:
                return ToolResult(
                    tool="replace_in_file",
                    success=False,
                    error=(
                        f"old_string matches several places in {relative} once "
                        "indentation is ignored. Include more surrounding lines to "
                        "make it unique"
                    ),
                )
            start, end = span
            updated = text[:start] + _reindented(new_string, text[start:end]) + text[end:]
            path.write_text(updated, encoding="utf-8")
            syntax_error = self._validate_python([relative])
            if syntax_error:
                path.write_text(text, encoding="utf-8")
                return ToolResult(tool="replace_in_file", success=False, error=syntax_error)
            return ToolResult(
                tool="replace_in_file",
                success=True,
                data={
                    "path": relative,
                    "replacements": 1,
                    # Recorded, never silent: the anchor did not match exactly.
                    "matched": "whitespace-insensitive",
                },
            )
        if occurrences > 1 and not replace_all:
            return ToolResult(
                tool="replace_in_file",
                success=False,
                error=(
                    f"old_string matches {occurrences} places in {relative}. Include "
                    "surrounding lines to make it unique, or set replace_all to change "
                    "every occurrence"
                ),
            )
        updated = (
            text.replace(old_string, new_string)
            if replace_all
            else text.replace(old_string, new_string, 1)
        )
        path.write_text(updated, encoding="utf-8")
        syntax_error = self._validate_python([relative])
        if syntax_error:
            path.write_text(text, encoding="utf-8")
            return ToolResult(tool="replace_in_file", success=False, error=syntax_error)
        return ToolResult(
            tool="replace_in_file",
            success=True,
            data={"path": relative, "replacements": occurrences if replace_all else 1},
        )

    def multi_replace(self, relative: str, edits: list[EditSpec]) -> ToolResult:
        """Apply exact replacements to one in-memory snapshot, then write once.

        Validating every anchor before the write makes ``multi_edit`` genuinely
        atomic.  It also validates Python only after all coordinated edits have
        been applied, so an intentionally incomplete intermediate state cannot
        reject an otherwise valid batch.
        """
        rejection = self._readonly_rejection("multi_edit")
        if rejection is not None:
            return rejection
        relative = self.normalize(relative)
        if not self._allowed(relative):
            allowed = ", ".join(sorted(self.allowed_files)) or "none"
            return ToolResult(
                tool="multi_edit",
                success=False,
                error=f"Path outside allowed task scope: {relative}. This task may edit: {allowed}",
            )
        blind = self._unseen(relative)
        if blind:
            return ToolResult(tool="multi_edit", success=False, error=blind)
        path = (self.root / relative).resolve()
        if not path.is_file():
            return ToolResult(
                tool="multi_edit",
                success=False,
                error=f"{relative} does not exist; use the write action to create it",
            )
        try:
            original = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(tool="multi_edit", success=False, error=str(exc))

        updated = original
        for index, edit in enumerate(edits, 1):
            if not edit.old_string:
                return ToolResult(
                    tool="multi_edit",
                    success=False,
                    error=f"Edit {index} of {len(edits)} failed: old_string is empty.",
                )
            if edit.old_string == edit.new_string:
                return ToolResult(
                    tool="multi_edit",
                    success=False,
                    error=(
                        f"Edit {index} of {len(edits)} failed: old_string and new_string "
                        "are identical."
                    ),
                )
            occurrences = updated.count(edit.old_string)
            expected = "at least one" if edit.replace_all else "exactly one"
            if occurrences == 0 or (occurrences > 1 and not edit.replace_all):
                return ToolResult(
                    tool="multi_edit",
                    success=False,
                    error=(
                        f"Edit {index} of {len(edits)} failed: old_string matches "
                        f"{occurrences} places in {relative}; expected {expected}. "
                        "Read the file again and use a unique exact anchor. No edits were applied."
                    ),
                )
            updated = (
                updated.replace(edit.old_string, edit.new_string)
                if edit.replace_all
                else updated.replace(edit.old_string, edit.new_string, 1)
            )

        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return ToolResult(tool="multi_edit", success=False, error=str(exc))
        syntax_error = self._validate_python([relative])
        if syntax_error:
            path.write_text(original, encoding="utf-8")
            return ToolResult(tool="multi_edit", success=False, error=syntax_error)
        return ToolResult(
            tool="multi_edit", success=True, data={"path": relative, "edits": len(edits)}
        )

    def apply_modification(self, modification: FileModification) -> ToolResult:
        rejection = self._readonly_rejection("apply_modification")
        if rejection is not None:
            return rejection
        relative = self.normalize(modification.path)
        if not relative:
            return ToolResult(
                tool="apply_modification", success=False, error="Modification has no path"
            )
        if not self._allowed(relative):
            allowed = ", ".join(sorted(self.allowed_files)) or "none"
            return ToolResult(
                tool="apply_modification",
                success=False,
                error=(
                    f"Path outside allowed task scope: {relative}. This task may edit: {allowed}"
                ),
            )
        if modification.action == "delete":
            return self.files.delete_file(relative)
        if modification.action == "replace" or modification.old_string:
            return self.replace_in_file(
                relative,
                modification.old_string or "",
                modification.new_string or "",
                replace_all=modification.replace_all,
            )
        if modification.action == "patch" and modification.unified_diff:
            return self.apply_unified_diff(modification.unified_diff)
        if modification.content is None:
            return ToolResult(
                tool="apply_modification",
                success=False,
                error=(
                    f"Modification for {relative} supplied neither a unified diff nor file content"
                ),
            )
        return self._write(relative, modification.content)

    def _write(self, relative: str, content: str) -> ToolResult:
        """Write full content, treating an existing path as a rewrite.

        Models return whole-file content for an existing file far more often than
        a clean diff, especially for markup and stylesheets. Refusing that as
        "file exists" made the agent unable to edit anything it had not just
        created, so full content is accepted as the file's new state.
        """
        unseen = self._unseen(relative)
        if unseen:
            return ToolResult(tool="write_file", success=False, error=unseen)
        path = (self.root / relative).resolve()
        existed = path.is_file()
        previous = path.read_text(encoding="utf-8") if existed else None
        result = self.files.write_file(relative, content, create=not existed)
        if not result.success:
            return result
        syntax_error = self._validate_python([relative])
        if syntax_error:
            # Never leave the workspace worse than we found it.
            if previous is None:
                self.files.delete_file(relative)
            else:
                path.write_text(previous, encoding="utf-8")
            return ToolResult(tool="apply_modification", success=False, error=syntax_error)
        return ToolResult(
            tool="write_file" if existed else "create_file",
            success=True,
            data={"path": relative, "bytes": len(content.encode()), "rewrote": existed},
            duration_seconds=result.duration_seconds,
        )

    def replace_symbol(self, relative: str, symbol: str, replacement: str) -> ToolResult:
        rejection = self._readonly_rejection("replace_symbol")
        if rejection is not None:
            return rejection
        if not self._allowed(relative):
            return ToolResult(tool="replace_symbol", success=False, error="Path not allowed")
        path = self.root / relative
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            candidates = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == symbol
            ]
            if len(candidates) != 1:
                return ToolResult(
                    tool="replace_symbol",
                    success=False,
                    error=f"Expected exactly one symbol {symbol}; found {len(candidates)}",
                )
            node = candidates[0]
            lines = text.splitlines(keepends=True)
            updated = "".join(
                lines[: node.lineno - 1] + [replacement.rstrip() + "\n"] + lines[node.end_lineno :]
            )
            ast.parse(updated)
            path.write_text(updated, encoding="utf-8")
            return ToolResult(
                tool="replace_symbol", success=True, data={"path": relative, "symbol": symbol}
            )
        except (OSError, SyntaxError) as exc:
            return ToolResult(tool="replace_symbol", success=False, error=str(exc))

    def _validate_python(self, paths: list[str]) -> str | None:
        for relative in paths:
            if not relative.endswith((".py", ".pyi")):
                continue
            try:
                ast.parse((self.root / relative).read_text(encoding="utf-8"))
            except (OSError, SyntaxError) as exc:
                return f"Python syntax validation failed for {relative}: {exc}"
        return None


class ActionExecutor:
    """Runs one iterative agent action against a workspace.

    The builder agent no longer returns all file changes in a single batch.
    It emits one ``AgentAction`` at a time; the executor applies it through
    the same validated, scope-checked, syntax-validated primitives used by the
    batch path, and returns the result plus the set of paths it touched. Reading
    a file marks it seen so the read-before-edit gate lets the next edit land.
    """

    def __init__(
        self,
        editor: EditTools,
        commands: CommandRunner | None = None,
        *,
        web: WebResearch | None = None,
        memory: MemoryManager | None = None,
        memory_task_id: str | None = None,
        memory_session_id: str | None = None,
        design: DesignService | None = None,
        workbench: WorkbenchService | None = None,
        workspace_id: str = "",
    ) -> None:
        self.editor = editor
        #: Attached when the agent is allowed to run commands. Absent for paths
        #: that must stay side-effect free, where ``run_command`` is refused.
        self.commands = commands
        #: Deliberately separate from command execution: fetching public text
        #: should not require curl in the project runtime or inherit its network.
        self.web = web
        self.memory = memory
        self.memory_task_id = memory_task_id
        self.memory_session_id = memory_session_id
        #: Attached when design-workspace tools are available (the GUI, and the
        #: TUI when a project is open). Absent means design actions are refused.
        self.design = design
        #: Attached when knowledge-work workspaces are available. Absent means
        #: workspace actions are refused, the same way design actions are.
        self.workbench = workbench
        #: The workspace the user has open, so the model does not have to name
        #: one it was never told about.
        self.workspace_id = workspace_id
        self.instructions = InstructionResolver(self.editor.root)
        #: The agent's current plan, replaced whenever it emits ``todo``.
        self.todos: list[TodoItem] = []
        #: Read windows accumulated per file. A model may page through a large
        #: file before editing it; once the windows cover the whole file, the
        #: read-before-write gate can safely treat it as seen.
        self.read_ranges: dict[str, list[tuple[int, int]]] = {}

    async def execute(self, action: AgentAction) -> tuple[ToolResult, list[str]]:
        name = action.action
        if name == "finish":
            return (
                ToolResult(tool="finish", success=True, data={"summary": action.summary}),
                [],
            )
        if name == "respond":
            return (
                ToolResult(tool="respond", success=True, data={"message": action.message}),
                [],
            )
        if name == "run_command":
            if self.commands is None:
                return (
                    ToolResult(
                        tool="run_command",
                        success=False,
                        error="Running commands is not available in this context.",
                    ),
                    [],
                )
            timeout = action.timeout or None
            return await self.commands.run(action.command, timeout=timeout), []
        if name in {"web_search", "fetch_url"}:
            if self.web is None:
                return (
                    ToolResult(
                        tool=name,
                        success=False,
                        error="Internet research is not available in this context.",
                    ),
                    [],
                )
            if name == "web_search":
                return (
                    await self.web.search(action.query, max_results=action.max_results),
                    [],
                )
            return await self.web.fetch(action.url, max_chars=action.max_chars), []
        if name.startswith("memory_"):
            return self._memory_action(action), []
        if name.startswith(("create_design", "read_design", "update_design")) or name in {
            "read_design_artifact",
            "add_design_node",
            "update_design_node",
            "delete_design_node",
            "connect_design_nodes",
            "disconnect_design_nodes",
        }:
            return self._design_action(action), []
        if name in {"workspace_read", "workspace_plan", "workspace_task"}:
            return self._workspace_action(action), []
        if name == "glob":
            return self.editor.files.glob_files(action.pattern or action.query), []
        if name == "grep":
            return self.editor.files.grep(action.query, path_glob=action.pattern), []
        if name == "todo":
            self.todos = list(action.todos)
            return (
                ToolResult(
                    tool="todo",
                    success=True,
                    data={"todos": [item.model_dump(mode="json") for item in self.todos]},
                ),
                [],
            )
        if name == "read_file":
            # offset/limit let the agent page through a file it cannot hold at
            # once, instead of getting a truncated head and guessing at the rest.
            start = action.offset or None
            end = (
                (action.offset + action.limit - 1)
                if action.offset and action.limit
                else (action.limit or None)
            )
            result = self.editor.files.read_file(action.path, start, end)
            if result.success:
                self._record_read(action.path, result)
                resolved = self.instructions.resolve([action.path])
                if resolved.text:
                    text = resolved.text
                    if len(text) > _TOOL_INSTRUCTION_CHARS:
                        text = text[:3_000] + "\n… broader instructions omitted …\n" + text[-8_900:]
                    result.data["effective_instructions"] = text
            return result, []
        if name == "list_directory":
            return self.editor.files.list_directory(action.path or "."), []
        if name == "search_text":
            return self.editor.files.search_text(action.query), []
        if name == "multi_edit":
            return self._multi_edit(action)
        if name == "delete":
            # Through EditTools, not FileTools: deletion is a mutation and has to
            # clear the same scope and read-only gates as every other write.
            result = self.editor.delete_file(action.path)
            return result, [self.editor.normalize(action.path)] if result.success else []
        if name == "replace":
            result = self.editor.replace_in_file(
                action.path,
                action.old_string,
                action.new_string,
                replace_all=action.replace_all,
            )
            return result, [self.editor.normalize(action.path)] if result.success else []
        if name == "write":
            result = self.editor.apply_modification(
                FileModification(
                    path=action.path,
                    action="create",
                    content=action.content,
                    reason=action.summary or "write",
                )
            )
            relative = self.editor.normalize(action.path)
            if result.success:
                # The model supplied the complete contents, so it knows the
                # current state of a file it just created and may refine it on
                # the next turn without a redundant read.
                self.editor.mark_seen(relative)
            return result, [relative] if result.success else []
        return (
            ToolResult(tool=name, success=False, error=f"Unknown action {name}"),
            [],
        )

    def _memory_action(self, action: AgentAction) -> ToolResult:
        if self.memory is None:
            return ToolResult(
                tool=action.action,
                success=False,
                error="Durable memory is not available in this context.",
            )
        try:
            if action.action == "memory_search":
                types = None if action.memory_type == "semantic" else [action.memory_type]
                matches = self.memory.search(
                    action.query,
                    memory_types=types,
                    task_id=self.memory_task_id,
                    session_id=self.memory_session_id,
                )
                return ToolResult(
                    tool=action.action,
                    success=True,
                    data={"memories": [item.model_dump(mode="json") for item in matches]},
                )
            if action.action == "memory_list":
                items = self.memory.list(
                    memory_type=None if action.memory_type == "semantic" else action.memory_type,
                    limit=max(1, min(action.limit or 20, 100)),
                )
                return ToolResult(
                    tool=action.action,
                    success=True,
                    data={"memories": [item.model_dump(mode="json") for item in items]},
                )
            if action.action == "memory_save":
                scope = MemoryScope(action.memory_scope)
                if scope == MemoryScope.GLOBAL and not re.search(
                    r"\b(for all|always|across (?:all )?projects|i prefer)\b",
                    action.content,
                    re.I,
                ):
                    return ToolResult(
                        tool=action.action,
                        success=False,
                        error="Global memory requires an explicit cross-project user preference.",
                    )
                memory_id = self.memory.remember(
                    action.content,
                    memory_type=MemoryType(action.memory_type),
                    scope=scope,
                    summary=action.summary,
                    importance=action.importance,
                    confidence=action.confidence,
                    source=action.source or "agent-memory-tool",
                    source_type="agent",
                    tags=action.tags,
                    task_id=self.memory_task_id,
                    session_id=self.memory_session_id,
                    rationale="Created through the validated memory_save tool",
                )
                return ToolResult(
                    tool=action.action,
                    success=True,
                    data={"memory_id": memory_id},
                )
            if action.action == "memory_update":
                updates: dict[str, object] = {}
                if action.content:
                    updates["content"] = action.content
                if action.summary:
                    updates["summary"] = action.summary
                updates["importance"] = action.importance
                updates["confidence"] = action.confidence
                self.memory.update(action.memory_id, **updates)
                return ToolResult(
                    tool=action.action,
                    success=True,
                    data={"memory_id": action.memory_id},
                )
            if action.action == "memory_forget":
                self.memory.forget(action.memory_id)
                return ToolResult(
                    tool=action.action,
                    success=True,
                    data={"memory_id": action.memory_id},
                )
            if action.action == "memory_verify":
                self.memory.verify(action.memory_id, confidence=action.confidence)
                return ToolResult(
                    tool=action.action,
                    success=True,
                    data={"memory_id": action.memory_id},
                )
        except (ValueError, OSError) as exc:
            return ToolResult(tool=action.action, success=False, error=str(exc))
        return ToolResult(tool=action.action, success=False, error="Unknown memory operation")

    #: Default card geometry per artifact kind, matching the canvas importer so
    #: an agent-authored page lands the same size as a dropped one.
    _ARTIFACT_SIZE = {
        "html": (460, 320),
        "svg": (340, 260),
        "markdown": (320, 220),
        "text": (320, 200),
    }
    #: Artifact source can be a whole web page; summarise it in tool results so
    #: reading a design never floods the context with markup.
    _ARTIFACT_PREVIEW_CHARS = 160

    def _artifact_data(self, action: AgentAction) -> dict[str, Any] | None:
        """Build the node payload for an artifact, or ``None`` for a plain box."""
        if not action.node_kind and not action.node_content:
            return None
        kind = action.node_kind or "html"
        extension = {"html": ".html", "svg": ".svg", "markdown": ".md", "text": ".txt"}[kind]
        stem = (action.node_id or action.node_label or "artifact").strip() or "artifact"
        width, height = self._ARTIFACT_SIZE[kind]
        return {
            "kind": kind,
            "content": action.node_content,
            "filename": stem if stem.endswith(extension) else f"{stem}{extension}",
            "width": width,
            "height": height,
        }

    @classmethod
    def _summarize_nodes(cls, design: Any) -> list[dict[str, Any]]:
        """Dump nodes with artifact source replaced by a short description."""
        summarized = []
        for node in design.nodes:
            payload = node.model_dump(mode="json")
            data = payload.get("data") or {}
            content = data.get("content")
            if isinstance(content, str) and content:
                data = dict(data)
                data["content"] = (
                    content[: cls._ARTIFACT_PREVIEW_CHARS] + "…"
                    if len(content) > cls._ARTIFACT_PREVIEW_CHARS
                    else content
                )
                data["content_chars"] = len(content)
                payload["data"] = data
            summarized.append(payload)
        return summarized

    def _workspace_action(self, action: AgentAction) -> ToolResult:
        """Read a workspace, and keep its visible plan current.

        Only three operations, because a workspace's documents are ordinary
        files: the agent already has ``read_file``, ``write`` and ``replace``
        for those. What a file cannot answer is "what is in this workspace" and
        "where is the work up to", which is all this covers.
        """
        if self.workbench is None:
            return ToolResult(
                tool=action.action,
                success=False,
                error="No workspace is available in this context.",
            )
        workspace_id = action.workspace_id or self.workspace_id
        if not workspace_id:
            return ToolResult(
                tool=action.action,
                success=False,
                error=(
                    "No workspace is open. Ask the user to open one in the "
                    "WORKSPACE tab, or name it with workspace_id."
                ),
            )
        try:
            if action.action == "workspace_read":
                return ToolResult(
                    tool="workspace_read",
                    success=True,
                    data=_workspace_overview(self.workbench.get(workspace_id)),
                )
            if action.action == "workspace_plan":
                tasks = self.workbench.set_tasks(workspace_id, list(action.plan_steps))
                return ToolResult(
                    tool="workspace_plan",
                    success=True,
                    data={"tasks": [_task_line(item) for item in tasks]},
                )
            task = self.workbench.update_task(
                workspace_id,
                action.task_id,
                status=action.task_status,
                content=action.content or None,
            )
            return ToolResult(tool="workspace_task", success=True, data={"task": _task_line(task)})
        except WorkbenchError as exc:
            return ToolResult(tool=action.action, success=False, error=str(exc))

    def _design_action(self, action: AgentAction) -> ToolResult:
        """Create and granularly edit structured design artifacts.

        Editing is granular by design: the model adds/updates/deletes one node or
        edge per call so a small change never requires re-emitting the whole
        document, and manual GUI edits share the same stored artifact.
        """
        if self.design is None:
            return ToolResult(
                tool=action.action,
                success=False,
                error="The design workspace is not available in this context.",
            )
        name = action.action
        try:
            if name == "create_design":
                design = self.design.create(action.design_name, action.design_type)
            elif name == "read_design":
                design = self.design.get(action.design_id)
            elif name == "read_design_artifact":
                design = self.design.get(action.design_id)
                node = design.node(action.node_id)
                if node is None:
                    return ToolResult(
                        tool=name,
                        success=False,
                        error=f"Unknown node {action.node_id!r} in design {design.id!r}",
                    )
                content = str(node.data.get("content", ""))
                return ToolResult(
                    tool=name,
                    success=True,
                    data={
                        "design_id": design.id,
                        "node_id": node.id,
                        "label": node.label,
                        "kind": str(node.data.get("kind", "")),
                        "filename": str(node.data.get("filename", "")),
                        "content": content,
                        "content_chars": len(content),
                    },
                )
            elif name == "update_design":
                design = self.design.get(action.design_id)
                if action.design_name:
                    design.name = action.design_name
                    design = self.design.replace(design)
            elif name == "add_design_node":
                artifact = self._artifact_data(action)
                design = self.design.add_node(
                    action.design_id,
                    label=action.node_label,
                    node_type="artifact" if artifact else action.node_type,
                    node_id=action.node_id or None,
                    x=action.x,
                    y=action.y,
                    data=artifact,
                )
            elif name == "update_design_node":
                # Only the source is replaced; the card's kind, filename, and
                # geometry are the user's and must survive an agent rewrite.
                content = {"content": action.node_content} if action.node_content else None
                design = self.design.update_node(
                    action.design_id,
                    action.node_id,
                    label=action.node_label or None,
                    node_type=action.node_type if action.node_type != "default" else None,
                    x=action.x or None,
                    y=action.y or None,
                    data=content,
                )
            elif name == "delete_design_node":
                design = self.design.delete_node(action.design_id, action.node_id)
            elif name == "connect_design_nodes":
                design = self.design.connect(
                    action.design_id,
                    action.source_node,
                    action.target_node,
                    label=action.edge_label,
                )
            elif name == "disconnect_design_nodes":
                design = self.design.disconnect(
                    action.design_id,
                    edge_id=action.edge_id or None,
                    source=action.source_node or None,
                    target=action.target_node or None,
                )
            else:  # pragma: no cover - dispatch guards the set
                return ToolResult(tool=name, success=False, error="Unknown design operation")
        except DesignError as exc:
            return ToolResult(tool=name, success=False, error=str(exc))
        return ToolResult(
            tool=name,
            success=True,
            data={
                "design_id": design.id,
                "name": design.name,
                "type": design.type,
                "version": design.version,
                "nodes": self._summarize_nodes(design),
                "edges": [edge.model_dump(mode="json") for edge in design.edges],
            },
        )

    def _record_read(self, relative: str, result: ToolResult) -> None:
        normalized = self.editor.normalize(relative)
        total = int(result.data.get("total_lines", 0))
        first = int(result.data.get("start_line", 1))
        last = int(result.data.get("end_line", 0))
        if bool(result.data.get("complete")) or total == 0:
            self.editor.mark_seen(normalized)
            return
        ranges = self.read_ranges.setdefault(normalized, [])
        ranges.append((first, last))
        covered = 0
        for start, end in sorted(ranges):
            if start > covered + 1:
                break
            covered = max(covered, end)
        if covered >= total:
            self.editor.mark_seen(normalized)

    def _multi_edit(self, action: AgentAction) -> tuple[ToolResult, list[str]]:
        """Apply several exact replacements to one file, all or nothing.

        A large change is many small spans. Doing them one action at a time costs
        a model round trip each and leaves the file half-edited if the run stops
        in the middle; applying them together avoids both. If any anchor fails to
        match, nothing is written.
        """
        relative = self.editor.normalize(action.path)
        result = self.editor.multi_replace(relative, action.edits)
        return result, [relative] if result.success else []


class RecordingActionExecutor(ActionExecutor):
    """An executor that remembers each file's contents before the first change.

    Showing the user what actually changed needs the "before" text, and by the
    time an action has been applied it is gone. Snapshotting here rather than
    diffing the worktree at the end keeps the record exact even when the
    repository already had uncommitted edits, and when the agent rewrites the
    same file several times in one run.
    """

    #: Actions that can alter a file on disk.
    MUTATIONS = frozenset({"write", "replace", "delete", "multi_edit"})

    def __init__(
        self,
        editor: EditTools,
        commands: CommandRunner | None = None,
        *,
        web: WebResearch | None = None,
        memory: MemoryManager | None = None,
        memory_task_id: str | None = None,
        memory_session_id: str | None = None,
        design: DesignService | None = None,
        workbench: WorkbenchService | None = None,
        workspace_id: str = "",
    ) -> None:
        super().__init__(
            editor,
            commands,
            web=web,
            memory=memory,
            memory_task_id=memory_task_id,
            memory_session_id=memory_session_id,
            design=design,
            workbench=workbench,
            workspace_id=workspace_id,
        )
        #: Relative path -> contents before the agent touched it, or None when
        #: the file did not exist.
        self.before: dict[str, str | None] = {}
        #: The most recent successful mutation as (path, before, after). Each
        #: edit is shown as it lands, so this is the state either side of that
        #: one action rather than of the run as a whole.
        self.last_edit: tuple[str, str | None, str | None] | None = None

    async def execute(self, action: AgentAction) -> tuple[ToolResult, list[str]]:
        self.last_edit = None
        mutating = action.action in self.MUTATIONS and bool(action.path)
        if not mutating:
            return await super().execute(action)
        relative = self.editor.normalize(action.path)
        previous = _read_or_none(self.editor.root / relative)
        if relative not in self.before:
            self.before[relative] = previous
        result, paths = await super().execute(action)
        if result.success:
            self.last_edit = (relative, previous, self.after(relative))
        return result, paths

    def after(self, relative: str) -> str | None:
        return _read_or_none(self.editor.root / relative)


def _workspace_overview(workspace: Workspace) -> dict[str, Any]:
    """What the workspace holds, without its documents' bodies.

    Same discipline as ``_summarize_nodes``: an orientation call must not spend
    the context window on text the agent may not need. Every document is named
    with the path that ``read_file`` accepts, so following up is one step.
    """
    return {
        "workspace_id": workspace.id,
        "name": workspace.name,
        "goal": workspace.goal,
        "kind": workspace.kind,
        "folder": workspace.folder,
        "plan": [_task_line(item) for item in workspace.tasks],
        "documents": [
            {
                "path": item.repo_path,
                "title": item.title,
                # Already bounded by the service; no second clip needed.
                "preview": item.preview,
                "bytes": item.bytes,
            }
            for item in workspace.artifacts
        ],
        "uploads": [
            {
                "path": item.repo_path,
                # The extraction is what is readable; the original usually is not.
                "read_this_instead": item.extracted_path,
                "unreadable_because": item.warning,
            }
            for item in workspace.uploads
        ],
        "sources": [
            {"url": item.url, "title": item.title, "cached_text": item.cache_path}
            for item in workspace.sources
        ],
    }


def _task_line(task: WorkspaceTask) -> dict[str, Any]:
    return {"task_id": task.id, "content": task.content, "status": task.status}


def _read_or_none(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A binary or unreadable file has no textual diff to show; the change is
        # still reported by path.
        return None
