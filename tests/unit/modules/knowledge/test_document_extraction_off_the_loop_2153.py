"""An uploaded document is prepared off the event loop, under a time bound, and
a PDF page pypdf refuses for its limits costs that page, not the document (#2153).

``DocumentPreprocessor.preprocess`` ran Stages 0-5 (extraction, cleanup,
redaction, token counting) inline in a coroutine: a dense PDF measured 53.6 s
of event-loop stall, during which every other request in the process waited.
And one page pypdf refused for its limits raised out of the page loop, so the
whole document was rejected as ``FILE_CORRUPT`` and every other page's text was
lost.

PDFs here are a few hundred bytes, built by hand. A page is refused by lowering
pypdf's per-stream parse budget in the test's own context, never by a stream
dense enough to hit the default, which costs tens of seconds of CPU.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from pypdf import PageObject, apply_configuration
from pypdf.errors import LimitReachedError

from faultmaven.modules.knowledge.domain.models.conversion import ConversionErrorCode
from faultmaven.modules.knowledge.domain.services import document_preprocessor as dp
from faultmaven.modules.knowledge.domain.services.document_parser import (
    DocumentParser,
    ParsedDocument,
)
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    DocumentPreprocessor,
)
from tests.unit.modules.knowledge.test_document_preprocessor_existing_runbook import (
    _app_with_real_service,
)
from tests.unit.modules.knowledge.test_gate_stays_off_the_event_loop import (
    _ticks_during,
)

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

#: Long and technical enough to clear the length and technical-content checks.
_TEXT = "ERROR connection timeout to postgres; kubectl rollout restart the api. " * 5

#: 600 operators: past a parse budget lowered to 1,000, far under the default
#: 2,000,000, and parsed in milliseconds.
_DENSE = b"BT /F1 12 Tf 72 720 Td " + b"(ab) Tj " * 600 + b"ET"

#: A parse budget the dense stream exceeds and a one-string page does not.
_LOW_BUDGET = {"stream_parsing_work_maximum_count": 1_000}


def _stream(words: str) -> bytes:
    return b"BT /F1 12 Tf 72 720 Td (" + words.encode() + b") Tj ET"


def _pdf(tmp_path: Path, *streams: bytes) -> Path:
    """A minimal PDF with one page per content stream."""
    kids = b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(len(streams)))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(streams)),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, stream in enumerate(streams):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R"
            b" /Resources << /Font << /F1 3 0 R >> >> >>" % (5 + 2 * i)
        )
        objects.append(
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path = tmp_path / "source.pdf"
    path.write_bytes(out)
    return path


def _text_file(tmp_path: Path) -> Path:
    """A file that passes the integrity check, for a stubbed extraction."""
    path = tmp_path / "notes.txt"
    path.write_text("replaced by the stub", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# A limit refusal costs a page, or is DOCUMENT_TOO_COMPLEX, never FILE_CORRUPT
# ---------------------------------------------------------------------------


async def test_a_refused_page_costs_that_page_and_the_limit_reaches_the_worker(
    tmp_path,
):
    """Page 2 is refused, page 1's text is kept, and the warning names page 2.

    The budget is lowered in THIS coroutine. pypdf's limits are a context
    variable, so page 2 is refused only if the worker runs in a copy of the
    caller's context: ``run_in_executor`` does not copy it by itself.
    """
    path = _pdf(tmp_path, _stream(_TEXT), _DENSE)

    with apply_configuration(**_LOW_BUDGET):
        result = await DocumentPreprocessor().preprocess(path, "application/pdf")

    assert not result.is_rejected, (result.error_code, result.rejection_reason)
    assert "kubectl rollout restart" in result.extracted_text
    assert result.source_metadata["page_count"] == 2
    assert [w for w in result.warnings if "page(s) 2 of 2" in w], result.warnings


@pytest.mark.parametrize(
    "streams, limit",
    [
        pytest.param((_DENSE, _DENSE), _LOW_BUDGET, id="every-page-refused"),
        # Raised from ``len(reader.pages)``, before any page is read.
        pytest.param(
            (_stream(_TEXT),), {"page_tree_maximum_depth": 0}, id="document-level"
        ),
    ],
)
async def test_a_limit_refusal_is_document_too_complex(tmp_path, streams, limit):
    path = _pdf(tmp_path, *streams)

    with apply_configuration(**limit):
        result = await DocumentPreprocessor().preprocess(path, "application/pdf")

    assert result.is_rejected
    assert result.error_code == ConversionErrorCode.DOCUMENT_TOO_COMPLEX
    assert "too complex for the PDF reader's limits" in result.rejection_reason


def test_post_convert_answers_document_too_complex(tmp_path, monkeypatch):
    """Through the route, with the real service and preprocessor. The status
    map answers 422 for a code it does not list, so the code is what this
    asserts, not only the status."""

    def refuse(self, *args, **kwargs):
        raise LimitReachedError("Reached maximum number of tokens per stream.")

    monkeypatch.setattr(PageObject, "extract_text", refuse)
    pdf = _pdf(tmp_path, _stream(_TEXT)).read_bytes()

    with _app_with_real_service() as client:
        response = client.post(
            "/api/v1/knowledge/convert",
            data={"scope": "personal"},
            files={"file": ("dense.pdf", pdf, "application/pdf")},
        )

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == "DOCUMENT_TOO_COMPLEX", response.text


@pytest.mark.parametrize(
    "name, body, content_type, code",
    [
        ("empty.pdf", b"", "application/pdf", "FILE_EMPTY"),
        ("renamed.pdf", b"not a pdf", "application/pdf", "FILE_CORRUPT"),
        ("data.csv", b"a,b,c", "application/octet-stream", "UNSUPPORTED_FORMAT"),
    ],
)
async def test_the_other_refusals_keep_their_codes_in_the_worker(
    tmp_path, name, body, content_type, code
):
    """The integrity and format refusals, now answered from the worker."""
    path = tmp_path / name
    path.write_bytes(body)

    result = await DocumentPreprocessor().preprocess(path, content_type)

    assert result.is_rejected
    assert result.error_code == code, result.rejection_reason


# ---------------------------------------------------------------------------
# Off the loop, in the pool, under the time bound
# ---------------------------------------------------------------------------


async def test_preparation_runs_in_the_extraction_pool(tmp_path, monkeypatch):
    seen: list[str] = []

    def record(self, file_path, content_type=None):
        seen.append(threading.current_thread().name)
        return ParsedDocument(_TEXT)

    monkeypatch.setattr(DocumentParser, "extract_document_text", record)

    await DocumentPreprocessor().preprocess(_text_file(tmp_path), "text/plain")

    assert len(seen) == 1 and seen[0].startswith("document-extract"), seen


async def test_preparation_does_not_stall_the_event_loop(tmp_path, monkeypatch):
    """A tick count, not a duration, for the reason ``_ticks_during`` gives.

    The inline run is the positive control: Stages 0-5 called from a coroutine,
    as before the fix, give the loop no tick at all. Without it a payload too
    small to stall anything would pass this while proving nothing.
    """

    def burn(self, file_path, content_type=None):
        sum(i * i for i in range(2_000_000))
        return ParsedDocument(_TEXT)

    monkeypatch.setattr(DocumentParser, "extract_document_text", burn)
    preprocessor, path = DocumentPreprocessor(), _text_file(tmp_path)

    async def inline():
        return preprocessor._prepare(path, "text/plain")

    async def through_preprocess():
        return await preprocessor.preprocess(path, "text/plain")

    inline_ticks = await _ticks_during(inline)
    hopped_ticks = await _ticks_during(through_preprocess)

    assert inline_ticks == 0, (
        f"positive control failed: the loop ran {inline_ticks} times during the "
        "inline preparation, so the stub is not burning enough CPU to mean anything"
    )
    assert hopped_ticks >= 3, (
        f"the loop ran only {hopped_ticks} times while preprocess prepared the "
        "document: the stages are not leaving the event loop"
    )


async def test_a_preparation_past_its_time_limit_is_refused(tmp_path, monkeypatch):
    """The request is answered at the limit, while the worker runs on. The
    worker is released and waited for, so none outlives the test."""
    release, finished = threading.Event(), threading.Event()

    def block(self, file_path, content_type=None):
        try:
            release.wait(5)
            return ParsedDocument(_TEXT)
        finally:
            finished.set()

    monkeypatch.setattr(DocumentParser, "extract_document_text", block)
    monkeypatch.setattr(dp, "EXTRACTION_TIMEOUT_SECONDS", 0.05)

    try:
        result = await DocumentPreprocessor().preprocess(
            _text_file(tmp_path), "text/plain"
        )
    finally:
        release.set()
        assert finished.wait(5), "the blocked worker outlived the test"

    assert result.is_rejected
    assert result.error_code == ConversionErrorCode.DOCUMENT_TOO_COMPLEX
    assert "took longer than the 0.05-second limit" in result.rejection_reason
