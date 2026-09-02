"""Tool-loop harness: native tool calling with structured-JSON fallback."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from daino.agents.gateway import ModelGateway, _fit_messages, _message_tokens, _resolved_config
from daino.agents.loop import ToolLoop, _clip_bundle_sources, _message_estimate
from daino.config.models import ModelProfileConfig, ProviderConfig, Settings
from daino.context import ModelExecutionProfile
from daino.exceptions import ProviderError, ToolCallingUnsupported
from daino.model_router import ModelRole
from daino.persistence import Database
from daino.schemas import AgentAction, ContextBundle, LLMResponse, Message, ToolCall, ToolResult
from daino.tools import ActionExecutor, EditTools


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
async def test_single_model_recovers_from_stall_via_strategy_intervention(
    executor: ActionExecutor,
) -> None:
    """A gateway with no stronger model must still get a corrective nudge.

    This is the pinned-session / local-only case: there is exactly one model, so
    the loop cannot swap in a stronger one. It must recover by telling *that*
    model to change approach — not by pretending an escalation happened and then
    giving up. ``NativeGateway`` exposes no ``router`` and no ``profile_override``,
    so it models that single-model deployment directly.
    """

    class SingleModelGateway(NativeGateway):
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

    gateway = SingleModelGateway()
    outcome = await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        execution_profile=compact_execution_profile(),
    ).run("mission-stall", context())

    # No stronger model exists, so the routing context is never bumped to the
    # escalation trigger and no fake model swap is claimed.
    assert gateway.routing_attempts == [0, 0, 0, 0]
    assert outcome.escalated is False
    # The last thing the model saw before recovering was concrete corrective
    # guidance, not the old "escalation requested" placeholder.
    intervention = next(
        (
            message
            for message in reversed(gateway.seen_messages)
            if message.role == "system" and "Intervention" in message.content
        ),
        None,
    )
    assert intervention is not None
    assert "materially DIFFERENT" in intervention.content


@pytest.mark.asyncio
async def test_repeated_non_progress_escalates_when_a_stronger_model_exists(
    executor: ActionExecutor,
) -> None:
    """When the router can reach a stronger model, the stall swaps it in."""

    class StrongerModelRouter:
        def select(self, role: object, context: object = None):
            failed = getattr(context, "failed_attempts", 0)
            name = "big" if failed >= 2 else "small"
            return SimpleNamespace(profile_name=name)

    class EscalatingGateway(NativeGateway):
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
            self.router = StrongerModelRouter()
            self.profile_override = ""
            self.routing_attempts: list[int] = []

        async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
            routing = kwargs.get("routing_context")
            self.routing_attempts.append(getattr(routing, "failed_attempts", 0))
            return await super().complete(*args, **kwargs)

    gateway = EscalatingGateway()
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
async def test_a_retyped_command_still_resolves_the_one_failure_waiting(
    tmp_path: Path,
) -> None:
    """Exact argv equality asked a model to retype a long script perfectly.

    It cannot: the command that failed was a forty-line ``python3 -c`` script,
    and a single dropped quote left the failure permanently unresolvable — a
    real run stalled on this five times. With exactly one failure outstanding
    there is nothing to confuse it with, so an approximate reference resolves
    it. The evidence requirement is unchanged.
    """
    script = 'python3 -c "import app; print(app.check())"'

    class Runner:
        async def run(self, command: str, *, timeout: int | None = None) -> ToolResult:
            return ToolResult(
                tool="run_command",
                success=command != script,
                data={"command": command},
                error="Traceback: ImportError" if command == script else None,
            )

    gateway = ScriptedGateway(
        [
            AgentAction(thought="write", action="write", path="app.py", content="VALUE = 1\n"),
            AgentAction(thought="check", action="run_command", command=script),
            AgentAction(thought="simpler check", action="run_command", command="pytest -q"),
            # The model's recollection of its own script, quotes mangled.
            AgentAction(
                thought="the import check is covered by the suite",
                action="resolve_command_failure",
                command="python3 -c import app; print(app.check())",
                evidence_command="pytest -q",
            ),
            AgentAction(
                thought="done",
                action="finish",
                summary="fixed and checked",
                verification_commands=["pytest -q"],
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

    assert outcome.completed
    assert outcome.implementation.summary == "fixed and checked"


@pytest.mark.asyncio
async def test_an_unmatched_resolution_names_what_is_actually_waiting(
    tmp_path: Path,
) -> None:
    """The refusal has to be actionable, and quote the model rather than shlex."""

    class Runner:
        async def run(self, command: str, *, timeout: int | None = None) -> ToolResult:
            return ToolResult(
                tool="run_command",
                success=command == "pytest -q",
                data={"command": command},
                error=None if command == "pytest -q" else "boom",
            )

    gateway = ScriptedGateway(
        [
            AgentAction(thought="a", action="run_command", command="npm run build"),
            AgentAction(thought="b", action="run_command", command="npm test"),
            AgentAction(thought="c", action="run_command", command="pytest -q"),
            AgentAction(
                thought="wrong one",
                action="resolve_command_failure",
                command="cargo build",
                evidence_command="pytest -q",
            ),
            AgentAction(thought="done", action="finish", summary="stop"),
        ]
    )
    executor = ActionExecutor(
        EditTools(tmp_path, require_read_before_write=False),
        Runner(),  # type: ignore[arg-type]
    )

    await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        require_verified_finish=False,
    ).run("mission-1", context())

    refusal = next(item for item in gateway.observations if "No unresolved failed" in item)
    assert "cargo build" in refusal
    assert "npm run build" in refusal and "npm test" in refusal


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
    assert outcome.stop_reason == "step_budget"
    assert "Step budget exhausted" in outcome.implementation.summary
    assert len(never_finishes) == 7


def test_incomplete_message_distinguishes_step_budget_from_stall() -> None:
    from daino.agents import describe_incomplete_outcome
    from daino.agents.loop import BuilderOutcome
    from daino.schemas import Implementation

    step_budget = BuilderOutcome(
        implementation=Implementation(
            summary="Step budget exhausted before the agent emitted finish.",
            modifications=[],
            verification_commands=[],
        ),
        changed=[],
        steps=4,
        completed=False,
        stop_reason="step_budget",
    )
    message = describe_incomplete_outcome(step_budget, role_label="coding")
    assert "step limit" in message
    assert "max_agent_steps" in message

    stall = BuilderOutcome(
        implementation=Implementation(
            summary="Stopped after 5 actions that changed nothing, even after escalation.",
            modifications=[],
            verification_commands=[],
        ),
        changed=[],
        steps=4,
        completed=False,
        escalated=True,
        stop_reason="stall",
    )
    # A stall must NOT be reported as a step limit, and must not suggest max_agent_steps.
    pinned_message = describe_incomplete_outcome(stall, role_label="coding", pinned=True)
    assert "max_agent_steps" not in pinned_message
    assert "changed nothing" in pinned_message
    assert "pinned" in pinned_message
    # When not pinned, the pinning hint is omitted.
    assert "pinned" not in describe_incomplete_outcome(stall, pinned=False)


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


def test_authoritative_files_message_reflects_current_disk_content(tmp_path: Path) -> None:
    """After an edit, compaction must keep the file's real bytes in front of the model."""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path, require_read_before_write=False))
    loop = ToolLoop(executor=executor, gateway=NativeGateway([]), role=ModelRole.BUILDER)  # type: ignore[arg-type]

    message = loop._authoritative_files_message(["app.py"])
    assert message is not None
    assert message.role == "system"
    assert "value = 1" in message.content
    assert "from memory" in message.content

    # The block is read from disk, so a later edit is reflected, not a stale read.
    (tmp_path / "app.py").write_text("value = 2\n", encoding="utf-8")
    refreshed = loop._authoritative_files_message(["app.py"])
    assert refreshed is not None
    assert "value = 2" in refreshed.content
    assert "value = 1" not in refreshed.content


def test_authoritative_files_message_is_empty_when_nothing_changed(tmp_path: Path) -> None:
    executor = ActionExecutor(EditTools(tmp_path, require_read_before_write=False))
    loop = ToolLoop(executor=executor, gateway=NativeGateway([]), role=ModelRole.BUILDER)  # type: ignore[arg-type]
    assert loop._authoritative_files_message([]) is None
    # A path that no longer exists on disk is skipped rather than raising.
    assert loop._authoritative_files_message(["gone.py"]) is None


def test_authoritative_files_message_bounds_the_pinned_file_count(tmp_path: Path) -> None:
    from daino.agents.loop import _PINNED_FILE_LIMIT

    for index in range(_PINNED_FILE_LIMIT + 3):
        (tmp_path / f"f{index}.py").write_text(f"x = {index}\n", encoding="utf-8")
    executor = ActionExecutor(EditTools(tmp_path, require_read_before_write=False))
    loop = ToolLoop(executor=executor, gateway=NativeGateway([]), role=ModelRole.BUILDER)  # type: ignore[arg-type]

    changed = [f"f{index}.py" for index in range(_PINNED_FILE_LIMIT + 3)]
    message = loop._authoritative_files_message(changed)
    assert message is not None
    # Only the most-recently-changed files are pinned, newest first.
    assert message.content.count("### ") == _PINNED_FILE_LIMIT
    last = changed[-1]
    assert f"### {last}" in message.content


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
        "daino.agents.gateway.create_provider", lambda name, config: Provider(name)
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
    from daino.tools import RecordingActionExecutor

    content = "const books = [];\n"
    executor = RecordingActionExecutor(EditTools(tmp_path, require_read_before_write=False))
    # Script far more identical rewrites than the loop should ever take: the
    # intervention ladder is bounded, so it must stop well before running them
    # all rather than spinning through every scripted turn.
    turns = 40
    gateway = NativeGateway([rewrite_turn(index, content) for index in range(1, turns + 1)])
    loop = ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        execution_profile=compact_execution_profile(),
    )

    outcome = await loop.run("mission-loop", context())

    # The first write lands; the identical rewrites after it are not progress,
    # so after a bounded number of corrective interventions the loop gives up
    # instead of rewriting the same file forever.
    assert (tmp_path / "books-data.js").read_text(encoding="utf-8") == content
    assert outcome.steps < turns, (
        f"the loop never noticed the no-op rewrites ({outcome.steps} steps)"
    )
    assert not outcome.completed
    assert outcome.stop_reason == "stall"


@pytest.mark.asyncio
async def test_a_no_op_write_tells_the_agent_nothing_changed(tmp_path: Path) -> None:
    """Reporting bare success invites the agent to write the same file again."""
    from daino.tools import RecordingActionExecutor

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


def test_small_file_read_is_shown_whole_without_a_paging_banner() -> None:
    from daino.agents.loop import _read_file_detail

    body = "line one\nline two\nline three\n"
    result = ToolResult(
        tool="read_file",
        success=True,
        data={"content": body, "total_lines": 3, "start_line": 1, "end_line": 3},
    )
    detail = _read_file_detail(result)
    assert detail == body
    assert "truncated" not in detail
    assert "offset" not in detail


def test_large_file_read_is_truncated_with_an_actionable_paging_banner() -> None:
    """A silent cut is what makes a local model hallucinate unseen lines."""
    from daino.agents.loop import _READ_FILE_MAX_CHARS, _read_file_detail

    body = "".join(f"line {number}\n" for number in range(1, 2001))  # ~14k+ chars
    assert len(body) > _READ_FILE_MAX_CHARS
    result = ToolResult(
        tool="read_file",
        success=True,
        data={"content": body, "total_lines": 2000, "start_line": 1, "end_line": 2000},
    )
    detail = _read_file_detail(result)
    assert "content truncated" in detail
    # The model is told the file is bigger and exactly how to reach the rest.
    assert "of 2000" in detail
    assert "offset:" in detail
    assert "from memory" in detail
    # The visible slice really is bounded, not the whole file.
    assert len(detail) < len(body)


def test_partial_range_read_reports_its_position_in_the_file() -> None:
    from daino.agents.loop import _read_file_detail

    body = "middle chunk line\nmiddle chunk line\n"
    result = ToolResult(
        tool="read_file",
        success=True,
        data={"content": body, "total_lines": 480, "start_line": 200, "end_line": 201},
    )
    detail = _read_file_detail(result)
    assert "Showing lines 200-201 of 480" in detail
    assert "offset:202" in detail


def _compaction_loop(
    executor: ActionExecutor, budget: int, threshold: float = 0.8
) -> tuple[ToolLoop, list[tuple[int, int]]]:
    """A loop whose gateway reports a fixed context budget and records compactions."""
    events: list[tuple[int, int]] = []

    class Bus:
        def publish(self, event: object) -> None:
            events.append((event.before_tokens, event.after_tokens))  # type: ignore[attr-defined]

    class Gateway(NativeGateway):
        def __init__(self) -> None:
            super().__init__([])
            self.events = Bus()
            self.settings = SimpleNamespace(memory=SimpleNamespace(compaction_threshold=threshold))

        def context_budget(self, role: object, tools: object = None) -> int:
            return budget

    loop = ToolLoop(executor=executor, gateway=Gateway(), role=ModelRole.BUILDER)  # type: ignore[arg-type]
    return loop, events


def _grounded_context(source_chars: int) -> ContextBundle:
    files = {f"app/mod{index}.py": "x = 1\n" * (source_chars // 6) for index in range(4)}
    return ContextBundle(
        task="Build the backend",
        acceptance_criteria=["it runs"],
        files=files,
        included_paths=list(files),
    )


def _transcript(context: ContextBundle, turns: int) -> list[Message]:
    messages = [
        Message(role="system", content="system prompt " * 200),
        Message(role="user", content=context.model_dump_json(indent=2)),
    ]
    for index in range(turns):
        messages.append(Message(role="assistant", content=f"thought {index}: listing files"))
        messages.append(Message(role="tool", content="observation " * 200))
    return messages


def test_compaction_brings_an_oversized_transcript_under_the_threshold(
    executor: ActionExecutor,
) -> None:
    """The field failure: a transcript pinned above the threshold, compacting forever.

    A bundle sized against most of the input budget made the rebuilt transcript's
    floor exceed the threshold, so compaction fired every turn, reclaimed almost
    nothing, and handed the model a byte-identical prompt. The model repeated the
    same action until the no-progress guard failed the mission.
    """
    budget = 21_249  # A 32k window after its output reservation, as in the field.
    loop, events = _compaction_loop(executor, budget)
    # Sized as the field bundle was: ~16k tokens of inlined source, enough that
    # rebuilding at full fidelity alone overshoots the threshold.
    context = _grounded_context(16_000)
    messages = _transcript(context, turns=20)
    target = int(budget * 0.8)
    assert sum(_message_estimate(item) for item in messages) > target

    loop._maybe_compact_messages(messages, context, "mission-1", [])

    assert events, "an oversized transcript must actually compact"
    before, after = events[-1]
    assert after <= target
    assert after < before

    # And it converges: a second pass on the compacted transcript is a no-op,
    # rather than the every-turn churn that produced the fixed point.
    settled = list(messages)
    loop._maybe_compact_messages(messages, context, "mission-1", [])
    assert messages == settled
    assert len(events) == 1


def test_compaction_never_grows_the_transcript(executor: ActionExecutor) -> None:
    """Re-added scaffolding once turned a 15.4k transcript into a 26.6k one."""
    loop, events = _compaction_loop(executor, budget=1_000)
    # A huge bundle with a short transcript: rebuilding costs more than it saves.
    context = _grounded_context(40_000)
    messages = _transcript(context, turns=5)
    before = sum(_message_estimate(item) for item in messages)

    loop._maybe_compact_messages(messages, context, "mission-2", [])

    assert sum(_message_estimate(item) for item in messages) <= before
    assert all(after < grew for grew, after in events)


def test_compaction_clips_bundle_sources_before_dropping_working_state(
    executor: ActionExecutor,
) -> None:
    """Inlined source goes first: it is the largest term and read_file recovers it."""
    loop, _ = _compaction_loop(executor, budget=6_000)
    context = _grounded_context(20_000)
    messages = _transcript(context, turns=12)
    marker = messages[-1].content

    loop._maybe_compact_messages(messages, context, "mission-3", [])

    body = "\n".join(item.content for item in messages)
    assert "use read_file" in body, "the model must be told what was dropped"
    assert marker in body, "the newest observation is working state and must survive"


def test_clip_bundle_sources_keeps_head_and_tail_and_records_the_omission() -> None:
    context = ContextBundle(
        task="t",
        acceptance_criteria=["a"],
        files={"app/big.py": "HEAD\n" + ("filler\n" * 4_000) + "TAIL\n"},
        included_paths=["app/big.py"],
    )

    clipped = _clip_bundle_sources(context, 0.25)
    body = clipped.files["app/big.py"]
    assert body.startswith("HEAD")
    assert body.endswith("TAIL\n")
    assert len(body) < len(context.files["app/big.py"])
    assert any("app/big.py" in note for note in clipped.omitted_context)

    dropped = _clip_bundle_sources(context, 0.0)
    assert dropped.files == {}
    assert any("read_file" in note for note in dropped.omitted_context)
    # The originals are never mutated in place; each stage re-derives from them.
    assert context.files["app/big.py"].count("filler") == 4_000


def _stall_profile(no_progress_limit: int = 3) -> ModelExecutionProfile:
    return ModelExecutionProfile.resolve(
        "openrouter",
        ModelProfileConfig(
            provider="openrouter",
            model="glm",
            context_window=32_768,
            max_output_tokens=16_384,
            no_progress_limit=no_progress_limit,
        ),
        input_budget_tokens=21_249,
        project_budget_tokens=24_000,
        memory_items=6,
        memory_tokens=2_000,
    )


def _productive(index: int, count: int) -> list[AgentAction]:
    """Real work: each write lands different content in a different file."""
    return [
        AgentAction(thought="work", action="write", path=f"f{index}_{n}.txt", content=f"{index}{n}")
        for n in range(count)
    ]


def _stall_burst(size: int = 5) -> list[AgentAction]:
    """Enough repeats of one no-op action to trip the no-progress limit."""
    return [AgentAction(thought="stuck", action="list_directory", path=".") for _ in range(size)]


@pytest.mark.asyncio
async def test_a_long_run_is_not_killed_by_stalls_it_already_recovered_from(
    tmp_path: Path,
) -> None:
    """The intervention budget bounds consecutive failed corrections, not a whole run.

    It was only ever zeroed in __init__, so a long task that stalled and
    recovered four separate times — with real work in between — died on the
    fourth, claiming three corrections had failed when all three had worked.
    """
    executor = ActionExecutor(EditTools(tmp_path, require_read_before_write=False))
    actions: list[AgentAction] = []
    for index in range(6):
        actions += _productive(index, 6) + _stall_burst()
    actions.append(AgentAction(thought="done", action="finish", summary="all done"))

    outcome = await ToolLoop(
        ScriptedGateway(actions),  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        execution_profile=_stall_profile(),
    ).run("mission-long", context())

    assert outcome.completed, outcome.implementation.summary
    assert outcome.stop_reason == ""
    assert len(outcome.changed) == 36


@pytest.mark.asyncio
async def test_a_genuinely_stuck_run_still_gives_up(tmp_path: Path) -> None:
    """The refund must not disarm the guard the field case depends on."""
    executor = ActionExecutor(EditTools(tmp_path, require_read_before_write=False))
    actions = _stall_burst(200)
    actions.append(AgentAction(thought="done", action="finish", summary="unreachable"))

    outcome = await ToolLoop(
        ScriptedGateway(actions),  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        execution_profile=_stall_profile(),
    ).run("mission-stuck", context())

    assert not outcome.completed
    assert outcome.stop_reason == "stall"


@pytest.mark.asyncio
async def test_alternating_one_real_action_with_no_ops_cannot_farm_the_budget(
    tmp_path: Path,
) -> None:
    """A single good action must not refund the budget, or churn spins forever.

    Refunding on one action lets an agent alternate a token write with a burst of
    no-ops indefinitely — the exact loop the cumulative counter existed to stop.
    """
    executor = ActionExecutor(EditTools(tmp_path, require_read_before_write=False))
    actions: list[AgentAction] = []
    for index in range(40):
        actions += _productive(index, 1) + _stall_burst()
    actions.append(AgentAction(thought="done", action="finish", summary="unreachable"))

    outcome = await ToolLoop(
        ScriptedGateway(actions),  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        execution_profile=_stall_profile(),
    ).run("mission-churn", context())

    assert not outcome.completed
    assert outcome.stop_reason == "stall"
    # It stopped early rather than burning all 40 cycles of churn.
    assert outcome.steps < len(actions) // 2


def test_max_agent_steps_accepts_a_ceiling_for_a_genuinely_long_task() -> None:
    """The loop and TUI tell operators to raise this; the cap must allow it."""
    profile = ModelProfileConfig(provider="openrouter", model="glm", max_agent_steps=2_000)
    assert profile.max_agent_steps == 2_000
    # Zero still means unlimited, and runaway values are still rejected.
    assert ModelProfileConfig(provider="openrouter", model="glm").max_agent_steps == 0
    with pytest.raises(ValidationError):
        ModelProfileConfig(provider="openrouter", model="glm", max_agent_steps=10_001)
