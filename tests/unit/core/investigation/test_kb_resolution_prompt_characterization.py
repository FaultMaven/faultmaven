"""Characterization tests for the KB-resolution prompt contract.

The DIAGNOSIS-stage ``KNOWLEDGE & RUNBOOK AUTHORITY`` block instructs the LLM to
collapse a *matched runbook Cause* into a *single hypothesis* and skip
independent hypothesis generation. That "one matched runbook → one flat
hypothesis" mapping is the **flag-off default** and remains the fallback for
prose-only sources.

When the KB cause seeder (``FAULTMAVEN_KB_CAUSE_SEEDER``) is enabled AND has
seeded candidate causes into a case, ``_select_diagnosis_block`` appends the
seeded-candidate override, which SUPERSEDES the flat "create hypotheses_to_add"
mapping: the structure already exists in the graph, so the LLM validates/refutes
it against evidence instead of re-deriving it from prose.

These tests pin both contracts (base + seeded override) as LLM-agnostic
string-presence assertions — they assert the load-bearing pieces are PRESENT,
not that the prompt is *effective* (an eval concern).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from faultmaven.config.settings import get_settings
from faultmaven.core.investigation.kb_cause_seeder import (
    SeededRunbook,
    seed_candidate_causes,
)
from faultmaven.core.investigation.prompts.templates import (
    _KB_SEEDED_AUTHORITY_OVERRIDE,
    _RCA_DIAGNOSIS_BLOCK,
    _select_diagnosis_block,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    InquiryData,
    InvestigationStage,
    ProblemVerification,
)


@pytest.mark.unit
class TestKBResolutionPromptContract:
    """Pin the flag-off runbook-Cause → single-hypothesis attribution contract."""

    def test_knowledge_authority_section_present(self):
        assert "KNOWLEDGE & RUNBOOK AUTHORITY" in _RCA_DIAGNOSIS_BLOCK
        assert "Cause attribution" in _RCA_DIAGNOSIS_BLOCK

    def test_single_match_maps_runbook_cause_to_one_hypothesis(self):
        # Flag-off default: exactly-one-match becomes the hypothesis, and
        # independent hypothesis generation is skipped. (The seeder override
        # supersedes this when enabled — see TestSeededCandidateOverride.)
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
        organization_id="o",
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


def _cause() -> dict:
    return {
        "cause_letter": "A",
        "cause_name": "Cause A",
        "cause_statement": "cause A symptom-level statement",
        "chain_nodes": [
            {"ref": "root", "node_type": "root", "statement": "root A fault"},
            {"ref": "D", "node_type": "problem", "statement": "X is failing"},
        ],
        "chain_edges": [{"cause_ref": "root", "effect_ref": "D"}],
        "rung_indicators": {"root": ["indicator"]},
        "interventions": [],
        "is_fallback_cause": False,
    }


@pytest.mark.unit
class TestSeededCandidateOverride:
    """The seeded-candidate override supersedes the flat mapping when enabled."""

    def test_override_block_frames_seeds_as_priors_to_test(self):
        block = _KB_SEEDED_AUTHORITY_OVERRIDE
        assert "priors to test" in block
        assert "Do NOT emit a `hypotheses_to_add`" in block
        assert "hypothesis_evidence_links" in block
        assert "REFUTE" in block
        # Anti-crowd-out: keep forming independent hypotheses.
        assert "independent hypotheses" in block

    def test_override_appended_when_flag_on_and_candidates_seeded(self, monkeypatch):
        monkeypatch.setattr(get_settings().features, "kb_cause_seeder_enabled", True)
        case = _diagnosis_case()
        report = seed_candidate_causes(
            case, [SeededRunbook("rb1", 0.9, [_cause()])], current_turn=1
        )
        assert report.seeded_anything
        block = _select_diagnosis_block(case)
        assert _KB_SEEDED_AUTHORITY_OVERRIDE in block

    def test_override_absent_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(get_settings().features, "kb_cause_seeder_enabled", False)
        case = _diagnosis_case()
        seed_candidate_causes(
            case, [SeededRunbook("rb1", 0.9, [_cause()])], current_turn=1
        )
        block = _select_diagnosis_block(case)
        assert _KB_SEEDED_AUTHORITY_OVERRIDE not in block

    def test_override_absent_when_flag_on_but_no_seeds(self, monkeypatch):
        # Flag on but no runbook matched → no candidates → no override (the prompt
        # must not claim candidates exist when none do).
        monkeypatch.setattr(get_settings().features, "kb_cause_seeder_enabled", True)
        case = _diagnosis_case()
        block = _select_diagnosis_block(case)
        assert _KB_SEEDED_AUTHORITY_OVERRIDE not in block
