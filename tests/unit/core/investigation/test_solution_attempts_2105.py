"""A fix attempt is a record, decided once, in the gate (#2105; fix-verification
design D2.1–D2.2).

After a fix fails its window, the gate asks for "a new fix after the one that
did not hold". ``solution_accepted`` used to be a set-once latch: a second
acceptance was absorbed, its action never got ``accepted_in_turn``, no window
opened for it and RESOLVED was out of reach. An acceptance now names the fix
the user carried out (``accepted_action_ref``), one decision in the stage gate
resolves it to a fix identity and registers the newest SOLUTION bearing it,
once, and a revision hold carries that identity to the replay. Everything here
goes through ``_apply_investigation_updates`` and the real stage gate; every
justification test through ``process_response_structured``.
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.core.investigation.cause_assurance import fix_windows
from faultmaven.core.investigation.milestone_engine import stage_gates
from faultmaven.core.investigation.milestone_engine.response_application import (
    WHY_OPEN_DOWNGRADE_REASON,
)
from faultmaven.core.investigation.milestone_engine.statement_revision import (
    confirm_revision,
    staged_work_summary,
)
from faultmaven.core.investigation.problem_status import (
    decline_revision,
    invalidate_problem,
    propose_revision,
)
from faultmaven.core.investigation.progress_monitor import ProgressMonitor
from faultmaven.core.investigation.prompts.context_builder.assembly import (
    build_investigation_context,
)
from faultmaven.core.investigation.prompts.templates.assembly import (
    _get_diagnosis_focus_emphasis,
)
from faultmaven.core.investigation.prompts.templates.diagnosis import (
    rca_diagnosis_block,
)
from faultmaven.core.investigation.prompts.templates.investigation import (
    INVESTIGATION_BASE,
    SCHEMA_INSTRUCTIONS,
)
from faultmaven.core.investigation.prompts.templates.treatment import (
    treatment_instructions,
)
from faultmaven.core.investigation.schemas import (
    EvidenceTrail,
    HypothesisEvidenceLinkToAdd,
    HypothesisUpdate,
    InvestigationResponse_Treatment,
    MilestoneJustifications,
    MilestoneUpdates,
    ProblemVerificationUpdate,
    SolutionToAdd,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.contracts import (
    Case,
    CauseState,
    ConfidenceLevel,
    EvidenceStance,
    HypothesisState,
    InvestigationActionType,
    PendingRevision,
    ProblemStatus,
    ProblemVerification,
    ProposedAction,
    RootCauseConclusion,
    SolutionType,
    StagedCauseWork,
    UploadedFile,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.loading import (
    _row_to_case,
)
from faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository.rows import (
    _case_record_params,
)
from faultmaven.modules.case.infrastructure.sqlite_case_repository.repository import (
    SQLiteCaseRepository,
)
from tests.unit.core.investigation.fix_turns_2039 import (
    _CAUSE_GONE,
    _GONE,
    _NEW_FIX,
    _PRESENT,
    _TSU,
    _ev,
    _fixed_and_relieved,
    _turn,
)
from tests.unit.core.investigation.given_tools import DEFAULT_GIVEN
from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
    _FILE_ID,
    _identified_case,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import (
    _apply,
    _case,
    _engine,
)
from tests.unit.modules.case.infrastructure.test_last_suggestions_persistence import (
    _pg_repo,
    _pg_row,
)

pytestmark = pytest.mark.unit

_FIRST = "raise max_connections to 100"
_SECOND = "raise max_connections to 200 and cap the pool per worker"
_RESTART = "restart the pool"
_LICENSE = "targets a root cause that is no longer established"


def _fix(description: str, solution_type=SolutionType.CONFIG_CHANGE) -> SolutionToAdd:
    return SolutionToAdd(
        description=description,
        solution_type=solution_type,
        estimated_impact="checkout recovers under peak",
        risks="none",
    )


def _accepted(ref=None) -> MilestoneUpdates:
    return MilestoneUpdates(solution_accepted=True, accepted_action_ref=ref)


def _solutions(case):
    return [
        a
        for a in case.proposed_actions
        if a.action_type == InvestigationActionType.SOLUTION
    ]


def _pending(case):
    return [a for a in _solutions(case) if a.state == "pending"]


def _states(case):
    return [(a.description, a.state, a.accepted_in_turn) for a in _solutions(case)]


def _accepted_turns(case):
    return sorted(a.accepted_in_turn for a in _solutions(case) if a.state == "accepted")


def _windows(case):
    return [(w.state, w.relief_turn, w.failure_turn) for w in fix_windows(case)]


def _identified():
    """The #1927 case at turn 5: the cause identified, no fix proposed yet."""
    case, _root, _hyp = _identified_case()
    case.uploaded_files.append(
        UploadedFile(
            file_id=_FILE_ID, filename="c.log", size_bytes=1, uploaded_at_turn=1
        )
    )
    return _engine(), case


async def _relapsed():
    """The first fix ran at 6 and held from 7; the problem is back at 13."""
    eng, case, _root, _hyp = await _fixed_and_relieved()
    await _turn(eng, case, 13, _ev(_PRESENT, "checkout returns 503 again"))
    assert assess_resolution_readiness(case).missing[0] == _NEW_FIX
    return eng, case


async def _cause_and_problem_gone(eng, case, turn):
    await _turn(
        eng,
        case,
        turn,
        _ev(_CAUSE_GONE, "max_connections now reads 200, per-worker cap 20"),
        _ev(_GONE, "checkout returns 200, 503 rate 0.0/s"),
    )


async def _process(eng, case, turn, *, justified, **updates) -> dict:
    """One turn through ``process_response_structured``: the reasoning check,
    the verdict, then the apply."""
    case.current_turn = turn
    response = InvestigationResponse_Treatment(
        agent_response="Noted.",
        evidence_trail=EvidenceTrail(
            evidence_analyzed=[],
            milestone_justifications=MilestoneJustifications(
                solution_accepted="the user reports running it" if justified else None
            ),
        ),
        state_updates=_TSU(**updates),
    )
    _case_out, meta = await eng.responses.process_response_structured(
        case, "I ran it", response
    )
    return meta


def _revise(case):
    """A revised statement awaits re-confirmation: cause work is held."""
    propose_revision(
        case,
        text="checkout returns 503 under peak load",
        evidence_ids=[case.evidence[-1].evidence_id],
        basis="the 503s come back only at peak",
        offer_key="rev_2105",
    )


async def _confirm(eng, case, turn) -> dict:
    case.current_turn = turn
    meta: dict = {}
    await confirm_revision(eng.responses, case, meta)
    return meta


async def _refute_the_cause(eng, case, turn) -> dict:
    """The model refutes the identified cause on a row on record (INV-47)."""
    hyp = next(iter(case.hypotheses.values()))
    link = HypothesisEvidenceLinkToAdd(
        hypothesis_id_ref=hyp.hypothesis_id,
        evidence_id_ref="new_index_0",
        stance=EvidenceStance.REFUTES,
        reasoning="the limit was raised and checkout still fails",
    )
    refute = HypothesisUpdate(
        hypothesis_id=hyp.hypothesis_id,
        state=HypothesisState.REFUTED,
        refutation_reason="checkout fails with the limit far above the pool",
    )
    return await _turn(
        eng,
        case,
        turn,
        _ev(_PRESENT, "checkout returns 503 with max_connections at 500"),
        hypothesis_evidence_links=[link],
        hypotheses_to_update=[refute],
    )


# ---------------------------------------------------------------------------
# A second fix registers when it is named.
# ---------------------------------------------------------------------------


async def test_a_second_fix_after_a_relapse_registers_and_can_resolve():
    """#2105's reproduction: the second fix registers at 14, its window
    succeeds at 15, and RESOLVED is reachable."""
    eng, case = await _relapsed()

    meta = await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted("new_index_0"),
    )
    await _cause_and_problem_gone(eng, case, 15)

    first, second = _solutions(case)
    assert [a.accepted_in_turn for a in (first, second)] == [6, 14]
    assert [t.action_id for t in case.action_attempts] == [
        first.action_id,
        second.action_id,
    ]
    assert "solution_accepted" in meta["milestones_completed"]
    assert _windows(case) == [
        ("unknown", None, None),
        ("failed", 7, 13),
        ("succeeded", 15, None),
    ]
    readiness = assess_resolution_readiness(case)
    assert readiness.missing == []
    assert readiness.verdict == ResolutionReadiness.READY


async def test_a_bare_reemission_registers_nothing_and_a_named_one_does():
    """The second fix proposed, then accepted by its rendered ACTION_ID."""
    eng, case = await _relapsed()
    await _turn(eng, case, 14, solutions_to_add=[_fix(_SECOND)])
    second = _solutions(case)[-1]
    context = "\n".join(str(v) for v in build_investigation_context(case, "x").values())
    assert f"ACTION_ID: {second.action_id}" in context
    assert f"accepted_action_ref: {second.action_id}" in context

    bare = await _turn(eng, case, 15, milestones=_accepted())
    assert (second.state, second.accepted_in_turn) == ("pending", None)
    assert (
        f"Only if the user reported carrying out {second.action_id}, set "
        f"accepted_action_ref to {second.action_id}"
    ) in bare["system_feedback"]

    await _turn(eng, case, 16, milestones=_accepted(second.action_id))
    assert (second.state, second.accepted_in_turn) == ("accepted", 16)
    assert len(case.action_attempts) == 2


@pytest.mark.parametrize("window", ["held", "failed"])
async def test_a_reassertion_naming_the_registered_fix_is_absorbed(window):
    """A re-assertion never registers a second fix. Silent while identity keeps
    the fix; once its window failed, one line says how to register a
    re-application (review finding 5)."""
    if window == "held":
        eng, case, _root, _hyp = await _fixed_and_relieved()
    else:
        eng, case = await _relapsed()
    first = _solutions(case)[0]

    meta = await _turn(eng, case, 14, milestones=_accepted(first.action_id))

    assert first.accepted_in_turn == 6
    assert len(case.action_attempts) == 1
    assert "solution_accepted" not in meta["milestones_completed"]
    feedback = meta.get("system_feedback", "")
    if window == "held":
        assert "solution_accepted" not in feedback
    else:
        assert f"SOLUTION {first.action_id} is registered, and its fix window " in (
            feedback
        )


async def test_a_first_fix_without_a_ref_registers_as_before():
    """Control: the first-fix flow and the KB-resolution bundle are unchanged."""
    _eng, case, _root, _hyp = await _fixed_and_relieved()

    (first,) = _solutions(case)
    assert (first.state, first.accepted_in_turn) == ("accepted", 6)
    assert [t.action_id for t in case.action_attempts] == [first.action_id]


# ---------------------------------------------------------------------------
# A ref that names anything else registers nothing, and says why.
# ---------------------------------------------------------------------------


async def test_a_ref_naming_a_downgraded_diagnostic_is_refused():
    eng, case = _engine(), _case()  # problem verified, no cause established
    await _apply(eng, case, _TSU(solutions_to_add=[_fix(_SECOND)]))
    (diagnostic,) = case.proposed_actions
    assert diagnostic.action_type == InvestigationActionType.DIAGNOSTIC
    case.current_turn += 1

    meta = await _apply(eng, case, _TSU(milestones=_accepted(diagnostic.action_id)))

    assert diagnostic.state == "pending"
    assert case.action_attempts == []
    assert case.progress.solution_accepted is False
    assert (
        f"accepted_action_ref {diagnostic.action_id} names "
        f"{diagnostic.action_id}, a DIAGNOSTIC (downgraded; see its ENGINE_NOTE)"
    ) in meta["system_feedback"]


@pytest.mark.parametrize(
    "solutions, milestones, said",
    [
        pytest.param(
            [],
            _accepted("act_0123456789ab"),
            "accepted_action_ref act_0123456789ab names no action on this case",
            id="unknown-id",
        ),
        pytest.param(
            [_fix(_SECOND)],
            _accepted("new_index_3"),
            "accepted_action_ref new_index_3 names no fix this response proposes",
            id="out-of-range-index",
        ),
        pytest.param(
            [_fix(_SECOND)],
            MilestoneUpdates(accepted_action_ref="new_index_0"),
            "accepted_action_ref new_index_0 was ignored: it is read only with "
            "solution_accepted",
            id="ref-without-the-signal",
        ),
    ],
)
async def test_a_ref_naming_anything_else_registers_nothing(
    solutions, milestones, said
):
    eng, case = await _relapsed()

    meta = await _turn(eng, case, 14, solutions_to_add=solutions, milestones=milestones)

    assert [a.accepted_in_turn for a in _solutions(case)][1:] == [None] * len(solutions)
    assert len(case.action_attempts) == 1
    assert "solution_accepted" not in meta["milestones_completed"]
    assert said in meta["system_feedback"]


@pytest.mark.parametrize("shape", ["first-fix", "after-a-relapse"])
async def test_a_fix_superseded_by_a_different_one_in_the_same_response_registers(
    shape,
):
    """``new_index_0`` of ``[X, Y]`` names X; Y, a different fix, superseded it
    as an offer in the same apply. The user ran X: supersession never decides
    whether a fix ran."""
    eng, case = _identified() if shape == "first-fix" else await _relapsed()
    turn = 5 if shape == "first-fix" else 14

    await _turn(
        eng,
        case,
        turn,
        solutions_to_add=[_fix(_RESTART), _fix(_SECOND)],
        milestones=_accepted("new_index_0"),
    )

    assert _states(case)[-2:] == [
        (_RESTART, "accepted", turn),
        (_SECOND, "pending", None),
    ]


# ---------------------------------------------------------------------------
# A registration is justified like the first; the gate decides when one is.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("justified", [False, True])
async def test_an_unjustified_second_registration_is_refused_like_the_first(
    justified,
):
    eng, case = await _relapsed()
    await _turn(eng, case, 14, solutions_to_add=[_fix(_SECOND)])
    second = _solutions(case)[-1]

    meta = await _process(
        eng, case, 15, justified=justified, milestones=_accepted(second.action_id)
    )

    if justified:
        assert (second.state, second.accepted_in_turn) == ("accepted", 15)
        assert "EVIDENCE VALIDATION" not in meta.get("system_feedback", "")
    else:
        assert (second.state, second.accepted_in_turn) == ("pending", None)
        assert (
            "EVIDENCE VALIDATION: Milestones ['solution_accepted'] were NOT recorded"
            in meta["system_feedback"]
        )
        # Judged by the gate, so the pre-apply strip left it alone.
        assert "accepted_action_ref" not in meta["system_feedback"]


async def test_an_unjustified_reassertion_is_absorbed_with_no_line():
    """Control: no registration, so no justification is asked."""
    eng, case, _root, _hyp = await _fixed_and_relieved()
    first = _solutions(case)[0]

    meta = await _process(
        eng, case, 14, justified=False, milestones=_accepted(first.action_id)
    )

    assert "solution_accepted" not in meta.get("system_feedback", "")
    assert len(case.action_attempts) == 1


async def test_a_restated_latest_fix_named_by_new_index_is_absorbed():
    """P5: identity keeps the registered fix, so its copy is a re-assertion and
    no false "NOT recorded" is said."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    await _turn(eng, case, 6, milestones=_accepted())

    meta = await _process(
        eng,
        case,
        7,
        justified=False,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )

    assert "NOT recorded" not in meta.get("system_feedback", "")
    assert len(_solutions(case)) == 1
    assert len(case.action_attempts) == 1


@pytest.mark.parametrize("named", ["superseded", "latest"])
async def test_a_superseded_fix_named_after_a_first_fix_must_be_justified(named):
    """R1/P1: naming X, superseded by Y, would register X (the license holds),
    so it is justified like naming Y."""
    eng, case = await _relapsed()
    await _turn(eng, case, 14, solutions_to_add=[_fix(_RESTART)])
    x = _pending(case)[-1]
    await _turn(eng, case, 15, solutions_to_add=[_fix(_SECOND)])
    y = _pending(case)[-1]
    assert (x.state, x.superseded_reason) == ("superseded", "reproposal")
    target = x if named == "superseded" else y

    meta = await _process(
        eng, case, 16, justified=False, milestones=_accepted(target.action_id)
    )

    assert target.accepted_in_turn is None
    assert (x.state, y.state) == ("superseded", "pending")
    assert "Milestones ['solution_accepted'] were NOT recorded" in (
        meta["system_feedback"]
    )


async def test_the_strip_and_the_gate_share_one_evidence_validation_prefix():
    eng, case = await _relapsed()
    await _turn(eng, case, 14, solutions_to_add=[_fix(_SECOND)])
    second = _solutions(case)[-1]

    meta = await _process(
        eng,
        case,
        15,
        justified=False,
        milestones=MilestoneUpdates(
            solution_accepted=True,
            accepted_action_ref=second.action_id,
            mitigation_accepted=True,
        ),
    )

    feedback = meta["system_feedback"]
    assert feedback.count("EVIDENCE VALIDATION") == 1
    assert (
        "EVIDENCE VALIDATION: Milestones ['mitigation_accepted', "
        "'solution_accepted'] were NOT recorded this turn."
    ) in feedback
    # One message names both (review finding 9), not one per writer.
    assert feedback.count("completed without justification") == 1
    assert (
        "Milestones ['mitigation_accepted', 'solution_accepted'] completed "
        "without justification."
    ) in feedback


async def test_an_unjustified_second_fix_registers_while_a_transition_pends():
    """Control (PF): like the first fix, a registration needs no justification
    while a terminal transition is pending (the skip ``validate_reasoning_first``
    already has)."""
    eng, case = await _relapsed()
    await _turn(eng, case, 14, solutions_to_add=[_fix(_SECOND)])
    second = _solutions(case)[-1]
    case.pending_transition = {"target_state": "resolved", "proposed_at_turn": 14}

    await _process(
        eng, case, 15, justified=False, milestones=_accepted(second.action_id)
    )

    assert second.accepted_in_turn == 15


# ---------------------------------------------------------------------------
# Identity: the same fix restated keeps one action until its window fails.
# ---------------------------------------------------------------------------


async def test_a_fix_named_by_id_and_reproposed_in_the_same_words_registers():
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (offer,) = _solutions(case)

    await _turn(
        eng,
        case,
        6,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted(offer.action_id),
    )

    assert (offer.state, offer.accepted_in_turn) == ("accepted", 6)
    assert _pending(case) == []
    assert [t.action_id for t in case.action_attempts] == [offer.action_id]


async def test_a_named_fix_resent_every_turn_registers_once():
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (offer,) = _solutions(case)

    for turn in (6, 7, 8):
        await _turn(
            eng,
            case,
            turn,
            solutions_to_add=[_fix(_FIRST)],
            milestones=_accepted(offer.action_id),
        )

    assert _accepted_turns(case) == [6]
    assert len(case.action_attempts) == 1
    assert _solutions(case) == [offer]  # no copy minted, none left pending


async def test_a_second_fix_named_by_id_and_reproposed_registers():
    eng, case, _root, _hyp = await _fixed_and_relieved()
    await _turn(
        eng,
        case,
        13,
        _ev(_PRESENT, "checkout returns 503 again"),
        solutions_to_add=[_fix(_SECOND)],
    )
    second = _pending(case)[-1]

    await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted(second.action_id),
    )

    assert (second.state, second.accepted_in_turn) == ("accepted", 14)
    assert _accepted_turns(case) == [6, 14]


async def test_a_named_fix_registers_beside_a_different_new_offer():
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (offer,) = _solutions(case)

    await _turn(
        eng,
        case,
        6,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted(offer.action_id),
    )

    assert (offer.state, offer.accepted_in_turn) == ("accepted", 6)
    assert [a.description for a in _pending(case)] == [_SECOND]


async def test_a_resent_bundle_before_any_verdict_is_a_reassertion():
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    await _turn(eng, case, 6, milestones=_accepted())

    await _turn(
        eng,
        case,
        7,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )

    assert len(_solutions(case)) == 1  # the restatement minted no action
    assert _accepted_turns(case) == [6]
    assert len(case.action_attempts) == 1
    assert _pending(case) == []


@pytest.mark.parametrize("ref", ["new_index_0", "first-id"])
async def test_the_same_fix_applied_again_after_a_failed_window_registers(ref):
    """After a FAILED window the same fix is a new attempt: re-sent, it mints,
    and a ref naming it (or the failed fix's own ACTION_ID, F6) registers the
    re-application."""
    eng, case = await _relapsed()
    first = _solutions(case)[0]

    await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted(first.action_id if ref == "first-id" else ref),
    )

    assert len(_solutions(case)) == 2
    assert _accepted_turns(case) == [6, 14]
    assert len(case.action_attempts) == 2


@pytest.mark.parametrize(
    "restated, minted",
    [(" Raise  MAX_CONNECTIONS to 100 ", False), ("set max_connections to 100", True)],
    ids=["same-text", "reworded-control"],
)
async def test_a_pending_fix_restated_keeps_its_action(restated, minted):
    """The same text, whitespace and case aside, keeps the pending action;
    a reworded fix is a new offer, as #1136 rules."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (offer,) = _solutions(case)

    await _turn(eng, case, 6, solutions_to_add=[_fix(restated)])

    assert len(case.solutions) == 2  # the Solution row is minted either way
    if minted:
        assert offer.state == "superseded"
        assert [a.description for a in _pending(case)] == [restated]
    else:
        assert _solutions(case) == [offer]
        assert offer.state == "pending"


@pytest.mark.parametrize(
    "resent, accepted",
    [(_FIRST, [6, 14, 15]), (_SECOND, [6, 14])],
    ids=["earlier-failed-fix-reapplied", "latest-fix-restated"],
)
async def test_an_accepted_fix_keeps_its_action_only_while_its_window_is_latest(
    resent, accepted
):
    """R2: A failed (relapse at 13), B ran at 14. Identity reads the matched
    fix's own window: A applied again is a new attempt; B re-sent before its
    window decides is a re-assertion."""
    eng, case = await _relapsed()
    await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted("new_index_0"),
    )

    await _turn(
        eng,
        case,
        15,
        solutions_to_add=[_fix(resent)],
        milestones=_accepted("new_index_0"),
    )

    assert _accepted_turns(case) == accepted
    assert len(case.action_attempts) == len(accepted)


# ---------------------------------------------------------------------------
# Supersession decides whether a fix ran only through the M5 license.
# ---------------------------------------------------------------------------


async def test_a_fix_superseded_on_an_earlier_turn_registers_by_name():
    """X offered, then Y offered; the user reports running X."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (x,) = _solutions(case)
    await _turn(eng, case, 6, solutions_to_add=[_fix(_SECOND)])
    assert (x.state, x.superseded_reason) == ("superseded", "reproposal")

    await _turn(eng, case, 7, milestones=_accepted(x.action_id))

    assert (x.state, x.accepted_in_turn) == ("accepted", 7)
    assert x.superseded_reason is None
    assert [a.description for a in _pending(case)] == [_SECOND]


async def test_a_superseded_fix_named_and_re_sent_registers_the_copy():
    """Control (PB): the re-sent copy is the newest bearer of the fix, so it
    registers and no phantom offer of a fix already run is left pending, as
    "the most recent pending" did before. Registering the named X instead left
    its copy pending (the design critique's PB)."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (x,) = _solutions(case)
    await _turn(eng, case, 6, solutions_to_add=[_fix(_SECOND)])

    await _turn(
        eng, case, 7, solutions_to_add=[_fix(_FIRST)], milestones=_accepted(x.action_id)
    )

    assert len(case.action_attempts) == 1
    assert _accepted_turns(case) == [7]
    assert _FIRST not in [a.description for a in _pending(case)]


async def test_a_fix_withdrawn_when_its_license_fell_is_refused():
    """M5: the cause the offer rested on was refuted, so the offer was
    withdrawn (``license_lost``), not replaced (ruled 2026-10-21)."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (x,) = _solutions(case)
    await _refute_the_cause(eng, case, 6)
    assert (x.state, x.superseded_reason) == ("superseded", "license_lost")

    meta = await _turn(eng, case, 7, milestones=_accepted(x.action_id))

    assert x.accepted_in_turn is None
    assert case.action_attempts == []
    assert f"{x.action_id} {_LICENSE}" in meta["system_feedback"]


async def test_a_fix_the_hold_withdrew_is_not_registered_by_name_on_a_live_turn():
    """Ruled 2026-10-21: on a live turn a ``license_lost`` bearer stays refused,
    even once the license holds again; only a decision carried through the
    hold registers it. The line says how to register it: re-propose and name
    the copy."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (x,) = _solutions(case)
    _revise(case)
    await _turn(eng, case, 6)
    await _confirm(eng, case, 7)
    assert (x.state, x.superseded_reason) == ("superseded", "license_lost")

    meta = await _turn(eng, case, 8, milestones=_accepted(x.action_id))
    assert case.action_attempts == []
    assert f"{x.action_id} was withdrawn while its root cause" in (
        meta["system_feedback"]
    )

    await _turn(
        eng,
        case,
        9,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )
    assert _accepted_turns(case) == [9]


async def test_a_superseded_fix_does_not_register_after_the_license_fell():
    """R3: X superseded by Y, then the cause refuted (Y withdrawn). Naming X
    registers nothing: a permanent fix needs an established cause."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (x,) = _solutions(case)
    await _turn(eng, case, 6, solutions_to_add=[_fix(_SECOND)])
    await _refute_the_cause(eng, case, 7)
    assert (x.state, x.superseded_reason) == ("superseded", "reproposal")

    meta = await _turn(eng, case, 8, milestones=_accepted(x.action_id))

    assert x.accepted_in_turn is None
    assert case.progress.solution_accepted is False
    assert len(fix_windows(case)) == 1  # no fix window opened
    assert f"{x.action_id} {_LICENSE}" in meta["system_feedback"]


# ---------------------------------------------------------------------------
# A hold turn registers nothing: it carries its decision, by identity, and the
# replay registers it, stamped at the hold turn.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("resent", [None, _SECOND, _FIRST])
async def test_a_hold_turn_naming_the_pending_fix_registers_it_once(resent):
    """R4/H1/F2: a revision waits; the response names pending X by its
    ACTION_ID, alone, re-sending X, or proposing a different Y. X registers at
    the replay, stamped at the hold turn; Y stays pending."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_SECOND)])
    (x,) = _solutions(case)
    _revise(case)
    sent = {"solutions_to_add": [_fix(resent)]} if resent else {}

    held = await _turn(eng, case, 6, milestones=_accepted(x.action_id), **sent)
    assert case.action_attempts == []
    assert "held until the user confirms" in held["system_feedback"]
    await _confirm(eng, case, 7)

    assert len(case.action_attempts) == 1
    assert [(d, t) for d, s, t in _states(case) if s == "accepted"] == [(_SECOND, 6)]
    assert [a.description for a in _pending(case)] == (
        [_FIRST] if resent == _FIRST else []
    )


async def test_a_held_new_index_registers_on_the_replay_for_the_held_fix():
    """H2: with the hold turn's message as the attempt's."""
    eng, case = _identified()
    _revise(case)
    await _process(
        eng,
        case,
        6,
        justified=True,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )
    assert case.action_attempts == []

    await _confirm(eng, case, 7)

    assert [(d, t) for d, s, t in _states(case) if s == "accepted"] == [(_FIRST, 6)]
    assert case.action_attempts[0].user_message == "I ran it"


@pytest.mark.parametrize("shape", ["first-fix", "after-a-relapse"])
async def test_an_acceptance_naming_a_fix_staged_on_an_earlier_hold_turn_registers_it(
    shape,
):
    """H3: X was staged on an earlier hold turn, so this turn's bundle does not
    stage it again; ``new_index_0`` still names X, by identity."""
    if shape == "first-fix":
        eng, case = _identified()
        turn, before = 6, []
    else:
        eng, case = await _relapsed()
        turn, before = 14, [6]
    _revise(case)
    await _turn(eng, case, turn, solutions_to_add=[_fix(_SECOND)])
    await _turn(
        eng,
        case,
        turn + 1,
        solutions_to_add=[_fix(_SECOND), _fix(_RESTART)],
        milestones=_accepted("new_index_0"),
    )

    await _confirm(eng, case, turn + 2)

    assert [(d, t) for d, s, t in _states(case) if s == "accepted"][len(before) :] == [
        (_SECOND, turn + 1)
    ]
    assert len(case.action_attempts) == len(before) + 1
    assert [a.description for a in _pending(case)] == [_RESTART]


async def test_a_staged_second_fix_registers_when_the_revision_is_confirmed():
    """The named fix was second in a response whose first entry was staged on
    an earlier hold turn; it registers at the replay, stamped at 15."""
    eng, case = await _relapsed()
    case.current_turn = 14
    _revise(case)
    await _turn(eng, case, 14, solutions_to_add=[_fix(_RESTART)])
    held = await _turn(
        eng,
        case,
        15,
        solutions_to_add=[_fix(_RESTART), _fix(_SECOND)],
        milestones=_accepted("new_index_1"),
    )
    assert "held until the user confirms" in held["system_feedback"]
    assert held.get("system_feedback", "").count("accepted_action_ref") == 0
    assert len(_solutions(case)) == 1

    await _confirm(eng, case, 16)

    assert case.progress.problem_status == ProblemStatus.VERIFIED
    second = next(a for a in _solutions(case) if a.description == _SECOND)
    assert (second.state, second.accepted_in_turn) == ("accepted", 15)
    assert len(case.action_attempts) == 2


async def test_a_named_fix_re_sent_with_a_revised_statement_registers_on_the_replay():
    """One response names pending X by its ACTION_ID, re-sends X and revises
    the statement: the hold withdraws X, the replay's copy of X is the fix the
    name means, stamped at the hold turn."""
    eng, case = _engine(), _case()
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Connection pool exhaustion under load",
        mechanism="Pool capped at 5; traffic saturated it",
        likelihood=0.85,
        confidence_level=ConfidenceLevel.CONFIDENT,
    )
    await _turn(eng, case, 5, solutions_to_add=[_fix(_SECOND)])
    (x,) = _solutions(case)

    await _turn(
        eng,
        case,
        6,
        _ev(_PRESENT, "logins return 401 only at peak load after the change"),
        verification_updates=ProblemVerificationUpdate(
            revised_problem_statement="a third of logins fail with 401 at peak load",
            revision_evidence_ids=["new_index_0"],
            revision_basis="the 401s come back only at peak",
        ),
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted(x.action_id),
    )
    assert case.progress.problem_status == ProblemStatus.REVISION_PENDING
    await _confirm(eng, case, 7)

    (attempt,) = case.action_attempts
    (registered,) = [a for a in _solutions(case) if a.state == "accepted"]
    assert (registered.description, registered.accepted_in_turn) == (_SECOND, 6)
    assert attempt.action_id == registered.action_id


@pytest.mark.parametrize("resend", [False, True])
async def test_a_fix_withdrawn_by_the_hold_and_named_on_a_later_hold_turn_registers(
    resend,
):
    """PA: every hold turn withdraws pending fixes (``license_lost``), so on the
    second hold turn the name meets a superseded X. Carried, it registers once
    at the replay, stamped 7, and leaves no pending copy."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (x,) = _solutions(case)
    _revise(case)
    await _turn(eng, case, 6)
    assert (x.state, x.superseded_reason) == ("superseded", "license_lost")
    sent = {"solutions_to_add": [_fix(_FIRST)]} if resend else {}

    await _turn(eng, case, 7, milestones=_accepted(x.action_id), **sent)
    await _confirm(eng, case, 8)

    assert len(case.action_attempts) == 1
    assert _FIRST not in [a.description for a in _pending(case)]
    assert _accepted_turns(case) == [7]


@pytest.mark.parametrize("licensed", [False, True])
async def test_a_fix_withdrawn_before_the_hold_keeps_the_ruled_refusal(
    licensed, monkeypatch
):
    """The license fell before the revision was proposed: the hold changes
    nothing about it, so the ruled refusal stands on the hold turn, worded as
    the live rule words it (review finding 7)."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (x,) = _solutions(case)
    await _refute_the_cause(eng, case, 6)
    case.current_turn = 7
    _revise(case)
    if licensed:
        monkeypatch.setattr(
            stage_gates, "_solution_cause_validated", lambda *a, **k: True
        )

    meta = await _turn(eng, case, 8, milestones=_accepted(x.action_id))

    said = "was withdrawn while its root cause" if licensed else _LICENSE
    assert f"{x.action_id} {said}" in meta["system_feedback"]
    assert all(
        b.held_fix is None for b in case.problem_verification.pending_revision.staged
    )


async def test_two_held_registrations_give_two_windows_and_a_decline_names_them():
    """PS + the decline summary: stamped at their own hold turns, each opens
    its own window; a decline discards both and says so."""
    eng, case = _identified()
    _revise(case)
    await _turn(
        eng,
        case,
        6,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )
    await _turn(
        eng,
        case,
        7,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted("new_index_0"),
    )
    pending = case.problem_verification.pending_revision
    assert "2 reported fix execution(s)" in staged_work_summary(pending)

    await _confirm(eng, case, 8)

    assert len(case.action_attempts) == 2
    assert _accepted_turns(case) == [6, 7]
    assert [w.fix_turn for w in fix_windows(case)] == [None, 6, 7]


async def test_a_declined_revision_discards_the_carried_registration():
    """H5."""
    eng, case = _identified()
    _revise(case)
    await _turn(
        eng,
        case,
        6,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )
    case.current_turn = 7

    declined = decline_revision(case)

    assert "1 reported fix execution(s)" in staged_work_summary(declined)
    assert case.action_attempts == []
    assert _solutions(case) == []


async def test_a_held_registration_whose_fix_the_replay_downgrades_registers_nothing():
    """H4: the cause falls before confirmation, so the replay mints the held
    fix as a DIAGNOSTIC (M5): nothing registers, with the license line."""
    eng, case = _identified()
    _revise(case)
    await _turn(
        eng,
        case,
        6,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )
    case.root_cause_conclusion = None
    case.progress.cause_state = CauseState.UNKNOWN
    for hyp in case.hypotheses.values():
        hyp.likelihood = 0.1

    meta = await _confirm(eng, case, 7)

    assert case.action_attempts == []
    assert [a.action_type for a in case.proposed_actions] == [
        InvestigationActionType.DIAGNOSTIC
    ]
    diagnostic = case.proposed_actions[0]
    assert f"{diagnostic.action_id} {_LICENSE}" in meta["system_feedback"]


async def test_a_bundle_that_no_longer_validates_drops_its_carried_registration():
    eng, case = _identified()
    _revise(case)
    await _turn(
        eng,
        case,
        6,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )
    (bundle,) = case.problem_verification.pending_revision.staged
    assert bundle.held_fix == _FIRST
    bundle.updates["solutions_to_add"] = [{"description": None}]

    meta = await _confirm(eng, case, 7)

    assert case.action_attempts == []
    assert "Staged cause work from turn 6 could not be replayed" in (
        meta["validation_repairs"]
    )


async def test_a_bare_acceptance_of_a_fix_staged_earlier_and_not_re_sent_is_refused():
    """Control (PD): the bare acceptance names this response's held fixes, and
    this response holds none; nothing is pending, so the no-pending notice."""
    eng, case = _identified()
    _revise(case)
    await _turn(eng, case, 6, solutions_to_add=[_fix(_FIRST)])

    meta = await _turn(eng, case, 7, milestones=_accepted())
    await _confirm(eng, case, 8)

    assert "no SOLUTION proposal is currently pending" in meta["system_feedback"]
    assert case.action_attempts == []


async def test_a_bare_acceptance_on_a_hold_after_a_first_fix_names_the_held_fix():
    """PX: after a first fix the bare acceptance is absorbed, with the line
    naming ``new_index_N`` of the fix this response proposes."""
    eng, case = await _relapsed()
    _revise(case)

    meta = await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_FIRST), _fix(_SECOND)],
        milestones=_accepted(),
    )

    assert "set accepted_action_ref to new_index_1 with solution_accepted" in (
        meta["system_feedback"]
    )
    assert all(
        b.held_fix is None for b in case.problem_verification.pending_revision.staged
    )


async def test_a_bundle_staged_with_the_milestone_replays_as_a_live_acceptance():
    """Control (consumer): a bundle persisted before #2105 holds
    ``milestones.solution_accepted`` in its updates. Pre-user, no
    compatibility: it replays as a live bare acceptance."""
    eng, case = _identified()
    _revise(case)
    case.problem_verification.pending_revision.staged.append(
        StagedCauseWork(
            turn=6,
            updates={
                "solutions_to_add": [_fix(_FIRST).model_dump(mode="json")],
                "milestones": {"solution_accepted": True},
            },
        )
    )

    await _confirm(eng, case, 7)

    assert _accepted_turns(case) == [7]
    assert len(case.action_attempts) == 1


# ---------------------------------------------------------------------------
# Review of #2239: findings 1-8 and 10.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symptom", [True, False], ids=["mitigation", "downgraded"])
@pytest.mark.parametrize("by", ["action-id", "new-index"])
async def test_an_acceptance_naming_a_workaround_never_registers_its_solution(
    symptom, by
):
    """Finding 1 (INV-32, type-matched): a WORKAROUND with the text of an older
    SOLUTION, named by its ACTION_ID or ``new_index_0``, is refused, typed;
    the SOLUTION is not accepted."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_RESTART, SolutionType.RESTART)])
    (x,) = _solutions(case)
    rows = [_ev(_PRESENT, "checkout returns 503 under load, pool exhausted")]
    workaround = {"solutions_to_add": [_fix(_RESTART, SolutionType.WORKAROUND)]}
    if by == "new-index":
        meta = await _turn(
            eng,
            case,
            6,
            *rows[:symptom],
            milestones=_accepted("new_index_0"),
            **workaround,
        )
    else:
        await _turn(eng, case, 6, *rows[:symptom], **workaround)
        named = case.proposed_actions[-1]
        meta = await _turn(eng, case, 7, milestones=_accepted(named.action_id))

    kind = "a MITIGATION (set mitigation_accepted" if symptom else "a DIAGNOSTIC"
    assert (x.state, case.action_attempts) == ("pending", [])
    assert f"{case.proposed_actions[-1].action_id}, {kind}" in meta["system_feedback"]


async def test_a_downgraded_re_send_of_a_failed_fix_is_refused_typed():
    """Finding 5's variant: the license is down, so the re-sent failed fix mints
    a DIAGNOSTIC; only an older accepted SOLUTION bears its text."""
    eng, case = await _relapsed()
    case.root_cause_conclusion = None
    case.progress.cause_state = CauseState.UNKNOWN
    for hyp in case.hypotheses.values():
        hyp.likelihood = 0.1

    meta = await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )

    assert len(case.action_attempts) == 1
    assert "a DIAGNOSTIC (downgraded; see its ENGINE_NOTE)" in meta["system_feedback"]


async def test_naming_a_downgraded_diagnostic_registers_the_newer_solution():
    """Control for finding 1 (the amendment's stated consequence): the fix
    downgraded at 5 is re-proposed as a SOLUTION once the cause stands, and
    the acceptance names the DIAGNOSTIC's ACTION_ID."""
    eng, case = _engine(), _case()
    await _apply(eng, case, _TSU(solutions_to_add=[_fix(_SECOND)]))
    (diagnostic,) = case.proposed_actions
    case.root_cause_conclusion = RootCauseConclusion(
        root_cause="Connection pool exhaustion under load",
        mechanism="Pool capped at 5; traffic saturated it",
        likelihood=0.85,
        confidence_level=ConfidenceLevel.CONFIDENT,
    )
    await _turn(eng, case, 6, solutions_to_add=[_fix(_SECOND)])

    await _turn(eng, case, 7, milestones=_accepted(diagnostic.action_id))

    assert _states(case) == [(_SECOND, "accepted", 7)]


async def test_a_reworded_revision_keeps_the_hold_start():
    """Finding 2: the fix the hold withdrew at 6 stays carried after the
    revision is re-worded at 7."""
    eng, case = _identified()
    await _turn(eng, case, 5, solutions_to_add=[_fix(_FIRST)])
    (x,) = _solutions(case)
    _revise(case)
    await _turn(eng, case, 6)
    case.current_turn = 7
    propose_revision(
        case,
        text="checkout returns 503 under peak load only",
        evidence_ids=[case.evidence[-1].evidence_id],
        basis="reworded",
        offer_key="rev_2105_b",
    )
    assert case.problem_verification.pending_revision.held_since_turn == 5

    await _turn(eng, case, 8, milestones=_accepted(x.action_id))
    await _confirm(eng, case, 9)

    assert len(case.action_attempts) == 1
    assert _accepted_turns(case) == [8]


async def test_the_skip_is_read_when_the_gate_decides():
    """Finding 3: the same apply withdraws the false-alarm close that was
    pending before it, so an unjustified new fix is asked its justification."""
    eng, case = await _relapsed()
    case.current_turn = 13
    invalidate_problem(
        case, evidence_ids=[case.evidence[-1].evidence_id], basis="never present"
    )
    case.pending_transition = {
        "to_state": "closed",
        "closure_reason": "closed_false_alarm",
        "proposed_at_turn": 13,
    }

    meta = await _process(
        eng,
        case,
        14,
        justified=False,
        verification_updates=ProblemVerificationUpdate(
            invalidation_withdrawn=True,
            withdrawal_basis="the user says checkout really fails",
        ),
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted("new_index_0"),
    )

    assert case.pending_transition is None
    assert len(case.action_attempts) == 1
    assert "Milestones ['solution_accepted'] were NOT recorded" in (
        meta["system_feedback"]
    )


@pytest.mark.parametrize("justified", [False, True])
async def test_a_hold_turn_naming_a_failed_fix_settles_it_now(justified):
    """Findings 4 and 5 on a hold: the failed first fix, named and not re-sent,
    is settled on the hold turn as on a live turn: no false promise, no false
    "NOT recorded", one line on how to register a re-application."""
    eng, case = await _relapsed()
    first = _solutions(case)[0]
    case.current_turn = 14
    _revise(case)

    meta = await _process(
        eng, case, 14, justified=justified, milestones=_accepted(first.action_id)
    )
    await _confirm(eng, case, 15)

    feedback = meta["system_feedback"]
    assert "held until the user confirms" not in feedback
    assert "NOT recorded" not in feedback
    assert f"SOLUTION {first.action_id} is registered, and its fix window " in feedback
    assert len(case.action_attempts) == 1


async def test_a_bare_acceptance_after_a_carried_one_says_the_report_stands():
    """Finding 6: not the no-pending notice, which invites a second
    registration of the same execution."""
    eng, case = _identified()
    _revise(case)
    await _turn(
        eng,
        case,
        6,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )

    meta = await _turn(eng, case, 7, milestones=_accepted())
    await _confirm(eng, case, 8)

    assert "no SOLUTION proposal is currently pending" not in meta["system_feedback"]
    assert "the fix execution reported at turn 6 stands" in meta["system_feedback"]
    assert _accepted_turns(case) == [6]


async def test_a_replayed_mitigation_attempt_keeps_the_replays_message():
    """Finding 8 (mitigation must not change): only the SOLUTION attempt takes
    the carried hold-turn message."""
    eng, case = _identified()
    _revise(case)
    case.current_turn = 6
    response = InvestigationResponse_Treatment(
        agent_response="Noted.",
        evidence_trail=EvidenceTrail(
            evidence_analyzed=[],
            milestone_justifications=MilestoneJustifications(
                solution_accepted="the user reports running it",
                mitigation_accepted="the user reports running the workaround",
            ),
        ),
        state_updates=_TSU(
            solutions_to_add=[_fix(_FIRST), _fix(_RESTART, SolutionType.WORKAROUND)],
            evidence_to_add=[_ev(_PRESENT, "checkout returns 503, pool exhausted")],
            milestones=MilestoneUpdates(
                solution_accepted=True,
                accepted_action_ref="new_index_0",
                mitigation_accepted=True,
            ),
        ),
    )
    await eng.responses.process_response_structured(case, "I ran it", response)

    await _confirm(eng, case, 7)

    by_type = {
        a.action_type: t.user_message
        for t in case.action_attempts
        for a in case.proposed_actions
        if a.action_id == t.action_id
    }
    assert by_type == {
        InvestigationActionType.SOLUTION: "I ran it",
        InvestigationActionType.MITIGATION: "",
    }


@pytest.mark.parametrize("at", ["replay", "live"])
async def test_a_failed_fix_re_sent_and_downgraded_gets_the_license_line(at):
    """Delta D1/D2: the cause is refuted, so a re-send of the failed X is minted
    a DIAGNOSTIC (M5), by the replay of a carried decision or on a live turn
    naming the old X. The license line; not absorbed, not "re-propose"."""
    eng, case = await _relapsed()
    first = _solutions(case)[0]
    await _refute_the_cause(eng, case, 14)
    if at == "replay":
        case.current_turn = 15
        _revise(case)
        await _turn(
            eng,
            case,
            15,
            solutions_to_add=[_fix(_FIRST)],
            milestones=_accepted("new_index_0"),
        )
        meta = await _confirm(eng, case, 16)
    else:
        meta = await _turn(
            eng,
            case,
            15,
            solutions_to_add=[_fix(_FIRST)],
            milestones=_accepted(first.action_id),
        )

    assert case.proposed_actions[-1].action_type == InvestigationActionType.DIAGNOSTIC
    assert _LICENSE in meta["system_feedback"]
    assert "re-propose it as a SolutionToAdd" not in meta["system_feedback"]
    assert len(case.action_attempts) == 1


async def test_a_fix_carried_in_this_revision_is_neither_named_nor_carried_again():
    """Delta N1: after a first fix, Y is carried at 14; at 15 a bare acceptance
    re-sends Y. No line names Y's ``new_index``, and naming it anyway at 16
    carries nothing a second time."""
    eng, case = await _relapsed()
    case.current_turn = 14
    _revise(case)
    await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted("new_index_0"),
    )

    bare = await _turn(
        eng, case, 15, solutions_to_add=[_fix(_SECOND)], milestones=_accepted()
    )
    named = await _turn(
        eng,
        case,
        16,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted("new_index_0"),
    )

    assert "new_index_0" not in bare.get("system_feedback", "")
    assert "held until the user confirms" not in named.get("system_feedback", "")
    pending = case.problem_verification.pending_revision
    assert "1 reported fix execution(s)" in staged_work_summary(pending)
    await _confirm(eng, case, 17)
    assert _accepted_turns(case) == [6, 14]


async def test_every_downgrade_reason_fits_its_field_with_margin():
    """Finding 10: both M5 downgrade reasons stay well inside
    ``ProposedAction.downgrade_reason``'s maximum, or a downgrade raises."""
    (limit,) = [
        m.max_length
        for m in ProposedAction.model_fields["downgrade_reason"].metadata
        if hasattr(m, "max_length")
    ]
    downgraded = _case()
    await _apply(_engine(), downgraded, _TSU(solutions_to_add=[_fix(_SECOND)]))

    for reason in (
        downgraded.proposed_actions[0].downgrade_reason,
        WHY_OPEN_DOWNGRADE_REASON,
    ):
        assert len(reason) <= limit - 20


# ---------------------------------------------------------------------------
# Both repositories keep the second attempt and a carried registration.
# ---------------------------------------------------------------------------


@pytest.fixture
async def sqlite_repository():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield SQLiteCaseRepository(session)
    await engine.dispose()


async def _two_attempts_and_a_carry() -> Case:
    eng, case = await _relapsed()
    await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted("new_index_0"),
    )
    record = Case(
        case_id=case.case_id,
        user_id="user_alpha",
        enterprise_id="ent_alpha",
        title="two fixes",
    )
    record.proposed_actions = list(case.proposed_actions)
    record.action_attempts = list(case.action_attempts)
    record.problem_verification = ProblemVerification(
        symptom_statement="checkout returns 503",
        pending_revision=PendingRevision(
            text="checkout returns 503 under peak load",
            proposed_at_turn=15,
            staged=[
                StagedCauseWork(
                    turn=15, held_fix=_RESTART, held_fix_message="I restarted it"
                )
            ],
        ),
    )
    return record


def _attempt_record(case: Case):
    (bundle,) = case.problem_verification.pending_revision.staged
    return (
        [(a.action_id, a.state, a.accepted_in_turn) for a in case.proposed_actions],
        [(t.action_id, t.attempt_id) for t in case.action_attempts],
        (bundle.turn, bundle.held_fix, bundle.held_fix_message),
    )


async def test_a_second_attempt_and_a_carry_round_trip_on_sqlite(sqlite_repository):
    case = await _two_attempts_and_a_carry()

    await sqlite_repository.save(case)
    reloaded = await sqlite_repository.get(case.case_id)

    assert _attempt_record(reloaded) == _attempt_record(case)
    assert [a.accepted_in_turn for a in reloaded.proposed_actions] == [6, 14]
    assert _attempt_record(reloaded)[2] == (15, _RESTART, "I restarted it")


async def test_a_second_attempt_and_a_carry_round_trip_on_the_postgresql_row_mapping():
    case = await _two_attempts_and_a_carry()

    params = _case_record_params(case, datetime.now(timezone.utc))
    row = _pg_row(json.loads(params["metadata"]))
    row.problem_verification = params["problem_verification"]
    with patch(
        "faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository"
        ".loading._load_case_actions",
        AsyncMock(return_value=[]),
    ):
        reloaded = await _row_to_case(_pg_repo().db, row)

    assert _attempt_record(reloaded) == _attempt_record(case)
    assert [a.accepted_in_turn for a in reloaded.proposed_actions] == [6, 14]
    assert _attempt_record(reloaded)[2] == (15, _RESTART, "I restarted it")


# ---------------------------------------------------------------------------
# The fix-failure cycle counts FAILED windows; a later success is no cycle.
# ---------------------------------------------------------------------------


async def test_the_failure_cycle_counts_failed_windows_until_a_fix_works():
    eng, case = await _relapsed()
    monitor = ProgressMonitor()
    await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted("new_index_0"),
    )
    await _cause_and_problem_gone(eng, case, 15)
    assert monitor._detect_fix_failure_cycle(case, "solution") is False

    await _turn(eng, case, 16, _ev(_PRESENT, "checkout returns 503 again at peak"))
    assert [w.state for w in fix_windows(case)][1:] == ["failed", "failed"]
    assert monitor._detect_fix_failure_cycle(case, "solution") is True

    # A third fix not yet observed does not lift it ...
    await _turn(
        eng,
        case,
        17,
        solutions_to_add=[_fix(_RESTART)],
        milestones=_accepted("new_index_0"),
    )
    assert [w.state for w in fix_windows(case)][1:] == ["failed", "failed", "unknown"]
    assert monitor._detect_fix_failure_cycle(case, "solution") is True
    # The message counts the failed windows, as the detector does (finding 11).
    repair = monitor._repair_fix_failure_cycle(case, "solution")
    assert repair.message.startswith("Fix failure cycle: 2 attempts")

    # ... and one that worked ends it.
    await _cause_and_problem_gone(eng, case, 18)
    assert [w.state for w in fix_windows(case)][1:] == ["failed", "failed", "succeeded"]
    assert monitor._detect_fix_failure_cycle(case, "solution") is False


# ---------------------------------------------------------------------------
# Every site that tells the model to register a fix names the ref.
# ---------------------------------------------------------------------------


def _between(text: str, start: str, end: str) -> str:
    """The passage of ``text`` from ``start`` up to ``end``: one prompt site."""
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


async def test_every_prompt_site_that_registers_a_fix_names_the_ref():
    eng, case = await _relapsed()
    # The failed-window notice: an absence row after the relapse.
    failed = await _turn(eng, case, 14, _ev(_GONE, "checkout returns 200 for now"))
    await _turn(eng, case, 15, solutions_to_add=[_fix(_SECOND)])
    # The no-pending notice: a first acceptance with nothing pending.
    no_pending = await _apply(_engine(), _case(), _TSU(milestones=_accepted()))
    # The M5 downgrade reason.
    downgraded = _case()
    await _apply(_engine(), downgraded, _TSU(solutions_to_add=[_fix(_SECOND)]))
    treatment = treatment_instructions(DEFAULT_GIVEN)
    context = "\n".join(str(v) for v in build_investigation_context(case, "x").values())

    for text in (
        _between(context, "<pending_action>", "</pending_action>"),
        failed["system_feedback"],
        no_pending["system_feedback"],
        downgraded.proposed_actions[0].downgrade_reason,
        _between(
            treatment, "4. **`state_updates.milestones.solution_accepted`", "5. **"
        ),
        _between(treatment, "User only APPLIED the fix", "User ASKS to resolve"),
        _between(
            rca_diagnosis_block(DEFAULT_GIVEN),
            "COMPLIANCE DETECTION",
            "NOT compliance",
        ),
        _get_diagnosis_focus_emphasis(case.progress, case),
        _between(INVESTIGATION_BASE, "milestone_justifications:", "uncertainties:"),
        _between(SCHEMA_INSTRUCTIONS, "- milestones:", "- outcome:"),
    ):
        assert "accepted_action_ref" in text
