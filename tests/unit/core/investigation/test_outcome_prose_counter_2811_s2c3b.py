"""``narration_outcome_prose_total{provider,set}`` (#2811 S2c-3b).

Counts the outcome claims an INVESTIGATING model turn's own reply states in
prose: one increment per matching set per turn, none for engine prose, a
synthesized placeholder, an INQUIRY turn or a negated phrase. It never changes
the reply. Driven through ``process_turn`` with only the LLM seam stubbed, and
through ``_compose_turn_reply`` for the gate-prose case. No live LLM.
"""

from __future__ import annotations

from collections import Counter as Tally
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation import lifecycle_metrics
from faultmaven.core.investigation.milestone_engine import turn_completion
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.response_synthesis import (
    OUTCOME_PROSE_SETS,
    outcome_prose_sets,
)
from faultmaven.core.investigation.milestone_engine.turn_completion import (
    _compose_turn_reply,
)
from faultmaven.core.investigation.schemas import (
    InquiryResponse,
    InvestigationResponse_Diagnosis,
)
from faultmaven.modules.case.contracts import CaseState
from tests.case_builders import inquiry_case, investigating_case

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
        "Without the logs, the symptom is verified only by your report",
        "It isn't clear the mitigation is holding.",
    ],
)
def test_a_negated_phrase_is_not_counted(text):
    assert outcome_prose_sets(text) == ()


def test_a_negation_in_an_earlier_clause_does_not_hide_a_claim():
    assert outcome_prose_sets("No errors since 10:00. The issue is resolved.") == (
        "problem",
    )


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
    """Every increment of the counter, as (provider, set)."""
    tally: Tally = Tally()

    def labels(*, provider, set):  # noqa: A002 — the label's name
        return SimpleNamespace(inc=lambda: tally.update([(provider, set)]))

    monkeypatch.setattr(
        turn_completion,
        "narration_outcome_prose_total",
        SimpleNamespace(labels=labels),
    )
    return tally


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
    assert counted == {(_PROVIDER, "root_cause"): 1, (_PROVIDER, "problem"): 1}
    assert result["agent_response"].startswith(text)


async def test_a_clean_investigating_turn_counts_nothing(counted):
    await _engine(_diagnosis("Let's check the pod events next.")).process_turn(
        case=_investigating(), user_message="what now?", redaction_ctx=None
    )
    assert counted == {}


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
    assert counted == {}


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
    assert counted == {}
