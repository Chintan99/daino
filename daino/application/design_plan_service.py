"""Turning a design into code, with a plan the user has actually agreed to.

The flow this drives:

1. **Propose.** One agent turn with a read-only tool surface studies the
   repository and writes a plan. It cannot write, edit, delete, or run anything
   — enforced by the tool list, by ``EditTools(read_only=True)``, and by having
   no command runner, rather than by asking it not to.
2. **Review.** The plan is parsed into structure and stored. The user reads it.
3. **Approve or reject.** Rejecting records why, so the next proposal can answer
   it.
4. **Implement.** Refused unless an approved plan exists *for this version of
   the design*.

The version check in step 4 is the part that would be easy to omit and is worth
the most: a plan written against version 4 of a canvas describes a canvas that
no longer exists once someone has rearranged it, and implementing it would build
the wrong thing while looking completely legitimate.
"""

from __future__ import annotations

import re

from daino.application.context import ProjectContext
from daino.application.mission_service import MissionApplicationService
from daino.design import DesignService
from daino.design.models import Design
from daino.design.plans import (
    PLAN_INSTRUCTION,
    DesignPlan,
    PlanGateError,
    PlanStep,
    PlanStore,
)
from daino.schemas import ChatOutcome

#: Markdown headings the planner is asked for, mapped to what they populate.
_SECTIONS = {
    "summary": "summary",
    "steps": "steps",
    "reviewed": "reviewed",
    "questions": "questions",
}
#: Backticked paths in a step or reviewed line.
_PATHS = re.compile(r"`([^`\n]+)`")
#: A numbered or bulleted list item.
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")


class DesignPlanApplicationService:
    """Propose, review, and gate the implementation of a design."""

    def __init__(
        self,
        context: ProjectContext,
        design: DesignService,
        missions: MissionApplicationService,
    ) -> None:
        self.context = context
        self.design = design
        self.missions = missions
        self.plans = PlanStore(design._designs_dir())

    # ---------------------------------------------------------------- reading

    def get(self, design_id: str) -> DesignPlan | None:
        return self.plans.get(design_id)

    def status(self, design_id: str) -> dict[str, object]:
        """The plan and whether implementation is currently allowed.

        The ``can_implement`` flag and its reason are computed by asking the
        gate rather than by re-deriving the rules here — one place decides, so
        the button and the endpoint can never disagree.
        """
        design = self.design.get(design_id)
        plan = self.plans.get(design_id)
        try:
            self.plans.require_approved(design_id, design_version=design.version)
            allowed, reason = True, ""
        except PlanGateError as exc:
            allowed, reason = False, str(exc)
        return {
            "plan": plan.model_dump(mode="json") if plan else None,
            "can_implement": allowed,
            "reason": reason,
            "design_version": design.version,
            "stale": bool(
                plan and plan.design_version and plan.design_version != design.version
            ),
        }

    # -------------------------------------------------------------- proposing

    async def propose(
        self, design_id: str, session_id: str, *, profile_override: str = ""
    ) -> DesignPlan:
        """Run one read-only turn to produce a plan."""
        design = self.design.get(design_id)
        outcome = await self.missions.chat(
            PLAN_INSTRUCTION.format(
                name=design.name,
                design_id=design.id,
                outline=_outline(design),
            ),
            session_id,
            profile_override=profile_override,
            read_only=True,
        )
        parsed = parse_plan(outcome.answer or outcome.summary or "")
        return self.plans.propose(
            design_id,
            summary=parsed["summary"],
            steps=parsed["steps"],
            reviewed_paths=parsed["reviewed"],
            questions=parsed["questions"],
            session_id=session_id,
            design_version=design.version,
        )

    def approve(self, design_id: str) -> DesignPlan:
        return self.plans.approve(design_id)

    def reject(self, design_id: str, reason: str = "") -> DesignPlan:
        return self.plans.reject(design_id, reason)

    # ------------------------------------------------------------ implementing

    async def implement(
        self, design_id: str, session_id: str, *, profile_override: str = ""
    ) -> ChatOutcome:
        """Carry out an approved plan. Refuses if there is not one."""
        design = self.design.get(design_id)
        plan = self.plans.require_approved(design_id, design_version=design.version)
        outcome = await self.missions.chat(
            _implementation_instruction(design, plan),
            session_id,
            profile_override=profile_override,
        )
        self.plans.mark_implemented(design_id)
        return outcome


def _outline(design: Design) -> str:
    """The design as text the planner can read.

    Nodes, edges and frames rather than the raw JSON: the model has to reason
    about the *structure*, and handing it a position-laden document invites it
    to describe coordinates instead.
    """
    lines: list[str] = [f"- Type: {design.type}"]
    for node in design.nodes[:60]:
        kind = str(node.data.get("kind") or node.type)
        source = node.data.get("source_path")
        detail = f" (from `{source}`)" if source else ""
        lines.append(f"- Node `{node.id}`: {node.label or kind} [{kind}]{detail}")
    if len(design.nodes) > 60:
        lines.append(f"- …and {len(design.nodes) - 60} more nodes")
    for edge in design.edges[:60]:
        label = f" — {edge.label}" if edge.label else ""
        lines.append(f"- Edge: {edge.source} → {edge.target}{label}")
    for frame in design.frames[:20]:
        lines.append(
            f"- Frame `{frame.id}`: {frame.name or 'untitled'} "
            f"({frame.width}×{frame.height}, {len(frame.children)} elements)"
        )
    return "\n".join(lines) or "- (the canvas is empty)"


def _implementation_instruction(design: Design, plan: DesignPlan) -> str:
    """The build turn's brief: the approved plan, and only the approved plan."""
    steps = "\n".join(
        f"{index}. {step.description}"
        + (f" — {', '.join(f'`{path}`' for path in step.paths)}" if step.paths else "")
        for index, step in enumerate(plan.steps, start=1)
    )
    return (
        f'Implement the approved plan for the design "{design.name}" '
        f"(id: {design.id}, version {design.version}).\n\n"
        f"## The plan you are implementing\n\n{plan.summary}\n\n"
        f"### Steps\n{steps or '(no steps were listed)'}\n\n"
        "Work the steps in order. This plan was reviewed and approved as "
        "written, so do not widen it: if a step turns out to need something the "
        "plan does not mention, do the rest and say so in your summary rather "
        "than deciding for the user. Verify what you can before finishing."
    )


def parse_plan(markdown: str) -> dict[str, object]:
    """Read the planner's markdown into the structure the gate stores.

    Forgiving on purpose. A model that writes "## Plan" instead of "## Steps",
    or bullets instead of numbers, has still produced a usable plan, and
    rejecting it on formatting would spend a turn on nothing. What is *not*
    guessed at is the content: an absent Questions section means no questions,
    never an invented one.
    """
    sections: dict[str, list[str]] = {name: [] for name in _SECTIONS.values()}
    current: str | None = None
    for raw in markdown.splitlines():
        line = raw.rstrip()
        heading = _heading(line)
        if heading is not None:
            current = heading
            continue
        if current:
            sections[current].append(line)

    summary = "\n".join(sections["summary"]).strip()
    if not summary:
        # No recognised Summary heading: the leading prose is the summary.
        summary = _leading_prose(markdown)

    return {
        "summary": summary,
        "steps": [
            PlanStep(description=_strip_paths(item), paths=_PATHS.findall(item))
            for item in _items(sections["steps"])
        ],
        "reviewed": sorted(
            {
                path
                for item in _items(sections["reviewed"])
                for path in _PATHS.findall(item)
            }
        ),
        "questions": _items(sections["questions"]),
    }


def _heading(line: str) -> str | None:
    if not line.startswith("#"):
        return None
    name = line.lstrip("#").strip().casefold().rstrip(":")
    for key, target in _SECTIONS.items():
        if key in name:
            return target
    # An unrecognised heading ends the previous section rather than extending
    # it, so stray prose does not land in the summary.
    return None


def _items(lines: list[str]) -> list[str]:
    """List items in a section, with their markers removed."""
    found: list[str] = []
    for line in lines:
        match = _ITEM.match(line)
        if match and match.group(1).strip():
            found.append(match.group(1).strip())
    return found


def _strip_paths(item: str) -> str:
    """A step's description without its trailing path list."""
    text = re.sub(r"\s*[—–-]\s*(?:`[^`]+`[,\s]*)+$", "", item).strip()
    return text or item.strip()


def _leading_prose(markdown: str) -> str:
    """The first paragraph, for a plan with no Summary heading."""
    collected: list[str] = []
    for raw in markdown.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            if collected:
                break
            continue
        if not line:
            if collected:
                break
            continue
        collected.append(line)
    return " ".join(collected).strip()
