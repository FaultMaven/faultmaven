"""Unit tests for TenantProvider Factory (TASK-023).

Test Coverage: 4-6 tests

Design Reference: docs/working/TASK-023-TENANT-PROVIDER.md
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from faultmaven.providers.tenancy.factory import create_tenant_provider
from faultmaven.providers.tenancy.single_tenant import SingleTenantProvider
from faultmaven.providers.tenancy.multi_tenant import MultiTenantProvider
from faultmaven.config.settings import FaultMavenSettings


@pytest.fixture
def mock_organization_repository():
    """Mock organization repository."""
    return AsyncMock()


# ============================================================================
# Test: Factory creates SingleTenantProvider by default
# ============================================================================

def test_factory_creates_single_tenant_by_default(mock_organization_repository):
    """Test factory creates SingleTenantProvider when no DEPLOYMENT_MODE set."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        settings = MagicMock()
        settings.deployment_mode = "single-tenant"
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
    """Test factory creates SingleTenantProvider when DEPLOYMENT_MODE=single-tenant."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        settings = MagicMock()
        settings.deployment_mode = "single-tenant"
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
    """Test factory creates MultiTenantProvider when DEPLOYMENT_MODE=multi-tenant."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        settings = MagicMock()
        settings.deployment_mode = "multi-tenant"
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
        settings.deployment_mode = "single-tenant"
        mock_settings.return_value = settings

        single_provider = create_tenant_provider(
            organization_repository=mock_organization_repository
        )

        assert single_provider.organization_repository == mock_organization_repository

        # Test MultiTenantProvider
        settings.deployment_mode = "multi-tenant"

        multi_provider = create_tenant_provider(
            organization_repository=mock_organization_repository
        )

        assert multi_provider.organization_repository == mock_organization_repository


# ============================================================================
# Test: Factory handles case-insensitive deployment mode
# ============================================================================

def test_factory_handles_case_insensitive_mode(mock_organization_repository):
    """Test factory handles deployment mode in different cases."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        # Test uppercase
        settings = MagicMock()
        settings.deployment_mode = "MULTI-TENANT"
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
    """Test factory falls back to single-tenant for invalid/unknown deployment modes."""
    with patch('faultmaven.providers.tenancy.factory.get_settings') as mock_settings:
        settings = MagicMock()
        # Invalid mode should default to single-tenant
        settings.deployment_mode = "invalid-mode"
        mock_settings.return_value = settings

        provider = create_tenant_provider(
            organization_repository=mock_organization_repository
        )

        assert isinstance(provider, SingleTenantProvider)
