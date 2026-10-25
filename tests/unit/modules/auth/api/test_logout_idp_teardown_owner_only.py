"""Logout ends an IdP session only for a session the caller owns (#2301 review).

``POST /auth/logout`` hands the caller's ``X-Session-Id`` header to
``SSOLoginService.end_idp_session``, which revokes the IdP (WorkOS) session
stored on that FaultMaven session. The header names whatever session the
caller chooses, and a session id is not a credential: it read the session and
revoked its handle without asking whose it was. Any signed-in user holding
another user's session id signed that user out of the IdP, and the
unauthenticated heartbeat keeps such an id alive for as long as anyone pings it.

Driven through the route with the real teardown method, over a real
``AuthSessionService`` and ``RedisSessionStore`` on FakeRedis; only the IdP
call is a recorder. No network.

The owner check is not enough while the handle is client-writable: it lived in
``metadata``, which ``POST /sessions`` merges from the request and
``PUT /sessions/{id}`` replaces, so an attacker could plant a victim's IdP
session id on a session of its own and log out. The handle is now a server-set
field no route accepts; the second half drives those two routes.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import fakeredis.aioredis as fakeredis_aio
import httpx
import jwt as jwt_lib
import pytest
from fastapi import FastAPI

from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_authentication,
)
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.modules.auth.api.auth import logout
from faultmaven.modules.auth.api.session import router as session_router
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.domain.services.sso_login_service import (
    SSOLoginService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

VICTIM_IDP_SESSION = "idp-session-of-the-victim"


@pytest.fixture
async def world():
    redis = fakeredis_aio.FakeRedis(
        server=fakeredis.FakeServer(), decode_responses=True
    )
    sessions = AuthSessionService(
        session_store=RedisSessionStore(redis, session_ttl_seconds=1800)
    )
    # As the SSO login stores it: the server-set field.
    victim, _ = await sessions.create_session(
        "victim-user", provider_session_id=VICTIM_IDP_SESSION
    )
    revoked: list[str] = []
    provider = SimpleNamespace(
        revoke_session=lambda *, provider_session_id: revoked.append(
            provider_session_id
        )
        or True
    )
    # The production method, bound to the two collaborators it reads.
    teardown = types.MethodType(
        SSOLoginService.end_idp_session,
        SimpleNamespace(_sessions=sessions, _provider=provider),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                auth_service=AsyncMock(),
                sso_login_service=SimpleNamespace(end_idp_session=teardown),
            )
        )
    )
    yield SimpleNamespace(
        request=request, victim=victim, revoked=revoked, sessions=sessions
    )
    await redis.aclose()


def _token(user_id):
    # Never verified: logout decodes without the signature, because
    # require_authentication already checked it.
    return jwt_lib.encode(
        {"sub": user_id, "jti": f"jti-{user_id}", "exp": 9999999999},
        "unit-test-secret-key-please-ignore",
        algorithm="HS256",
    )


async def _logout_as(world, user_id, session_id):
    return await logout(
        request=world.request,
        current_user=SimpleNamespace(user_id=user_id),
        token=_token(user_id),
        x_session_id=session_id,
    )


async def test_another_users_session_id_ends_nothing(world):
    result = await _logout_as(world, "attacker-user", world.victim.session_id)

    # The attacker's own sign-out still succeeds; only the victim's IdP
    # session is out of their reach.
    assert result.revoked_tokens == 1
    assert world.revoked == []


async def test_the_callers_own_session_ends_its_idp_session(world):
    """The positive control: the owner's logout still ends it."""
    await _logout_as(world, "victim-user", world.victim.session_id)

    assert world.revoked == [VICTIM_IDP_SESSION]


async def test_an_unknown_session_id_ends_nothing(world):
    await _logout_as(world, "victim-user", "no-such-session")

    assert world.revoked == []


# --------------------------------------------------------------------------- #
# The handle is server-set: no route lets a client plant or clear it
# --------------------------------------------------------------------------- #


def _attacker_client(world):
    """The real session router, the caller being the attacker."""
    attacker = DevUser(
        user_id="attacker-user",
        username="attacker",
        email="attacker@example.com",
        display_name="Attacker",
        created_at=datetime.now(timezone.utc),
    )
    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")

    async def _service():
        return world.sessions

    async def _user():
        return attacker

    app.dependency_overrides[get_session_service] = _service
    app.dependency_overrides[require_authentication] = _user
    app.dependency_overrides[get_current_user_optional] = _user
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_a_handle_planted_on_create_is_never_revoked(world):
    async with _attacker_client(world) as client:
        response = await client.post(
            "/api/v1/sessions",
            json={"metadata": {"provider_session_id": VICTIM_IDP_SESSION}},
        )
    assert response.status_code == 201, response.text
    planted = response.json()["session_id"]

    await _logout_as(world, "attacker-user", planted)

    assert world.revoked == []
    stored = await world.sessions.get_session(planted)
    assert stored.user_id == "attacker-user"
    assert stored.provider_session_id is None


async def test_a_handle_planted_on_update_is_never_revoked(world):
    own, _ = await world.sessions.create_session("attacker-user")
    async with _attacker_client(world) as client:
        in_metadata = await client.put(
            f"/api/v1/sessions/{own.session_id}",
            json={"metadata": {"provider_session_id": VICTIM_IDP_SESSION}},
        )
        as_a_field = await client.put(
            f"/api/v1/sessions/{own.session_id}",
            json={"provider_session_id": VICTIM_IDP_SESSION},
        )

    assert in_metadata.status_code == 200, in_metadata.text
    # The update allow-list names only ``metadata``.
    assert as_a_field.status_code == 400, as_a_field.text
    await _logout_as(world, "attacker-user", own.session_id)

    assert world.revoked == []
    stored = await world.sessions.get_session(own.session_id)
    assert stored.provider_session_id is None


async def test_a_metadata_update_keeps_the_server_set_handle(world):
    """Replacing ``metadata`` neither clears the handle nor reaches it: the
    owner's later logout still ends their IdP session."""
    own, _ = await world.sessions.create_session(
        "attacker-user", provider_session_id="idp-session-of-the-caller"
    )
    async with _attacker_client(world) as client:
        response = await client.put(
            f"/api/v1/sessions/{own.session_id}", json={"metadata": {}}
        )
    assert response.status_code == 200, response.text
    assert "provider_session_id" not in response.json()["metadata"]

    await _logout_as(world, "attacker-user", own.session_id)

    assert world.revoked == ["idp-session-of-the-caller"]
