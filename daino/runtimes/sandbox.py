"""A middle tier between "raw subprocess" and "the whole thing in Docker".

The two existing runtimes are an all-or-nothing choice. ``local`` runs the
agent's commands as a plain subprocess inheriting the user's entire environment —
every exported API key, cloud credential and session token — with unrestricted
filesystem and network access. ``docker`` fixes all of that and costs an image
that has to contain the project's whole toolchain, which for a Node or Go project
means the default image is wrong and the user has to go build one. So in practice
people run ``local``, and the sandbox is theoretical.

This tier keeps the host toolchain and removes the parts of host access an agent
has no business having:

* **A scrubbed environment.** Only variables the toolchain needs are passed
  through. This is the one that matters, and it works everywhere: it is pure
  Python, needs nothing installed, and closes the hole that made a bare ``env``
  dangerous in the first place.
* **OS-level confinement when the platform offers it.** macOS has ``sandbox-exec``
  and Linux has ``bubblewrap``; where one is present the command is additionally
  confined to the project directory, and optionally denied the network. Where
  neither is present the environment scrub still applies, and
  :meth:`SandboxedLocalRuntime.confinement` reports honestly which of the two
  levels is in force — a sandbox that quietly degrades to nothing is worse than
  no sandbox, because the user stops checking.

What this is not: a security boundary against a determined adversary. A process
running as the user can still reach the user's files if it works at it, and
``sandbox-exec`` is deprecated by Apple. It is a boundary against the realistic
failure — a model that runs the wrong command, or a dependency's install script
that reads the environment — and against that it is worth having.
"""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from daino.runtimes.local import LocalRuntime
from daino.schemas import CommandResult
from daino.security import PolicyEngine

#: Environment variables every toolchain needs to function at all. Anything not
#: here is dropped, so the list is what the agent's commands can see about the
#: machine — deliberately short, and deliberately containing no credentials.
BASE_ENVIRONMENT_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "TZ",
        "PWD",
        # Windows needs these to resolve anything at all.
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
    }
)

#: Toolchain configuration that is not a credential: interpreter and build
#: settings a project's tests genuinely need. Kept separate from the base set so
#: the reason each group is present stays legible.
TOOLCHAIN_ENVIRONMENT_KEYS = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "NODE_PATH",
        "NVM_DIR",
        "NPM_CONFIG_PREFIX",
        "GOPATH",
        "GOROOT",
        "GOCACHE",
        "GOMODCACHE",
        "CARGO_HOME",
        "RUSTUP_HOME",
        "JAVA_HOME",
        "MAVEN_HOME",
        "GRADLE_USER_HOME",
        "DOTNET_ROOT",
        "CI",
    }
)

#: Names that are credentials whatever else they look like. Applied *after* the
#: allowlist as a second check, so a project that widens ``passthrough_env``
#: cannot accidentally re-admit its own secrets.
CREDENTIAL_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "SESSION",
    "COOKIE",
    "PRIVATE",
)


def scrub_environment(
    source: dict[str, str] | None = None,
    *,
    passthrough: frozenset[str] | set[str] = frozenset(),
) -> dict[str, str]:
    """The environment an agent command should see, and nothing more.

    Allowlist rather than denylist, because the set of things worth hiding is
    open-ended — every service the user has ever exported a token for — while the
    set of things a build needs is small and knowable.
    """
    environment = dict(source if source is not None else os.environ)
    allowed = BASE_ENVIRONMENT_KEYS | TOOLCHAIN_ENVIRONMENT_KEYS | set(passthrough)
    return {
        key: value
        for key, value in environment.items()
        if key in allowed and not _looks_like_a_credential(key)
    }


def _looks_like_a_credential(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in CREDENTIAL_MARKERS)


@dataclass(frozen=True, slots=True)
class Confinement:
    """Which sandbox is actually in force, and why it is not a stronger one."""

    #: "sandbox-exec", "bubblewrap", or "" when only the environment is scrubbed.
    mechanism: str
    #: Whether writes outside the project root are blocked by the OS.
    filesystem: bool
    #: Whether outbound network access is blocked by the OS.
    network: bool
    #: Present when a stronger mechanism was wanted and could not be used.
    reason: str = ""

    @property
    def describe(self) -> str:
        if not self.mechanism:
            return (
                "environment-only: the process environment is scrubbed of credentials, but "
                f"filesystem and network access are unrestricted ({self.reason})"
            )
        parts = ["scrubbed environment"]
        if self.filesystem:
            parts.append("writes confined to the project")
        if self.network:
            parts.append("network denied")
        return f"{self.mechanism}: " + ", ".join(parts)


def available_mechanism() -> tuple[str, str]:
    """The strongest OS sandbox on this machine, and why it is not stronger."""
    if shutil.which("sandbox-exec"):
        return "sandbox-exec", ""
    if shutil.which("bwrap"):
        return "bubblewrap", ""
    if os.name == "nt":
        return "", "no OS sandbox is available on Windows"
    return "", "install bubblewrap (Linux) for filesystem and network confinement"


class SandboxedLocalRuntime(LocalRuntime):
    """Host toolchain, scrubbed environment, and OS confinement where possible."""

    def __init__(
        self,
        root: Path,
        policy: PolicyEngine | None = None,
        *,
        timeout: int = 600,
        allow_absolute_paths: bool = False,
        network_access: bool = False,
        passthrough_env: frozenset[str] | set[str] = frozenset(),
        mechanism: str | None = None,
    ) -> None:
        super().__init__(
            root,
            policy,
            timeout=timeout,
            allow_absolute_paths=allow_absolute_paths,
        )
        #: Most agent commands are tests and builds that need nothing outbound.
        #: A project whose tests hit a local service turns this on explicitly.
        self.network_access = network_access
        #: Extra variables this project's toolchain needs. Still filtered for
        #: credential-shaped names, so widening it cannot leak a token.
        self.passthrough_env = frozenset(passthrough_env)
        resolved, reason = available_mechanism()
        #: Overridable so a test can pin the mechanism instead of depending on
        #: what happens to be installed on the machine running the suite.
        self.mechanism = resolved if mechanism is None else mechanism
        self._mechanism_reason = reason if self.mechanism == resolved else ""

    def confinement(self) -> Confinement:
        """What this runtime is actually enforcing right now."""
        if not self.mechanism:
            return Confinement(
                mechanism="",
                filesystem=False,
                network=False,
                reason=self._mechanism_reason or "no OS sandbox available",
            )
        return Confinement(
            mechanism=self.mechanism,
            filesystem=True,
            network=not self.network_access,
        )

    def environment(self) -> dict[str, str]:
        """The scrubbed environment, plus the project's source path."""
        environment = scrub_environment(passthrough=self.passthrough_env)
        environment.setdefault("HOME", str(Path.home()))
        environment["PWD"] = str(self.root)
        source_root = self.root / "src"
        if source_root.is_dir():
            existing = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                item for item in (str(source_root), existing) if item
            )
        return environment

    def wrap(self, arguments: list[str]) -> list[str]:
        """Prefix a command with the OS sandbox, when there is one to use."""
        if self.mechanism == "sandbox-exec":
            return [
                "sandbox-exec",
                "-p",
                _seatbelt_profile(self.root, network=self.network_access),
                *arguments,
            ]
        if self.mechanism == "bubblewrap":
            return [*_bwrap_arguments(self.root, network=self.network_access), *arguments]
        return arguments

    async def execute(
        self, command: str, *, timeout: int | None = None, approved: bool = False
    ) -> CommandResult:
        """Run one command under the sandbox, reporting a missing one honestly."""
        result = await super().execute(command, timeout=timeout, approved=approved)
        if result.exit_code == 127 and self.mechanism:
            # ``sandbox-exec``/``bwrap`` itself being missing and the *command*
            # being missing both surface as 127, and they need opposite fixes.
            result.stderr += (
                f"\n(Ran under {self.mechanism}. If the executable exists on this machine, the "
                "sandbox may be blocking it — switch to /runtime local to check.)"
            )
        return result

    async def inspect(self) -> dict[str, Any]:
        base = await super().inspect()
        confinement = self.confinement()
        return {
            **base,
            "type": "sandbox",
            "mechanism": confinement.mechanism,
            "filesystem_confined": confinement.filesystem,
            "network_denied": confinement.network,
            "confinement": confinement.describe,
            "environment_keys": sorted(self.environment()),
        }


def _seatbelt_profile(root: Path, *, network: bool) -> str:
    """A macOS sandbox profile: read anywhere, write only inside the project.

    Reads stay open because a build reads the toolchain, the standard library and
    the package cache, all of which live outside the project. Writes are the
    thing worth confining, and are.
    """
    resolved = str(root.resolve())
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* (subpath "{resolved}"))',
        '(allow file-write* (subpath "/private/tmp"))',
        '(allow file-write* (subpath "/private/var/folders"))',
        '(allow file-write* (literal "/dev/null") (literal "/dev/stdout")'
        ' (literal "/dev/stderr"))',
    ]
    if not network:
        rules.append("(deny network*)")
    return "\n".join(rules)


def _bwrap_arguments(root: Path, *, network: bool) -> list[str]:
    """Bubblewrap flags: the host filesystem read-only, the project writable."""
    resolved = str(root.resolve())
    arguments = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--bind",
        resolved,
        resolved,
        # A build needs a writable temp directory, and this is the sandbox
        # granting one rather than a program choosing an insecure path.
        "--bind",
        "/tmp",  # nosec B108  # noqa: S108
        "/tmp",  # nosec B108  # noqa: S108
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--chdir",
        resolved,
        # Without this a killed sandbox leaves the command orphaned.
        "--die-with-parent",
    ]
    if not network:
        arguments.append("--unshare-net")
    return arguments


def describe_command(runtime: SandboxedLocalRuntime, command: str) -> str:
    """The exact argv the sandbox would run, for diagnostics and tests."""
    return shlex.join(runtime.wrap(shlex.split(command)))
