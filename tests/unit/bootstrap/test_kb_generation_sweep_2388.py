"""The generation sweep, the age floor and the chunk-id rules (#2388).

A chunk is superseded when its parent has a complete row whose generation its
id does not name; the sweep deletes it once older than the floor. It spares the
served generation, a parent with no row (the reconcile's or the authored-orphan
sweep's), an incomplete row's parent (the incomplete-ingest sweep's) and any
chunk younger than the floor, and it has no document cap. Real ChromaDB and
SQLite throughout.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.bootstrap import kb_init
from faultmaven.bootstrap.kb_generation_sweep import (
    GenerationSweepResult,
    sweep_superseded_generations,
    web_startup_generation_mode,
)
from faultmaven.bootstrap.kb_orphan_sweep import (
    SWEEP_ARMED,
    SWEEP_OFF,
    SWEEP_REPORT_ONLY,
    SweepCounts,
    _delete,
)
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.infrastructure.knowledge.admission import (
    chunk_generation_from_id,
    chunk_parent_id,
)
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
    _group_chunk_ids_by_parent,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope
from faultmaven.utils.runbook_id import is_builtin_item_id
from tests.utils import seed_enterprises

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
OLD = int((NOW - timedelta(hours=48)).timestamp())
YOUNG = int((NOW - timedelta(hours=1)).timestamp())
FLOOR = timedelta(hours=24)

SERVED = "kb_aaaaaaaaaaaa"
ROWLESS = "kb_bbbbbbbbbbbb"
INCOMPLETE = "kb_" + "c" * 16


def _gen(n: int) -> str:
    return f"{n:02d}" * 16


@pytest.fixture
async def world(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'kb.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await seed_enterprises(session, [STANDALONE_ENTERPRISE_ID])
    chroma = chromadb.PersistentClient(
        path=str(tmp_path / "chroma"),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
    )
    store = KnowledgeVectorStore(chroma)

    async def row(item_id: str, generation: str, *, complete: bool = True) -> None:
        async with factory() as session:
            await DatabaseKnowledgeItemRepository(session).create(
                KnowledgeItem(
                    item_id=item_id,
                    enterprise_id=STANDALONE_ENTERPRISE_ID,
                    title="Runbook",
                    content="body",
                    item_type=KnowledgeItemType.RUNBOOK,
                    scope=KnowledgeScope.GLOBAL,
                    ingest_complete=complete,
                    vector_generation=generation,
                )
            )

    async def chunk(chunk_id: str, indexed_at=None) -> None:
        metadata = {"scope": "global", "enterprise_id": STANDALONE_ENTERPRISE_ID}
        if indexed_at is not None:
            metadata["indexed_at"] = indexed_at
        await store.add_documents(
            [{"id": chunk_id, "content": chunk_id, "metadata": metadata}],
            embeddings=[[0.1, 0.2]],
        )

    def present() -> set[str]:
        return set(chroma.get_collection(KB_COLLECTION).get(include=[])["ids"])

    async def sweep(mode=SWEEP_ARMED) -> GenerationSweepResult:
        return await sweep_superseded_generations(
            SimpleNamespace(_vector_store=store),
            factory,
            mode=mode,
            max_age=FLOOR,
            now=NOW,
        )

    yield SimpleNamespace(
        row=row, chunk=chunk, present=present, sweep=sweep, store=store, factory=factory
    )
    await engine.dispose()


@pytest.fixture
async def corpus(world):
    """A served row with its own chunk, two superseded ones (another
    generation's, and one written before #2388) and a young superseded one; a
    row-less parent; an incomplete row whose chunk names another generation."""
    await world.row(SERVED, _gen(1))
    await world.chunk(f"{SERVED}_chunk_0_{_gen(1)}", OLD)
    await world.chunk(f"{SERVED}_chunk_0_{_gen(0)}", OLD)
    await world.chunk(f"{SERVED}_chunk_0")
    await world.chunk(f"{SERVED}_chunk_0_{_gen(2)}", YOUNG)
    await world.chunk(f"{ROWLESS}_chunk_0_{_gen(3)}", OLD)
    await world.row(INCOMPLETE, _gen(4), complete=False)
    await world.chunk(f"{INCOMPLETE}_chunk_0_{_gen(5)}", OLD)
    return world


SUPERSEDED = {f"{SERVED}_chunk_0_{_gen(0)}", f"{SERVED}_chunk_0"}


async def test_the_sweep_deletes_the_superseded_and_spares_everything_else(corpus):
    before = corpus.present()

    result = await corpus.sweep()

    assert (result.mode, result.would_delete, result.deleted) == (SWEEP_ARMED, 2, 2)
    assert result.documents == [SERVED]
    assert corpus.present() == before - SUPERSEDED
    # A second pass finds nothing: what is left is served, young, row-less or
    # an incomplete ingest's.
    assert (await corpus.sweep()).would_delete == 0


async def test_a_report_only_sweep_deletes_nothing(corpus):
    before = corpus.present()

    result = await corpus.sweep(SWEEP_REPORT_ONLY)

    assert (result.would_delete, result.deleted, result.skipped) == (2, 0, None)
    assert corpus.present() == before


async def test_an_off_sweep_reads_nothing(corpus):
    result = await corpus.sweep(SWEEP_OFF)
    assert (result.skipped, result.would_delete) == ("disabled", 0)


async def test_more_parents_than_the_repair_cap_are_still_swept(world):
    """No document cap: superseded chunks are the normal product of a pack
    update, of far more than ``KB_REPAIR_MAX_ROWS`` runbooks."""
    parents = [f"kb_{n:012x}" for n in range(kb_init.KB_REPAIR_MAX_ROWS + 1)]
    for parent in parents:
        await world.row(parent, _gen(1))
        await world.chunk(f"{parent}_chunk_0_{_gen(1)}", OLD)
        await world.chunk(f"{parent}_chunk_0_{_gen(0)}", OLD)

    result = await world.sweep()

    assert result.deleted == len(parents)
    assert world.present() == {f"{p}_chunk_0_{_gen(1)}" for p in parents}


async def test_a_failed_delete_is_counted_unremoved(corpus):
    with patch.object(
        corpus.store, "delete_observed_chunk_ids", side_effect=OSError("down")
    ):
        result = await corpus.sweep()
    assert (result.would_delete, result.deleted, result.unremoved) == (2, 0, 2)


@pytest.mark.parametrize(
    ("external", "mode"), [(False, SWEEP_ARMED), (True, SWEEP_REPORT_ONLY)]
)
def test_the_web_startup_deletes_only_on_a_local_store(external, mode):
    with (
        patch("faultmaven.config.settings.get_settings", return_value=MagicMock()),
        patch(
            "faultmaven.config.deployment_coherence.is_external_chroma_configured",
            return_value=external,
        ),
    ):
        assert web_startup_generation_mode() == mode


async def test_the_bootstrap_sweeps_report_only_under_an_external_chromadb(
    corpus, tmp_path
):
    before = corpus.present()
    service = MagicMock()
    service._vector_store = corpus.store
    with patch(
        "faultmaven.config.deployment_coherence.is_external_chroma_configured",
        return_value=True,
    ):
        result = await kb_init.bootstrap_kb(
            knowledge_service=service,
            db_session_factory=corpus.factory,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            project_root=tmp_path,
            pack_dir=tmp_path / "no-pack",
            incomplete_sweep_mode=SWEEP_OFF,
        )

    assert result.generation_sweep.mode == SWEEP_REPORT_ONLY
    assert result.generation_sweep.would_delete == 2
    assert corpus.present() == before


def test_the_counts_ride_the_result_line_and_keep_the_cd_rule():
    """The CD gate reads the line's LAST ``failed=``: no key after the first
    may end in ``failed``, and no value may hold a ``)``."""
    result = kb_init.BootstrapResult()
    result.generation_sweep = GenerationSweepResult(
        mode=SWEEP_ARMED, would_delete=4, deleted=3, unremoved=1, skipped="error"
    )
    result.unserved_rows = ["kb_aaaaaaaaaaaa"]
    line = repr(result)

    for key, value in {
        "generation_sweep_mode": "armed",
        "generation_sweep_would_delete": "4",
        "generation_sweep_deleted": "3",
        "generation_sweep_unremoved": "1",
        "generation_sweep_skipped": "error",
        "unserved_rows": "1",
    }.items():
        assert f"{key}={value}" in line
    keys = re.findall(r"(\w+)=", line)
    assert [k for k in keys if k.endswith("failed")] == ["failed"] == keys[2:3]
    assert re.fullmatch(r"BootstrapResult\([^)]*\)", line)


# --- the chunk-id rules at every site ----------------------------------------

NEW = f"{SERVED}_chunk_3_{_gen(7)}"


def test_every_parent_rule_names_the_parent_of_a_generation_id():
    assert chunk_parent_id(NEW, None) == SERVED
    assert chunk_parent_id(NEW, {}) == SERVED
    assert _group_chunk_ids_by_parent([NEW, f"{SERVED}_chunk_0"]) == {
        SERVED: [NEW, f"{SERVED}_chunk_0"]
    }
    assert kb_init._BUILTIN_ITEM_ID_RE.match(NEW.rsplit("_chunk_", 1)[0])
    assert is_builtin_item_id(next(iter(_group_chunk_ids_by_parent([NEW]))))


def test_the_generation_is_read_from_the_id():
    assert chunk_generation_from_id(NEW) == _gen(7)
    assert chunk_generation_from_id(f"{SERVED}_chunk_3") is None
    assert chunk_generation_from_id("no-separator") is None
    assert chunk_generation_from_id(None) is None


async def test_the_authored_sweep_reports_the_parent_of_a_generation_id():
    store = MagicMock()

    async def delete_chunk_ids(chunk_ids, where):
        return list(chunk_ids)

    store.delete_chunk_ids = delete_chunk_ids
    counts = SweepCounts()
    authored = "kb_" + "d" * 16
    await _delete(
        store,
        [f"{authored}_chunk_0_{_gen(1)}", f"{authored}_chunk_1_{_gen(1)}"],
        {"enterprise_id": STANDALONE_ENTERPRISE_ID},
        counts,
        1,
        label="standalone",
    )
    assert counts.deleted == [authored]


async def test_the_filtered_listing_groups_generation_ids_by_parent(world):
    """The enterprise backfill's and the authored sweep's listing."""
    await world.chunk(NEW, OLD)
    listed = await world.store.list_chunk_ids_by_parent(
        {"enterprise_id": STANDALONE_ENTERPRISE_ID}
    )
    assert listed == {SERVED: [NEW]}
