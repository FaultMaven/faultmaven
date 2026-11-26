"""A session-store failure no route catches is answered 503 with
``Retry-After: 5`` by the app's ``SessionStoreException`` handler (#2985).

Login and registration pass a ``FaultMavenException`` through to the app's
handlers, and the SSO exchange catches nothing, so each reaches the handler
when minting the session fails. The failure is a real ``RedisSessionStore``'s,
over a FakeRedis server emulating a lost connection.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis
import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.config.settings import AuthMode
from faultmaven.modules.auth.api import auth as auth_routes
from faultmaven.modules.auth.api import sso as sso_routes
from faultmaven.modules.auth.api.rate_limiting import require_sso_rate_limit_exchange
from faultmaven.modules.auth.domain.models.auth import DevUser
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)
from tests.utils import InMemoryRevocationStore

pytestmark = [pytest.mark.unit]


@pytest.fixture
async def sessions():
    server = fakeredis.FakeServer()
    server.connected = False
    redis = fakeredis_aio.FakeRedis(server=server, decode_responses=True)
    yield AuthSessionService(
        session_store=RedisSessionStore(redis, session_ttl_seconds=60)
    )
    await redis.aclose()


class _Users:
    """``user`` is found by name; ``create_user`` returns it."""

    def __init__(self, user):
        self.user = user

    async def get_user_by_username(self, username):
        return self.user

    async def create_user(self, username, email=None, display_name=None, **_):
        return _user()

    async def record_login(self, user_id):
        return None


class _Tokens:
    async def generate_token_pair(self, user, *, state_read_at=None):
        return "access", "refresh"


def _user() -> DevUser:
    return DevUser(
        user_id="u-1",
        username="u1",
        email="u1@example.com",
        display_name="U1",
        created_at=datetime.now(timezone.utc),
    )


async def _post(app: FastAPI, path: str, body: dict) -> httpx.Response:
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(path, json=body)


def _assert_store_unavailable(answer: httpx.Response) -> None:
    assert answer.status_code == 503, answer.text
    assert answer.json()["detail"] == "Session store temporarily unavailable"
    assert answer.headers["retry-after"] == "5"


@pytest.mark.parametrize(
    ("path", "found"),
    [
        ("/api/v1/auth/login", True),
        ("/api/v1/auth/dev-login", True),
        ("/api/v1/auth/register", False),
        ("/api/v1/auth/dev-register", False),
    ],
)
async def test_local_login_and_registration(sessions, path, found):
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api/v1")
    app.state.user_store = _Users(_user() if found else None)
    app.state.token_revocation_store = InMemoryRevocationStore()
    app.dependency_overrides[get_session_service] = lambda: sessions
    settings = SimpleNamespace(
        auth=SimpleNamespace(
            auth_mode=AuthMode.LOCAL, jwt_access_token_expire_minutes=15
        )
    )

    with (
        patch.object(auth_routes, "get_settings", return_value=settings),
        patch.object(auth_routes, "_build_local_jwt_generator", return_value=_Tokens()),
    ):
        answer = await _post(app, path, {"username": "operator"})

    _assert_store_unavailable(answer)


async def test_sso_exchange(sessions):
    async def exchange(code):
        await sessions.create_session(user_id="u-1")

    app = FastAPI()
    app.include_router(sso_routes.router, prefix="/api/v1")
    app.dependency_overrides[sso_routes.get_sso_login_service] = lambda: (
        SimpleNamespace(exchange=exchange)
    )
    app.dependency_overrides[require_sso_rate_limit_exchange] = lambda: None

    answer = await _post(app, "/api/v1/auth/sso/exchange", {"code": "c" * 32})

    _assert_store_unavailable(answer)
