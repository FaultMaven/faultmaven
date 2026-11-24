"""The prompt's working vocabulary never reaches the user (#2494).

Replies repeated the prompt's private shorthand ("fails clause (b) of the
Root-Cause Test") to a user who has never seen the rule. One rule, stated as a
property, is in every prompt that shows the test's name, alias or clause
letters, and each emitter of the name outside the templates (the held-open
withdrawal notice, the M5 downgrade reason, the response schemas) renders only
into such a prompt.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation import schemas
from faultmaven.core.investigation.milestone_engine.response_application import (
    WHY_OPEN_DOWNGRADE_REASON,
)
from faultmaven.core.investigation.narration.codes import NoticeCode
from faultmaven.core.investigation.narration.notices import render_notice
from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.blocks import (
    _ROOT_CAUSE_TEST_BLOCK,
    _WORKING_VOCABULARY_RULE,
)
from faultmaven.core.investigation.prompts.templates.fallback import (
    get_fallback_prompt_for_case,
)
from faultmaven.modules.case.contracts import (
    Case,
    InvestigationActionType,
    InvestigationStage,
    MitigationRecord,
    ProblemStatus,
    ProposedAction,
)
from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress
from tests.case_builders import investigating_case
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

pytestmark = pytest.mark.unit

#: How the prompt spells the test, its alias and its clauses.
_TEST_TERMS = ("ROOT-CAUSE TEST", "why-stopping", "clause (b)", "clause b")
_STAGES = ("diagnosis", "mitigation", "treatment")
_MODES = ("triage", "directed_analysis", "knowledge_query", "agent_meta")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _shows_the_test(text: str) -> bool:
    return any(term in text for term in _TEST_TERMS)


def _case(stage: str = "diagnosis") -> Case:
    case = investigating_case(
        statement="Order processing pods are OOM-killed",
        symptom="order-processing pods restart with OOMKilled",
        current_turn=4,
        problem_status=ProblemStatus.VERIFIED,
    )
    if stage == "mitigation":
        case.progress.mitigation = MitigationRecord(proposed_at_turn=2, accepted=True)
    elif stage == "treatment":
        case.progress.solution_accepted = True
    return case


def _with_notice(case: Case, notice: str) -> Case:
    case.turn_history = [
        TurnProgress(
            turn_number=n,
            timestamp=datetime.now(UTC),
            progress_made=False,
            outcome=TurnOutcome.CONVERSATION,
            user_message_summary=f"user {n}",
            agent_response_summary=f"agent {n}",
            system_notices=[notice] if n == 3 else [],
        )
        for n in (1, 2, 3)
    ]
    case.current_turn = 3
    return case


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_the_rule_is_a_property_with_the_test_as_its_example():
    text = _flat(_WORKING_VOCABULARY_RULE)
    assert "NEVER put working vocabulary in what the user reads" in text
    assert (
        "a term that takes its meaning only from this prompt, or that names "
        "FaultMaven's own machinery" in text
    )
    # Every channel the user reads.
    for field in ("agent_response", "suggested_follow_ups", "any action you propose"):
        assert field in text
    # The test's name, its alias and its clause letters are the examples.
    assert 'the ROOT-CAUSE TEST, its alias "why-stopping" and its clause letters' in (
        text
    )
    # The ruling's unacceptable words, and its acceptable ones.
    for term in ('"WORKAROUND" as a label', '"SYSTEM:"', '"the engine"'):
        assert term in text
    assert '"intercept" or "interception" as a category name' in text
    assert (
        'A lower-case "workaround", or "intercept" as an ordinary verb in a '
        "sentence that stands on its own, is plain words" in text
    )
    # The reasoning stays, in plain words.
    assert "Say the reasoning in plain words instead" in text
    # It is .format()-ed into the base's slot and into the fallback template.
    assert "{" not in _WORKING_VOCABULARY_RULE and "}" not in _WORKING_VOCABULARY_RULE


def test_the_test_points_at_the_rule():
    last = _flat(_ROOT_CAUSE_TEST_BLOCK.splitlines()[-2] + " ")
    last += _flat(_ROOT_CAUSE_TEST_BLOCK.splitlines()[-1])
    assert 'alias "why-stopping" and its clause letters are WORKING' in last
    assert "they never reach the user (see that rule)" in last
    assert "{" not in _ROOT_CAUSE_TEST_BLOCK and "}" not in _ROOT_CAUSE_TEST_BLOCK


# ---------------------------------------------------------------------------
# Every prompt that shows the test carries the rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stage", _STAGES)
@pytest.mark.parametrize("mode", _MODES)
def test_every_investigating_prompt_carries_the_rule(stage, mode):
    """The knowledge-query and meta turns waive the grounding block the rule
    sits in, but the base still names the test on them."""
    prompt = get_prompt_for_case(
        _case(stage), "what now?", processing_mode=mode, given_tools=DEFAULT_GIVEN
    )
    assert _shows_the_test(prompt)
    assert prompt.count(_WORKING_VOCABULARY_RULE) == 1


@pytest.mark.parametrize("fallback", [False, True])
def test_the_held_open_withdrawal_reaches_only_prompts_with_the_rule(fallback):
    """The fallback has no ROOT-CAUSE TEST block, but the previous turn's
    held-open withdrawal names the test (critique 1(b), amendment 4)."""
    (notice,) = render_notice(
        NoticeCode.SOLUTION_OFFER_WITHDRAWN, {"held_open": True}, None, []
    )
    case = _with_notice(_case(), notice)
    build = get_fallback_prompt_for_case if fallback else get_prompt_for_case
    prompt = build(case, "what now?", given_tools=DEFAULT_GIVEN)
    assert "ROOT-CAUSE TEST, clause b" in prompt
    assert _WORKING_VOCABULARY_RULE in prompt


def test_the_why_open_downgrade_reason_reaches_a_prompt_with_the_rule():
    """Read only as the pending action's ENGINE_NOTE, in the main
    investigation prompt."""
    assert _shows_the_test(WHY_OPEN_DOWNGRADE_REASON)
    case = _case()
    case.proposed_actions = [
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.MITIGATION,
            description="Raise MaxMetaspaceSize to 512m",
            proposed_in_turn=3,
            downgrade_reason=WHY_OPEN_DOWNGRADE_REASON,
        )
    ]
    prompt = get_prompt_for_case(case, "what now?", given_tools=DEFAULT_GIVEN)
    assert f"ENGINE_NOTE: {WHY_OPEN_DOWNGRADE_REASON}" in prompt
    assert _WORKING_VOCABULARY_RULE in prompt


def test_a_schema_that_names_the_test_goes_only_with_an_investigating_prompt():
    """On a strict-schema provider the response schema's descriptions reach
    the model beside the prompt. Every response model whose schema names the
    test is one ``get_schema_for_stage`` serves, the INVESTIGATING prompts
    above."""
    stage_schemas = {schemas.get_schema_for_stage(s) for s in InvestigationStage}
    stage_schemas.add(schemas.get_schema_for_stage(None))
    response_models = [
        obj
        for obj in vars(schemas).values()
        if isinstance(obj, type)
        and issubclass(obj, schemas.BaseInteractionResponse)
        and obj is not schemas.BaseInteractionResponse
    ]
    naming = {
        model
        for model in response_models
        if _shows_the_test(json.dumps(model.model_json_schema()))
    }
    assert naming, "no response schema names the test: the pin has lost its subject"
    assert naming <= stage_schemas
    assert schemas.InquiryResponse not in naming
    assert schemas.TerminalResponse not in naming
