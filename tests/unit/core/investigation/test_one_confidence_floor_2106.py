"""One confidence floor for every destructive outcome (#2106, design D4 + 3(a)).

M6's evidence arm and the conclusion retraction used to weigh the hypothesis's
OWN links at any confidence, so one self-hedged REFUTES against no direct
support demoted a cause whose support sat on its root node. They now weigh the
hypothesis and its chain, rows on record and not the engine's own, counting a
REFUTES only when declared confident, with strict ``refutes > supports``. Node
state follows: a hedged REFUTES is bearing, never a refute. The model's
explicit REFUTED on one confident row (INV-47) is unchanged.
"""

import pytest

from faultmaven.core.investigation.causal_graph.derivation import derive_node_states
from faultmaven.core.investigation.schemas import (
    HypothesisEvidenceLinkToAdd,
    HypothesisUpdate,
    NodeEvidenceLinkToAdd,
)
from faultmaven.modules.case.contracts import (
    CauseState,
    EvidenceCategory,
    EvidenceStance,
    HypothesisState,
    NodeState,
    NodeType,
)
from tests.unit.core.investigation.fix_turns_2039 import (
    _PRESENT,
    _assert_standing,
    _ev,
    _fixed_and_relieved,
    _turn,
)
from tests.unit.core.investigation.test_derive_node_states import (
    _case,
    _evidence,
    _link,
    _nid,
    _node,
)

pytestmark = pytest.mark.unit

_HEDGE = 0.3
_PROBES = (
    "curl to checkout returned 503 because the probe omitted the cookie",
    "a synthetic check against checkout timed out from the bastion host",
    "one checkout pod logged a 503 during its own rolling restart",
)


def _hyp_refutes(hyp, count, confidence=None):
    rows = [_ev(_PRESENT, probe) for probe in _PROBES[:count]]
    links = [
        HypothesisEvidenceLinkToAdd(
            hypothesis_id_ref=hyp.hypothesis_id,
            evidence_id_ref=f"new_index_{i}",
            stance=EvidenceStance.REFUTES,
            reasoning="the fix may not have held",
            **({} if confidence is None else {"stance_confidence": confidence}),
        )
        for i in range(count)
    ]
    return rows, links


def _root_refutes(root, count, confidence):
    rows = [_ev(_PRESENT, probe) for probe in _PROBES[:count]]
    links = [
        NodeEvidenceLinkToAdd(
            node_ref=root.node_id,
            evidence_id_ref=f"new_index_{i}",
            stance=EvidenceStance.REFUTES,
            reasoning="the fix may not have held",
            stance_confidence=confidence,
        )
        for i in range(count)
    ]
    return rows, links


def _assert_demoted(case, root, hyp):
    assert hyp.state == HypothesisState.REFUTED
    assert root.node_state == NodeState.REFUTED
    assert case.progress.cause_state != CauseState.IDENTIFIED
    assert case.root_cause_conclusion is None


# ---------------------------------------------------------------------------
# M6's evidence arm and the conclusion retraction.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hedges", [1, 3], ids=["the-2106-repro", "outnumbering"])
async def test_hedged_refutes_on_the_hypothesis_do_not_demote_the_cause(hedges):
    """#2106: the support is on the root, none on the hypothesis. Hedges do not
    count, however many: three hedged rows against two supports still stand."""
    eng, case, root, hyp = await _fixed_and_relieved()
    rows, links = _hyp_refutes(hyp, hedges, confidence=_HEDGE)

    await _turn(eng, case, 13, *rows, hypothesis_evidence_links=links)

    assert [link.stance_confidence for link in hyp.evidence_links] == [_HEDGE] * hedges
    _assert_standing(case, root, hyp)


async def test_control_the_hedged_link_on_the_root_alone_does_not_demote():
    """#2106's control: the same link on the root node only."""
    eng, case, root, hyp = await _fixed_and_relieved()
    rows, links = _root_refutes(root, 1, _HEDGE)

    await _turn(eng, case, 13, *rows, node_evidence_links=links)

    assert hyp.evidence_links == []
    _assert_standing(case, root, hyp)


async def test_one_unhedged_refute_is_weighed_against_the_chain_but_backs_a_refutation():
    """A default-confidence REFUTES on the hypothesis does not outweigh the two
    supports on its root. The same row still backs the model's explicit
    REFUTED (INV-47), which demotes."""
    eng, case, root, hyp = await _fixed_and_relieved()
    rows, links = _hyp_refutes(hyp, 1)

    await _turn(eng, case, 13, *rows, hypothesis_evidence_links=links)
    _assert_standing(case, root, hyp)

    refute = HypothesisUpdate(
        hypothesis_id=hyp.hypothesis_id,
        state=HypothesisState.REFUTED,
        refutation_reason="the probe shows checkout failing with the limit raised",
    )
    await _turn(eng, case, 14, hypotheses_to_update=[refute])
    _assert_demoted(case, root, hyp)


async def test_a_row_supporting_the_hypothesis_and_its_root_counts_once():
    """F9: rows, not links. ``pool_log`` already supports the root; linked to
    the hypothesis too it is still one of two supporting rows, so three
    confident refutes outnumber the support (as links, 3 against 3 would not)."""
    eng, case, root, hyp = await _fixed_and_relieved()
    rows, links = _hyp_refutes(hyp, 3, confidence=0.9)
    pool_log = root.evidence_links[0].evidence_id
    links.append(
        HypothesisEvidenceLinkToAdd(
            hypothesis_id_ref=hyp.hypothesis_id,
            evidence_id_ref=pool_log,
            stance=EvidenceStance.SUPPORTS,
            reasoning="the connection limit log",
        )
    )

    await _turn(eng, case, 13, *rows, hypothesis_evidence_links=links)

    assert pool_log in {link.evidence_id for link in hyp.evidence_links}
    _assert_demoted(case, root, hyp)


async def test_control_confident_refutes_outnumbering_the_support_demote():
    eng, case, root, hyp = await _fixed_and_relieved()
    rows, links = _hyp_refutes(hyp, 3, confidence=0.9)

    await _turn(eng, case, 13, *rows, hypothesis_evidence_links=links)

    _assert_demoted(case, root, hyp)


# ---------------------------------------------------------------------------
# Node state (3(a)): a hedged REFUTES is bearing, never a refute.
# ---------------------------------------------------------------------------


async def test_hedged_refutes_on_a_validated_root_leave_it_validated():
    eng, case, root, hyp = await _fixed_and_relieved()
    rows, links = _root_refutes(root, 2, _HEDGE)

    await _turn(eng, case, 13, *rows, node_evidence_links=links)

    _assert_standing(case, root, hyp)


async def test_control_confident_refutes_tying_the_root_hold_it_at_candidates():
    eng, case, root, hyp = await _fixed_and_relieved()
    rows, links = _root_refutes(root, 2, 0.9)

    await _turn(eng, case, 13, *rows, node_evidence_links=links)

    assert root.node_state == NodeState.INCONCLUSIVE
    assert case.progress.cause_state == CauseState.CANDIDATES
    assert hyp.state != HypothesisState.REFUTED


@pytest.mark.parametrize(
    "confidence, state",
    [(_HEDGE, NodeState.VALIDATED), (0.9, NodeState.INCONCLUSIVE)],
    ids=["hedged", "control-confident"],
)
def test_hedged_refutes_do_not_hold_a_grounded_root_out_of_validated(confidence, state):
    """Entry side: two independent causal supports ground the root; two
    REFUTES tie it only when they are declared confident."""
    rows = [
        _evidence(label, EvidenceCategory.CAUSAL_EVIDENCE)
        for label in ("pool_log", "pool_metric", "probe_a", "probe_b")
    ]
    root = _node(
        _nid(0x2106),
        node_type=NodeType.ROOT,
        links=[
            _link("pool_log", EvidenceStance.SUPPORTS),
            _link("pool_metric", EvidenceStance.SUPPORTS),
            _link("probe_a", EvidenceStance.REFUTES, stance_confidence=confidence),
            _link("probe_b", EvidenceStance.REFUTES, stance_confidence=confidence),
        ],
    )
    case = _case([root], evidence=rows)

    derive_node_states(case)

    assert root.node_state == state
