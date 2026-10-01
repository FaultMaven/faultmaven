"""The account list filters before it pages, on a real PostgreSQL.

``UserService.list_users`` — ``GET /api/v1/admin/users`` under single-tenancy —
applies role, search, active status and the enterprise in the query, ahead of
LIMIT/OFFSET, so every page and ``total`` are slices and counts of the whole
filtered set however many accounts are in scope. What only PostgreSQL can
answer: the role filter reads the ``dev_roles`` JSON through ``jsonb`` (SQLite
uses ``json_each``), search lowering and ``LIKE`` escaping are the server's,
and the two inputs its driver cannot bind — a NUL in the search and an offset
past ``bigint`` — must not reach it.

The population (``tests/account_list_population.py``) is more accounts than the
old 1,000-row window in one enterprise, under ids and a search needle unique to
this run, so the shared database's other rows never match; beside it, accounts
whose ``dev_roles`` the writer never produces. It is read through the
sessionless repository production composes.

Run locally::

    docker run -d -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16
    export DATABASE_URL=postgresql+asyncpg://postgres:pw@localhost:5432/postgres
    export SKIP_SERVICE_CHECKS=true
    alembic upgrade head
    pytest tests/integration/test_user_list_paging_postgres.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from tests import account_list_population as population

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

_PAGE = 100

#: Created "in 2097", so the population leads the newest-first order ahead of
#: anything another module left in the shared database.
_CREATED_FROM = datetime(2097, 1, 1, tzinfo=timezone.utc)


async def _as_owner(sql: str, **params) -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


_TOKEN = uuid.uuid4().hex[:8]
#: Names the population (and the cases) at collection; ``world`` builds it.
_SHAPE = population.shape(
    token=_TOKEN, enterprise_a=f"ulp_a_{_TOKEN}", enterprise_b=f"ulp_b_{_TOKEN}"
)
_M = f"ulp_m_{_TOKEN}"
# Outside the population's id prefix, so its cross-enterprise searches miss them.
_M_PREFIX = f"ulpm{_TOKEN}_"


async def _write(users, raw_accounts) -> None:
    """The enterprises, then every account in one bulk INSERT, each row as the
    production writer serialises it, then the raw ``dev_roles`` texts."""
    from faultmaven.infrastructure.persistence.models import UserModel
    from faultmaven.infrastructure.persistence.user_repository import (
        PostgreSQLUserRepository,
    )

    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    try:
        async with AsyncSession(bind=engine) as session:
            for enterprise in (_SHAPE.enterprise_a, _SHAPE.enterprise_b, _M):
                await session.execute(
                    text(
                        "INSERT INTO enterprises (enterprise_id, name, slug) "
                        "VALUES (:e, :e, :e)"
                    ),
                    {"e": enterprise},
                )
            writer = PostgreSQLUserRepository(session)
            await session.execute(
                insert(UserModel),
                [writer._domain_to_dict(u) for u in users + raw_accounts],
            )
            for suffix, raw in population.RAW_ROLES.items():
                await session.execute(
                    text("UPDATE users SET dev_roles = :raw WHERE user_id = :u"),
                    {"raw": raw, "u": f"{_M_PREFIX}{suffix}"},
                )
            await session.commit()
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def world():
    users = population.build(_SHAPE, created_from=_CREATED_FROM)
    raw_accounts = population.raw_roles_accounts(_M_PREFIX, _M, _CREATED_FROM)
    try:
        asyncio.run(_write(users, raw_accounts))
        yield SimpleNamespace(
            users=users,
            enterprise_a=_SHAPE.enterprise_a,
            needle=_SHAPE.needle,
        )
    finally:
        # The accounts go with their enterprises (ON DELETE CASCADE).
        for enterprise in (_SHAPE.enterprise_a, _SHAPE.enterprise_b, _M):
            asyncio.run(
                _as_owner(
                    "DELETE FROM enterprises WHERE enterprise_id = :e", e=enterprise
                )
            )


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop():
    """The sessionless repository's engine is bound to the loop that made it;
    each test runs on its own."""
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    reset_engine()
    yield
    await close_database()


@pytest.fixture
def service():
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )
    from faultmaven.modules.auth.domain.services.user_service import UserService

    return UserService(user_repo=SessionlessUserRepository(), auth_service=AsyncMock())


@pytest.mark.parametrize("filters", population.cases(_SHAPE), ids=population.case_id)
async def test_each_page_and_the_total_are_the_filtered_lists(world, service, filters):
    """The first page, the one at the old window's edge, the last and the one
    past the end are each that slice of the whole filtered list, and every one
    reports how many accounts match."""
    expected = population.expected_ids(world.users, **filters)
    # Precondition: some match lies beyond the window the service used to read.
    assert population.beyond_old_window(world.users, filters)

    for offset in population.sample_offsets(len(expected), _PAGE):
        users, total = await service.list_users(limit=_PAGE, offset=offset, **filters)
        assert ([u.user_id for u in users], total) == (
            expected[offset : offset + _PAGE],
            len(expected),
        ), f"offset {offset}"


async def test_walking_every_page_serves_each_match_once(world, service):
    filters = {"enterprise_id": world.enterprise_a, "role": "member"}
    expected = population.expected_ids(world.users, **filters)
    assert population.beyond_old_window(world.users, filters)

    served = []
    for offset in range(0, len(expected), _PAGE):
        users, _ = await service.list_users(limit=_PAGE, offset=offset, **filters)
        served.extend(u.user_id for u in users)

    assert served == expected


async def test_the_route_serves_the_last_page_and_the_true_total(world, service):
    """``GET /api/v1/admin/users`` end to end over PostgreSQL, single-tenant."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from faultmaven.api.middleware.auth import require_platform_admin
    from faultmaven.api.routes.admin import get_user_service, router
    from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

    filters = {"enterprise_id": world.enterprise_a, "is_active": True, "role": "member"}
    expected = population.expected_ids(world.users, **filters)
    assert population.beyond_old_window(world.users, filters)
    last = (len(expected) - 1) // _PAGE * _PAGE

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_platform_admin] = lambda: AuthenticatedUser(
        user_id="op-probe",
        enterprise_id=world.enterprise_a,
        email="operator@example.com",
        roles=["user", "platform_admin"],
        permissions=[],
    )
    app.dependency_overrides[get_user_service] = lambda: service
    app.state.user_store = SimpleNamespace(user_repository=service.user_repo)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/admin/users",
            params={**filters, "is_active": "true", "limit": _PAGE, "offset": last},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["user_id"] for row in body["users"]] == expected[last:]
    assert body["total"] == len(expected)


@pytest.mark.parametrize("offset", [2**63 - 1, 2**63, 10**30])
async def test_an_offset_past_bigint_is_an_empty_page_with_the_true_total(
    world, service, offset
):
    filters = {"enterprise_id": world.enterprise_a, "role": "member"}
    expected = population.expected_ids(world.users, **filters)
    assert await service.list_users(offset=offset, **filters) == ([], len(expected))


@pytest.mark.parametrize(
    "role, expected",
    list(population.RAW_ROLES_LISTED.items()),
    ids=["member", "admin", "emoji"],
)
async def test_a_value_that_is_not_an_array_of_strings_holds_no_roles(
    world, service, role, expected
):
    """Read as no roles — so as a member — and never handed to ``jsonb``, so no
    row can make the filter raise."""
    users, total = await service.list_users(enterprise_id=_M, role=role, limit=50)
    assert [u.user_id for u in users] == [f"{_M_PREFIX}{s}" for s in expected]
    assert total == len(expected)


async def test_every_such_account_is_listed_with_the_roles_the_filter_read(
    world, service
):
    users, total = await service.list_users(enterprise_id=_M, limit=50)
    roles = {u.user_id[len(_M_PREFIX) :]: u.roles for u in users}
    assert total == len(population.RAW_ROLES)
    assert roles.pop("admin") == ["admin"]
    assert roles.pop("pair") == ["\U0001f600", "admin"]
    assert roles == {s: [] for s in population.RAW_ROLES_LISTED["member"]}


@pytest.mark.parametrize(
    "filters",
    [
        {"role": "mem\x00ber"},
        {"enterprise_id": f"{_M}\x00"},
        {"search": "a\x00"},
        {"search": "%" * 60_000},
    ],
    ids=["nul-role", "nul-enterprise", "nul-search", "huge-search"],
)
async def test_an_input_the_database_cannot_take_is_an_empty_page(
    world, service, filters
):
    assert await service.list_users(**filters) == ([], 0)


async def test_counting_accounts(world):
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )

    repository = SessionlessUserRepository()
    assert await repository.count_users(_SHAPE.enterprise_a) == 2100
    assert await repository.count_users(_M) == len(population.RAW_ROLES)
