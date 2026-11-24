"""A case delete erases the answers the replay cache holds for it (#2624):
the copilot keys case creation, and a create retried after its case was
deleted was answered with the deleted case. A create overlapping its keyed
first attempt creates nothing (#2655), so the key is indexed under one case.

Built as ``test_kb_request_receipts_2433.py`` builds its app, since the
deployed app mounts no middleware under ``SKIP_SERVICE_CHECKS``: the real case
router and the ``CaseService`` ``create_case_service`` composes for the profile
that mounts it, with ONE FakeRedis held by the middleware and the erasers, as
the composition root's shared client is.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import chromadb
import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.middleware import idempotency
from faultmaven.api.middleware.idempotency import (
    KEY_PREFIX,
    IdempotencyMiddleware,
    case_index_key,
)
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.idempotency_key import IDEMPOTENCY_REPLAYED_HEADER
from faultmaven.config.settings import get_settings
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.container.providers.services import create_case_service
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.storage.filesystem import FilesystemStorageBackend
from faultmaven.modules.auth.contracts import UserDTO
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
    _di_get_session_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from faultmaven.modules.case.contracts import Case, CaseState
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)
from faultmaven.modules.knowledge.domain.services.conversion_service.case_erasure import (
    RunbookConversionEraser,
)
from tests.chroma_clients import closed_at_teardown

pytestmark = [pytest.mark.integration]

OWNER = "u_2624"
KEY, OTHER_KEY = "create-2624-aaaaaaaa", "create-2624-bbbbbbbb"
AUTHORIZATION = {"Authorization": "Bearer token-2624-aaaaaaaa"}


@pytest.fixture(autouse=True)
def _tenant():
    set_current_enterprise_id("ent_2624")
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


@pytest.fixture
async def world(tmp_path, monkeypatch):
    # The conversion eraser's SQL is test_case_deletion_repository_1016's.
    monkeypatch.setattr(
        RunbookConversionEraser, "erase_case", AsyncMock(return_value=0)
    )
    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    settings = get_settings().model_copy(deep=True)
    settings.server.skip_service_checks = False
    chroma = closed_at_teardown(chromadb.PersistentClient(path=str(tmp_path / "c")))
    service = create_case_service(
        InMemoryCaseRepository(),
        RedisSessionStore(fake, session_ttl_seconds=1800),
        CaseVectorStore(chroma),
        settings,
        file_storage_service=FileStorageService(
            backend=FilesystemStorageBackend(str(tmp_path / "blobs"))
        ),
        redis_client=fake,
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    owner = UserDTO(OWNER, OWNER, f"{OWNER}@example.com", OWNER, is_active=True)
    app.dependency_overrides[require_authentication] = lambda: owner
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: service
    app.dependency_overrides[_di_get_session_service_dependency] = lambda: None
    app.add_middleware(IdempotencyMiddleware, redis_client=fake)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield SimpleNamespace(client=client, service=service, fake=fake)


def _create(world, title="Disk full", key=KEY):
    headers = {**AUTHORIZATION, "Idempotency-Key": key}
    return world.client.post("/api/v1/cases", headers=headers, json={"title": title})


def _replayed(response) -> bool:
    return response.headers.get(IDEMPOTENCY_REPLAYED_HEADER) == "true"


async def test_a_deleted_cases_keyed_create_runs_again(world):
    first, replay = await _create(world), await _create(world)
    case_id = first.json()["case_id"]
    assert first.status_code == replay.status_code == 201
    assert _replayed(replay) and replay.json()["case_id"] == case_id

    assert (await world.client.delete(f"/api/v1/cases/{case_id}")).status_code == 204
    again = await _create(world)

    assert again.status_code == 201
    assert not _replayed(again)
    assert again.json()["case_id"] != case_id


async def test_an_overlapping_keyed_create_creates_one_case(world, monkeypatch):
    """The retry arrives while the first create runs: it waits, and is
    answered with the first's case. Before #2655 both ran: two cases, the
    key indexed under each."""
    monkeypatch.setattr(idempotency, "IDEMPOTENCY_POLL_SECONDS", 0, raising=False)
    create, created = world.service.create_case, []
    go, entered, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def held_create(*args, **kwargs):
        created.append(None)
        if len(created) == 1:
            entered.set()
            await go.wait()
        return await create(*args, **kwargs)

    pttl = world.fake.pttl

    async def watched_pttl(name):
        waiting.set()  # the retry found the first's claim held
        return await pttl(name)

    monkeypatch.setattr(world.service, "create_case", held_create)
    monkeypatch.setattr(world.fake, "pttl", watched_pttl)
    first = asyncio.ensure_future(_create(world))
    await asyncio.wait_for(entered.wait(), 30)
    retry = asyncio.ensure_future(_create(world))
    # Before the fix the retry runs a second create instead of waiting.
    signals = {
        asyncio.ensure_future(waiting.wait()),
        asyncio.ensure_future(_until(lambda: len(created) > 1)),
    }
    _, pending = await asyncio.wait(signals, timeout=30, return_when="FIRST_COMPLETED")
    for signal in pending:
        signal.cancel()
    go.set()
    first, retry = await asyncio.wait_for(asyncio.gather(first, retry), 30)

    assert first.status_code == retry.status_code == 201
    case_id = first.json()["case_id"]
    assert len(created) == 1
    assert _replayed(retry) and retry.json()["case_id"] == case_id
    (index,) = await world.fake.keys(f"{KEY_PREFIX}case:*")
    assert index == case_index_key(KEY_PREFIX, case_id)
    assert len(await world.fake.zrange(index, 0, -1)) == 1


async def _until(condition):
    while not condition():
        await asyncio.sleep(0)


async def test_another_cases_create_naming_it_keeps_replaying(world):
    """The create of Y answers Y, though its title names X."""
    x = (await _create(world)).json()["case_id"]
    y = (await _create(world, title=f"dup of {x}", key=OTHER_KEY)).json()["case_id"]

    assert (await world.client.delete(f"/api/v1/cases/{x}")).status_code == 204
    retry = await _create(world, title=f"dup of {x}", key=OTHER_KEY)

    assert _replayed(retry) and retry.json()["case_id"] == y


async def test_a_keyed_post_on_the_cases_path_is_erased_whatever_it_answers(world):
    """Its path declares the case; the answer names it only after a Unicode
    letter, which the regex scan missed and nothing depends on now."""
    now, case_id = datetime.now(timezone.utc), f"case_{uuid4().hex[:12]}"
    case = await world.service.repository.save(
        Case(
            case_id=case_id,
            user_id=OWNER,
            enterprise_id="ent_2624",
            title=f"案件{case_id}",
            state=CaseState.CLOSED,
            created_at=now - timedelta(minutes=1),
            closed_at=now,
            closure_reason="closed_insufficient_evidence",
        )
    )
    title = f"/api/v1/cases/{case.case_id}/title"
    headers = {**AUTHORIZATION, "Idempotency-Key": KEY}
    first = await world.client.post(title, headers=headers)
    assert first.status_code == 200 and first.json()["title"] == f"案件{case_id}"
    assert _replayed(await world.client.post(title, headers=headers))

    assert (await world.client.delete(title.removesuffix("/title"))).status_code == 204
    after = await world.client.post(title, headers=headers)

    assert after.status_code == 404 and not _replayed(after)
