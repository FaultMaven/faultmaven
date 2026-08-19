"""Anthropic extended thinking on structured-output calls (#1116).

Behind config, DEFAULT OFF. Pins, all asserted on the OUTGOING request payload
(the JSON body handed to aiohttp), never on a mock's mere invocation:

  1. DEFAULT OFF — with no thinking configuration the request carries NO
     `thinking` key, temperature is preserved, and forced tool_choice is
     preserved: byte-identical behavior to pre-#1116.
  2. "adaptive" mode sends ``{"type": "adaptive"}`` on tool-calling calls
     only (structured-output scope, mirroring Gemini), drops temperature
     (Anthropic rejects a modified temperature with thinking on), and
     downgrades forced tool_choice to auto (forced tool use with thinking
     is a 400).
  3. "enabled" mode budget validation — thinking bills INSIDE max_tokens
     (fm#1094): a budget that is >= max_tokens, leaves less than the answer
     floor, or is below the API minimum of 1024 downgrades the call to
     no-thinking with a warning instead of being issued.
  4. Round-trip — thinking / redacted_thinking blocks are captured verbatim
     into provider_metadata.assistant_content and echoed back VERBATIM as
     the assistant turn on the next request (Anthropic validates block
     signatures; rebuilt content would break the model's chain — same
     discipline as Gemini's thoughtSignature / assistant_parts path).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import ProviderConfig


def _config(thinking_mode=None, thinking_budget_tokens=None):
    return ProviderConfig(
        name="anthropic",
        api_key="test-key",
        base_url="https://api.anthropic.com/v1",
        models=["claude-sonnet-4-6"],
        default_model="claude-sonnet-4-6",
        timeout=30,
        confidence_score=0.9,
        thinking_mode=thinking_mode,
        thinking_budget_tokens=thinking_budget_tokens,
    )


def _mock_aiohttp_session(response_data: dict):
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=response_data)
    mock_response.text = AsyncMock(return_value="")
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


_TEXT_RESP = {
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 5},
}

# A thinking-enabled tool-use response: thinking + redacted_thinking + text +
# tool_use, each carrying provider-signed fields that must survive verbatim.
_THINKING_TOOL_RESP = {
    "content": [
        {
            "type": "thinking",
            "thinking": "Let me analyze the evidence...",
            "signature": "sig-abc123==",
        },
        {"type": "redacted_thinking", "data": "opaque-encrypted-payload=="},
        {"type": "text", "text": "Checking the logs."},
        {
            "type": "tool_use",
            "id": "toolu_01",
            "name": "search_file",
            "input": {"query": "OOMKilled"},
        },
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 100, "output_tokens": 50},
}

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_file",
            "description": "Search evidence",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }
]


async def _sent_request_body(provider, response_data=None, **generate_kwargs):
    """Run generate() against a mocked transport; return the outgoing JSON body."""
    mock_session = _mock_aiohttp_session(response_data or _TEXT_RESP)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await provider.generate("Test prompt", **generate_kwargs)
    call_kwargs = mock_session.post.call_args
    return call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")


# =========================================================================
# 1. Default off
# =========================================================================


@pytest.mark.unit
class TestThinkingDefaultOff:
    @pytest.mark.asyncio
    async def test_unset_config_sends_no_thinking_parameter(self):
        """No thinking config → outgoing payload has NO `thinking` key and the
        pre-#1116 fields (temperature, forced tool_choice) are untouched."""
        provider = AnthropicProvider(_config())

        body = await _sent_request_body(
            provider,
            tools=_TOOLS,
            tool_choice="required",
            max_tokens=8000,
            temperature=0.2,
        )

        assert "thinking" not in body
        assert body["temperature"] == 0.2
        assert body["tool_choice"] == {"type": "any"}

    @pytest.mark.asyncio
    async def test_explicit_off_sends_no_thinking_parameter(self):
        provider = AnthropicProvider(_config(thinking_mode="off"))

        body = await _sent_request_body(
            provider, tools=_TOOLS, max_tokens=8000, temperature=0.2
        )

        assert "thinking" not in body
        assert body["temperature"] == 0.2

    @pytest.mark.asyncio
    async def test_unknown_mode_fails_closed_to_off(self, caplog):
        provider = AnthropicProvider(_config(thinking_mode="turbo"))

        with caplog.at_level("WARNING"):
            body = await _sent_request_body(provider, tools=_TOOLS, max_tokens=8000)

        assert "thinking" not in body
        assert any("turbo" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_provider_config_defaults_are_off(self):
        """ProviderConfig without the new fields defaults to no thinking —
        every existing construction site keeps its exact behavior."""
        config = ProviderConfig(name="anthropic", api_key="k", models=["m"])
        assert config.thinking_mode is None
        assert config.thinking_budget_tokens is None


# =========================================================================
# 2. Adaptive mode
# =========================================================================


@pytest.mark.unit
class TestAdaptiveThinking:
    @pytest.mark.asyncio
    async def test_adaptive_sends_adaptive_thinking_on_tool_calls(self):
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))

        body = await _sent_request_body(
            provider,
            response_data=_THINKING_TOOL_RESP,
            tools=_TOOLS,
            max_tokens=8000,
            temperature=0.2,
        )

        assert body["thinking"] == {"type": "adaptive"}

    @pytest.mark.asyncio
    async def test_adaptive_drops_temperature(self):
        """Anthropic rejects a modified temperature when thinking is on."""
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))

        body = await _sent_request_body(
            provider, tools=_TOOLS, max_tokens=8000, temperature=0.2
        )

        assert "temperature" not in body

    @pytest.mark.asyncio
    async def test_adaptive_downgrades_forced_tool_choice_to_auto(self, caplog):
        """Forced tool use ({"type": "any"}/{"type": "tool"}) with thinking
        is a 400 — the provider downgrades to auto instead of issuing it."""
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))

        with caplog.at_level("WARNING"):
            body = await _sent_request_body(
                provider, tools=_TOOLS, tool_choice="required", max_tokens=8000
            )

        assert body["thinking"] == {"type": "adaptive"}
        assert body["tool_choice"] == {"type": "auto"}
        assert any("tool_choice" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_adaptive_not_applied_without_tools(self):
        """Structured-output scope only: plain chat calls get no thinking."""
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))

        body = await _sent_request_body(provider, max_tokens=8000, temperature=0.2)

        assert "thinking" not in body
        assert body["temperature"] == 0.2

    @pytest.mark.asyncio
    async def test_adaptive_disabled_when_max_tokens_too_small(self, caplog):
        """Thinking shares the max_tokens pool — a pool too small to hold
        minimum thinking plus the answer floor is a starvable call."""
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))

        with caplog.at_level("WARNING"):
            body = await _sent_request_body(provider, tools=_TOOLS, max_tokens=1000)

        assert "thinking" not in body
        assert any("max_tokens" in r.message for r in caplog.records)


# =========================================================================
# 3. Enabled mode — budget validation (fm#1094 guard)
# =========================================================================


@pytest.mark.unit
class TestEnabledThinkingBudget:
    @pytest.mark.asyncio
    async def test_valid_budget_sends_enabled_thinking(self):
        provider = AnthropicProvider(
            _config(thinking_mode="enabled", thinking_budget_tokens=4096)
        )

        body = await _sent_request_body(provider, tools=_TOOLS, max_tokens=8000)

        assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}

    @pytest.mark.asyncio
    async def test_budget_equal_to_max_tokens_is_refused(self, caplog):
        """budget_tokens must be strictly less than max_tokens — a starvable
        call is downgraded with a warning, never issued."""
        provider = AnthropicProvider(
            _config(thinking_mode="enabled", thinking_budget_tokens=8000)
        )

        with caplog.at_level("WARNING"):
            body = await _sent_request_body(provider, tools=_TOOLS, max_tokens=8000)

        assert "thinking" not in body
        assert any("starve" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_budget_leaving_less_than_answer_floor_is_refused(self, caplog):
        """8000 - 7500 = 500 tokens for the answer — under the floor, exactly
        the fm#1094 starvation shape."""
        provider = AnthropicProvider(
            _config(thinking_mode="enabled", thinking_budget_tokens=7500)
        )

        with caplog.at_level("WARNING"):
            body = await _sent_request_body(provider, tools=_TOOLS, max_tokens=8000)

        assert "thinking" not in body

    @pytest.mark.asyncio
    async def test_budget_below_api_minimum_is_refused(self, caplog):
        provider = AnthropicProvider(
            _config(thinking_mode="enabled", thinking_budget_tokens=512)
        )

        with caplog.at_level("WARNING"):
            body = await _sent_request_body(provider, tools=_TOOLS, max_tokens=8000)

        assert "thinking" not in body
        assert any("minimum" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_explicit_zero_budget_is_refused_not_defaulted(self, caplog):
        """ANTHROPIC_THINKING_BUDGET_TOKENS=0 is an explicit value, not an
        unset one: it must hit the below-minimum refuse path (downgrade with
        a warning), never silently take the 4096 default."""
        provider = AnthropicProvider(
            _config(thinking_mode="enabled", thinking_budget_tokens=0)
        )

        with caplog.at_level("WARNING"):
            body = await _sent_request_body(provider, tools=_TOOLS, max_tokens=8000)

        assert "thinking" not in body
        assert any("minimum" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unset_budget_uses_default(self):
        provider = AnthropicProvider(_config(thinking_mode="enabled"))

        body = await _sent_request_body(provider, tools=_TOOLS, max_tokens=8000)

        assert body["thinking"] == {
            "type": "enabled",
            "budget_tokens": AnthropicProvider._THINKING_DEFAULT_BUDGET_TOKENS,
        }


# =========================================================================
# 4. Thinking-block round-trip
# =========================================================================


@pytest.mark.unit
class TestThinkingRoundTrip:
    @pytest.mark.asyncio
    async def test_thinking_blocks_captured_verbatim_in_provider_metadata(self):
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))
        mock_session = _mock_aiohttp_session(_THINKING_TOOL_RESP)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            response = await provider.generate(
                "Investigate", tools=_TOOLS, max_tokens=8000
            )

        assert response.provider_metadata == {
            "assistant_content": _THINKING_TOOL_RESP["content"]
        }
        # Tool call and visible text extraction are unchanged.
        assert response.tool_calls is not None
        assert response.tool_calls[0].function["name"] == "search_file"
        assert json.loads(response.tool_calls[0].function["arguments"]) == {
            "query": "OOMKilled"
        }
        assert response.content == "Checking the logs."

    @pytest.mark.asyncio
    async def test_no_thinking_blocks_leaves_provider_metadata_none(self):
        """Responses without thinking blocks keep provider_metadata None —
        zero change for the thinking-off path."""
        provider = AnthropicProvider(_config())
        mock_session = _mock_aiohttp_session(_TEXT_RESP)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            response = await provider.generate("Hello", max_tokens=1000)

        assert response.provider_metadata is None

    def test_convert_messages_echoes_saved_blocks_verbatim(self):
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))
        messages = [
            {"role": "user", "content": "Investigate"},
            {
                "role": "assistant",
                "content": "Checking the logs.",
                "tool_calls": [
                    {
                        "id": "toolu_01",
                        "type": "function",
                        "function": {
                            "name": "search_file",
                            "arguments": '{"query": "OOMKilled"}',
                        },
                    }
                ],
                "provider_metadata": {
                    "assistant_content": _THINKING_TOOL_RESP["content"]
                },
            },
            {"role": "tool", "tool_call_id": "toolu_01", "content": "3 hits"},
        ]

        result = provider._convert_messages_to_anthropic(messages)

        assistant_msg = result["messages"][1]
        assert assistant_msg["role"] == "assistant"
        # VERBATIM: the exact raw block list, thinking + redacted_thinking
        # + signatures included — not a rebuild from content/tool_calls.
        assert assistant_msg["content"] == _THINKING_TOOL_RESP["content"]
        # Tool result still follows as a user turn referencing the tool_use id.
        assert result["messages"][2]["role"] == "user"
        assert result["messages"][2]["content"][0]["tool_use_id"] == "toolu_01"

    @pytest.mark.asyncio
    async def test_full_tool_turn_round_trips_thinking_blocks_on_the_wire(self):
        """Simulated tool-use turn: the follow-up request's assistant turn on
        the wire is the previous response's raw content array, verbatim."""
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))

        # Turn 1 — model thinks and calls a tool.
        mock_session = _mock_aiohttp_session(_THINKING_TOOL_RESP)
        with patch("aiohttp.ClientSession", return_value=mock_session):
            first = await provider.generate(
                "Investigate", tools=_TOOLS, max_tokens=8000
            )

        # Orchestrator carries the assistant turn exactly as
        # milestone_engine._build_assistant_message does: content +
        # tool_calls + response-level provider_metadata.
        history = [
            {"role": "user", "content": "Investigate"},
            {
                "role": "assistant",
                "content": first.content,
                "tool_calls": [
                    {"id": tc.id, "type": tc.type, "function": tc.function}
                    for tc in first.tool_calls
                ],
                "provider_metadata": first.provider_metadata,
            },
            {"role": "tool", "tool_call_id": "toolu_01", "content": "3 hits"},
        ]

        # Turn 2 — assert on the outgoing payload.
        body = await _sent_request_body(
            provider,
            response_data=_TEXT_RESP,
            messages=history,
            tools=_TOOLS,
            max_tokens=8000,
        )

        assert body["messages"][1] == {
            "role": "assistant",
            "content": _THINKING_TOOL_RESP["content"],
        }
        # The follow-up call still carries the thinking parameter, so the
        # echoed blocks are legal (thinking blocks with thinking disabled
        # are rejected by the API).
        assert body["thinking"] == {"type": "adaptive"}


# =========================================================================
# 5. Registry wiring — settings → ProviderConfig
# =========================================================================


@pytest.mark.unit
class TestRegistryWiring:
    def test_anthropic_config_carries_thinking_settings(self):
        from faultmaven.infrastructure.llm.providers.registry import (
            PROVIDER_SCHEMA,
            ProviderRegistry,
        )

        llm = MagicMock()
        llm.anthropic_api_key.get_secret_value.return_value = "k"
        llm.anthropic_model = "claude-sonnet-4-6"
        llm.anthropic_base_url = "https://api.anthropic.com/v1"
        llm.anthropic_thinking_mode = "adaptive"
        llm.anthropic_thinking_budget_tokens = 2048
        llm.timeout_for_provider.return_value = 30
        llm.max_retries = 3
        settings = MagicMock()
        settings.llm = llm

        registry = ProviderRegistry.__new__(ProviderRegistry)
        registry.settings = settings
        registry.logger = MagicMock()

        config = registry._create_provider_config(
            "anthropic", PROVIDER_SCHEMA["anthropic"]
        )

        assert config.thinking_mode == "adaptive"
        assert config.thinking_budget_tokens == 2048

    def test_other_providers_leave_thinking_unset(self):
        from faultmaven.infrastructure.llm.providers.registry import (
            PROVIDER_SCHEMA,
            ProviderRegistry,
        )

        llm = MagicMock()
        llm.gemini_api_key.get_secret_value.return_value = "k"
        llm.gemini_model = "gemini-3.5-flash"
        llm.gemini_base_url = "https://generativelanguage.googleapis.com/v1beta"
        llm.timeout_for_provider.return_value = 30
        llm.max_retries = 3
        settings = MagicMock()
        settings.llm = llm

        registry = ProviderRegistry.__new__(ProviderRegistry)
        registry.settings = settings
        registry.logger = MagicMock()

        config = registry._create_provider_config("gemini", PROVIDER_SCHEMA["gemini"])

        assert config.thinking_mode is None
        assert config.thinking_budget_tokens is None
