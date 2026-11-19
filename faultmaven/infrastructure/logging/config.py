"""
FaultMaven Logging Configuration

Provides enhanced logging configuration using structlog with JSON formatting,
request context injection and OpenTelemetry integration.

Configuration is read from the unified settings system (faultmaven.config.settings)
at runtime, not at import time.
"""

import logging
import sys
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, TextIO

import structlog
from opentelemetry import trace

from faultmaven.infrastructure.logging.url_redaction import redacting_renderer
from faultmaven.utils.exception_name import (
    exception_class_chain,
    exception_frames,
    logged_exception,
)

# Third-party loggers whose DEBUG output is per-operation tracing rather than
# signal, each with the level it is pinned at. Raising our own verbosity to
# DEBUG (what the `local` preset does) would otherwise bury the log in it:
#
# * httpcore logs exclusively at DEBUG, a record for every TCP/TLS step of
#   every request, and Opik's connection monitor probes its backend on a timer
#   from a daemon thread. Pinned at WARNING.
# * aiosqlite logs two DEBUG records per statement ("executing ..." and
#   "operation ... completed", each carrying the SQL), from its worker thread.
#   Measured on the 180-commit turn-commit benchmark at DEBUG: 87,546 records,
#   and the commit's p50 rose from ~70 ms to 224 ms (#1944). Pinned at INFO,
#   not WARNING, so its one INFO record
#   ("exception occurred while closing connection") still reaches the log.
#
# The other database loggers need no pin: SQLAlchemy pins its own "sqlalchemy"
# logger at WARN when nothing set it (its echo flag is the opt-in), and asyncpg
# logs nothing per statement.
#
# httpx is deliberately NOT listed. It logs only at INFO, and only the one
# "HTTP Request: <method> <url> -> <status>" line per request, which is the
# signal we want when an LLM provider starts returning 429s. It has no DEBUG
# output, so pinning it would cost that line and suppress nothing.
NOISY_THIRD_PARTY_LOGGERS: Mapping[str, int] = MappingProxyType(
    {
        "httpcore": logging.WARNING,
        "aiosqlite": logging.INFO,
    }
)


# Event keys the formatter's later steps read or write: exc_info_as_frames and
# StackInfoRenderer take exc_info / stack_info as directives (exc_info_as_frames
# writes frames, StackInfoRenderer stack); the console renderer renders an
# exception key; ProcessorFormatter writes the _record / _from_structlog meta
# keys. ``frames`` is not one: a caller's own (``log_error``, the policy's
# ``extra={"frames": ...}``) is lifted and wins over exc_info_as_frames'.
_FORMATTER_KEYS = frozenset(
    {"exc_info", "stack_info", "exception", "stack", "_record", "_from_structlog"}
)


#: Keys a caller's ``extra`` never replaces when the event already holds them:
#: the logger's own facts, and a ``user_id`` (the request context stamps one
#: only when verified; a native keyword ``user_id`` is held the same way). The
#: formatter's keys are never lifted at all.
_UNREPLACEABLE_KEYS = frozenset({"level", "logger", "timestamp", "user_id"})


def frames_as_console_stack(
    _logger: Any, _name: str, event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Show a line's ``frames`` (``exception_frames``) as the console renderer's
    multi-line ``stack`` block, for the console renderer only.

    ``frames`` is the field the logging policy records frames under
    (``docs/operations/monitoring/logging-policy.md``). The console renderer
    writes any other field inline with its newlines escaped, so the line
    redactor, which runs on each rendered line, would drop every frame after
    the first one it redacts. A line that already has a ``stack`` (from
    ``stack_info``) gets the frames after it. A non-string ``frames`` or
    ``stack`` leaves both as they are: the renderer concatenates ``stack`` as
    text. JSON lines keep ``frames``.
    """
    frames, stack = event_dict.get("frames"), event_dict.get("stack")
    if isinstance(frames, str) and (stack is None or isinstance(stack, str)):
        del event_dict["frames"]
        event_dict["stack"] = frames if stack is None else f"{stack}\n{frames}"
    return event_dict


def exc_info_as_frames(
    _logger: Any, _name: str, event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Render an ``exc_info`` (:func:`logged_exception`'s shapes) as ``frames``:
    :func:`exception_frames`, each link's class and frames with no message
    (principles §7, rule 4; #2888). A ``frames`` the caller gave is kept."""
    logged = logged_exception(event_dict.pop("exc_info", None))
    if logged is not None and "frames" not in event_dict:
        event_dict["frames"] = exception_frames(*logged)
    return event_dict


def _class_chain_or_value(value: Any) -> Any:
    if isinstance(value, BaseException):
        return exception_class_chain(value)
    return value


def exceptions_as_class_chains(
    _logger: Any, _name: str, event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Write an exception passed as a field (``error=e``, ``extra={"e": e}``,
    the event itself) as its class chain, never its repr, which holds its
    message (principles §7, rule 4; #2888). It maps top-level values, and the
    values of an ``extra`` dict one level down, where :func:`_lift_fields`
    nests a caller's colliding field. Nothing else is walked: under JSON, the
    renderer's ``default`` names an exception the encoder reaches."""
    for key, value in event_dict.items():
        event_dict[key] = _class_chain_or_value(value)
    nested = event_dict.get("extra")
    if isinstance(nested, dict):
        event_dict["extra"] = {k: _class_chain_or_value(v) for k, v in nested.items()}
    return event_dict


def _json_default(value: Any) -> Any:
    """The JSON renderer's fallback: an exception's class chain, else what
    structlog's own fallback writes (``__structlog__()``, else the repr)."""
    if isinstance(value, BaseException):
        return exception_class_chain(value)
    structlog_form = getattr(value, "__structlog__", None)
    return structlog_form() if callable(structlog_form) else repr(value)


def _lift_fields(event_dict: Dict[str, Any], fields: Dict[str, Any]) -> None:
    """Write a caller's ``extra`` fields onto the event, by the one rule both
    paths share.

    A caller's ``extra`` field wins over whatever the event already holds for
    that key: a request-context stamp, and equally a keyword or bound field of
    the same call. So a telemetry line's ``event`` name arrives as the caller
    wrote it, as it did on the stdlib path before (#2268). The value it
    replaces is not kept. A route never passes ``correlation_id``: the stamp is
    the request's own id, the one its response carries (#2278).

    The exceptions are the logger's own facts, a ``user_id`` the event already
    holds, and the keys the formatter acts on (a traceback directive) or writes:
    a caller's value for one of those stays under ``extra``, beside whatever the
    caller passed as ``extra`` itself (#2262, #2268). One is lost instead: on
    the stdlib path a caller's ``_record`` never reaches this function, since
    the record's attribute of that name is where structlog keeps the record.
    """
    kept = {}
    for key, value in fields.items():
        if key in _FORMATTER_KEYS or (key in _UNREPLACEABLE_KEYS and key in event_dict):
            kept[key] = value
        else:
            event_dict[key] = value
    if kept:
        held = event_dict.get("extra")
        if held is None:
            event_dict["extra"] = kept
        elif isinstance(held, dict):
            event_dict["extra"] = {**held, **kept}
        else:
            event_dict["extra"] = {"extra": held, **kept}


def lift_stdlib_extra(
    _logger: Any, _method: str, event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Lift a stdlib-style ``extra={...}`` dict into a structlog event's fields.

    A structlog logger takes fields as keyword arguments, so the stdlib idiom
    ``logger.info(msg, extra={...})`` made ``extra`` one nested field, and every
    key inside it was invisible to a log query and to a reader of the record's
    fields (#2262). Runs after the shared processors, by :func:`_lift_fields`'
    rule; :func:`lift_record_extra` applies the same rule to plain stdlib
    records (#2268).
    """
    extra = event_dict.get("extra")
    if not isinstance(extra, dict):
        return event_dict
    del event_dict["extra"]
    _lift_fields(event_dict, extra)
    return event_dict


#: Collects a stdlib record's ``extra`` fields, the attributes a plain
#: LogRecord does not have, without writing them onto the event.
_RECORD_EXTRA = structlog.stdlib.ExtraAdder()


def lift_record_extra(
    logger: Any, method: str, event_dict: Dict[str, Any]
) -> Dict[str, Any]:
    """Lift a stdlib record's ``extra={...}`` fields into the event, by
    :func:`_lift_fields`' rule.

    ``ExtraAdder`` wrote every field over whatever the event held, the logger's
    own ``level`` included (a caller's ``extra={"level": "top"}`` replaced the
    record's level), where the native path kept every collision under ``extra``.
    One rule for both paths now (#2268).
    """
    record = event_dict.get("_record")
    if record is None:
        return event_dict
    fields = _RECORD_EXTRA(logger, method, {"_record": record})
    del fields["_record"]
    _lift_fields(event_dict, fields)
    return event_dict


class LateBindingStreamHandler(logging.StreamHandler):
    """A StreamHandler that resolves ``sys.stderr`` at emit time.

    ``logging.StreamHandler()`` captures ``sys.stderr`` once, when it is
    constructed. This handler is installed from a module-level singleton, so
    that would pin whichever stream happened to be current at the first
    ``get_logger()`` call — under pytest, a capture buffer that gets closed
    when the test ends. Anything still logging afterwards (a daemon thread
    outliving the test) then writes into a closed file, and because the dead
    stream belongs to the *root* handler, every record routed here is lost,
    not only the thread's own.

    Resolving the stream per emit keeps the handler valid across stream swaps.
    Assigning ``stream`` still pins an explicit stream, so ``setStream`` and
    tests that redirect output behave as they do on the base class; assigning
    the current ``sys.stderr`` back releases the pin, so save/restore round
    trips leave the handler following ``sys.stderr`` as it found it.
    """

    def __init__(self) -> None:
        # Deliberately calls Handler.__init__ rather than StreamHandler's,
        # which would assign to the ``stream`` property below and pin it.
        self._stream_override: Optional[TextIO] = None
        logging.Handler.__init__(self)

    @property
    def stream(self) -> Optional[TextIO]:
        """The explicitly-set stream, else whatever ``sys.stderr`` is now."""
        if self._stream_override is not None:
            return self._stream_override
        return sys.stderr

    @stream.setter
    def stream(self, value: Optional[TextIO]) -> None:
        # Assigning back whatever ``sys.stderr`` currently is means "follow
        # sys.stderr again". Without this, the standard save/restore idiom
        # (``old = h.stream; h.stream = buf; h.stream = old``) would read a
        # materialised stream out of the getter and pin it on the way back,
        # silently reintroducing the very bug this class exists to fix.
        self._stream_override = None if value is sys.stderr else value


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
    Enhanced logger configuration for structured logging.

    This class configures structlog with processors for request context injection,
    trace context, and JSON formatting. It ensures consistent
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
        Configure structlog, and the root handler every line renders through.

        Native structlog events run the level filter and the shared processors
        (``_SHARED_PROCESSORS``), then go to the stdlib root handler, whose
        formatter (:func:`build_log_formatter`) renders them and plain stdlib
        records alike. A root that already holds :func:`install_log_handler`'s
        handler (a job) gets no second one: the events render through it, the
        same builder, and each line is written once (#2888).
        """
        # Native structlog loggers: filter + shared processors, then hand the
        # event to the stdlib ProcessorFormatter instead of rendering here.
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                *_SHARED_PROCESSORS,
                lift_stdlib_extra,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        formatter = build_log_formatter(
            self.config.LOG_FORMAT, self.config.LOG_HUMAN_READABLE, sys.stderr
        )
        handler = LateBindingStreamHandler()
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
        if not any(
            getattr(h, _INSTALLED_HANDLER_MARKER, False) for h in root_logger.handlers
        ):
            root_logger.addHandler(handler)

        # Lower the root level to our configured verbosity, but never RAISE it
        # above a level something else (e.g. pytest's caplog.at_level) has
        # already lowered it to — otherwise reconfiguration would suppress
        # records a caller explicitly asked to capture.
        configured_level = self.config.get_log_level()
        if root_logger.level == logging.NOTSET or root_logger.level > configured_level:
            root_logger.setLevel(configured_level)

        self.quiet_noisy_third_party_loggers()

    @staticmethod
    def quiet_noisy_third_party_loggers() -> None:
        """Pin each per-operation tracer at its level in NOISY_THIRD_PARTY_LOGGERS.

        An explicit level set on the logger by anyone else wins — an operator
        debugging HTTP or SQL, or ``caplog.set_level(..., logger="httpcore")``
        — so this only supplies a default where none was chosen. Setting the
        level on the parent logger beats the root's level for the whole
        subtree, which is what keeps LOG_LEVEL=DEBUG from reaching these.
        """
        for logger_name, level in NOISY_THIRD_PARTY_LOGGERS.items():
            third_party_logger = logging.getLogger(logger_name)
            if third_party_logger.level == logging.NOTSET:
                third_party_logger.setLevel(level)

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
            # ``claimed_session_id``, not ``session_id``: the value is whatever
            # the caller put in the header and has never been checked against
            # anything (fm#1461). The name is what stops a reader treating it as
            # established fact.
            if "claimed_session_id" not in event_dict and ctx.claimed_session_id:
                event_dict["claimed_session_id"] = ctx.claimed_session_id
            if "user_id" not in event_dict and ctx.user_id:
                event_dict["user_id"] = ctx.user_id
            # ``claimed_case_id`` for the same reason, and so that it never
            # competes with a record's own ``case_id``: under that name the
            # request's claim displaced the case a line was actually about
            # (#2268).
            if "claimed_case_id" not in event_dict and ctx.claimed_case_id:
                event_dict["claimed_case_id"] = ctx.claimed_case_id
            if "agent_phase" not in event_dict and ctx.agent_phase:
                event_dict["agent_phase"] = ctx.agent_phase

        return event_dict

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


#: Processors that BUILD the event dict, shared by native structlog logs AND
#: foreign stdlib LogRecords, so both render identically with their fields.
#: Rendering itself happens once, in :func:`build_log_formatter`.
_SHARED_PROCESSORS = (
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
    FaultMavenLogger.add_request_context,
    FaultMavenLogger.add_trace_context,
)


def build_log_formatter(
    log_format: str, human_readable: bool, stream: TextIO
) -> structlog.stdlib.ProcessorFormatter:
    """The one formatter every line renders through, structlog events and plain
    stdlib records alike: the API's root handler, and :func:`install_log_handler`'s
    for the jobs and alembic (principles §7, rule 5; #2888). It takes no settings
    object, so a process that builds none can build it from ``LOG_FORMAT``
    alone. The redactor runs last, on the rendered line.
    """
    # Renderer: JSON for production, human-readable console for dev.
    #
    # LOG_HUMAN_READABLE is an override in its own right. It used to be
    # guarded by ``LOG_FORMAT != "json"``, and since LOG_FORMAT defaults to
    # "json" and now accepts nothing but json/console, that guard could
    # never be true: setting LOG_HUMAN_READABLE alone did nothing.
    #
    # colors: structlog enables them whenever it is not on Windows —
    # ``structlog.dev._has_colors`` has no isatty() check — so the console
    # renderer would write ANSI escapes into a pod's log stream, where they
    # reach kubectl and the log collector verbatim. Decide from the stream.
    console = log_format == "console" or human_readable
    if console:
        renderer = structlog.dev.ConsoleRenderer(colors=stream.isatty())
    else:
        renderer = structlog.processors.JSONRenderer(default=_json_default)

    # Redact on the RENDERED line rather than on the event dict. Walking
    # values by type leaked every type it did not handle -- bytes, sets,
    # dict keys, anything the renderer repr()s such as an exception passed
    # as error=e, and anything nested past the walk's depth. After
    # rendering, all of it is text, and one pass covers the message, the
    # fields, the keys and the traceback alike: URL query strings, and
    # credential header values (a JSON line on its decoded keys and values).
    renderer = redacting_renderer(renderer)

    # For FOREIGN (stdlib) records — including third-party libs and every
    # ``logger.info(msg, extra={...})`` call in this codebase — the pre-chain
    # runs the shared processors PLUS lift_record_extra, so the ``extra``
    # fields are merged onto the event dict and actually render (previously
    # they were silently dropped by ``basicConfig(format="%(message)s")``).
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*_SHARED_PROCESSORS, lift_record_extra],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.StackInfoRenderer(),
            # Not format_exc_info: it writes every link's message (#2888).
            exc_info_as_frames,
            exceptions_as_class_chains,
            structlog.processors.UnicodeDecoder(),
            *([frames_as_console_stack] if console else []),
            renderer,
        ],
    )


#: Stamped on :func:`install_log_handler`'s handler, so the API chain adds no
#: second root handler in a process that installed it.
_INSTALLED_HANDLER_MARKER = "_faultmaven_installed_handler"


def install_log_handler(stream: TextIO, level: Optional[int]) -> None:
    """Give the root logger one handler writing ``stream`` through
    :func:`build_log_formatter`, at ``level`` (``None`` keeps the root's),
    unless it already has a handler (``logging.basicConfig``'s rule): the line
    of a process that builds no settings, the maintenance jobs and alembic
    (#2888).

    JSON or console is decided by ``LOG_FORMAT`` alone (``configured_log_format``,
    the working directory's ``.env`` loaded first, as the app loads it), read
    once the handler is in place: an unrecognized value is JSON, and its
    warning is written on a JSON line. No other setting is read, so none can
    refuse a migration or change the line; ``LOG_HUMAN_READABLE`` is not read.
    """
    from faultmaven.config.settings import configured_log_format, load_settings_dotenv

    load_settings_dotenv()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(build_log_formatter("json", False, stream))
    setattr(handler, _INSTALLED_HANDLER_MARKER, True)
    logging.basicConfig(level=level, handlers=[handler])
    if configured_log_format() == "console":
        handler.setFormatter(build_log_formatter("console", False, stream))


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
