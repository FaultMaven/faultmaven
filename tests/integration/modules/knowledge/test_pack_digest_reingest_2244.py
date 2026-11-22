"""A pack runbook is re-ingested when its chunks or pack-written fields change
under the same markdown (#2244).

The pack bootstrap identified a runbook version by its markdown alone
(``metadata.pack_content_hash``), so a re-vendored pack whose chunk texts,
vectors or manifest fields changed was skipped on every deployment holding
the runbook, and the store kept serving the old chunks. A row now also
carries ``metadata.pack_digest``; a row whose digest is not the pack's has
its stored chunks compared with the pack's. A chunks-only change never
republishes an unpublished row and never replaces an edit (#1966).

Everything is real but the embedder: FK-on SQLite, an on-disk ChromaDB, the
production ``KnowledgeVectorStore``, ``KnowledgeService`` and ``kb_init``.
Each test's docstring says whether it fails on ``c21fdc5b1`` or is a pin,
with the mutation that breaks the pin.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import List
from unittest.mock import patch

import pytest
from sqlalchemy import event, update

from faultmaven.bootstrap import kb_init
from faultmaven.bootstrap.kb_pack import KbPack, PackChunk, PackRunbook
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.knowledge.knowledge_vector_store import KB_COLLECTION
from faultmaven.infrastructure.persistence.models import KnowledgeItemModel
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    PACK_CONTENT_HASH_KEY,
    PACK_DIGEST_KEY,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)
from faultmaven.utils.serialization import decode_json_blob
from tests.integration.modules.knowledge.test_row_checked_retrieval_2397 import (
    REAL_VECTORS,
    _fixed_query_embedding,  # noqa: F401 - autouse: every query embeds to Q
    factory,  # noqa: F401 - the FK-on SQLite world
)
from tests.integration.modules.knowledge.test_vector_generation_2388 import (
    DOC,
    _boot,
    _chunks,
    _embedder,
    _row,
    _served,
    _text,
    _world,
    pack_dir,  # noqa: F401 - a two-chunk pack on disk
)

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

MD = "# Pool exhaustion\n\nRaise the pool size, then restart the service.\n"
MD_HASH = hashlib.sha256(MD.encode("utf-8")).hexdigest()


def _pack(
    marker: str = "v0",
    vector: List[float] = REAL_VECTORS[0],
    *,
    content: str = MD,
    title: str = "Pool exhaustion",
    tags: tuple = ("postgres",),
) -> PackRunbook:
    """One runbook, one chunk: the markdown is ``content`` whatever the chunk."""
    return PackRunbook(
        item_id=DOC,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        title=title,
        scope="global",
        relpath="global/pool.md",
        content=content,
        tags=list(tags),
        source_url=None,
        owner_id=None,
        team_id=None,
        chunks=[PackChunk(chunk_index=0, text=_text(marker), embedding=list(vector))],
    )


async def _ingest(world, runbook, *, role_may_update=True, served=True) -> str:
    """One runbook through ``_ingest_pack_runbook``, with the store listing
    the pass reads first (``served=False``: that listing failed)."""
    listing = await kb_init._list_chunk_ids(world.service)
    return await kb_init._ingest_pack_runbook(
        runbook=runbook,
        knowledge_service=world.service,
        db_session_factory=world.factory,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        role_may_update=role_may_update,
        served=kb_init._generations_by_parent(listing) if served else None,
    )


async def _metadata(world, item_id: str = DOC) -> dict:
    return decode_json_blob((await _row(world, item_id)).knowledge_metadata)


async def _restamp(world, metadata: dict, item_id: str = DOC) -> None:
    """The stamps an older version left: bookkeeping, not an edit."""
    async with world.factory() as session:
        await DatabaseKnowledgeItemRepository(session).update_fields(
            item_id, {"metadata": metadata}, touch=False
        )


async def _unpublish(world) -> None:
    async with world.factory() as session:
        await session.execute(
            update(KnowledgeItemModel)
            .where(KnowledgeItemModel.item_id == DOC)
            .values(is_published=False)
        )
        await session.commit()


def _stored_vector(world, generation: str) -> List[float]:
    got = world.chroma.get_collection(KB_COLLECTION).get(
        where={"vector_generation": generation}, include=["embeddings"]
    )
    (vector,) = got["embeddings"]
    return [float(x) for x in vector]


# --- the defect: a changed chunk under the same markdown ----------------------


async def test_a_changed_chunk_text_and_vector_are_re_ingested(
    factory, tmp_path  # noqa: F811
):
    """Fails on c21fdc5b1: the second boot skipped the row."""
    world = _world(factory, tmp_path)
    assert await _ingest(world, _pack("v0", REAL_VECTORS[0])) == "ingested"
    old = (await _row(world)).vector_generation
    revised = _pack("vA", REAL_VECTORS[1])

    assert await _ingest(world, revised) == "ingested"

    assert (await _row(world)).vector_generation != old
    assert await _served(world) == {"vA"}
    assert (await _metadata(world))[PACK_DIGEST_KEY] == revised.pack_digest
    assert await _ingest(world, revised) == "skipped"


async def test_a_changed_vector_alone_is_re_ingested(factory, tmp_path):  # noqa: F811
    """A pairing repair: the same text, a corrected vector. Fails on
    c21fdc5b1."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack("v0", REAL_VECTORS[0]))

    assert await _ingest(world, _pack("v0", REAL_VECTORS[2])) == "ingested"

    row = await _row(world)
    assert _stored_vector(world, row.vector_generation) == pytest.approx(
        REAL_VECTORS[2], rel=1e-6
    )


@pytest.mark.parametrize(
    "manifest", [{"title": "Pool exhaustion, revised"}, {"tags": ("postgres", "pool")}]
)
async def test_a_manifest_field_change_alone_is_re_ingested(
    factory, tmp_path, manifest  # noqa: F811
):
    """The same markdown and chunks, a new title or tags in the manifest (A5).
    Fails on c21fdc5b1."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack())

    assert await _ingest(world, _pack(**manifest)) == "ingested"

    row = await _row(world)
    assert row.title == manifest.get("title", "Pool exhaustion")
    assert list(row.tags) == list(manifest.get("tags", ("postgres",)))


async def test_an_unchanged_pack_is_skipped_without_reading_the_store(
    factory, tmp_path  # noqa: F811
):
    """Pin: both stamps on the row, nothing compared or written. Mutation: the
    INSERT omits ``pack_digest`` (``ingest_runbook``'s metadata) and the second
    boot reads the store."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack())
    before = await _row(world)

    with patch.object(
        world.store, "get_generation_chunks", wraps=world.store.get_generation_chunks
    ) as read:
        assert await _ingest(world, _pack()) == "skipped"

    read.assert_not_awaited()
    after = await _row(world)
    assert (after.vector_generation, after.created_at) == (
        before.vector_generation,
        before.created_at,
    )


# --- #1966: a chunks-only change never republishes or replaces an edit --------


async def test_a_chunks_only_change_leaves_an_unpublished_row_retired(
    factory, tmp_path  # noqa: F811
):
    """Pin. Mutation: drop ``is_published`` from the re-ingest condition in
    ``_ingest_pack_runbook``: the re-ingest is attempted."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack("v0"))
    old = (await _row(world)).vector_generation
    await _unpublish(world)

    with patch.object(
        world.service, "ingest_runbook", wraps=world.service.ingest_runbook
    ) as ingest:
        assert await _ingest(world, _pack("vA", REAL_VECTORS[1])) == "skipped"

    ingest.assert_not_awaited()
    row = await _row(world)
    assert row.is_published is False and row.vector_generation == old


async def test_a_chunks_only_change_keeps_a_content_edit(
    factory, tmp_path, monkeypatch  # noqa: F811
):
    """Pin. Mutation: drop ``not edited`` from the re-ingest condition: the
    pack's text replaces the edit."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack("v0"))
    _embedder(monkeypatch)
    edited = "# Pool exhaustion\n\nEdited: cap the pool, then restart.\n"
    await world.service.update_document_metadata(
        DOC, principal="user-1", content=edited
    )
    generation = (await _row(world)).vector_generation

    assert await _ingest(world, _pack("vA", REAL_VECTORS[1])) == "skipped"

    row = await _row(world)
    assert (row.content, row.vector_generation) == (edited, generation)


@pytest.mark.parametrize("stamps", ["both", "content_only"])
@pytest.mark.parametrize("edit", [{"title": "Operator title"}, {"tags": ["edited"]}])
async def test_a_title_or_tag_edit_survives_a_chunks_only_change(
    factory, tmp_path, edit, stamps  # noqa: F811
):
    """A1: an edit writes no new generation, so the content hash alone read it
    as unedited. Pin, on the stamped row and on the upgrade boot's legacy row.
    Mutation: ``fields_digest`` over the content alone: the pack's title or
    tags replace the edit."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack("v0"))
    await world.service.update_document_metadata(DOC, principal="user-1", **edit)
    if stamps == "content_only":
        await _restamp(world, {PACK_CONTENT_HASH_KEY: MD_HASH})
    generation = (await _row(world)).vector_generation

    assert await _ingest(world, _pack("vA", REAL_VECTORS[1])) == "skipped"

    row = await _row(world)
    assert row.vector_generation == generation
    assert (row.title, list(row.tags)) == (
        edit.get("title", "Pool exhaustion"),
        edit.get("tags", ["postgres"]),
    )


async def test_an_unpublish_after_the_read_is_not_republished(
    factory, tmp_path  # noqa: F811
):
    """A3: the unpublish lands between the row read and the replacing
    transaction. Pin. Mutation: drop ``only_published`` from the replacing
    DELETE: the row is replaced, published."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack("v0"))
    old = (await _row(world)).vector_generation
    real = KnowledgeService._insert_pack_row

    async def unpublished_first(self, item, **kwargs):
        await _unpublish(world)
        return await real(self, item, **kwargs)

    with patch.object(KnowledgeService, "_insert_pack_row", unpublished_first):
        assert await _ingest(world, _pack("vA", REAL_VECTORS[1])) == "skipped"

    row = await _row(world)
    assert row.is_published is False and row.vector_generation == old
    # The skipped write's vectors are deleted with it.
    assert sorted(_chunks(world).values()) == ["v0"]


async def test_a_peer_that_ingested_this_version_first_is_a_skip(
    factory, tmp_path  # noqa: F811
):
    """A4: two replicas replace the same row with the same new version; the
    one whose DELETE misses is skipped, not failed, and leaves no vector.
    Fails on c21fdc5b1: the second raised ``already exists``."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack("v0"))
    revised = _pack("vA", REAL_VECTORS[1], content=MD + "\nRevised.\n")
    gates: List[asyncio.Event] = []
    real = KnowledgeService._insert_pack_row

    async def at_the_boundary(self, item, **kwargs):
        gate = asyncio.Event()
        gates.append(gate)
        await gate.wait()
        return await real(self, item, **kwargs)

    with patch.object(KnowledgeService, "_insert_pack_row", at_the_boundary):
        tasks = [asyncio.create_task(_ingest(world, revised)) for _ in range(2)]
        while len(gates) < 2:
            await asyncio.sleep(0)
        gates[0].set()
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        assert [t.result() for t in done] == ["ingested"]
        gates[1].set()
        outcomes = await asyncio.gather(*tasks)

    assert sorted(outcomes) == ["ingested", "skipped"]
    assert (await _metadata(world))[PACK_DIGEST_KEY] == revised.pack_digest
    assert sorted(_chunks(world).values()) == ["vA"]


# --- rows stamped before the digest existed -----------------------------------


async def test_a_legacy_row_with_changed_chunks_is_re_ingested_once(
    factory, tmp_path  # noqa: F811
):
    """The content stamp alone, the pack's chunks changed: re-ingested, then
    skipped. Fails on c21fdc5b1."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack("v0"))
    await _restamp(world, {PACK_CONTENT_HASH_KEY: MD_HASH})
    revised = _pack("vA", REAL_VECTORS[1])

    assert await _ingest(world, revised) == "ingested"
    assert await _ingest(world, revised) == "skipped"
    assert await _served(world) == {"vA"}


@pytest.mark.parametrize("stamps", ["content_only", "none"])
@pytest.mark.parametrize("published", [True, False])
async def test_a_legacy_row_with_equal_chunks_is_stamped_not_rewritten(
    factory, tmp_path, stamps, published  # noqa: F811
):
    """A2/A6: a row stamped before the digest existed (or before any stamp),
    published or retired, whose stored chunks are the pack's: the digest is
    stamped and nothing else is written. Fails on c21fdc5b1 (no digest)."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack())
    if not published:
        await _unpublish(world)
    await _restamp(
        world, {PACK_CONTENT_HASH_KEY: MD_HASH} if stamps == "content_only" else {}
    )
    before = await _row(world)

    assert await _ingest(world, _pack()) == "skipped"

    after = await _row(world)
    assert (after.vector_generation, after.created_at, after.is_published) == (
        before.vector_generation,
        before.created_at,
        published,
    )
    assert await _metadata(world) == {
        PACK_CONTENT_HASH_KEY: MD_HASH,
        PACK_DIGEST_KEY: _pack().pack_digest,
    }


async def test_a_failed_listing_re_ingests_a_legacy_row(
    factory, tmp_path  # noqa: F811
):
    """``served`` None: the compare cannot read, so a published, unedited row
    whose digest differs is re-ingested under the guards. Fails on
    c21fdc5b1."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack())
    await _restamp(world, {PACK_CONTENT_HASH_KEY: MD_HASH})
    old = (await _row(world)).vector_generation

    assert await _ingest(world, _pack(), served=False) == "ingested"

    assert (await _row(world)).vector_generation != old
    assert (await _metadata(world))[PACK_DIGEST_KEY] == _pack().pack_digest


async def test_the_upgrade_boot_of_an_unchanged_pack_rewrites_no_row(
    factory, tmp_path, pack_dir  # noqa: F811
):
    """A2: the first full pass after the upgrade finds every pack row without
    a digest. It compares, stamps, and rewrites nothing. Fails on c21fdc5b1
    (no digest is stamped)."""
    world = _world(factory, tmp_path)
    await _boot(world, pack_dir)
    (shipped,) = [r.item_id for r in KbPack.load(pack_dir).runbooks]
    await _restamp(
        world,
        {PACK_CONTENT_HASH_KEY: KbPack.load(pack_dir).runbooks[0].content_hash},
        shipped,
    )
    before = await _row(world, shipped)

    result = await _boot(world, pack_dir)

    assert result.ingested == [] and result.skipped_unchanged == ["global/example.md"]
    after = await _row(world, shipped)
    assert (after.vector_generation, after.created_at) == (
        before.vector_generation,
        before.created_at,
    )
    assert (await _metadata(world, shipped))[PACK_DIGEST_KEY] == (
        KbPack.load(pack_dir).runbooks[0].pack_digest
    )


# --- kb_seed: no UPDATE --------------------------------------------------------


async def test_under_kb_seed_a_chunks_only_change_re_ingests_with_no_update(
    factory, tmp_path  # noqa: F811
):
    """The maintenance role holds no UPDATE on ``knowledge_items``. A chunks-
    only change is re-ingested by DELETE + INSERT, and a legacy row whose
    chunks compare equal is re-compared each run, never stamped. Every
    statement is captured. Fails on c21fdc5b1 (the row was skipped)."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack("v0"))
    statements: List[str] = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = factory.kw["bind"]
    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        revised = _pack("vA", REAL_VECTORS[1])
        assert await _ingest(world, revised, role_may_update=False) == "ingested"
        await _restamp_without_capture(world, statements)
        assert await _ingest(world, revised, role_may_update=False) == "skipped"
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)

    assert [s for s in statements if s.lstrip().upper().startswith("UPDATE")] == []
    assert any(s.lstrip().upper().startswith("DELETE") for s in statements)
    assert await _metadata(world) == {PACK_CONTENT_HASH_KEY: MD_HASH}


async def _restamp_without_capture(world, statements: List[str]) -> None:
    """Make the row legacy (content stamp only) as an older version left it;
    this test's own UPDATE is not the pass's."""
    seen = len(statements)
    await _restamp(world, {PACK_CONTENT_HASH_KEY: MD_HASH})
    del statements[seen:]
