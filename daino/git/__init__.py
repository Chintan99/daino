"""Controlled Git integration: one argument vector at a time, never a shell."""

from daino.git import hunks
from daino.git.client import GitClient, GitResult

__all__ = ["GitClient", "GitResult", "hunks"]
