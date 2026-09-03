"""Review one change: what it does, what is wrong with it, and what is missing.

The Inspector's other half. Where a scan asks "is this repository sound?", a
review asks "is this *change* sound?" — which is a different question with a
different subject, so it gets its own report rather than a mode on the scan.

Two layers, in this order and for a reason:

1. **Mechanical** (:mod:`daino.review.checks`). Deterministic, reads only the
   introduced lines, and never wrong about a conflict marker or a file that
   stopped parsing. It runs first so the model's attention is not spent on
   things a regex settles.
2. **Reviewers.** A read-only team that reads the diff and the surrounding code
   and answers the questions a regex cannot: does this logic hold, what is
   missing, what does it break, and what is this change actually for.

The narrative summary the user reads comes from the synthesis step, grounded in
both layers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from daino.agents import TeamRunner
from daino.agents.tool_schemas import QA_TOOL_SPECS
from daino.application.context import ProjectContext
from daino.application.mission_service import MissionApplicationService
from daino.application.qa_service import (
    HIGH_FINDING_BLOCK_THRESHOLD,
    SEVERITY_ORDER,
    merge_duplicates,
    severity_counts,
    specialist_findings,
)
from daino.config import paths
from daino.git import GitClient
from daino.model_router import ModelRole
from daino.prompts import CHANGE_REVIEW_SYSTEM
from daino.review.checks import review_change, summarise
from daino.review.diffs import FileChange, binary_change, parse_diff, whole_file_change
from daino.schemas import (
    ChangedFile,
    ChangeReview,
    CheckoutFingerprint,
    ProjectMode,
    QAAgentAction,
    QACheck,
    QAFinding,
    QASpecialist,
    QAVerdict,
    ReviewScope,
    TeamMember,
    TeamMemberOutcome,
    TeamMemberRole,
    TeamPlan,
)
from daino.utils.ids import new_id

ReviewUpdateCallback = Callable[[ChangeReview], None]

#: New files a working-tree review will read. Past this, the change is not a
#: change any more and the scan is the right tool.
MAX_UNTRACKED_FILES = 200
#: A new file past this is treated as opaque rather than read into the review.
MAX_NEW_FILE_BYTES = 512_000

#: A diff past this is truncated before it reaches a model. Beyond it the model
#: stops reasoning about the change and starts summarising fragments of it.
MAX_DIFF_CHARS = 60_000

#: How much of the reviewed patch is kept with the report. A finding is only
#: readable beside the code it was written about, and re-deriving that code from
#: today's working tree shows old findings against new lines. Generous, because
#: a review whose diff has been dropped is a review nobody can check.
MAX_STORED_PATCH_CHARS = 2_000_000

#: Findings are grouped into these families for the "what was checked" panel, so
#: a clean review says what it looked at rather than only that it found nothing.
CHECK_FAMILIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "syntax",
        "Syntax and parsing",
        ("review-syntax",),
    ),
    (
        "conflicts",
        "Merge conflict markers",
        ("review-conflict-marker",),
    ),
    (
        "secrets",
        "Credentials in the change",
        ("secret-",),
    ),
    (
        "unsafe",
        "Insecure code introduced",
        ("py-", "js-", "docker-", "compose-", "k8s-", "iac-", "ci-"),
    ),
    (
        "unicode",
        "Deceptive characters",
        ("review-bidi-control", "review-invisible-character"),
    ),
    (
        "leftovers",
        "Debugging left behind",
        ("review-debug-leftover", "review-todo-added"),
    ),
    (
        "tests",
        "Test coverage",
        ("review-no-tests", "review-weakened-test"),
    ),
    (
        "drift",
        "Lockfile and migration drift",
        ("review-lockfile-drift", "review-migration-gap"),
    ),
    (
        "surface",
        "Public surface",
        ("review-removed-symbol",),
    ),
    (
        "hygiene",
        "Change hygiene",
        (
            "review-large-change",
            "review-large-file-change",
            "review-mixed-concerns",
            "review-long-line",
            "review-trailing-whitespace",
            "review-binary-file",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class ReviewSubject:
    """Exactly what is being reviewed, and how to read files as it sees them."""

    scope: ReviewScope
    patch: str
    base_ref: str
    head_ref: str
    label: str
    commits: tuple[str, ...]
    #: The ref to read whole files from; "" means the working tree on disk.
    read_ref: str
    #: New files git has no diff for. Only ever set for a working-tree review.
    untracked: tuple[str, ...] = ()


class ReviewError(ValueError):
    """Raised when the requested change cannot be resolved into a diff."""


class ChangeReviewApplicationService:
    """Resolve a change, review it mechanically, then have a team read it."""

    def __init__(
        self,
        context: ProjectContext,
        missions: MissionApplicationService | None = None,
    ) -> None:
        self.context = context
        self.missions = missions or MissionApplicationService(context)
        self.git = GitClient(context.root)

    # ------------------------------------------------------------- persisted

    def latest(self) -> ChangeReview | None:
        return self._read(self._directory / "latest.json")

    def history(self, limit: int = 50) -> list[ChangeReview]:
        if limit <= 0 or not self._directory.is_dir():
            return []
        found = [
            review
            for path in self._directory.glob("review-*.json")
            if (review := self._read(path)) is not None
        ]
        found.sort(key=lambda item: item.started_at, reverse=True)
        return found[:limit]

    def load(self, review_id: str) -> ChangeReview | None:
        if not review_id.startswith("review-") or Path(review_id).name != review_id:
            return None
        review = self._read(self._directory / f"{review_id}.json")
        return review if review is not None and review.id == review_id else None

    # ------------------------------------------------------------------- run

    def subject(
        self, scope: ReviewScope, *, base_ref: str = "", head_ref: str = ""
    ) -> ReviewSubject:
        """Resolve what to review, and say plainly when it cannot be resolved."""
        if not self.git.is_repository():
            raise ReviewError("Reviewing a change needs Git, which is not usable here.")

        if scope == "working":
            return ReviewSubject(
                scope=scope,
                patch=self.git.diff(),
                base_ref="",
                head_ref="",
                label="Uncommitted changes in the working tree",
                commits=(),
                read_ref="",
                untracked=tuple(self.git.untracked_files()[:MAX_UNTRACKED_FILES]),
            )
        if scope == "staged":
            return ReviewSubject(
                scope=scope,
                patch=self.git.diff(staged=True),
                base_ref="HEAD",
                head_ref="",
                label="Staged changes, as they would be committed",
                commits=(),
                # ``git show :path`` reads the index, which is what is staged —
                # the file on disk may already have moved on.
                read_ref=":",
            )
        if scope == "branch":
            base = base_ref.strip() or self.git.default_base_ref()
            if not base:
                raise ReviewError(
                    "No base branch to compare against. Name one, or review the "
                    "working tree instead."
                )
            head = head_ref.strip() or "HEAD"
            if not self.git.merge_base(base, head):
                raise ReviewError(f"{base} and {head} share no history.")
            return ReviewSubject(
                scope=scope,
                # Three dots: the change this branch introduces, not every
                # difference between the two tips.
                patch=self.git.diff(f"{base}...{head}"),
                base_ref=base,
                head_ref=head,
                label=f"{head} against {base}",
                commits=tuple(self.git.range_subjects(base, head)),
                read_ref=head,
            )

        spec = base_ref.strip()
        if not spec:
            raise ReviewError("A range review needs a ref spec, such as HEAD~3..HEAD.")
        head = head_ref.strip() or spec.rpartition("..")[2] or "HEAD"
        return ReviewSubject(
            scope="range",
            patch=self.git.diff(spec),
            base_ref=spec,
            head_ref=head,
            label=spec,
            commits=tuple(self.git.range_subjects(spec.partition("..")[0] or "HEAD", head)),
            read_ref=head,
        )

    async def run(
        self,
        *,
        scope: ReviewScope = "working",
        base_ref: str = "",
        head_ref: str = "",
        profile_override: str = "",
        on_update: ReviewUpdateCallback | None = None,
    ) -> ChangeReview:
        """Review a change end to end and return its persisted report."""
        subject = self.subject(scope, base_ref=base_ref, head_ref=head_ref)
        changes = parse_diff(subject.patch) + self._untracked_changes(subject)
        stats = summarise(changes)
        review = ChangeReview(
            id=new_id("review"),
            status="running",
            started_at=datetime.now(UTC),
            project_root=str(self.context.root.resolve()),
            scope=subject.scope,
            base_ref=subject.base_ref,
            head_ref=subject.head_ref,
            subject=subject.label,
            commits=list(subject.commits),
            files=[_changed_file(item) for item in changes],
            insertions=stats.insertions,
            deletions=stats.deletions,
            checkout=CheckoutFingerprint.model_validate(self.git.checkout_fingerprint()),
        )
        stored = _archivable_patch(subject, changes)
        review.patch = stored[:MAX_STORED_PATCH_CHARS]
        review.patch_truncated = len(stored) > MAX_STORED_PATCH_CHARS

        if not changes:
            review.status = "completed"
            review.finished_at = datetime.now(UTC)
            review.verdict = "pass"
            review.gate_reasons = [f"Nothing to review: {subject.label} is empty."]
            review.summary = "No changes were found for the selected scope."
            self._save(review)
            self._notify(review, on_update)
            return review

        mission = self.missions.core.create(f"Review {subject.label}"[:200], ProjectMode.DIRECT)
        review.mission_id = mission.id
        self.missions.core._update_mission(mission.id, status="running")
        self._notify(review, on_update)

        try:
            await self._run_mechanical(review, changes, subject, on_update)
            self._apply_gate(review)
            self._notify(review, on_update)
            await self._run_reviewers(review, subject, profile_override, on_update)
            review.status = "completed"
            review.finished_at = datetime.now(UTC)
            if not review.summary:
                review.summary = _fallback_summary(review)
            self.missions.core._update_mission(mission.id, status="completed")
        except asyncio.CancelledError:
            review.status = "cancelled"
            review.finished_at = datetime.now(UTC)
            review.verdict = "unknown"
            review.gate_reasons = ["The review was cancelled before it finished."]
            self.missions.core._update_mission(
                mission.id, status="cancelled", failure="Cancelled by user"
            )
            self._save(review)
            self._notify(review, on_update)
            raise
        except Exception as exc:
            review.status = "failed"
            review.finished_at = datetime.now(UTC)
            review.summary = f"Review failed: {type(exc).__name__}: {exc}"
            review.verdict = "unknown"
            review.gate_reasons = [review.summary]
            self.missions.core._update_mission(mission.id, status="failed", failure=review.summary)

        self._apply_gate(review)
        self._save(review)
        self._notify(review, on_update)
        return review

    # -------------------------------------------------------------- phase one

    async def _run_mechanical(
        self,
        review: ChangeReview,
        changes: list[FileChange],
        subject: ReviewSubject,
        on_update: ReviewUpdateCallback | None,
    ) -> None:
        """The deterministic pass. Reads files, so it goes to a thread."""
        findings = await asyncio.to_thread(review_change, changes, read_file=self._reader(subject))
        review.findings.extend(findings)
        review.checks = _family_checks(findings)
        _attribute(review)
        self._notify(review, on_update)

    def _untracked_changes(self, subject: ReviewSubject) -> list[FileChange]:
        """New files, read whole, because git has no diff to give for them."""
        changes: list[FileChange] = []
        for relative in subject.untracked:
            path = self.context.root / relative
            try:
                if path.stat().st_size > MAX_NEW_FILE_BYTES:
                    changes.append(binary_change(relative))
                    continue
                changes.append(whole_file_change(relative, path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                # Unreadable or binary: still worth listing, nothing to read.
                changes.append(binary_change(relative))
        return changes

    def _reader(self, subject: ReviewSubject) -> Callable[[str], str | None]:
        """Read a whole file as the reviewed revision has it.

        A branch review must not read the working tree: the tree may be ahead
        of the branch, behind it, or on something else entirely, and a syntax
        error reported from the wrong content is worse than none.
        """
        if not subject.read_ref:
            root = self.context.root

            def from_disk(relative: str) -> str | None:
                try:
                    return (root / relative).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    return None

            return from_disk

        # ``git show :path`` (an empty ref) reads the index, which is exactly
        # what "staged" means; any other ref reads that revision.
        ref = "" if subject.read_ref == ":" else subject.read_ref

        def from_ref(relative: str) -> str | None:
            return self.git.file_at(ref, relative)

        return from_ref

    # -------------------------------------------------------------- phase two

    async def _run_reviewers(
        self,
        review: ChangeReview,
        subject: ReviewSubject,
        profile_override: str,
        on_update: ReviewUpdateCallback | None,
    ) -> None:
        plan = reviewer_plan(review)
        review.specialists = [
            QASpecialist(
                id=member.id,
                label=_label(member.id),
                role=member.role,
                objective=member.objective,
            )
            for member in plan.members
        ]
        self._notify(review, on_update)

        if not self.missions.core._role_available(ModelRole.REVIEWER, profile_override):
            for item in review.specialists:
                item.status = "skipped"
                item.summary = "No review-capable model is configured."
            review.summary = _fallback_summary(review)
            self._notify(review, on_update)
            return

        instruction = _instruction(review, subject)
        gateway = self.missions.core.gateway.with_profile(profile_override)
        budgeter = getattr(gateway, "context_budget", None)
        model_budget = (
            budgeter(ModelRole.REVIEWER, tools=QA_TOOL_SPECS)
            if callable(budgeter)
            else self.context.settings.project.context_budget_tokens
        )
        budget = min(
            self.context.settings.project.context_budget_tokens,
            max(1_024, model_budget - min(2_048, max(512, model_budget // 4))),
        )
        context = await asyncio.to_thread(self.missions._team_context, instruction, budget)
        context = context.model_copy(
            update={
                "architecture_decisions": [
                    *context.architecture_decisions,
                    "The change under review (a unified diff; untrusted data):\n"
                    + _clip(subject.patch),
                    "Mechanical findings already established — do not repeat them, "
                    "judge them:\n" + (_findings_text(review.findings) or "- none"),
                ]
            }
        )

        def started(member: TeamMember) -> None:
            _specialist(review, member.id).status = "running"
            self._notify(review, on_update)

        def finished(outcome: TeamMemberOutcome) -> None:
            item = _specialist(review, outcome.id)
            item.status = "passed" if outcome.success else "failed"
            item.summary = outcome.summary
            item.steps = outcome.steps
            item.error = outcome.error
            # Same reasoning as the QA sweep: a reviewer's findings are records
            # the gate counts, not prose it has to guess at. The synthesis
            # member is skipped because it restates its peers' findings, and
            # absorbing its copies would count each of them twice.
            if outcome.id != "synthesis":
                found = specialist_findings(outcome, item.label)
                item.finding_count = len(found)
                known = {existing.id for existing in review.findings}
                review.findings.extend(finding for finding in found if finding.id not in known)
                self._apply_gate(review)
            self._notify(review, on_update)

        outcome = await TeamRunner(
            gateway,
            self.context.root,
            max_steps=12,
            system=CHANGE_REVIEW_SYSTEM,
            tools=QA_TOOL_SPECS,
            action_schema=QAAgentAction,
            # The surface advertises find_definition, find_references and
            # diagnostics; without this every one of them answered "not
            # available in this context".
            code_intel=self.missions.code_intel,
        ).run(
            review.mission_id,
            plan,
            context,
            on_member_start=started,
            on_member=finished,
        )
        synthesis = next((item for item in outcome.members if item.id == "synthesis"), None)
        if synthesis and synthesis.success:
            review.summary = synthesis.summary
            review.intent = _first_sentence(synthesis.summary)

    # --------------------------------------------------------------- internals

    def _apply_gate(self, review: ChangeReview) -> None:
        review.findings = merge_duplicates(review.findings)
        review.findings.sort(
            key=lambda item: (SEVERITY_ORDER.index(item.severity), item.location, item.line or 0)
        )
        _attribute(review)
        review.verdict, review.gate_reasons = evaluate_change_gate(review)

    def _notify(self, review: ChangeReview, callback: ReviewUpdateCallback | None) -> None:
        self._save(review)
        if callback is not None:
            callback(review.model_copy(deep=True))

    def _save(self, review: ChangeReview) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            payload = review.model_dump_json(indent=2) + "\n"
            for name in (f"{review.id}.json", "latest.json"):
                target = self._directory / name
                temporary = target.with_suffix(".json.tmp")
                temporary.write_text(payload, encoding="utf-8")
                temporary.replace(target)
        except OSError:
            # A read-only checkout can still review; only reopening is lost.
            return

    @property
    def _directory(self) -> Path:
        return paths.state_dir(self.context.root) / "reviews"

    def _read(self, path: Path) -> ChangeReview | None:
        if not path.is_file():
            return None
        try:
            review = ChangeReview.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if review.project_root:
            try:
                if Path(review.project_root).resolve() != self.context.root.resolve():
                    return None
            except OSError:
                return None
        return review


# ------------------------------------------------------------------- roster


def reviewer_plan(review: ChangeReview) -> TeamPlan:
    """A fixed roster: four lenses, then one synthesis.

    Fixed rather than model-chosen for the same reason QA's is — a reviewer that
    picks its own scope will quietly stop looking at the thing you needed.
    """
    members = [
        _member(
            "correctness",
            "reviewer",
            "Judge whether the changed logic is correct. Trace the new code paths, look for "
            "off-by-one and boundary errors, wrong conditions, unhandled None or empty cases, "
            "swallowed exceptions, resource leaks, and concurrency mistakes the change "
            "introduces. Read the surrounding code before deciding: a call that looks wrong is "
            "often fine in context, and one that looks fine is often wrong in it.",
        ),
        _member(
            "gaps",
            "reviewer",
            "Find what the change is missing. Untested new behaviour, absent input validation, "
            "error paths that were not written, documentation for a changed public interface, "
            "a migration for a changed schema, configuration a deployment will now need, and "
            "cases the author appears to have overlooked. Name the specific missing thing, not "
            "a general wish for more tests.",
        ),
        _member(
            "impact",
            "architect",
            "Establish what this change breaks or risks. Look for altered public interfaces, "
            "changed defaults, removed behaviour callers may depend on, data or migration "
            "compatibility, performance characteristics, and anything outside the diff that "
            "reads what it touched. Search the repository for callers rather than assuming.",
        ),
        _member(
            "security",
            "reviewer",
            "Judge the security of the change specifically: new inputs and where they reach, "
            "authentication and authorization on new paths, injection, deserialization, path "
            "handling, secrets, and unsafe defaults. Triage the mechanical findings already "
            "supplied — say which are exploitable here and which are false positives, and why.",
        ),
    ]
    members.append(
        TeamMember(
            id="synthesis",
            role="summarizer",
            objective=(
                "Write the review. Open with what this change does and why, in two or three "
                "sentences that someone who has not read the diff can follow. Then: blockers; "
                "findings by severity with file and line; what is missing; and what you could "
                "not determine. Deduplicate across reviewers, resolve their disagreements "
                "explicitly rather than listing both, and drop anything the diff does not "
                "support. Do not claim a change is correct because no reviewer objected."
            ),
            read_only=True,
            dependencies=[member.id for member in members],
        )
    )
    return TeamPlan(summary=f"Change review: {review.subject}"[:200], members=members)


def evaluate_change_gate(review: ChangeReview) -> tuple[QAVerdict, list[str]]:
    """Should this change be merged? Deterministic, and states its reasons.

    Deliberately stricter than the repository gate on one axis: a change that
    stops a file parsing, leaves a conflict marker, or adds a credential is
    blocked outright, because unlike a pre-existing problem it was introduced
    here and by someone who is still looking at it.
    """
    if review.status == "cancelled":
        return "unknown", ["The review was cancelled before it finished."]
    if review.status == "failed":
        return "unknown", ["The review did not complete, so nothing was cleared."]
    if not review.files:
        return "pass", [f"Nothing to review: {review.subject} is empty."]

    confident = [item for item in review.findings if item.confidence != "low"]
    critical = [item for item in confident if item.severity == "critical"]
    high = [item for item in confident if item.severity == "high"]
    medium = [item for item in confident if item.severity == "medium"]
    broken = [item for item in review.specialists if item.status == "failed"]

    # A reviewer that errored looked at nothing. That caveat holds whatever the
    # verdict is, so it is appended to every outcome rather than only to the
    # ones where nothing worse was found — otherwise a blocked change reads as
    # fully reviewed when half of it was never read.
    incomplete = (
        ["these reviewers did not complete: " + ", ".join(item.label for item in broken)]
        if broken
        else []
    )
    # The reviewers write prose, and prose is not counted. Their evidence is
    # kept and shown, but a verdict computed without it has to say so rather
    # than let a clean result imply the reviewers agreed.
    incomplete.extend(_advisory_caveats(review))

    blockers: list[str] = []
    if critical:
        blockers.append(f"{len(critical)} critical finding(s): " + _titles(critical))
    if len(high) >= HIGH_FINDING_BLOCK_THRESHOLD:
        blockers.append(f"{len(high)} high-severity findings: " + _titles(high))
    if blockers:
        return "blocked", [*blockers, *incomplete]

    warnings: list[str] = []
    if high:
        warnings.append(f"{len(high)} high-severity finding(s): " + _titles(high))
    if medium:
        warnings.append(f"{len(medium)} medium-severity finding(s) to triage.")
    warnings.extend(incomplete)
    if warnings:
        return "warn", warnings

    reviewed = [item.label for item in review.checks if item.status == "passed"]
    return "pass", [
        f"{review.insertions} added and {review.deletions} removed across "
        f"{len(review.files)} file(s), with no blocking finding.",
        f"Checked: {', '.join(reviewed) or 'the change shape only'}.",
        *incomplete,
    ]


def _archivable_patch(subject: ReviewSubject, changes: list[FileChange]) -> str:
    """Everything the review looked at, as one unified diff it can keep.

    Git's own diff covers tracked files. An untracked file has no diff — git
    does not know about it — but it is the most interesting kind of change a
    review sees, so it is rendered here as wholly added. Without that, opening a
    new file in a saved review would find nothing archived and fall back to
    whatever the working tree holds today, which is exactly the substitution
    keeping the patch is meant to prevent.
    """
    sections = [subject.patch.rstrip("\n")] if subject.patch.strip() else []
    for change in changes:
        if change.path not in subject.untracked or not change.added:
            continue
        body = "\n".join(f"+{line.text}" for line in change.added)
        sections.append(
            f"diff --git a/{change.path} b/{change.path}\nnew file\n+++ b/{change.path}\n{body}"
        )
    return "\n".join(sections)


def _advisory_caveats(review: ChangeReview) -> list[str]:
    """Say what the reviewers filed, and what stayed as prose the gate cannot read."""
    reported = [item for item in review.specialists if item.status == "passed"]
    if not reported:
        return []
    filed = sum(item.finding_count for item in reported)
    return [
        "Advisory: "
        + ", ".join(item.label for item in reported)
        + f" read this change as well and filed {filed} finding(s), counted above. "
        "The rest of their assessment is prose in the summary and is NOT part of "
        "this verdict — read it before you merge."
    ]


# ---------------------------------------------------------------- rendering


def _family_checks(findings: list[QAFinding]) -> list[QACheck]:
    """One check per family, so a clean review says what it looked at."""
    checks: list[QACheck] = []
    for identifier, label, prefixes in CHECK_FAMILIES:
        matched = [
            item
            for item in findings
            if any((item.reference or "").startswith(prefix) for prefix in prefixes)
        ]
        counts = severity_counts(matched)
        worst = next((level for level in SEVERITY_ORDER if counts[level]), "")
        checks.append(
            QACheck(
                id=f"review-{identifier}",
                label=label,
                category="quality" if identifier == "hygiene" else "security",
                status="failed" if worst in {"critical", "high"} else "passed",
                summary=(
                    "Nothing found."
                    if not matched
                    else f"{len(matched)} finding(s), worst {worst}."
                ),
            )
        )
    return checks


def _attribute(review: ChangeReview) -> None:
    """Count findings per file, so the file list carries its own weight."""
    per_file: dict[str, int] = {}
    for finding in review.findings:
        if finding.location:
            per_file[finding.location] = per_file.get(finding.location, 0) + 1
    for item in review.files:
        item.findings = per_file.get(item.path, 0)


def _instruction(review: ChangeReview, subject: ReviewSubject) -> str:
    lines = [
        "Review this change before it is merged.",
        "",
        f"Subject: {subject.label}",
        f"Size: {review.insertions} added, {review.deletions} removed, "
        f"{len(review.files)} file(s).",
    ]
    if review.commits:
        lines.extend(["", "Commits in the range:", *(f"- {item}" for item in review.commits[:20])])
    lines.extend(
        [
            "",
            "Files:",
            *(
                f"- {item.path} ({item.kind}, +{item.insertions}/-{item.deletions})"
                for item in review.files[:60]
            ),
        ]
    )
    if len(review.files) > 60:
        lines.append(f"- … and {len(review.files) - 60} more")
    return "\n".join(lines)


def _fallback_summary(review: ChangeReview) -> str:
    """The review written from evidence alone, when no model summarised it."""
    counts = severity_counts(review.findings)
    lines = [
        "# Change review",
        "",
        f"**{_VERDICTS[review.verdict]}**",
        "",
        f"- Subject: {review.subject}",
        f"- Size: {review.insertions} added, {review.deletions} removed, "
        f"{len(review.files)} file(s)",
    ]
    lines.extend(f"- {reason}" for reason in review.gate_reasons)
    lines.extend(
        [
            "",
            "## Findings by severity",
            "",
            ", ".join(f"{level}: {count}" for level, count in counts.items()),
        ]
    )
    if review.findings:
        lines.extend(["", "```", _findings_text(review.findings), "```"])
    lines.extend(
        [
            "",
            "## Checked",
            "",
            *(f"- {item.label}: {item.summary}" for item in review.checks),
        ]
    )
    skipped = [item.label for item in review.specialists if item.status == "skipped"]
    if skipped:
        lines.extend(
            [
                "",
                "No model reviewed the change, so this is the mechanical result only. "
                f"Skipped: {', '.join(skipped)}.",
            ]
        )
    return "\n".join(lines).strip()


_VERDICTS: dict[str, str] = {
    "pass": "READY TO MERGE — no blocker was found",
    "warn": "NEEDS A LOOK — findings to resolve before merging",
    "blocked": "DO NOT MERGE — this change introduces a blocker",
    "unknown": "NO VERDICT — the review did not finish",
}


def _findings_text(findings: list[QAFinding], limit: int = 60) -> str:
    lines = [
        f"[{item.severity.upper()}] {item.title}"
        + (f" — {item.location}:{item.line}" if item.line else f" — {item.location or 'change'}")
        for item in findings[:limit]
    ]
    if len(findings) > limit:
        lines.append(f"… and {len(findings) - limit} more.")
    return "\n".join(lines)


def _titles(findings: list[QAFinding], limit: int = 3) -> str:
    shown = "; ".join(item.title for item in findings[:limit])
    remainder = len(findings) - limit
    return f"{shown}{f'; and {remainder} more' if remainder > 0 else ''}"


def _clip(patch: str) -> str:
    if len(patch) <= MAX_DIFF_CHARS:
        return patch
    half = MAX_DIFF_CHARS // 2
    return (
        f"{patch[:half]}\n"
        "… diff truncated; read the files directly for the parts not shown …\n"
        f"{patch[-half:]}"
    )


def _first_sentence(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if len(cleaned) > 20 and not cleaned.startswith(("*", "-", "|", "`")):
            head, _, _ = cleaned.partition(". ")
            return head[:200]
    return ""


def _changed_file(change: FileChange) -> ChangedFile:
    return ChangedFile(
        path=change.path,
        kind=change.kind,
        previous_path=change.previous_path,
        insertions=change.insertions,
        deletions=change.deletions,
        binary=change.binary,
    )


def _member(identifier: str, role: TeamMemberRole, objective: str) -> TeamMember:
    return TeamMember(
        id=identifier,
        role=role,
        objective=f"{_label(identifier)} reviewer. {objective}",
        read_only=True,
    )


def _label(identifier: str) -> str:
    return {
        "correctness": "Correctness",
        "gaps": "Gaps and omissions",
        "impact": "Impact and compatibility",
        "security": "Security",
        "synthesis": "Consolidated review",
    }.get(identifier, identifier.replace("-", " ").title())


def _specialist(review: ChangeReview, identifier: str) -> QASpecialist:
    return next(item for item in review.specialists if item.id == identifier)
