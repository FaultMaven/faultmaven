"""The shared LLM breaker never counts a caller-remedy failure (#2858).

Gemini raises its cut at ``MAX_TOKENS`` retryable, so three of them opened
``LLM_Providers`` while the provider was up. Driven through ``call_external``
with a real breaker at the router's threshold; every other failure is counted
exactly as before.
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


def _cut(category):
    return lambda: LLMException("cut", retryable=True, category=category)


def _wrapped_truncation():
    try:
        raise TRUNCATION()
    except LLMException as cut:
        try:
            raise RuntimeError("structured call failed") from cut
        except RuntimeError as wrapper:
            return wrapper


TRUNCATION = _cut(LLMErrorCategory.OUTPUT_TRUNCATION)
OVERFLOW = _cut(LLMErrorCategory.CONTEXT_OVERFLOW)
UNCLASSIFIED = lambda: LLMException("returned no choices", retryable=True)
THROTTLE = lambda: LLMException("rate limited", status_code=429)
QUOTA = lambda: LLMException("billing", status_code=402, error_code=QUOTA_EXHAUSTED)
REJECTED = lambda: LLMException("bad request", status_code=400)
CALLER_REMEDIES = [TRUNCATION, OVERFLOW, _wrapped_truncation]


@pytest.mark.parametrize(
    ("make", "counted"),
    [(m, False) for m in CALLER_REMEDIES]
    + [(UNCLASSIFIED, True), (THROTTLE, False), (QUOTA, True), (REJECTED, False)],
)
def test_counts_against_breaker(make, counted):
    assert counts_against_breaker(make()) is counted


async def _client_after_three(make):
    client = BaseExternalClient(
        "llm_router",
        LLM_BREAKER_SERVICE,
        circuit_breaker_threshold=LLM_CIRCUIT_BREAKER_THRESHOLD,
    )
    for _ in range(LLM_CIRCUIT_BREAKER_THRESHOLD):
        with pytest.raises(Exception):
            await client.call_external("route", AsyncMock(side_effect=make()))
    return client


@pytest.mark.parametrize("make", CALLER_REMEDIES + [REJECTED])
async def test_a_request_failure_leaves_the_breaker_closed(make):
    client = await _client_after_three(make)

    assert client.circuit_breaker.state == "closed"
    assert client.circuit_breaker.failure_count == 0
    provider = AsyncMock(return_value="ok")
    assert await client.call_external("route", provider) == "ok"
    provider.assert_awaited_once()


async def test_a_truncation_is_not_logged_as_a_rate_limit(caplog):
    with caplog.at_level(logging.WARNING):
        await _client_after_three(TRUNCATION)

    messages = [record.getMessage() for record in caplog.records]
    assert not [m for m in messages if "Rate-limited" in m]
    assert len([m for m in messages if "Caller remedy on" in m]) == 3


@pytest.mark.parametrize(
    ("make", "latched"), [(UNCLASSIFIED, None), (QUOTA, QUOTA_EXHAUSTED)]
)
async def test_a_service_failure_still_opens_the_breaker(make, latched):
    client = await _client_after_three(make)

    assert client.circuit_breaker.state == "open"
    provider = AsyncMock(return_value="ok")
    with pytest.raises(CircuitBreakerError) as refused:
        await client.call_external("route", provider)
    assert refused.value.error_code == latched
    provider.assert_not_awaited()
