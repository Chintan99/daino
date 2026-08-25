"""Scrollable persisted conversation."""

from __future__ import annotations

from time import monotonic
from typing import Any
from unicodedata import category

from textual.containers import VerticalScroll
from textual.content import Content
from textual.timer import Timer
from textual.widgets import Static

from vasuki.application.view_models import ConversationItem
from vasuki.security import redact
from vasuki.tui import palette
from vasuki.tui.widgets.message import MessageCard
from vasuki.tui.widgets.welcome import WelcomeBanner


class ConversationView(VerticalScroll):
    can_focus = True
    _BOTTOM_THRESHOLD = 3
    _PENDING_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    # Reasoning is a live tail, not transcript history. Keeping both a character
    # and line ceiling prevents a chatty local model from growing the widget (or
    # repaint cost) without bound while retaining enough recent context to show
    # what phase it is in.
    REASONING_MAX_CHARS = 2_000
    REASONING_MAX_LINES = 12
    _REASONING_REPAINT_INTERVAL = 0.08

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._stream_card: MessageCard | None = None
        self._pending_card: MessageCard | None = None
        self._pending_label = "working"
        self._pending_frame = 0
        self._pending_started = 0.0
        self._pending_timer: Timer | None = None
        self._reasoning_panel: Static | None = None
        self._reasoning_raw_buffer = ""
        self._reasoning_buffer = ""
        self._reasoning_dirty = False
        self._reasoning_timer: Timer | None = None
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
        self._reasoning_timer = self.set_interval(
            self._REASONING_REPAINT_INTERVAL,
            self._flush_reasoning,
            name="chat-live-reasoning",
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
        self._reset_reasoning_state()
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
        self._pending_started = monotonic()
        self._pending_card = await self.add_message("", kind="status", follow=True)
        self._paint_pending()
        if self._pending_timer is not None:
            self._pending_timer.resume()

    async def begin_reasoning(self) -> None:
        """Start a fresh, ephemeral provider-reasoning tail for one model call.

        ``ModelSelected`` is the call boundary. The panel itself is mounted only
        after a non-empty chunk arrives, so providers without exported reasoning
        keep the existing compact pending indicator.
        """
        await self.clear_reasoning()

    async def append_reasoning(self, chunk: str) -> None:
        """Append sanitized reasoning without turning it into a chat message.

        The first chunk paints immediately; subsequent high-frequency chunks are
        coalesced by a short timer. ``Static(markup=False)`` and styled ``Content``
        keep model-supplied brackets from being interpreted as Rich markup.
        """
        safe = _safe_reasoning_chunk(chunk)
        if not safe:
            return
        # Keep only a bounded in-memory raw tail and redact the whole tail after
        # each append. Per-chunk redaction misses credentials split across SSE
        # frames (``api_`` then ``key=...``), and redacting a partial token loses
        # the context needed when its remaining characters arrive.
        self._reasoning_raw_buffer = self._bounded_reasoning(
            self._reasoning_raw_buffer + safe
        )
        self._reasoning_buffer = self._bounded_reasoning(redact(self._reasoning_raw_buffer))
        self._reasoning_dirty = True
        if self._reasoning_panel is None or not self._reasoning_panel.is_mounted:
            self._reasoning_panel = Static(
                "",
                id="live-reasoning",
                classes="live-reasoning",
                markup=False,
            )
            await self.mount(self._reasoning_panel)
            self._paint_reasoning()
            self._scroll_end_after_layout()
            return
        if self._reasoning_timer is not None:
            self._reasoning_timer.resume()

    @property
    def reasoning_text(self) -> str:
        """Return the bounded sanitized live tail, primarily for UI assertions."""
        return self._reasoning_buffer

    def _bounded_reasoning(self, value: str) -> str:
        lines = value.splitlines(keepends=True)
        if len(lines) > self.REASONING_MAX_LINES:
            value = "…\n" + "".join(lines[-(self.REASONING_MAX_LINES - 1) :])
        if len(value) > self.REASONING_MAX_CHARS:
            value = "…" + value[-(self.REASONING_MAX_CHARS - 1) :]
        return value

    def _paint_reasoning(self) -> None:
        panel = self._reasoning_panel
        if panel is None or not panel.is_mounted:
            return
        follow = self._is_near_bottom()
        panel.update(
            Content.assemble(
                ("thinking · live recent\n", f"bold {palette.PLAN}"),
                (self._reasoning_buffer, palette.MUTED),
            )
        )
        self._reasoning_dirty = False
        if follow:
            self._scroll_end_after_layout()

    def _flush_reasoning(self) -> None:
        if self._reasoning_dirty:
            self._paint_reasoning()
        if self._reasoning_timer is not None:
            self._reasoning_timer.pause()

    def _reset_reasoning_state(self) -> None:
        if self._reasoning_timer is not None:
            self._reasoning_timer.pause()
        self._reasoning_panel = None
        self._reasoning_raw_buffer = ""
        self._reasoning_buffer = ""
        self._reasoning_dirty = False

    async def clear_reasoning(self) -> None:
        """Remove all reasoning at a model/action/turn boundary."""
        if self._reasoning_timer is not None:
            self._reasoning_timer.pause()
        panel, self._reasoning_panel = self._reasoning_panel, None
        self._reasoning_raw_buffer = ""
        self._reasoning_buffer = ""
        self._reasoning_dirty = False
        if panel is not None and panel.is_mounted:
            await panel.remove()

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
        elapsed = max(0, int(monotonic() - self._pending_started))
        self._pending_card.replace_content(
            f"{frame} {self._pending_label} · {elapsed}s{dots}"
        )

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
        await self.clear_reasoning()
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
        self._reset_reasoning_state()
        self._shown = []
        await self.remove_children()


def _safe_reasoning_chunk(value: str) -> str:
    """Remove terminal/control effects before rolling-buffer redaction."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    # Joining around controls before redaction prevents ``api_\0key`` from
    # bypassing the credential patterns. Newlines remain useful structure.
    visible = "".join(
        character
        for character in normalized
        if character == "\n" or category(character) not in {"Cc", "Cf", "Cs"}
    )
    return visible
