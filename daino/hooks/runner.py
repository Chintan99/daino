"""Run configured hooks and turn what they say into a decision.

**On running hooks through a shell.** Everywhere else in Daino a command is
``shlex.split`` and executed without a shell, because the command came from a
model and a shell would hand it pipes, redirects and substitution. A hook did
not come from a model: it is a line the user wrote in their own configuration
file, exactly like a Git hook, and pipes are most of what makes a one-line hook
useful. The shell is therefore correct here — but only as long as the model
cannot write the file that supplies it, which is why ``EditTools`` refuses every
write into the state directory outside the workspaces subtree, and why the hook
set is snapshotted when the session opens rather than re-read per action.

**On what a hook may decide.** Only ``pre_tool_use`` and ``user_prompt_submit``
can stop anything; at every other point the work has already happened and a
refusal would be a lie. Elsewhere a hook can still *say* something — feedback
that lands in the model's observation, or a message shown to the user — which is
what makes "format the file, then tell the agent what changed" a hook rather
than a feature.

**On failure.** A hook that crashes, times out, or prints nonsense is reported
and ignored. The alternative — a broken formatter hook that blocks every edit in
the repository — is worse than an unformatted file. The one exception is the
explicit block: exit code 2, or a JSON deny, both of which are a hook working
correctly and saying no.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from daino.hooks.models import BLOCKING_EVENTS, HookDefinition, HookEvent, HookSet

#: Exit status a hook uses to block. Chosen to match the Claude Code convention,
#: so a script written for one works in the other. Any other non-zero status is a
#: hook that broke, which is reported and ignored.
BLOCK_EXIT_CODE = 2

#: Ceiling on how much of a hook's output is kept. A hook that dumps a build log
#: to stdout must not push the transcript into a compaction.
MAX_OUTPUT_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class HookOutcome:
    """The combined verdict of every hook that ran for one event."""

    #: ``""`` when nothing was decided. ``"deny"`` blocks; ``"ask"`` escalates to
    #: the user's approval path; ``"allow"`` short-circuits an approval that
    #: would otherwise have been asked for.
    decision: str = ""
    #: Why, in the hook's own words. Shown to the user and fed to the model.
    reason: str = ""
    #: Text a hook asked to be added to the model's context or observation.
    context: str = ""
    #: Hooks that failed. Recorded so a silently broken hook is still visible.
    failures: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.decision == "deny"

    @property
    def quiet(self) -> bool:
        """Nothing to report: no verdict, no feedback, no failures."""
        return not (self.decision or self.reason or self.context or self.failures)


@dataclass
class HookRunner:
    """Executes the hooks configured for a project."""

    root: Path
    hooks: HookSet = field(default_factory=HookSet)
    #: Identifies the session in every payload, so a hook can correlate.
    session_id: str = ""
    #: Called with a one-line description of anything notable a hook did, so hook
    #: activity reaches the audit ledger rather than only the model.
    audit: Any = None
    #: Overridden in tests. Production passes the process environment through, as
    #: Git does, because a hook is the user's own script and needs their PATH.
    environment: dict[str, str] | None = None

    @property
    def enabled(self) -> bool:
        return not self.hooks.empty

    def configured_for(self, event: HookEvent) -> bool:
        """Whether running this event would do anything. Lets callers skip cheaply."""
        return bool(self.hooks.for_event(event))

    async def run(
        self,
        event: HookEvent,
        *,
        tool_name: str = "",
        payload: dict[str, Any] | None = None,
    ) -> HookOutcome:
        """Run every hook matching ``event`` and combine what they said."""
        definitions = [item for item in self.hooks.for_event(event) if _matches(item, tool_name)]
        if not definitions:
            return HookOutcome()
        body = {
            "hook_event_name": event.value,
            "session_id": self.session_id,
            "cwd": str(self.root),
            **({"tool_name": tool_name} if tool_name else {}),
            **(payload or {}),
        }
        encoded = json.dumps(body, default=str)
        results = await asyncio.gather(*(self._invoke(item, encoded) for item in definitions))
        return _combine(event, definitions, results)

    async def _invoke(self, definition: HookDefinition, encoded: str) -> _HookResult:
        try:
            process = await asyncio.create_subprocess_shell(  # noqa: S604 - see module docstring
                definition.command,
                cwd=self.root,
                env=self.environment if self.environment is not None else os.environ.copy(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return _HookResult(definition, failure=f"could not start: {exc}")
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(encoded.encode()), timeout=definition.timeout
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            return _HookResult(
                definition,
                failure=f"timed out after {definition.timeout:g}s",
            )
        return _HookResult(
            definition,
            exit_code=process.returncode or 0,
            stdout=_clip(stdout.decode(errors="replace")),
            stderr=_clip(stderr.decode(errors="replace")),
        )


@dataclass(slots=True)
class _HookResult:
    definition: HookDefinition
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    failure: str = ""


def _matches(definition: HookDefinition, tool_name: str) -> bool:
    """Whether this hook applies to this tool.

    An empty matcher means every tool. An invalid regular expression matches
    nothing and is reported by the loader rather than raised here, because a
    typo in a matcher must not take down every action in the session.
    """
    if not definition.matcher:
        return True
    if not tool_name:
        return True
    try:
        return re.fullmatch(definition.matcher, tool_name) is not None
    except re.error:
        return False


def _combine(
    event: HookEvent, definitions: list[HookDefinition], results: list[_HookResult]
) -> HookOutcome:
    """Fold several hooks' answers into one, with deny winning.

    Deny is strongest and allow is weakest, deliberately: two hooks disagreeing
    about whether an edit may happen should resolve to "no". A hook that wants
    something to happen can only decline to object.
    """
    decision = ""
    reasons: list[str] = []
    contexts: list[str] = []
    failures: list[str] = []
    for result in results:
        label = result.definition.label
        if result.failure:
            failures.append(f"{label}: {result.failure}")
            continue
        parsed = _parse_output(result)
        if parsed.get("failure"):
            failures.append(f"{label}: {parsed['failure']}")
            continue
        verdict = str(parsed.get("decision") or "")
        if verdict and _rank(verdict) > _rank(decision):
            decision = verdict
        reason = str(parsed.get("reason") or "")
        if reason:
            reasons.append(f"{label}: {reason}" if len(definitions) > 1 else reason)
        context = str(parsed.get("context") or "")
        if context:
            contexts.append(context)
    if decision and event not in BLOCKING_EVENTS:
        # The work already happened. A verdict here is treated as feedback, and
        # the reason still reaches the model — pretending it was enforced would
        # tell the agent something untrue about what its action did.
        decision = ""
    return HookOutcome(
        decision=decision,
        reason="\n".join(reasons),
        context="\n".join(contexts),
        failures=tuple(failures),
    )


_DECISION_RANK = {"": 0, "allow": 1, "ask": 2, "deny": 3}


def _rank(decision: str) -> int:
    return _DECISION_RANK.get(decision, 0)


def _parse_output(result: _HookResult) -> dict[str, str]:
    """Read a hook's verdict from its exit code and, if present, its JSON stdout.

    Both channels are supported because both are useful. A one-line shell hook
    says everything it needs to with ``exit 2`` and a message on stderr; a hook
    that wants to add context without blocking needs structured output to say so.
    """
    if result.exit_code == BLOCK_EXIT_CODE:
        return {
            "decision": "deny",
            "reason": result.stderr.strip()
            or result.stdout.strip()
            or "the hook refused this action",
        }
    if result.exit_code != 0:
        return {"failure": f"exit {result.exit_code}: {result.stderr.strip()[:400]}"}
    body = result.stdout.strip()
    if not body.startswith("{"):
        # Plain output on a successful hook is informational. Kept as context so
        # a formatter can report what it changed without inventing a JSON shape.
        return {"context": body} if body else {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        return {"failure": f"stdout was not valid JSON: {exc}"}
    if not isinstance(payload, dict):
        return {"failure": "stdout JSON was not an object"}
    decision = str(payload.get("permissionDecision") or payload.get("decision") or "").casefold()
    if decision == "block":
        # The Claude Code spelling for the same thing.
        decision = "deny"
    if decision not in _DECISION_RANK:
        decision = ""
    return {
        "decision": decision,
        "reason": str(payload.get("permissionDecisionReason") or payload.get("reason") or ""),
        "context": str(payload.get("additionalContext") or payload.get("systemMessage") or ""),
    }


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n… hook output truncated …"
