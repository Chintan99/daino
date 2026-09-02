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
from pydantic import BaseModel
from sqlalchemy import select

from daino.application.qa_service import severity_counts
from daino.application.view_models import ExecutionPrompt, ExecutionTrace
from daino.schemas import QAReport, QAScanProfile
from daino.security import redact
from daino.security.probe import target_is_local
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


class RunInspectionRequest(BaseModel):
    """What the Inspector asks for when it starts a scan.

    Every field is optional so an older client (and ``POST /api/qa/run`` with an
    empty body) still starts the same comprehensive scan it always did.
    """

    #: "full", "quality", or "security".
    profile: QAScanProfile = "full"
    #: A running application to probe. Empty means "static evidence only".
    target_url: str = ""
    #: The user's confirmation that a non-loopback target belongs to them.
    #: Ignored for loopback and private-network addresses, which never need it.
    authorize_remote_target: bool = False


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
        # A verdict describes a checkout. Once the tree has moved, the badge has
        # to stop claiming this code was cleared.
        "stale": (
            report is not None and not _qa_running(state) and not state.qa.is_current(report)
        ),
    }


@router.get("/qa/reports/{report_id}")
def qa_report(state: Annotated[GuiState, Depends(get_state)], report_id: str) -> dict:
    report = state.qa.load(report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"Unknown QA report {report_id}")
    return {
        "running": _qa_running(state),
        "report": report.model_dump(mode="json"),
        "stale": not state.qa.is_current(report),
    }


@router.post("/qa/run")
async def qa_run(
    state: Annotated[GuiState, Depends(get_state)],
    body: RunInspectionRequest | None = None,
) -> dict:
    """Start an inspection in the background and return immediately.

    A scan runs a whole team of reviewers plus the project's own test, audit,
    and security commands, which is far longer than a request should be held
    open, so the GUI starts it here and follows along through
    ``/api/qa/latest``. Checks that would need network approval are skipped
    rather than silently granted.

    When no target URL is supplied the running preview process is used, so the
    common case — start the app in the Live view, then inspect — needs no extra
    input from the user.
    """
    if _qa_running(state):
        raise HTTPException(status_code=409, detail="A QA run is already in progress")
    request = body or RunInspectionRequest()
    target_url = request.target_url.strip() or _running_preview_url(state)
    if target_url and not target_is_local(target_url) and not request.authorize_remote_target:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{target_url} is not a loopback or private-network address. Confirm you own "
                "the target before probing it."
            ),
        )
    if target_url and not target_is_local(target_url):
        # An operator-authorised scan of a remote host is exactly the decision
        # an audit log exists to record.
        state.audit.emit("InspectionRemoteTargetAuthorized", target=target_url)

    def on_update(report: QAReport) -> None:
        state.qa_live = report

    async def run() -> None:
        # A full scan is the longest thing D[Ai]NO does, so it is exactly the
        # work that must not be interrupted by the host sleeping — and exactly
        # the result worth a notification when it lands.
        async with state.missions.attention.turn("Inspection") as attention:
            try:
                report = await state.qa.run(
                    scan_profile=request.profile,
                    target_url=target_url,
                    authorize_remote_target=request.authorize_remote_target,
                    on_update=on_update,
                )
                state.qa_live = report
                headline = _verdict_notification(report)
                if report.verdict == "blocked":
                    attention.failed(headline)
                else:
                    attention.completed(headline)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a crashed scan must not kill the server
                # The service records orchestration failures in the report
                # itself, so reaching here means something unexpected; keep it
                # in the audit log.
                attention.failed(f"Inspection crashed: {type(exc).__name__}")
                state.audit.emit("QARunCrashed", error=f"{type(exc).__name__}: {exc}")

    state.qa_task = asyncio.create_task(run())
    return {"running": True, "profile": request.profile, "target_url": target_url}


def _running_preview_url(state: GuiState) -> str:
    """The app the user already started, so the probe needs no second answer."""
    preview = getattr(state, "preview", None)
    current = getattr(preview, "current", None) if preview is not None else None
    if current is None or not getattr(current, "running", False):
        return ""
    return str(getattr(current, "url", "") or "")


def _verdict_notification(report: QAReport) -> str:
    """One line that says whether this repository can be pushed."""
    counts = severity_counts(report.findings)
    tally = ", ".join(f"{count} {level}" for level, count in counts.items() if count)
    failed = sum(item.status == "failed" for item in report.checks)
    if report.verdict == "blocked":
        return f"Inspection BLOCKED — {tally or f'{failed} failed check(s)'}"
    if report.verdict == "warn":
        return f"Inspection needs review — {tally or f'{failed} failed check(s)'}"
    if report.verdict == "pass":
        return "Inspection passed — no critical or high findings"
    return "Inspection finished without a verdict"


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
