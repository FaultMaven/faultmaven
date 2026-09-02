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
    _build_causal_graph_block,
    build_investigation_context,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    ProblemVerification,
)


def _case_with_hypotheses(*ids: str) -> Case:
    now = datetime.now(UTC)
    case = Case(
        user_id="u",
        organization_id="o",
        title="t",
        description="VM fails to start because libvirt cannot write its PID file",
        state=CaseState.INVESTIGATING,
        current_turn=3,
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
    for i, hid in enumerate(ids):
        case.hypotheses[hid] = Hypothesis(
            hypothesis_id=hid,
            statement=f"cause number {i}",
            category=HypothesisCategory.ENVIRONMENT,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="r",
            likelihood=0.3,
            initial_likelihood=0.3,
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
