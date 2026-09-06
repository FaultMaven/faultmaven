"""Integration tests for GET /api/v1/auth/me/available-scopes.

The scope-capability signal frontends gate their publish UI on (KB team-publish,
case share-to-team, global authoring). Every scope it reports must be one the
caller can actually publish to — offering a target the backend then refuses is
the drift this endpoint exists to prevent.

- ``team`` only when the deployment is team-enabled (``team_service`` wired)
  AND the caller belongs to a Team.
- ``global`` only for a ``platform_admin``, since every global publish route
  requires that role.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.modules.auth.api.auth import router as auth_router
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser


@pytest.fixture
def mock_user():
    """A baseline user: no team, not an operator."""
    return AuthenticatedUser(
        user_id="user_789",
        enterprise_id="org_1",
        email="tester@example.com",
        roles=["member"],
        permissions=["cases:read"],
    )


@pytest.fixture
def operator_user():
    """A platform admin — the only principal that may publish at global scope."""
    return AuthenticatedUser(
        user_id="op_1",
        enterprise_id="org_1",
        email="operator@example.com",
        roles=["user", "admin", "platform_admin"],
        permissions=[],
    )


async def _get_scopes(mock_user, team_service):
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1")
    app.state.team_service = team_service

    async def _override_auth():
        return mock_user

    app.dependency_overrides[require_authentication] = _override_auth
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/api/v1/auth/me/available-scopes")


class TestAvailableScopes:
    async def test_baseline_user_gets_personal_only(self, mock_user):
        """No team, not an operator → the only scope they can publish to."""
        response = await _get_scopes(mock_user, team_service=None)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["scopes"] == ["personal"]

    async def test_team_reported_when_user_has_teams(self, mock_user):
        """Cloud + membership → team scope offered, narrowest-to-widest order."""
        team_service = AsyncMock()
        team_service.list_all_user_team_ids = AsyncMock(return_value=["team_a"])
        response = await _get_scopes(mock_user, team_service=team_service)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["scopes"] == ["personal", "team"]

    async def test_no_team_scope_when_user_has_no_teams(self, mock_user):
        """team_service wired but the caller belongs to no Team → no team scope
        (nothing to target)."""
        team_service = AsyncMock()
        team_service.list_all_user_team_ids = AsyncMock(return_value=[])
        response = await _get_scopes(mock_user, team_service=team_service)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["scopes"] == ["personal"]


class TestGlobalScopeIsOperatorOnly:
    """`global` is the platform tier (ADR-012 D9).

    Every route that publishes there requires `platform_admin` — unconditionally
    for `POST /knowledge/documents`, and for `scope == "global"` on the
    conversion routes. Reporting it to anyone else makes the UI offer a target
    the backend refuses.
    """

    async def test_not_offered_to_a_non_operator(self, mock_user):
        response = await _get_scopes(mock_user, team_service=None)
        assert "global" not in response.json()["scopes"]

    async def test_not_offered_to_an_org_admin(self):
        """The org-scoped `admin` role is tenant-bounded; global is not."""
        org_admin = AuthenticatedUser(
            user_id="org_admin_1",
            enterprise_id="org_1",
            email="orgadmin@example.com",
            roles=["user", "admin"],
            permissions=[],
        )
        response = await _get_scopes(org_admin, team_service=None)
        assert "global" not in response.json()["scopes"]

    async def test_offered_to_a_platform_admin(self, operator_user):
        response = await _get_scopes(operator_user, team_service=None)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["scopes"] == ["personal", "global"]

    async def test_ordering_stays_narrowest_to_widest(self, operator_user):
        """An operator who is also on a team gets all three, in order."""
        team_service = AsyncMock()
        team_service.list_all_user_team_ids = AsyncMock(return_value=["team_a"])
        response = await _get_scopes(operator_user, team_service=team_service)
        assert response.json()["scopes"] == ["personal", "team", "global"]
