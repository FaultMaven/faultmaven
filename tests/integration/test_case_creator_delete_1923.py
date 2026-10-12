"""Deleting an account against a real database (#1923).

The account-deletion route, the real ``DatabaseUserStore`` and the dialect case
repository's creator count, on SQLite migrated to head (so the
``users_delete_refuses_case_creator`` trigger is there) and on PostgreSQL when
``DATABASE_URL`` names one (CI's postgres job; ``cases_user_id_fkey`` is
``ON DELETE RESTRICT``):

- a creator is refused 409 ``ACCOUNT_OWNS_CASES`` before anything is revoked,
  and the account and its case are untouched;
- a driver who created nothing is deleted (200), and the case goes back to its
  creator (ADR-020 D3);
- when the count misses a case the database still refuses, and that refusal is
  the same 409 — the key is the guarantee, the count the explanation.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultmaven.api.operator_user_scope import OperatorUserScope
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.persistence.user_repository import (
    PostgreSQLUserRepository,
)
from faultmaven.modules.auth.api import auth as auth_routes
from faultmaven.modules.auth.exceptions import AccountOwnsCasesError
from tests.integration.security.conftest import alembic_on

pytestmark = [pytest.mark.integration]

_PG = os.environ.get("DATABASE_URL", "").startswith("postgresql")


class _PerCallUserRepository:
    """The user repository in a session of its own per call, as the sessionless
    wrapper makes it, on this test's engine."""

    def __init__(self, Session):
        self._Session = Session

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            async with self._Session() as session:
                result = await getattr(PostgreSQLUserRepository(session), name)(
                    *args, **kwargs
                )
                await session.commit()
                return result

        return call


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgresql",
            marks=[
                pytest.mark.postgres,
                pytest.mark.skipif(
                    not _PG, reason="PostgreSQL arm: set DATABASE_URL to a PG instance"
                ),
            ],
        ),
    ]
)
async def world(request, tmp_path):
    """An enterprise of its own with a CREATOR, whose case a DRIVER drives."""
    if request.param == "sqlite":
        path = tmp_path / "creator.db"
        result = alembic_on(f"sqlite:///{path}", "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

        @event.listens_for(engine.sync_engine, "connect")
        def _fk_on(dbapi_connection, _record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

        from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
            SQLiteCaseRepository as Repository,
        )
    else:
        engine = create_async_engine(os.environ["DATABASE_URL"])
        from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.repository import (
            PostgreSQLHybridCaseRepository as Repository,
        )

    tag = uuid4().hex[:8]
    enterprise = f"ent_{tag}"
    creator, driver = f"creator_{tag}", f"driver_{tag}"
    case_id = f"case_{tag}"
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        await session.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES (:e, :e, :e)"
            ),
            {"e": enterprise},
        )
        for uid in (creator, driver):
            await session.execute(
                text(
                    "INSERT INTO users (user_id, enterprise_id, username, email, "
                    "display_name) VALUES (:u, :e, :u, :m, :u)"
                ),
                {"u": uid, "e": enterprise, "m": f"{uid}@example.com"},
            )
        await session.execute(
            text(
                "INSERT INTO cases (case_id, enterprise_id, user_id, driver_id, "
                "title) VALUES (:c, :e, :creator, :driver, 't')"
            ),
            {"c": case_id, "e": enterprise, "creator": creator, "driver": driver},
        )
        await session.commit()

    async def count(user_id):
        async with Session() as session:
            return await Repository(session).count_cases_created_by(user_id)

    try:
        yield SimpleNamespace(
            Session=Session,
            store=DatabaseUserStore(_PerCallUserRepository(Session)),
            count=count,
            enterprise=enterprise,
            creator=creator,
            driver=driver,
            case_id=case_id,
        )
    finally:
        async with Session() as session:
            for statement in (
                "DELETE FROM cases WHERE enterprise_id = :e",
                "DELETE FROM users WHERE enterprise_id = :e",
                "DELETE FROM enterprises WHERE enterprise_id = :e",
            ):
                await session.execute(text(statement), {"e": enterprise})
            await session.commit()
        await engine.dispose()


async def _delete(world, username, *, count=None):
    """The route, with the real store and the dialect repository's count."""
    revoked: list = []

    async def _revoke(user_id):
        revoked.append(user_id)

    state = SimpleNamespace(
        user_service=SimpleNamespace(count_cases_created_by=count or world.count),
        auth_service=SimpleNamespace(revoke_user_tokens=_revoke),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    with patch.object(
        auth_routes, "get_user_store", AsyncMock(return_value=world.store)
    ):
        try:
            return (
                await auth_routes.delete_user(
                    username=username,
                    request=request,
                    operator=None,
                    scope=OperatorUserScope(users=None),
                ),
                revoked,
            )
        except HTTPException as refused:
            return refused, revoked


async def _row(world):
    async with world.Session() as session:
        return (
            await session.execute(
                text("SELECT user_id, driver_id FROM cases WHERE case_id = :c"),
                {"c": world.case_id},
            )
        ).one()


async def _exists(world, user_id) -> bool:
    return await world.store.get_user(user_id) is not None


async def test_the_dialect_repository_counts_what_an_account_created(world):
    assert await world.count(world.creator) == 1
    assert await world.count(world.driver) == 0


async def test_a_creator_is_refused_and_nothing_is_revoked(world):
    refused, revoked = await _delete(world, world.creator)

    assert isinstance(refused, HTTPException)
    assert refused.status_code == 409
    assert refused.headers == {"x-error-code": "ACCOUNT_OWNS_CASES"}
    assert "created 1 case(s)" in refused.detail
    assert revoked == []
    assert await _exists(world, world.creator)
    assert tuple(await _row(world)) == (world.creator, world.driver)


async def test_a_driver_is_deleted_and_the_case_goes_back_to_its_creator(world):
    result, revoked = await _delete(world, world.driver)

    assert result["user_id"] == world.driver
    assert revoked == [world.driver]
    assert not await _exists(world, world.driver)
    assert tuple(await _row(world)) == (world.creator, None)


async def test_the_database_refuses_what_the_count_missed(world):
    """A count that saw nothing (a case created after it, or hidden from it):
    the key refuses the delete, and the answer is the same 409."""
    refused, revoked = await _delete(
        world, world.creator, count=AsyncMock(return_value=0)
    )

    assert isinstance(refused, HTTPException)
    assert refused.status_code == 409
    assert refused.headers == {"x-error-code": "ACCOUNT_OWNS_CASES"}
    assert revoked == [world.creator]
    assert await _exists(world, world.creator)
    assert tuple(await _row(world)) == (world.creator, world.driver)


async def test_the_store_raises_the_typed_refusal(world):
    with pytest.raises(AccountOwnsCasesError):
        await world.store.delete_user(world.creator)
    assert await _exists(world, world.creator)
