"""A chunk is served only under its row's generation, and no writer deletes one (#2388).

Two ``kb_init`` runs seeding different pack versions of one id used to strip
each other's chunks: the indexer deleted every chunk under the parent before it
wrote its own, so one ingest's row could end up serving the other version's
vectors, and later boots skipped it. Now every row carries a
``vector_generation``, every chunk id and chunk carries the generation it was
written for, the pack path writes its vectors before one transaction that
replaces the row it observed (guarded on that row's generation), and every KB
door admits a chunk only when its row carries the chunk's generation.

Everything here is real but the embedder: FK-on SQLite (or PostgreSQL under
``-m postgres``), an on-disk ChromaDB of the test's own, the production
``KnowledgeVectorStore``, ``KnowledgeService`` and ``kb_init``.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Dict, List, Optional
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.bootstrap import kb_init
from faultmaven.bootstrap.kb_pack import PackChunk, PackRunbook
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure import embedding_guard
from faultmaven.infrastructure.knowledge.admission import chunk_generation_from_id
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.persistence.models import KnowledgeItemModel
from faultmaven.models.api import KnowledgeBaseDocument
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    PACK_CONTENT_HASH_KEY,
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope
from faultmaven.utils.runbook_id import item_id_from_runbook_id
from faultmaven.utils.serialization import decode_json_blob
from tests.integration.modules.knowledge.test_row_checked_retrieval_2397 import (
    OWNER,
    QUERY,
    REAL_VECTORS,
    SCOPE,
    Q,
    _CapturingRouter,
    _context,
    _fixed_query_embedding,  # noqa: F401 - autouse: every query embeds to Q
    _registered_kb_qa,
    _unit,
    factory,  # noqa: F401 - the FK-on SQLite world
)
from tests.integration.security.conftest import alembic_on, database_of_its_own
from tests.utils import regex_only_sanitizer, seed_enterprises

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

DOC = item_id_from_runbook_id("generation-2388")
#: A superseded generation's chunk, and one written before #2388, both carry
#: ``Q`` itself, so they OUTRANK the served chunk at every door.
SUPERSEDED = "5e" * 16
POSTGRES = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
)


def _text(marker: str) -> str:
    return (
        f"# Runbook {marker}\nMARKER::{marker}::\n"
        "Connection pool exhausted; queries time out under load."
    )


def _markers(text_: str) -> set[str]:
    return {
        m
        for m in ("v0", "vA", "vB", "served", "superseded", "legacy")
        if f"MARKER::{m}::" in text_
    }


def _world(factory_, tmp_path) -> SimpleNamespace:
    chroma = chromadb.PersistentClient(
        path=str(tmp_path / "chroma"),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
    )
    store = KnowledgeVectorStore(chroma)
    service = KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=store,
        db_session_factory=factory_,
    )
    return SimpleNamespace(
        chroma=chroma, store=store, service=service, factory=factory_
    )


def _pack(version: str, vector: List[float]) -> PackRunbook:
    return PackRunbook(
        item_id=DOC,
        content_hash=f"hash-{version}",
        title=f"Runbook {version}",
        scope="global",
        relpath="global/generation.md",
        content=_text(version),
        tags=[],
        source_url=None,
        owner_id=None,
        team_id=None,
        chunks=[PackChunk(chunk_index=0, text=_text(version), embedding=vector)],
    )


async def _seed(world, runbook: PackRunbook) -> str:
    return await kb_init._ingest_pack_runbook(
        runbook=runbook,
        knowledge_service=world.service,
        db_session_factory=world.factory,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        role_may_update=True,
    )


def _chunks(world, parent: str = DOC) -> Dict[str, str]:
    """Chunk id -> the marker its text carries, straight from ChromaDB."""
    got = world.chroma.get_collection(KB_COLLECTION).get(include=["documents"])
    return {
        chunk_id: next(iter(_markers(document)), "")
        for chunk_id, document in zip(got["ids"], got["documents"])
        if chunk_id.startswith(f"{parent}_chunk_")
    }


async def _row(world, item_id: str = DOC):
    async with world.factory() as session:
        return await session.get(KnowledgeItemModel, item_id)


async def _served(world) -> set[str]:
    """The markers the store's ``search`` door serves for ``DOC``."""
    hits = await world.store.search(
        KB_COLLECTION,
        QUERY,
        k=10,
        where=SCOPE,
        admit=world.service.admitted_generations,
    )
    return {m for hit in hits for m in _markers(hit["content"])}


# --- the #2388 regression: two pack versions of one id at once ---------------

ORDERS = ["old_row_a_commits_first", "old_row_b_commits_first", "no_row"]


async def _race(world, order: str):
    """Seed v0 (unless ``no_row``), then ingest vA and vB of the same id at
    once: both read the row and write their vectors, then their row
    transactions run in ``order``, with each writer's delete of the generation
    it replaced on (ruling (i)). Returns ``(winner, loser, old generation, the
    loser's error)``."""
    old = None
    if order != "no_row":
        await _seed(world, _pack("v0", _unit(0.8, 0.6)))
        old = (await _row(world)).vector_generation
    a, b = _pack("vA", REAL_VECTORS[0]), _pack("vB", REAL_VECTORS[1])
    first, second = (b, a) if order == "old_row_b_commits_first" else (a, b)
    gates = {a.content: asyncio.Event(), b.content: asyncio.Event()}
    arrived: List[str] = []
    real = KnowledgeService._insert_pack_row

    async def at_the_boundary(self, item, **kwargs):
        arrived.append(item.content)
        await gates[item.content].wait()
        return await real(self, item, **kwargs)

    store = world.store
    with (
        patch.object(KnowledgeService, "_insert_pack_row", at_the_boundary),
        patch.object(
            store,
            "delete_documents_by_parent_id",
            wraps=store.delete_documents_by_parent_id,
        ) as by_parent,
        patch.object(
            store, "delete_observed_chunk_ids", wraps=store.delete_observed_chunk_ids
        ) as by_id,
        patch.object(
            store, "delete_generation", wraps=store.delete_generation
        ) as by_generation,
    ):
        tasks = {rb.content: asyncio.create_task(_seed(world, rb)) for rb in (a, b)}
        while len(arrived) < 2:
            await asyncio.sleep(0)
        gates[first.content].set()
        assert await tasks[first.content] == "ingested"
        gates[second.content].set()
        with pytest.raises(ValueError, match="already exists") as lost:
            await tasks[second.content]
    assert (
        by_parent.await_count == by_id.await_count == 0
    ), "an ingest deleted by parent"
    # The winner deletes only the generation its guarded DELETE replaced; the
    # loser, whose transaction did not commit, deletes nothing.
    assert [c.args for c in by_generation.await_args_list] == (
        [] if old is None else [(DOC, old)]
    )
    return first, second, old, lost.value


async def _assert_only_the_winner_is_served(world, winner, loser, old):
    row = await _row(world)
    meta = decode_json_blob(row.knowledge_metadata)
    assert meta[PACK_CONTENT_HASH_KEY] == winner.content_hash
    chunks = _chunks(world)
    # The winner's and the loser's: v0's went with the winner's commit, and
    # the loser's are left, never served, for the sweep.
    assert sorted(chunks.values()) == ["vA", "vB"]
    by_marker = {m: chunk_generation_from_id(c) for c, m in chunks.items()}
    winner_marker = _markers(winner.content).pop()
    assert row.vector_generation == by_marker[winner_marker]
    assert row.vector_generation not in {old, by_marker[_markers(loser.content).pop()]}
    assert await world.service.admitted_generations([DOC]) == {
        DOC: row.vector_generation
    }
    assert await _served(world) == {winner_marker}


@pytest.mark.parametrize("order", ORDERS)
async def test_two_pack_versions_at_once_serve_only_the_surviving_rows_chunks(
    factory, tmp_path, order  # noqa: F811
):
    world = _world(factory, tmp_path)
    winner, loser, old, _error = await _race(world, order)
    await _assert_only_the_winner_is_served(world, winner, loser, old)


async def test_a_failed_insert_rolls_back_the_guarded_delete(
    factory, tmp_path
):  # noqa: F811
    """The guarded DELETE matched the old row, and the INSERT then failed (here
    on a foreign key): the transaction rolls back whole, so the old row and
    the chunks it serves stand."""
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v0", REAL_VECTORS[0]))
    old = await _row(world)
    replacement = KnowledgeItem(
        item_id=DOC,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        title="Runbook vA",
        content=_text("vA"),
        item_type=KnowledgeItemType.RUNBOOK,
        scope=KnowledgeScope.GLOBAL,
        verified_by="no-such-user",
    )

    with pytest.raises(Exception):
        await world.service._insert_pack_row(
            replacement,
            replaces_generation=old.vector_generation,
            team_id=None,
            created_by=None,
        )

    assert (await _row(world)).vector_generation == old.vector_generation
    assert await _served(world) == {"v0"}
    assert sorted(_chunks(world).values()) == ["v0"], "a failed switch deleted"


# --- the writer deletes the generation it replaced (ruling (i)) --------------


async def test_a_pack_replacement_leaves_only_the_new_generation(
    factory, tmp_path
):  # noqa: F811
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v0", REAL_VECTORS[0]))

    assert await _seed(world, _pack("vA", REAL_VECTORS[1])) == "ingested"

    row = await _row(world)
    assert {chunk_generation_from_id(c): m for c, m in _chunks(world).items()} == {
        row.vector_generation: "vA"
    }


async def test_a_failed_generation_delete_is_logged_and_the_replacement_stands(
    factory, tmp_path, caplog  # noqa: F811
):
    world = _world(factory, tmp_path)
    await _seed(world, _pack("v0", REAL_VECTORS[0]))
    old = (await _row(world)).vector_generation

    with patch.object(
        world.store, "delete_generation", side_effect=OSError("chromadb gone")
    ):
        assert await _seed(world, _pack("vA", REAL_VECTORS[1])) == "ingested"

    assert await _served(world) == {"vA"}
    assert sorted(_chunks(world).values()) == ["v0", "vA"]
    assert f"Could not delete the replaced generation {old}" in caplog.text


async def test_an_update_leaves_only_the_new_generation(
    factory, tmp_path, monkeypatch  # noqa: F811
):
    world = _world(factory, tmp_path)
    authored = "kb_" + "6" * 16
    await _authored(world, authored)
    _embedder(monkeypatch)
    await world.service.reindex_missing_vectors(authored)
    before = (await _row(world, authored)).vector_generation

    await world.service.update_document_metadata(
        authored, content="# Authored\n\nRaise the pool size, then restart."
    )

    after = (await _row(world, authored)).vector_generation
    assert after != before
    assert {chunk_generation_from_id(c) for c in _all_chunk_ids(world, authored)} == {
        after
    }


async def test_the_store_deletes_one_parents_one_generation_and_nothing_else(
    factory, tmp_path  # noqa: F811
):
    """The probe behind ruling (i), on the store itself (chromadb 1.5.8)."""
    world = _world(factory, tmp_path)
    other = item_id_from_runbook_id("generation-2388-other")

    def chunk(chunk_id, parent, generation=None):
        metadata = {
            "scope": "global",
            "enterprise_id": STANDALONE_ENTERPRISE_ID,
            "parent_document_id": parent,
        }
        if generation:
            metadata["vector_generation"] = generation
        return {"id": chunk_id, "content": chunk_id, "metadata": metadata}

    rows = [
        chunk(f"{DOC}_chunk_0_g1", DOC, "g1"),
        chunk(f"{DOC}_chunk_1_g1", DOC, "g1"),
        chunk(f"{DOC}_chunk_0_g2", DOC, "g2"),
        chunk(f"{other}_chunk_0_g1", other, "g1"),
        chunk(f"{DOC}_chunk_0", DOC),
    ]
    await world.store.add_documents(rows, embeddings=[list(Q) for _ in rows])

    deleted = await world.store.delete_generation(DOC, "g1")

    assert sorted(deleted) == [f"{DOC}_chunk_0_g1", f"{DOC}_chunk_1_g1"]
    assert set(_all_chunk_ids(world, DOC)) == {f"{DOC}_chunk_0_g2", f"{DOC}_chunk_0"}
    assert _all_chunk_ids(world, other) == [f"{other}_chunk_0_g1"]
    assert await world.store.delete_generation(DOC, "no-such-generation") == []
    for parent, generation in ((DOC, ""), ("", "g2"), (DOC, None)):
        with pytest.raises(ValueError, match="never deletes by parent alone"):
            await world.store.delete_generation(parent, generation)


async def test_a_pack_write_of_no_chunks_raises_before_any_row(
    factory, tmp_path
):  # noqa: F811
    world = _world(factory, tmp_path)
    with pytest.raises(Exception, match="0 chunks"):
        await world.service.ingest_runbook(
            document_id=DOC,
            title="Runbook empty",
            content=_text("v0"),
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            scope="global",
            prechunked=[],
            shipped=True,
        )
    assert await _row(world) is None


# --- every door admits only the row's generation -----------------------------


@pytest.fixture
async def mixed(factory, tmp_path):  # noqa: F811
    """One live row whose chunk (``served``) sits below a superseded
    generation's chunk and a chunk written before #2388, both at ``Q``."""
    world = _world(factory, tmp_path)
    await world.service.ingest_runbook(
        document_id=DOC,
        title="Runbook served",
        content=_text("served"),
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope="global",
        prechunked=[(_text("served"), list(REAL_VECTORS[0]))],
        shipped=True,
    )
    now = datetime.now(timezone.utc).isoformat()
    await world.service._index_document_in_vector_store(
        KnowledgeBaseDocument(
            document_id=DOC,
            title="Runbook superseded",
            content=_text("superseded"),
            document_type="runbook",
            scope="global",
            created_at=now,
            updated_at=now,
        ),
        prechunked=[(_text("superseded"), list(Q))],
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        generation=SUPERSEDED,
    )
    await world.store.add_documents(
        [
            {
                "id": f"{DOC}_chunk_0",
                "content": _text("legacy"),
                "metadata": {
                    "document_type": "runbook",
                    "scope": "global",
                    "enterprise_id": STANDALONE_ENTERPRISE_ID,
                    "title": "Runbook legacy",
                    "parent_document_id": DOC,
                },
            }
        ],
        embeddings=[list(Q)],
    )
    raw = world.chroma.get_collection(KB_COLLECTION).query(
        query_embeddings=[list(Q)], n_results=2, include=["documents"]
    )
    assert _markers(" ".join(raw["documents"][0])) == {"superseded", "legacy"}
    return world


async def test_the_push_serves_only_the_rows_generation(mixed):
    results = await mixed.service.search_knowledge(
        QUERY, limit=3, filters=SCOPE, min_score=0.5
    )
    assert [r.parent_document_id for r in results] == [DOC]
    assert _markers(" ".join(r.snippet for r in results)) == {"served"}


async def test_search_documents_serves_only_the_rows_generation(mixed):
    found = await mixed.service.search_documents(
        QUERY, limit=3, user=SimpleNamespace(user_id=OWNER, enterprise_id=None)
    )
    assert [r["document_id"] for r in found["results"]] == [DOC]
    assert _markers(" ".join(r["content"] for r in found["results"])) == {"served"}


async def test_kb_qa_hands_only_the_rows_generation_to_synthesis(mixed):
    router = _CapturingRouter()
    result = await _registered_kb_qa(mixed, router).execute_with_context(
        {"question": QUERY}, _context()
    )
    assert result.success, result.error
    assert _markers("\n".join([result.data, *router.prompts])) == {"served"}


async def test_dedup_matches_on_the_rows_generation_only(mixed):
    """The superseded and legacy chunks carry ``Q`` (cosine 1.0); the served
    one does not, so the match's score says which chunk dedup counted."""
    matches = await RunbookKnowledgeBase.over_kb_collection(
        mixed.chroma, admit=mixed.service.admitted_generations
    ).search_by_text(QUERY, scope_filter=SCOPE, top_k=5)
    assert [m.item_id for m in matches] == [DOC]
    assert matches[0].similarity_score < 0.99


# --- a row with no chunk of its generation -----------------------------------


async def _forget_the_generation(world, item_id: str) -> None:
    """What migration 022 leaves every row with: a generation no chunk carries."""
    async with world.factory() as session:
        await session.execute(
            update(KnowledgeItemModel)
            .where(KnowledgeItemModel.item_id == item_id)
            .values(vector_generation="0f" * 16)
        )
        await session.commit()


async def _boot(world, pack_dir) -> kb_init.BootstrapResult:
    return await kb_init.bootstrap_kb(
        knowledge_service=world.service,
        db_session_factory=world.factory,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        project_root=pack_dir.parent,
        pack_dir=pack_dir,
    )


@pytest.fixture
def pack_dir(tmp_path):
    from tests.unit.bootstrap.test_kb_init import _write_pack

    return _write_pack(tmp_path)


def _embedder(monkeypatch, *, available: bool = True) -> None:
    async def embed(texts, **_kwargs):
        if not available:
            raise KnowledgeBaseError(
                "embedder down", error_code="KNOWLEDGE_EMBEDDING_UNAVAILABLE"
            )
        return [_unit(0.0, 1.0) for _ in texts]

    monkeypatch.setattr(embedding_guard, "embed_texts_or_raise", embed)


async def _authored(world, item_id: str) -> None:
    async with world.factory() as session:
        await DatabaseKnowledgeItemRepository(session).create(
            KnowledgeItem(
                item_id=item_id,
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                title="Authored",
                content="# Authored\n\nRaise the pool size.",
                item_type=KnowledgeItemType.RUNBOOK,
                scope=KnowledgeScope.GLOBAL,
            )
        )


async def test_a_pack_row_no_chunk_serves_is_re_ingested_with_a_matching_hash(
    factory, tmp_path, pack_dir  # noqa: F811
):
    world = _world(factory, tmp_path)
    first = await _boot(world, pack_dir)
    (shipped,) = [p for p in _chunk_parents(world) if p.startswith("kb_")]
    await _forget_the_generation(world, shipped)
    assert await world.service.admitted_generations([shipped]) == {shipped: "0f" * 16}

    second = await _boot(world, pack_dir)

    assert first.ingested == second.ingested == ["global/example.md"]
    row = await _row(world, shipped)
    assert row.vector_generation != "0f" * 16
    assert row.vector_generation in {
        chunk_generation_from_id(c) for c in _all_chunk_ids(world, shipped)
    }
    assert second.unserved_rows == [] and "unserved_rows=0" in repr(second)


@pytest.mark.parametrize("embedder", ["available", "down"])
async def test_an_authored_row_no_chunk_serves_goes_to_repair_or_is_counted(
    factory, tmp_path, pack_dir, monkeypatch, embedder  # noqa: F811
):
    world = _world(factory, tmp_path)
    await _boot(world, pack_dir)
    authored = "kb_" + "9" * 16
    await _authored(world, authored)
    _embedder(monkeypatch, available=embedder == "available")

    result = await _boot(world, pack_dir)

    if embedder == "available":
        assert result.repaired_rows == [authored]
        assert result.unserved_rows == []
        row = await _row(world, authored)
        assert {
            chunk_generation_from_id(c) for c in _all_chunk_ids(world, authored)
        } == {row.vector_generation}
    else:
        assert result.repaired_rows == []
        assert result.unserved_rows == [authored]
        assert "unserved_rows=1)" in repr(result)


async def test_an_edited_built_in_no_chunk_serves_is_repaired_not_re_ingested(
    factory, tmp_path, pack_dir, monkeypatch  # noqa: F811
):
    """Re-ingesting it from the pack would replace the operator's edit with the
    pack's text (#1966): the repair re-embeds the row as it stands."""
    world = _world(factory, tmp_path)
    await _boot(world, pack_dir)
    (shipped,) = [p for p in _chunk_parents(world) if p.startswith("kb_")]
    edited = "# Edited by an operator\n\nRestart the pool, then verify."
    async with world.factory() as session:
        await session.execute(
            update(KnowledgeItemModel)
            .where(KnowledgeItemModel.item_id == shipped)
            .values(content=edited)
        )
        await session.commit()
    await _forget_the_generation(world, shipped)
    _embedder(monkeypatch)

    result = await _boot(world, pack_dir)

    assert result.ingested == [] and result.skipped_unchanged == ["global/example.md"]
    assert result.repaired_rows == [shipped] and result.unserved_rows == []
    row = await _row(world, shipped)
    assert row.content == edited and row.vector_generation == "0f" * 16
    got = world.chroma.get_collection(KB_COLLECTION).get(
        ids=_all_chunk_ids(world, shipped), include=["documents"]
    )
    served = [
        document
        for chunk_id, document in zip(got["ids"], got["documents"])
        if chunk_generation_from_id(chunk_id) == row.vector_generation
    ]
    assert served and all("Edited by an operator" in d for d in served)


async def test_an_update_between_the_row_read_and_the_listing_is_not_unserved(
    factory, tmp_path, monkeypatch  # noqa: F811
):
    """The reconcile reads the rows, then lists the chunks. An update that
    commits between the two, and deletes the generation it replaced, leaves no
    chunk of the generation the first read saw; the re-read sees the new one,
    so the row is not reported unserved (and not re-embedded)."""
    world = _world(factory, tmp_path)
    authored = "kb_" + "5" * 16
    await _authored(world, authored)
    _embedder(monkeypatch)
    await world.service.reindex_missing_vectors(authored)
    real_list = world.store.list_all_chunk_ids_by_parent
    real_read = kb_init._read_rows
    reads: List[Optional[List[str]]] = []

    async def read(factory_, item_ids=None):
        reads.append(item_ids)
        return await real_read(factory_, item_ids)

    async def an_update_lands_then_list():
        await world.service.update_document_metadata(
            authored, content="# Authored\n\nRaise the pool size, then restart."
        )
        return await real_list()

    with (
        patch.object(kb_init, "_read_rows", read),
        patch.object(
            world.store,
            "list_all_chunk_ids_by_parent",
            side_effect=an_update_lands_then_list,
        ),
    ):
        result = await kb_init._reconcile_vectors(world.service, world.factory)

    assert reads == [None, [authored]], "the row was never a candidate"
    assert result.unserved_rows == []


async def test_a_repair_re_run_under_the_same_generation_is_idempotent(
    factory, tmp_path, monkeypatch  # noqa: F811
):
    """No pre-delete: ChromaDB's ``add`` keeps the chunk already stored under an
    id and adds the rest, and one generation's content is one text."""
    world = _world(factory, tmp_path)
    authored = "kb_" + "8" * 16
    await _authored(world, authored)
    _embedder(monkeypatch)

    assert await world.service.reindex_missing_vectors(authored) > 0
    once = world.chroma.get_collection(KB_COLLECTION).get(
        ids=_all_chunk_ids(world, authored), include=["documents", "metadatas"]
    )
    assert await world.service.reindex_missing_vectors(authored) > 0
    twice = world.chroma.get_collection(KB_COLLECTION).get(
        ids=_all_chunk_ids(world, authored), include=["documents", "metadatas"]
    )

    assert twice == once


def _all_chunk_ids(world, parent: str) -> List[str]:
    got = world.chroma.get_collection(KB_COLLECTION).get(include=[])
    return sorted(c for c in got["ids"] if c.startswith(f"{parent}_chunk_"))


def _chunk_parents(world) -> set[str]:
    got = world.chroma.get_collection(KB_COLLECTION).get(include=[])
    return {c.rsplit("_chunk_", 1)[0] for c in got["ids"]}


# --- PostgreSQL ---------------------------------------------------------------


async def _pg_factory(url: str):
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        await seed_enterprises(session, [STANDALONE_ENTERPRISE_ID])
    return engine, sessions


@pytest.mark.postgres
@POSTGRES
@pytest.mark.parametrize("order", ORDERS)
async def test_two_pack_versions_at_once_on_postgresql(tmp_path, order):
    async with database_of_its_own(os.environ["DATABASE_URL"], "fm_2388") as url:
        result = alembic_on(url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        engine, sessions = await _pg_factory(url)
        try:
            world = _world(sessions, tmp_path)
            winner, loser, old, _error = await _race(world, order)
            await _assert_only_the_winner_is_served(world, winner, loser, old)
        finally:
            await engine.dispose()


@pytest.mark.postgres
@POSTGRES
async def test_a_replacement_waits_on_a_peers_and_then_conflicts(tmp_path):
    """Both observed v0. A deletes v0 and inserts vA and holds its commit; B's
    guarded DELETE waits on A's lock, matches nothing once A commits, and B's
    INSERT conflicts: vA stands, and B's transaction committed nothing."""
    async with database_of_its_own(os.environ["DATABASE_URL"], "fm_2388") as url:
        result = alembic_on(url, "upgrade head")
        assert result.returncode == 0, result.stderr[-2000:]
        engine, sessions = await _pg_factory(url)
        try:
            world = _world(sessions, tmp_path)
            await _seed(world, _pack("v0", _unit(0.8, 0.6)))
            old = (await _row(world)).vector_generation
            a, b = _pack("vA", REAL_VECTORS[0]), _pack("vB", REAL_VECTORS[1])
            held, release = asyncio.Event(), asyncio.Event()
            holder: Dict[str, Optional[int]] = {"pid": None}
            real_commit = DatabaseKnowledgeItemRepository.commit

            async def commit(self):
                if holder["pid"] is None:
                    holder["pid"] = await self.db.scalar(select(func.pg_backend_pid()))
                    held.set()
                    await release.wait()
                return await real_commit(self)

            with patch.object(DatabaseKnowledgeItemRepository, "commit", commit):
                first = asyncio.create_task(_seed(world, a))
                await held.wait()
                second = asyncio.create_task(_seed(world, b))
                await _until_blocked_by(sessions, holder["pid"])
                assert not second.done()
                release.set()
                assert await first == "ingested"
                with pytest.raises(ValueError, match="already exists"):
                    await second
            await _assert_only_the_winner_is_served(world, a, b, old)
        finally:
            await engine.dispose()


async def _until_blocked_by(sessions, backend: int) -> None:
    """Poll until a session waits on ``backend``'s lock (a condition; bounded
    only so a defect fails rather than hangs)."""
    for _ in range(500):
        async with sessions() as session:
            waiting = await session.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE :pid = ANY(pg_blocking_pids(pid))"
                ),
                {"pid": backend},
            )
        if waiting:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the second replacement never waited on the first's lock")


@pytest.mark.postgres
@POSTGRES
async def test_migration_022_fills_each_row_then_steps_back_on_postgresql():
    """Ruling (c): each existing row gets its own generation, which no chunk
    carries; the column is then NOT NULL; the downgrade drops it alone."""
    async with database_of_its_own(os.environ["DATABASE_URL"], "fm_022") as url:
        result = alembic_on(url, "upgrade d0eb318ce3e0")  # pragma: allowlist secret
        assert result.returncode == 0, result.stderr[-2000:]
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                enterprise = (
                    await conn.execute(text("SELECT enterprise_id FROM enterprises"))
                ).scalar()
                for item_id in ("kb_aaaaaaaaaaaa", "kb_bbbbbbbbbbbb"):
                    await conn.execute(
                        text(
                            "INSERT INTO knowledge_items (item_id, enterprise_id, "
                            "title, content, item_type) VALUES (:i, :e, 't', 'c', "
                            "'runbook')"
                        ),
                        {"i": item_id, "e": enterprise},
                    )
            assert alembic_on(url, "upgrade head").returncode == 0
            async with engine.connect() as conn:
                generations = (
                    (
                        await conn.execute(
                            text("SELECT vector_generation FROM knowledge_items")
                        )
                    )
                    .scalars()
                    .all()
                )
                nullable = (
                    await conn.execute(
                        text(
                            "SELECT is_nullable FROM information_schema.columns "
                            "WHERE table_name = 'knowledge_items' "
                            "AND column_name = 'vector_generation'"
                        )
                    )
                ).scalar()
            assert len(set(generations)) == 2
            assert all(len(g) == 32 for g in generations)
            assert nullable == "NO"

            down = alembic_on(url, "downgrade d0eb318ce3e0")  # pragma: allowlist secret
            assert down.returncode == 0, down.stderr[-2000:]
            async with engine.connect() as conn:
                columns = (
                    (
                        await conn.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_name = 'knowledge_items'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                count = (
                    await conn.execute(text("SELECT count(*) FROM knowledge_items"))
                ).scalar()
            assert "vector_generation" not in columns
            assert count == 2
        finally:
            await engine.dispose()
