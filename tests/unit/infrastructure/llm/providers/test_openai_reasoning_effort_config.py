"""OPENAI_REASONING_EFFORT — operator default for OpenAI reasoning effort.

The OpenAI analog of ANTHROPIC_THINKING_MODE: an operator-level default for
``reasoning_effort``, replacing the SHAPE default only. Precedence (weakest
to strongest): shape default < OPENAI_REASONING_EFFORT < per-call
``reasoning_intent`` (#1118) < explicit ``reasoning_effort`` kwarg. Hard model
constraints (function tools alongside reasoning; models that reject the
parameter) always win, and starve-protection is not operator-overridable.

Pins:
  settings —
    1. default is UNSET (None): existing deployments byte-identical;
    2. values normalize case/whitespace; unrecognized values fail closed to
       UNSET with a warning (never abort boot, never accidentally engage);
  registry —
    3. the OpenAI branch hands the setting to ProviderConfig; other branches
       leave it None;
  provider —
    4. unset config: payload identical to the shape defaults (no new key on a
       plain non-default-reasoning call);
    5. the configured value replaces the shape default where verified;
    6. "none" degrades to "low" WITH a warning where "none" is unverified
       (plain calls on non-5.6 families; structured calls everywhere);
    7. "medium"/"high" on a structured call clamp to the "low" floor WITH a
       warning (#625 — hidden reasoning must not starve the schema body);
    8. per-call reasoning_intent still overrides the operator default;
    9. an explicit reasoning_effort kwarg stays the last word;
   10. the gpt-5.6 tools branch still pins "none" regardless of config.
"""

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from faultmaven.config.settings import (
    OPENAI_REASONING_EFFORTS,
    LLMSettings,
)
from faultmaven.infrastructure.llm.providers.base import (
    ProviderConfig,
    ReasoningIntent,
)
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider

_OPENAI_RESP = {
    "choices": [
        {"message": {"content": "ok", "role": "assistant"}, "finish_reason": "stop"}
    ],
    "usage": {"total_tokens": 10},
}

_TOOLS = [
    {
        "type": "function",
        "function": {"name": "emit", "parameters": {"type": "object"}},
    }
]

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "S", "schema": {"type": "object", "properties": {}}},
}


def _provider(model: str, reasoning_effort=None) -> OpenAIProvider:
    return OpenAIProvider(
        ProviderConfig(
            name="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            models=[model],
            default_model=model,
            timeout=30,
            reasoning_effort=reasoning_effort,
        )
    )


async def _sent_body(make_session, provider, **generate_kwargs):
    mock_session = make_session(_OPENAI_RESP)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await provider.generate("Test", **generate_kwargs)
    call_kwargs = mock_session.post.call_args
    return call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")


@pytest.mark.unit
@pytest.mark.llm
class TestSettingNormalization:
    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        monkeypatch.delenv("OPENAI_REASONING_EFFORT", raising=False)
        return monkeypatch

    def test_default_is_unset(self):
        assert LLMSettings().openai_reasoning_effort is None

    @pytest.mark.parametrize("raw", ["NONE", " none ", "None"])
    def test_values_normalize(self, clean_env, raw):
        clean_env.setenv("OPENAI_REASONING_EFFORT", raw)
        assert LLMSettings().openai_reasoning_effort == "none"

    @pytest.mark.parametrize("valid", OPENAI_REASONING_EFFORTS)
    def test_all_documented_values_accepted(self, clean_env, valid):
        clean_env.setenv("OPENAI_REASONING_EFFORT", valid)
        assert LLMSettings().openai_reasoning_effort == valid

    def test_unrecognized_fails_closed_to_unset_with_warning(self, clean_env, caplog):
        clean_env.setenv("OPENAI_REASONING_EFFORT", "turbo")
        with caplog.at_level(logging.WARNING):
            assert LLMSettings().openai_reasoning_effort is None
        assert any("OPENAI_REASONING_EFFORT" in r.message for r in caplog.records)

    def test_empty_string_is_unset(self, clean_env):
        clean_env.setenv("OPENAI_REASONING_EFFORT", "")
        assert LLMSettings().openai_reasoning_effort is None


@pytest.mark.unit
@pytest.mark.llm
class TestRegistryHandoff:
    def _config_for(self, provider_name, monkeypatch, **env):
        from faultmaven.infrastructure.llm.providers.registry import (
            PROVIDER_SCHEMA,
            ProviderRegistry,
        )

        for var in (
            "OPENAI_REASONING_EFFORT",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "OPENAI_MODEL",
            "GEMINI_MODEL",
        ):
            monkeypatch.delenv(var, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        registry = ProviderRegistry(settings=SimpleNamespace(llm=LLMSettings()))
        return registry._create_provider_config(
            provider_name, PROVIDER_SCHEMA[provider_name]
        )

    def test_openai_branch_hands_setting_to_provider_config(self, monkeypatch):
        config = self._config_for(
            "openai",
            monkeypatch,
            OPENAI_API_KEY="sk-test",
            OPENAI_REASONING_EFFORT="none",
        )
        assert config.reasoning_effort == "none"

    def test_openai_branch_defaults_to_none(self, monkeypatch):
        config = self._config_for("openai", monkeypatch, OPENAI_API_KEY="sk-test")
        assert config.reasoning_effort is None

    def test_other_branches_stay_none(self, monkeypatch):
        config = self._config_for(
            "gemini",
            monkeypatch,
            GEMINI_API_KEY="g-test",
            OPENAI_REASONING_EFFORT="none",
        )
        assert config.reasoning_effort is None


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestProviderApplication:
    async def test_unset_config_is_byte_identical_no_param_on_plain_54(
        self, mock_aiohttp_session
    ):
        """gpt-5.4-mini plain chat sends NO reasoning_effort today; an unset
        knob must keep it that way."""
        body = await _sent_body(mock_aiohttp_session, _provider("gpt-5.4-mini"))
        assert "reasoning_effort" not in body

    async def test_configured_value_replaces_shape_default(self, mock_aiohttp_session):
        """Plain call on gpt-5.6 defaults to "none"; config "low" replaces it."""
        provider = _provider("gpt-5.6-luna", reasoning_effort="low")
        body = await _sent_body(mock_aiohttp_session, provider)
        assert body["reasoning_effort"] == "low"

    async def test_none_passes_through_where_verified(self, mock_aiohttp_session):
        provider = _provider("gpt-5.6-luna", reasoning_effort="none")
        body = await _sent_body(mock_aiohttp_session, provider)
        assert body["reasoning_effort"] == "none"

    async def test_none_degrades_to_low_on_unverified_family(
        self, mock_aiohttp_session, caplog
    ):
        """gpt-5.4 is not in _DEFAULT_REASONING_MODEL_FAMILIES: "none" is
        unverified there and degrades to "low" — loudly."""
        provider = _provider("gpt-5.4-mini", reasoning_effort="none")
        with caplog.at_level(logging.WARNING):
            body = await _sent_body(mock_aiohttp_session, provider)
        assert body["reasoning_effort"] == "low"
        assert any("unverified" in r.message for r in caplog.records)

    async def test_high_on_structured_clamps_to_low_floor(
        self, mock_aiohttp_session, caplog
    ):
        provider = _provider("gpt-5.4-mini", reasoning_effort="high")
        with caplog.at_level(logging.WARNING):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                response_format=_RESPONSE_FORMAT,
            )
        assert body["reasoning_effort"] == "low"
        assert any("starve" in r.message for r in caplog.records)

    async def test_medium_allowed_on_plain_call(self, mock_aiohttp_session):
        """Raising effort on a plain call is the operator's call — no clamp
        outside the structured floor."""
        provider = _provider("gpt-5.4-mini", reasoning_effort="medium")
        body = await _sent_body(mock_aiohttp_session, provider)
        assert body["reasoning_effort"] == "medium"

    async def test_reasoning_intent_still_overrides_config(self, mock_aiohttp_session):
        """EXTRACTION on a plain 5.4 call resolves to "low" and must beat the
        operator's "medium" — per-call semantic intent outranks env."""
        provider = _provider("gpt-5.4-mini", reasoning_effort="medium")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            reasoning_intent=ReasoningIntent.EXTRACTION,
        )
        assert body["reasoning_effort"] == "low"

    async def test_explicit_kwarg_stays_last_word(self, mock_aiohttp_session):
        provider = _provider("gpt-5.4-mini", reasoning_effort="low")
        body = await _sent_body(mock_aiohttp_session, provider, reasoning_effort="high")
        assert body["reasoning_effort"] == "high"

    async def test_tools_branch_still_pins_none_on_56(self, mock_aiohttp_session):
        provider = _provider("gpt-5.6-luna", reasoning_effort="high")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            tools=_TOOLS,
            tool_choice="required",
        )
        assert body["reasoning_effort"] == "none"
