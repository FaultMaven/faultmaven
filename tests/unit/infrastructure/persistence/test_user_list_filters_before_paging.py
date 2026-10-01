"""The account list filters every account before it pages, however many there are.

``UserService.list_users`` serves ``GET /api/v1/admin/users`` under
single-tenancy. It used to fetch at most 1,000 accounts, filter that window by
role and search in Python and page the result, reporting the window's matches as
``total``: with more than 1,000 accounts in scope, later pages lost accounts and
``total`` was wrong. Role, search, active status and the enterprise now all
reach the store's query, ahead of LIMIT/OFFSET.

The tests run the real service over the stores that implement the listing: the
SQL repository on SQLite, the sessionless wrapper production composes (on the
same SQLite file), and the in-memory store. The population and the answers it
must give are ``tests/account_list_population.py``; its PostgreSQL run is
``tests/integration/test_user_list_paging_postgres.py``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    UserModel,
)
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
    PostgreSQLUserRepository,
    SessionlessUserRepository,
    User,
)
from faultmaven.modules.auth.domain.services.user_service import UserService
from tests import account_list_population as population

pytestmark = pytest.mark.unit

_A, _B = "ulp_a", "ulp_b"
_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
_PAGE = 100

#: More accounts than the old window in enterprise A, and a second enterprise.
_LARGE = population.build(token="", enterprise_a=_A, enterprise_b=_B, created_from=_AT)


# =============================================================================
# Stores
# =============================================================================


async def _write(path, users, *, enterprises) -> None:
    """Create the two tables and bulk-insert ``users`` in one statement, each
    row exactly as the production writer serialises it."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[EnterpriseModel.__table__, UserModel.__table__],
            )
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            for enterprise in enterprises:
                session.add(
                    EnterpriseModel(
                        enterprise_id=enterprise, name=enterprise, slug=enterprise
                    )
                )
            await session.flush()
            writer = PostgreSQLUserRepository(session)
            await session.execute(
                insert(UserModel), [writer._domain_to_dict(u) for u in users]
            )
            await session.commit()
    finally:
        await engine.dispose()


@asynccontextmanager
async def _stores(path, memory, monkeypatch):
    """The three stores over one population: SQL and sessionless on the SQLite
    file at ``path`` (with every statement captured), and ``memory``."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    statements: list[str] = []
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, *_: statements.append(statement),
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def session_per_operation(database_url=None):
        async with maker() as session:
            yield session

    monkeypatch.setattr(
        "faultmaven.infrastructure.persistence.database.get_db_session",
        session_per_operation,
    )
    try:
        async with maker() as session:
            yield {
                "sql": PostgreSQLUserRepository(session),
                "sessionless": SessionlessUserRepository(),
                "memory": memory,
                "statements": statements,
            }
    finally:
        await engine.dispose()


def _memory(users) -> InMemoryUserRepository:
    repository = InMemoryUserRepository()

    async def fill():
        for user in users:
            await repository.create(user)

    asyncio.run(fill())
    return repository


@pytest.fixture(scope="module")
def large(tmp_path_factory):
    """The population, written once for the module."""
    path = tmp_path_factory.mktemp("user_list") / "large.db"
    asyncio.run(_write(path, _LARGE.users, enterprises=(_A, _B)))
    return SimpleNamespace(path=path, memory=_memory(_LARGE.users))


_STORES = ("sql", "sessionless", "memory")


#: The sessionless wrapper runs the same statement as ``sql``; that it forwards
#: every filter is held by the small cases and the route test below.
@pytest.fixture(params=("sql", "memory"))
async def large_service(request, large, monkeypatch):
    async with _stores(large.path, large.memory, monkeypatch) as stores:
        yield UserService(user_repo=stores[request.param], auth_service=AsyncMock())


# =============================================================================
# More than the old window in scope
# =============================================================================


@pytest.mark.parametrize("filters", population.cases(_LARGE), ids=population.case_id)
async def test_each_page_and_the_total_are_the_filtered_lists(large_service, filters):
    """The first page, the one at the old window's edge, the last and the one
    past the end are each that slice of the whole filtered list, and every one
    reports how many accounts match."""
    expected = population.expected_ids(_LARGE.users, **filters)
    # Precondition: some match lies beyond the window the service used to read.
    assert population.beyond_old_window(_LARGE.users, filters)

    for offset in population.sample_offsets(len(expected), _PAGE):
        users, total = await large_service.list_users(
            limit=_PAGE, offset=offset, **filters
        )
        assert ([u.user_id for u in users], total) == (
            expected[offset : offset + _PAGE],
            len(expected),
        ), f"offset {offset}"


async def test_walking_every_page_serves_each_match_once(large_service):
    filters = {"enterprise_id": _A, "role": "member"}
    expected = population.expected_ids(_LARGE.users, **filters)
    assert population.beyond_old_window(_LARGE.users, filters)

    served = []
    for offset in range(0, len(expected), _PAGE):
        users, _ = await large_service.list_users(limit=_PAGE, offset=offset, **filters)
        served.extend(u.user_id for u in users)

    assert served == expected


async def test_the_route_serves_the_last_page_and_the_true_total(large, monkeypatch):
    """``GET /api/v1/admin/users`` under single-tenancy, end to end over the
    SQLite store: the last page of a combined filter past the old window."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from faultmaven.api.middleware.auth import require_platform_admin
    from faultmaven.api.routes.admin import get_user_service, router
    from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

    filters = {"enterprise_id": _A, "is_active": True, "role": "member"}
    expected = population.expected_ids(_LARGE.users, **filters)
    assert population.beyond_old_window(_LARGE.users, filters)
    last = (len(expected) - 1) // _PAGE * _PAGE

    async with _stores(large.path, large.memory, monkeypatch) as stores:
        service = UserService(user_repo=stores["sessionless"], auth_service=AsyncMock())
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_platform_admin] = lambda: AuthenticatedUser(
            user_id="op-1",
            enterprise_id=_A,
            email="operator@example.com",
            roles=["user", "platform_admin"],
            permissions=[],
        )
        app.dependency_overrides[get_user_service] = lambda: service
        app.state.user_store = SimpleNamespace(user_repository=stores["sessionless"])
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/api/v1/admin/users",
                params={
                    "enterprise_id": _A,
                    "is_active": "true",
                    "role": "member",
                    "limit": _PAGE,
                    "offset": last,
                },
            )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [row["user_id"] for row in body["users"]] == expected[last:]
    assert body["total"] == len(expected)
    # An account holding no role is reported, as it is matched, as a member.
    assert all("member" in row["roles"] for row in body["users"])


# =============================================================================
# What the listing already did, for a handful of accounts
# =============================================================================

_SMALL = [
    User(
        user_id=user_id,
        username=user_id,
        email=email,
        display_name=name,
        enterprise_id=_A,
        is_active=active,
        roles=roles,
        created_at=_AT + timedelta(minutes=minutes),
        updated_at=_AT,
    )
    for user_id, email, name, active, roles, minutes in (
        ("s_none", "none@example.com", "No Roles Here", True, [], 1),
        ("s_member", "member@example.com", "Plain Member", True, ["member"], 2),
        ("s_admin", "Boss@Example.com", "The Admin", True, ["admin"], 3),
        ("s_capital", "capital@example.com", "Capital Admin", True, ["Admin"], 4),
        ("s_viewer", "viewer@example.com", "Mixed CASE Viewer", False, ["viewer"], 5),
        ("s_percent", "uptime@example.com", "100% uptime bot", True, [], 6),
        # Created in one instant with ``s_tie_b``, and written after it.
        ("s_tie_c", "tie-c@example.com", "Tie", True, [], 7),
        ("s_tie_b", "tie-b@example.com", "Tie", True, [], 7),
    )
]


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    path = tmp_path_factory.mktemp("user_list") / "small.db"
    asyncio.run(_write(path, _SMALL, enterprises=(_A,)))
    return SimpleNamespace(path=path, memory=_memory(_SMALL))


@pytest.fixture(params=_STORES)
async def small_stores(request, small, monkeypatch):
    async with _stores(small.path, small.memory, monkeypatch) as stores:
        stores["service"] = UserService(
            user_repo=stores[request.param], auth_service=AsyncMock()
        )
        yield stores


def _small_id(filters: dict) -> str:
    return "-".join(f"{k}={v!r}" for k, v in filters.items()) or "unfiltered"


async def _ids(service, **filters):
    users, total = await service.list_users(limit=50, **filters)
    assert total == len(users)
    return [u.user_id for u in users]


_SMALL_CASES = [
    # Newest first; the tie in user_id order whatever the insertion order.
    (
        {},
        [
            "s_tie_b",
            "s_tie_c",
            "s_percent",
            "s_viewer",
            "s_capital",
            "s_admin",
            "s_member",
            "s_none",
        ],
    ),
    # An account holding no role is listed as a member.
    (
        {"role": "member"},
        ["s_tie_b", "s_tie_c", "s_percent", "s_member", "s_none"],
    ),
    # A role is matched exactly: "Admin" is not "admin".
    ({"role": "admin"}, ["s_admin"]),
    ({"role": "Admin"}, ["s_capital"]),
    ({"role": "viewer"}, ["s_viewer"]),
    ({"role": "nobody"}, []),
    ({"is_active": False}, ["s_viewer"]),
    # Case-insensitive, on the email…
    ({"search": "boss@EXAMPLE"}, ["s_admin"]),
    # …or on the display name.
    ({"search": "mixed case"}, ["s_viewer"]),
    ({"search": "ADMIN"}, ["s_capital", "s_admin"]),
    # '%' and '_' are literal, as they always were here.
    ({"search": "%"}, ["s_percent"]),
    ({"search": "_"}, []),
    ({"search": "nobody-matches"}, []),
    (
        {"role": "member", "search": "e"},
        ["s_tie_b", "s_tie_c", "s_percent", "s_member", "s_none"],
    ),
    ({"role": "member", "is_active": False}, []),
    ({"enterprise_id": "ulp_nobody"}, []),
]


@pytest.mark.parametrize(
    "filters, expected",
    [pytest.param(f, e, id=_small_id(f)) for f, e in _SMALL_CASES],
)
async def test_the_listing_rules_are_unchanged(small_stores, filters, expected):
    assert await _ids(small_stores["service"], **filters) == expected


async def test_paging_slices_the_filtered_list(small_stores):
    service = small_stores["service"]
    users, total = await service.list_users(role="member", limit=2, offset=1)
    assert ([u.user_id for u in users], total) == (["s_tie_c", "s_percent"], 5)


async def test_a_nul_search_is_an_empty_page_without_a_query(small_stores):
    """No stored text contains NUL — PostgreSQL's driver refuses to bind one —
    so nothing matches, and the database is not asked."""
    small_stores["statements"].clear()
    assert await small_stores["service"].list_users(search="a\x00") == ([], 0)
    assert small_stores["statements"] == []


async def test_an_offset_past_bigint_is_an_empty_page_with_the_true_total(
    small_stores,
):
    """Only the count reaches the database; the offset is never bound."""
    small_stores["statements"].clear()
    page = await small_stores["service"].list_users(role="member", offset=2**63)
    assert page == ([], 5)
    assert all("OFFSET" not in s.upper() for s in small_stores["statements"])
