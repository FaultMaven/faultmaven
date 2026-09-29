"""Every KB chunk carries its row's ``enterprise_id`` — through every live writer.

#1168, slice 1 of 2. ChromaDB had no tenant dimension: cross-tenant isolation in
the vector layer was *derived* from the caller's identifiers plus one SQL
``WHERE`` behind the shared-id allowlist. This slice gives every KB chunk the
ADR-017 isolation key on WRITE; slice 2 (#1775) conjuncts it on read, and the
backfill of chunks written before this slice is #1777.

There is ONE live KB vector writer, ``KnowledgeService._index_document_in_vector_store``,
and it takes the owning enterprise as a required keyword from each of its four
callers. The stamp must be **the persisted row's own value** — never the ambient
request tenant — so every test here binds the ambient context to a DIFFERENT
enterprise than the row's. A caller that stamped ``get_current_enterprise_id()``
would write that other enterprise and fail here.

==========================================  ==============================================
indexer caller (knowledge_service.py)       test
==========================================  ==============================================
``ingest_runbook`` (authoring, conversion,  ``test_an_authored_runbook_...`` (both
suggestion acceptance, the pack)            enterprises) and ``test_a_pack_ingested_...``
``reindex_missing_vectors``                 ``test_a_repaired_row_is_restamped_...``
``update_document_metadata``                ``test_a_content_update_restamps_...``
``_resynchronise_after_failed_commit``      ``test_a_failed_commit_realigns_...``
==========================================  ==============================================

Everything below the service is real: SQLite rows written through the real
repository, and a real ChromaDB ``PersistentClient`` in ``tmp_path`` written
through the production ``KnowledgeVectorStore.add_documents`` — including its
refusal of an unstamped KB chunk. Only the embedding model is replaced, by a
fixed vector.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings

from faultmaven.bootstrap import kb_init
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.persistence.models import (
    Base,
    EnterpriseModel,
    KnowledgeItemModel,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

pytestmark = pytest.mark.integration

ENTERPRISE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ENTERPRISE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"

_DIM = 8
_VEC = [0.5] * _DIM

_BODY = (
    "# Connection pool exhaustion\n\n"
    "Symptoms: requests queue behind a saturated pool and time out.\n\n"
    "## Remediation\n\nRaise the pool ceiling, then find the leaking caller."
)


async def _fixed_embeddings(texts, **_kwargs):
    return [list(_VEC) for _ in texts]


class _PassthroughSanitizer:
    async def asanitize(self, value):
        return value


@pytest.fixture
async def session_factory():
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        for enterprise_id, slug in (
            (ENTERPRISE_A, "ent-a"),
            (ENTERPRISE_B, "ent-b"),
            (STANDALONE_ENTERPRISE_ID, "standalone"),
        ):
            session.add(
                EnterpriseModel(enterprise_id=enterprise_id, name=slug, slug=slug)
            )
        await session.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def chroma(tmp_path):
    """A real on-disk ChromaDB, private to this test (its own path)."""
    return chromadb.PersistentClient(
        path=str(tmp_path / "chroma"),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
    )


@pytest.fixture
def service(session_factory, chroma, tmp_path, monkeypatch) -> KnowledgeService:
    monkeypatch.chdir(tmp_path)
    return KnowledgeService(
        knowledge_ingester=MagicMock(),
        sanitizer=_PassthroughSanitizer(),
        tracer=MagicMock(),
        vector_store=KnowledgeVectorStore(chroma),
        db_session_factory=session_factory,
    )


@pytest.fixture(autouse=True)
def fixed_embedder():
    with patch(
        "faultmaven.infrastructure.embedding_guard.embed_texts_or_raise",
        new=_fixed_embeddings,
    ):
        yield


@pytest.fixture
def ambient_enterprise():
    """Bind the request tenant to ``value`` — always a DIFFERENT enterprise
    from the row under test, so a stamp read from the ambient context instead
    of the row is visible. Restored to the default afterwards."""

    def _bind(value: str) -> None:
        set_current_enterprise_id(value)

    yield _bind
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


def _chunks(chroma, parent_id: str) -> dict[str, Any]:
    """Every chunk of one parent, read straight from ChromaDB (id → metadata,
    document). A test-side read: the store itself exposes no metadata read."""
    got = chroma.get_collection(KB_COLLECTION).get(
        where={"parent_document_id": parent_id},
        include=["metadatas", "documents"],
    )
    return {
        chunk_id: (metadata, document)
        for chunk_id, metadata, document in zip(
            got["ids"], got["metadatas"], got["documents"]
        )
    }


def _stamps(chroma, parent_id: str) -> set:
    return {md.get("enterprise_id") for md, _ in _chunks(chroma, parent_id).values()}


async def _write_row(
    session_factory, *, item_id: str, enterprise_id: str, content: str
) -> None:
    async with session_factory() as session:
        session.add(
            KnowledgeItemModel(
                item_id=item_id,
                enterprise_id=enterprise_id,
                title="Connection pool exhaustion",
                content=content,
                item_type="runbook",
                scope="personal",
                owner_id=USER_B,
                is_published=True,
            )
        )
        await session.commit()


# =============================================================================
# ingest_runbook — authoring, conversion, suggestion acceptance, and the pack
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("author_enterprise", "ambient", "owner"),
    [(ENTERPRISE_A, ENTERPRISE_B, USER_A), (ENTERPRISE_B, ENTERPRISE_A, USER_B)],
    ids=["enterprise-a", "enterprise-b"],
)
async def test_an_authored_runbook_is_stamped_with_its_rows_enterprise(
    service,
    chroma,
    session_factory,
    ambient_enterprise,
    author_enterprise,
    ambient,
    owner,
):
    """Two enterprises, so the stamp cannot be a constant that happens to match."""
    ambient_enterprise(ambient)
    item_id = f"kb_authored_{author_enterprise[:4]}"

    chunks = await service.ingest_runbook(
        document_id=item_id,
        title="Connection pool exhaustion",
        content=_BODY,
        enterprise_id=author_enterprise,
        scope="personal",
        owner_id=owner,
    )

    assert chunks >= 1
    async with session_factory() as session:
        row = await DatabaseKnowledgeItemRepository(session).get_by_id(item_id)
    assert row.enterprise_id == author_enterprise  # the value the stamp must equal
    assert len(_chunks(chroma, item_id)) == chunks
    assert _stamps(chroma, item_id) == {author_enterprise}


def _write_pack(tmp_path: Path) -> tuple[Path, str]:
    """A one-runbook, two-chunk KB pack in the shipped format."""
    import numpy as np

    content = (
        '---\nid: stamp-probe\ntitle: "Stamp probe"\nscope: global\n---\n\n'
        "# Stamp probe\n\nBody.\n"
    )
    item_id = kb_init._item_id_from_runbook_id("stamp-probe")
    pack_dir = tmp_path / "pack"
    relpath = "global/stamp-probe.md"
    md_dest = pack_dir / "runbooks" / relpath
    md_dest.parent.mkdir(parents=True, exist_ok=True)
    md_dest.write_text(content, encoding="utf-8")
    np.savez(
        pack_dir / "vectors.npz",
        vectors=np.full((2, _DIM), 0.5, dtype=np.float32),
    )
    manifest = {
        "pack_format": 1,
        "version": "test",
        "model": "BAAI/bge-m3",
        "dim": _DIM,
        "runbooks": [
            {
                "item_id": item_id,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "title": "Stamp probe",
                "scope": "global",
                "relpath": relpath,
                "tags": [],
                "source_url": None,
                "owner_id": None,
                "team_id": None,
                "chunks": [
                    {"chunk_index": 0, "vector_row": 0, "text": "section one"},
                    {"chunk_index": 1, "vector_row": 1, "text": "section two"},
                ],
            }
        ],
    }
    (pack_dir / "pack.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir, item_id


@pytest.mark.asyncio
async def test_a_pack_ingested_runbook_carries_the_standalone_enterprise(
    service, chroma, session_factory, ambient_enterprise, tmp_path
):
    """The global tier's chunks carry the platform value their SQL row carries.

    That is the value #1775's read conjunct will accept for the global arm, and
    it is what every production pack ingestion passes (web-startup bootstrap,
    ``fm-reset-kb`` and the ``kb_seed`` job all hand ``bootstrap_kb``
    ``SingleTenantProvider.DEFAULT_ENTERPRISE_ID``).
    """
    ambient_enterprise(ENTERPRISE_A)
    pack_dir, item_id = _write_pack(tmp_path)

    result = await kb_init.bootstrap_kb(
        knowledge_service=service,
        db_session_factory=session_factory,
        enterprise_id=SingleTenantProvider.DEFAULT_ENTERPRISE_ID,
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.failed == [], result
    assert result.ingested == ["global/stamp-probe.md"], result
    chunks = _chunks(chroma, item_id)
    assert len(chunks) == 2
    assert {md["scope"] for md, _ in chunks.values()} == {"global"}
    assert _stamps(chroma, item_id) == {STANDALONE_ENTERPRISE_ID}


# =============================================================================
# reindex_missing_vectors — the boot repair of a row with no vectors
# =============================================================================


@pytest.mark.asyncio
async def test_a_repaired_row_is_restamped_from_the_row(
    service, chroma, session_factory, ambient_enterprise
):
    """Repair runs at boot with no request bound, so the row is the only
    honest source of the tenant — and here the ambient context says otherwise."""
    ambient_enterprise(ENTERPRISE_A)
    await _write_row(
        session_factory,
        item_id="kb_orphan_row",
        enterprise_id=ENTERPRISE_B,
        content=_BODY,
    )

    chunks = await service.reindex_missing_vectors("kb_orphan_row")

    assert chunks >= 1
    assert _stamps(chroma, "kb_orphan_row") == {ENTERPRISE_B}


# =============================================================================
# update_document_metadata — a content edit re-indexes the document
# =============================================================================


@pytest.mark.asyncio
async def test_a_content_update_restamps_from_the_row(
    service, chroma, session_factory, ambient_enterprise
):
    await _write_row(
        session_factory, item_id="kb_edited", enterprise_id=ENTERPRISE_B, content=_BODY
    )
    ambient_enterprise(ENTERPRISE_A)
    edited = _BODY + "\n\n## Verification\n\nThe queue drains within a minute."

    result = await service.update_document_metadata("kb_edited", content=edited)

    assert result is not None and result["content"] == edited
    chunks = _chunks(chroma, "kb_edited")
    # The vectors were REPLACED by this update (the edit is in them) ...
    assert any("queue drains" in document for _, document in chunks.values())
    # ... and every replacement carries the row's enterprise, not the ambient one.
    assert _stamps(chroma, "kb_edited") == {ENTERPRISE_B}


# =============================================================================
# _resynchronise_after_failed_commit — realigning vectors to the observed row
# =============================================================================


@pytest.mark.asyncio
async def test_a_failed_commit_realigns_the_vectors_stamped_from_the_observed_row(
    service, chroma, session_factory, ambient_enterprise
):
    """Driven through the path that runs it: the update re-indexes, its commit
    fails, and the compensation re-reads the row (still the previous content)
    and re-indexes it AS OBSERVED — stamping the observed row's enterprise."""
    await _write_row(
        session_factory,
        item_id="kb_realigned",
        enterprise_id=ENTERPRISE_B,
        content=_BODY,
    )
    ambient_enterprise(ENTERPRISE_A)
    edited = _BODY + "\n\n## Verification\n\nThe queue drains within a minute."

    with patch.object(
        DatabaseKnowledgeItemRepository,
        "update",
        side_effect=RuntimeError("commit failed"),
    ):
        with pytest.raises(RuntimeError, match="commit failed"):
            await service.update_document_metadata("kb_realigned", content=edited)

    chunks = _chunks(chroma, "kb_realigned")
    assert chunks, "the compensation left the document with no vectors"
    # Realigned to the row as observed (the edit never committed) ...
    assert not any("queue drains" in document for _, document in chunks.values())
    # ... and stamped from that observed row.
    assert _stamps(chroma, "kb_realigned") == {ENTERPRISE_B}
