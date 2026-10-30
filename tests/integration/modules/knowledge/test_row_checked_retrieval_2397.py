"""Row-checked KB retrieval: no door serves a chunk without a complete, published row (#2397, #2063).

The owner's ruling (#2397, 2026-10-30): one knowledge-owned check,
``IKnowledgeService.admitted_document_ids``, admits a KB hit only when its parent
has a complete, published ``knowledge_items`` row, and every read door applies
it — the investigation pre-fetch (push), ``search_documents``, the ``kb_qa``
pull and runbook dedup. Writers no longer delete vectors in-process: what a
failed or cancelled write leaves is never served, and is collected later.

Everything here is real but the embedder: an on-disk ChromaDB of the test's
own, FK-on SQLite, the production ``KnowledgeVectorStore``, ``KnowledgeService``
and ``RunbookKnowledgeBase``, and the ``kb_qa`` adapter as the container
registers it. Every query embeds to one vector ``Q``; the three stray parents —
vectors with no row, an incomplete ingest's, an unpublished built-in's — carry
``Q`` itself, so they OUTRANK the three real runbooks on every door, and a
door that skipped the check would serve them first.
"""

from __future__ import annotations

import asyncio
import math
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch

import chromadb
import pytest
from chromadb.config import Settings as ChromaSettings
from fastapi import FastAPI, HTTPException
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import faultmaven.infrastructure.knowledge.knowledge_vector_store as kvs_module
from faultmaven.bootstrap import kb_init
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.container.providers.services import _create_investigation_tools
from faultmaven.core.investigation import terminal_transitions as tt
from faultmaven.core.investigation.milestone_engine.dependencies import EngineDeps
from faultmaven.core.investigation.milestone_engine.kb_prefetch import KbPrefetcher
from faultmaven.exceptions import DatabaseTimeout
from faultmaven.infrastructure.knowledge.admission import MAX_DEEPENING
from faultmaven.infrastructure.knowledge.knowledge_vector_store import (
    KB_COLLECTION,
    KnowledgeVectorStore,
)
from faultmaven.infrastructure.knowledge.runbook_kb import RunbookKnowledgeBase
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.models.api import KnowledgeBaseDocument
from faultmaven.models.exceptions import KnowledgeBaseError
from faultmaven.modules.agent.tools.base import ToolContext
from faultmaven.modules.agent.tools.kb_qa import AnswerFromKB
from faultmaven.modules.agent.tools.kb_tool_adapter import KBToolAdapter
from faultmaven.modules.case.api.routes import reports
from faultmaven.modules.case.contracts import CaseState
from faultmaven.modules.knowledge.api.routes import (
    get_current_user_optional,
    get_knowledge_service,
)
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.modules.knowledge.domain.models.knowledge_item import (
    KnowledgeItem,
    KnowledgeItemType,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
    build_kb_scope_filter,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (  # noqa: E501
    DatabaseKnowledgeItemRepository,
)
from faultmaven.modules.knowledge.taxonomy import KnowledgeScope
from faultmaven.utils.runbook_id import item_id_from_runbook_id
from tests.integration.security.test_case_kb_scope_and_source_gate import (
    _case as _resolved_content_case,
)
from tests.model_content import disabled_redaction
from tests.utils import regex_only_sanitizer, seed_enterprises, seed_users

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

OWNER = "user-2397"
QUERY = "connection pool exhausted queries time out"
SCOPE = build_kb_scope_filter(OWNER, [])
_DIM = 8


def _unit(*leading: float) -> List[float]:
    vector = list(leading) + [0.0] * (_DIM - len(leading))
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


#: Every query embeds to ``Q``; a stray carries ``Q`` itself (cosine 1.0), and
#: the real runbooks sit just below it.
Q = _unit(1.0)
REAL_VECTORS = [_unit(0.95, 0.31), _unit(0.9, 0.44), _unit(0.85, 0.53)]
REALS = [item_id_from_runbook_id(f"real-runbook-{i}") for i in range(3)]
NO_ROW = "kb_" + "a" * 16
INCOMPLETE = "kb_" + "b" * 16
UNPUBLISHED = item_id_from_runbook_id("stray-unpublished")
STRAYS = {NO_ROW: "no-row", INCOMPLETE: "incomplete", UNPUBLISHED: "unpublished"}
AUTHORED_LATE = "kb_" + "c" * 16
PACK_LATE = item_id_from_runbook_id("pack-late-write")


def _text(marker: str) -> str:
    """One body for every chunk, so only the vector tells them apart; the
    marker names the document in a synthesis prompt."""
    return (
        f"# Runbook {marker}\nMARKER::{marker}::\n"
        "Connection pool exhausted; queries time out under load."
    )


def _markers(text_: str) -> set[str]:
    return {
        m
        for m in [*STRAYS.values(), "late", "after-completion"]
        if f"MARKER::{m}::" in text_
    }


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'kb.db'}", poolclass=NullPool
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sessions() as session:
        await seed_enterprises(session, [STANDALONE_ENTERPRISE_ID])
        await seed_users(session, [OWNER], enterprise_id=STANDALONE_ENTERPRISE_ID)
    yield sessions
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fixed_query_embedding():
    embed = AsyncMock(return_value=list(Q))
    with (
        patch.object(kvs_module, "embed_query_or_raise", new=embed),
        patch(
            "faultmaven.infrastructure.knowledge.runbook_kb.embed_query_or_raise",
            new=embed,
        ),
    ):
        yield


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _ingest_shipped(service, item_id, marker, vector):
    return await service.ingest_runbook(
        document_id=item_id,
        title=f"Runbook {marker}",
        content=_text(marker),
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope="global",
        prechunked=[(_text(marker), list(vector))],
        shipped=True,
    )


async def _index_with_no_ingest(service, item_id, marker):
    """Chunks as the live indexer writes them, for an id whose row this call
    does not complete."""
    await service._index_document_in_vector_store(
        KnowledgeBaseDocument(
            document_id=item_id,
            title=f"Runbook {marker}",
            content=_text(marker),
            document_type="runbook",
            scope="global",
            created_at=_now(),
            updated_at=_now(),
        ),
        prechunked=[(_text(marker), list(Q))],
        enterprise_id=STANDALONE_ENTERPRISE_ID,
    )


@pytest.fixture
async def kb(factory, tmp_path):
    chroma = chromadb.PersistentClient(
        path=str(tmp_path / "chroma"),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
    )
    store = KnowledgeVectorStore(chroma)
    service = KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=store,
        db_session_factory=factory,
    )
    for i, item_id in enumerate(REALS):
        await _ingest_shipped(service, item_id, f"real-{i}", REAL_VECTORS[i])

    # The three strays. A parent with vectors and no row:
    await _index_with_no_ingest(service, NO_ROW, "no-row")
    # an authored ingest that never completed:
    async with factory() as session:
        await DatabaseKnowledgeItemRepository(session).create(
            KnowledgeItem(
                item_id=INCOMPLETE,
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                title="Runbook incomplete",
                content=_text("incomplete"),
                item_type=KnowledgeItemType.RUNBOOK,
                scope=KnowledgeScope.GLOBAL,
                ingest_complete=False,
            )
        )
    await _index_with_no_ingest(service, INCOMPLETE, "incomplete")
    # and a built-in unpublished through the real delete (a SQL-only flip):
    await _ingest_shipped(service, UNPUBLISHED, "unpublished", Q)
    assert (await service.delete_document(UNPUBLISHED))["action"] == "unpublished"

    return SimpleNamespace(chroma=chroma, store=store, service=service, factory=factory)


def _raw_top(kb, n: int) -> set[str]:
    """The top ``n`` parents by vector alone, read straight from ChromaDB: the
    positive control that the strays really do outrank the real runbooks."""
    got = kb.chroma.get_collection(KB_COLLECTION).query(
        query_embeddings=[list(Q)], n_results=n, where=SCOPE, include=["metadatas"]
    )
    return {md["parent_document_id"] for md in got["metadatas"][0]}


class _CapturingRouter:
    def __init__(self) -> None:
        self.prompts: List[str] = []

    async def route(self, *, model, messages, max_tokens, temperature, **kwargs):
        self.prompts.append(messages[-1]["content"])
        return LLMResponse(
            content="stub answer",
            confidence=1.0,
            provider="stub",
            model="stub",
            tokens_used=1,
            response_time_ms=1,
        )


def _context() -> ToolContext:
    return ToolContext(
        session_id="session-2397",
        case_id="case-2397",
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        user_id=OWNER,
        kb_scope_filter=SCOPE,
        redaction_ctx=disabled_redaction(),
    )


# --- the four doors ----------------------------------------------------------


async def _push(kb) -> List[str]:
    results = await kb.service.search_knowledge(
        QUERY, limit=3, filters=SCOPE, min_score=0.5
    )
    return [r.parent_document_id for r in results]


async def _documents(kb) -> List[str]:
    found = await kb.service.search_documents(
        QUERY, limit=3, user=SimpleNamespace(user_id=OWNER, enterprise_id=None)
    )
    assert "error" not in found, found
    return [r["document_id"] for r in found["results"]]


def _registered_kb_qa(kb, router):
    """``kb_qa`` exactly as the container registers it: the adapter is built by
    ``_create_investigation_tools``, bound to the knowledge service's check."""
    registry = _create_investigation_tools(
        SimpleNamespace(
            kb_qa_tool=AnswerFromKB(vector_store=kb.store, llm_router=router)
        ),
        knowledge_service=kb.service,
    )
    return registry.get("kb_qa")


async def _pull(kb) -> str:
    router = _CapturingRouter()
    result = await _registered_kb_qa(kb, router).execute_with_context(
        {"question": QUERY}, _context()
    )
    assert result.success, result.error
    return "\n".join([result.data, *router.prompts])


def _dedup_kb(kb) -> RunbookKnowledgeBase:
    return RunbookKnowledgeBase.over_kb_collection(
        kb.chroma, admit=kb.service.admitted_document_ids
    )


async def _dedup(kb) -> List[str]:
    matches = await _dedup_kb(kb).search_by_text(QUERY, scope_filter=SCOPE, top_k=5)
    return [m.item_id for m in matches]


async def test_the_strays_outrank_the_real_runbooks(kb):
    assert _raw_top(kb, 3) == set(STRAYS)


async def test_the_push_serves_no_stray_and_fills_its_window_with_real_hits(kb):
    """k real hits come back where k exist within the recall window, though
    every stray outranks every one of them: the check runs before the cut."""
    assert sorted(await _push(kb)) == sorted(REALS)


async def test_search_documents_serves_no_stray(kb):
    assert sorted(await _documents(kb)) == sorted(REALS)


async def test_kb_qa_hands_no_stray_to_synthesis_or_sources(kb):
    seen = await _pull(kb)
    assert _markers(seen) == set()
    assert "MARKER::real-0::" in seen  # control: it did synthesize
    assert not any(f"Runbook {m}" in seen for m in STRAYS.values())


async def test_dedup_finds_no_stray(kb):
    assert sorted(await _dedup(kb)) == sorted(REALS)


# --- when the check cannot run, each door fails as on a store error ----------


@pytest.fixture
def check_down():
    """The admission check's query raises as ``get_db_session`` does on a
    database timeout."""
    failing = AsyncMock(side_effect=DatabaseTimeout("the database did not answer"))
    with patch.object(DatabaseKnowledgeItemRepository, "admitted_item_ids", failing):
        yield failing


async def test_the_push_goes_on_without_kb_when_the_check_fails(kb, check_down):
    deps = EngineDeps()
    deps.knowledge_service = kb.service
    case = SimpleNamespace(case_id="case-2397", user_id=OWNER, driver_id=None)
    case.kb_context = None
    case.kb_context_origin = None
    case.enterprise_id = STANDALONE_ENTERPRISE_ID

    completed = await KbPrefetcher(deps=deps).prefetch_kb_context(
        case, QUERY, "symptom"
    )

    assert completed is False
    assert case.kb_context is None
    check_down.assert_awaited()  # it failed at the check, not before it


async def test_search_documents_answers_the_unavailable_body_when_the_check_fails(
    kb, check_down
):
    import httpx

    app = FastAPI()
    app.include_router(knowledge_router)
    app.state.team_service = None
    app.dependency_overrides[get_knowledge_service] = lambda: kb.service
    app.dependency_overrides[get_current_user_optional] = lambda: SimpleNamespace(
        user_id=OWNER, enterprise_id=None
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        answered = await client.post("/knowledge/search", json={"query": QUERY})

    assert answered.status_code == 200
    body = answered.json()
    assert body["results"] == []
    assert body["error"].startswith("Knowledge base search is unavailable")
    check_down.assert_awaited()


async def test_kb_qa_reports_a_tool_failure_when_the_check_fails(kb, check_down):
    router = _CapturingRouter()
    result = await _registered_kb_qa(kb, router).execute_with_context(
        {"question": QUERY}, _context()
    )

    assert result.success is False
    assert "Knowledge base query failed" in result.error
    assert router.prompts == []
    check_down.assert_awaited()


async def test_core_dedup_takes_its_failed_caveat_when_the_check_fails(
    kb, check_down, monkeypatch
):
    monkeypatch.setattr(
        tt,
        "assess_runbook_readiness",
        lambda case: MagicMock(verdict=tt.RunbookReadiness.READY, message="ready"),
    )

    async def resolver():
        return SCOPE

    suggestion = await tt.evaluate_runbook_suggestion(
        _resolved_content_case(), _dedup_kb(kb), scope_resolver=resolver
    )

    assert suggestion.verdict == tt.RunbookSuggestion.SUGGEST_WITH_CAVEATS
    assert "the knowledge base search is unavailable" in suggestion.message
    check_down.assert_awaited()


async def test_the_report_route_answers_503_when_the_check_fails(kb, check_down):
    case = _resolved_content_case().model_copy(
        update={"state": CaseState.RESOLVED, "resolved_at": datetime.now(timezone.utc)}
    )
    case_service = SimpleNamespace(get_case=AsyncMock(return_value=case))

    with pytest.raises(HTTPException) as refused:
        await reports.get_report_recommendations(
            case_id=case.case_id,
            request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())),
            case_service=case_service,
            current_user=SimpleNamespace(user_id=OWNER),
            runbook_kb=_dedup_kb(kb),
        )

    assert refused.value.status_code == 503
    check_down.assert_awaited()


# --- #2397: a cancelled ingest's vector write lands after its cleanup ----------


class _LateAdd:
    """``add_documents`` as the HTTP client runs it: the await is cancelled, the
    executor thread is not, and its write lands after the caller has given up
    and cleaned up. ``land`` is that thread finishing."""

    def __init__(self, store: KnowledgeVectorStore) -> None:
        self._real = store.add_documents
        self.started = asyncio.Event()
        self.pending: tuple | None = None

    async def add_documents(self, documents, embeddings=None):
        self.pending = (documents, embeddings)
        self.started.set()
        await asyncio.Event().wait()

    async def land(self) -> None:
        documents, embeddings = self.pending
        await self._real(documents, embeddings=embeddings)


async def _cancelled_mid_add(kb, item_id: str, *, shipped: bool) -> int:
    """Run an ingest whose add is cancelled mid-flight; land the add after the
    cleanup. Returns how many vector deletes ran after the add began."""
    late = _LateAdd(kb.store)
    store = kb.store
    with (
        patch.object(store, "add_documents", late.add_documents),
        patch.object(
            store,
            "delete_documents_by_parent_id",
            wraps=store.delete_documents_by_parent_id,
        ) as vector_delete,
    ):
        task = asyncio.create_task(
            kb.service.ingest_runbook(
                document_id=item_id,
                title="Runbook late",
                content=_text("late"),
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                scope="global",
                prechunked=[(_text("late"), list(Q))],
                shipped=shipped,
            )
        )
        await asyncio.wait_for(late.started.wait(), 10)
        before = vector_delete.await_count  # the indexer's own pre-delete
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        after_cleanup = vector_delete.await_count - before
    await late.land()
    return after_cleanup


def _chunk_parents(kb) -> set[str]:
    got = kb.chroma.get_collection(KB_COLLECTION).get(include=["metadatas"])
    return {md["parent_document_id"] for md in got["metadatas"]}


async def _no_door_serves(kb, item_id: str) -> None:
    assert item_id not in await _push(kb)
    assert item_id not in await _documents(kb)
    assert "MARKER::late::" not in await _pull(kb)
    assert item_id not in await _dedup(kb)


@pytest.mark.parametrize(
    ("item_id", "shipped"),
    [
        pytest.param(AUTHORED_LATE, False, id="authored"),
        pytest.param(PACK_LATE, True, id="pack"),
    ],
)
async def test_a_write_that_lands_after_a_cancelled_ingest_is_never_served(
    kb, item_id, shipped
):
    deletes_after_the_add = await _cancelled_mid_add(kb, item_id, shipped=shipped)

    # No in-process cleanup is left to race the write...
    assert deletes_after_the_add == 0
    # ...the row is gone...
    async with kb.factory() as session:
        assert await DatabaseKnowledgeItemRepository(session).get_by_id(item_id) is None
    # ...the late chunks are in the store, and outrank everything...
    assert item_id in _chunk_parents(kb)
    assert item_id in _raw_top(kb, 4)
    # ...and no door serves them.
    await _no_door_serves(kb, item_id)


async def test_the_next_boot_collects_a_late_pack_write(kb):
    """A built-in id's no-row vectors are the reconcile's. ``knowledge_items``
    holds the real runbooks, so the reconcile runs (it refuses on an empty
    table)."""
    await _cancelled_mid_add(kb, PACK_LATE, shipped=True)
    assert PACK_LATE in _chunk_parents(kb)

    orphaned, _unpublished, _rows = await kb_init._reconcile_vectors(
        kb.service, kb.factory
    )

    assert PACK_LATE in orphaned
    assert PACK_LATE not in _chunk_parents(kb)


async def test_an_authored_raise_after_completion_removes_the_row_and_no_vector(kb):
    """Cancelled at the completing commit's return (critique B2): the row was
    complete, and the cleanup still removes it, so the caller's "failed" is
    true; it deletes no vector, and no door serves the chunks."""
    store = kb.store
    real_complete = kb.service._complete_authored_ingest

    async def complete_then_cancelled(*args, **kwargs):
        await real_complete(*args, **kwargs)
        raise asyncio.CancelledError()

    with (
        patch.object(kb.service, "_complete_authored_ingest", complete_then_cancelled),
        patch.object(
            store,
            "delete_documents_by_parent_id",
            wraps=store.delete_documents_by_parent_id,
        ) as vector_delete,
    ):
        with pytest.raises(asyncio.CancelledError):
            await kb.service.ingest_runbook(
                document_id=AUTHORED_LATE,
                title="Runbook after-completion",
                content=_text("after-completion"),
                enterprise_id=STANDALONE_ENTERPRISE_ID,
                scope="global",
                prechunked=[(_text("after-completion"), list(Q))],
            )

    assert vector_delete.await_count == 1  # the indexer's pre-delete, before its add
    async with kb.factory() as session:
        assert (
            await DatabaseKnowledgeItemRepository(session).get_by_id(AUTHORED_LATE)
            is None
        )
    assert AUTHORED_LATE in _chunk_parents(kb)
    assert AUTHORED_LATE not in await _push(kb)
    assert "MARKER::after-completion::" not in await _pull(kb)


# --- #2063: a delete whose vector delete times out ----------------------------


async def test_a_delete_whose_vector_delete_times_out_leaves_nothing_served(kb):
    await kb.service.ingest_runbook(
        document_id=AUTHORED_LATE,
        title="Runbook late",
        content=_text("late"),
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope="global",
        prechunked=[(_text("late"), list(Q))],
    )
    assert AUTHORED_LATE in await _push(kb)  # control: a live document is served

    with patch.object(
        kb.store,
        "delete_documents_by_parent_id",
        AsyncMock(side_effect=TimeoutError("vector delete timed out")),
    ):
        result = await kb.service.delete_document(AUTHORED_LATE)

    assert result["success"] is True
    assert AUTHORED_LATE in _chunk_parents(kb)  # the store kept them
    await _no_door_serves(kb, AUTHORED_LATE)


# --- crowding: an unpublished built-in fills a door's first page --------------

CROWD_PUBLISHED = item_id_from_runbook_id("crowd-published")
CROWD_RETIRED = item_id_from_runbook_id("crowd-unpublished")
#: At least every door's first fetch: the push's and the pull's 15-chunk
#: recall, ``search_documents``' ``limit * 3`` and dedup's ``top_k * 3``.
CROWD_CHUNKS = 16


async def _crowd(factory, tmp_path, retired_chunks: int):
    """A published runbook, and a built-in whose ``retired_chunks`` chunks all
    outrank it, unpublished by the real delete (a SQL-only flip, so its
    vectors stay until the next pack-loading bootstrap)."""
    chroma = chromadb.PersistentClient(
        path=str(tmp_path / "chroma"),
        settings=ChromaSettings(anonymized_telemetry=False, allow_reset=False),
    )
    store = KnowledgeVectorStore(chroma)
    service = KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=store,
        db_session_factory=factory,
    )
    await _ingest_shipped(service, CROWD_PUBLISHED, "crowd-published", REAL_VECTORS[0])
    await service.ingest_runbook(
        document_id=CROWD_RETIRED,
        title="Runbook crowd-unpublished",
        content=_text("crowd-unpublished"),
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope="global",
        prechunked=[
            (f"{_text('crowd-unpublished')} part {i}", list(Q))
            for i in range(retired_chunks)
        ],
        shipped=True,
    )
    assert (await service.delete_document(CROWD_RETIRED))["action"] == "unpublished"
    return SimpleNamespace(chroma=chroma, store=store, service=service)


@pytest.fixture
async def crowded(factory, tmp_path):
    kb = await _crowd(factory, tmp_path, CROWD_CHUNKS)
    assert _raw_top(kb, 15) == {CROWD_RETIRED}  # control: it fills the window
    return kb


async def test_the_push_reads_past_an_unpublished_builtins_chunks(crowded):
    assert await _push(crowded) == [CROWD_PUBLISHED]


async def test_search_documents_reads_past_an_unpublished_builtins_chunks(crowded):
    assert await _documents(crowded) == [CROWD_PUBLISHED]


async def test_kb_qa_reads_past_an_unpublished_builtins_chunks(crowded):
    seen = await _pull(crowded)
    assert "MARKER::crowd-published::" in seen
    assert "MARKER::crowd-unpublished::" not in seen


async def test_dedup_reads_past_an_unpublished_builtins_chunks(crowded):
    """Core dedup's own call (``top_k=3``): an empty answer here reads as
    "checked, nothing similar" and offers a duplicate."""
    matches = await _dedup_kb(crowded).search_by_text(
        QUERY, scope_filter=SCOPE, top_k=3
    )
    assert [m.item_id for m in matches] == [CROWD_PUBLISHED]


async def test_dedup_refuses_when_still_crowded_at_the_deepest_page(factory, tmp_path):
    """``top_k=3`` reads 9 chunks first and 72 at the cap: an unpublished
    built-in with 72 that outrank everything leaves the search unestablished."""
    kb = await _crowd(factory, tmp_path, 3 * 3 * MAX_DEEPENING)

    with pytest.raises(KnowledgeBaseError) as raised:
        await _dedup_kb(kb).search_by_text(QUERY, scope_filter=SCOPE, top_k=3)

    assert raised.value.error_code == "RUNBOOK_SEARCH_INCOMPLETE"


# --- the check itself -------------------------------------------------------


async def test_the_check_admits_only_complete_published_rows(kb):
    asked = [*REALS, *STRAYS, "kb_absent"]
    assert await kb.service.admitted_document_ids(asked) == set(REALS)
    assert await kb.service.admitted_document_ids([]) == set()


class _RaisingSession:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self):
        raise self._error

    async def __aexit__(self, *exc):
        return False


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(DatabaseTimeout("the database did not answer"), id="timeout"),
        pytest.param(RuntimeError("connection reset"), id="generic"),
    ],
)
async def test_the_check_raises_rather_than_answering_empty(kb, error):
    kb.service._db_session_factory = lambda: _RaisingSession(error)

    with pytest.raises(KnowledgeBaseError) as raised:
        await kb.service.admitted_document_ids(REALS)

    assert raised.value.error_code == "KNOWLEDGE_ADMISSION_UNAVAILABLE"
    assert raised.value.__cause__ is error


async def test_the_check_lets_a_cancellation_through(kb):
    kb.service._db_session_factory = lambda: _RaisingSession(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await kb.service.admitted_document_ids(REALS)


# --- no reader can skip it --------------------------------------------------


async def test_the_stores_refuse_a_read_without_a_check():
    client = MagicMock()
    store = KnowledgeVectorStore(client=client)
    for read in (store.search, store.hybrid_search):
        with pytest.raises(TypeError, match="requires an admit callback"):
            await read(KB_COLLECTION, QUERY, where=SCOPE, admit=None)
    client.get_or_create_collection.assert_not_called()


def test_the_dedup_reader_and_the_adapter_refuse_to_be_built_without_a_check():
    with pytest.raises(TypeError, match="requires an admit callback"):
        RunbookKnowledgeBase(vector_store=MagicMock(), admit=None)
    with pytest.raises(TypeError, match="requires an admit callback"):
        RunbookKnowledgeBase.over_kb_collection(MagicMock(), admit=None)
    with pytest.raises(TypeError, match="requires an admit callback"):
        KBToolAdapter(MagicMock(), admit=None)


# --- PostgreSQL: the check under RLS ------------------------------------------


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL-only; set DATABASE_URL to a migrated PG instance.",
)
async def test_the_check_under_rls_admits_own_and_global_rows_only():
    """As a NOBYPASSRLS role bound to one enterprise, the read policy (own
    enterprise OR global) can only narrow the answer: that enterprise's row and
    a global row are admitted, another enterprise's is not."""
    from tests.integration.security.conftest import (
        create_limited_role,
        drop_limited_role,
        limited_url,
    )

    superuser_url = os.environ["DATABASE_URL"]
    tag = uuid.uuid4().hex[:8]
    role, password = f"fm_2397_{tag}", "pw2397"
    ent_a, ent_b = f"ent_a_{tag}", f"ent_b_{tag}"
    own, foreign, platform = f"kb_own_{tag}", f"kb_foreign_{tag}", f"kb_global_{tag}"
    admin = create_async_engine(superuser_url)
    await create_limited_role(superuser_url, role, password)
    limited = create_async_engine(limited_url(superuser_url, role, password))

    @event.listens_for(limited.sync_engine, "begin")
    def _bind_enterprise(conn):  # what get_engine's listener does per transaction
        conn.execute(
            text("SELECT set_config('app.current_enterprise_id', :e, true)"),
            {"e": ent_a},
        )

    try:
        async with admin.begin() as conn:
            for enterprise in (ent_a, ent_b):
                await conn.execute(
                    text(
                        "INSERT INTO enterprises (enterprise_id, name, slug) "
                        "VALUES (:e, :e, :e)"
                    ),
                    {"e": enterprise},
                )
            for item_id, enterprise, scope in (
                (own, ent_a, "personal"),
                (foreign, ent_b, "personal"),
                (platform, STANDALONE_ENTERPRISE_ID, "global"),
            ):
                await conn.execute(
                    text(
                        "INSERT INTO knowledge_items (item_id, enterprise_id, title, "
                        "content, item_type, scope) VALUES "
                        "(:k, :e, 't', 'c', 'runbook', :s)"
                    ),
                    {"k": item_id, "e": enterprise, "s": scope},
                )
        service = KnowledgeService.__new__(KnowledgeService)
        service._db_session_factory = async_sessionmaker(
            limited, class_=AsyncSession, expire_on_commit=False
        )

        admitted = await service.admitted_document_ids([own, foreign, platform])

        assert admitted == {own, platform}
    finally:
        await limited.dispose()
        async with admin.begin() as conn:
            await conn.execute(
                text("DELETE FROM knowledge_items WHERE item_id IN (:a, :b, :c)"),
                {"a": own, "b": foreign, "c": platform},
            )
            await conn.execute(
                text("DELETE FROM enterprises WHERE enterprise_id IN (:a, :b)"),
                {"a": ent_a, "b": ent_b},
            )
        await admin.dispose()
        await drop_limited_role(superuser_url, role)
