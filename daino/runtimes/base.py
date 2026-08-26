"""Execution runtime abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from daino.schemas import CommandResult


class Runtime(ABC):
    @abstractmethod
    async def prepare(self) -> None: ...

    @abstractmethod
    async def execute(
        self, command: str, *, timeout: int | None = None, approved: bool = False
    ) -> CommandResult: ...

    @abstractmethod
    async def read_file(self, path: str) -> bytes: ...

    @abstractmethod
    async def write_file(self, path: str, content: bytes) -> None: ...

    @abstractmethod
    async def upload(self, local: Path, remote: str) -> None: ...

    @abstractmethod
    async def download(self, remote: str, local: Path) -> None: ...

    @abstractmethod
    async def start_service(self, name: str) -> CommandResult: ...

    @abstractmethod
    async def stop_service(self, name: str) -> CommandResult: ...

    @abstractmethod
    async def inspect(self) -> dict[str, Any]: ...

    @abstractmethod
    async def checkpoint(self, name: str) -> str: ...

    @abstractmethod
    async def cleanup(self) -> None: ...
