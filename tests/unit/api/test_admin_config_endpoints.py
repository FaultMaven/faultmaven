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
        enterprise_id="org_123",
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
    settings.auth.oauth_require_consent = True
    settings.auth.oauth_allowed_clients = []
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
    # Real numbers for the same reason as ``workers`` above: the endpoint models
    # the LLM retry ladder against the turn deadline (#1278/#1292) and does
    # arithmetic on both, so MagicMock attributes would fail every test here on
    # a TypeError rather than on what it is about. These are the shipped code
    # defaults, which is a FITTING configuration — 3x30 + 14 = 104s inside 120s.
    settings.llm.request_timeout = 30
    settings.llm.timeout_for_provider.return_value = 30
    settings.agent.agent_request_timeout = 120
    settings.agent.timeout_for_provider.return_value = 120
    # The self-service sign-up bounds (#1320, #1324), reported as VALUES
    # rather than as ``features`` entries. Real values for the same reason as
    # ``workers`` above and then some: the response model types two of them as
    # ``int``, so a MagicMock attribute would fail every test in this file on a
    # ValidationError rather than on what it is about. These are the shipped
    # defaults, which is the state an operator who has set nothing is in.
    settings.auth.sso_jit_personal_tenant_enabled = False
    settings.auth.sso_jit_personal_tenant_max_per_hour = 20
    settings.agent.tenant_daily_turn_cap = 30
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
        self, mock_admin_user, mock_settings, oauth_mounted_app
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
                request=_request_for(oauth_mounted_app), current_user=mock_admin_user
            )

        feature = result.features["first_party_consent_skip"]
        assert feature.enabled is False
        assert "OAUTH_FIRST_PARTY_REDIRECT_PATTERNS" in feature.config_hint

    @pytest.mark.asyncio
    async def test_consent_skip_reports_active_once_a_redirect_is_pinned(
        self, mock_admin_user, mock_settings, oauth_mounted_app
    ):
        """The fully configured deployment — every half the skip needs.

        The redirect is the half that carries the proof, but it is not the
        whole condition: ``_is_first_party`` also requires the client list,
        ``validate_authorization_request`` requires that client to be allowed
        at all, and the whole flow requires the OAuth router to be mounted.
        Setting all of them is what makes this True mean "the skip can fire".
        """
        _pin_first_party(mock_settings)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(oauth_mounted_app), current_user=mock_admin_user
            )

        assert result.features["first_party_consent_skip"].enabled is True

    @pytest.mark.asyncio
    async def test_consent_skip_reports_inactive_when_the_oauth_flow_is_not_mounted(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """No authorize endpoint, so no consent screen and nothing to skip.

        The RUNTIME half of the question, and the reason it is asked of the app
        rather than of ``oauth_enabled``: what matters is whether this process
        serves the flow, and ``rate_limited_app`` is a real app that does not.
        """
        _pin_first_party(mock_settings)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features["first_party_consent_skip"].enabled is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing_half",
        ["oauth_first_party_clients", "oauth_first_party_redirect_patterns"],
        ids=["no-client-listed", "no-redirect-pinned"],
    )
    async def test_consent_skip_reports_inactive_when_a_required_half_is_missing(
        self, mock_admin_user, mock_settings, oauth_mounted_app, missing_half
    ):
        """``_is_first_party`` needs a hit in BOTH lists, so either being empty
        means no client skips consent however carefully the other was set.

        Parametrised because the halves fail independently: a predicate that
        conjoined only one of them would pass the other case and look done.
        """
        _pin_first_party(mock_settings)
        setattr(mock_settings.auth, missing_half, [])

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(oauth_mounted_app), current_user=mock_admin_user
            )

        assert result.features["first_party_consent_skip"].enabled is False

    @pytest.mark.asyncio
    async def test_consent_skip_reports_inactive_when_the_client_is_not_allowed(
        self, mock_admin_user, mock_settings, oauth_mounted_app
    ):
        """A first-party client absent from ``oauth_allowed_clients`` never
        reaches ``_is_first_party`` at all.

        ``validate_authorization_request`` runs FIRST (oauth.py) and refuses an
        unknown client before any consent decision is made, so pinning a
        redirect for a client the deployment does not admit skips nothing.
        ``settings.py`` states the requirement; this is it, enforced.
        """
        _pin_first_party(mock_settings)
        mock_settings.auth.oauth_allowed_clients = ["some-other-client"]

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(oauth_mounted_app), current_user=mock_admin_user
            )

        assert result.features["first_party_consent_skip"].enabled is False

    @pytest.mark.asyncio
    async def test_no_consent_required_reports_the_outcome_as_active(
        self, mock_admin_user, mock_settings, oauth_mounted_app
    ):
        """``OAUTH_REQUIRE_CONSENT=false`` prompts nobody, so the outcome the
        feature describes holds for every client.

        The outermost term in the authorize leg (``if oauth_require_consent and
        not first_party``). Reporting this deployment as *disabled* — which the
        first version of the fix did — is the same substitution as #1234 with
        the sign flipped: describing a mechanism rather than the effect.
        """
        mock_settings.auth.oauth_enabled = True
        mock_settings.auth.oauth_require_consent = False
        mock_settings.auth.oauth_first_party_clients = []
        mock_settings.auth.oauth_first_party_redirect_patterns = []

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(oauth_mounted_app), current_user=mock_admin_user
            )

        assert result.features["first_party_consent_skip"].enabled is True

    @pytest.mark.asyncio
    async def test_retry_ladder_reports_coherent_on_the_shipped_defaults(
        self, monkeypatch, mock_admin_user, mock_settings, rate_limited_app
    ):
        """LLM_REQUEST_TIMEOUT=30 against AGENT_REQUEST_TIMEOUT=120 fits.

        3x30 + 14s of backoff = 104s inside a 120s turn. The env var is cleared
        because ``resolve_request_timeout`` honours it over the settings value,
        and this box's own ``.env`` sets it to a breaching 90 — a test that read
        the developer's environment would assert nothing about the code.
        """
        monkeypatch.delenv("LLM_REQUEST_TIMEOUT", raising=False)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        feature = result.features["llm_retry_ladder_fits_turn_budget"]
        assert feature.enabled is True
        assert "3 of 3 attempts" in feature.description

    @pytest.mark.asyncio
    async def test_retry_ladder_reports_incoherent_on_the_cluster_shape(
        self, monkeypatch, mock_admin_user, mock_settings, rate_limited_app
    ):
        """The live-cluster configuration: gemini pinned to 120s / 240s.

        3x120 + 14 = 374s against a 240s turn — a 134s breach. Nothing else in
        the system reports it, which is why it sat unnoticed: turns look fine
        until a provider hangs, and then the answer is an opaque 504.
        """
        monkeypatch.delenv("LLM_REQUEST_TIMEOUT", raising=False)
        mock_settings.llm.request_timeout = 30
        mock_settings.llm.timeout_for_provider.return_value = 120
        mock_settings.agent.timeout_for_provider.return_value = 240

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        feature = result.features["llm_retry_ladder_fits_turn_budget"]
        assert feature.enabled is False
        # The number that matters to an operator: a hung provider gets ONE of
        # its three configured attempts.
        assert "1 of 3 attempts" in feature.description
        assert "374s" in feature.description
        assert "LLM_REQUEST_TIMEOUT" in feature.config_hint

    @pytest.mark.asyncio
    async def test_retry_ladder_honours_the_env_override_the_router_honours(
        self, monkeypatch, mock_admin_user, mock_settings, rate_limited_app
    ):
        """``LLM_REQUEST_TIMEOUT`` beats the settings value at the router, so it
        must beat it here too — a report that read only the settings field would
        certify an env-configured breach as safe."""
        monkeypatch.setenv("LLM_REQUEST_TIMEOUT", "600")

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features["llm_retry_ladder_fits_turn_budget"].enabled is False

    def test_the_ladder_report_runs_against_the_real_settings_object(self):
        """The other three tests drive a ``MagicMock``, which answers any
        attribute name — including a misspelled one. This one asks the real
        settings class, so a rename on either timeout map lands here."""
        from faultmaven.config.retry_budget import describe_retry_ladder_budget
        from faultmaven.config.settings import get_settings

        plan = describe_retry_ladder_budget(get_settings())
        assert plan.paid_attempts == 3
        assert plan.attempts >= 1
        assert plan.full_ladder_seconds > 0

    @pytest.mark.asyncio
    async def test_consent_skip_matches_the_predicate_that_actually_gates_it(
        self, mock_admin_user, mock_settings, oauth_mounted_app
    ):
        """Cross-check the report against ``_is_first_party`` itself.

        Both halves present is reported enabled AND the real gate admits the
        shipped client; drop the client list and both flip. Asserting the pair
        together is what makes this a claim about the skip rather than about
        this endpoint's own arithmetic.
        """
        from faultmaven.modules.auth.api.oauth import _is_first_party

        _pin_first_party(mock_settings)
        redirect = "https://abcdefghijklmnopabcdefghijklmnop.chromiumapp.org"

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            configured = await get_env_config_status(
                request=_request_for(oauth_mounted_app), current_user=mock_admin_user
            )
        assert configured.features["first_party_consent_skip"].enabled is True
        assert _is_first_party("faultmaven-copilot", redirect, mock_settings) is True

        mock_settings.auth.oauth_first_party_clients = []
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            stripped = await get_env_config_status(
                request=_request_for(oauth_mounted_app), current_user=mock_admin_user
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


def _secret(value: str) -> MagicMock:
    """A ``SecretStr``-shaped double: truthy, with ``get_secret_value``."""
    key = MagicMock()
    key.get_secret_value.return_value = value
    return key


def _mount_oauth_router(app: FastAPI) -> FastAPI:
    """Mount the REAL OAuth router, the way ``main.py`` mounts it.

    The consent skip is reported from whether this process serves the
    authorize leg, so the fixture has to actually serve it. Mounting the real
    router rather than a look-alike route is what stops the test agreeing with
    a hand-rolled stand-in: if the router's path ever moves, this goes red
    instead of quietly measuring a path nobody serves.
    """
    from faultmaven.modules.auth.api.oauth import router as oauth_router

    app.include_router(oauth_router, prefix="/api/v1")
    # The fixture's own premise, asserted.
    assert any(
        str(getattr(r, "path", "")).endswith("/auth/oauth/authorize")
        for r in app.routes
    )
    return app


@pytest.fixture
def oauth_mounted_app(rate_limited_app) -> FastAPI:
    """A protected app that also serves the OAuth authorize leg."""
    return _mount_oauth_router(rate_limited_app)


def _pin_first_party(settings) -> None:
    """Every SETTING the first-party consent skip needs, and nothing runtime."""
    settings.auth.oauth_enabled = True
    settings.auth.oauth_require_consent = True
    settings.auth.oauth_allowed_clients = ["faultmaven-copilot"]
    settings.auth.oauth_first_party_clients = ["faultmaven-copilot"]
    settings.auth.oauth_first_party_redirect_patterns = [
        r"^https://abcdefghijklmnopabcdefghijklmnop\.chromiumapp\.org/?$"
    ]


def _set_tracing_runtime(monkeypatch, *, sdk: bool, configured: bool, active=True):
    """Drive the two runtime facts ``tracing_is_effective`` reads.

    ``sdk`` and ``configured`` are patched on the tracing module; ``active`` is
    served by a stand-in ``opik`` installed into ``sys.modules``. The stand-in
    matters in BOTH directions of the environment: this repo's venv has no
    ``opik`` at all, while CI's Test Cloud job installs the ``[cloud]`` extra
    and does. Patching the module in means these arms assert the same thing in
    both, instead of one of them passing by accident of what is installed.
    """
    import sys
    import types

    from faultmaven.infrastructure.observability import tracing

    monkeypatch.setattr(tracing, "OPIK_AVAILABLE", sdk)
    monkeypatch.setattr(tracing, "_tracing_configured", configured)

    fake = types.ModuleType("opik")
    fake.__file__ = "/stand-in/opik/__init__.py"
    fake.is_tracing_active = lambda: active
    monkeypatch.setitem(sys.modules, "opik", fake)


async def _feature(user, settings, app, name):
    with patch(SETTINGS_PATCH, return_value=settings):
        result = await get_env_config_status(
            request=_request_for(app), current_user=user
        )
    return result.features[name]


async def _tracing_enabled(user, settings, app) -> bool:
    return (await _feature(user, settings, app, "llm_tracing")).enabled


async def _web_search_feature(user, settings, app):
    return await _feature(user, settings, app, "web_search")


def _pure_settings_answer(feature: str, settings) -> bool:
    """What a SETTINGS-ONLY implementation of ``feature`` would report.

    The shape the population rule forbids — each entry is the most plausible
    version someone would write from configuration alone, including the two
    this endpoint actually shipped (``opik_enabled``; ``enable_web_search and
    <a key>``) and the historical ``WORKERS`` proxy #1227 replaced.

    Used to measure that each scenario's OFF arm can discriminate, rather than
    trusting that it does.
    """
    auth = settings.auth
    if feature == "llm_tracing":
        return bool(settings.observability.opik_enabled)
    if feature == "web_search":
        return bool(settings.knowledge.enable_web_search) and bool(
            settings.knowledge.tavily_api_key or settings.tools.web_search_api_key
        )
    if feature == "first_party_consent_skip":
        return (
            bool(auth.oauth_enabled)
            and bool(auth.oauth_first_party_clients)
            and bool(auth.oauth_first_party_redirect_patterns)
            and set(auth.oauth_first_party_clients).issubset(
                set(auth.oauth_allowed_clients)
            )
        )
    if feature == "suggestion_store_worker_safe":
        return settings.server.workers <= 1
    raise AssertionError(f"no settings-only stand-in defined for {feature}")


# ============================================================
# features[*].enabled reports EFFECT, not configured intent (#1234)
# ============================================================


class TestLLMTracingReportsEffect:
    """``llm_tracing.enabled`` must mean "a span will be recorded" (#1234).

    Enablement is necessary and nowhere near sufficient. ``init_opik_tracing``
    bails on an absent SDK, on ``OPIK_ENABLED=false``, on having no backend URL
    configured, and on any exception while configuring — and after any of those
    the ``@opik.track`` sites are live and record nothing.

    These arms drive the reported answer through the recorded outcome and the
    SDK's own switch, which is what the endpoint reads, rather than through the
    settings the first version of this fix re-derived from.
    """

    @pytest.mark.asyncio
    async def test_configured_but_sdk_absent_reports_disabled(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch
    ):
        """The default standalone install with OPIK_ENABLED=true."""
        _set_tracing_runtime(monkeypatch, sdk=False, configured=False)

        assert (
            await _tracing_enabled(mock_admin_user, mock_settings, rate_limited_app)
            is False
        )

    @pytest.mark.asyncio
    async def test_sdk_present_but_initialisation_never_configured_a_backend(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch
    ):
        """The case the first fix shipped broken, and the reason this reads a
        recorded outcome instead of re-deriving one.

        With the SDK installed — the ``[cloud]`` extra, i.e. every Cloud
        deployment — ``OPIK_ENABLED=true`` and no ``OPIK_USE_LOCAL`` or
        ``OPIK_URL_OVERRIDE`` makes ``init_opik_tracing`` log "Tracing will be
        disabled" and return. A predicate of ``opik_enabled and
        OPIK_AVAILABLE`` reports True here: #1234 verbatim, inside its own fix.
        """
        _set_tracing_runtime(monkeypatch, sdk=True, configured=False)

        assert (
            await _tracing_enabled(mock_admin_user, mock_settings, rate_limited_app)
            is False
        )

    @pytest.mark.asyncio
    async def test_configured_backend_reports_enabled(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch
    ):
        """The other direction, which this machine's environment cannot supply.

        ``opik`` is genuinely absent from this repo's venv, so every assertion
        above holds for a reason that has nothing to do with the fix — they
        would hold against a field hardcoded to False. This arm is what shows
        the answer is a conjunction rather than a constant.
        """
        _set_tracing_runtime(monkeypatch, sdk=True, configured=True)

        assert (
            await _tracing_enabled(mock_admin_user, mock_settings, rate_limited_app)
            is True
        )

    @pytest.mark.asyncio
    async def test_operator_kill_switch_reports_disabled(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch
    ):
        """``OPIK_TRACK_DISABLE=true`` suppresses spans with a backend still
        configured — a documented way to stop tracing — so initialisation
        succeeds and nothing is recorded. Only the SDK's live switch sees it.
        """
        _set_tracing_runtime(monkeypatch, sdk=True, configured=True, active=False)

        assert (
            await _tracing_enabled(mock_admin_user, mock_settings, rate_limited_app)
            is False
        )

    @pytest.mark.asyncio
    async def test_config_hint_names_every_half_the_operator_can_be_missing(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch
    ):
        """``enabled: false`` after setting OPIK_ENABLED=true is a puzzle
        unless the hint names the other halves — the install AND the backend
        URL, which is the half the first fix did not even check.

        Keyed on tokens main's hint does not contain. "cloud" would not have
        been: main already said "Opik cloud key", so a hint assertion on that
        word passes against the value it is supposed to be replacing.
        """
        _set_tracing_runtime(monkeypatch, sdk=True, configured=False)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        hint = result.features["llm_tracing"].config_hint
        assert "faultmaven[cloud]" in hint
        assert "OPIK_URL_OVERRIDE" in hint


class TestWebSearchReportsEffect:
    """``web_search.enabled`` must track the tool THIS PROCESS composed.

    Reported from ``app.state.web_search_tool``, which the composition root
    publishes, so the answer covers every way the registry declined — the knob
    is off, no provider key resolved, or construction raised. Deriving it from
    settings instead answers "would a tool compose", which is a different
    question and wrong exactly when startup composition failed.
    """

    @pytest.mark.asyncio
    async def test_composed_tool_reports_enabled(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Cross-checked against a REAL ``WebSearchTool`` built the way the
        registry builds it, so the report cannot be right about a capability
        that is not there."""
        from faultmaven.modules.agent.tools.web_search import WebSearchTool

        mock_settings.knowledge.tavily_api_key = _secret("tvly-key")
        tool = WebSearchTool(settings=mock_settings)
        assert tool.is_available() is True
        rate_limited_app.state.web_search_tool = tool

        feature = await _web_search_feature(
            mock_admin_user, mock_settings, rate_limited_app
        )
        assert feature.enabled is True
        assert feature.has_api_key is True

    @pytest.mark.asyncio
    async def test_google_cse_only_deployment_reports_enabled(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Google CSE is a full provider in ``_auto_select_provider``, and the
        Tavily-only check reported *disabled* on a deployment where the model
        could demonstrably search."""
        from faultmaven.modules.agent.tools.web_search import (
            GoogleCSEProvider,
            WebSearchTool,
        )

        mock_settings.knowledge.tavily_api_key = None
        mock_settings.tools.web_search_api_key = _secret("google-key")
        mock_settings.tools.web_search_engine_id = "cse-id"
        mock_settings.tools.web_search_api_endpoint = "https://example.invalid"

        tool = WebSearchTool(settings=mock_settings)
        assert isinstance(tool._provider, GoogleCSEProvider)
        rate_limited_app.state.web_search_tool = tool

        feature = await _web_search_feature(
            mock_admin_user, mock_settings, rate_limited_app
        )
        assert feature.enabled is True
        assert feature.has_api_key is True

    @pytest.mark.asyncio
    async def test_no_tool_composed_reports_disabled(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """The positive control. Without it every assertion above could be
        satisfied by a field wired to a constant ``True``."""
        rate_limited_app.state.web_search_tool = None

        feature = await _web_search_feature(
            mock_admin_user, mock_settings, rate_limited_app
        )
        assert feature.enabled is False
        assert feature.has_api_key is False

    @pytest.mark.asyncio
    async def test_keys_configured_but_composition_failed_reports_disabled(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Keys present, no tool composed — the case a settings-derived answer
        gets wrong, and the reason this reads the running object.

        ``has_api_key`` stays True, which is the point of having both fields:
        together they say "your credential is fine, the capability is not
        there", which is what an operator needs to know.
        """
        mock_settings.knowledge.tavily_api_key = _secret("tvly-key")
        rate_limited_app.state.web_search_tool = None

        feature = await _web_search_feature(
            mock_admin_user, mock_settings, rate_limited_app
        )
        assert feature.enabled is False
        assert feature.has_api_key is True

    @pytest.mark.asyncio
    async def test_a_plain_string_key_counts_as_configured(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """``_auto_select_provider`` falls back to ``str(key)``, so a key that
        is not a ``SecretStr`` composes a working provider.

        ``has_api_key`` demanding ``get_secret_value`` reported the credential
        missing for exactly that deployment — contradicting the ``enabled:
        true`` beside it.
        """
        from faultmaven.modules.agent.tools.web_search import WebSearchTool

        mock_settings.knowledge.tavily_api_key = "tvly-plain-string"
        tool = WebSearchTool(settings=mock_settings)
        assert tool.is_available() is True
        rate_limited_app.state.web_search_tool = tool

        feature = await _web_search_feature(
            mock_admin_user, mock_settings, rate_limited_app
        )
        assert feature.enabled is True
        assert feature.has_api_key is True

    @pytest.mark.asyncio
    async def test_a_google_key_without_an_engine_id_still_reports_the_key(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """``has_api_key`` answers "did you supply a credential", and a missing
        engine id is not a missing credential.

        Conjoining ``WEB_SEARCH_ENGINE_ID`` here reported the key absent on a
        deployment that had supplied one, and was asymmetric besides — Tavily
        was asked for a key and nothing more. Incompleteness belongs to
        ``enabled``, which stays False.
        """
        mock_settings.knowledge.tavily_api_key = None
        mock_settings.tools.web_search_api_key = _secret("google-key")
        mock_settings.tools.web_search_engine_id = None
        rate_limited_app.state.web_search_tool = None

        feature = await _web_search_feature(
            mock_admin_user, mock_settings, rate_limited_app
        )
        assert feature.enabled is False
        assert feature.has_api_key is True

    @pytest.mark.asyncio
    async def test_an_unreadable_settings_object_does_not_500_the_whole_report(
        self, mock_admin_user, rate_limited_app
    ):
        """One broken feature must not take the status endpoint down with it.

        Both halves of ``web_search`` are lenient, and symmetrically so. They
        used not to be: ``enabled`` swallowed everything while ``has_api_key``
        raised into the endpoint's handler, so the SAME broken input either
        reported False or 500-ed every other feature's status alongside it —
        on the endpoint an operator reaches for when something is already
        wrong. ``getattr(..., None)`` does not cover this: it swallows
        AttributeError and nothing else.
        """
        from faultmaven.api.routes.admin_config import (
            _web_search_api_key_configured,
            _web_search_is_effective,
        )

        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError("settings exploded")

        class NoState:
            pass

        assert _web_search_is_effective(NoState()) is False
        assert _web_search_api_key_configured(Exploding()) is False

    @pytest.mark.asyncio
    async def test_config_hint_names_the_toggle_and_both_providers(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        feature = await _web_search_feature(
            mock_admin_user, mock_settings, rate_limited_app
        )
        assert "ENABLE_WEB_SEARCH" in feature.config_hint
        assert "TAVILY_API_KEY" in feature.config_hint
        assert "WEB_SEARCH_ENGINE_ID" in feature.config_hint


# ============================================================
# The self-service sign-up bounds (#1320, #1324)
# ============================================================
#
# Each field, where it lives on the settings tree, and a PAIR of distinct
# values to drive it through. A pair rather than one value because what can go
# wrong for a reported setting is not echoing an instruction back — it is being
# a constant, and only the pair falsifies that. The numbers are mutually
# distinct as well, so a field wired to its neighbour's source fails too.
PERSONAL_TENANT_LIMIT_SETTINGS = {
    "sso_jit_personal_tenant_enabled": ("auth", False, True),
    "sso_jit_personal_tenant_max_per_hour": ("auth", 20, 3),
    "tenant_daily_turn_cap": ("agent", 30, 5),
}


async def _limits(user, settings, app):
    with patch(SETTINGS_PATCH, return_value=settings):
        result = await get_env_config_status(
            request=_request_for(app), current_user=user
        )
    return result.personal_tenant_limits


class TestPersonalTenantLimitsAreReported:
    """The three knobs that bound self-service sign-up, reported as values.

    The ``first_party_consent_skip`` argument (#1234) applied to
    configuration. Each of the three is silent by construction: sign-up being
    closed looks like an IdP misconfiguration to the person refused, the
    hourly provisioning ceiling refuses the same way, and a personal tenant at
    its daily turn cap gets a usage-allowance message that names no setting.
    Nothing else reports them — not ``/health``, and not a startup log line,
    which has rolled out of ``kubectl logs`` long before anyone asks.

    They are reported OUTSIDE ``features`` deliberately, and the population
    rule at the end of this file is why: an entry there owes an ``enabled``
    that reports a runtime EFFECT, which "did you set this knob" is not, and
    two of the three are numbers ``FeatureStatus`` has nowhere to put. They
    belong with ``auth_mode`` and ``pii_redaction_enabled``, whose claim is
    the same one.
    """

    @pytest.mark.asyncio
    async def test_the_defaults_an_operator_who_set_nothing_is_running(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Sign-up closed, 20 tenants an hour, 30 turns a day."""
        limits = await _limits(mock_admin_user, mock_settings, rate_limited_app)

        assert limits.sso_jit_personal_tenant_enabled is False
        assert limits.sso_jit_personal_tenant_max_per_hour == 20
        assert limits.tenant_daily_turn_cap == 30

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", sorted(PERSONAL_TENANT_LIMIT_SETTINGS))
    async def test_flipping_the_setting_changes_what_is_reported(
        self, mock_admin_user, mock_settings, rate_limited_app, field
    ):
        """Both verdicts on one member, in one test.

        Split into a "reports A" test and a "reports B" test, either half would
        pass alone against a field hardcoded to that half's value. Asserting
        the pair together is what makes this a claim about the report tracking
        the setting.
        """
        section, first, second = PERSONAL_TENANT_LIMIT_SETTINGS[field]
        assert first != second

        setattr(getattr(mock_settings, section), field, first)
        before = await _limits(mock_admin_user, mock_settings, rate_limited_app)

        setattr(getattr(mock_settings, section), field, second)
        after = await _limits(mock_admin_user, mock_settings, rate_limited_app)

        assert getattr(before, field) == first
        assert getattr(after, field) == second

    @pytest.mark.asyncio
    async def test_no_two_fields_read_the_same_setting(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Three sources, driven apart in ONE call.

        The per-field test above moves one knob at a time, which a report that
        read ``tenant_daily_turn_cap`` for both numbers would still pass — each
        assertion would be made against the value just written. Setting all
        three to distinct non-default values at once is what separates them.
        """
        mock_settings.auth.sso_jit_personal_tenant_enabled = True
        mock_settings.auth.sso_jit_personal_tenant_max_per_hour = 3
        mock_settings.agent.tenant_daily_turn_cap = 5

        limits = await _limits(mock_admin_user, mock_settings, rate_limited_app)

        assert limits.sso_jit_personal_tenant_enabled is True
        assert limits.sso_jit_personal_tenant_max_per_hour == 3
        assert limits.tenant_daily_turn_cap == 5

    @pytest.mark.asyncio
    async def test_the_report_agrees_with_the_readers_that_enforce_the_limits(
        self, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Cross-check against the three functions the product actually obeys.

        ``sso_login_service`` decides whether an org-less identity provisions
        and how many may provision per hour; ``tenant_turn_cap`` decides the
        default daily allowance. All three read through ``get_settings()`` at
        the point of use, so under one patched settings object they and this
        endpoint must agree — otherwise the status page is describing a
        configuration the enforcement paths are not using, which is the whole
        failure this reporting exists to prevent.
        """
        from faultmaven.infrastructure.protection.tenant_turn_cap import _default_limit
        from faultmaven.modules.auth.domain.services.sso_login_service import (
            _jit_personal_tenant_enabled,
            _personal_tenant_hourly_ceiling,
        )

        mock_settings.auth.sso_jit_personal_tenant_enabled = True
        mock_settings.auth.sso_jit_personal_tenant_max_per_hour = 3
        mock_settings.agent.tenant_daily_turn_cap = 5

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )
            limits = result.personal_tenant_limits

            assert (
                limits.sso_jit_personal_tenant_enabled is _jit_personal_tenant_enabled()
            )
            assert (
                limits.sso_jit_personal_tenant_max_per_hour
                == _personal_tenant_hourly_ceiling()
            )
            assert limits.tenant_daily_turn_cap == _default_limit()

    @pytest.mark.asyncio
    async def test_the_report_binds_to_the_env_names_an_operator_sets(
        self, monkeypatch, mock_admin_user, mock_settings, rate_limited_app
    ):
        """Against the REAL settings sections, driven by the REAL env names.

        Every test above drives a ``MagicMock``, which answers any attribute
        name — including a misspelled one, and including one that no longer
        exists. Here the two sections the block reads are the shipped classes,
        built from the three environment variables an operator actually types,
        so a rename of a field or of a ``validation_alias`` lands here rather
        than silently reporting a default nobody set — and so does the endpoint
        reading an attribute that is not there, which now raises instead of
        being answered by a mock.

        Constructed DIRECTLY rather than through ``get_settings()``, and that is
        not a shortcut. ``get_settings()`` caches a process-wide singleton, so
        driving it from here means resetting that singleton — and a reset is not
        local: every later ``get_settings()`` in the process rebuilds from the
        environment as it stands THEN. Three integration modules assign
        ``os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"`` outright
        rather than through ``monkeypatch``, and they run before this file in a
        whole-suite run, so that rebuild repoints the application at an empty
        in-memory database and the composition-root tests fail on "no such table:
        enterprises" — an endpoint report test breaking application bootstrap two
        files later. ``AuthSettings`` and ``AgentSettings`` declare no
        ``env_file``, so constructing them reads ``os.environ`` and nothing else,
        and mutates nothing.
        """
        from faultmaven.config.settings import AgentSettings, AuthSettings

        monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_ENABLED", "true")
        monkeypatch.setenv("SSO_JIT_PERSONAL_TENANT_MAX_PER_HOUR", "7")
        monkeypatch.setenv("TENANT_DAILY_TURN_CAP", "11")
        mock_settings.auth = AuthSettings()
        mock_settings.agent = AgentSettings()

        limits = await _limits(mock_admin_user, mock_settings, rate_limited_app)

        assert limits.sso_jit_personal_tenant_enabled is True
        assert limits.sso_jit_personal_tenant_max_per_hour == 7
        assert limits.tenant_daily_turn_cap == 11

    def test_the_block_cannot_become_optional(self):
        """A required field, asserted, for the reason the endpoint refuses to
        wrap the block in a try/except: a field that is simply absent from a
        response reads as "nothing to report", and an operator cannot tell that
        from "there was nothing to report". Giving it a default — the natural
        way to make a response model tolerant — would reintroduce exactly that.
        """
        from faultmaven.api.models import EnvConfigStatusResponse

        assert EnvConfigStatusResponse.model_fields[
            "personal_tenant_limits"
        ].is_required()


# ============================================================
# The population rule (#1234)
# ============================================================
#
# Each scenario is called twice. With ``reality=False`` it sets every knob an
# operator can reach for its feature and withholds the RUNTIME CAPABILITY; with
# ``reality=True`` it supplies the capability as well.
#
# The withheld thing must be a genuine runtime fact, not another setting. The
# first version of this sweep withheld a setting for two of the four members,
# and there it degenerated into "a conjunction of settings is a conjunction" —
# a pure-settings implementation passed the sweep unharmed. What each member
# withholds now:
#
#   llm_tracing                  the recorded outcome of init_opik_tracing
#   web_search                   the composed tool on app.state
#   first_party_consent_skip     the mounted OAuth authorize route
#   suggestion_store_worker_safe the composed suggestion repository
#
# ONE registry, and the sweep parametrises off it, so a scenario cannot be
# added and left unexercised.


def _scenario_llm_tracing(settings, app, monkeypatch, reality):
    settings.observability.opik_enabled = True
    settings.observability.opik_api_key = _secret("opik-key")
    settings.observability.opik_use_local = True
    _set_tracing_runtime(monkeypatch, sdk=True, configured=reality)


def _scenario_web_search(settings, app, monkeypatch, reality):
    from faultmaven.modules.agent.tools.web_search import WebSearchTool

    settings.knowledge.enable_web_search = True
    settings.knowledge.tavily_api_key = _secret("tvly-key")
    app.state.web_search_tool = WebSearchTool(settings=settings) if reality else None


def _scenario_first_party_consent_skip(settings, app, monkeypatch, reality):
    _pin_first_party(settings)
    if reality:
        _mount_oauth_router(app)


def _scenario_suggestion_store_worker_safe(settings, app, monkeypatch, reality):
    from faultmaven.modules.knowledge.domain.services.suggestion_service import (
        SuggestionService,
    )
    from faultmaven.modules.knowledge.infrastructure.persistence.suggestion_repository import (  # noqa: E501
        DatabaseSuggestionRepository,
        InMemorySuggestionRepository,
    )

    # WORKERS=1 — the knob operators used to reach for — is set in both arms
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


def _scenario_llm_retry_ladder_fits(settings, app, monkeypatch, reality):
    """The two states of a claim whose SUBJECT is the configuration.

    Nothing runtime is withheld here, because there is nothing to withhold:
    the field reports whether the LLM request timeout and the turn timeout fit
    each other, and reading those settings IS reading the reality it claims.
    What it owes instead is a pair — the shipped defaults (3x30 + 14s inside
    120s) and the live cluster's per-provider overrides (3x120 + 14s against
    240s) — so the field is shown to track them rather than being a constant.

    The env var is cleared because ``resolve_request_timeout`` honours it over
    the settings field exactly as the router does, and this box's own ``.env``
    sets it to a breaching 90.
    """
    monkeypatch.delenv("LLM_REQUEST_TIMEOUT", raising=False)
    settings.llm.request_timeout = 30
    settings.llm.timeout_for_provider.return_value = 30 if reality else 120
    settings.agent.timeout_for_provider.return_value = 120 if reality else 240


# Members whose subject IS the configuration (see
# ``TestEveryFeatureReportsEffectNotIntent``, category 2). They cannot register
# in FEATURE_SCENARIOS: that registry's premise is that a settings-only
# implementation FAILS its OFF arm, and a settings-only implementation is
# precisely what these members correctly are.
CONFIGURATION_DERIVED_SCENARIOS = {
    "llm_retry_ladder_fits_turn_budget": _scenario_llm_retry_ladder_fits,
}


class TestEveryFeatureReportsEffectNotIntent:
    """The population rule, swept over the whole ``features`` dict.

    #1234 was raised against ``llm_tracing``, but the contract it broke
    (``FeatureStatus.enabled`` — "Feature is active and usable") is one
    sentence covering every entry, and two of the four were breaking it. The
    per-feature tests above pin each case; this pins the RULE, so a fifth
    feature added as ``enabled=<some setting>`` fails here rather than shipping
    and being found the way this one was.

    The rule, falsifiable for any feature: **there is a state in which the
    feature's configuration is fully set and it still reports False, because
    the runtime capability behind it is absent.** A field wired to settings
    cannot satisfy it — but only if the withheld thing really is a runtime
    fact, which is the property the scenarios above are built to guarantee and
    which ``test_a_pure_settings_implementation_fails_this_sweep`` measures
    rather than assumes.

    That is one of TWO ways a member can satisfy the contract, and a member
    must satisfy one of them EXPLICITLY rather than by default:

    1. **Runtime-verified** — every member registered here today. The scenario
       withholds a genuine runtime fact and the field goes False.

    2. **Derived from configuration** — a member whose SUBJECT is the
       configuration: it reports a relation *between* settings that an
       operator cannot read off their own config. "Do these two knobs fit each
       other" is such a claim; "did you set this knob" is not. Reading
       settings IS reading reality for it, so there is nothing to withhold and
       no scenario here can express it. Such a member registers separately and
       owes a DIFFERENT proof — one configuration where it reports True and
       one where it reports False — because what can go wrong for it is not
       echoing an instruction back but being a constant.

    The distinction is about what the field CLAIMS, not where its bytes come
    from. A field computed from settings is the anti-pattern only when the
    claim it makes is one the operator already knows because they typed it.

    What no member may do is neither — drop out of the sweep in silence.
    ``test_every_reported_feature_is_classified_here`` forces the choice, and
    is deliberately strict: relaxing it to tolerate unregistered entries would
    turn this population rule into an opt-in list and hand back the property
    the sweep exists to establish.
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

        reported = set(result.features)
        classified = set(FEATURE_SCENARIOS) | set(CONFIGURATION_DERIVED_SCENARIOS)
        assert reported == classified, (
            "every entry in the features dict must be classified by this "
            "sweep.\n"
            f"  unclassified (endpoint has it, sweep does not): "
            f"{sorted(reported - classified)}\n"
            f"  stale (sweep has it, endpoint does not):        "
            f"{sorted(classified - reported)}\n"
            "A runtime-verified feature registers in FEATURE_SCENARIOS by "
            "adding a scenario that withholds its capability. A feature whose "
            "subject IS the configuration registers in "
            "CONFIGURATION_DERIVED_SCENARIOS and still owes a True-and-False "
            "pair - see this class's docstring."
        )
        assert len(result.features) >= 5
        # The two categories are disjoint by construction: a member that could
        # satisfy both would mean its "reality" was a setting after all, which
        # is the substitution this whole class exists to catch.
        assert not set(FEATURE_SCENARIOS) & set(CONFIGURATION_DERIVED_SCENARIOS)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("feature", sorted(CONFIGURATION_DERIVED_SCENARIOS))
    async def test_a_configuration_derived_feature_reports_both_verdicts(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch, feature
    ):
        """Category 2's proof: the pair, on ONE member, in ONE test.

        Splitting it into a True test and a False test would let either half
        pass alone against a constant. What can go wrong for a
        configuration-derived field is not echoing an instruction back — it is
        never changing its mind, and only the pair falsifies that.
        """
        scenario = CONFIGURATION_DERIVED_SCENARIOS[feature]

        scenario(mock_settings, rate_limited_app, monkeypatch, True)
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            coherent = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        scenario(mock_settings, rate_limited_app, monkeypatch, False)
        with patch(SETTINGS_PATCH, return_value=mock_settings):
            incoherent = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert coherent.features[feature].enabled is True
        assert incoherent.features[feature].enabled is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("feature", sorted(FEATURE_SCENARIOS))
    async def test_configured_but_capability_absent_reports_disabled(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch, feature
    ):
        """The load-bearing arm: configuration set, runtime capability absent."""
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
        """The other arm, without which "report False" satisfies everything."""
        FEATURE_SCENARIOS[feature](mock_settings, rate_limited_app, monkeypatch, True)

        with patch(SETTINGS_PATCH, return_value=mock_settings):
            result = await get_env_config_status(
                request=_request_for(rate_limited_app), current_user=mock_admin_user
            )

        assert result.features[feature].enabled is True

    @pytest.mark.parametrize("feature", sorted(FEATURE_SCENARIOS))
    def test_the_settings_only_stand_in_is_not_a_constant(self, mock_settings, feature):
        """The meta-test's own premise.

        ``test_a_pure_settings_implementation_fails_this_sweep`` asserts the
        stand-in reports True; a stand-in hardcoded to True would satisfy it
        while proving nothing — the same vacuity the sweep itself was guilty
        of. So each stand-in is shown to report False on settings that
        configure nothing.

        ``workers`` is raised because the suggestion store's historical proxy
        was ``WORKERS <= 1``, which the fixture's default of 1 satisfies: its
        unconfigured state is many workers, not few.
        """
        mock_settings.server.workers = 4

        assert _pure_settings_answer(feature, mock_settings) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("feature", sorted(FEATURE_SCENARIOS))
    async def test_a_pure_settings_implementation_fails_this_sweep(
        self, mock_admin_user, mock_settings, rate_limited_app, monkeypatch, feature
    ):
        """The sweep's own premise, measured per member instead of asserted.

        A population test is only as strong as the weakest thing its scenarios
        withhold, and that weakness is invisible to whoever wrote them: if a
        member's "reality" is really just another setting, its OFF arm passes
        against a pure-settings field and the sweep quietly stops discriminating
        for that member. The first version of this sweep had exactly that hole
        in two of four members and reported 9 passed.

        So each scenario's OFF arm is re-run against a stand-in that answers
        every feature from configuration alone — the shape the rule forbids. It
        must come out True, i.e. the arm really would have caught it.
        """
        FEATURE_SCENARIOS[feature](mock_settings, rate_limited_app, monkeypatch, False)

        assert _pure_settings_answer(feature, mock_settings) is True, (
            f"{feature}'s OFF arm leaves a settings-only implementation "
            f"reporting False, so that arm cannot discriminate and the sweep "
            f"does not cover this member."
        )
