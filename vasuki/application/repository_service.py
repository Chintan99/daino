"""Repository intelligence and safe file/diff presentation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vasuki.application.context import ProjectContext
from vasuki.application.view_models import FileItem
from vasuki.events import ToolCompleted, ToolStarted
from vasuki.git import GitClient
from vasuki.repository import RepositoryIndexer

MAX_PREVIEW_BYTES = 1_000_000


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
            from vasuki.persistence.models import Mission

            with self.context.database.session() as session:
                mission = session.get(Mission, mission_id)
                if mission and mission.workspace_path:
                    root = Path(mission.workspace_path)
                    if mission.initial_revision:
                        refs = (mission.initial_revision,)
        return GitClient(root).diff(*refs, staged=staged)

    def find_symbol(self, name: str) -> list[Any]:
        return self.indexer.find_symbol(name)

    def find_references(self, name: str) -> list[dict[str, int | str]]:
        return self.indexer.find_references(name)

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
