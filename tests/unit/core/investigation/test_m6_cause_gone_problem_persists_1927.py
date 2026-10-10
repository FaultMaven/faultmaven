"""#1927 — the cause seen removed while the reported problem persists is a
counterfactual disconfirmation, and M6 demotes on it from the case record.

#1906 split a resolution into two legs: a ``causal_absence`` row (the cause
observed removed) and a ``symptom_absence`` row at or after it (the problem
observed gone). M6 kept reading any cause row after the fix as "the problem
did not persist", and the engine never reached that predicate anyway: the
counterfactual arm ran only on a root that already carried the engine's own
marker, which only M6 mints. So a fix whose cause went away while the problem
stayed left the disproven cause IDENTIFIED unless the model refuted it, and a
later mitigation's problem-gone row completed both legs and resolved the case
on it.

These tests drive the real recompute (``_recompute_cause_state_from_chain``,
the seam the turn pipeline calls) over a validated chain, so they pin the
wiring as well as the predicate.
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.causal_graph.disconfirmation import (
    m6_disconfirmation_basis,
)
from faultmaven.core.investigation.causal_graph.ingestion import seed_problem_node
from faultmaven.core.investigation.cause_assurance import (
    ENGINE_EVIDENCE_AUTHOR,
    FIX_FAILED,
    FIX_SUCCEEDED,
    FIX_UNKNOWN,
    cause_elimination_rows,
    fix_windows,
    has_resolution_confirmation,
)
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_cause_state_from_chain,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
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
    InvestigationActionType,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ProblemStatus,
    ProblemVerification,
    ProposedAction,
    ValidationMethod,
)

pytestmark = pytest.mark.unit

_ROOT_ID = "cn_000000001927"
_HYP_ID = "hyp_000000001927"


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _row(label, category, turn, summary, collected_by="llm") -> Evidence:
    return Evidence(
        evidence_id=_eid(label),
        summary=summary,
        primary_purpose="diagnosis",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        collected_by=collected_by,
        collected_at_turn=turn,
        collected_at=datetime.now(timezone.utc),
    )


def _accept(case, action_type, turn):
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=action_type,
            description=f"{action_type.value} accepted at turn {turn}",
            proposed_in_turn=turn - 1,
            accepted_in_turn=turn,
            state="accepted",
        )
    )


def _identified_case(*, hypothesis_turn=1) -> tuple[Case, CausalNode, Hypothesis]:
    """checkout 503s; root: max_connections too low. Two independent causal
    supports validate the root, and the verified symptom anchors it, so the
    first recompute grounds ``cause_state=IDENTIFIED``."""
    supports = [
        _row(
            "pool_log",
            EvidenceCategory.CAUSAL_EVIDENCE,
            3,
            "postgres log: FATAL too many connections for role checkout",
        ),
        _row(
            "pool_metric",
            EvidenceCategory.CAUSAL_EVIDENCE,
            4,
            "pg_stat_activity shows 20 of 20 slots held by checkout workers",
        ),
    ]
    root = CausalNode(
        node_id=_ROOT_ID,
        statement="max_connections is set to 20, below the checkout pool size",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=row.evidence_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on the root",
                linked_at_turn=4,
            )
            for row in supports
        ],
        generated_at_turn=hypothesis_turn,
    )
    hyp = Hypothesis(
        hypothesis_id=_HYP_ID,
        statement="the postgres connection limit starves the checkout pool",
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=_ROOT_ID,
        generated_at_turn=hypothesis_turn,
    )
    case = Case(
        case_id="case_000000001927",
        user_id="u",
        enterprise_id="o",
        title="checkout returns 503",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=max(5, hypothesis_turn),
        inquiry=InquiryData(
            proposed_problem_statement="checkout returns 503",
            problem_statement_confirmed=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout returns 503", severity=CaseSeverity.HIGH
        ),
    )
    case.causal_nodes = {_ROOT_ID: root}
    case.evidence = list(supports)
    case.hypotheses = {_HYP_ID: hyp}
    case.progress.problem_status = ProblemStatus.VERIFIED
    problem = seed_problem_node(case)
    case.causal_edges = [
        CausalEdge(cause_node_id=_ROOT_ID, effect_node_id=problem.node_id)
    ]
    _recompute_cause_state_from_chain(case)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert root.node_state == NodeState.VALIDATED
    return case, root, hyp


def _cause_gone(turn=9) -> Evidence:
    return _row(
        "cause_gone",
        EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        turn,
        "max_connections now reads 100 after the config change",
    )


def _still_failing(turn=9, label="still_503") -> Evidence:
    return _row(
        label,
        EvidenceCategory.SYMPTOM_EVIDENCE,
        turn,
        "checkout still returns 503 after the fix",
    )


def _problem_gone(turn, label="problem_gone") -> Evidence:
    return _row(
        label,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn,
        "checkout returns 200 again",
    )


def _recompute_at(case, turn):
    case.current_turn = turn
    _recompute_cause_state_from_chain(case)


def _failed_fix(case, *, fix_turn=8, outcome_turn=9):
    """Fix A executed at ``fix_turn``; at ``outcome_turn`` its cause re-checks
    gone and the reported problem is still there."""
    _accept(case, InvestigationActionType.SOLUTION, fix_turn)
    case.evidence += [_cause_gone(outcome_turn), _still_failing(outcome_turn)]


def _assert_demoted(case, root, hyp):
    assert hyp.state == HypothesisState.REFUTED
    assert root.node_state == NodeState.REFUTED
    assert case.progress.cause_state != CauseState.IDENTIFIED
    assert case.root_cause_conclusion is None


def _engine_rows(case):
    return [e for e in case.evidence if e.collected_by == ENGINE_EVIDENCE_AUTHOR]


# ---------------------------------------------------------------------------
# The record establishes the failed fix: M6 fires without a model refutation.
# ---------------------------------------------------------------------------


def test_cause_gone_problem_persisting_demotes_the_identified_cause():
    case, root, hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)

    _assert_demoted(case, root, hyp)
    engine_rows = _engine_rows(case)
    assert len(engine_rows) == 1
    summary = engine_rows[0].summary
    assert summary.startswith("Engine inference (M6), not an observation:")
    assert "executed at turn 8" in summary.lower()
    assert "observed removed at turn 9" in summary


def test_the_demotion_holds_on_later_turns():
    case, root, hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)
    _recompute_at(case, 10)
    _recompute_at(case, 11)

    _assert_demoted(case, root, hyp)
    assert len(_engine_rows(case)) == 1


# ---------------------------------------------------------------------------
# The mitigation route (#1927 Impact): a later workaround's problem-gone row
# must not complete both legs on the failed fix's cause row.
# ---------------------------------------------------------------------------


def _mitigate(case, *, accepted_turn=10, relief_turn=11):
    _accept(case, InvestigationActionType.MITIGATION, accepted_turn)
    case.evidence.append(_problem_gone(relief_turn, label="restart_relief"))
    _recompute_at(case, relief_turn)


def _assert_not_resolved_on_the_failed_cause(case):
    assert cause_elimination_rows(case) == []
    assert has_resolution_confirmation(case) is False
    assert assess_resolution_readiness(case).verdict != ResolutionReadiness.READY


def test_a_later_mitigation_does_not_resolve_on_the_failed_fix():
    case, root, hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)
    _mitigate(case)

    _assert_not_resolved_on_the_failed_cause(case)


def test_a_model_refutation_in_the_cause_rows_turn_closes_the_route_too():
    """The FAILURE-PATH-compliant shape: the model refutes the cause in the
    very turn the cause row and the persistence land. The evidence arm fires
    there, and the disconfirmation window's ``>=`` keeps a same-turn cause row
    qualifying, so that window alone does not close the route. The fix window
    does: it FAILED, so nothing in it confirms a resolution."""
    case, root, hyp = _identified_case()
    _failed_fix(case, fix_turn=8, outcome_turn=9)
    hyp.state = HypothesisState.REFUTED
    hyp.refutation_reason = "max_connections reads 100 and checkout still 503s"
    _recompute_at(case, 9)
    _assert_demoted(case, root, hyp)
    _mitigate(case)

    _assert_not_resolved_on_the_failed_cause(case)


def test_a_fresh_confirmation_after_the_failed_fix_still_resolves():
    """Marking stops at the failed fix: a second fix that works records new
    rows, and those confirm the resolution (NO-COLLAPSE)."""
    case, root, hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)
    _accept(case, InvestigationActionType.SOLUTION, 12)
    case.evidence += [
        _row(
            "pgbouncer_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            13,
            "pgbouncer pool_mode is now transaction",
        ),
        _problem_gone(13, label="checkout_ok"),
    ]
    _recompute_at(case, 13)

    assert [e.evidence_id for e in cause_elimination_rows(case)] == [
        _eid("pgbouncer_gone")
    ]
    assert has_resolution_confirmation(case) is True


# ---------------------------------------------------------------------------
# What must NOT fire (the #987 refusal and the #1906 shapes).
# ---------------------------------------------------------------------------


def test_a_confirmed_fix_is_never_read_as_a_failed_one():
    """#987: both legs at/after the fix confirm the resolution. A symptom row
    in the same turn (quoting the pre-fix failure) does not turn it into a
    failed fix."""
    case, root, hyp = _identified_case()
    _failed_fix(case)
    case.evidence.append(_problem_gone(9))
    _recompute_at(case, 9)

    assert hyp.state != HypothesisState.REFUTED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []
    assert has_resolution_confirmation(case) is True


def test_the_cause_seen_gone_with_the_problem_unchecked_demotes_nothing():
    """The #1906 systemd shape: the cause row alone, with the symptom check
    asked for. No persistence is observed, so nothing is disconfirmed."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_cause_gone(9))
    _recompute_at(case, 9)

    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []


def test_symptom_rows_from_before_the_fix_are_not_persistence():
    """The stored charter-3/4 runs: every symptom row quoted output from
    before the fix and was recorded before the cause row."""
    case, root, hyp = _identified_case()
    case.evidence.append(_still_failing(4, label="pre_fix_203"))
    _accept(case, InvestigationActionType.SOLUTION, 5)
    case.evidence.append(_cause_gone(5))
    _recompute_at(case, 6)

    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []


def test_persistence_in_the_fix_turn_waits_for_a_later_observation():
    """The same-turn consideration: everything in the fix's own execution
    turn cannot be ordered after the fix, so the record alone demotes nothing
    there. The next turn's persistence does."""
    case, root, hyp = _identified_case()
    _failed_fix(case, fix_turn=9, outcome_turn=9)
    _recompute_at(case, 9)
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []

    case.evidence.append(_still_failing(10, label="still_503_next_turn"))
    _recompute_at(case, 10)
    _assert_demoted(case, root, hyp)


def test_a_mitigation_failure_is_not_a_failed_fix():
    """A MITIGATION is not a fix of the cause (INV-42), so a workaround with
    the cause seen gone and the problem present establishes nothing."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.MITIGATION, 8)
    case.evidence += [_cause_gone(9), _still_failing(9)]
    _recompute_at(case, 9)

    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []


def test_a_fix_executed_before_the_cause_was_proposed_does_not_disconfirm_it():
    """A fix can only have addressed a cause that existed when it ran. A cause
    identified after the latest fix is not disconfirmed by that fix's record."""
    case, root, hyp = _identified_case(hypothesis_turn=10)
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [_cause_gone(9), _still_failing(11)]
    _recompute_at(case, 11)

    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []


def test_fix_ran_and_problem_persists_without_a_cause_recheck_demotes_nothing():
    """ "I ran it, still failing" with no cause re-check may be an
    implementation error — the FAILURE PATH's first branch, where the cause is
    still present — which disconfirms nothing. The record trigger needs the
    cause observed removed."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_still_failing(9))
    _recompute_at(case, 9)

    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []


def test_a_retired_cause_is_not_refuted_by_the_record():
    """M6 runs before the recompute re-derives ``cause_state``, so the trigger
    can still read last turn's IDENTIFIED on a hypothesis the model retired
    this turn. Terminal states are immutable: the record must not refute it."""
    case, root, hyp = _identified_case()
    _failed_fix(case)
    hyp.state = HypothesisState.RETIRED
    _recompute_at(case, 9)

    assert hyp.state == HypothesisState.RETIRED
    assert _engine_rows(case) == []


# ---------------------------------------------------------------------------
# A failed fix window confirms nothing, by derivation: M6 writes no marks.
# ---------------------------------------------------------------------------


def _refuting_row_ids(root):
    return {
        link.evidence_id
        for link in root.evidence_links
        if link.stance == EvidenceStance.REFUTES
    }


def test_a_failed_window_confirms_nothing_and_m6_marks_no_row():
    """The failed fix's cause row stops standing as a cause leg because its
    window FAILED (``fix_windows``), not because M6 linked anything to it; a
    cause row from before the fix is overtaken by the failure as well."""
    case, root, hyp = _identified_case()
    case.evidence.append(
        _row(
            "pre_fix_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            6,
            "max_connections briefly read 100 during the failover",
        )
    )
    _failed_fix(case)
    _recompute_at(case, 9)

    _assert_demoted(case, root, hyp)
    assert _refuting_row_ids(root) == {e.evidence_id for e in _engine_rows(case)}
    assert cause_elimination_rows(case) == []


def test_a_confirmed_resolution_survives_a_same_turn_refutation():
    """The mixed single-turn shape: "the restart didn't fix it, correcting
    the config did". The model refutes the cause in the turn both legs of the
    confirmation land; those rows confirm the resolution, so M6 marks none of
    them."""
    case, root, hyp = _identified_case()
    _failed_fix(case)
    case.evidence.append(_problem_gone(9))
    hyp.state = HypothesisState.REFUTED
    hyp.refutation_reason = "the restart did not clear the 503s"
    _recompute_at(case, 9)

    assert _eid("cause_gone") not in _refuting_row_ids(root)
    assert has_resolution_confirmation(case) is True


def test_a_cause_proposed_after_the_latest_fix_does_not_take_its_rows():
    """The model refutes a cause first proposed after the latest fix. That
    fix did not address it, so a cause row recorded in the refutation's turn
    is not this cause's failed fix, and stays a cause leg."""
    case, root, hyp = _identified_case(hypothesis_turn=10)
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_cause_gone(11))
    hyp.state = HypothesisState.REFUTED
    hyp.refutation_reason = "the pool metric contradicts it"
    _recompute_at(case, 11)

    assert hyp.state == HypothesisState.REFUTED
    assert _eid("cause_gone") not in _refuting_row_ids(root)
    assert [e.evidence_id for e in cause_elimination_rows(case)] == [_eid("cause_gone")]


def _add_proxy_candidate(case, hyp) -> Hypothesis:
    """A second, unvalidated candidate that M6's likelihood proxy picks as the
    representative once no conclusion names the identified cause."""
    other_root = CausalNode(
        node_id="cn_00000000ffff",
        statement="a connection leak in the checkout worker",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        generated_at_turn=1,
    )
    other = Hypothesis(
        hypothesis_id="hyp_00000000ffff",
        statement="a connection leak in the checkout worker",
        category=HypothesisCategory.CODE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=other_root.node_id,
        initial_likelihood=0.9,
        generated_at_turn=1,
    )
    hyp.initial_likelihood = 0.6
    case.causal_nodes[other_root.node_id] = other_root
    case.hypotheses[other.hypothesis_id] = other
    case.root_cause_conclusion = None
    return other


def test_the_record_never_refutes_the_proxys_unvalidated_pick():
    """With no conclusion naming its cause, M6's representative hypothesis is
    the highest ``initial_likelihood`` — a proxy that can point at a candidate
    the case never validated. A failed fix disconfirms the cause the case
    IDENTIFIED, so the record trigger demotes only a validated root."""
    case, root, hyp = _identified_case()
    other = _add_proxy_candidate(case, hyp)
    _failed_fix(case)
    _recompute_at(case, 9)

    assert other.state == HypothesisState.ACTIVE
    assert _engine_rows(case) == []


def test_refuting_the_proxys_pick_never_takes_a_succeeded_fixs_cause_row():
    """The model refutes the proxy's unvalidated pick in the turn the
    identified cause's fix is seen working. M6's evidence arm demotes the pick,
    but no fix addressed it: the cause row stays the resolution's cause leg,
    and the user's problem-gone row resolves the case."""
    case, root, hyp = _identified_case()
    other = _add_proxy_candidate(case, hyp)
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_cause_gone(9))
    other.state = HypothesisState.REFUTED
    other.refutation_reason = "worker connection counts are flat"
    _recompute_at(case, 9)
    case.evidence.append(_problem_gone(10))
    _recompute_at(case, 10)

    assert _eid("cause_gone") not in _refuting_row_ids(root)
    assert _eid("cause_gone") not in _refuting_row_ids(
        case.causal_nodes[other.root_node_id]
    )
    assert has_resolution_confirmation(case) is True


def test_the_earliest_cause_row_orders_the_persistence():
    """The cause seen removed at turn 9 and the problem seen at 10 is the
    disconfirmation, whatever a later re-check of the cause records at 11."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _cause_gone(9),
        _still_failing(10),
        _row(
            "cause_still_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            11,
            "max_connections still reads 100",
        ),
    ]
    _recompute_at(case, 11)

    _assert_demoted(case, root, hyp)


def test_only_a_record_demotion_counts_as_one():
    """``m6_record_disconfirmation_total`` counts the demotions the record made
    while the model had not refuted the cause; a model refutation is the
    evidence arm and does not count."""
    target = (
        "faultmaven.core.investigation.causal_graph.disconfirmation."
        "m6_record_disconfirmation_total"
    )
    case, root, hyp = _identified_case()
    _failed_fix(case)
    with patch(target) as counter:
        _recompute_at(case, 9)
        _recompute_at(case, 10)
    _assert_demoted(case, root, hyp)
    counter.inc.assert_called_once_with()

    case, root, hyp = _identified_case()
    _failed_fix(case)
    hyp.state = HypothesisState.REFUTED
    hyp.refutation_reason = "max_connections reads 100 and checkout still 503s"
    with patch(target) as counter:
        _recompute_at(case, 9)
    counter.inc.assert_not_called()


# ---------------------------------------------------------------------------
# Review round (PR #1929): only a confirmation at or after the persistence
# contradicts it; the record reads rows about THIS cause and THIS problem;
# the marking takes rows only from a fix seen failing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mitigated", [False, True], ids=["delayed-effect", "restart-in-the-turn"]
)
def test_the_problem_seen_gone_in_the_persistence_turn_holds_it_off(mitigated):
    """ "Still 503... now it's up" in one turn after the cause was seen gone:
    the persistence and the problem-gone row cannot be ordered, and a
    same-turn ambiguity resolves toward the confirmation, as for the
    disconfirmation window and the mixed single-turn shape. With or without a
    restart recorded that turn, nothing is demoted: a turn holding both reads
    as gone (``fix_windows``, the owner's ruling)."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_cause_gone(9))
    _recompute_at(case, 9)
    if mitigated:
        _accept(case, InvestigationActionType.MITIGATION, 10)
    case.evidence += [_still_failing(10), _problem_gone(10, label="up_again")]
    _recompute_at(case, 10)

    assert hyp.state != HypothesisState.REFUTED
    assert _engine_rows(case) == []
    assert has_resolution_confirmation(case) is True


def test_a_confirmation_from_an_earlier_fix_window_does_not_hold_it_off():
    """A first fix confirmed at turns 4-5, a relapse, and a second fix whose
    cause is seen gone at 9 with the problem still there at 10. The first
    window's two legs are older than the persistence; they must not withhold
    the second fix's disconfirmation."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 3)
    case.evidence += [
        _row(
            "first_cause_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            4,
            "max_connections now reads 60 after the first change",
        ),
        _problem_gone(5, label="first_relief"),
    ]
    _recompute_at(case, 5)
    case.evidence.append(_still_failing(6, label="relapse"))
    _recompute_at(case, 6)
    _failed_fix(case, fix_turn=8, outcome_turn=9)
    case.evidence.append(_still_failing(10, label="still_503_after_second_fix"))
    _recompute_at(case, 10)

    _assert_demoted(case, root, hyp)


def test_persistence_and_a_fresh_confirmation_in_one_turn_withhold_it():
    """The mixed single-turn shape: "the first change didn't fix it, but
    pooling the connections did" records the persistence, a new cause row and
    the problem gone in ONE turn. Turn granularity cannot order them, so the
    confirmation stands and nothing is demoted (NO-COLLAPSE)."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_cause_gone(9))
    _recompute_at(case, 9)
    case.evidence += [
        _still_failing(10),
        _row(
            "pool_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            10,
            "max_connections headroom is now 80 free slots with pgbouncer",
        ),
        _problem_gone(10),
    ]
    _recompute_at(case, 10)

    assert hyp.state != HypothesisState.REFUTED
    assert _engine_rows(case) == []
    assert has_resolution_confirmation(case) is True


def test_a_symptom_the_cause_does_not_explain_is_not_persistence():
    """The fix worked (the cause seen gone at 9), and at 10 the user mentions
    an unrelated symptom. The record arm demotes with no model judgment, so a
    symptom row sharing nothing with the problem or the cause is not the
    problem persisting."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _cause_gone(9),
        _row(
            "billing_latency",
            EvidenceCategory.SYMPTOM_EVIDENCE,
            10,
            "also seeing latency on the billing service",
        ),
    ]
    _recompute_at(case, 10)

    assert hyp.state != HypothesisState.REFUTED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []


def _add_sibling_chain(case) -> CausalNode:
    """Another candidate cause in the graph, sharing no content with the
    identified one; the conclusion keeps naming the identified cause."""
    sibling = CausalNode(
        node_id="cn_00000000eeee",
        statement="a memory leak in the gunicorn worker",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.3,
        actionable=True,
        generated_at_turn=1,
    )
    case.causal_nodes[sibling.node_id] = sibling
    case.hypotheses["hyp_00000000eeee"] = Hypothesis(
        hypothesis_id="hyp_00000000eeee",
        statement="a memory leak in the gunicorn worker",
        category=HypothesisCategory.CODE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="initial",
        root_node_id=sibling.node_id,
        initial_likelihood=0.2,
        generated_at_turn=1,
    )
    return sibling


def test_a_fix_of_another_candidate_does_not_disconfirm_the_identified_cause():
    """The latest fix addressed another candidate in the graph: its cause row
    is about that chain and nothing in the identified cause's frame. That
    cause going away while the problem stays says nothing about the
    identified cause."""
    case, root, hyp = _identified_case()
    _add_sibling_chain(case)
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _row(
            "leak_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            9,
            "gunicorn worker memory leak patched, worker RSS now flat",
        ),
        _still_failing(10),
    ]
    _recompute_at(case, 10)

    assert hyp.state != HypothesisState.REFUTED
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert _engine_rows(case) == []


def test_a_demotion_with_no_failure_observed_marks_no_cause_row():
    """The model refutes the cause in the turn its fix's cause row lands, with
    no persistence observed. The evidence arm demotes, but no fix was seen
    failing: the cause row is not marked, and the user's problem-gone row
    still completes the resolution, as on main."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_cause_gone(9))
    hyp.state = HypothesisState.REFUTED
    hyp.refutation_reason = "the pool exhaustion pattern does not match"
    _recompute_at(case, 9)
    _assert_demoted(case, root, hyp)
    case.evidence.append(_problem_gone(10))
    _recompute_at(case, 10)

    assert _eid("cause_gone") not in _refuting_row_ids(root)
    assert has_resolution_confirmation(case) is True


def test_a_cause_row_the_root_supports_still_confirms_nothing_once_failed():
    """The derivation reads the window, not the links: a cause row the root
    SUPPORTS-links (a stamp) still stops standing as a cause leg once its
    window failed, and a later mitigation's relief resolves nothing on it."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    cause_row = _cause_gone(9)
    case.evidence += [cause_row, _still_failing(9)]
    root.evidence_links.append(
        NodeEvidenceLink(
            evidence_id=cause_row.evidence_id,
            stance=EvidenceStance.SUPPORTS,
            reasoning="stamped",
            linked_at_turn=9,
        )
    )
    _recompute_at(case, 9)
    _assert_demoted(case, root, hyp)
    _mitigate(case)

    _assert_not_resolved_on_the_failed_cause(case)


def test_the_latest_persistence_is_what_a_confirmation_must_follow():
    """The cause seen gone at 9, the problem at 10, both legs at 11, and the
    problem back at 12. The latest observation governs: the turn-11
    confirmation is overtaken by the turn-12 persistence."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _cause_gone(9),
        _still_failing(10),
        _row(
            "cause_gone_again",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            11,
            "max_connections still reads 100",
        ),
        _problem_gone(11),
        _still_failing(12, label="back_to_503"),
    ]
    _recompute_at(case, 12)

    _assert_demoted(case, root, hyp)


def test_a_standing_confirmation_with_no_persistence_is_metered_as_one():
    """With nothing observed persisting, a confirmed resolution is what the
    refusal reports, not the missing persistence."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [_cause_gone(9), _problem_gone(9)]
    with patch(
        "faultmaven.core.investigation.causal_graph.disconfirmation."
        "m6_demotion_refused_total"
    ) as counter:
        assert m6_disconfirmation_basis(case) is None
    counter.labels.assert_called_once_with(reason="resolution_confirmed")


# ---------------------------------------------------------------------------
# One answer to "did the latest fix work?" (``fix_windows``): the readiness
# gate and M6 read the same window, so they cannot disagree.
# ---------------------------------------------------------------------------


def _window_states(case):
    return [(w.fix_turn, w.state) for w in fix_windows(case)]


def test_a_fresh_cause_recheck_after_a_failure_resolves_nothing():
    """The fix failed at turn 10 and M6 demoted the cause. At 12 the model
    re-records the cause gone, beside a restart's relief. The re-check is in
    the same failed fix window, so it confirms nothing: only a new fix can."""
    case, root, hyp = _identified_case()
    _failed_fix(case, outcome_turn=9)
    case.evidence.append(_still_failing(10, label="still_503_at_10"))
    _recompute_at(case, 10)
    _accept(case, InvestigationActionType.MITIGATION, 11)
    case.evidence += [
        _row(
            "cause_rechecked",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            12,
            "max_connections still reads 100",
        ),
        _problem_gone(12, label="after_restart"),
    ]
    _recompute_at(case, 12)

    assert _window_states(case)[-1] == (8, FIX_FAILED)
    _assert_not_resolved_on_the_failed_cause(case)


def test_a_new_fix_after_a_failure_opens_a_window_that_resolves():
    case, root, hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)
    _accept(case, InvestigationActionType.SOLUTION, 12)
    case.evidence += [
        _row(
            "pgbouncer_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            13,
            "pgbouncer pool_mode is now transaction",
        ),
        _problem_gone(13, label="checkout_ok"),
    ]
    _recompute_at(case, 13)

    assert _window_states(case)[1:] == [(8, FIX_FAILED), (12, FIX_SUCCEEDED)]
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


def test_a_relapse_after_a_relief_fails_the_window():
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [_cause_gone(9), _problem_gone(10)]
    assert _window_states(case)[-1] == (8, FIX_SUCCEEDED)
    case.evidence.append(_still_failing(12, label="back_again"))

    assert _window_states(case)[-1] == (8, FIX_FAILED)
    assert has_resolution_confirmation(case) is False


def test_an_earlier_success_does_not_outlive_a_later_failed_fix():
    """Fix 1 confirmed at 4-5; at 8 a second fix, its cause seen gone at 9 and
    the problem there at 10. The gate reads the failure from the windows
    alone — no M6 recompute runs here — so the first window's confirmation
    is overtaken whether or not M6 could act."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 3)
    case.evidence += [
        _row(
            "first_cause_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            4,
            "max_connections now reads 60 after the first change",
        ),
        _problem_gone(5, label="first_relief"),
    ]
    assert has_resolution_confirmation(case) is True
    _failed_fix(case, fix_turn=8, outcome_turn=9)
    case.evidence.append(_still_failing(10, label="still_503_after_second_fix"))

    assert _window_states(case)[1:] == [(3, FIX_SUCCEEDED), (8, FIX_FAILED)]
    assert has_resolution_confirmation(case) is False


def test_an_out_of_band_confirmation_does_not_outlive_a_later_failed_fix():
    """Window 0 — no dated fix — confirmed at 4-5, then a dated fix fails."""
    case, root, hyp = _identified_case()
    case.evidence += [
        _row(
            "oob_cause_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            4,
            "max_connections was raised to 60 by hand",
        ),
        _problem_gone(5, label="oob_relief"),
    ]
    assert has_resolution_confirmation(case) is True
    _failed_fix(case, fix_turn=8, outcome_turn=9)
    case.evidence.append(_still_failing(10, label="still_503"))

    assert has_resolution_confirmation(case) is False


def test_the_problem_back_in_a_new_fixs_turn_is_the_old_windows_relapse():
    """ "It's back, so I'm applying the next fix" at turn 8: the persistence row
    in the new fix's own turn cannot be that fix's failure (it may quote what
    came before), so it is the previous window's relapse, and that window's
    confirmation no longer stands."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 3)
    case.evidence += [
        _row(
            "first_cause_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            4,
            "max_connections now reads 60 after the first change",
        ),
        _problem_gone(5, label="first_relief"),
    ]
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_still_failing(8, label="back_at_the_second_fix"))

    assert _window_states(case)[1:] == [(3, FIX_FAILED), (8, FIX_UNKNOWN)]
    assert has_resolution_confirmation(case) is False


def test_after_a_failure_the_gate_asks_for_a_new_fix_not_a_confirmation():
    """Asking the user to confirm again would loop: nothing they confirm in a
    failed window counts. The gate names the failure and asks for a new fix."""
    case, root, hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)
    readiness = assess_resolution_readiness(case)

    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert readiness.missing[0] == "a new fix after the one that did not hold"
    assert "didn't hold" in readiness.message
    assert "just tell me" not in readiness.message


def test_an_absence_row_inside_a_failed_window_tells_the_model_to_record_a_fix():
    from faultmaven.core.investigation.milestone_engine.stage_gates import (
        _flag_absence_inside_a_failed_fix_window,
    )

    case, root, hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)
    case.current_turn = 10
    quiet = {}
    assert _flag_absence_inside_a_failed_fix_window(case, quiet) is False
    assert quiet == {}

    case.evidence.append(_problem_gone(10, label="fine_now"))
    metadata = {}
    assert _flag_absence_inside_a_failed_fix_window(case, metadata) is True
    feedback = metadata["system_feedback"]
    assert "did not hold" in feedback
    assert "SolutionToAdd" in feedback and "solution_accepted" in feedback


def test_a_failed_fix_still_proves_the_problem_existed():
    """The false-alarm refusal reads every cause row, failed window or not: a
    fix seen failing is the strongest proof the problem was real."""
    from faultmaven.core.investigation.problem_status import invalidation_refusal

    case, root, hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)
    case.evidence.append(_problem_gone(10, label="never_there"))
    assert cause_elimination_rows(case) == []

    refusal = invalidation_refusal(case, [_eid("never_there")], "logs are clean")
    assert refusal == "a cause was confirmed eliminated, so the problem existed"


def test_a_relief_without_a_cause_recheck_is_not_a_successful_fix():
    """The problem gone after a fix with no cause re-check does not show the
    fix worked (a mitigation can relieve it with the cause still there): the
    window stays UNKNOWN, and M6's refusal names the missing cause check."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_problem_gone(9))

    assert _window_states(case)[-1] == (8, FIX_UNKNOWN)
    with patch(
        "faultmaven.core.investigation.causal_graph.disconfirmation."
        "m6_demotion_refused_total"
    ) as counter:
        assert m6_disconfirmation_basis(case) is None
    counter.labels.assert_called_once_with(reason="no_cause_removal")


# ---------------------------------------------------------------------------
# Review round on the fix windows.
# ---------------------------------------------------------------------------


def test_a_failure_reported_with_the_next_fix_is_still_demoted():
    """ "Still 503, so I'm putting pgbouncer in": the persistence and the next
    fix's acceptance land in one turn. The next fix has no verdict yet, so it
    does not erase the failure: M6 demotes the cause whose fix failed, and the
    gate agrees nothing is confirmed. The next fix's own confirmation later
    resolves the case."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_cause_gone(9))
    _recompute_at(case, 9)
    _accept(case, InvestigationActionType.SOLUTION, 10)
    case.evidence.append(_still_failing(10))
    _recompute_at(case, 10)

    assert _window_states(case)[1:] == [(8, FIX_FAILED), (10, FIX_UNKNOWN)]
    _assert_demoted(case, root, hyp)
    assert has_resolution_confirmation(case) is False

    case.evidence += [
        _row(
            "pgbouncer_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            11,
            "pgbouncer pool_mode is now transaction",
        ),
        _problem_gone(11, label="checkout_ok"),
    ]
    _recompute_at(case, 11)
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


def test_m6_reads_the_latest_fix_not_an_earlier_one():
    """A first fix that worked, then a second fix whose cause is seen gone and
    the problem still there: the second fix's failure demotes the cause."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 3)
    case.evidence += [
        _row(
            "first_cause_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            4,
            "max_connections now reads 60 after the first change",
        ),
        _problem_gone(5, label="first_relief"),
    ]
    _recompute_at(case, 5)
    _failed_fix(case, fix_turn=8, outcome_turn=9)
    case.evidence.append(_still_failing(10, label="still_503_after_second_fix"))
    _recompute_at(case, 10)

    _assert_demoted(case, root, hyp)


@pytest.mark.parametrize(
    "statement, symptom",
    [
        (
            "a memory leak in the gunicorn worker",
            "gunicorn worker memory is climbing again",
        ),
        (
            "the disk is full on the db host",
            "disk usage on the db host is at 80 percent",
        ),
    ],
    ids=["sibling-candidate", "refuted-guess"],
)
def test_a_symptom_about_another_candidate_is_not_the_problem_persisting(
    statement, symptom
):
    """The persistence frame is the problem plus the identified cause's chain,
    not every candidate's: a symptom that speaks to a sibling candidate or a
    refuted guess does not fail the fix that removed the identified cause."""
    case, root, hyp = _identified_case()
    sibling = _add_sibling_chain(case)
    sibling.statement = statement
    case.hypotheses["hyp_00000000eeee"].statement = statement
    case.hypotheses["hyp_00000000eeee"].state = HypothesisState.REFUTED
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _cause_gone(9),
        _row("other_symptom", EvidenceCategory.SYMPTOM_EVIDENCE, 10, symptom),
    ]
    _recompute_at(case, 10)

    assert _window_states(case)[-1] == (8, FIX_UNKNOWN)
    assert hyp.state != HypothesisState.REFUTED
    assert _engine_rows(case) == []


def test_a_new_fix_and_its_confirmation_in_one_turn_resolve():
    """The one-turn recovery: the new fix accepted, its cause seen gone and the
    problem gone, all in the turn after the failure."""
    case, root, hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)
    _accept(case, InvestigationActionType.SOLUTION, 10)
    case.evidence += [
        _row(
            "pgbouncer_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            10,
            "pgbouncer pool_mode is now transaction",
        ),
        _problem_gone(10, label="checkout_ok"),
    ]
    _recompute_at(case, 10)

    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


def test_a_success_between_two_failures_confirms_nothing():
    """Window 1 failed, window 2 succeeded, window 3 failed, window 4 is a new
    fix with nothing observed yet: the last observed window failed, so the
    resolution stands on nothing — not on window 2's rows."""
    case, root, hyp = _identified_case()
    for fix, cause, outcome in ((3, 4, 5), (8, 9, None), (12, 13, 14)):
        _accept(case, InvestigationActionType.SOLUTION, fix)
        case.evidence.append(
            _row(
                f"cause_gone_{cause}",
                EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
                cause,
                "max_connections now reads 100 after the config change",
            )
        )
        if outcome:
            case.evidence.append(_still_failing(outcome, label=f"still_{outcome}"))
    case.evidence.append(_problem_gone(10, label="ok_10"))
    _accept(case, InvestigationActionType.SOLUTION, 16)

    assert [w.state for w in fix_windows(case)[1:]] == [
        FIX_FAILED,
        FIX_SUCCEEDED,
        FIX_FAILED,
        FIX_UNKNOWN,
    ]
    assert has_resolution_confirmation(case) is False


def test_a_pre_fix_cause_row_does_not_confirm_the_fix_after_it():
    """The cause recorded gone at 7, before the fix executed at 8; the problem
    still there at 9; a restart's relief at 10. The fix's own window holds no
    cause re-check, so nothing confirms it — the turn-7 row is about the state
    before the fix, and it must not pair with the relief."""
    case, root, hyp = _identified_case()
    case.evidence.append(_cause_gone(7))
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_still_failing(9))
    _accept(case, InvestigationActionType.MITIGATION, 10)
    case.evidence.append(_problem_gone(10, label="restart_relief"))

    assert has_resolution_confirmation(case) is False


def test_an_out_of_band_confirmation_does_not_confirm_a_later_fix():
    """An out-of-band fix confirmed at 4-5, the problem back at 8 with a
    recorded fix, and still there at 9: the out-of-band confirmation says
    nothing about the recorded fix, as for a recorded first fix."""
    case, root, hyp = _identified_case()
    case.evidence += [
        _row(
            "oob_cause_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            4,
            "max_connections was raised to 60 by hand",
        ),
        _problem_gone(5, label="oob_relief"),
    ]
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _still_failing(8, label="back_at_8"),
        _still_failing(9, label="still_9"),
    ]

    assert has_resolution_confirmation(case) is False


def test_an_unobserved_newer_fix_leaves_the_confirmation_standing():
    """A fix confirmed, then another fix recorded with nothing observed after
    it yet: the newer fix has no verdict, so the window before it still
    answers (an execution stamp can lag the user's report)."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [_cause_gone(9), _problem_gone(10)]
    _accept(case, InvestigationActionType.SOLUTION, 12)

    assert has_resolution_confirmation(case) is True


def test_the_notice_is_not_sent_on_the_turn_that_establishes_the_failure():
    from faultmaven.core.investigation.milestone_engine.stage_gates import (
        _flag_absence_inside_a_failed_fix_window,
    )

    case, root, hyp = _identified_case()
    _failed_fix(case)
    case.current_turn = 9
    metadata = {}
    assert _flag_absence_inside_a_failed_fix_window(case, metadata) is False
    assert metadata == {}


def test_a_failure_seen_through_the_causes_chain_outlives_the_demotion():
    """The persistence row speaks to the identified cause's chain, not to the
    problem statement's words. Once M6 demotes that cause, its chain stays in
    the frame (it carries the engine's disconfirmation), so the window stays
    FAILED and a later relief resolves nothing."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _cause_gone(9),
        _row(
            "pool_still_starved",
            EvidenceCategory.SYMPTOM_EVIDENCE,
            10,
            "max_connections pool size limit still exhausted",
        ),
    ]
    _recompute_at(case, 10)
    _assert_demoted(case, root, hyp)
    _mitigate(case, accepted_turn=11, relief_turn=12)

    assert _window_states(case)[-1] == (8, FIX_FAILED)
    _assert_not_resolved_on_the_failed_cause(case)


def test_back_then_a_new_fix_then_up_in_one_turn_fails_the_old_fix():
    """ "It came back, so I applied the pool change, and now it's up" in one
    turn: the relapse is the old fix's (it cannot be the new fix's failure),
    the relief the new fix's (a window starts at its fix's execution turn).
    The old fix failed, so M6 demotes its cause; the new fix still needs its
    own cause check before anything resolves."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_cause_gone(9))
    _recompute_at(case, 9)
    _accept(case, InvestigationActionType.SOLUTION, 12)
    case.evidence += [_still_failing(12, label="back"), _problem_gone(12, label="up")]
    _recompute_at(case, 12)

    assert _window_states(case)[1:] == [(8, FIX_FAILED), (12, FIX_UNKNOWN)]
    _assert_demoted(case, root, hyp)
    assert has_resolution_confirmation(case) is False


# ---------------------------------------------------------------------------
# A newer fix answers only once its outcome was looked at.
# ---------------------------------------------------------------------------


def test_a_stamp_lagging_the_cause_check_still_resolves_on_the_next_turn():
    """ "The config reads 100" at 7, the acceptance stamped at 8, "checkout
    works" at 9. A problem-gone row alone does not look at the stamped fix,
    so the window before it — holding the cause check — still answers."""
    case, root, hyp = _identified_case()
    case.evidence.append(_cause_gone(7))
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_problem_gone(9))

    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


@pytest.mark.parametrize(
    "later",
    ["relief", "unrelated_symptom"],
)
def test_a_later_fix_seen_only_by_a_relief_or_an_aside_keeps_the_resolution(later):
    """A fix confirmed (cause at 9, problem gone at 10), then a hardening fix
    at 12 followed by only a relief or an unrelated remark: the later fix's
    outcome was not looked at, so the confirmed resolution stands."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [_cause_gone(9), _problem_gone(10)]
    _accept(case, InvestigationActionType.SOLUTION, 12)
    if later == "relief":
        case.evidence.append(_problem_gone(13, label="still_fine"))
    else:
        case.evidence.append(
            _row(
                "aside",
                EvidenceCategory.SYMPTOM_EVIDENCE,
                12,
                "also seeing latency on the billing service",
            )
        )

    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


def test_the_cause_check_between_two_fixes_still_confirms_a_later_relief():
    """Fix at 8, the cause seen gone at 9, another fix stamped at 10, the
    problem gone at 13: the newer fix was not looked at, so the cause check
    before it and the relief after it confirm the resolution, as on main."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence.append(_cause_gone(9))
    _accept(case, InvestigationActionType.SOLUTION, 10)
    case.evidence.append(_problem_gone(13))

    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


def test_the_problem_leg_counts_from_the_confirmed_fix_not_a_newer_stamp():
    """Fix at 8, "the target is up" at 9, "the config reads 100" at 10, then an
    unobserved fix stamped at 12: the problem leg counts from the fix the
    resolution confirms, so the turn-9 row still stands."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [_problem_gone(9), _cause_gone(10)]
    _accept(case, InvestigationActionType.SOLUTION, 12)

    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


def test_after_a_failure_a_relief_alone_asks_for_the_cause_recheck():
    """The fix failed; a new fix at 12 is followed only by the problem gone.
    The new fix is unconfirmed, and the ask names the missing leg — the cause
    re-checked after the fix — not the problem-gone leg already given."""
    case, root, hyp = _identified_case()
    _failed_fix(case)
    _recompute_at(case, 9)
    _accept(case, InvestigationActionType.SOLUTION, 12)
    case.evidence.append(_problem_gone(13, label="up_after_fix_2"))
    readiness = assess_resolution_readiness(case)

    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert "cause re-checked after the fix" in readiness.message

    case.evidence.append(
        _row(
            "pgbouncer_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            14,
            "pgbouncer pool_mode is now transaction",
        )
    )
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY


def test_a_failure_in_the_fixed_causes_own_terms_counts_without_a_validated_root():
    """No root is validated (the cause was never grounded), so the frame has
    no identified chain. The persistence row speaks in the terms of the cause
    the fix addressed — the chain its cause row is about — so it still fails
    the fix, and a later relief resolves nothing."""
    case, root, hyp = _identified_case()
    root.node_state = NodeState.CANDIDATE
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _row(
            "pool_raised",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            9,
            "max_connections raised above the checkout pool size",
        ),
        _row(
            "slots_still_held",
            EvidenceCategory.SYMPTOM_EVIDENCE,
            10,
            "pg_stat_activity: max_connections pool size still exhausted",
        ),
    ]
    _accept(case, InvestigationActionType.MITIGATION, 10)
    case.evidence.append(_problem_gone(11, label="restart_relief"))

    assert _window_states(case)[-1] == (8, FIX_FAILED)
    assert has_resolution_confirmation(case) is False


def test_with_no_cause_identified_a_failure_in_any_candidates_terms_counts():
    """No root validated and the fix's cause row too terse to say which chain
    it removed: the frame holds every candidate's chain, so a persistence row
    phrased in the cause's own terms still fails the fix, and a later relief
    resolves nothing. M6 has no identified cause to demote, so the wider frame
    only makes the gate more careful."""
    case, root, hyp = _identified_case()
    root.node_state = NodeState.CANDIDATE
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _cause_gone(9),
        _row(
            "slots_still_held",
            EvidenceCategory.SYMPTOM_EVIDENCE,
            10,
            "pg_stat_activity: the pool size is still exhausted",
        ),
    ]
    _accept(case, InvestigationActionType.MITIGATION, 10)
    case.evidence.append(_problem_gone(11, label="restart_relief"))

    assert _window_states(case)[-1] == (8, FIX_FAILED)
    assert has_resolution_confirmation(case) is False


def test_the_cause_recheck_ask_is_not_used_once_the_problem_is_back():
    """A relief then the problem seen again, with no cause re-check: the
    problem is not gone, so the ask does not say it is."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 9)
    case.evidence += [_problem_gone(10), _still_failing(11, label="back_at_11")]
    message = assess_resolution_readiness(case).message

    assert "cause re-checked after the fix" not in message


def test_a_siblings_fix_failing_in_the_siblings_terms_fails_its_window():
    """The case identifies A, but the latest fix addressed candidate B: its
    cause row is about B's chain, so B's chain is in the window's frame and a
    persistence row in B's terms fails the fix. The gate resolves nothing on
    it; M6 leaves A alone, since a failed fix of B disconfirms B, not A."""
    case, root, hyp = _identified_case()
    _add_sibling_chain(case)
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _row(
            "leak_gone",
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            9,
            "gunicorn worker memory leak patched, worker RSS now flat",
        ),
        _row(
            "leak_back",
            EvidenceCategory.SYMPTOM_EVIDENCE,
            10,
            "gunicorn worker memory is climbing again",
        ),
    ]
    _recompute_at(case, 10)
    _mitigate(case, accepted_turn=11, relief_turn=12)

    assert _window_states(case)[-1] == (8, FIX_FAILED)
    assert has_resolution_confirmation(case) is False
    assert hyp.state != HypothesisState.REFUTED


def test_a_demoted_causes_failure_stands_after_another_cause_is_identified():
    """A's fix failed in A's own terms and M6 demoted A; the investigation
    then validates B. A's chain stays in the frame because it carries M6's
    disconfirmation, so A's window stays FAILED and a relief does not turn it
    into a success."""
    case, root, hyp = _identified_case()
    sibling = _add_sibling_chain(case)
    _accept(case, InvestigationActionType.SOLUTION, 8)
    case.evidence += [
        _cause_gone(9),
        _row(
            "pool_still_starved",
            EvidenceCategory.SYMPTOM_EVIDENCE,
            10,
            "max_connections pool size limit still exhausted",
        ),
    ]
    _recompute_at(case, 10)
    _assert_demoted(case, root, hyp)
    sibling.node_state = NodeState.VALIDATED
    _accept(case, InvestigationActionType.MITIGATION, 11)
    case.evidence.append(_problem_gone(12, label="restart_relief"))

    assert _window_states(case)[-1] == (8, FIX_FAILED)
    assert has_resolution_confirmation(case) is False


def test_a_same_turn_relief_and_persistence_ask_for_the_cause_recheck():
    """The problem seen gone and still present in one turn after the fix, no
    cause re-check: the same-turn tie reads as gone (the owner's ruling), so
    the leg still owed is the cause."""
    case, root, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 9)
    case.evidence += [_problem_gone(10), _still_failing(10, label="also_10")]

    assert "cause re-checked after the fix" in assess_resolution_readiness(case).message
