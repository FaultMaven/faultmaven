"""Regression guard: GET /sessions/{id}/cases must NOT re-paginate.

``CaseService.list_user_cases`` now applies limit/offset in the repository
query and returns ``(page, true_total)``. The session-cases endpoint therefore
must surface the service's page and total verbatim — it must NOT re-slice the
page by ``offset`` again (double pagination) nor report ``len(page)`` as the
total.

Before the pagination fix, the endpoint did
``total_count = len(user_cases); paginated_cases = user_cases[offset:offset+limit]``.
With the service now returning an already-paginated page, that re-slice would
drop the entire page whenever ``offset >= len(page)`` and under-report the
total. This test pins the corrected behavior so a reintroduction is caught.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi import Response

from faultmaven.modules.auth.api import session as session_module
from faultmaven.modules.auth.api.session import list_session_cases


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_session_cases_uses_service_page_and_total_without_reslicing(
    monkeypatch,
):
    # The service returns the already-paginated page (3 rows) for a query whose
    # true total is larger (10) — mirrors offset-based paging where this page
    # sits partway through the result set.
    page = [SimpleNamespace(case_id=f"case_{i}") for i in range(3)]
    total = 10

    case_service = SimpleNamespace(
        list_user_cases=_async_return((page, total)),
    )
    session_service = SimpleNamespace(
        get_session=_async_return(object()),  # session exists
    )

    # Isolate from CaseConverter/CaseAPI: echo entities as objects exposing the
    # .dict() the endpoint serializes. The point of the test is the pagination
    # arithmetic, not entity conversion.
    monkeypatch.setattr(
        session_module.CaseConverter,
        "entities_to_api_list",
        staticmethod(
            lambda entities: [
                SimpleNamespace(dict=lambda cid=e.case_id: {"case_id": cid})
                for e in entities
            ]
        ),
    )

    # offset (3) >= len(page) (3): the OLD re-slice page[3:...] would return []
    # and X-Total-Count would be "3". The corrected path returns all 3 rows and
    # the true total 10.
    response = await list_session_cases(
        session_id="sess_1",
        response=Response(),
        limit=50,
        offset=3,
        include_empty=False,
        include_terminal=False,
        include_deleted=False,
        session_service=session_service,
        case_service=case_service,
        current_user=SimpleNamespace(user_id="user_1"),
    )

    body = json.loads(response.body)
    assert len(body) == 3, "endpoint must not re-slice the service's page"
    assert {c["case_id"] for c in body} == {"case_0", "case_1", "case_2"}
    # X-Total-Count is the service's true total, not len(page).
    assert response.headers["X-Total-Count"] == "10"


def _async_return(value):
    async def _coro(*args, **kwargs):
        return value

    return _coro
