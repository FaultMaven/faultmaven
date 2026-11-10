"""Whether a model takes a non-default ``temperature`` is LEARNED from the endpoint.

The adapter omits ``temperature`` for the families listed as reasoning by
default (``_DEFAULT_REASONING_MODEL_FAMILIES``) and sends it for the rest. That
list is a guess about models, but the answer belongs to the endpoint: Azure AI
Foundry's gpt-5.4-mini deployment refuses ``temperature: 0.2`` ("Only the
default (1) value is supported") while OpenAI's own endpoint accepts it, so
every structured call to that deployment failed before any generation
(2026-11-10).

The rule is #510's, applied to ``temperature``. A permanent rejection that names
the parameter gets one in-line re-send without it. The model is remembered only
once that re-send succeeds, and nothing else ever buys a re-send.
"""

import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import LLMException
from faultmaven.infrastructure.llm.dispatch import send_to_model
from faultmaven.infrastructure.llm.providers.azure_provider import AzureProvider
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider
from tests.model_content import vetted

LEGACY = "max_tokens"
MODERN = "max_completion_tokens"


def _config_for(model: str, *, name: str = "openai") -> ProviderConfig:
    return ProviderConfig(
        name=name,
        api_key="test-key",
        base_url="https://example.test/openai/v1",
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
    )


def _ok(content: str = "ok"):
    return {
        "status": 200,
        "json": {
            "choices": [
                {
                    "message": {"content": content, "role": "assistant"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"total_tokens": 10},
        },
    }


def _error(status: int, body: dict):
    return {"status": status, "text": json.dumps(body)}


def _refuses_temperature(value: float = 0.2) -> dict:
    """The body Azure AI Foundry returned for gpt-5.4-mini, verbatim in shape."""
    return {
        "error": {
            "message": (
                f"Unsupported value: 'temperature' does not support {value} with "
                "this model. Only the default (1) value is supported."
            ),
            "type": "invalid_request_error",
            "param": "temperature",
            "code": "unsupported_value",
        }
    }


def _refuses_param(param: str, recommend: str) -> dict:
    return {
        "error": {
            "message": (
                f"Unsupported parameter: '{param}' is not supported with this "
                f"model. Use '{recommend}' instead."
            ),
            "type": "invalid_request_error",
            "param": param,
            "code": "unsupported_parameter",
        }
    }


def _sequenced_session(specs):
    """A session answering *specs* in order; bodies are deep-copied as sent.

    ``_generate()`` mutates one payload in place across attempts, so keeping
    the reference would make every "what changed" assertion vacuous. An
    IndexError means more requests were sent than the test allowed: a loop.
    """
    responses = []
    for spec in specs:
        resp = AsyncMock()
        resp.status = spec["status"]
        resp.json = AsyncMock(return_value=spec.get("json", {}))
        resp.text = AsyncMock(return_value=spec.get("text", ""))
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


async def _send(provider, session, **kwargs):
    with patch("aiohttp.ClientSession", return_value=session):
        return await send_to_model(
            provider, prompt=vetted("hi"), max_tokens=512, temperature=0.2, **kwargs
        )


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestTemperatureRefusalIsLearned:
    async def test_a_refusal_is_answered_by_one_resend_without_temperature(self):
        provider = OpenAIProvider(_config_for("gpt-5.4-mini"))
        session, sent = _sequenced_session(
            [_error(400, _refuses_temperature()), _ok("served")]
        )

        result = await _send(provider, session)

        assert result.content == "served"
        assert len(sent) == 2, "exactly one re-send, never a loop"
        assert sent[0]["temperature"] == 0.2
        assert "temperature" not in sent[1]
        assert {k: v for k, v in sent[0].items() if k != "temperature"} == sent[1]

    async def test_the_next_call_omits_temperature_up_front(self):
        provider = OpenAIProvider(_config_for("gpt-5.4-mini"))
        first, _ = _sequenced_session([_error(400, _refuses_temperature()), _ok()])
        await _send(provider, first)

        second, sent = _sequenced_session([_ok("direct")])
        result = await _send(provider, second)

        assert result.content == "direct"
        assert len(sent) == 1 and "temperature" not in sent[0]

    async def test_the_lesson_is_per_model_and_per_instance(self):
        provider = OpenAIProvider(_config_for("gpt-5.4-mini"))
        session, _ = _sequenced_session([_error(400, _refuses_temperature()), _ok()])
        await _send(provider, session)

        assert provider._temperature_refused == {"gpt-5.4-mini"}
        assert OpenAIProvider(_config_for("gpt-5.4-mini"))._temperature_refused == set()

    async def test_a_failed_resend_is_raised_and_nothing_is_learned(self):
        provider = OpenAIProvider(_config_for("gpt-5.4-mini"))
        session, sent = _sequenced_session(
            [
                _error(400, _refuses_temperature()),
                _error(400, _refuses_param("response_format", "text")),
            ]
        )

        with pytest.raises(LLMException) as raised:
            await _send(provider, session)

        assert len(sent) == 2
        assert "response_format" in str(raised.value)
        assert "temperature" in str(raised.value.__cause__)
        assert provider._temperature_refused == set()

    async def test_a_model_needing_both_corrections_is_served_on_its_first_call(self):
        """Temperature first, then the token-cap spelling: one re-send each."""
        provider = OpenAIProvider(_config_for("gpt-6"))  # unlisted: legacy guess
        session, sent = _sequenced_session(
            [
                _error(400, _refuses_temperature()),
                _error(400, _refuses_param(LEGACY, MODERN)),
                _ok("both"),
            ]
        )

        result = await _send(provider, session)

        assert result.content == "both"
        assert len(sent) == 3
        assert "temperature" not in sent[2] and MODERN in sent[2]
        assert provider._temperature_refused == {"gpt-6"}
        assert provider._token_param_learned == {"gpt-6": MODERN}


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestNothingElseBuysAResend:
    @pytest.mark.parametrize(
        "status, body",
        [
            # Another parameter refused, its message mentioning temperature.
            (
                400,
                {
                    "error": {
                        "message": "Unsupported value: 'top_p' with temperature set.",
                        "type": "invalid_request_error",
                        "param": "top_p",
                        "code": "unsupported_value",
                    }
                },
            ),
            # A transient failure is the retry ladder's, never a re-shape.
            (
                429,
                {
                    "error": {
                        "message": "Rate limit reached.",
                        "type": "rate_limit_error",
                        "param": "temperature",
                        "code": "rate_limit_exceeded",
                    }
                },
            ),
        ],
    )
    async def test_it_is_raised_after_one_request(self, status, body):
        provider = OpenAIProvider(_config_for("gpt-5.4-mini"))
        session, sent = _sequenced_session([_error(status, body)])

        with pytest.raises(LLMException):
            await _send(provider, session)

        assert len(sent) == 1
        assert provider._temperature_refused == set()

    async def test_a_default_reasoning_family_never_sends_it_at_all(self):
        provider = OpenAIProvider(_config_for("gpt-5.6-luna"))
        session, sent = _sequenced_session([_ok()])

        await _send(provider, session)

        assert len(sent) == 1 and "temperature" not in sent[0]


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
async def test_the_azure_deployment_that_refused_it_is_served():
    """The 2026-11-10 failure: Azure's gpt-5.4-mini deployment, structured call."""
    provider = AzureProvider(_config_for("gpt-5.4-mini-1", name="azure"))
    session, sent = _sequenced_session(
        [_error(400, _refuses_temperature()), _ok('{"word": "ok"}')]
    )

    result = await _send(
        provider,
        session,
        response_format={"type": "json_object"},
    )

    assert result.content == '{"word": "ok"}'
    assert result.provider == "azure"
    assert "temperature" not in sent[1]
    assert sent[1]["response_format"] == {"type": "json_object"}
