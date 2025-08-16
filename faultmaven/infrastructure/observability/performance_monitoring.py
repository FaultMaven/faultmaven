"""Performance Monitoring Decorators and Context Managers

This module provides comprehensive performance monitoring decorators and context
managers specifically designed for FaultMaven Phase 2 intelligent troubleshooting
services including memory, planning, knowledge, orchestration, and agent services.

Key Features:
- Service-specific performance monitoring decorators
- Automatic metrics collection with minimal overhead
- Context-aware performance tracking
- Integration with MetricsCollector for advanced analytics
- Async and sync operation support
- Error tracking and performance correlation
- Cache performance monitoring
- User pattern analysis integration

Performance Overhead Target: < 2ms per monitored operation
"""

import asyncio
import functools
import logging
import time
from contextlib import contextmanager, asynccontextmanager
from typing import Dict, Any, Optional, Callable, List, Union, Tuple
from datetime import datetime
import inspect

from faultmaven.infrastructure.observability.metrics_collector import MetricsCollector
from faultmaven.models.interfaces import ITracer


class PerformanceMonitor:
    """Performance monitoring coordinator for FaultMaven Phase 2 services
    
    This class provides the coordination layer for all performance monitoring
    activities, integrating with the metrics collector and providing
    service-specific monitoring capabilities.
    """
    
    def __init__(
        self,
        metrics_collector: Optional[MetricsCollector] = None,
        tracer: Optional[ITracer] = None,
        enable_user_patterns: bool = True,
        enable_cache_monitoring: bool = True
    ):
        """Initialize performance monitor
        
        Args:
            metrics_collector: Metrics collection service
            tracer: Distributed tracing service
            enable_user_patterns: Whether to collect user interaction patterns
            enable_cache_monitoring: Whether to monitor cache performance
        """
        self._metrics_collector = metrics_collector
        self._tracer = tracer
        self._enable_user_patterns = enable_user_patterns
        self._enable_cache_monitoring = enable_cache_monitoring
        self._logger = logging.getLogger(__name__)
        
        # Service-specific configuration
        self._service_configs = {
            "memory_service": {
                "operations": {
                    "retrieve_context": {"target_ms": 50, "critical_ms": 100},
                    "consolidate_insights": {"target_ms": 200, "critical_ms": 500},
                    "get_user_profile": {"target_ms": 100, "critical_ms": 200},
                    "update_user_profile": {"target_ms": 100, "critical_ms": 200}
                },
                "track_patterns": True,
                "cache_enabled": True
            },
            "planning_service": {
                "operations": {
                    "generate_strategy": {"target_ms": 300, "critical_ms": 800},
                    "optimize_plan": {"target_ms": 200, "critical_ms": 600},
                    "adapt_strategy": {"target_ms": 150, "critical_ms": 400}
                },
                "track_patterns": True,
                "cache_enabled": True
            },
            "knowledge_service": {
                "operations": {
                    "search_documents": {"target_ms": 100, "critical_ms": 500},
                    "get_relevance_score": {"target_ms": 50, "critical_ms": 200},
                    "upload_document": {"target_ms": 1000, "critical_ms": 5000},
                    "enhanced_search": {"target_ms": 200, "critical_ms": 800}
                },
                "track_patterns": True,
                "cache_enabled": True
            },
            "orchestration_service": {
                "operations": {
                    "create_troubleshooting_workflow": {"target_ms": 500, "critical_ms": 2000},
                    "execute_workflow_step": {"target_ms": 3000, "critical_ms": 10000},
                    "get_workflow_status": {"target_ms": 100, "critical_ms": 500},
                    "pause_workflow": {"target_ms": 200, "critical_ms": 1000},
                    "resume_workflow": {"target_ms": 200, "critical_ms": 1000}
                },
                "track_patterns": True,
                "cache_enabled": False
            },
            "agent_service": {
                "operations": {
                    "process_query": {"target_ms": 2000, "critical_ms": 8000},
                    "calculate_confidence": {"target_ms": 100, "critical_ms": 400},
                    "generate_response": {"target_ms": 1500, "critical_ms": 6000}
                },
                "track_patterns": True,
                "cache_enabled": True
            },
            "enhanced_agent_service": {
                "operations": {
                    "intelligent_query_processing": {"target_ms": 3000, "critical_ms": 10000},
                    "context_aware_response": {"target_ms": 2000, "critical_ms": 8000},
                    "adaptive_learning": {"target_ms": 500, "critical_ms": 2000}
                },
                "track_patterns": True,
                "cache_enabled": True
            },
            "reasoning_service": {
                "operations": {
                    "analyze_problem": {"target_ms": 1000, "critical_ms": 4000},
                    "generate_hypotheses": {"target_ms": 800, "critical_ms": 3000},
                    "validate_solution": {"target_ms": 600, "critical_ms": 2500}
                },
                "track_patterns": True,
                "cache_enabled": True
            }
        }
    
    def get_service_config(self, service_name: str) -> Dict[str, Any]:
        """Get configuration for a specific service"""
        return self._service_configs.get(service_name, {
            "operations": {},
            "track_patterns": False,
            "cache_enabled": False
        })


# Global performance monitor instance
_performance_monitor: Optional[PerformanceMonitor] = None


def initialize_performance_monitoring(
    metrics_collector: Optional[MetricsCollector] = None,
    tracer: Optional[ITracer] = None,
    enable_user_patterns: bool = True,
    enable_cache_monitoring: bool = True
) -> PerformanceMonitor:
    """Initialize global performance monitoring
    
    Args:
        metrics_collector: Metrics collection service
        tracer: Distributed tracing service
        enable_user_patterns: Whether to collect user interaction patterns
        enable_cache_monitoring: Whether to monitor cache performance
        
    Returns:
        Configured performance monitor instance
    """
    global _performance_monitor
    _performance_monitor = PerformanceMonitor(
        metrics_collector=metrics_collector,
        tracer=tracer,
        enable_user_patterns=enable_user_patterns,
        enable_cache_monitoring=enable_cache_monitoring
    )
    return _performance_monitor


def get_performance_monitor() -> Optional[PerformanceMonitor]:
    """Get the global performance monitor instance"""
    return _performance_monitor


def monitor_service_operation(
    service: str,
    operation: Optional[str] = None,
    include_args: bool = False,
    include_result: bool = False,
    track_user_patterns: bool = False,
    cache_key_extractor: Optional[Callable] = None
):
    """Decorator for comprehensive service operation monitoring
    
    This decorator provides complete performance monitoring for service operations
    including timing, error tracking, cache performance, and user pattern analysis.
    
    Args:
        service: Name of the service being monitored
        operation: Operation name (auto-detected if None)
        include_args: Whether to include argument metadata
        include_result: Whether to include result metadata
        track_user_patterns: Whether to track user interaction patterns
        cache_key_extractor: Function to extract cache key from arguments
        
    Examples:
        @monitor_service_operation("memory_service", "retrieve_context")
        async def retrieve_context(self, session_id: str, query: str):
            # Implementation here
            pass
            
        @monitor_service_operation(
            "knowledge_service",
            "search_documents",
            track_user_patterns=True,
            cache_key_extractor=lambda *args, **kwargs: f"search:{kwargs.get('query', '')}"
        )
        async def search_documents(self, query: str, **kwargs):
            # Implementation here
            pass
    """
    
    def decorator(func: Callable) -> Callable:
        # Auto-detect operation name if not provided
        op_name = operation or func.__name__
        
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            monitor = get_performance_monitor()
            if not monitor or not monitor._metrics_collector:
                # Performance monitoring not initialized, just execute function
                return await func(*args, **kwargs)
            
            start_time = time.time()
            error_occurred = False
            result = None
            
            # Extract context information
            session_id = None
            user_id = None
            
            # Try to extract session and user info from arguments
            try:
                # Check for session_id in kwargs or args
                if 'session_id' in kwargs:
                    session_id = kwargs['session_id']
                elif len(args) > 1 and hasattr(args[0], '__dict__'):
                    # Check if first arg after self has session_id
                    if hasattr(args[1], 'session_id'):
                        session_id = args[1].session_id
                
                if 'user_id' in kwargs:
                    user_id = kwargs['user_id']
                elif session_id and hasattr(args[0], '_session_service'):
                    # Try to get user_id from session service
                    try:
                        session = await args[0]._session_service.get_session(session_id)
                        if session:
                            user_id = getattr(session, 'user_id', None)
                    except:
                        pass
            except:
                pass
            
            # Prepare metadata
            metadata = {
                "operation": op_name,
                "has_session": session_id is not None,
                "has_user": user_id is not None,
                "arg_count": len(args),
                "kwarg_count": len(kwargs)
            }
            
            if include_args:
                # Include argument metadata (sanitized)
                metadata["args_metadata"] = _sanitize_args_metadata(args, kwargs)
            
            # Cache monitoring
            cache_key = None
            if cache_key_extractor and monitor._enable_cache_monitoring:
                try:
                    cache_key = cache_key_extractor(*args, **kwargs)
                    # Check if this might be a cache hit (simplified heuristic)
                    metadata["cache_key_provided"] = True
                except Exception as e:
                    monitor._logger.error(f"Failed to extract cache key: {e}")
            
            # Start tracing if available
            trace_context = None
            if monitor._tracer:
                try:
                    trace_context = monitor._tracer.trace(f"{service}.{op_name}")
                    trace_span = trace_context.__enter__()
                except Exception as e:
                    monitor._logger.error(f"Failed to start trace: {e}")
            
            try:
                # Execute the function
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)
                
                # Record successful operation
                execution_time = (time.time() - start_time) * 1000  # Convert to ms
                
                if include_result:
                    metadata["result_metadata"] = _sanitize_result_metadata(result)
                
                # Record primary performance metric
                monitor._metrics_collector.record_metric(
                    service=service,
                    operation=op_name,
                    value=execution_time,
                    unit="milliseconds",
                    metadata=metadata,
                    tags={"success": "true", "operation": op_name}
                )
                
                # Track user patterns if enabled
                if (track_user_patterns and monitor._enable_user_patterns and 
                    session_id and user_id):
                    _track_user_interaction_pattern(
                        monitor, session_id, user_id, service, op_name, 
                        execution_time, args, kwargs, result
                    )
                
                # Track cache performance if applicable
                if cache_key and monitor._enable_cache_monitoring:
                    # This would typically check if result came from cache
                    # For now, we'll record it as a cache operation
                    monitor._metrics_collector.record_cache_event(
                        service=service,
                        cache_key=cache_key,
                        event_type="operation",  # Would be "hit" or "miss" in real implementation
                        retrieval_time_ms=execution_time,
                        metadata={"operation": op_name}
                    )
                
                return result
                
            except Exception as e:
                error_occurred = True
                execution_time = (time.time() - start_time) * 1000
                
                # Record error metric
                error_metadata = {
                    **metadata,
                    "error_type": type(e).__name__,
                    "error_message": str(e)[:200]  # Truncate long error messages
                }
                
                monitor._metrics_collector.record_metric(
                    service=service,
                    operation=f"{op_name}_error",
                    value=execution_time,
                    unit="milliseconds",
                    metadata=error_metadata,
                    tags={"success": "false", "error": type(e).__name__}
                )
                
                # Track error patterns
                if (track_user_patterns and monitor._enable_user_patterns and 
                    session_id and user_id):
                    _track_error_pattern(
                        monitor, session_id, user_id, service, op_name,
                        execution_time, e, args, kwargs
                    )
                
                raise
                
            finally:
                # Close trace context
                if trace_context:
                    try:
                        trace_context.__exit__(None, None, None)
                    except Exception as e:
                        monitor._logger.error(f"Failed to close trace: {e}")
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            monitor = get_performance_monitor()
            if not monitor or not monitor._metrics_collector:
                # Performance monitoring not initialized, just execute function
                return func(*args, **kwargs)
            
            start_time = time.time()
            error_occurred = False
            result = None
            
            # Extract context information (similar to async version)
            session_id = None
            user_id = None
            
            try:
                if 'session_id' in kwargs:
                    session_id = kwargs['session_id']
                if 'user_id' in kwargs:
                    user_id = kwargs['user_id']
            except:
                pass
            
            # Prepare metadata
            metadata = {
                "operation": op_name,
                "has_session": session_id is not None,
                "has_user": user_id is not None,
                "arg_count": len(args),
                "kwarg_count": len(kwargs)
            }
            
            if include_args:
                metadata["args_metadata"] = _sanitize_args_metadata(args, kwargs)
            
            # Cache monitoring
            cache_key = None
            if cache_key_extractor and monitor._enable_cache_monitoring:
                try:
                    cache_key = cache_key_extractor(*args, **kwargs)
                    metadata["cache_key_provided"] = True
                except Exception as e:
                    monitor._logger.error(f"Failed to extract cache key: {e}")
            
            try:
                # Execute the function
                result = func(*args, **kwargs)
                
                # Record successful operation
                execution_time = (time.time() - start_time) * 1000
                
                if include_result:
                    metadata["result_metadata"] = _sanitize_result_metadata(result)
                
                # Record primary performance metric
                monitor._metrics_collector.record_metric(
                    service=service,
                    operation=op_name,
                    value=execution_time,
                    unit="milliseconds",
                    metadata=metadata,
                    tags={"success": "true", "operation": op_name}
                )
                
                return result
                
            except Exception as e:
                error_occurred = True
                execution_time = (time.time() - start_time) * 1000
                
                # Record error metric
                error_metadata = {
                    **metadata,
                    "error_type": type(e).__name__,
                    "error_message": str(e)[:200]
                }
                
                monitor._metrics_collector.record_metric(
                    service=service,
                    operation=f"{op_name}_error",
                    value=execution_time,
                    unit="milliseconds",
                    metadata=error_metadata,
                    tags={"success": "false", "error": type(e).__name__}
                )
                
                raise
        
        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator


def monitor_memory_service(operation: Optional[str] = None):
    """Decorator specifically for memory service operations"""
    return monitor_service_operation(
        service="memory_service",
        operation=operation,
        track_user_patterns=True,
        include_args=True
    )


def monitor_planning_service(operation: Optional[str] = None):
    """Decorator specifically for planning service operations"""
    return monitor_service_operation(
        service="planning_service", 
        operation=operation,
        track_user_patterns=True,
        include_args=True
    )


def monitor_knowledge_service(
    operation: Optional[str] = None,
    cache_key_extractor: Optional[Callable] = None
):
    """Decorator specifically for knowledge service operations"""
    return monitor_service_operation(
        service="knowledge_service",
        operation=operation,
        track_user_patterns=True,
        cache_key_extractor=cache_key_extractor,
        include_args=True,
        include_result=True
    )


def monitor_orchestration_service(operation: Optional[str] = None):
    """Decorator specifically for orchestration service operations"""
    return monitor_service_operation(
        service="orchestration_service",
        operation=operation,
        track_user_patterns=True,
        include_args=True,
        include_result=True
    )


def monitor_agent_service(
    operation: Optional[str] = None,
    cache_key_extractor: Optional[Callable] = None
):
    """Decorator specifically for agent service operations"""
    return monitor_service_operation(
        service="agent_service",
        operation=operation,
        track_user_patterns=True,
        cache_key_extractor=cache_key_extractor,
        include_args=True
    )


def monitor_enhanced_agent_service(operation: Optional[str] = None):
    """Decorator specifically for enhanced agent service operations"""
    return monitor_service_operation(
        service="enhanced_agent_service",
        operation=operation,
        track_user_patterns=True,
        include_args=True,
        include_result=True
    )


@contextmanager
def measure_workflow_step(
    workflow_id: str,
    step_name: str,
    phase: str,
    step_number: int,
    expected_findings: int = 0,
    expected_knowledge_items: int = 0
):
    """Context manager for measuring workflow step performance
    
    Args:
        workflow_id: Workflow identifier
        step_name: Name of the step being measured
        phase: Troubleshooting phase
        step_number: Step number in workflow
        expected_findings: Expected number of findings
        expected_knowledge_items: Expected knowledge items to be retrieved
    """
    monitor = get_performance_monitor()
    start_time = time.time()
    success = True
    findings_count = 0
    knowledge_items_retrieved = 0
    confidence_score = None
    
    try:
        yield {
            "set_findings_count": lambda count: locals().update(findings_count=count),
            "set_knowledge_items": lambda count: locals().update(knowledge_items_retrieved=count),
            "set_confidence_score": lambda score: locals().update(confidence_score=score)
        }
        
    except Exception as e:
        success = False
        raise
        
    finally:
        execution_time = (time.time() - start_time) * 1000
        
        if monitor and monitor._metrics_collector:
            monitor._metrics_collector.record_workflow_metrics(
                workflow_id=workflow_id,
                phase=phase,
                step_number=step_number,
                execution_time_ms=execution_time,
                success=success,
                findings_count=findings_count,
                knowledge_items_retrieved=knowledge_items_retrieved,
                confidence_score=confidence_score,
                metadata={
                    "step_name": step_name,
                    "expected_findings": expected_findings,
                    "expected_knowledge_items": expected_knowledge_items
                }
            )


@asynccontextmanager
async def measure_async_workflow_step(
    workflow_id: str,
    step_name: str,
    phase: str,
    step_number: int,
    expected_findings: int = 0,
    expected_knowledge_items: int = 0
):
    """Async context manager for measuring workflow step performance"""
    monitor = get_performance_monitor()
    start_time = time.time()
    success = True
    findings_count = 0
    knowledge_items_retrieved = 0
    confidence_score = None
    
    step_context = {
        "findings_count": 0,
        "knowledge_items_retrieved": 0,
        "confidence_score": None
    }
    
    try:
        yield step_context
        findings_count = step_context["findings_count"]
        knowledge_items_retrieved = step_context["knowledge_items_retrieved"]
        confidence_score = step_context["confidence_score"]
        
    except Exception as e:
        success = False
        raise
        
    finally:
        execution_time = (time.time() - start_time) * 1000
        
        if monitor and monitor._metrics_collector:
            monitor._metrics_collector.record_workflow_metrics(
                workflow_id=workflow_id,
                phase=phase,
                step_number=step_number,
                execution_time_ms=execution_time,
                success=success,
                findings_count=findings_count,
                knowledge_items_retrieved=knowledge_items_retrieved,
                confidence_score=confidence_score,
                metadata={
                    "step_name": step_name,
                    "expected_findings": expected_findings,
                    "expected_knowledge_items": expected_knowledge_items
                }
            )


def track_cache_performance(
    service: str,
    operation: str,
    cache_key_extractor: Callable
):
    """Decorator for tracking cache performance
    
    Args:
        service: Service name
        operation: Operation name
        cache_key_extractor: Function to extract cache key from arguments
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            monitor = get_performance_monitor()
            if not monitor or not monitor._metrics_collector or not monitor._enable_cache_monitoring:
                return await func(*args, **kwargs)
            
            try:
                cache_key = cache_key_extractor(*args, **kwargs)
            except Exception as e:
                monitor._logger.error(f"Failed to extract cache key: {e}")
                return await func(*args, **kwargs)
            
            # Check cache first (this would be implemented in the actual service)
            cache_hit = False  # This would be determined by actual cache lookup
            start_time = time.time()
            
            try:
                result = await func(*args, **kwargs)
                retrieval_time = (time.time() - start_time) * 1000
                
                # Record cache event
                event_type = "hit" if cache_hit else "miss"
                monitor._metrics_collector.record_cache_event(
                    service=service,
                    cache_key=cache_key,
                    event_type=event_type,
                    retrieval_time_ms=retrieval_time,
                    metadata={
                        "operation": operation,
                        "cache_effectiveness": "high" if cache_hit else "low"
                    }
                )
                
                return result
                
            except Exception as e:
                retrieval_time = (time.time() - start_time) * 1000
                monitor._metrics_collector.record_cache_event(
                    service=service,
                    cache_key=cache_key,
                    event_type="error",
                    retrieval_time_ms=retrieval_time,
                    metadata={
                        "operation": operation,
                        "error": str(e)[:200]
                    }
                )
                raise
        
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            monitor = get_performance_monitor()
            if not monitor or not monitor._metrics_collector or not monitor._enable_cache_monitoring:
                return func(*args, **kwargs)
            
            try:
                cache_key = cache_key_extractor(*args, **kwargs)
            except Exception as e:
                monitor._logger.error(f"Failed to extract cache key: {e}")
                return func(*args, **kwargs)
            
            cache_hit = False  # This would be determined by actual cache lookup
            start_time = time.time()
            
            try:
                result = func(*args, **kwargs)
                retrieval_time = (time.time() - start_time) * 1000
                
                # Record cache event
                event_type = "hit" if cache_hit else "miss"
                monitor._metrics_collector.record_cache_event(
                    service=service,
                    cache_key=cache_key,
                    event_type=event_type,
                    retrieval_time_ms=retrieval_time,
                    metadata={
                        "operation": operation,
                        "cache_effectiveness": "high" if cache_hit else "low"
                    }
                )
                
                return result
                
            except Exception as e:
                retrieval_time = (time.time() - start_time) * 1000
                monitor._metrics_collector.record_cache_event(
                    service=service,
                    cache_key=cache_key,
                    event_type="error",
                    retrieval_time_ms=retrieval_time,
                    metadata={
                        "operation": operation,
                        "error": str(e)[:200]
                    }
                )
                raise
        
        # Return appropriate wrapper
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator


def _sanitize_args_metadata(args: tuple, kwargs: dict) -> Dict[str, Any]:
    """Sanitize argument metadata for logging"""
    metadata = {
        "arg_count": len(args),
        "kwarg_keys": list(kwargs.keys()),
        "has_session_id": "session_id" in kwargs,
        "has_user_id": "user_id" in kwargs
    }
    
    # Add safe argument information
    if "query" in kwargs:
        query_str = str(kwargs["query"])
        metadata["query_length"] = len(query_str)
        metadata["query_words"] = len(query_str.split())
    
    if "problem_description" in kwargs:
        desc_str = str(kwargs["problem_description"])
        metadata["description_length"] = len(desc_str)
        metadata["description_words"] = len(desc_str.split())
    
    return metadata


def _sanitize_result_metadata(result: Any) -> Dict[str, Any]:
    """Sanitize result metadata for logging"""
    if result is None:
        return {"type": "none"}
    
    metadata = {"type": type(result).__name__}
    
    if isinstance(result, dict):
        metadata["dict_keys"] = list(result.keys())
        metadata["dict_size"] = len(result)
    elif isinstance(result, (list, tuple)):
        metadata["collection_size"] = len(result)
    elif isinstance(result, str):
        metadata["string_length"] = len(result)
        metadata["word_count"] = len(result.split())
    
    return metadata


def _track_user_interaction_pattern(
    monitor: PerformanceMonitor,
    session_id: str,
    user_id: str,
    service: str,
    operation: str,
    execution_time: float,
    args: tuple,
    kwargs: dict,
    result: Any
) -> None:
    """Track user interaction patterns for analysis"""
    try:
        pattern_data = {
            "service": service,
            "operation": operation,
            "execution_time_ms": execution_time,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Extract operation-specific patterns
        if operation == "retrieve_context":
            query = kwargs.get("query", "")
            pattern_data.update({
                "query_length": len(query),
                "query_complexity": len(query.split()),
                "context_type": "retrieval"
            })
        elif operation == "search_documents":
            query = kwargs.get("query", "")
            pattern_data.update({
                "search_query_length": len(query),
                "search_type": kwargs.get("document_type", "all"),
                "has_filters": bool(kwargs.get("tags") or kwargs.get("document_type"))
            })
        elif "workflow" in operation:
            pattern_data.update({
                "workflow_operation": True,
                "has_context": bool(kwargs.get("context")),
                "priority_level": kwargs.get("priority_level", "medium")
            })
        
        # Calculate effectiveness score
        effectiveness_score = 1.0
        if execution_time > 5000:  # Very slow
            effectiveness_score = 0.3
        elif execution_time > 2000:  # Slow
            effectiveness_score = 0.6
        elif execution_time > 1000:  # Moderate
            effectiveness_score = 0.8
        
        monitor._metrics_collector.record_user_pattern(
            session_id=session_id,
            user_id=user_id,
            pattern_type=f"{service}_usage",
            pattern_data=pattern_data,
            effectiveness_score=effectiveness_score
        )
        
    except Exception as e:
        monitor._logger.error(f"Failed to track user interaction pattern: {e}")


def _track_error_pattern(
    monitor: PerformanceMonitor,
    session_id: str,
    user_id: str,
    service: str,
    operation: str,
    execution_time: float,
    error: Exception,
    args: tuple,
    kwargs: dict
) -> None:
    """Track error patterns for analysis"""
    try:
        pattern_data = {
            "service": service,
            "operation": operation,
            "execution_time_ms": execution_time,
            "error_type": type(error).__name__,
            "error_message": str(error)[:100],  # Truncated for privacy
            "timestamp": datetime.utcnow().isoformat()
        }
        
        monitor._metrics_collector.record_user_pattern(
            session_id=session_id,
            user_id=user_id,
            pattern_type="error_pattern",
            pattern_data=pattern_data,
            effectiveness_score=0.0  # Errors have zero effectiveness
        )
        
    except Exception as e:
        monitor._logger.error(f"Failed to track error pattern: {e}")


# Performance monitoring utilities

def get_service_performance_thresholds(service: str) -> Dict[str, Dict[str, int]]:
    """Get performance thresholds for a specific service"""
    monitor = get_performance_monitor()
    if monitor:
        return monitor.get_service_config(service).get("operations", {})
    return {}


def is_performance_monitoring_enabled() -> bool:
    """Check if performance monitoring is enabled"""
    monitor = get_performance_monitor()
    return monitor is not None and monitor._metrics_collector is not None


def get_monitoring_overhead_estimate() -> Dict[str, float]:
    """Get estimated monitoring overhead"""
    return {
        "per_operation_ms": 1.5,  # Estimated overhead per monitored operation
        "memory_mb": 2.0,         # Estimated memory overhead
        "cpu_percentage": 0.5     # Estimated CPU overhead percentage
    }