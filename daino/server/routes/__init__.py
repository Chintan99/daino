"""FastAPI routers for the Daino GUI backend."""

from daino.server.routes import (
    agent,
    debug,
    design,
    files,
    git,
    lsp,
    preview,
    tasks,
    terminal,
    tests,
)

__all__ = [
    "agent",
    "debug",
    "design",
    "files",
    "git",
    "lsp",
    "preview",
    "tasks",
    "terminal",
    "tests",
]
