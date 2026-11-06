"""What the deleted in-memory case stand-in's tests pinned, on the real service (#2548).

``MinimalCaseService`` was the case service the container handed out when no
case repository existed. Nothing held it to ``ICaseService`` or to the routes,
so four drifts were pinned by hand in ``tests/unit/container/`` and six
route-called methods were still missing. No supported mode reached it —
``create_case_repository`` returns a repository or refuses startup (#2103) —
so it is deleted, and ``create_case_service`` refuses a missing repository.

Most of what those files pinned is pinned on ``CaseService`` already (the PR
lists each). These are the four that were not, over the production
``SQLiteCaseRepository`` on a file in ``tmp_path``.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis as fakeredis_aio
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import (
    set_current_billing_organization_id,
    set_current_enterprise_id,
)
from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure.concurrency.inflight import CaseDeleteGate
from faultmaven.models.api_models import CaseListFilter
from faultmaven.modules.case.domain.models.lifecycle import CaseState, ClosureReason
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    SQLiteCaseRepository,
)
from tests.case_builders import make_case

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("restore_tenant_context")]

OWNER = "user_owner_2548"
BILLING_ORG = "org_payer_2548"


@pytest.fixture
async def service(tmp_path, case_schema_template):
    """The real service over the real SQLite repository, nothing stubbed."""
    database = tmp_path / "case-service.db"
    shutil.copyfile(case_schema_template(STANDALONE_ENTERPRISE_ID, OWNER), database)
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "INSERT INTO organizations (organization_id, enterprise_id, name, slug) "
            "VALUES (?, ?, 'Payer', 'payer')",
            (BILLING_ORG, STANDALONE_ENTERPRISE_ID),
        )
        connection.commit()
    finally:
        connection.close()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    try:
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            yield CaseService(SQLiteCaseRepository(session))
    finally:
        await engine.dispose()


async def test_billing_comes_from_the_actors_organization_not_from_the_owner_id(
    service,
):
    """The two tenancy columns come from two bindings (ADR-017 D1/D2): the
    stand-in once wrote ``organization_id = owner_id``, a user id in the
    billing column. The real service's ``None`` answer is pinned in
    ``test_case_service.py``; this is the non-``None`` one, read back."""
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)
    set_current_billing_organization_id(BILLING_ORG)

    case = await service.create_case(title="Checkout latency", owner_id=OWNER)
    stored = await service.get_case(case.case_id, OWNER)

    assert stored.enterprise_id == STANDALONE_ENTERPRISE_ID
    assert stored.organization_id == BILLING_ORG
    assert stored.organization_id != OWNER


async def _one_of_each_state(service) -> None:
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=1)
    for case in (
        make_case(
            user_id=OWNER, enterprise_id=STANDALONE_ENTERPRISE_ID, title="inquiry"
        ),
        make_case(
            user_id=OWNER,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            title="resolved",
            description="checkout latency, fixed",
            state=CaseState.RESOLVED,
            created_at=earlier,
            resolved_at=now,
            closed_at=now,
        ),
        make_case(
            user_id=OWNER,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            title="closed",
            state=CaseState.CLOSED,
            created_at=earlier,
            closed_at=now,
            closure_reason=ClosureReason.INQUIRY_ONLY,
        ),
    ):
        await service.repository.save(case)


async def _titles(service, filters) -> set[str]:
    """The page, its total and the count, agreeing: ``count_user_cases`` is
    ``list_user_cases``'s pair, and the stand-in's once raised."""
    page, total = await service.list_user_cases(OWNER, filters)
    assert total == len(page)
    assert await service.count_user_cases(OWNER, filters) == total
    return {summary.title for summary in page}


@pytest.mark.parametrize("filters", [None, CaseListFilter()], ids=["none", "default"])
async def test_terminal_cases_are_listed_when_nothing_narrows(service, filters):
    """The stand-in dropped every RESOLVED and CLOSED case unconditionally.
    The real service excludes no terminal state; a declared ``state`` is the
    only thing that narrows on it (``test_declared_filters_reach_the_query``)."""
    await _one_of_each_state(service)

    assert await _titles(service, filters) == {"inquiry", "resolved", "closed"}
    assert await _titles(service, CaseListFilter(state=CaseState.CLOSED)) == {"closed"}


@pytest.mark.parametrize("falsy", ["", None])
async def test_count_user_cases_refuses_a_falsy_user_id(service, falsy):
    """The stand-in's count answered a falsy id with EVERY user's cases, while
    its list raised. ``list_user_cases`` is pinned in ``test_case_service.py``;
    this is the count half of the pair."""
    await _one_of_each_state(service)

    with pytest.raises(ValidationException):
        await service.count_user_cases(falsy, CaseListFilter())


async def test_delete_in_progress_is_false_with_no_delete_running(service):
    """``False`` with no gate, and ``False`` from a gate holding no tombstone
    (#2510). The route tests mock this; the service's own answer was pinned
    only on the stand-in."""
    case = await service.create_case(title="Checkout latency", owner_id=OWNER)

    assert service.delete_gate is None
    assert await service.delete_in_progress(case.case_id, case.enterprise_id) is False

    service.delete_gate = CaseDeleteGate(fakeredis_aio.FakeRedis())
    assert await service.delete_in_progress(case.case_id, case.enterprise_id) is False
