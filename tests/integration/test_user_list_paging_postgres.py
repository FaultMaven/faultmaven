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
this run, so the shared database's other rows never match. It is read through
the sessionless repository production composes.

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


async def _write(world) -> None:
    """The enterprises, then every account in one bulk INSERT, each row as the
    production writer serialises it."""
    from faultmaven.infrastructure.persistence.models import UserModel
    from faultmaven.infrastructure.persistence.user_repository import (
        PostgreSQLUserRepository,
    )

    engine = create_async_engine(os.environ["DATABASE_URL"], future=True)
    try:
        async with AsyncSession(bind=engine) as session:
            for enterprise in (world.enterprise_a, world.enterprise_b):
                await session.execute(
                    text(
                        "INSERT INTO enterprises (enterprise_id, name, slug) "
                        "VALUES (:e, :e, :e)"
                    ),
                    {"e": enterprise},
                )
            writer = PostgreSQLUserRepository(session)
            await session.execute(
                insert(UserModel), [writer._domain_to_dict(u) for u in world.users]
            )
            await session.commit()
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def world():
    token = uuid.uuid4().hex[:8]
    built = population.build(
        token=token,
        enterprise_a=f"ulp_a_{token}",
        enterprise_b=f"ulp_b_{token}",
        created_from=_CREATED_FROM,
    )
    try:
        asyncio.run(_write(built))
        yield built
    finally:
        # The accounts go with their enterprises (ON DELETE CASCADE).
        for enterprise in (built.enterprise_a, built.enterprise_b):
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


#: The cases name this run's token, which exists only once ``world`` is built,
#: so they are parametrized by position, with ids from a token-free stand-in.
_CASE_IDS = [
    population.case_id(filters)
    for filters in population.cases(
        SimpleNamespace(enterprise_a="A", prefix="ulp_", needle="needle")
    )
]


@pytest.mark.parametrize("case", range(len(_CASE_IDS)), ids=_CASE_IDS)
async def test_each_page_and_the_total_are_the_filtered_lists(world, service, case):
    """The first page, the one at the old window's edge, the last and the one
    past the end are each that slice of the whole filtered list, and every one
    reports how many accounts match."""
    filters = population.cases(world)[case]
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


async def test_a_nul_search_is_an_empty_page_not_a_driver_error(world, service):
    """PostgreSQL stores no NUL and its driver refuses to bind one."""
    assert await service.list_users(
        enterprise_id=world.enterprise_a, search=f"{world.needle}\x00"
    ) == ([], 0)


@pytest.mark.parametrize("offset", [2**63 - 1, 2**63, 10**30])
async def test_an_offset_past_bigint_is_an_empty_page_with_the_true_total(
    world, service, offset
):
    filters = {"enterprise_id": world.enterprise_a, "role": "member"}
    expected = population.expected_ids(world.users, **filters)
    assert await service.list_users(offset=offset, **filters) == ([], len(expected))
