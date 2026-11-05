"""#2036 Phase 3, D2.3: a fix records its target, and a window is read against
the cause its fix named. With it, #2485 (a fallen cause confirming past another
candidate's fix) and window 0's relapse after a deepening.

A case resolves on a fix only when that fix's recorded target is the case's
cause. A fix of anything else, an untargeted one included, can relieve the
problem; it never confirms or disconfirms the cause. A relapse observed on a
cause still fails window 0 after the cause is deepened.

Every turn goes through ``_apply_investigation_updates`` and its real
recompute; every reading is the real gate's (``assess_resolution_readiness``,
``fix_windows``, M6). No live LLM.
"""

import pytest

from faultmaven.core.investigation.causal_graph.disconfirmation import (
    _fix_failure_refusal,
)
from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_INTERCEPTION,
    FIX_REMEDIATION,
    cause_recheck_status,
    fix_windows,
    latest_decided_fix_window,
)
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_assessment_state,
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
    SolutionToAdd,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    EvidenceCategory,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InvestigationActionType,
    NodeState,
    NodeType,
    SolutionType,
    ValidationMethod,
)
from tests.unit.core.investigation.bound_rows import VERIFYING_ID
from tests.unit.core.investigation.fix_turns_2039 import _engine_rows
from tests.unit.core.investigation.test_fix_registration_lag_2427 import (
    _PREVIOUS,
    _ev,
    _registered,
    _reported,
)
from tests.unit.core.investigation.test_fix_registration_lag_2427 import (
    _identified as _identified_2427,
)
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _CAUSE_GONE,
    _GONE,
    _PRESENT,
    _SUPPORT,
    _obs,
    _record,
    _support,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _ROOT_ID,
    _accept,
    _add_sibling_chain,
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _engine
from tests.unit.core.investigation.test_window_zero_relapse_2501 import _relapsed

pytestmark = pytest.mark.unit

_CAUSAL = EvidenceCategory.CAUSAL_EVIDENCE
_ROOT_FIX = "a fix of the root cause"
_B = "cn_00000000eeee"


def _fix(description, node_ref=None) -> SolutionToAdd:
    return SolutionToAdd(
        description=description,
        solution_type=SolutionType.CONFIG_CHANGE,
        estimated_impact="checkout recovers",
        risks="none",
        node_ref=node_ref,
    )


def _accepted() -> MilestoneUpdates:
    return MilestoneUpdates(solution_accepted=True, accepted_action_ref="new_index_0")


def _deepening(hyp, below, statement, **fields) -> tuple[list, dict]:
    """One response's rows and fields re-rooting ``hyp`` onto a new root above
    ``below``, with two supports of its own: what validates and identifies
    it."""
    rows = [
        _obs(_CAUSAL, f"{statement}: the config is templated by hand"),
        _obs(_CAUSAL, f"{statement}: nothing re-renders it"),
    ]
    return rows, dict(
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
            )
        ],
        **fields,
    )


async def _deepen(eng, case, hyp, turn, statement="pgbouncer runs in session mode"):
    below = hyp.root_node_id
    rows, fields = _deepening(hyp, below, statement)
    await _record(eng, case, turn, *rows, **fields)
    root = case.causal_nodes[hyp.root_node_id]
    assert root.is_identified_root, "premise: the deeper cause is identified"
    return root


async def _recheck(eng, case, turn, root):
    """The root's own support re-checked removed, and the problem gone."""
    support = root.evidence_links[0].evidence_id
    return await _record(
        eng,
        case,
        turn,
        _obs(_CAUSE_GONE, "the re-check of the cause", support),
        _obs(_GONE, "checkout 200 again", VERIFYING_ID),
    )


def _verdict(case):
    return assess_resolution_readiness(case).verdict


# ---------------------------------------------------------------------------
# 1. The renewal, c1030-g1's shape
# ---------------------------------------------------------------------------


async def test_1_a_renewal_before_a_deepening_never_confirms_the_deeper_cause():
    """The fix of A registered at 6 (its target settled to A), A deepened to
    B at 7, B re-checked removed with the problem gone at 8: the renewal's
    window never confirms B. A fix of B, accepted and re-checked, does."""
    case, a, hyp = _identified_case()
    eng = _engine()
    await _record(
        eng, case, 6, solutions_to_add=[_fix("renew it")], milestones=_accepted()
    )
    b = await _deepen(eng, case, hyp, 7)
    await _recheck(eng, case, 8, b)

    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert readiness.missing[0] == _ROOT_FIX
    assert a.metadata["deepened_to"] == b.node_id
    reason = (
        f"the fix executed at turn 6 acted on {_ROOT_ID}, not the root; only a "
        "fix of the root can confirm the cause"
    )
    assert [why for _row, why in cause_recheck_status(case)] == [reason]
    assert (
        f"executed turn 6, an interception at {_ROOT_ID}, not the root"
        in build_fix_verification_block(case)
    )

    await _record(
        eng,
        case,
        9,
        solutions_to_add=[_fix("re-template", b.node_id)],
        milestones=_accepted(),
    )
    assert case.proposed_actions[-1].target_ids == [b.node_id]
    await _recheck(eng, case, 10, b)
    assert fix_windows(case)[-1].kind == FIX_REMEDIATION
    assert _verdict(case) == ResolutionReadiness.READY


# ---------------------------------------------------------------------------
# 2. A target from the same turn; a restatement never retargets
# ---------------------------------------------------------------------------


async def test_2_a_new_index_node_ref_resolves_to_the_node_created_that_turn():
    case, _a, _hyp = _identified_case()
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
    )
    created = [n for n in case.causal_nodes if n != _ROOT_ID and n.startswith("cn_")]
    cron = next(n for n in created if case.causal_nodes[n].node_type == NodeType.ROOT)
    assert case.proposed_actions[-1].target_ids == [cron]


async def test_2_a_fix_sent_with_its_deepening_targets_the_new_root():
    """The re-root and the fix in one response, no node_ref: the target is
    read at settled time, so it is the new root, not the old one."""
    case, _a, hyp = _identified_case()
    eng = _engine()
    rows, fields = _deepening(
        hyp,
        _ROOT_ID,
        "pgbouncer runs in session mode",
        solutions_to_add=[_fix("re-template")],
        milestones=_accepted(),
    )
    await _record(eng, case, 7, *rows, **fields)
    assert case.proposed_actions[-1].target_ids == [hyp.root_node_id] != [_ROOT_ID]
    assert fix_windows(case)[-1].kind == FIX_REMEDIATION


async def test_2_a_restatement_keeps_its_target():
    case, _a, _hyp = _identified_case()
    eng = _engine()
    b = _add_sibling_chain(case)
    await _record(eng, case, 6, solutions_to_add=[_fix("raise max_connections")])
    first = case.proposed_actions[-1]
    await _record(
        eng, case, 7, solutions_to_add=[_fix("raise max_connections", b.node_id)]
    )
    assert case.proposed_actions[-1] is first and first.target_ids == [_ROOT_ID]


# ---------------------------------------------------------------------------
# 3. #2485: a fallen cause confirms only a fix that names it
# ---------------------------------------------------------------------------


async def _a_fell():
    """A identified, its fix (8) seen failing at 9: M6 demotes A."""
    case, _a, hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 8)
    eng = _engine()
    await _record(
        eng,
        case,
        9,
        _obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT),
        _obs(_PRESENT, "checkout still returns 503", VERIFYING_ID),
    )
    assert hyp.state == HypothesisState.REFUTED and _engine_rows(case), "premise"
    return eng, case


def _candidate(case, *, hypothesis=None, below=None, statement="a worker leak"):
    """B: a ROOT with one support, on its own edge to the problem (or below
    ``below``), named by a hypothesis in state ``hypothesis`` or by none."""
    problem = next(
        n for n in case.causal_nodes.values() if n.node_type == NodeType.PROBLEM
    )
    node = CausalNode(
        node_id=_B,
        statement=statement,
        node_type=NodeType.ROOT,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.3,
        actionable=True,
        evidence_links=[],
        generated_at_turn=10,
    )
    case.causal_nodes[_B] = node
    case.causal_edges.append(
        CausalEdge(cause_node_id=_B, effect_node_id=problem.node_id)
    )
    if below:
        case.causal_edges.append(CausalEdge(cause_node_id=below, effect_node_id=_B))
    if hypothesis:
        case.hypotheses["hyp_00000000eeee"] = Hypothesis(
            hypothesis_id="hyp_00000000eeee",
            statement=statement,
            category=HypothesisCategory.CODE,
            state=hypothesis,
            generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
            rationale="another candidate",
            root_node_id=_B,
            initial_likelihood=0.2,
            generated_at_turn=10,
        )


def _rootless(case, likelihood=0.4):
    case.hypotheses["hyp_00000000f1a7"] = Hypothesis(
        hypothesis_id="hyp_00000000f1a7",
        statement="a memory leak in the gunicorn workers",
        category=HypothesisCategory.CODE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="a flat guess",
        initial_likelihood=likelihood,
        likelihood=likelihood,
        generated_at_turn=10,
    )


_SHAPES = {
    "orphan": (dict(), [_B]),
    "retired": (dict(hypothesis=HypothesisState.RETIRED), [_B]),
    "inconclusive": (dict(hypothesis=HypothesisState.INCONCLUSIVE), [_B]),
    "duplicate-below": (dict(below=_ROOT_ID, statement="max_connections is 20"), [_B]),
    "active-rootless": (None, []),
}


@pytest.mark.parametrize("shape", list(_SHAPES))
async def test_3_another_candidates_fix_is_not_confirmed_through_the_fallen_cause(
    shape,
):
    """#2485 and its ``## Pulled`` shapes: B's fix, then A's support
    re-checked with the problem gone, reads NEEDS_INFO."""
    eng, case = await _a_fell()
    candidate, targets = _SHAPES[shape]
    if candidate is None:
        _rootless(case)
    else:
        _candidate(case, **candidate)
    await _record(eng, case, 10)
    _accept(case, InvestigationActionType.SOLUTION, 12, targets)
    await _recheck(eng, case, 13, case.causal_nodes[_ROOT_ID])

    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO
    assert fix_windows(case)[-1].kind == FIX_INTERCEPTION


@pytest.mark.parametrize("stray", [False, True], ids=["alone", "beside-a-stray-root"])
async def test_3_a_re_fix_that_names_the_fallen_cause_resolves(stray):
    """#1927: the fallen A re-fixed and re-checked resolves; a stray edgeless
    ROOT no hypothesis names does not block it."""
    eng, case = await _a_fell()
    if stray:
        case.causal_nodes[_B] = CausalNode(
            node_id=_B,
            statement="a disk filled up",
            node_type=NodeType.ROOT,
            generated_at_turn=10,
        )
    _accept(case, InvestigationActionType.SOLUTION, 12, [_ROOT_ID])
    await _recheck(eng, case, 13, case.causal_nodes[_ROOT_ID])
    assert _verdict(case) == ResolutionReadiness.READY


@pytest.mark.parametrize("named", [None, "rcc", "node_ref"])
async def test_3_a_fix_licensed_by_another_hypothesis_is_not_the_fallen_causes(named):
    """The ruled default (#2036, 2026-11-05): A fell, a rootless B at 0.75 is
    the working conclusion, and the model registers a fix naming nothing. It
    is not attributed to A, so A's re-check confirms nothing. Named A, by a
    conclusion or by node_ref, it is the re-fix and resolves."""
    eng, case = await _a_fell()
    _rootless(case, likelihood=0.75)
    await _record(eng, case, 10)
    _rows, meta = await _record(
        eng,
        case,
        12,
        solutions_to_add=[_fix("patch it", _ROOT_ID if named == "node_ref" else None)],
        milestones=_accepted(),
        root_cause_conclusion=(
            RootCauseConclusionUpdate(
                root_cause="max_connections below the checkout pool size",
                mechanism="the pool waits on connections the limit refuses",
                likelihood=0.8,
                names_root_node_id=_ROOT_ID,
            )
            if named == "rcc"
            else None
        ),
    )
    await _recheck(eng, case, 13, case.causal_nodes[_ROOT_ID])
    if named:
        assert _verdict(case) == ResolutionReadiness.READY
        return
    assert _verdict(case) == ResolutionReadiness.NEEDS_INFO
    assert case.proposed_actions[-1].target_ids == []
    assert "SYSTEM: The fix registered as" in meta["system_feedback"]
    assert "names no node it acted on" in cause_recheck_status(case)[-1][1]


# ---------------------------------------------------------------------------
# 4. An untargeted fix relieves and never confirms
# ---------------------------------------------------------------------------


async def test_4_an_untargeted_fix_relieves_but_never_confirms():
    case, _a, _hyp = _identified_case()
    _accept(case, InvestigationActionType.SOLUTION, 6, [])
    await _recheck(_engine(), case, 7, case.causal_nodes[_ROOT_ID])

    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == ResolutionReadiness.NEEDS_INFO
    assert readiness.missing[0] == _ROOT_FIX
    assert "cause re-checked after the fix" not in readiness.message
    window = fix_windows(case)[-1]
    assert (window.kind, window.relief_turn) == (FIX_INTERCEPTION, 7)
    assert window.state != "succeeded" and not window.confirmed_cause_rows


# ---------------------------------------------------------------------------
# 5. Window 0's relapse stands after the cause is deepened
# ---------------------------------------------------------------------------


async def test_5_window_0s_relapse_stands_after_two_deepenings():
    """Relapsed at 6 (#2501); A deepened to B at 7 and B to C at 8; C
    re-checked removed with the problem gone at 9. Window 0 stays FAILED, so
    a new fix is asked for; a dated fix of C then resolves."""
    case, a, hyp = _identified_case()
    eng, case = await _relapsed(case=case)
    assert fix_windows(case)[0].state == FIX_FAILED, "premise"
    b = await _deepen(eng, case, hyp, 7)
    c = await _deepen(eng, case, hyp, 8, "the pool template is never reviewed")
    await _recheck(eng, case, 9, c)

    window = fix_windows(case)[0]
    assert (window.state, window.relief_turn, window.failure_turn) == (FIX_FAILED, 5, 6)
    assert "a new fix after the one that did not hold" in (
        assess_resolution_readiness(case).missing
    )
    assert (a.metadata["deepened_to"], b.metadata["deepened_to"]) == (
        b.node_id,
        c.node_id,
    )

    await _record(
        eng,
        case,
        10,
        solutions_to_add=[_fix("review it", c.node_id)],
        milestones=_accepted(),
    )
    await _recheck(eng, case, 11, c)
    assert _verdict(case) == ResolutionReadiness.READY


# ---------------------------------------------------------------------------
# 6. An interception never disconfirms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relapse", [False, True], ids=["persists", "relapses"])
@pytest.mark.parametrize("target", ["sibling", "rung", "none"])
async def test_6_an_interceptions_failure_demotes_nothing(target, relapse):
    """A's support re-checked removed after a fix of another node, then the
    problem present (or gone, then back): the window FAILS, M6 leaves A
    standing, and the refusal reads ``interception``."""
    case, root, hyp = _identified_case()
    targets = []
    if target == "sibling":
        targets = [_add_sibling_chain(case).node_id]
    elif target == "rung":
        rung = CausalNode(
            node_id="cn_0000000ce47e",
            statement="the checkout pool waits on slots",
            node_type=NodeType.INTERMEDIATE,
            generated_at_turn=1,
        )
        case.causal_nodes[rung.node_id] = rung
        targets = [rung.node_id]
    _accept(case, InvestigationActionType.SOLUTION, 6, targets)
    eng = _engine()
    first = [_obs(_CAUSE_GONE, "max_connections reads 100", _SUPPORT)]
    if relapse:
        first.append(_obs(_GONE, "checkout 200 again", VERIFYING_ID))
    await _record(eng, case, 7, *first)
    await _record(eng, case, 8, _obs(_PRESENT, "checkout still 503", VERIFYING_ID))

    assert hyp.state == HypothesisState.VALIDATED and _engine_rows(case) == []
    assert _fix_failure_refusal(case, latest_decided_fix_window(case)) == (
        "interception"
    )
    window = fix_windows(case)[-1]
    assert (window.state, window.kind, window.disconfirmation) == (
        FIX_FAILED,
        FIX_INTERCEPTION,
        None,
    )


# ---------------------------------------------------------------------------
# 7. The settled-time re-read changes nothing a second time
# ---------------------------------------------------------------------------


async def test_7_a_second_recompute_after_a_settled_turn_changes_nothing():
    """#2427's shape: the fix minted and accepted in the turn its failure is
    seen, so M6 can act only once the target is settled (the re-read). A
    recompute after that turn changes nothing, and no notice repeats."""
    eng, case, hyp = _identified_2427()
    await _reported(eng, case)
    meta = await _registered(
        eng, case, _PREVIOUS, _ev(_PRESENT, "checkout returns 503 again")
    )
    assert hyp.state == HypothesisState.REFUTED, "premise: the re-read demoted it"
    lines = (meta.get("system_feedback") or "").splitlines()
    assert len(lines) == len(set(lines))

    before = case.model_dump(exclude={"working_conclusion": {"updated_at"}})
    _recompute_assessment_state(case, metadata={})
    assert case.model_dump(exclude={"working_conclusion": {"updated_at"}}) == before
