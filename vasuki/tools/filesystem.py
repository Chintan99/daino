"""Workspace-confined file operations returning structured results."""

from __future__ import annotations

import time
from pathlib import Path

from vasuki.schemas import ToolResult


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
            selected = lines[(start - 1 if start else 0) : end]
            return ToolResult(
                tool="read_file_range" if start or end else "read_file",
                success=True,
                data={"path": relative, "content": "".join(selected), "lines": len(selected)},
                duration_seconds=time.monotonic() - started,
            )
        except (OSError, UnicodeError, ValueError) as exc:
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
            if not path.is_file() or any(part in {".git", ".vasuki"} for part in relative.parts):
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
