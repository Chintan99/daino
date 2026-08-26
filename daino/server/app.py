"""FastAPI application factory for the Daino browser IDE backend."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from daino import __version__
from daino.application.context import ProjectContext
from daino.server import websocket
from daino.server.routes import agent, design, files, git, preview, terminal
from daino.server.state import GuiState

#: Built React assets, when present, are served from here in production.
_DIST_DIR = Path(__file__).resolve().parent.parent / "gui" / "dist"


def create_app(context: ProjectContext) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        app.state.gui.shutdown()

    app = FastAPI(title="Daino", version=__version__, lifespan=lifespan)
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

    for module in (agent, files, git, design, preview, terminal):
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

        @app.get("/")
        def _no_frontend() -> JSONResponse:
            return JSONResponse(
                {
                    "message": "Daino API is running. Build the GUI with `npm run build` "
                    "in daino/gui, or run the Vite dev server for development.",
                    "docs": "/docs",
                }
            )

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
