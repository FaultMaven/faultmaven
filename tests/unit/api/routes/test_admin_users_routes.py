"""Admin user-management routes (`/api/v1/admin/users*`).

This surface had NO test coverage, which is how it shipped calling
``get_user_service()`` with no argument against a signature requiring
``request`` — a TypeError swallowed by a broad ``except`` and returned as a 500
on every route. These tests pin that the routes actually reach their service.

They also pin the ADR-012 D9 invariant on this endpoint specifically: the
user-management API must not be able to grant the cross-tenant operator role.
That is the property the whole role split rests on, and this is the only API
surface that assigns roles at all.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from faultmaven.api.middleware.auth import require_platform_admin
from faultmaven.api.routes.admin import get_user_service, router
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser


def _operator() -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id="op-1",
        organization_id="org-1",
        email="operator@example.com",
        roles=["user", "admin", "platform_admin"],
        permissions=[],
    )


@pytest.fixture
def user_service():
    service = AsyncMock()
    service.list_users = AsyncMock(return_value=([], 0))
    service.assign_role = AsyncMock()
    return service


@pytest.fixture
def client(user_service):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_platform_admin] = _operator
    app.dependency_overrides[get_user_service] = lambda: user_service
    return TestClient(app)


@pytest.mark.unit
class TestRoutesReachTheirService:
    """Regression: every route here used to 500 before touching the service."""

    def test_list_users_reaches_the_service(self, client, user_service):
        resp = client.get("/api/v1/admin/users")

        assert resp.status_code == 200
        user_service.list_users.assert_awaited_once()

    def test_list_users_passes_filters_through(self, client, user_service):
        client.get("/api/v1/admin/users?is_active=true&search=alice")

        kwargs = user_service.list_users.await_args.kwargs
        assert kwargs["is_active"] is True
        assert kwargs["search"] == "alice"


@pytest.mark.unit
@pytest.mark.security
class TestRoleAssignmentCannotMintAnOperator:
    def test_platform_admin_is_rejected(self, client, user_service):
        """The operator role is not assignable through the user-management API.

        If this ever succeeds, `platform_admin` has been added to the org `Role`
        vocabulary and any org admin with access to this endpoint can grant
        themselves cross-tenant reach.
        """
        resp = client.post(
            "/api/v1/admin/users/target-1/roles", json={"role": "platform_admin"}
        )

        assert resp.status_code == 422
        user_service.assign_role.assert_not_awaited()

    @pytest.mark.parametrize("role", ["admin", "member", "viewer"])
    def test_org_roles_are_accepted(self, client, user_service, role):
        """The org-scoped vocabulary still works — this is not a blanket block."""
        client.post("/api/v1/admin/users/target-1/roles", json={"role": role})

        assert user_service.assign_role.await_args.kwargs["role"] == role
