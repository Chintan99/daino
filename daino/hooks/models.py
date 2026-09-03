"""What a hook is, and what it is allowed to say back.

Hooks are the escape hatch for everything Daino will never build in: run the
project's formatter after every edit, refuse edits to a generated directory,
post to Slack when a long mission finishes, block a command that a team policy
forbids. None of that belongs in the agent, and all of it is a two-line shell
script when there is somewhere to put it.

The contract is deliberately the one Claude Code uses, because that is the
convention people already have scripts for: JSON on stdin, an exit code that
means allow or block, and an optional JSON object on stdout for the cases where
an exit code is not expressive enough.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class HookEvent(StrEnum):
    """Points in a session where a hook may run."""

    #: A conversation session was opened.
    SESSION_START = "session_start"
    #: The user submitted a turn, before any context is built. May inject
    #: context, or refuse the turn outright.
    USER_PROMPT_SUBMIT = "user_prompt_submit"
    #: An action has been validated and is about to run. The only event that can
    #: stop work from happening.
    PRE_TOOL_USE = "pre_tool_use"
    #: An action finished. Can add feedback the model sees in its observation —
    #: which is how "auto-format on edit, then tell the agent what changed"
    #: works without the agent needing to know a formatter exists.
    POST_TOOL_USE = "post_tool_use"
    #: The agent needs the user: an approval prompt, or a long idle wait.
    NOTIFICATION = "notification"
    #: A turn finished.
    STOP = "stop"
    #: The session is closing.
    SESSION_END = "session_end"


#: Events where a hook's verdict can prevent something from happening. Everywhere
#: else a "deny" is recorded and ignored, because there is nothing left to stop.
BLOCKING_EVENTS = frozenset({HookEvent.PRE_TOOL_USE, HookEvent.USER_PROMPT_SUBMIT})


class HookDefinition(BaseModel):
    """One command to run, and when."""

    #: Shell command. A shell rather than an argv list on purpose: a hook is the
    #: user's own script, and pipes and redirects are most of what makes a
    #: one-line hook worth writing. See :mod:`daino.hooks.runner` for why that is
    #: safe here and not in the agent's own command path.
    command: str
    #: Regular expression matched against the tool name, fully anchored. Empty
    #: matches every tool. Ignored by events that have no tool.
    matcher: str = ""
    #: Seconds before the hook is killed. A hook that hangs must not hang the
    #: agent, so this is a hard ceiling rather than advice.
    timeout: float = Field(default=30.0, gt=0, le=600)
    #: Shown in logs and in the observation a blocking hook produces, so the
    #: model and the user can tell which hook spoke.
    name: str = ""
    #: Set false to keep a hook configured but inert.
    enabled: bool = True

    @property
    def label(self) -> str:
        return self.name or self.command.split()[0] if self.command else "hook"


class HookSet(BaseModel):
    """Every configured hook, grouped by the event that fires it."""

    session_start: list[HookDefinition] = Field(default_factory=list)
    user_prompt_submit: list[HookDefinition] = Field(default_factory=list)
    pre_tool_use: list[HookDefinition] = Field(default_factory=list)
    post_tool_use: list[HookDefinition] = Field(default_factory=list)
    notification: list[HookDefinition] = Field(default_factory=list)
    stop: list[HookDefinition] = Field(default_factory=list)
    session_end: list[HookDefinition] = Field(default_factory=list)

    def for_event(self, event: HookEvent) -> list[HookDefinition]:
        return [item for item in getattr(self, event.value, []) if item.enabled]

    @property
    def empty(self) -> bool:
        return not any(self.for_event(event) for event in HookEvent)

    def merged_with(self, other: HookSet) -> HookSet:
        """Combine two sets, running this one's hooks first.

        Used for global-then-project layering. Both run: a project cannot drop a
        hook its organisation configured globally, which is the point of having a
        global layer at all.
        """
        return HookSet(
            **{
                event.value: [*getattr(self, event.value), *getattr(other, event.value)]
                for event in HookEvent
            }
        )
