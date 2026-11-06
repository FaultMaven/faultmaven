"""#2036 D2.3 (with #2485 and #2509): a fix confirms a resolution only as a
remediation of the cause it was run against.

Each fix's target and kind (``ProposedAction.target_ids`` / ``fix_kind``) are
settled once, in the turn the user runs it, against the cause pool at the
start of that turn (``response_application._settle_fix_attempts``). Only a
remediation opens a cause window; an interception relieves but never resolves,
and never hides an earlier root fix. Every turn here enters through the real
apply (``_apply_investigation_updates``) and is judged by the real gate
(``assess_resolution_readiness``, ``fix_windows``, M6). The shapes are PR
#2538's review findings (``f..``), the round-64 critique's (``c..``) and
#2485's.
"""

from unittest.mock import patch

import pytest

from faultmaven.core.investigation.causal_graph.disconfirmation import (
    _fix_failure_refusal,
)
from faultmaven.core.investigation.causal_graph.pruning import prune_abandoned_nodes
from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_INTERCEPTION,
    FIX_REMEDIATION,
    FIX_SUCCEEDED,
    _cause_pool,
    confirming_polarity_rows,
    fix_windows,
)
from faultmaven.core.investigation.milestone_engine import response_application
from faultmaven.core.investigation.problem_status import invalidation_refusal
from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    build_fix_verification_block,
)
from faultmaven.core.investigation.schemas import (
    CausalNodeToAdd,
    HypothesisUpdate,
    MilestoneUpdates,
    NodeEvidenceLinkToAdd,
    RootCauseConclusionUpdate,
    SolutionToAdd,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
    derive_closure_reason,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    ClosureReason,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InvestigationActionType,
    NodeState,
    NodeType,
    ProposedAction,
    SolutionType,
    ValidationMethod,
)
from tests.unit.core.investigation.bound_rows import VERIFYING_ID
from tests.unit.core.investigation.fix_turns_2039 import _engine_rows
from tests.unit.core.investigation.recorded_turns import _record
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _CAUSAL,
    _CAUSE_GONE,
    _GONE,
    _PRESENT,
    _SUPPORT,
    _obs,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _ROOT_ID,
    _add_sibling_chain,
    _eid,
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _engine

pytestmark = pytest.mark.unit

_READY = ResolutionReadiness.READY
_ROOT_FIX = "a fix of the root cause"
_X = "cn_0000000000aa"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _fix(description, node_ref=None) -> SolutionToAdd:
    return SolutionToAdd(
        description=description,
        solution_type=SolutionType.CONFIG_CHANGE,
        estimated_impact="checkout recovers",
        risks="none",
        node_ref=node_ref,
    )


def _ran(ref="new_index_0") -> MilestoneUpdates:
    return MilestoneUpdates(solution_accepted=True, accepted_action_ref=ref)


def _intermediate(case, node_id=_X) -> str:
    """A rung below the identified root: the state a restart or a renewal
    resets."""
    case.causal_nodes[node_id] = CausalNode(
        node_id=node_id,
        statement="checkout pods hold exhausted pool connections",
        node_type=NodeType.INTERMEDIATE,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=True,
        generated_at_turn=3,
    )
    return node_id


async def _deepen(eng, case, hyp, turn, statement="pgbouncer runs in session mode"):
    """Re-root ``hyp`` onto a new identified root above its old one."""
    below = hyp.root_node_id
    await _record(
        eng,
        case,
        turn,
        _obs(_CAUSAL, f"{statement}: the config is templated by hand"),
        _obs(_CAUSAL, f"{statement}: nothing re-renders it"),
        causal_nodes_to_add=[
            CausalNodeToAdd(statement=statement, node_type="root", produces=below)
        ],
        node_evidence_links=[
            NodeEvidenceLinkToAdd(
                node_ref="new_index_0",
                evidence_id_ref=f"new_index_{i}",
                stance="supports",
                reasoning="bears on the new root",
            )
            for i in (0, 1)
        ],
        hypotheses_to_update=[
            HypothesisUpdate(
                hypothesis_id=hyp.hypothesis_id,
                root_node_ref="new_index_0",
                root_is_terminal=True,
                terminal_basis_evidence_ids=["new_index_1"],
            )
        ],
    )
    return case.causal_nodes[hyp.root_node_id]


async def _recheck(eng, case, turn, root, gone=True):
    """The cause re-checked gone (naming the root's support), and the problem
    gone or still present."""
    support = root.evidence_links[0].evidence_id
    problem = (
        _obs(_GONE, "checkout 200 again", VERIFYING_ID)
        if gone
        else _obs(_PRESENT, "checkout still 503", VERIFYING_ID)
    )
    return await _record(
        eng,
        case,
        turn,
        _obs(_CAUSE_GONE, "the re-check of the cause", support),
        problem,
    )


async def _failed_fix_of_a(eng, case):
    """A's fix (6) seen failing at 7: M6 demotes A, the only cause."""
    await _record(
        eng,
        case,
        6,
        solutions_to_add=[_fix("raise max_connections to 100")],
        milestones=_ran(),
    )
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_PRESENT, "checkout still returns 503", VERIFYING_ID),
    )


def _attempts(case) -> list:
    return [
        a
        for a in case.proposed_actions
        if a.action_type == InvestigationActionType.SOLUTION and a.state == "accepted"
    ]


def _readiness(case):
    return assess_resolution_readiness(case)


def _accepted(case, turn, kind=None, targets=()) -> ProposedAction:
    """An attempt registered at ``turn``, kinded as given (None: legacy)."""
    action = ProposedAction(
        case_id=case.case_id,
        action_type=InvestigationActionType.SOLUTION,
        description=f"fix accepted at turn {turn}",
        proposed_in_turn=turn - 1,
        accepted_in_turn=turn,
        state="accepted",
        target_ids=list(targets),
        fix_kind=kind,
    )
    case.proposed_actions.append(action)
    case.progress.solution_accepted = True
    return action


# ---------------------------------------------------------------------------
# The settle: target and kind, read once, when the user runs the fix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node_ref", [None, _ROOT_ID])
async def test_a_fix_of_the_identified_cause_is_a_remediation(node_ref):
    case, root, _hyp = _identified_case()
    await _record(
        _engine(),
        case,
        6,
        solutions_to_add=[_fix("raise max_connections to 100", node_ref)],
        milestones=_ran(),
    )
    (fix,) = _attempts(case)
    assert (fix.fix_kind, fix.target_ids, fix.target_named) == (
        FIX_REMEDIATION,
        [_ROOT_ID],
        node_ref is not None,
    )
    assert fix_windows(case)[-1].kind == FIX_REMEDIATION


@pytest.mark.parametrize("ref", ["new_index_0", "[new_index_0]"])
async def test_a_same_turn_new_index_target_resolves_on_the_action_and_the_solution(
    ref,
):
    """Findings 11 and 12: the ref resolves through ``_resolve_id_ref`` against
    the turn's created nodes, so the action and its Solution row agree."""
    case, _root, _hyp = _identified_case()
    await _record(
        _engine(),
        case,
        6,
        causal_nodes_to_add=[
            CausalNodeToAdd(
                statement="a cron job resets max_connections",
                node_type="root",
                produces=_ROOT_ID,
            )
        ],
        solutions_to_add=[_fix("remove the cron job", ref)],
    )
    (created,) = [
        n
        for n in case.causal_nodes
        if n not in (_ROOT_ID,) and case.causal_nodes[n].node_type == NodeType.ROOT
    ]
    action = case.proposed_actions[-1]
    assert (action.target_ids, action.target_named) == ([created], True)
    assert case.solutions[-1].node_id == created
    assert action.fix_kind is None  # not run yet


async def test_a_ref_naming_no_node_leaves_the_fix_unnamed_with_one_line():
    case, _root, _hyp = _identified_case()
    eng = _engine()
    _rows, meta = await _record(
        eng,
        case,
        6,
        solutions_to_add=[_fix("raise max_connections", "cn_00000000dead")],
    )
    assert "node_ref cn_00000000dead names no node" in "\n".join(meta["system_notices"])
    await _record(eng, case, 7, milestones=_ran(case.proposed_actions[-1].action_id))
    (fix,) = _attempts(case)
    assert (fix.target_ids, fix.target_named, fix.fix_kind) == (
        [_ROOT_ID],
        False,
        FIX_REMEDIATION,
    )


async def test_a_named_target_is_never_changed_by_a_restatement():
    case, _root, _hyp = _identified_case()
    eng = _engine()
    x = _intermediate(case)
    await _record(eng, case, 6, solutions_to_add=[_fix("restart the pods", x)])
    pending = case.proposed_actions[-1]
    await _record(eng, case, 7, solutions_to_add=[_fix("restart the pods", _ROOT_ID)])
    assert case.proposed_actions[-1] is pending
    assert pending.target_ids == [x]


async def test_an_intermediate_target_is_an_interception():
    """D2.3's class: a restart named at a rung below the identified root."""
    case, _root, _hyp = _identified_case()
    x = _intermediate(case)
    await _record(
        _engine(),
        case,
        6,
        solutions_to_add=[_fix("restart the pods", x)],
        milestones=_ran(),
    )
    (fix,) = _attempts(case)
    assert (fix.fix_kind, fix.target_ids) == (FIX_INTERCEPTION, [x])
    assert [w.fix_turn for w in fix_windows(case)] == [None]


async def test_a_same_turn_new_root_named_beside_a_standing_cause_is_an_interception():
    """The reference is the pool at the start of the turn: a root created in
    the turn the fix runs is not the cause it ran against while another one
    stood."""
    case, _root, _hyp = _identified_case()
    await _record(
        _engine(),
        case,
        6,
        causal_nodes_to_add=[
            CausalNodeToAdd(
                statement="a cron job resets max_connections",
                node_type="root",
                produces=_ROOT_ID,
            )
        ],
        solutions_to_add=[_fix("remove the cron job", "new_index_0")],
        milestones=_ran(),
    )
    assert _attempts(case)[0].fix_kind == FIX_INTERCEPTION


async def test_the_settle_runs_inside_the_one_recompute_and_keeps_the_withdrawal_notice():
    """Findings 1, 4 and 14: one recompute per apply, so the withdrawal notice
    the recompute writes is not erased by a second one."""
    case, _root, _hyp = _identified_case()
    calls = []
    real = response_application._recompute_assessment_state

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    with patch.object(response_application, "_recompute_assessment_state", counting):
        _rows, meta = await _record(
            _engine(),
            case,
            6,
            *[
                _obs(_CAUSAL, f"refuting observation {i}: the limit is 500 not 20")
                for i in range(4)
            ],
            node_evidence_links=[
                NodeEvidenceLinkToAdd(
                    node_ref=_ROOT_ID,
                    evidence_id_ref=f"new_index_{i}",
                    stance="refutes",
                    reasoning="contradicts the root",
                )
                for i in range(4)
            ],
            solutions_to_add=[_fix("raise max_connections to 100", _ROOT_ID)],
        )
    assert len(calls) == 1
    assert case.proposed_actions[-1].state == "superseded"
    assert "withdrawn" in "\n".join(meta["system_notices"])


# ---------------------------------------------------------------------------
# Windows: an interception relieves, never resolves, never hides a root fix
# ---------------------------------------------------------------------------


async def test_a_later_interception_does_not_hide_an_earlier_root_fix():
    """Finding 2: the re-checks land in the root fix's window, which still
    answers: a relief alone asks for the cause re-check, not for a fix
    already run; then READY as on ``main``, and the report cites the row."""
    case, root, _hyp = _identified_case()
    eng = _engine()
    x = _intermediate(case)
    await _record(
        eng,
        case,
        6,
        solutions_to_add=[_fix("raise max_connections to 100", _ROOT_ID)],
        milestones=_ran(),
    )
    await _record(
        eng,
        case,
        7,
        solutions_to_add=[_fix("restart the checkout pods", x)],
        milestones=_ran(),
    )
    await _record(eng, case, 8, _obs(_GONE, "checkout 200 again", VERIFYING_ID))
    assert "The cause re-checked after the fix" in _readiness(case).message
    await _recheck(eng, case, 9, root)
    assert [a.fix_kind for a in _attempts(case)] == [FIX_REMEDIATION, FIX_INTERCEPTION]
    assert fix_windows(case)[-1].fix_turn == 6
    assert fix_windows(case)[-1].state == FIX_SUCCEEDED
    assert _readiness(case).verdict == _READY
    assert len(confirming_polarity_rows(case)) == 1


async def test_after_an_interception_alone_the_gate_asks_for_a_fix_of_the_root():
    """Finding 6 and ruling 3: nothing after an interception confirms, the
    report's fallback cites no row, and readiness names what is missing."""
    case, root, _hyp = _identified_case()
    eng = _engine()
    x = _intermediate(case)
    await _record(
        eng,
        case,
        6,
        solutions_to_add=[_fix("restart the checkout pods", x)],
        milestones=_ran(),
    )
    await _recheck(eng, case, 7, root)
    readiness = _readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert readiness.missing[0] == _ROOT_FIX
    assert confirming_polarity_rows(case) == []
    assert derive_closure_reason(case) != ClosureReason.SOLUTION_DEFERRED


async def test_a_failed_fix_keeps_its_kind_after_m6_demotes_its_cause():
    """Finding 3: the kind was read when the fix ran, so M6's demotion does not
    turn a failed remediation into an interception; the block calls it a fix."""
    case, _root, hyp = _identified_case()
    _add_sibling_chain(case)
    eng = _engine()
    await _failed_fix_of_a(eng, case)
    window = fix_windows(case)[-1]
    assert hyp.state == HypothesisState.REFUTED
    assert (window.state, window.kind, window.disconfirms_cause) == (
        FIX_FAILED,
        FIX_REMEDIATION,
        True,
    )
    case.progress.solution_accepted = True
    (line,) = [
        ln
        for ln in build_fix_verification_block(case).splitlines()
        if "executed turn" in ln
    ]
    assert f"a fix of {_ROOT_ID}: failed at turn 7" in line


async def test_the_block_labels_each_attempt_by_its_own_kind():
    """Finding 10: a same-turn root fix and interception, each labelled."""
    case, _root, _hyp = _identified_case()
    x = _intermediate(case)
    await _record(
        _engine(),
        case,
        6,
        solutions_to_add=[_fix("raise max_connections to 100", _ROOT_ID)],
        milestones=_ran(),
    )
    _accepted(case, 6, FIX_INTERCEPTION, [x])
    lines = [
        ln
        for ln in build_fix_verification_block(case).splitlines()
        if "executed turn" in ln
    ]
    assert f"executed turn 6, a fix of {_ROOT_ID}:" in lines[0]
    assert (
        f"executed turn 6, an interception at {x} (cannot confirm the cause)"
        in lines[1]
    )


async def test_interceptions_count_in_no_fix_failure_cycle_and_relapse_demotes_nothing():
    """Finding 8, and an interception relapse: no window fails, M6 refuses
    with its own label, and the cause stands."""
    case, _root, hyp = _identified_case()
    eng = _engine()
    x = _intermediate(case)
    for turn, what in (
        (6, "restart the checkout pods"),
        (8, "scale the checkout pods"),
    ):
        await _record(
            eng, case, turn, solutions_to_add=[_fix(what, x)], milestones=_ran()
        )
        await _record(
            eng,
            case,
            turn + 1,
            _obs(_CAUSE_GONE, "the pool shows free slots", _SUPPORT),
            _obs(_GONE, "checkout 200 for a minute", VERIFYING_ID),
        )
    await _record(eng, case, 10, _obs(_PRESENT, "checkout 503 again", VERIFYING_ID))
    assert ProgressMonitor()._detect_fix_failure_cycle(case, "solution") is False
    assert hyp.state != HypothesisState.REFUTED and _engine_rows(case) == []
    assert _fix_failure_refusal(case, None) == "interception"
    assert _readiness(case).missing[0] == _ROOT_FIX


def test_pruning_a_target_drops_it_and_keeps_the_kind():
    """Finding 9, and PR #2558's delta 2 (F1): pruning drops a node from a
    kinded fix's target, its kind kept, named or not, remediation or
    interception, and from every fix's licence. A fix not yet kinded keeps
    its named target for the settle to kind it."""
    case, _root, _hyp = _identified_case()
    x = _intermediate(case)
    r = _orphan(case, "cn_00000000f00d", _problem(case).node_id)
    action = _accepted(case, 6, FIX_INTERCEPTION, [x])
    remediation = _accepted(case, 5, FIX_REMEDIATION, [r])
    remediation.target_named = True
    pending = ProposedAction(
        case_id=case.case_id,
        action_type=InvestigationActionType.SOLUTION,
        description="restart the pods",
        proposed_in_turn=7,
        target_ids=[x],
        target_named=True,
        licence_ids=[x],
    )
    case.proposed_actions.append(pending)
    prune_abandoned_nodes(case, [x, r])
    assert x not in case.causal_nodes and r not in case.causal_nodes
    assert (action.target_ids, action.fix_kind) == ([], FIX_INTERCEPTION)
    assert (remediation.target_ids, remediation.fix_kind) == ([], FIX_REMEDIATION)
    assert (pending.target_ids, pending.licence_ids) == ([x], [])


# ---------------------------------------------------------------------------
# Deepening: a window is never re-scoped after its fix ran
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("named", [True, False], ids=["named", "unnamed"])
async def test_a_renewal_before_a_deepening_confirms_only_a_fix_of_the_new_root(named):
    """c1030-g1: the renewal was a remediation of the then-cause; once the
    cause is deepened its window can no longer succeed, so a re-check of the
    new root reads NEEDS_INFO, then READY after a fix of that root."""
    case, cert, hyp = _identified_case()
    eng = _engine()
    await _record(eng, case, 5, solutions_to_add=[_fix("renew the certificate")])
    await _record(eng, case, 6, milestones=_ran(case.proposed_actions[-1].action_id))
    r2 = await _deepen(eng, case, hyp, 8, "nothing rotates the certificate")
    await _recheck(eng, case, 9, r2)
    assert r2.is_identified_root and fix_windows(case)[-1].off_cause
    readiness = _readiness(case)
    assert (readiness.verdict, readiness.missing[0]) == (
        ResolutionReadiness.NEEDS_INFO,
        _ROOT_FIX,
    )
    await _record(
        eng,
        case,
        10,
        solutions_to_add=[
            _fix("install the rotation CronJob", r2.node_id if named else None)
        ],
        milestones=_ran(),
    )
    await _recheck(eng, case, 11, r2)
    assert _attempts(case)[-1].target_ids == [r2.node_id]
    assert _readiness(case).verdict == _READY


@pytest.mark.parametrize("order", ["relapse-then-deepen", "deepen-then-relapse"])
async def test_a_relapsed_fix_stays_failed_after_a_deepening(order):
    """Done-when 5 for a dated window: the window binds to the renewal's own
    target, so the relapse on it still reads FAILED."""
    case, cert, hyp = _identified_case()
    eng = _engine()
    await _record(
        eng,
        case,
        5,
        solutions_to_add=[_fix("renew the certificate")],
        milestones=_ran(),
    )
    if order == "relapse-then-deepen":
        await _recheck(eng, case, 6, cert)
        await _record(eng, case, 7, _obs(_PRESENT, "checkout 503 again", VERIFYING_ID))
        await _deepen(eng, case, hyp, 8, "nothing rotates the certificate")
    else:
        await _deepen(eng, case, hyp, 6, "nothing rotates the certificate")
        await _recheck(eng, case, 7, cert)
        await _record(eng, case, 8, _obs(_PRESENT, "checkout 503 again", VERIFYING_ID))
    window = fix_windows(case)[-1]
    assert (window.fix_turn, window.state) == (5, FIX_FAILED)
    assert _readiness(case).verdict != _READY


async def test_window_zero_relapse_stays_failed_after_a_deepening():
    """Done-when 5 for window 0 (ruling 4): the deepening writes
    ``deepened_to``, so the relapse on the old root still fails window 0 and a
    re-check of the new root does not confirm an out-of-band fix."""
    case, a, hyp = _identified_case()
    eng = _engine()
    await _recheck(eng, case, 6, a)
    await _record(eng, case, 7, _obs(_PRESENT, "checkout 503 again", VERIFYING_ID))
    assert fix_windows(case)[0].state == FIX_FAILED
    r2 = await _deepen(eng, case, hyp, 8)
    assert a.metadata["deepened_to"] == r2.node_id and a.root_terminal is False
    await _recheck(eng, case, 9, r2)
    assert fix_windows(case)[0].state == FIX_FAILED
    assert _readiness(case).verdict != _READY


async def test_an_orphan_adoption_through_the_old_root_records_the_deepening():
    """Finding 7: the T1 re-root shares the attach writer, so the renewal of
    the old root confirms nothing about the adopted one."""
    case, a, hyp = _identified_case()
    eng = _engine()
    await _record(eng, case, 6, solutions_to_add=[_fix("renew it")], milestones=_ran())
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSAL, "pgbouncer.ini pool_mode=session"),
        _obs(_CAUSAL, "pgbouncer holds a server conn per client"),
        causal_nodes_to_add=[
            CausalNodeToAdd(
                statement=hyp.statement + " because pgbouncer runs in session mode",
                node_type="root",
                produces=a.node_id,
            )
        ],
        node_evidence_links=[
            NodeEvidenceLinkToAdd(
                node_ref="new_index_0",
                evidence_id_ref=f"new_index_{i}",
                stance="supports",
                reasoning="bears on it",
            )
            for i in (0, 1)
        ],
    )
    adopted = hyp.root_node_id
    assert adopted != a.node_id
    assert a.root_terminal is False and a.metadata["deepened_to"] == adopted
    assert fix_windows(case)[-1].off_cause
    (cause_row, gone), _meta = await _recheck(eng, case, 8, a)
    assert _readiness(case).verdict != _READY
    # The renewal's own window still sees its target re-checked: the problem
    # existed, whatever cause stands now.
    assert fix_windows(case)[-1].cause_rows == (cause_row,)
    assert invalidation_refusal(case, [gone.evidence_id], "nothing failed") == (
        "a cause was observed removed, so the problem existed"
    )


async def test_a_pending_fix_takes_its_target_when_it_runs_or_from_a_restatement():
    """Finding 5 (ruled (b)): an unnamed fix proposed before a deepening takes
    the cause identified when it runs; a restatement may name it once."""
    case, _a, hyp = _identified_case()
    eng = _engine()
    await _record(
        eng, case, 6, solutions_to_add=[_fix("switch pgbouncer to transaction mode")]
    )
    pending = case.proposed_actions[-1]
    b = await _deepen(eng, case, hyp, 7)
    await _record(
        eng,
        case,
        8,
        solutions_to_add=[_fix("switch pgbouncer to transaction mode", b.node_id)],
    )
    assert case.proposed_actions[-1] is pending
    assert (pending.target_ids, pending.target_named) == ([b.node_id], True)
    await _record(eng, case, 9, milestones=_ran(pending.action_id))
    await _recheck(eng, case, 10, b)
    assert pending.fix_kind == FIX_REMEDIATION
    assert _readiness(case).verdict == _READY


# ---------------------------------------------------------------------------
# The conclusion's cause, and the same-turn registration (M5's recovery)
# ---------------------------------------------------------------------------


def _bare_candidate(case, root, hyp) -> None:
    root.node_state = NodeState.CANDIDATE
    root.evidence_links = []
    root.metadata["root_terminal"] = None


def _rcc(names_root=None) -> RootCauseConclusionUpdate:
    return RootCauseConclusionUpdate(
        root_cause="max_connections is set to 20, below the checkout pool size",
        mechanism="the checkout pool opens more connections than postgres admits",
        evidence_ids=[_SUPPORT],
        likelihood=0.9,
        names_root_node_id=names_root,
    )


def _grounding(hyp):
    return dict(
        node_evidence_links=[
            NodeEvidenceLinkToAdd(
                node_ref=_ROOT_ID, evidence_id_ref=e, stance="supports", reasoning="r"
            )
            for e in (_SUPPORT, _eid("pool_metric"))
        ],
        hypotheses_to_update=[
            HypothesisUpdate(
                hypothesis_id=hyp.hypothesis_id,
                root_is_terminal=True,
                terminal_basis_evidence_ids=[_SUPPORT],
            )
        ],
    )


@pytest.mark.parametrize("identify", ["same-turn", "next-turn"])
async def test_conclusion_fix_and_acceptance_in_one_turn_with_no_cause_standing(
    identify,
):
    """Critique 3 and finding 13: with an empty pool at the start of the turn,
    the cause the turn's conclusion names is the reference."""
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    eng = _engine()
    await _record(eng, case, 6)
    assert not any(_cause_pool(case))
    await _record(
        eng,
        case,
        7,
        root_cause_conclusion=_rcc(_ROOT_ID),
        solutions_to_add=[_fix("raise max_connections to 100")],
        milestones=_ran(),
        **(_grounding(hyp) if identify == "same-turn" else {}),
    )
    if identify == "next-turn":
        await _record(eng, case, 8, **_grounding(hyp))
    assert _attempts(case)[0].fix_kind == FIX_REMEDIATION
    await _record(
        eng,
        case,
        9,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert _readiness(case).verdict == _READY


async def test_a_fix_licensed_by_a_conclusion_in_words_only_stays_unkinded():
    """Ruling 2: unkinded, read as before D2.3, and the model is asked for the
    cause's node."""
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    eng = _engine()
    await _record(eng, case, 6)
    _rows, meta = await _record(
        eng,
        case,
        7,
        root_cause_conclusion=_rcc(),
        solutions_to_add=[_fix("raise max_connections to 100")],
        milestones=_ran(),
        **_grounding(hyp),
    )
    (fix,) = _attempts(case)
    assert (fix.fix_kind, fix.target_ids) == (None, [])
    assert "names its cause in words only" in "\n".join(meta["system_notices"])
    await _record(
        eng,
        case,
        9,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert _readiness(case).verdict == _READY


@pytest.mark.parametrize("named", [True, False], ids=["named", "conclusion"])
async def test_the_only_cause_refuted_then_re_fixed_through_the_apply_still_resolves(
    named,
):
    """#1927's re-fix on the apply path: the failed-fix notice's route
    re-asserts the cause with a conclusion; the fix names it, or takes the
    conclusion's root, which is in the pool's fallen leg."""
    case, root, hyp = _identified_case()
    eng = _engine()
    await _failed_fix_of_a(eng, case)
    assert hyp.state == HypothesisState.REFUTED
    await _record(
        eng,
        case,
        10,
        root_cause_conclusion=_rcc(_ROOT_ID),
        solutions_to_add=[
            _fix("raise max_connections in the template", _ROOT_ID if named else None)
        ],
        milestones=_ran(),
    )
    assert _attempts(case)[-1].fix_kind == FIX_REMEDIATION
    await _record(
        eng,
        case,
        11,
        _obs(_CAUSE_GONE, "max_connections reads 200", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert _readiness(case).verdict == _READY


# ---------------------------------------------------------------------------
# #2485: a fallen cause is not confirmed past a fix of another candidate
# ---------------------------------------------------------------------------


def _problem(case):
    return next(
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )


def _orphan(case, node_id, effect) -> str:
    case.causal_nodes[node_id] = CausalNode(
        node_id=node_id,
        statement="a memory leak in the gunicorn worker" if effect else "a duplicate",
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.3,
        actionable=True,
        generated_at_turn=8,
    )
    case.causal_edges.append(CausalEdge(cause_node_id=node_id, effect_node_id=effect))
    return node_id


@pytest.mark.parametrize(
    "shape",
    [
        "orphan-b-named",
        "orphan-b-unnamed",
        "retired-b",
        "inconclusive-b",
        "rootless-active",
        "duplicate-below",
    ],
)
async def test_a_fallen_cause_is_not_confirmed_past_another_candidates_fix(shape):
    """#2485's shapes: A fell; the fix the user then ran, licensed by a
    conclusion naming B (or by the working conclusion), acted on B or on
    nothing named, so A's re-check after it confirms nothing."""
    case, a, _hyp = _identified_case()
    eng = _engine()
    await _failed_fix_of_a(eng, case)
    target, named = None, None
    if shape.startswith("orphan-b"):
        named = _orphan(case, "cn_00000000b0b0", _problem(case).node_id)
        target = named if shape == "orphan-b-named" else None
    elif shape in ("retired-b", "inconclusive-b"):
        target = named = _add_sibling_chain(case).node_id
        case.causal_edges.append(
            CausalEdge(cause_node_id=target, effect_node_id=_problem(case).node_id)
        )
        case.hypotheses["hyp_00000000eeee"].state = (
            HypothesisState.RETIRED
            if shape == "retired-b"
            else HypothesisState.INCONCLUSIVE
        )
    elif shape == "rootless-active":
        case.hypotheses["hyp_00000000f1a7"] = Hypothesis(
            hypothesis_id="hyp_00000000f1a7",
            statement="a DNS flap",
            category=HypothesisCategory.CODE,
            state=HypothesisState.ACTIVE,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="a flat guess",
            initial_likelihood=0.8,
            likelihood=0.8,
            generated_at_turn=8,
        )
    else:
        target = named = _orphan(case, "cn_00000000d0d0", a.node_id)
    await _record(eng, case, 9)
    await _record(
        eng,
        case,
        10,
        solutions_to_add=[_fix("fix accepted at turn 10", target)],
        milestones=_ran(),
        **({"root_cause_conclusion": _rcc(named)} if named else {}),
    )
    assert _attempts(case)[-1].fix_kind == FIX_INTERCEPTION
    await _record(
        eng,
        case,
        11,
        _obs(_CAUSE_GONE, "max_connections reads 100 still", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    readiness = _readiness(case)
    assert (readiness.verdict, readiness.missing[0]) == (
        ResolutionReadiness.NEEDS_INFO,
        _ROOT_FIX,
    )


@pytest.mark.parametrize(
    "latest, confirms",
    [(None, True), ("legacy", True), ("of-a", True), ("of-b", False)],
)
def test_the_fallen_leg_stands_only_for_the_latest_remediations_target(
    latest, confirms
):
    """Pool leg 3 (#2485): with no remediation or a legacy latest one it reads
    as on ``main``; a kinded one keeps it only for the fallen roots it names."""
    case, a, hyp = _identified_case()
    hyp.state = HypothesisState.REFUTED
    a.node_state = NodeState.REFUTED
    case.progress.identified_cause_root_ids = [a.node_id]
    if latest == "legacy":
        _accepted(case, 6)
    elif latest == "of-a":
        _accepted(case, 6, FIX_REMEDIATION, [a.node_id])
    elif latest == "of-b":
        _accepted(case, 6, FIX_REMEDIATION, [a.node_id])
        _accepted(case, 8, FIX_REMEDIATION, ["cn_00000000b0b0"])
    confirmation, window, _hyps = _cause_pool(case)
    assert confirmation == (frozenset({a.node_id}) if confirms else frozenset())
    assert window == frozenset({a.node_id})
