"""Integration tests for GET /api/v1/auth/me/available-scopes.

The scope-capability signal frontends gate their team UI on (KB team-publish,
case share-to-team). It must report ``team`` only when the deployment is
team-enabled (``team_service`` wired) AND the caller belongs to a Team.
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
    return AuthenticatedUser(
        user_id="user_789",
        organization_id="org_1",
        email="tester@example.com",
        roles=["member"],
        permissions=["cases:read"],
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
    async def test_standalone_personal_and_global_only(self, mock_user):
        """No team_service (standalone) → team scope is not offered."""
        response = await _get_scopes(mock_user, team_service=None)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["scopes"] == ["personal", "global"]

    async def test_team_reported_when_user_has_teams(self, mock_user):
        """Cloud + membership → team scope offered, narrowest-to-widest order."""
        team_service = AsyncMock()
        team_service.list_all_user_team_ids = AsyncMock(return_value=["team_a"])
        response = await _get_scopes(mock_user, team_service=team_service)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["scopes"] == ["personal", "team", "global"]

    async def test_no_team_scope_when_user_has_no_teams(self, mock_user):
        """team_service wired but the caller belongs to no Team → no team scope
        (nothing to target)."""
        team_service = AsyncMock()
        team_service.list_all_user_team_ids = AsyncMock(return_value=[])
        response = await _get_scopes(mock_user, team_service=team_service)
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["scopes"] == ["personal", "global"]
