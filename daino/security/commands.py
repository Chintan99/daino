"""Decide whether the agent may run a command, ask first, or not at all.

``PolicyEngine`` answers the fixed question — is this command destructive, an
installer, a network tool. This adds the two things an interactive agent needs on
top: a set of commands routine enough to run without interrupting the user, and a
memory of what the user already allowed in this session.

The three outcomes are deliberately distinct. ``DENY`` is not promptable: a
recursive delete stays refused however the conversation goes. ``ASK`` is the only
path that reaches the user, and an answer of "always" is remembered so the same
command never asks twice.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from enum import StrEnum

from daino.config.models import SecurityConfig
from daino.security.policy import PolicyDecision, PolicyEngine, docker_command_is_read_only

#: Executables routine enough to run unattended. They read, compile, test, or
#: report; none of them install packages, reach the network, or mutate anything
#: outside the workspace. A project can extend this through
#: ``security.allowed_commands`` and narrow it through ``denied_commands``.
#:
#: ``python`` and ``node`` are here deliberately: running the project's own code
#: is the point of a coding agent, and refusing it would make the shell useless
#: for the tests it exists to run. The destructive-pattern denylist still applies
#: to whatever they are asked to do.
#:
#: ``env`` is deliberately *not* here. It was, and a bare ``env`` prints the whole
#: process environment — every API key, session token, and cloud credential the
#: user happened to have exported — into a transcript that is persisted, shown in
#: two clients, and sent back to the model as context on the next turn. ``redact``
#: masks the secrets Daino itself manages and cannot mask the ones it has never
#: seen, so the dump had to stop being unattended rather than be cleaned up after.
#: The common legitimate use, ``env VAR=value pytest``, still runs without asking:
#: :func:`unwrap_env_prefix` judges the program ``env`` would exec instead.
DEFAULT_SAFE_COMMANDS = frozenset(
    {
        # Test, lint, type-check, audit.
        "pytest",
        "tox",
        "nox",
        "unittest",
        "ruff",
        "flake8",
        "black",
        "isort",
        "mypy",
        "pyright",
        "bandit",
        "eslint",
        "prettier",
        "tsc",
        "jest",
        "vitest",
        # Language and build runners.
        "python",
        "python3",
        "node",
        "deno",
        "bun",
        "go",
        "cargo",
        "javac",
        "java",
        "make",
        "just",
        "task",
        # Read-only inspection.
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "file",
        "stat",
        "du",
        "df",
        "find",
        "grep",
        "rg",
        "fd",
        "tree",
        "which",
        "echo",
        "pwd",
        "date",
        "uname",
        "whoami",
        # Package managers in their read-only modes are handled below by verb.
        "git",
        "docker",
    }
)

#: Git subcommands that only read. Anything else (push, reset, clean) is asked
#: about, because it can discard work or publish it.
SAFE_GIT_VERBS = frozenset(
    {
        "status",
        "diff",
        "log",
        "show",
        "branch",
        "remote",
        "rev-parse",
        "describe",
        "blame",
        "ls-files",
    }
)


#: ``env`` options that take a separate value, so the unwrapper can skip past
#: them to find the program. ``-S``/``--split-string`` is absent on purpose: it
#: re-parses its argument as a whole command line, which is a second layer of
#: quoting this must not try to reason about.
_ENV_OPTIONS_WITH_VALUE = frozenset({"-u", "--unset", "-C", "--chdir"})
#: ``env`` flags that take no value and do not change what program runs.
_ENV_FLAGS = frozenset({"-i", "--ignore-environment", "-0", "--null", "-v", "--debug"})


def unwrap_env_prefix(tokens: list[str]) -> list[str]:
    """Return the command ``env`` would actually run, or ``[]`` if it runs none.

    ``env FOO=bar pytest -q`` is an ordinary way to run a test suite, and judging
    it as "the ``env`` command" would either allow an environment dump or start
    asking about every parameterised test run. So the prefix is peeled off and
    the real executable is what the allowlist sees.

    An empty result means there is no program — a bare ``env``, or ``env`` with
    only assignments — which is the dump this exists to catch. Anything with an
    option this does not model conservatively returns empty too, so an unfamiliar
    flag falls through to "ask" rather than to "allow".
    """
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if token in _ENV_OPTIONS_WITH_VALUE:
            index += 2
            continue
        if token in _ENV_FLAGS:
            index += 1
            continue
        if token.startswith("-"):
            # Includes -S/--split-string and anything a future coreutils adds.
            return []
        if "=" in token and not token.startswith("="):
            index += 1
            continue
        break
    return tokens[index:]


class Verdict(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class CommandDecision:
    verdict: Verdict
    reason: str = ""
    #: The key an "always" answer is remembered under.
    signature: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


@dataclass
class CommandGate:
    """Policy plus session memory for one conversation."""

    config: SecurityConfig = field(default_factory=SecurityConfig)
    #: Command signatures the user answered "always" to.
    remembered: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.policy = PolicyEngine(self.config)

    @staticmethod
    def signature(command: str) -> str:
        """Identify a command for approval memory.

        Keyed on the executable and, for a subcommand-style tool, its first verb:
        approving ``pip install httpx`` should not silently approve ``pip
        uninstall``, but it should cover installing the next package.

        An ``env`` prefix is peeled off for the same reason the allowlist peels
        it off: the thing the user is being asked to approve is the program, and
        keying on ``env`` would let one approval cover every program run through
        it.
        """
        try:
            tokens = shlex.split(command)
        except ValueError:
            return command.strip()[:60]
        if not tokens:
            return ""
        if tokens[0].rsplit("/", 1)[-1] == "env":
            tokens = unwrap_env_prefix(tokens) or ["env"]
        executable = tokens[0].rsplit("/", 1)[-1]
        verb = next((token for token in tokens[1:] if not token.startswith("-")), "")
        return f"{executable} {verb}".strip() if verb else executable

    def _executable(self, command: str) -> tuple[str, list[str]]:
        """The program this command runs, with any ``env`` prefix peeled off."""
        try:
            tokens = shlex.split(command)
        except ValueError:
            return "", []
        if not tokens:
            return "", []
        if tokens[0].rsplit("/", 1)[-1] == "env":
            tokens = unwrap_env_prefix(tokens)
            if not tokens:
                # A bare environment dump. Reported as ``env`` so the reason the
                # user is asked names the command they actually typed.
                return "env", ["env"]
        return tokens[0].rsplit("/", 1)[-1], tokens

    def _is_safe(self, command: str) -> bool:
        executable, tokens = self._executable(command)
        if not executable or executable in self.config.denied_commands:
            return False
        if self._literal_executable(command) in self.config.denied_commands:
            # A project that denied ``env`` meant to deny it, and unwrapping the
            # prefix must not become the way around its own denylist.
            return False
        if executable in self.config.allowed_commands:
            return True
        if executable not in DEFAULT_SAFE_COMMANDS:
            return False
        if executable == "git":
            verb = next((token for token in tokens[1:] if not token.startswith("-")), "")
            return verb in SAFE_GIT_VERBS
        if executable == "docker":
            return docker_command_is_read_only(tokens)
        return True

    @staticmethod
    def _literal_executable(command: str) -> str:
        """The first token as typed, before any ``env`` prefix is peeled off."""
        try:
            tokens = shlex.split(command)
        except ValueError:
            return ""
        return tokens[0].rsplit("/", 1)[-1] if tokens else ""

    def decide(self, command: str, *, runtime: str = "local") -> CommandDecision:
        decision: PolicyDecision = self.policy.command_decision(command, runtime=runtime)
        signature = self.signature(command)
        # A hard policy refusal is never promptable. Destructive patterns come
        # back as requires_approval so a human operator can override them
        # elsewhere; from a chat agent they are simply refused.
        if not decision.allowed and not decision.requires_approval:
            return CommandDecision(Verdict.DENY, "; ".join(decision.reasons), signature)
        if decision.requires_approval and _is_destructive(decision):
            return CommandDecision(
                Verdict.DENY,
                "; ".join(decision.reasons) or "destructive command",
                signature,
            )
        if self._is_safe(command) and decision.allowed:
            return CommandDecision(Verdict.ALLOW, "", signature)
        if signature and signature in self.remembered:
            return CommandDecision(Verdict.ALLOW, "approved earlier this session", signature)
        reason = "; ".join(decision.reasons) or "not in the allowlist for unattended commands"
        return CommandDecision(Verdict.ASK, reason, signature)

    def remember(self, signature: str) -> None:
        if signature:
            self.remembered.add(signature)


def _is_destructive(decision: PolicyDecision) -> bool:
    from daino.security.policy import Permission

    return decision.permission is Permission.DELETE_RESOURCE
