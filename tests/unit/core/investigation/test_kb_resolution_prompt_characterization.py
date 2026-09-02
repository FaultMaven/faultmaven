"""Characterization tests for the KB-resolution prompt contract.

The DIAGNOSIS-stage ``KNOWLEDGE & RUNBOOK AUTHORITY`` block instructs the LLM to
collapse a *matched runbook Cause* into a *single hypothesis* and skip
independent hypothesis generation. That "one matched runbook → one flat
hypothesis" mapping is the contract for every matched runbook (the
seeded-candidate override that once superseded it went with the KB cause
seeder, fm#1295).

These tests pin that contract as LLM-agnostic string-presence assertions — they assert the load-bearing pieces are PRESENT,
not that the prompt is *effective* (an eval concern).
"""

from __future__ import annotations

import pytest

from faultmaven.core.investigation.prompts.templates import _RCA_DIAGNOSIS_BLOCK


@pytest.mark.unit
class TestKBResolutionPromptContract:
    """Pin the runbook-Cause → single-hypothesis attribution contract."""

    def test_knowledge_authority_section_present(self):
        assert "KNOWLEDGE & RUNBOOK AUTHORITY" in _RCA_DIAGNOSIS_BLOCK
        assert "Cause attribution" in _RCA_DIAGNOSIS_BLOCK

    def test_single_match_maps_runbook_cause_to_one_hypothesis(self):
        # Exactly-one-match becomes the hypothesis, and independent hypothesis
        # generation is skipped. (A seeded-candidate override that superseded
        # this existed with the KB cause seeder; both went in fm#1295.)
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
