"""The one module that imports the Opik trace SDK, and its process policy (#2616
part (c), Principle 13.4).

- Before ``import opik``: ``.env`` goes into the environment, then
  ``OPIK_SENTRY_ENABLE`` defaults to false, or the import starts Comet's Sentry
  crash reporting (#2585). An operator's ``true``, in either, is honoured (Q8).
- ``configure(settings)`` runs once per process before composition (the API's
  lifespan, ``run_with_container``, the jobs runner) and records the outcome in
  one :class:`TraceBackend`, which every span write, the case delete's eraser
  and ``GET /admin/config/status`` read. It refuses content capture until a
  retention bound is verified (#2616 K0), which stops the process.
- :func:`track` keeps an exception's text out of every span (#2669).
- The SDK names other modules use. The internals reached here are pinned by
  ``test_trace_erasure_loopback_2102.py``, their upgrade gate.
"""

import functools
import inspect
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

from faultmaven.config.settings import (
    ObservabilitySettings,
    env_var_spellings,
    load_settings_dotenv,
)
from faultmaven.utils.exception_name import exception_class_name
from faultmaven.utils.optional_dependency import module_is_usable

logger = logging.getLogger(__name__)

load_settings_dotenv()
if not env_var_spellings(os.environ, "OPIK_SENTRY_ENABLE"):
    os.environ["OPIK_SENTRY_ENABLE"] = "false"

# A bare ``import opik`` succeeding is not proof the SDK is installed: an
# uninstall leaves an empty ``site-packages/opik/`` that imports as a namespace
# package (faultmaven/utils/optional_dependency.py). No ``attr``: the symbols
# used here are version-sensitive, and a rename must not read as "no SDK".
try:
    import opik

    OPIK_AVAILABLE = module_is_usable(opik)
    if not OPIK_AVAILABLE:
        logger.warning(
            "Comet Opik not available: 'opik' resolved to a namespace package "
            "(no SDK) at %s",
            list(getattr(opik, "__path__", []) or ["<unknown>"]),
        )
except ImportError:
    OPIK_AVAILABLE = False

# The span writers every traced call tags its owner with. The eraser finds a
# trace only by those tags, so an SDK that moved either decorates no call.
try:
    from opik import opik_context

    SPAN_TAGGING_AVAILABLE = OPIK_AVAILABLE and all(
        module_is_usable(opik_context, attr)
        for attr in ("update_current_span", "update_current_trace")
    )
except ImportError:
    SPAN_TAGGING_AVAILABLE = False

#: The SDK's connection-monitor ping interval. Its default is 10 s, but its ping
#: URL drops the ``/opik/api/`` prefix (``urljoin`` with an absolute path), so it
#: stays this high until each deployment's URL form passes a live check (#2573,
#: the #2616 ruling, Q9). A parked trace waits this long for a replay.
PING_INTERVAL_S = 999999

NOT_CONFIGURED = "tracing was not configured in this process"


def _sentry_reporting() -> bool:
    """Is the SDK's crash reporter active in this process: the effect, read from
    ``sentry_sdk``, else the setting that decided at ``import opik``."""
    try:
        import sentry_sdk

        return bool(sentry_sdk.get_client().is_active())
    except (ImportError, AttributeError):
        flag = os.environ.get("OPIK_SENTRY_ENABLE", "").strip().lower()
        return OPIK_AVAILABLE and flag in {"1", "true", "t", "yes", "y", "on"}


@dataclass(frozen=True)
class TraceBackend:
    """Where this process sends traces, as :func:`configure` resolved it."""

    sdk_available: bool = False
    #: ``OPIK_ENABLED``, as configured.
    enabled: bool = False
    #: A backend was reached by configuration: the SDK is present, tracing is
    #: enabled and a URL is set, and nothing raised.
    configured: bool = False
    url: Optional[str] = None
    project: Optional[str] = None
    workspace: Optional[str] = None
    ping_interval_s: Optional[int] = None
    sentry_reporting: bool = False
    #: ``OPIK_CAPTURE_CONTENT``: spans carry prompts and responses. False in
    #: every record this release writes, since :func:`configure` refuses True.
    capture_content: bool = False
    #: Why ``configured`` is False; None when it is True.
    reason: Optional[str] = NOT_CONFIGURED


_record = TraceBackend(
    sdk_available=OPIK_AVAILABLE, sentry_reporting=_sentry_reporting()
)


def backend() -> TraceBackend:
    """The record :func:`configure` wrote, or the unconfigured placeholder."""
    return _record


# What OPIK_TRACK_DISABLE held before the disabled path overwrote it: the
# sentinel (we never wrote it), None (the operator had it unset), or the
# operator's exact string. Only what WE wrote may be undone.
_TRACK_DISABLE_UNRECORDED = object()
_track_disable_prior_value: Any = _TRACK_DISABLE_UNRECORDED


def _disable_sdk_tracing() -> None:
    """Flip the SDK's own kill switches so disabled means disabled: any tracked
    path the per-call gate does not cover would otherwise build a client on the
    SDK's default backend, Comet Cloud (#1121). ``set_tracing_active(False)``
    overrides a cached True, which every ``@opik.track`` wrapper consults before
    building a client; ``OPIK_TRACK_DISABLE=true`` covers anything that re-reads
    ``OpikConfig``. The prior value is recorded on the FIRST write only, so
    :func:`_enable_sdk_tracing` undoes exactly this write.
    """
    global _track_disable_prior_value
    if _track_disable_prior_value is _TRACK_DISABLE_UNRECORDED:
        _track_disable_prior_value = os.environ.get("OPIK_TRACK_DISABLE")

    os.environ["OPIK_TRACK_DISABLE"] = "true"
    try:
        import opik as _opik

        _opik.set_tracing_active(False)
    except Exception as e:
        logger.debug(f"Could not flip Opik SDK tracing switch: {e}")


def _enable_sdk_tracing() -> None:
    """Undo our own kill switch, honouring anything the operator set.

    Both switches are process-global, so an enabled configure after a disabled
    one would otherwise trace nothing. Forcing them on would also override an
    operator's ``OPIK_TRACK_DISABLE=true``, a documented way to suppress spans
    with a backend configured, and resume the egress #1121 stopped. So restore
    exactly the value the disabled path overwrote (its absence included), then
    let ``reset_tracing_to_config_default()`` re-derive the flag from the
    environment.
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
        logger.debug(f"Could not reset Opik SDK tracing switch: {e}")


class TraceContentCaptureRefused(RuntimeError):
    """``OPIK_CAPTURE_CONTENT=true`` without a verified retention bound."""


def content_capture_refusal(obs: Any) -> Optional[str]:
    """Why ``obs`` may not capture content, or None when it asks for none.

    #2616 K0: the trace store holds prompts and responses only under a verified
    retention bound (K3), and none is verified yet, so a request is always
    refused. K3 adds the verification here. The field is a pydantic bool: only
    ``True`` is a request, and settings without the field ask for nothing.
    """
    if getattr(obs, "opik_capture_content", False) is not True:
        return None
    return (
        "OPIK_CAPTURE_CONTENT=true is refused: recording prompts, responses and "
        "exception text in the trace store needs a verified retention bound for "
        "it, and none exists yet (#2616 K3). Unset OPIK_CAPTURE_CONTENT."
    )


def configure(settings: Any) -> TraceBackend:
    """Point the SDK at the configured backend, or switch it off, and record the
    outcome. Once per process, before composition. Raises only
    :class:`TraceContentCaptureRefused`, whatever ``OPIK_ENABLED`` holds, so the
    process stops (#2616 K0). Any other failure is recorded as the reason,
    beside every field resolved before it, and the process runs without tracing.

    It sets the environment variables the SDK's ``OpikConfig`` reads rather than
    calling ``opik.configure()``, which prompts, pings and writes
    ``~/.opik.config``. ``@opik.track`` builds its client lazily from them.
    """
    global _record
    refusal = content_capture_refusal(getattr(settings, "observability", None))
    if refusal is not None:
        raise TraceContentCaptureRefused(refusal)
    resolved: dict = {
        "sdk_available": OPIK_AVAILABLE,
        "sentry_reporting": _sentry_reporting(),
    }
    try:
        reason = _configure(settings, resolved)
    except Exception as e:
        logger.error(f"Failed to initialize Opik tracing: {e}")
        logger.info("Continuing without tracing...")
        reason = f"configuring the trace backend raised {exception_class_name(e)}"
    _record = TraceBackend(**resolved, configured=reason is None, reason=reason)
    return _record


def _configure(settings: Any, resolved: dict) -> Optional[str]:
    """Resolve the backend into ``resolved``, as far as it gets, and apply it.
    Returns why tracing is off, or None once a backend is configured."""
    obs = settings.observability
    resolved["enabled"] = bool(obs.opik_enabled)
    resolved["capture_content"] = getattr(obs, "opik_capture_content", False) is True
    if not OPIK_AVAILABLE:
        logger.warning("Opik SDK not installed, skipping tracing initialization")
        return "the Opik SDK is not installed"
    if not resolved["enabled"]:
        _disable_sdk_tracing()
        logger.info("Opik tracing disabled (OPIK_ENABLED=false)")
        return "OPIK_ENABLED is false"

    if obs.opik_use_local:
        # Self-hosted Opik (local Docker or K8s) expects {base}/api/.
        url = obs.opik_local_url.rstrip("/") + "/api/"
    elif obs.opik_url_override:
        # Cloud Opik (Comet) or a custom endpoint.
        url = obs.opik_url_override.rstrip("/") + "/"
    else:
        # Without the kill switch the call sites' SDK would fall back to its
        # Comet Cloud default.
        _disable_sdk_tracing()
        logger.warning(
            "Opik enabled but no URL configured. "
            "Set OPIK_USE_LOCAL=true or OPIK_URL_OVERRIDE. Tracing will be disabled."
        )
        return "no backend URL is set"
    resolved["url"] = url

    # One project: the setting's, or its default when it is set empty (#2588).
    project = resolved["project"] = (
        obs.opik_project_name
        or ObservabilitySettings.model_fields["opik_project_name"].default
    )
    api_key = obs.opik_api_key.get_secret_value() if obs.opik_api_key else None
    # FaultMaven reads COMET_WORKSPACE; the SDK reads OPIK_WORKSPACE.
    workspace = resolved["workspace"] = obs.comet_workspace or "default"

    _enable_sdk_tracing()
    os.environ["OPIK_URL_OVERRIDE"] = url
    os.environ["OPIK_PROJECT_NAME"] = project
    os.environ["OPIK_WORKSPACE"] = workspace
    if api_key:
        os.environ["OPIK_API_KEY"] = api_key
    os.environ["OPIK_CONNECTION_MONITOR_PING_INTERVAL"] = str(PING_INTERVAL_S)
    # And an OpikConfig already built in this process.
    try:
        import opik.config

        for key, value in (
            ("url_override", url),
            ("project_name", project),
            ("workspace", workspace),
            ("connection_monitor_ping_interval", PING_INTERVAL_S),
            *((("api_key", api_key),) if api_key else ()),
        ):
            opik.config.update_session_config(key, value)
    except Exception as e:
        logger.debug(f"Could not update Opik session config: {e}")

    logger.info(
        f"Opik tracing initialized: url={url}, project={project}, workspace={workspace}"
    )
    resolved["ping_interval_s"] = PING_INTERVAL_S
    return None


def trace_backend_configured() -> bool:
    """Did :func:`configure` reach a backend?

    The recorded outcome alone, without the SDK's tracing switch: an operator's
    ``OPIK_TRACK_DISABLE=true`` stops new spans, but the traces already recorded
    can still be read and deleted, which is what the case delete needs (#2102).
    """
    return _record.configured


def tracing_is_effective() -> bool:
    """Will a traced call record a span now? The per-call gate of every span
    write, and what ``/admin/config/status`` reports (#1234): the record, the
    span writers, then the SDK's switch (an operator's
    ``OPIK_TRACK_DISABLE=true``), read live through ``sys.modules``.

    Without the span writers (``SPAN_TAGGING_AVAILABLE``) no call is decorated
    and a span could not carry the owner the eraser finds it by, so nothing is
    recorded and this answers False; the gates then stop before calling the
    missing writers. :func:`trace_backend_configured` ignores them: traces
    already written can still be deleted. An unreadable switch answers True: a
    renamed symbol is not evidence that tracing stopped.
    """
    if not (_record.configured and SPAN_TAGGING_AVAILABLE):
        return False
    try:
        import opik as _opik

        return bool(_opik.is_tracing_active())
    except Exception as e:  # pragma: no cover - version-sensitive symbol
        logger.debug(f"Could not read Opik tracing switch: {e}")
        return True


# --------------------------------------------------------------------------- #
# The SDK surface other modules use. Each looks the SDK up per call, so a test's
# stand-in in ``sys.modules`` steers it.
# --------------------------------------------------------------------------- #


#: Per call, the exception :func:`track`'s guard caught: set by the outer
#: function before the SDK's wrapper runs, read after it returns.
_CAUGHT: ContextVar[Optional[list]] = ContextVar("opik_sdk_caught", default=None)


@contextmanager
def _caught() -> Iterator[list]:
    slot: list = []
    token = _CAUGHT.set(slot)
    try:
        yield slot
    finally:
        _CAUGHT.reset(token)


def _hold(exc: Exception) -> None:
    """Keep ``exc`` for the outer function, and put its class, never its text,
    on the span. A failed span write must not replace ``exc``. With no slot (no
    outer function ran) it re-raises, so nothing is lost."""
    slot = _CAUGHT.get()
    if slot is None:
        raise exc
    slot.append(exc)
    try:
        update_current_span(metadata={"error_class": exception_class_name(exc)})
    except Exception as e:
        logger.warning(
            "Could not tag the span with the error class: %s", exception_class_name(e)
        )


def _sdk_reads_as_async(func: Callable) -> bool:
    """Whether the SDK wraps ``func`` as a coroutine function, by its own test,
    so the guard is built the same way. An SDK that moved the test: the
    stdlib's, which agrees for a plain ``def`` or ``async def``."""
    try:
        from opik.decorator.inspect_helpers import is_async
    except ImportError:
        return inspect.iscoroutinefunction(func)
    return bool(is_async(func))


def track(**options: Any) -> Callable:
    """``opik.track(**options)``, with no exception text crossing it.

    The SDK records a tracked function's exception as ``error_info``: its
    message and traceback, which can embed a provider's response body (#2102).
    So the SDK wraps a guard that catches ``Exception``, holds it in a per-call
    slot, writes its class to the span and returns None; the outer function
    re-raises the same object. A ``BaseException`` that is not an ``Exception``
    (a cancellation) carries no provider text and passes through. Unconditional
    while content capture is refused; K3 decides whether text returns with
    capture on (#2616). Generators are refused: the SDK records their errors on
    a path the guard does not cover. Decoration builds no client.
    """
    import opik as _opik

    sdk_track = _opik.track(**options)

    def decorate(func: Callable) -> Callable:
        if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
            raise TypeError(
                f"opik_sdk.track does not trace generators: {func.__qualname__}"
            )
        if _sdk_reads_as_async(func):

            async def guard(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    return _hold(e)

            tracked = sdk_track(functools.wraps(func)(guard))

            @functools.wraps(func)
            async def outer(*args: Any, **kwargs: Any) -> Any:
                with _caught() as slot:
                    result = await tracked(*args, **kwargs)
                if slot:
                    raise slot[0]
                return result

            return outer

        def sync_guard(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                return _hold(e)

        sync_tracked = sdk_track(functools.wraps(func)(sync_guard))

        @functools.wraps(func)
        def sync_outer(*args: Any, **kwargs: Any) -> Any:
            with _caught() as slot:
                result = sync_tracked(*args, **kwargs)
            if slot:
                raise slot[0]
            return result

        return sync_outer

    return decorate


def update_current_span(**fields: Any) -> None:
    import opik as _opik

    _opik.opik_context.update_current_span(**fields)


def update_current_trace(**fields: Any) -> None:
    import opik as _opik

    _opik.opik_context.update_current_trace(**fields)


def cached_client() -> Any:
    """The client ``@opik.track`` writes through, built if none is, so a flush
    drains its queue."""
    from opik.api_objects.opik_client import get_client_cached

    return get_client_cached()


def parsed_filters(filter_string: str, entity_type: str) -> Optional[str]:
    """The JSON string the SDK's own export sends the paged list endpoints."""
    from opik.api_objects.opik_query_language import OpikQueryLanguage

    parser = getattr(OpikQueryLanguage, f"for_{entity_type}")
    return parser(filter_string).parsed_filters


def is_not_found(exc: BaseException) -> bool:
    """A 404 from the SDK's REST client. Its page reads raise the base
    ``ApiError`` carrying the status, not its ``NotFoundError`` subclass."""
    try:
        from opik.rest_api.core.api_error import ApiError
    except ImportError:
        return False
    return isinstance(exc, ApiError) and exc.status_code == 404


def replay_monitor(client: Any) -> Any:
    """The connection monitor whose flag gates the SDK's replay of parked
    traces. No public accessor; an SDK that moved it raises AttributeError."""
    return client._streamer._fallback_replay_manager._monitor


def _built_client() -> Any:
    """The client this process has already built, or None. Never builds one."""
    from opik.api_objects.opik_client import get_current_client_raw

    return get_current_client_raw()


def connection_state() -> tuple[str, Optional[int]]:
    """``(server_connection, parked_messages)`` of the client already built (#2573).

    ``server_connection`` is ``connected``, ``disconnected`` (a send failed, so
    the SDK parks every later trace until a replay), ``no_client``, or
    ``unknown`` when the SDK's internals moved. ``parked_messages`` counts the
    parked traces; None when unknown. Blocking (a SQLite count): run it off the
    event loop.
    """
    if not OPIK_AVAILABLE:
        return "no_client", None
    try:
        client = _built_client()
        if client is None:
            return "no_client", None
        manager = client._streamer._fallback_replay_manager
    except (ImportError, AttributeError):
        return "unknown", None
    try:
        connected = manager._monitor.has_server_connection
        server = "connected" if connected else "disconnected"
    except AttributeError:
        server = "unknown"
    try:
        count = manager._db_manager.failed_messages_count()
    except AttributeError:
        count = None
    # -1: the database is not open, or failed.
    parked = count if isinstance(count, int) and count >= 0 else None
    return server, parked


def fastapi_middleware() -> Optional[type]:
    """The SDK's FastAPI middleware class, or None. opik 1.11.14 ships none."""
    if not OPIK_AVAILABLE:
        return None
    try:
        from opik.integrations.fastapi import OpikMiddleware
    except ImportError:
        return None
    return OpikMiddleware
