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
from faultmaven.infrastructure.llm.providers.openrouter_provider import (
    OpenRouterProvider,
)


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


async def _sent_body(make_session, provider, response_data, **generate_kwargs):
    """Run generate() against a mocked HTTP boundary; return the sent body.

    ``make_session`` is the shared ``mock_aiohttp_session`` factory fixture
    (tests/unit/infrastructure/llm/conftest.py) — passed in rather than
    imported so this file holds no copy of the aiohttp double.
    """
    mock_session = make_session(response_data)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        await provider.generate("Test", **generate_kwargs)
    call_kwargs = mock_session.post.call_args
    return call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")


# --- OpenAI ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestOpenAIIntentTranslation:
    async def test_tools_on_gpt56_force_none_even_for_inference(
        self, mock_aiohttp_session, caplog
    ):
        """Hard constraint over caller intent: tools on /chat/completions run
        with reasoning off no matter what was asked — and the unhonoured
        intent is logged, not silently dropped."""
        provider = _openai_provider("gpt-5.6-mini")
        with caplog.at_level(logging.WARNING):
            body = await _sent_body(
                mock_aiohttp_session,
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

    async def test_inference_maps_to_medium_on_structured_call(
        self, mock_aiohttp_session
    ):
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.INFERENCE,
        )
        assert body["reasoning_effort"] == "medium"

    async def test_inference_maps_to_medium_on_plain_gpt56(self, mock_aiohttp_session):
        provider = _openai_provider("gpt-5.6-mini")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
        )
        assert body["reasoning_effort"] == "medium"

    async def test_extraction_maps_to_low_on_structured_call(
        self, mock_aiohttp_session
    ):
        """ "none" is only verified on the 5.6 family; "low" is the
        broadly-valid floor across gpt-5 and o-series."""
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.EXTRACTION,
        )
        assert body["reasoning_effort"] == "low"

    async def test_extraction_maps_to_none_on_plain_gpt56(self, mock_aiohttp_session):
        provider = _openai_provider("gpt-5.6-mini")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            reasoning_intent=ReasoningIntent.EXTRACTION,
        )
        assert body["reasoning_effort"] == "none"

    async def test_extraction_maps_to_low_on_structured_gpt56(
        self, mock_aiohttp_session
    ):
        """response_format on 5.6: "none" is unverified on that shape — the
        translation stays at the verified "low"."""
        provider = _openai_provider("gpt-5.6-mini")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.EXTRACTION,
        )
        assert body["reasoning_effort"] == "low"

    async def test_non_reasoning_model_never_gets_the_param(
        self, mock_aiohttp_session, caplog
    ):
        """gpt-4o rejects reasoning_effort; INFERENCE cannot be applied and
        must be logged at WARNING."""
        provider = _openai_provider("gpt-4o")
        with caplog.at_level(logging.WARNING):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        assert "reasoning_effort" not in body
        assert any(
            "not applied" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    async def test_extraction_on_param_rejecting_model_is_logged(
        self, mock_aiohttp_session, caplog
    ):
        """An EXTRACTION that cannot be applied must NOT exit silently — the
        model still reasons at its own default, billing hidden reasoning
        against the shared output budget, which is precisely the starvation
        EXTRACTION was declared to prevent. Reachable via o1-mini/o1-preview
        and via OpenRouter, which opts out of the parameter wholesale."""
        provider = _openai_provider("o1-mini")
        with caplog.at_level(logging.INFO):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                reasoning_intent=ReasoningIntent.EXTRACTION,
            )
        assert "reasoning_effort" not in body
        assert any(
            "extraction" in r.message and "not applied" in r.message
            for r in caplog.records
        ), "EXTRACTION was dropped with no log at all"

    async def test_param_rejecting_log_does_not_claim_no_reasoning_happened(
        self, mock_aiohttp_session, caplog
    ):
        """OpenRouter returns caps=False because it drives reasoning through
        its own gateway object, not because the model lacks reasoning — on
        openai/gpt-5 the model reasons natively. A log saying "proceeding
        without reasoning" would have an operator rule out the true cause of
        budget starvation."""
        provider = OpenRouterProvider(
            _config("openrouter", "openai/gpt-5", "https://openrouter.ai/api/v1")
        )
        with caplog.at_level(logging.WARNING):
            await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        messages = " ".join(r.message for r in caplog.records)
        assert "without reasoning" not in messages
        assert "does not accept reasoning_effort" in messages

    async def test_tools_on_non_reasoning_model_does_not_cite_responses_api(
        self, mock_aiohttp_session, caplog
    ):
        """gpt-4o + tools must not be sent after a /v1/responses migration —
        that endpoint would not help a model with no reasoning mode.

        The message states the mechanism (the parameter is not accepted)
        without asserting whether the model reasons: the same arm serves
        OpenRouter, whose models all DO reason (B3)."""
        provider = _openai_provider("gpt-4o")
        with caplog.at_level(logging.WARNING):
            await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                tools=_TOOLS,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        messages = " ".join(r.message for r in caplog.records)
        assert "does not accept reasoning_effort" in messages
        assert "/v1/responses" not in messages
        # Must not assert the model HAS no reasoning mode — the same arm is
        # reached by OpenRouter routes that reason natively.
        assert "no reasoning mode to enable" not in messages

    async def test_tools_on_reasoning_family_says_effort_is_absent(
        self, mock_aiohttp_session, caplog
    ):
        """gpt-5.4-mini + tools: no effort parameter is sent and the model
        reasons at its default — "proceeding with reasoning off" was the
        opposite of what happens."""
        provider = _openai_provider("gpt-5.4-mini")
        with caplog.at_level(logging.WARNING):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                tools=_TOOLS,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        assert "reasoning_effort" not in body
        messages = " ".join(r.message for r in caplog.records)
        assert "reasons at its own default" in messages

    async def test_extraction_with_tools_on_gpt56_is_not_logged_as_a_failure(
        self, mock_aiohttp_session, caplog
    ):
        """Supplement B: on gpt-5.6 + tools the forced ``reasoning_effort:
        "none"`` IS what EXTRACTION asked for, at the minimum the family
        accepts — it is on the wire. A log claiming the API would 400 on the
        combination the code just sent successfully spends the signal these
        logs exist to carry."""
        provider = _openai_provider("gpt-5.6-mini")
        with caplog.at_level(logging.INFO):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                tools=_TOOLS,
                reasoning_intent=ReasoningIntent.EXTRACTION,
            )
        assert body["reasoning_effort"] == "none"
        assert not any(
            "extraction" in r.message for r in caplog.records
        ), "EXTRACTION was honoured exactly; nothing should report a failure"

    async def test_explicit_reasoning_effort_kwarg_overrides_intent(
        self, mock_aiohttp_session
    ):
        """The documented escape hatch survives: an explicit effort threaded
        through kwargs still has the last word over the intent translation."""
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.EXTRACTION,
            reasoning_effort="high",
        )
        assert body["reasoning_effort"] == "high"

    async def test_router_knobs_never_leak_into_request_body(
        self, mock_aiohttp_session
    ):
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            reasoning_intent="extraction",
            min_output_tokens=500,
        )
        assert "reasoning_intent" not in body
        assert "min_output_tokens" not in body

    async def test_string_intent_accepted_on_direct_provider_call(
        self, mock_aiohttp_session
    ):
        """The router normalizes, but providers are also called directly —
        the string spelling must translate identically."""
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent="inference",
        )
        assert body["reasoning_effort"] == "medium"

    async def test_no_intent_keeps_shape_based_default(self, mock_aiohttp_session):
        """None preserves the pre-#1118 defaults exactly (structured → low)."""
        provider = _openai_provider("gpt-5.4-mini")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            response_format=_RESPONSE_FORMAT,
        )
        assert body["reasoning_effort"] == "low"


# --- Gemini ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestGeminiIntentTranslation:
    async def test_inference_without_floor_keeps_the_cap_and_warns(
        self, mock_aiohttp_session, caplog
    ):
        """The provider fails CLOSED on its own. The router raises on
        INFERENCE-without-floor, but the router is not the only door:
        ``milestone_engine`` binds a concrete provider and calls generate() on
        it directly, so an invariant enforced only in the router is not
        enforced at all. Without a floor the cap must stay."""
        provider = _gemini_provider("gemini-3.5-flash")
        with caplog.at_level(logging.WARNING):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _GEMINI_RESP,
                response_format=_RESPONSE_FORMAT,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
        assert any(
            "REFUSED" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )

    async def test_unhonourable_inference_warns_not_infos(
        self, mock_aiohttp_session, caplog
    ):
        """#7: at INFO this was invisible under the runbook's
        LOG_LEVEL=WARNING, making diagnosability a function of which provider
        answered — OpenAI warned for the identical condition."""
        provider = _gemini_provider("gemini-2.5-pro")
        with caplog.at_level(logging.WARNING):
            await _sent_body(
                mock_aiohttp_session,
                provider,
                _GEMINI_RESP,
                response_format=_RESPONSE_FORMAT,
                reasoning_intent=ReasoningIntent.INFERENCE,
                min_output_tokens=500,
            )
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    async def test_inference_with_floor_lifts_structured_thinking_cap_on_3x(
        self, mock_aiohttp_session, caplog
    ):
        """Native dynamic thinking IS the model reasoning at its own
        discretion — the honoured INFERENCE translation is to not cap it, and
        the declared floor is what makes lifting the guard safe."""
        provider = _gemini_provider("gemini-3.5-flash")
        with caplog.at_level(logging.INFO):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _GEMINI_RESP,
                response_format=_RESPONSE_FORMAT,
                reasoning_intent=ReasoningIntent.INFERENCE,
                min_output_tokens=500,
            )
        assert "thinkingConfig" not in body["generationConfig"]
        assert any("lifting the thinkingLevel" in r.message for r in caplog.records)

    async def test_extraction_caps_thinking_on_plain_3x_call(
        self, mock_aiohttp_session
    ):
        """EXTRACTION applies the cap on every shape — plain calls included,
        where the shape-based default would leave thinking dynamic."""
        provider = _gemini_provider("gemini-3.5-flash")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _GEMINI_RESP,
            reasoning_intent=ReasoningIntent.EXTRACTION,
        )
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}

    async def test_extraction_keeps_cap_on_structured_3x_call(
        self, mock_aiohttp_session
    ):
        provider = _gemini_provider("gemini-3.5-flash")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _GEMINI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.EXTRACTION,
        )
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}

    @pytest.mark.parametrize("model", ["gemini-2.5-pro", "gemini-1.5-pro"])
    async def test_pre_3x_models_get_no_thinking_config_and_a_log(
        self, mock_aiohttp_session, model, caplog
    ):
        """thinkingLevel is 3.x vocabulary — earlier models have no knob this
        provider can turn, so the intent is logged and nothing is sent."""
        provider = _gemini_provider(model)
        with caplog.at_level(logging.INFO):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _GEMINI_RESP,
                response_format=_RESPONSE_FORMAT,
                reasoning_intent=ReasoningIntent.EXTRACTION,
            )
        assert "thinkingConfig" not in body["generationConfig"]
        assert any("cannot be expressed" in r.message for r in caplog.records)

    async def test_no_intent_keeps_shape_based_default(self, mock_aiohttp_session):
        """None preserves the pre-#1118 rule: 3.x structured → low, plain →
        native dynamic thinking."""
        provider = _gemini_provider("gemini-3.5-flash")
        structured = await _sent_body(
            mock_aiohttp_session,
            provider,
            _GEMINI_RESP,
            response_format=_RESPONSE_FORMAT,
        )
        assert structured["generationConfig"]["thinkingConfig"] == {
            "thinkingLevel": "low"
        }
        plain = await _sent_body(mock_aiohttp_session, provider, _GEMINI_RESP)
        assert "thinkingConfig" not in plain["generationConfig"]

    async def test_router_knobs_never_leak_into_request_body(
        self, mock_aiohttp_session
    ):
        provider = _gemini_provider("gemini-3.5-flash")
        body = await _sent_body(
            mock_aiohttp_session,
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

    async def test_fireworks_pops_knobs_and_logs_intent(
        self, mock_aiohttp_session, caplog
    ):
        provider = FireworksProvider(
            _config(
                "fireworks",
                "accounts/fireworks/models/deepseek-v4-flash",
                "https://api.fireworks.ai/inference/v1",
            )
        )
        with caplog.at_level(logging.INFO):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                reasoning_intent=ReasoningIntent.INFERENCE,
                min_output_tokens=500,
            )
        assert _body_has_no_router_knobs(body)
        assert any(
            "does not act on reasoning intent" in r.message for r in caplog.records
        )

    async def test_groq_pops_knobs(self, mock_aiohttp_session):
        from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider

        provider = GroqProvider(
            _config("groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1")
        )
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert _body_has_no_router_knobs(body)

    async def test_cohere_pops_knobs(self, mock_aiohttp_session):
        from faultmaven.infrastructure.llm.providers.cohere_provider import (
            CohereProvider,
        )

        provider = CohereProvider(
            _config("cohere", "command-r-plus", "https://api.cohere.ai/v2")
        )
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _COHERE_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert _body_has_no_router_knobs(body)

    async def test_huggingface_pops_knobs(self, mock_aiohttp_session):
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
            mock_aiohttp_session,
            provider,
            _HF_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert _body_has_no_router_knobs(body)

    async def test_local_pops_knobs(self, mock_aiohttp_session):
        """Covers the OpenAI-compatible transport, which this base_url selects.
        The Ollama path is covered separately below."""
        from faultmaven.infrastructure.llm.providers.local_provider import (
            LocalProvider,
        )

        provider = LocalProvider(_config("local", "llama3", "http://localhost:5000"))
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert _body_has_no_router_knobs(body)

    async def test_anthropic_pops_knobs(self, mock_aiohttp_session):
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
            mock_aiohttp_session,
            provider,
            _ANTHROPIC_RESP,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert _body_has_no_router_knobs(body)


@pytest.mark.unit
@pytest.mark.llm
class TestShapeDefaultEffortIsOnePolicy:
    """#13: the shape→effort policy had two implementations in different
    vocabulary — two guarded assignments in ``generate()`` and a ternary in
    ``_apply_reasoning_intent``. They agreed, but the next family verified
    for "none" on structured calls would be added to one and not the other,
    and the stale copy would still look correct because the intent tests pin
    the old mapping independently. These pin the SINGLE helper against the
    behaviour both sites must produce.
    """

    @pytest.mark.parametrize(
        "defaults_reasoning,has_response_format,expected",
        [
            (True, True, "low"),  # structured wins over the plain default
            (False, True, "low"),  # structured on a non-default family
            (True, False, "none"),  # plain chat on a default-reasoning family
            (False, False, None),  # plain chat elsewhere: no default at all
        ],
    )
    def test_policy_table(self, defaults_reasoning, has_response_format, expected):
        assert (
            OpenAIProvider._shape_default_effort(
                defaults_reasoning, has_response_format
            )
            == expected
        )

    @pytest.mark.asyncio
    async def test_generate_and_intent_agree_wherever_the_shape_has_a_default(
        self, mock_aiohttp_session
    ):
        """The property that matters: wherever the SHAPE defines a default,
        declaring EXTRACTION — a request for that shape's own minimum — puts
        the same value on the wire as not declaring it. Both call sites read
        the one helper, so they cannot drift apart.

        Where the shape has NO default (a plain call on a family that does not
        reason unasked), the two legitimately differ: nothing is sent without
        an intent, and EXTRACTION explicitly requests the verified minimum.
        That difference is the feature, so it is asserted rather than elided.
        """
        for model, defaults_reasoning in (
            ("gpt-5.6-mini", True),
            ("gpt-5.4-mini", False),
        ):
            for response_format in (None, _RESPONSE_FORMAT):
                provider = _openai_provider(model)
                kwargs = {"response_format": response_format} if response_format else {}
                without = await _sent_body(
                    mock_aiohttp_session, provider, _OPENAI_RESP, **kwargs
                )
                with_intent = await _sent_body(
                    mock_aiohttp_session,
                    provider,
                    _OPENAI_RESP,
                    reasoning_intent=ReasoningIntent.EXTRACTION,
                    **kwargs,
                )
                shape_default = OpenAIProvider._shape_default_effort(
                    defaults_reasoning, bool(response_format)
                )
                assert without.get("reasoning_effort") == shape_default
                if shape_default is not None:
                    assert with_intent.get("reasoning_effort") == shape_default, (
                        f"EXTRACTION diverged from the shape default for "
                        f"{model} rf={bool(response_format)}"
                    )
                else:
                    # No shape default: EXTRACTION still asks for the minimum.
                    assert with_intent.get("reasoning_effort") == "low"


@pytest.mark.unit
@pytest.mark.llm
class TestReasoningIntentCoerce:
    """#15: three modules copy-pasted the same pop-and-coerce block. Every
    consumer compares with ``is`` against a member, so a raw string that
    reaches a comparison evaluates False and the intent is silently ignored —
    no exception, no log. One classmethod, used at every entry point."""

    def test_none_passes_through(self):
        assert ReasoningIntent.coerce(None) is None

    def test_member_is_idempotent(self):
        assert (
            ReasoningIntent.coerce(ReasoningIntent.INFERENCE)
            is ReasoningIntent.INFERENCE
        )

    def test_string_becomes_the_member(self):
        assert ReasoningIntent.coerce("extraction") is ReasoningIntent.EXTRACTION

    def test_unknown_spelling_raises_at_the_boundary(self):
        with pytest.raises(ValueError):
            ReasoningIntent.coerce("thinking-hard")


@pytest.mark.unit
@pytest.mark.llm
class TestEveryRegisteredProviderHandlesTheKnobs:
    def test_every_registry_provider_discards_the_knobs(self):
        """Derived from PROVIDER_SCHEMA, not hand-enumerated: a TENTH provider
        added to the registry is covered the moment it is registered.

        The per-provider payload tests below prove the discard on the wire for
        the ones that merge raw kwargs; this proves no registered provider was
        simply forgotten. ``_merge_extra_kwargs`` is the mechanism that makes
        it hold — a provider using it cannot leak, so this asserts either the
        merge helper or an explicit discard is present in the generate path.
        """
        import inspect

        from faultmaven.infrastructure.llm.providers.registry import PROVIDER_SCHEMA

        missing = []
        for name, schema in PROVIDER_SCHEMA.items():
            cls = schema["provider_class"]
            source = inspect.getsource(cls)
            # Subclasses inherit the parent's generate() (OpenRouter/OpenAI).
            if "async def generate" not in source:
                source += inspect.getsource(cls.__mro__[1])
            handles_knobs = (
                # Discards them via the shared mechanism…
                "_merge_extra_kwargs" in source
                or "_discard_reasoning_kwargs" in source
                # …or consumes both itself (Gemini translates the intent and
                # reads the floor to decide whether it may lift its cap).
                or ("reasoning_intent" in source and "min_output_tokens" in source)
            )
            if not handles_knobs:
                missing.append(name)
        assert not missing, (
            f"registered providers with no knob discard on the generate path: "
            f"{missing} — they will send reasoning_intent/min_output_tokens "
            f"as API body fields"
        )


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestRefusedInferenceRestoresTheShapeDefault:
    """B2: refusing an intent must restore the SHAPE DEFAULT, never impose a
    restriction the default did not apply.

    Failing closed on a STRUCTURED call keeps a cap that genuinely exists. On
    a PLAIN call there was never a cap — so capping it on refusal meant
    declaring INFERENCE bought LESS thinking than declaring nothing, which is
    the opposite of what the caller asked for.
    """

    async def test_plain_call_refusal_leaves_no_thinking_config(
        self, mock_aiohttp_session, caplog
    ):
        provider = _gemini_provider("gemini-3.5-flash")
        with caplog.at_level(logging.INFO):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _GEMINI_RESP,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        assert "thinkingConfig" not in body["generationConfig"]
        # The report must not claim a cap was kept on a shape that never had
        # one. What it says about this shape is pinned by
        # TestPlainRefusalReadsAsWhatHappened below; here we only require that
        # nothing claims a cap was retained.
        messages = " ".join(r.message for r in caplog.records)
        assert "keeping the cap" not in messages

    async def test_structured_call_refusal_still_keeps_the_cap(
        self, mock_aiohttp_session, caplog
    ):
        provider = _gemini_provider("gemini-3.5-flash")
        with caplog.at_level(logging.WARNING):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _GEMINI_RESP,
                response_format=_RESPONSE_FORMAT,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}
        assert any("keeping the cap" in r.message for r in caplog.records)

    async def test_declaring_inference_never_buys_less_than_declaring_nothing(
        self, mock_aiohttp_session
    ):
        """The property behind B2, stated directly: on every shape, a refused
        INFERENCE must land on exactly the config the same call would have got
        with no intent at all."""
        for kwargs in ({}, {"response_format": _RESPONSE_FORMAT}):
            provider = _gemini_provider("gemini-3.5-flash")
            no_intent = await _sent_body(
                mock_aiohttp_session, provider, _GEMINI_RESP, **kwargs
            )
            refused = await _sent_body(
                mock_aiohttp_session,
                provider,
                _GEMINI_RESP,
                reasoning_intent=ReasoningIntent.INFERENCE,
                **kwargs,
            )
            assert no_intent["generationConfig"].get("thinkingConfig") == refused[
                "generationConfig"
            ].get("thinkingConfig")


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestFalsyFloorIsNotADeclaredFloor:
    """R2: the fail-closed guard exists for the door that BYPASSES route()
    (``milestone_engine`` binds a concrete provider), so it cannot borrow the
    router's validation. ``0``/``False``/``""`` are not declared floors —
    accepting them lifts the cap and then logs that a floor will catch the
    starved body it can no longer catch."""

    @pytest.mark.parametrize("falsy_floor", [0, False, ""])
    async def test_falsy_floor_does_not_lift_the_cap(
        self, mock_aiohttp_session, falsy_floor
    ):
        provider = _gemini_provider("gemini-3.5-flash")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _GEMINI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=falsy_floor,
        )
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "low"}

    async def test_real_floor_still_lifts_the_cap(self, mock_aiohttp_session):
        provider = _gemini_provider("gemini-3.5-flash")
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _GEMINI_RESP,
            response_format=_RESPONSE_FORMAT,
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=500,
        )
        assert "thinkingConfig" not in body["generationConfig"]


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestToolsBranchStatesTheRealMechanism:
    """B3: the has_tools branch had one arm for two populations.

    ``_caps_reasoning_effort`` False means "does not take this parameter", NOT
    "does not reason". OpenRouter overrides it to False unconditionally — and
    tools are its NORMAL shape — so every gateway route was being told it had
    no reasoning mode, on models that all reason.
    """

    async def test_openrouter_with_tools_is_not_told_it_cannot_reason(
        self, mock_aiohttp_session, caplog
    ):
        provider = OpenRouterProvider(
            _config(
                "openrouter",
                "anthropic/claude-sonnet-4-6",
                "https://openrouter.ai/api/v1",
            )
        )
        with caplog.at_level(logging.WARNING):
            body = await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                tools=_TOOLS,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        assert "reasoning_effort" not in body
        messages = " ".join(r.message for r in caplog.records)
        assert "no reasoning mode to enable" not in messages
        assert "does not accept reasoning_effort" in messages

    @pytest.mark.parametrize(
        "provider_factory,model",
        [
            (_openai_provider, "gpt-4o"),
            (_openai_provider, "o1-mini"),
        ],
    )
    async def test_extraction_with_tools_and_no_param_support_is_logged(
        self, mock_aiohttp_session, caplog, provider_factory, model
    ):
        """#3 on the has_tools path: EXTRACTION matched no arm and returned
        with no log at all, which made the docstring and CLAUDE.md false."""
        provider = provider_factory(model)
        with caplog.at_level(logging.INFO):
            await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                tools=_TOOLS,
                reasoning_intent=ReasoningIntent.EXTRACTION,
            )
        assert any(
            "extraction" in r.message and "not applied" in r.message
            for r in caplog.records
        ), "EXTRACTION was dropped with no log on the tools path"


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestDiscardPathIsTolerant:
    """R1: ``_discard_reasoning_kwargs`` is a pure LOGGING path in providers
    with no reasoning mechanism. Coercing (and so raising) there turned an
    unrecognised intent into a hard failure from a provider that was never
    going to act on it — and behind the fallback chain every provider raises
    the same thing, surfacing as "All providers failed". Validation belongs at
    ``route()``, which rejects the same value loudly at the call site."""

    async def test_unknown_intent_string_does_not_raise_from_a_logging_path(
        self, mock_aiohttp_session
    ):
        from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider

        provider = GroqProvider(
            _config("groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1")
        )
        body = await _sent_body(
            mock_aiohttp_session,
            provider,
            _OPENAI_RESP,
            reasoning_intent="thinking-hard",
        )
        assert "reasoning_intent" not in body

    async def test_uncoerced_inference_string_still_warns(
        self, mock_aiohttp_session, caplog
    ):
        """Tolerance must not silently demote the INFERENCE warning to INFO —
        the level is classified on the VALUE, not on member identity."""
        from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider

        provider = GroqProvider(
            _config("groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1")
        )
        with caplog.at_level(logging.WARNING):
            await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                reasoning_intent="inference",
            )
        assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestLlamaCppTransportKeepsCachePrompt:
    """``cache_prompt`` is a REAL llama.cpp body field, and this transport had
    never been covered — ``test_local_pops_knobs``'s base_url routes to the
    OpenAI-compatible transport, so routing llama.cpp through the shared merge
    seam silently dropped a field that reached the wire at merge-base.

    ``milestone_engine`` passes ``cache_prompt=True`` on every DA tool-loop
    iteration, so llama.cpp-fallback deployments lost prompt-prefix caching.
    """

    def _llamacpp_provider(self):
        from faultmaven.infrastructure.llm.providers.local_provider import (
            LocalProvider,
        )

        # A base_url with no "ollama" in it, and a 404 from the
        # OpenAI-compatible endpoint, is what selects the llama.cpp transport.
        return LocalProvider(_config("local", "llama3", "http://localhost:8080"))

    async def _llamacpp_body(self, make_session, **generate_kwargs):
        """Drive generate() so it falls through to the llama.cpp transport."""
        from unittest.mock import AsyncMock, MagicMock

        openai_attempt = AsyncMock()
        openai_attempt.status = 404
        openai_attempt.json = AsyncMock(return_value={})
        openai_attempt.text = AsyncMock(return_value="not found")
        openai_attempt.__aenter__ = AsyncMock(return_value=openai_attempt)
        openai_attempt.__aexit__ = AsyncMock(return_value=False)

        llamacpp_attempt = AsyncMock()
        llamacpp_attempt.status = 200
        llamacpp_attempt.json = AsyncMock(
            return_value={"content": "ok", "stopped_limit": False}
        )
        llamacpp_attempt.text = AsyncMock(return_value="")
        llamacpp_attempt.__aenter__ = AsyncMock(return_value=llamacpp_attempt)
        llamacpp_attempt.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(side_effect=[openai_attempt, llamacpp_attempt])
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=session):
            await self._llamacpp_provider().generate("Test", **generate_kwargs)

        # The second post is the llama.cpp /completion call.
        last = session.post.call_args_list[-1]
        assert "/completion" in last.args[0]
        return last.kwargs.get("json") or last[1].get("json")

    async def test_cache_prompt_reaches_the_completion_body(self, mock_aiohttp_session):
        body = await self._llamacpp_body(mock_aiohttp_session, cache_prompt=True)
        assert body["cache_prompt"] is True

    async def test_router_knobs_still_do_not_reach_the_completion_body(
        self, mock_aiohttp_session
    ):
        body = await self._llamacpp_body(
            mock_aiohttp_session,
            cache_prompt=True,
            reasoning_intent=ReasoningIntent.EXTRACTION,
            min_output_tokens=500,
        )
        assert body["cache_prompt"] is True
        flat = str(body)
        assert "reasoning_intent" not in flat
        assert "min_output_tokens" not in flat

    async def test_absent_cache_prompt_is_not_invented(self, mock_aiohttp_session):
        body = await self._llamacpp_body(mock_aiohttp_session)
        assert "cache_prompt" not in body


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestPlainRefusalReadsAsWhatHappened:
    """Item 6: on a plain call nothing is refused — the outcome is identical
    to a floored INFERENCE — so it must not announce a REFUSAL at WARNING, nor
    advise a floor that would change nothing on that shape."""

    async def test_plain_call_does_not_warn_or_claim_refusal(
        self, mock_aiohttp_session, caplog
    ):
        provider = _gemini_provider("gemini-3.5-flash")
        with caplog.at_level(logging.INFO):
            await _sent_body(
                mock_aiohttp_session,
                provider,
                _GEMINI_RESP,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        assert not any(r.levelno >= logging.WARNING for r in caplog.records)
        messages = " ".join(r.message for r in caplog.records)
        assert "REFUSED" not in messages
        assert "no thinkingLevel cap applies" in messages

    async def test_structured_call_still_warns_about_the_refusal(
        self, mock_aiohttp_session, caplog
    ):
        provider = _gemini_provider("gemini-3.5-flash")
        with caplog.at_level(logging.INFO):
            await _sent_body(
                mock_aiohttp_session,
                provider,
                _GEMINI_RESP,
                response_format=_RESPONSE_FORMAT,
                reasoning_intent=ReasoningIntent.INFERENCE,
            )
        assert any(
            "REFUSED" in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        )


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestIntentLevelIsCaseInsensitive:
    """An uncoerced ``"INFERENCE"`` meant the same thing as ``"inference"``;
    an exact string compare logged it at INFO, where the runbook's
    LOG_LEVEL=WARNING hides it."""

    @pytest.mark.parametrize("spelling", ["INFERENCE", "Inference", " inference "])
    async def test_uppercase_inference_still_warns(
        self, mock_aiohttp_session, caplog, spelling
    ):
        from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider

        provider = GroqProvider(
            _config("groq", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1")
        )
        with caplog.at_level(logging.INFO):
            await _sent_body(
                mock_aiohttp_session,
                provider,
                _OPENAI_RESP,
                reasoning_intent=spelling,
            )
        assert any(r.levelno == logging.WARNING for r in caplog.records)
