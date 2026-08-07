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
permissions the TUI would have prompted for.

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

The current repository adapter uses Python AST and lightweight syntax extraction. `Runtime` and
`LLMProvider` are abstract interfaces. A future LSP adapter, Kubernetes runtime, or provider can be
added without changing mission orchestration.
