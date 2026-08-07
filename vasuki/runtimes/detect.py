"""Pick a runtime the machine can actually use.

Defaulting to Docker looked like the safe choice — commands run isolated from the
host — but a default is only safe if it works. Docker is frequently absent, or
installed with a socket the user cannot reach because they are not in the
``docker`` group. In that state every command the agent runs fails, which does
not read as "no container runtime": it reads as the agent being broken.

So the runtime is probed once at initialization and the answer is written into
the project's configuration, where the user can see and change it.
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404

#: Long enough for a healthy daemon to answer, short enough that a dead socket
#: does not stall project initialization.
PROBE_TIMEOUT_SECONDS = 5


def docker_status() -> tuple[bool, str]:
    """Return whether Docker is usable, and why not when it is not.

    Checks the daemon rather than the binary. ``docker`` being on PATH says
    nothing about whether this user can talk to it.
    """
    if shutil.which("docker") is None:
        return False, "Docker is not installed."
    try:
        result = subprocess.run(  # nosec B603, B607
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Docker did not respond: {exc}"
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout).strip().splitlines()
    reason = detail[-1] if detail else "the Docker daemon did not respond"
    if "permission denied" in reason.lower():
        reason += " — add yourself to the docker group: sudo usermod -aG docker $USER"
    return False, reason


def preferred_runtime() -> str:
    """The runtime to configure for a new project: Docker when it works, else local."""
    usable, _ = docker_status()
    return "docker" if usable else "local"
