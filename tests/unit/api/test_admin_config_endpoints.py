"""Tests for admin configuration endpoints (Dashboard Phase 1a).

Tests:
- GET  /api/v1/admin/llm/config     — LLM provider status
- PUT  /api/v1/admin/llm/config     — Update LLM config
- POST /api/v1/admin/llm/config/test — Connection test
- GET  /api/v1/admin/config/status   — Environment status
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from faultmaven.api.models import LLMConfigUpdateRequest, LLMConnectionTestRequest
from faultmaven.api.routes.admin_config import (
    check_llm_connection,
    get_env_config_status,
    get_llm_config,
    update_llm_config,
)
from faultmaven.models.auth import AuthenticatedUser

SETTINGS_PATCH = "faultmaven.config.settings.get_settings"


# ============================================================
# Fixtures
# ============================================================


@pytest.fixture
def mock_admin_user():
    """Mock authenticated admin user."""
    return AuthenticatedUser(
        user_id="admin_123",
        organization_id="org_123",
        email="admin@example.com",
        roles=["admin"],
        permissions=["admin:all"],
    )


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider with registry, provider status, and health data."""
    provider = MagicMock()

    # Registry mock
    registry = MagicMock()
    registry.get_provider_status.return_value = {
        "anthropic": {
            "available": True,
            "models": ["claude-3-5-sonnet-20241022"],
            "confidence_score": 0.85,
            "in_fallback_chain": True,
        },
        "fireworks": {
            "available": True,
            "models": ["llama-v3p1-405b-instruct"],
            "confidence_score": 0.9,
            "in_fallback_chain": True,
        },
    }
    registry.get_provider_health_summary.return_value = {
        "anthropic": {
            "health": "healthy",
            "consecutive_failures": 0,
            "avg_latency_ms": 450.2,
            "sticky": True,
            "last_success": 1707402345.67,
            "last_failure": 0.0,
        },
        "fireworks": {
            "health": "degraded",
            "consecutive_failures": 1,
            "avg_latency_ms": 230.5,
            "sticky": False,
            "last_success": 1707402000.0,
            "last_failure": 1707402100.0,
        },
    }
    registry.get_fallback_chain.return_value = ["anthropic", "fireworks"]
    registry.get_available_providers.return_value = ["anthropic", "fireworks"]
    registry.get_all_provider_names.return_value = [
        "fireworks",
        "openai",
        "local",
        "gemini",
        "huggingface",
        "openrouter",
        "anthropic",
        "groq",
        "cohere",
    ]

    # Provider access for connection test
    mock_anthropic = MagicMock()
    mock_anthropic.generate = AsyncMock(
        return_value=MagicMock(model="claude-3-5-sonnet-20241022")
    )
    registry.get_provider.return_value = mock_anthropic

    provider.registry = registry
    return provider


@pytest.fixture
def mock_settings():
    """Mock FaultMavenSettings for config endpoints."""
    settings = MagicMock()
    settings.llm.strict_provider_mode = False
    settings.llm.provider = MagicMock(value="anthropic")
    settings.llm.anthropic_api_key = MagicMock()
    settings.llm.anthropic_api_key.get_secret_value.return_value = "sk-ant-test123"
    settings.llm.openai_api_key = None
    settings.llm.fireworks_api_key = MagicMock()
    settings.llm.fireworks_api_key.get_secret_value.return_value = "fw-test123"
    settings.llm.groq_api_key = None
    settings.llm.gemini_api_key = None
    settings.llm.huggingface_api_key = None
    settings.llm.cohere_api_key = None
    settings.llm.openrouter_api_key = None
    settings.auth.auth_mode = "local"
    settings.server.environment = MagicMock(value="development")
    settings.database.case_storage_type = "sqlite"
    settings.database.session_storage_type = "inmemory"
    settings.database.vector_storage_type = "inmemory"
    settings.protection.protection_enabled = False
    settings.security.rate_limit_enabled = True
    return settings


# ============================================================
# GET /api/v1/admin/llm/config
# ============================================================


class TestGetLLMConfig:
    """Tests for get_llm_config endpoint."""

    @pytest.mark.asyncio
    async def test_returns_provider_status(
        self, mock_admin_user, mock_llm_provider, mock_settings
    ):
        """Returns provider details with fallback chain."""
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_llm_config(
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )

        assert result.primary_provider == "anthropic"
        assert result.fallback_chain == ["anthropic", "fireworks"]
        assert result.strict_mode is False
        assert "anthropic" in result.providers
        assert "fireworks" in result.providers
        assert result.timestamp is not None

    @pytest.mark.asyncio
    async def test_provider_details_structure(
        self, mock_admin_user, mock_llm_provider, mock_settings
    ):
        """Each provider has correct fields populated."""
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_llm_config(
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )

        anthropic = result.providers["anthropic"]
        assert anthropic.name == "anthropic"
        assert anthropic.display_name == "Anthropic"
        assert anthropic.enabled is True
        assert anthropic.connected is True
        assert anthropic.has_api_key is True
        assert anthropic.health == "healthy"
        assert "claude-3-5-sonnet-20241022" in anthropic.models

    @pytest.mark.asyncio
    async def test_api_keys_never_exposed(
        self, mock_admin_user, mock_llm_provider, mock_settings
    ):
        """API key values are never in the response — only has_api_key boolean."""
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_llm_config(
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )

        for provider_detail in result.providers.values():
            assert not hasattr(provider_detail, "api_key")
            assert isinstance(provider_detail.has_api_key, bool)

    @pytest.mark.asyncio
    async def test_has_api_key_false_when_not_configured(
        self, mock_admin_user, mock_llm_provider, mock_settings
    ):
        """Providers without API keys show has_api_key=False."""
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_llm_config(
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )

        # openai key is None in mock_settings
        openai = result.providers.get("openai")
        if openai:
            assert openai.has_api_key is False

    @pytest.mark.asyncio
    async def test_uninitialized_providers_shown(
        self, mock_admin_user, mock_llm_provider, mock_settings
    ):
        """Providers in schema but not initialized show as not_initialized."""
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_llm_config(
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )

        # groq is in PROVIDER_SCHEMA but not in the mock registry
        groq = result.providers.get("groq")
        if groq:
            assert groq.enabled is False
            assert groq.health == "not_initialized"

    @pytest.mark.asyncio
    async def test_503_when_llm_not_available(self, mock_admin_user):
        """Returns 503 when LLM provider is not initialized."""
        with pytest.raises(HTTPException) as exc_info:
            await get_llm_config(
                current_user=mock_admin_user,
                llm_provider=None,
            )
        assert exc_info.value.status_code == 503


# ============================================================
# PUT /api/v1/admin/llm/config
# ============================================================


class TestUpdateLLMConfig:
    """Tests for update_llm_config endpoint."""

    @pytest.fixture(autouse=True)
    def _cloud_mode(self, mock_settings):
        """PUT tests require cloud mode (oauth) since local mode is read-only."""
        mock_settings.auth.auth_mode = "oauth"
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            yield

    @pytest.mark.asyncio
    async def test_local_mode_returns_403(
        self, mock_admin_user, mock_llm_provider, mock_settings
    ):
        """Local deployment rejects config writes with 403."""
        mock_settings.auth.auth_mode = "local"
        request = LLMConfigUpdateRequest(primary_provider="fireworks")
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            with pytest.raises(HTTPException) as exc_info:
                await update_llm_config(
                    request=request,
                    current_user=mock_admin_user,
                    llm_provider=mock_llm_provider,
                )
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_update_primary_provider(self, mock_admin_user, mock_llm_provider):
        """Updating primary provider persists override and reloads."""
        request = LLMConfigUpdateRequest(primary_provider="fireworks")

        with patch(
            "faultmaven.config.llm_config_overrides.save_and_reload",
            new_callable=AsyncMock,
        ) as mock_set:
            result = await update_llm_config(
                request=request,
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )

        assert "primary_provider" in result.updated_keys
        assert result.message == "Configuration updated and applied"
        mock_set.assert_called_once()
        call_args = mock_set.call_args
        assert call_args[0][0]["primary_provider"] == "fireworks"

    @pytest.mark.asyncio
    async def test_update_api_key(self, mock_admin_user, mock_llm_provider):
        """Updating an API key persists and masks the key name in response."""
        request = LLMConfigUpdateRequest(
            provider_name="anthropic",
            api_key="sk-ant-new-key-123",
        )

        with patch(
            "faultmaven.config.llm_config_overrides.save_and_reload",
            new_callable=AsyncMock,
        ) as mock_set:
            result = await update_llm_config(
                request=request,
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )

        assert "anthropic_api_key_updated" in result.updated_keys
        # Verify the actual key was passed to set_overrides
        call_args = mock_set.call_args
        assert call_args[0][0]["anthropic_api_key"] == "sk-ant-new-key-123"

    @pytest.mark.asyncio
    async def test_unknown_provider_returns_422(
        self, mock_admin_user, mock_llm_provider
    ):
        """Unknown primary_provider returns 422."""
        request = LLMConfigUpdateRequest(primary_provider="nonexistent")

        with pytest.raises(HTTPException) as exc_info:
            await update_llm_config(
                request=request,
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_update_returns_no_changes(
        self, mock_admin_user, mock_llm_provider
    ):
        """Empty request returns no-op response."""
        request = LLMConfigUpdateRequest()

        result = await update_llm_config(
            request=request,
            current_user=mock_admin_user,
            llm_provider=mock_llm_provider,
        )

        assert result.updated_keys == []
        assert result.message == "No changes requested"

    @pytest.mark.asyncio
    async def test_503_when_llm_not_available(self, mock_admin_user):
        """Returns 503 when LLM provider is not initialized."""
        request = LLMConfigUpdateRequest(primary_provider="anthropic")

        with pytest.raises(HTTPException) as exc_info:
            await update_llm_config(
                request=request,
                current_user=mock_admin_user,
                llm_provider=None,
            )
        assert exc_info.value.status_code == 503


# ============================================================
# POST /api/v1/admin/llm/config/test
# ============================================================


class TestLLMConnectionCheck:
    """Tests for check_llm_connection endpoint."""

    @pytest.mark.asyncio
    async def test_successful_connection(self, mock_admin_user, mock_llm_provider):
        """Successful provider test returns connected=True with latency."""
        request = LLMConnectionTestRequest(provider="anthropic")

        result = await check_llm_connection(
            request=request,
            current_user=mock_admin_user,
            llm_provider=mock_llm_provider,
        )

        assert result.provider == "anthropic"
        assert result.connected is True
        assert result.response_time_ms >= 0
        assert result.error_message is None
        assert result.model_used == "claude-3-5-sonnet-20241022"

    @pytest.mark.asyncio
    async def test_failed_connection(self, mock_admin_user, mock_llm_provider):
        """Failed provider test returns connected=False with error message."""
        mock_provider = MagicMock()
        mock_provider.generate = AsyncMock(side_effect=Exception("API key invalid"))
        mock_llm_provider.registry.get_provider.return_value = mock_provider

        request = LLMConnectionTestRequest(provider="anthropic")

        result = await check_llm_connection(
            request=request,
            current_user=mock_admin_user,
            llm_provider=mock_llm_provider,
        )

        assert result.provider == "anthropic"
        assert result.connected is False
        assert "API key invalid" in result.error_message

    @pytest.mark.asyncio
    async def test_uninitialized_provider(self, mock_admin_user, mock_llm_provider):
        """Testing an uninitialized provider returns connected=False."""
        mock_llm_provider.registry.get_provider.return_value = None
        mock_llm_provider.registry.create_provider_for_test.return_value = None

        request = LLMConnectionTestRequest(provider="groq")

        result = await check_llm_connection(
            request=request,
            current_user=mock_admin_user,
            llm_provider=mock_llm_provider,
        )

        assert result.provider == "groq"
        assert result.connected is False
        assert result.error_message is not None
        assert (
            "groq" in result.error_message.lower()
            or "api key" in result.error_message.lower()
        )

    @pytest.mark.asyncio
    async def test_unknown_provider_returns_422(
        self, mock_admin_user, mock_llm_provider
    ):
        """Unknown provider name returns 422."""
        request = LLMConnectionTestRequest(provider="nonexistent")

        with pytest.raises(HTTPException) as exc_info:
            await check_llm_connection(
                request=request,
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_503_when_llm_not_available(self, mock_admin_user):
        """Returns 503 when LLM provider is not initialized."""
        request = LLMConnectionTestRequest(provider="anthropic")

        with pytest.raises(HTTPException) as exc_info:
            await check_llm_connection(
                request=request,
                current_user=mock_admin_user,
                llm_provider=None,
            )
        assert exc_info.value.status_code == 503


# ============================================================
# GET /api/v1/admin/config/status
# ============================================================


class TestGetEnvConfigStatus:
    """Tests for get_env_config_status endpoint."""

    @pytest.mark.asyncio
    async def test_returns_env_config(self, mock_admin_user, mock_settings):
        """Returns all environment configuration fields."""
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(current_user=mock_admin_user)

        assert result.auth_mode == "local"
        assert result.deployment == "local"
        assert result.db_backend == "sqlite"
        assert result.session_storage == "inmemory"
        assert result.vector_storage == "inmemory"
        assert result.llm_provider == "anthropic"
        assert result.pii_redaction_enabled is False
        assert result.rate_limit_enabled is True
        assert result.timestamp is not None

    @pytest.mark.asyncio
    async def test_cloud_config(self, mock_admin_user, mock_settings):
        """Returns correct values for cloud deployment config."""
        mock_settings.auth.auth_mode = "oauth"
        mock_settings.server.environment = MagicMock(value="production")
        mock_settings.database.case_storage_type = "postgresql"
        mock_settings.database.session_storage_type = "redis"
        mock_settings.database.vector_storage_type = "chromadb"
        mock_settings.protection.protection_enabled = True

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(current_user=mock_admin_user)

        assert result.auth_mode == "oauth"
        assert result.deployment == "cloud"
        assert result.db_backend == "postgresql"
        assert result.session_storage == "redis"
        assert result.vector_storage == "chromadb"
        assert result.pii_redaction_enabled is True
