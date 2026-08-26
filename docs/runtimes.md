# Runtimes

`Runtime` exposes prepare, execute, file transfer, service control, inspection, checkpoints, and
cleanup.

- `LocalRuntime` is the default for new projects. It runs policy-gated argument vectors without a
  shell and exposes the host's complete language toolchain.
- `DockerRuntime` mounts only the mission workspace, applies CPU/memory/time limits, disables the
  network by default, runs as the invoking UID/GID, and removes containers automatically. Ordinary
  commands run in the configured image; commands whose executable is `docker` run through the host
  client because nesting them in the sandbox would hide the daemon and fail with `docker: not
  found`.
- `RemoteSSHRuntime` uses AsyncSSH, agent/key-path authentication, host verification, timeouts,
  SFTP, output redaction, and a complete command result.

Local and Docker coding runtimes put the mission worktree's `src` directory first on
`PYTHONPATH`. This prevents an editable environment created for the original checkout from testing
stale code instead of the isolated mission. A missing executable is returned as a normal exit-127
command result, allowing verification and repair to report the prerequisite rather than crashing.
For a multi-language Compose repository, prefer local mode or validate the stack with `docker
compose`; a fixed Python sandbox image is not assumed to contain Node, Go, Rust, or project-specific
tools.

Set the coding runtime with:

```bash
daino config set runtime.default docker
daino config set runtime.docker_image your-project-test-image
daino config set runtime.network_access restricted
```
