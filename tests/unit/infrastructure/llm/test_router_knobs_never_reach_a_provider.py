"""No router-level knob, and no unset value, reaches a provider's request (#2002).

``LLMRouter.route`` takes keywords that are not provider API fields:
``case_id``, ``bypass_cache``, ``provider_override`` and ``data_type`` (used by
the router layer, never forwarded on a routed call) and ``cache_prompt``,
``reasoning_intent`` and ``min_output_tokens`` (forwarded to the provider,
which translates or pops them). The router is not the only caller:
``milestone_engine`` binds a concrete provider when ``DA_PROVIDER`` is set and
calls ``provider.generate()`` directly. ``BaseLLMProvider._merge_extra_kwargs``
is the provider-side choke point for that path.

The same seam carries a second rule, found in review: the router forwards
``response_format``, ``tools``, ``tool_choice`` and ``messages`` as ``None``
on every call that did not set them, and an adapter that tests key PRESENCE
puts them in the body (``"response_format": null`` on Cohere, HuggingFace and
the local OpenAI-compatible transport; ``format: "json"`` on EVERY Ollama
call). Every kwarg a provider puts in a body must be one the API accepts, and
an unset one is never a value.

Four layers are pinned here:

1. ``ROUTER_CONSUMED_KWARGS`` + ``FORWARDED_KNOB_KWARGS`` +
   ``PROVIDER_API_KWARGS`` are exactly ``route()``'s signature, so a parameter
   cannot be added to the router without being classified.
2. ``LLMRouter.generate`` forwards exactly that surface, reading its keys from
   the constants rather than a copy of its own.
3. The discard helper's own observable: ``kwargs`` after it holds no router-level
   key, whether the knob was set or ``None``.
4. Every adapter and transport, DIRECT (the way the DA tool loop calls a
   provider) and ROUTED (a real ``LLMRouter`` -> real ``ProviderRegistry`` ->
   adapter), with the HTTP boundary a double and sockets blocked: no knob name
   and no knob SENTINEL VALUE anywhere in the request (url, headers, params,
   json), and no ``None``-valued key at a body's top level or inside its
   options/parameters.
"""

import inspect
import json
import re
import socket
from dataclasses import dataclass
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.base import (
    FORWARDED_KNOB_KWARGS,
    PROVIDER_API_KWARGS,
    ROUTER_CONSUMED_KWARGS,
    ROUTER_LEVEL_KWARGS,
    ProviderConfig,
    ReasoningIntent,
)
from faultmaven.infrastructure.llm.providers.registry import (
    PROVIDER_SCHEMA,
    ProviderRegistry,
    ProviderState,
)
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
        router-consumed, a forwarded knob (every provider then discards it
        unless it translates it) or a provider API field (it is forwarded and
        sent). That is the whole mechanism: the discard list cannot lag the
        router."""
        route_keywords = _route_keywords()
        groups = {
            "ROUTER_CONSUMED_KWARGS": ROUTER_CONSUMED_KWARGS,
            "FORWARDED_KNOB_KWARGS": FORWARDED_KNOB_KWARGS,
            "PROVIDER_API_KWARGS": PROVIDER_API_KWARGS,
        }
        for left in groups:
            for right in groups:
                if left < right:
                    both = groups[left] & groups[right]
                    assert not both, f"{left} and {right} both list {sorted(both)}"

        classified = set().union(*groups.values())
        assert route_keywords - classified == set(), (
            f"route() takes keywords the provider discard list does not know: "
            f"{sorted(route_keywords - classified)} — list each in "
            f"ROUTER_CONSUMED_KWARGS, FORWARDED_KNOB_KWARGS or PROVIDER_API_KWARGS"
        )
        assert classified - route_keywords == set(), (
            f"constants name keywords route() no longer takes: "
            f"{sorted(classified - route_keywords)}"
        )
        assert ROUTER_LEVEL_KWARGS == ROUTER_CONSUMED_KWARGS | FORWARDED_KNOB_KWARGS

    def test_the_groups_are_spelled_out(self):
        """The knobs the matrices below drive, spelled out in the test rather
        than read from the constants: deleting one from a constant is then a
        failure here AND a leak the matrices still catch, instead of one that
        quietly drops its own test case."""
        assert set(_CONSUMED_BY_THE_ROUTER) == set(ROUTER_CONSUMED_KWARGS)
        assert set(_FORWARDED_TO_THE_PROVIDER) == set(FORWARDED_KNOB_KWARGS)
        assert set(_KNOB_VALUES) == set(ROUTER_LEVEL_KWARGS)


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


# --- Doubles and values -------------------------------------------------------

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


@dataclass(frozen=True)
class Target:
    """One adapter on one transport."""

    label: str
    provider: str  # PROVIDER_SCHEMA key
    response: object
    base_url: Optional[str] = None  # None: the schema's default
    # The raw llama.cpp transport is only reached by a 404 on the
    # OpenAI-compatible one; the double answers 404, then the real response.
    via_llamacpp_fallback: bool = False


# One entry per registered provider (a provider registered without one fails
# ``test_every_registered_provider_has_a_target``), plus the local provider's
# other two transports: ``PROVIDER_SCHEMA`` only selects its OpenAI-compatible
# one, and the Ollama and llama.cpp bodies are built by different code.
TARGETS = [
    Target("fireworks", "fireworks", _OPENAI_RESP),
    Target("openai", "openai", _OPENAI_RESP),
    Target("openrouter", "openrouter", _OPENAI_RESP),
    Target("groq", "groq", _OPENAI_RESP),
    Target("cohere", "cohere", _COHERE_RESP),
    Target("gemini", "gemini", _GEMINI_RESP),
    Target("anthropic", "anthropic", _ANTHROPIC_RESP),
    Target("huggingface", "huggingface", _HF_RESP),
    Target("local-openai", "local", _OPENAI_RESP),
    Target("local-ollama", "local", _OLLAMA_RESP, "http://localhost:11434/api"),
    Target("local-llamacpp", "local", _LLAMACPP_RESP, via_llamacpp_fallback=True),
]
_LABELS = [t.label for t in TARGETS]
_BY_LABEL = {t.label: t for t in TARGETS}

# What each group is, spelled out. EVERY knob has its own sentinel value, so a
# knob renamed on the way out (a label, a header) is still caught by its value.
# ``max_tokens`` is set above ``min_output_tokens`` wherever a router is in the
# path: the router raises a cap that is below the floor, which would put the
# floor's value in the body legitimately.
_FLOOR = 7777
_CONSUMED_BY_THE_ROUTER = {
    "case_id": "case-SENTINEL-2002",
    "bypass_cache": "bypass-SENTINEL-2002",
    "provider_override": "provider-SENTINEL-2002",
    "data_type": DataType.TRACE_DATA,
}
_FORWARDED_TO_THE_PROVIDER = {
    "cache_prompt": "cache-SENTINEL-2002",
    "reasoning_intent": ReasoningIntent.EXTRACTION,
    "min_output_tokens": _FLOOR,
}
_KNOB_VALUES = {**_CONSUMED_BY_THE_ROUTER, **_FORWARDED_TO_THE_PROVIDER}

# A forwarded knob may be translated to a same-named field by the one
# transport that has it: llama.cpp's ``/completion`` takes ``cache_prompt``
# (KV-cache reuse for a shared prefix). It is the only exemption, and the
# value must be the caller's.
_SENT_UNDER_ITS_OWN_NAME = {("local-llamacpp", "cache_prompt")}


def _sentinel_patterns(knob):
    value = _KNOB_VALUES[knob]
    if isinstance(value, DataType):
        return [re.escape(value.value), re.escape(value.name)]
    if isinstance(value, ReasoningIntent):
        return [re.escape(value.value), re.escape(value.name)]
    if isinstance(value, int):
        return [rf"(?<!\d){value}(?!\d)"]
    return [re.escape(value)]


_TOOLS = [
    {
        "type": "function",
        "function": {"name": "emit", "parameters": {"type": "object"}},
    }
]
_JSON_OBJECT = {"type": "json_object"}

# The call shapes. Router-level knobs are added per test.
_SHAPES = {
    # ``admin_config``'s connection check.
    "plain": {"prompt": "Say hello", "kwargs": {}},
    # The DA tool loop as ``generation.py`` calls it: empty prompt, messages,
    # tools, tool choice.
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


def _provider(target: Target):
    schema = PROVIDER_SCHEMA[target.provider]
    model = "llama3" if target.provider == "local" else schema["default_model"]
    return schema["provider_class"](
        _config(target.provider, model, target.base_url or schema["default_base_url"])
    )


def _session_for(target: Target, make_session):
    if not target.via_llamacpp_fallback:
        return make_session(target.response)
    not_found = make_session({}, status=404)
    served = make_session(target.response)
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(
        side_effect=[not_found.post.return_value, served.post.return_value]
    )
    return session


def _requests_sent(session) -> list:
    """Every POST the provider made: url, headers, params and json, whole."""
    sent = []
    for call in session.post.call_args_list:
        request = {"url": call.args[0] if call.args else None}
        request.update({k: v for k, v in call.kwargs.items() if k != "timeout"})
        sent.append(request)
    return sent


async def _direct(target: Target, make_session, prompt, kwargs) -> list:
    """``provider.generate`` called directly: the door the router does not guard."""
    session = _session_for(target, make_session)
    with patch("aiohttp.ClientSession", return_value=session):
        await _provider(target).generate(prompt, **kwargs)
    return _requests_sent(session)


async def _routed(target: Target, make_session, kwargs) -> list:
    """A real ``LLMRouter`` -> real ``ProviderRegistry`` -> the real adapter."""
    provider = _provider(target)
    registry = ProviderRegistry(settings=None)
    registry._providers = {target.provider: provider}
    registry._fallback_chain = [target.provider]
    registry._provider_states = {target.provider: ProviderState(name=target.provider)}
    registry._initialized = True  # skip environment-based lazy init
    session = _session_for(target, make_session)
    with patch(
        "faultmaven.infrastructure.llm.router.get_registry", return_value=registry
    ):
        router = LLMRouter()
        with patch("aiohttp.ClientSession", return_value=session):
            await router.route(model=provider.config.default_model, **kwargs)
    return _requests_sent(session)


# --- Assertions ---------------------------------------------------------------


def _assert_no_knob_anywhere(requests, knobs, label):
    """No knob NAME and no knob SENTINEL VALUE anywhere in any request."""
    serialised = json.dumps(requests, default=str)
    for knob in knobs:
        if (label, knob) in _SENT_UNDER_ITS_OWN_NAME:
            continue
        assert knob not in serialised, f"{label}: the name {knob!r} is in the request"
        for pattern in _sentinel_patterns(knob):
            assert not re.search(pattern, serialised), (
                f"{label}: {knob}'s value ({pattern}) reached the request: "
                f"{serialised[:400]}"
            )


# Containers whose own keys are request fields, not message content.
_NESTED_FIELD_CONTAINERS = ("options", "parameters", "generationConfig")


def _none_valued_keys(body) -> list:
    """Keys whose value is ``None`` at the body's top level or one level into
    the containers that hold request options. Messages are not scanned: a
    ``content: null`` on an assistant tool-call turn is legitimate."""
    found = [key for key, value in body.items() if value is None]
    for container in _NESTED_FIELD_CONTAINERS:
        inner = body.get(container)
        if isinstance(inner, dict):
            found += [f"{container}.{key}" for key, v in inner.items() if v is None]
    return found


def _assert_no_none_valued_key(requests, label):
    for request in requests:
        body = request.get("json")
        if isinstance(body, dict):
            assert (
                _none_valued_keys(body) == []
            ), f"{label}: an unset value was sent as null: {_none_valued_keys(body)}"


def test_every_registered_provider_has_a_target():
    """The matrices below are derived from ``TARGETS``; a provider registered
    without one would otherwise be skipped."""
    assert set(PROVIDER_SCHEMA) <= {t.provider for t in TARGETS}, (
        "add a Target (with a response double) for the new provider so every "
        "router-level knob is checked against it"
    )


# --- 3. The discard helper's own observable -----------------------------------


class TestTheDiscardHelper:
    """Asserted on ``kwargs`` itself, not on a body: ``_merge_extra_kwargs``
    drops ``None`` values anyway, so a discard that skipped a knob left at
    ``None`` is invisible in every body and would pass a body test."""

    @pytest.mark.parametrize("value", ["set", None], ids=["set", "none"])
    @pytest.mark.parametrize("knob", sorted(_KNOB_VALUES))
    def test_discard_removes_the_knob_whatever_its_value(self, knob, value):
        provider = _provider(_BY_LABEL["fireworks"])
        kwargs = {knob: _KNOB_VALUES[knob] if value else None, "messages": None}

        provider._discard_router_kwargs(kwargs)

        assert knob not in kwargs
        assert kwargs == {"messages": None}, "an API key was touched"

    @pytest.mark.parametrize("knob", sorted(_KNOB_VALUES))
    def test_merge_leaves_the_payload_without_the_knob(self, knob):
        provider = _provider(_BY_LABEL["fireworks"])
        payload = {"model": "m"}

        provider._merge_extra_kwargs(
            payload, {knob: _KNOB_VALUES[knob], "stop": ["x"]}, model="m"
        )

        assert payload == {"model": "m", "stop": ["x"]}


# --- 4a. DIRECT: every adapter, every knob -----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("knob", sorted(_KNOB_VALUES))
@pytest.mark.parametrize("label", _LABELS)
async def test_direct_call_sends_no_router_knob(
    mock_aiohttp_session, label, knob, shape
):
    """Each adapter, called directly the way the DA tool loop calls it, with
    each router-level knob alone. ``provider.generate`` is the door that
    bypasses the router, so nothing upstream is allowed to have stripped it."""
    spec = _SHAPES[shape]

    requests = await _direct(
        _BY_LABEL[label],
        mock_aiohttp_session,
        spec["prompt"],
        {**spec["kwargs"], knob: _KNOB_VALUES[knob]},
    )

    assert requests, "no request was made"
    _assert_no_knob_anywhere(requests, {knob}, label)
    if (label, knob) in _SENT_UNDER_ITS_OWN_NAME:
        assert requests[-1]["json"][knob] == _KNOB_VALUES[knob], (
            f"{label} consumes {knob}: discarding it would cost that "
            f"transport the feature"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("label", _LABELS)
async def test_direct_call_sends_no_router_knob_when_all_are_given(
    mock_aiohttp_session, label, shape
):
    spec = _SHAPES[shape]

    requests = await _direct(
        _BY_LABEL[label],
        mock_aiohttp_session,
        spec["prompt"],
        {**spec["kwargs"], **_KNOB_VALUES},
    )

    _assert_no_knob_anywhere(requests, set(_KNOB_VALUES), label)
    _assert_no_none_valued_key(requests, label)


@pytest.mark.asyncio
@pytest.mark.parametrize("label", _LABELS)
async def test_direct_call_with_every_knob_unset_sends_no_null(
    mock_aiohttp_session, label
):
    """What the router forwards on a plain call: every knob and every optional
    API field present as ``None``. An unset value is "not given", never a
    value to send."""
    unset = {knob: None for knob in _KNOB_VALUES}
    unset.update({"response_format": None, "tools": None, "tool_choice": None})
    unset.update({"messages": None, "model": None})

    requests = await _direct(_BY_LABEL[label], mock_aiohttp_session, "Say hello", unset)

    _assert_no_none_valued_key(requests, label)
    _assert_no_knob_anywhere(requests, set(_KNOB_VALUES), label)


# --- 4b. ROUTED: real router -> real registry -> adapter ---------------------

_ROUTED_ALL_KNOBS = {
    "prompt": "Say hello",
    "max_tokens": 9000,  # above the floor: the router would otherwise raise it
    **_KNOB_VALUES,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("label", _LABELS)
async def test_routed_call_with_every_knob_sends_none_of_them(
    mock_aiohttp_session, label
):
    requests = await _routed(_BY_LABEL[label], mock_aiohttp_session, _ROUTED_ALL_KNOBS)

    assert requests, "no request was made"
    _assert_no_knob_anywhere(requests, set(_KNOB_VALUES), label)
    _assert_no_none_valued_key(requests, label)


@pytest.mark.asyncio
@pytest.mark.parametrize("knob", sorted(_KNOB_VALUES))
@pytest.mark.parametrize("label", _LABELS)
async def test_routed_call_with_each_knob_alone_sends_none_of_them(
    mock_aiohttp_session, label, knob
):
    requests = await _routed(
        _BY_LABEL[label],
        mock_aiohttp_session,
        {"prompt": "Say hello", "max_tokens": 9000, knob: _KNOB_VALUES[knob]},
    )

    _assert_no_knob_anywhere(requests, {knob}, label)
    _assert_no_none_valued_key(requests, label)


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("label", _LABELS)
async def test_routed_call_sends_no_unset_value(mock_aiohttp_session, label, shape):
    """The router forwards ``response_format``, ``tools``, ``tool_choice`` and
    ``messages`` as ``None`` on a call that did not set them. A plain routed
    call must carry no null and no structured-output request at all."""
    spec = _SHAPES[shape]
    # The router turns an empty prompt into ``None``, and a messages-only call
    # reaches the transports that take no ``messages`` field (HuggingFace,
    # Ollama ``/api/generate``, llama.cpp ``/completion``) as ``prompt: null``.
    # That is a separate defect (those transports drop the messages), filed on
    # its own and deliberately not pinned here: this shape carries a prompt.
    kwargs = {
        "prompt": spec["prompt"] or "Investigate the case",
        "max_tokens": 9000,
        **spec["kwargs"],
    }

    requests = await _routed(_BY_LABEL[label], mock_aiohttp_session, kwargs)

    _assert_no_none_valued_key(requests, label)
    for request in requests:
        body = request["json"]
        assert "response_format" not in body, f"{label}: asked for structured output"
        assert "format" not in body, f"{label}: asked for json mode on a plain call"
        assert "response_format" not in body.get("parameters", {})
        assert "response_mime_type" not in body.get("generationConfig", {})


# A request that DOES ask for structured output must still say so: the fix for
# the null above is truthiness, not removal. Each transport's own spelling.
_JSON_OBJECT_IS_SENT_AS = {
    "fireworks": lambda b: b["response_format"] == _JSON_OBJECT,
    "openai": lambda b: b["response_format"] == _JSON_OBJECT,
    "openrouter": lambda b: b["response_format"] == _JSON_OBJECT,
    "groq": lambda b: b["response_format"] == _JSON_OBJECT,
    "cohere": lambda b: b["response_format"] == _JSON_OBJECT,
    "local-openai": lambda b: b["response_format"] == _JSON_OBJECT,
    "local-llamacpp": lambda b: b["response_format"] == _JSON_OBJECT,
    "huggingface": lambda b: b["parameters"]["response_format"] == _JSON_OBJECT,
    "local-ollama": lambda b: b["format"] == "json",
    "gemini": lambda b: (
        b["generationConfig"]["response_mime_type"] == "application/json"
    ),
    # The Messages API has no such field; nothing to carry.
    "anthropic": None,
}


def test_every_target_says_how_json_mode_is_sent():
    assert set(_JSON_OBJECT_IS_SENT_AS) == set(_LABELS)


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [lb for lb in _LABELS if _JSON_OBJECT_IS_SENT_AS[lb]])
async def test_routed_json_object_request_still_asks_for_json(
    mock_aiohttp_session, label
):
    requests = await _routed(
        _BY_LABEL[label],
        mock_aiohttp_session,
        {"prompt": "Say hello", "max_tokens": 9000, "response_format": _JSON_OBJECT},
    )

    # The llama.cpp fallback's first request is the OpenAI-compatible one, which
    # carries the field; its second is a raw prompt with no such field.
    carrier = requests[0]["json"]
    assert _JSON_OBJECT_IS_SENT_AS[label](carrier), carrier
    _assert_no_none_valued_key(requests, label)


# --- Local transports: what the issue's probe measured ------------------------


@pytest.mark.asyncio
async def test_local_llamacpp_fallback_consumes_cache_prompt(mock_aiohttp_session):
    """``cache_prompt`` is a real llama.cpp body field and IS sent there with the
    caller's value, on the second leg; the OpenAI-compatible first leg, which
    does not take it, carries nothing."""
    requests = await _direct(
        _BY_LABEL["local-llamacpp"],
        mock_aiohttp_session,
        "Say hello",
        {"cache_prompt": True},
    )

    assert len(requests) == 2, "the llama.cpp leg was not reached"
    assert "cache_prompt" not in requests[0]["json"]
    assert requests[1]["json"]["cache_prompt"] is True


@pytest.mark.asyncio
async def test_ollama_options_hold_generation_options_only(mock_aiohttp_session):
    """``/api/generate`` takes a prompt and generation options. The DA tool-loop
    shape (messages, tools, tool_choice) must not land in ``options``."""
    spec = _SHAPES["tool_loop"]

    requests = await _direct(
        _BY_LABEL["local-ollama"],
        mock_aiohttp_session,
        spec["prompt"],
        {**spec["kwargs"], **_KNOB_VALUES, "response_format": None},
    )

    (request,) = requests
    assert sorted(request["json"]["options"]) == ["num_predict", "temperature"]
    assert "format" not in request["json"]


@pytest.mark.asyncio
async def test_the_issue_probe_shape_sends_exactly_the_api_fields(mock_aiohttp_session):
    """The reported call, verbatim: the DA tool loop's ``generate_kwargs`` on
    a real ``OpenAIProvider`` (``gpt-4o-mini``, as the probe used). #2002
    measured ``payload keys: ['case_id', 'max_tokens', 'messages', 'model',
    'temperature']``; the body is now exactly the API fields."""
    provider = PROVIDER_SCHEMA["openai"]["provider_class"](
        _config("openai", "gpt-4o-mini", "https://api.openai.com/v1")
    )
    session = mock_aiohttp_session(_OPENAI_RESP)

    with patch("aiohttp.ClientSession", return_value=session):
        await provider.generate(
            "",
            messages=[{"role": "user", "content": "hi"}],
            case_id="case_abc123",
            max_tokens=10,
        )

    (request,) = _requests_sent(session)
    assert sorted(request["json"]) == ["max_tokens", "messages", "model", "temperature"]
