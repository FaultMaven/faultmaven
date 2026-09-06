"""Unit tests for MultiTenantProvider (ADR-017 D1).

What this provider answers is the ENTERPRISE — the tenant that isolates — and
what it checks is the account's anchor, ``users.enterprise_id``. There is no
``organization_members`` read left in it, and its absence is the design rather
than an omission: under ADR-017 D2 an organization is a billing target and
grants nothing about data, so confining a request by it would confine it by who
pays.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from faultmaven.exceptions import AuthorizationError, NotFoundError, ValidationException
from faultmaven.models.interfaces_user import Enterprise, EnterprisePlanTier
from faultmaven.modules.auth.domain.models.user import User
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider

ENTERPRISE_A = "ent_a"
ENTERPRISE_B = "ent_b"


@pytest.fixture
def enterprises():
    return AsyncMock()


@pytest.fixture
def provider(enterprises):
    return MultiTenantProvider(enterprise_repository=enterprises)


def _enterprise(enterprise_id: str = ENTERPRISE_A) -> Enterprise:
    now = datetime.now(timezone.utc)
    return Enterprise(
        enterprise_id=enterprise_id,
        slug=f"slug-{enterprise_id}",
        name=f"Enterprise {enterprise_id}",
        plan_tier=EnterprisePlanTier.BUSINESS,
        max_members=50,
        max_cases=1000,
        settings={},
        created_at=now,
        updated_at=now,
    )


def _user(enterprise_id=ENTERPRISE_A) -> User:
    return User(
        user_id="user_123",
        enterprise_id=enterprise_id,
        email="test@example.com",
        hashed_password="hashed",
        full_name="Test User",
    )


async def test_an_anchored_account_resolves_its_own_enterprise(provider, enterprises):
    enterprises.get_enterprise.return_value = _enterprise()

    resolved = await provider.get_current_enterprise(
        current_user=_user(), enterprise_id=ENTERPRISE_A
    )

    assert resolved.enterprise_id == ENTERPRISE_A
    enterprises.get_enterprise.assert_awaited_once_with(ENTERPRISE_A)


@pytest.mark.parametrize("missing", [None, ""])
async def test_a_request_with_no_enterprise_is_refused(provider, missing):
    """Not defaulted, not guessed. The claim is the only input (ADR-017 D9)."""
    with pytest.raises(ValidationException):
        await provider.get_current_enterprise(
            current_user=_user(), enterprise_id=missing
        )


async def test_an_enterprise_that_does_not_exist_is_not_found(provider, enterprises):
    enterprises.get_enterprise.return_value = None

    with pytest.raises(NotFoundError):
        await provider.get_current_enterprise(
            current_user=_user(), enterprise_id=ENTERPRISE_A
        )


async def test_an_account_anchored_elsewhere_is_refused(provider, enterprises):
    """The whole membership check, and it reads one column.

    B's account, A's enterprise: the row exists and is perfectly readable, so
    the refusal can only come from the anchor comparison.
    """
    enterprises.get_enterprise.return_value = _enterprise(ENTERPRISE_A)

    with pytest.raises(AuthorizationError):
        await provider.get_current_enterprise(
            current_user=_user(ENTERPRISE_B), enterprise_id=ENTERPRISE_A
        )


async def test_an_unanchored_account_is_refused_rather_than_admitted(
    provider, enterprises
):
    """Absence is not membership.

    The fail-open shape this guards is ``if user.enterprise_id and ... !=``,
    which admits every account that never resolved an anchor.
    """
    enterprises.get_enterprise.return_value = _enterprise(ENTERPRISE_A)

    with pytest.raises(AuthorizationError):
        await provider.get_current_enterprise(
            current_user=_user(None), enterprise_id=ENTERPRISE_A
        )


async def test_there_is_no_default_enterprise_under_multi(provider):
    """A multi-tenant deployment has no ambient tenant to fall back to."""
    with pytest.raises(NotImplementedError):
        await provider.get_default_enterprise()


async def test_it_reports_itself_multi_tenant(provider):
    assert await provider.is_multi_tenant() is True


async def test_it_never_consults_an_organization(provider, enterprises):
    """The billing roster is not part of this decision (ADR-017 D2).

    Asserted structurally rather than by absence of a call: the provider holds
    no organization port at all, so there is nothing it could ask.
    """
    enterprises.get_enterprise.return_value = _enterprise()
    await provider.get_current_enterprise(
        current_user=_user(), enterprise_id=ENTERPRISE_A
    )

    assert not hasattr(provider, "organization_repository")
    assert set(vars(provider)) == {"enterprise_repository"}
