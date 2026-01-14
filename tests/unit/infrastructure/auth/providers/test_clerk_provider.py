"""Unit tests for ClerkProvider (Cloud Deployment)

Tests JWT validation using Clerk secret key (HS256).
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest

from faultmaven.infrastructure.auth.providers.clerk import ClerkProvider
from faultmaven.modules.auth.domain.models import AuthenticatedUser


@pytest.fixture
def clerk_provider():
    """Create ClerkProvider instance."""
    return ClerkProvider(
        secret_key="test-secret-key-12345",
        audience="https://clerk.faultmaven.ai"
    )


@pytest.fixture
def valid_clerk_token(clerk_provider):
    """Create a valid Clerk JWT token."""
    now = datetime.now(timezone.utc)
    claims = {
        "sub": "user_abc123",
        "email": "user@example.com",
        "aud": "https://clerk.faultmaven.ai",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "https://faultmaven.ai/roles": ["admin"],
        "org_id": "org-789",
        "jti": "token-xyz"
    }
    
    token = jwt.encode(
        claims,
        clerk_provider.secret_key,
        algorithm="HS256"
    )
    
    return token, claims


class TestClerkProvider:
    """Tests for ClerkProvider."""

    def test_initialization(self, clerk_provider):
        """Test that ClerkProvider initializes correctly."""
        assert clerk_provider.secret_key == "test-secret-key-12345"
        assert clerk_provider.audience == "https://clerk.faultmaven.ai"

    def test_initialization_default_audience(self):
        """Test that default audience is used when not provided."""
        provider = ClerkProvider(secret_key="test-key")
        assert provider.audience == ClerkProvider.DEFAULT_AUDIENCE

    def test_is_enabled_returns_true(self, clerk_provider):
        """Test that is_enabled returns True (auth enabled)."""
        assert clerk_provider.is_enabled() is True

    @pytest.mark.asyncio
    async def test_validate_token_empty_returns_none(self, clerk_provider):
        """Test that empty token returns None."""
        user = await clerk_provider.validate_token("")
        assert user is None

    @pytest.mark.asyncio
    async def test_validate_token_invalid_returns_none(self, clerk_provider):
        """Test that invalid token returns None."""
        user = await clerk_provider.validate_token("invalid-token")
        assert user is None

    @pytest.mark.asyncio
    async def test_validate_token_valid_returns_user(self, clerk_provider, valid_clerk_token):
        """Test that valid token returns AuthenticatedUser."""
        token, claims = valid_clerk_token
        
        user = await clerk_provider.validate_token(token)
        
        assert user is not None
        assert isinstance(user, AuthenticatedUser)
        assert user.user_id == claims["sub"]
        assert user.email == claims["email"]
        assert user.organization_id == claims["org_id"]
        assert user.roles == claims["https://faultmaven.ai/roles"]
        assert user.token_jti == claims["jti"]

    @pytest.mark.asyncio
    async def test_validate_token_expired_returns_none(self, clerk_provider):
        """Test that expired token returns None."""
        now = datetime.now(timezone.utc)
        expired_claims = {
            "sub": "user_abc123",
            "email": "user@example.com",
            "aud": "https://clerk.faultmaven.ai",
            "iat": int((now - timedelta(hours=2)).timestamp()),
            "exp": int((now - timedelta(hours=1)).timestamp()),  # Expired
        }
        
        token = jwt.encode(
            expired_claims,
            clerk_provider.secret_key,
            algorithm="HS256"
        )
        
        user = await clerk_provider.validate_token(token)
        assert user is None

    @pytest.mark.asyncio
    async def test_validate_token_wrong_secret_returns_none(self, clerk_provider):
        """Test that token signed with wrong secret returns None."""
        now = datetime.now(timezone.utc)
        claims = {
            "sub": "user_abc123",
            "email": "user@example.com",
            "aud": "https://clerk.faultmaven.ai",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        }
        
        # Sign with different secret
        token = jwt.encode(
            claims,
            "wrong-secret-key",
            algorithm="HS256"
        )
        
        user = await clerk_provider.validate_token(token)
        assert user is None

    @pytest.mark.asyncio
    async def test_validate_token_wrong_audience_returns_none(self, clerk_provider):
        """Test that token with wrong audience returns None."""
        now = datetime.now(timezone.utc)
        claims = {
            "sub": "user_abc123",
            "email": "user@example.com",
            "aud": "wrong-audience",  # Wrong audience
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        }
        
        token = jwt.encode(
            claims,
            clerk_provider.secret_key,
            algorithm="HS256"
        )
        
        user = await clerk_provider.validate_token(token)
        assert user is None

    @pytest.mark.asyncio
    async def test_validate_token_fallback_roles(self, clerk_provider):
        """Test that token without custom roles uses fallback."""
        now = datetime.now(timezone.utc)
        claims = {
            "sub": "user_abc123",
            "email": "user@example.com",
            "aud": "https://clerk.faultmaven.ai",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
            "roles": ["user"],  # Standard claim, not custom
        }
        
        token = jwt.encode(
            claims,
            clerk_provider.secret_key,
            algorithm="HS256"
        )
        
        user = await clerk_provider.validate_token(token)
        
        assert user is not None
        assert user.roles == ["user"]

    @pytest.mark.asyncio
    async def test_validate_token_missing_org_id_uses_default(self, clerk_provider):
        """Test that missing org_id uses default value."""
        now = datetime.now(timezone.utc)
        claims = {
            "sub": "user_abc123",
            "email": "user@example.com",
            "aud": "https://clerk.faultmaven.ai",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        }
        
        token = jwt.encode(
            claims,
            clerk_provider.secret_key,
            algorithm="HS256"
        )
        
        user = await clerk_provider.validate_token(token)
        
        assert user is not None
        assert user.organization_id == "default"
