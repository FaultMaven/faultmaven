"""
New LLM Router using Centralized Provider Registry.

This router replaces the old scattered configuration approach with a clean,
centralized provider registry system that handles provider management,
fallback strategies, and configuration in a unified way.

Inherits from BaseExternalClient for unified logging, retry logic, and
circuit breaker patterns for external LLM provider calls.
"""

import logging
import os
from typing import Any, Dict, List, Optional

from faultmaven.config.settings import get_settings
from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.models import DataType
from faultmaven.models.interfaces import ILLMProvider

from .cache import SemanticCache
from .providers import LLMResponse, get_registry


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
        self.registry = get_registry()
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

    @trace("llm_router_route")
    async def route(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        data_type: Optional[DataType] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """
        Route request through the centralized provider registry

        Args:
            prompt: Input prompt
            model: Specific model to use (optional)
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature
            data_type: Type of data being processed

        Returns:
            LLMResponse with generated content
        """
        # Validate prompt
        if prompt is None:
            raise TypeError("Prompt cannot be None")

        # Sanitize prompt before sending to external providers (conditional)
        sanitized_prompt = self._sanitize_if_needed(prompt)

        # Check cache first (skip for multi-turn conversations — not cacheable)
        # The cache will be stored with the effective model used
        cache_model = model  # Use the requested model for cache lookup
        if cache_model and not messages:
            cached_response = self.cache.check(sanitized_prompt, cache_model)
            if cached_response:
                self.logger.info("✅ Using cached response")
                # Attach raw prompt for telemetry if enabled
                if self.settings.observability.opik_log_raw_prompts:
                    setattr(cached_response, "raw_prompt", prompt)
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
                messages=messages,
                confidence_threshold=self.confidence_threshold,
                timeout=self.request_timeout,  # Configurable timeout from environment/settings
                retries=1,  # Single retry for failed LLM calls
                retry_delay=2.0,
            )

            # Store successful response in cache
            if response.confidence >= self.confidence_threshold:
                # Store with the requested model key for consistent cache lookup
                store_model = model or response.model
                self.cache.store(sanitized_prompt, store_model, response)

            # Attach raw prompt for telemetry if enabled
            if self.settings.observability.opik_log_raw_prompts:
                setattr(response, "raw_prompt", prompt)

            return response

        except Exception as e:
            self.logger.error(
                f"❌ LLM Router: All providers failed: {type(e).__name__}: {e}",
                exc_info=True,
            )
            raise

    @trace("llm_router_generate")
    async def generate(self, prompt: str, **kwargs) -> LLMResponse:
        """
        ILLMProvider interface implementation - delegates to route()

        This method provides the standard ILLMProvider interface while leveraging
        all the existing functionality of the router including caching, sanitization,
        fallback strategies, and provider registry management.

        Args:
            prompt: Input prompt for text generation
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
        )

        # Return the full LLMResponse (milestone_engine expects this)
        return response

    def _sanitize_if_needed(self, prompt: str) -> str:
        """
        Conditionally sanitize prompt based on SANITIZE_PII setting.

        Returns:
            Sanitized or original prompt
        """
        if self.settings.protection.sanitize_pii:
            self.logger.debug("🔒 LLM Router: Applying PII sanitization")
            return self.sanitizer.sanitize(prompt)
        else:
            self.logger.debug("🔓 LLM Router: Skipping PII sanitization")
            return prompt

    def get_provider_status(self):
        """Get status of all providers"""
        return self.registry.get_provider_status()

    def supports_tool_calling(self, model: Optional[str] = None) -> bool:
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

    def get_structured_output_capability(self, model: Optional[str] = None):
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
        self, schema: Dict[str, Any], model: Optional[str] = None
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
        from faultmaven.infrastructure.llm.structured_output_capability import (
            StructuredOutputStrategy,
        )

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
