"""#1284 — ``hypotheses_validated`` is a progress arm, and it has a writer.

The arm is one of the nine ``check_if_progress_made`` scores and a field on
every persisted turn record, and for as long as nothing wrote it five consumers
read a permanently-empty list: the momentum bands summed three inputs of which
one was always 0, the loop fingerprint carried a constant component, the
context-builder line could never render, ``TurnProgress.advancement_count``
undercounted, and the predicate arm could never fire.

Measured before the fix: non-empty on 0 of 2,129 persisted turns while the
sibling arms fired on hundreds — and 64 hypotheses had nonetheless reached
VALIDATED. The event happened; nothing recorded it.

Every assertion below is a mechanical read of engine state over a hand-built
graph: nothing here depends on model behavior or wording.
"""

import hashlib
from datetime import datetime, timezone

import pytest

from faultmaven.core.investigation.causal_graph import (
    project_hypothesis_states_from_roots,
    seed_problem_node,
)
from faultmaven.core.investigation.milestone_engine import (
    _recompute_assessment_state,
    check_if_progress_made,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    CausalEdge,
    CausalNode,
    CauseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemVerification,
    ValidationMethod,
)

pytestmark = pytest.mark.unit

_CAUSE = "checkout-api v2.14.0 retains an unbounded orderSummaryCache"
_MECH = "JVM heap pressure causes GC pauses and readiness failure before OOM"
_ROOT = "cn_0000000000aa"
_MID = "cn_0000000000cc"
_HYP = "hyp_0000000000aa"


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _evidence(label: str) -> Evidence:
    # Label embedded as content tokens so two rows read as INDEPENDENT
    # observations under the INV-29 mirror collapse.
    return Evidence(
        evidence_id=_eid(label),
        summary=f"fact-{label} metric-{label} reading-{label}",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _node(node_id, statement, node_type=NodeType.ROOT, *, supports=()) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement=statement,
        node_type=node_type,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=_eid(label),
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on the node",
                linked_at_turn=2,
            )
            for label in supports
        ],
        generated_at_turn=1,
    )


def _validating_case() -> Case:
    """ROOT -> MID -> D, the root carrying two independent supports so
    ``derive_node_states`` validates it (INV-29), with the symptom verified so
    the chain can reach IDENTIFIED."""
    root = _node(_ROOT, _CAUSE, supports=["a1", "a2"])
    mid = _node(_MID, _MECH, node_type=NodeType.INTERMEDIATE)
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="checkout orders failing with 500s",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout orders failing with 500s",
            severity=CaseSeverity.HIGH,
        ),
    )
    case.causal_nodes = {n.node_id: n for n in (root, mid)}
    case.evidence = [_evidence(x) for x in ("a1", "a2")]
    d = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=_ROOT, effect_node_id=_MID),
        CausalEdge(cause_node_id=_MID, effect_node_id=d.node_id),
    ]
    case.hypotheses = {
        _HYP: Hypothesis(
            hypothesis_id=_HYP,
            statement=_CAUSE,
            category=HypothesisCategory.CODE,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="initial",
            root_node_id=_ROOT,
            path=[_ROOT, _MID, d.node_id],
            generated_at_turn=1,
        )
    }
    case.progress.symptom_verified = True
    return case


def test_the_turn_a_hypothesis_is_validated_records_the_arm():
    """The defect, stated positively: the event reaches the turn metadata."""
    case = _validating_case()
    metadata: dict = {}

    _recompute_assessment_state(case, metadata=metadata)

    # Positive control: the projection really did validate, so an empty arm
    # below would be a missing WRITE and not an absent event.
    assert case.hypotheses[_HYP].state is HypothesisState.VALIDATED
    assert case.progress.cause_state is CauseState.IDENTIFIED

    assert metadata["hypotheses_validated"] == [_HYP]


def test_the_arm_is_scored_as_progress():
    """A turn that validated a hypothesis must not read as an idle engine."""
    case = _validating_case()
    metadata: dict = {}
    _recompute_assessment_state(case, metadata=metadata)

    # Isolate the arm: the predicate scores it on its own, with no other arm
    # present, or a validation turn would depend on a co-firing sibling.
    assert check_if_progress_made(
        {"hypotheses_validated": metadata["hypotheses_validated"]}
    )


def test_a_standing_validation_is_not_re_reported():
    """Rising edge only. The #1136 ``novel_*`` rule, here by construction: a
    hypothesis that merely STAYS validated across turns is not fresh progress,
    so a parked case cannot hold the stall net open by restating itself."""
    case = _validating_case()
    _recompute_assessment_state(case, metadata={})
    assert case.hypotheses[_HYP].state is HypothesisState.VALIDATED

    case.current_turn = 6
    later: dict = {}
    _recompute_assessment_state(case, metadata=later)

    assert later.get("hypotheses_validated", []) == []


def test_losing_validation_is_not_recorded_as_progress():
    """The revert direction moves ``changed`` but must not enter the arm —
    de-validation is not advancement."""
    case = _validating_case()
    _recompute_assessment_state(case, metadata={})
    assert case.hypotheses[_HYP].state is HypothesisState.VALIDATED

    # Knock the root out of VALIDATED; the projection reverts the hypothesis.
    case.causal_nodes[_ROOT].node_state = NodeState.CANDIDATE
    case.hypotheses[_HYP].state = HypothesisState.VALIDATED

    changed, newly = project_hypothesis_states_from_roots(case)

    assert case.hypotheses[_HYP].state is HypothesisState.ACTIVE
    assert changed is True
    assert newly == []


def test_the_identification_edge_is_recorded_on_the_turn():
    """The other per-turn signal the same recompute owes the case.

    ``root_cause_identified`` left the per-turn ``milestones_completed`` channel
    with #675/INV-35 and no writer replaced it, so every per-turn consumer keyed
    on that channel went quiet — the transparency counter above all. The engine
    records it here, from its own derivation.
    """
    case = _validating_case()
    metadata: dict = {}
    _recompute_assessment_state(case, metadata=metadata)

    assert case.progress.cause_state is CauseState.IDENTIFIED
    assert metadata["milestones_completed"] == ["root_cause_identified"]
    # The two arms are independent: the milestone reports the cause_state edge,
    # the id list reports the hypothesis projection. Neither is derived from the
    # other, so one going quiet cannot silently mask the other.
    assert metadata["hypotheses_validated"] == [_HYP]


def test_a_standing_identification_is_not_re_recorded():
    """Rising edge only — a case that stays IDENTIFIED must not re-record the
    milestone every turn, which would hold the transparency light off forever."""
    case = _validating_case()
    _recompute_assessment_state(case, metadata={})
    assert case.progress.cause_state is CauseState.IDENTIFIED

    case.current_turn = 6
    later: dict = {}
    _recompute_assessment_state(case, metadata=later)

    assert later.get("milestones_completed", []) == []


# ============================================================
# The other half of the same root cause: the identification EDGE
# ============================================================
#
# #675/INV-35 made cause identification engine-derived and removed the
# LLM-claimed ``root_cause_identified`` milestone. Every CASE-level reader was
# rewired to the derived ``completed_milestones`` property; the PER-TURN readers
# were not, and they key on ``turn.milestones_completed`` — a channel the name
# stopped arriving in. The transparency counter is the loudest of them:
# ``progress-transparency.md`` §Transitions promises "Transparent -> Silent |
# Any milestone is completed" and its Turn 11 example says deriving
# ``cause_state=IDENTIFIED`` resets the counter. Measured on the stored corpus
# before the fix: 30 of 49 identification turns did not reset it.


@pytest.mark.parametrize(
    "prior,current,expected",
    [
        (CauseState.UNKNOWN, CauseState.IDENTIFIED, True),
        (CauseState.CANDIDATES, CauseState.IDENTIFIED, True),
        # Standing, not rising: a case that stays identified across turns must
        # not re-record the milestone every turn — that would hold the
        # transparency light off forever and restate progress the case already had.
        (CauseState.IDENTIFIED, CauseState.IDENTIFIED, False),
        # Falling and non-arrivals.
        (CauseState.IDENTIFIED, CauseState.CANDIDATES, False),
        (CauseState.IDENTIFIED, CauseState.UNKNOWN, False),
        (CauseState.UNKNOWN, CauseState.CANDIDATES, False),
        (CauseState.UNKNOWN, CauseState.UNKNOWN, False),
    ],
)
def test_the_identification_edge_is_the_rising_edge_only(prior, current, expected):
    from faultmaven.core.investigation.milestone_engine import is_identification_edge

    assert is_identification_edge(prior, current) is expected


def test_identifying_the_cause_turns_the_transparency_light_off():
    """The promise in progress-transparency.md §Transitions, end to end.

    The counter reads the PER-TURN list, so this holds only because the engine
    records ``root_cause_identified`` there on the edge. Deleting that append
    puts this case back to five investigative turns and the light back on.
    """
    from faultmaven.core.investigation.progress_monitor import ProgressMonitor
    from faultmaven.modules.case.contracts import TurnOutcome, TurnProgress

    def _turn(n, milestones=()):
        return TurnProgress(
            turn_number=n,
            timestamp=datetime.now(timezone.utc),
            milestones_completed=list(milestones),
            evidence_added=[f"ev_{n}"],
            hypotheses_generated=[],
            hypotheses_validated=[],
            solutions_proposed=[],
            progress_made=bool(milestones),
            outcome=TurnOutcome.DATA_PROVIDED,
        )

    case = _validating_case()
    monitor = ProgressMonitor()

    # Five investigative turns with no milestone: the light is on.
    case.turn_history = [_turn(n) for n in range(1, 6)]
    assert monitor._count_investigative_turns_since_milestone(case) == 5

    # The engine derives IDENTIFIED and records it on the turn. The light goes
    # out — the counter restarts from that turn, not from the last symptom or
    # stage gate.
    case.turn_history.append(_turn(6, milestones=["root_cause_identified"]))
    assert monitor._count_investigative_turns_since_milestone(case) == 0

    case.turn_history.append(_turn(7))
    assert monitor._count_investigative_turns_since_milestone(case) == 1
