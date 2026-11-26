"""``POST /sessions/{session_id}/heartbeat`` answers a session store it cannot
use with 503, chosen by the exception's type (#2985).

Its 503 arm matched ``"Session store unavailable"`` in a ``RuntimeError``'s
text, which nothing raises, while the store reported a Redis failure as a
builtin ``ConnectionError``: the heartbeat answered 500. The outage is driven
end to end, through ``AuthSessionService`` and a real ``RedisSessionStore``
over a FakeRedis server emulating a lost connection.
"""

from unittest.mock import AsyncMock, MagicMock

import fakeredis
import fakeredis.aioredis as fakeredis_aio
import httpx
import pytest
from fastapi import FastAPI

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.exceptions import SessionWriteConflict
from faultmaven.modules.auth.api.session import router as session_router
from faultmaven.modules.auth.domain.services.auth_session_service import (
    AuthSessionService,
)
from faultmaven.modules.auth.infrastructure.stores.redis_session_store import (
    RedisSessionStore,
)

pytestmark = [pytest.mark.unit]


async def _heartbeat(service, session_id: str = "s-1") -> httpx.Response:
    app = FastAPI()
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.include_router(session_router, prefix="/api/v1")
    app.dependency_overrides[get_session_service] = lambda: service
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post(f"/api/v1/sessions/{session_id}/heartbeat")


def _service(update: AsyncMock) -> MagicMock:
    service = MagicMock()
    service.update_last_activity = update
    return service


async def test_an_unreachable_store_is_a_retryable_503():
    server = fakeredis.FakeServer()
    server.connected = False
    redis = fakeredis_aio.FakeRedis(server=server, decode_responses=True)
    try:
        service = AuthSessionService(
            session_store=RedisSessionStore(redis, session_ttl_seconds=60)
        )
        answer = await _heartbeat(service)
    finally:
        await redis.aclose()

    assert answer.status_code == 503, answer.text
    assert answer.json()["detail"] == "Session store temporarily unavailable"
    assert answer.headers["retry-after"] == "5"


async def test_an_unconfigured_store_is_a_503_with_no_retry_after():
    """``ServiceException("Session store not configured")``: a retry cannot
    change it, so no ``Retry-After``."""
    answer = await _heartbeat(AuthSessionService(session_store=None))

    assert answer.status_code == 503, answer.text
    assert answer.json()["detail"] == "Session service unavailable"
    assert "retry-after" not in answer.headers


@pytest.mark.parametrize(
    ("update", "status", "retry_after"),
    [
        pytest.param(
            AsyncMock(side_effect=SessionWriteConflict("kept changing")),
            503,
            "1",
            id="write-conflict",
        ),
        pytest.param(AsyncMock(return_value=None), 404, None, id="no-session"),
        pytest.param(
            AsyncMock(side_effect=RuntimeError("a bug")), 500, None, id="a-bug"
        ),
    ],
)
async def test_the_other_answers_are_unchanged(update, status, retry_after):
    answer = await _heartbeat(_service(update))

    assert answer.status_code == status, answer.text
    assert answer.headers.get("retry-after") == retry_after


async def test_a_blank_session_id_is_a_400():
    update = AsyncMock()
    answer = await _heartbeat(_service(update), session_id="%20")

    assert answer.status_code == 400, answer.text
    update.assert_not_awaited()
