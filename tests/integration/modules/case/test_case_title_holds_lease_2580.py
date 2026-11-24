"""#2580 — ``POST /cases/{id}/title`` holds the case's in-flight lease.

The title call loads the case's PII redaction registry, calls a model, saves
the registry back and writes the case row. The save writes back every loaded
entry the store no longer holds, so an erased registry comes back whole, and
re-arms the 7-day TTL. A delete erases the registry, so one that passed under a
running title call left the deleted case's original values in Redis. The call
now holds the lease a turn holds (#2096): a delete refuses while it runs, and
it refuses while a delete runs.

Driven through the MOUNTED case router with the app's exception handlers, the
REAL ``CaseService`` over the production ``SessionlessCaseRepository`` on a
SQLite file, the real registry eraser and delete gate over FakeRedis and a real
regex sanitizer, under each ``SANITIZE_PII`` profile pinned (the registry is
seeded under both; only with it on does the title call load and save it). Only
the model, and the conversation read (long enough
user chat that the title takes the model path), are stubbed. The bare app
mounts no protection middleware, so the ``title_generation`` rate limit (5 per
300 s) does not apply to the calls here.

A title call cancelled under asyncio releases its hold in the route's
``finally``. A cancel that reaches the release itself, for example a cancelled
anyio scope, which cancels every await in that ``finally`` too, leaves the
member to lapse on its TTL
(``inflight.LEASE_TTL_MS``), as a turn's does.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.exceptions import (
    CaseNotDriverError,
    NotFoundError,
    RawModelContentRefused,
)
from faultmaven.infrastructure.concurrency import inflight
from faultmaven.infrastructure.concurrency.inflight import CaseDeleteGate
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.security.case_redaction import (
    CaseRedactionContext,
    CaseRedactionRegistryEraser,
)
from faultmaven.modules.case.api.routes import cases as cases_module
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from faultmaven.modules.case.api.title_generation import (
    _TitleDisclosureGateUnavailable,
    _TitleSubstanceTooThin,
)
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure import (
    sessionless_case_repository as sessionless_module,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)
from tests.unit.modules.case.test_case_delete_gate_2096 import _GatedEraser
from tests.unit.modules.case.test_turn_receipt_1888 import (
    CASE_ID,
    ENTERPRISE,
    OWNER,
    _investigating_case,
    _user,
)
from tests.utils import regex_only_sanitizer, sanitize_pii_pinned

pytestmark = pytest.mark.integration

CASE = f"/api/v1/cases/{CASE_ID}"
KEY = (ENTERPRISE, CASE_ID)
REGISTRY = f"{CaseRedactionContext.REDIS_KEY_PREFIX}:{CASE_ID}"
#: One original value and its placeholder, as a turn would have stored them.
SEEDED = {json.dumps(["EMAIL_ADDRESS", "oncall@example.com"]): "<EMAIL_ADDRESS_1a2b>"}
TITLE = "Checkout Pool Exhaustion"
#: User chat past ``EXTRACTIVE_MAX_CONTENT_LENGTH``: the title calls the model.
CHAT = "\n".join(
    f"{i}. [09:0{i}] User: checkout pods return 503 while the postgres connection "
    f"pool sits at its limit of 50 and requests queue for thirty seconds ({i})"
    for i in range(1, 5)
)


@dataclass
class World:
    client: httpx.AsyncClient
    repository: SessionlessCaseRepository
    redis: Any
    model: AsyncMock
    gate: _GatedEraser

    async def title(self) -> httpx.Response:
        return await self.client.post(f"{CASE}/title")

    async def delete(self) -> httpx.Response:
        return await self.client.delete(CASE)

    async def registry(self) -> bool:
        return bool(await self.redis.exists(REGISTRY))

    async def row(self) -> bool:
        return await self.repository.get(CASE_ID) is not None

    async def lease_members(self) -> int:
        return await self.redis.zcard(inflight.lease_key(KEY))


@pytest.fixture(params=[False, True], ids=["pii_off", "pii_on"])
async def world(request, tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'fm-2580.db'}", poolclass=NullPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def _get_db_session():
        session = sessions()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    monkeypatch.setattr(sessionless_module, "get_db_session", _get_db_session)
    repository = SessionlessCaseRepository()
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    gate = _GatedEraser()
    # The registry eraser runs before the gate, so a delete held open at the
    # gate has already erased the registry.
    case_service = CaseService(
        repository,
        store_erasers=[CaseRedactionRegistryEraser(redis), gate],
        delete_gate=CaseDeleteGate(redis),
    )
    case_service.get_case_conversation_context = AsyncMock(return_value=CHAT)
    await repository.save(_investigating_case())
    await redis.hset(REGISTRY, mapping=SEEDED)

    model = MagicMock()
    model.route = AsyncMock(
        return_value=LLMResponse(
            content=TITLE,
            confidence=0.9,
            provider="test",
            model="test-model",
            tokens_used=8,
            response_time_ms=1,
        )
    )
    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.include_router(router, prefix="/api/v1")
    app.state.llm_provider = model
    app.state.redis_client = redis
    app.state.sanitizer = regex_only_sanitizer()
    app.dependency_overrides[require_authentication] = lambda: _user(OWNER)
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service

    with sanitize_pii_pinned(request.param):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield World(client, repository, redis, model.route, gate)
    await inflight.drain()
    await engine.dispose()


async def test_a_delete_during_a_title_call_refuses_and_leaves_no_registry_behind(
    world,
):
    entered, release = asyncio.Event(), asyncio.Event()
    reply = world.model.return_value

    async def _thinking(**_kwargs):
        entered.set()
        await release.wait()
        return reply

    world.model.side_effect = _thinking
    title = asyncio.create_task(world.title())
    await entered.wait()  # the registry is loaded; the model call runs

    # (a) The delete refuses with its lease answer and clears nothing.
    refused = await world.delete()
    assert refused.status_code == 409, refused.text
    assert refused.headers["x-error-code"] == "CASE_WORK_IN_PROGRESS"
    assert int(refused.headers["Retry-After"]) >= 1
    assert (await world.row(), await world.registry()) == (True, True)

    release.set()
    answer = await title
    assert answer.status_code == 200, answer.text
    assert answer.headers["x-title-source"] == "llm"
    assert answer.json()["title"] == TITLE
    assert (await world.repository.get(CASE_ID)).title == TITLE

    # (b) The registry key is present exactly when the case row is: before the
    # delete both stand, and the delete the released hold lets through takes
    # both.
    assert (await world.row(), await world.registry()) == (True, True)
    assert (await world.delete()).status_code == 204
    assert (await world.row(), await world.registry()) == (False, False)


async def test_a_title_call_during_a_delete_is_refused_before_any_model_call(world):
    world.gate.release.clear()
    deleting = asyncio.create_task(world.delete())
    await world.gate.entered.wait()  # the tombstone stands; the registry is erased

    answer = await world.title()

    assert answer.status_code == 409, answer.text
    assert answer.headers["x-error-code"] == "CASE_DELETE_IN_PROGRESS"
    assert answer.json()["detail"] == "This case is being deleted. Reload the case."
    assert "Retry-After" not in answer.headers
    world.model.assert_not_awaited()
    assert not await world.registry()
    assert KEY not in inflight._LEASES

    world.gate.release.set()
    assert (await deleting).status_code == 204
    assert (await world.row(), await world.registry()) == (False, False)


@pytest.mark.parametrize(
    "outcome, status",
    [
        ((TITLE, "llm", 250), 200),
        (_TitleSubstanceTooThin("too thin to name"), 422),
        (_TitleDisclosureGateUnavailable("gate down"), 503),
        (NotFoundError("Case", CASE_ID), 404),
        (CaseNotDriverError(), 403),
        (RawModelContentRefused("prompt raw"), 500),
        (RuntimeError("boom"), 500),
    ],
    ids=["ok", "substance", "gate", "deleted", "handed-off", "refused", "unexpected"],
)
async def test_the_hold_is_released_on_every_exit(world, monkeypatch, outcome, status):
    held: list[int] = []

    async def _generate(**_kwargs):
        held.append(await world.lease_members())
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(cases_module, "_generate_and_persist_title", _generate)

    assert (await world.title()).status_code == status
    assert held == [1]  # the generation ran under the case's lease
    assert KEY not in inflight._LEASES
    assert await world.lease_members() == 0
    assert (await world.delete()).status_code == 204


async def test_a_title_call_cancelled_mid_generation_releases_the_hold(world):
    entered = asyncio.Event()

    async def _stuck(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    world.model.side_effect = _stuck
    title = asyncio.create_task(world.title())
    await entered.wait()
    assert await world.lease_members() == 1

    title.cancel()
    with pytest.raises(asyncio.CancelledError):
        await title

    assert KEY not in inflight._LEASES
    assert await world.lease_members() == 0
    assert (await world.delete()).status_code == 204
