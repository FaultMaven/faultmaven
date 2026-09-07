"""The prompt must show each hypothesis's id (#1116 follow-up).

``hypothesis_evidence_links.hypothesis_id_ref`` and
``hypotheses_to_update.hypothesis_id`` accept an existing ``hyp_...`` id or a
same-turn ``new_index_N``. Until this change no prompt renderer emitted the id
— hypotheses appeared as statement + confidence only — so the model could link
evidence only to a hypothesis it created that same turn. Replaying
case_bf484a484a77 turn 9 through the engine: 0 of 65 reps linked a causal row;
prefixing the statements with their ids linked on the existing hypotheses.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    STATE_SUMMARY_TURN_THRESHOLD,
    _build_causal_graph_block,
    _build_state_summary,
    build_investigation_context,
)
from faultmaven.core.investigation.prompts.templates import (
    get_fallback_prompt_for_case,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    InvestigationStage,
    MitigationRecord,
    ProblemVerification,
)


def _case_with_hypotheses(
    *ids: str,
    current_turn: int = 3,
    stage: InvestigationStage = InvestigationStage.DIAGNOSIS,
    likelihoods: tuple[float, ...] = (),
    states: tuple[HypothesisState, ...] = (),
) -> Case:
    now = datetime.now(UTC)
    case = Case(
        user_id="u",
        enterprise_id="o",
        title="t",
        description="VM fails to start because libvirt cannot write its PID file",
        state=CaseState.INVESTIGATING,
        current_turn=current_turn,
        problem_verification=ProblemVerification(
            symptom_statement="VM fails to start", severity="high"
        ),
        inquiry=InquiryData(
            proposed_problem_statement="VM fails to start",
            problem_statement_confirmed=True,
            problem_statement_confirmed_at=now,
            decided_to_investigate=True,
            decision_made_at=now,
            inquiry_turns=1,
        ),
    )
    # ``current_stage`` is derived from the gate milestones, not settable.
    if stage == InvestigationStage.MITIGATION:
        case.progress.mitigation = MitigationRecord(
            proposed_at_turn=current_turn, accepted=True
        )
    elif stage == InvestigationStage.TREATMENT:
        case.progress.solution_accepted = True
    assert case.current_stage == stage
    for i, hid in enumerate(ids):
        likelihood = likelihoods[i] if i < len(likelihoods) else 0.3
        state = states[i] if i < len(states) else HypothesisState.ACTIVE
        case.hypotheses[hid] = Hypothesis(
            hypothesis_id=hid,
            statement=f"cause number {i}",
            category=HypothesisCategory.ENVIRONMENT,
            state=state,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="r",
            likelihood=likelihood,
            initial_likelihood=likelihood,
            generated_at_turn=1,
            last_updated_turn=1,
            last_progress_at_turn=1,
            iterations_without_progress=0,
        )
    return case


@pytest.mark.unit
def test_causal_graph_block_prefixes_each_hypothesis_with_its_id():
    case = _case_with_hypotheses("hyp_aaaaaaaaaaaa", "hyp_bbbbbbbbbbbb")
    block = _build_causal_graph_block(case)
    assert "- [hyp_aaaaaaaaaaaa] cause number 0" in block
    assert "- [hyp_bbbbbbbbbbbb] cause number 1" in block
    # The instruction that tells the model what the id is FOR travels with it.
    assert "hypothesis_evidence_links" in block


@pytest.mark.unit
def test_assembled_diagnosis_context_carries_every_active_hypothesis_id():
    case = _case_with_hypotheses("hyp_cccccccccccc", "hyp_dddddddddddd")
    ctx = build_investigation_context(case, "what next?", provider_name="openai")
    corpus = "\n".join(str(v) for v in ctx.values())
    assert corpus.count("[hyp_cccccccccccc]") >= 1
    assert corpus.count("[hyp_dddddddddddd]") >= 1


def _corpus(case: Case) -> str:
    ctx = build_investigation_context(case, "what next?", provider_name="openai")
    return "\n".join(str(v) for v in ctx.values())


# The three ``<working_hypotheses>`` branches of ``build_investigation_context``
# each render their own line. A mutation run against the first revision of this
# file showed stripping the id from all three left the suite green: the
# assembled-context test above runs at turn 3 with no stage, so only the
# causal-graph renderer was covered.


@pytest.mark.unit
def test_long_diagnosis_case_carries_every_active_id_not_just_the_top_three():
    """Past ``STATE_SUMMARY_TURN_THRESHOLD`` the causal-graph block gives way
    to a top-3 list plus the state summary. Rank 4 must still have its id
    somewhere, or its causal evidence can only be linked via a same-turn
    ``new_index_N`` — the #1116 failure recurring on exactly the long cases."""
    ids = (
        "hyp_111111111111",
        "hyp_222222222222",
        "hyp_333333333333",
        "hyp_444444444444",
    )
    case = _case_with_hypotheses(
        *ids,
        current_turn=STATE_SUMMARY_TURN_THRESHOLD + 5,
        likelihoods=(0.9, 0.7, 0.5, 0.3),
    )
    corpus = _corpus(case)
    for hid in ids:
        assert f"[{hid}]" in corpus, hid
    # The top-3 working list carries ids too (the DIAGNOSIS state-summary branch).
    working = corpus[
        corpus.find("<working_hypotheses>") : corpus.find("</working_hypotheses>")
    ]
    assert "[hyp_111111111111]" in working
    assert "[hyp_333333333333]" in working
    assert "[hyp_444444444444]" not in working  # rank 4 lives in the summary


@pytest.mark.unit
def test_state_summary_lists_every_active_hypothesis_with_its_id():
    ids = (
        "hyp_111111111111",
        "hyp_222222222222",
        "hyp_333333333333",
        "hyp_444444444444",
    )
    case = _case_with_hypotheses(
        *ids,
        likelihoods=(0.9, 0.7, 0.5, 0.3),
        states=(HypothesisState.VALIDATED,) + (HypothesisState.ACTIVE,) * 3,
    )
    summary = _build_state_summary(case)
    for hid in ids:
        assert f"- [{hid}] cause number" in summary, hid
    assert "[VALIDATED]" in summary
    # The summary says what the id is for, since the causal-graph header is
    # absent in this mode.
    assert "hypothesis_evidence_links" in summary


@pytest.mark.unit
def test_mitigation_branch_prefixes_ids():
    case = _case_with_hypotheses(
        "hyp_aaaaaaaaaaaa",
        "hyp_bbbbbbbbbbbb",
        stage=InvestigationStage.MITIGATION,
        states=(HypothesisState.VALIDATED, HypothesisState.ACTIVE),
    )
    corpus = _corpus(case)
    working = corpus[
        corpus.find("<working_hypotheses>") : corpus.find("</working_hypotheses>")
    ]
    assert "- [hyp_aaaaaaaaaaaa] cause number 0" in working
    assert "- [hyp_bbbbbbbbbbbb] cause number 1" in working


@pytest.mark.unit
def test_treatment_branch_prefixes_the_validated_id():
    case = _case_with_hypotheses(
        "hyp_aaaaaaaaaaaa",
        "hyp_bbbbbbbbbbbb",
        stage=InvestigationStage.TREATMENT,
        likelihoods=(0.9, 0.4),
        states=(HypothesisState.VALIDATED, HypothesisState.ACTIVE),
    )
    corpus = _corpus(case)
    working = corpus[
        corpus.find("<working_hypotheses>") : corpus.find("</working_hypotheses>")
    ]
    assert "- [hyp_aaaaaaaaaaaa] cause number 0" in working
    assert "VALIDATED" in working


@pytest.mark.unit
def test_fallback_prompt_prefixes_ids():
    """The degraded renderer (context-length rejection, starvation, hard-ceiling
    overflow) answers against the same schema, so it needs the ids as well —
    and overflow reaches it on the long cases that have standing hypotheses."""
    case = _case_with_hypotheses("hyp_aaaaaaaaaaaa", "hyp_bbbbbbbbbbbb")
    prompt = get_fallback_prompt_for_case(case, "what next?")
    assert "[hyp_aaaaaaaaaaaa] cause number 0 (active)" in prompt
    assert "[hyp_bbbbbbbbbbbb] cause number 1 (active)" in prompt


@pytest.mark.unit
def test_causal_graph_header_is_one_clause():
    """The instruction travels with the ids but costs one clause, not three."""
    case = _case_with_hypotheses("hyp_aaaaaaaaaaaa")
    block = _build_causal_graph_block(case)
    assert "Reference a hypothesis by its [hyp_...] id" in block
    assert "instead of creating a duplicate" not in block
