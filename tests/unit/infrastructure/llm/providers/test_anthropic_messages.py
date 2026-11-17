"""
Tests for Anthropic provider message conversion and multi-turn support.

Validates _convert_messages_to_anthropic() converts OpenAI-format messages
to Anthropic API format, and that generate() correctly uses the messages kwarg.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.prompt_cache import CACHE_BOUNDARY
from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import LLMResponse, ProviderConfig
from tests.model_content import vetted, vetted_messages


@pytest.fixture
def anthropic_config():
    """Minimal Anthropic provider config for unit tests."""
    return ProviderConfig(
        name="anthropic",
        api_key="test-key",
        base_url="https://api.anthropic.com/v1",
        models=["claude-3-sonnet-20240229"],
        default_model="claude-3-sonnet-20240229",
        timeout=30,
        confidence_score=0.9,
    )


@pytest.fixture
def provider(anthropic_config):
    """Create an AnthropicProvider instance for testing."""
    return AnthropicProvider(anthropic_config)


# =========================================================================
# _convert_messages_to_anthropic tests
# =========================================================================


@pytest.mark.unit
class TestConvertMessagesToAnthropic:
    """Tests for _convert_messages_to_anthropic()."""

    def test_system_messages_extracted_to_top_level(self, provider):
        """System messages should be extracted to a top-level 'system' field."""
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert result["system"] == "You are a helpful assistant."
        assert len(result["messages"]) == 1
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"] == "Hello"

    def test_multiple_system_messages_joined(self, provider):
        """Multiple system messages should be joined with double newlines."""
        messages = [
            {"role": "system", "content": "Rule 1: Be helpful."},
            {"role": "system", "content": "Rule 2: Be concise."},
            {"role": "user", "content": "Hello"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert result["system"] == "Rule 1: Be helpful.\n\nRule 2: Be concise."

    def test_user_messages_passed_through(self, provider):
        """User messages should be passed through unchanged."""
        messages = [
            {"role": "user", "content": "What is the issue?"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert len(result["messages"]) == 1
        assert result["messages"][0] == {
            "role": "user",
            "content": "What is the issue?",
        }
        assert "system" not in result

    def test_assistant_message_with_tool_calls(self, provider):
        """Assistant messages with tool_calls become content blocks with tool_use."""
        messages = [
            {"role": "user", "content": "Search for SSH errors"},
            {
                "role": "assistant",
                "content": "Let me search for that.",
                "tool_calls": [
                    {
                        "id": "call_abc123",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": '{"query": "SSH error"}',
                        },
                    }
                ],
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assistant_msg = result["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert isinstance(assistant_msg["content"], list)
        assert len(assistant_msg["content"]) == 2

        # First block: text
        assert assistant_msg["content"][0] == {
            "type": "text",
            "text": "Let me search for that.",
        }

        # Second block: tool_use
        tool_use = assistant_msg["content"][1]
        assert tool_use["type"] == "tool_use"
        assert tool_use["id"] == "call_abc123"
        assert tool_use["name"] == "search_file"
        assert tool_use["input"] == {"query": "SSH error"}

    def test_assistant_tool_call_arguments_parsed_from_json_string(self, provider):
        """JSON string arguments in tool_calls should be parsed to dict for tool_use input."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_001",
                        "type": "function",
                        "function": {
                            "name": "deep_analysis",
                            "arguments": '{"file_id": "ev_123", "question": "What failed?"}',
                        },
                    }
                ],
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        tool_use = result["messages"][1]["content"][
            0
        ]  # No text content, tool_use is first
        assert tool_use["type"] == "tool_use"
        assert tool_use["input"] == {"file_id": "ev_123", "question": "What failed?"}

    def test_assistant_tool_call_invalid_json_arguments(self, provider):
        """Invalid JSON in arguments should fall back to empty dict."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_002",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": "not valid json",
                        },
                    }
                ],
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        tool_use = result["messages"][1]["content"][0]
        assert tool_use["input"] == {}

    def test_tool_messages_become_user_with_tool_result(self, provider):
        """Tool messages should become user messages with tool_result content blocks."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "tool",
                "tool_call_id": "call_abc123",
                "content": "Found 3 SSH errors in syslog",
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        tool_msg = result["messages"][1]
        assert tool_msg["role"] == "user"
        assert isinstance(tool_msg["content"], list)
        assert len(tool_msg["content"]) == 1
        assert tool_msg["content"][0] == {
            "type": "tool_result",
            "tool_use_id": "call_abc123",
            "content": "Found 3 SSH errors in syslog",
        }

    def test_consecutive_tool_results_grouped(self, provider):
        """Consecutive tool messages should be grouped into a single user message."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search_file", "arguments": "{}"},
                    },
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "search_file", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "Result 1",
            },
            {
                "role": "tool",
                "tool_call_id": "call_2",
                "content": "Result 2",
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        # Should have: user, assistant, user (grouped tool results)
        assert len(result["messages"]) == 3

        grouped_msg = result["messages"][2]
        assert grouped_msg["role"] == "user"
        assert isinstance(grouped_msg["content"], list)
        assert len(grouped_msg["content"]) == 2
        assert grouped_msg["content"][0]["tool_use_id"] == "call_1"
        assert grouped_msg["content"][1]["tool_use_id"] == "call_2"

    def test_assistant_message_empty_content_no_tool_calls(self, provider):
        """Assistant message with empty content and no tool_calls should pass through."""
        messages = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "content": ""},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][1]["content"] == ""

    def test_no_system_messages_omits_system_key(self, provider):
        """When no system messages present, result should not contain 'system' key."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert "system" not in result
        assert len(result["messages"]) == 2

    def test_full_multi_turn_conversation(self, provider):
        """Full multi-turn conversation with system, user, assistant, and tool messages."""
        messages = [
            {"role": "system", "content": "You are an investigator."},
            {"role": "user", "content": "Check SSH logs"},
            {
                "role": "assistant",
                "content": "Searching...",
                "tool_calls": [
                    {
                        "id": "call_search",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": '{"query": "SSH"}',
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_search",
                "content": "SSH auth failure at 14:03:21",
            },
            {"role": "user", "content": "What does that mean?"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assert result["system"] == "You are an investigator."
        assert len(result["messages"]) == 4
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][1]["role"] == "assistant"
        assert result["messages"][2]["role"] == "user"  # tool result
        assert result["messages"][3]["role"] == "user"

    def test_assistant_message_with_dict_arguments(self, provider):
        """Arguments that are already dicts should be used directly."""
        messages = [
            {"role": "user", "content": "test"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_dict",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": {"query": "error", "limit": 10},
                        },
                    }
                ],
            },
        ]

        result = provider._convert_messages_to_anthropic(messages)

        tool_use = result["messages"][1]["content"][0]
        assert tool_use["input"] == {"query": "error", "limit": 10}


# =========================================================================
# generate() message handling tests
# =========================================================================


def _mock_aiohttp_session(response_data: dict):
    """Create a properly nested mock for aiohttp.ClientSession().post() pattern.

    Handles the double async context manager:
        async with aiohttp.ClientSession() as session:
            async with session.post(...) as response:
    """
    # Mock the response object
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=response_data)
    mock_response.text = AsyncMock(return_value="")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    # Mock the session
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session


@pytest.mark.unit
@pytest.mark.asyncio
class TestAnthropicGenerateMessages:
    """Tests for generate() with messages kwarg."""

    async def test_no_messages_wraps_prompt(self, provider):
        """Without messages kwarg, prompt should be wrapped as single user message."""
        response_data = {
            "content": [{"type": "text", "text": "Hello!"}],
            "usage": {"output_tokens": 10},
        }
        mock_session = _mock_aiohttp_session(response_data)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await send_to_model(provider, prompt=vetted("Test prompt"))

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            assert request_body["messages"] == [
                {"role": "user", "content": "Test prompt"}
            ]
            assert "system" not in request_body

    async def test_messages_kwarg_used_directly(self, provider):
        """When messages kwarg is provided, it should be converted and used."""
        messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "Hello"},
        ]
        response_data = {
            "content": [{"type": "text", "text": "Hi!"}],
            "usage": {"output_tokens": 5},
        }
        mock_session = _mock_aiohttp_session(response_data)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await send_to_model(
                provider,
                prompt=vetted("ignored prompt"),
                messages=vetted_messages(messages),
            )

            call_kwargs = mock_session.post.call_args
            request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

            # System should be extracted
            assert request_body["system"] == "Be helpful."
            # Messages should contain the converted user message
            assert request_body["messages"] == [{"role": "user", "content": "Hello"}]


# =========================================================================
# Durable-prefix cache breakpoint (#613)
# =========================================================================

_PREFIX = "You are FaultMaven.\nSTANDING INSTRUCTIONS\n\n" + CACHE_BOUNDARY + "\n"
_TAIL = "\nSTATE: INVESTIGATING\n<case_identity>turn 4</case_identity>\n"
_DA_SYSTEM = "Use the tools, then call the schema tool."


async def _sent_body(provider, messages, **kwargs) -> dict:
    """Drive ``generate()`` with the tool loop's message shape; return the body."""
    response_data = {
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    mock_session = _mock_aiohttp_session(response_data)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await send_to_model(
            provider,
            prompt=vetted("ignored prompt"),
            messages=vetted_messages(messages),
            **kwargs,
        )
    call_kwargs = mock_session.post.call_args
    return call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")


def _loop_messages(user_content: str) -> list[dict]:
    return [
        {"role": "system", "content": _DA_SYSTEM},
        {"role": "user", "content": user_content},
    ]


@pytest.mark.unit
@pytest.mark.asyncio
class TestAnthropicCacheBoundaryBreakpoint:
    """``cache_prompt`` places a second breakpoint at the end of the boundary line.

    These requests offer no tool, so nothing can extend them and they carry
    no rolling breakpoint (``TestAnthropicRollingBreakpoint``)."""

    async def test_boundary_once_splits_the_first_user_message(self, provider):
        body = await _sent_body(
            provider, _loop_messages(_PREFIX + _TAIL), cache_prompt=True
        )

        content = body["messages"][0]["content"]
        assert body["messages"][0]["role"] == "user"
        assert content == [
            {
                "type": "text",
                "text": _PREFIX,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": _TAIL},
        ]
        assert content[0]["text"].endswith(CACHE_BOUNDARY + "\n")
        # The model reads the same prompt: the blocks concatenate to it.
        assert content[0]["text"] + content[1]["text"] == _PREFIX + _TAIL

    async def test_system_breakpoint_is_unchanged(self, provider):
        body = await _sent_body(
            provider, _loop_messages(_PREFIX + _TAIL), cache_prompt=True
        )
        assert body["system"] == [
            {
                "type": "text",
                "text": _DA_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    async def test_no_boundary_is_not_split(self, provider):
        """No boundary, no split and no breakpoint in the message."""
        prompt = "You are FaultMaven.\nSTATE: INVESTIGATING\n"
        body = await _sent_body(provider, _loop_messages(prompt), cache_prompt=True)
        assert body["messages"][0]["content"] == prompt

    async def test_case_content_quoting_the_boundary_splits_at_the_first(
        self, provider
    ):
        """The first occurrence is the template's: the prefix above it is static
        text with no case data in it. A quote in the case data comes later, so
        it can neither move the breakpoint nor turn caching off."""
        quoted = "user pasted: " + CACHE_BOUNDARY + "\n"
        prompt = _PREFIX + _TAIL + quoted
        body = await _sent_body(provider, _loop_messages(prompt), cache_prompt=True)
        assert body["messages"][0]["content"] == [
            {
                "type": "text",
                "text": _PREFIX,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": _TAIL + quoted},
        ]

    async def test_boundary_with_nothing_after_it_is_not_split(self, provider):
        """Anthropic rejects a blank text block, so a blank tail is not split off."""
        prompt = _PREFIX + "  \n"
        body = await _sent_body(provider, _loop_messages(prompt), cache_prompt=True)
        assert body["messages"][0]["content"] == prompt

    async def test_cache_prompt_off_leaves_the_content_untouched(self, provider):
        body = await _sent_body(provider, _loop_messages(_PREFIX + _TAIL))
        assert body["messages"][0]["content"] == _PREFIX + _TAIL
        assert body["system"] == _DA_SYSTEM

    async def test_later_tool_loop_iterations_split_only_the_base(self, provider):
        """Every loop iteration re-sends the base first; tool turns are untouched."""
        messages = _loop_messages(_PREFIX + _TAIL) + [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": CACHE_BOUNDARY},
        ]
        body = await _sent_body(provider, messages, cache_prompt=True)

        assert body["messages"][0]["content"][0]["cache_control"] == {
            "type": "ephemeral"
        }
        assert body["messages"][2] == {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": CACHE_BOUNDARY,
                }
            ],
        }
        assert "cache_control" not in body["messages"][0]["content"][1]


def _strip_cache_control(value):
    """*value* with every ``cache_control`` key removed, recursively."""
    if isinstance(value, dict):
        return {
            k: _strip_cache_control(v) for k, v in value.items() if k != "cache_control"
        }
    if isinstance(value, list):
        return [_strip_cache_control(v) for v in value]
    return value


def _breakpoints(body: dict) -> list:
    """Every block of *body* that carries a ``cache_control`` breakpoint."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            if "cache_control" in value:
                found.append(value)
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(body)
    return found


def _tool_round(n: int) -> list[dict]:
    """One tool-loop round as the engine appends it: a call and its result."""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": f"call_{n}",
                    "type": "function",
                    "function": {"name": "search_file", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": f"call_{n}", "content": f"result {n}"},
    ]


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": f"The {name} tool.",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    # An iteration offers the investigation tools and the schema tool.
    for name in ("search_file", "submit_response")
]


@pytest.mark.unit
@pytest.mark.asyncio
class TestAnthropicRollingBreakpoint:
    """``cache_prompt`` also marks the END of a request offering more than one
    tool, so the next tool-loop iteration reads this one's whole request back
    from the cache, not only the standing prefix."""

    async def _iterations(self, provider) -> list[dict]:
        """Bodies of four consecutive tool-loop iterations of one turn, the
        third carrying the engine's plain-string nudge as its last message."""
        base = _loop_messages(_PREFIX + _TAIL)
        nudge = {"role": "user", "content": "Call one of the tools."}
        histories = [
            base,
            base + _tool_round(1),
            base + _tool_round(1) + [nudge],
            base + _tool_round(1) + [nudge] + _tool_round(2),
        ]
        return [
            await _sent_body(
                provider, h, cache_prompt=True, tools=_TOOLS, tool_choice="auto"
            )
            for h in histories
        ]

    async def test_the_last_block_of_every_iteration_is_marked(self, provider):
        for body in await self._iterations(provider):
            last_block = body["messages"][-1]["content"][-1]
            assert last_block["cache_control"] == {"type": "ephemeral"}

    async def test_each_iteration_extends_the_previous_one(self, provider):
        """Up to the moved breakpoint, iteration k+1 re-sends iteration k's
        request unchanged: the same tools, system and tool_choice, and k's
        messages as the first messages of k+1. A string nudge is a text block
        both when it is last and when it is not."""
        bodies = [_strip_cache_control(b) for b in await self._iterations(provider)]
        for before, after in zip(bodies, bodies[1:]):
            for key in ("tools", "system", "tool_choice"):
                assert before.get(key) == after.get(key)
            n = len(before["messages"])
            assert after["messages"][:n] == before["messages"]
        assert bodies[2]["messages"][-1]["content"] == [
            {"type": "text", "text": "Call one of the tools."}
        ]

    async def test_breakpoints_stay_within_the_api_limit(self, provider):
        """System, boundary and the rolling one: three of the API's four."""
        for body in await self._iterations(provider):
            assert len(_breakpoints(body)) == 3

    async def test_a_thinking_block_is_never_marked(self, provider):
        """A thinking or redacted_thinking block cannot carry a breakpoint, nor
        can an empty text block: the mark goes on the last block that can."""
        echoed = [
            {"type": "text", "text": "Looking."},
            {"type": "text", "text": ""},
            {"type": "thinking", "thinking": "hmm", "signature": "sig"},
            {"type": "redacted_thinking", "data": "opaque"},
        ]
        messages = _loop_messages(_PREFIX + _TAIL) + [
            {
                "role": "assistant",
                "content": "Looking.",
                "provider_metadata": {"assistant_content": echoed},
            }
        ]
        body = await _sent_body(
            provider, messages, cache_prompt=True, tools=_TOOLS, tool_choice="auto"
        )
        last = body["messages"][-1]["content"]
        assert last[0]["cache_control"] == {"type": "ephemeral"}
        assert "cache_control" not in last[1]
        assert "cache_control" not in last[2]
        assert "cache_control" not in last[3]

    async def test_a_single_tool_request_marks_only_its_prefix(self, provider):
        """The schema-only call offers one tool, and its reply ends the loop:
        nothing extends it, so its end is not written to the cache. The system
        and boundary marks stay, for the next turn's schema-only call."""
        messages = _loop_messages(_PREFIX + _TAIL) + _tool_round(1)
        body = await _sent_body(
            provider,
            messages,
            cache_prompt=True,
            tools=_TOOLS[1:],
            tool_choice="required",
        )
        assert len(_breakpoints(body)) == 2
        assert "cache_control" in body["system"][0]
        assert "cache_control" in body["messages"][0]["content"][0]

    async def test_a_cached_prompt_only_call_marks_its_boundary(self, provider):
        """The single shot sends ``prompt`` alone. It takes the boundary mark,
        so the next turn reads the standing prefix back, and no mark at its
        end, which only this call's own retry could read."""
        response_data = {
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        mock_session = _mock_aiohttp_session(response_data)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await send_to_model(
                provider,
                prompt=vetted(_PREFIX + _TAIL),
                cache_prompt=True,
                tools=_TOOLS[1:],
                tool_choice="required",
            )
        body = mock_session.post.call_args.kwargs["json"]
        assert body["messages"] == [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": _PREFIX,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": _TAIL},
                ],
            }
        ]

    async def test_cache_prompt_off_marks_nothing(self, provider):
        messages = _loop_messages(_PREFIX + _TAIL) + _tool_round(1)
        body = await _sent_body(provider, messages, tools=_TOOLS, tool_choice="auto")
        assert _breakpoints(body) == []
        assert body["messages"][0]["content"] == _PREFIX + _TAIL
