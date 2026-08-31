# CLI reference

Run `daino --help` or `daino <group> --help` for the authoritative options installed with your
version. Commands operate on the current repository unless `--project <path>` is supplied.

## Interactive workspaces

| Command | Purpose |
|---|---|
| `daino . --tui` | Open the terminal workspace for the current directory |
| `daino . --gui` | Start the browser IDE in the background and open it |
| `daino . --gui --foreground` | Keep the browser IDE server attached to the terminal |
| `daino ps` | List background GUI servers (session id, URL, project, PID, started, uptime) |
| `daino kill [.\|<session-id>]` | Stop a GUI server by directory (default: current) or session id |
| `daino tui --project <path>` | Open the TUI with an explicit project path |

Bare `daino` or `daino --help` prints every command; `daino --tui` and `daino --gui` open the
workspaces for the current directory.

The browser server binds to `127.0.0.1:4173` by default. Use `--port 0` for a free port,
`--no-browser` to suppress auto-opening, or `--host` only when deliberate remote exposure is
required. Read [Browser IDE security](gui.md#security) first.

## Setup and diagnostics

| Command | Purpose |
|---|---|
| `daino init [path]` | Initialize configuration, database, runtime detection, Git baseline, and repository index |
| `daino doctor` | Check configuration, database, Git, and optional runtime tools |
| `daino doctor --fix-terminal` | Restore a terminal left in alternate-screen or mouse mode after a native crash |
| `daino config show` | Print validated configuration with secret references, never secret values |
| `daino config set <key> <value>` | Update a dotted setting using a YAML-formatted value |
| `daino config validate` | Validate the complete merged configuration |

## Providers and routing

| Command | Purpose |
|---|---|
| `daino providers list` | List configured endpoints and secret references |
| `daino providers add <name> ...` | Add a provider and matching model profile |
| `daino providers test <name>` | Run a provider health check |
| `daino providers remove <name>` | Remove a provider and its project routing |
| `daino models list` | List model profiles, context windows, and assigned roles |
| `daino models test <profile>` | Make a minimal completion request |
| `daino models route <role> <profile>` | Assign a profile and optional fallbacks to one agent role |

See [Providers](providers.md) and [Model routing](model-routing.md) for examples and configuration.

## Repository intelligence

| Command | Purpose |
|---|---|
| `daino repo index` | Refresh the incremental repository index |
| `daino repo status` | Show index freshness and summary |
| `daino repo map` | Render a compact repository map |
| `daino repo symbols [query]` | List or search declarations |
| `daino repo references <symbol>` | Find indexed references to a declaration |
| `daino repo routes` | List detected API routes |
| `daino repo databases` | List database and persistence candidates |
| `daino repo tests` | List detected test files and frameworks |
| `daino repo dependencies` | Show dependency relationships |

## Missions

| Command | Purpose |
|---|---|
| `daino ask "<question>"` | Ask about a compact repository summary without editing |
| `daino plan "<request>"` | Compile requirements and persist an approval-gated task plan |
| `daino build "<request>"` | Implement and verify a change in an isolated worktree |
| `daino run "<request>"` | Plan, implement, verify, review, commit in the worktree, and export evidence |
| `daino run "<request>" --non-interactive` | Run without prompts; approval-requiring actions fail closed |
| `daino missions list` | List durable missions |
| `daino missions show <id> --diff` | Inspect status, tasks, and the mission diff |
| `daino missions resume <id>` | Continue an interrupted mission |
| `daino missions cancel <id>` | Cancel a pending or running mission |
| `daino missions retry <id>` | Start a new isolated attempt while preserving failed evidence |
| `daino missions approve <id>` | Record plan approval |
| `daino missions discard <id>` | Discard a mission workspace after confirmation |
| `daino missions export <id> --format markdown` | Export the evidence bundle |

Missions edit an isolated Git worktree and never push or merge automatically. Use `--mode` on
planning/build commands to select a project mode when needed.

## Verification and review

| Command | Purpose |
|---|---|
| `daino test` | Run the project's configured verification commands |
| `daino test "pytest -q"` | Run an explicit command through the configured runtime |
| `daino test --json` | Emit structured verification data for scripts or CI |
| `daino review` | Ask an independent reviewer model to inspect the current Git diff |

## Memory and checkpoints

| Command | Purpose |
|---|---|
| `daino memory list` | List durable memory records |
| `daino memory search "<query>"` | Search project and user memory |
| `daino memory forget <id>` | Remove one memory record |
| `daino memory verify <id>` | Mark a record verified after checking its source |
| `daino memory clear-project` | Clear project memory after confirmation |
| `daino checkpoints list` | List recoverable workspace snapshots |
| `daino checkpoints create --description "<text>"` | Create a manual checkpoint |
| `daino checkpoints restore <id>` | Restore matching files after confirmation |

Read [Memory and DAINO.md](memory.md) for scope, precedence, staleness, and storage details.

## Playbooks

| Command | Purpose |
|---|---|
| `daino playbooks list` | List built-in and project playbooks |
| `daino playbooks show <name>` | Print the validated playbook YAML |
| `daino playbooks run <name> --request "<request>"` | Start the playbook as a specification-mode mission |

## Deployment

| Command | Purpose |
|---|---|
| `daino deploy inspect --target <name>` | Inspect a remote target without changes |
| `daino deploy plan --target <name>` | Create a structured deployment plan |
| `daino deploy apply --target <name> --approve` | Upload, start, verify, and promote a versioned release |
| `daino deploy verify --target <name>` | Run Compose and health-endpoint checks |
| `daino deploy status --target <name>` | Show recorded target state |
| `daino deploy logs --target <name>` | Retrieve bounded service logs |
| `daino deploy rollback --target <name> --approve` | Restore the previous healthy release |

See [Deployment](deployment.md) for target configuration and safety behavior.

## Infrastructure

| Command | Purpose |
|---|---|
| `daino infra validate` | Format-check and validate Terraform/OpenTofu files |
| `daino infra plan` | Produce an infrastructure plan |
| `daino infra apply --approve` | Apply after explicit approval |
| `daino infra destroy --approve --confirm destroy` | Destroy after the stronger confirmation gate |

## Observability

| Command | Purpose |
|---|---|
| `daino stats` | Emit project usage and execution statistics as JSON |
| `daino stats --mission <id>` | Limit statistics to one mission |
| `daino logs --limit 100` | Print recent redacted audit events |
| `daino logs --mission <id>` | Filter audit events to one mission |

OpenTelemetry export is optional and installed with the `otel` package extra. Audit logs and
database events remain local unless you deliberately configure an exporter.
