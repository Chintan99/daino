"""Uploaded documents become markdown, or say plainly why they could not."""

from __future__ import annotations

from pathlib import Path

import pytest

from daino.workbench import extraction
from daino.workbench.extraction import (
    Extraction,
    ExtractionError,
    extract,
    extract_to_cache,
    extracted_path,
    missing_extra_message,
    needs_extraction,
)

pypdf = pytest.importorskip("pypdf")


def _pdf(path: Path, pages: list[str]) -> Path:
    """Write a real PDF whose pages carry the given text.

    Built by hand rather than with a rendering library so the test suite needs
    no extra dependency: a Type1 base font plus a content stream is the minimum
    a PDF needs for ``extract_text`` to find anything.
    """
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for text in pages:
        writer.add_blank_page(width=612, height=792)
        page = writer.pages[-1]

        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = font
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources

        stream = DecodedStreamObject()
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode())
        page[NameObject("/Contents")] = stream

    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_a_pdf_becomes_markdown_with_a_page_per_section(tmp_path: Path) -> None:
    source = _pdf(tmp_path / "report.pdf", ["Revenue grew 12 percent", "Churn fell to 3 percent"])

    result = extract(source)

    assert result.extractor == "pypdf"
    assert result.pages == 2
    assert "Revenue grew 12 percent" in result.text
    assert "Churn fell to 3 percent" in result.text
    assert "## Page 1" in result.text and "## Page 2" in result.text
    assert result.warnings == []


def test_a_pdf_with_no_text_layer_says_so_instead_of_returning_nothing(
    tmp_path: Path,
) -> None:
    """A scan must not reach the agent as a silently empty document."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    source = tmp_path / "scan.pdf"
    with source.open("wb") as handle:
        writer.write(handle)

    result = extract(source)

    assert result.empty
    assert any("scanned" in warning for warning in result.warnings)
    assert any("OCR" in warning for warning in result.warnings)


def test_a_word_document_keeps_its_headings_and_tables(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")

    document = docx.Document()
    document.add_heading("Pricing review", level=1)
    document.add_paragraph("Three competitors were compared.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Vendor"
    table.cell(0, 1).text = "Price"
    table.cell(1, 0).text = "Acme"
    table.cell(1, 1).text = "$40"
    source = tmp_path / "review.docx"
    document.save(str(source))

    result = extract(source)

    assert result.extractor == "python-docx"
    assert "## Pricing review" in result.text
    assert "Three competitors were compared." in result.text
    assert "| Vendor | Price |" in result.text
    assert "| Acme | $40 |" in result.text


def test_a_spreadsheet_becomes_one_markdown_table_per_sheet(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Q3"
    sheet.append(["Region", "Revenue"])
    sheet.append(["EMEA", 120])
    workbook.create_sheet("Notes").append(["Prepared by finance"])
    source = tmp_path / "figures.xlsx"
    workbook.save(str(source))

    result = extract(source)

    assert result.extractor == "openpyxl"
    assert "## Q3" in result.text and "## Notes" in result.text
    assert "| Region | Revenue |" in result.text
    assert "| EMEA | 120 |" in result.text


def test_a_pipe_in_a_cell_cannot_break_the_table(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")

    workbook = openpyxl.Workbook()
    workbook.active.append(["a|b", "c"])
    source = tmp_path / "pipes.xlsx"
    workbook.save(str(source))

    line = next(line for line in extract(source).text.splitlines() if line.startswith("| a"))

    # Two cells means three unescaped delimiters; the cell's own pipe is escaped
    # and so does not split the row.
    assert line.replace(r"\|", "").count("|") == 3
    assert r"a\|b" in line


def test_slides_are_extracted_one_section_each(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")

    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Roadmap"
    source = tmp_path / "deck.pptx"
    presentation.save(str(source))

    result = extract(source)

    assert result.extractor == "python-pptx"
    assert result.pages == 1
    assert "Roadmap" in result.text


def test_text_formats_need_no_parser(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nNothing to parse here.\n", encoding="utf-8")

    result = extract(source)

    assert result.extractor == "text"
    assert not needs_extraction(source)
    assert "Nothing to parse here." in result.text


def test_a_missing_parser_names_the_extra_to_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install hint is the whole value of failing here; assert on it."""
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.4 not really\n")

    def refuse(module: str, attribute: str, suffix: str) -> object:
        raise ExtractionError(missing_extra_message(suffix))

    monkeypatch.setattr(extraction, "_import", refuse)

    with pytest.raises(ExtractionError) as raised:
        extract(source)

    assert "pip install 'daino[documents]'" in str(raised.value)
    assert "pypdf" in str(raised.value)


def test_a_legacy_binary_format_is_told_what_to_convert_to(tmp_path: Path) -> None:
    source = tmp_path / "old.doc"
    source.write_bytes(b"\xd0\xcf\x11\xe0legacy")

    with pytest.raises(ExtractionError) as raised:
        extract(source)

    assert ".docx" in str(raised.value)


def test_extraction_is_cached_by_content_and_redone_when_the_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reopening a workspace must not re-parse every upload it holds."""
    source = tmp_path / "notes.md"
    source.write_text("first", encoding="utf-8")

    first, target = extract_to_cache(source)
    assert target == extracted_path(source)
    assert target.is_file()

    calls: list[Path] = []
    original = extraction.extract

    def counted(path: Path) -> Extraction:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(extraction, "extract", counted)

    # Unchanged file: served from the cache, parser never invoked.
    cached, _ = extract_to_cache(source)
    assert calls == []
    assert cached.digest == first.digest
    assert "first" in cached.text

    # Changed file: the digest no longer matches, so it re-extracts.
    source.write_text("second", encoding="utf-8")
    refreshed, _ = extract_to_cache(source)
    assert len(calls) == 1
    assert refreshed.digest != first.digest
    assert "second" in refreshed.text


def test_the_cached_markdown_is_readable_on_its_own(tmp_path: Path) -> None:
    """The agent reads this file directly, so it has to stand alone."""
    source = tmp_path / "notes.md"
    source.write_text("Revenue grew.", encoding="utf-8")

    _, target = extract_to_cache(source)
    text = target.read_text(encoding="utf-8")

    assert text.startswith("<!-- daino-extraction")
    assert "# notes" in text
    assert "Revenue grew." in text


def test_reading_a_document_points_the_agent_at_its_extraction(tmp_path: Path) -> None:
    """A decode error teaches a model nothing; the extracted path does."""
    from daino.tools.filesystem import FileTools

    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "report.pdf").write_bytes(b"%PDF-1.4\xd0\xcf")
    (tmp_path / "legacy.doc").write_bytes(b"\xd0\xcf\x11\xe0")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    tools = FileTools(tmp_path)

    document = tools.read_file("uploads/report.pdf")
    legacy = tools.read_file("legacy.doc")
    unknown = tools.read_file("blob.bin")

    assert not document.success
    assert "uploads/.extracted/report.md" in (document.error or "")
    assert ".docx" in (legacy.error or "")
    assert "not UTF-8 text" in (unknown.error or "")
