"""Scrollable persisted conversation."""

from __future__ import annotations

from typing import Any

from textual.containers import VerticalScroll
from textual.timer import Timer

from vasuki.application.view_models import ConversationItem
from vasuki.tui.widgets.message import MessageCard
from vasuki.tui.widgets.welcome import WelcomeBanner


class ConversationView(VerticalScroll):
    can_focus = True
    _BOTTOM_THRESHOLD = 3
    _PENDING_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stream_card: MessageCard | None = None
        self._pending_card: MessageCard | None = None
        self._pending_label = "working"
        self._pending_frame = 0
        self._pending_timer: Timer | None = None
        self.provider = "offline"
        self.runtime = "local"
        #: Ids of persisted messages already on screen, in order, so a refresh
        #: can mount only what is new.
        self._shown: list[str] = []

    def on_mount(self) -> None:
        self._pending_timer = self.set_interval(
            0.12,
            self._animate_pending,
            name="chat-working-indicator",
            pause=True,
        )

    def set_environment(self, provider: str, runtime: str) -> None:
        """Record what the welcome banner should report about this session."""
        self.provider = provider
        self.runtime = runtime

    def _is_near_bottom(self) -> bool:
        return self.max_scroll_y <= 0 or (
            self.max_scroll_y - self.scroll_y <= self._BOTTOM_THRESHOLD
        )

    def _scroll_end_after_layout(self) -> None:
        self.call_after_refresh(
            self.scroll_end,
            animate=False,
            force=True,
            immediate=True,
        )

    @staticmethod
    def _card(item: ConversationItem) -> MessageCard:
        return MessageCard(
            item.content,
            kind=item.kind,
            role=item.role,
            metadata=item.metadata,
        )

    async def load_messages(self, items: list[ConversationItem]) -> None:
        """Replace the whole transcript. Use ``sync_messages`` after a turn."""
        self._stream_card = None
        self._stop_pending_animation()
        self._pending_card = None
        self._shown = []
        await self.remove_children()
        if not items:
            await self.mount(WelcomeBanner(self.provider, self.runtime))
            return
        await self.mount_all([self._card(item) for item in items])
        self._shown = [item.id for item in items]
        self._scroll_end_after_layout()

    async def sync_messages(self, items: list[ConversationItem]) -> None:
        """Bring the view up to date by mounting only what it has not shown.

        Reloading the transcript after every turn costs a mount and a layout per
        message, so the cost grew with the length of the conversation until
        sending anything stalled for a second or more. Appending the tail keeps
        it proportional to what actually changed.
        """
        ids = [item.id for item in items]
        if ids[: len(self._shown)] != self._shown:
            # History was rewritten (a different session, or /clear): start over.
            await self.load_messages(items)
            return
        fresh = items[len(self._shown) :]
        self._stream_card = None
        await self.clear_pending()
        for welcome in self.query(".welcome-card"):
            await welcome.remove()
        # Cards mounted from live events during the turn are the same edits the
        # service persisted. They exist so the user sees work as it lands; the
        # persisted copies now replace them, and keeping both would show every
        # diff twice.
        mounted = list(self.query(MessageCard))
        for card in mounted[len(self._shown) :]:
            await card.remove()
        if not fresh:
            self._shown = ids
            return
        follow = self._is_near_bottom()
        await self.mount_all([self._card(item) for item in fresh])
        self._shown = ids
        if follow:
            self._scroll_end_after_layout()

    async def add_message(
        self,
        content: str,
        *,
        kind: str = "agent",
        role: str = "",
        metadata: dict[str, object] | None = None,
        follow: bool | None = None,
    ) -> MessageCard:
        should_follow = self._is_near_bottom() if follow is None else follow
        for welcome in self.query(".welcome-card"):
            await welcome.remove()
        card = MessageCard(content, kind=kind, role=role, metadata=metadata)
        await self.mount(card)
        if should_follow:
            self._scroll_end_after_layout()
        return card

    async def begin_pending(self, label: str = "Working…") -> None:
        """Show an immediate placeholder so a slow first token never looks like a hang."""
        await self.clear_pending()
        self._pending_label = self._clean_pending_label(label)
        self._pending_frame = 0
        self._pending_card = await self.add_message("", kind="status", follow=True)
        self._paint_pending()
        if self._pending_timer is not None:
            self._pending_timer.resume()

    def update_pending(self, label: str) -> None:
        """Retitle the placeholder with whatever the agent is doing right now.

        A turn is several model calls and several tool actions. Without this the
        chat area shows one unchanging line for the whole turn, which reads as a
        hang rather than as work in progress.
        """
        if self._pending_card is not None and self._pending_card.is_mounted:
            self._pending_label = self._clean_pending_label(label)
            self._pending_frame = 0
            self._paint_pending()

    @staticmethod
    def _clean_pending_label(label: str) -> str:
        return label.strip().rstrip(".…").strip() or "working"

    def _paint_pending(self) -> None:
        if self._pending_card is None or not self._pending_card.is_mounted:
            return
        frame = self._PENDING_FRAMES[self._pending_frame % len(self._PENDING_FRAMES)]
        dots = "." * (self._pending_frame % 3 + 1)
        self._pending_card.replace_content(f"{frame} {self._pending_label}{dots}")

    def _animate_pending(self) -> None:
        if self._pending_card is None or not self._pending_card.is_mounted:
            self._stop_pending_animation()
            return
        self._pending_frame += 1
        self._paint_pending()

    def _stop_pending_animation(self) -> None:
        if self._pending_timer is not None:
            self._pending_timer.pause()
        self._pending_frame = 0

    async def clear_pending(self) -> None:
        self._stop_pending_animation()
        if self._pending_card is not None:
            card, self._pending_card = self._pending_card, None
            if card.is_mounted:
                await card.remove()

    async def append_stream(self, chunk: str, *, role: str = "assistant") -> None:
        should_follow = self._is_near_bottom()
        if self._stream_card is None:
            await self.clear_pending()
            self._stream_card = await self.add_message(
                "",
                kind="agent",
                role=role,
                follow=should_follow,
            )
        self._stream_card.append_chunk(chunk)
        if should_follow:
            self._scroll_end_after_layout()

    def finish_stream(self, duration: float | None = None) -> None:
        """Close the streaming card, stamping how long the answer took."""
        if self._stream_card is not None and duration is not None:
            self._stream_card.set_duration(duration)
        self._stream_card = None

    async def clear_visible(self) -> None:
        self._stream_card = None
        self._stop_pending_animation()
        self._pending_card = None
        self._shown = []
        await self.remove_children()
