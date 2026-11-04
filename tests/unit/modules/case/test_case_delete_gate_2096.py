"""#2096 — a case delete refuses while work on the case is in flight.

Driven through the MOUNTED case router (``DELETE /api/v1/cases/{id}`` and
``POST /api/v1/cases/{id}/turns``), the REAL ``CaseService`` with the real
upload and case-vector erasers (a filesystem backend, a ChromaDB
``PersistentClient``), the REAL ``InvestigationService`` and ``MilestoneEngine``
(the generator stubbed, no provider call), and the production
``SessionlessCaseRepository`` over a SQLite file. The lease and the tombstone
live in FakeRedis, as standalone's do. Requests overlap on one event loop.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import chromadb
import fakeredis
import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from chromadb.errors import NotFoundError as ChromaNotFoundError
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_investigation_service
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.infrastructure.concurrency import inflight
from faultmaven.infrastructure.concurrency.inflight import CaseDeleteGate
from faultmaven.infrastructure.persistence.case_vector_store import CaseVectorStore
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.infrastructure.storage.filesystem import FilesystemStorageBackend
from faultmaven.modules.agent.domain.services.investigation_service.service import (
    InvestigationService,
)
from faultmaven.modules.case.api.routes import conversation as conversation_module
from faultmaven.modules.case.api.routes.dependencies import (
    _di_get_case_service_dependency,
)
from faultmaven.modules.case.api.routes.router import router
from faultmaven.modules.case.domain.services.case_service import CaseService
from faultmaven.modules.case.infrastructure import (
    sessionless_case_repository as sessionless_module,
)
from faultmaven.modules.case.infrastructure.sessionless_case_repository import (
    SessionlessCaseRepository,
)
from faultmaven.modules.evidence.domain.services.file_storage_service import (
    FileStorageService,
)
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)
from tests.chroma_clients import closed_at_teardown
from tests.unit.modules.case.test_turn_receipt_1888 import (
    CASE_ID,
    ENTERPRISE,
    OWNER,
    REPLY,
    _investigating_case,
    _preprocessing,
    _user,
)

pytestmark = pytest.mark.unit

CASE = f"/api/v1/cases/{CASE_ID}"
COLLECTION = f"case_{CASE_ID}"
LOG = {"files": ("pool.log", b"ERROR pool exhausted after 30s\n", "text/plain")}


class _GatedEraser:
    """The first eraser: holds the delete open, or raises, when told to."""

    store_name = "gate"

    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.fail = False

    async def erase_case(self, case_id: str, enterprise_id: str) -> int:
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise ConnectionError("store unreachable")
        return 0


@dataclass
class World:
    client: httpx.AsyncClient
    repository: SessionlessCaseRepository
    case_service: CaseService
    blobs: FilesystemStorageBackend
    chroma: Any
    redis: Any
    generate: AsyncMock
    gate: _GatedEraser

    async def turn(self, **files) -> httpx.Response:
        return await self.client.post(
            f"{CASE}/turns", data={"query": "why 503?"}, files=files or None
        )

    async def delete(self) -> httpx.Response:
        return await self.client.delete(CASE)

    async def stored(self) -> dict:
        """What each store holds of the case."""
        try:
            self.chroma.get_collection(name=COLLECTION)
            collection = True
        except ChromaNotFoundError:
            collection = False
        return {
            "blobs": len(await self.blobs.list_keys(prefix=f"{ENTERPRISE}/{CASE_ID}/")),
            "collection": collection,
            "row": await self.repository.get(CASE_ID) is not None,
        }


@pytest.fixture
async def world(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'fm-2096.db'}", poolclass=NullPool
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
    blobs = FilesystemStorageBackend(storage_root=str(tmp_path / "blobs"))
    storage = FileStorageService(backend=blobs)
    chroma = closed_at_teardown(chromadb.PersistentClient(path=str(tmp_path / "c")))
    redis = fakeredis_aio.FakeRedis(decode_responses=True)

    conversion = MagicMock()
    conversion.get_conversion_by_case = AsyncMock(return_value=None)
    conversion.list_drafts_for_case = AsyncMock(return_value=[])
    milestone_engine = MilestoneEngine(
        MagicMock(),
        repository,
        investigation_tools=MagicMock(),
        report_service=ReportGenerationService(case_repository=repository),
        conversion_service=conversion,
    )
    generate = AsyncMock(
        return_value=InvestigationResponse_Diagnosis(
            agent_response=REPLY, state_updates={}
        )
    )
    milestone_engine.generator.generate_structured_output = generate
    cap = MagicMock()
    cap.reserve = AsyncMock()
    investigation = InvestigationService(
        milestone_engine,
        repository,
        preprocessing_service=_preprocessing(),
        file_storage_service=storage,
        turn_cap=cap,
    )
    investigation.out_of_band_triage.triage = AsyncMock(return_value=None)
    gate = _GatedEraser()
    case_service = CaseService(
        repository,
        store_erasers=[gate, storage, CaseVectorStore(chroma)],
        delete_gate=CaseDeleteGate(redis),
    )
    await case_service.repository.save(_investigating_case())
    chroma.get_or_create_collection(name=COLLECTION)  # its evidence index

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.llm_provider = None
    app.state.redis_client = redis
    app.dependency_overrides[require_authentication] = lambda: _user(OWNER)
    app.dependency_overrides[_di_get_case_service_dependency] = lambda: case_service
    app.dependency_overrides[get_investigation_service] = lambda: investigation

    with patch.object(conversation_module, "_auto_title_case_if_default", AsyncMock()):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            yield World(
                client, repository, case_service, blobs, chroma, redis, generate, gate
            )
    await inflight.drain()
    await engine.dispose()


def _refused_as_work_in_progress(answer: httpx.Response) -> None:
    assert answer.status_code == 409, answer.text
    assert answer.headers["x-error-code"] == "CASE_WORK_IN_PROGRESS"
    assert int(answer.headers["Retry-After"]) >= 1
    assert answer.json()["detail"]["error"]["code"] == "CASE_WORK_IN_PROGRESS"


#: The upload and its metadata sidecar, the case's evidence index, its row.
ALL_PRESENT = {"blobs": 2, "collection": True, "row": True}
ALL_GONE = {"blobs": 0, "collection": False, "row": False}


async def test_a_delete_during_a_turn_clears_nothing_and_one_after_it_proceeds(world):
    entered, release = asyncio.Event(), asyncio.Event()
    reply = world.generate.return_value

    async def _thinking(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return reply

    world.generate.side_effect = _thinking
    turn = asyncio.create_task(world.turn(**LOG))
    await entered.wait()  # the upload's blob is stored; the turn still runs

    _refused_as_work_in_progress(await world.delete())
    assert await world.stored() == ALL_PRESENT

    release.set()
    assert (await turn).status_code == 200
    await inflight.drain()  # the turn's post-commit upload link
    assert (await world.delete()).status_code == 204
    assert await world.stored() == ALL_GONE


async def test_a_task_a_turn_spawned_holds_the_delete_off_until_it_ends(world):
    """Proactive vectorization outlives its turn: the case's lease outlives the
    turn with it, and the delete refuses until the task ends."""
    finish = asyncio.Event()
    reply = world.generate.return_value

    async def _spawning(*_args, **_kwargs):
        inflight.track(
            asyncio.create_task(finish.wait()),
            what="vectorization of evidence ev_1",
            case=(ENTERPRISE, CASE_ID),
            kind=inflight.Kind.CANCELLABLE,
        )
        return reply

    world.generate.side_effect = _spawning
    assert (await world.turn(**LOG)).status_code == 200

    _refused_as_work_in_progress(await world.delete())
    assert await world.stored() == ALL_PRESENT

    finish.set()
    await inflight.drain()
    assert (await world.delete()).status_code == 204


async def test_a_turn_during_a_delete_is_refused_and_writes_nothing(world):
    world.gate.release.clear()
    deleting = asyncio.create_task(world.delete())
    await world.gate.entered.wait()  # the tombstone stands; the erasers run
    before = (await world.repository.get(CASE_ID)).current_turn

    answer = await world.turn(**LOG)

    assert answer.status_code == 409
    assert answer.headers["x-error-code"] == "CASE_DELETE_IN_PROGRESS"
    assert "Retry-After" not in answer.headers
    world.generate.assert_not_awaited()
    assert (await world.stored())["blobs"] == 0
    assert (await world.repository.get(CASE_ID)).current_turn == before
    world.gate.release.set()
    assert (await deleting).status_code == 204


async def test_a_delete_whose_eraser_raises_leaves_no_tombstone(world):
    """The retryable 500 must not hold the case's turns off."""
    world.gate.fail = True

    answer = await world.delete()

    assert answer.status_code == 500
    assert answer.json()["detail"]["error"]["code"] == "DELETE_CASE_ERROR"
    assert not await world.redis.exists(inflight.tombstone_key((ENTERPRISE, CASE_ID)))
    assert (await world.turn()).status_code == 200


async def test_an_unreachable_lease_store_answers_503_and_clears_nothing(world):
    assert (await world.turn(**LOG)).status_code == 200
    await inflight.drain()
    server = fakeredis.FakeServer()
    server.connected = False
    world.case_service.delete_gate = CaseDeleteGate(
        fakeredis_aio.FakeRedis(server=server, decode_responses=True)
    )

    answer = await world.delete()

    assert answer.status_code == 503
    assert answer.headers["x-error-code"] == "CASE_DELETE_UNAVAILABLE"
    assert int(answer.headers["Retry-After"]) >= 1
    assert await world.stored() == ALL_PRESENT


async def test_an_interrupted_keyed_release_still_releases_the_case_lease(
    world, monkeypatch
):
    """Skipped, the case's count never reached 0: renewed forever, and every
    delete of the case answered 409 until the process restarted."""
    from faultmaven.modules.case.api.turn_idempotency import KeyedTurn

    async def _interrupted(self):
        raise asyncio.CancelledError()

    monkeypatch.setattr(KeyedTurn, "release", _interrupted)

    with pytest.raises(asyncio.CancelledError):
        await world.client.post(
            f"{CASE}/turns",
            data={"query": "why 503?"},
            headers={"Idempotency-Key": "opt_msg_1759900000000_7"},
        )
    assert (ENTERPRISE, CASE_ID) not in inflight._LEASES
    assert not await world.redis.zcard(inflight.lease_key((ENTERPRISE, CASE_ID)))
