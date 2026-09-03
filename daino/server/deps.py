"""Shared FastAPI dependencies and helpers for GUI routes."""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import HTTPException, Request

from daino.server.state import GuiState

#: Extension → Monaco language id for the editor and syntax highlighting.
_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".json": "json",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".sh": "shell",
    ".bash": "shell",
    ".sql": "sql",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".xml": "xml",
    ".txt": "plaintext",
    ".cfg": "ini",
    ".ini": "ini",
    ".dockerfile": "dockerfile",
    ".tsx.": "typescript",
}


def get_state(request: Request) -> GuiState:
    state = getattr(request.app.state, "gui", None)
    if state is None:  # pragma: no cover - server always sets this
        raise HTTPException(status_code=503, detail="GUI state is not initialised")
    return state


def language_for(path: Path) -> str:
    if path.name.lower() == "dockerfile":
        return "dockerfile"
    return _LANGUAGES.get(path.suffix.lower(), "plaintext")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def safe_path(state: GuiState, relative: str, *, must_exist: bool = False) -> Path:
    """Resolve ``relative`` inside the project root, rejecting escapes."""
    target = (state.root / relative).resolve()
    if target != state.root and state.root not in target.parents:
        raise HTTPException(status_code=400, detail="Path escapes the project workspace")
    if must_exist and not target.exists():
        raise HTTPException(status_code=404, detail=f"Not found: {relative}")
    return target
