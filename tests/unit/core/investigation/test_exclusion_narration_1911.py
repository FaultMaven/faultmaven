"""Narration follows the record (#1911, plan items 2-4).

The engine refuses a refutation with no data behind it
(``test_refutation_needs_data_1911.py``). What the model says about the
alternatives is a prompt matter, so these pins hold the instructions and the
context the model reads:

- the investigation base states what a valid ground is, names the
  isolation-to-one-scope anti-pattern, and says how to answer a user who
  questions an exclusion;
- the context states plainly what the record excludes, in EVERY stage: the
  condensed stage blocks that replace the causal graph used to drop refuted
  hypotheses, and the charter-1 challenge happened in TREATMENT.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.cause_assurance import (
    FIX_SUCCEEDED,
    latest_decided_fix_window,
)
from faultmaven.core.investigation.prompts.context_builder import (
    assembly as context_assembly,
)
from faultmaven.core.investigation.prompts.context_builder import (
    causal_graph_block,
)
from faultmaven.core.investigation.prompts.context_builder.assembly import (
    build_investigation_context,
)
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    MAX_NAMED_EXCLUSIONS,
    _build_causal_graph_block,
    exclusions_reminder,
)
from faultmaven.core.investigation.prompts.templates.blocks import (
    _EXCLUSION_GROUNDS_BLOCK,
)
from faultmaven.core.investigation.prompts.templates.diagnosis import (
    _CHAIN_EMISSION_BLOCK,
    _RCA_DIAGNOSIS_BLOCK,
)
from faultmaven.core.investigation.prompts.templates.fallback import (
    get_fallback_prompt_for_case,
)
from faultmaven.core.investigation.prompts.templates.investigation import (
    INVESTIGATION_BASE,
)
from faultmaven.core.investigation.prompts.templates.treatment import (
    TREATMENT_INSTRUCTIONS,
)
from faultmaven.infrastructure.llm.prompt_cache import CACHE_BOUNDARY
from faultmaven.modules.case.contracts import InvestigationActionType, ProblemStatus
from faultmaven.modules.case.domain.models.case import Case
from faultmaven.modules.case.domain.models.hypothesis import (
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
)
from faultmaven.modules.case.domain.models.lifecycle import CaseState
from faultmaven.modules.case.domain.models.problem import (
    InquiryData,
    InvestigationStage,
    ProblemVerification,
)
from faultmaven.modules.case.domain.models.progress import MitigationRecord
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _accept,
    _cause_gone,
    _identified_case,
    _problem_gone,
)

pytestmark = pytest.mark.unit

_CAUSE = "hyp_aaaaaaaaaaaa"
_KEY = "hyp_bbbbbbbbbbbb"


def _case(stage=InvestigationStage.DIAGNOSIS, *, key_refuted: bool) -> Case:
    now = datetime.now(UTC)
    case = Case(
        user_id="u",
        enterprise_id="o",
        title="t",
        description="a third of logins fail with 401",
        state=CaseState.INVESTIGATING,
        current_turn=3,
        problem_verification=ProblemVerification(
            symptom_statement="a third of logins fail with 401", severity="high"
        ),
        inquiry=InquiryData(
            proposed_problem_statement="a third of logins fail with 401",
            problem_statement_confirmed=True,
            problem_statement_confirmed_at=now,
            inquiry_turns=1,
        ),
    )
    if stage == InvestigationStage.MITIGATION:
        case.progress.mitigation = MitigationRecord(proposed_at_turn=3, accepted=True)
    elif stage == InvestigationStage.TREATMENT:
        case.progress.solution_accepted = True
    assert case.current_stage == stage
    for h_id, statement, state, reason in (
        (_CAUSE, "app-02's clock runs 247 s fast", HypothesisState.VALIDATED, None),
        (
            _KEY,
            "app-02 signs with a stale signing key",
            HypothesisState.REFUTED if key_refuted else HypothesisState.ACTIVE,
            (
                "the gateway rejected with a time check, after the signature verified"
                if key_refuted
                else None
            ),
        ),
    ):
        case.hypotheses[h_id] = Hypothesis(
            hypothesis_id=h_id,
            statement=statement,
            category=HypothesisCategory.ENVIRONMENT,
            state=state,
            refutation_reason=reason,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="r",
            likelihood=0.0 if state == HypothesisState.REFUTED else 0.9,
            initial_likelihood=0.5,
            generated_at_turn=1,
        )
    return case


def _working(case: Case) -> str:
    ctx = build_investigation_context(case, "what rules out the alternatives?")
    corpus = "\n".join(str(v) for v in ctx.values())
    start = corpus.find("<working_hypotheses>")
    assert start >= 0, "the condensed block is the one this stage renders"
    return corpus[start : corpus.find("</working_hypotheses>")]


# ---------------------------------------------------------------------------
# The record, rendered
# ---------------------------------------------------------------------------


def test_the_graph_says_when_nothing_is_excluded_on_record():
    block = _build_causal_graph_block(_case(key_refuted=False))
    assert "Ruled out on record: nothing." in block


def test_the_graph_names_what_is_excluded_on_record():
    block = _build_causal_graph_block(_case(key_refuted=True))
    assert f"Ruled out on record: {_KEY}" in block
    assert "No other alternative is excluded on record." in block
    assert "Refuted because: the gateway rejected with a time check" in block


@pytest.mark.parametrize(
    "stage", [InvestigationStage.TREATMENT, InvestigationStage.MITIGATION]
)
def test_a_condensed_stage_block_keeps_the_exclusions(stage):
    """The charter-1 challenge came after the fix, in TREATMENT, whose block
    shows only the validated cause. The refuted competitor and the summary
    must survive the condensing, or the answer is re-derived from scratch."""
    working = _working(_case(stage, key_refuted=True))
    assert f"[{_KEY}] app-02 signs with a stale signing key (REFUTED)" in working
    assert "Refuted because: the gateway rejected with a time check" in working
    assert f"Ruled out on record: {_KEY}" in working


def test_a_condensed_stage_block_says_when_nothing_is_excluded():
    working = _working(_case(InvestigationStage.TREATMENT, key_refuted=False))
    assert "Ruled out on record: nothing." in working


# ---------------------------------------------------------------------------
# The instructions
# ---------------------------------------------------------------------------


def test_the_investigation_base_carries_the_exclusion_rules():
    assert _EXCLUSION_GROUNDS_BLOCK in INVESTIGATION_BASE


def test_a_ground_is_an_observation_the_competitor_could_not_produce():
    assert "only by an observation its own chain could" in _EXCLUSION_GROUNDS_BLOCK
    assert "not produce" in _EXCLUSION_GROUNDS_BLOCK


def test_isolation_to_one_scope_is_named_as_no_ground():
    text = " ".join(_EXCLUSION_GROUNDS_BLOCK.split())
    assert (
        "Isolation to one scope (one host, one region, one tenant, one version) "
        "never excludes a cause local to that scope" in text
    )
    assert '"Every node runs the same code and config" excludes nothing' in text


def test_a_competitor_the_reply_rules_out_is_recorded_that_turn():
    text = " ".join(_EXCLUSION_GROUNDS_BLOCK.split())
    assert "Record every competitor your reply rules out, in the same turn" in text
    assert "new_index_N" in text
    assert "A row already on record counts" in text


def test_ruled_out_is_said_only_of_the_record():
    text = " ".join(_EXCLUSION_GROUNDS_BLOCK.split())
    assert 'Say "ruled out" only of a cause refuted on record' in text
    assert "was not what caused this problem, never that it is absent" in text
    assert "say it is not excluded and name the check that would settle it" in text


def test_a_repeated_challenge_changes_the_approach():
    text = " ".join(_EXCLUSION_GROUNDS_BLOCK.split())
    assert "restating the same answer will not settle it" in text
    assert "Ask what would settle it for them" in text
    assert "do not propose resolved on the evidence already on record" in text


def test_the_diagnosis_block_ties_refuted_to_a_linked_row():
    text = " ".join(_RCA_DIAGNOSIS_BLOCK.split())
    assert "The reason is not the disproof" in text
    assert "A competitor your reply rules out is recorded in the same turn" in text


def test_signature_screening_records_a_screened_cause_the_reply_names():
    """F3 stays silent for a cause the reply never mentions, so the map is not
    flooded; one the reply names as ruled out is recorded, on its row."""
    text = " ".join(_CHAIN_EMISSION_BLOCK.split())
    assert "Screening is silent" in text
    assert "One your reply DOES name as ruled out is recorded refuted" in text


def test_the_treatment_block_scopes_the_no_more_data_line():
    text = " ".join(TREATMENT_INSTRUCTIONS.split())
    assert "On the turn you propose resolved, do not suggest additional" in text
    assert "A user who declines because they question the cause" in text
    assert "The engine accepts it once the case records the failure" in text


# ---------------------------------------------------------------------------
# The record restated beside the question (live run, 2026-10-11)
# ---------------------------------------------------------------------------


def _assembled(case: Case) -> str:
    from faultmaven.core.investigation.prompts.templates.assembly import (
        get_prompt_for_case,
    )

    return get_prompt_for_case(
        case, "What rules out the alternatives?", provider_name="openai"
    )


def test_the_record_is_restated_just_before_the_users_message():
    """A live login-401 run had the rule and the record in its prompt and still
    answered with exclusions nothing backed. The fact is repeated in the slot
    read last, between the conversation and the user's message."""
    prompt = _assembled(_case(InvestigationStage.TREATMENT, key_refuted=False))
    reminder = prompt.find("<exclusions_on_record>")
    # In the per-turn tail (#613), after the conversation, before the message.
    assert reminder > prompt.find(CACHE_BOUNDARY) > 0
    assert reminder > prompt.find("CONVERSATION HISTORY:") > 0
    assert reminder < prompt.rfind("What rules out the alternatives?")
    block = prompt[reminder : prompt.find("</exclusions_on_record>")]
    assert "Ruled out on record: nothing." in block
    assert 'Say "ruled out" only of a cause refuted on record' in block


def test_no_reminder_before_there_is_a_hypothesis():
    case = _case(key_refuted=False)
    case.hypotheses.clear()
    assert "<exclusions_on_record>" not in _assembled(case)


@pytest.mark.parametrize("state", [CaseState.INQUIRY, CaseState.RESOLVED])
def test_no_reminder_outside_investigating(state):
    case = _case(key_refuted=True).model_copy(
        update={"state": state, "resolved_at": datetime.now(UTC)}
    )
    assert exclusions_reminder(case) == ""


def test_the_feedback_cap_never_cuts_the_reminder(monkeypatch):
    """The cap keeps the head of the feedback. Under it, the reminder was the
    first thing lost, and a cut could leave its tag open against the user's
    message. It is appended after the cap."""
    monkeypatch.setattr(
        context_assembly, "_cap_text_tokens", lambda text, *a, **k: text[:10]
    )
    prompt = _assembled(_case(InvestigationStage.TREATMENT, key_refuted=False))
    block = prompt[prompt.find("<exclusions_on_record>") :]
    assert "Ruled out on record: nothing." in block
    assert "</exclusions_on_record>" in block


def test_the_fallback_prompt_keeps_the_reminder_as_a_heading():
    """The degraded prompt fires on oversized prompts, where the rule is
    easiest to lose. Its trust rule reads an unfenced tag as quoted data, so
    the reminder is a bare heading there, like the prompt's other lines."""
    prompt = get_fallback_prompt_for_case(
        _case(key_refuted=True), "What rules out the alternatives?"
    )
    assert "<exclusions_on_record>" not in prompt
    assert f"EXCLUSIONS ON RECORD: Ruled out on record: {_KEY}" in prompt


def test_the_fallback_shows_every_refuted_hypothesis_it_names():
    """The fallback lists three hypotheses; a refuted one past them would be
    named by id alone, with no statement anywhere in the prompt."""
    case = _case(key_refuted=False)
    for i in range(4):
        h_id = f"hyp_00000000000{i}"
        case.hypotheses[h_id] = case.hypotheses[_CAUSE].model_copy(
            update={"hypothesis_id": h_id, "statement": f"standing cause {i}"}
        )
    # Re-inserted last, past the three the fallback lists by insertion order.
    case.hypotheses[_KEY] = case.hypotheses.pop(_KEY).model_copy(
        update={
            "state": HypothesisState.REFUTED,
            "refutation_reason": "a time check ran after the signature verified",
            "likelihood": 0.0,
        }
    )
    prompt = get_fallback_prompt_for_case(case, "What rules out the alternatives?")
    assert f"[{_KEY}] app-02 signs with a stale signing key (refuted)" in prompt


def test_a_feedback_cut_mid_tag_does_not_swallow_the_reminder(monkeypatch):
    """The reminder is its own section, so the per-section tag termination
    closes a feedback the cap left inside a tag before the reminder starts."""
    monkeypatch.setattr(
        context_assembly,
        "system_feedback_block",
        lambda case, **k: 'Refused hypothesis <hyp statement="pool exhausted',
    )
    prompt = _assembled(_case(InvestigationStage.TREATMENT, key_refuted=False))
    feedback = prompt.find('statement="pool exhausted')
    reminder = prompt.find("<exclusions_on_record>")
    assert 0 < feedback < reminder
    between = prompt[feedback:reminder]
    assert '"' in between[len('statement="pool exhausted') :]  # quote closed
    assert ">" in between  # tag closed before the reminder


def test_the_named_ids_are_capped():
    """The reminder sits in the never-trimmed reserve, so it must not grow
    with the case."""
    case = _case(key_refuted=True)
    template = case.hypotheses[_KEY]
    for i in range(MAX_NAMED_EXCLUSIONS + 2):
        h_id = f"hyp_0000000000{i:02d}"
        case.hypotheses[h_id] = template.model_copy(update={"hypothesis_id": h_id})
    reminder = exclusions_reminder(case)
    assert reminder.count("hyp_") == MAX_NAMED_EXCLUSIONS
    assert "and 3 more" in reminder


def test_a_false_alarm_hold_keeps_only_the_narration_rule():
    """While the problem is held as a false alarm, a refutation would be
    refused (INV-46): the reminder asks for no checks and no refutation."""
    case = _case(key_refuted=False)
    case.progress.problem_status = ProblemStatus.INVALIDATED
    reminder = " ".join(exclusions_reminder(case).split())
    assert 'Say "ruled out" only of a cause refuted on record.' in reminder
    assert "name the check" not in reminder
    assert "state=REFUTED" not in reminder


def test_after_a_confirmed_fix_the_rule_is_the_counterfactual():
    """A real confirmed fix (the cause and then the problem observed gone after
    a dated fix): asking for checks on a competitor would pull against the
    resolve turn. The counterfactual is stated once, as the rule."""
    case, _root, _hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [_cause_gone(9), _problem_gone(10)]
    assert latest_decided_fix_window(case).state == FIX_SUCCEEDED

    reminder = " ".join(exclusions_reminder(case).split())
    assert "was not what caused this problem, never that it is absent" in reminder
    assert "name the check that would settle it" not in reminder
    assert reminder.count("not what caused this problem") == 1
