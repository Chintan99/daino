# Security

The main trust boundaries are the repository, model provider, execution runtime, secret store, Git
workspace, and deployment target.

The TUI exposes four explicit autonomy modes through `Shift+Tab` or `/mode`: Plan is read-only,
Ask prompts for approval-gated commands, Session grants those command approvals until a new
conversation starts, and Full also auto-approves mission execution/change gates. Full access does
not bypass hard-denied destructive-command rules, repository boundaries, or deployment safety
checks.

Configuration stores secret references, never values. `env://NAME`, `keyring://service/account`,
and `file://path` are resolved immediately before use. The Providers screen may accept a pasted
OpenRouter key, but validates it first and then writes it under `.vasuki/secrets` with user-only
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

QA sub-agents are read-only by construction. Their action schema exposes only repository reads,
search, globbing, directory listing, and finish operations, while `EditTools` also enforces
read-only mode underneath. Automated QA commands are selected by Vasuki rather than the model and
still pass through the configured runtime and command policy. Registry-backed dependency audits
are grouped behind one explicit network approval; declining it records those checks as skipped and
sends no audit request.

Production deployment and rollback require `--approve`. Vasuki never pushes or merges a mission.
Every mission begins with a worktree and archive checkpoint and records its initial commit as the
rollback point. Task and final commits stage only paths changed by successful model edit actions,
so test-generated caches, bytecode, coverage data, and other command side effects are not swept
into a commit.
