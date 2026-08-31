"""Serve Daino's own documentation to the browser IDE.

The `?` button in the GUI opens `/docs`, which must be usage documentation — how
to run Daino, configure providers, route models, and so on — not the generated
API reference. The reference is still there, at `/api-docs`; this router hands
the React app the markdown that lives in `docs/`, which it renders with the same
theme and markdown renderer as the rest of the IDE.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api/docs", tags=["docs"])

#: Reading order. Anything found on disk but not listed here is appended
#: alphabetically, so a new page shows up without editing this list.
_ORDER = (
    "installation",
    "getting-started",
    "features",
    "tui",
    "gui",
    "cli-reference",
    "configuration",
    "providers",
    "model-routing",
    "memory",
    "repository-intelligence",
    "missions",
    "playbooks",
    "runtimes",
    "security",
    "deployment",
    "infrastructure",
    "architecture",
    "contributing",
)

#: Grouping shown in the sidebar.
_SECTIONS: dict[str, str] = {
    "installation": "Getting started",
    "getting-started": "Getting started",
    "features": "Getting started",
    "tui": "Getting started",
    "gui": "Getting started",
    "cli-reference": "Reference",
    "configuration": "Configuration",
    "providers": "Configuration",
    "model-routing": "Configuration",
    "runtimes": "Configuration",
    "memory": "How it works",
    "repository-intelligence": "How it works",
    "missions": "How it works",
    "playbooks": "How it works",
    "architecture": "How it works",
    "security": "Operations",
    "deployment": "Operations",
    "infrastructure": "Operations",
    "contributing": "Operations",
}


def _candidate_roots() -> tuple[Path, ...]:
    """Where the markdown may live: shipped in the wheel, or in a checkout."""
    package = Path(__file__).resolve().parent.parent.parent
    return (package / "_docs", package.parent / "docs")


@lru_cache(maxsize=1)
def _docs_root() -> Path | None:
    for candidate in _candidate_roots():
        if candidate.is_dir() and any(candidate.glob("*.md")):
            return candidate
    return None


def _title_of(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return path.stem.replace("-", " ").title()


def _pages(root: Path) -> list[dict[str, str]]:
    found = {item.stem: item for item in sorted(root.glob("*.md"))}
    ordered = [slug for slug in _ORDER if slug in found]
    ordered += [slug for slug in found if slug not in _ORDER]
    return [
        {
            "slug": slug,
            "title": _title_of(found[slug]),
            "section": _SECTIONS.get(slug, "Reference"),
        }
        for slug in ordered
    ]


@router.get("")
def list_pages(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    root = _docs_root()
    return {
        "available": root is not None,
        "project": state.context.settings.project.name,
        "pages": _pages(root) if root else [],
    }


@router.get("/{slug}")
def read_page(slug: str) -> dict:
    root = _docs_root()
    if root is None:
        raise HTTPException(status_code=404, detail="Documentation is not installed")
    # Slugs come from the listing; still refuse anything that could traverse.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise HTTPException(status_code=400, detail="Invalid page name")
    path = root / f"{slug}.md"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"No documentation page {slug!r}")
    return {
        "slug": slug,
        "title": _title_of(path),
        "markdown": path.read_text(encoding="utf-8"),
    }
