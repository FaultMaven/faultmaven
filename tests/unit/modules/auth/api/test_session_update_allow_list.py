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

import json
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
    _FIELD_MESSAGES,
    CLIENT_UPDATABLE_SESSION_FIELDS,
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
NOT_JSON_DETAIL = "metadata must hold only JSON values"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.security,
    pytest.mark.session,
]


def _refused_detail(refused: list[str]) -> str:
    """The refusal for up to five short keys: each named verbatim, then a count."""
    named = ", ".join(repr(key) for key in refused)
    return (
        f"Session fields cannot be updated: {named} ({len(refused)} refused). "
        f"{ONLY_METADATA}"
    )


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
    # An exception that escapes the app is answered 500, as a server would,
    # rather than re-raised into the test: the status is what is asserted.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
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


@pytest.mark.parametrize(
    "body, count, shown, hidden",
    [
        pytest.param(
            {"k" * 5000: 1},
            1,
            # repr() clipped to 64 characters, the last of them the ellipsis.
            "'" + "k" * 62 + "…",
            "k" * 63,
            id="one-5000-char-key",
        ),
        pytest.param(
            {f"k{i}": 0 for i in range(10_000)},
            10_000,
            # The first five, sorted, then an ellipsis for the rest.
            "'k0', 'k1', 'k10', 'k100', 'k1000', …",
            "'k1001'",
            id="10000-keys",
        ),
    ],
)
async def test_the_refusal_echoes_a_bounded_number_of_clipped_keys(
    world, body, count, shown, hidden
):
    """The refused keys are caller input; the 400 does not grow with them."""
    own_before = await _record(world, world.own_id)
    keys_before = await _keys(world)

    response = await _put(world, body)

    assert response.status_code == 400, response.text[:200]
    detail = response.json()["detail"]
    assert len(detail) < 600, len(detail)
    assert f"({count} refused)" in detail
    assert shown in detail
    assert hidden not in detail
    assert await _record(world, world.own_id) == own_before
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


def test_every_allowed_field_has_a_fixed_wrong_type_message():
    """The wrong-type 400 is looked up by the failing field's name.

    An allowed field without an entry would fall back to a generic message
    that says nothing about what was wrong.
    """
    assert CLIENT_UPDATABLE_SESSION_FIELDS <= _FIELD_MESSAGES.keys()


async def test_an_empty_body_is_a_400_that_changes_nothing(world):
    own_before = await _record(world, world.own_id)
    keys_before = await _keys(world)

    response = await _put(world, {})

    assert response.status_code == 400, response.text
    assert await _record(world, world.own_id) == own_before
    assert await _keys(world) == keys_before


@pytest.mark.parametrize(
    "raw, status, detail",
    [
        pytest.param("not json", 422, None, id="invalid-json"),
        pytest.param("[]", 422, None, id="array"),
        pytest.param('"x"', 422, None, id="string"),
        pytest.param("null", 422, None, id="null"),
        pytest.param("", 422, None, id="empty"),
        # Python's JSON parser accepts these; no JSON response can carry them.
        # Before the check they were saved, and that PUT and every later read
        # of the session answered 500.
        pytest.param('{"metadata": {"x": NaN}}', 400, NOT_JSON_DETAIL, id="nan"),
        pytest.param(
            '{"metadata": {"x": Infinity}}', 400, NOT_JSON_DETAIL, id="infinity"
        ),
        pytest.param(
            '{"metadata": {"x": -Infinity}}', 400, NOT_JSON_DETAIL, id="-infinity"
        ),
        pytest.param(
            '{"metadata": {"x": 1e400}}', 400, NOT_JSON_DETAIL, id="1e400-is-inf"
        ),
        pytest.param(
            '{"metadata": {"x": "\\ud800"}}',
            400,
            NOT_JSON_DETAIL,
            id="lone-surrogate-value",
        ),
        pytest.param(
            '{"metadata": {"\\ud800": 1}}',
            400,
            NOT_JSON_DETAIL,
            id="lone-surrogate-key",
        ),
        pytest.param(
            '{"metadata": {"a": {"b": [1, NaN]}}}',
            400,
            NOT_JSON_DETAIL,
            id="nested-nan",
        ),
    ],
)
async def test_a_body_that_is_not_a_json_object_is_a_4xx_not_a_500(
    world, raw, status, detail
):
    """A body that is not a JSON object, or holds a non-JSON value, is a 4xx.

    Sent as raw text: ``httpx``'s ``json=`` refuses NaN before it is sent.
    """
    own_before = await _record(world, world.own_id)
    keys_before = await _keys(world)

    response = await world.client.put(
        f"/api/v1/sessions/{world.own_id}",
        content=raw,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == status, response.text
    if detail is not None:
        assert response.json()["detail"] == detail
    assert await _record(world, world.own_id) == own_before
    assert await _keys(world) == keys_before


async def test_a_stored_record_naming_another_session_is_not_written_through(
    world,
):
    """``save()`` keys on the record's own ``session_id``, never the path's.

    A stored record whose ``session_id`` names another session would redirect
    the write onto that session: the #1834 mechanism from the stored side. The
    record is planted directly, so the route's ownership check passes, and the
    update must write nothing anywhere. A 5xx is the right answer: the stored
    state is corrupt, which no request body caused.
    """
    key = f"{world.service.session_store.prefix}{world.own_id}"
    planted = json.loads(await world.redis.get(key))
    planted["session_id"] = world.victim_id
    await world.redis.set(key, json.dumps(planted))
    planted_raw = await world.redis.get(key)
    victim_before = await _record(world, world.victim_id)
    keys_before = await _keys(world)

    response = await _put(world, {"metadata": {"a": 1}})

    assert response.status_code == 500, response.text
    assert await world.redis.get(key) == planted_raw
    victim_after = await _record(world, world.victim_id)
    assert victim_after["user_id"] == VICTIM
    assert victim_after == victim_before
    assert await _keys(world) == keys_before


# ---------------------------------------------------------------------------
# Vacuity controls: what a client may set, it still can
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({}, id="cleared"),
        pytest.param({"nested": {"x": [1, 2]}}, id="nested"),
        # Non-ASCII is JSON: the representability check must not refuse it.
        pytest.param({"emoji": "\N{GRINNING FACE}"}, id="emoji"),
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
    # An accepted update stamps the record.
    assert own_after["updated_at"] > own_before["updated_at"]
    # Only metadata and the update stamp moved.
    for field in ("metadata", "updated_at"):
        own_before.pop(field)
        own_after.pop(field)
    assert own_after == own_before
    assert await _record(world, world.victim_id) == victim_before
