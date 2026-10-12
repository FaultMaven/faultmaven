"""Characterization tests for the KB-resolution prompt contract.

The DIAGNOSIS-stage ``KNOWLEDGE & RUNBOOK AUTHORITY`` block instructs the LLM to
collapse a *matched runbook Cause* into a *single hypothesis* and skip
independent hypothesis generation. That "one matched runbook → one flat
hypothesis" mapping is the contract for every matched runbook.

These tests pin that contract as LLM-agnostic string-presence assertions — they assert the load-bearing pieces are PRESENT,
not that the prompt is *effective* (an eval concern).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from faultmaven.core.investigation.prompts.templates.assembly import (
    _select_diagnosis_block,
)
from faultmaven.core.investigation.prompts.templates.diagnosis import (
    _CHAIN_EMISSION_BLOCK,
    rca_diagnosis_block,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    InquiryData,
    InvestigationStage,
    ProblemVerification,
)
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN


@pytest.mark.unit
class TestKBResolutionPromptContract:
    """Pin the runbook-Cause → single-hypothesis attribution contract."""

    def test_knowledge_authority_section_present(self):
        assert "KNOWLEDGE & RUNBOOK AUTHORITY" in rca_diagnosis_block(DEFAULT_GIVEN)
        assert "Cause attribution" in rca_diagnosis_block(DEFAULT_GIVEN)

    def test_single_match_maps_runbook_cause_to_one_hypothesis(self):
        # Exactly-one-match becomes the hypothesis, and independent hypothesis
        # generation is skipped.
        assert "Exactly one Cause matches:" in rca_diagnosis_block(DEFAULT_GIVEN)
        assert "that Cause IS your hypothesis" in rca_diagnosis_block(DEFAULT_GIVEN)
        assert "hypotheses_to_add" in rca_diagnosis_block(DEFAULT_GIVEN)
        assert "Skip independent hypothesis" in rca_diagnosis_block(DEFAULT_GIVEN)

    def test_multi_and_no_match_branches_present(self):
        # The other two attribution outcomes the current prompt encodes.
        assert "Two or more Causes plausibly match" in rca_diagnosis_block(
            DEFAULT_GIVEN
        )
        assert "disambiguating question" in rca_diagnosis_block(DEFAULT_GIVEN)
        assert "No Cause" in rca_diagnosis_block(DEFAULT_GIVEN)

    def test_knowledge_match_signal_emitted_for_treatment_handoff(self):
        # The attribution persists a knowledge_match signal the TREATMENT-stage
        # KB-RESOLUTION variant reads back (Statement/mechanism direct-copy).
        assert "knowledge_match" in rca_diagnosis_block(DEFAULT_GIVEN)
        assert "match_likelihood" in rca_diagnosis_block(DEFAULT_GIVEN)
        assert "root_cause_conclusion" in rca_diagnosis_block(DEFAULT_GIVEN)


def _diagnosis_case() -> Case:
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_stage=InvestigationStage.DIAGNOSIS,
        inquiry=InquiryData(
            proposed_problem_statement="X fails",
            problem_statement_confirmed=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="X fails", severity=CaseSeverity.HIGH
        ),
        current_turn=1,
    )


@pytest.mark.unit
def test_every_diagnosing_turn_gets_the_flat_matched_cause_directive():
    """One directive for every case: nothing writes seeded candidates, so the
    flat "matched Cause IS your hypothesis" directive is never swapped out."""
    block = _select_diagnosis_block(_diagnosis_case(), DEFAULT_GIVEN)
    assert "that Cause IS your hypothesis" in block
    assert block == rca_diagnosis_block(DEFAULT_GIVEN) + _CHAIN_EMISSION_BLOCK
