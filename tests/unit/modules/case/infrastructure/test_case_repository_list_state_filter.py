"""Regression guard for the Status→State rename in the case-repository hierarchy.

The #405 rename updated the SQLite repo's ``list()`` filter parameter to
``state`` and the service caller to pass ``state=``, but missed the abstract
base, the in-memory impl, and — critically — ``SessionlessCaseRepository``,
the wrapper actually wired in at runtime. Because that wrapper's signature
still declared ``status``, the service's keyword call ``repo.list(state=...)``
raised ``TypeError`` and the route swallowed it into an empty result — the
list endpoint returned no cases while single-case GET worked.

Nothing exercised the sessionless wrapper or the contract↔impl signature
agreement (the API test mocks the service; repo tests hit SQLite directly).
These tests close that gap.
"""

import inspect

import pytest

from faultmaven.modules.case.contracts import ICaseRepository
from faultmaven.modules.case.domain.models import Case, CaseState
from faultmaven.modules.case.infrastructure.case_repository import (
    CaseRepository,
    InMemoryCaseRepository,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository import (
    PostgreSQLHybridCaseRepository,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

# Every type that exposes a case ``list()`` — the contract and all concrete
# implementations must agree on the lifecycle filter being named ``state``.
_LIST_PROVIDERS = [
    ICaseRepository,
    CaseRepository,
    InMemoryCaseRepository,
    SessionlessCaseRepository,
    SQLiteCaseRepository,
    PostgreSQLHybridCaseRepository,
]


@pytest.mark.unit
@pytest.mark.parametrize("provider", _LIST_PROVIDERS, ids=lambda c: c.__name__)
def test_list_filter_param_is_state_not_status(provider):
    """Case ``list()`` must take the lifecycle filter as ``state`` (not ``status``).

    A divergence here is exactly what broke the live list endpoint: the
    service calls ``repo.list(state=...)`` by keyword, so any impl still
    declaring ``status`` raises ``unexpected keyword argument 'state'``.
    """
    params = inspect.signature(provider.list).parameters
    assert "state" in params, f"{provider.__name__}.list() missing 'state' param"
    assert "status" not in params, (
        f"{provider.__name__}.list() still declares 'status' — the service "
        f"calls list(state=...) by keyword and this would raise at runtime"
    )


@pytest.mark.unit
@pytest.mark.parametrize("provider", _LIST_PROVIDERS, ids=lambda c: c.__name__)
def test_list_declares_include_empty_param(provider):
    """Case ``list()`` must accept ``include_empty`` across contract + all impls.

    ``include_empty`` is pushed into the repository query so the empty-case
    predicate constrains BOTH the returned page and the total count. The
    service calls ``repo.list(include_empty=...)`` by keyword, so any impl
    missing the param would raise ``unexpected keyword argument`` at runtime.
    """
    params = inspect.signature(provider.list).parameters
    assert "include_empty" in params, (
        f"{provider.__name__}.list() missing 'include_empty' param — the "
        f"service calls list(include_empty=...) by keyword"
    )
    # Default must preserve the "show everything" behavior.
    assert params["include_empty"].default is True


def _make_case(title, user_id="u1", current_turn=1):
    case = Case(title=title, user_id=user_id, enterprise_id="org1", description="")
    # Bypass Pydantic cross-field validators to set current_turn directly.
    object.__setattr__(case, "current_turn", current_turn)
    return case


@pytest.mark.unit
async def test_inmemory_pagination_returns_distinct_pages_and_true_total():
    """limit/offset yield distinct, non-overlapping pages; total is the true
    match count, not the page length."""
    repo = InMemoryCaseRepository()
    for i in range(6):
        await repo.save(_make_case(title=f"Case {i}"))

    page1, total1 = await repo.list(user_id="u1", limit=2, offset=0)
    page2, total2 = await repo.list(user_id="u1", limit=2, offset=2)

    # Total is the full count (6), independent of the page size (2).
    assert total1 == 6
    assert total2 == 6
    assert len(page1) == 2
    assert len(page2) == 2
    # Pages do not overlap.
    ids1 = {c.case_id for c in page1}
    ids2 = {c.case_id for c in page2}
    assert ids1.isdisjoint(ids2)


@pytest.mark.unit
async def test_inmemory_include_empty_false_is_sound_across_pages():
    """include_empty=False constrains both the page and the total: empties are
    excluded from the count AND never appear on any page (page/total agree)."""
    repo = InMemoryCaseRepository()
    # 3 active (current_turn>0) + 2 empty (current_turn==0) = 5 rows total.
    for i in range(3):
        await repo.save(_make_case(title=f"Active {i}", current_turn=i + 1))
    for i in range(2):
        await repo.save(_make_case(title=f"Empty {i}", current_turn=0))

    # With empties included, total is 5.
    _, total_all = await repo.list(user_id="u1", include_empty=True)
    assert total_all == 5

    # With empties excluded, total drops to 3 and NO empty row surfaces on any
    # page — walk the whole result set page by page and assert consistency.
    _, total_active = await repo.list(user_id="u1", include_empty=False)
    assert total_active == 3

    seen = []
    offset = 0
    while offset < total_active:
        page, total = await repo.list(
            user_id="u1", include_empty=False, limit=2, offset=offset
        )
        assert total == total_active  # total is stable across pages
        seen.extend(page)
        offset += 2
    # Every surfaced case is non-empty, and the pages summed to exactly total.
    assert len(seen) == total_active
    assert all(c.current_turn > 0 for c in seen)


@pytest.mark.unit
async def test_inmemory_list_filters_by_state_keyword():
    """Functional: list(state=...) reads the renamed field, matching and excluding.

    Uses INQUIRY cases (the valid default) to avoid the model's terminal-state
    validators — the point is to exercise the keyword ``state=`` call (the path
    that broke at runtime) and the ``c.state == state`` comparison, not to build
    a particular lifecycle value.
    """
    repo = InMemoryCaseRepository()
    for i in range(2):
        await repo.save(
            Case(
                title=f"Case {i}",
                user_id="u1",
                enterprise_id="org1",
                description="",
            )
        )

    # Keyword call mirrors the service caller; matching state returns the rows...
    matched, total = await repo.list(user_id="u1", state=CaseState.INQUIRY)
    assert total == 2
    assert {c.title for c in matched} == {"Case 0", "Case 1"}

    # ...and a non-matching state filters them out (proves it compares .state).
    excluded, total_excluded = await repo.list(user_id="u1", state=CaseState.RESOLVED)
    assert total_excluded == 0
    assert excluded == []


@pytest.mark.unit
@pytest.mark.parametrize("provider", _LIST_PROVIDERS, ids=lambda c: c.__name__)
def test_list_declares_creation_date_bounds(provider):
    """Case ``list()`` must accept ``created_after``/``created_before`` everywhere.

    Same failure mode as ``state`` and ``include_empty`` above: the service
    calls ``repo.list(created_after=..., created_before=...)`` by keyword, so an
    impl missing either raises ``unexpected keyword argument`` at runtime — and
    the list route swallows that into an empty result rather than a 500, which
    is how the ``state`` rename went unnoticed.
    """
    params = inspect.signature(provider.list).parameters
    for name in ("created_after", "created_before"):
        assert name in params, (
            f"{provider.__name__}.list() missing '{name}' param — the service "
            f"calls list({name}=...) by keyword"
        )
        # Default must preserve the unbounded behavior.
        assert params[name].default is None


@pytest.mark.unit
async def test_inmemory_creation_bounds_are_inclusive_and_constrain_the_total():
    """The bounds are INCLUSIVE at both ends, and they move the total with the page.

    Inclusivity is the half a client cannot discover by experiment: a date
    picker sends the last instant of the chosen day, so an exclusive upper bound
    would silently drop every case created in that final microsecond — and more
    importantly would make "1 Jan to 1 Jan" mean nothing rather than that day.
    """
    from datetime import datetime, timezone

    repo = InMemoryCaseRepository()
    days = [datetime(2026, 9, d, 12, 0, tzinfo=timezone.utc) for d in (10, 11, 12, 13)]
    for i, created in enumerate(days):
        case = _make_case(title=f"Day {10 + i}")
        object.__setattr__(case, "created_at", created)
        await repo.save(case)

    # Unbounded: everything.
    _, total_all = await repo.list(user_id="u1")
    assert total_all == 4

    # Lower bound INCLUDES a case created exactly on it.
    rows, total = await repo.list(user_id="u1", created_after=days[1])
    assert total == 3
    assert {c.title for c in rows} == {"Day 11", "Day 12", "Day 13"}

    # Upper bound INCLUDES a case created exactly on it.
    rows, total = await repo.list(user_id="u1", created_before=days[1])
    assert total == 2
    assert {c.title for c in rows} == {"Day 10", "Day 11"}

    # Both ends together, and a single-instant window resolves to one case —
    # the "one day" case a date picker produces.
    rows, total = await repo.list(
        user_id="u1", created_after=days[1], created_before=days[2]
    )
    assert total == 2
    assert {c.title for c in rows} == {"Day 11", "Day 12"}

    rows, total = await repo.list(
        user_id="u1", created_after=days[0], created_before=days[0]
    )
    assert total == 1
    assert {c.title for c in rows} == {"Day 10"}

    # The total is the BOUNDED count, not the unfiltered one: a page taken from
    # inside the window must be described by a total drawn from the same window,
    # or the caller pages past the end.
    page, total = await repo.list(
        user_id="u1", created_after=days[1], limit=1, offset=0
    )
    assert total == 3
    assert len(page) == 1
