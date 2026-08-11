"""Stable repository identity and source-version metadata."""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    project_id: str
    root: Path
    remote: str = ""
    branch: str = ""
    revision: str = ""


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(  # nosec B603, B607
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def normalize_remote(remote: str) -> str:
    """Normalize common Git URL spellings without retaining credentials."""
    value = remote.strip().removesuffix("/").removesuffix(".git")
    if "@" in value and "://" in value:
        # Strip user-info (including an accidentally credentialed HTTPS URL).
        scheme, rest = value.split("://", 1)
        value = f"{scheme}://{rest.split('@', 1)[-1]}"
    if value.startswith("git@") and ":" in value:
        host, path = value.split(":", 1)
        value = f"ssh://{host.split('@', 1)[-1]}/{path}"
    return value.casefold()


def identify_repository(root: Path) -> RepositoryIdentity:
    """Identify a project by its Git remote when possible, path otherwise.

    The hash prevents unrelated repositories with the same basename from
    sharing memories. A remote-backed checkout keeps its identity when moved;
    a non-Git directory remains stable at its resolved location.
    """
    resolved = root.resolve()
    inside = _git(resolved, "rev-parse", "--is-inside-work-tree") == "true"
    remote = normalize_remote(_git(resolved, "remote", "get-url", "origin")) if inside else ""
    seed = f"git:{remote}" if remote else f"path:{resolved.as_posix()}"
    project_id = f"project-{hashlib.sha256(seed.encode()).hexdigest()[:32]}"
    return RepositoryIdentity(
        project_id=project_id,
        root=resolved,
        remote=remote,
        branch=_git(resolved, "branch", "--show-current") if inside else "",
        revision=_git(resolved, "rev-parse", "HEAD") if inside else "",
    )


def file_digest(root: Path, source: str) -> str | None:
    """Return the current digest for a repository source, if it is a safe file."""
    candidate = (root / source).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        return None
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return None
