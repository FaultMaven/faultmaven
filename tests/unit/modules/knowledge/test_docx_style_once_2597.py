"""A DOCX paragraph costs its own text, not a scan of the document's styles, and
its extraction stops between paragraphs and tables once its caller has given
up, as a PDF's does between pages (#2597).

python-docx's ``Paragraph.style`` scans every style element for a paragraph
with no style of its own (``CT_Styles.default_for``), and the parser read it
twice a paragraph: a 40 KB file of plain paragraphs held a converter for a
minute. Cost is counted in ``default_for`` calls, never in seconds. Documents
are built with python-docx's default template.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.styles import CT_Styles
from docx.text.paragraph import Paragraph

from faultmaven.modules.knowledge.domain.services.document_parser import (
    DocumentParser,
    ExtractionAbandoned,
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

#: What ``main`` extracted from ``_styled`` before #2597: a heading style
#: becomes its markdown level; every other paragraph, an absent, unknown or
#: character style's id included, is plain; tables follow the paragraphs.
_STYLED_TEXT = """\
# Database failover

## Symptoms

### Replica lag

Check the lag first.

Explicit Normal

Unknown style id

Character style id

| Host | Lag |
| --- | --- |
| db-2 | 40 s |"""


def _styled(tmp_path: Path) -> Path:
    doc = Document()
    for level, text in enumerate(("Database failover", "Symptoms", "Replica lag"), 1):
        doc.add_paragraph(text, style=f"Heading {level}")
    doc.styles.add_style("Heading Note", WD_STYLE_TYPE.PARAGRAPH)
    doc.add_paragraph("Check the lag first.", style="Heading Note")
    # A character style whose name reads as a heading: its id is not a
    # paragraph style's, so the paragraph takes the default style.
    doc.styles.add_style("HEADING 4", WD_STYLE_TYPE.CHARACTER)
    for text, style_id in (
        ("Explicit Normal", "Normal"),
        ("Unknown style id", "NoSuchStyle"),
        ("Character style id", "HEADING4"),
    ):
        doc.add_paragraph(text)._p.get_or_add_pPr().style = style_id
    table = doc.add_table(rows=2, cols=2)
    for row, cells in zip(table.rows, (("Host", "Lag"), ("db-2", "40 s"))):
        for cell, text in zip(row.cells, cells):
            cell.text = text
    path = tmp_path / "failover.docx"
    doc.save(str(path))
    return path


def test_a_500_paragraph_document_scans_its_styles_at_most_once(tmp_path, monkeypatch):
    doc = Document()
    for n in range(500):
        doc.add_paragraph(f"Restart api pod {n} after the rollout.")
    path = tmp_path / "plain.docx"
    doc.save(str(path))
    scans, default_for = [], CT_Styles.default_for

    def counted(self, style_type):
        scans.append(style_type)
        return default_for(self, style_type)

    monkeypatch.setattr(CT_Styles, "default_for", counted)
    text = DocumentParser().extract_document_text(path).text

    assert text.count("Restart api pod") == 500
    assert len(scans) <= 1, f"{len(scans)} style scans for 500 paragraphs"


def test_headings_and_tables_extract_as_before(tmp_path):
    """An unset ``abandoned`` changes nothing."""
    parsed = DocumentParser().extract_document_text(
        _styled(tmp_path), abandoned=threading.Event()
    )

    assert parsed.text == _STYLED_TEXT


def test_an_abandoned_extraction_reads_no_paragraph(tmp_path, monkeypatch):
    path = _styled(tmp_path)
    abandoned, read, text = threading.Event(), [], Paragraph.text
    abandoned.set()
    monkeypatch.setattr(
        Paragraph, "text", property(lambda p: read.append(p) or text.fget(p))
    )

    with pytest.raises(ExtractionAbandoned):
        DocumentParser().extract_document_text(path, abandoned=abandoned)

    assert read == []


def test_an_extraction_abandoned_mid_document_stops_after_its_current_paragraph(
    tmp_path, monkeypatch, caplog
):
    path = _styled(tmp_path)
    abandoned, read, text = threading.Event(), [], Paragraph.text

    def read_then_abandon(paragraph):
        read.append(paragraph)
        abandoned.set()
        return text.fget(paragraph)

    monkeypatch.setattr(Paragraph, "text", property(read_then_abandon))

    with caplog.at_level(logging.WARNING), pytest.raises(ExtractionAbandoned):
        DocumentParser().extract_document_text(path, abandoned=abandoned)

    assert len(read) == 1, f"read {len(read)} paragraphs after its caller left"
    assert "DOCX failover.docx: its caller stopped waiting" in caplog.text
    assert "stopped before paragraph 2 of 7" in caplog.text


def test_an_extraction_abandoned_between_tables_stops_after_its_current_table(
    tmp_path, monkeypatch, caplog
):
    doc = Document()
    doc.add_paragraph("Hosts by lag")
    for _ in range(3):
        doc.add_table(rows=1, cols=1).cell(0, 0).text = "db-2"
    path = tmp_path / "tables.docx"
    doc.save(str(path))
    abandoned, read = threading.Event(), []
    to_markdown = DocumentParser._docx_table_to_markdown

    def read_then_abandon(self, table):
        read.append(table)
        abandoned.set()
        return to_markdown(self, table)

    monkeypatch.setattr(DocumentParser, "_docx_table_to_markdown", read_then_abandon)

    with caplog.at_level(logging.WARNING), pytest.raises(ExtractionAbandoned):
        DocumentParser().extract_document_text(path, abandoned=abandoned)

    assert len(read) == 1, f"read {len(read)} tables after its caller left"
    assert "stopped before table 2 of 3" in caplog.text
