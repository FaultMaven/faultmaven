"""A local call that carries ``messages`` is sent as a conversation or refused (#2016).

Two of the three local transports used to take only a prompt, so a call whose
conversation was in ``messages`` (deep analysis, the engine's routed tool loop
with ``prompt=""``) reached them as an empty prompt. The invariant, pinned here
on the real request body each transport POSTs to a server the test binds
itself:

* Ollama native posts ``/api/chat`` and sends the conversation;
* the raw llama.cpp ``/completion`` fallback refuses a conversation before any
  request, with a non-retryable error that names the fix;
* a prompt-only call sends its prompt unchanged on every transport.

The server is the test's own loopback socket (``sockets_refused(own_loopback=
True)``); nothing here can reach a real model server.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.base import ProviderConfig, StopReason
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider
from tests.utils import sockets_refused

pytestmark = [pytest.mark.unit, pytest.mark.llm]

# The shape Ollama documents for a non-streaming /api/chat reply.
_CHAT_REPLY = {
    "model": "llama3.2",
    "message": {"role": "assistant", "content": "chat answer"},
    "done": True,
    "done_reason": "stop",
    "prompt_eval_count": 11,
    "eval_count": 7,
}

_TOOLS = [
    {"type": "function", "function": {"name": "emit", "parameters": {"type": "object"}}}
]
_CONVERSATION = [
    {"role": "system", "content": "You investigate."},
    {"role": "user", "content": "why is it down?"},
]
_TOOL_LOOP_CONVERSATION = _CONVERSATION + [
    {"role": "assistant", "content": ""},
    {"role": "tool", "content": "disk full", "tool_name": "emit"},
]


@asynccontextmanager
async def _server(*, chat_reply=None, completion_reply=None, chat_completions=404):
    """A loopback server that records every request it receives."""
    seen = []

    def route(name, reply, status=200):
        async def handler(request):
            seen.append((name, await request.json()))
            return web.json_response(reply if reply is not None else {}, status=status)

        return handler

    app = web.Application()
    app.router.add_post("/api/chat", route("/api/chat", chat_reply or _CHAT_REPLY))
    app.router.add_post(
        "/v1/chat/completions", route("/v1/chat/completions", {}, chat_completions)
    )
    app.router.add_post(
        "/completion",
        route(
            "/completion",
            completion_reply
            or {"content": "raw answer", "tokens_predicted": 3, "stopped_eos": True},
        ),
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}", seen
    finally:
        await runner.cleanup()


def _provider(base_url: str) -> LocalProvider:
    return LocalProvider(
        ProviderConfig(
            name="local",
            api_key=None,
            base_url=base_url,
            models=["llama3.2"],
            default_model="llama3.2",
            timeout=10,
        )
    )


def _run(coro):
    with sockets_refused(own_loopback=True):
        return asyncio.run(coro)


# --- Ollama native: POST /api/chat ---------------------------------------------


def _native(call_kwargs, **server_kwargs):
    async def go():
        async with _server(**server_kwargs) as (root, seen):
            provider = _provider(f"{root}/api")
            try:
                response = await send_to_model(provider, **call_kwargs)
            except LLMException as exc:
                return exc, seen
            return response, seen

    return _run(go())


def test_native_prompt_only_sends_the_prompt_as_one_user_message():
    response, seen = _native({"prompt": "hello", "max_tokens": 50, "temperature": 0.2})

    ((path, body),) = seen
    assert path == "/api/chat"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["stream"] is False
    assert body["model"] == "llama3.2"
    assert body["options"] == {"num_predict": 50, "temperature": 0.2}
    assert "prompt" not in body
    assert "format" not in body
    assert response.content == "chat answer"
    assert response.tokens_used == 7
    assert response.stop_reason is StopReason.STOP


def test_native_messages_only_with_prompt_none_sends_the_conversation():
    _response, seen = _native({"prompt": None, "messages": _CONVERSATION})

    ((_path, body),) = seen
    assert body["messages"] == _CONVERSATION
    assert "prompt" not in body


def test_native_empty_prompt_with_messages_is_the_tool_loop_shape():
    """The engine's routed tool loop: ``prompt=""`` plus the conversation."""
    _response, seen = _native({"prompt": "", "messages": _CONVERSATION})

    ((_path, body),) = seen
    assert body["messages"] == _CONVERSATION


def test_native_messages_win_over_a_prompt_given_beside_them():
    _response, seen = _native({"prompt": "ignored", "messages": _CONVERSATION})

    ((_path, body),) = seen
    assert body["messages"] == _CONVERSATION
    assert "ignored" not in str(body)


def test_native_tool_loop_sends_the_tool_turn_and_no_tools():
    _response, seen = _native(
        {
            "prompt": "",
            "messages": _TOOL_LOOP_CONVERSATION,
            "tools": _TOOLS,
            "tool_choice": "auto",
        }
    )

    ((_path, body),) = seen
    assert body["messages"] == _TOOL_LOOP_CONVERSATION
    assert body["messages"][-1]["role"] == "tool"
    assert "tools" not in body
    assert "tool_choice" not in body
    assert sorted(body["options"]) == ["num_predict", "temperature"]


@pytest.mark.parametrize(
    "response_format,present",
    [({"type": "json_object"}, True), (None, False)],
    ids=["json", "none"],
)
def test_native_format_json_only_when_response_format_is_truthy(
    response_format, present
):
    _response, seen = _native({"prompt": "hi", "response_format": response_format})

    ((_path, body),) = seen
    assert ("format" in body) is present
    if present:
        assert body["format"] == "json"


def test_native_done_reason_length_is_max_tokens():
    reply = {**_CHAT_REPLY, "done_reason": "length"}
    response, _seen = _native({"prompt": "hi"}, chat_reply=reply)

    assert response.stop_reason is StopReason.MAX_TOKENS


@pytest.mark.parametrize("suffix", ["/api", "/api/chat", "/api/generate"])
def test_native_every_pasted_suffix_posts_to_api_chat(suffix):
    async def go():
        async with _server() as (root, seen):
            await send_to_model(_provider(f"{root}{suffix}"), prompt="hi")
            return seen

    ((path, _body),) = _run(go())
    assert path == "/api/chat"


# --- raw llama.cpp /completion fallback ----------------------------------------


def _fallback(call_kwargs):
    """A bare host: /v1/chat/completions answers 404, so /completion is tried."""

    async def go():
        async with _server() as (root, seen):
            provider = _provider(root)
            try:
                response = await send_to_model(provider, **call_kwargs)
            except LLMException as exc:
                return exc, seen
            return response, seen

    return _run(go())


def test_fallback_prompt_only_still_posts_the_prompt_to_completion():
    response, seen = _fallback({"prompt": "hello", "cache_prompt": True})

    assert [path for path, _ in seen] == ["/v1/chat/completions", "/completion"]
    body = seen[-1][1]
    assert body["prompt"] == "hello"
    assert body["cache_prompt"] is True
    assert response.content == "raw answer"


@pytest.mark.parametrize(
    "call_kwargs",
    [
        {"prompt": None, "messages": _CONVERSATION},
        {"prompt": "", "messages": _CONVERSATION},
        {"prompt": "beside", "messages": _CONVERSATION},
    ],
    ids=["prompt-none", "prompt-empty-tool-loop", "prompt-and-messages"],
)
def test_fallback_refuses_a_conversation_before_any_completion_request(call_kwargs):
    error, seen = _fallback(call_kwargs)

    assert isinstance(error, LLMException)
    assert error.retryable is False
    assert "/v1/chat/completions" in str(error)
    assert "LOCAL_LLM_URL" in str(error)
    # The refusal is itself, not the two-format composite.
    assert "failed with both API formats" not in str(error)
    assert "/completion" not in [path for path, _ in seen]


def test_openai_compatible_transport_still_sends_messages_unchanged():
    async def go():
        async with _server(chat_completions=200) as (root, seen):
            # The 200 reply above is empty, so the call errors after the send;
            # the request body is what this pins.
            with pytest.raises(LLMException):
                await send_to_model(_provider(root), prompt="", messages=_CONVERSATION)
            return seen

    ((path, body),) = _run(go())
    assert path == "/v1/chat/completions"
    assert body["messages"] == _CONVERSATION
