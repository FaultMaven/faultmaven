"""Gemini structured-output calls must cap thinking so it can't starve the JSON.

Thinking-capable models (2.5+, 3.x) bill hidden reasoning tokens against
maxOutputTokens. gemini-3.5-flash (the shipped Gemini default) consumed nearly
the entire budget on deep-context turns, leaving only a few hundred chars of
output before finishReason=MAX_TOKENS — structured generation then 500s with no
fallback in strict mode. The provider now sets a bounded
``generationConfig.thinkingConfig.thinkingBudget`` on structured calls so the
output is never starved.

Pins:
  1. structured call on a 3.x thinking model sets thinkingBudget, capped at
     half the output budget;
  2. the budget is the configured cap when that is smaller than half;
  3. non-thinking models (1.5) never get thinkingConfig (they 400 on it);
  4. non-structured calls never get thinkingConfig (partial text is fine);
  5. the function-calling (tools) path is treated as structured too.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider


class _Schema(BaseModel):
    answer: str


def _config(model):
    return ProviderConfig(
        name="gemini",
        api_key="test-key",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
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


_OK_RESP = {
    "candidates": [{"content": {"parts": [{"text": "{}"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"candidatesTokenCount": 5},
}


def _response_format():
    return {
        "type": "json_schema",
        "json_schema": {"name": "Schema", "schema": _Schema.model_json_schema()},
    }


async def _sent_generation_config(provider, **generate_kwargs):
    mock_session = _mock_aiohttp_session(_OK_RESP)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await provider.generate("Test", **generate_kwargs)
    call_kwargs = mock_session.post.call_args
    request_body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
    return request_body["generationConfig"]


# --- pure helper -------------------------------------------------------------


@pytest.mark.unit
class TestStructuredThinkingBudgetHelper:
    def test_caps_at_half_output_budget(self):
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        # max_tokens//2 = 256 < cap (2048) → reserve half for output.
        assert (
            provider._structured_thinking_budget("gemini-3.5-flash", 512, True) == 256
        )

    def test_uses_cap_when_smaller_than_half(self):
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        # max_tokens//2 = 8000 > cap (2048) → cap wins.
        assert (
            provider._structured_thinking_budget("gemini-3.5-flash", 16000, True)
            == 2048
        )

    def test_none_for_non_structured(self):
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        assert (
            provider._structured_thinking_budget("gemini-3.5-flash", 8000, False)
            is None
        )

    @pytest.mark.parametrize("model", ["gemini-1.5-pro", "gemini-1.5-flash"])
    def test_none_for_non_thinking_models(self, model):
        provider = GeminiProvider(_config(model))
        assert provider._structured_thinking_budget(model, 8000, True) is None

    @pytest.mark.parametrize(
        "model", ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.5-flash"]
    )
    def test_thinking_models_detected(self, model):
        assert GeminiProvider._is_thinking_model(model) is True

    @pytest.mark.parametrize("model", ["gemini-1.5-pro", "gemini-2.0-flash"])
    def test_non_thinking_models_detected(self, model):
        assert GeminiProvider._is_thinking_model(model) is False


# --- live generate() wiring --------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestGenerateThinkingConfigWiring:
    async def test_structured_call_on_flash_sets_bounded_thinking_budget(self):
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        gen = await _sent_generation_config(
            provider, response_format=_response_format(), max_tokens=8000
        )
        assert gen["thinkingConfig"]["thinkingBudget"] == 2048
        # Output floor preserved: budget never exceeds half of maxOutputTokens.
        assert gen["thinkingConfig"]["thinkingBudget"] <= gen["maxOutputTokens"] // 2

    async def test_tools_call_on_flash_is_structured(self):
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        tools = [
            {
                "type": "function",
                "function": {"name": "emit", "parameters": {"type": "object"}},
            }
        ]
        gen = await _sent_generation_config(
            provider, tools=tools, tool_choice="required", max_tokens=8000
        )
        assert "thinkingConfig" in gen

    async def test_non_structured_call_has_no_thinking_config(self):
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        gen = await _sent_generation_config(provider, max_tokens=8000)
        assert "thinkingConfig" not in gen

    async def test_structured_call_on_non_thinking_model_omits_thinking_config(self):
        """gemini-1.5 rejects thinkingConfig — must not be sent."""
        provider = GeminiProvider(_config("gemini-1.5-pro"))
        gen = await _sent_generation_config(
            provider, response_format=_response_format(), max_tokens=8000
        )
        assert "thinkingConfig" not in gen
