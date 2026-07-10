"""INV-33 (#656): Zone-3-pending exit conditions.

A pending solution proposal is a NON-suppressive hold, not a freeze on the
investigation. Two coupled properties:

  (A) Prompt de-absolutization — the DIAGNOSIS-stage focus emphasis and the
      standby suggestion clamp no longer forbid further evidence / diagnostic
      asks outright; they name the diagnostic exit (new evidence, a dispute, a
      competing cause reopens root-cause analysis). The Zone-3-pending emphasis
      renders ONLY while the solution is pending in DIAGNOSIS — once accepted
      the stage is TREATMENT and it does not render.

  (C) Pending-action hygiene — when a pending SOLUTION is WITHDRAWN on license
      loss, a stale DIAGNOSTIC ask it shadowed is retired so it cannot resurface
      as the <pending_action> compliance target. A pending MITIGATION is
      cause-independent (INV-32) and survives; a DIAGNOSTIC proposed AFTER the
      offer is a live thread and survives.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from faultmaven.core.investigation.milestone_engine import (
    _recompute_assessment_state,
)
from faultmaven.core.investigation.prompts.templates import (
    _RCA_DIAGNOSIS_BLOCK,
    _get_diagnosis_focus_emphasis,
    get_prompt_for_case,
)
from faultmaven.modules.case.domain.models import (
    CauseState,
    InvestigationActionType,
    InvestigationStage,
    ProposedAction,
    SolutionState,
)

# Reuse the INV-32 liveness fixtures (same call site, same established-cause
# license construction).
from tests.unit.core.investigation.test_solution_offer_liveness import (
    _make_case,
    _make_engine,
    _pending_solutions,
    _solution_updates,
    _workaround_updates,
)

pytestmark = pytest.mark.unit


def _diagnostic(
    case, *, turn: int, desc: str = "run kubectl get pods"
) -> ProposedAction:
    action = ProposedAction(
        case_id=case.case_id,
        action_type=InvestigationActionType.DIAGNOSTIC,
        description=desc,
        commands=["kubectl get pods"],
        proposed_in_turn=turn,
    )
    case.proposed_actions.append(action)
    return action


# ---------------------------------------------------------------------------
# (A) Prompt de-absolutization + zone exit
# ---------------------------------------------------------------------------


class TestZonePendingEmphasis:
    def _pending_progress(self):
        # symptom verified, cause identified, solution pending → the else branch.
        from faultmaven.modules.case.domain.models import InvestigationProgress

        p = InvestigationProgress()
        p.symptom_verified = True
        p.cause_state = CauseState.IDENTIFIED
        p.solution_proposed = True
        return p

    def test_pending_emphasis_drops_absolutist_prohibition(self):
        text = _get_diagnosis_focus_emphasis(self._pending_progress())
        # The suppressive frame that stranded the #656 diagnostic thread is gone.
        assert "Do not request further evidence" not in text
        assert "introduce alternative proposals" not in text

    def test_pending_emphasis_names_the_diagnostic_exit(self):
        text = _get_diagnosis_focus_emphasis(self._pending_progress())
        low = text.lower()
        assert "not a freeze" in low
        assert "resume root-cause analysis" in low
        # Still points at the compliance path.
        assert "solution_accepted" in text

    def test_rca_block_drops_exactly_two_suggestions_clamp(self):
        # The no-parallel-suggestions clamp that forbade diagnostic asks while
        # awaiting compliance is gone; the reopen instruction is present.
        assert (
            "offer exactly two suggestions — and no others" not in _RCA_DIAGNOSIS_BLOCK
        )
        assert "This hold is not absolute" in _RCA_DIAGNOSIS_BLOCK


class TestTreatmentDoesNotRenderPendingEmphasis:
    async def _accepted_case(self):
        case = _make_case()
        await _make_engine()._apply_investigation_updates(
            case, _solution_updates(), _meta()
        )
        offer = _pending_solutions(case)[0]
        offer.state = "accepted"
        case.progress.solution_accepted = True
        return case

    @pytest.mark.asyncio
    async def test_accepted_solution_moves_to_treatment_stage(self):
        case = await self._accepted_case()
        # solution_accepted & not verified → the derived stage is TREATMENT.
        assert case.current_stage == InvestigationStage.TREATMENT

    @pytest.mark.asyncio
    async def test_treatment_prompt_omits_zone_pending_frame(self):
        case = await self._accepted_case()
        prompt = get_prompt_for_case(case, user_message="I ran it")
        # The Zone-3-pending emphasis is DIAGNOSIS-only — it must not bleed into
        # the TREATMENT verify-the-fix prompt.
        assert "awaiting execution" not in prompt
        assert "FOCUS: TREATMENT" in prompt


# ---------------------------------------------------------------------------
# (C) Pending-action hygiene on license-loss withdrawal
# ---------------------------------------------------------------------------


def _meta() -> dict:
    return {
        "milestones_completed": [],
        "evidence_added": [],
        "hypotheses_generated": [],
        "hypotheses_validated": [],
        "solutions_proposed": [],
        "evidence_needs_updated": [],
        "progress_made": False,
        "status_transitioned": False,
    }


@pytest.mark.asyncio
class TestPendingActionHygiene:
    async def _case_with_offer(self):
        case = _make_case()
        await _make_engine()._apply_investigation_updates(
            case, _solution_updates(), _meta()
        )
        assert case.progress.solution_proposed is True
        return case

    async def test_stale_diagnostic_retired_on_withdrawal(self):
        # A DIAGNOSTIC ask proposed BEFORE the solution (turn 3 < the offer's
        # turn 5) is shadowed; on license-loss withdrawal it must be retired so
        # it cannot resurface in <pending_action>.
        case = await self._case_with_offer()
        stale = _diagnostic(case, turn=3)
        case.root_cause_conclusion = None  # license falls
        case.current_turn = 7
        _recompute_assessment_state(case, metadata={})

        assert _pending_solutions(case) == []  # solution withdrawn
        assert stale.state == "superseded"
        assert stale.superseded_reason == "stale_pending"
        assert stale.superseded_in_turn == 7

    async def test_diagnostic_after_offer_survives(self):
        # A DIAGNOSTIC proposed AFTER the offer is a live thread, not shadowed.
        case = await self._case_with_offer()
        offer_turn = _pending_solutions(case)[0].proposed_in_turn
        live = _diagnostic(case, turn=offer_turn + 1, desc="run dig +short")
        case.root_cause_conclusion = None
        case.current_turn = offer_turn + 2
        _recompute_assessment_state(case, metadata={})

        assert live.state == "pending"
        assert live.superseded_reason is None

    async def test_pending_mitigation_survives_withdrawal(self):
        # INV-32 boundary held: a cause-independent workaround is not retired by
        # solution withdrawal even though it predates the offer.
        case = _make_case()
        eng = _make_engine()
        await eng._apply_investigation_updates(case, _workaround_updates(), _meta())
        await eng._apply_investigation_updates(case, _solution_updates(), _meta())
        case.root_cause_conclusion = None
        _recompute_assessment_state(case, metadata={})

        assert _pending_solutions(case) == []
        assert any(
            a.action_type == InvestigationActionType.MITIGATION and a.state == "pending"
            for a in case.proposed_actions
        )

    async def test_counter_increments_per_retired_diagnostic(self):
        case = await self._case_with_offer()
        _diagnostic(case, turn=2, desc="a")
        _diagnostic(case, turn=3, desc="b")
        case.root_cause_conclusion = None
        with patch(
            "faultmaven.core.investigation.milestone_engine."
            "pending_action_superseded_stale_total"
        ) as counter:
            _recompute_assessment_state(case, metadata={})
        assert counter.inc.call_count == 2

    async def test_no_withdrawal_leaves_diagnostic_untouched(self):
        # Standing license → no solution withdrawal → no stale retirement.
        case = await self._case_with_offer()
        stale = _diagnostic(case, turn=3)
        _recompute_assessment_state(case, metadata={})  # license stands
        assert _pending_solutions(case) != []
        assert stale.state == "pending"
