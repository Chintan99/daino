"""The product's user-facing name, in one place so it cannot drift.

The name is stylised **D[Ai]NO**. Those square brackets are also console markup
syntax, and the two renderers this project uses disagree about them:

* Rich only treats ``[tag]`` as markup when the tag starts with a lowercase
  letter, ``#``, ``/`` or ``@``, so the capital ``A`` in ``[Ai]`` means
  :data:`NAME` survives ``console.print`` untouched.
* Textual's parser is not so fussy and swallows ``[Ai]`` as an unknown style,
  rendering the name as ``DNO``.

So anything that goes through a *markup-parsed* string — a Textual ``Static``,
``Label`` or ``Button`` built from ``str`` — must use :data:`NAME_MARKUP`.
Plain text, ``Content`` objects, HTML, JSON, and the browser IDE all take
:data:`NAME`.
"""

from __future__ import annotations

#: The name as a person reads it.
NAME = "D[Ai]NO"

#: The same name, escaped for console markup.
NAME_MARKUP = NAME.replace("[", "\\[")


#: The dinosaur shown while the browser IDE builds. Raw strings so the
#: backslashes in the art are taken literally.
DINO_ART = (
    r"               __",
    r"              / _)",
    r"     _/\/\/\_/ /",
    r"   _|         /",
    r" _|  (  | (  |",
    r"/__.-'|_|--|_|  ",
)


def dino_banner(*, color: bool = True) -> str:
    """The dinosaur plus the wordmark, for the one-time GUI build."""
    green = "\033[32m" if color else ""
    bold = "\033[1m" if color else ""
    reset = "\033[0m" if color else ""
    art = "\n".join(f"{green}{line}{reset}" for line in DINO_ART)
    return f"\n{art}\n\n    {bold}{NAME}{reset} — building the browser IDE\n"


def escape_markup(text: str) -> str:
    """Escape ``text`` so console markup renders every bracket literally.

    Rich and Textual both ship an ``escape`` helper, and both only escape tags
    they would themselves recognise — which is why neither protects ``[Ai]``.
    Textual's *parser*, meanwhile, is happy to swallow it. Escaping every
    bracket is the only form that survives both, and it is what data being
    interpolated into a markup string wants anyway: a slash command documented
    as taking ``[title]`` should show those brackets, not lose them to a parser.
    """
    return text.replace("[", "\\[")


__all__ = ["NAME", "NAME_MARKUP", "DINO_ART", "dino_banner", "escape_markup"]
