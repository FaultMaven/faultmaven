"""Unit tests for SingleTenantProvider (ADR-017 D8).

Standalone is one seeded ENTERPRISE and one seeded team. It has **no
organization**, and the absence is the design: an organization is a billing
target created by payment (D5), and nobody is billed for a self-hosted install.
So there is no ``ensure_default_organization_exists`` here to test — its absence
is asserted, because a provider that quietly re-grew one would put a row under
every standalone deployment that the rest of the campaign assumes is not there.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from faultmaven.config.constants import (
    STANDALONE_ENTERPRISE_ID,
    STANDALONE_TEAM_ID,
    STANDALONE_TEAM_NAME,
)
from faultmaven.exceptions import NotFoundError
from faultmaven.models.interfaces_user import Enterprise, EnterprisePlanTier, Team
from faultmaven.modules.auth.domain.models.user import User
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider


def _enterprise() -> Enterprise:
    now = datetime.now(timezone.utc)
    return Enterprise(
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        slug="default",
        name="Default Enterprise",
        plan_tier=EnterprisePlanTier.PRO,
        max_members=100,
        max_cases=None,
        settings={},
        created_at=now,
        updated_at=now,
    )


def _team() -> Team:
    now = datetime.now(timezone.utc)
    return Team(
        team_id=STANDALONE_TEAM_ID,
        enterprise_id=STANDALONE_ENTERPRISE_ID,
        name=STANDALONE_TEAM_NAME,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def enterprises():
    return AsyncMock()


@pytest.fixture
def teams():
    return AsyncMock()


@pytest.fixture
def provider(enterprises, teams):
    return SingleTenantProvider(
        enterprise_repository=enterprises, team_repository=teams
    )


def _user() -> User:
    return User(
        user_id="user_1",
        email="u@example.com",
        hashed_password="h",
        full_name="U",
    )


async def test_every_request_resolves_the_one_enterprise(provider, enterprises):
    enterprises.get_enterprise.return_value = _enterprise()

    resolved = await provider.get_current_enterprise(current_user=_user())

    assert resolved.enterprise_id == STANDALONE_ENTERPRISE_ID


async def test_an_injected_enterprise_is_ignored(provider, enterprises):
    """The standalone re-leak guard (ADR-010).

    A forged claim must not re-scope a single-tenant deployment, so the argument
    is not merely defaulted — it is discarded.
    """
    enterprises.get_enterprise.return_value = _enterprise()

    resolved = await provider.get_current_enterprise(
        current_user=_user(), enterprise_id="ent_somebody_elses"
    )

    assert resolved.enterprise_id == STANDALONE_ENTERPRISE_ID
    enterprises.get_enterprise.assert_awaited_once_with(STANDALONE_ENTERPRISE_ID)


async def test_the_default_enterprise_is_cached(provider, enterprises):
    enterprises.get_enterprise.return_value = _enterprise()

    first = await provider.get_default_enterprise()
    second = await provider.get_default_enterprise()

    assert first is second
    assert enterprises.get_enterprise.await_count == 1


async def test_a_missing_default_enterprise_is_not_found(provider, enterprises):
    """The seed did not run. Reported, not invented."""
    enterprises.get_enterprise.return_value = None

    with pytest.raises(NotFoundError):
        await provider.get_default_enterprise()


async def test_it_reports_itself_single_tenant(provider):
    assert await provider.is_multi_tenant() is False


async def test_ensure_default_enterprise_creates_when_absent(provider, enterprises):
    enterprises.get_enterprise.return_value = None
    enterprises.create_enterprise.side_effect = lambda e: e

    created = await provider.ensure_default_enterprise_exists()

    assert created.enterprise_id == STANDALONE_ENTERPRISE_ID
    assert created.plan_tier is EnterprisePlanTier.PRO


async def test_ensure_default_enterprise_is_idempotent(provider, enterprises):
    enterprises.get_enterprise.return_value = _enterprise()

    first = await provider.ensure_default_enterprise_exists()
    second = await provider.ensure_default_enterprise_exists()

    assert first.enterprise_id == second.enterprise_id
    enterprises.create_enterprise.assert_not_awaited()


async def test_the_default_team_hangs_off_the_enterprise(provider, teams):
    """Not off an organization — there is none (ADR-017 D4/D8)."""
    teams.get_team.return_value = None
    teams.create_team.side_effect = lambda t: t

    team = await provider.ensure_default_team_exists()

    assert team.enterprise_id == STANDALONE_ENTERPRISE_ID
    assert not hasattr(team, "organization_id")


async def test_ensure_default_team_is_idempotent(provider, teams):
    teams.get_team.return_value = _team()

    await provider.ensure_default_team_exists()

    teams.create_team.assert_not_awaited()


async def test_an_unwired_team_repository_seeds_nothing(enterprises):
    provider = SingleTenantProvider(enterprise_repository=enterprises)
    assert await provider.ensure_default_team_exists() is None


async def test_there_is_no_default_organization_to_ensure(provider):
    """Deleted, not deprecated (the owner's rule for this campaign).

    A standalone deployment writes ``organization_id = NULL`` on every row, so a
    provider that still minted a default organization would put a row under it
    that nothing pays for and nothing reads — and the next reader to find one
    would reasonably conclude the deployment has a billing subject.
    """
    assert not hasattr(provider, "ensure_default_organization_exists")
    assert not hasattr(provider, "get_default_organization")
    assert not hasattr(SingleTenantProvider, "DEFAULT_ORG_ID")
