"""FastAPI application factory for the Daino browser IDE backend."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from daino import __version__, branding
from daino.application.context import ProjectContext
from daino.application.review_service import ReviewError
from daino.server import websocket
from daino.server.routes import (
    agent,
    customization,
    design,
    docs,
    files,
    git,
    insights,
    preview,
    review,
    settings,
    terminal,
    workbench,
)
from daino.server.security import DEV_ORIGINS, OriginPolicy
from daino.server.state import GuiState
from daino.workbench.service import WorkbenchError

#: Built React assets, when present, are served from here in production.
_DIST_DIR = Path(__file__).resolve().parent.parent / "gui" / "dist"


def create_app(context: ProjectContext, *, host: str = "127.0.0.1") -> FastAPI:
    """Build the API for one open project.

    ``host`` is the interface the server will be bound to; it is what the
    :class:`~daino.server.security.OriginPolicy` accepts in a ``Host`` header,
    so a page reached through a rebound hostname is refused.
    """

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Interactive terminals outlive the page that opened them, but not by
        # much: a sweeper closes the ones no client came back for.
        reaper = asyncio.create_task(_reap_terminals(app))
        try:
            yield
        finally:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
            app.state.gui.shutdown()

    # /docs belongs to the usage documentation the GUI links to; the generated
    # API reference moves to /api-docs.
    app = FastAPI(
        title=branding.NAME,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api-docs",
        redoc_url="/api-redoc",
    )
    app.state.gui = GuiState.from_context(context)
    app.state.gui.start_watchers()
    app.state.origins = OriginPolicy.for_host(host)

    # Local-only: the Vite dev server (5173) may call the API during development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(DEV_ORIGINS),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def enforce_origin(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Refuse requests from a foreign page or a rebound hostname.

        CORS alone is not enough: it stops a cross-origin page from *reading* a
        response, not from causing the side effect of a "simple" request.
        """
        reason = app.state.origins.rejection(
            request.headers.get("origin"), request.headers.get("host")
        )
        if reason is not None:
            return JSONResponse(status_code=403, content={"detail": reason})
        return await call_next(request)

    for module in (
        agent,
        files,
        git,
        design,
        preview,
        review,
        terminal,
        insights,
        docs,
        settings,
        customization,
        workbench,
    ):
        app.include_router(module.router)
    app.include_router(websocket.router)

    @app.exception_handler(ReviewError)
    async def review_error(_: Request, exc: Exception) -> JSONResponse:
        """A change that cannot be resolved into a diff is the caller's mistake."""
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(WorkbenchError)
    async def workbench_error(_: Request, exc: Exception) -> JSONResponse:
        """An unknown workspace, or a path that escapes one, is a 404.

        Registered once rather than wrapped around each of the workspace
        routes: a decorator would erase the signatures FastAPI reads to build
        their request models.
        """
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__, "project": str(context.root)}

    _mount_frontend(app)
    return app


#: How often the terminal sweeper runs, and how long an unattached terminal is
#: kept. A reload should find its shell again; a closed tab should not leak one.
_TERMINAL_SWEEP_SECONDS = 60.0
_TERMINAL_IDLE_SECONDS = 600.0


async def _reap_terminals(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(_TERMINAL_SWEEP_SECONDS)
        with contextlib.suppress(Exception):
            app.state.gui.terminals.prune(idle_seconds=_TERMINAL_IDLE_SECONDS)


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built React app, with an SPA fallback, when it exists.

    In development the frontend runs under Vite and talks to this API via CORS,
    so a missing ``dist/`` is expected and only affects production serving.
    """
    index = _DIST_DIR / "index.html"
    if not index.is_file():
        gui_dir = _DIST_DIR.parent

        @app.get("/", response_class=HTMLResponse)
        def _no_frontend() -> str:
            return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{branding.NAME} — build the browser IDE</title>
<style>
  body{{background:#0c0e0d;color:#e4e7e5;font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;
       margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}}
  .card{{max-width:640px;padding:2.5rem}}
  h1{{font-size:1.5rem;margin:0 0 .25rem}} .dino{{color:#6cbf8d}}
  code,pre{{background:#141716;border:1px solid #2a2f2c;border-radius:6px}}
  pre{{padding:1rem;overflow:auto}} code{{padding:.15rem .4rem}}
  a{{color:#6cbf8d}} .muted{{color:#7f8683}}
</style></head><body><div class="card">
  <h1><span class="dino">◆</span> {branding.NAME} API is running</h1>
  <p class="muted">The browser IDE hasn't been built yet. It builds automatically on
  <code>daino . --gui</code> when Node.js is installed — install Node 18+ and relaunch, or build it
  manually:</p>
  <pre>cd {gui_dir}
npm install
npm run build</pre>
  <p class="muted">Then reload this page. The API reference is at
  <a href="/api-docs">/api-docs</a>.</p>
</div></body></html>"""

        return

    assets = _DIST_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    def _index() -> FileResponse:
        return FileResponse(index)

    @app.get("/{full_path:path}")
    def _spa(full_path: str) -> FileResponse:
        # Client-side routes fall back to index.html; real files are served as-is.
        candidate = _DIST_DIR / full_path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)
