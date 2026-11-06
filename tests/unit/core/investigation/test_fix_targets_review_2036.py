"""#2036 D2.3, PR #2553's review (``## Review — e94689e``): the kind's
reference when no cause stands, the window binding, and the readers that
report on them.

- **The reference (F2, F4, F11).** With no root identified or count-held at
  the start of the turn, the reference also admits what the fix's licence
  names: the root of every standing rooted hypothesis
  (``cause_assurance.licensed_roots``). The unnamed default is read from the two
  records (``test_fix_records_2036``): the start-of-turn licence, or the
  licence M5 recorded on the fix when it was proposed.
- **The binding (F1, F3).** A kinded window binds to its targets plus the
  roots that fell and are not the cause now. An unkinded window ends at the
  first interception by which it has FAILED, and spans it while live.
- **The readers (F5, F6, F9, F10).** F8's backdate refusal went with
  the re-plan on recorded facts: a backdated fix reads its turn's record.

Every turn enters through the real apply and is judged by the real gate.
"""

import pytest

from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_INTERCEPTION,
    FIX_REMEDIATION,
    cause_recheck_status,
    conclusion_node,
    confirming_polarity_rows,
    fix_windows,
    interception_relief,
)
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    standing_fix_action,
)
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    build_fix_verification_block,
)
from faultmaven.core.investigation.schemas import (
    CausalNodeToAdd,
    HypothesisUpdate,
    MilestoneUpdates,
    NodeEvidenceLinkToAdd,
    RootCauseConclusionUpdate,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    NodeState,
    RootCauseConclusion,
)
from tests.unit.core.investigation.bound_rows import VERIFYING_ID
from tests.unit.core.investigation.recorded_turns import _record
from tests.unit.core.investigation.test_fix_targets_2036 import (
    _ROOT_FIX,
    _accepted,
    _attempts,
    _bare_candidate,
    _deepen,
    _failed_fix_of_a,
    _fix,
    _grounding,
    _intermediate,
    _orphan,
    _problem,
    _ran,
    _recheck,
)
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
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _engine,
    _link,
    _refute,
)

pytestmark = pytest.mark.unit

_READY = ResolutionReadiness.READY
_B_HYP = "hyp_00000000eeee"
_LEAD = "hyp_0000000ead01"


def _ground_b(b) -> dict:
    """B's two supports and its terminal verdict: B identified."""
    return dict(
        node_evidence_links=[
            NodeEvidenceLinkToAdd(
                node_ref=b.node_id,
                evidence_id_ref=f"new_index_{i}",
                stance="supports",
                reasoning="bears on the leak",
            )
            for i in (0, 1)
        ],
        hypotheses_to_update=[
            HypothesisUpdate(
                hypothesis_id=_B_HYP,
                root_is_terminal=True,
                terminal_basis_evidence_ids=["new_index_1"],
            )
        ],
    )


def _leak_rows():
    return (
        _obs(_CAUSAL, "worker RSS grows 40MB/h until OOM"),
        _obs(_CAUSAL, "OOM killer log for gunicorn worker every hour"),
    )


async def _a_fell_b_identified(eng):
    """A's fix (6) seen failing at 7, so M6 demotes A; B grounded at 8."""
    case, a, hyp = _identified_case()
    b = _add_sibling_chain(case)
    await _failed_fix_of_a(eng, case)
    await _record(eng, case, 8, *_leak_rows(), **_ground_b(b))
    assert hyp.state == HypothesisState.REFUTED and b.is_identified_root
    return case, b


async def _renewed_then_deepened(eng, deepen_turn=6):
    """A renewal of the identified root A at 5, a remediation then; the cause
    deepened to R2 at ``deepen_turn``."""
    case, a, hyp = _identified_case()
    await _record(
        eng,
        case,
        5,
        solutions_to_add=[_fix("renew the certificate")],
        milestones=_ran(),
    )
    r2 = await _deepen(eng, case, hyp, deepen_turn, "nothing rotates the certificate")
    return case, hyp, r2


# ---------------------------------------------------------------------------
# The reference when no cause stands (F2, F4, F11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("named", [True, False], ids=["named", "unnamed"])
async def test_the_m5_recovery_after_a_failed_fix_is_a_remediation_of_the_new_cause(
    named,
):
    """P1: A fell; in one response the model concludes B, grounds it, and
    records the fix of B the user ran. The conclusion names B, a standing
    hypothesis's root, so the fix is a remediation of B and resolves."""
    eng = _engine()
    case, _a, _hyp = _identified_case()
    b = _add_sibling_chain(case)
    await _failed_fix_of_a(eng, case)
    await _record(eng, case, 8)
    await _record(
        eng,
        case,
        9,
        *_leak_rows(),
        root_cause_conclusion=RootCauseConclusionUpdate(
            root_cause="a memory leak in the gunicorn worker",
            mechanism="the worker leaks until OOM and the pod 503s",
            evidence_ids=["new_index_0"],
            likelihood=0.9,
            names_root_node_id=b.node_id,
        ),
        solutions_to_add=[
            _fix("upgrade the leaking library", b.node_id if named else None)
        ],
        milestones=_ran(),
        **_ground_b(b),
    )
    fix = _attempts(case)[-1]
    assert (fix.fix_kind, fix.target_ids) == (FIX_REMEDIATION, [b.node_id])
    await _recheck(eng, case, 10, b)
    assert assess_resolution_readiness(case).verdict == _READY


@pytest.mark.parametrize("named", [True, False], ids=["named", "unnamed"])
async def test_a_fix_under_the_working_conclusions_licence_is_a_remediation(named):
    """P3: no cause in the pool and no conclusion; the hypothesis leads at
    0.85 (M5's working-conclusion leg). A fix of its root, named or not, is
    a remediation, and resolves once the root is grounded and re-checked."""
    eng = _engine()
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    hyp.likelihood = hyp.initial_likelihood = 0.85
    await _record(eng, case, 6)
    await _record(
        eng,
        case,
        7,
        solutions_to_add=[
            _fix("raise max_connections to 100", _ROOT_ID if named else None)
        ],
        milestones=_ran(),
    )
    assert (_attempts(case)[0].fix_kind, _attempts(case)[0].target_ids) == (
        FIX_REMEDIATION,
        [_ROOT_ID],
    )
    await _record(eng, case, 8, **_grounding(hyp))
    await _record(
        eng,
        case,
        9,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert assess_resolution_readiness(case).verdict == _READY


async def test_a_fix_of_a_trailing_candidate_is_a_remediation_of_it():
    """Every standing rooted hypothesis, not only the leading one: A fell, B
    leads at 0.7 and C trails at 0.4. A fix named at C, then C grounded and
    re-checked gone with the problem gone, resolves."""
    eng = _engine()
    case, _a, _hyp = _identified_case()
    c = _add_sibling_chain(case)
    case.hypotheses[_B_HYP].likelihood = 0.4
    await _failed_fix_of_a(eng, case)
    leading = _orphan(case, "cn_00000000b0b0", _problem(case).node_id)
    case.hypotheses["hyp_00000000b0b0"] = Hypothesis(
        hypothesis_id="hyp_00000000b0b0",
        statement="the gunicorn worker count is too low",
        category=HypothesisCategory.CODE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="r",
        initial_likelihood=0.7,
        likelihood=0.7,
        generated_at_turn=8,
        root_node_id=leading,
    )
    await _record(eng, case, 9)
    await _record(
        eng, case, 10, solutions_to_add=[_fix("fix of c", c.node_id)], milestones=_ran()
    )
    assert _attempts(case)[-1].fix_kind == FIX_REMEDIATION
    await _record(eng, case, 11, *_leak_rows(), **_ground_b(c))
    await _recheck(eng, case, 12, c)
    assert assess_resolution_readiness(case).verdict == _READY


async def test_a_conclusion_naming_an_intermediate_names_no_cause():
    """F11 and delta item 2: the conclusion's root admits only a ROOT node. A
    conclusion naming a rung names a node, not words, so the unnamed fix
    targets that rung and is an interception: grounding and re-checking the
    root afterwards does not resolve on it."""
    eng = _engine()
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    # The recompute derives the bare root's state: no cause identified.
    await _record(eng, case, 5)
    x = _intermediate(case)
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="pods hold exhausted connections",
        mechanism="m",
        names_root_node_id=x,
        likelihood=0.8,
        confidence_level="confident",
    )
    assert conclusion_node(case, None, x) is None
    _rows, meta = await _record(
        eng, case, 6, solutions_to_add=[_fix("restart the pods")], milestones=_ran()
    )
    fix = _attempts(case)[0]
    assert (fix.fix_kind, fix.target_ids) == (FIX_INTERCEPTION, [x])
    assert "words only" not in (meta.get("system_feedback") or "")
    await _record(eng, case, 7, **_grounding(hyp))
    await _record(
        eng,
        case,
        8,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )
    assert assess_resolution_readiness(case).missing[0] == _ROOT_FIX


# ---------------------------------------------------------------------------
# The binding (F1, F3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("problem", ["gone", "present"])
async def test_a_failed_window_does_not_read_the_cause_identified_since(problem):
    """P1b: A fell; B is identified; a restart (an interception) runs; B is
    re-checked. A's failed window does not bind B's re-check, so M6 does not
    refute B on it."""
    eng = _engine()
    case, b = await _a_fell_b_identified(eng)
    x = _intermediate(case)
    await _record(
        eng, case, 9, solutions_to_add=[_fix("restart the pods", x)], milestones=_ran()
    )
    (b_recheck, _), _meta = await _recheck(eng, case, 10, b, gone=problem == "gone")
    assert b_recheck not in fix_windows(case)[-1].cause_rows
    assert case.hypotheses[_B_HYP].state != HypothesisState.REFUTED
    assert b.node_state == NodeState.VALIDATED


async def test_a_deeper_roots_recheck_does_not_fail_the_renewals_window():
    """P4: the renewal (5) was a fix of A; after the deepening to R2 (6), R2
    re-checked gone with the problem present (7) is not the renewal's
    outcome: its window does not FAIL, and R2 is not refuted on it."""
    eng = _engine()
    case, hyp, r2 = await _renewed_then_deepened(eng)
    await _recheck(eng, case, 7, r2, gone=False)
    window = fix_windows(case)[-1]
    assert (window.fix_turn, window.off_cause) == (5, True)
    assert window.state != FIX_FAILED
    assert hyp.state != HypothesisState.REFUTED and r2.node_state == NodeState.VALIDATED
    assert assess_resolution_readiness(case).missing[0] == _ROOT_FIX


async def test_a_failed_legacy_fix_absorbs_no_recheck_after_an_interception():
    """An unkinded fix (accepted before D2.3) at 6 fails at 7; B is identified
    at 8; a restart at 9; B re-checked gone with the problem present at 10.
    The legacy window ends at the restart, so B is not refuted on it."""
    eng = _engine()
    case, _a, hyp = _identified_case()
    b = _add_sibling_chain(case)
    _accepted(case, 6)
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_PRESENT, "checkout still returns 503", VERIFYING_ID),
    )
    assert hyp.state == HypothesisState.REFUTED
    await _record(eng, case, 8, *_leak_rows(), **_ground_b(b))
    x = _intermediate(case)
    await _record(
        eng, case, 9, solutions_to_add=[_fix("restart the pods", x)], milestones=_ran()
    )
    await _recheck(eng, case, 10, b, gone=False)
    window = fix_windows(case)[-1]
    assert (window.fix_turn, window.end_turn, window.state) == (6, 9, FIX_FAILED)
    assert case.hypotheses[_B_HYP].state != HypothesisState.REFUTED


async def test_a_live_legacy_fix_spans_an_interception_and_resolves():
    """Ruling 1: an unkinded fix still live when a restart runs keeps the
    reading it had, READY on its re-checks."""
    eng = _engine()
    case, root, _hyp = _identified_case()
    _accepted(case, 6)
    x = _intermediate(case)
    await _record(
        eng, case, 7, solutions_to_add=[_fix("restart the pods", x)], milestones=_ran()
    )
    await _recheck(eng, case, 8, root)
    assert (fix_windows(case)[-1].fix_turn, fix_windows(case)[-1].end_turn) == (6, None)
    assert assess_resolution_readiness(case).verdict == _READY


# ---------------------------------------------------------------------------
# The readers (F5, F6, F9, F10)
# ---------------------------------------------------------------------------


async def test_a_recheck_after_an_off_cause_fix_says_why_it_does_not_count():
    """P5: the renewal's window is observed and off_cause, a newer fix of R2
    is not re-checked yet; the re-check of R2 is not counted, and the reason
    says so rather than "the latest fix was seen failing"."""
    eng = _engine()
    case, a, hyp = _identified_case()
    await _record(
        eng,
        case,
        5,
        solutions_to_add=[_fix("renew the certificate")],
        milestones=_ran(),
    )
    await _record(eng, case, 6, _obs(_PRESENT, "checkout still 503", VERIFYING_ID))
    r2 = await _deepen(eng, case, hyp, 7, "nothing rotates the certificate")
    (row,), _meta = await _record(
        eng,
        case,
        8,
        _obs(
            _CAUSE_GONE, "a rotation job exists now", r2.evidence_links[0].evidence_id
        ),
    )
    await _record(
        eng,
        case,
        9,
        solutions_to_add=[_fix("install the rotation CronJob", r2.node_id)],
        milestones=_ran(),
    )
    (why,) = [why for e, why in cause_recheck_status(case) if e is row]
    assert why == (
        "the fix executed at turn 5 acted on a cause no longer identified; the "
        "newer fix (executed turn 9) is not re-checked yet"
    )


async def test_the_report_cites_no_row_from_an_off_cause_window():
    """F6: the gate refuses R2's re-check in the renewal's window, and the
    report's fallback does not render it as confirming either."""
    eng = _engine()
    case, _hyp, r2 = await _renewed_then_deepened(eng)
    await _recheck(eng, case, 7, r2)
    assert fix_windows(case)[-1].off_cause
    assert assess_resolution_readiness(case).missing[0] == _ROOT_FIX
    assert confirming_polarity_rows(case) == []


async def test_an_interception_rerun_after_a_relapse_is_a_new_attempt():
    """F9: an interception stands while the problem stays gone; once it is
    back, the same restart run again is a new attempt."""
    eng = _engine()
    case, _root, _hyp = _identified_case()
    x = _intermediate(case)
    await _record(
        eng, case, 6, solutions_to_add=[_fix("restart the pods", x)], milestones=_ran()
    )
    await _record(eng, case, 7, _obs(_GONE, "checkout 200", VERIFYING_ID))
    assert standing_fix_action(case, "restart the pods") is not None
    await _record(eng, case, 8, _obs(_PRESENT, "checkout 503 again", VERIFYING_ID))
    assert standing_fix_action(case, "restart the pods") is None
    await _record(
        eng, case, 9, solutions_to_add=[_fix("restart the pods", x)], milestones=_ran()
    )
    assert [a.accepted_in_turn for a in _attempts(case)] == [6, 9]


async def test_an_interceptions_line_shows_its_relapse_and_no_later_relief():
    """F10: the latest gone and present turns, up to the next attempt: the
    relapse at 9 is shown, and a later root fix's relief is not credited."""
    eng = _engine()
    case, root, _hyp = _identified_case()
    x = _intermediate(case)
    await _record(
        eng, case, 6, solutions_to_add=[_fix("restart the pods", x)], milestones=_ran()
    )
    await _record(eng, case, 7, _obs(_PRESENT, "checkout 503 still", VERIFYING_ID))
    await _record(eng, case, 8, _obs(_GONE, "checkout 200", VERIFYING_ID))
    await _record(eng, case, 9, _obs(_PRESENT, "checkout 503 again", VERIFYING_ID))
    assert interception_relief(case, 6) == (8, 9)
    await _record(
        eng,
        case,
        10,
        solutions_to_add=[_fix("raise max_connections", _ROOT_ID)],
        milestones=_ran(),
    )
    await _recheck(eng, case, 11, root)
    assert interception_relief(case, 6) == (8, 9)
    line, _root_fix = [
        ln
        for ln in build_fix_verification_block(case).splitlines()
        if "executed turn" in ln
    ]
    assert line.endswith(
        f"an interception at {x} (cannot confirm the cause): problem seen again at turn 9"
    )
    assert _attempts(case)[0].fix_kind == FIX_INTERCEPTION


# ---------------------------------------------------------------------------
# Delta 1 on fd0f96a: the licence M5 read, F10's bound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("named", [False, True], ids=["unnamed", "named"])
@pytest.mark.parametrize("move", ["link", "update", "m5-recovery"])
async def test_a_reply_moving_the_cause_needs_the_fix_named_to_credit_the_new_one(
    move, named
):
    """Ruled 2026-11-06 (ruling 1's reading): A (R1) leads at the start of
    turn 7, and the reply the user runs the fix in moves the cause to B: a
    row refuting A (delta 1's p5), an update lowering A (delta 2's P3), or a
    conclusion naming B, grounded at once (an M5 recovery beside a rooted
    leader). Unnamed, the fix is credited to R1, the start-of-turn licence,
    so B's re-check reads NEEDS_INFO; named at B (``node_ref``), READY."""
    eng = _engine()
    case, a, hyp = _identified_case()
    _bare_candidate(case, a, hyp)
    b = _add_sibling_chain(case)
    hyp.likelihood = hyp.initial_likelihood = 0.85 if move == "m5-recovery" else 0.7
    if move != "m5-recovery":
        case.hypotheses[_B_HYP].likelihood = 0.65
        case.hypotheses[_B_HYP].initial_likelihood = 0.65
    await _record(eng, case, 6)
    rows = [_obs(_CAUSAL, "pg_stat_activity: 12 of 20 connections in use")]
    if move == "link":
        fields = dict(
            hypothesis_evidence_links=[_link(hyp.hypothesis_id, "new_index_0")]
        )
    elif move == "update":
        fields = dict(
            hypotheses_to_update=[
                HypothesisUpdate(hypothesis_id=hyp.hypothesis_id, likelihood=0.3)
            ]
        )
    else:
        rows = list(_leak_rows())
        fields = dict(
            root_cause_conclusion=RootCauseConclusionUpdate(
                root_cause="a memory leak in the gunicorn worker",
                mechanism="the worker leaks until OOM",
                evidence_ids=["new_index_0"],
                likelihood=0.9,
                names_root_node_id=b.node_id,
            ),
            **_ground_b(b),
        )
    await _record(
        eng,
        case,
        7,
        *rows,
        solutions_to_add=[
            _fix("pin the leaking library", b.node_id if named else None)
        ],
        milestones=_ran(),
        **fields,
    )
    fix = _attempts(case)[0]
    target = b.node_id if named else a.node_id
    assert (fix.fix_kind, fix.target_ids) == (FIX_REMEDIATION, [target])
    if move != "m5-recovery":
        await _record(eng, case, 8, *_leak_rows(), **_ground_b(b))
    await _recheck(eng, case, 9, b)
    assert assess_resolution_readiness(case).verdict == (
        _READY if named else ResolutionReadiness.NEEDS_INFO
    )


@pytest.mark.parametrize("grounded", ["the-leaders-own-root", "a-trailing-root"])
async def test_a_fix_licensed_by_an_unrooted_leader_is_credited_to_no_root(grounded):
    """Delta item 3 (as ruled, option A): the working conclusion leads with a
    hypothesis that has no root node. The unnamed fix it licenses has no
    target, so it is an interception, and neither the leader's root posited
    later nor a trailing rooted hypothesis is credited with it."""
    eng = _engine()
    case, root, hyp = _identified_case()
    _bare_candidate(case, root, hyp)
    hyp.likelihood = hyp.initial_likelihood = 0.3
    case.hypotheses[_LEAD] = Hypothesis(
        hypothesis_id=_LEAD,
        statement="the pgbouncer pool is too small for the checkout burst",
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="r",
        initial_likelihood=0.85,
        likelihood=0.85,
        generated_at_turn=2,
    )
    await _record(eng, case, 6)
    _rows, meta = await _record(
        eng,
        case,
        7,
        solutions_to_add=[_fix("raise the pgbouncer default_pool_size to 50")],
        milestones=_ran(),
    )
    fix = _attempts(case)[0]
    assert (fix.fix_kind, fix.target_ids) == (FIX_INTERCEPTION, [])
    assert "without a target" not in (meta.get("system_feedback") or "")
    if grounded == "a-trailing-root":
        await _record(eng, case, 8, **_grounding(hyp))
        await _record(
            eng,
            case,
            9,
            _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
            _obs(_GONE, "checkout 200 again", VERIFYING_ID),
        )
    else:
        await _record(
            eng,
            case,
            8,
            _obs(_CAUSAL, "pgbouncer SHOW POOLS: cl_waiting 40 at the burst"),
            _obs(_CAUSAL, "pgbouncer default_pool_size = 20"),
            causal_nodes_to_add=[
                CausalNodeToAdd(
                    statement="the pgbouncer pool is too small for the burst",
                    node_type="root",
                    produces="D",
                )
            ],
            node_evidence_links=[
                NodeEvidenceLinkToAdd(
                    node_ref="new_index_0",
                    evidence_id_ref=f"new_index_{i}",
                    stance="supports",
                    reasoning="bears on the pool size",
                )
                for i in (0, 1)
            ],
            hypotheses_to_update=[
                HypothesisUpdate(
                    hypothesis_id=_LEAD,
                    root_node_ref="new_index_0",
                    root_is_terminal=True,
                    terminal_basis_evidence_ids=["new_index_1"],
                )
            ],
        )
        lead_root = case.causal_nodes[case.hypotheses[_LEAD].root_node_id]
        await _recheck(eng, case, 9, lead_root)
    assert assess_resolution_readiness(case).missing[0] == _ROOT_FIX


async def test_a_relapse_in_the_next_attempts_own_turn_is_the_interceptions():
    """F10's bound: a problem-present row in the next attempt's own turn is
    still the interception's (as a window reads it), while a relief in that
    turn is the next attempt's."""
    eng = _engine()
    case, _root, _hyp = _identified_case()
    x = _intermediate(case)
    await _record(
        eng, case, 6, solutions_to_add=[_fix("restart the pods", x)], milestones=_ran()
    )
    await _record(eng, case, 7, _obs(_GONE, "checkout 200", VERIFYING_ID))
    await _record(
        eng,
        case,
        9,
        _obs(_PRESENT, "checkout 503 again", VERIFYING_ID),
        _obs(_GONE, "checkout 200 after the config change", VERIFYING_ID),
        solutions_to_add=[_fix("raise max_connections", _ROOT_ID)],
        milestones=_ran(),
    )
    assert interception_relief(case, 6) == (7, 9)
