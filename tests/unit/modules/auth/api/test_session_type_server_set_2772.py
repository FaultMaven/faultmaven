"""A session's type is the server's, set once from the typed create field (#2772).

``POST /sessions`` stored the typed ``session_type`` in ``metadata`` and then
merged the client's ``metadata`` over it, so ``metadata.session_type``
replaced the validated field; ``PUT /sessions/{id}`` replaces ``metadata``
wholesale, so it could change or drop the type later. The list's read of the
type sat behind a guard that was always false, so every row read
``troubleshooting`` and ``?session_type=<other>`` matched nothing.

Driven through the real routes, ``AuthSessionService`` and
``RedisSessionStore`` over FakeRedis.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from faultmaven.api.exception_handlers import (
    get_exception_handlers,
    http_exception_handler,
    request_validation_exception_handler,
)
from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_authentication,
)
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.models.common import SessionContext
from faultmaven.modules.auth.api.session import router as session_router
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from tests.utils import InMemorySessionStore

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.session]

OWNER = "user-owner-2772"


@pytest.fixture
async def world():
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    store = RedisSessionStore(redis, session_ttl_seconds=1800)
    service = AuthSessionService(session_store=store)
    owner = DevUser(
        user_id=OWNER,
        username="owner",
        email="owner@example.com",
        display_name="Owner",
        created_at=datetime.now(timezone.utc),
    )

    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(
        RequestValidationError, request_validation_exception_handler
    )
    app.add_exception_handler(HTTPException, http_exception_handler)

    async def _service():
        return service

    async def _owner():
        return owner

    app.dependency_overrides[get_session_service] = _service
    app.dependency_overrides[require_authentication] = _owner
    app.dependency_overrides[get_current_user_optional] = _owner
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(client=client, redis=redis, store=store, service=service)


async def _create(world, **body) -> dict:
    response = await world.client.post("/api/v1/sessions", json=body)
    assert response.status_code == 201, response.text[:300]
    return response.json()


async def _listed_type(world, session_id: str) -> str:
    response = await world.client.get("/api/v1/sessions")
    assert response.status_code == 200, response.text[:300]
    (row,) = [r for r in response.json()["sessions"] if r["session_id"] == session_id]
    return row["session_type"]


# ---------------------------------------------------------------------------
# Create: the typed field, never metadata
# ---------------------------------------------------------------------------


async def test_a_metadata_session_type_does_not_override_the_typed_field(world):
    """On ``main`` the echo was ``{"x": 1}``: metadata merged over the field."""
    created = await _create(
        world, session_type="troubleshooting", metadata={"session_type": {"x": 1}}
    )

    assert created["session_type"] == "troubleshooting"
    stored = await world.service.get_session(created["session_id"])
    assert stored.session_type == "troubleshooting"
    # Inert: stored as the client wrote it, as any other client key.
    assert stored.metadata == {"session_type": {"x": 1}}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(None, id="no-body"),
        pytest.param({}, id="empty"),
        pytest.param({"session_type": None}, id="explicit-null"),
    ],
)
async def test_the_type_defaults_to_troubleshooting(world, body):
    response = await world.client.post("/api/v1/sessions", json=body)

    assert response.status_code == 201, response.text[:300]
    assert response.json()["session_type"] == "troubleshooting"
    stored = await world.service.get_session(response.json()["session_id"])
    assert stored.session_type == "troubleshooting"
    assert stored.metadata == {}


async def test_the_copilot_create_shape_is_answered_with_its_type(world):
    created = await _create(
        world,
        client_id="copilot-1",
        session_type="troubleshooting",
        timeout_minutes=180,
    )

    assert created["session_type"] == "troubleshooting"
    assert created["session_resumed"] is False


async def test_a_resume_echoes_the_stored_type_not_the_requests(world):
    first = await _create(world, client_id="laptop", session_type="analysis")

    resumed = await _create(world, client_id="laptop", session_type="incident")

    assert resumed["session_id"] == first["session_id"]
    assert resumed["session_resumed"] is True
    assert resumed["session_type"] == "analysis"
    assert await _listed_type(world, first["session_id"]) == "analysis"


# ---------------------------------------------------------------------------
# Update: no metadata write changes the type
# ---------------------------------------------------------------------------


async def test_a_metadata_update_never_changes_the_type(world):
    """On ``main`` the type lived in the metadata the PUT replaces."""
    created = await _create(world, session_type="analysis")
    session_id = created["session_id"]

    for metadata in ({"session_type": "other"}, {"unrelated": True}):
        response = await world.client.put(
            f"/api/v1/sessions/{session_id}", json={"metadata": metadata}
        )
        assert response.status_code == 200, response.text[:300]
        # metadata stays wholesale-replaceable: exactly what the client wrote.
        assert response.json()["metadata"] == metadata
        # The type it used to carry in metadata, at the top level.
        assert response.json()["session_type"] == "analysis"
        assert await _listed_type(world, session_id) == "analysis"


async def test_the_type_is_not_an_update_field(world):
    created = await _create(world, session_type="analysis")

    response = await world.client.put(
        f"/api/v1/sessions/{created['session_id']}", json={"session_type": "other"}
    )

    assert response.status_code == 400, response.text[:300]
    assert await _listed_type(world, created["session_id"]) == "analysis"


# ---------------------------------------------------------------------------
# List: rows and the filter read the field
# ---------------------------------------------------------------------------


async def test_the_list_reports_and_filters_on_the_type(world):
    """On ``main`` every row read ``troubleshooting``; the filter matched nothing."""
    analysis = await _create(world, session_type="analysis")
    default = await _create(world)
    # A metadata key of the same name is not the type, for the filter either.
    decoy = await _create(world, metadata={"session_type": "analysis"})

    assert await _listed_type(world, analysis["session_id"]) == "analysis"
    assert await _listed_type(world, decoy["session_id"]) == "troubleshooting"

    filtered = await world.client.get(
        "/api/v1/sessions", params={"session_type": "analysis"}
    )
    assert filtered.status_code == 200, filtered.text[:300]
    body = filtered.json()
    assert [r["session_id"] for r in body["sessions"]] == [analysis["session_id"]]
    assert body["total_count"] == 1

    default_rows = await world.client.get(
        "/api/v1/sessions", params={"session_type": "troubleshooting"}
    )
    assert {r["session_id"] for r in default_rows.json()["sessions"]} == {
        default["session_id"],
        decoy["session_id"],
    }


async def test_the_filter_counts_before_pagination(world):
    for _ in range(3):
        await _create(world, session_type="analysis")
    await _create(world)

    response = await world.client.get(
        "/api/v1/sessions", params={"session_type": "analysis", "limit": 2}
    )

    body = response.json()
    assert body["total_count"] == 3
    assert len(body["sessions"]) == 2
    assert {r["session_type"] for r in body["sessions"]} == {"analysis"}


async def test_a_search_row_carries_the_type(world):
    """At the previous head the type left the row with ``metadata``."""
    created = await _create(world, session_type="analysis", metadata={"k": "v"})

    response = await world.client.post("/api/v1/sessions/search", json={})

    assert response.status_code == 200, response.text[:300]
    (row,) = response.json()["sessions"]
    assert row["session_id"] == created["session_id"]
    assert row["session_type"] == "analysis"
    assert row["metadata"] == {"k": "v"}


async def test_a_list_row_reports_the_sessions_status(world):
    """On ``main`` every row read ``inquiry``, archived or not."""
    archived = await _create(world)
    active = await _create(world)
    response = await world.client.post(
        f"/api/v1/sessions/{archived['session_id']}/archive"
    )
    assert response.status_code == 200, response.text[:300]

    rows = (await world.client.get("/api/v1/sessions")).json()["sessions"]

    assert {r["session_id"]: r["status"] for r in rows} == {
        archived["session_id"]: "archived",
        active["session_id"]: "active",
    }


async def test_a_heartbeat_reports_the_sessions_status(world):
    """At ``6615e3e4a`` the heartbeat answered ``inquiry``, archived or not."""
    archived = await _create(world)
    active = await _create(world)
    response = await world.client.post(
        f"/api/v1/sessions/{archived['session_id']}/archive"
    )
    assert response.status_code == 200, response.text[:300]

    for created, status in ((archived, "archived"), (active, "active")):
        beat = await world.client.post(
            f"/api/v1/sessions/{created['session_id']}/heartbeat"
        )
        assert beat.status_code == 200, beat.text[:300]
        assert beat.json()["status"] == status


# ---------------------------------------------------------------------------
# Stores: every implementation writes and reads the field
# ---------------------------------------------------------------------------


def _session(**fields) -> SessionContext:
    return SessionContext(session_id="s-2772", user_id=OWNER, **fields)


async def test_the_redis_store_round_trips_the_type(world):
    await world.store.save(_session(session_type="analysis"))

    assert (await world.store.get_session("s-2772")).session_type == "analysis"
    raw = json.loads(await world.redis.get(f"{world.store.prefix}s-2772"))
    assert raw["session_type"] == "analysis"


async def test_a_redis_record_without_the_type_reads_the_default(world):
    await world.store.save(_session(session_type="analysis"))
    key = f"{world.store.prefix}s-2772"
    record = json.loads(await world.redis.get(key))
    del record["session_type"]
    await world.redis.set(key, json.dumps(record))

    assert (await world.store.get_session("s-2772")).session_type == ("troubleshooting")


async def test_the_in_memory_store_round_trips_the_type():
    store = InMemorySessionStore()

    await store.save(_session(session_type="analysis"))

    assert (await store.get_session("s-2772")).session_type == "analysis"
