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

import pytest

from faultmaven.core.investigation.llm_error_handler import (
    ErrorAction,
    LLMErrorHandler,
    RetryConfig,
)
from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.base_client import BaseExternalClient


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
