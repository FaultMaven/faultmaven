"""tracing.py

Purpose: Observability configuration

Requirements:
--------------------------------------------------------------------------------
• Initialize Comet Opik tracing
• Create @trace decorator
• Integrate Prometheus metrics

Key Components:
--------------------------------------------------------------------------------
  def init_opik_tracing():
  def trace(name: str):

Technology Stack:
--------------------------------------------------------------------------------
Comet Opik SDK, prometheus-client

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Cost-Efficiency: Use semantic caching
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import asyncio
import logging
import time
import functools
from typing import Callable, Optional
import os

# Prometheus metrics
try:
    from prometheus_client import Counter, Histogram, Gauge
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logging.warning("Prometheus client not available")

# Comet Opik tracing
try:
    import opik
    OPIK_AVAILABLE = True
except ImportError:
    OPIK_AVAILABLE = False
    logging.warning("Comet Opik not available")


# Prometheus metrics
if PROMETHEUS_AVAILABLE:
    # Request counters
    REQUEST_COUNTER = Counter(
        'faultmaven_requests_total',
        'Total number of requests',
        ['endpoint', 'method', 'status']
    )
    
    # Request duration histogram
    REQUEST_DURATION = Histogram(
        'faultmaven_request_duration_seconds',
        'Request duration in seconds',
        ['endpoint', 'method']
    )
    
    # Active sessions gauge
    ACTIVE_SESSIONS = Gauge(
        'faultmaven_active_sessions',
        'Number of active sessions'
    )
    
    # LLM request metrics
    LLM_REQUEST_COUNTER = Counter(
        'faultmaven_llm_requests_total',
        'Total number of LLM requests',
        ['provider', 'model', 'status']
    )
    
    LLM_REQUEST_DURATION = Histogram(
        'faultmaven_llm_request_duration_seconds',
        'LLM request duration in seconds',
        ['provider', 'model']
    )
    
    # Generic function metrics
    GENERIC_FUNCTION_DURATION = Histogram(
        'faultmaven_function_duration_seconds',
        'Generic function duration in seconds',
        ['function_name', 'status']
    )


def init_opik_tracing(api_key: Optional[str] = None, project_name: str = "faultmaven"):
    """
    Initialize Comet Opik tracing
    
    Args:
        api_key: Comet API key (optional, can be set via environment)
        project_name: Project name for tracing
    """
    if not OPIK_AVAILABLE:
        logging.warning(
            "Comet Opik not available, skipping tracing initialization"
        )
        return
    
    try:
        # Get API key from environment if not provided
        if not api_key:
            api_key = os.getenv('COMET_API_KEY')
        
        if not api_key:
            logging.warning(
                "No Comet API key provided, tracing will be disabled"
            )
            return
        
        # Initialize Opik
        opik.init(
            api_key=api_key,
            project_name=project_name,
            workspace=os.getenv('COMET_WORKSPACE', 'default')
        )
        
        logging.info("Comet Opik tracing initialized successfully")
        
    except Exception as e:
        logging.error(f"Failed to initialize Comet Opik tracing: {e}")


def trace(name: str, tags: Optional[dict] = None):
    """
    Decorator to trace function calls
    
    Args:
        name: Name for the trace span
        tags: Optional tags for the span
        
    Returns:
        Decorated function
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            start_time = time.time()
            
            # Create span if Opik is available
            span = None
            if OPIK_AVAILABLE:
                try:
                    span = opik.Span(name=name, tags=tags or {})
                    span.start()
                except Exception as e:
                    logging.warning(f"Failed to create Opik span: {e}")
            
            try:
                # Execute the function
                result = func(*args, **kwargs)
                
                # Record success metrics
                duration = time.time() - start_time
                _record_metrics(name, duration, "success")
                
                return result
                
            except Exception as e:
                # Record error metrics
                duration = time.time() - start_time
                _record_metrics(name, duration, "error")
                
                # Log error
                logging.error(
                    f"Function {name} failed after {duration:.3f}s: {e}"
                )
                raise
                
            finally:
                # End span if created
                if span and OPIK_AVAILABLE:
                    try:
                        span.end()
                    except Exception as e:
                        logging.warning(f"Failed to end Opik span: {e}")
        
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()
            
            # Create span if Opik is available
            span = None
            if OPIK_AVAILABLE:
                try:
                    span = opik.Span(name=name, tags=tags or {})
                    span.start()
                except Exception as e:
                    logging.warning(f"Failed to create Opik span: {e}")
            
            try:
                # Execute the async function
                result = await func(*args, **kwargs)
                
                # Record success metrics
                duration = time.time() - start_time
                _record_metrics(name, duration, "success")
                
                return result
                
            except Exception as e:
                # Record error metrics
                duration = time.time() - start_time
                _record_metrics(name, duration, "error")
                
                # Log error
                logging.error(
                    f"Async function {name} failed after {duration:.3f}s: {e}"
                )
                raise
                
            finally:
                # End span if created
                if span and OPIK_AVAILABLE:
                    try:
                        span.end()
                    except Exception as e:
                        logging.warning(f"Failed to end Opik span: {e}")
        
        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return wrapper
    
    return decorator


def _record_metrics(function_name: str, duration: float, status: str):
    """
    Record metrics for function calls
    
    Args:
        function_name: Name of the function
        duration: Duration in seconds
        status: Success or error status
    """
    if not PROMETHEUS_AVAILABLE:
        return
    
    try:
        # Determine metric type based on function name
        if function_name.startswith('llm_'):
            # LLM metrics
            provider = (
                function_name.split('_')[1] if '_' in function_name else 'unknown'
            )
            model = (
                function_name.split('_')[2] 
                if len(function_name.split('_')) > 2 
                else 'unknown'
            )
            
            LLM_REQUEST_COUNTER.labels(
                provider=provider,
                model=model,
                status=status
            ).inc()
            
            LLM_REQUEST_DURATION.labels(
                provider=provider,
                model=model
            ).observe(duration)
            
        elif function_name.startswith('api_'):
            # API metrics
            endpoint = function_name.replace('api_', '')
            method = 'POST'  # Default, could be extracted from function name
            
            REQUEST_COUNTER.labels(
                endpoint=endpoint,
                method=method,
                status=status
            ).inc()
            
            REQUEST_DURATION.labels(
                endpoint=endpoint,
                method=method
            ).observe(duration)
            
        else:
            # Generic function metrics
            GENERIC_FUNCTION_DURATION.labels(
                function_name=function_name,
                status=status
            ).observe(duration)
            
    except Exception as e:
        logging.warning(f"Failed to record metrics: {e}")


def create_span(name: str, tags: Optional[dict] = None):
    """
    Context manager for creating spans
    
    Args:
        name: Name for the span
        tags: Optional tags for the span
        
    Returns:
        Span context manager
    """
    if not OPIK_AVAILABLE:
        # Return a dummy context manager
        class DummySpan:
            def __enter__(self):
                return self
            
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
        
        return DummySpan()
    
    try:
        return opik.Span(name=name, tags=tags or {})
    except Exception as e:
        logging.warning(f"Failed to create span: {e}")
        return DummySpan()


def set_global_tags(tags: dict):
    """
    Set global tags for all spans
    
    Args:
        tags: Dictionary of tags to set globally
    """
    if not OPIK_AVAILABLE:
        logging.warning("Comet Opik not available, cannot set global tags")
        return
    
    try:
        opik.set_global_tags(tags)
        logging.info(f"Set global tags: {tags}")
    except Exception as e:
        logging.error(f"Failed to set global tags: {e}")


def record_exception(exception: Exception, tags: Optional[dict] = None):
    """
    Record an exception in tracing
    
    Args:
        exception: Exception to record
        tags: Optional tags for the exception
    """
    if not OPIK_AVAILABLE:
        return
    
    try:
        opik.record_exception(exception, tags=tags or {})
    except Exception as e:
        logging.warning(f"Failed to record exception: {e}")