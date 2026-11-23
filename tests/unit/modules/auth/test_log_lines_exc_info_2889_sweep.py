"""The auth area's arms that log an unexpected failure before a 5xx carry its
frames (#2889, the ``exc_info`` sweep).

Principles §7, "What a log line may hold", rule 4, as ruled on #2886: "A line
that logs an unexpected failure before the request is answered with a 5xx
carries that exception's frames (``exc_info``)." Stage S3 of #2886.

Each case drives one arm through its mounted route with :data:`MARKER` in the
exception's message, captures every record at DEBUG, and renders each through
the real chain's formatter (``build_log_formatter``). The marker is on no
rendered line, the arm's line names the class and carries its frames, and the
status the caller is answered is the one it was before the frames were added.
The user stores' delete arms (#2886) are driven through the store: the route
answers their ``False`` with a 500, pinned by
``test_delete_user_refuses_case_creator_1923.py``.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.v1.auth_dependencies import (
    get_current_user_optional,
    require_authentication,
)
from faultmaven.api.v1.dependencies import get_session_service
from faultmaven.infrastructure.auth.database_user_store import DatabaseUserStore
from faultmaven.infrastructure.auth.user_store import RedisUserStore
from faultmaven.modules.auth.api.oauth import get_oauth_service
from faultmaven.modules.auth.api.oauth import router as oauth_router
from faultmaven.modules.auth.api.rate_limiting import (
    require_oauth_rate_limit_authorize,
    require_oauth_rate_limit_revoke,
)
from faultmaven.modules.auth.api.session import router as session_router
from faultmaven.modules.auth.domain.models.auth import DevUser
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)
from tests.unit.modules.auth.test_revocation_fails_closed import (
    _auth_service,
    _RaisingStore,
    _token,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _framed(rendered: list[str], event: str, cls: str) -> None:
    """The arm's line names ``cls`` and carries its frames; no line holds the
    marker."""
    line = _named(rendered, event, cls)
    assert line["frames"].startswith(f"{cls}:"), line


def _user() -> DevUser:
    return DevUser(
        user_id="u1",
        username="u1",
        email="u1@example.com",
        display_name="U1",
        created_at=datetime.now(timezone.utc),
    )


async def _call(app: FastAPI, method: str, path: str, **kwargs) -> httpx.Response:
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, **kwargs)


# ── modules/auth/api/oauth.py ─────────────────────────────────────────────


def _oauth_app(oauth_service) -> FastAPI:
    app = FastAPI()
    app.include_router(oauth_router, prefix="/api/v1")
    app.dependency_overrides[require_authentication] = _user
    app.dependency_overrides[get_oauth_service] = lambda: oauth_service
    app.dependency_overrides[require_oauth_rate_limit_authorize] = lambda: None
    app.dependency_overrides[require_oauth_rate_limit_revoke] = lambda: None
    return app


_AUTHORIZE = {
    "client_id": "faultmaven-copilot",
    "redirect_uri": "chrome-extension://abc/callback.html",
    "state": "s-1",
    "code_challenge": "c" * 43,
    "code_challenge_method": "S256",
    "scope": "openid profile email",
}


async def test_a_failed_authorization_request_carries_its_frames(lines):
    """``GET /auth/oauth/authorize`` whose client validation raises: the static
    500, and the catch-all's line with its frames."""
    service = MagicMock()
    service.validate_authorization_request = AsyncMock(
        side_effect=RuntimeError(f"code store {MARKER}")
    )

    answer = await _call(
        _oauth_app(service),
        "GET",
        "/api/v1/auth/oauth/authorize",
        params={"response_type": "code", **_AUTHORIZE},
    )

    assert answer.status_code == 500
    assert answer.json()["detail"] == "Internal server error"
    _framed(lines(), "Authorization error", "RuntimeError")


async def test_a_failed_authorization_approval_carries_its_frames(lines):
    """``POST /auth/oauth/authorize`` whose code mint raises: the static 500,
    and the catch-all's line with its frames."""
    service = MagicMock()
    service.create_authorization_code = AsyncMock(
        side_effect=RuntimeError(f"code store {MARKER}")
    )

    answer = await _call(
        _oauth_app(service),
        "POST",
        "/api/v1/auth/oauth/authorize",
        json={"approved": True, **_AUTHORIZE},
    )

    assert answer.status_code == 500
    assert answer.json()["detail"] == "Internal server error"
    _framed(lines(), "Authorization approval error", "RuntimeError")


async def test_a_revocation_that_was_not_recorded_carries_its_frames(lines):
    """``POST /auth/oauth/revoke`` whose revocation write raises: RFC 7009's
    503 ``temporarily_unavailable``, and the catch-all's line with its
    frames."""
    service = MagicMock()
    service.revoke_token = AsyncMock(side_effect=ConnectionError(f"redis {MARKER}"))

    answer = await _call(
        _oauth_app(service),
        "POST",
        "/api/v1/auth/oauth/revoke",
        data={"token": "t-1", "client_id": "faultmaven-copilot"},
    )

    assert answer.status_code == 503
    assert answer.json()["error"] == "temporarily_unavailable"
    _framed(lines(), "Token revocation error", "ConnectionError")


# ── modules/auth/domain/services/auth_service.py ──────────────────────────


async def test_an_unknown_revocation_state_carries_its_frames(lines):
    """A bearer token checked against a revocation store whose read raises,
    through ``get_current_user_optional``: the 503 the caller gets, and the
    service's line with the store failure's frames."""
    service = _auth_service(_RaisingStore(ConnectionError(f"store down {MARKER}")))

    with pytest.raises(HTTPException) as refused:
        await get_current_user_optional(MagicMock(), _token(service), service)

    assert refused.value.status_code == 503
    _framed(
        lines(), "Revocation state unknown, refusing the request", "ConnectionError"
    )


# ── modules/auth/api/session.py ───────────────────────────────────────────


class _Unanswerable:
    """An activity result whose truth test raises: the heartbeat's outer
    catch-all is the arm that catches it."""

    def __bool__(self) -> bool:
        raise RuntimeError(f"result {MARKER}")


@pytest.mark.parametrize(
    ("update", "event", "cls", "status", "detail"),
    [
        pytest.param(
            AsyncMock(side_effect=RuntimeError(f"Session store unavailable {MARKER}")),
            "Session store connection issue during heartbeat for s-1",
            "RuntimeError",
            503,
            "Session store temporarily unavailable",
            id="store-unavailable",
        ),
        pytest.param(
            AsyncMock(
                side_effect=RuntimeError(f"Activity update operation failed {MARKER}")
            ),
            "Session activity update failed for s-1",
            "RuntimeError",
            500,
            "Failed to update session activity",
            id="activity-update-failed",
        ),
        pytest.param(
            AsyncMock(side_effect=RuntimeError(f"pool {MARKER}")),
            "Unexpected runtime error during heartbeat for s-1",
            "RuntimeError",
            500,
            "Internal server error",
            id="unexpected-runtime-error",
        ),
        pytest.param(
            AsyncMock(side_effect=ConnectionError(f"redis {MARKER}")),
            "Unexpected error during heartbeat for s-1",
            "ConnectionError",
            500,
            "Internal server error",
            id="inner-catch-all",
        ),
        pytest.param(
            AsyncMock(return_value=_Unanswerable()),
            "Unexpected error in heartbeat for session s-1",
            "RuntimeError",
            500,
            "Internal server error during heartbeat operation",
            id="outer-catch-all",
        ),
    ],
)
async def test_a_failed_heartbeat_carries_its_frames(
    lines, update, event, cls, status, detail
):
    """``POST /sessions/{id}/heartbeat`` whose activity update fails: each arm's
    status and detail, and its line with the frames."""
    service = MagicMock()
    service.update_last_activity = update
    app = FastAPI()
    app.include_router(session_router, prefix="/api/v1")
    app.dependency_overrides[get_session_service] = lambda: service

    answer = await _call(app, "POST", "/api/v1/sessions/s-1/heartbeat")

    assert answer.status_code == status
    assert answer.json()["detail"] == detail
    _framed(lines(), event, cls)


# ── infrastructure/auth/{database_user_store,user_store}.py (#2886) ─────────


async def test_a_failed_database_delete_carries_its_frames(lines):
    """``DatabaseUserStore.delete_user`` whose repository delete raises: still
    ``False``, and the arm's line with its frames."""
    repository = MagicMock()
    repository.delete = AsyncMock(side_effect=RuntimeError(f"db down {MARKER}"))

    assert await DatabaseUserStore(repository).delete_user("u1") is False
    _framed(lines(), "Failed to delete user u1", "RuntimeError")


async def test_a_failed_redis_delete_carries_its_frames(lines):
    """``RedisUserStore.delete_user`` whose user read cannot reach Redis: still
    ``False``, and the arm's line with the lookup failure's frames."""
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=ConnectionError(f"redis {MARKER}"))

    assert await RedisUserStore(redis).delete_user("u1") is False
    _framed(lines(), "Failed to delete user u1", "UserLookupFailed")
