"""The output-truncation recovery ladder actually runs (#513).

Truncation means the prompt fit but the ANSWER did not. The remedies, in order:

1. raise ``max_tokens`` and retry — the targeted fix, and the retry must reach
   the provider rather than be served the truncated body the first attempt
   left in the response cache;
2. once the ceiling is reached, hand off to the ``#662`` minimal-prompt degrade
   — the only remaining lever is shrinking the question so the answer has room.

Every previous attempt at (1) was tested one level too high: at the classifier,
asserting booleans about error strings. That is exactly where this bug hid.
Both real truncation sites word the failure in vocabulary the classifier was
never checking — the provider says ``finishReason=MAX_TOKENS`` from inside
``generate()`` (so the parse block never sees it), and CPython's JSON decoder
says ``Expecting ',' delimiter`` or ``Unterminated string starting at`` (never
``truncated``, never ``EOF while parsing``). A green classifier test sat above a
ladder that never moved a single rung.

So these drive ``_generate_structured_output_inner`` for real and assert on the
``max_tokens`` and ``bypass_cache`` that reach the provider on each attempt.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import BaseModel

from faultmaven.core.investigation.llm_error_handler import (
    RECOVERY_REASON_OUTPUT_TRUNCATION,
    LLMErrorHandler,
    OutputTruncationError,
    RetryConfig,
    classify_token_limit_reason,
    is_output_truncation_error,
    is_truncated_json_error,
)
from faultmaven.core.investigation.milestone_engine import (
    STRUCTURED_OUTPUT_MAX_TOKENS,
    STRUCTURED_OUTPUT_MAX_TOKENS_CEILING,
    MilestoneEngineError,
    _is_context_length_error,
)
from faultmaven.exceptions import TOKEN_LIMIT, LLMException
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    StructuredOutputMode,
    StructuredOutputStrategy,
)

pytestmark = pytest.mark.unit


class _Schema(BaseModel):
    agent_response: str


# A body cut off mid-string, and the same answer complete. The truncated one is
# what a real generation cap produces: valid JSON right up to where it stops.
TRUNCATED = '{"agent_response": "the kubelet on node-3 is out of dis'
COMPLETE = '{"agent_response": "the kubelet on node-3 is out of disk"}'

# Complete but malformed — the model emitted a bare token instead of a value.
# A bigger cap cannot fix this, so it must NOT consume the truncation ladder.
MALFORMED = '{"agent_response": tru}'

# What Gemini raises from inside generate() when a structured request hits the
# output cap: there is no body to parse, so the parse block never runs.
GEMINI_TRUNCATION = LLMException(
    "Response truncated due to token limit (finishReason=MAX_TOKENS). "
    "Response length: 8000 chars. Increase max_tokens parameter or simplify prompt.",
    retryable=True,
)


def _make_engine():
    """An engine whose retry loop is real but spends no wall-clock on backoff.

    A real ``LLMErrorHandler`` on purpose: the whole failure was in how the
    handler classified what the engine raised, so a mocked handler would test
    the two halves separately and prove nothing about the seam between them.
    """
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    repo = MagicMock()
    repo.save = AsyncMock()
    engine = MilestoneEngine(
        llm_provider=AsyncMock(),
        repository=repo,
        investigation_tools=MagicMock(),
    )
    engine.llm_error_handler = LLMErrorHandler(
        RetryConfig(max_retries=3, base_delay_seconds=0.0, max_delay_seconds=0.0)
    )
    engine.llm_provider.get_structured_output_strategy = MagicMock(
        return_value=StructuredOutputStrategy(
            capability=StructuredOutputCapability.STRICT,
            mode=StructuredOutputMode.JSON_SCHEMA_STRICT,
            include_schema_in_prompt=False,
            response_format={"type": "json_object"},
        )
    )
    return engine


def _attempts(generate_mock):
    """The kwargs of each call that actually reached the provider."""
    return [call.kwargs for call in generate_mock.await_args_list]


# ---------------------------------------------------------------------------
# is_truncated_json_error — truncation is a position, not a phrase
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '{"a": "abcdef',  # cut inside a string
        '{"a": 1',  # cut after a value
        '{"a": [1,2',  # cut inside an array
        '{"a": 1, "b"',  # cut after a key
        '{"a": {"b": 1',  # cut inside a nested object
        "",  # nothing came back at all
    ],
    ids=["in-string", "after-value", "in-array", "after-key", "nested", "empty"],
)
def test_a_body_that_ran_out_is_truncation(body):
    """Swept over the shapes a cut lands in, not one example. The decoder's
    message differs for each — the shared property is that it stopped at the
    end of the input."""
    with pytest.raises(json.JSONDecodeError) as exc_info:
        json.loads(body, strict=False)
    assert is_truncated_json_error(exc_info.value, body) is True


@pytest.mark.parametrize(
    "body",
    [
        '{"a": 1,, "b": 2}',  # stray comma mid-document
        "Sure! Here is the JSON you asked for.",  # prose, no JSON at all
        '{"a": tru, "b": 2}',  # bad literal mid-document
        '{"a": 1,}',  # trailing comma
    ],
    ids=["stray-comma", "prose", "bad-literal", "trailing-comma"],
)
def test_a_body_malformed_in_the_middle_is_not_truncation(body):
    """The negative half, and the reason position beats phrasing: several of
    these produce the *same* decoder messages as a truncated body. Only where
    the decoder stopped tells them apart, and calling them truncation would burn
    the turn's attempts raising a cap that was never the problem."""
    with pytest.raises(json.JSONDecodeError) as exc_info:
        json.loads(body, strict=False)
    assert is_truncated_json_error(exc_info.value, body) is False


def test_a_schema_violation_is_not_truncation():
    """By the time Pydantic validates, the body has been re-serialized from a
    successfully parsed object, so a failure there is about the schema and must
    fall through untouched."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        _Schema.model_validate_json('{"agent_response": 17}')
    assert is_truncated_json_error(exc_info.value, '{"agent_response": 17}') is False


def test_provider_truncation_is_recognized_from_its_own_wording():
    """The one site where wording is the only evidence there is."""
    assert is_output_truncation_error(GEMINI_TRUNCATION) is True


@pytest.mark.asyncio
async def test_an_overflow_wearing_truncation_wording_still_compresses():
    """A gateway that says "input truncated: context length exceeded" is
    reporting that the PROMPT did not fit. Raising the generation cap cannot
    help, so it must not be diverted into the truncation ladder — it belongs on
    the COMPRESS_MEMORY path on the first attempt, not two wasted calls later.
    """
    both = LLMException(
        "Request rejected: input truncated, context length exceeded", retryable=True
    )
    assert is_output_truncation_error(both) is False

    engine = _make_engine()
    generate = AsyncMock(side_effect=[both, COMPLETE])
    engine.llm_provider.generate = generate

    with pytest.raises(MilestoneEngineError) as exc_info:
        await engine._generate_structured_output_inner(
            prompt="why is node-3 NotReady?", schema_model=_Schema
        )

    assert exc_info.value.error_code == TOKEN_LIMIT
    assert generate.await_count == 1, "must not spend an attempt raising the cap"


def test_unsupported_parameter_error_is_not_truncation():
    """A request-shape 400 that merely names a token parameter is a config
    error. Reading it as truncation would raise the cap on a call that will be
    rejected identically at any cap."""
    assert (
        is_output_truncation_error(
            Exception(
                "Unsupported parameter: 'max_tokens' is not supported with this model."
            )
        )
        is False
    )


# ---------------------------------------------------------------------------
# The ladder, rung 1: raise the cap and get the retry past the cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_truncation_raises_the_cap_and_bypasses_the_cache():
    """The headline fix. The first attempt comes back cut off; the second must
    reach the provider with a doubled cap AND with the cache bypassed.

    Both halves matter and they fail independently. Without the raised cap the
    retry is a verbatim repeat that truncates identically. Without the bypass it
    never reaches the provider at all: ``max_tokens`` is not part of the cache
    key, so the truncated body the first attempt stored answers the second one
    instantly and the raised cap is never exercised.
    """
    engine = _make_engine()
    generate = AsyncMock(side_effect=[TRUNCATED, COMPLETE])
    engine.llm_provider.generate = generate

    result = await engine._generate_structured_output_inner(
        prompt="why is node-3 NotReady?", schema_model=_Schema
    )

    assert result.agent_response == "the kubelet on node-3 is out of disk"
    first, second = _attempts(generate)
    assert first["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS
    assert first["bypass_cache"] is False
    assert second["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS * 2
    assert second["bypass_cache"] is True


@pytest.mark.asyncio
async def test_provider_reported_truncation_raises_the_cap_too():
    """The site the fix was missing entirely. Gemini raises from inside
    ``generate()``, so the failure never reaches the parse block where the cap
    used to be raised — it was classified as an ordinary retryable error and
    repeated at the same size until the attempts ran out."""
    engine = _make_engine()
    generate = AsyncMock(side_effect=[GEMINI_TRUNCATION, COMPLETE])
    engine.llm_provider.generate = generate

    result = await engine._generate_structured_output_inner(
        prompt="why is node-3 NotReady?", schema_model=_Schema
    )

    assert result.agent_response == "the kubelet on node-3 is out of disk"
    first, second = _attempts(generate)
    assert first["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS
    assert second["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS * 2
    assert second["bypass_cache"] is True


@pytest.mark.asyncio
async def test_malformed_json_does_not_spend_the_truncation_ladder():
    """The guard against an over-broad fix. A complete-but-malformed body must
    not raise the cap: retrying a bigger call cannot make the model emit valid
    JSON, and every rung spent here is a rung a real truncation cannot use."""
    engine = _make_engine()
    generate = AsyncMock(side_effect=[MALFORMED, COMPLETE])
    engine.llm_provider.generate = generate

    with pytest.raises(MilestoneEngineError):
        await engine._generate_structured_output_inner(
            prompt="why is node-3 NotReady?", schema_model=_Schema
        )

    assert generate.await_count == 1, "malformed JSON must not trigger a retry"
    assert _attempts(generate)[0]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS


# ---------------------------------------------------------------------------
# The ladder, rung 2: at the ceiling, hand off to the degrade
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_the_ceiling_it_stops_climbing_and_asks_for_the_degrade():
    """Raising the cap past its ceiling is a no-op, so continuing to retry
    spends the turn's remaining attempts on identical calls and then fails.
    At the ceiling the failure must convert to TOKEN_LIMIT, which is what the
    minimal-prompt recovery keys on — the NO-COLLAPSE guarantee (#662)."""
    engine = _make_engine()
    generate = AsyncMock(side_effect=[TRUNCATED, TRUNCATED, COMPLETE])
    engine.llm_provider.generate = generate

    with pytest.raises(MilestoneEngineError) as exc_info:
        await engine._generate_structured_output_inner(
            prompt="why is node-3 NotReady?", schema_model=_Schema
        )

    raised = exc_info.value
    assert raised.error_code == TOKEN_LIMIT
    # Two attempts, not the full retry budget: the second hit the ceiling and
    # handed off rather than climbing.
    assert generate.await_count == 2
    assert _attempts(generate)[1]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS_CEILING
    # And it must reach the recovery, attributed to truncation rather than
    # silently counted as an input overflow.
    assert _is_context_length_error(raised) is True
    assert classify_token_limit_reason(raised) == RECOVERY_REASON_OUTPUT_TRUNCATION


@pytest.mark.asyncio
async def test_ceiling_truncation_degrades_to_the_minimal_prompt():
    """End to end, with nothing hand-built: real truncation off the provider,
    through the real classifier, into the real recovery. This closes the loop
    the classifier-level tests could not — the degrade fires because the engine
    produced TOKEN_LIMIT, not because a test constructed one."""
    engine = _make_engine()
    case = MagicMock()
    case.case_id = "case_test"
    generate = AsyncMock(side_effect=[TRUNCATED, TRUNCATED, COMPLETE])
    engine.llm_provider.generate = generate

    with patch(
        "faultmaven.core.investigation.prompts.templates.get_fallback_prompt_for_case",
        return_value="MINIMAL FALLBACK PROMPT",
    ):
        result = await engine._generate_structured_output(
            prompt="why is node-3 NotReady?",
            schema_model=_Schema,
            case=case,
            user_message="node-3 went NotReady an hour ago",
        )

    assert result.agent_response == "the kubelet on node-3 is out of disk"
    # Two truncated attempts on the full prompt, then one on the minimal prompt.
    assert generate.await_count == 3
    assert "MINIMAL FALLBACK PROMPT" in _attempts(generate)[2]["prompt"]


@pytest.mark.asyncio
async def test_the_degrade_is_metered_as_truncation_not_overflow():
    """The metric label this fix makes reachable again. While truncation was
    classified as a plain retryable error it never reached the recovery, so the
    ``output_truncation`` series read a permanent zero — indistinguishable from
    'this never happens', which is the opposite of what the counter is for."""
    engine = _make_engine()
    case = MagicMock()
    case.case_id = "case_test"
    engine.llm_provider.generate = AsyncMock(
        side_effect=[TRUNCATED, TRUNCATED, COMPLETE]
    )
    metric = MagicMock()

    with (
        patch(
            "faultmaven.core.investigation.milestone_engine."
            "prompt_context_recovery_total",
            metric,
        ),
        patch(
            "faultmaven.core.investigation.prompts.templates."
            "get_fallback_prompt_for_case",
            return_value="MINIMAL FALLBACK PROMPT",
        ),
    ):
        await engine._generate_structured_output(
            prompt="why is node-3 NotReady?",
            schema_model=_Schema,
            case=case,
            user_message="node-3 went NotReady an hour ago",
        )

    metric.labels.assert_called_once_with(reason=RECOVERY_REASON_OUTPUT_TRUNCATION)
    metric.labels.return_value.inc.assert_called_once()


# ---------------------------------------------------------------------------
# The typed signal routes on type, never on the message it carries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncation_wording_is_not_re_read_by_the_string_classifiers():
    """``OutputTruncationError`` carries the provider's own text, which mentions
    the model and the token cap. Dispatching on type keeps the phrase-matching
    classifiers below from re-reading it and escalating a recoverable cut as,
    say, a model-not-found configuration failure."""
    handler = LLMErrorHandler(
        RetryConfig(max_retries=3, base_delay_seconds=0.0, max_delay_seconds=0.0)
    )
    hostile = OutputTruncationError(
        "Response truncated at max_tokens=8000: model not found, 404, unauthorized",
        cap_reached=False,
    )

    result = await handler.handle_error(hostile, retry_count=0)

    from faultmaven.core.investigation.llm_error_handler import ErrorAction

    assert result.action is ErrorAction.RETRY
    assert result.error_code is None


@pytest.mark.asyncio
async def test_a_truncation_retry_honours_the_shared_attempt_ceiling():
    """The truncation ladder must not get its own retry budget on top of the
    handler's. With the cap ceiling far away, repeated truncation still stops at
    ``max_retries`` rather than climbing forever."""
    handler = LLMErrorHandler(
        RetryConfig(max_retries=2, base_delay_seconds=0.0, max_delay_seconds=0.0)
    )
    err = OutputTruncationError("Response truncated at max_tokens=8000", False)

    from faultmaven.core.investigation.llm_error_handler import ErrorAction

    assert (await handler.handle_error(err, retry_count=1)).action is ErrorAction.RETRY
    exhausted = await handler.handle_error(err, retry_count=2)
    assert exhausted.action is ErrorAction.FAIL
    assert exhausted.error_code == "RETRY_EXHAUSTED"


# ---------------------------------------------------------------------------
# The third trigger: the provider simply says so (#1094)
# ---------------------------------------------------------------------------


def _decode_error(body: str) -> json.JSONDecodeError:
    """The decoder's own error for *body*, rather than a hand-built stand-in."""
    try:
        json.loads(body, strict=False)
    except json.JSONDecodeError as exc:
        return exc
    raise AssertionError(f"{body!r} unexpectedly parsed")


def _llm_response(content: str, stop_reason):
    from faultmaven.infrastructure.llm.providers import LLMResponse

    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="openai",
        model="gpt-5.4-mini",
        tokens_used=8000,
        response_time_ms=10,
        stop_reason=stop_reason,
    )


@pytest.mark.asyncio
async def test_a_body_that_parses_is_kept_even_when_the_provider_reports_a_cut():
    """A cut only matters if it cost us the answer.

    On prompt-only / BEST_EFFORT modes the answer is not the whole body: those
    models routinely emit a complete ```json block and then keep talking, which
    is why the extractor handles "Some text\n```json\n{...}\n```\nMore text".
    When the cap lands in that trailing prose the JSON is whole and validates.

    Raising on the stop reason alone would throw that good response away, spend
    a second full-size generation, and — if the second attempt also trailed off
    — hand the turn to the minimal-prompt degrade, losing the prompt context
    too. So the stop reason is consulted only once something has failed.
    """
    from faultmaven.infrastructure.llm.providers import StopReason

    trailing_prose_cut = (
        "Here is the response you asked for:\n"
        "```json\n" + COMPLETE + "\n```\n"
        "I should note that this conclusion depends on the kubelet log excerpt "
        "shared earlier, and that disk pressure on node-3 would also expl"
    )

    engine = _make_engine()
    generate = AsyncMock(
        return_value=_llm_response(trailing_prose_cut, StopReason.MAX_TOKENS)
    )
    engine.llm_provider.generate = generate

    result = await engine._generate_structured_output_inner(
        prompt="why is node-3 NotReady?", schema_model=_Schema
    )

    assert result.agent_response == "the kubelet on node-3 is out of disk"
    assert len(_attempts(generate)) == 1, "a usable response must not be retried"


@pytest.mark.asyncio
async def test_the_stop_reason_rescues_a_cut_the_positional_test_declines():
    """What the typed signal is actually worth on this path.

    ``is_truncated_json_error`` is positional, and deliberately says False for a
    body malformed in the MIDDLE — on its own that is right, since a bigger cap
    cannot fix a stray token. But a body that is BOTH malformed and cut is a
    real shape, and there the provider's report is the only evidence that more
    room is the remedy. Without it the turn spends its attempts repeating the
    same failing call at the same size.
    """
    from faultmaven.infrastructure.llm.providers import StopReason

    # Malformed mid-document (bad literal), so the positional test declines it.
    assert (
        is_truncated_json_error(
            _decode_error(MALFORMED),
            MALFORMED,
        )
        is False
    )

    engine = _make_engine()
    generate = AsyncMock(
        side_effect=[
            _llm_response(MALFORMED, StopReason.MAX_TOKENS),
            _llm_response(COMPLETE, StopReason.STOP),
        ]
    )
    engine.llm_provider.generate = generate

    result = await engine._generate_structured_output_inner(
        prompt="why is node-3 NotReady?", schema_model=_Schema
    )

    assert result.agent_response == "the kubelet on node-3 is out of disk"
    first, second = _attempts(generate)
    assert first["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS
    assert second["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS * 2
    assert second["bypass_cache"] is True


@pytest.mark.asyncio
async def test_a_malformed_body_without_a_reported_cut_still_does_not_ladder():
    """Negative control for the pair above — the guard is the stop reason.

    The same malformed body with no provider report keeps its existing
    behaviour exactly: one attempt, no cap raise, hard failure. That contrast
    is the whole point. Without the report there is no evidence a bigger cap
    would help, and spending a rung on every stray token is a rung a real
    truncation cannot use.
    """
    from faultmaven.infrastructure.llm.providers import StopReason

    engine = _make_engine()
    generate = AsyncMock(
        side_effect=[
            _llm_response(MALFORMED, StopReason.STOP),
            _llm_response(COMPLETE, StopReason.STOP),
        ]
    )
    engine.llm_provider.generate = generate

    with pytest.raises(MilestoneEngineError):
        await engine._generate_structured_output_inner(
            prompt="why is node-3 NotReady?", schema_model=_Schema
        )

    assert generate.await_count == 1
    assert _attempts(generate)[0]["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS


@pytest.mark.asyncio
async def test_a_complete_response_is_not_diverted_into_the_ladder():
    """Negative control. Only MAX_TOKENS may spend the turn's attempts."""
    from faultmaven.infrastructure.llm.providers import StopReason

    engine = _make_engine()
    generate = AsyncMock(return_value=_llm_response(COMPLETE, StopReason.STOP))
    engine.llm_provider.generate = generate

    result = await engine._generate_structured_output_inner(
        prompt="why is node-3 NotReady?", schema_model=_Schema
    )

    assert result.agent_response == "the kubelet on node-3 is out of disk"
    assert len(_attempts(generate)) == 1


@pytest.mark.asyncio
async def test_an_unknown_stop_reason_is_not_treated_as_a_cut():
    """A provider that reports nothing is not evidence of truncation.

    UNKNOWN is the default and the honest state for HuggingFace and for any
    parse gap. Treating it as a cut would put every such call through the
    ladder and burn the turn's attempts on responses that were never truncated.
    """
    from faultmaven.infrastructure.llm.providers import StopReason

    engine = _make_engine()
    generate = AsyncMock(return_value=_llm_response(COMPLETE, StopReason.UNKNOWN))
    engine.llm_provider.generate = generate

    result = await engine._generate_structured_output_inner(
        prompt="why is node-3 NotReady?", schema_model=_Schema
    )

    assert result.agent_response == "the kubelet on node-3 is out of disk"
    assert len(_attempts(generate)) == 1
