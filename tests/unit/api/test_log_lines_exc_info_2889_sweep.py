"""The api area's arms that log an unexpected failure before a 5xx carry its
frames (#2889, the ``exc_info`` sweep).

Principles §7, "What a log line may hold", rule 4, as ruled on #2886: "A line
that logs an unexpected failure before the request is answered with a 5xx
carries that exception's frames (``exc_info``)." Stage S3 of #2886.

Each case drives one arm with :data:`MARKER` in the exception's message,
captures every record at DEBUG, and renders each through the real chain's
formatter (``build_log_formatter``). The marker is on no rendered line, the
arm's line names the class and carries its frames, and the status the caller
is answered is the one it was before the frames were added. The admin routes
are driven through their mounted router; the audit write and the user-store
dependency, which answer by raising, through the function that holds them.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import APIRouter, FastAPI, HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from faultmaven.api.exception_handlers import get_exception_handlers
from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.middleware.deduplication import DeduplicationMiddleware
from faultmaven.api.middleware.rate_limiting import RateLimitMiddleware
from faultmaven.api.operator_audit import record_operator_access
from faultmaven.api.operator_user_scope import get_operator_user_scope
from faultmaven.api.routes import admin_config, admin_usage
from faultmaven.api.routes.admin import get_user_service
from faultmaven.api.routes.admin import router as admin_router
from faultmaven.api.routes.admin_config import router as admin_config_router
from faultmaven.api.v1.auth_dependencies import get_user_store
from faultmaven.api.v1.dependencies import get_llm_provider
from faultmaven.config.protection import get_development_protection_settings
from faultmaven.infrastructure.persistence import database
from faultmaven.models.interfaces_operator_audit import OperatorAction
from faultmaven.models.protection import RateLimitConfig
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.knowledge.exceptions import (
    IngestOutcomeUnknown,
    ItemTitleScrubUnavailable,
)
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

OPERATOR = AuthenticatedUser(
    user_id="op-1",
    enterprise_id="ent-1",
    email="op@example.com",
    roles=["platform_admin"],
    permissions=[],
)


def _framed(rendered: list[str], event: str, cls: str) -> None:
    """The arm's line names ``cls`` and carries its frames; no line holds the
    marker."""
    line = _named(rendered, event, cls)
    assert line["frames"].startswith(f"{cls}:"), line


def _failing(method: str) -> MagicMock:
    """A collaborator whose ``method`` raises a store failure carrying the
    marker."""
    double = MagicMock()
    setattr(double, method, AsyncMock(side_effect=RuntimeError(f"pool {MARKER}")))
    return double


async def _call_admin(
    method: str, path: str, *, user_service=None, llm_provider=None, **kwargs
) -> httpx.Response:
    """Both admin routers, the app's exception handlers, the caller a platform
    administrator whose single-tenant scope admits every target."""
    app = FastAPI()
    app.include_router(admin_router)
    app.include_router(admin_config_router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    scope = SimpleNamespace(
        confined=False,
        listing_enterprise=lambda operator: None,
        admits=AsyncMock(return_value=True),
    )
    app.dependency_overrides[require_platform_admin] = lambda: OPERATOR
    app.dependency_overrides[get_operator_user_scope] = lambda: scope
    app.dependency_overrides[get_user_service] = lambda: user_service
    app.dependency_overrides[get_llm_provider] = lambda: llm_provider
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, **kwargs)


# ── api/routes/admin.py ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "service_method", "body", "event", "detail"),
    [
        pytest.param(
            "GET",
            "/api/v1/admin/users",
            "list_users",
            None,
            "Failed to list users",
            "Failed to list users",
            id="list-users",
        ),
        pytest.param(
            "GET",
            "/api/v1/admin/users/u-2",
            "get_user_with_metadata",
            None,
            "Failed to get user details",
            "Failed to get user details",
            id="get-user-details",
        ),
        pytest.param(
            "POST",
            "/api/v1/admin/users/u-2/deactivate",
            "deactivate_user_admin",
            None,
            "Failed to deactivate user",
            "Failed to deactivate user",
            id="deactivate-user",
        ),
        pytest.param(
            "POST",
            "/api/v1/admin/users/u-2/activate",
            "activate_user_admin",
            None,
            "Failed to activate user",
            "Failed to activate user",
            id="activate-user",
        ),
        pytest.param(
            "POST",
            "/api/v1/admin/users/u-2/roles",
            "assign_role",
            {"role": "member"},
            "Failed to assign role",
            "Failed to assign role",
            id="assign-role",
        ),
        pytest.param(
            "DELETE",
            "/api/v1/admin/users/u-2/roles/member",
            "remove_role",
            None,
            "Failed to remove role",
            "Failed to remove role",
            id="remove-role",
        ),
    ],
)
async def test_a_failed_user_administration_carries_its_frames(
    lines, method, path, service_method, body, event, detail
):
    """Each user-administration route with a user service whose call raises:
    the catch-all's static 500, and its line with the frames."""
    answer = await _call_admin(
        method, path, user_service=_failing(service_method), json=body
    )

    assert answer.status_code == 500
    assert answer.json()["detail"] == detail
    _framed(lines(), event, "RuntimeError")


async def test_a_failed_llm_routing_read_carries_its_frames(lines):
    """``GET /admin/debug/llm-routing`` with a provider whose status read
    raises: the static 500, and the line with its frames."""
    provider = MagicMock()
    provider.get_provider_status.side_effect = RuntimeError(f"registry {MARKER}")

    answer = await _call_admin(
        "GET", "/api/v1/admin/debug/llm-routing", llm_provider=provider
    )

    assert answer.status_code == 500
    assert answer.json()["detail"] == "Failed to get LLM routing health"
    _framed(lines(), "Failed to get LLM routing health", "RuntimeError")


# ── api/routes/admin_config.py ────────────────────────────────────────────


async def test_a_failed_llm_config_read_carries_its_frames(lines):
    """``GET /admin/llm/config`` with a registry whose status read raises: the
    static 500, and the line with its frames."""
    provider = MagicMock()
    provider.registry.get_provider_status.side_effect = RuntimeError(
        f"registry {MARKER}"
    )

    answer = await _call_admin("GET", "/api/v1/admin/llm/config", llm_provider=provider)

    assert answer.status_code == 500
    assert answer.json()["detail"] == "Failed to get LLM configuration"
    _framed(lines(), "Failed to get LLM config", "RuntimeError")


async def test_a_failed_llm_config_update_carries_its_frames(lines):
    """``PUT /admin/llm/config`` on a cloud deployment whose registry raises:
    the static 500, and the line with its frames."""
    provider = MagicMock()
    provider.registry.get_all_provider_names.side_effect = RuntimeError(
        f"registry {MARKER}"
    )

    with patch(
        "faultmaven.config.settings.get_settings",
        return_value=SimpleNamespace(is_cloud=True),
    ):
        answer = await _call_admin(
            "PUT",
            "/api/v1/admin/llm/config",
            llm_provider=provider,
            json={"primary_provider": "openai"},
        )

    assert answer.status_code == 500
    assert answer.json()["detail"] == "Failed to update LLM configuration"
    _framed(lines(), "Failed to update LLM config", "RuntimeError")


async def test_a_failed_config_status_read_carries_its_frames(lines):
    """``GET /admin/config/status`` whose limiter read raises: the static 500,
    and the line with its frames."""
    with patch.object(
        admin_config,
        "_installed_bypass_headers",
        side_effect=RuntimeError(f"middleware {MARKER}"),
    ):
        answer = await _call_admin("GET", "/api/v1/admin/config/status")

    assert answer.status_code == 500
    assert answer.json()["detail"] == "Failed to get configuration status"
    _framed(lines(), "Failed to get env config status", "RuntimeError")


# ── api/operator_audit.py ─────────────────────────────────────────────────


async def test_a_failed_audit_write_carries_its_frames(lines):
    """``record_operator_access`` with a repository whose write raises: the
    503 refusal, and the line with its frames."""
    repo = MagicMock(record_access=AsyncMock(side_effect=RuntimeError(MARKER)))

    with pytest.raises(HTTPException) as refused:
        await record_operator_access(
            repo, OPERATOR, OperatorAction.LIST, deployment_mode="standalone"
        )

    assert refused.value.status_code == 503
    _framed(lines(), "operator_access_audit_write_failed", "RuntimeError")


# ── api/v1/auth_dependencies.py ───────────────────────────────────────────


class _Request:
    """A request whose ``app`` cannot be read: reading it raises ``error``."""

    def __init__(self, error: Exception):
        self._error = error

    @property
    def app(self):
        raise self._error


@pytest.mark.parametrize(
    ("error", "event", "detail"),
    [
        pytest.param(
            AttributeError(f"state {MARKER}"),
            "User store attribute not found in app.state",
            "User management service not initialized. Please check server "
            "startup logs.",
            id="attribute-error",
        ),
        pytest.param(
            RuntimeError(f"state {MARKER}"),
            "Failed to get user store",
            "User management service error",
            id="catch-all",
        ),
    ],
)
async def test_an_unreadable_user_store_carries_its_frames(lines, error, event, detail):
    """``get_user_store`` whose read of ``app.state`` raises: the 503, and the
    arm's line with its frames."""
    with pytest.raises(HTTPException) as refused:
        await get_user_store(_Request(error))

    assert refused.value.status_code == 503
    assert refused.value.detail == detail
    _framed(lines(), event, type(error).__name__)


def _container_unreadable():
    """A container whose user-store read raises."""
    container = MagicMock()
    container.get_user_store.side_effect = RuntimeError(f"container {MARKER}")
    return container


async def test_an_unreadable_container_carries_its_frames(lines):
    """``get_user_store`` with no store on ``app.state`` and a container whose
    read raises: the 503, and the container check's line with its frames."""
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    with patch("faultmaven.container.container", _container_unreadable()):
        with pytest.raises(HTTPException) as refused:
            await get_user_store(request)

    assert refused.value.status_code == 503
    _framed(lines(), "Failed to check container", "RuntimeError")


# ── api/middleware/rate_limiting.py, api/middleware/deduplication.py ──────


def _fail_closed_settings():
    settings = get_development_protection_settings()
    settings.fail_open_on_redis_error = False
    settings.deduplication_enabled = True
    settings.rate_limits = {
        "global": RateLimitConfig(enabled=True, requests=5, window=60)
    }
    return settings


def _http_request(path: str = "/api/v1/cases") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "root_path": "",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("10.4.9.2", 1234),
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
    )


async def _served(request):
    return PlainTextResponse("served")


async def _asgi(scope, receive, send):  # pragma: no cover - never reached
    raise AssertionError("the route ran")


async def test_a_failed_rate_limit_check_fails_closed_with_its_frames(lines):
    """``RateLimitMiddleware.dispatch`` under ``fail_open_on_redis_error=False``
    whose check raises: the 503, and the line with its frames."""
    middleware = RateLimitMiddleware(app=_asgi, settings=_fail_closed_settings())
    middleware._initialize = AsyncMock(return_value=True)
    middleware._check_rate_limits = AsyncMock(
        side_effect=ConnectionError(f"redis {MARKER}")
    )

    answer = await middleware.dispatch(_http_request(), _served)

    assert answer.status_code == 503
    _framed(lines(), "Rate limiting error", "ConnectionError")


async def test_a_failed_deduplication_check_fails_closed_with_its_frames(lines):
    """``DeduplicationMiddleware.dispatch`` under
    ``fail_open_on_redis_error=False`` whose claim raises: the 503, and the
    line with its frames."""
    middleware = DeduplicationMiddleware(app=_asgi, settings=_fail_closed_settings())
    middleware._initialized = True
    middleware._should_skip = lambda request: False
    middleware._claim_for = AsyncMock(side_effect=ConnectionError(f"redis {MARKER}"))

    answer = await middleware.dispatch(_http_request(), _served)

    assert answer.status_code == 503
    _framed(lines(), "Deduplication error", "ConnectionError")


# ── api/exception_handlers.py ─────────────────────────────────────────────


async def _raised_through_the_app(error: Exception) -> httpx.Response:
    """A route that raises ``error``, answered by the app's handlers."""
    raising = APIRouter()

    @raising.post("/raise")
    async def _raise():
        raise error

    app = FastAPI()
    app.include_router(raising)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.post("/raise")


def _from(error: Exception, cause: Exception) -> Exception:
    """``error``, raised from ``cause``, with both tracebacks set."""
    try:
        try:
            raise cause
        except Exception as inner:
            raise error from inner
    except Exception as outer:
        return outer


async def test_an_unavailable_title_scrub_carries_its_frames(lines):
    """An uncaught ``ItemTitleScrubUnavailable`` raised from a sanitizer
    failure: the handler's 503, and its line with the frames of both links."""
    error = _from(
        ItemTitleScrubUnavailable("check could not run (ConnectionError)"),
        ConnectionError(f"presidio {MARKER}"),
    )

    answer = await _raised_through_the_app(error)

    assert answer.status_code == 503
    _framed(lines(), "Title scrub unavailable", "ItemTitleScrubUnavailable")
    line = _named(lines(), "Title scrub unavailable", "ConnectionError")
    assert "ConnectionError:" in line["frames"]


async def test_an_unknown_ingest_outcome_carries_its_frames(lines):
    """An ``IngestOutcomeUnknown`` raised from a failed COMMIT: the handler's
    500 ``KB_INGEST_OUTCOME_UNKNOWN``, and its line with the frames of both
    links."""
    error = _from(IngestOutcomeUnknown("kb-1"), ConnectionError(f"commit {MARKER}"))

    answer = await _raised_through_the_app(error)

    assert answer.status_code == 500
    assert answer.headers["x-error-code"] == "KB_INGEST_OUTCOME_UNKNOWN"
    _framed(lines(), "Authored ingest outcome unknown", "IngestOutcomeUnknown")
    line = _named(lines(), "Authored ingest outcome unknown", "ConnectionError")
    assert "ConnectionError:" in line["frames"]


# ── infrastructure/llm/usage_report.py ────────────────────────────────────


async def test_an_unreadable_usage_store_carries_its_frames(lines, monkeypatch):
    """``GET /admin/llm/usage`` whose database session cannot be opened: the
    route's 503 ``DATABASE_UNAVAILABLE``, and the report's line with the
    frames."""

    @asynccontextmanager
    async def _refused():
        raise ConnectionRefusedError(111, f"connect to {MARKER}")
        yield  # pragma: no cover

    monkeypatch.setattr(database, "get_db_session", _refused)
    monkeypatch.setattr(admin_usage, "get_current_enterprise_id", lambda: "ent-1")
    app = FastAPI()
    app.include_router(admin_usage.router)
    for exc_type, handler in get_exception_handlers().items():
        app.add_exception_handler(exc_type, handler)
    app.dependency_overrides[require_platform_admin] = lambda: OPERATOR
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        answer = await c.get("/api/v1/admin/llm/usage")

    assert answer.status_code == 503
    assert answer.headers["x-error-code"] == "DATABASE_UNAVAILABLE"
    _framed(lines(), "llm_usage_report_unavailable", "ConnectionRefusedError")
