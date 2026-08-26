"""Default-deny policy gates for commands and sensitive operations."""

from __future__ import annotations

import re
import shlex
from enum import StrEnum

from pydantic import BaseModel, Field

from daino.config.models import SecurityConfig


class Permission(StrEnum):
    READ_REPOSITORY = "read_repository"
    MODIFY_REPOSITORY = "modify_repository"
    RUN_LOCAL_COMMAND = "run_local_command"
    RUN_CONTAINER_COMMAND = "run_container_command"
    INSTALL_DEPENDENCY = "install_dependency"
    ACCESS_NETWORK = "access_network"
    ACCESS_SECRET = "access_secret"  # nosec B105
    RUN_DATABASE_MIGRATION = "run_database_migration"
    DEPLOY_DEVELOPMENT = "deploy_development"
    DEPLOY_PRODUCTION = "deploy_production"
    MODIFY_FIREWALL = "modify_firewall"
    MODIFY_REVERSE_PROXY = "modify_reverse_proxy"
    DELETE_RESOURCE = "delete_resource"
    ROLLBACK_RELEASE = "rollback_release"


class PolicyDecision(BaseModel):
    allowed: bool
    requires_approval: bool = False
    reasons: list[str] = Field(default_factory=list)
    permission: Permission


#: Operators that only mean anything to a shell. Commands are executed directly,
#: never through one, so these would be handed to the program as plain arguments.
SHELL_OPERATORS = frozenset({"|", "||", "&&", "&", ";", ">", ">>", "<", "<<"})

DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(^|\s)rm\s+(-\S*r\S*f|-\S*f\S*r)\b"), "recursive forced deletion"),
    (re.compile(r"(^|\s)(mkfs|shutdown|reboot)\b"), "host-destructive command"),
    (re.compile(r"\bDROP\s+(DATABASE|TABLE|SCHEMA)\b", re.I), "destructive database statement"),
    (re.compile(r"\bdocker\s+system\s+prune\b"), "global Docker cleanup"),
    (re.compile(r"(^|\s)(chmod|chown)\s+.*-[rR]\b"), "recursive permission change"),
    (re.compile(r"\b(iptables|nft)\s+(-F|flush)\b"), "firewall flush"),
    (re.compile(r"\b(terraform|tofu)\s+destroy\b"), "infrastructure destruction"),
)

INSTALLERS = {"pip", "pip3", "uv", "poetry", "npm", "pnpm", "yarn", "apt", "apt-get"}
NETWORK_TOOLS = {"curl", "wget", "ssh", "scp", "rsync", "nc", "ncat"}

_DOCKER_SAFE_VERBS = {
    "context",
    "diff",
    "events",
    "history",
    "images",
    "info",
    "inspect",
    "logs",
    "port",
    "ps",
    "stats",
    "top",
    "version",
}
_DOCKER_RESOURCE_SAFE_VERBS = {
    "context": {"inspect", "list", "ls", "show"},
    "container": {"diff", "inspect", "list", "logs", "ls", "port", "stats", "top"},
    "image": {"history", "inspect", "list", "ls"},
    "network": {"inspect", "list", "ls"},
    "system": {"df", "info"},
    "volume": {"inspect", "list", "ls"},
}
_COMPOSE_SAFE_VERBS = {"config", "images", "list", "ls", "logs", "port", "ps", "top", "version"}
_DOCKER_GLOBAL_OPTIONS_WITH_VALUE = {"--config", "--context", "--host", "--log-level", "-H"}
_COMPOSE_OPTIONS_WITH_VALUE = {
    "--ansi",
    "--env-file",
    "--file",
    "--parallel",
    "--profile",
    "--progress",
    "--project-directory",
    "--project-name",
    "-f",
    "-p",
}


def _first_command(arguments: list[str], options_with_value: set[str]) -> tuple[str, int]:
    """Return the first non-option command and its index."""
    skip_value = False
    for index, argument in enumerate(arguments):
        if skip_value:
            skip_value = False
            continue
        option = argument.split("=", 1)[0]
        if option in options_with_value and "=" not in argument:
            skip_value = True
            continue
        if argument.startswith("-"):
            continue
        return argument, index
    return "", -1


def docker_command_is_read_only(tokens: list[str]) -> bool:
    """Classify Docker daemon operations that are safe to repeat unattended."""
    if not tokens or tokens[0].rsplit("/", 1)[-1] != "docker":
        return False
    command, index = _first_command(tokens[1:], _DOCKER_GLOBAL_OPTIONS_WITH_VALUE)
    if not command:
        return False
    remaining = tokens[index + 2 :]
    if command == "compose":
        verb, _ = _first_command(remaining, _COMPOSE_OPTIONS_WITH_VALUE)
        return verb in _COMPOSE_SAFE_VERBS
    if command in _DOCKER_RESOURCE_SAFE_VERBS:
        verb, _ = _first_command(remaining, set())
        return verb in _DOCKER_RESOURCE_SAFE_VERBS[command]
    return command in _DOCKER_SAFE_VERBS


class PolicyEngine:
    """Evaluates permissions without executing commands."""

    def __init__(self, config: SecurityConfig | None = None) -> None:
        self.config = config or SecurityConfig()

    def command_decision(
        self, command: str, *, runtime: str = "local", approved: bool = False
    ) -> PolicyDecision:
        permission = (
            Permission.RUN_CONTAINER_COMMAND
            if runtime == "docker"
            else Permission.RUN_LOCAL_COMMAND
        )
        reasons: list[str] = []
        for pattern, reason in DANGEROUS_PATTERNS:
            if pattern.search(command):
                return PolicyDecision(
                    allowed=approved,
                    requires_approval=not approved,
                    reasons=[reason],
                    permission=Permission.DELETE_RESOURCE,
                )
        try:
            tokens = shlex.split(command)
        except ValueError:
            return PolicyDecision(
                allowed=False,
                reasons=["malformed shell command"],
                permission=permission,
            )
        # Commands run without a shell, so metacharacters are passed through as
        # literal arguments and misbehave in confusing ways (`a | head` becomes
        # grep looking for a file called "head"). Say so plainly instead.
        shell_tokens = sorted({token for token in tokens if token in SHELL_OPERATORS})
        if shell_tokens:
            return PolicyDecision(
                allowed=False,
                reasons=[
                    f"shell syntax is not available: {', '.join(shell_tokens)}. "
                    "Use a single command with arguments, or a script file"
                ],
                permission=permission,
            )
        executable = tokens[0].rsplit("/", 1)[-1] if tokens else ""
        if executable in self.config.denied_commands:
            return PolicyDecision(
                allowed=False,
                reasons=["command explicitly denied by project policy"],
                permission=permission,
            )
        if executable in INSTALLERS and any(verb in tokens for verb in ("install", "add", "sync")):
            permission = Permission.INSTALL_DEPENDENCY
            if self.config.require_approval_for_install and not approved:
                reasons.append("dependency installation requires approval")
        if executable in NETWORK_TOOLS:
            permission = Permission.ACCESS_NETWORK
            if self.config.require_approval_for_network and not approved:
                reasons.append("network access requires approval")
        if executable == "docker" and not docker_command_is_read_only(tokens):
            permission = Permission.RUN_CONTAINER_COMMAND
            if not approved:
                reasons.append("a host Docker mutation requires approval")
        return PolicyDecision(
            allowed=not reasons,
            requires_approval=bool(reasons),
            reasons=reasons,
            permission=permission,
        )

    def deployment_decision(self, environment: str, approved: bool) -> PolicyDecision:
        production = environment.lower() in {"production", "prod"}
        permission = Permission.DEPLOY_PRODUCTION if production else Permission.DEPLOY_DEVELOPMENT
        required = production and self.config.require_approval_for_production
        return PolicyDecision(
            allowed=approved or not required,
            requires_approval=required and not approved,
            reasons=["production deployment requires explicit approval"]
            if required and not approved
            else [],
            permission=permission,
        )
