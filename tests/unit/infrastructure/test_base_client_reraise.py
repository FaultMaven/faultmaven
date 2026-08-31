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


@pytest.mark.asyncio
async def test_rejected_credential_opens_the_breaker():
    """A revoked/invalid key (401/403) is account-scoped like quota: every later
    request fails identically until an operator rotates it. Before this was
    classified, the breaker never opened for it and every turn kept making a
    doomed round trip — and the open-breaker error carried nothing actionable."""
    from faultmaven.exceptions import PROVIDER_AUTH_FAILED

    for status in (401, 403):
        client = _BreakerClient(threshold=2)

        async def fail_with_auth():
            raise LLMException(
                f"provider rejected the key ({status})", status_code=status
            )

        for _ in range(2):
            with pytest.raises(LLMException):
                await client.call_external(
                    operation_name="route_llm_request",
                    call_func=fail_with_auth,
                    retries=0,
                )

        assert client.circuit_breaker.state == "open", status
        assert client.circuit_breaker.last_failure_error_code == PROVIDER_AUTH_FAILED


@pytest.mark.asyncio
async def test_not_found_stays_request_scoped():
    """404 is deliberately NOT account-scoped: a wrong path fails only that call
    shape, and turning it into a breaker trip would replace an actionable
    'not found' with an opaque outage."""
    client = _BreakerClient(threshold=2)

    async def fail_with_404():
        raise LLMException("model not found", status_code=404)

    for _ in range(4):
        with pytest.raises(LLMException):
            await client.call_external(
                operation_name="route_llm_request", call_func=fail_with_404, retries=0
            )

    assert client.circuit_breaker.state == "closed"
    assert client.circuit_breaker.failure_count == 0


# =============================================================================
# fm#1287 — the client-side deadline DECLARES its own retryability
# =============================================================================


@pytest.mark.asyncio
async def test_timeout_raises_typed_error_declaring_retryable():
    """A ``call_external`` deadline expiry must raise something that SAYS it is
    retryable, not a bare ``TimeoutError`` whose sentence has to be parsed.

    This site used to raise ``TimeoutError("… timed out after 30.0s")`` and the
    engine's ladder matched that against a phrase list containing ``"timeout"``,
    which is not a substring of ``"timed out"`` — so a hung provider got zero
    retries while every provider's own read timeout (a 504) got three.

    Pinned here, separately from the ladder's call-count test, because the two
    fixes are deliberately redundant: the classifier also has a
    ``TimeoutError``-by-type rule, so the ladder still retries if this raise
    regresses. Without this assertion that regression would be silent.
    """
    import asyncio

    client = _Client()

    async def hang():
        await asyncio.sleep(3600)

    with pytest.raises(TimeoutError) as exc_info:
        await client.call_external(
            operation_name="route_llm_request",
            call_func=hang,
            timeout=0.05,
            retries=0,
        )

    err = exc_info.value
    # Declared, and declared as a real bool — a truthy non-bool (a Mock's
    # auto-attribute, a "yes") is not a declaration and the classifier
    # deliberately ignores it.
    assert getattr(err, "retryable", None) is True
    # Still a TimeoutError, so existing ``except TimeoutError`` handlers around
    # call_external are unaffected.
    assert isinstance(err, asyncio.TimeoutError)
    assert err.service == "TestService"
    assert err.operation == "route_llm_request"
    assert err.timeout == 0.05


@pytest.mark.asyncio
async def test_successful_call_raises_nothing():
    """POSITIVE CONTROL for the timeout pin above: the same client, the same
    call shape, a callable that returns — proves the timeout path is reached by
    the timeout and not by the harness always erroring."""
    client = _Client()

    async def quick():
        return "ok"

    result = await client.call_external(
        operation_name="route_llm_request",
        call_func=quick,
        timeout=5.0,
        retries=0,
    )
    assert result == "ok"
