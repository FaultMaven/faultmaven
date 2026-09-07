"""Tests for LLM provider health monitoring endpoint"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from faultmaven.api.routes.admin import get_llm_routing_health
from faultmaven.infrastructure.llm.providers.registry import ProviderHealth
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser


@pytest.fixture
def mock_admin_user():
    """Mock authenticated admin user"""
    return AuthenticatedUser(
        user_id="admin_123",
        enterprise_id="org_123",
        email="admin@example.com",
        roles=["admin"],
        permissions=["admin:all"],
    )


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider with health data"""
    provider = MagicMock()

    # Mock provider status
    provider.get_provider_status.return_value = {
        "gemini": {
            "health": "healthy",
            "consecutive_failures": 0,
            "avg_latency_ms": 234.5,
            "sticky": True,
            "last_success": 1707402345.67,
            "last_failure": 0.0,
        },
        "fireworks": {
            "health": "degraded",
            "consecutive_failures": 1,
            "avg_latency_ms": 456.7,
            "sticky": False,
            "last_success": 1707402000.0,
            "last_failure": 1707402100.0,
        },
    }

    # Mock registry methods
    provider.registry = MagicMock()
    provider.registry.get_fallback_chain.return_value = ["gemini", "fireworks"]
    provider.registry.get_available_providers.return_value = ["gemini", "fireworks"]
    provider.registry._sticky_provider = "gemini"

    # Mock cache
    provider.cache = MagicMock()
    provider.cache.current_size = 5
    provider.cache.max_size = 100

    # Mock config
    provider.confidence_threshold = 0.8
    provider.request_timeout = 30.0

    return provider


class TestLLMHealthEndpoint:
    """Test LLM provider health monitoring endpoint"""

    @pytest.mark.asyncio
    async def test_get_llm_routing_health_success(
        self, mock_admin_user, mock_llm_provider
    ):
        """Test successful health retrieval"""
        result = await get_llm_routing_health(
            current_user=mock_admin_user,
            llm_provider=mock_llm_provider,
        )

        # Verify structure
        assert "providers" in result
        assert "routing" in result
        assert "cache" in result
        assert "config" in result

        # Verify provider status
        assert "gemini" in result["providers"]
        assert "fireworks" in result["providers"]
        assert result["providers"]["gemini"]["health"] == "healthy"
        assert result["providers"]["fireworks"]["health"] == "degraded"

        # Verify routing configuration
        assert result["routing"]["fallback_chain"] == ["gemini", "fireworks"]
        assert result["routing"]["sticky_provider"] == "gemini"

        # Verify cache metrics
        assert result["cache"]["size"] == 5
        assert result["cache"]["max_size"] == 100

        # Verify config
        assert result["config"]["confidence_threshold"] == 0.8
        assert result["config"]["request_timeout"] == 30.0

    @pytest.mark.asyncio
    async def test_get_llm_routing_health_no_provider(self, mock_admin_user):
        """Test when LLM provider is not configured"""
        with pytest.raises(HTTPException) as exc_info:
            await get_llm_routing_health(
                current_user=mock_admin_user,
                llm_provider=None,
            )

        assert exc_info.value.status_code == 503
        assert "not configured" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_llm_routing_health_error(
        self, mock_admin_user, mock_llm_provider
    ):
        """Test error handling when health check fails"""
        # Make get_provider_status raise an exception
        mock_llm_provider.get_provider_status.side_effect = Exception(
            "Health check failed"
        )

        with pytest.raises(HTTPException) as exc_info:
            await get_llm_routing_health(
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )

        assert exc_info.value.status_code == 500
        assert "Failed to get LLM routing health" in exc_info.value.detail
