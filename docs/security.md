# Security

The main trust boundaries are the repository, model provider, execution runtime, secret store, Git
workspace, and deployment target.

The TUI exposes four explicit autonomy modes through `Shift+Tab` or `/mode`: Plan is read-only,
Ask prompts for approval-gated commands, Session grants those command approvals until a new
conversation starts, and Full also auto-approves mission execution/change gates. Full access does
not bypass hard-denied destructive-command rules, repository boundaries, or deployment safety
checks.

The browser IDE (`daino . --gui`) is held to the same model. It binds `127.0.0.1` by default and
never `0.0.0.0` implicitly, so the file, shell, and terminal APIs are not network-exposed without
deliberate configuration. Command approvals round-trip over the WebSocket to the same `CommandGate`
and `PolicyEngine` the TUI uses — the GUI is not permitted to be less secure than the TUI — and
never-approvable commands are still refused. File writes from the editor use content-hash
optimistic concurrency and reject a stale write rather than clobbering an out-of-band change. The
GUI never commits or pushes, and Design changes never write production code without an explicit
plan-first "Implement Design" step.

Configuration stores secret references, never values. `env://NAME`, `keyring://service/account`,
and `file://path` are resolved immediately before use. The Providers screen may accept a pasted
OpenRouter key, but validates it first and then writes it under `.daino/secrets` with user-only
permissions; YAML receives only the resulting `file://` reference. Key paths are passed to
AsyncSSH; key contents are not read into prompts. Likely credentials and private keys are redacted
from tool results and JSON audit logs.

`PolicyEngine` classifies repository reads/writes, local/container commands, installations, network
access, secrets, migrations, deployment, firewall/proxy changes, deletion, and rollback. Forced
recursive deletion, host shutdown, filesystem formatting, destructive SQL, global Docker pruning,
recursive permission changes, firewall flush, and infrastructure destroy are blocked unless the
specific operation has explicit approval.

Local commands are tokenized with `shlex` and executed without a shell. Docker sandbox commands run
in an ephemeral container as the invoking user, with CPU/memory limits, a workspace-only mount,
timeout, automatic cleanup, and network disabled by default. Read-only Docker daemon inspection and
Compose configuration commands may use the host Docker client; builds, service lifecycle changes,
and other daemon mutations require approval. The daemon socket is never mounted into the sandbox.
Remote inspection uses a fixed read-only command inventory. SSH host key verification uses
AsyncSSH's known-host behavior or an explicit `known_hosts` path.

Agent web research is separate from the project runtime and always follows the network approval
policy. It accepts only public HTTP(S) destinations, rejects URL credentials and local/private/link-
local addresses, resolves and checks hostnames before connecting, and revalidates every redirect.
Downloads and extracted text are bounded, binary responses are rejected, scripts/styles are
removed from HTML, and page text is labeled as untrusted data in the model observation.

The Inspector's vulnerability assessment is subject to the same boundaries as everything else. The
offline audit only reads the working tree — it never executes project code, resolves a name, or
shells out — and it reports a matched credential with the value masked, so a report shared with a
teammate does not become the leak. Third-party scanners are run through the same runtime and command
policy as any other check, and none of them is installed on your behalf.

The live probe is deliberately narrow. It issues only `GET`, `HEAD`, and `OPTIONS`, sends no
payload, follows no redirect, and reads a bounded prefix of each response. Its target must resolve
entirely to loopback, private, or link-local address space; anything else is refused until the
caller asserts ownership, and that assertion is written to the audit log as
`InspectionRemoteTargetAuthorized`. Every request it makes is listed in the check's own output, so
what was probed is reviewable after the fact.

A Workspace is a boundary rather than a hint. Every path that reaches it — from the browser and
from the agent — is resolved and checked for containment before anything touches the disk, so a
traversing path is refused; an absolute path is normalised into the folder, matching how `EditTools`
treats agent-supplied paths, and the guarantee is that a write never lands outside. Uploads reuse
the attachment path's hardening (name sanitisation, an 8 MB ceiling, never overwriting), and
document extraction parses rather than executes. The parsers are an optional extra, so a base
install has no additional attack surface from formats it cannot read. Deleting a workspace removes
its entry and leaves the files alone unless the caller asks for both, which is a second explicit
decision because written work is not recoverable from a list.

Research inside a workspace goes through the same hardened web tool as everywhere else; the
workspace only records what came back, caching each page's text so a cited claim stays checkable.
Parallel researchers are read-only by construction — no edit tools in their surface and no write
scope in their roster — which is why several can run at once with nothing to arbitrate.

A change review is read-only in the same way. It resolves a diff through the same argv-only
``GitClient`` as everything else, never mutates the index or the working tree to produce one, and
its reviewers run with the QA tool surface — reads, search, and finish, with no edit, command, or
network tool between them. Findings are reported at the line the change introduced, and a credential
matched in a diff is masked in the report rather than reprinted, so a review shared with a colleague
does not become the leak.

QA sub-agents are read-only by construction. Their action schema exposes only repository reads,
search, globbing, directory listing, and finish operations, while `EditTools` also enforces
read-only mode underneath. Automated QA commands are selected by D[Ai]NO rather than the model and
still pass through the configured runtime and command policy. Registry-backed dependency audits
are grouped behind one explicit network approval; declining it records those checks as skipped and
sends no audit request.

Production deployment and rollback require `--approve`. D[Ai]NO never pushes or merges a mission.
Every mission begins with a worktree and archive checkpoint and records its initial commit as the
rollback point. Task and final commits stage only paths changed by successful model edit actions,
so test-generated caches, bytecode, coverage data, and other command side effects are not swept
into a commit.
