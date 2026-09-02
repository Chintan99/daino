"""Repository intelligence and safe file/diff presentation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from daino.application.context import ProjectContext
from daino.application.view_models import FileItem
from daino.events import ToolCompleted, ToolStarted
from daino.git import GitClient
from daino.repository import RepositoryIndexer

MAX_PREVIEW_BYTES = 1_000_000


def _line_count(path: Path) -> int:
    if not path.is_file():
        return 0
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


class RepositoryApplicationService:
    def __init__(self, context: ProjectContext) -> None:
        self.context = context
        self.indexer = RepositoryIndexer(context.root)

    def index(self) -> Any:
        self.context.events.publish(
            ToolStarted(tool="repository.index", summary="Indexing repository")
        )
        index = self.indexer.build()
        self.context.events.publish(
            ToolCompleted(
                tool="repository.index",
                summary=f"Indexed {len(index.files)} files",
            )
        )
        return index

    def summary(self) -> str:
        return self.indexer.summary()

    def files(self, query: str = "") -> list[FileItem]:
        lowered = query.lower().removeprefix("@file:").removeprefix("@symbol:")
        index = self.indexer.load()
        statuses = self.git_status()
        return [
            FileItem(
                path=item.path,
                language=item.language,
                status=statuses.get(item.path, ""),
                symbols=tuple(symbol.name for symbol in item.symbols),
            )
            for item in index.files
            if not lowered
            or lowered in item.path.lower()
            or any(lowered in symbol.name.lower() for symbol in item.symbols)
        ]

    def preview(self, relative_path: str) -> tuple[str, str]:
        target = (self.context.root / relative_path).resolve()
        if not target.is_relative_to(self.context.root) or not target.is_file():
            raise ValueError("File is outside the project or does not exist")
        if target.stat().st_size > MAX_PREVIEW_BYTES:
            raise ValueError("File is too large to preview")
        data = target.read_bytes()
        if b"\0" in data[:8192]:
            raise ValueError("Binary files cannot be previewed")
        language = next(
            (item.language for item in self.indexer.load().files if item.path == relative_path),
            "text",
        )
        return data.decode("utf-8"), language

    def git_status(self) -> dict[str, str]:
        try:
            output = GitClient(self.context.root).status()
        except Exception:
            return {}
        result: dict[str, str] = {}
        for line in output.splitlines():
            if len(line) >= 4:
                result[line[3:]] = line[:2].strip() or "M"
        return result

    def diff(self, *, staged: bool = False, mission_id: str | None = None) -> str:
        root = self.context.root
        refs: tuple[str, ...] = ()
        if mission_id:
            from daino.persistence.models import Mission

            with self.context.database.session() as session:
                mission = session.get(Mission, mission_id)
                if mission and mission.workspace_path:
                    root = Path(mission.workspace_path)
                    if mission.initial_revision:
                        refs = (mission.initial_revision,)
        return GitClient(root).diff(*refs, staged=staged)

    def has_git(self) -> bool:
        """Whether this directory is a Git repository Daino can diff against."""
        try:
            return GitClient(self.context.root).is_repository()
        except Exception:  # noqa: BLE001 - a missing git binary is not an error here
            return False

    def written_files(self, mission_id: str | None = None) -> list[dict[str, Any]]:
        """Files the agent wrote, taken from its own recorded tool calls.

        Without Git there is no diff to show, but there is still a truthful
        answer to "what changed": the edits are recorded as they happen, so the
        work can be listed even in a directory that was never initialized.
        """
        from daino.persistence.models import ToolCall

        mutations = {
            "chat.write",
            "chat.replace",
            "chat.multi_edit",
            "chat.delete",
            "chat.patch",
        }
        seen: dict[str, dict[str, Any]] = {}
        with self.context.database.session() as session:
            query = select(ToolCall).order_by(ToolCall.created_at)
            if mission_id:
                query = query.where(ToolCall.mission_id == mission_id)
            for row in session.scalars(query).all():
                if row.tool not in mutations or not row.success:
                    continue
                arguments = row.arguments if isinstance(row.arguments, dict) else {}
                relative = str(arguments.get("path") or "").strip()
                if not relative:
                    continue
                target = self.context.root / relative
                seen[relative] = {
                    "path": relative,
                    "action": row.tool.removeprefix("chat."),
                    "exists": target.is_file(),
                    "lines": _line_count(target),
                    "bytes": target.stat().st_size if target.is_file() else 0,
                }
        return sorted(seen.values(), key=lambda item: str(item["path"]))

    def find_symbol(self, name: str) -> list[Any]:
        return self.indexer.find_symbol(name)

    def find_references(self, name: str) -> list[dict[str, int | str]]:
        return self.indexer.find_references(name)

    def find_references_at(self, relative: str, line: int) -> list[dict[str, Any]]:
        """Textual occurrences of whatever identifier sits at ``relative:line``.

        The fallback for a project with no language server installed. It reads
        the identifier off the line and greps the index for it, which finds real
        uses and also finds the same word in a comment — so callers label it as
        text matching rather than as references. Never good enough to drive a
        rename; good enough to navigate.
        """
        import re

        target = (self.context.root / relative).resolve()
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return []
        if line < 1 or line > len(lines):
            return []
        # The longest identifier on the line is nearly always the one being
        # asked about — a def, class, or assignment target.
        names = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", lines[line - 1])
        if not names:
            return []
        wanted = max(names, key=len)
        return [
            {
                "path": str(item.get("path", "")),
                "line": int(item.get("line", 0)),
                "column": 1,
                "text": str(item.get("text", "")),
            }
            for item in self.indexer.find_references(wanted)
        ]

    def intelligence(self) -> dict[str, Any]:
        index = self.indexer.load()
        return {
            "languages": index.languages,
            "frameworks": index.frameworks,
            "entrypoints": index.entrypoints,
            "routes": self.indexer.api_routes(),
            "database_models": [
                item.model_dump(mode="json") for item in self.indexer.database_models()
            ],
            "tests": self.indexer.tests(),
            "dependencies": self.indexer.dependencies(),
            "generated_at": index.generated_at,
        }
