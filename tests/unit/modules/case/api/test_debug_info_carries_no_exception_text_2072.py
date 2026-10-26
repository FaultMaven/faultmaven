"""#2072: ``include_debug=true`` returns failure codes, never exception text.

Any reader of a case may set ``include_debug`` on ``GET /cases/{id}/messages``,
and ``debug_info.storage_errors`` carried ``str(e)`` of whatever failed: a row
that would not convert, or the read itself. The text was built a service hop
from the route, where the route-scanning error-text guards do not look. Each
entry is now ``CODE: ExceptionClass`` and the text goes to the log, in both
services that answer the route: ``CaseService`` and the container's fallback.
"""

import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.modules.case.domain.services.case_service import CaseService
from tests.case_builders import inquiry_case

pytestmark = pytest.mark.unit

SECRET = "db-password=hunter2 at 10.0.0.5"
CASE_ID = "case_aabb11223344"
PATH = f"/api/v1/cases/{CASE_ID}/messages"
ROW = {"message_id": "m-1", "turn_number": 1, "role": "user", "content": "hi"}


def _service(second_read=None) -> CaseService:
    """The real service. The route's access check reads the case first; the
    service's own read is the second, and ``second_read`` replaces it."""
    case = inquiry_case(case_id=CASE_ID, user_id="user-1", enterprise_id="ent_1")
    case.messages = [dict(ROW, metadata={}, created_at=datetime.now(timezone.utc))]
    repo = AsyncMock()
    repo.get = AsyncMock(side_effect=[case, second_read or case])
    return CaseService(
        case_repository=repo, session_store=AsyncMock(), max_cases_per_user=50
    )


def _logged(caplog) -> bool:
    return any(
        SECRET in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )


@pytest.mark.parametrize(
    "second_read, entry, parse_errors",
    [
        (None, "MESSAGE_PARSE_FAILED: ValueError", 1),
        (RuntimeError(SECRET), "STORAGE_READ_FAILED: RuntimeError", 0),
    ],
)
async def test_the_route_returns_a_code_and_logs_the_text(
    build_app, call_api, caplog, second_read, entry, parse_errors
):
    caplog.set_level(logging.WARNING)
    with patch("faultmaven.models.api.Message", side_effect=ValueError(SECRET)):
        response = await call_api(
            build_app(case_service=_service(second_read)),
            "GET",
            PATH,
            params={"include_debug": "true"},
        )

    assert response.status_code == 200
    assert response.json()["debug_info"]["storage_errors"] == [entry]
    assert response.json()["debug_info"]["message_parsing_errors"] == parse_errors
    assert "hunter2" not in response.text
    assert _logged(caplog)


async def test_without_include_debug_there_is_no_debug_info(build_app, call_api):
    with patch("faultmaven.models.api.Message", side_effect=ValueError(SECRET)):
        response = await call_api(build_app(case_service=_service()), "GET", PATH)

    assert response.status_code == 200
    assert response.json()["debug_info"] is None


class _UnreadableStore(dict):
    def __contains__(self, key):
        return True

    def __getitem__(self, key):
        raise ConnectionError(SECRET)


@pytest.fixture
def fallback():
    from faultmaven.container import DIContainer

    return object.__new__(DIContainer)._create_minimal_case_service()


async def test_the_fallback_codes_a_row_that_will_not_convert(fallback, caplog):
    caplog.set_level(logging.WARNING)
    fallback.case_messages[CASE_ID] = [dict(ROW)]
    with patch("faultmaven.models.api.Message", side_effect=ValueError(SECRET)):
        response = await fallback.get_case_messages_enhanced(
            CASE_ID, include_debug=True
        )

    assert response.debug_info.storage_errors == ["MESSAGE_PARSE_FAILED: ValueError"]
    assert SECRET not in response.model_dump_json()
    assert _logged(caplog)
    assert any("m-1" in r.getMessage() for r in caplog.records)


async def test_the_fallback_codes_a_failed_read(fallback, caplog):
    caplog.set_level(logging.WARNING)
    fallback.case_messages = _UnreadableStore()
    response = await fallback.get_case_messages_enhanced(CASE_ID, include_debug=True)

    assert response.debug_info.storage_errors == [
        "STORAGE_READ_FAILED: ConnectionError"
    ]
    assert SECRET not in response.model_dump_json()
    assert _logged(caplog)
