"""Unit tests for TeamService (KB team-scope resolver) + its DI gating.

Covers the thin resolver service and the container factory ``create_team_service``
that wires it only in multi-tenant (Cloud) mode.
"""

from unittest.mock import AsyncMock, Mock

import pytest

from faultmaven.container.providers.services import create_team_service
from faultmaven.modules.auth.domain.services.team_service import TeamService
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider


@pytest.mark.asyncio
@pytest.mark.unit
async def test_team_service_delegates_to_repository():
    """list_all_user_team_ids passes through to the repository."""
    repo = AsyncMock()
    repo.list_all_user_team_ids.return_value = ["t1", "t2"]
    service = TeamService(repo)

    result = await service.list_all_user_team_ids("user-1")

    assert result == ["t1", "t2"]
    repo.list_all_user_team_ids.assert_awaited_once_with("user-1")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_team_service_returns_empty_for_no_memberships():
    """Standalone-inert / no-membership case degrades to an empty set."""
    repo = AsyncMock()
    repo.list_all_user_team_ids.return_value = []
    service = TeamService(repo)

    assert await service.list_all_user_team_ids("user-1") == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_team_service_list_user_teams_delegates_to_repository():
    """list_user_teams passes through to the repository (GET /teams read path)."""
    repo = AsyncMock()
    teams = [Mock(team_id="t1"), Mock(team_id="t2")]
    repo.list_user_teams.return_value = teams
    service = TeamService(repo)

    result = await service.list_user_teams("user-1")

    assert result == teams
    repo.list_user_teams.assert_awaited_once_with("user-1")


@pytest.mark.unit
def test_create_team_service_none_in_single_tenant():
    """Standalone (SingleTenantProvider) leaves team_service unwired."""
    single = SingleTenantProvider(organization_repository=Mock())
    team_repo = Mock()

    assert create_team_service(single, team_repo) is None


@pytest.mark.unit
def test_create_team_service_wired_in_multi_tenant():
    """A non-single tenant provider (Cloud/multi) gets a real resolver."""
    multi_like = Mock()  # any provider that is not a SingleTenantProvider
    team_repo = Mock()

    service = create_team_service(multi_like, team_repo)

    assert isinstance(service, TeamService)


@pytest.mark.unit
def test_create_team_service_none_without_repository():
    """No repository → no service, regardless of provider."""
    assert create_team_service(Mock(), None) is None


@pytest.mark.unit
def test_create_team_service_none_without_provider():
    """No tenant provider → no service."""
    assert create_team_service(None, Mock()) is None
