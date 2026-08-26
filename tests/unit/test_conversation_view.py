"""Transcript rendering: incremental sync, and code that looks like code."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.app import App, ComposeResult
from textual.color import Color

from daino.application.view_models import ConversationItem
from daino.tui import palette
from daino.tui.highlight import guess_language, highlight_body, highlight_unified_diff
from daino.tui.widgets import ConversationView
from daino.tui.widgets.message import MessageCard


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


@pytest.mark.asyncio
async def test_pending_work_indicator_animates_and_stops() -> None:
    async with Harness().run_test(size=(80, 30)) as pilot:
        view = await mounted(pilot)
        await view.load_messages([])
        await view.begin_pending("thinking…")
        card = view._pending_card
        assert card is not None
        first = card.raw_content

        await pilot.pause(0.2)

        assert card.raw_content != first
        assert "thinking" in card.raw_content
        await view.clear_pending()
        assert view._pending_card is None


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


def test_common_message_kinds_use_quiet_inline_glyphs() -> None:
    user = MessageCard("inspect alpha", kind="user").render()
    tool = MessageCard("read compositor.ts", kind="tool").render()
    agent = MessageCard("Alpha is premultiplied.", kind="agent", duration=1.2).render()

    assert user.plain == "› inspect alpha"
    assert tool.plain == "… read compositor.ts"
    assert agent.plain == "Alpha is premultiplied.\n↳ 1.2s"


def test_unified_diff_uses_backgrounds_and_source_syntax() -> None:
    rendered = highlight_unified_diff(
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )

    assert "\n\n" not in rendered.plain
    backgrounds = {
        span.style.background
        for span in rendered.spans
        if getattr(span.style, "background", None) is not None
    }
    assert Color.parse(palette.DIFF_ADDED_BG) in backgrounds
    assert Color.parse(palette.DIFF_REMOVED_BG) in backgrounds
    foregrounds = {
        span.style.foreground
        for span in rendered.spans
        if getattr(span.style, "foreground", None) is not None
    }
    assert len(foregrounds) >= 3


def test_extensionless_build_files_have_a_lexer() -> None:
    assert guess_language("backend/Dockerfile") == "docker"
    assert guess_language("Dockerfile.production") == "docker"
    assert guess_language("Makefile") == "make"
    assert guess_language("public/index.php") == "php"


def test_deleted_file_diff_uses_the_old_path_language() -> None:
    rendered = highlight_unified_diff(
        "diff --git a/old.py b/old.py\n"
        "deleted file mode 100644\n"
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-value = 1\n"
    )

    foregrounds = {
        span.style.foreground
        for span in rendered.spans
        if getattr(span.style, "foreground", None) is not None
    }
    assert len(foregrounds) >= 3
