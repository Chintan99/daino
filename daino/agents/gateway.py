"""Audited model gateway shared by all specialist agents."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from time import monotonic
from typing import Any, TypeVar

from pydantic import BaseModel

from daino.agents.budget import BudgetLedger, BudgetSnapshot, RunBudget
from daino.agents.tokens import CALIBRATION, estimate_message, message_chars
from daino.config.models import ProviderConfig, Settings
from daino.context.profiles import CapabilityEnvelope, ModelExecutionProfile
from daino.events import AgentRoleChanged, EventBus, ModelReasoningChunk, ModelSelected
from daino.exceptions import ProviderError
from daino.model_router import ModelRole, ModelRouter, RoutingContext
from daino.model_router.router import ModelSelection
from daino.observability import span
from daino.persistence import Database
from daino.persistence.models import ModelCall
from daino.providers import create_provider
from daino.providers.base import LLMProvider, ProviderUsage
from daino.providers.pool import POOL, ProviderPool
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
        budgets: BudgetLedger | None = None,
        pool: ProviderPool | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.router = ModelRouter(settings)
        self.events = events
        self.profile_override = profile_override
        #: Spend ceilings, keyed by mission. Shared with every gateway derived
        #: from this one, so a team's nine members draw on one account rather
        #: than nine — see :mod:`daino.agents.budget`.
        self.budgets = budgets or BudgetLedger(settings.budget, events)
        #: Warm provider adapters. Process-wide by default: a gateway is rebuilt
        #: per pinned profile and per turn, so a per-gateway pool would be empty
        #: on the very call it exists to speed up.
        self.pool = pool or POOL

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
            # Deliberately shared, not copied: a pinned session is the same run,
            # and giving it a fresh budget would silently double the ceiling.
            budgets=self.budgets,
            pool=self.pool,
        )

    def budget_snapshot(self, mission_id: str) -> BudgetSnapshot | None:
        """What this mission has spent so far, or ``None`` when uncapped."""
        return self.budgets.snapshot(mission_id)

    def release_budget(self, mission_id: str) -> BudgetSnapshot | None:
        """Close a finished mission's account and return its final state."""
        return self.budgets.release(mission_id)

    def _budget(self, mission_id: str) -> RunBudget | None:
        """The mission's budget, checked before the call it is about to admit."""
        budget = self.budgets.budget_for(mission_id)
        if budget is not None:
            budget.check()
        return budget

    @staticmethod
    def _charge(budget: RunBudget | None, usage: ProviderUsage) -> None:
        if budget is not None:
            budget.record(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cached_tokens,
                cost=usage.cost,
            )

    async def _acquire(
        self, selection: ModelSelection, mission_id: str, role: ModelRole
    ) -> LLMProvider:
        """Borrow a warm adapter for one call and arm its reasoning channel."""
        provider_config = self.settings.providers.get(selection.profile.provider)
        if provider_config is None:
            raise RuntimeError(
                f"Model {selection.profile_name} references missing provider "
                f"{selection.profile.provider}"
            )
        provider = await self.pool.acquire(
            selection.profile.provider,
            _resolved_config(provider_config, selection),
            # Resolved from this module's namespace on every call rather than
            # captured once, so a substituted factory is honoured — and, because
            # the pool keys on it, never served an adapter another factory built.
            factory=create_provider,
        )
        self._attach_reasoning_handler(provider, mission_id, role, selection)
        return provider

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

    @staticmethod
    def _calibrate(
        model: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        usage: ProviderUsage,
    ) -> None:
        """Teach the estimator what this request actually cost.

        Tool schemas are counted because the provider charged for them: a ratio
        derived from the messages alone would attribute their tokens to the
        transcript and read as denser text than the transcript really is.
        """
        if not messages or usage.input_tokens <= 0:
            return
        chars = sum(message_chars(item) for item in messages)
        if tools:
            chars += len(json.dumps(tools, separators=(",", ":")))
        CALIBRATION.observe(model, chars, usage.input_tokens)

    def supports_vision(
        self,
        role: ModelRole,
        routing_context: RoutingContext | None = None,
        *,
        profile_override: str | None = None,
    ) -> bool:
        """Whether the model this role routes to can be shown an image."""
        try:
            selection = self.router.select(
                role,
                routing_context,
                profile_override=profile_override or self.profile_override,
            )
        except Exception:  # noqa: BLE001 - an unroutable role cannot see anything
            return False
        return bool(getattr(selection.profile, "vision", False))

    def selected_model(
        self,
        role: ModelRole,
        routing_context: RoutingContext | None = None,
        *,
        profile_override: str | None = None,
    ) -> str:
        """The model this role would use, so estimates use its calibration."""
        try:
            selection = self.router.select(
                role,
                routing_context,
                profile_override=profile_override or self.profile_override,
            )
        except Exception:  # noqa: BLE001 - an unroutable role estimates at the default
            return ""
        return selection.profile.model

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
            # The same threshold the agent loop compacts at, so the scaffolding
            # is sized against the limit it actually has to fit under.
            compaction_threshold=self.settings.memory.compaction_threshold,
        )

    def capability_envelope(
        self,
        role: ModelRole,
        routing_context: RoutingContext | None = None,
        *,
        tools: list[dict[str, Any]] | None = None,
        profile_override: str | None = None,
    ) -> CapabilityEnvelope:
        """Describe what the model routed to *role* can hold while working.

        Callers deciding how big a task may be should pass the same ``tools``
        the executor will run with — ``AGENT_TOOL_SPECS`` for a builder. The
        tool schemas are subtracted from the input budget (see
        ``_input_budget``), so an envelope resolved without them over-reports
        the headroom by exactly what the executor is charged for tools it will
        certainly have.
        """
        return CapabilityEnvelope.from_profile(
            self.execution_profile(
                role,
                routing_context,
                tools=tools,
                profile_override=profile_override,
            ),
            compaction_threshold=self.settings.memory.compaction_threshold,
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
            # Before the provider is borrowed, so an exhausted run costs neither
            # a connection nor a secret lookup. Propagates out of the failover
            # loop deliberately: a ceiling is not a provider fault to fail over.
            budget = self._budget(mission_id)
            provider = await self._acquire(selection, mission_id, role)
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
            transport_failure = False
            with span(
                "model.structured",
                **_call_attributes(mission_id, role, selection),
                **{"gen_ai.output.schema": schema.__name__},
            ) as recording:
                try:
                    fitted = _fit_messages(messages, _input_budget(selection))
                    result = await provider.structured_complete(fitted, schema)
                    record.success = True
                except ProviderError as exc:
                    last_failure = exc
                    transport_failure = True
                finally:
                    usage = _provider_usage(provider)
                    record.input_tokens = usage.input_tokens
                    record.output_tokens = usage.output_tokens
                    record.cached_tokens = usage.cached_tokens
                    record.estimated_cost = usage.cost
                    record.latency_ms = (monotonic() - started) * 1000
                    # Returned rather than closed, so the next step reuses the
                    # connection. A transport failure discards instead: the
                    # socket that just failed is the one a reuse would hand back.
                    await self.pool.release(provider, discard=transport_failure)
                    self._charge(budget, usage)
                    _record_usage_span(recording, record)
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
            budget = self._budget(mission_id)
            provider = await self._acquire(selection, mission_id, role)
            effective_tools = tools if provider.supports_tools() else None
            # A text-only model answers an image part with a hard request error,
            # not a degraded reply, so the images come off here rather than
            # failing the turn. What replaces them is a note, because a model
            # told nothing would answer confidently about a picture it never saw.
            sendable = _without_unusable_images(messages, selection)
            response: LLMResponse | None = None
            fitted: list[Message] = []
            transport_failure = False
            with span(
                "model.complete",
                **_call_attributes(mission_id, role, selection),
                **{"gen_ai.request.tools": len(effective_tools or ())},
            ) as recording:
                try:
                    fitted = _fit_messages(sendable, _input_budget(selection, effective_tools))
                    response = await provider.complete(
                        fitted, tools=effective_tools, tool_choice=tool_choice
                    )
                except ProviderError as exc:
                    last_failure = exc
                    transport_failure = True
                finally:
                    usage = _provider_usage(provider)
                    # What the provider charged for the request just sent is the
                    # only honest measurement of how this model tokenizes this
                    # workload. Feeding it back is what stops every budget in the
                    # agent from being computed against a guess.
                    self._calibrate(selection.profile.model, fitted, effective_tools, usage)
                    await self.pool.release(provider, discard=transport_failure)
                    self._charge(budget, usage)
                    record = ModelCall(
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
                        cached_tokens=(
                            usage.cached_tokens or (response.cached_tokens if response else 0)
                        ),
                        latency_ms=response.latency_ms if response else 0,
                        # This column predates provider-side accounting. When
                        # available it now stores the actual charged amount.
                        estimated_cost=usage.cost,
                        success=response is not None,
                    )
                    _record_usage_span(recording, record)
                    if response is not None:
                        recording.set_attribute(
                            "gen_ai.response.tool_calls", len(response.tool_calls)
                        )
                    with self.database.session() as session:
                        session.add(record)
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
            budget = self._budget(mission_id)
            provider = await self._acquire(selection, mission_id, role)
            success = False
            emitted = False
            started = monotonic()
            transport_failure = False
            with span("model.stream", **_call_attributes(mission_id, role, selection)) as recording:
                try:
                    fitted = _fit_messages(messages, _input_budget(selection))
                    async for chunk in provider.stream(fitted):
                        emitted = True
                        yield chunk
                    success = True
                except ProviderError as exc:
                    last_failure = exc
                    transport_failure = True
                finally:
                    usage = _provider_usage(provider)
                    # A generator abandoned mid-stream (the user cancelled) has
                    # a half-read response body on the wire, so it is discarded
                    # rather than returned to the pool for the next borrower.
                    await self.pool.release(provider, discard=transport_failure or not success)
                    self._charge(budget, usage)
                    record = ModelCall(
                        id=new_id("model-call"),
                        mission_id=mission_id,
                        role=role.value,
                        provider=selection.profile.provider,
                        model=selection.profile.model,
                        selection_reason=selection.reason,
                        included_files=included_files or [],
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cached_tokens=usage.cached_tokens,
                        latency_ms=(monotonic() - started) * 1000,
                        estimated_cost=usage.cost,
                        success=success,
                    )
                    _record_usage_span(recording, record)
                    with self.database.session() as session:
                        session.add(record)
            if success:
                return
            # Once text reached the user, retrying from another model would
            # append a second beginning to the same answer.
            if emitted and last_failure is not None:
                raise last_failure
        if last_failure is not None:
            raise last_failure
        raise ProviderError(f"No usable model provider is configured for {role.value}")


def _without_unusable_images(messages: list[Message], selection: ModelSelection) -> list[Message]:
    """Drop image parts a model cannot read, leaving a note in their place.

    Returns the original list untouched when there is nothing to strip, which is
    almost every call — this must not copy a long transcript for nothing.
    """
    if getattr(selection.profile, "vision", False):
        return messages
    if not any(message.images for message in messages):
        return messages
    stripped: list[Message] = []
    for message in messages:
        if not message.images:
            stripped.append(message)
            continue
        described = "; ".join(
            image.description or f"an image ({image.media_type})" for image in message.images
        )
        note = (
            f"[{len(message.images)} image(s) were attached here — {described} — but "
            f"{selection.profile.model} cannot read images, so they were not sent. "
            "Ask the user to describe them, or route this turn to a vision-capable model.]"
        )
        stripped.append(
            message.model_copy(
                update={
                    "images": [],
                    "content": f"{message.content}\n\n{note}" if message.content else note,
                }
            )
        )
    return stripped


def _call_attributes(mission_id: str, role: ModelRole, selection: ModelSelection) -> dict[str, Any]:
    """Identity of one model call, in OpenTelemetry's GenAI attribute names.

    Deliberately no prompt, no completion, no file contents. A trace collector
    is usually the least access-controlled sink in a deployment, and a span that
    carried the transcript would ship the user's source code to it on every
    step. What is here is what a cost or latency question actually needs.
    """
    return {
        "daino.mission_id": mission_id,
        "daino.role": role.value,
        "daino.profile": selection.profile_name,
        "daino.selection_reason": selection.reason,
        "gen_ai.system": selection.profile.provider,
        "gen_ai.request.model": selection.profile.model,
    }


def _record_usage_span(recording: Any, record: ModelCall) -> None:
    """Fold a finished call's accounting onto its span."""
    from daino.observability.tracing import set_attributes

    set_attributes(
        recording,
        {
            "gen_ai.usage.input_tokens": record.input_tokens,
            "gen_ai.usage.output_tokens": record.output_tokens,
            "gen_ai.usage.cached_tokens": record.cached_tokens,
            "daino.cost_usd": record.estimated_cost,
            "daino.latency_ms": record.latency_ms,
            "daino.success": record.success,
        },
    )


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


def _message_tokens(message: Message, model: str = "") -> int:
    """Estimate one message against what this model was last seen to charge."""
    return estimate_message(message, model)


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
