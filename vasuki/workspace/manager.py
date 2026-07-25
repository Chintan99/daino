"""Mission-specific branches, worktrees, and recoverable checkpoints."""

from __future__ import annotations

import hashlib
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

from vasuki.git import GitClient
from vasuki.utils.ids import new_id


@dataclass(frozen=True)
class Workspace:
    mission_id: str
    path: Path
    branch: str
    initial_revision: str
    original_status: str


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.git = GitClient(self.root)
        self.state_dir = self.root / ".vasuki"

    @staticmethod
    def _slug(description: str) -> str:
        cleaned = "".join(char.lower() if char.isalnum() else "-" for char in description)
        return "-".join(filter(None, cleaned.split("-")))[:40] or "mission"

    def create(self, mission_id: str, description: str, *, use_worktree: bool = True) -> Workspace:
        if not self.git.is_repository():
            raise RuntimeError("Coding missions require a Git repository")
        initial = self.git.revision()
        status = self.git.status()
        branch = f"vasuki/{mission_id}/{self._slug(description)}"
        if use_worktree:
            path = self.state_dir / "worktrees" / mission_id
            self.git.create_worktree(path, branch, initial)
        else:
            if status:
                raise RuntimeError(
                    "Cannot create an in-place mission branch with uncommitted changes"
                )
            self.git.run("switch", "-c", branch)
            path = self.root
        return Workspace(mission_id, path, branch, initial, status)

    def checkpoint(
        self, workspace: Workspace, description: str, *, mission_id: str | None = None
    ) -> tuple[str, Path]:
        checkpoint_id = new_id("checkpoint")
        destination = self.state_dir / "checkpoints" / f"{checkpoint_id}.tar.gz"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(destination, "w:gz") as archive:
            for path in workspace.path.rglob("*"):
                relative = path.relative_to(workspace.path)
                if (
                    path.is_file()
                    and ".git" not in relative.parts
                    and ".vasuki" not in relative.parts
                    and not path.is_symlink()
                ):
                    archive.add(path, arcname=relative)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        metadata = destination.with_suffix(".sha256")
        metadata.write_text(f"{digest}  {destination.name}\n{description}\n", encoding="utf-8")
        return checkpoint_id, destination

    def restore_checkpoint(self, archive_path: Path, target: Path) -> None:
        target = target.resolve()
        with tarfile.open(archive_path, "r:gz") as archive:
            for member in archive.getmembers():
                destination = (target / member.name).resolve()
                if not destination.is_relative_to(target):
                    raise ValueError("Unsafe path in checkpoint archive")
            archive.extractall(target, filter="data")

    def cleanup(self, workspace: Workspace, *, discard: bool = False) -> None:
        if workspace.path != self.root and workspace.path.exists():
            self.git.remove_worktree(workspace.path, force=discard)
        if discard:
            self.git.run("branch", "-D", workspace.branch, check=False)

    def detect_runtimes(self) -> dict[str, bool]:
        return {
            "local": True,
            "docker": shutil.which("docker") is not None,
            "ssh": shutil.which("ssh") is not None,
            "git": shutil.which("git") is not None,
        }
