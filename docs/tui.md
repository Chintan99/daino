# Terminal UI (TUI)

The Textual terminal UI is the primary D[Ai]NO experience — a presentation layer over the shared
mission engine, not a second implementation of providers, agents, Git, verification, or deployment.
Prefer a graphical editor? `daino . --gui` opens the [browser IDE](gui.md) on the same runtime and
the same sessions.

## Launch

Requires Python 3.12+ and Git; Docker is optional. These forms are equivalent:

```bash
daino . --tui
daino tui
daino --project /path/to/repo --tui
```

First launch in an uninitialized directory runs onboarding (choose global or project model settings)
before the database, index, and runtime are created. Provider setup can be deferred, so a local
Ollama or vLLM workflow never needs cloud credentials. Every launch starts a fresh conversation;
earlier sessions stay browsable, and interrupted work shows its goal, progress, and `/resume`
command on startup.

## The interface

A compact, flat workspace: hierarchy comes from colour and hairline rules, not boxes. Each message
kind has its own hue, and diffs get filled green/red backgrounds while keeping syntax colours.

- **Two-line header** — project and agent state above model, provider, runtime, usage, and spend
  (OpenRouter spend is its provider-reported charged `usage.cost`).
- **Tab row** — eight working views with live counts: **chat, missions, QA, files, changes, tests,
  logs, map**. Click, or focus and press Enter/Space. The rest (repository, approvals, checkpoints,
  playbooks, deployments, providers, settings, help) open from the command palette or slash commands.
- **Checklist panel** (right) — appears when the agent makes a multi-step plan; each item moves
  pending → active → completed, with a live phase label (thinking, planning, inspecting, building,
  verifying). Its header becomes a running dinosaur that freezes on `ERROR` or stops at
  `TASK COMPLETED`. Below it, while a turn edits, an **EDITED** list shows every file touched with
  running `+`/`-` counts.
- **Context strip** — a dim line above the prompt reporting active mission, attached files,
  verification state, pending approvals, and latest activity. `Ctrl+I` hides it.
- **Prompt** — multiline, with paste, history, slash completion, and `@` completion.

## Autonomy modes

`Shift+Tab` cycles the mode (shown as a colour-coded badge); `/mode <name>` sets one directly.

| Mode | Behaviour |
|---|---|
| `Plan` | Read-only planning. Instructions produce a checklist but do not execute it. |
| `Ask` | Routine repository work runs; installs, network, and other gated commands ask once. |
| `Session` | Command approvals are granted until a new conversation starts. |
| `Full` | In-scope commands and mission execution/change gates continue without prompts. |

Hard-denied destructive commands stay blocked in every mode. Plan mode also blocks team writes and
deployment changes.

## Talking to the agent

Plain text goes to the agent, which decides whether the request is a question to answer or a change
to make. `/ask` forces an answer without touching the repository. `/plan`, `/run`, and `/build`
drive the approval-gated mission workflow. A prompt beginning with `!` runs a shell command
*yourself* — through a real shell in the project runtime, so pipes and redirects work — and adds its
output to the conversation. It is not policy-gated, because a command you just typed needs no
approval.

**References** attach exact source (within the repository boundary and context budget):

```text
@file:app/services/tariff.py
@symbol:TariffVersionService.publish
@mission:M-104
@playbook:fix-failing-test
```

**Web research** — the agent can search the public web and fetch readable text from the most
relevant pages. In Ask mode the first network operation asks for approval; Session/Full skip the
prompt. Only public `http`/`https` URLs are accepted, redirects are rechecked, and
localhost/private-network destinations are blocked. Results are marked untrusted; ask for sources to
get URLs in the answer.

## Keyboard shortcuts

| Shortcut | Action | Shortcut | Action |
|---|---|---|---|
| `Ctrl+P` | Command palette | `Ctrl+D` | Diff |
| `Shift+Tab` | Cycle autonomy mode | `Ctrl+L` | Redacted logs |
| `Ctrl+N` | New session | `Ctrl+I` | Toggle context strip |
| `Ctrl+O` | Files | `Enter` | Submit |
| `Ctrl+M` | Session model selector | `Shift+Enter` | Newline |
| `Ctrl+R` | Missions / resume | `Esc` | Close modal / cancel |
| `Ctrl+T` | Run verification | `Ctrl+C` | Cancel work, preserve state |
| `?` | Help | `Ctrl+Q` | Quit when safe |

Enter always submits and Shift+Enter always inserts a newline; neither is configurable. Theme
(`dark`, `light`, `system`), display mode, hints, streaming, and custom bindings are validated under
the `tui` section of `.daino/config.yaml`.

## Slash commands

Type `/` to open the command menu; Up/Down to choose, Enter to insert, Enter again to run (an
exactly typed command like `/bye` runs on the first Enter). The same catalog is available in the
[browser IDE](gui.md#slash-commands).

| Command | Purpose |
|---|---|
| `/help` | Help, workflow, security, shortcuts |
| `/clear` | Clear the visible transcript |
| `/new [title]` | New persistent conversation session |
| `/mode [plan\|ask\|session\|full]` | Show or set agent autonomy |
| `/ask <question>` | Repository-grounded answer, no edits |
| `/plan <instruction>` | Requirements plus an approval-gated plan |
| `/build [instruction]` | Plan a change or execute the active approved plan |
| `/run <instruction>` | Start the full mission workflow |
| `/team <instruction>` | Split work across parallel scoped sub-agents |
| `/review` | Fresh independent model review of active changes |
| `/test [targeted\|failed\|full\|command]` | Run verification asynchronously |
| `/qa [run]` | Open QA or run the full parallel repository audit |
| `/status` | Current project, mission, model, runtime |
| `/missions` | Durable mission browser |
| `/tasks` | List unfinished crash-safe task state |
| `/memory [subcommand]` | Inspect/search/verify/forget scoped memory |
| `/resume [mission-id]` | Open/resume a mission |
| `/cancel` | Cancel active work safely |
| `/files [query]` | File/symbol browser |
| `/diff [staged]` | Mission or staged diff |
| `/checkpoints` | Checkpoint browser |
| `/checkpoint [description]` | Create a recoverable checkpoint |
| `/restore <checkpoint-id>` | Preview impact and request restore approval |
| `/model [profile]` | Session-only model selection |
| `/effort [auto\|none\|minimal\|low\|medium\|high\|xhigh\|max]` | Session reasoning effort |
| `/verbose [on\|off]` | Show detailed live progress or only `working…` |
| `/provider [name]` | Provider view or connection test |
| `/globalprovider` | Configure providers shared by every project |
| `/runtime [local\|docker\|ssh]` | Session runtime switch |
| `/index` | Rebuild repository intelligence |
| `/playbooks` | Playbook browser |
| `/deploy <action> <target>` | Inspect, plan, apply, verify, or roll back |
| `/logs` | Filtered, redacted logs |
| `/map` | Prompt execution graphs with models, tools, timing, tokens, cost |
| `/settings` | Validated settings |
| `/bye`, `/quit` | Exit safely |

Reasoning-effort levels are provider-specific; a level a provider cannot represent returns an
explicit error rather than being silently ignored. `/verbose on` expands the live indicator into
phases and, when the provider exports a reasoning stream, shows a bounded, redacted **thinking ·
live recent** tail that is never added to the answer, history, or audit log. `/verbose off`
collapses live events to `working…`.

## What the agent can do

A bare prompt runs the agent in a loop: pick one action, see the result, pick the next, until it
answers or finishes. Every edit posts its diff as it lands and every command posts what it ran, so a
long turn stays readable.

| Action | Purpose |
|---|---|
| `read_file`, `glob`, `grep`, `search_text`, `list_directory` | Read and find files by name, pattern, or content |
| `replace`, `multi_edit`, `write`, `delete` | Change spans, create files, or remove them |
| `run_command`, `resolve_command_failure` | Run a command; link a failure to a later passing check |
| `todo` | Record and update a plan for multi-step work |
| `memory_search`, `memory_list`, `memory_save`, `memory_update`, `memory_verify`, `memory_forget` | Durable memory lifecycle |
| `respond`, `finish` | Answer without changing anything, or stop after changes |

Two honesty rules: a whole-file overwrite of a file the agent has not read is refused, and `write`
is for new files only (large rewrites are several `replace` edits, because a reply cut off past the
output limit is discarded). After files change, any failing command blocks `finish` — an unrelated
green check cannot erase it; the agent must retry it, split a rejected `a && b`, or link a passing
equivalent.

### Commands the agent runs

`run_command` has no shell — one executable and its arguments, so a pipe cannot be smuggled in.
Commands are gated by category:

| Category | Behaviour |
|---|---|
| Tests, linters, type checkers, builds, language runners, `git status`/`diff`/`log` | Run immediately |
| Installs, network tools, `git push`, `git reset --hard` | Ask once; "approve for this session" is remembered |
| `rm -rf`, `mkfs`, `DROP DATABASE`, `terraform destroy`, firewall flushes | Refused, not approvable |

Approving `pip install httpx` covers `pip install requests` for the session but not `pip uninstall`:
the memory is keyed on the executable and its verb. Widen or narrow with `security.allowed_commands`
and `security.denied_commands`.

## Teams of sub-agents

`/team <instruction>` asks a team lead to split one instruction into a roster of sub-agents that run
in dependency order, with independent members running in parallel:

```text
/team add rate limiting to the API and cover it with tests

Wave 1: survey [architect]  scope: read-only
Wave 2: limiter [builder] scope: api/**   ·   tests [tester] scope: tests/**
```

Three rules, all enforced before any model call: **scopes may not overlap within a wave**, **writers
must declare a scope** (empty means "anything" and is rejected), and **explorers are read-only**. A
team runs in an isolated Git worktree with a checkpoint taken first, so `/diff`, `/checkpoints`, and
`/restore` all apply. A failed member does not abort its peers; dependents are reported skipped.
Teams cap at eight members, and deployment is deliberately not a team role.

## Inspector

The **inspector** tab has two halves, reachable with `/inspector`, `/review`, or the older `/qa`.

### Repository scan

**Run scan** (or `/qa run`) runs a repository-wide, read-only audit *and* a vulnerability
assessment. D[Ai]NO detects the project stacks and collects deterministic evidence — configured
lint/type/test/build commands, Playwright e2e tests when present, dependency audits
(`npm`/`pnpm`/`yarn`/`bun`, bundled `pip-audit`, installed `cargo-audit`/`govulncheck`), and
whichever security scanners the host has (`bandit`, `gitleaks`, `semgrep`, `osv-scanner`, `trivy`).
A built-in offline audit runs regardless of what is installed: credential shapes, insecure code
patterns, and weak container/IaC/CI configuration. Unavailable scanners are shown as skipped;
nothing is installed silently.

Then architecture, application-security, threat-model, supply-chain, code-quality, and detected
frontend/backend specialists run concurrently (read/search tools only — they cannot edit or run
commands), and a summarizer produces the severity-ordered consolidated report. Everything is folded
into one findings list and a deterministic **release-gate verdict** — pass, review, or blocked —
which the completion notification carries. Reports are saved under `.daino/qa/`; the **Saved scans**
table reloads any prior run.

The live probe of a running application is browser-only; the terminal client has no app to point it
at. See [the Inspector](gui.md#the-inspector).

### Change review

**Review change** reads the change in front of you rather than the whole repository. Pick the scope
beside the button — the working tree (including files you have just created), what is staged, or
this branch against its base.

It runs mechanically first, over only the lines the change introduced: every changed file is
re-parsed for syntax, and it looks for conflict markers, debugging left in, credentials, deceptive
unicode, missing or weakened tests, lockfile and migration drift, and removed public definitions.
Then four read-only reviewers — correctness, gaps, impact, security — read the diff and the code
around it, and a synthesis step writes up what the change does, what blocks it, and what is missing.

It ends in a merge verdict: **ready to merge**, **needs a look**, or **do not merge**. Reviews are
saved under `.daino/reviews/`. See [Review](gui.md#review) for the full rule list.

## Providers

The Providers screen adds and tests OpenRouter, local Ollama, local vLLM, and generic
OpenAI-compatible endpoints. Selecting OpenRouter fills its endpoint and fetches the live model
catalog into a searchable selector; on save the key is validated before anything is written, and a
pasted key is stored privately under `.daino/secrets` (config keeps only a `file://` reference).
`env://` and `keyring://` references are also supported.

```bash
daino providers add openrouter --type openrouter \
  --base-url https://openrouter.ai/api/v1 --model openai/gpt-oss-20b:free \
  --api-key-ref env://OPENROUTER_API_KEY

daino providers add local-ollama --type ollama \
  --base-url http://127.0.0.1:11434/v1 --model qwen2.5-coder:7b --local
```

Choosing `ollama` (rather than `openai-compatible`) enables native tool calling and Ollama's
top-level `format` schema constraint. Local endpoints need no cloud credentials.

## Sessions and context

`/new [title]` (Ctrl+N) starts a fresh conversation. That matters beyond tidiness: each turn sends
the session's recent exchanges as history, so a long session makes every prompt larger and answers a
new request in the shadow of old ones. On a local model, where prompt size is throughput, starting a
session for new work is often the cheapest speed-up available. Sessions are named after their first
request, and the TUI and GUI share the same session store.

## The closing changeset

A turn that edited anything ends with one summary of everything it touched, so "what changed?" never
means scrolling back:

```
changed
Edited 3 files  +48 -17
  new.py        +30 -0
  README.md     +16 -1
```

The counts carry the colour because they are what gets scanned. The same message drives the browser
IDE's changeset card, so the two clients cannot disagree about what a turn did.

## Notifications and staying awake

The client raises an OS notification (and the terminal bell) when a turn completes, when something
fails, and when a command is waiting for approval — the three moments that arrive after you look
away. While work is in progress it holds an OS sleep inhibitor (`caffeinate`, `systemd-inhibit`, or
`SetThreadExecutionState`), released as soon as work ends. Both are configurable:

```
/settings
notifications.on_completed   false
notifications.desktop        false
keep_awake                   false
```

Notification preferences are user-level and follow you between projects; `DAINO_NOTIFY=off` and
`DAINO_WAKELOCK=off` disable each for one run.

## Automation

Explicit commands never open the TUI, so the same runtime scripts in CI:

```bash
daino run "Fix failing authentication test" --non-interactive
daino plan "Add JWT authentication"
daino test --json
daino missions list --json
daino deploy inspect --target production --json
```

See the [CLI reference](cli-reference.md) for the complete command set.

## Known limitations

- Long-running background processes (a dev server) are not managed: commands run to completion within
  a timeout, so the agent cannot start a server and then probe it.
- Diffs are unified and syntax-highlighted; side-by-side and hunk-level restore are next.
- The file browser is read-only apart from agent context selection.
- Sessions are local to one repository database.
