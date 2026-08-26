"""Hierarchical DAINO.md discovery with explicit scope and precedence.

Legacy ``VASUKI.md`` files are still discovered when no ``DAINO.md`` is present
at the same level, so a repository written for Vasuki keeps its instructions.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from daino.config import paths
from daino.memory.types import EffectiveInstructions

MAX_INSTRUCTION_BYTES = 128_000


def global_memory_dir() -> Path:
    """Return the private user-memory directory (``~/.daino``, legacy ``~/.vasuki``)."""
    return paths.global_memory_dir()


def global_instruction_path() -> Path:
    return paths.global_instruction_path()


def _instruction_in(directory: Path) -> Path:
    """The instruction file to use in ``directory`` — ``DAINO.md`` preferred."""
    daino_file = directory / paths.INSTRUCTION_FILENAME
    legacy_file = directory / paths.LEGACY_INSTRUCTION_FILENAME
    if not daino_file.exists() and legacy_file.exists():
        return legacy_file
    return daino_file


class InstructionResolver:
    """Resolve global, repository, and closest-directory procedural memory."""

    def __init__(self, root: Path, *, global_path: Path | None = None) -> None:
        self.root = root.resolve()
        self.global_path = (global_path or global_instruction_path()).expanduser().resolve()

    @staticmethod
    def _read(path: Path) -> str:
        try:
            if not path.is_file() or path.stat().st_size > MAX_INSTRUCTION_BYTES:
                return ""
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""

    def _scoped_paths(self, target: str) -> list[Path]:
        relative = PurePosixPath(target.strip().replace("\\", "/").lstrip("/"))
        candidate = (self.root / relative).resolve()
        if not candidate.is_relative_to(self.root):
            return []
        directory = candidate if candidate.is_dir() else candidate.parent
        discovered: list[Path] = []
        cursor = self.root
        discovered.append(_instruction_in(cursor))
        try:
            relative_directory = directory.relative_to(self.root)
        except ValueError:
            return discovered
        for part in relative_directory.parts:
            cursor /= part
            discovered.append(_instruction_in(cursor))
        return discovered

    @staticmethod
    def _deduplicate_keyed_rules(layers: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Drop overridden key/value rules while preserving non-keyed prose.

        Files remain separate precedence layers. For explicit ``name: value`` or
        ``name = value`` directives, the closest layer replaces the broader one
        instead of both conflicting values being sent to the model.
        """
        winning: dict[str, tuple[int, int]] = {}
        split_lines: list[list[str]] = []
        for layer_index, (_, content) in enumerate(layers):
            lines = content.splitlines()
            split_lines.append(lines)
            for line_index, line in enumerate(lines):
                stripped = line.strip().lstrip("-* ")
                separator = ":" if ":" in stripped else "=" if "=" in stripped else ""
                if not separator:
                    continue
                key, value = stripped.split(separator, 1)
                normalized = " ".join(key.casefold().split())
                if normalized and value.strip() and len(normalized) <= 80:
                    winning[normalized] = (layer_index, line_index)

        resolved: list[tuple[str, str]] = []
        for layer_index, (label, _) in enumerate(layers):
            kept: list[str] = []
            for line_index, line in enumerate(split_lines[layer_index]):
                stripped = line.strip().lstrip("-* ")
                separator = ":" if ":" in stripped else "=" if "=" in stripped else ""
                if separator:
                    key, value = stripped.split(separator, 1)
                    normalized = " ".join(key.casefold().split())
                    if value.strip() and winning.get(normalized) != (layer_index, line_index):
                        continue
                kept.append(line)
            content = "\n".join(kept).strip()
            if content:
                resolved.append((label, content))
        return resolved

    def resolve(
        self,
        paths: list[str] | None = None,
        *,
        user_instruction: str = "",
    ) -> EffectiveInstructions:
        target_paths = list(dict.fromkeys(paths or []))
        layers: list[tuple[str, str]] = []
        sources: list[str] = []
        scopes: dict[str, list[str]] = {}

        global_text = self._read(self.global_path)
        if global_text:
            layers.append(("global", global_text))
            sources.append(str(self.global_path))
            scopes[str(self.global_path)] = ["*"]

        scoped: dict[Path, list[str]] = {}
        for target in target_paths or ["."]:
            for path in self._scoped_paths(target):
                scoped.setdefault(path, []).append(target)
        # Parents first, closest directories last. A rule in a later block wins.
        for path in sorted(scoped, key=lambda item: len(item.relative_to(self.root).parts)):
            content = self._read(path)
            if not content:
                continue
            relative = path.relative_to(self.root).as_posix()
            label = "repository" if path.parent == self.root else f"scoped:{relative}"
            layers.append((label, content))
            sources.append(str(path))
            scopes[str(path)] = sorted(set(scoped[path]))

        layers = self._deduplicate_keyed_rules(layers)
        rendered = [
            "DAINO.md instructions are scoped precedence layers. Later/closer layers override "
            "broader layers on conflict; the current user request and repository source remain "
            "authoritative over every layer."
        ]
        for label, content in layers:
            rendered.append(f"\n[{label}]\n{content}")
        if user_instruction.strip():
            rendered.append(f"\n[current explicit user instruction]\n{user_instruction.strip()}")
        return EffectiveInstructions(
            text="\n".join(rendered) if layers or user_instruction.strip() else "",
            sources=sources,
            scopes=scopes,
        )
