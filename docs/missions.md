# Missions

Missions are D[Ai]NO's durable workflow for changes that need an approved plan, an isolated
workspace, repeatable verification, independent review, and exportable evidence. They never edit
the original checkout and never push or merge automatically.

## When to use a mission

Use a direct TUI or GUI prompt for a focused change that you want to see in the current working
tree. Use a mission when the work is larger, must survive interruption, or needs a formal approval
and evidence trail.

```bash
daino plan "Add cursor pagination to the documents API"
daino run "Add cursor pagination to the documents API with tests"
```

The same workflow is available interactively through `/plan`, `/build`, and `/run`.

## Lifecycle

1. **Requirements** — the compiler turns the request into goals, constraints, acceptance criteria,
   and a test strategy.
2. **Plan** — the planner produces dependency-aware, file-scoped tasks and records a plan awaiting
   approval.
3. **Isolation** — execution creates a Git worktree and a pre-change archive checkpoint from the
   original revision.
4. **Implementation** — tasks run in dependency order. Each builder can read, edit, and run
   policy-gated commands inside the mission scope.
5. **Verification** — task checks run as work completes, followed by an integration gate over the
   assembled change when enabled.
6. **Review** — an independent reviewer evaluates correctness, security, compatibility, and test
   evidence. Rejections create bounded corrective work rather than an unbounded loop.
7. **Evidence** — D[Ai]NO records the plan, model selections, changed files, commands, results,
   review, commits, and rollback point.

Verified task commits exist only on the mission branch. Inspect the diff and decide yourself how
to integrate it into the original checkout.

## Approval and execution

A separately created plan waits for approval:

```bash
daino missions list
daino missions show <mission-id>
daino missions approve <mission-id>
daino missions resume <mission-id>
```

In the interactive clients, the approval appears inline with the plan. Plan mode remains
read-only; Ask mode prompts at gates; Session remembers command-category approvals for the current
conversation; Full continues mission gates automatically but still cannot bypass hard-denied
commands or repository boundaries.

## Inspect and recover

Mission state lives in the project database, so a provider error, process exit, or disconnected UI
does not erase the active plan:

```bash
daino missions show <mission-id> --diff
daino missions resume <mission-id>
daino missions retry <failed-mission-id>
daino missions export <mission-id> --format markdown
```

`resume` continues durable unfinished state. `retry` creates a new isolated attempt and preserves
the failed mission's evidence. To stop work or remove its isolated branch and worktree:

```bash
daino missions cancel <mission-id>
daino missions discard <mission-id>
```

Discarding a mission workspace is destructive to that isolated worktree, so the command asks for
confirmation unless `--yes` is supplied. It does not remove changes from the original checkout.

## Tasks too large for the model

A task whose file scope exceeds what the routed builder can hold is cut into smaller tasks rather
than attempted and failed. This happens before the turn when the scope is measurably too big, and
after a stall when a run compacted repeatedly without progress — the signature of a window problem
rather than a stuck model.

Slices appear in the plan as `<task-id>-s1-01`, `-s1-02` and so on, run in sequence, and cover
exactly the parent's files between them. The parent is cancelled and recorded with what replaced it,
so a resumed mission continues with the slices instead of re-attempting the oversized task. Only the
final slice runs the verification commands and commits; the earlier ones leave their work in the
working tree, so the branch never holds a state that no check ever passed.

## Verification behavior

Project verification commands come from `.daino/config.yaml` or are discovered from common Python,
Node.js, Rust, Go, and Git project files. They execute through the selected [coding runtime](runtimes.md).
A missing runtime tool is reported as a prerequisite failure rather than mistaken for a code
failure.

When `verification.require_review` is enabled, a passing test suite is necessary but not
sufficient: the independent review must also approve. Repair attempts are bounded by the
verification configuration, and an unresolved result remains failed or blocked instead of being
reported as complete.

## Missions, teams, and playbooks

| Workflow | Best for | Concurrency and scope |
|---|---|---|
| Direct prompt | One focused task in the current checkout | One tool loop; checkpoint before editing |
| Mission | Durable, approval-gated implementation | Dependency-ordered tasks in one isolated worktree |
| `/team` | Independent subtasks that can run together | Parallel waves with non-overlapping write scopes |
| Playbook | Repeatable domain-specific procedure | A validated template that starts a normal mission |

See [Teams of sub-agents](tui.md#teams-of-sub-agents) and [Playbooks](playbooks.md) for the other
orchestration paths.
