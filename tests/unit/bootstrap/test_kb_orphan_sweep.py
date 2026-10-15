"""The authored-orphan sweep on SQLite, over a real ChromaDB (#1982).

SQLite has no row-level security, so its row read is always complete: these
tests pin what the sweep decides once the proof holds — per-enterprise scoping,
the listing-before-read order, the reference-set check acknowledged one bucket
at a time, the cap, mis-stamped chunks of live documents, the unattributed
bucket, and the report-only default (a dry run's would-delete set is exactly
what the armed run deletes). What a PostgreSQL role's posture does to the proof is
``tests/integration/modules/knowledge/test_kb_orphan_sweep_postgres_1982.py``.

The corpus is ``tests/kb_orphan_sweep_scenario.py``: three enterprises, each
with live authored documents and one authored orphan, plus unstamped legacy
chunks, a live built-in and a built-in orphan.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultmaven.bootstrap import kb_init, kb_orphan_sweep
from faultmaven.bootstrap.kb_orphan_sweep import (
    SWEEP_ARMED,
    SWEEP_OFF,
    SWEEP_REPORT_ONLY,
    UNATTRIBUTED,
    sweep_authored_orphans,
)
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.persistence.models import Base
from tests import kb_orphan_sweep_scenario as sc

pytestmark = [pytest.mark.unit]


@dataclass
class World:
    engine: AsyncEngine
    factory: Any
    client: Any
    store: KnowledgeVectorStore
    svc: Any

    async def sweep(self, **kwargs) -> kb_orphan_sweep.AuthoredSweepResult:
        """An ARMED sweep unless the test says otherwise: most tests here pin
        what deletion does; the report-only default has tests of its own."""
        kwargs.setdefault("max_parents", 25)
        kwargs.setdefault("mode", SWEEP_ARMED)
        return await sweep_authored_orphans(self.svc, self.factory, **kwargs)

    def present(self) -> set[str]:
        return sc.chunk_ids_present(self.client)


def chroma_client(path: Path) -> Any:
    """A ChromaDB of this test's own (a path no other test uses)."""
    return chromadb.PersistentClient(
        path=str(path),
        settings=ChromaSettings(
            anonymized_telemetry=False,
            allow_reset=False,
            environment="",
            is_persistent=True,
        ),
    )


async def make_world(tmp_path: Path, docs=sc.DOCS) -> World:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb.sqlite'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await sc.seed_database(conn, docs)
    client = chroma_client(tmp_path / "chroma")
    sc.add_chunks(client, docs)
    store = KnowledgeVectorStore(client)
    return World(
        engine=engine,
        factory=async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
        client=client,
        store=store,
        svc=SimpleNamespace(_vector_store=store),
    )


@pytest.fixture
async def world(tmp_path):
    w = await make_world(tmp_path)
    yield w
    await w.engine.dispose()


def _orphan_chunks(*docs: sc.Doc) -> set[str]:
    return {cid for doc in docs for cid in doc.chunk_ids()}


ALL_AUTHORED_ORPHANS = tuple(sc.AUTHORED_ORPHANS.values()) + (sc.UNSTAMPED_ORPHAN,)


# ---------------------------------------------------------------------------
# What a proven sweep deletes
# ---------------------------------------------------------------------------


async def test_each_enterprises_orphans_go_and_every_live_chunk_stays(world):
    result = await world.sweep()

    assert result.proven is True
    assert result.skipped is None
    for enterprise_id, orphan in sc.AUTHORED_ORPHANS.items():
        assert result.enterprises[enterprise_id].deleted == [orphan.item_id]
    assert result.unattributed.deleted == [sc.UNSTAMPED_ORPHAN.item_id]
    present = world.present()
    assert sc.live_chunk_ids() <= present, sc.live_chunk_ids() - present
    assert not _orphan_chunks(*ALL_AUTHORED_ORPHANS) & present
    # Built-ins are the built-in reconcile's: the sweep never deletes one,
    # orphaned or not.
    assert set(sc.BUILTIN_ORPHAN.chunk_ids()) <= present
    # Every chunk no listed stamp attributes is counted, built-ins included:
    # two unstamped live authored, one unstamped orphan, one unstamped built-in.
    assert result.unattributed_chunks == 8
    assert result.summary()["deleted"] == 4


async def test_a_second_pass_finds_nothing(world):
    await world.sweep()
    again = await world.sweep()

    assert again.deleted == []
    assert sc.live_chunk_ids() <= world.present()


async def test_a_chunk_written_between_the_listing_and_the_row_read_survives(
    world, monkeypatch
):
    """Every create path commits the row before it writes vectors, so a chunk
    the listing saw has a row the later read sees. The document created here
    lands between the two steps — whichever runs first — and must survive.
    Reading the rows first would see the chunk and miss the row."""
    new = sc.Doc("1c000000-0000-4000-8000-000000000001", sc.ST, sc.ST)
    created: list[bool] = []

    async def create_document():
        if created:
            return
        created.append(True)
        async with world.engine.begin() as conn:
            await sc.insert_rows(conn, [new])  # the row commits first
        sc.add_chunks(world.client, [new])

    real_list = world.store.list_chunk_ids_by_parent

    async def list_then_create(where, *args, **kwargs):
        found = await real_list(where, *args, **kwargs)
        await create_document()
        return found

    real_read = kb_orphan_sweep._read_rows_if_complete

    async def read_then_create(factory):
        rows = await real_read(factory)
        await create_document()
        return rows

    monkeypatch.setattr(world.store, "list_chunk_ids_by_parent", list_then_create)
    monkeypatch.setattr(kb_orphan_sweep, "_read_rows_if_complete", read_then_create)

    result = await world.sweep()

    assert created == [True]
    assert new.item_id not in result.deleted
    assert set(new.chunk_ids()) <= world.present()
    assert result.enterprises[sc.ST].deleted == [sc.ST_ORPHAN.item_id]


async def test_a_live_documents_misstamped_chunk_is_reported_and_kept(tmp_path):
    """A live document of E2 with one stray chunk stamped E1. Its parent is
    absent from E1's rows but HAS a row (in E2), so it is no orphan: the chunk
    is counted as mis-stamped and kept. Deleting it would leave a live
    document short a chunk repair never restores (repair re-embeds only a
    document with NO vectors). A delete by parent would also take E2's live
    chunk."""
    mixed = sc.Doc("3c000000-0000-4000-8000-0000000000e2", sc.E2, sc.E2)
    world = await make_world(tmp_path, sc.DOCS + (mixed,))
    try:
        collection = world.client.get_or_create_collection(sc.COLLECTION)
        collection.update(
            ids=[f"{mixed.item_id}_chunk_0"],
            metadatas=[
                {
                    "parent_document_id": mixed.item_id,
                    "scope": "personal",
                    "enterprise_id": sc.E1,
                }
            ],
        )

        result = await world.sweep()

        present = world.present()
        assert set(mixed.chunk_ids()) <= present
        assert result.enterprises[sc.E1].misstamped == 1
        assert result.enterprises[sc.E1].deleted == [sc.E1_ORPHAN.item_id]
        assert mixed.item_id not in result.would_delete
        assert result.enterprises[sc.E2].deleted == [sc.E2_ORPHAN.item_id]
        assert result.totals()["misstamped"] == 1
    finally:
        await world.engine.dispose()


async def test_a_chunk_restamped_after_the_listing_is_not_deleted(world, monkeypatch):
    """The delete is the observed ids AND the stamp they were observed under.
    A chunk rewritten under another enterprise's stamp after the listing (its
    document re-indexed by its owner) no longer matches, and stays."""
    restamped = f"{sc.E1_ORPHAN.item_id}_chunk_0"
    real_read = kb_orphan_sweep._read_rows_if_complete

    async def read_then_restamp(factory):
        rows = await real_read(factory)
        world.client.get_or_create_collection(sc.COLLECTION).update(
            ids=[restamped],
            metadatas=[
                {
                    "parent_document_id": sc.E1_ORPHAN.item_id,
                    "scope": "personal",
                    "enterprise_id": sc.E2,
                }
            ],
        )
        return rows

    monkeypatch.setattr(kb_orphan_sweep, "_read_rows_if_complete", read_then_restamp)

    result = await world.sweep()

    present = world.present()
    assert restamped in present
    assert f"{sc.E1_ORPHAN.item_id}_chunk_1" not in present
    assert result.enterprises[sc.E1].deleted == [sc.E1_ORPHAN.item_id]


# ---------------------------------------------------------------------------
# The guards: reference set, cap, empty table
# ---------------------------------------------------------------------------


E3 = "e3000000-0000-0000-0000-00000000e003"
E3_ORPHANS = (
    sc.Doc("0f000000-0000-4000-8000-0000000000f1", None, E3),
    sc.Doc("0f000000-0000-4000-8000-0000000000f2", None, E3),
)


async def _with_disjoint_enterprise(tmp_path) -> World:
    world = await make_world(tmp_path, sc.DOCS + E3_ORPHANS)
    async with world.engine.begin() as conn:
        await sc.insert_enterprises(conn, [E3])
    return world


async def test_an_enterprise_none_of_whose_vectors_has_a_row_is_skipped(
    tmp_path, caplog
):
    """Every authored chunk stamped E3 is an orphan by the diff — the shape a
    partial row read, a keyspace change and a genuinely emptied enterprise all
    take. Not deleted without the operator's acknowledgment."""
    world = await _with_disjoint_enterprise(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger=kb_orphan_sweep.__name__):
            result = await world.sweep()

        assert result.enterprises[E3].disjoint == 2
        assert result.enterprises[E3].deleted == []
        assert _orphan_chunks(*E3_ORPHANS) <= world.present()
        # The other enterprises are swept as usual.
        assert result.enterprises[sc.E1].deleted == [sc.E1_ORPHAN.item_id]
        assert any("NONE is referenced" in r.getMessage() for r in caplog.records)
    finally:
        await world.engine.dispose()


async def test_the_acknowledgment_lets_a_disjoint_enterprise_be_swept(tmp_path):
    world = await _with_disjoint_enterprise(tmp_path)
    try:
        result = await world.sweep(allow_disjoint={E3})

        assert sorted(result.enterprises[E3].deleted) == sorted(
            d.item_id for d in E3_ORPHANS
        )
        assert not _orphan_chunks(*E3_ORPHANS) & world.present()
        assert sc.live_chunk_ids() <= world.present()
    finally:
        await world.engine.dispose()


async def test_acknowledging_one_enterprise_acknowledges_no_other_bucket(tmp_path):
    """#2018 review R5: one blanket acknowledgment unguarded every bucket, so
    acknowledging an emptied enterprise also let the unattributed bucket —
    another database's chunks, in the shared-Chroma probe — be deleted. Here
    E3 and the unattributed bucket are both disjoint (no live unstamped
    document); naming E3 sweeps E3 and leaves the unattributed bucket."""
    docs = (
        tuple(
            d for d in sc.DOCS if d not in (sc.UNSTAMPED_LIVE_ST, sc.UNSTAMPED_LIVE_E1)
        )
        + E3_ORPHANS
    )
    world = await make_world(tmp_path, docs)
    try:
        async with world.engine.begin() as conn:
            await sc.insert_enterprises(conn, [E3])

        result = await world.sweep(allow_disjoint={E3})

        assert sorted(result.enterprises[E3].deleted) == sorted(
            d.item_id for d in E3_ORPHANS
        )
        assert result.unattributed.disjoint == 1
        assert result.unattributed.deleted == []
        assert set(sc.UNSTAMPED_ORPHAN.chunk_ids()) <= world.present()

        named = await world.sweep(allow_disjoint={UNATTRIBUTED})
        assert named.unattributed.deleted == [sc.UNSTAMPED_ORPHAN.item_id]
    finally:
        await world.engine.dispose()


async def test_more_orphans_than_the_cap_deletes_none(world, caplog):
    """Four orphan documents, a cap of three: all or nothing, so nothing."""
    with caplog.at_level(logging.ERROR, logger=kb_orphan_sweep.__name__):
        result = await world.sweep(max_parents=3)

    assert result.deleted == []
    for enterprise_id in sc.AUTHORED_ORPHANS:
        assert result.enterprises[enterprise_id].capped == 1
    assert result.unattributed.capped == 1
    assert result.summary()["capped"] == 4
    assert _orphan_chunks(*ALL_AUTHORED_ORPHANS) <= world.present()
    assert any("deleting NONE" in r.getMessage() for r in caplog.records)


async def test_orphans_up_to_the_cap_are_deleted(world):
    result = await world.sweep(max_parents=4)

    assert len(result.deleted) == 4
    assert result.summary()["capped"] == 0


async def test_an_empty_table_deletes_nothing(tmp_path):
    """Chunks with no rows at all is an anomaly (an emptied or not-yet-seeded
    database beside an older index), never a signal to delete them all."""
    world = await make_world(tmp_path, docs=())
    try:
        sc.add_chunks(world.client, sc.DOCS)
        result = await world.sweep(allow_disjoint={*sc.ENTERPRISES, UNATTRIBUTED})

        assert result.skipped == "no_rows"
        assert result.deleted == []
        assert _orphan_chunks(*sc.DOCS) <= world.present()
    finally:
        await world.engine.dispose()


# ---------------------------------------------------------------------------
# Unproven, degraded and failed passes
# ---------------------------------------------------------------------------


async def test_an_unproven_read_sweeps_nothing_and_says_what_it_left(
    world, monkeypatch
):
    """The branch the RLS-scoped app role takes on PostgreSQL (proved there by
    the PostgreSQL module): every enterprise reported unproven, the
    unattributed bucket too, and nothing deleted."""

    async def unproven(_factory):
        return False, {}, kb_orphan_sweep.UNPROVEN_FILTERED

    monkeypatch.setattr(kb_orphan_sweep, "_read_rows_if_complete", unproven)

    result = await world.sweep()

    assert result.proven is False
    assert result.deleted == []
    assert {e: c.unproven for e, c in result.enterprises.items()} == {
        sc.ST: 3,
        sc.E1: 3,
        sc.E2: 2,
    }
    assert result.unattributed.unproven == 3
    assert _orphan_chunks(*ALL_AUTHORED_ORPHANS) <= world.present()


async def test_a_failed_delete_is_counted_and_the_rest_proceed(world, monkeypatch):
    real_delete = world.store.delete_chunk_ids

    async def delete(chunk_ids, where, *args, **kwargs):
        if where == {"enterprise_id": sc.E1}:
            raise RuntimeError("chroma unavailable")
        return await real_delete(chunk_ids, where, *args, **kwargs)

    monkeypatch.setattr(world.store, "delete_chunk_ids", delete)

    result = await world.sweep()

    assert result.enterprises[sc.E1].unremoved == 1
    assert result.enterprises[sc.E1].deleted == []
    assert set(sc.E1_ORPHAN.chunk_ids()) <= world.present()
    assert result.enterprises[sc.E2].deleted == [sc.E2_ORPHAN.item_id]


async def test_a_store_that_cannot_list_by_stamp_is_skipped(world):
    result = await sweep_authored_orphans(
        SimpleNamespace(_vector_store=object()), world.factory, max_parents=25
    )
    assert result.skipped == "store_cannot_enumerate"


async def test_a_listing_that_raises_leaves_everything(world, monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("chroma unavailable")

    monkeypatch.setattr(world.store, "list_chunk_ids_by_parent", boom)

    result = await world.sweep()

    assert result.skipped == "error"
    assert result.deleted == []
    assert _orphan_chunks(*sc.DOCS) <= world.present()


# ---------------------------------------------------------------------------
# The store's two new calls
# ---------------------------------------------------------------------------


async def test_the_store_lists_and_deletes_only_with_a_where_clause(world):
    with pytest.raises(ValueError, match="requires a where clause"):
        await world.store.list_chunk_ids_by_parent({})
    with pytest.raises(ValueError, match="requires a where clause"):
        await world.store.delete_chunk_ids(["x_chunk_0"], {})


async def test_the_store_deletes_the_intersection_of_ids_and_stamp(world):
    """``E1_LIVE_1``'s ids under E2's stamp match nothing: nothing goes."""
    deleted = await world.store.delete_chunk_ids(
        sc.E1_LIVE_1.chunk_ids(), {"enterprise_id": sc.E2}
    )
    assert deleted == []
    assert set(sc.E1_LIVE_1.chunk_ids()) <= world.present()

    listed = await world.store.list_chunk_ids_by_parent({"enterprise_id": sc.E2})
    assert listed == {
        sc.E2_LIVE.item_id: sc.E2_LIVE.chunk_ids(),
        sc.E2_ORPHAN.item_id: sc.E2_ORPHAN.chunk_ids(),
    }


# ---------------------------------------------------------------------------
# Report-only by default; an operator arms it (the storage_cleanup gate)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "enabled, configured_dry_run, override, expected",
    [
        (False, True, None, SWEEP_REPORT_ONLY),  # the shipped defaults
        (True, True, None, SWEEP_REPORT_ONLY),
        (True, False, None, SWEEP_ARMED),
        (False, False, None, SWEEP_OFF),
        (True, False, True, SWEEP_REPORT_ONLY),  # kb_seed --dry-run
        (True, True, False, SWEEP_ARMED),  # kb_seed --no-dry-run, enabled
        (False, True, False, SWEEP_OFF),  # --no-dry-run cannot grant deletion
    ],
)
def test_the_sweep_mode_is_the_storage_cleanup_gate(
    monkeypatch, enabled, configured_dry_run, override, expected
):
    database = SimpleNamespace(
        kb_orphan_sweep_enabled=enabled, kb_orphan_sweep_dry_run=configured_dry_run
    )
    monkeypatch.setattr(
        "faultmaven.config.settings.get_settings",
        lambda: SimpleNamespace(database=database),
    )
    assert kb_orphan_sweep.resolve_sweep_mode(override) == expected


def test_the_shipped_settings_are_report_only():
    from faultmaven.config.settings import DatabaseSettings

    fields = DatabaseSettings.model_fields
    assert fields["kb_orphan_sweep_enabled"].default is False
    assert fields["kb_orphan_sweep_dry_run"].default is True


async def test_a_report_only_sweep_deletes_nothing_and_lists_what_it_would(world):
    result = await world.sweep(mode=SWEEP_REPORT_ONLY)

    assert result.deleted == []
    assert result.would_delete == sorted(d.item_id for d in ALL_AUTHORED_ORPHANS)
    assert result.summary()["would_delete_documents"] == {
        **{e: [d.item_id] for e, d in sc.AUTHORED_ORPHANS.items()},
        UNATTRIBUTED: [sc.UNSTAMPED_ORPHAN.item_id],
    }
    assert _orphan_chunks(*sc.DOCS) <= world.present()


async def test_a_dry_runs_would_delete_set_is_what_the_armed_run_then_deletes(
    tmp_path,
):
    """The canary's promise (#2018 review R4): every decision — the proof,
    the per-bucket acknowledgment, the mis-stamp filter, the cap — is made the
    same way in both modes, so what the dry run lists is what arming deletes."""
    mixed = sc.Doc("3c000000-0000-4000-8000-0000000000e2", sc.E2, sc.E2)
    world = await make_world(tmp_path, sc.DOCS + E3_ORPHANS + (mixed,))
    try:
        async with world.engine.begin() as conn:
            await sc.insert_enterprises(conn, [E3])
        world.client.get_or_create_collection(sc.COLLECTION).update(
            ids=[f"{mixed.item_id}_chunk_0"],
            metadatas=[
                {
                    "parent_document_id": mixed.item_id,
                    "scope": "personal",
                    "enterprise_id": sc.E1,
                }
            ],
        )
        before = world.present()

        dry = await world.sweep(mode=SWEEP_REPORT_ONLY, allow_disjoint={E3})
        assert world.present() == before
        assert dry.deleted == []

        armed = await world.sweep(mode=SWEEP_ARMED, allow_disjoint={E3})

        def by_bucket(result, attribute):
            return {
                b: getattr(c, attribute)
                for b, c in result._buckets().items()
                if getattr(c, attribute)
            }

        assert by_bucket(armed, "deleted") == by_bucket(dry, "would_delete")
        assert len(dry.would_delete) == 6  # 3 stamped + E3's 2 + unattributed
        assert before - world.present() == _orphan_chunks(
            *ALL_AUTHORED_ORPHANS, *E3_ORPHANS
        )
    finally:
        await world.engine.dispose()


def _by_bucket(result, attribute):
    return {
        b: getattr(c, attribute)
        for b, c in result._buckets().items()
        if getattr(c, attribute)
    }


async def test_dry_and_armed_sweeps_agree_on_an_unacknowledged_disjoint_enterprise(
    tmp_path,
):
    """#2022: a dry run makes the disjoint decision an armed run makes. E3's
    chunks have no row anywhere, and nothing acknowledges E3: neither mode may
    list or delete them, and both count the same disjoint chunks."""
    world = await _with_disjoint_enterprise(tmp_path)
    try:
        before = world.present()
        dry = await world.sweep(mode=SWEEP_REPORT_ONLY)
        armed = await world.sweep(mode=SWEEP_ARMED)

        assert dry.enterprises[E3].disjoint == armed.enterprises[E3].disjoint == 2
        assert dry.enterprises[E3].would_delete == []
        assert _by_bucket(armed, "deleted") == _by_bucket(dry, "would_delete")
        assert _orphan_chunks(*E3_ORPHANS) <= world.present()
        assert before - world.present() == _orphan_chunks(
            *ALL_AUTHORED_ORPHANS
        ), "armed deleted something the dry run had not listed, or less"
    finally:
        await world.engine.dispose()


async def test_dry_and_armed_sweeps_agree_at_the_cap(world):
    """#2022: a dry run applies the cap an armed run applies. Four orphan
    documents against a cap of three: both modes cap the same non-zero count
    and neither lists nor deletes anything."""
    dry = await world.sweep(mode=SWEEP_REPORT_ONLY, max_parents=3)
    armed = await world.sweep(mode=SWEEP_ARMED, max_parents=3)

    assert dry.summary()["capped"] == armed.summary()["capped"] == 4
    assert dry.would_delete == [] == armed.deleted
    assert _by_bucket(dry, "would_delete") == _by_bucket(armed, "deleted") == {}
    assert _orphan_chunks(*ALL_AUTHORED_ORPHANS) <= world.present()


async def test_an_off_sweep_does_not_even_list(world, monkeypatch):
    async def boom(*_a, **_k):
        raise AssertionError("an off sweep listed the index")

    monkeypatch.setattr(world.store, "list_chunk_ids_by_parent", boom)

    result = await world.sweep(mode=SWEEP_OFF)

    assert result.skipped == "disabled"
    assert result.mode == SWEEP_OFF


# ---------------------------------------------------------------------------
# The counts ride on the line CD keeps
# ---------------------------------------------------------------------------

#: faultmaven-enterprise-infra .github/workflows/cd-pipeline.yaml, the kb_seed
#: step: ``grep -o "BootstrapResult([^)]*)"`` then
#: ``sed -n 's/.*failed=\([0-9]*\).*/\1/p'`` — the sed's greedy ``.*`` makes
#: it read the LAST ``failed=`` on the line.
_CD_RESULT = re.compile(r"BootstrapResult\([^)]*\)")
_CD_FAILED = re.compile(r".*failed=([0-9]*).*")


def _cd_gate(log: str) -> tuple[list[str], list[str]]:
    results = _CD_RESULT.findall(log)
    failed = [m.group(1) for r in results if (m := _CD_FAILED.match(r))]
    return results, failed


def test_the_cd_gate_reads_the_real_failed_count_beside_every_sweep_count():
    """A capped, disjoint, mis-stamped, report-only run with an unremoved
    delete: every count is on the line, and the gate still reads ``failed=0``
    (the one key that may end in ``failed``)."""
    bootstrap = kb_init.BootstrapResult()
    sweep = bootstrap.authored_orphans
    sweep.mode = SWEEP_REPORT_ONLY
    sweep.enterprises = {
        sc.E1: kb_orphan_sweep.SweepCounts(capped=3, misstamped=1, unremoved=2),
        sc.E2: kb_orphan_sweep.SweepCounts(disjoint=4, would_delete=["x"]),
    }
    line = f"KB seed completed: {bootstrap!r}"

    results, failed = _cd_gate(line)

    assert results == [repr(bootstrap)]
    assert failed == ["0"]
    for key, value in (
        ("authored_orphans_mode", "report_only"),
        ("authored_orphans_would_delete", "1"),
        ("authored_orphans_capped", "3"),
        ("authored_orphans_disjoint", "4"),
        ("authored_orphans_misstamped", "1"),
        ("authored_orphans_unremoved", "2"),
    ):
        assert f"{key}={value}," in line
    keys = re.findall(r"(\w+)=", repr(bootstrap))
    assert [k for k in keys if k.endswith("failed")] == ["failed"]


# ---------------------------------------------------------------------------
# Through the bootstrap: reported, and the built-in path as it was
# ---------------------------------------------------------------------------


@pytest.fixture
def sweep_settings(monkeypatch):
    """Point KB_ORPHAN_SWEEP_* at the values a test names, then restore the
    settings instance (not reset it: a later test must not rebuild settings
    inside its own log window because of this one)."""
    from faultmaven.config import settings as settings_module
    from faultmaven.config.settings import reset_settings

    saved = settings_module._settings_instance

    def apply(**env):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        reset_settings()

    yield apply
    settings_module._settings_instance = saved


async def _bootstrap(tmp_path, caplog):
    from faultmaven.modules.knowledge.domain.services.knowledge_service import (
        KnowledgeService,
    )
    from tests.unit.bootstrap.test_kb_init import _write_pack

    docs = tuple(
        d for d in sc.DOCS if d not in (sc.BUILTIN_LIVE, sc.BUILTIN_UNSTAMPED_LIVE)
    )
    world = await make_world(tmp_path, docs)
    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = world.factory
    svc._vector_store = world.store
    svc._share_repo = None
    svc._tracer = MagicMock()
    with caplog.at_level(logging.INFO, logger=kb_init.__name__):
        result = await kb_init.bootstrap_kb(
            knowledge_service=svc,
            db_session_factory=world.factory,
            enterprise_id=sc.ST,
            project_root=tmp_path,
            pack_dir=_write_pack(tmp_path),
        )
    return world, docs, result


async def test_the_bootstrap_reports_authored_orphans_and_deletes_none_by_default(
    tmp_path, caplog, sweep_settings
):
    """The shipped settings over SQLite and a real ChromaDB: the pack runbook
    is ingested, the built-in orphan is the reconcile's
    (``orphaned_vectors_cleaned``, unchanged), and the authored orphans are
    REPORTED — on the result, the repr CD keeps, and the completion line —
    but not deleted."""
    sweep_settings()
    world, docs, result = await _bootstrap(tmp_path, caplog)
    try:
        assert result.failed == []
        assert result.ingested == ["global/example.md"]
        assert result.orphaned_vectors_cleaned == [sc.BUILTIN_ORPHAN.item_id]
        assert result.authored_orphans.mode == SWEEP_REPORT_ONLY
        assert result.authored_orphans.deleted == []
        assert result.authored_orphans.would_delete == sorted(
            d.item_id for d in ALL_AUTHORED_ORPHANS
        )
        present = world.present()
        assert _orphan_chunks(*ALL_AUTHORED_ORPHANS) <= present
        assert sc.BUILTIN_ORPHAN.chunk_ids()[0] not in present
        assert (
            "authored_orphans_mode=report_only, authored_orphans_would_delete=4, "
            "authored_orphans_deleted=0," in repr(result)
        )
        assert any(
            "0 authored orphans swept (report_only: 4 would be)" in r.getMessage()
            for r in caplog.records
        )
    finally:
        await world.engine.dispose()


async def test_the_bootstrap_deletes_authored_orphans_once_armed(
    tmp_path, caplog, sweep_settings
):
    sweep_settings(KB_ORPHAN_SWEEP_ENABLED="true", KB_ORPHAN_SWEEP_DRY_RUN="false")
    world, docs, result = await _bootstrap(tmp_path, caplog)
    try:
        assert result.authored_orphans.mode == SWEEP_ARMED
        assert result.authored_orphans.deleted == sorted(
            d.item_id for d in ALL_AUTHORED_ORPHANS
        )
        present = world.present()
        assert sc.live_chunk_ids(d for d in sc.LIVE if d in docs) <= present
        assert not _orphan_chunks(*ALL_AUTHORED_ORPHANS, sc.BUILTIN_ORPHAN) & present
        assert "authored_orphans_deleted=4," in repr(result)
    finally:
        await world.engine.dispose()
