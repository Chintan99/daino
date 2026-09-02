"""Change review: start one, follow it, and read the diff it is about.

The Inspector's REVIEW view drives these. Shaped like ``/api/qa/*`` on purpose —
a review is long-running for the same reason a scan is, so the browser starts it
and follows along by polling rather than holding a request open.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from daino.application.qa_service import severity_counts
from daino.application.review_service import ReviewError
from daino.schemas import ChangeReview, ReviewScope
from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api/review", tags=["review"])


class RunReviewRequest(BaseModel):
    """What to review. Defaults to the uncommitted working tree."""

    scope: ReviewScope = "working"
    #: Base branch for a "branch" review, or the ref spec for a "range" one.
    #: Empty lets the service pick the branch this one would be proposed against.
    base_ref: str = ""
    head_ref: str = ""


def _running(state: GuiState) -> bool:
    task = state.review_task
    return isinstance(task, asyncio.Task) and not task.done()


@router.get("/subject")
def subject(
    state: Annotated[GuiState, Depends(get_state)],
    scope: ReviewScope = "working",
    base_ref: str = Query(default=""),
) -> dict[str, Any]:
    """What a review of this scope would cover, without running one.

    Lets the view show "12 files, 340 lines, against origin/main" before anyone
    commits to a model call, and surfaces an unresolvable base immediately
    rather than after a failed run.
    """
    try:
        resolved = state.review.subject(scope, base_ref=base_ref)
    except ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    files = len([line for line in resolved.patch.splitlines() if line.startswith("diff --git")])
    return {
        "scope": resolved.scope,
        "base_ref": resolved.base_ref,
        "head_ref": resolved.head_ref,
        "label": resolved.label,
        "commits": list(resolved.commits),
        "files": files + len(resolved.untracked),
        "untracked": list(resolved.untracked),
        "empty": not resolved.patch.strip() and not resolved.untracked,
    }


@router.get("/latest")
def latest(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    """The live review while one is in flight, else the last persisted one."""
    review = state.review_live if _running(state) else (state.review.latest() or state.review_live)
    return {
        "running": _running(state),
        "review": review.model_dump(mode="json") if review is not None else None,
    }


@router.get("/history")
def history(
    state: Annotated[GuiState, Depends(get_state)],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return {
        "running": _running(state),
        "reviews": [item.model_dump(mode="json") for item in state.review.history(limit)],
    }


@router.get("/reports/{review_id}")
def report(state: Annotated[GuiState, Depends(get_state)], review_id: str) -> dict[str, Any]:
    review = state.review.load(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail=f"Unknown review {review_id}")
    return {"running": _running(state), "review": review.model_dump(mode="json")}


@router.get("/diff")
def file_diff(
    state: Annotated[GuiState, Depends(get_state)],
    path: str = Query(min_length=1),
    scope: ReviewScope = "working",
    base_ref: str = Query(default=""),
) -> dict[str, Any]:
    """The patch for one file of the reviewed change.

    Scoped to a single path so opening a file in the review costs one small
    response rather than re-sending a diff that can run to hundreds of
    kilobytes.
    """
    try:
        resolved = state.review.subject(scope, base_ref=base_ref)
    except ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    wanted: list[str] = []
    collecting = False
    for line in resolved.patch.splitlines():
        if line.startswith("diff --git "):
            collecting = line.endswith(f" b/{path}")
        if collecting:
            wanted.append(line)
    if not wanted and path in resolved.untracked:
        # An untracked file has no patch; show it as wholly added.
        try:
            content = (state.root / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {"path": path, "patch": "", "readable": False}
        wanted = [f"diff --git a/{path} b/{path}", "new file", f"+++ b/{path}"]
        wanted += [f"+{line}" for line in content.splitlines()]
    return {"path": path, "patch": "\n".join(wanted), "readable": True}


@router.post("/run")
async def run(
    state: Annotated[GuiState, Depends(get_state)],
    body: RunReviewRequest | None = None,
) -> dict[str, Any]:
    """Start a review in the background and return immediately."""
    if _running(state):
        raise HTTPException(status_code=409, detail="A review is already in progress")
    request = body or RunReviewRequest()
    try:
        resolved = state.review.subject(request.scope, base_ref=request.base_ref)
    except ReviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def on_update(review: ChangeReview) -> None:
        state.review_live = review

    async def execute() -> None:
        # Long enough that the user will look away, so it holds the machine
        # awake and says how it ended.
        async with state.missions.attention.turn("Change review") as attention:
            try:
                review = await state.review.run(
                    scope=request.scope,
                    base_ref=request.base_ref,
                    head_ref=request.head_ref,
                    on_update=on_update,
                )
                state.review_live = review
                attention_report = _notification(review)
                if review.verdict == "blocked":
                    attention.failed(attention_report)
                else:
                    attention.completed(attention_report)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a crash must not kill the server
                attention.failed(f"Review crashed: {type(exc).__name__}")
                state.audit.emit("ChangeReviewCrashed", error=f"{type(exc).__name__}: {exc}")

    state.review_task = asyncio.create_task(execute())
    return {"running": True, "scope": request.scope, "subject": resolved.label}


@router.post("/cancel")
def cancel(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    task = state.review_task
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        return {"cancelled": True}
    return {"cancelled": False}


def _notification(review: ChangeReview) -> str:
    """One line that says whether the change can be merged."""
    counts = severity_counts(review.findings)
    tally = ", ".join(f"{count} {level}" for level, count in counts.items() if count)
    scale = f"{len(review.files)} file(s)"
    if review.verdict == "blocked":
        return f"Review BLOCKED — {tally or scale}"
    if review.verdict == "warn":
        return f"Review needs a look — {tally or scale}"
    if review.verdict == "pass":
        return f"Review passed — {scale}, no blocking finding"
    return "Review finished without a verdict"
