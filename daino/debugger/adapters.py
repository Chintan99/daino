"""Finding and starting a debug adapter for a project.

Same principle as the language servers: nothing is bundled, and what is already
installed is used. A Python project that has debugpy gets a debugger; one that
does not is told the single command that would fix it, rather than being shown a
button that does nothing.

Two adapters are supported, chosen because they cover what this codebase and its
GUI are written in and because both are driven without a marketplace extension:

* **debugpy** for Python. Started in listen mode on a loopback port; the client
  connects to it over TCP. It is a plain pip package and is very often already
  present.
* **js-debug** for Node, when it is installed. Node's own ``--inspect``
  protocol is *not* DAP, so driving it directly would mean a second protocol
  implementation; the adapter is what makes one client enough.
"""

from __future__ import annotations

import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Adapter:
    """How to start one debug adapter, and what to tell the user if it is absent."""

    id: str
    label: str
    #: Language ids this adapter debugs, for picking one from a file.
    languages: tuple[str, ...]
    #: "tcp" adapters are started listening and connected to; "stdio" ones speak
    #: over their own pipes.
    transport: str = "tcp"
    install: str = ""


PYTHON = Adapter(
    id="debugpy",
    label="Python (debugpy)",
    languages=("python",),
    transport="tcp",
    install="pip install debugpy",
)
NODE = Adapter(
    id="pwa-node",
    label="Node.js (js-debug)",
    languages=("javascript", "typescript"),
    transport="stdio",
    install="npm i -g js-debug  (or install the VS Code JavaScript debugger)",
)

ADAPTERS: tuple[Adapter, ...] = (PYTHON, NODE)

#: Suffix -> language id, for choosing an adapter from the file being debugged.
LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
}


def language_of(path: str | Path) -> str:
    return LANGUAGES.get(Path(path).suffix.lower(), "")


def project_python(root: Path) -> str:
    """The interpreter to debug with.

    The project's own virtualenv first: debugging a project with a different
    interpreter than it runs under produces import errors that look like bugs in
    the code. Never the bare name ``python`` — it does not exist on a modern
    macOS or most Linux distributions.
    """
    for relative in (Path(".venv/bin/python"), Path(".venv/Scripts/python.exe")):
        if (root / relative).is_file():
            return str(root / relative)
    return sys.executable or "python3"


def _has_debugpy(root: Path) -> bool:
    import subprocess  # noqa: PLC0415 - a capability probe, never a shell

    try:
        return (
            subprocess.run(  # noqa: S603
                [project_python(root), "-c", "import debugpy"],
                cwd=str(root),
                capture_output=True,
                check=False,
                timeout=20,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def _js_debug(root: Path) -> str | None:
    """Where js-debug's DAP entry point is, if it is installed.

    Checked in the project first, then globally. The file is called
    ``dapDebugServer.js`` in every distribution of it.
    """
    candidates = [
        root / "node_modules" / "js-debug" / "src" / "dapDebugServer.js",
        root / "node_modules" / "@vscode" / "js-debug" / "src" / "dapDebugServer.js",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def available(root: Path) -> list[dict[str, object]]:
    """Every adapter, whether it can run here, and how to get it.

    Reported so a language with no debugger says *why*, and so "no debugger"
    never looks the same as "the debugger found nothing".
    """
    rows: list[dict[str, object]] = []
    for adapter in ADAPTERS:
        if adapter is PYTHON:
            ready = _has_debugpy(root)
        elif adapter is NODE:
            ready = _js_debug(root) is not None and shutil.which("node") is not None
        else:  # pragma: no cover - defensive
            ready = False
        rows.append(
            {
                "id": adapter.id,
                "label": adapter.label,
                "languages": list(adapter.languages),
                "available": ready,
                "install": adapter.install,
            }
        )
    return rows


def for_language(language: str) -> Adapter | None:
    return next((item for item in ADAPTERS if language in item.languages), None)


def free_port() -> int:
    """An unused loopback port for an adapter to listen on.

    There is an unavoidable race between choosing a port and the adapter binding
    it. Binding to port 0 and reading back the assignment is the narrowest form
    of it available, and the alternative — a fixed port — fails outright the
    second time two projects are debugged at once.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def python_launch_argv(root: Path, port: int, program: str, args: list[str]) -> list[str]:
    """Start debugpy listening, with the debuggee not yet running.

    ``--wait-for-client`` is what makes breakpoints in module-level code work:
    without it the program has usually run past them before the client has
    connected, and a breakpoint on line 1 of a script would never be hit.
    """
    return [
        project_python(root),
        "-m",
        "debugpy",
        "--listen",
        f"127.0.0.1:{port}",
        "--wait-for-client",
        program,
        *args,
    ]


def python_module_argv(root: Path, port: int, module: str, args: list[str]) -> list[str]:
    """The same, for ``-m some.module`` rather than a file path."""
    return [
        project_python(root),
        "-m",
        "debugpy",
        "--listen",
        f"127.0.0.1:{port}",
        "--wait-for-client",
        "-m",
        module,
        *args,
    ]


def node_adapter_argv(root: Path, port: int) -> list[str] | None:
    """Start js-debug's DAP server on a port, or None when it is not installed."""
    entry = _js_debug(root)
    node = shutil.which("node")
    if entry is None or node is None:
        return None
    return [node, entry, str(port), "127.0.0.1"]
