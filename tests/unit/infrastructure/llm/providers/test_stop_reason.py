"""Every provider must report WHY generation stopped.

A response cut at the output cap arrives as an ordinary success — HTTP 200, a
body, a token count — and used to be indistinguishable from one that finished.
The only thing that ever noticed was JSON parsing failing on the malformed
remains, which by construction can never catch a cut *prose* answer: half a
sentence is still valid text. So truncated KB answers, runbooks and analyses
were returned, cited and persisted as complete (#1094).

These tests pin the signal at its source: each provider's real ``generate()``
with the HTTP layer mocked, asserting the normalised ``stop_reason`` for a
length-stop, a natural stop, and the no-signal case. The last one is not
padding — ``UNKNOWN`` collapsing into "finished fine" is what would make every
``is_truncated`` check downstream fail open.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.infrastructure.llm.providers.anthropic import AnthropicProvider
from faultmaven.infrastructure.llm.providers.base import (
    LLMResponse,
    ProviderConfig,
    StopReason,
    normalize_stop_reason,
)
from faultmaven.infrastructure.llm.providers.cohere_provider import CohereProvider
from faultmaven.infrastructure.llm.providers.fireworks_provider import FireworksProvider
from faultmaven.infrastructure.llm.providers.gemini import GeminiProvider
from faultmaven.infrastructure.llm.providers.groq_provider import GroqProvider
from faultmaven.infrastructure.llm.providers.local_provider import LocalProvider
from faultmaven.infrastructure.llm.providers.openai_provider import OpenAIProvider

pytestmark = [pytest.mark.unit, pytest.mark.llm]


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


def _config(name: str, base_url: str, model: str) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        api_key="test-key",
        base_url=base_url,
        models=[model],
        default_model=model,
        timeout=30,
        confidence_score=0.9,
    )


# --------------------------------------------------------------------------
# The normaliser
# --------------------------------------------------------------------------


class TestNormalizeStopReason:
    """Nine APIs, nine vocabularies, one enum."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Output cap — every spelling in the fleet
            ("length", StopReason.MAX_TOKENS),  # OpenAI-family, Ollama
            ("max_tokens", StopReason.MAX_TOKENS),  # Anthropic, Cohere
            ("MAX_TOKENS", StopReason.MAX_TOKENS),  # Gemini (upper case)
            ("stopped_limit", StopReason.MAX_TOKENS),  # llama.cpp
            # Natural completion
            ("stop", StopReason.STOP),
            ("end_turn", StopReason.STOP),  # Anthropic
            ("COMPLETE", StopReason.STOP),  # Cohere v2
            # Safety — must NOT read as truncation, the recovery is opposite
            ("content_filter", StopReason.CONTENT_FILTER),
            ("SAFETY", StopReason.CONTENT_FILTER),
            # Tool handoff
            ("tool_calls", StopReason.TOOL_CALLS),
            ("tool_use", StopReason.TOOL_CALLS),
        ],
    )
    def test_known_vocabularies_map(self, raw, expected):
        assert normalize_stop_reason(raw) is expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "some_future_reason", 42])
    def test_unrecognised_is_unknown_never_stop(self, raw):
        """Silence must never be read as "it finished".

        Guessing STOP from a missing or unfamiliar value is the failure this
        enum exists to prevent: it would let a new provider, or a provider that
        adds a reason we have not seen, report a cut body as a complete one.
        """
        assert normalize_stop_reason(raw) is StopReason.UNKNOWN


class TestIsTruncated:
    def test_only_max_tokens_is_truncation(self):
        for reason in StopReason:
            response = LLMResponse(
                content="x",
                confidence=1.0,
                provider="p",
                model="m",
                tokens_used=1,
                response_time_ms=1,
                stop_reason=reason,
            )
            assert response.is_truncated is (reason is StopReason.MAX_TOKENS)

    def test_unknown_stays_distinguishable_from_stop(self):
        """`is_truncated` is False for both, but the field still tells them apart.

        Logging and metrics depend on this: a provider sitting at 100% UNKNOWN
        is a blind spot to close, not a clean bill of health.
        """
        default = LLMResponse(
            content="x",
            confidence=1.0,
            provider="p",
            model="m",
            tokens_used=1,
            response_time_ms=1,
        )
        assert default.stop_reason is StopReason.UNKNOWN
        assert default.is_truncated is False


# --------------------------------------------------------------------------
# OpenAI-compatible family: OpenAI (and therefore OpenRouter), Fireworks, Groq
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOpenAICompatibleFamily:
    @staticmethod
    def _body(finish_reason):
        choice = {"message": {"content": "partial answer"}}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        return {"choices": [choice], "usage": {"total_tokens": 10}}

    @pytest.mark.parametrize(
        "provider_factory",
        [
            lambda: OpenAIProvider(
                _config("openai", "https://api.openai.com/v1", "gpt-4o")
            ),
            lambda: FireworksProvider(
                _config("fireworks", "https://api.fireworks.ai/inference/v1", "m")
            ),
            lambda: GroqProvider(
                _config("groq", "https://api.groq.com/openai/v1", "llama-3.3-70b")
            ),
        ],
        ids=["openai", "fireworks", "groq"],
    )
    @pytest.mark.parametrize(
        "finish_reason,expected",
        [
            ("length", StopReason.MAX_TOKENS),
            ("stop", StopReason.STOP),
            (None, StopReason.UNKNOWN),
        ],
    )
    async def test_finish_reason_surfaces(
        self, provider_factory, finish_reason, expected
    ):
        provider = provider_factory()
        session = _mock_aiohttp_session(self._body(finish_reason))
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi")

        assert result.stop_reason is expected
        assert result.is_truncated is (expected is StopReason.MAX_TOKENS)
        # The cut body is still returned — refusing it would turn every long
        # prose answer into a hard failure.
        assert result.content == "partial answer"

    async def test_openrouter_inherits_the_openai_parse(self):
        from faultmaven.infrastructure.llm.providers.openrouter_provider import (
            OpenRouterProvider,
        )

        provider = OpenRouterProvider(
            _config("openrouter", "https://openrouter.ai/api/v1", "anthropic/claude")
        )
        session = _mock_aiohttp_session(self._body("length"))
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi")

        assert result.is_truncated is True


# --------------------------------------------------------------------------
# Providers with their own vocabulary
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAnthropic:
    @pytest.mark.parametrize(
        "stop_reason,expected",
        [
            ("max_tokens", StopReason.MAX_TOKENS),
            ("end_turn", StopReason.STOP),
            ("tool_use", StopReason.TOOL_CALLS),
            (None, StopReason.UNKNOWN),
        ],
    )
    async def test_stop_reason_surfaces(self, stop_reason, expected):
        provider = AnthropicProvider(
            _config("anthropic", "https://api.anthropic.com/v1", "claude-sonnet-4-6")
        )
        body = {
            "content": [{"type": "text", "text": "partial"}],
            "usage": {"input_tokens": 5, "output_tokens": 5},
        }
        if stop_reason is not None:
            body["stop_reason"] = stop_reason
        session = _mock_aiohttp_session(body)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi")

        assert result.stop_reason is expected


@pytest.mark.asyncio
class TestCohere:
    async def test_top_level_finish_reason(self):
        """Cohere v2 reports it beside `message`, not inside it."""
        provider = CohereProvider(_config("cohere", "https://api.cohere.com/v2", "cmd"))
        body = {
            "message": {"content": "partial"},
            "finish_reason": "MAX_TOKENS",
            "usage": {"tokens": {"input_tokens": 3, "output_tokens": 4}},
        }
        session = _mock_aiohttp_session(body)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi")

        assert result.stop_reason is StopReason.MAX_TOKENS


@pytest.mark.asyncio
class TestGemini:
    """Gemini always PARSED finishReason; what it never did was hand it on."""

    async def test_prose_truncation_returns_partial_content_flagged(self):
        provider = GeminiProvider(
            _config("gemini", "https://generativelanguage.googleapis.com/v1beta", "g")
        )
        body = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "partial answer"}]},
                    "finishReason": "MAX_TOKENS",
                }
            ]
        }
        session = _mock_aiohttp_session(body)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi")

        assert result.stop_reason is StopReason.MAX_TOKENS
        assert result.content == "partial answer"

    async def test_no_sentinel_string_is_written_into_content(self):
        """The placeholder channel is retired.

        This provider used to substitute "[Response truncated due to token
        limit]" into `content`, which the case-title route then string-matched
        back out. One layer inventing a sentinel and another blacklisting it
        existed only because there was no field to carry the fact. There is now.
        """
        provider = GeminiProvider(
            _config("gemini", "https://generativelanguage.googleapis.com/v1beta", "g")
        )
        body = {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}
        session = _mock_aiohttp_session(body)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi")

        assert "[" not in result.content
        assert result.content == ""
        assert result.stop_reason is StopReason.CONTENT_FILTER
        # A safety block is NOT truncation: retrying it with a bigger budget
        # just buys the same refusal again.
        assert result.is_truncated is False

    async def test_structured_truncation_still_raises_with_the_matched_wording(self):
        """The engine's ladder keys on this message; it must not drift.

        ``is_output_truncation_error`` matches the literal
        "finishreason=max_tokens". Rewording the raise without migrating that
        classifier kills the provider-raised trigger silently.
        """
        from faultmaven.core.investigation.llm_error_handler import (
            is_output_truncation_error,
        )
        from faultmaven.exceptions import LLMException

        provider = GeminiProvider(
            _config("gemini", "https://generativelanguage.googleapis.com/v1beta", "g")
        )
        body = {
            "candidates": [
                {
                    "content": {"parts": [{"text": '{"partial": '}]},
                    "finishReason": "MAX_TOKENS",
                }
            ]
        }
        session = _mock_aiohttp_session(body)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(LLMException) as exc:
                await provider.generate("hi", response_format={"type": "json_object"})

        assert is_output_truncation_error(exc.value)

    async def test_structured_truncation_raises_even_with_empty_content(self):
        """Regression guard for removing the sentinel.

        The raise used to be gated on `content` being truthy, and only ever
        fired because the sentinel string had made empty content truthy first.
        With the sentinel gone, an empty truncated structured response would
        otherwise return as a successful empty answer.
        """
        from faultmaven.exceptions import LLMException

        provider = GeminiProvider(
            _config("gemini", "https://generativelanguage.googleapis.com/v1beta", "g")
        )
        body = {
            "candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]
        }
        session = _mock_aiohttp_session(body)
        with patch("aiohttp.ClientSession", return_value=session):
            with pytest.raises(LLMException):
                await provider.generate("hi", response_format={"type": "json_object"})


@pytest.mark.asyncio
class TestLocalProvider:
    """All three sub-paths: Ollama, OpenAI-compatible, raw llama.cpp."""

    async def test_ollama_done_reason(self):
        provider = LocalProvider(_config("local", "http://ollama:11434", "llama3.2"))
        body = {"response": "partial", "eval_count": 7, "done_reason": "length"}
        session = _mock_aiohttp_session(body)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi")

        assert result.stop_reason is StopReason.MAX_TOKENS

    async def test_openai_compatible_finish_reason(self):
        provider = LocalProvider(_config("local", "http://vllm:8000/v1", "hermes"))
        body = {
            "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
            "usage": {"total_tokens": 9},
        }
        session = _mock_aiohttp_session(body)
        with patch("aiohttp.ClientSession", return_value=session):
            result = await provider.generate("hi")

        assert result.stop_reason is StopReason.MAX_TOKENS

    async def test_llamacpp_stopped_limit(self):
        from faultmaven.exceptions import LLMException

        provider = LocalProvider(_config("local", "http://llamacpp:8080", "local"))
        body = {"content": "partial", "tokens_predicted": 5, "stopped_limit": True}
        session = _mock_aiohttp_session(body)
        # A REAL 404 from that transport is ``LLMException(status_code=404)`` —
        # what ``_call_openai_compatible_api`` actually raises. The stand-in used
        # to be ``Exception("404")``, which only worked while the fallback was
        # selected by ``"404" in str(error)``; that substring also matched the
        # PORT in "Cannot connect to host localhost:4040" and sent an
        # unreachable server down the fallback for a second full timeout
        # (fm#1287 follow-up). The selector now keys on the status, so the
        # stand-in has to carry one.
        with patch.object(
            LocalProvider,
            "_call_openai_compatible_api",
            side_effect=LLMException(
                "Local OpenAI-compatible API error 404: not found", status_code=404
            ),
        ):
            with patch("aiohttp.ClientSession", return_value=session):
                result = await provider.generate("hi")

        assert result.stop_reason is StopReason.MAX_TOKENS

    async def test_llamacpp_prompt_truncation_is_not_output_truncation(self):
        """`truncated` on llama.cpp means the PROMPT was cut, not the output.

        Reading it as an output cut would send the retry-with-a-bigger-cap
        ladder after a failure that a bigger cap makes strictly worse.
        """
        from faultmaven.exceptions import LLMException

        provider = LocalProvider(_config("local", "http://llamacpp:8080", "local"))
        body = {
            "content": "answer",
            "tokens_predicted": 5,
            "truncated": True,
            "stopped_eos": True,
        }
        session = _mock_aiohttp_session(body)
        # A REAL 404 from that transport is ``LLMException(status_code=404)`` —
        # what ``_call_openai_compatible_api`` actually raises. The stand-in used
        # to be ``Exception("404")``, which only worked while the fallback was
        # selected by ``"404" in str(error)``; that substring also matched the
        # PORT in "Cannot connect to host localhost:4040" and sent an
        # unreachable server down the fallback for a second full timeout
        # (fm#1287 follow-up). The selector now keys on the status, so the
        # stand-in has to carry one.
        with patch.object(
            LocalProvider,
            "_call_openai_compatible_api",
            side_effect=LLMException(
                "Local OpenAI-compatible API error 404: not found", status_code=404
            ),
        ):
            with patch("aiohttp.ClientSession", return_value=session):
                result = await provider.generate("hi")

        assert result.stop_reason is StopReason.STOP
        assert result.is_truncated is False
