"""A removed knowledge document leaves no retrievable chunk (#1966, #2397).

Invariant: a document ``delete_document`` reports removed (deleted or
unpublished) has no chunk retrievable through a KB door — now, and after any
later restart, reconcile, repair or kb_seed run. Every door admits a chunk only
when its parent's row is complete and published at query time
(``KnowledgeService.admitted_generations``), so the delete commits the row
change first and is done: an unpublish is a SQL-only flip, and an authored
delete removes its vectors afterwards, best effort. A vector delete that fails
or times out is not a failed delete (#2063). What it leaves is never served:
an unpublished built-in's vectors are removed by the next pack-loading
bootstrap's reconcile, an authored document's only by the authored-orphan
sweep once an operator arms it.

Before #1966 the row was committed first and the vector delete's failure was
swallowed, with retrieval reading the vector store alone, so a chunk outliving
its row stayed searchable for good; #1966 then held the row change across the
vector delete and rolled it back on a failure, which treated a timed-out delete
as one that never happened (#2063). The reconcile/repair pass also re-embedded
an unpublished built-in at the next boot, undoing the unpublish.

Everything relational here is real (FK-on SQLite, the real repository and the
real ``KnowledgeService``, so the admission check runs its real query); the
vector store is an in-memory double that keeps chunk counts by parent id, so
"the store still holds it" and "a door would serve it" are separately
observable. That every door applies the check is pinned over a real ChromaDB in
``tests/integration/modules/knowledge/test_row_checked_retrieval_2397.py``.

T9 (a verify-race withdrawal whose delete fails answers a 409 that says the
copy could not be withdrawn) is pinned by
``test_drafts_live_in_the_database_1950.py::TestAWithdrawalThatFailsIsReportedTruthfully``,
parametrized over a raising and a not-successful ``delete_document``; it is not
duplicated here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.bootstrap import kb_init
from faultmaven.infrastructure.base_client import CircuitBreakerError
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.persistence.share_repository import (
    PostgreSQLShareRepository,
)
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.domain import global_authoring
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
from faultmaven.providers.tenancy.factory import BUILTIN_SINGLE
from faultmaven.utils.runbook_id import is_builtin_item_id, item_id_from_runbook_id
from tests.unit.bootstrap.test_kb_init import RUNBOOK_MD, _write_pack

pytestmark = [pytest.mark.unit]

BUILTIN_ID = item_id_from_runbook_id("openssh-auth-failures")  # kb_<12 hex>
AUTHORED_ID = "11111111-2222-3333-4444-555555555555"
OTHER_AUTHORED_ID = "66666666-7777-8888-9999-000000000000"

# Text a vector-store failure carries that must never reach a response body.
DRIVER_TEXT = "chromadb sqlite3.OperationalError at /var/lib/chroma/chroma.sqlite3"

VECTOR_FAILURES = [
    pytest.param(RuntimeError(DRIVER_TEXT), id="store-raises"),
    pytest.param(
        CircuitBreakerError("Circuit breaker is open for chromadb", service="kb"),
        id="breaker-open",
    ),
    # #2063: the store may have applied a delete whose call timed out.
    pytest.param(TimeoutError("vector delete timed out"), id="timeout"),
]


class _KbStore:
    """The KB vector store as these paths use it: chunk counts by parent id.

    The delete signature mirrors ``KnowledgeVectorStore``'s, so a call the real
    store would reject is rejected here too, and ``retries`` is recorded.
    """

    def __init__(self) -> None:
        self.parents: dict[str, int] = {}
        #: The generation each parent's chunks carry (#2388).
        self.generations: dict[str, str] = {}
        self.fail_for: dict[str, Exception] = {}
        self.delete_calls: list[tuple[str, int]] = []

    async def delete_documents_by_parent_id(
        self,
        parent_document_id: str,
        collection_name: Optional[str] = None,
        *,
        retries: int = 2,
    ) -> int:
        self.delete_calls.append((parent_document_id, retries))
        error = self.fail_for.get(parent_document_id)
        if error is not None:
            raise error  # before anything is removed
        return self.parents.pop(parent_document_id, 0)

    async def list_all_chunk_ids_by_parent(self) -> dict:
        return {
            p: [f"{p}_chunk_{n}_{self.generations[p]}" for n in range(c)]
            for p, c in self.parents.items()
        }

    async def list_chunk_ids_by_parent(self, where) -> dict:
        return {}  # the age floor's read: every chunk here is old

    async def delete_observed_chunk_ids(self, chunk_ids, collection_name=None):
        ids = list(chunk_ids)
        for parent in {c.rsplit("_chunk_", 1)[0] for c in ids}:
            self.parents.pop(parent, None)
        return ids


class _Shares:
    """The real share repository over the test database, a session per call
    (production wraps the same repository the same way)."""

    def __init__(self, factory) -> None:
        self._factory = factory

    async def share(self, **kwargs):
        async with self._factory() as session:
            return await PostgreSQLShareRepository(session).share(**kwargs)

    async def list_scopes_for_resource(self, resource_type, resource_id):
        async with self._factory() as session:
            return await PostgreSQLShareRepository(session).list_scopes_for_resource(
                resource_type, resource_id
            )

    async def delete_for_resource(self, resource_type, resource_id):
        async with self._factory() as session:
            return await PostgreSQLShareRepository(session).delete_for_resource(
                resource_type, resource_id
            )


def _item(item_id, *, scope=KnowledgeScope.PERSONAL, owner_id="user-1", published=True):
    return KnowledgeItem(
        item_id=item_id,
        enterprise_id="ent-1",
        organization_id=None,
        title="Runbook",
        content="# body\nsteps",
        item_type=KnowledgeItemType.RUNBOOK,
        scope=scope,
        owner_id=None if scope == KnowledgeScope.GLOBAL else owner_id,
        is_published=published,
    )


def _builtin(**kwargs):
    return _item(BUILTIN_ID, scope=KnowledgeScope.GLOBAL, **kwargs)


@pytest.fixture
async def kb():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # the #378 connect listener
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "INSERT INTO enterprises (enterprise_id, name, slug) "
                "VALUES ('ent-1', 'Default', 'default')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO users (user_id, enterprise_id, username, email, "
                "display_name) VALUES ('user-1', 'ent-1', 'user-1', 'u1@e', 'u1')"
            )
        )

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    store = _KbStore()
    indexed: list[str] = []

    async def _index(document, prechunked=None, enterprise_id=None, generation=None):
        # The embedder is the only stubbed step: whatever is indexed lands in
        # the store, so a re-index is observable as the parent coming back.
        indexed.append(document.document_id)
        store.parents[document.document_id] = 3
        store.generations[document.document_id] = generation
        return 3

    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = factory
    svc._vector_store = store
    svc._tracer = MagicMock()
    svc._share_repo = _Shares(factory)
    svc._index_document_in_vector_store = _index
    svc._delete_knowledge_item_row = AsyncMock()
    svc._sanitizer = MagicMock()

    async def _same(text):
        return text

    svc._sanitizer.asanitize = _same

    async def seed(item, *, chunks=3):
        async with factory() as session:
            await DatabaseKnowledgeItemRepository(session).create(item)
        if chunks:
            store.parents[item.item_id] = chunks
            store.generations[item.item_id] = item.vector_generation

    async def row(item_id):
        async with factory() as session:
            return await DatabaseKnowledgeItemRepository(session).get_by_id(item_id)

    async def share_to_team(item_id):
        await svc._share_repo.share(
            resource_type="knowledge_item",
            resource_id=item_id,
            scope_type="team",
            scope_id="team-1",
            enterprise_id="ent-1",
        )

    async def shares(item_id):
        return await svc._share_repo.list_scopes_for_resource("knowledge_item", item_id)

    async def admitted(item_id):
        """Whether a KB door would serve this document's chunks: the real
        admission check, over the real rows, against the generation its
        chunks carry."""
        admitted = await svc.admitted_generations([item_id])
        return item_id in store.parents and admitted.get(
            item_id
        ) == store.generations.get(item_id)

    async def listed(item_id):
        async with factory() as session:
            items = await DatabaseKnowledgeItemRepository(session).list_for_inventory(
                "ent-1", user_id="user-1"
            )
        return item_id in {i.item_id for i in items}

    async def boot_repair():
        """The reconcile + repair pair exactly as ``bootstrap_kb`` runs it."""
        _cleaned, _unpublished, orphaned = await kb_init._reconcile_vectors(
            svc, factory
        )
        repaired, _still = await kb_init._repair_unserved_rows(orphaned, svc)
        return orphaned, repaired

    async def boot(tmp_path, pack_dir):
        """One startup bootstrap (or kb_seed run) over the shipped pack."""
        return await kb_init.bootstrap_kb(
            knowledge_service=svc,
            db_session_factory=factory,
            enterprise_id="ent-1",
            project_root=tmp_path,
            pack_dir=pack_dir,
        )

    yield SimpleNamespace(
        svc=svc,
        factory=factory,
        store=store,
        indexed=indexed,
        seed=seed,
        row=row,
        share_to_team=share_to_team,
        shares=shares,
        admitted=admitted,
        listed=listed,
        boot_repair=boot_repair,
        boot=boot,
    )
    await engine.dispose()


# ---------------------------------------------------------------------------
# T3 / success arms: removed means no admitted chunk
# ---------------------------------------------------------------------------


def test_the_ids_take_the_arms_they_are_named_for():
    assert is_builtin_item_id(BUILTIN_ID)
    assert not is_builtin_item_id(AUTHORED_ID)
    assert not is_builtin_item_id(OTHER_AUTHORED_ID)


async def test_t3_an_authored_delete_removes_row_vectors_and_shares(kb):
    await kb.seed(_item(AUTHORED_ID))
    await kb.share_to_team(AUTHORED_ID)
    assert await kb.admitted(AUTHORED_ID)  # control: a live document is served

    result = await kb.svc.delete_document(AUTHORED_ID)

    assert result == {"success": True, "document_id": AUTHORED_ID, "action": "deleted"}
    assert await kb.row(AUTHORED_ID) is None
    assert not await kb.admitted(AUTHORED_ID)
    assert AUTHORED_ID not in kb.store.parents
    assert await kb.shares(AUTHORED_ID) == []
    # The vector delete runs after the commit, so no transaction (on SQLite
    # the writer lock) is held across it.
    assert [parent for parent, _ in kb.store.delete_calls] == [AUTHORED_ID]


async def test_a_builtin_unpublish_is_a_sql_only_flip(kb):
    """#2397: an unpublish makes no vector call. Its chunks stay in the store,
    unadmitted, until the next boot's reconcile collects them (T5)."""
    await kb.seed(_builtin())

    result = await kb.svc.delete_document(BUILTIN_ID)

    assert result["success"] is True and result["action"] == "unpublished"
    assert (await kb.row(BUILTIN_ID)).is_published is False
    assert kb.store.delete_calls == []
    assert kb.store.parents[BUILTIN_ID] == 3
    assert not await kb.admitted(BUILTIN_ID)


# ---------------------------------------------------------------------------
# T1 / T4: the vector store fails → the delete still completes (#2063)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", VECTOR_FAILURES)
async def test_t1_an_authored_delete_whose_vectors_fail_still_completes(
    kb, caplog, failure
):
    """The row change committed before the vector delete, so its failure — a
    timeout included, which may have applied — is logged and not raised: no
    door admits the chunks it leaves, and nothing rolls the delete back."""
    await kb.seed(_item(AUTHORED_ID))
    await kb.share_to_team(AUTHORED_ID)
    kb.store.fail_for[AUTHORED_ID] = failure

    with caplog.at_level(logging.WARNING):
        result = await kb.svc.delete_document(AUTHORED_ID)

    assert result == {"success": True, "document_id": AUTHORED_ID, "action": "deleted"}
    assert await kb.row(AUTHORED_ID) is None
    assert not await kb.admitted(AUTHORED_ID)
    assert not await kb.listed(AUTHORED_ID)
    assert await kb.shares(AUTHORED_ID) == []
    assert kb.store.parents[AUTHORED_ID] == 3  # left for the orphan sweep
    assert [parent for parent, _ in kb.store.delete_calls] == [AUTHORED_ID]
    logged = [r.getMessage() for r in caplog.records if AUTHORED_ID in r.getMessage()]
    assert any("removing its vectors failed" in m for m in logged), logged
    assert not any(DRIVER_TEXT in m for m in logged)


@pytest.mark.parametrize("failure", VECTOR_FAILURES)
async def test_t4_a_builtin_unpublish_never_reaches_a_failing_store(kb, failure):
    await kb.seed(_builtin())
    kb.store.fail_for[BUILTIN_ID] = failure

    result = await kb.svc.delete_document(BUILTIN_ID)

    assert result["success"] is True and result["action"] == "unpublished"
    assert (await kb.row(BUILTIN_ID)).is_published is False
    assert not await kb.admitted(BUILTIN_ID)
    assert kb.store.delete_calls == []


# ---------------------------------------------------------------------------
# T2 / T2b: no row changed → the shared vector store is never called
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item_id", "make"),
    [
        pytest.param(AUTHORED_ID, lambda: _item(AUTHORED_ID), id="t2-authored-delete"),
        pytest.param(BUILTIN_ID, _builtin, id="t2b-builtin-unpublish"),
    ],
)
async def test_t2_a_write_that_matches_no_row_never_touches_the_vectors(
    kb, item_id, make
):
    """The row vanished between the read and the write (a concurrent delete
    won; under multi an RLS write policy refusing the session reports the same
    zero rows). The real repository runs the real statement against a database
    without the row, so its rowcount is genuinely 0.

    ChromaDB is shared across tenants, so a vector delete issued for a row this
    session did not remove would let a tenant destroy platform content (#770).
    The built-in arm used to raise a 500 here instead of reporting it (A2).
    """
    kb.store.parents[item_id] = 3  # someone's chunks, which must survive
    with patch.object(
        DatabaseKnowledgeItemRepository, "get_by_id", AsyncMock(return_value=make())
    ):
        result = await kb.svc.delete_document(item_id)

    assert result["success"] is False
    assert kb.store.delete_calls == []
    assert kb.store.parents[item_id] == 3


# ---------------------------------------------------------------------------
# T5: an unpublish survives the next boot (FAILED on main 069b9906e)
# ---------------------------------------------------------------------------


async def test_t5_an_unpublished_builtin_is_not_re_embedded_by_the_next_boot(
    kb, tmp_path
):
    """Boot, unpublish, boot again: the second boot must not bring it back.

    On main the reconcile pass listed every row without vectors as orphaned,
    published or not, and repair re-embedded it: the runbook the user removed
    was retrievable again after the next restart or kb_seed run.

    Positive control in the same boot: a PUBLISHED row without vectors (the
    split state) IS repaired, so this passes only because the unpublished row
    is excluded, not because repair never ran.
    """
    pack_dir = _write_pack(tmp_path)
    shipped_id = item_id_from_runbook_id("example-runbook")

    first = await kb_init.bootstrap_kb(
        knowledge_service=kb.svc,
        db_session_factory=kb.factory,
        enterprise_id="ent-1",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )
    assert first.ingested == ["global/example.md"], first
    assert shipped_id in kb.store.parents

    assert (await kb.svc.delete_document(shipped_id))["action"] == "unpublished"
    # A SQL-only flip: its chunks stay, unadmitted, for the reconcile below.
    assert shipped_id in kb.store.parents
    assert not await kb.admitted(shipped_id)
    await kb.seed(_item(OTHER_AUTHORED_ID), chunks=0)  # published, vectors lost
    kb.indexed.clear()

    second = await kb_init.bootstrap_kb(
        knowledge_service=kb.svc,
        db_session_factory=kb.factory,
        enterprise_id="ent-1",
        project_root=tmp_path,
        pack_dir=pack_dir,
    )

    assert second.skipped_unchanged == ["global/example.md"], second
    assert shipped_id not in second.unserved_rows + second.repaired_rows
    assert second.unpublished_vectors_cleaned == [shipped_id]
    assert shipped_id not in kb.store.parents, "the unpublish was undone"
    assert kb.indexed == [OTHER_AUTHORED_ID]
    assert second.repaired_rows == [OTHER_AUTHORED_ID]
    assert (await kb.row(shipped_id)).is_published is False


async def test_t10_reindex_missing_vectors_refuses_an_unpublished_row(kb, caplog):
    """Defence in depth behind the reconcile filter: a caller that hands repair
    an unpublished row anyway gets nothing written."""
    await kb.seed(_builtin(published=False), chunks=0)

    with caplog.at_level(logging.WARNING):
        assert await kb.svc.reindex_missing_vectors(BUILTIN_ID) == 0

    assert kb.indexed == []
    assert BUILTIN_ID not in kb.store.parents
    assert any(
        BUILTIN_ID in r.getMessage() and "unpublished" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# T6: the commit fails → nothing changed, and no vector was touched
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item_id", "make"),
    [
        pytest.param(AUTHORED_ID, lambda: _item(AUTHORED_ID), id="authored"),
        pytest.param(BUILTIN_ID, _builtin, id="builtin"),
    ],
)
async def test_t6_a_failed_commit_changes_nothing_and_touches_no_vector(
    kb, item_id, make
):
    """The vector delete comes after the commit, so a commit that fails leaves
    the document exactly as it was — no split state for a repair to restore."""
    await kb.seed(make())

    commit_failure = OperationalError("COMMIT", {}, Exception("connection reset"))
    with (
        patch.object(AsyncSession, "commit", AsyncMock(side_effect=commit_failure)),
        pytest.raises(RuntimeError, match="Document removal failed"),
    ):
        await kb.svc.delete_document(item_id)

    row = await kb.row(item_id)
    assert row is not None and row.is_published is True
    assert await kb.admitted(item_id)
    assert kb.store.parents[item_id] == 3
    assert kb.store.delete_calls == []


# ---------------------------------------------------------------------------
# T7: the share cleanup fails after the commit → still a successful delete
# ---------------------------------------------------------------------------


async def test_t7_a_share_cleanup_failure_after_the_commit_is_logged_not_raised(
    kb, caplog
):
    await kb.seed(_item(AUTHORED_ID))
    kb.svc._share_repo = MagicMock()
    kb.svc._share_repo.delete_for_resource = AsyncMock(
        side_effect=RuntimeError("share table locked")
    )

    with caplog.at_level(logging.ERROR):
        result = await kb.svc.delete_document(AUTHORED_ID)

    assert result["success"] is True and result["action"] == "deleted"
    assert await kb.row(AUTHORED_ID) is None
    assert AUTHORED_ID not in kb.store.parents
    kb.svc._share_repo.delete_for_resource.assert_awaited_once_with(
        "knowledge_item", AUTHORED_ID
    )
    assert any(
        r.levelname == "ERROR" and AUTHORED_ID in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# T8: bulk delete counts a delete whose vectors fail as a delete
# ---------------------------------------------------------------------------


async def test_t8_bulk_delete_counts_a_delete_whose_vectors_fail_as_deleted(kb):
    """A vector failure after the commit is no failed delete (#2063); what
    still fails is reported in a constant sentence, with no driver text."""
    await kb.seed(_item(AUTHORED_ID))
    await kb.seed(_item(OTHER_AUTHORED_ID))
    kb.store.fail_for[AUTHORED_ID] = RuntimeError(DRIVER_TEXT)

    result = await kb.svc.bulk_delete_documents(
        [AUTHORED_ID, OTHER_AUTHORED_ID, "absent-id"]
    )

    assert result["deleted_count"] == 2
    assert result["errors"] == ["Document absent-id: delete failed"]
    for item_id in (AUTHORED_ID, OTHER_AUTHORED_ID):
        assert await kb.row(item_id) is None
        assert not await kb.admitted(item_id)
    assert kb.store.parents[AUTHORED_ID] == 3  # left for the orphan sweep
    assert OTHER_AUTHORED_ID not in kb.store.parents


# ---------------------------------------------------------------------------
# Item 6: a cancellation is logged, and leaves no admitted chunk without a row
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item_id", "make"),
    [
        pytest.param(AUTHORED_ID, lambda: _item(AUTHORED_ID), id="authored"),
        pytest.param(BUILTIN_ID, _builtin, id="builtin"),
    ],
)
async def test_a_cancellation_at_the_commit_leaves_the_document_whole(
    kb, item_id, make
):
    """``CancelledError`` is not an ``Exception``. Cancelled at its commit, the
    delete has touched no vector, so the document is as it was."""
    await kb.seed(make())
    with (
        patch.object(
            AsyncSession, "commit", AsyncMock(side_effect=asyncio.CancelledError())
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await kb.svc.delete_document(item_id)

    row = await kb.row(item_id)
    assert row is not None and row.is_published is True
    assert await kb.admitted(item_id)
    assert kb.store.parents[item_id] == 3
    assert kb.store.delete_calls == []


async def test_a_cancellation_during_the_vector_delete_is_logged_after_the_commit(
    kb, caplog
):
    await kb.seed(_item(AUTHORED_ID))
    kb.store.fail_for[AUTHORED_ID] = asyncio.CancelledError()

    with caplog.at_level(logging.WARNING), pytest.raises(asyncio.CancelledError):
        await kb.svc.delete_document(AUTHORED_ID)

    assert any(
        "cancelled" in r.getMessage() and AUTHORED_ID in r.getMessage()
        for r in caplog.records
    )
    # The delete had committed: the document is gone from every door.
    assert await kb.row(AUTHORED_ID) is None
    assert not await kb.admitted(AUTHORED_ID)


# ---------------------------------------------------------------------------
# Item 2: a reboot compares the pack with what the row was ingested from
# ---------------------------------------------------------------------------

PACK_V1_HASH = hashlib.sha256(RUNBOOK_MD.encode("utf-8")).hexdigest()
RUNBOOK_V2 = RUNBOOK_MD.replace("# Body content for tests.", "# Body, version two.")


async def test_the_pack_bootstrap_stamps_the_pack_hash_on_the_row(kb, tmp_path):
    shipped = item_id_from_runbook_id("example-runbook")
    await kb.boot(tmp_path, _write_pack(tmp_path))
    assert (await kb.row(shipped)).metadata == {PACK_CONTENT_HASH_KEY: PACK_V1_HASH}


async def test_an_edited_then_unpublished_builtin_stays_unpublished_after_a_reboot(
    kb, tmp_path
):
    """The pack is unchanged; the ROW was edited (PUT) and then unpublished. The
    skip used to hash the row's current content, so the edit looked like a new
    pack version: delete + re-ingest republished the runbook with vectors."""
    pack_dir = _write_pack(tmp_path)
    shipped = item_id_from_runbook_id("example-runbook")
    await kb.boot(tmp_path, pack_dir)
    assert await kb.svc.update_document_metadata(shipped, content="# Edited\nfix")
    assert (await kb.svc.delete_document(shipped))["action"] == "unpublished"

    second = await kb.boot(tmp_path, pack_dir)

    assert second.skipped_unchanged == ["global/example.md"], second
    row = await kb.row(shipped)
    assert row.is_published is False, "the reboot republished it"
    assert row.content == "# Edited\nfix", "the reboot replaced the operator's edit"
    assert shipped not in kb.store.parents


async def test_a_new_pack_version_republishes_an_unpublished_builtin(kb, tmp_path):
    """The documented rule still holds: a shipped update is a new document."""
    shipped = item_id_from_runbook_id("example-runbook")
    await kb.boot(tmp_path, _write_pack(tmp_path))
    await kb.svc.delete_document(shipped)

    second = await kb.boot(tmp_path, _write_pack(tmp_path, content=RUNBOOK_V2))

    assert second.ingested == ["global/example.md"], second
    row = await kb.row(shipped)
    assert row.is_published is True and row.content == RUNBOOK_V2
    assert row.metadata[PACK_CONTENT_HASH_KEY] == (
        hashlib.sha256(RUNBOOK_V2.encode("utf-8")).hexdigest()
    )
    assert shipped in kb.store.parents


async def test_a_row_from_before_the_stamp_is_compared_by_content_once(kb, tmp_path):
    """Every row predating the stamp: compared by content, then stamped —
    without touching ``updated_at`` (bookkeeping, not an edit) — so a later
    edit and unpublish survive the boot after that."""
    pack_dir = _write_pack(tmp_path)
    shipped = item_id_from_runbook_id("example-runbook")
    await kb.boot(tmp_path, pack_dir)
    async with kb.factory() as session:  # what every row looks like today
        await DatabaseKnowledgeItemRepository(session).update_fields(
            shipped, {"metadata": {}}, touch=False
        )
    before = await kb.row(shipped)

    second = await kb.boot(tmp_path, pack_dir)

    assert second.skipped_unchanged == ["global/example.md"], second
    stamped = await kb.row(shipped)
    assert stamped.metadata == {PACK_CONTENT_HASH_KEY: PACK_V1_HASH}
    assert stamped.updated_at == before.updated_at

    await kb.svc.update_document_metadata(shipped, content="# Edited\nfix")
    await kb.svc.delete_document(shipped)
    await kb.boot(tmp_path, pack_dir)
    assert (await kb.row(shipped)).is_published is False
    assert shipped not in kb.store.parents


async def test_a_pre_stamp_row_edited_and_unpublished_is_not_republished(kb, tmp_path):
    """A built-in edited and retired BEFORE the stamp existed: its content no
    longer matches the pack, which the content comparison reads as a new pack
    version — delete + re-ingest, republished with vectors at the first boot
    after the upgrade. An unpublished row's retirement outranks its content,
    so it is taken as this pack version, stamped and skipped."""
    pack_dir = _write_pack(tmp_path)
    shipped = item_id_from_runbook_id("example-runbook")
    await kb.boot(tmp_path, pack_dir)
    await kb.svc.update_document_metadata(shipped, content="# Edited\nfix")
    await kb.svc.delete_document(shipped)
    async with kb.factory() as session:  # what an older version left behind
        await DatabaseKnowledgeItemRepository(session).update_fields(
            shipped, {"metadata": {}}, touch=False
        )

    second = await kb.boot(tmp_path, pack_dir)

    assert second.skipped_unchanged == ["global/example.md"], second
    row = await kb.row(shipped)
    assert row.is_published is False, "the first boot after the upgrade republished it"
    assert row.content == "# Edited\nfix"
    assert row.metadata == {PACK_CONTENT_HASH_KEY: PACK_V1_HASH}
    assert shipped not in kb.store.parents


# ---------------------------------------------------------------------------
# Item 7: the next boot collects the vectors every unpublish leaves
# ---------------------------------------------------------------------------


async def test_the_next_boot_collects_an_unpublished_builtins_vectors(kb, tmp_path):
    """An unpublish is a SQL-only flip (#2397), so the retired runbook's chunks
    stay in the store, unadmitted, until the next pack-loading boot's reconcile
    deletes them; no later boot brings them back."""
    pack_dir = _write_pack(tmp_path)
    shipped = item_id_from_runbook_id("example-runbook")
    await kb.boot(tmp_path, pack_dir)
    await kb.svc.delete_document(shipped)
    assert shipped in kb.store.parents
    assert not await kb.admitted(shipped)

    second = await kb.boot(tmp_path, pack_dir)

    assert second.unpublished_vectors_cleaned == [shipped]
    assert second.orphaned_vectors_cleaned == []
    assert shipped not in kb.store.parents
    assert (await kb.row(shipped)).is_published is False

    third = await kb.boot(tmp_path, pack_dir)
    assert third.repaired_rows == [] and third.unpublished_vectors_cleaned == []
    assert shipped not in kb.store.parents


# ---------------------------------------------------------------------------
# #2063 at the route: DELETE /knowledge/documents/{id} answers 204
# ---------------------------------------------------------------------------


def _user(*, roles=("user",)) -> DevUser:
    return DevUser(
        user_id="user-1",
        username="user-1",
        email="u1@e",
        display_name="u1",
        created_at=datetime.now(timezone.utc),
        roles=list(roles),
    )


def _app(service, user):
    from fastapi import FastAPI

    from faultmaven.api.exception_handlers import get_exception_handlers
    from faultmaven.api.v1.auth_dependencies import require_authentication
    from faultmaven.modules.knowledge.api.routes import get_knowledge_service, router

    app = FastAPI()
    app.include_router(router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[get_knowledge_service] = lambda: service
    app.dependency_overrides[require_authentication] = lambda: user
    return app


@pytest.mark.parametrize(
    ("item_id", "make", "roles"),
    [
        pytest.param(AUTHORED_ID, lambda: _item(AUTHORED_ID), ("user",), id="owner"),
        pytest.param(BUILTIN_ID, _builtin, ("platform_admin",), id="operator"),
    ],
)
async def test_the_delete_route_answers_204_when_the_vectors_fail(
    kb, monkeypatch, item_id, make, roles
):
    """#2063: the 503 is gone. A 204 means no chunk of the document is
    retrievable through a KB door, whatever happened to its vectors."""
    monkeypatch.setattr(
        global_authoring, "requested_tenant_provider", lambda: BUILTIN_SINGLE
    )
    await kb.seed(make())
    kb.store.fail_for[item_id] = RuntimeError(DRIVER_TEXT)
    transport = httpx.ASGITransport(app=_app(kb.svc, _user(roles=roles)))

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        answered = await client.delete(f"/knowledge/documents/{item_id}")

    assert answered.status_code == 204, answered.text
    assert not await kb.admitted(item_id)
    assert not await kb.listed(item_id)
    row = await kb.row(item_id)
    assert row is None if item_id == AUTHORED_ID else row.is_published is False
