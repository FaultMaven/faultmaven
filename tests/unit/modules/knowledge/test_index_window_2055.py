"""A KB document this deployment cannot index within its request window is
refused before anything is written (#2055).

Invariant: on the upload, update and verify paths a document estimated to need
more than ``INDEX_REQUEST_WINDOW_SECONDS`` of indexing is refused before any
row, vector or embed: 413, or 503 + ``Retry-After`` when queued work or a cold
model is the obstacle. The failed-commit compensation is exempt; the boot
repair reports a refused row; evidence vectorization takes its trigger's budget.

The real services run over in-memory SQLite. The embed is a double patched at
``embed_texts_or_raise``, as the existing indexing tests patch it, which is what
shows the refusal does not live inside it. Window 60 s on one core at the
measured rate: the sample runbook (1.2 KB, ~9 s) fits; 10 KB (~76 s) does not.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
)
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.bootstrap.kb_init import _repair_orphaned_rows
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.settings import get_settings
from faultmaven.core.investigation.milestone_engine.vectorization import (
    EvidenceVectorizer,
)
from faultmaven.infrastructure import embedding_guard as guard
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KnowledgeItemModel,
    UploadedFileModel,
)
from faultmaven.models.exceptions import IndexBudgetRefused
from faultmaven.models.interfaces import ToolResult
from faultmaven.modules.agent.tools import vectorize_file_tool
from faultmaven.modules.knowledge.api import conversion_routes, routes
from faultmaven.modules.knowledge.domain.models.conversion import DraftStatus
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

_CACHE = "faultmaven.infrastructure.model_cache"
_DOC = "kb_0123456789ab"
_BIG = "# Too big\n\n" + "Check the node pressure conditions before evicting.\n" * 200


@pytest.fixture
def window(monkeypatch):
    """A 60 s window, one core, the measured rate, the model loaded."""
    monkeypatch.setattr(get_settings().knowledge, "index_request_window_seconds", 60)
    with (
        patch(f"{_CACHE}.inference_cores", return_value=1.0),
        patch.object(guard, "_rate", guard._LearnedRate()),
        patch(f"{_CACHE}.model_cache.peek_bge_m3_model", return_value=object()),
    ):
        yield


@pytest.fixture
def embed():
    double = AsyncMock(side_effect=lambda texts, **_: [[0.5] * 8 for _ in texts])
    with patch(
        "faultmaven.infrastructure.embedding_guard.embed_texts_or_raise", double
    ):
        yield double


@pytest.fixture
async def service():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(enterprise_id=STANDALONE_ENTERPRISE_ID, name="E", slug="e")
        )
        await session.commit()
    store = MagicMock(
        delete_documents_by_parent_id=AsyncMock(), add_documents=AsyncMock()
    )
    yield KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=store,
        db_session_factory=factory,
    )
    await engine.dispose()


async def _rows(service) -> dict:
    async with service._db_session_factory() as session:
        return {
            model.__tablename__: (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            for model in (
                KnowledgeItemModel,
                ConversionDraftModel,
                ConversionJobModel,
                UploadedFileModel,
            )
        }


async def _ingest(service, content: str) -> None:
    await service.ingest_runbook(
        document_id=_DOC,
        title="Draining a node",
        content=content,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope="personal",
        owner_id="u1",
    )


# --- the KB paths --------------------------------------------------------------


async def test_an_upload_too_large_for_the_window_writes_nothing(
    window, embed, service
):
    with pytest.raises(IndexBudgetRefused) as refused:
        await service.upload_document(
            content=_BIG, title="Draining", document_type="runbook", scope="personal"
        )

    assert refused.value.retry_after_seconds is None
    assert set((await _rows(service)).values()) == {0}
    embed.assert_not_awaited()


async def test_a_document_inside_the_window_indexes_as_before(window, embed, service):
    await _ingest(service, valid_runbook())

    assert (await _rows(service))["knowledge_items"] == 1
    embed.assert_awaited_once()


async def test_an_update_too_large_leaves_the_document_and_its_vectors(
    window, embed, service
):
    await _ingest(service, valid_runbook())
    embed.reset_mock()
    service._vector_store.reset_mock()

    with pytest.raises(IndexBudgetRefused):
        await service.update_document_metadata(_DOC, content=_BIG)

    async with service._db_session_factory() as session:
        assert (await session.get(KnowledgeItemModel, _DOC)).content == valid_runbook()
    embed.assert_not_awaited()
    service._vector_store.delete_documents_by_parent_id.assert_not_awaited()
    service._vector_store.add_documents.assert_not_awaited()


async def test_the_failed_commit_compensation_is_not_refused(
    window, embed, service, monkeypatch
):
    monkeypatch.setattr(get_settings().knowledge, "index_request_window_seconds", 0)
    await _ingest(service, _BIG)  # stored before the window was set
    monkeypatch.setattr(get_settings().knowledge, "index_request_window_seconds", 60)
    embed.reset_mock()

    await service._resynchronise_after_failed_commit(
        _DOC, attempted_content="other", commit_error=RuntimeError("commit")
    )

    embed.assert_awaited_once()  # at 60 s it would have been refused
    service._vector_store.add_documents.assert_awaited()


async def test_the_boot_repair_reports_a_refused_row(
    window, embed, service, monkeypatch
):
    monkeypatch.setattr(get_settings().knowledge, "index_request_window_seconds", 0)
    await _ingest(service, _BIG)
    monkeypatch.setattr(get_settings().knowledge, "index_request_window_seconds", 60)
    embed.reset_mock()
    refused: list = []

    repaired, orphaned = await _repair_orphaned_rows(
        [_DOC], service, role_may_update=True, refused=refused
    )

    assert (repaired, orphaned, refused) == ([], [_DOC], [_DOC])
    embed.assert_not_awaited()


async def test_verify_refuses_a_draft_before_ingesting_it(window):
    draft = SimpleNamespace(
        status=DraftStatus.DRAFT.value,
        validation_passed=True,
        content=_BIG,
        title="Draining a node",
        runbook_id="rb",
    )
    job = SimpleNamespace(
        user_id="u1", scope="personal", enterprise_id=STANDALONE_ENTERPRISE_ID
    )
    results = iter([job, draft])
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=lambda _: MagicMock(scalar_one_or_none=lambda: next(results))
    )
    factory = MagicMock(
        return_value=MagicMock(
            __aenter__=AsyncMock(return_value=session), __aexit__=AsyncMock()
        )
    )
    knowledge = MagicMock(ingest_runbook=AsyncMock())
    service = ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=factory,
        knowledge_service=knowledge,
    )

    with pytest.raises(IndexBudgetRefused):
        await service.verify_draft("conv", "draft", "u1")
    knowledge.ingest_runbook.assert_not_awaited()


# --- the routes ----------------------------------------------------------------


@pytest.fixture
def cold_model():
    """No model resident, so a busy 503 waits for exactly one load."""
    with patch(f"{_CACHE}.model_cache.peek_bge_m3_model", return_value=None):
        yield


def _client(router, overrides) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.team_service = None
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.dependency_overrides.update(overrides)

    async def _user():
        return SimpleNamespace(user_id="user-1", is_platform_admin=lambda: False)

    app.dependency_overrides[require_authentication] = _user
    return TestClient(app, raise_server_exceptions=False)


_TOO_LARGE = IndexBudgetRefused("This document is too large to index in one request.")
_BUSY = IndexBudgetRefused(
    "Indexing is busy. Retry in about 77 seconds.", retry_after_seconds=77
)


def _kb(service):
    async def _service():
        return service

    return _client(routes.router, {routes.get_knowledge_service: _service})


def _answer(response, refusal):
    assert response.status_code == (
        413 if refusal is _TOO_LARGE else 503
    ), response.text
    assert response.headers["x-error-code"] == refusal.error_code
    if refusal is _TOO_LARGE:
        assert response.json()["detail"] == refusal.message
        assert "retry-after" not in response.headers
    else:
        # Read from the queue as it stands, not off the exception: here only a
        # model load is ahead (``cold_model``).
        assert response.headers["retry-after"] == "180"
        assert "retry in about 180 seconds" in response.json()["detail"]


@pytest.mark.parametrize("refusal", [_TOO_LARGE, _BUSY], ids=["413", "503"])
def test_the_upload_route_answers_the_refusal(refusal, cold_model):
    service = SimpleNamespace(upload_document=AsyncMock(side_effect=refusal))
    response = _kb(service).post(
        "/api/v1/knowledge/documents",
        data={"title": "T", "document_type": "runbook", "scope": "personal"},
        files={"file": ("rb.md", b"# Big\n", "text/markdown")},
    )
    _answer(response, refusal)


@pytest.mark.parametrize("refusal", [_TOO_LARGE, _BUSY], ids=["413", "503"])
def test_the_update_route_answers_the_refusal(refusal, monkeypatch, cold_model):
    monkeypatch.setattr(routes, "_authorize_document_write", AsyncMock())
    service = SimpleNamespace(
        get_document=AsyncMock(return_value={"document_id": "kb_x"}),
        update_document_metadata=AsyncMock(side_effect=refusal),
    )
    response = _kb(service).put(
        "/api/v1/knowledge/documents/kb_x", json={"content": "x"}
    )
    _answer(response, refusal)


@pytest.mark.parametrize("refusal", [_TOO_LARGE, _BUSY], ids=["413", "503"])
def test_the_verify_route_answers_the_refusal(refusal, cold_model):
    service = SimpleNamespace(verify_draft=AsyncMock(side_effect=refusal))
    response = _client(
        conversion_routes.router,
        {conversion_routes._get_conversion_service: lambda: service},
    ).post("/api/v1/knowledge/conversions/conv/drafts/draft/verify")
    _answer(response, refusal)


# --- evidence: each trigger states its budget -----------------------------------


async def test_each_vectorization_trigger_states_its_budget():
    budgets = []

    async def _execute(name, params, context):
        budgets.append(vectorize_file_tool._index_budget())
        return ToolResult(success=False, data=None, error="x")

    file = SimpleNamespace(file_id="f", size_bytes=1_000_000)
    ev = SimpleNamespace(evidence_id="ev", source_file_id="f", vectorized=False)
    case = SimpleNamespace(evidence=[ev], find_uploaded_file=lambda _: file)
    context = SimpleNamespace(in_memory_case=case, case_id="c", case_repository=None)
    vectorizer = EvidenceVectorizer(
        deps=SimpleNamespace(investigation_tools=SimpleNamespace(execute_tool=_execute))
    )

    with patch(
        "faultmaven.core.investigation.turn_budget.spendable_turn_budget",
        return_value=42.0,
    ):
        assert vectorize_file_tool._index_budget() == 42.0  # model-invoked
        tasks = await vectorizer.start_proactive_vectorization(case, context)
        await tasks["ev"]
        await vectorizer._reactive_vectorize("ev", context, "", "tool_timeout")

    reactive = float(get_settings().agent.vectorization_reactive_timeout_seconds)
    assert budgets == [None, reactive]


def test_a_file_too_large_to_index_is_not_announced_as_indexed():
    from faultmaven.core.preprocessing.vector_storage import VectorIndexOutcome

    result = vectorize_file_tool.VectorizeFileTool._not_indexed(
        VectorIndexOutcome.TOO_LARGE, "ev", "logs"
    )

    assert not result.success and "too large to index" in result.error
    assert "says nothing about its contents" in result.error
    assert "search_file" in result.error
