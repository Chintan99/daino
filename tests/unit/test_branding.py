"""The stylised product name has to survive two different markup parsers."""

from __future__ import annotations

from rich.text import Text
from textual.content import Content

from daino import branding
from daino.tui.keybindings import SLASH_COMMANDS


def test_the_name_is_the_stylised_form() -> None:
    assert branding.NAME == "D[Ai]NO"


def test_rich_leaves_the_plain_name_alone() -> None:
    """Rich only treats a lowercase-initial tag as markup, so ``[Ai]`` is safe."""
    assert Text.from_markup(branding.NAME).plain == branding.NAME


def test_textual_needs_the_escaped_form() -> None:
    """Textual's parser is less fussy and would otherwise render ``DNO``."""
    assert Content.from_markup(branding.NAME).plain == "DNO"
    assert Content.from_markup(branding.NAME_MARKUP).plain == branding.NAME


def test_escape_markup_protects_data_interpolated_into_markup() -> None:
    """Neither library's own ``escape`` helper covers this; ours must."""
    assert Content.from_markup(branding.escape_markup(branding.NAME)).plain == branding.NAME
    # A usage string documented as "[title]" must keep its brackets too.
    assert Content.from_markup(branding.escape_markup("[title]")).plain == "[title]"


def test_help_text_renders_the_name_and_usage_intact() -> None:
    """The help view interpolates command metadata into a markup string."""
    rendered = Content.from_markup(
        "\n".join(
            f"[b]{item.name}[/b] {branding.escape_markup(item.description)} "
            f"[dim]{branding.escape_markup(item.usage)}[/dim]"
            for item in SLASH_COMMANDS
        )
    ).plain
    assert f"Exit {branding.NAME} safely" in rendered
    assert "[title]" in rendered
