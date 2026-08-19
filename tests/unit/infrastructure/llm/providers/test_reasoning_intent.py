"""Per-provider translation of the caller-declared ReasoningIntent (#1118).

The intent is semantic (EXTRACTION / INFERENCE), never raw provider
vocabulary: each provider translates it into its own mechanism, model hard
constraints override caller intent, and an intent that cannot be honoured is
logged, not silently dropped.

Pins:
  OpenAI —
    1. hard constraint wins: gpt-5.6 + function tools forces
       ``reasoning_effort: "none"`` regardless of intent (the API rejects
       tools alongside reasoning on /chat/completions), and the unhonoured
       INFERENCE is logged;
    2. INFERENCE → ``"medium"`` (the API's own default level) where the
       param is accepted;
    3. EXTRACTION → the verified minimum: ``"none"`` on gpt-5.6 plain calls,
       ``"low"`` elsewhere ("none" is only verified on the 5.6 family);
    4. non-reasoning models (gpt-4o) never receive the param — INFERENCE is
       logged as unhonourable;
    5. an explicit ``reasoning_effort`` kwarg still overrides the intent
       translation (the kwargs merge stays the last word);
    6. neither router knob ever leaks into the request body.
  Gemini —
    7. INFERENCE on a 3.x structured call lifts the thinkingLevel starvation
       cap (native dynamic thinking IS the honoured translation) — logged;
    8. EXTRACTION caps thinking at "low" on 3.x on EVERY shape, plain calls
       included;
    9. pre-3.x models have no knob (thinkingLevel is 3.x vocabulary) — no
       thinkingConfig is ever sent, and the intent is logged;
    10. no-intent behavior is byte-identical to the shape-based default
        (structured → low, plain → nothing).
  Providers with no reasoning control (Fireworks as representative) —
    11. both knobs are popped before the payload merge (never leak), and the
        intent is logged.
"""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.base import (
    ProviderConfig,
    ReasoningIntent,
)
from faultmaven.infrastructure.llm.providers.fireworks_provider import (
    FireworksProvider,
)
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider


def _config(name, model, base_url):
    return ProviderConfig(
        name=name,
        api_key="test-key",
        base_url=base_url,
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
    )


def _openai_provider(model):
    return OpenAIProvider(_config("openai", model, "https://api.openai.com/v1"))


def _gemini_provider(model):
    return GeminiProvider(
        _config("gemini", model, "https://generativelanguage.googleapis.com/v1beta")
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


_OPENAI_RESP = {
    "choices": [
        {"message": {"content": "ok", "role": "assistant"}, "finish_reason": "stop"}
    ],
    "usage": {"total_tokens": 10},
}

_GEMINI_RESP = {
    "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
    "usageMetadata": {"candidatesTokenCount": 5},
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


async def _sent_body(provider, response_data, **generate_kwargs):
    mock_session = _mock_aiohttp_session(response_data)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await provider.generate("Test", **generate_kwargs)
    call_kwargs = mock_session.post.call_args
    return call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")


# --- OpenAI ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestOpenAIIntentTranslation:
    async def test_tools_on_gpt56_force_none_even_for_inference(self, caplog):
        """Hard constraint over caller intent: tools on /chat/completions run
        with reasoning off no matter what was asked — and the unhonoured
        intent is logged, not silently dropped."""
        provider = _openai_provider("gpt-5.6-mini")
        with caplog.at_level(logging.WARNING):
            body = await _sent_body(
                provider,
                _OPENAI_RESP,
                tools=_TOOLS,
                tool_choice="required",
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        assert body["reasoning_effort"] == "none"
        assert any(
            "inference" in r.message and "cannot be honoured" in r.message
            for r in caplog.records
        )

    async def test_inference_maps_to_medium_on_structured_call(self):
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.INFERENCE,
        )
        assert body["reasoning_effort"] == "medium"

    async def test_inference_maps_to_medium_on_plain_gpt56(self):
        provider = _openai_provider("gpt-5.6-mini")
        body = await _sent_body(
            provider, _OPENAI_RESP, reasoning_intent=ReasoningIntent.INFERENCE
        )
        assert body["reasoning_effort"] == "medium"

    async def test_extraction_maps_to_low_on_structured_call(self):
        """ "none" is only verified on the 5.6 family; "low" is the
        broadly-valid floor across gpt-5 and o-series."""
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.EXTRACTION,
        )
        assert body["reasoning_effort"] == "low"

    async def test_extraction_maps_to_none_on_plain_gpt56(self):
        provider = _openai_provider("gpt-5.6-mini")
        body = await _sent_body(
            provider, _OPENAI_RESP, reasoning_intent=ReasoningIntent.EXTRACTION
        )
        assert body["reasoning_effort"] == "none"

    async def test_extraction_maps_to_low_on_structured_gpt56(self):
        """response_format on 5.6: "none" is unverified on that shape — the
        translation stays at the verified "low"."""
        provider = _openai_provider("gpt-5.6-mini")
        body = await _sent_body(
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.EXTRACTION,
        )
        assert body["reasoning_effort"] == "low"

    async def test_non_reasoning_model_never_gets_the_param(self, caplog):
        """gpt-4o rejects reasoning_effort; INFERENCE cannot be honoured and
        must be logged."""
        provider = _openai_provider("gpt-4o")
        with caplog.at_level(logging.WARNING):
            body = await _sent_body(
                provider, _OPENAI_RESP, reasoning_intent=ReasoningIntent.INFERENCE
            )
        assert "reasoning_effort" not in body
        assert any("cannot be honoured" in r.message for r in caplog.records)

    async def test_explicit_reasoning_effort_kwarg_overrides_intent(self):
        """The documented escape hatch survives: an explicit effort threaded
        through kwargs still has the last word over the intent translation."""
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.EXTRACTION,
            reasoning_effort="high",
        )
        assert body["reasoning_effort"] == "high"

    async def test_router_knobs_never_leak_into_request_body(self):
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            provider,
            _OPENAI_RESP,
            reasoning_intent="extraction",
            min_output_tokens=500,
        )
        assert "reasoning_intent" not in body
        assert "min_output_tokens" not in body

    async def test_string_intent_accepted_on_direct_provider_call(self):
        """The router normalizes, but providers are also called directly —
        the string spelling must translate identically."""
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent="inference",
        )
        assert body["reasoning_effort"] == "medium"

    async def test_no_intent_keeps_shape_based_default(self):
        """None preserves the pre-#1118 defaults exactly (structured → low)."""
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            provider, _OPENAI_RESP, response_format=_RESPONSE_FORMAT
        )
        assert body["reasoning_effort"] == "low"


# --- Gemini ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestGeminiIntentTranslation:
    async def test_inference_lifts_structured_thinking_cap_on_3x(self, caplog):
        """Native dynamic thinking IS the model reasoning at its own
        discretion — the honoured INFERENCE translation is to not cap it.
        Lifting the starvation guard is logged."""
        provider = _gemini_provider("gemini-3.5-flash")
        with caplog.at_level(logging.INFO):
            body = await _sent_body(
                provider,
                _GEMINI_RESP,
                response_format=_RESPONSE_FORMAT,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        assert "thinkingConfig" not in body["generationConfig"]
        assert any("lifting the thinkingLevel" in r.message for r in caplog.records)

    async def test_extraction_caps_thinking_on_plain_3x_call(self):
        """EXTRACTION applies the cap on every shape — plain calls included,
        where the shape-based default would leave thinking dynamic."""
        provider = _gemini_provider("gemini-3.5-flash")
        body = await _sent_body(
            provider, _GEMINI_RESP, reasoning_intent=ReasoningIntent.EXTRACTION
        )
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}

    async def test_extraction_keeps_cap_on_structured_3x_call(self):
        provider = _gemini_provider("gemini-3.5-flash")
        body = await _sent_body(
            provider,
            _GEMINI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.EXTRACTION,
        )
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}

    @pytest.mark.parametrize("model", ["gemini-2.5-pro", "gemini-1.5-pro"])
    async def test_pre_3x_models_get_no_thinking_config_and_a_log(self, model, caplog):
        """thinkingLevel is 3.x vocabulary — earlier models have no knob this
        provider can turn, so the intent is logged and nothing is sent."""
        provider = _gemini_provider(model)
        with caplog.at_level(logging.INFO):
            body = await _sent_body(
                provider,
                _GEMINI_RESP,
                response_format=_RESPONSE_FORMAT,
                reasoning_intent=ReasoningIntent.EXTRACTION,
            )
        assert "thinkingConfig" not in body["generationConfig"]
        assert any("cannot be expressed" in r.message for r in caplog.records)

    async def test_no_intent_keeps_shape_based_default(self):
        """None preserves the pre-#1118 rule: 3.x structured → low, plain →
        native dynamic thinking."""
        provider = _gemini_provider("gemini-3.5-flash")
        structured = await _sent_body(
            provider, _GEMINI_RESP, response_format=_RESPONSE_FORMAT
        )
        assert structured["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "low"
        }
        plain = await _sent_body(provider, _GEMINI_RESP)
        assert "thinkingConfig" not in plain["generationConfig"]

    async def test_router_knobs_never_leak_into_request_body(self):
        provider = _gemini_provider("gemini-3.5-flash")
        body = await _sent_body(
            provider,
            _GEMINI_RESP,
            reasoning_intent="extraction",
            min_output_tokens=500,
        )
        flat = str(body)
        assert "reasoning_intent" not in flat
        assert "min_output_tokens" not in flat


# --- Providers that do not act on the intent ---------------------------------


_ANTHROPIC_RESP = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 5},
}

_COHERE_RESP = {
    "message": {"content": "ok"},
    "finish_reason": "COMPLETE",
    "usage": {"tokens": {"input_tokens": 5, "output_tokens": 5}},
}

_HF_RESP = [{"generated_text": "ok"}]


def _body_has_no_router_knobs(body) -> bool:
    """The knobs must appear NOWHERE in the outgoing body — not as top-level
    keys, not nested (HuggingFace tucks kwargs under ``parameters``, the local
    Ollama path under ``options``)."""
    flat = str(body)
    return "reasoning_intent" not in flat and "min_output_tokens" not in flat


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestNoMechanismProviders:
    """Exhaustive over the providers that discard the knobs — not one
    representative. Deleting a single provider's ``_discard_reasoning_kwargs``
    call must fail THAT provider's test here, because several of them merge
    leftover kwargs straight into the request body and would 400 (or silently
    send garbage) in production."""

    async def test_fireworks_pops_knobs_and_logs_intent(self, caplog):
        provider = FireworksProvider(
            _config(
                "fireworks",
                "accounts/fireworks/models/deepseek-v4-flash",
                "https://api.fireworks.ai/inference/v1",
            )
        )
        with caplog.at_level(logging.INFO):
            body = await _sent_body(
                provider,
                _OPENAI_RESP,
                reasoning_intent=ReasoningIntent.INFERENCE,
                min_output_tokens=500,
            )
        assert _body_has_no_router_knobs(body)
        assert any(
            "does not act on reasoning intent" in r.message for r in caplog.records
        )

    async def test_groq_pops_knobs(self):
        from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider

        provider = GroqProvider(
            _config("groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1")
        )
        body = await _sent_body(
            provider,
            _OPENAI_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert _body_has_no_router_knobs(body)

    async def test_cohere_pops_knobs(self):
        from faultmaven.infrastructure.llm.providers.cohere_provider import (
            CohereProvider,
        )

        provider = CohereProvider(
            _config("cohere", "command-r-plus", "https://api.cohere.ai/v2")
        )
        body = await _sent_body(
            provider,
            _COHERE_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert _body_has_no_router_knobs(body)

    async def test_huggingface_pops_knobs(self):
        from faultmaven.infrastructure.llm.providers.huggingface import (
            HuggingFaceProvider,
        )

        provider = HuggingFaceProvider(
            _config(
                "huggingface",
                "mistralai/Mistral-Large-Instruct-2411",
                "https://api-inference.huggingface.co/models",
            )
        )
        body = await _sent_body(
            provider,
            _HF_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert _body_has_no_router_knobs(body)

    async def test_local_pops_knobs(self):
        """The local provider pops in generate() BEFORE transport dispatch —
        its Ollama path merges raw kwargs into payload['options']."""
        from faultmaven.infrastructure.llm.providers.local_provider import (
            LocalProvider,
        )

        provider = LocalProvider(_config("local", "llama3", "http://localhost:5000"))
        body = await _sent_body(
            provider,
            _OPENAI_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert _body_has_no_router_knobs(body)

    async def test_anthropic_pops_knobs(self):
        """Anthropic discards via the shared seam for now (the concurrent
        extended-thinking work replaces that call with a real translation) —
        until then the knobs must not reach the Messages API body."""
        from faultmaven.infrastructure.llm.providers.anthropic import (
            AnthropicProvider,
        )

        provider = AnthropicProvider(
            _config("anthropic", "claude-sonnet-4-6", "https://api.anthropic.com/v1")
        )
        body = await _sent_body(
            provider,
            _ANTHROPIC_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert _body_has_no_router_knobs(body)
