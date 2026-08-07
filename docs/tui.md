# Vasuki interactive terminal UI

The Textual UI is the primary Vasuki experience. It is a presentation layer over the existing
mission engine—not a second implementation of providers, agents, Git, verification, persistence,
or deployment.

## Install and launch

Vasuki requires Python 3.12 or newer and Git. Docker is optional: a project records the runtime
this machine can actually use when it is initialized.

```bash
./scripts/install.sh

cd /path/to/repository
vasuki
```

The installer creates a managed user application and command (normally
`~/.local/bin/vasuki`). No virtual environment activation is needed. See the complete
[installation guide](installation.md) for PATH, upgrade, uninstall, and pipx instructions.

Explicit and cross-project launch forms are equivalent:

```bash
vasuki tui
vasuki --project /path/to/repository
vasuki tui --project /path/to/repository
```

Onboarding runs only when nothing is configured anywhere. Once a model exists in the global
configuration, a new directory is initialized silently — database, index, runtime probe — and opens
straight to the workspace. Provider configuration can still be deferred, so a local Ollama or vLLM
workflow never requires cloud credentials.

Every launch starts a fresh conversation. Resuming the previous one re-sent its whole transcript as
history on the next prompt, so a new session paid for a conversation that was already finished.
Earlier sessions stay in the database and remain browsable.

## Interface

The interface is a single flat column. Hierarchy comes from colour and hairline rules rather than
from panels, boxes, or borders: each kind of message has its own hue, and diffs are the one place
the surface is allowed a filled background, so an added line reads as added at a glance.

The top row carries identity and session vitals: project path, branch, provider, model, runtime,
and on the right a connection dot, token count, and spend. Beneath it a tab strip covers the six
working views — chat, missions, files, changes, tests, logs — with live counts. The remaining
views (repository, approvals, checkpoints, playbooks, deployments, providers, settings, help)
open from the command palette or their slash commands, which is what `ctrl+p  more` advertises.

Just above the prompt, a dim strip reports the active mission, attached file count, verification
state, pending approvals, and the most recent activity. `Ctrl+I` hides it. The prompt itself is a
borderless multiline field behind an accent `❯`, and the bottom row lists the active keys.

Messages are a lowercase role label above their content — `you`, `vasuki`, `edit` for a diff,
`tool` for a command and its output. Colour distinguishes plans, tools, test results, approvals,
errors, and deployments. Focus an event and press Enter or Space to expand its structured
metadata.

The prompt is multiline and supports paste, history, slash completion, and `@` completion.
Files selected in the Files view can be added to or removed from durable session context.
References resolve inside the repository boundary:

Typing `/` opens the complete command menu. Use Up/Down to choose a command, Enter to insert it,
and Enter again to run it. An exactly typed command such as `/bye` runs on the first Enter.
Plain Enter submits normal instructions; Shift+Enter inserts a newline.

Normal text goes to the agent, which decides whether the request was a question to answer or a
change to make. `/ask` forces an answer without touching the repository, and a prompt beginning with
`!` runs a shell command yourself instead of asking the agent. Use `/plan`, `/run`, or `/build` for
the approval-gated mission workflow.

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
`.vasuki/config.yaml`. Themes are `dark`, `light`, and terminal-compatible `system`.

## Slash commands

| Command | Purpose |
|---|---|
| `/help` | Help, workflow, security, shortcuts |
| `/clear` | Clear the current visible transcript |
| `/new [title]` | New persistent conversation session |
| `/ask <question>` | Stream a repository-grounded answer |
| `/plan <instruction>` | Create requirements and an approval-gated plan |
| `/build [instruction]` | Plan a change or execute the active approved plan |
| `/run <instruction>` | Start the full mission workflow |
| `/team <instruction>` | Split the work across parallel scoped sub-agents |
| `/review` | Run a fresh independent model review of active changes |
| `/test [targeted\|failed\|full\|command]` | Run verification asynchronously |
| `/status` | Current project, mission, model, and runtime |
| `/missions` | Durable mission browser |
| `/resume [mission-id]` | Open/resume a mission |
| `/cancel` | Cancel active work safely |
| `/files [query]` | File/symbol browser |
| `/diff [staged]` | Mission or staged diff |
| `/checkpoints` | Checkpoint browser |
| `/checkpoint [description]` | Create a recoverable checkpoint |
| `/restore <checkpoint-id>` | Preview impact and request restore approval |
| `/model [profile]` | Session-only model selection |
| `/provider [name]` | Provider view or connection test |
| `/runtime [local\|docker\|ssh]` | Session runtime switch |
| `/index` | Rebuild repository intelligence |
| `/playbooks` | Playbook browser |
| `/deploy <action> <target>` | Inspect, plan, apply, verify, or roll back |
| `/logs` | Filtered, redacted logs |
| `/settings` | Validated settings |
| `/bye` | Exit safely |
| `/quit` | Quit safely |

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
| `todo` | Record and update a plan for multi-step work |
| `respond`, `finish` | Answer without changing anything, or stop after changes |

Every edit posts its diff as it lands, and every command posts what it ran and what came back, so a
long turn is readable while it happens rather than only at the end.

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

The Providers screen can add and test OpenRouter, local Ollama, local vLLM, and generic
OpenAI-compatible endpoints. Selecting OpenRouter fills its official endpoint and fetches
the current model catalog into a searchable model selector. On save, Vasuki validates the entered
key with OpenRouter before writing any provider configuration. Rejected keys remain unsaved and
the form shows OpenRouter's reason. A pasted valid key is stored privately under
`.vasuki/secrets`; configuration contains only its `file://` reference. Environment and keyring
references remain supported:

```bash
export OPENROUTER_API_KEY='...'
vasuki providers add openrouter \
  --type openrouter \
  --base-url https://openrouter.ai/api/v1 \
  --model openai/gpt-oss-20b:free \
  --api-key-ref env://OPENROUTER_API_KEY

vasuki providers add local-vllm \
  --type vllm \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --local
```

Ollama is a first-class provider type. Pick **Local Ollama** in the TUI, or:

```bash
vasuki providers add local-ollama \
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

`vasuki.events` contains UI-independent dataclass events. `MissionService` emits mission, task,
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
vasuki run "Fix failing authentication test" --non-interactive
vasuki plan "Add JWT authentication"
vasuki repo index
vasuki missions list --json
vasuki test --json
vasuki deploy inspect --target production --json
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
