"""tracing.py

Purpose: Observability configuration

Requirements:
--------------------------------------------------------------------------------
• Create @trace decorator
• Integrate Prometheus metrics

The Opik SDK is configured, and imported, by ``opik_sdk`` alone (#2616 part c).

Key Components:
--------------------------------------------------------------------------------
  class OpikTracer:
  def trace(name: str):

Technology Stack:
--------------------------------------------------------------------------------
prometheus-client

Core Design Principles:
--------------------------------------------------------------------------------
• Privacy-First: Sanitize all external-bound data
• Resilience: Implement retries and fallbacks
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import asyncio
import functools
import logging
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.models.interfaces import ITracer

# Prometheus metrics
try:
    from prometheus_client import Counter, Gauge, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logging.warning("Prometheus client not available")

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


class _NoOpSpan:
    """No-op span yielded by OpikTracer for general (non-LLM) operations.

    LLM-specific Opik tracing is handled by @opik.track on the LLM router.
    Other callers (knowledge service, case ingestion) use OpikTracer for
    context scoping only and don't need real Opik spans.
    """

    def set_attribute(self, key: str, value):
        pass

    def set_status(self, status: str):
        pass


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
            circuit_breaker_timeout=30,
        )

        # Use settings-based configuration
        if settings is None:
            from faultmaven.config.settings import get_settings

            settings = get_settings()

        self.settings = settings
        # Imported here, not at module load: this module's importers must not
        # load the SDK, or the settings' .env, by importing it.
        from faultmaven.infrastructure.observability import opik_sdk

        self.opik_available = opik_sdk.OPIK_AVAILABLE

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
        Context manager for general (non-LLM) operation scoping.

        Yields a _NoOpSpan. LLM-specific Opik tracing is handled separately
        by @opik.track on the LLM router, which uses the native Opik SDK to
        create properly nested traces and spans with prompt/response data.

        This context manager is used by knowledge service, case data ingestion,
        and other general operations that need timing/scoping but not LLM spans.

        Args:
            operation: Operation name for metrics

        Yields:
            _NoOpSpan
        """
        start_time = time.time()
        try:
            yield _NoOpSpan()
            self._record_fallback_metrics(operation, start_time, "success")
        except Exception:
            self._record_fallback_metrics(operation, start_time, "error")
            raise

    async def health_check(self) -> Dict[str, Any]:
        """
        Perform comprehensive health check for OpikTracer.

        Returns:
            Dictionary containing health status and metrics
        """
        base_health = await super().health_check()

        # Add tracing-specific health data
        try:
            # Check Opik availability and configuration
            tracer_health = {
                "opik_sdk_available": self.opik_available,
                "use_local_opik": self.use_local_opik,
                "local_opik_url": self.local_opik_url,
                "local_opik_host": self.local_opik_host,
                "prometheus_available": PROMETHEUS_AVAILABLE,
            }

            # Test service connectivity if using local Opik
            if self.use_local_opik and self.opik_available:
                try:

                    def check_opik_service():
                        import requests

                        response = requests.get(
                            f"{self.local_opik_url}/health", timeout=5
                        )
                        return response.status_code

                    status_code = self.call_external_sync(
                        operation_name="health_check",
                        call_func=check_opik_service,
                        retries=1,
                        retry_delay=1.0,
                    )

                    tracer_health["service_connectivity"] = {
                        "status_code": status_code,
                        "reachable": status_code
                        in [200, 404],  # 404 is OK for local instances
                    }

                except Exception as e:
                    tracer_health["service_connectivity"] = {
                        "error": str(e),
                        "reachable": False,
                    }

            # Determine overall status
            if self.opik_available:
                if self.use_local_opik:
                    # For local Opik, check service connectivity
                    service_ok = tracer_health.get("service_connectivity", {}).get(
                        "reachable", False
                    )
                    status = "healthy" if service_ok else "degraded"
                else:
                    status = "healthy"  # Cloud Opik assumed working if configured
            else:
                status = "degraded"  # Can still record fallback metrics

            base_health.update({"tracer_specific": tracer_health, "status": status})

        except Exception as e:
            base_health.update(
                {"tracer_specific": {"error": str(e)}, "status": "unhealthy"}
            )

        return base_health

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


def trace(name: str, settings=None):
    """
    Decorator for local performance metrics (Prometheus counters/histograms).

    This decorator does NOT create Opik traces. LLM-specific Opik tracing is
    handled by @opik.track on the LLM router, which uses the native Opik SDK
    to create properly nested traces and spans with prompt/response data.

    Args:
        name: Name for the metric label
        settings: Optional FaultMavenSettings instance

    Returns:
        Decorated function
    """

    def decorator(func: Callable) -> Callable:
        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = await func(*args, **kwargs)
                    _record_metrics(name, time.time() - start_time, "success")
                    return result
                except Exception:
                    _record_metrics(name, time.time() - start_time, "error")
                    raise

            return async_wrapper
        else:

            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = func(*args, **kwargs)
                    _record_metrics(name, time.time() - start_time, "success")
                    return result
                except Exception:
                    _record_metrics(name, time.time() - start_time, "error")
                    raise

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
