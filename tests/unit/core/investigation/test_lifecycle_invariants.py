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

import pytest
from pydantic import ValidationError

from faultmaven.core.investigation.terminal_transitions import (
    _execute_resolved_transition,
    cancel_pending_transition,
    confirm_pending_transition,
    propose_transition,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseAction,
    CaseStatus,
    InquiryData,
    InvestigationProgress,
    ProblemVerification,
    is_valid_action,
)
from faultmaven.modules.case.domain.services.case_action_manager import (
    ALLOWED_ACTIONS,
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


# =============================================================================
# INV-04: INQUIRY → RESOLVED has no direct edge
# =============================================================================
#
# Source: §1.3 line 442 — "There is no INQUIRY → RESOLVED edge. KB-driven
#   cases route through INVESTIGATING via the same-turn milestone collapse."
# Statement: Every RESOLVED case flows through INVESTIGATING — even KB-matched
#   cases. INQUIRY cannot transition directly to RESOLVED.
# Enforcement: Code-guarded + Schema (per the matrix).
#
# Verification surfaced three enforcement surfaces — pinned below:
#   1. ``is_valid_action(INQUIRY, RESOLVED)`` returns False.
#   2. Constructing a ``CaseAction(from_status=INQUIRY, to_status=RESOLVED)``
#      raises a Pydantic ValidationError via the model_validator that calls
#      ``is_valid_action``. CaseAction is frozen, so the validator is the
#      schema-level gate on the audit history.
#   3. ``_execute_resolved_transition`` raises ``ValueError`` when called
#      against a non-INVESTIGATING case — the runtime backstop that prevents
#      mutating status even if a caller skips the audit-history check.
#
# Drift findings captured during this verification (not bugs — to be
# folded into the matrix's drift notes at cluster end):
#   a. The matrix names "VALID_TRANSITIONS dict" — that name doesn't exist
#      in code. Closest: ``ALLOWED_ACTIONS`` (with alias ``ALLOWED_TRANSITIONS``)
#      in case_action_manager.py, and ``valid_actions`` (local) inside
#      ``is_valid_action()`` in models.py.
#   b. The valid-action graph is duplicated across THREE locations
#      (case_action_manager.ALLOWED_ACTIONS, models.is_valid_action,
#      and implicit in the _execute_*_transition preconditions). They
#      currently agree but have no single source of truth.
#   c. ``CaseActionManager.validate_action`` has zero production callers —
#      effectively dead code. The ALLOWED_ACTIONS dict is used by the UI
#      adapter (get_allowed_transitions) to populate dropdown options; it
#      informs the affordance surface, not enforcement.


class TestINV04_NoDirectInquiryToResolved:
    """INV-04: INQUIRY → RESOLVED is forbidden at every enforcement surface."""

    def test_inv04_is_valid_action_rejects_inquiry_to_resolved(self):
        """``is_valid_action()`` in models.py returns False for the forbidden edge.

        This is the function the Pydantic ``CaseAction`` model_validator
        consults. If a future refactor changes the valid_actions map, this
        test fails — surfacing the design-vs-code divergence.
        """
        assert is_valid_action(CaseStatus.INQUIRY, CaseStatus.RESOLVED) is False

        # And the canonical edges that ARE allowed stay allowed:
        assert is_valid_action(CaseStatus.INQUIRY, CaseStatus.INVESTIGATING) is True
        assert is_valid_action(CaseStatus.INQUIRY, CaseStatus.CLOSED) is True
        assert is_valid_action(CaseStatus.INVESTIGATING, CaseStatus.RESOLVED) is True
        assert is_valid_action(CaseStatus.INVESTIGATING, CaseStatus.CLOSED) is True

    def test_inv04_case_action_validator_rejects_inquiry_to_resolved(self):
        """Constructing a ``CaseAction`` for INQUIRY → RESOLVED raises.

        ``CaseAction`` is the audit-history record. ``Config.frozen=True``
        plus the ``validate_action`` model_validator together ensure that
        even an in-memory attempt to record the forbidden transition
        cannot succeed. The audit history therefore cannot lie about an
        impossible transition having happened.
        """
        with pytest.raises(ValidationError, match="Invalid case action"):
            CaseAction(
                from_status=CaseStatus.INQUIRY,
                to_status=CaseStatus.RESOLVED,
                triggered_by="user_test",
                reason="forbidden",
            )

    def test_inv04_execute_resolved_transition_rejects_inquiry_case(self):
        """``_execute_resolved_transition`` raises against a non-INVESTIGATING
        case. This is the runtime backstop: even if a future code path were
        to skip the audit-history check, the execute function would still
        refuse to mutate ``case.status``.
        """
        # Build a case stuck in INQUIRY (do NOT promote it to INVESTIGATING)
        case = Case(
            case_id="case_a1b2c3d4e5f6",
            title="INV-04 inquiry case",
            status=CaseStatus.INQUIRY,
            user_id="user_test",
            organization_id="org_test",
            description="Stuck in inquiry",
            problem_verification=ProblemVerification(
                symptom_statement="Test symptom",
                severity="HIGH",
                temporal_state="ongoing",
                urgency_level="high",
            ),
            inquiry=InquiryData(thread_id="thread_test"),
        )
        assert case.status == CaseStatus.INQUIRY

        with pytest.raises(ValueError, match="Cannot resolve case"):
            _execute_resolved_transition(case, user_id="user_test")

        # Status untouched after the exception
        assert case.status == CaseStatus.INQUIRY
        assert case.resolved_at is None

    def test_inv04_ui_affordance_omits_resolved_from_inquiry(self):
        """The UI's ``ALLOWED_ACTIONS`` dict — used by ``get_allowed_transitions``
        to populate the status-dropdown — does not offer RESOLVED as a
        target when the case is in INQUIRY.

        This is the affordance-surface check (not enforcement). A user
        looking at the dropdown sees only [INVESTIGATING, CLOSED]; the
        forbidden edge is invisible.
        """
        inquiry_targets = ALLOWED_ACTIONS[CaseStatus.INQUIRY]
        assert CaseStatus.RESOLVED not in inquiry_targets
        # The two legitimate targets are present:
        assert CaseStatus.INVESTIGATING in inquiry_targets
        assert CaseStatus.CLOSED in inquiry_targets

    def test_inv04_valid_action_graphs_agree_across_definitions(self):
        """The valid-action graph appears in two places: ``ALLOWED_ACTIONS``
        (case_action_manager.py) and ``valid_actions`` inside
        ``is_valid_action()`` (models.py). They MUST agree.

        Duplication is a maintenance risk: a future edit to one copy
        without the other would let the forbidden edge slip through one
        enforcement surface while the other still rejects it. This test
        pins agreement so any divergence breaks CI immediately.

        Drift to address separately: consolidate to a single source of
        truth. Until then, this test is the consistency guard.
        """
        for from_status in [
            CaseStatus.INQUIRY,
            CaseStatus.INVESTIGATING,
            CaseStatus.RESOLVED,
            CaseStatus.CLOSED,
        ]:
            for to_status in [
                CaseStatus.INQUIRY,
                CaseStatus.INVESTIGATING,
                CaseStatus.RESOLVED,
                CaseStatus.CLOSED,
            ]:
                dict_allows = to_status in ALLOWED_ACTIONS.get(from_status, [])
                func_allows = is_valid_action(from_status, to_status)
                assert dict_allows == func_allows, (
                    f"Disagreement on {from_status.value} → {to_status.value}: "
                    f"ALLOWED_ACTIONS says {dict_allows}, "
                    f"is_valid_action says {func_allows}. "
                    f"These must agree — see INV-04 drift note."
                )
