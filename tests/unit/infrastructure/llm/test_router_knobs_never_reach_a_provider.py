"""No router-level knob ever reaches a provider's request body (#2002).

``LLMRouter.route`` takes keywords that only the router consumes (``case_id``,
``bypass_cache``, ``provider_override``, ``data_type``, ``cache_prompt``,
``reasoning_intent``, ``min_output_tokens``). They are not provider API fields.
The router strips most of them before it reaches a provider, but it is not the
only caller: ``milestone_engine`` binds a concrete provider when
``DA_PROVIDER`` is set and calls ``provider.generate()`` directly, with
``case_id`` (and, with no ``DA_MODEL``, ``provider_override``) in its kwargs.
``BaseLLMProvider._merge_extra_kwargs`` is the choke point for that path, and
it used to discard the reasoning knobs and ``cache_prompt`` only, so
``case_id`` went out in the body of every OpenAI-compatible adapter.

Three layers are pinned here:

1. ``ROUTER_ONLY_KWARGS`` + ``PROVIDER_API_KWARGS`` are exactly ``route()``'s
   signature, so a parameter cannot be added to the router without being
   classified (a new knob is discarded by every provider the moment it is
   listed; an unlisted one fails this file).
2. ``LLMRouter.generate`` forwards exactly that surface, reading its keys from
   the constants rather than a copy of its own.
3. Every registered adapter (derived from ``PROVIDER_SCHEMA``, plus the local
   provider's three transports) called with each knob puts none of them in its
   request body. Network is blocked; the HTTP boundary is a double.
"""

import inspect
import json
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.base import (
    PROVIDER_API_KWARGS,
    ROUTER_ONLY_KWARGS,
    LLMResponse,
    ProviderConfig,
    ReasoningIntent,
)
from faultmaven.infrastructure.llm.providers.registry import PROVIDER_SCHEMA
from faultmaven.infrastructure.llm.router import LLMRouter
from faultmaven.models import DataType

pytestmark = [pytest.mark.unit, pytest.mark.llm]


@pytest.fixture(autouse=True)
def _network_blocked(monkeypatch):
    """Nothing here may open a socket: the HTTP boundary is a double, and a
    provider that escaped it would otherwise reach a real endpoint."""

    def _refuse(*args, **kwargs):
        raise RuntimeError("socket blocked: the HTTP double was bypassed")

    monkeypatch.setattr(socket.socket, "connect", _refuse)


# --- 1. The constants ARE route()'s signature --------------------------------


def _route_keywords() -> set:
    params = set(inspect.signature(LLMRouter.route).parameters)
    return params - {"self", "prompt"}


class TestTheConstantsAreRoutesSignature:
    def test_every_route_keyword_is_classified_exactly_once(self):
        """Adding a parameter to ``route()`` fails here until it is listed as
        router-only (every provider then discards it) or as a provider API
        field (it is forwarded and sent). That is the whole mechanism: the
        discard list cannot lag the router."""
        route_keywords = _route_keywords()
        classified = ROUTER_ONLY_KWARGS | PROVIDER_API_KWARGS

        assert not (ROUTER_ONLY_KWARGS & PROVIDER_API_KWARGS), (
            "a keyword cannot be both router-only and a provider API field: "
            f"{sorted(ROUTER_ONLY_KWARGS & PROVIDER_API_KWARGS)}"
        )
        assert route_keywords - classified == set(), (
            f"route() takes keywords the provider discard list does not know: "
            f"{sorted(route_keywords - classified)} — list each in "
            f"ROUTER_ONLY_KWARGS (not an API field) or PROVIDER_API_KWARGS"
        )
        assert classified - route_keywords == set(), (
            f"constants name keywords route() no longer takes: "
            f"{sorted(classified - route_keywords)}"
        )

    def test_the_named_knobs_are_router_only(self):
        """The knobs the adapter matrix below drives, spelled out in the test
        rather than read from the constant: deleting one from
        ``ROUTER_ONLY_KWARGS`` is then a failure here AND a leak the matrix
        still catches, instead of one that quietly drops its own test case."""
        assert set(_KNOB_VALUES) == set(ROUTER_ONLY_KWARGS)


# --- 2. generate() forwards exactly that surface -----------------------------


@pytest.fixture
def router():
    registry = MagicMock()
    registry.get_available_providers.return_value = ["openai"]
    registry.get_fallback_chain.return_value = ["openai"]
    with patch(
        "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
    ):
        yield LLMRouter()


class TestGenerateForwardsTheRouteSurface:
    @pytest.mark.asyncio
    async def test_every_route_keyword_given_to_generate_reaches_route(self, router):
        """Iterates ``route()``'s REAL signature: a keyword ``route`` gains and
        ``generate`` does not forward fails here by name."""
        router.route = AsyncMock(return_value="response")
        sent = {name: f"value-for-{name}" for name in _route_keywords()}

        await router.generate("a prompt", **sent)

        router.route.assert_awaited_once_with(prompt="a prompt", **sent)

    @pytest.mark.asyncio
    async def test_a_key_route_does_not_take_is_not_forwarded(self, router):
        router.route = AsyncMock(return_value="response")

        await router.generate("a prompt", model="m", not_a_route_keyword=1)

        router.route.assert_awaited_once_with(prompt="a prompt", model="m")

    @pytest.mark.asyncio
    async def test_absent_keys_take_routes_own_defaults(self, router):
        """``generate`` no longer restates route()'s defaults (1000 / 0.7 /
        False); an omitted key must reach ``route`` omitted."""
        router.route = AsyncMock(return_value="response")

        await router.generate("a prompt")

        router.route.assert_awaited_once_with(prompt="a prompt")


# --- 3. Every adapter, every knob --------------------------------------------

_OPENAI_RESP = {
    "choices": [
        {"message": {"content": "ok", "role": "assistant"}, "finish_reason": "stop"}
    ],
    "usage": {"total_tokens": 10},
}
_COHERE_RESP = {
    "message": {"content": "ok"},
    "finish_reason": "COMPLETE",
    "usage": {"tokens": {"input_tokens": 5, "output_tokens": 5}},
}
_GEMINI_RESP = {
    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"candidatesTokenCount": 5},
}
_ANTHROPIC_RESP = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 5},
}
_HF_RESP = [{"generated_text": "ok"}]
_OLLAMA_RESP = {"response": "ok", "eval_count": 1, "done_reason": "stop"}
_LLAMACPP_RESP = {"content": "ok", "tokens_predicted": 1, "stopped_eos": True}

# One response shape per registered provider. A provider added to the registry
# without an entry here fails ``test_every_registered_provider_has_a_double``
# rather than silently dropping out of the matrix.
_RESPONSE_BY_PROVIDER = {
    "fireworks": _OPENAI_RESP,
    "openai": _OPENAI_RESP,
    "openrouter": _OPENAI_RESP,
    "groq": _OPENAI_RESP,
    "local": _OPENAI_RESP,  # bare host: the OpenAI-compatible transport
    "cohere": _COHERE_RESP,
    "gemini": _GEMINI_RESP,
    "anthropic": _ANTHROPIC_RESP,
    "huggingface": _HF_RESP,
}

# A knob a transport genuinely SENDS under its own name is consumed there, not
# leaked: llama.cpp's ``/completion`` takes ``cache_prompt`` as a body field
# (KV-cache reuse for a shared prefix). Nothing else is allowed.
_CONSUMED_UNDER_ITS_OWN_NAME = {("local-llamacpp", "cache_prompt")}

_STRING_SENTINELS = {
    "case_id": "case-SENTINEL-2002",
    "provider_override": "provider-SENTINEL",
}

# One value per knob, shaped as a real caller shapes it.
_KNOB_VALUES = {
    "case_id": _STRING_SENTINELS["case_id"],
    "provider_override": _STRING_SENTINELS["provider_override"],
    "data_type": DataType.LOGS_AND_ERRORS,
    "cache_prompt": True,
    "bypass_cache": True,
    "reasoning_intent": ReasoningIntent.EXTRACTION,
    "min_output_tokens": 500,
}

_TOOLS = [
    {
        "type": "function",
        "function": {"name": "emit", "parameters": {"type": "object"}},
    }
]

# The two call shapes that matter: a plain prompt (``admin_config``'s
# connection check), and the DA tool loop exactly as ``generation.py`` calls it
# (empty prompt, messages, tools, forced tool choice).
_SHAPES = {
    "plain": {"prompt": "Say hello", "kwargs": {}},
    "tool_loop": {
        "prompt": "",
        "kwargs": {
            "messages": [{"role": "user", "content": "investigate"}],
            "tools": _TOOLS,
            "tool_choice": "auto",
        },
    },
}


def _config(name, model, base_url):
    return ProviderConfig(
        name=name,
        api_key="test-key",
        base_url=base_url,
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
    )


def _provider_from_schema(name):
    schema = PROVIDER_SCHEMA[name]
    return schema["provider_class"](
        _config(name, schema["default_model"], schema["default_base_url"])
    )


def _local_provider(base_url):
    return PROVIDER_SCHEMA["local"]["provider_class"](
        _config("local", "llama3", base_url)
    )


def _keys_of(value):
    """Every dict key anywhere in *value*, however deeply nested."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from _keys_of(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _keys_of(inner)


def _assert_no_knob_in(body, knobs, label):
    keys = set(_keys_of(body))
    leaked = {
        knob
        for knob in knobs
        if knob in keys and (label, knob) not in _CONSUMED_UNDER_ITS_OWN_NAME
    }
    assert not leaked, f"{label}: router-level knob(s) {sorted(leaked)} in the body"
    # Values too: a knob renamed on the way out would still carry its sentinel.
    serialised = json.dumps(body, default=str)
    for knob, sentinel in _STRING_SENTINELS.items():
        assert sentinel not in serialised, f"{label}: {knob}'s value reached the body"


async def _sent_body(make_session, provider, response_data, prompt, kwargs):
    """Run generate() against the HTTP double; return the body it POSTed.

    ``make_session`` is the shared ``mock_aiohttp_session`` fixture
    (tests/unit/infrastructure/llm/conftest.py)."""
    session = make_session(response_data)
    with patch("aiohttp.ClientSession", return_value=session):
        await provider.generate(prompt, **kwargs)
    return session.post.call_args.kwargs["json"]


async def _sent_bodies_through_llamacpp_fallback(
    make_session, provider, prompt, kwargs
):
    """Drive ``generate`` down the OpenAI-compatible → llama.cpp fallback: the
    first POST answers 404 (the server is up and does not serve that path), the
    second is the raw ``/completion`` call. Returns both bodies, in order."""
    not_found = make_session({}, status=404)
    served = make_session(_LLAMACPP_RESP)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(
        side_effect=[not_found.post.return_value, served.post.return_value]
    )
    with patch("aiohttp.ClientSession", return_value=session):
        result = await provider.generate(prompt, **kwargs)
    assert isinstance(result, LLMResponse) and result.content == "ok"
    return [call.kwargs["json"] for call in session.post.call_args_list]


def test_every_registered_provider_has_a_double():
    """The matrix below is derived from ``PROVIDER_SCHEMA``; a provider
    registered without a response double would otherwise be skipped."""
    assert set(PROVIDER_SCHEMA) == set(_RESPONSE_BY_PROVIDER), (
        "register a response double for the new provider in "
        "_RESPONSE_BY_PROVIDER so every router-level knob is checked against it"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("knob", sorted(_KNOB_VALUES))
@pytest.mark.parametrize("name", sorted(PROVIDER_SCHEMA))
async def test_no_registered_adapter_sends_a_router_knob(
    mock_aiohttp_session, name, knob, shape
):
    """Each adapter, called directly the way the DA tool loop calls it, with
    each router-level knob alone. ``provider.generate`` is the door that
    bypasses the router, so nothing upstream is allowed to have stripped it."""
    provider = _provider_from_schema(name)
    spec = _SHAPES[shape]

    body = await _sent_body(
        mock_aiohttp_session,
        provider,
        _RESPONSE_BY_PROVIDER[name],
        spec["prompt"],
        {**spec["kwargs"], knob: _KNOB_VALUES[knob]},
    )

    _assert_no_knob_in(body, {knob}, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("name", sorted(PROVIDER_SCHEMA))
async def test_no_registered_adapter_sends_any_router_knob_when_all_are_given(
    mock_aiohttp_session, name, shape
):
    provider = _provider_from_schema(name)
    spec = _SHAPES[shape]

    body = await _sent_body(
        mock_aiohttp_session,
        provider,
        _RESPONSE_BY_PROVIDER[name],
        spec["prompt"],
        {**spec["kwargs"], **_KNOB_VALUES},
    )

    _assert_no_knob_in(body, set(_KNOB_VALUES), name)


# The local provider speaks three protocols; ``PROVIDER_SCHEMA`` only selects
# the first. Each is driven explicitly (the ``local`` row above is the
# OpenAI-compatible transport).


@pytest.mark.asyncio
@pytest.mark.parametrize("knob", sorted(_KNOB_VALUES))
async def test_local_ollama_native_transport_sends_no_router_knob(
    mock_aiohttp_session, knob
):
    """Ollama's ``/api/generate`` merges leftover kwargs into ``options``, so
    this is the transport where a leaked knob lands somewhere other than the
    top level; ``_keys_of`` walks it."""
    provider = _local_provider("http://localhost:11434/api")

    body = await _sent_body(
        mock_aiohttp_session,
        provider,
        _OLLAMA_RESP,
        "Say hello",
        {knob: _KNOB_VALUES[knob]},
    )

    assert "options" in body, "the Ollama native path was not the one exercised"
    _assert_no_knob_in(body, {knob}, "local-ollama")


@pytest.mark.asyncio
@pytest.mark.parametrize("knob", sorted(_KNOB_VALUES))
async def test_local_llamacpp_fallback_transport_sends_no_router_knob(
    mock_aiohttp_session, knob
):
    """The raw ``/completion`` fallback. ``cache_prompt`` is a real llama.cpp
    body field and IS sent there, with the caller's value; every other knob is
    discarded on both legs of the fallback."""
    provider = _local_provider("http://localhost:5000")

    bodies = await _sent_bodies_through_llamacpp_fallback(
        mock_aiohttp_session, provider, "Say hello", {knob: _KNOB_VALUES[knob]}
    )

    assert len(bodies) == 2, "the llama.cpp leg was not reached"
    openai_compatible_body, llamacpp_body = bodies
    _assert_no_knob_in(openai_compatible_body, {knob}, "local-openai-compatible")
    _assert_no_knob_in(llamacpp_body, {knob}, "local-llamacpp")
    if knob == "cache_prompt":
        assert llamacpp_body["cache_prompt"] is True, (
            "llama.cpp consumes cache_prompt — discarding it would cost the "
            "fallback its prompt-prefix caching"
        )


@pytest.mark.asyncio
async def test_local_llamacpp_fallback_with_every_knob(mock_aiohttp_session):
    provider = _local_provider("http://localhost:5000")

    bodies = await _sent_bodies_through_llamacpp_fallback(
        mock_aiohttp_session, provider, "Say hello", dict(_KNOB_VALUES)
    )

    openai_compatible_body, llamacpp_body = bodies
    _assert_no_knob_in(openai_compatible_body, set(_KNOB_VALUES), "local-openai")
    _assert_no_knob_in(llamacpp_body, set(_KNOB_VALUES), "local-llamacpp")


@pytest.mark.asyncio
async def test_the_issue_probe_shape_sends_exactly_the_api_fields(mock_aiohttp_session):
    """The reported call, verbatim: the DA tool loop's ``generate_kwargs`` on
    a real ``OpenAIProvider`` (``gpt-4o-mini``, as the probe used). #2002
    measured ``payload keys: ['case_id', 'max_tokens', 'messages', 'model',
    'temperature']``; the body is now exactly the API fields."""
    provider = PROVIDER_SCHEMA["openai"]["provider_class"](
        _config("openai", "gpt-4o-mini", "https://api.openai.com/v1")
    )

    body = await _sent_body(
        mock_aiohttp_session,
        provider,
        _OPENAI_RESP,
        "",
        {
            "messages": [{"role": "user", "content": "hi"}],
            "case_id": "case_abc123",
            "max_tokens": 10,
        },
    )

    assert sorted(body) == ["max_tokens", "messages", "model", "temperature"]
