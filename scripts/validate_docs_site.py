"""Validate the dependency-free GitHub Pages documentation site."""

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


def main() -> None:
    if not INDEX.is_file():
        raise SystemExit("docs/index.html is missing")

    parser = SiteParser()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    identifiers = set(parser.ids)
    failures: list[str] = []

    duplicates = sorted(item for item, count in Counter(parser.ids).items() if count > 1)
    if duplicates:
        failures.append(f"duplicate element ids: {', '.join(duplicates)}")

    for reference in parser.links:
        parsed = urlsplit(reference)
        if parsed.scheme or parsed.netloc:
            continue
        if parsed.fragment and not parsed.path and parsed.fragment not in identifiers:
            failures.append(f"missing anchor target: #{parsed.fragment}")
        if parsed.path:
            target = (DOCS / parsed.path).resolve()
            if DOCS.resolve() not in target.parents and target != DOCS.resolve():
                failures.append(f"link escapes docs directory: {reference}")
            elif not target.exists():
                failures.append(f"missing local asset: {reference}")

    for target in (*parser.controls, *parser.copy_targets):
        if target not in identifiers:
            failures.append(f"missing controlled element: #{target}")

    required_sections = {
        "installation",
        "getting-started",
        "providers",
        "interfaces",
        "features",
        "reference",
    }
    missing_sections = sorted(required_sections - identifiers)
    if missing_sections:
        failures.append(f"missing required sections: {', '.join(missing_sections)}")

    if failures:
        raise SystemExit("Documentation validation failed:\n- " + "\n- ".join(failures))
    print(f"Documentation site valid: {len(identifiers)} ids, {len(parser.links)} links/assets")


if __name__ == "__main__":
    main()
