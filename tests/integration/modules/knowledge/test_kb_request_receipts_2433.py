"""A keyed KB upload or conversion publishes at most once per key (#2433).

``POST /knowledge/documents`` and ``POST /knowledge/convert`` mint their ids on
the server, and the idempotency middleware that answered a keyed retry cached
only a built 2xx, held no in-flight claim and never fingerprinted a multipart
body. So a retry after a lost response or ``KB_INGEST_OUTCOME_UNKNOWN``, or
while the first still ran, published a duplicate runbook or a second set of
drafts. Now each keyed request commits a receipt in the transaction that
publishes it, and the route answers a retry from it.

Driven through the mounted routers and the real ``KnowledgeService`` and
``ConversionService`` on FK-on SQLite (the #2365 harness), the model calls
stubbed, the claim on FakeRedis, requests on one event loop
(``httpx.ASGITransport``, as the turn receipt's suite) so two can overlap.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.api.middleware.route_policy import APP_STATE_POLICY_ATTR
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.idempotency_key import (
    IDEMPOTENCY_KEY_REUSE,
    IDEMPOTENCY_REPLAY_UNAVAILABLE,
    IDEMPOTENCY_REPLAYED_HEADER,
)
from faultmaven.config.tenant_context import _current_enterprise_id
from faultmaven.infrastructure.persistence.models import (
    ConversionJobModel,
    EnterpriseModel,
    KbRequestReceiptModel,
    KnowledgeItemModel,
    ResourceShareModel,
)
from faultmaven.infrastructure.persistence.share_repository import (
    PostgreSQLShareRepository,
)
from faultmaven.modules.knowledge.api import conversion_routes
from faultmaven.modules.knowledge.api.request_idempotency import (
    KB_REQUEST_IN_PROGRESS,
)
from faultmaven.modules.knowledge.api.routes import get_knowledge_service
from faultmaven.modules.knowledge.api.routes import router as knowledge_router
from faultmaven.modules.knowledge.domain.models.conversion import (
    AnalysisResult,
    ConversionStatus,
    PreprocessingResult,
    SourceAssessment,
    SourceFileInfo,
)
from faultmaven.modules.knowledge.domain.request_receipts import (
    ROUTE_UPLOAD,
    KbReceiptKey,
)
from faultmaven.modules.knowledge.domain.services.conversion_service import (
    service as conversion_module,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.job_persistence import (
    _persist_job,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.service import (
    ConversionService,
)
from faultmaven.modules.knowledge.exceptions import (
    KB_INGEST_OUTCOME_UNKNOWN,
    UPLOAD_OUTCOME_UNKNOWN_DETAIL,
    KbReceiptExistsError,
)
from faultmaven.modules.knowledge.infrastructure.persistence.knowledge_item_repository import (
    DatabaseKnowledgeItemRepository,
)
from tests.integration.modules.knowledge.test_conversion_read_back import (
    RUNBOOK,
    _mode,
)
from tests.integration.modules.knowledge.test_upload_writes_rows_after_ingest_2365 import (  # noqa: F401
    OWNER,
    TEAM_ID,
    _FailingCommit,
    _knowledge,
    _Shares,
    session_factory,
)
from tests.runbook_samples import valid_runbook
from tests.utils import regex_only_sanitizer, seed_enterprises, seed_users

pytestmark = [pytest.mark.integration, pytest.mark.knowledge_base]

KEY = "kb-2433-0001"
UPLOAD, CONVERT = "/api/v1/knowledge/documents", "/api/v1/knowledge/convert"
RUNBOOK_BYTES = valid_runbook("Redis Evictions Under Memory Pressure").encode()
SOURCE_BYTES = b"Redis evicts keys under memory pressure."
ANALYSIS = AnalysisResult(
    is_actionable=True,
    failure_modes=[_mode("fm-0", "Redis OOM", "saturation")],
    source_assessment=SourceAssessment(
        content_type="doc", actionability_rating="high", missing_information=[]
    ),
)


def _conversion(factory) -> ConversionService:
    settings = MagicMock()
    settings.llm.get_knowledge_model.return_value = "test-model"
    settings.llm.explicit_role_provider.return_value = None
    service = ConversionService(
        sanitizer=regex_only_sanitizer(),
        llm_router=MagicMock(),
        settings=settings,
        db_session_factory=factory,
    )
    service._preprocessor.preprocess = AsyncMock(
        return_value=PreprocessingResult(
            extracted_text=SOURCE_BYTES.decode(), source_metadata={}
        )
    )
    service._llm_router.route = AsyncMock(
        return_value=SimpleNamespace(content=RUNBOOK, is_truncated=False)
    )
    return service


@dataclass
class World:
    client: httpx.AsyncClient
    factory: Any
    app: FastAPI
    knowledge: Any
    conversion: Any
    analyze: AsyncMock

    async def post(
        self,
        route: str,
        *,
        key=KEY,
        content=None,
        title="Redis Evictions",
        content_type="text/markdown",
    ):
        headers = {"Idempotency-Key": key} if key else {}
        if route == "upload":
            return await self.client.post(
                UPLOAD,
                headers=headers,
                data={"title": title, "document_type": "runbook", "scope": "personal"},
                files={"file": ("rb.md", content or RUNBOOK_BYTES, "text/markdown")},
            )
        return await self.client.post(
            CONVERT,
            headers=headers,
            data={"scope": "personal"},
            files={"file": ("doc.md", content or SOURCE_BYTES, content_type)},
        )

    async def _scalar(self, statement):
        async with self.factory() as session:
            return (await session.execute(statement)).scalar_one()

    async def published(self, route: str) -> int:
        """Complete runbooks (an upload) or conversion jobs (a conversion)."""
        if route == "upload":
            return await self._scalar(
                select(func.count()).where(KnowledgeItemModel.ingest_complete)
            )
        return await self._scalar(select(func.count()).select_from(ConversionJobModel))

    async def receipts(self) -> list:
        async with self.factory() as session:
            rows = await session.execute(select(KbRequestReceiptModel.route))
            return sorted(rows.scalars())

    def service(self, route: str):
        return self.knowledge if route == "upload" else self.conversion


@pytest.fixture
async def world(session_factory, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    analyze = AsyncMock(return_value=ANALYSIS)
    monkeypatch.setattr(conversion_module, "_analyze_document", analyze)
    app = FastAPI()
    app.include_router(knowledge_router, prefix="/api/v1")
    app.include_router(conversion_routes.router, prefix="/api/v1")
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.state.team_service = None
    app.state.redis_client = fakeredis_aio.FakeRedis(decode_responses=True)
    app.dependency_overrides[require_authentication] = lambda: SimpleNamespace(
        user_id=OWNER,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        organization_id=None,
        is_platform_admin=lambda: False,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        w = World(
            client=client,
            factory=session_factory,
            app=app,
            knowledge=_knowledge(session_factory),
            conversion=_conversion(session_factory),
            analyze=analyze,
        )
        app.dependency_overrides[get_knowledge_service] = lambda: w.knowledge
        app.dependency_overrides[conversion_routes._get_conversion_service] = (
            lambda: w.conversion
        )
        yield w


ROUTES = pytest.mark.parametrize("route", ["upload", "convert"])


@ROUTES
async def test_a_retry_after_a_lost_response_replays_it(world, route):
    first = await world.post(route)
    again = await world.post(route)

    assert first.status_code == 201, first.text
    assert IDEMPOTENCY_REPLAYED_HEADER not in first.headers
    assert again.status_code == 201, again.text
    assert again.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
    assert again.content == first.content
    assert again.headers.get("location") == first.headers.get("location")
    assert await world.published(route) == 1
    assert await world.receipts() == [route]


@pytest.mark.parametrize(
    "route, change",
    [
        ("upload", {"content": valid_runbook("Another Runbook Entirely").encode()}),
        ("upload", {"title": "Another title"}),
        ("convert", {"content": b"Another source document."}),
        # The parser reads the declared type ahead of the extension.
        ("convert", {"content_type": "text/html"}),
    ],
    ids=["upload-file", "upload-field", "convert-file", "convert-content-type"],
)
async def test_the_key_with_another_file_or_field_is_refused(world, route, change):
    assert (await world.post(route)).status_code == 201

    reused = await world.post(route, **change)

    assert reused.status_code == 409, reused.text
    assert reused.headers["x-error-code"] == IDEMPOTENCY_KEY_REUSE
    assert await world.published(route) == 1


@ROUTES
async def test_a_duplicate_while_the_first_runs_is_told_it_is_in_progress(world, route):
    parked, go = asyncio.Event(), asyncio.Event()

    async def park(*_args, **_kwargs):
        parked.set()
        await go.wait()
        return 3 if route == "upload" else ANALYSIS

    if route == "upload":
        world.knowledge._index_document_in_vector_store = AsyncMock(side_effect=park)
    else:
        world.analyze.side_effect = park
    first = asyncio.create_task(world.post(route))
    await asyncio.wait_for(parked.wait(), 10)

    try:  # bounded: a duplicate that runs would wait on the first forever
        duplicate = await asyncio.wait_for(world.post(route), 10)
    finally:
        go.set()
    first_response = await asyncio.wait_for(first, 30)
    retry = await world.post(route)

    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.headers["x-error-code"] == KB_REQUEST_IN_PROGRESS
    assert 1 <= int(duplicate.headers["retry-after"]) <= 60
    assert first_response.status_code == 201, first_response.text
    assert retry.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
    assert await world.published(route) == 1


@ROUTES
async def test_a_duplicate_that_ran_without_a_claim_is_answered_with_the_first(
    world, route
):
    """No claim store: a duplicate whose lookup missed (made before the first
    committed) runs, and its receipt INSERT is refused in its own transaction.
    It answers with the first's 201, and nothing of it is left: the upload's
    incomplete row is deleted, the conversion's job never written."""
    world.app.state.redis_client = None
    first = await world.post(route)
    service = world.service(route)
    real, calls = service.get_request_receipt, []

    async def lookup(key):
        calls.append(key)
        return None if len(calls) == 1 else await real(key)

    service.get_request_receipt = lookup

    duplicate = await world.post(route)

    assert first.status_code == 201, first.text
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.headers[IDEMPOTENCY_REPLAYED_HEADER] == "true"
    assert duplicate.content == first.content
    assert len(calls) == 2, "the duplicate read the receipt back"
    assert await world.published(route) == 1
    rows = await world._scalar(select(func.count()).select_from(KnowledgeItemModel))
    assert rows == (1 if route == "upload" else 0)
    assert world.analyze.await_count == (2 if route == "convert" else 0)


@pytest.mark.parametrize("claimless", [False, True], ids=["retry", "claimless"])
async def test_a_stored_conversion_that_no_longer_validates_is_not_replayed(
    world, claimless
):
    """A receipt lives as long as its job, across deploys: a stored body that
    no longer validates as a ``ConversionResponse`` is a 409, on a retry and on
    a claimless duplicate's read-back alike, as the turn's replay is."""
    if claimless:
        world.app.state.redis_client = None
    assert (await world.post("convert")).status_code == 201
    async with world.factory() as session:
        await session.execute(
            update(KbRequestReceiptModel).values(response={"status": "completed"})
        )
        await session.commit()
    if claimless:
        real, calls = world.conversion.get_request_receipt, []

        async def lookup(key):
            calls.append(key)
            return None if len(calls) == 1 else await real(key)

        world.conversion.get_request_receipt = lookup

    again = await world.post("convert")

    assert again.status_code == 409, again.text
    assert again.headers["x-error-code"] == IDEMPOTENCY_REPLAY_UNAVAILABLE
    assert await world.published("convert") == 1


@pytest.mark.parametrize("landed", [True, False], ids=["landed", "refused"])
async def test_an_unknown_outcome_retried_under_its_key_replays_or_runs(
    world, session_factory, monkeypatch, landed
):
    """The completing COMMIT raised, and so did the cleanup that would have
    told whether it landed (#2396). The 500 tells a keyed client to retry with
    the key; the retry replays the upload if it landed and runs if not."""
    factory = _FailingCommit(session_factory, (RuntimeError("commit lost"), landed))
    world.knowledge = _knowledge(factory)
    factory.arm_at_completion(world.knowledge)
    monkeypatch.setattr(
        DatabaseKnowledgeItemRepository, "delete", AsyncMock(side_effect=OSError())
    )

    unknown = await world.post("upload")
    retry = await world.post("upload")

    assert unknown.status_code == 500, unknown.text
    assert unknown.headers["x-error-code"] == KB_INGEST_OUTCOME_UNKNOWN
    assert unknown.json()["detail"] == UPLOAD_OUTCOME_UNKNOWN_DETAIL
    assert retry.status_code == 201, retry.text
    assert (retry.headers.get(IDEMPOTENCY_REPLAYED_HEADER) == "true") is landed
    assert await world.published("upload") == 1
    assert await world.receipts() == ["upload"]


async def test_a_refused_upload_writes_no_receipt_and_its_retry_is_refused(world):
    refused = await world.post("upload", content=b"# Not a runbook\n")
    again = await world.post("upload", content=b"# Not a runbook\n")

    assert refused.status_code == 422, refused.text
    assert again.status_code == 422, "the claim outlived the refused request"
    assert await world.receipts() == []


@ROUTES
async def test_unkeyed_requests_publish_each_time(world, route):
    responses = [await world.post(route, key=None) for _ in range(2)]

    assert [r.status_code for r in responses] == [201, 201]
    assert not any(IDEMPOTENCY_REPLAYED_HEADER in r.headers for r in responses)
    assert await world.published(route) == 2
    assert await world.receipts() == []


async def test_a_deleted_runbook_takes_its_receipt_with_it(world):
    """The receipt lives as long as its subject: once the runbook is deleted,
    the key names nothing, and a retry under it publishes anew."""
    first = await world.post("upload")
    await world.knowledge.delete_document(first.json()["document_id"])

    assert await world.receipts() == []
    again = await world.post("upload")
    assert again.status_code == 201, again.text
    assert IDEMPOTENCY_REPLAYED_HEADER not in again.headers
    assert again.json()["document_id"] != first.json()["document_id"]


@pytest.mark.parametrize("share_fails", [False, True], ids=["commits", "fails"])
async def test_a_team_jobs_share_commits_in_the_jobs_transaction(
    session_factory, monkeypatch, share_fails
):
    """Before #2433 the share was written by a second call after the job's
    commit, so a share that failed left a team job without its share, which a
    receipt would then replay as a success."""
    if share_fails:
        monkeypatch.setattr(
            PostgreSQLShareRepository,
            "share",
            AsyncMock(side_effect=RuntimeError("share failed")),
        )
    persist = _persist_job(
        session_factory,
        _Shares(session_factory),
        conversion_id="conv_2433",
        user_id=OWNER,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        scope="team",
        team_id=TEAM_ID,
        status=ConversionStatus.FAILED,
        source_file=SourceFileInfo(filename="doc.md", size_bytes=1, content_type="x"),
        analysis=ANALYSIS,
        drafts=[],
        created_at=datetime.now(timezone.utc),
        sanitizer=regex_only_sanitizer(),
    )

    if share_fails:
        with pytest.raises(RuntimeError, match="share failed"):
            await persist
    else:
        await persist
    async with session_factory() as session:
        jobs = (await session.execute(select(ConversionJobModel.id))).scalars().all()
        shares = (
            await session.execute(
                select(ResourceShareModel.resource_id, ResourceShareModel.scope_id)
            )
        ).all()
    if share_fails:
        assert (jobs, shares) == ([], [])
    else:
        assert (jobs, shares) == (["conv_2433"], [("conv_2433", TEAM_ID)])


async def test_the_deployed_app_leaves_both_routes_to_their_receipts():
    """``main.py`` declares both routes ``never_replayed``: a keyed repeat
    reaches the route, never the middleware's cache. A control route under the
    same declaration map is replayed."""
    from faultmaven.main import app as deployed

    app, runs = FastAPI(), {UPLOAD: 0, CONVERT: 0, "/api/v1/other": 0}
    for path in runs:

        async def run(path=path):
            runs[path] += 1
            return {"run": runs[path]}

        app.add_api_route(path, run, methods=["POST"], status_code=201)
    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    app.state.redis_client = fake
    setattr(
        app.state, APP_STATE_POLICY_ATTR, getattr(deployed.state, APP_STATE_POLICY_ATTR)
    )
    app.add_middleware(IdempotencyMiddleware, redis_client=fake)
    headers = {"Idempotency-Key": KEY, "Authorization": "Bearer token-2433-aaaaaaaa"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for path in runs:
            for _ in range(2):
                assert (await client.post(path, headers=headers)).status_code == 201

    assert runs == {UPLOAD: 2, CONVERT: 2, "/api/v1/other": 1}


@pytest.mark.postgres
@pytest.mark.skipif(
    not os.environ.get("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL-only; set DATABASE_URL to a PG instance to run.",
)
async def test_a_duplicate_is_refused_at_the_key_on_postgresql():
    """PostgreSQL's key refuses the duplicate's receipt at its flush, and the
    refusal is classified once the session is back in the pool."""
    enterprise = f"ent_2433_{uuid4().hex[:8]}"
    engine = create_async_engine(os.environ["DATABASE_URL"])
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    token = _current_enterprise_id.set(enterprise)
    key = KbReceiptKey(enterprise, OWNER, ROUTE_UPLOAD, KEY, "f" * 64)
    service = _knowledge(factory)
    try:
        async with factory() as session:
            await seed_enterprises(session, [enterprise])
            await seed_users(session, [OWNER], enterprise_id=enterprise)
        upload = dict(
            content=RUNBOOK_BYTES.decode(),
            title="Redis Evictions",
            document_type="runbook",
            scope="personal",
            owner_id=OWNER,
            receipt_key=key,
        )

        first = await service.upload_document(**upload)
        with pytest.raises(KbReceiptExistsError):
            await service.upload_document(**upload)

        assert (await service.get_request_receipt(key)).response == first
        async with factory() as session:
            items = await session.execute(
                select(KnowledgeItemModel.item_id).where(
                    KnowledgeItemModel.enterprise_id == enterprise
                )
            )
            assert items.scalars().all() == [first["document_id"]]
    finally:
        async with factory() as session:
            await session.execute(
                delete(EnterpriseModel).where(
                    EnterpriseModel.enterprise_id == enterprise
                )
            )
            await session.commit()
        _current_enterprise_id.reset(token)
        await engine.dispose()
