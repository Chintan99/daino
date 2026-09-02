"""Plan-first implementation: a design cannot become code until a plan is agreed.

"Propose a plan before writing code" used to be a sentence in a prompt, which is
a request rather than a rule — the model was free to start writing on the same
turn, and frequently did. This module makes it a state machine with a gate:

    absent ──propose──▶ proposed ──approve──▶ approved ──implement──▶ implemented
                            │
                            └──reject──▶ absent (with the rejection recorded)

Two things enforce it, not one:

* **The gate.** ``implement`` refuses unless the stored plan is ``approved``.
  That is a check on the server, not a hope about the prompt.
* **The tool surface.** The planning turn is given a read-only tool set and a
  read-only ``EditTools``, so a model that decides to start writing anyway
  cannot. A restriction the model can talk its way past is not a restriction.

The plan lives next to the design it is for, under
``.daino/designs/<id>/plan.json``, so deleting a design takes its plan with it
and a plan can never outlive its subject.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

#: Where a plan sits, relative to its design directory.
PLAN_FILE = "plan.json"

PlanStatus = Literal["proposed", "approved", "rejected", "implemented"]


class PlanStep(BaseModel):
    """One thing the implementation will do."""

    #: What will change, in the user's terms rather than the model's.
    description: str
    #: Repository paths this step expects to touch. Advisory — it is what the
    #: plan claimed, which is exactly what makes it reviewable.
    paths: list[str] = Field(default_factory=list)


class DesignPlan(BaseModel):
    """How a design will be turned into code, and whether that was agreed."""

    design_id: str
    status: PlanStatus = "proposed"
    #: The narrative: what is being built and how it fits the existing code.
    summary: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    #: What the planner read to write this. Shown so a reader can judge whether
    #: it looked at the right things.
    reviewed_paths: list[str] = Field(default_factory=list)
    #: Open questions the planner could not resolve. A plan with these is still
    #: approvable — the user may know the answers — but they are surfaced.
    questions: list[str] = Field(default_factory=list)
    #: Why the user rejected it, kept so the next proposal can address it.
    rejection_reason: str = ""
    #: The session the plan was produced in, so implementation continues the
    #: same conversation rather than starting a fresh one with no context.
    session_id: str = ""
    #: The design's version when the plan was written. A plan for version 4 is
    #: not a plan for version 9, and the gate says so.
    design_version: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    #: Set once implementation has run, so a second Implement is a decision
    #: rather than an accident.
    implemented_at: datetime | None = None


class PlanError(Exception):
    """Raised when an operation is not allowed in the plan's current state."""


class PlanGateError(PlanError):
    """Raised when implementation is attempted without an approved plan.

    Separate from :class:`PlanError` so the route can answer 409 — this is not a
    malformed request, it is a request made too early.
    """


class PlanStore:
    """Read and write the plan beside its design."""

    def __init__(self, designs_dir: Path) -> None:
        self.designs_dir = Path(designs_dir)

    def _path(self, design_id: str) -> Path:
        return self.designs_dir / design_id / PLAN_FILE

    def get(self, design_id: str) -> DesignPlan | None:
        path = self._path(design_id)
        if not path.is_file():
            return None
        try:
            return DesignPlan.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A plan that cannot be read is treated as absent rather than as a
            # crash: the worst outcome is having to plan again, and the best
            # alternative — refusing to open the design at all — is worse.
            return None
    def save(self, plan: DesignPlan) -> DesignPlan:
        plan.updated_at = datetime.now(UTC)
        path = self._path(plan.design_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(plan.model_dump(mode="json"), indent=2), encoding="utf-8"
        )
        return plan

    def delete(self, design_id: str) -> None:
        path = self._path(design_id)
        if path.is_file():
            path.unlink()

    # ------------------------------------------------------------ transitions

    def propose(
        self,
        design_id: str,
        *,
        summary: str,
        steps: list[PlanStep],
        reviewed_paths: list[str],
        questions: list[str],
        session_id: str,
        design_version: int,
    ) -> DesignPlan:
        """Record a fresh proposal, replacing any earlier one.

        Replacing rather than versioning: a superseded proposal is not something
        anyone goes back to, and keeping several would make "which plan was
        approved" a question the gate has to answer.
        """
        previous = self.get(design_id)
        return self.save(
            DesignPlan(
                design_id=design_id,
                status="proposed",
                summary=summary,
                steps=steps,
                reviewed_paths=reviewed_paths,
                questions=questions,
                session_id=session_id,
                design_version=design_version,
                # Carrying the last rejection forward is what lets the UI show
                # "you asked for X; here is a plan that does X".
                rejection_reason=previous.rejection_reason if previous else "",
            )
        )

    def approve(self, design_id: str) -> DesignPlan:
        plan = self._require(design_id)
        if plan.status == "approved":
            return plan
        if plan.status != "proposed":
            raise PlanError(
                f"This plan is {plan.status}, so there is nothing to approve. "
                "Propose a new one."
            )
        plan.status = "approved"
        plan.rejection_reason = ""
        return self.save(plan)

    def reject(self, design_id: str, reason: str = "") -> DesignPlan:
        plan = self._require(design_id)
        plan.status = "rejected"
        plan.rejection_reason = reason.strip()
        return self.save(plan)

    def mark_implemented(self, design_id: str) -> DesignPlan:
        plan = self._require(design_id)
        plan.status = "implemented"
        plan.implemented_at = datetime.now(UTC)
        return self.save(plan)

    def require_approved(self, design_id: str, *, design_version: int) -> DesignPlan:
        """The gate. Returns the plan, or explains why work cannot start.

        The version check is the part that is easy to leave out and matters:
        a plan written against version 4 of a canvas describes a canvas that no
        longer exists once someone has moved things around, and implementing it
        would build the wrong thing while looking entirely legitimate.
        """
        plan = self.get(design_id)
        if plan is None:
            raise PlanGateError(
                "This design has no plan yet. Propose one first — nothing is "
                "written until you have read it and approved it."
            )
        if plan.status == "rejected":
            raise PlanGateError(
                "The last plan was rejected"
                + (f": {plan.rejection_reason}" if plan.rejection_reason else "")
                + ". Propose a new one."
            )
        if plan.status == "proposed":
            raise PlanGateError(
                "This plan has not been approved yet. Read it and approve it, "
                "or reject it and say what to change."
            )
        if plan.status == "implemented":
            raise PlanGateError(
                "This plan has already been implemented. Propose a new one to "
                "make further changes."
            )
        if plan.design_version and plan.design_version != design_version:
            raise PlanGateError(
                f"This plan was written for version {plan.design_version} of the "
                f"design, which is now version {design_version}. Propose a new "
                "plan so it describes the canvas as it is now."
            )
        return plan

    def _require(self, design_id: str) -> DesignPlan:
        plan = self.get(design_id)
        if plan is None:
            raise PlanError(f"Design {design_id!r} has no plan.")
        return plan


#: What the planner is asked to produce. Kept here rather than in the prompt
#: module because the shape and the gate have to agree: every field the gate
#: reads is a field this asks for.
PLAN_INSTRUCTION = """\
Produce an implementation plan for the design "{name}" (id: {design_id}).

You are in PLANNING MODE. You have read-only tools: you can read, search, and
list files, and you cannot write, edit, delete, or run commands. Do not attempt
to — the tools are not there, and a turn spent trying is a turn wasted.

The design contains:
{outline}

Do this:
1. Read the parts of the repository this would touch. Name them.
2. Write a plan the user can judge: what you would change, in what order, and
   in which files.
3. State anything you could not determine from the code, as a question.

Answer with a `respond` call containing markdown in exactly this shape:

## Summary
Two or three sentences: what gets built, and how it fits what is already here.

## Steps
1. <what changes> — `path/one.ts`, `path/two.ts`
2. ...

## Reviewed
- `path/read.ts` — why it mattered

## Questions
- <anything you could not settle from the code; omit the section if none>
"""
