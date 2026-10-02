"""``PUT /api/v1/sessions/{session_id}`` sets ``metadata`` and nothing else (#1834).

The route checks that the caller owns the path's session, then hands the raw
body to ``AuthSessionService.update_session``. That method used to apply every
key naming any attribute of the session, guarded only by a deny-list of four
case-data keys, none of which ``SessionContext`` even declares::

    for key, value in updates.items():
        if hasattr(session, key):
            setattr(session, key, value)

``save()`` keys its write on ``session.session_id``, so a body carrying another
session's id overwrote THAT record, and a ``user_id`` beside it made the stolen
record the caller's. ``{"active": false}`` named a read-only property and
answered 500.

These tests drive the real route with the real ``AuthSessionService`` and
``RedisSessionStore`` over FakeRedis, the shape the issue reproduced on. Only
the two dependencies that need a deployment are overridden: the session
service (to inject the FakeRedis-backed one) and ``require_authentication``.
The app registers the same exception handlers ``main.py`` does, so a status
or ``detail`` asserted here is the one production renders.

Every refusal is checked for its effect as well as its status: the stored
record, read back through ``get_session``, must be exactly what it was.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict

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
from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.modules.auth.api.session import router as session_router
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)

ATTACKER = "user-attacker-01"
VICTIM = "user-victim-02"

#: What the caller's own session holds before each request, so a refusal that
#: cleared or replaced it is visible.
SEED_METADATA = {"seed": "kept", "n": 1}

ONLY_METADATA = "Only 'metadata' may be updated."
WRONG_TYPE_DETAIL = "metadata must be a JSON object"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.security,
    pytest.mark.session,
]


def _refused_detail(refused: list[str]) -> str:
    return f"Session fields cannot be updated: {refused}. {ONLY_METADATA}"


def _build_app(service: AuthSessionService, current_user: DevUser) -> FastAPI:
    """Mount the real session router with production's exception handlers."""
    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.add_exception_handler(
        RequestValidationError, request_validation_exception_handler
    )
    app.add_exception_handler(HTTPException, http_exception_handler)

    async def _session_service() -> AuthSessionService:
        return service

    async def _current_user() -> DevUser:
        return current_user

    app.dependency_overrides[get_session_service] = _session_service
    app.dependency_overrides[require_authentication] = _current_user
    return app


@pytest.fixture
async def world():
    """The attacker owns ``own``; the victim owns ``victim``. Both are stored."""
    redis = fakeredis_aio.FakeRedis(decode_responses=True)
    service = AuthSessionService(session_store=RedisSessionStore(redis))
    own, _ = await service.create_session(ATTACKER, metadata=dict(SEED_METADATA))
    victim, _ = await service.create_session(VICTIM)
    attacker = DevUser(
        user_id=ATTACKER,
        username="attacker",
        email="attacker@example.com",
        display_name="Attacker",
        created_at=datetime.now(timezone.utc),
    )
    app = _build_app(service, attacker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield SimpleNamespace(
            client=client,
            redis=redis,
            service=service,
            own_id=own.session_id,
            victim_id=victim.session_id,
        )


async def _record(world, session_id: str) -> Dict[str, Any]:
    """The stored session, read back through the service's own reader."""
    session = await world.service.get_session(session_id)
    assert session is not None, f"session {session_id} vanished"
    return session.model_dump()


async def _keys(world) -> list[str]:
    """Every key in the store: a refused write must not land on a NEW record.

    The two records above are not enough on their own. ``save()`` keys its
    write on the session id in the body, so ``{"session_id": "victim"}``
    applied anyway would create a record nobody reads back by id.
    """
    return sorted(await world.redis.keys("*"))


async def _put(world, body: Any) -> httpx.Response:
    return await world.client.put(f"/api/v1/sessions/{world.own_id}", json=body)


# ---------------------------------------------------------------------------
# The issue's attack
# ---------------------------------------------------------------------------


async def test_reassigning_session_id_and_user_id_is_refused_and_changes_nothing(
    world,
):
    """The reproduction from #1834, through the route.

    Before the fix this answered 200 and the victim's record read
    ``user_id == attacker`` afterwards.
    """
    victim_before = await _record(world, world.victim_id)
    own_before = await _record(world, world.own_id)
    keys_before = await _keys(world)

    response = await _put(world, {"session_id": world.victim_id, "user_id": ATTACKER})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == _refused_detail(["session_id", "user_id"])
    # The submitted value is never echoed — here, another account's session id.
    assert world.victim_id not in response.text

    victim_after = await _record(world, world.victim_id)
    assert victim_after["user_id"] == VICTIM
    assert victim_after == victim_before
    assert await _record(world, world.own_id) == own_before
    assert await _keys(world) == keys_before


# ---------------------------------------------------------------------------
# The probe table: every key other than ``metadata`` is refused
# ---------------------------------------------------------------------------

REFUSED_BODIES = [
    pytest.param({"session_id": "victim"}, ["session_id"], id="session_id"),
    pytest.param({"user_id": "x"}, ["user_id"], id="user_id"),
    # A read-only property: setattr raised AttributeError and the route said 500.
    pytest.param({"active": False}, ["active"], id="active-property"),
    pytest.param(
        {"expires_at": "2099-01-01T00:00:00Z"}, ["expires_at"], id="expires_at"
    ),
    pytest.param({"client_id": "c"}, ["client_id"], id="client_id"),
    pytest.param({"model_config": {}}, ["model_config"], id="model_config"),
    pytest.param({"__class__": 1}, ["__class__"], id="dunder-class"),
    pytest.param({"Metadata": {}}, ["Metadata"], id="near-miss-case"),
    pytest.param({"metadata ": {}}, ["metadata "], id="near-miss-space"),
    pytest.param({"case_history": []}, ["case_history"], id="case_history"),
]


@pytest.mark.parametrize("body, refused", REFUSED_BODIES)
async def test_a_key_other_than_metadata_is_a_400_that_changes_nothing(
    world, body, refused
):
    own_before = await _record(world, world.own_id)
    victim_before = await _record(world, world.victim_id)
    keys_before = await _keys(world)

    response = await _put(world, body)

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == _refused_detail(refused)
    assert await _record(world, world.own_id) == own_before
    assert await _record(world, world.victim_id) == victim_before
    assert await _keys(world) == keys_before


async def test_a_body_mixing_metadata_and_a_refused_key_applies_neither(world):
    """Refused as a whole, before anything is applied.

    ``metadata`` alone would be accepted, so this fails if the allow-list is
    checked after the update is written rather than before.
    """
    own_before = await _record(world, world.own_id)
    keys_before = await _keys(world)

    response = await _put(world, {"metadata": {"a": 1}, "user_id": "x"})

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == _refused_detail(["user_id"])
    own_after = await _record(world, world.own_id)
    assert own_after["metadata"] == SEED_METADATA
    assert own_after["user_id"] == ATTACKER
    assert own_after == own_before
    assert await _keys(world) == keys_before


# ---------------------------------------------------------------------------
# ``metadata`` is validated against its declared type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("x", id="string"),
        pytest.param(None, id="null"),
        pytest.param([1], id="list"),
    ],
)
async def test_a_metadata_that_is_not_an_object_is_a_400_that_changes_nothing(
    world, value
):
    own_before = await _record(world, world.own_id)
    keys_before = await _keys(world)

    response = await _put(world, {"metadata": value})

    assert response.status_code == 400, response.text
    # A fixed message: neither pydantic's error text nor the value is echoed.
    assert response.json()["detail"] == WRONG_TYPE_DETAIL
    assert await _record(world, world.own_id) == own_before
    assert await _keys(world) == keys_before


async def test_an_empty_body_is_a_400_that_changes_nothing(world):
    own_before = await _record(world, world.own_id)
    keys_before = await _keys(world)

    response = await _put(world, {})

    assert response.status_code == 400, response.text
    assert await _record(world, world.own_id) == own_before
    assert await _keys(world) == keys_before


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json", id="invalid-json"),
        pytest.param("[]", id="array"),
        pytest.param('"x"', id="string"),
        pytest.param("null", id="null"),
        pytest.param("", id="empty"),
    ],
)
async def test_a_body_that_is_not_a_json_object_is_a_4xx_not_a_500(world, raw):
    """The invariant's last clause: no body, valid JSON or not, answers 500."""
    own_before = await _record(world, world.own_id)
    keys_before = await _keys(world)

    response = await world.client.put(
        f"/api/v1/sessions/{world.own_id}",
        content=raw,
        headers={"content-type": "application/json"},
    )

    assert 400 <= response.status_code < 500, response.text
    assert await _record(world, world.own_id) == own_before
    assert await _keys(world) == keys_before


# ---------------------------------------------------------------------------
# Vacuity controls: what a client may set, it still can
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({}, id="cleared"),
        pytest.param({"nested": {"x": [1, 2]}}, id="nested"),
    ],
)
async def test_metadata_round_trips(world, metadata):
    """``metadata`` replaces the stored value and reads back as given."""
    own_before = await _record(world, world.own_id)
    victim_before = await _record(world, world.victim_id)

    response = await _put(world, {"metadata": metadata})

    assert response.status_code == 200, response.text
    assert response.json()["metadata"] == metadata
    own_after = await _record(world, world.own_id)
    assert own_after["metadata"] == metadata
    # Only metadata and the update stamp moved.
    for field in ("metadata", "updated_at"):
        own_before.pop(field)
        own_after.pop(field)
    assert own_after == own_before
    assert await _record(world, world.victim_id) == victim_before
