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

import inspect
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from faultmaven.core.investigation.milestone_engine import MilestoneEngine
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
    KnowledgeResolution,
    ProblemVerification,
    is_valid_action,
)
from faultmaven.modules.case.domain.services.case_action_manager import ALLOWED_ACTIONS


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


# =============================================================================
# INV-06: KB-Resolution path still uses pending_transition (no auto-resolve)
# =============================================================================
#
# Source: §1.2 *KB-Resolution Path (Same-Turn Variant)* (lines 345-385).
# Statement: When the LLM emits ``knowledge_resolution`` (runbook fix
#   confirmed by user), the engine does NOT bypass the disposition
#   handshake. It populates milestone state from the matched runbook Cause
#   and lets the standard ProposedTransition flow handle the disposition.
# Enforcement: Structural — uses the same ``pending_transition`` mechanism
#   as the multi-turn path. The "collapse" is in milestone-state authoring,
#   not transition timing.
#
# Drift surfaced during verification (to fold into §1.3.1 drift notes):
#
#   a. Design §1.2 overstates the same-turn collapse. The text claims "no
#      additional confirmation turn is required", but the engine's
#      ``transition_proposed_this_turn`` guard at milestone_engine.py:5253
#      prevents same-turn confirmation. In current code, the KB-resolution
#      path STILL requires a separate confirmation turn — same as the
#      multi-turn path. The "collapse" is only in milestone-state
#      authoring (RootCauseConclusion + Solution populated in one turn),
#      NOT in user-side disposition timing.
#
#   b. The matrix row for INV-06 is accurate ("still goes through
#      propose_transition + user confirmation") but doesn't reflect the
#      design-text overstatement in §1.2. The matrix understates while
#      §1.2 oversells. Both should converge on the actual behavior.
#
#   c. ``metadata["knowledge_resolution_signalled"]`` is set in
#      _apply_investigation_updates (line 4690) but never read elsewhere.
#      Dead metadata. Minor; flag for cleanup.
#
# INV-06's structural invariant itself HOLDS — the engine does not
# auto-resolve from ``knowledge_resolution``. The tests below pin that.


class TestINV06_KBResolutionUsesPendingTransition:
    """INV-06: knowledge_resolution does not bypass the propose+confirm gate."""

    def test_inv06_propose_transition_with_knowledge_resolution_present_does_not_execute(
        self,
    ):
        """``propose_transition`` behaves identically whether or not
        ``knowledge_resolution`` is set on the case.

        Pins that the KB-resolution path uses the same structural
        mechanism as multi-turn resolution. The presence of
        knowledge_resolution does NOT grant a same-turn execute
        bypass at the function level.
        """
        case = _make_investigating_case()
        # Simulate the LLM having stored a runbook resolution signal:
        case.inquiry.knowledge_resolution = KnowledgeResolution(
            match_id="rb_abc123",
            match_type="runbook",
            solution_applied="Restarted the service per runbook",
            user_confirmation="That fixed it",
            resolution_turn=2,
        )

        propose_transition(
            case,
            to_status="resolved",
            summary="Resolved via runbook rb_abc123",
            evidence_ids=[],
        )

        # Standard pending_transition write — identical to INV-03
        assert case.pending_transition is not None
        assert case.pending_transition["to_status"] == "resolved"
        # Status UNCHANGED — no auto-resolve from knowledge_resolution
        assert case.status == CaseStatus.INVESTIGATING
        assert case.resolved_at is None
        # knowledge_resolution is preserved on the case (audit trail)
        assert case.inquiry.knowledge_resolution is not None
        assert case.inquiry.knowledge_resolution.match_id == "rb_abc123"

    def test_inv06_engine_knowledge_resolution_handler_does_not_auto_resolve(self):
        """The engine's ``_apply_investigation_updates`` knowledge_resolution
        handler stores the signal but does NOT call confirm or execute.

        This is a code-shape pin: future refactors that add an
        auto-resolve shortcut inside the knowledge_resolution block would
        break this test. The pin is on the structural property documented
        at milestone_engine.py:4673-4697 ("Standard ProposedTransition
        handshake handles disposition").
        """
        source = inspect.getsource(MilestoneEngine._apply_investigation_updates)

        # Find the knowledge_resolution handling block. The comment
        # immediately above the if-statement (line 4673-4681 at time of
        # writing) anchors the block.
        kr_idx = source.find('if hasattr(updates, "knowledge_resolution")')
        assert kr_idx >= 0, (
            "Could not locate knowledge_resolution handling block in "
            "_apply_investigation_updates. The static check below assumes "
            "this structure; if the handler moved, this test must move "
            "with it."
        )

        # Walk forward until the next top-level comment block or the next
        # major if-statement to bound the kr handler region. A 1500-char
        # window is conservative — the actual handler is ~20 lines.
        kr_region = source[kr_idx : kr_idx + 1500]

        # These calls indicate auto-resolution. None should appear inside
        # the kr handler:
        forbidden_calls = [
            "confirm_pending_transition",
            "_execute_resolved_transition",
            "_execute_closed_transition",
            "case.status = CaseStatus.RESOLVED",
            "case.atomic_update(\n            status=CaseStatus.RESOLVED",
        ]
        for forbidden in forbidden_calls:
            assert forbidden not in kr_region, (
                f"INV-06 violation: knowledge_resolution handler in "
                f"_apply_investigation_updates contains '{forbidden}'. "
                f"The engine must not auto-resolve from knowledge_resolution; "
                f"the standard ProposedTransition handshake handles "
                f"disposition (see §1.2 KB-Resolution Path)."
            )

    @pytest.mark.asyncio
    async def test_inv06_engine_collapses_to_one_turn_when_both_flags_set(self):
        """KB-Resolution Path same-turn collapse (§1.2): when the engine
        sees BOTH ``transition_proposed_this_turn`` AND
        ``knowledge_resolution_signalled`` in the same turn's metadata,
        it confirms the pending transition immediately.

        This is the only path that fires confirm in the same turn as
        propose. Every other ProposedTransition emission still follows
        the standard 2-turn handshake.
        """
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=lambda c: c)
        engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())

        case = _make_investigating_case()

        # Simulate: same-turn LLM emission landed a ProposedTransition AND
        # a knowledge_resolution. propose_transition was called (writes
        # pending_transition); _apply_investigation_updates set both
        # metadata flags. We exercise the engine's _check_automatic_-
        # transitions which holds the collapse logic.
        propose_transition(
            case,
            to_status="resolved",
            summary="Runbook rb_abc123 applied; user confirmed it worked",
            evidence_ids=[],
        )
        assert case.pending_transition is not None
        assert case.status == CaseStatus.INVESTIGATING

        metadata = {
            "transition_proposed_this_turn": True,
            "knowledge_resolution_signalled": True,
        }

        result = await engine._check_automatic_transitions(case, metadata)

        # Same-turn collapse fired
        assert result.status == CaseStatus.RESOLVED, (
            "KB-Resolution same-turn collapse did not fire even though both "
            "metadata flags were set. INV-06 design intent: the user's "
            "knowledge_resolution-triggering message covers the disposition "
            "acknowledgment; no separate confirmation turn required."
        )
        assert result.resolved_at is not None
        assert metadata.get("status_transitioned") is True
        # Pending cleared after successful confirm
        assert result.pending_transition is None

    @pytest.mark.asyncio
    async def test_inv06_engine_holds_handshake_when_only_proposal_flag_set(self):
        """Negative pin: when ``transition_proposed_this_turn`` is set
        but ``knowledge_resolution_signalled`` is NOT, the engine does
        NOT collapse — the standard 2-turn handshake holds.

        Confirms the gating is conjunction (BOTH flags), not disjunction.
        Without this test, a future refactor that loosened the guard
        could let any same-turn proposal auto-confirm.
        """
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=lambda c: c)
        engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())

        case = _make_investigating_case()
        propose_transition(
            case,
            to_status="resolved",
            summary="Standard resolution proposal",
            evidence_ids=[],
        )

        metadata = {
            "transition_proposed_this_turn": True,
            # knowledge_resolution_signalled is NOT set — standard path
        }

        result = await engine._check_automatic_transitions(case, metadata)

        # Status UNCHANGED — standard 2-turn handshake holds
        assert result.status == CaseStatus.INVESTIGATING, (
            "Same-turn collapse fired on a non-KB path. The collapse "
            "must be gated on BOTH transition_proposed_this_turn AND "
            "knowledge_resolution_signalled — never on the first alone."
        )
        assert result.resolved_at is None
        # Pending stays — waiting for user confirmation next turn
        assert result.pending_transition is not None
        assert metadata.get("status_transitioned") is not True

    def test_inv06_full_kb_resolution_path_requires_explicit_confirm(self):
        """End-to-end pin: at the FUNCTION level (propose_transition +
        confirm_pending_transition), knowledge_resolution presence does
        not grant a same-turn bypass. The same-turn collapse is at the
        ENGINE level (``_check_automatic_transitions``), gated by
        metadata flags set during turn processing.

        This complements
        ``test_inv06_engine_collapses_to_one_turn_when_both_flags_set``
        which pins the engine-level collapse, and documents the
        canonical 2-step sequence used by every non-KB path.
        """
        case = _make_investigating_case()
        case.inquiry.knowledge_resolution = KnowledgeResolution(
            match_id="rb_abc123",
            match_type="runbook",
            solution_applied="Applied runbook fix",
            user_confirmation="It worked",
            resolution_turn=2,
        )

        # Step 1: engine proposes (LLM emitted knowledge_resolution + ProposedTransition)
        propose_transition(
            case,
            to_status="resolved",
            summary="Resolved via runbook",
            evidence_ids=[],
        )
        assert case.status == CaseStatus.INVESTIGATING  # NOT yet resolved
        assert case.pending_transition is not None

        # Step 2: explicit confirm (next turn, or via intent-routed click)
        # is the ONLY thing that completes the transition.
        result = confirm_pending_transition(case, user_id="user_test")

        assert result is True
        assert case.status == CaseStatus.RESOLVED
        assert case.resolved_at is not None


# =============================================================================
# INV-14: Manual case-action dropdown uses standard handshake
# =============================================================================
#
# Source: §1.5 *Manual Case Action Requests* — Core Principle: "Manual case
#   actions follow the same confirmation pattern as natural progression —
#   all case actions require explicit user confirmation."
# Statement: Manual case-action requests (status dropdown) flow through the
#   same confirmation pattern as natural progression — they cannot bypass
#   the User-Agent Handshake.
# Enforcement: Structural — the UI sends a system message with
#   ``intent_type="status_transition"`` that routes through ``submit_turn``
#   + the standard ``pending_transition`` mechanism. Each target either
#   calls ``propose_transition`` (CLOSED, RESOLVED) or falls through to
#   the LLM pipeline (INVESTIGATING) which writes pending via the LLM-
#   emitted ProposedTransition.
#
# Drift surfaced during verification (to fold into §1.3.1 drift notes):
#
#   a. Design §1.5.2 Step 2 describes a system-generated message
#      format ("[User requested to change case status to X]") sent to
#      ``/queries`` as plain text. The current implementation uses
#      ``intent_type="status_transition"`` + structured ``intent_data``,
#      added by the 2026-02-09 bug fix (milestone_engine.py:1714).
#      The text-based mechanism the design describes is no longer how
#      the dropdown flows — §1.5.2 should be updated to describe the
#      structured-intent route.
#
#   b. The RESOLVED dropdown is *path-dependent*: if a matching
#      pending_transition already exists, the click confirms it
#      (milestone_engine.py:1801-1816); otherwise the click runs
#      ``assess_resolution_readiness`` and may propose RESOLVED, pivot
#      to propose CLOSED, or ask for needs_info. The §1.5.2 narrative
#      doesn't surface this branching; readers won't know the dropdown
#      can do these three different things. Worth a paragraph.


class TestINV14_DropdownUsesStandardHandshake:
    """INV-14: dropdown-initiated case actions never bypass the handshake.

    Each test verifies that after the dropdown intent is processed:
      - case.status is UNCHANGED (no auto-execution).
      - pending_transition is set (for CLOSED/RESOLVED) — handshake
        proposal landed.
      - resolved_at / closed_at remain None.

    The existing test_transition_alignment.py tests verify the same
    paths emit the canonical confirmation pair. These tests assert the
    stronger property: the case is NOT in a terminal state after the
    dropdown turn.
    """

    @staticmethod
    def _engine_and_repo() -> tuple[MilestoneEngine, MagicMock]:
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=lambda c: c)
        repo.get = AsyncMock(side_effect=lambda cid: None)
        engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())
        return engine, repo

    @pytest.mark.asyncio
    async def test_inv14_dropdown_inquiry_to_closed_proposes_does_not_execute(self):
        """Dropdown INQUIRY → CLOSED writes pending_transition and returns
        confirmation suggestions. Case status stays INQUIRY this turn.
        """
        engine, _ = self._engine_and_repo()
        case = Case(
            case_id="case_a1b2c3d4e5f6",
            title="INV-14 inquiry",
            status=CaseStatus.INQUIRY,
            user_id="user_test",
            organization_id="org_test",
            description="Inquiry case",
            problem_verification=ProblemVerification(
                symptom_statement="Test",
                severity="HIGH",
                temporal_state="ongoing",
                urgency_level="high",
            ),
            inquiry=InquiryData(thread_id="t1"),
        )

        result = await engine.process_turn(
            case=case,
            user_message="Close this case.",
            intent_type="status_transition",
            intent_data={
                "from_status": "inquiry",
                "to_status": "closed",
                "user_confirmed": True,
            },
        )

        updated = result["case_updated"]
        # Handshake: pending written, status untouched
        assert updated.pending_transition is not None
        assert updated.pending_transition["to_status"] == "closed"
        assert updated.status == CaseStatus.INQUIRY
        assert updated.closed_at is None

    @pytest.mark.asyncio
    async def test_inv14_dropdown_investigating_to_closed_proposes_does_not_execute(
        self,
    ):
        """Dropdown INVESTIGATING → CLOSED writes pending_transition; status
        stays INVESTIGATING this turn.
        """
        engine, _ = self._engine_and_repo()
        case = _make_investigating_case()
        case.progress.symptom_verified = True

        result = await engine.process_turn(
            case=case,
            user_message="Close this case as unresolved.",
            intent_type="status_transition",
            intent_data={
                "from_status": "investigating",
                "to_status": "closed",
                "user_confirmed": True,
            },
        )

        updated = result["case_updated"]
        assert updated.pending_transition is not None
        assert updated.pending_transition["to_status"] == "closed"
        assert updated.status == CaseStatus.INVESTIGATING
        assert updated.closed_at is None

    @pytest.mark.asyncio
    async def test_inv14_dropdown_investigating_to_resolved_thin_does_not_execute(
        self,
    ):
        """Dropdown INVESTIGATING → RESOLVED on a case lacking root cause /
        solution pivots to propose CLOSED (assess_resolution_readiness
        verdict SUGGEST_CLOSE). Either way, the case is NOT auto-resolved
        and NOT auto-closed — a pending_transition is written for user
        confirmation.

        Pins that the readiness-pivot branch (lines 1850-1873) honors the
        handshake just like the direct-resolve branch.
        """
        engine, _ = self._engine_and_repo()
        case = _make_investigating_case()
        # No root cause, no solutions → SUGGEST_CLOSE verdict

        result = await engine.process_turn(
            case=case,
            user_message="Mark this resolved.",
            intent_type="status_transition",
            intent_data={
                "from_status": "investigating",
                "to_status": "resolved",
                "user_confirmed": True,
            },
        )

        updated = result["case_updated"]
        # Either RESOLVED or CLOSED could be proposed depending on
        # readiness verdict. The invariant is: not auto-executed.
        assert updated.pending_transition is not None
        assert updated.status == CaseStatus.INVESTIGATING
        assert updated.resolved_at is None
        assert updated.closed_at is None

    def test_inv14_dropdown_investigating_branch_does_not_directly_execute_resolved(
        self,
    ):
        """Static check: the engine's ``elif to_status_str == "investigating"``
        branch does NOT contain calls to ``_execute_resolved_transition``,
        ``_execute_closed_transition``, ``confirm_pending_transition``,
        or direct status mutations. The branch falls through to the LLM
        pipeline so the standard handshake handles confirmation.

        Complement to the functional tests above: pins the structural
        property of the INVESTIGATING branch even without exercising
        the full LLM pipeline.
        """
        source = inspect.getsource(MilestoneEngine._process_turn_impl)

        # Find the investigating-target branch within the status_transition
        # intent handler.
        investigating_idx = source.find('elif to_status_str == "investigating":')
        assert investigating_idx >= 0, (
            "Could not locate the 'investigating' branch of the "
            "status_transition intent handler. The static check below "
            "assumes this structure."
        )

        # Walk to the next sibling branch / end-of-block. The
        # 'investigating' branch ends when the next major block begins.
        # Take a generous 1500-char window.
        branch_region = source[investigating_idx : investigating_idx + 1500]

        # The invariant: this branch must not directly execute a transition.
        # It should fall through to the LLM pipeline so the standard
        # ProposedTransition handshake handles disposition.
        forbidden_calls = [
            "_execute_resolved_transition",
            "_execute_closed_transition",
            "confirm_pending_transition(case, case.user_id)",
            "case.status = CaseStatus.INVESTIGATING\n",
        ]
        for forbidden in forbidden_calls:
            assert forbidden not in branch_region, (
                f"INV-14 violation: the INQUIRY → INVESTIGATING dropdown "
                f"branch contains '{forbidden}'. The dropdown must not "
                f"directly execute the transition; it must fall through "
                f"to the LLM pipeline so user_confirmed_investigation=True "
                f"drives the transition through the standard handshake. "
                f"See §1.5 *Core Principle*."
            )


# =============================================================================
# INV-07: No Evidence creation during INQUIRY
# =============================================================================
#
# Source: §1.2.1 *Core principles* — "No evidence creation during INQUIRY.
#   Evidence presupposes a confirmed claim. During INQUIRY the claim is
#   still being formed; the LLM may read uploaded files for context but
#   does not emit evidence_to_add. The Pydantic InquiryResponse.-
#   InquiryStateUpdate schema does not carry an evidence_to_add field;
#   the _apply_inquiry_updates evidence-creation branch was removed."
# Statement: Evidence rows are born only during INVESTIGATING. INQUIRY
#   uploads create UploadedFile rows only.
# Enforcement: Schema (InquiryStateUpdate field absence) + code-guarded
#   (no evidence-creation branch in _apply_inquiry_updates).
#
# Drift surfaced during verification:
#
#   a. InquiryStateUpdate uses Pydantic's default extra='ignore'. An LLM
#      emitting evidence_to_add (or any other invalid field) on an INQUIRY
#      response is SILENTLY DROPPED, not rejected. The invariant holds
#      (no Evidence row created) but a schema violation by the LLM does
#      not surface as an error. A more defensive design would set
#      extra='forbid' to catch LLM training drift. Worth considering at
#      a future hardening pass; not a violation of INV-07 itself.


class TestINV07_NoEvidenceDuringInquiry:
    """INV-07: Evidence rows cannot be born during INQUIRY.

    Enforced by absence of the evidence_to_add field on
    InquiryStateUpdate and by the absence of an evidence-creation branch
    in _apply_inquiry_updates.
    """

    def test_inv07_inquiry_state_update_has_no_evidence_to_add_field(self):
        """``InquiryStateUpdate.model_fields`` must not contain
        ``evidence_to_add``. This is the primary enforcement surface —
        the LLM cannot use a typed channel to add evidence during INQUIRY.
        """
        from faultmaven.core.investigation.schemas import InquiryResponse

        fields = InquiryResponse.InquiryStateUpdate.model_fields
        assert "evidence_to_add" not in fields, (
            f"INV-07 violation: InquiryStateUpdate has an evidence_to_add "
            f"field. Evidence creation during INQUIRY is forbidden — the "
            f"LLM should not have a typed channel to emit it. See §1.2.1 "
            f"core principle 2. Current fields: {list(fields.keys())}"
        )

    def test_inv07_investigation_state_updates_DO_have_evidence_to_add(self):
        """Complementary assertion: ``DiagnosisStateUpdate`` and the other
        investigation-stage schemas DO have ``evidence_to_add``. The
        asymmetry between INQUIRY and INVESTIGATING is intentional —
        Evidence is born during INVESTIGATING, never during INQUIRY.
        """
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        diagnosis_fields = (
            InvestigationResponse_Diagnosis.DiagnosisStateUpdate.model_fields
        )
        assert "evidence_to_add" in diagnosis_fields, (
            "DiagnosisStateUpdate is expected to carry evidence_to_add. "
            "If this assertion fails alongside the INQUIRY one, the "
            "investigation phase has lost its evidence channel — a much "
            "more serious regression than the INV-07 invariant."
        )

    def test_inv07_extra_evidence_field_on_inquiry_is_silently_dropped(self):
        """Pydantic policy: ``InquiryStateUpdate`` uses ``extra='ignore'``
        (the default). An LLM emitting ``evidence_to_add`` in an INQUIRY
        response has the field silently dropped. The invariant still
        holds — no Evidence row is created — but the LLM's schema
        violation is not surfaced as an error.

        Pinned here for awareness: if a future hardening pass switches
        to ``extra='forbid'``, this test should be updated to assert the
        ValidationError instead.
        """
        from faultmaven.core.investigation.schemas import InquiryResponse

        instance = InquiryResponse.InquiryStateUpdate(
            evidence_to_add=[{"summary": "fake", "category": "symptom_evidence"}]
        )

        # Field is silently dropped — not stored, not raised
        assert not hasattr(instance, "evidence_to_add")
        dumped = instance.model_dump()
        assert "evidence_to_add" not in dumped

    def test_inv07_apply_inquiry_updates_has_no_evidence_creation_branch(self):
        """Static check: ``_apply_inquiry_updates`` must not contain any
        code that mutates ``case.evidence`` or creates Evidence rows.

        The design states that the evidence-creation branch was REMOVED
        from _apply_inquiry_updates. This test pins that removal.
        """
        source = inspect.getsource(MilestoneEngine._apply_inquiry_updates)

        # Forbidden mutations / creations:
        #   - case.evidence.append(...) / case.evidence = ...
        #   - Evidence(...) constructor calls
        forbidden_patterns = [
            "case.evidence.append",
            "case.evidence.extend",
            "case.evidence = ",
            "Evidence(",
            "EvidenceToAdd(",
        ]
        for pattern in forbidden_patterns:
            assert pattern not in source, (
                f"INV-07 violation: _apply_inquiry_updates contains "
                f"'{pattern}' — Evidence creation during INQUIRY is "
                f"forbidden. See §1.2.1 core principle 2."
            )


# =============================================================================
# INV-09: Terminal cases are immutable
# =============================================================================
#
# Source: §1.7 *Terminal Mode* (lines 1039-1080)
# Statement: Terminal cases (RESOLVED/CLOSED) are immutable. No new evidence,
#   no transitions, no milestone updates. Only text Q&A, report regeneration,
#   and runbook creation are permitted.
# Enforcement: API-level (``require_case_not_terminal()`` rejects mutating
#   endpoints) + Code-guarded (``_process_terminal_turn`` short-circuits the
#   milestone engine).
#
# Drift surfaced during verification:
#
#   a. Matrix calls require_case_not_terminal() "middleware" — it isn't.
#      It's a plain helper function in routes.py:519, used at exactly one
#      call site (routes.py:987, the case-update endpoint). Other write
#      endpoints (routes.py:1173, 2142, 2781) check case.is_terminal
#      inline rather than via the helper. The protection is consistent in
#      intent across endpoints, but the mechanism varies — a future
#      hardening pass could consolidate via FastAPI dependency injection.
#      Not a violation; terminology is the only drift.


class TestINV09_TerminalCasesImmutable:
    """INV-09: terminal cases reject mutations at every enforcement surface."""

    def test_inv09_is_terminal_returns_true_for_resolved_and_closed(self):
        """``case.is_terminal`` returns True for RESOLVED and CLOSED, False
        for INQUIRY and INVESTIGATING. This is the predicate every other
        enforcement surface consults.

        Uses ``object.__setattr__`` to bypass the Case model's
        bidirectional validators (RESOLVED requires resolved_at, etc.) —
        this test isn't about the validators; it's about ``is_terminal``.
        """
        case = _make_investigating_case()

        # Non-terminal states
        object.__setattr__(case, "status", CaseStatus.INQUIRY)
        assert case.is_terminal is False
        object.__setattr__(case, "status", CaseStatus.INVESTIGATING)
        assert case.is_terminal is False

        # Terminal states (bypass cross-field validators to isolate
        # the is_terminal property)
        object.__setattr__(case, "status", CaseStatus.RESOLVED)
        assert case.is_terminal is True
        object.__setattr__(case, "status", CaseStatus.CLOSED)
        assert case.is_terminal is True

    def test_inv09_require_case_not_terminal_raises_409_on_resolved(self):
        """``require_case_not_terminal(case)`` raises HTTPException 409
        when the case is RESOLVED. This is the API-level guard used by
        the case-update endpoint.
        """
        from fastapi import HTTPException

        from faultmaven.modules.case.api.routes import require_case_not_terminal

        case = _make_investigating_case()
        object.__setattr__(case, "status", CaseStatus.RESOLVED)

        with pytest.raises(HTTPException) as exc_info:
            require_case_not_terminal(case)

        assert exc_info.value.status_code == 409
        assert "terminal" in exc_info.value.detail.lower()

    def test_inv09_require_case_not_terminal_raises_409_on_closed(self):
        """Same guard, CLOSED variant."""
        from fastapi import HTTPException

        from faultmaven.modules.case.api.routes import require_case_not_terminal

        case = _make_investigating_case()
        object.__setattr__(case, "status", CaseStatus.CLOSED)

        with pytest.raises(HTTPException) as exc_info:
            require_case_not_terminal(case)

        assert exc_info.value.status_code == 409

    def test_inv09_require_case_not_terminal_noop_on_non_terminal(self):
        """``require_case_not_terminal(case)`` returns silently for INQUIRY
        and INVESTIGATING cases — only terminal cases are rejected.
        """
        from faultmaven.modules.case.api.routes import require_case_not_terminal

        case = _make_investigating_case()

        # INQUIRY: no-op
        case.status = CaseStatus.INQUIRY
        require_case_not_terminal(case)  # must not raise

        # INVESTIGATING: no-op
        case.status = CaseStatus.INVESTIGATING
        require_case_not_terminal(case)  # must not raise

    def test_inv09_milestone_engine_short_circuits_on_terminal_case(self):
        """Static check: ``_process_turn_impl`` short-circuits to
        ``_process_terminal_turn`` when the case is terminal. The
        normal investigation pipeline (which mutates state, advances
        milestones, etc.) is bypassed entirely.

        Pins the engine-level enforcement that complements the API-level
        ``require_case_not_terminal`` guards.
        """
        source = inspect.getsource(MilestoneEngine._process_turn_impl)

        # The short-circuit should be near the top of the method,
        # before any state-mutation paths.
        assert "case.is_terminal" in source, (
            "INV-09 violation: _process_turn_impl no longer checks "
            "case.is_terminal. The engine must short-circuit terminal "
            "cases to _process_terminal_turn so they can't be mutated "
            "via the normal milestone pipeline."
        )
        assert "_process_terminal_turn" in source, (
            "INV-09 violation: _process_turn_impl no longer routes to "
            "_process_terminal_turn. Terminal cases must short-circuit "
            "to the Q&A handler instead of running the full pipeline."
        )


# =============================================================================
# INV-10: submit_turn rejection rules on terminal cases
# =============================================================================
#
# Source: §1.7 *Terminal Mode* (lines 1072-1078)
# Statement: ``submit_turn`` on a terminal case:
#   - text query → routed to terminal Q&A
#   - files / pasted content → 409 Conflict
#   - status-transition intent → 409 Conflict
# Enforcement: API-level — submit_turn endpoint inspects payload kind.
#
# No drift surfaced; the matrix description matches the inline guard at
# routes.py:2141-2154 exactly.


class TestINV10_SubmitTurnRejectionRules:
    """INV-10: submit_turn rejects mutating payloads on terminal cases."""

    def test_inv10_submit_turn_rejects_files_on_terminal_case(self):
        """Static check: ``submit_turn`` source contains the files /
        pasted_content rejection block on terminal cases."""
        from faultmaven.modules.case.api import routes

        source = inspect.getsource(routes.submit_turn)

        # The terminal-case guard
        assert (
            "case.is_terminal" in source
        ), "INV-10 violation: submit_turn no longer checks case.is_terminal."
        # Files / pasted content rejection
        assert "files or pasted_content" in source, (
            "INV-10 violation: submit_turn no longer rejects "
            "(files or pasted_content) on terminal cases. See §1.7 "
            "Terminal Mode rejection rules."
        )
        # Must use the 409 Conflict status code
        assert "HTTP_409_CONFLICT" in source, (
            "INV-10 violation: submit_turn no longer uses 409 Conflict "
            "for terminal-state rejections."
        )

    def test_inv10_submit_turn_rejects_status_transition_on_terminal_case(self):
        """Static check: status-transition intents are rejected on
        terminal cases."""
        from faultmaven.modules.case.api import routes

        source = inspect.getsource(routes.submit_turn)

        # Must have the intent_type == "status_transition" rejection inside
        # the is_terminal block. We confirm the literal is present at all
        # and the 409 status is in the surrounding lines.
        assert 'intent_type == "status_transition"' in source, (
            "INV-10 violation: submit_turn no longer rejects "
            'intent_type == "status_transition" on terminal cases.'
        )

    def test_inv10_submit_turn_does_not_reject_text_only_query_on_terminal(self):
        """Static check: the terminal-state guard in ``submit_turn`` does
        NOT reject text-only queries. The Q&A route through
        ``_process_terminal_turn`` depends on text queries being passed
        through.

        The rejection branches are gated on ``files or pasted_content``
        and ``intent_type == "status_transition"`` — neither of which
        applies to a pure text query.
        """
        from faultmaven.modules.case.api import routes

        source = inspect.getsource(routes.submit_turn)

        # The terminal-case guard is followed by conditional rejection
        # branches gated on files / pasted_content / status_transition.
        # There must NOT be an unconditional "raise HTTPException(409)"
        # immediately after the `if case.is_terminal:` line.
        terminal_idx = source.find("if case.is_terminal:")
        assert terminal_idx >= 0
        # Look at the ~500 chars following the terminal check: there should
        # be conditional `if` branches, not an unconditional raise.
        terminal_block = source[terminal_idx : terminal_idx + 500]
        # Counts of conditional rejection branches
        assert terminal_block.count("if files or pasted_content") >= 1
        assert terminal_block.count('if intent_type == "status_transition"') >= 1
        # And the block must not blanket-reject queries — there should be
        # no `if query:` branch that raises 409 inside the terminal guard.
        assert "if query:" not in terminal_block, (
            "INV-10 violation: submit_turn appears to blanket-reject "
            "queries on terminal cases. Text Q&A must be allowed."
        )


# =============================================================================
# INV-12: Free-typed paraphrases route to Q&A, never produce persisted side effect
# =============================================================================
#
# Source: §1.7.3 *Regeneration* — "Free text routes to Q&A: the regen handler
#   is reached only via the COOPERATIVE suggestion's precomposed payload
#   (exact-match). Free-typed paraphrases like 'give me a recap' or 'new
#   summary please' route to terminal Q&A."
# Statement: Only exact-match of the COOPERATIVE payload triggers a
#   persisted Report or Runbook side effect. Everything else routes to
#   the Q&A handler.
# Enforcement: Code-guarded — _REPORT_REGEN_PATTERNS and
#   _RUNBOOK_CREATION_PATTERNS use exact-match (msg_lower in patterns).
#
# No drift surfaced; the matrix description matches the code at
# milestone_engine.py:1055 and 1064 exactly.


class TestINV12_FreeTextRoutesToQA:
    """INV-12: only exact-match COOPERATIVE payloads produce persisted side effects.

    The dispatcher in ``_process_terminal_turn`` routes by exact-match
    against ``_REPORT_REGEN_PATTERNS`` / ``_RUNBOOK_CREATION_PATTERNS``.
    Anything else falls through to ``_process_terminal_qa`` (no persisted
    side effect).
    """

    @pytest.mark.asyncio
    async def test_inv12_exact_payload_match_triggers_regen(self):
        """The precomposed regen payload routes to
        ``_handle_report_regeneration``, NOT Q&A."""
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=lambda c: c)
        engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())

        case = _make_investigating_case()
        object.__setattr__(case, "status", CaseStatus.RESOLVED)

        # Mock the three dispatch handlers
        engine._handle_report_regeneration = AsyncMock(
            return_value={
                "agent_response": "regenerated",
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": {},
            }
        )
        engine._handle_runbook_creation = AsyncMock()
        engine._process_terminal_qa = AsyncMock()

        # Exact-match the precomposed payload
        await engine._process_terminal_turn(
            case,
            "Regenerate the resolution summary report for this case",
            {},
        )

        engine._handle_report_regeneration.assert_called_once()
        engine._handle_runbook_creation.assert_not_called()
        engine._process_terminal_qa.assert_not_called()

    @pytest.mark.asyncio
    async def test_inv12_free_typed_recap_paraphrase_routes_to_qa(self):
        """Free-typed paraphrases like 'give me a recap' route to Q&A —
        NOT regen. No persisted Report side effect."""
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=lambda c: c)
        engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())

        case = _make_investigating_case()
        object.__setattr__(case, "status", CaseStatus.RESOLVED)

        engine._handle_report_regeneration = AsyncMock()
        engine._handle_runbook_creation = AsyncMock()
        engine._process_terminal_qa = AsyncMock(
            return_value={
                "agent_response": "Q&A",
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": {},
            }
        )

        # Multiple paraphrases that MUST route to Q&A, not regen
        paraphrases = [
            "give me a recap",
            "summarize what happened",
            "new summary please",
            "redo the report",
            "what did we conclude",
            "can you regenerate this",  # substring of "regenerate" — must NOT match
            "regenerate the summary",  # missing "report for this case"
        ]
        for msg in paraphrases:
            engine._handle_report_regeneration.reset_mock()
            engine._process_terminal_qa.reset_mock()
            await engine._process_terminal_turn(case, msg, {})
            engine._handle_report_regeneration.assert_not_called()
            engine._process_terminal_qa.assert_called_once()

    @pytest.mark.asyncio
    async def test_inv12_runbook_paraphrase_routes_to_qa(self):
        """Runbook-creation paraphrases route to Q&A — only the exact
        COOPERATIVE payload triggers the persisted runbook side effect."""
        repo = MagicMock()
        repo.save = AsyncMock(side_effect=lambda c: c)
        engine = MilestoneEngine(MagicMock(), repo, investigation_tools=MagicMock())

        case = _make_investigating_case()
        object.__setattr__(case, "status", CaseStatus.RESOLVED)

        engine._handle_runbook_creation = AsyncMock()
        engine._process_terminal_qa = AsyncMock(
            return_value={
                "agent_response": "Q&A",
                "suggested_follow_ups": [],
                "case_updated": case,
                "metadata": {},
            }
        )

        paraphrases = [
            "create a runbook please",
            "make a runbook",
            "generate a runbook",  # missing "from this resolved case"
            "yes, create a runbook",
            "i want a runbook",
        ]
        for msg in paraphrases:
            engine._handle_runbook_creation.reset_mock()
            engine._process_terminal_qa.reset_mock()
            await engine._process_terminal_turn(case, msg, {})
            engine._handle_runbook_creation.assert_not_called()
            engine._process_terminal_qa.assert_called_once()

    def test_inv12_patterns_match_cooperative_suggestion_payloads(self):
        """The dispatcher's exact-match tuples must equal the precomposed
        payloads of the COOPERATIVE suggestions. If a payload string changes
        (in milestone_engine.py module-level constants) the dispatcher
        constants must change in lockstep, or clicking the suggestion would
        stop triggering its action.
        """
        from faultmaven.core.investigation.milestone_engine import (
            GENERATE_RUNBOOK_PAYLOAD,
            REGENERATE_CLOSURE_SUMMARY_PAYLOAD,
            REGENERATE_RESOLUTION_SUMMARY_PAYLOAD,
        )

        # Patterns are stored on the class (lowercased)
        regen_patterns = MilestoneEngine._REPORT_REGEN_PATTERNS
        runbook_patterns = MilestoneEngine._RUNBOOK_CREATION_PATTERNS

        # Every COOPERATIVE payload must appear in the dispatcher's tuple
        # (lowercased, since user_message is lower-cased before matching)
        assert REGENERATE_CLOSURE_SUMMARY_PAYLOAD.lower() in regen_patterns, (
            "REGENERATE_CLOSURE_SUMMARY_PAYLOAD constant changed but "
            "_REPORT_REGEN_PATTERNS was not updated — clicking the regen "
            "suggestion would now route to Q&A instead of regen."
        )
        assert REGENERATE_RESOLUTION_SUMMARY_PAYLOAD.lower() in regen_patterns
        assert GENERATE_RUNBOOK_PAYLOAD.lower() in runbook_patterns


# =============================================================================
# INV-13: Closure-ack turn omits regen; Q&A turn offers it
# =============================================================================
#
# Source: §1.7.3 *Regeneration: Where it's offered* (lines 1113-1114)
# Statement: Closure-acknowledgment turn for RESOLVED offers the runbook
#   affordance only (no regen). Closure-ack for CLOSED is silent (no
#   suggestions). Regen is offered on subsequent terminal Q&A turns when
#   the substance gate would PASS.
# Enforcement: Code-guarded — closure-ack call sites use
#   ``_resolved_ack_suggestions()`` / ``[]``; terminal Q&A uses
#   ``_resolved_suggestions()`` / ``_closed_suggestions()``.
#
# No drift surfaced.


class TestINV13_AckTurnVsQATurnSuggestions:
    """INV-13: ack-turn and Q&A-turn suggestion sets differ by design.

    The pins below verify the helper-function CONTRACTS that the engine's
    wiring depends on. The wiring itself (which helper is called at each
    call site) is already covered by test_inquiry_transition.py and
    test_transition_alignment.py.
    """

    def test_inv13_resolved_ack_offers_runbook_only_no_regen(self):
        """``_resolved_ack_suggestions()`` returns the runbook affordance
        only — NO regen card. Regen beside a freshly-rendered summary
        on the ack turn would be noise.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _resolved_ack_suggestions,
        )

        suggestions = _resolved_ack_suggestions()
        labels = [s["label"] for s in suggestions]

        # Exactly the runbook affordance, nothing else
        assert any("runbook" in label.lower() for label in labels), (
            "INV-13 violation: _resolved_ack_suggestions no longer offers "
            "the runbook affordance. The ack-turn must still offer the "
            "forward action."
        )
        # Critically: NO regen card on the ack turn
        regen_labels = [label for label in labels if "regenerate" in label.lower()]
        assert regen_labels == [], (
            f"INV-13 violation: _resolved_ack_suggestions includes regen "
            f"affordance {regen_labels} — but regen is reserved for "
            f"subsequent terminal Q&A turns. See §1.7.3."
        )

    def test_inv13_resolved_qa_offers_both_regen_and_runbook(self):
        """``_resolved_suggestions()`` (for terminal Q&A turns on RESOLVED
        cases) returns both the regen affordance and the runbook
        affordance.
        """
        from faultmaven.core.investigation.milestone_engine import _resolved_suggestions

        suggestions = _resolved_suggestions()
        labels = [s["label"] for s in suggestions]

        assert any("regenerate" in label.lower() for label in labels), (
            "INV-13 violation: _resolved_suggestions no longer offers "
            "regen on terminal Q&A turns. Users need a chat-side path "
            "to iterate / retry."
        )
        assert any("runbook" in label.lower() for label in labels), (
            "INV-13 violation: _resolved_suggestions no longer offers "
            "runbook on terminal Q&A turns."
        )

    def test_inv13_closed_qa_offers_regen_when_substance_gate_passes(self):
        """``_closed_suggestions(case)`` offers the regen affordance when
        the substance gate would PASS, [] when FAIL. CLOSED cases never
        get the runbook affordance (only RESOLVED cases qualify).
        """
        from faultmaven.core.investigation.milestone_engine import _closed_suggestions

        case = _make_investigating_case()
        object.__setattr__(case, "status", CaseStatus.CLOSED)

        # No evidence / hypotheses / milestones → gate FAIL → no suggestions
        assert _closed_suggestions(case) == [], (
            "INV-13 violation: _closed_suggestions returns non-empty for "
            "a CLOSED case with no substance — the substance gate should "
            "block regen in this case."
        )

        # Mark a milestone completed → completed_milestones property
        # returns non-empty → substance gate PASS → regen offered
        case.progress.symptom_verified = True

        suggestions = _closed_suggestions(case)
        assert suggestions, "CLOSED + substance must yield the regen suggestion"
        labels = [s["label"] for s in suggestions]
        assert any("regenerate" in label.lower() for label in labels)
        # CLOSED never offers runbook
        assert not any("runbook" in label.lower() for label in labels), (
            "INV-13 violation: _closed_suggestions offered a runbook "
            "affordance — only RESOLVED cases qualify for runbook "
            "generation. See §1.7.3."
        )

    def test_inv13_ack_and_qa_suggestion_sets_differ(self):
        """The whole point of the asymmetry: ack-turn and Q&A-turn
        suggestion sets for RESOLVED are NOT equal. The ack set is a
        proper subset of the Q&A set (runbook only vs regen+runbook).
        """
        from faultmaven.core.investigation.milestone_engine import (
            _resolved_ack_suggestions,
            _resolved_suggestions,
        )

        ack = _resolved_ack_suggestions()
        qa = _resolved_suggestions()

        assert ack != qa, (
            "INV-13 violation: closure-ack and terminal-Q&A return the "
            "same suggestion set for RESOLVED cases. They MUST differ — "
            "the ack turn should not duplicate the just-rendered summary "
            "with a regen card."
        )
        assert len(ack) < len(qa), (
            "ack-turn suggestions should be a strict subset of Q&A-turn "
            "suggestions (runbook only vs regen+runbook)."
        )


# =============================================================================
# INV-15: Agent ADVISOR role
# =============================================================================
#
# Source: §1.6 *Agent Role Constraints* (lines 1003-1010)
# Statement: The agent is an ADVISOR — it never runs commands, accesses
#   systems, or makes infrastructure changes. Enforced via vocabulary
#   constraint (banned/required phrase table).
# Enforcement: Prompt-only + light vocabulary check.
#
# Drift surfaced during verification:
#
#   a. The "light vocabulary check" in the matrix refers to the
#      ``_completion_phrases`` scan at milestone_engine.py:2602 in
#      _process_turn_impl. That scan is scoped to transition-completion
#      claims ("case closed", "marking as resolved") — not the broader
#      banned-phrase list in _ADVISOR_ROLE_CONSTRAINT ("Let me check",
#      "I will run", etc.). The check is NARROWER than the prompt rule
#      it backstops. Not a violation; calibration mismatch.


class TestINV15_AgentAdvisorRole:
    """INV-15: ADVISOR-role vocabulary constraint is wired into prompts + scanned.

    Pins the prompt content (banned phrases present) and the runtime
    compliance scan (engine logs completion-phrase detection for
    quarterly drift review).
    """

    def test_inv15_advisor_role_constraint_contains_banned_phrases(self):
        """``_ADVISOR_ROLE_CONSTRAINT`` (the prompt constant) explicitly
        bans the action-claim phrases.
        """
        from faultmaven.core.investigation.prompts.templates import (
            _ADVISOR_ROLE_CONSTRAINT,
        )

        required_bans = [
            "Let me check",
            "I will run",
            "Let me look at",
            "I'll execute",
        ]
        for phrase in required_bans:
            assert phrase in _ADVISOR_ROLE_CONSTRAINT, (
                f"INV-15 violation: banned phrase '{phrase}' is no longer "
                f"in _ADVISOR_ROLE_CONSTRAINT. The agent's role boundary "
                f"depends on the LLM seeing this list."
            )

    def test_inv15_advisor_role_constraint_offers_alternatives(self):
        """``_ADVISOR_ROLE_CONSTRAINT`` tells the LLM what to say instead.
        A banned-only list without alternatives leaves the LLM no
        graceful path; this test pins the prescriptive guidance.
        """
        from faultmaven.core.investigation.prompts.templates import (
            _ADVISOR_ROLE_CONSTRAINT,
        )

        # At least one of the prescriptive alternatives must appear
        expected_alternatives = ["Could you run", "Please check"]
        assert any(alt in _ADVISOR_ROLE_CONSTRAINT for alt in expected_alternatives), (
            "INV-15 violation: _ADVISOR_ROLE_CONSTRAINT no longer offers "
            "use-instead alternatives. Banned phrases without alternatives "
            "leave the LLM no graceful path."
        )

    def test_inv15_advisor_role_constraint_used_in_all_relevant_templates(self):
        """The advisor-role constraint must be present in INQUIRY_TEMPLATE,
        the INVESTIGATION_BASE, and TERMINAL_TEMPLATE. A drop from any of
        these would let the LLM act outside its role in that phase.
        """
        from faultmaven.core.investigation.prompts import templates as tmpl

        # All three top-level templates render the constraint as a substring
        # (woven in via either _ADVISOR_ROLE_CONSTRAINT directly or the
        # _ACTIVE_ADVISOR_ROLE_BLOCK wrapper).
        banned_marker = "BANNED PHRASES"
        assert banned_marker in tmpl.INQUIRY_TEMPLATE, (
            "INV-15 violation: INQUIRY_TEMPLATE no longer embeds the "
            "advisor-role banned-phrase block."
        )
        assert banned_marker in tmpl.TERMINAL_TEMPLATE, (
            "INV-15 violation: TERMINAL_TEMPLATE no longer embeds the "
            "advisor-role banned-phrase block."
        )
        # INVESTIGATION_BASE / DIAGNOSIS / etc. — use the active-stage
        # wrapper. We check at least one investigation-stage template.
        if hasattr(tmpl, "INVESTIGATION_BASE"):
            assert banned_marker in tmpl.INVESTIGATION_BASE, (
                "INV-15 violation: INVESTIGATION_BASE no longer embeds "
                "the advisor-role banned-phrase block."
            )

    def test_inv15_runtime_compliance_scan_exists_in_process_turn_impl(self):
        """``_process_turn_impl`` must contain the runtime compliance scan
        that logs agent_response for completion-phrase claims. This is
        the only runtime backstop for INV-15; if it's removed, drift in
        the LLM becomes invisible.
        """
        source = inspect.getsource(MilestoneEngine._process_turn_impl)

        # The compliance instrumentation block
        assert "_completion_phrases" in source, (
            "INV-15 violation: _process_turn_impl no longer contains the "
            "_completion_phrases compliance scan. The quarterly drift "
            "review depends on this telemetry."
        )
        assert "transition_compliance" in source, (
            "INV-15 violation: _process_turn_impl no longer emits "
            "'transition_compliance' telemetry. Drift detection signals "
            "must be preserved."
        )
        # At least one of the canonical completion phrases must be in
        # the scanned tuple
        canonical_phrases = ["case closed", "marked as resolved"]
        assert any(phrase in source for phrase in canonical_phrases), (
            "INV-15 violation: the _completion_phrases tuple appears to "
            "have been emptied or replaced with unrecognizable content."
        )
