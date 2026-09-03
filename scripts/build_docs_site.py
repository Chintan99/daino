"""Render ``docs/*.md`` into the documentation site: pages, nav, and search.

GitHub Pages serves this repository's ``docs/`` folder, so a ``.md`` file in
there is not a page — it is either a download or, when Pages builds the branch
with Jekyll, a default-themed conversion that shares nothing with the site
around it. Both were happening: the landing page's "read more" links pointed at
``github.com/.../blob/v2/docs/*.md`` because there was nothing else to point at,
and once they were repointed the reader landed on Jekyll's rendering instead.

This builds the real thing. Every markdown file becomes ``docs/<name>.html``
wearing the site's own header, sidebar, contents rail, and stylesheet; internal
``.md`` links are rewritten to the pages beside them; and ``search-index.json``
is written so the whole set can be searched without a server.

    python scripts/build_docs_site.py            # build
    python scripts/build_docs_site.py --check    # fail if the build is stale

The output is committed, because Pages may be serving the branch directly
rather than a workflow artifact. ``docs/.nojekyll`` is what stops it rewriting
these pages on the way out.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
HERE = Path(__file__).resolve().parent
TEMPLATE = (HERE / "docs_page_template.html").read_text(encoding="utf-8")

#: The sidebar, and with it the reading order. Grouped by what the reader is
#: trying to do rather than alphabetically: a list of twenty-two file names is a
#: directory listing, not documentation. A markdown file missing from here is
#: reported rather than quietly left out of the navigation.
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Start here", ("installation", "getting-started", "features")),
    ("Interfaces", ("tui", "gui", "workspace")),
    ("Models", ("providers", "model-routing")),
    (
        "Working with it",
        ("missions", "memory", "repository-intelligence", "extending", "playbooks", "evals"),
    ),
    ("Operating", ("configuration", "runtimes", "security", "deployment", "infrastructure")),
    ("Reference", ("cli-reference", "architecture", "contributing")),
)

#: Shown in the sidebar and search results instead of the file name.
TITLES = {
    "cli-reference": "CLI reference",
    "getting-started": "Getting started",
    "gui": "Browser IDE (GUI)",
    "model-routing": "Model routing",
    "repository-intelligence": "Repository intelligence",
    "tui": "Terminal UI (TUI)",
}

#: How much of a section's prose is indexed. Enough to match a phrase the reader
#: half-remembers, bounded so the index stays a small download.
SECTION_CHARS = 700


def nav_title(stem: str) -> str:
    return TITLES.get(stem) or stem.replace("-", " ").capitalize()


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
    """Give every heading an id, and return the outline for nav and search."""
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
            href = str(child.attrGet("href") or "")
            child.attrSet("href", rewrite_link(href))
            if urlsplit(href).netloc:
                child.attrSet("target", "_blank")
                child.attrSet("rel", "noreferrer")


#: Markdown that means nothing once the text is an excerpt in a result list:
#: emphasis markers, backticks, link syntax around the words it wraps, and the
#: box-drawing characters a directory tree is made of.
_NOISE = (
    (re.compile(r"!?\[([^\]]*)\]\([^)]*\)"), r"\1"),
    (re.compile(r"[*_`>|#]+"), " "),
    (re.compile(r"[\u2500-\u257f]+"), " "),
)


def section_text(tokens: list[Token], start: int) -> str:
    """The prose under one heading, flattened and cleaned for the search index."""
    collected: list[str] = []
    for token in tokens[start + 3 :]:
        if token.type == "heading_open":
            break
        if token.type in {"inline", "fence", "code_block"}:
            collected.append(token.content)
        if sum(len(item) for item in collected) > SECTION_CHARS * 2:
            break
    text = " ".join(collected)
    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)
    return " ".join(text.split())[:SECTION_CHARS]


def build_index(stem: str, title: str, tokens: list[Token]) -> list[dict[str, str]]:
    """One search entry per heading, so a hit lands on the section, not the page."""
    entries: list[dict[str, str]] = []
    for index, token in enumerate(tokens):
        if token.type != "heading_open" or token.tag not in {"h1", "h2", "h3"}:
            continue
        anchor = str(token.attrGet("id") or "")
        entries.append(
            {
                "page": title,
                "heading": "" if token.tag == "h1" else tokens[index + 1].content,
                "url": f"{stem}.html" if token.tag == "h1" else f"{stem}.html#{anchor}",
                "text": section_text(tokens, index),
            }
        )
    return entries


def asset_version() -> str:
    """A short hash of the shared assets, used to bust a stale cached copy.

    Without it a returning reader gets the new HTML against whatever stylesheet
    their browser already had — which is exactly how a screenshot ends up
    rendering at its full 1728px inside a 1140px column.
    """
    digest = hashlib.sha256()
    for name in ("styles.css", "script.js", "docs.js"):
        digest.update((DOCS / name).read_bytes())
    return digest.hexdigest()[:10]


def sidebar_html(current: str) -> str:
    """The full documentation index, with the open page marked."""
    blocks: list[str] = []
    for group, stems in GROUPS:
        links = "\n".join(
            '            <a href="{stem}.html"{aria}>{label}</a>'.format(
                stem=stem,
                aria=' class="current" aria-current="page"' if stem == current else "",
                label=html.escape(nav_title(stem)),
            )
            for stem in stems
        )
        blocks.append(
            '          <div class="doc-nav-group">\n'
            f'            <div class="doc-nav-label">{html.escape(group)}</div>\n'
            f"{links}\n"
            "          </div>"
        )
    return "\n".join(blocks)


def pager_html(stem: str) -> str:
    """Previous and next page, in the sidebar's reading order."""
    order = [item for _, stems in GROUPS for item in stems]
    position = order.index(stem)
    parts: list[str] = []
    if position > 0:
        previous = order[position - 1]
        parts.append(
            f'            <a class="doc-pager-prev" href="{previous}.html">'
            f"<span>Previous</span><strong>{html.escape(nav_title(previous))}</strong></a>"
        )
    if position < len(order) - 1:
        following = order[position + 1]
        parts.append(
            f'            <a class="doc-pager-next" href="{following}.html">'
            f"<span>Next</span><strong>{html.escape(nav_title(following))}</strong></a>"
        )
    return "\n".join(parts)


def group_of(stem: str) -> tuple[str, str]:
    """The sidebar group this page sits in, and the page that opens it."""
    for group, stems in GROUPS:
        if stem in stems:
            return group, f"{stems[0]}.html"
    return "Documentation", "index.html"


def description_of(tokens: list[Token], title: str) -> str:
    """The opening sentence, for the page's meta description."""
    first = next(
        (
            token.content
            for index, token in enumerate(tokens)
            if token.type == "inline" and index and tokens[index - 1].type == "paragraph_open"
        ),
        "",
    )
    cleaned = " ".join(re.sub(r"[`*\[\]]", "", first).split())[:180]
    return cleaned or f"{title} — D[Ai]NO documentation."


def toc_html(outline: list[tuple[int, str, str]]) -> str:
    """The contents rail, for a page long enough to need one."""
    contents = [(slug, text) for level, slug, text in outline if level == 2]
    if len(contents) <= 2:
        return ""
    items = "\n            ".join(
        f'<a href="#{slug}">{html.escape(text)}</a>' for slug, text in contents
    )
    return (
        '      <nav class="doc-toc" aria-label="On this page">\n'
        '        <div class="doc-toc-inner">\n'
        '          <div class="doc-toc-label">On this page</div>\n'
        '          <div class="doc-toc-links">\n'
        f"            {items}\n"
        "          </div>\n"
        "        </div>\n"
        "      </nav>\n"
    )


def render(path: Path, version: str) -> tuple[str, list[dict[str, str]]]:
    """Turn one markdown file into a page and its search entries."""
    parser = MarkdownIt("commonmark", {"typographer": False})
    parser.enable(["table", "strikethrough", "linkify"])
    tokens = parser.parse(path.read_text(encoding="utf-8"))
    outline = anchor_headings(tokens)
    retarget_links(tokens)
    body = parser.renderer.render(tokens, parser.options, {})

    stem = path.stem
    title = next((text for level, _, text in outline if level == 1), "") or nav_title(stem)
    group, group_href = group_of(stem)
    page = TEMPLATE.format(
        title=html.escape(title),
        description=html.escape(description_of(tokens, title)),
        version=version,
        sidebar=sidebar_html(stem),
        body=body,
        toc=toc_html(outline),
        pager=pager_html(stem),
        group=html.escape(group),
        group_href=group_href,
    )
    return page, build_index(stem, title, tokens)


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


def out_of_step(paths: list[Path]) -> list[str]:
    """Markdown the sidebar does not mention, and nav entries with no file."""
    listed = {stem for _, stems in GROUPS for stem in stems}
    present = {path.stem for path in paths}
    return sorted(
        [f"{stem}.md is not listed in the sidebar" for stem in present - listed]
        + [f"{stem}.md is in the sidebar but does not exist" for stem in listed - present]
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the documentation site.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail instead of writing, so CI can reject a stale committed build.",
    )
    arguments = parser.parse_args()

    paths = sorted(DOCS.glob("*.md"))
    if not paths:
        raise SystemExit("No markdown pages found in docs/")
    if problems := out_of_step(paths):
        raise SystemExit("Documentation navigation is out of step:\n- " + "\n- ".join(problems))

    version = asset_version()
    entries: list[dict[str, str]] = []
    pages: dict[Path, str] = {}
    for source in paths:
        page, index = render(source, version)
        pages[DOCS / f"{source.stem}.html"] = page
        entries.extend(index)
    index_json = json.dumps({"pages": entries}, separators=(",", ":")) + "\n"
    search = DOCS / "search-index.json"

    if arguments.check:
        stale = [
            target.name
            for target, page in pages.items()
            if not target.is_file() or target.read_text(encoding="utf-8") != page
        ]
        if not search.is_file() or search.read_text(encoding="utf-8") != index_json:
            stale.append(search.name)
        if stale:
            raise SystemExit(
                "These build outputs are out of date; run scripts/build_docs_site.py:\n- "
                + "\n- ".join(stale)
            )
        print(f"Documentation build up to date: {len(pages)} pages, assets v{version}")
        return

    for target, page in pages.items():
        target.write_text(page, encoding="utf-8")
    search.write_text(index_json, encoding="utf-8")
    # Pages may be building this branch with Jekyll, which would rewrite every
    # page above into its own default theme. This is what stops it.
    (DOCS / ".nojekyll").touch()
    stamped = stamp_index(version)
    print(
        f"Built {len(pages)} pages and {len(entries)} search entries "
        f"({len(index_json) // 1024} KB) at assets v{version}"
        + (" (landing page re-stamped)" if stamped else "")
    )


if __name__ == "__main__":
    main()
