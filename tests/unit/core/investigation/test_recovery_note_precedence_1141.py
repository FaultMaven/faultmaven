"""#1141: a root under more than one hold carries ONE recovery note, chosen by
one declared precedence (``_root_recovery_reasons``): a MECE contest, then an
open WHY, then the restatement hold, then a grounding-bar reason.

The old code chose by the order of a four-way dict merge, and its comment
called the populations "disjoint by construction". Since #2036 added the open
WHY they are not: why_open overlaps the MECE contest and both the grounding and
the restatement holds. The first block below builds those overlaps from the
suite's own fixtures; the oracle pins that the declared precedence is the
merge's old order, and that no real case logs a collision; the table pins the
declared pairs' order, and that a root in a pair disjoint by definition is
logged rather than resolved silently.
"""

import logging

import pytest

from faultmaven.core.investigation.causal_graph import support
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

_COLLISION_EVENT = "recovery_note_population_collision"
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
# A second count-held root, on its own node id, to stand beside _RESTATING.
_ONE_SUPPORT_B = (
    "cn_00000000000b",
    "hyp_0000000000bb",
    "connection pool max size undersized for current checkout load",
    ["ev_b1"],
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


# Builders, not cases: collection must run no engine code.
_CASES = {
    "C+D: contested roots, none terminal": lambda: _built(
        _SPECIMEN, root_terminal=None
    ),
    "C+A: counted root, non-terminal": lambda: _built(
        [_ONE_SUPPORT], root_terminal=False
    ),
    "C+B: restating root, non-terminal": lambda: _built(
        [_RESTATING], root_terminal=False
    ),
    "A only": lambda: _built([_ONE_SUPPORT], root_terminal=True),
    "B only": lambda: _built([_RESTATING], root_terminal=True),
    "D only": lambda: _built(_SPECIMEN),
    "C only": lambda: _built([_SPECIMEN[0]], root_terminal=None),
    "A+B: a count-held root beside a restatement-held root": lambda: _built(
        [_ONE_SUPPORT_B, _RESTATING]
    ),
}


def _root_line(block, node_id):
    return next(line for line in block.splitlines() if line.strip().startswith(node_id))


def _collisions(caplog):
    return [r for r in caplog.records if getattr(r, "event", None) == _COLLISION_EVENT]


# 1. C and D
def test_contest_wins_over_open_why_and_the_why_note_is_not_rendered():
    case = _CASES["C+D: contested roots, none terminal"]()
    ids = {m[0] for m in _SPECIMEN}
    assert held_open_root_ids(case) == ids  # the WHY hold stands on all three
    assert mece_contested_root_ids(case) == ids
    assert cgb._root_recovery_reasons(case) == dict.fromkeys(ids, cgb._MECE_CONTESTED)
    block = cgb._build_causal_graph_block(case)
    assert block.count(_MECE_NOTE_FRAGMENT) == 3
    assert block.count(_WHY_NOTE_FRAGMENT) == 0


# 2. C and A
def test_open_why_wins_over_a_count_hold():
    case = _CASES["C+A: counted root, non-terminal"]()
    node_id = _ONE_SUPPORT[0]
    assert root_support_block_reasons(case) == {node_id: BLOCK_REASON_COUNT}
    assert node_id in held_open_root_ids(case)
    assert cgb._root_recovery_reasons(case) == {node_id: cgb._WHY_OPEN}
    line = _root_line(cgb._build_causal_graph_block(case), node_id)
    assert line.endswith(cgb._RECOVERY_NOTES[cgb._WHY_OPEN])


# 3. C and B
def test_open_why_wins_over_a_restatement_hold():
    case = _CASES["C+B: restating root, non-terminal"]()
    node_id = _RESTATING[0]
    assert restatement_held_root_ids(case) == {node_id}
    assert node_id in held_open_root_ids(case)
    assert cgb._root_recovery_reasons(case) == {node_id: cgb._WHY_OPEN}
    line = _root_line(cgb._build_causal_graph_block(case), node_id)
    assert line.endswith(cgb._RECOVERY_NOTES[cgb._WHY_OPEN])


# 4. each population alone
def test_each_population_alone_keeps_its_own_note():
    a = _CASES["A only"]()
    assert cgb._root_recovery_reasons(a) == {_ONE_SUPPORT[0]: BLOCK_REASON_COUNT}
    b = _CASES["B only"]()
    assert cgb._root_recovery_reasons(b) == {_RESTATING[0]: BLOCK_REASON_RESTATEMENT}
    d = _CASES["D only"]()
    assert cgb._root_recovery_reasons(d) == {
        m[0]: cgb._MECE_CONTESTED for m in _SPECIMEN
    }
    c = _CASES["C only"]()
    assert cgb._root_recovery_reasons(c) == {_SPECIMEN[0][0]: cgb._WHY_OPEN}


# 4a. the grounding and restatement holds both non-empty on one case
def test_a_count_hold_and_a_restatement_hold_each_render_on_their_own_root():
    case = _CASES["A+B: a count-held root beside a restatement-held root"]()
    count_id, restating_id = _ONE_SUPPORT_B[0], _RESTATING[0]
    assert root_support_block_reasons(case) == {count_id: BLOCK_REASON_COUNT}
    assert restatement_held_root_ids(case) == {restating_id}
    assert cgb._root_recovery_reasons(case) == {
        count_id: BLOCK_REASON_COUNT,
        restating_id: BLOCK_REASON_RESTATEMENT,
    }
    block = cgb._build_causal_graph_block(case)
    assert _root_line(block, count_id).endswith(cgb._RECOVERY_NOTES[BLOCK_REASON_COUNT])
    assert _root_line(block, restating_id).endswith(
        cgb._RECOVERY_NOTES[BLOCK_REASON_RESTATEMENT]
    )


# 5. oracle, and no real case logs a collision
@pytest.mark.parametrize("name", list(_CASES))
def test_choice_equals_the_old_merge_order_and_logs_no_collision(name, caplog):
    caplog.set_level(logging.ERROR, logger=cgb.logger.name)
    case = _CASES[name]()
    reasons = cgb._root_recovery_reasons(case)
    assert reasons  # a case with no note would pin nothing
    assert reasons == _old_merge(case)
    cgb._build_causal_graph_block(case)  # the path that runs the selector
    assert _collisions(caplog) == []


# 6. precedence table, including pairs no fixture builds yet
# (predicate name, the reason its population carries), highest precedence first
_POPULATIONS = [
    ("mece_contested_root_ids", cgb._MECE_CONTESTED),
    ("held_open_root_ids", cgb._WHY_OPEN),
    ("restatement_held_root_ids", BLOCK_REASON_RESTATEMENT),
    ("root_support_block_reasons", BLOCK_REASON_COUNT),
]
# why_open with mece_contested, with restates_frame, and with a grounding reason
_DECLARED_PAIRS = [(0, 1), (1, 2), (1, 3)]
# disjoint by the predicates' definitions
_IMPOSSIBLE_PAIRS = [(0, 2), (0, 3), (2, 3)]


def _patch_populations(monkeypatch, memberships):
    """Point the four predicates, in the selector's namespace, at the given
    root ids: ``memberships`` maps each root id to the population indices it
    sits in."""
    for k, (name, _reason) in enumerate(_POPULATIONS):
        ids = {root_id for root_id, ks in memberships.items() if k in ks}
        if name == "root_support_block_reasons":
            monkeypatch.setattr(
                cgb, name, lambda case, ids=ids: dict.fromkeys(ids, BLOCK_REASON_COUNT)
            )
        else:
            monkeypatch.setattr(cgb, name, lambda case, ids=ids: set(ids))


def _pair_id(pair):
    return f"cn_pair_{pair[0]}{pair[1]}"


def test_declared_pairs_resolve_by_the_precedence_without_a_log(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=cgb.logger.name)
    _patch_populations(
        monkeypatch, {_pair_id(pair): set(pair) for pair in _DECLARED_PAIRS}
    )
    reasons = cgb._root_recovery_reasons(None)
    assert len(reasons) == len(_DECLARED_PAIRS)
    for i, j in _DECLARED_PAIRS:
        assert reasons[_pair_id((i, j))] == _POPULATIONS[i][1], (i, j)
    assert _collisions(caplog) == []


def test_every_impossible_pair_logs_naming_both_populations(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=cgb.logger.name)
    _patch_populations(
        monkeypatch, {_pair_id(pair): set(pair) for pair in _IMPOSSIBLE_PAIRS}
    )
    reasons = cgb._root_recovery_reasons(None)
    records = _collisions(caplog)
    assert sorted(r.root_id for r in records) == sorted(
        _pair_id(pair) for pair in _IMPOSSIBLE_PAIRS
    )  # one record per root
    for record in records:
        assert record.levelno == logging.ERROR
    by_root = {r.root_id: r for r in records}
    for i, j in _IMPOSSIBLE_PAIRS:
        node_id = _pair_id((i, j))
        assert by_root[node_id].populations == [
            _POPULATIONS[i][0],
            _POPULATIONS[j][0],
        ]
        # still annotated, by one of its two holds: the prompt is built
        assert reasons[node_id] in {_POPULATIONS[i][1], _POPULATIONS[j][1]}


def test_a_root_in_all_four_still_gets_the_contest_note_and_logs_once(
    monkeypatch, caplog
):
    caplog.set_level(logging.ERROR, logger=cgb.logger.name)
    everywhere = "cn_in_all_four"
    _patch_populations(monkeypatch, {everywhere: {0, 1, 2, 3}})
    reasons = cgb._root_recovery_reasons(None)
    assert reasons == {everywhere: cgb._MECE_CONTESTED}
    records = _collisions(caplog)
    assert len(records) == 1
    assert records[0].root_id == everywhere
    assert records[0].populations == [name for name, _ in _POPULATIONS]


# 7. every reason has a note: every BLOCK_REASON_* the support module defines,
# found by inspection so a new one cannot render bare, plus the two holds the
# selector adds
_BLOCK_REASONS = sorted(
    value
    for name, value in vars(support).items()
    if name.startswith("BLOCK_REASON_") and isinstance(value, str)
)


def test_the_block_reason_scan_finds_the_known_reasons():
    assert {
        BLOCK_REASON_COUNT,
        BLOCK_REASON_MIRROR,
        BLOCK_REASON_HEDGED,
        BLOCK_REASON_RESTATEMENT,
    } <= set(_BLOCK_REASONS)


@pytest.mark.parametrize(
    "reason", [*_BLOCK_REASONS, cgb._MECE_CONTESTED, cgb._WHY_OPEN]
)
def test_every_reason_the_choice_can_return_has_a_note(reason):
    assert reason in cgb._RECOVERY_NOTES
    assert cgb._RECOVERY_NOTES[reason].startswith(" — ")
