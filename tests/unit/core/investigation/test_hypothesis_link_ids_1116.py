"""Two apply-layer consequences of rendering ``[hyp_...]`` ids (#1116 review).

1. **Bracket/whitespace normalisation.** The prompt renders ids in square
   brackets and tells the model to use them. ``_resolve_id_ref`` did no
   normalisation, so a bracket-echoed ref missed ``case.hypotheses`` and the
   link or update was dropped with only a log line — the outcome the id
   rendering set out to fix. Evidence refs render the same way and get the
   same treatment.

2. **Terminal-state guard on the link path.** The causal-graph block renders
   REFUTED hypotheses (so the model does not re-create them), and the link
   path had no terminal guard: one SUPPORTS link lifted a refuted hypothesis
   from 0.0 to 0.35 and reset its progress counter. The guard mirrors the one
   ``_apply_hypothesis_updates`` already had, feedback included.

Run:
    pytest tests/unit/core/investigation/test_hypothesis_link_ids_1116.py -v
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from faultmaven.core.investigation.hypothesis_manager import HypothesisManager
from faultmaven.core.investigation.milestone_engine import MilestoneEngine
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


def _case() -> Case:
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


def _hypothesis(
    case: Case, *, state=HypothesisState.ACTIVE, likelihood=0.6
) -> Hypothesis:
    h = Hypothesis(
        hypothesis_id=f"hyp_{uuid4().hex[:12]}",
        statement="Test hypothesis",
        category=HypothesisCategory.DATABASE,
        state=state,
        likelihood=likelihood,
        initial_likelihood=likelihood,
        generated_at_turn=1,
        last_updated_turn=1,
        last_progress_at_turn=1,
        iterations_without_progress=2,
        generation_mode=HypothesisGenerationMode.SYSTEMATIC,
        rationale="test",
        refutation_reason="disproved" if state == HypothesisState.REFUTED else None,
    )
    case.hypotheses[h.hypothesis_id] = h
    return h


def _evidence(case: Case) -> Evidence:
    ev = Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        primary_purpose="cause_identified",
        summary="mount at 100%",
        extract="/dev/sda1 100% /var/run/libvirt",
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_aabb12345678",
        collected_by="user_test",
        collected_at_turn=case.current_turn,
    )
    case.evidence.append(ev)
    return ev


def _link(h_ref: str, e_ref: str, stance=EvidenceStance.SUPPORTS):
    return SimpleNamespace(
        hypothesis_id_ref=h_ref,
        evidence_id_ref=e_ref,
        stance=stance,
        reasoning="the mount is the mechanism",
        stance_confidence=0.9,
    )


def _engine() -> MilestoneEngine:
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng.hypothesis_manager = HypothesisManager()
    return eng


@pytest.mark.unit
class TestIdRefNormalisation:
    @pytest.mark.parametrize(
        "raw",
        [
            "hyp_abc123abc123",
            "[hyp_abc123abc123]",
            " hyp_abc123abc123 ",
            "[ hyp_abc123abc123 ]",
        ],
    )
    def test_real_id_survives_brackets_and_whitespace(self, raw):
        assert _engine()._resolve_id_ref(raw, [], "hyp") == "hyp_abc123abc123"

    @pytest.mark.parametrize("raw", ["new_index_0", "[new_index_0]", " new_index_0"])
    def test_placeholder_resolves_through_the_same_normalisation(self, raw):
        assert (
            _engine()._resolve_id_ref(raw, ["hyp_created0000"], "hyp")
            == "hyp_created0000"
        )

    def test_unresolvable_placeholder_still_returns_a_probeable_value(self):
        """Callers probe ``startswith("new_index_")`` on the return; the
        normalisation must not break that contract."""
        out = _engine()._resolve_id_ref("[new_index_7]", ["hyp_created0000"], "hyp")
        assert out.startswith("new_index_")

    def test_none_and_empty_pass_through(self):
        eng = _engine()
        assert eng._resolve_id_ref(None, [], "hyp") is None
        assert eng._resolve_id_ref("", [], "hyp") == ""

    def test_bracketed_refs_link_end_to_end(self):
        """The prompt's exact rendering, echoed back on both refs, lands."""
        case = _case()
        h = _hypothesis(case)
        ev = _evidence(case)
        metadata: dict = {}

        _engine()._apply_hypothesis_evidence_links(
            case, [_link(f"[{h.hypothesis_id}]", f"[{ev.evidence_id}]")], metadata
        )

        assert [link.evidence_id for link in h.evidence_links] == [ev.evidence_id]
        assert metadata["hypothesis_evidence_links_applied"] == 1


@pytest.mark.unit
class TestTerminalHypothesisRefusesLinks:
    @pytest.mark.parametrize(
        "state", [HypothesisState.REFUTED, HypothesisState.RETIRED]
    )
    def test_link_on_terminal_hypothesis_is_refused_and_surfaced(self, state):
        case = _case()
        h = _hypothesis(case, state=state, likelihood=0.0)
        ev = _evidence(case)
        metadata: dict = {}

        _engine()._apply_hypothesis_evidence_links(
            case, [_link(h.hypothesis_id, ev.evidence_id)], metadata
        )

        assert h.evidence_links == []
        assert h.likelihood == 0.0
        assert h.state == state
        assert h.last_updated_turn == 1
        assert h.last_progress_at_turn == 1
        assert h.iterations_without_progress == 2
        assert "hypothesis_evidence_links_applied" not in metadata
        assert h.hypothesis_id in metadata["system_feedback"]
        assert "terminal" in metadata["system_feedback"]
        assert "NEW hypothesis" in metadata["system_feedback"]

    def test_active_hypothesis_in_the_same_batch_still_links(self):
        """The guard skips the one link, not the batch."""
        case = _case()
        dead = _hypothesis(case, state=HypothesisState.REFUTED, likelihood=0.0)
        live = _hypothesis(case)
        ev = _evidence(case)
        metadata: dict = {}

        _engine()._apply_hypothesis_evidence_links(
            case,
            [
                _link(dead.hypothesis_id, ev.evidence_id),
                _link(live.hypothesis_id, ev.evidence_id),
            ],
            metadata,
        )

        assert dead.evidence_links == []
        assert [link.evidence_id for link in live.evidence_links] == [ev.evidence_id]
        assert live.likelihood == pytest.approx(0.75)
        assert metadata["hypothesis_evidence_links_applied"] == 1
