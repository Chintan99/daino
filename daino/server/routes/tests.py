"""Test discovery and execution for the CODE workspace's Tests panel.

Shaped like ``/api/qa/*``: a run is long-lived, so the browser starts it and
follows along by polling rather than holding a request open. The panel needs to
survive a reload mid-run, which a held request cannot provide.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from daino.events import TestsCompleted, TestsStarted
from daino.server.deps import get_state
from daino.server.state import GuiState
from daino.testing import TestRun, TestRunError

router = APIRouter(prefix="/api/tests", tags=["tests"])


class RunTestsRequest(BaseModel):
    #: Empty picks the first runnable framework, which is what a bare "Run
    #: tests" means in a single-language project.
    framework: str = ""
    #: Framework-native test ids. Empty runs everything.
    selection: list[str] = Field(default_factory=list)
    coverage: bool = False
    #: Re-run exactly the tests that failed last time, ignoring `selection`.
    failed_only: bool = False


def _payload(state: GuiState, run: TestRun | None) -> dict[str, Any]:
    return {
        "running": state.tests.running,
        "run": (
            {
                **run.model_dump(mode="json"),
                # Computed rather than stored, so a reader never sees a tally
                # that disagrees with the results beside it.
                "counts": run.counts,
            }
            if run is not None
            else None
        ),
    }


@router.get("/frameworks")
async def frameworks(
    state: Annotated[GuiState, Depends(get_state)],
    framework: str = Query(default=""),
) -> dict[str, Any]:
    """What test frameworks this project has, and what tests they contain.

    Frameworks that cannot run are listed with the reason: "no tests found" and
    "the runner is not installed" are different problems, and only one of them
    is the user's to fix by writing tests.
    """
    described, cases = await state.tests.discover(framework)
    return {
        "frameworks": [item.model_dump(mode="json") for item in described],
        "tests": [item.model_dump(mode="json") for item in cases],
        "running": state.tests.running,
    }


@router.get("/latest")
def latest(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    """The live run while one is in flight, else the last one that finished."""
    return _payload(state, state.tests.current or state.tests.last)


@router.post("/run")
async def run(
    state: Annotated[GuiState, Depends(get_state)],
    body: RunTestsRequest | None = None,
) -> dict[str, Any]:
    """Start a run in the background and return its initial state."""
    request = body or RunTestsRequest()
    selection = list(request.selection)
    if request.failed_only:
        selection = state.tests.rerun_selection()
        if not selection:
            raise HTTPException(
                status_code=400,
                detail="No failing tests from the last run to re-run.",
            )

    def on_update(current: TestRun) -> None:
        # The same events the TUI renders, so both clients agree about what
        # happened without either owning the truth.
        if current.status == "running":
            state.context.events.publish(TestsStarted(commands=[current.command]))
            return
        if current.finished_at is None:
            return
        tally = current.counts
        state.context.events.publish(
            TestsCompleted(
                passed=current.status == "passed",
                passed_count=tally["passed"],
                failed_count=tally["failed"] + tally["errored"],
                duration_seconds=current.duration_seconds,
                failures=[item.model_dump(mode="json") for item in current.failures],
            )
        )

    try:
        started = await state.tests.start(
            framework_id=request.framework,
            selection=selection,
            coverage=request.coverage,
            on_update=on_update,
        )
    except TestRunError as exc:
        # 409 for "already running", 400 for anything the caller can fix.
        status = 409 if "already in progress" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return _payload(state, started)


@router.post("/cancel")
def cancel(state: Annotated[GuiState, Depends(get_state)]) -> dict[str, Any]:
    return {"cancelled": state.tests.cancel()}
