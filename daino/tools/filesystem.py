"""Workspace-confined file operations returning structured results."""

from __future__ import annotations

import re
import time
from pathlib import Path, PurePosixPath

from daino.config import paths
from daino.schemas import ToolResult

#: Directories never worth searching: version control internals, Daino's own
#: state, and the usual dependency and build caches.
_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".daino",
        ".vasuki",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".tox",
    }
)


#: What ``search_text`` alone skips: version control and Daino's own state.
#: Narrower than ``_IGNORED_DIRS`` on purpose — a literal substring search is
#: sometimes aimed at a dependency — and kept as it was.
_STATE_DIRS = frozenset({".git", ".daino", ".vasuki"})


def _ignored(relative: PurePosixPath | Path, ignored: frozenset[str] = _IGNORED_DIRS) -> bool:
    """Whether a repository-relative path is one a search should skip.

    Workspace documents live inside ``.daino`` — Daino's own state directory,
    and therefore ignored wholesale — but they are the user's writing, not
    Daino's bookkeeping. Searching has to reach them, or the workspace agent
    cannot grep the documents it just wrote.
    """
    if paths.in_workspaces(relative):
        return False
    return any(part in ignored for part in relative.parts)


def _undecodable(relative: str) -> str:
    """Explain a failed decode in terms of what to do about it.

    A bare ``UnicodeDecodeError`` tells a model nothing actionable, and models
    respond to it by retrying the same read. Workspace uploads are extracted to
    markdown beside the original, so for a document format the useful answer is
    the path of that extraction rather than the error.
    """
    from daino.workbench.extraction import (
        CONVERTIBLE_SUFFIXES,
        DOCUMENT_SUFFIXES,
        extracted_path,
    )

    path = PurePosixPath(relative)
    suffix = path.suffix.casefold()
    if suffix in DOCUMENT_SUFFIXES:
        return (
            f"{relative} is a {suffix} document, not text. Read "
            f"{extracted_path(path).as_posix()} instead — Daino extracts uploads to markdown."
        )
    if suffix in CONVERTIBLE_SUFFIXES:
        return (
            f"{relative} is a legacy binary format. Re-save it as "
            f"{CONVERTIBLE_SUFFIXES[suffix]} and it can be read."
        )
    return f"{relative} is not UTF-8 text and cannot be read."


class FileTools:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, relative: str, *, must_exist: bool = False) -> Path:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"Path escapes workspace: {relative}")
        if must_exist and not path.exists():
            raise FileNotFoundError(relative)
        return path

    def list_directory(self, relative: str = ".") -> ToolResult:
        started = time.monotonic()
        try:
            path = self._path(relative, must_exist=True)
            entries = [
                {"name": child.name, "type": "directory" if child.is_dir() else "file"}
                for child in sorted(path.iterdir())
            ]
            return ToolResult(
                tool="list_directory",
                success=True,
                data={"path": relative, "entries": entries},
                duration_seconds=time.monotonic() - started,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(tool="list_directory", success=False, error=str(exc))

    def read_file(
        self, relative: str, start: int | None = None, end: int | None = None
    ) -> ToolResult:
        started = time.monotonic()
        try:
            path = self._path(relative, must_exist=True)
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            first = start or 1
            last = min(end or len(lines), len(lines))
            selected = lines[first - 1 : last]
            return ToolResult(
                tool="read_file_range" if start or end else "read_file",
                success=True,
                data={
                    "path": relative,
                    "content": "".join(selected),
                    "lines": len(selected),
                    "total_lines": len(lines),
                    "start_line": first,
                    "end_line": last,
                    "complete": first <= 1 and last >= len(lines),
                },
                duration_seconds=time.monotonic() - started,
            )
        except UnicodeError:
            return ToolResult(tool="read_file", success=False, error=_undecodable(relative))
        except (OSError, ValueError) as exc:
            return ToolResult(tool="read_file", success=False, error=str(exc))

    def write_file(self, relative: str, content: str, *, create: bool = False) -> ToolResult:
        started = time.monotonic()
        try:
            path = self._path(relative)
            if create and path.exists():
                raise FileExistsError(relative)
            if not create and not path.exists():
                raise FileNotFoundError(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult(
                tool="create_file" if create else "write_file",
                success=True,
                data={"path": relative, "bytes": len(content.encode())},
                duration_seconds=time.monotonic() - started,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(tool="write_file", success=False, error=str(exc))

    def delete_file(self, relative: str) -> ToolResult:
        try:
            path = self._path(relative, must_exist=True)
            if not path.is_file():
                raise ValueError("Only individual files may be deleted")
            path.unlink()
            return ToolResult(tool="delete_file", success=True, data={"path": relative})
        except (OSError, ValueError) as exc:
            return ToolResult(tool="delete_file", success=False, error=str(exc))

    def move_file(self, source: str, destination: str) -> ToolResult:
        try:
            source_path = self._path(source, must_exist=True)
            destination_path = self._path(destination)
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.replace(destination_path)
            return ToolResult(
                tool="move_file",
                success=True,
                data={"source": source, "destination": destination},
            )
        except (OSError, ValueError) as exc:
            return ToolResult(tool="move_file", success=False, error=str(exc))

    def search_text(self, query: str) -> ToolResult:
        matches: list[dict[str, str | int]] = []
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root)
            if not path.is_file() or _ignored(relative, _STATE_DIRS):
                continue
            try:
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if query in line:
                        matches.append(
                            {
                                "path": path.relative_to(self.root).as_posix(),
                                "line": line_number,
                                "text": line.strip(),
                            }
                        )
            except (OSError, UnicodeError):
                continue
        return ToolResult(tool="search_text", success=True, data={"matches": matches})

    def glob_files(self, pattern: str, *, limit: int = 300) -> ToolResult:
        """List repository files matching a path pattern such as ``src/**/*.py``."""
        if not pattern.strip():
            return ToolResult(tool="glob", success=False, error="No pattern given.")
        matches: list[str] = []
        for path in sorted(self.root.glob(pattern.strip().lstrip("/"))):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root)
            if _ignored(relative):
                continue
            matches.append(relative.as_posix())
        truncated = len(matches) > limit
        return ToolResult(
            tool="glob",
            success=True,
            data={
                "pattern": pattern,
                "matches": matches[:limit],
                "count": len(matches),
                "truncated": truncated,
            },
        )

    def grep(self, expression: str, *, path_glob: str = "", limit: int = 200) -> ToolResult:
        """Search file contents by regular expression.

        ``search_text`` matches a literal substring, which cannot express "the
        definition of this function" or "any import of this module". This can.
        """
        try:
            regex = re.compile(expression)
        except re.error as exc:
            return ToolResult(
                tool="grep", success=False, error=f"Invalid regular expression: {exc}"
            )
        candidates = (
            self.root.glob(path_glob.strip().lstrip("/"))
            if path_glob.strip()
            else self.root.rglob("*")
        )
        matches: list[dict[str, str | int]] = []
        for path in sorted(candidates):
            if not path.is_file():
                continue
            relative = path.relative_to(self.root)
            if _ignored(relative):
                continue
            try:
                for line_number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1
                ):
                    if regex.search(line):
                        matches.append(
                            {
                                "path": relative.as_posix(),
                                "line": line_number,
                                "text": line.strip()[:300],
                            }
                        )
                        if len(matches) >= limit:
                            return ToolResult(
                                tool="grep",
                                success=True,
                                data={"matches": matches, "truncated": True},
                            )
            except (OSError, UnicodeError):
                continue
        return ToolResult(tool="grep", success=True, data={"matches": matches, "truncated": False})
