"""#2060: a root a chain was deepened past is a rung to every reader.

Deepening keeps the old root's stored ``NodeType.ROOT`` (#2036), and the
causal-graph block shows it as ``intermediate``. The ruling (2026-11-17,
option (a)): its position decides, however it was validated. One predicate,
``deepened_past_root_ids`` (a ROOT-typed node on a shown hypothesis's path
that no shown hypothesis is rooted at), is read by the label, the bar, the
notes, the grade and the readers marked *position*. A re-root back onto such a
node puts it to the ROOT bar again in the same turn (``attach_root``).
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.causal_graph import pruning
from faultmaven.core.investigation.causal_graph.derivation import (
    derive_node_states,
    validate_by_exclusion,
)
from faultmaven.core.investigation.causal_graph.pruning import attach_root
from faultmaven.core.investigation.causal_graph.queries import deepened_past_root_ids
from faultmaven.core.investigation.causal_graph.support import (
    _restating_root_ids,
    restatement_held_root_ids,
    root_restates_case_frame,
    root_support_block_reasons,
    summarize_restatement_hold,
)
from faultmaven.core.investigation.causal_map import render_causal_map
from faultmaven.core.investigation.cause_assurance import (
    FIX_FAILED,
    FIX_INTERCEPTION,
    FIX_REMEDIATION,
    _pool_legs,
    _validated_roots,
    fix_windows,
    grade_cause_assurance,
)
from faultmaven.core.investigation.milestone_engine import chain_emission
from faultmaven.core.investigation.milestone_engine.stage_gates import (
    licence_leg_ids,
)
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    _build_causal_graph_block,
)
from faultmaven.core.investigation.schemas import (
    CausalNodeToAdd,
    HypothesisToAdd,
    HypothesisUpdate,
    NodeEvidenceLinkToAdd,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
    CauseAssuranceGrade,
    CauseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    EvidenceStance,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    NodeEvidenceLink,
    NodeState,
    NodeType,
    ValidationMethod,
)
from tests.case_builders import investigating_case
from tests.unit.core.investigation.recorded_turns import _record
from tests.unit.core.investigation.test_fix_targets_2036 import (
    _attempts,
    _deepen,
    _failed_fix_of_a,
    _fix,
    _problem,
    _ran,
    _rcc,
    _recheck,
)
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _CAUSAL,
    _obs,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _add_sibling_chain,
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _engine

pytestmark = pytest.mark.unit

_PROBLEM = "checkout requests fail with 502 under load"
_D, _X, _Y = "cn_0000000000d0", "cn_0000000000e1", "cn_0000000000f1"
_HYP = "hyp_000000000001"
_COUNTER = (
    "faultmaven.core.investigation.causal_graph.derivation."
    "root_validation_blocked_support_count_total"
)


# ---------------------------------------------------------------------------
# Builders: a graph a hypothesis is rooted in, deepened through attach_root
# ---------------------------------------------------------------------------


def _eid(label: str) -> str:
    return "ev_" + hashlib.md5(label.encode()).hexdigest()[:12]


def _evidence(label: str) -> Evidence:
    return Evidence(
        evidence_id=_eid(label),
        summary=f"fact-{label} metric-{label} reading-{label}",
        primary_purpose="diagnosis",
        category=EvidenceCategory.CAUSAL_EVIDENCE,
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_0123456789ab",
        collected_by="llm",
        collected_at_turn=2,
        collected_at=datetime.now(timezone.utc),
    )


def _supports(node: CausalNode, case, *labels, confidence: float = 1.0) -> None:
    for label in labels:
        case.evidence.append(_evidence(label))
        node.evidence_links.append(
            NodeEvidenceLink(
                evidence_id=_eid(label),
                stance=EvidenceStance.SUPPORTS,
                reasoning="bears on the node",
                linked_at_turn=2,
                stance_confidence=confidence,
            )
        )


def _node(node_id, statement, node_type=NodeType.ROOT) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement=statement,
        node_type=node_type,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=node_type == NodeType.ROOT,
        generated_at_turn=1,
    )


def _case(x_statement="pool connections leak until the pool is exhausted"):
    """A hypothesis rooted at X (``path = [X, D]``); Y, ROOT-typed, sits
    above X with nothing linked."""
    nodes = [
        _node(_D, _PROBLEM, NodeType.PROBLEM),
        _node(_X, x_statement),
        _node(_Y, "a config push halved the pool's connection lifetime"),
    ]
    hyp = Hypothesis(
        hypothesis_id=_HYP,
        statement="leaked connections exhaust the checkout pool",
        category=HypothesisCategory.DATABASE,
        state=HypothesisState.ACTIVE,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="posited",
        root_node_id=_X,
        path=[_X, _D],
        generated_at_turn=1,
    )
    case = investigating_case(
        case_id="case_000000002060",
        current_turn=5,
        statement=_PROBLEM,
        symptom=_PROBLEM,
        causal_nodes={n.node_id: n for n in nodes},
        causal_edges=[
            CausalEdge(cause_node_id=_Y, effect_node_id=_X),
            CausalEdge(cause_node_id=_X, effect_node_id=_D),
        ],
        evidence=[],
        hypotheses={hyp.hypothesis_id: hyp},
    )
    return case, case.causal_nodes[_X], hyp


def _deepen_to_y(case, hyp, turn=6) -> None:
    attach_root(case, hyp, _Y, [_Y, _X, _D], turn)


def _line(block: str, node_id: str) -> str:
    return next(line for line in block.splitlines() if node_id in line)


# ---------------------------------------------------------------------------
# The regression: the bar, the label and the note read one predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["deepened", "passed-through"])
def test_a_root_deepened_past_validates_on_one_support_as_the_rung_it_is_shown_as(
    shape,
):
    """The issue's fixture: X, ROOT-typed with one causal support, below the
    root of a hypothesis's path. On main it stayed INCONCLUSIVE, shown
    ``[intermediate/inconclusive]`` with the ROOT bar's SECOND INDEPENDENT
    note. ``passed-through``: the path runs through X, never the
    hypothesis's root (an orphan the chain was extended through)."""
    case, x, hyp = _case()
    _supports(x, case, "x1")
    if shape == "passed-through":
        attach_root(case, hyp, _Y, [_Y, _D], 5)
        case.causal_edges.append(CausalEdge(cause_node_id=_Y, effect_node_id=_D))
        attach_root(case, hyp, _Y, [_Y, _X, _D], 6)
    else:
        _deepen_to_y(case, hyp)
    derive_node_states(case)
    block = _build_causal_graph_block(case)
    assert deepened_past_root_ids(case) == {_X}
    assert x.node_state == NodeState.VALIDATED
    assert "[intermediate/validated]" in _line(block, _X)
    assert "SECOND INDEPENDENT" not in block


# ---------------------------------------------------------------------------
# Amendment 1: every deepened-past node, however it was validated
# ---------------------------------------------------------------------------


def test_deepening_past_a_validated_root_drops_the_grade_until_the_new_root_validates():
    case, x, hyp = _case()
    _supports(x, case, "x1", "x2")
    derive_node_states(case)
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC

    _deepen_to_y(case, hyp)
    derive_node_states(case)
    assert x.node_state == NodeState.VALIDATED  # a validated rung
    assert _validated_roots(case) == []
    assert grade_cause_assurance(case) == CauseAssuranceGrade.NO_ROOT

    _supports(case.causal_nodes[_Y], case, "y1", "y2")
    derive_node_states(case)
    assert [n.node_id for n in _validated_roots(case)] == [_Y]
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC


# ---------------------------------------------------------------------------
# A node any shown hypothesis is rooted at stands as a root: the shared
# paths, through the real apply, with the invariant checked at every attach
# ---------------------------------------------------------------------------


def _invariant(case) -> None:
    shown_roots = {
        h.root_node_id
        for h in case.hypotheses.values()
        if h.state != HypothesisState.RETIRED and h.root_node_id
    }
    assert not shown_roots & deepened_past_root_ids(case)


def _checked_attaches():
    """``attach_root`` at both call sites, asserting the invariant after
    every attach; the list counts them."""
    seen: list = []

    def checked(case, hyp, root_id, path, turn):
        attach_root(case, hyp, root_id, path, turn)
        _invariant(case)
        seen.append(root_id)

    return seen, (
        patch.object(chain_emission, "attach_root", checked),
        patch.object(pruning, "attach_root", checked),
    )


def _one_determination(case) -> None:
    """#695: a hypothesis reads VALIDATED only over a validated root the
    grade counts."""
    roots = {n.node_id for n in _validated_roots(case)}
    for h in case.hypotheses.values():
        if h.state == HypothesisState.VALIDATED:
            assert h.root_node_id in roots
            assert grade_cause_assurance(case) != CauseAssuranceGrade.NO_ROOT


async def test_a_rival_rooted_above_an_identified_root_leaves_it_identified():
    """A new hypothesis's path runs through H1's identified root A: A is
    H1's root, so it stands as one; the grade stays MECHANISTIC."""
    case, a, hyp = _identified_case()
    seen, (p1, p2) = _checked_attaches()
    with p1, p2:
        await _record(
            _engine(),
            case,
            6,
            _obs(_CAUSAL, "pgbouncer.ini pool_mode=session"),
            causal_nodes_to_add=[
                CausalNodeToAdd(
                    statement="an unrelated deploy removed the pooler sidecar",
                    node_type="root",
                    produces=a.node_id,
                )
            ],
            hypotheses_to_add=[
                HypothesisToAdd(
                    statement="an unrelated deploy removed the pooler sidecar",
                    category=HypothesisCategory.DATABASE,
                    likelihood=0.4,
                    rationale="r",
                    root_node_ref="new_index_0",
                )
            ],
        )
    rival = next(h for h in case.hypotheses.values() if h is not hyp)
    assert seen and a.node_id in rival.path[1:]
    assert a.node_id not in deepened_past_root_ids(case)
    assert a.is_identified_root and hyp.root_node_id == a.node_id
    assert case.progress.cause_state == CauseState.IDENTIFIED
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC
    _one_determination(case)


async def test_a_root_handed_to_an_heir_stands_as_a_root_after_a_restatement():
    """H1 deepens past A onto R2 while a new H2 is rooted at A; later H1
    restates its unchanged root ref. A is the heir's root throughout."""
    case, a, hyp = _identified_case()
    eng = _engine()
    seen, (p1, p2) = _checked_attaches()
    with p1, p2:
        await _record(
            eng,
            case,
            6,
            _obs(_CAUSAL, "nothing re-renders pgbouncer.ini"),
            _obs(_CAUSAL, "it is templated by hand"),
            causal_nodes_to_add=[
                CausalNodeToAdd(
                    statement="pgbouncer runs in session mode",
                    node_type="root",
                    produces=a.node_id,
                )
            ],
            node_evidence_links=[
                NodeEvidenceLinkToAdd(
                    node_ref="new_index_0",
                    evidence_id_ref=f"new_index_{i}",
                    stance="supports",
                    reasoning="b",
                )
                for i in (0, 1)
            ],
            hypotheses_to_add=[
                HypothesisToAdd(
                    statement="max_connections is too low for the checkout fleet",
                    category=HypothesisCategory.DATABASE,
                    likelihood=0.4,
                    rationale="r",
                    root_node_ref=a.node_id,
                )
            ],
            hypotheses_to_update=[
                HypothesisUpdate(
                    hypothesis_id=hyp.hypothesis_id, root_node_ref="new_index_0"
                )
            ],
        )
        heir = next(h for h in case.hypotheses.values() if h is not hyp)
        r2 = hyp.root_node_id
        assert heir.root_node_id == a.node_id and a.node_id in hyp.path[1:]
        await _record(
            eng,
            case,
            8,
            hypotheses_to_update=[
                HypothesisUpdate(hypothesis_id=hyp.hypothesis_id, root_node_ref=r2)
            ],
        )
    assert len(seen) >= 2
    assert a.node_id not in deepened_past_root_ids(case)
    assert a.node_state == NodeState.VALIDATED
    assert a.node_id in {n.node_id for n in _validated_roots(case)}
    assert "[root/validated]" in _line(_build_causal_graph_block(case), a.node_id)
    _one_determination(case)


# ---------------------------------------------------------------------------
# The re-root reset (amendment 4): in attach_root, before the recompute
# ---------------------------------------------------------------------------


def test_5a_a_rung_validated_on_one_support_re_rooted_ends_inconclusive():
    """Its reason is the position alone: re-rooted, X faces the ROOT bar,
    which one support does not clear whatever state X entered with."""
    case, x, hyp = _case()
    _deepen_to_y(case, hyp)
    _supports(x, case, "x1")
    derive_node_states(case)
    assert x.node_state == NodeState.VALIDATED
    attach_root(case, hyp, _X, [_X, _D], 7)
    derive_node_states(case)
    assert x.node_state == NodeState.INCONCLUSIVE


def test_5b_a_rung_with_two_independent_supports_re_validates_as_a_root():
    case, x, hyp = _case()
    _deepen_to_y(case, hyp)
    _supports(x, case, "x1", "x2")
    derive_node_states(case)
    attach_root(case, hyp, _X, [_X, _D], 7)
    assert x.node_state == NodeState.CANDIDATE  # the reset, before the recompute
    derive_node_states(case)
    assert (x.node_state, x.validation_method) == (
        NodeState.VALIDATED,
        ValidationMethod.EMPIRICAL,
    )


def test_5c_a_rung_restating_the_frame_re_rooted_faces_the_restatement_guard():
    """The guard is an entry bar, so a VALIDATED root keeps its state; the
    reset is what puts X through it. Without the reset (the hypothesis
    pointed at X by assignment), X stays VALIDATED."""
    case, x, hyp = _case(x_statement=_PROBLEM)
    _deepen_to_y(case, hyp)
    _supports(x, case, "x1", "x2")
    derive_node_states(case)
    assert x.node_state == NodeState.VALIDATED  # rungs are outside the guard
    attach_root(case, hyp, _X, [_X, _D], 7)
    derive_node_states(case)
    assert x.node_state == NodeState.INCONCLUSIVE

    control, cx, chyp = _case(x_statement=_PROBLEM)
    _deepen_to_y(control, chyp)
    _supports(cx, control, "x1", "x2")
    derive_node_states(control)
    chyp.root_node_id, chyp.path = _X, [_X, _D]
    derive_node_states(control)
    assert cx.node_state == NodeState.VALIDATED


def test_5d_a_deductive_validation_stamped_while_the_node_stood_as_root_is_kept():
    case, x, hyp = _case()
    x.node_state = NodeState.VALIDATED
    x.validation_method = ValidationMethod.DEDUCTIVE
    _deepen_to_y(case, hyp)
    derive_node_states(case)
    attach_root(case, hyp, _X, [_X, _D], 7)
    derive_node_states(case)
    assert (x.node_state, x.validation_method) == (
        NodeState.VALIDATED,
        ValidationMethod.DEDUCTIVE,
    )


# ---------------------------------------------------------------------------
# The readers marked "position"
# ---------------------------------------------------------------------------


def test_a_deepened_past_validated_node_is_no_root_to_the_position_readers():
    """The pool legs (and through them the record's ``cause.*`` fields), the
    report map's gate, the rcc licence leg and the deductive lane."""
    case, x, hyp = _case()
    _supports(x, case, "x1", "x2")
    derive_node_states(case)
    _deepen_to_y(case, hyp)
    derive_node_states(case)
    x.metadata["root_terminal"] = True
    x.metadata["root_terminal_evidence_ids"] = [_eid("x1")]
    case.progress.identified_cause_root_ids = [_X]
    assert x.is_identified_root  # the node-local property; callers filter
    identified, held, fallen = _pool_legs(case)
    assert _X not in identified | held | fallen
    assert licence_leg_ids(case, "rcc", (None, _X)) == ([_X], False)


def test_the_report_map_draws_no_validated_rung_as_the_established_cause():
    """Another validated root, off every chain into D, holds the grade; the
    only validated node on the chain is the rung X."""
    case, x, hyp = _case()
    _supports(x, case, "x1")
    _deepen_to_y(case, hyp)
    z = _node("cn_0000000000c3", "a noisy neighbour saturates the node's NIC")
    case.causal_nodes[z.node_id] = z
    _supports(z, case, "z1", "z2")
    derive_node_states(case)
    assert x.node_state == z.node_state == NodeState.VALIDATED
    assert grade_cause_assurance(case) == CauseAssuranceGrade.MECHANISTIC
    assert render_causal_map(case) is None


@pytest.mark.parametrize("deepened", [True, False])
def test_the_deductive_lane_validates_only_a_node_standing_as_a_root(deepened):
    case, x, hyp = _case()
    sibling = _node("cn_0000000000b2", "the pooler restarts every minute")
    sibling.node_state = NodeState.REFUTED
    sibling.refutation_reason = "the pooler has run for 9 days"
    sibling.belief = 0.0
    case.causal_nodes[sibling.node_id] = sibling
    case.causal_edges.append(
        CausalEdge(cause_node_id=sibling.node_id, effect_node_id=_D)
    )
    if deepened:
        _deepen_to_y(case, hyp)
    assert validate_by_exclusion(case, {_X}) is not deepened
    assert (x.node_state == NodeState.VALIDATED) is not deepened


def test_a_deepened_past_node_is_outside_the_restatement_guard():
    case, x, hyp = _case(x_statement=_PROBLEM)
    _supports(x, case, "x1", "x2")
    assert _X in _restating_root_ids(case)
    _deepen_to_y(case, hyp)
    assert _X not in _restating_root_ids(case)
    assert not root_restates_case_frame(x, case)
    derive_node_states(case)
    assert _X not in restatement_held_root_ids(case)


def test_a_deepened_past_node_is_no_unresolved_root_beside_a_restatement_hold():
    """Y restates the problem and is held by the guard alone; X, below it,
    is a rung. The hold governs the case, as the grade (NO_ROOT) says."""
    case, x, hyp = _case()
    y = case.causal_nodes[_Y]
    y.statement = _PROBLEM
    _supports(y, case, "y1", "y2")
    _supports(x, case, "x1", confidence=0.3)
    _deepen_to_y(case, hyp)
    derive_node_states(case)
    hold = summarize_restatement_hold(case)
    assert hold is not None and hold.root_ids == {_Y}
    assert hold.is_sole_root_block
    assert grade_cause_assurance(case) == CauseAssuranceGrade.NO_ROOT


@pytest.mark.parametrize("deepened", [True, False])
def test_the_root_block_metric_counts_only_a_node_standing_as_a_root(deepened):
    """X has one hedged causal support: a held rung when deepened past,
    a held root otherwise (the control)."""
    case, x, hyp = _case()
    _supports(x, case, "x1", confidence=0.3)
    if deepened:
        _deepen_to_y(case, hyp)
    else:
        hyp.root_node_id, hyp.path = _Y, [_Y, _D]
    with patch(_COUNTER) as counter:
        derive_node_states(case)
    assert x.node_state == NodeState.INCONCLUSIVE
    assert counter.labels.called is not deepened
    assert (_X in root_support_block_reasons(case)) is not deepened


# ---------------------------------------------------------------------------
# Engine-driven: the kinded window and the fix kind across a deepening
# ---------------------------------------------------------------------------


async def test_a_kinded_window_keeps_a_fallen_cause_deepened_past_after_m6():
    """Row 18's REFUTES arm stays on the stored type: M6 demotes A, a fix of
    B is run, a new chain is deepened past A, and A's re-check still fails
    B's window, which binds the roots that fell."""
    case, a, hyp = _identified_case()
    eng = _engine()
    await _failed_fix_of_a(eng, case)
    b = _add_sibling_chain(case).node_id
    case.causal_edges.append(
        CausalEdge(cause_node_id=b, effect_node_id=_problem(case).node_id)
    )
    await _record(eng, case, 9)
    await _record(
        eng,
        case,
        10,
        solutions_to_add=[_fix("restart the gunicorn workers nightly", b)],
        milestones=_ran(),
        root_cause_conclusion=_rcc(b),
    )
    assert _attempts(case)[-1].fix_kind == FIX_REMEDIATION
    hyp.state = HypothesisState.RETIRED
    await _record(
        eng,
        case,
        11,
        causal_nodes_to_add=[
            CausalNodeToAdd(
                statement="pgbouncer runs in session mode",
                node_type="root",
                produces=a.node_id,
            )
        ],
        hypotheses_to_add=[
            HypothesisToAdd(
                statement="pgbouncer session mode pins a server connection",
                category=HypothesisCategory.DATABASE,
                likelihood=0.4,
                rationale="r",
                root_node_ref="new_index_0",
            )
        ],
    )
    assert a.node_id in deepened_past_root_ids(case)
    (row, _gone), _meta = await _recheck(eng, case, 12, a, gone=False)
    window = fix_windows(case)[-1]
    assert window.fix_turn == 10
    assert row in window.cause_rows and window.state == FIX_FAILED


async def test_a_fix_licensed_on_a_root_run_as_it_is_deepened_past_keeps_its_kind():
    case, a, hyp = _identified_case()
    eng = _engine()
    await _record(eng, case, 6, solutions_to_add=[_fix("raise max_connections to 100")])
    pending = case.proposed_actions[-1]
    assert pending.licence_ids == [a.node_id]
    await _record(
        eng,
        case,
        7,
        _obs(_CAUSAL, "pgbouncer.ini pool_mode=session"),
        _obs(_CAUSAL, "pgbouncer holds a server conn per client"),
        causal_nodes_to_add=[
            CausalNodeToAdd(
                statement="pgbouncer runs in session mode",
                node_type="root",
                produces=a.node_id,
            )
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
                hypothesis_id=hyp.hypothesis_id, root_node_ref="new_index_0"
            )
        ],
        milestones=_ran(pending.action_id),
    )
    assert a.node_id in deepened_past_root_ids(case)
    assert (pending.fix_kind, pending.target_ids) == (FIX_REMEDIATION, [a.node_id])


async def test_a_fix_named_at_a_root_run_after_it_was_deepened_past_is_an_interception():
    """Declared change: the licence's own roots read position. A fix named
    at A, run once the chain was deepened past A, acts on a rung."""
    case, a, hyp = _identified_case()
    eng = _engine()
    await _record(
        eng,
        case,
        6,
        solutions_to_add=[_fix("raise max_connections to 100", a.node_id)],
    )
    pending = case.proposed_actions[-1]
    assert pending.licence_ids == [a.node_id]
    await _deepen(eng, case, hyp, 7)
    await _record(eng, case, 8, milestones=_ran(pending.action_id))
    assert (pending.fix_kind, pending.target_ids) == (FIX_INTERCEPTION, [a.node_id])
