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
    off_cause_attempt,
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
from faultmaven.core.investigation.narration.rechecks import recheck_text
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
    NeedState,
    PendingCheckReason,
    RecheckReason,
    RecheckWhy,
    WritePoint,
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
    problem_rows = (
        symptom_elimination_rows(case, cause_rows, subjects=subjects, windows=windows)
        if cause_rows
        else []
    )
    named = {getattr(e, "rechecks_evidence_id", None) for e in problem_rows}
    return {
        "recheck": [
            (
                (e.evidence_id, why.reason, why.turn or why.window or why.uncited)
                if why
                else (e.evidence_id, RecheckReason.COUNTS, None)
            )
            for e, why in status
        ],
        # What each reason's text states, rendered as the block renders it.
        "recheck.text": [recheck_text(why) for _e, why in status],
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
            [e.evidence_id for e in problem_rows],
        ),
        # Shape 3 (#2811 S2b-1a): what the fix-verification block reads.
        "off_cause": off_cause_attempt(case, windows),
        "unrechecked": (
            [v for v in subjects.verifying if v in subjects.rows and v not in named]
            if cause_rows
            else []
        ),
        "bindable": (
            [e.evidence_id for e in problem],
            [e.evidence_id for e in cause],
        ),
        "held_need": held.need_id if held is not None else None,
        # Shape 4 (#2811 S2b-1b-i): why it holds, as the block's mark read it.
        "held_reason": (
            None
            if held is None
            else (
                PendingCheckReason.FULFILLED_BEFORE_ASK
                if held.state == NeedState.FULFILLED
                else PendingCheckReason.UNANSWERED
            )
        ),
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
    }


def recorded(outcome, case) -> dict:
    """The same facts, read off the record."""
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
    params = {
        RecheckReason.PREDATES_POSITING: cause.posited_turn,
        RecheckReason.PREDATES_FAILED_FIX: cause.latest_disconfirmation_turn,
        RecheckReason.OTHER_CAUSE: cause.other_cause_fix_turn,
        RecheckReason.NO_CONFIRMING_WINDOW: cause.no_confirming_window,
        RecheckReason.UNCITED: cause.uncited,
    }
    mitigation = outcome.mitigation
    resolution = outcome.resolution
    return {
        "recheck": [(i, code, params.get(code)) for i, code in cause.recheck.items()],
        "recheck.text": [
            recheck_text(_why(code, params.get(code)))
            for code in cause.recheck.values()
        ],
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
        "off_cause": resolution.off_cause_turn,
        "unrechecked": resolution.unrechecked_row_ids,
        "bindable": (outcome.bindable.problem_row_ids, outcome.bindable.cause_row_ids),
        "held_need": resolution.pending_check_need_id,
        "held_reason": resolution.pending_check_reason,
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
    }


def _why(code, param):
    """A ``RecheckWhy`` built from the record's fields alone."""
    if code == RecheckReason.COUNTS:
        return None
    if code == RecheckReason.NO_CONFIRMING_WINDOW:
        return RecheckWhy(code, window=param)
    if code == RecheckReason.UNCITED:
        return RecheckWhy(code, uncited=param)
    return RecheckWhy(code, turn=param)


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
    record_case_outcome(case, at=WritePoint.CYCLE)
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
    record_case_outcome(case, at=WritePoint.CYCLE)
    return case


async def _held_check_on_a_false_alarm():
    """The held check FULFILLED on rows from before its ask, then the problem
    found a false alarm: the finding supersedes no FULFILLED need, so the
    check still holds while readiness answers FALSE_ALARM first."""
    from faultmaven.core.investigation.problem_status import invalidate_problem
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _held_case,
    )

    case = _held_case()
    need = case.evidence_needs[-1]
    need.state = NeedState.FULFILLED
    need.fulfilling_evidence_ids = list(
        case.problem_verification.verifying_evidence_ids
    )
    invalidate_problem(
        case,
        evidence_ids=list(case.problem_verification.verifying_evidence_ids),
        basis="the scrape target was never down",
    )
    record_case_outcome(case, at=WritePoint.CYCLE)
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
    record_case_outcome(case, at=WritePoint.CYCLE)
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
    record_case_outcome(case, at=WritePoint.CYCLE)
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
    case.progress.outcome = compute_case_outcome(None, case, at=WritePoint.CYCLE)
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
    "held_check_on_a_false_alarm": (_held_check_on_a_false_alarm, set()),
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


# ---------------------------------------------------------------------------
# Each re-check reason, with every kind of its parameters, rendered from the
# record's fields alone (``recheck.text``), window 0 included
# ---------------------------------------------------------------------------


async def _held_open():
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _case,
    )

    case = _case("treatment")
    case.progress.cause_identification_held_open = True
    return case


async def _posited_later():
    from tests.unit.core.investigation.prompts.test_fix_verification_1906 import (
        _case,
    )

    case = _case("treatment")
    for node in case.causal_nodes.values():
        node.metadata["root_attached_turn"] = 5
    return case


async def _before_the_failed_fix():
    from tests.unit.core.investigation.test_declined_resolution_binds_the_model_1895 import (
        _engine_disconfirmation,
    )
    from tests.unit.core.investigation.test_resolution_backstop_turn import (
        _confirmed_case,
    )

    case = _confirmed_case()
    case.evidence.append(_engine_disconfirmation(case.current_turn + 2))
    case.current_turn += 2
    return case


async def _window_zero_failed(newer: bool):
    from faultmaven.modules.case.contracts import InvestigationActionType as Kind
    from tests.unit.core.investigation.test_window_zero_relapse_2501 import (
        _accept,
        _relapsed,
    )

    _eng, case = await _relapsed()
    if newer:
        _accept(case, Kind.SOLUTION, 8)
    return case


async def _fix_failed(newer: bool):
    from tests.unit.core.investigation.test_pending_check_holds_the_offer_1906 import (
        _relapsed,
    )

    case, _cause = await _relapsed(second_fix=newer)
    return case


async def _no_longer_identified():
    """#2036 P5: the renewal acted on a cause deepened past since, and a
    newer fix of the deeper cause is not re-checked yet."""
    from tests.unit.core.investigation.bound_rows import VERIFYING_ID
    from tests.unit.core.investigation.recorded_turns import _record
    from tests.unit.core.investigation.test_fix_targets_review_2036 import (
        _CAUSE_GONE,
        _PRESENT,
        _deepen,
        _engine,
        _fix,
        _identified_case,
        _obs,
        _ran,
    )

    eng = _engine()
    case, _a, hyp = _identified_case()
    renewal = [_fix("renew the certificate")]
    await _record(eng, case, 5, solutions_to_add=renewal, milestones=_ran())
    await _record(eng, case, 6, _obs(_PRESENT, "checkout still 503", VERIFYING_ID))
    r2 = await _deepen(eng, case, hyp, 7, "nothing rotates the certificate")
    cited = r2.metadata["root_terminal_evidence_ids"][0]
    await _record(eng, case, 8, _obs(_CAUSE_GONE, "a rotation job exists now", cited))
    rotation = [_fix("install the rotation CronJob", r2.node_id)]
    await _record(eng, case, 9, solutions_to_add=rotation, milestones=_ran())
    return case


async def _latest_failed():
    """No observed window failed or acted off-cause: the fix of A at 5 is
    never observed, A is deepened to B at 7, B re-checked gone at 8, and B's
    fix runs at 9."""
    from tests.unit.core.investigation.recorded_turns import _record
    from tests.unit.core.investigation.test_fix_targets_review_2036 import (
        _CAUSE_GONE,
        _deepen,
        _engine,
        _fix,
        _identified_case,
        _obs,
        _ran,
    )

    eng = _engine()
    case, _a, hyp = _identified_case()
    renewal = [_fix("renew the certificate")]
    await _record(eng, case, 5, solutions_to_add=renewal, milestones=_ran())
    r2 = await _deepen(eng, case, hyp, 7, "nothing rotates the certificate")
    cited = r2.metadata["root_terminal_evidence_ids"][0]
    await _record(eng, case, 8, _obs(_CAUSE_GONE, "a rotation job exists now", cited))
    rotation = [_fix("install the rotation CronJob", r2.node_id)]
    await _record(eng, case, 9, solutions_to_add=rotation, milestones=_ran())
    return case


async def _uncited(cited: bool):
    from tests.unit.core.investigation.test_cause_leg_cited_basis_2557 import (
        _fixed,
        _mirror,
        _recheck,
    )

    case, final, relief, _login, ttl = _mirror(cited=cited)
    _fixed(case, final)
    _recheck(case, (relief if cited else ttl).evidence_id)
    return case


async def _uncited_with_no_root():
    """#2557 F2: the cause fell, a candidate stands that is not identified."""
    from tests.unit.core.investigation.test_fix_verification_subjects_1906 import (
        _SUPPORT,
        _a_fell_then_b,
        _fix_b_and_recheck,
    )

    eng, case, _a, _b, _b_support = await _a_fell_then_b(1, False)
    await _fix_b_and_recheck(eng, case, _SUPPORT)
    return case


async def _other_cause():
    """A fix run against one identified root, and a re-check of the other."""
    from tests.unit.core.investigation.bound_rows import _row
    from tests.unit.core.investigation.golden_cases_2636 import (
        _second_validated_root,
    )
    from tests.unit.core.investigation.test_case_outcome_fold_2636 import _fix
    from tests.unit.core.investigation.test_m6_cause_gone_problem_persists_1927 import (
        _ROOT_ID,
        _eid,
        _identified_case,
        _recompute_at,
    )
    from tests.utils import terminal_verdict

    case, root, hyp = _identified_case()
    _second_validated_root(case)
    second = case.causal_nodes["cn_00000000b0b0"]
    second.metadata.update(terminal_verdict(_eid("pgb_log")))
    second.node_state, second.belief = root.node_state, root.belief
    second.validation_method = root.validation_method
    case.hypotheses["hyp_00000000b0b0"].state = hyp.state
    _recompute_at(case, 5)
    _fix(case, turn=6, target_ids=[_ROOT_ID], fix_kind="remediation")
    for label, named in (("pgbgone", "pgb_log"), ("poolgone", "pool_log")):
        _row(
            case,
            EvidenceCategory.CAUSAL_ABSENCE_EVIDENCE,
            turn=7,
            evidence_id=_eid(label),
            summary=f"{named} re-checked",
            rechecks=_eid(named),
        )
    _recompute_at(case, 7)
    return case


def _shows(case) -> set:
    """What a case's live derivation shows of each reason: its code, the
    kind of its parameters, and whether a missing window is window 0 or has a
    newer fix. Read live, so a record that lost a parameter fails the parity
    check below, not this premise."""
    shown = set()
    for _row, why in cause_recheck_status(case):
        if why is None:
            continue
        shown.add(why.reason.value)
        if why.window is not None:
            shown.add(why.window.kind.value)
            shown.add("window_0" if why.window.fix_turn is None else "window_n")
            newer = why.window.newer_fix_turn is not None
            shown.add("newer" if newer else "no_newer")
        if why.uncited is not None:
            shown.add(f"uncited.{why.uncited.kind.value}")
    return shown


#: Each fixture, and what it must show (``_shows``).
REASONS = {
    "held_open": (_held_open, {"held_open"}),
    "posited_later": (_posited_later, {"predates_positing"}),
    "before_the_failed_fix": (_before_the_failed_fix, {"predates_failed_fix"}),
    "off_cause_attempt": (_held_check_then_interception, {"off_cause_attempt"}),
    "window_zero_failed": (
        lambda: _window_zero_failed(False),
        {"failed", "window_0", "no_newer"},
    ),
    "window_zero_failed_newer": (
        lambda: _window_zero_failed(True),
        {"failed", "window_0", "newer"},
    ),
    "fix_failed_newer": (lambda: _fix_failed(True), {"failed", "window_n", "newer"}),
    "fix_failed": (lambda: _fix_failed(False), {"failed", "window_n", "no_newer"}),
    "no_longer_identified": (
        _no_longer_identified,
        {"no_longer_identified", "window_n", "newer"},
    ),
    "latest_failed": (_latest_failed, {"latest_failed"}),
    "uncited_cited": (lambda: _uncited(True), {"uncited", "uncited.cited"}),
    "uncited_no_citation": (
        lambda: _uncited(False),
        {"uncited", "uncited.no_citation"},
    ),
    "uncited_no_root": (_uncited_with_no_root, {"uncited", "uncited.no_root"}),
    "other_cause": (_other_cause, {"other_cause"}),
}


@pytest.mark.parametrize("name", sorted(REASONS))
async def test_each_reason_renders_from_the_record_as_it_does_live(name):
    build, shows = REASONS[name]
    case = await build()
    assert shows <= _shows(case), "premise: the fixture shows it"
    assert differences(case) == set()


def test_every_reason_and_kind_is_shown_by_a_fixture():
    from faultmaven.modules.case.contracts import NoWindowKind, UncitedKind

    shown = set().union(*(shows for _build, shows in REASONS.values()))
    expected = {
        *(r.value for r in RecheckReason if r not in _NOT_PARAMETRISED),
        *(k.value for k in NoWindowKind),
        *(f"uncited.{k.value}" for k in UncitedKind),
        "window_0",
        "window_n",
        "newer",
        "no_newer",
    } - {RecheckReason.NO_CONFIRMING_WINDOW.value}
    assert expected <= shown


#: Reasons with no parameters and no fixture of their own here.
_NOT_PARAMETRISED = {
    RecheckReason.COUNTS,
    RecheckReason.DISCONFIRMATION,
    RecheckReason.OUTSIDE_WINDOW,
}
