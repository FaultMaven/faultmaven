"""Unit tests for AuthProvider Factory

Tests provider selection based on configuration.
"""

from unittest.mock import MagicMock, patch

import pytest

from faultmaven.infrastructure.auth.providers.auth0 import Auth0Provider
from faultmaven.infrastructure.auth.providers.clerk import ClerkProvider
from faultmaven.infrastructure.auth.providers.factory import create_auth_provider
from faultmaven.infrastructure.auth.providers.no_auth import NoAuthProvider


@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = MagicMock()
    settings.security = MagicMock()
    return settings


class TestAuthProviderFactory:
    """Tests for create_auth_provider factory function."""

    def test_create_no_auth_provider_default(self, mock_settings):
        """Test that NoAuthProvider is created by default."""
        mock_settings.security.auth_provider = "no-auth"
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            provider = create_auth_provider()
            
            assert isinstance(provider, NoAuthProvider)

    def test_create_no_auth_provider_none(self, mock_settings):
        """Test that 'none' creates NoAuthProvider."""
        mock_settings.security.auth_provider = "none"
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            provider = create_auth_provider()
            
            assert isinstance(provider, NoAuthProvider)

    def test_create_no_auth_provider_empty(self, mock_settings):
        """Test that empty string creates NoAuthProvider."""
        mock_settings.security.auth_provider = ""
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            provider = create_auth_provider()
            
            assert isinstance(provider, NoAuthProvider)

    def test_create_auth0_provider(self, mock_settings):
        """Test that Auth0Provider is created with correct config."""
        mock_settings.security.auth_provider = "auth0"
        mock_settings.security.auth0_domain = "test-tenant.auth0.com"
        mock_settings.security.auth0_audience = "test-api-audience"
        mock_settings.security.auth0_issuer = None
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            provider = create_auth_provider()
            
            assert isinstance(provider, Auth0Provider)
            assert provider.domain == "test-tenant.auth0.com"
            assert provider.audience == "test-api-audience"

    def test_create_auth0_provider_with_issuer(self, mock_settings):
        """Test that Auth0Provider uses custom issuer when provided."""
        mock_settings.security.auth_provider = "auth0"
        mock_settings.security.auth0_domain = "test-tenant.auth0.com"
        mock_settings.security.auth0_audience = "test-api-audience"
        mock_settings.security.auth0_issuer = "https://custom-issuer.com/"
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            provider = create_auth_provider()
            
            assert isinstance(provider, Auth0Provider)
            assert provider.issuer == "https://custom-issuer.com/"

    def test_create_auth0_provider_missing_domain_raises_error(self, mock_settings):
        """Test that missing AUTH0_DOMAIN raises ValueError."""
        mock_settings.security.auth_provider = "auth0"
        mock_settings.security.auth0_domain = None
        mock_settings.security.auth0_audience = "test-api-audience"
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            with pytest.raises(ValueError, match="AUTH0_DOMAIN is required"):
                create_auth_provider()

    def test_create_auth0_provider_missing_audience_raises_error(self, mock_settings):
        """Test that missing AUTH0_AUDIENCE raises ValueError."""
        mock_settings.security.auth_provider = "auth0"
        mock_settings.security.auth0_domain = "test-tenant.auth0.com"
        mock_settings.security.auth0_audience = None
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            with pytest.raises(ValueError, match="AUTH0_AUDIENCE is required"):
                create_auth_provider()

    def test_create_clerk_provider(self, mock_settings):
        """Test that ClerkProvider is created with correct config."""
        from pydantic import SecretStr
        
        mock_settings.security.auth_provider = "clerk"
        mock_settings.security.clerk_secret_key = SecretStr("test-secret-key")
        mock_settings.security.clerk_audience = "https://clerk.faultmaven.ai"
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            provider = create_auth_provider()
            
            assert isinstance(provider, ClerkProvider)
            assert provider.secret_key == "test-secret-key"
            assert provider.audience == "https://clerk.faultmaven.ai"

    def test_create_clerk_provider_missing_secret_raises_error(self, mock_settings):
        """Test that missing CLERK_SECRET_KEY raises ValueError."""
        mock_settings.security.auth_provider = "clerk"
        mock_settings.security.clerk_secret_key = None
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            with pytest.raises(ValueError, match="CLERK_SECRET_KEY is required"):
                create_auth_provider()

    def test_create_provider_unknown_raises_error(self, mock_settings):
        """Test that unknown provider raises ValueError."""
        mock_settings.security.auth_provider = "unknown-provider"
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            with pytest.raises(ValueError, match="Unknown AUTH_PROVIDER"):
                create_auth_provider()

    def test_create_provider_case_insensitive(self, mock_settings):
        """Test that provider selection is case-insensitive."""
        mock_settings.security.auth_provider = "AUTH0"  # Uppercase
        
        with patch('faultmaven.infrastructure.auth.providers.factory.get_settings', return_value=mock_settings):
            # Should still work (lower() is called in factory)
            mock_settings.security.auth0_domain = "test-tenant.auth0.com"
            mock_settings.security.auth0_audience = "test-api-audience"
            mock_settings.security.auth0_issuer = None
            
            provider = create_auth_provider()
            assert isinstance(provider, Auth0Provider)
