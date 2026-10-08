"""Unit tests for MultiTenantProvider (ADR-017 D1).

The provider no longer resolves the request's enterprise: that is bound once per
request by ``api/middleware/tenant_scope`` from the verified claim (pinned in
``tests/unit/api/middleware/test_tenant_scope.py``), and RLS scopes every read to
it. What is left to pin is the strategy's own surface.

There is no ``organization_members`` read in it, and its absence is the design
rather than an omission: under ADR-017 D2 an organization is a billing target and
grants nothing about data, so confining a request by it would confine it by who
pays.
"""

import pytest

from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider


@pytest.fixture
def provider():
    return MultiTenantProvider()


async def test_there_is_no_per_request_resolution_left_on_the_provider(provider):
    """Removed in #1891: nothing called it, and its membership check was a tautology.

    A re-add would revive a guard that cannot fail (both sides of its comparison
    came from the request binding), so the absence is asserted.
    """
    assert not hasattr(provider, "get_current_enterprise")
    assert not hasattr(provider, "get_default_enterprise")


async def test_it_reports_itself_multi_tenant(provider):
    assert await provider.is_multi_tenant() is True


async def test_it_never_consults_an_organization(provider):
    """The billing roster is not part of this decision (ADR-017 D2).

    Asserted structurally rather than by absence of a call: the provider holds
    no repository port at all, so there is nothing it could ask.
    """
    assert not hasattr(provider, "organization_repository")
    assert set(vars(provider)) == set()
