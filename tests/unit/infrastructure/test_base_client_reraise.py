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
