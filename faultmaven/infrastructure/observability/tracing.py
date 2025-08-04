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
import functools
import logging
import os
import time
from typing import Callable, Optional

# Prometheus metrics
try:
    from prometheus_client import Counter, Gauge, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logging.warning("Prometheus client not available")

# Comet Opik tracing
try:
    import opik

    OPIK_AVAILABLE = True
    logging.info("Opik SDK loaded successfully")
except ImportError:
    OPIK_AVAILABLE = False
    logging.warning("Comet Opik not available")


# Prometheus metrics
if PROMETHEUS_AVAILABLE:
    # Request counters
    REQUEST_COUNTER = Counter(
        "faultmaven_requests_total",
        "Total number of requests",
        ["endpoint", "method", "status"],
    )

    # Request duration histogram
    REQUEST_DURATION = Histogram(
        "faultmaven_request_duration_seconds",
        "Request duration in seconds",
        ["endpoint", "method"],
    )

    # Active sessions gauge
    ACTIVE_SESSIONS = Gauge("faultmaven_active_sessions", "Number of active sessions")

    # LLM request metrics
    LLM_REQUEST_COUNTER = Counter(
        "faultmaven_llm_requests_total",
        "Total number of LLM requests",
        ["provider", "model", "status"],
    )

    LLM_REQUEST_DURATION = Histogram(
        "faultmaven_llm_request_duration_seconds",
        "LLM request duration in seconds",
        ["provider", "model"],
    )

    # Generic function metrics
    GENERIC_FUNCTION_DURATION = Histogram(
        "faultmaven_function_duration_seconds",
        "Generic function duration in seconds",
        ["function_name", "status"],
    )


def init_opik_tracing(api_key: Optional[str] = None, project_name: str = "FaultMaven Development"):
    """
    Initialize Comet Opik tracing with support for local and cloud instances

    Args:
        api_key: Comet API key (optional, can be set via environment)
        project_name: Project name for tracing
    """
    if not OPIK_AVAILABLE:
        logging.warning("Comet Opik not available, skipping tracing initialization")
        return

    try:
        # Check for local Opik configuration first
        local_opik_url = os.getenv("OPIK_LOCAL_URL", "http://192.168.0.112:30080")
        local_opik_host = os.getenv("OPIK_LOCAL_HOST", "opik-api.faultmaven.local")
        use_local_opik = os.getenv("OPIK_USE_LOCAL", "true").lower() == "true"
        
        # Check for cloud Opik configuration
        url_override = os.getenv("OPIK_URL_OVERRIDE")
        api_key = api_key or os.getenv("COMET_API_KEY")

        # Determine which Opik instance to use
        if use_local_opik:
            # Configure for local Opik instance
            logging.info(f"Configuring Opik for local instance at {local_opik_url}")
            
            # Check if local Opik service is accessible
            try:
                import requests
                response = requests.get(f"{local_opik_url}/health", timeout=5)
                if response.status_code == 404:
                    logging.info(f"Local Opik service is running but health endpoint not found. Proceeding with configuration.")
                elif response.status_code != 200:
                    logging.warning(f"Local Opik service returned status {response.status_code}")
            except Exception as e:
                logging.info(f"Could not reach local Opik service: {e}. Will attempt configuration anyway.")
            
            # Set environment variables for Opik SDK
            os.environ["OPIK_URL_OVERRIDE"] = local_opik_url
            os.environ["OPIK_PROJECT_NAME"] = project_name
            
            # For local Opik instances, handle connection gracefully
            try:
                # First try with minimal configuration
                opik.configure(url=local_opik_url)
                logging.info(f"Local Opik tracing initialized successfully at {local_opik_url}")
            except Exception as e1:
                # Check if it's a 404 error (service not ready/available)
                if "404" in str(e1):
                    logging.info(f"Local Opik service at {local_opik_url} not ready yet (404). Continuing without tracing.")
                    logging.info("Note: Make sure your local Opik service is running and accessible")
                else:
                    logging.debug(f"Minimal config failed: {e1}")
                    try:
                        # Try with default API key
                        local_api_key = api_key or os.getenv("OPIK_API_KEY", "local-dev-key")
                        opik.configure(url=local_opik_url, api_key=local_api_key)
                        logging.info(f"Local Opik tracing initialized with API key at {local_opik_url}")
                    except Exception as e2:
                        if "404" in str(e2):
                            logging.info(f"Local Opik service at {local_opik_url} not accessible (404). Continuing without tracing.")
                        else:
                            logging.debug(f"API key config failed: {e2}")
                        logging.info("FaultMaven will continue running without Opik tracing")
                        return
            
            # Set project name separately if needed
            try:
                opik.set_project_name(project_name)
            except AttributeError:
                # If set_project_name doesn't exist, set as environment variable
                os.environ["OPIK_PROJECT_NAME"] = project_name
            except Exception as e:
                logging.warning(f"Failed to set project name: {e}")
            
            logging.info(f"Local Opik tracing setup completed")
            
        elif url_override or api_key:
            # Configure for cloud Opik instance
            logging.info("Configuring Opik for cloud instance")
            
            config_params = {}

            if api_key:
                config_params["api_key"] = api_key
                
            if url_override:
                config_params["url"] = url_override

            opik.configure(**config_params)
            
            # Set project name and workspace as environment variables
            os.environ["OPIK_PROJECT_NAME"] = project_name
            if os.getenv("COMET_WORKSPACE"):
                os.environ["COMET_WORKSPACE"] = os.getenv("COMET_WORKSPACE", "default")
            
            logging.info("Cloud Opik tracing initialized successfully")
            
        else:
            logging.warning(
                "No Opik configuration found. Set OPIK_USE_LOCAL=true for local instance "
                "or provide COMET_API_KEY for cloud instance. Tracing will be disabled."
            )
            return

    except Exception as e:
        logging.error(f"Failed to initialize Opik tracing: {e}")
        logging.info("Continuing without tracing...")


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
                    # Add local Opik headers if using local instance
                    span_tags = tags or {}
                    if os.getenv("OPIK_USE_LOCAL", "true").lower() == "true":
                        span_tags.update({
                            "opik_local_host": os.getenv("OPIK_LOCAL_HOST", "opik-api.faultmaven.local"),
                            "opik_local_url": os.getenv("OPIK_LOCAL_URL", "http://192.168.0.112:30080")
                        })
                    
                    # Simple span tracking for local Opik instance  
                    span = {"name": name, "tags": span_tags, "start_time": start_time}
                    logging.debug(f"Opik span started: {name}")
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
                logging.error(f"Function {name} failed after {duration:.3f}s: {e}")
                raise

            finally:
                # Finalize span logging
                if span and OPIK_AVAILABLE:
                    duration = time.time() - start_time
                    logging.debug(f"Opik span completed: {span.get('name', 'unknown')} ({duration:.3f}s)")

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            start_time = time.time()

            # Create span if Opik is available
            span = None
            if OPIK_AVAILABLE:
                try:
                    # Add local Opik headers if using local instance
                    span_tags = tags or {}
                    if os.getenv("OPIK_USE_LOCAL", "true").lower() == "true":
                        span_tags.update({
                            "opik_local_host": os.getenv("OPIK_LOCAL_HOST", "opik-api.faultmaven.local"),
                            "opik_local_url": os.getenv("OPIK_LOCAL_URL", "http://192.168.0.112:30080")
                        })
                    
                    # Simple span tracking for local Opik instance  
                    span = {"name": name, "tags": span_tags, "start_time": start_time}
                    logging.debug(f"Opik span started: {name}")
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
                # Finalize span logging
                if span and OPIK_AVAILABLE:
                    duration = time.time() - start_time
                    logging.debug(f"Opik span completed: {span.get('name', 'unknown')} ({duration:.3f}s)")

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
        if function_name.startswith("llm_"):
            # LLM metrics
            provider = (
                function_name.split("_")[1] if "_" in function_name else "unknown"
            )
            model = (
                function_name.split("_")[2]
                if len(function_name.split("_")) > 2
                else "unknown"
            )

            LLM_REQUEST_COUNTER.labels(
                provider=provider, model=model, status=status
            ).inc()

            LLM_REQUEST_DURATION.labels(provider=provider, model=model).observe(
                duration
            )

        elif function_name.startswith("api_"):
            # API metrics
            endpoint = function_name.replace("api_", "")
            method = "POST"  # Default, could be extracted from function name

            REQUEST_COUNTER.labels(
                endpoint=endpoint, method=method, status=status
            ).inc()

            REQUEST_DURATION.labels(endpoint=endpoint, method=method).observe(duration)

        else:
            # Generic function metrics
            GENERIC_FUNCTION_DURATION.labels(
                function_name=function_name, status=status
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
        # Add local Opik configuration if using local instance
        span_tags = tags or {}
        if os.getenv("OPIK_USE_LOCAL", "true").lower() == "true":
            span_tags.update({
                "opik_local_host": os.getenv("OPIK_LOCAL_HOST", "opik-api.faultmaven.local"),
                "opik_local_url": os.getenv("OPIK_LOCAL_URL", "http://192.168.0.112:30080")
            })
        
        # Return a simple span dict for local Opik
        return {"name": name, "tags": span_tags}
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
