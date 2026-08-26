"""Adapter boundary for optional language-server intelligence."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from daino.schemas import RepositorySymbol


class LSPAdapter(ABC):
    """Optional richer semantic queries; no language server is bundled."""

    @abstractmethod
    async def start(self, root: Path) -> None: ...

    @abstractmethod
    async def symbols(self, path: Path) -> list[RepositorySymbol]: ...

    @abstractmethod
    async def references(self, path: Path, line: int, column: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def implementations(self, path: Path, line: int, column: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def close(self) -> None: ...
