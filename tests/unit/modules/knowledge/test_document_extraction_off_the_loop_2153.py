"""An uploaded document is prepared in a worker process its parent kills at the
deadline (#2153, #2622), and a PDF page pypdf refuses for its limits costs that
page, not the document (#2153).

``DocumentPreprocessor.preprocess`` ran Stages 0-5 (extraction, cleanup,
redaction, token counting) inline in a coroutine: a dense PDF measured 53.6 s
of event-loop stall, during which every other request in the process waited.
A thread pool then moved them off the loop, but a thread holds the interpreter
lock through a C call, and one whose caller gave up runs on (Principle 14). And
one page pypdf refused for its limits raised out of the page loop, so the whole
document was rejected as ``FILE_CORRUPT`` and every other page's text was lost.

PDFs here are a few hundred bytes, built by hand. A page is refused by lowering
pypdf's per-stream parse budget in the test's own context, never by a stream
dense enough to hit the default, which costs tens of seconds of CPU. A test
that needs the worker to do something a document cannot make it do names a
stage from ``preparation_stages`` in ``_PREPARATION_STAGE``. Every worker is a
real process: they are few, and their deadlines short.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import apply_configuration

from faultmaven.api.exception_handlers import internal_server_error_handler
from faultmaven.api.middleware.request_id import RequestIdMiddleware
from faultmaven.config.settings import get_settings
from faultmaven.modules.knowledge.domain.models.conversion import ConversionErrorCode
from faultmaven.modules.knowledge.domain.services import document_preprocessor as dp
from faultmaven.modules.knowledge.domain.services.document_preprocessor import (
    DocumentPreprocessor,
    PreparationFailed,
)
from faultmaven.utils.token_estimation import _get_tiktoken_encoder
from tests import offline_tiktoken
from tests.unit.modules.knowledge import preparation_stages
from tests.unit.modules.knowledge.test_document_preprocessor_existing_runbook import (
    _app_with_real_service,
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
    variable, so page 2 is refused only if they reach the worker process: the
    request passes them. The warning is the worker's own record, emitted here.
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
# In a worker process, killed at its deadline (Principle 14, #2622)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[4]

#: The two sentences the owner ruled for #2622, verbatim.
_OUT_OF_MEMORY = (
    "Reading this document needed more memory than the conversion allows, so it "
    "was not converted. Export its text to a TXT or Markdown file and convert "
    "that instead."
)
_UNREADABLE = (
    "This document could not be read, so it was not converted. Export its text "
    "to a TXT or Markdown file and convert that instead."
)


def _stage(monkeypatch, name: str) -> None:
    """The worker runs ``preparation_stages.<name>`` in place of ``prepare``."""
    monkeypatch.setattr(
        dp, "_PREPARATION_STAGE", f"{preparation_stages.__name__}:{name}"
    )


def _slots(monkeypatch, count: int) -> None:
    monkeypatch.setattr(dp, "PREPARATION_SLOTS", count)
    monkeypatch.setattr(dp, "_slots", None)


@pytest.fixture
def spawned(monkeypatch):
    """Every worker the test starts, to read how it ended."""
    processes: list[asyncio.subprocess.Process] = []
    spawn = asyncio.create_subprocess_exec

    async def record(*args, **kwargs):
        processes.append(await spawn(*args, **kwargs))
        return processes[-1]

    monkeypatch.setattr(dp.asyncio, "create_subprocess_exec", record)
    return processes


class _OnEntered(logging.Handler):
    """Calls ``then`` on the record a test stage logs once it has entered: in a
    worker it reaches this process through the worker's pipe."""

    def __init__(self, then) -> None:
        super().__init__()
        self._then = then

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage() == preparation_stages.ENTERED:
            self._then()


@pytest.fixture
def on_entered(caplog):
    """Install ``_OnEntered(then)`` for the test; WARNING reaches the worker."""
    caplog.set_level(logging.WARNING)
    logger, handlers = logging.getLogger(preparation_stages.__name__), []

    def install(then) -> None:
        handlers.append(_OnEntered(then))
        logger.addHandler(handlers[-1])

    yield install
    for handler in handlers:
        logger.removeHandler(handler)


async def _preprocess(path: Path, content_type: str = "text/plain"):
    return await DocumentPreprocessor().preprocess(
        path, content_type, conversion_id="conv_test"
    )


async def test_a_stage_holding_the_lock_in_c_leaves_the_event_loop_free(
    tmp_path, monkeypatch, on_entered
):
    """Principle 14's responsiveness check, ordered rather than timed.

    The stage holds the interpreter lock through one C call, a large
    ``re.sub``, and answers with the moment that call returned. Once the stage
    has entered, the loop must complete three turns (each a sleep: the loop
    blocks in its selector and wakes) before then. In the worker the call holds
    the worker's lock. On a thread of this process it holds this one's, and no
    turn can run until the call has returned: the same test, pointed at an
    in-thread run of the stage, fails (demonstrated on #2622, not run here).
    """
    _stage(monkeypatch, "hold_the_lock")
    loop, turned, tasks = asyncio.get_running_loop(), [], []

    async def three_turns() -> None:
        for _ in range(3):
            await asyncio.sleep(0.005)
        turned.append(time.monotonic())

    on_entered(
        lambda: loop.call_soon_threadsafe(
            lambda: tasks.append(loop.create_task(three_turns()))
        )
    )
    result = await _preprocess(_text_file(tmp_path))
    await asyncio.gather(*tasks)

    returned = float(result.rejection_reason)
    assert turned, "the stage's entry never reached the loop"
    assert turned[0] < returned, "the loop was held while the stage held its lock"


async def test_a_preparation_past_its_deadline_is_killed_and_its_slot_frees(
    tmp_path, monkeypatch, spawned, caplog
):
    _stage(monkeypatch, "never_return")
    _slots(monkeypatch, 1)
    monkeypatch.setattr(dp, "EXTRACTION_TIMEOUT_SECONDS", 1)

    with caplog.at_level(logging.WARNING):
        result = await _preprocess(_text_file(tmp_path))

    assert result.error_code == ConversionErrorCode.DOCUMENT_TOO_COMPLEX
    assert "took longer than the 1-second limit" in result.rejection_reason
    assert [m for m in _warnings(caplog) if "notes.txt" in m and "past 1 s" in m]
    assert spawned[0].returncode == -signal.SIGKILL
    assert not dp._preparation_slots().locked(), "the killed worker kept its slot"

    # The next job takes the slot, in a process of its own.
    _stage(monkeypatch, "answer_with_my_pid")
    monkeypatch.setattr(dp, "EXTRACTION_TIMEOUT_SECONDS", 120)
    result = await _preprocess(_text_file(tmp_path))
    assert int(result.rejection_reason) == spawned[1].pid != os.getpid()


async def test_a_cancelled_request_kills_its_worker(
    tmp_path, monkeypatch, spawned, on_entered
):
    _stage(monkeypatch, "never_return")
    _slots(monkeypatch, 1)
    loop, entered = asyncio.get_running_loop(), asyncio.Event()
    on_entered(lambda: loop.call_soon_threadsafe(entered.set))

    request = asyncio.create_task(_preprocess(_text_file(tmp_path)))
    await asyncio.wait_for(entered.wait(), 60)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request

    assert spawned[0].returncode == -signal.SIGKILL
    assert not dp._preparation_slots().locked()


async def test_a_request_with_no_free_slot_is_busy_and_starts_no_worker(
    tmp_path, monkeypatch, spawned, caplog
):
    """Time spent waiting is not the document's fault: the answer is the
    server's capacity, and nothing of the request runs."""
    _slots(monkeypatch, 0)
    monkeypatch.setattr(dp, "EXTRACTION_TIMEOUT_SECONDS", 0.05)

    with caplog.at_level(logging.WARNING):
        result = await _preprocess(_text_file(tmp_path, "queued.txt"))

    assert [m for m in _warnings(caplog) if "queued.txt: no extraction worker" in m]
    assert result.error_code == ConversionErrorCode.CONVERSION_BUSY
    assert "busy converting other documents" in result.rejection_reason
    assert spawned == []


def test_post_convert_answers_conversion_busy_with_retry_after(tmp_path, monkeypatch):
    _slots(monkeypatch, 0)
    monkeypatch.setattr(dp, "EXTRACTION_TIMEOUT_SECONDS", 0.05)
    pdf = _pdf(tmp_path, _stream(_TEXT)).read_bytes()

    with _app_with_real_service() as client:
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


@pytest.mark.parametrize(
    "stage, refusal",
    [
        pytest.param("run_out_of_memory", _OUT_OF_MEMORY, id="memory-code"),
        pytest.param("die_while_parsing", _UNREADABLE, id="dies-in-stage-1"),
        pytest.param("die_before_reading", None, id="dies-in-stage-0"),
        pytest.param("die_after_parsing", None, id="dies-in-stage-2"),
        pytest.param("no_such_stage", None, id="fails-to-start"),
    ],
)
async def test_a_dead_worker_is_refused_only_for_a_cause_in_the_document(
    tmp_path, monkeypatch, spawned, caplog, stage, refusal
):
    """The owner's ruling on #2622. Its memory code, or a death while parsing,
    is the document's: ``DOCUMENT_TOO_COMPLEX`` with the ruled sentence. Any
    other death may be the server's: a server error, the exit and the stage
    logged."""
    _stage(monkeypatch, stage)

    with caplog.at_level(logging.WARNING):
        if refusal is None:
            with pytest.raises(PreparationFailed, match="exited"):
                await _preprocess(_text_file(tmp_path))
        else:
            result = await _preprocess(_text_file(tmp_path))
            assert result.error_code == ConversionErrorCode.DOCUMENT_TOO_COMPLEX
            assert result.rejection_reason == refusal

    code = spawned[0].returncode
    assert code is not None
    assert [m for m in caplog.messages if f"exited {code} in stage" in m]


def test_a_worker_death_after_parsing_answers_the_global_500(tmp_path, monkeypatch):
    """Through the route: not the route's own "try again" 500, but the global
    handler's, with the request id and no advice."""
    _stage(monkeypatch, "die_after_parsing")
    app = _app_with_real_service().app
    app.add_exception_handler(500, internal_server_error_handler)
    app.add_middleware(RequestIdMiddleware)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/knowledge/convert",
            data={"scope": "personal"},
            files={"file": ("notes.txt", _TEXT.encode(), "text/plain")},
        )

    assert response.status_code == 500, response.text
    body = response.json()
    assert body["detail"] == "Internal server error"
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert "error_code" not in body


def test_post_convert_answers_a_death_while_parsing_with_its_sentence(
    tmp_path, monkeypatch
):
    """Through the route, with the real service and preprocessor. The status
    map answers 422 for a code it does not list, so the code is asserted too."""
    _stage(monkeypatch, "die_while_parsing")

    with _app_with_real_service() as client:
        response = client.post(
            "/api/v1/knowledge/convert",
            data={"scope": "personal"},
            files={"file": ("notes.txt", _TEXT.encode(), "text/plain")},
        )

    assert response.status_code == 422, response.text
    assert response.json() == {
        "detail": _UNREADABLE,
        "error_code": "DOCUMENT_TOO_COMPLEX",
    }


async def test_a_worker_past_its_output_limit_is_killed_and_a_server_error(
    tmp_path, monkeypatch, spawned
):
    """An accepted document's frames fit the limit by construction, so a worker
    past it is a fault, not the document's."""
    _stage(monkeypatch, "answer_too_much")
    monkeypatch.setattr(dp, "PREPARATION_OUTPUT_LIMIT_BYTES", 64 * 1024)

    with pytest.raises(PreparationFailed, match="output passed its limit"):
        await _preprocess(_text_file(tmp_path))

    assert spawned[0].returncode is not None


async def test_a_text_file_at_the_upload_limit_fits_under_the_memory_limit(tmp_path):
    """The memory refusal's advice, to export the text, is true because a
    plain-text file at ``MAX_UPLOAD_SIZE_MB`` always gets through Stages 0-5
    (the owner's ruling on #2622). This one is the costliest text measured
    when the limit was set: random bytes 0x80-0xFF, read as Latin-1, 14.5
    million tokens. It is refused for its length, not its memory."""
    size = get_settings().upload.max_upload_size_mb * 1024 * 1024
    head = b"ERROR timeout; kubectl rollout restart the api.\n"
    high = bytes(range(0x80, 0x100)) * 2
    body = random.Random(2622).randbytes(size - len(head)).translate(high)
    path = tmp_path / "export.txt"
    path.write_bytes(head + body)

    result = await _preprocess(path)

    assert (
        result.error_code == ConversionErrorCode.DOCUMENT_TOO_LONG
    ), result.rejection_reason


async def test_the_worker_counts_tokens_as_this_process_does(tmp_path):
    """The worker inherits the environment, so it loads the encoding from
    ``TIKTOKEN_CACHE_DIR`` (the knowledge conftest seeds it). Were the file
    missing, the worker would download it: refused here instead."""
    seeded = (
        Path(os.environ["TIKTOKEN_CACHE_DIR"])
        / hashlib.sha1(offline_tiktoken.BLOB_URL.encode()).hexdigest()
    )
    assert hashlib.sha256(seeded.read_bytes()).hexdigest() == (
        offline_tiktoken.BLOB_SHA256
    ), "the worker's tiktoken cache is missing: it would ask the network"
    path = tmp_path / "notes.txt"
    path.write_text("支付服务在部署后开始返回错误 " + _TEXT, encoding="utf-8")

    result = await _preprocess(path)

    assert not result.is_rejected, result.rejection_reason
    counted = len(_get_tiktoken_encoder("gpt-4").encode(result.extracted_text))
    assert result.token_count == counted != len(result.extracted_text) // 4


def test_the_worker_imports_the_parsers_and_nothing_heavy():
    """The worker's import surface, in a fresh interpreter: no store, model,
    tracing or settings library, and no thread beside the main one, so its
    address-space limit counts no thread pool's reservations."""
    probe = (
        "import sys, threading\n"
        "import faultmaven\n"
        "import faultmaven.modules.knowledge.domain.services.preparation_worker\n"
        "import faultmaven.modules.knowledge.domain.services.preparation\n"
        "heavy = ('chromadb', 'sqlalchemy', 'torch', 'opik', 'numpy', 'pydantic')\n"
        "print(faultmaven.__file__)\n"
        "print([name for name in heavy if name in sys.modules])\n"
        "print(threading.active_count())\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr
    where, heavy, threads = done.stdout.splitlines()
    assert Path(where).is_relative_to(_REPO_ROOT), where
    assert heavy == "[]"
    assert threads == "1"
