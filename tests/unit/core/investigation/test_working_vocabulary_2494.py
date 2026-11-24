"""The prompt's working vocabulary never reaches the user (#2494).

Replies repeated the prompt's private shorthand ("fails clause (b) of the
Root-Cause Test") to a user who has never seen the rule. One rule, stated as a
property, is in every prompt that shows a term it names ("ROOT-CAUSE TEST",
"why-stopping", "the engine", "SYSTEM:", "WORKAROUND"), exactly once: in full
with its examples in the main prompts, as the property alone in the
size-bounded fallbacks. Each emitter of the test's name outside the templates
(the held-open withdrawal notice, the M5 downgrade reason, the response
schemas) renders only into such a prompt. The terms stay in the model-facing
text: they are the model's vocabulary.
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
    _WORKING_VOCABULARY_PROPERTY,
    _WORKING_VOCABULARY_RULE,
)
from faultmaven.core.investigation.prompts.templates.fallback import (
    get_fallback_prompt_for_case,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    InvestigationActionType,
    InvestigationStage,
    MitigationRecord,
    ProblemStatus,
    ProposedAction,
)
from faultmaven.modules.case.domain.models.turn import TurnOutcome, TurnProgress
from tests.case_builders import inquiry_case, investigating_case
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

pytestmark = pytest.mark.unit

#: How the prompt spells the test, its alias and its clauses.
_TEST_TERMS = ("ROOT-CAUSE TEST", "why-stopping", "clause (b)", "clause b")
#: The terms the rule names that the model-facing text shows.
_NAMED_TERMS = (
    "ROOT-CAUSE TEST",
    "why-stopping",
    "the engine",
    "SYSTEM:",
    "WORKAROUND",
)
_STAGES = ("diagnosis", "mitigation", "treatment")
_MODES = ("triage", "directed_analysis", "knowledge_query", "agent_meta")
_WITHDRAWN = render_notice(
    NoticeCode.SOLUTION_OFFER_WITHDRAWN, {"held_open": True}, None, []
)[0]
_INQUIRY_NOTICE = "SYSTEM: proposed_problem_statement was not recorded: it was empty."


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


def test_the_property_is_stated_once_and_the_full_rule_adds_examples():
    prop = _flat(_WORKING_VOCABULARY_PROPERTY)
    assert "NEVER let WORKING VOCABULARY reach the user" in prop
    assert (
        "a term that takes its meaning only from this prompt, or that names "
        "FaultMaven's own machinery" in prop
    )
    assert "Say the reasoning in plain words" in prop
    # The full rule is the property plus examples, never a restatement.
    assert _WORKING_VOCABULARY_RULE.startswith(_WORKING_VOCABULARY_PROPERTY)
    text = _flat(_WORKING_VOCABULARY_RULE[len(_WORKING_VOCABULARY_PROPERTY) :])
    assert "takes its meaning" not in text
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
    # Both are .format()-ed into templates.
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


def _terminal(state: CaseState) -> Case:
    stamp = "resolved_at" if state == CaseState.RESOLVED else "closed_at"
    update = {"state": state, stamp: datetime.now(UTC)}
    if state == CaseState.CLOSED:
        update["closure_reason"] = "solution_deferred"
    return _case().model_copy(update=update)


def _render(key: str) -> str:
    """One prompt of the matrix. The open-case ones carry the previous turn's
    notice: the held-open withdrawal, which names the test, while
    investigating; a "SYSTEM:" line in inquiry."""
    kind, _, rest = key.partition(":")
    build = get_fallback_prompt_for_case if kind == "fallback" else get_prompt_for_case
    if rest.startswith("inquiry"):
        case = _with_notice(
            inquiry_case(statement="Pods are OOM-killed"), _INQUIRY_NOTICE
        )
    elif rest in ("resolved", "closed"):
        case = _terminal(CaseState(rest))
    else:
        case = _with_notice(_case(rest.partition("/")[0]), _WITHDRAWN)
    mode = rest.partition("/")[2] or None
    if kind == "fallback":
        return build(case, "what now?", given_tools=DEFAULT_GIVEN)
    return build(case, "what now?", processing_mode=mode, given_tools=DEFAULT_GIVEN)


_OPEN = [f"main:{s}/{m}" for s in _STAGES for m in _MODES] + [
    "main:inquiry",
    "main:inquiry/agent_meta",
    "fallback:diagnosis",
    "fallback:inquiry",
]
_TERMINAL = [f"{k}:{s}" for k in ("main", "fallback") for s in ("resolved", "closed")]


@pytest.mark.parametrize("key", _OPEN + _TERMINAL)
def test_every_prompt_that_shows_a_named_term_carries_the_property_once(key):
    """Main prompts carry the full rule, the size-bounded fallbacks the
    property alone. The rule names the terms itself, so what the prompt shows
    is read with it taken out. Knowledge-query and meta turns waive the
    grounding block the rule sits in, yet the base still names the test."""
    prompt = _render(key)
    bare = prompt.replace(_WORKING_VOCABULARY_RULE, "").replace(
        _WORKING_VOCABULARY_PROPERTY, ""
    )
    shown = [t for t in _NAMED_TERMS if t in bare]
    # The open-case prompts show one (the matrix bites); terminal ones none.
    assert bool(shown) == (key in _OPEN)
    if shown:
        assert prompt.count(_WORKING_VOCABULARY_PROPERTY) == 1
        full = prompt.count(_WORKING_VOCABULARY_RULE)
        assert full == (0 if key.startswith("fallback:") else 1)


@pytest.mark.parametrize("key", ["main:diagnosis/triage", "fallback:diagnosis"])
def test_the_held_open_withdrawal_reaches_only_prompts_with_the_rule(key):
    """The fallback has no ROOT-CAUSE TEST block, but the previous turn's
    held-open withdrawal names the test (critique 1(b), amendment 4)."""
    prompt = _render(key)
    assert "ROOT-CAUSE TEST, clause b" in prompt
    assert _WORKING_VOCABULARY_PROPERTY in prompt


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
