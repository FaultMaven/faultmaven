"""Regression tests for BaseExternalClient.call_external error re-raising.

The contract: when a wrapped call fails, the original exception must
propagate unwrapped so callers can inspect typed metadata such as
``LLMException.status_code`` and ``LLMException.retryable``. Wrapping in
``RuntimeError`` (the old behavior) silently destroyed retryability and
caused upstream retry handlers to misclassify transient 5xx errors.
"""

import pytest

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.base_client import BaseExternalClient


class _Client(BaseExternalClient):
    def __init__(self):
        super().__init__(
            client_name="test_client",
            service_name="TestService",
            enable_circuit_breaker=False,
        )


@pytest.mark.asyncio
async def test_call_external_reraises_llm_exception_unwrapped():
    """LLMException raised by call_func must propagate as itself, not as
    a RuntimeError wrapper. The handler downstream needs status_code +
    retryable to make the right retry decision."""
    client = _Client()

    async def fail_with_502():
        raise LLMException("Fireworks 502 bad gateway", status_code=502)

    with pytest.raises(LLMException) as exc_info:
        await client.call_external(
            operation_name="route_llm_request",
            call_func=fail_with_502,
            retries=0,
        )

    assert exc_info.value.status_code == 502
    assert exc_info.value.retryable is True


@pytest.mark.asyncio
async def test_call_external_reraises_4xx_unwrapped_and_non_retryable():
    """4xx LLMException must propagate with retryable=False intact so the
    retry handler fails fast instead of burning attempts on a client error."""
    client = _Client()

    async def fail_with_400():
        raise LLMException("bad request", status_code=400)

    with pytest.raises(LLMException) as exc_info:
        await client.call_external(
            operation_name="route_llm_request",
            call_func=fail_with_400,
            retries=0,
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_call_external_sync_reraises_llm_exception_unwrapped():
    """Sync variant must follow the same re-raise contract as the async one."""
    client = _Client()

    def fail_with_503():
        raise LLMException("upstream busy", status_code=503)

    with pytest.raises(LLMException) as exc_info:
        client.call_external_sync(
            operation_name="route_llm_request",
            call_func=fail_with_503,
            retries=0,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.retryable is True


# --- circuit-breaker scope for non-retryable errors --------------------------
#
# The breaker is scoped to the SERVICE (e.g. "LLM_Providers"), so what may open it
# is decided by the failure's SCOPE, not its retryability:
#   request-scoped (rejected schema, malformed body) -> must NOT open it. Live
#     incident: three deterministic Gemini 400s took down every other LLM call,
#     including the fallback chain and smaller payloads that would have succeeded.
#   service/account-scoped (quota exhausted) -> must STILL open it. Every request
#     fails the same way until an operator acts, and the latched error_code keeps
#     the open-breaker error mapping to 402 (case_b639fac38fe0).
# Both directions are pinned below; testing only one would let the other regress.


class _BreakerClient(BaseExternalClient):
    def __init__(self, threshold=3):
        super().__init__(
            client_name="test_client",
            service_name="TestService",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=threshold,
            circuit_breaker_timeout=30,
        )


@pytest.mark.asyncio
async def test_request_scoped_rejections_never_open_the_breaker():
    """A request the service will always reject is not evidence of ill-health.
    Well past the threshold, the breaker must still be closed."""
    client = _BreakerClient(threshold=3)

    async def fail_with_400():
        raise LLMException("schema rejected: too many states", status_code=400)

    for _ in range(6):
        with pytest.raises(LLMException):
            await client.call_external(
                operation_name="route_llm_request",
                call_func=fail_with_400,
                retries=0,
            )

    assert client.circuit_breaker is not None
    assert client.circuit_breaker.failure_count == 0
    # And the service is still callable — the whole point.
    assert (
        await client.call_external(
            operation_name="route_llm_request",
            call_func=_ok,
            retries=0,
        )
    ) == "ok"


@pytest.mark.asyncio
async def test_non_retryable_errors_still_counted_in_connection_metrics():
    """Not counting them against the breaker must not make them invisible."""
    client = _BreakerClient()

    async def fail_with_400():
        raise LLMException("bad request", status_code=400)

    with pytest.raises(LLMException):
        await client.call_external(
            operation_name="route_llm_request", call_func=fail_with_400, retries=0
        )

    assert client.connection_metrics["failed_calls"] == 1
    assert client.connection_metrics["last_failure_time"] is not None


@pytest.mark.asyncio
async def test_retryable_exhaustion_still_opens_the_breaker():
    """The converse: genuine transient failure IS a health signal, so the
    breaker must still trip. Without this, the change above would have disabled
    the breaker for the case it exists to handle."""
    client = _BreakerClient(threshold=2)

    async def fail_with_503():
        raise LLMException("upstream unavailable", status_code=503)

    for _ in range(2):
        with pytest.raises(Exception):
            await client.call_external(
                operation_name="route_llm_request",
                call_func=fail_with_503,
                retries=0,
            )

    assert client.circuit_breaker.failure_count >= 2


async def _ok():
    return "ok"


@pytest.mark.asyncio
async def test_service_scoped_quota_failures_still_open_the_breaker():
    """Quota exhaustion is permanent for the whole account, so unlike a rejected
    request it SHOULD trip the breaker — otherwise every turn keeps calling a
    provider that is out of credits, and the open-breaker error loses the
    QUOTA_EXHAUSTED code that maps it to 402 instead of a generic 500."""
    from faultmaven.exceptions import QUOTA_EXHAUSTED

    client = _BreakerClient(threshold=3)

    async def fail_with_quota():
        raise LLMException(
            "OpenAI API error 429: insufficient_quota - You have exceeded your "
            "current quota",
            status_code=429,
        )

    for _ in range(3):
        with pytest.raises(LLMException):
            await client.call_external(
                operation_name="route_llm_request",
                call_func=fail_with_quota,
                retries=0,
            )

    assert client.circuit_breaker.state == "open"
    # The classification survives on the breaker for the open-breaker error.
    assert client.circuit_breaker.last_failure_error_code == QUOTA_EXHAUSTED


@pytest.mark.asyncio
async def test_quota_is_non_retryable_yet_still_breaker_worthy():
    """Pins the reason the rule keys on scope rather than retryability: a quota
    error is BOTH non-retryable and breaker-worthy, so a rule written as
    'non-retryable never counts' silently regresses the billing path."""
    err = LLMException("insufficient_quota", status_code=429)
    assert err.retryable is False
    from faultmaven.exceptions import SERVICE_SCOPED_ERROR_CODES

    assert err.error_code in SERVICE_SCOPED_ERROR_CODES
