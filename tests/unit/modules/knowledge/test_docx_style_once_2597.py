"""A DOCX paragraph costs its own text, not a scan of the document's styles, and
its extraction stops between paragraphs and table rows once its caller has
given up, as a PDF's does between pages (#2597).

python-docx's ``Paragraph.style`` scans the styles on every read
(``CT_Styles.default_for`` for a paragraph with no style of its own,
``CT_Styles.get_by_id`` for one with), and the parser read it three times a
paragraph: a 40 KB file of plain paragraphs held a converter for a minute. Cost
is counted in those calls, never in seconds. Documents are built with
python-docx's default template.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import pytest
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.styles import CT_Styles
from docx.table import _Row
from docx.text.paragraph import Paragraph

from faultmaven.modules.knowledge.domain.models.conversion import ConversionErrorCode
from faultmaven.modules.knowledge.domain.services import document_preprocessor as dp
from faultmaven.modules.knowledge.domain.services.document_parser import (
    DocumentParser,
    ExtractionAbandoned,
)
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    DocumentPreprocessor,
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: What ``main`` extracted from ``_styled`` before #2597: a heading style
#: becomes its markdown level; every other paragraph, an absent, unknown or
#: character style's id included, is plain; an id two styles share is the
#: first's; tables follow the paragraphs.
_STYLED_TEXT = """\
# Database failover

## Symptoms

### Replica lag

Check the lag first.

Explicit Normal

Unknown style id

Character style id

## Duplicate style id

| Host | Lag |
| --- | --- |
| db-2 | 40 s |"""


def _save(doc, path: Path) -> Path:
    doc.save(str(path))
    return path


def _styled(tmp_path: Path) -> Path:
    doc = Document()
    for level, text in enumerate(("Database failover", "Symptoms", "Replica lag"), 1):
        doc.add_paragraph(text, style=f"Heading {level}")
    doc.styles.add_style("Heading Note", WD_STYLE_TYPE.PARAGRAPH)
    doc.add_paragraph("Check the lag first.", style="Heading Note")
    # A character style whose name reads as a heading: its id is not a
    # paragraph style's, so the paragraph takes the default style.
    doc.styles.add_style("HEADING 4", WD_STYLE_TYPE.CHARACTER)
    # Two paragraph styles sharing one id: the first, Heading 2, is the one.
    for name, heading in (("Dup Two", "Heading 2"), ("Dup Five", "Heading 5")):
        style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        style.style_id, style.name = "Dup", heading
    for text, style_id in (
        ("Explicit Normal", "Normal"),
        ("Unknown style id", "NoSuchStyle"),
        ("Character style id", "HEADING4"),
        ("Duplicate style id", "Dup"),
    ):
        doc.add_paragraph(text)._p.get_or_add_pPr().style = style_id
    table = doc.add_table(rows=2, cols=2)
    for row, cells in zip(table.rows, (("Host", "Lag"), ("db-2", "40 s"))):
        for cell, text in zip(row.cells, cells):
            cell.text = text
    return _save(doc, tmp_path / "failover.docx")


@pytest.mark.parametrize(
    "styles",
    [(None,), ("Heading 1", "Heading 2", "Quote", None)],
    ids=["unstyled", "four-styles"],
)
def test_500_paragraphs_scan_the_styles_once_per_distinct_style(
    tmp_path, monkeypatch, styles
):
    """On ``main``: 1,500 scans for either document."""
    doc = Document()
    for n in range(500):
        doc.add_paragraph(f"Restart api pod {n}.", style=styles[n % len(styles)])
    path = _save(doc, tmp_path / "plain.docx")
    scans = []
    for name in ("default_for", "get_by_id"):
        scan = getattr(CT_Styles, name)
        monkeypatch.setattr(
            CT_Styles,
            name,
            lambda self, key, _scan=scan: scans.append(key) or _scan(self, key),
        )

    text = DocumentParser().extract_document_text(path).text

    assert text.count("Restart api pod") == 500
    assert len(scans) <= len(styles), f"{len(scans)} style scans for 500 paragraphs"


def test_headings_and_tables_extract_as_before(tmp_path):
    """An unset ``abandoned`` changes nothing."""
    parsed = DocumentParser().extract_document_text(
        _styled(tmp_path), abandoned=threading.Event()
    )

    assert parsed.text == _STYLED_TEXT


def test_a_heading_named_default_style_heads_every_unstyled_paragraph(tmp_path):
    doc = Document()
    doc.styles["Normal"].element.default = None
    doc.styles["Heading 1"].element.default = True
    doc.add_paragraph("Failover")
    doc.add_paragraph("Symptoms", style="Heading 2")
    path = _save(doc, tmp_path / "heading-default.docx")

    assert DocumentParser().extract_document_text(path).text == (
        "# Failover\n\n## Symptoms"
    )


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
    assert "stopped before paragraph 2 of 8" in caplog.text


def test_an_extraction_abandoned_in_a_table_stops_after_its_current_row(
    tmp_path, monkeypatch, caplog
):
    """A row with merged cells costs more the larger its table, so one table is
    no checkpoint."""
    doc = Document()
    doc.add_paragraph("Hosts by lag")
    for _ in range(2):
        doc.add_table(rows=3, cols=1)
    path = _save(doc, tmp_path / "tables.docx")
    abandoned, read, cells = threading.Event(), [], _Row.cells

    def read_then_abandon(row):
        read.append(row)
        abandoned.set()
        return cells.fget(row)

    monkeypatch.setattr(_Row, "cells", property(read_then_abandon))

    with caplog.at_level(logging.WARNING), pytest.raises(ExtractionAbandoned):
        DocumentParser().extract_document_text(path, abandoned=abandoned)

    assert len(read) == 1, f"read {len(read)} rows after its caller left"
    assert "DOCX tables.docx table 1 of 2: its caller stopped waiting" in caplog.text
    assert "stopped before row 2 of 3" in caplog.text


async def test_an_abandoned_docx_worker_stops_after_its_current_paragraph(
    tmp_path, monkeypatch, caplog
):
    """Through the extraction pool, as the PDF test in
    ``test_document_extraction_off_the_loop_2153.py`` does: once its caller has
    timed out, the worker finishes the paragraph it is on and reads no other."""
    doc = Document()
    for n in range(5):
        doc.add_paragraph(f"Restart api pod {n}; kubectl rollout restart the api.")
    path = _save(doc, tmp_path / "runbook.docx")
    gate, finished, read = threading.Event(), threading.Event(), []
    text, extract = Paragraph.text, DocumentParser.extract_document_text

    def slow_text(paragraph):
        gate.wait(5)
        read.append(paragraph)
        return text.fget(paragraph)

    def extract_then_signal(self, *args, **kwargs):
        try:
            return extract(self, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(Paragraph, "text", property(slow_text))
    monkeypatch.setattr(DocumentParser, "extract_document_text", extract_then_signal)
    monkeypatch.setattr(dp, "EXTRACTION_TIMEOUT_SECONDS", 0.25)

    try:
        with caplog.at_level(logging.WARNING):
            result = await DocumentPreprocessor().preprocess(path, _DOCX)
            gate.set()
            assert finished.wait(5), "the worker never finished"
    finally:
        gate.set()

    assert result.error_code == ConversionErrorCode.DOCUMENT_TOO_COMPLEX
    assert len(read) == 1, f"the worker read {len(read)} paragraphs after it left"
    assert "stopped before paragraph 2 of 5" in caplog.text
