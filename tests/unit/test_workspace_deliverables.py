"""Finished files: structure has to survive the crossing out of markdown.

Every assertion here is really the same one — that a heading arrived as a
heading and a table as a table — because the failure this module exists to
prevent is markdown text poured into a .docx and called a Word document.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.config import default_settings, save_settings
from daino.events import EventBus
from daino.persistence import Database
from daino.workbench import deliverables
from daino.workbench.service import WorkbenchService

REPORT = """# Vendor Recommendation

We recommend Beta for our volume.

## Comparison

| Vendor | Price | Seats |
| --- | --- | --- |
| Alpha | 1200 | 50 |
| Beta | 900 | 30 |

## Risks

- The migration window is tight
  - A staged rollout covers it
- Pricing is list, not negotiated

> Revisit at 100 seats.
"""


def test_the_parser_finds_the_structure_a_document_actually_has() -> None:
    blocks = deliverables.parse_markdown(REPORT)

    assert [block.kind for block in blocks] == [
        "heading",
        "paragraph",
        "heading",
        "table",
        "heading",
        "bullets",
        "quote",
    ]
    table = blocks[3]
    assert table.rows[0] == ["Vendor", "Price", "Seats"]
    assert table.rows[2] == ["Beta", "900", "30"]
    # Nesting survives, because a flat list is a different document.
    assert [depth for depth, _ in blocks[5].items] == [0, 1, 0]


def test_emphasis_markers_do_not_reach_the_finished_file() -> None:
    """Asterisks in a Word document are the tell that nothing was rendered."""
    blocks = deliverables.parse_markdown("**Bold** and *italic* and `code` and [link](http://x)")

    assert blocks[0].text == "Bold and italic and code and link (http://x)"


def test_word_output_carries_headings_lists_and_tables() -> None:
    data = deliverables.render(REPORT, "docx")

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "Vendor Recommendation" in document
    # Real Word structure, not text that looks like it: a heading style, a
    # list style, and an actual table element.
    assert "Heading" in document
    # Word stores style ids without the spaces the API takes.
    assert "ListBullet" in document
    assert "ListBullet2" in document
    assert "<w:tbl>" in document
    assert "|" not in document


def test_a_workbook_types_its_numbers_and_heads_its_tables() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    data = deliverables.render(REPORT, "xlsx")

    workbook = openpyxl.load_workbook(io.BytesIO(data))
    assert "Summary" in workbook.sheetnames
    sheet = workbook[[name for name in workbook.sheetnames if name != "Summary"][0]]
    assert [cell.value for cell in sheet[1]] == ["Vendor", "Price", "Seats"]
    # 900 as a number, not as the string "900" — otherwise nothing can add it up.
    assert sheet["B3"].value == 900
    assert sheet.freeze_panes == "A2"


def test_a_deck_gets_a_title_slide_and_one_slide_per_section() -> None:
    pptx = pytest.importorskip("pptx")
    data = deliverables.render(REPORT, "pptx", title="Vendor Recommendation")

    presentation = pptx.Presentation(io.BytesIO(data))
    titles = [slide.shapes.title.text for slide in presentation.slides]
    assert titles[0] == "Vendor Recommendation"
    assert "Comparison" in titles
    assert "Risks" in titles
    # Bullet depth survives into the outline.
    risks = next(s for s in presentation.slides if s.shapes.title.text == "Risks")
    levels = [p.level for p in risks.placeholders[1].text_frame.paragraphs]
    assert 1 in levels


def test_a_pdf_is_produced_without_any_optional_dependency() -> None:
    """The base install must always be able to hand someone a file."""
    data = deliverables.render(REPORT, "pdf", title="Vendor Recommendation")

    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(data))
    text = "".join(page.extract_text() for page in reader.pages)
    assert "Vendor Recommendation" in text
    assert "Beta" in text


def test_an_unknown_format_is_refused_by_name() -> None:
    with pytest.raises(deliverables.DeliverableError, match="docx"):
        deliverables.render(REPORT, "rtf")


# ------------------------------------------------- through the workspace


@pytest.fixture
def workbench(tmp_path: Path) -> Iterator[WorkbenchService]:
    settings = default_settings(tmp_path)
    save_settings(settings, tmp_path)
    database = Database(settings, tmp_path)
    database.initialize()
    yield WorkbenchService(tmp_path, database, events=EventBus())
    database.engine.dispose()


def test_a_deliverable_lands_beside_its_source_as_an_ordinary_artifact(
    workbench: WorkbenchService,
) -> None:
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "report.md", REPORT, author="agent")

    artifact = workbench.save_deliverable(workspace.id, "report.md", "pdf")

    assert artifact.path == "report.pdf"
    assert artifact.kind == "document"
    assert artifact.bytes > 0
    # It is a workspace document like any other: listed, and versioned.
    paths = [item.path for item in workbench.get(workspace.id).artifacts]
    assert paths == ["report.md", "report.pdf"]
    assert workbench.revisions(workspace.id, "report.pdf")


def test_regenerating_a_deliverable_keeps_the_previous_one_in_history(
    workbench: WorkbenchService,
) -> None:
    """A rendering is regenerated, so the last one has to remain recoverable."""
    workspace = workbench.create("Proposal")
    workbench.write_artifact(workspace.id, "report.md", REPORT, author="agent")
    workbench.save_deliverable(workspace.id, "report.md", "pdf")

    workbench.write_artifact(workspace.id, "report.md", REPORT + "\n## Addendum\n", author="agent")
    workbench.save_deliverable(workspace.id, "report.md", "pdf")

    assert len(workbench.revisions(workspace.id, "report.pdf")) == 2
