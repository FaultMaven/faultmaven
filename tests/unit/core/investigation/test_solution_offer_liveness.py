"""INV-32: ``solution_proposed`` reflects a LIVE fix offer, never a latch.

P2.1 of the #656 systemic correction (DF-3, the frame latch): the indicator is
engine-DERIVED at the end-of-turn assessment recompute from SOLUTION-typed
``ProposedAction`` liveness (``pending``/``accepted``) plus the forward-only
gate ladder (``solution_accepted``/``solution_verified``). Two supersession
events end a pending offer's liveness:

- **reproposal** — a new SOLUTION offer supersedes every prior pending one
  (the newest proposal IS the offer).
- **license_lost** — the M5 established-cause license
  (``_solution_cause_validated`` → the shared ``_cause_identified`` predicate)
  is re-checked each recompute; a pending offer whose license fell (M6
  demotion, conclusion retraction, MECE hold) is WITHDRAWN, with
  ``system_feedback`` to the LLM.

ACCEPTED offers are historical facts and never withdrawn (their truth surface
is the M6 counterfactual machinery). MITIGATION/DIAGNOSTIC actions never feed
the indicator and are never superseded by these events.

Derivation-level pins (ladder coherence, stale-flag flip-off) live in
``test_surgical_strip_and_cause_state.py::TestSolutionStateDerivation``; the
M5 creation gate in ``test_solution_validation_gate.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _live_solution_offer_exists,
    _recompute_assessment_state,
    _supersede_pending_solution_offers,
    _withdraw_unlicensed_solution_offers,
)
from faultmaven.core.investigation.prompts.templates import (
    _get_diagnosis_focus_emphasis,
)
from faultmaven.core.investigation.schemas import SolutionToAdd
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InvestigationActionType,
    ProposedAction,
    RootCauseConclusion,
    SolutionState,
    SolutionType,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures (mirror test_solution_validation_gate.py — same call site)
# ---------------------------------------------------------------------------


def _make_case(*, established: bool = True) -> Case:
    """INVESTIGATING case; ``established=True`` gives it the anchored-RCC
    established-cause license (survives the empty-graph chain recompute,
    unlike a flat cause_state=IDENTIFIED)."""
    case = Case(
        user_id="u1",
        organization_id="o1",
        title="t",
        description="pods crashing",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="pods crashing",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
    )
    case.current_turn = 5
    if established:
        case.progress.symptom_verified = True
        case.evidence.append(
            Evidence(
                evidence_id="ev_000000000001",
                summary="observed failure",
                content_ref="x.log",
                category=EvidenceCategory.SYMPTOM_EVIDENCE,
                source_type=EvidenceSourceType.USER_DESCRIPTION,
                collected_at=datetime.now(UTC),
                collected_by="user",
                primary_purpose="symptom",
                collected_at_turn=1,
            )
        )
        case.root_cause_conclusion = RootCauseConclusion(
            root_cause="the connection pool is exhausted",
            mechanism="all pool slots are held by stuck queries",
            likelihood=0.8,
            confidence_level=ConfidenceLevel.from_score(0.8),
        )
    return case


def _make_engine() -> MilestoneEngine:
    eng = MilestoneEngine.__new__(MilestoneEngine)
    eng._apply_chain_emission = lambda *a, **k: None
    eng._nudge_ambiguous_orphan_chains = lambda *a, **k: None
    return eng


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


class _Updates:
    def __init__(self, **set_fields):
        self.__dict__.update(set_fields)

    def __getattr__(self, name):
        return None


def _solution_updates(description: str = "Apply the permanent fix") -> _Updates:
    return _Updates(
        solutions_to_add=[
            SolutionToAdd(
                description=description,
                solution_type=SolutionType.CODE_FIX,
                estimated_impact="resolves the failure",
                risks="low",
                commands=["kubectl apply -f fix.yaml"],
            )
        ]
    )


def _workaround_updates() -> _Updates:
    return _Updates(
        solutions_to_add=[
            SolutionToAdd(
                description="Restart the pods to relieve pressure",
                solution_type=SolutionType.WORKAROUND,
                estimated_impact="temporary relief",
                risks="low",
                commands=["kubectl rollout restart deploy/app"],
            )
        ]
    )


def _pending_solutions(case: Case) -> list[ProposedAction]:
    return [
        a
        for a in case.proposed_actions
        if a.action_type == InvestigationActionType.SOLUTION and a.state == "pending"
    ]


# ---------------------------------------------------------------------------
# Derivation through the real apply pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDerivationThroughApply:
    async def test_offer_creation_derives_true_same_turn(self):
        case = _make_case()
        await _make_engine()._apply_investigation_updates(
            case, _solution_updates(), _meta()
        )
        assert len(_pending_solutions(case)) == 1
        assert case.progress.solution_proposed is True
        assert case.progress.solution_state == SolutionState.SELECTED

    async def test_reproposal_supersedes_prior_pending_offer(self):
        case = _make_case()
        eng = _make_engine()
        await eng._apply_investigation_updates(case, _solution_updates(), _meta())
        case.current_turn = 6
        await eng._apply_investigation_updates(
            case, _solution_updates("Apply the corrected fix"), _meta()
        )

        pending = _pending_solutions(case)
        assert len(pending) == 1
        assert pending[0].description == "Apply the corrected fix"
        old = [a for a in case.proposed_actions if a.state == "superseded"]
        assert len(old) == 1
        assert old[0].superseded_reason == "reproposal"
        assert old[0].superseded_in_turn == 6
        assert case.progress.solution_proposed is True

    async def test_two_offers_same_turn_leave_exactly_one_pending(self):
        case = _make_case()
        updates = _Updates(
            solutions_to_add=[
                _solution_updates("fix A").solutions_to_add[0],
                _solution_updates("fix B").solutions_to_add[0],
            ]
        )
        await _make_engine()._apply_investigation_updates(case, updates, _meta())
        pending = _pending_solutions(case)
        assert len(pending) == 1
        assert pending[0].description == "fix B"

    async def test_mitigation_never_feeds_solution_proposed(self):
        case = _make_case()  # symptom evidence present → no 3D downgrade
        await _make_engine()._apply_investigation_updates(
            case, _workaround_updates(), _meta()
        )
        assert any(
            a.action_type == InvestigationActionType.MITIGATION and a.state == "pending"
            for a in case.proposed_actions
        )
        assert case.progress.solution_proposed is False
        assert case.progress.solution_state == SolutionState.UNKNOWN


# ---------------------------------------------------------------------------
# License re-check (withdrawal) at the recompute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLicenseLostWithdrawal:
    async def _case_with_offer(self) -> Case:
        case = _make_case()
        await _make_engine()._apply_investigation_updates(
            case, _solution_updates(), _meta()
        )
        assert case.progress.solution_proposed is True
        return case

    async def test_license_loss_withdraws_pending_offer(self):
        case = await self._case_with_offer()
        # The established-cause license falls: conclusion retracted (as the
        # M6/retraction machinery does); the empty graph re-derives
        # cause_state below IDENTIFIED on the next recompute.
        case.root_cause_conclusion = None
        case.current_turn = 7
        metadata: dict = {}
        _recompute_assessment_state(case, metadata=metadata)

        assert _pending_solutions(case) == []
        withdrawn = [a for a in case.proposed_actions if a.state == "superseded"]
        assert len(withdrawn) == 1
        assert withdrawn[0].superseded_reason == "license_lost"
        assert withdrawn[0].superseded_in_turn == 7
        assert case.progress.solution_proposed is False
        assert case.progress.solution_state == SolutionState.UNKNOWN
        assert "withdrawn" in metadata.get("system_feedback", "")

    async def test_withdrawal_logs_warning_event(self, caplog):
        import logging

        case = await self._case_with_offer()
        case.root_cause_conclusion = None
        with caplog.at_level(
            logging.WARNING,
            logger="faultmaven.core.investigation.milestone_engine",
        ):
            _recompute_assessment_state(case, metadata={})
        events = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "solution_offer_withdrawn"
        ]
        assert len(events) == 1

    async def test_counter_labeled_by_reason(self):
        case = await self._case_with_offer()
        case.root_cause_conclusion = None
        with patch(
            "faultmaven.core.investigation.milestone_engine."
            "solution_offer_superseded_total"
        ) as counter:
            _recompute_assessment_state(case, metadata={})
        counter.labels.assert_called_once_with(reason="license_lost")
        assert counter.labels.return_value.inc.call_count == 1

    async def test_standing_license_keeps_offer_and_flag(self):
        # RCC + verified symptom stand → the offer survives every recompute.
        case = await self._case_with_offer()
        metadata: dict = {}
        _recompute_assessment_state(case, metadata=metadata)
        assert len(_pending_solutions(case)) == 1
        assert case.progress.solution_proposed is True
        assert "system_feedback" not in metadata

    async def test_accepted_offer_is_never_withdrawn(self):
        # An executed fix is a historical fact — license loss cannot unmake
        # it; its truth surface is the M6 machinery, not offer liveness.
        case = await self._case_with_offer()
        offer = _pending_solutions(case)[0]
        offer.state = "accepted"
        case.root_cause_conclusion = None
        _recompute_assessment_state(case, metadata={})
        assert offer.state == "accepted"
        assert case.progress.solution_proposed is True

    async def test_pending_mitigation_survives_license_loss(self):
        case = _make_case()
        eng = _make_engine()
        await eng._apply_investigation_updates(case, _workaround_updates(), _meta())
        await eng._apply_investigation_updates(case, _solution_updates(), _meta())
        case.root_cause_conclusion = None
        _recompute_assessment_state(case, metadata={})
        # SOLUTION offer withdrawn; the mitigation offer is untouched (not
        # licensed by an established cause).
        assert _pending_solutions(case) == []
        assert any(
            a.action_type == InvestigationActionType.MITIGATION and a.state == "pending"
            for a in case.proposed_actions
        )

    async def test_noop_without_pending_offers(self):
        case = _make_case(established=False)
        case.progress.symptom_verified = False
        metadata: dict = {}
        assert _withdraw_unlicensed_solution_offers(case, metadata) == 0
        assert metadata == {}


# ---------------------------------------------------------------------------
# Helper-level pins
# ---------------------------------------------------------------------------


class TestHelpers:
    def _offer(self, *, action_type=InvestigationActionType.SOLUTION, state="pending"):
        return ProposedAction(
            case_id="case_x",
            action_type=action_type,
            description="fix it",
            proposed_in_turn=2,
            state=state,
        )

    def test_liveness_reads_pending_and_accepted_solutions_only(self):
        case = _make_case(established=False)
        assert _live_solution_offer_exists(case) is False
        case.proposed_actions.append(
            self._offer(action_type=InvestigationActionType.DIAGNOSTIC)
        )
        case.proposed_actions.append(
            self._offer(action_type=InvestigationActionType.MITIGATION)
        )
        assert _live_solution_offer_exists(case) is False
        case.proposed_actions.append(self._offer(state="superseded"))
        case.proposed_actions.append(self._offer(state="rejected"))
        assert _live_solution_offer_exists(case) is False
        case.proposed_actions.append(self._offer(state="accepted"))
        assert _live_solution_offer_exists(case) is True

    def test_supersede_touches_only_pending_solutions(self):
        case = _make_case(established=False)
        pending = self._offer()
        accepted = self._offer(state="accepted")
        diag = self._offer(action_type=InvestigationActionType.DIAGNOSTIC)
        case.proposed_actions.extend([pending, accepted, diag])
        with patch(
            "faultmaven.core.investigation.milestone_engine."
            "solution_offer_superseded_total"
        ) as counter:
            count = _supersede_pending_solution_offers(case, reason="reproposal")
        assert count == 1
        assert pending.state == "superseded"
        assert pending.superseded_reason == "reproposal"
        assert pending.superseded_in_turn == case.current_turn
        assert accepted.state == "accepted"
        assert diag.state == "pending"
        counter.labels.assert_called_once_with(reason="reproposal")


# ---------------------------------------------------------------------------
# The frame conjunction (#656: Zone-3-pending must not outlive the offer)
# ---------------------------------------------------------------------------


class TestZoneFrameFollowsDerivation:
    def test_withdrawn_offer_exits_awaiting_execution_frame(self):
        # The #656 latch shape: solution_proposed=True froze the DIAGNOSIS
        # frame at "awaiting execution". Derived OFF, the frame re-opens.
        case = _make_case(established=False)
        p = case.progress
        p.symptom_verified = True
        p.cause_state = CauseState.CANDIDATES
        p.solution_proposed = False
        emphasis = _get_diagnosis_focus_emphasis(p)
        assert "Root cause analysis" in emphasis
        assert "awaiting execution" not in emphasis

    def test_live_offer_with_identified_cause_still_holds_frame(self):
        # While the license stands and the offer is live, the pending frame
        # is correct (its yields under contradiction are P2.2, not P2.1).
        case = _make_case(established=False)
        p = case.progress
        p.symptom_verified = True
        p.cause_state = CauseState.IDENTIFIED
        p.solution_proposed = True
        assert "awaiting execution" in _get_diagnosis_focus_emphasis(p)
