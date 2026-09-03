"""Controlled Git command-line integration."""

from __future__ import annotations

import hashlib
import re
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

    def run(self, *args: str, check: bool = True, stdin: str | None = None) -> GitResult:
        """Run one Git argument vector. Never a shell, never a string command.

        ``stdin`` feeds a patch to ``git apply``, which is the only way to stage
        part of a file — there is no plumbing command that takes a line range.
        """
        try:
            completed = subprocess.run(  # nosec B603, B607
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                input=stdin,
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

    def checkout_fingerprint(self) -> dict[str, str | bool]:
        """Identify exactly which working tree a report was taken from.

        A verdict is only ever true of the code it looked at. Storing the commit
        alone is not enough — most inspections run against an uncommitted tree —
        so the digest folds in the porcelain status and the tracked diff as
        well. Two runs that agree on this fingerprint looked at the same bytes;
        any change to them retires the earlier verdict rather than letting it
        stand over code nobody inspected.

        Never raises: a report is still worth saving in a directory Git cannot
        read, it just cannot be pinned to a checkout.
        """
        if not self.is_repository():
            return {"commit": "", "branch": "", "digest": "", "dirty": False}
        commit = self.run("rev-parse", "HEAD", check=False).stdout.strip()
        branch = self.run("branch", "--show-current", check=False).stdout.strip()
        status = self.run("status", "--porcelain=v1", check=False).stdout
        # ``diff HEAD`` covers tracked edits, staged and unstaged alike.
        tracked = self.run("diff", "HEAD", check=False).stdout if commit else ""
        payload = "\0".join((commit, status, tracked, self._untracked_stamp())).encode(
            "utf-8", "replace"
        )
        return {
            "commit": commit,
            "branch": branch,
            "digest": hashlib.sha256(payload).hexdigest(),
            "dirty": bool(status.strip()),
        }

    def _untracked_stamp(self) -> str:
        """Size and mtime of every untracked file, so editing one is noticed.

        The porcelain listing names untracked files but says nothing about their
        contents, which would let "write the secret into a new file, then push"
        keep a passing verdict. Stat rather than hash: this runs on every poll of
        the verdict badge, and size-plus-mtime is the same cheap staleness signal
        Git's own index uses.

        ``ls-files -z`` rather than parsing the porcelain, because that quotes
        and escapes unusual paths — and a path this misreads is one whose edits
        would go unnoticed. ``--exclude-standard`` means an ignored tree such as
        node_modules is never walked.
        """
        listing = self.run("ls-files", "--others", "--exclude-standard", "-z", check=False)
        stamps: list[str] = []
        for relative in listing.stdout.split("\0"):
            if not relative:
                continue
            try:
                stat = (self.root / relative).stat()
            except OSError:
                stamps.append(f"{relative}:gone")
                continue
            stamps.append(f"{relative}:{stat.st_size}:{stat.st_mtime_ns}")
        return "\n".join(stamps)

    # --------------------------------------------------------------- branches

    def branches(self) -> list[dict[str, str | bool]]:
        """Local branches, with upstream tracking and how far each has drifted.

        The ahead/behind pair is what makes "push" a decision rather than a
        guess: a branch 3 behind its upstream needs a pull first, and the UI can
        only say so if it knows.
        """
        result = self.run(
            "for-each-ref",
            "--format=%(refname:short)%09%(upstream:short)%09%(upstream:track)"
            "%09%(HEAD)%09%(objectname:short)%09%(contents:subject)",
            "refs/heads",
            check=False,
        )
        if not result.succeeded:
            return []
        found: list[dict[str, str | bool]] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            while len(parts) < 6:
                parts.append("")
            name, upstream, track, head, commit, subject = parts[:6]
            ahead, behind = _parse_track(track)
            found.append(
                {
                    "name": name,
                    "upstream": upstream,
                    "current": head.strip() == "*",
                    "ahead": ahead,
                    "behind": behind,
                    "gone": "gone" in track,
                    "commit": commit,
                    "subject": subject,
                }
            )
        return found

    def remote_branches(self) -> list[str]:
        result = self.run("for-each-ref", "--format=%(refname:short)", "refs/remotes", check=False)
        if not result.succeeded:
            return []
        return [
            line.strip()
            for line in result.stdout.splitlines()
            # `origin/HEAD` is a symbolic pointer, not somewhere to check out.
            if line.strip() and not line.strip().endswith("/HEAD")
        ]

    def remotes(self) -> list[dict[str, str]]:
        result = self.run("remote", "-v", check=False)
        if not result.succeeded:
            return []
        seen: dict[str, str] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                seen.setdefault(parts[0], parts[1])
        return [{"name": name, "url": url} for name, url in seen.items()]

    def checkout(self, ref: str, *, create: bool = False, start: str = "") -> GitResult:
        """Switch branches, optionally creating one.

        ``switch`` rather than ``checkout``: it only ever changes branches, so a
        ref that happens to share a name with a file cannot be read as "discard
        my changes to that file" — which is the accident ``checkout`` is famous
        for.
        """
        if create:
            args = ["switch", "-c", ref, *([start] if start else [])]
        else:
            args = ["switch", ref]
        return self.run(*args, check=False)

    def delete_branch(self, name: str, *, force: bool = False) -> GitResult:
        return self.run("branch", "-D" if force else "-d", name, check=False)

    # ----------------------------------------------------------------- remotes

    def fetch(self, remote: str = "", *, prune: bool = True) -> GitResult:
        args = ["fetch"]
        if prune:
            args.append("--prune")
        args.append(remote or "--all")
        return self.run(*args, check=False)

    def pull(self, *, rebase: bool = False) -> GitResult:
        args = ["pull", "--rebase" if rebase else "--no-rebase"]
        return self.run(*args, check=False)

    def push(self, *, remote: str = "", branch: str = "", set_upstream: bool = False) -> GitResult:
        args = ["push"]
        if set_upstream:
            args.append("--set-upstream")
        if remote:
            args.append(remote)
            if branch:
                args.append(branch)
        return self.run(*args, check=False)

    # ------------------------------------------------------------------ merge

    def merge(self, ref: str, *, no_commit: bool = False) -> GitResult:
        args = ["merge", "--no-ff" if no_commit else "--no-edit"]
        if no_commit:
            args.append("--no-commit")
        args.append(ref)
        return self.run(*args, check=False)

    def merge_abort(self) -> GitResult:
        return self.run("merge", "--abort", check=False)

    def conflicts(self) -> list[str]:
        """Paths Git has left with conflict markers, in order."""
        result = self.run("diff", "--name-only", "--diff-filter=U", check=False)
        if not result.succeeded:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def merge_state(self) -> dict[str, object]:
        """Whether a merge, rebase, or cherry-pick is in progress, and on what.

        Read from the git directory rather than inferred from the presence of
        conflicts: a merge with every conflict resolved is still an unfinished
        merge, and offering "commit" without saying so is how people commit a
        half-done merge.
        """
        git_dir = self.run("rev-parse", "--git-dir", check=False).stdout.strip()
        base = (self.root / git_dir) if git_dir else (self.root / ".git")
        merging = (base / "MERGE_HEAD").is_file()
        rebasing = (base / "rebase-merge").is_dir() or (base / "rebase-apply").is_dir()
        cherry = (base / "CHERRY_PICK_HEAD").is_file()
        message = ""
        if merging and (base / "MERGE_MSG").is_file():
            try:
                message = (base / "MERGE_MSG").read_text(encoding="utf-8")
            except OSError:
                message = ""
        return {
            "merging": merging,
            "rebasing": rebasing,
            "cherry_picking": cherry,
            "message": message,
            "conflicts": self.conflicts(),
        }

    def conflict_stage(self, path: str, stage: int) -> str | None:
        """One side of a conflict: 1 = base, 2 = ours, 3 = theirs.

        Returned rather than written, so a three-way view can show all three
        without touching the file the user is editing.
        """
        result = self.run("show", f":{stage}:{path}", check=False)
        return result.stdout if result.succeeded else None

    def resolve_with(self, path: str, side: str) -> GitResult:
        """Take one whole side of a conflict for this file.

        `--ours` and `--theirs` mean what they say during a merge and are
        *reversed* during a rebase, which is a well-known way to lose work.
        Rather than encode that trap, the caller is given the two sides by
        content (:2 and :3) and this writes the one it asked for by index.
        """
        stage = 2 if side == "ours" else 3
        content = self.conflict_stage(path, stage)
        if content is None:
            return GitResult(
                command=("show", f":{stage}:{path}"),
                returncode=1,
                stdout="",
                stderr=f"{path} has no {side} side — it was deleted on that side.",
            )
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return self.run("add", "--", path, check=False)

    # -------------------------------------------------------------- committing

    def commit_message_of(self, ref: str = "HEAD") -> str:
        result = self.run("log", "-1", "--format=%B", ref, check=False)
        return result.stdout.strip() if result.succeeded else ""

    def commit_staged(
        self,
        message: str,
        *,
        amend: bool = False,
        sign_off: bool = False,
        allow_empty: bool = False,
    ) -> GitResult:
        """Commit exactly what is staged. Never stages anything itself.

        The difference from :meth:`commit` matters: that one is the agent's
        (``git add -A`` then commit, for a mission checkpoint), and this one is
        the user's. A commit button that quietly staged the rest of the working
        tree would be the single most surprising thing in the product.
        """
        args = ["commit", "-m", message]
        if amend:
            args.append("--amend")
        if sign_off:
            args.append("--signoff")
        if allow_empty:
            args.append("--allow-empty")
        return self.run(*args, check=False)

    def apply_patch(self, patch: str, *, cached: bool = True, reverse: bool = False) -> GitResult:
        """Apply a patch, optionally to the index only, optionally backwards.

        This is how partial staging works: a patch containing only the chosen
        hunks, applied with ``--cached`` to move them into the index without
        touching the working tree.
        """
        args = ["apply"]
        if cached:
            args.append("--cached")
        if reverse:
            args.append("--reverse")
        # Whitespace errors in someone else's code must not block staging it.
        args.extend(["--whitespace=nowarn", "--unidiff-zero", "-"])
        return self.run(*args, check=False, stdin=patch)


def _parse_track(track: str) -> tuple[int, int]:
    """Read `[ahead 2, behind 1]` into a pair."""
    ahead = re.search(r"ahead (\d+)", track)
    behind = re.search(r"behind (\d+)", track)
    return (int(ahead.group(1)) if ahead else 0, int(behind.group(1)) if behind else 0)
