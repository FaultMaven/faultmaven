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

from faultmaven.models import DataType
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.security.redaction import DataSanitizer
from faultmaven.config.settings import get_settings
from .providers import LLMResponse, get_registry
from .cache import SemanticCache


class LLMRouter(BaseExternalClient, ILLMProvider):
    """Simplified LLM router using centralized provider registry"""
    
    def __init__(self, confidence_threshold: float = 0.8):
        # Initialize BaseExternalClient
        super().__init__(
            client_name="llm_router",
            service_name="LLM_Providers",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,  # Lower threshold for LLM failures
            circuit_breaker_timeout=30    # Shorter timeout for LLM recovery
        )

        self.sanitizer = DataSanitizer()
        self.cache = SemanticCache()
        self.confidence_threshold = confidence_threshold
        self.registry = get_registry()

        # Get timeout from settings with environment variable override
        self.settings = get_settings()
        self.request_timeout = float(os.getenv("LLM_REQUEST_TIMEOUT", str(self.settings.llm.request_timeout)))

        # Don't initialize registry immediately - wait for first use
        self.logger.info(f"🔍 LLMRouter created, request timeout: {self.request_timeout}s")
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
        
        # Check cache first - always check with the original model parameter
        # The cache will be stored with the effective model used
        cache_model = model  # Use the requested model for cache lookup
        if cache_model:
            cached_response = self.cache.check(sanitized_prompt, cache_model)
            if cached_response:
                self.logger.info("✅ Using cached response")
                return cached_response
        
        # Route through registry with BaseExternalClient wrapping
        try:
            # Log provider information on first use
            available = self.registry.get_available_providers()
            fallback_chain = self.registry.get_fallback_chain()
            self.logger.info(f"🔍 LLM Router: Available providers: {available}")
            self.logger.info(f"🔍 LLM Router: Fallback chain: {' -> '.join(fallback_chain)}")
            
            self.logger.info(f"🔍 LLM Router: About to call registry.route_request")
            self.logger.info(f"🔍 LLM Router: Registry type: {type(self.registry)}")
            self.logger.info(f"🔍 LLM Router: Registry available: {self.registry is not None}")
            
            response = await self.call_external(
                operation_name="route_llm_request",
                call_func=self.registry.route_request,
                prompt=sanitized_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                tools=tools,
                tool_choice=tool_choice,
                confidence_threshold=self.confidence_threshold,
                timeout=self.request_timeout,  # Configurable timeout from environment/settings
                retries=1,     # Single retry for failed LLM calls
                retry_delay=2.0
            )
            
            self.logger.info(f"✅ LLM Router: Registry call successful, response type: {type(response)}")
            
            # Store successful response in cache
            if response.confidence >= self.confidence_threshold:
                # Store with the requested model key for consistent cache lookup
                store_model = model or response.model
                self.cache.store(sanitized_prompt, store_model, response)
            
            return response
            
        except Exception as e:
            self.logger.error(f"❌ LLM Router: All providers failed: {e}")
            self.logger.error(f"❌ LLM Router: Exception type: {type(e)}")
            import traceback
            self.logger.error(f"❌ LLM Router: Full traceback: {traceback.format_exc()}")
            raise
    
    @trace("llm_router_generate")
    async def generate(self, prompt: str, **kwargs) -> str:
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
                
        Returns:
            Generated text content as string
            
        Raises:
            TypeError: If prompt is None
            Exception: If all providers fail to generate a response
        """
        # Extract parameters from kwargs with defaults
        model = kwargs.get('model')
        max_tokens = kwargs.get('max_tokens', 1000)
        temperature = kwargs.get('temperature', 0.7)
        data_type = kwargs.get('data_type')
        
        # Call existing route method with all the robust functionality
        response = await self.route(
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            data_type=data_type
        )
        
        # Extract and return the text content from LLMResponse
        return response.content
    
    def _sanitize_if_needed(self, prompt: str) -> str:
        """
        Conditionally sanitize prompt based on settings and provider type

        Returns:
            Sanitized or original prompt
        """
        # Check if auto-detect is enabled
        if self.settings.protection.auto_sanitize_based_on_provider:
            # Auto mode: Sanitize only for external providers (not LOCAL)
            from faultmaven.config.settings import LLMProvider
            is_local = self.settings.llm.provider == LLMProvider.LOCAL

            if is_local:
                self.logger.debug("🔓 LLM Router: Skipping PII sanitization (LOCAL provider)")
                return prompt
            else:
                self.logger.debug(f"🔒 LLM Router: Applying PII sanitization (provider: {self.settings.llm.provider})")
                return self.sanitizer.sanitize(prompt)
        else:
            # Manual mode: Use explicit setting
            if self.settings.protection.sanitize_pii:
                self.logger.debug("🔒 LLM Router: Applying PII sanitization (explicit config)")
                return self.sanitizer.sanitize(prompt)
            else:
                self.logger.debug("🔓 LLM Router: Skipping PII sanitization (explicit config)")
                return prompt

    def get_provider_status(self):
        """Get status of all providers"""
        return self.registry.get_provider_status()