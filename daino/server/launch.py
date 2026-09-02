"""``daino <path> --gui`` entry point: open the project and serve the browser IDE."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

from daino import branding

# The runner sprite, matching the terminal UI's dinosaur (daino/tui/widgets).
_RUN_BODY = ("   ▄██", "▖▄███▀")
_RUN_FEET = (" ▀▘  ▀", "  ▀ ▀ ")


def _run_dino(stop: threading.Event) -> None:
    """Animate a small running dinosaur until ``stop`` is set (TTY only).

    Redraws four lines in place — two body rows, cycling feet, and a scrolling
    ground — so the one-time build reads as progress rather than a frozen prompt.
    """
    green, dim, reset = "\033[32m", "\033[2m", "\033[0m"
    track = 24
    ground_pattern = "·   " * 10
    sys.stdout.write("\n" * 4)  # reserve four lines beneath the message
    frame = 0
    try:
        while not stop.is_set():
            feet = _RUN_FEET[frame % len(_RUN_FEET)]
            pad = " " * (frame % (track - 6))
            ground = ground_pattern[frame % 4 :][:track]
            sys.stdout.write("\033[4A")  # back to the top of the reserved block
            sys.stdout.write(f"\r\033[K  {pad}{green}{_RUN_BODY[0]}{reset}\n")
            sys.stdout.write(f"\r\033[K  {pad}{green}{_RUN_BODY[1]}{reset}\n")
            sys.stdout.write(f"\r\033[K  {pad}{green}{feet}{reset}\n")
            sys.stdout.write(f"\r\033[K  {dim}{ground}{reset}\n")
            sys.stdout.flush()
            frame += 1
            time.sleep(0.16)
    except Exception:  # noqa: BLE001,S110 - a cosmetic animation must never crash launch
        pass


def _stop_dino(thread: threading.Thread | None, stop: threading.Event) -> None:
    if thread is None:
        return
    stop.set()
    thread.join(timeout=1)
    # Wipe the four animation lines so the next message starts clean.
    sys.stdout.write("\033[4A")
    for _ in range(4):
        sys.stdout.write("\r\033[K\n")
    sys.stdout.write("\033[4A")
    sys.stdout.flush()


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

    animate = sys.stdout.isatty()
    print(branding.dino_banner(color=animate))
    print(f"  Building the {branding.NAME} GUI (one-time; this can take a minute)…\n")

    # While the build runs, animate the dinosaur (a TTY only) and keep npm's
    # own output captured so it does not fight the animation; on failure the
    # captured logs are shown so the error is not lost.
    stop = threading.Event()
    thread = threading.Thread(target=_run_dino, args=(stop,), daemon=True) if animate else None
    if thread is not None:
        thread.start()

    run_kwargs: dict = {"cwd": str(gui_dir), "check": True}
    if animate:
        run_kwargs["capture_output"] = True
        run_kwargs["text"] = True
    try:
        if not (gui_dir / "node_modules").is_dir():
            subprocess.run([npm, "install"], **run_kwargs)  # noqa: S603
        subprocess.run([npm, "run", "build"], **run_kwargs)  # noqa: S603
    except (OSError, subprocess.CalledProcessError) as exc:
        _stop_dino(thread, stop)
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            logs = (exc.stderr or exc.stdout or "").strip()
            if logs:
                detail = "\n" + logs[-1500:]
        print(
            f"\n  The GUI build did not complete ({exc}). Serving the API-only page.{detail}\n"
            f"  You can build it manually: cd {gui_dir} && npm install && npm run build\n"
        )
        return False
    _stop_dino(thread, stop)
    if index.is_file():
        print(f"  ✓ {branding.NAME} GUI ready.\n")
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


def _wait_port(host: str, port: int, timeout: float = 25.0) -> bool:
    """Block until ``host:port`` accepts a connection, or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.25)
    return False


# ---- Background server registry (shared across projects, per user) ----
#
# Each `daino --gui` runs detached; this registry is how `daino ps` / `daino
# kill` find them again. It lives in the user's home so one command lists every
# project's server, while each server's *logs* stay in that project's `.daino`.


def _registry_path() -> Path:
    home = Path.home() / ".daino"
    home.mkdir(parents=True, exist_ok=True)
    return home / "gui-servers.json"


def _load_registry() -> list[dict]:
    path = _registry_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text() or "[]")
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _save_registry(records: list[dict]) -> None:
    try:
        _registry_path().write_text(json.dumps(records, indent=2))
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def list_servers() -> list[dict]:
    """Return the registered servers whose process is still alive, pruning dead."""
    records = _load_registry()
    alive = [r for r in records if _pid_alive(int(r.get("pid", -1)))]
    if len(alive) != len(records):
        _save_registry(alive)
    return alive


def _find_for_dir(root: Path) -> dict | None:
    target = str(root)
    for record in list_servers():
        if record.get("dir") == target:
            return record
    return None


def serve_gui(
    project: Path | None,
    *,
    host: str = "127.0.0.1",
    port: int = 4173,
) -> None:
    """Run the GUI server in the foreground — the body of the detached process.

    Binds the exact port chosen by the launcher (no re-resolution) so the URL
    the launcher printed and registered is the one that answers.
    """
    import uvicorn

    from daino.application import initialize_project, open_project
    from daino.config import config_path, find_project_root
    from daino.server.app import create_app

    root = find_project_root(project)
    if not config_path(root).exists():
        initialize_project(root)
    _ensure_frontend_built()  # a no-op once dist/ exists

    context = open_project(root)
    app = create_app(context, host=host)
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        context.close()


def launch_gui_background(
    project: Path | None,
    *,
    host: str = "127.0.0.1",
    port: int = 4173,
    open_browser: bool = True,
) -> None:
    """Start the GUI server detached, freeing the terminal.

    The one-time frontend build runs here (so its animation is visible) before
    the server forks into the background with its logs in the project's
    ``.daino`` directory and an entry in the shared registry.
    """
    from daino.application import initialize_project
    from daino.config import config_path, find_project_root

    root = find_project_root(project)
    if not config_path(root).exists():
        initialize_project(root)

    if not _ensure_frontend_built():
        print(f"  The {branding.NAME} GUI could not be built; not starting a server.\n")
        return

    existing = _find_for_dir(root)
    if existing:
        print(f"\n  {branding.NAME} GUI is already running for this project.")
        print(f"    session {existing['id']} · {existing['url']}")
        print(f"    stop with:  daino kill {existing['id']}\n")
        if open_browser:
            webbrowser.open(existing["url"])
        return

    resolved_port = _resolve_port(host, port)
    session_id = secrets.token_hex(3)
    url = f"http://{host}:{resolved_port}"
    log_path = root / ".daino" / "logs" / f"gui-{session_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "daino",
        "--project",
        str(root),
        "--gui",
        "--serve",
        "--host",
        host,
        "--port",
        str(resolved_port),
        "--no-browser",
    ]
    # A fresh session detaches the child from this terminal and makes it a
    # process-group leader, so `daino kill` can stop it and its uvicorn workers.
    log_handle = open(log_path, "ab", buffering=0)  # noqa: SIM115
    try:
        proc = subprocess.Popen(  # noqa: S603
            command,
            stdout=log_handle,
            stderr=log_handle,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(root),
        )
    finally:
        log_handle.close()

    records = list_servers()
    records.append(
        {
            "id": session_id,
            "dir": str(root),
            "host": host,
            "port": resolved_port,
            "pid": proc.pid,
            "started": datetime.now(UTC).isoformat(),
            "log": str(log_path),
            "url": url,
        }
    )
    _save_registry(records)

    if _wait_port(host, resolved_port):
        if open_browser:
            webbrowser.open(url)
        print(f"\n  {branding.NAME} GUI running in the background.")
        print(f"    session {session_id} · {url}")
        print(f"    project {root}")
        print(f"    logs    {log_path}")
        print(f"    stop    daino kill {session_id}\n")
    else:
        print(f"\n  {branding.NAME} GUI is still starting; it has not answered on {url} yet.")
        print(f"    session {session_id} · check the log: {log_path}\n")


def kill_server(target: str | None) -> dict | None:
    """Stop a background server by session id or project directory.

    ``target`` of ``None`` stops the server for the current directory. Returns
    the stopped record, or ``None`` when nothing matched.
    """
    records = list_servers()
    if not records:
        return None

    match: dict | None = None
    if target is None:
        match = _find_for_dir(Path.cwd().resolve())
    else:
        ids = {r["id"] for r in records}
        resolved_dir = None if target in ids else str(Path(target).expanduser().resolve())
        for record in records:
            if record["id"] == target or record["dir"] == target or record["dir"] == resolved_dir:
                match = record
                break
    if match is None:
        return None

    pid = int(match["pid"])
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    _save_registry([r for r in records if r["id"] != match["id"]])
    return match


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
    # The bind host is the one the Host header must name (anti-rebinding).
    app = create_app(context, host=host)

    print(f"\n  {branding.NAME} GUI → {url}")
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
