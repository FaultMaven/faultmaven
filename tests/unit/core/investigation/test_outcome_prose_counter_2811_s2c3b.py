"""``narration_outcome_prose_total{provider,set}`` (#2811 S2c-3b).

Counts the outcome claims an INVESTIGATING model turn's own reply states in
prose: one increment per matching set per turn, none for engine prose, a
synthesized placeholder, an INQUIRY turn, a turn answered from the minimal
fallback prompt (which carries no rule) or a negated phrase. It never changes
the reply. ``investigating_model_turns_total`` counts the turns it divides by.
Driven through ``process_turn`` with only the LLM seam stubbed, through
``_compose_turn_reply`` for the gate-prose case, and through the generator's
context-length retry. No live LLM.
"""

from __future__ import annotations

from collections import Counter as Tally
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.config.settings import get_settings
from faultmaven.core.investigation import lifecycle_metrics
from faultmaven.core.investigation.milestone_engine import turn_completion
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.errors import MilestoneEngineError
from faultmaven.core.investigation.milestone_engine.response_synthesis import (
    OUTCOME_PROSE_SETS,
    outcome_prose_sets,
)
from faultmaven.core.investigation.milestone_engine.turn_completion import (
    _compose_turn_reply,
)
from faultmaven.core.investigation.prompts.templates.assembly import PromptReport
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
)
from faultmaven.exceptions import TOKEN_LIMIT
from faultmaven.modules.case.contracts import CaseState
from tests.case_builders import inquiry_case, investigating_case
from tests.model_content import disabled_redaction

pytestmark = pytest.mark.unit

_PROVIDER = "test-provider"

#: One stored-corpus shape per set (#2811 S2c-3b measurement), each a claim.
_CLAIMS = {
    "root_cause": "Based on the output, the root cause is confirmed.",
    "fix": "The fix held and the errors stopped.",
    "problem": "We know the issue is resolved because both rows are recorded.",
    "problem_confirmed": "Now that the problem is confirmed, we'll check the cert.",
    "mitigation": "The data confirms that the mitigation is holding.",
}


def test_the_sets_are_the_counters_closed_label_set():
    """The scan can only return a label the counter declares, and the counter
    declares exactly the ruled sets."""
    assert tuple(OUTCOME_PROSE_SETS) == lifecycle_metrics._OUTCOME_PROSE_SET_LABELS
    assert lifecycle_metrics._OUTCOME_PROSE_SET_LABELS == (
        "root_cause",
        "fix",
        "problem",
        "problem_confirmed",
        "mitigation",
    )


@pytest.mark.parametrize("name", sorted(_CLAIMS))
def test_each_set_matches_its_claim_and_no_other(name):
    assert outcome_prose_sets(_CLAIMS[name]) == (name,)


@pytest.mark.parametrize(
    "text",
    [
        "I can't say the root cause is confirmed yet.",
        "There is no sign the issue is resolved.",
        "We have not shown that the fix held.",
        "It is not yet clear the symptom is verified.",
        "It isn't clear the mitigation is holding.",
    ],
)
def test_a_negated_phrase_is_not_counted(text):
    assert outcome_prose_sets(text) == ()


@pytest.mark.parametrize(
    "text, sets",
    [
        ("No errors since 10:00. The issue is resolved.", ("problem",)),
        # The review of #3045: a negation in an earlier clause of the same
        # sentence hides nothing.
        (
            "I'm not sure about the cert, but the root cause is confirmed — it "
            "is the pool.",
            ("root_cause",),
        ),
        ("No, the issue is resolved.", ("problem",)),
        ("Unable to reproduce earlier, the issue is resolved now.", ("problem",)),
        ("Not the cert - the root cause is identified as the pool.", ("root_cause",)),
        ("Nothing failed — the fix is verified.", ("fix",)),
    ],
)
def test_a_negation_in_an_earlier_clause_does_not_hide_a_claim(text, sets):
    assert outcome_prose_sets(text) == sets


@pytest.mark.parametrize(
    "text, sets",
    [
        ("The issue has now been resolved.", ("problem",)),
        ("The root cause was finally identified.", ("root_cause",)),
        ("The root-cause is confirmed.", ("root_cause",)),
        ("The mitigation is now holding.", ("mitigation",)),
        ("The mitigation has been verified.", ("mitigation",)),
        ("The fix is verified.", ("fix",)),
        ("The fix is now holding.", ("fix",)),
        ("The symptom has been fully confirmed.", ("problem_confirmed",)),
    ],
)
def test_an_auxiliary_with_an_adverb_and_the_hyphenated_form_count(text, sets):
    assert outcome_prose_sets(text) == sets


def test_a_copied_engine_clause_counts():
    """The model copying the engine's statement still states it in prose."""
    assert outcome_prose_sets(
        "**Case record:** the problem is confirmed; a root cause is identified."
    ) == ("root_cause", "problem_confirmed")


# ---------------------------------------------------------------------------
# Through the turn
# ---------------------------------------------------------------------------


@pytest.fixture
def counted(monkeypatch):
    """Every increment of the counter, as (provider, set), and of its
    denominator, as ("turns", provider, prompt)."""
    tally: Tally = Tally()

    def labels(*, provider, set):  # noqa: A002 — the label's name
        return SimpleNamespace(inc=lambda: tally.update([(provider, set)]))

    def turn_labels(*, provider, prompt):
        return SimpleNamespace(inc=lambda: tally.update([("turns", provider, prompt)]))

    monkeypatch.setattr(
        turn_completion,
        "narration_outcome_prose_total",
        SimpleNamespace(labels=labels),
    )
    monkeypatch.setattr(
        turn_completion,
        "investigating_model_turns_total",
        SimpleNamespace(labels=turn_labels),
    )
    return tally


_FULL = ("turns", "test-provider", "full")
_FALLBACK = ("turns", "test-provider", "fallback")


def _engine(response) -> MilestoneEngine:
    llm = MagicMock()
    # Real strings: the provider label, and a model name the budget resolver
    # can read (see test_narration_overclaim_apply._make_engine).
    llm.provider_name = _PROVIDER
    llm.config.default_model = "test-model"
    repo = MagicMock()
    repo.save = AsyncMock(side_effect=lambda c, **_: c)
    repo.get = AsyncMock(side_effect=lambda cid: None)
    engine = MilestoneEngine(llm, repo, investigation_tools=MagicMock())
    engine.generator.generate_structured_output = AsyncMock(return_value=response)
    return engine


def _investigating():
    return investigating_case(
        case_id="case_2811c3b00002",
        title="Outcome prose counter",
        user_id="user_test",
        enterprise_id="org_test",
        description="Outcome prose counter",
        statement="Outcome prose counter",
        current_turn=3,
    )


def _diagnosis(text: str):
    return InvestigationResponse_Diagnosis.model_validate(
        {"agent_response": text, "state_updates": {}}
    )


async def test_an_investigating_turn_counts_each_set_once_and_keeps_the_reply(
    counted,
):
    text = (
        "The root cause is confirmed: the pool cap. The root cause has been "
        "established twice over. The issue is resolved."
    )
    result = await _engine(_diagnosis(text)).process_turn(
        case=_investigating(), user_message="what now?", redaction_ctx=None
    )
    assert counted == {
        (_PROVIDER, "root_cause"): 1,
        (_PROVIDER, "problem"): 1,
        _FULL: 1,
    }
    assert result["agent_response"].startswith(text)


async def test_a_clean_investigating_turn_counts_only_the_turn(counted):
    await _engine(_diagnosis("Let's check the pod events next.")).process_turn(
        case=_investigating(), user_message="what now?", redaction_ctx=None
    )
    assert counted == {_FULL: 1}


async def test_a_turn_answered_from_the_budget_fallback_is_not_counted(
    counted, monkeypatch
):
    """A target the template cannot fit starves the assembly into the minimal
    fallback prompt, which carries no rule: the turn is volume only."""
    context = get_settings().model_context
    monkeypatch.setattr(context, "prompt_target_tokens", 2_000)
    prompts = []
    engine = _engine(_diagnosis(_CLAIMS["root_cause"]))
    answer = engine.generator.generate_structured_output.return_value

    async def answered(prompt, *_args, **_kwargs):
        prompts.append(prompt)
        return answer

    engine.generator.generate_structured_output.side_effect = answered
    await engine.process_turn(
        case=_investigating(), user_message="what now?", redaction_ctx=None
    )
    (prompt,) = prompts
    assert "only through the structured fields" not in prompt
    assert counted == {_FALLBACK: 1}


async def test_a_turn_answered_from_the_context_length_retry_is_not_counted(
    counted,
):
    """The generator marks the turn's report when its context-length retry
    answers from the minimal prompt; the engine reads it."""
    engine = _engine(_diagnosis(_CLAIMS["root_cause"]))
    answer = engine.generator.generate_structured_output.return_value

    async def retried(*_args, prompt_report, **_kwargs):
        prompt_report.fallback = True
        return answer

    engine.generator.generate_structured_output.side_effect = retried
    await engine.process_turn(
        case=_investigating(), user_message="what now?", redaction_ctx=None
    )
    assert counted == {_FALLBACK: 1}


@pytest.mark.parametrize("overflows", [True, False])
async def test_the_context_length_retry_marks_the_report(overflows):
    engine = _engine(_diagnosis("unused"))
    del engine.generator.generate_structured_output  # the real method
    answer = MagicMock(name="answer")
    overflow = MilestoneEngineError("Context too large", error_code=TOKEN_LIMIT)
    inner = AsyncMock(side_effect=[overflow, answer] if overflows else [answer])
    report = PromptReport()
    with (
        patch.object(engine.generator, "_generate_structured_output_inner", inner),
        patch(
            "faultmaven.core.investigation.prompts.templates.fallback."
            "get_fallback_prompt_for_case",
            return_value="MINIMAL FALLBACK PROMPT",
        ),
    ):
        result = await engine.generator.generate_structured_output(
            prompt="the full prompt",
            schema_model=MagicMock(),
            case=MagicMock(case_id="case_2811c3b00004"),
            user_message="what now?",
            redaction_ctx=disabled_redaction(),
            prompt_report=report,
        )
    assert result is answer
    assert report.fallback is overflows


async def test_an_inquiry_turn_counts_nothing(counted):
    response = InquiryResponse.model_validate(
        {"agent_response": _CLAIMS["problem_confirmed"], "state_updates": {}}
    )
    case = inquiry_case(
        case_id="case_2811c3b00003",
        title="Outcome prose counter",
        user_id="user_test",
        enterprise_id="org_test",
        description="Outcome prose counter",
        current_turn=1,
    )
    result = await _engine(response).process_turn(
        case=case, user_message="the api is down", redaction_ctx=None
    )
    assert _CLAIMS["problem_confirmed"] in result["agent_response"]
    assert counted == {}  # not an INVESTIGATING turn: no volume either


async def test_a_synthesized_turn_counts_nothing(counted):
    """The flag decides, not the text: even a placeholder that read as a
    claim is the engine's, not the model's."""
    response = _diagnosis(_CLAIMS["root_cause"])
    response._agent_response_synthesized = True
    await _engine(response).process_turn(
        case=_investigating(), user_message="what now?", redaction_ctx=None
    )
    assert counted == {}


async def test_gate_prose_is_not_counted(counted):
    """Engine prose composed under the reply states outcomes; only the
    model's own text is scanned."""
    llm = SimpleNamespace(provider_name=_PROVIDER)
    result = await _compose_turn_reply(
        llm,
        None,
        MagicMock(),
        case_updated=_investigating(),
        follow_ups=[],
        metadata={
            "milestones_completed": [],
            "false_alarm_closure_message": (
                "The root cause is confirmed and the issue is resolved."
            ),
        },
        plan=MagicMock(),
        redaction_ctx=None,
        response_obj=_diagnosis("Noted."),
        stagnation_str="",
        summary_failed=False,
        summary_payload=None,
        validation_repairs=[],
        prompted_state=CaseState.INVESTIGATING,
    )
    assert "the issue is resolved" in result["agent_response"]
    assert counted == {_FULL: 1}
