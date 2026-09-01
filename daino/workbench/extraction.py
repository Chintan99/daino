"""Turn an uploaded document into markdown the agent can actually read.

Daino's file tools are UTF-8 only: ``FileTools.read_file`` is
``path.read_text(encoding="utf-8")``, so a PDF or a spreadsheet reaches the
model as a decode error. A workspace whose premise is "upload your files" needs
one layer that converts those formats to text once, and then gets out of the
way — everything downstream keeps reading plain markdown from the repository.

Three decisions shape this module:

* **The parsers are optional.** ``pypdf`` and the Office readers are pulled in
  by the ``daino[documents]`` extra, never by the base install. A missing parser
  is reported as a named, actionable gap ("install this extra"), not as a
  traceback and not as an empty document.
* **Extraction is cached by content digest.** Re-uploading the same file, or
  reopening a workspace, must not re-parse a 200-page report. The digest also
  means an edited source is re-extracted without anyone tracking mtimes.
* **An empty result is reported, never hidden.** A scanned PDF has no text
  layer, and there is no OCR here. Saying so is far better than handing the
  agent a blank document it will confidently summarise.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

#: Where an upload's extracted text lands, relative to the uploads directory.
EXTRACTED_DIR = ".extracted"

#: Suffix -> the extra that provides its parser. Everything absent from this
#: table is either already readable as text or genuinely unsupported.
DOCUMENT_SUFFIXES: dict[str, str] = {
    ".pdf": "pypdf",
    ".docx": "python-docx",
    ".xlsx": "openpyxl",
    ".xlsm": "openpyxl",
    ".pptx": "python-pptx",
}

#: Formats that need no parser: the agent can read these directly.
TEXT_SUFFIXES = frozenset(
    {
        ".csv",
        ".htm",
        ".html",
        ".json",
        ".jsonl",
        ".log",
        ".md",
        ".markdown",
        ".rst",
        ".tex",
        ".text",
        ".toml",
        ".tsv",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)

#: Legacy binary formats whose modern sibling is supported. Naming them lets the
#: upload panel say "convert to .docx" instead of "unsupported".
CONVERTIBLE_SUFFIXES: dict[str, str] = {
    ".doc": ".docx",
    ".xls": ".xlsx",
    ".ppt": ".pptx",
}

#: A spreadsheet can be enormous. Extraction is for reading, not for archiving.
MAX_SHEET_ROWS = 2_000
MAX_SHEET_COLUMNS = 64
#: Guard against a pathological document producing a gigabyte of markdown.
MAX_EXTRACTED_CHARS = 2_000_000


class ExtractionError(RuntimeError):
    """Raised when a document cannot be read at all."""


@dataclass(frozen=True, slots=True)
class Extraction:
    """The readable form of one uploaded document."""

    #: Markdown text, or "" when nothing could be extracted.
    text: str
    #: Which parser produced it ("pypdf", "text", "python-docx", …).
    extractor: str
    #: Pages, slides, or sheets, when the format has such a notion.
    pages: int = 0
    #: sha256 of the source file, which is also the cache key.
    digest: str = ""
    #: Non-fatal problems worth telling the user about, in plain language.
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "extractor": self.extractor,
            "pages": self.pages,
            "digest": self.digest,
            "characters": len(self.text),
            "warnings": list(self.warnings),
        }


def supported_suffixes() -> list[str]:
    """Every suffix the workspace can turn into readable text."""
    return sorted(TEXT_SUFFIXES | DOCUMENT_SUFFIXES.keys())


def needs_extraction(path: Path) -> bool:
    """Whether reading this file requires a parser rather than a decode."""
    return path.suffix.casefold() in DOCUMENT_SUFFIXES


def missing_extra_message(suffix: str) -> str:
    """What to tell someone whose install cannot read this format."""
    package = DOCUMENT_SUFFIXES.get(suffix.casefold(), "")
    if not package:
        return f"{suffix} files cannot be read."
    return (
        f"{suffix} files need the document parsers: install them with "
        f"`pip install 'daino[documents]'` (provides {package})."
    )


def extracted_path(source: PurePath) -> PurePath:
    """Where ``source``'s markdown lives, beside the upload it came from.

    Pure path arithmetic, so a caller holding only a repository-relative
    ``PurePosixPath`` can name the extraction without touching the disk.
    """
    return source.parent / EXTRACTED_DIR / f"{source.stem}.md"


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_digest_of_text(text: str) -> str:
    """The same digest for content that is already in memory."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract(source: Path) -> Extraction:
    """Read one document, choosing a parser by suffix.

    Raises :class:`ExtractionError` only when the format has no reader at all.
    A format that is readable but yields nothing returns an empty extraction
    carrying a warning, because "this PDF is a scan" is a useful answer and an
    exception is not.
    """
    source = Path(source)
    if not source.is_file():
        raise ExtractionError(f"{source.name} does not exist")
    suffix = source.suffix.casefold()
    digest = file_digest(source)

    if suffix in TEXT_SUFFIXES or suffix not in DOCUMENT_SUFFIXES:
        return _extract_text(source, digest)
    reader = {
        ".pdf": _extract_pdf,
        ".docx": _extract_docx,
        ".xlsx": _extract_workbook,
        ".xlsm": _extract_workbook,
        ".pptx": _extract_slides,
    }[suffix]
    extraction = reader(source, digest)
    if extraction.empty and not extraction.warnings:
        return _with_warning(extraction, _empty_warning(suffix))
    return extraction


def extract_to_cache(source: Path, *, force: bool = False) -> tuple[Extraction, Path]:
    """Extract ``source`` and persist the markdown, reusing a valid cache.

    The cache is keyed on the source's digest, recorded in the markdown's own
    front matter, so a re-uploaded or edited file re-extracts without anyone
    comparing timestamps.
    """
    source = Path(source)
    target = Path(extracted_path(source))
    digest = file_digest(source)
    if not force and target.is_file():
        cached = _read_cache(target)
        if cached is not None and cached.digest == digest:
            return cached, target

    extraction = extract(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render(source, extraction), encoding="utf-8")
    return extraction, target


# ------------------------------------------------------------------ parsers


def _extract_text(source: Path, digest: str) -> Extraction:
    """Anything already UTF-8 needs no parser, only a decode."""
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        suffix = source.suffix.casefold()
        replacement = CONVERTIBLE_SUFFIXES.get(suffix)
        if replacement:
            raise ExtractionError(
                f"{suffix} is a legacy binary format. Re-save it as {replacement} and upload again."
            ) from None
        raise ExtractionError(
            f"{source.name} is not text, and {suffix or 'this format'} has no reader."
        ) from None
    return Extraction(text=_clip(text), extractor="text", digest=digest)


def _extract_pdf(source: Path, digest: str) -> Extraction:
    reader_class = _import("pypdf", "PdfReader", ".pdf")
    warnings: list[str] = []
    try:
        reader = reader_class(str(source))
        if getattr(reader, "is_encrypted", False):
            # An empty user password is common for "protected" PDFs and costs
            # nothing to try; a real password is a stop.
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001 - any failure means the same thing
                raise ExtractionError(f"{source.name} is password protected.") from None
        pages = list(reader.pages)
    except ExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 - third-party parsers raise freely
        raise ExtractionError(f"{source.name} could not be read: {exc}") from exc

    chunks: list[str] = []
    for number, page in enumerate(pages, start=1):
        try:
            content = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - one bad page must not lose the rest
            warnings.append(f"Page {number} could not be read: {exc}")
            continue
        if content.strip():
            chunks.append(f"## Page {number}\n\n{content.strip()}")
    return Extraction(
        text=_clip("\n\n".join(chunks)),
        extractor="pypdf",
        pages=len(pages),
        digest=digest,
        warnings=warnings,
    )


def _extract_docx(source: Path, digest: str) -> Extraction:
    document_class = _import("docx", "Document", ".docx")
    try:
        document = document_class(str(source))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"{source.name} could not be read: {exc}") from exc

    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        # Word's built-in heading styles are the only reliable structure signal.
        style = (getattr(paragraph.style, "name", "") or "").casefold()
        if style.startswith("heading"):
            level = "".join(char for char in style if char.isdigit())
            lines.append(f"{'#' * min(int(level or 2) + 1, 6)} {text}")
        else:
            lines.append(text)
    for index, table in enumerate(getattr(document, "tables", []), start=1):
        rendered = _render_table([[cell.text.strip() for cell in row.cells] for row in table.rows])
        if rendered:
            lines.append(f"### Table {index}\n\n{rendered}")
    return Extraction(
        text=_clip("\n\n".join(lines)),
        extractor="python-docx",
        digest=digest,
    )


def _extract_workbook(source: Path, digest: str) -> Extraction:
    load_workbook = _import("openpyxl", "load_workbook", source.suffix.casefold())
    try:
        workbook = load_workbook(filename=str(source), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"{source.name} could not be read: {exc}") from exc

    warnings: list[str] = []
    sections: list[str] = []
    try:
        for sheet in workbook.worksheets:
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True):
                if len(rows) >= MAX_SHEET_ROWS:
                    warnings.append(
                        f"Sheet '{sheet.title}' was truncated to the first {MAX_SHEET_ROWS} rows."
                    )
                    break
                cells = ["" if value is None else str(value) for value in row]
                if any(cell.strip() for cell in cells):
                    rows.append(cells[:MAX_SHEET_COLUMNS])
            rendered = _render_table(rows)
            if rendered:
                sections.append(f"## {sheet.title}\n\n{rendered}")
    finally:
        workbook.close()
    return Extraction(
        text=_clip("\n\n".join(sections)),
        extractor="openpyxl",
        pages=len(sections),
        digest=digest,
        warnings=warnings,
    )


def _extract_slides(source: Path, digest: str) -> Extraction:
    presentation_class = _import("pptx", "Presentation", ".pptx")
    try:
        presentation = presentation_class(str(source))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"{source.name} could not be read: {exc}") from exc

    sections: list[str] = []
    slides = list(presentation.slides)
    for number, slide in enumerate(slides, start=1):
        lines = [
            shape.text.strip()
            for shape in slide.shapes
            if getattr(shape, "has_text_frame", False) and shape.text.strip()
        ]
        if lines:
            sections.append(f"## Slide {number}\n\n" + "\n\n".join(lines))
    return Extraction(
        text=_clip("\n\n".join(sections)),
        extractor="python-pptx",
        pages=len(slides),
        digest=digest,
    )


# ---------------------------------------------------------------- utilities


def _import(module: str, attribute: str, suffix: str) -> Any:
    """Import an optional parser, or explain how to install it."""
    try:
        imported = __import__(module, fromlist=[attribute])
    except ImportError:
        raise ExtractionError(missing_extra_message(suffix)) from None
    value = getattr(imported, attribute, None)
    if value is None:
        raise ExtractionError(missing_extra_message(suffix))
    return value


def _render_table(rows: list[list[str]]) -> str:
    """Render rows as a GitHub-flavoured markdown table.

    The first row becomes the header even when the sheet has none: a table
    without a header row is not valid GFM, and a wrong header is easier for a
    reader to see through than a table that fails to render.
    """
    populated = [row for row in rows if any(cell.strip() for cell in row)]
    if not populated:
        return ""
    width = max(len(row) for row in populated)
    padded = [[*row, *[""] * (width - len(row))] for row in populated]
    header, *body = padded
    lines = [
        "| " + " | ".join(_cell(value) for value in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(_cell(value) for value in row) + " |" for row in body)
    return "\n".join(lines)


def _cell(value: str) -> str:
    """Keep a cell on one line and stop a pipe from breaking the table."""
    return value.replace("|", "\\|").replace("\n", " ").strip() or " "


def _clip(text: str) -> str:
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text
    return text[:MAX_EXTRACTED_CHARS] + "\n\n… extraction truncated …\n"


def _empty_warning(suffix: str) -> str:
    if suffix == ".pdf":
        return (
            "No text layer was found. This is usually a scanned document; "
            "Daino does not run OCR, so the text has to be supplied another way."
        )
    return "The file was read successfully but contained no text."


def _with_warning(extraction: Extraction, warning: str) -> Extraction:
    return Extraction(
        text=extraction.text,
        extractor=extraction.extractor,
        pages=extraction.pages,
        digest=extraction.digest,
        warnings=[*extraction.warnings, warning],
    )


_FRONT_MATTER = "<!-- daino-extraction"


def _render(source: Path, extraction: Extraction) -> str:
    """Markdown plus a machine-readable header carrying the cache key."""
    header = [
        _FRONT_MATTER,
        f"source: {source.name}",
        f"extractor: {extraction.extractor}",
        f"digest: {extraction.digest}",
        f"pages: {extraction.pages}",
        *(f"warning: {item}" for item in extraction.warnings),
        "-->",
        "",
        f"# {source.stem}",
        "",
    ]
    body = extraction.text or "_No text could be extracted from this file._"
    return "\n".join(header) + body.rstrip() + "\n"


def _read_cache(target: Path) -> Extraction | None:
    """Recover a previous extraction from its own front matter."""
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith(_FRONT_MATTER):
        return None
    header, _, body = text.partition("-->")
    fields: dict[str, str] = {}
    warnings: list[str] = []
    for line in header.splitlines()[1:]:
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "warning":
            warnings.append(value)
        elif key:
            fields[key] = value
    if "digest" not in fields:
        return None
    return Extraction(
        text=body.lstrip("\n"),
        extractor=fields.get("extractor", "cache"),
        pages=int(fields["pages"]) if fields.get("pages", "").isdigit() else 0,
        digest=fields["digest"],
        warnings=warnings,
    )
