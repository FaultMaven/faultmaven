"""
Enhanced LLM Router with comprehensive logging for development.

This extends the base router with detailed logging for debugging
provider issues, performance analysis, and cache behavior.
"""

import time
from typing import Optional

from ...models import DataType
from ..logging.config import get_logger
from ..logging.unified import UnifiedLogger
from .router import LLMRouter as BaseRouter
from .providers import LLMResponse

logger = get_logger(__name__)

class DevelopmentLLMRouter(BaseRouter):
    """Enhanced LLM Router with comprehensive development logging."""
    
    def __init__(self):
        super().__init__()
        self.unified_logger = UnifiedLogger(__name__, "infrastructure")
        
        # Log detailed initialization info
        logger.debug("Initializing Enhanced LLM Router for development")
        
        # Log provider configurations
        for provider_name in self.registry.get_available_providers():
            config = self.registry.get_provider_config(provider_name)
            logger.debug(
                f"Provider {provider_name} configured",
                extra={
                    'provider': provider_name,
                    'model': config.default_model,
                    'confidence': config.confidence_score,
                    'timeout': config.timeout,
                    'max_retries': config.max_retries
                }
            )
    
    async def route(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        data_type: Optional[DataType] = None,
    ) -> LLMResponse:
        """Enhanced route method with comprehensive logging."""
        
        with self.unified_logger.operation(
            "llm_request",
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            data_type=data_type.value if data_type else None,
            prompt_length=len(prompt) if prompt else 0
        ):
            # Log prompt analysis
            if logger.isEnabledFor(logger.DEBUG):
                logger.debug(
                    "Analyzing prompt",
                    extra={
                        'prompt_preview': prompt[:100] + "..." if len(prompt) > 100 else prompt,
                        'prompt_length': len(prompt),
                        'contains_sensitive_data': self._detect_sensitive_data(prompt)
                    }
                )
            
            # Check cache first with detailed logging
            cache_key = f"{prompt}_{model or 'default'}"
            cache_start = time.time()
            
            if model:
                cached_response = self.cache.check(prompt, model)
                cache_time = time.time() - cache_start
                
                if cached_response:
                    logger.info(
                        "Cache HIT - returning cached response",
                        extra={
                            'cache_lookup_time_ms': cache_time * 1000,
                            'cached_model': cached_response.model,
                            'cached_confidence': cached_response.confidence,
                            'cached_tokens': cached_response.tokens_used
                        }
                    )
                    return cached_response
                else:
                    logger.debug(
                        "Cache MISS - proceeding to LLM providers",
                        extra={'cache_lookup_time_ms': cache_time * 1000}
                    )
            
            # Execute the actual request with provider fallback logging
            return await self._route_with_fallback_logging(
                prompt, model, max_tokens, temperature, data_type
            )
    
    async def _route_with_fallback_logging(
        self,
        prompt: str,
        model: Optional[str],
        max_tokens: int,
        temperature: float,
        data_type: Optional[DataType]
    ) -> LLMResponse:
        """Route request with detailed fallback chain logging."""
        
        fallback_chain = self.registry.get_fallback_chain()
        logger.info(
            f"Starting LLM request with fallback chain: {' -> '.join(fallback_chain)}",
            extra={'fallback_chain': fallback_chain, 'chain_length': len(fallback_chain)}
        )
        
        for i, provider_name in enumerate(fallback_chain):
            is_last_provider = (i == len(fallback_chain) - 1)
            
            logger.info(
                f"Attempting provider {provider_name} ({i+1}/{len(fallback_chain)})",
                extra={
                    'provider': provider_name,
                    'attempt_number': i + 1,
                    'is_last_provider': is_last_provider
                }
            )
            
            try:
                # Time the provider request
                provider_start = time.time()
                
                # Get the provider and make request
                provider = self.registry.get_provider(provider_name)
                response = await provider.generate(
                    prompt=prompt,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature
                )
                
                provider_duration = time.time() - provider_start
                
                # Log successful provider response
                logger.info(
                    f"Provider {provider_name} SUCCESS",
                    extra={
                        'provider': provider_name,
                        'response_time_ms': provider_duration * 1000,
                        'tokens_used': response.tokens_used,
                        'confidence': response.confidence,
                        'model_used': response.model,
                        'response_length': len(response.content) if response.content else 0
                    }
                )
                
                # Cache the successful response
                if response.confidence >= self.confidence_threshold:
                    cache_model = model or response.model
                    self.cache.store(prompt, cache_model, response)
                    logger.debug(f"Cached response for model {cache_model}")
                
                return response
                
            except Exception as e:
                provider_duration = time.time() - provider_start
                
                logger.warning(
                    f"Provider {provider_name} FAILED",
                    extra={
                        'provider': provider_name,
                        'error': str(e),
                        'error_type': type(e).__name__,
                        'response_time_ms': provider_duration * 1000,
                        'is_last_provider': is_last_provider
                    },
                    exc_info=logger.isEnabledFor(logger.DEBUG)  # Full traceback only in DEBUG
                )
                
                if is_last_provider:
                    logger.error("All providers in fallback chain failed")
                    raise Exception(f"All providers failed. Last error: {str(e)}")
                
                # Continue to next provider
                continue
    
    def _detect_sensitive_data(self, text: str) -> bool:
        """Simple heuristic to detect potentially sensitive data in prompts."""
        if not text:
            return False
            
        # Basic patterns that might indicate sensitive data
        sensitive_patterns = [
            'password', 'secret', 'token', 'key', 'credential',
            '@', 'api_key', 'private', 'confidential'
        ]
        
        text_lower = text.lower()
        return any(pattern in text_lower for pattern in sensitive_patterns)
    
    def get_cache_stats(self) -> dict:
        """Get detailed cache statistics for development monitoring."""
        stats = {
            'cache_size': len(self.cache.cache),
            'max_cache_size': self.cache.max_size,
            'cache_utilization': len(self.cache.cache) / self.cache.max_size if self.cache.max_size > 0 else 0
        }
        
        logger.debug("Cache statistics", extra=stats)
        return stats
    
    def get_provider_health(self) -> dict:
        """Get provider health status for development monitoring."""
        health = {}
        
        for provider_name in self.registry.get_available_providers():
            try:
                provider = self.registry.get_provider(provider_name)
                config = self.registry.get_provider_config(provider_name)
                
                health[provider_name] = {
                    'available': True,
                    'confidence_score': config.confidence_score,
                    'timeout': config.timeout,
                    'max_retries': config.max_retries
                }
            except Exception as e:
                health[provider_name] = {
                    'available': False,
                    'error': str(e)
                }
        
        logger.debug("Provider health check", extra={'provider_health': health})
        return health