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
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import List
from unittest.mock import patch

import pytest
from sqlalchemy import event, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.bootstrap import kb_init
from faultmaven.bootstrap.kb_pack import KbPack, PackChunk, PackRunbook
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.knowledge.knowledge_vector_store import KB_COLLECTION
from faultmaven.infrastructure.persistence.models import Base, KnowledgeItemModel
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
    OWNER,
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
from tests.utils import seed_enterprises

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
    """A retirement that leaves ``updated_at`` alone, so only the published
    guard can see it."""
    async with world.factory() as session:
        await session.execute(
            update(KnowledgeItemModel)
            .where(KnowledgeItemModel.item_id == DOC)
            .values(is_published=False, updated_at=KnowledgeItemModel.updated_at)
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


TITLE, TAGS = {"title": "Operator title"}, {"tags": ["edited"]}


@pytest.mark.parametrize(
    ("edit", "legacy"),
    [
        pytest.param(TITLE, False, id="title"),
        pytest.param(TAGS, False, id="tags"),
        pytest.param({"document_type": "troubleshooting_guide"}, False, id="item_type"),
        pytest.param({"version": "2"}, False, id="metadata_version"),
        pytest.param(TITLE, True, id="title-legacy"),
        pytest.param(TAGS, True, id="tags-legacy"),
    ],
)
async def test_an_edit_survives_a_chunks_only_change(
    factory, tmp_path, edit, legacy  # noqa: F811
):
    """A1, and review item 4: an edit writes no new generation, so the content
    hash alone read it as unedited. Pin (title, tags). Mutation:
    ``fields_digest`` over the content alone. ``item_type`` and a metadata key
    fail on e8cfe73e: the re-ingest wrote RUNBOOK back and dropped the key.
    ``*-legacy`` (a row stamped before the digest, so judged by
    ``updated_at`` and content) fails on b1893598: the edit was replaced."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack("v0"))
    await world.service.update_document_metadata(DOC, principal="user-1", **edit)
    if legacy:
        await _restamp(world, {PACK_CONTENT_HASH_KEY: MD_HASH})
    before = await _row(world)
    generation = (await _row(world)).vector_generation

    assert await _ingest(world, _pack("vA", REAL_VECTORS[1])) == "skipped"

    row = await _row(world)
    assert row.vector_generation == generation
    assert (row.title, list(row.tags), row.item_type, row.knowledge_metadata) == (
        before.title,
        list(before.tags),
        before.item_type,
        before.knowledge_metadata,
    )


async def _a_change_after_the_read(world, change) -> SimpleNamespace:
    """A chunks-only re-ingest with ``change`` landing between the row read
    and the replacing transaction."""
    await _ingest(world, _pack("v0"))
    old = (await _row(world)).vector_generation
    real = KnowledgeService._insert_pack_row

    async def changed_first(self, item, **kwargs):
        await change()
        return await real(self, item, **kwargs)

    with patch.object(KnowledgeService, "_insert_pack_row", changed_first):
        assert await _ingest(world, _pack("vA", REAL_VECTORS[1])) == "skipped"
    # The skipped write's vectors are deleted with it.
    assert sorted(_chunks(world).values()) == ["v0"]
    return SimpleNamespace(old=old, row=await _row(world))


async def _an_unpublish_after_the_read(world) -> None:
    got = await _a_change_after_the_read(world, lambda: _unpublish(world))
    assert got.row.is_published is False and got.row.vector_generation == got.old


async def test_an_unpublish_after_the_read_is_not_republished(
    factory, tmp_path  # noqa: F811
):
    """A3. Pin. Mutation: drop ``is_published`` from the replacing DELETE's
    guard: the row is replaced, published."""
    await _an_unpublish_after_the_read(_world(factory, tmp_path))


async def test_an_edit_after_the_read_is_kept(factory, tmp_path):  # noqa: F811
    """Review item 2: a title edit moves neither the generation nor
    ``is_published``. Fails on e8cfe73e (the edit was replaced)."""
    world = _world(factory, tmp_path)

    async def edit():
        async with world.factory() as session:
            await DatabaseKnowledgeItemRepository(session).update_fields(
                DOC, {"title": "Operator title"}
            )

    got = await _a_change_after_the_read(world, edit)
    assert (got.row.title, got.row.vector_generation) == ("Operator title", got.old)


async def test_a_peer_that_ingested_this_version_first_is_a_skip(
    factory, tmp_path  # noqa: F811
):
    """A4: two replicas replace the same row with the same new version; the
    one whose DELETE misses is skipped, not failed, and leaves no vector.
    Fails on c21fdc5b1: the second raised ``already exists``."""
    await _a_peer_wins(_world(factory, tmp_path))


async def _a_peer_wins(world) -> None:
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


# --- review fixes (#2981) -------------------------------------------------------


@pytest.mark.parametrize("upgrade_first", [True, False])
async def test_a_legacy_row_takes_a_pack_field_change(
    factory, tmp_path, upgrade_first  # noqa: F811
):
    """Review item 3: a legacy row is judged edited by its content, so a title
    the pack changed is the pack's, before or after the upgrade boot stamps
    it. ``False`` fails on e8cfe73e: stamped as edited, never re-ingested.
    ``True`` is a pin. Mutation: the compare alone decides staleness (a
    lagging field is not stale)."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack())
    await _restamp(world, {PACK_CONTENT_HASH_KEY: MD_HASH})
    if upgrade_first:
        assert await _ingest(world, _pack()) == "skipped"
    revised = _pack(title="Pool exhaustion, revised")

    assert await _ingest(world, revised) == "ingested"

    assert (await _row(world)).title == "Pool exhaustion, revised"
    assert await _ingest(world, revised) == "skipped"


async def test_a_peer_replacement_before_the_stamp_is_not_overwritten(
    factory, tmp_path  # noqa: F811
):
    """Review item 6: the stamp lands only on the row as read. Fails on
    e8cfe73e (the read's stamps overwrote the peer's row). Mutation: drop the
    generation from the stamp's guard."""
    world = _world(factory, tmp_path)
    await _ingest(world, _pack())
    await _restamp(world, {PACK_CONTENT_HASH_KEY: MD_HASH})
    peer = {PACK_CONTENT_HASH_KEY: "peer"}
    real = kb_init._stored_chunks_match

    async def a_peer_replaces_it(*args):
        async with world.factory() as session:
            await session.execute(
                update(KnowledgeItemModel)
                .where(KnowledgeItemModel.item_id == DOC)
                .values(
                    vector_generation="f" * 32,
                    knowledge_metadata=json.dumps(peer),
                    updated_at=KnowledgeItemModel.updated_at,
                )
            )
            await session.commit()
        return await real(*args)

    with patch.object(kb_init, "_stored_chunks_match", a_peer_replaces_it):
        assert await _ingest(world, _pack()) == "skipped"

    assert await _metadata(world) == peer


async def test_an_empty_pack_tag_is_not_an_edit(factory, tmp_path):  # noqa: F811
    """Review item 8: SQLite keeps no empty tag, so the stored row lacks one
    the pack carries. Fails on e8cfe73e: the row read as edited, so the later
    chunks-only change was skipped."""
    world = _world(factory, tmp_path)
    shipped = _pack(tags=("postgres", ""))
    await _ingest(world, shipped)
    await _restamp(world, {PACK_CONTENT_HASH_KEY: MD_HASH})
    generation = (await _row(world)).vector_generation

    assert await _ingest(world, shipped) == "skipped"

    assert (await _row(world)).vector_generation == generation
    assert (await _metadata(world))[PACK_DIGEST_KEY] == shipped.pack_digest
    revised = _pack("vA", REAL_VECTORS[1], tags=("postgres", ""))
    assert await _ingest(world, revised) == "ingested"


# --- PostgreSQL ------------------------------------------------------------------

POSTGRES = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
)


@asynccontextmanager
async def _pg_world(tmp_path):
    from tests.integration.security.conftest import database_of_its_own

    async with database_of_its_own(os.environ["DATABASE_URL"], "fm_2244") as url:
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False
            )
            async with sessions() as session:
                await seed_enterprises(session, [STANDALONE_ENTERPRISE_ID])
            yield url, _world(sessions, tmp_path)
        finally:
            await engine.dispose()


@pytest.mark.postgres
@POSTGRES
@pytest.mark.parametrize("race", ["unpublish", "peer"])
async def test_the_races_on_postgresql(tmp_path, race):
    """The unpublish race and the peer win, under PostgreSQL's locking."""
    async with _pg_world(tmp_path) as (_url, world):
        if race == "unpublish":
            await _an_unpublish_after_the_read(world)
        else:
            await _a_peer_wins(world)


@pytest.mark.postgres
@POSTGRES
async def test_kb_seed_under_its_real_role_needs_no_update(tmp_path, caplog):
    """The ``kb_seed`` maintenance role exactly as #1966's suite grants it
    (SELECT, INSERT and DELETE on ``knowledge_items``, no UPDATE): a
    chunks-only change re-ingests and a legacy row is compared, not stamped,
    with no statement refused."""
    role = f"fm_maint_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(
        os.environ["DATABASE_URL"], isolation_level="AUTOCOMMIT"
    )
    try:
        async with _pg_world(tmp_path) as (url, world):
            await _ingest(world, _pack("v0"))
            async with admin.connect() as conn:
                await conn.execute(
                    text(f"CREATE ROLE {role} LOGIN PASSWORD 'pw' BYPASSRLS")
                )
                db = make_url(url).database
                await conn.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO {role}'))
            async with world.factory() as session:
                for grant in (
                    f"GRANT USAGE ON SCHEMA public TO {role}",
                    f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}",
                    f"GRANT INSERT, DELETE ON TABLE knowledge_items TO {role}",
                ):
                    await session.execute(text(grant))
                await session.commit()
            maint = create_async_engine(make_url(url).set(username=role, password="pw"))
            try:
                sessions = async_sessionmaker(
                    maint, class_=AsyncSession, expire_on_commit=False
                )
                seed = SimpleNamespace(**{**vars(world), "factory": sessions})
                world.service._db_session_factory = sessions
                may_update = await kb_init._role_may_update(sessions)
                revised = _pack("vA", REAL_VECTORS[1])
                with caplog.at_level(logging.WARNING):
                    assert (
                        await _ingest(seed, revised, role_may_update=may_update)
                        == "ingested"
                    )
                    await _restamp(world, {PACK_CONTENT_HASH_KEY: MD_HASH})
                    assert (
                        await _ingest(seed, revised, role_may_update=may_update)
                        == "skipped"
                    )
            finally:
                await maint.dispose()
            assert may_update is False
            assert await _metadata(world) == {PACK_CONTENT_HASH_KEY: MD_HASH}
            assert await _served(world) == {"vA"}
            assert [
                r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
            ] == []
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


# --- delta review fixes (#2981, b1893598) ----------------------------------------


async def test_a_missed_replacement_deletes_its_vectors_outside_its_transaction(
    factory, tmp_path  # noqa: F811
):
    """Delta item 3: the miss rolls back before the Chroma delete, so another
    connection can write meanwhile. Mutation: drop the ``rollback`` in
    ``_insert_pack_row``: the probe finds the database locked."""
    world = _world(factory, tmp_path)
    probe = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'kb.db'}", connect_args={"timeout": 0.5}
    )
    real = KnowledgeService._delete_replaced_generation
    wrote: List[bool] = []

    async def probe_first(self, item_id, generation):
        try:
            async with probe.begin() as conn:
                await conn.execute(text("UPDATE knowledge_items SET view_count = 0"))
            wrote.append(True)
        except Exception:
            wrote.append(False)
        return await real(self, item_id, generation)

    try:
        with patch.object(KnowledgeService, "_delete_replaced_generation", probe_first):
            await _an_unpublish_after_the_read(world)
    finally:
        await probe.dispose()
    assert wrote == [True]


async def test_a_null_owner_on_a_personal_entry_is_not_an_edit(
    factory, tmp_path  # noqa: F811
):
    """Delta item 3: deleting the owner sets ``owner_id`` NULL (ON DELETE SET
    NULL); that is not an operator's edit. Mutation: ``fields_digest`` without
    the ``owner_id`` stand-in: the chunks-only change is skipped."""
    world = _world(factory, tmp_path)

    def personal(marker, vector):
        runbook = _pack(marker, vector)
        runbook.scope, runbook.owner_id = "personal", OWNER
        return runbook

    await _ingest(world, personal("v0", REAL_VECTORS[0]))
    async with world.factory() as session:
        await session.execute(
            update(KnowledgeItemModel)
            .where(KnowledgeItemModel.item_id == DOC)
            .values(owner_id=None, updated_at=KnowledgeItemModel.updated_at)
        )
        await session.commit()

    assert await _ingest(world, personal("vA", REAL_VECTORS[1])) == "ingested"
    assert (await _row(world)).owner_id == OWNER


@pytest.mark.parametrize("cause", ["unpublished", "still_as_read"])
async def test_a_missed_replacement_logs_at_its_level(
    factory, tmp_path, caplog, cause  # noqa: F811
):
    """Delta item 3: a miss the row's state explains (an unpublish) is INFO; a
    miss on a row still as read repeats every run and is a WARNING. Mutation:
    log every miss at one level."""
    world = _world(factory, tmp_path)
    caplog.set_level(logging.INFO, logger="faultmaven.bootstrap.kb_init")
    if cause == "unpublished":
        await _an_unpublish_after_the_read(world)
        level = logging.INFO
    else:
        await _ingest(world, _pack("v0"))
        with patch.object(
            DatabaseKnowledgeItemRepository, "delete", return_value=False
        ):
            assert await _ingest(world, _pack("vA", REAL_VECTORS[1])) == "skipped"
        level = logging.WARNING
    (miss,) = [r for r in caplog.records if "matched no row" in r.getMessage()]
    assert miss.levelno == level
