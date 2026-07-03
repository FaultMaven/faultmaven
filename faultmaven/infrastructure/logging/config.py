"""
FaultMaven Logging Configuration

Provides enhanced logging configuration using structlog with JSON formatting,
request context injection, deduplication, and OpenTelemetry integration.

Configuration is read from the unified settings system (faultmaven.config.settings)
at runtime, not at import time.
"""

import logging
from typing import Any, Dict, Optional

import structlog
from opentelemetry import trace


class LoggingConfig:
    """
    Configuration for logging system from unified settings.

    This class provides type-safe access to logging configuration values
    from the unified settings system. Configuration is read at runtime
    when methods are called, not at import time.

    All configuration comes from faultmaven.config.settings.LoggingSettings.
    """

    def __init__(self):
        """Initialize logging config by reading from settings."""
        self._load_from_settings()

    def _load_from_settings(self) -> None:
        """Load configuration from unified settings system."""
        from faultmaven.config.settings import get_settings

        settings = get_settings()

        self.LOG_LEVEL = settings.logging.level.value.upper()
        self.LOG_FORMAT = settings.logging.log_output_format.lower()
        self.LOG_DEDUPE = settings.logging.log_dedupe
        self.LOG_BUFFER_SIZE = settings.logging.log_buffer_size
        self.LOG_FLUSH_INTERVAL = settings.logging.log_flush_interval
        self.LOG_HUMAN_READABLE = settings.logging.log_human_readable

    def get_log_level(self) -> int:
        """
        Convert string log level to logging constant.

        Returns:
            Logging level constant (logging.DEBUG, logging.INFO, etc.)
        """
        levels = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "CRITICAL": logging.CRITICAL,
        }
        return levels.get(self.LOG_LEVEL, logging.INFO)


class FaultMavenLogger:
    """
    Enhanced logger configuration with deduplication and structured logging.

    This class configures structlog with processors for request context injection,
    deduplication, trace context, and JSON formatting. It ensures consistent
    log structure across all application components.
    """

    # Marker attribute stamped on the root handler we install, so a repeat
    # configure() removes only OUR handler by identity and leaves foreign
    # handlers (pytest's caplog, a server-installed root handler) untouched.
    _ROOT_HANDLER_MARKER = "_faultmaven_root_handler"

    def __init__(self, config: Optional["LoggingConfig"] = None):
        """Initialize the logger configuration.

        Args:
            config: Optional pre-built config; defaults to reading unified
                settings. Injectable so tests can pin format/level without
                mutating the process-wide settings singleton.
        """
        self.config = config or LoggingConfig()
        self.configure_structlog()

    def configure_structlog(self) -> None:
        """
        Configure structlog with comprehensive processors.

        Sets up a processor chain that handles:
        - Log level filtering
        - Logger name and level addition
        - Timestamp formatting
        - Exception information
        - Request context injection
        - Field deduplication
        - OpenTelemetry trace context
        - JSON output formatting
        """
        # Processors that BUILD the event dict, shared by native structlog logs
        # AND foreign stdlib LogRecords, so both render identically with their
        # fields. Rendering itself happens once, in the ProcessorFormatter below.
        shared_processors = [
            # Merge structlog contextvars (e.g. request_id bound by
            # RequestIdMiddleware) — first so later processors can override.
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            # Interpolate %-style positional args into the event message so
            # ``logger.info("env=%s", env)`` renders "env=prod", not a raw
            # "env=%s" plus a dangling positional_args field. No-ops when a log
            # has no positional args.
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            self.add_request_context,
            self.add_trace_context,
        ]
        if self.config.LOG_DEDUPE:
            shared_processors.append(self.deduplicate_fields)

        # Renderer: JSON for production, human-readable console for dev.
        if self.config.LOG_FORMAT == "console" or (
            self.config.LOG_FORMAT != "json" and self.config.LOG_HUMAN_READABLE
        ):
            renderer = structlog.dev.ConsoleRenderer()
        else:
            renderer = structlog.processors.JSONRenderer()

        # Native structlog loggers: filter + shared processors, then hand the
        # event to the stdlib ProcessorFormatter instead of rendering here.
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                *shared_processors,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        # One formatter renders BOTH structlog events and plain stdlib records.
        # For FOREIGN (stdlib) records — including third-party libs and every
        # ``logger.info(msg, extra={...})`` call in this codebase — the pre-chain
        # runs the shared processors PLUS ExtraAdder, so the ``extra`` fields are
        # merged onto the event dict and actually render (previously they were
        # silently dropped by ``basicConfig(format="%(message)s")``).
        formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                *shared_processors,
                structlog.stdlib.ExtraAdder(),
            ],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                structlog.processors.UnicodeDecoder(),
                renderer,
            ],
        )

        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        setattr(handler, self._ROOT_HANDLER_MARKER, True)

        root_logger = logging.getLogger()
        # Remove only a handler WE previously installed, by identity — never a
        # blanket handlers.clear(). Clearing would drop foreign root handlers,
        # most importantly pytest's caplog handler, silently breaking log
        # capture if this runs mid-test (the singleton can be re-initialized).
        for existing in list(root_logger.handlers):
            if getattr(existing, self._ROOT_HANDLER_MARKER, False):
                root_logger.removeHandler(existing)
        root_logger.addHandler(handler)

        # Lower the root level to our configured verbosity, but never RAISE it
        # above a level something else (e.g. pytest's caplog.at_level) has
        # already lowered it to — otherwise reconfiguration would suppress
        # records a caller explicitly asked to capture.
        configured_level = self.config.get_log_level()
        if root_logger.level == logging.NOTSET or root_logger.level > configured_level:
            root_logger.setLevel(configured_level)

    @staticmethod
    def add_request_context(
        logger, method_name: str, event_dict: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Add request context without duplication.

        This processor injects request-scoped context into log entries,
        ensuring consistent correlation tracking across all log messages
        within a request.

        Args:
            logger: Logger instance
            method_name: Log method name
            event_dict: Event dictionary to process

        Returns:
            Enhanced event dictionary with request context
        """
        # Import here to avoid circular imports
        from faultmaven.infrastructure.logging.coordinator import request_context

        ctx = request_context.get()
        if ctx:
            # Only add if not already present to prevent duplication
            if "correlation_id" not in event_dict:
                event_dict["correlation_id"] = ctx.correlation_id
            if "session_id" not in event_dict and ctx.session_id:
                event_dict["session_id"] = ctx.session_id
            if "user_id" not in event_dict and ctx.user_id:
                event_dict["user_id"] = ctx.user_id
            if "case_id" not in event_dict and ctx.case_id:
                event_dict["case_id"] = ctx.case_id
            if "agent_phase" not in event_dict and ctx.agent_phase:
                event_dict["agent_phase"] = ctx.agent_phase

        return event_dict

    @staticmethod
    def deduplicate_fields(
        logger, method_name: str, event_dict: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Remove duplicate fields from log entries.

        This processor ensures that each field appears only once in the log entry,
        preventing cluttered logs with repeated information.

        Args:
            logger: Logger instance
            method_name: Log method name
            event_dict: Event dictionary to process

        Returns:
            Deduplicated event dictionary
        """
        seen = set()
        deduped = {}

        for key, value in event_dict.items():
            if key not in seen:
                deduped[key] = value
                seen.add(key)

        return deduped

    @staticmethod
    def add_trace_context(
        logger, method_name: str, event_dict: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Add OpenTelemetry trace context to log entries.

        This processor injects distributed tracing information into logs,
        enabling correlation between logs and traces in observability systems.

        Args:
            logger: Logger instance
            method_name: Log method name
            event_dict: Event dictionary to process

        Returns:
            Event dictionary with trace context
        """
        span = trace.get_current_span()
        if span and span.is_recording():
            span_context = span.get_span_context()
            # Add trace information only if not already present
            if "trace_id" not in event_dict:
                event_dict["trace_id"] = format(span_context.trace_id, "032x")
            if "span_id" not in event_dict:
                event_dict["span_id"] = format(span_context.span_id, "016x")

        return event_dict


# Singleton configuration instance
_logger_config: Optional[FaultMavenLogger] = None


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Get a configured logger instance.

    Factory function that ensures consistent logger configuration across
    the application. Uses singleton pattern to avoid reconfiguring structlog
    multiple times.

    Args:
        name: Logger name, typically module or class name

    Returns:
        Configured structlog BoundLogger instance

    Example:
        >>> logger = get_logger(__name__)
        >>> logger.info("Operation completed", operation="test", duration=0.123)
    """
    global _logger_config
    if _logger_config is None:
        _logger_config = FaultMavenLogger()

    return structlog.get_logger(name)
