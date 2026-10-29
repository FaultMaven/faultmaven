"""Deleting a published runbook discards the draft that published it (#2369).

A published draft keeps its row: ``verified``, naming its item through
``conversion_drafts.knowledge_item_id``, whose key is ``ON DELETE SET NULL``.
``delete_document`` and ``fm-reset-kb`` left that row as it was, so
``list_all_drafts`` went on listing a verified draft that named nothing. Both
now discard it in their own transaction, before their ``DELETE`` runs, with the
item rows locked: the text is kept, the draft leaves the list, and a case job's
live key is released once its last live draft goes. A delete that does not
complete leaves the draft as it was.

``fm-reset-kb --all-drafts`` deletes the drafts instead. On either path the
reset then releases the live key of each case job left with no live draft, so a
case whose pending draft this reset or an earlier one deleted converts again
through ``convert_from_case`` (#2387). A dry run releases no key.

The SQLite tests run on the #2365 tests' database, which enforces foreign keys
as the app's engine does, so a discard issued after the ``DELETE`` would match
nothing. Four run on PostgreSQL: one races a link against the delete, and two
run a reset, in a database of their own.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import delete, make_url, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from faultmaven.cli import reset_kb
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import _current_enterprise_id
from faultmaven.infrastructure.llm.providers import LLMResponse, StopReason
from faultmaven.infrastructure.persistence import database
from faultmaven.infrastructure.persistence.models import (
    Base,
    CaseModel,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KnowledgeItemModel,
    ResourceShareModel,
    UploadedFileModel,
    UserModel,
)
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.knowledge.domain.models.conversion import (
    CaseConversionRequest,
    ConversionResponse,
    ConversionStatus,
    SourceFileInfo,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    draft_retirement,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.job_persistence import (
    _persist_job,
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
from tests.unit.modules.knowledge.test_conversion_service import (
    _case_analysis,
    _make_case_draft,
)
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


def _draft(draft_id: str, job_id: str, *, item_id: str | None = None, status=None):
    return ConversionDraftModel(
        id=draft_id,
        enterprise_id=_current_enterprise_id.get(),
        conversion_id=job_id,
        runbook_id=f"rb-{draft_id}",
        title="Redis Evictions",
        content=valid_runbook("Redis Evictions Under Memory Pressure"),
        status=status or ("verified" if item_id else "draft"),
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


async def _open_case(factory, case_id: str = CASE_KEY) -> None:
    """The case row ``conversion_jobs.case_id`` keys on."""
    enterprise = _current_enterprise_id.get()
    await _add(factory, CaseModel(case_id=case_id, enterprise_id=enterprise, title="t"))


async def _convert(factory, case_id: str = CASE_KEY) -> ConversionResponse:
    """Convert the case through ``convert_from_case``, its model mocked to
    write a valid runbook."""
    router = AsyncMock()
    router.route.return_value = LLMResponse(
        content=valid_runbook("Redis Evictions Under Memory Pressure"),
        confidence=0.9,
        provider="test",
        model="test-model",
        tokens_used=100,
        response_time_ms=10,
        stop_reason=StopReason.STOP,
    )
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    converter = ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=router,
        settings=settings,
        db_session_factory=factory,
        knowledge_service=None,
    )
    return await converter.convert_from_case(
        CaseConversionRequest(
            case_id=case_id,
            title="Redis evictions",
            description="Keys are evicted under memory pressure.",
            root_cause="maxmemory is set below the working set",
        ),
        user_id=OWNER,
        enterprise_id=_current_enterprise_id.get(),
    )


async def _key_of(factory, job_id: str):
    async with factory() as session:
        return (await session.get(ConversionJobModel, job_id)).live_case_id


async def _reset(factory, monkeypatch, *, all_drafts: bool, dry_run=False) -> int:
    monkeypatch.setattr(database, "get_db_session", factory)
    return await reset_kb.reset_kb(
        dry_run=dry_run, all_drafts=all_drafts, rebuild=False, keep_chroma=True
    )


async def _orphan_a_key(factory, case_id: str = CASE_KEY) -> str:
    """Convert the case, then delete its drafts as ``--all-drafts`` did
    before #2387, keeping its job's key; return the job's id."""
    job_id = (await _convert(factory, case_id)).conversion_id
    async with factory() as session:
        await session.execute(
            delete(ConversionDraftModel).where(
                ConversionDraftModel.conversion_id == job_id
            )
        )
        await session.commit()
    assert await _key_of(factory, job_id) == case_id
    return job_id


async def _persist_case_job(factory, job_id: str, *draft_ids: str) -> None:
    """Write a case job with several pending drafts through ``_persist_job``:
    ``convert_from_case`` writes one draft per job."""
    await _persist_job(
        factory,
        None,
        conversion_id=job_id,
        user_id=OWNER,
        enterprise_id=_current_enterprise_id.get(),
        scope="personal",
        team_id=None,
        status=ConversionStatus.COMPLETED,
        source_file=SourceFileInfo(filename="case", size_bytes=1, content_type="x"),
        analysis=_case_analysis(),
        drafts=[_make_case_draft(draft_id=draft_id) for draft_id in draft_ids],
        created_at=datetime.now(timezone.utc),
        source_type="case",
        case_id=CASE_KEY,
        sanitizer=regex_only_sanitizer(),
    )


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


async def test_the_key_release_counts_discards_not_yet_flushed(session_factory):
    """The scan's sweep flips statuses on loaded rows, and the app's sessions
    do not autoflush (``database.get_engine``'s sessionmaker), so
    ``release_drained_case_keys`` flushes before it counts."""
    await _add(
        session_factory,
        *_job("conv_2369", kind="case", key=CASE_KEY),
        _draft("a", "conv_2369"),
        _draft("b", "conv_2369"),
    )
    async with session_factory(autoflush=False) as session:
        drafts = [await session.get(ConversionDraftModel, d) for d in ("a", "b")]
        for draft in drafts:
            draft.status = "discarded"
        await draft_retirement.release_drained_case_keys(
            session, [(d.id, d.conversion_id) for d in drafts]
        )
        await session.commit()

    assert (await _draft_and_key(session_factory, "a"))[1] is None


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
    ones that published those items are discarded first. One the scan already
    discarded, which still names its item, is not counted again."""
    knowledge = _knowledge(session_factory)
    item_id, case_draft = await _publish(session_factory, knowledge, "case")
    _, upload_draft = await _publish(session_factory, knowledge, "upload")
    await _add(
        session_factory,
        _draft("old", "conv_2369", item_id=item_id, status="discarded"),
    )
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


async def _reset_every_draft_then_convert_again(factory, monkeypatch) -> None:
    """A case's conversion is pending. ``fm-reset-kb --all-drafts`` deletes its
    draft and releases its job's key, so converting the case again completes:
    with the key held, ``uq_conversion_jobs_live_case_id`` refused it."""
    await _open_case(factory)
    before = await _convert(factory)
    (pending,) = before.drafts
    assert await _key_of(factory, before.conversion_id) == CASE_KEY

    assert await _reset(factory, monkeypatch, all_drafts=True) == 0

    async with factory() as session:
        assert await session.get(ConversionDraftModel, pending.draft_id) is None
    after = await _convert(factory)
    assert after.status == ConversionStatus.COMPLETED
    assert await _key_of(factory, after.conversion_id) == CASE_KEY


async def test_a_reset_of_every_draft_frees_their_cases(session_factory, monkeypatch):
    await _reset_every_draft_then_convert_again(session_factory, monkeypatch)


async def test_a_job_losing_two_drafts_to_a_reset_is_released_once(
    session_factory, monkeypatch, capsys
):
    await _open_case(session_factory)
    await _persist_case_job(session_factory, "conv_two", "first", "second")

    await _reset(session_factory, monkeypatch, all_drafts=True)

    assert await _key_of(session_factory, "conv_two") is None
    out = capsys.readouterr().out
    assert "Deleted 2 conversion_drafts rows (--all-drafts)." in out
    assert "Released 1 case conversion keys held with no live draft." in out


@pytest.mark.parametrize("all_drafts", [False, True])
async def test_a_reset_releases_a_key_an_earlier_reset_left_without_a_draft(
    session_factory, monkeypatch, capsys, all_drafts
):
    await _open_case(session_factory)
    job_id = await _orphan_a_key(session_factory)

    assert await _reset(session_factory, monkeypatch, all_drafts=all_drafts) == 0

    assert await _key_of(session_factory, job_id) is None
    out = capsys.readouterr().out
    assert "Released 1 case conversion keys held with no live draft." in out
    assert (await _convert(session_factory)).status == ConversionStatus.COMPLETED


async def test_a_dry_run_with_all_drafts_changes_nothing(
    session_factory, monkeypatch, capsys
):
    """One case's conversion is pending and another's key has no draft left:
    ``--dry-run --all-drafts`` deletes no draft and releases no key."""
    await _open_case(session_factory)
    await _open_case(session_factory, "case-2387-orphan")
    (pending,) = (await _convert(session_factory)).drafts
    orphan_job = await _orphan_a_key(session_factory, "case-2387-orphan")

    code = await _reset(session_factory, monkeypatch, all_drafts=True, dry_run=True)

    assert code == 0
    draft, key = await _draft_and_key(session_factory, pending.draft_id)
    assert (draft.status, key) == ("draft", CASE_KEY)
    assert await _key_of(session_factory, orphan_job) == "case-2387-orphan"
    assert "(dry-run) No changes made." in capsys.readouterr().out


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
        # A bounded number of polls, not a clock: the delete is waiting, or
        # done, long before they run out.
        for _ in range(600):
            if deleting.done():
                break
            blocked = (await probe.execute(waiting, {"pid": linker})).scalar()
            await probe.rollback()
            if blocked:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the delete never waited on the link")
        await linking.commit()
        assert (await deleting)["success"] is True

    draft, _ = await _draft_and_key(pg_factory, "linked")
    assert (draft.status, draft.knowledge_item_id) == ("discarded", None)


@pytest.fixture
async def own_pg():
    """A PostgreSQL database of the test's own, dropped after: a reset deletes
    every item it can see, and the configured database is shared with every
    other ``-m postgres`` test."""
    url = make_url(os.environ["DATABASE_URL"])
    name = f"fm2369_{uuid4().hex[:8]}"
    admin = create_async_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_async_engine(url.set(database=name), poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            await seed_enterprises(session, [STANDALONE_ENTERPRISE_ID])
            await seed_users(session, [OWNER], enterprise_id=STANDALONE_ENTERPRISE_ID)
        yield factory
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


@pytest.mark.postgres
@_needs_postgres
async def test_a_runbook_published_during_a_kb_reset_is_kept_whole(
    own_pg, monkeypatch, capsys
):
    """An upload lands between the reset's discard and its ``DELETE``. The
    reset deletes only the items it locked, so the new runbook and its draft
    stand together; a ``DELETE`` of every row would have left that draft
    verified, naming nothing."""
    knowledge = _knowledge(own_pg)
    _, before = await _publish(own_pg, knowledge, "upload")
    discard = draft_retirement.discard_drafts_naming_any_item
    landed: dict[str, str] = {}

    async def discard_then_upload(session):
        retired = await discard(session)
        landed["item"], landed["draft"] = await _publish(own_pg, knowledge, "upload")
        return retired

    monkeypatch.setattr(
        draft_retirement, "discard_drafts_naming_any_item", discard_then_upload
    )
    monkeypatch.setattr(database, "get_db_session", own_pg)

    code = await reset_kb.reset_kb(
        dry_run=False, all_drafts=False, rebuild=False, keep_chroma=True
    )

    assert code == 0
    draft, _ = await _draft_and_key(own_pg, before)
    assert (draft.status, draft.knowledge_item_id) == ("discarded", None)
    draft, _ = await _draft_and_key(own_pg, landed["draft"])
    assert (draft.status, draft.knowledge_item_id) == ("verified", landed["item"])
    assert "Kept 1 knowledge_items rows written while" in capsys.readouterr().out


@pytest.mark.postgres
@_needs_postgres
async def test_a_reset_of_every_draft_frees_their_cases_on_postgresql(
    own_pg, monkeypatch
):
    await _reset_every_draft_then_convert_again(own_pg, monkeypatch)
