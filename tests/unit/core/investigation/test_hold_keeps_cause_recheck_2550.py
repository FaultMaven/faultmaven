"""A causal_absence row recorded while a statement revision waits (#2550).

A cause cannot be shown eliminated before its problem is verified, so a
causal_absence row on a turn that ends with the problem unverified is held as
symptom_absence, judged on the status the turn ends with. A revision's hold is
judged again when the user answers it: an answer that leaves the problem
verified restores the hold's rows, and the cause work a confirmation replays
binds them; an answer back to unverified or to a false alarm keeps them held.

Driven through ``_apply_investigation_updates`` and the user's answer
(``confirm_revision``, ``decline_revision``, ``execute_user_closure``). No live
LLM.
"""

import pytest

from faultmaven.core.investigation.cause_assurance import (
    FIX_UNKNOWN,
    SUBJECT_CAUSE,
    SUBJECT_UNBOUND,
    cause_recheck_status,
    fix_windows,
    subject_index,
)
from faultmaven.core.investigation.milestone_engine.statement_revision import (
    confirm_revision,
)
from faultmaven.core.investigation.problem_status import (
    decline_revision,
    invalidate_problem,
    pending_revision,
    verify_problem,
)
from faultmaven.core.investigation.schemas import (
    MilestoneUpdates,
    ProblemVerificationUpdate,
)
from faultmaven.core.investigation.terminal_transitions import (
    assess_resolution_readiness,
    derive_disposition_eligibility,
    execute_user_closure,
)
from faultmaven.modules.case.contracts import (
    AnswerVia,
    ClosureReason,
    EvidenceCategory,
    ProblemStatus,
    ResolveEligibility,
)
from tests.unit.core.investigation import (
    test_problem_statement_revision_and_false_alarm as revision,
)
from tests.unit.core.investigation.bound_rows import (
    bound_to_add,
    ensure_subjects,
    upload,
)

pytestmark = pytest.mark.unit

CAUSAL_ABSENCE = EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE
SYMPTOM_ABSENCE = EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
SYMPTOM = EvidenceCategory.SYMPTOM_EVIDENCE
ANSWER = "if the user's answer to the revised statement leaves the problem verified"
REWORDED = "Checkout requests to /orders time out after 30s at the gateway"


def _recheck(case, *, turn: int, of: str):
    """The model's re-check of the cause row ``of`` (an id or a ``new_index_N``
    ref), on data uploaded at ``turn``: the cause is gone."""
    summary = "the orders pool now reads 50 connections"
    return bound_to_add(case, CAUSAL_ABSENCE, summary, turn=turn, rechecks=of)


async def _revision_turn_with_recheck(engine, case) -> tuple[dict, str]:
    """Turn 4: the revision, the cause work and a re-check of a staged cause
    row, on a file uploaded that turn. Returns the turn's metadata and the
    re-check's id."""
    dsu = revision._revision_turn_with_cause_work()
    dsu.evidence_to_add.append(_recheck(case, turn=4, of="new_index_1"))
    meta = await revision._apply(engine, case, dsu)
    return meta, meta["evidence_added"][-1]


async def _propose(engine, case, *rows, symptom="s1", **verification) -> dict:
    """A revision citing a new symptom row, beside ``rows``; ``verification``
    adds to or overrides the update's fields."""
    fields = dict(
        revised_problem_statement=revision.REVISED,
        revision_evidence_ids=["new_index_0"],
        revision_basis="the gateway log shows timeouts with normal DB latency",
    )
    return await revision._apply(
        engine,
        case,
        revision._DSU(
            evidence_to_add=[revision._row(SYMPTOM, symptom), *rows],
            verification_updates=ProblemVerificationUpdate(
                **{**fields, **verification}
            ),
        ),
    )


async def _hold_turn(engine, case, *rows) -> list[str]:
    """Turn 5, while the revision waits: ``rows`` and nothing else."""
    case.current_turn = 5
    meta = await revision._apply(engine, case, revision._DSU(evidence_to_add=rows))
    return meta["evidence_added"]


async def _confirm(engine, case) -> None:
    case.current_turn += 1
    await confirm_revision(engine.responses, case, {}, via=AnswerVia.CLICK)
    assert case.progress.problem_status == ProblemStatus.VERIFIED


def _row(case, evidence_id: str):
    return next(e for e in case.evidence if e.evidence_id == evidence_id)


def _assert_counts_for_the_cause(case, evidence_id: str) -> None:
    assert _row(case, evidence_id).category == CAUSAL_ABSENCE
    subjects = subject_index(case)
    assert subjects.subject(_row(case, evidence_id), windows=True) == SUBJECT_CAUSE
    assert [(e.evidence_id, why) for e, why in cause_recheck_status(case)] == [
        (evidence_id, None)
    ]
    assert [e.evidence_id for e in fix_windows(case)[0].cause_rows] == [evidence_id]


def _invalidated(prior: ProblemStatus = ProblemStatus.UNVERIFIED):
    case = revision._case(prior)
    finding = revision._with(case, revision._evidence(SYMPTOM_ABSENCE, "a"))
    invalidate_problem(case, evidence_ids=finding, basis="the alert misfired")
    return case


async def _hold_with_both_legs(engine, case) -> str:
    """A verified problem with a cause in the pool; a revision at turn 4; at
    hold turn 5, the cause's support and the problem re-checked gone. Returns
    the cause re-check's id."""
    verifying, support = ensure_subjects(case, turn=3)
    await _propose(engine, case)
    absent, _ = await _hold_turn(
        engine,
        case,
        _recheck(case, turn=5, of=support),
        bound_to_add(case, SYMPTOM_ABSENCE, "checkout ok", turn=5, rechecks=verifying),
    )
    return absent


class TestAConfirmationCountsTheHoldsRecheck:
    async def test_on_the_proposal_turn(self):
        engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
        _, absent = await _revision_turn_with_recheck(engine, case)
        assert _row(case, absent).category == SYMPTOM_ABSENCE

        await _confirm(engine, case)

        _assert_counts_for_the_cause(case, absent)

    @pytest.mark.parametrize(
        "prior", [ProblemStatus.VERIFIED, ProblemStatus.INVALIDATED]
    )
    async def test_on_a_later_hold_turn(self, prior):
        engine = revision._engine()
        case = (
            _invalidated()
            if prior == ProblemStatus.INVALIDATED
            else revision._case(prior)
        )
        await _propose(engine, case)
        assert pending_revision(case).prior_status == prior
        # Hold turn 5 brings the cause work and its re-check. Without the
        # revision's symptom row, the cause rows are new_index_0 and _1.
        dsu = revision._revision_turn_with_cause_work()
        dsu.verification_updates = None
        del dsu.evidence_to_add[0]
        shifted = {"new_index_1": "new_index_0", "new_index_2": "new_index_1"}
        for link in dsu.node_evidence_links:
            link.evidence_id_ref = shifted[link.evidence_id_ref]
        dsu.hypotheses_to_add[0].terminal_basis_evidence_ids = ["new_index_1"]
        case.current_turn = 5
        dsu.evidence_to_add.append(_recheck(case, turn=5, of="new_index_0"))
        absent = (await revision._apply(engine, case, dsu))["evidence_added"][-1]

        await _confirm(engine, case)

        _assert_counts_for_the_cause(case, absent)

    async def test_after_a_revision_on_the_verifying_turn(self):
        engine, case = revision._engine(), revision._case()
        dsu = revision._revision_turn_with_cause_work()
        dsu.milestones = MilestoneUpdates(symptom_verified=True)
        meta = await revision._apply(
            engine, case, dsu, revision._Response("timeouts in the gateway log")
        )
        assert pending_revision(case).prior_status == ProblemStatus.UNVERIFIED
        support = meta["evidence_added"][1]
        (absent,) = await _hold_turn(engine, case, _recheck(case, turn=5, of=support))

        await _confirm(engine, case)

        _assert_counts_for_the_cause(case, absent)


class TestAnAnswerBackToWhereTheCaseWas:
    async def test_to_verified_restores_the_row_unbound(self):
        engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
        _, absent = await _revision_turn_with_recheck(engine, case)

        decline_revision(case, via=AnswerVia.CLICK)

        assert case.progress.problem_status == ProblemStatus.VERIFIED
        assert _row(case, absent).category == CAUSAL_ABSENCE
        assert subject_index(case).subject(_row(case, absent)) == SUBJECT_UNBOUND
        assert cause_recheck_status(case) == []

    async def test_to_unverified_keeps_it_held(self):
        engine, case = revision._engine(), revision._case()
        _, absent = await _revision_turn_with_recheck(engine, case)

        decline_revision(case, via=AnswerVia.CLICK)

        assert case.progress.problem_status == ProblemStatus.UNVERIFIED
        assert _row(case, absent).category == SYMPTOM_ABSENCE

    async def test_to_a_false_alarm_keeps_a_later_hold_turns_row_held(self):
        engine, case = revision._engine(), _invalidated()
        await _propose(engine, case)
        assert pending_revision(case).prior_status == ProblemStatus.INVALIDATED
        (absent,) = await _hold_turn(engine, case, revision._row(CAUSAL_ABSENCE, "g"))

        decline_revision(case, via=AnswerVia.CLICK)

        assert case.progress.problem_status == ProblemStatus.INVALIDATED
        assert _row(case, absent).category == SYMPTOM_ABSENCE


async def test_a_false_alarms_hold_confirmed_restores_a_row_only_turn():
    """A hold turn with no cause work stages nothing, so no replay carries its
    row: only the hold's own restore can, over a false alarm too."""
    engine, case = revision._engine(), _invalidated()
    await _propose(engine, case)
    assert pending_revision(case).prior_status == ProblemStatus.INVALIDATED
    (absent,) = await _hold_turn(engine, case, revision._row(CAUSAL_ABSENCE, "g"))
    assert pending_revision(case).staged == ()

    await _confirm(engine, case)

    assert _row(case, absent).category == CAUSAL_ABSENCE


async def test_readers_during_the_hold_read_the_row_as_held():
    """A re-check of a pool root's support, beside the problem re-checked
    gone: while the statement is in question nothing reads it as the cause
    removed. Confirmed, it is causal_absence."""
    engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
    absent = await _hold_with_both_legs(engine, case)

    assert _row(case, absent).category == SYMPTOM_ABSENCE
    assert cause_recheck_status(case) == []
    readiness = assess_resolution_readiness(case)
    assert readiness.verdict == readiness.NEEDS_INFO
    assert derive_disposition_eligibility(case)["resolved"] == (
        ResolveEligibility.NEEDS_INFO
    )
    assert [(w.state, len(w.cause_rows)) for w in fix_windows(case)] == [
        (FIX_UNKNOWN, 0)
    ]

    await _confirm(engine, case)

    assert _row(case, absent).category == CAUSAL_ABSENCE


async def test_a_turn_judges_its_rows_on_the_status_it_ends_with():
    """A withdrawn finding ends the turn verified: the row held before step 2c
    is restored, and nothing reports it. Beside a revision, the rows are held
    until the user's answer and reported once, by the final call."""
    withdrawn = dict(
        invalidation_withdrawn=True,
        withdrawal_basis="the user has customer tickets from that hour",
    )
    engine, case = revision._engine(), _invalidated(ProblemStatus.VERIFIED)
    meta = await revision._apply(
        engine,
        case,
        revision._DSU(
            evidence_to_add=[revision._row(CAUSAL_ABSENCE, "g")],
            verification_updates=ProblemVerificationUpdate(**withdrawn),
        ),
    )
    assert case.progress.problem_status == ProblemStatus.VERIFIED
    assert case.evidence[-1].category == CAUSAL_ABSENCE
    assert not [r for r in meta.get("validation_repairs", []) if "causal_absence" in r]
    assert "symptom_absence_evidence" not in "\n".join(meta.get("system_notices") or [])

    engine, case = revision._engine(), _invalidated(ProblemStatus.VERIFIED)
    meta = await _propose(
        engine,
        case,
        revision._row(CAUSAL_ABSENCE, "g"),
        revision._row(CAUSAL_ABSENCE, "g2"),
        **withdrawn,
    )
    held = meta["evidence_added"][1:]  # 2 of the turn's 3 rows
    assert pending_revision(case).prior_status == ProblemStatus.VERIFIED
    assert [_row(case, i).category for i in held] == [SYMPTOM_ABSENCE] * 2
    repairs = [r for r in meta["validation_repairs"] if "causal_absence" in r]
    assert len(repairs) == 1
    assert repairs[0].startswith("2 causal_absence_evidence row(s)")
    assert (
        "\n".join(meta["system_notices"]).count("causal_absence_evidence row(s)") == 1
    )
    assert "2 causal_absence_evidence row(s) arrived" in "\n".join(
        meta["system_notices"]
    )

    await _confirm(engine, case)

    assert [_row(case, i).category for i in held] == [CAUSAL_ABSENCE] * 2


async def test_a_confirmation_restores_only_the_holds_rows():
    """A row held at the end of an ordinary unverified turn was judged then:
    a later revision's confirmation does not restore it."""
    engine, case = revision._engine(), revision._case()
    case.current_turn = 2
    early = revision._row(CAUSAL_ABSENCE, "early")
    early.source_file_id = upload(case, 2)
    meta = await revision._apply(engine, case, revision._DSU(evidence_to_add=[early]))
    (held,) = meta["evidence_added"]
    case.current_turn = 3
    verify_problem(case, via="test")
    case.current_turn = 4
    await _propose(engine, case)
    assert pending_revision(case).held_since_turn == 4

    await _confirm(engine, case)

    assert _row(case, held).category == SYMPTOM_ABSENCE


async def test_a_reworded_revision_restores_the_rows_from_where_the_hold_began():
    """A re-wording keeps the turn the hold began, so a confirmation restores
    a row-only hold turn's row recorded before the re-wording."""
    engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
    await _propose(engine, case)
    (absent,) = await _hold_turn(engine, case, revision._row(CAUSAL_ABSENCE, "g"))
    case.current_turn = 6
    await _propose(engine, case, symptom="s6", revised_problem_statement=REWORDED)
    held = pending_revision(case)
    assert (held.text, held.held_since_turn, held.proposed_at_turn) == (REWORDED, 4, 6)
    assert held.staged == ()

    await _confirm(engine, case)

    assert _row(case, absent).category == CAUSAL_ABSENCE


async def test_a_menu_close_during_the_hold_declines_the_resolution():
    """The close cancels the revision back to a verified problem, which
    restores the hold's rows, so the case confirms a resolution and the close
    is the user's decline of it (#2418)."""
    engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
    absent = await _hold_with_both_legs(engine, case)

    reason = execute_user_closure(case, case.effective_driver_id)

    assert _row(case, absent).category == CAUSAL_ABSENCE
    assert reason == ClosureReason.RESOLUTION_DECLINED


async def test_a_hold_turns_feedback_names_the_answer():
    engine, case = revision._engine(), revision._case(ProblemStatus.VERIFIED)
    meta, _ = await _revision_turn_with_recheck(engine, case)
    assert ANSWER in "\n".join(meta["system_notices"])

    engine, case = revision._engine(), revision._case()
    meta = await revision._apply(
        engine,
        case,
        revision._DSU(evidence_to_add=[revision._row(CAUSAL_ABSENCE, "g")]),
    )
    assert "recorded as symptom_absence_evidence" in "\n".join(meta["system_notices"])
    assert ANSWER not in "\n".join(meta["system_notices"])
