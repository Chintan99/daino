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

from pygments.lexers import get_lexer_for_filename  # type: ignore[import-untyped]
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
from pygments.util import ClassNotFound  # type: ignore[import-untyped]
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

#: Keep short changed lines as visible rectangular fills without allowing one
#: generated/minified line to make every other line hundreds of cells wide.
UNIFIED_DIFF_MAX_WIDTH = 160


@lru_cache(maxsize=_CACHE_SIZE)
def highlight_code(code: str, language: str, background: str = "") -> Content:
    """Highlight source while optionally filling its complete background.

    The background is applied as a colour-only overlay, so token foregrounds
    remain syntax colours. This is what lets a diff communicate addition or
    removal with its fill without turning every character green or red.
    """
    try:
        syntax = Syntax(
            code,
            language or "text",
            theme=_SYNTAX_THEME,
            background_color=None,
            word_wrap=True,
        )
        highlighted = syntax.highlight(code)
        # ``Syntax.highlight`` appends a newline even for one source line. Diff
        # renderers own their separators, so retaining it would double-space
        # every line and break the continuous red/green blocks.
        highlighted.remove_suffix("\n")
        if background:
            highlighted.stylize(Style(bgcolor=background))
        return Content.from_rich_text(highlighted)
    except Exception:
        # An unknown lexer or malformed source must never cost us the message.
        style = palette.TEXT + (f" on {background}" if background else "")
        return Content.styled(code, style)


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
            pieces.append(highlight_code(code.rstrip("\n"), language))
        cursor = match.end()
    remainder = text[cursor:]
    if remainder or not pieces:
        pieces.append(Content.styled(remainder, prose_style))
    return pieces


def highlight_unified_diff(diff: str) -> Content:
    """Render a Git diff with file-language syntax and change backgrounds."""
    if not diff:
        return Content.styled("No changes", palette.MUTED)

    lines = diff.splitlines()
    fill_width = min(max((len(line) for line in lines), default=0), UNIFIED_DIFF_MAX_WIDTH)
    language = ""
    parts: list[str | Content | tuple[str, str]] = []
    for index, line in enumerate(lines):
        if index:
            parts.append("\n")

        if line.startswith(("+++ ", "--- ")):
            path = line[4:].split("\t", 1)[0]
            if path != "/dev/null":
                language = guess_language(path.removeprefix("a/").removeprefix("b/"))
            parts.append((line, palette.MUTED))
            continue
        if line.startswith("diff --git "):
            parts.append((line, f"bold {palette.ACCENT}"))
            continue
        if line.startswith("@@"):
            parts.append((line, f"bold {palette.PLAN}"))
            continue
        if line.startswith(("index ", "new file ", "deleted file ")):
            parts.append((line, palette.MUTED))
            continue

        marker = line[:1]
        if marker in {"+", "-", " "}:
            background = (
                palette.DIFF_ADDED_BG
                if marker == "+"
                else palette.DIFF_REMOVED_BG
                if marker == "-"
                else ""
            )
            marker_style = palette.DIFF_GUTTER + (f" on {background}" if background else "")
            code = line[1:]
            if background:
                code = code.ljust(max(fill_width - 1, 0))
            parts.append((marker, marker_style))
            parts.append(highlight_code(code, language, background=background))
            continue

        parts.append((line, palette.MUTED))
    return Content.assemble(*parts)


@lru_cache(maxsize=256)
def guess_language(path: str) -> str:
    """Map a file extension to a lexer name for diff and preview highlighting."""
    name = path.rsplit("/", 1)[-1].casefold()
    special = {
        "dockerfile": "docker",
        "makefile": "make",
        "justfile": "make",
    }
    if name in special:
        return special[name]
    if name.startswith("dockerfile."):
        return "docker"
    suffix = path.rsplit(".", 1)[-1].casefold() if "." in path else ""
    configured = _EXTENSIONS.get(suffix)
    if configured:
        return configured
    try:
        lexer = get_lexer_for_filename(name)
    except ClassNotFound:
        return ""
    return lexer.aliases[0] if lexer.aliases else ""


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
