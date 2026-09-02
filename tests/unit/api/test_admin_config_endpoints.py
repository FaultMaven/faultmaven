"""Tests for admin configuration endpoints (Dashboard Phase 1a).

Tests:
- GET  /api/v1/admin/llm/config     — LLM provider status
- PUT  /api/v1/admin/llm/config     — Update LLM config
- POST /api/v1/admin/llm/config/test — Connection test
- GET  /api/v1/admin/config/status   — Environment status
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request

from faultmaven.api.models import LLMConfigUpdateRequest, LLMConnectionTestRequest
from faultmaven.api.routes.admin_config import (
    check_llm_connection,
    get_env_config_status,
    get_llm_config,
    update_llm_config,
)
from faultmaven.config.settings import Environment
from faultmaven.modules.auth.domain.models.auth import AuthenticatedUser

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
    # Set explicitly: on a MagicMock this attribute would auto-create as a
    # truthy object, so the consent-skip feature would report *enabled* on a
    # deployment that had pinned nothing — the one answer this endpoint exists
    # to give correctly.
    settings.auth.oauth_first_party_redirect_patterns = []
    # Every other input the feature predicates read, likewise explicit. An
    # unset MagicMock attribute does not merely leave a gap here, it
    # MANUFACTURES an enabled feature: the attribute is truthy, and pydantic
    # coerces it — measured, ``FeatureStatus(enabled=MagicMock()).enabled`` is
    # ``True``. A test asserting ``enabled is True`` against an unset field is
    # therefore asserting against itself, which is how the endpoint reported
    # tracing as on for as long as it did (#1234).
    settings.auth.oauth_enabled = False
    settings.auth.oauth_first_party_clients = []
    settings.observability.opik_enabled = False
    settings.observability.opik_api_key = None
    settings.observability.opik_use_local = False
    settings.knowledge.enable_web_search = False
    settings.knowledge.tavily_api_key = None
    settings.tools.web_search_api_key = None
    settings.tools.web_search_engine_id = None
    settings.is_cloud = False  # standalone (canonical DEPLOYMENT_MODE, ADR-004)
    settings.server.environment = MagicMock(value="development")
    # A real int, not a MagicMock: the endpoint compares it (#1214 reports
    # whether the per-worker suggestion store is worker-safe), and a bare
    # MagicMock attribute would make every test here fail on the comparison
    # rather than on what it is about.
    settings.server.workers = 1
    settings.database.case_storage_type = "sqlite"
    settings.database.session_storage_type = "inmemory"
    settings.database.vector_storage_type = "chromadb"
    settings.protection.protection_enabled = False
    return settings


def _request_for(app: FastAPI) -> Request:
    """A Request carrying a REAL app, because ``rate_limit_enabled`` reads it.

    The field is derived from ``app.user_middleware``, so a ``MagicMock`` app
    would answer the question with a mock and assert nothing. Building the app
    for real is the point: the assertion then depends on whether
    ``RateLimitMiddleware`` was actually installed.
    """
    return Request({"type": "http", "method": "GET", "path": "/", "app": app})


@pytest.fixture
def rate_limited_app() -> FastAPI:
    """An app wired by the REAL production path, not by a hand-rolled stand-in.

    ``setup_protection_middleware`` is what ``main.py`` calls, so building the
    fixture with it means the test cannot pass by agreeing with itself: if that
    function ever installs something ``_rate_limiting_installed`` does not
    recognise, this goes red rather than quietly measuring a fixture nobody
    runs. ``production`` is the default preset — every environment but
    ``development`` lands on it (fm#1023).
    """
    from faultmaven.api.protection import setup_protection_middleware

    app = FastAPI()
    info = setup_protection_middleware(app, environment=Environment.PRODUCTION)
    # The fixture's own premise, asserted: a preset that stopped installing rate
    # limiting would otherwise make every test below vacuously agree with an
    # unprotected app.
    assert "rate_limiting" in info["middleware_added"]
    return app


@pytest.fixture
def unprotected_app() -> FastAPI:
    """An app with NO protection middleware.

    Not hypothetical: the development carve-out in ``main.py`` boots this way
    when protection setup raises. ``SKIP_SERVICE_CHECKS=True`` used to skip
    ``setup_protection_middleware`` outright as well, until fm#990 removed that
    gate; the carve-out is now the only route to an app in this state.
    """
    return FastAPI()


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
    async def test_config_sources_all_env_default_in_standalone(
        self, mock_admin_user, mock_llm_provider, mock_settings
    ):
        """Standalone has no DB overrides: every overridable key is env-default."""
        from faultmaven.config.llm_config_overrides import _ALLOWED_OVERRIDES

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_llm_config(
                current_user=mock_admin_user, llm_provider=mock_llm_provider
            )

        assert set(result.config_sources) == set(_ALLOWED_OVERRIDES)
        assert all(v == "env-default" for v in result.config_sources.values())

    @pytest.mark.asyncio
    async def test_config_sources_marks_admin_overrides_in_cloud(
        self, mock_admin_user, mock_llm_provider, mock_settings
    ):
        """Cloud: keys present in the DB are admin-override; the rest env-default."""
        mock_settings.is_cloud = True
        overrides = {"gemini_model": "gemini-2.5-flash", "primary_provider": "gemini"}

        with (
            patch(SETTINGS_PATCH, return_value=mock_settings),
            patch(
                "faultmaven.infrastructure.persistence.llm_config_repository.get_all_overrides",
                new=AsyncMock(return_value=overrides),
            ),
        ):
            result = await get_llm_config(
                current_user=mock_admin_user, llm_provider=mock_llm_provider
            )

        assert result.config_sources["gemini_model"] == "admin-override"
        assert result.config_sources["primary_provider"] == "admin-override"
        assert result.config_sources["anthropic_model"] == "env-default"
        assert result.config_sources["openai_api_key"] == "env-default"

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
        """PUT tests require cloud mode since standalone is read-only."""
        mock_settings.auth.auth_mode = "oauth"
        mock_settings.is_cloud = True
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            yield

    @pytest.mark.asyncio
    async def test_local_mode_returns_403(
        self, mock_admin_user, mock_llm_provider, mock_settings
    ):
        """Standalone deployment rejects config writes with 403."""
        mock_settings.auth.auth_mode = "local"
        mock_settings.is_cloud = False  # override the class-level cloud fixture
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
    async def test_incapable_model_rejected_with_422(
        self, mock_admin_user, mock_llm_provider
    ):
        """A model that cannot serve the engine's response schemas is refused at
        the point of change. The startup gate only covers boot; this endpoint
        hot-reloads config afterwards, so without this check an operator could
        swap in a model the engine cannot drive and the deployment would only find
        out several turns into the next live investigation."""
        request = LLMConfigUpdateRequest(
            provider_name="gemini", model="gemini-2.5-flash"
        )
        provider = MagicMock()
        provider.supports_engine_response_schemas = MagicMock(return_value=False)

        mock_llm_provider.registry.get_provider.return_value = provider

        with patch(
            "faultmaven.config.llm_config_overrides.save_and_reload",
            new_callable=AsyncMock,
        ) as mock_set:
            with pytest.raises(HTTPException) as exc_info:
                await update_llm_config(
                    request=request,
                    current_user=mock_admin_user,
                    llm_provider=mock_llm_provider,
                )

        assert exc_info.value.status_code == 422
        assert "gemini-2.5-flash" in exc_info.value.detail
        # Nothing persisted — the rejection happens before save_and_reload.
        mock_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_capable_model_is_accepted(self, mock_admin_user, mock_llm_provider):
        """The converse: a capable model still saves, so the guard cannot have
        become a blanket refusal of model changes."""
        request = LLMConfigUpdateRequest(
            provider_name="gemini", model="gemini-3.5-flash"
        )
        provider = MagicMock()
        provider.supports_engine_response_schemas = MagicMock(return_value=True)

        mock_llm_provider.registry.get_provider.return_value = provider

        with patch(
            "faultmaven.config.llm_config_overrides.save_and_reload",
            new_callable=AsyncMock,
        ) as mock_set:
            result = await update_llm_config(
                request=request,
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )

        assert "gemini_model" in result.updated_keys
        assert mock_set.call_args[0][0]["gemini_model"] == "gemini-3.5-flash"

    @pytest.mark.asyncio
    async def test_model_change_allowed_when_capacity_unknown(
        self, mock_admin_user, mock_llm_provider
    ):
        """Fails OPEN, matching the startup gate: a provider that does not report
        capacity must not have its model changes refused on speculation."""
        request = LLMConfigUpdateRequest(provider_name="gemini", model="gemini-9.9-new")

        # A provider object that does not report capacity at all.
        mock_llm_provider.registry.get_provider.return_value = object()

        with patch(
            "faultmaven.config.llm_config_overrides.save_and_reload",
            new_callable=AsyncMock,
        ) as mock_set:
            result = await update_llm_config(
                request=request,
                current_user=mock_admin_user,
                llm_provider=mock_llm_provider,
            )

        assert "gemini_model" in result.updated_keys
        mock_set.assert_called_once()

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
    async def test_returns_env_config(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Returns all environment configuration fields."""
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.auth_mode == "local"
        assert result.deployment == "standalone"
        assert result.db_backend == "sqlite"
        assert result.session_storage == "fakeredis (inmemory)"
        assert result.vector_storage == "chromadb"
        assert result.llm_provider == "anthropic"
        assert result.pii_redaction_enabled is False
        assert result.rate_limit_enabled is True
        assert result.timestamp is not None

    @pytest.mark.asyncio
    async def test_cloud_config(
        self, mock_admin_user, mock_settings, rate_limited_app, tmp_path, monkeypatch
    ):
        """Returns correct values for cloud deployment config."""
        mock_settings.auth.auth_mode = "oauth"
        mock_settings.is_cloud = True
        mock_settings.server.environment = MagicMock(value="production")
        mock_settings.database.case_storage_type = "postgresql"
        mock_settings.database.session_storage_type = "redis"
        mock_settings.database.redis_url = "redis://localhost:6379"
        mock_settings.database.vector_storage_type = "chromadb"
        mock_settings.protection.protection_enabled = True

        # Run from temp dir so alembic.ini is not found (avoids sqlite override)
        monkeypatch.chdir(tmp_path)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.auth_mode == "oauth"
        assert result.deployment == "cloud"
        assert result.db_backend == "postgresql"
        assert result.session_storage == "redis"
        assert result.vector_storage == "chromadb"
        assert result.pii_redaction_enabled is True

    @pytest.mark.asyncio
    async def test_consent_skip_reports_inactive_when_no_redirect_is_pinned(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """The state an operator cannot otherwise observe.

        An unpinned deployment renders the consent screen exactly as it always
        did, so "the skip never activated" looks identical to "the skip is
        working" from outside. This endpoint is where that is answerable — not
        a startup log line, which has rolled out of ``kubectl logs`` by the time
        anyone asks.
        """
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        feature = result.features["first_party_consent_skip"]
        assert feature.enabled is False
        assert "OAUTH_FIRST_PARTY_REDIRECT_PATTERNS" in feature.config_hint

    @pytest.mark.asyncio
    async def test_consent_skip_reports_active_once_a_redirect_is_pinned(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """The fully configured deployment — every half the skip needs.

        The redirect is the half that carries the proof, but it is not the
        whole condition: ``_is_first_party`` also requires the client list, and
        the router carrying the flow is mounted only under ``oauth_enabled``.
        Setting all three is what makes the True here mean "the skip can fire"
        rather than "a list is non-empty".
        """
        mock_settings.auth.oauth_enabled = True
        mock_settings.auth.oauth_first_party_clients = ["faultmaven-copilot"]
        mock_settings.auth.oauth_first_party_redirect_patterns = [
            r"^https://abcdefghijklmnopabcdefghijklmnop\.chromiumapp\.org/?$"
        ]

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features["first_party_consent_skip"].enabled is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing_half",
        ["oauth_enabled", "oauth_first_party_clients"],
        ids=["oauth-disabled", "no-client-listed"],
    )
    async def test_consent_skip_reports_inactive_when_a_required_half_is_missing(
        self, mock_admin_user, mock_settings, rate_limited_app, missing_half
    ):
        """A pinned redirect is necessary and NOT sufficient.

        ``_is_first_party`` returns False for every caller when the client list
        is empty, and there is no authorize endpoint at all when OAuth is off —
        so in both states no client skips consent, however carefully the
        redirect was pinned. The field used to read the redirect list alone and
        reported *enabled* in both, which is the same substitution as #1234:
        the configuration is echoed back where the effect was promised.

        Parametrised rather than written once because the halves fail
        independently: a fix that conjoined only one of them would pass the
        other case and look complete.
        """
        mock_settings.auth.oauth_enabled = True
        mock_settings.auth.oauth_first_party_clients = ["faultmaven-copilot"]
        mock_settings.auth.oauth_first_party_redirect_patterns = [
            r"^https://abcdefghijklmnopabcdefghijklmnop\.chromiumapp\.org/?$"
        ]
        setattr(
            mock_settings.auth,
            missing_half,
            False if missing_half == "oauth_enabled" else [],
        )

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features["first_party_consent_skip"].enabled is False

    @pytest.mark.asyncio
    async def test_consent_skip_matches_the_predicate_that_actually_gates_it(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Cross-check the report against ``_is_first_party`` itself.

        Both halves present is reported enabled AND the real gate admits the
        shipped client; drop the client list and both flip. Asserting the pair
        together is what makes this a claim about the skip rather than about
        this endpoint's own arithmetic.
        """
        from faultmaven.modules.auth.api.oauth import _is_first_party

        redirect = "https://abcdefghijklmnopabcdefghijklmnop.chromiumapp.org"
        mock_settings.auth.oauth_enabled = True
        mock_settings.auth.oauth_first_party_clients = ["faultmaven-copilot"]
        mock_settings.auth.oauth_first_party_redirect_patterns = [
            r"^https://abcdefghijklmnopabcdefghijklmnop\.chromiumapp\.org/?$"
        ]

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            configured = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )
        assert configured.features["first_party_consent_skip"].enabled is True
        assert _is_first_party("faultmaven-copilot", redirect, mock_settings) is True

        mock_settings.auth.oauth_first_party_clients = []
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            stripped = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )
        assert stripped.features["first_party_consent_skip"].enabled is False
        assert _is_first_party("faultmaven-copilot", redirect, mock_settings) is False

    async def _suggestion_store_feature(
        self, mock_admin_user, mock_settings, app, repository
    ):
        """Compose ``app.state.suggestion_service`` over ``repository`` and read
        the reported feature back."""
        from faultmaven.modules.knowledge.domain.services.suggestion_service import (
            SuggestionService,
        )

        if repository is not None:
            app.state.suggestion_service = SuggestionService(
                knowledge_service=MagicMock(), suggestion_repository=repository
            )

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(app), current_user=mock_admin_user
            )
        return result.features["suggestion_store_worker_safe"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("workers", [1, 4])
    async def test_the_database_backed_store_reports_safe_at_any_worker_count(
        self, mock_admin_user, mock_settings, rate_limited_app, workers
    ):
        """Since #1227 the store is ``knowledge_suggestions``, so extract and
        approve reach the same rows from any worker or pod.

        Swept over WORKERS deliberately. The field used to BE
        ``settings.server.workers <= 1``, and that proxy stopped being one the
        moment the store could be a database: the parametrisation is what makes
        this test fail against the old implementation rather than agree with it
        by coincidence at ``workers == 1``.
        """
        from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
            DatabaseSuggestionRepository,
        )

        mock_settings.server.workers = workers

        feature = await self._suggestion_store_feature(
            mock_admin_user,
            mock_settings,
            rate_limited_app,
            DatabaseSuggestionRepository(session_factory=MagicMock()),
        )

        assert feature.enabled is True

    @pytest.mark.asyncio
    async def test_the_in_memory_double_reports_unsafe_even_on_one_worker(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """The other half of the same point: a single worker over a dict is
        still a process that loses every pending review on restart, and
        ``WORKERS=1`` cannot see that."""
        from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
            InMemorySuggestionRepository,
        )

        mock_settings.server.workers = 1

        feature = await self._suggestion_store_feature(
            mock_admin_user,
            mock_settings,
            rate_limited_app,
            InMemorySuggestionRepository(),
        )

        assert feature.enabled is False

    @pytest.mark.asyncio
    async def test_no_composed_suggestion_service_reports_unsafe(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Composition failed, so the suggestion routes answer 503 — reported
        as not-safe rather than as an unqualified True, since there is no store
        at all to be safe."""
        mock_settings.server.workers = 1

        feature = await self._suggestion_store_feature(
            mock_admin_user, mock_settings, rate_limited_app, None
        )

        assert feature.enabled is False
        assert "database-backed" in feature.config_hint

    @pytest.mark.asyncio
    async def test_rate_limit_enabled_is_false_when_middleware_absent(
        self, mock_admin_user, mock_settings, unprotected_app
    ):
        """An unprotected app must not report itself rate limited.

        This is the half of fm#985 item 16 that mattered. The removed
        ``settings.security.rate_limit_enabled`` defaulted to ``True`` and was
        read by no enforcement path, so ``SKIP_SERVICE_CHECKS=True`` — which at
        the time installed no protection middleware at all — still reported
        *enabled*, and the dashboard drew a green "Rate Limiting: enabled" row
        over a deployment anyone could flood. fm#990 has since removed that
        gate, but the development carve-out still produces the same app, so the
        property this asserts is unchanged.
        """
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(unprotected_app), current_user=mock_admin_user
            )

        assert result.rate_limit_enabled is False

    # The "a retired env key cannot talk this field out of the truth" case is
    # NOT tested here, deliberately. This module patches ``get_settings`` to a
    # MagicMock, so a monkeypatched RATE_LIMIT_ENABLED never reaches a settings
    # object and the test would assert nothing while appearing to. It is tested
    # where it can bite, against a real ``SecuritySettings``:
    # tests/unit/config/test_settings_mapping.py::TestRateLimitingIsNotASetting.


# ============================================================
# features[*].enabled reports EFFECT, not configured intent (#1234)
# ============================================================


def _secret(value: str) -> MagicMock:
    """A ``SecretStr``-shaped double: truthy, with ``get_secret_value``."""
    key = MagicMock()
    key.get_secret_value.return_value = value
    return key


class TestLLMTracingReportsEffect:
    """``llm_tracing.enabled`` must mean "a span will be recorded" (#1234).

    ``FeatureStatus.enabled`` is documented as "Feature is active and usable",
    and ``OPIK_ENABLED=true`` does not establish that: ``opik`` ships only in
    pyproject's ``[cloud]`` extra, so on the default standalone install
    ``init_opik_tracing`` returns at its first line and nothing is ever traced,
    while this endpoint reported tracing on.

    BOTH directions are pinned deliberately. ``opik`` is genuinely absent from
    this repo's venv, so the SDK-absent assertion passes here for a reason that
    has nothing to do with the fix — it would hold just as well against a field
    hardcoded to False. Only the monkeypatched SDK-present arm shows that the
    conjunction is a conjunction.
    """

    @staticmethod
    def _set_opik_available(monkeypatch, value: bool) -> None:
        """Patch the flag ON THE MODULE the endpoint reads at call time.

        Patching the module attribute (rather than a name imported into the
        route module) is what makes this bite: the predicate is written to read
        ``tracing.OPIK_AVAILABLE`` fresh precisely so it tracks the SDK, and a
        from-imported copy would silently ignore this.
        """
        from faultmaven.infrastructure.observability import tracing

        monkeypatch.setattr(tracing, "OPIK_AVAILABLE", value)

    @pytest.mark.asyncio
    async def test_configured_but_sdk_absent_reports_disabled(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch
    ):
        """#1234's case: the standalone install with OPIK_ENABLED=true."""
        mock_settings.observability.opik_enabled = True
        self._set_opik_available(monkeypatch, False)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features["llm_tracing"].enabled is False

    @pytest.mark.asyncio
    async def test_configured_with_sdk_present_reports_enabled(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch
    ):
        """The other direction, which the environment cannot supply.

        Without this arm ``enabled=False`` would satisfy the whole class on
        this machine, so a fix that simply hardcoded the feature off would
        read as green.
        """
        mock_settings.observability.opik_enabled = True
        self._set_opik_available(monkeypatch, True)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features["llm_tracing"].enabled is True

    @pytest.mark.asyncio
    async def test_not_configured_reports_disabled_even_with_the_sdk_present(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch
    ):
        """An installed SDK is not an instruction to trace.

        The positive control for the other conjunct: with the SDK available,
        ``OPIK_ENABLED=false`` must still read False, so the field cannot have
        degenerated into "is opik importable".
        """
        mock_settings.observability.opik_enabled = False
        self._set_opik_available(monkeypatch, True)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features["llm_tracing"].enabled is False

    @pytest.mark.asyncio
    async def test_config_hint_names_the_install_the_operator_is_missing(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch
    ):
        """``enabled: false`` on a deployment that set OPIK_ENABLED=true is
        confusing unless the hint names the missing half."""
        mock_settings.observability.opik_enabled = True
        self._set_opik_available(monkeypatch, False)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert "cloud" in result.features["llm_tracing"].config_hint


class TestWebSearchReportsEffect:
    """``web_search.enabled`` must track the provider the DA registry composes.

    The registry (``container/providers/tools.py``) registers web search when
    ``WebSearchTool(settings=settings).is_available()``, and
    ``_auto_select_provider`` supports Google CSE as a full fallback to Tavily.
    Reporting a Tavily-only check said *disabled* on a CSE deployment where the
    model could demonstrably search — the same intent-for-effect substitution
    as #1234, pointing the other way.
    """

    @staticmethod
    def _configure_google_cse(settings) -> None:
        settings.knowledge.tavily_api_key = None
        settings.tools.web_search_api_key = _secret("google-key")
        settings.tools.web_search_engine_id = "cse-id"
        settings.tools.web_search_api_endpoint = "https://example.invalid"

    @pytest.mark.asyncio
    async def test_google_cse_only_deployment_reports_enabled(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Cross-checked against the tool, not against a second copy of the
        rule: the same settings must compose a provider that reports itself
        available, so the report cannot be right about a capability that is
        not there."""
        from faultmaven.modules.agent.tools.web_search import WebSearchTool

        self._configure_google_cse(mock_settings)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert WebSearchTool(settings=mock_settings).is_available() is True
        assert result.features["web_search"].enabled is True
        assert result.features["web_search"].has_api_key is True

    @pytest.mark.asyncio
    async def test_tavily_deployment_reports_enabled(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        mock_settings.knowledge.tavily_api_key = _secret("tvly-key")

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features["web_search"].enabled is True
        assert result.features["web_search"].has_api_key is True

    @pytest.mark.asyncio
    async def test_no_provider_configured_reports_disabled(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """The positive control. Without it every assertion above could be
        satisfied by a field wired to a constant ``True``."""
        from faultmaven.modules.agent.tools.web_search import WebSearchTool

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert WebSearchTool(settings=mock_settings).is_available() is False
        assert result.features["web_search"].enabled is False
        assert result.features["web_search"].has_api_key is False

    @pytest.mark.asyncio
    async def test_google_cse_key_without_an_engine_id_reports_disabled(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """CSE needs both halves — ``_auto_select_provider`` composes nothing
        from a key alone, so a half-configured deployment must not read as
        working."""
        mock_settings.knowledge.tavily_api_key = None
        mock_settings.tools.web_search_api_key = _secret("google-key")
        mock_settings.tools.web_search_engine_id = None

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features["web_search"].enabled is False
        assert result.features["web_search"].has_api_key is False

    @pytest.mark.asyncio
    async def test_config_hint_names_both_supported_providers(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """The hint used to name only Tavily, and to name ENABLE_WEB_SEARCH,
        which no production path reads."""
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        hint = result.features["web_search"].config_hint
        assert "TAVILY_API_KEY" in hint
        assert "WEB_SEARCH_ENGINE_ID" in hint


# ============================================================
# The population rule (#1234)
# ============================================================
#
# Each scenario configures ONE feature and is called twice: with
# ``reality=False`` it sets every knob an operator can reach for that feature
# and withholds the runtime capability; with ``reality=True`` it additionally
# supplies the capability. Both mutate ``settings``/``app`` in place.
#
# ONE registry, and the sweep parametrises off it. A hand-written list beside
# the registry is how a population test quietly stops covering a member: add a
# scenario, forget the list, and the entry is never exercised while the
# exhaustiveness check keeps passing because the registry did grow.


def _scenario_llm_tracing(settings, app, monkeypatch, reality):
    from faultmaven.infrastructure.observability import tracing

    settings.observability.opik_enabled = True
    settings.observability.opik_api_key = _secret("opik-key")
    monkeypatch.setattr(tracing, "OPIK_AVAILABLE", reality)


def _scenario_web_search(settings, app, monkeypatch, reality):
    # ``ENABLE_WEB_SEARCH`` is the only pure-intent knob web search has, and it
    # is switched ON in both arms — including the one that must report False.
    # It was conjoined into ``enabled`` until this fix, so a regression to that
    # shape shows up here rather than in a comment.
    settings.knowledge.enable_web_search = True
    if reality:
        settings.knowledge.tavily_api_key = _secret("tvly-key")
    else:
        # No provider resolvable: no key for EITHER provider, which is all
        # ``_auto_select_provider`` consults.
        settings.knowledge.tavily_api_key = None
        settings.tools.web_search_api_key = None
        settings.tools.web_search_engine_id = None


def _scenario_first_party_consent_skip(settings, app, monkeypatch, reality):
    # The pinned redirect — the knob operators actually set, and the one the
    # field used to report on its own — is present in BOTH arms.
    settings.auth.oauth_enabled = True
    settings.auth.oauth_first_party_redirect_patterns = [
        r"^https://abcdefghijklmnopabcdefghijklmnop\.chromiumapp\.org/?$"
    ]
    settings.auth.oauth_first_party_clients = ["faultmaven-copilot"] if reality else []


def _scenario_suggestion_store_worker_safe(settings, app, monkeypatch, reality):
    from faultmaven.modules.knowledge.domain.services.suggestion_service import (
        SuggestionService,
    )
    from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
        DatabaseSuggestionRepository,
        InMemorySuggestionRepository,
    )

    # The control that was already correct (#1227): it reads no setting at all,
    # so WORKERS=1 — the knob operators used to reach for — is set in both arms
    # precisely to show it buys nothing.
    settings.server.workers = 1
    repository = (
        DatabaseSuggestionRepository(session_factory=MagicMock())
        if reality
        else InMemorySuggestionRepository()
    )
    app.state.suggestion_service = SuggestionService(
        knowledge_service=MagicMock(), suggestion_repository=repository
    )


FEATURE_SCENARIOS = {
    "web_search": _scenario_web_search,
    "llm_tracing": _scenario_llm_tracing,
    "first_party_consent_skip": _scenario_first_party_consent_skip,
    "suggestion_store_worker_safe": _scenario_suggestion_store_worker_safe,
}


class TestEveryFeatureReportsEffectNotIntent:
    """The population rule, swept over the whole ``features`` dict.

    #1234 was raised against ``llm_tracing``, but the contract it broke
    (``FeatureStatus.enabled`` — "Feature is active and usable") is one
    sentence covering every entry, and two of the four were breaking it. The
    per-feature tests above pin each case; this pins the RULE, so a fifth
    feature added as ``enabled=<some setting>`` fails here rather than shipping
    and being found the way this one was.

    The rule, stated so it is falsifiable for any feature: **there is a state
    in which the feature's configuration knobs are set and it still reports
    False, because the runtime capability is absent.** That is exactly the
    state #1234 mis-reported, and a field wired to a setting cannot satisfy it.

    Every feature supplies BOTH arms. There is deliberately no "effect is
    unknowable" bucket: each of these four has a reachable runtime signal, and
    an entry that genuinely had none would have to argue for it here rather
    than drop out of the sweep in silence.
    """

    @pytest.mark.asyncio
    async def test_every_reported_feature_is_classified_here(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """The anti-vacuity guard, and the reason the sweep cannot rot.

        Equality in BOTH directions. A feature added to the endpoint without a
        scenario fails here — otherwise the sweep silently would not cover it,
        which is how a population test becomes a test of three things out of
        five. A scenario naming a feature the endpoint no longer emits fails
        too, so the sweep cannot keep passing over a shrunken dict.

        The count floor is the third leg: equality against an empty registry
        would still be equality.
        """
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert set(result.features) == set(FEATURE_SCENARIOS)
        assert len(result.features) >= 4

    @pytest.mark.asyncio
    @pytest.mark.parametrize("feature", sorted(FEATURE_SCENARIOS))
    async def test_configured_but_capability_absent_reports_disabled(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch, feature
    ):
        """The load-bearing arm: knobs set, capability absent.

        A field wired to a setting reports True here. That is #1234 stated as a
        property rather than as one endpoint's bug, and it is what the two
        broken entries failed.
        """
        FEATURE_SCENARIOS[feature](mock_settings, rate_limited_app, monkeypatch, False)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features[feature].enabled is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("feature", sorted(FEATURE_SCENARIOS))
    async def test_capability_present_reports_enabled(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch, feature
    ):
        """The other arm, without which "report False" satisfies everything.

        Every feature must be reachable, or the sweep degenerates into
        asserting that the endpoint reports nothing as working — which the
        shipped standalone default satisfies for three of the four, and which a
        fix that simply switched tracing off would satisfy for all of them.
        """
        FEATURE_SCENARIOS[feature](mock_settings, rate_limited_app, monkeypatch, True)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features[feature].enabled is True
