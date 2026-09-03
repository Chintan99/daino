"""Validated contracts shared by providers, agents, tools, and persistence."""

from __future__ import annotations

import shlex
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_COMMAND_EXECUTABLES = frozenset(
    {
        "bandit",
        "cargo",
        "eslint",
        "git",
        "go",
        "make",
        "mypy",
        "node",
        "npm",
        "npx",
        "pnpm",
        "poetry",
        "pyright",
        "pytest",
        "python",
        "python3",
        "ruff",
        "tox",
        "uv",
        "yarn",
    }
)


def _normalize_commands(commands: list[str]) -> list[str]:
    """Repair the common model mistake of returning one argv as many commands."""
    values = [item.strip() for item in commands if item.strip()]
    if len(values) < 2 or any(" " in item for item in values[:2]):
        return values
    first = values[0].rsplit("/", 1)[-1]
    second = values[1].rsplit("/", 1)[-1]
    looks_like_argv = values[1].startswith("-") or (
        first in _COMMAND_EXECUTABLES and second not in _COMMAND_EXECUTABLES
    )
    return [shlex.join(values)] if looks_like_argv else values


class StrictModel(BaseModel):
    """Base contract that rejects unknown model output fields."""

    model_config = ConfigDict(extra="forbid")


class ToolCall(StrictModel):
    """One native function call requested by a model."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ImagePart(StrictModel):
    """One image travelling with a message.

    Base64 rather than a path, because by the time a message reaches a provider
    the file may be gone and the provider certainly cannot read the disk. The
    media type is carried separately rather than sniffed at the wire, since the
    only party that knows it for certain is whoever read the bytes.
    """

    media_type: str
    #: Standard base64, without the ``data:`` prefix.
    data: str
    #: What this image is, for the transcript and for a model that cannot see it.
    description: str = ""

    @property
    def data_url(self) -> str:
        return f"data:{self.media_type};base64,{self.data}"


class Message(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    #: Native tool calls attached to an assistant message.
    tool_calls: list[ToolCall] = Field(default_factory=list)
    #: Identifies which tool call a ``tool`` message answers.
    tool_call_id: str = ""
    #: Images the model should see alongside ``content``. Only ever attached to
    #: a ``user`` message: the chat-completions wire format accepts image parts
    #: there and not on a ``tool`` result, so an observation that produced an
    #: image is followed by a user message carrying it rather than trying to
    #: smuggle it into the tool reply.
    images: list[ImagePart] = Field(default_factory=list)


class LLMResponse(StrictModel):
    content: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: Part of ``input_tokens`` the provider served from its prompt cache.
    cached_tokens: int = 0
    latency_ms: float = 0
    finish_reason: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class ProjectMode(StrEnum):
    DIRECT = "direct"
    SPECIFICATION = "specification"
    PROGRAM = "program"


class InteractionMode(StrEnum):
    """How much autonomy the interactive coding agent has in this session."""

    PLAN = "plan"
    ASK = "ask"
    SESSION = "session"
    FULL = "full"


class MissionStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CHANGE_APPROVAL = "awaiting_change_approval"
    RUNNING = "running"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    BLOCKED = "blocked"
    AWAITING_APPROVAL = "awaiting_approval"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RequirementSpec(StrictModel):
    problem_statement: str
    goals: list[str]
    non_goals: list[str] = Field(default_factory=list)
    functional_requirements: list[str]
    non_functional_requirements: list[str] = Field(default_factory=list)
    user_roles: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    api_contracts: list[str] = Field(default_factory=list)
    data_contracts: list[str] = Field(default_factory=list)
    security_constraints: list[str] = Field(default_factory=list)
    deployment_impact: str = "none"
    acceptance_criteria: list[str]
    test_strategy: list[str]
    assumptions: list[str] = Field(default_factory=list)
    open_decisions: list[str] = Field(default_factory=list)


class TaskSpec(StrictModel):
    id: str
    title: str
    objective: str
    dependencies: list[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    expected_files: list[str] = Field(default_factory=list)
    allowed_files: list[str] = Field(default_factory=list)
    relevant_symbols: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str]
    verification_commands: list[str]
    rollback_notes: str = ""
    status: TaskStatus = TaskStatus.PENDING
    assigned_model: str | None = None
    attempt_count: int = 0
    evidence: list[str] = Field(default_factory=list)
    #: The id of the original task this one was cut out of, when a task proved
    #: too large for the executing model and was split. Holds the *root* id, not
    #: the immediate parent, so the cap on repeated splitting is a dict lookup
    #: rather than id parsing. Empty for every planned task; the planner is a
    #: `StrictModel` and so is able to emit this field, which is why the mission
    #: service forces it back to "" when it normalises a plan.
    slice_of: str = ""

    @field_validator("verification_commands")
    @classmethod
    def normalize_verification_commands(cls, value: list[str]) -> list[str]:
        return _normalize_commands(value)


class TaskPlan(StrictModel):
    summary: str
    mode: ProjectMode
    tasks: list[TaskSpec]


class FileModification(StrictModel):
    path: str
    action: Literal["create", "patch", "replace", "delete"]
    unified_diff: str | None = None
    content: str | None = None
    #: For ``replace``: the exact text to find. Must occur once unless
    #: ``replace_all`` is set. Preferred over ``unified_diff`` — an anchor string
    #: either matches or it does not, with no line numbers or context to drift.
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False
    reason: str


class Implementation(StrictModel):
    summary: str
    modifications: list[FileModification]
    verification_commands: list[str] = Field(default_factory=list)


class EditSpec(StrictModel):
    """One exact-span replacement inside a ``multi_edit``."""

    old_string: str
    new_string: str
    replace_all: bool = False


class TodoItem(StrictModel):
    """One step of the agent's plan for the current request."""

    content: str
    status: Literal["pending", "in_progress", "completed", "failed"] = "pending"


#: What an inspection is asked to cover. "quality" is the historical QA sweep,
#: "security" is the vulnerability assessment on its own, "full" is both.
QAScanProfile = Literal["full", "quality", "security"]

#: Ordered worst-first; the release gate reads them in this order.
QASeverity = Literal["critical", "high", "medium", "low", "info"]

QAFindingCategory = Literal[
    "secrets",
    "vulnerability",
    "dependencies",
    "configuration",
    "runtime",
    "quality",
    "tests",
    "browser",
]


class QAFindingDraft(StrictModel):
    """One issue a review specialist reports, in the shape the gate can read.

    Defined up here, next to :class:`AgentAction`, because a specialist reports
    its findings *through* ``finish`` — and that is the whole point. Reviewers
    used to hand back prose, so everything they found was invisible to the
    release gate and to the file annotations: only the deterministic scanners
    counted, and a specialist that found a critical authorization hole
    contributed a paragraph nobody's tooling could act on.

    Deliberately smaller than :class:`QAFinding`. ``id`` and ``source`` are the
    inspection's to assign — a model inventing either would let one specialist
    overwrite another's finding, or claim to be a scanner.
    """

    title: str
    severity: QASeverity = "medium"
    category: QAFindingCategory = "vulnerability"
    #: Repository-relative path, or a URL for a finding against a live target.
    location: str = ""
    line: int | None = None
    detail: str = ""
    remediation: str = ""
    #: CWE identifier ("CWE-798") when one applies.
    cwe: str = ""
    #: Advisory or rule identifier ("GHSA-…", "CVE-…", "B602").
    reference: str = ""
    #: How sure the specialist is that this is real and reachable here. Low
    #: confidence is a legitimate answer, and far more useful than silence.
    confidence: Literal["high", "medium", "low"] = "medium"


class AgentAction(StrictModel):
    """One step an implementing agent chooses to take.

    Deliberately flat rather than a discriminated union: a single object with an
    action enum is the shape that survives every provider's structured-output
    support, from OpenRouter through local vLLM and Ollama.
    """

    thought: str
    action: Literal[
        "read_file",
        "search_text",
        "list_directory",
        "replace",
        "write",
        "delete",
        "multi_edit",
        "run_command",
        "resolve_command_failure",
        "web_search",
        "fetch_url",
        "glob",
        "grep",
        "todo",
        "memory_search",
        "memory_save",
        "memory_update",
        "memory_forget",
        "memory_list",
        "memory_verify",
        "create_design",
        "read_design",
        "read_design_artifact",
        "update_design",
        "add_design_node",
        "update_design_node",
        "delete_design_node",
        "connect_design_nodes",
        "disconnect_design_nodes",
        "add_design_frame",
        "update_design_frame",
        "delete_design_frame",
        "workspace_read",
        "workspace_plan",
        "workspace_task",
        "workspace_link",
        "workspace_code",
        "workspace_deliverable",
        "call_tool",
        "skill",
        "delegate",
        "find_definition",
        "find_references",
        "diagnostics",
        "read_image",
        "respond",
        "finish",
    ]
    path: str = ""
    query: str = ""
    old_string: str = ""
    new_string: str = ""
    replace_all: bool = False
    content: str = ""
    #: For ``run_command``: the executable and its arguments. Runs without a
    #: shell, so pipes and redirects are not available.
    command: str = ""
    #: For ``resolve_command_failure``: a later successful command that covers
    #: the same concern through an environment-appropriate route.
    evidence_command: str = ""
    timeout: int = 0
    #: For web research. URL is used by fetch_url; result/character limits are
    #: bounded again by the executor and cannot disable its safety limits.
    url: str = ""
    max_results: int = 0
    max_chars: int = 0
    #: For ``glob``: a path pattern such as ``src/**/*.py``.
    pattern: str = ""
    #: For a review specialist's ``finish``: what it found, structured. Empty
    #: for every other agent, and ignored by every caller that does not ask for
    #: it — the field lives on the base action because native tool calls are
    #: validated against this class rather than the caller's narrower schema.
    findings: list[QAFindingDraft] = Field(default_factory=list)
    #: For ``read_file``: read a window of a large file instead of the head.
    offset: int = 0
    limit: int = 0
    #: For ``call_tool``: the namespaced name of an external tool, as
    #: ``mcp__<server>__<tool>``. A model with native tool calling never fills
    #: this in — it calls the tool directly and the loop converts the call —
    #: but a schema-constrained model has only this one action to reach an MCP
    #: server through, so the field has to exist on the flat action.
    tool_name: str = ""
    #: For ``call_tool``: whatever the external tool's own schema asks for.
    #: Deliberately untyped: it belongs to a server Daino has never seen.
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: For ``skill``: which set of project instructions to load into the turn.
    skill_name: str = ""
    #: For ``delegate``: the subagents to run concurrently for this step.
    delegates: list[DelegateSpec] = Field(default_factory=list)
    #: For ``find_definition`` / ``find_references``: the identifier to resolve.
    #: A name rather than a line and column, because deriving LSP's zero-based
    #: coordinates from a file it read as text is arithmetic a model gets wrong
    #: often enough to make the tool a net negative.
    symbol: str = ""
    #: For ``multi_edit``: several replacements applied to one file in order.
    edits: list[EditSpec] = Field(default_factory=list)
    #: For the ``*_design_frame`` actions: which mock-up frame, and what it
    #: holds. ``frame_elements`` replaces the frame's contents wholesale rather
    #: than merging — a frame's children are an ordered tree, and a merge would
    #: make removing an element impossible to express.
    frame_id: str = ""
    frame_name: str = ""
    frame_width: int = 0
    frame_height: int = 0
    frame_elements: list[dict[str, Any]] = Field(default_factory=list)
    #: For ``todo``: the current plan, replaced in full each time.
    todos: list[TodoItem] = Field(default_factory=list)
    summary: str = ""
    #: For ``respond``: the answer to show the user. Kept separate from
    #: ``summary``, which means "what you changed" and is empty when the agent
    #: only answered.
    message: str = ""
    #: Workspace operations. Documents are ordinary files, so the agent writes
    #: them with ``write``/``replace``; these cover only what a file cannot say:
    #: what the workspace holds, and what the plan is.
    workspace_id: str = ""
    #: For ``workspace_plan``: the plan, replaced in full each time.
    plan_steps: list[str] = Field(default_factory=list)
    #: For ``workspace_task``: which step, and what it becomes.
    task_id: str = ""
    task_status: Literal["pending", "in_progress", "completed", "failed"] = "pending"
    #: For ``workspace_link``: what this document was made from, and how.
    source_path: str = ""
    target_path: str = ""
    relation: str = "references"
    #: For ``workspace_code``: what the coding work should achieve, and which
    #: workspace documents it needs. Paths, never pasted content.
    request: str = ""
    context_paths: list[str] = Field(default_factory=list)
    #: For ``workspace_deliverable``: the finished-file format to produce, and
    #: where it should land in the workspace folder.
    format: str = ""
    title: str = ""
    #: Controlled memory operations. The database is never exposed to the LLM.
    memory_id: str = ""
    memory_type: str = "semantic"
    memory_scope: str = "project"
    importance: float = 0.5
    confidence: float = 0.5
    tags: list[str] = Field(default_factory=list)
    source: str = ""
    verification_commands: list[str] = Field(default_factory=list)
    #: Design-workspace operations. These edit structured diagram artifacts under
    #: ``.daino/designs`` through granular mutations rather than rewriting JSON.
    design_id: str = ""
    design_name: str = ""
    design_type: str = "architecture"
    node_id: str = ""
    node_label: str = ""
    node_type: str = "default"
    #: Canvas artifacts (a dropped or authored HTML page, SVG, or note) carry
    #: their own source. Kept separate from the diagram fields so a node can be
    #: either a box in a diagram or a real file the user previews full screen.
    node_kind: str = ""
    node_content: str = ""
    source_node: str = ""
    target_node: str = ""
    edge_id: str = ""
    edge_label: str = ""
    x: float = 0.0
    y: float = 0.0

    @field_validator("verification_commands")
    @classmethod
    def normalize_verification_commands(cls, value: list[str]) -> list[str]:
        return _normalize_commands(value)

    @model_validator(mode="after")
    def validate_action_arguments(self) -> AgentAction:
        """Reject incomplete flat actions before they reach the executor.

        The flat contract is intentionally friendly to local grammar-constrained
        decoders, but its default-valued fields otherwise let a response such as
        ``{"action": "replace"}`` pass schema validation.  Turning that into a
        provider repair is both cheaper and clearer than spending an agent step on
        an edit that could never run.
        """
        required_text: dict[str, tuple[str, ...]] = {
            "read_file": ("path",),
            "search_text": ("query",),
            "replace": ("path", "old_string"),
            "write": ("path",),
            "delete": ("path",),
            "multi_edit": ("path",),
            "run_command": ("command",),
            "resolve_command_failure": ("command", "evidence_command"),
            "web_search": ("query",),
            "fetch_url": ("url",),
            "glob": ("pattern",),
            "grep": ("query",),
            "memory_search": ("query",),
            "memory_save": ("content",),
            "memory_update": ("memory_id",),
            "memory_forget": ("memory_id",),
            "memory_verify": ("memory_id",),
            "create_design": ("design_name",),
            "read_design": ("design_id",),
            "update_design": ("design_id",),
            "add_design_node": ("design_id", "node_label"),
            "update_design_node": ("design_id", "node_id"),
            "delete_design_node": ("design_id", "node_id"),
            "connect_design_nodes": ("design_id", "source_node", "target_node"),
            "disconnect_design_nodes": ("design_id",),
            "workspace_task": ("task_id",),
            "workspace_link": ("source_path", "target_path"),
            "workspace_code": ("request",),
            "workspace_deliverable": ("path", "format"),
            "respond": ("message",),
            "finish": ("summary",),
        }
        missing = [name for name in required_text.get(self.action, ()) if not getattr(self, name)]
        if missing:
            raise ValueError(f"{self.action} requires non-empty {', '.join(missing)}")
        if self.action == "multi_edit" and not self.edits:
            raise ValueError("multi_edit requires at least one edit")
        if self.action == "todo" and not self.todos:
            raise ValueError("todo requires at least one item")
        return self


class QAAgentAction(AgentAction):
    """Read-only structured-output contract for QA-capable local models.

    Kept in step with ``QA_TOOL_SPECS`` by hand rather than derived, because the
    guarantee runs the other way: the narrow list is the contract, and a new
    action appearing in it should be a deliberate edit here rather than
    something a base class quietly inherited.
    """

    action: Literal[
        "read_file",
        "search_text",
        "list_directory",
        "glob",
        "grep",
        "find_definition",
        "find_references",
        "diagnostics",
        "read_image",
        "finish",
    ]


class AgentObservation(StrictModel):
    """The result of an action, fed back to the agent before its next step."""

    action: str
    success: bool
    detail: str


class DiffLine(StrictModel):
    """One rendered line of a file diff."""

    #: " " context, "-" removed, "+" added.
    marker: Literal[" ", "-", "+"]
    #: Line number in the file after the edit, or before it for removed lines.
    number: int
    text: str


class FileDiff(StrictModel):
    """What changed in one file, ready to render without re-reading the disk."""

    path: str
    #: "created", "modified", or "deleted".
    change: Literal["created", "modified", "deleted"]
    added: int = 0
    removed: int = 0
    lines: list[DiffLine] = Field(default_factory=list)
    #: Set when the file has no textual diff to show, such as a binary file.
    note: str = ""


class ChatOutcome(StrictModel):
    """The result of one chat-agent turn, ready to render in the transcript."""

    mission_id: str = ""
    #: Set when the agent answered instead of editing.
    answer: str = ""
    #: Set when the agent edited; what it says it did.
    summary: str = ""
    diffs: list[FileDiff] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    steps: int = 0
    #: None when no verification ran, otherwise whether every check passed.
    verified: bool | None = None
    verification_summary: str = ""


#: Roles a team member may take. Deliberately every routed role except
#: ``deployer``: a sub-agent spawned from a chat instruction must never reach the
#: deployment path, which has its own approval gates. Kept in sync with
#: ``ModelRole`` by ``test_team_member_roles_track_model_roles``.
TeamMemberRole = Literal[
    "architect",
    "planner",
    "builder",
    "reviewer",
    "debugger",
    "tester",
    "summarizer",
    "researcher",
]


class DelegateSpec(StrictModel):
    """One subagent the *model* asked for, mid-turn.

    Deliberately smaller than :class:`TeamMember`. A team lead plans a roster and
    reasons about dependency waves; an agent that has just realised it needs
    three subsystems investigated should not have to invent member ids and a
    dependency graph to say so. Everything else is derived: ids are generated,
    and delegates always run as one concurrent wave, because a model that wanted
    them sequenced would simply not have asked for them at once.
    """

    objective: str
    #: Repository-relative paths or globs this subagent may modify. Required for
    #: a writer, and checked against its siblings before any of them start.
    scope: list[str] = Field(default_factory=list)
    #: Defaults to read-only, which is both the safe answer and the common one:
    #: the reason to delegate is usually to look at several things at once.
    read_only: bool = True


class TeamMember(StrictModel):
    """One sub-agent in a team, with the scope it is allowed to touch."""

    id: str
    role: TeamMemberRole
    objective: str
    #: Repository-relative paths or glob patterns this member may modify. Empty
    #: is only valid for a read-only member; a writer with no scope could touch
    #: the whole repository and could not be checked against its peers.
    scope: list[str] = Field(default_factory=list)
    #: Read-only members are the explorers. They fan out widest, so they are the
    #: ones that must be unable to write.
    read_only: bool = False
    #: Ids of members that must finish before this one starts.
    dependencies: list[str] = Field(default_factory=list)


class TeamPlan(StrictModel):
    """The roster a team lead proposes for one instruction."""

    summary: str
    members: list[TeamMember]


class TeamMemberOutcome(StrictModel):
    """What one member did, reported back to the chat transcript."""

    id: str
    role: str
    objective: str
    summary: str
    changed: list[str] = Field(default_factory=list)
    steps: int = 0
    success: bool = True
    error: str = ""
    #: Structured findings, for a member that reviews rather than builds. The
    #: prose in ``summary`` is what a person reads; this is what a gate counts.
    findings: list[QAFindingDraft] = Field(default_factory=list)


class TeamOutcome(StrictModel):
    plan: TeamPlan
    members: list[TeamMemberOutcome] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    #: The mission the run was recorded under, so a caller can reach its
    #: workspace, diff, and checkpoints afterwards.
    mission_id: str = ""


QARunStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
QACheckStatus = Literal["pending", "running", "passed", "failed", "skipped"]

#: The release gate's answer to "is this safe to push?".
QAVerdict = Literal["unknown", "pass", "warn", "blocked"]


class QAFinding(StrictModel):
    """One issue an inspection can point at, from a scanner or a specialist.

    Findings are what the release gate reasons over. They are deliberately
    flatter than any scanner's native record: everything that decides whether a
    push is blocked has to be comparable across a secret scan, a dependency
    audit, and a live probe.
    """

    id: str
    title: str
    severity: QASeverity = "medium"
    category: QAFindingCategory = "vulnerability"
    #: The check id, scanner, or specialist that produced this finding.
    source: str = ""
    #: Repository-relative path, or a URL for findings against a live target.
    location: str = ""
    line: int | None = None
    detail: str = ""
    remediation: str = ""
    #: CWE identifier ("CWE-798") when the source names one.
    cwe: str = ""
    #: Advisory or rule identifier ("GHSA-…", "CVE-…", "B602").
    reference: str = ""
    confidence: Literal["high", "medium", "low"] = "medium"


class CheckoutFingerprint(StrictModel):
    """Which working tree a report was taken from.

    A verdict is a statement about code, not about a project. Without this a
    "safe to push" badge outlives the checkout that earned it and keeps
    reassuring people about files nobody inspected.
    """

    commit: str = ""
    branch: str = ""
    #: sha256 over the commit, the porcelain status, and the tracked diff.
    digest: str = ""
    dirty: bool = False


class QACheck(StrictModel):
    """One deterministic quality, security, browser, or dependency check."""

    id: str
    label: str
    category: Literal["quality", "tests", "browser", "dependencies", "security", "runtime"]
    command: str = ""
    status: QACheckStatus = "pending"
    summary: str = ""
    output: str = ""
    duration_seconds: float = 0.0
    network_required: bool = False


class QASpecialist(StrictModel):
    """Progress and final evidence from one read-only QA sub-agent."""

    id: str
    label: str
    role: str
    objective: str
    status: QACheckStatus = "pending"
    summary: str = ""
    steps: int = 0
    error: str = ""
    #: How many structured findings this reviewer filed. The findings themselves
    #: live on the report, merged with every other source, because that is where
    #: the gate and the file annotations read them from — but a reviewer that
    #: wrote four pages and filed nothing is worth being able to see at a glance.
    finding_count: int = 0


class QAReport(StrictModel):
    """Persisted result shown in the Inspector workspace."""

    id: str
    status: QARunStatus = "pending"
    started_at: datetime
    finished_at: datetime | None = None
    project_root: str = ""
    project_profile: list[str] = Field(default_factory=list)
    checks: list[QACheck] = Field(default_factory=list)
    specialists: list[QASpecialist] = Field(default_factory=list)
    summary: str = ""
    mission_id: str = ""
    #: Which half of the inspection was asked for.
    scan_profile: QAScanProfile = "full"
    #: The running application the live probe was pointed at, if any.
    target_url: str = ""
    #: Deduplicated findings from scanners, parsed tool output, and specialists.
    findings: list[QAFinding] = Field(default_factory=list)
    #: The release gate's answer, and the specific reasons behind it.
    verdict: QAVerdict = "unknown"
    gate_reasons: list[str] = Field(default_factory=list)
    #: The checkout this verdict is about. A report whose fingerprint no longer
    #: matches the working tree is history, not a clearance.
    checkout: CheckoutFingerprint = Field(default_factory=CheckoutFingerprint)


#: What a change review was pointed at.
#: "working" is the uncommitted tree, "staged" is what is about to be committed,
#: "branch" is this branch against its base — the closest thing to a pull
#: request in a tool that never pushes — and "range" is an explicit ref spec.
ReviewScope = Literal["working", "staged", "branch", "range"]

ChangeKind = Literal["added", "modified", "deleted", "renamed", "binary"]


class ChangedFile(StrictModel):
    """One file's part of a change, without its content."""

    path: str
    kind: ChangeKind = "modified"
    #: Set only for a rename, so a move reads as a move.
    previous_path: str = ""
    insertions: int = 0
    deletions: int = 0
    binary: bool = False
    #: How many findings point at this file, for the file list's own summary.
    findings: int = 0


class ChangeReview(StrictModel):
    """A review of one change: what it does, what is wrong, what is missing."""

    id: str
    status: QARunStatus = "pending"
    started_at: datetime
    finished_at: datetime | None = None
    project_root: str = ""
    scope: ReviewScope = "working"
    #: The refs the diff was taken between; empty for an uncommitted tree.
    base_ref: str = ""
    head_ref: str = ""
    #: Human-readable description of exactly what was compared.
    subject: str = ""
    #: Commit subjects in the range, newest first.
    commits: list[str] = Field(default_factory=list)
    files: list[ChangedFile] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    #: The narrative: what this change does and why, written by the reviewer.
    summary: str = ""
    #: One line of inferred intent, for a list view.
    intent: str = ""
    checks: list[QACheck] = Field(default_factory=list)
    specialists: list[QASpecialist] = Field(default_factory=list)
    findings: list[QAFinding] = Field(default_factory=list)
    verdict: QAVerdict = "unknown"
    gate_reasons: list[str] = Field(default_factory=list)
    mission_id: str = ""
    #: The checkout the diff was taken from, so a stale review reads as stale.
    checkout: CheckoutFingerprint = Field(default_factory=CheckoutFingerprint)
    #: The exact patch that was reviewed, kept so findings are always shown
    #: beside the code they were written about rather than beside whatever the
    #: working tree holds today. Truncated reviews say so in ``patch_truncated``.
    patch: str = ""
    patch_truncated: bool = False


class ReviewFinding(StrictModel):
    severity: Literal["info", "low", "medium", "high", "critical"]
    category: str
    message: str
    file: str | None = None
    line: int | None = None


class ReviewReport(StrictModel):
    approved: bool
    summary: str
    findings: list[ReviewFinding] = Field(default_factory=list)
    missing_tests: list[str] = Field(default_factory=list)


class FailureReport(StrictModel):
    failure_type: str
    command: str
    summary: str
    file: str | None = None
    line: int | None = None
    likely_correction_area: str | None = None
    output_excerpt: str = ""


class CommandResult(StrictModel):
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    redacted: bool = True

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class ToolResult(StrictModel):
    tool: str
    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    duration_seconds: float = 0


class VerificationCheck(StrictModel):
    name: str
    command: str
    passed: bool
    result: CommandResult | None = None
    skipped: bool = False
    skip_reason: str = ""


class VerificationReport(StrictModel):
    passed: bool
    checks: list[VerificationCheck]
    failures: list[FailureReport] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime


class RepositorySymbol(StrictModel):
    name: str
    kind: str
    path: str
    line: int
    signature: str | None = None


class RepositoryFile(StrictModel):
    path: str
    language: str
    size: int
    digest: str
    summary: str
    imports: list[str] = Field(default_factory=list)
    symbols: list[RepositorySymbol] = Field(default_factory=list)


class RepositoryIndex(StrictModel):
    root: str
    generated_at: datetime
    files: list[RepositoryFile]
    languages: dict[str, int]
    frameworks: list[str] = Field(default_factory=list)
    entrypoints: list[str] = Field(default_factory=list)


class TaskPacket(StrictModel):
    """A compact, model-independent handoff for one bounded coding step."""

    objective: str
    acceptance_checks: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    active_decisions: list[str] = Field(default_factory=list)
    relevant_files: list[str] = Field(default_factory=list)
    completed_steps: list[str] = Field(default_factory=list)
    pending_steps: list[str] = Field(default_factory=list)
    current_errors: list[str] = Field(default_factory=list)
    verification_commands: list[str] = Field(default_factory=list)
    next_action: str = ""
    retrieval_hint: str = ""


class ContextBundle(StrictModel):
    task: str
    acceptance_criteria: list[str]
    effective_instructions: str = ""
    working_memory: dict[str, Any] = Field(default_factory=dict)
    compacted_context: dict[str, Any] = Field(default_factory=dict)
    relevant_memories: list[dict[str, Any]] = Field(default_factory=list)
    memory_precedence: str = ""
    architecture_decisions: list[str] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)
    tests: dict[str, str] = Field(default_factory=dict)
    failure_summary: str | None = None
    token_estimate: int = 0
    included_paths: list[str] = Field(default_factory=list)
    task_packet: TaskPacket | None = None
    execution_mode: Literal["standard", "compact"] = "standard"
    retrieval_stage: Literal["initial", "expanded"] = "expanded"
    omitted_context: list[str] = Field(default_factory=list)


class DeploymentRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DeploymentPlan(StrictModel):
    target: str
    detected_environment: dict[str, Any]
    deployment_strategy: str
    required_changes: list[str]
    files_to_upload: list[str]
    images: list[str] = Field(default_factory=list)
    persistent_volumes: list[str] = Field(default_factory=list)
    environment_variables: list[str] = Field(default_factory=list)
    database_migrations: list[str] = Field(default_factory=list)
    port_changes: list[str] = Field(default_factory=list)
    reverse_proxy_changes: list[str] = Field(default_factory=list)
    tls_requirements: list[str] = Field(default_factory=list)
    health_checks: list[str]
    risk_level: DeploymentRisk
    destructive_actions: list[str] = Field(default_factory=list)
    rollback_strategy: list[str]
