"""The symptom's observation window must survive as a tracked fact.

The prompt has always said the symptom timeline "becomes the anchor for all
Zone 2 searches — every evidence request in Zone 2 references this window", and
in the same breath called it "an extracted fact, not a tracked variable". So it
lived in prose for one turn and vanished, and evidence requests defaulted to
the present: an investigation of a symptom observed two hours ago asks for
`--since=30m`, inspects a period the problem was never in, finds nothing.

This is a reading about WHERE TO LOOK. A problem is investigable while it
EXISTS — evidence collectible, cause unidentified, solution unknown —
regardless of whether it is firing right now.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.core.investigation.symptom_currency import (
    STALE_AFTER,
    SymptomCurrency,
    assess_symptom_currency,
    newest_symptom_observation,
)
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    ProblemVerification,
    TemporalState,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 8, 4, 19, 36, 17, tzinfo=timezone.utc)


def _case(*, verified=True, temporal=TemporalState.ONGOING, evidence=()) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="checkout 500s",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="checkout 500s",
            severity=CaseSeverity.HIGH,
            temporal_state=temporal,
        ),
    )
    case.progress.symptom_verified = verified
    case.evidence = list(evidence)
    return case


def _ev(category, observed) -> Evidence:
    return Evidence(
        summary="s",
        category=category,
        source_type=EvidenceSourceType.USER_DESCRIPTION,
        primary_purpose="p",
        collected_by="u",
        collected_at_turn=1,
        coverage_start_ts=observed,
        coverage_end_ts=observed,
    )


def _symptom(observed):
    return _ev(EvidenceCategory.SYMPTOM_EVIDENCE, observed)


# -- the defect ---------------------------------------------------------------
def test_old_symptom_observation_on_an_ongoing_problem_is_stale():
    """The failure this exists for, in its general form: the problem was shown
    to have EXISTED; nothing establishes it still does."""

    case = _case(evidence=[_symptom(NOW - timedelta(hours=2))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_recent_symptom_observation_is_current():
    case = _case(evidence=[_symptom(NOW - timedelta(minutes=5))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.CURRENT


def test_the_newest_observation_decides_not_the_oldest():
    """A stale row plus a fresh re-check reads CURRENT — re-verification is
    exactly the action the stale reading asks for, so it has to clear it."""

    case = _case(
        evidence=[
            _symptom(NOW - timedelta(hours=6)),
            _symptom(NOW - timedelta(minutes=2)),
        ]
    )
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.CURRENT


def test_boundary_is_not_stale_until_past_the_window():
    case = _case(evidence=[_symptom(NOW - STALE_AFTER)])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.CURRENT
    case = _case(evidence=[_symptom(NOW - STALE_AFTER - timedelta(seconds=1))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


# -- scoping: keyed on being under investigation ------------------------------
def test_a_historical_incident_still_needs_its_window():
    """An inactive incident is investigable while it EXISTS, and it needs the
    anchor MORE than a live one — there is no current state to fall back on."""

    case = _case(
        temporal=TemporalState.HISTORICAL,
        evidence=[_symptom(NOW - timedelta(days=30))],
    )
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_unset_temporal_state_does_not_drop_the_anchor():
    """``temporal_state`` is populated only when the LLM happened to emit
    preliminary_urgency during INQUIRY — unrelated to whether a window exists."""

    case = _case(temporal=None, evidence=[_symptom(NOW - timedelta(days=1))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_a_case_not_under_investigation_has_no_window_to_anchor():
    case = _case(evidence=[_symptom(NOW - timedelta(days=1))])
    case.state = CaseState.INQUIRY
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.NOT_APPLICABLE


def test_unverified_symptom_has_no_currency_question():
    case = _case(verified=False, evidence=[_symptom(NOW - timedelta(hours=9))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.NOT_APPLICABLE


# -- unknown is not an assurance ---------------------------------------------
def test_undated_evidence_is_distinct_from_current():
    """Most content has no parseable timestamps. That is the absence of an
    answer, and must never collapse into "recent"."""

    case = _case(evidence=[_symptom(None)])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.UNDATED


def test_no_symptom_evidence_at_all_is_undated_not_current():
    case = _case(evidence=[])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.UNDATED


# -- absence rows must not invert the reading ---------------------------------
def test_absence_evidence_is_not_read_as_a_symptom_observation():
    """A symptom-absence row evidences the problem is GONE. Counting it would
    make the strongest proof the problem stopped register as proof it is
    present — the exact inversion."""

    case = _case(
        evidence=[
            _symptom(NOW - timedelta(hours=4)),
            _ev(EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE, NOW),
        ]
    )
    assert newest_symptom_observation(case) == NOW - timedelta(hours=4)
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_causal_evidence_does_not_establish_symptom_currency():
    """Causal evidence explains WHY, not that the problem is still occurring."""

    case = _case(
        evidence=[
            _symptom(NOW - timedelta(hours=3)),
            _ev(EvidenceCategory.CAUSAL_EVIDENCE, NOW),
        ]
    )
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_naive_timestamps_do_not_raise():
    """SQLite round-trips coverage columns without tzinfo; a mixed set must not
    blow up prompt assembly."""

    case = _case(evidence=[_symptom(datetime(2026, 8, 4, 12, 0, 0))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


# -- what the model actually sees --------------------------------------------
def test_progress_indicator_no_longer_reports_a_bare_flag_when_stale():
    """The named defect: the indicator stated a conclusion while withholding
    everything needed to weigh it, so it read as settled fact."""

    from faultmaven.core.investigation.prompts.context_builder import (
        _symptom_currency_note,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(hours=2))])
    note = _symptom_currency_note(case, "symptom_verified")
    assert "investigation window" in note
    assert "--since=30m" in note  # names the concrete failure mode
    assert "not read a clean current-state reading as counter-evidence" in note


def test_undated_is_reported_as_unknown_not_as_recent():
    from faultmaven.core.investigation.prompts.context_builder import (
        _symptom_currency_note,
    )

    note = _symptom_currency_note(_case(evidence=[_symptom(None)]), "symptom_verified")
    assert "UNKNOWN" in note
    assert "not confirmed recent" in note


def test_other_indicators_are_untouched():
    from faultmaven.core.investigation.prompts.context_builder import (
        _symptom_currency_note,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(hours=2))])
    assert _symptom_currency_note(case, "root_cause_identified") == ""
    assert _symptom_currency_note(case, "solution_proposed") == ""


def test_inquiry_case_gets_no_note_at_all():
    from faultmaven.core.investigation.prompts.context_builder import (
        _symptom_currency_note,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(days=30))])
    case.state = CaseState.INQUIRY
    assert _symptom_currency_note(case, "symptom_verified") == ""


def test_zone2_emphasis_anchors_the_window_without_calling_the_case_dead():
    """Zone 2 asserted "Symptoms are confirmed" flatly on every remaining turn,
    with nothing to say about WHERE to look. It must now name the window — and
    must not imply a non-firing problem is not worth investigating."""

    from faultmaven.core.investigation.prompts.templates import (
        _get_diagnosis_focus_emphasis,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(hours=2))])
    text = _get_diagnosis_focus_emphasis(case.progress, case)
    assert "anchor to the symptom's window" in text.lower()
    assert "ABSOLUTE timestamps" in text
    assert "not counter-evidence" in text
    assert "while it EXISTS" in text
    assert "do not retract on it" in text


def test_zone2_emphasis_is_unchanged_when_current():
    from faultmaven.core.investigation.prompts.templates import (
        _get_diagnosis_focus_emphasis,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(minutes=1))])
    text = _get_diagnosis_focus_emphasis(case.progress, case)
    assert "Symptoms are confirmed" in text


def test_progress_only_callers_keep_working():
    """The optional ``case`` parameter must not change existing call sites."""

    from faultmaven.core.investigation.prompts.templates import (
        _get_diagnosis_focus_emphasis,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(hours=9))])
    assert "Symptoms are confirmed" in _get_diagnosis_focus_emphasis(case.progress)
