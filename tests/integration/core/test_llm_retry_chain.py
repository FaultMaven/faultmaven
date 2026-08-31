"""End-to-end regression: LLM retry chain preserves typed exception metadata.

Reproduces the production failure mode where a Fireworks 502 was misclassified
as non-retryable and surfaced as a wrapped RuntimeError. The chain under test
mirrors what ``MilestoneEngine._generate_structured_output`` does:

    LLMErrorHandler.with_retry( call_external( provider_call ) )

Where ``call_external`` is ``BaseExternalClient.call_external`` (the LLM
router's own retry/circuit-breaker boundary) and ``provider_call`` is a
provider whose underlying HTTP layer raises ``LLMException(status_code=502)``.

If either fix regresses (base_client re-wraps in RuntimeError, OR the handler
forgets to consult ``LLMException.retryable``), these tests fail.
"""

import asyncio

import pytest

from faultmaven.core.investigation.llm_error_handler import (
    ErrorAction,
    LLMErrorHandler,
    RetryConfig,
)
from faultmaven.exceptions import (
    PROVIDER_CIRCUIT_OPEN,
    QUOTA_EXHAUSTED,
    LLMException,
)
from faultmaven.infrastructure.base_client import (
    BaseExternalClient,
    CircuitBreakerError,
)


class _RouterLike(BaseExternalClient):
    """Stand-in for LLMRouter: a real BaseExternalClient subclass that
    forwards to an injected provider call, exactly as the production router
    does via ``self.registry.route_request``."""

    def __init__(self):
        super().__init__(
            client_name="llm_router_test",
            service_name="LLM_Providers",
            enable_circuit_breaker=False,
        )


@pytest.fixture
def fast_handler():
    return LLMErrorHandler(
        RetryConfig(max_retries=3, base_delay_seconds=0.01, max_delay_seconds=0.05)
    )


@pytest.fixture
def router():
    return _RouterLike()


@pytest.mark.asyncio
async def test_502_retried_then_succeeds(fast_handler, router):
    """The canonical happy path after the fix: Fireworks returns 502 once,
    the handler retries, the second call succeeds. Before the fix, the 502
    was misclassified (not in the string-pattern list) and never retried."""
    attempts = 0

    async def provider_call():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LLMException("Fireworks 502 bad gateway", status_code=502)
        return "ok"

    async def llm_operation():
        return await router.call_external(
            operation_name="route_llm_request",
            call_func=provider_call,
            retries=0,
        )

    result, error = await fast_handler.with_retry(operation=llm_operation)

    assert result == "ok"
    assert error is None
    assert attempts == 2


@pytest.mark.asyncio
async def test_persistent_502_exhausts_and_surfaces_unwrapped(fast_handler, router):
    """When Fireworks keeps returning 502, the handler exhausts max_retries
    and the original LLMException must be retrievable (via the action / error
    payload or, in production, propagated by the caller). Critically, the
    underlying exception type observed at each attempt is LLMException, not
    a RuntimeError wrapper."""
    observed_types: list[type] = []

    async def provider_call():
        raise LLMException("Fireworks 502 bad gateway", status_code=502)

    async def llm_operation():
        try:
            return await router.call_external(
                operation_name="route_llm_request",
                call_func=provider_call,
                retries=0,
            )
        except Exception as e:
            observed_types.append(type(e))
            raise

    result, error = await fast_handler.with_retry(operation=llm_operation)

    assert result is None
    assert error is not None
    assert error.action == ErrorAction.FAIL
    assert error.error_code == "RETRY_EXHAUSTED"
    # Every attempt must see the typed exception, not a RuntimeError wrap.
    assert observed_types, "expected at least one attempt"
    assert all(
        t is LLMException for t in observed_types
    ), f"base_client wrapped exception in RuntimeError; got {observed_types}"
    # The handler made multiple attempts (1 initial + max_retries).
    assert len(observed_types) == 4


@pytest.mark.asyncio
async def test_400_fails_fast_no_retry(fast_handler, router):
    """4xx must fail immediately. Before the fix, the wrapper RuntimeError
    hid the typed retryable=False flag and the string-pattern fallback was
    silent on 400s, so behavior was accidental rather than principled."""
    attempts = 0

    async def provider_call():
        nonlocal attempts
        attempts += 1
        raise LLMException("bad request: malformed payload", status_code=400)

    async def llm_operation():
        return await router.call_external(
            operation_name="route_llm_request",
            call_func=provider_call,
            retries=0,
        )

    result, error = await fast_handler.with_retry(operation=llm_operation)

    assert result is None
    assert error is not None
    assert error.action == ErrorAction.FAIL
    # Exactly one provider call — no retry burned on a client error.
    assert attempts == 1


class _BreakerRouterLike(BaseExternalClient):
    """Router stand-in WITH the circuit breaker enabled, matching the real
    LLMRouter (threshold=3). Used to reproduce the case_b639fac38fe0 failure:
    a billing 429 trips the breaker, and later turns fast-fail with an open
    breaker that must still carry the QUOTA_EXHAUSTED classification."""

    def __init__(self, threshold=3):
        super().__init__(
            client_name="llm_router_test",
            service_name="LLM_Providers",
            enable_circuit_breaker=True,
            circuit_breaker_threshold=threshold,
            circuit_breaker_timeout=30,
        )


@pytest.mark.asyncio
async def test_billing_error_fails_fast_with_quota_code(fast_handler, router):
    """A provider billing/quota body (429 'check your plan and billing') must
    fail fast as QUOTA_EXHAUSTED — no retries burned waiting for credits that
    won't appear, and an operator-actionable error_code surfaced."""
    attempts = 0

    async def provider_call():
        nonlocal attempts
        attempts += 1
        raise LLMException(
            "Gemini API request failed: 429 - You exceeded your current quota, "
            "please check your plan and billing details",
            status_code=429,
        )

    async def llm_operation():
        return await router.call_external(
            operation_name="route_llm_request",
            call_func=provider_call,
            retries=0,
        )

    result, error = await fast_handler.with_retry(operation=llm_operation)

    assert result is None
    assert error is not None
    assert error.action == ErrorAction.ESCALATE
    assert error.error_code == QUOTA_EXHAUSTED
    # Billing is non-retryable: exactly one provider call, not 1 + max_retries.
    assert attempts == 1


@pytest.mark.asyncio
async def test_billing_open_breaker_preserves_quota_code(fast_handler):
    """The full case_b639fac38fe0 chain: repeated billing failures trip the
    circuit breaker, and the subsequent open-breaker turn — which never reaches
    the provider — must STILL classify as QUOTA_EXHAUSTED rather than collapsing
    into an opaque CircuitBreakerError that maps to a generic 500."""
    breaker_router = _BreakerRouterLike(threshold=3)

    async def provider_call():
        raise LLMException(
            "OpenAI API error 429: insufficient_quota - You have exceeded your "
            "current quota",
            status_code=429,
        )

    async def llm_operation():
        return await breaker_router.call_external(
            operation_name="route_llm_request",
            call_func=provider_call,
            retries=0,
        )

    # Drive enough failing turns to open the breaker (threshold=3).
    for _ in range(3):
        await fast_handler.with_retry(operation=llm_operation)

    # Next turn: breaker is open, provider is never called, but the billing
    # signal must survive on the raised CircuitBreakerError...
    with pytest.raises(CircuitBreakerError) as exc_info:
        await breaker_router.call_external(
            operation_name="route_llm_request",
            call_func=provider_call,
            retries=0,
        )
    assert exc_info.value.error_code == QUOTA_EXHAUSTED

    # ...and the error handler must classify that open-breaker error as billing,
    # exactly as it would the original provider error.
    result, error = await fast_handler.with_retry(operation=llm_operation)
    assert result is None
    assert error is not None
    assert error.action == ErrorAction.ESCALATE
    assert error.error_code == QUOTA_EXHAUSTED


def test_billing_signal_latches_past_trailing_transient():
    """A permanent billing classification must survive a later *transient*
    failure in the same streak. If the most recent failure before the breaker
    opens carries no error_code, the breaker must still report QUOTA_EXHAUSTED —
    otherwise the open-breaker error maps to a generic 500 instead of 402."""
    from faultmaven.infrastructure.base_client import CircuitBreaker

    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30)
    billing = LLMException("insufficient_quota", status_code=429)
    transient = LLMException("502 bad gateway", status_code=502)

    cb.record_failure(billing)  # classified QUOTA_EXHAUSTED
    cb.record_failure(billing)
    cb.record_failure(transient)  # error_code=None — must NOT erase the latch

    assert cb.state == "open"
    assert cb.last_failure_error_code == QUOTA_EXHAUSTED

    # A success clears the latch so a fresh streak starts unclassified.
    cb.record_success()
    assert cb.last_failure_error_code is None
    cb.record_failure(transient)
    assert cb.last_failure_error_code is None


# =============================================================================
# fm#1287 — a HUNG provider must exhaust the ladder, not fail at zero retries
# =============================================================================
#
# ``call_external`` bounds each attempt with ``asyncio.wait_for``. When that
# deadline expires the dependency never answered, which is exactly the
# transient condition the ladder exists for — and is how every provider
# classifies its OWN read timeout (``LLMException(status_code=504)``, four
# attempts).
#
# The outer deadline used to raise a bare ``TimeoutError("… timed out after
# 30.0s")``, so retryability was decided by matching that sentence against a
# phrase list containing ``"timeout"`` — not a substring of ``"timed out"``.
# Measured on the pre-fix tree: ONE provider call, ``action=fail``,
# ``error_code=UNKNOWN_ERROR``. The two halves disagreed about the same
# condition purely because one of them was a sentence.
#
# The assertion is the PROVIDER CALL COUNT, not the phrase list. Asserting
# ``"timed out" in retryable_patterns`` would pass by construction and
# discriminate nothing.
#
# The four rows run through ONE harness — same client, same ``call_external``
# invocation, same deadline, same handler — and only the provider's behaviour
# differs. Three of them are controls, and each is anchored on behaviour this
# change does NOT touch:
#
#   hang  -> 4   THE MEASUREMENT.
#   503   -> 4   REACHABILITY. A raw 5xx was retryable before this change and
#                after it, so this row measures only whether the ladder can
#                reach four attempts IN THIS HARNESS. Without it, "hang -> 4"
#                rests on a number a broken ladder could also produce — and the
#                healthy-provider row could not tell the difference, because a
#                ladder that never retries also yields exactly 1 there.
#   400   -> 1   DISCRIMINATION. Proves 4 is not simply what this harness
#                always returns.
#   ok    -> 1   NO-ERROR. Proves a success costs one call and the harness is
#                not erroring for some reason of its own.


async def _hanging_call(counter: list) -> None:
    """A provider that accepts the request and never answers."""
    counter.append(1)
    await asyncio.sleep(3600)


async def _call_503(counter: list) -> None:
    counter.append(1)
    raise LLMException("upstream 503 over capacity", status_code=503)


async def _call_400(counter: list) -> None:
    counter.append(1)
    raise LLMException("bad request: malformed payload", status_code=400)


async def _call_ok(counter: list) -> str:
    counter.append(1)
    return "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,call_func,expected_calls,expected_code",
    [
        ("hang", _hanging_call, 4, "RETRY_EXHAUSTED"),
        ("retryable_503", _call_503, 4, "RETRY_EXHAUSTED"),
        ("terminal_400", _call_400, 1, None),
        ("success", _call_ok, 1, None),
    ],
)
async def test_retry_ladder_attempt_counts(
    fast_handler, router, label, call_func, expected_calls, expected_code
):
    """1 initial attempt + max_retries(3) = FOUR calls for a transient failure.

    ``router`` has its circuit breaker disabled, which isolates the ladder: the
    counts here are the ladder's, and only the ladder's. (The breaker's effect
    on the same failure is measured separately below.)
    """
    calls: list = []

    async def llm_operation():
        return await router.call_external(
            operation_name="route_llm_request",
            call_func=call_func,
            counter=calls,
            timeout=0.05,
            retries=0,
        )

    result, error = await fast_handler.with_retry(operation=llm_operation)

    assert len(calls) == expected_calls, (
        f"[{label}] got {len(calls)} provider call(s), expected "
        f"{expected_calls}. On the 'hang' row, one call means the client-side "
        f"deadline was classified non-retryable again (fm#1287); on the "
        f"'retryable_503' row, one call means the ladder is unreachable in "
        f"this harness and the 'hang' row proves nothing."
    )

    if expected_code is None and label == "success":
        assert result == "ok"
        assert error is None
        return

    assert result is None
    assert error is not None
    assert error.action == ErrorAction.FAIL
    if expected_code is not None:
        # Not UNKNOWN_ERROR: the failure is understood, and the ladder ran.
        assert error.error_code == expected_code


@pytest.mark.asyncio
async def test_hanging_provider_reports_open_breaker_not_unknown(fast_handler):
    """With the breaker LIVE (as the real router runs it, threshold=3), the
    ladder stops one attempt early because the fourth attempt meets an open
    breaker — and that must be reported as such.

    ``CircuitBreakerError`` carries no provider status and its message
    ("Circuit breaker is open for LLM_Providers") matches no retry phrase, so
    it used to reach the UNKNOWN_ERROR tail: the operator was told "unknown"
    about the one failure the system understands completely.
    """
    breaker_router = _BreakerRouterLike(threshold=3)
    calls: list = []

    async def llm_operation():
        return await breaker_router.call_external(
            operation_name="route_llm_request",
            call_func=_hanging_call,
            counter=calls,
            timeout=0.05,
            retries=0,
        )

    result, error = await fast_handler.with_retry(operation=llm_operation)

    assert result is None
    assert error is not None
    assert error.action == ErrorAction.FAIL
    assert error.error_code == PROVIDER_CIRCUIT_OPEN
    # Three timeouts open the breaker; the fourth attempt never reaches the
    # provider. Three is therefore the correct count here, and it is still
    # three MORE than the pre-fix zero-retry behaviour.
    assert len(calls) == 3
