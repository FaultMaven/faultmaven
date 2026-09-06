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
def ent_repo():
    return AsyncMock()


def _settings(tenant_provider: str, is_cloud: bool = False) -> MagicMock:
    s = MagicMock()
    s.providers = MagicMock()
    s.providers.tenant_provider = tenant_provider
    s.is_cloud = is_cloud
    return s


# --- single (built-in) ------------------------------------------------------


@pytest.mark.unit
def test_single_is_builtin_and_gets_repositories(ent_repo):
    with patch(_SETTINGS, return_value=_settings("single")):
        provider = create_tenant_provider(enterprise_repository=ent_repo)
    assert isinstance(provider, SingleTenantProvider)
    assert provider.enterprise_repository is ent_repo


# --- multi (built-in, in-core — requires cloud) -----------------------------


@pytest.mark.unit
@pytest.mark.security
def test_multi_outside_cloud_fails_closed(ent_repo):
    """``multi`` requires DEPLOYMENT_MODE=cloud — outside it, the cloud stack
    its RLS isolation relies on isn't guaranteed, so the factory must refuse
    (covering the gate-less jobs/CLI path, not only the startup gate)."""
    with patch(_SETTINGS, return_value=_settings("multi", is_cloud=False)):
        with pytest.raises(TenancyConfigurationError) as exc:
            create_tenant_provider(enterprise_repository=ent_repo)
    assert "requires DEPLOYMENT_MODE=cloud" in str(exc.value)


@pytest.mark.unit
def test_multi_resolves_in_core_under_cloud(ent_repo):
    """Under DEPLOYMENT_MODE=cloud, ``multi`` builds the in-core
    MultiTenantProvider (no plugin)."""
    with patch(_SETTINGS, return_value=_settings("multi", is_cloud=True)):
        provider = create_tenant_provider(enterprise_repository=ent_repo)
    assert isinstance(provider, MultiTenantProvider)
    assert provider.enterprise_repository is ent_repo


@pytest.mark.unit
def test_single_value_is_case_insensitive(ent_repo):
    """'SINGLE' resolves to the built-in single provider."""
    with patch(_SETTINGS, return_value=_settings("SINGLE")):
        assert isinstance(
            create_tenant_provider(enterprise_repository=ent_repo),
            SingleTenantProvider,
        )


# --- unrecognized: fail closed ----------------------------------------------


@pytest.mark.unit
@pytest.mark.security
def test_unknown_value_fails_closed(ent_repo):
    """An unrecognized provider value must NOT fall back to single-tenant."""
    with patch(_SETTINGS, return_value=_settings("bogus")):
        with pytest.raises(TenancyConfigurationError) as exc:
            create_tenant_provider(enterprise_repository=ent_repo)
    assert "not a recognized provider" in str(exc.value)


@pytest.mark.unit
def test_coerce_provider_name_handles_enum_str_and_none():
    from faultmaven.providers.tenancy.factory import coerce_provider_name

    assert coerce_provider_name(None) == "single"  # unset -> built-in default
    assert coerce_provider_name("MULTI") == "multi"
    assert coerce_provider_name(SimpleNamespace(value="Multi")) == "multi"
