"""The account list breaks ``created_at`` ties on ``user_id`` (SQLite, in memory).

Accounts provisioned in one statement share a creation time, and ``ORDER BY
created_at DESC`` alone leaves their order to the engine — so a page boundary
among them can move between two reads, serving an account twice or never. The
cross-enterprise operator account list orders the same way, and its pages are
held equal to this listing's on PostgreSQL in
``tests/integration/security/test_admin_account_metadata_postgres.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    UserModel,
)
from faultmaven.infrastructure.persistence.user_repository import (
    InMemoryUserRepository,
    PostgreSQLUserRepository,
    User,
)

pytestmark = pytest.mark.unit

_ENTERPRISE = "ent_tie_order"
_AT = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
#: Written in DESCENDING id order, so insertion order is not the order the
#: list promises.
_IDS = [f"user_{n:012d}" for n in range(6)]


def _user(user_id: str) -> User:
    return User(
        user_id=user_id,
        username=user_id,
        email=f"{user_id}@example.com",
        display_name=user_id,
        enterprise_id=_ENTERPRISE,
        created_at=_AT,
        updated_at=_AT,
    )


async def _pages(repository):
    pages = []
    for offset, limit in ((0, 4), (4, 2)):
        users, total = await repository.list_users(
            limit=limit, offset=offset, enterprise_id=_ENTERPRISE
        )
        pages.append([user.user_id for user in users])
    return pages, total


async def test_a_page_boundary_among_tied_rows_is_stable_on_sqlite(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ties.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                tables=[EnterpriseModel.__table__, UserModel.__table__],
            )
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as session:
            session.add(EnterpriseModel(enterprise_id=_ENTERPRISE, name="e", slug="e"))
            await session.commit()
            repository = PostgreSQLUserRepository(session)
            for user_id in reversed(_IDS):
                await repository.create(_user(user_id))
            pages, total = await _pages(repository)
    finally:
        await engine.dispose()

    assert total == 6
    assert pages == [_IDS[:4], _IDS[4:]]


async def test_a_page_boundary_among_tied_rows_is_stable_in_memory():
    repository = InMemoryUserRepository()
    for user_id in reversed(_IDS):
        await repository.create(_user(user_id))

    pages, total = await _pages(repository)

    assert total == 6
    assert pages == [_IDS[:4], _IDS[4:]]
