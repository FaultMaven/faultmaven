"""A failure after the item's INSERT commits leaves no item, and the cleanup
removes only the row its own call wrote (#2378).

``DatabaseKnowledgeItemRepository.create`` commits the row and then refreshes
it. ``ingest_runbook`` used to call it before its compensated block, so a raise
or a cancel in the refresh left a published row with no vectors, which the
startup repair re-embeds: a failed upload went live with no draft. Inside the
block, its "already exists" refusal must not run the cleanup, and the cleanup
must not delete a row another call created under the same id (``kb_init``
replicas re-ingesting one pack id). Driven through the real service on SQLite
with foreign keys enforced, the vectors stubbed, as the #2365 tests are.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import anyio
import pytest
from sqlalchemy import select, update

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.persistence.models import KnowledgeItemModel
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    KnowledgeItemRepositoryException,
)
from tests.integration.modules.knowledge.test_upload_writes_rows_after_ingest_2365 import (  # noqa: F401
    NOTHING,
    OWNER,
    PARK,
    TEAM_ID,
    _drafts,
    _knowledge,
    _rows,
    _upload,
    session_factory,
)
from tests.runbook_samples import valid_runbook

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
    service._index_document_in_vector_store.assert_not_awaited()
    service._vector_store.delete_documents_by_parent_id.assert_awaited_once()


async def test_an_already_exists_refusal_cleans_up_nothing(session_factory):
    service = _knowledge(session_factory)
    await _ingest(service)
    existing = await _created_at(session_factory)
    service._index_document_in_vector_store.reset_mock()

    with pytest.raises(ValueError, match="already exists"):
        await _ingest(service)

    assert await _created_at(session_factory) == existing
    assert await _rows(session_factory) == ONE_TEAM_ITEM
    service._index_document_in_vector_store.assert_not_awaited()
    service._vector_store.delete_documents_by_parent_id.assert_not_awaited()


async def test_the_cleanup_leaves_a_row_another_call_created(session_factory):
    """A peer replaced this call's row (a ``kb_init`` replica deletes the row and
    ingests its own) before this call's refresh failed."""

    async def a_peer_replaces_the_row():
        async with session_factory() as session:
            await session.execute(
                update(KnowledgeItemModel)
                .where(KnowledgeItemModel.item_id == DOC_ID)
                .values(created_at=PEER_CREATED_AT)
            )
            await session.commit()
        await service._create_team_share(
            resource_type="knowledge_item",
            resource_id=DOC_ID,
            team_id=TEAM_ID,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            created_by=OWNER,
        )

    factory = _ItemRefresh(
        session_factory, RuntimeError("refresh failed"), a_peer_replaces_the_row
    )
    service = _knowledge(factory)

    with pytest.raises(KnowledgeItemRepositoryException):
        await _ingest(service)

    (created_at,) = await _created_at(session_factory)
    assert created_at.replace(tzinfo=None) == PEER_CREATED_AT.replace(tzinfo=None)
    assert await _rows(session_factory) == ONE_TEAM_ITEM
