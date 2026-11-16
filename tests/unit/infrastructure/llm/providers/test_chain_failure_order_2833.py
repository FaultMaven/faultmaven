"""Which failure a fallback chain raises, and which failures mark a provider
unhealthy (#2833, stage C1 of #2832; order (A), ruled 2026-11-16).

Before: the chain raised whatever failed last, so [primary 503, fallback
request-scoped 400] raised the 400 (a terminal 502, never counted by the
breaker), and the registry's health counted that 400 as an outage.

Every test drives ``route_request`` on a two-provider registry whose providers
are stubs that raise or answer; nothing reaches a network.
"""

from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.exceptions import (
    CallShapeUnsupported,
    LLMException,
    RawModelContentRefused,
)
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.infrastructure.llm.providers.registry import (
    ProviderHealth,
    ProviderRegistry,
    ProviderState,
)
from faultmaven.infrastructure.llm.router import LLMRouter
from tests.model_content import vetted

pytestmark = [pytest.mark.unit, pytest.mark.llm]

# The ruled order (A), highest first.
_FAILURES = {
    "503": lambda: LLMException("upstream 503", status_code=503),
    "quota": lambda: LLMException(
        "insufficient_quota", status_code=429, provider_error_code="insufficient_quota"
    ),
    "throttle": lambda: LLMException("rate limited", status_code=429),
    "400": lambda: LLMException("bad request shape", status_code=400),
    "call-shape": lambda: CallShapeUnsupported("cannot carry a conversation"),
}
_ORDER = list(_FAILURES)


class _Stub:
    """A provider whose every send raises a fresh ``failure()``, kept in
    ``raised``, or answers ``response``."""

    def __init__(self, failure=None, response=None):
        self.raised = []

        def send(**_):
            self.raised.append(failure())
            raise self.raised[-1]

        self._generate = AsyncMock(
            side_effect=send if failure else None, return_value=response
        )


def _registry(primary, fallback, health=ProviderHealth.UNKNOWN):
    registry = ProviderRegistry(settings=None)
    registry._providers = {"primary": primary, "fallback": fallback}
    registry._fallback_chain = ["primary", "fallback"]
    registry._provider_states = {
        name: ProviderState(name=name, health=health)
        for name in registry._fallback_chain
    }
    registry._initialized = True
    return registry


@pytest.fixture(autouse=True)
def _no_metering():
    with patch(
        "faultmaven.infrastructure.llm.providers.registry.record_provider_call",
        lambda *a, **k: None,
    ):
        yield


async def _raised(registry):
    with pytest.raises(Exception) as raised:
        await registry.route_request(prompt=vetted("why is node-3 down?"))
    return raised.value


# Pin 1 -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("second", _ORDER)
@pytest.mark.parametrize("first", _ORDER)
async def test_the_chain_raises_its_highest_ranked_failure(first, second):
    """Every ordered pair: the higher rank wins, the later one on a tie."""
    primary, fallback = _Stub(_FAILURES[first]), _Stub(_FAILURES[second])
    raised = await _raised(_registry(primary, fallback))

    earlier_wins = _ORDER.index(first) < _ORDER.index(second)
    assert raised is (primary if earlier_wins else fallback).raised[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("pair", [("quota", "503"), ("503", "quota")])
async def test_a_quota_failure_and_a_503_raise_the_503_either_way(pair):
    raised = await _raised(_registry(*(_Stub(_FAILURES[k]) for k in pair)))
    assert raised.status_code == 503 and raised.retryable is True


# Pins 2 and 3 ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", ["400", "throttle"])
async def test_a_503_beside_a_fallback_failure_opens_the_breaker(fallback):
    """[503, 400] raises the 503, as [503, throttle] has since #2816; three
    such calls open the router's shared breaker (asserted on its state)."""
    registry = _registry(_Stub(_FAILURES["503"]), _Stub(_FAILURES[fallback]))
    with patch(
        "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
    ):
        router = LLMRouter()
        for turn in range(3):
            with pytest.raises(LLMException) as raised:
                await router.route(prompt=vetted(f"turn {turn}"), bypass_cache=True)
            assert raised.value.status_code == 503

    assert router.circuit_breaker.state == "open"


@pytest.mark.asyncio
async def test_a_throttle_beside_a_request_scoped_400_raises_the_429():
    raised = await _raised(
        _registry(_Stub(_FAILURES["throttle"]), _Stub(_FAILURES["400"]))
    )
    assert raised.status_code == 429 and raised.retryable is True


# Pin 4 -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", _ORDER)
@pytest.mark.parametrize("answering", ["primary", "fallback"])
async def test_a_low_confidence_response_beats_any_failure(failure, answering):
    low = LLMResponse(
        content="maybe",
        confidence=0.1,
        provider=answering,
        model="m",
        tokens_used=1,
        response_time_ms=1,
    )
    stubs = {
        "primary": _Stub(_FAILURES[failure]),
        "fallback": _Stub(_FAILURES[failure]),
    }
    stubs[answering] = _Stub(response=low)
    registry = _registry(stubs["primary"], stubs["fallback"])

    result = await registry.route_request(prompt=vetted("hello"))

    assert result is low and result.provider == f"{answering} (low-confidence)"


# Pin 5 -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_records_only_failures_the_breaker_counts():
    """Three request-scoped 400s leave a provider HEALTHY; three 503s still
    make the other UNHEALTHY."""
    registry = _registry(
        _Stub(_FAILURES["400"]), _Stub(_FAILURES["503"]), ProviderHealth.HEALTHY
    )
    for _ in range(3):
        await _raised(registry)

    states = registry._provider_states
    assert registry._providers["primary"]._generate.await_count == 3
    assert states["primary"].health is ProviderHealth.HEALTHY
    assert states["primary"].consecutive_failures == 0
    assert states["fallback"].health is ProviderHealth.UNHEALTHY


# Must not change -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_content_refusal_still_aborts_the_chain():
    fallback = _Stub(_FAILURES["503"])
    registry = _registry(_Stub(lambda: RawModelContentRefused("prompt raw")), fallback)

    assert isinstance(await _raised(registry), RawModelContentRefused)
    fallback._generate.assert_not_awaited()
