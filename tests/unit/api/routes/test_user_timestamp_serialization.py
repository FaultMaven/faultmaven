"""fm#1129: user timestamps serialize in ONE format across the API.

``GET /auth/me`` emits ``to_json_compatible`` strings (``…Z``, the documented
single serialization source of truth), while the admin surface fed raw
``.isoformat()`` values into its response models. The format on the wire is
decided by whether the datetime is tz-aware: under the default SQLite backend
``DateTime(timezone=True)`` round-trips ``tzinfo=None``, so the admin
endpoints emitted a suffix-less naive string (``2026-02-14T22:45:53``, not
even localizable) for the same row ``/auth/me`` reported as
``2026-02-14T22:45:53Z``; under Postgres they emitted ``+00:00``. A frontend
or snapshot comparing one row across the two endpoints broke.

Both admin sites now route through ``to_json_compatible`` before the value
enters the response model. The model fields stay ``datetime`` (no OpenAPI
schema change): pydantic parses the ``…Z`` string to an aware datetime and
re-emits it as ``…Z``, so the wire format converges. These tests pin that at
the JSON boundary — through the real routes and the real ``UserService`` —
against the exact string ``to_json_compatible`` produces, which is what
``/auth/me`` emits for the same value (#1123). Parametrized over naive
(SQLite round-trip) and aware-UTC (Postgres round-trip) datetimes: the two
backends must not produce two formats.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.routes.admin import get_user_service, router
from faultmaven.infrastructure.persistence.user_repository import User as RepositoryUser
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser
from faultmaven.modules.auth.domain.services.user_service import UserService
from faultmaven.utils.serialization import to_json_compatible

pytestmark = pytest.mark.unit

#: One instant, both round-trip shapes. SQLite hands DateTime(timezone=True)
#: back naive; Postgres hands it back aware-UTC. Microseconds on purpose —
#: they must survive the string→datetime→string round-trip too.
NAIVE = datetime(2026, 2, 14, 22, 45, 53, 123456)
AWARE = NAIVE.replace(tzinfo=timezone.utc)

#: What /auth/me emits for this instant (via to_json_compatible, #1123) —
#: the value the admin surface must now agree with.
CANONICAL = "2026-02-14T22:45:53.123456Z"


def _stored_user(dt: datetime) -> RepositoryUser:
    return RepositoryUser(
        user_id="user-1129",
        username="operator",
        email="operator@local.faultmaven",
        display_name="Operator",
        created_at=dt,
        updated_at=dt,
        last_login_at=dt,
        roles=["user"],
    )


def _operator() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="op-1",
        organization_id="org-1",
        email="operator@example.com",
        roles=["user", "admin", "platform_admin"],
        permissions=[],
    )


class _Repo:
    def __init__(self, user: RepositoryUser):
        self._user = user

    async def get(self, user_id: str):
        return self._user if user_id == self._user.user_id else None


def _client(user_service) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_platform_admin] = _operator
    app.dependency_overrides[get_user_service] = lambda: user_service
    return TestClient(app)


def test_the_canonical_constant_is_what_to_json_compatible_emits():
    """Self-check: the string the tests compare against IS the /auth/me shape,
    for both round-trip forms of the same instant."""
    assert to_json_compatible(NAIVE) == CANONICAL
    assert to_json_compatible(AWARE) == CANONICAL


@pytest.mark.parametrize("dt", [NAIVE, AWARE], ids=["sqlite-naive", "postgres-aware"])
def test_admin_user_list_emits_the_auth_me_timestamp_format(dt: datetime):
    service = AsyncMock()
    service.list_users = AsyncMock(return_value=([_stored_user(dt)], 1))

    response = _client(service).get("/api/v1/admin/users")

    assert response.status_code == 200
    row = response.json()["users"][0]
    assert row["created_at"] == CANONICAL
    assert row["updated_at"] == CANONICAL
    assert row["last_login_at"] == CANONICAL


@pytest.mark.parametrize("dt", [NAIVE, AWARE], ids=["sqlite-naive", "postgres-aware"])
def test_admin_user_detail_emits_the_auth_me_timestamp_format(dt: datetime):
    """Through the REAL UserService.get_user_with_metadata — the other
    .isoformat() site the issue names — and the real detail route."""
    service = UserService(
        user_repo=_Repo(_stored_user(dt)),
        auth_service=SimpleNamespace(),
    )

    response = _client(service).get("/api/v1/admin/users/user-1129")

    assert response.status_code == 200
    body = response.json()
    assert body["created_at"] == CANONICAL
    assert body["updated_at"] == CANONICAL
    assert body["last_login_at"] == CANONICAL


def test_absent_last_login_stays_null_through_both_admin_surfaces():
    """to_json_compatible(None) is None — the fm#1127 'never logged in'
    signal must survive the convergence, not become an epoch or a string."""
    user = _stored_user(NAIVE)
    user.last_login_at = None

    list_service = AsyncMock()
    list_service.list_users = AsyncMock(return_value=([user], 1))
    assert (
        _client(list_service)
        .get("/api/v1/admin/users")
        .json()["users"][0]["last_login_at"]
        is None
    )

    detail_service = UserService(user_repo=_Repo(user), auth_service=SimpleNamespace())
    assert (
        _client(detail_service)
        .get("/api/v1/admin/users/user-1129")
        .json()["last_login_at"]
        is None
    )
