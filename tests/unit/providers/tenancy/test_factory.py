"""Unit tests for the TenantProvider factory (ADR-006 entry-point seam).

`single` is the built-in default; non-`single` providers (e.g. `multi`) come
from an installed plugin via the `faultmaven.providers.tenancy` entry-point
group. Missing plugin -> fail closed (never silently downgrade to single).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.providers.tenancy.factory import (
    TenancyConfigurationError,
    create_tenant_provider,
    find_tenant_provider_plugin,
)
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

_SETTINGS = "faultmaven.providers.tenancy.factory.get_settings"
_PLUGIN = "faultmaven.providers.tenancy.factory.find_tenant_provider_plugin"


@pytest.fixture
def org_repo():
    return AsyncMock()


def _settings(tenant_provider: str) -> MagicMock:
    s = MagicMock()
    s.providers = MagicMock()
    s.providers.tenant_provider = tenant_provider
    return s


# --- single (built-in) ------------------------------------------------------


@pytest.mark.unit
def test_single_is_builtin_and_gets_repositories(org_repo):
    with patch(_SETTINGS, return_value=_settings("single")):
        provider = create_tenant_provider(
            organization_repository=org_repo, enterprise_repository="ent"
        )
    assert isinstance(provider, SingleTenantProvider)
    assert provider.organization_repository is org_repo


# --- non-single: fail closed without a plugin -------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_multi_without_plugin_fails_closed(org_repo):
    """No silent downgrade to single-tenant when the multi plugin is absent."""
    with patch(_SETTINGS, return_value=_settings("multi")):
        with patch(_PLUGIN, return_value=None):
            with pytest.raises(TenancyConfigurationError) as exc:
                create_tenant_provider(organization_repository=org_repo)
    assert "TENANT_PROVIDER='multi'" in str(exc.value)
    assert "faultmaven-cloud" in str(exc.value)


@pytest.mark.unit
@pytest.mark.security
def test_unknown_non_single_value_fails_closed(org_repo):
    """An unrecognized (non-single) value must NOT fall back to single."""
    with patch(_SETTINGS, return_value=_settings("bogus")):
        with patch(_PLUGIN, return_value=None):
            with pytest.raises(TenancyConfigurationError):
                create_tenant_provider(organization_repository=org_repo)


# --- non-single: loads the installed plugin ---------------------------------


@pytest.mark.unit
def test_multi_loads_plugin_and_forwards_repositories(org_repo):
    built = object()
    builder = MagicMock(return_value=built)
    fake_ep = SimpleNamespace(load=lambda: builder, value="pkg:build")

    with patch(_SETTINGS, return_value=_settings("multi")):
        with patch(_PLUGIN, return_value=fake_ep):
            provider = create_tenant_provider(
                organization_repository=org_repo, enterprise_repository="ent"
            )

    assert provider is built
    builder.assert_called_once_with(
        organization_repository=org_repo, enterprise_repository="ent"
    )


@pytest.mark.unit
def test_provider_value_is_case_insensitive(org_repo):
    """'SINGLE' resolves to the built-in single provider."""
    with patch(_SETTINGS, return_value=_settings("SINGLE")):
        provider = create_tenant_provider(organization_repository=org_repo)
    assert isinstance(provider, SingleTenantProvider)


# --- plugin discovery --------------------------------------------------------


@pytest.mark.unit
def test_find_plugin_returns_none_when_unregistered():
    """No plugin is registered for 'multi' in the core test env."""
    assert find_tenant_provider_plugin("multi") is None
