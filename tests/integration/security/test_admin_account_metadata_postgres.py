"""The operator account list on a real PostgreSQL.

Under ``TENANT_PROVIDER=multi`` ``GET /api/v1/admin/users`` reads every
enterprise's accounts with an ordinary query: ``users`` is outside row-level
security. What only a real database can answer:

* **The read is bounded by what it selects.** The statement the database
  receives names exactly the eleven ``AccountMetadata`` columns, and an account
  whose password hash, SSO subject, avatar, locale and role list are sentinels
  never surfaces one.
* **Parity.** For the operator's own enterprise the route serves, field for
  field and in the same order, what the listing confined to that enterprise
  serves — under every filter, across a page boundary among accounts created in
  one instant. Another enterprise's rows carry ``roles == []`` and
  ``manageable == False``.
* **The roles read is confined.** Asked about ids of two enterprises, it
  answers only the operator's.
* **Page and total agree**, and the two inputs the database cannot bind — an
  offset past ``bigint`` and a search containing NUL — answer an empty page
  with the true total instead of an error.

Everything runs as a role with the deployed ``faultmaven_app`` grants and no
ownership, bound to enterprise A; the accounts are written through the
production user writer.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from tests.integration.security.conftest import (
    create_limited_role,
    drop_limited_role,
    limited_url,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.security,
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
        reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
    ),
]

_ROLE = f"fm_acctmeta_probe_{uuid.uuid4().hex[:8]}"
_PW = "fm_acctmeta_probe_pw"

#: Every fixture account is created "in 2098", so the fixtures lead the
#: newest-first order ahead of any account another module left in the shared
#: database, in an order this module decides.
_EPOCH = datetime(2098, 1, 1, tzinfo=timezone.utc)

#: Written into every fixture account's columns that the list must never carry.
_SENTINEL = "ACCT-SENTINEL"

#: The columns the list reads — exactly ``AccountMetadata``'s fields.
_ACCOUNT_COLUMNS = {
    "user_id",
    "enterprise_id",
    "email",
    "display_name",
    "account_kind",
    "service_channel",
    "is_active",
    "is_email_verified",
    "last_login_at",
    "created_at",
    "updated_at",
}

#: Columns of ``users`` the list must never select.
_NEVER_SELECTED = {
    "hashed_password",
    "sso_provider",
    "sso_provider_id",
    "dev_roles",
    "avatar_url",
    "timezone",
    "locale",
    "deleted_at",
    "username",
    "email_verified_at",
    "last_password_change_at",
}


# =============================================================================
# Environment
# =============================================================================


@pytest.fixture(scope="module")
def limited_role_env():
    """Point the persistence layer at a limited role for this module only —
    restored wholesale in teardown (the ``-m postgres`` lane runs sibling
    modules that expect the superuser url)."""
    superuser_url = os.environ["DATABASE_URL"]
    saved = {
        key: os.environ.get(key)
        for key in ("DATABASE_URL", "DEPLOYMENT_MODE", "TENANT_PROVIDER")
    }
    asyncio.run(create_limited_role(superuser_url, _ROLE, _PW))
    os.environ["DATABASE_URL"] = limited_url(superuser_url, _ROLE, _PW)
    os.environ["DEPLOYMENT_MODE"] = "cloud"
    os.environ["TENANT_PROVIDER"] = "multi"

    from faultmaven.infrastructure.persistence.database import reset_engine
    from tests.utils import reset_settings_singleton

    reset_settings_singleton()
    reset_engine()
    yield superuser_url
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings_singleton()
    reset_engine()
    asyncio.run(drop_limited_role(superuser_url, _ROLE))


@pytest.fixture(autouse=True)
async def fresh_engine_per_loop(limited_role_env):
    from faultmaven.infrastructure.persistence.database import (
        close_database,
        reset_engine,
    )

    reset_engine()
    yield
    await close_database()
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


async def _as_owner(superuser_url: str, sql: str, **params):
    engine = create_async_engine(superuser_url, future=True)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql), params)
            return result.fetchall() if result.returns_rows else []
    finally:
        await engine.dispose()


# =============================================================================
# Fixtures — written through the production user writer
# =============================================================================


def _user(user_id: str, enterprise_id: str, created_at: datetime, **fields):
    from faultmaven.infrastructure.persistence.user_repository import User

    values = dict(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        enterprise_id=enterprise_id,
        # Never part of the list: each must stay out of every row it serves.
        hashed_password=f"{_SENTINEL}-hash-{user_id}",
        sso_provider="workos",
        sso_provider_id=f"{_SENTINEL}-subject-{user_id}",
        avatar_url=f"https://example.com/{_SENTINEL}.png",
        locale="xx-XX",
        roles=["admin"],
        created_at=created_at,
        updated_at=created_at,
    )
    values.update(fields)
    return User(**values)


@pytest.fixture
async def world(limited_role_env):
    """Two enterprises. A holds an account of every shape the parity test needs
    — and six created in one instant, for the page boundary; B holds two, so a
    read spanning enterprises has something to span."""
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )

    owner = limited_role_env
    suffix = uuid.uuid4().hex[:8]
    ent_a, ent_b = f"ent_acct_a_{suffix}", f"ent_acct_b_{suffix}"
    repository = SessionlessUserRepository()
    minute = timedelta(minutes=1)

    a_shapes = {
        "person": dict(roles=["admin"]),
        "no_roles": dict(roles=[]),
        "inactive": dict(is_active=False, deleted_at=_EPOCH),
        "unverified": dict(is_email_verified=False),
        "verified": dict(is_email_verified=True),
        "logged_in": dict(last_login_at=_EPOCH - timedelta(days=3)),
        "slack": dict(account_kind="service", service_channel="slack", roles=[]),
        "mixed_case": dict(display_name=f"MiXeD Case Person {suffix}"),
        "percent": dict(display_name=f"100% uptime bot {suffix}"),
    }

    # Cleanup is armed before the first insert: a setup that fails half way
    # must not leave enterprises behind for the rest of the lane to count.
    try:
        for enterprise in (ent_a, ent_b):
            await _as_owner(
                owner,
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES (:e, :e, :e)",
                e=enterprise,
            )
        a = {}
        for minutes, (label, fields) in enumerate(a_shapes.items()):
            a[label] = await repository.create(
                _user(
                    f"acct_a_{label}_{suffix}",
                    ent_a,
                    _EPOCH + minutes * minute,
                    **fields,
                )
            )
        b = {}
        for minutes, label in enumerate(("person", "slack")):
            b[label] = await repository.create(
                _user(
                    f"acct_b_{label}_{suffix}",
                    ent_b,
                    _EPOCH + minutes * minute,
                    **(
                        dict(account_kind="service", service_channel="slack")
                        if label == "slack"
                        else {}
                    ),
                )
            )
        # Six created in one instant, newer than everything else, written in
        # DESCENDING id order so insertion order cannot pass for the tiebreak.
        tied_ids = sorted(f"acct_a_tied_{n}_{suffix}" for n in range(6))
        tied_at = _EPOCH + timedelta(days=1)
        for user_id in reversed(tied_ids):
            await repository.create(_user(user_id, ent_a, tied_at))

        a_order = tied_ids + [
            user.user_id
            for user in sorted(a.values(), key=lambda u: u.created_at, reverse=True)
        ]
        yield SimpleNamespace(
            owner=owner,
            ent_a=ent_a,
            ent_b=ent_b,
            a=a,
            b=b,
            suffix=suffix,
            tied=tied_ids,
            # Newest first; the tied six lead, ascending by user_id.
            a_order=a_order,
        )
    finally:
        for enterprise in (ent_a, ent_b):
            await _as_owner(
                owner, "DELETE FROM enterprises WHERE enterprise_id = :e", e=enterprise
            )


async def _read(**filters):
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )

    return await SessionlessUserRepository().list_account_metadata(
        is_active=filters.get("is_active"),
        search=filters.get("search"),
        enterprise_id=filters.get("enterprise_id"),
        limit=filters.get("limit", 100_000),
        offset=filters.get("offset", 0),
    )


def _route_app(operator_enterprise: str):
    """The real route over the real account store, as the composition root
    wires it — with the operator bound to ``operator_enterprise``."""
    from fastapi import FastAPI

    from faultmaven.api.middleware.auth import require_platform_admin
    from faultmaven.api.routes.admin import get_user_service, router
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )
    from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
    from faultmaven.modules.auth.domain.services.user_service import UserService

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_platform_admin] = lambda: AuthenticatedUser(
        user_id="op-probe",
        enterprise_id=operator_enterprise,
        email="operator@example.com",
        roles=["user", "platform_admin"],
        permissions=[],
    )
    repository = SessionlessUserRepository()
    service = UserService(user_repo=repository, auth_service=AsyncMock())
    app.dependency_overrides[get_user_service] = lambda: service
    app.state.user_store = SimpleNamespace(user_repository=repository)
    app.state.operator_audit_repository = AsyncMock()
    return app, service


async def _get(app, path: str, **params):
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path, params=params)


def _confined_rows(users, enterprise_id: str):
    """What the listing confined to ``enterprise_id`` serves, projected as the
    route projects a manageable account."""
    from faultmaven.api.routes.admin import _manageable_item

    return [
        _manageable_item(user, enterprise_id=enterprise_id).model_dump(mode="json")
        for user in users
    ]


# =============================================================================
# The read, as the database receives it
# =============================================================================


async def test_the_role_under_test_owns_nothing(limited_role_env):
    """The reads run as the deployment's runtime role, not as an owner."""
    engine = create_async_engine(limited_url(limited_role_env, _ROLE, _PW), future=True)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT r.rolsuper, r.rolbypassrls, "
                        "pg_has_role(current_user, t.tableowner, 'USAGE') "
                        "FROM pg_roles r, pg_tables t "
                        "WHERE r.rolname = current_user AND t.tablename = 'users'"
                    )
                )
            ).one()
            assert tuple(row) == (False, False, False)
    finally:
        await engine.dispose()


async def test_the_read_selects_exactly_the_account_columns(world):
    """The statement the database receives — captured on the wire, not built
    in the test — selects the eleven account columns and nothing else from
    ``users``: no credential, SSO subject, role list or preference."""
    from faultmaven.infrastructure.persistence.models import UserModel
    from faultmaven.infrastructure.persistence.user_repository import (
        PostgreSQLUserRepository,
    )

    statements = []
    engine = create_async_engine(limited_url(world.owner, _ROLE, _PW), future=True)

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with AsyncSession(bind=engine) as session:
            accounts, _ = await PostgreSQLUserRepository(session).list_account_metadata(
                is_active=None,
                search=world.suffix,
                enterprise_id=None,
                limit=100,
                offset=0,
            )
    finally:
        await engine.dispose()

    assert accounts, "the read returned nothing, so its statement proves nothing"
    (select_list,) = [
        re.split(r"\sFROM\s", statement, maxsplit=1)[0]
        for statement in statements
        if statement.lstrip().upper().startswith("SELECT")
        and re.search(r"\bFROM users\b", statement)
    ]
    columns = {column.name for column in UserModel.__table__.columns}
    selected = {
        name for name in columns if re.search(rf"\busers\.{name}\b", select_list)
    }
    assert selected == _ACCOUNT_COLUMNS, select_list
    assert (
        _NEVER_SELECTED <= columns
    ), "a never-selected name is not a users column — the check is vacuous"
    assert not (_NEVER_SELECTED & selected)


async def test_no_returned_value_carries_a_credential_sso_subject_or_role(world):
    """Behaviourally: every fixture's password hash, SSO subject, avatar and
    locale is a sentinel and every one holds a role, and none of them reaches
    the read. The fixture ids are asserted present, so a read that returned
    nothing cannot pass."""
    accounts, _ = await _read(search=world.suffix)

    assert len(accounts) == len(world.a_order) + len(world.b)
    dumped = "\n".join(repr(account) for account in accounts)
    assert _SENTINEL not in dumped
    assert "xx-XX" not in dumped
    assert "workos" not in dumped
    assert "admin" not in dumped


# =============================================================================
# Parity with the listing confined to the operator's enterprise
# =============================================================================


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"is_active": False},
        {"is_active": True},
        {"search": "mixed case"},
        {"search": "EXAMPLE.COM"},
        {"search": "acct_a_s"},
        # A literal percent sign and underscore: as LIKE wildcards the first
        # would match every account and the second "100% uptime bot".
        {"search": "%"},
        {"search": "_uptime"},
        {"search": "nobody-matches-this"},
    ],
    ids=lambda f: "-".join(f"{k}={v}" for k, v in f.items()) or "none",
)
async def test_the_multi_path_serves_what_the_confined_path_serves(world, filters):
    """Field for field and in order, for the operator's own enterprise.

    The confined path is ``UserService.list_users`` with the enterprise as its
    tenant predicate — what this route served under multi before it spanned
    enterprises — projected as the route projects a manageable row. The multi
    path is the route itself, over the real account store, narrowed to the
    operator's enterprise by its filter."""
    app, service = _route_app(world.ent_a)
    set_current_enterprise_id(world.ent_a)
    users, confined_total = await service.list_users(
        enterprise_id=world.ent_a, limit=100, **filters
    )
    confined = _confined_rows(users, world.ent_a)

    params = {"enterprise_id": world.ent_a, "limit": 100}
    params.update(
        {k: str(v).lower() if isinstance(v, bool) else v for k, v in filters.items()}
    )
    response = await _get(app, "/api/v1/admin/users", **params)

    assert response.status_code == 200, response.text
    assert response.json()["users"] == confined
    assert response.json()["total"] == confined_total

    if not filters:
        # The comparison is only as strong as its fixtures: every shape is here…
        assert [row["user_id"] for row in confined] == world.a_order
        rows = {row["user_id"]: row for row in confined}
        a = world.a
        assert rows[a["no_roles"].user_id]["roles"] == ["member"]
        assert rows[a["person"].user_id]["roles"] == ["admin"]
        assert rows[a["inactive"].user_id]["is_active"] is False
        assert rows[a["unverified"].user_id]["is_verified"] is False
        assert rows[a["logged_in"].user_id]["last_login_at"] is not None
        assert rows[a["slack"].user_id]["account_kind"] == "service"
        assert rows[a["slack"].user_id]["service_channel"] == "slack"
        assert all(row["manageable"] is True for row in confined)
    if filters.get("search") == "%":
        assert [row["user_id"] for row in confined] == [world.a["percent"].user_id]
    if filters.get("search") == "_uptime":
        # As a LIKE wildcard '_' would match the space in "100% uptime bot".
        assert confined == []


async def test_another_enterprises_rows_report_no_roles_and_are_not_manageable(
    world,
):
    """The same route, unfiltered: B's accounts are listed beside A's — the
    list spans enterprises — with ``roles == []`` although every one holds a
    role, and ``manageable == False``."""
    app, _ = _route_app(world.ent_a)
    set_current_enterprise_id(world.ent_a)
    response = await _get(app, "/api/v1/admin/users", search=world.suffix, limit=100)

    assert response.status_code == 200, response.text
    rows = {row["user_id"]: row for row in response.json()["users"]}
    assert set(rows) == set(world.a_order) | {u.user_id for u in world.b.values()}
    assert response.json()["total"] == len(rows)
    for user in world.b.values():
        row = rows[user.user_id]
        assert row["enterprise_id"] == world.ent_b
        assert (row["roles"], row["manageable"]) == ([], False), row
    assert rows[world.b["slack"].user_id]["account_kind"] == "service"
    for user_id in world.a_order:
        assert rows[user_id]["manageable"] is True
        assert rows[user_id]["enterprise_id"] == world.ent_a


async def test_the_roles_read_answers_only_the_operators_enterprise(world):
    """Asked about an id of each enterprise and one naming nobody, the roles
    read answers A's account alone — the enterprise predicate is the query's."""
    from faultmaven.infrastructure.persistence.user_repository import (
        SessionlessUserRepository,
    )

    set_current_enterprise_id(world.ent_a)
    users = await SessionlessUserRepository().get_many_in_enterprise(
        world.ent_a,
        [world.a["person"].user_id, world.b["person"].user_id, "nobody-at-all"],
    )

    assert [user.user_id for user in users] == [world.a["person"].user_id]
    assert users[0].roles == ["admin"]
    assert (
        await SessionlessUserRepository().get_many_in_enterprise(world.ent_a, []) == []
    )


async def test_a_page_boundary_among_tied_creations_falls_in_the_same_place(world):
    """Six accounts share one ``created_at``, written in descending id order.
    Both paths break the tie on ``user_id``, so paging through them — a boundary
    falling mid-tie — serves the same rows on the same pages, each once."""
    app, service = _route_app(world.ent_a)
    set_current_enterprise_id(world.ent_a)
    pages = [(0, 4), (4, 2)]

    confined, multi = [], []
    for offset, limit in pages:
        users, _ = await service.list_users(
            enterprise_id=world.ent_a, limit=limit, offset=offset
        )
        confined.append([user.user_id for user in users])
        response = await _get(
            app,
            "/api/v1/admin/users",
            enterprise_id=world.ent_a,
            limit=limit,
            offset=offset,
        )
        multi.append([row["user_id"] for row in response.json()["users"]])

    assert confined == [world.tied[:4], world.tied[4:]]
    assert multi == confined


# =============================================================================
# Page, total, and the inputs the database cannot bind
# =============================================================================


@pytest.mark.parametrize("is_active", [None, True, False])
async def test_the_total_counts_every_match_in_every_enterprise(world, is_active):
    """Counted with the page, checked against a count written independently."""
    accounts, total = await _read(is_active=is_active, search=world.suffix)
    ((expected,),) = await _as_owner(
        world.owner,
        "SELECT count(*) FROM users WHERE (CAST(:a AS boolean) IS NULL "
        "OR is_active = CAST(:a AS boolean)) AND (position(:s in email) > 0 "
        "OR position(:s in display_name) > 0)",
        a=is_active,
        s=world.suffix,
    )
    assert total == expected == len(accounts)
    assert all(is_active is None or a.is_active is is_active for a in accounts)


async def test_a_short_page_carries_the_total_of_all_matches(world):
    """A page of two still reports every match, and the page past the last one
    is empty with the same total."""
    expected = len(world.a_order)
    page, total = await _read(enterprise_id=world.ent_a, limit=2)
    assert (len(page), total) == (2, expected)
    beyond, beyond_total = await _read(
        enterprise_id=world.ent_a, limit=2, offset=expected + 5
    )
    assert (beyond, beyond_total) == ([], expected)


@pytest.mark.parametrize("offset", [2**63 - 1, 2**63, 10**30])
async def test_an_offset_past_bigint_is_an_empty_page_with_the_true_total(
    world, offset
):
    """The API bounds the offset below and not above. Up to ``bigint`` the
    database pages normally; past it, the value cannot be bound — the read
    answers an empty page with the true total instead of failing after the
    access was recorded."""
    app, _ = _route_app(world.ent_a)
    set_current_enterprise_id(world.ent_a)
    response = await _get(
        app, "/api/v1/admin/users", enterprise_id=world.ent_a, offset=offset
    )

    assert response.status_code == 200, response.text
    assert response.json()["users"] == []
    assert response.json()["total"] == len(world.a_order)


async def test_a_search_containing_nul_is_an_empty_page_not_an_error(world):
    """PostgreSQL text cannot hold NUL, so no account matches — and the driver
    would refuse to bind it. Answered as the single-tenant listing answers it:
    an empty page."""
    app, _ = _route_app(world.ent_a)
    set_current_enterprise_id(world.ent_a)
    response = await _get(app, "/api/v1/admin/users", search=f"acct\x00{world.suffix}")

    assert response.status_code == 200, response.text
    assert response.json()["users"] == []
    assert response.json()["total"] == 0


async def test_the_read_spans_enterprises_from_a_session_bound_to_one(world):
    """``users`` is outside row-level security: bound to A, the read still
    returns B's accounts."""
    set_current_enterprise_id(world.ent_a)
    accounts, total = await _read(search=world.suffix)
    assert {a.enterprise_id for a in accounts} == {world.ent_a, world.ent_b}
    assert total == len(world.a_order) + len(world.b)
