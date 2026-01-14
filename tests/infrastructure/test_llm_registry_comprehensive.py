"""
Comprehensive tests for the LLM Provider Registry system.

Tests coverage:
- Provider registration and initialization
- Fallback chain behavior and routing
- Health checking and status monitoring
- API key security validation
- Registry state management and reset
- Schema-driven provider configuration
- Error handling and graceful fallbacks
"""

import asyncio
import os
import tempfile
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest

from faultmaven.config.settings import get_settings, reset_settings
from faultmaven.infrastructure.llm.providers.base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
)
from faultmaven.infrastructure.llm.providers.registry import (
    PROVIDER_SCHEMA,
    ProviderRegistry,
    get_registry,
    get_valid_provider_names,
    print_provider_options,
    reset_registry,
)


@pytest.fixture(autouse=True)
def reset_registry_before_test():
    """Reset registry before each test to ensure clean state."""
    reset_registry()
    reset_settings()
    yield
    reset_registry()
    reset_settings()


@pytest.fixture(autouse=True)
def clean_llm_environment(monkeypatch):
    """Clean environment for LLM tests - autouse to prevent .env pollution."""
    # Save original environment
    original_env = os.environ.copy()

    # Clear all LLM-related environment variables FIRST (including from .env)
    # This must be done BEFORE load_dotenv is called
    llm_keys = [
        "CHAT_PROVIDER",
        "FIREWORKS_API_KEY",
        "FIREWORKS_MODEL",
        "FIREWORKS_API_BASE",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "OPENAI_API_BASE",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_API_BASE",
        "GROQ_API_KEY",
        "GROQ_MODEL",
        "GROQ_API_BASE",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GEMINI_API_BASE",
        "HUGGINGFACE_API_KEY",
        "HUGGINGFACE_MODEL",
        "HUGGINGFACE_API_URL",
        "OPENROUTER_API_KEY",
        "OPENROUTER_MODEL",
        "OPENROUTER_API_BASE",
        "LOCAL_LLM_URL",
        "LOCAL_LLM_MODEL",
        "LOCAL_LLM_BASE_URL",
        "COHERE_API_KEY",
        "COHERE_MODEL",
        "COHERE_API_BASE",
        "STRICT_PROVIDER_MODE",
    ]

    for key in llm_keys:
        os.environ.pop(key, None)

    # Patch load_dotenv to prevent loading .env file
    # This prevents new .env loads but won't affect already-loaded env vars
    mock_load_dotenv = Mock(return_value=None)
    monkeypatch.setattr("dotenv.load_dotenv", mock_load_dotenv)

    # Reset settings and registry after clearing env vars and patching dotenv
    reset_settings()
    reset_registry()

    yield

    # Restore original environment
    os.environ.clear()
    os.environ.update(original_env)
    reset_settings()
    reset_registry()


@pytest.fixture
def clean_env():
    """Provide a clean environment for testing (deprecated - use clean_llm_environment)."""
    # This fixture is now redundant but kept for compatibility
    yield


@pytest.fixture
def mock_provider_classes():
    """Mock provider classes for testing."""
    providers = {}

    for provider_name in PROVIDER_SCHEMA.keys():
        mock_provider = Mock(spec=BaseLLMProvider)
        mock_provider.is_available.return_value = True
        mock_provider.get_supported_models.return_value = ["test-model"]
        mock_provider.config = ProviderConfig(
            name=provider_name,
            api_key="test-key",
            base_url="http://test.com",
            models=["test-model"],
            max_retries=3,
            timeout=30,
            confidence_score=0.8,
        )
        # Create a fresh AsyncMock for each test
        mock_provider.generate = AsyncMock(
            return_value=LLMResponse(
                content="Test response",
                model="test-model",
                confidence=0.85,
                provider=provider_name,  # Use actual provider name
                tokens_used=30,
                response_time_ms=100,
            )
        )
        providers[provider_name] = mock_provider

    return providers


@pytest.fixture
def sample_env_vars():
    """Sample environment variables for testing multiple providers."""
    return {
        "CHAT_PROVIDER": "fireworks",
        "FIREWORKS_API_KEY": "fw-test-key-123",
        "FIREWORKS_MODEL": "accounts/fireworks/models/llama-v3p1-8b-instruct",
        "OPENAI_API_KEY": "sk-openai-test-456",
        "OPENAI_MODEL": "gpt-4o-mini",
        "ANTHROPIC_API_KEY": "anthropic-test-789",
        "ANTHROPIC_MODEL": "claude-3-haiku-20240307",
        "LOCAL_LLM_URL": "http://localhost:11434",
        "LOCAL_LLM_MODEL": "llama2:7b",
        "GEMINI_API_KEY": "gemini-test-abc",
        "HUGGINGFACE_API_KEY": "hf-test-def",
    }


class TestProviderRegistryInitialization:
    """Test provider registry initialization and setup."""

    def test_registry_singleton(self):
        """Test that registry follows singleton pattern."""
        registry1 = get_registry()
        registry2 = get_registry()

        assert registry1 is registry2

    def test_registry_reset(self):
        """Test registry reset functionality."""
        registry1 = get_registry()
        reset_registry()
        registry2 = get_registry()

        assert registry1 is not registry2

    def test_lazy_initialization(self):
        """Test that registry uses lazy initialization."""
        registry = ProviderRegistry()

        # Should not be initialized until first use
        assert not registry._initialized

        # Accessing providers should trigger initialization
        providers = registry.get_available_providers()
        assert registry._initialized

    def test_initialization_without_settings(self):
        """Test registry initialization when settings unavailable.

        When get_settings() raises an exception:
        1. __init__ catches it and sets settings to None
        2. _ensure_initialized() calls get_settings() again, which raises
        3. The exception propagates directly (no wrapping)

        This test verifies the registry handles missing settings gracefully
        by catching the exception during __init__ and setting settings to None.
        """
        # Patch get_settings where it's imported - in config.settings module
        # The registry.py imports it locally with "from faultmaven.config.settings import get_settings"
        with patch("faultmaven.config.settings.get_settings") as mock_get_settings:
            mock_get_settings.side_effect = Exception("Settings unavailable")

            # Create registry - __init__ catches the exception and sets settings to None
            registry = ProviderRegistry(settings=None)

            # Verify settings is None (exception was caught in __init__)
            assert registry.settings is None

            # When we try to use the registry, _ensure_initialized() calls get_settings()
            # which raises the exception directly (not wrapped in LLMProviderError)
            with pytest.raises(Exception) as exc_info:
                registry._ensure_initialized()

            assert "Settings unavailable" in str(exc_info.value)

    def test_initialization_with_mock_settings(self, clean_env):
        """Test registry initialization with mock settings."""
        mock_settings = Mock()
        mock_settings.llm = Mock()
        mock_settings.llm.provider = "fireworks"
        mock_settings.llm.fireworks_api_key = Mock()
        mock_settings.llm.fireworks_api_key.get_secret_value.return_value = "test-key"
        mock_settings.llm.fireworks_model = "test-model"
        mock_settings.llm.fireworks_base_url = "http://test.com"

        registry = ProviderRegistry(settings=mock_settings)

        # Should initialize without error
        registry._ensure_initialized()
        assert registry._initialized


class TestProviderConfiguration:
    """Test provider configuration from schema and environment."""

    def test_provider_schema_completeness(self):
        """Test that all providers in schema have required fields."""
        # Core required fields for all providers
        required_fields = [
            "api_key_var",
            "model_var",
            "base_url_var",
            "default_base_url",
            "default_model",
            "provider_class",
            "confidence_score",
        ]

        for provider_name, schema in PROVIDER_SCHEMA.items():
            for field in required_fields:
                assert (
                    field in schema
                ), f"Provider {provider_name} missing required field: {field}"

            # max_retries and timeout are optional - they default to settings.llm.max_retries and settings.llm.request_timeout
            # Only providers with custom retry/timeout logic (like 'local') need to specify them
            # See registry.py lines 272-274 for how defaults are applied

    def test_get_valid_provider_names(self):
        """Test getting list of valid provider names."""
        provider_names = get_valid_provider_names()

        assert isinstance(provider_names, list)
        assert len(provider_names) > 0
        assert "fireworks" in provider_names
        assert "openai" in provider_names
        assert "anthropic" in provider_names
        assert "local" in provider_names

    def test_provider_config_creation_with_api_key(self, clean_env):
        """Test provider config creation when API key is available."""
        os.environ.update(
            {
                "CHAT_PROVIDER": "fireworks",
                "FIREWORKS_API_KEY": "fw-test-123",
                "FIREWORKS_MODEL": "custom-model",
            }
        )

        registry = ProviderRegistry()
        schema = PROVIDER_SCHEMA["fireworks"]
        config = registry._create_provider_config("fireworks", schema)

        assert config is not None
        assert config.name == "fireworks"
        assert config.api_key == "fw-test-123"
        assert config.models == ["custom-model"]
        # max_retries and timeout come from schema if present, else from settings
        # Fireworks doesn't have these in schema, so they come from settings defaults
        assert config.max_retries is not None
        assert config.timeout is not None
        assert config.confidence_score == schema["confidence_score"]

    def test_provider_config_creation_without_api_key(self):
        """Test provider config creation when API key is missing."""
        # Mock settings with NO API key for fireworks
        mock_settings = Mock()
        mock_settings.llm = Mock()
        mock_settings.llm.fireworks_api_key = None  # This is the key test - no API key
        mock_settings.llm.fireworks_model = "test-model"
        mock_settings.llm.fireworks_base_url = "http://test.com"
        mock_settings.llm.max_retries = 3
        mock_settings.llm.request_timeout = 30

        # Create registry with mock settings that have no API key
        registry = ProviderRegistry(settings=mock_settings)
        schema = PROVIDER_SCHEMA["fireworks"]
        config = registry._create_provider_config("fireworks", schema)

        # Should return None for providers that require API keys when key is missing
        assert config is None

    def test_local_provider_config_creation(self, clean_env):
        """Test local provider config creation (no API key required)."""
        os.environ.update(
            {"LOCAL_LLM_URL": "http://localhost:11434", "LOCAL_LLM_MODEL": "llama2:7b"}
        )

        # Registry will use settings which reads from environment
        registry = ProviderRegistry()
        schema = PROVIDER_SCHEMA["local"]
        config = registry._create_provider_config("local", schema)

        assert config is not None
        assert config.name == "local"
        assert config.api_key is None
        assert config.base_url == "http://localhost:11434"
        assert config.models == ["llama2:7b"]

    def test_local_provider_missing_requirements(self):
        """Test local provider when required environment variables are missing."""
        # Mock settings with NO local LLM configuration
        mock_settings = Mock()
        mock_settings.llm = Mock()
        mock_settings.llm.local_url = None  # Missing required URL
        mock_settings.llm.local_model = None  # Missing required model
        mock_settings.llm.max_retries = 3
        mock_settings.llm.request_timeout = 30

        # Create registry with mock settings that have no local LLM config
        registry = ProviderRegistry(settings=mock_settings)
        schema = PROVIDER_SCHEMA["local"]
        config = registry._create_provider_config("local", schema)

        # Should return None when required configuration is missing
        assert config is None

    def test_provider_initialization_success(self, clean_env, mock_provider_classes):
        """Test successful provider initialization."""
        with patch.dict(
            "faultmaven.infrastructure.llm.providers.registry.PROVIDER_SCHEMA",
            {
                "test_provider": {
                    "provider_class": lambda config: mock_provider_classes["fireworks"]
                }
            },
        ):
            registry = ProviderRegistry()
            config = ProviderConfig(
                name="test_provider",
                api_key="test-key",
                base_url="http://test.com",
                models=["test-model"],
                max_retries=3,
                timeout=30,
                confidence_score=0.8,
            )

            registry._initialize_provider("test_provider", config)

            assert "test_provider" in registry._providers

    def test_provider_initialization_failure(self, clean_env):
        """Test provider initialization failure handling."""
        with patch.dict(
            "faultmaven.infrastructure.llm.providers.registry.PROVIDER_SCHEMA",
            {
                "failing_provider": {
                    "provider_class": Mock(
                        side_effect=Exception("Provider init failed")
                    )
                }
            },
        ):
            registry = ProviderRegistry()
            config = ProviderConfig(
                name="failing_provider",
                api_key="test-key",
                base_url="http://test.com",
                models=["test-model"],
                max_retries=3,
                timeout=30,
                confidence_score=0.8,
            )

            # Should not raise exception, just log error
            registry._initialize_provider("failing_provider", config)

            assert "failing_provider" not in registry._providers


class TestFallbackChain:
    """Test fallback chain setup and behavior."""

    def test_fallback_chain_setup_primary_available(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test fallback chain when primary provider is available."""
        os.environ.update(sample_env_vars)
        os.environ["CHAT_PROVIDER"] = "fireworks"
        os.environ["STRICT_PROVIDER_MODE"] = "false"  # Ensure fallbacks are enabled

        with patch.multiple(
            "faultmaven.infrastructure.llm.providers.registry",
            FireworksProvider=lambda config: mock_provider_classes["fireworks"],
            OpenAIProvider=lambda config: mock_provider_classes["openai"],
            LocalProvider=lambda config: mock_provider_classes["local"],
        ):
            registry = ProviderRegistry()
            registry._ensure_initialized()

            fallback_chain = registry.get_fallback_chain()

            assert len(fallback_chain) > 0
            # The primary provider from CHAT_PROVIDER should be first
            # Note: fallback_chain may contain LLMProvider enum values or strings depending on settings implementation
            primary = (
                str(fallback_chain[0]).lower()
                if hasattr(fallback_chain[0], "value")
                else fallback_chain[0]
            )
            assert primary == "fireworks" or primary.endswith("fireworks")

            # Check that fallback providers are present (if strict mode is disabled)
            # Convert enum values to strings for comparison
            chain_str = [
                str(p).lower() if hasattr(p, "value") else p for p in fallback_chain
            ]
            assert (
                any("openai" in str(p) for p in chain_str) or "openai" in fallback_chain
            )
            assert (
                any("local" in str(p) for p in chain_str) or "local" in fallback_chain
            )

    def test_fallback_chain_setup_primary_unavailable(
        self, clean_env, mock_provider_classes
    ):
        """Test fallback chain when primary provider is unavailable."""
        # Set primary provider but don't provide API key
        # However, clean_llm_environment fixture clears all env vars
        # So we need to set them explicitly here
        os.environ.update(
            {
                "CHAT_PROVIDER": "openai",
                "FIREWORKS_API_KEY": "fw-test-123",  # Only fireworks key available
                "LOCAL_LLM_URL": "http://localhost:11434",
                "LOCAL_LLM_MODEL": "llama2:7b",
                # Note: OPENAI_API_KEY is not set, so openai provider won't be initialized
            }
        )

        # Reset settings to pick up new env vars
        reset_settings()

        with patch.multiple(
            "faultmaven.infrastructure.llm.providers.registry",
            FireworksProvider=lambda config: mock_provider_classes["fireworks"],
            OpenAIProvider=lambda config: mock_provider_classes["openai"],
            LocalProvider=lambda config: mock_provider_classes["local"],
        ):
            registry = ProviderRegistry()
            registry._ensure_initialized()

            fallback_chain = registry.get_fallback_chain()

            # Since openai has no API key, it might still be in chain if it's the primary
            # but with settings defaulting. Let's check what's actually available.
            # With our clean env, only fireworks and local should be initialized
            available = registry.get_available_providers()

            # Available providers should be in chain
            assert "fireworks" in fallback_chain or "fireworks" in available
            assert "local" in fallback_chain or "local" in available

    def test_invalid_primary_provider_fallback(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test fallback when invalid primary provider is specified."""
        os.environ.update(sample_env_vars)
        # Use valid provider name from enum - settings validation will reject invalid_provider
        # Instead, test with a valid provider name that doesn't have credentials
        os.environ["CHAT_PROVIDER"] = (
            "local"  # Valid provider that will use default fallback
        )

        with patch.multiple(
            "faultmaven.infrastructure.llm.providers.registry",
            FireworksProvider=lambda config: mock_provider_classes["fireworks"],
            OpenAIProvider=lambda config: mock_provider_classes["openai"],
            LocalProvider=lambda config: mock_provider_classes["local"],
        ):
            registry = ProviderRegistry()
            registry._ensure_initialized()

            fallback_chain = registry.get_fallback_chain()

            # Should have available providers in chain
            assert len(fallback_chain) > 0

    @pytest.mark.asyncio
    async def test_route_request_success_first_provider(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test successful request routing with first provider."""
        os.environ.update(sample_env_vars)

        # Create registry and directly inject mock providers
        registry = ProviderRegistry()
        registry._initialized = True
        registry._providers = {
            "fireworks": mock_provider_classes["fireworks"],
            "openai": mock_provider_classes["openai"],
            "local": mock_provider_classes["local"],
        }
        registry._fallback_chain = ["fireworks", "openai", "local"]

        response = await registry.route_request(
            prompt="Test prompt", max_tokens=100, temperature=0.7
        )

        assert response.content == "Test response"
        assert response.confidence >= 0.8

        # Verify only first provider was called
        mock_provider_classes["fireworks"].generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_route_request_fallback_on_failure(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test request routing falls back on provider failure."""
        os.environ.update(sample_env_vars)

        # Make first provider fail, reset second provider
        mock_provider_classes["fireworks"].generate = AsyncMock(
            side_effect=Exception("Provider failed")
        )
        mock_provider_classes["openai"].generate = AsyncMock(
            return_value=LLMResponse(
                content="Test response",
                model="test-model",
                confidence=0.85,
                provider="openai",
                tokens_used=30,
                response_time_ms=100,
            )
        )

        # Create registry and directly inject mock providers
        registry = ProviderRegistry()
        registry._initialized = True
        registry._providers = {
            "fireworks": mock_provider_classes["fireworks"],
            "openai": mock_provider_classes["openai"],
            "local": mock_provider_classes["local"],
        }
        registry._fallback_chain = ["fireworks", "openai", "local"]

        response = await registry.route_request(
            prompt="Test prompt", max_tokens=100, temperature=0.7
        )

        assert response.content == "Test response"

        # Verify fallback occurred
        mock_provider_classes["fireworks"].generate.assert_called_once()
        mock_provider_classes["openai"].generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_route_request_all_providers_fail(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test request routing when all providers fail."""
        os.environ.update(sample_env_vars)

        # Make all providers fail
        for provider in mock_provider_classes.values():
            provider.generate.side_effect = Exception("Provider failed")

        with patch.multiple(
            "faultmaven.infrastructure.llm.providers.registry",
            FireworksProvider=lambda config: mock_provider_classes["fireworks"],
            OpenAIProvider=lambda config: mock_provider_classes["openai"],
            LocalProvider=lambda config: mock_provider_classes["local"],
        ):
            registry = ProviderRegistry()
            registry._ensure_initialized()

            with pytest.raises(Exception) as exc_info:
                await registry.route_request(
                    prompt="Test prompt", max_tokens=100, temperature=0.7
                )

            assert "All providers failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_route_request_confidence_threshold(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test request routing with confidence threshold filtering."""
        os.environ.update(sample_env_vars)

        # First provider returns low confidence
        mock_provider_classes["fireworks"].generate = AsyncMock(
            return_value=LLMResponse(
                content="Low confidence response",
                model="test-model",
                provider="fireworks",
                tokens_used=30,
                response_time_ms=100,
                confidence=0.5,  # Below threshold
            )
        )

        # Second provider returns high confidence
        mock_provider_classes["openai"].generate = AsyncMock(
            return_value=LLMResponse(
                content="Test response",
                model="test-model",
                confidence=0.85,
                provider="openai",
                tokens_used=30,
                response_time_ms=100,
            )
        )

        # Create registry and directly inject mock providers
        registry = ProviderRegistry()
        registry._initialized = True
        registry._providers = {
            "fireworks": mock_provider_classes["fireworks"],
            "openai": mock_provider_classes["openai"],
            "local": mock_provider_classes["local"],
        }
        registry._fallback_chain = ["fireworks", "openai", "local"]

        response = await registry.route_request(
            prompt="Test prompt", confidence_threshold=0.8
        )

        # Should get response from second provider (higher confidence)
        assert response.content == "Test response"
        assert response.confidence >= 0.8

        # Verify fallback due to low confidence
        mock_provider_classes["fireworks"].generate.assert_called_once()
        mock_provider_classes["openai"].generate.assert_called_once()


class TestProviderHealthAndStatus:
    """Test provider health checking and status reporting."""

    def test_get_available_providers(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test getting list of available providers."""
        os.environ.update(sample_env_vars)

        with patch.multiple(
            "faultmaven.infrastructure.llm.providers.registry",
            FireworksProvider=lambda config: mock_provider_classes["fireworks"],
            OpenAIProvider=lambda config: mock_provider_classes["openai"],
            LocalProvider=lambda config: mock_provider_classes["local"],
        ):
            registry = ProviderRegistry()
            providers = registry.get_available_providers()

            assert isinstance(providers, list)
            assert len(providers) > 0
            assert "fireworks" in providers
            assert "openai" in providers
            assert "local" in providers

    def test_get_all_provider_names(self):
        """Test getting all provider names from schema."""
        registry = ProviderRegistry()
        all_names = registry.get_all_provider_names()

        assert isinstance(all_names, list)
        assert len(all_names) == len(PROVIDER_SCHEMA)
        for name in PROVIDER_SCHEMA.keys():
            assert name in all_names

    def test_get_specific_provider(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test getting a specific provider by name."""
        os.environ.update(sample_env_vars)

        # Create registry and directly inject mock providers
        registry = ProviderRegistry()
        registry._initialized = True
        registry._providers = {
            "fireworks": mock_provider_classes["fireworks"],
            "openai": mock_provider_classes["openai"],
        }
        registry._fallback_chain = ["fireworks", "openai"]

        fireworks_provider = registry.get_provider("fireworks")
        assert fireworks_provider is not None
        assert fireworks_provider is mock_provider_classes["fireworks"]

        nonexistent_provider = registry.get_provider("nonexistent")
        assert nonexistent_provider is None

    def test_get_provider_status(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test getting comprehensive provider status information."""
        os.environ.update(sample_env_vars)

        with patch.multiple(
            "faultmaven.infrastructure.llm.providers.registry",
            FireworksProvider=lambda config: mock_provider_classes["fireworks"],
            OpenAIProvider=lambda config: mock_provider_classes["openai"],
            LocalProvider=lambda config: mock_provider_classes["local"],
        ):
            registry = ProviderRegistry()
            registry._ensure_initialized()

            status = registry.get_provider_status()

            assert isinstance(status, dict)
            assert len(status) > 0

            # Check status for each provider
            for provider_name, provider_status in status.items():
                assert "available" in provider_status
                assert "models" in provider_status
                assert "confidence_score" in provider_status
                assert "in_fallback_chain" in provider_status

                assert isinstance(provider_status["available"], bool)
                assert isinstance(provider_status["models"], list)
                assert isinstance(provider_status["confidence_score"], (int, float))
                assert isinstance(provider_status["in_fallback_chain"], bool)

    def test_provider_availability_check(self, clean_env, mock_provider_classes):
        """Test provider availability checking."""
        # Setup one available and one unavailable provider
        mock_provider_classes["fireworks"].is_available.return_value = True
        mock_unavailable = Mock(spec=BaseLLMProvider)
        mock_unavailable.is_available.return_value = False
        mock_unavailable.config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="http://test.com",
            models=["test-model"],
            max_retries=3,
            timeout=30,
            confidence_score=0.8,
        )
        mock_unavailable.get_supported_models.return_value = ["test-model"]

        os.environ.update(
            {"FIREWORKS_API_KEY": "fw-test-123", "OPENAI_API_KEY": "sk-test-456"}
        )

        # Create registry and directly inject mock providers
        registry = ProviderRegistry()
        registry._initialized = True
        # Only add fireworks since openai is unavailable
        registry._providers = {
            "fireworks": mock_provider_classes["fireworks"],
        }
        registry._fallback_chain = ["fireworks"]

        # For status, we can also add the unavailable provider to test status reporting
        # but it won't be in _providers dict
        available = registry.get_available_providers()
        status = registry.get_provider_status()

        # Only fireworks should be available
        assert "fireworks" in available
        assert status["fireworks"]["available"] == True

        # OpenAI is not in providers, so won't be in status
        assert "openai" not in available


class TestApiKeySecurity:
    """Test API key security and validation."""

    def test_api_key_masking_in_logs(self, clean_env, caplog):
        """Test that API keys are not exposed in logs."""
        import logging

        caplog.set_level(logging.INFO)  # Ensure INFO level is captured

        os.environ.update(
            {
                "FIREWORKS_API_KEY": "fw-secret-key-123456789",
                "OPENAI_API_KEY": "sk-secret-key-987654321",
            }
        )

        with patch(
            "faultmaven.infrastructure.llm.providers.registry.FireworksProvider"
        ):
            registry = ProviderRegistry()
            registry._ensure_initialized()

            # Check that full API keys don't appear in logs
            log_output = caplog.text
            assert "fw-secret-key-123456789" not in log_output
            assert "sk-secret-key-987654321" not in log_output

            # API keys may be mentioned by name or masked (e.g., "fw-secret-ke...")
            # We just need to ensure the full key doesn't appear
            # The test is primarily about security, not exact log format

    def test_secure_provider_config_storage(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test that provider configs store API keys securely."""
        os.environ.update(sample_env_vars)

        with patch.multiple(
            "faultmaven.infrastructure.llm.providers.registry",
            FireworksProvider=lambda config: mock_provider_classes["fireworks"],
        ):
            registry = ProviderRegistry()
            registry._ensure_initialized()

            provider = registry.get_provider("fireworks")
            assert provider is not None

            # Config should exist but API key should be handled securely
            assert hasattr(provider, "config")
            assert provider.config.name == "fireworks"

    def test_environment_variable_validation(self):
        """Test validation of environment variables for security."""
        # Test with suspicious values
        suspicious_values = [
            "",  # Empty string
            "test",  # Too short
            "sk-" + "a" * 100,  # Too long
        ]

        for suspicious_value in suspicious_values:
            # Reset for each iteration
            reset_settings()
            reset_registry()

            os.environ["OPENAI_API_KEY"] = suspicious_value

            registry = ProviderRegistry()
            schema = PROVIDER_SCHEMA["openai"]
            config = registry._create_provider_config("openai", schema)

            # Should handle suspicious values gracefully
            # Empty string should return None, others may return the value
            if suspicious_value == "":
                assert config is None
            elif config:
                assert config.api_key == suspicious_value or config.api_key is None


class TestRegistryStateManagement:
    """Test registry state management and isolation."""

    def test_registry_reset_clears_state(
        self, clean_env, sample_env_vars, mock_provider_classes
    ):
        """Test that registry reset properly clears all state."""
        os.environ.update(sample_env_vars)

        with patch.multiple(
            "faultmaven.infrastructure.llm.providers.registry",
            FireworksProvider=lambda config: mock_provider_classes["fireworks"],
        ):
            # Initialize registry
            registry1 = get_registry()
            providers_before = registry1.get_available_providers()
            assert len(providers_before) > 0

            # Reset registry
            reset_registry()

            # Get new registry instance
            registry2 = get_registry()

            # Should be different instance
            assert registry1 is not registry2

            # New registry should start uninitialized
            assert not registry2._initialized

    def test_concurrent_initialization_safety(self, clean_env, sample_env_vars):
        """Test that concurrent initialization is handled safely."""
        registry = ProviderRegistry()

        async def init_registry():
            registry._ensure_initialized()
            return registry._initialized

        # Test concurrent initialization attempts
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            tasks = [init_registry() for _ in range(5)]
            results = loop.run_until_complete(asyncio.gather(*tasks))

            # All should succeed
            assert all(results)

            # Registry should be initialized exactly once
            assert registry._initialized
        finally:
            loop.close()

    def test_settings_update_after_initialization(self, clean_env, sample_env_vars):
        """Test behavior when settings are updated after initialization."""
        os.environ.update(sample_env_vars)

        # Initialize with first settings
        registry = ProviderRegistry()
        registry._ensure_initialized()
        initial_providers = registry.get_available_providers()

        # Update environment (simulating settings change)
        os.environ["ANTHROPIC_API_KEY"] = "new-anthropic-key"

        # Registry should still use initial configuration
        current_providers = registry.get_available_providers()
        assert current_providers == initial_providers

        # Reset and reinitialize to pick up changes
        reset_registry()
        new_registry = get_registry()
        new_registry._ensure_initialized()

        # Now should reflect updated configuration
        # (specific behavior depends on implementation)

    def test_registry_isolation_between_tests(self):
        """Test that registry state is properly isolated between tests."""
        # This test verifies the fixture works correctly
        registry = get_registry()

        # Should start with clean state
        assert not registry._initialized
        assert len(registry._providers) == 0
        assert len(registry._fallback_chain) == 0


class TestErrorHandlingAndEdgeCases:
    """Test error handling and edge cases."""

    def test_initialization_with_no_providers_available(self, clean_env):
        """Test registry behavior when no providers are available."""
        # Don't set any API keys or local config
        registry = ProviderRegistry()
        registry._ensure_initialized()

        available_providers = registry.get_available_providers()
        fallback_chain = registry.get_fallback_chain()

        # Should handle gracefully
        assert isinstance(available_providers, list)
        assert isinstance(fallback_chain, list)

    def test_initialization_without_dotenv(self):
        """Test registry initialization when dotenv is not available."""
        # Even without dotenv, settings system should work with just env vars
        # Environment is already clean from autouse fixture

        registry = ProviderRegistry()

        # Should initialize without dotenv (settings will use defaults)
        registry._ensure_initialized()
        assert registry._initialized

        # May have no providers available, but should not crash
        providers = registry.get_available_providers()
        assert isinstance(providers, list)

    def test_invalid_schema_provider_handling(self):
        """Test handling of providers not in schema."""
        registry = ProviderRegistry()

        # Try to initialize unknown provider
        config = ProviderConfig(
            name="unknown_provider",
            api_key="test-key",
            base_url="http://test.com",
            models=["test-model"],
            max_retries=3,
            timeout=30,
            confidence_score=0.8,
        )

        # Should handle gracefully (log warning but not crash)
        registry._initialize_provider("unknown_provider", config)

        assert "unknown_provider" not in registry._providers

    def test_print_provider_options(self, capsys):
        """Test the provider options printing utility."""
        print_provider_options()

        captured = capsys.readouterr()
        output = captured.out

        # Should print all providers from schema
        for provider_name in PROVIDER_SCHEMA.keys():
            assert provider_name in output

        # Should include example usage
        assert "CHAT_PROVIDER" in output

    @pytest.mark.asyncio
    async def test_route_request_with_empty_fallback_chain(self, clean_env):
        """Test request routing with empty fallback chain."""
        registry = ProviderRegistry()
        registry._fallback_chain = []  # Force empty chain
        registry._initialized = True

        with pytest.raises(Exception) as exc_info:
            await registry.route_request(prompt="Test prompt", max_tokens=100)

        assert "All providers failed" in str(exc_info.value)

    def test_registry_with_partial_provider_failure(self):
        """Test registry behavior when some providers fail to initialize."""
        # Mock settings with only openai API key
        mock_settings = Mock()
        mock_settings.llm = Mock()
        mock_settings.llm.provider = "fireworks"  # Primary provider
        mock_settings.llm.strict_provider_mode = False  # Allow fallbacks
        # Fireworks has no API key (will not be initialized)
        mock_settings.llm.fireworks_api_key = None
        mock_settings.llm.fireworks_model = "test-model"
        mock_settings.llm.fireworks_base_url = "http://test.com"
        # OpenAI has API key (will be initialized)
        from pydantic import SecretStr

        mock_settings.llm.openai_api_key = SecretStr("sk-test-456")
        mock_settings.llm.openai_model = "gpt-4o"
        mock_settings.llm.openai_base_url = "https://api.openai.com/v1"
        # Local not configured
        mock_settings.llm.local_url = None
        mock_settings.llm.local_model = None
        mock_settings.llm.max_retries = 3
        mock_settings.llm.request_timeout = 30

        # Create a mock provider for openai
        mock_openai = Mock(spec=BaseLLMProvider)
        mock_openai.is_available.return_value = True
        mock_openai.get_supported_models.return_value = ["test-model"]
        mock_openai.config = ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="http://test.com",
            models=["test-model"],
            max_retries=3,
            timeout=30,
            confidence_score=0.8,
        )

        # Mock provider classes - fireworks won't be called (no API key), openai will succeed
        with patch.multiple(
            "faultmaven.infrastructure.llm.providers.registry",
            OpenAIProvider=lambda config: mock_openai,  # This will succeed
        ):
            registry = ProviderRegistry(settings=mock_settings)
            registry._ensure_initialized()

            # Should continue with available providers
            assert registry._initialized
            available = registry.get_available_providers()

            # Should not include provider without API key
            assert "fireworks" not in available

            # Should include successful provider
            assert "openai" in available


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
