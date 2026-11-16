"""#2587 — the case delete path logs exceptions by class, never their text.

A store client's error can carry its backend's output: an HTTP client's
response headers and body, a storage key, a connection string. The delete
route's catch-all and its 503 branch, the uploads eraser and the release of the
delete's tombstone claim each logged that text. Each now names classes only,
and every answer is unchanged.

Driven through the MOUNTED case router (``DELETE /api/v1/cases/{id}``). The
eraser and claim tests run the REAL ``CaseService.hard_delete_case``, the real
uploads eraser (``FileStorageService``) and the real ``CaseDeleteGate``; only
the backends under them raise.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.infrastructure.concurrency.inflight import (
    DELETE_UNAVAILABLE_RETRY_AFTER_SECONDS,
    CaseDeleteGate,
    CaseDeleteUnavailable,
    CaseWorkInProgress,
)
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)
from tests.unit.modules.case.test_turn_receipt_1888 import (
    CASE_ID,
    OWNER,
    _investigating_case,
    _user,
)

pytestmark = pytest.mark.unit

CASE = f"/api/v1/cases/{CASE_ID}"

#: What a client's error carries: its backend's response headers and body.
LEAKED = (
    'status 502, headers {"set-cookie": "session=SECRET"}, '
    'body {"bucket": "fm-prod-uploads", "detail": "upstream"}'
)
FORBIDDEN = ("SECRET", "set-cookie", "fm-prod-uploads")


class ApiError(Exception):
    """A store client's error, its text the backend's response."""


def _leaky() -> ApiError:
    """An ``ApiError`` carrying the response, raised from a cause that does."""
    try:
        try:
            raise ConnectionError(LEAKED)
        except ConnectionError as cause:
            raise ApiError(LEAKED) from cause
    except ApiError as error:
        return error


def _raise_leaky(*_args, **_kwargs):
    raise _leaky()


_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def _rendered(record: logging.LogRecord) -> str:
    """All a handler can write of ``record``: its message, its traceback
    (``exc_info``, ``exc_text``, ``stack_info``) and its extra fields."""
    parts = [logging.Formatter().format(record), str(record.exc_text or "")]
    parts += [repr(v) for k, v in vars(record).items() if k not in _STANDARD]
    return "\n".join(parts)


def _assert_no_text_logged(caplog) -> str:
    """No record holds the client's text; all records, rendered, for the
    positive controls."""
    rendered = "\n".join(_rendered(record) for record in caplog.records)
    for secret in FORBIDDEN:
        assert secret not in rendered, rendered
    return rendered


def _client(case_service) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_authentication] = lambda: _user(OWNER)
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )


async def _delete(case_service) -> httpx.Response:
    async with _client(case_service) as client:
        return await client.delete(CASE)


def _raising(error: BaseException) -> MagicMock:
    service = MagicMock()
    service.hard_delete_case = AsyncMock(side_effect=error)
    return service


def _real_service(*, store_erasers=(), delete_gate=None):
    repository = MagicMock()
    repository.get = AsyncMock(return_value=_investigating_case())
    repository.delete = AsyncMock(return_value=True)
    service = CaseService(
        repository, store_erasers=list(store_erasers), delete_gate=delete_gate
    )
    return service, repository


@pytest.fixture
def caplog_all(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


async def test_the_catch_all_logs_the_class_chain_not_the_text(caplog_all):
    answer = await _delete(_raising(_leaky()))

    assert answer.status_code == 500
    error = answer.json()["detail"]["error"]
    assert (error["code"], error["message"]) == (
        "DELETE_CASE_ERROR",
        "Failed to delete case",
    )
    rendered = _assert_no_text_logged(caplog_all)
    assert (
        f"Unexpected error in delete_case {CASE_ID}: ApiError <- ConnectionError"
        in rendered
    )


async def test_the_503_logs_the_causes_class_and_the_release_its_own(caplog_all):
    """The lease read fails after the tombstone is written: the delete's own
    release of its tombstone then fails too. Neither line holds the text."""
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    redis.eval = AsyncMock(side_effect=_raise_leaky)
    service, repository = _real_service(delete_gate=CaseDeleteGate(redis))

    answer = await _delete(service)

    assert answer.status_code == 503
    assert answer.headers["x-error-code"] == "CASE_DELETE_UNAVAILABLE"
    assert answer.headers["Retry-After"] == str(DELETE_UNAVAILABLE_RETRY_AFTER_SECONDS)
    assert answer.json()["detail"]["error"]["code"] == "CASE_DELETE_UNAVAILABLE"
    rendered = _assert_no_text_logged(caplog_all)
    assert f"Refused delete of case {CASE_ID}: ApiError <- ConnectionError" in rendered
    assert "Could not release claim" in rendered
    assert ": ApiError" in rendered.split("Could not release claim", 1)[1]
    repository.delete.assert_not_awaited()


async def test_a_503_without_a_cause_says_so(caplog_all):
    service, _ = _real_service(delete_gate=CaseDeleteGate(None))

    answer = await _delete(service)

    assert answer.status_code == 503
    assert answer.headers["Retry-After"] == str(DELETE_UNAVAILABLE_RETRY_AFTER_SECONDS)
    assert f"Refused delete of case {CASE_ID}: no cause" in caplog_all.text


async def test_an_uploads_eraser_failure_logs_classes_and_keeps_the_case(
    caplog_all,
):
    backend = MagicMock()
    backend.list_keys = AsyncMock(side_effect=_raise_leaky)
    service, repository = _real_service(
        store_erasers=[FileStorageService(backend=backend)],
        delete_gate=CaseDeleteGate(fakeredis_aio.FakeRedis(decode_responses=True)),
    )

    answer = await _delete(service)

    assert answer.status_code == 500
    assert answer.json()["detail"]["error"]["code"] == "DELETE_CASE_ERROR"
    rendered = _assert_no_text_logged(caplog_all)
    assert f"Could not clear uploads for case {CASE_ID}" in rendered
    assert f"erase_case failed for case {CASE_ID}: ApiError" in rendered
    assert (
        f"Unexpected error in delete_case {CASE_ID}: ServiceError <- ApiError"
        in rendered
    )
    repository.delete.assert_not_awaited()


async def test_a_refused_delete_keeps_the_holders_retry_after():
    answer = await _delete(_raising(CaseWorkInProgress(7)))

    assert answer.status_code == 409
    assert answer.headers["x-error-code"] == "CASE_WORK_IN_PROGRESS"
    assert answer.headers["Retry-After"] == "7"


async def test_an_unavailable_store_answer_is_unchanged():
    answer = await _delete(_raising(CaseDeleteUnavailable()))

    assert answer.status_code == 503
    error = answer.json()["detail"]["error"]
    assert (error["code"], error["message"]) == (
        "CASE_DELETE_UNAVAILABLE",
        "The case could not be deleted just now; try again.",
    )
