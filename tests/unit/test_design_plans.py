"""Plan-first implementation: the gate, the parser, frames, and code analysis.

The gate tests are the point. "Propose a plan before writing code" was a
sentence in a prompt, which is a request rather than a rule; these assert it is
now a state machine that refuses, and that the read-only planning surface has no
way to write even if the model tries.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.agents.tool_schemas import MUTATING_ACTIONS, PLANNING_TOOL_SPECS
from daino.application.design_plan_service import parse_plan
from daino.design import DesignService
from daino.design.plans import PlanError, PlanGateError, PlanStep, PlanStore
from daino.schemas.core import RepositoryIndex
from tests.conftest import repository_index


@pytest.fixture
def service(tmp_path: Path) -> Iterator[DesignService]:
    yield DesignService(tmp_path)


@pytest.fixture
def plans(service: DesignService) -> PlanStore:
    return PlanStore(service._designs_dir())


def _propose(plans: PlanStore, design_id: str, *, version: int = 2) -> None:
    plans.propose(
        design_id,
        summary="Add the login screen.",
        steps=[PlanStep(description="Add a route", paths=["src/routes.ts"])],
        reviewed_paths=["src/routes.ts"],
        questions=[],
        session_id="session-1",
        design_version=version,
    )


# ------------------------------------------------------------------ the gate


def test_implementation_is_refused_with_no_plan(
    service: DesignService, plans: PlanStore
) -> None:
    design = service.create("Login", "ui")

    with pytest.raises(PlanGateError) as caught:
        plans.require_approved(design.id, design_version=design.version)

    assert "no plan yet" in str(caught.value)


def test_implementation_is_refused_while_a_plan_is_only_proposed(
    service: DesignService, plans: PlanStore
) -> None:
    """A proposal nobody has read is not an agreement."""
    design = service.create("Login", "ui")
    _propose(plans, design.id, version=design.version)

    with pytest.raises(PlanGateError) as caught:
        plans.require_approved(design.id, design_version=design.version)

    assert "not been approved" in str(caught.value)


def test_an_approved_plan_opens_the_gate(
    service: DesignService, plans: PlanStore
) -> None:
    design = service.create("Login", "ui")
    _propose(plans, design.id, version=design.version)
    plans.approve(design.id)

    approved = plans.require_approved(design.id, design_version=design.version)

    assert approved.status == "approved"


def test_a_rejected_plan_keeps_the_gate_shut_and_says_why(
    service: DesignService, plans: PlanStore
) -> None:
    design = service.create("Login", "ui")
    _propose(plans, design.id, version=design.version)
    plans.reject(design.id, "Do not touch the auth module.")

    with pytest.raises(PlanGateError) as caught:
        plans.require_approved(design.id, design_version=design.version)

    assert "Do not touch the auth module." in str(caught.value)


def test_a_plan_for_an_older_version_of_the_design_is_refused(
    service: DesignService, plans: PlanStore
) -> None:
    """The check most easily left out, and the one that matters most.

    A plan written against version 4 describes a canvas that no longer exists
    once someone has rearranged it. Implementing it would build the wrong thing
    while looking entirely legitimate.
    """
    design = service.create("Login", "ui")
    _propose(plans, design.id, version=design.version)
    plans.approve(design.id)

    # Someone moves a node: the design advances, the plan does not.
    moved = service.add_node(design.id, label="Password field")

    with pytest.raises(PlanGateError) as caught:
        plans.require_approved(design.id, design_version=moved.version)

    message = str(caught.value)
    assert f"version {design.version}" in message
    assert f"now version {moved.version}" in message


def test_an_implemented_plan_cannot_be_implemented_twice(
    service: DesignService, plans: PlanStore
) -> None:
    design = service.create("Login", "ui")
    _propose(plans, design.id, version=design.version)
    plans.approve(design.id)
    plans.mark_implemented(design.id)

    with pytest.raises(PlanGateError) as caught:
        plans.require_approved(design.id, design_version=design.version)

    assert "already been implemented" in str(caught.value)


def test_approving_something_that_is_not_proposed_is_refused(
    service: DesignService, plans: PlanStore
) -> None:
    design = service.create("Login", "ui")
    _propose(plans, design.id, version=design.version)
    plans.reject(design.id, "no")

    with pytest.raises(PlanError, match="rejected"):
        plans.approve(design.id)


def test_a_new_proposal_carries_the_last_rejection_forward(
    service: DesignService, plans: PlanStore
) -> None:
    """So the UI can show "you asked for X; here is a plan that does X"."""
    design = service.create("Login", "ui")
    _propose(plans, design.id, version=design.version)
    plans.reject(design.id, "Use the existing form component.")

    _propose(plans, design.id, version=design.version)

    plan = plans.get(design.id)
    assert plan is not None
    assert plan.status == "proposed"
    assert plan.rejection_reason == "Use the existing form component."


def test_a_plan_dies_with_its_design(service: DesignService, plans: PlanStore) -> None:
    design = service.create("Login", "ui")
    _propose(plans, design.id, version=design.version)
    assert plans.get(design.id) is not None

    service.delete(design.id)

    assert plans.get(design.id) is None


# ------------------------------------------------------- the planning surface


def test_the_planning_surface_has_no_way_to_write() -> None:
    """A restriction the model can talk its way past is not a restriction."""
    names = {spec["function"]["name"] for spec in PLANNING_TOOL_SPECS}

    assert not (names & MUTATING_ACTIONS)
    # It still has everything needed to study the code and answer.
    assert {"read_file", "search_text", "grep", "glob", "respond"} <= names


# -------------------------------------------------------------- plan parsing


def test_a_well_formed_plan_is_parsed_into_structure() -> None:
    parsed = parse_plan(
        textwrap.dedent(
            """\
            ## Summary
            Adds a login screen wired to the existing session store.

            ## Steps
            1. Add the route — `src/routes.ts`, `src/pages/Login.tsx`
            2. Wire the session store — `src/store/session.ts`

            ## Reviewed
            - `src/routes.ts` — where routes are declared
            - `src/store/session.ts` — the existing auth state

            ## Questions
            - Should a failed login redirect, or stay on the page?
            """
        )
    )

    assert "login screen" in str(parsed["summary"])
    steps = parsed["steps"]
    assert len(steps) == 2  # type: ignore[arg-type]
    assert steps[0].description == "Add the route"  # type: ignore[index]
    assert steps[0].paths == ["src/routes.ts", "src/pages/Login.tsx"]  # type: ignore[index]
    assert parsed["reviewed"] == ["src/routes.ts", "src/store/session.ts"]
    assert len(parsed["questions"]) == 1  # type: ignore[arg-type]


def test_a_plan_with_no_headings_still_yields_a_summary() -> None:
    """Rejecting a usable plan over formatting would spend a turn on nothing."""
    parsed = parse_plan("I would add a login route and wire it to the store.")

    assert "login route" in str(parsed["summary"])
    assert parsed["steps"] == []


def test_an_absent_questions_section_means_no_questions() -> None:
    """Forgiving about form, never about content."""
    parsed = parse_plan("## Summary\nSmall change.\n\n## Steps\n1. Do it\n")

    assert parsed["questions"] == []
    assert len(parsed["steps"]) == 1  # type: ignore[arg-type]


def test_bulleted_steps_are_accepted_as_readily_as_numbered_ones() -> None:
    parsed = parse_plan("## Steps\n- First thing\n* Second thing\n")

    assert [step.description for step in parsed["steps"]] == [  # type: ignore[union-attr]
        "First thing",
        "Second thing",
    ]


# -------------------------------------------------------------------- frames


def test_frames_can_be_created_updated_and_deleted(service: DesignService) -> None:
    design = service.create("Screens", "ui")

    with_frame = service.add_frame(design.id, name="Login", width=390, height=844)
    assert [(item.id, item.name, item.width) for item in with_frame.frames] == [
        ("login", "Login", 390)
    ]

    updated = service.update_frame(
        design.id, "login", children=[{"type": "button", "label": "Sign in"}]
    )
    assert updated.frames[0].children == [{"type": "button", "label": "Sign in"}]

    # Children are replaced, not merged — otherwise removing one is impossible.
    emptied = service.update_frame(design.id, "login", children=[])
    assert emptied.frames[0].children == []

    assert service.delete_frame(design.id, "login").frames == []


def test_a_second_frame_with_the_same_name_gets_its_own_id(
    service: DesignService,
) -> None:
    design = service.create("Screens", "ui")
    service.add_frame(design.id, name="Login")

    twice = service.add_frame(design.id, name="Login")

    assert [item.id for item in twice.frames] == ["login", "login-2"]


def test_frame_operations_bump_the_version_like_any_other_edit(
    service: DesignService,
) -> None:
    """So the plan gate notices a canvas that has moved on."""
    design = service.create("Screens", "ui")
    before = design.version

    after = service.add_frame(design.id, name="Login")

    assert after.version > before


# ------------------------------------------------------------- code analysis


def _index(paths: dict[str, list[str]]) -> RepositoryIndex:
    """A synthetic index: path -> its import statements."""
    return repository_index(paths)


def test_modules_come_from_the_layout_and_edges_from_imports() -> None:
    from daino.design import architecture

    analysis = architecture.analyse(
        _index(
            {
                "api/routes.py": ["core.models", "core.db"],
                "api/views.py": ["core.models"],
                "core/models.py": [],
                "core/db.py": [],
                "worker/jobs.py": ["core.db"],
            }
        )
    )

    names = {module.name for module in analysis["modules"]}  # type: ignore[union-attr]
    assert names == {"api", "core", "worker"}
    edges = {(item["source"], item["target"]): item["weight"] for item in analysis["edges"]}  # type: ignore[union-attr]
    # Two files in `api` import from `core`, so the edge carries a weight of 2 —
    # the difference between "these touch" and "these are welded together".
    assert edges[("api", "core")] == 3
    assert edges[("worker", "core")] == 1


def test_a_single_package_repository_is_not_drawn_as_one_box() -> None:
    """The most common layout of all, and the one a fixed depth gets wrong."""
    from daino.design import architecture

    analysis = architecture.analyse(
        _index(
            {
                "myapp/api/routes.py": ["myapp.core.models"],
                "myapp/api/views.py": ["myapp.core.models"],
                "myapp/core/models.py": [],
                "myapp/worker/jobs.py": ["myapp.core.models"],
            }
        )
    )

    names = {module.name for module in analysis["modules"]}  # type: ignore[union-attr]
    assert names == {"myapp/api", "myapp/core", "myapp/worker"}


def test_external_imports_do_not_become_edges() -> None:
    """A diagram of PyPI is the dependency list, which says nothing about this."""
    from daino.design import architecture

    analysis = architecture.analyse(
        _index({"api/routes.py": ["fastapi", "pydantic", "os"], "core/models.py": []})
    )

    assert analysis["edges"] == []


def test_tests_and_generated_output_are_left_out() -> None:
    from daino.design import architecture

    analysis = architecture.analyse(
        _index(
            {
                "api/routes.py": [],
                "tests/test_routes.py": ["api.routes"],
                "node_modules/pkg/index.ts": [],
                "dist/bundle.js": [],
            }
        )
    )

    assert {module.name for module in analysis["modules"]} == {"api"}  # type: ignore[union-attr]


def test_the_layout_places_dependencies_below_their_dependents() -> None:
    """The one thing a diagram carries that a file list cannot."""
    from daino.design import architecture

    analysis = architecture.analyse(
        _index({"api/routes.py": ["core.models"], "core/models.py": []})
    )
    nodes, _ = architecture.layout(analysis)

    by_id = {node["id"]: node for node in nodes}
    assert by_id["api"]["position"]["y"] < by_id["core"]["position"]["y"]


def test_layout_nodes_are_shaped_as_the_design_model_expects() -> None:
    """The bug this guards: pydantic drops keys it does not know.

    `DesignService.create` validates these dicts straight into `DesignNode`, so
    a node described with `node_type` and a flat `x`/`y` — add_node's keyword
    names — loses both silently. Every node came out "default" and stacked at
    the origin, which renders as one pile with the whole layout thrown away.
    """
    from daino.design import architecture
    from daino.design.models import DesignNode

    analysis = architecture.analyse(
        _index({"api/routes.py": ["core.models"], "core/models.py": []})
    )
    nodes, _ = architecture.layout(analysis)

    for described in nodes:
        node = DesignNode.model_validate(described)
        # The keys actually survived validation.
        assert node.type == described["type"]
        assert node.position.x == described["position"]["x"]
        assert node.position.y == described["position"]["y"]
    # And they are not all in the same place.
    assert len({node["position"]["y"] for node in nodes}) > 1


def test_an_import_cycle_does_not_hang_the_layout() -> None:
    """Cycles are common in real code and are not an error here."""
    from daino.design import architecture

    analysis = architecture.analyse(
        _index({"a/one.py": ["b.two"], "b/two.py": ["a.one"]})
    )

    nodes, edges = architecture.layout(analysis)

    assert len(nodes) == 2
    assert len(edges) == 2


def test_the_generated_design_states_its_own_limits() -> None:
    """A generated picture that does not admit it was generated gets over-trusted."""
    from daino.design import architecture

    analysis = architecture.analyse(
        _index({"api/routes.py": ["core.models"], "core/models.py": []})
    )

    text = architecture.summary(analysis)

    assert "import" in text
    assert "starting point" in text
