"""Unit tests for the TenantProvider factory (ADR-010).

Tenancy is config-selected in the core: ``single`` (the Standalone default) and
``multi`` (Cloud) are both in-core. An unrecognized value fails closed — it never
silently downgrades to single-tenant.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.providers.tenancy.factory import (
    TenancyConfigurationError,
    create_tenant_provider,
)
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider

_SETTINGS = "faultmaven.providers.tenancy.factory.get_settings"


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


# --- multi (built-in, in-core) ----------------------------------------------


@pytest.mark.unit
def test_multi_is_builtin_and_gets_org_repository(org_repo):
    """``multi`` resolves to the in-core MultiTenantProvider (no plugin)."""
    with patch(_SETTINGS, return_value=_settings("multi")):
        provider = create_tenant_provider(
            organization_repository=org_repo, enterprise_repository="ent"
        )
    assert isinstance(provider, MultiTenantProvider)
    assert provider.organization_repository is org_repo


@pytest.mark.unit
def test_provider_value_is_case_insensitive(org_repo):
    """'SINGLE'/'MULTI' resolve to the built-in providers."""
    with patch(_SETTINGS, return_value=_settings("SINGLE")):
        assert isinstance(
            create_tenant_provider(organization_repository=org_repo),
            SingleTenantProvider,
        )
    with patch(_SETTINGS, return_value=_settings("MULTI")):
        assert isinstance(
            create_tenant_provider(organization_repository=org_repo),
            MultiTenantProvider,
        )


# --- unrecognized: fail closed ----------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_unknown_value_fails_closed(org_repo):
    """An unrecognized provider value must NOT fall back to single-tenant."""
    with patch(_SETTINGS, return_value=_settings("bogus")):
        with pytest.raises(TenancyConfigurationError) as exc:
            create_tenant_provider(organization_repository=org_repo)
    assert "not a recognized provider" in str(exc.value)


@pytest.mark.unit
def test_coerce_provider_name_handles_enum_str_and_none():
    from faultmaven.providers.tenancy.factory import coerce_provider_name

    assert coerce_provider_name(None) == "single"  # unset -> built-in default
    assert coerce_provider_name("MULTI") == "multi"
    assert coerce_provider_name(SimpleNamespace(value="Multi")) == "multi"
