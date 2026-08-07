"""Transcript rendering: incremental sync, and code that looks like code."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.app import App, ComposeResult

from vasuki.application.view_models import ConversationItem
from vasuki.tui.highlight import highlight_body
from vasuki.tui.widgets import ConversationView
from vasuki.tui.widgets.message import MessageCard


def item(identifier: str, kind: str = "agent", content: str = "hello") -> ConversationItem:
    return ConversationItem(
        id=identifier,
        kind=kind,
        role="builder",
        content=content,
        created_at=datetime.now(UTC),
    )


class Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield ConversationView(id="chat-view")


async def mounted(pilot: object) -> ConversationView:
    return pilot.app.query_one("#chat-view", ConversationView)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_sync_mounts_only_what_is_new() -> None:
    """Reloading the transcript each turn cost a mount per message.

    The cost grew with the length of the conversation until sending anything
    stalled, so a refresh must touch only the tail.
    """
    async with Harness().run_test(size=(80, 30)) as pilot:
        view = await mounted(pilot)
        first = [item(f"m{index}") for index in range(6)]
        await view.load_messages(first)
        before = list(view.query(MessageCard))

        await view.sync_messages([*first, item("m6"), item("m7")])
        after = list(view.query(MessageCard))

        assert len(after) == 8
        # The original cards are the same objects: they were never remounted.
        assert after[:6] == before


@pytest.mark.asyncio
async def test_sync_is_a_no_op_when_nothing_changed() -> None:
    async with Harness().run_test(size=(80, 30)) as pilot:
        view = await mounted(pilot)
        items = [item(f"m{index}") for index in range(4)]
        await view.load_messages(items)
        before = list(view.query(MessageCard))

        await view.sync_messages(items)

        assert list(view.query(MessageCard)) == before


@pytest.mark.asyncio
async def test_live_cards_are_replaced_by_their_persisted_copies() -> None:
    """An edit shown as it lands must not appear twice once it is persisted.

    Each edit is rendered immediately from a FileChanged event so the user sees
    work happening, and the service also persists it. Without reconciliation the
    transcript showed every diff two times.
    """
    async with Harness().run_test(size=(80, 30)) as pilot:
        view = await mounted(pilot)
        await view.load_messages([item("m0", kind="user", content="edit it")])

        # Live feedback during the turn, not backed by a persisted id.
        await view.add_message("app.py  +1 -1", kind="diff")
        assert len(view.query(MessageCard)) == 2

        await view.sync_messages(
            [
                item("m0", kind="user", content="edit it"),
                item("m1", kind="diff", content="app.py  +1 -1"),
            ]
        )

        cards = list(view.query(MessageCard))
        assert [card.kind for card in cards] == ["user", "diff"]


@pytest.mark.asyncio
async def test_a_rewritten_history_falls_back_to_a_full_reload() -> None:
    async with Harness().run_test(size=(80, 30)) as pilot:
        view = await mounted(pilot)
        await view.load_messages([item("a0"), item("a1")])

        await view.sync_messages([item("b0"), item("b1"), item("b2")])

        assert len(view.query(MessageCard)) == 3


def test_fenced_code_is_highlighted_and_prose_is_not() -> None:
    pieces = highlight_body("Look:\n```python\ndef f():\n    return 'x'\n```\ndone", "#b6bcc7")
    styles = {str(span.style) for piece in pieces for span in piece.spans}

    assert len(pieces) == 3  # prose, code, trailing prose
    # The code block carries several token colours, not one flat body style.
    assert len(styles) > 2
    assert "".join(piece.plain for piece in pieces).startswith("Look:")


def test_a_message_without_code_is_a_single_span() -> None:
    pieces = highlight_body("just prose", "#b6bcc7")

    assert len(pieces) == 1
    assert pieces[0].plain == "just prose"


def test_an_unknown_language_still_renders_the_code() -> None:
    pieces = highlight_body("```wat\nsome ?? text\n```", "#b6bcc7")

    assert "some ?? text" in "".join(piece.plain for piece in pieces)


def test_an_unterminated_fence_highlights_what_has_arrived() -> None:
    """A streamed answer is mid-block for most of its life."""
    pieces = highlight_body("Here:\n```python\ndef f():", "#b6bcc7")

    assert "def f():" in "".join(piece.plain for piece in pieces)
