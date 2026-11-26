"""The outcome rule line and the ruled prompt wording (#2811 S2c-3b).

Every investigating prompt (each stage, and the knowledge-query and meta
turns) carries the ruled rule line in its cached prefix; INQUIRY, the fallback
stub and TERMINAL carry none. The prompts no longer teach the outcome phrases
the owner ruled out, and each reworded line renders as ruled. All through
``get_prompt_for_case``, the path a turn takes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.prompts.templates.assembly import (
    get_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.blocks import (
    _OUTCOME_FIELDS_RULE,
)
from faultmaven.infrastructure.llm.prompt_cache import CACHE_BOUNDARY
from faultmaven.modules.case.contracts import Case, CaseState, InquiryData
from faultmaven.modules.case.domain.models.progress import MitigationRecord
from faultmaven.utils.token_estimation import estimate_tokens
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN

pytestmark = pytest.mark.unit

#: The ruled wording (#2811 comment 7118574166, Q1 (a); widened by #3046
#: comment 7161149979, Q3 (a), and by #3053 comment 7175936827, Q8), verbatim.
RULE = (
    "State that the problem is confirmed, that a root cause is identified, or "
    "that a fix or a mitigation worked only through the structured fields; the "
    "engine states what the case record holds under your reply."
)
_MSG = "what next?"


def _case(state: CaseState = CaseState.INVESTIGATING, stage: str = "diagnosis"):
    case = Case(
        case_id="case_2811c3b00001",
        title="API timeouts",
        description="Upstream timeouts since 10:00",
        user_id="user_2811",
        enterprise_id="org_2811",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            proposed_problem_statement="API timeouts",
        ),
        current_turn=3,
    )
    if stage == "mitigation":
        case.progress.mitigation = MitigationRecord(proposed_at_turn=2, accepted=True)
    elif stage == "treatment":
        case.progress.solution_accepted = True
    if state == CaseState.INQUIRY:
        return case.model_copy(update={"state": state, "inquiry": InquiryData()})
    if state != CaseState.INVESTIGATING:
        stamp = "resolved_at" if state == CaseState.RESOLVED else "closed_at"
        return case.model_copy(
            update={"state": state, stamp: datetime.now(timezone.utc)}
        )
    return case


#: Each investigating turn the rule reaches: (stage, processing mode, the
#: text that shows the turn took that dispatch).
_CARRIES = {
    "diagnosis": ("diagnosis", None, "**FOCUS: DIAGNOSIS**"),
    "mitigation": ("mitigation", None, "**FOCUS: MITIGATION**"),
    "treatment": ("treatment", None, "**FOCUS: TREATMENT**"),
    "knowledge_query": ("diagnosis", "knowledge_query", "GENERAL KNOWLEDGE"),
    "agent_meta": ("diagnosis", "agent_meta", "QUESTION ABOUT FAULTMAVEN ITSELF"),
}


def _investigating(column: str) -> str:
    stage, mode, marker = _CARRIES[column]
    prompt = get_prompt_for_case(
        _case(stage=stage), _MSG, processing_mode=mode, given_tools=DEFAULT_GIVEN
    )
    assert marker in prompt, f"{column} did not dispatch as named"
    return prompt


@pytest.mark.parametrize("column", sorted(_CARRIES))
def test_every_investigating_turn_carries_the_rule_once_in_the_prefix(column):
    prompt = _investigating(column)
    assert prompt.count(RULE) == 1
    # In the cached prefix (#613), above the per-turn tail.
    assert prompt.index(RULE) < prompt.index(CACHE_BOUNDARY)


def _fallback_stub() -> str:
    """The minimal fallback the budget backstop returns in place of the
    assembled prompt when nothing fits."""
    prompt = get_prompt_for_case(
        _case(), _MSG, given_tools=DEFAULT_GIVEN, target_tokens=1
    )
    assert CACHE_BOUNDARY not in prompt, "not the fallback stub"
    return prompt


_EXCLUDED = {
    "inquiry": lambda: get_prompt_for_case(
        _case(CaseState.INQUIRY), _MSG, given_tools=DEFAULT_GIVEN
    ),
    "fallback": _fallback_stub,
    "resolved": lambda: get_prompt_for_case(
        _case(CaseState.RESOLVED), _MSG, given_tools=DEFAULT_GIVEN
    ),
    "closed": lambda: get_prompt_for_case(
        _case(CaseState.CLOSED), _MSG, given_tools=DEFAULT_GIVEN
    ),
}


@pytest.mark.parametrize("column", sorted(_EXCLUDED))
def test_inquiry_the_fallback_stub_and_terminal_carry_no_rule(column):
    prompt = _EXCLUDED[column]()
    assert prompt.strip()
    assert "only through the structured fields" not in prompt


#: (stage, the ruled line as it renders, the text it replaced).
_REWORDED = {
    "treatment_lead_in": (
        "treatment",
        '"Based on the verification evidence, here is what the data shows: …"',
        "the fix appears to have resolved",
    ),
    "treatment_confident_enough": (
        "treatment",
        "statement the engine then has to append a correction beneath.\n"
        "  CORRECT: prose describing what the new data shows + evidence_to_add",
        "confident enough to write that the fix worked",
    ),
    "treatment_user_intent_example": (
        "treatment",
        "\"Thanks — that's the result we needed. I'll propose marking this "
        'resolved. One click to confirm."',
        "Sounds like the fix held",
    ),
    "treatment_revert_workaround": (
        "treatment",
        '"Now that the permanent fix is in place, you should [revert the '
        'temporary workaround]"',
        "Now that the root cause is fixed",
    ),
    "treatment_substantiate": (
        "treatment",
        "4. **Substantiate When Evidence Confirms**\n"
        "   - When fix results match expectations, connect the data to the fix "
        '("The error rate returned to baseline after the config change.") and '
        "record the conclusion through the structured fields; the engine states "
        "what the case record holds.",
        "which confirms that",
    ),
    "diagnosis_substantiate": (
        "diagnosis",
        '"[evidence] supports the theory that [hypothesis]."',
        "This confirms what we suspected",
    ),
    "advisor_no_premature_resolution": (
        "diagnosis",
        "Never state that a problem is resolved or fixed\n"
        "without verification evidence",
        "root-caused",
    ),
    # #3053 S3d-4 (D1): the MITIGATION prompt no longer hands the model a
    # sentence saying the mitigation worked.
    "mitigation_transition_back": (
        "mitigation",
        "   - Say what comes next, the root-cause work, without saying the mitigation\n"
        "     worked; the engine states what the case record holds under your reply.",
        "things are stabilizing",
    ),
    "grounding_never_state": (
        "treatment",
        "- NEVER state that a problem is resolved or fixed without\n"
        "  verification evidence",
        "root-caused",
    ),
}


def test_the_rule_is_the_blocks_text_at_its_measured_cost():
    """The rule as the template carries it, and its cost in the cached prefix
    (``estimate_tokens``): 37 before #3053 S3d-4 added "or a mitigation", 40
    after. ``lifecycle-metrics.md`` states the prompt figure, "about 46"."""
    assert _OUTCOME_FIELDS_RULE == RULE
    assert estimate_tokens(RULE) == 40


@pytest.mark.parametrize("name", sorted(_REWORDED))
def test_each_reworded_line_renders_as_ruled_and_the_old_text_is_gone(name):
    stage, ruled, old = _REWORDED[name]
    prompt = _investigating(stage)
    assert ruled in prompt
    # The old text is gone from every dispatched prompt, not only this one.
    for column in _CARRIES:
        assert old not in _investigating(column), (column, old)
