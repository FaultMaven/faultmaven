"""#1141: a root under more than one hold carries ONE recovery note, chosen by
one declared precedence (``root_recovery_reasons``): a MECE contest, then an
open WHY, then the restatement hold, then a grounding-bar reason.

The old code chose by the order of a four-way dict merge, and its comment
called the populations "disjoint by construction". Since #2036 added the open
WHY they are not: why_open overlaps the MECE contest and both the grounding and
the restatement holds. The first block below builds those overlaps from the
suite's own fixtures; the oracle pins that the declared precedence is the
merge's old order; the table pins the order for pairs no fixture can build.
"""

import pytest

from faultmaven.core.investigation.causal_graph.clusters import (
    mece_contested_root_ids,
)
from faultmaven.core.investigation.causal_graph.projection import held_open_root_ids
from faultmaven.core.investigation.causal_graph.support import (
    BLOCK_REASON_COUNT,
    BLOCK_REASON_HEDGED,
    BLOCK_REASON_MIRROR,
    BLOCK_REASON_RESTATEMENT,
    restatement_held_root_ids,
    root_support_block_reasons,
)
from faultmaven.core.investigation.milestone_engine.cause_state import (
    _recompute_cause_state_from_chain,
)
from faultmaven.core.investigation.prompts.context_builder import (
    causal_graph_block as cgb,
)
from tests.unit.core.investigation.test_mece_arbitration import (
    _SPECIMEN,
    _specimen_case,
)

pytestmark = pytest.mark.unit

_WHY_NOTE_FRAGMENT = "its WHY is open"
_MECE_NOTE_FRAGMENT = "MUTUALLY-EXCLUSIVE"
_RESTATING = (
    "cn_00000000000a",
    "hyp_0000000000aa",
    "checkout orders failing with 500s",
    ["ev_a1", "ev_a2"],
)
_ONE_SUPPORT = (
    "cn_00000000000a",
    "hyp_0000000000aa",
    "the deploy removed the connection release call so pool "
    "connections leak until exhaustion",
    ["ev_a1"],
)


def _built(members, *, root_terminal="keep"):
    """A specimen case with every root's terminal verdict set (or popped),
    its derived state recomputed as the engine does."""
    case = _specimen_case(members=members)
    for member in members:
        node = case.causal_nodes[member[0]]
        if root_terminal is None:
            node.metadata.pop("root_terminal", None)
        elif root_terminal != "keep":
            node.metadata["root_terminal"] = root_terminal
    _recompute_cause_state_from_chain(case)
    return case


def _old_merge(case):
    """The four-way merge ``_build_causal_graph_block`` used before #1141,
    copied from main."""
    return {
        **root_support_block_reasons(case),
        **dict.fromkeys(restatement_held_root_ids(case), BLOCK_REASON_RESTATEMENT),
        **dict.fromkeys(held_open_root_ids(case), cgb._WHY_OPEN),
        **dict.fromkeys(mece_contested_root_ids(case), cgb._MECE_CONTESTED),
    }


def _cases():
    return {
        "C+D: contested roots, none terminal": _built(_SPECIMEN, root_terminal=None),
        "C+A: counted root, non-terminal": _built([_ONE_SUPPORT], root_terminal=False),
        "C+B: restating root, non-terminal": _built([_RESTATING], root_terminal=False),
        "A only": _built([_ONE_SUPPORT], root_terminal=True),
        "B only": _built([_RESTATING], root_terminal=True),
        "D only": _built(_SPECIMEN),
        "C only": _built([_SPECIMEN[0]], root_terminal=None),
    }


def _root_line(block, node_id):
    return next(line for line in block.splitlines() if line.strip().startswith(node_id))


# 1. C and D
def test_contest_wins_over_open_why_and_the_why_note_is_not_rendered():
    case = _built(_SPECIMEN, root_terminal=None)
    ids = {m[0] for m in _SPECIMEN}
    assert held_open_root_ids(case) == ids  # the WHY hold stands on all three
    assert mece_contested_root_ids(case) == ids
    assert cgb.root_recovery_reasons(case) == dict.fromkeys(ids, cgb._MECE_CONTESTED)
    block = cgb._build_causal_graph_block(case)
    assert block.count(_MECE_NOTE_FRAGMENT) == 3
    assert block.count(_WHY_NOTE_FRAGMENT) == 0


# 2. C and A
def test_open_why_wins_over_a_count_hold():
    case = _built([_ONE_SUPPORT], root_terminal=False)
    node_id = _ONE_SUPPORT[0]
    assert root_support_block_reasons(case) == {node_id: BLOCK_REASON_COUNT}
    assert node_id in held_open_root_ids(case)
    assert cgb.root_recovery_reasons(case) == {node_id: cgb._WHY_OPEN}
    line = _root_line(cgb._build_causal_graph_block(case), node_id)
    assert line.endswith(cgb._RECOVERY_NOTES[cgb._WHY_OPEN])


# 3. C and B
def test_open_why_wins_over_a_restatement_hold():
    case = _built([_RESTATING], root_terminal=False)
    node_id = _RESTATING[0]
    assert restatement_held_root_ids(case) == {node_id}
    assert node_id in held_open_root_ids(case)
    assert cgb.root_recovery_reasons(case) == {node_id: cgb._WHY_OPEN}
    line = _root_line(cgb._build_causal_graph_block(case), node_id)
    assert line.endswith(cgb._RECOVERY_NOTES[cgb._WHY_OPEN])


# 4. each population alone
def test_each_population_alone_keeps_its_own_note():
    a = _built([_ONE_SUPPORT], root_terminal=True)
    assert cgb.root_recovery_reasons(a) == {_ONE_SUPPORT[0]: BLOCK_REASON_COUNT}
    b = _built([_RESTATING], root_terminal=True)
    assert cgb.root_recovery_reasons(b) == {_RESTATING[0]: BLOCK_REASON_RESTATEMENT}
    d = _built(_SPECIMEN)
    assert cgb.root_recovery_reasons(d) == {
        m[0]: cgb._MECE_CONTESTED for m in _SPECIMEN
    }
    c = _built([_SPECIMEN[0]], root_terminal=None)
    assert cgb.root_recovery_reasons(c) == {_SPECIMEN[0][0]: cgb._WHY_OPEN}


# 5. oracle
@pytest.mark.parametrize("name", list(_cases()))
def test_choice_equals_the_old_merge_order(name):
    case = _cases()[name]
    reasons = cgb.root_recovery_reasons(case)
    assert reasons  # a case with no note would pin nothing
    assert reasons == _old_merge(case)


# 6. precedence table, including pairs no fixture builds yet
_ORDER = [
    cgb._MECE_CONTESTED,
    cgb._WHY_OPEN,
    BLOCK_REASON_RESTATEMENT,
    BLOCK_REASON_COUNT,
]


def test_precedence_is_mece_then_why_then_restatement_then_grounding(monkeypatch):
    pairs = [(i, j) for i in range(4) for j in range(i + 1, 4)]
    ids = {pair: f"cn_pair_{pair[0]}{pair[1]}" for pair in pairs}
    everywhere = "cn_in_all_four"
    members = {k: {everywhere} for k in range(4)}
    for (i, j), node_id in ids.items():
        members[i].add(node_id)
        members[j].add(node_id)
    monkeypatch.setattr(cgb, "mece_contested_root_ids", lambda case: members[0])
    monkeypatch.setattr(cgb, "held_open_root_ids", lambda case: members[1])
    monkeypatch.setattr(cgb, "restatement_held_root_ids", lambda case: members[2])
    monkeypatch.setattr(
        cgb,
        "root_support_block_reasons",
        lambda case: dict.fromkeys(members[3], BLOCK_REASON_COUNT),
    )
    reasons = cgb.root_recovery_reasons(None)
    assert reasons[everywhere] == _ORDER[0]
    for (i, j), node_id in ids.items():
        assert reasons[node_id] == _ORDER[i], (i, j)
    assert len(reasons) == len(ids) + 1


# 7. every reason has a note
@pytest.mark.parametrize(
    "reason",
    [
        cgb._MECE_CONTESTED,
        cgb._WHY_OPEN,
        BLOCK_REASON_RESTATEMENT,
        BLOCK_REASON_COUNT,
        BLOCK_REASON_MIRROR,
        BLOCK_REASON_HEDGED,
    ],
)
def test_every_reason_the_choice_can_return_has_a_note(reason):
    assert reason in cgb._RECOVERY_NOTES
    assert cgb._RECOVERY_NOTES[reason].startswith(" — ")
