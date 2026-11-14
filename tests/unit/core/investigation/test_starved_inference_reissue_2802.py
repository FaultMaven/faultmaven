"""A starved INFERENCE call is re-issued once without the intent (#2802).

The tool-less single shot declares ``ReasoningIntent.INFERENCE`` with an output
floor (fm#1116/#1117). When hidden reasoning takes the whole cap the call is
starved, and that surfaces three ways: the router's floor check
(``LLMOutputFloorError``), a provider that reports the cut itself (Gemini raises
``OUTPUT_TRUNCATION`` before the router sees a body), and a body returned cut at
the cap. Before, the first failed the turn and the other two doubled the cap
with the intent kept. Now all three are one signal: the call is re-issued once
per engine call without the intent and its floor, at the same cap, never from
or into the cache, when the turn budget affords a fraction of the starved
attempt's duration. After that a cut follows the ordinary truncation ladder.

Entered at ``generate_structured_output``, the router a send recorder.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation import turn_budget
from faultmaven.core.investigation.llm_error_handler import LLMErrorHandler, RetryConfig
from faultmaven.core.investigation.milestone_engine import generation
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.generation import (
    STRUCTURED_OUTPUT_MAX_TOKENS,
    TOOLLESS_INFERENCE_OUTPUT_FLOOR,
)
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.exceptions import LLMErrorCategory, LLMException, LLMOutputFloorError
from faultmaven.infrastructure.llm.providers import ReasoningIntent, StopReason
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    create_strategy_for_capability,
)
from tests.model_content import disabled_redaction

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

INFERENCE = dict(
    reasoning_intent=ReasoningIntent.INFERENCE,
    min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR,
)


def _response(content: str, stop: StopReason = StopReason.STOP) -> LLMResponse:
    return LLMResponse(
        content=content,
        confidence=0.9,
        provider="azure",
        model="gpt-6-luna-1",
        tokens_used=10,
        response_time_ms=1,
        stop_reason=stop,
    )


ANSWER = _response(
    '{"agent_response": "the clock on app-02 is ahead", "state_updates": {}}'
)
CUT = _response('{"agent_response": "the clock on app-02 is', StopReason.MAX_TOKENS)


def _floor() -> LLMOutputFloorError:
    """What the router raises (router.py, the post-call floor check)."""
    return LLMOutputFloorError(
        "LLM response starved below the declared output floor: ~0 visible output "
        "tokens < min_output_tokens=2048, with stop_reason=MAX_TOKENS at "
        "max_tokens=8000 (provider=azure model=gpt-6-luna-1)"
    )


def _gemini_cut() -> LLMException:
    """What Gemini raises on finishReason=MAX_TOKENS for a structured call."""
    return LLMException(
        "Response truncated due to token limit (finishReason=MAX_TOKENS).",
        retryable=True,
        category=LLMErrorCategory.OUTPUT_TRUNCATION,
    )


def _queue(replies):
    queue, sent = list(replies), []

    async def _send(**kwargs):
        sent.append(kwargs)
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return AsyncMock(side_effect=_send), sent


def _engine():
    engine = MilestoneEngine(
        llm_provider=AsyncMock(), repository=MagicMock(), investigation_tools=None
    )
    engine.deps.llm_error_handler = LLMErrorHandler(
        RetryConfig(max_retries=3, base_delay_seconds=0.0, max_delay_seconds=0.0)
    )
    engine.deps.llm_provider.get_structured_output_strategy = MagicMock(
        side_effect=lambda schema, model=None: create_strategy_for_capability(
            StructuredOutputCapability.STRICT, schema
        )
    )
    return engine


async def _single_shot(engine, **intent):
    case = MagicMock()
    case.case_id = "case_2802"
    with patch(
        "faultmaven.core.investigation.prompts.templates.fallback."
        "get_fallback_prompt_for_case",
        return_value="MINIMAL FALLBACK PROMPT",
    ):
        return await engine.generator.generate_structured_output(
            "why do first requests after login get 401?",
            InvestigationResponse_Diagnosis,
            case=case,
            user_message="first requests after login get 401",
            redaction_ctx=disabled_redaction(),
            **intent,
        )


def _intent(call):
    return call.get("reasoning_intent"), call.get("min_output_tokens")


@pytest.mark.parametrize(
    "starved",
    [_floor, _gemini_cut, lambda: CUT],
    ids=["router_floor", "provider_reported_cut", "cut_body"],
)
async def test_every_form_of_starvation_is_reissued_once_without_the_intent(
    starved, monkeypatch
):
    counter = MagicMock()
    monkeypatch.setattr(generation, "inference_starved_total", counter)
    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([starved(), ANSWER])

    result = await _single_shot(engine, **INFERENCE)

    assert result.agent_response == "the clock on app-02 is ahead"
    first, second = sent
    assert _intent(first) == (
        ReasoningIntent.INFERENCE,
        TOOLLESS_INFERENCE_OUTPUT_FLOOR,
    )
    assert _intent(second) == (None, None)
    # The same cap: reasoning is lowered, not the cap raised.
    assert first["max_tokens"] == second["max_tokens"] == STRUCTURED_OUTPUT_MAX_TOKENS
    assert second["bypass_cache"] is True and second["store_in_cache"] is False
    assert second["prompt"] == first["prompt"]
    outcomes = [c.kwargs["outcome"] for c in counter.labels.call_args_list]
    assert outcomes == ["reissued", "recovered"]


async def test_after_the_reissue_a_cut_follows_the_ordinary_ladder():
    """The realistic second failure: the lowered call returns a cut body. It
    raises the cap; the intent is never put back."""
    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([_floor(), CUT, ANSWER])

    await _single_shot(engine, **INFERENCE)

    assert [_intent(c) for c in sent] == [
        (ReasoningIntent.INFERENCE, TOOLLESS_INFERENCE_OUTPUT_FLOOR),
        (None, None),
        (None, None),
    ]
    assert [c["max_tokens"] for c in sent] == [
        STRUCTURED_OUTPUT_MAX_TOKENS,
        STRUCTURED_OUTPUT_MAX_TOKENS,
        STRUCTURED_OUTPUT_MAX_TOKENS * 2,
    ]


async def test_the_reissue_runs_once_per_engine_call_the_degrade_included():
    """Lowered, then cut to the cap ceiling: #662's minimal-prompt degrade runs,
    and it too is sent without the intent: nothing re-arms it."""
    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([_floor(), CUT, CUT, ANSWER])

    await _single_shot(engine, **INFERENCE)

    assert sent[-1]["prompt"].startswith("MINIMAL FALLBACK PROMPT")
    assert [_intent(c)[0] for c in sent] == [
        ReasoningIntent.INFERENCE,
        None,
        None,
        None,
    ]


@pytest.mark.parametrize(
    "intent",
    [
        {},
        dict(reasoning_intent=ReasoningIntent.EXTRACTION, min_output_tokens=512),
    ],
    ids=["no_intent", "extraction"],
)
async def test_only_an_inference_call_is_lowered(intent):
    """Without INFERENCE nothing lifted the reasoning, so a cut raises the cap
    as before (positive control: the bump), and a floor error stays final."""
    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([CUT, ANSWER])
    await _single_shot(engine, **intent)
    assert [c["max_tokens"] for c in sent] == [
        STRUCTURED_OUTPUT_MAX_TOKENS,
        STRUCTURED_OUTPUT_MAX_TOKENS * 2,
    ]

    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([_floor(), ANSWER])
    with pytest.raises(MilestoneEngineError):
        await _single_shot(engine, **intent)
    assert len(sent) == 1


@pytest.mark.parametrize("spendable, reissued", [(30.0, False), (40.0, True)])
async def test_the_budget_is_judged_on_the_starved_attempts_own_duration(
    spendable, reissued, monkeypatch
):
    """The real fraction (0.6) of a 60 s starved attempt is 36 s: under 30 s
    spendable it is not re-issued, under 40 s it is."""
    assert generation.STARVED_INFERENCE_RETRY_COST_FRACTION == 0.6
    ticks = iter([0.0, 60.0])
    monkeypatch.setattr(
        generation, "time", MagicMock(monotonic=lambda: next(ticks, 60.0))
    )
    counter = MagicMock()
    monkeypatch.setattr(generation, "inference_starved_total", counter)
    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([_floor(), ANSWER])
    reserves = (
        turn_budget.TURN_BUDGET_RESERVE_SECONDS
        + turn_budget.TURN_COMMIT_RESERVE_SECONDS
    )

    with turn_budget.bind_turn_deadline(reserves + spendable):
        if reissued:
            await _single_shot(engine, **INFERENCE)
        else:
            with pytest.raises(MilestoneEngineError, match="starved"):
                await _single_shot(engine, **INFERENCE)

    assert len(sent) == (2 if reissued else 1)
    outcomes = [c.kwargs["outcome"] for c in counter.labels.call_args_list]
    assert outcomes == (["reissued", "recovered"] if reissued else ["unaffordable"])


async def test_a_failed_reissue_still_names_the_starvation(monkeypatch):
    counter = MagicMock()
    monkeypatch.setattr(generation, "inference_starved_total", counter)
    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([_floor(), ValueError("boom")])

    with pytest.raises(MilestoneEngineError) as raised:
        await _single_shot(engine, **INFERENCE)

    assert len(sent) == 2
    assert "after the INFERENCE attempt starved" in str(raised.value)
    outcomes = [c.kwargs["outcome"] for c in counter.labels.call_args_list]
    assert outcomes == ["reissued", "failed"]
