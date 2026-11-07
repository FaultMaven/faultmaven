"""Deleting a draft or a document purges its conversion's LLM traces first (#2575).

Every model call that carried an uploaded document's text is tagged with the
conversion and its enterprise. Deleting a draft of a conversion that made model
calls, or a document one of its drafts published, removes those traces before
any row changes, with no session open across the trace backend's calls; when
they cannot be removed the delete fails and the row stays as it was. A
conversion that made no model call (a template, a scan, a direct upload) has
no traces and erases nothing. With no trace backend configured nothing is
erased; with no eraser wired every purge fails. ``fm-reset-kb`` purges the same
way, first.

The document delete is keyed on the ``conversion_drafts`` rows naming the item,
never on its ``source_url``, which the uploader writes. Driven through the real
services, routes and CLI on a SQLite database that enforces foreign keys, with
the trace eraser a double that records what the database held when it was
called, or the real eraser over a client whose backend is unreachable.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.container.providers import services
from faultmaven.exceptions import ServiceException
from faultmaven.infrastructure.observability import trace_erasure, tracing
from faultmaven.infrastructure.observability.trace_erasure import LLMTraceEraser
from faultmaven.infrastructure.persistence.models import (
    ConversionDraftModel,
    KnowledgeItemModel,
)
from faultmaven.modules.auth.contracts import DevUser
from faultmaven.modules.knowledge.api import conversion_routes
from faultmaven.modules.knowledge.api import routes as knowledge_routes
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.case_erasure import (
    RunbookConversionEraser,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
)
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope
from faultmaven.utils.runbook_id import item_id_from_runbook_id
from tests.integration.modules.knowledge.test_delete_discards_its_draft_2369 import (
    CASE_KEY,
    _add,
    _convert,
    _draft,
    _job,
    _open_case,
    _reset,
)
from tests.integration.modules.knowledge.test_upload_writes_rows_after_ingest_2365 import (  # noqa: F401
    OWNER,
    _Shares,
    _upload,
    session_factory,
)
from tests.runbook_samples import valid_runbook
from tests.unit.infrastructure.observability.test_trace_erasure_2102 import (
    _FakeClient,
)
from tests.unit.modules.knowledge.test_conversion_model_boundary_redaction_1901 import (
    _DOCUMENT,
    _SETTINGS,
    RecordingRouter,
)
from tests.unit.modules.knowledge.test_drafts_live_in_the_database_1950 import (
    TEMPLATE,
)
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

ENT = STANDALONE_ENTERPRISE_ID
DOWN = ServiceException("Cannot clear llm_traces for conversion: unreachable")
#: What a raw SDK error can carry: the backend's response body.
BODY = "Opik answered 503: <html>backend body with internal detail</html>"


class _Sessions:
    """The session factory, counting the sessions open."""

    def __init__(self, factory) -> None:
        self._factory, self.open = factory, 0

    @asynccontextmanager
    async def __call__(self):
        self.open += 1
        try:
            async with self._factory() as session:
                yield session
        finally:
            self.open -= 1


class _Eraser:
    """Records each erase with the drafts and items the database held then and
    the sessions the services had open; raises ``fails`` for the conversions
    it names."""

    def __init__(self, factory, sessions, fails=None, only=None) -> None:
        self._factory, self._sessions = factory, sessions
        self.fails, self.only, self.calls = fails, only, []
        #: Awaited before each erase returns: what another request does meanwhile.
        self.meanwhile = None

    async def erase_conversion(self, conversion_id: str, enterprise_id: str) -> int:
        drafts, items = await _state(self._factory)
        self.calls.append(
            SimpleNamespace(
                owner=(conversion_id, enterprise_id),
                drafts=drafts,
                items=items,
                open=self._sessions.open,
            )
        )
        if self.fails and self.only in (None, conversion_id):
            raise self.fails
        if self.meanwhile:
            await self.meanwhile()
        return 1

    @property
    def owners(self):
        return [call.owner for call in self.calls]


@pytest.fixture
def stack(session_factory):  # noqa: F811
    """The two services over one database, sharing one eraser double."""
    sessions = _Sessions(session_factory)
    eraser = _Eraser(session_factory, sessions)

    def build(trace_eraser=eraser):
        vectors = MagicMock()
        vectors.delete_documents_by_parent_id = AsyncMock(return_value=3)
        knowledge = KnowledgeService(
            sanitizer=regex_only_sanitizer(),
            tracer=MagicMock(),
            vector_store=vectors,
            db_session_factory=sessions,
            share_repository=_Shares(session_factory),
            trace_eraser=trace_eraser,
        )
        knowledge._index_document_in_vector_store = AsyncMock(return_value=3)
        conversion = ConversionService(
            sanitizer=regex_only_sanitizer(),
            llm_router=AsyncMock(),
            settings=MagicMock(),
            db_session_factory=sessions,
            knowledge_service=knowledge,
            trace_eraser=trace_eraser,
        )
        return SimpleNamespace(
            factory=session_factory,
            knowledge=knowledge,
            conversion=conversion,
            eraser=eraser,
        )

    return build


def _model_job(job_id: str) -> list:
    """A document conversion's job, as one that analysed a failure mode stores
    it (``trace_purge``), and its source row."""
    rows = _job(job_id, kind="document")
    rows[1].failure_modes_detected = 1
    return rows


async def _published(kb, job_id: str, title: str, draft_id: str = "") -> str:
    """A draft of a document conversion, verified; return the item it
    published. A ``draft_id`` names a second draft of a job already written."""
    row = _draft(draft_id or f"d_{job_id}", job_id)
    row.content, row.runbook_id = valid_runbook(title), f"rb-{row.id}"
    await _add(kb.factory, *([] if draft_id else _model_job(job_id)), row)
    published = await kb.conversion.verify_draft(job_id, row.id, OWNER)
    return published.knowledge_item_id


def _unreachable(monkeypatch) -> tuple:
    """The real eraser, its backend configured, over a client whose reads
    raise the SDK's own error, body text included."""
    monkeypatch.setattr(tracing, "OPIK_AVAILABLE", True)
    monkeypatch.setattr(tracing, "_tracing_configured", True)
    monkeypatch.setattr(trace_erasure, "_parsed_filters", lambda text, entity: text)
    client = _FakeClient()

    def refused(**_):
        raise httpx.ConnectError(BODY)

    client.trace_page = refused
    return LLMTraceEraser("faultmaven", client_factory=lambda: client), client


async def _state(factory):
    """The drafts' statuses and the items present."""
    async with factory() as session:
        drafts = dict(
            (
                await session.execute(
                    select(ConversionDraftModel.id, ConversionDraftModel.status)
                )
            ).all()
        )
        items = set(
            (await session.execute(select(KnowledgeItemModel.item_id))).scalars()
        )
    return drafts, items


async def _seed_item(factory, item: KnowledgeItem) -> None:
    async with factory() as session:
        await DatabaseKnowledgeItemRepository(session).create(item)


def _item(item_id: str, scope=KnowledgeScope.PERSONAL, **fields) -> KnowledgeItem:
    return KnowledgeItem(
        item_id=item_id,
        enterprise_id=ENT,
        organization_id=None,
        title="Redis Evictions",
        content=valid_runbook("Redis Evictions Under Memory Pressure"),
        item_type=KnowledgeItemType.RUNBOOK,
        scope=scope,
        owner_id=None if scope == KnowledgeScope.GLOBAL else OWNER,
        is_published=True,
        **fields,
    )


def _user() -> DevUser:
    return DevUser(
        user_id=OWNER,
        username=OWNER,
        email="o@e",
        display_name="o",
        created_at=datetime.now(timezone.utc),
        roles=["user"],
    )


def _app(kb) -> FastAPI:
    app = FastAPI()
    app.include_router(knowledge_routes.router)
    app.include_router(conversion_routes.router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[knowledge_routes.get_knowledge_service] = (
        lambda: kb.knowledge
    )
    app.dependency_overrides[require_authentication] = _user
    app.dependency_overrides[conversion_routes._get_conversion_service] = (
        lambda: kb.conversion
    )
    app.dependency_overrides[conversion_routes._require_auth] = _user
    return app


async def _delete(kb, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app(kb), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.delete(path)


class TestTheDraftDelete:
    async def test_it_erases_the_conversion_before_the_row_with_no_session_open(
        self, stack
    ):
        kb = stack()
        await _add(
            kb.factory,
            *_model_job("conv_a"),
            _draft("d1", "conv_a"),
            _draft("d2", "conv_a"),
        )

        assert await kb.conversion.delete_draft("conv_a", "d1", OWNER) is True

        (call,) = kb.eraser.calls
        assert call.owner == ("conv_a", ENT)
        assert call.drafts == {"d1": "draft", "d2": "draft"}
        assert call.open == 0
        assert (await _state(kb.factory))[0] == {"d1": "discarded", "d2": "draft"}

    async def test_a_refused_erase_leaves_the_draft(self, stack):
        kb = stack()
        kb.eraser.fails = DOWN
        await _add(kb.factory, *_model_job("conv_a"), _draft("d1", "conv_a"))

        with pytest.raises(ServiceException) as refused:
            await kb.conversion.delete_draft("conv_a", "d1", OWNER)

        assert refused.value is DOWN
        assert (await _state(kb.factory))[0] == {"d1": "draft"}

    async def test_an_unreachable_backend_answers_500_and_keeps_the_draft(
        self, stack, monkeypatch, caplog
    ):
        eraser, client = _unreachable(monkeypatch)
        kb = stack(trace_eraser=eraser)
        await _add(kb.factory, *_model_job("conv_a"), _draft("d1", "conv_a"))

        with caplog.at_level(logging.INFO):
            answered = await _delete(kb, "/knowledge/conversions/conv_a/drafts/d1")

        assert answered.status_code == 500
        assert answered.json()["detail"] == "Failed to delete draft"
        assert "llm_traces for conversion conv_a: ConnectError" in caplog.text
        assert BODY not in caplog.text and BODY not in answered.text
        assert (await _state(kb.factory))[0] == {"d1": "draft"}

    async def test_no_trace_backend_deletes_with_no_erase(self, stack):
        kb = stack(trace_eraser=None)
        await _add(kb.factory, *_model_job("conv_a"), _draft("d1", "conv_a"))

        answered = await _delete(kb, "/knowledge/conversions/conv_a/drafts/d1")

        assert answered.status_code == 204
        assert kb.eraser.calls == []
        assert (await _state(kb.factory))[0] == {"d1": "discarded"}

    async def test_a_draft_that_is_not_found_erases_nothing(self, stack):
        kb = stack()
        await _add(kb.factory, *_model_job("conv_a"), _draft("d1", "conv_a"))

        assert await kb.conversion.delete_draft("conv_a", "nope", OWNER) is False
        assert await kb.conversion.delete_draft("conv_a", "d1", "someone") is False

        assert kb.eraser.calls == []

    async def test_a_draft_gone_during_the_erase_answers_false(self, stack):
        kb = stack()
        await _add(kb.factory, *_model_job("conv_a"), _draft("d1", "conv_a"))

        async def deleted_meanwhile():
            async with kb.factory() as session:
                await session.execute(
                    delete(ConversionDraftModel).where(ConversionDraftModel.id == "d1")
                )
                await session.commit()

        kb.eraser.meanwhile = deleted_meanwhile

        assert await kb.conversion.delete_draft("conv_a", "d1", OWNER) is False
        assert kb.eraser.owners == [("conv_a", ENT)]


class TestTheDocumentDelete:
    async def test_it_erases_the_conversion_before_the_row_with_no_session_open(
        self, stack
    ):
        kb = stack()
        item_id = await _published(kb, "conv_a", "Redis Evictions Under Pressure")

        result = await kb.knowledge.delete_document(item_id)

        assert result["action"] == "deleted"
        (call,) = kb.eraser.calls
        assert call.owner == ("conv_a", ENT)
        assert item_id in call.items and call.drafts == {"d_conv_a": "verified"}
        assert call.open == 0
        assert await _state(kb.factory) == ({"d_conv_a": "discarded"}, set())

    async def test_an_unreachable_backend_answers_500_and_keeps_the_document(
        self, stack, monkeypatch, caplog
    ):
        eraser, _client = _unreachable(monkeypatch)
        kb = stack(trace_eraser=eraser)
        item_id = await _published(kb, "conv_a", "Redis Evictions Under Pressure")

        with caplog.at_level(logging.INFO):
            answered = await _delete(kb, f"/knowledge/documents/{item_id}")

        assert answered.status_code == 500
        assert answered.json()["detail"] == "Failed to delete document"
        assert "llm_traces for conversion conv_a: ConnectError" in caplog.text
        assert BODY not in caplog.text and BODY not in answered.text
        assert await _state(kb.factory) == ({"d_conv_a": "verified"}, {item_id})

    async def test_no_trace_backend_deletes_with_no_erase(self, stack):
        kb = stack(trace_eraser=None)
        item_id = await _published(kb, "conv_a", "Redis Evictions Under Pressure")

        answered = await _delete(kb, f"/knowledge/documents/{item_id}")

        assert answered.status_code == 204
        assert kb.eraser.calls == []
        assert (await _state(kb.factory))[1] == set()

    async def test_the_bulk_delete_erases_each_conversion_once(self, stack):
        kb = stack()
        first = await _published(kb, "conv_a", "Redis Evictions Under Pressure")
        second = await _published(kb, "conv_a", "Redis Swap Storms", "d_conv_a2")
        other = await _published(kb, "conv_b", "Kafka Consumer Lag On Rebalance")

        result = await kb.knowledge.bulk_delete_documents([first, second, other])

        assert (result["deleted_count"], result["errors"]) == (3, [])
        assert kb.eraser.owners == [("conv_a", ENT), ("conv_b", ENT)]
        assert (await _state(kb.factory))[1] == set()

    async def test_the_bulk_delete_stops_erasing_at_the_first_refusal(
        self, stack, monkeypatch
    ):
        """The rest are counted failed with no other call to the backend, a
        document with nothing to purge included."""
        eraser, client = _unreachable(monkeypatch)
        kb = stack(trace_eraser=eraser)
        first = await _published(kb, "conv_a", "Redis Evictions Under Pressure")
        other = await _published(kb, "conv_b", "Kafka Consumer Lag On Rebalance")
        upload = (await _upload(kb.knowledge))["document_id"]

        result = await kb.knowledge.bulk_delete_documents([first, other, upload])

        assert result["deleted_count"] == 0
        assert result["errors"] == [
            f"Document {doc}: delete failed" for doc in (first, other, upload)
        ]
        assert len(client.trace_reads) == 1
        assert (await _state(kb.factory))[1] == {first, other, upload}

    async def test_it_is_keyed_on_the_drafts_not_on_the_source_url(self, stack):
        """``source_url`` is a field the uploader writes: naming another
        conversion there purges nothing."""
        kb = stack()
        await _published(kb, "conv_victim", "Redis Evictions Under Pressure")
        item_id = str(uuid4())
        await _seed_item(
            kb.factory, _item(item_id, source_url="conversion:conv_victim")
        )

        assert (await kb.knowledge.delete_document(item_id))["action"] == "deleted"

        assert kb.eraser.calls == []

    async def test_a_built_in_unpublish_erases_nothing(self, stack):
        """The unpublish keeps the row; even a draft naming it is not read."""
        kb = stack()
        builtin = item_id_from_runbook_id("redis-evictions")
        await _seed_item(kb.factory, _item(builtin, scope=KnowledgeScope.GLOBAL))
        await _add(
            kb.factory, *_model_job("conv_a"), _draft("d1", "conv_a", item_id=builtin)
        )

        result = await kb.knowledge.delete_document(builtin)

        assert result["action"] == "unpublished"
        assert kb.eraser.calls == []

    async def test_a_case_runbook_whose_drafts_the_case_delete_removed_erases_nothing(
        self, stack
    ):
        """Its traces carry the case, and went with the case's delete."""
        kb = stack()
        await _open_case(kb.factory)
        converted = await _convert(kb.factory)
        (draft,) = converted.drafts
        published = await kb.conversion.verify_draft(
            converted.conversion_id, draft.draft_id, OWNER
        )
        await RunbookConversionEraser(kb.factory).erase_case(CASE_KEY, ENT)

        result = await kb.knowledge.delete_document(published.knowledge_item_id)

        assert result["action"] == "deleted"
        assert kb.eraser.calls == []


class TestOnlyAConversionThatMadeModelCallsIsPurged:
    """The marker is ``conversion_jobs.failure_modes_detected``, as each path
    that writes a job stores it (``trace_purge``)."""

    async def test_a_document_conversions_draft(self, stack, tmp_path):
        kb = stack()
        source = tmp_path / "replica-notes.md"
        source.write_text(_DOCUMENT)
        converted = await ConversionService(
            llm_router=RecordingRouter(),
            settings=_SETTINGS,
            db_session_factory=kb.factory,
            sanitizer=regex_only_sanitizer(),
            trace_eraser=None,
        ).convert_document(
            file_path=source,
            content_type="text/markdown",
            original_filename=source.name,
            scope="personal",
            user_id=OWNER,
            enterprise_id=None,
        )
        (draft,) = converted.drafts

        await kb.conversion.delete_draft(converted.conversion_id, draft.draft_id, OWNER)

        assert kb.eraser.owners == [(converted.conversion_id, ENT)]

    async def test_a_case_conversions_draft(self, stack):
        kb = stack()
        await _open_case(kb.factory)
        converted = await _convert(kb.factory)
        (draft,) = converted.drafts

        await kb.conversion.delete_draft(converted.conversion_id, draft.draft_id, OWNER)

        assert kb.eraser.owners == [(converted.conversion_id, ENT)]

    @pytest.mark.parametrize("made_by", ["template", "scan"])
    async def test_a_draft_made_with_no_model_call(
        self, stack, made_by, tmp_path, monkeypatch
    ):
        kb = stack()
        if made_by == "template":
            await kb.conversion.create_runbook_from_template(
                **{**TEMPLATE, "user_id": OWNER}
            )
        else:
            (tmp_path / "personal_ops").mkdir()
            (tmp_path / "personal_ops" / "redis.md").write_text(
                valid_runbook("Redis Evictions Under Memory Pressure")
            )
            monkeypatch.setattr(
                ConversionService, "_data_dir", property(lambda self: tmp_path)
            )
            await kb.conversion.scan_for_runbooks(user_id=OWNER)
        async with kb.factory() as session:
            ((conversion_id, draft_id),) = (
                await session.execute(
                    select(ConversionDraftModel.conversion_id, ConversionDraftModel.id)
                )
            ).all()

        assert await kb.conversion.delete_draft(conversion_id, draft_id, OWNER) is True

        assert kb.eraser.calls == []

    async def test_a_direct_upload(self, stack):
        """``upload_document`` writes a job and a verified draft naming the item
        (#2365); it made no model call."""
        kb = stack()
        item_id = (await _upload(kb.knowledge))["document_id"]

        assert (await kb.knowledge.delete_document(item_id))["action"] == "deleted"

        assert kb.eraser.calls == []


@pytest.mark.parametrize("service", ["knowledge", "conversion"])
async def test_a_service_given_no_eraser_fails_every_purge(stack, service):
    """``None`` is "not configured"; leaving the argument out is not."""
    kb = stack()
    item_id = await _published(kb, "conv_a", "Redis Evictions Under Pressure")
    if service == "knowledge":
        unwired = KnowledgeService(
            sanitizer=regex_only_sanitizer(),
            tracer=MagicMock(),
            db_session_factory=kb.factory,
        )
        call = unwired.delete_document(item_id)
    else:
        unwired = ConversionService(
            llm_router=AsyncMock(), settings=MagicMock(), db_session_factory=kb.factory
        )
        call = unwired.delete_draft("conv_a", "d_conv_a", OWNER)

    with pytest.raises(Exception, match="conversion conv_a: no trace eraser was wired"):
        await call

    assert await _state(kb.factory) == ({"d_conv_a": "verified"}, {item_id})


class TestTheKbReset:
    """``fm-reset-kb`` erases each conversion of what it deletes, first."""

    @pytest.fixture
    def reset_eraser(self, stack, monkeypatch):
        kb = stack()
        monkeypatch.setattr(services, "llm_trace_eraser", lambda settings: kb.eraser)
        monkeypatch.setattr(tracing, "init_opik_tracing", lambda settings=None: None)
        return kb

    @pytest.mark.parametrize("all_drafts", [False, True], ids=["default", "all-drafts"])
    async def test_it_purges_before_it_deletes(
        self, reset_eraser, monkeypatch, all_drafts
    ):
        kb = reset_eraser
        item_id = await _published(kb, "conv_a", "Redis Evictions Under Pressure")
        await _upload(kb.knowledge)
        await _add(kb.factory, *_model_job("conv_p"), _draft("pending", "conv_p"))

        assert await _reset(kb.factory, monkeypatch, all_drafts=all_drafts) == 0

        owners = [("conv_a", ENT)] + ([("conv_p", ENT)] if all_drafts else [])
        assert kb.eraser.owners == owners
        assert all(item_id in call.items for call in kb.eraser.calls)
        assert (await _state(kb.factory))[1] == set()

    async def test_a_refused_purge_deletes_nothing(self, reset_eraser, monkeypatch):
        kb = reset_eraser
        kb.eraser.fails = DOWN
        item_id = await _published(kb, "conv_a", "Redis Evictions Under Pressure")

        assert await _reset(kb.factory, monkeypatch, all_drafts=True) == 1

        assert await _state(kb.factory) == ({"d_conv_a": "verified"}, {item_id})
