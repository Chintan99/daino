# Architecture

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

The scheduler is sequential by design. Task dependencies are validated and topologically ordered.
A later scheduler may execute independent, disjoint worktrees concurrently without changing task
contracts.

The current repository adapter uses Python AST and lightweight syntax extraction. `Runtime` and
`LLMProvider` are abstract interfaces. A future LSP adapter, Kubernetes runtime, or provider can be
added without changing mission orchestration.
