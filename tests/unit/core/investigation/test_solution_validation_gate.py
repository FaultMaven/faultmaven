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
    _coerce_intervention_quadrant,
    _solution_cause_validated,
)
from faultmaven.core.investigation.schemas import SolutionToAdd
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    CausalNode,
    CauseState,
    ConfidenceLevel,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    InterventionQuadrant,
    InvestigationActionType,
    NodeType,
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
        enterprise_id="o1",
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
        # A verified symptom is the evidence-grounded anchor for cause
        # identification — the fallback signals in ``_cause_identified`` (a set
        # RootCauseConclusion / a working_conclusion at threshold) are only
        # trusted once the symptom is verified.
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
        # The RCC backstop covers the under-reported-cause_state case, but only
        # for a case whose symptom is verified (the cause-identification anchor).
        case = _make_case(CauseState.UNKNOWN, with_symptom=True)
        case.root_cause_conclusion = _rcc()
        assert _solution_cause_validated(case) is True

    def test_rcc_backstop_requires_verified_symptom(self):
        # Without the verified-symptom anchor an RCC alone does NOT establish the
        # cause — an unanchored conclusion must not unlock the solution/resolution
        # gates while cause_state is not IDENTIFIED.
        case = _make_case(CauseState.UNKNOWN)  # no verified symptom
        case.root_cause_conclusion = _rcc()
        assert _solution_cause_validated(case) is False

    def test_working_conclusion_at_threshold_is_true(self):
        case = _make_case(CauseState.CANDIDATES, with_symptom=True)
        case.working_conclusion = _wc(0.6)
        assert _solution_cause_validated(case) is True

    def test_working_conclusion_backstop_requires_verified_symptom(self):
        case = _make_case(CauseState.CANDIDATES)  # no verified symptom
        case.working_conclusion = _wc(0.6)
        assert _solution_cause_validated(case) is False

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
        stands and solution_proposed derives True.

        The fixture's flat IDENTIFIED is re-derived DOWN by the end-of-turn
        chain recompute (no graph in this fixture), so the anchored RCC
        fallback carries the established-cause license through the INV-32
        liveness re-check — the same fallback the M5 creation gate reads.
        """
        case = _make_case(CauseState.IDENTIFIED, with_symptom=True)
        case.root_cause_conclusion = _rcc()
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
        Keying M5 on raw cause_state would wrongly downgrade this. The symptom is
        verified (the cause-identification anchor the RCC backstop now requires)."""
        case = _make_case(CauseState.UNKNOWN, with_symptom=True)
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


# ---------------------------------------------------------------------------
# R9 — SolutionToAdd causal-graph linkage mapped onto Solution (honor-or-reject)
# ---------------------------------------------------------------------------


class TestCoerceInterventionQuadrant:
    """The apply-path quadrant coercion is honor-or-reject: a recognized value
    (case-insensitive) maps to the enum; anything else — a typo, empty, None —
    yields None (recorded unquadranted), never a parse crash on a BEST_EFFORT
    provider."""

    def test_recognized_values_map(self):
        assert (
            _coerce_intervention_quadrant("remediation")
            == InterventionQuadrant.REMEDIATION
        )
        assert (
            _coerce_intervention_quadrant("DEFENSIVE_FIX")
            == InterventionQuadrant.DEFENSIVE_FIX
        )
        assert (
            _coerce_intervention_quadrant(" Mitigation ")
            == InterventionQuadrant.MITIGATION
        )

    def test_unrecognized_or_missing_is_none(self):
        assert _coerce_intervention_quadrant("bogus") is None
        assert _coerce_intervention_quadrant("") is None
        assert _coerce_intervention_quadrant(None) is None


def _solution_update(**extra) -> _Updates:
    """A solutions_to_add update carrying the R9 optional linkage fields."""
    return _Updates(
        solutions_to_add=[
            SolutionToAdd(
                description="Apply the permanent fix",
                solution_type=SolutionType.CODE_FIX,
                estimated_impact="resolves the failure",
                risks="low",
                commands=["kubectl apply -f fix.yaml"],
                **extra,
            )
        ]
    )


@pytest.mark.asyncio
class TestR9SolutionLinkageMapping:
    """The R9 emission-mediated path: a quadrant-carrying SolutionToAdd maps its
    quadrant/node_ref onto the persisted Solution. Recorded as DATA; the M5
    downgrade logic is unchanged (a mapped Solution is still constructed even if
    its ProposedAction downgrades)."""

    async def test_linkage_mapped_onto_solution(self):
        case = _make_case(CauseState.IDENTIFIED, with_symptom=True)
        case.root_cause_conclusion = _rcc()
        node = CausalNode(
            statement="the connection pool is exhausted",
            node_type=NodeType.ROOT,
            generated_at_turn=1,
        )
        case.causal_nodes[node.node_id] = node
        eng = _make_engine()

        await eng._apply_investigation_updates(
            case,
            _solution_update(quadrant="remediation", node_ref=node.node_id),
            _meta(),
        )

        sol = case.solutions[-1]
        assert sol.quadrant == InterventionQuadrant.REMEDIATION
        assert sol.node_id == node.node_id

    async def test_proposed_solution_never_claims_verification(self):
        """A proposed candidate solution must NOT populate ``verification_method``
        (past-tense 'how the fix WAS verified', read by the resolution report +
        confirmation gate). An unverified proposal claiming verification would
        print a false 'Verified by' and stop the engine soliciting real
        verification — so the R9 emission never writes that field."""
        case = _make_case(CauseState.IDENTIFIED, with_symptom=True)
        case.root_cause_conclusion = _rcc()
        eng = _make_engine()

        await eng._apply_investigation_updates(
            case, _solution_update(quadrant="remediation"), _meta()
        )

        assert case.solutions[-1].verification_method is None

    async def test_unknown_node_ref_and_quadrant_rejected(self):
        """honor-or-reject: a node_ref not on the graph and an unrecognized
        quadrant are dropped to None rather than persisted as bogus linkage."""
        case = _make_case(CauseState.IDENTIFIED, with_symptom=True)
        case.root_cause_conclusion = _rcc()
        eng = _make_engine()

        await eng._apply_investigation_updates(
            case,
            _solution_update(quadrant="not-a-quadrant", node_ref="cn_deadbeef0000"),
            _meta(),
        )

        sol = case.solutions[-1]
        assert sol.quadrant is None
        assert sol.node_id is None

    async def test_absent_linkage_maps_to_none(self):
        """A plain SolutionToAdd (no R9 fields, the flag-off / unprompted case)
        maps to a Solution with the linkage all None — unchanged from before R9."""
        case = _make_case(CauseState.IDENTIFIED, with_symptom=True)
        case.root_cause_conclusion = _rcc()
        eng = _make_engine()

        await eng._apply_investigation_updates(case, _solution_update(), _meta())

        sol = case.solutions[-1]
        assert sol.quadrant is None
        assert sol.node_id is None
        assert sol.verification_method is None
