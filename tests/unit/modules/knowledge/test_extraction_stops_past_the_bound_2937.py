"""Extraction stops past the character bound and refuses 413 first (#2937).

No cl100k token is longer than ``MAX_CHARS_PER_TOKEN`` characters, so text
longer than ``MAX_TEXT_CHARS`` is over ``MAX_TOKEN_LIMIT`` tokens as extracted.
The ruling of 2026-11-25: such text is refused ``DOCUMENT_TOO_LONG`` during
Stage 1, ahead of every later check, unless the file has no text
(``NO_TEXT_LAYER``). At or under the bound every outcome is as before.

Through ``prepare`` directly, with a stage spy; ``pypdf`` is mocked for the
PDF shapes, as in ``test_document_parser.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pypdf
import pytest
from pypdf.errors import LimitReachedError

from faultmaven.modules.knowledge.domain.services import preparation
from faultmaven.modules.knowledge.domain.services.document_parser import (
    DocumentParser,
    DocumentRefusal,
    DocumentTooLong,
)
from faultmaven.modules.knowledge.domain.services.preparation import (
    MAX_CHARS_PER_TOKEN,
    MAX_TEXT_CHARS,
    MAX_TOKEN_LIMIT,
    MIN_TEXT_LENGTH,
    check_technical_content,
    cleanup_text,
    count_tokens,
    prepare,
)
from tests.runbook_samples import valid_runbook

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

_SENTENCE = (
    "Document contains more than 30,000 tokens (limit: 30,000). Please split "
    "the document into smaller, focused chapters and convert each one separately."
)
_TOO_LONG = {"refused": "DOCUMENT_TOO_LONG", "reason": _SENTENCE}

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: Ordinary prose: no technical signal, so ``main`` answers NO_TECHNICAL_CONTENT.
_PROSE = "The quarterly garden party moved to the north lawn this year. "


def _run(path: Path, content_type: str) -> tuple[dict, list]:
    entered: list = []
    return prepare(path, content_type, entered.append), entered


def _unbounded(path: Path, content_type: str, monkeypatch) -> dict:
    """``prepare`` with no character bound: what ``main`` answered."""
    with monkeypatch.context() as patched:
        patched.setattr(preparation, "MAX_TEXT_CHARS", 10**12)
        return prepare(path, content_type, lambda stage: None)


def _file(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _prose(length: int) -> str:
    return (_PROSE * (length // len(_PROSE) + 1))[:length]


class _Page:
    """A pypdf page: its text, or a limit error when it has none: a budget's,
    or ``error``, pypdf's wording for corruption."""

    def __init__(self, text: str | None = None, error: str = "budget") -> None:
        self.text = text
        self.error = error
        self.reads = 0

    def extract_text(self) -> str:
        self.reads += 1
        if self.text is None:
            raise LimitReachedError(self.error)
        return self.text


def _pdf(tmp_path: Path, monkeypatch, *pages: _Page) -> Path:
    reader = MagicMock(pages=list(pages))
    monkeypatch.setattr(pypdf, "PdfReader", MagicMock(return_value=reader))
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    return path


def _docx(tmp_path: Path, paragraphs: list[str]) -> Path:
    from docx import Document

    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    path = tmp_path / "source.docx"
    document.save(str(path))
    return path


# ── Pins 1-4: over the bound, the 413 comes first ────────────────────────────


def test_over_bound_runbook_markdown_is_refused_before_the_runbook_gate(tmp_path):
    """Pin 1: ``main`` answered ALREADY_A_RUNBOOK at Stage 1b."""
    head = valid_runbook() + "\n"
    path = _file(tmp_path, "runbook.md", head + "x" * (MAX_TEXT_CHARS + 1 - len(head)))

    result, entered = _run(path, "text/markdown")

    assert result == _TOO_LONG
    assert entered == [0, 1]


def test_over_bound_non_technical_text_is_refused_too_long(tmp_path):
    """Pin 2: ``main`` answered NO_TECHNICAL_CONTENT at Stage 2b."""
    assert not check_technical_content(_prose(10_000))[0]
    path = _file(tmp_path, "notes.txt", _prose(MAX_TEXT_CHARS + 1))

    result, entered = _run(path, "text/plain")

    assert result == _TOO_LONG
    assert entered == [0, 1]


def test_over_bound_text_cleanup_would_shrink_is_refused_too_long(tmp_path):
    """Pin 3: page footers, every one stripped by cleanup: ``main`` answered
    DOCUMENT_TOO_SHORT. What cleanup would remove is not measured."""
    footers = "Page 1\n" * (MAX_TEXT_CHARS // 7 + 1)
    text = footers + "a short tail"
    assert len(cleanup_text(text)) < MIN_TEXT_LENGTH
    path = _file(tmp_path, "footers.txt", text)

    result, _ = _run(path, "text/plain")

    assert result == _TOO_LONG


def test_over_bound_pdf_with_refused_pages_is_refused_too_long(tmp_path, monkeypatch):
    """Pin 4: ``main`` answered preparation's refused-pages
    DOCUMENT_TOO_COMPLEX (the readable text is not technical)."""
    path = _pdf(tmp_path, monkeypatch, _Page(), _Page(_prose(MAX_TEXT_CHARS + 1)))

    result, entered = _run(path, "application/pdf")

    assert result == _TOO_LONG
    assert entered == [0, 1]


@pytest.mark.parametrize("corrupt_first", [False, True])
def test_a_corrupt_page_answers_by_its_order_against_the_bound(
    tmp_path, monkeypatch, corrupt_first
):
    """A corrupt page after the page whose text passes the bound is never
    read: 413, where ``main`` answered FILE_CORRUPT. One before it is read
    first and is still FILE_CORRUPT."""
    corrupt = _Page(error="Detected cycle in the page tree")
    crossing = _Page(_prose(MAX_TEXT_CHARS + 1))
    pages = (corrupt, crossing) if corrupt_first else (crossing, corrupt)
    path = _pdf(tmp_path, monkeypatch, *pages)

    result, _ = _run(path, "application/pdf")

    if corrupt_first:
        assert result == {
            "refused": "FILE_CORRUPT",
            "reason": "Failed to parse source.pdf (LimitReachedError)",
        }
    else:
        assert result == _TOO_LONG
        assert corrupt.reads == 0


def test_a_pdf_of_refused_pages_and_no_text_is_still_too_complex(tmp_path, monkeypatch):
    """Pin 4: the parser's DOCUMENT_TOO_COMPLEX is unchanged."""
    path = _pdf(tmp_path, monkeypatch, _Page(), _Page(" \n "))

    result, _ = _run(path, "application/pdf")

    assert result["refused"] == "DOCUMENT_TOO_COMPLEX"
    assert result["reason"].startswith("This PDF is too complex")


def test_over_bound_docx_is_refused_too_long(tmp_path):
    """At the true bound: one character past it, joined text counted."""
    half = MAX_TEXT_CHARS // 2
    path = _docx(tmp_path, ["a" * half, "b" * (MAX_TEXT_CHARS - half - 1)])

    assert _run(path, _DOCX)[0] == _TOO_LONG


# ── Pin 5: no text still answers NO_TEXT_LAYER (a guard: passes on main) ────


@pytest.mark.parametrize(
    "name, content_type", [("blank.txt", "text/plain"), ("blank.md", "text/markdown")]
)
def test_a_whitespace_file_over_the_bound_has_no_text_layer(
    tmp_path, name, content_type
):
    path = _file(tmp_path, name, " \n\t" * (MAX_TEXT_CHARS // 3 + 1))

    result, _ = _run(path, content_type)

    assert result["refused"] == "NO_TEXT_LAYER"


@pytest.mark.parametrize(
    "name, body",
    [
        # At the true bound this is about 29 MB through BeautifulSoup, so the
        # parser runs with a small bound instead (the PR states the departure).
        ("blank.html", "<p> </p><pre>\n \n</pre><table></table>" * 20),
        # Blanked by ``mask_html_comments`` before the bound is checked.
        ("comments.md", "<!-- a note -->\n" * 20),
    ],
    ids=["html", "markdown-comments"],
)
def test_a_file_with_no_text_past_a_small_bound_has_no_text_layer(tmp_path, name, body):
    path = _file(tmp_path, name, body)

    with pytest.raises(DocumentRefusal) as refused:
        DocumentParser().extract_document_text(path, max_chars=10)

    assert refused.value.code == "NO_TEXT_LAYER"


# ── Pin 6: at or under the bound, nothing changes ────────────────────────────


def test_exactly_the_bound_is_not_refused_early(tmp_path, monkeypatch):
    """The comparison is strict: TXT of exactly ``MAX_TEXT_CHARS`` answers
    what it answered on ``main`` (a guard: passes on main)."""
    path = _file(tmp_path, "notes.txt", _prose(MAX_TEXT_CHARS))

    result, entered = _run(path, "text/plain")

    assert result["refused"] == "NO_TECHNICAL_CONTENT"
    assert result == _unbounded(path, "text/plain", monkeypatch)


def test_a_docx_of_exactly_the_bound_is_not_refused_early(tmp_path, monkeypatch):
    half = MAX_TEXT_CHARS // 2
    path = _docx(tmp_path, ["a" * half, "b" * (MAX_TEXT_CHARS - half - 2)])

    result, _ = _run(path, _DOCX)

    assert result["refused"] == "NO_TECHNICAL_CONTENT"
    assert result == _unbounded(path, _DOCX, monkeypatch)


@pytest.mark.parametrize("extra, refused", [(0, "NO_TECHNICAL_CONTENT"), (1, None)])
def test_a_page_ending_in_cr_is_counted_as_normalised(
    tmp_path, monkeypatch, extra, refused
):
    """A page ending ``\\r`` joined by ``\\n\\n``: the CRLF collapses across
    the join, so the raw text is one longer than the text returned. Exactly
    the bound is not refused; one past it is."""
    first = "a" * 1000 + "\r"
    rest = MAX_TEXT_CHARS - 1000 - 2 + extra
    path = _pdf(tmp_path, monkeypatch, _Page(first), _Page(_prose(rest)))

    result, _ = _run(path, "application/pdf")

    if refused is None:
        assert result == _TOO_LONG
    else:
        assert result["refused"] == refused
        assert result == _unbounded(path, "application/pdf", monkeypatch)


@pytest.mark.parametrize(
    "name, text",
    [
        ("short.txt", "kubectl rollout restart failed"),
        ("prose.txt", _prose(50_000)),
        ("runbook.md", valid_runbook()),
        # Over 30,000 tokens, under the bound: Stage 4's exact count.
        ("long.txt", "ERROR timeout connecting to db-7 on port 5432\n" * 8_000),
    ],
    ids=["too-short", "not-technical", "runbook", "stage-4-count"],
)
def test_under_the_bound_every_outcome_is_unchanged(tmp_path, monkeypatch, name, text):
    path = _file(tmp_path, name, text)
    content_type = "text/markdown" if name.endswith(".md") else "text/plain"

    result, _ = _run(path, content_type)

    assert result == _unbounded(path, content_type, monkeypatch)
    if name == "long.txt":
        assert result["refused"] == "DOCUMENT_TOO_LONG"
        assert result["reason"].startswith("Document contains 103,999 tokens")


# ── Pin 7: the bound's premise ───────────────────────────────────────────────


def test_space_runs_just_over_the_bound_are_over_the_token_limit():
    assert count_tokens(" " * (MAX_TEXT_CHARS + 1)) > MAX_TOKEN_LIMIT


def test_no_cl100k_token_is_longer_than_max_chars_per_token():
    """Read from the encoding itself (shipped with the tests, no network): the
    longest token is ``MAX_CHARS_PER_TOKEN`` bytes, so at most that many
    characters."""
    tiktoken = pytest.importorskip("tiktoken")

    vocabulary = tiktoken.get_encoding("cl100k_base").token_byte_values()

    assert max(map(len, vocabulary)) == MAX_CHARS_PER_TOKEN


# ── The stop: the parser reads no further, and prepare passes the bound ──────


def test_a_pdf_stops_being_read_past_the_bound(tmp_path, monkeypatch):
    pages = [_Page("x" * 20) for _ in range(100)]
    path = _pdf(tmp_path, monkeypatch, *pages)

    with pytest.raises(DocumentTooLong):
        DocumentParser().extract_document_text(path, max_chars=50)

    assert sum(page.reads for page in pages) == 3


def test_a_docx_stops_being_read_past_the_bound(tmp_path, monkeypatch):
    from docx.text.paragraph import Paragraph

    path = _docx(tmp_path, ["x" * 20] * 100)
    reads = []
    text = Paragraph.text
    monkeypatch.setattr(
        Paragraph, "text", property(lambda p: reads.append(1) or text.fget(p))
    )

    with pytest.raises(DocumentTooLong):
        DocumentParser().extract_document_text(path, max_chars=50)

    assert len(reads) == 3


def test_html_stops_being_read_past_the_bound(tmp_path, monkeypatch):
    from bs4 import Tag

    path = _file(tmp_path, "page.html", f"<p>{'x' * 20}</p>" * 100)
    reads = []
    get_text = Tag.get_text
    monkeypatch.setattr(
        Tag,
        "get_text",
        lambda self, *a, **k: reads.append(1) or get_text(self, *a, **k),
    )

    with pytest.raises(DocumentTooLong):
        DocumentParser().extract_document_text(path, max_chars=50)

    assert len(reads) == 3


def test_prepare_passes_the_bound(tmp_path, monkeypatch):
    seen = []

    def spy(self, file_path, content_type=None, *, max_chars):
        seen.append(max_chars)
        raise DocumentTooLong(max_chars)

    monkeypatch.setattr(DocumentParser, "extract_document_text", spy)

    result, _ = _run(_file(tmp_path, "notes.txt", "text"), "text/plain")

    assert seen == [MAX_TEXT_CHARS] == [MAX_TOKEN_LIMIT * MAX_CHARS_PER_TOKEN]
    assert result == _TOO_LONG
