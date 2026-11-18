"""OpenAI GPT-5.6+ caches the investigation prompt's durable prefix only with a breakpoint.

From GPT-5.6, OpenAI (Azure included) matches the prompt cache at message ends
and explicit breakpoints, never inside a message. The durable prefix and this
turn's case share one user message, so the adapter splits that message at
``CACHE_BOUNDARY`` and marks the head with ``prompt_cache_breakpoint``.
Measured on Azure gpt-6-luna-1 (2026-11-17): a second turn read 11,116 cached
tokens without the breakpoint and 32,933 with it.
"""

import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.prompt_cache import CACHE_BOUNDARY
from faultmaven.infrastructure.llm.providers.azure_provider import AzureProvider
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.huggingface import HuggingFaceProvider
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider
from faultmaven.infrastructure.llm.providers.openrouter_provider import (
    OpenRouterProvider,
)
from tests.model_content import vetted, vetted_messages

PREFIX = "You are FaultMaven.\nStanding instructions.\n" + CACHE_BOUNDARY + "\n"
TAIL = "STATE: INVESTIGATING\nCURRENT_TIME: 2026-11-17T10:00:00Z\nThe pod restarts.\n"
SYSTEM = "You are the directed-analysis agent."
BREAKPOINT = {"mode": "explicit"}


def _config(model: str, name: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        api_key="test-key",
        base_url="https://example.test/openai/v1",
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
    )


def _ok(usage=None):
    return {
        "status": 200,
        "json": {
            "choices": [
                {
                    "message": {"content": "ok", "role": "assistant"},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage or {"total_tokens": 10},
        },
    }


def _refuses_breakpoint():
    return {
        "status": 400,
        "text": json.dumps(
            {
                "error": {
                    "message": "Unrecognized request argument supplied: "
                    "prompt_cache_breakpoint",
                    "type": "invalid_request_error",
                    "param": "messages[1].content[0].prompt_cache_breakpoint",
                    "code": None,
                }
            }
        ),
    }


def _session(specs):
    """A session answering *specs* in order; bodies deep-copied as sent."""
    responses = []
    for spec in specs:
        resp = AsyncMock()
        resp.status = spec["status"]
        resp.json = AsyncMock(return_value=spec.get("json", {}))
        resp.text = AsyncMock(return_value=spec.get("text", ""))
        resp.headers = {}
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        responses.append(resp)
    sent = []

    def _post(*args, **kwargs):
        sent.append(copy.deepcopy(kwargs.get("json")))
        return responses[len(sent) - 1]

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, sent


def _loop_messages(extra=()):
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": PREFIX + TAIL},
        *extra,
    ]


async def _send(provider, specs, *, messages=None, prompt="ignored", **kwargs):
    session, sent = _session(specs)
    with patch("aiohttp.ClientSession", return_value=session):
        result = await send_to_model(
            provider,
            prompt=vetted(prompt),
            messages=vetted_messages(messages) if messages is not None else None,
            max_tokens=256,
            **kwargs,
        )
    return result, sent


def _azure():
    return AzureProvider(_config("gpt-6-luna-1", "azure"))


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestBreakpointPlacement:
    async def test_the_first_user_message_is_split_at_the_boundary(self):
        _, sent = await _send(
            _azure(), [_ok()], messages=_loop_messages(), cache_prompt=True
        )

        system, user = sent[0]["messages"]
        assert system == {"role": "system", "content": SYSTEM}
        assert user["content"] == [
            {"type": "text", "text": PREFIX, "prompt_cache_breakpoint": BREAKPOINT},
            {"type": "text", "text": TAIL},
        ]

    async def test_a_prompt_only_call_is_split_too(self):
        """The single shot sends ``prompt`` alone."""
        _, sent = await _send(
            _azure(), [_ok()], messages=None, prompt=PREFIX + TAIL, cache_prompt=True
        )
        assert sent[0]["messages"][0]["content"][0]["prompt_cache_breakpoint"] == (
            BREAKPOINT
        )

    async def test_each_tool_loop_iteration_extends_the_previous_request(self):
        """The marked base is the same bytes on every iteration, and the
        caller's own messages are never mutated."""
        tool_round = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "search_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "found"},
        ]
        first = _loop_messages()
        _, sent_a = await _send(_azure(), [_ok()], messages=first, cache_prompt=True)
        _, sent_b = await _send(
            _azure(), [_ok()], messages=_loop_messages(tool_round), cache_prompt=True
        )
        n = len(sent_a[0]["messages"])
        assert sent_b[0]["messages"][:n] == sent_a[0]["messages"]
        assert first[1]["content"] == PREFIX + TAIL

    @pytest.mark.parametrize(
        "case",
        ["no_cache_prompt", "unsupported_model", "no_boundary", "blank_tail"],
    )
    async def test_nothing_is_marked_when_it_should_not_be(self, case):
        provider = _azure()
        messages = _loop_messages()
        kwargs = {"cache_prompt": True}
        if case == "no_cache_prompt":
            kwargs = {}
        elif case == "unsupported_model":
            provider = OpenAIProvider(_config("gpt-5.4-mini", "openai"))
        elif case == "no_boundary":
            messages[1]["content"] = TAIL
        elif case == "blank_tail":
            messages[1]["content"] = PREFIX + "  \n"
        _, sent = await _send(provider, [_ok()], messages=messages, **kwargs)
        assert isinstance(sent[0]["messages"][1]["content"], str)
        assert "prompt_cache_breakpoint" not in json.dumps(sent[0])

    async def test_a_gateway_never_sends_a_breakpoint(self):
        """OpenRouter routes ``openai/gpt-6-luna``, which the family pattern
        matches; no breakpoint has been measured through a gateway."""
        provider = OpenRouterProvider(_config("openai/gpt-6-luna", "openrouter"))
        _, sent = await _send(
            provider, [_ok()], messages=_loop_messages(), cache_prompt=True
        )
        assert "prompt_cache_breakpoint" not in json.dumps(sent[0])
        assert HuggingFaceProvider._SENDS_PROMPT_CACHE_BREAKPOINTS is False


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestBreakpointRefusalIsLearned:
    async def test_a_refusal_is_answered_by_one_resend_without_the_breakpoint(self):
        provider = _azure()
        result, sent = await _send(
            provider,
            [_refuses_breakpoint(), _ok()],
            messages=_loop_messages(),
            cache_prompt=True,
        )
        assert result.content == "ok"
        assert len(sent) == 2
        assert "prompt_cache_breakpoint" in json.dumps(sent[0])
        assert sent[1]["messages"][1]["content"] == PREFIX + TAIL

        _, later = await _send(
            provider, [_ok()], messages=_loop_messages(), cache_prompt=True
        )
        assert len(later) == 1
        assert "prompt_cache_breakpoint" not in json.dumps(later[0])


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestCacheWriteUsage:
    async def test_cache_writes_are_their_own_bucket(self):
        """The usage of the live probe's second turn with the breakpoint:
        writes bill 1.25x input on GPT-5.6+, so they leave the input bucket."""
        usage = {
            "prompt_tokens": 37023,
            "completion_tokens": 40,
            "total_tokens": 37063,
            "prompt_tokens_details": {
                "cached_tokens": 32933,
                "cache_write_tokens": 3964,
            },
        }
        result, _ = await _send(
            _azure(), [_ok(usage)], messages=_loop_messages(), cache_prompt=True
        )
        assert result.cache_read_tokens == 32933
        assert result.cache_write_tokens == 3964
        assert result.input_tokens == 37023 - 32933 - 3964


def _refuses_token_param():
    return {
        "status": 400,
        "text": json.dumps(
            {
                "error": {
                    "message": "Unsupported parameter: 'max_completion_tokens' is "
                    "not supported with this model. Use 'max_tokens' instead.",
                    "type": "invalid_request_error",
                    "param": "max_completion_tokens",
                    "code": "unsupported_parameter",
                }
            }
        ),
    }


def _refuses_temperature():
    return {
        "status": 400,
        "text": json.dumps(
            {
                "error": {
                    "message": "Unsupported value: 'temperature' does not support "
                    "0.2 with this model. Only the default (1) value is supported.",
                    "type": "invalid_request_error",
                    "param": "temperature",
                    "code": "unsupported_value",
                }
            }
        ),
    }


def _unavailable():
    return {"status": 503, "text": json.dumps({"error": {"message": "busy"}})}


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestBreakpointRefusalBesideOtherCorrections:
    """The breakpoint only saves cost: a call never fails for it, whichever
    refusal comes first."""

    async def test_breakpoint_then_token_param(self):
        provider = _azure()
        result, sent = await _send(
            provider,
            [_refuses_breakpoint(), _refuses_token_param(), _ok()],
            messages=_loop_messages(),
            cache_prompt=True,
        )
        assert result.content == "ok"
        assert len(sent) == 3
        assert "prompt_cache_breakpoint" not in json.dumps(sent[2])
        assert "max_tokens" in sent[2] and "max_completion_tokens" not in sent[2]

    async def test_temperature_then_breakpoint(self):
        """A gpt-6 line outside the default-reasoning families is sent a
        temperature; the temperature correction's re-send still carries the
        breakpoint, and its refusal is answered too."""
        provider = AzureProvider(_config("gpt-6.2-astra", "azure"))
        result, sent = await _send(
            provider,
            [_refuses_temperature(), _refuses_breakpoint(), _ok()],
            messages=_loop_messages(),
            cache_prompt=True,
            temperature=0.2,
        )
        assert result.content == "ok"
        assert len(sent) == 3
        assert "temperature" not in sent[2]
        assert "prompt_cache_breakpoint" not in json.dumps(sent[2])

    async def test_the_refusal_is_learned_even_when_the_resend_fails(self):
        """The 400 is the endpoint's answer: the ladder's next attempt must
        not pay for the breakpoint again."""
        provider = _azure()
        with pytest.raises(Exception):
            await _send(
                provider,
                [_refuses_breakpoint(), _unavailable()],
                messages=_loop_messages(),
                cache_prompt=True,
            )
        _, later = await _send(
            provider, [_ok()], messages=_loop_messages(), cache_prompt=True
        )
        assert "prompt_cache_breakpoint" not in json.dumps(later[0])


@pytest.mark.unit
@pytest.mark.parametrize(
    "model,marked",
    [
        ("gpt-5.6-luna", True),
        ("gpt-6-luna-1", True),
        ("gpt-5.7", True),
        ("gpt-7-mini", True),
        ("gpt-5.4-mini", False),
        ("gpt-5", False),
        ("gpt-4o", False),
        ("gpt-4.1-mini", False),
        ("o3", False),
    ],
)
def test_breakpoints_go_to_every_model_from_gpt_5_6(model, marked):
    assert OpenAIProvider._marks_cache_boundary(model) is marked
