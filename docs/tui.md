# D[Ai]NO interactive terminal UI

The Textual UI is the primary D[Ai]NO experience. It is a presentation layer over the existing
mission engine—not a second implementation of providers, agents, Git, verification, persistence,
or deployment.

> Prefer a graphical editor? `daino . --gui` opens a local browser IDE (Monaco editor, integrated
> terminal, Git views, and AI Design/Preview workspaces) driven by the same agent runtime and the
> same session. See the [browser IDE guide](gui.md).

## Install and launch

D[Ai]NO requires Python 3.12 or newer and Git. Docker is optional: a project records the runtime
this machine can actually use when it is initialized.

```bash
./scripts/install.sh

cd /path/to/repository
daino
```

The installer creates a managed user application and command (normally
`~/.local/bin/daino`). No virtual environment activation is needed. See the complete
[installation guide](installation.md) for PATH, upgrade, uninstall, and pipx instructions.

Explicit and cross-project launch forms are equivalent:

```bash
daino tui
daino --project /path/to/repository
daino tui --project /path/to/repository
```

Onboarding runs when a directory has not been initialized. It offers global or project-specific
model settings before the database, index, and runtime are initialized and the workspace opens.
Provider configuration can still be deferred, so a local Ollama or vLLM workflow never requires
cloud credentials.

Every launch starts a fresh conversation rather than resending an old transcript. Earlier sessions
remain browsable, while structured unfinished-task state is loaded separately. If work was
interrupted, startup shows its goal, progress, last action, remaining steps, and `/resume` command.

## Interface

The interface is a compact, flat workspace. Hierarchy comes from colour and hairline rules rather
than boxes: each kind of message has its own hue, and diffs are the one place the surface is
allowed a filled background. Added and removed lines use green and red fills while their source
keeps language-aware syntax colours instead of becoming uniformly green, red, or white. When the
agent creates a multi-step plan, a right-side checklist appears and updates each item from pending
to active to completed. It hides when there is no plan and on terminals too narrow to show it
safely. Its small activity label reports the live phase: thinking, planning, inspecting, building,
verifying, completed, or needs attention.

While work is active, the checklist header becomes a small terminal runner: the dinosaur runs and
jumps incoming obstacles as agent phases change. It freezes in a collision frame with `ERROR` when
a tool, check, or mission fails; successful completion stops it cleanly at `TASK COMPLETED`.

The two-line header keeps project identity and agent state above model, provider, runtime, usage,
and spend. OpenRouter spend comes from its provider-reported charged `usage.cost`, including the
final usage chunk of a streamed response; very small non-zero charges retain enough precision to
stay visible. Beneath it, one tab row covers the eight working views — chat, missions, QA, files,
changes, tests, logs, map — with live counts and a subtle active state. Tabs switch views on click and
can also be focused and opened with Enter or Space. The remaining views (repository, approvals,
checkpoints, playbooks, deployments, providers, settings, help)
open from the command palette or their slash commands, which is what `ctrl+p commands` advertises.

Just above the prompt, a dim strip reports the active mission, attached file count, verification
state, pending approvals, and the most recent activity. `Ctrl+I` hides it. The compact multiline
prompt sits behind an accent `›`, with a line/character counter and a one-line key bar.

High-frequency messages stay quiet: `›` marks the user's prompt, `…` marks tool and status
activity, and the answer reads as unlabelled prose with a dim `↳` timing footer. Explicit labels
remain for plans, diffs, tests, approvals, errors, and deployments, where the message kind matters
more. Colour distinguishes those events. Focus one and press Enter or Space to expand its
structured metadata.

The prompt is multiline and supports paste, history, slash completion, and `@` completion.
Bracketed paste normalizes Windows and terminal line endings without flattening the block; the
header shows `PASTED`, line count, and character count so large pasted instructions are visible
before they are submitted. Applying a completion replaces only the active token and preserves all
earlier pasted lines.
Files selected in the Files view can be added to or removed from durable session context.
References resolve inside the repository boundary:

Typing `/` opens the complete command menu. Use Up/Down to choose a command, Enter to insert it,
and Enter again to run it. An exactly typed command such as `/bye` runs on the first Enter.
The menu grows upward only to the visible matches, capped at six rows, so the input line stays
anchored while the drawer shrinks as the query narrows. Plain Enter submits normal instructions;
Shift+Enter inserts a newline.

`Shift+Tab` cycles the autonomy mode. The active mode is shown as a filled, colour-coded badge in
the bottom key bar as well as in the header (`Ctrl+Tab` remains a compatibility alias):

| Mode | Behaviour |
|---|---|
| `Plan` | Read-only planning. Bare instructions produce a checklist but do not execute it. |
| `Ask` | Routine repository work runs; installs, network access, and other gated commands ask. |
| `Session` | Agent command approvals are granted until a new conversation starts. |
| `Full` | In-scope commands and mission execution/change gates continue without prompts. |

Hard-denied destructive commands remain blocked in every mode. Plan mode also blocks team writes
and deployment changes. Use `/mode plan|ask|session|full` when a named transition is clearer than
cycling.

Normal text goes to the agent, which decides whether the request was a question to answer or a
change to make. `/ask` forces an answer without touching the repository, and a prompt beginning with
`!` runs a shell command yourself instead of asking the agent. Use `/plan`, `/run`, or `/build` for
the approval-gated mission workflow.

For research questions, the chat agent can search the public web and fetch readable text from the
most relevant pages. In Ask mode the first network operation opens an approval modal; approving for
the session, or using Session/Full mode, lets the agent search and follow source links without
repeated prompts. Results are marked as untrusted content, public `http`/`https` URLs only are
accepted, redirects are rechecked, and localhost/private-network destinations are blocked. Ask for
sources when you want URLs in the final answer.

```text
@file:app/services/tariff.py
@symbol:TariffVersionService.publish
@mission:M-104
@playbook:fix-failing-test
```

`@file:` and `@symbol:` references contribute exact source to repository questions and planning;
the context budget and project boundary still apply.

## Keyboard shortcuts

| Shortcut | Action |
|---|---|
| `Ctrl+P` | Searchable command palette |
| `Shift+Tab` | Cycle Plan / Ask / Session / Full mode |
| `Ctrl+N` | New conversation/mission session |
| `Ctrl+O` | Files |
| `Ctrl+M` | Session model selector |
| `Ctrl+R` | Missions/resume |
| `Ctrl+T` | Run verification |
| `Ctrl+D` | Diff |
| `Ctrl+L` | Redacted logs |
| `Ctrl+I` | Toggle the context strip |
| `Enter` | Submit input |
| `Shift+Enter` | Insert a newline |
| `Esc` | Close modal/cancel prompt interaction |
| `Ctrl+C` | Cancel active work and preserve mission state |
| `Ctrl+Q` | Quit when no critical work is active |
| `?` | Help |

Enter always submits and Shift+Enter always inserts a newline; neither is configurable, so the key
that sends a prompt is the one every chat interface uses. Theme, display mode, hints, streaming
preference, and the custom binding map are validated under the `tui` section of
`.daino/config.yaml`. Themes are `dark`, `light`, and terminal-compatible `system`.

## Slash commands

| Command | Purpose |
|---|---|
| `/help` | Help, workflow, security, shortcuts |
| `/clear` | Clear the current visible transcript |
| `/new [title]` | New persistent conversation session |
| `/mode [plan\|ask\|session\|full]` | Show or set agent autonomy |
| `/ask <question>` | Stream a repository-grounded answer |
| `/plan <instruction>` | Create requirements and an approval-gated plan |
| `/build [instruction]` | Plan a change or execute the active approved plan |
| `/run <instruction>` | Start the full mission workflow |
| `/team <instruction>` | Split the work across parallel scoped sub-agents |
| `/review` | Run a fresh independent model review of active changes |
| `/test [targeted\|failed\|full\|command]` | Run verification asynchronously |
| `/qa [run]` | Open QA or run the complete parallel repository audit |
| `/status` | Current project, mission, model, and runtime |
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
| `/verbose [on\|off]` | Show detailed safe progress or only `working…` |
| `/provider [name]` | Provider view or connection test |
| `/globalprovider` | Configure providers shared by every project |
| `/runtime [local\|docker\|ssh]` | Session runtime switch |
| `/index` | Rebuild repository intelligence |
| `/playbooks` | Playbook browser |
| `/deploy <action> <target>` | Inspect, plan, apply, verify, or roll back |
| `/logs` | Filtered, redacted logs |
| `/map` | Clickable prompt execution graphs with models, tools, timing, tokens, and cost |
| `/settings` | Validated settings |
| `/bye` | Exit safely |
| `/quit` | Quit safely |

Reasoning-effort levels are provider-specific. The current Ollama integration accepts
`/effort auto|none|low|medium|high|max`; choosing a provider-specific level it cannot represent
returns an explicit error instead of silently ignoring the setting.

`/verbose on` expands the live indicator into operational phases such as planning, inspecting,
building, tool execution, and verification. When the selected provider exports a reasoning stream,
a dedicated **thinking · live recent** area also shows its bounded recent tail while that model call
is active. The tail is redacted, control-character safe, markup-safe, never added to the answer or
conversation history, and cleared before a tool, answer, next model call, or completed turn. It is
not stored in the audit log or prompt map. Providers that do not export reasoning retain the normal
phase indicator. `/verbose off` ignores reasoning chunks and collapses live events to `working…`
while keeping final answers, failures, approvals, and results.

The **Logs** tab starts with a live activity section that follows the current model, agent role,
tool, file change, and verification phase with elapsed time. The recorded audit log remains below
it with summary, detailed, and raw redacted modes. During exported model reasoning, Logs shows only
a coalesced `Model reasoning…` state; it never receives the reasoning text itself.

The adjacent **Map** tab lists every recorded project prompt. Selecting a prompt draws a
chronological Unicode graph of the models, tools, files, tasks, and verification events involved.
Each model node shows its own input/output tokens, latency, status, provider, model, and cost;
tool nodes show duration but do not falsely claim independent model tokens. Historical parallel
team relationships are displayed chronologically when no exact parent link was recorded. The map
uses allowlisted structured audit fields and never renders model thoughts, file bodies, edit
contents, raw command output, or secrets. Selecting an older prompt is read-only and does not
replace the current chat session.

Model and runtime selection in a session does not silently overwrite saved routing. Persist changes
explicitly in configuration.

## What the agent can do

A bare prompt runs the agent in a loop: it picks one action, sees the result, and picks the next
one until it answers or finishes. Its actions are

| Action | Purpose |
|---|---|
| `read_file` | Read a file, optionally a window of a large one |
| `glob`, `grep`, `search_text`, `list_directory` | Find files by name, by pattern, or by content |
| `replace`, `multi_edit` | Change exact spans of an existing file |
| `write`, `delete` | Create a new file, or remove one |
| `run_command` | Run a command and read its output |
| `resolve_command_failure` | Link a failed command to a later, successful equivalent check |
| `todo` | Record and update a plan for multi-step work |
| `memory_search`, `memory_list` | Inspect relevant facts, decisions, episodes and fixes |
| `memory_save`, `memory_update`, `memory_verify`, `memory_forget` | Controlled durable memory lifecycle |
| `respond`, `finish` | Answer without changing anything, or stop after changes |

Every edit posts its diff as it lands, and every command posts what it ran and what came back, so a
long turn is readable while it happens rather than only at the end.

After files change, any red command remains unresolved and prevents `finish`; an unrelated green
check cannot erase it. The agent must retry it successfully, split a rejected `a && b` into two
successful commands, or explicitly link an environment-appropriate equivalent command that has
already passed. If none of those is possible, the turn remains failed/blocked instead of claiming
the task was fixed.

Two rules keep the agent honest about what it changed. A whole-file overwrite of a file it has not
read is refused, because replacing a file it has not seen discards work it does not know exists.
And `write` is documented as new files only: rewriting a large file in one reply routinely exceeds
the model's output limit, and a reply cut off part way through is discarded, so a large change is
several `replace` operations instead.

### Commands the agent runs

`run_command` has no shell — one executable and its arguments, so a pipe cannot be smuggled into an
argument. Commands are gated by category:

| Category | Behaviour |
|---|---|
| Tests, linters, type checkers, builds, language runners, `git status`/`diff`/`log` | Run immediately |
| Installs, network tools, `git push`, `git reset --hard` | Ask once; "approve for this session" is remembered |
| `rm -rf`, `mkfs`, `DROP DATABASE`, `terraform destroy`, firewall flushes | Refused, and not approvable |

Approving `pip install httpx` covers `pip install requests` for the rest of the session, but not
`pip uninstall`: the memory is keyed on the executable and its verb. Widen or narrow the safe set
with `security.allowed_commands` and `security.denied_commands`.

When the configured runtime cannot start — Docker installed but its socket unreachable, say — the
agent is told once, in one sentence naming the remedy, and continues without running anything
rather than retrying every command.

## Running your own commands

A prompt beginning with `!` is a command you run yourself, not a request to the agent:

```text
!git status
!cat *.html | wc -l
!pytest -q
```

It runs through a real shell in the project runtime, so pipes, redirects and globs work. It is not
policy-gated and never asks for approval: the gate exists to stop the *model* running something you
did not ask for, and prompting you to approve a command you just typed protects nobody. The command
and its output are added to the conversation, so you can run something and then ask the agent about
the result.

This is deliberately different from the agent's own `run_command`, which has no shell and is gated:
routine commands run unattended, installs and network access ask first, and destructive commands are
refused outright.

## Teams of sub-agents

`/team <instruction>` asks a team lead to split one instruction into a roster of sub-agents and
then runs them, instead of driving a single builder loop start to finish.

Each member declares a role, an objective, and the scope it may modify. Members with no dependency
between them run at the same time; the rest run in dependency order:

```text
/team add rate limiting to the API and cover it with tests

Wave 1 (runs alone):
  survey [architect] Map the current middleware chain
      scope: read-only
Wave 2 (2 members in parallel):
  limiter [builder] Implement the rate limiter
      scope: api/**
  tests [tester] Add rate-limit tests
      scope: tests/**
```

Three rules make running members at once safe, and all three are enforced before any model call:

- **Scopes may not overlap within a wave.** Two members that run at the same time cannot be allowed
  to touch the same path, so a roster with overlapping scopes is rejected and nothing runs. Scopes
  accept exact paths and glob patterns (`*` within one segment, `**` across segments).
- **Writers must declare a scope.** An empty scope means "anything", so a writer without one cannot
  be checked against its peers and is rejected.
- **Explorers are read-only.** Investigating members are constructed so that every mutation is
  refused, not merely unscoped.

Scope is enforced per member while it runs, so a member that ignores its objective still cannot
write outside its lane. A member that fails does not abort its peers: the wave finishes, and
members that depended on the failure are reported as skipped rather than run against work that was
never produced.

A team runs in an isolated Git worktree with a checkpoint taken first, exactly as a mission does,
so `/diff`, `/checkpoints`, and `/restore` work against a team run. Every member's actions land on
the same audit ledger as a solo builder, tagged with the member id.

Teams are capped at eight members. Deployment is deliberately not a team role: a sub-agent spawned
from a chat instruction cannot reach the deployment path.

## Quality assurance workspace

The **QA** tab beside Missions runs a repository-wide, read-only audit. Select **Run QA**, or use
`/qa run`. D[Ai]NO first detects the project stacks and runs applicable deterministic evidence:

- configured lint, type, test, and build commands;
- Playwright end-to-end tests when a local Playwright configuration or script exists;
- `npm`/`pnpm`/`yarn`/`bun` audits, D[Ai]NO's bundled `pip-audit`, and installed `cargo-audit`
  or `govulncheck` scanners for dependency vulnerabilities.

Unavailable optional scanners are shown as skipped with the missing prerequisite; D[Ai]NO does not
silently install a scanner during an audit. Dependency scans that can contact registries request
one network approval in Ask mode, are skipped in Plan mode, and continue automatically in Session
or Full mode.

After command evidence is collected, architecture, security, general code-quality, and detected
frontend/backend specialists run concurrently. Frontend projects also get a UI/accessibility
reviewer that interprets the Playwright result without claiming that static inspection was a
browser test. A final read-only summarizer receives every specialist report and produces the
severity-ordered consolidated report.

QA agents receive only file read/search tools: they cannot edit the application or run arbitrary
commands. The entire QA document scrolls vertically. Reports update live and are preserved under
the current repository's `.daino/qa/` directory. The **Saved scans** table lists prior runs newest
first; selecting a row reloads its specialists, automated evidence, and consolidated report in the
same tab. **Refresh scans** discovers reports created since the tab was opened, while `latest.json`
keeps reopening D[Ai]NO fast.

The Providers screen can add and test OpenRouter, local Ollama, local vLLM, and generic
OpenAI-compatible endpoints. Selecting OpenRouter fills its official endpoint and fetches
the current model catalog into a searchable model selector. On save, D[Ai]NO validates the entered
key with OpenRouter before writing any provider configuration. Rejected keys remain unsaved and
the form shows OpenRouter's reason. A pasted valid key is stored privately under
`.daino/secrets`; configuration contains only its `file://` reference. Environment and keyring
references remain supported:

```bash
export OPENROUTER_API_KEY='...'
daino providers add openrouter \
  --type openrouter \
  --base-url https://openrouter.ai/api/v1 \
  --model openai/gpt-oss-20b:free \
  --api-key-ref env://OPENROUTER_API_KEY

daino providers add local-vllm \
  --type vllm \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --local
```

Ollama is a first-class provider type. Pick **Local Ollama** in the TUI, or:

```bash
daino providers add local-ollama \
  --type ollama \
  --base-url http://127.0.0.1:11434/v1 \
  --model qwen2.5-coder:7b \
  --local
```

Selecting `ollama` (rather than `openai-compatible`) enables native tool calling and Ollama's
top-level `format` schema constraint by default. Local endpoints do not require cloud credentials.

## Architecture and persistence

```text
Providers / mission engine / Git / verification / deployment
                         |
               application services
                         |
                 typed event bus
                         |
          Textual controllers and view models
                         |
             screens and reusable widgets
```

`daino.events` contains UI-independent dataclass events. `MissionService` emits mission, task,
agent, model, tool, file, verification, approval, checkpoint, and completion lifecycle events.
`ProjectContext` persists every event to `mission_events` and the redacted audit log. Textual
workers consume the same stream without blocking the UI thread.

Conversation sessions, messages, active mission, session model, selected context files, events,
tasks, approvals, test reports, checkpoints, reviews, Git locations, and deployment state live in
the existing project database. Reopening the TUI restores the latest session and all durable
mission views.

Only one in-place write mission may own a project worktree. The normal configuration uses isolated
per-mission Git worktrees, allowing independent work without changing the original checkout.

## Automation remains available

Explicit commands never open the TUI:

```bash
daino run "Fix failing authentication test" --non-interactive
daino plan "Add JWT authentication"
daino repo index
daino missions list --json
daino test --json
daino deploy inspect --target production --json
```

## Testing

The suite uses Textual's headless pilot and deterministic provider doubles. It covers initial and
existing-project launch, navigation, narrow terminals, command palette, slash commands, persisted
sessions, streaming events, tool/test/deployment rendering, approval blocking and rejection, the
event sink, and legacy CLI launch behavior.

The end-to-end health workflow proves:

1. TUI instruction and persisted plan.
2. Explicit plan approval.
3. Isolated Git worktree.
4. Initial implementation and a deliberate verification failure.
5. Bounded debugger repair and passing verification.
6. Independent review, commit, diff/evidence, completion.
7. Mission restoration from a reopened project context.

Run it with:

```bash
pytest tests/e2e/test_tui_health_workflow.py
pytest
```

No paid API key is required.

## Known limitations and next work

- Long-running background processes (a dev server) are not managed: commands run to completion
  within a timeout, so the agent cannot start a server and then probe it.
- Plan approval is supported. Structured plan field editing and revision-feedback tasks are not yet
  included.
- Diffs are unified and syntax highlighted. Side-by-side mode and safe hunk restore are next.
- The file browser is intentionally read-only apart from agent context selection; manual editing
  can be added behind the existing controller boundary.
- Deployment progress reflects real manager stages, but more granular Docker/SSH progress requires
  event hooks inside individual runtime operations.
- Sessions are local to one repository database. A future Mission Control client can consume the
  same typed event contracts for multi-user, remote operation.
