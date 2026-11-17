"""A tool-loop iteration re-sends the previous request unchanged and only appends.

The rule (prompt-assembly-architecture §3.3, "Within the tool loop"): every
iteration of one turn's tool loop carries the previous iteration's request
byte for byte and adds to the end of it, so a provider's automatic prefix
cache can serve the earlier bytes. Pinned here on the body each adapter hands
its transport, over four consecutive iterations of one turn:

* h0: system + one user message (long static prefix, then the case tail);
* h1: h0 + a tool round;
* h2: h1 + a user nudge;
* h3: h2 + a second tool round.

For every consecutive pair: each top-level body key other than the
conversation field is equal, and the conversation list of iteration k is an
exact prefix of iteration k+1's. Anthropic, whose
explicit breakpoints move with the request's end, is covered in
``test_anthropic_messages.py``.

Transports: the OpenAI wire protocol (OpenAI, Azure, Fireworks, Groq, Local's
OpenAI-compatible route), Gemini ``generateContent`` and Ollama's native
``/api/chat``. Ollama native accepts the conversation but drops ``tools`` and
``tool_choice`` from its body (``local_provider.py``), so its case pins the
conversation prefix and the remaining keys only. Local's raw llama.cpp
``/completion`` fallback refuses a conversation (#2016) and is skipped.

Each transport is captured at its HTTP client (``aiohttp.ClientSession``)
except OpenAI and Azure, whose ``_post_chat_completion`` is patched: no
request leaves the process.
"""

import copy
from unittest.mock import patch

import pytest

from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.azure_provider import AzureProvider
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ProviderConfig
from faultmaven.infrastructure.llm.providers.fireworks_provider import (
    FireworksProvider,
)
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider
from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider
from tests.model_content import vetted_messages
from tests.unit.infrastructure.llm.conftest import make_mock_aiohttp_session

pytestmark = [pytest.mark.unit, pytest.mark.llm]

STATIC_PREFIX = "Static investigation guidance. " * 200
H0_USER = f"{STATIC_PREFIX}\n=== CURRENT CASE ===\nsymptom: 500s on checkout\n"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_file",
            "description": "Search an uploaded file.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "emit_state",
            "description": "The schema tool.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

_OPENAI_REPLY = {
    "choices": [
        {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}
_GEMINI_REPLY = {
    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1},
}
_OLLAMA_REPLY = {
    "model": "llama3.2",
    "message": {"role": "assistant", "content": "ok"},
    "done": True,
    "done_reason": "stop",
}


def _config(name, model, base_url, api_key="test-key"):
    return ProviderConfig(
        name=name,
        api_key=api_key,
        base_url=base_url,
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
    )


def _tool_round(call_id, result, *, gemini):
    """The assistant tool-call message and its tool result, as the engine shapes them."""
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "search_file",
                    "arguments": '{"query": "timeout"}',
                },
            }
        ],
    }
    if gemini:
        # Gemini's echo path: the verbatim parts the API returned, with the
        # functionCall id and the thought signature it issued.
        assistant["provider_metadata"] = {
            "assistant_parts": [
                {
                    "functionCall": {
                        "id": call_id,
                        "name": "search_file",
                        "args": {"query": "timeout"},
                    },
                    "thoughtSignature": f"sig-{call_id}",
                }
            ]
        }
    tool = {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "search_file",
        "content": result,
    }
    return [assistant, tool]


def _histories(*, gemini=False):
    h0 = [
        {"role": "system", "content": "Use the tools, then call the schema tool."},
        {"role": "user", "content": H0_USER},
    ]
    h1 = h0 + _tool_round("call_1", "result 1", gemini=gemini)
    h2 = h1 + [{"role": "user", "content": "Call one of the tools."}]
    h3 = h2 + _tool_round("call_2", "result 2", gemini=gemini)
    return [h0, h1, h2, h3]


# --- transports: each yields a context manager that records request bodies ----


class _Capture:
    def __init__(self):
        self.bodies = []

    def record(self, body):
        # A copy: the rule is about the bytes at send time, not about an object
        # a later iteration could mutate in place.
        self.bodies.append(copy.deepcopy(body))


def _patch_post_chat_completion(capture):
    async def post(self, headers, payload, effective_model):
        capture.record(payload)
        return LLMResponse(
            content="ok",
            confidence=0.9,
            provider="test",
            model=effective_model,
            tokens_used=4,
            response_time_ms=1,
        )

    return patch.object(OpenAIProvider, "_post_chat_completion", post)


def _patch_aiohttp(capture, reply):
    session = make_mock_aiohttp_session(reply)
    real_post = session.post

    def post(*args, **kwargs):
        capture.record(kwargs["json"])
        return real_post(*args, **kwargs)

    session.post = post
    return patch("aiohttp.ClientSession", return_value=session)


def _openai():
    return OpenAIProvider(_config("openai", "gpt-4o", "https://api.openai.com/v1"))


def _azure():
    return AzureProvider(
        _config(
            "azure",
            "gpt-5.6-luna",
            "https://example-resource.services.ai.azure.com/openai/v1",
        )
    )


def _fireworks():
    return FireworksProvider(
        _config(
            "fireworks",
            "accounts/fireworks/models/llama-v3p1-70b-instruct",
            "https://api.fireworks.ai/inference/v1",
        )
    )


def _groq():
    return GroqProvider(
        _config("groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1")
    )


def _local_openai():
    return LocalProvider(
        _config("local", "llama3.2", "http://localhost:5000", api_key=None)
    )


def _local_ollama():
    return LocalProvider(
        _config("local", "llama3.2", "http://localhost:11434/api", api_key=None)
    )


def _gemini(model):
    return GeminiProvider(
        _config("gemini", model, "https://generativelanguage.googleapis.com/v1beta")
    )


# (id, provider factory, patch factory, conversation key, gemini-shaped history)
CASES = [
    ("openai", _openai, lambda c: _patch_post_chat_completion(c), "messages", False),
    ("azure", _azure, lambda c: _patch_post_chat_completion(c), "messages", False),
    (
        "fireworks",
        _fireworks,
        lambda c: _patch_aiohttp(c, _OPENAI_REPLY),
        "messages",
        False,
    ),
    ("groq", _groq, lambda c: _patch_aiohttp(c, _OPENAI_REPLY), "messages", False),
    (
        "local-openai-compatible",
        _local_openai,
        lambda c: _patch_aiohttp(c, _OPENAI_REPLY),
        "messages",
        False,
    ),
    (
        "local-ollama-native",
        _local_ollama,
        lambda c: _patch_aiohttp(c, _OLLAMA_REPLY),
        "messages",
        False,
    ),
    (
        "gemini-classic",
        lambda: _gemini("gemini-1.5-pro"),
        lambda c: _patch_aiohttp(c, _GEMINI_REPLY),
        "contents",
        True,
    ),
    (
        "gemini-3.6",
        lambda: _gemini("gemini-3.6-flash"),
        lambda c: _patch_aiohttp(c, _GEMINI_REPLY),
        "contents",
        True,
    ),
    (
        "gemini-3.7",
        lambda: _gemini("gemini-3.7-flash"),
        lambda c: _patch_aiohttp(c, _GEMINI_REPLY),
        "contents",
        True,
    ),
]


async def _iterate(provider, patcher, gemini):
    """Four consecutive iterations of one turn; the request bodies, in order."""
    capture = _Capture()
    with patcher(capture):
        for history in _histories(gemini=gemini):
            await send_to_model(
                provider,
                messages=vetted_messages(history),
                tools=TOOLS,
                tool_choice="auto",
                cache_prompt=True,
                max_tokens=512,
            )
    return capture.bodies


@pytest.mark.parametrize(
    "make_provider,patcher,conversation_key,gemini",
    [c[1:] for c in CASES],
    ids=[c[0] for c in CASES],
)
async def test_each_tool_loop_iteration_only_appends_to_the_previous_request(
    make_provider, patcher, conversation_key, gemini
):
    bodies = await _iterate(make_provider(), patcher, gemini)

    assert len(bodies) == 4, "one request per iteration"
    for k, (before, after) in enumerate(zip(bodies, bodies[1:])):
        label = f"iteration {k} -> {k + 1}"
        # No key outside the conversation legitimately differs between
        # iterations: tools, tool_choice, system instruction, generation
        # config, model and the rest must be identical.
        assert {key: v for key, v in after.items() if key != conversation_key} == {
            key: v for key, v in before.items() if key != conversation_key
        }, f"{label}: a body key other than {conversation_key!r} changed"
        old, new = before[conversation_key], after[conversation_key]
        assert len(new) > len(old), f"{label}: the conversation did not grow"
        assert new[: len(old)] == old, f"{label}: earlier turns were not re-sent as-is"
