"""Characterization tests for the KB-resolution prompt contract.

The DIAGNOSIS-stage ``KNOWLEDGE & RUNBOOK AUTHORITY`` block instructs the LLM to
collapse a *matched runbook Cause* into a *single hypothesis* and skip
independent hypothesis generation. That "one matched runbook → one flat
hypothesis" mapping is the **flag-off default** and remains the fallback for
prose-only sources.

When the KB cause seeder has seeded candidate causes into a case (the graph
holds them — the flag only decides whether NEW seeds are minted),
``_select_diagnosis_block`` applies the seeded-candidate override, which SUPERSEDES the flat "create hypotheses_to_add"
mapping: the structure already exists in the graph, so the LLM validates/refutes
it against evidence instead of re-deriving it from prose.

These tests pin both contracts (base + seeded override) as LLM-agnostic
string-presence assertions — they assert the load-bearing pieces are PRESENT,
not that the prompt is *effective* (an eval concern).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from faultmaven.core.investigation.kb_cause_seeder import (
    SeededRunbook,
    seed_candidate_causes,
)
from faultmaven.core.investigation.prompts.templates import (
    _KB_MATCHED_CAUSE_FLAT,
    _KB_MATCHED_CAUSE_SEEDED,
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


def _force_seeder_flag(monkeypatch, enabled: bool) -> None:
    """Force the KB-seeder feature flag on/off robustly.

    ``_select_diagnosis_block`` reads
    ``get_settings().features.kb_cause_seeder_enabled``. ``get_settings`` is a
    module-global singleton that another test in the suite may rebuild via
    ``reset_settings()``; a plain ``monkeypatch.setattr(get_settings().features,
    ...)`` then patches an instance the code no longer reads. Pin ``get_settings``
    to one settings object (with the flag set) so the value the code reads is
    stable regardless of singleton resets mid-suite.
    """
    from faultmaven.config import settings as settings_mod

    s = settings_mod.get_settings()
    monkeypatch.setattr(s.features, "kb_cause_seeder_enabled", enabled)
    monkeypatch.setattr(settings_mod, "get_settings", lambda: s)


@pytest.mark.unit
class TestSeededCandidateDirectiveSwap:
    """A seeded turn REPLACES the flat directive with the validate/refute one —
    a single coherent instruction, never two contradictory ones."""

    def test_flat_directive_is_sliced_verbatim_from_the_block(self):
        # Drift guard: the sliced flat directive must exist in the block exactly,
        # or the runtime replace would silently no-op. Anchors changing raises at
        # import; this pins the content too.
        assert _KB_MATCHED_CAUSE_FLAT
        assert _KB_MATCHED_CAUSE_FLAT in _RCA_DIAGNOSIS_BLOCK
        assert "hypotheses_to_add" in _KB_MATCHED_CAUSE_FLAT

    def test_seeded_directive_frames_priors_and_forbids_recreation(self):
        block = _KB_MATCHED_CAUSE_SEEDED
        assert "prior" in block and "TEST" in block
        assert "Do NOT create a `hypotheses_to_add`" in block
        assert "hypothesis_evidence_links" in block
        # Preserves the TREATMENT handoff.
        assert "knowledge_match" in block
        # Anti-crowd-out: keep forming your own hypotheses.
        assert "your own hypotheses" in block

    def test_seeded_turn_replaces_flat_with_seeded_directive(self, monkeypatch):
        _force_seeder_flag(monkeypatch, True)
        case = _diagnosis_case()
        report = seed_candidate_causes(
            case, [SeededRunbook("rb1", 0.9, [_cause()])], current_turn=1
        )
        assert report.seeded_anything
        block = _select_diagnosis_block(case)
        # Seeded directive present; flat directive GONE — no contradiction.
        assert _KB_MATCHED_CAUSE_SEEDED in block
        assert _KB_MATCHED_CAUSE_FLAT not in block
        assert "that Cause IS your hypothesis" not in block

    def test_flag_off_with_persisted_seeds_still_gets_seeded_directive(
        self, monkeypatch
    ):
        """The swap keys on graph state, not on the flag.

        Seeds persist. A case seeded while the flag was on still holds them
        after fm#1295 turned the default off, and the causal-graph block still
        renders them — so it must get the validate/refute directive, not the
        flat "that Cause IS your hypothesis" one on top of a graph that already
        holds the seed.
        """
        _force_seeder_flag(monkeypatch, False)
        case = _diagnosis_case()
        seed_candidate_causes(
            case, [SeededRunbook("rb1", 0.9, [_cause()])], current_turn=1
        )
        block = _select_diagnosis_block(case)
        assert _KB_MATCHED_CAUSE_SEEDED in block
        assert _KB_MATCHED_CAUSE_FLAT not in block

    def test_flag_off_no_seeds_keeps_flat_directive(self, monkeypatch):
        # The production default after fm#1295: nothing seeds, flat path.
        _force_seeder_flag(monkeypatch, False)
        case = _diagnosis_case()
        block = _select_diagnosis_block(case)
        assert _KB_MATCHED_CAUSE_FLAT in block
        assert _KB_MATCHED_CAUSE_SEEDED not in block

    def test_flag_on_no_seeds_keeps_flat_directive(self, monkeypatch):
        # Flag on but no runbook matched → no candidates → flat directive stays
        # (the prompt must not claim candidates exist when none do).
        _force_seeder_flag(monkeypatch, True)
        case = _diagnosis_case()
        block = _select_diagnosis_block(case)
        assert _KB_MATCHED_CAUSE_FLAT in block
        assert _KB_MATCHED_CAUSE_SEEDED not in block
