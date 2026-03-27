"""
New LLM Router using Centralized Provider Registry.

This router replaces the old scattered configuration approach with a clean,
centralized provider registry system that handles provider management,
fallback strategies, and configuration in a unified way.

Inherits from BaseExternalClient for unified logging, retry logic, and
circuit breaker patterns for external LLM provider calls.
"""

import functools
import os
from typing import Any

from faultmaven.config.settings import get_settings
from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.models import DataType
from faultmaven.models.interfaces import ILLMProvider

from .cache import SemanticCache
from .providers import LLMResponse, get_registry

# Opik native tracing for LLM calls
try:
    import opik
    from opik import opik_context

    OPIK_AVAILABLE = True
except ImportError:
    OPIK_AVAILABLE = False

TELEMETRY_PAYLOAD_MAX_CHARS = 8000


def _opik_track_llm(name: str):
    """Decorator: wraps function with @opik.track(type='llm') when Opik is available."""
    if OPIK_AVAILABLE:
        return opik.track(
            name=name, type="llm", capture_input=False, capture_output=False
        )

    # No-op passthrough when Opik is not installed
    def identity(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await func(*args, **kwargs)

        return wrapper

    return identity


class LLMRouter(BaseExternalClient, ILLMProvider):
    """Simplified LLM router using centralized provider registry"""

    def __init__(self, confidence_threshold: float = 0.8):
        # Initialize BaseExternalClient
        super().__init__(
            client_name="llm_router",
            service_name="LLM_Providers",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,  # Lower threshold for LLM failures
            circuit_breaker_timeout=30,  # Shorter timeout for LLM recovery
        )

        self.sanitizer = DataSanitizer()
        self.cache = SemanticCache()
        self.confidence_threshold = confidence_threshold
        self._router_initialized = False  # Track router initialization separately

        # Get timeout from settings with environment variable override
        self.settings = get_settings()
        self.request_timeout = float(
            os.getenv("LLM_REQUEST_TIMEOUT", str(self.settings.llm.request_timeout))
        )

        # Don't initialize registry immediately - wait for first use
        self.logger.info(
            f"🔍 LLMRouter created, request timeout: {self.request_timeout}s"
        )
        self.logger.info("🔍 LLMRouter registry will be initialized on first use")

    @property
    def registry(self):
        """Always fetch the current global registry so hot-reloads take effect."""
        return get_registry()

    @_opik_track_llm("llm_router_route")
    async def route(
        self,
        prompt: str | None = None,
        model: str | None = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        data_type: DataType | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        response_format: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        case_id: str | None = None,
    ) -> LLMResponse:
        """
        Route request through the centralized provider registry

        Args:
            prompt: Input prompt (optional if messages is provided)
            model: Specific model to use (optional)
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            data_type: Type of data being processed

        Returns:
            LLMResponse with generated content
        """
        # Validate prompt or messages
        if prompt is None and not messages:
            raise TypeError("Either prompt or messages must be provided")

        # Sanitize before sending to external providers (conditional)
        sanitized_prompt = self._sanitize_if_needed(prompt) if prompt else None
        sanitized_messages = self._sanitize_if_needed(messages) if messages else None

        # Check cache first (skip for multi-turn conversations — not cacheable)
        # The cache will be stored with the effective model used
        cache_model = model  # Use the requested model for cache lookup
        if cache_model and not messages and sanitized_prompt:
            cached_response = self.cache.check(
                sanitized_prompt, cache_model, case_id=case_id
            )
            if cached_response:
                self.logger.info("✅ Using cached response")
                cached_response.sanitized_prompt = sanitized_prompt
                if self.settings.observability.opik_log_raw_prompts:
                    cached_response.raw_prompt = prompt
                self._update_opik_span(
                    cached_response, sanitized_prompt=sanitized_prompt, cached=True
                )
                return cached_response

        # Route through registry with BaseExternalClient wrapping
        try:
            # Initialize registry and log provider info only once
            if not self._router_initialized:
                available = self.registry.get_available_providers()
                fallback_chain = self.registry.get_fallback_chain()
                self.logger.info(f"🔍 LLM Router: Available providers: {available}")
                self.logger.info(
                    f"🔍 LLM Router: Fallback chain: {' -> '.join(fallback_chain)}"
                )
                self._router_initialized = True

            response = await self.call_external(
                operation_name="route_llm_request",
                call_func=self.registry.route_request,
                prompt=sanitized_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                tool_choice=tool_choice,
                response_format=response_format,
                messages=sanitized_messages,
                confidence_threshold=self.confidence_threshold,
                timeout=self.request_timeout,  # Enforce router-level timeout ceiling
                retries=0,  # Do not duplicate retries at the router level
                retry_delay=1.0,
            )

            # Store successful response in cache
            if response.confidence >= self.confidence_threshold and sanitized_prompt:
                # Store with the requested model key for consistent cache lookup
                store_model = model or response.model
                self.cache.store(
                    sanitized_prompt, store_model, response, case_id=case_id
                )

            # Attach prompt data for telemetry
            response.sanitized_prompt = sanitized_prompt
            if self.settings.observability.opik_log_raw_prompts:
                response.raw_prompt = prompt
                response.raw_messages = messages

            response.sanitized_messages = sanitized_messages

            # Update the current Opik span with LLM-specific data
            self._update_opik_span(
                response,
                sanitized_prompt=sanitized_prompt,
                sanitized_messages=sanitized_messages,
            )

            return response

        except Exception as e:
            self.logger.error(
                f"❌ LLM Router: All providers failed: {type(e).__name__}: {e}",
                exc_info=True,
            )
            raise

    def _update_opik_span(
        self,
        response: LLMResponse,
        sanitized_prompt: str | None = None,
        sanitized_messages: list[dict[str, Any]] | None = None,
        cached: bool = False,
    ) -> None:
        """Update the current Opik span with LLM call data.

        Uses opik_context.update_current_span() to attach prompt, response,
        model, provider, and token usage to whichever span is currently active.
        This works whether the span was created by @opik.track on this function
        or by a parent caller.
        """
        if not OPIK_AVAILABLE:
            self.logger.warning("Opik SDK not available — skipping span update")
            return

        try:
            input_data = {}
            if sanitized_prompt:
                input_data["prompt"] = sanitized_prompt[:TELEMETRY_PAYLOAD_MAX_CHARS]
            if sanitized_messages:
                # Truncate messages if they become too large for telemetry stringification
                messages_str = str(sanitized_messages)
                input_data["messages"] = messages_str[:TELEMETRY_PAYLOAD_MAX_CHARS]
            output_data = {}
            if response.content:
                output_data["response"] = response.content[:TELEMETRY_PAYLOAD_MAX_CHARS]

            metadata = {
                "cached": cached or response.cached,
                "tokens_used": response.tokens_used,
                "response_time_ms": response.response_time_ms,
                "confidence": response.confidence,
            }

            usage = None
            if response.tokens_used:
                usage = {"total_tokens": response.tokens_used}

            opik_context.update_current_span(
                input=input_data,
                output=output_data,
                metadata=metadata,
                model=response.model,
                provider=response.provider,
                usage=usage,
            )
        except Exception as e:
            self.logger.warning(f"Failed to update Opik span: {e}")

    async def generate(self, prompt: str | None = None, **kwargs) -> LLMResponse:
        """
        ILLMProvider interface implementation - delegates to route()

        This method provides the standard ILLMProvider interface while leveraging
        all the existing functionality of the router including caching, sanitization,
        fallback strategies, and provider registry management.

        Args:
            prompt: Input prompt for text generation (optional if messages is provided in kwargs)
            **kwargs: Additional parameters including:
                - model: Specific model to use (optional)
                - max_tokens: Maximum tokens to generate (default: 1000)
                - temperature: Sampling temperature (default: 0.7)
                - data_type: Type of data being processed (optional)
                - response_format: Structured output format (optional)
                - tools: Tool/function definitions (optional)
                - tool_choice: Tool choice strategy (optional)

        Returns:
            LLMResponse with generated content

        Raises:
            TypeError: If prompt is None
            Exception: If all providers fail to generate a response
        """
        # Extract parameters from kwargs with defaults
        model = kwargs.get("model")
        max_tokens = kwargs.get("max_tokens", 1000)
        temperature = kwargs.get("temperature", 0.7)
        data_type = kwargs.get("data_type")
        tools = kwargs.get("tools")
        tool_choice = kwargs.get("tool_choice")
        response_format = kwargs.get("response_format")
        messages = kwargs.get("messages")
        case_id = kwargs.get("case_id")

        # Call existing route method with all the robust functionality
        response = await self.route(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            data_type=data_type,
            tools=tools,
            tool_choice=tool_choice,
            response_format=response_format,
            messages=messages,
            case_id=case_id,
        )

        # Return the full LLMResponse (milestone_engine expects this)
        return response

    def _sanitize_if_needed(self, data: Any) -> Any:
        """
        Conditionally sanitize data based on SANITIZE_PII setting.

        Returns:
            Sanitized or original data
        """
        if self.settings.protection.sanitize_pii:
            self.logger.debug("🔒 LLM Router: Applying PII sanitization")
            return self.sanitizer.sanitize(data)
        else:
            self.logger.debug("🔓 LLM Router: Skipping PII sanitization")
            return data

    def get_provider_status(self):
        """Get status of all providers"""
        return self.registry.get_provider_status()

    def supports_tool_calling(self, model: str | None = None) -> bool:
        """Check if the primary provider/model supports tool calling.

        Delegates to the primary provider in the fallback chain.
        """
        self.registry._ensure_initialized()

        fallback_chain = self.registry.get_fallback_chain()
        if not fallback_chain:
            return False

        primary_provider = self.registry._providers.get(fallback_chain[0])
        if not primary_provider:
            return False

        return primary_provider.supports_tool_calling(model)

    def get_structured_output_capability(self, model: str | None = None):
        """
        Get the structured output capability for the primary provider/model.

        Delegates to the primary provider in the fallback chain.

        Args:
            model: Model name to check (uses default if None)

        Returns:
            StructuredOutputCapability level
        """
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputCapability,
        )

        # Ensure registry is initialized
        self.registry._ensure_initialized()

        # Get the primary provider (first in fallback chain)
        fallback_chain = self.registry.get_fallback_chain()
        if not fallback_chain:
            self.logger.warning("No providers available for capability detection")
            return StructuredOutputCapability.BEST_EFFORT

        primary_provider_name = fallback_chain[0]
        primary_provider = self.registry._providers.get(primary_provider_name)

        if not primary_provider:
            self.logger.warning(f"Primary provider {primary_provider_name} not found")
            return StructuredOutputCapability.BEST_EFFORT

        # Delegate to the primary provider
        capability = primary_provider.get_structured_output_capability(model)

        self.logger.debug(
            f"Router delegating capability detection to {primary_provider_name}: "
            f"{capability.value}"
        )

        return capability

    def get_structured_output_strategy(
        self, schema: dict[str, Any], model: str | None = None
    ):
        """
        Get the appropriate structured output strategy for the primary provider/model.

        Delegates to the primary provider in the fallback chain.

        Args:
            schema: Pydantic JSON schema
            model: Model name (uses default if None)

        Returns:
            StructuredOutputStrategy with configuration
        """

        # Ensure registry is initialized
        self.registry._ensure_initialized()

        # Get the primary provider (first in fallback chain)
        fallback_chain = self.registry.get_fallback_chain()
        if not fallback_chain:
            self.logger.warning("No providers available for strategy creation")
            # Return a safe default strategy
            from faultmaven.infrastructure.llm.structured_output_capability import (
                StructuredOutputCapability,
                create_strategy_for_capability,
            )

            return create_strategy_for_capability(
                StructuredOutputCapability.BEST_EFFORT, schema
            )

        primary_provider_name = fallback_chain[0]
        primary_provider = self.registry._providers.get(primary_provider_name)

        if not primary_provider:
            self.logger.warning(f"Primary provider {primary_provider_name} not found")
            from faultmaven.infrastructure.llm.structured_output_capability import (
                StructuredOutputCapability,
                create_strategy_for_capability,
            )

            return create_strategy_for_capability(
                StructuredOutputCapability.BEST_EFFORT, schema
            )

        # Delegate to the primary provider
        strategy = primary_provider.get_structured_output_strategy(schema, model)

        self.logger.info(
            f"Router delegating strategy creation to {primary_provider_name}: "
            f"mode={strategy.mode.value}, include_schema_in_prompt={strategy.include_schema_in_prompt}"
        )

        return strategy
