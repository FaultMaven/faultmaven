"""Deleting a published runbook discards the draft that published it (#2369).

A published draft keeps its row: ``verified``, naming its item through
``conversion_drafts.knowledge_item_id``, whose key is ``ON DELETE SET NULL``.
``delete_document`` and ``fm-reset-kb`` left that row as it was, so
``list_all_drafts`` went on listing a verified draft that named nothing. Both
now discard it in their own transaction, before their ``DELETE`` runs, with the
item rows locked: the text is kept, the draft leaves the list, and a case job's
live key is released once its last live draft goes. A delete that does not
complete leaves the draft as it was.

The SQLite tests run on the #2365 tests' database, which enforces foreign keys
as the app's engine does, so a discard issued after the ``DELETE`` would match
nothing. Two run on PostgreSQL, one of them racing a link against the delete.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from faultmaven.cli import reset_kb
from faultmaven.config.tenant_context import _current_enterprise_id
from faultmaven.infrastructure.persistence import database
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KnowledgeItemModel,
    ResourceShareModel,
    UploadedFileModel,
    UserModel,
)
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    draft_retirement,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
)
from tests.integration.modules.knowledge.test_upload_writes_rows_after_ingest_2365 import (  # noqa: F401
    OWNER,
    _drafts,
    _FailingCommit,
    _knowledge,
    _upload,
    session_factory,
)
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer, seed_enterprises, seed_users

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

CASE_KEY = "case-2369"


def _job(job_id: str, *, kind: str, key: str | None = None) -> list:
    """A conversion job and the source row its key requires."""
    enterprise = _current_enterprise_id.get()
    file_id = f"file_{job_id}"
    return [
        UploadedFileModel(
            file_id=file_id, enterprise_id=enterprise, filename="f", size_bytes=1
        ),
        ConversionJobModel(
            id=job_id,
            user_id=OWNER,
            enterprise_id=enterprise,
            scope="personal",
            status="completed",
            source_file_id=file_id,
            source_type=kind,
            live_case_id=key,
        ),
    ]


def _draft(draft_id: str, job_id: str, *, item_id: str | None = None):
    return ConversionDraftModel(
        id=draft_id,
        enterprise_id=_current_enterprise_id.get(),
        conversion_id=job_id,
        runbook_id=f"rb-{draft_id}",
        title="Redis Evictions",
        content=valid_runbook("Redis Evictions Under Memory Pressure"),
        status="verified" if item_id else "draft",
        validation_passed=True,
        knowledge_item_id=item_id,
    )


async def _add(factory, *rows) -> None:
    """Insert ``rows`` in order: a row's keys name rows before it."""
    async with factory() as session:
        for row in rows:
            session.add(row)
            await session.flush()
        await session.commit()


async def _publish(factory, knowledge, kind: str) -> tuple[str, str]:
    """Publish a runbook as ``kind`` does; return its item and draft ids."""
    if kind == "upload":
        document_id = (await _upload(knowledge))["document_id"]
        async with factory() as session:
            (draft_id,) = (
                await session.execute(
                    select(ConversionDraftModel.id).where(
                        ConversionDraftModel.runbook_id == document_id
                    )
                )
            ).scalars()
        return document_id, draft_id
    key = CASE_KEY if kind == "case" else None
    await _add(
        factory, *_job("conv_2369", kind=kind, key=key), _draft("d", "conv_2369")
    )
    published = await ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=AsyncMock(),
        settings=MagicMock(),
        db_session_factory=factory,
        knowledge_service=knowledge,
    ).verify_draft("conv_2369", "d", OWNER)
    return published.knowledge_item_id, "d"


async def _draft_and_key(factory, draft_id: str):
    """The draft's row, and its job's live case key."""
    async with factory() as session:
        draft = await session.get(ConversionDraftModel, draft_id)
        job = await session.get(ConversionJobModel, draft.conversion_id)
    return draft, job.live_case_id


@pytest.mark.parametrize("kind", ["document", "case", "upload"])
async def test_deleting_a_published_runbook_discards_its_draft(session_factory, kind):
    knowledge = _knowledge(session_factory)
    item_id, draft_id = await _publish(session_factory, knowledge, kind)
    published, key = await _draft_and_key(session_factory, draft_id)
    assert (published.status, published.knowledge_item_id) == ("verified", item_id)
    assert key == (CASE_KEY if kind == "case" else None)

    result = await knowledge.delete_document(item_id)

    assert result["action"] == "deleted"
    assert await _drafts(session_factory) == []
    draft, key = await _draft_and_key(session_factory, draft_id)
    assert (draft.status, draft.knowledge_item_id) == ("discarded", None)
    assert draft.content == published.content
    # The case is free to be converted again.
    assert key is None


async def test_each_job_keeps_its_key_while_it_has_a_live_draft(session_factory):
    """Drafts of several jobs name the item. The one whose job still has
    another live draft keeps that job's key; the other job's is released."""
    knowledge = _knowledge(session_factory)
    item_id, draft_id = await _publish(session_factory, knowledge, "case")
    await _add(
        session_factory,
        *_job("conv_b", kind="case", key="case-b"),
        _draft("other", "conv_2369"),
        _draft("b", "conv_b", item_id=item_id),
    )

    await knowledge.delete_document(item_id)

    assert [d["draft_id"] for d in await _drafts(session_factory)] == ["other"]
    for draft, key in (("d", CASE_KEY), ("b", None)):
        row, held = await _draft_and_key(session_factory, draft)
        assert (row.status, row.knowledge_item_id, held) == ("discarded", None, key)


@pytest.mark.parametrize("failure", ["zero_rows", "vectors", "commit", "cancelled"])
async def test_a_delete_that_does_not_complete_keeps_its_draft(
    session_factory, monkeypatch, failure
):
    """No row matched, the vector delete failed, the commit failed, or the task
    was cancelled inside the discard: the discard rolls back with the row
    change. These pass where nothing discards; they pin that the discard is
    the delete's own, so it is never applied without the delete."""
    factory = _FailingCommit(session_factory, (RuntimeError("commit failed"), False))
    knowledge = _knowledge(factory)
    item_id, draft_id = await _publish(session_factory, knowledge, "case")
    parked = asyncio.Event()

    async def park(*_args):
        parked.set()
        await asyncio.Event().wait()

    if failure == "zero_rows":
        monkeypatch.setattr(
            DatabaseKnowledgeItemRepository, "delete", AsyncMock(return_value=False)
        )
    elif failure == "vectors":
        knowledge._vector_store.delete_documents_by_parent_id.side_effect = OSError()
    elif failure == "commit":
        factory.armed = True
    else:
        monkeypatch.setattr(draft_retirement, "release_live_case_key_if_drained", park)

    task = asyncio.create_task(knowledge.delete_document(item_id))
    if failure == "cancelled":
        await parked.wait()
        task.cancel()
    (outcome,) = await asyncio.gather(task, return_exceptions=True)

    expected = {
        "zero_rows": dict,
        "vectors": KnowledgeBaseError,
        "commit": RuntimeError,
        "cancelled": asyncio.CancelledError,
    }[failure]
    assert isinstance(outcome, expected), outcome
    assert failure != "zero_rows" or outcome["success"] is False
    draft, key = await _draft_and_key(session_factory, draft_id)
    assert (draft.status, draft.knowledge_item_id, key) == (
        "verified",
        item_id,
        CASE_KEY,
    )
    assert [d["draft_id"] for d in await _drafts(session_factory)] == [draft_id]


async def test_a_kb_reset_discards_the_drafts_of_what_it_deletes(
    session_factory, monkeypatch, capsys
):
    """``fm-reset-kb`` deletes every item and by default keeps the drafts: the
    ones that published those items are discarded first."""
    knowledge = _knowledge(session_factory)
    _, case_draft = await _publish(session_factory, knowledge, "case")
    _, upload_draft = await _publish(session_factory, knowledge, "upload")
    monkeypatch.setattr(database, "get_db_session", session_factory)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=True
    )

    assert code == 0
    assert await _drafts(session_factory) == []
    for draft_id in (case_draft, upload_draft):
        draft, key = await _draft_and_key(session_factory, draft_id)
        assert (draft.status, draft.knowledge_item_id, key) == ("discarded", None, None)
    assert "discarding the 2 that published" in capsys.readouterr().out


_needs_postgres = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
)


@pytest.fixture
async def pg_factory():
    """The configured PostgreSQL, with an enterprise of the test's own: the
    database is shared with every other ``-m postgres`` test."""
    enterprise = f"ent_2369_{uuid4().hex[:8]}"
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    token = _current_enterprise_id.set(enterprise)
    try:
        async with factory() as session:
            await seed_enterprises(session, [enterprise])
            await seed_users(session, [OWNER], enterprise_id=enterprise)
        yield factory
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


@pytest.mark.postgres
@_needs_postgres
async def test_a_deleted_runbooks_draft_is_discarded_on_postgresql(pg_factory):
    knowledge = _knowledge(pg_factory)
    item_id, draft_id = await _publish(pg_factory, knowledge, "upload")

    await knowledge.delete_document(item_id)

    draft, _ = await _draft_and_key(pg_factory, draft_id)
    assert (draft.status, draft.knowledge_item_id) == ("discarded", None)
    assert draft_id not in [d["draft_id"] for d in await _drafts(pg_factory)]


@pytest.mark.postgres
@_needs_postgres
async def test_a_draft_linked_while_the_delete_runs_is_discarded(pg_factory):
    """A transaction links a draft to the item (``verify_draft``'s swap) and is
    still open when the delete starts. Its key check holds a key-share lock on
    the item row, so the delete's lock on that row waits for the link, and the
    discard then sees it. Unlocked, the discard missed the draft, the
    ``DELETE`` waited, and the key's ``SET NULL`` left it verified, naming
    nothing."""
    knowledge = _knowledge(pg_factory)
    item_id, draft_id = await _publish(pg_factory, knowledge, "upload")
    job_id = (await _draft_and_key(pg_factory, draft_id))[0].conversion_id
    await _add(pg_factory, _draft("linked", job_id))

    async with pg_factory() as linking, pg_factory() as probe:
        await linking.execute(
            update(ConversionDraftModel)
            .where(ConversionDraftModel.id == "linked")
            .values(status="verified", knowledge_item_id=item_id)
        )
        linker = (await linking.execute(text("SELECT pg_backend_pid()"))).scalar()
        deleting = asyncio.create_task(knowledge.delete_document(item_id))
        waiting = text(
            "SELECT count(*) FROM pg_locks "
            "WHERE NOT granted AND :pid = ANY(pg_blocking_pids(pid))"
        )
        while not deleting.done():
            blocked = (await probe.execute(waiting, {"pid": linker})).scalar()
            await probe.rollback()
            if blocked:
                break
            await asyncio.sleep(0.01)
        await linking.commit()
        assert (await deleting)["success"] is True

    draft, _ = await _draft_and_key(pg_factory, "linked")
    assert (draft.status, draft.knowledge_item_id) == ("discarded", None)
