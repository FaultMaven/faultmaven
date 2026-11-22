"""#2333: ``GET /cases/{id}/messages`` answers 200 only with the page it read.

The service answered every failure of the read as an empty page, and converted
the page outside its per-row guard, so a database timeout, a case deleted after
the access check, or one bad stored row all read as an empty conversation
reported ``X-Storage-Status: success``. Now a row that fails is skipped and
counted, ``retrieved_count`` / ``has_more`` / ``next_offset`` /
``X-Storage-Status`` describe the page, and a failed read answers an error
status: 503 ``DATABASE_UNAVAILABLE`` for a database timeout, 404
``CASE_NOT_FOUND`` for a vanished case, 500 otherwise.

The route's access check reads the case first (``resolve_case``); the
service's own read is the second ``repository.get``.
"""

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from faultmaven.exceptions import DatabaseTimeout
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.repository import (
    RepositoryException,
)
from tests.case_builders import inquiry_case

pytestmark = pytest.mark.unit

SECRET = "db-password=hunter2 at 10.0.0.5"
CASE_ID = "case_aabb11223344"
PATH = f"/api/v1/cases/{CASE_ID}/messages"
UNAVAILABLE_DETAIL = (
    "The database did not answer in time, so the conversation could not be "
    "loaded. Please retry shortly."
)


def _row(i: int, *, bad: bool = False) -> dict:
    row = {
        "message_id": f"m-{i}",
        "turn_number": i,
        "role": "user",
        "content": f"text {i}",
        "created_at": datetime.now(timezone.utc),
    }
    if bad:
        del row["content"]  # the loader's ``row["content"]`` raises KeyError
    return row


def _case(*rows: dict):
    case = inquiry_case(case_id=CASE_ID, user_id="user-1", enterprise_id="ent_1")
    case.messages = list(rows)
    return case


def _service(*reads) -> CaseService:
    repo = AsyncMock(get=AsyncMock(side_effect=list(reads)))
    return CaseService(
        case_repository=repo, session_store=AsyncMock(), max_cases_per_user=50
    )


async def _get(build_app, call_api, service, **params):
    return await call_api(
        build_app(case_service=service), "GET", PATH, params=params or None
    )


def _five_rows(*bad: int):
    return _case(*(_row(i, bad=i in bad) for i in range(5)))


async def test_one_bad_row_is_skipped_and_the_page_counts_it(build_app, call_api):
    case = _five_rows(1)

    response = await _get(build_app, call_api, _service(case, case), limit=3)

    assert response.status_code == 200
    body = response.json()
    assert [m["message_id"] for m in body["messages"]] == ["m-0", "m-2"]
    assert body["retrieved_count"] == 2
    # The page consumed three rows, the skipped one included.
    assert body["has_more"] is True
    assert body["next_offset"] == 3
    assert response.headers["X-Storage-Status"] == "partial"

    debugged = await _get(
        build_app, call_api, _service(case, case), limit=3, include_debug=1
    )
    assert debugged.json()["debug_info"]["message_parsing_errors"] == 1
    assert debugged.headers["X-Storage-Status"] == "partial"


async def test_an_all_bad_page_with_rows_after_it_is_an_error(build_app, call_api):
    case = _five_rows(0, 1, 2)

    response = await _get(build_app, call_api, _service(case, case), limit=3)

    assert response.status_code == 200
    body = response.json()
    assert body["messages"] == []
    assert body["has_more"] is True
    assert body["next_offset"] == 3
    assert response.headers["X-Storage-Status"] == "error"


async def test_a_database_timeout_on_the_read_is_503(build_app, call_api, caplog):
    caplog.set_level(logging.WARNING)
    case = _five_rows()

    response = await _get(build_app, call_api, _service(case, DatabaseTimeout()))

    assert response.status_code == 503
    assert response.json()["detail"] == UNAVAILABLE_DETAIL
    assert response.headers["x-error-code"] == "DATABASE_UNAVAILABLE"
    assert response.headers["Retry-After"] == "5"
    assert any(
        CASE_ID in r.getMessage() and "DatabaseTimeout" in r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
    )


async def test_a_database_timeout_wrapped_by_a_repository_is_the_same_503(
    build_app, call_api
):
    """An inner session's timeout reaches the route wrapped: ``get_db_session``
    does not rewrap a chain that already holds a ``DatabaseTimeout``."""
    try:
        raise RepositoryException("load failed") from DatabaseTimeout()
    except RepositoryException as wrapped:
        failure = wrapped

    response = await _get(build_app, call_api, _service(_five_rows(), failure))

    assert response.status_code == 503
    assert response.json()["detail"] == UNAVAILABLE_DETAIL
    assert response.headers["x-error-code"] == "DATABASE_UNAVAILABLE"
    assert response.headers["Retry-After"] == "5"


async def test_a_database_timeout_on_the_access_check_is_the_same_503(
    build_app, call_api
):
    response = await _get(build_app, call_api, _service(DatabaseTimeout()))

    assert response.status_code == 503
    assert response.json()["detail"] == UNAVAILABLE_DETAIL
    assert response.headers["x-error-code"] == "DATABASE_UNAVAILABLE"
    assert response.headers["Retry-After"] == "5"


async def test_a_case_deleted_after_the_access_check_is_404(build_app, call_api):
    response = await _get(build_app, call_api, _service(_five_rows(), None))

    assert response.status_code == 404
    assert response.headers["x-error-code"] == "CASE_NOT_FOUND"


async def test_any_other_read_failure_is_500(build_app, call_api):
    service = _service(_five_rows(), RuntimeError(SECRET))

    response = await _get(build_app, call_api, service, include_debug=1)

    assert response.status_code == 500
    assert "hunter2" not in response.text


@pytest.mark.parametrize(
    "case, params",
    [
        pytest.param(_five_rows(), {"offset": 3, "limit": 3}, id="last-page"),
        pytest.param(_five_rows(), {"offset": 10}, id="offset-past-the-end"),
        pytest.param(_case(), {}, id="empty-case"),
    ],
)
async def test_a_page_with_nothing_after_it(build_app, call_api, case, params):
    """A pin of unchanged behaviour: it passes before #2333's change too."""
    response = await _get(build_app, call_api, _service(case, case), **params)

    assert response.status_code == 200
    body = response.json()
    assert body["has_more"] is False
    assert body["next_offset"] is None
    assert response.headers["X-Storage-Status"] == "success"
