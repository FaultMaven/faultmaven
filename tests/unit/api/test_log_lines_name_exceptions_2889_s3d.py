"""The API's log lines name an exception by its class and code, never its text
(#2889 S3d).

Principles §7, "What a log line may hold", rule 1: "A message that interpolates
an exception's text, a payload, or a user-supplied value is not metadata. A
first-party exception contributes its class and its ``error_code`` ..., not its
message." Stage S3 of #2886.

Each case drives one site with :data:`MARKER` in the exception's message,
captures every record at DEBUG, and renders each through the real chain's
formatter (``build_log_formatter``). The marker is on no rendered line, and the
class name (with its code, where first-party) is on the site's line. How each
site is driven is stated on its case.
"""

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from starlette.requests import Request

from faultmaven.api.exception_handlers import service_error_handler
from faultmaven.api.middleware import route_policy
from faultmaven.api.middleware.idempotency import IdempotencyMiddleware
from faultmaven.api.middleware.logging import LoggingMiddleware
from faultmaven.api.operator_audit import record_operator_access
from faultmaven.api.v1.auth_dependencies import get_current_user_optional
from faultmaven.exceptions import ServiceError
from faultmaven.infrastructure.health.component_monitor import component_monitor
from faultmaven.infrastructure.protection.rate_limiter import (
    CHECK_FAILURE_DEMOTION_THRESHOLD,
    RedisRateLimiter,
)
from faultmaven.models.interfaces_operator_audit import OperatorAction
from faultmaven.models.protection import LimitType, RateLimitConfig, RateLimitSpec
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.auth.domain.services.auth_service import (
    AuthenticationError,
    RevocationStateUnknownError,
)
from tests.unit.api.middleware.test_rate_limit_fail_closed_integrity import (
    _FailingClient,
)
from tests.unit.infrastructure.llm.test_log_lines_name_exceptions_2889 import (
    MARKER,
    _named,
    lines,  # noqa: F401 - fixture
)

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _request(path: str = "/api/v1/x", method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
        }
    )


# ── api/middleware/logging.py (pin 2) ─────────────────────────────────────


def test_a_failed_request_is_named_by_class(lines):
    """Driven through the middleware: a route raises."""
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/boom")
    async def boom():
        raise RuntimeError(f"kaboom {MARKER}")

    with (
        patch("faultmaven.api.middleware.logging.request_counter"),
        patch("faultmaven.api.middleware.logging.request_duration"),
        patch("faultmaven.api.middleware.logging.sla_tracker"),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        assert client.get("/boom").status_code == 500
    failed = _named(lines(), "Request failed", "RuntimeError")
    assert failed["error"] == "RuntimeError"
    assert failed["error_type"] == "RuntimeError"


# ── api/exception_handlers.py (pin 2) ─────────────────────────────────────


async def test_a_service_error_is_named_by_class(lines):
    """Driven through the handler, called with the error it is registered
    for."""
    exc = ServiceError(f"db said {MARKER}")
    response = await service_error_handler(_request(), exc)
    assert response.status_code == 500
    _named(lines(), "Service error", "Service error: GET /api/v1/x - ServiceError")


# ── api/v1/auth_dependencies.py ───────────────────────────────────────────


def _verifier(error: BaseException) -> MagicMock:
    return MagicMock(verify_token_with_revocation_check=AsyncMock(side_effect=error))


async def test_a_refused_token_is_named_by_class_and_code(lines):
    """Driven through the dependency, its verifier stubbed to raise."""
    error = AuthenticationError(f"expired for {MARKER}", "TOKEN_EXPIRED")
    user = await get_current_user_optional(MagicMock(), "t", _verifier(error))
    assert user is None
    _named(lines(), "JWT validation failed", "AuthenticationError[TOKEN_EXPIRED]")


async def test_an_unknown_revocation_state_names_both_classes(lines):
    """Driven through the dependency, its verifier stubbed to raise as
    ``auth_service.py`` does (``kind`` the class name, raised ``from`` the
    store's error). The line held ``kind`` alone, a class name, so on the base
    this case fails on the chain, not on the marker."""
    error = RevocationStateUnknownError(kind="OperationalError")
    error.__cause__ = sqlite3.OperationalError(f"database is locked {MARKER}")
    with pytest.raises(HTTPException):
        await get_current_user_optional(MagicMock(), "t", _verifier(error))
    _named(
        lines(),
        "Revocation state unknown",
        "RevocationStateUnknownError[REVOCATION_STATE_UNKNOWN] <- OperationalError",
    )


# ── infrastructure/protection/rate_limiter.py ─────────────────────────────


async def test_a_check_failing_past_the_threshold_is_named_by_class(lines):
    """Driven through ``check_rate_limits``, on a client whose script
    raises."""
    limiter = RedisRateLimiter(fallback_enabled=False)
    boom = ConnectionError(f"connection reset by {MARKER}")
    await limiter._adopt(_FailingClient(boom), owns=False, degraded=False)
    limiter.configure_limits(
        {LimitType.GLOBAL.value: RateLimitConfig(enabled=True, requests=5, window=60)}
    )
    spec = RateLimitSpec(key="10.4.9.1", limit_type=LimitType.GLOBAL)
    for _ in range(CHECK_FAILURE_DEMOTION_THRESHOLD):
        with pytest.raises(ConnectionError):
            await limiter.check_rate_limits([spec])
    _named(
        lines(),
        f"Rate limit check failed ({CHECK_FAILURE_DEMOTION_THRESHOLD} consecutive)",
        "ConnectionError",
    )


# ── api/middleware/route_policy.py ────────────────────────────────────────


class _Unreadable(dict):
    def items(self):
        raise RuntimeError(f"declaration {MARKER}")


def test_an_unreadable_route_policy_is_named_by_class(lines, monkeypatch):
    """``_read_policy_map``'s unreadable-declaration line, which reaches the log
    through ``_report_once``. Driven through ``_read_policy_map`` with a mapping
    whose ``items()`` raises."""
    monkeypatch.setattr(route_policy, "_REPORTED_BAD_DECLARATIONS", set())
    assert not route_policy._read_policy_map(_Unreadable())
    _named(lines(), "could not be read (RuntimeError)", "RuntimeError")


# ── api/middleware/idempotency.py ─────────────────────────────────────────


async def test_an_idempotency_failure_is_named_by_class(lines):
    """Driven through the middleware: resolving its Redis client raises before
    the route runs, so the request degrades to an uncached one."""
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)

    @app.post("/api/v1/cases")
    async def cases():
        return {"ok": True}

    with patch.object(
        IdempotencyMiddleware,
        "_ensure_redis",
        side_effect=RuntimeError(f"redis {MARKER}"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/cases",
                json={},
                headers={
                    "Idempotency-Key": "11111111-2222-3333-4444-555555555555",
                    "Authorization": "Bearer caller-token-aaaaaaaaaaaaaaaa",
                },
            )
    assert response.status_code == 200
    _named(lines(), "Error in idempotency middleware", "RuntimeError")


# ── main.py ───────────────────────────────────────────────────────────────


async def test_a_readiness_probe_that_cannot_run_is_named_by_class(lines):
    """``main.py``'s readiness site (``readiness``, the ``Readiness probe
    failed`` line), driven through the route function, the monitor stubbed to
    raise."""
    from faultmaven.main import readiness

    with patch.object(
        component_monitor,
        "check_serving_readiness",
        AsyncMock(side_effect=RuntimeError(f"probe {MARKER}")),
    ):
        body = await readiness(Response())
    assert body["reason"] == "readiness_check_unavailable"
    _named(lines(), "Readiness probe failed", "RuntimeError")


# ── api/operator_audit.py ─────────────────────────────────────────────────


async def test_a_failed_audit_write_is_named_by_class(lines):
    """Driven through ``record_operator_access``, its repository stubbed to
    raise. The ``error`` extra holds the class, not the text."""
    repo = MagicMock(record_access=AsyncMock(side_effect=RuntimeError(MARKER)))
    operator = AuthenticatedUser(
        user_id="op-1",
        enterprise_id="ent-1",
        email="op@example.com",
        roles=["platform_admin"],
        permissions=[],
    )
    with pytest.raises(HTTPException):
        await record_operator_access(
            repo, operator, OperatorAction.LIST, deployment_mode="standalone"
        )
    failed = _named(lines(), "operator_access_audit_write_failed", "RuntimeError")
    assert failed["error"] == "RuntimeError"
