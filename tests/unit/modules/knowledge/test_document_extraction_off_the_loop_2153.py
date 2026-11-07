"""An uploaded document is prepared off the event loop, under time bounds, and
a PDF page pypdf refuses for its limits costs that page, not the document (#2153).

``DocumentPreprocessor.preprocess`` ran Stages 0-5 (extraction, cleanup,
redaction, token counting) inline in a coroutine: a dense PDF measured 53.6 s
of event-loop stall, during which every other request in the process waited.
And one page pypdf refused for its limits raised out of the page loop, so the
whole document was rejected as ``FILE_CORRUPT`` and every other page's text was
lost.

PDFs here are a few hundred bytes, built by hand. A page is refused by lowering
pypdf's per-stream parse budget in the test's own context, never by a stream
dense enough to hit the default, which costs tens of seconds of CPU. Tests that
hold the two extraction workers release them, and wait for them, before they
end.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
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

_CATALOG = b"<< /Type /Catalog /Pages 2 0 R >>"
_FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def _stream(words: str) -> bytes:
    return b"BT /F1 12 Tf 72 720 Td (" + words.encode() + b") Tj ET"


def _write(tmp_path: Path, objects: list[bytes], xref_count: int | None = None) -> Path:
    """A PDF of ``objects``, numbered from 1, with its cross-reference table.
    ``xref_count`` overstates the table's entry count: corrupt."""
    out, offsets = b"%PDF-1.4\n", []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (xref_count or len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path = tmp_path / "source.pdf"
    path.write_bytes(out)
    return path


def _pdf(
    tmp_path: Path,
    *streams: bytes,
    self_length: tuple[int, ...] = (),
    huge_length: tuple[int, ...] = (),
    xref_count: int | None = None,
) -> Path:
    """One page per content stream. A page numbered in ``self_length`` declares
    its stream's ``/Length`` as a reference to that same stream, and one in
    ``huge_length`` declares 999,999,999 bytes, past pypdf's 75 MB stream limit,
    in a file of under 2 KB: both corrupt."""
    kids = b" ".join(b"%d 0 R" % (4 + 2 * i) for i in range(len(streams)))
    objects = [
        _CATALOG,
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(streams)),
        _FONT,
    ]
    for i, stream in enumerate(streams):
        number = 5 + 2 * i
        length = b"%d" % len(stream)
        if i + 1 in self_length:
            length = b"%d 0 R" % number
        if i + 1 in huge_length:
            length = b"999999999"
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents %d 0 R"
            b" /Resources << /Font << /F1 3 0 R >> >> >>" % number
        )
        objects.append(b"<< /Length %s >>\nstream\n" % length + stream + b"\nendstream")
    return _write(tmp_path, objects, xref_count)


def _parent_cycle(tmp_path: Path) -> Path:
    """A page with no /Resources whose /Pages node names the page as its own
    /Parent: looking the inherited /Resources up walks a cycle."""
    stream = _stream(_TEXT)
    return _write(
        tmp_path,
        [
            _CATALOG,
            b"<< /Type /Pages /Kids [4 0 R] /Count 1 /Parent 4 0 R >>",
            _FONT,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        ],
    )


def _text_file(tmp_path: Path, name: str = "notes.txt") -> Path:
    """A file that passes the integrity check, for a stubbed extraction."""
    path = tmp_path / name
    path.write_text("replaced by the stub", encoding="utf-8")
    return path


@contextmanager
def _both_workers_held():
    """Both extraction workers busy until the block ends. On the way out the
    queue is drained through a two-worker barrier, so any job still queued
    behind the holds has run, and finished, before the caller asserts."""
    release, running = threading.Event(), threading.Semaphore(0)

    def hold():
        running.release()
        release.wait(5)

    holds = [dp._EXTRACTION_POOL.submit(hold) for _ in range(2)]
    try:
        for _ in holds:
            assert running.acquire(timeout=5), "a worker never took its hold"
        yield
    finally:
        release.set()
        for job in holds:
            job.result(timeout=5)
        barrier = threading.Barrier(2, timeout=5)
        for job in [dp._EXTRACTION_POOL.submit(barrier.wait) for _ in range(2)]:
            job.result(timeout=5)


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# A budget refusal costs a page, or is DOCUMENT_TOO_COMPLEX, never FILE_CORRUPT
# ---------------------------------------------------------------------------


async def test_a_refused_page_costs_that_page_and_the_limit_reaches_the_worker(
    tmp_path, caplog
):
    """Page 2 is refused, page 1's text is kept, and the warning names page 2.

    The budget is lowered in THIS coroutine. pypdf's limits are a context
    variable, so page 2 is refused only if the worker runs in a copy of the
    caller's context: an executor does not copy it by itself.
    """
    path = _pdf(tmp_path, _stream(_TEXT), _DENSE)

    with caplog.at_level(logging.WARNING), apply_configuration(**_LOW_BUDGET):
        result = await DocumentPreprocessor().preprocess(
            path, "application/pdf", conversion_id="conv_test"
        )

    assert not result.is_rejected, (result.error_code, result.rejection_reason)
    assert "kubectl rollout restart" in result.extracted_text
    assert result.source_metadata["page_count"] == 2
    assert [w for w in result.warnings if "page(s) 2 of 2" in w], result.warnings
    logged = _warnings(caplog)
    assert [m for m in logged if "source.pdf" in m and "[2]" in m and "tokens" in m]
    assert not [m for m in logged if "kubectl" in m], "the document's text was logged"


@pytest.mark.parametrize(
    "streams, limit",
    [
        pytest.param((_DENSE, _DENSE), _LOW_BUDGET, id="every-page-refused"),
        # Raised from ``len(reader.pages)``, before any page is read.
        pytest.param(
            (_stream(_TEXT),), {"page_tree_maximum_depth": 0}, id="document-level"
        ),
        # A page of nothing but a space has no text either.
        pytest.param(
            (_stream(" "), _DENSE), _LOW_BUDGET, id="whitespace-page-beside-it"
        ),
    ],
)
async def test_a_limit_refusal_is_document_too_complex(
    tmp_path, streams, limit, caplog
):
    path = _pdf(tmp_path, *streams)

    with caplog.at_level(logging.WARNING), apply_configuration(**limit):
        result = await DocumentPreprocessor().preprocess(
            path, "application/pdf", conversion_id="conv_test"
        )

    assert result.is_rejected
    assert result.error_code == ConversionErrorCode.DOCUMENT_TOO_COMPLEX
    assert "too complex for the PDF reader's limits" in result.rejection_reason
    assert [m for m in _warnings(caplog) if "source.pdf: pypdf refused" in m]


@pytest.mark.parametrize(
    "words",
    [
        pytest.param("Runbook: Postgres connection pool", id="too-short"),
        pytest.param("Quarterly planning notes for the team. " * 8, id="no-signals"),
    ],
)
async def test_refused_pages_that_leave_too_little_are_document_too_complex(
    tmp_path, words
):
    """What the refused page left fails the length gate (a 32-character title)
    or the technical-content gate (prose with no signal), but the refused page
    is why: the answer says so, not that the source is short or off-topic."""
    path = _pdf(tmp_path, _stream(words), _DENSE)

    with apply_configuration(**_LOW_BUDGET):
        result = await DocumentPreprocessor().preprocess(
            path, "application/pdf", conversion_id="conv_test"
        )

    assert result.error_code == ConversionErrorCode.DOCUMENT_TOO_COMPLEX
    assert "refused page(s) 2 of 2" in result.rejection_reason


async def test_the_skipped_page_list_names_ten_and_counts_the_rest(tmp_path):
    path = _pdf(tmp_path, _stream(_TEXT), *[_DENSE] * 12)

    with apply_configuration(**_LOW_BUDGET):
        result = await DocumentPreprocessor().preprocess(
            path, "application/pdf", conversion_id="conv_test"
        )

    assert not result.is_rejected, result.rejection_reason
    assert [
        w
        for w in result.warnings
        if "2, 3, 4, 5, 6, 7, 8, 9, 10, 11 and 2 more of 13" in w
    ], result.warnings


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda d: _pdf(d, _stream(_TEXT), _stream(_TEXT), self_length=(2,)),
            id="self-referencing-length-on-page-2",
        ),
        pytest.param(
            lambda d: _pdf(d, _stream(_TEXT), self_length=(1,)),
            id="self-referencing-length-one-page",
        ),
        pytest.param(_parent_cycle, id="parent-cycle"),
        # "Declared size ... exceeds allowed limit": 999,999 entries in ~1 KB.
        pytest.param(
            lambda d: _pdf(d, _stream(_TEXT), xref_count=999_999),
            id="xref-declares-more-entries-than-the-file-holds",
        ),
        # "Declared stream length ... exceeds maximum allowed length".
        pytest.param(
            lambda d: _pdf(d, _stream(_TEXT), _stream(_TEXT), huge_length=(2,)),
            id="declared-stream-length-past-the-limit-on-page-2",
        ),
        pytest.param(
            lambda d: _pdf(d, _stream(_TEXT), huge_length=(1,)),
            id="declared-stream-length-past-the-limit-one-page",
        ),
    ],
)
async def test_corruption_pypdf_reports_as_a_limit_stays_file_corrupt(
    tmp_path, build, caplog
):
    """pypdf raises ``LimitReachedError`` for these too, and they are broken
    files: FILE_CORRUPT for the whole document, as before #2153."""
    with caplog.at_level(logging.WARNING):
        result = await DocumentPreprocessor().preprocess(
            build(tmp_path), "application/pdf", conversion_id="conv_test"
        )

    assert result.error_code == ConversionErrorCode.FILE_CORRUPT
    assert not result.warnings
    assert [m for m in _warnings(caplog) if "read as corruption" in m]


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

    result = await DocumentPreprocessor().preprocess(
        path, content_type, conversion_id="conv_test"
    )

    assert result.is_rejected
    assert result.error_code == code, result.rejection_reason


# ---------------------------------------------------------------------------
# Off the loop, in the pool, under the bounds
# ---------------------------------------------------------------------------


async def test_preparation_runs_in_the_extraction_pool(tmp_path, monkeypatch):
    seen: list[str] = []

    def record(self, *args, **kwargs):
        seen.append(threading.current_thread().name)
        return ParsedDocument(_TEXT)

    monkeypatch.setattr(DocumentParser, "extract_document_text", record)

    await DocumentPreprocessor().preprocess(
        _text_file(tmp_path), "text/plain", conversion_id="conv_test"
    )

    assert len(seen) == 1 and seen[0].startswith("document-extract"), seen


async def test_preparation_does_not_stall_the_event_loop(tmp_path, monkeypatch):
    """A tick count, not a duration, for the reason ``_ticks_during`` gives.

    The inline run is the positive control: Stages 0-5 called from a coroutine,
    as before the fix, give the loop no tick at all. Without it a payload too
    small to stall anything would pass this while proving nothing.
    """

    def burn(self, *args, **kwargs):
        sum(i * i for i in range(2_000_000))
        return ParsedDocument(_TEXT)

    monkeypatch.setattr(DocumentParser, "extract_document_text", burn)
    preprocessor, path = DocumentPreprocessor(), _text_file(tmp_path)

    async def inline():
        return preprocessor._prepare(path, "text/plain")

    async def through_preprocess():
        return await preprocessor.preprocess(
            path, "text/plain", conversion_id="conv_test"
        )

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


async def test_a_preparation_past_its_time_limit_is_refused(
    tmp_path, monkeypatch, caplog
):
    """The request is answered at the limit, while the worker runs on. The
    worker is released and waited for, so none outlives the test."""
    release, finished = threading.Event(), threading.Event()

    def block(self, *args, **kwargs):
        try:
            release.wait(5)
            return ParsedDocument(_TEXT)
        finally:
            finished.set()

    monkeypatch.setattr(DocumentParser, "extract_document_text", block)
    monkeypatch.setattr(dp, "EXTRACTION_TIMEOUT_SECONDS", 0.05)

    try:
        with caplog.at_level(logging.WARNING):
            result = await DocumentPreprocessor().preprocess(
                _text_file(tmp_path), "text/plain", conversion_id="conv_test"
            )
    finally:
        release.set()
        assert finished.wait(5), "the blocked worker outlived the test"

    assert result.is_rejected
    assert result.error_code == ConversionErrorCode.DOCUMENT_TOO_COMPLEX
    assert "took longer than the 0.05-second limit" in result.rejection_reason
    assert [m for m in _warnings(caplog) if "notes.txt" in m and "0.05 s" in m]


async def test_an_abandoned_worker_stops_after_its_current_page(
    tmp_path, monkeypatch, caplog
):
    """Once its caller has timed out, the worker finishes the page it is on and
    reads no other: a thread cannot be killed, so this is what bounds it."""
    gate, finished, read = threading.Event(), threading.Event(), []
    read_page, extract = PageObject.extract_text, DocumentParser.extract_document_text

    def slow_page(self, *args, **kwargs):
        gate.wait(5)
        read.append(1)
        return read_page(self, *args, **kwargs)

    def extract_then_signal(self, *args, **kwargs):
        try:
            return extract(self, *args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(PageObject, "extract_text", slow_page)
    monkeypatch.setattr(DocumentParser, "extract_document_text", extract_then_signal)
    monkeypatch.setattr(dp, "EXTRACTION_TIMEOUT_SECONDS", 0.05)
    path = _pdf(tmp_path, *[_stream(_TEXT)] * 5)

    try:
        with caplog.at_level(logging.WARNING):
            result = await DocumentPreprocessor().preprocess(
                path, "application/pdf", conversion_id="conv_test"
            )
            gate.set()
            assert finished.wait(5), "the worker never finished"
    finally:
        gate.set()

    assert result.error_code == ConversionErrorCode.DOCUMENT_TOO_COMPLEX
    assert len(read) == 1, f"the worker read {len(read)} pages after its caller left"
    assert [m for m in _warnings(caplog) if "stopped before page 2 of 5" in m]


async def test_a_request_with_no_free_worker_is_busy_and_its_work_never_runs(
    tmp_path, monkeypatch, caplog
):
    """Time spent queued is not the document's fault: the answer is the
    server's capacity, and the queued work is cancelled, not left to run."""
    ran: list[str] = []

    def record(self, file_path, *args, **kwargs):
        ran.append(file_path.name)
        return ParsedDocument(_TEXT)

    monkeypatch.setattr(DocumentParser, "extract_document_text", record)
    monkeypatch.setattr(dp, "EXTRACTION_TIMEOUT_SECONDS", 0.05)

    with caplog.at_level(logging.WARNING), _both_workers_held():
        result = await DocumentPreprocessor().preprocess(
            _text_file(tmp_path, "queued.txt"), "text/plain", conversion_id="conv_test"
        )

    assert [m for m in _warnings(caplog) if "queued.txt: no extraction worker" in m]
    assert result.error_code == ConversionErrorCode.CONVERSION_BUSY
    assert "busy converting other documents" in result.rejection_reason
    assert "complex" not in result.rejection_reason
    assert ran == [], f"the queued work ran after its caller left: {ran}"


def test_post_convert_answers_conversion_busy_with_retry_after(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, "EXTRACTION_TIMEOUT_SECONDS", 0.05)
    pdf = _pdf(tmp_path, _stream(_TEXT)).read_bytes()

    with _both_workers_held(), _app_with_real_service() as client:
        response = client.post(
            "/api/v1/knowledge/convert",
            data={"scope": "personal"},
            files={"file": ("guide.pdf", pdf, "application/pdf")},
        )

    assert response.status_code == 503, response.text
    assert response.json()["error_code"] == "CONVERSION_BUSY", response.text
    assert response.headers["Retry-After"] == str(
        dp.CONVERSION_BUSY_RETRY_AFTER_SECONDS
    )


async def test_a_timeout_error_raised_in_the_worker_propagates(tmp_path, monkeypatch):
    """Only the bounds' own expiry is the time limit. A ``TimeoutError`` from
    inside the work (an ``ETIMEDOUT`` from a read) is an error, not a refusal."""

    def fail(self, *args, **kwargs):
        raise TimeoutError("ETIMEDOUT reading the upload")

    monkeypatch.setattr(DocumentParser, "extract_document_text", fail)

    with pytest.raises(TimeoutError, match="ETIMEDOUT"):
        await DocumentPreprocessor().preprocess(
            _text_file(tmp_path), "text/plain", conversion_id="conv_test"
        )
