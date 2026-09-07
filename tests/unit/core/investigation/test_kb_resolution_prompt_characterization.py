"""Characterization tests for the KB-resolution prompt contract.

The DIAGNOSIS-stage ``KNOWLEDGE & RUNBOOK AUTHORITY`` block instructs the LLM to
collapse a *matched runbook Cause* into a *single hypothesis* and skip
independent hypothesis generation. That "one matched runbook → one flat
hypothesis" mapping is the contract for every matched runbook. A
seeded-candidate override supersedes it only for LEGACY cases whose graph still
holds candidates the removed KB cause seeder planted (``seeded_provenance``,
fm#1295): the structure already exists there, so the model validates/refutes it
instead of re-creating it beside the seed.

These tests pin that contract as LLM-agnostic string-presence assertions — they assert the load-bearing pieces are PRESENT,
not that the prompt is *effective* (an eval concern).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from faultmaven.core.investigation.prompts.templates import (
    _KB_MATCHED_CAUSE_FLAT,
    _KB_MATCHED_CAUSE_SEEDED,
    _RCA_DIAGNOSIS_BLOCK,
    _select_diagnosis_block,
)
from faultmaven.core.investigation.seeded_provenance import SEEDED_FROM_RUNBOOK_KEY
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalNode,
    InquiryData,
    InvestigationStage,
    NodeType,
    ProblemVerification,
)


@pytest.mark.unit
class TestKBResolutionPromptContract:
    """Pin the runbook-Cause → single-hypothesis attribution contract."""

    def test_knowledge_authority_section_present(self):
        assert "KNOWLEDGE & RUNBOOK AUTHORITY" in _RCA_DIAGNOSIS_BLOCK
        assert "Cause attribution" in _RCA_DIAGNOSIS_BLOCK

    def test_single_match_maps_runbook_cause_to_one_hypothesis(self):
        # Exactly-one-match becomes the hypothesis, and independent hypothesis
        # generation is skipped. (The seeded-candidate override below supersedes
        # this only for legacy cases that still carry seeds — see
        # ``seeded_provenance``.)
        assert "Exactly one Cause matches:" in _RCA_DIAGNOSIS_BLOCK
        assert "that Cause IS your hypothesis" in _RCA_DIAGNOSIS_BLOCK
        assert "hypotheses_to_add" in _RCA_DIAGNOSIS_BLOCK
        assert "Skip independent hypothesis" in _RCA_DIAGNOSIS_BLOCK

    def test_multi_and_no_match_branches_present(self):
        # The other two attribution outcomes the current prompt encodes.
        assert "Two or more Causes plausibly match" in _RCA_DIAGNOSIS_BLOCK
        assert "disambiguating question" in _RCA_DIAGNOSIS_BLOCK
        assert "No Cause" in _RCA_DIAGNOSIS_BLOCK

    def test_knowledge_match_signal_emitted_for_treatment_handoff(self):
        # The attribution persists a knowledge_match signal the TREATMENT-stage
        # KB-RESOLUTION variant reads back (Statement/mechanism direct-copy).
        assert "knowledge_match" in _RCA_DIAGNOSIS_BLOCK
        assert "match_likelihood" in _RCA_DIAGNOSIS_BLOCK
        assert "root_cause_conclusion" in _RCA_DIAGNOSIS_BLOCK


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
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="X fails", severity=CaseSeverity.HIGH
        ),
        current_turn=1,
    )


def _plant_legacy_seed(case: Case) -> None:
    """A CANDIDATE root the removed seeder would have written, marker and all."""
    node = CausalNode(
        statement="root A fault",
        node_type=NodeType.ROOT,
        generated_at_turn=1,
        metadata={SEEDED_FROM_RUNBOOK_KEY: "rb1"},
    )
    case.causal_nodes[node.node_id] = node


@pytest.mark.unit
class TestSeededCandidateDirectiveSwap:
    """A legacy seeded case REPLACES the flat directive with the validate/refute
    one — a single coherent instruction, never two contradictory ones."""

    def test_flat_directive_is_sliced_verbatim_from_the_block(self):
        assert _KB_MATCHED_CAUSE_FLAT in _RCA_DIAGNOSIS_BLOCK
        assert "that Cause IS your hypothesis" in _KB_MATCHED_CAUSE_FLAT

    def test_seeded_directive_frames_priors_and_forbids_recreation(self):
        assert "ALREADY in your `<causal_graph>`" in _KB_MATCHED_CAUSE_SEEDED
        assert "Do NOT create a `hypotheses_to_add` record" in _KB_MATCHED_CAUSE_SEEDED
        assert "your own hypotheses" in _KB_MATCHED_CAUSE_SEEDED

    def test_a_legacy_seeded_case_gets_the_seeded_directive(self):
        case = _diagnosis_case()
        _plant_legacy_seed(case)
        block = _select_diagnosis_block(case)
        assert _KB_MATCHED_CAUSE_SEEDED in block
        assert _KB_MATCHED_CAUSE_FLAT not in block
        assert "that Cause IS your hypothesis" not in block

    def test_a_case_without_seeds_keeps_the_flat_directive(self):
        # Every case opened after fm#1295: nothing seeds, flat path, byte-identical.
        block = _select_diagnosis_block(_diagnosis_case())
        assert _KB_MATCHED_CAUSE_FLAT in block
        assert _KB_MATCHED_CAUSE_SEEDED not in block
