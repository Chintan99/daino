"""Controlled Git command-line integration."""

from __future__ import annotations

import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path

from daino.exceptions import WorkspaceError


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

    def ensure_repository(self) -> bool:
        """Initialize a repository here if there is not one, and report success.

        A directory that was never ``git init``-ed is not a user error worth
        refusing a mission over: checkpoints and diffs simply need a revision to
        anchor to, and one can be created. Returns ``False`` only when Git
        itself is unusable, which callers handle by working without it.
        """
        try:
            if self.is_repository():
                if not self.run("rev-parse", "--verify", "HEAD", check=False).succeeded:
                    self.commit("Initialize project")
                return True
            if not self.run("init", "-b", "main", check=False).succeeded:
                return False
            self.commit("Initialize project")
            return True
        except (WorkspaceError, OSError):
            return False

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
        has_head = self.run("rev-parse", "--verify", "HEAD", check=False).succeeded
        if paths:
            self.run("add", "--", *paths)
        else:
            self.run("add", "-A")
        staged_changes = not self.run("diff", "--cached", "--quiet", check=False).succeeded
        if staged_changes or not has_head:
            args = [
                "-c",
                "user.name=Daino",
                "-c",
                "user.email=daino@localhost",
                "commit",
            ]
            if not has_head and not staged_changes:
                args.append("--allow-empty")
            self.run(*args, "-m", message)
        return self.revision()

    def restore(self, revision: str, paths: list[str] | None = None) -> None:
        self.run("restore", "--source", revision, "--", *(paths or ["."]))

    def show(self, revision: str) -> str:
        return self.run("show", "--stat", "--patch", revision).stdout

    def file_at(self, ref: str, relative: str) -> str | None:
        """One file's content at a revision, or None when it is not there.

        A review of a branch has to read files as that branch has them, not as
        the working tree happens to have them.
        """
        result = self.run("show", f"{ref}:{relative}", check=False)
        return result.stdout if result.succeeded else None

    def untracked_files(self) -> list[str]:
        """New files, honouring .gitignore.

        ``git diff`` never mentions them, so anything reviewing a working tree
        has to ask separately or it silently skips every file just created —
        which is usually the part most worth reading.
        """
        result = self.run("ls-files", "--others", "--exclude-standard", check=False)
        if not result.succeeded:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def merge_base(self, base: str, head: str = "HEAD") -> str:
        """Where two refs diverged, or "" when they share no history."""
        result = self.run("merge-base", base, head, check=False)
        return result.stdout.strip() if result.succeeded else ""

    def default_base_ref(self) -> str:
        """The branch a change would most likely be proposed against.

        Prefers what the remote itself calls its default, because a repository
        whose trunk is neither ``main`` nor ``master`` is common enough that
        guessing those two first would be wrong for it every time.
        """
        remote = self.run("symbolic-ref", "refs/remotes/origin/HEAD", check=False)
        if remote.succeeded and remote.stdout.strip():
            return remote.stdout.strip().removeprefix("refs/remotes/")
        current = self.current_branch()
        for candidate in ("origin/main", "origin/master", "main", "master", "develop"):
            if candidate.removeprefix("origin/") == current:
                continue
            if self.run("rev-parse", "--verify", candidate, check=False).succeeded:
                return candidate
        return ""

    def range_subjects(self, base: str, head: str = "HEAD", limit: int = 50) -> list[str]:
        """Commit subjects in ``base..head``, newest first."""
        result = self.run("log", f"-{limit}", "--format=%s", f"{base}..{head}", check=False)
        if not result.succeeded:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
