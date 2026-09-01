"""Detect and run a project's development server for the Inspector's Live view.

This is separate from Design: the Live view runs the *actual* project, and the
URL it lands on is what the Inspector's live probe is pointed at. Candidate
commands are detected from ``package.json`` / ``pyproject.toml`` /
``compose.yaml``; starting one goes through the GUI's normal approval flow
before this manager launches it. Output is captured (bounded) and the guessed
URL is reported for iframe embedding.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

_URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)(?::\d+)?[^\s]*")
_LOG_LINES = 500


@dataclass
class PreviewCommand:
    label: str
    command: str
    source: str
    default_url: str = ""


@dataclass
class PreviewProcess:
    command: str
    process: subprocess.Popen
    url: str = ""
    logs: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_LINES))

    @property
    def running(self) -> bool:
        return self.process.poll() is None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def detect_preview_commands(root: Path) -> list[PreviewCommand]:
    """Return candidate dev-server commands discovered from project manifests."""
    commands: list[PreviewCommand] = []
    package = root / "package.json"
    if package.is_file():
        data = _read_json(package)
        scripts = data.get("scripts", {}) if isinstance(data.get("scripts"), dict) else {}
        manager = "npm"
        if (root / "pnpm-lock.yaml").exists():
            manager = "pnpm"
        elif (root / "yarn.lock").exists():
            manager = "yarn"
        for name in ("dev", "start", "serve", "preview"):
            if name in scripts:
                run = "run " if manager != "yarn" else ""
                commands.append(
                    PreviewCommand(
                        label=f"{manager} {name}",
                        command=f"{manager} {run}{name}".strip(),
                        source="package.json",
                        default_url="http://localhost:3000",
                    )
                )
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8", errors="ignore")
        if "fastapi" in text or "uvicorn" in text:
            commands.append(
                PreviewCommand(
                    label="uvicorn (app:app)",
                    command="uvicorn app:app --reload --port 8000",
                    source="pyproject.toml",
                    default_url="http://localhost:8000",
                )
            )
        if "flask" in text:
            commands.append(
                PreviewCommand(
                    label="flask run",
                    command="flask run --port 5000",
                    source="pyproject.toml",
                    default_url="http://localhost:5000",
                )
            )
        if "streamlit" in text:
            commands.append(
                PreviewCommand(
                    label="streamlit run app.py",
                    command="streamlit run app.py",
                    source="pyproject.toml",
                    default_url="http://localhost:8501",
                )
            )
    if (root / "compose.yaml").is_file() or (root / "docker-compose.yml").is_file():
        commands.append(
            PreviewCommand(
                label="docker compose up",
                command="docker compose up",
                source="compose.yaml",
            )
        )
    return commands


class PreviewManager:
    """Runs at most one preview process per project."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._current: PreviewProcess | None = None

    @property
    def current(self) -> PreviewProcess | None:
        if self._current is not None and not self._current.running:
            self._current = None
        return self._current

    def detect(self) -> list[PreviewCommand]:
        return detect_preview_commands(self.root)

    def start(self, command: str, *, url: str = "") -> PreviewProcess:
        self.stop()
        args = shlex.split(command)
        process = subprocess.Popen(  # noqa: S603 - command is user-approved
            args,
            cwd=str(self.root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
            env={**os.environ, "BROWSER": "none"},
        )
        self._current = PreviewProcess(command=command, process=process, url=url)
        return self._current

    def record_output(self, line: str) -> str | None:
        """Append a captured output line; return a URL if one is discovered."""
        if self._current is None:
            return None
        self._current.logs.append(line)
        if not self._current.url:
            match = _URL_RE.search(line)
            if match:
                self._current.url = match.group(0)
                return self._current.url
        return None

    def stop(self) -> None:
        if self._current is None:
            return
        process = self._current.process
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        self._current = None
