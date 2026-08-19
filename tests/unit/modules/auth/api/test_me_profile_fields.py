"""`GET /auth/me` reports the stored account's timestamps, not the token's.

The JWT-derived ``DevUser`` synthesizes ``created_at`` at authentication time
and carries no last-login at all, so building the profile from it fabricated
both fields (#1120): ``created_at`` changed on every request and ``last_login``
was hardcoded null while the row's ``last_login_at`` sat populated. These pin
the corrected sourcing — the persisted user row — and, because the fields are
display-only, the degrade paths: a missing row, an unwired service, or an
unreadable store must fall back to the principal's view rather than 500 a
profile request.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.infrastructure.persistence.user_repository import User
from faultmaven.modules.auth.api.auth import (
    _resolve_profile_timestamps,
    get_current_user_profile,
    router,
)
from faultmaven.modules.auth.domain.models.auth import DevUser

pytestmark = [pytest.mark.unit]

_USER_ID = "550e8400-e29b-41d4-a716-446655440000"

#: The stored account predates the request by months — any drift toward
#: "now" in the reported value is the #1120 fabrication reappearing.
_ROW_CREATED = datetime(2026, 2, 14, 22, 45, 53, tzinfo=timezone.utc)
_ROW_LAST_LOGIN = datetime(2026, 8, 17, 21, 4, 31, tzinfo=timezone.utc)


def _principal(created_at: datetime | None = None) -> DevUser:
    """The token principal as ``require_authentication`` builds it: its
    ``created_at`` is the moment of authentication, not the account's."""
    return DevUser(
        user_id=_USER_ID,
        username="faultmavenuserguest",
        email="guest@faultmaven.example",
        display_name="faultmavenuserguest",
        created_at=created_at or datetime.now(timezone.utc),
        roles=["user"],
    )


def _row(last_login_at: datetime | None) -> User:
    """A real repository ``User`` — the model the service actually returns,
    so field renames there break these tests instead of sliding past a Mock."""
    return User(
        user_id=_USER_ID,
        username="faultmavenuserguest",
        email="guest@faultmaven.example",
        display_name="faultmavenuserguest",
        created_at=_ROW_CREATED,
        updated_at=_ROW_CREATED,
        last_login_at=last_login_at,
        roles=["user"],
    )


class _UserService:
    """Stand-in for UserService.get_user: returns the given row, or raises."""

    def __init__(self, row: User | None = None, error: Exception | None = None):
        self._row = row
        self._error = error

    async def get_user(self, user_id: str) -> User | None:
        if self._error is not None:
            raise self._error
        # The row is only *this* account's row.
        return self._row if self._row and self._row.user_id == user_id else None


class TestResolveProfileTimestamps:
    async def test_created_at_is_the_rows_not_the_requests(self):
        """The value must be the stored row's, byte for byte — a value derived
        from the principal would move with the clock."""
        request_time = datetime(2026, 8, 19, 21, 4, 10, tzinfo=timezone.utc)

        created_at, _ = await _resolve_profile_timestamps(
            _principal(created_at=request_time), _UserService(_row(None))
        )

        assert created_at == "2026-02-14T22:45:53Z"
        assert created_at != "2026-08-19T21:04:10Z"

    async def test_last_login_reports_the_stored_value(self):
        _, last_login = await _resolve_profile_timestamps(
            _principal(), _UserService(_row(_ROW_LAST_LOGIN))
        )

        assert last_login == "2026-08-17T21:04:31Z"

    async def test_a_never_logged_in_row_reports_null_faithfully(self):
        """Null from a found row is the row's truth, not a fallback — local
        passwordless login never stamps ``last_login_at``."""
        created_at, last_login = await _resolve_profile_timestamps(
            _principal(), _UserService(_row(None))
        )

        assert last_login is None
        # The row was found: created_at is still the stored one.
        assert created_at == "2026-02-14T22:45:53Z"

    async def test_missing_row_degrades_to_the_principals_view(self):
        """A principal with no persisted row (dev tokens under local mode)
        keeps a working profile: the synthesized created_at, null last_login."""
        request_time = datetime(2026, 8, 19, 21, 4, 10, tzinfo=timezone.utc)

        created_at, last_login = await _resolve_profile_timestamps(
            _principal(created_at=request_time), _UserService(row=None)
        )

        assert created_at == "2026-08-19T21:04:10Z"
        assert last_login is None

    async def test_an_unwired_service_degrades_too(self):
        """``get_user_service`` returns None rather than 503 — a profile is
        worth returning without its persisted timestamps."""
        request_time = datetime(2026, 8, 19, 21, 4, 10, tzinfo=timezone.utc)

        created_at, last_login = await _resolve_profile_timestamps(
            _principal(created_at=request_time), None
        )

        assert created_at == "2026-08-19T21:04:10Z"
        assert last_login is None

    async def test_an_unreadable_store_degrades_instead_of_raising(self):
        """Raising would turn a store blip into a failed `/auth/me`, and a
        client that reads that as "not authenticated" signs the user out over
        two display fields."""
        request_time = datetime(2026, 8, 19, 21, 4, 10, tzinfo=timezone.utc)

        created_at, last_login = await _resolve_profile_timestamps(
            _principal(created_at=request_time),
            _UserService(error=RuntimeError("db down")),
        )

        assert created_at == "2026-08-19T21:04:10Z"
        assert last_login is None


class TestEndpointWiring:
    """The handler must actually consult the helper — a correct helper left
    unwired is exactly how the original hardcoded ``last_login=None`` read."""

    async def test_response_carries_the_rows_timestamps(self):
        response = await get_current_user_profile(
            current_user=_principal(),
            organization_repository=None,
            user_service=_UserService(_row(_ROW_LAST_LOGIN)),
        )

        assert response.created_at == "2026-02-14T22:45:53Z"
        assert response.last_login == "2026-08-17T21:04:31Z"

    async def test_response_survives_a_missing_row(self):
        request_time = datetime(2026, 8, 19, 21, 4, 10, tzinfo=timezone.utc)

        response = await get_current_user_profile(
            current_user=_principal(created_at=request_time),
            organization_repository=None,
            user_service=_UserService(row=None),
        )

        assert response.created_at == "2026-08-19T21:04:10Z"
        assert response.last_login is None
        # Identity fields still come from the authenticated principal.
        assert response.user_id == _USER_ID
        assert response.username == "faultmavenuserguest"


class TestRealDependencyChain:
    """The stored timestamps must arrive through ``Depends(get_user_service)``
    reading ``app.state.user_service`` — not through kwargs handed to the
    handler. The direct-call tests above would stay green if that attribute
    were renamed or the dependency mistyped, because the ``user_service is
    None`` branch degrades; this one goes through the real wire, so breaking
    the wire breaks the test."""

    def test_stored_timestamps_flow_through_app_state(self):
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        # Only authentication is overridden — token minting is not the wiring
        # under test. get_user_service is deliberately NOT overridden: it must
        # find the service on app.state itself, exactly as main.py wires it.
        principal = _principal(
            created_at=datetime(2026, 8, 19, 21, 4, 10, tzinfo=timezone.utc)
        )
        app.dependency_overrides[require_authentication] = lambda: principal
        app.state.user_service = _UserService(_row(_ROW_LAST_LOGIN))

        response = TestClient(app).get("/api/v1/auth/me")

        assert response.status_code == 200
        body = response.json()
        assert body["created_at"] == "2026-02-14T22:45:53Z"
        assert body["last_login"] == "2026-08-17T21:04:31Z"
        # The principal's request-time timestamp did NOT leak through.
        assert body["created_at"] != "2026-08-19T21:04:10Z"
