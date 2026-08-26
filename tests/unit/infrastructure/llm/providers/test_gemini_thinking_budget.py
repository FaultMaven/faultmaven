"""Gemini 3.x structured-output calls must cap thinking so it can't starve the JSON.

Gemini 3.x thinking models bill hidden reasoning tokens against maxOutputTokens.
gemini-3.5-flash (the shipped Gemini default) consumed nearly the entire budget
on deep-context turns, leaving only a few hundred chars of output before
finishReason=MAX_TOKENS — structured generation then 500s with no fallback in
strict mode. The provider now sets ``thinkingConfig.thinkingLevel: "low"`` on
3.x structured calls so the output is never starved.

Scope is deliberately 3.x-ONLY. Gemini 2.5 also bills thinking against
maxOutputTokens, but the truncation was only ever observed on 3.x flash; 2.5-pro
ran clean, so it is left at its native dynamic thinking rather than changing a
working, reasoning-heavy path without evidence. (3.x also dropped the 2.5-era
integer ``thinkingBudget`` for the string ``thinkingLevel`` — sending the
integer to a 3.x model is a 400.)

Pins:
  1. structured call on a 3.x model sets ``thinkingLevel: low``;
  2. Gemini 2.5 structured calls get NO thinkingConfig (left native);
  3. non-thinking models (1.5/2.0) never get thinkingConfig (they 400 on it);
  4. non-structured calls never get thinkingConfig (partial text is fine) —
     PRE-3.7 ONLY: the 3.7+ surface deliberately caps every call shape, plain
     chat included (see test_gemini_37_surface.py::TestThinkingLevel37);
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
class TestStructuredThinkingConfigHelper:
    @pytest.mark.parametrize("model", ["gemini-3.5-flash", "gemini-3.0-pro"])
    def test_gemini_3x_uses_thinking_level_string(self, model):
        """3.x dropped the integer thinkingBudget for the string thinkingLevel;
        sending thinkingBudget to a 3.x model is a 400."""
        provider = GeminiProvider(_config(model))
        assert provider._structured_thinking_config(model, True) == {
            "thinkingLevel": "low"
        }

    @pytest.mark.parametrize("model", ["gemini-2.5-pro", "gemini-2.5-flash"])
    def test_gemini_25_left_native_no_thinking_config(self, model):
        """2.5 was never observed to starve; left at native dynamic thinking
        rather than changing a working, reasoning-heavy path without evidence."""
        provider = GeminiProvider(_config(model))
        assert provider._structured_thinking_config(model, True) is None

    def test_none_for_non_structured(self):
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        assert provider._structured_thinking_config("gemini-3.5-flash", False) is None

    @pytest.mark.parametrize("model", ["gemini-1.5-pro", "gemini-2.0-flash"])
    def test_none_for_pre_3x_models(self, model):
        provider = GeminiProvider(_config(model))
        assert provider._structured_thinking_config(model, True) is None

    @pytest.mark.parametrize(
        "model,expected",
        [("gemini-2.5-pro", 2), ("gemini-3.5-flash", 3), ("gemini-1.5-pro", 1)],
    )
    def test_major_version_parsing(self, model, expected):
        assert GeminiProvider._gemini_major_version(model) == expected


# --- live generate() wiring --------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
class TestGenerateThinkingConfigWiring:
    async def test_structured_call_on_flash_sets_thinking_level(self):
        """gemini-3.5-flash (the shipped default) — 3.x string thinkingLevel."""
        provider = GeminiProvider(_config("gemini-3.5-flash"))
        gen = await _sent_generation_config(
            provider, response_format=_response_format(), max_tokens=8000
        )
        assert gen["thinkingConfig"] == {"thinkingLevel": "low"}

    async def test_structured_call_on_25_pro_leaves_thinking_native(self):
        """2.5-pro ran clean during validation — must be left untouched (no
        thinkingConfig), not capped on a working path."""
        provider = GeminiProvider(_config("gemini-2.5-pro"))
        gen = await _sent_generation_config(
            provider, response_format=_response_format(), max_tokens=8000
        )
        assert "thinkingConfig" not in gen

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
