"""Terraform/OpenTofu validation, plan, apply, and guarded destroy."""

from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from daino.config import paths
from daino.exceptions import PolicyDenied
from daino.runtimes import LocalRuntime


class InfrastructureManager:
    def __init__(self, root: Path, runtime: LocalRuntime) -> None:
        self.root = root
        self.runtime = runtime
        self.binary = (
            "tofu" if shutil.which("tofu") else "terraform" if shutil.which("terraform") else ""
        )

    def detect(self) -> str:
        if not self.binary:
            raise RuntimeError("Neither OpenTofu nor Terraform is installed")
        if not any(self.root.rglob("*.tf")):
            raise RuntimeError("No Terraform/OpenTofu files detected")
        return self.binary

    async def validate(self) -> list[dict[str, Any]]:
        binary = self.detect()
        results = []
        for command in (f"{binary} fmt -check -recursive", f"{binary} validate"):
            result = await self.runtime.execute(command, approved=True)
            results.append(result.model_dump(mode="json"))
        return results

    async def plan(self) -> dict[str, Any]:
        binary = self.detect()
        plan_path = paths.state_dir(self.root) / "artifacts" / "infrastructure.tfplan"
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        command = f"{binary} plan -out {shlex.quote(str(plan_path))}"
        result = await self.runtime.execute(command, approved=True)
        changes = {"add": 0, "change": 0, "destroy": 0}
        match = re.search(
            r"Plan:\s+(\d+) to add,\s+(\d+) to change,\s+(\d+) to destroy", result.stdout
        )
        if match:
            changes = dict(zip(changes, map(int, match.groups()), strict=True))
        return {
            "result": result.model_dump(mode="json"),
            "plan_file": str(plan_path),
            "changes": changes,
            "destructive": changes["destroy"] > 0,
        }

    async def apply(self, *, approved: bool) -> dict[str, Any]:
        if not approved:
            raise PolicyDenied("Infrastructure apply requires explicit approval")
        binary = self.detect()
        plan_path = paths.state_dir(self.root) / "artifacts" / "infrastructure.tfplan"
        if not plan_path.exists():
            raise RuntimeError("No saved plan; run `daino infra plan` first")
        result = await self.runtime.execute(
            f"{binary} apply {shlex.quote(str(plan_path))}", approved=True
        )
        return result.model_dump(mode="json")

    async def destroy(self, *, approved: bool, confirmation: str) -> dict[str, Any]:
        if not approved or confirmation != "destroy":
            raise PolicyDenied("Destroy requires --approve and --confirm destroy")
        binary = self.detect()
        result = await self.runtime.execute(f"{binary} destroy -auto-approve", approved=True)
        return result.model_dump(mode="json")
