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
                organization_id="org1",
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
