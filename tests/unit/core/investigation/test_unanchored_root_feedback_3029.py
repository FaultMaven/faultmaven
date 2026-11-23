"""A ``root_node_ref`` the engine does not attach, and a root verdict it drops,
is told to the model on that turn, one line per hypothesis (#3029).

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

_DROPPED = "Its root verdict was not recorded; send it again with the root it is about."
_POINT = "Point root_node_ref at a ROOT node."
# A cause the emitted chain does not restate, so the orphan pass leaves it flat.
_OTHER_CAUSE = "a firewall rule drops the issuer's traffic to Vault"


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


# A root restating ``_CAUSE`` -> an intermediate rung -> D.
_CHAIN = [
    CausalNodeToAdd(statement=_CAUSE, node_type="root", produces="new_index_1"),
    CausalNodeToAdd(
        statement="the issuer cannot log in to Vault",
        node_type="intermediate",
        produces="D",
    ),
]


def _new_hypothesis(ref, *, statement=_OTHER_CAUSE, with_verdict=False, update=None):
    """A new hypothesis stating ``statement`` whose add item names ``ref``
    (``"D"`` names the problem node's own id), beside ``_CHAIN``."""
    case = emission_case()
    case.hypotheses = {}
    if ref == "D":
        ref = seed_problem_node(case).node_id
    add = HypothesisToAdd(
        statement=statement,
        category=HypothesisCategory.CONFIG,
        likelihood=0.6,
        rationale="r",
        root_node_ref=ref,
        **(_verdict(case) if with_verdict else {}),
    )
    notices = _turn(
        case,
        hypotheses_to_add=[add],
        hypotheses_to_update=update,
        causal_nodes_to_add=_CHAIN,
    )
    ((hyp_id, hyp),) = case.hypotheses.items()
    return case, hyp_id, hyp, ref, notices


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
    _case, hyp_id, hyp, ref, notices = _new_hypothesis(ref)
    assert hyp.root_node_id is None
    assert _lines_about(notices, hyp_id) == [
        f"Hypothesis {hyp_id} was NOT anchored: its root_node_ref '{ref}' {why}. "
        + _POINT
    ]


def test_a_verdict_sent_with_a_ref_that_does_not_resolve_joins_its_line():
    _case, hyp_id, hyp, _ref, notices = _new_hypothesis(
        "new_index_9", with_verdict=True
    )
    assert hyp.root_node_id is None
    assert _lines_about(notices, hyp_id) == [
        f"Hypothesis {hyp_id} was NOT anchored: its root_node_ref 'new_index_9' "
        f"names no node emitted this turn. {_POINT} {_DROPPED}"
    ]


def test_a_hypothesis_the_orphan_pass_anchors_is_told_where():
    """The ref fails, but the emitted root restates the hypothesis, so the
    orphan pass anchors it there in the same turn: the line says so."""
    case, hyp_id, hyp, _ref, notices = _new_hypothesis("new_index_9", statement=_CAUSE)
    assert case.causal_nodes[hyp.root_node_id].statement == _CAUSE
    assert _lines_about(notices, hyp_id) == [
        f"Hypothesis {hyp_id} was NOT anchored: its root_node_ref 'new_index_9' "
        "names no node emitted this turn; the engine anchored it to "
        f"{hyp.root_node_id} instead, the emitted root that restates it."
    ]


def test_a_verdict_on_the_root_the_orphan_pass_gives_is_told_so():
    """The add item names the restating root with a verdict; the update item's
    ref (the last entry, so the one read) fails. The orphan pass anchors the
    hypothesis to the root the verdict named, after the verdict was dropped
    (#2536): the line says when it got there, not that it stands elsewhere."""
    update = [
        HypothesisUpdate(hypothesis_id="new_index_0", root_node_ref="new_index_9")
    ]
    case, hyp_id, hyp, _ref, notices = _new_hypothesis(
        "new_index_0", statement=_CAUSE, with_verdict=True, update=update
    )
    root = case.causal_nodes[hyp.root_node_id]
    assert root.statement == _CAUSE
    assert root.root_terminal is None
    assert _lines_about(notices, hyp_id) == [
        f"Hypothesis {hyp_id} was NOT anchored: its root_node_ref 'new_index_9' "
        "names no node emitted this turn; the engine anchored it to "
        f"{root.node_id} instead, the emitted root that restates it. Its root "
        "verdict, sent with 'new_index_0', which the engine anchored it to only "
        "after this turn's refs, was not recorded; send it again with the root "
        "it is about."
    ]


def test_a_rooted_hypothesis_whose_re_root_ref_fails_keeps_its_root():
    case, root, hyp = _open_chain_case()
    update = HypothesisUpdate(hypothesis_id=hyp.hypothesis_id, root_node_ref="cn_gone")
    notices = _turn(case, hypotheses_to_update=[update])
    assert hyp.root_node_id == root.node_id
    assert _lines_about(notices, hyp.hypothesis_id) == [
        f"Hypothesis {hyp.hypothesis_id} was NOT re-rooted: its root_node_ref "
        f"'cn_gone' names no node of the case, so it keeps {root.node_id}. {_POINT}"
    ]


def test_a_verdict_the_orphan_pass_gives_no_root_is_told_dropped():
    """The entry named no root, so the verdict waits for the orphan pass; the
    orphan pass gives the hypothesis none."""
    case = emission_case()
    case.hypotheses = {}
    add = HypothesisToAdd(
        statement=_OTHER_CAUSE,
        category=HypothesisCategory.CONFIG,
        likelihood=0.6,
        rationale="r",
        **_verdict(case),
    )
    notices = _turn(case, hypotheses_to_add=[add], causal_nodes_to_add=_CHAIN)
    ((hyp_id, hyp),) = case.hypotheses.items()
    assert hyp.root_node_id is None
    assert _lines_about(notices, hyp_id) == [
        f"Hypothesis {hyp_id} has no root. {_DROPPED}"
    ]


def test_a_verdict_whose_own_ref_differs_from_the_line_names_its_ref():
    """The add item's ref fails, with the verdict; the update item's ref
    anchors the hypothesis. One line, giving the verdict's own ref."""
    update = [
        HypothesisUpdate(hypothesis_id="new_index_0", root_node_ref="new_index_0")
    ]
    case, hyp_id, hyp, _ref, notices = _new_hypothesis(
        "new_index_9", statement=_CAUSE, with_verdict=True, update=update
    )
    assert case.causal_nodes[hyp.root_node_id].root_terminal is None
    assert _lines_about(notices, hyp_id) == [
        f"Hypothesis {hyp_id} stands on {hyp.root_node_id}. Its root verdict, sent "
        "with 'new_index_9', which names no node emitted this turn, was not "
        "recorded; send it again with the root it is about."
    ]


def _adopt_owned_root(with_verdict: bool):
    """A second hypothesis's update names the root the first one owns
    (fm#1091 refuses it). With a verdict, the owner also sends one citing
    nothing, refused as a code whose token the verdict loop adds after the
    refusal."""
    case, root, owner = _open_chain_case()
    adopter = _second_hyp()
    case.hypotheses[adopter.hypothesis_id] = adopter
    updates = [
        HypothesisUpdate(
            hypothesis_id=adopter.hypothesis_id,
            root_node_ref=root.node_id,
            **(_verdict(case) if with_verdict else {}),
        )
    ]
    if with_verdict:
        updates.append(
            HypothesisUpdate(hypothesis_id=owner.hypothesis_id, root_is_terminal=True)
        )
    notices = _turn(case, hypotheses_to_update=updates)
    assert root.root_terminal is None
    return (
        notices,
        _lines_about(notices, adopter.hypothesis_id),
        (
            f"Hypothesis {adopter.hypothesis_id} was NOT anchored to node "
            f"{root.node_id}: that node is already the chain root of "
            f"{owner.hypothesis_id} ('{owner.statement[:80]}'). One cause = one "
            "chain. Emit a NEW root node stating THIS hypothesis's own cause and "
            "point its root_node_ref at it — or, if the two are the same cause, "
            f"update {owner.hypothesis_id} instead of keeping both."
        ),
    )


@pytest.mark.parametrize("with_verdict", [False, True])
def test_a_refused_ref_says_whether_its_verdict_was_dropped(with_verdict):
    """Without a verdict the fm#1091 line is unchanged; with one, the same
    entry says the verdict went with the ref, still ahead of what the verdict
    loop added after it."""
    notices, lines, refusal = _adopt_owned_root(with_verdict)
    expected = refusal + (f" {_DROPPED}" if with_verdict else "")
    assert lines == [expected]
    assert notices[0] == expected
    assert len(notices) == (2 if with_verdict else 1)


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
    return hyp, old_root, new_root, notices


@pytest.mark.parametrize("with_verdict", [False, True])
def test_a_declined_re_root_is_told(with_verdict):
    """A declined re-root is a root_node_ref the engine did not attach, so it is
    told with or without a verdict."""
    hyp, old_root, new_root, notices = _re_root(
        reaches_d=False, with_verdict=with_verdict
    )
    assert hyp.root_node_id == old_root.node_id
    assert new_root.root_terminal is None
    assert _lines_about(notices, hyp.hypothesis_id) == [
        f"Hypothesis {hyp.hypothesis_id} was NOT re-rooted onto {new_root.node_id}: "
        f"its chain does not reach the problem, so it keeps {old_root.node_id}. "
        "Link that chain to the problem, then send root_node_ref again."
        + (f" {_DROPPED}" if with_verdict else "")
    ]


def test_a_declined_re_root_the_orphan_pass_re_roots_is_told_where():
    """The re-root names an open root (declined); the same batch emits a root
    restating the hypothesis that reaches D, and the orphan pass moves the
    hypothesis onto it: the line names that root, not the old one."""
    case, old_root, hyp = _open_chain_case()
    notices = _turn(
        case,
        causal_nodes_to_add=[
            CausalNodeToAdd(
                statement="nothing rotates the secret_id", node_type="root"
            ),
            CausalNodeToAdd(statement=hyp.statement, node_type="root", produces="D"),
        ],
        hypotheses_to_update=[
            HypothesisUpdate(
                hypothesis_id=hyp.hypothesis_id, root_node_ref="new_index_0"
            )
        ],
    )
    (declined,) = (
        n for n in case.causal_nodes.values() if n.statement.startswith("nothing")
    )
    assert hyp.root_node_id not in (None, old_root.node_id, declined.node_id)
    assert case.causal_nodes[hyp.root_node_id].statement == hyp.statement
    assert _lines_about(notices, hyp.hypothesis_id) == [
        f"Hypothesis {hyp.hypothesis_id} was NOT re-rooted onto {declined.node_id}: "
        f"its chain does not reach the problem; the engine anchored it to "
        f"{hyp.root_node_id} instead, the emitted root that restates it."
    ]


# Pinned: what attaches, records, or is held says nothing new (the whole list).


@pytest.mark.parametrize("with_verdict", [False, True])
def test_a_ref_that_attaches_is_not_told(with_verdict):
    case, _hyp_id, hyp, _ref, notices = _new_hypothesis(
        "new_index_0", statement=_CAUSE, with_verdict=with_verdict
    )
    root = case.causal_nodes[hyp.root_node_id]
    assert root.statement == _CAUSE
    assert root.root_terminal is (True if with_verdict else None)
    assert notices == []


@pytest.mark.parametrize("with_verdict", [False, True])
def test_a_re_root_that_attaches_is_not_told(with_verdict):
    hyp, _old, new_root, notices = _re_root(reaches_d=True, with_verdict=with_verdict)
    assert hyp.root_node_id == new_root.node_id
    assert new_root.root_terminal is (True if with_verdict else None)
    assert notices == []


def test_a_hand_off_is_not_told():
    """The owner deepens onto a new root in the same batch, freeing its old
    root, which the other hypothesis's ref names: both attach, no line."""
    case, root, owner = _open_chain_case()
    adopter = _second_hyp()
    case.hypotheses[adopter.hypothesis_id] = adopter
    notices = _turn(
        case,
        causal_nodes_to_add=[
            CausalNodeToAdd(
                statement="an egress rule was dropped", node_type="root", produces="D"
            )
        ],
        hypotheses_to_update=[
            HypothesisUpdate(
                hypothesis_id=adopter.hypothesis_id, root_node_ref=root.node_id
            ),
            HypothesisUpdate(
                hypothesis_id=owner.hypothesis_id, root_node_ref="new_index_0"
            ),
        ],
    )
    assert adopter.root_node_id == root.node_id
    assert owner.root_node_id not in (None, root.node_id)
    assert notices == []


def test_a_staged_turn_says_nothing_about_its_refs():
    """While a revision waits the refs and verdicts are refused whole, with
    ``refuse_cause_work``'s own line, so that line is the turn's only notice."""
    case, _root, hyp = _open_chain_case()
    case.progress.problem_status = ProblemStatus.REVISION_PENDING
    update = HypothesisUpdate(
        hypothesis_id=hyp.hypothesis_id, root_node_ref="new_index_9", **_verdict(case)
    )
    (only,) = _turn(case, hypotheses_to_update=[update])
    assert only.startswith(
        "CAUSE WORK NOT ACCEPTED: 1 hypothesis root refs, 1 root verdicts"
    )
