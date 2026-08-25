"""Tool-loop harness: native tool calling with structured-JSON fallback."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from vasuki.agents.gateway import ModelGateway, _fit_messages, _message_tokens, _resolved_config
from vasuki.agents.loop import ToolLoop
from vasuki.config.models import ModelProfileConfig, ProviderConfig, Settings
from vasuki.context import ModelExecutionProfile
from vasuki.exceptions import ProviderError, ToolCallingUnsupported
from vasuki.model_router import ModelRole
from vasuki.persistence import Database
from vasuki.schemas import AgentAction, ContextBundle, LLMResponse, Message, ToolCall, ToolResult
from vasuki.tools import ActionExecutor, EditTools


@pytest.fixture()
def executor(tmp_path: Path) -> Iterator[ActionExecutor]:
    yield ActionExecutor(EditTools(tmp_path, require_read_before_write=False))


def context() -> ContextBundle:
    return ContextBundle(task="Add greeter", acceptance_criteria=["greeter exists"])


def tool_response(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(content="", model="mock", provider="mock", tool_calls=list(calls))


def compact_execution_profile() -> ModelExecutionProfile:
    return ModelExecutionProfile.resolve(
        "tiny",
        ModelProfileConfig(
            provider="local",
            model="tiny",
            local=True,
            execution_mode="compact",
        ),
        input_budget_tokens=8_000,
        project_budget_tokens=24_000,
        memory_items=8,
        memory_tokens=2_000,
    )


class NativeGateway:
    """Gateway double that answers with native tool calls."""

    def __init__(self, turns: list[list[ToolCall]]) -> None:
        self.turns = turns
        self.complete_calls = 0
        self.structured_calls = 0
        #: Messages as the loop last presented them, for asserting on what the
        #: agent was actually told about its own actions.
        self.seen_messages: list[Message] = []

    def route_supports_tools(self, role: object, context: object = None) -> bool:
        return True

    async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
        self.complete_calls += 1
        for value in (*args, *kwargs.values()):
            if isinstance(value, list) and all(isinstance(item, Message) for item in value):
                self.seen_messages = list(value)
                break
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
async def test_action_start_callback_fires_before_completion(
    executor: ActionExecutor, tmp_path: Path
) -> None:
    gateway = NativeGateway(
        [
            [
                ToolCall(
                    id="call_1",
                    name="write",
                    arguments={"thought": "private", "path": "live.py", "content": "x = 1\n"},
                )
            ],
            [
                ToolCall(
                    id="call_2",
                    name="finish",
                    arguments={"thought": "done", "summary": "Finished"},
                )
            ],
        ]
    )
    lifecycle: list[str] = []
    loop = ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        on_action_start=lambda action: lifecycle.append(f"start:{action.path}"),
    )

    await loop.run(
        "mission-live",
        context(),
        on_action=lambda action, _result, _paths: lifecycle.append(f"done:{action.path}"),
    )

    assert lifecycle == ["start:live.py", "done:live.py"]
    assert (tmp_path / "live.py").exists()


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


@pytest.mark.asyncio
async def test_compact_profile_executes_one_bounded_tool_call_per_turn(
    executor: ActionExecutor, tmp_path: Path
) -> None:
    gateway = NativeGateway(
        [
            [
                ToolCall(
                    id="call_1",
                    name="write",
                    arguments={"thought": "first", "path": "one.py", "content": "x = 1\n"},
                ),
                ToolCall(
                    id="call_2",
                    name="write",
                    arguments={"thought": "second", "path": "two.py", "content": "y = 2\n"},
                ),
            ],
            [
                ToolCall(
                    id="call_3",
                    name="write",
                    arguments={"thought": "retry", "path": "two.py", "content": "y = 2\n"},
                )
            ],
            [ToolCall(id="call_4", name="finish", arguments={"thought": "done", "summary": "ok"})],
        ]
    )
    outcome = await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        execution_profile=compact_execution_profile(),
    ).run("mission-compact", context())

    assert outcome.steps == 3
    assert outcome.changed == ["one.py", "two.py"]
    assert (tmp_path / "one.py").exists()
    assert (tmp_path / "two.py").exists()


@pytest.mark.asyncio
async def test_repeated_non_progress_signals_model_escalation(executor: ActionExecutor) -> None:
    class EscalationGateway(NativeGateway):
        def __init__(self) -> None:
            super().__init__(
                [
                    [
                        ToolCall(
                            id=f"missing_{number}",
                            name="read_file",
                            arguments={"thought": "inspect", "path": "missing.py"},
                        )
                    ]
                    for number in range(3)
                ]
                + [
                    [
                        ToolCall(
                            id="finish",
                            name="finish",
                            arguments={"thought": "blocked", "summary": "Could not inspect"},
                        )
                    ]
                ]
            )
            self.routing_attempts: list[int] = []

        async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
            routing = kwargs.get("routing_context")
            self.routing_attempts.append(getattr(routing, "failed_attempts", 0))
            return await super().complete(*args, **kwargs)

    gateway = EscalationGateway()
    outcome = await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        execution_profile=compact_execution_profile(),
    ).run("mission-escalate", context())

    assert outcome.escalated is True
    assert "consecutive" in outcome.escalation_reason
    assert gateway.routing_attempts == [0, 0, 0, 2]


class RejectingToolsGateway:
    """Advertises tool support but the server rejects the tools parameter."""

    def __init__(self, actions: list[AgentAction]) -> None:
        self.actions = actions
        self.complete_calls = 0

    def route_supports_tools(self, role: object, context: object = None) -> bool:
        return True

    async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
        self.complete_calls += 1
        raise ToolCallingUnsupported("mock rejected the request (HTTP 400): tools not supported")

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


@pytest.mark.asyncio
async def test_finish_is_rejected_after_a_failed_call_in_the_same_turn(
    executor: ActionExecutor, tmp_path: Path
) -> None:
    gateway = NativeGateway(
        [
            [
                ToolCall(
                    id="call_1",
                    name="replace",
                    arguments={
                        "thought": "Guess an edit.",
                        "path": "missing.py",
                        "old_string": "x",
                        "new_string": "y",
                    },
                ),
                ToolCall(
                    id="call_2",
                    name="finish",
                    arguments={"thought": "Done.", "summary": "Changed it"},
                ),
            ],
            [
                ToolCall(
                    id="call_3",
                    name="write",
                    arguments={"thought": "Create it.", "path": "fixed.py", "content": "x = 1\n"},
                )
            ],
            [
                ToolCall(
                    id="call_4",
                    name="finish",
                    arguments={"thought": "Done.", "summary": "Fixed"},
                )
            ],
        ]
    )

    outcome = await ToolLoop(gateway, ModelRole.BUILDER, executor).run("mission-1", context())  # type: ignore[arg-type]

    assert outcome.steps == 3
    assert outcome.implementation.summary == "Fixed"
    assert outcome.changed == ["fixed.py"]
    assert not (tmp_path / "missing.py").exists()


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
async def test_chat_finish_requires_its_failed_check_to_pass_after_correction(
    tmp_path: Path,
) -> None:
    class FlakyRunner:
        calls = 0

        async def run(self, command: str, *, timeout: int | None = None) -> ToolResult:
            self.calls += 1
            return ToolResult(
                tool="run_command",
                success=self.calls > 1,
                data={"command": command, "exit_code": 0 if self.calls > 1 else 1},
                error=None if self.calls > 1 else "check failed",
            )

    gateway = ScriptedGateway(
        [
            AgentAction(thought="write", action="write", path="app.py", content="VALUE = 1\n"),
            AgentAction(thought="check", action="run_command", command="python -m compileall -q ."),
            AgentAction(
                thought="done",
                action="finish",
                summary="complete",
                verification_commands=["python -m compileall -q ."],
            ),
            AgentAction(thought="retry", action="run_command", command="python -m compileall -q ."),
            AgentAction(
                thought="done",
                action="finish",
                summary="verified",
                verification_commands=["python -m compileall -q ."],
            ),
        ]
    )
    executor = ActionExecutor(
        EditTools(tmp_path, require_read_before_write=False),
        FlakyRunner(),  # type: ignore[arg-type]
    )

    outcome = await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        require_verified_finish=True,
    ).run("mission-1", context())

    assert outcome.steps == 5
    assert outcome.implementation.summary == "verified"
    assert any("proposed verification commands failed" in item for item in gateway.observations)


@pytest.mark.asyncio
async def test_finish_waits_for_every_part_of_a_rejected_shell_chain(
    tmp_path: Path,
) -> None:
    class Runner:
        async def run(self, command: str, *, timeout: int | None = None) -> ToolResult:
            if "&&" in command:
                return ToolResult(
                    tool="run_command",
                    success=False,
                    error="Refused: shell syntax is not available: &&",
                )
            return ToolResult(tool="run_command", success=True, data={"command": command})

    gateway = ScriptedGateway(
        [
            AgentAction(thought="write", action="write", path="app.py", content="VALUE = 1\n"),
            AgentAction(
                thought="inspect",
                action="run_command",
                command="git diff --stat && git diff -- app.py",
            ),
            AgentAction(
                thought="verify",
                action="run_command",
                command="python -m py_compile app.py",
            ),
            AgentAction(
                thought="done",
                action="finish",
                summary="fixed",
                verification_commands=["python -m py_compile app.py"],
            ),
            AgentAction(thought="retry first", action="run_command", command="git diff --stat"),
            AgentAction(
                thought="retry second",
                action="run_command",
                command="git diff -- app.py",
            ),
            AgentAction(
                thought="done",
                action="finish",
                summary="fixed and fully checked",
                verification_commands=["python -m py_compile app.py"],
            ),
        ]
    )
    executor = ActionExecutor(
        EditTools(tmp_path, require_read_before_write=False),
        Runner(),  # type: ignore[arg-type]
    )

    outcome = await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        require_verified_finish=True,
    ).run("mission-1", context())

    assert outcome.steps == 7
    assert outcome.implementation.summary == "fixed and fully checked"
    assert any("command errors remain unresolved" in item for item in gateway.observations)


@pytest.mark.asyncio
async def test_a_successful_equivalent_command_can_resolve_an_environment_failure(
    tmp_path: Path,
) -> None:
    class Runner:
        async def run(self, command: str, *, timeout: int | None = None) -> ToolResult:
            return ToolResult(
                tool="run_command",
                success=command != "npm run build",
                data={"command": command},
                error="Executable not found: npm" if command == "npm run build" else None,
            )

    gateway = ScriptedGateway(
        [
            AgentAction(thought="write", action="write", path="app.py", content="VALUE = 1\n"),
            AgentAction(thought="host build", action="run_command", command="npm run build"),
            AgentAction(
                thought="container build",
                action="run_command",
                command="docker compose build web",
            ),
            AgentAction(
                thought="done",
                action="finish",
                summary="fixed",
                verification_commands=["docker compose build web"],
            ),
            AgentAction(
                thought="same build through the project environment",
                action="resolve_command_failure",
                command="npm run build",
                evidence_command="docker compose build web",
            ),
            AgentAction(
                thought="done",
                action="finish",
                summary="fixed and checked in Docker",
                verification_commands=["docker compose build web"],
            ),
        ]
    )
    executor = ActionExecutor(
        EditTools(tmp_path, require_read_before_write=False),
        Runner(),  # type: ignore[arg-type]
    )

    outcome = await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        require_verified_finish=True,
    ).run("mission-1", context())

    assert outcome.steps == 6
    assert outcome.implementation.summary == "fixed and checked in Docker"
    assert any("command errors remain unresolved" in item for item in gateway.observations)


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
    assert not outcome.completed
    assert "Step budget exhausted" in outcome.implementation.summary
    assert len(never_finishes) == 7


@pytest.mark.asyncio
async def test_default_loop_can_continue_past_the_old_24_step_limit(tmp_path: Path) -> None:
    executor = ActionExecutor(EditTools(tmp_path))
    actions = [
        AgentAction(thought="continue", action="list_directory", path=".")
        for _ in range(24)
    ]
    actions.append(AgentAction(thought="done", action="finish", summary="finished"))
    gateway = ScriptedGateway(actions)

    outcome = await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
    ).run("mission-1", context())

    assert outcome.completed
    assert outcome.steps == 25
    assert outcome.implementation.summary == "finished"


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
        "big": ModelProfileConfig(
            provider="cloud",
            model="big-model",
            context_window=65_536,
            max_output_tokens=32_768,
        )
    }
    settings.routing = {"builder": "big"}
    gateway = ModelGateway(settings, Database(settings, tmp_path))
    selection = gateway.router.select(ModelRole.BUILDER)

    resolved = _resolved_config(settings.providers["cloud"], selection)

    assert resolved.model == "big-model"
    assert resolved.max_output_tokens == 32_768


def test_context_compaction_keeps_task_and_complete_recent_tool_exchange() -> None:
    messages = [
        Message(role="system", content="system"),
        Message(role="user", content="old question " * 200),
        Message(role="assistant", content="old answer " * 200),
        Message(role="user", content="current task " * 40),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="recent", name="read_file", arguments={"path": "a.py"})],
        ),
        Message(role="tool", content="recent observation " * 300, tool_call_id="recent"),
    ]

    fitted = _fit_messages(messages, 350)

    assert fitted[0].role == "system"
    assert any("current task" in item.content for item in fitted)
    assert not any("old question" in item.content for item in fitted)
    recent = [item for item in fitted if item.tool_call_id == "recent"]
    assert recent and any(call.id == "recent" for item in fitted for call in item.tool_calls)
    assert "oversized context omitted" in recent[0].content
    assert sum(_message_tokens(item) for item in fitted) <= 350


@pytest.mark.asyncio
async def test_gateway_uses_configured_fallback_after_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = configured_gateway_settings()
    database = RecordingDatabase()
    attempts: list[str] = []

    class Provider:
        def __init__(self, name: str) -> None:
            self.name = name

        def supports_tools(self) -> bool:
            return False

        async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
            attempts.append(self.name)
            if self.name == "local":
                raise ProviderError("local server is unavailable")
            return LLMResponse(content="ok", model="strong", provider=self.name)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "vasuki.agents.gateway.create_provider", lambda name, config: Provider(name)
    )
    response = await ModelGateway(settings, database).complete(  # type: ignore[arg-type]
        "mission-1", ModelRole.BUILDER, [Message(role="user", content="work")]
    )

    assert response.content == "ok"
    assert attempts == ["local", "cloud"]
    assert [record.success for record in database.records] == [False, True]


def configured_gateway_settings() -> Settings:
    settings = Settings()
    settings.providers = {
        "local": ProviderConfig(type="ollama", base_url="http://local/v1", model="small"),
        "cloud": ProviderConfig(
            type="openrouter", base_url="https://cloud.invalid/v1", model="strong"
        ),
    }
    settings.models = {
        "small": ModelProfileConfig(provider="local", model="small", local=True),
        "strong": ModelProfileConfig(provider="cloud", model="strong"),
    }
    settings.routing = {"builder": "small"}
    settings.routing_fallbacks = {"builder": ["strong"]}
    return settings


class RecordingDatabase:
    def __init__(self) -> None:
        self.records: list[Any] = []

    @contextmanager
    def session(self) -> Iterator[RecordingDatabase]:
        yield self

    def add(self, record: Any) -> None:
        self.records.append(record)


def rewrite_turn(index: int, content: str) -> list[ToolCall]:
    return [
        ToolCall(
            id=f"call_{index}",
            name="write",
            arguments={
                "thought": "Write the data file.",
                "path": "books-data.js",
                "content": content,
            },
        )
    ]


@pytest.mark.asyncio
async def test_rewriting_identical_content_counts_as_no_progress(tmp_path: Path) -> None:
    """A write that changes nothing used to reset the no-progress counter.

    Because a successful mutation reports the path it touched, an agent that had
    compacted away the memory of its own work could rewrite the same file with
    byte-identical content forever without ever tripping the no-progress limit.
    Observed in the field as an agent writing books-data.js over and over while
    every diff read "No textual change."
    """
    from vasuki.tools import RecordingActionExecutor

    content = "const books = [];\n"
    executor = RecordingActionExecutor(EditTools(tmp_path, require_read_before_write=False))
    gateway = NativeGateway([rewrite_turn(index, content) for index in range(1, 9)])
    loop = ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        execution_profile=compact_execution_profile(),
    )

    outcome = await loop.run("mission-loop", context())

    # The first write lands; the identical rewrites after it are not progress,
    # so the loop stops instead of spinning through every scripted turn.
    assert (tmp_path / "books-data.js").read_text(encoding="utf-8") == content
    assert outcome.steps < 8, f"the loop never noticed the no-op rewrites ({outcome.steps} steps)"


@pytest.mark.asyncio
async def test_a_no_op_write_tells_the_agent_nothing_changed(tmp_path: Path) -> None:
    """Reporting bare success invites the agent to write the same file again."""
    from vasuki.tools import RecordingActionExecutor

    content = "const books = [];\n"
    (tmp_path / "books-data.js").write_text(content, encoding="utf-8")
    executor = RecordingActionExecutor(EditTools(tmp_path, require_read_before_write=False))
    gateway = NativeGateway(
        [
            rewrite_turn(1, content),
            [
                ToolCall(
                    id="call_2",
                    name="finish",
                    arguments={"thought": "Done.", "summary": "Nothing to do"},
                )
            ],
        ]
    )
    loop = ToolLoop(gateway, ModelRole.BUILDER, executor)  # type: ignore[arg-type]

    await loop.run("mission-noop", context())

    observations = [
        message.content for message in gateway.seen_messages if message.role == "tool"
    ]
    assert any("already contained exactly this content" in item for item in observations), (
        observations
    )
