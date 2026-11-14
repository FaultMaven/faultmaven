"""A starved INFERENCE call is re-issued once without the intent (#2802).

The tool-less single shot declares ``ReasoningIntent.INFERENCE`` with an output
floor (fm#1116/#1117). When hidden reasoning takes the whole cap, the router
raises ``LLMOutputFloorError``. The error is non-retryable (an identical retry
starves identically), so the turn used to fail. Now the call is re-issued once
without the intent and its floor, at the provider's default for a structured
call, never from or into the cache, when the turn budget affords it. Measured
on Azure gpt-6-luna-1: the starved turns at "low" never starved (48 calls).

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
    TOOLLESS_INFERENCE_OUTPUT_FLOOR,
)
from faultmaven.core.investigation.schemas import InvestigationResponse_Diagnosis
from faultmaven.exceptions import LLMOutputFloorError
from faultmaven.infrastructure.llm.providers import ReasoningIntent, StopReason
from faultmaven.infrastructure.llm.providers.base import LLMResponse
from faultmaven.infrastructure.llm.structured_output_capability import (
    StructuredOutputCapability,
    create_strategy_for_capability,
)
from tests.model_content import disabled_redaction

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ANSWER = LLMResponse(
    content='{"agent_response": "the clock on app-02 is ahead", "state_updates": {}}',
    confidence=0.9,
    provider="azure",
    model="gpt-6-luna-1",
    tokens_used=10,
    response_time_ms=1,
    stop_reason=StopReason.STOP,
)


def _starved() -> LLMOutputFloorError:
    return LLMOutputFloorError(
        "LLM response starved below the declared output floor: ~0 visible output "
        "tokens < min_output_tokens=2048, with stop_reason=MAX_TOKENS at "
        "max_tokens=8000 (provider=azure model=gpt-6-luna-1)"
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


async def _single_shot(engine, *, inference=True):
    case = MagicMock()
    case.case_id = "case_2802"
    kwargs = (
        dict(
            reasoning_intent=ReasoningIntent.INFERENCE,
            min_output_tokens=TOOLLESS_INFERENCE_OUTPUT_FLOOR,
        )
        if inference
        else {}
    )
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
            **kwargs,
        )


async def test_a_starved_call_is_reissued_once_without_the_intent():
    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([_starved(), ANSWER])

    result = await _single_shot(engine)

    assert result.agent_response == "the clock on app-02 is ahead"
    first, second = sent
    assert first["reasoning_intent"] is ReasoningIntent.INFERENCE
    assert first["min_output_tokens"] == TOOLLESS_INFERENCE_OUTPUT_FLOOR
    assert "reasoning_intent" not in second and "min_output_tokens" not in second
    assert second["bypass_cache"] is True and second["store_in_cache"] is False
    assert second["prompt"] == first["prompt"]
    assert second["max_tokens"] == first["max_tokens"]


async def test_starved_again_fails_after_two_calls():
    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([_starved(), _starved(), ANSWER])

    with pytest.raises(MilestoneEngineError, match="starved"):
        await _single_shot(engine)

    assert len(sent) == 2


async def test_a_call_without_the_intent_is_not_reissued():
    """The floor buys the INFERENCE lift; without the lift there is nothing to
    lower, so a starved call stays a failure."""
    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([_starved(), ANSWER])

    with pytest.raises(MilestoneEngineError):
        await _single_shot(engine, inference=False)

    assert len(sent) == 1


@pytest.mark.parametrize("affordable", [False, True])
async def test_the_reissue_is_made_only_when_the_turn_budget_affords_it(
    affordable, monkeypatch
):
    """Within a bound turn, the estimate is a fraction of the starved attempt's
    duration: an unaffordable estimate makes no second call; a cheap one,
    under the same deadline, does (the positive control)."""
    monkeypatch.setattr(
        generation, "STARVED_INFERENCE_RETRY_COST_FRACTION", 0.0 if affordable else 1e9
    )
    clock = iter([0.0, 60.0])
    monkeypatch.setattr(
        generation, "time", MagicMock(monotonic=lambda: next(clock, 60.0))
    )
    engine = _engine()
    engine.deps.llm_provider.route, sent = _queue([_starved(), ANSWER])
    reserves = (
        turn_budget.TURN_BUDGET_RESERVE_SECONDS
        + turn_budget.TURN_COMMIT_RESERVE_SECONDS
    )

    with turn_budget.bind_turn_deadline(reserves + 30.0):
        if affordable:
            await _single_shot(engine)
        else:
            with pytest.raises(MilestoneEngineError):
                await _single_shot(engine)

    assert len(sent) == (2 if affordable else 1)
