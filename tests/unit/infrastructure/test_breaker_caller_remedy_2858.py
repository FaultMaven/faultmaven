"""The shared LLM breaker never counts a caller-remedy failure (#2858).

A failure that declares ``CONTEXT_OVERFLOW`` or ``OUTPUT_TRUNCATION`` is a
verdict on the request: the provider answered, and only the caller changing the
request (a smaller prompt, a bigger cap) helps. Gemini raises its cut at
``MAX_TOKENS`` retryable, so on ``main`` three of them opened ``LLM_Providers``
and every later call was refused while the provider was up. Driven through
``call_external`` with a real breaker at the router's shipped threshold; every
other failure is counted exactly as before.
"""

import logging
from unittest.mock import AsyncMock

import pytest

from faultmaven.exceptions import (
    LLM_BREAKER_SERVICE,
    QUOTA_EXHAUSTED,
    CircuitBreakerError,
    LLMErrorCategory,
    LLMException,
    counts_against_breaker,
)
from faultmaven.infrastructure.base_client import BaseExternalClient
from faultmaven.infrastructure.llm.router import LLM_CIRCUIT_BREAKER_THRESHOLD

pytestmark = pytest.mark.unit


class _Client(BaseExternalClient):
    def __init__(self):
        super().__init__(
            client_name="llm_router",
            service_name=LLM_BREAKER_SERVICE,
            circuit_breaker_threshold=LLM_CIRCUIT_BREAKER_THRESHOLD,
        )


def _truncation():
    # Gemini's structured cut at MAX_TOKENS (gemini.py).
    return LLMException(
        "cut at the output cap",
        retryable=True,
        category=LLMErrorCategory.OUTPUT_TRUNCATION,
    )


def _overflow():
    return LLMException(
        "prompt too large",
        retryable=True,
        category=LLMErrorCategory.CONTEXT_OVERFLOW,
    )


def _wrapped_truncation():
    try:
        raise _truncation()
    except LLMException as cut:
        try:
            raise RuntimeError("structured call failed") from cut
        except RuntimeError as wrapper:
            return wrapper


def _unclassified():
    return LLMException("returned no choices", retryable=True)


def _throttle():
    return LLMException("rate limited", status_code=429)


def _quota():
    return LLMException("billing", status_code=402, error_code=QUOTA_EXHAUSTED)


def _request_rejected():
    return LLMException("bad request", status_code=400)


@pytest.mark.parametrize(
    ("make", "counted"),
    [
        (_truncation, False),
        (_overflow, False),
        (_wrapped_truncation, False),
        (_unclassified, True),
        (_throttle, False),
        (_quota, True),
        (_request_rejected, False),
    ],
)
def test_counts_against_breaker(make, counted):
    assert counts_against_breaker(make()) is counted


async def _fail_three_times(client, make):
    provider = AsyncMock(
        side_effect=[make() for _ in range(LLM_CIRCUIT_BREAKER_THRESHOLD)]
    )
    for _ in range(LLM_CIRCUIT_BREAKER_THRESHOLD):
        with pytest.raises(Exception):
            await client.call_external("route_llm_request", provider, retries=0)
    return provider


@pytest.mark.parametrize("make", [_truncation, _overflow, _wrapped_truncation])
async def test_caller_remedies_leave_the_breaker_closed(make):
    client = _Client()
    await _fail_three_times(client, make)

    assert client.circuit_breaker.state == "closed"
    assert client.circuit_breaker.failure_count == 0
    # A fourth call reaches the provider.
    provider = AsyncMock(return_value="ok")
    assert await client.call_external("route_llm_request", provider) == "ok"
    provider.assert_awaited_once()


async def test_a_truncation_is_not_logged_as_a_rate_limit(caplog):
    client = _Client()
    with caplog.at_level(logging.WARNING):
        await _fail_three_times(client, _truncation)

    messages = [record.getMessage() for record in caplog.records]
    assert not [m for m in messages if "Rate-limited" in m]
    assert len([m for m in messages if "Caller remedy on" in m]) == 3


@pytest.mark.parametrize(
    ("make", "latched"),
    [(_unclassified, None), (_quota, QUOTA_EXHAUSTED)],
)
async def test_a_service_failure_still_opens_the_breaker(make, latched):
    client = _Client()
    await _fail_three_times(client, make)

    assert client.circuit_breaker.state == "open"
    provider = AsyncMock(return_value="ok")
    with pytest.raises(CircuitBreakerError) as refused:
        await client.call_external("route_llm_request", provider)
    assert refused.value.error_code == latched
    provider.assert_not_awaited()


async def test_a_request_scoped_rejection_is_still_not_counted():
    client = _Client()
    await _fail_three_times(client, _request_rejected)

    assert client.circuit_breaker.state == "closed"
    assert client.circuit_breaker.failure_count == 0
