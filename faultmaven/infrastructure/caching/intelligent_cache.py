"""Intelligent Caching System with Usage Pattern Analysis

This module provides an advanced caching system specifically designed for
FaultMaven Phase 2 intelligent troubleshooting services. It includes
usage pattern analysis, adaptive caching strategies, and performance
optimization based on real-world usage data.

Key Features:
- Multi-tier caching (L1: in-memory, L2: Redis, L3: persistent storage)
- Usage pattern analysis and adaptive cache sizing
- Context-aware cache keys with semantic similarity
- Intelligent cache eviction based on access patterns
- Cache warming strategies for frequently accessed data
- Performance-driven cache optimization
- Cross-service cache coordination
- Privacy-preserving cache analytics

Performance Targets:
- L1 cache hit: < 1ms
- L2 cache hit: < 5ms
- L3 cache hit: < 20ms
- Cache analytics: < 10ms overhead
"""

import asyncio
import hashlib
import json
import logging
import pickle
import time
from collections import defaultdict, Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, List, Tuple, Union, Callable, Set
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
import statistics

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.observability.metrics_collector import MetricsCollector


@dataclass
class CacheEntry:
    """Individual cache entry with metadata"""
    key: str
    value: Any
    created_at: datetime
    last_accessed: datetime
    access_count: int = 0
    size_bytes: int = 0
    ttl_seconds: Optional[int] = None
    tags: Set[str] = field(default_factory=set)
    semantic_hash: Optional[str] = None
    priority_score: float = 1.0


@dataclass
class CacheStats:
    """Cache performance statistics"""
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    size_bytes: int = 0
    entry_count: int = 0
    hit_rate: float = 0.0
    avg_access_time: float = 0.0
    last_updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class AccessPattern:
    """Cache access pattern for analysis"""
    key_pattern: str
    access_times: List[datetime]
    access_frequency: float
    seasonal_pattern: Dict[str, float]  # hour -> frequency
    user_distribution: Dict[str, int]  # user_hash -> count
    effectiveness_score: float
    recommended_ttl: int
    cache_tier: str


class CacheAnalytics:
    """Cache usage analytics and pattern detection"""
    
    def __init__(self, metrics_collector: Optional[MetricsCollector] = None):
        self.metrics_collector = metrics_collector
        self.access_patterns: Dict[str, AccessPattern] = {}
        self.user_patterns: Dict[str, Dict[str, Any]] = defaultdict(dict)
        self.temporal_patterns: Dict[str, List[datetime]] = defaultdict(list)
        self._analytics_lock = threading.RLock()
        self.logger = logging.getLogger(__name__)
    
    def record_access(
        self,
        key: str,
        hit: bool,
        access_time: float,
        user_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record cache access for pattern analysis"""
        with self._analytics_lock:
            # Hash user ID for privacy
            user_hash = None
            if user_id:
                user_hash = hashlib.sha256(user_id.encode()).hexdigest()[:16]
            
            # Generalize key pattern for analysis
            key_pattern = self._generalize_key_pattern(key)
            
            # Update access patterns
            if key_pattern not in self.access_patterns:
                self.access_patterns[key_pattern] = AccessPattern(
                    key_pattern=key_pattern,
                    access_times=[],
                    access_frequency=0.0,
                    seasonal_pattern={},
                    user_distribution={},
                    effectiveness_score=0.0,
                    recommended_ttl=3600,  # 1 hour default
                    cache_tier="L1"
                )
            
            pattern = self.access_patterns[key_pattern]
            now = datetime.now(timezone.utc)
            pattern.access_times.append(now)
            
            # Update user distribution
            if user_hash:
                pattern.user_distribution[user_hash] = pattern.user_distribution.get(user_hash, 0) + 1
            
            # Update seasonal patterns (hour of day)
            hour = now.hour
            pattern.seasonal_pattern[str(hour)] = pattern.seasonal_pattern.get(str(hour), 0) + 1
            
            # Calculate effectiveness score
            if hit:
                pattern.effectiveness_score = min(1.0, pattern.effectiveness_score + 0.1)
            else:
                pattern.effectiveness_score = max(0.0, pattern.effectiveness_score - 0.05)
            
            # Update frequency calculation
            if len(pattern.access_times) > 1:
                time_span = (pattern.access_times[-1] - pattern.access_times[0]).total_seconds()
                pattern.access_frequency = len(pattern.access_times) / max(1, time_span / 3600)  # per hour
            
            # Record temporal access pattern
            self.temporal_patterns[key_pattern].append(now)
            
            # Limit stored access times to prevent memory bloat
            if len(pattern.access_times) > 1000:
                pattern.access_times = pattern.access_times[-500:]
            
            if len(self.temporal_patterns[key_pattern]) > 1000:
                self.temporal_patterns[key_pattern] = self.temporal_patterns[key_pattern][-500:]
    
    def get_optimization_recommendations(self) -> List[Dict[str, Any]]:
        """Get cache optimization recommendations based on usage patterns"""
        recommendations = []
        
        with self._analytics_lock:
            for key_pattern, pattern in self.access_patterns.items():
                # High-frequency, low-effectiveness patterns
                if pattern.access_frequency > 10 and pattern.effectiveness_score < 0.5:
                    recommendations.append({
                        "type": "cache_strategy",
                        "priority": "high",
                        "pattern": key_pattern,
                        "issue": "High access frequency with low hit rate",
                        "recommendation": "Increase cache TTL or implement predictive caching",
                        "current_hit_rate": pattern.effectiveness_score,
                        "access_frequency": pattern.access_frequency,
                        "suggested_ttl": min(86400, int(pattern.recommended_ttl * 2))
                    })
                
                # Seasonal access patterns
                if self._has_strong_seasonal_pattern(pattern):
                    peak_hours = self._get_peak_hours(pattern)
                    recommendations.append({
                        "type": "cache_warming",
                        "priority": "medium",
                        "pattern": key_pattern,
                        "issue": "Strong seasonal access pattern detected",
                        "recommendation": f"Implement cache warming for peak hours: {peak_hours}",
                        "peak_hours": peak_hours,
                        "seasonal_strength": self._calculate_seasonal_strength(pattern)
                    })
                
                # Multi-user patterns
                if len(pattern.user_distribution) > 10:
                    recommendations.append({
                        "type": "cache_sharing",
                        "priority": "medium", 
                        "pattern": key_pattern,
                        "issue": "High multi-user access pattern",
                        "recommendation": "Consider moving to shared cache tier (L2/L3)",
                        "user_count": len(pattern.user_distribution),
                        "suggested_tier": "L2" if len(pattern.user_distribution) < 50 else "L3"
                    })
        
        return sorted(recommendations, key=lambda x: {"high": 3, "medium": 2, "low": 1}[x["priority"]], reverse=True)
    
    def _generalize_key_pattern(self, key: str) -> str:
        """Generalize cache key to identify patterns"""
        # Replace UUIDs, timestamps, and other variable parts with placeholders
        import re
        
        # Replace UUIDs
        key = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', '<UUID>', key)
        
        # Replace timestamps
        key = re.sub(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', '<TIMESTAMP>', key)
        
        # Replace numbers that might be IDs
        key = re.sub(r':\d+:', ':<ID>:', key)
        
        # Replace hash-like strings
        key = re.sub(r'[0-9a-f]{16,}', '<HASH>', key)
        
        return key
    
    def _has_strong_seasonal_pattern(self, pattern: AccessPattern) -> bool:
        """Check if pattern has strong seasonal component"""
        if len(pattern.seasonal_pattern) < 3:
            return False
        
        values = list(pattern.seasonal_pattern.values())
        if not values:
            return False
        
        max_val = max(values)
        min_val = min(values)
        
        # Strong seasonal pattern if max is >3x min
        return max_val > 3 * min_val
    
    def _get_peak_hours(self, pattern: AccessPattern) -> List[int]:
        """Get peak usage hours from seasonal pattern"""
        if not pattern.seasonal_pattern:
            return []
        
        # Find hours with above-average usage
        values = list(pattern.seasonal_pattern.values())
        avg_usage = statistics.mean(values)
        
        peak_hours = []
        for hour_str, usage in pattern.seasonal_pattern.items():
            if usage > avg_usage * 1.5:
                peak_hours.append(int(hour_str))
        
        return sorted(peak_hours)
    
    def _calculate_seasonal_strength(self, pattern: AccessPattern) -> float:
        """Calculate strength of seasonal pattern (0-1)"""
        if not pattern.seasonal_pattern:
            return 0.0
        
        values = list(pattern.seasonal_pattern.values())
        if len(values) < 2:
            return 0.0
        
        # Use coefficient of variation as seasonal strength indicator
        mean_val = statistics.mean(values)
        if mean_val == 0:
            return 0.0
        
        std_val = statistics.stdev(values)
        cv = std_val / mean_val
        
        # Normalize to 0-1 range
        return min(1.0, cv / 2.0)


class IntelligentCache(BaseExternalClient):
    """Multi-tier intelligent cache with usage pattern analysis
    
    This cache system provides:
    - L1: In-memory cache for fastest access
    - L2: Redis-based cache for shared access
    - L3: Persistent storage for long-term caching
    - Advanced analytics and optimization recommendations
    - Context-aware caching strategies
    - Automatic cache warming and eviction
    """
    
    def __init__(
        self,
        l1_max_size: int = 1000,
        l1_ttl_seconds: int = 300,  # 5 minutes
        l2_ttl_seconds: int = 3600,  # 1 hour
        l3_ttl_seconds: int = 86400,  # 24 hours
        metrics_collector: Optional[MetricsCollector] = None,
        redis_client: Optional[Any] = None,
        enable_analytics: bool = True
    ):
        """Initialize intelligent cache system
        
        Args:
            l1_max_size: Maximum entries in L1 cache
            l1_ttl_seconds: Default TTL for L1 cache
            l2_ttl_seconds: Default TTL for L2 cache
            l3_ttl_seconds: Default TTL for L3 cache
            metrics_collector: Metrics collection service
            redis_client: Optional Redis client for L2 cache
            enable_analytics: Whether to enable usage analytics
        """
        super().__init__(
            client_name="IntelligentCache",
            service_name="FaultMaven-Cache",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=5,
            circuit_breaker_timeout=30
        )
        
        # Cache configuration
        self._l1_max_size = l1_max_size
        self._l1_ttl_seconds = l1_ttl_seconds
        self._l2_ttl_seconds = l2_ttl_seconds
        self._l3_ttl_seconds = l3_ttl_seconds
        
        # L1 Cache (in-memory)
        self._l1_cache: Dict[str, CacheEntry] = {}
        self._l1_lock = threading.RLock()
        self._l1_stats = CacheStats()
        
        # L2 Cache (Redis)
        self._redis_client = redis_client
        self._l2_stats = CacheStats()
        
        # L3 Cache (persistent - would be implemented with actual storage)
        self._l3_stats = CacheStats()
        
        # Analytics
        self._enable_analytics = enable_analytics
        self._analytics = CacheAnalytics(metrics_collector) if enable_analytics else None
        self._metrics_collector = metrics_collector
        
        # Background processing
        self._background_tasks_running = False
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cache")
        
        # Cache optimization
        self._optimization_rules: List[Callable] = []
        self._setup_optimization_rules()
        
        # Precomputed cache warming data
        self._warming_candidates: Dict[str, Dict[str, Any]] = {}
        self._warming_lock = threading.RLock()
        
        self.logger.info("IntelligentCache initialized with multi-tier architecture")
    
    async def start_background_processing(self):
        """Start background cache optimization tasks"""
        if self._background_tasks_running:
            return
        
        self._background_tasks_running = True
        
        # Start background optimization tasks
        asyncio.create_task(self._periodic_optimization())
        asyncio.create_task(self._cache_warming_task())
        asyncio.create_task(self._analytics_processor())
        
        self.logger.info("Cache background processing tasks started")
    
    async def stop_background_processing(self):
        """Stop background cache processing tasks"""
        self._background_tasks_running = False
        self._executor.shutdown(wait=True)
        self.logger.info("Cache background processing tasks stopped")
    
    async def get(
        self,
        key: str,
        context: Optional[Dict[str, Any]] = None,
        user_id: Optional[str] = None
    ) -> Tuple[Any, bool]:
        """Get value from cache with intelligent tier selection
        
        Args:
            key: Cache key
            context: Optional context for cache key generation
            user_id: Optional user ID for analytics
            
        Returns:
            Tuple of (value, hit) where hit indicates if value was found
        """
        start_time = time.time()
        
        # Generate semantic key if context provided
        cache_key = self._generate_cache_key(key, context)
        
        try:
            # Try L1 cache first
            value, hit = await self._get_from_l1(cache_key)
            if hit:
                access_time = (time.time() - start_time) * 1000
                self._record_cache_access("L1", cache_key, True, access_time, user_id, context)
                return value, True
            
            # Try L2 cache (Redis)
            if self._redis_client:
                value, hit = await self._get_from_l2(cache_key)
                if hit:
                    # Promote to L1 for faster future access
                    await self._set_to_l1(cache_key, value, self._l1_ttl_seconds, context)
                    access_time = (time.time() - start_time) * 1000
                    self._record_cache_access("L2", cache_key, True, access_time, user_id, context)
                    return value, True
            
            # Try L3 cache (persistent storage)
            value, hit = await self._get_from_l3(cache_key)
            if hit:
                # Promote to L2 and L1
                if self._redis_client:
                    await self._set_to_l2(cache_key, value, self._l2_ttl_seconds, context)
                await self._set_to_l1(cache_key, value, self._l1_ttl_seconds, context)
                access_time = (time.time() - start_time) * 1000
                self._record_cache_access("L3", cache_key, True, access_time, user_id, context)
                return value, True
            
            # Cache miss
            access_time = (time.time() - start_time) * 1000
            self._record_cache_access("miss", cache_key, False, access_time, user_id, context)
            return None, False
            
        except Exception as e:
            self.logger.error(f"Cache get error for key {cache_key}: {e}")
            return None, False
    
    async def set(
        self,
        key: str,
        value: Any,
        ttl_seconds: Optional[int] = None,
        context: Optional[Dict[str, Any]] = None,
        cache_tier: str = "auto",
        tags: Optional[Set[str]] = None
    ) -> bool:
        """Set value in cache with intelligent tier placement
        
        Args:
            key: Cache key
            value: Value to cache
            ttl_seconds: Time to live in seconds
            context: Optional context for cache key generation
            cache_tier: Target cache tier ("L1", "L2", "L3", "auto")
            tags: Optional tags for cache entry categorization
            
        Returns:
            True if value was successfully cached
        """
        try:
            cache_key = self._generate_cache_key(key, context)
            ttl = ttl_seconds or self._get_recommended_ttl(cache_key, context)
            
            if cache_tier == "auto":
                cache_tier = self._determine_optimal_tier(cache_key, value, context)
            
            success = False
            
            # Set in appropriate tiers based on strategy
            if cache_tier in ["L1", "auto"]:
                success = await self._set_to_l1(cache_key, value, ttl, context, tags) or success
            
            if cache_tier in ["L2", "auto"] and self._redis_client:
                success = await self._set_to_l2(cache_key, value, ttl, context, tags) or success
            
            if cache_tier in ["L3", "auto"]:
                success = await self._set_to_l3(cache_key, value, ttl, context, tags) or success
            
            return success
            
        except Exception as e:
            self.logger.error(f"Cache set error for key {key}: {e}")
            return False
    
    async def delete(self, key: str, context: Optional[Dict[str, Any]] = None) -> bool:
        """Delete key from all cache tiers"""
        try:
            cache_key = self._generate_cache_key(key, context)
            
            success = True
            success &= await self._delete_from_l1(cache_key)
            
            if self._redis_client:
                success &= await self._delete_from_l2(cache_key)
            
            success &= await self._delete_from_l3(cache_key)
            
            return success
            
        except Exception as e:
            self.logger.error(f"Cache delete error for key {key}: {e}")
            return False
    
    async def clear_by_pattern(self, pattern: str) -> int:
        """Clear cache entries matching a pattern"""
        cleared_count = 0
        
        try:
            # Clear from L1
            with self._l1_lock:
                keys_to_remove = [k for k in self._l1_cache.keys() if pattern in k]
                for key in keys_to_remove:
                    del self._l1_cache[key]
                    cleared_count += 1
            
            # Clear from L2 (Redis pattern matching would be implemented here)
            if self._redis_client:
                # Redis SCAN with pattern would be used here
                pass
            
            # Clear from L3 (persistent storage pattern matching)
            # Implementation would depend on storage backend
            
            return cleared_count
            
        except Exception as e:
            self.logger.error(f"Cache pattern clear error for pattern {pattern}: {e}")
            return 0
    
    async def get_cache_statistics(self) -> Dict[str, Any]:
        """Get comprehensive cache statistics and analytics"""
        try:
            stats = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "l1_cache": {
                    "hits": self._l1_stats.hits,
                    "misses": self._l1_stats.misses,
                    "hit_rate": self._l1_stats.hit_rate,
                    "entry_count": len(self._l1_cache),
                    "max_size": self._l1_max_size,
                    "avg_access_time_ms": self._l1_stats.avg_access_time,
                    "size_bytes": self._l1_stats.size_bytes
                },
                "l2_cache": {
                    "hits": self._l2_stats.hits,
                    "misses": self._l2_stats.misses,
                    "hit_rate": self._l2_stats.hit_rate,
                    "available": self._redis_client is not None
                },
                "l3_cache": {
                    "hits": self._l3_stats.hits,
                    "misses": self._l3_stats.misses,
                    "hit_rate": self._l3_stats.hit_rate
                },
                "overall": {
                    "total_hits": self._l1_stats.hits + self._l2_stats.hits + self._l3_stats.hits,
                    "total_misses": self._l1_stats.misses + self._l2_stats.misses + self._l3_stats.misses
                }
            }
            
            # Calculate overall hit rate
            total_requests = stats["overall"]["total_hits"] + stats["overall"]["total_misses"]
            if total_requests > 0:
                stats["overall"]["hit_rate"] = stats["overall"]["total_hits"] / total_requests
            else:
                stats["overall"]["hit_rate"] = 0.0
            
            # Add analytics data if available
            if self._analytics:
                recommendations = self._analytics.get_optimization_recommendations()
                stats["analytics"] = {
                    "patterns_detected": len(self._analytics.access_patterns),
                    "optimization_recommendations": recommendations[:10],  # Top 10
                    "top_access_patterns": self._get_top_access_patterns()
                }
            
            return stats
            
        except Exception as e:
            self.logger.error(f"Error getting cache statistics: {e}")
            return {"error": str(e)}
    
    async def warm_cache(self, warming_data: Dict[str, Any]) -> Dict[str, Any]:
        """Warm cache with predicted data
        
        Args:
            warming_data: Dictionary of key-value pairs to pre-load
            
        Returns:
            Warming results with success/failure counts
        """
        results = {
            "requested": len(warming_data),
            "succeeded": 0,
            "failed": 0,
            "errors": []
        }
        
        for key, value in warming_data.items():
            try:
                success = await self.set(key, value, cache_tier="L1")
                if success:
                    results["succeeded"] += 1
                else:
                    results["failed"] += 1
                    results["errors"].append(f"Failed to warm key: {key}")
            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"Error warming key {key}: {e}")
        
        self.logger.info(f"Cache warming completed: {results['succeeded']}/{results['requested']} successful")
        return results
    
    def _generate_cache_key(self, key: str, context: Optional[Dict[str, Any]] = None) -> str:
        """Generate cache key with optional context"""
        if not context:
            return key
        
        # Create semantic hash from context
        context_str = json.dumps(context, sort_keys=True)
        context_hash = hashlib.md5(context_str.encode()).hexdigest()[:8]
        
        return f"{key}:{context_hash}"
    
    def _get_recommended_ttl(self, cache_key: str, context: Optional[Dict[str, Any]] = None) -> int:
        """Get recommended TTL based on usage patterns"""
        if not self._analytics:
            return self._l1_ttl_seconds
        
        key_pattern = self._analytics._generalize_key_pattern(cache_key)
        if key_pattern in self._analytics.access_patterns:
            pattern = self._analytics.access_patterns[key_pattern]
            return pattern.recommended_ttl
        
        return self._l1_ttl_seconds
    
    def _determine_optimal_tier(
        self,
        cache_key: str,
        value: Any,
        context: Optional[Dict[str, Any]] = None
    ) -> str:
        """Determine optimal cache tier for a key-value pair"""
        # Size-based decisions
        try:
            size_bytes = len(pickle.dumps(value))
            if size_bytes > 10 * 1024 * 1024:  # >10MB
                return "L3"
            elif size_bytes > 1024 * 1024:  # >1MB
                return "L2"
        except:
            pass
        
        # Pattern-based decisions
        if self._analytics:
            key_pattern = self._analytics._generalize_key_pattern(cache_key)
            if key_pattern in self._analytics.access_patterns:
                pattern = self._analytics.access_patterns[key_pattern]
                
                # High-frequency access -> L1
                if pattern.access_frequency > 10:
                    return "L1"
                # Multi-user access -> L2
                elif len(pattern.user_distribution) > 5:
                    return "L2"
                # Infrequent access -> L3
                elif pattern.access_frequency < 1:
                    return "L3"
        
        return "L1"  # Default to L1
    
    async def _get_from_l1(self, cache_key: str) -> Tuple[Any, bool]:
        """Get value from L1 cache"""
        with self._l1_lock:
            if cache_key in self._l1_cache:
                entry = self._l1_cache[cache_key]
                
                # Check TTL
                if entry.ttl_seconds:
                    age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                    if age > entry.ttl_seconds:
                        del self._l1_cache[cache_key]
                        self._l1_stats.misses += 1
                        self._update_hit_rate(self._l1_stats)
                        return None, False
                
                # Update access metadata
                entry.last_accessed = datetime.now(timezone.utc)
                entry.access_count += 1
                
                self._l1_stats.hits += 1
                self._update_hit_rate(self._l1_stats)
                return entry.value, True
            
            self._l1_stats.misses += 1
            self._update_hit_rate(self._l1_stats)
            return None, False
    
    async def _set_to_l1(
        self,
        cache_key: str,
        value: Any,
        ttl_seconds: int,
        context: Optional[Dict[str, Any]] = None,
        tags: Optional[Set[str]] = None
    ) -> bool:
        """Set value in L1 cache"""
        try:
            with self._l1_lock:
                # Check if cache is full and evict if necessary
                if len(self._l1_cache) >= self._l1_max_size:
                    self._evict_from_l1()
                
                # Calculate size
                try:
                    size_bytes = len(pickle.dumps(value))
                except:
                    size_bytes = 0
                
                # Create cache entry
                entry = CacheEntry(
                    key=cache_key,
                    value=value,
                    created_at=datetime.now(timezone.utc),
                    last_accessed=datetime.now(timezone.utc),
                    size_bytes=size_bytes,
                    ttl_seconds=ttl_seconds,
                    tags=tags or set()
                )
                
                self._l1_cache[cache_key] = entry
                self._l1_stats.size_bytes += size_bytes
                return True
                
        except Exception as e:
            self.logger.error(f"Error setting L1 cache: {e}")
            return False
    
    async def _get_from_l2(self, cache_key: str) -> Tuple[Any, bool]:
        """Get value from L2 cache (Redis)"""
        if not self._redis_client:
            return None, False
        
        try:
            # This would be implemented with actual Redis operations
            value = await self.call_external_async(
                "redis_get",
                lambda: None  # Placeholder - would use redis_client.get(cache_key)
            )
            
            if value is not None:
                self._l2_stats.hits += 1
                self._update_hit_rate(self._l2_stats)
                return pickle.loads(value), True
            else:
                self._l2_stats.misses += 1
                self._update_hit_rate(self._l2_stats)
                return None, False
                
        except Exception as e:
            self.logger.error(f"Error getting from L2 cache: {e}")
            self._l2_stats.misses += 1
            self._update_hit_rate(self._l2_stats)
            return None, False
    
    async def _set_to_l2(
        self,
        cache_key: str,
        value: Any,
        ttl_seconds: int,
        context: Optional[Dict[str, Any]] = None,
        tags: Optional[Set[str]] = None
    ) -> bool:
        """Set value in L2 cache (Redis)"""
        if not self._redis_client:
            return False
        
        try:
            serialized_value = pickle.dumps(value)
            
            # This would be implemented with actual Redis operations
            success = await self.call_external_async(
                "redis_set",
                lambda: True  # Placeholder - would use redis_client.setex(cache_key, ttl_seconds, serialized_value)
            )
            
            return success
            
        except Exception as e:
            self.logger.error(f"Error setting L2 cache: {e}")
            return False
    
    async def _get_from_l3(self, cache_key: str) -> Tuple[Any, bool]:
        """Get value from L3 cache (persistent storage)"""
        # Placeholder for L3 cache implementation
        self._l3_stats.misses += 1
        self._update_hit_rate(self._l3_stats)
        return None, False
    
    async def _set_to_l3(
        self,
        cache_key: str,
        value: Any,
        ttl_seconds: int,
        context: Optional[Dict[str, Any]] = None,
        tags: Optional[Set[str]] = None
    ) -> bool:
        """Set value in L3 cache (persistent storage)"""
        # Placeholder for L3 cache implementation
        return False
    
    async def _delete_from_l1(self, cache_key: str) -> bool:
        """Delete key from L1 cache"""
        with self._l1_lock:
            if cache_key in self._l1_cache:
                entry = self._l1_cache[cache_key]
                self._l1_stats.size_bytes -= entry.size_bytes
                del self._l1_cache[cache_key]
                return True
        return False
    
    async def _delete_from_l2(self, cache_key: str) -> bool:
        """Delete key from L2 cache"""
        if not self._redis_client:
            return False
        
        try:
            return await self.call_external_async(
                "redis_delete",
                lambda: True  # Placeholder - would use redis_client.delete(cache_key)
            )
        except Exception as e:
            self.logger.error(f"Error deleting from L2 cache: {e}")
            return False
    
    async def _delete_from_l3(self, cache_key: str) -> bool:
        """Delete key from L3 cache"""
        # Placeholder for L3 cache implementation
        return False
    
    def _evict_from_l1(self) -> None:
        """Evict entries from L1 cache using LRU + intelligent scoring"""
        if len(self._l1_cache) < self._l1_max_size * 0.8:
            return
        
        # Calculate eviction scores for all entries
        scored_entries = []
        for key, entry in self._l1_cache.items():
            score = self._calculate_eviction_score(entry)
            scored_entries.append((score, key, entry))
        
        # Sort by score (lower score = higher eviction priority)
        scored_entries.sort(key=lambda x: x[0])
        
        # Evict 25% of cache
        evict_count = max(1, len(scored_entries) // 4)
        for score, key, entry in scored_entries[:evict_count]:
            self._l1_stats.size_bytes -= entry.size_bytes
            del self._l1_cache[key]
            self._l1_stats.evictions += 1
    
    def _calculate_eviction_score(self, entry: CacheEntry) -> float:
        """Calculate eviction score for cache entry (lower = more likely to evict)"""
        now = datetime.now(timezone.utc)
        
        # Time-based factors
        age_seconds = (now - entry.created_at).total_seconds()
        time_since_access = (now - entry.last_accessed).total_seconds()
        
        # Base score from access patterns
        score = entry.access_count * 0.3  # Access frequency
        score += entry.priority_score * 0.2  # Manual priority
        
        # Time decay factors
        score *= max(0.1, 1.0 - (age_seconds / 86400))  # Age decay over 24 hours
        score *= max(0.1, 1.0 - (time_since_access / 3600))  # Access recency decay over 1 hour
        
        # Size penalty for large entries
        if entry.size_bytes > 100 * 1024:  # 100KB
            score *= 0.8
        
        return score
    
    def _record_cache_access(
        self,
        tier: str,
        cache_key: str,
        hit: bool,
        access_time: float,
        user_id: Optional[str],
        context: Optional[Dict[str, Any]]
    ) -> None:
        """Record cache access for analytics"""
        if self._analytics:
            self._analytics.record_access(cache_key, hit, access_time, user_id, context)
        
        if self._metrics_collector:
            self._metrics_collector.record_cache_event(
                service="intelligent_cache",
                cache_key=cache_key,
                event_type="hit" if hit else "miss",
                retrieval_time_ms=access_time,
                metadata={
                    "cache_tier": tier,
                    "has_context": context is not None,
                    "has_user": user_id is not None
                }
            )
    
    def _update_hit_rate(self, stats: CacheStats) -> None:
        """Update hit rate for cache stats"""
        total = stats.hits + stats.misses
        if total > 0:
            stats.hit_rate = stats.hits / total
        stats.last_updated = datetime.now(timezone.utc)
    
    def _setup_optimization_rules(self) -> None:
        """Setup cache optimization rules"""
        self._optimization_rules = [
            self._rule_promote_frequent_access,
            self._rule_demote_large_unused,
            self._rule_seasonal_warming,
            self._rule_user_pattern_optimization
        ]
    
    def _rule_promote_frequent_access(self, key_pattern: str, pattern: AccessPattern) -> Dict[str, Any]:
        """Rule: Promote frequently accessed items to L1"""
        if pattern.access_frequency > 5 and pattern.cache_tier != "L1":
            return {
                "action": "promote_to_l1",
                "pattern": key_pattern,
                "reason": f"High access frequency: {pattern.access_frequency}/hour"
            }
        return {}
    
    def _rule_demote_large_unused(self, key_pattern: str, pattern: AccessPattern) -> Dict[str, Any]:
        """Rule: Demote large, infrequently accessed items"""
        # This would check actual entry sizes in implementation
        if pattern.access_frequency < 0.1:
            return {
                "action": "demote_to_l3",
                "pattern": key_pattern,
                "reason": f"Low access frequency: {pattern.access_frequency}/hour"
            }
        return {}
    
    def _rule_seasonal_warming(self, key_pattern: str, pattern: AccessPattern) -> Dict[str, Any]:
        """Rule: Pre-warm cache based on seasonal patterns"""
        if self._analytics._has_strong_seasonal_pattern(pattern):
            peak_hours = self._analytics._get_peak_hours(pattern)
            current_hour = datetime.now(timezone.utc).hour
            
            if current_hour in peak_hours:
                return {
                    "action": "warm_cache",
                    "pattern": key_pattern,
                    "reason": f"Peak usage hour detected: {current_hour}"
                }
        return {}
    
    def _rule_user_pattern_optimization(self, key_pattern: str, pattern: AccessPattern) -> Dict[str, Any]:
        """Rule: Optimize based on user access patterns"""
        if len(pattern.user_distribution) > 20:
            return {
                "action": "move_to_shared_tier",
                "pattern": key_pattern,
                "reason": f"High multi-user access: {len(pattern.user_distribution)} users"
            }
        return {}
    
    def _get_top_access_patterns(self) -> List[Dict[str, Any]]:
        """Get top access patterns for analytics"""
        if not self._analytics:
            return []
        
        patterns = []
        for key_pattern, pattern in self._analytics.access_patterns.items():
            patterns.append({
                "pattern": key_pattern,
                "access_frequency": pattern.access_frequency,
                "effectiveness_score": pattern.effectiveness_score,
                "user_count": len(pattern.user_distribution),
                "recommended_ttl": pattern.recommended_ttl
            })
        
        # Sort by access frequency
        patterns.sort(key=lambda x: x["access_frequency"], reverse=True)
        return patterns[:10]
    
    async def _periodic_optimization(self):
        """Periodic cache optimization task"""
        while self._background_tasks_running:
            try:
                await asyncio.sleep(300)  # Run every 5 minutes
                
                if self._analytics:
                    # Apply optimization rules
                    for key_pattern, pattern in self._analytics.access_patterns.items():
                        for rule in self._optimization_rules:
                            try:
                                optimization = rule(key_pattern, pattern)
                                if optimization:
                                    await self._apply_optimization(optimization)
                            except Exception as e:
                                self.logger.error(f"Error applying optimization rule: {e}")
                
            except Exception as e:
                self.logger.error(f"Error in periodic optimization: {e}")
    
    async def _cache_warming_task(self):
        """Cache warming based on predicted patterns"""
        while self._background_tasks_running:
            try:
                await asyncio.sleep(600)  # Run every 10 minutes
                
                # Implement intelligent cache warming based on patterns
                with self._warming_lock:
                    for key_pattern, warming_data in self._warming_candidates.items():
                        # Warm cache with predicted data
                        pass
                
            except Exception as e:
                self.logger.error(f"Error in cache warming task: {e}")
    
    async def _analytics_processor(self):
        """Process cache analytics in background"""
        while self._background_tasks_running:
            try:
                await asyncio.sleep(60)  # Process every minute
                
                if self._analytics:
                    # Clean up old access patterns
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                    
                    for pattern in self._analytics.access_patterns.values():
                        pattern.access_times = [
                            t for t in pattern.access_times if t > cutoff
                        ]
                
            except Exception as e:
                self.logger.error(f"Error in analytics processor: {e}")
    
    async def _apply_optimization(self, optimization: Dict[str, Any]):
        """Apply cache optimization action"""
        action = optimization.get("action")
        
        if action == "promote_to_l1":
            # Implementation would promote matching entries to L1
            pass
        elif action == "demote_to_l3":
            # Implementation would demote matching entries to L3
            pass
        elif action == "warm_cache":
            # Implementation would warm cache for pattern
            pass
        elif action == "move_to_shared_tier":
            # Implementation would move entries to shared tier
            pass
    
    async def health_check(self) -> Dict[str, Any]:
        """Check health of intelligent cache system"""
        base_health = await super().health_check()
        
        cache_health = {
            **base_health,
            "service": "intelligent_cache",
            "l1_cache": {
                "status": "healthy",
                "entry_count": len(self._l1_cache),
                "hit_rate": self._l1_stats.hit_rate,
                "size_utilization": len(self._l1_cache) / self._l1_max_size
            },
            "l2_cache": {
                "status": "healthy" if self._redis_client else "unavailable",
                "available": self._redis_client is not None,
                "hit_rate": self._l2_stats.hit_rate
            },
            "l3_cache": {
                "status": "not_implemented",
                "hit_rate": self._l3_stats.hit_rate
            },
            "analytics": {
                "enabled": self._enable_analytics,
                "patterns_tracked": len(self._analytics.access_patterns) if self._analytics else 0
            },
            "background_processing": self._background_tasks_running
        }
        
        # Determine overall status
        if len(self._l1_cache) > self._l1_max_size * 0.95:
            cache_health["status"] = "degraded"
            cache_health["warning"] = "L1 cache near capacity"
        elif not self._background_tasks_running:
            cache_health["status"] = "degraded"
            cache_health["warning"] = "Background processing not running"
        
        return cache_health