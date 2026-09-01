"""Workspaces: goals, documents, research, and tasks for general knowledge work.

Named ``workbench`` rather than ``workspace`` because :mod:`daino.workspace` is
already the git-worktree and checkpoint manager for missions. Everything the
user sees calls this a Workspace.
"""

from daino.workbench.extraction import (
    DOCUMENT_SUFFIXES,
    TEXT_SUFFIXES,
    Extraction,
    ExtractionError,
    extract,
    extract_to_cache,
    extracted_path,
    missing_extra_message,
    needs_extraction,
    supported_suffixes,
)

__all__ = [
    "DOCUMENT_SUFFIXES",
    "TEXT_SUFFIXES",
    "Extraction",
    "ExtractionError",
    "extract",
    "extract_to_cache",
    "extracted_path",
    "missing_extra_message",
    "needs_extraction",
    "supported_suffixes",
]
