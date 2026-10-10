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

from faultmaven.core.investigation.causal_graph.ingestion import seed_problem_node
from faultmaven.core.investigation.cause_assurance import (
    ENGINE_EVIDENCE_AUTHOR,
    cause_elimination_rows,
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


@pytest.mark.parametrize(
    "fix_turn", [8, 9], ids=["fix-a-turn-earlier", "fix-in-the-outcome-turn"]
)
def test_a_model_refutation_in_the_cause_rows_turn_closes_the_route_too(fix_turn):
    """The FAILURE-PATH-compliant shape: the model refutes the cause in the
    very turn the cause row and the persistence land. The evidence arm fires
    there, and the disconfirmation window's ``>=`` keeps a same-turn cause row
    qualifying, so the window alone does not close the route. M6 marking the
    failed fix's cause rows does — including when the fix was executed in
    that same turn, where the record alone establishes nothing."""
    case, root, hyp = _identified_case()
    _failed_fix(case, fix_turn=fix_turn, outcome_turn=9)
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
# What the marking records, and what it leaves alone.
# ---------------------------------------------------------------------------


def _refuting_row_ids(root):
    return {
        link.evidence_id
        for link in root.evidence_links
        if link.stance == EvidenceStance.REFUTES
    }


def test_marking_takes_only_the_failed_fixs_cause_rows():
    """A cause row from before the fix belongs to no fix that failed here."""
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

    marked = _refuting_row_ids(root)
    assert _eid("cause_gone") in marked
    assert _eid("pre_fix_gone") not in marked


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
