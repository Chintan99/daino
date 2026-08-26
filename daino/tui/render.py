"""Helpers for composing styled terminal text safely.

Textual's ``Static.update`` parses console markup, and its parser is strict: an
unbalanced ``[`` raises ``MarkupError``. Model output, tool summaries, file
paths, and exception strings all contain brackets, and a streamed answer can
end mid-token on an unclosed one — ``button[type="submit`` arrives before its
``]`` does. No escaper can repair that, because the bracket only becomes safe
once its partner shows up.

So untrusted text never goes through markup here. It is attached to a style as
a span instead, via ``Content``, which performs no parsing at all.
"""

from __future__ import annotations

from textual.content import Content

Part = str | Content | tuple[str, str]


def _is_empty(part: Part) -> bool:
    if isinstance(part, tuple):
        return not part[0]
    if isinstance(part, Content):
        return not part.plain
    return not part


def join(separator: str, *parts: Part) -> Content:
    """Join styled parts with a plain separator, skipping empty ones."""
    joined: list[Part] = []
    for part in parts:
        if _is_empty(part):
            continue
        if joined:
            joined.append(separator)
        joined.append(part)
    return Content.assemble(*joined)
