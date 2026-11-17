"""Which failure a fallback chain raises, and which failures a provider's
health records (#2833, stage C1 of #2832; the amended rule and pins ruled
2026-11-16).

Before: the chain raised a 503 over a quota failure or a context overflow
seen earlier, and the registry's health counted every status-carrying 4xx as
an outage.

Every test drives ``route_request`` on a two-provider registry whose providers
are stubs that raise or answer; nothing reaches a network.
"""

from unittest.mock import AsyncMock, patch

import pytest

from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler
from faultmaven.exceptions import (
    QUOTA_EXHAUSTED,
    CallShapeUnsupported,
    CircuitBreakerError,
    LLMErrorCategory,
    LLMException,
    RawModelContentRefused,
    declared_llm_category,
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


def _wrapped_404():
    wrapper = RuntimeError("model lookup failed")
    wrapper.__cause__ = LLMException("model not found", status_code=404)
    return wrapper


# The ruled ranks, highest first.
_FAILURES = {
    "overflow": lambda: LLMException(
        "context_length_exceeded",
        status_code=400,
        provider_error_code="context_length_exceeded",
    ),
    "quota": lambda: LLMException(
        "insufficient_quota", status_code=429, provider_error_code="insufficient_quota"
    ),
    "503": lambda: LLMException("upstream 503", status_code=503),
    "throttle": lambda: LLMException("rate limited", status_code=429),
    "400": lambda: LLMException("bad request shape", status_code=400),
    "call-shape": lambda: CallShapeUnsupported("cannot carry a conversation"),
}
_ORDER = list(_FAILURES)
_TRUNCATION = lambda: LLMException(  # noqa: E731
    "answer cut at the cap", category=LLMErrorCategory.OUTPUT_TRUNCATION
)
_MALFUNCTION = lambda: LLMException("Groq API returned no choices")  # noqa: E731


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


@pytest.mark.parametrize("second", _ORDER)
@pytest.mark.parametrize("first", _ORDER)
async def test_the_chain_raises_its_highest_ranked_failure(first, second):
    """Every ordered pair: the higher rank wins, the later one on a tie."""
    primary, fallback = _Stub(_FAILURES[first]), _Stub(_FAILURES[second])
    raised = await _raised(_registry(primary, fallback))

    earlier_wins = _ORDER.index(first) < _ORDER.index(second)
    assert raised is (primary if earlier_wins else fallback).raised[0]


@pytest.mark.parametrize(
    "first,second",
    [("truncation", "overflow"), ("overflow", "truncation"), ("truncation", "quota")],
)
async def test_output_truncation_ranks_with_the_overflow(first, second):
    """Truncation ties the overflow, so the later one wins, and it outranks a
    quota failure."""
    make = {**_FAILURES, "truncation": _TRUNCATION}
    primary, fallback = _Stub(make[first]), _Stub(make[second])
    raised = await _raised(_registry(primary, fallback))

    assert raised is (fallback if second != "quota" else primary).raised[0]


# Pin 2 -----------------------------------------------------------------------


async def test_a_503_then_a_context_overflow_raises_the_overflow():
    raised = await _raised(
        _registry(_Stub(_FAILURES["503"]), _Stub(_FAILURES["overflow"]))
    )
    assert raised.status_code == 400
    assert LLMErrorHandler().is_token_limit_error(raised)


async def test_a_503_then_an_output_truncation_raises_the_truncation():
    raised = await _raised(_registry(_Stub(_FAILURES["503"]), _Stub(_TRUNCATION)))
    assert declared_llm_category(raised) is LLMErrorCategory.OUTPUT_TRUNCATION


# Pin 3 and consequence (i) ---------------------------------------------------


@pytest.mark.parametrize("pair", [("503", "quota"), ("quota", "503"), ("quota", "400")])
async def test_quota_is_raised_and_latched_on_the_breaker(pair):
    """Three such calls open the router's breaker with ``QUOTA_EXHAUSTED``
    latched, so the next call is refused with it (asserted on state)."""
    registry = _registry(*(_Stub(_FAILURES[k]) for k in pair))
    with patch(
        "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
    ):
        router = LLMRouter()
        for turn in range(3):
            with pytest.raises(LLMException) as raised:
                await router.route(prompt=vetted(f"turn {turn}"), bypass_cache=True)
            assert raised.value.error_code == QUOTA_EXHAUSTED
        with pytest.raises(CircuitBreakerError) as refused:
            await router.route(prompt=vetted("turn 4"), bypass_cache=True)

    assert router.circuit_breaker.state == "open"
    assert refused.value.error_code == QUOTA_EXHAUSTED


# Pin 4 and consequence (ii) --------------------------------------------------


@pytest.mark.parametrize("failure", ["malfunction", "503"])
async def test_health_records_a_malfunction_and_a_503(failure):
    make = _MALFUNCTION if failure == "malfunction" else _FAILURES["503"]
    registry = _registry(_Stub(make), _Stub(make), ProviderHealth.HEALTHY)
    for _ in range(3):
        await _raised(registry)

    for state in registry._provider_states.values():
        assert state.consecutive_failures == 3
        assert state.health is ProviderHealth.UNHEALTHY


@pytest.mark.parametrize(
    "make",
    [
        _FAILURES["400"],
        _FAILURES["throttle"],
        _FAILURES["call-shape"],
        lambda: LLMException("model not found", status_code=404),
        _wrapped_404,
    ],
    ids=["400", "throttle", "call-shape", "404", "404-on-the-cause"],
)
async def test_health_never_records_a_throttle_a_refusal_or_a_4xx(make):
    registry = _registry(_Stub(make), _Stub(make), ProviderHealth.HEALTHY)
    for _ in range(3):
        await _raised(registry)

    assert registry._providers["primary"]._generate.await_count == 3
    for state in registry._provider_states.values():
        assert state.consecutive_failures == 0
        assert state.health is ProviderHealth.HEALTHY


# Pin 5: unchanged ------------------------------------------------------------


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


async def test_a_content_refusal_still_aborts_the_chain():
    fallback = _Stub(_FAILURES["503"])
    registry = _registry(_Stub(lambda: RawModelContentRefused("prompt raw")), fallback)

    assert isinstance(await _raised(registry), RawModelContentRefused)
    fallback._generate.assert_not_awaited()
