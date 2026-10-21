"""Anthropic extended thinking on structured-output calls (#1116).

Behind config, DEFAULT OFF. Pins, all asserted on the OUTGOING request payload
(the JSON body handed to aiohttp), never on a mock's mere invocation:

  1. DEFAULT OFF — with no thinking configuration, on a model with no
     measured OFF control (sections 1-3 use claude-sonnet-4-6), the request
     carries NO `thinking` key, temperature is preserved, and forced
     tool_choice is preserved. Section 6 pins the per-model OFF control
     (#1800): `disabled` on opus-5, `output_config.effort: low` on opus-5-5
     and fable-5-1.
  2. "adaptive" mode sends ``{"type": "adaptive"}`` on tool-calling calls
     only (structured-output scope, mirroring Gemini) and drops temperature
     (Anthropic rejects a modified temperature with thinking on). When the
     caller FORCED tool use it FAILS CLOSED instead: thinking is refused and
     the forcing is left exactly as the caller set it, because dropping the
     forcing would disarm schema forcing on the single-shot structured path
     (a prose reply gets one re-ask, then fails the turn) and let prose
     replies strip the evidence tools on DA turns.
  3. "enabled" mode budget validation — thinking bills INSIDE max_tokens
     (fm#1094): a budget that is >= max_tokens, leaves less than the answer
     floor, or is below the API minimum of 1024 downgrades the call to
     thinking off with a warning instead of being issued.
  4. Round-trip — thinking / redacted_thinking blocks are captured verbatim
     into provider_metadata.assistant_content and echoed back VERBATIM as
     the assistant turn on the next request (Anthropic validates block
     signatures; rebuilt content would break the model's chain — same
     discipline as Gemini's thoughtSignature / assistant_parts path).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from tests.model_content import vetted, vetted_kwargs


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
        await send_to_model(
            provider, **vetted_kwargs({"prompt": "Test prompt", **generate_kwargs})
        )
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
    async def test_forced_tool_choice_refuses_thinking_and_keeps_forcing(self, caplog):
        """FAIL CLOSED: forced tool use with thinking is a 400, so thinking is
        refused and the caller's forcing is left untouched.

        Downgrading the forcing to auto instead would disarm schema forcing on
        the single-shot structured path (milestone_engine sets
        tool_choice="required" for FUNCTION_CALLING providers and re-asks a
        prose reply only once before the turn fails), and on DA turns would
        let repeated prose replies send the loop to its schema-only
        iteration, stripping the evidence tools. A soundness property is never
        traded for an experiment knob.
        """
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))

        with caplog.at_level("WARNING"):
            body = await _sent_request_body(
                provider, tools=_TOOLS, tool_choice="required", max_tokens=8000
            )

        assert "thinking" not in body
        # The caller's forcing survives EXACTLY as it set it.
        assert body["tool_choice"] == {"type": "any"}
        # Temperature is untouched too — the drop is part of the thinking path.
        assert any("forced tool use" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_explicit_forced_tool_dict_also_refuses_thinking(self):
        """Anthropic-native {"type": "tool", ...} forcing is refused the same
        way as the "required" → {"type": "any"} translation."""
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))

        body = await _sent_request_body(
            provider,
            tools=_TOOLS,
            tool_choice={"type": "tool", "name": "search_file"},
            max_tokens=8000,
        )

        assert "thinking" not in body
        assert body["tool_choice"] == {"type": "tool", "name": "search_file"}

    @pytest.mark.asyncio
    async def test_auto_tool_choice_still_carries_thinking(self):
        """Only forced choice is incompatible — auto turns keep thinking."""
        provider = AnthropicProvider(_config(thinking_mode="adaptive"))

        body = await _sent_request_body(
            provider, tools=_TOOLS, tool_choice="auto", max_tokens=8000
        )

        assert body["thinking"] == {"type": "adaptive"}
        assert body["tool_choice"] == {"type": "auto"}

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
            response = await send_to_model(
                provider, prompt=vetted("Investigate"), tools=_TOOLS, max_tokens=8000
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
            response = await send_to_model(
                provider, prompt=vetted("Hello"), max_tokens=1000
            )

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
            first = await send_to_model(
                provider, prompt=vetted("Investigate"), tools=_TOOLS, max_tokens=8000
            )

        # Orchestrator carries the assistant turn exactly as the tool loop's
        # echo (model_output) does: content + tool_calls + response-level
        # provider_metadata.
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


# =========================================================================
# 5. Shape per model (#1756)
# =========================================================================


def _model_config(model, mode, budget=4096):
    config = _config(thinking_mode=mode, thinking_budget_tokens=budget)
    config.models = [model]
    config.default_model = model
    return config


def _substitution_records(caplog):
    return [r for r in caplog.records if "(#1756)" in r.getMessage()]


@pytest.mark.unit
@pytest.mark.asyncio
class TestThinkingShapePerModel:
    @pytest.fixture(autouse=True)
    def _fresh_log_memory(self, monkeypatch):
        monkeypatch.setattr(AnthropicProvider, "_THINKING_SUBSTITUTION_LOGGED", set())
        monkeypatch.setattr(AnthropicProvider, "_REQUEST_SHAPE_LOGGED", set())

    async def _body(self, model, mode, budget=4096, **kwargs):
        provider = AnthropicProvider(_model_config(model, mode, budget))
        kwargs.setdefault("max_tokens", 8000)
        return await _sent_request_body(provider, tools=_TOOLS, **kwargs)

    @pytest.mark.parametrize(
        "model",
        [
            "claude-sonnet-4-6",
            "claude-opus-4-6",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-5-20251101",
        ],
    )
    async def test_enabled_kept_on_models_that_accept_budget_tokens(
        self, model, caplog
    ):
        with caplog.at_level("WARNING"):
            body = await self._body(model, "enabled")
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}
        assert not _substitution_records(caplog)

    @pytest.mark.parametrize(
        "model",
        ["claude-opus-4-7", "claude-opus-5-5", "claude-fable-5-1", "claude-sonnet-5-5"],
    )
    async def test_enabled_becomes_adaptive_on_models_that_reject_budget_tokens(
        self, model, caplog
    ):
        with caplog.at_level("WARNING"):
            body = await self._body(model, "enabled")
        assert body["thinking"] == {"type": "adaptive"}
        records = _substitution_records(caplog)
        assert len(records) == 1
        assert "ANTHROPIC_THINKING_MODE=enabled" in records[0].getMessage()
        assert model in records[0].getMessage()

    async def test_enabled_becomes_adaptive_on_a_version_above_the_ceiling(self):
        body = await self._body("claude-opus-6", "enabled")
        assert body["thinking"] == {"type": "adaptive"}

    @pytest.mark.parametrize(
        ("model", "mode", "expected"),
        [
            (
                "claude-mythos-preview",
                "enabled",
                {"type": "enabled", "budget_tokens": 4096},
            ),
            (
                "claude-opus-4-5@20251101",
                "enabled",
                {"type": "enabled", "budget_tokens": 4096},
            ),
            ("claude-mythos-preview", "adaptive", {"type": "adaptive"}),
        ],
    )
    async def test_unparseable_id_keeps_the_configured_mode(
        self, model, mode, expected, caplog
    ):
        """No thinking shape is accepted by every model, so an id the adapter
        cannot read is sent what the operator configured."""
        with caplog.at_level("WARNING"):
            body = await self._body(model, mode)
        assert body["thinking"] == expected
        assert not _substitution_records(caplog)

    @pytest.mark.parametrize(
        "model",
        [
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-5-20251101",
        ],
    )
    async def test_adaptive_becomes_enabled_on_models_that_reject_adaptive(
        self, model, caplog
    ):
        with caplog.at_level("WARNING"):
            body = await self._body(model, "adaptive")
        assert body["thinking"] == {"type": "enabled", "budget_tokens": 4096}
        records = _substitution_records(caplog)
        assert len(records) == 1
        assert "ANTHROPIC_THINKING_MODE=adaptive" in records[0].getMessage()

    @pytest.mark.parametrize(
        "model",
        [
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-fable-5-1",
            "claude-mythos-preview",
        ],
    )
    async def test_adaptive_kept_on_models_that_accept_it(self, model, caplog):
        with caplog.at_level("WARNING"):
            body = await self._body(model, "adaptive")
        assert body["thinking"] == {"type": "adaptive"}
        assert not _substitution_records(caplog)

    async def test_substituted_adaptive_keeps_the_adaptive_floor(self):
        body = await self._body("claude-opus-4-7", "enabled", max_tokens=2047)
        assert "thinking" not in body

    @pytest.mark.parametrize(
        ("budget", "max_tokens"),
        [
            # Below the API minimum: the enabled guard would refuse it.
            (512, 8000),
            # 3000 - 4096 leaves no answer floor: the enabled guard would
            # refuse it, while the adaptive floor of 2048 passes.
            (4096, 3000),
        ],
    )
    async def test_substituted_adaptive_skips_the_enabled_budget_guards(
        self, budget, max_tokens
    ):
        body = await self._body(
            "claude-opus-4-7", "enabled", budget=budget, max_tokens=max_tokens
        )
        assert body["thinking"] == {"type": "adaptive"}

    async def test_substituted_enabled_keeps_the_answer_floor(self):
        body = await self._body(
            "claude-haiku-4-5-20251001", "adaptive", budget=7500, max_tokens=8000
        )
        assert "thinking" not in body

    async def test_off_sends_nothing_and_logs_nothing(self, caplog):
        with caplog.at_level("WARNING"):
            body = await self._body("claude-opus-4-7", "off")
        assert "thinking" not in body
        assert not _substitution_records(caplog)

    async def test_substitution_warns_once_per_model_and_mode(self, caplog):
        with caplog.at_level("WARNING"):
            await self._body("claude-opus-4-7", "enabled")
            await self._body("claude-opus-4-7", "enabled")
            assert len(_substitution_records(caplog)) == 1
            await self._body("claude-opus-5", "enabled")
        records = _substitution_records(caplog)
        assert len(records) == 2
        assert "claude-opus-5" in records[1].getMessage()

    async def test_forced_tool_choice_and_thinking_both_follow_the_model(self):
        body = await self._body("claude-opus-5-5", "enabled", tool_choice="required")
        assert body["tool_choice"] == {"type": "auto"}
        assert body["thinking"] == {"type": "adaptive"}


# =========================================================================
# 6. Thinking OFF control per model (#1800)
# =========================================================================
#
# Omitting `thinking` does not turn thinking off on every model: on opus-5-5
# and fable-5-1 it runs adaptive (1206 and 1720 thinking tokens measured at a
# real INVESTIGATING call, docs/reference/llm-model-capabilities.md
# §"Thinking controls at the INVESTIGATING call (#1800)"). So a thinking-off
# request — every tool-less call whatever the mode, and a tool call under
# ANTHROPIC_THINKING_MODE=off, an unknown mode, a starvation guard or the
# forced-tool refusal — sends the control measured for the model:
# `thinking: {"type": "disabled"}` on opus-5 (withheld when the history
# echoes thinking blocks), `output_config.effort: "low"` on opus-5-5 and
# fable-5-1, and nothing on every model not measured.

_SCHEMA_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "InvestigationResponse_Diagnosis",
            "description": "Schema tool",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }
]
_DISABLED = {"type": "disabled"}
_EFFORT_LOW = {"effort": "low"}
_WITHHELD = "thinking.type=disabled withheld"


def _off_records(caplog, label):
    return [
        r
        for r in caplog.records
        if r.levelname == "WARNING"
        and f"thinking off is sent as {label}" in r.getMessage().lower()
    ]


def _echo_history(*reasoning_blocks):
    """A tool loop's history: an earlier iteration's assistant turn echoed
    verbatim through provider_metadata.assistant_content, as model_output
    does, with *reasoning_blocks* ahead of its tool_use block."""
    return [
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
                "assistant_content": [
                    *reasoning_blocks,
                    {"type": "text", "text": "Checking the logs."},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "search_file",
                        "input": {"query": "OOMKilled"},
                    },
                ]
            },
        },
        {"role": "tool", "tool_call_id": "toolu_01", "content": "3 hits"},
    ]


_THINKING_BLOCK = {
    "type": "thinking",
    "thinking": "Let me analyze the evidence...",
    "signature": "sig-abc123==",
}
_REDACTED_BLOCK = {"type": "redacted_thinking", "data": "opaque-encrypted-payload=="}


@pytest.mark.unit
@pytest.mark.asyncio
class TestThinkingOffControl:
    @pytest.fixture(autouse=True)
    def _fresh_log_memory(self, monkeypatch):
        monkeypatch.setattr(AnthropicProvider, "_THINKING_SUBSTITUTION_LOGGED", set())
        monkeypatch.setattr(AnthropicProvider, "_REQUEST_SHAPE_LOGGED", set())

    async def _body(self, model, mode, *, tools=True, budget=4096, **kwargs):
        provider = AnthropicProvider(_model_config(model, mode, budget))
        kwargs.setdefault("max_tokens", 8000)
        kwargs.setdefault("temperature", 0.2)
        if tools:
            kwargs["tools"] = _SCHEMA_TOOL
        return await _sent_request_body(provider, **kwargs)

    # --- off, per model ---------------------------------------------------

    async def test_opus_5_off_forced_sends_disabled_beside_the_forcing(self):
        """The measured opus-5 cell: `disabled` beside `tool_choice: any`."""
        body = await self._body("claude-opus-5", "off", tool_choice="required")

        assert body["thinking"] == _DISABLED
        assert body["tool_choice"] == {"type": "any"}
        assert "output_config" not in body
        assert "temperature" not in body

    @pytest.mark.parametrize("model", ["claude-opus-5", "claude-opus-5-20261001"])
    async def test_opus_5_off_toolless_sends_disabled(self, model):
        body = await self._body(model, "off", tools=False)

        assert body["thinking"] == _DISABLED
        assert "output_config" not in body

    async def test_opus_5_5_off_sends_effort_low(self):
        """Forcing is sent as auto plus the instruction on opus-5-5 (#1695)."""
        body = await self._body("claude-opus-5-5", "off", tool_choice="required")

        assert body["output_config"] == _EFFORT_LOW
        assert "thinking" not in body
        assert body["tool_choice"] == {"type": "auto"}
        assert "temperature" not in body

    async def test_opus_5_5_dated_off_toolless_sends_effort_low(self):
        body = await self._body("claude-opus-5-5-20261001", "off", tools=False)

        assert body["output_config"] == _EFFORT_LOW
        assert "thinking" not in body

    async def test_fable_5_1_off_sends_effort_low(self):
        body = await self._body("claude-fable-5-1", "off", tool_choice="required")

        assert body["output_config"] == _EFFORT_LOW
        assert "thinking" not in body

    @pytest.mark.parametrize(
        ("model", "tools"),
        [
            ("claude-fable-5", True),
            ("claude-opus-5-6", True),
            ("claude-sonnet-5-5", False),
            ("anthropic/claude-opus-5-5", True),
            ("claude-opus-5-5-latest", True),
        ],
    )
    async def test_unmeasured_model_off_sends_neither_key(self, model, tools):
        """No measured cell: the pre-#1800 omission, which never 400s."""
        body = await self._body(model, "off", tools=tools)

        assert "thinking" not in body
        assert "output_config" not in body

    async def test_sampling_model_off_toolless_keeps_temperature(self):
        body = await self._body("claude-opus-4-6", "off", tools=False)

        assert "thinking" not in body
        assert "output_config" not in body
        assert body["temperature"] == 0.2

    async def test_unknown_mode_sends_the_off_control_and_names_it(self, caplog):
        with caplog.at_level("WARNING"):
            body = await self._body("claude-opus-5-5", "bogus")

        assert body["output_config"] == _EFFORT_LOW
        assert "thinking" not in body
        records = _off_records(caplog, "output_config.effort=low")
        assert len(records) == 1
        assert "'bogus'" in records[0].getMessage()

    @pytest.mark.parametrize("mode", [None, "", "  OFF ", "bogus"])
    async def test_every_off_spelling_toolless_sends_the_off_control(self, mode):
        """Tool-less: unset, empty, any case of off and an unknown mode all
        send the OFF control."""
        body = await self._body("claude-opus-5-5", mode, tools=False)

        assert body["output_config"] == _EFFORT_LOW
        assert "thinking" not in body

    # --- a tool-less call is a thinking-off call under an ON mode too -----

    @pytest.mark.parametrize(
        ("model", "mode", "key", "value", "label"),
        [
            (
                "claude-opus-5-5",
                "adaptive",
                "output_config",
                _EFFORT_LOW,
                "output_config.effort=low",
            ),
            (
                "claude-fable-5-1",
                "adaptive",
                "output_config",
                _EFFORT_LOW,
                "output_config.effort=low",
            ),
            (
                "claude-opus-5",
                "enabled",
                "thinking",
                _DISABLED,
                "thinking.type=disabled",
            ),
        ],
    )
    async def test_on_mode_toolless_call_sends_the_off_control(
        self, model, mode, key, value, label, caplog
    ):
        """ON modes apply to tool calls only, so a tool-less call under one
        is a thinking-off call: omission would run adaptive thinking there,
        unguarded, on the classifier, triage and doc-QA calls."""
        with caplog.at_level("DEBUG"):
            body = await self._body(model, mode, tools=False)

        assert body[key] == value
        other = "thinking" if key == "output_config" else "output_config"
        assert other not in body
        sent = [
            r.thinking_control for r in caplog.records if hasattr(r, "thinking_control")
        ]
        assert sent == [label]

    # --- ON modes on tool calls are unchanged -----------------------------

    async def test_adaptive_above_the_floor_is_unchanged(self):
        """Byte-identical to pre-#1800: adaptive, no output_config."""
        body = await self._body("claude-opus-5-5", "adaptive", tool_choice="required")

        assert body["thinking"] == {"type": "adaptive"}
        assert set(body) == {
            "model",
            "max_tokens",
            "messages",
            "tools",
            "tool_choice",
            "system",
            "thinking",
        }

    async def test_enabled_on_a_budget_model_is_unchanged(self):
        body = await self._body("claude-sonnet-4-6", "enabled", budget=2048)

        assert body["thinking"] == {"type": "enabled", "budget_tokens": 2048}
        assert "output_config" not in body

    # --- guards and the refusal resolve to the OFF control ----------------

    async def test_adaptive_starvation_guard_sends_effort_low(self, caplog):
        with caplog.at_level("WARNING"):
            body = await self._body("claude-opus-5-5", "adaptive", max_tokens=2000)

        assert body["output_config"] == _EFFORT_LOW
        assert "thinking" not in body
        records = _off_records(caplog, "output_config.effort=low")
        assert len(records) == 1
        message = records[0].getMessage()
        assert "max_tokens=2000" in message
        assert "lowers thinking rather than removing it" in message

    async def test_forced_refusal_sends_disabled_and_keeps_the_forcing(self, caplog):
        with caplog.at_level("WARNING"):
            body = await self._body("claude-opus-5", "adaptive", tool_choice="required")

        assert body["thinking"] == _DISABLED
        assert body["tool_choice"] == {"type": "any"}
        assert "output_config" not in body
        refusals = [r for r in caplog.records if "thinking refused" in r.getMessage()]
        assert len(refusals) == 1
        assert "thinking.type=disabled" in refusals[0].getMessage()

    async def test_substituted_adaptive_guard_sends_disabled(self, caplog):
        """enabled → adaptive on opus-5 (#1756), then the adaptive floor."""
        with caplog.at_level("WARNING"):
            body = await self._body(
                "claude-opus-5", "enabled", tool_choice="auto", max_tokens=2000
            )

        assert body["thinking"] == _DISABLED
        assert body["tool_choice"] == {"type": "auto"}
        assert len(_off_records(caplog, "thinking.type=disabled")) == 1

    async def test_enabled_budget_guard_on_a_budget_model_sends_neither(self, caplog):
        """The enabled guards are reachable only on models that accept
        budget_tokens (<= 4.6), and none of those has a measured cell."""
        with caplog.at_level("WARNING"):
            body = await self._body("claude-sonnet-4-6", "enabled", budget=512)

        assert "thinking" not in body
        assert "output_config" not in body
        assert body["temperature"] == 0.2
        assert len(_off_records(caplog, "no thinking key")) == 1

    # --- `disabled` never meets echoed thinking ---------------------------

    @pytest.mark.parametrize(
        "block", [_THINKING_BLOCK, _REDACTED_BLOCK], ids=["thinking", "redacted"]
    )
    async def test_refused_forced_iteration_with_echoed_thinking_sends_no_key(
        self, block, caplog
    ):
        """A tool loop under adaptive on opus-5: an earlier `auto` iteration
        thought, and this forced iteration is refused thinking. `disabled`
        beside echoed thinking blocks is unmeasured and Anthropic rejects
        echoed thinking without thinking enabled, so no key is sent, which is
        the pre-#1800 body."""
        with caplog.at_level("DEBUG"):
            body = await self._body(
                "claude-opus-5",
                "adaptive",
                tool_choice="required",
                messages=_echo_history(block),
            )

        assert "thinking" not in body
        assert "output_config" not in body
        assert body["tool_choice"] == {"type": "any"}
        # The echo itself is still sent verbatim.
        assert block in body["messages"][1]["content"]
        refusals = [r for r in caplog.records if "thinking refused" in r.getMessage()]
        assert len(refusals) == 1
        assert _WITHHELD in refusals[0].getMessage()
        sent = [
            r.thinking_control for r in caplog.records if hasattr(r, "thinking_control")
        ]
        assert len(sent) == 1
        assert _WITHHELD in sent[0]

    async def test_starvation_guard_with_echoed_thinking_names_the_withholding(
        self, caplog
    ):
        with caplog.at_level("WARNING"):
            body = await self._body(
                "claude-opus-5",
                "adaptive",
                tool_choice="auto",
                max_tokens=2000,
                messages=_echo_history(_THINKING_BLOCK),
            )

        assert "thinking" not in body
        assert len(_off_records(caplog, f"no thinking key ({_WITHHELD}")) == 1

    async def test_echoed_history_on_opus_5_5_keeps_the_on_param(self):
        """opus-5-5 never reaches the refusal (forcing is sent as auto), so
        the ON param is sent beside the echo, unchanged."""
        body = await self._body(
            "claude-opus-5-5",
            "adaptive",
            tool_choice="required",
            messages=_echo_history(_THINKING_BLOCK),
        )

        assert body["thinking"] == {"type": "adaptive"}
        assert "output_config" not in body

    async def test_effort_low_is_sent_beside_echoed_thinking(self):
        """effort low keeps adaptive thinking on, so echoed blocks stay valid
        beside it: the withholding is specific to a `thinking`-key control."""
        body = await self._body(
            "claude-fable-5-1",
            "off",
            tool_choice="required",
            messages=_echo_history(_THINKING_BLOCK),
        )

        assert body["output_config"] == _EFFORT_LOW
        assert "thinking" not in body

    async def test_history_without_reasoning_blocks_still_sends_disabled(self):
        """The scan reads assistant reasoning blocks only: an echoed text and
        tool_use turn, and tool results, do not withhold `disabled`."""
        body = await self._body(
            "claude-opus-5",
            "adaptive",
            tool_choice="required",
            messages=_echo_history(),
        )

        assert body["thinking"] == _DISABLED
        assert body["tool_choice"] == {"type": "any"}

    # --- merge, not replace -------------------------------------------------

    async def test_effort_merges_into_an_existing_output_config(self, monkeypatch):
        build = AnthropicProvider._base_request_body
        fmt = {"type": "json_schema", "schema": {"type": "object"}}

        def with_output_config(self, model, max_tokens, temperature):
            body = build(self, model, max_tokens, temperature)
            body["output_config"] = {"format": dict(fmt)}
            return body

        monkeypatch.setattr(AnthropicProvider, "_base_request_body", with_output_config)

        body = await self._body("claude-opus-5-5", "off", tools=False)

        assert body["output_config"] == {"format": fmt, "effort": "low"}


# --- the table itself ------------------------------------------------------


def _model_id(family, version):
    major, minor = version
    return f"claude-{family}-{major}" + (f"-{minor}" if minor else "")


_OFF_CELLS = sorted(AnthropicProvider._THINKING_OFF_CONTROL)
_CONTROL_LABELS = {
    "thinking.type=disabled": {"thinking": _DISABLED},
    "output_config.effort=low": {"output_config": _EFFORT_LOW},
}


@pytest.mark.unit
@pytest.mark.parametrize(
    "cell", _OFF_CELLS, ids=[_model_id(*cell) for cell in _OFF_CELLS]
)
class TestThinkingOffTable:
    def test_cell_is_a_parsed_version_that_takes_no_temperature(self, cell):
        """An OFF control never meets a temperature: a cell for a model that
        accepts sampling must first measure the control beside one."""
        model = _model_id(*cell)

        assert AnthropicProvider._claude_version(model) == cell
        assert not AnthropicProvider._accepts_sampling(model)

    def test_cell_label_names_its_body(self, cell):
        control = AnthropicProvider._THINKING_OFF_CONTROL[cell]

        assert _CONTROL_LABELS[control.label] == control.body
        assert AnthropicProvider._thinking_off_control(_model_id(*cell), []) == control

    @pytest.mark.asyncio
    async def test_cell_body_carries_no_temperature(self, cell):
        """The same property through the request build, with a temperature
        passed: off, tools and tool-less."""
        model = _model_id(*cell)
        provider = AnthropicProvider(_model_config(model, "off"))

        for kwargs in ({"tools": _SCHEMA_TOOL}, {}):
            body = await _sent_request_body(
                provider, max_tokens=8000, temperature=0.2, **kwargs
            )
            assert "temperature" not in body
            assert ("thinking" in body) != ("output_config" in body)


# A cell on a model that ACCEPTS forced tool use is sent beside a forced
# tool_choice, so it needs that pairing measured. Today only opus-5's
# `disabled` beside `tool_choice: any` was (2026-10-02, 200).
_MEASURED_FORCED_PAIRINGS = {("opus", (5, 0)): {"thinking": _DISABLED}}


def _unmeasured_forced_pairings():
    return sorted(
        key
        for key, control in AnthropicProvider._THINKING_OFF_CONTROL.items()
        if AnthropicProvider._accepts_forced_tool_choice(_model_id(*key))
        and _MEASURED_FORCED_PAIRINGS.get(key) != control.body
    )


@pytest.mark.unit
class TestThinkingOffForcedPairings:
    def test_every_forcing_cell_is_a_measured_pairing(self):
        assert _unmeasured_forced_pairings() == []

    @pytest.mark.parametrize(
        ("key", "source"),
        [
            # sonnet-5 accepts forcing and rejects sampling: a new cell there.
            (("sonnet", (5, 0)), ("opus", (5, 5))),
            # opus-5's cell swapped for a control never measured under forcing.
            (("opus", (5, 0)), ("fable", (5, 1))),
        ],
        ids=["new-sonnet-5-cell", "opus-5-swapped-to-effort"],
    )
    def test_guard_fails_on_an_unmeasured_forcing_cell(self, monkeypatch, key, source):
        table = dict(AnthropicProvider._THINKING_OFF_CONTROL)
        table[key] = table[source]
        monkeypatch.setattr(AnthropicProvider, "_THINKING_OFF_CONTROL", table)

        assert _unmeasured_forced_pairings() == [key]


# --- the observable ----------------------------------------------------------


def _usage_resp(**usage):
    return {
        "content": [{"type": "text", "text": "hello"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 4124, **usage},
    }


async def _response(model, response_data, mode="off"):
    provider = AnthropicProvider(_model_config(model, mode))
    mock_session = _mock_aiohttp_session(response_data)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        return await send_to_model(
            provider, prompt=vetted("Investigate"), max_tokens=8000
        )


@pytest.mark.unit
@pytest.mark.asyncio
class TestThinkingTokens:
    async def test_reported_thinking_tokens_reach_the_response(self):
        response = await _response(
            "claude-fable-5-1",
            _usage_resp(output_tokens_details={"thinking_tokens": 893}),
        )

        assert response.thinking_tokens == 893
        # A part of output_tokens, never added to it.
        assert response.output_tokens == 4124
        assert response.tokens_used == 10 + 4124

    async def test_absent_figure_is_none(self):
        response = await _response("claude-fable-5-1", _usage_resp())

        assert response.thinking_tokens is None

    @pytest.mark.parametrize(
        "details",
        [
            {"thinking_tokens": "893"},
            {"thinking_tokens": 893.0},
            {"thinking_tokens": True},
            {"thinking_tokens": None},
            "893",
            None,
        ],
    )
    async def test_non_int_figure_is_none(self, details):
        response = await _response(
            "claude-fable-5-1", _usage_resp(output_tokens_details=details)
        )

        assert response.thinking_tokens is None

    async def test_one_debug_line_per_call_names_the_control(self, caplog):
        """The adapter's line carries the model and the control it sent; the
        token counts are metering's llm_call record (test_metering.py)."""
        with caplog.at_level("DEBUG"):
            await _response(
                "claude-opus-5-5",
                _usage_resp(output_tokens_details={"thinking_tokens": 0}),
            )

        records = [r for r in caplog.records if hasattr(r, "thinking_control")]
        assert len(records) == 1
        record = records[0]
        assert record.levelname == "DEBUG"
        assert record.model == "claude-opus-5-5"
        assert record.thinking_control == "output_config.effort=low"

    async def test_debug_line_names_an_on_mode(self, caplog):
        provider = AnthropicProvider(_model_config("claude-opus-5-5", "adaptive"))
        mock_session = _mock_aiohttp_session(_usage_resp())
        with (
            caplog.at_level("DEBUG"),
            patch("aiohttp.ClientSession", return_value=mock_session),
        ):
            await send_to_model(
                provider,
                **vetted_kwargs(
                    {"prompt": "Investigate", "tools": _SCHEMA_TOOL, "max_tokens": 8000}
                ),
            )

        records = [r for r in caplog.records if hasattr(r, "thinking_control")]
        assert [r.thinking_control for r in records] == ["adaptive"]


# --- the once-per-model shape line -------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestThinkingOffShapeLog:
    @pytest.fixture(autouse=True)
    def _fresh_log_memory(self, monkeypatch):
        monkeypatch.setattr(AnthropicProvider, "_REQUEST_SHAPE_LOGGED", set())

    @pytest.mark.parametrize(
        ("model", "label"),
        [
            ("claude-opus-5", "thinking.type=disabled"),
            ("claude-opus-5-5", "output_config.effort=low"),
            ("claude-fable-5-1", "output_config.effort=low"),
        ],
    )
    async def test_measured_model_names_its_off_control_once(
        self, model, label, caplog
    ):
        with caplog.at_level("INFO"):
            for _ in range(2):
                await _response(model, _TEXT_RESP)

        lines = [r for r in caplog.records if "(#1695)" in r.getMessage()]
        assert len(lines) == 1
        assert lines[0].levelname == "INFO"
        assert f"thinking off is sent as {label}" in lines[0].getMessage()

    @pytest.mark.parametrize("model", ["claude-fable-5", "claude-opus-4-8"])
    async def test_unmeasured_model_logs_nothing_new(self, model, caplog):
        with caplog.at_level("INFO"):
            await _response(model, _TEXT_RESP)

        lines = [r for r in caplog.records if "(#1695)" in r.getMessage()]
        assert len(lines) == 1
        assert "thinking" not in lines[0].getMessage()
