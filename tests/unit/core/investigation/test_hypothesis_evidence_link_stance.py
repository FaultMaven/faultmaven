"""Evidence-link stance fidelity (#514, #521).

- ``_apply_hypothesis_evidence_links`` carries the LLM-emitted stance
  through verbatim — a NEUTRAL link attaches for the audit trail without
  any likelihood effect, instead of collapsing to a -0.20 REFUTES
  penalty (#514).
- The likelihood delta per stance is exactly the documented formula
  (+0.15 SUPPORTS, -0.20 REFUTES, 0 NEUTRAL), swept across the whole
  ``EvidenceStance`` enum so a new member fails loudly.
- Prompt templates never use the legacy ``CONTRADICTS`` stance label —
  the enum is supports / neutral / refutes (#521).

Run:
    pytest tests/unit/core/investigation/test_hypothesis_evidence_link_stance.py -v
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
from faultmaven.core.investigation.prompts import templates
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
)

# The documented likelihood formula (update_likelihood_from_evidence):
# +0.15 per SUPPORTS link, -0.20 per REFUTES link, NEUTRAL inert.
EXPECTED_DELTA = {
    EvidenceStance.SUPPORTS: +0.15,
    EvidenceStance.NEUTRAL: 0.0,
    EvidenceStance.REFUTES: -0.20,
}


# ============================================================
# Fixtures
# ============================================================


def _make_case() -> Case:
    inquiry = InquiryData()
    inquiry.proposed_problem_statement = "Test problem"
    inquiry.problem_statement_confirmed = True
    inquiry.decided_to_investigate = True

    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_test",
        enterprise_id="org_test",
        title="Test case",
        description="Test problem",
        state=CaseState.INVESTIGATING,
        inquiry=inquiry,
    )
    case.current_turn = 5
    case.progress.symptom_verified = True
    return case


def _make_hypothesis(case: Case) -> Hypothesis:
    h = Hypothesis(
        hypothesis_id=f"hyp_{uuid4().hex[:12]}",
        statement="Test hypothesis",
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        likelihood=0.6,
        initial_likelihood=0.6,
        generated_at_turn=case.current_turn,
        last_updated_turn=case.current_turn,
        last_progress_at_turn=case.current_turn,
        iterations_without_progress=0,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        rationale="test",
    )
    case.hypotheses[h.hypothesis_id] = h
    return h


def _make_evidence(case: Case) -> Evidence:
    ev = Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="symptom_verified",
        summary="test symptom",
        extract="error log line",
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_aabb12345678",
        collected_by="user_test",
        collected_at_turn=case.current_turn,
    )
    case.evidence.append(ev)
    return ev


def _make_link(hypothesis_id: str, evidence_id: str, stance: EvidenceStance):
    """SimpleNamespace mimicking HypothesisEvidenceLinkToAdd — the apply
    layer only reads attributes, no instance check."""
    return SimpleNamespace(
        hypothesis_id_ref=hypothesis_id,
        evidence_id_ref=evidence_id,
        stance=stance,
        reasoning="test link",
        stance_confidence=0.9,
    )


def _make_engine() -> MilestoneEngine:
    """Bare engine — only the apply helper is exercised."""
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.hypothesis_manager = HypothesisManager()
    return eng


# ============================================================
# #514 — stance carried verbatim through the apply layer
# ============================================================


@pytest.mark.unit
class TestApplyLayerStanceFidelity:
    def test_expected_delta_covers_whole_enum(self):
        """A new EvidenceStance member must fail this sweep loudly."""
        assert set(EXPECTED_DELTA) == set(EvidenceStance)

    @pytest.mark.parametrize("stance", list(EvidenceStance))
    def test_emitted_stance_stored_verbatim(self, stance):
        case = _make_case()
        h = _make_hypothesis(case)
        ev = _make_evidence(case)
        engine = _make_engine()

        engine._apply_hypothesis_evidence_links(
            case, [_make_link(h.hypothesis_id, ev.evidence_id, stance)], {}
        )

        assert len(h.evidence_links) == 1
        assert h.evidence_links[0].stance == stance
        assert h.evidence_links[0].stance_confidence == 0.9
        assert h.evidence_links[0].reasoning == "test link"

    @pytest.mark.parametrize("stance", list(EvidenceStance))
    def test_likelihood_delta_matches_formula(self, stance):
        case = _make_case()
        h = _make_hypothesis(case)
        ev = _make_evidence(case)
        engine = _make_engine()

        engine._apply_hypothesis_evidence_links(
            case, [_make_link(h.hypothesis_id, ev.evidence_id, stance)], {}
        )

        assert h.likelihood == pytest.approx(0.6 + EXPECTED_DELTA[stance])

    def test_metadata_counter_increments_per_applied_link(self):
        case = _make_case()
        h = _make_hypothesis(case)
        ev1 = _make_evidence(case)
        ev2 = _make_evidence(case)
        engine = _make_engine()
        meta: dict = {}

        engine._apply_hypothesis_evidence_links(
            case,
            [
                _make_link(h.hypothesis_id, ev1.evidence_id, EvidenceStance.NEUTRAL),
                _make_link(h.hypothesis_id, ev2.evidence_id, EvidenceStance.SUPPORTS),
            ],
            meta,
        )

        assert meta["hypothesis_evidence_links_applied"] == 2

    def test_unknown_hypothesis_ref_skipped(self):
        case = _make_case()
        ev = _make_evidence(case)
        engine = _make_engine()
        meta: dict = {}

        engine._apply_hypothesis_evidence_links(
            case,
            [_make_link("hyp_missing000000", ev.evidence_id, EvidenceStance.SUPPORTS)],
            meta,
        )

        assert meta.get("hypothesis_evidence_links_applied", 0) == 0

    def test_unknown_evidence_ref_skipped(self):
        case = _make_case()
        h = _make_hypothesis(case)
        engine = _make_engine()
        meta: dict = {}

        engine._apply_hypothesis_evidence_links(
            case,
            [_make_link(h.hypothesis_id, "ev_missing0000000", EvidenceStance.SUPPORTS)],
            meta,
        )

        assert h.evidence_links == []
        assert meta.get("hypothesis_evidence_links_applied", 0) == 0

    def test_same_turn_new_index_refs_resolve(self):
        case = _make_case()
        h = _make_hypothesis(case)
        ev = _make_evidence(case)
        engine = _make_engine()
        meta = {
            "hypotheses_generated": [h.hypothesis_id],
            "evidence_added": [ev.evidence_id],
        }

        engine._apply_hypothesis_evidence_links(
            case,
            [_make_link("new_index_0", "new_index_0", EvidenceStance.NEUTRAL)],
            meta,
        )

        assert len(h.evidence_links) == 1
        assert h.evidence_links[0].evidence_id == ev.evidence_id
        assert h.evidence_links[0].stance == EvidenceStance.NEUTRAL

    def test_relink_stance_revision_recomputes_likelihood(self):
        """Re-linking the same evidence upserts the link, so a
        SUPPORTS→NEUTRAL revision removes the earlier +0.15 rather than
        retaining it — the likelihood recompute must stay unconditional
        even though NEUTRAL itself is inert."""
        case = _make_case()
        h = _make_hypothesis(case)
        ev = _make_evidence(case)
        engine = _make_engine()

        engine._apply_hypothesis_evidence_links(
            case,
            [_make_link(h.hypothesis_id, ev.evidence_id, EvidenceStance.SUPPORTS)],
            {},
        )
        assert h.likelihood == pytest.approx(0.75)

        engine._apply_hypothesis_evidence_links(
            case,
            [_make_link(h.hypothesis_id, ev.evidence_id, EvidenceStance.NEUTRAL)],
            {},
        )

        assert len(h.evidence_links) == 1
        assert h.evidence_links[0].stance == EvidenceStance.NEUTRAL
        assert h.likelihood == pytest.approx(0.6)


# ============================================================
# #521 — no legacy CONTRADICTS stance label in prompt templates
# ============================================================


@pytest.mark.unit
class TestNoLegacyStanceLabelInPrompts:
    def test_templates_never_use_contradicts_label(self):
        """The stance vocabulary shown to the LLM must match the
        EvidenceStance enum (supports/neutral/refutes). CONTRADICTS is
        not a member and misleads stance emission."""
        src = inspect.getsource(templates)
        assert "CONTRADICTS" not in src
