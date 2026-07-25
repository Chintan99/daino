"""Controlled Git command-line integration."""

from __future__ import annotations

import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path

from vasuki.exceptions import WorkspaceError


@dataclass(frozen=True)
class GitResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


class GitClient:
    """Runs only explicit Git argument vectors, never a shell."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def run(self, *args: str, check: bool = True) -> GitResult:
        try:
            completed = subprocess.run(  # nosec B603, B607
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WorkspaceError(f"Git execution failed: {exc}") from exc
        result = GitResult(tuple(args), completed.returncode, completed.stdout, completed.stderr)
        if check and not result.succeeded:
            raise WorkspaceError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    def is_repository(self) -> bool:
        return self.run("rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true"

    def status(self, *, porcelain: bool = True) -> str:
        args = ("status", "--porcelain=v1") if porcelain else ("status",)
        return self.run(*args).stdout

    def revision(self, ref: str = "HEAD") -> str:
        return self.run("rev-parse", ref).stdout.strip()

    def current_branch(self) -> str:
        return self.run("branch", "--show-current").stdout.strip()

    def diff(self, *refs: str, staged: bool = False) -> str:
        args = ["diff"]
        if staged:
            args.append("--cached")
        args.extend(refs)
        return self.run(*args).stdout

    def log(self, limit: int = 10) -> str:
        return self.run("log", f"-{limit}", "--oneline", "--decorate").stdout

    def create_branch(self, name: str, start_point: str = "HEAD") -> None:
        self.run("branch", name, start_point)

    def create_worktree(self, path: Path, branch: str, start_point: str = "HEAD") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run("worktree", "add", "-b", branch, str(path), start_point)

    def remove_worktree(self, path: Path, *, force: bool = False) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(path))
        self.run(*args)

    def commit(self, message: str, paths: list[str] | None = None) -> str:
        if paths:
            self.run("add", "--", *paths)
        else:
            self.run("add", "-A")
        if not self.run("diff", "--cached", "--quiet", check=False).succeeded:
            self.run(
                "-c",
                "user.name=Vasuki",
                "-c",
                "user.email=vasuki@localhost",
                "commit",
                "-m",
                message,
            )
        return self.revision()

    def restore(self, revision: str, paths: list[str] | None = None) -> None:
        self.run("restore", "--source", revision, "--", *(paths or ["."]))

    def show(self, revision: str) -> str:
        return self.run("show", "--stat", "--patch", revision).stdout
