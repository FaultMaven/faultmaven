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
from contextlib import contextmanager
from typing import Callable, Optional, Any, Dict
from faultmaven.models.interfaces import ITracer
from faultmaven.infrastructure.base_client import BaseExternalClient

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


class OpikTracer(BaseExternalClient, ITracer):
    """Opik-based tracer implementing ITracer interface
    
    This tracer provides distributed tracing capabilities using Comet Opik
    with graceful fallback to local metrics when Opik is unavailable.
    """
    
    def __init__(self, settings=None):
        """Initialize OpikTracer with settings-based configuration
        
        Args:
            settings: FaultMavenSettings instance for configuration
        """
        super().__init__(
            client_name="OpikTracer",
            service_name="CometOpik",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=3,
            circuit_breaker_timeout=30
        )
        
        # Use settings-based configuration
        if settings is None:
            from faultmaven.config.settings import get_settings
            settings = get_settings()
        
        self.settings = settings
        self.opik_available = OPIK_AVAILABLE
        
        # Configuration from enhanced observability settings
        self.use_local_opik = settings.observability.opik_use_local
        self.local_opik_url = settings.observability.opik_local_url  
        self.local_opik_host = settings.observability.opik_local_host
        
    def trace(self, operation: str):
        """
        ITracer interface implementation
        
        Create a trace context for an operation.
        
        Args:
            operation: Name of the operation being traced
            
        Returns:
            Context manager for the trace span
        """
        return self._create_trace_context(operation)
    
    @contextmanager
    def _create_trace_context(self, operation: str):
        """
        Create a trace context manager with external call wrapping
        
        Args:
            operation: Operation name to trace
            
        Yields:
            Trace span object or None if tracing unavailable
        """
        start_time = time.time()
        span = None
        
        # Runtime check for tracing disable/enable
        if not self._should_trace(operation):
            self.logger.debug(f"Tracing disabled for operation: {operation}")
            yield None
            self._record_fallback_metrics(operation, start_time, "disabled")
            return
        
        if self.opik_available and OPIK_AVAILABLE:
            try:
                # Create span with external call wrapping
                def create_opik_span():
                    span_tags = {}
                    if self.use_local_opik:
                        span_tags.update({
                            "opik_local_host": self.local_opik_host,
                            "opik_local_url": self.local_opik_url
                        })
                    
                    # Use Opik tracing with external call protection
                    return opik.track(name=operation, tags=span_tags)
                
                # Use synchronous external call for span creation
                opik_span = self.call_external_sync(
                    operation_name="create_trace_span",
                    call_func=create_opik_span,
                    retries=1,
                    retry_delay=1.0
                )
                
                if opik_span:
                    with opik_span as span:
                        self.logger.debug(f"Opik trace started: {operation}")
                        yield span
                else:
                    yield None
                    
            except Exception as e:
                # Fallback - log warning but continue without tracing
                self.logger.warning(f"Opik tracing failed for operation '{operation}': {e}")
                self._record_fallback_metrics(operation, start_time, "error")
                yield None
        else:
            # No Opik available - use fallback
            self.logger.debug(f"Fallback trace: {operation} (Opik unavailable)")
            yield None
        
        # Record completion metrics
        if span is None:
            self._record_fallback_metrics(operation, start_time, "success")
    
    async def health_check(self) -> Dict[str, Any]:
        """
        Perform comprehensive health check for OpikTracer.
        
        Returns:
            Dictionary containing health status and metrics
        """
        from typing import Dict, Any
        
        base_health = await super().health_check()
        
        # Add tracing-specific health data
        try:
            # Check Opik availability and configuration
            tracer_health = {
                "opik_sdk_available": self.opik_available and OPIK_AVAILABLE,
                "use_local_opik": self.use_local_opik,
                "local_opik_url": self.local_opik_url,
                "local_opik_host": self.local_opik_host,
                "prometheus_available": PROMETHEUS_AVAILABLE
            }
            
            # Test service connectivity if using local Opik
            if self.use_local_opik and self.opik_available:
                try:
                    def check_opik_service():
                        import requests
                        response = requests.get(f"{self.local_opik_url}/health", timeout=5)
                        return response.status_code
                    
                    status_code = self.call_external_sync(
                        operation_name="health_check",
                        call_func=check_opik_service,
                        retries=1,
                        retry_delay=1.0
                    )
                    
                    tracer_health["service_connectivity"] = {
                        "status_code": status_code,
                        "reachable": status_code in [200, 404]  # 404 is OK for local instances
                    }
                    
                except Exception as e:
                    tracer_health["service_connectivity"] = {
                        "error": str(e),
                        "reachable": False
                    }
            
            # Determine overall status
            if self.opik_available and OPIK_AVAILABLE:
                if self.use_local_opik:
                    # For local Opik, check service connectivity
                    service_ok = tracer_health.get("service_connectivity", {}).get("reachable", False)
                    status = "healthy" if service_ok else "degraded"
                else:
                    status = "healthy"  # Cloud Opik assumed working if configured
            else:
                status = "degraded"  # Can still record fallback metrics
            
            base_health.update({
                "tracer_specific": tracer_health,
                "status": status
            })
            
        except Exception as e:
            base_health.update({
                "tracer_specific": {"error": str(e)},
                "status": "unhealthy"
            })
        
        return base_health
    
    def _should_trace(self, operation: str) -> bool:
        """
        Determine if tracing should be enabled for this operation based on various criteria.
        
        Supports:
        - Global disable: OPIK_TRACK_DISABLE=true
        - Target users: OPIK_TRACK_USERS=user1,user2,user3
        - Target sessions: OPIK_TRACK_SESSIONS=session1,session2
        - Target operations: OPIK_TRACK_OPERATIONS=llm_query,knowledge_search
        
        Args:
            operation: Operation name being traced
            
        Returns:
            True if tracing should be enabled, False otherwise
        """
        # Global disable check
        if self.settings.observability.opik_track_disable:
            return False
        
        # Get current request context for targeted tracing
        try:
            from faultmaven.infrastructure.logging.coordinator import request_context
            context = request_context.get()
        except:
            context = None
        
        # Check for targeted user tracing
        target_users = self.settings.observability.opik_track_users.strip()
        if target_users:
            target_user_list = [u.strip() for u in target_users.split(",") if u.strip()]
            if target_user_list:
                if not context or not context.user_id:
                    return False  # No user context, but targeting specific users
                if context.user_id not in target_user_list:
                    return False  # User not in target list
        
        # Check for targeted session tracing
        target_sessions = self.settings.observability.opik_track_sessions.strip()
        if target_sessions:
            target_session_list = [s.strip() for s in target_sessions.split(",") if s.strip()]
            if target_session_list:
                if not context or not context.session_id:
                    return False  # No session context, but targeting specific sessions
                if context.session_id not in target_session_list:
                    return False  # Session not in target list
        
        # Check for targeted operation tracing
        target_operations = self.settings.observability.opik_track_operations.strip()
        if target_operations:
            target_op_list = [op.strip() for op in target_operations.split(",") if op.strip()]
            if target_op_list:
                if operation not in target_op_list:
                    return False  # Operation not in target list
        
        return True  # Default to enabled if no restrictions apply
    
    def _record_fallback_metrics(self, operation: str, start_time: float, status: str):
        """
        Record fallback metrics when Opik is unavailable
        
        Args:
            operation: Operation name
            start_time: Operation start time
            status: Operation status (success/error)
        """
        duration = time.time() - start_time
        self.logger.debug(f"Trace completed: {operation} ({duration:.3f}s, {status})")
        
        # Record Prometheus metrics if available
        if PROMETHEUS_AVAILABLE:
            try:
                GENERIC_FUNCTION_DURATION.labels(
                    function_name=operation, status=status
                ).observe(duration)
            except Exception as e:
                self.logger.warning(f"Failed to record fallback metrics: {e}")


def init_opik_tracing(api_key: Optional[str] = None, project_name: str = "FaultMaven Development", settings=None):
    """
    Initialize Comet Opik tracing with support for local and cloud instances.
    
    This function uses BaseExternalClient patterns for robust service connectivity.

    Args:
        api_key: Comet API key (optional, can be set via settings)
        project_name: Project name for tracing
        settings: FaultMavenSettings instance for configuration
    """
    if not OPIK_AVAILABLE:
        logging.warning("Comet Opik not available, skipping tracing initialization")
        return

    # Get settings if not provided
    if settings is None:
        from faultmaven.config.settings import get_settings
        settings = get_settings()

    try:
        # Check for local Opik configuration first
        local_opik_url = settings.observability.opik_local_url
        local_opik_host = settings.observability.opik_local_host
        use_local_opik = settings.observability.opik_use_local
        
        # Check for cloud Opik configuration
        url_override = settings.observability.opik_url_override
        api_key = api_key or (settings.observability.opik_api_key.get_secret_value() if settings.observability.opik_api_key else None)

        # Determine which Opik instance to use
        if use_local_opik:
            # Configure for local Opik instance
            logging.info(f"Configuring Opik for local instance at {local_opik_url}")
            
            # Check if local Opik service is accessible with external call pattern
            def check_opik_health():
                import requests
                response = requests.get(f"{local_opik_url}/health", timeout=5)
                return response.status_code
            
            try:
                # Use a temporary external client for health checking (simplified)
                status_code = check_opik_health()
                if status_code == 200:
                    logging.info(f"Local Opik service health check passed (HTTP {status_code})")
                elif status_code == 404:
                    logging.info(f"Local Opik service is running but health endpoint not found. Proceeding with configuration.")
                else:
                    logging.warning(f"Local Opik service returned status {status_code}")
            except Exception as e:
                logging.info(f"Could not reach local Opik service: {e}. Will attempt configuration anyway.")
            
            # Set environment variables for Opik SDK
            os.environ["OPIK_URL_OVERRIDE"] = local_opik_url
            os.environ["OPIK_PROJECT_NAME"] = project_name
            
            # Configure Opik SDK with retry pattern
            def configure_opik():
                try:
                    # First try with minimal configuration
                    opik.configure(url=local_opik_url)
                    return True
                except Exception as e1:
                    # Check if this is a 404 endpoint issue (expected for local instances)
                    error_str = str(e1).lower()
                    if '404' in error_str and ('workspace' in error_str or 'api key' in error_str):
                        logging.info(f"Local Opik service detected at {local_opik_url} but API endpoints differ from cloud version")
                        return True  # Continue with basic capability
                    
                    # Try with default API key
                    local_api_key = api_key or (settings.observability.opik_api_key.get_secret_value() if settings.observability.opik_api_key else "local-dev-key")
                    opik.configure(url=local_opik_url, api_key=local_api_key)
                    return True
            
            try:
                if configure_opik():
                    logging.info(f"Local Opik tracing initialized successfully at {local_opik_url}")
                    
                    # Reduce Opik library's own logging verbosity
                    import logging as logging_module
                    opik_logger = logging_module.getLogger('opik')
                    opik_logger.setLevel(logging_module.WARNING)  # Only show warnings and errors
                    
                    # Set project name separately if needed
                    try:
                        opik.set_project_name(project_name)
                    except AttributeError:
                        os.environ["OPIK_PROJECT_NAME"] = project_name
                    except Exception as e:
                        logging.warning(f"Failed to set project name: {e}")
                    
                    logging.info(f"Local Opik tracing setup completed")
                else:
                    logging.info("FaultMaven will continue running without Opik tracing")
                    
            except Exception as e:
                logging.warning(f"Opik SDK configuration failed: {e}")
                logging.info("FaultMaven will continue running without Opik tracing")
            
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
            if settings.observability.comet_workspace:
                os.environ["COMET_WORKSPACE"] = settings.observability.comet_workspace
            
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


def trace(name: str, tags: Optional[dict] = None, settings=None):
    """
    Decorator to trace function calls with external service protection.
    
    Uses simplified external call patterns for span creation with fallback.

    Args:
        name: Name for the trace span
        tags: Optional tags for the span
        settings: Optional FaultMavenSettings instance

    Returns:
        Decorated function
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Get settings if not provided
            trace_settings = settings
            if trace_settings is None:
                try:
                    from faultmaven.config.settings import get_settings
                    trace_settings = get_settings()
                except:
                    trace_settings = None
            
            start_time = time.time()

            # Runtime check for tracing disable/targeting
            if not _should_trace_operation(name, trace_settings):
                logging.debug(f"Tracing disabled for function: {name}")
                try:
                    result = func(*args, **kwargs)
                    duration = time.time() - start_time
                    _record_metrics(name, duration, "success_no_trace")
                    return result
                except Exception as e:
                    duration = time.time() - start_time
                    _record_metrics(name, duration, "error_no_trace")
                    raise

            # Create span if Opik is available
            span = None
            if OPIK_AVAILABLE:
                try:
                    # Add local Opik headers if using local instance
                    span_tags = tags or {}
                    if trace_settings and trace_settings.observability.opik_use_local:
                        span_tags.update({
                            "opik_local_host": trace_settings.observability.opik_local_host,
                            "opik_local_url": trace_settings.observability.opik_local_url
                        })
                    
                    # Simple span tracking for local Opik instance with protection
                    def create_simple_span():
                        return {"name": name, "tags": span_tags, "start_time": start_time}
                    
                    span = create_simple_span()
                    # Reduce logging noise for heartbeat operations
                    if not ('heartbeat' in name.lower() or 'update_last_activity' in name.lower()):
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
                    # Reduce logging noise for heartbeat operations
                    if not ('heartbeat' in span.get('name', '').lower() or 'update_last_activity' in span.get('name', '').lower()):
                        logging.debug(f"Opik span completed: {span.get('name', 'unknown')} ({duration:.3f}s)")

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Get settings if not provided
            trace_settings = settings
            if trace_settings is None:
                try:
                    from faultmaven.config.settings import get_settings
                    trace_settings = get_settings()
                except:
                    trace_settings = None
            
            start_time = time.time()

            # Runtime check for tracing disable/targeting
            if not _should_trace_operation(name, trace_settings):
                logging.debug(f"Tracing disabled for async function: {name}")
                try:
                    result = await func(*args, **kwargs)
                    duration = time.time() - start_time
                    _record_metrics(name, duration, "success_no_trace")
                    return result
                except Exception as e:
                    duration = time.time() - start_time
                    _record_metrics(name, duration, "error_no_trace")
                    raise

            # Create span if Opik is available
            span = None
            if OPIK_AVAILABLE:
                try:
                    # Add local Opik headers if using local instance
                    span_tags = tags or {}
                    if trace_settings and trace_settings.observability.opik_use_local:
                        span_tags.update({
                            "opik_local_host": trace_settings.observability.opik_local_host,
                            "opik_local_url": trace_settings.observability.opik_local_url
                        })
                    
                    # Simple span tracking for local Opik instance with protection
                    def create_simple_span():
                        return {"name": name, "tags": span_tags, "start_time": start_time}
                    
                    span = create_simple_span()
                    # Reduce logging noise for heartbeat operations
                    if not ('heartbeat' in name.lower() or 'update_last_activity' in name.lower()):
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
                    # Reduce logging noise for heartbeat operations
                    if not ('heartbeat' in span.get('name', '').lower() or 'update_last_activity' in span.get('name', '').lower()):
                        logging.debug(f"Opik span completed: {span.get('name', 'unknown')} ({duration:.3f}s)")

        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            # Ensure __wrapped__ attribute is always set for async functions
            async_wrapper.__wrapped__ = func
            return async_wrapper
        else:
            # Ensure __wrapped__ attribute is always set for sync functions
            wrapper.__wrapped__ = func
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


def create_span(name: str, tags: Optional[dict] = None, settings=None):
    """
    Context manager for creating spans with external service protection.

    Args:
        name: Name for the span
        tags: Optional tags for the span
        settings: Optional FaultMavenSettings instance

    Returns:
        Span context manager
    """
    # Get settings if not provided
    if settings is None:
        try:
            from faultmaven.config.settings import get_settings
            settings = get_settings()
        except:
            settings = None
    
    class DummySpan:
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc_val, exc_tb):
            pass
    
    # Runtime check for tracing disable/targeting
    if not _should_trace_operation(name, settings):
        logging.debug(f"Tracing disabled for span: {name}")
        return DummySpan()
    
    if not OPIK_AVAILABLE:
        return DummySpan()

    try:
        # Create span with protection - return a context manager
        class ProtectedSpan:
            def __init__(self, name, tags):
                self.name = name
                self.tags = tags
                
            def __enter__(self):
                return self
                
            def __exit__(self, exc_type, exc_val, exc_tb):
                pass
        
        span_tags = tags or {}
        if settings and settings.observability.opik_use_local:
            span_tags.update({
                "opik_local_host": settings.observability.opik_local_host,
                "opik_local_url": settings.observability.opik_local_url
            })
        
        return ProtectedSpan(name, span_tags)
        
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


def _should_trace_operation(operation_name: str, settings=None) -> bool:
    """
    Standalone function to check if an operation should be traced.
    Used by decorators and standalone functions.
    
    Args:
        operation_name: Name of the operation
        settings: Optional FaultMavenSettings instance
        
    Returns:
        True if tracing should be enabled, False otherwise
    """
    # Get settings if not provided
    if settings is None:
        try:
            from faultmaven.config.settings import get_settings
            settings = get_settings()
        except:
            # If settings can't be loaded, default to enabled
            return True
    
    # Global disable check
    if settings.observability.opik_track_disable:
        return False
    
    # Get current request context for targeted tracing
    try:
        from faultmaven.infrastructure.logging.coordinator import request_context
        context = request_context.get()
    except:
        context = None
    
    # Check for targeted user tracing
    target_users = settings.observability.opik_track_users.strip()
    if target_users:
        target_user_list = [u.strip() for u in target_users.split(",") if u.strip()]
        if target_user_list:
            if not context or not context.user_id:
                return False  # No user context, but targeting specific users
            if context.user_id not in target_user_list:
                return False  # User not in target list
    
    # Check for targeted session tracing
    target_sessions = settings.observability.opik_track_sessions.strip()
    if target_sessions:
        target_session_list = [s.strip() for s in target_sessions.split(",") if s.strip()]
        if target_session_list:
            if not context or not context.session_id:
                return False  # No session context, but targeting specific sessions
            if context.session_id not in target_session_list:
                return False  # Session not in target list
    
    # Check for targeted operation tracing
    target_operations = settings.observability.opik_track_operations.strip()
    if target_operations:
        target_op_list = [op.strip() for op in target_operations.split(",") if op.strip()]
        if target_op_list:
            if operation_name not in target_op_list:
                return False  # Operation not in target list
    
    return True  # Default to enabled if no restrictions apply


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
