"""A provider's 429 is waited out at the provider boundary, the same way by
every provider, and a throttle is not counted as an outage (#2793).

Before: the OpenAI-compatible family raised on the first 429 without reading
``Retry-After``; three such failures inside one rate-limit window opened the
shared LLM breaker and the turn answered 503 ``LLM_PROVIDER_UNAVAILABLE``.

Every wait here goes through a patched ``asyncio.sleep`` and is asserted by the
duration requested, never by the clock.
"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from multidict import CIMultiDict

from faultmaven.core.investigation.turn_budget import (
    TURN_BUDGET_RESERVE_SECONDS,
    TURN_COMMIT_RESERVE_SECONDS,
    bind_turn_deadline,
)
from faultmaven.exceptions import (
    QUOTA_EXHAUSTED,
    LLMException,
    is_rate_limit_throttle,
)
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers import base
from faultmaven.infrastructure.llm.providers.azure_provider import AzureProvider
from faultmaven.infrastructure.llm.providers.base import (
    RATE_LIMIT_DEFAULT_WAIT_SECONDS,
    RATE_LIMIT_MAX_RETRIES,
    RATE_LIMIT_MAX_WAIT_SECONDS,
    ProviderConfig,
    rate_limit_wait,
)
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider
from faultmaven.infrastructure.llm.providers.registry import (
    PROVIDER_SCHEMA,
    ProviderHealth,
    ProviderRegistry,
    ProviderState,
)
from faultmaven.infrastructure.llm.router import LLMRouter
from tests.model_content import vetted

pytestmark = [pytest.mark.unit, pytest.mark.llm]

_THROTTLE_BODY = (
    '{"error":{"code":"rate_limit_exceeded","message":"Requests to the '
    'deployment have exceeded token rate limit."}}'
)
_QUOTA_BODY = (
    '{"error":{"code":"insufficient_quota","message":"You exceeded your '
    'current quota, please check your plan and billing details."}}'
)

_OPENAI_OK = {
    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}
# A 200 each adapter parses, by the wire format it speaks.
_OK_BODY = {
    "openai": _OPENAI_OK,
    "azure": _OPENAI_OK,
    "openrouter": _OPENAI_OK,
    "groq": _OPENAI_OK,
    "fireworks": _OPENAI_OK,
    "anthropic": {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 1},
    },
    "gemini": {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}]
    },
    "cohere": {
        "message": {"content": "ok"},
        "finish_reason": "COMPLETE",
        "usage": {"tokens": {"input_tokens": 3, "output_tokens": 1}},
    },
    "huggingface": _OPENAI_OK,
}


def _response(status: int, body=None, headers=None):
    response = AsyncMock()
    response.status = status
    response.json = AsyncMock(
        return_value=body if isinstance(body, (dict, list)) else {}
    )
    response.text = AsyncMock(return_value=body if isinstance(body, str) else "")
    response.headers = CIMultiDict(headers or {})
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _session(*responses):
    """A session whose successive POSTs answer ``responses`` in order."""
    session = MagicMock()
    session.post = MagicMock(side_effect=list(responses))
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _throttled(headers=None):
    return _response(429, _THROTTLE_BODY, headers)


def _config(name, base_url, model):
    return ProviderConfig(
        name=name,
        api_key="test-key",
        base_url=base_url,
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
    )


def _azure():
    return AzureProvider(
        _config(
            "azure", "https://example-resource.test.invalid/openai/v1", "gpt-5.4-mini"
        )
    )


@pytest.fixture
def sleep():
    with patch("asyncio.sleep", new_callable=AsyncMock) as slept:
        yield slept


# ---------------------------------------------------------------------------
# 1. The three header shapes, through the Azure adapter
# ---------------------------------------------------------------------------


class _FixedNow(datetime):
    NOW = datetime(2026, 11, 13, 19, 55, 21, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.NOW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,waited",
    [
        ({"Retry-After": "1"}, 1.0),
        ({"retry-after-ms": "1500", "retry-after": "2"}, 1.5),
        ({"Retry-After": format_datetime(_FixedNow.NOW + timedelta(seconds=3))}, 3.0),
    ],
    ids=["seconds", "milliseconds-first", "http-date"],
)
async def test_azure_waits_out_a_429_and_returns_the_200(sleep, headers, waited):
    session = _session(_throttled(headers), _response(200, _OPENAI_OK))
    with (
        patch.object(base, "datetime", _FixedNow),
        patch("aiohttp.ClientSession", return_value=session),
    ):
        result = await send_to_model(_azure(), prompt=vetted("why is node-3 down?"))

    assert result.content == "ok"
    assert session.post.call_count == 2
    assert sleep.await_args_list == [call(waited)]


# ---------------------------------------------------------------------------
# 2. Every provider's POST, and each local transport, uses the one policy
# ---------------------------------------------------------------------------

_SCHEMA_PROVIDERS = [
    pytest.param(name, schema["provider_class"], schema["default_model"], id=name)
    for name, schema in sorted(PROVIDER_SCHEMA.items())
    if name != "local"
]


def test_the_ok_bodies_cover_every_provider():
    """A provider added to the schema is checked below, or fails here."""
    assert set(_OK_BODY) == set(PROVIDER_SCHEMA) - {"local"}


@pytest.mark.asyncio
@pytest.mark.parametrize("name,provider_class,model", _SCHEMA_PROVIDERS)
async def test_every_provider_waits_out_a_429(sleep, name, provider_class, model):
    provider = provider_class(_config(name, f"https://{name}.test.invalid/v1", model))
    session = _session(_throttled({"retry-after": "1"}), _response(200, _OK_BODY[name]))
    with patch("aiohttp.ClientSession", return_value=session):
        result = await send_to_model(provider, prompt=vetted("hello"))

    assert result.content == "ok"
    assert sleep.await_args_list == [call(1.0)]


_LOCAL_OK = {
    "ollama": {"message": {"content": "ok"}, "done_reason": "stop"},
    "openai-compatible": _OPENAI_OK,
    "llamacpp": {"content": "ok", "stopped_eos": True},
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport,base_url,first",
    [
        ("ollama", "http://ollama.test.invalid:11434/api", []),
        ("openai-compatible", "http://vllm.test.invalid:8000/v1", []),
        # Reached only after /v1/chat/completions answers 404.
        ("llamacpp", "http://llamacpp.test.invalid:8080", [404]),
    ],
)
async def test_each_local_transport_waits_out_a_429(sleep, transport, base_url, first):
    provider = LocalProvider(_config("local", base_url, "local-model"))
    session = _session(
        *[_response(status, "not found") for status in first],
        _throttled({"retry-after": "1"}),
        _response(200, _LOCAL_OK[transport]),
    )
    with patch("aiohttp.ClientSession", return_value=session):
        result = await send_to_model(provider, prompt=vetted("hello"))

    assert result.content == "ok"
    assert sleep.await_args_list == [call(1.0)]


# ---------------------------------------------------------------------------
# 3. The policy's bounds: retries, the cap, the turn's spendable budget
# ---------------------------------------------------------------------------


def test_without_a_header_the_wait_doubles_then_stops():
    waits = [
        rate_limit_wait({}, attempt) for attempt in range(RATE_LIMIT_MAX_RETRIES + 1)
    ]
    assert waits == [
        RATE_LIMIT_DEFAULT_WAIT_SECONDS,
        RATE_LIMIT_DEFAULT_WAIT_SECONDS * 2,
        None,
    ]


def test_an_unreadable_header_falls_back_to_the_default():
    assert (
        rate_limit_wait({"retry-after": "soon"}, 0) == RATE_LIMIT_DEFAULT_WAIT_SECONDS
    )


@pytest.mark.asyncio
async def test_a_429_that_outlasts_the_retries_is_raised_as_a_throttle(sleep):
    session = _session(*[_throttled({"retry-after": "1"}) for _ in range(3)])
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(LLMException) as raised:
            await send_to_model(_azure(), prompt=vetted("hello"))

    assert raised.value.status_code == 429
    assert is_rate_limit_throttle(raised.value)
    assert sleep.await_args_list == [call(1.0), call(1.0)]


@pytest.mark.asyncio
async def test_a_wait_past_the_cap_raises_at_once(sleep):
    session = _session(
        _throttled({"retry-after": str(RATE_LIMIT_MAX_WAIT_SECONDS + 1)})
    )
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(LLMException) as raised:
            await send_to_model(_azure(), prompt=vetted("hello"))

    assert raised.value.status_code == 429
    assert session.post.call_count == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after,waits", [("10", False), ("1", True)])
async def test_a_wait_past_the_turns_spendable_budget_raises_at_once(
    sleep, retry_after, waits
):
    """Five seconds spendable: a 10 s wait is refused, a 1 s wait (the control)
    is taken."""
    session = _session(
        _throttled({"retry-after": retry_after}), _response(200, _OPENAI_OK)
    )
    deadline = TURN_BUDGET_RESERVE_SECONDS + TURN_COMMIT_RESERVE_SECONDS + 5.0
    with (
        bind_turn_deadline(deadline),
        patch("aiohttp.ClientSession", return_value=session),
    ):
        if waits:
            assert (await send_to_model(_azure(), prompt=vetted("hi"))).content == "ok"
        else:
            with pytest.raises(LLMException):
                await send_to_model(_azure(), prompt=vetted("hi"))

    assert sleep.await_args_list == ([call(1.0)] if waits else [])


# ---------------------------------------------------------------------------
# 4. A throttle is not an outage; quota exhaustion and a 5xx still are
# ---------------------------------------------------------------------------


def _routed_registry(provider):
    registry = ProviderRegistry(settings=None)
    registry._providers = {"azure": provider}
    registry._fallback_chain = ["azure"]
    registry._provider_states = {"azure": ProviderState(name="azure")}
    registry._initialized = True
    return registry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,opens,unhealthy",
    [
        (lambda: _throttled({"retry-after": "1"}), False, False),
        # A status-carrying 4xx is never recorded on health (#2833
        # consequence (ii)); the breaker still opens and latches quota.
        (lambda: _response(429, _QUOTA_BODY, {"retry-after": "1"}), True, False),
        (lambda: _response(500, '{"error":{"message":"internal"}}'), True, True),
    ],
    ids=["throttle", "insufficient-quota", "5xx"],
)
async def test_three_failed_routes_open_the_breaker_only_for_an_outage(
    sleep, failure, opens, unhealthy
):
    registry = _routed_registry(_azure())
    # Each of the three routes sends until its waits are spent (three POSTs
    # for a 429, one for a 5xx); the fourth route is answered.
    session = _session(*[failure() for _ in range(9)], _response(200, _OPENAI_OK))
    with (
        patch(
            "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
        ),
        patch(
            "faultmaven.infrastructure.llm.providers.registry.record_provider_call",
            lambda *a, **k: None,
        ),
        patch("aiohttp.ClientSession", return_value=session),
    ):
        router = LLMRouter()
        errors = []
        for turn in range(3):
            with pytest.raises(Exception) as raised:
                await router.route(prompt=vetted(f"turn {turn}"), bypass_cache=True)
            errors.append(raised.value)
        posts_before_fourth = session.post.call_count
        breaker_state = router.circuit_breaker.state
        health = registry._provider_states["azure"].health
        if opens:
            with pytest.raises(Exception):
                await router.route(prompt=vetted("turn 3"), bypass_cache=True)
        else:
            result = await router.route(prompt=vetted("turn 3"), bypass_cache=True)
            assert result.content == "ok"

    if opens:
        assert breaker_state == "open"
        assert (health is ProviderHealth.UNHEALTHY) is unhealthy
        # The open breaker refuses the fourth route before any request.
        assert session.post.call_count == posts_before_fourth
    else:
        assert breaker_state == "closed"
        assert health is not ProviderHealth.UNHEALTHY
        assert session.post.call_count == posts_before_fourth + 1
        assert all(is_rate_limit_throttle(e) for e in errors)


def test_quota_exhaustion_at_429_is_not_a_throttle():
    quota = LLMException(
        "OpenAI API error 429: insufficient_quota",
        status_code=429,
        provider_error_code="insufficient_quota",
    )
    assert quota.error_code == QUOTA_EXHAUSTED
    assert not is_rate_limit_throttle(quota)
    assert is_rate_limit_throttle(LLMException("rate limited", status_code=429))
    assert not is_rate_limit_throttle(LLMException("server", status_code=503))


# ---------------------------------------------------------------------------
# 5. Review fixes: the call's deadline, the chain's real failure, quota,
#    non-finite headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wait_the_calls_timeout_cannot_also_afford_is_not_taken(sleep):
    """Two 15 s waits under a 30 s call timeout used to end in a counted
    ``ExternalCallTimeout``. The re-send must get as long as the wait, so a
    15 s wait under 30 s is refused and the throttle raised, uncounted."""
    router = LLMRouter()
    session = _session(_throttled({"retry-after": "15"}), _response(200, _OPENAI_OK))

    async def send():
        return await send_to_model(_azure(), prompt=vetted("hello"))

    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(LLMException) as raised:
            await router.call_external(
                operation_name="route_llm_request",
                call_func=send,
                timeout=30,
                retries=0,
            )

    assert not isinstance(raised.value, TimeoutError)
    assert is_rate_limit_throttle(raised.value)
    assert session.post.call_count == 1
    sleep.assert_not_awaited()
    assert router.circuit_breaker.failure_count == 0


@pytest.mark.parametrize("retry_after,taken", [("4.9", 4.9), ("5", None)])
def test_a_wait_is_taken_only_when_twice_it_fits_the_call(retry_after, taken):
    with patch.object(base, "remaining_call_time", return_value=10.0):
        assert rate_limit_wait({"retry-after": retry_after}, 0) == taken


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "primary,raised_status,counted",
    [
        # A counted outage stays the raised error, so the breaker sees it.
        ((503, '{"error":{"message":"overloaded"}}'), 503, 1),
        # A request-scoped rejection is not counted, so the throttle (the
        # retryable answer) is raised, uncounted; nor is the status-carrying
        # 400 recorded on the primary's health (#2833 consequence (ii)).
        ((400, '{"error":{"type":"invalid_request_error","message":"bad"}}'), 429, 0),
    ],
    ids=["503-counted", "400-request-scoped"],
)
async def test_a_throttled_fallback_raises_the_failure_the_breaker_counts(
    sleep, primary, raised_status, counted
):
    registry = ProviderRegistry(settings=None)
    fallback = OpenAIProvider(
        _config("openai", "https://openai.test.invalid/v1", "gpt-5.4-mini")
    )
    registry._providers = {"azure": _azure(), "openai": fallback}
    registry._fallback_chain = ["azure", "openai"]
    registry._provider_states = {
        name: ProviderState(name=name) for name in registry._fallback_chain
    }
    registry._initialized = True
    session = _session(
        _response(*primary),
        *[_throttled({"retry-after": "1"}) for _ in range(3)],
    )
    with (
        patch(
            "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
        ),
        patch("aiohttp.ClientSession", return_value=session),
    ):
        router = LLMRouter()
        with pytest.raises(LLMException) as raised:
            await router.route(prompt=vetted("hello"), bypass_cache=True)

    assert raised.value.status_code == raised_status
    assert router.circuit_breaker.failure_count == counted
    assert registry._provider_states["azure"].consecutive_failures == counted
    assert registry._provider_states["openai"].consecutive_failures == 0


@pytest.mark.asyncio
async def test_a_quota_429_is_raised_after_one_post_with_no_wait(sleep):
    session = _session(
        _response(429, _QUOTA_BODY, {"retry-after": "1"}), _response(200, _OPENAI_OK)
    )
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(LLMException) as raised:
            await send_to_model(_azure(), prompt=vetted("hello"))

    assert raised.value.error_code == QUOTA_EXHAUSTED
    assert session.post.call_count == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"retry-after": "nan"}, RATE_LIMIT_DEFAULT_WAIT_SECONDS),
        ({"retry-after": "inf"}, RATE_LIMIT_DEFAULT_WAIT_SECONDS),
        ({"retry-after": "-inf"}, RATE_LIMIT_DEFAULT_WAIT_SECONDS),
        ({"retry-after-ms": "nan"}, RATE_LIMIT_DEFAULT_WAIT_SECONDS),
        ({"retry-after-ms": "inf"}, RATE_LIMIT_DEFAULT_WAIT_SECONDS),
        ({"retry-after-ms": "inf", "retry-after": "3"}, 3.0),
    ],
)
def test_a_non_finite_header_names_nothing(headers, expected):
    assert rate_limit_wait(headers, 0) == expected
