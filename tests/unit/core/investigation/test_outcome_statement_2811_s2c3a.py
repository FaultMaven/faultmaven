"""The engine's outcome statement under a model turn's reply (#2811 S2c-3a).

On a model turn the reply ends with "**Case record:**" and a clause per
closed-set fact the turn changed, exactly when the turn's own prose does not
state it. The facts are compared between the turn-start snapshot
(``OutcomeBaseline``) and the turn-final record, every record here built by
``compute_case_outcome`` from a real case. The copy is pinned verbatim here,
not imported. No live LLM.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation import problem_status as ps
from faultmaven.core.investigation.milestone_engine import (
    turn_application,
    turn_completion,
    turn_narration,
)
from faultmaven.core.investigation.milestone_engine.engine import MilestoneEngine
from faultmaven.core.investigation.milestone_engine.response_synthesis import (
    is_agent_response_synthesized,
)
from faultmaven.core.investigation.milestone_engine.structured_output import (
    _synthesize_agent_response,
)
from faultmaven.core.investigation.milestone_engine.turn_completion import (
    _compose_turn_reply,
    _outcome_statement_for_turn,
    _statement_record,
)
from faultmaven.core.investigation.milestone_engine.turn_narration import (
    OUTCOME_STATED_KEY,
    OUTCOME_WRITTEN_KEY,
    outcome_baseline,
)
from faultmaven.core.investigation.narration.codes import (
    DECIDED_FROM,
    PROSE_KEYS,
    OfferCode,
)
from faultmaven.core.investigation.narration.fix_verification import Content
from faultmaven.core.investigation.narration.replies import (
    NO_ROOT_ON_RECORD,
    ResolutionContent,
    confirmation_params,
)
from faultmaven.core.investigation.narration.statement import (
    STATED_BY,
    Change,
    OutcomeBaseline,
    OutcomeChange,
    offer_stated,
    outcome_changes,
    outcome_statement,
)
from faultmaven.core.investigation.offers import remember_resolve_request
from faultmaven.core.investigation.outcome import (
    compute_case_outcome,
    record_case_outcome,
)
from faultmaven.core.investigation.problem_status import (
    commit_revision,
    decline_revision,
    invalidate_problem,
    restores_to,
    unverify_problem,
    verify_problem,
    withdraw_invalidation,
)
from faultmaven.core.investigation.schemas import (
    EvidenceTrail,
    InvestigationResponse_Diagnosis,
    InvestigationResponse_Treatment,
    MilestoneJustifications,
    MilestoneUpdates,
    ProblemVerificationUpdate,
    SolutionToAdd,
)
from faultmaven.infrastructure.llm.providers.base import StopReason
from faultmaven.modules.case.contracts import (
    MESSAGE_METADATA_AGENT_SYNTHESIZED,
    AnswerVia,
    CaseState,
    CauseState,
    ConfidenceLevel,
    EvidenceCategory,
    InvestigationActionType,
    PoolEmptyReason,
    ProblemStatus,
    ProposedAction,
    ResolutionReason,
    RootCauseConclusion,
    SolutionType,
    WritePoint,
)
from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
    _cited_but_unlinked,
    _contested,
    _failed_fix,
    _fallen_case,
    _fix,
    _held_open,
    _other_hypothesis,
    _relapse,
    _rootless,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _ROOT_ID,
    _identified_case,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _DSU,
    _apply,
    _case,
    _evidence,
    _propose,
    _revision_turn_with_cause_work,
    _row,
)
from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
    _engine as _revision_engine,
)
from tests.unit.core.investigation.test_record_prose_2811_s2b2c import _Poisoned

pytestmark = pytest.mark.unit

_C = EvidenceCategory
_V, _U = ProblemStatus.VERIFIED, ProblemStatus.UNVERIFIED
_P, _I = ProblemStatus.REVISION_PENDING, ProblemStatus.INVALIDATED
SEP = "\n\n---\n\n"
HEAD = "**Case record:** "


def _record(case, prior=None):
    return compute_case_outcome(prior, case, at=WritePoint.CYCLE)


def _kinds(changes) -> list:
    return [c.kind for c in changes]


def _base(record, *, restores=None, comparable=True, accepted=()) -> OutcomeBaseline:
    return OutcomeBaseline(
        record=record,
        restores_to=restores,
        problem_comparable=comparable,
        accepted=frozenset(accepted),
    )


def _unidentified(case):
    """The case before its cause was identified: no leg, nothing recorded."""
    before = copy.deepcopy(case)
    before.progress.cause_state = CauseState.CANDIDATES
    before.progress.identified_cause_root_ids = []
    before.root_cause_conclusion = None
    for node in before.causal_nodes.values():
        node.metadata = {}
    return before


def _rcc(case) -> None:
    """The cause stands on the conclusion alone: a backstop leg."""
    case.progress.cause_state = CauseState.CANDIDATES
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="the orders pool is capped at 5 connections",
        mechanism="requests queue behind the cap",
        confidence_level=ConfidenceLevel.CONFIDENT,
        likelihood=0.8,
    )


# ---------------------------------------------------------------------------
# The restore function: one reading of the two owners of ``prior_status``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build, status, expected",
    [
        (lambda c: None, _V, None),
        (lambda c: None, _U, None),
        (lambda c: None, _P, None),  # no revision open
        (lambda c: _propose(c), _P, _V),
        (lambda c: None, _I, _U),  # no invalidation: as _clear_invalidation
        (lambda c: _invalidate(c), _I, _V),
    ],
    ids=["verified", "unverified", "pending-none", "pending", "inval-none", "inval"],
)
def test_restores_to_reads_each_owner(build, status, expected):
    case = _case(_V)
    build(case)
    assert restores_to(case, status) == expected


def _invalidate(case) -> None:
    ids = [e.evidence_id for e in case.evidence] or []
    row = _evidence(_C.SYMPTOM_ABSENCE_EVIDENCE, "absent")
    case.evidence.append(row)
    invalidate_problem(case, evidence_ids=[*ids, row.evidence_id], basis="no 5xx")


@pytest.fixture
def spy(monkeypatch):
    calls = []

    def _spy(case, status):
        calls.append(status)
        return restores_to(case, status)

    monkeypatch.setattr(ps, "restores_to", _spy)
    monkeypatch.setattr(turn_narration, "restores_to", _spy)
    return calls


def test_a_decline_returns_to_what_restores_to_reads(spy):
    case = _case(_V)
    _propose(case)
    decline_revision(case, via=AnswerVia.CLICK)
    assert case.progress.problem_status == _V
    assert spy == [_P]


def test_a_withdrawal_returns_to_what_restores_to_reads(spy):
    case = _case(_V)
    _invalidate(case)
    assert withdraw_invalidation(case, basis="the user saw the 503s")
    assert case.progress.problem_status == _V
    assert spy == [_I]


def test_a_withdrawal_with_no_finding_on_record_returns_to_unverified(spy):
    case = _case(_V)
    _invalidate(case)
    case.problem_verification.invalidation = None
    withdraw_invalidation(case, basis="b")
    assert case.progress.problem_status == _U
    assert spy == [_I]


def test_the_baseline_reads_restores_to_by_the_records_own_status(spy):
    case = _case(_V)
    _propose(case)
    record_case_outcome(case, at=WritePoint.COMMIT)
    baseline = outcome_baseline(case)
    assert (baseline.restores_to, baseline.problem_comparable) == (_V, True)
    assert spy == [_P]
    assert baseline.record is not case.progress.outcome, "a deep copy"
    assert baseline.record == case.progress.outcome


def test_the_baseline_of_a_case_with_no_record_holds_none():
    case = _case(_V)
    case.progress.outcome = None
    assert outcome_baseline(case) == OutcomeBaseline(None, None, False, frozenset())


# ---------------------------------------------------------------------------
# outcome_changes: the problem
# ---------------------------------------------------------------------------


def _turn_of(case, move) -> list:
    """The changes ``move`` makes to ``case``, compared as a turn would."""
    record_case_outcome(case, at=WritePoint.COMMIT)
    baseline = outcome_baseline(case)
    move(case)
    return _kinds(outcome_changes(baseline, _record(case, case.progress.outcome)))


def _silent_with_control(case, move) -> None:
    """``move`` states nothing; the same turn with a fix carried out too
    states that fix alone (the same-turn positive control)."""
    record_case_outcome(case, at=WritePoint.COMMIT)
    baseline = outcome_baseline(case)
    control = copy.deepcopy(case)
    move(case)
    move(control)
    _carried_out(control, case.current_turn)
    assert outcome_changes(baseline, _record(case, case.progress.outcome)) == []
    assert _kinds(
        outcome_changes(baseline, _record(control, control.progress.outcome))
    ) == [OutcomeChange.FIX_CARRIED_OUT]


def _carried_out(case, turn, description="restart the orders service", **kw):
    """A SOLUTION accepted at ``turn``, appended to the case."""
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description=description,
            proposed_in_turn=turn - 1,
            accepted_in_turn=turn,
            state="accepted",
            **kw,
        )
    )


def _decline(case):
    decline_revision(case, via=AnswerVia.CLICK)


def _confirm(case):
    commit_revision(case, via=AnswerVia.CLICK)


def test_verifying_states_the_problem_confirmed():
    case = _case(_U)
    assert _turn_of(case, lambda c: verify_problem(c, via="t")) == [
        OutcomeChange.PROBLEM_VERIFIED
    ]
    assert _turn_of(case, lambda c: None) == [], "and nothing after"


def test_leaving_verified_states_no_longer_confirmed():
    case = _case(_V)
    assert _turn_of(case, lambda c: unverify_problem(c, via="t")) == [
        OutcomeChange.PROBLEM_UNCONFIRMED
    ]


def test_moving_into_a_revision_hold_states_nothing_its_confirm_states():
    case = _case(_U)
    _silent_with_control(case, _propose)
    assert _turn_of(case, _confirm) == [OutcomeChange.PROBLEM_VERIFIED]


def test_moving_from_verified_into_a_revision_hold_states_nothing():
    case = _case(_V)
    _silent_with_control(case, _propose)
    # Control: the same snapshot, a decline and a retraction in one turn.
    assert _turn_of(case, lambda c: (_decline(c), unverify_problem(c, via="t"))) == [
        OutcomeChange.PROBLEM_UNCONFIRMED
    ]


def test_a_declined_revision_restores_and_states_nothing():
    case = _case(_V)
    _propose(case)
    _silent_with_control(case, _decline)
    assert case.progress.problem_status == _V


def test_a_declined_revision_of_a_false_alarm_states_nothing():
    case = _case(_V)
    _invalidate(case)
    _propose(case)
    _silent_with_control(copy.deepcopy(case), _decline)
    # Control: the same hold confirmed verifies.
    assert _turn_of(case, _confirm) == [OutcomeChange.PROBLEM_VERIFIED]


def test_a_finding_states_and_its_withdrawal_restores_silently():
    case = _case(_V)
    assert _turn_of(case, _invalidate) == [OutcomeChange.PROBLEM_INVALIDATED]
    _silent_with_control(
        case, lambda c: withdraw_invalidation(c, basis="the user saw it")
    )
    assert case.progress.problem_status == _V


def test_a_baseline_the_live_status_disagrees_with_states_no_problem_clause():
    case = _case(_U)
    record_case_outcome(case, at=WritePoint.COMMIT)
    baseline = outcome_baseline(case)
    verify_problem(case, via="t")
    now = _record(case, case.progress.outcome)
    stale = OutcomeBaseline(
        baseline.record, baseline.restores_to, False, baseline.accepted
    )
    assert _kinds(outcome_changes(baseline, now)) == [OutcomeChange.PROBLEM_VERIFIED]
    assert outcome_changes(stale, now) == []


def test_a_record_minted_this_turn_states_nothing():
    case = _case(_U)
    before = _record(case)
    verify_problem(case, via="t")
    now = _record(case)
    assert outcome_changes(_base(None), now) == []
    # Control: the same turn-final record against a snapshot states it.
    assert _kinds(outcome_changes(_base(before), now)) == [
        OutcomeChange.PROBLEM_VERIFIED
    ]


# ---------------------------------------------------------------------------
# outcome_changes: the cause
# ---------------------------------------------------------------------------


def test_an_identified_case_states_only_its_identification_then_nothing():
    case, _root, _hyp = _identified_case()
    before = _record(_unidentified(case))
    now = _record(case, before)
    # The trap the pool predicate avoids: an identified root is "fallen" too.
    assert now.cause.fallen_root_ids == [_ROOT_ID]
    assert _kinds(outcome_changes(_base(before), now)) == [
        OutcomeChange.CAUSE_IDENTIFIED
    ]
    assert outcome_changes(_base(now), _record(case, now)) == []


def _fallen():
    case, _root = _fallen_case()
    _other_hypothesis(case)
    _rootless(case)
    return case


def test_a_fallen_cause_states_once_and_its_return_states_again():
    identified = _record(_identified_case()[0])
    fallen = _record(_fallen(), identified)
    assert fallen.cause.pool_empty_reason == PoolEmptyReason.FALLEN_WITH_OTHER_STANDING
    assert _kinds(outcome_changes(_base(identified), fallen)) == [
        OutcomeChange.CAUSE_FALLEN
    ]
    assert outcome_changes(_base(fallen), _record(_fallen(), fallen)) == []
    back = _record(_identified_case()[0], fallen)
    assert _kinds(outcome_changes(_base(fallen), back)) == [
        OutcomeChange.CAUSE_STANDS_AGAIN
    ]


def test_a_leg_first_set_onto_a_fallen_pool_states_it_fell():
    case, _root, _hyp = _identified_case()
    before = _record(_unidentified(case))
    assert _kinds(outcome_changes(_base(before), _record(_fallen(), before))) == [
        OutcomeChange.CAUSE_FALLEN
    ]


@pytest.mark.parametrize("mask", [_held_open, _contested], ids=["held", "contested"])
def test_a_fallen_pool_masked_by_its_reason_still_reads_fallen(mask):
    fallen = _record(_fallen())
    masked_case = _fallen()
    mask(masked_case)
    masked = _record(masked_case, fallen)
    assert masked.cause.pool_empty_reason != PoolEmptyReason.FALLEN_WITH_OTHER_STANDING
    # Neither a second "no longer stands" nor a false "stands again".
    assert outcome_changes(_base(fallen), masked) == []
    # Control: from a standing cause, the masked pool reads as fallen.
    identified = _record(_identified_case()[0])
    assert _kinds(outcome_changes(_base(identified), masked)) == [
        OutcomeChange.CAUSE_FALLEN
    ]


def test_a_cleared_leg_states_no_longer_identified():
    case, _root, _hyp = _identified_case()
    identified = _record(case)
    assert _kinds(
        outcome_changes(_base(identified), _record(_unidentified(case), identified))
    ) == [OutcomeChange.CAUSE_CLEARED]


def _rcc_case(status=_V):
    case = _case(status)
    _rcc(case)
    return case


def test_a_hold_moves_a_backstop_leg_without_a_cause_clause():
    """Ruled 2026-11-24 (a): the rcc leg goes with the hold and comes back
    with its decline; neither is a cause clause."""
    case = _rcc_case()
    assert _record(case).cause.identification_leg == "rcc"
    assert _turn_of(case, _propose) == []
    assert _record(case).cause.identification_leg is None
    assert _turn_of(case, _decline) == []
    assert _record(case).cause.identification_leg == "rcc"
    # Control: off the hold the same leg states both ways.
    assert _turn_of(case, lambda c: unverify_problem(c, via="t")) == [
        OutcomeChange.PROBLEM_UNCONFIRMED,
        OutcomeChange.CAUSE_CLEARED,
    ]
    assert _turn_of(case, lambda c: verify_problem(c, via="t")) == [
        OutcomeChange.PROBLEM_VERIFIED,
        OutcomeChange.CAUSE_IDENTIFIED,
    ]


def test_leaving_a_finding_and_verifying_states_no_backstop_cause():
    """The residual, pinned: a finding withdrawn to UNVERIFIED and the
    problem verified in the same turn (INVALIDATED -> VERIFIED) states the
    problem, but not the rcc leg it brings back."""
    case = _rcc_case()
    unverify_problem(case, via="t")
    _invalidate(case)
    assert case.problem_verification.invalidation.prior_status == _U
    assert _turn_of(
        case,
        lambda c: (
            withdraw_invalidation(c, basis="the user saw it"),
            verify_problem(c, via="t"),
        ),
    ) == [OutcomeChange.PROBLEM_VERIFIED]
    assert _record(case).cause.identification_leg == "rcc"


def test_a_decline_onto_verified_with_a_new_backstop_leg_states_no_cause():
    """The residual's sibling, pinned: the decline restores VERIFIED and an
    rcc set the same turn is not stated. Control: off the hold it is."""
    case = _case(_V)
    _propose(case)
    assert _turn_of(case, lambda c: (_decline(c), _rcc(c))) == []
    assert _record(case).cause.identification_leg == "rcc"
    assert _turn_of(_case(_V), _rcc) == [OutcomeChange.CAUSE_IDENTIFIED]


def test_a_hold_ending_on_a_chain_leg_states_it_and_a_backstop_does_not():
    """The residual, pinned: a revision confirm that newly identifies on a
    backstop leg states no cause; on the chain leg it does."""
    case, _root, _hyp = _identified_case()
    pending = _unidentified(case)
    pending.progress.problem_status = _P
    prior = _record(pending)
    base = _base(prior, restores=_U)
    assert _kinds(outcome_changes(base, _record(case, prior))) == [
        OutcomeChange.PROBLEM_VERIFIED,
        OutcomeChange.CAUSE_IDENTIFIED,
    ]
    backstop = _unidentified(case)
    _rcc(backstop)
    now = _record(backstop, prior)
    assert now.cause.identification_leg == "rcc"
    assert _kinds(outcome_changes(base, now)) == [OutcomeChange.PROBLEM_VERIFIED]


# ---------------------------------------------------------------------------
# outcome_changes: the attempts, keyed by action_id
# ---------------------------------------------------------------------------


def _fixes(changes) -> list:
    return [
        (c.attempt.action_id, c.attempt.accepted_in_turn, c.attempt.kind)
        for c in changes
        if c.kind == OutcomeChange.FIX_CARRIED_OUT
    ]


def test_an_accepted_attempt_states_once_unkinded_as_a_fix():
    case, _root, _hyp = _identified_case()
    before = _record(case)
    fix = _fix(case, turn=6)
    now = _record(case, before)
    changes = outcome_changes(_base(before), now)
    assert _fixes(changes) == [(fix.action_id, 6, None)]
    assert outcome_statement(changes, frozenset()) == (
        HEAD + "a fix was carried out (turn 6)."
    )
    assert outcome_changes(_base(now), _record(case, now)) == []


def test_an_earlier_turns_fix_a_stale_snapshot_lacks_is_not_this_turns():
    """Only changes this turn made are stated (Q4 (a)): a fix the snapshot
    lacks but the case had accepted at turn start is not this turn's."""
    case, _root, _hyp = _identified_case()
    stale = _record(case)
    fix = _fix(case, turn=6)
    now = _record(case, stale)
    assert outcome_changes(_base(stale, accepted={fix.action_id}), now) == []
    # Control: the same records when the turn accepted it.
    assert _kinds(outcome_changes(_base(stale), now)) == [OutcomeChange.FIX_CARRIED_OUT]


def test_an_interception_states_a_workaround():
    case, _root, _hyp = _identified_case()
    before = _record(case)
    _fix(case, fix_kind="interception", target_ids=[_ROOT_ID])
    changes = outcome_changes(_base(before), _record(case, before))
    assert outcome_statement(changes, frozenset()) == (
        HEAD + "a workaround was carried out (turn 6)."
    )


def test_a_second_attempt_states_only_itself():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=6)
    before = _record(case)
    second = ProposedAction(
        case_id=case.case_id,
        action_type=InvestigationActionType.SOLUTION,
        description="cap the pool per worker",
        proposed_in_turn=8,
        accepted_in_turn=9,
        state="accepted",
    )
    case.proposed_actions.append(second)
    changes = outcome_changes(_base(before), _record(case, before))
    assert _fixes(changes) == [(second.action_id, 9, None)]


def test_window_zero_states_nothing():
    case, root, _hyp = _identified_case()
    before = _record(case)
    cited = _cited_but_unlinked(case, root)
    _relapse(case, rechecks=cited.evidence_id)
    now = _record(case, before)
    assert [a.action_id for a in now.attempts] == [None], "premise: window 0"
    assert _fixes(outcome_changes(_base(before), now)) == []
    # Control: the same turn with a fix carried out states that fix alone.
    fix = _fix(case, turn=9)
    assert _fixes(outcome_changes(_base(before), _record(case, before))) == [
        (fix.action_id, 9, None)
    ]


def test_a_carried_failed_attempt_states_nothing():
    failed = _failed_fix()
    prior = _record(failed)
    (attempt,) = [a for a in prior.attempts if a.action_id is not None]
    assert attempt.window.final_at is not None, "premise: a final FAILED window"
    # Control: it stated on the turn it was accepted.
    unfixed = copy.deepcopy(failed)
    unfixed.proposed_actions.clear()
    assert _fixes(outcome_changes(_base(_record(unfixed)), prior)) == [
        (attempt.action_id, 8, None)
    ]
    failed.proposed_actions.clear()
    now = _record(failed, prior)
    carried = [a.action_id for a in now.attempts if a.action_id is not None]
    assert carried == [attempt.action_id], "carried"
    assert _fixes(outcome_changes(_base(prior), now)) == []


def _this_turns_fixes(base, case) -> list:
    record_case_outcome(case, at=WritePoint.COMMIT)
    changes = outcome_changes(base, case.progress.outcome)
    return outcome_statement(changes, frozenset())


async def test_a_fix_dated_back_to_its_report_is_this_turns():
    """#2427: accepted at turn 11, dated to the report at 10; turn 11 made
    the change, so it is stated, dated as the record holds it."""
    from tests.unit.core.investigation import test_fix_registration_lag_2427 as lag

    eng, case, _hyp = lag._identified()
    await lag._reported(eng, case)
    record_case_outcome(case, at=WritePoint.COMMIT)
    case.current_turn = 11
    base = outcome_baseline(case)
    await lag._registered(eng, case, lag._PREVIOUS)
    assert _this_turns_fixes(base, case) == HEAD + "a fix was carried out (turn 10)."


async def test_a_fix_carried_through_a_hold_is_the_confirming_turns():
    """Accepted on hold turn 6, registered when turn 7 confirms the revision:
    turn 7 states it, dated 6."""
    from tests.unit.core.investigation import test_solution_attempts_2105 as sa
    from tests.unit.core.investigation.fix_turns_2039 import _turn

    eng, case = sa._identified()
    await _turn(eng, case, 5, solutions_to_add=[sa._fix(sa._SECOND)])
    (fix,) = sa._solutions(case)
    sa._revise(case)
    await _turn(eng, case, 6, milestones=sa._accepted(fix.action_id))
    record_case_outcome(case, at=WritePoint.COMMIT)
    case.current_turn = 7
    base = outcome_baseline(case)
    await sa._confirm(eng, case, 7)
    statement = _this_turns_fixes(base, case)
    assert statement is not None
    assert statement.endswith("a fix was carried out (turn 6).")


# ---------------------------------------------------------------------------
# outcome_statement: the ruled copy, its order and join, and ``stated``
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind, clause",
    [
        (OutcomeChange.PROBLEM_VERIFIED, "the problem is confirmed"),
        (OutcomeChange.PROBLEM_UNCONFIRMED, "the problem is no longer confirmed"),
        (
            OutcomeChange.PROBLEM_INVALIDATED,
            "the data shows the reported problem was not present",
        ),
        (OutcomeChange.CAUSE_IDENTIFIED, "a root cause is identified"),
        (
            OutcomeChange.CAUSE_FALLEN,
            "the root cause identified earlier no longer stands",
        ),
        (
            OutcomeChange.CAUSE_STANDS_AGAIN,
            "the root cause identified earlier stands again",
        ),
        (OutcomeChange.CAUSE_CLEARED, "the root cause is no longer identified"),
    ],
)
def test_each_clause_is_the_ruled_copy(kind, clause):
    assert outcome_statement([Change(kind)], frozenset()) == f"{HEAD}{clause}."


def _attempt(action_id, turn, kind=None):
    return SimpleNamespace(action_id=action_id, accepted_in_turn=turn, kind=kind)


def test_the_clauses_join_in_order_and_stated_drops_by_kind_and_action():
    changes = [
        Change(OutcomeChange.PROBLEM_VERIFIED),
        Change(OutcomeChange.CAUSE_IDENTIFIED),
        Change(OutcomeChange.FIX_CARRIED_OUT, _attempt("act_a", 5)),
        Change(OutcomeChange.FIX_CARRIED_OUT, _attempt("act_b", 6, "interception")),
    ]
    assert outcome_statement(changes, frozenset()) == (
        HEAD + "the problem is confirmed; a root cause is identified; a fix was "
        "carried out (turn 5); a workaround was carried out (turn 6)."
    )
    stated = frozenset(
        {
            (OutcomeChange.CAUSE_IDENTIFIED, None),
            (OutcomeChange.FIX_CARRIED_OUT, "act_a"),
        }
    )
    assert outcome_statement(changes, stated) == (
        HEAD + "the problem is confirmed; a workaround was carried out (turn 6)."
    )
    everything = frozenset(c.key for c in changes)
    assert outcome_statement(changes, everything) is None
    assert outcome_statement([], frozenset()) is None


def test_every_offer_key_says_what_it_states():
    """Coverage by the RECORD_PROSE keying: every (code, composer) a decision
    is logged under, and every code whose prose the reply composes."""
    offers = {k for k in DECIDED_FROM if k[0] in set(OfferCode)}
    assert set(STATED_BY) == offers
    assert {(code, None) for code in PROSE_KEYS} <= set(STATED_BY)


def _readiness(reason, missing=(), cause=None, **extra) -> dict:
    return {"reason": reason, "missing": list(missing), "cause": cause, **extra}


#: Record lines as ``resolution_record_lines`` writes them.
_CAUSE = (
    "- **The cause** — max_connections is 20: the re-check after the fix (turn 7)\n"
)
_NO_ROOT = "- **The cause** — no root cause on record: the re-check (turn 7)\n"
_PROBLEM = "- **The problem** — the reported problem, re-checked: the re-check (turn 7)"


def test_a_deferred_resolve_naming_only_the_problem_leaves_the_cause_to_state():
    stated = offer_stated(OfferCode.DEFERRED_RESOLVE, {"record_lines": _PROBLEM})
    changes = [Change(OutcomeChange.CAUSE_IDENTIFIED)]
    assert outcome_statement(changes, stated) == HEAD + "a root cause is identified."
    named = offer_stated(OfferCode.DEFERRED_RESOLVE, {"record_lines": _CAUSE})
    assert outcome_statement(changes, named) is None


def test_what_each_offer_prose_states():
    fallen = _record(_fallen()).cause
    identified = _record(_identified_case()[0]).cause
    I, F, P = (
        OutcomeChange.CAUSE_IDENTIFIED,
        OutcomeChange.CAUSE_FALLEN,
        OutcomeChange.PROBLEM_INVALIDATED,
    )
    gaps = ResolutionReason.DOCUMENTATION_GAPS
    cases = [
        (OfferCode.FALSE_ALARM_CLOSE, {}, {(P, None)}),
        (OfferCode.RCA_INFEASIBLE_CLOSE, {}, set()),
        (OfferCode.DEFERRED_CLOSE, {}, set()),
        (
            OfferCode.BACKSTOP_RESOLVE,
            {"identification_leg": "chain", "record_lines": ""},
            {(I, None)},
        ),
        (
            OfferCode.BACKSTOP_RESOLVE,
            {"identification_leg": None, "record_lines": "- x"},
            set(),
        ),
        (OfferCode.DEFERRED_RESOLVE, {"record_lines": _CAUSE + _PROBLEM}, {(I, None)}),
        (OfferCode.DEFERRED_RESOLVE, {"record_lines": _PROBLEM}, set()),
        (OfferCode.DEFERRED_RESOLVE, {"record_lines": _NO_ROOT + _PROBLEM}, set()),
        (OfferCode.DEFERRED_RESOLVE, {"record_lines": ""}, set()),
        (
            OfferCode.RESOLUTION_NEEDS_INFO,
            _readiness(gaps, ["root cause"], fallen),
            {(F, None)},
        ),
        (OfferCode.RESOLUTION_NEEDS_INFO, _readiness(gaps, ["root cause"]), set()),
        (
            OfferCode.RESOLUTION_NEEDS_INFO,
            _readiness(gaps, ["solution"], fallen),
            set(),
        ),
        (
            OfferCode.RESOLUTION_NEEDS_INFO,
            _readiness(gaps, ["root cause"], identified),
            set(),
        ),
        (
            OfferCode.NEEDS_INFO_UNMET_CLOSE,
            _readiness(ResolutionReason.FALSE_ALARM, suggest_close=True),
            {(P, None)},
        ),
        (
            OfferCode.NEEDS_INFO_UNMET_CLOSE,
            _readiness(ResolutionReason.FALSE_ALARM, suggest_close=False),
            set(),
        ),
        (
            OfferCode.MODEL_PROPOSAL,
            _readiness(ResolutionReason.FALSE_ALARM, composer="readiness"),
            set(),
        ),
    ]
    for code, params, expected in cases:
        assert offer_stated(code, params) == frozenset(expected), (code, params)


def _confirmed_attempts():
    """A fix carried out at 6 whose window SUCCEEDED: the confirming attempt."""
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
        _fix as fold_fix,
    )
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _cause_gone,
        _problem_gone,
    )

    case, _root, _hyp = _identified_case()
    fix = fold_fix(case, turn=6)
    case.evidence += [_cause_gone(7), _problem_gone(7)]
    record = _record(case)
    return record, fix


_CONTENT = ResolutionContent(
    root_cause="max_connections is 20",
    solution="the latest solution",
    has_evidence=True,
    has_commands=False,
)


def test_the_confirmation_states_what_its_lines_name():
    record, fix = _confirmed_attempts()
    text = {fix.action_id: Content(text="raise max_connections to 100")}.get
    params = confirmation_params(record, _CONTENT, text)
    assert params["solution"] == "raise max_connections to 100 (carried out turn 6)"
    I, X = OutcomeChange.CAUSE_IDENTIFIED, OutcomeChange.FIX_CARRIED_OUT
    assert offer_stated(OfferCode.REQUEST_READY, params, record.attempts) == {
        (I, None),
        (X, fix.action_id),
    }
    # No leg: the root line is "no root cause on record".
    no_leg = {**params, "root": NO_ROOT_ON_RECORD}
    assert offer_stated(OfferCode.REQUEST_READY, no_leg, record.attempts) == {
        (X, fix.action_id)
    }
    # No description: the latest solution is named, not the attempt.
    bare = confirmation_params(record, _CONTENT, lambda _id: None)
    assert bare["solution"] == "the latest solution"
    assert offer_stated(OfferCode.REQUEST_READY, bare, record.attempts) == {(I, None)}


# ---------------------------------------------------------------------------
# The composer: a pre-empted prose key, and pin 2 at the statement site
# ---------------------------------------------------------------------------


def _invalidated_and_identified():
    """Before: verified, no leg. Now: invalidated, the chain leg set."""
    case, _root, _hyp = _identified_case()
    before = _record(_unidentified(case))
    case.progress.problem_status = _I
    case.progress.outcome = _record(case, before)
    return case, before


async def test_a_pre_empted_prose_key_states_nothing_for_the_statement():
    """The false-alarm close pre-empts the backstop's prose, so the backstop's
    CAUSE_IDENTIFIED is not stated by the reply: the statement says it."""
    case, before = _invalidated_and_identified()
    metadata = {
        "milestones_completed": [],
        "false_alarm_closure_message": "FALSE ALARM PROSE",
        "resolution_ready_gate_message": "BACKSTOP PROSE",
        OUTCOME_WRITTEN_KEY: True,
        OUTCOME_STATED_KEY: {
            "false_alarm_closure_message": offer_stated(
                OfferCode.FALSE_ALARM_CLOSE, {}
            ),
            "resolution_ready_gate_message": offer_stated(
                OfferCode.BACKSTOP_RESOLVE,
                {"identification_leg": "chain", "record_lines": ""},
            ),
        },
    }
    result = await _compose_turn_reply(
        MagicMock(),
        None,
        MagicMock(),
        case_updated=case,
        follow_ups=[],
        metadata=metadata,
        plan=MagicMock(),
        redaction_ctx=None,
        response_obj=InvestigationResponse_Diagnosis(
            agent_response="Noted.", state_updates={}
        ),
        stagnation_str="",
        summary_failed=False,
        summary_payload=None,
        validation_repairs=[],
        baseline=_base(before),
    )
    assert result["agent_response"] == (
        "Noted."
        + SEP
        + "FALSE ALARM PROSE"
        + SEP
        + HEAD
        + "a root cause is identified."
    )
    assert OUTCOME_STATED_KEY not in metadata, "turn-local: popped"


def test_the_statement_site_reads_the_record_alone():
    """Pin 2: on a case that lets it read only the record, the site renders
    the statement from the snapshot and the record."""
    case, before = _invalidated_and_identified()
    poisoned = _Poisoned(case)
    now = _statement_record(poisoned, {OUTCOME_WRITTEN_KEY: True}, _base(before))
    assert now is case.progress.outcome
    assert _outcome_statement_for_turn(_base(before), now, frozenset()) == (
        HEAD + "the data shows the reported problem was not present; a root cause "
        "is identified."
    )


# ---------------------------------------------------------------------------
# Through process_turn
# ---------------------------------------------------------------------------

_TSU = InvestigationResponse_Treatment.TreatmentStateUpdate
_FIX_TEXT = "cap the orders pool at 50 connections"


def _response(*, text="Noted.", why=None, **updates):
    """The model's response: Treatment, so a fix can be carried out with it."""
    return InvestigationResponse_Treatment(
        agent_response=text,
        evidence_trail=EvidenceTrail(
            evidence_analyzed=[],
            milestone_justifications=MilestoneJustifications(**(why or {})),
        ),
        state_updates=_TSU(**updates),
    )


def _verifying(**top):
    return _response(
        why={"symptom_verified": "timeouts in the gateway log"},
        milestones=MilestoneUpdates(symptom_verified=True),
        evidence_to_add=[_row(_C.SYMPTOM_EVIDENCE, "s1")],
        **top,
    )


def _fix_carried_out():
    return _response(
        why={"solution_accepted": "the user reports applying it"},
        solutions_to_add=[
            SolutionToAdd(
                description=_FIX_TEXT,
                solution_type=SolutionType.CONFIG_CHANGE,
                estimated_impact="timeouts stop",
                risks="none",
            )
        ],
        milestones=MilestoneUpdates(solution_accepted=True),
    )


def _cause_work():
    """The revision turn's cause work, sent on a verified problem."""
    dsu = _revision_turn_with_cause_work()
    return _response(
        evidence_to_add=dsu.evidence_to_add,
        hypotheses_to_add=dsu.hypotheses_to_add,
        causal_nodes_to_add=dsu.causal_nodes_to_add,
        node_evidence_links=dsu.node_evidence_links,
    )


async def _process(engine, case, message, response, *, after_save=None, during=None):
    """One turn: the case saved with its record, then ``process_turn``;
    ``during`` runs on the case while the model answers."""
    record_case_outcome(case, at=WritePoint.COMMIT)
    if after_save is not None:
        after_save(case)
    case.current_turn += 1

    async def _answer(*args, **kwargs):
        if during is not None:
            during(case)
        return response

    engine.generator.generate_structured_output = AsyncMock(side_effect=_answer)
    result = await engine.process_turn(
        case=case, user_message=message, redaction_ctx=None
    )
    return result["agent_response"], result


async def test_a_verifying_turn_states_the_problem_confirmed():
    reply, _ = await _process(_revision_engine(), _case(_U), "the log", _verifying())
    assert reply == "Noted." + SEP + HEAD + "the problem is confirmed."


async def test_a_turn_that_changes_nothing_states_nothing():
    case = _case(_U)
    engine = _revision_engine()
    first, _ = await _process(engine, case, "the log", _verifying())
    assert first == "Noted." + SEP + HEAD + "the problem is confirmed."
    reply, _ = await _process(engine, case, "thanks", _response())
    assert reply == "Noted."


async def _staged_revision():
    engine, case = _revision_engine(), _case(_U)
    await _apply(engine, case, _revision_turn_with_cause_work())
    assert case.progress.problem_status == _P
    return engine, case


async def test_a_step_zero_replay_states_what_it_changed():
    engine, case = await _staged_revision()
    reply, _ = await _process(engine, case, "yes", _response())
    assert reply == (
        "Noted." + SEP + HEAD + "the problem is confirmed; a root cause is identified."
    )


async def test_a_replay_and_a_fix_in_its_cycle_state_each_fact_once():
    engine, case = await _staged_revision()
    reply, result = await _process(engine, case, "yes", _fix_carried_out())
    turn = result["case_updated"].current_turn
    assert reply == (
        "Noted." + SEP + HEAD + "the problem is confirmed; a root cause is "
        f"identified; a fix was carried out (turn {turn})."
    )
    assert reply.count(HEAD) == 1


async def test_a_decline_states_nothing_of_the_restoration():
    """The decline restores VERIFIED: no problem clause. Positive control: the
    same turn identifies a cause, which is stated."""
    engine, case = _revision_engine(), _case(_V)
    _propose(case)
    reply, result = await _process(engine, case, "no", _cause_work())
    assert result["case_updated"].progress.problem_status == _V
    assert reply == "Noted." + SEP + HEAD + "a root cause is identified."


def _retract_and_propose():
    return _response(
        why={"symptom_verified": "the user says the 503s came from a load test"},
        milestones=MilestoneUpdates(symptom_verified=False),
        evidence_to_add=[_row(_C.SYMPTOM_EVIDENCE, "s7")],
        verification_updates=ProblemVerificationUpdate(
            revised_problem_statement=(
                "API requests to /orders time out after 30s under real traffic"
            ),
            revision_evidence_ids=["new_index_0"],
            revision_basis="the gateway log shows timeouts outside the load test",
        ),
    )


def _accepted_mid_turn(case, **kw):
    """A fix accepted during the turn, outside its cycle: the positive control
    of a turn on which no closed-set fact but the problem can move."""
    _carried_out(case, case.current_turn, **kw)


async def test_the_residual_a_retract_and_propose_turn_says_no_unconfirmed():
    """Ruled residual: one response retracts the confirmed symptom and
    proposes a revision; the reply asks for the revision and says no "no
    longer confirmed" (nor, across the hold, that the rcc cause went).
    Positive control: a fix accepted during the turn is stated (cause work
    and fixes are held on a revision turn, so nothing else can move)."""
    await _residual_turn()


async def _residual_turn():
    engine, case = _revision_engine(), _rcc_case()
    turn = case.current_turn + 1
    reply, result = await _process(
        engine,
        case,
        "it was a load test",
        _retract_and_propose(),
        during=_accepted_mid_turn,
    )
    updated = result["case_updated"]
    assert updated.progress.problem_status == _P
    assert ps.pending_revision(updated).prior_status == _U, "retracted first"
    assert "no longer" not in reply
    assert reply.endswith(SEP + HEAD + f"a fix was carried out (turn {turn}).")
    return engine, updated


async def test_the_decline_tail_leaves_the_case_unverified_with_no_clause():
    """Accepted tail: the decline of that revision returns the case to
    UNVERIFIED, which states nothing. Positive control: a fix accepted during
    the turn is stated (cause work and a fix's licence are refused on an
    unverified problem, so nothing else can move)."""
    engine, case = await _residual_turn()
    turn = case.current_turn + 1
    reply, result = await _process(
        engine, case, "no", _response(), during=_accepted_mid_turn
    )
    updated = result["case_updated"]
    assert updated.progress.problem_status == _U
    assert reply == "Noted." + SEP + HEAD + f"a fix was carried out (turn {turn})."


async def test_a_baseline_out_of_step_with_the_live_status_states_no_problem():
    """The record at turn start says UNVERIFIED, the case VERIFIED (a write
    that failed at the last save): the verify-shaped move states nothing.
    Positive control: the cause the same turn identifies is stated."""
    engine, case = _revision_engine(), _case(_V)

    def _stale(c):
        c.progress.outcome.problem.status = _U

    reply, _ = await _process(
        engine, case, "the pool", _cause_work(), after_save=_stale
    )
    assert reply == "Noted." + SEP + HEAD + "a root cause is identified."


async def test_a_placeholder_keeps_its_flag_with_the_statement_under_it():
    placeholder = _synthesize_agent_response(_verifying(text=""), StopReason.MAX_TOKENS)
    assert is_agent_response_synthesized(placeholder)
    reply, result = await _process(_revision_engine(), _case(_U), "log", placeholder)
    assert (
        reply == placeholder.agent_response + SEP + HEAD + "the problem is confirmed."
    )
    assert result["metadata"].get(MESSAGE_METADATA_AGENT_SYNTHESIZED) is True
    assert result["case_updated"].turn_history[-1].agent_response_synthesized is True


def _cycle_write_fails(monkeypatch) -> None:
    real = turn_application.record_case_outcome

    def _write(case, *, at, **kw):
        if at == WritePoint.CYCLE:
            return False
        return real(case, at=at, **kw)

    monkeypatch.setattr(turn_application, "record_case_outcome", _write)


async def test_a_failed_cycle_write_states_from_the_narrations_fold(monkeypatch):
    _cycle_write_fails(monkeypatch)
    reply, result = await _process(_revision_engine(), _case(_U), "log", _verifying())
    assert result["case_updated"].progress.outcome.problem.status == _U, "unwritten"
    assert reply == "Noted." + SEP + HEAD + "the problem is confirmed."


async def test_a_second_failure_states_nothing_and_is_counted(monkeypatch):
    """Positive control: the same turn with the fold succeeding states the
    clause (the test above)."""
    _cycle_write_fails(monkeypatch)

    def _raise(case):
        raise RuntimeError("fold failed")

    monkeypatch.setattr(turn_completion, "narration_outcome", _raise)
    counter = MagicMock()
    monkeypatch.setattr(turn_completion, "case_outcome_write_failures_total", counter)
    reply, _ = await _process(_revision_engine(), _case(_U), "log", _verifying())
    assert reply == "Noted."
    counter.labels.assert_called_once_with(at=WritePoint.NARRATION)
    counter.labels.return_value.inc.assert_called_once_with()


async def test_a_resolve_confirmation_states_neither_fact_it_confirms(monkeypatch):
    """The confirmation names the cause the stale snapshot lacks and the
    confirming fix, carried out this turn: no Case record line repeats them.
    Positive control: those two facts changed this turn."""
    from tests.unit.core.investigation.test_resolution_backstop_turn import (
        _confirmed_case,
    )

    case = _confirmed_case()
    case.offer_record = remember_resolve_request(case, ["solution"])
    conclusion = case.root_cause_conclusion

    def _stale(c):
        # The snapshot was folded before the conclusion.
        c.root_cause_conclusion = None
        record_case_outcome(c, at=WritePoint.COMMIT)
        c.root_cause_conclusion = conclusion

    seen = []
    real = turn_completion._outcome_statement_for_turn

    def _spy(baseline, now, stated):
        seen.append(_kinds(outcome_changes(baseline, now)))
        return real(baseline, now, stated)

    monkeypatch.setattr(turn_completion, "_outcome_statement_for_turn", _spy)
    reply, _ = await _process(
        _revision_engine(),
        case,
        "done",
        _response(),
        after_save=_stale,
        during=_accepted_mid_turn,
    )
    assert f"- **Root cause**: {conclusion.root_cause}" in reply
    assert "- **Solution**: restart the orders service (carried out turn 1)" in reply
    assert HEAD not in reply
    assert seen == [[OutcomeChange.CAUSE_IDENTIFIED, OutcomeChange.FIX_CARRIED_OUT]]


# ---------------------------------------------------------------------------
# The deterministic exits compose no statement: they change no closed-set fact
# ---------------------------------------------------------------------------


async def _exit_changes(engine, case, message, **kw) -> tuple:
    """The closed-set changes a deterministic exit leaves, read as the save
    folds the record."""
    from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
        _turn,
    )

    record_case_outcome(case, at=WritePoint.COMMIT)
    baseline = outcome_baseline(case)
    result = await _turn(engine, case, message, **kw)
    saved = compute_case_outcome(case.progress.outcome, case, at=WritePoint.COMMIT)
    # Control: the same turn with a fix carried out states it.
    control = copy.deepcopy(case)
    _carried_out(control, case.current_turn)
    moved = compute_case_outcome(
        control.progress.outcome, control, at=WritePoint.COMMIT
    )
    assert _kinds(outcome_changes(baseline, moved)) == [OutcomeChange.FIX_CARRIED_OUT]
    return outcome_changes(baseline, saved), result


async def test_the_confirmed_resolve_exit_changes_no_closed_set_fact():
    from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
        _engine,
        _turn,
    )
    from tests.unit.core.investigation.test_resolution_backstop_turn import (
        _confirmed_case,
    )

    case, engine = _confirmed_case(), _engine()
    await _turn(engine, case, "errors gone")
    assert case.offer_record.open.kind == "resolve", "premise: offered"
    changes, result = await _exit_changes(engine, case, "yes")
    assert case.state == CaseState.RESOLVED, "premise: the :894 exit ran"
    assert changes == []
    assert HEAD not in result["agent_response"]


@pytest.mark.parametrize("hold", [False, True], ids=["open", "revision-hold"])
async def test_the_menu_close_exit_changes_no_closed_set_fact(hold):
    """The menu close; under a revision hold it cancels the revision, which
    restores the status (the out-of-turn residual, stated nothing)."""
    from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
        _engine,
    )

    case, engine = _case(_V), _engine()
    if hold:
        _propose(case)
    changes, result = await _exit_changes(
        engine,
        case,
        "close it",
        intent_type="status_transition",
        intent_data={"to_state": "closed"},
    )
    assert case.offer_record.open.kind == "close", "premise: the exit ran"
    assert ps.pending_revision(case) is None and case.progress.problem_status == _V
    assert changes == []
    assert HEAD not in result["agent_response"]


async def test_the_statement_sits_below_the_inv40_notice():
    from faultmaven.core.investigation.milestone_engine.response_synthesis import (
        _NARRATION_OVERCLAIM_NOTICE,
    )

    reply, _ = await _process(
        _revision_engine(), _case(_U), "log", _verifying(text="Case resolved.")
    )
    assert reply == (
        "Case resolved."
        + SEP
        + _NARRATION_OVERCLAIM_NOTICE
        + SEP
        + HEAD
        + "the problem is confirmed."
    )
