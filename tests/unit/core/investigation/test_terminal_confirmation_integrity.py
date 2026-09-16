"""Terminal-confirmation integrity tests (#722 / #721 / #787).

One principle, three enforcement points: an irreversible RESOLVED/CLOSED
executes only on an explicit, bare user confirmation given on a turn AFTER
the proposal — and nothing may fabricate that consent (or per-action
compliance) from a message that doesn't carry it.

- #722: no same-turn confirm — pinned in ``test_lifecycle_invariants.py``
  (INV-06 tests). This file covers the shared substance predicate the
  handshake's confirm lanes rely on.
- #721 (widened by fm#918): the IntentResolver classifier tier must not mint
  GATE consent from substantive typed text
  (``_minted_intent_swallows_gate_consent``) — the pending terminal
  transition, and INQUIRY's Gate 1.
- #787: user-confirmed resolution must not stamp never-run pending
  ProposedActions as executed.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from faultmaven.core.investigation.terminal_transitions import (
    BARE_CONSENT_MAX_LENGTH,
    _execute_resolved_transition,
    is_substantive_reply,
)
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    InquiryData,
    InvestigationActionType,
    InvestigationProgress,
    ProblemVerification,
    ProposedAction,
    Solution,
    SolutionOutcome,
    SolutionType,
    classify_solution_outcome,
)

pytestmark = pytest.mark.unit


def _make_investigating_case() -> Case:
    case = Case(
        case_id="case_00000000abcd",
        title="Terminal confirmation integrity",
        state=CaseState.INQUIRY,
        user_id="user_test",
        enterprise_id="org_test",
        description="Terminal confirmation integrity test",
        problem_verification=ProblemVerification(
            symptom_statement="Test symptom",
            severity="HIGH",
            temporal_state="ongoing",
            urgency_level="high",
        ),
        inquiry=InquiryData(thread_id="thread_test"),
    )
    case.inquiry.proposed_problem_statement = "Test problem"
    case.inquiry.problem_statement_confirmed = True
    case.inquiry.problem_statement_confirmed_at = datetime.now(timezone.utc)
    case.inquiry.decided_to_investigate = True
    case.inquiry.decision_made_at = datetime.now(timezone.utc)
    case.state = CaseState.INVESTIGATING
    case.progress = InvestigationProgress()
    return case


# =============================================================================
# Shared INV-26 substance predicate (is_substantive_reply)
# =============================================================================


class TestIsSubstantiveReply:
    """The single source of truth for the confirm-side substance test."""

    @pytest.mark.parametrize(
        "message",
        [
            "yes but what about the replication lag?",  # the #721 reproducer
            "ok but the disk is still filling",  # contrastive continuation
            "did you see anything wrong?",  # question
            "yes, and also " + "x" * BARE_CONSENT_MAX_LENGTH,  # too long
            "sounds right but",  # trailing contrastive
        ],
    )
    def test_substantive_messages_detected(self, message):
        assert is_substantive_reply(message) is True

    @pytest.mark.parametrize(
        "message",
        [
            "yes",
            "Yes, mark as resolved",
            "affirmative",  # bare but NOT in the confirm-pattern list —
            # exactly the phrasing the classifier tier exists to catch
            "go ahead",
        ],
    )
    def test_bare_replies_are_not_substantive(self, message):
        assert is_substantive_reply(message) is False

    def test_empty_and_none_are_not_substantive(self):
        # Not substantive — and not consent either; callers reject
        # empties separately.
        assert is_substantive_reply("") is False
        assert is_substantive_reply(None) is False

    def test_parity_with_typed_confirmation_matcher(self):
        """The typed-pattern lane (_user_confirms_transition) and the
        minted-intent lane must reject the same substantive message —
        the predicate is shared precisely so they cannot drift."""
        from faultmaven.core.investigation.milestone_engine import MilestoneEngine

        engine = MilestoneEngine(
            MagicMock(), MagicMock(), investigation_tools=MagicMock()
        )
        msg = "yes but what about the replication lag?"
        assert is_substantive_reply(msg) is True
        assert engine._user_confirms_transition(msg) is False


# =============================================================================
# #721 — classifier-minted intents must not swallow terminal consent
# =============================================================================


def _pending_resolve(case: Case) -> None:
    case.pending_transition = {
        "to_state": "resolved",
        "summary": "Confirm resolution?",
        "evidence_ids": [],
        "proposed_at": datetime.now(timezone.utc).isoformat(),
    }


class TestMintedIntentTerminalConsentGuard:
    GUARD = staticmethod(InvestigationService._minted_intent_swallows_gate_consent)

    def test_substantive_confirmation_mint_is_rejected(self):
        """The #721 reproducer: typed contrastive text classifier-matched
        to "Yes, mark as resolved" must NOT confirm the pending resolve."""
        case = _make_investigating_case()
        _pending_resolve(case)
        minted = QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=True)
        assert (
            self.GUARD(case, minted, "yes but what about the replication lag?") is True
        )

    def test_bare_unlisted_confirmation_mint_adopts(self):
        """A bare phrasing outside the pattern list ("affirmative") is the
        classifier tier's legitimate value — it must still adopt."""
        case = _make_investigating_case()
        _pending_resolve(case)
        minted = QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=True)
        assert self.GUARD(case, minted, "affirmative") is False

    def test_no_pending_transition_and_no_gate_one_adopts(self):
        """With no pending transition AND no Gate 1 to commit, a minted
        confirmation reaches the engine's section 0c and does nothing there
        (it logs "status is investigating" and falls through). Nothing to
        guard."""
        case = _make_investigating_case()
        assert case.state == CaseState.INVESTIGATING
        minted = QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=True)
        assert (
            self.GUARD(case, minted, "yes but what about the replication lag?") is False
        )

    def test_decline_mint_is_not_guarded(self):
        """Declines only cancel the proposal (reversible) and the engine's
        escape lane still processes the substantive message."""
        case = _make_investigating_case()
        _pending_resolve(case)
        minted = QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=False)
        assert self.GUARD(case, minted, "no but wait — what about the cache?") is False

    def test_status_transition_mint_matching_pending_is_guarded(self):
        """A minted status_transition to the pending target is an implicit
        confirmation in the engine (repeated-intent rule) — same guard."""
        case = _make_investigating_case()
        _pending_resolve(case)
        minted = QueryIntent(
            type=IntentType.STATUS_TRANSITION, to_state=CaseState.RESOLVED
        )
        assert (
            self.GUARD(case, minted, "resolve it but first tell me the root cause?")
            is True
        )

    def test_status_transition_mint_to_other_target_adopts(self):
        """A contradicting status_transition merely cancels the pending
        (reversible) — the guard must not block it."""
        case = _make_investigating_case()
        _pending_resolve(case)
        minted = QueryIntent(
            type=IntentType.STATUS_TRANSITION, to_state=CaseState.INVESTIGATING
        )
        assert (
            self.GUARD(case, minted, "keep investigating? I found new errors") is False
        )


class TestMintedIntentGateOneConsentGuard:
    """fm#918 exposure 3 — the SECOND gate a minted confirmation can commit.

    #721 scoped this guard to a pending TERMINAL transition and recorded the
    reason: a mint with no pending transition "cannot execute a terminal
    transition". True, and it is not the whole question. With no pending
    transition the minted ``confirmation`` reaches the engine's section 0c,
    which on an INQUIRY case carrying a proposed problem statement sets
    ``problem_statement_confirmed`` + ``decided_to_investigate`` — Gate 1 —
    and ``_check_automatic_transitions`` then fires INQUIRY → INVESTIGATING.

    The suggestions that reach this are the engine's own
    ``_investigation_confirmation_suggestions`` pair, so the classifier is
    choosing between "Yes, let's investigate" and "Not quite, let me clarify"
    against whatever the user typed — which is how a question about the
    problem statement started the investigation off it.
    """

    GUARD = staticmethod(InvestigationService._minted_intent_swallows_gate_consent)

    @staticmethod
    def _inquiry_awaiting_gate_one() -> Case:
        case = _make_investigating_case()
        case.state = CaseState.INQUIRY
        case.inquiry.problem_statement_confirmed = False
        case.inquiry.problem_statement_confirmed_at = None
        case.inquiry.decided_to_investigate = False
        case.inquiry.decision_made_at = None
        case.pending_transition = None
        return case

    def test_bare_consent_still_commits_gate_one(self):
        """The positive control. A bare "yes" is what the affordance is FOR;
        if this were guarded the tier would be dead rather than corrected."""
        case = self._inquiry_awaiting_gate_one()
        minted = QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=True)
        assert self.GUARD(case, minted, "yes") is False
        assert self.GUARD(case, minted, "affirmative") is False

    @pytest.mark.parametrize(
        "message",
        [
            # Noun-sharing negatives first: each reuses the vocabulary of the
            # case and of the two offered choices, which is where the
            # classifier's false positives live.
            "yes but the errors started after the deploy, not before",
            "correct — is the problem statement about the replica or the primary?",
            "let's investigate, but is the symptom the lag or the errors?",
            "that's right about the deploy — but which replica?",
        ],
    )
    def test_substantive_reply_does_not_commit_gate_one(self, message):
        case = self._inquiry_awaiting_gate_one()
        minted = QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=True)
        assert is_substantive_reply(message) is True, (
            "the shared predicate must read this as substantive, or this test "
            "is asserting the guard for a reason the guard does not have"
        )
        assert self.GUARD(case, minted, message) is True

    def test_the_decline_arm_is_guarded_too(self):
        """``confirmation_value`` is not consulted on this arm, because the
        ENGINE does not consult it: section 0c commits Gate 1 for a minted
        ``confirmation`` whatever the value says. Pinned so that if the engine
        learns to decline, this test is what says the guard may narrow."""
        case = self._inquiry_awaiting_gate_one()
        minted = QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=False)
        assert (
            self.GUARD(case, minted, "not quite — is it the replica or the primary?")
            is True
        )

    def test_a_case_with_no_proposed_statement_has_no_gate_to_commit(self):
        case = self._inquiry_awaiting_gate_one()
        case.inquiry.proposed_problem_statement = None
        minted = QueryIntent(type=IntentType.CONFIRMATION, confirmation_value=True)
        assert (
            self.GUARD(case, minted, "yes but what about the replication lag?") is False
        )

    def test_a_minted_status_transition_is_not_a_gate_one_commit(self):
        """Only ``confirmation`` reaches the 0c branch that commits Gate 1;
        a status_transition with no pending goes to the transition handler,
        which is a different question and not widened here."""
        case = self._inquiry_awaiting_gate_one()
        minted = QueryIntent(
            type=IntentType.STATUS_TRANSITION, to_state=CaseState.INVESTIGATING
        )
        assert (
            self.GUARD(case, minted, "keep investigating? I found new errors") is False
        )


# =============================================================================
# #787 — resolution must not stamp never-run pending actions as executed
# =============================================================================


class TestNoBlanketAcceptAtResolution:
    def _case_with_pending_solution_offer(self) -> tuple[Case, Solution]:
        case = _make_investigating_case()
        solution = Solution(
            case_id=case.case_id,
            solution_type=SolutionType.CONFIG_CHANGE,
            title="Raise pool size",
            immediate_action="Raise the connection pool size",
            commands=["kubectl set env deploy/api DB_POOL_SIZE=50"],
            proposed_in_turn=3,
        )
        case.solutions.append(solution)
        case.proposed_actions.append(
            ProposedAction(
                case_id=case.case_id,
                action_type=InvestigationActionType.SOLUTION,
                description="Raise the connection pool size",
                commands=["kubectl set env deploy/api DB_POOL_SIZE=50"],
                proposed_in_turn=3,
            )
        )
        return case, solution

    def test_pending_action_stays_pending_on_confirmed_resolution(self):
        """Out-of-band resolution: the user confirms RESOLVED without ever
        running the standing SOLUTION offer. The offer must stay pending —
        no fabricated 'accepted' stamp, no fabricated ActionAttempt."""
        case, _ = self._case_with_pending_solution_offer()

        _execute_resolved_transition(case, user_id="user_test")

        assert case.state == CaseState.RESOLVED
        action = case.proposed_actions[0]
        assert action.state == "pending", (
            "#787: user-confirmed resolution stamped a never-run pending "
            "ProposedAction as accepted — consent to the case-level "
            "transition carries no per-action execution signal."
        )
        assert case.action_attempts == [], (
            "#787: a fabricated full-confidence ActionAttempt was created "
            "for an action the user never reported executing."
        )

    def test_never_run_offer_classifies_proposed_not_applied(self):
        """R5 boundary: the never-run offer must reach runbook conversion
        as PROPOSED (surfaced, flagged unconfirmed) — never APPLIED."""
        case, solution = self._case_with_pending_solution_offer()

        _execute_resolved_transition(case, user_id="user_test")

        outcome = classify_solution_outcome(solution, case.proposed_actions)
        assert outcome == SolutionOutcome.PROPOSED, (
            "#787: a never-run offer classified as "
            f"{outcome} — a generated runbook would claim the user "
            "executed a fix they never ran."
        )

    def test_case_level_gate_milestones_still_latch(self):
        """The settled FM-trusts-resolution-claim design is case-level and
        unchanged: confirming resolution still latches the case-level
        solution gate milestones — only the per-action stamps are gone."""
        case, _ = self._case_with_pending_solution_offer()

        _execute_resolved_transition(case, user_id="user_test")

        assert case.progress.solution_proposed is True
        assert case.progress.solution_accepted is True
        assert case.progress.solution_verified is True
