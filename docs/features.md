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
| Public web research | Searches and fetches bounded public pages through the active network-approval policy; DuckDuckGo by default, or an API-backed engine | [Security](security.md), [Configuration](configuration.md#web-search) |
| Compiler feedback on edits | Every edit reports the language server's errors and warnings for the file, unasked, so a broken caller surfaces immediately rather than at the next test run | [Repository intelligence](repository-intelligence.md) |
| Definition and reference lookup | The agent resolves symbols through the same language servers the IDE uses, by name rather than by line and column | [Repository intelligence](repository-intelligence.md) |
| Mid-turn delegation | The model can split independent work across scoped subagents itself, with non-overlapping write scopes checked before any of them start | [TUI: teams](tui.md#teams-of-sub-agents) |
| Parallel tool calls | Independent lookups in one turn are awaited together, so four reads cost one read's latency | [Architecture](architecture.md#the-chat-agent) |
| Image input | Screenshots, mockups and exported diagrams reach a vision-capable model, by `@image:` reference or by the agent reading one | [TUI](tui.md) |

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
| Project QA | Combines deterministic tests, linters, browser tests, dependency scans, and parallel read-only specialists | [TUI: Inspector](tui.md#inspector) |
| Knowledge workspaces | A goal, uploaded files, documents, a plan, and research — continuing across many sessions, stored as real files under `.daino/workspaces/` | [Workspace](workspace.md) |
| Document extraction | PDF, Word, Excel, and PowerPoint uploads become markdown the agent can read | [Workspace: uploads](workspace.md#uploads) |
| Cited research | Every page the agent fetches is recorded as a source and cached, so a claim stays checkable | [Workspace: research](workspace.md#research-and-sources) |
| Executable plans | Daino works through a workspace plan one step per agent turn, pausing, resuming and reporting as it goes | [Workspace: running the plan](workspace.md#running-the-plan) |
| Steerable runs | New direction typed mid-run updates the plan at the next step boundary without discarding finished work | [Workspace: steering](workspace.md#steering-it-while-it-works) |
| Run approvals | Actions are classified by what they can cost; reading and writing in the workspace proceed, commands and deletions ask | [Workspace: approvals](workspace.md#approvals) |
| Change sets | Everything one step touched, reviewed and undone together, on top of the per-file history | [Workspace: reviewing](workspace.md#reviewing-what-changed) |
| Skills | Reusable ways of working — PRD, competitive research, incident review — chosen from the goal and overridable per project | [Workspace: skills](workspace.md#skills) |
| Office deliverables | Word, Excel, PowerPoint and PDF rendered from a document with its structure intact | [Workspace: finished files](workspace.md#finished-files) |
| Stale-document warnings | A document written from another is flagged when its source changes; advisory, never rewritten automatically | [Workspace: provenance](workspace.md#provenance-and-stale-documents) |
| Cross-mode handoff | A workspace starts diagrams in DESIGN and prepares coding work for CODE, linking both back | [Workspace: CODE and DESIGN](workspace.md#working-with-code-and-design) |
| Vulnerability assessment | Offline audit for secrets, insecure code, and weak configuration; installed SAST and dependency scanners; a non-destructive probe of the running app | [GUI: the Inspector](gui.md#the-inspector) |
| Change review | Reviews one change — working tree, staged, or branch against base — for correctness, gaps, and what was left behind, ending in a merge verdict | [GUI: review](gui.md#review) |
| Syntax verification | Every changed file is re-parsed, through the language's own parser or its grammar, so a change that stops a file compiling cannot pass | [GUI: review](gui.md#review) |
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

## Extensibility

| Feature | What it does | Learn more |
|---|---|---|
| Slash commands | Reusable prompts invoked by name, with argument substitution and namespacing | [Extending](extending.md#slash-commands) |
| Agent skills | Written-down practice the model loads when the task matches its description, so a dozen skills cost a dozen lines of prompt | [Extending](extending.md#skills) |
| Lifecycle hooks | Commands run before and after each tool, on prompt submit, and at stop; can block an action or feed the model what changed | [Extending](extending.md#hooks) |
| MCP servers | External tool servers over stdio or HTTP, discovered and namespaced, reaching the model as ordinary tools | [Extending](extending.md#mcp-servers) |
| Extension diagnostics | One endpoint reporting what loaded, what did not, and why — because everything here fails quietly by design | [Extending](extending.md#seeing-what-loaded) |

## Safety and operations

| Feature | What it does | Learn more |
|---|---|---|
| Four autonomy modes | Moves between read-only planning, per-command approval, session approval, and full mission execution | [Security](security.md) |
| Policy-gated commands | Allows routine work, prompts for sensitive categories, and refuses destructive patterns | [Security](security.md) |
| Secret references | Resolves `env://`, `keyring://`, and `file://` values only at the provider or SSH boundary | [Configuration](configuration.md) |
| Runtime isolation | Local, sandboxed-local, or ephemeral Docker coding runtimes, plus verified SSH for deployment operations | [Runtimes](runtimes.md) |
| Sandboxed local mode | The host toolchain with credentials scrubbed from the environment and, where the platform provides it, writes confined to the project and the network denied | [Runtimes](runtimes.md#sandboxed-local-mode) |
| Spend ceilings | Per-mission cost, token, and call limits that stop a productive but useless run, enforced rather than only recorded | [Configuration](configuration.md#budget) |
| Compose deployment | Inspects, plans, deploys, verifies, promotes, and rolls back versioned remote releases | [Deployment](deployment.md) |
| Terraform/OpenTofu | Validates and plans infrastructure, with explicit gates for apply and destroy | [Infrastructure as code](infrastructure.md) |
| Audit and usage data | Records redacted events, token/cost usage, model decisions, checks, approvals, and rollback points | [CLI reference: observability](cli-reference.md#observability) |
| Distributed tracing | OpenTelemetry spans around every model call, loop step, tool execution and delegation, carrying identity and cost but never prompts or file contents | [Configuration](configuration.md) |
| Eval harness | Measures end-to-end task success per model, and pins the retrieval ranking and context sizing constants with model-free cases that run in CI | [Evals](evals.md) |
| Notifications and wake lock | Signals completion and optionally prevents the machine from sleeping during active work | [TUI](tui.md#notifications-and-staying-awake), [GUI](gui.md#notifications-and-staying-awake) |

## Current boundaries

D[Ai]NO does not silently push, merge, or deploy. Missions execute their dependency-ordered tasks
sequentially; `/team` is the parallel path. Sessions are local to one repository database, and
remote Compose deployment expects an already provisioned Linux host. See the
[README limitations](https://github.com/Chintan99/daino#current-limitations) for the concise list.
