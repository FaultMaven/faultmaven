"""``GET /api/v1/cases`` must BIND its creation-date bounds, not just accept them.

The defect this closes is the quiet one. ``CaseListFilter`` has carried
``created_after``/``created_before`` since it was written, so every layer below
the route looked ready — but the route never declared them as query parameters,
and FastAPI drops an unknown query parameter without a word. A client sending
``?created_after=...`` therefore got 200 OK and the unfiltered list: no error to
notice, no log line, nothing to fail on. The dashboard shipped a date picker
against that for months before it was deleted as a lie
(faultmaven-dashboard#51).

So these tests assert at the seam where the silence lived: what the handler put
into the filter it built. A test that only checked the status code would have
passed throughout the entire life of the bug.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


def _capturing_case_service(captured: list):
    """A case service that records the filter the route hands it."""

    async def list_user_cases(user_id, filters=None):
        captured.append(filters)
        return [], 0

    return SimpleNamespace(list_user_cases=list_user_cases)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_creation_bounds_reach_the_filter(build_app, call_api):
    """Both bounds arrive on the filter, as the instants the client sent."""
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    response = await call_api(
        app,
        "GET",
        "/api/v1/cases",
        params={
            "created_after": "2026-09-10T00:00:00Z",
            "created_before": "2026-09-12T23:59:59.999000Z",
        },
    )

    assert response.status_code == 200
    assert len(captured) == 1
    filters = captured[0]
    assert filters.created_after == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert filters.created_before == datetime(
        2026, 9, 12, 23, 59, 59, 999000, tzinfo=timezone.utc
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_either_bound_alone_is_honoured(build_app, call_api):
    """One end of the range is a legitimate filter, not an incomplete one.

    "Everything since Monday" is the common case, and a route that required
    both would answer it by returning everything — the same silence in a
    different shape.
    """
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    await call_api(
        app, "GET", "/api/v1/cases", params={"created_after": "2026-09-10T00:00:00Z"}
    )
    assert captured[-1].created_after == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert captured[-1].created_before is None

    await call_api(
        app, "GET", "/api/v1/cases", params={"created_before": "2026-09-10T00:00:00Z"}
    )
    assert captured[-1].created_before == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert captured[-1].created_after is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_omitting_the_bounds_leaves_them_unset(build_app, call_api):
    """An unfiltered list stays unfiltered: no invented default window."""
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    response = await call_api(app, "GET", "/api/v1/cases")

    assert response.status_code == 200
    assert captured[0].created_after is None
    assert captured[0].created_before is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_naive_bound_is_read_as_utc(build_app, call_api):
    """A value with no offset is anchored to UTC before it reaches a driver.

    ``cases.created_at`` is ``timestamptz``; asyncpg refuses to compare a naive
    datetime against one, so an un-anchored bound would raise inside the query
    rather than filter. UTC is also the only reading that does not shift by
    whichever zone the server happens to run in.
    """
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    await call_api(
        app, "GET", "/api/v1/cases", params={"created_after": "2026-09-10T00:00:00"}
    )

    assert captured[0].created_after == datetime(2026, 9, 10, tzinfo=timezone.utc)
    assert captured[0].created_after.tzinfo is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unparseable_bound_is_refused_not_ignored(build_app, call_api):
    """Garbage in the bound is a 422 — the opposite of the original bug.

    Dropping it would put the caller back where they started: a full list that
    looks like a filtered one.
    """
    captured: list = []
    app = build_app(case_service=_capturing_case_service(captured))

    response = await call_api(
        app, "GET", "/api/v1/cases", params={"created_after": "last Tuesday"}
    )

    assert response.status_code == 422
    assert captured == []
