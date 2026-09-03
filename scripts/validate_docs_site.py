"""Validate the GitHub Pages documentation site.

Checks the hand-written landing page and every page ``build_docs_site.py``
generates from the markdown beside it. Both matter for the same reason: the
site is deployed as static files, so a link that resolves to nothing is a 404
rather than something a server can redirect.
"""

from __future__ import annotations

from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
INDEX = DOCS / "index.html"


class SiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.links: list[str] = []
        self.controls: list[str] = []
        self.copy_targets: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if element_id := values.get("id"):
            self.ids.append(element_id)
        if tag in {"a", "link"} and (href := values.get("href")):
            self.links.append(href)
        if tag in {"img", "script"} and (src := values.get("src")):
            self.links.append(src)
        if controls := values.get("aria-controls"):
            self.controls.append(controls)
        if target := values.get("data-copy-target"):
            self.copy_targets.append(target)


def parse(path: Path) -> SiteParser:
    parser = SiteParser()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def check(path: Path, parser: SiteParser, anchors: dict[str, set[str]]) -> list[str]:
    """Every problem this page has, named with the page it is on."""
    identifiers = anchors[path.name]
    failures: list[str] = []
    where = path.name

    duplicates = sorted(item for item, count in Counter(parser.ids).items() if count > 1)
    if duplicates:
        failures.append(f"{where}: duplicate element ids: {', '.join(duplicates)}")

    for reference in parser.links:
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc:
            continue
        if parsed.fragment and not parsed.path and parsed.fragment not in identifiers:
            failures.append(f"{where}: missing anchor target: #{parsed.fragment}")
        if not parsed.path:
            continue
        target = (DOCS / parsed.path).resolve()
        if DOCS.resolve() not in target.parents and target != DOCS.resolve():
            failures.append(f"{where}: link escapes docs directory: {reference}")
        elif not target.exists():
            failures.append(f"{where}: missing local asset: {reference}")
        # A cross-page fragment is the one a rewritten `.md#section` link
        # produces, and the one most likely to be silently wrong.
        elif parsed.fragment and target.name in anchors:
            if parsed.fragment not in anchors[target.name]:
                failures.append(f"{where}: missing anchor {parsed.fragment} in {target.name}")

    for controlled in (*parser.controls, *parser.copy_targets):
        if controlled not in identifiers:
            failures.append(f"{where}: missing controlled element: #{controlled}")
    return failures


def main() -> None:
    if not INDEX.is_file():
        raise SystemExit("docs/index.html is missing")

    pages = sorted(DOCS.glob("*.html"))
    parsed = {path: parse(path) for path in pages}
    anchors = {path.name: set(parser.ids) for path, parser in parsed.items()}

    failures: list[str] = []
    for path, parser in parsed.items():
        failures.extend(check(path, parser, anchors))

    required_sections = {
        "installation",
        "getting-started",
        "providers",
        "interfaces",
        "features",
        "reference",
    }
    missing_sections = sorted(required_sections - anchors["index.html"])
    if missing_sections:
        failures.append(f"index.html: missing required sections: {', '.join(missing_sections)}")

    # Every markdown file has to have a page built from it, or a link the
    # landing page makes to it 404s on a site that cannot render markdown.
    unbuilt = sorted(
        source.name for source in DOCS.glob("*.md") if not (DOCS / f"{source.stem}.html").is_file()
    )
    if unbuilt:
        failures.append(
            "no page built for: " + ", ".join(unbuilt) + " (run scripts/build_docs_site.py)"
        )

    if failures:
        raise SystemExit("Documentation validation failed:\n- " + "\n- ".join(failures))
    links = sum(len(parser.links) for parser in parsed.values())
    print(
        f"Documentation site valid: {len(pages)} pages, "
        f"{len(anchors['index.html'])} ids, {links} links/assets"
    )


if __name__ == "__main__":
    main()
