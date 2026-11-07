"""#2100, Phase 4 lane A: a mitigation is recorded as what was observed.

The prompt asked for ``mitigation_verified`` on "improvement", on the user's
words and on the stall's "proceed", and the gate set ``verified`` whenever it
came: nothing recorded what was observed. Now the verdict is derived from a
re-check bound to the problem at or after the mitigation's attempt, and what is
not an observation (the user's declaration, the stall's choice) is the record's
disposition, never read as verified. Driven through the real
``_apply_investigation_updates``; no live LLM.
"""

import pytest

from faultmaven.core.investigation.cause_assurance import _dated_fix_turns, fix_windows
from faultmaven.core.investigation.milestone_engine.milestone_inference import (
    validate_reasoning_first,
)
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    build_fix_verification_block,
)
from faultmaven.core.investigation.prompts.templates.treatment import (
    mitigation_instructions,
)
from faultmaven.core.investigation.schemas import (
    EvidenceTrail,
    InvestigationResponse_Mitigation,
    MilestoneJustifications,
    MilestoneUpdates,
)
from faultmaven.core.investigation.terminal_transitions import derive_closure_reason
from faultmaven.modules.case.contracts import (
    ClosureReason,
    EvidenceCategory,
    InvestigationActionType,
    InvestigationStage,
    MitigationRecord,
    ProblemStatus,
    ProposedAction,
)
from tests.unit.core.investigation import test_replay_emission_turn_2537 as replay
from tests.unit.core.investigation.bound_rows import bound_row, bound_to_add
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _DSU,
    _apply,
    _case,
    _engine,
    _on_record,
)

pytestmark = pytest.mark.unit

_GONE = EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
_PRESENT = EvidenceCategory.SYMPTOM_EVIDENCE
_SUFFICIENT = ClosureReason.MITIGATION_SUFFICIENT


def _pending():
    """A verified problem with the row that showed it, and a workaround
    proposed at 5. Returns the case and that row's id."""
    case = _case()
    shown = _on_record(case, "orders time out after 30s on the primary")
    case.problem_verification.verifying_evidence_ids = [shown]
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.MITIGATION,
            description="route orders to the replica",
            proposed_in_turn=5,
        )
    )
    return case, shown


async def _turn(eng, case, turn: int, *rows, **milestones) -> dict:
    case.current_turn = turn
    dsu = _DSU(
        evidence_to_add=list(rows),
        milestones=MilestoneUpdates(**milestones) if milestones else None,
    )
    return await _apply(eng, case, dsu)


async def _accepted():
    """Accepted at 6: the attempt the verdict reads."""
    eng, (case, shown) = _engine(), _pending()
    await _turn(eng, case, 6, mitigation_accepted=True)
    return eng, case, shown


def _relief(case, turn: int, rechecks: str | None, category=_GONE):
    row = bound_to_add(case, category, "orders answer in 200ms", turn=turn)
    return row.model_copy(update={"rechecks": rechecks})


def _feedback(meta: dict) -> str:
    return "\n".join(meta.get("system_notices") or [])


# ---------------------------------------------------------------------------
# The verdict is derived
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("one_response", [False, True], ids=["two", "one"])
async def test_a_bound_relief_after_the_attempt_verifies(one_response):
    """#2100's shape: accepted at 6, the problem re-checked gone after it. The
    accept and the verify may come in one response: the accept dates the
    attempt before the verdict reads it."""
    eng, (case, shown) = _engine(), _pending()
    turn = 6 if one_response else 7
    if not one_response:
        await _turn(eng, case, 6, mitigation_accepted=True)
        assert case.current_stage is InvestigationStage.MITIGATION
    meta = await _turn(
        eng,
        case,
        turn,
        _relief(case, turn, shown),
        mitigation_accepted=True if one_response else None,
        mitigation_verified=True,
    )

    (attempt,) = [a for a in case.proposed_actions if a.state == "accepted"]
    assert attempt.accepted_in_turn == 6
    record = case.progress.mitigation
    assert (record.verified, record.completed_at_turn) == (True, turn)
    assert "mitigation_verified" in meta["milestones_completed"]
    assert case.current_stage is InvestigationStage.DIAGNOSIS
    assert derive_closure_reason(case) is _SUFFICIENT


def _unverified(case):
    case.progress.problem_status = ProblemStatus.UNVERIFIED


def _revision_pending(case):
    case.progress.problem_status = ProblemStatus.REVISION_PENDING


@pytest.mark.parametrize(
    "shape, said",
    [
        ("unbound", "no re-check bound to the problem shows it relieved after "),
        ("before_the_attempt", "after the mitigation (turn 6)"),
        ("relapse", "(it was seen present at turn 8)"),
        ("unverified", "verify the problem first, citing symptom_verification"),
        ("revision_pending", "the revised problem statement awaits the user's"),
        ("legacy", "after the mitigation: record the post-mitigation"),
    ],
)
async def test_mitigation_verified_without_a_bound_relief_is_refused(shape, said):
    """The label alone is refused with one line, ``verified`` unchanged: no
    re-check bound to the problem (a row naming nothing, one observed before
    the attempt, a relief the problem was seen after again), no verified
    problem to bind to (ruling (iii), with its own words while a revision
    waits), or an attempt with no turn (a legacy record)."""
    eng, case, shown = await _accepted()
    (attempt,) = [a for a in case.proposed_actions if a.state == "accepted"]
    rows = [_relief(case, 8, shown)]
    if shape == "unbound":
        rows = [_relief(case, 8, None)]
    elif shape == "before_the_attempt":
        bound_row(case, _GONE, turn=5, rechecks=shown)
        rows = []
    elif shape == "relapse":
        bound_row(case, _GONE, turn=7, rechecks=shown)
        rows = [_relief(case, 8, shown, category=_PRESENT)]
    elif shape == "unverified":
        _unverified(case)
    elif shape == "revision_pending":
        _revision_pending(case)
    else:
        attempt.accepted_in_turn = None

    meta = await _turn(eng, case, 8, *rows, mitigation_verified=True)

    record = case.progress.mitigation
    assert (record.verified, record.completed_at_turn) == (False, None)
    assert "mitigation_verified" not in meta["milestones_completed"]
    assert case.current_stage is InvestigationStage.MITIGATION
    feedback = _feedback(meta)
    assert said in feedback
    assert feedback.count("mitigation_verified was not recorded") == 1
    assert "set mitigation_disposition declared_stable" in feedback
    if shape in ("unbound", "before_the_attempt", "relapse"):
        assert f"rechecks naming one of {shown}" in feedback


async def test_a_relief_after_a_later_fix_still_verifies_the_mitigation():
    """The mitigation's read has no end at a later SOLUTION: one record, and a
    relief after the fix is still one after the mitigation (critique 2)."""
    eng, case, shown = await _accepted()
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="raise the pool to 50",
            proposed_in_turn=7,
            state="accepted",
            accepted_in_turn=8,
        )
    )
    case.progress.solution_accepted = True
    await _turn(eng, case, 9, _relief(case, 9, shown), mitigation_verified=True)

    assert case.progress.mitigation.verified is True
    assert case.current_stage is InvestigationStage.TREATMENT


async def test_a_later_persistence_takes_the_reason_not_the_stored_verdict():
    """Relief at 7 verifies; the problem seen again at 9 leaves the stored
    verdict and the stage alone, and the case no longer closes as
    mitigation_sufficient (critique 4)."""
    eng, case, shown = await _accepted()
    await _turn(eng, case, 7, _relief(case, 7, shown), mitigation_verified=True)
    assert derive_closure_reason(case) is _SUFFICIENT
    await _turn(eng, case, 9, _relief(case, 9, shown, category=_PRESENT))

    assert case.progress.mitigation.verified is True
    assert case.current_stage is InvestigationStage.DIAGNOSIS
    assert derive_closure_reason(case) is not _SUFFICIENT


async def test_a_stored_verdict_is_read_as_stored():
    """The dev corpus's shape: verified, no bound row, no dated attempt. It
    keeps its stage and its reason, and a re-emission is absorbed."""
    eng, (case, _shown) = _engine(), _pending()
    case.progress.mitigation = MitigationRecord(accepted=True, verified=True)
    meta = await _turn(eng, case, 7, mitigation_verified=True)

    assert case.progress.mitigation.verified is True
    assert "mitigation_verified" not in _feedback(meta)
    assert case.current_stage is InvestigationStage.DIAGNOSIS
    assert derive_closure_reason(case) is _SUFFICIENT


# ---------------------------------------------------------------------------
# The disposition
# ---------------------------------------------------------------------------


async def test_the_users_declaration_is_a_disposition_written_once():
    eng, case, _shown = await _accepted()
    meta = await _turn(eng, case, 7, mitigation_disposition="declared_stable")

    record = case.progress.mitigation
    assert (record.verified, record.disposition) == (False, "declared_stable")
    assert (record.disposition_at_turn, record.completed_at_turn) == (7, 7)
    assert "mitigation_disposed" in meta["milestones_completed"]
    assert case.current_stage is InvestigationStage.DIAGNOSIS
    assert derive_closure_reason(case) is _SUFFICIENT

    restated = await _turn(eng, case, 8, mitigation_disposition="declared_stable")
    assert "mitigation_disposition" not in _feedback(restated)
    assert "mitigation_disposed" not in restated["milestones_completed"]
    other = await _turn(eng, case, 9, mitigation_disposition="proceed_unmitigated")
    assert record.disposition == "declared_stable"
    assert "already carries declared_stable, written once" in _feedback(other)


async def test_the_stalls_proceed_is_never_a_verdict_nor_a_reason():
    eng, case, shown = await _accepted()
    meta = await _turn(eng, case, 7, mitigation_disposition="proceed_unmitigated")

    record = case.progress.mitigation
    assert (record.verified, record.disposition) == (False, "proceed_unmitigated")
    # Not a relieved symptom: no pre-mitigation boundary (critique 8).
    assert record.completed_at_turn is None
    assert "mitigation_disposed" in meta["milestones_completed"]
    assert case.current_stage is InvestigationStage.DIAGNOSIS
    assert derive_closure_reason(case) is ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE

    later = await _turn(eng, case, 8, _relief(case, 8, shown), mitigation_verified=True)
    assert record.verified is False
    assert "recorded as proceed_unmitigated" in _feedback(later)


async def test_a_disposition_needs_an_accepted_mitigation():
    eng, (case, _shown) = _engine(), _pending()
    meta = await _turn(eng, case, 6, mitigation_disposition="declared_stable")
    assert case.progress.mitigation is None
    assert "no mitigation is accepted" in _feedback(meta)


async def test_one_response_judges_the_verdict_before_the_disposition():
    """A verdict derived this turn refuses the proceed; a refused verify beside
    a disposition that registered does not ask for the declaration again
    (critique 5)."""
    eng, case, shown = await _accepted()
    meta = await _turn(
        eng,
        case,
        7,
        _relief(case, 7, shown),
        mitigation_verified=True,
        mitigation_disposition="proceed_unmitigated",
    )
    record = case.progress.mitigation
    assert (record.verified, record.disposition) == (True, None)
    assert "already showed the mitigation holding" in _feedback(meta)

    eng, case, _shown = await _accepted()
    meta = await _turn(
        eng,
        case,
        7,
        mitigation_verified=True,
        mitigation_disposition="declared_stable",
    )
    record = case.progress.mitigation
    assert (record.verified, record.disposition) == (False, "declared_stable")
    assert "mitigation_verified was not recorded" in _feedback(meta)
    assert "set mitigation_disposition declared_stable" not in _feedback(meta)


async def test_a_declared_mitigation_offers_the_close_in_its_own_words():
    """The 3B propose-close fires on a declaration and says what the record
    holds: the user's report, not a verification."""
    eng, case, _shown = await _accepted()
    case.problem_verification.rca_infeasible = True
    case.problem_verification.rca_infeasible_rationale = "the vendor API is opaque"
    await _turn(eng, case, 7, mitigation_disposition="declared_stable")

    assert case.offer_record.open.summary == (
        "You reported the mitigation stable. Since the vendor API is opaque, "
        "shall we close this case as stabilized?"
    )


def _response(value: str, justification: str | None):
    updates = InvestigationResponse_Mitigation.MitigationStateUpdate(
        milestones=MilestoneUpdates(mitigation_disposition=value)
    )
    return InvestigationResponse_Mitigation(
        agent_response="Noted.",
        state_updates=updates,
        evidence_trail=EvidenceTrail(
            milestone_justifications=MilestoneJustifications(
                mitigation_disposition=justification
            )
        ),
    )


@pytest.mark.parametrize(
    "recorded, justification, offending",
    [
        (None, "the user: checkout has been stable since", set()),
        (None, None, {"mitigation_disposition"}),
        ("declared_stable", None, set()),
    ],
    ids=["justified", "unjustified", "restated"],
)
def test_a_new_disposition_is_justified_like_a_milestone(
    recorded, justification, offending
):
    case, _shown = _pending()
    case.progress.mitigation = MitigationRecord(accepted=True, disposition=recorded)
    _valid, _errors, found = validate_reasoning_first(
        _response("declared_stable", justification), case
    )
    assert found == offending


async def test_a_disposition_held_on_a_revision_is_replayed_by_its_value():
    """Held with the workaround on a staging turn and replayed on the "yes":
    the value, not ``True``, and both dated at the hold turn (critique 1)."""

    def declared(dsu):
        dsu.solutions_to_add = [
            replay._solution("workaround", "route orders to the replica")
        ]
        dsu.milestones = MilestoneUpdates(
            mitigation_accepted=True, mitigation_disposition="declared_stable"
        )

    _, case, *_ = await replay._replayed(edit=declared, confirmed=replay._STAGED + 2)

    (attempt,) = [a for a in case.proposed_actions if a.state == "accepted"]
    record = case.progress.mitigation
    assert attempt.accepted_in_turn == replay._STAGED
    assert (record.disposition, record.disposition_at_turn) == (
        "declared_stable",
        replay._STAGED,
    )


# ---------------------------------------------------------------------------
# What the model is shown
# ---------------------------------------------------------------------------


async def test_an_unbound_post_mitigation_row_is_told_what_it_could_name():
    """The fix's feedback, anchored on the mitigation's attempt; the mitigation
    opens no fix window."""
    eng, case, shown = await _accepted()
    meta = await _turn(eng, case, 7, _relief(case, 7, None))

    row = case.evidence[-1].evidence_id
    assert f"Post-mitigation row(s) {row} name nothing they re-check" in _feedback(meta)
    assert f"symptom rows: one of {shown}" in _feedback(meta)
    assert _dated_fix_turns(case) == []
    assert [w.fix_turn for w in fix_windows(case)] == [None]


async def test_the_block_shows_the_mitigation_and_then_its_relief():
    eng, case, shown = await _accepted()
    block = build_fix_verification_block(case)
    assert "Mitigation: executed turn 6: not yet re-checked" in block
    assert f"re-check names in rechecks: {shown}" in block
    assert "Resolution legs" not in block

    await _turn(eng, case, 7, _relief(case, 7, shown))
    assert "Mitigation: executed turn 6: problem re-checked relieved at turn 7" in (
        build_fix_verification_block(case)
    )


def test_the_prompt_asks_for_the_disposition_never_the_label():
    text = mitigation_instructions(frozenset())
    stall = text[text.index("WHEN MITIGATION STALLS") :]
    stall = stall[: stall.index("EVIDENCE TYPES FOR THIS STAGE")]
    assert "mitigation_disposition: proceed_unmitigated" in stall
    assert "mitigation_verified=True" not in stall
    assert "mitigation_disposition: declared_stable" in text
    assert "ACCEPT SUBJECTIVE CONFIRMATION" not in text
    assert "rechecks: one of the rows that showed the problem" in text
    assert "Skip this step" not in text
