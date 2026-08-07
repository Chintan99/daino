"""Iterative builder agent loop.

A single-shot builder returns every file change in one batch, so a single
miscounted hunk or an unread file fails the whole task at once. The loop runs
one action at a time instead: the model emits an ``AgentAction``, the executor
applies it through the validated tools, and the observation is fed back before
the next turn.

The loop speaks both dialects a model backend may offer. When the routed
provider advertises native tool calling, each turn sends the action space as
OpenAI-format tools and executes the returned tool calls (a turn may carry
several, as in Claude Code / opencode). When the backend has no tool support,
or rejects the tools parameter once, the loop falls back to the same action
expressed as schema-constrained JSON, which Ollama ``format`` and vLLM
``guided_json`` decode server-side. Either way the executor sees one validated
``AgentAction`` at a time.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from vasuki.agents.gateway import ModelGateway
from vasuki.agents.tool_schemas import (
    AGENT_TOOL_SPECS,
    action_arguments_invalid,
    tool_call_to_action,
)
from vasuki.exceptions import ProviderError
from vasuki.model_router import ModelRole, RoutingContext
from vasuki.prompts import BUILD_LOOP_SYSTEM, DEBUG_LOOP_SYSTEM
from vasuki.schemas import (
    AgentAction,
    AgentObservation,
    ContextBundle,
    Implementation,
    Message,
    ToolCall,
    ToolResult,
)
from vasuki.tools import ActionExecutor

#: Hard ceiling on turns. A deliberately generous bound: an editor that cannot
#: finish in this many grounded steps is looping rather than implementing, and
#: verification will catch the result either way.
DEFAULT_MAX_STEPS = 24

#: Actions that end the loop. ``finish`` reports work done; ``respond`` answers
#: without changing anything and is only offered to the chat agent.
_TERMINAL = frozenset({"finish", "respond"})

#: Fed back when a turn is cut off at the output-token ceiling. Whole-file writes
#: are what usually blow the budget, and the way out is a targeted edit.
TRUNCATED_TURN_NOTICE = (
    "Your last reply was cut off at the output token limit before it produced a usable "
    "action, so nothing was applied. Do not try to write the whole file again. Use the "
    "replace action with a short old_string and new_string covering only the lines that "
    "must change. If several regions change, do them one replace at a time."
)


@dataclass
class BuilderOutcome:
    implementation: Implementation
    #: Repository-relative paths the loop actually changed, de-duplicated.
    changed: list[str]
    #: Turns the model took, including the finishing turn.
    steps: int
    #: Prose the agent chose to answer with, set only when it ended via
    #: ``respond`` rather than ``finish``.
    answer: str = ""


class ToolLoop:
    """Drive a builder/debugger through grounded actions, one model turn at a time."""

    def __init__(
        self,
        gateway: ModelGateway,
        role: ModelRole,
        executor: ActionExecutor,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        debugger: bool = False,
        attempts: int = 0,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        self.gateway = gateway
        self.role = role
        self.executor = executor
        self.max_steps = max_steps
        self.debugger = debugger
        self.attempts = attempts
        #: Overrides the build/debug system prompt, so the same loop can drive the
        #: chat agent without a second implementation of the turn machinery.
        self.system = system
        self.tools = tools if tools is not None else AGENT_TOOL_SPECS
        #: Set when the backend rejected the native tool-calling request, so the
        #: rest of the run uses schema-constrained JSON without paying for a
        #: failing request on every turn.
        self.native_tools_disabled = False

    async def run(
        self,
        mission_id: str,
        context: ContextBundle,
        *,
        on_action: OnActionCallback | None = None,
        history: list[Message] | None = None,
    ) -> BuilderOutcome:
        system = self.system or (DEBUG_LOOP_SYSTEM if self.debugger else BUILD_LOOP_SYSTEM)
        messages: list[Message] = [
            Message(role="system", content=system),
            # Earlier turns sit between the system prompt and this turn's task, so
            # a follow-up like "now do the same for the footer" resolves.
            *(history or []),
            Message(role="user", content=context.model_dump_json(indent=2)),
        ]
        routing_context = RoutingContext(
            failed_attempts=self.attempts,
            affected_files=len(context.included_paths),
            tests_failing=self.debugger,
        )
        changed: list[str] = []
        steps = 0
        while steps < self.max_steps:
            steps += 1
            finish = await self._step(
                mission_id,
                messages,
                routing_context,
                context,
                changed,
                on_action,
            )
            if finish is not None:
                return BuilderOutcome(
                    implementation=Implementation(
                        summary=finish.summary or finish.message or "",
                        modifications=[],
                        verification_commands=finish.verification_commands,
                    ),
                    changed=sorted(set(changed)),
                    steps=steps,
                    answer=finish.message if finish.action == "respond" else "",
                )
        return BuilderOutcome(
            implementation=Implementation(
                summary="Step budget exhausted before the agent emitted finish.",
                modifications=[],
                verification_commands=[],
            ),
            changed=sorted(set(changed)),
            steps=steps,
        )

    def _native_tools_available(self, routing_context: RoutingContext) -> bool:
        if self.native_tools_disabled:
            return False
        probe = getattr(self.gateway, "route_supports_tools", None)
        if probe is None:
            return False
        try:
            return bool(probe(self.role, routing_context))
        except Exception:
            return False

    async def _step(
        self,
        mission_id: str,
        messages: list[Message],
        routing_context: RoutingContext,
        context: ContextBundle,
        changed: list[str],
        on_action: OnActionCallback | None,
    ) -> AgentAction | None:
        """Run one model turn; return the finishing action when the model stops."""
        if self._native_tools_available(routing_context):
            try:
                response = await self.gateway.complete(
                    mission_id,
                    self.role,
                    messages,
                    tools=self.tools,
                    tool_choice="required",
                    routing_context=routing_context,
                    included_files=context.included_paths,
                )
            except ProviderError:
                # The backend refused the tool-calling request (for example a
                # vLLM server started without a tool-call parser). Use the
                # structured path for the rest of this run.
                self.native_tools_disabled = True
            else:
                if response.tool_calls:
                    return await self._apply_tool_calls(
                        response.content, response.tool_calls, messages, changed, on_action
                    )
                if response.finish_reason == "length":
                    # The turn was cut off at the output ceiling, so whatever it
                    # was writing is unusable. Retrying the same request would be
                    # cut off in the same place; ask for a smaller edit instead.
                    messages.append(Message(role="user", content=TRUNCATED_TURN_NOTICE))
                    return None
        return await self._structured_step(
            mission_id, messages, routing_context, context, changed, on_action
        )

    async def _structured_step(
        self,
        mission_id: str,
        messages: list[Message],
        routing_context: RoutingContext,
        context: ContextBundle,
        changed: list[str],
        on_action: OnActionCallback | None,
    ) -> AgentAction | None:
        action = await self.gateway.structured(
            mission_id,
            self.role,
            messages,
            AgentAction,
            routing_context=routing_context,
            included_files=context.included_paths,
        )
        messages.append(Message(role="assistant", content=action.model_dump_json(indent=2)))
        if action.action in _TERMINAL:
            return action
        await self._execute(action, messages, changed, on_action, tool_call_id="")
        return None

    async def _apply_tool_calls(
        self,
        content: str,
        calls: list[ToolCall],
        messages: list[Message],
        changed: list[str],
        on_action: OnActionCallback | None,
    ) -> AgentAction | None:
        messages.append(Message(role="assistant", content=content, tool_calls=list(calls)))
        for call in calls:
            try:
                action = tool_call_to_action(call)
            except ValidationError as exc:
                observation = AgentObservation(
                    action=call.name,
                    success=False,
                    detail=action_arguments_invalid(exc),
                )
                messages.append(
                    Message(
                        role="tool",
                        content=observation.model_dump_json(indent=2),
                        tool_call_id=call.id,
                    )
                )
                continue
            if action.action in _TERMINAL:
                return action
            await self._execute(action, messages, changed, on_action, tool_call_id=call.id)
        return None

    async def _execute(
        self,
        action: AgentAction,
        messages: list[Message],
        changed: list[str],
        on_action: OnActionCallback | None,
        *,
        tool_call_id: str,
    ) -> None:
        result, paths = await self.executor.execute(action)
        changed.extend(paths)
        if on_action is not None:
            on_action(action, result, paths)
        observation = AgentObservation(
            action=action.action, success=result.success, detail=_detail(action, result)
        )
        messages.append(
            Message(
                role="tool",
                content=observation.model_dump_json(indent=2),
                tool_call_id=tool_call_id,
            )
        )


def _detail(action: AgentAction, result: ToolResult) -> str:
    if action.action == "read_file" and result.success:
        # The observation must show what was read so the next edit can
        # copy it exactly; truncate to keep the conversation bounded.
        content = str(result.data.get("content", ""))
        if len(content) > 6_000:
            content = content[:6_000] + "\n… truncated …\n"
        return content
    return result.error or _summarize(result)


def _summarize(result: ToolResult) -> str:
    data = result.data or {}
    if not data:
        return "ok"
    parts = [f"{key}: {value}" for key, value in data.items() if key != "content"]
    return "; ".join(parts) or "ok"


# A callback the orchestrator installs to observe every executed action. It is
# a callable rather than a Protocol so mission execution can build a closure
# without defining a new class.
OnActionCallback = Callable[[AgentAction, ToolResult, list[str]], None]
