"""``daino <path> --gui`` entry point: open the project and serve the browser IDE."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from pathlib import Path


def _ensure_frontend_built() -> bool:
    """Build the React GUI on first launch so ``--gui`` shows the IDE, not JSON.

    The browser IDE is a compiled React app; its ``dist`` bundle must be built
    once with Node. If it is missing we build it automatically when ``npm`` is
    available (a one-time step), and otherwise fall back to the API-only page
    with a clear instruction rather than failing.
    """
    from daino.server.app import _DIST_DIR

    gui_dir = _DIST_DIR.parent
    index = _DIST_DIR / "index.html"
    if index.is_file():
        return True
    if not (gui_dir / "package.json").is_file():
        return False

    npm = shutil.which("npm")
    if npm is None:
        print(
            "\n  The browser IDE needs a one-time build, but `npm` (Node.js) was not found.\n"
            "  Install Node.js 18+ from https://nodejs.org, then run:\n"
            f"    cd {gui_dir} && npm install && npm run build\n"
            "  Serving the API-only page until then.\n"
        )
        return False

    print("\n  Building the Daino GUI (one-time; this can take a minute)…\n")
    try:
        if not (gui_dir / "node_modules").is_dir():
            subprocess.run([npm, "install"], cwd=str(gui_dir), check=True)  # noqa: S603
        subprocess.run([npm, "run", "build"], cwd=str(gui_dir), check=True)  # noqa: S603
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"\n  The GUI build did not complete ({exc}). Serving the API-only page.\n"
            f"  You can build it manually: cd {gui_dir} && npm install && npm run build\n"
        )
        return False
    return index.is_file()


def _resolve_port(host: str, port: int) -> int:
    """Return an available port, starting from ``port`` (0 picks any free port)."""
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, 0))
            return probe.getsockname()[1]
    for candidate in range(port, port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex((host, candidate)) != 0:
                return candidate
    return port


def _open_when_ready(host: str, port: int, url: str) -> None:
    """Open the browser once the server accepts connections."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex((host, port)) == 0:
                webbrowser.open(url)
                return
        time.sleep(0.2)


def run_gui(
    project: Path | None,
    *,
    host: str = "127.0.0.1",
    port: int = 4173,
    open_browser: bool = True,
) -> None:
    """Resolve the project, start the local API, and open the browser IDE.

    Reuses the same configuration, model, session, and tool setup as the TUI by
    going through :func:`~daino.application.context.open_project`. Binds to
    ``127.0.0.1`` by default and never to ``0.0.0.0`` implicitly.
    """
    import uvicorn

    from daino.application import initialize_project, open_project
    from daino.config import config_path, find_project_root
    from daino.server.app import create_app

    root = find_project_root(project)
    if not config_path(root).exists():
        # First GUI launch in a fresh directory: set it up like ``daino init``.
        initialize_project(root)

    # Build the React bundle on first run so the browser shows the IDE directly.
    _ensure_frontend_built()

    context = open_project(root)

    resolved_port = _resolve_port(host, port)
    url = f"http://{host}:{resolved_port}"
    app = create_app(context)

    print(f"\n  Daino GUI → {url}")
    print(f"  Project:  {root}")
    print("  Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Thread(
            target=_open_when_ready, args=(host, resolved_port, url), daemon=True
        ).start()

    try:
        uvicorn.run(app, host=host, port=resolved_port, log_level="info")
    finally:
        context.close()
