"""Tests for the M5 SOLUTION-validation gate.

M5 (two-dimensional-hypothesis-methodology §0): a permanent-fix SOLUTION may not
be registered before its root is *mechanistically validated* — i.e. before
``cause_state == IDENTIFIED``. A premature remediation is downgraded to
DIAGNOSTIC with a recovery ``downgrade_reason`` (graceful — the flow continues;
the LLM validates the root or proposes a mitigation). Mitigation (WORKAROUND) is
exempt by design.

- ``TestSolutionCauseValidatedPredicate`` — the pure gate predicate.
- ``TestM5SolutionGate`` — the gate wired into ``_apply_investigation_updates``
  (the chain-emission tail is stubbed to isolate the gate decision).

Sibling of ``test_mitigation_evidence_gate.py`` (the 3D MITIGATION gate at the
same call site).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from faultmaven.core.investigation.milestone_engine import (
    MilestoneEngine,
    _solution_cause_validated,
)
from faultmaven.core.investigation.schemas import SolutionToAdd
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    CauseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InvestigationActionType,
    SolutionType,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_case(cause_state: CauseState, *, with_symptom: bool = False) -> Case:
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
    case.progress.cause_state = cause_state
    if with_symptom:
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
    return case


def _make_engine() -> MilestoneEngine:
    """Bare engine; stub the chain-emission tail (runs after the gate) so the
    test isolates the gate decision and needs no DI wiring."""
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
    """Updates stub: any field not explicitly set reads as None (falsy), so
    every block in _apply_investigation_updates is skipped except the one(s)
    set here. Robust to the method reading any update attribute (guarded or
    not, e.g. `milestones`, `outcome`)."""

    def __init__(self, **set_fields):
        self.__dict__.update(set_fields)

    def __getattr__(self, name):  # only for attrs absent from __dict__
        return None


def _updates(solution_type: SolutionType) -> _Updates:
    return _Updates(
        solutions_to_add=[
            SolutionToAdd(
                description="Apply the permanent fix",
                solution_type=solution_type,
                estimated_impact="resolves the failure",
                risks="low",
                commands=["kubectl apply -f fix.yaml"],
            )
        ]
    )


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


class TestSolutionCauseValidatedPredicate:
    def test_unknown_is_not_validated(self):
        assert _solution_cause_validated(_make_case(CauseState.UNKNOWN)) is False

    def test_candidates_is_not_validated(self):
        assert _solution_cause_validated(_make_case(CauseState.CANDIDATES)) is False

    def test_identified_is_validated(self):
        assert _solution_cause_validated(_make_case(CauseState.IDENTIFIED)) is True


# ---------------------------------------------------------------------------
# Gate behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestM5SolutionGate:
    async def test_solution_downgraded_when_cause_not_identified(self):
        """A permanent fix proposed before the root is validated → DIAGNOSTIC,
        with an M5 recovery reason; no premature solution_proposed."""
        case = _make_case(CauseState.CANDIDATES)
        eng = _make_engine()

        await eng._apply_investigation_updates(
            case, _updates(SolutionType.CODE_FIX), _meta()
        )

        action = case.proposed_actions[-1]
        assert action.action_type == InvestigationActionType.DIAGNOSTIC
        assert action.downgrade_reason is not None
        assert "IDENTIFIED" in action.downgrade_reason
        assert case.progress.solution_proposed is False

    async def test_solution_allowed_when_cause_identified(self):
        """With a mechanistically-validated root (IDENTIFIED), the SOLUTION
        action stands and solution_proposed is set."""
        case = _make_case(CauseState.IDENTIFIED)
        eng = _make_engine()

        await eng._apply_investigation_updates(
            case, _updates(SolutionType.CODE_FIX), _meta()
        )

        action = case.proposed_actions[-1]
        assert action.action_type == InvestigationActionType.SOLUTION
        assert action.downgrade_reason is None
        assert case.progress.solution_proposed is True

    async def test_mitigation_workaround_is_exempt_from_m5(self):
        """A WORKAROUND (→ MITIGATION) is NOT gated by M5 even with the cause
        unvalidated — it precedes a known root by design. (Symptom evidence is
        present so the separate 3D gate does not fire.)"""
        case = _make_case(CauseState.UNKNOWN, with_symptom=True)
        eng = _make_engine()

        await eng._apply_investigation_updates(
            case, _updates(SolutionType.WORKAROUND), _meta()
        )

        action = case.proposed_actions[-1]
        assert action.action_type == InvestigationActionType.MITIGATION
        assert action.downgrade_reason is None
