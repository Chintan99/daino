"""Tool-loop harness: native tool calling with structured-JSON fallback."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from vasuki.agents.gateway import ModelGateway, _resolved_config
from vasuki.agents.loop import ToolLoop
from vasuki.config.models import ModelProfileConfig, ProviderConfig, Settings
from vasuki.exceptions import ProviderError
from vasuki.model_router import ModelRole
from vasuki.persistence import Database
from vasuki.schemas import AgentAction, ContextBundle, LLMResponse, ToolCall
from vasuki.tools import ActionExecutor, EditTools


@pytest.fixture()
def executor(tmp_path: Path) -> Iterator[ActionExecutor]:
    yield ActionExecutor(EditTools(tmp_path, require_read_before_write=False))


def context() -> ContextBundle:
    return ContextBundle(task="Add greeter", acceptance_criteria=["greeter exists"])


def tool_response(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(content="", model="mock", provider="mock", tool_calls=list(calls))


class NativeGateway:
    """Gateway double that answers with native tool calls."""

    def __init__(self, turns: list[list[ToolCall]]) -> None:
        self.turns = turns
        self.complete_calls = 0
        self.structured_calls = 0

    def route_supports_tools(self, role: object, context: object = None) -> bool:
        return True

    async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
        self.complete_calls += 1
        return tool_response(*self.turns.pop(0))

    async def structured(self, *args: object, **kwargs: object) -> AgentAction:
        self.structured_calls += 1
        raise AssertionError("structured path should not run")


@pytest.mark.asyncio
async def test_native_tool_calls_drive_the_loop(executor: ActionExecutor, tmp_path: Path) -> None:
    gateway = NativeGateway(
        [
            [
                ToolCall(
                    id="call_1",
                    name="write",
                    arguments={
                        "thought": "Create the module.",
                        "path": "greeter.py",
                        "content": "def greet():\n    return 'hi'\n",
                    },
                )
            ],
            [
                ToolCall(
                    id="call_2",
                    name="finish",
                    arguments={"thought": "Done.", "summary": "Added greeter"},
                )
            ],
        ]
    )
    outcome = await ToolLoop(gateway, ModelRole.BUILDER, executor).run("mission-1", context())  # type: ignore[arg-type]

    assert outcome.steps == 2
    assert outcome.changed == ["greeter.py"]
    assert outcome.implementation.summary == "Added greeter"
    assert (tmp_path / "greeter.py").read_text(encoding="utf-8").startswith("def greet")
    assert gateway.complete_calls == 2


@pytest.mark.asyncio
async def test_multiple_tool_calls_in_one_turn_execute_in_order(
    executor: ActionExecutor, tmp_path: Path
) -> None:
    gateway = NativeGateway(
        [
            [
                ToolCall(
                    id="call_1",
                    name="write",
                    arguments={"thought": "a", "path": "one.py", "content": "x = 1\n"},
                ),
                ToolCall(
                    id="call_2",
                    name="write",
                    arguments={"thought": "b", "path": "two.py", "content": "y = 2\n"},
                ),
            ],
            [
                ToolCall(
                    id="call_3",
                    name="finish",
                    arguments={"thought": "Done.", "summary": "wrote both"},
                )
            ],
        ]
    )
    outcome = await ToolLoop(gateway, ModelRole.BUILDER, executor).run("mission-1", context())  # type: ignore[arg-type]

    assert outcome.changed == ["one.py", "two.py"]
    assert outcome.steps == 2


class RejectingToolsGateway:
    """Advertises tool support but the server rejects the tools parameter."""

    def __init__(self, actions: list[AgentAction]) -> None:
        self.actions = actions
        self.complete_calls = 0

    def route_supports_tools(self, role: object, context: object = None) -> bool:
        return True

    async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
        self.complete_calls += 1
        raise ProviderError("mock rejected the request (HTTP 400): tools not supported")

    async def structured(
        self,
        mission_id: str,
        role: object,
        messages: object,
        schema: type[Any],
        **kwargs: object,
    ) -> AgentAction:
        return self.actions.pop(0)


@pytest.mark.asyncio
async def test_rejected_tool_calls_fall_back_to_structured(
    executor: ActionExecutor, tmp_path: Path
) -> None:
    gateway = RejectingToolsGateway(
        [
            AgentAction(
                thought="Create the module.",
                action="write",
                path="greeter.py",
                content="def greet():\n    return 'hi'\n",
            ),
            AgentAction(thought="Done.", action="finish", summary="Added greeter"),
        ]
    )
    outcome = await ToolLoop(gateway, ModelRole.BUILDER, executor).run("mission-1", context())  # type: ignore[arg-type]

    assert outcome.implementation.summary == "Added greeter"
    # One rejected native attempt, then structured for every remaining turn.
    assert gateway.complete_calls == 1
    assert (tmp_path / "greeter.py").exists()


class PlainGateway:
    """Duck-typed gateway without tool support, like the e2e stubs."""

    def __init__(self, actions: list[AgentAction]) -> None:
        self.actions = actions

    async def structured(
        self,
        mission_id: str,
        role: object,
        messages: object,
        schema: type[Any],
        **kwargs: object,
    ) -> AgentAction:
        return self.actions.pop(0)


@pytest.mark.asyncio
async def test_gateway_without_tool_probe_uses_structured(
    executor: ActionExecutor, tmp_path: Path
) -> None:
    gateway = PlainGateway(
        [
            AgentAction(thought="write", action="write", path="a.txt", content="1"),
            AgentAction(thought="done", action="finish", summary="ok"),
        ]
    )
    outcome = await ToolLoop(gateway, ModelRole.BUILDER, executor).run("mission-1", context())  # type: ignore[arg-type]
    assert outcome.implementation.summary == "ok"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "1"


@pytest.mark.asyncio
async def test_invalid_tool_arguments_are_reported_and_retried(
    executor: ActionExecutor, tmp_path: Path
) -> None:
    gateway = NativeGateway(
        [
            [
                # Unknown argument fields fail schema validation: the loop must
                # feed back an error observation and keep going rather than
                # abort the mission.
                ToolCall(id="call_1", name="replace", arguments={"thought": "edit", "bogus": 1}),
                ToolCall(
                    id="call_2",
                    name="write",
                    arguments={"thought": "write instead", "path": "a.txt", "content": "ok"},
                ),
            ],
            [ToolCall(id="call_3", name="finish", arguments={"thought": "d", "summary": "s"})],
        ]
    )
    outcome = await ToolLoop(gateway, ModelRole.BUILDER, executor).run("mission-1", context())  # type: ignore[arg-type]

    assert outcome.changed == ["a.txt"]
    assert outcome.implementation.summary == "s"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "ok"


class ScriptedGateway:
    """Replays actions and records the observation fed back before each one."""

    def __init__(self, actions: list[AgentAction]) -> None:
        self.actions = actions
        self.observations: list[str] = []

    async def structured(
        self,
        mission_id: str,
        role: object,
        messages: list[Any],
        schema: type[Any],
        **kwargs: object,
    ) -> AgentAction:
        tool_messages = [message for message in messages if message.role == "tool"]
        if tool_messages:
            self.observations.append(tool_messages[-1].content)
        return self.actions.pop(0)


@pytest.mark.asyncio
async def test_a_failed_edit_is_reported_back_so_the_agent_can_correct_it(
    tmp_path: Path,
) -> None:
    """The point of the loop: a bad edit costs one turn, not the whole task.

    The single-shot builder returned every change at once, so one anchor that
    did not match failed the task outright with no chance to look and retry.
    """
    source = tmp_path / "greet.py"
    source.write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path, require_read_before_write=True))
    gateway = ScriptedGateway(
        [
            # Guessed anchor, and the file has not been read: refused.
            AgentAction(
                thought="Change the greeting.",
                action="replace",
                path="greet.py",
                old_string="return 'hi'",
                new_string="return 'hey'",
            ),
            AgentAction(thought="Look at it properly.", action="read_file", path="greet.py"),
            AgentAction(
                thought="Now use the exact text.",
                action="replace",
                path="greet.py",
                old_string="return 'hello'",
                new_string="return 'hey'",
            ),
            AgentAction(thought="Done.", action="finish", summary="Changed the greeting"),
        ]
    )

    outcome = await ToolLoop(gateway, ModelRole.BUILDER, executor).run("mission-1", context())  # type: ignore[arg-type]

    assert outcome.steps == 4
    assert outcome.changed == ["greet.py"]
    assert source.read_text(encoding="utf-8") == "def greet():\n    return 'hey'\n"
    # The refusal reached the model, and so did the file it then read.
    assert "has not been read in this task" in gateway.observations[0]
    assert "return 'hello'" in gateway.observations[1]


@pytest.mark.asyncio
async def test_the_step_budget_ends_a_looping_agent(tmp_path: Path) -> None:
    executor = ActionExecutor(EditTools(tmp_path))
    never_finishes = [
        AgentAction(thought="again", action="list_directory", path=".") for _ in range(10)
    ]
    gateway = ScriptedGateway(never_finishes)

    outcome = await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        max_steps=3,
    ).run("mission-1", context())

    assert outcome.steps == 3
    assert "Step budget exhausted" in outcome.implementation.summary
    assert len(never_finishes) == 7


def test_route_supports_tools_follows_the_selected_provider(tmp_path: Path) -> None:
    """The real probe, not a stub: it must read the routed provider's features.

    The loop chooses its dialect from this answer, so a role routed to a backend
    without a tool-call parser has to report False and take the structured path.
    """
    settings = Settings()
    settings.providers = {
        "local-ollama": ProviderConfig(
            type="ollama", base_url="http://127.0.0.1:11434/v1", model="qwen2.5-coder"
        ),
        # vLLM only parses tool calls when served with --tool-call-parser, so
        # its default features omit "tools".
        "local-vllm": ProviderConfig(
            type="vllm", base_url="http://127.0.0.1:8000/v1", model="local-coder"
        ),
    }
    settings.models = {
        "ollama": ModelProfileConfig(provider="local-ollama", model="qwen2.5-coder", local=True),
        "vllm": ModelProfileConfig(provider="local-vllm", model="local-coder", local=True),
    }
    settings.routing = {"builder": "ollama", "debugger": "vllm"}
    gateway = ModelGateway(settings, Database(settings, tmp_path))

    assert gateway.route_supports_tools(ModelRole.BUILDER) is True
    assert gateway.route_supports_tools(ModelRole.DEBUGGER) is False
    # A profile override has to re-route the probe, not just the completion.
    assert gateway.route_supports_tools(ModelRole.DEBUGGER, profile_override="ollama") is True


def test_profile_output_ceiling_reaches_the_provider(tmp_path: Path) -> None:
    """A profile that raises max_output_tokens must actually change the request.

    Declaring it on the profile and having the provider keep its own smaller
    value is the failure that makes a truncated reply look unfixable.
    """
    settings = Settings()
    settings.providers = {
        "cloud": ProviderConfig(
            type="openrouter",
            base_url="https://example.invalid/v1",
            model="small",
            max_output_tokens=4096,
        )
    }
    settings.models = {
        "big": ModelProfileConfig(provider="cloud", model="big-model", max_output_tokens=32_768)
    }
    settings.routing = {"builder": "big"}
    gateway = ModelGateway(settings, Database(settings, tmp_path))
    selection = gateway.router.select(ModelRole.BUILDER)

    resolved = _resolved_config(settings.providers["cloud"], selection)

    assert resolved.model == "big-model"
    assert resolved.max_output_tokens == 32_768
