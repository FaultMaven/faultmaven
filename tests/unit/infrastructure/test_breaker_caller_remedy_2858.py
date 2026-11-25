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
# Non-retryable, so it takes call_external's fail-fast branch.
OVERFLOW_400 = lambda: LLMException(
    "too long", status_code=400, category=LLMErrorCategory.CONTEXT_OVERFLOW
)
# Service scope outranks the category: still counted, and its code latches.
QUOTA_OVERFLOW = lambda: LLMException(
    "billing",
    status_code=402,
    error_code=QUOTA_EXHAUSTED,
    category=LLMErrorCategory.CONTEXT_OVERFLOW,
)
CALLER_REMEDIES = [TRUNCATION, OVERFLOW, _wrapped_truncation, OVERFLOW_400]


@pytest.mark.parametrize(
    ("make", "counted"),
    [(m, False) for m in CALLER_REMEDIES]
    + [(UNCLASSIFIED, True), (THROTTLE, False), (QUOTA, True), (REJECTED, False)]
    + [(QUOTA_OVERFLOW, True)],
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


def _warnings(caplog, text):
    return [r for r in caplog.records if text in r.getMessage()]


@pytest.mark.parametrize("make", [TRUNCATION, OVERFLOW_400])
async def test_a_caller_remedy_is_logged_as_one_not_a_rate_limit(caplog, make):
    with caplog.at_level(logging.WARNING):
        await _client_after_three(make)

    assert not _warnings(caplog, "Rate-limited")
    assert len(_warnings(caplog, "the request must change, not counted")) == 3
    assert not _warnings(caplog, "request-scoped")


async def test_a_throttle_logs_one_rate_limited_line(caplog):
    client = BaseExternalClient("llm_router", LLM_BREAKER_SERVICE)
    with caplog.at_level(logging.WARNING), pytest.raises(LLMException):
        await client.call_external("route", AsyncMock(side_effect=THROTTLE()))

    assert len(_warnings(caplog, "Rate-limited on")) == 1


@pytest.mark.parametrize(
    ("make", "latched"),
    [(UNCLASSIFIED, None), (QUOTA, QUOTA_EXHAUSTED), (QUOTA_OVERFLOW, QUOTA_EXHAUSTED)],
)
async def test_a_service_failure_still_opens_the_breaker(make, latched):
    client = await _client_after_three(make)

    assert client.circuit_breaker.state == "open"
    provider = AsyncMock(return_value="ok")
    with pytest.raises(CircuitBreakerError) as refused:
        await client.call_external("route", provider)
    assert refused.value.error_code == latched
    provider.assert_not_awaited()
