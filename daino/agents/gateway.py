"""Audited model gateway shared by all specialist agents."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any, TypeVar

from pydantic import BaseModel

from daino.config.models import ProviderConfig, Settings
from daino.context.profiles import ModelExecutionProfile
from daino.events import AgentRoleChanged, EventBus, ModelReasoningChunk, ModelSelected
from daino.exceptions import ProviderError
from daino.model_router import ModelRole, ModelRouter, RoutingContext
from daino.model_router.router import ModelSelection
from daino.persistence import Database
from daino.persistence.models import ModelCall
from daino.providers import create_provider
from daino.providers.base import ProviderUsage
from daino.schemas import LLMResponse, Message
from daino.utils.ids import new_id

StructuredT = TypeVar("StructuredT", bound=BaseModel)

_CONTEXT_SAFETY_TOKENS = 512
_MIN_INPUT_TOKENS = 512
#: Floor on the reply allowance, so a small window still leaves room to answer.
_MIN_OUTPUT_TOKENS = 2_048
#: Input budget to protect before honouring a profile's output ceiling. Matches
#: the default project context budget: below it the agent cannot hold repository
#: grounding and a working transcript at the same time.
_MIN_WORKING_INPUT_TOKENS = 24_000


class ModelGateway:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        events: EventBus | None = None,
        *,
        profile_override: str = "",
    ) -> None:
        self.settings = settings
        self.database = database
        self.router = ModelRouter(settings)
        self.events = events
        self.profile_override = profile_override

    def with_profile(self, profile_override: str) -> ModelGateway:
        """Return a gateway pinned to one model profile for every role.

        Agents receive a gateway rather than a routing decision, so binding here
        applies a session model choice across the whole agent graph without every
        specialist having to thread the selection through its own signature.
        """
        if not profile_override or profile_override == self.profile_override:
            return self
        return ModelGateway(
            self.settings,
            self.database,
            self.events,
            profile_override=profile_override,
        )

    def _emit_selection(
        self,
        mission_id: str,
        role: ModelRole,
        profile_name: str,
        provider: str,
        model: str,
    ) -> None:
        if self.events is None:
            return
        self.events.publish(AgentRoleChanged(mission_id=mission_id, role=role.value))
        self.events.publish(
            ModelSelected(
                mission_id=mission_id,
                profile=profile_name,
                provider=provider,
                model=model,
                role=role.value,
            )
        )

    def _attach_reasoning_handler(
        self,
        provider: object,
        mission_id: str,
        role: ModelRole,
        selection: ModelSelection,
    ) -> None:
        """Forward ephemeral provider reasoning onto the typed mission bus.

        ``getattr`` keeps gateways compatible with legacy/custom provider
        doubles which predate the optional handler method. The event deliberately
        carries only the chunk and routing identity; provider response payloads
        and other metadata must not enter the live reasoning channel.
        """
        setter = getattr(provider, "set_reasoning_handler", None)
        if self.events is None or not callable(setter):
            return

        def publish(content: str) -> None:
            if not isinstance(content, str) or not content:
                return
            self.events.publish(
                ModelReasoningChunk(
                    mission_id=mission_id,
                    content=content,
                    role=role.value,
                    provider=selection.profile.provider,
                    model=selection.profile.model,
                    profile=selection.profile_name,
                )
            )

        setter(publish)

    def context_budget(
        self,
        role: ModelRole,
        routing_context: RoutingContext | None = None,
        *,
        tools: list[dict[str, Any]] | None = None,
        profile_override: str | None = None,
    ) -> int:
        """Return a safe input budget for the selected model and action schema."""
        selection = self.router.select(
            role,
            routing_context,
            profile_override=profile_override or self.profile_override,
        )
        return _input_budget(selection, tools)

    def execution_profile(
        self,
        role: ModelRole,
        routing_context: RoutingContext | None = None,
        *,
        tools: list[dict[str, Any]] | None = None,
        profile_override: str | None = None,
    ) -> ModelExecutionProfile:
        """Resolve concrete context and loop limits for the selected model."""
        selection = self.router.select(
            role,
            routing_context,
            profile_override=profile_override or self.profile_override,
        )
        return ModelExecutionProfile.resolve(
            selection.profile_name,
            selection.profile,
            input_budget_tokens=_input_budget(selection, tools),
            project_budget_tokens=self.settings.project.context_budget_tokens,
            memory_items=self.settings.memory.max_retrieved_items,
            memory_tokens=self.settings.memory.max_context_tokens,
        )

    async def structured(
        self,
        mission_id: str,
        role: ModelRole,
        messages: list[Message],
        schema: type[StructuredT],
        *,
        routing_context: RoutingContext | None = None,
        included_files: list[str] | None = None,
        profile_override: str | None = None,
    ) -> StructuredT:
        selections = self.router.failover_selections(
            role,
            routing_context,
            profile_override=profile_override or self.profile_override,
        )
        last_failure: ProviderError | None = None
        for selection in selections:
            self._emit_selection(
                mission_id,
                role,
                selection.profile_name,
                selection.profile.provider,
                selection.profile.model,
            )
            provider_config = self.settings.providers.get(selection.profile.provider)
            if provider_config is None:
                raise RuntimeError(
                    f"Model {selection.profile_name} references missing provider "
                    f"{selection.profile.provider}"
                )
            provider = create_provider(
                selection.profile.provider, _resolved_config(provider_config, selection)
            )
            self._attach_reasoning_handler(provider, mission_id, role, selection)
            record = ModelCall(
                id=new_id("model-call"),
                mission_id=mission_id,
                role=role.value,
                provider=selection.profile.provider,
                model=selection.profile.model,
                selection_reason=selection.reason,
                included_files=included_files or [],
                success=False,
            )
            started = monotonic()
            try:
                fitted = _fit_messages(messages, _input_budget(selection))
                result = await provider.structured_complete(fitted, schema)
                record.success = True
            except ProviderError as exc:
                last_failure = exc
            finally:
                usage = _provider_usage(provider)
                record.input_tokens = usage.input_tokens
                record.output_tokens = usage.output_tokens
                record.estimated_cost = usage.cost
                record.latency_ms = (monotonic() - started) * 1000
                await provider.close()
                with self.database.session() as session:
                    session.add(record)
            if record.success:
                return result
        if last_failure is not None:
            raise last_failure
        raise ProviderError(f"No usable model provider is configured for {role.value}")

    def route_supports_tools(
        self,
        role: ModelRole,
        routing_context: RoutingContext | None = None,
        *,
        profile_override: str | None = None,
    ) -> bool:
        """Report whether the provider this role routes to advertises native tool calling."""
        selection = self.router.select(
            role,
            routing_context,
            profile_override=profile_override or self.profile_override,
        )
        provider_config = self.settings.providers.get(selection.profile.provider)
        return provider_config is not None and "tools" in provider_config.features

    async def complete(
        self,
        mission_id: str,
        role: ModelRole,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        routing_context: RoutingContext | None = None,
        included_files: list[str] | None = None,
        profile_override: str | None = None,
    ) -> LLMResponse:
        selections = self.router.failover_selections(
            role,
            routing_context,
            profile_override=profile_override or self.profile_override,
        )
        last_failure: ProviderError | None = None
        for selection in selections:
            self._emit_selection(
                mission_id,
                role,
                selection.profile_name,
                selection.profile.provider,
                selection.profile.model,
            )
            provider_config = self.settings.providers.get(selection.profile.provider)
            if provider_config is None:
                raise RuntimeError(f"Missing provider {selection.profile.provider}")
            provider = create_provider(
                selection.profile.provider, _resolved_config(provider_config, selection)
            )
            self._attach_reasoning_handler(provider, mission_id, role, selection)
            effective_tools = tools if provider.supports_tools() else None
            response: LLMResponse | None = None
            try:
                fitted = _fit_messages(messages, _input_budget(selection, effective_tools))
                response = await provider.complete(
                    fitted, tools=effective_tools, tool_choice=tool_choice
                )
            except ProviderError as exc:
                last_failure = exc
            finally:
                usage = _provider_usage(provider)
                await provider.close()
                with self.database.session() as session:
                    session.add(
                        ModelCall(
                            id=new_id("model-call"),
                            mission_id=mission_id,
                            role=role.value,
                            provider=selection.profile.provider,
                            model=selection.profile.model,
                            selection_reason=selection.reason,
                            included_files=included_files or [],
                            input_tokens=(
                                usage.input_tokens or (response.input_tokens if response else 0)
                            ),
                            output_tokens=(
                                usage.output_tokens or (response.output_tokens if response else 0)
                            ),
                            latency_ms=response.latency_ms if response else 0,
                            # This column predates provider-side accounting. When
                            # available it now stores the actual charged amount.
                            estimated_cost=usage.cost,
                            success=response is not None,
                        )
                    )
            if response is not None:
                return response
        if last_failure is not None:
            raise last_failure
        raise ProviderError(f"No usable model provider is configured for {role.value}")

    async def stream(
        self,
        mission_id: str,
        role: ModelRole,
        messages: list[Message],
        *,
        included_files: list[str] | None = None,
        profile_override: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream from the selected provider and persist the audited model call."""
        selections = self.router.failover_selections(
            role,
            profile_override=profile_override or self.profile_override,
        )
        last_failure: ProviderError | None = None
        for selection in selections:
            self._emit_selection(
                mission_id,
                role,
                selection.profile_name,
                selection.profile.provider,
                selection.profile.model,
            )
            provider_config = self.settings.providers.get(selection.profile.provider)
            if provider_config is None:
                raise RuntimeError(f"Missing provider {selection.profile.provider}")
            provider = create_provider(
                selection.profile.provider, _resolved_config(provider_config, selection)
            )
            self._attach_reasoning_handler(provider, mission_id, role, selection)
            success = False
            emitted = False
            started = monotonic()
            try:
                fitted = _fit_messages(messages, _input_budget(selection))
                async for chunk in provider.stream(fitted):
                    emitted = True
                    yield chunk
                success = True
            except ProviderError as exc:
                last_failure = exc
            finally:
                usage = _provider_usage(provider)
                await provider.close()
                with self.database.session() as session:
                    session.add(
                        ModelCall(
                            id=new_id("model-call"),
                            mission_id=mission_id,
                            role=role.value,
                            provider=selection.profile.provider,
                            model=selection.profile.model,
                            selection_reason=selection.reason,
                            included_files=included_files or [],
                            input_tokens=usage.input_tokens,
                            output_tokens=usage.output_tokens,
                            latency_ms=(monotonic() - started) * 1000,
                            estimated_cost=usage.cost,
                            success=success,
                        )
                    )
            if success:
                return
            # Once text reached the user, retrying from another model would
            # append a second beginning to the same answer.
            if emitted and last_failure is not None:
                raise last_failure
        if last_failure is not None:
            raise last_failure
        raise ProviderError(f"No usable model provider is configured for {role.value}")


def _resolved_config(provider_config: ProviderConfig, selection: ModelSelection) -> ProviderConfig:
    """Apply the chosen profile's model and output ceiling to the provider config.

    The profile is the per-model half of the routing decision, so a profile that
    raises max_output_tokens for a model with a bigger window has to actually
    reach the provider. Previously only the model name was carried over and the
    profile's ceiling was silently ignored.
    """
    return provider_config.model_copy(
        update={
            "model": selection.profile.model,
            "max_output_tokens": _output_limit(selection),
            "context_limit": selection.profile.context_window,
            "reasoning_effort": selection.profile.reasoning_effort,
        }
    )


def _provider_usage(provider: object) -> ProviderUsage:
    """Read optional accounting without breaking custom provider adapters."""
    usage = getattr(provider, "last_usage", None)
    return usage if isinstance(usage, ProviderUsage) else ProviderUsage()


def _output_limit(selection: ModelSelection) -> int:
    """Reserve for the reply only what the window can spare.

    The profile's ceiling is honoured whenever the window is big enough to afford
    it, so an operator who raises it for a roomy model still gets it. What the
    ceiling may not do is starve the input side: reserving half of a 32k window
    for output left about 14k for instructions, repository grounding and
    observations, so compaction fired on nearly every turn and the agent kept
    re-doing work it had already completed.
    """
    window = selection.profile.context_window
    affordable = max(_MIN_OUTPUT_TOKENS, window - _MIN_WORKING_INPUT_TOKENS)
    return min(selection.profile.max_output_tokens, affordable)


def _input_budget(selection: ModelSelection, tools: list[dict[str, Any]] | None = None) -> int:
    tool_tokens = max(0, len(json.dumps(tools or [], separators=(",", ":"))) // 4)
    return max(
        _MIN_INPUT_TOKENS,
        selection.profile.context_window
        - _output_limit(selection)
        - tool_tokens
        - _CONTEXT_SAFETY_TOKENS,
    )


def _message_tokens(message: Message) -> int:
    tool_json = (
        json.dumps([item.model_dump(mode="json") for item in message.tool_calls])
        if message.tool_calls
        else ""
    )
    return max(1, (len(message.content) + len(tool_json)) // 4) + 8


def _clip_text(text: str, chars: int) -> str:
    if len(text) <= chars:
        return text
    if chars <= 160:
        return text[:chars]
    notice = "\n… older/oversized context omitted …\n"
    remaining = chars - len(notice)
    head = remaining * 2 // 3
    return text[:head] + notice + text[-(remaining - head) :]


def _fit_messages(messages: list[Message], budget_tokens: int) -> list[Message]:
    """Keep the task and newest complete tool exchanges within a model window."""
    if sum(_message_tokens(message) for message in messages) <= budget_tokens:
        return list(messages)
    if not messages:
        return []

    system_index = next((i for i, item in enumerate(messages) if item.role == "system"), None)
    first_tool = next((i for i, item in enumerate(messages) if item.role == "tool"), len(messages))
    task_index = next(
        (i for i in range(first_tool - 1, -1, -1) if messages[i].role == "user"),
        next((i for i in range(len(messages) - 1, -1, -1) if messages[i].role == "user"), 0),
    )
    pinned = {task_index}
    if system_index is not None:
        pinned.add(system_index)

    replacements: dict[int, Message] = {}
    remaining_chars = budget_tokens * 4
    for index in sorted(pinned):
        message = messages[index]
        # Split the hard budget across pinned messages first. The task receives
        # what remains after the system prompt, and head+tail clipping keeps its
        # objective as well as end-of-bundle metadata visible.
        other_cost = sum(len(replacements[item].content) for item in replacements)
        allowance = max(160, remaining_chars - other_cost)
        replacements[index] = message.model_copy(
            update={"content": _clip_text(message.content, allowance)}
        )

    used = sum(_message_tokens(replacements.get(i, messages[i])) for i in pinned)
    selected = set(pinned)
    groups: list[list[int]] = []
    index = 0
    while index < len(messages):
        if index in pinned:
            index += 1
            continue
        group = [index]
        if messages[index].role == "assistant":
            cursor = index + 1
            while cursor < len(messages) and messages[cursor].role == "tool":
                group.append(cursor)
                cursor += 1
            index = cursor
        else:
            index += 1
        groups.append(group)

    for group in reversed(groups):
        cost = sum(_message_tokens(messages[item]) for item in group)
        if used + cost <= budget_tokens:
            selected.update(group)
            used += cost
        elif selected == pinned:
            # The newest observation is what lets the loop correct its last
            # action. Keep the complete protocol group and clip its prose rather
            # than dropping the observation and inviting the same mistake again.
            available = budget_tokens - used
            empty_cost = sum(
                _message_tokens(messages[item].model_copy(update={"content": ""})) for item in group
            )
            content_items = [item for item in group if messages[item].content]
            if content_items and available > empty_cost + 16:
                chars_each = max(64, (available - empty_cost) * 4 // len(content_items))
                for item in content_items:
                    replacements[item] = messages[item].model_copy(
                        update={"content": _clip_text(messages[item].content, chars_each)}
                    )
                clipped_cost = sum(
                    _message_tokens(replacements.get(item, messages[item])) for item in group
                )
                if used + clipped_cost <= budget_tokens:
                    selected.update(group)
                    used += clipped_cost
    return [replacements.get(i, message) for i, message in enumerate(messages) if i in selected]
