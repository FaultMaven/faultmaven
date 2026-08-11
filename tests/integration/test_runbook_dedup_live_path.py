"""The central fm#1030 claim, end-to-end at the wire.

A runbook published through the LIVE path — ``KnowledgeService.ingest_runbook``,
the one method all three production publishers call (the shipped pack, uploads,
and the case-conversion flywheel) — is found by runbook dedup afterwards.

Before fm#1030 this round trip was structurally impossible: dedup filtered on
``report_type == "runbook"``, a key no live writer stamped, so the query could
only return ``[]`` no matter what the KB held. These tests run BOTH halves for
real — the SQL row goes into a real (in-memory) database, the chunks into a
real ephemeral ChromaDB through the production ``add_documents`` normalization,
and the search is the production ``RunbookKnowledgeBase`` read path. Only the
embedding MODEL is stubbed (through its own guard seam, returning toy vectors
in one consistent space): BGE-M3 is a 2GB download and the property under test
is metadata round-trip + scope filtering, which is embedding-space agnostic.

Negative controls live in the same tests as the positives, so "no duplicate
found" can never pass vacuously: each test first proves the feature live (the
entitled principal DOES find the runbook), then asserts the exclusions.
"""

import contextlib
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.persistence.chromadb_store import ChromaDBVectorStore
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
    build_kb_scope_filter,
)

pytestmark = [pytest.mark.integration]

_DIM = 8
_TOY_EMBEDDING = [0.1] * _DIM

_RUNBOOK_MD = """# Runbook: Connection pool exhaustion

## Symptoms
Database queries time out under load; the pool reports no free connections.

## Resolution
Raise the pool timeout to 30s and cap per-worker connections.
"""


def _real_chroma_store(name: str) -> ChromaDBVectorStore:
    """The production vector store over a real in-process ChromaDB.

    Settings pinned — a bare ``EphemeralClient()`` is order-fragile (#823):
    chromadb caches one System per identifier and refuses a second client
    whose ``Settings`` differ in any field, and ``Settings.environment``
    defaults to the ambient ENVIRONMENT variable other tests mutate.
    """
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    return ChromaDBVectorStore(
        chromadb.EphemeralClient(
            settings=ChromaSettings(
                anonymized_telemetry=False,
                allow_reset=False,
                environment="",
                is_persistent=False,
            )
        ),
        collection_name=name,
    )


@pytest.fixture()
async def db_session_factory():
    """A real async-SQLAlchemy session factory over in-memory SQLite.

    FK pragmas are left at SQLite's default (OFF), matching the standalone
    runtime — the property under test is the vector round trip, not relational
    integrity.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    base_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    @contextlib.asynccontextmanager
    async def factory():
        async with base_factory() as session:
            yield session

    yield factory
    await engine.dispose()


def _knowledge_service(
    store: ChromaDBVectorStore, db_session_factory
) -> KnowledgeService:
    return KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=MagicMock(),
        tracer=MagicMock(),
        vector_store=store,
        db_session_factory=db_session_factory,
    )


def _toy_embed_texts(texts: List[str], **_kwargs) -> List[List[float]]:
    return [list(_TOY_EMBEDDING) for _ in texts]


@pytest.mark.asyncio
async def test_a_published_runbook_is_found_by_dedup_and_only_within_its_scope(
    db_session_factory, request
):
    """Publish through the LIVE upload/flywheel leg, then find it via dedup.

    This is the whole issue: ``ingest_runbook`` (no ``prechunked`` — the
    chunker and the per-chunk metadata construction run for real) followed by
    the production ``search_by_text``. The same test carries the negative
    controls: another user's scope does NOT surface the personal runbook, and
    a dissimilar query finds nothing — both meaningful only because the
    positive arm just proved the feature live.
    """
    store = _real_chroma_store("kb_live_path_scoped")
    service = _knowledge_service(store, db_session_factory)

    with patch(
        "faultmaven.infrastructure.embedding_guard.embed_texts_or_raise",
        new=AsyncMock(side_effect=_toy_embed_texts),
    ):
        chunks_created = await service.ingest_runbook(
            document_id="kb-pool-exhaustion",
            title="Runbook: Connection pool exhaustion",
            content=_RUNBOOK_MD,
            organization_id="org-1",
            document_type="runbook",
            tags=["postgresql", "database"],
            scope="personal",
            owner_id="user-a",
            verified_by="user-a",
        )

    assert chunks_created > 0, "the live writer indexed nothing — nothing to find"

    kb = RunbookKnowledgeBase(vector_store=store)

    with patch(
        "faultmaven.infrastructure.knowledge.runbook_kb.embed_query_or_raise",
        new=AsyncMock(return_value=list(_TOY_EMBEDDING)),
    ):
        # POSITIVE — the owner's scope finds the runbook it just published.
        found = await kb.search_by_text(
            query_text="database connection pool exhausted, queries time out",
            scope_filter=build_kb_scope_filter("user-a", []),
        )

        assert [m.item_id for m in found] == ["kb-pool-exhaustion"]
        assert found[0].title == "Runbook: Connection pool exhaustion"
        assert found[0].scope == "personal"
        assert found[0].similarity_score > 0.9

        # NEGATIVE — another user's scope must not surface it. Cannot pass
        # vacuously: the positive arm above proved the search finds this row.
        assert (
            await kb.search_by_text(
                query_text="database connection pool exhausted, queries time out",
                scope_filter=build_kb_scope_filter("user-b", []),
            )
            == []
        ), "a personal runbook leaked into another user's dedup scope"

        # ...unless it is team-shared to them (the parent_document_id arm).
        shared = await kb.search_by_text(
            query_text="database connection pool exhausted, queries time out",
            scope_filter=build_kb_scope_filter("user-b", ["kb-pool-exhaustion"]),
        )
        assert [m.item_id for m in shared] == ["kb-pool-exhaustion"]

    # NEGATIVE — a dissimilar query finds nothing, in the same proven-live
    # collection.
    with patch(
        "faultmaven.infrastructure.knowledge.runbook_kb.embed_query_or_raise",
        new=AsyncMock(return_value=[5.0] * _DIM),
    ):
        assert (
            await kb.search_by_text(
                query_text="entirely unrelated topic",
                scope_filter=build_kb_scope_filter("user-a", []),
            )
            == []
        )


@pytest.mark.asyncio
async def test_a_pack_published_global_runbook_is_found_by_every_principal(
    db_session_factory,
):
    """The pack/boot leg: ``ingest_runbook(prechunked=...)`` — build-time
    chunks and vectors, no chunker, no embedding model — lands rows that the
    dedup read path finds for any principal (the global arm)."""
    store = _real_chroma_store("kb_live_path_pack")
    service = _knowledge_service(store, db_session_factory)

    chunks_created = await service.ingest_runbook(
        document_id="kb-shipped",
        title="Runbook: OOMKilled pods after deploy",
        content="# Runbook: OOMKilled pods after deploy\n\nRaise the limit.",
        organization_id="org-ignored-for-global",
        document_type="runbook",
        scope="global",
        prechunked=[
            ("# Runbook: OOMKilled pods after deploy", list(_TOY_EMBEDDING)),
            ("Raise the limit.", [0.12] * _DIM),
        ],
    )
    assert chunks_created == 2

    kb = RunbookKnowledgeBase(vector_store=store)

    for principal in ("user-a", None):
        matches = await kb.search_runbooks(
            query_embedding=list(_TOY_EMBEDDING),
            scope_filter=build_kb_scope_filter(principal, []),
        )
        # Two chunks, ONE runbook: the chunk collapse holds on live-written
        # rows too.
        assert [m.item_id for m in matches] == ["kb-shipped"]
        assert matches[0].title == "Runbook: OOMKilled pods after deploy"
        assert matches[0].scope == "global"
