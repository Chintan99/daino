"""Colour tokens for the Daino TUI.

The stylesheet, the Textual theme, the syntax highlighter, and inline spans all
read from here so they cannot drift apart.

The surface stays a near-black field with hairline rules rather than panels, but
hierarchy is no longer carried by dimming alone. A terminal that greys
everything reads as flat and unreadable at a glance, so each kind of message,
each diff side, and each syntax token gets a hue of its own. The accents come
from one harmonised family — cool blues and violets against warm greens and
ambers — so the surface holds together instead of looking like confetti.
"""

from __future__ import annotations

BACKGROUND = "#0a0d0b"
#: Barely lifted from the field. Used for focus, selection, and code blocks.
SURFACE = "#101511"
SURFACE_BRIGHT = "#171d18"
RULE = "#252d27"

#: Brand. Also the agent's own voice and the prompt caret.
ACCENT = "#72d895"
#: Higher-luminance prompt chrome, kept separate so the rest of the interface
#: retains the calmer brand accent.
PROMPT_ACCENT = "#86e7a6"
ACCENT_CHOICES: dict[str, str] = {
    "jade": ACCENT,
    "azure": "#7aa2f7",
    "violet": "#bb9af7",
    "amber": "#e0af68",
}

# Neutral ramp, brightest first. Cooler and higher-contrast than a warm grey
# scale, so body text stays legible against saturated accents.
INPUT = "#e2e8e1"
BRIGHT = "#ced5cd"
TEXT = "#b7c0b7"
MUTED = "#7c877e"
VALUE = "#909b92"
HINT = "#626c64"
DIM = "#59625b"
FAINT = "#49514b"
FAINTEST = "#3a413c"

#: Semantic accents, one per kind of thing the agent reports.
USER = "#7aa2f7"
PLAN = "#bb9af7"
TOOL = "#56cfe1"
READY = "#64d98b"
CAUTION = "#e2b86b"
ALERT = "#e2767f"
DEPLOY = "#ff9e64"
CHECKPOINT = "#c0a7e8"

#: Diff colours. The neutral ramp carries hierarchy by dimming, which is right
#: for prose and wrong for a diff: added and removed have to be told apart at a
#: glance, not merely distinguished. Diffs are the one place the surface is
#: allowed a filled background, so a changed line reads as a block.
DIFF_ADDED = "#b9f27c"
DIFF_ADDED_BG = "#16251a"
DIFF_REMOVED = "#ff9aa8"
DIFF_REMOVED_BG = "#2b161c"
#: Line numbers and the +/- column, dim enough to stay out of the way.
DIFF_GUTTER = "#5a6170"
DIFF_ADDED_GUTTER = "#6fbf73"
DIFF_REMOVED_GUTTER = "#d16b7c"
#: Unchanged context lines: present, but clearly not part of the change.
DIFF_CONTEXT = "#8d94a3"


def resolve_accent(name: str) -> str:
    """Return an accent hex value by name, falling back to the default."""
    return ACCENT_CHOICES.get(name.strip().casefold(), ACCENT)
