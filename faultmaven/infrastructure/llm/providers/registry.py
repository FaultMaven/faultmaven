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

from .anthropic import AnthropicProvider
from .base import BaseLLMProvider, LLMResponse, ProviderConfig
from .cohere_provider import CohereProvider
from .fireworks_provider import FireworksProvider
from .gemini import GeminiProvider
from .groq_provider import GroqProvider
from .huggingface import HuggingFaceProvider
from .local_provider import LocalProvider
from .openai_provider import OpenAIProvider


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
        "default_model": "accounts/fireworks/models/llama-v3p1-8b-instruct",
        "provider_class": FireworksProvider,
        "confidence_score": 0.9,
        # max_retries and timeout loaded from settings.llm.max_retries and settings.llm.request_timeout
    },
    "openai": {
        "api_key_var": "OPENAI_API_KEY",
        "model_var": "OPENAI_MODEL",
        "base_url_var": "OPENAI_API_BASE",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "provider_class": OpenAIProvider,
        "confidence_score": 0.85,
        # max_retries and timeout loaded from settings
    },
    "local": {
        "api_key_var": None,  # No API key needed
        "model_var": "LOCAL_LLM_MODEL",
        "base_url_var": "LOCAL_LLM_BASE_URL",
        "default_base_url": "http://localhost:5000",  # Default llama.cpp server endpoint
        "default_model": "llama2-7b",  # Default model name
        "provider_class": LocalProvider,
        "max_retries": 1,  # Local typically needs fewer retries
        "timeout": 60,  # Local may need more time
        "confidence_score": 0.6,
        # Note: Local provider has different defaults for retries/timeout than settings
    },
    "gemini": {
        "api_key_var": "GEMINI_API_KEY",
        "model_var": "GEMINI_MODEL",
        "base_url_var": "GEMINI_API_BASE",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-1.5-pro",
        "provider_class": GeminiProvider,
        "confidence_score": 0.8,
        # max_retries and timeout loaded from settings
    },
    "huggingface": {
        "api_key_var": "HUGGINGFACE_API_KEY",
        "model_var": "HUGGINGFACE_MODEL",
        "base_url_var": "HUGGINGFACE_API_URL",
        "default_base_url": "https://api-inference.huggingface.co/models",
        "default_model": "tiiuae/falcon-7b-instruct",
        "provider_class": HuggingFaceProvider,
        "confidence_score": 0.75,
        # max_retries and timeout loaded from settings
    },
    "openrouter": {
        "api_key_var": "OPENROUTER_API_KEY",
        "model_var": "OPENROUTER_MODEL",
        "base_url_var": "OPENROUTER_API_BASE",
        "default_base_url": "https://openrouter.ai/api/v1",
        "default_model": "openrouter-default",
        "provider_class": OpenAIProvider,  # Compatible API
        "confidence_score": 0.8,
        # max_retries and timeout loaded from settings
    },
    "anthropic": {
        "api_key_var": "ANTHROPIC_API_KEY",
        "model_var": "ANTHROPIC_MODEL",
        "base_url_var": "ANTHROPIC_API_BASE",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-sonnet-20240229",
        "provider_class": AnthropicProvider,
        "confidence_score": 0.85,
        # max_retries and timeout loaded from settings
    },
    "groq": {
        "api_key_var": "GROQ_API_KEY",
        "model_var": "GROQ_MODEL",
        "base_url_var": "GROQ_API_BASE",
        "default_base_url": "https://api.groq.com/openai/v1",
        "default_model": "meta-llama/Llama-4-Scout-17B-16E-Instruct",
        "provider_class": GroqProvider,
        "confidence_score": 0.88,  # Groq is fast and reliable
        # max_retries and timeout loaded from settings
    },
    "cohere": {
        "api_key_var": "COHERE_API_KEY",
        "model_var": "COHERE_CHAT_MODEL",
        "base_url_var": "COHERE_API_BASE",
        "default_base_url": "https://api.cohere.ai/v2",
        "default_model": "command-r-plus",
        "provider_class": CohereProvider,
        "confidence_score": 0.82,  # Between Groq (0.88) and Gemini/OpenRouter (0.8)
        # max_retries and timeout loaded from settings
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
        """Ensure providers are initialized before use.

        Note: This method no longer reloads .env files. All environment variable
        loading is handled by the unified settings system (faultmaven.config.settings).
        """
        if not self._initialized:
            self.logger.info("🔍 Lazy-initializing provider registry...")

            # Re-fetch settings to ensure we have the latest configuration
            if self.settings is None:
                from faultmaven.config.settings import get_settings

                self.settings = get_settings()

            self._initialize_from_settings()
            self._initialized = True

    def _initialize_from_settings(self):
        """Initialize providers based on settings configuration using schema"""
        if self.settings:
            # Use settings-based configuration
            self.logger.info(f"🔍 Settings-based provider configuration:")
            self.logger.info(f"🔍 CHAT_PROVIDER: {self.settings.llm.provider}")
            self.logger.info(
                f"🔍 LOCAL_LLM_URL: {self.settings.llm.local_url or 'NOT_SET'}"
            )
            self.logger.info(
                f"🔍 LOCAL_LLM_MODEL: {self.settings.llm.local_model or 'NOT_SET'}"
            )
            fireworks_key_preview = "NOT_SET"
            if self.settings.llm.fireworks_api_key:
                fireworks_key_preview = (
                    self.settings.llm.fireworks_api_key.get_secret_value()[:10] + "..."
                )
            self.logger.info(f"🔍 FIREWORKS_API_KEY: {fireworks_key_preview}")

            # Get primary provider from settings (convert enum to string)
            primary_provider = self.settings.llm.provider
            # Convert enum to string if needed (LLMProvider enum inherits from str)
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

        # Validate that primary_provider is in schema
        if primary_provider not in PROVIDER_SCHEMA:
            valid_options = list(PROVIDER_SCHEMA.keys())
            self.logger.error(
                f"❌ Invalid CHAT_PROVIDER: '{primary_provider}'. "
                f"Valid options: {valid_options}. Defaulting to 'local'"
            )
            primary_provider = "local"

        # Check if strict mode is enabled
        strict_mode = self.settings.llm.strict_provider_mode

        # Determine which providers to initialize
        if strict_mode:
            # In strict mode, only initialize the primary provider
            providers_to_init = {primary_provider: PROVIDER_SCHEMA[primary_provider]}
            self.logger.info(
                f"🔒 Strict mode enabled - initializing only '{primary_provider}'"
            )
        else:
            # In normal mode, initialize all providers
            providers_to_init = PROVIDER_SCHEMA

        # Initialize selected providers
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
            model = llm_settings.groq_chat_model or schema["default_model"]
            base_url = llm_settings.groq_base_url or schema["default_base_url"]

        elif provider_name == "cohere":
            api_key = (
                llm_settings.cohere_api_key.get_secret_value()
                if llm_settings.cohere_api_key
                else None
            )
            model = llm_settings.cohere_chat_model or schema["default_model"]
            base_url = llm_settings.cohere_base_url or schema["default_base_url"]

        # Skip providers without required API keys (except local)
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

        # Debug configuration values
        self.logger.info(f"🔍 Provider '{provider_name}' config:")
        self.logger.info(f"🔍   Model: {model} (from {schema.get('model_var', 'N/A')})")
        self.logger.info(
            f"🔍   Base URL: {base_url} (from {schema.get('base_url_var', 'default')})"
        )
        self.logger.info(f"🔍   API Key: {'SET' if api_key else 'NOT_SET'}")

        # Get timeout and max_retries from schema or settings
        timeout = schema.get("timeout", llm_settings.request_timeout)
        max_retries = schema.get("max_retries", llm_settings.max_retries)

        self.logger.info(f"🔍   Timeout: {timeout}s")
        self.logger.info(f"🔍   Max Retries: {max_retries}")

        return ProviderConfig(
            name=provider_name,
            api_key=api_key,
            base_url=base_url,
            models=[model],
            max_retries=max_retries,
            timeout=timeout,
            confidence_score=schema["confidence_score"],
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

        # All providers failed completely
        error_msg = f"All providers failed. Last error: {last_error}"
        self.logger.error(error_msg)
        raise Exception(error_msg)

    def get_provider_status(self) -> Dict[str, Dict[str, any]]:
        """Get status information for all providers"""
        self._ensure_initialized()
        status = {}

        for name, provider in self._providers.items():
            status[name] = {
                "available": provider.is_available(),
                "models": provider.get_supported_models(),
                "confidence_score": provider.config.confidence_score,
                "in_fallback_chain": name in self._fallback_chain,
            }

        return status


# Global registry instance
_registry = None


def get_registry(settings=None) -> ProviderRegistry:
    """Get the global provider registry instance"""
    global _registry
    if _registry is None:
        print("🔍 Creating new ProviderRegistry instance...")
        _registry = ProviderRegistry(settings=settings)
        print("🔍 ProviderRegistry instance created")
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
