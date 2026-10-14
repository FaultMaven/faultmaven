"""A removed knowledge document leaves no retrievable chunk (#1966).

Invariant: a document ``delete_document`` reports removed (deleted or
unpublished) has no retrievable chunk — now, and after any later restart,
reconcile, repair or kb_seed run. When it cannot guarantee that it reports
failure and leaves the document as it was, or in the one recoverable split
state (row present, vectors missing), which reconcile repairs only for a
published row. It never leaves a chunk whose row is gone.

Retrieval never checks that a chunk's row exists, and the boot reconcile only
deletes built-in orphans, so a chunk outliving its row stays searchable for
good. Before this change the row was committed first and the vector delete ran
last with its failure swallowed; and the reconcile/repair pass re-embedded an
unpublished built-in at the next boot, undoing the unpublish.

Everything relational here is real (FK-on SQLite, the real repository and the
real ``KnowledgeService``); the vector store is an in-memory double that keeps
chunk counts by parent id, so "retrievable" is observable as "the store holds
the parent".

T9 (a verify-race withdrawal whose delete fails answers a 409 that says the
copy could not be withdrawn) is pinned by
``test_drafts_live_in_the_database_1950.py::TestAWithdrawalThatFailsIsReportedTruthfully``,
parametrized over a raising and a not-successful ``delete_document``; it is not
duplicated here.
"""

from __future__ import annotations

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
from tests.unit.bootstrap.test_kb_init import _write_pack

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
]


class _KbStore:
    """The KB vector store as these paths use it: chunk counts by parent id.

    The delete signature mirrors ``KnowledgeVectorStore``'s, so a call the real
    store would reject is rejected here too, and ``retries`` is recorded.
    """

    def __init__(self) -> None:
        self.parents: dict[str, int] = {}
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

    async def list_parent_document_ids(self) -> set:
        return set(self.parents)

    async def delete_documents_by_parents(self, parent_ids, collection_name=None):
        return sum(self.parents.pop(p, 0) for p in list(parent_ids))


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

    async def _index(document, prechunked=None, enterprise_id=None):
        # The embedder is the only stubbed step: whatever is indexed lands in
        # the store, so a re-index is observable as the parent coming back.
        indexed.append(document.document_id)
        store.parents[document.document_id] = 3
        return 3

    svc = KnowledgeService.__new__(KnowledgeService)
    svc._db_session_factory = factory
    svc._vector_store = store
    svc._tracer = MagicMock()
    svc._share_repo = _Shares(factory)
    svc._index_document_in_vector_store = _index
    svc._delete_knowledge_item_row = AsyncMock()

    async def seed(item, *, chunks=3):
        async with factory() as session:
            await DatabaseKnowledgeItemRepository(session).create(item)
        if chunks:
            store.parents[item.item_id] = chunks

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

    async def boot_repair():
        """The reconcile + repair pair exactly as ``bootstrap_kb`` runs it."""
        _cleaned, orphaned = await kb_init._reconcile_vectors(svc, factory)
        repaired, _still = await kb_init._repair_orphaned_rows(orphaned, svc)
        return orphaned, repaired

    yield SimpleNamespace(
        svc=svc,
        factory=factory,
        store=store,
        indexed=indexed,
        seed=seed,
        row=row,
        share_to_team=share_to_team,
        shares=shares,
        boot_repair=boot_repair,
    )
    await engine.dispose()


# ---------------------------------------------------------------------------
# T3 / success arms: removed means no chunk, and one vector attempt
# ---------------------------------------------------------------------------


def test_the_ids_take_the_arms_they_are_named_for():
    assert is_builtin_item_id(BUILTIN_ID)
    assert not is_builtin_item_id(AUTHORED_ID)
    assert not is_builtin_item_id(OTHER_AUTHORED_ID)


async def test_t3_an_authored_delete_removes_row_vectors_and_shares(kb):
    await kb.seed(_item(AUTHORED_ID))
    await kb.share_to_team(AUTHORED_ID)

    result = await kb.svc.delete_document(AUTHORED_ID)

    assert result == {"success": True, "document_id": AUTHORED_ID, "action": "deleted"}
    assert await kb.row(AUTHORED_ID) is None
    assert AUTHORED_ID not in kb.store.parents
    assert await kb.shares(AUTHORED_ID) == []
    # ONE attempt: the row change is held uncommitted across this call, and on
    # SQLite that hold is the single writer lock — retries would extend it.
    assert kb.store.delete_calls == [(AUTHORED_ID, 0)]


async def test_a_builtin_unpublish_keeps_the_row_and_drops_the_vectors(kb):
    await kb.seed(_builtin())

    result = await kb.svc.delete_document(BUILTIN_ID)

    assert result["success"] is True and result["action"] == "unpublished"
    assert (await kb.row(BUILTIN_ID)).is_published is False
    assert BUILTIN_ID not in kb.store.parents
    assert kb.store.delete_calls == [(BUILTIN_ID, 0)]


# ---------------------------------------------------------------------------
# T1 / T4: the vector store fails → nothing changes, and the caller is told
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", VECTOR_FAILURES)
async def test_t1_an_authored_delete_whose_vectors_fail_changes_nothing(kb, failure):
    await kb.seed(_item(AUTHORED_ID))
    await kb.share_to_team(AUTHORED_ID)
    kb.store.fail_for[AUTHORED_ID] = failure

    with pytest.raises(KnowledgeBaseError) as raised:
        await kb.svc.delete_document(AUTHORED_ID)

    assert raised.value.error_code == "KNOWLEDGE_VECTOR_DELETE_FAILED"
    # The row change was rolled back, not committed: the document is intact.
    assert await kb.row(AUTHORED_ID) is not None
    assert kb.store.parents[AUTHORED_ID] == 3
    assert len(await kb.shares(AUTHORED_ID)) == 1
    assert kb.store.delete_calls == [(AUTHORED_ID, 0)]


@pytest.mark.parametrize("failure", VECTOR_FAILURES)
async def test_t4_a_builtin_unpublish_whose_vectors_fail_stays_published(kb, failure):
    await kb.seed(_builtin())
    kb.store.fail_for[BUILTIN_ID] = failure

    with pytest.raises(KnowledgeBaseError):
        await kb.svc.delete_document(BUILTIN_ID)

    assert (await kb.row(BUILTIN_ID)).is_published is True
    assert kb.store.parents[BUILTIN_ID] == 3


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
    shipped_id = kb_init._item_id_from_runbook_id("example-runbook")

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
    assert shipped_id not in kb.store.parents
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
    assert shipped_id not in second.orphaned_rows + second.repaired_rows
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
# T6: the commit fails after the vectors are gone → the recoverable split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item_id", "make", "action"),
    [
        pytest.param(AUTHORED_ID, lambda: _item(AUTHORED_ID), "deleted", id="authored"),
        pytest.param(BUILTIN_ID, _builtin, "unpublished", id="builtin"),
    ],
)
async def test_t6_a_failed_commit_leaves_a_live_row_that_reconcile_repairs(
    kb, caplog, item_id, make, action
):
    await kb.seed(make())

    commit_failure = OperationalError("COMMIT", {}, Exception("connection reset"))
    with (
        patch.object(AsyncSession, "commit", AsyncMock(side_effect=commit_failure)),
        caplog.at_level(logging.ERROR),
        pytest.raises(Exception) as raised,
    ):
        await kb.svc.delete_document(item_id)

    # Surfaced as a failure — and NOT as the "nothing changed" domain error,
    # because the vectors did change.
    assert not isinstance(raised.value, KnowledgeBaseError)
    split = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "SPLIT KNOWLEDGE DOCUMENT" in r.getMessage()
    ]
    assert len(split) == 1 and item_id in split[0].getMessage()
    assert "Retry the delete" in split[0].getMessage()

    # The split state: row present and still published, vectors gone.
    row = await kb.row(item_id)
    assert row is not None and row.is_published is True
    assert item_id not in kb.store.parents

    # ...which the reconcile/repair pass restores, because the row is live.
    orphaned, repaired = await kb.boot_repair()
    assert orphaned == [item_id] and repaired == [item_id]
    assert kb.store.parents[item_id] == 3


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
# T8: bulk delete reports the vector failure per id, in a constant sentence
# ---------------------------------------------------------------------------


async def test_t8_bulk_delete_reports_a_vector_failure_without_driver_text(kb):
    await kb.seed(_item(AUTHORED_ID))
    await kb.seed(_item(OTHER_AUTHORED_ID))
    kb.store.fail_for[AUTHORED_ID] = RuntimeError(DRIVER_TEXT)

    result = await kb.svc.bulk_delete_documents([AUTHORED_ID, OTHER_AUTHORED_ID])

    assert result["deleted_count"] == 1
    assert result["errors"] == [
        f"Document {AUTHORED_ID}: not deleted, because removing it from search "
        "failed; nothing was changed, so the delete can be retried"
    ]
    assert not any(
        fragment in error
        for error in result["errors"]
        for fragment in ("chroma", "sqlite", "/var/lib", "KNOWLEDGE_", "[")
    )
    assert await kb.row(AUTHORED_ID) is not None
    assert kb.store.parents[AUTHORED_ID] == 3
    assert await kb.row(OTHER_AUTHORED_ID) is None
    assert OTHER_AUTHORED_ID not in kb.store.parents


# ---------------------------------------------------------------------------
# T1 at the route: DELETE /knowledge/documents/{id} answers 503
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
async def test_the_delete_route_answers_503_and_says_nothing_was_deleted(
    kb, monkeypatch, item_id, make, roles
):
    monkeypatch.setattr(
        global_authoring, "requested_tenant_provider", lambda: BUILTIN_SINGLE
    )
    await kb.seed(make())
    kb.store.fail_for[item_id] = RuntimeError(DRIVER_TEXT)
    transport = httpx.ASGITransport(app=_app(kb.svc, _user(roles=roles)))

    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        failed = await client.delete(f"/knowledge/documents/{item_id}")
        detail = failed.json()["detail"]

        assert failed.status_code == 503
        assert "not deleted" in detail and "nothing was changed" in detail
        assert "Retry" in detail
        assert "chroma" not in detail.lower()
        row = await kb.row(item_id)
        assert row is not None and row.is_published is True
        assert kb.store.parents[item_id] == 3

        # The promise is what makes the retry safe: it completes the delete.
        del kb.store.fail_for[item_id]
        retried = await client.delete(f"/knowledge/documents/{item_id}")

    assert retried.status_code == 204
    assert item_id not in kb.store.parents
