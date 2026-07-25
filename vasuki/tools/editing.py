"""Validated patch and symbol-level editing."""

from __future__ import annotations

import ast
import os
import subprocess  # nosec B404
import tempfile
from pathlib import Path

from vasuki.schemas import FileModification, ToolResult
from vasuki.tools.filesystem import FileTools


class EditTools:
    def __init__(self, root: Path, allowed_files: list[str] | None = None) -> None:
        self.root = root.resolve()
        self.files = FileTools(self.root)
        self.allowed_files = set(allowed_files or [])

    def _allowed(self, relative: str) -> bool:
        return not self.allowed_files or relative in self.allowed_files

    def apply_unified_diff(self, patch: str) -> ToolResult:
        touched = []
        for line in patch.splitlines():
            if line.startswith("+++ b/"):
                relative = line.removeprefix("+++ b/")
                if relative == "/dev/null":
                    continue
                path = (self.root / relative).resolve()
                if not path.is_relative_to(self.root) or not self._allowed(relative):
                    return ToolResult(
                        tool="apply_unified_diff",
                        success=False,
                        error=f"Patch touches disallowed path {relative}",
                    )
                touched.append(relative)
        if not touched:
            return ToolResult(
                tool="apply_unified_diff", success=False, error="Patch has no target files"
            )
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".patch", encoding="utf-8", delete=False
            ) as handle:
                handle.write(patch)
                handle.flush()
                patch_path = handle.name
                check = subprocess.run(  # nosec B603, B607
                    ["git", "apply", "--check", patch_path],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if check.returncode != 0:
                    return ToolResult(
                        tool="apply_unified_diff", success=False, error=check.stderr.strip()
                    )
                applied = subprocess.run(  # nosec B603, B607
                    ["git", "apply", patch_path],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            try:
                if applied.returncode != 0:
                    return ToolResult(
                        tool="apply_unified_diff", success=False, error=applied.stderr.strip()
                    )
                syntax_error = self._validate_python(touched)
                if syntax_error:
                    subprocess.run(  # nosec B603, B607
                        ["git", "apply", "--reverse", patch_path],
                        cwd=self.root,
                        capture_output=True,
                        check=False,
                    )
                    return ToolResult(tool="apply_unified_diff", success=False, error=syntax_error)
                return ToolResult(tool="apply_unified_diff", success=True, data={"files": touched})
            finally:
                os.unlink(patch_path)
        except (OSError, subprocess.SubprocessError) as exc:
            return ToolResult(tool="apply_unified_diff", success=False, error=str(exc))

    def apply_modification(self, modification: FileModification) -> ToolResult:
        if not self._allowed(modification.path):
            return ToolResult(
                tool="apply_modification",
                success=False,
                error=f"Path outside allowed task scope: {modification.path}",
            )
        if modification.action == "patch":
            if not modification.unified_diff:
                return ToolResult(tool="apply_modification", success=False, error="Missing diff")
            return self.apply_unified_diff(modification.unified_diff)
        if modification.action == "create":
            if modification.content is None:
                return ToolResult(tool="apply_modification", success=False, error="Missing content")
            result = self.files.write_file(modification.path, modification.content, create=True)
            if result.success:
                syntax_error = self._validate_python([modification.path])
                if syntax_error:
                    self.files.delete_file(modification.path)
                    return ToolResult(tool="apply_modification", success=False, error=syntax_error)
            return result
        return self.files.delete_file(modification.path)

    def replace_symbol(self, relative: str, symbol: str, replacement: str) -> ToolResult:
        if not self._allowed(relative):
            return ToolResult(tool="replace_symbol", success=False, error="Path not allowed")
        path = self.root / relative
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
            candidates = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name == symbol
            ]
            if len(candidates) != 1:
                return ToolResult(
                    tool="replace_symbol",
                    success=False,
                    error=f"Expected exactly one symbol {symbol}; found {len(candidates)}",
                )
            node = candidates[0]
            lines = text.splitlines(keepends=True)
            updated = "".join(
                lines[: node.lineno - 1] + [replacement.rstrip() + "\n"] + lines[node.end_lineno :]
            )
            ast.parse(updated)
            path.write_text(updated, encoding="utf-8")
            return ToolResult(
                tool="replace_symbol", success=True, data={"path": relative, "symbol": symbol}
            )
        except (OSError, SyntaxError) as exc:
            return ToolResult(tool="replace_symbol", success=False, error=str(exc))

    def _validate_python(self, paths: list[str]) -> str | None:
        for relative in paths:
            if not relative.endswith((".py", ".pyi")):
                continue
            try:
                ast.parse((self.root / relative).read_text(encoding="utf-8"))
            except (OSError, SyntaxError) as exc:
                return f"Python syntax validation failed for {relative}: {exc}"
        return None
