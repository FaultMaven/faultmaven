"""#2060: a root a chain was deepened past is held to the rung bar it is shown as.

Deepening keeps the old root's stored ``NodeType.ROOT`` (#2036), and the
causal-graph block already shows it as ``intermediate``. The ruling (round 34,
option (b)): its position on the hypothesis path sets the bar, so it validates
on one causal support, carries no ROOT recovery note and is outside the
restatement guard and the ROOT block metric. One predicate decides it,
``deepened_past_root_ids``. A node some non-retired hypothesis is rooted at,
or one on no non-retired path, keeps the ROOT bar.
"""

import hashlib
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from faultmaven.core.investigation.causal_graph.derivation import derive_node_states
from faultmaven.core.investigation.causal_graph.support import (
    BLOCK_REASON_COUNT,
    deepened_past_root_ids,
    restatement_held_root_ids,
    root_support_block_reasons,
)
from faultmaven.core.investigation.prompts.context_builder.causal_graph_block import (
    _build_causal_graph_block,
)
from faultmaven.modules.case.contracts import (
    CausalEdge,
    CausalNode,
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

pytestmark = pytest.mark.unit

_PROBLEM = "checkout requests fail with 502 under load"
_D, _R, _X = "cn_0000000000d0", "cn_0000000000a1", "cn_0000000000e1"
_COUNTER = (
    "faultmaven.core.investigation.causal_graph.derivation."
    "root_validation_blocked_support_count_total"
)


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


def _link(label: str, confidence: float = 1.0) -> NodeEvidenceLink:
    return NodeEvidenceLink(
        evidence_id=_eid(label),
        stance=EvidenceStance.SUPPORTS,
        reasoning="bears on the rung",
        linked_at_turn=2,
        stance_confidence=confidence,
    )


def _node(node_id, statement, node_type, links=()) -> CausalNode:
    return CausalNode(
        node_id=node_id,
        statement=statement,
        node_type=node_type,
        node_state=NodeState.CANDIDATE,
        validation_method=ValidationMethod.NONE,
        belief=0.5,
        actionable=node_type == NodeType.ROOT,
        evidence_links=list(links),
        generated_at_turn=1,
    )


def _hyp(hyp_id, root, path, state=HypothesisState.ACTIVE) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hyp_id,
        statement=f"chain {hyp_id} explains the failing checkout",
        category=HypothesisCategory.DATABASE,
        state=state,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="posited",
        root_node_id=root,
        path=path,
        generated_at_turn=1,
    )


def _case(
    *,
    x_statement="pool connections leak until the pool is exhausted",
    x_links=(("x1", 1.0),),
    deepened=True,
    rooted_at_x=False,
    retired=False,
):
    """The issue's fixture: a hypothesis rooted at R (two independent causal
    supports) with ``path = [R, X, D]``, X ROOT-typed. ``deepened=False``
    leaves X on no path (an orphan); ``rooted_at_x`` adds a second hypothesis
    rooted at X (a re-root); ``retired`` retires the hypothesis rooted at R."""
    d = _node(_D, _PROBLEM, NodeType.PROBLEM)
    r = _node(
        _R,
        "a deploy halved the database max_connections setting",
        NodeType.ROOT,
        [_link("r1"), _link("r2")],
    )
    x = _node(_X, x_statement, NodeType.ROOT, [_link(lbl, c) for lbl, c in x_links])
    labels = ["r1", "r2"] + [lbl for lbl, _ in x_links]
    hyps = []
    if deepened:
        state = HypothesisState.RETIRED if retired else HypothesisState.ACTIVE
        hyps.append(_hyp("hyp_000000000001", _R, [_R, _X, _D], state))
    else:
        hyps.append(_hyp("hyp_000000000001", _R, [_R, _D]))
    if rooted_at_x:
        hyps.append(_hyp("hyp_000000000002", _X, [_X, _D]))
    case = investigating_case(
        case_id="case_000000000001",
        current_turn=5,
        statement=_PROBLEM,
        symptom=_PROBLEM,
        causal_nodes={n.node_id: n for n in (d, r, x)},
        causal_edges=[
            CausalEdge(cause_node_id=_R, effect_node_id=_X),
            CausalEdge(cause_node_id=_X, effect_node_id=_D),
        ],
        evidence=[_evidence(lbl) for lbl in labels],
        hypotheses={h.hypothesis_id: h for h in hyps},
    )
    return case, case.causal_nodes[_X]


def _line(block: str, node_id: str) -> str:
    return next(line for line in block.splitlines() if node_id in line)


def _one(label="x1"):
    return ((label, 1.0),)


def test_a_root_the_chain_was_deepened_past_validates_on_the_rung_bar():
    """The regression: one causal support validates X, and the block shows it
    as a validated rung with no request for a second observation."""
    case, x = _case(x_links=_one())
    assert x.node_type == NodeType.ROOT  # storage keeps the type (#2036)
    assert deepened_past_root_ids(case) == {_X}

    derive_node_states(case)

    assert x.node_state == NodeState.VALIDATED
    assert x.validation_method == ValidationMethod.EMPIRICAL
    assert _X not in root_support_block_reasons(case)
    line = _line(_build_causal_graph_block(case), _X)
    assert "[intermediate/validated]" in line
    assert "SECOND INDEPENDENT" not in line


def test_a_re_rooted_node_keeps_the_root_bar():
    """X is also another live hypothesis's root: it is a root again."""
    case, x = _case(x_links=_one(), rooted_at_x=True)
    assert deepened_past_root_ids(case) == set()

    derive_node_states(case)

    assert x.node_state == NodeState.INCONCLUSIVE
    assert root_support_block_reasons(case) == {_X: BLOCK_REASON_COUNT}
    block = _build_causal_graph_block(case)
    assert all("[root/inconclusive]" in ln for ln in block.splitlines() if _X in ln)


def test_an_orphan_root_keeps_the_root_bar():
    case, x = _case(x_links=_one(), deepened=False)
    assert deepened_past_root_ids(case) == set()

    derive_node_states(case)

    assert x.node_state == NodeState.INCONCLUSIVE
    assert root_support_block_reasons(case) == {_X: BLOCK_REASON_COUNT}
    line = _line(_build_causal_graph_block(case), _X)
    assert "[root/inconclusive]" in line
    assert "SECOND INDEPENDENT" in line


def test_a_root_on_a_retired_path_only_keeps_the_root_bar():
    case, x = _case(x_links=_one(), retired=True)
    assert deepened_past_root_ids(case) == set()

    derive_node_states(case)

    assert x.node_state == NodeState.INCONCLUSIVE
    assert root_support_block_reasons(case) == {_X: BLOCK_REASON_COUNT}


def test_a_deepened_past_root_is_outside_the_restatement_guard():
    """X restates the problem and clears the ROOT bar. On no path the guard
    holds it (the control); deepened past, it is a rung and validates."""
    restating = {"x_statement": _PROBLEM, "x_links": (("x1", 1.0), ("x2", 1.0))}
    control, cx = _case(deepened=False, **restating)
    derive_node_states(control)
    assert cx.node_state == NodeState.INCONCLUSIVE
    assert restatement_held_root_ids(control) == {_X}

    case, x = _case(**restating)
    derive_node_states(case)
    assert x.node_state == NodeState.VALIDATED
    assert restatement_held_root_ids(case) == set()
    assert "SECOND INDEPENDENT" not in _line(_build_causal_graph_block(case), _X)


def test_a_deepened_past_root_is_not_counted_by_the_root_block_metric():
    """A hedged-only causal link fails the rung bar too, which on a ROOT is a
    ``hedged_only`` block event. Deepened past, X is no ROOT for the metric
    or the classification; on no path it is (the control)."""
    hedged = {"x_links": (("x1", 0.3),)}
    control, cx = _case(deepened=False, **hedged)
    with patch(_COUNTER) as counter:
        derive_node_states(control)
    assert cx.node_state == NodeState.INCONCLUSIVE
    counter.labels.assert_called_once_with(reason="hedged_only")
    assert _X in root_support_block_reasons(control)

    case, x = _case(**hedged)
    with patch(_COUNTER) as counter:
        derive_node_states(case)
    assert x.node_state == NodeState.INCONCLUSIVE
    counter.labels.assert_not_called()
    assert _X not in root_support_block_reasons(case)
