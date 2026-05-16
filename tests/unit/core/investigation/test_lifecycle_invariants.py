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

    def test_inv06_full_kb_resolution_path_requires_explicit_confirm(self):
        """End-to-end pin: knowledge_resolution + propose_transition leave
        the case INVESTIGATING. Only ``confirm_pending_transition`` —
        invoked separately — executes the disposition.

        Documents the canonical KB-resolution sequence and asserts the
        invariant explicitly.
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
