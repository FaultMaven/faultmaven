"""Polymorphic resource-share repository (``resource_shares``, ADR-013 §D4).

Pins the share table's behaviour as the single source of truth for team
visibility: idempotent share, unshare, the allowlist direction
(``list_resource_ids``), the resource→scopes direction, and the two cascade
deletes (resource-delete / scope-delete). Exercised over FK-on SQLite — the same
constraint regime production runs under (#378).
"""

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.share_repository import (
    PostgreSQLShareRepository,
)


@pytest.fixture
async def share_factory():
    """FK-on in-memory SQLite with enterprise/org/user seeded for the FKs."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES ('ent-1', 'Default', 'default')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO organizations "
                "(organization_id, enterprise_id, name, slug) "
                "VALUES ('org-1', 'ent-1', 'Org', 'org')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO users "
                "(user_id, enterprise_id, username, email, display_name) "
                "VALUES ('user-1', 'ent-1', 'u1', 'u1@e', 'u1')"
            )
        )

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _share(factory, **kw):
    async with factory() as session:
        return await PostgreSQLShareRepository(session).share(**kw)


_KB = {
    "resource_type": "knowledge_item",
    "scope_type": "team",
    "organization_id": "org-1",
}


@pytest.mark.asyncio
class TestShareRepository:
    async def test_share_then_list_resource_ids(self, share_factory):
        await _share(share_factory, resource_id="kb-1", scope_id="team-A", **_KB)
        async with share_factory() as session:
            ids = await PostgreSQLShareRepository(session).list_resource_ids(
                resource_type="knowledge_item",
                scope_type="team",
                scope_ids=["team-A", "team-B"],
            )
        assert ids == ["kb-1"]

    async def test_share_is_idempotent(self, share_factory):
        # Re-sharing the same (resource, scope) is a no-op — one row survives the
        # unique key, and the same row is returned.
        r1 = await _share(share_factory, resource_id="kb-1", scope_id="team-A", **_KB)
        r2 = await _share(share_factory, resource_id="kb-1", scope_id="team-A", **_KB)
        assert r1.share_id == r2.share_id
        async with share_factory() as session:
            n = await PostgreSQLShareRepository(session).count_shares_for_resource(
                "knowledge_item", "kb-1"
            )
        assert n == 1

    async def test_list_resource_ids_empty_scopes_returns_empty(self, share_factory):
        await _share(share_factory, resource_id="kb-1", scope_id="team-A", **_KB)
        async with share_factory() as session:
            ids = await PostgreSQLShareRepository(session).list_resource_ids(
                resource_type="knowledge_item", scope_type="team", scope_ids=[]
            )
        assert ids == []

    async def test_list_resource_ids_deduplicates(self, share_factory):
        # One item shared to two of the user's teams resolves once.
        await _share(share_factory, resource_id="kb-1", scope_id="team-A", **_KB)
        await _share(share_factory, resource_id="kb-1", scope_id="team-B", **_KB)
        async with share_factory() as session:
            ids = await PostgreSQLShareRepository(session).list_resource_ids(
                resource_type="knowledge_item",
                scope_type="team",
                scope_ids=["team-A", "team-B"],
            )
        assert ids == ["kb-1"]

    async def test_unshare(self, share_factory):
        await _share(share_factory, resource_id="kb-1", scope_id="team-A", **_KB)
        async with share_factory() as session:
            removed = await PostgreSQLShareRepository(session).unshare(
                resource_type="knowledge_item",
                resource_id="kb-1",
                scope_type="team",
                scope_id="team-A",
            )
        assert removed is True
        async with share_factory() as session:
            ids = await PostgreSQLShareRepository(session).list_resource_ids(
                resource_type="knowledge_item",
                scope_type="team",
                scope_ids=["team-A"],
            )
        assert ids == []

    async def test_list_scopes_for_resource(self, share_factory):
        await _share(share_factory, resource_id="kb-1", scope_id="team-A", **_KB)
        await _share(share_factory, resource_id="kb-1", scope_id="team-B", **_KB)
        async with share_factory() as session:
            scopes = await PostgreSQLShareRepository(session).list_scopes_for_resource(
                "knowledge_item", "kb-1"
            )
        assert {s.scope_id for s in scopes} == {"team-A", "team-B"}
        assert all(s.scope_type == "team" for s in scopes)

    async def test_delete_for_resource_cascade(self, share_factory):
        # Resource-delete cascade (no DB FK on the polymorphic resource_id).
        await _share(share_factory, resource_id="kb-1", scope_id="team-A", **_KB)
        await _share(share_factory, resource_id="kb-1", scope_id="team-B", **_KB)
        await _share(share_factory, resource_id="kb-2", scope_id="team-A", **_KB)
        async with share_factory() as session:
            n = await PostgreSQLShareRepository(session).delete_for_resource(
                "knowledge_item", "kb-1"
            )
        assert n == 2
        async with share_factory() as session:
            repo = PostgreSQLShareRepository(session)
            assert await repo.count_shares_for_resource("knowledge_item", "kb-1") == 0
            assert await repo.count_shares_for_resource("knowledge_item", "kb-2") == 1

    async def test_delete_for_scope_cascade(self, share_factory):
        # Team-delete cascade — every share to that team is removed.
        await _share(share_factory, resource_id="kb-1", scope_id="team-A", **_KB)
        await _share(share_factory, resource_id="kb-2", scope_id="team-A", **_KB)
        await _share(share_factory, resource_id="kb-3", scope_id="team-B", **_KB)
        async with share_factory() as session:
            n = await PostgreSQLShareRepository(session).delete_for_scope(
                "team", "team-A"
            )
        assert n == 2
        async with share_factory() as session:
            ids = await PostgreSQLShareRepository(session).list_resource_ids(
                resource_type="knowledge_item",
                scope_type="team",
                scope_ids=["team-A", "team-B"],
            )
        assert ids == ["kb-3"]

    async def test_resource_type_isolation(self, share_factory):
        # A conversion_job share to team-A must not surface as a knowledge_item.
        await _share(share_factory, resource_id="kb-1", scope_id="team-A", **_KB)
        await _share(
            share_factory,
            resource_type="conversion_job",
            resource_id="job-1",
            scope_type="team",
            scope_id="team-A",
            organization_id="org-1",
        )
        async with share_factory() as session:
            kb_ids = await PostgreSQLShareRepository(session).list_resource_ids(
                resource_type="knowledge_item",
                scope_type="team",
                scope_ids=["team-A"],
            )
        assert kb_ids == ["kb-1"]
