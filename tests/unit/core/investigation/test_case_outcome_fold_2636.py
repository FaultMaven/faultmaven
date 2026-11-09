"""The case outcome record, folded and written (#2636, #2615 stage S1, PR A).

``compute_case_outcome(prior, case)`` reads the case as it stands and folds the
result over the prior record. A verdict the rules call final is recorded once,
with its rows and the problem statement it was read under, and carried; a fresh
derivation that disagrees is a conflict, logged and counted, and the carried
verdict stands. The record is written at the end of each decision cycle, after
each bundle of a statement-revision replay and at both terminal transitions.
Nothing reads it in this stage, and no text changes.

The first three tests are the regressions for the issues PR A refers to: a
FAILED window re-read after the cause pool moves (#2595), a failure read
through a citation that a re-root erases (#2582), and the same across the
bundles of a replay. Each one carries the fresh derivation's reversal as a
premise, so it fails without the fold. No live LLM.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from faultmaven.core.investigation import outcome as outcome_module
from faultmaven.core.investigation.causal_graph.pruning import attach_root
from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_SUCCEEDED,
    fix_windows,
)
from faultmaven.core.investigation.milestone_engine.statement_revision import (
    confirm_revision,
)
from faultmaven.core.investigation.offers import answer_offer
from faultmaven.core.investigation.outcome import (
    closure_outcome,
    compute_case_outcome,
    decision_cycle,
    record_case_outcome,
)
from faultmaven.core.investigation.problem_status import (
    propose_revision,
    stage_cause_work,
)
from faultmaven.core.investigation.terminal_transitions import (
    derive_closure_reason,
    disposition_refusal,
    execute_user_closure,
    open_disposition_offer,
)
from faultmaven.modules.case.contracts import (
    AnswerKind,
    AnswerVia,
    CaseState,
    CausalEdge,
    CausalNode,
    ClosureEligibility,
    ClosureReason,
    ConfirmationGap,
    EvidenceCategory,
    EvidenceStance,
    FixWindowState,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InvestigationActionType,
    MitigationRecord,
    MitigationRefusalReason,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    OfferBasis,
    OfferKind,
    PoolEmptyReason,
    ProblemStatus,
    ProposedAction,
    ResolutionReason,
    ResolutionVerdict,
    ValidationMethod,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation.bound_rows import VERIFYING_ID, _row
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _engine as _turn_engine,
)
from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
    _offered_then_declined,
    _turn,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _HYP_ID,
    _ROOT_ID,
    _accept,
    _eid,
    _identified_case,
    _recompute_at,
)
from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
    _held_case,
)
from tests.unit.core.investigation.test_resolution_backstop_turn import (
    _confirmed_case,
)
from tests.utils import terminal_verdict

pytestmark = pytest.mark.unit

_C = EvidenceCategory
_DEEPER_ID = "cn_0000000a2a2a"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _fix(case, turn=6, **fields) -> ProposedAction:
    """A SOLUTION the user ran at ``turn``; unkinded unless ``fields`` kind it."""
    action = ProposedAction(
        case_id=case.case_id,
        action_type=InvestigationActionType.SOLUTION,
        description="raise max_connections to 100",
        proposed_in_turn=turn - 1,
        accepted_in_turn=turn,
        state="accepted",
        **fields,
    )
    case.proposed_actions.append(action)
    case.progress.solution_accepted = True
    return action


def _relapse(case, rechecks, turns=(7, 8, 9)) -> None:
    """The cause re-checked gone, the problem gone, then back."""
    gone, relief, back = turns
    _row(
        case,
        _C.CAUSAL_ABSENCE_EVIDENCE,
        turn=gone,
        evidence_id=_eid("gone"),
        summary="max_connections now reads 100",
        rechecks=rechecks,
    )
    _row(
        case,
        _C.SYMPTOM_ABSENCE_EVIDENCE,
        turn=relief,
        evidence_id=_eid("relief"),
        summary="checkout answers 200",
        rechecks=VERIFYING_ID,
    )
    _row(
        case,
        _C.SYMPTOM_EVIDENCE,
        turn=back,
        evidence_id=_eid("back"),
        summary="checkout returns 503 again",
        rechecks=VERIFYING_ID,
    )


def _deepen(case, hyp, root, turn=10) -> CausalNode:
    """Re-root ``hyp`` past ``root`` onto a deeper cause, as the attach writer
    does: the old root's verdict is reversed and its citation erased."""
    datum = _row(
        case,
        _C.CAUSAL_EVIDENCE,
        turn=turn,
        evidence_id=_eid("deeper"),
        summary="pgbouncer pool_size 20 overrides max_connections",
    )
    deeper = CausalNode(
        metadata=terminal_verdict(datum.evidence_id),
        node_id=_DEEPER_ID,
        statement="pgbouncer caps the pool at 20 connections",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        evidence_links=[
            NodeEvidenceLink(
                evidence_id=datum.evidence_id,
                stance=EvidenceStance.SUPPORTS,
                reasoning="it",
                linked_at_turn=turn,
            )
        ],
        generated_at_turn=turn,
    )
    case.causal_nodes[deeper.node_id] = deeper
    case.causal_edges.append(
        CausalEdge(cause_node_id=deeper.node_id, effect_node_id=root.node_id)
    )
    path = [deeper.node_id, *(hyp.path or [root.node_id])]
    attach_root(case, hyp, deeper.node_id, path, turn)
    _recompute_at(case, turn)
    return deeper


def _cited_but_unlinked(case, root):
    """A row the root's verdict cites with no SUPPORTS link: a re-check binds
    to the root through the citation alone (#2557)."""
    cited = _row(
        case,
        _C.CAUSAL_EVIDENCE,
        turn=4,
        evidence_id=_eid("cited"),
        summary="postgresql.conf sets max_connections = 20",
    )
    root.metadata.update(
        terminal_verdict(_eid("pool_log"), _eid("pool_metric"), cited.evidence_id)
    )
    return cited


def _window(record, action_id):
    (attempt,) = [a for a in record.attempts if a.action_id == action_id]
    return attempt.window


@pytest.fixture
def conflicts(monkeypatch):
    """The conflict counter's increments, by field."""
    counted = []
    counter = MagicMock()
    counter.labels.side_effect = lambda field: SimpleNamespace(
        inc=lambda: counted.append(field)
    )
    monkeypatch.setattr(outcome_module, "case_outcome_conflicts_total", counter)
    return counted


# ---------------------------------------------------------------------------
# The regressions (#2595, #2582, the replay)
# ---------------------------------------------------------------------------


def test_2595_an_unkinded_window_read_failed_stays_failed_after_the_pool_rebinds(
    conflicts,
):
    case, root, hyp = _identified_case()
    fix = _fix(case)
    _relapse(case, rechecks=_eid("pool_log"))
    _recompute_at(case, 9)
    first = compute_case_outcome(None, case)
    assert _window(first, fix.action_id).state == FixWindowState.FAILED

    _deepen(case, hyp, root)
    assert fix_windows(case)[-1].state != FIX_FAILED, "premise: re-read at the pool"
    second = compute_case_outcome(first, case)

    window = _window(second, fix.action_id)
    assert window.state == FixWindowState.FAILED
    assert window.cause_row_ids == [_eid("gone")]
    assert window.failure_turn == 9
    assert window.read_under == "checkout returns 503"
    assert conflicts == ["attempts.window"]


@pytest.mark.parametrize("kinded", [False, True], ids=["window-0", "kinded-fix"])
def test_2582_a_failure_read_through_a_cited_row_survives_a_re_root(kinded, conflicts):
    case, root, hyp = _identified_case()
    cited = _cited_but_unlinked(case, root)
    _recompute_at(case, 5)
    action_id = None
    if kinded:
        action_id = _fix(
            case, target_ids=[root.node_id], fix_kind="remediation"
        ).action_id
    _relapse(case, rechecks=cited.evidence_id)
    _recompute_at(case, 9)
    first = compute_case_outcome(None, case)

    _deepen(case, hyp, root)
    assert root.metadata["root_terminal_evidence_ids"] == [], "premise: erased"
    assert all(w.state != FIX_FAILED for w in fix_windows(case)), "premise: re-read"
    second = compute_case_outcome(first, case)

    window = _window(second, action_id)
    assert window.state == FixWindowState.FAILED
    assert window.cited_basis == {_ROOT_ID: [cited.evidence_id]}
    assert window.read_under == "checkout returns 503"
    assert second == compute_case_outcome(second, case), "carried, not re-read"
    assert "attempts.window" in conflicts


async def test_the_replay_keeps_a_failure_one_bundle_read_after_the_next_re_roots():
    """Bundle 1 cites the re-checked row (window 0 reads the relapse FAILED),
    bundle 2 re-roots past it. Folding once after the last bundle would
    never see the failure; the fold after each bundle carries it."""
    case, root, hyp = _identified_case()
    cited = _row(
        case,
        _C.CAUSAL_EVIDENCE,
        turn=4,
        evidence_id=_eid("cited"),
        summary="postgresql.conf sets max_connections = 20",
    )
    _relapse(case, rechecks=cited.evidence_id)
    _recompute_at(case, 9)
    record_case_outcome(case, at="cycle")
    assert not case.progress.outcome.attempts, "premise: nothing read before"

    case.current_turn = 10
    propose_revision(
        case,
        text="checkout returns 503 under load",
        evidence_ids=[VERIFYING_ID],
        basis="only under load",
        wording_key="k",
    )
    stage_cause_work(case, updates={}, evidence_added=[])
    case.current_turn = 11
    stage_cause_work(case, updates={}, evidence_added=[])
    case.current_turn = 12

    async def _apply(case, _updates, metadata):
        if metadata["replay_emission_turn"] == 10:
            _cited_but_unlinked(case, root)
        else:
            _deepen(case, hyp, root, turn=12)

    responses = SimpleNamespace(
        kb_prefetcher=SimpleNamespace(prefetch_kb_context=AsyncMock()),
        _apply_investigation_updates=_apply,
    )
    await confirm_revision(responses, case, {}, via=AnswerVia.CLICK)

    assert all(w.state != FIX_FAILED for w in fix_windows(case)), "premise"
    window = _window(case.progress.outcome, None)
    assert window.state == FixWindowState.FAILED
    assert window.read_under == "checkout returns 503 under load"


# ---------------------------------------------------------------------------
# The fold
# ---------------------------------------------------------------------------


def test_an_accepted_in_turn_is_carried_and_a_re_dating_counted(conflicts):
    case, _root, _hyp = _identified_case()
    fix = _fix(case)
    first = compute_case_outcome(None, case)
    fix.accepted_in_turn = 5
    second = compute_case_outcome(first, case)
    (attempt,) = second.attempts
    assert attempt.accepted_in_turn == 6
    assert conflicts == ["attempts.accepted_in_turn"]


def test_window_zero_is_one_entry_carried_across_cycles(conflicts):
    case, root, hyp = _identified_case()
    cited = _cited_but_unlinked(case, root)
    _relapse(case, rechecks=cited.evidence_id)
    record = None
    for turn in (9, 10, 11):
        _recompute_at(case, turn)
        record = compute_case_outcome(record, case)
    _deepen(case, hyp, root, turn=12)
    for _ in range(2):
        record = compute_case_outcome(record, case)
    zero = [a for a in record.attempts if a.action_id is None]
    assert len(zero) == 1
    assert zero[0].window.state == FixWindowState.FAILED
    assert zero[0].kind is None and zero[0].accepted_in_turn is None


def test_window_zero_is_recorded_only_once_observed():
    case, _root, _hyp = _identified_case()
    assert compute_case_outcome(None, case).attempts == []


def test_the_resolved_rows_are_final(conflicts):
    case = _held_case(asked=False)
    case.atomic_update(
        state=CaseState.RESOLVED, resolved_at=case.updated_at, closed_at=case.updated_at
    )
    first = compute_case_outcome(None, case)
    assert first.resolution.confirmed_row_ids, "premise: confirmed"
    case.evidence = [
        e for e in case.evidence if e.category != _C.SYMPTOM_ABSENCE_EVIDENCE
    ]
    second = compute_case_outcome(first, case)
    assert second.resolution.confirmed_row_ids == first.resolution.confirmed_row_ids
    assert conflicts == ["resolution.confirmed_row_ids"]


def test_a_terminal_closure_is_carried(conflicts):
    case, _root, _hyp = _identified_case()
    case.atomic_update(
        state=CaseState.CLOSED,
        closed_at=case.updated_at,
        closure_reason=ClosureReason.SOLUTION_DEFERRED,
    )
    first = compute_case_outcome(None, case)
    assert first.closure.reason == ClosureReason.SOLUTION_DEFERRED
    case.closure_reason = ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE
    assert compute_case_outcome(first, case).closure == first.closure
    assert conflicts == ["closure"]


# ---------------------------------------------------------------------------
# Each field, against a hand-built case
# ---------------------------------------------------------------------------


def test_each_section_of_an_identified_case():
    case, _root, _hyp = _identified_case()
    record = compute_case_outcome(None, case)

    assert record.problem.status == ProblemStatus.VERIFIED
    assert record.problem.verifying_row_ids == [VERIFYING_ID]
    assert record.problem.statement == "checkout returns 503"
    cause = record.cause
    assert cause.identified_root_ids == [_ROOT_ID]
    assert cause.confirmation_root_ids == [_ROOT_ID]
    assert cause.held_root_ids == [] and cause.held_open_root_ids == []
    assert cause.fallen_root_ids == [_ROOT_ID], "recorded as identified"
    assert cause.cited_basis == {
        _ROOT_ID: sorted([_eid("pool_log"), _eid("pool_metric")])
    }
    assert cause.pool_empty_reason is None
    assert record.attempts == [] and record.demotions == []
    resolution = record.resolution
    assert resolution.verdict == ResolutionVerdict.NEEDS_INFO
    assert resolution.reason == ResolutionReason.DOCUMENTATION_GAPS
    assert resolution.missing == [
        "confirmation the problem is now resolved",
        "solution",
    ]
    assert resolution.confirmation_gaps == []
    assert resolution.cause_row_ids == resolution.problem_row_ids == []
    assert resolution.pending_check_need_id is None
    assert resolution.declined_stands is False
    assert resolution.confirmed_row_ids == [] and resolution.statement == ""
    assert record.unbound_rechecks == [] and record.mitigation is None
    assert record.milestones_shown == ["symptom_verified", "root_cause_identified"]
    assert record.closure.eligibility == ClosureEligibility.HAS_SUBSTANCE
    assert record.closure.reason == derive_closure_reason(case)
    assert record.closure.statement is None


def test_a_failed_fix_records_its_disconfirmation_as_rows():
    case, _root, _hyp = _identified_case()
    fix = _fix(case, turn=8)
    _row(
        case,
        _C.CAUSAL_ABSENCE_EVIDENCE,
        turn=9,
        evidence_id=_eid("removed"),
        summary="max_connections now 100",
        rechecks=_eid("pool_log"),
    )
    _row(
        case,
        _C.SYMPTOM_EVIDENCE,
        turn=9,
        evidence_id=_eid("still"),
        summary="checkout still 503",
        rechecks=VERIFYING_ID,
    )
    window = _window(compute_case_outcome(None, case), fix.action_id)
    assert window.state == FixWindowState.FAILED
    assert (window.fix_turn, window.end_turn) == (8, None)
    pair = window.disconfirmation
    assert (pair.cause_row_id, pair.problem_row_id) == (_eid("removed"), _eid("still"))
    assert (pair.cause_turn, pair.problem_turn) == (9, 9)


def test_a_confirmed_case_records_both_legs_and_its_statement():
    case = _held_case(asked=False)
    record = compute_case_outcome(None, case)
    resolution = record.resolution
    assert resolution.reason == ResolutionReason.CONFIRMED
    assert resolution.verdict == ResolutionVerdict.READY
    assert resolution.cause_row_ids and resolution.problem_row_ids
    assert resolution.statement.startswith("- **The cause**")
    assert resolution.confirmed_row_ids == [], "only at RESOLVED"
    (attempt,) = record.attempts
    assert attempt.window.state == FixWindowState.SUCCEEDED == FIX_SUCCEEDED
    assert record.closure.eligibility == ClosureEligibility.SUGGEST_RESOLVE


def test_an_unkinded_window_with_nothing_seen_reads_unknown_and_an_interception_none():
    case, _root, _hyp = _identified_case()
    fix = _fix(case)
    relief = _fix(case, turn=7, fix_kind="interception", target_ids=[_ROOT_ID])
    record = compute_case_outcome(None, case)
    assert _window(record, fix.action_id).state == FixWindowState.UNKNOWN
    (attempt,) = [a for a in record.attempts if a.action_id == relief.action_id]
    assert attempt.window is None and attempt.kind == "interception"
    assert attempt.target_ids == [_ROOT_ID]


def test_the_unbound_rechecks_list_every_turn_and_carry_the_narrated_turn():
    """#2555: a re-check recorded during a hold, or on an earlier turn, is on
    the record; the same-turn notice reads only this turn's rows."""
    case, _root, _hyp = _identified_case()
    _fix(case)
    _row(
        case,
        _C.SYMPTOM_ABSENCE_EVIDENCE,
        turn=7,
        evidence_id=_eid("unnamed"),
        summary="checkout answers 200",
        rechecks=None,
    )
    case.current_turn = 9
    case.progress.problem_status = ProblemStatus.REVISION_PENDING
    first = compute_case_outcome(None, case)
    (unbound,) = first.unbound_rechecks
    assert (unbound.evidence_id, unbound.observed_turn) == (_eid("unnamed"), 7)
    assert unbound.uncited is False and unbound.after_mitigation is False
    assert unbound.problem_row_ids == [VERIFYING_ID]
    assert unbound.narrated_turn is None
    narrated = first.model_copy(
        update={"unbound_rechecks": [unbound.model_copy(update={"narrated_turn": 8})]}
    )
    assert compute_case_outcome(narrated, case).unbound_rechecks[0].narrated_turn == 8


def test_the_mitigation_section_reads_its_observation():
    case, _root, _hyp = _identified_case()
    case.progress.mitigation = MitigationRecord(accepted=True)
    _accept(case, InvestigationActionType.MITIGATION, 6)
    _row(
        case,
        _C.SYMPTOM_ABSENCE_EVIDENCE,
        turn=7,
        evidence_id=_eid("relieved"),
        summary="checkout answers 200 after the failover",
        rechecks=VERIFYING_ID,
    )
    mitigation = compute_case_outcome(None, case).mitigation
    assert (mitigation.attempt_turn, mitigation.relief_turn) == (6, 7)
    assert mitigation.relief_row_id == _eid("relieved")
    assert mitigation.holding is True and mitigation.present_turn is None
    assert mitigation.verified is False, "nothing stored it verified"
    assert mitigation.settled is False and mitigation.refusal_reason is None


# ---------------------------------------------------------------------------
# Each enum, one test per branch
# ---------------------------------------------------------------------------


def _bare_case():
    return investigating_case(problem_status=ProblemStatus.VERIFIED, current_turn=4)


def _fallen_case():
    """The identified root recorded as the cause, then fallen: no longer
    validated, with no other cause standing."""
    case, root, _hyp = _identified_case()
    root.metadata = {}
    return case, root


def _other_hypothesis(case):
    node = CausalNode(
        node_id="cn_00000000beef",
        statement="a DNS flap drops pool " "connections",
        node_type=NodeType.ROOT,
        generated_at_turn=3,
    )
    case.causal_nodes[node.node_id] = node
    case.hypotheses["hyp_00000000beef"] = Hypothesis(
        hypothesis_id="hyp_00000000beef",
        statement="DNS flaps",
        category=HypothesisCategory.NETWORK,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="r",
        root_node_id=node.node_id,
        generated_at_turn=3,
    )


def _held_open(case):
    case.progress.cause_identification_held_open = True


def _contested(case):
    case.progress.cause_identification_contested = True


def _rootless(case):
    case.hypotheses["hyp_000000000a0a"] = Hypothesis(
        hypothesis_id="hyp_000000000a0a",
        statement="a rootless cause",
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.VALIDATED,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="r",
        generated_at_turn=3,
    )


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (lambda: _identified_case()[0], None),
        (
            lambda: (c := _bare_case(), _held_open(c), _contested(c))[0],
            PoolEmptyReason.HELD_OPEN,
        ),
        (
            lambda: (c := _bare_case(), _contested(c), _rootless(c))[0],
            PoolEmptyReason.IDENTIFICATION_CONTESTED,
        ),
        (
            lambda: (c := _fallen_case()[0], _other_hypothesis(c), _rootless(c))[0],
            PoolEmptyReason.FALLEN_WITH_OTHER_STANDING,
        ),
        (
            lambda: (
                c := _fallen_case()[0],
                _fix(c, fix_kind="remediation", target_ids=["cn_00000000beef"]),
                _rootless(c),
            )[0],
            PoolEmptyReason.FIX_TARGETS_OTHER_CAUSE,
        ),
        (
            lambda: (c := _bare_case(), _rootless(c))[0],
            PoolEmptyReason.ROOTLESS_VALIDATED_ONLY,
        ),
        (_bare_case, PoolEmptyReason.NO_CAUSE),
    ],
    ids=lambda v: getattr(v, "value", None) if not callable(v) else "",
)
def test_each_way_the_confirmation_pool_comes_back_empty(build, reason):
    cause = compute_case_outcome(None, build()).cause
    assert cause.pool_empty_reason == reason
    assert bool(cause.confirmation_root_ids) == (reason is None)


def test_the_fallen_leg_stands_when_nothing_refuses_it():
    case, root = _fallen_case()
    assert compute_case_outcome(None, case).cause.confirmation_root_ids == [_ROOT_ID]


def _invalidated():
    case = _bare_case()
    case.progress.problem_status = ProblemStatus.INVALIDATED
    return case


def _interception_only():
    case, _root, _hyp = _identified_case()
    _fix(case, fix_kind="interception", target_ids=[_ROOT_ID])
    return case


def _failed_fix():
    case, _root, _hyp = _identified_case()
    _fix(case, turn=8)
    _row(
        case,
        _C.CAUSAL_ABSENCE_EVIDENCE,
        turn=9,
        evidence_id=_eid("removed"),
        summary="max_connections now 100",
        rechecks=_eid("pool_log"),
    )
    _row(
        case,
        _C.SYMPTOM_EVIDENCE,
        turn=9,
        evidence_id=_eid("still"),
        summary="checkout still 503",
        rechecks=VERIFYING_ID,
    )
    return case


@pytest.mark.parametrize(
    ("build", "reason", "verdict"),
    [
        (_invalidated, ResolutionReason.FALSE_ALARM, ResolutionVerdict.SUGGEST_CLOSE),
        (_held_case, ResolutionReason.PENDING_CHECK, ResolutionVerdict.NEEDS_INFO),
        (
            lambda: _held_case(asked=False),
            ResolutionReason.CONFIRMED,
            ResolutionVerdict.READY,
        ),
        (
            _bare_case,
            ResolutionReason.NOTHING_ON_RECORD,
            ResolutionVerdict.SUGGEST_CLOSE,
        ),
        (
            _interception_only,
            ResolutionReason.FIX_OFF_CAUSE,
            ResolutionVerdict.NEEDS_INFO,
        ),
        (_failed_fix, ResolutionReason.FIX_FAILED, ResolutionVerdict.NEEDS_INFO),
        (
            lambda: _identified_case()[0],
            ResolutionReason.DOCUMENTATION_GAPS,
            ResolutionVerdict.NEEDS_INFO,
        ),
    ],
)
def test_each_branch_of_resolution_readiness_has_its_reason(build, reason, verdict):
    case = build()
    resolution = compute_case_outcome(None, case).resolution
    assert (resolution.reason, resolution.verdict) == (reason, verdict)
    if reason == ResolutionReason.PENDING_CHECK:
        assert resolution.pending_check_need_id == case.evidence_needs[0].need_id


def _relief_only():
    case, _root, _hyp = _identified_case()
    _fix(case)
    _row(
        case,
        _C.SYMPTOM_ABSENCE_EVIDENCE,
        turn=7,
        evidence_id=_eid("relief"),
        summary="checkout answers 200",
        rechecks=VERIFYING_ID,
    )
    return case


def _cause_leg_only():
    case, _root, _hyp = _identified_case()
    _fix(case)
    _row(
        case,
        _C.CAUSAL_ABSENCE_EVIDENCE,
        turn=7,
        evidence_id=_eid("gone"),
        summary="max_connections now reads 100",
        rechecks=_eid("pool_log"),
    )
    return case


def _no_symptom_row(case):
    """Nothing on record showed the problem (#1906, ruled 2026-11-03)."""
    case.evidence = [e for e in case.evidence if e.category != _C.SYMPTOM_EVIDENCE]
    case.problem_verification.verifying_evidence_ids = []
    return case


@pytest.mark.parametrize(
    ("build", "gaps"),
    [
        (_relief_only, [ConfirmationGap.RELIEF_WITHOUT_CAUSE_RECHECK]),
        (_cause_leg_only, [ConfirmationGap.PROBLEM_UNRECHECKED]),
        (
            lambda: _no_symptom_row(_identified_case()[0]),
            [ConfirmationGap.NO_SYMPTOM_ROW],
        ),
        # The pair the code asks together: the cause leg stands through its
        # citation, and nothing showed the problem.
        (
            lambda: _no_symptom_row(_cause_leg_only()),
            [ConfirmationGap.PROBLEM_UNRECHECKED, ConfirmationGap.NO_SYMPTOM_ROW],
        ),
        (lambda: _identified_case()[0], []),
    ],
    ids=["relief", "cause-leg", "no-symptom-row", "pair", "none"],
)
def test_the_confirmation_gaps_are_the_asks_the_branch_makes(build, gaps):
    resolution = compute_case_outcome(None, build()).resolution
    assert resolution.reason == ResolutionReason.DOCUMENTATION_GAPS
    assert resolution.confirmation_gaps == gaps


def _mitigated(status=ProblemStatus.VERIFIED, *, dated=True, relieved=False):
    case, _root, _hyp = _identified_case()
    case.progress.mitigation = MitigationRecord(accepted=True)
    if dated:
        _accept(case, InvestigationActionType.MITIGATION, 6)
    if relieved:
        _row(
            case,
            _C.SYMPTOM_ABSENCE_EVIDENCE,
            turn=7,
            evidence_id=_eid("relieved"),
            summary="checkout answers 200 after the failover",
            rechecks=VERIFYING_ID,
        )
    case.progress.problem_status = status
    return case


@pytest.mark.parametrize(
    ("build", "reason"),
    [
        (lambda: _mitigated(dated=False), MitigationRefusalReason.ATTEMPT_UNDATED),
        (
            lambda: _mitigated(ProblemStatus.REVISION_PENDING),
            MitigationRefusalReason.REVISION_PENDING,
        ),
        (
            lambda: _mitigated(ProblemStatus.UNVERIFIED),
            MitigationRefusalReason.PROBLEM_UNVERIFIED,
        ),
        (_mitigated, MitigationRefusalReason.NO_BOUND_RELIEF),
        (lambda: _mitigated(relieved=True), None),
    ],
)
def test_each_branch_of_the_mitigation_refusal_has_its_reason(build, reason):
    assert compute_case_outcome(None, build()).mitigation.refusal_reason == reason


def test_a_stored_verdict_carries_no_refusal_reason():
    case = _mitigated(relieved=True)
    case.progress.mitigation.verified = True
    mitigation = compute_case_outcome(None, case).mitigation
    assert mitigation.verified and mitigation.settled
    assert mitigation.refusal_reason is None


@pytest.mark.parametrize(
    ("build", "eligibility"),
    [
        (lambda: investigating_case(current_turn=1), ClosureEligibility.TRIVIAL),
        (lambda: _identified_case()[0], ClosureEligibility.HAS_SUBSTANCE),
        (_confirmed_case, ClosureEligibility.SUGGEST_RESOLVE),
    ],
)
def test_each_closure_eligibility(build, eligibility):
    assert compute_case_outcome(None, build()).closure.eligibility == eligibility


# ---------------------------------------------------------------------------
# Closure on demand: the four entries, the memo and the cycle
# ---------------------------------------------------------------------------


@pytest.fixture
def entry_closures(monkeypatch):
    """Every closure ``closure_outcome`` returned."""
    returned = []
    real = outcome_module.closure_outcome

    def _spy(case):
        returned.append(real(case))
        return returned[-1]

    monkeypatch.setattr(outcome_module, "closure_outcome", _spy)
    return returned


def test_open_disposition_offer_reaches_closure_outcome(entry_closures):
    case, _root, _hyp = _identified_case()
    record = open_disposition_offer(case, kind=OfferKind.CLOSE, basis=OfferBasis.MENU)
    (closure,) = entry_closures
    assert record.open.closure_reason == closure.reason == derive_closure_reason(case)


def test_disposition_refusal_reaches_closure_outcome(entry_closures):
    case, _root, _hyp = _identified_case()
    disposition_refusal(case, kind=OfferKind.CLOSE, basis=OfferBasis.MENU)
    assert len(entry_closures) == 1


async def test_the_rest_close_persists_its_entry_closure_with_the_transition(
    entry_closures,
):
    """A decline, then a REST close: the reason a re-derivation on the CLOSED
    case could not give, persisted as its entry computed it."""
    _e, case, _result = await _offered_then_declined()
    reason = execute_user_closure(case, case.user_id)
    (closure,) = entry_closures
    assert reason == closure.reason == ClosureReason.RESOLUTION_DECLINED
    assert case.state == CaseState.CLOSED
    assert case.progress.outcome.closure is closure


async def test_an_accepted_close_on_a_resolvable_case_persists_its_closure(
    entry_closures,
):
    """``confirm_open_offer``'s re-derivation under a standing decline: the
    close closes, with the closure its entry computed."""
    engine, case, _result = await _offered_then_declined()
    case.offer_record = open_disposition_offer(
        case, kind=OfferKind.CLOSE, basis=OfferBasis.MENU
    )
    entry_closures.clear()
    await _turn(
        engine,
        case,
        "yes",
        intent_type="confirmation",
        intent_data={"value": True, "proposal_id": case.offer_record.open.proposal_id},
    )
    assert case.state == CaseState.CLOSED
    closure = entry_closures[-1]
    assert closure.reason == ClosureReason.RESOLUTION_DECLINED
    assert case.progress.outcome.closure is closure


def test_inside_a_cycle_the_closure_is_computed_fresh(monkeypatch):
    monkeypatch.setattr(
        outcome_module,
        "_ENTRY_CLOSURE",
        outcome_module.ContextVar("probe", default=None),
    )
    case, _root, _hyp = _identified_case()
    with decision_cycle():
        record = open_disposition_offer(
            case, kind=OfferKind.CLOSE, basis=OfferBasis.MENU
        )
        assert outcome_module._ENTRY_CLOSURE.get() is None, "nothing memoized"
        case.progress.problem_status = ProblemStatus.INVALIDATED
        record_case_outcome(case, at="cycle")
    assert record.open.closure_reason != ClosureReason.CLOSED_FALSE_ALARM, "premise"
    assert case.progress.outcome.closure.reason == ClosureReason.CLOSED_FALSE_ALARM

    closure = closure_outcome(case)
    assert outcome_module._ENTRY_CLOSURE.get() == (case, closure), "memoized outside"


def test_two_close_offers_on_one_case_each_read_the_case_as_it_stands():
    """Outside a cycle the memo feeds only the record: each entry still
    decides on the case as it stands, as on main."""
    case, _root, _hyp = _identified_case()
    first = open_disposition_offer(case, kind=OfferKind.CLOSE, basis=OfferBasis.MENU)
    case.offer_record = first
    case.offer_record = answer_offer(
        case,
        proposal_id=first.open.proposal_id,
        answer=AnswerKind.LAPSED,
        via=AnswerVia.MENU,
    )
    case.progress.problem_status = ProblemStatus.INVALIDATED
    second = open_disposition_offer(case, kind=OfferKind.CLOSE, basis=OfferBasis.MENU)
    assert first.open.closure_reason == ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE
    assert second.open.closure_reason == ClosureReason.CLOSED_FALSE_ALARM


# ---------------------------------------------------------------------------
# The write points
# ---------------------------------------------------------------------------


async def test_a_turn_writes_the_record_last_in_its_cycle_inside_the_cycle(
    monkeypatch,
):
    from faultmaven.core.investigation.milestone_engine import turn_application

    order = []
    for name in ("_sweep_needs_for_terminal_hypotheses", "sweep_silent_inferred_needs"):
        real = getattr(turn_application, name)
        monkeypatch.setattr(
            turn_application,
            name,
            lambda *a, _n=name, _r=real, **k: (order.append(_n), _r(*a, **k))[1],
        )
    real_record = turn_application.record_case_outcome

    def _record(case, *, at):
        order.append((at, outcome_module._IN_DECISION_CYCLE.get()))
        real_record(case, at=at)

    monkeypatch.setattr(turn_application, "record_case_outcome", _record)
    case = _confirmed_case()
    engine = _turn_engine()
    await _turn(engine, case, "errors gone")
    assert order == [
        "_sweep_needs_for_terminal_hypotheses",
        "sweep_silent_inferred_needs",
        ("cycle", True),
    ]
    assert case.progress.outcome.resolution.reason == ResolutionReason.CONFIRMED
    assert outcome_module._IN_DECISION_CYCLE.get() is False


async def test_writing_the_record_changes_no_text_and_no_decision(monkeypatch):
    """The same turn with and without the write: one reply, one set of
    notices, one offer."""

    async def _run():
        case = _confirmed_case()
        case.case_id = "case_00000000c0de"
        engine = _turn_engine()
        result = await _turn(engine, case, "errors gone")
        offer = case.offer_record.open
        return (
            result["agent_response"],
            [f["label"] for f in result["suggested_follow_ups"]],
            case.turn_history[-1].system_notices,
            (offer.kind, offer.basis, offer.summary),
        ), case

    written, with_record = await _run()
    monkeypatch.setattr(
        outcome_module, "compute_case_outcome", lambda prior, case, **_: prior
    )
    unwritten, without = await _run()
    assert with_record.progress.outcome is not None
    assert without.progress.outcome is None
    assert written == unwritten


async def test_both_terminal_transitions_write_the_record():
    _e, case, _result = await _offered_then_declined()
    execute_user_closure(case, case.user_id)
    assert case.progress.outcome.closure.reason == case.closure_reason

    case = _confirmed_case()
    engine = _turn_engine()
    await _turn(engine, case, "errors gone")
    await _turn(
        engine,
        case,
        "yes",
        intent_type="confirmation",
        intent_data={"value": True, "proposal_id": case.offer_record.open.proposal_id},
    )
    assert case.state == CaseState.RESOLVED
    record = case.progress.outcome
    assert record.resolution.confirmed_row_ids
    assert record.closure.reason is None


def test_a_write_that_raises_fails_nothing_and_is_counted(monkeypatch, caplog):
    counted = []
    monkeypatch.setattr(
        outcome_module,
        "case_outcome_write_failures_total",
        SimpleNamespace(
            labels=lambda at: SimpleNamespace(inc=lambda: counted.append(at))
        ),
    )

    def _boom(*_a, **_k):
        raise RuntimeError("no record")

    monkeypatch.setattr(outcome_module, "compute_case_outcome", _boom)
    case = investigating_case(current_turn=2)
    reason = execute_user_closure(case, case.user_id)
    assert case.state == CaseState.CLOSED and reason is not None
    assert case.progress.outcome is None
    assert counted == ["closed"]
    assert "the case outcome record was not written at closed" in caplog.text
