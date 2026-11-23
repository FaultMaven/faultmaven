"""Every KB write that indexes is admitted against the request window before it
writes anything (#2055).

Upload, PUT and verify index inside the request. With
``INDEX_REQUEST_WINDOW_SECONDS`` set, a document that alone cannot fit is
refused 413, one the queue cannot fit 503 + ``Retry-After``, both before the
row; verify-batch returns what the rest of its window cannot fit as
``deferred``. PUT content is held to the draft bound, and bulk-update to
metadata. The boot repair encodes on the same slot. An embed that failed is
503 with its code and its cause's sentence on upload and verify, and adds the
code to PUT's 503 (#2461).

The REAL services over in-memory SQLite and the real indexer and slot; the
encode (``index_slot.encode_in_worker``, the KB slot's worker, #2851), the
token count, the cores and the slot's clock are doubles, so nothing loads the
model. Every KB ticket is charged ``WORKER_LOAD_SECONDS``.

Mutation: remove the upload's admission (``ingest_runbook``'s
``_index_ticket``): ``TestUpload``'s refusals write the row.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
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
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.settings import get_settings
from faultmaven.exceptions import ValidationException
from faultmaven.infrastructure import embedding_guard, index_slot
from faultmaven.infrastructure.index_slot import index_admission
from faultmaven.infrastructure.persistence.models import (
    Base,
    ConversionDraftModel,
    ConversionJobModel,
    EnterpriseModel,
    KbRequestReceiptModel,
    KnowledgeItemModel,
    UploadedFileModel,
)
from faultmaven.models.exceptions import IndexAdmissionRefused
from faultmaven.modules.knowledge.api import routes
from faultmaven.modules.knowledge.api.conversion_routes import (
    router as conversion_router,
)
from faultmaven.modules.knowledge.api.routes import get_knowledge_service
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.modules.knowledge.domain.request_receipts import KbReceiptKey
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.domain.services.knowledge_service import (
    KnowledgeService,
)
from faultmaven.modules.knowledge.draft_content import DRAFT_CONTENT_MAX_CHARS
from tests.runbook_samples import valid_runbook
from tests.utils import record_nothing, regex_only_sanitizer

pytestmark = [pytest.mark.unit, pytest.mark.knowledge_base]

RUNBOOK_TOKENS = 29_746  # 371.825 s warm on 2 cores, 401.825 s with the load
TOO_LARGE = 45_601  # 570.0125 s warm: past the 600 s line (45,440, #2851)
ITEM = "kb_0123456789ab"
_TABLES = (
    KnowledgeItemModel,
    ConversionDraftModel,
    ConversionJobModel,
    UploadedFileModel,
    KbRequestReceiptModel,
)


class Encodes:
    """The KB slot's encode: records each encode and its thread, holding the
    first ``held`` until released."""

    def __init__(self, clock=None, seconds: float = 0.0) -> None:
        self.calls: list[tuple[str, str]] = []
        self.held = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = self.most_active = 0
        self._clock, self._seconds = clock, seconds
        self._lock = threading.Lock()

    def __call__(self, _ticket, texts, _guard_seconds):
        with self._lock:
            self.calls.append((texts[0], threading.current_thread().name))
            self.active += 1
            self.most_active = max(self.most_active, self.active)
            held = len(self.calls) <= self.held
        self.entered.set()
        if held:
            self.release.wait(30)
        if self._clock is not None:
            self._clock.now += self._seconds
        with self._lock:
            self.active -= 1
        return [[0.0] * 4 for _ in texts]


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(index_slot, "_clock", fake)
    return fake


@pytest.fixture(autouse=True)
async def world(monkeypatch, clock):
    """2 cores, the KB encode a double, window 0. Teardown
    waits for every held encode to settle, then asserts no ticket leaked into
    the process's slots, so a failing test cannot fail the next one's setup."""
    monkeypatch.setattr(
        "faultmaven.infrastructure.model_cache._cgroup_cpu_quota", lambda: 2.0
    )
    encodes = Encodes(clock)
    monkeypatch.setattr(index_slot, "encode_in_worker", encodes)
    tokens = AsyncMock(return_value=RUNBOOK_TOKENS)
    monkeypatch.setattr(embedding_guard, "count_index_tokens", tokens)
    monkeypatch.setattr(get_settings().knowledge, "index_request_window_seconds", 0)
    yield SimpleNamespace(encodes=encodes, tokens=tokens)
    encodes.release.set()
    for executor in (index_slot._KB_EXECUTOR, index_slot._EVIDENCE_EXECUTOR):
        # Bounded: a slot left blocked fails the teardown, not the run.
        await asyncio.wait_for(asyncio.wrap_future(executor.submit(lambda: None)), 30)
    assert index_admission.queued() == 0, "a KB ticket leaked"
    assert index_slot.evidence_slot.queued() == 0, "an evidence ticket leaked"


@pytest.fixture
def window(monkeypatch):
    monkeypatch.setattr(get_settings().knowledge, "index_request_window_seconds", 600)


@pytest.fixture
def busy():
    """Another runbook's encode held on this server: 371.825 s pending."""
    held = index_admission.admit_forced(["other"], RUNBOOK_TOKENS, principal="u9")
    yield held
    held.close()


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EnterpriseModel(enterprise_id=STANDALONE_ENTERPRISE_ID, name="E", slug="e")
        )
        await session.commit()
    yield factory
    await engine.dispose()


def _knowledge(session_factory) -> KnowledgeService:
    store = MagicMock(add_documents=AsyncMock(), delete_generation=AsyncMock())
    return KnowledgeService(
        sanitizer=regex_only_sanitizer(),
        tracer=MagicMock(),
        vector_store=store,
        db_session_factory=session_factory,
    )


def _conversion(session_factory, knowledge) -> ConversionService:
    return ConversionService(
        llm_router=MagicMock(),
        settings=MagicMock(),
        db_session_factory=session_factory,
        knowledge_service=knowledge,
        sanitizer=regex_only_sanitizer(),
    )


async def _counts(session_factory) -> dict[str, int]:
    async with session_factory() as session:
        return {
            model.__tablename__: (
                await session.execute(select(func.count()).select_from(model))
            ).scalar_one()
            for model in _TABLES
        }


async def _row(session_factory):
    async with session_factory() as session:
        return await session.get(KnowledgeItemModel, ITEM)


async def _published(service) -> None:
    await service.ingest_runbook(
        document_id=ITEM,
        title="Draining a node",
        content=valid_runbook(),
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope="personal",
        owner_id="u1",
        record=record_nothing,
    )


def _upload(service, **extra):
    return service.upload_document(
        content=valid_runbook(),
        title="Draining a node",
        document_type="runbook",
        scope="personal",
        owner_id="u1",
        **extra,
    )


def _app(*, knowledge=None, conversion=None) -> FastAPI:
    """The real knowledge and conversion routers, with the app's handlers."""
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(conversion_router, prefix="/api/v1")
    app.state.team_service = None
    app.state.conversion_service = conversion
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(HTTPException, http_exception_handler)

    async def _service():
        return knowledge

    async def _user():
        return SimpleNamespace(
            user_id="u1",
            organization_id=None,
            enterprise_id=STANDALONE_ENTERPRISE_ID,
            is_platform_admin=lambda: False,
        )

    app.dependency_overrides[get_knowledge_service] = _service
    app.dependency_overrides[require_authentication] = _user
    return app


def _client(**services) -> TestClient:
    return TestClient(_app(**services), raise_server_exceptions=False)


def _upload_route():
    return _client(knowledge=_knowledge(_no_database())).post(
        "/api/v1/knowledge/documents",
        data={"title": "T", "document_type": "runbook", "scope": "personal"},
        files={"file": ("rb.md", valid_runbook().encode(), "text/markdown")},
    )


def _no_database():
    """A session factory for a request refused before it reads anything."""
    return MagicMock(side_effect=AssertionError("the database was reached"))


# =============================================================================
# Upload
# =============================================================================


class TestUpload:
    async def test_a_busy_refusal_writes_no_row_no_receipt_and_embeds_nothing(
        self, session_factory, world, window, busy
    ):
        key = KbReceiptKey(STANDALONE_ENTERPRISE_ID, "u1", "upload", "k-1", "fp")

        with pytest.raises(IndexAdmissionRefused) as refused:
            await _upload(_knowledge(session_factory), receipt_key=key)

        assert (refused.value.status_code, refused.value.retry_after_seconds) == (
            503,
            402,
        )
        assert set((await _counts(session_factory)).values()) == {0}
        assert world.encodes.calls == []

    async def test_a_too_large_refusal_writes_nothing(
        self, session_factory, world, window
    ):
        world.tokens.return_value = TOO_LARGE

        with pytest.raises(IndexAdmissionRefused) as refused:
            await _upload(_knowledge(session_factory))

        assert refused.value.status_code == 413
        assert set((await _counts(session_factory)).values()) == {0}
        assert world.encodes.calls == []

    async def test_with_a_window_and_no_tokenizer_nothing_is_written(
        self, session_factory, world, window
    ):
        world.tokens.return_value = None

        with pytest.raises(Exception) as refused:
            await _upload(_knowledge(session_factory))

        assert getattr(refused.value, "error_code", None) == (
            "KNOWLEDGE_EMBEDDER_UNAVAILABLE"
        )
        assert set((await _counts(session_factory)).values()) == {0}

    @pytest.mark.parametrize("over", [0, 1])
    async def test_an_uncontended_count_leaves_the_413_line_admitted(
        self, session_factory, world, window, clock, over
    ):
        """#2851 delta B2: the line is the window less the load and the
        count's 2 s allowance (45,440 at 600 s, 2 cores). A count that took
        1.9 s uncontended leaves the document at the line admitted, and one
        token over it is 413. On ``fbb129525`` the line was 45,600, so the
        token over it was admitted, and a band below 45,600 answered busy."""
        line = index_slot.index_max_tokens(600 - 30 - 2)

        async def counts(_chunks, **_kwargs):
            clock.now += 1.9
            return line + over

        world.tokens.side_effect = counts

        if over:
            with pytest.raises(IndexAdmissionRefused) as refused:
                await _upload(_knowledge(session_factory))
            assert refused.value.status_code == 413
            assert f"at most {line:,} tokens" in refused.value.message
        else:
            await _upload(_knowledge(session_factory))
            assert len(world.encodes.calls) == 1

    async def test_the_token_counts_time_is_spent_from_the_window(
        self, session_factory, world, window, clock
    ):
        """#2851 review: a count that waits ``w`` seconds for a slot leaves the
        document ``w`` less: 401.8 s of work fits 600 s, not 600 - 200. On
        ``7c5318825`` the count's time was not charged, and it was admitted."""

        async def waits_then_counts(_chunks, **_kwargs):
            clock.now += 200.0
            return RUNBOOK_TOKENS

        world.tokens.side_effect = waits_then_counts

        with pytest.raises(IndexAdmissionRefused) as refused:
            await _upload(_knowledge(session_factory))

        assert (refused.value.status_code, refused.value.reason) == (503, "busy")
        assert world.encodes.calls == []

    async def test_a_document_inside_the_window_indexes_on_the_slot(
        self, session_factory, world, window
    ):
        service = _knowledge(session_factory)

        result = await _upload(service)

        assert (await _counts(session_factory))["knowledge_items"] == 1
        assert result["status"] == "completed"
        assert [thread for _, thread in world.encodes.calls] == ["kb-index_0"]
        service._vector_store.add_documents.assert_awaited_once()
        assert index_admission.queued() == 0

    @pytest.mark.parametrize("refusal", ["busy", "too_large"])
    def test_the_route_answers_the_status_code_and_retry_after(
        self, world, window, refusal, request
    ):
        if refusal == "busy":
            request.getfixturevalue("busy")
        else:
            world.tokens.return_value = TOO_LARGE

        response = _client(knowledge=_knowledge(_no_database())).post(
            "/api/v1/knowledge/documents",
            data={"title": "T", "document_type": "runbook", "scope": "personal"},
            files={"file": ("rb.md", valid_runbook().encode(), "text/markdown")},
        )

        if refusal == "busy":
            assert response.status_code == 503, response.text
            assert response.headers["x-error-code"] == "KNOWLEDGE_INDEXING_BUSY"
            assert response.headers["Retry-After"] == "402"
            assert "retry in about 402 seconds" in response.json()["detail"]
        else:
            assert response.status_code == 413, response.text
            assert response.headers["x-error-code"] == "KNOWLEDGE_DOCUMENT_TOO_LARGE"
            assert "Retry-After" not in response.headers
            assert "Split it" in response.json()["detail"]

    def test_the_route_answers_a_stuck_encode_with_a_fixed_wait(
        self, world, window, clock
    ):
        stuck = index_admission.admit_forced(["stuck"], 0, principal="u9")
        stuck.guard_seconds = 10.0
        stuck._started()
        clock.now = 11.0
        try:
            response = _upload_route()
        finally:
            stuck.close()

        assert response.status_code == 503, response.text
        assert response.headers["x-error-code"] == "KNOWLEDGE_INDEXING_BUSY"
        assert response.headers["Retry-After"] == "60"
        assert response.json()["detail"] == index_slot.refusal("hung", wait=60).message


# =============================================================================
# PUT
# =============================================================================


class TestUpdate:
    async def test_content_over_the_draft_bound_is_422_before_any_embed(
        self, session_factory, world
    ):
        service = _knowledge(session_factory)
        await _published(service)
        before = await _row(session_factory)
        world.encodes.calls.clear()

        with pytest.raises(ValidationException):
            await service.update_document_metadata(
                ITEM, principal="user-1", content="x" * (DRAFT_CONTENT_MAX_CHARS + 1)
            )

        after = await _row(session_factory)
        assert (after.content, after.vector_generation) == (
            before.content,
            before.vector_generation,
        )
        assert world.encodes.calls == []
        service._vector_store.add_documents.assert_awaited_once()  # the publish

    @pytest.mark.parametrize("refusal", ["busy", "too_large"])
    async def test_a_refusal_leaves_the_document_and_its_vectors_unchanged(
        self, session_factory, world, refusal, request
    ):
        service = _knowledge(session_factory)
        await _published(service)
        before = await _row(session_factory)
        request.getfixturevalue("window")
        if refusal == "busy":
            request.getfixturevalue("busy")
        else:
            world.tokens.return_value = TOO_LARGE

        with pytest.raises(IndexAdmissionRefused) as refused:
            await service.update_document_metadata(
                ITEM, principal="user-1", content=valid_runbook() + "\nAnd uncordon.\n"
            )

        assert refused.value.status_code == (503 if refusal == "busy" else 413)
        after = await _row(session_factory)
        assert (after.content, after.vector_generation) == (
            before.content,
            before.vector_generation,
        )
        service._vector_store.add_documents.assert_awaited_once()
        service._vector_store.delete_generation.assert_not_awaited()

    def test_a_validation_exception_is_422_not_500(self, monkeypatch):
        service = SimpleNamespace(
            get_document=AsyncMock(return_value={"document_id": ITEM}),
            update_document_metadata=AsyncMock(
                side_effect=ValidationException("Draft content is too long")
            ),
        )
        monkeypatch.setattr(routes, "_authorize_document_write", AsyncMock())

        response = _client(knowledge=service).put(
            f"/api/v1/knowledge/documents/{ITEM}", json={"content": "x"}
        )

        assert response.status_code == 422, response.text
        assert "too long" in response.json()["detail"]


# =============================================================================
# Verify
# =============================================================================


async def _drafts(session_factory, n: int) -> list[tuple[str, str]]:
    """``n`` jobs, each with a draft that passed validation."""
    refs = []
    async with session_factory() as session:
        for i in range(n):
            session.add(
                UploadedFileModel(
                    file_id=f"file_{i}",
                    enterprise_id=STANDALONE_ENTERPRISE_ID,
                    filename="f",
                    size_bytes=1,
                )
            )
            await session.flush()
            session.add(
                ConversionJobModel(
                    id=f"conv_{i}",
                    user_id="u1",
                    enterprise_id=STANDALONE_ENTERPRISE_ID,
                    scope="personal",
                    status="completed",
                    source_file_id=f"file_{i}",
                    source_type="document",
                )
            )
            session.add(
                ConversionDraftModel(
                    id=f"draft_{i}",
                    enterprise_id=STANDALONE_ENTERPRISE_ID,
                    conversion_id=f"conv_{i}",
                    runbook_id=f"rb-{i}",
                    title=f"Redis Evictions {i}",
                    content=valid_runbook(f"Redis Evictions Under Pressure {i}"),
                    status="draft",
                    validation_passed=True,
                )
            )
            refs.append((f"conv_{i}", f"draft_{i}"))
        await session.commit()
    return refs


async def _statuses(session_factory, refs) -> list[str]:
    async with session_factory() as session:
        return [
            (await session.get(ConversionDraftModel, draft_id)).status
            for _, draft_id in refs
        ]


class TestVerify:
    @pytest.mark.parametrize("refusal", ["busy", "too_large"])
    async def test_a_single_verify_refusal_is_answered_and_the_draft_stays(
        self, session_factory, world, window, refusal, request
    ):
        knowledge = _knowledge(session_factory)
        conversion = _conversion(session_factory, knowledge)
        [(conversion_id, draft_id)] = await _drafts(session_factory, 1)
        if refusal == "busy":
            request.getfixturevalue("busy")
        else:
            world.tokens.return_value = TOO_LARGE

        transport = httpx.ASGITransport(app=_app(conversion=conversion))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            response = await c.post(
                f"/api/v1/knowledge/conversions/{conversion_id}/drafts/"
                f"{draft_id}/verify"
            )

        assert response.status_code == (503 if refusal == "busy" else 413)
        assert response.headers["x-error-code"] == (
            "KNOWLEDGE_INDEXING_BUSY"
            if refusal == "busy"
            else "KNOWLEDGE_DOCUMENT_TOO_LARGE"
        )
        assert await _statuses(session_factory, [(conversion_id, draft_id)]) == [
            "draft"
        ]
        assert (await _counts(session_factory))["knowledge_items"] == 0

    async def test_the_batch_defers_what_the_rest_of_its_window_cannot_fit(
        self, session_factory, world, window, clock
    ):
        """Each encode takes its estimate on the slot's clock: the first spends
        372 of the 600 s, and the second, with the load, no longer fits."""
        world.encodes._seconds = 372.0
        knowledge = _knowledge(session_factory)
        conversion = _conversion(session_factory, knowledge)
        refs = await _drafts(session_factory, 3)

        result = await conversion.verify_batch(refs, user_id="u1")

        assert [r["status"] for r in result["results"]] == [
            "verified",
            "deferred",
            "deferred",
        ]
        assert (result["verified"], result["deferred"], result["failed"]) == (1, 2, 0)
        assert "Verify it again" in result["results"][1]["error"]
        assert await _statuses(session_factory, refs) == ["verified", "draft", "draft"]

    async def test_a_spent_window_defers_without_admitting(
        self, session_factory, world, window, clock
    ):
        world.encodes._seconds = 600.0
        conversion = _conversion(session_factory, _knowledge(session_factory))
        refs = await _drafts(session_factory, 2)

        result = await conversion.verify_batch(refs, user_id="u1")

        assert [r["status"] for r in result["results"]] == ["verified", "deferred"]
        assert world.tokens.await_count == 1

    async def test_a_document_too_large_for_any_window_fails_and_the_batch_goes_on(
        self, session_factory, world, window
    ):
        world.tokens.side_effect = [TOO_LARGE, RUNBOOK_TOKENS]
        conversion = _conversion(session_factory, _knowledge(session_factory))
        refs = await _drafts(session_factory, 2)

        result = await conversion.verify_batch(refs, user_id="u1")

        assert [r["status"] for r in result["results"]] == ["failed", "verified"]
        assert "Split it" in result["results"][0]["error"]
        assert (result["failed"], result["verified"], result["deferred"]) == (1, 1, 0)

    async def test_a_window_of_zero_defers_nothing(self, session_factory, world, clock):
        world.encodes._seconds = 10_000.0
        conversion = _conversion(session_factory, _knowledge(session_factory))
        refs = await _drafts(session_factory, 3)

        result = await conversion.verify_batch(refs, user_id="u1")

        assert result["verified"] == 3 and result["deferred"] == 0


# =============================================================================
# Bulk update
# =============================================================================


class TestBulkUpdate:
    async def test_content_is_refused_and_nothing_is_updated(
        self, session_factory, world
    ):
        service = _knowledge(session_factory)
        await _published(service)

        with pytest.raises(ValidationException, match="metadata only"):
            await service.bulk_update_documents(
                [ITEM],
                {"content": valid_runbook() + "\nx\n", "tags": ["new"]},
                principal="user-1",
            )

        row = await _row(session_factory)
        assert (row.content, list(row.tags or [])) == (valid_runbook(), [])

    async def test_a_metadata_only_bulk_update_is_unchanged(
        self, session_factory, world
    ):
        service = _knowledge(session_factory)
        await _published(service)

        result = await service.bulk_update_documents(
            [ITEM], {"tags": ["new"]}, principal="user-1"
        )

        assert result["updated_count"] == 1 and result["errors"] == []
        assert list((await _row(session_factory)).tags) == ["new"]

    def test_the_route_answers_422(self, monkeypatch):
        monkeypatch.setattr(
            routes, "_partition_bulk_targets", AsyncMock(return_value=([ITEM], []))
        )

        response = _client(knowledge=_knowledge(_no_database())).post(
            "/api/v1/knowledge/documents/bulk-update",
            json={"document_ids": [ITEM], "updates": {"content": "x"}},
        )

        assert response.status_code == 422, response.text
        assert "PUT /knowledge/documents/{id}" in response.json()["detail"]


# =============================================================================
# The boot repair
# =============================================================================


async def test_the_boot_repair_encodes_on_the_slot_after_an_upload(
    session_factory, world
):
    service = _knowledge(session_factory)
    await _published(service)
    world.encodes.calls.clear()
    world.encodes.held = 1
    world.encodes.release.clear()
    world.encodes.entered.clear()

    upload = asyncio.create_task(_upload(service))
    await asyncio.to_thread(world.encodes.entered.wait, 30)
    repair = asyncio.create_task(service.reindex_missing_vectors([ITEM]))
    while sum(t._future is not None for t in index_admission._tickets) < 2:
        await asyncio.sleep(0.01)

    assert len(world.encodes.calls) == 1, "the repair encoded beside the upload"
    world.encodes.release.set()
    await upload
    assert (await repair)[ITEM] > 0

    assert [thread for _, thread in world.encodes.calls] == ["kb-index_0"] * 2
    assert world.encodes.most_active == 1


# =============================================================================
# An embed that failed (#2461)
# =============================================================================

#: The real KB encode, before ``world`` replaces it with a double.
_REAL_ENCODE_IN_WORKER = index_slot.encode_in_worker

#: Each cause: its code and the text of its raise site's own message, which
#: no 503 body may carry.
CAUSES = {
    "model_unavailable_count": ("KNOWLEDGE_EMBEDDER_UNAVAILABLE", ["retry shortly"]),
    "model_unavailable": ("KNOWLEDGE_EMBEDDER_UNAVAILABLE", ["could not be loaded"]),
    "worker_failed": ("KNOWLEDGE_EMBEDDER_UNAVAILABLE", ["ended without vectors"]),
    "encode_timeout": ("KNOWLEDGE_EMBEDDER_TIMEOUT", ["chunks timed out"]),
    "queued_too_long": (
        "KNOWLEDGE_EMBEDDER_TIMEOUT",
        ["for other indexing on this server"],
    ),
}


@pytest.fixture
async def embed_fails(monkeypatch, world, request):
    """The KB encode ends the way ``request.param`` names, through its real
    raise site: the token count (a window, no count), the real worker step
    over a ``worker_process.run`` that ends as named, or a held encode ahead
    with a bound shorter than the wait."""
    from faultmaven.infrastructure import encode_worker, worker_process

    cause = request.param
    held = None
    if cause == "model_unavailable_count":
        monkeypatch.setattr(
            get_settings().knowledge, "index_request_window_seconds", 600
        )
        world.tokens.return_value = None
    elif cause == "queued_too_long":
        world.tokens.return_value = 0
        world.encodes.held = 1
        held = index_admission.admit_forced(["other"], 0, principal="u9")
        future, _ = held.run(["other"], guard_seconds=0.0)
        await asyncio.to_thread(world.encodes.entered.wait, 30)
        monkeypatch.setattr(index_slot, "EMBED_BATCH_LOAD_SECONDS", 0.05)
    else:
        ends = {
            "model_unavailable": dict(code=1, failure=encode_worker.MODEL_NOT_LOADED),
            "worker_failed": dict(code=1, failure="an encode that failed"),
            "encode_timeout": dict(timed_out=True),
        }

        real_run = worker_process.run

        async def run(module, *args, **kwargs):
            if module != index_slot._ENCODE_WORKER:
                return await real_run(module, *args, **kwargs)  # the gate's
            return worker_process.WorkerEnd(phase="work", **ends[cause])

        monkeypatch.setattr(index_slot, "encode_in_worker", _REAL_ENCODE_IN_WORKER)
        monkeypatch.setattr(worker_process, "run", run)
    try:
        yield cause
    finally:
        world.encodes.release.set()
        if held is not None:
            await asyncio.wrap_future(future)


#: The owner's sentence for each cause (#2461), verbatim.
SENTENCES = {
    "model_unavailable": (
        "Indexing unavailable: the search model is not ready on this server, so "
        "this document was not added. Retry in a few minutes."
    ),
    "worker_failed": (
        "Indexing failed: the server could not prepare this document for search, "
        "so it was not added. If it fails again, the document may be too large; "
        "try splitting it."
    ),
    "queued_too_long": (
        "Indexing busy: this document waited too long behind other indexing on "
        "this server and was not added. Retry in a few minutes."
    ),
    "encode_timeout": (
        "Indexing timed out: preparing this document for search took too long, "
        "so it was not added. Retry later; a very large document may need to be "
        "split."
    ),
}


def _assert_embed_failure(response, cause: str) -> None:
    code, own_text = CAUSES[cause]
    assert response.status_code == 503, response.text
    assert response.headers["x-error-code"] == code
    assert "Retry-After" not in response.headers
    detail = response.json()["detail"]
    assert detail == SENTENCES[cause.removesuffix("_count")]
    for text in [*own_text, "Indexing document", ITEM]:
        assert text not in response.text


def _async_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


class TestEmbedFailure:
    """On ``main`` upload and single verify answered these 500."""

    @pytest.mark.parametrize("embed_fails", list(CAUSES), indirect=True)
    async def test_an_upload_answers_503_with_the_causes_sentence(
        self, session_factory, embed_fails
    ):
        app = _app(knowledge=_knowledge(session_factory))
        async with _async_client(app) as client:
            response = await client.post(
                "/api/v1/knowledge/documents",
                data={"title": "T", "document_type": "runbook", "scope": "personal"},
                files={"file": ("rb.md", valid_runbook().encode(), "text/markdown")},
            )

        _assert_embed_failure(response, embed_fails)
        assert (await _counts(session_factory))["knowledge_items"] == 0

    @pytest.mark.parametrize("embed_fails", list(CAUSES), indirect=True)
    async def test_a_single_verify_answers_503_and_the_draft_stays(
        self, session_factory, embed_fails
    ):
        knowledge = _knowledge(session_factory)
        conversion = _conversion(session_factory, knowledge)
        refs = await _drafts(session_factory, 1)
        [(conversion_id, draft_id)] = refs

        async with _async_client(_app(conversion=conversion)) as client:
            response = await client.post(
                f"/api/v1/knowledge/conversions/{conversion_id}/drafts/"
                f"{draft_id}/verify"
            )

        _assert_embed_failure(response, embed_fails)
        assert await _statuses(session_factory, refs) == ["draft"]
        assert (await _counts(session_factory))["knowledge_items"] == 0

    @pytest.mark.parametrize("embed_fails", ["worker_failed"], indirect=True)
    async def test_put_keeps_its_503_and_detail_and_adds_the_code(
        self, session_factory, world, monkeypatch, embed_fails
    ):
        service = _knowledge(session_factory)
        with monkeypatch.context() as published:
            published.setattr(index_slot, "encode_in_worker", world.encodes)
            await _published(service)
        monkeypatch.setattr(routes, "_authorize_document_write", AsyncMock())

        async with _async_client(_app(knowledge=service)) as client:
            response = await client.put(
                f"/api/v1/knowledge/documents/{ITEM}",
                json={"content": valid_runbook() + "\nAnd uncordon.\n"},
            )

        assert response.status_code == 503, response.text
        assert response.headers["x-error-code"] == "KNOWLEDGE_EMBEDDER_UNAVAILABLE"
        assert response.json()["detail"].startswith("Your edit was not saved")

    @pytest.mark.parametrize("embed_fails", ["worker_failed"], indirect=True)
    async def test_verify_batch_records_the_class(self, session_factory, embed_fails):
        conversion = _conversion(session_factory, _knowledge(session_factory))
        refs = await _drafts(session_factory, 1)

        result = await conversion.verify_batch(refs, user_id="u1")

        [item] = result["results"]
        assert (item["status"], item["error"]) == (
            "failed",
            "Verification failed (EmbedderUnavailable)",
        )
