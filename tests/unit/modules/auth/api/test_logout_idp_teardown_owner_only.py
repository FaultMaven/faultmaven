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
"""

from __future__ import annotations

import types
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import fakeredis.aioredis as fakeredis_aio
import jwt as jwt_lib
import pytest

from faultmaven.modules.auth.api.auth import logout
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
    victim, _ = await sessions.create_session(
        "victim-user", metadata={"provider_session_id": VICTIM_IDP_SESSION}
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
    yield SimpleNamespace(request=request, victim=victim, revoked=revoked)
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
