"""
Centralized Provider Registry for LLM providers.

This module provides a central registry for managing LLM providers, their configurations,
and fallback strategies. It resolves the scattered configuration problem by providing
a single source of truth for provider management.

Configuration is read from the unified settings system (faultmaven.config.settings).
Note: This module no longer calls load_dotenv() at import time. Environment variable
loading is handled by the settings system.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Type, Union

from faultmaven.exceptions import ModelLoadingException
from faultmaven.infrastructure.llm.metering import record_provider_call

from .anthropic import AnthropicProvider
from .base import BaseLLMProvider, LLMResponse, ProviderConfig
from .cohere_provider import CohereProvider
from .fireworks_provider import FireworksProvider
from .gemini import GeminiProvider
from .groq_provider import GroqProvider
from .huggingface import HuggingFaceProvider
from .local_provider import LocalProvider
from .openai_provider import OpenAIProvider
from .openrouter_provider import OpenRouterProvider


class ProviderHealth(Enum):
    """Health status for LLM providers"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # responding but slow/partial failures
    UNHEALTHY = "unhealthy"  # circuit open, skip entirely
    UNKNOWN = "unknown"  # never tried or recovery period expired


@dataclass
class ProviderState:
    """Track health and performance metrics for a provider"""

    name: str
    health: ProviderHealth = ProviderHealth.UNKNOWN
    consecutive_failures: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    avg_latency_ms: float = 0.0
    _latency_window: list = field(default_factory=list)  # rolling window
    _recovery_cooldown: float = 60.0  # Dynamic cooldown

    # Thresholds
    FAILURE_THRESHOLD: int = 3  # failures before UNHEALTHY
    DEGRADED_THRESHOLD: int = 1  # failures before DEGRADED
    BASE_RECOVERY_COOLDOWN: float = 60.0  # initial seconds before retrying UNHEALTHY
    MAX_RECOVERY_COOLDOWN: float = 600.0  # max cooldown (10 minutes)
    LATENCY_WINDOW_SIZE: int = 10

    def record_success(self, latency_ms: float):
        """Record successful provider call"""
        self.consecutive_failures = 0
        self.last_success_time = time.monotonic()
        self.health = ProviderHealth.HEALTHY
        self._recovery_cooldown = self.BASE_RECOVERY_COOLDOWN  # Reset cooldown
        self._update_latency(latency_ms)

    def record_failure(self):
        """Record failed provider call with exponential backoff"""
        self.consecutive_failures += 1
        self.last_failure_time = time.monotonic()

        # Exponential backoff: 60s, 120s, 240s, 480s, max 600s
        self._recovery_cooldown = min(
            self.BASE_RECOVERY_COOLDOWN * (2 ** (self.consecutive_failures - 1)),
            self.MAX_RECOVERY_COOLDOWN,
        )

        if self.consecutive_failures >= self.FAILURE_THRESHOLD:
            self.health = ProviderHealth.UNHEALTHY
        elif self.consecutive_failures >= self.DEGRADED_THRESHOLD:
            self.health = ProviderHealth.DEGRADED

    def should_attempt(self) -> bool:
        """Can we try this provider right now?"""
        if self.health in (ProviderHealth.HEALTHY, ProviderHealth.UNKNOWN):
            return True
        if self.health == ProviderHealth.DEGRADED:
            return True  # still usable, just not preferred
        # UNHEALTHY: only retry after cooldown
        elapsed = time.monotonic() - self.last_failure_time
        if elapsed >= self._recovery_cooldown:
            self.health = ProviderHealth.UNKNOWN  # give it another chance
            return True
        return False

    def _update_latency(self, latency_ms: float):
        """Update rolling average latency"""
        self._latency_window.append(latency_ms)
        if len(self._latency_window) > self.LATENCY_WINDOW_SIZE:
            self._latency_window.pop(0)
        self.avg_latency_ms = sum(self._latency_window) / len(self._latency_window)


# Data-driven provider schema - single source of truth
PROVIDER_SCHEMA = {
    "fireworks": {
        "api_key_var": "FIREWORKS_API_KEY",
        "model_var": "FIREWORKS_MODEL",
        "base_url_var": "FIREWORKS_API_BASE",
        "default_base_url": "https://api.fireworks.ai/inference/v1",
        "default_model": "accounts/fireworks/models/deepseek-v4-flash",
        "available_models": [
            "accounts/fireworks/models/llama-v3p1-8b-instruct",
            "accounts/fireworks/models/llama-v3p1-70b-instruct",
            "accounts/fireworks/models/qwen2p5-coder-32b-instruct",
            "accounts/fireworks/models/deepseek-v3",
            "accounts/fireworks/models/deepseek-v4-flash",
        ],
        "provider_class": FireworksProvider,
        "confidence_score": 0.9,
    },
    "openai": {
        "api_key_var": "OPENAI_API_KEY",
        "model_var": "OPENAI_MODEL",
        "base_url_var": "OPENAI_API_BASE",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-5.4-mini",
        "available_models": [
            "gpt-4.1-mini",
            "gpt-5.4-mini",
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4-turbo",
            "o3-mini",
        ],
        "provider_class": OpenAIProvider,
        "confidence_score": 0.85,
    },
    "local": {
        "api_key_var": None,  # No API key needed
        "model_var": "LOCAL_LLM_MODEL",
        "base_url_var": "LOCAL_LLM_BASE_URL",
        "default_base_url": "http://localhost:5000",
        "default_model": "llama2-7b",
        "available_models": [],  # Dynamic — depends on what the user has pulled
        "provider_class": LocalProvider,
        "max_retries": 1,
        "timeout": 60,
        "confidence_score": 0.6,
    },
    "gemini": {
        "api_key_var": "GEMINI_API_KEY",
        "model_var": "GEMINI_MODEL",
        "base_url_var": "GEMINI_API_BASE",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-3.5-flash",
        "available_models": [
            "gemini-3.5-flash",
        ],
        "provider_class": GeminiProvider,
        "confidence_score": 0.8,
    },
    "huggingface": {
        "api_key_var": "HUGGINGFACE_API_KEY",
        "model_var": "HUGGINGFACE_MODEL",
        "base_url_var": "HUGGINGFACE_API_URL",
        "default_base_url": "https://api-inference.huggingface.co/models",
        "default_model": "mistralai/Mistral-Large-Instruct-2411",
        "available_models": [
            "mistralai/Mistral-Large-Instruct-2411",
            "meta-llama/Llama-3.3-70B-Instruct",
        ],
        "provider_class": HuggingFaceProvider,
        "confidence_score": 0.75,
    },
    "openrouter": {
        "api_key_var": "OPENROUTER_API_KEY",
        "model_var": "OPENROUTER_MODEL",
        "base_url_var": "OPENROUTER_API_BASE",
        "default_base_url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-sonnet-4-6",
        "available_models": [],  # Dynamic — depends on OpenRouter's catalog
        # OpenAI-compatible wire protocol, but namespace-aware capability
        # detection (vendor/model ids). See OpenRouterProvider.
        "provider_class": OpenRouterProvider,
        "confidence_score": 0.8,
    },
    "anthropic": {
        "api_key_var": "ANTHROPIC_API_KEY",
        "model_var": "ANTHROPIC_MODEL",
        "base_url_var": "ANTHROPIC_API_BASE",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-6",
        "available_models": [
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-haiku-4-5-20251001",
            "claude-3-5-sonnet-20241022",
        ],
        "provider_class": AnthropicProvider,
        "confidence_score": 0.85,
    },
    "groq": {
        "api_key_var": "GROQ_API_KEY",
        "model_var": "GROQ_MODEL",
        "base_url_var": "GROQ_API_BASE",
        "default_base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "available_models": [
            "meta-llama/Llama-4-Scout-17B-16E-Instruct",
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
        ],
        "provider_class": GroqProvider,
        "confidence_score": 0.88,
    },
    "cohere": {
        "api_key_var": "COHERE_API_KEY",
        "model_var": "COHERE_MODEL",
        "base_url_var": "COHERE_API_BASE",
        "default_base_url": "https://api.cohere.ai/v2",
        "default_model": "command-r-plus",
        "available_models": [
            "command-r-plus",
            "command-r",
            "command-light",
        ],
        "provider_class": CohereProvider,
        "confidence_score": 0.82,
    },
}


class ProviderRegistry:
    """Central registry for managing LLM providers"""

    def __init__(self, settings=None):
        self.logger = logging.getLogger(__name__)

        # Get settings if not provided
        if settings is None:
            try:
                from faultmaven.config.settings import get_settings

                settings = get_settings()
            except Exception:
                settings = None

        self.settings = settings
        self._providers: Dict[str, BaseLLMProvider] = {}
        self._fallback_chain: List[str] = []
        self._initialized = False

        # Provider health tracking (stateful routing)
        self._provider_states: Dict[str, ProviderState] = {}
        self._sticky_provider: Optional[str] = None  # last known good provider
        self._routing_initialized = False  # Track if we've logged routing config

        # Don't initialize immediately - wait for first use
        # self._initialize_from_environment()

    def _ensure_initialized(self):
        """Ensure providers are initialized (lazy initialization)"""
        if not self._initialized:
            self.logger.info("Lazy-initializing provider registry...")

            # Re-fetch settings to ensure we have the latest configuration
            if self.settings is None:
                from faultmaven.config.settings import get_settings

                self.settings = get_settings()

            self._initialize_from_settings()
            self._initialized = True

    def _initialize_from_settings(self):
        """Initialize providers based on settings configuration using schema"""
        if self.settings:
            self.logger.info(
                f"Provider configuration: CHAT_PROVIDER={self.settings.llm.provider}"
            )

            # Get primary provider from settings (convert enum to string)
            primary_provider = self.settings.llm.provider
            if hasattr(primary_provider, "value"):
                primary_provider = primary_provider.value
        else:
            # No fallback - unified settings system is mandatory
            from faultmaven.models.exceptions import LLMProviderError

            raise LLMProviderError(
                "LLM provider registry requires unified settings system to be available",
                error_code="LLM_CONFIG_ERROR",
                context={"settings_available": self.settings is not None},
            )

        # Safeguard: ensure it's a string for dictionary lookup
        primary_provider_key = str(
            primary_provider.value
            if hasattr(primary_provider, "value")
            else primary_provider
        ).lower()
        if primary_provider_key not in PROVIDER_SCHEMA:
            self.logger.warning(
                f"⚠️ Unknown primary provider {primary_provider_key} (type={type(primary_provider)}). Defaulting to 'local'."
            )
            primary_provider_key = "local"

        primary_provider = primary_provider_key

        # Always initialize all providers that have valid API keys.
        # Strict mode only affects routing (fallback chain), not initialization.
        # This ensures admins can configure and test any provider via the dashboard.
        providers_to_init = PROVIDER_SCHEMA

        # Initialize all providers with valid keys
        for provider_name, schema in providers_to_init.items():
            try:
                self.logger.info(
                    f"🔍 Attempting to initialize provider: {provider_name}"
                )
                config = self._create_provider_config(provider_name, schema)
                if config:
                    self._initialize_provider(provider_name, config)
                    self.logger.info(
                        f"✅ Provider '{provider_name}' initialized successfully"
                    )
                else:
                    self.logger.warning(
                        f"⚠️ Provider '{provider_name}' config returned None (skipped)"
                    )
            except Exception as e:
                self.logger.warning(
                    f"❌ Failed to initialize provider {provider_name}: {e}"
                )

        # Set up fallback chain with primary first
        self._setup_fallback_chain(primary_provider)

    def _create_provider_config(
        self, provider_name: str, schema: Dict
    ) -> Optional[ProviderConfig]:
        """Create provider configuration from schema and settings.

        Note: This method requires settings to be available. All configuration
        comes from the unified settings system (faultmaven.config.settings).
        """
        api_key = None
        model = None
        base_url = None
        # Extended-thinking knobs — only the Anthropic branch sets these
        # (#1116); every other provider leaves them None (= no thinking
        # configuration sent, identical to pre-#1116 requests).
        thinking_mode = None
        thinking_budget_tokens = None

        # Settings is required - use settings-based configuration
        llm_settings = self.settings.llm

        if provider_name == "fireworks":
            api_key = (
                llm_settings.fireworks_api_key.get_secret_value()
                if llm_settings.fireworks_api_key
                else None
            )
            model = llm_settings.fireworks_model or schema["default_model"]
            base_url = llm_settings.fireworks_base_url or schema["default_base_url"]
        elif provider_name == "openai":
            api_key = (
                llm_settings.openai_api_key.get_secret_value()
                if llm_settings.openai_api_key
                else None
            )
            model = llm_settings.openai_model or schema["default_model"]
            base_url = llm_settings.openai_base_url or schema["default_base_url"]
        elif provider_name == "local":
            api_key = None  # Local doesn't need API key
            model = llm_settings.local_model
            base_url = llm_settings.local_url
        elif provider_name == "anthropic":
            api_key = (
                llm_settings.anthropic_api_key.get_secret_value()
                if llm_settings.anthropic_api_key
                else None
            )
            model = llm_settings.anthropic_model or schema["default_model"]
            base_url = llm_settings.anthropic_base_url or schema["default_base_url"]
            thinking_mode = llm_settings.anthropic_thinking_mode
            thinking_budget_tokens = llm_settings.anthropic_thinking_budget_tokens
        elif provider_name == "gemini":
            api_key = (
                llm_settings.gemini_api_key.get_secret_value()
                if llm_settings.gemini_api_key
                else None
            )
            model = llm_settings.gemini_model or schema["default_model"]
            base_url = llm_settings.gemini_base_url or schema["default_base_url"]
        elif provider_name == "huggingface":
            api_key = (
                llm_settings.huggingface_api_key.get_secret_value()
                if llm_settings.huggingface_api_key
                else None
            )
            model = llm_settings.huggingface_model or schema["default_model"]
            base_url = llm_settings.huggingface_base_url or schema["default_base_url"]
        elif provider_name == "openrouter":
            api_key = (
                llm_settings.openrouter_api_key.get_secret_value()
                if llm_settings.openrouter_api_key
                else None
            )
            model = llm_settings.openrouter_model or schema["default_model"]
            base_url = llm_settings.openrouter_base_url or schema["default_base_url"]
        elif provider_name == "groq":
            api_key = (
                llm_settings.groq_api_key.get_secret_value()
                if llm_settings.groq_api_key
                else None
            )
            model = llm_settings.groq_model or schema["default_model"]
            base_url = llm_settings.groq_base_url or schema["default_base_url"]

        elif provider_name == "cohere":
            api_key = (
                llm_settings.cohere_api_key.get_secret_value()
                if llm_settings.cohere_api_key
                else None
            )
            model = llm_settings.cohere_model or schema["default_model"]
            base_url = llm_settings.cohere_base_url or schema["default_base_url"]

        if schema.get("api_key_var") and not api_key and provider_name != "local":
            self.logger.warning(
                f"⚠️ Skipping provider '{provider_name}': "
                f"API key '{schema['api_key_var']}' not found in settings"
            )
            return None

        # For local provider, require model and base_url from settings
        if provider_name == "local":
            if not model:
                self.logger.warning(
                    f"❌ Local provider requires LOCAL_LLM_MODEL in settings"
                )
                return None
            if not base_url:
                self.logger.warning(
                    f"❌ Local provider requires LOCAL_LLM_URL in settings"
                )
                return None

        # Get timeout and max_retries from schema or settings.
        # Prefer the per-provider override so slow models (Fireworks/DeepSeek,
        # local Ollama) get their own ceiling without widening the global default.
        # Note: `or` falls through on schema timeout=0, which is fine — 0 is
        # not a meaningful HTTP timeout and would never be set intentionally.
        timeout = schema.get("timeout") or llm_settings.timeout_for_provider(
            provider_name
        )
        max_retries = schema.get("max_retries", llm_settings.max_retries)

        self.logger.debug(
            f"Provider '{provider_name}': model={model}, "
            f"api_key={'SET' if api_key else 'NOT_SET'}, timeout={timeout}s"
        )

        return ProviderConfig(
            name=provider_name,
            api_key=api_key,
            base_url=base_url,
            models=[model],
            max_retries=max_retries,
            timeout=timeout,
            confidence_score=schema["confidence_score"],
            thinking_mode=thinking_mode,
            thinking_budget_tokens=thinking_budget_tokens,
        )

    def _initialize_provider(self, name: str, config: ProviderConfig):
        """Initialize a single provider using schema"""
        if name not in PROVIDER_SCHEMA:
            self.logger.warning(f"Unknown provider in schema: {name}")
            return

        schema = PROVIDER_SCHEMA[name]
        provider_class = schema["provider_class"]

        try:
            provider = provider_class(config)

            if provider.is_available():
                self._providers[name] = provider
                self.logger.info(f"✅ Provider '{name}' initialized successfully")
            else:
                self.logger.warning(
                    f"❌ Provider '{name}' not available (missing config)"
                )
        except Exception as e:
            self.logger.error(f"❌ Error creating provider '{name}': {e}")

    def _setup_fallback_chain(self, primary_provider: str):
        """Set up the provider fallback chain.

        Uses settings.llm.strict_provider_mode to determine if fallbacks are allowed.
        """
        # Start with primary provider
        chain = [primary_provider] if primary_provider in self._providers else []

        # Check if strict mode is enabled (from settings)
        strict_mode = self.settings.llm.strict_provider_mode

        if strict_mode:
            # In strict mode, only use the primary provider
            self.logger.info(
                f"🔒 Strict provider mode enabled - using only '{primary_provider}', no fallbacks"
            )
        else:
            # Add other available providers as fallbacks
            fallback_order = ["fireworks", "openai", "local"]
            for provider in fallback_order:
                if provider != primary_provider and provider in self._providers:
                    chain.append(provider)

        self._fallback_chain = chain

        # Initialize provider health states for all providers in chain
        self._provider_states = {name: ProviderState(name=name) for name in chain}

        if strict_mode and len(chain) == 1:
            self.logger.info(f"Provider chain (strict mode): {chain[0]} ONLY")
        else:
            self.logger.info(f"Provider fallback chain: {' -> '.join(chain)}")

    def register_provider(self, name: str, provider_class: Type[BaseLLMProvider]):
        """Register a custom provider class"""
        self._provider_classes[name] = provider_class
        self.logger.info(f"Registered custom provider class: {name}")

    def get_provider(self, name: str) -> Optional[BaseLLMProvider]:
        """Get a specific provider by name"""
        self._ensure_initialized()
        return self._providers.get(name)

    def create_provider_for_test(self, name: str) -> Optional[BaseLLMProvider]:
        """Create a temporary provider instance for connection testing.

        Unlike get_provider(), this works even in strict mode by initializing
        the provider on-the-fly from current settings without adding it to the
        active registry. Returns None if the API key is missing.
        """
        self._ensure_initialized()
        if name not in PROVIDER_SCHEMA:
            return None
        schema = PROVIDER_SCHEMA[name]
        config = self._create_provider_config(name, schema)
        if not config:
            return None
        provider_class = schema["provider_class"]
        try:
            provider = provider_class(config)
            return provider if provider.is_available() else None
        except Exception as e:
            self.logger.warning(f"Failed to create test provider '{name}': {e}")
            return None

    def get_available_providers(self) -> List[str]:
        """Get list of available provider names"""
        self._ensure_initialized()
        return list(self._providers.keys())

    def get_all_provider_names(self) -> List[str]:
        """Get list of all provider names defined in schema"""
        self._ensure_initialized()
        return list(PROVIDER_SCHEMA.keys())

    def get_fallback_chain(self) -> List[str]:
        """Get the current fallback chain"""
        self._ensure_initialized()
        return self._fallback_chain.copy()

    def _get_routing_order(self) -> List[str]:
        """Build provider attempt order: sticky first, then healthy, then degraded.

        Implements smart routing that:
        1. Prefers last successful provider (sticky routing)
        2. Routes by health status (HEALTHY > UNKNOWN > DEGRADED)
        3. Skips UNHEALTHY providers until recovery cooldown expires
        4. Falls back to original chain order for tie-breaking
        """
        if self._sticky_provider and self._sticky_provider in self._provider_states:
            state = self._provider_states[self._sticky_provider]
            if state.should_attempt():
                # Sticky provider is still viable — put it first
                rest = [p for p in self._fallback_chain if p != self._sticky_provider]
                return [self._sticky_provider] + rest

        # No sticky or sticky is down — route by health
        attemptable = [
            (name, state)
            for name, state in self._provider_states.items()
            if state.should_attempt()
        ]

        # Sort: HEALTHY > UNKNOWN > DEGRADED, then by original chain order
        health_priority = {
            ProviderHealth.HEALTHY: 0,
            ProviderHealth.UNKNOWN: 1,
            ProviderHealth.DEGRADED: 2,
        }
        attemptable.sort(
            key=lambda x: (
                health_priority.get(x[1].health, 3),
                self._fallback_chain.index(x[0]),
            )
        )
        return [name for name, _ in attemptable]

    def get_provider_health_summary(self) -> Dict[str, Dict]:
        """Get health status summary for all providers.

        Useful for debugging/monitoring endpoints.

        Returns:
            Dict with provider health metrics including:
            - health status
            - consecutive failures
            - average latency
            - sticky status
        """
        self._ensure_initialized()
        return {
            name: {
                "health": state.health.value,
                "consecutive_failures": state.consecutive_failures,
                "avg_latency_ms": round(state.avg_latency_ms, 1),
                "sticky": name == self._sticky_provider,
                "last_success": state.last_success_time,
                "last_failure": state.last_failure_time,
            }
            for name, state in self._provider_states.items()
        }

    async def route_request(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        confidence_threshold: float = 0.8,
        provider_override: Optional[str] = None,
        **kwargs,
    ) -> LLMResponse:
        """Route request through health-aware fallback chain.

        Uses smart routing to:
        - Prefer last successful provider (sticky routing)
        - Skip unhealthy providers until recovery cooldown
        - Track provider health and performance metrics

        Args:
            prompt: Input prompt
            model: Specific model to use (optional)
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            confidence_threshold: Minimum confidence threshold
            provider_override: When set, route exclusively through this
                provider (by name) instead of the CHAT_PROVIDER fallback
                chain. Used by callers that need capability-specific
                routing (e.g. structured-output calls forced through a
                known-STRICT-capable provider via
                ``STRUCTURED_OUTPUT_PROVIDER``). If the named provider is
                not initialized in the registry (no API key configured),
                falls back to the normal fallback chain so a misconfigured
                override doesn't break the whole investigation.
            **kwargs: Additional parameters

        Returns:
            LLMResponse from successful provider

        Raises:
            Exception: If all providers fail
        """
        self._ensure_initialized()

        # Log routing config only once
        if not self._routing_initialized:
            self.logger.info(
                f"🔍 Smart routing initialized: chain={' → '.join(self._fallback_chain)}"
            )
            self._routing_initialized = True

        # When tools are present, bypass confidence filtering. Tool-calling
        # responses have different confidence characteristics (e.g. empty text
        # + valid tool calls is normal). The caller (DA tool loop) has its own
        # retry logic and must see the raw response to decide what to do.
        skip_confidence_check = bool(kwargs.get("tools"))

        # Determine routing order. provider_override (when set AND that
        # provider is initialized) bypasses the normal fallback chain so
        # capability-specific calls land deterministically on the
        # requested provider — no silent drift to CHAT_PROVIDER if the
        # override target is healthy.
        if provider_override and provider_override in self._providers:
            routing_order = [provider_override]
            self.logger.info(
                f"🎯 Provider override: routing this call exclusively through "
                f"'{provider_override}' (no fallback chain)"
            )
        else:
            if provider_override:
                self.logger.warning(
                    f"⚠️ Provider override '{provider_override}' requested but "
                    f"not initialized (no API key?) — falling back to normal "
                    f"routing chain"
                )
            # Get health-aware routing order
            routing_order = self._get_routing_order()

        if not routing_order:
            # All providers unhealthy — force retry primary as last resort
            self.logger.warning("⚠️ All providers unhealthy, forcing primary retry")
            routing_order = [self._fallback_chain[0]]

        last_error = None
        best_low_confidence_response = None
        best_low_confidence_score = -1.0

        for provider_name in routing_order:
            provider = self._providers.get(provider_name)
            if not provider:
                continue

            state = self._provider_states.get(provider_name)
            if not state:
                continue

            try:
                # Log only on provider change (not every request)
                if provider_name != self._sticky_provider:
                    self.logger.info(f"Trying provider: {provider_name}")

                # Track call start time for latency measurement
                start_time = time.monotonic()

                # Retry loop for ModelLoadingException
                max_model_loading_retries = 2
                for retry_attempt in range(max_model_loading_retries + 1):
                    try:
                        response = await provider.generate(
                            prompt=prompt,
                            model=model,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            **kwargs,
                        )
                        break  # Success, exit retry loop
                    except ModelLoadingException as mle:
                        if retry_attempt < max_model_loading_retries:
                            self.logger.info(
                                f"⏳ Model '{mle.model_name}' is loading, "
                                f"waiting {mle.retry_after}s before retry "
                                f"({retry_attempt + 1}/{max_model_loading_retries})"
                            )
                            await asyncio.sleep(mle.retry_after)
                        else:
                            self.logger.warning(
                                f"⚠️ Model '{mle.model_name}' still loading after "
                                f"{max_model_loading_retries} retries, trying next provider"
                            )
                            raise  # Re-raise to trigger fallback to next provider

                # Calculate latency
                latency_ms = (time.monotonic() - start_time) * 1000

                # Meter this real, billed provider call BEFORE the confidence
                # branch, so a response that gets discarded as low-confidence
                # below is still counted — it was paid for. This is the single
                # chokepoint where fallback-chain spend becomes visible.
                _kept = skip_confidence_check or (
                    response.confidence >= confidence_threshold
                )
                record_provider_call(
                    provider_name,
                    model or getattr(response, "model", None) or "unknown",
                    response,
                    latency_ms,
                    outcome="kept" if _kept else "low_confidence",
                )

                # For tool-calling requests, skip confidence filtering and
                # return the response directly. The caller has its own retry
                # logic and needs to inspect tool_calls regardless of confidence.
                if skip_confidence_check:
                    state.record_success(latency_ms)
                    if provider_name != self._sticky_provider:
                        self.logger.info(
                            f"🔄 Switched to {provider_name} "
                            f"(was: {self._sticky_provider or 'none'})"
                        )
                    self._sticky_provider = provider_name
                    return response

                # Check confidence threshold
                if response.confidence >= confidence_threshold:
                    # Record success with latency
                    state.record_success(latency_ms)

                    # Update sticky provider (log only on change)
                    if provider_name != self._sticky_provider:
                        self.logger.info(
                            f"🔄 Switched to {provider_name} "
                            f"(was: {self._sticky_provider or 'none'})"
                        )
                    self._sticky_provider = provider_name

                    return response
                else:
                    # Log the actual response content for debugging
                    self.logger.warning(
                        f"⚠️ Low confidence from {provider_name} "
                        f"({response.confidence:.2f} < {confidence_threshold})"
                    )
                    self.logger.info(
                        f"🔍 Low confidence response content from {provider_name}: "
                        f"{response.content[:200]}{'...' if len(response.content) > 200 else ''}"
                    )

                    # Keep track of the best low-confidence response
                    if response.confidence > best_low_confidence_score:
                        best_low_confidence_response = response
                        best_low_confidence_score = response.confidence
                        self.logger.info(
                            f"📝 Keeping {provider_name} as best low-confidence option "
                            f"(confidence: {response.confidence:.2f})"
                        )

                    continue

            except Exception as e:
                # Record failure for health tracking
                state.record_failure()

                self.logger.warning(
                    f"❌ {provider_name} failed (consecutive: {state.consecutive_failures}, "
                    f"health: {state.health.value}): {e}"
                )
                last_error = e
                continue

        # If we have a low-confidence response, return it with appropriate metadata
        if best_low_confidence_response:
            self.logger.info(
                f"🎯 Returning best low-confidence response "
                f"(confidence: {best_low_confidence_score:.2f}) instead of failing completely"
            )
            # Add metadata to indicate this is a low-confidence response
            best_low_confidence_response.provider = (
                f"{best_low_confidence_response.provider} (low-confidence)"
            )
            return best_low_confidence_response

        # All providers failed completely — re-raise the last error directly
        # so that retryability metadata (e.g. LLMException.retryable) is
        # preserved for upstream retry logic in call_external().
        self.logger.error(f"All providers failed. Last error: {last_error}")
        if last_error is not None:
            raise last_error
        raise Exception("All providers failed with no error details")

    def get_provider_status(self) -> Dict[str, Dict[str, any]]:
        """Get status information for all providers"""
        self._ensure_initialized()
        status = {}

        for name, provider in self._providers.items():
            schema = PROVIDER_SCHEMA.get(name, {})
            available = schema.get("available_models", [])
            selected = provider.config.default_model or (
                provider.config.models[0] if provider.config.models else None
            )
            status[name] = {
                "available": provider.is_available(),
                "models": provider.get_supported_models(),
                "selected_model": selected,
                "available_models": available,
                "confidence_score": provider.config.confidence_score,
                "in_fallback_chain": name in self._fallback_chain,
            }

        return status

    @staticmethod
    def get_available_models_for(provider_name: str) -> list[str]:
        """Get available model choices for a provider from the schema."""
        schema = PROVIDER_SCHEMA.get(provider_name, {})
        return schema.get("available_models", [])


# Global registry instance
_registry = None


def get_registry(settings=None) -> ProviderRegistry:
    """Get the global provider registry instance"""
    global _registry
    if _registry is None:
        _registry = ProviderRegistry(settings=settings)
    return _registry


def reset_registry():
    """Reset the global registry (mainly for testing)"""
    global _registry
    _registry = None


def get_valid_provider_names() -> List[str]:
    """Get list of valid provider names for CHAT_PROVIDER"""
    return list(PROVIDER_SCHEMA.keys())


def print_provider_options():
    """Print all valid provider options with descriptions"""
    print("Valid CHAT_PROVIDER options:")
    for name, schema in PROVIDER_SCHEMA.items():
        provider_class = schema["provider_class"].__name__
        default_model = schema["default_model"]
        print(f'  "{name}" - {provider_class} ({default_model})')
    print(f'\nExample: CHAT_PROVIDER="fireworks"')
