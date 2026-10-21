"""A fix attempt is a record (#2105, fix-verification design D2.1–D2.2).

After a fix fails its window, the gate asks for "a new fix after the one that
did not hold". ``solution_accepted`` used to be a set-once latch: a second
acceptance was absorbed, its action never got ``accepted_in_turn``, no window
opened for it and RESOLVED was out of reach. An acceptance now names the
SOLUTION the user carried out (``accepted_action_ref``), and that action
registers once, in its own turn. Everything here goes through
``_apply_investigation_updates`` and the real stage gate.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from faultmaven.core.investigation.cause_assurance import fix_windows
from faultmaven.core.investigation.milestone_engine.milestone_inference import (
    validate_reasoning_first,
)
from faultmaven.core.investigation.milestone_engine.statement_revision import (
    confirm_revision,
)
from faultmaven.core.investigation.problem_status import propose_revision
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
    InvestigationResponse_Treatment,
    MilestoneJustifications,
    MilestoneUpdates,
    SolutionToAdd,
)
from faultmaven.core.investigation.terminal_transitions import (
    ResolutionReadiness,
    assess_resolution_readiness,
)
from faultmaven.infrastructure.persistence.models import Base
from faultmaven.modules.case.contracts import (
    Case,
    InvestigationActionType,
    ProblemStatus,
    SolutionType,
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


def _fix(description: str) -> SolutionToAdd:
    return SolutionToAdd(
        description=description,
        solution_type=SolutionType.CONFIG_CHANGE,
        estimated_impact="checkout recovers under peak",
        risks="none",
    )


_SECOND = "raise max_connections to 200 and cap the pool per worker"


def _accepted(ref=None) -> MilestoneUpdates:
    return MilestoneUpdates(solution_accepted=True, accepted_action_ref=ref)


def _solutions(case):
    return [
        a
        for a in case.proposed_actions
        if a.action_type == InvestigationActionType.SOLUTION
    ]


def _windows(case):
    return [(w.state, w.relief_turn, w.failure_turn) for w in fix_windows(case)]


async def _relapsed():
    """The first fix ran at 6 and held from 7; the problem is back at 13."""
    eng, case, root, hyp = await _fixed_and_relieved()
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


# ---------------------------------------------------------------------------
# A second fix registers when it is named.
# ---------------------------------------------------------------------------


async def test_a_second_fix_after_a_relapse_registers_and_can_resolve():
    """#2105's reproduction, naming the fix it proposes."""
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
    assert second.state == "pending"
    assert second.accepted_in_turn is None
    assert (
        f"Only if the user reported carrying out {second.action_id}, set "
        f"accepted_action_ref to {second.action_id}"
    ) in bare["system_feedback"]

    await _turn(eng, case, 16, milestones=_accepted(second.action_id))
    assert (second.state, second.accepted_in_turn) == ("accepted", 16)
    assert len(case.action_attempts) == 2


async def test_a_reassertion_naming_the_registered_fix_is_absorbed():
    eng, case = await _relapsed()
    first = _solutions(case)[0]

    meta = await _turn(eng, case, 14, milestones=_accepted(first.action_id))

    assert first.accepted_in_turn == 6
    assert len(case.action_attempts) == 1
    assert "solution_accepted" not in meta["milestones_completed"]
    assert "solution_accepted" not in meta.get("system_feedback", "")


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
            [_fix("restart the pool"), _fix(_SECOND)],
            _accepted("new_index_0"),
            "a superseded SOLUTION",
            id="superseded-in-the-same-response",
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


# ---------------------------------------------------------------------------
# A second registration is justified like the first.
# ---------------------------------------------------------------------------


def _response(milestones: MilestoneUpdates) -> SimpleNamespace:
    trail = EvidenceTrail(
        evidence_analyzed=[], milestone_justifications=MilestoneJustifications()
    )
    return SimpleNamespace(
        evidence_trail=trail,
        state_updates=_TSU(milestones=milestones, evidence_to_add=[]),
    )


async def test_an_unjustified_second_registration_is_refused_like_the_first():
    eng, case = await _relapsed()
    await _turn(eng, case, 14, solutions_to_add=[_fix(_SECOND)])
    first, second = _solutions(case)

    named = validate_reasoning_first(_response(_accepted(second.action_id)), case)
    minted = validate_reasoning_first(_response(_accepted("new_index_0")), case)
    restated = validate_reasoning_first(_response(_accepted(first.action_id)), case)

    assert named[0] is False and named[2] == {"solution_accepted"}
    assert minted[0] is False and minted[2] == {"solution_accepted"}
    assert restated == (True, [], set())


# ---------------------------------------------------------------------------
# A second fix staged behind a revised statement registers on the replay.
# ---------------------------------------------------------------------------


async def test_a_staged_second_fix_registers_when_the_revision_is_confirmed():
    """The named fix was second in a response whose first entry was staged on
    an earlier hold turn, so its ``new_index_1`` is re-indexed to the staged
    list's ``new_index_0``."""
    eng, case = await _relapsed()
    relapse_row = case.evidence[-1].evidence_id
    case.current_turn = 14
    propose_revision(
        case,
        text="checkout returns 503 under peak load",
        evidence_ids=[relapse_row],
        basis="the 503s come back only at peak",
        offer_key="rev_2105",
    )
    await _turn(eng, case, 14, solutions_to_add=[_fix("restart the pool")])
    held = await _turn(
        eng,
        case,
        15,
        solutions_to_add=[_fix("restart the pool"), _fix(_SECOND)],
        milestones=_accepted("new_index_1"),
    )
    assert "held until the user confirms" in held["system_feedback"]
    assert held.get("system_feedback", "").count("accepted_action_ref") == 0
    assert len(_solutions(case)) == 1

    case.current_turn = 16
    await confirm_revision(eng.responses, case, {})

    assert case.progress.problem_status == ProblemStatus.VERIFIED
    second = next(a for a in _solutions(case) if a.description == _SECOND)
    assert (second.state, second.accepted_in_turn) == ("accepted", 16)
    assert len(case.action_attempts) == 2


# ---------------------------------------------------------------------------
# Both repositories keep the second attempt.
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


async def _two_attempts() -> Case:
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
    return record


def _attempt_record(case: Case):
    return (
        [(a.action_id, a.state, a.accepted_in_turn) for a in case.proposed_actions],
        [(t.action_id, t.attempt_id) for t in case.action_attempts],
    )


async def test_a_second_attempt_round_trips_on_sqlite(sqlite_repository):
    case = await _two_attempts()

    await sqlite_repository.save(case)
    reloaded = await sqlite_repository.get(case.case_id)

    assert _attempt_record(reloaded) == _attempt_record(case)
    assert [a.accepted_in_turn for a in reloaded.proposed_actions] == [6, 14]


async def test_a_second_attempt_round_trips_on_the_postgresql_row_mapping():
    case = await _two_attempts()

    metadata = json.loads(
        _case_record_params(case, datetime.now(timezone.utc))["metadata"]
    )
    with patch(
        "faultmaven.modules.case.infrastructure.postgresql_hybrid_case_repository"
        ".loading._load_case_actions",
        AsyncMock(return_value=[]),
    ):
        reloaded = await _row_to_case(_pg_repo().db, _pg_row(metadata))

    assert _attempt_record(reloaded) == _attempt_record(case)
    assert [a.accepted_in_turn for a in reloaded.proposed_actions] == [6, 14]


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

    for text in (
        failed["system_feedback"],
        no_pending["system_feedback"],
        downgraded.proposed_actions[0].downgrade_reason,
        _between(
            treatment, "4. **`state_updates.milestones.solution_accepted`", "5. **"
        ),
        _between(treatment, "User only APPLIED the fix", "User ASKS to resolve"),
        rca_diagnosis_block(DEFAULT_GIVEN),
        _get_diagnosis_focus_emphasis(case.progress, case),
        INVESTIGATION_BASE,
        SCHEMA_INSTRUCTIONS,
    ):
        assert "accepted_action_ref" in text


# ---------------------------------------------------------------------------
# PR #2143 review: what the user ran is the action named (F1, F3), an
# acceptance is never held bare (F2), the reasoning strip takes the ref with
# the signal (F8), and a fix that worked is no failure cycle (F5).
# ---------------------------------------------------------------------------

_FIRST = "raise max_connections to 100"


def _identified():
    """The #1927 case at turn 5: the cause identified, no fix proposed yet."""
    case, _root, _hyp = _identified_case()
    case.uploaded_files.append(
        UploadedFile(
            file_id=_FILE_ID, filename="c.log", size_bytes=1, uploaded_at_turn=1
        )
    )
    return _engine(), case


def _states(case):
    return [(a.description, a.state, a.accepted_in_turn) for a in _solutions(case)]


def _accepted_turns(case):
    return sorted(a.accepted_in_turn for a in _solutions(case) if a.state == "accepted")


def _pending(case):
    return [a for a in _solutions(case) if a.state == "pending"]


async def test_a_fix_named_by_id_and_reproposed_in_the_same_words_registers():
    """P1: the response re-sends the fix it names, superseding it first."""
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
    """P2."""
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


async def test_a_second_fix_named_by_id_and_reproposed_registers():
    """P3."""
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
    """P4: the new offer is a different fix, so it stays pending."""
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
    """P5: the registered fix re-sent with ``new_index_0`` before its window
    has a verdict registers nothing and leaves no dead offer pending."""
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

    assert _accepted_turns(case) == [6]
    assert len(case.action_attempts) == 1
    assert _pending(case) == []


async def test_the_same_fix_applied_again_after_a_failed_window_registers():
    """P5b."""
    eng, case = await _relapsed()

    await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_FIRST)],
        milestones=_accepted("new_index_0"),
    )

    assert _accepted_turns(case) == [6, 14]


@pytest.mark.parametrize("shape", ["first-fix", "after-a-relapse"])
async def test_an_acceptance_naming_an_earlier_staged_fix_is_never_held_bare(shape):
    """F2: the named fix was staged on an earlier hold turn, so this bundle
    cannot name it. Held bare, the replay registered the newest pending fix
    (before any fix) or nothing (after one)."""
    if shape == "first-fix":
        eng, case = _identified()
        turn, before = 6, []
    else:
        eng, case = await _relapsed()
        turn, before = 14, [6]
    propose_revision(
        case,
        text="checkout returns 503 under peak load",
        evidence_ids=[case.evidence[-1].evidence_id],
        basis="the 503s come back only at peak",
        offer_key="rev_2143",
    )
    await _turn(eng, case, turn, solutions_to_add=[_fix(_SECOND)])
    meta = await _turn(
        eng,
        case,
        turn + 1,
        solutions_to_add=[_fix(_SECOND), _fix("restart the pool")],
        milestones=_accepted("new_index_0"),
    )
    assert (
        "solution_accepted was not held: accepted_action_ref new_index_0 names no "
        "fix this response stages"
    ) in meta["system_feedback"]

    case.current_turn = turn + 2
    await confirm_revision(eng.responses, case, {})

    assert _accepted_turns(case) == before
    assert len(case.action_attempts) == len(before)


async def test_the_reasoning_strip_takes_the_ref_with_the_signal():
    """F8: an unjustified acceptance is stripped, and its ref with it, so the
    gate does not answer it as a ref sent alone."""
    eng, case = await _relapsed()
    await _turn(eng, case, 14, solutions_to_add=[_fix(_SECOND)])
    second = _pending(case)[-1]
    case.current_turn = 15
    response = InvestigationResponse_Treatment(
        agent_response="Noted.",
        evidence_trail=EvidenceTrail(
            evidence_analyzed=[], milestone_justifications=MilestoneJustifications()
        ),
        state_updates=_TSU(milestones=_accepted(second.action_id)),
    )

    _case_out, meta = await eng.responses.process_response_structured(
        case, "I ran it", response
    )

    assert response.state_updates.milestones.accepted_action_ref is None
    assert (
        "Milestones ['solution_accepted'] were NOT recorded" in meta["system_feedback"]
    )
    assert "was ignored" not in meta["system_feedback"]
    assert second.state == "pending"


async def test_a_fix_that_worked_after_one_that_failed_is_no_failure_cycle():
    """F5: the monitor counts FAILED fix windows, not accepted actions."""
    eng, case = await _relapsed()
    await _turn(
        eng,
        case,
        14,
        solutions_to_add=[_fix(_SECOND)],
        milestones=_accepted("new_index_0"),
    )
    await _cause_and_problem_gone(eng, case, 15)
    monitor = ProgressMonitor()
    assert assess_resolution_readiness(case).verdict == ResolutionReadiness.READY
    assert monitor._detect_fix_failure_cycle(case, "solution") is False

    await _turn(eng, case, 16, _ev(_PRESENT, "checkout returns 503 again at peak"))

    assert [w.state for w in fix_windows(case)][1:] == ["failed", "failed"]
    assert monitor._detect_fix_failure_cycle(case, "solution") is True
