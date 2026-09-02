# Model routing

Roles are `architect`, `planner`, `builder`, `reviewer`, `debugger`, `tester`, `summarizer`, and
`deployer`. Each route names a model profile, while fallbacks are ordered profile names.

Profiles record local/remote placement, context and output limits, planning/coding/debugging/review
scores, tool and structured-output reliability, domain capability, cost, latency, and allowed data
sensitivity.

The router escalates after two task failures, architecture changes, a large affected file set,
security-critical scope, repeated structured-output failures, or persistent test failure. Data
sensitivity can force a suitable local profile. Every selection and reason is persisted.

Fallbacks also provide operational failover. If the selected local server is unavailable, rate
limited, or returns an invalid provider response, D[Ai]NO tries the configured fallbacks in order
and records both the failed call and the replacement selection. A model chosen explicitly for a
session stays pinned and is never silently replaced. Fallback candidates that do not allow the
task's data sensitivity are skipped; work forced local by sensitivity never falls through to a
remote provider.

The selected profile's context window is enforced on every request. D[Ai]NO reserves room for the
model's reply and tool schema, budgets repository context against what remains, and retains the
system prompt, current task, and newest complete tool exchanges as a conversation grows. Oversized
observations are clipped head-and-tail rather than dropping the failure the model needs to repair.

## Small-model execution profiles

Each model profile also controls how a coding task is handed off:

```yaml
models:
  local-coder:
    provider: local-ollama
    model: qwen2.5-coder:7b
    local: true
    coding_score: 8
    execution_mode: compact       # auto, compact, standard
    initial_context_tokens: 8192  # 0 derives it from the model window
    max_agent_steps: 0            # 0 is unlimited; positive values set a hard cap
    no_progress_limit: 3
    staged_retrieval: true
```

`auto` chooses compact mode for context windows at or below 16K, or for local profiles with modest
coding/tool/structured-output reliability. Neutral default capability scores do not classify a
remote model as small. `standard` is an explicit opt-out.

Compact mode sends a task packet containing the objective, acceptance checks, current constraints
and decisions, relevant files, progress, current failures, verification commands, and exactly one
recommended next action. The initial packet contains at most four ranked memories and four source
files. Large relevant files prefer indexed symbol windows; omitted content is labeled rather than
presented as absent. The agent expands context with `read_file`, `grep`, or `memory_search` only
when the current step needs it.

Compact models receive one tool action per turn and a shorter recent-tool history. Deterministic
tests, lint, type checks, and builds remain authoritative. After `no_progress_limit` consecutive
failed or repeated actions, the loop updates its routing context to the existing escalation path;
the next call uses the configured stronger fallback. A model explicitly selected for the session
stays pinned, so D[Ai]NO reports the escalation signal but never silently violates that choice.

```bash
daino models route builder local-coder --fallback strong-cloud
daino models route debugger strong-cloud
```

## Task size follows the executing model

The planner is told what the **builder** can hold, not what the planner itself can. The shipped
configuration pairs a strong cloud planner with a local builder, and without this the planner writes
tasks in its own terms: a scope the executor cannot fit, which it then thrashes on.

Those limits are derived from the same arithmetic that sizes context packing — files per task,
source tokens per task, and the working room left after that context is re-added on every compaction
pass — and are rendered into the planning prompt as numbers, alongside the file sizes the repository
map already reports.

A task that overruns them anyway is split rather than failed:

- **Before the turn.** If the scope no longer fits — a file grew, a glob expanded, or the plan was
  made with no builder routed — the task is cut into slices without spending a model call.
- **After a stall.** A run that stops having compacted repeatedly is a window problem, not a stuck
  model, so the budget is tightened against that evidence and the task is split again.

Slices are cut deterministically over the task's own file list, in the planner's order, and their
union is exactly the parent's scope. They run in sequence, and only the last one carries the
parent's verification commands — the earlier ones do not commit, so nothing enters the branch that
no check has passed. Splitting is bounded: two rounds per planned task, never a slice of one file,
and never a "split" into a single copy of the original.

One case arithmetic cannot decide is a single file larger than the whole per-task budget, where the
split has to run *through* the file. There the planner is asked once more, given that file's symbol
outline so it can cut along real boundaries; the ids and dependencies it returns are rebuilt rather
than trusted.

A Workspace plan is prose and has no file scope to pack, so nothing is split automatically. Instead
the plan is scaled up front — how many artifacts one step may produce, how much material it may read
— and a step that stalls on compaction says so, naming the fix, rather than reporting a generic
failure.
