"""Case read-scope allowlist — ``owned ∪ shared-to-my-teams`` (ADR-013 §D4 / U9).

Covers the SQL-level case analogue of the KB ``build_kb_scope_filter`` allowlist:
the ``case_scope_where`` predicate builder and its use in the SQLite repository's
``list`` / ``search``. The allowlist is behavior-neutral until case shares exist
(U10): with an empty ``shared_case_ids`` the query collapses to the pre-existing
owner-only filter.

Run:
    pytest tests/unit/modules/case/infrastructure/test_case_scope_allowlist.py -v
"""

from __future__ import annotations

from typing import AsyncGenerator
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.domain.models import Case, CaseState
from faultmaven.modules.case.infrastructure.case_scope import case_scope_where
from faultmaven.modules.case.infrastructure.sqlite_case_repository import (
    SQLiteCaseRepository,
)

# ============================================================
# case_scope_where — the predicate builder
# ============================================================


class TestCaseScopeWhere:
    def test_no_principal_returns_none_and_no_params(self):
        """Cross-tenant admin path (user_id falsy) adds no scope clause."""
        params: dict = {}
        assert case_scope_where(params, None) is None
        assert params == {}

    def test_owner_only_when_no_shares(self):
        """Behavior-neutral: without shares the clause is the legacy owner filter."""
        params: dict = {}
        clause = case_scope_where(params, "user_a")
        assert clause == "user_id = :user_id"
        assert params == {"user_id": "user_a"}

    def test_owner_or_shared_ids(self):
        params: dict = {}
        clause = case_scope_where(params, "user_a", ["case_1", "case_2"])
        assert clause == (
            "(user_id = :user_id OR case_id IN (:shared_case_0, :shared_case_1))"
        )
        assert params == {
            "user_id": "user_a",
            "shared_case_0": "case_1",
            "shared_case_1": "case_2",
        }

    def test_column_prefix_for_aliased_queries(self):
        """PostgreSQL full-text search aliases ``cases`` as ``c``."""
        params: dict = {}
        clause = case_scope_where(params, "user_a", ["case_1"], col_prefix="c.")
        assert clause == "(c.user_id = :user_id OR c.case_id IN (:shared_case_0))"


# ============================================================
# SQLite repository — list / search honor the allowlist
# ============================================================


@pytest.fixture(scope="function")
async def async_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="function")
async def async_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with session_factory() as session:
        yield session


@pytest.fixture
def repository(async_session) -> SQLiteCaseRepository:
    return SQLiteCaseRepository(async_session)


def _make_case(*, user_id: str, title: str = "Broker connection refused") -> Case:
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id=user_id,
        organization_id="org_alpha",
        title=title,
        description="",
        state=CaseState.INQUIRY,
    )


@pytest.fixture
async def seeded(repository):
    """One case owned by user_a, two owned by user_b (one to be shared)."""
    mine = _make_case(user_id="user_a")
    theirs_shared = _make_case(user_id="user_b")
    theirs_private = _make_case(user_id="user_b")
    for case in (mine, theirs_shared, theirs_private):
        await repository.save(case)
    return {
        "mine": mine.case_id,
        "theirs_shared": theirs_shared.case_id,
        "theirs_private": theirs_private.case_id,
    }


class TestSqliteListAllowlist:
    async def test_empty_shares_is_owner_only(self, repository, seeded):
        """U9 is behavior-neutral until U10: no shares → only own case."""
        cases, total = await repository.list(user_id="user_a", shared_case_ids=[])
        ids = {c.case_id for c in cases}
        assert ids == {seeded["mine"]}
        assert total == 1

    async def test_none_shares_is_owner_only(self, repository, seeded):
        cases, _ = await repository.list(user_id="user_a")
        assert {c.case_id for c in cases} == {seeded["mine"]}

    async def test_shared_case_is_visible(self, repository, seeded):
        """A case shared to my team surfaces alongside my own; others stay hidden."""
        cases, total = await repository.list(
            user_id="user_a", shared_case_ids=[seeded["theirs_shared"]]
        )
        ids = {c.case_id for c in cases}
        assert ids == {seeded["mine"], seeded["theirs_shared"]}
        assert seeded["theirs_private"] not in ids
        assert total == 2

    async def test_admin_path_sees_all(self, repository, seeded):
        """user_id=None (platform admin) is unscoped."""
        cases, total = await repository.list(user_id=None)
        assert total == 3


class TestSqliteSearchAllowlist:
    async def test_empty_shares_is_owner_only(self, repository, seeded):
        cases, _ = await repository.search(
            query="connection", user_id="user_a", shared_case_ids=[]
        )
        assert {c.case_id for c in cases} == {seeded["mine"]}

    async def test_shared_case_is_visible(self, repository, seeded):
        cases, _ = await repository.search(
            query="connection",
            user_id="user_a",
            shared_case_ids=[seeded["theirs_shared"]],
        )
        ids = {c.case_id for c in cases}
        assert ids == {seeded["mine"], seeded["theirs_shared"]}
        assert seeded["theirs_private"] not in ids
