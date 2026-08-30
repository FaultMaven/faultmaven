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
• Extensibility: Use interfaces for pluggable components
• Observability: Add tracing spans for key operations
"""

import asyncio
import functools
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional

from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.models.interfaces import ITracer
from faultmaven.utils.optional_dependency import module_is_usable

# Prometheus metrics
try:
    from prometheus_client import Counter, Gauge, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    logging.warning("Prometheus client not available")

# Comet Opik tracing
#
# A bare ``import opik`` succeeding is not proof the SDK is installed. Opik is
# optional (pyproject's ``[cloud]`` extra) and pip's uninstall removes a
# package's files but leaves its directories, so an environment that once had
# the extra keeps an empty ``site-packages/opik/`` tree — which PEP 420
# imports as a namespace package exposing none of the SDK. Trusting the import
# alone makes init_opik_tracing log "initialized" and health_check report
# ``opik_sdk_available: True`` against nothing. The sites that additionally
# from-import a symbol (llm/router.py, preprocessing/classifier.py,
# shims/observability.py) already fail correctly; the bare-import sites are
# the ones that need the check. A namespace package has ``__file__ is None``.
try:
    import opik

    # No `attr` deliberately. The symbols this module calls
    # (set_tracing_active, reset_tracing_to_config_default) are version-
    # sensitive, and probing one would turn an SDK rename into tracing
    # silently switching OFF — #1121's defect class inverted. `__file__`
    # cannot misfire that way. Callers whose symbol is stable (boto3.client,
    # tiktoken.get_encoding) do pass one.
    OPIK_AVAILABLE = module_is_usable(opik)
    if OPIK_AVAILABLE:
        logging.debug("Opik SDK loaded successfully")
    else:
        # Name the observation, not a presumed cause, and print the path —
        # it is the only thing that tells an operator which directory to
        # remove. An empty leftover tree is the common source but not the
        # only way a namespace package resolves.
        logging.warning(
            "Comet Opik not available: 'opik' resolved to a namespace package "
            "(no SDK) at %s",
            list(getattr(opik, "__path__", []) or ["<unknown>"]),
        )
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
                "opik_sdk_available": self.opik_available and OPIK_AVAILABLE,
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
            if self.opik_available and OPIK_AVAILABLE:
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


# Sentinel distinguishing "we have not written OPIK_TRACK_DISABLE" from "the
# operator had it unset", which are different states with different undos.
_TRACK_DISABLE_UNRECORDED = object()

# What OPIK_TRACK_DISABLE held before the disabled path overwrote it: either
# _TRACK_DISABLE_UNRECORDED (we never wrote it), None (the operator had it
# unset), or the operator's exact string. Only what WE wrote may be undone —
# an operator's explicit "off" is never overridden.
_track_disable_prior_value: Any = _TRACK_DISABLE_UNRECORDED


def _disable_sdk_tracing() -> None:
    """Flip the Opik SDK's own kill switches so disabled means disabled.

    The @opik.track call sites (llm/router.py, preprocessing/classifier.py)
    already gate per call when OPIK_ENABLED=false, but any other tracked code
    path would still lazily build an SDK client pointed at the SDK's default
    backend (Comet Cloud) — outbound requests and 401s from a self-hosted
    deployment (#1121). Two switches, belt and braces:

    - ``set_tracing_active(False)`` makes ``is_tracing_active()`` False
      process-wide, overriding any True the SDK may already have cached.
      Every @opik.track wrapper and ``opik_context.update_current_span``
      consults it per call and returns before any client is constructed.
    - ``OPIK_TRACK_DISABLE=true`` covers anything that re-reads OpikConfig
      from the environment (the SDK owns this env var; it is deliberately
      not a settings field).

    The pre-existing value is recorded so _enable_sdk_tracing can undo exactly
    this write and nothing else. Recorded only on the FIRST write: a second
    disabled init must not record our own "true" as if the operator set it.
    """
    global _track_disable_prior_value
    if _track_disable_prior_value is _TRACK_DISABLE_UNRECORDED:
        _track_disable_prior_value = os.environ.get("OPIK_TRACK_DISABLE")

    os.environ["OPIK_TRACK_DISABLE"] = "true"
    try:
        import opik as _opik

        _opik.set_tracing_active(False)
    except Exception as e:
        logging.debug(f"Could not flip Opik SDK tracing switch: {e}")


def _enable_sdk_tracing() -> None:
    """Undo our own kill switch, honouring anything the operator set.

    Both SDK switches are process-global with no restore path, so a disabled
    init leaves tracing off for everything that runs afterwards in the same
    process; an enabled init that follows one would otherwise log that tracing
    was initialized while silently tracing nothing.

    The undo is deliberately narrow. Forcing the switches on instead —
    ``os.environ.pop`` plus ``set_tracing_active(True)`` — also overrides an
    OPERATOR-set ``OPIK_TRACK_DISABLE=true``, which is a documented way to
    suppress spans while keeping a backend configured. That would make a
    documented knob silently inert (#1121's own defect class, moved to the
    other switch) and would RESUME tracing on upgrade for anyone relying on
    it — restarting the very egress this change exists to stop.

    So: restore exactly the value the disabled path overwrote (including its
    absence), then let ``reset_tracing_to_config_default()`` re-derive the flag
    from the environment. That clears a stale programmatic ``False`` while
    still reading an operator's ``OPIK_TRACK_DISABLE=true`` as off.
    """
    global _track_disable_prior_value
    if _track_disable_prior_value is not _TRACK_DISABLE_UNRECORDED:
        if _track_disable_prior_value is None:
            os.environ.pop("OPIK_TRACK_DISABLE", None)
        else:
            os.environ["OPIK_TRACK_DISABLE"] = _track_disable_prior_value
        _track_disable_prior_value = _TRACK_DISABLE_UNRECORDED

    try:
        import opik as _opik

        # Re-derives from the environment, so an operator's OPIK_TRACK_DISABLE
        # still wins. Never set_tracing_active(True) — that would override it.
        _opik.reset_tracing_to_config_default()
    except Exception as e:
        logging.debug(f"Could not reset Opik SDK tracing switch: {e}")


def init_opik_tracing(
    api_key: Optional[str] = None,
    project_name: str = "FaultMaven Development",
    settings=None,
):
    """
    Initialize Opik tracing by setting the environment variables that the
    Opik SDK's ``OpikConfig`` (pydantic-settings with ``env_prefix="opik_"``)
    reads at runtime.

    We intentionally do NOT call ``opik.configure()`` because:
      - It triggers interactive prompts (workspace confirmation, API key input)
      - It makes GET health-check requests that can hit the wrong URL
      - It writes to ~/.opik.config which persists stale state

    Instead we set the four env vars that OpikConfig reads directly:
      - OPIK_URL_OVERRIDE  (the full API base URL, e.g. https://www.comet.com/opik/api/)
      - OPIK_API_KEY
      - OPIK_WORKSPACE
      - OPIK_PROJECT_NAME

    The ``@opik.track`` decorator lazily creates its client on first
    invocation via ``OpikConfig``, so these env vars are picked up
    automatically — no explicit ``configure()`` call is needed.
    """
    if not OPIK_AVAILABLE:
        logging.warning("Opik SDK not installed, skipping tracing initialization")
        return

    if settings is None:
        from faultmaven.config.settings import get_settings

        settings = get_settings()

    if not settings.observability.opik_enabled:
        _disable_sdk_tracing()
        logging.info("Opik tracing disabled (OPIK_ENABLED=false)")
        return

    try:
        obs = settings.observability
        project = obs.opik_project_name or project_name

        if obs.opik_use_local:
            # Self-hosted Opik (local Docker or K8s)
            # Local Opik expects: {base}/api/  (no /opik prefix)
            url = obs.opik_local_url.rstrip("/") + "/api/"
            logging.info(f"Configuring Opik for self-hosted instance: {url}")
        elif obs.opik_url_override:
            # Cloud Opik (Comet) or custom endpoint
            url = obs.opik_url_override.rstrip("/") + "/"
            logging.info(f"Configuring Opik for cloud instance: {url}")
        else:
            # Without this the warning below would be a lie: the @opik.track
            # call sites are live (opik_enabled=True) and the SDK would fall
            # back to its Comet Cloud default instead of being disabled.
            _disable_sdk_tracing()
            logging.warning(
                "Opik enabled but no URL configured. "
                "Set OPIK_USE_LOCAL=true or OPIK_URL_OVERRIDE. Tracing will be disabled."
            )
            return

        # Resolve API key
        resolved_api_key = api_key or (
            obs.opik_api_key.get_secret_value() if obs.opik_api_key else None
        )

        # Resolve workspace (FaultMaven uses COMET_WORKSPACE; Opik SDK reads OPIK_WORKSPACE)
        workspace = obs.comet_workspace or "default"

        # Assert the SDK is actually tracing before configuring where to.
        # (Mirror of the disabled path's kill switch — see _enable_sdk_tracing.)
        _enable_sdk_tracing()

        # Set the env vars that OpikConfig reads (env_prefix="opik_")
        os.environ["OPIK_URL_OVERRIDE"] = url
        os.environ["OPIK_PROJECT_NAME"] = project
        os.environ["OPIK_WORKSPACE"] = workspace
        if resolved_api_key:
            os.environ["OPIK_API_KEY"] = resolved_api_key

        # Disable the SDK's connection monitor health-check ping.
        # The SDK pings /is-alive/ping every 10s but has a URL bug (urljoin
        # with absolute path strips /opik/api/ prefix).  The ping hits the
        # Comet homepage, gets 200 HTML, and creates log noise.  Traces are
        # sent to the correct URL regardless, so we disable the ping and
        # only log errors if actual trace POSTs fail.
        os.environ["OPIK_CONNECTION_MONITOR_PING_INTERVAL"] = "999999"

        # Also update the session config so already-created OpikConfig instances refresh
        try:
            import opik.config

            opik.config.update_session_config("url_override", url)
            opik.config.update_session_config("project_name", project)
            opik.config.update_session_config("workspace", workspace)
            opik.config.update_session_config(
                "connection_monitor_ping_interval", 999999
            )
            if resolved_api_key:
                opik.config.update_session_config("api_key", resolved_api_key)
        except Exception as e:
            logging.debug(f"Could not update Opik session config: {e}")

        logging.info(
            f"Opik tracing initialized: url={url}, project={project}, workspace={workspace}"
        )

    except Exception as e:
        logging.error(f"Failed to initialize Opik tracing: {e}")
        logging.info("Continuing without tracing...")


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
