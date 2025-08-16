"""System-Wide Performance Optimization Middleware

This middleware provides comprehensive system-wide performance enhancements:
- Response compression and streaming optimization
- API endpoint caching with intelligent invalidation
- Background task optimization and batching
- Resource cleanup and garbage collection optimization
- Request/response optimization with adaptive strategies
"""

import logging
import asyncio
import time
import gzip
import json
import gc
from typing import Callable, Dict, Any, Optional, List
from datetime import datetime, timedelta
from collections import defaultdict, deque

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import StreamingResponse
from starlette.background import BackgroundTasks


logger = logging.getLogger(__name__)


class SystemOptimizationMiddleware(BaseHTTPMiddleware):
    """Comprehensive system-wide performance optimization middleware
    
    Features:
    - Intelligent response compression based on content type and size
    - API endpoint caching with TTL and intelligent invalidation
    - Background task optimization with batching and prioritization
    - Resource cleanup optimization with adaptive garbage collection
    - Request/response streaming for large payloads
    - Performance monitoring and adaptive tuning
    """
    
    def __init__(
        self,
        app,
        enable_compression: bool = True,
        enable_caching: bool = True,
        enable_background_optimization: bool = True,
        enable_resource_cleanup: bool = True,
        cache_ttl_seconds: int = 300,
        compression_threshold: int = 1024,
        gc_threshold_factor: float = 2.0
    ):
        """Initialize system optimization middleware
        
        Args:
            app: FastAPI application instance
            enable_compression: Enable intelligent response compression
            enable_caching: Enable API endpoint caching
            enable_background_optimization: Enable background task optimization
            enable_resource_cleanup: Enable resource cleanup optimization
            cache_ttl_seconds: Default cache TTL in seconds
            compression_threshold: Minimum response size for compression
            gc_threshold_factor: Factor for adaptive garbage collection
        """
        super().__init__(app)
        
        # Configuration
        self.enable_compression = enable_compression
        self.enable_caching = enable_caching
        self.enable_background_optimization = enable_background_optimization
        self.enable_resource_cleanup = enable_resource_cleanup
        self.cache_ttl_seconds = cache_ttl_seconds
        self.compression_threshold = compression_threshold
        self.gc_threshold_factor = gc_threshold_factor
        
        # Response caching
        self._response_cache = {}
        self._cache_access_times = {}
        self._cache_hit_counts = defaultdict(int)
        
        # Background task optimization
        self._background_task_queue = deque()
        self._task_batch_size = 5
        self._background_executor = None
        
        # Resource cleanup tracking
        self._request_count = 0
        self._last_gc_time = time.time()
        self._memory_usage_history = deque(maxlen=100)
        
        # Performance metrics
        self._optimization_metrics = {
            "requests_processed": 0,
            "responses_compressed": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "background_tasks_optimized": 0,
            "gc_optimizations": 0,
            "avg_response_time": 0.0,
            "compression_ratio": 0.0,
            "cache_hit_rate": 0.0,
            "memory_optimizations": 0
        }
        
        # Start background optimization if enabled
        if self.enable_background_optimization:
            self._start_background_optimization()
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Main middleware dispatch with comprehensive optimization"""
        start_time = time.time()
        
        # Increment request counter for resource cleanup optimization
        self._request_count += 1
        
        try:
            # Check cache first if enabled
            if self.enable_caching:
                cached_response = await self._check_cache(request)
                if cached_response:
                    self._optimization_metrics["cache_hits"] += 1
                    self._update_cache_access(request)
                    return cached_response
                else:
                    self._optimization_metrics["cache_misses"] += 1
            
            # Process request with optimization
            response = await self._process_request_optimized(request, call_next)
            
            # Apply response optimizations
            optimized_response = await self._optimize_response(request, response)
            
            # Cache response if applicable
            if self.enable_caching:
                await self._cache_response(request, optimized_response)
            
            # Trigger resource cleanup if needed
            if self.enable_resource_cleanup:
                await self._optimize_resource_cleanup()
            
            # Update performance metrics
            processing_time = (time.time() - start_time) * 1000
            self._update_performance_metrics(processing_time)
            
            return optimized_response
            
        except Exception as e:
            logger.error(f"System optimization middleware error: {e}")
            # Fallback to normal processing
            return await call_next(request)
    
    async def _process_request_optimized(self, request: Request, call_next: Callable) -> Response:
        """Process request with optimization strategies"""
        # Add background tasks for non-critical operations
        background_tasks = BackgroundTasks()
        
        # Store background tasks reference for optimization
        if hasattr(request.state, 'background_tasks'):
            original_tasks = request.state.background_tasks
        else:
            original_tasks = None
        
        request.state.background_tasks = background_tasks
        
        # Process the request
        response = await call_next(request)
        
        # Optimize background tasks if enabled
        if self.enable_background_optimization and background_tasks.tasks:
            await self._optimize_background_tasks(background_tasks)
        
        # Restore original background tasks
        if original_tasks:
            request.state.background_tasks = original_tasks
        
        return response
    
    async def _optimize_response(self, request: Request, response: Response) -> Response:
        """Apply comprehensive response optimizations"""
        # Skip optimization for certain response types
        if not hasattr(response, 'body') or response.status_code >= 400:
            return response
        
        # Apply compression optimization
        if self.enable_compression:
            response = await self._apply_intelligent_compression(request, response)
        
        # Apply streaming optimization for large responses
        response = await self._apply_streaming_optimization(request, response)
        
        # Add optimization headers
        response.headers["X-Optimization-Applied"] = "true"
        response.headers["X-Optimization-Timestamp"] = str(int(time.time()))
        
        return response
    
    async def _check_cache(self, request: Request) -> Optional[Response]:
        """Check if response is cached with intelligent cache management"""
        cache_key = self._generate_cache_key(request)
        
        if cache_key in self._response_cache:
            cached_data, cached_time, ttl = self._response_cache[cache_key]
            
            # Check if cache is still valid
            if time.time() - cached_time < ttl:
                # Update access tracking
                self._cache_access_times[cache_key] = time.time()
                self._cache_hit_counts[cache_key] += 1
                
                # Reconstruct response from cached data
                return Response(
                    content=cached_data["content"],
                    status_code=cached_data["status_code"],
                    headers=dict(cached_data["headers"]),
                    media_type=cached_data.get("media_type")
                )
            else:
                # Remove expired cache entry
                del self._response_cache[cache_key]
                if cache_key in self._cache_access_times:
                    del self._cache_access_times[cache_key]
        
        return None
    
    async def _cache_response(self, request: Request, response: Response):
        """Cache response with intelligent TTL and size management"""
        # Skip caching for certain conditions
        if (response.status_code >= 400 or 
            not hasattr(response, 'body') or
            len(self._response_cache) >= 1000):  # Cache size limit
            return
        
        cache_key = self._generate_cache_key(request)
        
        # Determine TTL based on endpoint characteristics
        ttl = self._calculate_cache_ttl(request, response)
        
        # Cache response data
        cached_data = {
            "content": response.body,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "media_type": response.media_type
        }
        
        self._response_cache[cache_key] = (cached_data, time.time(), ttl)
        
        # Manage cache size with LRU eviction
        await self._manage_cache_size()
    
    async def _apply_intelligent_compression(self, request: Request, response: Response) -> Response:
        """Apply intelligent compression based on content analysis"""
        if not hasattr(response, 'body') or len(response.body) < self.compression_threshold:
            return response
        
        # Check if client accepts compression
        accept_encoding = request.headers.get("accept-encoding", "")
        if "gzip" not in accept_encoding.lower():
            return response
        
        # Skip compression for already compressed content
        content_type = response.headers.get("content-type", "")
        if any(ct in content_type.lower() for ct in ["image/", "video/", "audio/", "application/zip"]):
            return response
        
        # Check if already compressed
        if response.headers.get("content-encoding"):
            return response
        
        try:
            # Compress response body
            original_size = len(response.body)
            compressed_body = gzip.compress(response.body)
            compressed_size = len(compressed_body)
            
            # Only apply compression if it provides significant reduction
            compression_ratio = compressed_size / original_size
            if compression_ratio < 0.9:  # At least 10% reduction
                response.body = compressed_body
                response.headers["content-encoding"] = "gzip"
                response.headers["content-length"] = str(compressed_size)
                
                # Update metrics
                self._optimization_metrics["responses_compressed"] += 1
                self._optimization_metrics["compression_ratio"] = (
                    self._optimization_metrics["compression_ratio"] * 0.9 + compression_ratio * 0.1
                )
        
        except Exception as e:
            logger.warning(f"Compression failed: {e}")
        
        return response
    
    async def _apply_streaming_optimization(self, request: Request, response: Response) -> Response:
        """Apply streaming optimization for large responses"""
        # Only apply streaming for large JSON responses
        if (hasattr(response, 'body') and 
            len(response.body) > 50000 and  # 50KB threshold
            response.headers.get("content-type", "").startswith("application/json")):
            
            try:
                # Convert to streaming response for large JSON
                def generate_chunks():
                    chunk_size = 8192  # 8KB chunks
                    body = response.body
                    for i in range(0, len(body), chunk_size):
                        yield body[i:i + chunk_size]
                
                return StreamingResponse(
                    generate_chunks(),
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type
                )
            except Exception as e:
                logger.warning(f"Streaming optimization failed: {e}")
        
        return response
    
    async def _optimize_background_tasks(self, background_tasks: BackgroundTasks):
        """Optimize background task execution with batching"""
        if not background_tasks.tasks:
            return
        
        # Add tasks to optimization queue
        for task in background_tasks.tasks:
            self._background_task_queue.append({
                "func": task.func,
                "args": task.args,
                "kwargs": task.kwargs,
                "timestamp": time.time(),
                "priority": 5  # Default priority
            })
        
        # Clear original tasks to prevent duplicate execution
        background_tasks.tasks.clear()
        
        # Process tasks in batches if queue is large enough
        if len(self._background_task_queue) >= self._task_batch_size:
            await self._process_background_task_batch()
        
        self._optimization_metrics["background_tasks_optimized"] += len(background_tasks.tasks)
    
    async def _process_background_task_batch(self):
        """Process background tasks in optimized batches"""
        if not self._background_task_queue:
            return
        
        # Extract batch of tasks
        batch = []
        for _ in range(min(self._task_batch_size, len(self._background_task_queue))):
            if self._background_task_queue:
                batch.append(self._background_task_queue.popleft())
        
        # Sort by priority
        batch.sort(key=lambda x: x["priority"], reverse=True)
        
        # Execute tasks in parallel
        async def execute_task(task):
            try:
                if asyncio.iscoroutinefunction(task["func"]):
                    await task["func"](*task["args"], **task["kwargs"])
                else:
                    task["func"](*task["args"], **task["kwargs"])
            except Exception as e:
                logger.warning(f"Background task execution failed: {e}")
        
        # Run tasks concurrently
        await asyncio.gather(
            *[execute_task(task) for task in batch],
            return_exceptions=True
        )
    
    async def _optimize_resource_cleanup(self):
        """Optimize resource cleanup with adaptive strategies"""
        current_time = time.time()
        
        # Adaptive garbage collection based on request frequency
        if (self._request_count % 100 == 0 or 
            current_time - self._last_gc_time > 60):  # Every 100 requests or 60 seconds
            
            # Get memory usage before cleanup
            import psutil
            process = psutil.Process()
            memory_before = process.memory_info().rss
            
            # Perform garbage collection
            collected = gc.collect()
            
            # Get memory usage after cleanup
            memory_after = process.memory_info().rss
            memory_saved = memory_before - memory_after
            
            if collected > 0 or memory_saved > 0:
                self._optimization_metrics["gc_optimizations"] += 1
                self._optimization_metrics["memory_optimizations"] += memory_saved
                
                logger.debug(
                    f"Resource cleanup: {collected} objects collected, "
                    f"{memory_saved / 1024 / 1024:.2f}MB memory freed"
                )
            
            self._last_gc_time = current_time
            
            # Track memory usage history
            self._memory_usage_history.append({
                "timestamp": current_time,
                "memory_rss": memory_after,
                "objects_collected": collected
            })
        
        # Clean expired cache entries periodically
        if self._request_count % 50 == 0:
            await self._clean_expired_cache()
    
    async def _manage_cache_size(self):
        """Manage cache size with intelligent LRU eviction"""
        if len(self._response_cache) > 800:  # Start eviction at 80% of limit
            # Sort by access time and hit count (LRU with frequency consideration)
            cache_items = []
            for key, (data, cached_time, ttl) in self._response_cache.items():
                access_time = self._cache_access_times.get(key, cached_time)
                hit_count = self._cache_hit_counts.get(key, 1)
                score = access_time + (hit_count * 3600)  # Boost frequently accessed items
                cache_items.append((key, score))
            
            # Sort by score (lower score = less valuable)
            cache_items.sort(key=lambda x: x[1])
            
            # Remove least valuable 20% of entries
            remove_count = len(cache_items) // 5
            for key, _ in cache_items[:remove_count]:
                if key in self._response_cache:
                    del self._response_cache[key]
                if key in self._cache_access_times:
                    del self._cache_access_times[key]
                if key in self._cache_hit_counts:
                    del self._cache_hit_counts[key]
    
    async def _clean_expired_cache(self):
        """Clean expired cache entries"""
        current_time = time.time()
        expired_keys = []
        
        for key, (data, cached_time, ttl) in self._response_cache.items():
            if current_time - cached_time > ttl:
                expired_keys.append(key)
        
        for key in expired_keys:
            if key in self._response_cache:
                del self._response_cache[key]
            if key in self._cache_access_times:
                del self._cache_access_times[key]
            if key in self._cache_hit_counts:
                del self._cache_hit_counts[key]
    
    def _generate_cache_key(self, request: Request) -> str:
        """Generate cache key for request"""
        import hashlib
        
        # Include method, path, and relevant query parameters
        key_components = [
            request.method,
            str(request.url.path),
            str(sorted(request.query_params.items()))
        ]
        
        # Include authorization if present (for user-specific caching)
        auth_header = request.headers.get("authorization")
        if auth_header:
            key_components.append(hashlib.md5(auth_header.encode()).hexdigest()[:8])
        
        cache_string = "|".join(key_components)
        return hashlib.md5(cache_string.encode()).hexdigest()
    
    def _calculate_cache_ttl(self, request: Request, response: Response) -> int:
        """Calculate intelligent cache TTL based on endpoint characteristics"""
        path = request.url.path
        
        # Different TTLs for different endpoint types
        if "/health" in path:
            return 30  # Health endpoints cached for 30 seconds
        elif "/metrics" in path:
            return 10  # Metrics cached for 10 seconds
        elif "/api/v1/knowledge" in path:
            return 600  # Knowledge queries cached for 10 minutes
        elif "/api/v1/data" in path:
            return 300  # Data endpoints cached for 5 minutes
        elif response.status_code == 200:
            return self.cache_ttl_seconds  # Default successful response TTL
        else:
            return 60  # Error responses cached briefly
    
    def _update_cache_access(self, request: Request):
        """Update cache access tracking"""
        cache_key = self._generate_cache_key(request)
        self._cache_access_times[cache_key] = time.time()
        self._cache_hit_counts[cache_key] += 1
    
    def _update_performance_metrics(self, processing_time: float):
        """Update performance metrics"""
        self._optimization_metrics["requests_processed"] += 1
        
        # Update average response time
        current_avg = self._optimization_metrics["avg_response_time"]
        request_count = self._optimization_metrics["requests_processed"]
        
        if request_count == 1:
            self._optimization_metrics["avg_response_time"] = processing_time
        else:
            self._optimization_metrics["avg_response_time"] = (
                (current_avg * (request_count - 1) + processing_time) / request_count
            )
        
        # Update cache hit rate
        total_cache_requests = (
            self._optimization_metrics["cache_hits"] + 
            self._optimization_metrics["cache_misses"]
        )
        if total_cache_requests > 0:
            self._optimization_metrics["cache_hit_rate"] = (
                self._optimization_metrics["cache_hits"] / total_cache_requests
            )
    
    def _start_background_optimization(self):
        """Start background optimization tasks"""
        async def background_optimizer():
            while True:
                try:
                    # Process pending background tasks
                    if self._background_task_queue:
                        await self._process_background_task_batch()
                    
                    # Clean up resources periodically
                    await self._clean_expired_cache()
                    
                    await asyncio.sleep(5)  # Run every 5 seconds
                except Exception as e:
                    logger.warning(f"Background optimizer error: {e}")
                    await asyncio.sleep(10)
        
        # Start background task
        asyncio.create_task(background_optimizer())
    
    def get_optimization_metrics(self) -> Dict[str, Any]:
        """Get comprehensive optimization metrics"""
        return {
            **self._optimization_metrics,
            "cache_status": {
                "cache_size": len(self._response_cache),
                "cache_memory_usage": sum(
                    len(str(data)) for data, _, _ in self._response_cache.values()
                ),
                "most_accessed_endpoints": sorted(
                    self._cache_hit_counts.items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:5]
            },
            "background_tasks": {
                "queued_tasks": len(self._background_task_queue),
                "batch_size": self._task_batch_size
            },
            "resource_cleanup": {
                "gc_cycles": self._optimization_metrics["gc_optimizations"],
                "memory_usage_history": list(self._memory_usage_history)[-10:],  # Last 10 samples
                "request_count": self._request_count
            },
            "optimization_config": {
                "compression_enabled": self.enable_compression,
                "caching_enabled": self.enable_caching,
                "background_optimization_enabled": self.enable_background_optimization,
                "resource_cleanup_enabled": self.enable_resource_cleanup,
                "compression_threshold": self.compression_threshold,
                "cache_ttl_seconds": self.cache_ttl_seconds
            }
        }