"""Regression: a recoverable context overflow degrades instead of failing.

The NO-COLLAPSE guarantee (#662): when an input-heavy turn exceeds the model's
context window, the engine must recompile the turn with the minimal fallback
prompt and answer *degraded* — never fail the turn on a recoverable overflow.

The failure mode this pins:

    provider raises "prompt is too long"                     (context overflow)
      → LLMErrorHandler.handle_error → COMPRESS_MEMORY        (not RETRY)
      → with_retry returns (None, ErrorResult TOKEN_LIMIT)
      → _generate_structured_output_inner raises
            MilestoneEngineError(error_code=TOKEN_LIMIT)
      → outer _generate_structured_output must recognize THAT as a
            context-length error and retry once with the minimal prompt.

Before the fix the outer classifier only matched provider *phrasing* in the
message; the engine-raised "Context too large" wording matched nothing, the
recovery was skipped, and the turn hard-failed (500, later 503) instead of
degrading.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngineError,
    _is_context_length_error,
)
from faultmaven.exceptions import TOKEN_LIMIT

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# _is_context_length_error — the classifier that gates the degrade-recovery
# ---------------------------------------------------------------------------


def test_recognizes_engine_token_limit_error_code():
    """The retry-loop path surfaces as a MilestoneEngineError carrying
    error_code == 'TOKEN_LIMIT' whose message no longer contains any provider
    overflow phrase. It must still classify as a context-length error."""
    exc = MilestoneEngineError(
        "Structured output generation failed: Context too large. "
        "Compressing conversation history...",
        error_code=TOKEN_LIMIT,
    )
    assert _is_context_length_error(exc) is True


def test_walks_cause_chain_for_token_limit():
    """When the overflow is chained under an outer wrapper, the classifier must
    walk __cause__ to find the deterministic engine signal."""
    inner = MilestoneEngineError("boom", error_code=TOKEN_LIMIT)
    try:
        raise RuntimeError("wrapper") from inner
    except RuntimeError as outer:
        assert _is_context_length_error(outer) is True


def test_still_matches_raw_provider_phrase():
    """The direct-propagation path (raw provider exception) must keep working —
    the message still carries the provider's overflow wording."""
    exc = Exception("prompt is too long: 250000 tokens > 200000 maximum")
    assert _is_context_length_error(exc) is True


def test_non_overflow_engine_code_is_not_context_length():
    """A different engine failure (e.g. quota) must NOT be mistaken for an
    overflow — that would send it into a futile prompt-reduction retry."""
    exc = MilestoneEngineError("out of credits", error_code="QUOTA_EXHAUSTED")
    assert _is_context_length_error(exc) is False


def test_plain_error_without_overflow_phrase_is_not_context_length():
    exc = Exception("value too long for type character varying(255)")
    assert _is_context_length_error(exc) is False


# ---------------------------------------------------------------------------
# End-to-end: the outer wrapper actually degrades on the engine's TOKEN_LIMIT
# ---------------------------------------------------------------------------


def _make_engine():
    from faultmaven.core.investigation.milestone_engine import MilestoneEngine

    repo = MagicMock()
    repo.save = AsyncMock()
    return MilestoneEngine(
        llm_provider=AsyncMock(),
        repository=repo,
        investigation_tools=MagicMock(),
    )


@pytest.mark.asyncio
async def test_token_limit_triggers_minimal_prompt_retry_and_degrades():
    """The core NO-COLLAPSE regression. The inner generator raises the
    engine's TOKEN_LIMIT overflow on the first (full) attempt; the outer
    wrapper must retry once with the minimal fallback prompt (tools dropped)
    and return that degraded response instead of failing the turn."""
    engine = _make_engine()
    degraded = MagicMock(name="degraded_response")
    case = MagicMock()
    case.case_id = "case_test"

    overflow = MilestoneEngineError(
        "Structured output generation failed: Context too large. "
        "Compressing conversation history...",
        error_code=TOKEN_LIMIT,
    )

    inner = AsyncMock(side_effect=[overflow, degraded])

    with (
        patch.object(engine, "_generate_structured_output_inner", inner),
        patch(
            "faultmaven.core.investigation.prompts.templates."
            "get_fallback_prompt_for_case",
            return_value="MINIMAL FALLBACK PROMPT",
        ),
    ):
        result = await engine._generate_structured_output(
            prompt="A very large prompt " * 10000,
            schema_model=MagicMock(),
            investigation_tools=[{"name": "search_file"}],
            tool_context=MagicMock(),
            force_tool_use=True,
            case=case,
            user_message="what is wrong?",
        )

    assert result is degraded
    assert inner.await_count == 2
    # The recovery call must drop tools to shrink the request.
    _, retry_kwargs = inner.await_args_list[1]
    assert retry_kwargs["investigation_tools"] is None
    assert retry_kwargs["tool_context"] is None
    # The minimal body, plus the degraded-turn notice appended by the recovery
    # (pinned in detail by test_degraded_prompt_tells_the_agent_it_has_no_tools).
    assert inner.await_args_list[1].args[0].startswith("MINIMAL FALLBACK PROMPT")


@pytest.mark.asyncio
async def test_double_overflow_propagates_without_infinite_retry():
    """If even the minimal fallback prompt overflows, the wrapper must NOT loop:
    it retries exactly once, then lets the TOKEN_LIMIT error propagate (the
    caller maps it to a retryable 503 — a genuinely over-limit turn)."""
    engine = _make_engine()
    case = MagicMock()
    case.case_id = "case_test"

    overflow = MilestoneEngineError(
        "Structured output generation failed: Context too large.",
        error_code=TOKEN_LIMIT,
    )
    inner = AsyncMock(side_effect=[overflow, overflow])

    with (
        patch.object(engine, "_generate_structured_output_inner", inner),
        patch(
            "faultmaven.core.investigation.prompts.templates."
            "get_fallback_prompt_for_case",
            return_value="MINIMAL FALLBACK PROMPT",
        ),
    ):
        with pytest.raises(MilestoneEngineError) as exc_info:
            await engine._generate_structured_output(
                prompt="huge",
                schema_model=MagicMock(),
                case=case,
                user_message="what is wrong?",
            )

    assert exc_info.value.error_code == TOKEN_LIMIT
    # Exactly two attempts: the original + one minimal-prompt recovery.
    assert inner.await_count == 2


@pytest.mark.asyncio
async def test_non_overflow_failure_does_not_trigger_fallback_retry():
    """The wrapper must recover ONLY for overflows. A different failure (quota)
    has to propagate on the first attempt — retrying it with a smaller prompt
    burns a second provider call and cannot help. This is the integration-level
    counterpart to the classifier's negative cases: it catches an over-broad
    classifier change at the seam where the retry is actually spent."""
    engine = _make_engine()
    case = MagicMock()
    case.case_id = "case_test"

    quota = MilestoneEngineError("out of credits", error_code="QUOTA_EXHAUSTED")
    inner = AsyncMock(side_effect=quota)

    with patch.object(engine, "_generate_structured_output_inner", inner):
        with pytest.raises(MilestoneEngineError) as exc_info:
            await engine._generate_structured_output(
                prompt="hi",
                schema_model=MagicMock(),
                case=case,
                user_message="what is wrong?",
            )

    assert exc_info.value.error_code == "QUOTA_EXHAUSTED"
    # Exactly one attempt — no minimal-prompt retry for a non-overflow failure.
    assert inner.await_count == 1


@pytest.mark.asyncio
async def test_no_user_message_skips_recovery_and_raises():
    """The recovery needs the user message to build the fallback prompt. Without
    it, the overflow must propagate unchanged (no silent swallow)."""
    engine = _make_engine()
    case = MagicMock()
    case.case_id = "case_test"

    overflow = MilestoneEngineError("boom", error_code=TOKEN_LIMIT)
    inner = AsyncMock(side_effect=overflow)

    with patch.object(engine, "_generate_structured_output_inner", inner):
        with pytest.raises(MilestoneEngineError) as exc_info:
            await engine._generate_structured_output(
                prompt="huge",
                schema_model=MagicMock(),
                case=case,
                user_message=None,
            )

    assert exc_info.value.error_code == TOKEN_LIMIT
    assert inner.await_count == 1


# ---------------------------------------------------------------------------
# HTTP-contract guard: a propagated overflow must NOT leak a provider status
# ---------------------------------------------------------------------------
#
# `llm_service_error_http_exception` reads a provider `LLMException.status_code`
# from the __cause__ chain BEFORE it consults the engine `error_code`. A context
# overflow is HTTP 400 at the provider, so if the engine chained that exception
# onto the raised MilestoneEngineError, the documented `TOKEN_LIMIT → 503
# (retry 30)` mapping would silently become `4xx → 502 (no retry)`. These pin
# the source (no LLMException on the chain) AND the resulting HTTP contract.


@pytest.mark.asyncio
async def test_inner_raise_does_not_chain_provider_exception():
    """Source guard: the overflow the inner generator raises must carry the
    engine error_code but NOT put the provider LLMException on its __cause__
    chain — otherwise the HTTP boundary reads the provider 400 first."""
    from pydantic import BaseModel

    from faultmaven.core.investigation.llm_error_handler import (
        ErrorAction,
        ErrorResult,
    )
    from faultmaven.exceptions import LLMException

    class _Schema(BaseModel):
        agent_response: str = "x"

    engine = _make_engine()
    engine.llm_provider.get_structured_output_strategy = MagicMock(
        return_value=MagicMock()
    )

    provider_overflow = LLMException(
        "prompt is too long: 250000 > 200000", status_code=400
    )
    err = ErrorResult(
        action=ErrorAction.COMPRESS_MEMORY,
        message="Context too large. Compressing conversation history...",
        error_code=TOKEN_LIMIT,
        original_exception=provider_overflow,
    )

    with patch.object(
        engine.llm_error_handler, "with_retry", AsyncMock(return_value=(None, err))
    ):
        with pytest.raises(MilestoneEngineError) as exc_info:
            await engine._generate_structured_output_inner(
                prompt="hi", schema_model=_Schema
            )

    raised = exc_info.value
    assert raised.error_code == TOKEN_LIMIT
    # The provider LLMException must NOT be chained (would flip 503 → 502).
    assert raised.__cause__ is None
    assert raised.__context__ is None
    # ...but its wording is folded into the message text for diagnostics.
    assert "prompt is too long" in str(raised)


def test_propagated_token_limit_maps_to_retryable_503():
    """Contract guard: the exception shape the engine actually produces
    (ServiceException wrapping a MilestoneEngineError TOKEN_LIMIT, no provider
    LLMException on the chain) maps to 503 retry — the documented mapping. A
    regression that re-chained the provider 400 would land here as 502."""
    from faultmaven.api.exception_handlers import llm_service_error_http_exception
    from faultmaven.exceptions import ServiceException

    engine_err = MilestoneEngineError(
        "Structured output generation failed: Context too large.",
        error_code=TOKEN_LIMIT,
    )
    service_err = ServiceException(
        "Turn processing failed", details={"error_code": "TOKEN_LIMIT"}
    )
    service_err.__cause__ = engine_err  # as process_turn does via `raise ... from`

    http = llm_service_error_http_exception(service_err)
    assert http.status_code == 503
    assert http.headers["x-error-code"] == "LLM_PROVIDER_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Honest surfacing + the truncation class (Fable sign-off follow-ups)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degraded_prompt_tells_the_agent_it_has_no_tools():
    """The recovery drops the tool set, but the fallback body still lists
    addressable files (written for a tool-capable turn). Without the notice the
    agent is invited to search what it cannot reach, and the user cannot tell a
    context-starved answer from a normal one. Pin that the notice is appended and
    that it states both facts: reduced context AND no tools."""
    from faultmaven.core.investigation.prompts.templates import (
        DEGRADED_NO_TOOLS_NOTICE,
    )

    engine = _make_engine()
    case = MagicMock()
    case.case_id = "case_test"
    overflow = MilestoneEngineError("prompt is too long", error_code=TOKEN_LIMIT)
    inner = AsyncMock(side_effect=[overflow, MagicMock(name="degraded")])

    with (
        patch.object(engine, "_generate_structured_output_inner", inner),
        patch(
            "faultmaven.core.investigation.prompts.templates."
            "get_fallback_prompt_for_case",
            return_value="MINIMAL FALLBACK PROMPT",
        ),
    ):
        await engine._generate_structured_output(
            prompt="huge",
            schema_model=MagicMock(),
            case=case,
            user_message="what is wrong?",
        )

    retry_prompt = inner.await_args_list[1].args[0]
    assert retry_prompt.startswith("MINIMAL FALLBACK PROMPT")
    assert DEGRADED_NO_TOOLS_NOTICE in retry_prompt
    lowered = DEGRADED_NO_TOOLS_NOTICE.lower()
    assert "no" in lowered and "tools" in lowered, "must state tools are gone"
    assert "cannot be searched" in lowered, "must retract the file affordance"


@pytest.mark.asyncio
async def test_output_truncation_also_takes_the_degrade_path():
    """The D-path. Output truncation also classifies as TOKEN_LIMIT, so it
    reaches this recovery too — via the error_code, since truncation wording is
    NOT in CONTEXT_OVERFLOW_PHRASES. Pins that it degrades rather than failing,
    and that it is labeled as truncation (not silently counted as an overflow)
    so a rising truncation share is visible in the metric."""
    from faultmaven.core.investigation.llm_error_handler import (
        RECOVERY_REASON_OUTPUT_TRUNCATION,
        classify_token_limit_reason,
    )

    truncated = MilestoneEngineError(
        "Structured output generation failed: Context too large. "
        "(Unterminated string starting at line 3)",
        error_code=TOKEN_LIMIT,
    )
    # It must reach the recovery, and be attributed to truncation.
    assert _is_context_length_error(truncated) is True
    assert (
        classify_token_limit_reason(truncated) == RECOVERY_REASON_OUTPUT_TRUNCATION
    ), "truncation must not be mislabeled as an input overflow"

    engine = _make_engine()
    case = MagicMock()
    case.case_id = "case_test"
    degraded = MagicMock(name="degraded_response")
    inner = AsyncMock(side_effect=[truncated, degraded])

    with (
        patch.object(engine, "_generate_structured_output_inner", inner),
        patch(
            "faultmaven.core.investigation.prompts.templates."
            "get_fallback_prompt_for_case",
            return_value="MINIMAL FALLBACK PROMPT",
        ),
    ):
        result = await engine._generate_structured_output(
            prompt="huge",
            schema_model=MagicMock(),
            case=case,
            user_message="what is wrong?",
        )

    assert result is degraded
    assert inner.await_count == 2


def test_recovery_reason_classification():
    """The metric label must distinguish the class the recovery targets (input
    overflow) from the one it only serves incidentally (truncation), and must not
    guess when the provider wording did not survive."""
    from faultmaven.core.investigation.llm_error_handler import (
        RECOVERY_REASON_INPUT_OVERFLOW,
        RECOVERY_REASON_OUTPUT_TRUNCATION,
        RECOVERY_REASON_UNCLASSIFIED,
        classify_token_limit_reason,
    )

    assert (
        classify_token_limit_reason(Exception("prompt is too long: 250000 > 200000"))
        == RECOVERY_REASON_INPUT_OVERFLOW
    )
    assert (
        classify_token_limit_reason(Exception("EOF while parsing a value"))
        == RECOVERY_REASON_OUTPUT_TRUNCATION
    )
    # Pure engine signal, provider wording gone -> reported, never guessed.
    assert (
        classify_token_limit_reason(
            MilestoneEngineError("boom", error_code=TOKEN_LIMIT)
        )
        == RECOVERY_REASON_UNCLASSIFIED
    )
    # Both kinds present (the engine folds provider text into its message):
    # input-overflow wins, because that is what the recovery is designed for.
    assert (
        classify_token_limit_reason(
            Exception("prompt is too long ... unterminated string")
        )
        == RECOVERY_REASON_INPUT_OVERFLOW
    )


@pytest.mark.asyncio
async def test_degrade_emits_the_recovery_metric_with_its_reason():
    """Emission must actually happen and carry the reason label — an unwired
    metric reads as 'this never occurs', which is the opposite of the truth this
    counter exists to surface."""
    engine = _make_engine()
    case = MagicMock()
    case.case_id = "case_test"
    overflow = MilestoneEngineError(
        "Structured output generation failed: Context too large. "
        "(prompt is too long: 250000 > 200000)",
        error_code=TOKEN_LIMIT,
    )
    inner = AsyncMock(side_effect=[overflow, MagicMock(name="degraded")])
    metric = MagicMock()

    with (
        patch.object(engine, "_generate_structured_output_inner", inner),
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
            prompt="huge",
            schema_model=MagicMock(),
            case=case,
            user_message="what is wrong?",
        )

    metric.labels.assert_called_once_with(reason="input_overflow")
    metric.labels.return_value.inc.assert_called_once()
