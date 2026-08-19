"""Unit tests for faultmaven.bootstrap.kb_init.

Covers the atomicity + idempotency guarantees the bootstrap was created
to enforce. Uses fakes for KnowledgeService and the DB session factory
so the tests run without ChromaDB or a real database — the bootstrap's
contract is fully describable in terms of those collaborators.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.bootstrap import kb_init
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    chunk_stamp_identity,
)

RUNBOOK_MD = """---
id: example-runbook
title: "Example Runbook"
scope: global
tags: [postgres, latency]
---

# Body content for tests.
"""


def _write_pack(
    tmp_path: Path,
    *,
    content: str = RUNBOOK_MD,
    title: str = "Example Runbook",
    scope: str = "global",
    relpath: str = "global/example.md",
    chunk_texts: tuple = ("section one", "section two"),
    dim: int = 8,
    causes: Optional[list] = None,
    model: str = "BAAI/bge-m3",
    chunk_indices: Optional[tuple] = None,
) -> Path:
    """Write a minimal but valid KB pack under ``tmp_path/pack`` and return it.

    Vectors are zeros (shape only matters for the loader); chunk texts +
    content_hash are what the bootstrap asserts on.
    """
    import numpy as np

    item_id = kb_init._item_id_from_runbook_id("example-runbook")
    pack_dir = tmp_path / "pack"
    md_dest = pack_dir / "runbooks" / relpath
    md_dest.parent.mkdir(parents=True, exist_ok=True)
    md_dest.write_text(content, encoding="utf-8")

    np.savez(
        pack_dir / "vectors.npz",
        vectors=np.zeros((len(chunk_texts), dim), dtype=np.float32),
    )
    indices = (
        chunk_indices if chunk_indices is not None else tuple(range(len(chunk_texts)))
    )
    manifest = {
        "pack_format": 1,
        "version": "test",
        "model": model,
        "dim": dim,
        "runbooks": [
            {
                "item_id": item_id,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "title": title,
                "scope": scope,
                "relpath": relpath,
                "tags": ["postgres", "latency"],
                "source_url": None,
                "owner_id": None,
                "team_id": None,
                "chunks": [
                    {"chunk_index": ci, "vector_row": i, "text": t}
                    for i, (ci, t) in enumerate(zip(indices, chunk_texts))
                ],
            }
        ],
    }
    if causes is not None:
        manifest["runbooks"][0]["causes"] = causes
    (pack_dir / "pack.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pack_dir


class FakeSession:
    """Async-context-manager fake that returns no existing rows."""

    def __init__(self, existing_row: Optional[object] = None) -> None:
        self._existing = existing_row

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def execute(self, _stmt):
        scalar_mock = MagicMock()
        scalar_mock.scalar_one_or_none = MagicMock(return_value=self._existing)
        return scalar_mock

    async def delete(self, _row):
        return None

    async def commit(self):
        return None


def _make_session_factory(existing_row=None):
    def factory():
        return FakeSession(existing_row)

    return factory


@pytest.mark.asyncio
async def test_bootstrap_ingests_new_runbook(tmp_path: Path):
    """A fresh pack runbook (no DB row) is ingested, with the pack's pre-chunked
    text + vectors passed straight through to ingest_runbook."""
    pack_dir = _write_pack(tmp_path)
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=2)

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == ["global/example.md"]
    assert result.skipped_unchanged == []
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_awaited_once()
    kw = knowledge_service.ingest_runbook.await_args.kwargs
    assert kw["document_id"] == kb_init._item_id_from_runbook_id("example-runbook")
    assert kw["title"] == "Example Runbook"
    # The pack's (text, vector) pairs are forwarded verbatim — vectors are the
    # zeros written by _write_pack (dim 8).
    assert kw["prechunked"] == [
        ("section one", [0.0] * 8),
        ("section two", [0.0] * 8),
    ]
    # Platform-shipped runbooks carry COMMUNITY trust via verification_level,
    # NOT a fake verified_by (an FK to users.user_id — real user or NULL; #378).
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )

    assert kw["verified_by"] is None
    assert kw["verification_level"] == VerificationLevel.COMMUNITY


@pytest.mark.asyncio
async def test_bootstrap_never_loads_model(tmp_path: Path):
    """Ingesting a pack NEVER loads BGE-M3 — the fast, model-free boot path."""
    pack_dir = _write_pack(tmp_path)
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=2)

    with patch("faultmaven.infrastructure.model_cache.model_cache") as model_cache_mock:
        result = await kb_init.bootstrap_kb(
            knowledge_service=knowledge_service,
            db_session_factory=_make_session_factory(existing_row=None),
            organization_id="org-test",
            project_root=tmp_path,
            pack_dir=pack_dir,
        )

    model_cache_mock.get_bge_m3_model.assert_not_called()
    assert result.ingested == ["global/example.md"]


@pytest.mark.asyncio
async def test_bootstrap_skips_unchanged_runbook(tmp_path: Path):
    """An existing row whose content hash equals the pack's is skipped."""
    pack_dir = _write_pack(tmp_path)
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=2)

    existing = MagicMock()
    existing.content = RUNBOOK_MD  # identical to the pack's runbook content
    # ...and stamped by this code, which is the other half of "unchanged".
    existing.knowledge_metadata = {"chunk_stamp": chunk_stamp_identity()}

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == []
    assert result.skipped_unchanged == ["global/example.md"]
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_re_ingests_changed_runbook(tmp_path: Path):
    """Content drift (existing row != pack content) triggers delete + re-create."""
    pack_dir = _write_pack(tmp_path)
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=2)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    existing = MagicMock()
    existing.content = "STALE CONTENT — does not match the pack"

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == ["global/example.md"]
    assert result.skipped_unchanged == []
    knowledge_service.ingest_runbook.assert_awaited_once()
    # Stale chunks cleaned out of ChromaDB before re-ingest.
    knowledge_service._vector_store.delete_documents_by_parent_id.assert_awaited()


@pytest.mark.asyncio
async def test_bootstrap_re_ingests_on_causes_change_with_unchanged_markdown(
    tmp_path: Path,
):
    """A causes-only pack change (markdown byte-identical) must re-ingest.

    The content hash covers only the markdown; without a causes comparison the
    stale structured record would survive and the live consumer (KB cause
    seeder) would read it. So drift in metadata["causes"] alone forces re-ingest.
    """
    pack_causes = [{"cause_letter": "A", "cause_name": "New cause"}]
    pack_dir = _write_pack(tmp_path, causes=pack_causes)
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=2)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    existing = MagicMock()
    existing.content = RUNBOOK_MD  # markdown unchanged → content hash matches
    existing.knowledge_metadata = {
        "causes": [{"cause_letter": "A", "cause_name": "OLD cause"}],
        "chunk_stamp": chunk_stamp_identity(),
    }

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == ["global/example.md"]
    assert result.skipped_unchanged == []
    knowledge_service.ingest_runbook.assert_awaited_once()
    knowledge_service._vector_store.delete_documents_by_parent_id.assert_awaited()


# ``knowledge_items.metadata`` is ``JsonBlob`` =
# ``Text().with_variant(JSONB, "postgresql")``. Rows written by
# ``KnowledgeItemRepository`` read back as a *str* on BOTH backends (it binds an
# already-serialized ``json.dumps(...)``; JSONB stores that as a JSON string
# scalar), while a writer binding a real object yields a *dict*. Both shapes must
# reach the same verdict, or the idempotency comparison silently degrades — it
# did: reading only the dict shape meant every runbook re-ingested on every boot.
# Parameterising by the STORED SHAPE is the point; a dict-only fixture asserts a
# shape the production writer never produces.
CAUSES_RECORD = [{"cause_letter": "A", "cause_name": "Same cause"}]

# fm#1108 added a THIRD idempotency axis beside content hash and causes: the
# identity of the chunk stamp (schema version + cause-heading grammar) the
# chunks were written with. A row carrying no stamp — or a stale one — is not
# "unchanged", it is content whose stored join key no longer means what it says,
# so it must re-ingest. These fixtures therefore carry the CURRENT identity to
# keep meaning "already ingested by this code".
CURRENT_STAMP = chunk_stamp_identity()
METADATA_SHAPES = [
    pytest.param(
        {"causes": CAUSES_RECORD, "chunk_stamp": CURRENT_STAMP},
        id="postgresql-jsonb-dict",
    ),
    pytest.param(
        json.dumps({"causes": CAUSES_RECORD, "chunk_stamp": CURRENT_STAMP}),
        id="sqlite-text-json-string",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_metadata", METADATA_SHAPES)
async def test_bootstrap_skips_when_causes_match_in_either_metadata_shape(
    tmp_path: Path, stored_metadata
):
    """Matching causes must skip regardless of how the column deserialises.

    Guards the SQLite half specifically: a JSON-string metadata value that
    decodes to the pack's causes is UNCHANGED, so the runbook must be skipped —
    not deleted and rewritten on every boot.
    """
    pack_dir = _write_pack(tmp_path, causes=CAUSES_RECORD)
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=2)

    existing = MagicMock()
    existing.content = RUNBOOK_MD  # markdown unchanged → content hash matches
    existing.knowledge_metadata = stored_metadata

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.skipped_unchanged == ["global/example.md"]
    assert result.ingested == []
    knowledge_service.ingest_runbook.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_metadata",
    [
        pytest.param(
            {
                "causes": [{"cause_letter": "A", "cause_name": "OLD cause"}],
                "chunk_stamp": CURRENT_STAMP,
            },
            id="postgresql-jsonb-dict",
        ),
        pytest.param(
            json.dumps(
                {
                    "causes": [{"cause_letter": "A", "cause_name": "OLD cause"}],
                    "chunk_stamp": CURRENT_STAMP,
                }
            ),
            id="sqlite-text-json-string",
        ),
    ],
)
async def test_bootstrap_re_ingests_on_causes_drift_in_either_metadata_shape(
    tmp_path: Path, stored_metadata
):
    """The converse of the skip: genuine causes drift still re-ingests in both
    shapes, so the fix cannot have turned the comparison into a constant."""
    pack_dir = _write_pack(tmp_path, causes=CAUSES_RECORD)
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=2)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    existing = MagicMock()
    existing.content = RUNBOOK_MD
    existing.knowledge_metadata = stored_metadata

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == ["global/example.md"]
    assert result.skipped_unchanged == []
    # Stale chunks must leave ChromaDB before the re-ingest, on both shapes.
    knowledge_service._vector_store.delete_documents_by_parent_id.assert_awaited()


@pytest.mark.parametrize(
    "value,expected",
    [
        ({"causes": CAUSES_RECORD}, {"causes": CAUSES_RECORD}),
        (json.dumps({"causes": CAUSES_RECORD}), {"causes": CAUSES_RECORD}),
        (None, {}),
        ("", {}),
        ("not json at all", {}),
        ("[1, 2, 3]", {}),  # valid JSON, wrong container
        (12345, {}),  # non-str, non-dict
    ],
)
def test_decode_metadata_normalises_every_stored_shape(value, expected):
    """``_decode_metadata`` always yields a dict, so the caller's ``.get`` is
    safe for absent, malformed, and wrong-container values alike."""
    assert kb_init._decode_metadata(value) == expected


@pytest.mark.asyncio
async def test_second_bootstrap_over_real_sqlite_skips_everything(
    fk_on_ingest_service, tmp_path: Path
):
    """Two bootstraps over a REAL SQLite session: the second must skip.

    The shape-parameterised tests above encode the SQLite representation as a
    hand-written constant; this one DERIVES it, by writing through the real
    ``KnowledgeService`` and reading back through the real ORM. That is what the
    prior fixtures could not do — they set ``knowledge_metadata`` to a dict (the
    PostgreSQL shape), and the persistence-side causes test read back through
    ``DatabaseKnowledgeItemRepository``, which parses the JSON. Neither ever put
    a real SQLite row in front of ``kb_init``, which is how a permanently-false
    ``causes_unchanged`` shipped.

    Pinning the round trip rather than the representation also means a future
    change to the ``JsonBlob`` variant (or a third backend) fails here instead of
    silently re-ingesting the whole KB on every boot.
    """
    svc, factory = fk_on_ingest_service
    pack_dir = _write_pack(tmp_path, causes=CAUSES_RECORD)

    first = await kb_init.bootstrap_kb(
        knowledge_service=svc,
        db_session_factory=factory,
        organization_id="org-1",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )
    assert first.ingested == ["global/example.md"], first
    assert first.failed == []

    second = await kb_init.bootstrap_kb(
        knowledge_service=svc,
        db_session_factory=factory,
        organization_id="org-1",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )
    assert second.skipped_unchanged == ["global/example.md"], second
    assert second.ingested == []
    assert second.pruned == []


@pytest.mark.asyncio
async def test_bootstrap_raises_on_zero_chunks(tmp_path: Path):
    """If ingest_runbook reports 0 chunks, the runbook is recorded as failed
    and the orphan-cleanup path runs."""
    pack_dir = _write_pack(tmp_path)
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=0)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == []
    assert len(result.failed) == 1
    failed_file, reason = result.failed[0]
    assert failed_file == "global/example.md"
    assert "0 chunks" in reason


@pytest.mark.parametrize(
    "bad_indices",
    [
        (0, 2, 1),  # out of order
        (0, 1, 3),  # gap (missing 2)
        (0, 1, 1),  # duplicate index
        (1, 2, 3),  # does not start at 0
    ],
)
@pytest.mark.asyncio
async def test_bootstrap_fails_runbook_on_noncanonical_chunk_indices(
    tmp_path: Path, bad_indices
):
    """A pack whose chunk_index list is not contiguous 0..n-1 in list order is
    rejected loudly for that runbook (recorded as failed, not ingested) — the
    ingest path re-derives ids by list position, so a misordered pack would
    silently misalign chunk ids from the manifest. ingest_runbook is never
    reached for the malformed runbook."""
    pack_dir = _write_pack(
        tmp_path,
        chunk_texts=("a", "b", "c"),
        chunk_indices=bad_indices,
    )
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=3)

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == []
    assert len(result.failed) == 1
    failed_file, reason = result.failed[0]
    assert failed_file == "global/example.md"
    assert "chunk ordering invalid" in reason
    knowledge_service.ingest_runbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_accepts_canonical_chunk_indices(tmp_path: Path):
    """The ordering guard is transparent to a well-formed pack (0..n-1 in order)."""
    pack_dir = _write_pack(
        tmp_path, chunk_texts=("a", "b", "c"), chunk_indices=(0, 1, 2)
    )
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=3)

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == ["global/example.md"]
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_empty_chunks_update_does_not_delete_existing_row(
    tmp_path: Path,
):
    """A runbook with an EMPTY chunk list can never be retrieved. It passes the
    ordering check vacuously ([] == range(0)), so it must be rejected explicitly
    BEFORE the re-ingest delete — otherwise a content-changed update would delete
    the previously-good row and only then trip the downstream zero-chunks check,
    losing the row (the 'no silently-unretrievable runbook' invariant)."""
    pack_dir = _write_pack(
        tmp_path,
        content="# Updated body (content changed since last ingest)\n",
        chunk_texts=(),  # no chunks
    )
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=0)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    existing = MagicMock()
    existing.content = "# STALE body — differs from the updated pack content\n"

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == []
    assert len(result.failed) == 1
    assert "no chunks" in result.failed[0][1]
    # Rejected before the delete → the existing good row/vectors survive.
    knowledge_service._vector_store.delete_documents_by_parent_id.assert_not_awaited()
    knowledge_service.ingest_runbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_malformed_update_does_not_delete_existing_row(tmp_path: Path):
    """Fail-safe placement: a malformed pack UPDATE of an existing runbook is
    rejected BEFORE the re-ingest delete, so the previously-good row is left
    intact rather than deleted-with-nothing-behind (the 'no silently-unretrievable
    runbook' invariant). The guard runs first, so _delete_existing never fires."""
    pack_dir = _write_pack(
        tmp_path,
        content="# Updated body (content changed since last ingest)\n",
        chunk_texts=("a", "b", "c"),
        chunk_indices=(0, 2, 1),  # malformed update
    )
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=3)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    existing = MagicMock()
    existing.content = "# STALE body — differs from the updated pack content\n"

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == []
    assert len(result.failed) == 1
    assert "chunk ordering invalid" in result.failed[0][1]
    # The existing good row was NOT deleted (guard rejected before re-ingest).
    knowledge_service._vector_store.delete_documents_by_parent_id.assert_not_awaited()
    knowledge_service.ingest_runbook.assert_not_awaited()


@pytest.mark.asyncio
async def test_bootstrap_no_pack_is_safe(tmp_path: Path):
    """If no pack is present, bootstrap returns an empty result without
    attempting ingestion or raising."""
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=2)

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=None),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=tmp_path / "no-such-pack",
    )

    assert result.ingested == []
    assert result.skipped_unchanged == []
    assert result.failed == []
    knowledge_service.ingest_runbook.assert_not_awaited()


def test_item_id_from_runbook_id_is_deterministic():
    assert kb_init._item_id_from_runbook_id("foo") == kb_init._item_id_from_runbook_id(
        "foo"
    )
    assert kb_init._item_id_from_runbook_id("foo") != kb_init._item_id_from_runbook_id(
        "bar"
    )
    assert kb_init._item_id_from_runbook_id("foo").startswith("kb_")


# ============================================================
# FK-ON regression: the composition seam #378 + kb_init broke
# ============================================================
#
# The unit tests above use FakeSession, so the FK on
# knowledge_items.verified_by → users.user_id is never exercised. That is
# exactly the dynamic-drift seam: #378 (foreign_keys=ON) and kb_init
# (verified_by="system") were each locally fine; only their composition
# failed — every shipped-runbook ingest hit "FOREIGN KEY constraint
# failed" because no users row has user_id="system".
#
# This test drives the REAL ingest_runbook SQL path against an in-memory
# SQLite with foreign_keys=ON and a seeded enterprise+org but NO "system"
# user — the fresh-install shape — and asserts a clean ingest. A negative
# control proves the FK is actually enforced in the test (so a green run
# isn't a false pass).


@pytest.fixture
async def fk_on_ingest_service():
    """Real KnowledgeService.ingest_runbook over FK-on SQLite.

    ChromaDB is the only thing stubbed (``_index_document_in_vector_store``
    returns a positive chunk count); the relational write — where the FK
    fires — is real.
    """
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from faultmaven.infrastructure.persistence.models import Base
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # replicates the #378 connect listener
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Seed enterprise + org so knowledge_items.organization_id FK is
        # satisfiable. Deliberately seed NO "system" user.
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

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = factory
    svc._index_document_in_vector_store = AsyncMock(return_value=3)
    svc._delete_knowledge_item_row = AsyncMock()

    yield svc, factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_ingest_runbook_succeeds_with_fk_enforced_fresh_install(
    fk_on_ingest_service,
):
    """The fix: verified_by=None + verification_level=COMMUNITY ingests
    cleanly under foreign_keys=ON with no 'system' user present."""
    from sqlalchemy import text

    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )

    svc, factory = fk_on_ingest_service

    chunks = await svc.ingest_runbook(
        document_id="kb_fkprobe",
        title="Probe Runbook",
        content="# body",
        organization_id="org-1",
        scope="global",
        verified_by=None,
        verification_level=VerificationLevel.COMMUNITY,
    )
    assert chunks == 3

    async with factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT verified_by, verification_level "
                    "FROM knowledge_items WHERE item_id='kb_fkprobe'"
                )
            )
        ).first()
    assert row is not None  # the row actually persisted (no FK rollback)
    assert row[0] is None  # verified_by NULL — never a sentinel
    assert row[1] == int(VerificationLevel.COMMUNITY)  # COMMUNITY trust kept


@pytest.mark.asyncio
async def test_ingest_runbook_with_sentinel_verified_by_fails_fk(
    fk_on_ingest_service,
):
    """Negative control: the old behaviour (verified_by='system') still
    violates the FK — proving foreign_keys=ON is genuinely active in this
    test, so the positive assertion above isn't a false pass."""
    svc, _ = fk_on_ingest_service

    with pytest.raises(Exception) as exc_info:
        await svc.ingest_runbook(
            document_id="kb_sentinel",
            title="Sentinel Runbook",
            content="# body",
            organization_id="org-1",
            scope="global",
            verified_by="system",  # no such users row → FK violation
        )
    assert "foreign key" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_ingest_runbook_coerces_numeric_tags_for_both_models(
    fk_on_ingest_service,
):
    """A numeric YAML tag (e.g. 503) must not break ingest at EITHER the
    relational KnowledgeItem OR the Pydantic KnowledgeBaseDocument.

    Regression: KnowledgeItem.__post_init__ coercion (#394) was not enough —
    ingest_runbook also builds a KnowledgeBaseDocument (List[str] tags, no
    coercion hook) which rejected the int and failed the whole ingest
    (unknown-case-260526-4.md, failed=1 after #394).
    """
    from sqlalchemy import text

    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )

    svc, factory = fk_on_ingest_service

    # Must not raise (pre-fix: ValidationError on KnowledgeBaseDocument.tags).
    chunks = await svc.ingest_runbook(
        document_id="kb_numtag",
        title="Numeric Tag Runbook",
        content="# body",
        organization_id="org-1",
        scope="global",
        tags=["istio", 503, "envoy"],
        verified_by=None,
        verification_level=VerificationLevel.COMMUNITY,
    )
    assert chunks == 3

    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT tags FROM knowledge_items WHERE item_id='kb_numtag'")
            )
        ).first()
    assert row is not None  # persisted (no rollback)
    assert "503" in row[0]  # numeric tag coerced to "503"


class TestPruneOrphanBuiltins:
    """Bootstrap prunes built-in rows whose runbook id no longer maps to a
    file on disk (id-churn orphans), without touching authored/uuid items.

    Regression: id churn between runs left stale kb_<hash> rows (59 orphans
    vs 60 files in eval 2026-06-01); the dashboard double-listed once the
    Runbooks tab read knowledge_items.
    """

    @pytest.mark.asyncio
    async def test_prunes_builtin_orphan_not_on_disk(self):
        deleted: list[str] = []

        async def fake_delete(item_id, _svc, _factory):
            deleted.append(item_id)

        keep = {"kb_aaaaaaaaaaaa"}  # the one current on-disk runbook
        all_ids = [
            "kb_aaaaaaaaaaaa",  # on disk → keep
            "kb_bbbbbbbbbbbb",  # built-in, NOT on disk → prune
            "550e8400-e29b-41d4-a716-446655440000",  # authored uuid → never touch
        ]

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def execute(self, _stmt):
                return MagicMock(all=lambda: [(i,) for i in all_ids])

        with patch.object(kb_init, "_delete_existing", side_effect=fake_delete):
            pruned = await kb_init._prune_orphan_builtins(
                keep,
                knowledge_service=MagicMock(),
                db_session_factory=lambda: _Session(),
            )

        assert pruned == ["kb_bbbbbbbbbbbb"]
        assert deleted == ["kb_bbbbbbbbbbbb"]  # uuid + kept id untouched

    @pytest.mark.asyncio
    async def test_empty_keepset_prunes_nothing(self):
        """Safety guard: empty keep-set (disk/parse anomaly) removes nothing."""
        with patch.object(kb_init, "_delete_existing") as del_mock:
            pruned = await kb_init._prune_orphan_builtins(
                set(), knowledge_service=MagicMock(), db_session_factory=MagicMock()
            )
        assert pruned == []
        del_mock.assert_not_called()


class TestReconcileVectors:
    """SQL <-> ChromaDB reconcile pass.

    The row-keyed prune can only ever delete a row plus its own vectors, so it is
    structurally blind to vectors whose row is already gone. Those orphans
    accumulated (210 vectors vs 91 rows in the 2026-06 KB drift) and silently
    killed runbook-cause matches when a ghost vector landed as the top KB hit.
    This pass compares both sides and cleans the index.
    """

    @staticmethod
    def _service(*, chroma_parents, delete_batches):
        async def _list():
            return set(chroma_parents)

        async def _delete_batch(parent_ids, collection_name=None):
            ids = list(parent_ids)
            delete_batches.append(ids)
            return 3 * len(ids)  # pretend 3 chunks per parent

        vector_store = MagicMock()
        vector_store.list_parent_document_ids = AsyncMock(side_effect=_list)
        vector_store.delete_documents_by_parents = AsyncMock(side_effect=_delete_batch)
        svc = MagicMock()
        svc._vector_store = vector_store
        return svc

    @staticmethod
    def _factory(db_ids):
        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def execute(self, _stmt):
                return MagicMock(all=lambda: [(i,) for i in db_ids])

        return lambda: _Session()

    @pytest.mark.asyncio
    async def test_deletes_orphaned_builtin_vectors_in_one_batch(self):
        """Built-in vector parents with no DB row are deleted in a single batch."""
        delete_batches: list[list[str]] = []
        svc = self._service(
            chroma_parents={"kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb", "kb_cccccccccccc"},
            delete_batches=delete_batches,
        )
        # DB has only a and b → c is an orphaned built-in vector.
        cleaned, orphaned_rows = await kb_init._reconcile_vectors(
            svc, self._factory({"kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb"})
        )
        assert cleaned == ["kb_cccccccccccc"]
        assert delete_batches == [["kb_cccccccccccc"]]  # ONE batched call
        assert orphaned_rows == []

    @pytest.mark.asyncio
    async def test_never_deletes_non_builtin_orphans(self):
        """CRITICAL (RLS/tenancy): an orphaned vector whose parent is NOT a built-in
        id (authored kb_<16hex> or a uuid — possibly another tenant's, invisible to
        the RLS-scoped row query) is NEVER deleted. db_ids is one org's rows; the
        chroma collection is shared, so unbounded deletion would wipe other tenants."""
        delete_batches: list[list[str]] = []
        svc = self._service(
            chroma_parents={
                "kb_aaaaaaaaaaaa",  # built-in, in DB → keep
                "kb_deadbeefcafe1234",  # authored 16-hex orphan → MUST NOT delete
                "550e8400-e29b-41d4-a716-446655440000",  # uuid orphan → MUST NOT delete
            },
            delete_batches=delete_batches,
        )
        cleaned, orphaned_rows = await kb_init._reconcile_vectors(
            svc, self._factory({"kb_aaaaaaaaaaaa"})
        )
        assert cleaned == []
        assert delete_batches == []  # nothing deleted — blast radius bounded
        assert orphaned_rows == []

    @pytest.mark.asyncio
    async def test_warns_orphaned_rows_without_deleting(self):
        """A DB row with no vectors is reported, never deleted (can't re-embed here)."""
        delete_batches: list[list[str]] = []
        svc = self._service(
            chroma_parents={"kb_aaaaaaaaaaaa"}, delete_batches=delete_batches
        )
        cleaned, orphaned_rows = await kb_init._reconcile_vectors(
            svc, self._factory({"kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb"})
        )
        assert cleaned == []
        assert delete_batches == []
        assert orphaned_rows == ["kb_bbbbbbbbbbbb"]

    @pytest.mark.asyncio
    async def test_consistent_index_is_noop(self):
        delete_batches: list[list[str]] = []
        svc = self._service(
            chroma_parents={"kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb"},
            delete_batches=delete_batches,
        )
        cleaned, orphaned_rows = await kb_init._reconcile_vectors(
            svc, self._factory({"kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb"})
        )
        assert cleaned == []
        assert orphaned_rows == []
        assert delete_batches == []

    @pytest.mark.asyncio
    async def test_empty_db_refuses_to_delete_vectors(self):
        """Zero rows + non-empty index = transient row-write failure, NOT a wipe
        signal. Refuse to delete (mirrors the prune's empty-keepset guard)."""
        delete_batches: list[list[str]] = []
        svc = self._service(
            chroma_parents={"kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb"},
            delete_batches=delete_batches,
        )
        cleaned, orphaned_rows = await kb_init._reconcile_vectors(
            svc, self._factory(set())
        )
        assert cleaned == []
        assert delete_batches == []  # the safety guard held
        assert orphaned_rows == []

    @pytest.mark.asyncio
    async def test_skips_when_store_cannot_enumerate(self):
        """A store that can't enumerate (e.g. the ChromaDBVectorStore fallback)
        degrades to a clean no-op — and warns, since drift goes uncleaned."""
        svc = MagicMock()
        svc._vector_store = object()  # no list_parent_document_ids
        cleaned, orphaned_rows = await kb_init._reconcile_vectors(
            svc, self._factory({"kb_aaaaaaaaaaaa"})
        )
        assert cleaned == []
        assert orphaned_rows == []


# ---------------------------------------------------------------------------
# Cross-store repair (6.3): re-embed orphaned rows (SQL present, vectors gone),
# the half-state a crash between the SQL commit and the Chroma write leaves.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reindex_missing_vectors_re_embeds_from_persisted_row(
    fk_on_ingest_service,
):
    """Repair re-derives an existing row's vectors via the NON-prechunked index
    path (chunk + BGE-M3) and never touches the SQL row."""
    from sqlalchemy import text

    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )

    svc, factory = fk_on_ingest_service
    # Seed a real SQL row (the vector write is stubbed by the fixture).
    await svc.ingest_runbook(
        document_id="kb_repairme",
        title="Repair Me",
        content="# Body\n\nSome content to chunk.",
        organization_id="org-1",
        scope="global",
        tags=["postgres"],
        verified_by=None,
        verification_level=VerificationLevel.COMMUNITY,
    )
    svc._index_document_in_vector_store.reset_mock()
    svc._index_document_in_vector_store.return_value = 4

    chunks = await svc.reindex_missing_vectors("kb_repairme")

    assert chunks == 4
    svc._index_document_in_vector_store.assert_awaited_once()
    (doc,), kwargs = svc._index_document_in_vector_store.await_args
    # Doc rebuilt from the persisted row, mirroring ingest_runbook's construction.
    assert doc.document_id == "kb_repairme"
    assert doc.title == "Repair Me"
    assert "Some content to chunk." in doc.content
    assert doc.scope == "global"
    assert doc.tags == ["postgres"]
    # prechunked=None → the re-embed path, NOT pack passthrough.
    assert kwargs["prechunked"] is None

    # The SQL row is the source of truth and must be untouched by repair.
    async with factory() as session:
        row = (
            await session.execute(
                text("SELECT title FROM knowledge_items WHERE item_id='kb_repairme'")
            )
        ).first()
    assert row is not None and row[0] == "Repair Me"


@pytest.mark.asyncio
async def test_reindex_missing_vectors_absent_row_is_noop(fk_on_ingest_service):
    """No row for the id → fail-safe 0, and the index path is never called."""
    svc, _ = fk_on_ingest_service
    svc._index_document_in_vector_store.reset_mock()

    chunks = await svc.reindex_missing_vectors("kb_doesnotexist")

    assert chunks == 0
    svc._index_document_in_vector_store.assert_not_awaited()


class TestRepairOrphanedRows:
    """The bounded startup repair over the orphaned-row set reconcile produces."""

    @staticmethod
    def _service(reindex):
        svc = MagicMock()
        svc.reindex_missing_vectors = reindex
        return svc

    @pytest.mark.asyncio
    async def test_empty_is_noop(self):
        svc = self._service(AsyncMock(return_value=3))
        repaired, still = await kb_init._repair_orphaned_rows([], svc)
        assert repaired == []
        assert still == []
        svc.reindex_missing_vectors.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_re_embeds_all_orphaned_rows(self):
        svc = self._service(AsyncMock(return_value=3))
        repaired, still = await kb_init._repair_orphaned_rows(
            ["kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb"], svc
        )
        assert repaired == ["kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb"]
        assert still == []
        assert svc.reindex_missing_vectors.await_count == 2

    @pytest.mark.asyncio
    async def test_zero_chunks_leaves_row_orphaned(self):
        """Embedding model unavailable → 0 chunks → fail-safe: NOT repaired,
        left for a later boot rather than reported as healed."""
        svc = self._service(AsyncMock(return_value=0))
        repaired, still = await kb_init._repair_orphaned_rows(["kb_aaaaaaaaaaaa"], svc)
        assert repaired == []
        assert still == ["kb_aaaaaaaaaaaa"]

    @pytest.mark.asyncio
    async def test_exception_leaves_row_orphaned(self):
        """A repair error (e.g. ChromaDB down) never aborts the boot path."""
        svc = self._service(AsyncMock(side_effect=RuntimeError("chroma down")))
        repaired, still = await kb_init._repair_orphaned_rows(["kb_aaaaaaaaaaaa"], svc)
        assert repaired == []
        assert still == ["kb_aaaaaaaaaaaa"]

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure(self):
        async def _reindex(item_id):
            return 3 if item_id == "kb_aaaaaaaaaaaa" else 0

        svc = self._service(AsyncMock(side_effect=_reindex))
        repaired, still = await kb_init._repair_orphaned_rows(
            ["kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb"], svc
        )
        assert repaired == ["kb_aaaaaaaaaaaa"]
        assert still == ["kb_bbbbbbbbbbbb"]

    @pytest.mark.asyncio
    async def test_over_cap_repairs_nothing(self):
        """A mass orphan (bulk vector loss) is NOT the crash recovery this pass
        handles — repair nothing (fast boot preserved), leave all warned."""
        svc = self._service(AsyncMock(return_value=1))
        rows = [f"kb_{i:012x}" for i in range(kb_init.KB_REPAIR_MAX_ROWS + 1)]
        repaired, still = await kb_init._repair_orphaned_rows(rows, svc)
        assert repaired == []
        assert still == rows
        svc.reindex_missing_vectors.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_at_row_cap_still_repairs_within_chunk_budget(self):
        """Exactly at the row cap is targeted recovery, not a bulk anomaly — all
        repair when the total chunk work stays under the per-boot budget (1 chunk
        each here so KB_REPAIR_MAX_ROWS rows < KB_REPAIR_MAX_CHUNKS)."""
        assert kb_init.KB_REPAIR_MAX_ROWS <= kb_init.KB_REPAIR_MAX_CHUNKS
        svc = self._service(AsyncMock(return_value=1))
        rows = [f"kb_{i:012x}" for i in range(kb_init.KB_REPAIR_MAX_ROWS)]
        repaired, still = await kb_init._repair_orphaned_rows(rows, svc)
        assert repaired == rows
        assert still == []

    @pytest.mark.asyncio
    async def test_chunk_budget_defers_remaining_rows(self):
        """The per-boot chunk budget bounds embedding WORK (not just row count):
        once spent, remaining rows are deferred to the next boot, not embedded on
        the startup path. Here the first row alone spends the whole budget."""
        svc = self._service(AsyncMock(return_value=kb_init.KB_REPAIR_MAX_CHUNKS))
        rows = ["kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb", "kb_cccccccccccc"]
        repaired, still = await kb_init._repair_orphaned_rows(rows, svc)
        assert repaired == ["kb_aaaaaaaaaaaa"]
        assert still == ["kb_bbbbbbbbbbbb", "kb_cccccccccccc"]
        # The deferred rows are never embedded this boot.
        assert svc.reindex_missing_vectors.await_count == 1

    @pytest.mark.asyncio
    async def test_honors_explicit_max_rows(self):
        """The row cap is a parameter (deployment-configurable), not hardcoded."""
        svc = self._service(AsyncMock(return_value=1))
        rows = ["kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb", "kb_cccccccccccc"]
        repaired, still = await kb_init._repair_orphaned_rows(rows, svc, max_rows=2)
        assert repaired == []  # 3 > cap 2 → bulk-loss, repair nothing
        assert still == rows
        svc.reindex_missing_vectors.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_honors_explicit_max_chunks(self):
        """The chunk budget is a parameter (deployment-configurable)."""
        svc = self._service(AsyncMock(return_value=5))
        rows = ["kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb", "kb_cccccccccccc"]
        repaired, still = await kb_init._repair_orphaned_rows(
            rows, svc, max_rows=10, max_chunks=5
        )
        # First row embeds 5 chunks (== budget) → the rest defer.
        assert repaired == ["kb_aaaaaaaaaaaa"]
        assert still == ["kb_bbbbbbbbbbbb", "kb_cccccccccccc"]

    def test_resolve_repair_bounds_reads_settings(self):
        """_resolve_repair_bounds pulls the deployment-configured bounds."""
        fake = MagicMock()
        fake.database.kb_repair_max_rows = 7
        fake.database.kb_repair_max_chunks = 13
        with patch("faultmaven.config.settings.get_settings", return_value=fake):
            assert kb_init._resolve_repair_bounds() == (7, 13)

    def test_resolve_repair_bounds_falls_back_on_error(self):
        """Settings unavailable (some test contexts) → module defaults, no raise."""
        with patch(
            "faultmaven.config.settings.get_settings",
            side_effect=RuntimeError("no settings"),
        ):
            assert kb_init._resolve_repair_bounds() == (
                kb_init.KB_REPAIR_MAX_ROWS,
                kb_init.KB_REPAIR_MAX_CHUNKS,
            )

    @pytest.mark.parametrize(
        "env, value",
        [
            ("KB_REPAIR_MAX_ROWS", "0"),
            ("KB_REPAIR_MAX_ROWS", "-1"),
            ("KB_REPAIR_MAX_CHUNKS", "0"),
            ("KB_REPAIR_MAX_CHUNKS", "-5"),
        ],
    )
    def test_bounds_reject_nonpositive_at_load(self, monkeypatch, env, value):
        """A 0/negative bound silently disables repair — so it is rejected loudly
        at settings load (ge=1) rather than accepted and quietly no-op'd."""
        from pydantic import ValidationError

        from faultmaven.config.settings import DatabaseSettings

        monkeypatch.setenv(env, value)
        with pytest.raises(ValidationError):
            DatabaseSettings()


class TestCrossStoreRepairSeam:
    """DoD: a simulated mid-ingest failure (SQL row present, vectors missing)
    leaves no silently-unretrievable runbook — reconcile detects the orphan and
    repair re-embeds it, exactly as bootstrap_kb wires the two in sequence."""

    @pytest.mark.asyncio
    async def test_orphaned_row_is_detected_then_repaired(self):
        reindexed: list[str] = []

        async def _reindex(item_id):
            reindexed.append(item_id)
            return 3

        async def _list():
            # Only 'a' has vectors; 'b's Chroma write crashed mid-ingest.
            return {"kb_aaaaaaaaaaaa"}

        vector_store = MagicMock()
        vector_store.list_parent_document_ids = AsyncMock(side_effect=_list)
        vector_store.delete_documents_by_parents = AsyncMock(return_value=0)
        svc = MagicMock()
        svc._vector_store = vector_store
        svc.reindex_missing_vectors = AsyncMock(side_effect=_reindex)

        factory = TestReconcileVectors._factory({"kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb"})

        # Step 1 — reconcile only DETECTS the orphaned row (cannot fix it alone).
        _cleaned, orphaned = await kb_init._reconcile_vectors(svc, factory)
        assert orphaned == ["kb_bbbbbbbbbbbb"]

        # Step 2 — repair HEALS it by re-embedding from its persisted SQL row.
        repaired, still = await kb_init._repair_orphaned_rows(orphaned, svc)
        assert repaired == ["kb_bbbbbbbbbbbb"]
        assert still == []
        assert reindexed == ["kb_bbbbbbbbbbbb"]


# ---------------------------------------------------------------------------
# Per-Cause graph record persistence (the app half of the cross-repo pack
# contract — see tests/integration/modules/knowledge/test_runbook_causes_contract.py)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_runbook_persists_causes_to_metadata(fk_on_ingest_service):
    """The pack's per-Cause graph record round-trips through
    knowledge_items.metadata['causes'], keyed by item_id — the foundation the
    matcher reads back (increment 4)."""
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
        DatabaseKnowledgeItemRepository,
    )

    svc, factory = fk_on_ingest_service
    causes = [
        {
            "cause_letter": "A",
            "cause_statement": "idle txns exhaust the pool",
            "chain_nodes": [{"ref": "root", "node_type": "root", "statement": "x"}],
            "is_fallback_cause": False,
        },
        {"cause_letter": "Z", "is_fallback_cause": True},
    ]
    await svc.ingest_runbook(
        document_id="kb_causes",
        title="Causes Runbook",
        content="# body",
        organization_id="org-1",
        scope="global",
        verified_by=None,
        verification_level=VerificationLevel.COMMUNITY,
        causes=causes,
    )
    async with factory() as session:
        item = await DatabaseKnowledgeItemRepository(session).get_by_id("kb_causes")
    assert item is not None
    assert item.metadata is not None
    assert item.metadata["causes"] == causes


@pytest.mark.asyncio
async def test_ingest_runbook_without_causes_leaves_metadata_unset(
    fk_on_ingest_service,
):
    """No causes payload → metadata stays unset (upload path unchanged)."""
    from faultmaven.modules.knowledge.domain.models.knowledge_item import (
        VerificationLevel,
    )
    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
        DatabaseKnowledgeItemRepository,
    )

    svc, factory = fk_on_ingest_service
    await svc.ingest_runbook(
        document_id="kb_nocauses",
        title="No Causes",
        content="# body",
        organization_id="org-1",
        scope="global",
        verified_by=None,
        verification_level=VerificationLevel.COMMUNITY,
    )
    async with factory() as session:
        item = await DatabaseKnowledgeItemRepository(session).get_by_id("kb_nocauses")
    assert item is not None
    assert not (item.metadata or {}).get("causes")


def test_pack_load_carries_causes_from_fixture_pack(tmp_path):
    """KbPack.load lifts each runbook's per-Cause graph record from pack.json.
    Deterministic in-test fixture (not the shipped pack) so it runs everywhere,
    including a packaged install where resources/ is absent."""
    from faultmaven.bootstrap.kb_pack import KbPack

    causes = [
        {"cause_letter": "A", "cause_statement": "root", "is_fallback_cause": False},
        {"cause_letter": "Z", "is_fallback_cause": True},
    ]
    pack = KbPack.load(_write_pack(tmp_path, causes=causes))
    assert pack is not None
    (rb,) = pack.runbooks
    assert rb.causes == causes


def test_pack_load_old_pack_without_causes_field(tmp_path):
    """A pack.json predating the `causes` record (no `causes` key) loads as []."""
    from faultmaven.bootstrap.kb_pack import KbPack

    pack = KbPack.load(_write_pack(tmp_path))  # no causes= → key omitted
    assert pack is not None
    assert pack.runbooks[0].causes == []


def test_shipped_pack_carries_causes_if_vendored():
    """Guard the PRODUCTION pack: the vendored runbooks actually ship a causes
    record — catches a kb-toolkit rebuild that drops it (the fixture test only
    proves the loader copies a field it is handed). Skips where the pack isn't
    vendored (local dev); the CI lane that vendors it runs the assertion."""
    from pathlib import Path

    import faultmaven
    from faultmaven.bootstrap.kb_pack import KbPack, baseline_pack_dir

    root = Path(faultmaven.__file__).resolve().parent.parent
    pack = KbPack.load(baseline_pack_dir(root))
    if pack is None:
        pytest.skip("KB pack not vendored in this tree")
    with_causes = [rb for rb in pack.runbooks if rb.causes]
    assert with_causes, "shipped pack carries no causes record"
    assert "cause_letter" in with_causes[0].causes[0]


def test_pack_load_rejects_mismatched_embedding_model(tmp_path, caplog):
    """A pack built with a different embedding model is refused (returns None):
    its vectors live in another space than the runtime query embedder, so every
    similarity would be garbage while the dimension can still match. Refusing
    keeps the store from being filled with unqueryable vectors."""
    import logging

    from faultmaven.bootstrap.kb_pack import KbPack

    pack_dir = _write_pack(tmp_path, model="sentence-transformers/all-MiniLM-L6-v2")
    with caplog.at_level(logging.ERROR):
        pack = KbPack.load(pack_dir)
    assert pack is None
    assert any(
        "incompatible" in r.message and "all-MiniLM-L6-v2" in r.getMessage()
        for r in caplog.records
    )


def test_pack_load_accepts_matching_embedding_model(tmp_path):
    """The guard does not fire when the pack's declared model matches the runtime
    embedder (the normal, shipped case)."""
    from faultmaven.bootstrap.kb_pack import KbPack

    pack = KbPack.load(_write_pack(tmp_path, model="BAAI/bge-m3"))
    assert pack is not None
    assert pack.model == "BAAI/bge-m3"


def test_pack_load_accepts_case_variant_model(tmp_path):
    """A same-model alias that only differs in case is NOT refused — HF model ids
    resolve case-insensitively, so the vectors are compatible. Guards against a
    false positive that would empty the whole KB over a cosmetic case difference."""
    from faultmaven.bootstrap.kb_pack import KbPack

    pack = KbPack.load(_write_pack(tmp_path, model="baai/BGE-M3"))
    assert pack is not None


def test_embedding_model_matches_runtime_embedder():
    """Drift guard: the loader's expected pack model (kb_pack.EMBEDDING_MODEL)
    must equal the runtime query embedder's id (model_cache.BGE_M3_MODEL_ID).
    They are separate literals (kb_pack stays model-free and cannot import
    model_cache), so this pins them — if the runtime embedder changes and this
    constant does not, the identity guard would silently check against a stale id."""
    from faultmaven.bootstrap.kb_pack import EMBEDDING_MODEL
    from faultmaven.infrastructure.model_cache import BGE_M3_MODEL_ID

    assert EMBEDDING_MODEL == BGE_M3_MODEL_ID


def test_pack_load_model_less_pack_fails_open(tmp_path):
    """A pack.json that declares no `model` at all cannot be checked, so the guard
    fails OPEN (loads) rather than refusing an otherwise-valid pack."""
    import json as _json

    from faultmaven.bootstrap.kb_pack import KbPack

    pack_dir = _write_pack(tmp_path)
    manifest_path = pack_dir / "pack.json"
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["model"]
    manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")

    pack = KbPack.load(pack_dir)
    assert pack is not None


@pytest.mark.parametrize("bad_row", [-1, -2, 2, 99, "0", 1.5, True])
def test_pack_load_rejects_out_of_range_vector_row(tmp_path, caplog, bad_row):
    """A vector_row that is negative, out of range, or not a plain int refuses the
    pack whole (returns None). A NEGATIVE row is the silent hazard: numpy indexing
    wraps it (-1 -> the last vector) and pairs a chunk's text with the WRONG
    embedding, undetectably. The pack here ships 2 vectors (rows 0 and 1), so
    -1/-2/2/99, the string "0", the float 1.5, and bool True are all invalid."""
    import json as _json
    import logging

    from faultmaven.bootstrap.kb_pack import KbPack

    pack_dir = _write_pack(tmp_path)  # 2 chunks -> vectors rows 0, 1
    manifest_path = pack_dir / "pack.json"
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runbooks"][0]["chunks"][0]["vector_row"] = bad_row
    manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        pack = KbPack.load(pack_dir)
    assert pack is None
    assert any("vector_row" in r.getMessage() for r in caplog.records)


def test_pack_load_accepts_highest_valid_vector_row(tmp_path):
    """Boundary: the highest in-range row (n_vectors - 1) loads — the guard is
    `row < n_vectors`, so it must not reject the last valid row off-by-one. Rows
    stay distinct (the first chunk takes the highest row) so the uniqueness guard
    does not fire."""
    import json as _json

    from faultmaven.bootstrap.kb_pack import KbPack

    pack_dir = _write_pack(tmp_path)  # 2 vectors -> rows 0, 1
    manifest_path = pack_dir / "pack.json"
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runbooks"][0]["chunks"][0]["vector_row"] = 1  # the last valid row
    manifest["runbooks"][0]["chunks"][1]["vector_row"] = 0
    manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")

    pack = KbPack.load(pack_dir)
    assert pack is not None
    assert len(pack.runbooks[0].chunks) == 2


def test_pack_load_rejects_duplicate_vector_row_within_runbook(tmp_path, caplog):
    """Two chunks in one runbook naming the SAME valid row refuse the pack whole
    (returns None). Both rows pass the bounds check individually, so only the
    uniqueness guard catches it: sharing one vector means at least one chunk is
    paired with the wrong embedding."""
    import json as _json
    import logging

    from faultmaven.bootstrap.kb_pack import KbPack

    pack_dir = _write_pack(tmp_path)  # 2 chunks -> rows 0, 1
    manifest_path = pack_dir / "pack.json"
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    # Point the second chunk at the first chunk's (in-range) row.
    manifest["runbooks"][0]["chunks"][1]["vector_row"] = 0
    manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        pack = KbPack.load(pack_dir)
    assert pack is None
    assert any(
        "vector_row=0" in r.getMessage() and "two" in r.getMessage()
        for r in caplog.records
    )


def test_pack_load_rejects_duplicate_vector_row_across_runbooks(tmp_path, caplog):
    """The uniqueness check is pack-wide: two chunks in DIFFERENT runbooks sharing
    a row also refuse the pack. The `vectors` matrix is shared across runbooks, so
    a collision anywhere means a misaligned text<->vector pairing."""
    import json as _json
    import logging

    from faultmaven.bootstrap.kb_pack import KbPack

    pack_dir = _write_pack(tmp_path)  # 1 runbook, 2 chunks -> rows 0, 1
    manifest_path = pack_dir / "pack.json"
    manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
    # A second runbook (reusing the same md file) whose only chunk reuses row 0.
    first = manifest["runbooks"][0]
    second = dict(first)
    second["item_id"] = kb_init._item_id_from_runbook_id("second-runbook")
    second["chunks"] = [{"chunk_index": 0, "vector_row": 0, "text": "other"}]
    manifest["runbooks"].append(second)
    manifest_path.write_text(_json.dumps(manifest), encoding="utf-8")

    with caplog.at_level(logging.ERROR):
        pack = KbPack.load(pack_dir)
    assert pack is None
    assert any("vector_row=0" in r.getMessage() for r in caplog.records)


def test_pack_load_baseline_vector_rows_are_unique():
    """No false positive: the shipped baseline pack pairs each chunk with a
    distinct vector_row, so the uniqueness guard loads it clean. Skipped when the
    pack is not vendored (local dev); the CI lane that vendors it runs it. Also
    cross-checks the shipped pack.json directly (it, unlike the loaded PackChunk,
    still carries vector_row) so the 1:1 invariant is pinned at its source."""
    import json as _json
    from pathlib import Path

    import faultmaven
    from faultmaven.bootstrap.kb_pack import KbPack, baseline_pack_dir

    root = Path(faultmaven.__file__).resolve().parent.parent
    pack_dir = baseline_pack_dir(root)
    pack = KbPack.load(pack_dir)
    if pack is None:
        pytest.skip("KB pack not vendored in this tree")

    manifest = _json.loads((pack_dir / "pack.json").read_text(encoding="utf-8"))
    rows = [
        c["vector_row"] for rb in manifest["runbooks"] for c in rb.get("chunks", [])
    ]
    assert rows, "baseline pack has no chunks"
    assert len(rows) == len(set(rows)), "baseline pack has duplicate vector_row(s)"


def test_parse_json_dict_handles_dict_and_str_inputs():
    """The knowledge-item repo read must accept BOTH a JSON string (SQLite TEXT)
    and an already-decoded dict (PostgreSQL JSONB) — otherwise metadata['causes']
    is silently dropped on PG. Regression guard for the unguarded json.loads."""
    from unittest.mock import MagicMock

    from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
        DatabaseKnowledgeItemRepository,
    )

    repo = DatabaseKnowledgeItemRepository(MagicMock())
    payload = {"causes": [{"cause_letter": "A"}]}
    assert repo._parse_json_dict('{"causes": [{"cause_letter": "A"}]}') == payload
    assert repo._parse_json_dict(payload) == payload  # PG JSONB dict return
    assert repo._parse_json_dict(None) is None
    assert repo._parse_json_dict("") is None
    assert repo._parse_json_dict("[1, 2]") is None  # non-dict JSON → None


# ---------------------------------------------------------------------------
# fm#1108: the chunk stamp is a third idempotency axis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_stamp",
    [
        pytest.param(None, id="never-stamped-predates-1108"),
        pytest.param("0000000000000000", id="stale-stamp-grammar-or-schema-moved"),
    ],
)
async def test_bootstrap_re_ingests_when_the_chunk_stamp_is_absent_or_stale(
    tmp_path: Path, stored_stamp
):
    """Content and causes both unchanged, and it STILL re-ingests.

    This is the lever that makes fm#1108 take effect instead of waiting. The
    seeder's join key now lives in chunk metadata, stamped by a specific schema
    and cause-heading grammar; a row written before that, or under a grammar
    since edited, holds stamps that no longer mean what they say. Neither fact
    is in the content hash — the grammar lives in code — so without this axis a
    grammar change would leave every stored stamp quietly wrong, which is the
    exact hazard fm#1108 exists to close.

    Cheap by construction: pack re-ingest is prechunked, so it costs no
    embedding.
    """
    pack_dir = _write_pack(tmp_path, causes=CAUSES_RECORD)
    knowledge_service = MagicMock()
    knowledge_service.ingest_runbook = AsyncMock(return_value=2)
    knowledge_service._vector_store = MagicMock()
    knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

    stored = {"causes": CAUSES_RECORD}
    if stored_stamp is not None:
        stored["chunk_stamp"] = stored_stamp

    existing = MagicMock()
    existing.content = RUNBOOK_MD  # content hash matches
    existing.knowledge_metadata = stored  # causes match too

    result = await kb_init.bootstrap_kb(
        knowledge_service=knowledge_service,
        db_session_factory=_make_session_factory(existing_row=existing),
        organization_id="org-test",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert result.ingested == ["global/example.md"]
    assert result.skipped_unchanged == []
    knowledge_service._vector_store.delete_documents_by_parent_id.assert_awaited()


def test_the_stamp_identity_tracks_the_cause_heading_grammar():
    """The identity must be DERIVED from the grammar, not a constant someone
    remembers to bump. ``CAUSE_HEADING_RE`` is a manual mirror of kb-toolkit's
    and is expected to change; a discipline-based bump is exactly the kind of
    step that gets skipped, and skipping it is silent."""
    import re

    from faultmaven.modules.knowledge.domain.services import runbook_grammar as g

    before = chunk_stamp_identity()
    saved = g.CAUSE_HEADING_RE
    try:
        g.CAUSE_HEADING_RE = re.compile(r"^### Cause ([A-Z]+):\s*(.+?)\s*$", re.M)
        assert chunk_stamp_identity() != before
    finally:
        g.CAUSE_HEADING_RE = saved
    assert chunk_stamp_identity() == before, "identity must be stable otherwise"


@pytest.mark.asyncio
async def test_a_stamp_change_does_not_republish_an_unpublished_runbook():
    """The stamp axis must not undo an operator's unpublish.

    ``delete_document`` retires a built-in by dropping its VECTORS — retrieval
    ignores ``is_published``, so that is what actually removes it from
    investigations — and its contract is that the retirement survives restart
    until the runbook's CONTENT changes. Re-ingest is delete-then-create and the
    created row is published by default, so re-ingesting for a stamp change
    would put a deliberately retired runbook back into retrieval on the strength
    of a code edit. Skipping is also right on its own terms: an unpublished
    built-in has no vectors, so it carries no stamps to refresh.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pack_dir = _write_pack(tmp_path, causes=CAUSES_RECORD)
        knowledge_service = MagicMock()
        knowledge_service.ingest_runbook = AsyncMock(return_value=2)
        knowledge_service._vector_store = MagicMock()
        knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

        existing = MagicMock()
        existing.content = RUNBOOK_MD  # content unchanged
        existing.knowledge_metadata = {"causes": CAUSES_RECORD}  # no stamp
        existing.is_published = False  # ...but retired by an operator

        result = await kb_init.bootstrap_kb(
            knowledge_service=knowledge_service,
            db_session_factory=_make_session_factory(existing_row=existing),
            organization_id="org-test",
            project_root=tmp_path,
            pack_dir=pack_dir,
        )

        assert result.ingested == []
        assert result.skipped_unchanged == ["global/example.md"]
        knowledge_service.ingest_runbook.assert_not_awaited()
        knowledge_service._vector_store.delete_documents_by_parent_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_content_change_still_re_ingests_an_unpublished_runbook():
    """The converse, so the guard above cannot become a blanket exemption.

    A content change IS an intentional new version — the documented behaviour
    the unpublish contract carves out — and must still re-ingest.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pack_dir = _write_pack(tmp_path, causes=CAUSES_RECORD)
        knowledge_service = MagicMock()
        knowledge_service.ingest_runbook = AsyncMock(return_value=2)
        knowledge_service._vector_store = MagicMock()
        knowledge_service._vector_store.delete_documents_by_parent_id = AsyncMock()

        existing = MagicMock()
        existing.content = "# Something else entirely\n"  # content MOVED
        existing.knowledge_metadata = {"causes": CAUSES_RECORD}
        existing.is_published = False

        result = await kb_init.bootstrap_kb(
            knowledge_service=knowledge_service,
            db_session_factory=_make_session_factory(existing_row=existing),
            organization_id="org-test",
            project_root=tmp_path,
            pack_dir=pack_dir,
        )

        assert result.ingested == ["global/example.md"]
