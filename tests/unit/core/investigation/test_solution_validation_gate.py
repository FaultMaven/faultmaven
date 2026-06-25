"""Tests for the M5 SOLUTION-validation gate.

M5 (two-dimensional-hypothesis-methodology §0): a permanent-fix SOLUTION may not
be registered before the cause is *established*. M5 reuses the resolution gate's
``_cause_identified`` predicate (cause_state == IDENTIFIED OR a set
RootCauseConclusion OR working_conclusion ≥ 0.6) so it is never stricter than the
gate that lets a case RESOLVE, and so the RCC branch covers the same-turn
validate-and-fix path (cause_state is recomputed only after this gate runs). A
premature remediation is downgraded to DIAGNOSTIC with a recovery
``downgrade_reason`` (graceful — flow continues; the LLM grounds the root or
proposes a mitigation). Mitigation (WORKAROUND) is exempt by design.

- ``TestSolutionCauseValidatedPredicate`` — the pure gate predicate.
- ``TestM5SolutionGate`` — the gate wired into ``_apply_investigation_updates``
  (the chain-emission tail is stubbed to isolate the gate decision).

Sibling of ``test_mitigation_evidence_gate.py`` (the 3D MITIGATION gate at the
same call site).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

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
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InvestigationActionType,
    RootCauseConclusion,
    SolutionType,
    WorkingConclusion,
)


def _rcc() -> RootCauseConclusion:
    return RootCauseConclusion(
        root_cause="the connection pool is exhausted",
        mechanism="all pool slots are held by stuck queries",
        likelihood=0.8,
        confidence_level=ConfidenceLevel.from_score(0.8),
    )


def _wc(likelihood: float) -> WorkingConclusion:
    return WorkingConclusion(
        statement="the pool is exhausted",
        reasoning="observed stuck queries holding all slots",
        likelihood=likelihood,
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


def _updates(solution_type: SolutionType, *, with_rcc: bool = False) -> _Updates:
    fields = {
        "solutions_to_add": [
            SolutionToAdd(
                description="Apply the permanent fix",
                solution_type=solution_type,
                estimated_impact="resolves the failure",
                risks="low",
                commands=["kubectl apply -f fix.yaml"],
            )
        ]
    }
    if with_rcc:
        # The LLM's root_cause_conclusion, applied to the case early in
        # _apply_investigation_updates (before the M5 gate). Shape per the apply
        # block: reads root_cause / mechanism / evidence_ids / likelihood.
        fields["root_cause_conclusion"] = SimpleNamespace(
            root_cause="the connection pool is exhausted",
            mechanism="all pool slots are held by stuck queries",
            evidence_ids=[],
            likelihood=0.8,
        )
    return _Updates(**fields)


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


class TestSolutionCauseValidatedPredicate:
    """M5 shares the resolution gate's `_cause_identified` predicate: cause is
    established via cause_state==IDENTIFIED OR a set RCC OR working_conclusion
    >= 0.6. (Consistency with the resolution gate prevents a deadlock; the RCC
    branch covers the same-turn validate+fix path — see the gate timing.)"""

    def test_nothing_established_is_false(self):
        assert _solution_cause_validated(_make_case(CauseState.UNKNOWN)) is False
        assert _solution_cause_validated(_make_case(CauseState.CANDIDATES)) is False

    def test_cause_state_identified_is_true(self):
        assert _solution_cause_validated(_make_case(CauseState.IDENTIFIED)) is True

    def test_rcc_backstop_is_true_even_when_cause_state_not_identified(self):
        case = _make_case(CauseState.UNKNOWN)
        case.root_cause_conclusion = _rcc()
        assert _solution_cause_validated(case) is True

    def test_working_conclusion_at_threshold_is_true(self):
        case = _make_case(CauseState.CANDIDATES)
        case.working_conclusion = _wc(0.6)
        assert _solution_cause_validated(case) is True

    def test_working_conclusion_below_threshold_is_false(self):
        case = _make_case(CauseState.CANDIDATES)
        case.working_conclusion = _wc(0.59)
        assert _solution_cause_validated(case) is False


# ---------------------------------------------------------------------------
# Gate behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestM5SolutionGate:
    async def test_solution_downgraded_when_cause_not_established(self):
        """A permanent fix proposed before the cause is established (no IDENTIFIED,
        no RCC, no working conclusion) → DIAGNOSTIC, with an M5 recovery reason;
        no premature solution_proposed."""
        case = _make_case(CauseState.CANDIDATES)
        eng = _make_engine()

        await eng._apply_investigation_updates(
            case, _updates(SolutionType.CODE_FIX), _meta()
        )

        action = case.proposed_actions[-1]
        assert action.action_type == InvestigationActionType.DIAGNOSTIC
        assert action.downgrade_reason is not None
        assert "root cause is not yet established" in action.downgrade_reason
        assert case.progress.solution_proposed is False

    async def test_solution_allowed_when_cause_identified(self):
        """With a validated root (cause_state IDENTIFIED), the SOLUTION action
        stands and solution_proposed is set."""
        case = _make_case(CauseState.IDENTIFIED)
        eng = _make_engine()

        await eng._apply_investigation_updates(
            case, _updates(SolutionType.CODE_FIX), _meta()
        )

        action = case.proposed_actions[-1]
        assert action.action_type == InvestigationActionType.SOLUTION
        assert action.downgrade_reason is None
        assert case.progress.solution_proposed is True

    async def test_solution_allowed_same_turn_root_cause_conclusion(self):
        """REGRESSION (the ordering bug): the LLM grounds the root (emits a
        root_cause_conclusion) AND proposes the fix in the SAME turn. cause_state
        for this turn is recomputed only at the end of the method (after the
        gate), so it is still UNKNOWN at gate time — but the RCC is applied
        BEFORE the gate, so M5 (via _cause_identified) must allow the SOLUTION.
        Keying M5 on raw cause_state would wrongly downgrade this."""
        case = _make_case(CauseState.UNKNOWN)
        eng = _make_engine()

        await eng._apply_investigation_updates(
            case, _updates(SolutionType.CODE_FIX, with_rcc=True), _meta()
        )

        # The RCC was applied this turn, before the gate.
        assert case.root_cause_conclusion is not None
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
