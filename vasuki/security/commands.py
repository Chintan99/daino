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

from vasuki.config.models import SecurityConfig
from vasuki.security.policy import PolicyDecision, PolicyEngine

#: Executables routine enough to run unattended. They read, compile, test, or
#: report; none of them install packages, reach the network, or mutate anything
#: outside the workspace. A project can extend this through
#: ``security.allowed_commands`` and narrow it through ``denied_commands``.
#:
#: ``python`` and ``node`` are here deliberately: running the project's own code
#: is the point of a coding agent, and refusing it would make the shell useless
#: for the tests it exists to run. The destructive-pattern denylist still applies
#: to whatever they are asked to do.
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
        "env",
        "date",
        "uname",
        "whoami",
        # Package managers in their read-only modes are handled below by verb.
        "git",
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
        """
        try:
            tokens = shlex.split(command)
        except ValueError:
            return command.strip()[:60]
        if not tokens:
            return ""
        executable = tokens[0].rsplit("/", 1)[-1]
        verb = next((token for token in tokens[1:] if not token.startswith("-")), "")
        return f"{executable} {verb}".strip() if verb else executable

    def _executable(self, command: str) -> tuple[str, list[str]]:
        try:
            tokens = shlex.split(command)
        except ValueError:
            return "", []
        return (tokens[0].rsplit("/", 1)[-1] if tokens else ""), tokens

    def _is_safe(self, command: str) -> bool:
        executable, tokens = self._executable(command)
        if not executable or executable in self.config.denied_commands:
            return False
        if executable in self.config.allowed_commands:
            return True
        if executable not in DEFAULT_SAFE_COMMANDS:
            return False
        if executable == "git":
            verb = next((token for token in tokens[1:] if not token.startswith("-")), "")
            return verb in SAFE_GIT_VERBS
        return True

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
    from vasuki.security.policy import Permission

    return decision.permission is Permission.DELETE_RESOURCE
