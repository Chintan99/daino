# Security

The main trust boundaries are the repository, model provider, execution runtime, secret store, Git
workspace, and deployment target.

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

Local commands are tokenized with `shlex` and executed without a shell. Docker commands run in an
ephemeral container with CPU/memory limits, a workspace-only mount, timeout, automatic cleanup, and
network disabled by default. Remote inspection uses a fixed read-only command inventory. SSH host
key verification uses AsyncSSH's known-host behavior or an explicit `known_hosts` path.

Production deployment and rollback require `--approve`. Vasuki never pushes or merges a mission.
Every mission begins with a worktree and archive checkpoint and records its initial commit as the
rollback point.
