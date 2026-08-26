"""Design-artifact CRUD and granular node/edge mutations.

Both this route and the agent's design tools call the same
:class:`~daino.design.DesignService`, so manual canvas edits and AI edits mutate
one shared document.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from daino.design import Design, DesignError
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
    """Seed an architecture design from a best-effort scan of the repository.

    Deterministic (no model call): detected frameworks and top-level source
    directories become nodes; a database node is added when models are found. The
    result is a starting point the user and agent refine, not a reverse-engineered
    truth.
    """
    from daino.repository import RepositoryIndexer

    indexer = RepositoryIndexer(state.root)
    try:
        index = indexer.load()
    except (OSError, ValueError):
        index = indexer.build()

    frameworks = sorted(index.frameworks)[:6]
    has_db = bool(indexer.database_models())
    has_routes = bool(indexer.api_routes())

    name = f"{state.context.settings.project.name} architecture"
    design = state.design.create(name, "architecture")
    y = 0
    previous: str | None = None
    for framework in frameworks or ["Application"]:
        node = state.design.add_node(
            design.id, label=framework, node_type="service", x=0, y=y
        ).nodes[-1]
        if previous is not None:
            state.design.connect(design.id, previous, node.id)
        previous = node.id
        y += 120
    if has_routes and previous is not None:
        api = state.design.add_node(design.id, label="API", node_type="api", x=280, y=0).nodes[-1]
        state.design.connect(design.id, previous, api.id, label="serves")
        previous = api.id
    if has_db and previous is not None:
        db = state.design.add_node(design.id, label="Database", node_type="database", x=280, y=160)
        state.design.connect(design.id, previous, db.nodes[-1].id, label="persists")
    return state.design.get(design.id).model_dump(mode="json")


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
    if body.id != design_id:
        raise HTTPException(status_code=400, detail="Design id mismatch")
    return _handle(state.design.replace)(body).model_dump(mode="json")


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
