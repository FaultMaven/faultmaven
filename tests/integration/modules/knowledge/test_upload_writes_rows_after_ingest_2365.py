"""A KB upload writes its rows after the item they name, and a failed one leaves none (#2365).

``upload_document`` used to commit an ``uploaded_files`` row, a
``conversion_jobs`` row and a VERIFIED ``conversion_drafts`` row naming the item
before ``ingest_runbook`` wrote that item. The draft's foreign key to
``knowledge_items`` is enforced on every deployment (PostgreSQL's key, SQLite
through the app engine's ``PRAGMA foreign_keys=ON``), so every upload failed at
that first commit; and were it accepted, a failed ingestion stranded all three.

Each property is driven through the real service on a database that enforces
foreign keys, the vectors stubbed: an upload lands and its draft names its item;
and each failure class (ingestion's refusal, a vector failure at personal and
team scope, the row write failing before or after its commit landed, and the
withdrawal failing too) leaves no upload, job, draft or share row behind, and
nothing in ``list_all_drafts``. One case runs the upload on PostgreSQL.
"""

from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import _current_enterprise_id
from faultmaven.exceptions import ConflictError
from faultmaven.infrastructure import embedding_guard
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KnowledgeItemModel,
    ResourceShareModel,
    UploadedFileModel,
    UserModel,
)
from faultmaven.infrastructure.persistence.share_repository import (
    PostgreSQLShareRepository,
)
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.api.routes import get_knowledge_service
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.modules.knowledge.domain.services import knowledge_service
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.domain.services.runbook_validator import (
    RunbookQualityError,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
)
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer, seed_enterprises, seed_users

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

OWNER = "user-2365"
TEAM_ID = "11111111-1111-4111-8111-111111111111"
#: Assembled at runtime: push protection reads literals.
_AWS_KEY = "AK" + "IA" + "Q3EGRPBZ7MXT4LKD"

_TABLES = (
    UploadedFileModel,
    ConversionJobModel,
    ConversionDraftModel,
    KnowledgeItemModel,
    ResourceShareModel,
)
NOTHING = dict.fromkeys((m.__tablename__ for m in _TABLES), 0)


@pytest.fixture
async def session_factory(tmp_path):
    """A SQLite database of the test's own, enforcing foreign keys as the app's
    engine does (``database.get_engine``)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'kb.db'}", poolclass=NullPool
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enforce_foreign_keys(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        await seed_enterprises(session, [STANDALONE_ENTERPRISE_ID])
        await seed_users(session, [OWNER], enterprise_id=STANDALONE_ENTERPRISE_ID)
    yield factory
    await engine.dispose()


class _Shares:
    """The real share repository over the test database, a session per call."""

    def __init__(self, factory) -> None:
        self._factory = factory

    async def share(self, **kwargs):
        async with self._factory() as session:
            return await PostgreSQLShareRepository(session).share(**kwargs)

    async def delete_for_resource(self, resource_type, resource_id):
        async with self._factory() as session:
            return await PostgreSQLShareRepository(session).delete_for_resource(
                resource_type, resource_id
            )


#: A commit that never returns until it is cancelled.
PARK = None


class _FailingCommit:
    """The session factory. Once ``armed``, each next session's commit takes one
    ``(error, landed)``: it raises ``error`` before the commit lands or, with
    ``landed``, after it; or, for ``PARK``, waits to be cancelled."""

    def __init__(self, factory, *failures) -> None:
        self._factory, self._failures = factory, list(failures)
        self.armed, self.parked = False, asyncio.Event()

    def __call__(self):
        session = self._factory()
        if self.armed and self._failures:
            error, landed = self._failures.pop(0)
            real_commit = session.commit

            async def commit():
                if error is PARK:
                    self.parked.set()
                    await asyncio.Event().wait()
                if landed:
                    await real_commit()
                raise error

            session.commit = commit
        return session

    def arm_after_ingestion(self, service: KnowledgeService) -> None:
        """Arm once ``ingest_runbook`` has returned: the next session is the
        upload's own row write."""
        ingest = service.ingest_runbook

        async def ingest_then_arm(**kwargs):
            chunks = await ingest(**kwargs)
            self.armed = True
            return chunks

        service.ingest_runbook = ingest_then_arm

    def arm_at_completion(self, service: KnowledgeService) -> None:
        """Arm once the vectors are written: the next session is the ingest's
        completing transaction, its flip and its share (#2394)."""

        async def index_then_arm(*_args, **_kwargs):
            self.armed = True
            return 3

        service._index_document_in_vector_store = AsyncMock(side_effect=index_then_arm)


def _knowledge(factory) -> KnowledgeService:
    vectors = MagicMock()
    vectors.delete_documents_by_parent_id = AsyncMock(return_value=3)
    service = KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=vectors,
        db_session_factory=factory,
        share_repository=_Shares(factory),
    )
    service._index_document_in_vector_store = AsyncMock(return_value=3)
    return service


async def _upload(service: KnowledgeService, *, scope="personal", content=None):
    return await service.upload_document(
        content=content or valid_runbook("Redis Evictions Under Memory Pressure"),
        title="Redis Evictions",
        document_type="runbook",
        scope=scope,
        owner_id=OWNER,
        team_id=TEAM_ID if scope == "team" else None,
    )


async def _rows(factory) -> dict[str, int]:
    async with factory() as session:
        return {
            m.__tablename__: (
                await session.execute(select(func.count()).select_from(m))
            ).scalar_one()
            for m in _TABLES
        }


async def _drafts(factory) -> list[dict]:
    """What ``GET /knowledge/drafts`` lists to the uploader."""
    return await ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=AsyncMock(),
        settings=MagicMock(),
        db_session_factory=factory,
    ).list_all_drafts(OWNER)


async def _assert_landed(factory, document_id: str) -> None:
    """The upload's own rows, read by its ids: on PostgreSQL the database is
    shared with every other ``-m postgres`` test."""
    async with factory() as session:
        (draft,) = (
            await session.execute(
                select(ConversionDraftModel).where(
                    ConversionDraftModel.runbook_id == document_id
                )
            )
        ).scalars()
        job = await session.get(ConversionJobModel, draft.conversion_id)
        upload = await session.get(UploadedFileModel, job.source_file_id)
        item = await session.get(KnowledgeItemModel, document_id)
    assert None not in (upload, item)
    assert (draft.status, draft.knowledge_item_id) == ("verified", document_id)
    assert draft.id in [d["draft_id"] for d in await _drafts(factory)]


async def test_an_upload_lands_where_foreign_keys_are_enforced(session_factory):
    result = await _upload(_knowledge(session_factory))

    await _assert_landed(session_factory, result["document_id"])
    assert await _rows(session_factory) == {
        **dict.fromkeys(NOTHING, 1),
        "resource_shares": 0,
    }


async def test_a_body_ingestion_refuses_leaves_nothing(session_factory, monkeypatch):
    """Past the upload's own gate, so the refusal is ``ingest_runbook``'s (#2012)."""
    monkeypatch.setattr(knowledge_service, "aenforce_runbook_quality", AsyncMock())
    anchor = 'psql -c "SELECT 1"'
    body = valid_runbook("Postgres Credential Rotation Runbook").replace(
        anchor, f"{anchor}\nexport AWS_ACCESS_KEY_ID={_AWS_KEY}", 1
    )
    service = _knowledge(session_factory)

    with pytest.raises(RunbookQualityError):
        await _upload(service, content=body)

    assert await _rows(session_factory) == NOTHING
    assert await _drafts(session_factory) == []
    service._index_document_in_vector_store.assert_not_awaited()


@pytest.mark.parametrize("scope", ["personal", "team"])
async def test_a_vector_failure_leaves_nothing(session_factory, scope, caplog):
    service = _knowledge(session_factory)
    at_failure: dict[str, int] = {}

    async def fail(*_args, **_kwargs):
        at_failure.update(await _rows(session_factory))
        raise RuntimeError("embedder unavailable")

    service._index_document_in_vector_store = AsyncMock(side_effect=fail)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError, match="embedder unavailable"):
            await _upload(service, scope=scope)

    # Ingestion undid its own writes; the upload withdraws nothing more.
    assert "withdraw" not in caplog.text
    # What ingestion had written when the vectors failed: its incomplete item
    # and, at either scope, no share: an authored item's share is written in
    # the transaction that completes it (#2394). The item does not outlive the
    # failure.
    assert at_failure == {**NOTHING, "knowledge_items": 1}
    assert await _rows(session_factory) == NOTHING
    assert await _drafts(session_factory) == []


@pytest.mark.parametrize("landed", [False, True], ids=["refused", "landed"])
@pytest.mark.parametrize(
    "error",
    [RuntimeError("commit failed"), asyncio.CancelledError()],
    ids=["error", "cancelled"],
)
async def test_a_failed_row_write_withdraws_the_item(session_factory, error, landed):
    """A commit refused, or one that landed and still raised or was cancelled:
    no row remains, so withdrawing the item cannot leave a verified draft with a
    NULL item (the key's ``SET NULL``), and the item's vectors are removed."""
    factory = _FailingCommit(session_factory, (error, landed))
    service = _knowledge(factory)
    factory.arm_after_ingestion(service)

    with pytest.raises(type(error)) as raised:
        await _upload(service, scope="team")

    assert raised.value is error
    document_id = service._index_document_in_vector_store.await_args.args[0].document_id
    service._vector_store.delete_documents_by_parent_id.assert_awaited_once_with(
        document_id, retries=0
    )
    assert await _rows(session_factory) == NOTHING
    assert await _drafts(session_factory) == []


async def test_a_failed_withdrawal_names_the_item(session_factory, caplog):
    factory = _FailingCommit(session_factory, (RuntimeError("commit failed"), False))
    service = _knowledge(factory)
    factory.arm_after_ingestion(service)
    service._vector_store.delete_documents_by_parent_id.side_effect = OSError("down")

    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(RuntimeError, match="commit failed"),
    ):
        await _upload(service)

    document_id = service._index_document_in_vector_store.await_args.args[0].document_id
    assert any(
        r.levelno == logging.ERROR
        and document_id in r.getMessage()
        and "withdraw" in r.getMessage()
        for r in caplog.records
    ), caplog.text
    # The failure's class, never its text: a failed INSERT's carries its
    # parameters, the document among them.
    assert "Failed to upload document: RuntimeError" in caplog.text
    assert "commit failed" not in caplog.text
    # What a crash between the two commits leaves too: the item, live and
    # deletable, and no upload, job or draft.
    assert await _rows(session_factory) == {**NOTHING, "knowledge_items": 1}
    assert await _drafts(session_factory) == []


async def test_rows_that_cannot_be_removed_keep_their_item(session_factory, caplog):
    """The commit landed and raised, and removing the rows failed too. The item
    stays, so the verified draft still names it (its withdrawal would have set
    the draft's item to NULL, the key's ``SET NULL``)."""
    factory = _FailingCommit(
        session_factory,
        (RuntimeError("commit failed"), True),
        (RuntimeError("delete failed"), False),
    )
    service = _knowledge(factory)
    factory.arm_after_ingestion(service)

    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="commit"):
        await _upload(service)

    document_id = service._index_document_in_vector_store.await_args.args[0].document_id
    await _assert_landed(session_factory, document_id)
    service._vector_store.delete_documents_by_parent_id.assert_not_awaited()
    (draft,) = await _drafts(session_factory)
    assert any(
        draft["draft_id"] in r.getMessage() and document_id in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR
    ), caplog.text


async def test_a_failed_share_write_leaves_nothing(session_factory, monkeypatch):
    """The share is written in the ingest's completing transaction (#2394), so
    its failure takes the completion with it, and the cleanup removes the
    row and the vectors."""
    monkeypatch.setattr(
        PostgreSQLShareRepository,
        "share",
        AsyncMock(side_effect=RuntimeError("share failed")),
    )
    service = _knowledge(session_factory)

    with pytest.raises(RuntimeError, match="share failed"):
        await _upload(service, scope="team")

    assert await _rows(session_factory) == NOTHING
    service._vector_store.delete_documents_by_parent_id.assert_awaited_once()


async def test_an_item_row_that_survives_keeps_its_share(
    session_factory, monkeypatch, caplog
):
    """The commit of ``_complete_authored_ingest`` (the flip and the share)
    landed and then raised, and the cleanup's ``repo.delete`` raised, so the
    item keeps the share that makes it a team item: shares go only after the
    item's delete commits. The raising commit is the completion's: the item is
    complete, and ``upload_document`` never reached its own row write, whose
    failure would withdraw the item."""
    monkeypatch.setattr(
        DatabaseKnowledgeItemRepository,
        "delete",
        AsyncMock(side_effect=RuntimeError("row delete failed")),
    )
    factory = _FailingCommit(session_factory, (RuntimeError("commit failed"), True))
    service = _knowledge(factory)
    factory.arm_at_completion(service)

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(RuntimeError, match="commit failed"),
    ):
        await _upload(service, scope="team")

    assert "withdraw" not in caplog.text
    assert "the cleanup's row delete failed too" in caplog.text
    assert await _rows(session_factory) == {
        **NOTHING,
        "knowledge_items": 1,
        "resource_shares": 1,
    }
    async with session_factory() as session:
        assert await session.scalar(select(KnowledgeItemModel.ingest_complete))


class _AddsThenRaises:
    """A vector store that applies ``add_documents`` and then raises, as a
    remote store can inside the retry ladder."""

    def __init__(self) -> None:
        self.chunks: dict[str, str] = {}
        self.added = 0

    async def add_documents(self, documents, embeddings):
        self.chunks.update(
            {d["id"]: d["metadata"]["parent_document_id"] for d in documents}
        )
        self.added += len(documents)
        raise RuntimeError("store failed after the write")

    async def delete_documents_by_parent_id(self, parent_id, retries=2):
        for chunk_id in [k for k, v in self.chunks.items() if v == parent_id]:
            del self.chunks[chunk_id]


async def test_vectors_a_failed_store_wrote_are_removed(session_factory, monkeypatch):
    monkeypatch.setattr(
        embedding_guard,
        "embed_texts_or_raise",
        AsyncMock(side_effect=lambda texts, **_: [[0.0] * 4 for _ in texts]),
    )
    service = _knowledge(session_factory)
    del service._index_document_in_vector_store  # the real indexer
    service._vector_store = vectors = _AddsThenRaises()

    with pytest.raises(KnowledgeBaseError):
        await _upload(service)

    assert vectors.added > 0
    assert vectors.chunks == {}
    assert await _rows(session_factory) == NOTHING


@pytest.mark.parametrize(
    "during,how",
    [
        ("ingestion", "task_cancel"),
        ("ingestion", "anyio_scope"),
        ("row_write", "anyio_scope"),
    ],
)
async def test_a_cancelled_upload_leaves_nothing(session_factory, during, how):
    """Cancelled while it embeds or while its rows commit, by ``task.cancel()``
    (a shutdown) or by an anyio scope, which cancels every await that is not
    shielded: ``BaseHTTPMiddleware`` runs the route in one."""
    factory = _FailingCommit(session_factory, (PARK, False))
    service = _knowledge(factory)
    parked = factory.parked
    if during == "row_write":
        factory.arm_after_ingestion(service)
    else:

        async def park(*_args, **_kwargs):
            parked.set()
            await asyncio.Event().wait()

        service._index_document_in_vector_store = AsyncMock(side_effect=park)

    async def run():
        await _upload(service, scope="team")

    if how == "task_cancel":
        task = asyncio.create_task(run())
        await parked.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with anyio.create_task_group() as group:
            group.start_soon(run)
            await parked.wait()
            group.cancel_scope.cancel()

    service._vector_store.delete_documents_by_parent_id.assert_awaited()
    assert await _rows(session_factory) == NOTHING
    assert await _drafts(session_factory) == []


def test_the_upload_route_logs_the_class_not_the_text(caplog):
    """A failed row write's text carries its INSERT parameters, the document's
    content among them."""
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    app.state.team_service = None
    failing = AsyncMock(side_effect=RuntimeError("INSERT params: BODY-2365"))
    app.dependency_overrides[get_knowledge_service] = lambda: SimpleNamespace(
        upload_document=failing
    )
    app.dependency_overrides[require_authentication] = lambda: SimpleNamespace(
        user_id=OWNER, enterprise_id=STANDALONE_ENTERPRISE_ID, organization_id=None
    )

    with caplog.at_level(logging.ERROR):
        response = TestClient(app, raise_server_exceptions=False).post(
            "/api/v1/knowledge/documents",
            data={"title": "T", "document_type": "runbook", "scope": "personal"},
            files={"file": ("rb.md", b"# A runbook\n", "text/markdown")},
        )

    assert response.status_code == 500, response.text
    failing.assert_awaited_once()
    assert "Document upload failed: RuntimeError" in caplog.text
    assert "BODY-2365" not in caplog.text


async def test_verify_drafts_withdrawal_survives_an_anyio_cancel(session_factory):
    """``verify_draft`` lost its swap (the draft was discarded during ingestion)
    and is withdrawing what it published when the request's anyio scope is
    cancelled: the withdrawal still completes."""
    service = _knowledge(session_factory)
    async with session_factory() as session:
        session.add(
            UploadedFileModel(
                file_id="file_v",
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                filename="f",
                size_bytes=1,
            )
        )
        await session.flush()
        session.add(
            ConversionJobModel(
                id="conv_v",
                user_id=OWNER,
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                scope="personal",
                status="completed",
                source_file_id="file_v",
                source_type="document",
            )
        )
        session.add(
            ConversionDraftModel(
                id="draft_v",
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                conversion_id="conv_v",
                runbook_id="rb-v",
                title="Redis Evictions",
                content=valid_runbook("Redis Evictions Under Memory Pressure"),
                status="draft",
                validation_passed=True,
            )
        )
        await session.commit()
    ingest = service.ingest_runbook

    async def ingest_then_discard(**kwargs):
        chunks = await ingest(**kwargs)
        async with session_factory() as session:
            await session.execute(
                update(ConversionDraftModel).values(status="discarded")
            )
            await session.commit()
        return chunks

    service.ingest_runbook = ingest_then_discard
    entered, release = asyncio.Event(), asyncio.Event()

    async def vector_delete(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return 3

    service._vector_store.delete_documents_by_parent_id.side_effect = vector_delete
    conversion = ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=AsyncMock(),
        settings=MagicMock(),
        db_session_factory=session_factory,
        knowledge_service=service,
    )

    async def run():
        with pytest.raises(ConflictError, match="nothing was published"):
            await conversion.verify_draft("conv_v", "draft_v", OWNER)

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await entered.wait()
        group.cancel_scope.cancel()
        release.set()

    assert (await _rows(session_factory))["knowledge_items"] == 0


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
)
async def test_an_upload_lands_on_postgresql():
    """PostgreSQL's key is non-deferrable: the draft's INSERT is checked at once."""
    enterprise = f"ent_2365_{uuid4().hex[:8]}"
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    token = _current_enterprise_id.set(enterprise)
    try:
        async with factory() as session:
            await seed_enterprises(session, [enterprise])
            await seed_users(session, [OWNER], enterprise_id=enterprise)

        result = await _upload(_knowledge(factory))

        await _assert_landed(factory, result["document_id"])
    finally:
        async with factory() as session:
            for model in (
                ResourceShareModel,
                ConversionDraftModel,
                ConversionJobModel,
                UploadedFileModel,
                KnowledgeItemModel,
                UserModel,
            ):
                await session.execute(
                    delete(model).where(model.enterprise_id == enterprise)
                )
            await session.execute(
                delete(EnterpriseModel).where(
                    EnterpriseModel.enterprise_id == enterprise
                )
            )
            await session.commit()
        _current_enterprise_id.reset(token)
        await engine.dispose()
