# Runtimes

`Runtime` exposes prepare, execute, file transfer, service control, inspection, checkpoints, and
cleanup.

- `LocalRuntime` is intended for explicit local checks. It runs argument vectors without a shell.
- `DockerRuntime` mounts only the mission workspace, applies CPU/memory/time limits, disables the
  network by default, and removes containers automatically.
- `RemoteSSHRuntime` uses AsyncSSH, agent/key-path authentication, host verification, timeouts,
  SFTP, output redaction, and a complete command result.

Set the coding runtime with:

```bash
vasuki config set runtime.default docker
vasuki config set runtime.docker_image your-project-test-image
vasuki config set runtime.network_access restricted
```
