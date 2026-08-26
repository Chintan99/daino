"""FastAPI application factory for the Daino browser IDE backend."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from daino import __version__, branding
from daino.application.context import ProjectContext
from daino.server import websocket
from daino.server.routes import (
    agent,
    design,
    docs,
    files,
    git,
    insights,
    preview,
    terminal,
)
from daino.server.state import GuiState

#: Built React assets, when present, are served from here in production.
_DIST_DIR = Path(__file__).resolve().parent.parent / "gui" / "dist"


def create_app(context: ProjectContext) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
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

    # Local-only: the Vite dev server (5173) may call the API during development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for module in (agent, files, git, design, preview, terminal, insights, docs):
        app.include_router(module.router)
    app.include_router(websocket.router)

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__, "project": str(context.root)}

    _mount_frontend(app)
    return app


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
