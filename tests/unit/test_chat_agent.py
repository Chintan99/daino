"""The chat agent: one loop that either answers or edits, plus diff rendering."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from textual.content import Content

from vasuki.agents.loop import ToolLoop
from vasuki.agents.tool_schemas import AGENT_TOOL_SPECS, CHAT_TOOL_SPECS, tool_call_to_action
from vasuki.application.mission_service import MissionApplicationService
from vasuki.model_router import ModelRole
from vasuki.prompts import CHAT_AGENT_SYSTEM
from vasuki.schemas import AgentAction, ContextBundle, LLMResponse, Message, ToolCall
from vasuki.tools import EditTools, RecordingActionExecutor
from vasuki.tools.diffing import build_file_diff, render, summarize
from vasuki.tui import palette
from vasuki.tui.widgets.message import DIFF_MAX_WIDTH, MessageCard, _diff_marker


@pytest.fixture()
def executor(tmp_path: Path) -> Iterator[RecordingActionExecutor]:
    yield RecordingActionExecutor(EditTools(tmp_path, require_read_before_write=False))


def context() -> ContextBundle:
    return ContextBundle(task="make it interactive", acceptance_criteria=["it changes"])


class ScriptedGateway:
    """Gateway double replaying a fixed action script through the structured path."""

    def __init__(self, actions: list[AgentAction]) -> None:
        self.actions = actions
        self.seen_systems: list[str] = []

    async def structured(
        self,
        mission_id: str,
        role: object,
        messages: Any,
        schema: type[Any],
        **kwargs: object,
    ) -> AgentAction:
        self.seen_systems.append(str(messages[0].content))
        return self.actions.pop(0)


# --------------------------------------------------------------------------
# respond vs finish
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_respond_ends_the_loop_and_carries_the_answer(
    executor: RecordingActionExecutor, tmp_path: Path
) -> None:
    gateway = ScriptedGateway(
        [AgentAction(thought="just answering", action="respond", message="It renders a header.")]
    )

    outcome = await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        system=CHAT_AGENT_SYSTEM,
        tools=CHAT_TOOL_SPECS,
    ).run("mission-1", context())

    assert outcome.answer == "It renders a header."
    assert outcome.changed == []
    assert outcome.steps == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_finish_after_edits_reports_changes_and_no_answer(
    executor: RecordingActionExecutor, tmp_path: Path
) -> None:
    (tmp_path / "landing.html").write_text("<h1>Hi</h1>\n", encoding="utf-8")
    gateway = ScriptedGateway(
        [
            AgentAction(
                thought="restyle",
                action="replace",
                path="landing.html",
                old_string="<h1>Hi</h1>",
                new_string='<h1 class="glass">Hi</h1>',
            ),
            AgentAction(thought="done", action="finish", summary="Applied the glass style."),
        ]
    )

    outcome = await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        system=CHAT_AGENT_SYSTEM,
        tools=CHAT_TOOL_SPECS,
    ).run("mission-1", context())

    assert outcome.answer == ""
    assert outcome.changed == ["landing.html"]
    assert outcome.implementation.summary == "Applied the glass style."
    assert 'class="glass"' in (tmp_path / "landing.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_chat_agent_uses_its_own_system_prompt(
    executor: RecordingActionExecutor,
) -> None:
    gateway = ScriptedGateway([AgentAction(thought="t", action="respond", message="hi")])

    await ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        system=CHAT_AGENT_SYSTEM,
        tools=CHAT_TOOL_SPECS,
    ).run("mission-1", context())

    assert gateway.seen_systems[0] == CHAT_AGENT_SYSTEM
    # The instruction that actually fixes the reported bug must survive edits.
    assert "writing the code into the file IS the answer" in CHAT_AGENT_SYSTEM


def test_respond_is_offered_to_the_chat_agent_but_not_the_builder() -> None:
    """A mission builder has no way to answer instead of building."""
    chat = {spec["function"]["name"] for spec in CHAT_TOOL_SPECS}
    builder = {spec["function"]["name"] for spec in AGENT_TOOL_SPECS}
    assert "respond" in chat
    assert "respond" not in builder
    assert builder < chat


def test_respond_tool_call_validates_into_an_action() -> None:
    action = tool_call_to_action(
        ToolCall(id="c1", name="respond", arguments={"thought": "t", "message": "the answer"})
    )
    assert action.action == "respond"
    assert action.message == "the answer"


@pytest.mark.asyncio
async def test_native_respond_tool_call_ends_the_loop() -> None:
    """The terminal check must work on the native tool-calling path too."""

    class NativeGateway:
        def route_supports_tools(self, role: object, ctx: object = None) -> bool:
            return True

        async def complete(self, *args: object, **kwargs: object) -> LLMResponse:
            return LLMResponse(
                content="",
                model="m",
                provider="p",
                tool_calls=[
                    ToolCall(id="c1", name="respond", arguments={"thought": "t", "message": "hi"})
                ],
            )

        async def structured(self, *args: object, **kwargs: object) -> AgentAction:
            raise AssertionError("must not fall back to structured")

    outcome = await ToolLoop(
        NativeGateway(),  # type: ignore[arg-type]
        ModelRole.BUILDER,
        RecordingActionExecutor(EditTools(Path("."), read_only=True)),
        system=CHAT_AGENT_SYSTEM,
        tools=CHAT_TOOL_SPECS,
    ).run("mission-1", context())

    assert outcome.answer == "hi"
    assert outcome.steps == 1


# --------------------------------------------------------------------------
# Recording the before-state
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_keeps_the_original_across_repeated_edits(tmp_path: Path) -> None:
    """Two edits to one file must still diff against the state before edit one."""
    target = tmp_path / "a.txt"
    target.write_text("original\n", encoding="utf-8")
    executor = RecordingActionExecutor(EditTools(tmp_path))

    await executor.execute(
        AgentAction(thought="t", action="write", path="a.txt", content="second\n")
    )
    await executor.execute(
        AgentAction(thought="t", action="write", path="a.txt", content="third\n")
    )

    assert executor.before["a.txt"] == "original\n"
    assert executor.after("a.txt") == "third\n"


@pytest.mark.asyncio
async def test_recorder_marks_a_new_file_as_absent_before(tmp_path: Path) -> None:
    executor = RecordingActionExecutor(EditTools(tmp_path))

    await executor.execute(AgentAction(thought="t", action="write", path="new.txt", content="x\n"))

    assert executor.before["new.txt"] is None


@pytest.mark.asyncio
async def test_recorder_ignores_reads(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    executor = RecordingActionExecutor(EditTools(tmp_path))

    await executor.execute(AgentAction(thought="t", action="read_file", path="a.txt"))

    assert executor.before == {}


# --------------------------------------------------------------------------
# Diff building and rendering
# --------------------------------------------------------------------------


def test_modified_file_diff_counts_and_numbers_lines() -> None:
    diff = build_file_diff("a.py", "a\nb\nc\n", "a\nB\nc\nd\n")

    assert diff.change == "modified"
    assert (diff.added, diff.removed) == (2, 1)
    assert [(line.marker, line.number, line.text) for line in diff.lines] == [
        (" ", 1, "a"),
        ("-", 2, "b"),
        ("+", 2, "B"),
        (" ", 3, "c"),
        ("+", 4, "d"),
    ]


def test_created_and_deleted_files_are_labelled() -> None:
    created = build_file_diff("n.py", None, "x = 1\n")
    deleted = build_file_diff("o.py", "x = 1\n", None)

    assert created.change == "created" and created.added == 1
    assert deleted.change == "deleted" and deleted.removed == 1
    assert summarize(created) == "Created with 1 lines"
    assert summarize(deleted) == "Deleted 1 lines"


def test_identical_content_reports_no_textual_change() -> None:
    diff = build_file_diff("a.py", "same\n", "same\n")
    assert diff.lines == []
    assert diff.note == "No textual change."


def test_unchanged_regions_are_elided() -> None:
    """A one-line edit in a long file shows context, not the whole file."""
    before = "\n".join(f"line{index}" for index in range(200)) + "\n"
    after = before.replace("line100", "changed")

    diff = build_file_diff("big.py", before, after)

    assert (diff.added, diff.removed) == (1, 1)
    # 3 lines of context either side of the single change.
    assert len(diff.lines) == 8
    assert {line.number for line in diff.lines if line.marker != " "} == {101}


def test_a_huge_diff_is_truncated_with_a_note() -> None:
    before = ""
    after = "\n".join(f"line{index}" for index in range(500)) + "\n"

    diff = build_file_diff("big.py", before, after)

    assert diff.added == 500
    assert len(diff.lines) == 120
    assert "first 120" in diff.note
    assert diff.note in render(diff)


def test_binary_file_is_reported_without_a_diff() -> None:
    diff = build_file_diff("logo.png", None, None)
    assert diff.lines == []
    assert "binary" in diff.note


def test_rendered_diff_lines_parse_back_to_their_markers() -> None:
    """The renderer and the TUI's colour parser must agree on the line format."""
    diff = build_file_diff("a.py", "a\nb\nc\n", "a\nB\nc\nd\n")
    body = render(diff).splitlines()

    assert body[0] == "a.py"
    assert body[1] == "Added 2 lines, removed 1 line"
    assert [_diff_marker(line) for line in body[2:]] == [" ", "-", "+", " ", "+"]


def test_a_context_line_starting_with_a_dash_is_not_read_as_a_removal() -> None:
    """Real code beginning with - or + must not be mistaken for a diff marker."""
    diff = build_file_diff("list.md", "- one\n- two\n", "- one\n- three\n")
    body = render(diff).splitlines()

    markers = [_diff_marker(line) for line in body[2:]]
    assert markers == [" ", "-", "+"]


@pytest.mark.asyncio
async def test_a_truncated_native_turn_is_nudged_toward_a_smaller_edit(
    executor: RecordingActionExecutor, tmp_path: Path
) -> None:
    """A cut-off turn produced no tool call; retrying it unchanged would repeat."""
    (tmp_path / "landing.html").write_text("<h1>Hi</h1>\n", encoding="utf-8")
    turns: list[list[Message]] = []

    class TruncatingGateway:
        def __init__(self) -> None:
            self.calls = 0

        def route_supports_tools(self, role: object, ctx: object = None) -> bool:
            return True

        async def complete(self, mission_id: str, role: object, messages: Any, **kw: object) -> Any:
            self.calls += 1
            turns.append(list(messages))
            if self.calls == 1:
                # Cut off while writing the whole file: no usable tool call.
                return LLMResponse(
                    content="", model="m", provider="p", finish_reason="length", tool_calls=[]
                )
            return LLMResponse(
                content="",
                model="m",
                provider="p",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="replace",
                        arguments={
                            "thought": "smaller edit",
                            "path": "landing.html",
                            "old_string": "<h1>Hi</h1>",
                            "new_string": '<h1 class="glass">Hi</h1>',
                        },
                    )
                ],
            )

        async def structured(self, *args: object, **kwargs: object) -> AgentAction:
            raise AssertionError("a truncated turn must not fall back to structured JSON")

    gateway = TruncatingGateway()
    loop = ToolLoop(
        gateway,  # type: ignore[arg-type]
        ModelRole.BUILDER,
        executor,
        system=CHAT_AGENT_SYSTEM,
        tools=CHAT_TOOL_SPECS,
        max_steps=3,
    )
    await loop.run("mission-1", context())

    # The second turn carries the nudge, and the smaller edit then landed.
    assert "cut off at the output token limit" in turns[1][-1].content
    assert 'class="glass"' in (tmp_path / "landing.html").read_text(encoding="utf-8")


def test_write_is_documented_as_new_files_only() -> None:
    """The guidance that prevents whole-file rewrites must survive prompt edits."""
    write = next(spec for spec in CHAT_TOOL_SPECS if spec["function"]["name"] == "write")
    assert "NEW file" in write["function"]["description"]
    assert "Prefer replace" in write["function"]["description"]
    assert "exceeds the output limit" in CHAT_AGENT_SYSTEM
    assert "several replace actions, not one giant write" in CHAT_AGENT_SYSTEM


@pytest.mark.asyncio
async def test_an_unavailable_runtime_does_not_report_the_edit_as_failed(
    tmp_path: Path,
) -> None:
    """Docker missing means the checks never ran; the edit itself was fine."""
    from vasuki.schemas import ChatOutcome

    service = MissionApplicationService.__new__(MissionApplicationService)
    recorded: list[tuple[str, str]] = []
    service.add_message = lambda session_id, **kw: recorded.append(  # type: ignore[method-assign]
        (kw["kind"], kw["content"])
    )
    service.update_verification_todo = lambda *args, **kwargs: None  # type: ignore[method-assign]
    service._session_gate = lambda session_id: object()  # type: ignore[method-assign]

    class Boom:
        def __init__(self, context: object) -> None: ...

        async def run(self, commands: list[str], **kwargs: object) -> object:
            raise RuntimeError("Docker is not installed or not on PATH")

    import vasuki.application.verification_service as verification

    original = verification.VerificationApplicationService
    verification.VerificationApplicationService = Boom  # type: ignore[misc]
    try:
        outcome = ChatOutcome(mission_id="m1", summary="did a thing", changed=["a.py"])
        await service._verify_chat_edit(outcome, ["pytest -q"], "session-1", "m1")
    finally:
        verification.VerificationApplicationService = original  # type: ignore[misc]

    # Unverified, not failed.
    assert outcome.verified is None
    kind, content = recorded[-1]
    assert kind == "status"
    assert "verification was skipped" in content
    assert "pytest -q" in content
    assert "/runtime local" in content


# --------------------------------------------------------------------------
# Diff colouring in the TUI
# --------------------------------------------------------------------------


def _spans(text: str) -> list[tuple[str, str]]:
    card = MessageCard.__new__(MessageCard)
    card.raw_content = text
    return [part for part in card._diff_spans() if isinstance(part, tuple)]


def _diff_parts(text: str) -> list[str | Content | tuple[str, str]]:
    card = MessageCard.__new__(MessageCard)
    card.raw_content = text
    return card._diff_spans()


def _changed_code(
    parts: list[str | Content | tuple[str, str]], marker: str
) -> list[Content]:
    return [
        parts[index + 1]
        for index, part in enumerate(parts[:-1])
        if isinstance(part, tuple)
        and part[0] == f"{marker} "
        and isinstance(parts[index + 1], Content)
    ]


def test_added_and_removed_lines_get_filled_backgrounds() -> None:
    """Change meaning comes from the fill, not green/red source text."""
    body = render(build_file_diff("a.py", "a\nb\nc\n", "a\nB\nc\nd\n"))
    marker_styles = {
        text: style
        for text, style in _spans(body)
        if text in {"+ ", "- "}
    }

    assert marker_styles["+ "] == f"{palette.TEXT} on {palette.DIFF_ADDED_BG}"
    assert marker_styles["- "] == f"{palette.TEXT} on {palette.DIFF_REMOVED_BG}"
    # Context lines stay unfilled so the change is what stands out.
    assert any(style == palette.DIFF_CONTEXT for _, style in _spans(body))


def test_the_gutter_is_styled_apart_from_the_code() -> None:
    body = render(build_file_diff("a.py", "a\n", "B\n"))
    spans = _spans(body)

    gutters = [text for text, style in spans if style.startswith(palette.DIFF_GUTTER)]
    bodies = [text for text, style in spans if text == "- " and palette.TEXT in style]
    assert gutters and gutters[0].strip() == "1"
    assert bodies == ["- "]


def test_diff_code_uses_the_file_language_highlighter() -> None:
    parts = _diff_parts(render(build_file_diff("a.py", "value = 1\n", "value = 2\n")))
    added = _changed_code(parts, "+")

    assert added
    foregrounds = {
        str(span.style.foreground)
        for span in added[0].spans
        if getattr(span.style, "foreground", None) is not None
    }
    assert len(foregrounds) >= 2


def test_changed_lines_are_padded_into_an_even_block() -> None:
    """Ragged fills look like stripes; equal width reads as a block."""
    before = "short\n"
    after = "a considerably longer replacement line\n"
    parts = _diff_parts(render(build_file_diff("a.py", before, after)))
    bodies = [item.plain for marker in ("+", "-") for item in _changed_code(parts, marker)]
    assert len(bodies) == 2
    assert len({len(text) for text in bodies}) == 1, bodies


def test_one_long_line_does_not_pad_every_other_line_out_to_match() -> None:
    """The cap bounds padding, and must never truncate the code itself."""
    after = "x" * 400 + "\nshort\n"
    parts = _diff_parts(render(build_file_diff("a.py", "", after)))
    bodies = [item.plain for item in _changed_code(parts, "+")]

    long_line = max(bodies, key=len)
    short_line = min(bodies, key=len)
    # The long line survives intact...
    assert "x" * 400 in long_line
    # ...but the short one is padded to the cap, not out to 400.
    assert len(short_line) <= DIFF_MAX_WIDTH


def test_the_header_lines_are_never_filled() -> None:
    spans = _spans(render(build_file_diff("a.py", "a\n", "b\n")))
    path_style = spans[0][1]
    assert " on " not in path_style
    assert spans[0][0] == "a.py"


@pytest.mark.asyncio
async def test_each_mutation_records_its_own_before_and_after(tmp_path: Path) -> None:
    """Per-edit diffs need the state either side of that edit, not of the run."""
    target = tmp_path / "a.txt"
    target.write_text("one\n", encoding="utf-8")
    executor = RecordingActionExecutor(EditTools(tmp_path))

    await executor.execute(AgentAction(thought="t", action="write", path="a.txt", content="two\n"))
    first = executor.last_edit
    await executor.execute(
        AgentAction(thought="t", action="write", path="a.txt", content="three\n")
    )
    second = executor.last_edit

    assert first == ("a.txt", "one\n", "two\n")
    assert second == ("a.txt", "two\n", "three\n")
    # The run-level baseline still points at the original.
    assert executor.before["a.txt"] == "one\n"


@pytest.mark.asyncio
async def test_a_failed_edit_records_no_change(tmp_path: Path) -> None:
    """A rejected edit must not post a diff for something that did not happen."""
    executor = RecordingActionExecutor(EditTools(tmp_path, ["allowed/**"]))

    result, _ = await executor.execute(
        AgentAction(thought="t", action="write", path="denied/a.txt", content="x")
    )

    assert not result.success
    assert executor.last_edit is None


@pytest.mark.asyncio
async def test_a_read_clears_the_previous_edit(tmp_path: Path) -> None:
    """last_edit must describe the current action, not linger from an earlier one."""
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    executor = RecordingActionExecutor(EditTools(tmp_path))

    await executor.execute(AgentAction(thought="t", action="write", path="a.txt", content="two\n"))
    assert executor.last_edit is not None
    await executor.execute(AgentAction(thought="t", action="read_file", path="a.txt"))

    assert executor.last_edit is None
