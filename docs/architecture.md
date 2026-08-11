# Architecture

## Interactive presentation boundary

The Textual application under `vasuki/tui` depends only on `vasuki/application` facades and typed
events. It never applies patches, chooses provider credentials, creates worktrees, executes test
commands, or deploys services directly.

`ProjectContext` assembles validated settings, the database, and an `EventBus`. Application services
provide mission, repository, verification, provider, settings, checkpoint, and deployment use
cases. The existing `MissionService`, model gateway, repository indexer, runtime, verification
engine, workspace manager, and deployment manager remain the authoritative implementations.

Core lifecycle events are serializable dataclasses in `vasuki/events/events.py`. They are persisted
to `mission_events`, mirrored to the redacted audit log, and consumed live by Textual workers.
This boundary is intentionally reusable by a future web or remote Mission Control client.

Vasuki separates orchestration from every environment-specific boundary:

```text
Typer CLI
  -> MissionService
     -> RequirementsCompiler / Planner / specialist agents
     -> ModelGateway -> ModelRouter -> LLMProvider
     -> RepositoryIndexer -> ContextCompiler
     -> EditTools / VerificationEngine -> Runtime
     -> WorkspaceManager -> controlled Git client
     -> SQLAlchemy persistence / evidence / audit log
DeploymentManager -> LocalRuntime or RemoteSSHRuntime
```

Strict Pydantic contracts are the protocol between model calls, agents, tools, verification, and
deployment. Malformed model output is repaired a bounded number of times and is never executed.
SQLAlchemy records durable state independently of terminal history. This allows a future API,
worker queue, IDE extension, or web UI to call the same services without moving orchestration logic.

Mission task execution is sequential by design. Task dependencies are validated and topologically
ordered, and one builder loop runs at a time inside the mission worktree.

## The chat agent

A bare prompt does not go to a question-answering path. It runs `ToolLoop` — the same loop the
mission builder uses — with the builder's grounded tools plus `respond`, and the model chooses
which the request called for. There is no separate classifier: a request to change something is
carried out rather than described, and a question is answered without touching the repository.

The loop speaks both dialects a backend may offer. Where the provider advertises native tool
calling the action space is sent as OpenAI-format tools; where it does not, or where the backend
rejects the tools parameter once, the same action is expressed as schema-constrained JSON, which
Ollama decodes with `format` and vLLM with `guided_json`. Either way the executor sees one
validated `AgentAction` at a time, so scope checks, observations, and the audit ledger do not vary
by backend.

Command execution is layered so that no single component both decides and acts: `CommandGate`
decides (allow, ask, refuse) from policy plus session memory, `CommandRunner` asks the interface
when the gate says to, and the configured `Runtime` executes. The interface supplies the approval
callback, so a headless caller with no approver gets a refusal rather than silently inheriting
permissions the TUI would have prompted for. Mission builders use the same runner for unattended
safe commands, which lets them inspect tests and builds before the outer verification/repair gate.
For interactive edits, `finish` is refused until every proposed verification command has most
recently succeeded in the same tool loop. A failed agent command also remains an unresolved gate:
the exact command must later pass, or a rejected `a && b` chain must be rerun as two successful
standalone commands. An environment-specific failure can be cleared by an explicitly linked,
already-successful equivalent command (for example a Docker build replacing unavailable host
`npm`); Vasuki rejects the link unless that evidence really passed. This prevents an unrelated
green check from silently hiding earlier red evidence. The independent verifier then repeats the
declared checks; a failure records a failed mission and the UI says the changes are incomplete
instead of rendering the builder's optimistic summary as success.

The model gateway has two independent recovery layers. Request-shape incompatibilities fall back
from native tools or grammar constraints to structured prompt JSON on the same provider. Provider
failures move to the role's configured model fallback. Before either request is sent, repository
context and prior tool exchanges are fitted to the selected profile's actual context window while
preserving the current task and complete recent tool-call groups.

Planner and agent contracts repair a common small-model ambiguity where one command is returned as
an argv-shaped string list. The approved task's verification commands remain authoritative over a
builder's ad-hoc finishing suggestions. After all task checks pass, an independent rejection is
turned into a scoped corrective task and re-reviewed, with two repair attempts at most; the mission
blocks if review still fails.

## Teams of sub-agents

`vasuki/agents/team.py` is the concurrent path, reached from `/team`. A team lead turns one
instruction into a `TeamPlan`, and `validate_team_plan` topologically orders the roster into waves
of members that may run at the same time. Each member is an ordinary `ToolLoop` bound to its own
`ModelRole` and its own scoped `EditTools`, so routing, tool calling, structured fallback, the
observation format, and the audit ledger are all unchanged.

Concurrency is made safe by construction rather than by locking. Members share one worktree, so the
isolation boundary is the file scope: `validate_team_plan` rejects a roster whose same-wave writers
have overlapping scopes before any model call happens, and `EditTools` then enforces each member's
scope at the point of mutation. Read-only members are constructed to refuse every mutation, because
an empty scope means "unrestricted" rather than "nothing".

This is the concurrency the sequential mission scheduler leaves open, obtained through disjoint
scopes in one worktree instead of disjoint worktrees. Task contracts are untouched.

The QA workspace reuses `TeamRunner` with a stricter surface. Its fixed roster is entirely
read-only and receives `QA_TOOL_SPECS`, which omits every mutation and command action. Independent
architecture, security, code-quality, frontend/backend, and UI specialists form the first wave; a
summarizer depending on all applicable specialists forms the second. Deterministic checks run
through the configured runtime and command policy before the model wave, and their bounded output
is supplied as untrusted evidence. Each resulting `QAReport` carries its project root, is stored in
that repository's `.vasuki/qa/` directory, and is rendered live by `QAView`. The application service
also validates, sorts, and reloads the repository-local report history for the QA tab.

The current repository adapter uses Python AST and lightweight syntax extraction. `Runtime` and
`LLMProvider` are abstract interfaces. A future LSP adapter, Kubernetes runtime, or provider can be
added without changing mission orchestration.

## Multi-layer memory

`MemoryManager` is the service boundary over project SQLite and the private user memory database.
`ContextBuilder` centralizes hierarchical `VASUKI.md`, persistent working state, relevant project
facts/decisions/episodes/failures, source context, compaction, authority rules, and token budgets.
Mission and chat actions checkpoint working state immediately, so provider failure or process exit
does not erase the active plan. Source-derived facts carry digests and become stale when the
repository changes. See [memory architecture](memory.md) for the schema, precedence, retrieval
ranking, commands, migration, security model, and multi-session demonstration.
