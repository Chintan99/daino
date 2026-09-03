# Runtimes

`Runtime` exposes prepare, execute, file transfer, service control, inspection, checkpoints, and
cleanup. Coding turns and missions use either the local or Docker runtime.

- `LocalRuntime` is the default for new projects. It runs policy-gated argument vectors without a
  shell and exposes the host's complete language toolchain. It is the *unsandboxed* tier: commands
  inherit your entire environment, including every credential you have exported.
- `SandboxedLocalRuntime` is the middle tier — see [Sandboxed local mode](#sandboxed-local-mode).
  Host toolchain, scrubbed environment, and OS-level confinement where the platform provides one.
- `DockerRuntime` mounts only the mission workspace, applies CPU/memory/time limits, disables the
  network by default, runs as the invoking UID/GID, and removes containers automatically. Ordinary
  commands run in the configured image; commands whose executable is `docker` run through the host
  client because nesting them in the sandbox would hide the daemon and fail with `docker: not
  found`.
- `RemoteSSHRuntime` uses AsyncSSH, agent/key-path authentication, host verification, timeouts,
  SFTP, output redaction, and a complete command result for deployment operations. SSH is not a
  coding-mission runtime; source changes run locally or in Docker before deployment.

Local and Docker coding runtimes put the mission worktree's `src` directory first on
`PYTHONPATH`. This prevents an editable environment created for the original checkout from testing
stale code instead of the isolated mission. A missing executable is returned as a normal exit-127
command result, allowing verification and repair to report the prerequisite rather than crashing.
For a multi-language Compose repository, prefer local mode or validate the stack with `docker
compose`; a fixed Python sandbox image is not assumed to contain Node, Go, Rust, or project-specific
tools.

Set the coding runtime with:

```bash
daino config set runtime.default local    # host toolchain, no isolation
daino config set runtime.default sandbox  # host toolchain, scrubbed environment
daino config set runtime.default docker   # isolated container
daino config set runtime.docker_image your-project-test-image
daino config set runtime.network_access restricted
```

`daino init` probes the Docker daemon once. It records Docker when the daemon is reachable and
falls back to local execution otherwise. Confirm the active prerequisites with:

```bash
daino doctor
daino config show
```

Both coding runtimes enforce the same command policy and timeout. Docker adds isolation; local mode
has immediate access to the repository's installed language toolchains. Configure an image that
contains your project's tools before selecting Docker for Node.js, Go, Rust, or polyglot checks.

## Sandboxed local mode

`local` and `docker` are an all-or-nothing choice, and the practical consequence was that people
who could not build an image with their project's toolchain ended up on `local` with no isolation
at all. `sandbox` is the tier between them:

- **The environment is scrubbed.** Commands see an allowlist — `PATH`, `HOME`, the interpreter and
  build variables a toolchain needs — and nothing else. Every API key, cloud credential and session
  token you have exported is absent. This is the part that works everywhere: it is pure Python and
  needs nothing installed.
- **The filesystem and network are confined where the platform allows it.** On macOS through
  `sandbox-exec`, on Linux through `bubblewrap`: reads stay open (a build reads the toolchain and
  the package cache), writes are limited to the project directory, and outbound network is denied
  unless `runtime.network_access` is `allowed`.

Where neither mechanism is installed, the environment scrub still applies and D[Ai]NO says so
rather than implying an isolation it is not providing:

```bash
daino config set runtime.default sandbox
daino doctor        # reports the mechanism actually in force
```

A project whose tests genuinely need an extra variable can list it:

```yaml
runtime:
  default: sandbox
  sandbox_passthrough_env: [MY_BUILD_FLAG]
```

Credential-shaped names are filtered even here, so widening the list cannot re-admit a token by
accident.

This is a boundary against the realistic failure — a model running the wrong command, a dependency's
install script reading the environment — not against a determined adversary. A process running as
you can still reach your files if it works at it, and Apple has deprecated `sandbox-exec`. For a
real boundary, use Docker.

Remote Compose targets are configured separately under `deployment.targets`; see
[Deployment](deployment.md).
