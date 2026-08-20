"""Regression tests for the investigation-flow redesign trap fix (Phase 2).

Covers the two engine mechanisms that dissolve the S1 (k8s-pvc-pending)
mitigation_first trap documented in
docs/architecture/investigation-engine/investigation-flow-redesign.md §1.1:

1. Surgical reasoning-validation strip — a single unjustified milestone no
   longer wipes co-emitted VALID milestones (the literal collateral-wipe bug).
2. cause_state derivation — the engine-owned assessment variable derives
   CANDIDATES from >=2 ACTIVE hypotheses, is UNKNOWN otherwise, and IDENTIFIED
   is forward-only (sticky).
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from faultmaven.core.investigation.milestone_engine import (
    _recompute_assessment_state,
    validate_reasoning_first,
)
from faultmaven.core.investigation.schemas import InternalReasoning, MilestoneUpdates
from faultmaven.modules.case.contracts import (
    Case,
    CaseState,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InvestigationProgress,
    RootCauseConclusion,
    Solution,
    SolutionFeasible,
    SolutionState,
    SolutionType,
)


def _response(milestones: MilestoneUpdates, justifications: dict):
    """Minimal duck-typed investigation response (not Inquiry/Terminal)."""
    ir = InternalReasoning(
        milestone_justifications=justifications, evidence_analyzed=[]
    )
    state_updates = SimpleNamespace(milestones=milestones, evidence_to_add=[])
    return SimpleNamespace(internal_reasoning=ir, state_updates=state_updates)


def _case(*, evidence=True):
    return SimpleNamespace(
        state=CaseState.INVESTIGATING,
        is_terminal=False,
        pending_transition=None,
        progress=SimpleNamespace(solution_verified=False),
        evidence=["ev_1"] if evidence else [],
        current_turn=5,
        case_id="case_test",
    )


class TestSurgicalStrip:
    def test_unjustified_milestone_is_offending_justified_one_is_preserved(self):
        """The S1 mechanism: a single unjustified milestone (symptom_verified)
        must not drag a justified mitigation_accepted into the strip."""
        milestones = MilestoneUpdates(symptom_verified=True, mitigation_accepted=True)
        # Only mitigation_accepted is justified; symptom_verified is not.
        justifications = {"mitigation_accepted": "User applied the fix (ev_1)"}

        is_valid, errors, offending = validate_reasoning_first(
            _response(milestones, justifications), _case()
        )

        assert is_valid is False
        assert offending == {"symptom_verified"}
        # mitigation_accepted was justified -> NOT stripped.
        assert "mitigation_accepted" not in offending

    def test_no_internal_reasoning_strips_all_completed(self):
        milestones = MilestoneUpdates(symptom_verified=True, mitigation_accepted=True)
        resp = _response(milestones, {})
        resp.internal_reasoning = None

        is_valid, errors, offending = validate_reasoning_first(resp, _case())

        assert is_valid is False
        assert offending == {"symptom_verified", "mitigation_accepted"}

    def test_no_actionable_evidence_strips_all_completed(self):
        milestones = MilestoneUpdates(symptom_verified=True)
        justifications = {"symptom_verified": "confirmed"}

        is_valid, errors, offending = validate_reasoning_first(
            _response(milestones, justifications), _case(evidence=False)
        )

        assert is_valid is False
        assert offending == {"symptom_verified"}

    def test_all_justified_passes_clean(self):
        milestones = MilestoneUpdates(symptom_verified=True)
        justifications = {"symptom_verified": "confirmed via ev_1"}

        is_valid, errors, offending = validate_reasoning_first(
            _response(milestones, justifications), _case()
        )

        assert is_valid is True
        assert offending == set()


class TestSolutionStateDerivation:
    """``solution_state`` / ``solution_proposed`` derivation in
    ``_recompute_assessment_state`` (INV-32; cause_state derivation itself is
    exercised by test_chain_cause_state.py, offer liveness by
    test_solution_offer_liveness.py)."""

    def _case(self):
        return SimpleNamespace(
            case_id="case_test",
            current_turn=3,
            progress=InvestigationProgress(),
            hypotheses={},
            solutions=[],
            evidence=[],
            causal_nodes={},
            causal_edges={},
            root_cause_conclusion=None,
            proposed_actions=[],
        )

    def test_solution_state_selected_when_ladder_advanced(self):
        # The gate ladder is a forward-only fact: solution_accepted keeps the
        # derived pair True/SELECTED even with no live offer standing.
        case = self._case()
        case.progress.solution_accepted = True
        _recompute_assessment_state(case)
        assert case.progress.solution_state == SolutionState.SELECTED
        assert case.progress.solution_proposed is True

    def test_solution_state_unknown_when_no_live_offer(self):
        # A stale persisted True with no live offer and no ladder derives OFF —
        # the write-once latch is gone (INV-32).
        case = self._case()
        case.progress.solution_proposed = True
        _recompute_assessment_state(case)
        assert case.progress.solution_state == SolutionState.UNKNOWN
        assert case.progress.solution_proposed is False


def _solution(title: str = "Correct the OIDC provider client ID"):
    """A real ``Solution`` — the model rejects duck-typed stand-ins on assignment."""
    return Solution(
        solution_type=SolutionType.CONFIG_CHANGE,
        title=title,
        # A Solution must carry actionable content (model validator).
        longterm_fix="Set the provider ClientIDList to sts.amazonaws.com.",
    )


class TestDeferredImplementationClose:
    """Follow-on A: solution_feasible=DEFERRED proposes CLOSE-with-documented-solution.

    INV-32: the proposal additionally requires the established-cause license
    (the closure message asserts "the root cause and fix are documented") —
    positive cases set cause_state=IDENTIFIED; a case whose cause fell must
    NOT be proposed for closure on its monotone Solution records.
    """

    def _case(
        self,
        *,
        feasible,
        solution_proposed,
        pending=None,
        terminal=False,
        cause_identified=True,
        causal_absence=False,
        inquiry=False,
    ):
        """A REAL ``Case`` — not a SimpleNamespace.

        ``_maybe_propose_deferred_close`` now consults ``assess_closure_readiness``
        for the resolve-preservation pivot, which walks ``case.evidence`` and
        ``case.progress.completed_milestones``. A duck-typed stand-in silently
        lacks those and turns a genuine behavioural test into an AttributeError,
        so the fixture uses the real type and the real readiness predicate.
        """
        # ``description`` is required before INVESTIGATING (Case validator) —
        # one of the real constraints the SimpleNamespace stand-in hid.
        case = Case(
            organization_id="org_test",
            title="deferred fix",
            description="Cross-account AssumeRole fails for the data-processor pods.",
        )
        # Inquiry readiness must be satisfied BEFORE the state assignment —
        # validate_assignment runs the INVESTIGATING gate on every __setattr__.
        case.inquiry.problem_statement_confirmed = True
        case.inquiry.decided_to_investigate = True
        # NB: the state is chosen here but every other field is set BELOW, so
        # an early return would leave solution_feasible unset and the proposer
        # would bail at its FIRST guard — the test would pass while proving
        # nothing about the state guard.
        if terminal:
            # CLOSED and closed_at each require the other, so neither can be
            # assigned first — they have to arrive together, after created_at.
            # The old fixture set state alone and RAISED, which is why this
            # parameter was dead and the is_terminal guard untested.
            case = Case(
                organization_id="org_test",
                title="deferred fix",
                description=case.description,
                state=CaseState.CLOSED,
                closed_at=case.created_at + timedelta(seconds=1),
                closure_reason="solution_deferred",
            )
        elif not inquiry:
            case.state = CaseState.INVESTIGATING
        # inquiry=True leaves the case in INQUIRY — the state the NEW guard
        # adds. A terminal case was ALREADY rejected by the `is_terminal`
        # check this replaced, so a CLOSED-only test cannot tell the old
        # guard from the new one.
        case.progress.solution_feasible = feasible
        case.progress.solution_proposed = solution_proposed
        if cause_identified:
            case.progress.cause_state = CauseState.IDENTIFIED
        case.pending_transition = pending
        if causal_absence:
            # A qualifying gone=>gone confirmation: user-authored (not the
            # engine's M6 failed-fix disconfirmation) causal_absence row. This
            # is what actually flips assess_closure_readiness to SUGGEST_RESOLVE.
            case.evidence.append(
                Evidence(
                    category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
                    primary_purpose="confirm the cause was eliminated",
                    summary="After the provider client-ID correction the pods "
                    "obtained credentials and the AssumeRole failures stopped.",
                    source_type=EvidenceSourceType.USER_DESCRIPTION,
                    collected_by="user",
                    collected_at_turn=9,
                )
            )
        return case

    def test_no_proposal_when_cause_license_fell(self):
        # The monotone Solution record survives a license_lost withdrawal;
        # the close proposal must not cite it while no cause stands (INV-32).
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(
            feasible=SolutionFeasible.DEFERRED,
            solution_proposed=False,
            cause_identified=False,
        )
        case.solutions = [_solution()]
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert "transition_proposed_this_turn" not in meta
        assert case.pending_transition is None

    def test_no_proposal_when_feasible_now(self):
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(feasible=SolutionFeasible.NOW, solution_proposed=True)
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert "transition_proposed_this_turn" not in meta
        assert case.pending_transition is None

    def test_no_proposal_when_deferred_but_no_solution(self):
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=False)
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert "transition_proposed_this_turn" not in meta

    def test_proposes_close_when_deferred_with_solution(self):
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=True)
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert meta.get("transition_proposed_this_turn") is True
        assert case.pending_transition is not None
        assert case.pending_transition["to_state"] == "closed"
        assert meta.get("override_suggestions")

    def test_no_proposal_when_handshake_in_flight(self):
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(
            feasible=SolutionFeasible.DEFERRED,
            solution_proposed=True,
            pending={"to_state": "resolved"},
        )
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        # existing pending transition must not be clobbered
        assert case.pending_transition == {"to_state": "resolved"}
        assert "transition_proposed_this_turn" not in meta

    def test_pivots_to_resolve_when_a_confirmation_stands(self):
        """Resolve preservation: a deferred fix on a case carrying a gone=>gone
        confirmation is offered RESOLVED, not close-without-resolution.

        This proposer is one of three disposition paths; the LLM-proposal path
        and the confirm-time INV-37 guard both pivot on SUGGEST_RESOLVE. Before
        this it called propose_transition("closed") directly, so the engine
        could offer to discard an attribution its own eligibility scored
        resolvable.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )
        from faultmaven.core.investigation.terminal_transitions import (
            ClosureReadiness,
            assess_closure_readiness,
        )

        case = self._case(
            feasible=SolutionFeasible.DEFERRED,
            solution_proposed=True,
            causal_absence=True,
        )
        case.solutions = [_solution()]
        # Precondition asserted through the REAL predicate, not assumed: if the
        # pivot's trigger ever moves, this fails here rather than passing
        # vacuously below.
        assert (
            assess_closure_readiness(case).verdict == ClosureReadiness.SUGGEST_RESOLVE
        )

        meta = {}
        _maybe_propose_deferred_close(case, meta)

        assert case.pending_transition["to_state"] == "resolved"
        labels = [s["label"] for s in meta["override_suggestions"]]
        assert "Yes, mark as resolved" in labels
        assert "Yes, close this case" not in labels

    def test_close_branch_survives_without_a_confirmation(self):
        """The pivot must not swallow the ordinary deferred close: with no
        gone=>gone row the case is NOT resolution-grade and CLOSE is correct."""
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=True)
        case.solutions = [_solution()]
        meta = {}
        _maybe_propose_deferred_close(case, meta)

        assert case.pending_transition["to_state"] == "closed"
        labels = [s["label"] for s in meta["override_suggestions"]]
        assert "Yes, close this case" in labels

    @pytest.mark.parametrize("causal_absence", [False, True])
    def test_publishes_a_rationale_for_the_composer(self, causal_absence):
        """The engine-proposed disposition publishes its reason.

        The old key (``deferred_solution_closure_message``) was written and read
        NOWHERE, so the user saw a bare confirm/decline pair with no stated
        reason. The message must be non-empty and must match what the handshake
        is actually proposing.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(
            feasible=SolutionFeasible.DEFERRED,
            solution_proposed=True,
            causal_absence=causal_absence,
        )
        case.solutions = [_solution()]
        meta = {}
        _maybe_propose_deferred_close(case, meta)

        message = meta.get("deferred_solution_gate_message")
        assert message, "the proposal must publish its rationale for rendering"
        assert message == case.pending_transition["summary"]
        assert "deferred_solution_closure_message" not in meta

    def test_resolve_branch_does_not_reuse_the_pivot_from_close_prose(self):
        """The resolve branch needs its OWN sentence.

        `assess_closure_readiness().message` is a pivot-FROM-a-close text
        ("Closing would record it as unresolved..."), coherent only where a
        close was actually requested. This proposer offers the disposition
        unprompted, so reusing it would presuppose a close the user never made
        and never state the deferred-implementation reason — reintroducing, on
        the resolve branch, the prose/affordance incoherence this function was
        fixed to stop producing.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )
        from faultmaven.core.investigation.terminal_transitions import (
            assess_closure_readiness,
        )

        case = self._case(
            feasible=SolutionFeasible.DEFERRED,
            solution_proposed=True,
            causal_absence=True,
        )
        case.solutions = [_solution()]
        borrowed = assess_closure_readiness(case).message

        meta = {}
        _maybe_propose_deferred_close(case, meta)
        message = meta["deferred_solution_gate_message"]

        assert message != borrowed
        assert (
            "Closing would" not in message
        ), "the resolve branch presupposes a close the user never requested"
        # It must state the reason the engine is proposing anything at all.
        assert "out-of-band" in message
        assert "resolved" in message.lower()

    def test_decline_stops_the_re_proposal_loop(self):
        """A decline POSTPONES the offer instead of being forgotten.

        fm#1122: the proposer re-fired every turn while solution_feasible was
        DEFERRED. A decline clears pending_transition, the only state the
        guards read, so nothing carried the refusal forward — five identical
        offers against five explicit declines.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
            _record_deferred_disposition_decline,
        )
        from faultmaven.core.investigation.terminal_transitions import (
            cancel_pending_transition,
        )

        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=True)
        case.solutions = [_solution()]

        offers = 0
        for _ in range(10):
            meta = {}
            _maybe_propose_deferred_close(case, meta)
            if meta.get("transition_proposed_this_turn"):
                offers += 1
                _record_deferred_disposition_decline(case)
                cancel_pending_transition(case)

        assert offers == 1, f"offered {offers}x across 10 declined turns"

    def test_offer_returns_when_the_justifying_state_changes(self):
        """A decline must never permanently disarm the offer.

        Suppressing it for good would be engine-steered abandonment (D4): the
        case would carry a documented cause and fix with no route to a
        disposition. When a premise moves the offer is legitimate again.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
            _record_deferred_disposition_decline,
        )
        from faultmaven.core.investigation.terminal_transitions import (
            cancel_pending_transition,
        )

        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=True)
        case.solutions = [_solution()]

        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert meta.get("transition_proposed_this_turn") is True
        _record_deferred_disposition_decline(case)
        cancel_pending_transition(case)

        # Silent while nothing has changed.
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert "transition_proposed_this_turn" not in meta

        # A second solution lands — a premise moved, so the offer is live again.
        case.solutions = [_solution(), _solution("Raise the container limit")]
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert meta.get("transition_proposed_this_turn") is True

    def test_oscillating_justifying_state_does_not_re_arm_the_offer(self):
        """A signature the user already refused must stay refused when the
        case flips back into it.

        ``cause_identification_leg`` reads "chain" only while cause_state is
        IDENTIFIED — recomputed from the chain every turn — and falls back to
        "rcc" while an RCC stands. A case that flickers between the two
        alternates S1->S2->S1, so a single stored slot is evicted on every
        flip and the offer re-fires forever against a user who has refused
        both.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
            _record_deferred_disposition_decline,
        )
        from faultmaven.core.investigation.terminal_transitions import (
            cancel_pending_transition,
        )

        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=True)
        case.solutions = [_solution()]
        # An RCC gives the case a second, non-"chain" leg to fall back to
        # when cause_state is not IDENTIFIED.
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="The OIDC provider client ID does not match the "
            "audience the pods present.",
            confidence_level=ConfidenceLevel.VERIFIED,
            likelihood=0.9,
            mechanism="The pods present sts.amazonaws.com; the provider lists "
            "a different audience, so AssumeRole is rejected.",
        )
        case.progress.symptom_verified = True

        offers = 0
        for turn in range(6):
            # Flip the leg between "chain" (IDENTIFIED) and the RCC backstop.
            case.progress.cause_state = (
                CauseState.IDENTIFIED if turn % 2 == 0 else CauseState.CANDIDATES
            )
            meta = {}
            _maybe_propose_deferred_close(case, meta)
            if meta.get("transition_proposed_this_turn"):
                offers += 1
                _record_deferred_disposition_decline(case)
                cancel_pending_transition(case)

        # One offer per distinct justifying state, and never again after the
        # user has refused in it.
        assert offers <= 2, f"offered {offers}x across 6 refused turns"
        assert len(case.progress.deferred_disposition_declined_signatures) == offers

    def test_refused_signatures_are_bounded(self):
        """The refusal record is persisted in the progress blob, so it must
        not grow without limit on a case that keeps changing underneath the
        offer."""
        from faultmaven.core.investigation.milestone_engine import (
            _MAX_DECLINED_DISPOSITION_SIGNATURES,
            _record_deferred_disposition_decline,
        )

        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=True)
        for i in range(_MAX_DECLINED_DISPOSITION_SIGNATURES + 5):
            case.pending_transition = {
                "to_state": "closed",
                "summary": "offer",
                "justifying_signature": f"SUGGEST_CLOSE|{i}|chain",
            }
            _record_deferred_disposition_decline(case)

        declined = case.progress.deferred_disposition_declined_signatures
        assert len(declined) == _MAX_DECLINED_DISPOSITION_SIGNATURES
        # Oldest dropped first: the most recent refusals are the ones most
        # likely to recur.
        assert declined[-1] == (
            f"SUGGEST_CLOSE|{_MAX_DECLINED_DISPOSITION_SIGNATURES + 4}|chain"
        )

    def test_resolve_pivot_keeps_the_offer_refusable(self):
        """The INV-37 pivot must not launder an engine offer into an
        anonymous one.

        ``confirm_pending_transition`` replaces the pending dict wholesale via
        ``propose_transition``, so the provenance the refusal recorder reads
        was dropped: engine proposes CLOSED -> a qualifying causal-absence row
        lands -> the user confirms -> the pivot presents RESOLVED -> the user
        refuses THAT -> nothing is recorded and the offer returns next turn.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
            _record_deferred_disposition_decline,
        )
        from faultmaven.core.investigation.terminal_transitions import (
            cancel_pending_transition,
            confirm_pending_transition,
        )

        # No causal absence yet: the engine offers the CLOSE.
        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=True)
        case.solutions = [_solution()]
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert case.pending_transition["to_state"] == "closed"

        # A qualifying gone=>gone row lands after the offer was made.
        case.evidence.append(
            Evidence(
                category=EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
                primary_purpose="confirm the cause was eliminated",
                summary="After the provider client-ID correction the pods "
                "obtained credentials and the AssumeRole failures stopped.",
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                collected_by="user",
                collected_at_turn=9,
            )
        )

        # Confirm pivots CLOSED -> RESOLVED without committing anything.
        executed = confirm_pending_transition(case, "user_test")
        assert executed is False
        assert case.pending_transition["to_state"] == "resolved"

        # The user refuses the pivoted offer. It is still the engine's offer,
        # so the refusal must stick — against the verdict that now holds.
        _record_deferred_disposition_decline(case)
        cancel_pending_transition(case)
        assert case.progress.deferred_disposition_declined_signatures != []

        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert "transition_proposed_this_turn" not in meta

    def test_decline_of_another_proposers_offer_is_not_recorded(self):
        """A decline of an LLM- or user-initiated disposition says nothing
        about the engine-initiated one, so it must not suppress it."""
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
            _record_deferred_disposition_decline,
        )

        case = self._case(feasible=SolutionFeasible.DEFERRED, solution_proposed=True)
        case.solutions = [_solution()]
        # An LLM- or user-initiated disposition: no `justifying_signature`,
        # because this proposer is its only writer. That absence is the whole
        # discriminator, so the fixture must not smuggle one in.
        case.pending_transition = {"to_state": "closed", "summary": "LLM proposed"}
        assert "justifying_signature" not in case.pending_transition

        _record_deferred_disposition_decline(case)
        assert case.progress.deferred_disposition_declined_signatures == []

        case.pending_transition = None
        meta = {}
        _maybe_propose_deferred_close(case, meta)
        assert meta.get("transition_proposed_this_turn") is True

    def test_no_proposal_from_inquiry(self):
        """INQUIRY is the state the new guard actually adds.

        "closed" was a legal edge from any state; "resolved" is not one from
        INQUIRY, and a proposal that cannot execute leaves pending_transition
        standing so every later confirm turn fails identically. The terminal
        case below does NOT pin this — `is_terminal` already rejected it — so
        without this case the guard is asserted by its sibling and tested by
        neither.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(
            feasible=SolutionFeasible.DEFERRED,
            solution_proposed=True,
            causal_absence=True,
            inquiry=True,
        )
        case.solutions = [_solution()]
        assert case.state == CaseState.INQUIRY
        assert (
            case.progress.solution_feasible == SolutionFeasible.DEFERRED
        ), "the fixture must arm the trigger, or this passes at the wrong guard"

        meta = {}
        _maybe_propose_deferred_close(case, meta)

        assert case.pending_transition is None
        assert meta == {}

    def test_no_proposal_outside_investigating(self):
        """The proposal target is state-dependent now, so the state is guarded.

        "closed" was a legal edge from any state; "resolved" is not one from
        INQUIRY, and a proposal that cannot execute leaves `pending_transition`
        standing so every later confirm turn fails identically. Exercises the
        previously-dead ``terminal=True`` fixture path.
        """
        from faultmaven.core.investigation.milestone_engine import (
            _maybe_propose_deferred_close,
        )

        case = self._case(
            feasible=SolutionFeasible.DEFERRED,
            solution_proposed=True,
            terminal=True,
        )
        case.solutions = [_solution()]
        meta = {}
        _maybe_propose_deferred_close(case, meta)

        assert case.pending_transition is None
        assert "transition_proposed_this_turn" not in meta
        assert "deferred_solution_gate_message" not in meta


class TestStructuredOutputDegradation:
    """Follow-on C: parse-time cross-field validation errors degrade gracefully
    (prune the offending sub-record / conversational fallback) instead of 500ing.
    """

    def _engine(self):
        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        return MilestoneEngine.__new__(MilestoneEngine)

    def test_prunes_invalid_evidence_keeps_valid(self):
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {
            "agent_response": "Here is my analysis.",
            "state_updates": {
                "evidence_to_add": [
                    # invalid: source_type=text without source_file_id (the S4 trigger)
                    {
                        "summary": "bad",
                        "extract": "x",
                        "category": "causal_evidence",
                        "source_type": "text",
                    },
                    # valid: source_file_id present
                    {
                        "summary": "good",
                        "extract": "y",
                        "category": "symptom_evidence",
                        "source_type": "text",
                        "source_file_id": "file_123",
                    },
                ]
            },
        }
        parsed = eng._validate_with_degradation(
            content, InvestigationResponse_Diagnosis
        )
        assert parsed.agent_response == "Here is my analysis."
        # the invalid entry is pruned; the valid one survives
        summaries = [e.summary for e in parsed.state_updates.evidence_to_add]
        assert summaries == ["good"]

    def test_valid_response_passes_unchanged(self):
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {"agent_response": "All good.", "state_updates": {}}
        parsed = eng._validate_with_degradation(
            content, InvestigationResponse_Diagnosis
        )
        assert parsed.agent_response == "All good."

    def test_conversational_fallback_drops_state_updates(self):
        # All evidence entries invalid -> after pruning, state_updates still has
        # the (now empty) list; the turn survives with the response text intact.
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {
            "agent_response": "Survives as conversation.",
            "state_updates": {
                "evidence_to_add": [
                    {
                        "summary": "bad",
                        "extract": "x",
                        "category": "causal_evidence",
                        "source_type": "text",
                    }
                ]
            },
        }
        parsed = eng._validate_with_degradation(
            content, InvestigationResponse_Diagnosis
        )
        assert parsed.agent_response == "Survives as conversation."
        assert parsed.state_updates.evidence_to_add == []

    def test_synthesizes_missing_agent_response_keeps_state(self, caplog):
        # gemini-3.5-flash sometimes omits the required agent_response ITSELF on
        # resolution turns. The rungs above preserve agent_response, so they
        # cannot help; this rung synthesizes a placeholder and KEEPS the model's
        # otherwise-valid state_updates instead of 500ing.
        import logging

        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {
            # no agent_response at all
            "state_updates": {
                "evidence_to_add": [
                    {
                        "summary": "good",
                        "extract": "y",
                        "category": "symptom_evidence",
                        "source_type": "text",
                        "source_file_id": "file_123",
                    }
                ]
            },
        }
        with caplog.at_level(
            logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
        ):
            parsed = eng._validate_with_degradation(
                content, InvestigationResponse_Diagnosis
            )
        assert parsed.agent_response  # a non-empty placeholder was synthesized
        # the model's valid work survives — state_updates are NOT dropped here
        summaries = [e.summary for e in parsed.state_updates.evidence_to_add]
        assert summaries == ["good"]
        assert any(
            "synthesized missing agent_response" in r.getMessage()
            for r in caplog.records
        )

    def test_synthesizes_missing_agent_response_drops_invalid_state(self):
        # Both gaps at once (the turn-7 500 shape): no agent_response AND an
        # unrepairable state_updates error. The turn must still survive — the
        # placeholder is synthesized and the bad state is dropped, never a 500.
        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {"state_updates": {"evidence_to_add": "should-be-a-list"}}
        parsed = eng._validate_with_degradation(
            content, InvestigationResponse_Diagnosis
        )
        assert parsed.agent_response  # placeholder synthesized, no 500

    def test_fallback_logs_non_prunable_errors(self, caplog):
        # A NON-prunable error (loc has no list index) forces the conversational
        # fallback; that branch must log the offending loc/msg so each fallback is
        # self-diagnosing (S4 observability). Here evidence_to_add is the wrong
        # TYPE (a string, not a list) -> loc ('state_updates','evidence_to_add')
        # has no int index -> not prunable.
        import logging

        from faultmaven.core.investigation.schemas import (
            InvestigationResponse_Diagnosis,
        )

        eng = self._engine()
        content = {
            "agent_response": "hi",
            "state_updates": {"evidence_to_add": "should-be-a-list"},
        }
        with caplog.at_level(
            logging.WARNING, logger="faultmaven.core.investigation.milestone_engine"
        ):
            parsed = eng._validate_with_degradation(
                content, InvestigationResponse_Diagnosis
            )
        assert parsed.agent_response == "hi"  # turn survives, no 500
        degraded = [
            r for r in caplog.records if "structured_output_degraded" in r.getMessage()
        ]
        assert degraded, "fallback must emit a degraded warning"
        non_prunable = getattr(degraded[0], "non_prunable_errors", None)
        assert non_prunable, "fallback must log the non-prunable errors"
        assert any("evidence_to_add" in loc for loc, _msg in non_prunable)
