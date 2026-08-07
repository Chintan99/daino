"""Opening screen: the Vasuki serpent beside the wordmark."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.widgets import Static

from vasuki import __version__
from vasuki.tui import palette

#: Vasuki, coiled. Drawn on the density ramp " .:-=+*#%@" so the shading reads as
#: depth rather than as a flat outline, and kept small on purpose: the banner
#: sits above a working prompt, and art that fills the screen on every launch
#: stops being a welcome and becomes something to scroll past.
_ART_ROWS: tuple[str, ...] = (
    "...",
    "                  .--+=:-::-:.",
    "                 -*****++==--:",
    "                 -****+++++=-",
    "                 =****++=-::",
    "                  ***+==-::",
    "                  -**+=--:::.",
    "                 -+*===-:::::=+-.",
    "               =++ +*+=--:..:  +=::",
    "     :::.    =++=   **+=-::..: +--=",
    "  .+*#        *+    =**==-::.:",
    "  +==               +**+=-::::",
    "  -+=.           ..-***+==-:::",
    "  =++=+======--=---****++==-:",
    "    **+++++++****+==##**++==-:",
    "        =#######*=+=#***+  :#+",
    "                 *+-       +*==.",
)

ART_WIDTH = max(len(row) for row in _ART_ROWS)


def serpent_markup() -> str:
    """Return the serpent as Rich markup in the body colour."""
    return "\n".join(f"[{palette.SNAKE}]{row}[/]" for row in _ART_ROWS)


class WelcomeBanner(Vertical):
    """Serpent beside the wordmark, with a dim hint on its own full-width row.

    The hint sits below rather than beside the wordmark: the art claims most of
    the line, and squeezing prose into what is left truncates it.
    """

    def __init__(self, provider: str, runtime: str) -> None:
        super().__init__(classes="welcome-card")
        self.provider = provider
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        with Horizontal(id="welcome-banner"):
            yield Static(serpent_markup(), id="welcome-art")
            with Vertical(id="welcome-title"):
                yield Static(
                    Content.styled("V A S U K I", f"bold {palette.BRIGHT}"),
                    id="welcome-wordmark",
                )
                yield Static(
                    Content.styled(
                        f"v{__version__} · {self.provider} · {self.runtime}",
                        palette.FAINT,
                    ),
                    id="welcome-meta",
                )
        yield Static(
            Content.styled(
                "— ask a repository question, or /plan to start an approval-gated mission",
                palette.FAINT,
            ),
            id="welcome-help",
        )
