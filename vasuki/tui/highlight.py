"""Syntax highlighting for code inside conversation messages.

Model answers are mostly code, and a wall of one-colour code is the single
biggest reason a transcript reads as dull and hard to scan. Fenced blocks are
highlighted per token here.

Two constraints shape the implementation. The highlighter is driven from
``palette`` rather than a bundled Pygments theme, so code cannot clash with the
surface around it. And the result is converted to ``Content`` spans rather than
console markup, because model output is arbitrary text: an unclosed ``[`` would
otherwise be parsed as a tag and swallow whatever followed.
"""

from __future__ import annotations

import re
from functools import lru_cache

from pygments.token import (  # type: ignore[import-untyped]
    Comment,
    Error,
    Generic,
    Keyword,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Token,
)
from rich.style import Style
from rich.syntax import ANSISyntaxTheme, Syntax
from textual.content import Content

from vasuki.tui import palette

#: ```lang fenced blocks, including an unterminated final fence so a streaming
#: answer highlights the block it is still in the middle of writing.
_FENCE = re.compile(r"^[ \t]*```([\w+.#-]*)[ \t]*\n(.*?)(?:^[ \t]*```[ \t]*$|\Z)", re.S | re.M)

_SYNTAX_THEME = ANSISyntaxTheme(
    {
        Token: Style(color=palette.TEXT),
        Comment: Style(color=palette.DIM, italic=True),
        Keyword: Style(color=palette.PLAN, bold=True),
        Keyword.Constant: Style(color=palette.DEPLOY),
        Keyword.Type: Style(color=palette.TOOL),
        Name: Style(color=palette.BRIGHT),
        Name.Function: Style(color=palette.USER),
        Name.Class: Style(color=palette.CAUTION),
        Name.Decorator: Style(color=palette.CAUTION),
        Name.Builtin: Style(color=palette.TOOL),
        Name.Attribute: Style(color=palette.BRIGHT),
        Name.Tag: Style(color=palette.ALERT),
        Name.Variable: Style(color=palette.BRIGHT),
        String: Style(color=palette.READY),
        String.Escape: Style(color=palette.DEPLOY),
        Number: Style(color=palette.DEPLOY),
        Operator: Style(color=palette.TOOL),
        Punctuation: Style(color=palette.MUTED),
        Generic.Inserted: Style(color=palette.DIFF_ADDED),
        Generic.Deleted: Style(color=palette.DIFF_REMOVED),
        Generic.Heading: Style(color=palette.ACCENT, bold=True),
        Error: Style(color=palette.ALERT),
    }
)

#: Highlighting is pure for a given (code, language), and a streamed answer
#: repaints its card on every chunk, so the same block is highlighted many times.
_CACHE_SIZE = 256


@lru_cache(maxsize=_CACHE_SIZE)
def _highlight(code: str, language: str) -> Content:
    syntax = Syntax(
        code,
        language or "text",
        theme=_SYNTAX_THEME,
        background_color="default",
        word_wrap=True,
    )
    try:
        return Content.from_rich_text(syntax.highlight(code))
    except Exception:
        # An unknown lexer or malformed source must never cost us the message.
        return Content.styled(code, palette.TEXT)


def highlight_body(text: str, prose_style: str) -> list[Content]:
    """Split a message into prose and fenced code, highlighting the code.

    Returns the pieces in order, ready for ``Content.assemble``.
    """
    pieces: list[Content] = []
    cursor = 0
    for match in _FENCE.finditer(text):
        prose = text[cursor : match.start()]
        if prose:
            pieces.append(Content.styled(prose, prose_style))
        language = (match.group(1) or "").strip().casefold()
        code = match.group(2)
        if code:
            pieces.append(_highlight(code.rstrip("\n"), language))
        cursor = match.end()
    remainder = text[cursor:]
    if remainder or not pieces:
        pieces.append(Content.styled(remainder, prose_style))
    return pieces


def guess_language(path: str) -> str:
    """Map a file extension to a lexer name for diff and preview highlighting."""
    suffix = path.rsplit(".", 1)[-1].casefold() if "." in path else ""
    return _EXTENSIONS.get(suffix, "")


_EXTENSIONS = {
    "py": "python",
    "pyi": "python",
    "js": "javascript",
    "jsx": "jsx",
    "ts": "typescript",
    "tsx": "tsx",
    "html": "html",
    "htm": "html",
    "css": "css",
    "scss": "scss",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "md": "markdown",
    "sh": "bash",
    "bash": "bash",
    "sql": "sql",
    "go": "go",
    "rs": "rust",
    "java": "java",
    "rb": "ruby",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "dockerfile": "docker",
}
