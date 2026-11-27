"""The failed-fix notice states the reason the one decider holds (#3120,
#3053 S3d-7).

A registered fix named again that no longer stands gets
``FAILED_FIX_NOT_REREGISTERED``. Its clause used to say "its fix window failed
or a later fix followed it" for every case, though an interception has no
window and stops standing when the problem is seen again after its relief, in
the relief's turn, or with no relief (#3117). The clause is now the reason
``fix_standing``, the verdict ``standing_fix_action`` keeps a fix by, holds
for the bearer, or ``no_identity`` for a blank description, which
``standing_fix_action`` keeps by no identity; one notice per fix in a turn.

``standing_fix_action`` itself is unchanged: ``standing_fix_oracle_3120.json``
holds what it returned for each shape on ``main`` at d08615584, captured by
``capture_main`` there, never typed.
"""

import json
from pathlib import Path

import pytest

from faultmaven.core.investigation.cause_assurance import interception_relief
from faultmaven.core.investigation.milestone_engine import stage_gates
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    build_fix_verification_block,
)
from tests.unit.core.investigation.bound_rows import VERIFYING_ID
from tests.unit.core.investigation.narrated import narrated
from tests.unit.core.investigation.recorded_turns import _record
from tests.unit.core.investigation.test_fix_targets_2036 import (
    _fix,
    _intermediate,
    _ran,
)
from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
    _GONE,
    _PRESENT,
    _obs,
)
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _ROOT_ID,
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _engine
from tests.unit.core.investigation.test_solution_attempts_2105 import (
    _FIRST,
    _relapsed,
)

pytestmark = pytest.mark.unit

ORACLE = Path(__file__).with_name("standing_fix_oracle_3120.json")
_ROOT_FIX, _OTHER_FIX, _RESTART = "raise max_connections", "cap the pool", "restart"


async def _run(eng, case, turn, description, node_ref=None, accept=True):
    fields = {"milestones": _ran()} if accept else {}
    await _record(
        eng, case, turn, solutions_to_add=[_fix(description, node_ref)], **fields
    )


async def _seen(eng, case, turn, *kinds):
    await _record(eng, case, turn, *(_obs(k, "checkout", VERIFYING_ID) for k in kinds))


async def _window_failed():
    _eng, case = await _relapsed()
    return case, _FIRST


async def _root_fix(*later):
    eng = _engine()
    case, _root, _hyp = _identified_case()
    await _run(eng, case, 6, _ROOT_FIX, _ROOT_ID)
    if later:
        await _run(eng, case, 8, _OTHER_FIX, _ROOT_ID)
    return case, _ROOT_FIX


async def _interception(*turns, fix_after=False):
    """An interception at 6; each of ``turns`` the kinds seen in turn 7, 8…"""
    eng = _engine()
    case, _root, _hyp = _identified_case()
    await _run(eng, case, 6, _RESTART, _intermediate(case))
    for turn, kinds in enumerate(turns, start=7):
        await _seen(eng, case, turn, *kinds)
    if fix_after:
        await _run(eng, case, 9, _ROOT_FIX, _ROOT_ID)
    return case, _RESTART


async def _undated():
    case, description = await _root_fix()
    _bearer(case, description).accepted_in_turn = None
    return case, description


async def _pending_offer():
    eng = _engine()
    case, _root, _hyp = _identified_case()
    await _run(eng, case, 6, _ROOT_FIX, _ROOT_ID, accept=False)
    return case, _ROOT_FIX


#: name -> (builder, the clause after "is registered, and"; None: it stands).
SHAPES = {
    "window_failed": (_window_failed, "its fix window failed"),
    "later_remediation": (lambda: _root_fix(True), "a later fix followed it"),
    "interception_then_fix": (
        lambda: _interception([_GONE], fix_after=True),
        "a later fix followed it",
    ),
    "relapse": (
        lambda: _interception([_GONE], [_PRESENT]),
        "the problem was seen again at turn 8, after its relief (turn 7)",
    ),
    "no_relief": (
        lambda: _interception([_PRESENT]),
        "the problem was still present after it (turn 7)",
    ),
    "same_turn": (
        lambda: _interception([_GONE, _PRESENT]),
        "the problem was still present after it (turn 7)",
    ),
    "undated": (_undated, "it no longer stands"),
    "standing_remediation": (_root_fix, None),
    "standing_interception": (lambda: _interception([_GONE]), None),
    "pending_offer": (_pending_offer, None),
}


def _bearer(case, description):
    return stage_gates._newest_fix_bearer(case, stage_gates.fix_identity(description))


async def _standing(name):
    case, description = await SHAPES[name][0]()
    kept = stage_gates.standing_fix_action(case, description)
    return None if kept is None else case.proposed_actions.index(kept)


async def capture_main():
    """Write the oracle: run on ``main`` only, before the change."""
    ORACLE.write_text(
        json.dumps({n: await _standing(n) for n in SHAPES}, indent=2) + "\n"
    )


@pytest.mark.parametrize("name", list(SHAPES))
async def test_standing_fix_action_returns_what_main_returned(name):
    assert await _standing(name) == json.loads(ORACLE.read_text())[name]


@pytest.mark.parametrize(
    "name", [n for n, (_build, clause) in SHAPES.items() if clause is not None]
)
async def test_the_notice_states_the_reason_the_decider_holds(name):
    build, clause = SHAPES[name]
    case, description = await build()
    bearer = _bearer(case, description)
    assert bearer.state == "accepted", "premise"
    meta: dict = {}

    stage_gates._note_failed_fix(case, bearer, meta)

    (line,) = narrated(case, meta)
    assert line == (
        "SYSTEM: 'solution_accepted' was not registered again: SOLUTION "
        f"{bearer.action_id} is registered, and {clause}. If the user re-applied "
        "it, re-propose it as a SolutionToAdd and set accepted_action_ref "
        "new_index_N naming it."
    )


async def test_the_premises_of_the_interception_shapes():
    """Each interception shape is the record state its name says."""
    for name, relief in [("relapse", (7, 8)), ("no_relief", (None, 7))]:
        case, _description = await SHAPES[name][0]()
        assert interception_relief(case, 6) == relief, name
    case, _description = await SHAPES["same_turn"][0]()
    assert interception_relief(case, 6) == (7, 7)


async def test_the_attempt_line_reads_no_relief_as_still_present():
    """The block's line words one record as the notice does (#3120): an
    interception present with no relief is still present, never seen again."""
    case, _description = await SHAPES["no_relief"][0]()
    (line,) = [
        ln
        for ln in build_fix_verification_block(case).splitlines()
        if "executed turn" in ln
    ]
    assert line.endswith("(cannot confirm the cause): problem still present at turn 7")


async def test_a_carried_and_a_live_decision_on_one_fix_render_one_line():
    """A replayed bundle decides the relapse; the live apply, after a later
    fix, decides ``later_fix`` into its own metadata, which the turn folds in
    as ``turn_application`` does. The fix gets one notice, the later reason."""
    eng = _engine()
    case, _description = await SHAPES["relapse"][0]()
    bearer = _bearer(case, _RESTART)
    meta: dict = {}
    stage_gates._bearer_registers(case, bearer, meta, carried=True, licensed=True)
    await _run(eng, case, 9, _ROOT_FIX, _ROOT_ID)
    live: dict = {}
    stage_gates._bearer_registers(case, bearer, live, carried=False)

    stage_gates.merge_system_notices(meta, live)
    stage_gates.merge_decisions(meta, live)

    (line,) = narrated(case, meta)
    assert "is registered, and a later fix followed it." in line


@pytest.mark.parametrize("blank", ["", "  \t"], ids=["empty", "whitespace"])
async def test_a_blank_description_is_told_it_keeps_nothing(blank):
    """A blank identity keeps nothing (``standing_fix_action``), so the bearer
    is told to re-propose, as on ``main``, though its own verdict stands."""
    case, description = await _root_fix()
    bearer = _bearer(case, description)
    bearer.description = blank
    assert stage_gates.fix_standing(case, bearer) == (None, {}), "premise"
    meta: dict = {}

    stage_gates._note_failed_fix(case, bearer, meta)

    (line,) = narrated(case, meta)
    assert "is registered, and it has no description it can be kept by." in line
