"""Conversation messages rendered as a label line above their content.

The design carries hierarchy through colour and dimming rather than through
borders or filled cards, so a message is nothing but two stacked lines of text.
"""

from __future__ import annotations

import json

from textual.content import Content
from textual.widgets import Static

from vasuki.tui import palette
from vasuki.tui.highlight import highlight_body

LABELS = {
    "user": "you",
    "agent": "vasuki",
    "plan": "plan",
    "tool": "tool",
    "test": "test",
    "approval": "approval",
    "error": "error",
    "checkpoint": "checkpoint",
    "deployment": "deploy",
    "summary": "summary",
    "diff": "edit",
    "status": "",
}

#: Diff bodies are painted per line rather than as one block, so an added line
#: reads as added at a glance instead of being lost in a wall of text. Each entry
#: is (gutter style, body style); changed lines carry a filled background.
DIFF_STYLES = {
    "+": (
        f"{palette.DIFF_ADDED_GUTTER} on {palette.DIFF_ADDED_BG}",
        f"{palette.DIFF_ADDED} on {palette.DIFF_ADDED_BG}",
    ),
    "-": (
        f"{palette.DIFF_REMOVED_GUTTER} on {palette.DIFF_REMOVED_BG}",
        f"{palette.DIFF_REMOVED} on {palette.DIFF_REMOVED_BG}",
    ),
    " ": (palette.DIFF_GUTTER, palette.DIFF_CONTEXT),
}

#: Widest a filled diff line is padded to. Padding makes each changed line a
#: rectangle rather than a ragged stripe; the cap stops one very long line from
#: dragging the whole block wide.
DIFF_MAX_WIDTH = 120

#: One hue per kind, so the transcript can be scanned by colour alone.
LABEL_STYLES = {
    "user": f"bold {palette.USER}",
    "agent": f"bold {palette.ACCENT}",
    "plan": f"bold {palette.PLAN}",
    "summary": f"bold {palette.ACCENT}",
    "tool": palette.TOOL,
    "diff": f"bold {palette.CAUTION}",
    "checkpoint": palette.CHECKPOINT,
    "test": f"bold {palette.READY}",
    "approval": f"bold {palette.CAUTION}",
    "deployment": f"bold {palette.DEPLOY}",
    "error": f"bold {palette.ALERT}",
    "status": palette.FAINT,
}

BODY_STYLES = {
    "user": palette.BRIGHT,
    "status": palette.FAINT,
    "error": palette.ALERT,
}


class MessageCard(Static):
    can_focus = True
    BINDINGS = [
        ("enter", "toggle_details", "Details"),
        ("space", "toggle_details", "Details"),
    ]

    def __init__(
        self,
        content: str,
        *,
        kind: str = "agent",
        role: str = "",
        metadata: dict[str, object] | None = None,
        message_id: str | None = None,
        duration: float | None = None,
    ) -> None:
        self.kind = kind
        self.role = role
        self.metadata = metadata or {}
        self.raw_content = content
        self.duration = duration
        self.expanded = False
        super().__init__("", id=message_id, classes=f"message-card message-{kind}")
        self._paint()

    def _paint(self) -> None:
        """Rebuild the message as styled spans.

        Assembled as ``Content`` rather than a markup string on purpose. Model
        output is arbitrary text — a streamed chunk can end mid-token on an
        unclosed ``[``, as ``button[type="submit`` does, and no escaper can fix
        that because the bracket only becomes safe once its partner arrives.
        Building spans directly means the body is never parsed as markup.

        Do not name this ``_render_content``: that is Textual's own ``Widget``
        method for populating the render cache, and shadowing it makes every
        message occupy space while painting nothing at all.
        """
        parts: list[str | Content | tuple[str, str]] = []
        label = LABELS.get(self.kind, self.kind)
        if label:
            parts.append((label, LABEL_STYLES.get(self.kind, palette.MUTED)))
            meta = " · ".join(
                part
                for part in (
                    self.role.casefold(),
                    f"{self.duration:.1f}s" if self.duration is not None else "",
                )
                if part
            )
            if meta and self.kind != "user":
                parts.append((f" {meta}", palette.FAINT))
            parts.append("\n")
        if self.kind == "diff":
            parts.extend(self._diff_spans())
        else:
            # Prose stays one span; fenced code is highlighted per token, which
            # is what stops an answer that is mostly code reading as a grey wall.
            parts.extend(highlight_body(self.raw_content, BODY_STYLES.get(self.kind, palette.TEXT)))
        if self.metadata:
            parts.append(("\nenter  details", palette.FAINTEST))
        if self.expanded and self.metadata:
            details = json.dumps(self.metadata, indent=2, default=str)
            parts.append((f"\n\n{details}", palette.FAINTEST))
        self.update(Content.assemble(*parts))

    def _diff_spans(self) -> list[str | Content | tuple[str, str]]:
        """Colour each diff line by its marker, leaving the header plain.

        The marker sits after the line number, as ``12 + text``, so the prefix is
        found by position rather than by the first character: a context line of
        real code may itself begin with ``-`` or ``+``.

        The gutter is styled separately from the body so the line number stays
        quiet while the code itself carries the colour, and changed lines are
        padded to a common width so the fill reads as a block.
        """
        lines = self.raw_content.splitlines()
        width = min(max((len(line) for line in lines), default=0), DIFF_MAX_WIDTH)
        spans: list[str | Content | tuple[str, str]] = []
        for index, line in enumerate(lines):
            if index:
                spans.append("\n")
            marker = _diff_marker(line)
            if marker is None:
                spans.append((line, palette.TEXT if index == 0 else palette.MUTED))
                continue
            gutter_style, body_style = DIFF_STYLES[marker]
            # Split "  12 + code" into the "  12 " gutter and "+ code" body.
            cut = len(line) - len(line.lstrip())
            number_end = line.index(" ", cut) + 1
            body = line[number_end:].ljust(max(width - number_end, 0))
            spans.append((line[:number_end], gutter_style))
            spans.append((body, body_style))
        return spans

    def append_chunk(self, chunk: str) -> None:
        self.raw_content += chunk
        self._paint()

    def replace_content(self, content: str) -> None:
        self.raw_content = content
        self._paint()

    def set_duration(self, seconds: float) -> None:
        self.duration = seconds
        self._paint()

    def action_toggle_details(self) -> None:
        if self.metadata:
            self.expanded = not self.expanded
            self._paint()


def _diff_marker(line: str) -> str | None:
    """Return the +/-/space marker of a rendered diff line, or None for a header.

    Rendered lines look like ``  12 + text``: a right-aligned line number, the
    marker, then the text. Anything that does not start with a number is part of
    the diff's own header or note.
    """
    stripped = line.lstrip()
    number, separator, rest = stripped.partition(" ")
    if not separator or not number.isdigit() or not rest:
        return None
    marker = rest[0]
    return marker if marker in DIFF_STYLES and rest[1:2] == " " else None
