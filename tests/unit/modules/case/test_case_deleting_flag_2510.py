"""#2510 — a read sees a running delete: ``GET /cases/{id}`` carries ``deleting``.

A turn refused with 409 ``CASE_DELETE_IN_PROGRESS`` tells its client to reload
the case, but the delete clears every store before the row, so a reload while
it ran read the case unchanged and live. Driven through the MOUNTED case router
with the REAL ``CaseService`` (over the in-memory repository) and the REAL
``CaseDeleteGate`` on FakeRedis, as standalone runs it. The delete is held open
in its first eraser: its tombstone written, its row still there.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI, Request

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.config.constants import STANDALONE_ENTERPRISE_ID
from faultmaven.config.tenant_context import set_current_enterprise_id
from faultmaven.infrastructure.concurrency import inflight
from faultmaven.infrastructure.concurrency.inflight import CaseDeleteGate
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)
from tests.unit.modules.case.test_case_delete_gate_2096 import _GatedEraser
from tests.unit.modules.case.test_turn_receipt_1888 import _user

pytestmark = pytest.mark.unit

ENTERPRISE = "ent_2510"
OWNER = "user_2510_owner"
READER = "user_2510_reader"  # reads the case through a team share


@pytest.fixture(autouse=True)
def _tenant():
    set_current_enterprise_id(ENTERPRISE)
    yield
    set_current_enterprise_id(STANDALONE_ENTERPRISE_ID)


@pytest.fixture
async def world():
    repository = InMemoryCaseRepository()
    case = await repository.save(
        Case(user_id=OWNER, enterprise_id=ENTERPRISE, title="Checkout 503s")
    )
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    gate = _GatedEraser()
    service = CaseService(
        repository,
        team_service=SimpleNamespace(
            list_all_user_team_ids=AsyncMock(return_value=["team_1"])
        ),
        share_repository=SimpleNamespace(
            list_resource_ids=AsyncMock(return_value=[case.case_id]),
            list_scopes_for_resources=AsyncMock(return_value={}),
        ),
        store_erasers=[gate],
        delete_gate=CaseDeleteGate(redis),
    )
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: service

    def _caller(request: Request):
        return _user(request.headers.get("x-test-user", OWNER))

    app.dependency_overrides[require_authentication] = _caller
    url = f"/api/v1/cases/{case.case_id}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:

        async def read(user: str = OWNER) -> httpx.Response:
            return await client.get(url, headers={"x-test-user": user})

        async def delete() -> httpx.Response:
            return await client.delete(url)

        yield SimpleNamespace(
            read=read,
            delete=delete,
            gate=gate,
            redis=redis,
            key=(ENTERPRISE, case.case_id),
        )


async def _deleting(world) -> bool:
    answer = await world.read()
    assert answer.status_code == 200, answer.text
    return answer.json()["deleting"]


async def _delete_held_open(world) -> asyncio.Task:
    world.gate.release.clear()
    deleting = asyncio.create_task(world.delete())
    await world.gate.entered.wait()  # the tombstone stands; the row is there
    return deleting


async def test_every_reader_sees_the_delete_run_and_then_the_case_is_gone(world):
    assert await _deleting(world) is False
    deleting = await _delete_held_open(world)

    for reader in (OWNER, READER):
        answer = await world.read(reader)
        assert answer.status_code == 200, answer.text
        assert answer.json()["deleting"] is True

    world.gate.release.set()
    assert (await deleting).status_code == 204
    assert (await world.read()).status_code == 404


async def test_a_delete_that_fails_leaves_the_case_live_and_writable(world):
    world.gate.fail = True
    deleting = await _delete_held_open(world)
    assert await _deleting(world) is True

    world.gate.release.set()
    assert (await deleting).status_code == 500
    assert await _deleting(world) is False
    await (await inflight.hold_case(world.redis, world.key)).release()  # a turn


async def test_a_delete_refused_on_work_in_flight_leaves_the_case_reading_live(
    world,
):
    """``begin`` writes the tombstone before it reads the lease; refused, it
    takes the tombstone back, so the next read is not told a delete runs."""
    hold = await inflight.hold_case(world.redis, world.key)  # a turn in flight
    try:
        refused = await world.delete()
        assert refused.status_code == 409
        assert refused.headers["x-error-code"] == "CASE_WORK_IN_PROGRESS"
        assert await _deleting(world) is False
    finally:
        await hold.release()


async def test_a_tombstone_lapsed_under_a_running_delete_reads_false(world):
    """Best-effort: the row is still there and the delete still runs."""
    deleting = await _delete_held_open(world)
    await world.redis.delete(inflight.tombstone_key(world.key))

    assert await _deleting(world) is False
    world.gate.release.set()
    assert (await deleting).status_code == 204


# ============================================================
# The gate's read
# ============================================================

KEY = (ENTERPRISE, "case_2510aaaaaaaa")


async def test_the_gate_reads_the_tombstone_while_it_stands():
    gate = CaseDeleteGate(fakeredis_aio.FakeRedis(decode_responses=True))
    assert await gate.deleting(KEY) is False

    tombstone = await gate.begin(KEY)
    assert await gate.deleting(KEY) is True

    await tombstone.end(deleted=False)
    assert await gate.deleting(KEY) is False


def _unreachable():
    server = fakeredis.FakeServer()
    server.connected = False
    return fakeredis_aio.FakeRedis(server=server, decode_responses=True)


@pytest.mark.parametrize(
    "store, reason", [(None, "no_store"), (_unreachable, "store_error")]
)
async def test_an_unreadable_tombstone_reads_false_and_is_logged(
    store, reason, caplog, monkeypatch
):
    monkeypatch.setattr(inflight, "_REPORTED", set())
    gate = CaseDeleteGate(store() if store else None)

    with caplog.at_level(logging.WARNING, logger=inflight.__name__):
        assert await gate.deleting(KEY) is False
        assert await gate.deleting(KEY) is False  # logged once per reason

    logged = [r.getMessage() for r in caplog.records if "tombstone" in r.getMessage()]
    assert len(logged) == 1 and reason in logged[0]
