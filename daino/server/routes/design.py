"""Design-artifact CRUD and granular node/edge mutations.

Both this route and the agent's design tools call the same
:class:`~daino.design.DesignService`, so manual canvas edits and AI edits mutate
one shared document.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from daino.application.design_plan_service import DesignPlanApplicationService
from daino.design import Design, DesignConflictError, DesignError
from daino.design.plans import PlanError, PlanGateError
from daino.exceptions import TurnBusy
from daino.server.deps import get_state
from daino.server.state import GuiState

router = APIRouter(prefix="/api/designs", tags=["design"])


class CreateDesignRequest(BaseModel):
    name: str
    type: str = "architecture"


class AddNodeRequest(BaseModel):
    label: str
    node_type: str = "default"
    node_id: str | None = None
    x: float = 0.0
    y: float = 0.0
    data: dict | None = None


class UpdateNodeRequest(BaseModel):
    label: str | None = None
    node_type: str | None = None
    x: float | None = None
    y: float | None = None
    data: dict | None = None


class ConnectRequest(BaseModel):
    source: str
    target: str
    label: str = ""


def _handle(func):  # small wrapper to map DesignError → HTTP
    def inner(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except DesignError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    return inner


@router.get("")
def list_designs(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    return {"designs": [item.model_dump(mode="json") for item in state.design.list_designs()]}


@router.post("/generate-from-code")
def generate_from_code(state: Annotated[GuiState, Depends(get_state)]) -> dict:
    """Derive an architecture design from what the code actually does.

    Deterministic — no model call. Modules come from how the source is laid out
    (at a granularity chosen from how the code is spread, so a single-package
    repository does not draw one box), edges come from import statements and
    carry the number of files behind them, and layers come from the dependency
    order. Routes and persistence models relabel the modules that expose them.

    The design records its own caveat in ``metadata.generated``: imports
    overstate coupling and miss dynamic dispatch, so this is a starting point to
    correct rather than a reverse-engineered truth. A generated diagram that
    does not admit it was generated gets trusted more than it should be.
    """
    from daino.design import architecture
    from daino.repository import RepositoryIndexer

    indexer = RepositoryIndexer(state.root)
    try:
        index = indexer.load()
        if not index.files:
            index = indexer.build()
    except (OSError, ValueError):
        index = indexer.build()

    analysis = architecture.analyse(
        index,
        routes=indexer.api_routes(),
        models=indexer.database_models(),
        env_vars=indexer.environment_variables(),
    )
    nodes, edges = architecture.layout(analysis)
    if not nodes:
        raise HTTPException(
            status_code=400,
            detail=(
                "Nothing to draw: the repository index is empty. Run the "
                "repository indexer first (Insights ▸ Repository)."
            ),
        )

    name = f"{state.context.settings.project.name} architecture"
    design = state.design.create(
        name,
        "architecture",
        nodes=nodes,
        edges=[
            {
                "id": f"{edge['source']}-{edge['target']}",
                "source": edge["source"],
                "target": edge["target"],
                "label": edge["label"],
            }
            for edge in edges
        ],
        metadata={
            "generated": architecture.summary(analysis),
            "generated_from": "repository-index",
            "module_count": len(nodes),
            "edge_count": len(edges),
            "env_vars": analysis["env_vars"],
        },
    )
    return design.model_dump(mode="json")


# --------------------------------------------------------------------- frames


class FrameRequest(BaseModel):
    name: str = ""
    frame_id: str | None = None
    width: int = 1440
    height: int = 900
    children: list[dict] | None = None


class UpdateFrameRequest(BaseModel):
    name: str | None = None
    width: int | None = None
    height: int | None = None
    #: Replaces the child list wholesale — a merge would make removing an
    #: element impossible.
    children: list[dict] | None = None


@router.post("/{design_id}/frames")
def add_frame(
    state: Annotated[GuiState, Depends(get_state)], design_id: str, body: FrameRequest
) -> dict:
    """Add a UI mock-up frame: a device viewport with nested elements."""
    return _handle(state.design.add_frame)(
        design_id,
        name=body.name,
        frame_id=body.frame_id,
        width=body.width,
        height=body.height,
        children=body.children,
    ).model_dump(mode="json")


@router.patch("/{design_id}/frames/{frame_id}")
def update_frame(
    state: Annotated[GuiState, Depends(get_state)],
    design_id: str,
    frame_id: str,
    body: UpdateFrameRequest,
) -> dict:
    return _handle(state.design.update_frame)(
        design_id,
        frame_id,
        name=body.name,
        width=body.width,
        height=body.height,
        children=body.children,
    ).model_dump(mode="json")


@router.delete("/{design_id}/frames/{frame_id}")
def delete_frame(
    state: Annotated[GuiState, Depends(get_state)], design_id: str, frame_id: str
) -> dict:
    return _handle(state.design.delete_frame)(design_id, frame_id).model_dump(mode="json")


@router.post("")
def create_design(
    state: Annotated[GuiState, Depends(get_state)], body: CreateDesignRequest
) -> dict:
    try:
        design = state.design.create(body.name, body.type)
    except DesignError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return design.model_dump(mode="json")


@router.get("/{design_id}")
def get_design(state: Annotated[GuiState, Depends(get_state)], design_id: str) -> dict:
    return _handle(state.design.get)(design_id).model_dump(mode="json")


@router.put("/{design_id}")
def replace_design(
    state: Annotated[GuiState, Depends(get_state)], design_id: str, body: Design
) -> dict:
    """Save a whole canvas, refusing to overwrite work done since it was loaded.

    The body carries the version the editor loaded, which is exactly the
    optimistic-concurrency token this needs: two windows both editing version 2
    used to both write version 3, and the second silently erased the first.
    """
    if body.id != design_id:
        raise HTTPException(status_code=400, detail="Design id mismatch")
    try:
        design = state.design.replace(body, expected_version=body.version)
    except DesignConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": str(exc), "stored_version": exc.stored_version},
        ) from exc
    except DesignError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return design.model_dump(mode="json")


@router.get("/{design_id}/revisions")
def list_revisions(state: Annotated[GuiState, Depends(get_state)], design_id: str) -> dict:
    """Every kept version of this design, newest first."""
    return {"revisions": _handle(state.design.revisions)(design_id)}


@router.get("/{design_id}/revisions/{version}")
def read_revision(
    state: Annotated[GuiState, Depends(get_state)], design_id: str, version: int
) -> dict:
    return _handle(state.design.revision)(design_id, version).model_dump(mode="json")


@router.post("/{design_id}/revisions/{version}/restore")
def restore_revision(
    state: Annotated[GuiState, Depends(get_state)], design_id: str, version: int
) -> dict:
    """Bring an old version back as the newest one, keeping everything between."""
    return _handle(state.design.restore)(design_id, version).model_dump(mode="json")


@router.delete("/{design_id}")
def delete_design(state: Annotated[GuiState, Depends(get_state)], design_id: str) -> dict:
    _handle(state.design.delete)(design_id)
    return {"id": design_id, "deleted": True}


@router.post("/{design_id}/nodes")
def add_node(
    state: Annotated[GuiState, Depends(get_state)], design_id: str, body: AddNodeRequest
) -> dict:
    return _handle(state.design.add_node)(
        design_id,
        label=body.label,
        node_type=body.node_type,
        node_id=body.node_id,
        x=body.x,
        y=body.y,
        data=body.data,
    ).model_dump(mode="json")


@router.patch("/{design_id}/nodes/{node_id}")
def update_node(
    state: Annotated[GuiState, Depends(get_state)],
    design_id: str,
    node_id: str,
    body: UpdateNodeRequest,
) -> dict:
    return _handle(state.design.update_node)(
        design_id,
        node_id,
        label=body.label,
        node_type=body.node_type,
        x=body.x,
        y=body.y,
        data=body.data,
    ).model_dump(mode="json")


@router.delete("/{design_id}/nodes/{node_id}")
def delete_node(
    state: Annotated[GuiState, Depends(get_state)], design_id: str, node_id: str
) -> dict:
    return _handle(state.design.delete_node)(design_id, node_id).model_dump(mode="json")


@router.post("/{design_id}/edges")
def connect(
    state: Annotated[GuiState, Depends(get_state)], design_id: str, body: ConnectRequest
) -> dict:
    return _handle(state.design.connect)(
        design_id, body.source, body.target, label=body.label
    ).model_dump(mode="json")


@router.delete("/{design_id}/edges/{edge_id}")
def disconnect(
    state: Annotated[GuiState, Depends(get_state)], design_id: str, edge_id: str
) -> dict:
    return _handle(state.design.disconnect)(design_id, edge_id=edge_id).model_dump(mode="json")


# ---------------------------------------------------------------------- plans


class ProposePlanRequest(BaseModel):
    """Ask for an implementation plan. The session carries the conversation."""

    session_id: str = Field(min_length=1)
    profile: str = ""


class RejectPlanRequest(BaseModel):
    #: Recorded so the next proposal can address it.
    reason: str = ""


class ImplementRequest(BaseModel):
    session_id: str = Field(min_length=1)
    profile: str = ""


def _plans(state: GuiState) -> DesignPlanApplicationService:
    return DesignPlanApplicationService(state.context, state.design, state.missions)


@router.get("/{design_id}/plan")
def read_plan(state: Annotated[GuiState, Depends(get_state)], design_id: str) -> dict:
    """The plan, and whether implementation is allowed right now.

    ``can_implement`` and its reason come from the same gate the implement
    endpoint uses, so the button and the endpoint can never disagree about
    whether work may start.
    """
    return _handle(_plans(state).status)(design_id)


@router.post("/{design_id}/plan")
async def propose_plan(
    state: Annotated[GuiState, Depends(get_state)],
    design_id: str,
    body: ProposePlanRequest,
) -> dict:
    """Run one read-only turn to produce a plan.

    The turn cannot write, edit, delete, or run anything: the tool surface omits
    every mutating tool, ``EditTools`` refuses mutations, and no command runner
    is attached. "Propose a plan before writing code" used to be a sentence in a
    prompt, which the model was free to ignore — and did.

    It still takes the project-wide turn slot. Read-only or not, it is a full
    agent turn against the same runtime, and two turns sharing one gateway
    budget and one context pipeline is the interleaving the lock exists to stop.
    """
    service = _plans(state)
    try:
        await state.run_exclusive_turn(
            lambda: service.propose(design_id, body.session_id, profile_override=body.profile)
        )
    except TurnBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DesignError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return service.status(design_id)


@router.post("/{design_id}/plan/approve")
def approve_plan(state: Annotated[GuiState, Depends(get_state)], design_id: str) -> dict:
    try:
        _plans(state).approve(design_id)
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _plans(state).status(design_id)


@router.post("/{design_id}/plan/reject")
def reject_plan(
    state: Annotated[GuiState, Depends(get_state)],
    design_id: str,
    body: RejectPlanRequest | None = None,
) -> dict:
    try:
        _plans(state).reject(design_id, (body or RejectPlanRequest()).reason)
    except PlanError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _plans(state).status(design_id)


@router.post("/{design_id}/implement")
async def implement_design(
    state: Annotated[GuiState, Depends(get_state)],
    design_id: str,
    body: ImplementRequest,
) -> dict:
    """Carry out an approved plan.

    Refused with 409 when there is no approved plan for *this version* of the
    design, and with 409 again when another turn already holds the project. This
    writes to the working tree, so running it beside a CODE or Workspace turn
    would have two agents editing the same files with neither aware of the
    other.

    The version check is the part that matters most: a plan written against
    version 4 of a canvas describes a canvas that no longer exists once someone
    has rearranged it, and implementing it would build the wrong thing while
    looking entirely legitimate.
    """
    service = _plans(state)
    try:
        outcome = await state.run_exclusive_turn(
            lambda: service.implement(design_id, body.session_id, profile_override=body.profile)
        )
    except TurnBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PlanGateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DesignError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    status = service.status(design_id)
    plan = status.get("plan") or {}
    return {
        # The plan's own state, not "we asked and it returned". A turn whose
        # verification failed, or that changed nothing, leaves the plan approved
        # and this False — which is what the user needs to see.
        "implemented": isinstance(plan, dict) and plan.get("status") == "implemented",
        "verified": outcome.verified,
        "summary": outcome.answer or outcome.summary,
        "files": sorted({diff.path for diff in outcome.diffs}),
        **status,
    }
