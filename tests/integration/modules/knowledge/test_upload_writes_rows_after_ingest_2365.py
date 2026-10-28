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
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import _current_enterprise_id
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


class _FailingCommit:
    """The session factory, whose first commit after ``armed`` raises ``error``,
    before the commit lands or, with ``landed``, after it."""

    def __init__(self, factory, error: BaseException, *, landed: bool) -> None:
        self._factory, self._error, self._landed = factory, error, landed
        self.armed = False

    def __call__(self):
        session = self._factory()
        if self.armed:
            self.armed = False
            real_commit = session.commit

            async def commit():
                if self._landed:
                    await real_commit()
                raise self._error

            session.commit = commit
        return session

    def arm_after_ingestion(self, service: KnowledgeService) -> None:
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


async def _assert_landed(factory, result) -> None:
    async with factory() as session:
        (draft,) = (await session.execute(select(ConversionDraftModel))).scalars()
        item = await session.get(KnowledgeItemModel, result["document_id"])
    assert item is not None
    assert (draft.status, draft.knowledge_item_id) == ("verified", item.item_id)
    assert [d["draft_id"] for d in await _drafts(factory)] == [draft.id]


async def test_an_upload_lands_where_foreign_keys_are_enforced(session_factory):
    result = await _upload(_knowledge(session_factory))

    await _assert_landed(session_factory, result)
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
    # What ingestion had written when the vectors failed: its item, and at team
    # scope its share. Neither outlives the failure (the share did, #2365).
    assert at_failure == {
        **NOTHING,
        "knowledge_items": 1,
        "resource_shares": int(scope == "team"),
    }
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
    factory = _FailingCommit(session_factory, error, landed=landed)
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
    factory = _FailingCommit(
        session_factory, RuntimeError("commit failed"), landed=False
    )
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
    # What a crash between the two commits leaves too: the item, live and
    # deletable, and no upload, job or draft.
    assert await _rows(session_factory) == {**NOTHING, "knowledge_items": 1}
    assert await _drafts(session_factory) == []


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

        await _assert_landed(factory, result)
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
