"""#2100, Phase 4 lane B: the closure states the mitigation's record (with #2602).

The closure read the reason alone and said "stabilized by a verified
mitigation" whatever the record held: a five-minute reading, the user's own
word, a relapse after it. Now one helper, ``mitigation_closure_line``, states
the record (the declaration, the bound relief and its data window, the problem
seen again, the stall's proceed), and the reply, the rca-infeasible
propose-close and the report read it. A bound relapse after a declaration drops
``mitigation_sufficient`` (F5, ruled 2026-11-07), and a holding relief the
model has not registered is named in the ``<fix_verification>`` block (#2602,
ruled (b)). Real ``derive_closure_reason``, replies, report and gate; no live
LLM.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.core.investigation.cause_assurance import mitigation_observation
from faultmaven.core.investigation.milestone_engine.terminal_replies import (
    _CLOSED_REPLIES,
    _terminal_confirmation_response,
)
from faultmaven.core.investigation.prompts.context_builder.fix_verification_block import (
    build_fix_verification_block,
)
from faultmaven.core.investigation.terminal_transitions import (
    derive_closure_reason,
    mitigation_closure_line,
)
from faultmaven.modules.case.contracts import (
    CaseState,
    ClosureReason,
    EvidenceCategory,
    InvestigationActionType,
    ProblemStatus,
    ProposedAction,
    Solution,
    SolutionType,
)
from faultmaven.modules.case.domain.models.lifecycle import _CLOSURE_REASON_MEANINGS
from faultmaven.modules.report.domain.services.report_generation_service import (
    ReportGenerationService,
)
from tests.unit.core.investigation.bound_rows import bound_row, mitigated
from tests.unit.core.investigation.test_mitigation_record_2100 import (
    _accepted,
    _relief,
    _turn,
)
from tests.unit.core.investigation.test_refutation_needs_data_1911 import _case

pytestmark = pytest.mark.unit

_PRESENT = EvidenceCategory.SYMPTOM_EVIDENCE
_SUFFICIENT = ClosureReason.MITIGATION_SUFFICIENT
_T0 = datetime(2026, 11, 7, 8, 0, tzinfo=timezone.utc)
_RELIEVED_7 = "the problem was observed relieved after the mitigation (turn 7)"


def _mitigation(attempt: int = 6, *, relief: int | None = None):
    """A verified problem with a mitigation run at ``attempt``; with
    ``relief``, a bound re-check showing it gone that turn. Returns the case
    and the verifying row's id."""
    case = _case()
    shown = mitigated(case, attempt=attempt, relief=relief)
    return case, shown


def _declared(case, turn: int) -> None:
    record = case.progress.mitigation
    record.disposition, record.disposition_at_turn = "declared_stable", turn
    record.completed_at_turn = turn


def _closed(case, reason=None):
    reason = derive_closure_reason(case) if reason is None else reason
    return case.model_copy(update={"state": CaseState.CLOSED, "closure_reason": reason})


async def _report(case) -> str:
    svc = object.__new__(ReportGenerationService)
    return await svc._generate_closure_summary(case, {})


def _section(report: str, heading: str) -> str:
    start = report.index(heading)
    return report[start : report.index("\n## ", start + 1)]


# ---------------------------------------------------------------------------
# #2100's case, through the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "coverage, said",
    [
        (None, ""),
        (("iso8601", 5), ", and the data it cites covers about 5 minutes"),
        (("syslog_bsd_noyear", 5), ""),
    ],
    ids=["undated", "vouched", "unvouched"],
)
async def test_the_closure_states_the_relief_and_never_stabilized(coverage, said):
    """Proposed at 5, accepted at 6, a bound relief at 7 registered: the reply
    and the report state the relief and its turn, and the data window only
    when the row's own coverage is vouched. "stabilized" nowhere."""
    eng, case, shown = await _accepted()
    dated = {}
    if coverage is not None:
        source, minutes = coverage
        dated = dict(
            coverage_start_ts=_T0,
            coverage_end_ts=_T0 + timedelta(minutes=minutes),
            coverage_source=source,
        )
    bound_row(case, turn=7, rechecks=shown, **dated)
    await _turn(eng, case, 7, mitigation_verified=True)
    assert case.progress.mitigation.verified is True, "premise: registered"

    closed = _closed(case)
    assert closed.closure_reason is _SUFFICIENT
    reply = _terminal_confirmation_response(closed)
    assert reply == (
        f"Case closed — {_RELIEVED_7}{said}; root-cause analysis was deferred."
    )
    report = await _report(closed)
    assert _section(report, "## Mitigation Status") == (
        f"## Mitigation Status\n\nT{_RELIEVED_7[1:]}{said}.\n"
    )
    assert "Closed — a mitigation relieved the symptom; root-cause analysis" in report
    assert "stabiliz" not in reply + report


async def test_the_rca_infeasible_close_leads_with_the_relief():
    """The 3B propose-close states the record and asks to close, not to close
    "as stabilized" (C1)."""
    eng, case, shown = await _accepted()
    case.problem_verification.rca_infeasible = True
    case.problem_verification.rca_infeasible_rationale = "the vendor API is opaque"
    meta = await _turn(eng, case, 7, _relief(case, 7, shown), mitigation_verified=True)
    summary = (
        "The problem was observed relieved after the mitigation (turn 7). Since "
        "the vendor API is opaque, shall we close this case?"
    )
    assert case.offer_record.open.summary == summary
    assert meta["rca_infeasible_closure_message"] == summary


# ---------------------------------------------------------------------------
# The line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds, source, said",
    [
        (59, "iso8601", None),
        (60, "iso8601", "about 1 minute"),
        (0, "caller_declared", None),
        (-300, "iso8601", None),
        (119 * 60, "epoch_s", "about 119 minutes"),
        (2 * 3600, "iso8601", "about 2 hours"),
        (47 * 3600, "iso8601", "about 47 hours"),
        (90 * 86400, "iso8601", "about 90 days"),
        (3600, None, None),
    ],
    ids=[
        "under-the-floor",
        "the-floor",
        "instant",
        "negative",
        "minutes",
        "hours",
        "hours-under-two-days",
        "ninety-days",
        "unrecorded-source",
    ],
)
def test_the_span_has_a_floor_and_human_units(seconds, source, said):
    """The relief row's own vouched window, forward and at least a minute long,
    in minutes under 2 hours, hours under 2 days, then days (C2)."""
    case, shown = _mitigation()
    bound_row(
        case,
        turn=7,
        rechecks=shown,
        coverage_start_ts=_T0,
        coverage_end_ts=_T0 + timedelta(seconds=seconds),
        coverage_source=source,
    )
    line = mitigation_closure_line(case)
    if said is None:
        assert line == _RELIEVED_7
        assert mitigation_observation(case).span is None
    else:
        assert line == f"{_RELIEVED_7}, and the data it cites covers {said}"


def test_the_relief_row_is_the_one_its_coverage_dates_latest():
    """At the relief turn: the bound row whose vouched coverage ends latest,
    else the last recorded (C10). An unbound row is never it."""
    case, shown = _mitigation()
    dated = bound_row(
        case,
        turn=7,
        rechecks=shown,
        coverage_start_ts=_T0,
        coverage_end_ts=_T0 + timedelta(minutes=30),
        coverage_source="iso8601",
    )
    bound_row(case, turn=7, rechecks=shown, coverage_end_ts=_T0 + timedelta(days=1))
    seen = mitigation_observation(case)
    assert (seen.relief_turn, seen.relief_row_id) == (7, dated.evidence_id)
    assert seen.span == timedelta(minutes=30)

    case, shown = _mitigation()
    bound_row(case, turn=7, rechecks=shown)
    last = bound_row(case, turn=7, rechecks=shown)
    bound_row(case, turn=7, rechecks="ev_0000000000aa")  # names no row: unbound
    assert mitigation_observation(case).relief_row_id == last.evidence_id


def test_a_relief_after_a_later_fix_names_the_fix():
    """A dated SOLUTION between the attempt and the relief (C5)."""
    case, shown = _mitigation(attempt=4)
    case.proposed_actions.append(
        ProposedAction(
            case_id=case.case_id,
            action_type=InvestigationActionType.SOLUTION,
            description="raise the pool to 50",
            proposed_in_turn=5,
            state="accepted",
            accepted_in_turn=6,
        )
    )
    bound_row(case, turn=7, rechecks=shown)
    assert mitigation_closure_line(case) == (
        "the problem was observed relieved after the mitigation and a later fix "
        "(turn 7)"
    )


def test_no_line_without_an_accepted_mitigation():
    assert mitigation_closure_line(_case()) is None


@pytest.mark.parametrize("attempt", [4, None], ids=["dated", "legacy"])
def test_a_stored_verdict_with_no_bound_row_says_so(attempt):
    """The dev corpus's 17: ``verified`` stored, no bound row. It closes as
    stored, and the line says no bound re-check is on record; an attempt with
    no turn names none (C9, C10)."""
    case, _shown = _mitigation(attempt=4)
    case.progress.mitigation.verified = True
    if attempt is None:
        case.proposed_actions[-1].accepted_in_turn = None
    closed = _closed(case)
    assert closed.closure_reason is _SUFFICIENT
    turn = " (turn 4)" if attempt else ""
    assert _terminal_confirmation_response(closed) == (
        f"Case closed — a mitigation was applied{turn}; no re-check bound to the "
        "problem is on record; root-cause analysis was deferred."
    )


# ---------------------------------------------------------------------------
# F5: a relapse after the declaration (ruled 2026-11-07)
# ---------------------------------------------------------------------------


async def test_a_bound_relapse_after_the_declaration_drops_the_reason():
    """Declared at 7, seen again at 9: the generic reason, its reply carrying
    both facts. A relief at 11 restores ``mitigation_sufficient``."""
    case, shown = _mitigation(attempt=6)
    _declared(case, 7)
    assert derive_closure_reason(case) is _SUFFICIENT, "premise"
    bound_row(case, _PRESENT, turn=9, rechecks=shown)

    closed = _closed(case)
    assert closed.closure_reason is ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE
    reply = _terminal_confirmation_response(closed)
    assert reply == (
        f"{_CLOSED_REPLIES[ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE]} You reported "
        "the mitigation stable (turn 7); the problem was seen again at turn 9."
    )
    assert "You reported the mitigation stable (turn 7); the problem was seen " in (
        _section(await _report(closed), "## Mitigation Status")
    )

    bound_row(case, turn=11, rechecks=shown)
    assert derive_closure_reason(case) is _SUFFICIENT
    assert mitigation_closure_line(case) == (
        "you reported the mitigation stable (turn 7); the problem was observed "
        "relieved after the mitigation (turn 11); the problem was seen again at "
        "turn 9, before that relief"
    )


@pytest.mark.parametrize(
    "rows, sufficient",
    [
        ([(_PRESENT, 5)], True),
        ([(_PRESENT, 7)], True),
        ([(_PRESENT, 9), (EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, 9)], False),
    ],
    ids=["seen-before-the-declaration", "same-turn", "relief-and-present-tie"],
)
def test_the_relapse_edges(rows, sufficient):
    """The attempt at 4, declared at 7 (C3): a presence the user saw before
    declaring, or in the declaration's turn, stays their call; a relief and a
    presence in one later turn is no holding relief (lane A's tie rule)."""
    case, shown = _mitigation(attempt=4)
    _declared(case, 7)
    for category, turn in rows:
        bound_row(case, category, turn=turn, rechecks=shown)
    assert (derive_closure_reason(case) is _SUFFICIENT) is sufficient


async def test_the_stalls_proceed_is_said_in_the_reply_and_the_report():
    """Ruling (iv): the generic reason, and the proceeded clause."""
    case, _shown = _mitigation(attempt=6)
    record = case.progress.mitigation
    record.disposition, record.disposition_at_turn = "proceed_unmitigated", 8
    closed = _closed(case)
    assert closed.closure_reason is ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE
    proceeded = (
        "The investigation went on without the mitigation shown to hold (turn 8)."
    )
    assert _terminal_confirmation_response(closed).endswith(f"summary. {proceeded}")
    assert proceeded in _section(await _report(closed), "## Mitigation Status")


async def test_an_rca_infeasible_close_states_a_relapse_and_not_the_mitigation():
    """Its reply and label no longer assert the mitigation stands (C4); the
    line follows with the relapse."""
    case, shown = _mitigation(attempt=6, relief=7)
    case.progress.mitigation.verified = True
    case.problem_verification.rca_infeasible = True
    case.problem_verification.rca_infeasible_rationale = "the vendor API is opaque"
    bound_row(case, _PRESENT, turn=9, rechecks=shown)
    closed = _closed(case)
    assert closed.closure_reason is ClosureReason.CLOSED_RCA_INFEASIBLE
    assert _terminal_confirmation_response(closed) == (
        "Case closed — the root cause is not reachable for this problem. The "
        "problem was observed relieved after the mitigation (turn 7); the problem "
        "was seen again at turn 9."
    )
    report = await _report(closed)
    assert "## Closure Reason\n\nClosed — root cause structurally unreachable\n" in (
        report
    )


# ---------------------------------------------------------------------------
# Every reason's reply, with and without a record (C5)
# ---------------------------------------------------------------------------

_LINE = f"T{_RELIEVED_7[1:]}."
_WITH_RECORD = {
    ClosureReason.INQUIRY_ONLY: None,
    ClosureReason.CLOSED_FALSE_ALARM: None,
    ClosureReason.RESOLUTION_DECLINED: None,
    ClosureReason.SOLUTION_DEFERRED: "follows",
    ClosureReason.CLOSED_RCA_INFEASIBLE: "follows",
    ClosureReason.MITIGATION_SUFFICIENT: (
        f"Case closed — {_RELIEVED_7}; root-cause analysis was deferred."
    ),
    ClosureReason.CLOSED_RESTATEMENT_HELD: "follows",
    ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE: "follows",
}


@pytest.mark.parametrize("reason", list(ClosureReason))
def test_each_reasons_reply_with_and_without_a_record(reason):
    case, _shown = _mitigation(attempt=6, relief=7)
    expected = _WITH_RECORD[reason]
    if expected is None:
        expected = _CLOSED_REPLIES[reason]
    elif expected == "follows":
        expected = f"{_CLOSED_REPLIES[reason]} {_LINE}"
    reply = _terminal_confirmation_response(_closed(case, reason))
    assert reply == expected
    bare = _terminal_confirmation_response(_closed(_case(), reason))
    assert bare == _CLOSED_REPLIES[reason]
    assert "stabiliz" not in reply + bare


def test_the_record_free_fallback_claims_no_verdict():
    reply = _CLOSED_REPLIES[_SUFFICIENT]
    assert reply == (
        "Case closed — a mitigation was applied; root-cause analysis was deferred."
    )
    assert "verified" not in reply and "stabiliz" not in reply


async def test_a_declined_resolution_and_a_false_alarm_report_no_line():
    """A declined resolution keeps its Mitigation Status exclusion; a false
    alarm's section names the solutions, never a relief of a problem that was
    not present (C5)."""
    case, _shown = _mitigation(attempt=6, relief=7)
    case.solutions = [
        Solution(
            solution_type=SolutionType.WORKAROUND,
            title="Route orders to the replica",
            immediate_action="Point the order service at the replica.",
        )
    ]
    declined = await _report(_closed(case, ClosureReason.RESOLUTION_DECLINED))
    assert "## Mitigation Status" not in declined
    assert "observed relieved" not in declined

    false_alarm = await _report(_closed(case, ClosureReason.CLOSED_FALSE_ALARM))
    section = _section(false_alarm, "## Mitigation Status")
    assert "Route orders to the replica" in section
    assert "observed relieved" not in section


# ---------------------------------------------------------------------------
# #2602: a holding relief the model has not registered
# ---------------------------------------------------------------------------

_ASK = "is on record but unregistered: send mitigation_verified to record it"


async def test_the_block_names_an_unregistered_relief_until_it_is_registered():
    eng, case, shown = await _accepted()
    await _turn(eng, case, 7, _relief(case, 7, shown))
    row = case.evidence[-1].evidence_id
    block = build_fix_verification_block(case)
    assert f"  - relief row {row} (turn 7) {_ASK}" in block
    assert case.progress.mitigation.verified is False, "premise: the label unsent"

    await _turn(eng, case, 8, mitigation_verified=True)
    assert case.progress.mitigation.verified is True
    assert _ASK not in build_fix_verification_block(case)


@pytest.mark.parametrize(
    "shape", ["declared", "not_rechecked", "relapse", "revision_pending"]
)
async def test_no_ask_without_a_holding_relief_on_an_unsettled_record(shape):
    """A disposition settles it; no relief, or one the problem was seen after,
    has nothing to register; under a pending revision the gate refuses the
    label (C6)."""
    eng, case, shown = await _accepted()
    if shape != "not_rechecked":
        await _turn(eng, case, 7, _relief(case, 7, shown))
    if shape == "declared":
        await _turn(eng, case, 8, mitigation_disposition="declared_stable")
    elif shape == "relapse":
        await _turn(eng, case, 8, _relief(case, 8, shown, category=_PRESENT))
    elif shape == "revision_pending":
        case.progress.problem_status = ProblemStatus.REVISION_PENDING
    block = build_fix_verification_block(case)
    assert _ASK not in block
    if shape == "not_rechecked":
        assert "Mitigation: executed turn 6: not yet re-checked" in block
    if shape == "revision_pending":
        assert "problem re-checked relieved at turn 7" in block


# ---------------------------------------------------------------------------
# The published meanings (ruling (i))
# ---------------------------------------------------------------------------


def test_no_published_meaning_claims_a_verified_mitigation():
    meanings = " ".join(_CLOSURE_REASON_MEANINGS.values())
    assert "verified mitigation" not in meanings
    assert "as the user declared" in _CLOSURE_REASON_MEANINGS[_SUFFICIENT]
    assert "no mitigation that held" in (
        _CLOSURE_REASON_MEANINGS[ClosureReason.CLOSED_INSUFFICIENT_EVIDENCE]
    )
