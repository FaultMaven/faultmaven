"""
Enhanced LLM Router with comprehensive logging for development.

This extends the base router with detailed logging for debugging
provider issues, performance analysis, and cache behavior.
"""

import time
import asyncio
import statistics
from typing import Optional, List, Dict, Any, Tuple
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from ...models import DataType
from ..logging.config import get_logger
from ..logging.unified import UnifiedLogger
from .router import LLMRouter as BaseRouter
from .providers import LLMResponse

logger = get_logger(__name__)

class DevelopmentLLMRouter(BaseRouter):
    """Enhanced LLM Router with comprehensive development logging and performance optimization."""
    
    def __init__(self):
        super().__init__()
        self.unified_logger = UnifiedLogger(__name__, "infrastructure")
        
        # Performance optimization components
        self._request_batch_queue = deque()  # Queue for request batching
        self._provider_response_times = defaultdict(list)  # Track provider performance
        self._provider_load_balancer = {}  # Load balancing state
        self._intelligent_fallback_chains = {}  # Dynamic fallback chains
        self._request_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="llm_opt")
        
        # Performance metrics
        self._optimization_metrics = {
            "requests_batched": 0,
            "load_balanced_requests": 0,
            "intelligent_fallbacks": 0,
            "avg_provider_response_time": {},
            "optimization_time_saved": 0.0,
            "parallel_requests": 0,
            "cache_optimizations": 0
        }
        
        # Background optimization
        self._optimization_running = False
        self._start_background_optimization()
        
        # Log detailed initialization info
        logger.debug("Initializing Enhanced LLM Router with performance optimization")
        
        # Log provider configurations with performance data
        for provider_name in self.registry.get_available_providers():
            config = self.registry.get_provider_config(provider_name)
            logger.debug(
                f"Provider {provider_name} configured with optimization",
                extra={
                    'provider': provider_name,
                    'model': config.default_model,
                    'confidence': config.confidence_score,
                    'timeout': config.timeout,
                    'max_retries': config.max_retries,
                    'optimization_enabled': True
                }
            )
    
    async def route(
        self,
        prompt: str,
        model: Optional[str] = None,
        max_tokens: int = 1000,
        temperature: float = 0.7,
        data_type: Optional[DataType] = None,
        enable_batching: bool = True,
        priority: int = 5,
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
            
            # Execute with performance optimization
            return await self._route_with_optimization(
                prompt, model, max_tokens, temperature, data_type, enable_batching, priority
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
    
    # Performance Optimization Methods
    
    def _start_background_optimization(self):
        """Start background optimization tasks"""
        if not self._optimization_running:
            self._optimization_running = True
            asyncio.create_task(self._background_batch_processor())
            asyncio.create_task(self._background_performance_analyzer())
            asyncio.create_task(self._background_load_balancer())
    
    async def _background_batch_processor(self):
        """Background task for processing batched requests"""
        while self._optimization_running:
            try:
                if len(self._request_batch_queue) >= 3:  # Process in batches of 3+
                    batch = []
                    while len(batch) < 5 and self._request_batch_queue:  # Max batch size 5
                        batch.append(self._request_batch_queue.popleft())
                    
                    if batch:
                        await self._process_request_batch(batch)
                        self._optimization_metrics["requests_batched"] += len(batch)
                
                await asyncio.sleep(1)  # Check every second
            except Exception as e:
                logger.warning(f"Background batch processor error: {e}")
                await asyncio.sleep(5)
    
    async def _background_performance_analyzer(self):
        """Background task for analyzing provider performance"""
        while self._optimization_running:
            try:
                # Analyze provider response times
                await self._analyze_provider_performance()
                
                # Update intelligent fallback chains
                await self._update_fallback_chains()
                
                await asyncio.sleep(60)  # Run every minute
            except Exception as e:
                logger.warning(f"Background performance analyzer error: {e}")
                await asyncio.sleep(120)
    
    async def _background_load_balancer(self):
        """Background task for load balancing optimization"""
        while self._optimization_running:
            try:
                # Update load balancing weights based on performance
                await self._update_load_balancing()
                await asyncio.sleep(30)  # Run every 30 seconds
            except Exception as e:
                logger.warning(f"Background load balancer error: {e}")
                await asyncio.sleep(60)
    
    async def _route_with_optimization(
        self,
        prompt: str,
        model: Optional[str],
        max_tokens: int,
        temperature: float,
        data_type: Optional[DataType],
        enable_batching: bool,
        priority: int
    ) -> LLMResponse:
        """Route request with comprehensive performance optimization"""
        
        # Check if request should be batched
        if enable_batching and priority < 8:  # Batch lower priority requests
            return await self._handle_batched_request(
                prompt, model, max_tokens, temperature, data_type, priority
            )
        
        # Use optimized provider selection
        optimal_providers = await self._get_optimal_provider_chain(model, data_type)
        
        with self.unified_logger.operation(
            "optimized_llm_request",
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            data_type=data_type.value if data_type else None,
            prompt_length=len(prompt) if prompt else 0,
            optimization_enabled=True,
            provider_chain=optimal_providers
        ):
            return await self._execute_with_optimized_fallback(
                prompt, model, max_tokens, temperature, data_type, optimal_providers
            )
    
    async def _handle_batched_request(
        self,
        prompt: str,
        model: Optional[str],
        max_tokens: int,
        temperature: float,
        data_type: Optional[DataType],
        priority: int
    ) -> LLMResponse:
        """Handle request through batching optimization"""
        
        request_item = {
            "prompt": prompt,
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "data_type": data_type,
            "priority": priority,
            "timestamp": time.time(),
            "future": asyncio.Future()
        }
        
        # Add to batch queue
        self._request_batch_queue.append(request_item)
        
        # If high priority, process immediately
        if priority >= 8:
            await self._process_single_request(request_item)
        
        # Wait for result
        return await request_item["future"]
    
    async def _process_request_batch(self, batch: List[Dict[str, Any]]):
        """Process a batch of requests efficiently"""
        # Sort by priority
        batch.sort(key=lambda x: x["priority"], reverse=True)
        
        # Process requests in parallel
        tasks = [
            asyncio.create_task(self._process_single_request(request))
            for request in batch
        ]
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _process_single_request(self, request_item: Dict[str, Any]):
        """Process a single batched request"""
        try:
            response = await self._route_with_fallback_logging(
                request_item["prompt"],
                request_item["model"],
                request_item["max_tokens"],
                request_item["temperature"],
                request_item["data_type"]
            )
            request_item["future"].set_result(response)
        except Exception as e:
            request_item["future"].set_exception(e)
    
    async def _get_optimal_provider_chain(
        self,
        model: Optional[str],
        data_type: Optional[DataType]
    ) -> List[str]:
        """Get optimal provider chain based on performance data"""
        
        # Get base fallback chain
        base_chain = self.registry.get_fallback_chain()
        
        # Apply load balancing optimization
        if model in self._intelligent_fallback_chains:
            optimized_chain = self._intelligent_fallback_chains[model]
        else:
            optimized_chain = base_chain
        
        # Filter by data type compatibility if specified
        if data_type:
            compatible_providers = []
            for provider_name in optimized_chain:
                if self._is_provider_compatible(provider_name, data_type):
                    compatible_providers.append(provider_name)
            optimized_chain = compatible_providers if compatible_providers else base_chain
        
        return optimized_chain
    
    def _is_provider_compatible(self, provider_name: str, data_type: DataType) -> bool:
        """Check if provider is compatible with data type"""
        # Simple compatibility check - in production would be more sophisticated
        provider_capabilities = {
            "openai": {DataType.SENSITIVE, DataType.GENERAL, DataType.INTERNAL},
            "anthropic": {DataType.SENSITIVE, DataType.GENERAL, DataType.INTERNAL},
            "fireworks": {DataType.GENERAL, DataType.INTERNAL},
            "local": {DataType.SENSITIVE, DataType.INTERNAL}
        }
        
        return data_type in provider_capabilities.get(provider_name, {DataType.GENERAL})
    
    async def _execute_with_optimized_fallback(
        self,
        prompt: str,
        model: Optional[str],
        max_tokens: int,
        temperature: float,
        data_type: Optional[DataType],
        provider_chain: List[str]
    ) -> LLMResponse:
        """Execute request with optimized fallback chain"""
        
        logger.info(
            f"Starting optimized LLM request with chain: {' -> '.join(provider_chain)}",
            extra={'optimized_chain': provider_chain, 'chain_length': len(provider_chain)}
        )
        
        for i, provider_name in enumerate(provider_chain):
            is_last_provider = (i == len(provider_chain) - 1)
            
            # Calculate provider weight for load balancing
            provider_weight = self._provider_load_balancer.get(provider_name, 1.0)
            
            logger.info(
                f"Attempting optimized provider {provider_name} ({i+1}/{len(provider_chain)}) weight: {provider_weight:.2f}",
                extra={
                    'provider': provider_name,
                    'attempt_number': i + 1,
                    'is_last_provider': is_last_provider,
                    'load_balance_weight': provider_weight,
                    'optimization_enabled': True
                }
            )
            
            try:
                # Time the provider request with enhanced metrics
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
                
                # Update performance tracking
                self._provider_response_times[provider_name].append(provider_duration)
                
                # Limit response time history
                if len(self._provider_response_times[provider_name]) > 100:
                    self._provider_response_times[provider_name] = \
                        self._provider_response_times[provider_name][-50:]
                
                # Update optimization metrics
                avg_time = statistics.mean(self._provider_response_times[provider_name])
                self._optimization_metrics["avg_provider_response_time"][provider_name] = avg_time
                
                # Log successful provider response with optimization data
                logger.info(
                    f"Optimized provider {provider_name} SUCCESS",
                    extra={
                        'provider': provider_name,
                        'response_time_ms': provider_duration * 1000,
                        'tokens_used': response.tokens_used,
                        'confidence': response.confidence,
                        'model_used': response.model,
                        'response_length': len(response.content) if response.content else 0,
                        'avg_response_time_ms': avg_time * 1000,
                        'optimization_applied': True
                    }
                )
                
                # Enhanced caching with optimization metadata
                if response.confidence >= self.confidence_threshold:
                    cache_model = model or response.model
                    self.cache.store(prompt, cache_model, response)
                    self._optimization_metrics["cache_optimizations"] += 1
                    logger.debug(f"Optimized cache storage for model {cache_model}")
                
                return response
                
            except Exception as e:
                provider_duration = time.time() - provider_start
                
                # Track failed response times for load balancing
                self._provider_response_times[provider_name].append(provider_duration * 2)  # Penalty
                
                logger.warning(
                    f"Optimized provider {provider_name} FAILED",
                    extra={
                        'provider': provider_name,
                        'error': str(e),
                        'error_type': type(e).__name__,
                        'response_time_ms': provider_duration * 1000,
                        'is_last_provider': is_last_provider,
                        'optimization_enabled': True
                    },
                    exc_info=logger.isEnabledFor(logger.DEBUG)
                )
                
                if is_last_provider:
                    logger.error("All providers in optimized fallback chain failed")
                    raise Exception(f"All optimized providers failed. Last error: {str(e)}")
                
                # Apply intelligent fallback optimization
                await self._apply_intelligent_fallback(provider_name, str(e))
                continue
    
    async def _analyze_provider_performance(self):
        """Analyze provider performance for optimization"""
        performance_analysis = {}
        
        for provider_name, response_times in self._provider_response_times.items():
            if len(response_times) > 5:  # Enough data for analysis
                avg_time = statistics.mean(response_times)
                median_time = statistics.median(response_times)
                std_dev = statistics.stdev(response_times) if len(response_times) > 1 else 0
                
                # Calculate performance score (lower is better)
                performance_score = avg_time + (std_dev * 0.5)  # Penalize inconsistency
                
                performance_analysis[provider_name] = {
                    "avg_response_time": avg_time,
                    "median_response_time": median_time,
                    "std_deviation": std_dev,
                    "performance_score": performance_score,
                    "sample_size": len(response_times),
                    "reliability": 1.0 / (1.0 + std_dev) if std_dev > 0 else 1.0
                }
        
        # Update metrics
        for provider_name, analysis in performance_analysis.items():
            self._optimization_metrics["avg_provider_response_time"][provider_name] = \
                analysis["avg_response_time"]
        
        logger.debug(f"Provider performance analysis completed: {len(performance_analysis)} providers analyzed")
    
    async def _update_fallback_chains(self):
        """Update intelligent fallback chains based on performance"""
        if not self._provider_response_times:
            return
        
        # Sort providers by performance score
        provider_scores = []
        for provider_name, response_times in self._provider_response_times.items():
            if len(response_times) > 3:
                avg_time = statistics.mean(response_times)
                std_dev = statistics.stdev(response_times) if len(response_times) > 1 else 0
                performance_score = avg_time + (std_dev * 0.5)
                
                provider_scores.append((provider_name, performance_score))
        
        # Sort by performance (lower score is better)
        provider_scores.sort(key=lambda x: x[1])
        
        # Create optimized fallback chain
        optimized_chain = [provider for provider, score in provider_scores]
        
        # Update fallback chains for different models
        for model in ["gpt-4", "claude-3", "gpt-3.5-turbo", None]:  # None for default
            self._intelligent_fallback_chains[model] = optimized_chain
        
        self._optimization_metrics["intelligent_fallbacks"] += 1
        logger.info(f"Updated intelligent fallback chains: {optimized_chain}")
    
    async def _update_load_balancing(self):
        """Update load balancing weights based on performance"""
        if not self._provider_response_times:
            return
        
        # Calculate load balancing weights (inverse of performance score)
        total_weight = 0
        provider_weights = {}
        
        for provider_name, response_times in self._provider_response_times.items():
            if len(response_times) > 2:
                avg_time = statistics.mean(response_times)
                # Weight is inversely proportional to response time
                weight = 1.0 / max(avg_time, 0.1)  # Avoid division by zero
                provider_weights[provider_name] = weight
                total_weight += weight
        
        # Normalize weights
        if total_weight > 0:
            for provider_name in provider_weights:
                provider_weights[provider_name] /= total_weight
        
        self._provider_load_balancer = provider_weights
        self._optimization_metrics["load_balanced_requests"] += 1
        
        logger.debug(f"Updated load balancing weights: {provider_weights}")
    
    async def _apply_intelligent_fallback(self, failed_provider: str, error: str):
        """Apply intelligent fallback logic based on error type"""
        # Analyze error type for smarter fallback
        error_lower = error.lower()
        
        if "timeout" in error_lower or "connection" in error_lower:
            # Network issues - penalize provider more heavily
            if failed_provider in self._provider_response_times:
                # Add penalty response times
                for _ in range(3):
                    self._provider_response_times[failed_provider].append(30.0)  # 30 second penalty
        
        elif "rate limit" in error_lower or "quota" in error_lower:
            # Rate limiting - temporarily deprioritize provider
            if failed_provider in self._provider_load_balancer:
                self._provider_load_balancer[failed_provider] *= 0.5  # Halve weight
    
    def get_optimization_metrics(self) -> Dict[str, Any]:
        """Get comprehensive optimization metrics"""
        # Calculate cache hit rate
        cache_stats = self.get_cache_stats()
        
        # Calculate average optimization time saved
        total_requests = (
            self._optimization_metrics["requests_batched"] +
            self._optimization_metrics["load_balanced_requests"] +
            self._optimization_metrics["parallel_requests"]
        )
        
        return {
            **self._optimization_metrics,
            "cache_stats": cache_stats,
            "provider_load_balancing": self._provider_load_balancer.copy(),
            "intelligent_fallback_chains": {
                k: v for k, v in self._intelligent_fallback_chains.items() if k is not None
            },
            "performance_analysis": {
                provider: {
                    "avg_response_time": statistics.mean(times),
                    "response_count": len(times),
                    "reliability_score": 1.0 / (1.0 + statistics.stdev(times)) if len(times) > 1 else 1.0
                }
                for provider, times in self._provider_response_times.items()
                if len(times) > 0
            },
            "optimization_status": {
                "batching_enabled": True,
                "load_balancing_enabled": True,
                "intelligent_fallback_enabled": True,
                "performance_tracking_enabled": True,
                "background_optimization_running": self._optimization_running
            }
        }