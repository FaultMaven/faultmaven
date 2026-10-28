"""A failure after the item's INSERT commits leaves no item, and the cleanup
removes only what its own call wrote (#2378).

``DatabaseKnowledgeItemRepository.create`` commits the row and then refreshes
it. ``ingest_runbook`` used to call it before its compensated block, so a raise
or a cancel in the refresh left a published row with no vectors, which the
startup repair re-embeds: a failed upload went live with no draft. Inside the
block, the cleanup must not touch what another call wrote under the same id:
the row an "already exists" refusal names, or one a ``kb_init`` replica
re-created (its ``_delete_existing`` removes the id's chunks and row, then it
ingests its own). Driven through the real service on SQLite with foreign keys
enforced, as the #2365 tests are, over a vector store that keeps each chunk's
parent id and writer.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import partial
from uuid import uuid4

import anyio
import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import (
    EnterpriseModel,
    KnowledgeItemModel,
    UserModel,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
    KnowledgeItemRepositoryException,
)
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope
from tests.integration.modules.knowledge.test_upload_writes_rows_after_ingest_2365 import (  # noqa: F401
    NOTHING,
    OWNER,
    PARK,
    TEAM_ID,
    _drafts,
    _knowledge,
    _rows,
    _Shares,
    _upload,
    session_factory,
)
from tests.runbook_samples import valid_runbook
from tests.utils import seed_enterprises, seed_users

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

DOC_ID = "kb_0123456789abcdef"
#: What a peer's row carries: not the instant this call stamps.
PEER_CREATED_AT = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
ONE_TEAM_ITEM = {**NOTHING, "knowledge_items": 1, "resource_shares": 1}


class _ItemRefresh:
    """The session factory. The item row's refresh, which ``create`` runs once
    its INSERT has committed, runs ``meanwhile`` and then raises ``error``, or
    for ``PARK`` waits to be cancelled."""

    def __init__(self, factory, error, meanwhile=None) -> None:
        self._factory, self._error, self._meanwhile = factory, error, meanwhile
        self.parked = asyncio.Event()

    def __call__(self):
        session = self._factory()
        real_refresh = session.refresh

        async def refresh(instance, *args, **kwargs):
            if not isinstance(instance, KnowledgeItemModel):
                return await real_refresh(instance, *args, **kwargs)
            if self._meanwhile:
                await self._meanwhile()
            if self._error is PARK:
                self.parked.set()
                await asyncio.Event().wait()
            raise self._error

        session.refresh = refresh
        return session


class _Chunks:
    """A vector store keeping each chunk's parent id, under ids naming its
    writer."""

    def __init__(self) -> None:
        self.chunks: dict[str, str] = {}

    def index(self, parent_id: str, writer: str) -> None:
        self.chunks.update({f"{writer}-{i}": parent_id for i in range(3)})

    async def delete_documents_by_parent_id(self, parent_id, retries=2):
        for chunk_id in [k for k, v in self.chunks.items() if v == parent_id]:
            del self.chunks[chunk_id]


class _FailingClose:
    """The session factory. Once armed, the next session's close raises after
    it has closed."""

    def __init__(self, factory) -> None:
        self._factory, self.armed = factory, False

    async def arm(self) -> None:
        self.armed = True

    def __call__(self):
        session = self._factory()
        if self.armed:
            self.armed = False
            real_close = session.close

            async def close():
                await real_close()
                raise OSError("close failed")

            session.close = close
        return session


def _indexing_into(service, store: _Chunks, *, then=None):
    """``service`` indexing into ``store``; with ``then``, it runs ``then`` once
    its chunks are written and raises, as a store failing after the write."""

    async def index(document, prechunked=None, enterprise_id=None):
        store.index(document.document_id, "this")
        if then:
            await then()
            raise RuntimeError("store failed after the write")
        return 3

    service._vector_store = store
    service._index_document_in_vector_store = index
    return service


async def _delete_the_row(factory) -> None:
    async with factory() as session:
        await session.execute(
            delete(KnowledgeItemModel).where(KnowledgeItemModel.item_id == DOC_ID)
        )
        await session.commit()


async def _a_peer_reingests(factory, store: _Chunks):
    """``kb_init`` on another replica: ``_delete_existing`` (the id's chunks,
    then its row), then its own ingest under the same id."""
    await store.delete_documents_by_parent_id(DOC_ID)
    await _delete_the_row(factory)
    async with factory() as session:
        await DatabaseKnowledgeItemRepository(session).create(
            KnowledgeItem(
                item_id=DOC_ID,
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                title="Peer",
                content="peer",
                item_type=KnowledgeItemType.RUNBOOK,
                scope=KnowledgeScope.TEAM,
                owner_id=OWNER,
                created_at=PEER_CREATED_AT,
                updated_at=PEER_CREATED_AT,
            )
        )
    await _Shares(factory).share(
        resource_type="knowledge_item",
        resource_id=DOC_ID,
        scope_type="team",
        scope_id=TEAM_ID,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        created_by=OWNER,
    )
    store.index(DOC_ID, "peer")


async def _ingest(service):
    return await service.ingest_runbook(
        document_id=DOC_ID,
        title="Redis Evictions",
        content=valid_runbook("Redis Evictions Under Memory Pressure"),
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope="team",
        owner_id=OWNER,
        team_id=TEAM_ID,
    )


async def _created_at(factory) -> list[datetime]:
    query = select(KnowledgeItemModel.created_at).where(
        KnowledgeItemModel.item_id == DOC_ID
    )
    async with factory() as session:
        return list(await session.scalars(query))


@pytest.mark.parametrize("how", ["error", "anyio_scope"])
async def test_a_failure_after_the_insert_commits_leaves_nothing(session_factory, how):
    factory = _ItemRefresh(
        session_factory, RuntimeError("refresh failed") if how == "error" else PARK
    )
    service = _knowledge(factory)

    if how == "error":
        with pytest.raises(KnowledgeItemRepositoryException):
            await _upload(service, scope="team")
    else:

        async def run():
            await _upload(service, scope="team")

        async with anyio.create_task_group() as group:
            group.start_soon(run)
            await factory.parked.wait()
            group.cancel_scope.cancel()

    assert await _rows(session_factory) == NOTHING
    assert await _drafts(session_factory) == []
    # Nothing was indexed, so there are no chunks of this call's to remove.
    service._index_document_in_vector_store.assert_not_awaited()
    service._vector_store.delete_documents_by_parent_id.assert_not_awaited()


async def test_an_already_exists_refusal_leaves_the_row_whole(session_factory):
    store = _Chunks()
    service = _indexing_into(_knowledge(session_factory), store)
    await _ingest(service)
    existing = await _created_at(session_factory)

    with pytest.raises(ValueError, match="already exists"):
        await _ingest(service)

    assert await _created_at(session_factory) == existing
    assert await _rows(session_factory) == ONE_TEAM_ITEM
    assert sorted(store.chunks) == ["this-0", "this-1", "this-2"]


@pytest.mark.parametrize("fails_in", ["refresh", "share", "indexing"])
async def test_the_cleanup_leaves_what_a_peer_created(session_factory, fails_in):
    """A peer re-ingested the id, and then this call failed: before its own
    indexing (in the item's refresh or its share write) or after it."""
    store = _Chunks()
    peer = partial(_a_peer_reingests, session_factory, store)
    factory = session_factory
    if fails_in == "refresh":
        factory = _ItemRefresh(session_factory, RuntimeError("refresh failed"), peer)
    service = _indexing_into(
        _knowledge(factory), store, then=peer if fails_in == "indexing" else None
    )
    if fails_in == "share":

        async def share(**_kwargs):
            await peer()
            raise RuntimeError("share failed")

        service._share_repo.share = share

    with pytest.raises((KnowledgeItemRepositoryException, RuntimeError)):
        await _ingest(service)

    (created_at,) = await _created_at(session_factory)
    assert created_at.replace(tzinfo=None) == PEER_CREATED_AT.replace(tzinfo=None)
    assert await _rows(session_factory) == ONE_TEAM_ITEM
    assert sorted(store.chunks) == ["peer-0", "peer-1", "peer-2"]


async def test_a_share_whose_row_is_gone_is_removed(session_factory):
    """A peer's ``_delete_existing`` removed the id's chunks before this call
    wrote its own, then this call's row (not its share), and has not
    re-created it: no item stands, so this call's chunks and share go."""
    store = _Chunks()
    await store.delete_documents_by_parent_id(DOC_ID)
    service = _indexing_into(
        _knowledge(session_factory),
        store,
        then=partial(_delete_the_row, session_factory),
    )

    with pytest.raises(RuntimeError, match="after the write"):
        await _ingest(service)

    assert await _rows(session_factory) == NOTHING
    assert store.chunks == {}


async def test_a_cleanup_session_that_fails_to_close_carries_on(
    session_factory, caplog
):
    """The cleanup's row delete committed, and then closing its session raised:
    the row is gone, so its chunks and share go too."""
    factory = _FailingClose(session_factory)
    store = _Chunks()
    service = _indexing_into(_knowledge(factory), store, then=factory.arm)

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(RuntimeError, match="after the write"),
    ):
        await _ingest(service)

    assert await _rows(session_factory) == NOTHING
    assert store.chunks == {}
    assert "closing its cleanup's session failed (OSError)" in caplog.text
    assert "check for a row" not in caplog.text


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
)
async def test_the_guard_compares_instants_on_postgresql():
    """``timestamptz`` keeps the instant: the stamped one, in any zone, matches;
    a microsecond off does not."""
    enterprise, owner = f"ent_2378_{uuid4().hex[:8]}", f"user-2378-{uuid4().hex[:8]}"
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    stamped = datetime.now(timezone.utc)
    item = KnowledgeItem(
        item_id=f"kb_{uuid4().hex[:16]}",
        enterprise_id=enterprise,
        title="T",
        content="c",
        item_type=KnowledgeItemType.RUNBOOK,
        scope=KnowledgeScope.PERSONAL,
        owner_id=owner,
        created_at=stamped,
        updated_at=stamped,
    )
    try:
        async with factory() as session:
            await seed_enterprises(session, [enterprise])
            await seed_users(session, [owner], enterprise_id=enterprise)
            await DatabaseKnowledgeItemRepository(session).create(item)

        async with factory() as session:
            repo = DatabaseKnowledgeItemRepository(session)
            off = stamped - timedelta(microseconds=1)
            assert await repo.delete(item.item_id, created_at=off) is False
            assert await repo.get_by_id(item.item_id) is not None
            ist = timezone(timedelta(hours=5, minutes=30))
            same = stamped.astimezone(ist)
            assert await repo.delete(item.item_id, created_at=same) is True
            assert await repo.get_by_id(item.item_id) is None
    finally:
        async with factory() as session:
            for model in (KnowledgeItemModel, UserModel, EnterpriseModel):
                await session.execute(
                    delete(model).where(model.enterprise_id == enterprise)
                )
            await session.commit()
        await engine.dispose()
