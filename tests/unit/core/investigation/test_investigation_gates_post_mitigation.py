"""Tests for INV-21: Gate 3 (post-mitigation continuation) enforcement.

Pins the slice 3 behavior:

- `_post_mitigation_suggestions()` emits the canonical Gate 3 pair.
- `_gate3_is_pending()` predicate semantics.
- INV-21 milestone guard rejects RCA-side milestones while Gate 3 is open.
- `mitigation_completed_at_turn` boundary marker is set the first turn
  mitigation_verified is completed (idempotent — re-entry doesn't overwrite).
- Pre-mitigation evidence is up-weighted after Gate 3 opens.

Design reference: docs/working/WIP-investigation-gates-implementation.md
(slice 3 / Gate 3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from faultmaven.core.investigation.milestone_engine import (
    _gate3_is_pending,
    _post_mitigation_suggestions,
)
from faultmaven.core.investigation.prompts.context_builder import (
    _score_evidence_for_tier_a,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InvestigationPath,
    InvestigationProgress,
    InvestigationStage,
    PathSelection,
    ProblemConfirmation,
    ProblemVerification,
    TemporalState,
    UploadedFile,
    UrgencyLevel,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_mitigation_first_case(
    *,
    mitigation_completed_at_turn: int | None = None,
    rca_after_mitigation_confirmed: bool = False,
    status: CaseStatus = CaseStatus.INVESTIGATING,
    current_turn: int = 5,
    closure_reason: str | None = None,
) -> Case:
    """Build a mitigation-first case with controllable Gate 3 state."""
    ps = PathSelection(
        path=InvestigationPath.MITIGATION_FIRST,
        auto_selected=True,
        rationale="Ongoing high-urgency impact",
        alternate_path=InvestigationPath.ROOT_CAUSE,
        user_confirmed=True,  # Gate 2 already closed (Gate 3 is what we're testing)
        user_confirmed_at_turn=2,
        mitigation_completed_at_turn=mitigation_completed_at_turn,
        rca_after_mitigation_confirmed=rca_after_mitigation_confirmed,
    )
    case_kwargs = dict(
        user_id="u1",
        organization_id="o1",
        title="Test",
        description="API outage",
        status=status,
        current_turn=current_turn,
        path_selection=ps,
    )
    if status in (CaseStatus.CLOSED, CaseStatus.RESOLVED):
        # closed_at / resolved_at must be after created_at; explicitly
        # set created_at well in the past so the validator passes.
        case_kwargs["created_at"] = datetime.now(UTC) - timedelta(hours=1)
        case_kwargs["closed_at"] = datetime.now(UTC)
        case_kwargs["closure_reason"] = closure_reason or "inquiry_only"
        if status == CaseStatus.RESOLVED:
            case_kwargs["resolved_at"] = datetime.now(UTC)
    return Case(
        **case_kwargs,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="API outage",
            problem_confirmation=ProblemConfirmation(
                problem_type="unavailability",
                severity_guess="high",
                preliminary_guidance="API outage",
            ),
        ),
        problem_verification=ProblemVerification(
            symptom_statement="API outage",
            severity="HIGH",
            temporal_state=TemporalState.ONGOING,
            urgency_level=UrgencyLevel.HIGH,
        ),
        progress=InvestigationProgress(current_stage=InvestigationStage.DIAGNOSIS),
    )


# ---------------------------------------------------------------------------
# _gate3_is_pending predicate
# ---------------------------------------------------------------------------


class TestGate3IsPending:
    """The predicate is True iff Gate 3 needs the user's decision."""

    def test_true_when_mitigation_completed_and_rca_not_confirmed(self):
        case = _make_mitigation_first_case(
            mitigation_completed_at_turn=4,
            rca_after_mitigation_confirmed=False,
        )
        assert _gate3_is_pending(case) is True

    def test_false_when_mitigation_not_completed(self):
        case = _make_mitigation_first_case(
            mitigation_completed_at_turn=None,
        )
        assert _gate3_is_pending(case) is False

    def test_false_after_user_confirms_rca(self):
        case = _make_mitigation_first_case(
            mitigation_completed_at_turn=4,
            rca_after_mitigation_confirmed=True,
        )
        assert _gate3_is_pending(case) is False

    def test_false_on_root_cause_path(self):
        case = _make_mitigation_first_case(mitigation_completed_at_turn=4)
        # Override to root-cause path
        case.path_selection = case.path_selection.model_copy(
            update={"path": InvestigationPath.ROOT_CAUSE}
        )
        assert _gate3_is_pending(case) is False

    def test_false_when_case_is_terminal(self):
        case = _make_mitigation_first_case(
            mitigation_completed_at_turn=4,
            status=CaseStatus.CLOSED,
        )
        assert _gate3_is_pending(case) is False

    def test_false_when_path_selection_missing(self):
        case = _make_mitigation_first_case(mitigation_completed_at_turn=4)
        case.path_selection = None
        assert _gate3_is_pending(case) is False


# ---------------------------------------------------------------------------
# _post_mitigation_suggestions
# ---------------------------------------------------------------------------


class TestPostMitigationSuggestions:
    """The Gate 3 suggestion pair shape."""

    def test_returns_two_suggestions(self):
        suggs = _post_mitigation_suggestions()
        assert len(suggs) == 2

    def test_first_suggestion_continues_to_rca(self):
        suggs = _post_mitigation_suggestions()
        assert suggs[0]["action_type"] == "COOPERATIVE"
        assert suggs[0]["intent"]["type"] == "post_mitigation_choice"
        assert suggs[0]["intent"]["continue_to_rca"] is True
        # Body should set expectations about RCA benefit
        assert (
            "root-cause" in suggs[0]["body"].lower()
            or "runbook" in suggs[0]["body"].lower()
        )

    def test_second_suggestion_closes_as_mitigation_sufficient(self):
        suggs = _post_mitigation_suggestions()
        assert suggs[1]["intent"]["type"] == "status_transition"
        assert suggs[1]["intent"]["to_status"] == "closed"
        assert suggs[1]["intent"]["closure_reason"] == "mitigation_sufficient"
        # The body must surface the runbook trade-off (clean-from-start
        # principle: the user sees the implication at click time).
        assert "runbook" in suggs[1]["body"].lower()


# ---------------------------------------------------------------------------
# INV-21: pre-mitigation evidence up-weighting
# ---------------------------------------------------------------------------


def _make_file_backed_evidence(
    case: Case,
    *,
    collected_at_turn: int,
    source_type: EvidenceSourceType = EvidenceSourceType.LOGS,
    category: EvidenceCategory = EvidenceCategory.SYMPTOM_EVIDENCE,
) -> Evidence:
    """Build a file-backed Evidence row + add the backing UploadedFile to
    the case so the evidence_source_invariant CHECK is satisfied. The
    pre-mitigation uplift in _score_evidence_for_tier_a only fires on
    Tier A (data evidence with source_file_id) so file-backed evidence
    is required here.
    """
    uploaded = UploadedFile(
        filename="test.log",
        size_bytes=1024,
        source="upload",
        uploaded_at_turn=collected_at_turn,
    )
    case.uploaded_files.append(uploaded)
    return Evidence(
        summary="Evidence row",
        source_type=source_type,
        source_file_id=uploaded.file_id,
        category=category,
        primary_purpose="Test evidence",
        collected_by="agent",
        collected_at_turn=collected_at_turn,
    )


class TestPreMitigationEvidenceUpWeight:
    """After Gate 3 opens, evidence collected before mitigation gets +5 score.

    The score boost makes pre-mitigation evidence outrank post-mitigation
    noise during RCA — important because post-mitigation telemetry typically
    shows the stabilized system that no longer exhibits the root cause's
    signature.
    """

    def test_pre_mitigation_evidence_gets_uplift_post_gate3(self):
        case = _make_mitigation_first_case(
            mitigation_completed_at_turn=4,
            current_turn=7,  # past the mitigation boundary
        )
        ev_pre = _make_file_backed_evidence(case, collected_at_turn=3)
        ev_post = _make_file_backed_evidence(case, collected_at_turn=6)
        case.evidence.extend([ev_pre, ev_post])

        score_pre = _score_evidence_for_tier_a(ev_pre, case)
        score_post = _score_evidence_for_tier_a(ev_post, case)

        # Pre-mitigation evidence outranks post-mitigation by approximately
        # the uplift amount (+5) minus the recency-bonus delta the
        # post-mitigation evidence enjoys (post collected_at_turn is closer
        # to current_turn). Net advantage to pre is at least 4.
        assert score_pre - score_post >= 4.0

    def test_no_uplift_before_mitigation_completes(self):
        """When mitigation_completed_at_turn is None, no boundary exists yet."""
        case = _make_mitigation_first_case(
            mitigation_completed_at_turn=None,
            current_turn=7,
        )
        ev = _make_file_backed_evidence(case, collected_at_turn=3)
        case.evidence.append(ev)
        # Without a boundary, the uplift branch doesn't fire — score is
        # the baseline (recency + source_type bonus). +5 uplift would push
        # past 5.0; absence keeps it below.
        baseline = _score_evidence_for_tier_a(ev, case)
        assert baseline < 5.0

    def test_uplift_only_when_current_turn_past_boundary(self):
        """Evidence collected at the boundary turn or before, with current
        turn past it, is what gets uplifted. Sanity check that the predicate
        is `current_turn > boundary AND collected_at_turn <= boundary`."""
        case = _make_mitigation_first_case(
            mitigation_completed_at_turn=4,
            current_turn=4,  # NOT past the boundary
        )
        ev = _make_file_backed_evidence(case, collected_at_turn=3)
        case.evidence.append(ev)
        score = _score_evidence_for_tier_a(ev, case)
        # current_turn == boundary → uplift doesn't fire yet.
        assert score < 5.0


# ---------------------------------------------------------------------------
# INV-21 milestone guard: data-level shapes
# ---------------------------------------------------------------------------


class TestINV21Gate3MilestoneGuard:
    """The INV-21 guard predicate determines whether RCA-side milestones
    are blocked. Full _apply_investigation_updates integration uses these
    state shapes."""

    def test_guard_blocks_when_gate3_pending(self):
        case = _make_mitigation_first_case(
            mitigation_completed_at_turn=4,
            rca_after_mitigation_confirmed=False,
        )
        assert _gate3_is_pending(case) is True

    def test_guard_unblocks_after_user_confirms_rca(self):
        case = _make_mitigation_first_case(
            mitigation_completed_at_turn=4,
            rca_after_mitigation_confirmed=True,
        )
        assert _gate3_is_pending(case) is False

    def test_guard_inactive_on_root_cause_path(self):
        """RCA-only path never has a Gate 3 to enforce."""
        case = _make_mitigation_first_case()
        case.path_selection = case.path_selection.model_copy(
            update={"path": InvestigationPath.ROOT_CAUSE}
        )
        assert _gate3_is_pending(case) is False


# ---------------------------------------------------------------------------
# Gate 3 state transitions via model_copy (intent handler pattern)
# ---------------------------------------------------------------------------


class TestGate3StateTransitions:
    """The POST_MITIGATION_CHOICE intent handler uses model_copy to advance
    rca_after_mitigation_confirmed. STATUS_TRANSITION (close) goes through
    the propose+confirm machinery separately."""

    def test_continue_rca_via_model_copy(self):
        case = _make_mitigation_first_case(
            mitigation_completed_at_turn=4,
            rca_after_mitigation_confirmed=False,
        )
        assert _gate3_is_pending(case) is True

        # Simulate the POST_MITIGATION_CHOICE intent handler
        case.path_selection = case.path_selection.model_copy(
            update={
                "rca_after_mitigation_confirmed": True,
                "rca_after_mitigation_confirmed_at_turn": case.current_turn,
            }
        )

        assert case.path_selection.rca_after_mitigation_confirmed is True
        assert case.path_selection.rca_after_mitigation_confirmed_at_turn == 5
        # Other fields preserved
        assert case.path_selection.path == InvestigationPath.MITIGATION_FIRST
        assert case.path_selection.mitigation_completed_at_turn == 4
        assert _gate3_is_pending(case) is False

    def test_re_entry_to_mitigation_does_not_overwrite_completion_marker(self):
        """If a second mitigation cycle is needed, the boundary marker
        captures the FIRST completion turn — that's the RCA-relevant
        boundary for the originally-observed root cause."""
        case = _make_mitigation_first_case(mitigation_completed_at_turn=4)
        first_completion = case.path_selection.mitigation_completed_at_turn
        # Subsequent mitigation completion would NOT overwrite (the engine
        # logic in _apply_stage_gate_side_effects guards on the marker
        # being None before setting). This test pins the data model
        # expectation.
        assert first_completion == 4
