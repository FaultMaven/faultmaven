"""Integration tests for GET /api/v1/teams (list the caller's teams).

Drives the route end-to-end (auth dependency + app.state.team_service). This is
the endpoint the dashboard needs to resolve team ids to names (case share badges)
and to populate the share-to-team picker.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, status
from httpx import ASGITransport, AsyncClient

from faultmaven.api.v1.auth_dependencies import require_authentication
from faultmaven.models.interfaces_user import Team
from faultmaven.modules.auth.api.teams import router as teams_router
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser


def _make_team(team_id: str, name: str, org: str = "org_1") -> Team:
    now = datetime.now(timezone.utc)
    return Team(
        team_id=team_id,
        organization_id=org,
        name=name,
        description=None,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def mock_user():
    return AuthenticatedUser(
        user_id="user_789",
        organization_id="org_1",
        email="tester@example.com",
        roles=["member"],
        permissions=["cases:read"],
    )


def _build_app(mock_user, team_service):
    app = FastAPI()
    app.include_router(teams_router, prefix="/api/v1")
    app.state.team_service = team_service

    async def _override_auth():
        return mock_user

    app.dependency_overrides[require_authentication] = _override_auth
    return app


@pytest.fixture
async def client_factory(mock_user):
    clients = []

    async def _make(team_service):
        app = _build_app(mock_user, team_service)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return client

    yield _make
    for c in clients:
        await c.aclose()


class TestListMyTeams:
    async def test_returns_the_users_teams_with_names(self, client_factory):
        team_service = AsyncMock()
        team_service.list_user_teams = AsyncMock(
            return_value=[
                _make_team("team_a", "Platform"),
                _make_team("team_b", "Payments"),
            ]
        )
        client = await client_factory(team_service)

        response = await client.get("/api/v1/teams")

        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert [t["team_id"] for t in data] == ["team_a", "team_b"]
        assert [t["name"] for t in data] == ["Platform", "Payments"]
        team_service.list_user_teams.assert_awaited_once_with("user_789")

    async def test_empty_when_user_has_no_teams(self, client_factory):
        team_service = AsyncMock()
        team_service.list_user_teams = AsyncMock(return_value=[])
        client = await client_factory(team_service)

        response = await client.get("/api/v1/teams")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    async def test_standalone_no_team_service_returns_empty(self, client_factory):
        """team_service unwired (standalone) → empty list, never a 500."""
        client = await client_factory(None)

        response = await client.get("/api/v1/teams")

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == []

    async def test_requires_authentication(self, mock_user):
        """No auth override → 401 (endpoint is authenticated)."""
        app = FastAPI()
        app.include_router(teams_router, prefix="/api/v1")
        app.state.team_service = None
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/teams")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
