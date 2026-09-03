"""Render every ``docs/*.md`` into a styled page of the documentation site.

The site is deployed as static files — ``upload-pages-artifact`` on ``docs/``,
with no Jekyll — so a ``.md`` file in there is served as a download, not as a
page. That is why every "read more" link on the landing page used to point at
``github.com/.../blob/v2/docs/*.md``: there was nothing else to point at, and a
reader following one left the documentation site for a source-code view of the
file they wanted to read.

This closes that. Each markdown file becomes ``docs/<name>.html`` wearing the
same header, navigation, footer, and stylesheet as the landing page, and every
internal ``.md`` link is rewritten to the generated page beside it.

Run it before deploying::

    python scripts/build_docs_site.py

Generated pages are not committed — the workflow builds them into the artifact
it uploads — so nothing here has to be kept in sync by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

#: Files in ``docs/`` that are the site itself rather than a page of it.
NOT_PAGES = {"index.html", "styles.css", "script.js", "install.sh"}

#: The landing-page sections the top navigation points at. Written as
#: ``index.html#…`` because a generated page is not the landing page, and a bare
#: fragment would scroll the reader to nowhere on the page they are already on.
NAV = (
    ("index.html#installation", "Installation"),
    ("index.html#getting-started", "Get started"),
    ("index.html#interfaces", "Interfaces"),
    ("index.html#workspaces", "GUI tabs"),
    ("index.html#providers", "Providers"),
    ("index.html#features", "Features"),
    ("index.html#reference", "Reference"),
)

#: Title shown in the browser tab and the page header, when the markdown does
#: not open with an ``h1`` of its own.
FALLBACK_TITLES = {"index": "Documentation"}

#: The page shell, kept beside this file rather than inside it. It is HTML —
#: SVG path data does not wrap to a Python line limit, and editing the site
#: chrome should not mean editing a string literal.
TEMPLATE = (Path(__file__).resolve().parent / "docs_page_template.html").read_text(encoding="utf-8")


def slugify(text: str, seen: dict[str, int]) -> str:
    """A GitHub-compatible heading anchor.

    Matched to GitHub's rules on purpose: links written against the rendered
    markdown — ``tui.md#slash-commands`` is one of them — have to keep working
    once the same document is served from here instead.
    """
    normalized = unicodedata.normalize("NFKD", text).casefold()
    cleaned = re.sub(r"[^\w\- ]+", "", normalized, flags=re.UNICODE)
    slug = cleaned.strip().replace(" ", "-") or "section"
    count = seen.get(slug, 0)
    seen[slug] = count + 1
    return slug if count == 0 else f"{slug}-{count}"


def rewrite_link(href: str) -> str:
    """Point an internal ``.md`` link at the page generated from it."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or not parts.path.endswith(".md"):
        return href
    return urlunsplit(parts._replace(path=parts.path[: -len(".md")] + ".html"))


def anchor_headings(tokens: list[Token]) -> list[tuple[int, str, str]]:
    """Give every heading an id, and return the outline for the page's contents."""
    outline: list[tuple[int, str, str]] = []
    seen: dict[str, int] = {}
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        inline = tokens[index + 1]
        text = "".join(child.content for child in (inline.children or []) if child.type == "text")
        slug = slugify(text or inline.content, seen)
        token.attrSet("id", slug)
        outline.append((int(token.tag[1:]), slug, text or inline.content))
    return outline


def retarget_links(tokens: Iterable[Token]) -> None:
    """Rewrite ``.md`` hrefs, and send external links away in a new tab."""
    for token in tokens:
        for child in token.children or ():
            if child.type != "link_open":
                continue
            href = child.attrGet("href") or ""
            child.attrSet("href", rewrite_link(str(href)))
            if urlsplit(str(href)).netloc:
                child.attrSet("target", "_blank")
                child.attrSet("rel", "noreferrer")


def asset_version() -> str:
    """A short hash of the shared assets, used to bust a stale cached copy.

    Without it a returning reader gets the new HTML against whatever stylesheet
    their browser already had — which is exactly how a screenshot ends up
    rendering at its full 1728px inside a 1140px column.
    """
    digest = hashlib.sha256()
    for name in ("styles.css", "script.js"):
        digest.update((DOCS / name).read_bytes())
    return digest.hexdigest()[:10]


def render(path: Path, version: str) -> str:
    """Turn one markdown file into a complete page."""
    parser = MarkdownIt("commonmark", {"typographer": False})
    parser.enable(["table", "strikethrough", "linkify"])
    tokens = parser.parse(path.read_text(encoding="utf-8"))
    outline = anchor_headings(tokens)
    retarget_links(tokens)
    body = parser.renderer.render(tokens, parser.options, {})

    stem = path.stem
    title = next((text for level, _, text in outline if level == 1), None)
    title = title or FALLBACK_TITLES.get(stem, stem.replace("-", " ").capitalize())
    # The h1 is rendered by the markdown itself, so the page header must not
    # print it a second time.
    contents = [(level, slug, text) for level, slug, text in outline if level == 2]

    nav = "\n          ".join(f'<a href="{href}">{html.escape(label)}</a>' for href, label in NAV)
    toc = ""
    if len(contents) > 2:
        items = "\n            ".join(
            f'<a href="#{slug}">{html.escape(text)}</a>' for _, slug, text in contents
        )
        toc = f"""
        <nav class="doc-toc" aria-label="On this page">
          <div class="doc-toc-label">On this page</div>
          <div class="doc-toc-links">
            {items}
          </div>
        </nav>
"""

    return TEMPLATE.format(
        title=html.escape(title),
        version=version,
        nav=nav,
        body=body,
        toc=toc,
    )


def stamp_index(version: str) -> bool:
    """Version the landing page's own asset links. True when it changed."""
    index = DOCS / "index.html"
    original = index.read_text(encoding="utf-8")
    updated = re.sub(
        r'(href="styles\.css|src="script\.js)(\?v=[0-9a-f]+)?"',
        lambda match: f'{match.group(1)}?v={version}"',
        original,
    )
    if updated == original:
        return False
    index.write_text(updated, encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail instead of writing, for use in CI on a committed build.",
    )
    arguments = parser.parse_args()

    version = asset_version()
    sources = sorted(path for path in DOCS.glob("*.md") if path.name not in NOT_PAGES)
    if not sources:
        raise SystemExit("No markdown pages found in docs/")

    stale: list[str] = []
    for source in sources:
        target = DOCS / f"{source.stem}.html"
        rendered = render(source, version)
        if arguments.check:
            if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
                stale.append(target.name)
            continue
        target.write_text(rendered, encoding="utf-8")

    if arguments.check:
        if stale:
            raise SystemExit(
                "These pages are out of date; run scripts/build_docs_site.py:\n- "
                + "\n- ".join(stale)
            )
        print(f"Documentation pages up to date: {len(sources)} pages, assets v{version}")
        return

    stamped = stamp_index(version)
    print(
        f"Built {len(sources)} documentation pages at assets v{version}"
        + (" (landing page re-stamped)" if stamped else "")
    )


if __name__ == "__main__":
    main()
