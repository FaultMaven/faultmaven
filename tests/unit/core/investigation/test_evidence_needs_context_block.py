"""Phase 4 tests: ``<evidence_needs>`` context-builder block.

Verifies the demand-side pool rendering rules from
``docs/architecture/investigation-engine/evidence-needs-design.md`` §6.1
and §8.4:

- Progressive activation (design §10.6): empty pool → ``""``
- Default filter (DIAGNOSIS): PENDING + PARTIALLY_MET only; FULFILLED
  and SUPERSEDED excluded.
- MITIGATION/TREATMENT stage exception: FULFILLED also surfaces as a
  re-verification checklist. SUPERSEDED remains excluded.
- Symptom needs render ``motivated_by: problem_statement``; causal
  needs render ``motivated_by: [hyp_*, ...]``.
- High-priority needs render first (presentation preference, not a
  correctness rule).
- Block is omitted entirely outside INVESTIGATING (terminal/inquiry
  have their own surfaces).
- The ``{evidence_needs}`` ctx key is always present so the
  INVESTIGATION_BASE template can reference it unconditionally.

Run:
    pytest tests/unit/core/investigation/test_evidence_needs_context_block.py -v
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    _build_evidence_needs_block,
    build_investigation_context,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseStatus,
    EvidenceNeed,
    InquiryData,
    InvestigationPath,
    InvestigationStage,
    NeedPriority,
    NeedPurpose,
    NeedStatus,
    PathSelection,
)

# ============================================================
# Fixtures
# ============================================================


def _make_case(
    *,
    status: CaseStatus = CaseStatus.INVESTIGATING,
    stage: InvestigationStage = InvestigationStage.DIAGNOSIS,
    path: InvestigationPath = InvestigationPath.ROOT_CAUSE,
) -> Case:
    inquiry = InquiryData()
    inquiry.proposed_problem_statement = "Test problem"
    inquiry.problem_statement_confirmed = True
    inquiry.decided_to_investigate = True

    path_selection = None
    if status == CaseStatus.INVESTIGATING:
        path_selection = PathSelection(
            path=path,
            auto_selected=False,
            rationale="test fixture",
            selected_by="user_test",
        )

    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_test",
        organization_id="org_test",
        title="Test case",
        description="Test problem",
        status=status,
        inquiry=inquiry,
        path_selection=path_selection,
    )
    case.current_turn = 5
    if status == CaseStatus.INVESTIGATING:
        case.progress.symptom_verified = True
        if stage == InvestigationStage.MITIGATION:
            case.progress.mitigation_accepted = True
        elif stage == InvestigationStage.TREATMENT:
            case.progress.solution_accepted = True
    return case


def _make_need(
    case: Case,
    *,
    purpose: NeedPurpose = NeedPurpose.SYMPTOM_VERIFICATION,
    status: NeedStatus = NeedStatus.PENDING,
    priority: NeedPriority = NeedPriority.MEDIUM,
    request_text: str = "kubectl get pods",
    rationale: str = "confirm symptom",
    motivating_hypothesis_ids: list[str] | None = None,
    fulfilling_evidence_ids: list[str] | None = None,
    superseded_reason: str | None = None,
) -> EvidenceNeed:
    if status == NeedStatus.SUPERSEDED and superseded_reason is None:
        superseded_reason = "stale"
    # FULFILLED needs require a non-empty fulfillment list per model
    # invariant — synthesize one if the caller didn't provide it.
    if status == NeedStatus.FULFILLED and not fulfilling_evidence_ids:
        fulfilling_evidence_ids = [f"ev_{uuid4().hex[:12]}"]
    need = EvidenceNeed(
        case_id=case.case_id,
        purpose=purpose,
        request_text=request_text,
        rationale=rationale,
        priority=priority,
        status=status,
        motivating_hypothesis_ids=motivating_hypothesis_ids or [],
        fulfilling_evidence_ids=fulfilling_evidence_ids or [],
        superseded_reason=superseded_reason,
        created_at_turn=case.current_turn,
    )
    case.evidence_needs.append(need)
    return need


# ============================================================
# Progressive activation
# ============================================================


@pytest.mark.unit
class TestProgressiveActivation:
    def test_empty_pool_renders_empty_string(self):
        case = _make_case()
        assert _build_evidence_needs_block(case) == ""

    def test_non_investigating_case_renders_empty_string(self):
        case = _make_case(status=CaseStatus.INQUIRY)
        # Even with a need attached, INQUIRY case suppresses the block.
        _make_need(case)
        assert _build_evidence_needs_block(case) == ""

    def test_only_superseded_renders_empty_string(self):
        case = _make_case()
        _make_need(case, status=NeedStatus.SUPERSEDED)
        assert _build_evidence_needs_block(case) == ""

    def test_only_fulfilled_in_diagnosis_renders_empty_string(self):
        """FULFILLED is excluded by default in DIAGNOSIS — pool with only
        fulfilled needs surfaces no block."""
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        _make_need(case, status=NeedStatus.FULFILLED)
        assert _build_evidence_needs_block(case) == ""


# ============================================================
# Default DIAGNOSIS filter
# ============================================================


@pytest.mark.unit
class TestDiagnosisStageFilter:
    def test_pending_need_renders(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        need = _make_need(case, status=NeedStatus.PENDING)
        out = _build_evidence_needs_block(case)
        assert "<evidence_needs>" in out
        assert "</evidence_needs>" in out
        assert need.need_id in out

    def test_partially_met_need_renders(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        need = _make_need(case, status=NeedStatus.PARTIALLY_MET)
        out = _build_evidence_needs_block(case)
        assert need.need_id in out
        # Non-PENDING status is surfaced in the header line
        assert "PARTIALLY_MET" in out

    def test_fulfilled_excluded_in_diagnosis(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        pending = _make_need(case, status=NeedStatus.PENDING)
        fulfilled = _make_need(case, status=NeedStatus.FULFILLED)
        out = _build_evidence_needs_block(case)
        assert pending.need_id in out
        assert fulfilled.need_id not in out

    def test_superseded_excluded_in_diagnosis(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        pending = _make_need(case, status=NeedStatus.PENDING)
        superseded = _make_need(case, status=NeedStatus.SUPERSEDED)
        out = _build_evidence_needs_block(case)
        assert pending.need_id in out
        assert superseded.need_id not in out


# ============================================================
# MITIGATION / TREATMENT re-verification exception
# ============================================================


@pytest.mark.unit
class TestPostDiagnosisReVerificationException:
    """During MITIGATION and TREATMENT, FULFILLED needs surface as a
    re-verification checklist (design §8.4). SUPERSEDED remains
    excluded across all stages."""

    def test_fulfilled_surfaces_in_mitigation(self):
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
            path=InvestigationPath.MITIGATION_FIRST,
        )
        fulfilled = _make_need(case, status=NeedStatus.FULFILLED)
        out = _build_evidence_needs_block(case)
        assert fulfilled.need_id in out
        assert "FULFILLED" in out

    def test_fulfilled_surfaces_in_treatment(self):
        case = _make_case(stage=InvestigationStage.TREATMENT)
        fulfilled = _make_need(case, status=NeedStatus.FULFILLED)
        out = _build_evidence_needs_block(case)
        assert fulfilled.need_id in out

    def test_superseded_still_excluded_in_mitigation(self):
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
            path=InvestigationPath.MITIGATION_FIRST,
        )
        superseded = _make_need(case, status=NeedStatus.SUPERSEDED)
        pending = _make_need(case, status=NeedStatus.PENDING)
        out = _build_evidence_needs_block(case)
        assert superseded.need_id not in out
        assert pending.need_id in out


# ============================================================
# Per-need formatting
# ============================================================


@pytest.mark.unit
class TestNeedFormatting:
    def test_symptom_need_motivated_by_problem_statement(self):
        case = _make_case()
        _make_need(
            case,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            motivating_hypothesis_ids=[],
        )
        out = _build_evidence_needs_block(case)
        assert "motivated_by: problem_statement" in out
        assert "SYMPTOM" in out

    def test_causal_need_lists_motivating_hypotheses(self):
        case = _make_case()
        _make_need(
            case,
            purpose=NeedPurpose.CAUSAL_VERIFICATION,
            motivating_hypothesis_ids=["hyp_aaaa11112222", "hyp_bbbb33334444"],
        )
        out = _build_evidence_needs_block(case)
        assert "motivated_by: [hyp_aaaa11112222, hyp_bbbb33334444]" in out
        assert "CAUSAL" in out

    def test_header_includes_priority_label(self):
        case = _make_case()
        _make_need(case, priority=NeedPriority.HIGH)
        out = _build_evidence_needs_block(case)
        assert "HIGH" in out

    def test_pending_status_not_repeated_in_header(self):
        """The header reads cleaner without ``, PENDING`` since that's
        the default status; non-default statuses (PARTIALLY_MET,
        FULFILLED) are explicit."""
        case = _make_case()
        _make_need(case, status=NeedStatus.PENDING)
        out = _build_evidence_needs_block(case)
        assert "PENDING" not in out


# ============================================================
# Ordering — HIGH priority first
# ============================================================


@pytest.mark.unit
class TestPriorityOrdering:
    def test_high_priority_rendered_before_low(self):
        case = _make_case()
        low = _make_need(case, priority=NeedPriority.LOW, request_text="low")
        high = _make_need(case, priority=NeedPriority.HIGH, request_text="high")
        medium = _make_need(case, priority=NeedPriority.MEDIUM, request_text="medium")
        out = _build_evidence_needs_block(case)
        idx_high = out.index(high.need_id)
        idx_medium = out.index(medium.need_id)
        idx_low = out.index(low.need_id)
        assert idx_high < idx_medium < idx_low


# ============================================================
# Cap + overflow note
# ============================================================


@pytest.mark.unit
class TestRenderCap:
    def test_overflow_marker_appears_when_pool_exceeds_cap(self):
        from faultmaven.core.investigation.prompts.context_builder import (
            _EVIDENCE_NEEDS_RENDER_CAP,
        )

        case = _make_case()
        for i in range(_EVIDENCE_NEEDS_RENDER_CAP + 3):
            _make_need(case, request_text=f"need {i}")
        out = _build_evidence_needs_block(case)
        assert "3 more open need(s) not shown" in out

    def test_no_overflow_marker_when_within_cap(self):
        case = _make_case()
        _make_need(case)
        _make_need(case)
        out = _build_evidence_needs_block(case)
        assert "more open need(s) not shown" not in out


# ============================================================
# ctx integration
# ============================================================


@pytest.mark.unit
class TestContextBuilderIntegration:
    """The ``evidence_needs`` key must always be present in the ctx
    returned by ``build_investigation_context`` so the
    ``INVESTIGATION_BASE`` template can reference it unconditionally."""

    def test_key_present_when_pool_empty(self):
        case = _make_case()
        ctx = build_investigation_context(case, "user message", max_tokens=8000)
        assert "evidence_needs" in ctx
        assert ctx["evidence_needs"] == ""

    def test_key_present_with_visible_needs(self):
        case = _make_case()
        need = _make_need(case)
        ctx = build_investigation_context(case, "user message", max_tokens=8000)
        assert need.need_id in ctx["evidence_needs"]

    def test_key_present_for_non_investigating_case(self):
        """Even outside INVESTIGATING the key must be present so the
        template format call doesn't KeyError."""
        case = _make_case(status=CaseStatus.INQUIRY)
        ctx = build_investigation_context(case, "user message", max_tokens=8000)
        assert "evidence_needs" in ctx
        assert ctx["evidence_needs"] == ""
