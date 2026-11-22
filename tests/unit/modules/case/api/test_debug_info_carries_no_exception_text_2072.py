"""#2072: ``include_debug=true`` returns failure codes, never exception text.

Any reader of a case may set ``include_debug`` on ``GET /cases/{id}/messages``,
and ``debug_info.storage_errors`` carried ``str(e)`` of whatever failed: a row
that would not convert, or the read itself. The text was built a service hop
from the route, where the route-scanning error-text guards do not look. Each
entry is now ``CODE: ExceptionClass`` and the text goes to the log. The model
itself refuses any other entry. A failed read is no longer an entry at all: it
answers an error status (#2333), whose body carries no text either.
"""

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.models.api import (
    MESSAGE_PARSE_FAILED,
    STORAGE_ERROR_CODES,
    MessageRetrievalDebugInfo,
    debug_error,
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from tests.case_builders import inquiry_case

pytestmark = pytest.mark.unit

SECRET = "db-password=hunter2 at 10.0.0.5"
CASE_ID = "case_aabb11223344"
PATH = f"/api/v1/cases/{CASE_ID}/messages"
ROW = {"message_id": "m-1", "turn_number": 1, "role": "user", "content": "hi"}
# A failure with text, and one with none, whose log line must still name it.
FAILURES = [ValueError(SECRET), asyncio.TimeoutError()]


def _case(**row):
    case = inquiry_case(case_id=CASE_ID, user_id="user-1", enterprise_id="ent_1")
    case.messages = [dict(ROW, created_at=datetime.now(timezone.utc), **row)]
    return case


def _service(repo) -> CaseService:
    return CaseService(
        case_repository=repo, session_store=AsyncMock(), max_cases_per_user=50
    )


def _logged(caplog, exc) -> logging.LogRecord:
    """The WARNING-or-worse record naming the exception's class and its text."""
    return next(
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and f"{type(exc).__name__}: {exc}" in r.getMessage()
    )


async def _get(build_app, call_api, service, **params):
    app = build_app(case_service=service)
    return await call_api(app, "GET", PATH, params=params)


@pytest.mark.parametrize("exc", FAILURES)
async def test_the_route_returns_a_code_and_logs_the_text(
    build_app, call_api, caplog, exc
):
    caplog.set_level(logging.WARNING)
    repo = AsyncMock(get=AsyncMock(return_value=_case()))
    with patch("faultmaven.models.api.Message", side_effect=exc):
        response = await _get(build_app, call_api, _service(repo), include_debug=1)

    debug = response.json()["debug_info"]
    assert debug["storage_errors"] == [f"MESSAGE_PARSE_FAILED: {type(exc).__name__}"]
    assert debug["message_parsing_errors"] == 1
    assert "hunter2" not in response.text
    record = _logged(caplog, exc)
    assert CASE_ID in record.getMessage()
    assert "m-1" in record.getMessage()


@pytest.mark.parametrize("exc", FAILURES)
async def test_a_failed_read_answers_500_and_logs_the_case(
    build_app, call_api, caplog, exc
):
    """The service's read failing is the route's 500, not an empty page with a
    ``STORAGE_READ_FAILED`` entry (#2333). A bare ``asyncio.TimeoutError`` is
    not a ``DatabaseTimeout``, so it is the generic 500, not the 503."""
    caplog.set_level(logging.WARNING)
    # The route's access check reads the case; the service's own read is next.
    repo = AsyncMock(get=AsyncMock(side_effect=[_case(), exc]))

    response = await _get(build_app, call_api, _service(repo), include_debug=1)

    assert response.status_code == 500
    assert "hunter2" not in response.text
    record = next(
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and CASE_ID in r.getMessage()
    )
    assert record.exc_info is not None


async def test_a_stored_row_the_loader_rejects_is_skipped_and_counted(
    build_app, call_api
):
    """Each row is loaded inside the per-row arm (#2333), so a stored row the
    loader rejects is skipped and counted as ``MESSAGE_PARSE_FAILED``."""
    repo = InMemoryCaseRepository()
    await repo.save(_case(token_count=SECRET))  # stored; ``CaseMessage`` refuses it

    response = await _get(build_app, call_api, _service(repo), include_debug=1)

    body = response.json()
    assert body["debug_info"]["storage_errors"] == [
        "MESSAGE_PARSE_FAILED: ValidationError"
    ]
    assert body["debug_info"]["message_parsing_errors"] == 1
    assert body["messages"] == []
    assert response.headers["X-Storage-Status"] == "error"
    assert "hunter2" not in response.text


async def test_without_include_debug_there_is_no_debug_info(build_app, call_api):
    repo = AsyncMock(get=AsyncMock(return_value=_case()))
    with patch("faultmaven.models.api.Message", side_effect=ValueError(SECRET)):
        response = await _get(build_app, call_api, _service(repo))

    assert response.status_code == 200
    assert response.json()["debug_info"] is None


def _debug_info(*entries: str) -> MessageRetrievalDebugInfo:
    return MessageRetrievalDebugInfo(
        redis_key="k", redis_operation_time_ms=0, storage_errors=list(entries)
    )


@pytest.mark.parametrize(
    "entry",
    [
        f"Storage error: {SECRET}",
        # A valid code and class, then text: the class half is pinned too.
        f"{debug_error(MESSAGE_PARSE_FAILED, ValueError('x'))}: {SECRET}",
    ],
)
def test_the_model_refuses_an_entry_that_is_not_coded(entry):
    with pytest.raises(ValueError):
        _debug_info(entry)


def test_debug_error_produces_only_entries_the_model_accepts():
    coded = [debug_error(code, TimeoutError()) for code in STORAGE_ERROR_CODES]
    assert _debug_info(*coded).storage_errors == coded

    # Not identifiers to the shared pattern: a dotted name, and a non-ASCII
    # one that ``str.isidentifier`` would accept and the model refuses.
    for odd_name in ("pkg.Odd", "Ошибка"):
        odd = type(odd_name, (Exception,), {})()
        entry = debug_error(MESSAGE_PARSE_FAILED, odd)
        assert _debug_info(entry).storage_errors == ["MESSAGE_PARSE_FAILED: Exception"]
    with pytest.raises(ValueError):
        debug_error("NOT_A_CODE", ValueError())
