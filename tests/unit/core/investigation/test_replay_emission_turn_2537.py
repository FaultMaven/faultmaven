"""#2537: cause work replayed from a staged bundle is dated at the bundle's
turn, so the rows recorded with it count for every floor that reads a root's
attach or a replayed hypothesis's ``generated_at_turn``.

A revision turn (4) stages the cause work, and the user's "yes" at 5 replays
it through the real ``_apply_investigation_updates`` (``confirm_revision``).
No causal_absence row can be recorded while the revision waits (it is read as
symptom_absence), so the cause row recorded with the cause is one whose data
the user submitted on the staged turn: the floors compare ``observed_turn``.
No live LLM.
"""

import pytest

from faultmaven.core.investigation.cause_assurance import (
    cause_recheck_status,
    fix_windows,
)
from faultmaven.core.investigation.milestone_engine.statement_revision import (
    confirm_revision,
)
from faultmaven.core.investigation.schemas import EvidenceToAdd
from faultmaven.modules.case.contracts import (
    AnswerVia,
    EvidenceCategory,
    EvidenceSourceType,
    ProblemStatus,
)
from tests.unit.core.investigation import (
    test_problem_statement_revision_and_false_alarm as revision,
)
from tests.unit.core.investigation.bound_rows import upload

pytestmark = pytest.mark.unit

_STAGED = 4  # the revision helper's case is at turn 4
_CAUSE_GONE = EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE


def _obs(category, summary, rechecks, file_id) -> EvidenceToAdd:
    return EvidenceToAdd(
        summary=summary,
        extract=summary,
        category=category,
        source_type=EvidenceSourceType.LOGS,
        source_file_id=file_id,
        rechecks=rechecks,
    )


async def _record(engine, case, turn: int, *rows) -> None:
    case.current_turn = turn
    await revision._apply(engine, case, revision._DSU(evidence_to_add=list(rows)))


async def _replayed(data_turn: int = _STAGED):
    """The cause, its chain and its rows staged at 4 from data uploaded at
    ``data_turn``, replayed on the "yes" at 5. Returns the engine, the case,
    the hypothesis, its root, the staged rows' ids (the symptom row, then the
    root's two supports) and the file."""
    engine, case = revision._engine(), revision._case()
    file_id = upload(case, data_turn)
    dsu = revision._revision_turn_with_cause_work()
    for row in dsu.evidence_to_add:
        row.source_file_id = file_id
    meta = await revision._apply(engine, case, dsu)
    assert case.hypotheses == {}, "premise: staged"
    case.current_turn = _STAGED + 1
    await confirm_revision(engine.responses, case, {}, via=AnswerVia.CLICK)
    (hyp,) = case.hypotheses.values()
    root = case.causal_nodes[hyp.root_node_id]
    return engine, case, hyp, root, meta["evidence_slots"], file_id


@pytest.mark.parametrize(
    "data_turn, why",
    [
        (_STAGED, None),
        (_STAGED - 1, f"its data predates the cause's positing (turn {_STAGED})"),
    ],
)
async def test_a_replayed_cause_is_posited_in_its_own_turn(data_turn, why):
    """The root's attach, the hypothesis and the nodes read the staged turn,
    so the cause leg counts a re-check of the cause from that turn's data. A
    row from data submitted before the cause still predates it."""
    engine, case, hyp, root, (_, support, _), file_id = await _replayed(data_turn)

    assert root.metadata["root_attached_turn"] == _STAGED
    assert root.metadata["root_first_attached_turn"] == _STAGED
    assert hyp.generated_at_turn == root.generated_at_turn == _STAGED
    # Its progress stamps are the confirmation's: it could not be worked
    # while the revision waited.
    assert hyp.last_progress_at_turn == hyp.last_updated_turn == _STAGED + 1

    await _record(
        engine,
        case,
        _STAGED + 1,
        _obs(_CAUSE_GONE, "the orders pool reads 50 connections", support, file_id),
    )

    ((row, reason),) = cause_recheck_status(case)
    assert row.category == _CAUSE_GONE
    assert reason == why


async def test_a_relapse_on_a_replayed_cause_fails_window_zero():
    """The replayed cause re-checked gone on the staged turn's data, the
    problem gone at 5, then back at 6: window 0's relapse, floored at the
    cause's first attach, the staged turn."""
    engine, case, _hyp, _root, (shown, support, _), file_id = await _replayed()

    await _record(
        engine,
        case,
        _STAGED + 1,
        _obs(_CAUSE_GONE, "the orders pool reads 50 connections", support, file_id),
        _obs(
            EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
            "orders answer in 80 ms",
            shown,
            upload(case, _STAGED + 1),
        ),
    )
    await _record(
        engine,
        case,
        _STAGED + 2,
        _obs(
            EvidenceCategory.SYMPTOM_EVIDENCE,
            "orders time out after 30s again",
            shown,
            upload(case, _STAGED + 2),
        ),
    )

    window = fix_windows(case)[0]
    assert (window.state, window.relief_turn, window.failure_turn) == (
        "failed",
        _STAGED + 1,
        _STAGED + 2,
    )


async def test_live_cause_work_is_dated_at_the_current_turn():
    """Outside a replay nothing moves: the same work applied live at 7."""
    engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
    case.current_turn = 7
    dsu = revision._revision_turn_with_cause_work()
    dsu.verification_updates = None
    await revision._apply(engine, case, dsu)

    (hyp,) = case.hypotheses.values()
    root = case.causal_nodes[hyp.root_node_id]
    assert root.metadata["root_attached_turn"] == 7
    assert root.metadata["root_first_attached_turn"] == 7
    assert hyp.generated_at_turn == root.generated_at_turn == 7
