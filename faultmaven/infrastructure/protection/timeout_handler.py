"""
Timeout handling for agent operations

Provides hierarchical timeout management to prevent runaway processes
and resource exhaustion.
"""

import asyncio
import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Callable, TypeVar, Coroutine
from dataclasses import dataclass, field

from ...models.protection import TimeoutConfig, TimeoutError

T = TypeVar('T')


@dataclass
class TimeoutContext:
    """Context information for a timeout operation"""
    operation_name: str
    timeout_duration: float
    start_time: float = field(default_factory=time.time)
    parent_context: Optional['TimeoutContext'] = None
    
    def elapsed(self) -> float:
        """Get elapsed time since operation started"""
        return time.time() - self.start_time
    
    def remaining(self) -> float:
        """Get remaining time before timeout"""
        return max(0, self.timeout_duration - self.elapsed())


class TimeoutHandler:
    """
    Hierarchical timeout management system
    
    Features:
    - Nested timeout contexts with inheritance
    - Operation-specific timeout limits
    - Graceful cleanup on timeout
    - Resource monitoring and alerts
    - Emergency shutdown mechanisms
    """
    
    def __init__(self, config: TimeoutConfig):
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Active timeout contexts
        self._active_contexts: Dict[str, TimeoutContext] = {}
        
        # Timeout statistics
        self._timeout_stats = {
            "total_operations": 0,
            "timeouts_triggered": 0,
            "avg_duration": 0.0,
            "max_duration": 0.0,
            "operations_by_type": {}
        }
        
        # Emergency shutdown tracking
        self._emergency_shutdowns = 0
        self._last_emergency_shutdown = 0
    
    @asynccontextmanager
    async def timeout_context(
        self,
        operation_name: str,
        timeout_duration: Optional[float] = None,
        parent_context: Optional[TimeoutContext] = None
    ):
        """
        Create a timeout context for an operation
        
        Args:
            operation_name: Name of the operation for logging
            timeout_duration: Timeout in seconds (uses config default if None)
            parent_context: Parent timeout context for hierarchical timeouts
            
        Yields:
            TimeoutContext: Context manager for the operation
        """
        
        # Determine timeout duration
        if timeout_duration is None:
            timeout_duration = self._get_default_timeout(operation_name)
        
        # Respect parent timeout limits
        if parent_context:
            parent_remaining = parent_context.remaining()
            timeout_duration = min(timeout_duration, parent_remaining)
        
        # Create context
        context = TimeoutContext(
            operation_name=operation_name,
            timeout_duration=timeout_duration,
            parent_context=parent_context
        )
        
        context_id = f"{operation_name}_{id(context)}"
        self._active_contexts[context_id] = context
        
        self.logger.debug(
            f"Starting timeout context: {operation_name}, "
            f"timeout={timeout_duration}s"
        )
        
        try:
            yield context
            
            # Record successful completion
            duration = context.elapsed()
            self._record_operation_stats(operation_name, duration, False)
            
            self.logger.debug(
                f"Completed operation: {operation_name}, "
                f"duration={duration:.3f}s"
            )
            
        except asyncio.TimeoutError:
            # Record timeout
            duration = context.elapsed()
            self._record_operation_stats(operation_name, duration, True)
            
            self.logger.warning(
                f"Operation timed out: {operation_name}, "
                f"duration={duration:.3f}s, limit={timeout_duration}s"
            )
            
            raise TimeoutError(
                operation=operation_name,
                timeout_duration=timeout_duration
            )
            
        finally:
            # Clean up context
            if context_id in self._active_contexts:
                del self._active_contexts[context_id]
    
    async def with_timeout(
        self,
        coro: Coroutine[Any, Any, T],
        operation_name: str,
        timeout_duration: Optional[float] = None,
        parent_context: Optional[TimeoutContext] = None
    ) -> T:
        """
        Execute a coroutine with timeout protection
        
        Args:
            coro: Coroutine to execute
            operation_name: Name for logging and statistics
            timeout_duration: Timeout in seconds
            parent_context: Parent timeout context
            
        Returns:
            Result of the coroutine
            
        Raises:
            TimeoutError: If operation times out
        """
        
        async with self.timeout_context(
            operation_name, timeout_duration, parent_context
        ) as context:
            try:
                return await asyncio.wait_for(coro, timeout=context.timeout_duration)
            except asyncio.TimeoutError:
                # Re-raise as our custom TimeoutError
                raise TimeoutError(
                    operation=operation_name,
                    timeout_duration=context.timeout_duration
                )
    
    async def with_agent_timeout(
        self,
        coro: Coroutine[Any, Any, T],
        operation_name: str = "agent_execution"
    ) -> T:
        """Execute agent operation with standard agent timeout"""
        return await self.with_timeout(
            coro,
            operation_name,
            self.config.agent_total
        )
    
    async def with_phase_timeout(
        self,
        coro: Coroutine[Any, Any, T],
        phase_name: str,
        parent_context: Optional[TimeoutContext] = None
    ) -> T:
        """Execute agent phase with phase timeout"""
        return await self.with_timeout(
            coro,
            f"agent_phase_{phase_name}",
            self.config.agent_phase,
            parent_context
        )
    
    async def with_llm_timeout(
        self,
        coro: Coroutine[Any, Any, T],
        parent_context: Optional[TimeoutContext] = None
    ) -> T:
        """Execute LLM call with LLM timeout"""
        return await self.with_timeout(
            coro,
            "llm_call",
            self.config.llm_call,
            parent_context
        )
    
    async def emergency_shutdown(
        self,
        operation_name: str,
        reason: str = "Resource exhaustion"
    ) -> None:
        """
        Trigger emergency shutdown for runaway operations
        
        This is a last resort when normal timeouts fail
        """
        current_time = time.time()
        self._emergency_shutdowns += 1
        self._last_emergency_shutdown = current_time
        
        self.logger.critical(
            f"EMERGENCY SHUTDOWN triggered for {operation_name}: {reason}"
        )
        
        # Cancel all active operations
        cancelled_count = 0
        for context_id, context in list(self._active_contexts.items()):
            if context.operation_name == operation_name or operation_name == "all":
                self.logger.warning(f"Force cancelling: {context.operation_name}")
                del self._active_contexts[context_id]
                cancelled_count += 1
        
        self.logger.critical(
            f"Emergency shutdown complete: cancelled {cancelled_count} operations"
        )
        
        # Alert if too many emergency shutdowns
        if self._emergency_shutdowns > 5:
            self.logger.critical(
                f"CRITICAL: {self._emergency_shutdowns} emergency shutdowns detected. "
                "System may be unstable."
            )
    
    def _get_default_timeout(self, operation_name: str) -> float:
        """Get default timeout for operation type"""
        
        if not self.config.enabled:
            return 300.0  # 5 minutes if timeouts disabled
        
        if "llm" in operation_name.lower():
            return self.config.llm_call
        elif "phase" in operation_name.lower():
            return self.config.agent_phase
        elif "agent" in operation_name.lower():
            return self.config.agent_total
        else:
            return self.config.agent_phase  # Default to phase timeout
    
    def _record_operation_stats(
        self,
        operation_name: str,
        duration: float,
        timed_out: bool
    ) -> None:
        """Record statistics for an operation"""
        
        self._timeout_stats["total_operations"] += 1
        
        if timed_out:
            self._timeout_stats["timeouts_triggered"] += 1
        
        # Update average duration
        total_ops = self._timeout_stats["total_operations"]
        current_avg = self._timeout_stats["avg_duration"]
        self._timeout_stats["avg_duration"] = (
            (current_avg * (total_ops - 1) + duration) / total_ops
        )
        
        # Update max duration
        if duration > self._timeout_stats["max_duration"]:
            self._timeout_stats["max_duration"] = duration
        
        # Update per-operation-type stats
        if operation_name not in self._timeout_stats["operations_by_type"]:
            self._timeout_stats["operations_by_type"][operation_name] = {
                "count": 0,
                "timeouts": 0,
                "avg_duration": 0.0,
                "max_duration": 0.0
            }
        
        op_stats = self._timeout_stats["operations_by_type"][operation_name]
        op_stats["count"] += 1
        
        if timed_out:
            op_stats["timeouts"] += 1
        
        # Update operation-specific averages
        op_count = op_stats["count"]
        current_op_avg = op_stats["avg_duration"]
        op_stats["avg_duration"] = (
            (current_op_avg * (op_count - 1) + duration) / op_count
        )
        
        if duration > op_stats["max_duration"]:
            op_stats["max_duration"] = duration
    
    def get_active_operations(self) -> Dict[str, Dict[str, Any]]:
        """Get information about currently active operations"""
        
        active_ops = {}
        current_time = time.time()
        
        for context_id, context in self._active_contexts.items():
            active_ops[context_id] = {
                "operation_name": context.operation_name,
                "elapsed": context.elapsed(),
                "remaining": context.remaining(),
                "timeout_duration": context.timeout_duration,
                "start_time": datetime.fromtimestamp(context.start_time, tz=timezone.utc).isoformat(),
                "has_parent": context.parent_context is not None
            }
        
        return active_ops
    
    def get_timeout_statistics(self) -> Dict[str, Any]:
        """Get comprehensive timeout statistics"""
        
        stats = self._timeout_stats.copy()
        
        # Calculate timeout rate
        total_ops = stats["total_operations"]
        if total_ops > 0:
            stats["timeout_rate"] = stats["timeouts_triggered"] / total_ops
        else:
            stats["timeout_rate"] = 0.0
        
        # Add emergency shutdown info
        stats["emergency_shutdowns"] = self._emergency_shutdowns
        stats["last_emergency_shutdown"] = self._last_emergency_shutdown
        
        # Add active operation count
        stats["active_operations"] = len(self._active_contexts)
        
        # Add configuration info
        stats["config"] = {
            "enabled": self.config.enabled,
            "agent_total": self.config.agent_total,
            "agent_phase": self.config.agent_phase,
            "llm_call": self.config.llm_call,
            "emergency_shutdown": self.config.emergency_shutdown
        }
        
        return stats
    
    async def health_check(self) -> Dict[str, Any]:
        """Perform health check on timeout system"""
        
        health_status = {
            "healthy": True,
            "issues": [],
            "warnings": []
        }
        
        # Check for excessive active operations
        active_count = len(self._active_contexts)
        if active_count > 50:
            health_status["healthy"] = False
            health_status["issues"].append(
                f"Too many active operations: {active_count}"
            )
        elif active_count > 20:
            health_status["warnings"].append(
                f"High number of active operations: {active_count}"
            )
        
        # Check timeout rate
        stats = self.get_timeout_statistics()
        timeout_rate = stats["timeout_rate"]
        
        if timeout_rate > 0.2:  # More than 20% timeouts
            health_status["healthy"] = False
            health_status["issues"].append(
                f"High timeout rate: {timeout_rate:.1%}"
            )
        elif timeout_rate > 0.1:  # More than 10% timeouts
            health_status["warnings"].append(
                f"Elevated timeout rate: {timeout_rate:.1%}"
            )
        
        # Check for recent emergency shutdowns
        if self._emergency_shutdowns > 0:
            recent_threshold = time.time() - 3600  # Last hour
            if self._last_emergency_shutdown > recent_threshold:
                health_status["healthy"] = False
                health_status["issues"].append(
                    "Recent emergency shutdown detected"
                )
        
        # Add statistics
        health_status.update(stats)
        
        return health_status