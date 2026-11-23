"""A ``root_node_ref`` the engine does not attach, and a root verdict it drops,
is told to the model on that turn (#3029).

Driven through the real apply path (``_apply_investigation_updates``: the
hypothesis passes, chain emission, the orphan pass), as #3026's verdict tests
are. Each line names the hypothesis and the ref; a ref that attaches and a
verdict that records produce none.
"""

from __future__ import annotations

import asyncio

import pytest

from faultmaven.core.investigation.causal_graph.ingestion import seed_problem_node
from faultmaven.core.investigation.schemas import (
    CausalNodeToAdd,
    HypothesisToAdd,
    HypothesisUpdate,
)
from faultmaven.modules.case.contracts import HypothesisCategory, ProblemStatus
from tests.unit.core.investigation.test_chain_emission_wiring import (
    _case as emission_case,
)
from tests.unit.core.investigation.test_chain_emission_wiring import _second_hyp
from tests.unit.core.investigation.test_m5_recovery_1679 import _meta, _Updates
from tests.unit.core.investigation.test_terminal_root_identification_2036 import (
    _CAUSE,
    _basis_row,
    _engine,
    _open_chain_case,
)

pytestmark = pytest.mark.unit

_VERDICT_DROPPED = (
    "Its root verdict was not recorded; send it again with the root it is about."
)
_POINT = (
    "Point root_node_ref at a ROOT node: a root's cn_ id, or new_index_N of a "
    "root node emitted this turn."
)


def _turn(case, **fields) -> list[str]:
    """One turn through the real apply path; the turn's system notices."""
    metadata = _meta()
    asyncio.run(
        _engine().responses._apply_investigation_updates(
            case, _Updates(**fields), metadata
        )
    )
    return list(metadata.get("system_notices", []))


def _lines_about(notices: list[str], hyp_id: str) -> list[str]:
    return [n for n in notices if n.startswith(f"Hypothesis {hyp_id} ")]


def _verdict(case) -> dict:
    return {
        "root_is_terminal": True,
        "terminal_basis_evidence_ids": [_basis_row(case, case.current_turn)],
    }


# A root -> an intermediate rung -> D, emitted beside each new hypothesis.
_CHAIN = [
    CausalNodeToAdd(statement=_CAUSE, node_type="root", produces="new_index_1"),
    CausalNodeToAdd(
        statement="the issuer cannot log in to Vault",
        node_type="intermediate",
        produces="D",
    ),
]


def _new_hypothesis(ref: str, *, with_verdict: bool = False):
    """A new hypothesis whose add item names ``ref`` (``"D"`` names the
    problem node's own id), beside ``_CHAIN``."""
    case = emission_case()
    case.hypotheses = {}
    if ref == "D":
        ref = seed_problem_node(case).node_id
    add = HypothesisToAdd(
        statement=_CAUSE,
        category=HypothesisCategory.CONFIG,
        likelihood=0.6,
        rationale="r",
        root_node_ref=ref,
        **(_verdict(case) if with_verdict else {}),
    )
    notices = _turn(case, hypotheses_to_add=[add], causal_nodes_to_add=_CHAIN)
    (hyp_id,) = case.hypotheses
    return case, hyp_id, ref, notices


@pytest.mark.parametrize(
    ("ref", "why"),
    [
        ("new_index_9", "names no node emitted this turn"),
        ("new_index_x", "names no node emitted this turn"),
        ("cn_never_emitted", "names no node of the case"),
        ("new_index_1", "names an intermediate node, not a root cause"),
        ("D", "names the problem node D, not a root cause"),
    ],
)
def test_a_ref_that_does_not_resolve_is_told_with_its_reason(ref, why):
    _case, hyp_id, ref, notices = _new_hypothesis(ref)
    assert _lines_about(notices, hyp_id) == [
        f"Hypothesis {hyp_id} was NOT anchored: its root_node_ref '{ref}' "
        f"{why}. {_POINT}"
    ]


def test_a_verdict_sent_with_a_ref_that_does_not_resolve_is_told_dropped():
    case, hyp_id, _ref, notices = _new_hypothesis("new_index_9", with_verdict=True)
    assert _lines_about(notices, hyp_id) == [
        f"Hypothesis {hyp_id} was NOT anchored: its root_node_ref 'new_index_9' "
        f"names no node emitted this turn. {_POINT} {_VERDICT_DROPPED}"
    ]
    assert case.causal_nodes[case.hypotheses[hyp_id].root_node_id].root_terminal is (
        None
    )


def _adopt_owned_root(with_verdict: bool):
    """A second hypothesis's update names the root the first one owns
    (fm#1091 refuses it)."""
    case, root, owner = _open_chain_case()
    adopter = _second_hyp()
    case.hypotheses[adopter.hypothesis_id] = adopter
    update = HypothesisUpdate(
        hypothesis_id=adopter.hypothesis_id,
        root_node_ref=root.node_id,
        **(_verdict(case) if with_verdict else {}),
    )
    notices = _turn(case, hypotheses_to_update=[update])
    assert root.root_terminal is None
    return _lines_about(notices, adopter.hypothesis_id), (
        f"Hypothesis {adopter.hypothesis_id} was NOT anchored to node "
        f"{root.node_id}: that node is already the chain root of "
        f"{owner.hypothesis_id} ('{owner.statement[:80]}'). One cause = one "
        "chain. Emit a NEW root node stating THIS hypothesis's own cause and "
        "point its root_node_ref at it — or, if the two are the same cause, "
        f"update {owner.hypothesis_id} instead of keeping both."
    )


@pytest.mark.parametrize("with_verdict", [False, True])
def test_a_refused_ref_says_whether_its_verdict_was_dropped(with_verdict):
    """Without a verdict the fm#1091 line is unchanged; with one, the line
    says the verdict went with the ref."""
    lines, refusal = _adopt_owned_root(with_verdict)
    assert lines == [refusal + (f" {_VERDICT_DROPPED}" if with_verdict else "")]


def _re_root(*, reaches_d: bool, with_verdict: bool):
    """A rooted hypothesis's update re-roots it onto a new root, whose chain
    reaches D through the old root or reaches nothing (the engine declines)."""
    case, old_root, hyp = _open_chain_case()
    new = CausalNodeToAdd(
        statement="nothing rotates the secret_id",
        node_type="root",
        produces=old_root.node_id if reaches_d else None,
    )
    update = HypothesisUpdate(
        hypothesis_id=hyp.hypothesis_id,
        root_node_ref="new_index_0",
        **(_verdict(case) if with_verdict else {}),
    )
    notices = _turn(case, causal_nodes_to_add=[new], hypotheses_to_update=[update])
    (new_root,) = (
        n for n in case.causal_nodes.values() if n.statement == new.statement
    )
    return case, hyp, old_root, new_root, _lines_about(notices, hyp.hypothesis_id)


@pytest.mark.parametrize("with_verdict", [False, True])
def test_a_declined_re_root_is_told(with_verdict):
    """A declined re-root is a root_node_ref the engine did not
    attach, so it is told with or without a verdict."""
    _case, hyp, old_root, new_root, lines = _re_root(
        reaches_d=False, with_verdict=with_verdict
    )
    assert hyp.root_node_id == old_root.node_id
    assert new_root.root_terminal is None
    assert lines == [
        f"Hypothesis {hyp.hypothesis_id} was NOT re-rooted onto {new_root.node_id}: "
        f"that root's chain does not reach the problem yet, so it keeps "
        f"{old_root.node_id}. Link the chain to the problem, then send "
        "root_node_ref again." + (f" {_VERDICT_DROPPED}" if with_verdict else "")
    ]


def test_a_verdict_whose_ref_another_entry_replaced_is_told_dropped():
    """The add item names a ref that resolves to nothing, with the verdict; the
    update item's ref anchors the hypothesis. The verdict is still dropped
    (#3026), on a line of its own."""
    case = emission_case()
    case.hypotheses = {}
    add = HypothesisToAdd(
        statement=_CAUSE,
        category=HypothesisCategory.CONFIG,
        likelihood=0.6,
        rationale="r",
        root_node_ref="new_index_9",
        **_verdict(case),
    )
    update = HypothesisUpdate(hypothesis_id="new_index_0", root_node_ref="new_index_0")
    notices = _turn(
        case,
        hypotheses_to_add=[add],
        hypotheses_to_update=[update],
        causal_nodes_to_add=_CHAIN,
    )
    ((hyp_id, hyp),) = case.hypotheses.items()
    assert case.causal_nodes[hyp.root_node_id].root_terminal is None
    assert _lines_about(notices, hyp_id) == [
        f"Hypothesis {hyp_id} does not stand on 'new_index_9', the root_node_ref "
        f"its root verdict named. {_VERDICT_DROPPED}"
    ]


# Pinned: what attaches, records, or is held says nothing new.


@pytest.mark.parametrize("with_verdict", [False, True])
def test_a_ref_that_attaches_is_not_told(with_verdict):
    case, hyp_id, _ref, notices = _new_hypothesis(
        "new_index_0", with_verdict=with_verdict
    )
    root = case.causal_nodes[case.hypotheses[hyp_id].root_node_id]
    assert root.statement == _CAUSE
    assert root.root_terminal is (True if with_verdict else None)
    assert _lines_about(notices, hyp_id) == []


@pytest.mark.parametrize("with_verdict", [False, True])
def test_a_re_root_that_attaches_is_not_told(with_verdict):
    _case, hyp, _old, new_root, lines = _re_root(
        reaches_d=True, with_verdict=with_verdict
    )
    assert hyp.root_node_id == new_root.node_id
    assert new_root.root_terminal is (True if with_verdict else None)
    assert lines == []


def test_a_staged_turn_says_nothing_about_its_refs():
    """While a revision waits the refs and verdicts are refused whole, with
    ``refuse_cause_work``'s own line, so none of these lines is sent."""
    case, _root, hyp = _open_chain_case()
    case.progress.problem_status = ProblemStatus.REVISION_PENDING
    update = HypothesisUpdate(
        hypothesis_id=hyp.hypothesis_id, root_node_ref="new_index_9", **_verdict(case)
    )
    assert (
        _lines_about(_turn(case, hypotheses_to_update=[update]), hyp.hypothesis_id)
        == []
    )
