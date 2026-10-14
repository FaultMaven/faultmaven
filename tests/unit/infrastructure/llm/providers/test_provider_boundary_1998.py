"""The provider boundary checks model-bound content on every adapter (#1998).

``BaseLLMProvider.generate`` is a template method: it checks the call's
content, reports what it does not accept, and hands the adapter's
``_generate`` plain values. These tests drive every concrete adapter the
package ships through it, with ``_generate`` replaced by a double — the
adapter's transport, as far as the boundary is concerned — and sockets
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

And the set of adapters is pinned, each one inheriting ``generate`` from the
base: an adapter that overrode it would step around the boundary.
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
from faultmaven.infrastructure.llm.providers import base
from faultmaven.infrastructure.llm.providers.base import (
    BaseLLMProvider,
    LLMResponse,
    ProviderConfig,
    RawModelContentWarning,
)
from tests.model_content import forged, vetted, vetted_messages

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
def _no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a boundary test reached the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def metric():
    with patch.object(base, "llm_model_content_violations") as counter:
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


async def _generate(adapter: BaseLLMProvider, call: Dict[str, Any]):
    call = dict(call)
    prompt = call.pop("prompt")
    return await adapter.generate(prompt, **call)


def test_the_adapter_set_is_every_shipped_provider():
    """A positive control: an empty enumeration would pass everything below."""
    assert {cls.__name__ for cls in ADAPTER_CLASSES} == ADAPTERS


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
def test_every_adapter_routes_through_the_template(cls):
    """No adapter defines ``generate``: the boundary is the only way in."""
    owners = [klass.__name__ for klass in cls.__mro__ if "generate" in vars(klass)]
    assert owners == ["BaseLLMProvider"], (
        f"{cls.__name__} resolves generate() from {owners[0]}: implement "
        "_generate and inherit the boundary"
    )
    assert cls.generate is BaseLLMProvider.generate
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
        await _generate(adapter, _call(**overrides))

    transport.assert_awaited_once()  # warn-only: the call is still sent
    labels = [c.kwargs for c in metric.labels.call_args_list]
    assert {"argument": argument, "kind": kind} in [
        {"argument": item["argument"], "kind": item["kind"]} for item in labels
    ]
    assert all(item["content"] == "raw" for item in labels)
    assert all(item["provider"] == adapter.provider_name for item in labels)
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
        await _generate(_adapter(cls), _minted_call())

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
        await _generate(_adapter(cls), call)

    args, kwargs = transport.await_args
    sent = {"prompt": args[0], **kwargs}
    strings = _str_values(sent)
    assert (".prompt", SECRET) in strings
    assert (".system", "system text") in strings
    assert (".messages[3].content", SECRET) in strings
    assert (".messages[2].tool_calls[0].function.arguments", '{"q": "x"}') in strings
    not_exact = [path for path, value in strings if type(value) is not str]
    assert not not_exact, f"{cls.__name__}._generate got a str subclass at {not_exact}"
    # The caller's own values are untouched.
    assert type(call["messages"][1]["content"]).__name__ == "ModelText"


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("case", sorted(VIOLATIONS))
async def test_refusal_raises_before_the_transport(cls, case, metric, monkeypatch):
    monkeypatch.setattr(base, "REFUSE_RAW_MODEL_CONTENT", True)
    overrides, (argument, kind) = VIOLATIONS[case]
    transport = AsyncMock(return_value=_response())
    with patch.object(cls, "_generate", transport):
        with pytest.raises(TypeError, match=f"{argument} {kind}") as raised:
            await _generate(_adapter(cls), _call(**overrides))
    transport.assert_not_called()
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_refusal_still_sends_minted_content(cls, metric, monkeypatch):
    monkeypatch.setattr(base, "REFUSE_RAW_MODEL_CONTENT", True)
    transport = AsyncMock(return_value=_response())
    with patch.object(cls, "_generate", transport):
        await _generate(_adapter(cls), _minted_call())
    transport.assert_awaited_once()


async def test_a_prompt_of_none_is_accepted(metric):
    cls = ADAPTER_CLASSES[0]
    transport = AsyncMock(return_value=_response())
    with (
        patch.object(cls, "_generate", transport),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await _adapter(cls).generate(
            None, messages=vetted_messages([{"role": "user", "content": "x"}])
        )
    assert not [w for w in recorded if issubclass(w.category, RawModelContentWarning)]
    assert transport.await_args.args[0] is None


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
    # And the set names nothing no adapter reads (tools/tool_choice are named
    # parameters on several adapters, so they are read without kwargs).
    unread = base.GENERATE_KWARGS - set(read) - {"tools", "tool_choice"}
    assert not unread, f"the closed set names keywords no adapter reads: {unread}"


async def test_the_warning_points_at_the_caller_of_generate(metric):
    """stacklevel: the warning names the line that called ``generate``."""
    cls = ADAPTER_CLASSES[0]
    with (
        patch.object(cls, "_generate", AsyncMock(return_value=_response())),
        warnings.catch_warnings(record=True) as recorded,
    ):
        warnings.simplefilter("always")
        await _adapter(cls).generate("raw")  # the line the warning must name
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
    monkeypatch.setattr(base, "llm_model_content_violations", counter)
    cls = ADAPTER_CLASSES[0]
    with (
        patch.object(cls, "_generate", AsyncMock(return_value=_response())),
        pytest.warns(RawModelContentWarning),
    ):
        await _adapter(cls).generate(SECRET)
    sample = registry.get_sample_value(
        "llm_model_content_violations_total",
        {
            "provider": _adapter(cls).provider_name,
            "argument": "prompt",
            "kind": "raw",
            "content": "raw",
        },
    )
    assert sample == 1.0


# ---------------------------------------------------------------------------
# #2015 review fixes
# ---------------------------------------------------------------------------


def test_a_subclass_that_defines_generate_is_refused():
    """F3: the boundary cannot be stepped around by a subclass, anywhere."""
    with pytest.raises(TypeError, match="defines generate"):

        class _Around(ADAPTER_CLASSES[0]):
            async def generate(self, prompt, **kwargs):
                return await self._generate(prompt, **kwargs)

    with pytest.raises(TypeError, match="defines generate"):
        type("_AroundToo", (BaseLLMProvider,), {"generate": lambda *a, **k: None})

    class _Fine(ADAPTER_CLASSES[0]):  # only _generate: accepted
        async def _generate(self, prompt, **kwargs):
            return _response()

    assert _Fine.generate is BaseLLMProvider.generate


class _Role(str, __import__("enum").Enum):
    """A ``(str, Enum)``: ``str()`` of a member is not its value."""

    USER = "the enum's value"


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_unwrapping_keeps_a_str_subclass_value(cls, metric):
    """F4: unwrap with ``str.__str__``: ``str(_Role.USER)`` is ``"_Role.USER"``."""
    transport = AsyncMock(return_value=_response())
    with patch.object(cls, "_generate", transport), warnings.catch_warnings():
        warnings.simplefilter("ignore", RawModelContentWarning)
        await _adapter(cls).generate(
            _Role.USER, messages=[{"role": "user", "content": _Role.USER}]
        )
    args, kwargs = transport.await_args
    assert args[0] == "the enum's value" and type(args[0]) is str
    assert kwargs["messages"][0]["content"] == "the enum's value"
    assert type(kwargs["messages"][0]["content"]) is str


async def test_an_unknown_kwarg_name_is_reported_only_as_an_identifier(metric, caplog):
    cls = ADAPTER_CLASSES[0]
    with (
        patch.object(cls, "_generate", AsyncMock(return_value=_response())),
        caplog.at_level(logging.WARNING),
        pytest.warns(RawModelContentWarning) as recorded,
    ):
        await _adapter(cls).generate(vetted("x"), **{SECRET: 1, "case_id": "c"})
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
        await _adapter(cls).generate(None, messages=[echo])
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
        await _adapter(cls).generate("raw")
    assert (recorded[0].filename, recorded[0].lineno) == (__file__, line)
