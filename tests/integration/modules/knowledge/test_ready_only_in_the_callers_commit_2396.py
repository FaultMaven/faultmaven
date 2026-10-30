"""An authored runbook is ready only in the commit that records its caller's success (#2396).

``ingest_runbook`` used to complete an authored row in a transaction of its own,
and its caller then recorded its success in a second one: ``verify_draft``'s
compare-and-swap of the draft, ``upload_document``'s upload, job and draft
rows. Whatever stopped the second commit (a lost swap, an exception from its
``execute`` or its ``commit``, a cancellation, a COMMIT in doubt) left a
complete, listed, shared item while the caller reported failure; the
withdrawals meant to undo it ran on some of those routes only, and could fail
themselves. Now the completion runs the caller's ``record`` and commits once.
The cleanup deletes only an incomplete row, and its guarded delete decides a
completing COMMIT that raised: it matched the row, so the COMMIT did not land;
it matched nothing and the row is complete, so it landed; it raised, so the
caller is told the outcome is unknown. Driven through the real services on
FK-on SQLite, reusing the #2365 harness.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.exceptions import ConflictError
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    ConversionJobModel,
    KnowledgeItemModel,
    UploadedFileModel,
)
from faultmaven.modules.knowledge.api import conversion_routes
from faultmaven.modules.knowledge.api.routes import get_knowledge_service
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.exceptions import (
    INGEST_OUTCOME_UNKNOWN_DETAIL,
    KB_INGEST_OUTCOME_UNKNOWN,
    IngestOutcomeUnknown,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
    KnowledgeItemRepositoryException,
)
from tests.integration.modules.knowledge.test_upload_writes_rows_after_ingest_2365 import (  # noqa: F401
    NOTHING,
    OWNER,
    PARK,
    _drafts,
    _FailingCommit,
    _knowledge,
    _rows,
    _upload,
    session_factory,
)
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

CONVERSION, DRAFT = "conv_2396", "draft_2396"


async def _verifiable_draft(factory) -> None:
    """An uploaded file, its job, and a draft that passed validation."""
    async with factory() as session:
        session.add(
            UploadedFileModel(
                file_id="file_2396",
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                filename="f",
                size_bytes=1,
            )
        )
        await session.flush()
        session.add(
            ConversionJobModel(
                id=CONVERSION,
                user_id=OWNER,
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                scope="personal",
                status="completed",
                source_file_id="file_2396",
                source_type="document",
            )
        )
        session.add(
            ConversionDraftModel(
                id=DRAFT,
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                conversion_id=CONVERSION,
                runbook_id="rb-2396",
                title="Redis Evictions",
                content=valid_runbook("Redis Evictions Under Memory Pressure"),
                status="draft",
                validation_passed=True,
            )
        )
        await session.commit()


def _conversion(factory, knowledge) -> ConversionService:
    return ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=AsyncMock(),
        settings=MagicMock(),
        db_session_factory=factory,
        knowledge_service=knowledge,
    )


async def _state(factory) -> tuple[str, dict]:
    """The draft's status, and each item row: whether the draft names it, and
    whether it is ready."""
    async with factory() as session:
        draft = await session.get(ConversionDraftModel, DRAFT)
        items = (
            await session.execute(
                select(KnowledgeItemModel.item_id, KnowledgeItemModel.ingest_complete)
            )
        ).all()
    return draft.status, {
        item_id: (item_id == draft.knowledge_item_id, complete)
        for item_id, complete in items
    }


VERIFIED = "verified"


def _verified(items: dict) -> bool:
    """One item, ready, and the draft names it."""
    return list(items.values()) == [(True, True)]


# ---------------------------------------------------------------------------
# verify_draft
# ---------------------------------------------------------------------------


async def test_an_exception_from_the_swap_publishes_nothing(session_factory):
    """The swap's ``execute`` raised inside the completing transaction: the
    completion rolls back with it, and the cleanup removes the row. Before
    #2396 the item stayed complete and live, the draft still a draft."""
    await _verifiable_draft(session_factory)
    knowledge = _knowledge(session_factory)
    error = RuntimeError("swap failed")

    def swap_raises():
        session = session_factory()
        real_execute = session.execute

        async def execute(statement, *args, **kwargs):
            if getattr(statement, "is_dml", False) and "conversion_drafts" in str(
                statement
            ):
                raise error
            return await real_execute(statement, *args, **kwargs)

        session.execute = execute
        return session

    knowledge._db_session_factory = swap_raises

    with pytest.raises(RuntimeError) as raised:
        await _conversion(session_factory, knowledge).verify_draft(
            CONVERSION, DRAFT, OWNER
        )

    assert raised.value is error
    assert await _state(session_factory) == ("draft", {})


@pytest.mark.parametrize("landed", [False, True], ids=["refused", "landed"])
@pytest.mark.parametrize(
    "error",
    [RuntimeError("commit failed"), asyncio.CancelledError()],
    ids=["error", "cancelled"],
)
async def test_a_verify_whose_commit_raised_is_whole_or_absent(
    session_factory, error, landed
):
    """The one commit that completes the item and verifies the draft was
    refused, or landed and still raised or was cancelled. Landed: the item is
    ready and the draft verified, and an exception becomes the verify's
    success while a cancellation is re-raised. Refused: no item, and the draft
    is still a draft."""
    await _verifiable_draft(session_factory)
    factory = _FailingCommit(session_factory, (error, landed))
    knowledge = _knowledge(factory)
    factory.arm_at_completion(knowledge)
    conversion = _conversion(session_factory, knowledge)
    cancelled = isinstance(error, asyncio.CancelledError)

    if landed and not cancelled:
        response = await conversion.verify_draft(CONVERSION, DRAFT, OWNER)
        assert response.status == VERIFIED
    else:
        expected = (
            asyncio.CancelledError if cancelled else KnowledgeItemRepositoryException
        )
        with pytest.raises(expected) as raised:
            await conversion.verify_draft(CONVERSION, DRAFT, OWNER)
        assert raised.value is error or raised.value.__cause__ is error

    status, items = await _state(session_factory)
    if landed:
        assert status == VERIFIED and _verified(items)
    else:
        assert (status, items) == ("draft", {})


async def test_a_verify_cancelled_at_its_commit_by_an_anyio_scope_publishes_nothing(
    session_factory,
):
    """The request's anyio scope is cancelled while the completing commit is
    outstanding: the shielded cleanup still runs, and removes the row the
    commit never completed. Replaces the test of a withdrawal surviving that
    cancellation, whose withdrawal is gone."""
    await _verifiable_draft(session_factory)
    factory = _FailingCommit(session_factory, (PARK, False))
    knowledge = _knowledge(factory)
    factory.arm_at_completion(knowledge)
    conversion = _conversion(session_factory, knowledge)

    async def run():
        await conversion.verify_draft(CONVERSION, DRAFT, OWNER)

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await factory.parked.wait()
        group.cancel_scope.cancel()

    assert await _state(session_factory) == ("draft", {})


@pytest.mark.parametrize("landed", [False, True], ids=["refused", "landed"])
async def test_an_unknown_outcome_is_reported_unknown_never_failed(
    session_factory, monkeypatch, landed
):
    """The completing commit raised, and so did the cleanup's delete, which
    would have told whether it landed. ``verify_batch`` reports the draft
    ``unknown`` with the sentence that tells the caller to look first, and the
    id to look for. The state is consistent either way: the item ready and the
    draft verified, or an incomplete row (never listed or served) and a
    draft."""
    await _verifiable_draft(session_factory)
    factory = _FailingCommit(session_factory, (RuntimeError("commit failed"), landed))
    knowledge = _knowledge(factory)
    factory.arm_at_completion(knowledge)
    monkeypatch.setattr(
        DatabaseKnowledgeItemRepository,
        "delete",
        AsyncMock(side_effect=OSError("down")),
    )

    summary = await _conversion(session_factory, knowledge).verify_batch(
        [(CONVERSION, DRAFT)], OWNER
    )

    (result,) = summary["results"]
    assert (result["status"], result["error"]) == (
        "unknown",
        INGEST_OUTCOME_UNKNOWN_DETAIL,
    )
    assert (summary["failed"], summary["unknown"]) == (0, 1)
    status, items = await _state(session_factory)
    assert list(items) == [result["knowledge_item_id"]]
    if landed:
        assert status == VERIFIED and _verified(items)
    else:
        assert (status, list(items.values())) == ("draft", [(False, False)])


# ---------------------------------------------------------------------------
# Only a failure from the completing COMMIT on is in doubt
# ---------------------------------------------------------------------------
#
# Each failure below is paired with a cleanup DELETE that raises, which leaves
# the cleanup unable to tell whether a COMMIT landed. Before the COMMIT is
# issued nothing can have landed, so the failure is re-raised as itself. A
# COMMIT that raised with such a cleanup is ``IngestOutcomeUnknown``: pinned by
# ``test_an_unknown_outcome_is_reported_unknown_never_failed`` above.


def _cleanup_delete_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        DatabaseKnowledgeItemRepository,
        "delete",
        AsyncMock(side_effect=OSError("down")),
    )


@pytest.mark.parametrize("via", ["verify", "batch"])
async def test_a_lost_swap_is_a_conflict_even_when_the_cleanup_fails(
    session_factory, monkeypatch, via
):
    """The draft was edited during ingestion: the swap missed, and the
    completion rolled back before any COMMIT. The outcome is known, so
    ``verify_draft`` answers its 409 ``changed_during_verification`` and
    ``verify_batch`` records ``failed``, never ``unknown``. The draft is still
    a draft, and the row the cleanup could not remove is incomplete."""
    await _verifiable_draft(session_factory)
    knowledge = _knowledge(session_factory)
    edited = valid_runbook("Redis Evictions Edited During Verify")

    async def index_while_edited(*_args, **_kwargs):
        async with session_factory() as session:
            await session.execute(update(ConversionDraftModel).values(content=edited))
            await session.commit()
        return 3

    knowledge._index_document_in_vector_store = AsyncMock(
        side_effect=index_while_edited
    )
    _cleanup_delete_raises(monkeypatch)
    conversion = _conversion(session_factory, knowledge)

    if via == "verify":
        with pytest.raises(ConflictError) as lost:
            await conversion.verify_draft(CONVERSION, DRAFT, OWNER)
        assert lost.value.conflict_reason == "changed_during_verification"
    else:
        summary = await conversion.verify_batch([(CONVERSION, DRAFT)], OWNER)
        (result,) = summary["results"]
        assert result["status"] == "failed"
        assert (summary["failed"], summary["unknown"]) == (1, 0)

    status, items = await _state(session_factory)
    assert (status, list(items.values())) == ("draft", [(False, False)])


async def test_a_flip_miss_is_its_own_error_even_when_the_cleanup_fails(
    session_factory, monkeypatch
):
    """The row was removed while the vectors were written, so the flip matched
    nothing and no COMMIT was issued: the upload raises the flip's own error,
    never ``IngestOutcomeUnknown``."""
    service = _knowledge(session_factory)

    async def index_while_removed(document, *_args, **_kwargs):
        async with session_factory() as session:
            await session.execute(
                delete(KnowledgeItemModel).where(
                    KnowledgeItemModel.item_id == document.document_id
                )
            )
            await session.commit()
        return 3

    service._index_document_in_vector_store = AsyncMock(side_effect=index_while_removed)
    _cleanup_delete_raises(monkeypatch)

    with pytest.raises(RuntimeError, match="not marked complete"):
        await _upload(service)

    assert await _rows(session_factory) == NOTHING


class _CloseFailsToo(_FailingCommit):
    """As ``_FailingCommit``; the armed session's close then raises too, after
    it has closed."""

    def __call__(self):
        armed = self.armed and bool(self._failures)
        session = super().__call__()
        if armed:
            real_close = session.close

            async def close():
                await real_close()
                raise OSError("close failed")

            session.close = close
        return session


async def test_a_cancellation_replaced_by_a_close_failure_is_never_a_success(
    session_factory,
):
    """The completing COMMIT landed and then raised ``CancelledError``, and the
    session's close raised while it propagated: the ``OSError`` that arrives
    carries the cancellation only as its ``__context__``. It is a cancellation
    all the same, so it is re-raised as it arrived and never answered as a
    success. The upload is whole: its COMMIT landed."""
    factory = _CloseFailsToo(session_factory, (asyncio.CancelledError(), True))
    service = _knowledge(factory)
    factory.arm_at_completion(service)

    with pytest.raises(OSError, match="close failed") as raised:
        await _upload(service, scope="team")

    assert isinstance(raised.value.__context__, asyncio.CancelledError)
    assert await _rows(session_factory) == dict.fromkeys(NOTHING, 1)


# ---------------------------------------------------------------------------
# upload_document
# ---------------------------------------------------------------------------


class _FailingFlush(_FailingCommit):
    """As ``_FailingCommit``, but the armed session's first explicit flush (the
    upload's ``record``) raises its error."""

    def __call__(self):
        session = self._factory()
        if self.armed and self._failures:
            error, _ = self._failures.pop(0)

            async def flush(*_args, **_kwargs):
                raise error

            session.flush = flush
        return session


@pytest.mark.parametrize("scope", ["personal", "team"])
async def test_an_upload_whose_record_raises_leaves_nothing(
    session_factory, caplog, scope
):
    """The upload's rows failed inside the completing transaction: no item, no
    upload, job or draft row, and at team scope no share. The class, never the
    text, is logged: a failed INSERT's text carries the document."""
    error = RuntimeError("INSERT params: BODY-2396")
    factory = _FailingFlush(session_factory, (error, False))
    service = _knowledge(factory)
    factory.arm_at_completion(service)

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError) as raised:
        await _upload(service, scope=scope)

    assert raised.value is error
    assert await _rows(session_factory) == NOTHING
    assert await _drafts(session_factory) == []
    assert "Failed to upload document: RuntimeError" in caplog.text
    assert "BODY-2396" not in caplog.text


# ---------------------------------------------------------------------------
# The HTTP surface of an unknown outcome
# ---------------------------------------------------------------------------


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(conversion_routes.router, prefix="/api/v1")
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.state.team_service = None
    app.dependency_overrides[require_authentication] = lambda: SimpleNamespace(
        user_id=OWNER,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        organization_id=None,
        is_platform_admin=lambda: False,
    )
    return app


@pytest.mark.parametrize("route", ["upload", "verify"])
def test_an_unknown_outcome_answers_500_with_its_code_and_no_retry_after(route):
    """Not a 503, which here says nothing was saved: a client honouring its
    ``Retry-After`` would publish the upload twice. The upload route's blanket
    500 must not swallow it either."""
    app = _app()
    unknown = AsyncMock(side_effect=IngestOutcomeUnknown("kb_0123456789abcdef"))
    client = TestClient(app, raise_server_exceptions=False)
    if route == "upload":
        app.dependency_overrides[get_knowledge_service] = lambda: SimpleNamespace(
            upload_document=unknown
        )
        response = client.post(
            "/api/v1/knowledge/documents",
            data={"title": "T", "document_type": "runbook", "scope": "personal"},
            files={"file": ("rb.md", b"# A runbook\n", "text/markdown")},
        )
    else:
        app.dependency_overrides[conversion_routes._get_conversion_service] = (
            lambda: SimpleNamespace(verify_draft=unknown)
        )
        response = client.post(
            f"/api/v1/knowledge/conversions/{CONVERSION}/drafts/{DRAFT}/verify"
        )

    unknown.assert_awaited_once()
    assert response.status_code == 500, response.text
    assert response.headers["x-error-code"] == KB_INGEST_OUTCOME_UNKNOWN
    assert "retry-after" not in response.headers
    assert response.json()["detail"] == INGEST_OUTCOME_UNKNOWN_DETAIL
