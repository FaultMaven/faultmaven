"""The token-cap parameter name is LEARNED from the endpoint, not predicted.

``/v1/chat/completions`` takes the generation cap under one of two names:
the legacy ``max_tokens`` or the modern ``max_completion_tokens``. Which one
a given model accepts is a fact the endpoint owns. Before #510 the provider
answered it from an enumerated family list, so a family that rejects
``max_tokens`` but is not listed (``gpt-6``, ``o5``) silently fell back to the
legacy name and every call to it 400'd.

The list survives as the OPENING GUESS — right for every shipped
configuration, and worth a saved round trip. What changed is that it is no
longer load-bearing: a rejection that NAMES the spelling we sent is the
endpoint correcting us, and the provider re-sends once under the other name
and remembers the answer.

These tests pin the four things that make that safe:

* it fires on a rejection naming the parameter WE SENT, and on nothing else;
* it is bounded at one re-send, so it can never consume the retry ladder's
  budget for transient failures nor loop;
* the re-send differs from the rejected request in the KEY and nothing else;
* it holds for :class:`OpenRouterProvider`, which inherits ``generate()``.
"""

import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.exceptions import LLMErrorCategory, LLMException
from faultmaven.infrastructure.llm.providers.base import ProviderConfig
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider
from faultmaven.infrastructure.llm.providers.openrouter_provider import (
    OpenRouterProvider,
)
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
)

LEGACY = "max_tokens"
MODERN = "max_completion_tokens"


def _config_for(
    model: str,
    *,
    name: str = "openai",
    base_url: str = "https://api.openai.com/v1",
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        api_key="test-key",
        base_url=base_url,
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
    )


def _ok(content: str = "ok"):
    """A 200 completion."""
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


def _rejects_param(param: str, recommend: str) -> dict:
    """OpenAI's real ``unsupported_parameter`` body, verbatim in shape."""
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
    """A mock ``aiohttp.ClientSession`` answering *specs* in order.

    Returns ``(session, sent_bodies)``. Each request body is DEEP-COPIED as it
    is sent: ``generate()`` mutates one payload dict in place across the two
    attempts, so recording the reference would make both snapshots read as the
    corrected one and every "what changed between attempts" assertion vacuous.
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

    sent_bodies = []

    def _post(*args, **kwargs):
        sent_bodies.append(copy.deepcopy(kwargs.get("json")))
        # IndexError here means the code issued more requests than the test
        # allowed for — a loop, which is exactly what must never happen.
        return responses[len(sent_bodies) - 1]

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, sent_bodies


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestTokenParamCorrection:
    """A rejection naming the spelling we sent is acted on, once."""

    async def test_unlisted_family_is_corrected_instead_of_failing(self):
        """The #510 regression itself: a model family nobody listed.

        ``gpt-6`` matches no entry in ``_COMPLETION_TOKENS_MODEL_FAMILIES``, so
        the opening guess is the legacy name and the endpoint refuses it.
        Before #510 that 400 was the caller's answer. Now it is an instruction.
        """
        provider = OpenAIProvider(_config_for("gpt-6"))
        assert provider._uses_completion_tokens_param("gpt-6") is False  # unlisted
        session, sent = _sequenced_session(
            [_error(400, _rejects_param(LEGACY, MODERN)), _ok("corrected")]
        )

        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi", max_tokens=512)

        assert result.content == "corrected"
        assert len(sent) == 2, "exactly one re-send, never a loop"
        assert sent[0][LEGACY] == 512 and MODERN not in sent[0]
        assert sent[1][MODERN] == 512 and LEGACY not in sent[1]

    async def test_resend_differs_in_the_key_and_nothing_else(self):
        """The correction must not smuggle in any other payload change."""
        provider = OpenAIProvider(_config_for("gpt-6"))
        session, sent = _sequenced_session(
            [_error(400, _rejects_param(LEGACY, MODERN)), _ok()]
        )

        with patch("aiohttp.ClientSession", return_value=session):
            await provider.generate("hi", max_tokens=512, temperature=0.3)

        first, second = (
            {k: v for k, v in b.items() if k not in (LEGACY, MODERN)} for b in sent
        )
        assert first == second
        assert sent[0][LEGACY] == sent[1][MODERN] == 512

    async def test_the_answer_is_learned_so_later_calls_pay_nothing(self):
        """One round trip per model per provider instance, not per call."""
        provider = OpenAIProvider(_config_for("gpt-6"))
        session, sent = _sequenced_session(
            [_error(400, _rejects_param(LEGACY, MODERN)), _ok(), _ok()]
        )

        with patch("aiohttp.ClientSession", return_value=session):
            await provider.generate("one", max_tokens=100)
            await provider.generate("two", max_tokens=100)

        assert len(sent) == 3, "the second call must not re-pay the correction"
        assert MODERN in sent[2] and LEGACY not in sent[2]

    async def test_correction_runs_in_the_legacy_direction_too(self):
        """An endpoint that only knows ``max_tokens`` served a listed family.

        The mechanism is symmetric on purpose: the failure mode it answers is
        "our expectation is wrong", not "our expectation is old".
        """
        provider = OpenAIProvider(_config_for("gpt-5"))
        assert provider._uses_completion_tokens_param("gpt-5") is True
        session, sent = _sequenced_session(
            [
                _error(
                    400,
                    {
                        "error": {
                            "message": (
                                "Unrecognized request argument supplied: "
                                "max_completion_tokens"
                            ),
                            "type": "BadRequestError",
                        }
                    },
                ),
                _ok("legacy"),
            ]
        )

        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi", max_tokens=64)

        assert result.content == "legacy"
        assert MODERN in sent[0] and LEGACY not in sent[0]
        assert sent[1][LEGACY] == 64 and MODERN not in sent[1]

    async def test_learned_answer_does_not_leak_between_provider_instances(self):
        """The spelling is a property of the ENDPOINT as much as the model."""
        taught = OpenAIProvider(_config_for("gpt-6"))
        session, _ = _sequenced_session(
            [_error(400, _rejects_param(LEGACY, MODERN)), _ok()]
        )
        with patch("aiohttp.ClientSession", return_value=session):
            await taught.generate("hi", max_tokens=8)
        assert taught._token_param_learned == {"gpt-6": MODERN}

        untaught = OpenAIProvider(_config_for("gpt-6"))
        assert untaught._token_param_learned == {}
        session2, sent2 = _sequenced_session([_ok()])
        with patch("aiohttp.ClientSession", return_value=session2):
            await untaught.generate("hi", max_tokens=8)
        assert LEGACY in sent2[0]


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestTokenParamCorrectionDoesNotOverreach:
    """``REQUEST_REJECTED`` is far broader than this one failure, and the retry
    ladder owns everything transient. Each test below is a failure that DOES
    reach the provider and must NOT buy a parameter swap — the observable is
    always "exactly one request was sent, and the error propagated"."""

    async def _assert_not_retried(self, provider, spec, expected_category):
        session, sent = _sequenced_session([spec])
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(LLMException) as excinfo:
                await provider.generate("hi", max_tokens=32)
        assert len(sent) == 1, "no re-send may be issued"
        assert excinfo.value.category is expected_category
        return excinfo.value

    async def test_rejection_naming_a_different_parameter(self):
        """Same code, same category — a swap cannot fix ``reasoning_effort``."""
        await self._assert_not_retried(
            OpenAIProvider(_config_for("gpt-4o")),
            _error(400, _rejects_param("reasoning_effort", "/v1/responses")),
            LLMErrorCategory.REQUEST_REJECTED,
        )

    async def test_rejection_naming_only_the_spelling_we_did_not_send(self):
        """We sent ``max_tokens``; the endpoint is refusing ``max_completion_tokens``.

        A containment test on the RECOMMENDED name rather than the SENT one
        would flip us onto the very parameter being rejected. (The two
        spellings are not substrings of each other, which is what lets the
        check be a plain containment test at all.)
        """
        provider = OpenAIProvider(_config_for("gpt-4o"))
        assert provider._uses_completion_tokens_param("gpt-4o") is False
        await self._assert_not_retried(
            provider,
            _error(
                400,
                {
                    "error": {
                        "message": "Unsupported parameter: 'max_completion_tokens'.",
                        "code": "unsupported_parameter",
                    }
                },
            ),
            LLMErrorCategory.REQUEST_REJECTED,
        )

    async def test_transient_failure_that_happens_to_name_the_parameter(self):
        """Category gates first: the ladder, not a swap, owns a 429."""
        await self._assert_not_retried(
            OpenAIProvider(_config_for("gpt-4o")),
            _error(
                429,
                {
                    "error": {
                        "message": "Rate limit reached; reduce max_tokens or retry.",
                        "code": "rate_limit_exceeded",
                    }
                },
            ),
            LLMErrorCategory.TRANSIENT,
        )

    async def test_context_overflow_that_names_the_parameter(self):
        """A 400 is not enough: an overflow needs a smaller prompt, not a rename."""
        await self._assert_not_retried(
            OpenAIProvider(_config_for("gpt-4o")),
            _error(
                400,
                {
                    "error": {
                        "message": (
                            "This model's maximum context length is 128000 tokens, "
                            "however you requested max_tokens of 200000."
                        ),
                        "code": "context_length_exceeded",
                    }
                },
            ),
            LLMErrorCategory.CONTEXT_OVERFLOW,
        )

    async def test_a_failed_correction_surfaces_and_teaches_nothing(self):
        """The swap is a hypothesis; a second failure refutes it.

        The SECOND error is raised, chained from the first, because its
        category is what the ladder above must act on — collapsing a transient
        second failure into the first would report it as permanent. Nothing is
        learned, so the next call starts from the static expectation again.
        """
        provider = OpenAIProvider(_config_for("gpt-6"))
        session, sent = _sequenced_session(
            [
                _error(400, _rejects_param(LEGACY, MODERN)),
                _error(
                    503, {"error": {"message": "overloaded", "code": "server_error"}}
                ),
            ]
        )

        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(LLMException) as excinfo:
                await provider.generate("hi", max_tokens=32)

        assert len(sent) == 2
        raised = excinfo.value
        assert raised.status_code == 503
        assert raised.category is LLMErrorCategory.TRANSIENT
        assert raised.retryable is True, "the ladder must still see a retryable failure"
        assert isinstance(raised.__cause__, LLMException)
        assert raised.__cause__.status_code == 400
        assert provider._token_param_learned == {}


@pytest.mark.unit
@pytest.mark.llm
@pytest.mark.asyncio
class TestOpenRouterInheritsTheCorrection:
    """OpenRouter subclasses ``OpenAIProvider`` and routes to other vendors'
    models. Its ``_uses_completion_tokens_param`` override is an opening guess
    like any other, and the gateway's normalization is a claim about the
    gateway, not a guarantee."""

    async def test_routed_model_rejecting_the_legacy_name_is_corrected(self):
        provider = OpenRouterProvider(
            _config_for(
                "openai/gpt-9",
                name="openrouter",
                base_url="https://openrouter.ai/api/v1",
            )
        )
        session, sent = _sequenced_session(
            [_error(400, _rejects_param(LEGACY, MODERN)), _ok("routed")]
        )

        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi", max_tokens=128)

        assert result.content == "routed"
        assert LEGACY in sent[0] and MODERN not in sent[0]
        assert sent[1][MODERN] == 128 and LEGACY not in sent[1]
        assert provider._token_param_learned == {"openai/gpt-9": MODERN}

    async def test_the_gateway_default_is_unchanged(self):
        """The override still says "start with the legacy name" — the
        correction is what makes that safe, not a reason to change it."""
        assert OpenRouterProvider._uses_completion_tokens_param("openai/gpt-5") is False
        assert OpenRouterProvider._uses_completion_tokens_param("anything") is False


@pytest.mark.unit
def test_every_completion_tokens_family_is_also_declared_STRICT():
    """The invariant the two lists carry in a comment, enforced.

    ``_COMPLETION_TOKENS_MODEL_FAMILIES`` and ``_STRICT_MODEL_INDICATORS`` are
    separate axes that must nevertheless overlap in one direction: a reasoning
    family missing from the STRICT list routes structured extraction through
    FUNCTION_CALLING instead of ``response_format``, which moves the call out
    of reach of the reasoning cap that is scoped to ``response_format`` and
    starves the schema body (#625).

    The lists HAVE diverged: ``o4`` was added to the token-param tuple and not
    to the STRICT one, and was reconciled months later by an unrelated change
    (#641) — the comment was there the whole time. A comment is not a
    mechanism; this is.
    """
    for family in OpenAIProvider._COMPLETION_TOKENS_MODEL_FAMILIES:
        for model in (family, f"{family}-mini", f"{family}.1-preview"):
            assert (
                OpenAIProvider._capability_for_model_name(model)
                is StructuredOutputCapability.STRICT
            ), f"{model} requires max_completion_tokens but is not declared STRICT"
