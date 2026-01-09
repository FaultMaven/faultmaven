"""Unit tests for TenantProvider Factory (TASK-023).

Test Coverage: 4-6 tests

Design Reference: docs/working/TASK-023-TENANT-PROVIDER.md
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from faultmaven.providers.tenancy.factory import create_tenant_provider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
from faultmaven.config.settings import TenantProvider


@pytest.fixture
def mock_organization_repository():
    """Mock organization repository."""
    return AsyncMock()


# ============================================================================
# Test: Factory creates SingleTenantProvider by default
# ============================================================================

def test_factory_creates_single_tenant_by_default(mock_organization_repository):
    """Test factory creates SingleTenantProvider when tenant_provider is single."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        settings = MagicMock()
        settings.providers = MagicMock()
        settings.providers.tenant_provider = TenantProvider.SINGLE
        mock_settings.return_value = settings

        provider = create_tenant_provider(
            organization_repository=mock_organization_repository
        )

        assert isinstance(provider, SingleTenantProvider)
        assert provider.organization_repository == mock_organization_repository


# ============================================================================
# Test: Factory creates SingleTenantProvider when mode is "single-tenant"
# ============================================================================

def test_factory_creates_single_tenant_when_mode_is_single_tenant(
    mock_organization_repository
):
    """Test factory creates SingleTenantProvider when TENANT_PROVIDER=single."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        settings = MagicMock()
        settings.providers = MagicMock()
        settings.providers.tenant_provider = TenantProvider.SINGLE
        mock_settings.return_value = settings

        provider = create_tenant_provider(
            organization_repository=mock_organization_repository
        )

        assert isinstance(provider, SingleTenantProvider)
        assert not isinstance(provider, MultiTenantProvider)


# ============================================================================
# Test: Factory creates MultiTenantProvider when mode is "multi-tenant"
# ============================================================================

def test_factory_creates_multi_tenant_when_mode_is_multi_tenant(
    mock_organization_repository
):
    """Test factory creates MultiTenantProvider when TENANT_PROVIDER=multi."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        settings = MagicMock()
        settings.providers = MagicMock()
        settings.providers.tenant_provider = TenantProvider.MULTI
        mock_settings.return_value = settings

        provider = create_tenant_provider(
            organization_repository=mock_organization_repository
        )

        assert isinstance(provider, MultiTenantProvider)
        assert not isinstance(provider, SingleTenantProvider)


# ============================================================================
# Test: Factory passes repositories to providers
# ============================================================================

def test_factory_passes_repositories_to_providers(mock_organization_repository):
    """Test factory correctly passes repository to both provider types."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        # Test SingleTenantProvider
        settings = MagicMock()
        settings.providers = MagicMock()
        settings.providers.tenant_provider = TenantProvider.SINGLE
        mock_settings.return_value = settings

        single_provider = create_tenant_provider(
            organization_repository=mock_organization_repository
        )

        assert single_provider.organization_repository == mock_organization_repository

        # Test MultiTenantProvider
        settings.providers.tenant_provider = TenantProvider.MULTI

        multi_provider = create_tenant_provider(
            organization_repository=mock_organization_repository
        )

        assert multi_provider.organization_repository == mock_organization_repository


# ============================================================================
# Test: Factory handles case-insensitive deployment mode
# ============================================================================

def test_factory_handles_case_insensitive_mode(mock_organization_repository):
    """Test factory creates multi-tenant provider when tenant_provider is MULTI."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        settings = MagicMock()
        settings.providers = MagicMock()
        settings.providers.tenant_provider = TenantProvider.MULTI
        mock_settings.return_value = settings

        provider = create_tenant_provider(
            organization_repository=mock_organization_repository
        )

        assert isinstance(provider, MultiTenantProvider)


# ============================================================================
# Test: Factory defaults to single-tenant for unknown modes
# ============================================================================

def test_factory_defaults_to_single_tenant_for_unknown_modes(
    mock_organization_repository
):
    """Test factory defaults to single-tenant for unknown tenant_provider values."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        settings = MagicMock()
        settings.providers = MagicMock()
        # Invalid/unknown value should default to single-tenant
        settings.providers.tenant_provider = "invalid-mode"
        mock_settings.return_value = settings

        provider = create_tenant_provider(
            organization_repository=mock_organization_repository
        )

        assert isinstance(provider, SingleTenantProvider)
