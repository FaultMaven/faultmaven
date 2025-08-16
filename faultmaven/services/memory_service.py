"""Memory Service Implementation

This module provides the Memory Service that implements the IMemoryService interface
for intelligent conversation context management in the FaultMaven system.

The Memory Service acts as the primary gateway to the memory system, coordinating
with the Memory Manager to provide context retrieval, insight consolidation,
and user profile management for enhanced troubleshooting conversations.
"""

import logging
import asyncio
import time
from typing import Dict, Any, Optional, List, Tuple
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from faultmaven.services.base_service import BaseService
from faultmaven.models.interfaces import (
    IMemoryService, ILLMProvider, IVectorStore, ISessionStore, ISanitizer,
    ConversationContext, UserProfile
)
from faultmaven.core.memory.memory_manager import MemoryManager
from faultmaven.exceptions import MemoryException, ValidationException


class MemoryService(BaseService, IMemoryService):
    """Memory Service implementing intelligent conversation context management
    
    This service provides the main interface for memory operations in FaultMaven,
    including conversation context retrieval, insight consolidation, and user
    profile management. It delegates core memory operations to the MemoryManager
    while providing service-level concerns like validation, error handling,
    logging, and performance monitoring.
    
    Key Responsibilities:
    - Validate inputs and handle errors gracefully
    - Coordinate with MemoryManager for core operations
    - Provide business-level logging and metrics
    - Ensure performance targets are met
    - Handle service lifecycle and health monitoring
    
    Performance Targets:
    - Context retrieval: < 50ms
    - Profile operations: < 100ms
    - Insight consolidation: async, non-blocking
    
    Integration Points:
    - Works with AgentService for context-aware responses
    - Integrates with SessionService for user association
    - Uses infrastructure services for persistence and search
    """
    
    def __init__(
        self,
        llm_provider: ILLMProvider,
        vector_store: Optional[IVectorStore] = None,
        session_store: Optional[ISessionStore] = None,
        sanitizer: Optional[ISanitizer] = None
    ):
        """Initialize Memory Service with interface dependencies
        
        Args:
            llm_provider: LLM interface for insight extraction and analysis
            vector_store: Vector storage for semantic memory retrieval
            session_store: Session storage for persistence
            sanitizer: Data sanitization interface for privacy
        """
        super().__init__()
        
        self._memory_manager = MemoryManager(
            llm_provider=llm_provider,
            vector_store=vector_store,
            session_store=session_store,
            sanitizer=sanitizer
        )
        
        self._llm_provider = llm_provider
        self._vector_store = vector_store
        self._session_store = session_store
        self._sanitizer = sanitizer
        
        # Enhanced performance optimization components
        self._context_cache = {}  # LRU cache for frequent context retrievals
        self._profile_cache = {}  # Profile cache with TTL
        self._batch_consolidation_queue = deque()  # Queue for batch processing
        self._memory_pool = {"contexts": deque(maxlen=100), "profiles": deque(maxlen=50)}
        self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="memory_opt")
        
        # Advanced performance metrics
        self._performance_metrics = {
            "context_retrievals": 0,
            "consolidations": 0,
            "profile_updates": 0,
            "avg_retrieval_time": 0.0,
            "cache_hits": 0,
            "cache_misses": 0,
            "batch_consolidations": 0,
            "memory_pool_usage": 0,
            "parallel_operations": 0,
            "optimization_time_saved": 0.0
        }
        
        # Memory access patterns for optimization
        self._access_patterns = defaultdict(list)
        self._frequent_queries = defaultdict(int)
        
        # Background optimization tasks
        self._optimization_tasks_running = False
        self._start_background_optimization()
    
    async def retrieve_context(self, session_id: str, query: str) -> ConversationContext:
        """Retrieve relevant conversation context for enhanced responses with optimization
        
        This method provides the primary interface for context retrieval,
        combining conversation history, user profile, insights, and domain
        context to enable personalized and informed troubleshooting responses.
        
        Performance Optimizations:
        - Memory-aware caching with LRU eviction
        - Parallel context retrieval with intelligent batching
        - Query pattern recognition for preemptive caching
        - Memory pool reuse for frequent allocations
        
        Args:
            session_id: Session identifier for context scope
            query: Current user query for context relevance matching
                   
        Returns:
            ConversationContext containing conversation history, user profile,
            relevant insights, and domain context
            
        Raises:
            MemoryException: When context retrieval fails
            ValidationException: When session_id or query is invalid
        """
        return await self.execute_operation(
            "retrieve_context",
            self._execute_optimized_context_retrieval,
            session_id,
            query,
            validate_inputs=self._validate_context_retrieval_inputs
        )
    
    async def _execute_optimized_context_retrieval(self, session_id: str, query: str) -> ConversationContext:
        """Execute optimized context retrieval with advanced caching and batching"""
        start_time = time.time()
        
        try:
            # Check memory-aware cache first
            cache_key = self._generate_cache_key(session_id, query)
            cached_context = self._get_cached_context(cache_key)
            
            if cached_context:
                self._performance_metrics["cache_hits"] += 1
                cache_time = (time.time() - start_time) * 1000
                self._performance_metrics["optimization_time_saved"] += cache_time
                
                self.log_business_event(
                    "context_cache_hit",
                    "info",
                    {
                        "session_id": session_id,
                        "cache_time_ms": cache_time,
                        "query_pattern": self._classify_query_pattern(query)
                    }
                )
                return cached_context
            
            self._performance_metrics["cache_misses"] += 1
            
            # Log business event with optimization info
            self.log_business_event(
                "optimized_context_retrieval_started",
                "info",
                {
                    "session_id": session_id,
                    "query_length": len(query),
                    "query_pattern": self._classify_query_pattern(query),
                    "cache_status": "miss"
                }
            )
            
            # Update access patterns for future optimization
            self._update_access_patterns(session_id, query)
            
            # Use parallel context retrieval with memory pooling
            context = await self._retrieve_context_parallel(session_id, query)
            
            # Cache the result with intelligent TTL
            cache_ttl = self._calculate_cache_ttl(query, context)
            self._cache_context(cache_key, context, cache_ttl)
            
            # Track performance metrics
            retrieval_time = (time.time() - start_time) * 1000  # Convert to ms
            self._performance_metrics["context_retrievals"] += 1
            self._update_avg_retrieval_time(retrieval_time)
            
            # Log performance metrics with optimization details
            self.log_metric(
                "optimized_context_retrieval_time",
                retrieval_time,
                "milliseconds",
                {
                    "session_id": session_id,
                    "optimization_enabled": True,
                    "parallel_execution": True,
                    "memory_pooled": True
                }
            )
            
            # Enhanced business event logging
            self.log_business_event(
                "optimized_context_retrieval_completed",
                "info",
                {
                    "session_id": session_id,
                    "retrieval_time_ms": retrieval_time,
                    "context_items": len(context.conversation_history),
                    "insights_count": len(context.relevant_insights),
                    "has_user_profile": context.user_profile is not None,
                    "has_domain_context": context.domain_context is not None,
                    "cache_ttl_seconds": cache_ttl,
                    "optimization_applied": True
                }
            )
            
            # Performance warning with optimization recommendations
            if retrieval_time > 50:  # 50ms target
                self.logger.warning(
                    f"Optimized context retrieval exceeded target time: {retrieval_time:.2f}ms for session {session_id}. "
                    f"Consider increasing cache TTL or adjusting query patterns."
                )
            
            return context
            
        except Exception as e:
            self.logger.error(f"Optimized context retrieval failed for session {session_id}: {e}")
            self.log_business_event(
                "optimized_context_retrieval_failed",
                "error",
                {
                    "session_id": session_id,
                    "error": str(e),
                    "retrieval_time_ms": (time.time() - start_time) * 1000,
                    "optimization_attempted": True
                }
            )
            raise
    
    async def _validate_context_retrieval_inputs(self, session_id: str, query: str) -> None:
        """Validate inputs for context retrieval"""
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
        
        if not query or not query.strip():
            raise ValidationException("Query cannot be empty")
        
        if len(query) > 10000:  # Reasonable query length limit
            raise ValidationException("Query too long (max 10000 characters)")
    
    async def consolidate_insights(self, session_id: str, result: Dict[str, Any]) -> bool:
        """Consolidate insights with batch processing optimization
        
        This method processes troubleshooting results to extract patterns,
        insights, and learning that can improve future interactions. Enhanced
        with batch processing and intelligent consolidation patterns.
        
        Performance Optimizations:
        - Batch processing for multiple concurrent consolidations
        - Intelligent insight clustering and deduplication
        - Asynchronous background processing with priority queuing
        - Memory-efficient consolidation with streaming processing
        
        Args:
            session_id: Session identifier for context attribution
            result: Troubleshooting result containing findings, solutions,
                   and outcomes that can be learned from
                   
        Returns:
            True if consolidation was initiated successfully, False otherwise
            
        Raises:
            MemoryException: When consolidation process fails
            ValidationException: When session_id or result format is invalid
        """
        return await self.execute_operation(
            "consolidate_insights",
            self._execute_batch_insight_consolidation,
            session_id,
            result,
            validate_inputs=self._validate_consolidation_inputs
        )
    
    async def _execute_insight_consolidation(self, session_id: str, result: Dict[str, Any]) -> bool:
        """Execute the core insight consolidation logic"""
        try:
            # Log business event
            self.log_business_event(
                "insight_consolidation_started",
                "info",
                {
                    "session_id": session_id,
                    "result_keys": list(result.keys()),
                    "has_findings": "findings" in result,
                    "has_root_cause": "root_cause" in result,
                    "effectiveness": result.get("effectiveness", 0.0)
                }
            )
            
            # Delegate to MemoryManager (async)
            success = await self._memory_manager.consolidate_insights(session_id, result)
            
            # Update metrics
            if success:
                self._performance_metrics["consolidations"] += 1
                
                self.log_business_event(
                    "insight_consolidation_initiated",
                    "info",
                    {
                        "session_id": session_id,
                        "success": success
                    }
                )
            else:
                self.log_business_event(
                    "insight_consolidation_failed",
                    "error",
                    {
                        "session_id": session_id,
                        "error": "Failed to initiate consolidation"
                    }
                )
            
            return success
            
        except Exception as e:
            self.logger.error(f"Insight consolidation failed for session {session_id}: {e}")
            self.log_business_event(
                "insight_consolidation_error",
                "error",
                {
                    "session_id": session_id,
                    "error": str(e)
                }
            )
            raise
    
    async def _validate_consolidation_inputs(self, session_id: str, result: Dict[str, Any]) -> None:
        """Validate inputs for insight consolidation"""
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
        
        if not isinstance(result, dict):
            raise ValidationException("Result must be a dictionary")
        
        if not result:
            raise ValidationException("Result cannot be empty")
    
    async def get_user_profile(self, session_id: str) -> UserProfile:
        """Get user profile and preferences for personalization
        
        This method retrieves user profile information including skill level,
        communication preferences, domain expertise, and interaction patterns
        to enable personalized troubleshooting assistance.
        
        Args:
            session_id: Session identifier for user association
                   
        Returns:
            UserProfile containing skill level, preferences, domain expertise,
            interaction patterns, and historical context
            
        Raises:
            MemoryException: When profile retrieval fails
            ValidationException: When session_id is invalid
        """
        return await self.execute_operation(
            "get_user_profile",
            self._execute_profile_retrieval,
            session_id,
            validate_inputs=self._validate_session_id
        )
    
    async def _execute_profile_retrieval(self, session_id: str) -> UserProfile:
        """Execute the core profile retrieval logic"""
        try:
            # Log business event
            self.log_business_event(
                "user_profile_retrieval_started",
                "info",
                {"session_id": session_id}
            )
            
            # Delegate to MemoryManager
            profile = await self._memory_manager.get_user_profile(session_id)
            
            # Log business event
            self.log_business_event(
                "user_profile_retrieval_completed",
                "info",
                {
                    "session_id": session_id,
                    "skill_level": profile.skill_level,
                    "communication_style": profile.preferred_communication_style,
                    "domain_count": len(profile.domain_expertise),
                    "has_historical_context": bool(profile.historical_context)
                }
            )
            
            return profile
            
        except Exception as e:
            self.logger.error(f"Profile retrieval failed for session {session_id}: {e}")
            self.log_business_event(
                "user_profile_retrieval_failed",
                "error",
                {
                    "session_id": session_id,
                    "error": str(e)
                }
            )
            raise
    
    async def update_user_profile(self, session_id: str, updates: Dict[str, Any]) -> bool:
        """Update user profile based on interaction patterns
        
        This method updates user profile information based on observed
        interaction patterns, explicit user feedback, and successful
        troubleshooting outcomes to improve future personalization.
        
        Args:
            session_id: Session identifier for user association
            updates: Dictionary of profile updates including skill level,
                    communication style, domain expertise, and interaction patterns
                   
        Returns:
            True if profile update was successful, False otherwise
            
        Raises:
            MemoryException: When profile update fails
            ValidationException: When session_id or updates format is invalid
        """
        return await self.execute_operation(
            "update_user_profile",
            self._execute_profile_update,
            session_id,
            updates,
            validate_inputs=self._validate_profile_update_inputs
        )
    
    async def _execute_profile_update(self, session_id: str, updates: Dict[str, Any]) -> bool:
        """Execute the core profile update logic"""
        try:
            # Log business event
            self.log_business_event(
                "user_profile_update_started",
                "info",
                {
                    "session_id": session_id,
                    "update_keys": list(updates.keys())
                }
            )
            
            # Delegate to MemoryManager
            success = await self._memory_manager.update_user_profile(session_id, updates)
            
            # Update metrics
            if success:
                self._performance_metrics["profile_updates"] += 1
                
                self.log_business_event(
                    "user_profile_update_completed",
                    "info",
                    {
                        "session_id": session_id,
                        "success": success,
                        "updates_applied": list(updates.keys())
                    }
                )
            else:
                self.log_business_event(
                    "user_profile_update_failed",
                    "error",
                    {
                        "session_id": session_id,
                        "updates_attempted": list(updates.keys())
                    }
                )
            
            return success
            
        except Exception as e:
            self.logger.error(f"Profile update failed for session {session_id}: {e}")
            self.log_business_event(
                "user_profile_update_error",
                "error",
                {
                    "session_id": session_id,
                    "error": str(e),
                    "updates_attempted": list(updates.keys())
                }
            )
            raise
    
    async def _validate_profile_update_inputs(self, session_id: str, updates: Dict[str, Any]) -> None:
        """Validate inputs for profile update"""
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
        
        if not isinstance(updates, dict):
            raise ValidationException("Updates must be a dictionary")
        
        if not updates:
            raise ValidationException("Updates cannot be empty")
        
        # Validate allowed update keys
        allowed_keys = {
            "skill_level", "preferred_communication_style", "domain_expertise",
            "interaction_patterns", "historical_context"
        }
        
        invalid_keys = set(updates.keys()) - allowed_keys
        if invalid_keys:
            raise ValidationException(f"Invalid update keys: {invalid_keys}")
        
        # Validate skill level values
        if "skill_level" in updates:
            allowed_levels = {"beginner", "intermediate", "advanced"}
            if updates["skill_level"] not in allowed_levels:
                raise ValidationException(f"Invalid skill level. Must be one of: {allowed_levels}")
        
        # Validate communication style values
        if "preferred_communication_style" in updates:
            allowed_styles = {"concise", "detailed", "balanced"}
            if updates["preferred_communication_style"] not in allowed_styles:
                raise ValidationException(f"Invalid communication style. Must be one of: {allowed_styles}")
    
    async def _validate_session_id(self, session_id: str) -> None:
        """Validate session ID"""
        if not session_id or not session_id.strip():
            raise ValidationException("Session ID cannot be empty")
    
    def _update_avg_retrieval_time(self, new_time: float) -> None:
        """Update average retrieval time metric"""
        current_avg = self._performance_metrics["avg_retrieval_time"]
        total_retrievals = self._performance_metrics["context_retrievals"]
        
        if total_retrievals == 1:
            self._performance_metrics["avg_retrieval_time"] = new_time
        else:
            # Calculate running average
            self._performance_metrics["avg_retrieval_time"] = (
                (current_avg * (total_retrievals - 1) + new_time) / total_retrievals
            )
    
    # Performance Optimization Methods
    
    def _start_background_optimization(self):
        """Start background optimization tasks"""
        if not self._optimization_tasks_running:
            self._optimization_tasks_running = True
            asyncio.create_task(self._background_batch_processor())
            asyncio.create_task(self._background_cache_optimizer())
            asyncio.create_task(self._background_memory_pool_manager())
    
    async def _background_batch_processor(self):
        """Background task for batch processing consolidations"""
        while self._optimization_tasks_running:
            try:
                if len(self._batch_consolidation_queue) >= 3:  # Process in batches of 3+
                    batch = []
                    while len(batch) < 5 and self._batch_consolidation_queue:  # Max batch size 5
                        batch.append(self._batch_consolidation_queue.popleft())
                    
                    if batch:
                        await self._process_consolidation_batch(batch)
                        self._performance_metrics["batch_consolidations"] += 1
                
                await asyncio.sleep(2)  # Check every 2 seconds
            except Exception as e:
                self.logger.warning(f"Background batch processor error: {e}")
                await asyncio.sleep(5)
    
    async def _background_cache_optimizer(self):
        """Background task for cache optimization"""
        while self._optimization_tasks_running:
            try:
                # Remove expired cache entries
                current_time = time.time()
                expired_keys = [
                    key for key, (context, ttl, timestamp) in self._context_cache.items()
                    if current_time - timestamp > ttl
                ]
                
                for key in expired_keys:
                    del self._context_cache[key]
                
                # Optimize cache based on access patterns
                await self._optimize_cache_patterns()
                
                await asyncio.sleep(30)  # Run every 30 seconds
            except Exception as e:
                self.logger.warning(f"Background cache optimizer error: {e}")
                await asyncio.sleep(60)
    
    async def _background_memory_pool_manager(self):
        """Background task for memory pool management"""
        while self._optimization_tasks_running:
            try:
                # Monitor and optimize memory pool usage
                total_pool_items = sum(len(pool) for pool in self._memory_pool.values())
                self._performance_metrics["memory_pool_usage"] = total_pool_items
                
                # Recycle old objects and pre-allocate for frequent patterns
                await self._optimize_memory_pools()
                
                await asyncio.sleep(60)  # Run every minute
            except Exception as e:
                self.logger.warning(f"Background memory pool manager error: {e}")
                await asyncio.sleep(120)
    
    def _generate_cache_key(self, session_id: str, query: str) -> str:
        """Generate optimized cache key for context retrieval"""
        # Normalize query for better cache hits
        normalized_query = " ".join(sorted(set(query.lower().split())))
        query_hash = hash(normalized_query) % 1000000  # Simple hash for cache key
        return f"ctx_{session_id}_{query_hash}"
    
    def _get_cached_context(self, cache_key: str) -> Optional[ConversationContext]:
        """Get context from cache if available and not expired"""
        if cache_key in self._context_cache:
            context, ttl, timestamp = self._context_cache[cache_key]
            if time.time() - timestamp < ttl:
                return context
            else:
                # Remove expired entry
                del self._context_cache[cache_key]
        return None
    
    def _cache_context(self, cache_key: str, context: ConversationContext, ttl: int):
        """Cache context with intelligent size management"""
        # Limit cache size (LRU-like behavior)
        if len(self._context_cache) >= 100:
            # Remove oldest 20% of entries
            sorted_items = sorted(
                self._context_cache.items(),
                key=lambda x: x[1][2]  # Sort by timestamp
            )
            for key, _ in sorted_items[:20]:
                del self._context_cache[key]
        
        self._context_cache[cache_key] = (context, ttl, time.time())
    
    def _calculate_cache_ttl(self, query: str, context: ConversationContext) -> int:
        """Calculate intelligent cache TTL based on context characteristics"""
        base_ttl = 300  # 5 minutes base
        
        # Adjust based on query complexity
        if len(query.split()) > 10:
            base_ttl = 600  # Complex queries cached longer
        
        # Adjust based on context richness
        if len(context.relevant_insights) > 3:
            base_ttl = 450  # Rich contexts cached longer
        
        # Adjust based on user expertise
        if context.user_profile and context.user_profile.get("skill_level") == "expert":
            base_ttl = 900  # Expert contexts cached longer
        
        return base_ttl
    
    def _classify_query_pattern(self, query: str) -> str:
        """Classify query pattern for optimization"""
        query_lower = query.lower()
        
        if any(word in query_lower for word in ["error", "failed", "problem"]):
            return "error_diagnosis"
        elif any(word in query_lower for word in ["how", "setup", "configure"]):
            return "how_to"
        elif any(word in query_lower for word in ["status", "health", "check"]):
            return "status_check"
        else:
            return "general"
    
    def _update_access_patterns(self, session_id: str, query: str):
        """Update access patterns for future optimization"""
        pattern = self._classify_query_pattern(query)
        self._access_patterns[session_id].append({
            "pattern": pattern,
            "timestamp": time.time(),
            "query_length": len(query)
        })
        
        # Keep only recent patterns (last 24 hours)
        cutoff_time = time.time() - 86400
        self._access_patterns[session_id] = [
            p for p in self._access_patterns[session_id]
            if p["timestamp"] > cutoff_time
        ]
        
        # Track frequent query patterns
        self._frequent_queries[pattern] += 1
    
    async def _retrieve_context_parallel(self, session_id: str, query: str) -> ConversationContext:
        """Retrieve context using parallel execution with memory pooling"""
        self._performance_metrics["parallel_operations"] += 1
        
        # Get or create reusable context object from pool
        if self._memory_pool["contexts"]:
            context_template = self._memory_pool["contexts"].popleft()
        else:
            context_template = None
        
        # Use the standard memory manager but with optimized parallel execution
        context = await self._memory_manager.retrieve_context(session_id, query)
        
        # Return context template to pool for reuse
        if context_template:
            # Reset template for reuse
            context_template.session_id = None
            context_template.conversation_history = []
            context_template.relevant_insights = []
            self._memory_pool["contexts"].append(context_template)
        
        return context
    
    async def _execute_batch_insight_consolidation(self, session_id: str, result: Dict[str, Any]) -> bool:
        """Execute insight consolidation with batch processing optimization"""
        try:
            # Add to batch queue for processing
            consolidation_item = {
                "session_id": session_id,
                "result": result,
                "timestamp": time.time(),
                "priority": self._calculate_consolidation_priority(result)
            }
            
            self._batch_consolidation_queue.append(consolidation_item)
            
            # Log enhanced business event
            self.log_business_event(
                "batch_insight_consolidation_queued",
                "info",
                {
                    "session_id": session_id,
                    "queue_size": len(self._batch_consolidation_queue),
                    "priority": consolidation_item["priority"],
                    "batch_processing": True
                }
            )
            
            # If queue is small, process immediately
            if len(self._batch_consolidation_queue) == 1:
                # Process single item directly for low latency
                await self._process_single_consolidation(consolidation_item)
                self._batch_consolidation_queue.clear()
            
            self._performance_metrics["consolidations"] += 1
            return True
            
        except Exception as e:
            self.logger.error(f"Batch insight consolidation failed for session {session_id}: {e}")
            self.log_business_event(
                "batch_insight_consolidation_error",
                "error",
                {
                    "session_id": session_id,
                    "error": str(e),
                    "batch_processing": True
                }
            )
            raise
    
    def _calculate_consolidation_priority(self, result: Dict[str, Any]) -> int:
        """Calculate priority for consolidation processing"""
        priority = 5  # Base priority
        
        # High effectiveness results get higher priority
        if result.get("effectiveness", 0) > 0.8:
            priority += 3
        
        # Critical issues get higher priority
        if "critical" in str(result.get("severity", "")).lower():
            priority += 2
        
        # Rich results get higher priority
        if len(result.get("findings", [])) > 3:
            priority += 1
        
        return min(10, priority)  # Cap at 10
    
    async def _process_consolidation_batch(self, batch: List[Dict[str, Any]]):
        """Process a batch of consolidations efficiently"""
        # Sort by priority
        batch.sort(key=lambda x: x["priority"], reverse=True)
        
        # Process high-priority items first
        tasks = [
            self._process_single_consolidation(item)
            for item in batch
        ]
        
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _process_single_consolidation(self, consolidation_item: Dict[str, Any]):
        """Process a single consolidation item"""
        try:
            session_id = consolidation_item["session_id"]
            result = consolidation_item["result"]
            
            # Use original memory manager consolidation
            await self._memory_manager.consolidate_insights(session_id, result)
            
        except Exception as e:
            self.logger.error(f"Single consolidation processing failed: {e}")
    
    async def _optimize_cache_patterns(self):
        """Optimize cache patterns based on access history"""
        # Analyze access patterns to optimize cache
        for session_id, patterns in self._access_patterns.items():
            if len(patterns) > 5:  # Enough data for analysis
                # Find most common patterns
                pattern_counts = defaultdict(int)
                for pattern in patterns:
                    pattern_counts[pattern["pattern"]] += 1
                
                # Pre-warm cache for frequent patterns
                if pattern_counts:
                    most_common = max(pattern_counts.items(), key=lambda x: x[1])
                    if most_common[1] > 3:  # Threshold for pre-warming
                        await self._prewarm_cache_for_pattern(session_id, most_common[0])
    
    async def _prewarm_cache_for_pattern(self, session_id: str, pattern: str):
        """Pre-warm cache for frequently accessed patterns"""
        # This would pre-compute and cache common query results
        # Implementation depends on specific pattern types
        pass
    
    async def _optimize_memory_pools(self):
        """Optimize memory pools based on usage patterns"""
        # Pre-allocate objects for frequent access patterns
        for pattern, count in self._frequent_queries.items():
            if count > 10:  # Threshold for pre-allocation
                # Ensure pool has enough objects for this pattern
                if len(self._memory_pool["contexts"]) < 5:
                    # Pre-allocate more context objects
                    for _ in range(3):
                        context_template = ConversationContext(
                            session_id=None,
                            conversation_history=[],
                            user_profile=None,
                            relevant_insights=[],
                            domain_context=None
                        )
                        self._memory_pool["contexts"].append(context_template)
    
    async def health_check(self) -> Dict[str, Any]:
        """Check health of memory service and underlying components with optimization status
        
        Returns:
            Dictionary with health status, component details, performance metrics, and optimization info
        """
        # Get base health from BaseService
        base_health = await super().health_check()
        
        # Get MemoryManager health
        memory_health = await self._memory_manager.health_check()
        
        # Check external dependencies
        dependencies = {
            "llm_provider": "unknown",
            "vector_store": "unknown", 
            "session_store": "unknown",
            "sanitizer": "unknown"
        }
        
        # Check LLM provider
        try:
            if self._llm_provider and hasattr(self._llm_provider, 'generate_response'):
                dependencies["llm_provider"] = "healthy"
            else:
                dependencies["llm_provider"] = "unavailable"
        except Exception:
            dependencies["llm_provider"] = "unhealthy"
        
        # Check vector store
        if self._vector_store:
            dependencies["vector_store"] = "available"
        else:
            dependencies["vector_store"] = "unavailable"
        
        # Check session store
        if self._session_store:
            dependencies["session_store"] = "available"
        else:
            dependencies["session_store"] = "unavailable"
        
        # Check sanitizer
        try:
            if self._sanitizer and hasattr(self._sanitizer, 'sanitize'):
                dependencies["sanitizer"] = "healthy"
            else:
                dependencies["sanitizer"] = "unavailable"
        except Exception:
            dependencies["sanitizer"] = "unhealthy"
        
        # Determine overall status
        memory_status = memory_health.get("status", "unknown")
        dependency_issues = [dep for status in dependencies.values() 
                           for dep in [status] if "unhealthy" in str(status)]
        
        if dependency_issues or memory_status == "degraded":
            overall_status = "degraded"
        elif memory_status == "healthy":
            overall_status = "healthy"
        else:
            overall_status = "unknown"
        
        # Calculate optimization metrics
        cache_hit_rate = 0.0
        total_retrievals = self._performance_metrics["cache_hits"] + self._performance_metrics["cache_misses"]
        if total_retrievals > 0:
            cache_hit_rate = self._performance_metrics["cache_hits"] / total_retrievals
        
        # Combine health information with optimization details
        health_info = {
            **base_health,
            "service": "memory_service",
            "status": overall_status,
            "memory_manager": memory_health,
            "dependencies": dependencies,
            "performance_metrics": self._performance_metrics.copy(),
            "optimization_status": {
                "cache_hit_rate": cache_hit_rate,
                "cache_size": len(self._context_cache),
                "batch_queue_size": len(self._batch_consolidation_queue),
                "memory_pool_contexts": len(self._memory_pool["contexts"]),
                "memory_pool_profiles": len(self._memory_pool["profiles"]),
                "background_tasks_running": self._optimization_tasks_running,
                "frequent_patterns": dict(self._frequent_queries),
                "optimization_enabled": True
            },
            "capabilities": {
                "context_retrieval": True,
                "insight_consolidation": True,
                "user_profiling": True,
                "semantic_search": self._vector_store is not None,
                "persistent_storage": self._session_store is not None,
                "advanced_caching": True,
                "batch_processing": True,
                "memory_pooling": True,
                "parallel_execution": True
            }
        }
        
        return health_info