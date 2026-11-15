"""Shadow parity (#2811 S2a): every fact a narrator prints has a record field
equal to the derivation the narrator calls today, but for the declared
differences.

The record narration reads is ``narration_outcome``: the case folded in
memory over its stored record, at the moment narrated. The fixtures are built
through ``process_turn`` (the LLM stubbed) at each moment that narrates
without a cycle's record: step 0b's answer to an offer, Gate 1, the
``hypothesis_action`` intent, a deterministic step-0 exit (the menu close),
a terminal Q&A turn, and ``mark_told`` followed by the commit. One fixture
each shows a declared difference; a declared difference no fixture shows
fails. No narrator reads the record yet (S2b).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from faultmaven.core.investigation.cause_assurance import (
    FIX_INTERCEPTION,
    FIX_SUCCEEDED,
    SUBJECT_PROBLEM,
    _cause_posited_turn,
    cause_recheck_status,
    fix_windows,
    interception_relief,
    latest_decided_fix_window,
    latest_disconfirmation_turn,
    mitigation_observation,
    pending_symptom_check,
    subject_index,
    symptom_elimination_rows,
)
from faultmaven.core.investigation.cause_identification import (
    cause_identification_leg,
)
from faultmaven.core.investigation.milestone_engine.turn_commit import (
    TurnCommitPlan,
    commit_turn_plan,
)
from faultmaven.core.investigation.narration.content import content
from faultmaven.core.investigation.outcome import (
    compute_case_outcome,
    narration_outcome,
    record_case_outcome,
)
from faultmaven.core.investigation.terminal_transitions import (
    _mitigation_verified,
    closure_verdict,
    declined_resolve_entry,
    execute_user_closure,
    mitigation_closure_line,
    resolution_record_lines,
    should_generate_terminal_summary,
    shown_milestones,
)
from faultmaven.modules.case.contracts import (
    ClosureEligibility,
    EvidenceCategory,
    FixWindowState,
    InvestigationActionType,
    MitigationRecord,
    NeedPurpose,
    RecheckReason,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)

pytestmark = pytest.mark.unit

#: Each declared difference, by fact: the record holds the corrected value.
DECLARED = {
    # ``assembly.py`` prints the stored ``mitigation.verified``; the record
    # holds it relapse-adjusted.
    "mitigation.verified": "#2609",
    # ``fix_confirmed`` and the block read windows fresh in fix-turn order;
    # the record carries a FAILED window over a fresh read.
    "fix_confirmed": "#2582/#2595",
    "windows": "#2582/#2595",
}


# ---------------------------------------------------------------------------
# The facts: today's derivation, and the record's field
# ---------------------------------------------------------------------------


def _dated(case) -> list:
    return sorted(
        (
            a
            for a in case.proposed_actions or []
            if a.state == "accepted"
            and getattr(a.action_type, "value", a.action_type)
            == InvestigationActionType.SOLUTION.value
            and a.accepted_in_turn is not None
        ),
        key=lambda a: a.accepted_in_turn,
    )


def derived(case) -> dict:
    """Each narrator fact as the narrator derives it today."""
    subjects = subject_index(case)
    windows = fix_windows(case, subjects)
    status = cause_recheck_status(case, subjects=subjects, windows=windows)
    cause_rows = [e for e, why in status if why is None]
    by_turn = {w.fix_turn: w for w in windows}
    seen = mitigation_observation(case, subjects)
    held = pending_symptom_check(case, subjects=subjects, windows=windows)
    problem, cause = subjects.bindable_rows()
    window = latest_decided_fix_window(case)
    stored = case.progress.mitigation
    attempts = _dated(case)
    return {
        "recheck": [
            (e.evidence_id, why.reason if why else RecheckReason.COUNTS)
            for e, why in status
        ],
        "recheck.turns": sorted(
            {
                (why.reason, why.turn)
                for _e, why in status
                if why is not None
                and why.reason
                in (RecheckReason.PREDATES_POSITING, RecheckReason.PREDATES_FAILED_FIX)
            }
        ),
        "windows": [
            (
                w.fix_turn,
                w.state,
                w.observed,
                w.relief_turn,
                w.failure_turn,
                sorted(
                    e.evidence_id
                    for e in subjects.rows.values()
                    if e.category == EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE
                    and subjects.subject(e) == SUBJECT_PROBLEM
                    and w.holds(subjects.observed(e))
                ),
            )
            for w in (by_turn.get(a.accepted_in_turn) for a in attempts)
            if w is not None
        ],
        "interceptions": [
            (a.action_id, interception_relief(case, a.accepted_in_turn, subjects))
            for a in attempts
            if a.fix_kind == FIX_INTERCEPTION
        ],
        "fix_confirmed": window is not None and window.state == FIX_SUCCEEDED,
        "legs": (
            [e.evidence_id for e in cause_rows],
            [
                e.evidence_id
                for e in (
                    symptom_elimination_rows(
                        case, cause_rows, subjects=subjects, windows=windows
                    )
                    if cause_rows
                    else []
                )
            ],
        ),
        "bindable": (
            [e.evidence_id for e in problem],
            [e.evidence_id for e in cause],
        ),
        "held_need": held.need_id if held is not None else None,
        "open_needs": [
            (n.need_id, n.request_text)
            for n in case.evidence_needs
            if n.is_outstanding and n.purpose == NeedPurpose.SYMPTOM_VERIFICATION
        ],
        "declined": declined_resolve_entry(case) is not None,
        "mitigation": (
            None
            if seen is None
            else (seen.attempt_turn, seen.relief_turn, seen.present_turn, seen.holding)
        ),
        "mitigation.verified": bool(stored is not None and stored.verified),
        # Read by the closed reply only (``_closed_suggestions``).
        "summary_due": (
            should_generate_terminal_summary(case) if case.is_terminal else None
        ),
        "record_lines": resolution_record_lines(case),
        "identification_leg": cause_identification_leg(case),
        "closure": (closure_verdict(case), mitigation_closure_line(case)),
        "milestones": shown_milestones(case),
        "solution": (
            (case.solutions[-1].solution_id, case.solutions[-1].title)
            if case.solutions
            else None
        ),
    }


def recorded(outcome, case) -> dict:
    """The same facts, read off the record (and ``content`` by its ids)."""
    cause = outcome.cause
    attempts = sorted(
        (a for a in outcome.attempts if a.accepted_in_turn is not None),
        key=lambda a: a.accepted_in_turn,
    )
    windows = sorted(
        (a.window for a in outcome.attempts if a.window is not None),
        key=lambda w: -1 if w.fix_turn is None else w.fix_turn,
    )
    decided = [w for w in windows if w.state != FixWindowState.UNKNOWN]
    turns = {
        RecheckReason.PREDATES_POSITING: cause.posited_turn,
        RecheckReason.PREDATES_FAILED_FIX: cause.latest_disconfirmation_turn,
    }
    mitigation = outcome.mitigation
    resolution = outcome.resolution
    return {
        "recheck": list(cause.recheck.items()),
        "recheck.turns": sorted(
            {(code, turns[code]) for code in cause.recheck.values() if code in turns}
        ),
        "windows": [
            (
                a.window.fix_turn,
                a.window.state,
                a.window.observed,
                a.window.relief_turn,
                a.window.failure_turn,
                sorted(a.window.problem_row_ids),
            )
            for a in attempts
            if a.window is not None
        ],
        "interceptions": [
            (a.action_id, (a.relief_turn, a.present_turn))
            for a in attempts
            if a.kind == FIX_INTERCEPTION
        ],
        "fix_confirmed": bool(decided)
        and decided[-1].state == FixWindowState.SUCCEEDED,
        "legs": (resolution.cause_row_ids, resolution.problem_row_ids),
        "bindable": (outcome.bindable.problem_row_ids, outcome.bindable.cause_row_ids),
        "held_need": resolution.pending_check_need_id,
        "open_needs": [(i, content(case, i)) for i in resolution.open_need_ids],
        "declined": resolution.declined_stands,
        "mitigation": (
            None
            if mitigation is None
            else (
                mitigation.attempt_turn,
                mitigation.relief_turn,
                mitigation.present_turn,
                mitigation.holding,
            )
        ),
        "mitigation.verified": bool(mitigation is not None and mitigation.verified),
        "summary_due": (
            outcome.closure.eligibility != ClosureEligibility.TRIVIAL
            if case.is_terminal
            else None
        ),
        "record_lines": resolution.statement,
        "identification_leg": cause.identification_leg,
        "closure": (outcome.closure.eligibility, outcome.closure.statement),
        "milestones": outcome.milestones_shown,
        "solution": (
            (resolution.solution_id, content(case, resolution.solution_id))
            if resolution.solution_id
            else None
        ),
    }


def differences(case) -> set[str]:
    today, record = derived(case), recorded(narration_outcome(case), case)
    assert today.keys() == record.keys()
    return {fact for fact in today if today[fact] != record[fact]}


# ---------------------------------------------------------------------------
# The fixtures, at each moment narrated
# ---------------------------------------------------------------------------


async def _offer_answered_at_0b():
    from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
        _offered_then_declined,
    )

    _engine, case, _result = await _offered_then_declined()
    return case


async def _gate_one():
    from tests.unit.core.investigation.test_gate_one_decline_1464 import (
        _decide_click,
        _inquiry_case_awaiting_gate_one,
    )

    case = _inquiry_case_awaiting_gate_one()
    await _decide_click(case, True, "Yes, that's the problem")
    return case


async def _hypothesis_action():
    from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
        _engine,
        _turn,
    )
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _HYP_ID,
        _identified_case,
    )

    case, _root, _hyp = _identified_case()
    record_case_outcome(case, at="cycle")
    await _turn(
        _engine(),
        case,
        "",
        intent_type="hypothesis_action",
        intent_data={"hypothesis_id": _HYP_ID, "action": "validate"},
    )
    return case


async def _menu_close():
    from tests.unit.core.investigation.test_current_record_2811 import (
        _revision_pending,
    )
    from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
        _engine,
    )

    case = _revision_pending()
    await _engine().process_turn(
        case=case,
        user_message="",
        intent_type="status_transition",
        intent_data={"to_state": "closed"},
        redaction_ctx=None,
    )
    return case


async def _terminal_qa():
    from faultmaven.core.investigation.schemas import TerminalResponse
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _identified_case,
    )
    from tests.unit.core.investigation.test_problem_statement_revision_and_false_alarm import (
        _engine,
    )

    case, _root, _hyp = _identified_case()
    execute_user_closure(case, "u")
    engine = _engine()
    engine.generator.generate_structured_output = AsyncMock(
        return_value=TerminalResponse(
            agent_response="It was the pool size.", state_updates={}
        )
    )
    case.current_turn += 1
    await engine.process_turn(
        case=case, user_message="what was the cause again?", redaction_ctx=None
    )
    return case


async def _told_then_committed():
    from tests.unit.core.investigation.test_turn_reorder_2636 import (
        _decline,
        _held_recheck,
        _live,
    )

    eng, case = _held_recheck()
    case.current_turn = 8
    _decline(case)
    await _live(eng, case, 8)
    repository = InMemoryCaseRepository()
    await repository.save(case)
    return await commit_turn_plan(repository, case, TurnCommitPlan())


async def _held_check():
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _held_case,
    )

    case = _held_case()
    record_case_outcome(case, at="cycle")
    return case


async def _held_check_then_interception():
    """A check asked after the fix holds the resolution, and an interception
    run since, re-checked gone then present; committed as a step-0 exit."""
    from tests.unit.core.investigation.bound_rows import bound_row
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import _fix
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _held_case,
    )

    case = _held_case()
    shown = case.problem_verification.verifying_evidence_ids[0]
    _fix(case, turn=8, fix_kind="interception", target_ids=[])
    bound_row(case, EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, turn=9, rechecks=shown)
    bound_row(case, EvidenceCategory.SYMPTOM_EVIDENCE, turn=10, rechecks=shown)
    case.current_turn = 10
    record_case_outcome(case, at="cycle")
    repository = InMemoryCaseRepository()
    await repository.save(case)
    return await commit_turn_plan(repository, case, TurnCommitPlan())


async def _relapsed_mitigation():
    """#2609: a mitigation verified, then the problem seen present again."""
    from tests.unit.core.investigation.bound_rows import bound_row
    from tests.unit.core.investigation.golden_cases_2636 import _accepted_mitigation

    case, shown = _accepted_mitigation()
    bound_row(case, EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, turn=7, rechecks=shown)
    case.progress.mitigation = MitigationRecord(
        **{**case.progress.mitigation.model_dump(), "verified": True}
    )
    bound_row(case, EvidenceCategory.SYMPTOM_EVIDENCE, turn=9, rechecks=shown)
    case.current_turn = 9
    record_case_outcome(case, at="cycle")
    assert _mitigation_verified(case) is False, "premise: relapsed"
    return case


def _carried_failure(kinded: bool):
    """#2582 (a failure read through a cited row, re-rooted) and #2595 (an
    unkinded window re-read at the pool): the record carries FAILED."""
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import (
        _cited_but_unlinked,
        _deepen,
        _fix,
        _relapse,
    )
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _eid,
        _identified_case,
        _recompute_at,
    )

    case, root, hyp = _identified_case()
    if kinded:
        cited = _cited_but_unlinked(case, root)
        _recompute_at(case, 5)
        _fix(case, target_ids=[root.node_id], fix_kind="remediation")
        _relapse(case, rechecks=cited.evidence_id)
    else:
        _fix(case)
        _relapse(case, rechecks=_eid("pool_log"))
    _recompute_at(case, 9)
    case.progress.outcome = compute_case_outcome(None, case, at="cycle")
    _deepen(case, hyp, root)
    return case


async def _2582():
    return _carried_failure(kinded=True)


async def _2595():
    """Re-read at the deeper pool, the fix's window now SUCCEEDS on a re-check
    of the deeper cause: ``fix_confirmed`` reads it, the record its FAILED."""
    from tests.unit.core.investigation.bound_rows import VERIFYING_ID, _row
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _eid,
        _recompute_at,
    )

    case = _carried_failure(kinded=False)
    _row(
        case,
        EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
        turn=11,
        evidence_id=_eid("deepgone"),
        summary="pgbouncer pool_size now reads 100",
        rechecks=_eid("deeper"),
    )
    _row(
        case,
        EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
        turn=12,
        evidence_id=_eid("relief2"),
        summary="checkout answers 200",
        rechecks=VERIFYING_ID,
    )
    _recompute_at(case, 12)
    return case


#: Each fixture, and the declared differences it shows.
FIXTURES = {
    "offer_answered_at_0b": (_offer_answered_at_0b, set()),
    "gate_one": (_gate_one, set()),
    "hypothesis_action": (_hypothesis_action, set()),
    "menu_close": (_menu_close, set()),
    "terminal_qa": (_terminal_qa, set()),
    "told_then_committed": (_told_then_committed, set()),
    "held_check": (_held_check, set()),
    "held_check_then_interception": (_held_check_then_interception, set()),
    "2609_relapsed_mitigation": (_relapsed_mitigation, {"mitigation.verified"}),
    "2582_cited_failure_re_rooted": (_2582, {"windows"}),
    "2595_unkinded_failure_rebound": (_2595, {"fix_confirmed", "windows"}),
}


@pytest.mark.parametrize("name", sorted(FIXTURES))
async def test_each_narrator_fact_has_a_field_equal_to_its_derivation(name):
    build, declared = FIXTURES[name]
    case = await build()
    assert differences(case) == declared


def test_each_declared_difference_is_shown_by_a_fixture():
    shown = set().union(*(declared for _build, declared in FIXTURES.values()))
    assert shown == set(DECLARED), "a declared difference no fixture shows"


async def test_content_returns_the_text_an_id_names_and_none_for_another():
    case = await _offer_answered_at_0b()
    row = case.evidence[0]
    assert content(case, row.evidence_id) == row.summary
    assert content(case, "ev_000000000000") is None
