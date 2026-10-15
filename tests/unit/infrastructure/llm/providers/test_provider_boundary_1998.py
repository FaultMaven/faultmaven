"""The model dispatch checks model-bound content for every adapter (#1998).

``dispatch.send_to_model`` is the one way a call reaches a provider: it checks
the call's content, reports what it does not accept, and hands the adapter's
``_generate`` plain values. These tests drive every concrete adapter the
package ships through it, with ``_generate`` replaced by a double — the
adapter's transport, as far as the dispatch is concerned — and sockets
blocked, so nothing here can reach a provider.

For each adapter:

* each kind of unaccepted content is reported three ways — a WARNING log line,
  the ``llm_model_content_violations`` metric (``content="raw"``) and a
  ``RawModelContentWarning`` — none of which carries the content — and the
  call is still sent;
* correctly minted content is not reported, and ``_generate`` receives exact
  ``str`` values;
* with ``REFUSE_RAW_MODEL_CONTENT`` on, each violation raises ``TypeError``
  and the transport is never called.

And the set of adapters is pinned, none with a public send method: whatever a
provider class or instance defines besides ``_generate``, the dispatch never
calls it.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
import socket
import warnings
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

import faultmaven.infrastructure.llm.providers as providers_package
from faultmaven.infrastructure.llm import dispatch
from faultmaven.infrastructure.llm.dispatch import (
    RawModelContentWarning,
    send_to_model,
)
from faultmaven.infrastructure.llm.providers import base
from faultmaven.infrastructure.llm.providers.base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
)
from tests.model_content import forged, vetted, vetted_messages
from tests.utils import sockets_refused

pytestmark = [pytest.mark.unit, pytest.mark.llm, pytest.mark.security]

#: Content that must never appear in a log line, a warning or a metric label.
SECRET = "alice@example.com 10.9.8.7 SECRET-CONTENT-1998"

#: Every concrete adapter the package ships, by class name.
ADAPTERS = {
    "AnthropicProvider",
    "CohereProvider",
    "FireworksProvider",
    "GeminiProvider",
    "GroqProvider",
    "HuggingFaceProvider",
    "LocalProvider",
    "OpenAIProvider",
    "OpenRouterProvider",
}


def _concrete_adapters() -> List[type]:
    """Every ``BaseLLMProvider`` subclass the package defines (all concrete)."""
    for module in pkgutil.iter_modules(providers_package.__path__):
        importlib.import_module(f"{providers_package.__name__}.{module.name}")
    found, stack = [], [BaseLLMProvider]
    while stack:
        for sub in stack.pop().__subclasses__():
            stack.append(sub)
            if sub.__module__.startswith(providers_package.__name__ + "."):
                found.append(sub)
    return sorted(set(found), key=lambda cls: cls.__name__)


ADAPTER_CLASSES = _concrete_adapters()


def _adapter(cls: type) -> BaseLLMProvider:
    return cls(
        ProviderConfig(
            name=cls.__name__.removesuffix("Provider").lower(),
            api_key="test-key-not-real",
            base_url="http://127.0.0.1:9",
            models=["model-x"],
            default_model="model-x",
        )
    )


def _response() -> LLMResponse:
    return LLMResponse(
        content="ok",
        confidence=1.0,
        provider="p",
        model="model-x",
        tokens_used=1,
        response_time_ms=1,
    )


@pytest.fixture(autouse=True)
def _no_network():
    """No connection leaves this file's tests, by any path (#1998): on a
    MonkeyPatch of its own, so a test's monkeypatch.undo() cannot lift it."""
    with sockets_refused():
        yield


@pytest.fixture
def metric():
    with patch.object(dispatch, "llm_model_content_violations") as counter:
        yield counter


def _tool_call(arguments: Any) -> Dict[str, Any]:
    return {
        "id": "call_1",
        "type": "function",
        "function": {"name": "search_file", "arguments": arguments},
    }


def _minted_call() -> Dict[str, Any]:
    """Every content channel the boundary checks, correctly minted."""
    return {
        "prompt": vetted(SECRET),
        "messages": vetted_messages(
            [
                {"role": "system", "content": "fixed"},
                {"role": "user", "content": SECRET},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            **_tool_call('{"q": "x"}'),
                            "provider_metadata": {"thought_signature": "s"},
                        }
                    ],
                    "provider_metadata": {"assistant_parts": []},
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "search_file",
                    "content": SECRET,
                },
            ]
        ),
        "system": vetted("system text"),
        "preamble": vetted("preamble text"),
        "stop_sequences": [vetted("END", "non_case"), vetted("STOP", "non_case")],
        "tools": [{"type": "function", "function": {"name": "search_file"}}],
        "tool_choice": "auto",
        "max_tokens": 64,
        "reasoning_intent": None,
        "cache_prompt": True,
    }


#: name -> (call overrides, the (argument, kind) the boundary must report).
VIOLATIONS: Dict[str, tuple] = {
    "raw_prompt": ({"prompt": SECRET}, ("prompt", "raw")),
    "raw_message_list": (
        {"messages": [{"role": "user", "content": SECRET}]},
        ("messages.content", "raw"),
    ),
    "forged_prompt": ({"prompt": forged(SECRET)}, ("prompt", "forged")),
    "forged_content": (
        {"messages": [{"role": "user", "content": forged(SECRET)}]},
        ("messages.content", "forged"),
    ),
    "structured_content": (
        {"messages": [{"role": "user", "content": [{"text": SECRET}]}]},
        ("messages.content", "not_text"),
    ),
    "unknown_message_key": (
        {"messages": [{"role": "user", "content": vetted("x"), "case_id": "c"}]},
        ("messages", "unknown_key"),
    ),
    "unknown_role": (
        {"messages": [{"role": "developer", "content": vetted("x")}]},
        ("messages.role", "bad_role"),
    ),
    "provider_metadata_off_assistant": (
        {
            "messages": [
                {"role": "user", "content": vetted("x"), "provider_metadata": {}}
            ]
        },
        ("messages", "unknown_key"),
    ),
    "raw_arguments": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "tool_calls": [_tool_call(SECRET)],
                }
            ]
        },
        ("messages.tool_calls.function.arguments", "raw"),
    ),
    "case_minted_arguments": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "tool_calls": [_tool_call(vetted("{}"))],
                }
            ]
        },
        ("messages.tool_calls.function.arguments", "not_model_output"),
    ),
    "raw_system": ({"system": SECRET}, ("system", "raw")),
    "raw_preamble": ({"preamble": SECRET}, ("preamble", "raw")),
    # D5: every stop sequence is text the model is sent, as ``system`` is.
    "raw_stop_sequence": (
        {"stop_sequences": [vetted("END", "non_case"), SECRET]},
        ("stop_sequences", "raw"),
    ),
    "stop_sequences_not_a_list": (
        {"stop_sequences": SECRET},
        ("stop_sequences", "bad_shape"),
    ),
    "unknown_kwarg": ({"case_id": "case-123"}, ("kwargs", "unknown_kwarg")),
    # F1: the reasoning artifacts an adapter echoes in place of the content.
    "raw_anthropic_assistant_content": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "provider_metadata": {
                        "assistant_content": [{"type": "text", "text": SECRET}]
                    },
                }
            ]
        },
        ("messages.provider_metadata", "raw"),
    ),
    "raw_gemini_assistant_parts": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "provider_metadata": {"assistant_parts": [{"text": SECRET}]},
                }
            ]
        },
        ("messages.provider_metadata", "raw"),
    ),
    "raw_thought_signature": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "tool_calls": [
                        {
                            **_tool_call(vetted("{}", "model_output")),
                            "provider_metadata": {"thought_signature": SECRET},
                        }
                    ],
                }
            ]
        },
        ("messages.tool_calls.provider_metadata", "raw"),
    ),
    "raw_metadata_key": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "provider_metadata": {
                        vetted("assistant_content", "model_output"): [
                            {
                                vetted("type", "model_output"): vetted(
                                    "text", "model_output"
                                ),
                                SECRET: 1,
                            }
                        ]
                    },
                }
            ]
        },
        ("messages.provider_metadata", "raw"),
    ),
    # MUT-1b: a raw key that looks like an identifier is still raw text.
    "raw_identifier_shaped_metadata_key": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "provider_metadata": {
                        "assistant_content": [
                            {
                                vetted("type", "model_output"): vetted(
                                    "text", "model_output"
                                )
                            }
                        ]
                    },
                }
            ]
        },
        ("messages.provider_metadata", "raw"),
    ),
    "case_minted_metadata": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "provider_metadata": {"assistant_parts": [{"text": vetted("x")}]},
                }
            ]
        },
        ("messages.provider_metadata", "not_model_output"),
    ),
    # λ: a tool result's content is model-bound like any other.
    "raw_tool_result_content": (
        {
            "messages": [
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "name": "search_file",
                    "content": SECRET,
                }
            ]
        },
        ("messages.content", "raw"),
    ),
    # Identifier channels carry no text.
    "text_in_tool_call_id": (
        {
            "messages": [
                {"role": "tool", "tool_call_id": SECRET, "content": vetted("x")}
            ]
        },
        ("messages.tool_call_id", "bad_id"),
    ),
    "text_in_tool_name": (
        {"messages": [{"role": "tool", "name": SECRET, "content": vetted("x")}]},
        ("messages.name", "bad_id"),
    ),
    "text_in_call_id": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "tool_calls": [
                        {**_tool_call(vetted("{}", "model_output")), "id": SECRET}
                    ],
                }
            ]
        },
        ("messages.tool_calls.id", "bad_id"),
    ),
    "text_in_function_name": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": SECRET,
                                "arguments": vetted("{}", "model_output"),
                            },
                        }
                    ],
                }
            ]
        },
        ("messages.tool_calls.function.name", "bad_id"),
    ),
    "empty_tool_call_id": (
        {"messages": [{"role": "tool", "tool_call_id": "", "content": vetted("x")}]},
        ("messages.tool_call_id", "bad_id"),
    ),
}


def _call(**overrides) -> Dict[str, Any]:
    call = _minted_call()
    if "messages" in overrides or "prompt" in overrides:
        call.pop("messages")
        call["prompt"] = None
    call.update(overrides)
    return call


async def _send(adapter: BaseLLMProvider, call: Dict[str, Any]):
    return await send_to_model(adapter, **call)


def test_the_adapter_set_is_every_shipped_provider():
    """A positive control: an empty enumeration would pass everything below."""
    assert {cls.__name__ for cls in ADAPTER_CLASSES} == ADAPTERS


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
def test_every_adapter_implements_only_the_private_send(cls):
    """A provider has no public send method: the dispatch is the only way in."""
    assert not hasattr(cls, "generate"), f"{cls.__name__} has a public generate()"
    assert not hasattr(BaseLLMProvider, "generate")
    assert not inspect.isabstract(cls), f"{cls.__name__} implements no _generate"
    assert cls._generate is not BaseLLMProvider._generate


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("case", sorted(VIOLATIONS))
async def test_a_violation_is_reported_and_sent(cls, case, metric, caplog):
    overrides, (argument, kind) = VIOLATIONS[case]
    adapter = _adapter(cls)
    transport = AsyncMock(return_value=_response())
    with (
        patch.object(cls, "_generate", transport),
        caplog.at_level(logging.WARNING),
        pytest.warns(RawModelContentWarning) as recorded,
    ):
        await _send(adapter, _call(**overrides))

    transport.assert_awaited_once()  # warn-only: the call is still sent
    labels = [c.kwargs for c in metric.labels.call_args_list]
    assert {"argument": argument, "kind": kind} in [
        {"argument": item["argument"], "kind": item["kind"]} for item in labels
    ]
    assert all(item["content"] == "raw" for item in labels)
    # The label is the adapter's LABEL, read statically: provider_name's value,
    # without running provider code.
    assert all(
        item["provider"] == adapter.LABEL == adapter.provider_name for item in labels
    )
    assert metric.labels.return_value.inc.call_count == len(labels)

    lines = [r.getMessage() for r in caplog.records if "#1998" in r.getMessage()]
    assert len(lines) == 1 and f"{argument} {kind}" in lines[0]
    assert f"{argument} {kind}" in str(recorded[0].message)
    # Where and what kind, never the content.
    for text in lines + [str(w.message) for w in recorded] + [str(labels)]:
        assert SECRET not in text and "alice@example.com" not in text


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_minted_content_is_not_reported(cls, metric, caplog):
    transport = AsyncMock(return_value=_response())
    with (
        patch.object(cls, "_generate", transport),
        caplog.at_level(logging.WARNING),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await _send(_adapter(cls), _minted_call())

    assert not [w for w in recorded if issubclass(w.category, RawModelContentWarning)]
    metric.labels.assert_not_called()
    assert not [r for r in caplog.records if "#1998" in r.getMessage()]
    transport.assert_awaited_once()


def _str_values(value: Any, path: str = "") -> List[tuple]:
    """Every ``str`` in a payload, with its path."""
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        return [
            item
            for key, inner in value.items()
            for item in _str_values(inner, f"{path}.{key}")
        ]
    if isinstance(value, list):
        return [
            item
            for index, inner in enumerate(value)
            for item in _str_values(inner, f"{path}[{index}]")
        ]
    return []


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_the_adapter_receives_exact_str(cls, metric):
    transport = AsyncMock(return_value=_response())
    call = _minted_call()
    with patch.object(cls, "_generate", transport):
        await _send(_adapter(cls), call)

    sent = dict(transport.await_args.kwargs)
    assert not transport.await_args.args, "the dispatch passes keywords only"
    strings = _str_values(sent)
    assert (".prompt", SECRET) in strings
    assert (".system", "system text") in strings
    assert (".preamble", "preamble text") in strings
    assert (".stop_sequences[1]", "STOP") in strings
    assert (".messages[3].content", SECRET) in strings
    assert (".messages[2].tool_calls[0].function.arguments", '{"q": "x"}') in strings
    not_exact = [path for path, value in strings if type(value) is not str]
    assert not not_exact, f"{cls.__name__}._generate got a str subclass at {not_exact}"
    # The caller's own values are untouched.
    assert type(call["messages"][1]["content"]).__name__ == "ModelText"


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("case", sorted(VIOLATIONS))
async def test_refusal_raises_before_the_transport(cls, case, metric, monkeypatch):
    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    overrides, (argument, kind) = VIOLATIONS[case]
    transport = AsyncMock(return_value=_response())
    with patch.object(cls, "_generate", transport):
        with pytest.raises(TypeError, match=f"{argument} {kind}") as raised:
            await _send(_adapter(cls), _call(**overrides))
    transport.assert_not_called()
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_refusal_still_sends_minted_content(cls, metric, monkeypatch):
    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    transport = AsyncMock(return_value=_response())
    with patch.object(cls, "_generate", transport):
        await _send(_adapter(cls), _minted_call())
    transport.assert_awaited_once()


async def test_a_minted_preamble_reaches_cohere_as_exact_str(metric):
    """MUT-2: Cohere's system prompt is a content keyword: minted, it is
    accepted, and the adapter's ``_generate`` receives an exact ``str``."""
    [cls] = [c for c in ADAPTER_CLASSES if c.__name__ == "CohereProvider"]
    transport = AsyncMock(return_value=_response())
    with (
        patch.object(cls, "_generate", transport),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await send_to_model(
            _adapter(cls), prompt=vetted("q"), preamble=vetted("PREAMBLE")
        )
    assert not [w for w in recorded if issubclass(w.category, RawModelContentWarning)]
    preamble = transport.await_args.kwargs["preamble"]
    assert preamble == "PREAMBLE" and type(preamble) is str


async def test_a_prompt_of_none_is_accepted(metric):
    cls = ADAPTER_CLASSES[0]
    transport = AsyncMock(return_value=_response())
    with (
        patch.object(cls, "_generate", transport),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await send_to_model(
            _adapter(cls),
            prompt=None,
            messages=vetted_messages([{"role": "user", "content": "x"}]),
        )
    assert not [w for w in recorded if issubclass(w.category, RawModelContentWarning)]
    assert transport.await_args.kwargs["prompt"] is None


def test_the_closed_kwarg_set_covers_what_the_adapters_read():
    """Every keyword an adapter reads by name is accepted at the boundary.

    Read from the adapters' source — ``kwargs.pop("x")``, ``kwargs.get("x")``,
    ``kwargs["x"]``, ``"x" in kwargs`` — so a keyword an adapter starts to read
    fails here until the closed set names it.
    """
    import ast

    read: Dict[str, set] = {}
    for cls in ADAPTER_CLASSES + [BaseLLMProvider]:
        tree = ast.parse(inspect.getsource(inspect.getmodule(cls)))
        for node in ast.walk(tree):
            key = None
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"pop", "get"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "kwargs"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                key = node.args[0].value
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "kwargs"
                and isinstance(node.slice, ast.Constant)
            ):
                key = node.slice.value
            elif (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Constant)
                and isinstance(node.ops[0], ast.In)
                and isinstance(node.comparators[0], ast.Name)
                and node.comparators[0].id == "kwargs"
            ):
                key = node.left.value
            if isinstance(key, str):
                read.setdefault(key, set()).add(cls.__name__)
    assert len(read) > 10, "the scan found almost nothing: it is broken"
    # Stray copies of the cap that OpenAI drops so its own selection wins: a
    # caller never passes them, and they are not accepted.
    dropped = {"max_tokens", "max_completion_tokens"}
    unaccepted = set(read) - base.GENERATE_KWARGS - dropped
    assert not unaccepted, f"adapters read keywords the boundary refuses: {unaccepted}"
    # And the set names nothing no adapter reads (the named parameters of
    # _generate are read without kwargs).
    named = {"model", "max_tokens", "temperature", "tools", "tool_choice"}
    unread = base.GENERATE_KWARGS - set(read) - named
    assert not unread, f"the closed set names keywords no adapter reads: {unread}"


def test_the_closed_set_is_a_union_of_disjoint_parts():
    """One classification of every keyword (#2002, #1998): the router's three
    groups and the provider-only options are disjoint, and every accepted or
    discarded set is a union of them."""
    parts = {
        "ROUTER_CONSUMED_KWARGS": base.ROUTER_CONSUMED_KWARGS,
        "FORWARDED_KNOB_KWARGS": base.FORWARDED_KNOB_KWARGS,
        "PROVIDER_API_KWARGS": base.PROVIDER_API_KWARGS,
        "PROVIDER_OPTION_KWARGS": base.PROVIDER_OPTION_KWARGS,
    }
    names = sorted(parts)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            assert (
                not parts[a] & parts[b]
            ), f"{a} and {b} overlap: {parts[a] & parts[b]}"
    assert base.GENERATE_KWARGS == (
        base.FORWARDED_KNOB_KWARGS
        | base.PROVIDER_API_KWARGS
        | base.PROVIDER_OPTION_KWARGS
    )
    assert base.ROUTER_LEVEL_KWARGS == (
        base.ROUTER_CONSUMED_KWARGS | base.FORWARDED_KNOB_KWARGS
    )
    assert not base.GENERATE_KWARGS & base.ROUTER_CONSUMED_KWARGS
    assert set(base.CONTENT_KWARGS) <= base.PROVIDER_OPTION_KWARGS
    assert set(base.CONTENT_LIST_KWARGS) <= base.PROVIDER_OPTION_KWARGS
    assert len(base.PROVIDER_OPTION_KWARGS) == 11


async def test_the_warning_points_at_the_caller_of_the_dispatch(metric):
    """The warning names the line that called ``send_to_model``."""
    cls = ADAPTER_CLASSES[0]
    with (
        patch.object(cls, "_generate", AsyncMock(return_value=_response())),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await send_to_model(_adapter(cls), prompt="raw")  # the line it names
    [warning] = [w for w in recorded if issubclass(w.category, RawModelContentWarning)]
    assert warning.filename == __file__


def _declared_labelnames() -> List[str]:
    """The label names ``shims/metrics.py`` declares for the boundary metric."""
    import ast

    from faultmaven.infrastructure.shims import metrics

    tree = ast.parse(inspect.getsource(metrics))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and [
            getattr(t, "id", None) for t in node.targets
        ] == ["llm_model_content_violations"]:
            [labelnames] = [
                k.value for k in node.value.keywords if k.arg == "labelnames"
            ]
            return [element.value for element in labelnames.elts]
    raise AssertionError("llm_model_content_violations is not declared")


async def test_a_real_counter_with_the_declared_labels_counts_it(monkeypatch):
    """The labels the boundary passes are the ones declared, and it counts.

    The shipped metric is a no-op unless ``ENABLE_METRICS`` was on at import,
    and a mock accepts any label names — so this builds a real Prometheus
    counter, in a private registry, from the declaration itself.
    """
    prometheus_client = pytest.importorskip("prometheus_client")
    registry = prometheus_client.CollectorRegistry()
    counter = prometheus_client.Counter(
        "llm_model_content_violations",
        "probe",
        labelnames=_declared_labelnames(),
        registry=registry,
    )
    monkeypatch.setattr(dispatch, "llm_model_content_violations", counter)
    cls = ADAPTER_CLASSES[0]
    with (
        patch.object(cls, "_generate", AsyncMock(return_value=_response())),
        pytest.warns(RawModelContentWarning),
    ):
        await send_to_model(_adapter(cls), prompt=SECRET)
    sample = registry.get_sample_value(
        "llm_model_content_violations_total",
        {
            "provider": cls.LABEL,
            "argument": "prompt",
            "kind": "raw",
            "content": "raw",
        },
    )
    assert sample == 1.0


# ---------------------------------------------------------------------------
# #2015 review fixes
# ---------------------------------------------------------------------------


async def test_what_a_provider_defines_besides_generate_changes_nothing_checked(
    metric, monkeypatch
):
    """The #2015 round-3 replacements, at runtime: a subclass, a mixin, a
    ``partialmethod``, a ``__getattribute__`` redirect, an instance-dict store,
    a duck-typed class. The dispatch calls nothing on a provider but
    ``_generate``, so with refusal on raw content is refused before every
    transport, and minted content reaches each one plain."""
    import functools

    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    sent: List[Any] = []

    class _Adapter(ADAPTER_CLASSES[0]):
        async def _generate(self, prompt=None, **kwargs):
            sent.append(prompt)
            return _response()

    class _Mixin:
        async def generate(self, prompt=None, **kwargs):
            return await self._generate(prompt=prompt, **kwargs)

        async def _send(self, prompt=None, **kwargs):
            return await self._generate(prompt=prompt, **kwargs)

        send = functools.partialmethod(_send)

    class _Mixed(_Mixin, _Adapter):
        pass

    class _Redirect(_Adapter):
        def __getattribute__(self, name):
            if name == "generate":
                return object.__getattribute__(self, "_generate")
            return object.__getattribute__(self, name)

    class _Duck:
        provider_name = "duck"

        async def _generate(self, prompt=None, **kwargs):
            sent.append(prompt)
            return _response()

    stored = _adapter(_Adapter)
    vars(stored)["generate"] = stored._generate
    stored.__dict__.update(send=stored._generate)
    for provider in (_adapter(_Mixed), _adapter(_Redirect), stored, _Duck()):
        with pytest.raises(TypeError, match="prompt raw"):
            await send_to_model(provider, prompt=SECRET)
        assert not sent, f"{type(provider).__name__}: raw content was sent"
        await send_to_model(provider, prompt=vetted(SECRET))
        assert sent == [SECRET] and type(sent[0]) is str
        sent.clear()


class _Role(str, __import__("enum").Enum):
    """A ``(str, Enum)``: ``str()`` of a member is not its value."""

    USER = "the enum's value"


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_unwrapping_keeps_a_str_subclass_value(cls, metric):
    """F4: unwrap with ``str.__str__``: ``str(_Role.USER)`` is ``"_Role.USER"``."""
    transport = AsyncMock(return_value=_response())
    with patch.object(cls, "_generate", transport), warnings.catch_warnings():
        warnings.simplefilter("ignore", RawModelContentWarning)
        await send_to_model(
            _adapter(cls),
            prompt=_Role.USER,
            messages=[{"role": "user", "content": _Role.USER}],
        )
    kwargs = transport.await_args.kwargs
    assert kwargs["prompt"] == "the enum's value" and type(kwargs["prompt"]) is str
    assert kwargs["messages"][0]["content"] == "the enum's value"
    assert type(kwargs["messages"][0]["content"]) is str


async def test_an_unknown_kwarg_name_is_reported_only_as_an_identifier(metric, caplog):
    cls = ADAPTER_CLASSES[0]
    with (
        patch.object(cls, "_generate", AsyncMock(return_value=_response())),
        caplog.at_level(logging.WARNING),
        pytest.warns(RawModelContentWarning) as recorded,
    ):
        await send_to_model(
            _adapter(cls), prompt=vetted("x"), **{SECRET: 1, "case_id": "c"}
        )
    texts = [r.getMessage() for r in caplog.records] + [
        str(w.message) for w in recorded
    ]
    assert any("case_id" in text for text in texts)
    assert any("?" in text for text in texts)
    for text in texts:
        assert SECRET not in text and "alice@example.com" not in text


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_minted_reasoning_artifacts_reach_the_adapter_as_they_came(cls, metric):
    """F1: ``model_output`` mints the artifacts' leaves, the boundary accepts
    them, and the adapter receives the original structure and values, plain."""
    from faultmaven.infrastructure.llm.providers.base import ToolCall
    from faultmaven.infrastructure.security.model_content import model_output

    metadata = {
        "assistant_content": [
            {"type": "thinking", "thinking": "the disk is full", "signature": "EqQB"},
            {"type": "tool_use", "id": "toolu_1", "input": {"q": "disk", "n": 3}},
        ],
        "assistant_parts": [{"text": "checking", "thoughtSignature": "CiwB"}],
    }
    echo = model_output(
        LLMResponse(
            content="checking",
            confidence=1.0,
            provider="p",
            model="m",
            tokens_used=1,
            response_time_ms=1,
            tool_calls=[
                ToolCall(
                    id="toolu_1",
                    type="function",
                    function={"name": "search_file", "arguments": '{"q": "disk"}'},
                    provider_metadata={"thought_signature": "CikB"},
                )
            ],
            provider_metadata=metadata,
        )
    )
    transport = AsyncMock(return_value=_response())
    with (
        patch.object(cls, "_generate", transport),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await send_to_model(_adapter(cls), prompt=None, messages=[echo])
    assert not [w for w in recorded if issubclass(w.category, RawModelContentWarning)]
    [sent] = transport.await_args.kwargs["messages"]
    assert sent["provider_metadata"] == metadata
    assert sent["tool_calls"][0]["provider_metadata"] == {"thought_signature": "CikB"}
    strings = _str_values(
        {
            "m": sent["provider_metadata"],
            "c": sent["tool_calls"][0]["provider_metadata"],
        }
    )
    assert strings and all(type(value) is str for _, value in strings)
    assert sent["provider_metadata"]["assistant_content"][1]["input"]["n"] == 3


async def test_a_direct_call_names_its_caller(metric):
    cls = ADAPTER_CLASSES[0]
    with (
        patch.object(cls, "_generate", AsyncMock(return_value=_response())),
        pytest.warns(RawModelContentWarning) as recorded,
    ):
        line = inspect.currentframe().f_lineno + 1
        await send_to_model(_adapter(cls), prompt="raw")
    assert (recorded[0].filename, recorded[0].lineno) == (__file__, line)


# ---------------------------------------------------------------------------
# The dispatch's own observables
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_keywords_reach_the_adapter_in_the_callers_order(cls, metric):
    """A transport that merges leftover keywords into its body (llama.cpp's
    ``/completion``) writes them in call order, so the dispatch must not move
    one: a ``messages`` parameter of its own put it behind ``tools`` in that
    body, which the #1998 wire check measured as a changed request."""
    transport = AsyncMock(return_value=_response())
    with patch.object(cls, "_generate", transport):
        await send_to_model(
            _adapter(cls),
            prompt=vetted(""),
            messages=vetted_messages([{"role": "user", "content": "q"}]),
            tools=[{"type": "function", "function": {"name": "search_file"}}],
            tool_choice="auto",
            max_tokens=10,
            cache_prompt=True,
        )
    assert list(transport.await_args.kwargs) == [
        "prompt",
        "model",
        "max_tokens",
        "temperature",
        "messages",
        "tools",
        "tool_choice",
        "cache_prompt",
    ]


def test_register_provider_takes_only_a_provider_class():
    """A7: hygiene, and nothing more — the dispatch checks a call's values
    whatever the provider's class, so this closes nothing it leaves open."""
    from faultmaven.infrastructure.llm.providers.registry import ProviderRegistry
    from tests.model_content import FakeProvider

    registry = ProviderRegistry(settings=None)

    class _Duck:
        async def _generate(self, prompt=None, **kwargs):
            return _response()

    for not_a_provider in (_Duck, _Duck(), FakeProvider()):
        with pytest.raises(TypeError, match="not a BaseLLMProvider subclass"):
            registry.register_provider("duck", not_a_provider)
    registry.register_provider("fake", FakeProvider)
    assert registry._provider_classes == {"fake": FakeProvider}


# ---------------------------------------------------------------------------
# #2023 review fixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_raw_messages_beside_a_minted_prompt_are_reported(cls, metric):
    """ρ: the messages are checked whatever the prompt is. Every grammar case
    above sends ``prompt=None``; the engine's routed tool loop sends a prompt
    beside its messages."""
    transport = AsyncMock(return_value=_response())
    with (
        patch.object(cls, "_generate", transport),
        pytest.warns(RawModelContentWarning) as recorded,
    ):
        await send_to_model(
            _adapter(cls),
            prompt=vetted("q"),
            messages=[{"role": "user", "content": SECRET}],
        )
    assert "messages.content raw" in str(recorded[0].message)


class _Flip(list):
    """A list that iterates as minted once, then raw (#2023 review)."""

    calls = 0

    def __iter__(self):
        type(self).calls += 1
        good = [{"role": "user", "content": vetted("hello")}]
        bad = [{"role": "user", "content": SECRET}]
        return iter(good if type(self).calls == 1 else bad)


class _FlipText(list):
    """A stop-sequence list that iterates as minted once, then raw (M1)."""

    calls = 0

    def __iter__(self):
        type(self).calls += 1
        return iter([vetted("END", "non_case")] if type(self).calls == 1 else [SECRET])


class _FlipTuple(tuple):
    calls = 0

    def __iter__(self):
        type(self).calls += 1
        return iter([vetted("END", "non_case")] if type(self).calls == 1 else [SECRET])


class _LyingMessage(dict):
    def get(self, key, default=None):
        return vetted("hello") if key == "content" else dict.get(self, key, default)


class _SubStr(str):
    pass


class _KwargName(str):
    def __hash__(self):
        return str.__hash__(self)

    def __eq__(self, other):
        return str.__eq__(self, other)


#: I2: values that could answer the check one way and the adapter another.
#: Only exact builtins pass, so each is a violation before any of its methods
#: runs.
_LYING = {
    "list subclass messages": (
        {"messages": _Flip([{"role": "user", "content": SECRET}])},
        ("messages", "bad_shape"),
    ),
    "dict subclass message": (
        {"messages": [_LyingMessage(role="user", content=SECRET)]},
        ("messages", "bad_shape"),
    ),
    "str subclass role": (
        {"messages": [{"role": _SubStr("user"), "content": vetted("x")}]},
        ("messages.role", "bad_role"),
    ),
    "str subclass message key": (
        {"messages": [{"role": "user", _SubStr("content"): vetted("x")}]},
        ("messages", "bad_shape"),
    ),
    # M1: a content list must be an EXACT list or tuple, like the messages.
    "list subclass stop_sequences": (
        {"stop_sequences": _FlipText([SECRET])},
        ("stop_sequences", "bad_shape"),
    ),
    "tuple subclass stop_sequences": (
        {"stop_sequences": _FlipTuple([SECRET])},
        ("stop_sequences", "bad_shape"),
    ),
    "str subclass keyword name": (
        {_KwargName("system"): SECRET},
        ("kwargs", "bad_shape"),
    ),
    "dict subclass tool call": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "tool_calls": [
                        _LyingMessage(_tool_call(vetted("{}", "model_output")))
                    ],
                }
            ]
        },
        ("messages.tool_calls", "bad_shape"),
    ),
    "a non-builtin metadata leaf": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "provider_metadata": {
                        vetted("k", "model_output"): __import__("decimal").Decimal(1)
                    },
                }
            ]
        },
        ("messages.provider_metadata", "bad_shape"),
    ),
    "a str subclass metadata leaf": (
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": vetted("", "model_output"),
                    "provider_metadata": {vetted("k", "model_output"): _SubStr(SECRET)},
                }
            ]
        },
        ("messages.provider_metadata", "bad_shape"),
    ),
}


@pytest.mark.parametrize("case", sorted(_LYING))
async def test_a_lying_container_is_refused_before_the_transport(
    case, metric, monkeypatch
):
    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    _Flip.calls = _FlipText.calls = _FlipTuple.calls = 0
    overrides, (argument, kind) = _LYING[case]
    cls = next(c for c in ADAPTER_CLASSES if c.__name__ == "OpenAIProvider")
    transport = AsyncMock(return_value=_response())
    call = {"prompt": None, **overrides}
    with patch.object(cls, "_generate", transport):
        with pytest.raises(TypeError, match=f"{argument} {kind}"):
            await send_to_model(_adapter(cls), **call)
    transport.assert_not_called()
    assert _Flip.calls == 0, "the check iterated the caller's list subclass"
    assert _FlipText.calls == _FlipTuple.calls == 0, "a content list was iterated"


def test_the_grammar_tables_are_read_only():
    """I3: a public table the check reads cannot be widened at runtime."""
    from faultmaven.infrastructure.security.model_content import (
        FUNCTION_KEYS,
        MESSAGE_KEYS,
        TOOL_CALL_KEYS,
    )

    with pytest.raises(TypeError):
        MESSAGE_KEYS["user"] = MESSAGE_KEYS["user"] | {"x"}  # type: ignore[index]
    with pytest.raises(TypeError):
        MESSAGE_KEYS["developer"] = frozenset({"role", "content"})  # type: ignore[index]
    assert type(TOOL_CALL_KEYS) is frozenset and type(FUNCTION_KEYS) is frozenset
    assert type(base.GENERATE_KWARGS) is frozenset
    assert type(base.CONTENT_KWARGS) is tuple


async def test_a_refusal_is_its_own_type_and_not_retryable(metric, monkeypatch):
    """O1: a refusal is the request's fault, never a provider's."""
    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    with pytest.raises(dispatch.RawModelContentRefused) as raised:
        await send_to_model(_adapter(ADAPTER_CLASSES[0]), prompt=SECRET)
    assert isinstance(raised.value, TypeError)
    assert raised.value.retryable is False


async def test_no_provider_code_runs_before_a_refusal(metric, monkeypatch):
    """O6: the dispatch labels its report with the class's own name. A provider
    property (``provider_name`` is one on every adapter) would run while the
    dispatch's frame holds the raw call, and could read it from there."""
    import sys

    monkeypatch.setattr(dispatch, "REFUSE_RAW_MODEL_CONTENT", True)
    seen: List[Any] = []

    class _Probe(ADAPTER_CLASSES[0]):
        @property
        def provider_name(self):
            seen.append(dict(sys._getframe(1).f_locals))
            return "probe"

        def __getattribute__(self, name):
            seen.append(name)
            return object.__getattribute__(self, name)

    probe = _Probe.__new__(_Probe)
    # The label is the LABEL the adapter class declares, read from the MRO's
    # own namespaces: the probe's property never runs.
    with pytest.raises(
        dispatch.RawModelContentRefused, match=f"^{ADAPTER_CLASSES[0].LABEL}: "
    ):
        await send_to_model(probe, prompt=SECRET, messages=[{"role": "x"}])
    assert seen == [], f"provider code ran before the refusal: {seen}"
