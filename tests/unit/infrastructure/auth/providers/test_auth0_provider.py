"""Unit tests for Auth0Provider (Cloud Deployment)

Tests JWT validation using Auth0 JWKS endpoint.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jose import jwt, jwk

from faultmaven.infrastructure.auth.providers.auth0 import Auth0Provider
from faultmaven.modules.auth.domain.models import AuthenticatedUser


@pytest.fixture
def auth0_provider():
    """Create Auth0Provider instance."""
    return Auth0Provider(
        domain="test-tenant.auth0.com",
        audience="test-api-audience",
        issuer="https://test-tenant.auth0.com/"
    )


@pytest.fixture
def mock_jwks():
    """Mock JWKS response from Auth0."""
    # Simplified mock JWKS - actual key validation is tested via mocking
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-key-id",
                "use": "sig",
                "n": "test-n-value",
                "e": "AQAB",
                "alg": "RS256"
            }
        ]
    }


@pytest.fixture
def valid_auth0_token():
    """Create a valid Auth0 JWT token (mocked)."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": "auth0|user-123",
        "email": "user@example.com",
        "aud": "test-api-audience",
        "iss": "https://test-tenant.auth0.com/",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "https://faultmaven.ai/roles": ["admin", "member"],
        "org_id": "org-456",
        "jti": "token-789"
    }
    
    # Return mock token string and claims
    # Actual validation will be mocked in tests
    return "mock-valid-token", claims


class TestAuth0Provider:
    """Tests for Auth0Provider."""

    def test_initialization(self, auth0_provider):
        """Test that Auth0Provider initializes correctly."""
        assert auth0_provider.domain == "test-tenant.auth0.com"
        assert auth0_provider.audience == "test-api-audience"
        assert auth0_provider.issuer == "https://test-tenant.auth0.com/"
        assert auth0_provider.jwks_url == "https://test-tenant.auth0.com/.well-known/jwks.json"

    def test_is_enabled_returns_true(self, auth0_provider):
        """Test that is_enabled returns True (auth enabled)."""
        assert auth0_provider.is_enabled() is True

    @pytest.mark.asyncio
    async def test_validate_token_empty_returns_none(self, auth0_provider):
        """Test that empty token returns None."""
        user = await auth0_provider.validate_token("")
        assert user is None

    @pytest.mark.asyncio
    async def test_validate_token_invalid_returns_none(self, auth0_provider):
        """Test that invalid token returns None."""
        user = await auth0_provider.validate_token("invalid-token")
        assert user is None

    @pytest.mark.asyncio
    async def test_validate_token_valid_returns_user(self, auth0_provider, mock_jwks, valid_auth0_token):
        """Test that valid token returns AuthenticatedUser."""
        jwks_dict = mock_jwks
        token, claims = valid_auth0_token
        
        # Mock the JWT decode to return our claims
        with patch.object(auth0_provider, '_get_jwks', return_value=jwks_dict):
            with patch('faultmaven.infrastructure.auth.providers.auth0.jwt.decode', return_value=claims):
                with patch('faultmaven.infrastructure.auth.providers.auth0.jwt.get_unverified_header', return_value={"kid": "test-key-id"}):
                    with patch('faultmaven.infrastructure.auth.providers.auth0.jwk.construct') as mock_construct:
                        mock_key = MagicMock()
                        mock_construct.return_value = mock_key
                        
                        user = await auth0_provider.validate_token(token)
                        
                        assert user is not None
                        assert isinstance(user, AuthenticatedUser)
                        assert user.user_id == claims["sub"]
                        assert user.email == claims["email"]
                        assert user.organization_id == claims["org_id"]
                        assert user.roles == claims["https://faultmaven.ai/roles"]
                        assert user.token_jti == claims["jti"]

    @pytest.mark.asyncio
    async def test_validate_token_expired_returns_none(self, auth0_provider, mock_jwks):
        """Test that expired token returns None."""
        jwks_dict = mock_jwks
        token = "expired-token"
        
        # Mock jwt.decode to raise ExpiredSignatureError
        from jose.exceptions import ExpiredSignatureError
        
        with patch.object(auth0_provider, '_get_jwks', return_value=jwks_dict):
            with patch('faultmaven.infrastructure.auth.providers.auth0.jwt.decode', side_effect=ExpiredSignatureError("Token expired")):
                with patch('faultmaven.infrastructure.auth.providers.auth0.jwt.get_unverified_header', return_value={"kid": "test-key-id"}):
                    with patch('faultmaven.infrastructure.auth.providers.auth0.jwk.construct') as mock_construct:
                        mock_key = MagicMock()
                        mock_construct.return_value = mock_key
                        
                        user = await auth0_provider.validate_token(token)
                        assert user is None

    @pytest.mark.asyncio
    async def test_validate_token_wrong_audience_returns_none(self, auth0_provider, mock_jwks):
        """Test that token with wrong audience returns None."""
        jwks_dict = mock_jwks
        token = "wrong-audience-token"
        
        # Mock jwt.decode to raise JWTClaimsError for wrong audience
        from jose.exceptions import JWTClaimsError
        
        with patch.object(auth0_provider, '_get_jwks', return_value=jwks_dict):
            with patch('faultmaven.infrastructure.auth.providers.auth0.jwt.decode', side_effect=JWTClaimsError("Invalid audience")):
                with patch('faultmaven.infrastructure.auth.providers.auth0.jwt.get_unverified_header', return_value={"kid": "test-key-id"}):
                    with patch('faultmaven.infrastructure.auth.providers.auth0.jwk.construct') as mock_construct:
                        mock_key = MagicMock()
                        mock_construct.return_value = mock_key
                        
                        user = await auth0_provider.validate_token(token)
                        assert user is None

    @pytest.mark.asyncio
    async def test_validate_token_fallback_roles(self, auth0_provider, mock_jwks):
        """Test that token without custom roles uses fallback."""
        jwks_dict = mock_jwks
        token = "fallback-roles-token"
        
        now = datetime.now(timezone.utc)
        claims = {
            "sub": "auth0|user-123",
            "email": "user@example.com",
            "aud": "test-api-audience",
            "iss": "https://test-tenant.auth0.com/",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "roles": ["user"],  # Standard claim, not custom
        }
        
        with patch.object(auth0_provider, '_get_jwks', return_value=jwks_dict):
            with patch('faultmaven.infrastructure.auth.providers.auth0.jwt.decode', return_value=claims):
                with patch('faultmaven.infrastructure.auth.providers.auth0.jwt.get_unverified_header', return_value={"kid": "test-key-id"}):
                    with patch('faultmaven.infrastructure.auth.providers.auth0.jwk.construct') as mock_construct:
                        mock_key = MagicMock()
                        mock_construct.return_value = mock_key
                        
                        user = await auth0_provider.validate_token(token)
                        
                        assert user is not None
                        assert user.roles == ["user"]

    @pytest.mark.asyncio
    async def test_get_jwks_caches_result(self, auth0_provider):
        """Test that JWKS is cached after first fetch."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"keys": []}
        mock_response.raise_for_status = MagicMock()
        
        with patch('httpx.AsyncClient') as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_response)
            
            # First call
            jwks1 = await auth0_provider._get_jwks()
            
            # Second call should use cache
            jwks2 = await auth0_provider._get_jwks()
            
            # Should only call HTTP once
            assert mock_client.return_value.__aenter__.return_value.get.call_count == 1
            assert jwks1 == jwks2
