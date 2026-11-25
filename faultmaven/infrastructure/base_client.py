"""
FaultMaven Base Infrastructure Client

Provides a base class for all external service clients in the infrastructure
layer with unified logging, retry logic, circuit breaker patterns, and
comprehensive error handling.
"""

import asyncio
import contextvars
import functools
import re
import time
from abc import ABC
from concurrent.futures import Executor
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, Optional, TypeVar, Union

from faultmaven.exceptions import (
    CALLER_REMEDY_CATEGORIES,
    SERVICE_SCOPED_ERROR_CODES,
    CircuitBreakerError,
    ExternalCallTimeout,
    FaultMavenException,
    LLMTurnBudgetSpent,
    counts_against_breaker,
    declared_llm_category,
    is_rate_limit_throttle,
)
from faultmaven.infrastructure.logging.unified import UnifiedLogger, get_unified_logger
from faultmaven.utils.exception_name import (
    ERROR_CODE_PATTERN,
    exception_class_chain,
    exception_class_name,
    exception_log_name,
)

#: How ``call_external`` labels a caller remedy it does not count (#2858).
_CALLER_REMEDY = "the request must change, not counted against the circuit breaker"

# Type variable for generic return types
T = TypeVar("T")

# The monotonic deadline of the ``call_external`` call in progress, bound
# around its ``call_func`` whenever it has a timeout. A wait inside the call
# (a provider waiting out a 429, #2793) reads it so it never outlasts the call.
_call_deadline: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "call_deadline", default=None
)


def remaining_call_time() -> Optional[float]:
    """Seconds left before the enclosing ``call_external`` call's timeout, or
    ``None`` outside a call bound by one."""
    deadline = _call_deadline.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


@contextmanager
def call_deadline(seconds: float) -> Iterator[None]:
    """Rebind the enclosing call's deadline to ``seconds`` from now, for the
    block: one attempt of a routed walk (#2983), so a wait inside it spends
    the attempt's share, not the walk's."""
    token = _call_deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _call_deadline.reset(token)


def _provider_codes(error: BaseException) -> Dict[str, Any]:
    """The ``status_code`` and ``error_code`` a first-party exception declares,
    logged beside the class chain where its text no longer carries them (#2844).

    Read only from the two first-party hierarchies, ``FaultMavenException`` and
    ``models.exceptions.FaultMavenError``: both carry codes the breaker latches
    (an ``LLMException``'s, an ``LLMProviderError``'s ``LLM_CONFIG_ERROR``). A
    third party's attributes of the same names are not first-party fields, and
    their values are its own to word. An ``error_code`` is kept only when it
    is exactly a ``str`` matching :data:`ERROR_CODE_PATTERN`, the check
    :func:`exception_log_name` makes: a first-party class can carry a code it
    copied from another's (#2889).
    """
    # Imported here: ``faultmaven.models`` runs the modules layer on import,
    # which this client must not pull in.
    from faultmaven.models.exceptions import FaultMavenError

    if not isinstance(error, (FaultMavenException, FaultMavenError)):
        return {}
    codes = {}
    if isinstance(status := getattr(error, "status_code", None), int):
        codes["status_code"] = status
    code = getattr(error, "error_code", None)
    if type(code) is str and re.fullmatch(ERROR_CODE_PATTERN, code):
        codes["error_code"] = code
    return codes


class CircuitBreaker:
    """
    Simple circuit breaker implementation for external service calls.

    This class implements the circuit breaker pattern to prevent cascading
    failures when external services are unavailable or responding slowly.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        expected_exception: type = Exception,
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Seconds to wait before attempting to close circuit
            expected_exception: Exception type that triggers circuit breaker
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception

        self.failure_count = 0
        self.last_failure_time = None
        self.state = "closed"  # closed, open, half-open
        # error_code of the most recent *classified* failure in the current
        # failing streak, surfaced on the CircuitBreakerError raised while the
        # breaker is open. Latches across the streak (see record_failure) and
        # resets on success.
        self.last_failure_error_code = None

    def can_execute(self) -> bool:
        """Check if circuit allows execution."""
        if self.state == "closed":
            return True

        if self.state == "open":
            # Check if we should try half-open
            if self.last_failure_time and datetime.now(
                timezone.utc
            ) - self.last_failure_time > timedelta(seconds=self.recovery_timeout):
                self.state = "half-open"
                return True
            return False

        # half-open state
        return True

    def record_success(self) -> None:
        """Record successful execution."""
        self.failure_count = 0
        self.state = "closed"
        # Fresh streak starts clean — a recovered provider drops any latched
        # permanent classification.
        self.last_failure_error_code = None

    def record_failure(self, exception: Exception) -> None:
        """Record failed execution."""
        if isinstance(exception, self.expected_exception):
            self.failure_count += 1
            self.last_failure_time = datetime.now(timezone.utc)
            # Latch the failure classification so the open-breaker error can
            # carry it forward (e.g. QUOTA_EXHAUSTED billing). Only a *classified*
            # failure updates it: a later unclassified transient (error_code=None)
            # in the same streak must NOT erase a permanent billing signal that
            # opened — or is about to open — the breaker.
            new_code = getattr(exception, "error_code", None)
            if new_code is not None:
                self.last_failure_error_code = new_code

            if self.failure_count >= self.failure_threshold:
                self.state = "open"
            elif self.state == "half-open":
                self.state = "open"


class BaseExternalClient(ABC):
    """
    Base class for all external service clients in the infrastructure layer.

    This class provides common functionality for infrastructure components
    that interact with external services, including unified logging, retry
    logic, circuit breaker patterns, and comprehensive error handling.

    All infrastructure clients should inherit from this base class to ensure
    consistent patterns for external service interaction.

    Attributes:
        client_name: Name of the external client
        service_name: Name of the external service being accessed
        logger: UnifiedLogger instance for infrastructure layer
        circuit_breaker: Circuit breaker for service protection
        blocking_executor: Where ``call_external`` runs a SYNC ``call_func``
            (see :meth:`run_blocking`), or ``None`` to run it inline
    """

    #: Where :meth:`run_blocking` runs blocking work, and so where
    #: ``call_external`` runs a SYNC ``call_func``. ``None`` (the default, and
    #: every client but the ChromaDB stores on the ``HttpClient`` arm) runs it
    #: inline, on the calling thread, as it always has. A client whose sync
    #: calls block on I/O sets one so that they leave the event loop (#2056),
    #: and bounds those calls itself: a ``timeout`` ends the caller's await,
    #: never the call in its thread (see ``call_external``).
    blocking_executor: Optional[Executor] = None

    def __init__(
        self,
        client_name: str,
        service_name: str,
        enable_circuit_breaker: bool = True,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: int = 60,
    ):
        """
        Initialize base external client with unified logging and circuit breaker.

        Args:
            client_name: Name of the client (e.g., "redis_client", "chromadb_client")
            service_name: Name of the external service (e.g., "Redis", "ChromaDB")
            enable_circuit_breaker: Whether to enable circuit breaker protection
            circuit_breaker_threshold: Failures before opening circuit
            circuit_breaker_timeout: Seconds to wait before retry
        """
        self.client_name = client_name
        self.service_name = service_name
        self.logger = get_unified_logger(
            f"faultmaven.infrastructure.{client_name}", "infrastructure"
        )

        # Initialize circuit breaker if enabled
        if enable_circuit_breaker:
            self.circuit_breaker = CircuitBreaker(
                failure_threshold=circuit_breaker_threshold,
                recovery_timeout=circuit_breaker_timeout,
            )
        else:
            self.circuit_breaker = None

        # Connection metrics
        self.connection_metrics = {
            "total_calls": 0,
            "successful_calls": 0,
            "failed_calls": 0,
            "circuit_breaker_trips": 0,
            "last_success_time": None,
            "last_failure_time": None,
        }

        # Log client initialization
        self.logger.log_event(
            event_type="system",
            event_name="external_client_initialized",
            severity="info",
            data={
                "client_name": self.client_name,
                "service_name": self.service_name,
                "circuit_breaker_enabled": enable_circuit_breaker,
            },
        )

    async def run_blocking(self, fn: Callable[..., T], *args, **kwargs) -> T:
        """Run a blocking ``fn(*args, **kwargs)`` where this client runs blocking work.

        With a :attr:`blocking_executor`, in that executor, so the event loop
        stays free while ``fn`` blocks; ``fn`` runs in a copy of the caller's
        context, as ``asyncio.to_thread`` runs it, so the request id and other
        context the log lines carry come with it. Without one, inline on the
        calling thread.
        """
        if self.blocking_executor is None:
            return fn(*args, **kwargs)
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        return await loop.run_in_executor(
            self.blocking_executor,
            functools.partial(context.run, fn, *args, **kwargs),
        )

    async def call_external(
        self,
        operation_name: str,
        call_func: Callable[..., Union[T, any]],
        *args,
        timeout: Optional[float] = None,
        retries: int = 0,
        retry_delay: float = 1.0,
        validate_response: Optional[Callable[[T], bool]] = None,
        transform_response: Optional[Callable[[T], T]] = None,
        **kwargs,
    ) -> T:
        """
        Execute external service call with unified logging, retries, and circuit breaker.

        This method provides a standardized way to call external services with:
        - Unified infrastructure logging
        - Circuit breaker protection
        - Automatic retry with exponential backoff
        - Response validation and transformation
        - Comprehensive error handling and metrics

        Args:
            operation_name: Name of the external operation (e.g., "get", "set", "query")
            call_func: The function to call (can be sync or async). A sync one
                runs through :meth:`run_blocking`: in :attr:`blocking_executor`
                when the client sets one, inline otherwise
            *args: Arguments to pass to the call function
            timeout: Optional budget, applied with ``asyncio.timeout`` to a
                coroutine ``call_func``, and to a sync one that runs in a
                :attr:`blocking_executor`. For a sync call it ends the
                caller's AWAIT, not the call: that runs on in its thread, to
                the client's own bound. So a call that WRITES passes
                ``timeout=None`` and is awaited to its end. Otherwise its
                caller could see a timeout for a write still landing and
                compensate for it while it lands (#2056). A sync call that
                runs inline, with no executor, is never bounded here:
                a timeout cannot preempt the loop thread.
            retries: Number of retry attempts on failure
            retry_delay: Base delay between retries (with exponential backoff)
            validate_response: Optional function to validate response
            transform_response: Optional function to transform response
            **kwargs: Keyword arguments to pass to the call function

        Returns:
            Result of the external call, potentially transformed

        Raises:
            CircuitBreakerError: If circuit breaker is open
            TimeoutError: If operation times out
            Original exception raised by ``call_func`` on final failure
            (e.g. ``LLMException``). Re-raised unwrapped so callers retain
            access to typed metadata like ``status_code`` and ``retryable``.

        Example:
            >>> async def redis_get(key: str) -> str:
            ...     return await redis_client.get(key)
            >>>
            >>> def validate_not_none(value) -> bool:
            ...     return value is not None
            >>>
            >>> result = await self.call_external(
            ...     "get_user_session",
            ...     redis_get,
            ...     "session:123",
            ...     timeout=5.0,
            ...     retries=2,
            ...     validate_response=validate_not_none
            ... )
        """
        # Check circuit breaker
        if self.circuit_breaker and not self.circuit_breaker.can_execute():
            self.connection_metrics["circuit_breaker_trips"] += 1
            self.logger.log_event(
                event_type="technical",
                event_name="circuit_breaker_open",
                severity="warning",
                data={
                    "client": self.client_name,
                    "service": self.service_name,
                    "operation": operation_name,
                },
            )
            raise CircuitBreakerError(
                f"Circuit breaker is open for {self.service_name}",
                error_code=self.circuit_breaker.last_failure_error_code,
                service=self.service_name,
            )

        # Log external call boundary - inbound (DEBUG level to reduce verbosity)
        if self.logger.logger.isEnabledFor(10):  # DEBUG level
            self.logger.log_boundary(
                operation=operation_name,
                direction="inbound",
                data={
                    "client": self.client_name,
                    "service": self.service_name,
                    "args_count": len(args),
                    "kwargs_keys": list(kwargs.keys()) if kwargs else [],
                    "timeout": timeout,
                    "retries": retries,
                },
            )

        # Track metrics
        self.connection_metrics["total_calls"] += 1

        # Execute with unified operation logging
        async with self.logger.operation(
            operation_name, client=self.client_name, service=self.service_name
        ) as ctx:
            last_exception = None

            for attempt in range(retries + 1):
                try:
                    # Log retry attempt if not first attempt
                    if attempt > 0:
                        ctx[f"retry_attempt_{attempt}"] = {
                            "delay": retry_delay * (2 ** (attempt - 1)),
                            "previous_error": (
                                exception_class_chain(last_exception)
                                if last_exception is not None
                                else None
                            ),
                        }

                        self.logger.log_event(
                            event_type="technical",
                            event_name="external_call_retry",
                            severity="info",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "attempt": attempt + 1,
                                "max_attempts": retries + 1,
                            },
                        )

                    # Execute with timeout
                    start_time = time.time()

                    # Bound before the call starts, so a wait inside it reads
                    # the deadline (#2793).
                    deadline_token = (
                        _call_deadline.set(time.monotonic() + timeout)
                        if timeout
                        else None
                    )
                    # This call's own bound, so a timeout can be told from one
                    # the call raised itself (#2983).
                    own_bound = None
                    try:
                        if asyncio.iscoroutinefunction(call_func):
                            if timeout:
                                async with asyncio.timeout(timeout) as own_bound:
                                    result = await call_func(*args, **kwargs)
                            else:
                                result = await call_func(*args, **kwargs)
                        elif timeout and self.blocking_executor is not None:
                            # Off the loop, so the budget can end the AWAIT. The
                            # call itself runs on in its thread to the client's
                            # own bound, which is why a write passes no timeout
                            # (see ``timeout`` above, #2056).
                            async with asyncio.timeout(timeout) as own_bound:
                                result = await self.run_blocking(
                                    call_func, *args, **kwargs
                                )
                        else:
                            # Awaited to its end: with no timeout (a write), or
                            # inline, where a timeout could not preempt it.
                            result = await self.run_blocking(call_func, *args, **kwargs)
                    finally:
                        if deadline_token is not None:
                            _call_deadline.reset(deadline_token)

                    call_duration = time.time() - start_time
                    ctx["call_duration"] = call_duration

                    # Validate response if validator provided
                    if validate_response:
                        try:
                            is_valid = validate_response(result)
                            if not is_valid:
                                raise ValueError("Response validation failed")
                            ctx["validation"] = "passed"
                        except Exception as validation_error:
                            ctx["validation"] = "failed"
                            ctx["validation_error"] = exception_class_chain(
                                validation_error
                            )
                            self.logger.error(
                                f"Response validation failed for {self.service_name}.{operation_name}",
                                error=validation_error,
                                client=self.client_name,
                                service=self.service_name,
                            )
                            raise RuntimeError(
                                f"Response validation failed: {exception_class_chain(validation_error)}"
                            ) from validation_error

                    # Transform response if transformer provided
                    if transform_response:
                        try:
                            if asyncio.iscoroutinefunction(transform_response):
                                result = await transform_response(result)
                            else:
                                result = transform_response(result)
                            ctx["transformation"] = "applied"
                        except Exception as transform_error:
                            ctx["transformation"] = "failed"
                            ctx["transform_error"] = exception_class_chain(
                                transform_error
                            )
                            self.logger.error(
                                f"Response transformation failed for {self.service_name}.{operation_name}",
                                error=transform_error,
                                client=self.client_name,
                                service=self.service_name,
                            )
                            # Don't raise - continue with untransformed result
                            self.logger.warning(
                                f"Continuing with untransformed response for {self.service_name}.{operation_name}",
                                client=self.client_name,
                                service=self.service_name,
                            )

                    # Success - record metrics and circuit breaker state
                    self.connection_metrics["successful_calls"] += 1
                    self.connection_metrics["last_success_time"] = datetime.now(
                        timezone.utc
                    ).isoformat()

                    if self.circuit_breaker:
                        self.circuit_breaker.record_success()

                    # Log performance metrics (DEBUG level to reduce verbosity)
                    if self.logger.logger.isEnabledFor(10):  # DEBUG level
                        self.logger.log_metric(
                            metric_name="external_call_duration",
                            value=call_duration,
                            unit="seconds",
                            tags={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "success": "true",
                            },
                        )

                    # Log successful boundary - outbound (DEBUG level to reduce verbosity)
                    if self.logger.logger.isEnabledFor(10):  # DEBUG level
                        self.logger.log_boundary(
                            operation=operation_name,
                            direction="outbound",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "success": True,
                                "duration": call_duration,
                                "attempts": attempt + 1,
                            },
                        )

                    # Log success event (DEBUG level to reduce verbosity)
                    if self.logger.logger.isEnabledFor(10):  # DEBUG level
                        self.logger.log_event(
                            event_type="technical",
                            event_name="external_call_success",
                            severity="info",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "duration": call_duration,
                                "attempts": attempt + 1,
                            },
                        )

                    return result

                except asyncio.TimeoutError as timeout_error:
                    last_exception = timeout_error
                    ctx[f"timeout_attempt_{attempt}"] = True

                    # Don't retry on timeout - fail fast
                    self.connection_metrics["failed_calls"] += 1
                    self.connection_metrics["last_failure_time"] = datetime.now(
                        timezone.utc
                    ).isoformat()

                    if self.circuit_breaker:
                        self.circuit_breaker.record_failure(timeout_error)

                    self.logger.error(
                        f"External call timed out: {self.service_name}.{operation_name}",
                        error=timeout_error,
                        client=self.client_name,
                        service=self.service_name,
                        timeout=timeout,
                    )

                    # A typed timeout of this service, raised inside the call
                    # (one attempt of a routed walk, #2983), already names
                    # what ran out and after how long: passed through.
                    if (
                        isinstance(timeout_error, ExternalCallTimeout)
                        and timeout_error.service == self.service_name
                    ):
                        raise

                    # Typed, and carrying its own ``retryable=True``. A bare
                    # ``TimeoutError`` left retryability to be guessed from this
                    # sentence downstream, and the guess was wrong: the engine's
                    # phrase list has "timeout", the sentence says "timed out",
                    # and a hung provider got zero retries (#1287). The class is
                    # still a ``TimeoutError``, so existing handlers are
                    # unaffected. Only this call's own bound has a figure to
                    # name; a timeout the call raised itself names none.
                    if own_bound is not None and own_bound.expired():
                        raise ExternalCallTimeout(
                            f"External call to {self.service_name}.{operation_name} timed out after {timeout}s",
                            service=self.service_name,
                            operation=operation_name,
                            timeout=timeout,
                        )
                    raise ExternalCallTimeout(
                        f"External call to {self.service_name}.{operation_name}"
                        " timed out inside the call",
                        service=self.service_name,
                        operation=operation_name,
                    ) from timeout_error

                except Exception as call_error:
                    last_exception = call_error
                    # Every line below names the class chain, never the text:
                    # a client's exception can carry its output (#2844).
                    ctx[f"error_attempt_{attempt}"] = {
                        "error": exception_class_chain(call_error),
                        "error_type": type(call_error).__name__,
                        **_provider_codes(call_error),
                    }

                    # Non-retryable errors (e.g. HTTP 4xx) — fail fast,
                    # don't waste time retrying a request that will fail again.
                    #
                    # Whether this counts against the circuit breaker depends on
                    # the failure's SCOPE, not on retryability:
                    #
                    # * REQUEST-scoped (bad shape, unsupported feature, a response
                    #   schema the model refuses to compile) — NOT counted. The
                    #   breaker is service-wide, so counting these let one
                    #   permanently-bad request disable everything: three
                    #   deterministic Gemini 400s opened the shared
                    #   ``LLM_Providers`` breaker and every subsequent LLM call —
                    #   including smaller payloads that would have succeeded, and
                    #   the fallback chain behind it — failed with an opaque
                    #   CircuitBreakerError, re-opening on each half-open probe.
                    #   Waiting cannot fix a request the service always rejects.
                    # * SERVICE/ACCOUNT-scoped (``SERVICE_SCOPED_ERROR_CODES`` —
                    #   quota exhausted, billing disabled) — COUNTED. Every other
                    #   request will fail the same way until an operator acts, so
                    #   opening the breaker stops pointless calls, and the latched
                    #   error_code keeps the open-breaker error mapping to 402
                    #   rather than a generic 500 (the case_b639fac38fe0 chain).
                    # * CALLER-REMEDY (``CALLER_REMEDY_CATEGORIES``) — NOT counted,
                    #   retryable or not: only changing the request helps (#2858).
                    #
                    # ``failed_calls`` counts all three, so none becomes invisible.
                    if getattr(call_error, "retryable", True) is False:
                        self.connection_metrics["failed_calls"] += 1
                        self.connection_metrics["last_failure_time"] = datetime.now(
                            timezone.utc
                        ).isoformat()

                        service_scoped = counts_against_breaker(call_error)
                        if self.circuit_breaker and service_scoped:
                            self.circuit_breaker.record_failure(call_error)

                        self.logger.warning(
                            f"Non-retryable error for {self.service_name}.{operation_name}, "
                            f"failing fast ("
                            f"{'service-scoped — counted against the circuit breaker' if service_scoped else 'caller remedy — ' + _CALLER_REMEDY if declared_llm_category(call_error) in CALLER_REMEDY_CATEGORIES else 'request-scoped — not counted against the circuit breaker'}"
                            f"): {exception_class_chain(call_error)}",
                            client=self.client_name,
                            service=self.service_name,
                            **_provider_codes(call_error),
                        )

                        raise

                    # Log the error (will be retried if more attempts remain)
                    if attempt < retries:
                        self.logger.warning(
                            f"External call failed, will retry: {self.service_name}.{operation_name}",
                            error_type=exception_class_name(call_error),
                            error_chain=exception_class_chain(call_error),
                            client=self.client_name,
                            service=self.service_name,
                            attempt=attempt + 1,
                            remaining_attempts=retries - attempt,
                        )

                        # Wait before retry with exponential backoff
                        retry_wait = retry_delay * (2**attempt)
                        await asyncio.sleep(retry_wait)
                    else:
                        # Final failure
                        self.connection_metrics["failed_calls"] += 1
                        self.connection_metrics["last_failure_time"] = datetime.now(
                            timezone.utc
                        ).isoformat()

                        # Neither a caller remedy (#2858) nor a throttle, a 429 the
                        # provider's waits did not outlast (#2793), nor a call
                        # refused for a spent turn, which reached no provider
                        # (#2983), is counted.
                        if self.circuit_breaker and counts_against_breaker(call_error):
                            self.circuit_breaker.record_failure(call_error)
                        if (
                            declared_llm_category(call_error)
                            in CALLER_REMEDY_CATEGORIES
                        ):
                            self.logger.warning(
                                f"Caller remedy on {self.service_name}.{operation_name}"
                                f" ({_CALLER_REMEDY}): {exception_class_chain(call_error)}",
                                client=self.client_name,
                                service=self.service_name,
                                **_provider_codes(call_error),
                            )
                        elif is_rate_limit_throttle(call_error):
                            self.logger.warning(
                                f"Rate-limited on {self.service_name}.{operation_name}"
                                f" — a throttle, not counted against the circuit"
                                f" breaker: {exception_class_chain(call_error)}",
                                client=self.client_name,
                                service=self.service_name,
                                **_provider_codes(call_error),
                            )
                        elif isinstance(call_error, LLMTurnBudgetSpent):
                            self.logger.warning(
                                f"Turn budget spent on {self.service_name}.{operation_name}"
                                f" — no provider called, not counted against the"
                                f" circuit breaker: {exception_class_chain(call_error)}",
                                client=self.client_name,
                                service=self.service_name,
                            )

                        # Log failed boundary - outbound
                        self.logger.log_boundary(
                            operation=operation_name,
                            direction="outbound",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "success": False,
                                "error": exception_log_name(call_error),
                                "attempts": attempt + 1,
                            },
                        )

                        # Log failure event
                        self.logger.log_event(
                            event_type="technical",
                            event_name="external_call_failed",
                            severity="error",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "error": exception_class_chain(call_error),
                                "attempts": attempt + 1,
                            },
                        )

                        raise

        # Should not reach here, but just in case
        raise RuntimeError(
            f"Unexpected error in external call to {self.service_name}.{operation_name}"
        )

    def call_external_sync(
        self,
        operation_name: str,
        call_func: Callable[..., T],
        *args,
        retries: int = 0,
        retry_delay: float = 1.0,
        validate_response: Optional[Callable[[T], bool]] = None,
        transform_response: Optional[Callable[[T], T]] = None,
        **kwargs,
    ) -> T:
        """
        Synchronous version of call_external for non-async operations.

        Args:
            operation_name: Name of the external operation
            call_func: The synchronous function to call
            *args: Arguments to pass to the call function
            retries: Number of retry attempts on failure
            retry_delay: Base delay between retries
            validate_response: Optional function to validate response
            transform_response: Optional function to transform response
            **kwargs: Keyword arguments to pass to the call function

        Returns:
            Result of the external call, potentially transformed
        """
        # Check circuit breaker
        if self.circuit_breaker and not self.circuit_breaker.can_execute():
            self.connection_metrics["circuit_breaker_trips"] += 1
            self.logger.log_event(
                event_type="technical",
                event_name="circuit_breaker_open",
                severity="warning",
                data={
                    "client": self.client_name,
                    "service": self.service_name,
                    "operation": operation_name,
                },
            )
            raise CircuitBreakerError(
                f"Circuit breaker is open for {self.service_name}",
                error_code=self.circuit_breaker.last_failure_error_code,
                service=self.service_name,
            )

        # Log external call boundary - inbound
        self.logger.log_boundary(
            operation=operation_name,
            direction="inbound",
            data={
                "client": self.client_name,
                "service": self.service_name,
                "args_count": len(args),
                "kwargs_keys": list(kwargs.keys()) if kwargs else [],
                "retries": retries,
            },
        )

        # Track metrics
        self.connection_metrics["total_calls"] += 1

        # Execute with unified operation logging (synchronous)
        with self.logger.operation_sync(
            operation_name, client=self.client_name, service=self.service_name
        ) as ctx:
            last_exception = None

            for attempt in range(retries + 1):
                try:
                    # Log retry attempt if not first attempt
                    if attempt > 0:
                        ctx[f"retry_attempt_{attempt}"] = {
                            "delay": retry_delay * (2 ** (attempt - 1)),
                            "previous_error": (
                                exception_class_chain(last_exception)
                                if last_exception is not None
                                else None
                            ),
                        }

                        self.logger.log_event(
                            event_type="technical",
                            event_name="external_call_retry",
                            severity="info",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "attempt": attempt + 1,
                                "max_attempts": retries + 1,
                            },
                        )

                    # Execute call
                    start_time = time.time()
                    result = call_func(*args, **kwargs)
                    call_duration = time.time() - start_time
                    ctx["call_duration"] = call_duration

                    # Validate response if validator provided
                    if validate_response:
                        try:
                            is_valid = validate_response(result)
                            if not is_valid:
                                raise ValueError("Response validation failed")
                            ctx["validation"] = "passed"
                        except Exception as validation_error:
                            ctx["validation"] = "failed"
                            ctx["validation_error"] = exception_class_chain(
                                validation_error
                            )
                            self.logger.error(
                                f"Response validation failed for {self.service_name}.{operation_name}",
                                error=validation_error,
                                client=self.client_name,
                                service=self.service_name,
                            )
                            raise RuntimeError(
                                f"Response validation failed: {exception_class_chain(validation_error)}"
                            ) from validation_error

                    # Transform response if transformer provided
                    if transform_response:
                        try:
                            result = transform_response(result)
                            ctx["transformation"] = "applied"
                        except Exception as transform_error:
                            ctx["transformation"] = "failed"
                            ctx["transform_error"] = exception_class_chain(
                                transform_error
                            )
                            self.logger.error(
                                f"Response transformation failed for {self.service_name}.{operation_name}",
                                error=transform_error,
                                client=self.client_name,
                                service=self.service_name,
                            )
                            # Don't raise - continue with untransformed result
                            self.logger.warning(
                                f"Continuing with untransformed response for {self.service_name}.{operation_name}",
                                client=self.client_name,
                                service=self.service_name,
                            )

                    # Success - record metrics and circuit breaker state
                    self.connection_metrics["successful_calls"] += 1
                    self.connection_metrics["last_success_time"] = datetime.now(
                        timezone.utc
                    ).isoformat()

                    if self.circuit_breaker:
                        self.circuit_breaker.record_success()

                    # Log performance metrics (DEBUG level to reduce verbosity)
                    if self.logger.logger.isEnabledFor(10):  # DEBUG level
                        self.logger.log_metric(
                            metric_name="external_call_duration",
                            value=call_duration,
                            unit="seconds",
                            tags={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "success": "true",
                            },
                        )

                    # Log successful boundary - outbound (DEBUG level to reduce verbosity)
                    if self.logger.logger.isEnabledFor(10):  # DEBUG level
                        self.logger.log_boundary(
                            operation=operation_name,
                            direction="outbound",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "success": True,
                                "duration": call_duration,
                                "attempts": attempt + 1,
                            },
                        )

                    # Log success event (DEBUG level to reduce verbosity)
                    if self.logger.logger.isEnabledFor(10):  # DEBUG level
                        self.logger.log_event(
                            event_type="technical",
                            event_name="external_call_success",
                            severity="info",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "duration": call_duration,
                                "attempts": attempt + 1,
                            },
                        )

                    return result

                except Exception as call_error:
                    last_exception = call_error
                    ctx[f"error_attempt_{attempt}"] = {
                        "error": exception_class_chain(call_error),
                        "error_type": type(call_error).__name__,
                        **_provider_codes(call_error),
                    }

                    # Non-retryable errors (e.g. HTTP 4xx) — fail fast. Counted
                    # against the breaker only when SERVICE/ACCOUNT-scoped; see
                    # the async path above for the full rationale.
                    if getattr(call_error, "retryable", True) is False:
                        self.connection_metrics["failed_calls"] += 1
                        self.connection_metrics["last_failure_time"] = datetime.now(
                            timezone.utc
                        ).isoformat()

                        service_scoped = (
                            getattr(call_error, "error_code", None)
                            in SERVICE_SCOPED_ERROR_CODES
                        )
                        if self.circuit_breaker and service_scoped:
                            self.circuit_breaker.record_failure(call_error)

                        self.logger.warning(
                            f"Non-retryable error for {self.service_name}.{operation_name}, "
                            f"failing fast ("
                            f"{'service-scoped — counted against the circuit breaker' if service_scoped else 'request-scoped — not counted against the circuit breaker'}"
                            f"): {exception_class_chain(call_error)}",
                            client=self.client_name,
                            service=self.service_name,
                            **_provider_codes(call_error),
                        )

                        raise

                    # Log the error (will be retried if more attempts remain)
                    if attempt < retries:
                        self.logger.warning(
                            f"External call failed, will retry: {self.service_name}.{operation_name}",
                            error_type=exception_class_name(call_error),
                            error_chain=exception_class_chain(call_error),
                            client=self.client_name,
                            service=self.service_name,
                            attempt=attempt + 1,
                            remaining_attempts=retries - attempt,
                        )

                        # Wait before retry with exponential backoff
                        retry_wait = retry_delay * (2**attempt)
                        time.sleep(retry_wait)
                    else:
                        # Final failure
                        self.connection_metrics["failed_calls"] += 1
                        self.connection_metrics["last_failure_time"] = datetime.now(
                            timezone.utc
                        ).isoformat()

                        if self.circuit_breaker:
                            self.circuit_breaker.record_failure(call_error)

                        # Log failed boundary - outbound
                        self.logger.log_boundary(
                            operation=operation_name,
                            direction="outbound",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "success": False,
                                "error": exception_log_name(call_error),
                                "attempts": attempt + 1,
                            },
                        )

                        # Log failure event
                        self.logger.log_event(
                            event_type="technical",
                            event_name="external_call_failed",
                            severity="error",
                            data={
                                "client": self.client_name,
                                "service": self.service_name,
                                "operation": operation_name,
                                "error": exception_class_chain(call_error),
                                "attempts": attempt + 1,
                            },
                        )

                        raise

        # Should not reach here, but just in case
        raise RuntimeError(
            f"Unexpected error in external call to {self.service_name}.{operation_name}"
        )

    async def health_check(self) -> Dict[str, Any]:
        """
        Perform health check for the external service client.

        This method should be overridden by subclasses to provide
        client-specific health checking logic.

        Returns:
            Dictionary containing health status and metrics
        """
        return {
            "client": self.client_name,
            "service": self.service_name,
            "status": "unknown",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "layer": "infrastructure",
            "metrics": self.connection_metrics.copy(),
            "circuit_breaker": {
                "enabled": self.circuit_breaker is not None,
                "state": (
                    getattr(self.circuit_breaker, "state", "disabled")
                    if self.circuit_breaker
                    else "disabled"
                ),
                "failure_count": (
                    getattr(self.circuit_breaker, "failure_count", 0)
                    if self.circuit_breaker
                    else 0
                ),
            },
        }
