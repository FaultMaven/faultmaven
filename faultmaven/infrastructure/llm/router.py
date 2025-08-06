"""
New LLM Router using Centralized Provider Registry.

This router replaces the old scattered configuration approach with a clean,
centralized provider registry system that handles provider management,
fallback strategies, and configuration in a unified way.
"""

import logging
from typing import Optional

from faultmaven.models import DataType
from faultmaven.models.interfaces import ILLMProvider
from faultmaven.infrastructure.observability.tracing import trace
from faultmaven.infrastructure.security.redaction import DataSanitizer
from .providers import LLMResponse, get_registry
from .cache import SemanticCache


class LLMRouter(ILLMProvider):
    """Simplified LLM router using centralized provider registry"""
    
    def __init__(self, confidence_threshold: float = 0.8):
        self.logger = logging.getLogger(__name__)
        self.sanitizer = DataSanitizer()
        self.cache = SemanticCache()
        self.confidence_threshold = confidence_threshold
        self.registry = get_registry()
        
        # Log available providers
        available = self.registry.get_available_providers()
        fallback_chain = self.registry.get_fallback_chain()
        
        self.logger.info(f"Available providers: {available}")
        self.logger.info(f"Fallback chain: {' -> '.join(fallback_chain)}")
        
        if not available:
            self.logger.warning("⚠️ No LLM providers available!")
        else:
            self.logger.info("✅ LLMRouter initialized with centralized registry")
    
    @trace("llm_router_route")
    async def route(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        data_type: Optional[DataType] = None,
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
        
        # Sanitize prompt before sending to external providers
        sanitized_prompt = self.sanitizer.sanitize(prompt)
        
        # Check cache first - always check with the original model parameter
        # The cache will be stored with the effective model used
        cache_model = model  # Use the requested model for cache lookup
        if cache_model:
            cached_response = self.cache.check(sanitized_prompt, cache_model)
            if cached_response:
                self.logger.info("✅ Using cached response")
                return cached_response
        
        # Route through registry
        try:
            response = await self.registry.route_request(
                prompt=sanitized_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                confidence_threshold=self.confidence_threshold,
            )
            
            # Store successful response in cache
            if response.confidence >= self.confidence_threshold:
                # Store with the requested model key for consistent cache lookup
                store_model = model or response.model
                self.cache.store(sanitized_prompt, store_model, response)
            
            return response
            
        except Exception as e:
            self.logger.error(f"❌ All providers failed: {e}")
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
    
    def get_provider_status(self):
        """Get status of all providers"""
        return self.registry.get_provider_status()