"""Phase 4 tests: ``<evidence_needs>`` context-builder block.

Verifies the demand-side pool rendering rules from
``docs/architecture/investigation-engine/evidence-needs-design.md`` §6.1
and §8.4:

- Progressive activation (design §10.6): empty pool → ``""``
- Default filter (DIAGNOSIS): PENDING + PARTIALLY_MET needs only;
  FULFILLED and SUPERSEDED excluded.
- MITIGATION/TREATMENT re-verification checklist is built from the
  confirmed presence-evidence rows (symptom_evidence / causal_evidence),
  NOT from FULFILLED needs (needs are gap-rare; evidence rows are
  complete). SUPERSEDED needs remain excluded from the outstanding list.
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

from faultmaven.core.investigation.intake_evaluation import _SURFACED_CAUSAL_CAP
from faultmaven.core.investigation.prompts.context_builder import (
    _build_evidence_needs_block,
    build_investigation_context,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    InquiryData,
    InvestigationStage,
    MitigationRecord,
    NeedPriority,
    NeedPurpose,
    NeedState,
)

# ============================================================
# Fixtures
# ============================================================


def _make_case(
    *,
    state: CaseState = CaseState.INVESTIGATING,
    stage: InvestigationStage = InvestigationStage.DIAGNOSIS,
) -> Case:
    """Build a Case in the unified opportunistic flow (no path fork).

    The display stage (DIAGNOSIS / MITIGATION / TREATMENT) is derived
    from the action-compliance gates: MITIGATION when a mitigation is
    accepted-but-not-verified, TREATMENT when a solution is
    accepted-but-not-verified, else DIAGNOSIS.
    """
    inquiry = InquiryData()
    inquiry.proposed_problem_statement = "Test problem"
    inquiry.problem_statement_confirmed = True
    inquiry.decided_to_investigate = True

    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="user_test",
        organization_id="org_test",
        title="Test case",
        description="Test problem",
        state=state,
        inquiry=inquiry,
    )
    case.current_turn = 5
    if state == CaseState.INVESTIGATING:
        case.progress.symptom_verified = True
        if stage == InvestigationStage.MITIGATION:
            case.progress.mitigation = MitigationRecord(
                proposed_at_turn=case.current_turn, accepted=True
            )
        elif stage == InvestigationStage.TREATMENT:
            case.progress.solution_accepted = True
    return case


def _make_need(
    case: Case,
    *,
    purpose: NeedPurpose = NeedPurpose.SYMPTOM_VERIFICATION,
    state: NeedState = NeedState.PENDING,
    priority: NeedPriority = NeedPriority.MEDIUM,
    request_text: str = "kubectl get pods",
    rationale: str = "confirm symptom",
    motivating_hypothesis_ids: list[str] | None = None,
    fulfilling_evidence_ids: list[str] | None = None,
    superseded_reason: str | None = None,
) -> EvidenceNeed:
    if state == NeedState.SUPERSEDED and superseded_reason is None:
        superseded_reason = "stale"
    # FULFILLED needs require a non-empty fulfillment list per model
    # invariant — synthesize one if the caller didn't provide it.
    if state == NeedState.FULFILLED and not fulfilling_evidence_ids:
        fulfilling_evidence_ids = [f"ev_{uuid4().hex[:12]}"]
    need = EvidenceNeed(
        case_id=case.case_id,
        purpose=purpose,
        request_text=request_text,
        rationale=rationale,
        priority=priority,
        state=state,
        motivating_hypothesis_ids=motivating_hypothesis_ids or [],
        fulfilling_evidence_ids=fulfilling_evidence_ids or [],
        superseded_reason=superseded_reason,
        created_at_turn=case.current_turn,
    )
    case.evidence_needs.append(need)
    return need


def _make_evidence(
    case: Case,
    *,
    category: EvidenceCategory = EvidenceCategory.SYMPTOM_EVIDENCE,
    summary: str = "confirmed symptom finding",
) -> Evidence:
    """Add a confirmed presence-evidence row — the re-verification
    checklist is anchored on these, not on FULFILLED needs."""
    ev = Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        category=category,
        primary_purpose="symptom_verified",
        summary=summary,
        extract="observed signature",
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_aabb12345678",
        collected_by="user_test",
        collected_at_turn=case.current_turn,
    )
    case.evidence.append(ev)
    return ev


# ============================================================
# Progressive activation
# ============================================================


@pytest.mark.unit
class TestProgressiveActivation:
    def test_empty_pool_renders_empty_string(self):
        case = _make_case()
        assert _build_evidence_needs_block(case) == ""

    def test_non_investigating_case_renders_empty_string(self):
        case = _make_case(state=CaseState.INQUIRY)
        # Even with a need attached, INQUIRY case suppresses the block.
        _make_need(case)
        assert _build_evidence_needs_block(case) == ""

    def test_only_superseded_renders_empty_string(self):
        case = _make_case()
        _make_need(case, state=NeedState.SUPERSEDED)
        assert _build_evidence_needs_block(case) == ""

    def test_only_fulfilled_in_diagnosis_renders_empty_string(self):
        """FULFILLED is excluded by default in DIAGNOSIS — pool with only
        fulfilled needs surfaces no block."""
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        _make_need(case, state=NeedState.FULFILLED)
        assert _build_evidence_needs_block(case) == ""


# ============================================================
# Default DIAGNOSIS filter
# ============================================================


@pytest.mark.unit
class TestDiagnosisStageFilter:
    def test_pending_need_renders(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        need = _make_need(case, state=NeedState.PENDING)
        out = _build_evidence_needs_block(case)
        assert "<evidence_needs>" in out
        assert "</evidence_needs>" in out
        assert need.need_id in out

    def test_partially_met_need_renders(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        need = _make_need(case, state=NeedState.PARTIALLY_MET)
        out = _build_evidence_needs_block(case)
        assert need.need_id in out
        # Non-PENDING status is surfaced in the header line
        assert "PARTIALLY_MET" in out

    def test_fulfilled_excluded_in_diagnosis(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        pending = _make_need(case, state=NeedState.PENDING)
        fulfilled = _make_need(case, state=NeedState.FULFILLED)
        out = _build_evidence_needs_block(case)
        assert pending.need_id in out
        assert fulfilled.need_id not in out

    def test_superseded_excluded_in_diagnosis(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        pending = _make_need(case, state=NeedState.PENDING)
        superseded = _make_need(case, state=NeedState.SUPERSEDED)
        out = _build_evidence_needs_block(case)
        assert pending.need_id in out
        assert superseded.need_id not in out


# ============================================================
# MITIGATION / TREATMENT re-verification exception
# ============================================================


@pytest.mark.unit
class TestPostDiagnosisReVerificationException:
    """During MITIGATION and TREATMENT, the re-verification checklist is
    built from the confirmed presence-evidence rows
    (symptom_evidence / causal_evidence) — NOT from FULFILLED needs.

    Evidence rows exist for every confirmed finding; FULFILLED needs are
    gap-conditional and gap-rare, so a need-anchored checklist would omit
    findings confirmed from already-available data (the common case)."""

    def test_presence_evidence_surfaces_in_mitigation(self):
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
        )
        ev = _make_evidence(case, category=EvidenceCategory.SYMPTOM_EVIDENCE)
        out = _build_evidence_needs_block(case)
        assert ev.evidence_id in out
        assert "Re-verification checklist" in out

    def test_causal_evidence_surfaces_in_treatment(self):
        case = _make_case(stage=InvestigationStage.TREATMENT)
        ev = _make_evidence(case, category=EvidenceCategory.CAUSAL_EVIDENCE)
        out = _build_evidence_needs_block(case)
        assert ev.evidence_id in out
        assert "CAUSE" in out

    def test_confirmed_finding_surfaces_without_any_need(self):
        """The decisive case: a cause confirmed from already-available
        data leaves a causal_evidence row but NO need. It must still
        appear on the re-verification checklist."""
        case = _make_case(stage=InvestigationStage.TREATMENT)
        ev = _make_evidence(case, category=EvidenceCategory.CAUSAL_EVIDENCE)
        assert not case.evidence_needs  # no need was ever created
        out = _build_evidence_needs_block(case)
        assert ev.evidence_id in out

    def test_fulfilled_need_does_not_drive_checklist(self):
        """A FULFILLED need with no corresponding evidence row no longer
        produces a re-verification entry (the anchor moved to evidence)."""
        case = _make_case(stage=InvestigationStage.TREATMENT)
        fulfilled = _make_need(case, state=NeedState.FULFILLED)
        out = _build_evidence_needs_block(case)
        # No presence-evidence rows → no re-verification section at all.
        assert "Re-verification checklist" not in out
        assert fulfilled.need_id not in out

    def test_presence_evidence_not_shown_in_diagnosis(self):
        """Re-verification is post-diagnosis only — presence rows do not
        surface as a checklist during DIAGNOSIS."""
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        _make_evidence(case, category=EvidenceCategory.SYMPTOM_EVIDENCE)
        out = _build_evidence_needs_block(case)
        assert "Re-verification checklist" not in out

    def test_superseded_need_still_excluded_in_mitigation(self):
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
        )
        superseded = _make_need(case, state=NeedState.SUPERSEDED)
        pending = _make_need(case, state=NeedState.PENDING)
        out = _build_evidence_needs_block(case)
        assert superseded.need_id not in out
        assert pending.need_id in out  # outstanding-needs section still works


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
        _make_need(case, state=NeedState.PENDING)
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
        assert "3 more outstanding need(s) not shown" in out

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
        case = _make_case(state=CaseState.INQUIRY)
        ctx = build_investigation_context(case, "user message", max_tokens=8000)
        assert "evidence_needs" in ctx
        assert ctx["evidence_needs"] == ""


# ============================================================
# Outstanding vs re-verification section split (review fix #1)
# ============================================================


@pytest.mark.unit
class TestOutstandingVsReVerificationSplit:
    """In MITIGATION/TREATMENT, outstanding (PENDING/PARTIALLY_MET) and
    FULFILLED needs render under separate section headings so the LLM
    knows which line is a hunt-for vs which is a confirm-still-true."""

    def test_diagnosis_uses_outstanding_preamble_only(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        _make_need(case, state=NeedState.PENDING)
        out = _build_evidence_needs_block(case)
        assert "Outstanding needs" in out
        assert "Re-verification checklist" not in out

    def test_mitigation_with_only_reverification_omits_outstanding_section(self):
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
        )
        # A confirmed finding (presence-evidence row) but no PENDING need.
        _make_evidence(case, category=EvidenceCategory.CAUSAL_EVIDENCE)
        out = _build_evidence_needs_block(case)
        assert "Outstanding needs" not in out
        assert "Re-verification checklist" in out

    def test_mitigation_with_only_outstanding_omits_reverification_section(self):
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
        )
        _make_need(case, state=NeedState.PENDING)
        out = _build_evidence_needs_block(case)
        assert "Outstanding needs" in out
        assert "Re-verification checklist" not in out

    def test_mitigation_with_both_renders_both_sections_in_order(self):
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
        )
        pending = _make_need(case, state=NeedState.PENDING, request_text="open work")
        finding = _make_evidence(case, category=EvidenceCategory.CAUSAL_EVIDENCE)
        out = _build_evidence_needs_block(case)
        assert "Outstanding needs" in out
        assert "Re-verification checklist" in out
        # Outstanding section precedes re-verification section
        assert out.index("Outstanding needs") < out.index("Re-verification checklist")
        # The PENDING need lands in outstanding; the confirmed finding
        # (evidence row) lands in re-verification.
        assert out.index(pending.need_id) < out.index("Re-verification checklist")
        assert out.index(finding.evidence_id) > out.index("Re-verification checklist")


# ============================================================
# Anti-anchoring framing (design §6.1) — present in all shapes
# ============================================================


_ANTI_ANCHORING_MARKER = "Unexpected findings outside the entries below"


@pytest.mark.unit
class TestAntiAnchoringFraming:
    """The "unexpected findings are equally important" sentence is
    load-bearing per design §6.1 — it stops the LLM from treating the
    list as exhaustive. It must appear in every rendered shape (not
    just DIAGNOSIS-only), including during re-verification where
    evidence the fix introduced a new problem is exactly the kind of
    finding this framing keeps in view."""

    def test_framing_present_in_diagnosis_outstanding_only(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        _make_need(case, state=NeedState.PENDING)
        out = _build_evidence_needs_block(case)
        assert _ANTI_ANCHORING_MARKER in out

    def test_framing_present_in_mitigation_outstanding_only(self):
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
        )
        _make_need(case, state=NeedState.PENDING)
        out = _build_evidence_needs_block(case)
        assert _ANTI_ANCHORING_MARKER in out

    def test_framing_present_in_mitigation_reverification_only(self):
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
        )
        _make_evidence(case, category=EvidenceCategory.SYMPTOM_EVIDENCE)
        out = _build_evidence_needs_block(case)
        assert _ANTI_ANCHORING_MARKER in out

    def test_framing_present_in_mitigation_both_sections(self):
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
        )
        _make_need(case, state=NeedState.PENDING)
        _make_evidence(case, category=EvidenceCategory.CAUSAL_EVIDENCE)
        out = _build_evidence_needs_block(case)
        assert _ANTI_ANCHORING_MARKER in out
        # Single emission — appears exactly once across the whole block.
        assert out.count(_ANTI_ANCHORING_MARKER) == 1

    def test_framing_precedes_first_section_heading(self):
        """The anti-anchoring sentence renders at the block opening,
        before any section heading. Pins the layout the design spec
        depends on (LLM sees framing before parsing entries)."""
        case = _make_case(
            stage=InvestigationStage.MITIGATION,
        )
        _make_need(case, state=NeedState.PENDING)
        _make_evidence(case, category=EvidenceCategory.CAUSAL_EVIDENCE)
        out = _build_evidence_needs_block(case)
        assert out.index(_ANTI_ANCHORING_MARKER) < out.index("Outstanding needs")
        assert out.index(_ANTI_ANCHORING_MARKER) < out.index(
            "Re-verification checklist"
        )


# ============================================================
# request_text truncation (review fix #2)
# ============================================================


@pytest.mark.unit
class TestRequestTextTruncation:
    """``request_text`` is capped at 500 chars by the schema; the
    rendered line truncates to keep the block scannable. Full text
    stays in the DB for downstream consumers (suggestion-side, UI)."""

    def test_short_request_text_renders_verbatim(self):
        case = _make_case()
        _make_need(case, request_text="short text")
        out = _build_evidence_needs_block(case)
        assert "short text" in out
        assert "…" not in out

    def test_long_request_text_truncated_with_ellipsis(self):
        from faultmaven.core.investigation.prompts.context_builder import (
            _REQUEST_TEXT_RENDER_CAP,
        )

        case = _make_case()
        long_text = "x" * (_REQUEST_TEXT_RENDER_CAP + 50)
        _make_need(case, request_text=long_text)
        out = _build_evidence_needs_block(case)
        # Truncated marker present
        assert "…" in out
        # Full text NOT present in render
        assert long_text not in out
        # Rendered prefix is within the cap
        prefix = "x" * (_REQUEST_TEXT_RENDER_CAP - 1)
        assert prefix in out

    def test_at_exact_cap_no_truncation(self):
        from faultmaven.core.investigation.prompts.context_builder import (
            _REQUEST_TEXT_RENDER_CAP,
        )

        case = _make_case()
        boundary_text = "y" * _REQUEST_TEXT_RENDER_CAP
        _make_need(case, request_text=boundary_text)
        out = _build_evidence_needs_block(case)
        assert boundary_text in out
        assert "…" not in out


# ============================================================
# Tied-priority deterministic ordering (review fix #4)
# ============================================================


@pytest.mark.unit
class TestTiedPriorityOrdering:
    """Two needs at the same priority must render in a stable order so
    the LLM sees a deterministic list across turns. The sort is
    stable; the in-memory test exercises insertion order (the repo's
    SELECT supplies created_at ASC, need_id ASC as the source order in
    production)."""

    def test_same_priority_needs_render_in_insertion_order(self):
        case = _make_case()
        a = _make_need(case, priority=NeedPriority.HIGH, request_text="first high")
        b = _make_need(case, priority=NeedPriority.HIGH, request_text="second high")
        out = _build_evidence_needs_block(case)
        assert out.index(a.need_id) < out.index(b.need_id)


@pytest.mark.unit
class TestCausalSurfaceCap:
    """#604: the block shows at most _SURFACED_CAUSAL_CAP causal asks (surface cap),
    but never caps SYMPTOM needs — a broad Part-A differential can't flood the user."""

    def test_causal_needs_capped_symptom_untouched(self):
        case = _make_case(stage=InvestigationStage.DIAGNOSIS)
        for i in range(6):  # 6 distinct PENDING causal needs
            _make_need(
                case,
                purpose=NeedPurpose.CAUSAL_VERIFICATION,
                request_text=f"causal datum {i}",
            )
        _make_need(
            case,
            purpose=NeedPurpose.SYMPTOM_VERIFICATION,
            request_text="the symptom datum",
        )
        out = _build_evidence_needs_block(case)
        shown_causal = sum(f"causal datum {i}" in out for i in range(6))
        assert shown_causal == _SURFACED_CAUSAL_CAP  # causal asks bounded
        assert "the symptom datum" in out  # symptom need never capped
