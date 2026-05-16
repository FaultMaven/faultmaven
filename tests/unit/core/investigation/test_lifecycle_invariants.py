"""Lifecycle Invariant tests — pin the design invariants enumerated in §1.3.1.

Each test asserts one row from the Invariant Enforcement Matrix in
``docs/architecture/investigation-engine/investigation-lifecycle-logic.md``.
Test names carry the ``inv_XX`` prefix so they remain identifiable across
refactors that rename the underlying functions.

When the matrix gains or loses a row, this file should add or remove the
corresponding test. When an existing row's enforcement category changes
(e.g., from Structural to Code-guarded), the test should be updated to
pin the new mechanism.
"""

from datetime import datetime, timezone

from faultmaven.core.investigation.terminal_transitions import (
    cancel_pending_transition,
    confirm_pending_transition,
    propose_transition,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
)


def _make_investigating_case() -> Case:
    """Minimal INVESTIGATING case with the inquiry-confirmation fields set.

    The lifecycle invariants are about transitions, so the case factories
    avoid loading any progress / evidence / hypothesis state that's
    unrelated to the invariant under test. Each test adds whatever it
    needs.
    """
    case = Case(
        case_id="case_a1b2c3d4e5f6",
        title="Invariant test",
        status=CaseStatus.INQUIRY,
        user_id="user_test",
        organization_id="org_test",
        description="Invariant test description",
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(thread_id="thread_test"),
    )
    case.inquiry.proposed_problem_statement = "Invariant test problem"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(timezone.utc)
    case.status = CaseStatus.INVESTIGATING
    case.progress = InvestigationProgress()
    return case


# =============================================================================
# INV-03: Disposition transitions are never auto-fired
# =============================================================================
#
# Source: §1.2 *INVESTIGATING → RESOLVED (Disposition)*; §1.4 line 488
# Statement: Disposition transitions (INVESTIGATING → RESOLVED, INVESTIGATING →
#   CLOSED, INQUIRY → CLOSED) NEVER auto-fire. The agent emits
#   ProposedTransition; the user confirms on a subsequent turn.
# Enforcement: Structural — propose_transition writes pending_transition only;
#   confirm_pending_transition is the only path that mutates case.status.
#
# These tests pin the *function-level contract* that the structural property
# rests on: propose has no side effect on status, and confirm requires a
# prior propose. The engine's per-turn message dispatch then ensures the two
# calls land in separate turns. A refactor that consolidates propose+confirm
# into a single function would break these tests immediately.


class TestINV03_DispositionHandshake:
    """INV-03: disposition transitions cannot complete without two separate calls."""

    def test_inv03_propose_resolved_writes_pending_only(self):
        """``propose_transition`` to RESOLVED stores a pending transition but
        does not mutate ``case.status``. Mirrors the design's "writes
        pending_transition; does NOT execute" guarantee.
        """
        case = _make_investigating_case()
        assert case.status == CaseStatus.INVESTIGATING
        assert case.resolved_at is None
        assert case.pending_transition is None

        propose_transition(
            case,
            to_status="resolved",
            summary="Solution applied and verified",
            evidence_ids=[],
        )

        # Pending transition is recorded for the next-turn confirmation
        assert case.pending_transition is not None
        assert case.pending_transition["to_status"] == "resolved"
        assert "proposed_at" in case.pending_transition
        # Status has NOT changed — propose is write-only-to-pending
        assert case.status == CaseStatus.INVESTIGATING
        assert case.resolved_at is None

    def test_inv03_propose_closed_writes_pending_only(self):
        """``propose_transition`` to CLOSED stores a pending transition with
        engine-derived closure_reason but does not mutate ``case.status``.
        """
        case = _make_investigating_case()

        propose_transition(
            case,
            to_status="closed",
            summary="Closing without resolution",
            evidence_ids=[],
        )

        assert case.pending_transition is not None
        assert case.pending_transition["to_status"] == "closed"
        # closure_reason is engine-derived at propose time (one of the three
        # canonical values: inquiry_only | closed_after_investigation |
        # mitigation_sufficient)
        assert "closure_reason" in case.pending_transition
        assert case.pending_transition["closure_reason"] in (
            "inquiry_only",
            "closed_after_investigation",
            "mitigation_sufficient",
        )
        # Status unchanged
        assert case.status == CaseStatus.INVESTIGATING
        assert case.closed_at is None

    def test_inv03_confirm_without_prior_propose_is_noop(self):
        """``confirm_pending_transition`` is a no-op when no pending exists.

        Pins the one-way data dependency from propose to confirm: there is
        no path to mutate status via confirm without first writing
        pending_transition via propose.
        """
        case = _make_investigating_case()
        assert case.pending_transition is None
        assert case.status == CaseStatus.INVESTIGATING

        result = confirm_pending_transition(case, user_id="user_test")

        # Confirm returns False and mutates nothing
        assert result is False
        assert case.status == CaseStatus.INVESTIGATING
        assert case.resolved_at is None
        assert case.closed_at is None
        assert case.pending_transition is None

    def test_inv03_full_handshake_executes_only_via_explicit_confirm(self):
        """End-to-end function-level handshake: propose → confirm executes.

        Documents the canonical sequence and pins that ``confirm_pending_-
        transition`` is the ONLY path that actually mutates ``case.status``
        for disposition transitions. The engine's per-turn message dispatch
        ensures these two calls land in separate process_turn invocations
        (Turn N: propose, Turn N+1: confirm).
        """
        case = _make_investigating_case()

        # Turn N: agent proposes
        propose_transition(
            case,
            to_status="resolved",
            summary="Solution applied",
            evidence_ids=[],
        )
        assert case.pending_transition is not None
        assert case.status == CaseStatus.INVESTIGATING  # NOT yet resolved

        # Turn N+1: user confirms via explicit confirm call
        result = confirm_pending_transition(case, user_id="user_test")

        # Now and only now does status change
        assert result is True
        assert case.status == CaseStatus.RESOLVED
        assert case.resolved_at is not None
        # Pending is cleared after successful execution
        assert case.pending_transition is None

    def test_inv03_decline_clears_pending_without_executing(self):
        """``cancel_pending_transition`` clears the pending transition and
        leaves ``case.status`` unchanged. Complements the propose/confirm
        pair: the user can decline the proposal, and the case stays in
        its current state.
        """
        case = _make_investigating_case()
        propose_transition(
            case,
            to_status="resolved",
            summary="Solution applied",
            evidence_ids=[],
        )
        assert case.pending_transition is not None

        cleared = cancel_pending_transition(case)

        assert cleared is True
        assert case.pending_transition is None
        assert case.status == CaseStatus.INVESTIGATING  # unchanged
        assert case.resolved_at is None
