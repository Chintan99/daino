"""Engineering-evidence endpoints: logs, execution map, QA, missions, checkpoints.

These are the browser counterparts of the TUI's workspace views. Every one of
them reads through the same application service the Textual client uses, so the
two front-ends can never drift into telling different stories about a run.
Everything here is read-only apart from ``POST /api/qa/run``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from daino.application.view_models import ExecutionPrompt, ExecutionTrace
from daino.security import redact
from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api", tags=["insights"])


# ---------------------------------------------------------------- audit logs


@router.get("/logs")
def logs(
    state: Annotated[GuiState, Depends(get_state)],
    q: str = Query(default=""),
    limit: int = Query(default=500, ge=1, le=5000),
    mission_id: str = Query(default=""),
) -> dict:
    """Return the tail of the append-only audit log, newest last.

    The log is already redacted on write; the filter is applied to the rendered
    JSON so a search matches whatever the reader can actually see.
    """
    try:
        events = state.audit.read(mission_id or None)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"Audit log unreadable: {exc}") from exc

    needle = q.strip().casefold()
    selected: list[dict[str, Any]] = []
    for event in events:
        if needle and needle not in json.dumps(event, default=str).casefold():
            continue
        selected.append(event)
    tail = selected[-limit:]
    return {"total": len(events), "matched": len(selected), "events": tail}


# ------------------------------------------------------------- execution map


def _prompt_payload(prompt: ExecutionPrompt) -> dict:
    return {
        "mission_id": prompt.mission_id,
        "request": prompt.request,
        "title": prompt.title,
        "status": prompt.status,
        "created_at": prompt.created_at.isoformat(),
        "total_tokens": prompt.total_tokens,
        "estimated_cost": prompt.estimated_cost,
        "step_count": prompt.step_count,
        "tool_count": prompt.tool_count,
        "model_call_count": prompt.model_call_count,
    }


def _trace_payload(trace: ExecutionTrace) -> dict:
    return {
        "mission_id": trace.mission_id,
        "request": trace.request,
        "status": trace.status,
        "created_at": trace.created_at.isoformat(),
        "total_input_tokens": trace.total_input_tokens,
        "total_output_tokens": trace.total_output_tokens,
        "total_tokens": trace.total_tokens,
        "estimated_cost": trace.estimated_cost,
        "total_model_latency_ms": trace.total_model_latency_ms,
        "total_tool_duration_seconds": trace.total_tool_duration_seconds,
        "model_call_count": trace.model_call_count,
        "tool_count": trace.tool_count,
        "steps": [
            {
                **asdict(step),
                "timestamp": step.timestamp.isoformat(),
                "model_usage": (
                    {**asdict(step.model_usage), "total_tokens": step.model_usage.total_tokens}
                    if step.model_usage is not None
                    else None
                ),
            }
            for step in trace.steps
        ],
    }


@router.get("/map/prompts")
def map_prompts(
    state: Annotated[GuiState, Depends(get_state)],
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    prompts = state.execution_map.prompts(limit)
    return {"prompts": [_prompt_payload(item) for item in prompts]}


@router.get("/map/prompts/{mission_id}")
def map_trace(state: Annotated[GuiState, Depends(get_state)], mission_id: str) -> dict:
    try:
        trace = state.execution_map.trace(mission_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _trace_payload(trace)


# ---------------------------------------------------------------------- QA


def _qa_running(state: GuiState) -> bool:
    task = state.qa_task
    return isinstance(task, asyncio.Task) and not task.done()


@router.get("/qa/history")
def qa_history(
    state: Annotated[GuiState, Depends(get_state)],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    return {
        "running": _qa_running(state),
        "reports": [item.model_dump(mode="json") for item in state.qa.history(limit)],
    }


@router.get("/qa/latest")
def qa_latest(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """The live report while a run is in flight, else the last persisted one."""
    report = state.qa_live if _qa_running(state) else (state.qa.latest() or state.qa_live)
    return {
        "running": _qa_running(state),
        "report": report.model_dump(mode="json") if report is not None else None,
    }


@router.get("/qa/reports/{report_id}")
def qa_report(state: Annotated[GuiState, Depends(get_state)], report_id: str) -> dict:
    report = state.qa.load(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Unknown QA report {report_id}")
    return {"running": _qa_running(state), "report": report.model_dump(mode="json")}


@router.post("/qa/run")
async def qa_run(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """Start a QA scan in the background and return immediately.

    A scan runs a whole team of reviewers plus the project's own test and audit
    commands, which is far longer than a request should be held open, so the GUI
    starts it here and follows along through ``/api/qa/latest``. Checks that
    would need network approval are skipped rather than silently granted.
    """
    if _qa_running(state):
        raise HTTPException(status_code=409, detail="A QA run is already in progress")

    def on_update(report) -> None:
        state.qa_live = report

    async def run() -> None:
        try:
            state.qa_live = await state.qa.run(on_update=on_update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a crashed scan must not kill the server
            # The service records orchestration failures in the report itself, so
            # reaching here means something unexpected; keep it in the audit log.
            state.audit.emit("QARunCrashed", error=f"{type(exc).__name__}: {exc}")

    state.qa_task = asyncio.create_task(run())
    return {"running": True}


@router.post("/qa/cancel")
def qa_cancel(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    task = state.qa_task
    if isinstance(task, asyncio.Task) and not task.done():
        task.cancel()
        return {"cancelled": True}
    return {"cancelled": False}


# ---------------------------------------------------------------- missions


@router.get("/missions")
def missions(
    state: Annotated[GuiState, Depends(get_state)],
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    return {
        "missions": [
            {
                "id": item.id,
                "title": item.title,
                "status": item.status,
                "mode": item.mode,
                "updated_at": item.updated_at.isoformat(),
                "branch": item.branch,
                "workspace": item.workspace,
                "task_counts": item.task_counts,
            }
            for item in state.missions.list_missions(limit)
        ]
    }


@router.get("/missions/{mission_id}")
def mission_details(state: Annotated[GuiState, Depends(get_state)], mission_id: str) -> dict:
    try:
        details = state.missions.mission_details(mission_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return json.loads(json.dumps(details, default=str))


# ------------------------------------------------------------- checkpoints


@router.get("/checkpoints")
def checkpoints(
    state: Annotated[GuiState, Depends(get_state)],
    mission_id: str = Query(default=""),
) -> dict:
    items = state.checkpoints.list(mission_id or None)
    return {
        "checkpoints": [
            {
                "id": item.id,
                "mission_id": item.mission_id or "",
                "revision": item.revision or "",
                "description": item.description,
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in items
        ]
    }


# --------------------------------------------------------------- approvals


@router.get("/approvals")
def approvals(
    state: Annotated[GuiState, Depends(get_state)],
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    from daino.persistence.models import Approval

    with state.context.database.session() as session:
        rows = session.scalars(
            select(Approval).order_by(Approval.created_at.desc()).limit(limit)
        ).all()
        return {
            "approvals": [
                {
                    "id": item.id,
                    "mission_id": item.mission_id or "",
                    "category": item.category,
                    "subject": redact(item.subject),
                    "approved": item.approved,
                    "approver": item.approver,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
                for item in rows
            ]
        }


# -------------------------------------------------------------- repository


@router.get("/repository")
def repository(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """Summarise the stored repository index, exactly as the TUI reads it."""
    try:
        item = state.repository.intelligence()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Index unavailable: {exc}") from exc
    index = state.repository.indexer.load()
    return {
        "summary": state.repository.summary(),
        "file_count": len(index.files),
        "languages": item["languages"],
        "frameworks": list(item["frameworks"]),
        "entrypoints": list(item["entrypoints"]),
        "routes": item["routes"],
        "database_models": item["database_models"],
        "tests": item["tests"],
        "dependencies": item["dependencies"],
        "generated_at": str(item["generated_at"] or ""),
    }


@router.post("/repository/index")
def reindex(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """Rebuild the index — the browser equivalent of the TUI's ``/index``."""
    index = state.repository.index()
    return {"file_count": len(index.files), "frameworks": sorted(index.frameworks)}
