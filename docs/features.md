# Feature overview

D[Ai]NO uses one agent runtime behind a terminal UI, a browser IDE, and automation-friendly CLI
commands. The table below is a map of the complete feature set and where to learn each workflow.

## Coding workflow

| Feature | What it does | Learn more |
|---|---|---|
| Grounded questions and edits | Reads repository context, answers questions, or edits files from the same prompt surface | [TUI](tui.md), [GUI](gui.md) |
| Agent shell | Runs tests, linters, builds, and inspection commands; uses the results to continue fixing the task | [TUI: commands](tui.md#commands-the-agent-runs) |
| Visible task plans | Streams a live checklist of pending, active, and completed steps | [TUI](tui.md#the-interface), [GUI](gui.md#stop-tasks-and-live-changes) |
| Diff and changeset review | Shows file-level changes, patch details, verification, and restore actions | [TUI](tui.md#the-closing-changeset), [GUI](gui.md#the-changeset) |
| Verification and repair | Runs configured checks, performs bounded repair, and blocks completion while required evidence is failing | [Architecture](architecture.md#the-chat-agent) |
| Independent review | Routes the resulting diff to a reviewer model before a mission is accepted | [CLI reference](cli-reference.md#verification-and-review) |
| Public web research | Searches and fetches bounded public pages through the active network-approval policy | [Security](security.md) |

## Interfaces

| Feature | What it does | Learn more |
|---|---|---|
| Terminal UI | Persistent chat, files, Git, QA, tasks, logs, approvals, providers, memory, and command palette | [Terminal UI](tui.md) |
| Browser IDE | Monaco editor, file explorer/search, source control, terminal, agent panel, and shared sessions | [Browser IDE](gui.md) |
| Design workspace | Creates and edits architecture, flowchart, database, API-flow, UI, and prototype artifacts on a shared canvas | [GUI: Design](gui.md#workspaces-top-tabs) |
| Visual HTML editor | Preview, edit, split, and source modes, with viewport controls and agent-assisted styling | [GUI: visual editing](gui.md#the-visual-html-editor) |
| Project preview | Detects and runs a development server, then embeds the actual app, and makes it the inspection's live target | [GUI: live app](gui.md#live-app) |
| Headless CLI | Supports scripts and CI with structured verification, repository queries, mission controls, and evidence export | [CLI reference](cli-reference.md) |

## Planning, teams, and durability

| Feature | What it does | Learn more |
|---|---|---|
| Isolated missions | Compiles requirements, asks for plan approval, edits a Git worktree, verifies, reviews, and records evidence | [Missions](missions.md) |
| Parallel sub-agents | Splits work into dependency waves with non-overlapping write scopes and read-only specialist roles | [TUI: teams](tui.md#teams-of-sub-agents) |
| Project QA | Combines deterministic tests, linters, browser tests, dependency scans, and parallel read-only specialists | [TUI: QA](tui.md#quality-assurance) |
| Vulnerability assessment | Offline audit for secrets, insecure code, and weak configuration; installed SAST and dependency scanners; a non-destructive probe of the running app | [GUI: the Inspector](gui.md#the-inspector) |
| Release gate | One deterministic verdict — safe to push, review first, or do not push — with every reason it depended on | [GUI: the verdict](gui.md#the-verdict) |
| Sessions | Persists conversation history, selected context, current plans, model choice, and events per repository | [TUI: sessions](tui.md#sessions-and-context), [GUI: sessions](gui.md#sessions) |
| Checkpoints and restore | Archives the working tree before edits and supports reviewed recovery without automatic pushes | [CLI reference](cli-reference.md#memory-and-checkpoints) |
| Playbooks | Loads validated, versioned engineering workflows from built-ins and `.daino/playbooks` | [Playbooks](playbooks.md) |

## Models, context, and memory

| Feature | What it does | Learn more |
|---|---|---|
| Multiple providers | Connects OpenRouter, Ollama, vLLM, and OpenAI-compatible endpoints | [Providers](providers.md) |
| Role-based routing | Assigns architect, planner, builder, reviewer, debugger, tester, summarizer, and deployer roles to different profiles | [Model routing](model-routing.md) |
| Local-model profiles | Provides compact context, staged retrieval, bounded steps, and serialized local-server requests | [Model routing](model-routing.md#small-model-execution-profiles) |
| Repository intelligence | Incrementally indexes languages, symbols, references, routes, tests, dependencies, databases, and entry points | [Repository intelligence](repository-intelligence.md) |
| Hierarchical instructions | Applies global, repository, and directory-scoped `DAINO.md` guidance according to file scope | [Memory](memory.md#dainomd-hierarchy) |
| Durable memory | Recovers unfinished work and retrieves verified facts, decisions, episodes, and preferences without an external vector database | [Memory](memory.md) |

## Safety and operations

| Feature | What it does | Learn more |
|---|---|---|
| Four autonomy modes | Moves between read-only planning, per-command approval, session approval, and full mission execution | [Security](security.md) |
| Policy-gated commands | Allows routine work, prompts for sensitive categories, and refuses destructive patterns | [Security](security.md) |
| Secret references | Resolves `env://`, `keyring://`, and `file://` values only at the provider or SSH boundary | [Configuration](configuration.md) |
| Runtime isolation | Uses local or ephemeral Docker coding runtimes, plus verified SSH for deployment operations | [Runtimes](runtimes.md) |
| Compose deployment | Inspects, plans, deploys, verifies, promotes, and rolls back versioned remote releases | [Deployment](deployment.md) |
| Terraform/OpenTofu | Validates and plans infrastructure, with explicit gates for apply and destroy | [Infrastructure as code](infrastructure.md) |
| Audit and usage data | Records redacted events, token/cost usage, model decisions, checks, approvals, and rollback points | [CLI reference: observability](cli-reference.md#observability) |
| Notifications and wake lock | Signals completion and optionally prevents the machine from sleeping during active work | [TUI](tui.md#notifications-and-staying-awake), [GUI](gui.md#notifications-and-staying-awake) |

## Current boundaries

D[Ai]NO does not silently push, merge, or deploy. Missions execute their dependency-ordered tasks
sequentially; `/team` is the parallel path. Sessions are local to one repository database, and
remote Compose deployment expects an already provisioned Linux host. See the
[README limitations](https://github.com/Chintan99/daino#current-limitations) for the concise list.
