"""Integration test for KnowledgeService.ingest_runbook against a real
in-memory SQLAlchemy engine.

The method is the single atomic dual-write entry point. It writes the
relational ``knowledge_items`` row first, then the ChromaDB chunks. The
contract (tightened 2026-05-26 — see PR #380 / kb-ingestion-architecture.md)
is **atomic across both stores**: on any failure (vector indexing raises,
or returns 0 chunks), the SQL row is rolled back before the exception
propagates. Callers can rely on a non-zero return meaning "fully ingested
in both stores"; an exception means "no half-state remains anywhere".

This test pins four invariants:

1. **Happy path** — the SQL row exists with the expected verification
   level, scope, and timestamps; ChromaDB ingestion was invoked.
2. **ChromaDB failure rolls back the SQL row** — orphans are the bug
   class the new design prevents (the previous "leave SQL for recovery"
   policy produced half-state rows that downstream scans then
   mis-classified, silently downgrading user-verified runbooks).
3. **Zero-chunks return is treated as failure** — the silent
   `chunks_created == 0` path (BGE-M3 unavailable, chunker empty, etc.)
   is now the same as raising: SQL row is rolled back, RuntimeError is
   raised.
4. **SQL failure short-circuits before ChromaDB** — ordering matters;
   ChromaDB must not be touched if the SQL write didn't succeed.

If any of those regress, this test fails on a real INSERT, not a mock.
"""

from __future__ import annotations

from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    KnowledgeItemModel,
    OrganizationModel,
)
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItemType,
    KnowledgeScope,
    VerificationLevel,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)

# =============================================================================
# Fixtures
# =============================================================================

DEFAULT_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ENTERPRISE_ID = "00000000-0000-0000-0000-000000000002"


@pytest.fixture(scope="function")
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture(scope="function")
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="function")
async def seeded_session_factory(session_factory):
    """Seed default enterprise + org so knowledge_items.organization_id FK is satisfied."""
    async with session_factory() as session:
        session.add(
            EnterpriseModel(
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                name="Default Enterprise",
                slug="default",
            )
        )
        session.add(
            OrganizationModel(
                organization_id=DEFAULT_ENTERPRISE_ID,
                enterprise_id=DEFAULT_ENTERPRISE_ID,
                name="Default Org",
                slug="default-org",
            )
        )
        await session.commit()
    return session_factory


def make_service(
    session_factory,
    *,
    chroma_returns: int = 5,
    chroma_raises: Optional[Exception] = None,
) -> KnowledgeService:
    """Construct a KnowledgeService wired with mocks for everything except
    the db_session_factory. The vector-store half is mocked because we
    want to assert SQL persistence without booting ChromaDB.
    """
    service = KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        vector_store=MagicMock(),
        db_session_factory=session_factory,
    )

    # _index_document_in_vector_store is the inner ChromaDB call.
    if chroma_raises is not None:
        service._index_document_in_vector_store = AsyncMock(side_effect=chroma_raises)
    else:
        service._index_document_in_vector_store = AsyncMock(return_value=chroma_returns)
    return service


# =============================================================================
# Tests
# =============================================================================


class TestIngestRunbookDualWrite:
    """Invariants of the renamed ingest_runbook method."""

    @pytest.mark.asyncio
    async def test_happy_path_writes_sql_row_then_chromadb(
        self, seeded_session_factory
    ):
        """SQL row exists with verifier metadata; ChromaDB was invoked once."""
        service = make_service(seeded_session_factory, chroma_returns=7)
        item_id = "kb_abc123def456"

        chunks = await service.ingest_runbook(
            document_id=item_id,
            title="Redis OOM",
            content="# Redis OOM\n\nIncrease maxmemory.",
            organization_id=DEFAULT_ENTERPRISE_ID,
            scope="global",
            owner_id="user-1",
            verified_by="user-1",
        )

        assert chunks == 7
        assert service._index_document_in_vector_store.await_count == 1

        async with seeded_session_factory() as session:
            row = (
                await session.execute(
                    select(KnowledgeItemModel).where(
                        KnowledgeItemModel.item_id == item_id
                    )
                )
            ).scalar_one_or_none()
            assert row is not None
            assert row.title == "Redis OOM"
            assert row.scope == "global"
            # Global rows are the org-free platform tier (#770): the passed
            # organization_id applies only to org-owned scopes.
            assert row.organization_id is None
            assert row.item_type == KnowledgeItemType.RUNBOOK.value
            assert row.verification_level == int(VerificationLevel.COMMUNITY)
            assert row.verified_by == "user-1"
            assert row.verified_at is not None

    @pytest.mark.asyncio
    async def test_unverified_upload_uses_experimental_level(
        self, seeded_session_factory
    ):
        """upload_document path passes verified_by=None → EXPERIMENTAL level, no verified_at."""
        service = make_service(seeded_session_factory)
        item_id = "kb_unverif00001"

        await service.ingest_runbook(
            document_id=item_id,
            title="Unverified",
            content="raw content",
            organization_id=DEFAULT_ENTERPRISE_ID,
            scope="personal",
            owner_id="user-2",
            verified_by=None,
        )

        async with seeded_session_factory() as session:
            row = (
                await session.execute(
                    select(KnowledgeItemModel).where(
                        KnowledgeItemModel.item_id == item_id
                    )
                )
            ).scalar_one()
            assert row.verification_level == int(VerificationLevel.EXPERIMENTAL)
            assert row.verified_by is None
            assert row.verified_at is None
            assert row.scope == KnowledgeScope.PERSONAL.value

    @pytest.mark.asyncio
    async def test_chromadb_failure_rolls_back_sql_row(self, seeded_session_factory):
        """If ChromaDB indexing raises, the just-written SQL row must be
        rolled back before the exception propagates — no orphan rows."""
        service = make_service(
            seeded_session_factory, chroma_raises=RuntimeError("chroma down")
        )
        item_id = "kb_chromaf00001"

        with pytest.raises(RuntimeError, match="chroma down"):
            await service.ingest_runbook(
                document_id=item_id,
                title="Doomed embed",
                content="content",
                organization_id=DEFAULT_ENTERPRISE_ID,
                scope="global",
                verified_by="user-3",
            )

        async with seeded_session_factory() as session:
            row = (
                await session.execute(
                    select(KnowledgeItemModel).where(
                        KnowledgeItemModel.item_id == item_id
                    )
                )
            ).scalar_one_or_none()
            assert row is None, (
                "SQL row must be rolled back when ChromaDB ingestion fails "
                "(atomicity contract — see kb-ingestion-architecture.md)"
            )

    @pytest.mark.asyncio
    async def test_zero_chunks_returns_rolls_back_sql_row(self, seeded_session_factory):
        """If vector indexing silently returns 0 chunks (BGE-M3 missing,
        chunker empty, etc.), treat it as failure: roll back the SQL row
        and raise RuntimeError."""
        service = make_service(seeded_session_factory, chroma_returns=0)
        item_id = "kb_zerochunk001"

        with pytest.raises(RuntimeError, match="0 chunks"):
            await service.ingest_runbook(
                document_id=item_id,
                title="Empty result",
                content="content",
                organization_id=DEFAULT_ENTERPRISE_ID,
                scope="global",
                verified_by="user-4",
            )

        async with seeded_session_factory() as session:
            row = (
                await session.execute(
                    select(KnowledgeItemModel).where(
                        KnowledgeItemModel.item_id == item_id
                    )
                )
            ).scalar_one_or_none()
            assert row is None, (
                "SQL row must be rolled back when vector indexing returns "
                "0 chunks (atomicity contract)"
            )

    @pytest.mark.asyncio
    async def test_sql_failure_short_circuits_chromadb(self, seeded_session_factory):
        """If the SQL write fails (here: duplicate item_id), ChromaDB is
        never invoked — there's no inverted state to recover from."""
        service = make_service(seeded_session_factory)
        item_id = "kb_dup00000001"

        # First write succeeds.
        await service.ingest_runbook(
            document_id=item_id,
            title="Original",
            content="c",
            organization_id=DEFAULT_ENTERPRISE_ID,
            scope="global",
        )
        first_chroma_count = service._index_document_in_vector_store.await_count

        # Second write with the same id should raise from the SQL repo
        # (PRIMARY KEY violation on knowledge_items.item_id) and leave
        # ChromaDB untouched on this attempt.
        with pytest.raises(Exception):
            await service.ingest_runbook(
                document_id=item_id,
                title="Duplicate",
                content="c2",
                organization_id=DEFAULT_ENTERPRISE_ID,
                scope="global",
            )

        assert service._index_document_in_vector_store.await_count == first_chroma_count

    def test_missing_session_factory_is_a_construction_error(self):
        """A DB-less KnowledgeService cannot be built at all (#899).

        This used to be a runtime refusal inside ``ingest_runbook``, which
        meant a misconfigured container composed cleanly, passed startup, and
        failed one runbook at a time during ``kb_seed`` (#894). Moving the
        refusal to construction turns that whole bug class into a TypeError at
        container init — before anything serves or seeds.
        """
        with pytest.raises(TypeError, match="db_session_factory"):
            KnowledgeService(
                knowledge_ingester=MagicMock(),
                sanitizer=MagicMock(),
                tracer=MagicMock(),
                vector_store=MagicMock(),
            )
