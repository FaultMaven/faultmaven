"""Problem verification must speak to the PRESENT while a case is investigated.

``symptom_verified`` recorded that the problem was shown to exist, never when,
and never fell once set. Any observation of the past — a log excerpt from
yesterday, a screenshot from last week, a notification captured hours ago —
satisfied it exactly as a live measurement would, and the investigation
proceeded as though the problem were happening now.

The read is keyed on the case being UNDER INVESTIGATION, not on the kind of
evidence and not on ``temporal_state``: an inactive problem is not a problem to
investigate, so investigating one presupposes it is live.
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


# -- scoping: keyed on being under investigation, not on temporal_state -------
def test_historical_tag_does_not_switch_the_question_off():
    """An inactive problem is not investigable at all — if it stopped, its cause
    was eliminated and a fix applied, and what remains is inquiry. So a
    HISTORICAL tag on a case that is nonetheless being INVESTIGATED is a
    contradiction to surface, not a reason to go quiet."""

    case = _case(
        temporal=TemporalState.HISTORICAL,
        evidence=[_symptom(NOW - timedelta(days=30))],
    )
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_unset_temporal_state_does_not_silently_opt_out():
    """``temporal_state`` is populated only when the LLM happened to emit
    preliminary_urgency during INQUIRY. Keying on it would let live cases skip
    the check for a reason that has nothing to do with the problem."""

    case = _case(temporal=None, evidence=[_symptom(NOW - timedelta(days=1))])
    assert assess_symptom_currency(case, now=NOW) is SymptomCurrency.STALE


def test_a_case_not_under_investigation_has_no_currency_question():
    """INQUIRY is where history and hypothesis are discussed; there is no live
    chain being walked, so there is nothing to keep current."""

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
    assert "STILL HAPPENING is not" in note
    assert "not one to investigate" in note


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


def test_zone2_emphasis_asks_for_re_confirmation_when_stale():
    """Zone 2 asserted "Symptoms are confirmed" flatly on every turn for the
    rest of the case."""

    from faultmaven.core.investigation.prompts.templates import (
        _get_diagnosis_focus_emphasis,
    )

    case = _case(evidence=[_symptom(datetime.now(timezone.utc) - timedelta(hours=2))])
    text = _get_diagnosis_focus_emphasis(case.progress, case)
    assert "re-confirm the symptom first" in text.lower()
    assert "symptom_verified=False" in text


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


# -- the exit: a stopped problem must close as a FINDING, not a failed hunt ---
class TestClosedNotReproduced:
    """`closed_insufficient_evidence` says "a real problem, cause not grounded",
    which presupposes the problem. Before this, a case whose problem turned out
    not to be occurring landed on whichever generic reason the hypothesis count
    happened to select — under the work gate `closed_after_investigation`
    (which says nothing), over it `closed_insufficient_evidence` (which credits
    a cause hunt that had nothing to find).
    """

    @staticmethod
    def _absence():
        from faultmaven.modules.case.contracts import Evidence, EvidenceSourceType

        return Evidence(
            summary="etcd reports three started members; all endpoints healthy",
            category=EvidenceCategory.SYMPTOM_ABSENCE_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            primary_purpose="p",
            collected_by="u",
            collected_at_turn=9,
        )

    def test_checked_and_absent_yields_the_finding(self):
        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )

        case = _case(verified=False, evidence=[self._absence()])
        assert derive_closure_reason(case) == "closed_not_reproduced"

    def test_never_looked_is_not_a_finding(self):
        """A case closed because the user stopped supplying data never verified
        its symptom either. Reporting that as "not reproduced" would assert a
        conclusion nobody reached — it must keep its previous generic reason."""

        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )

        case = _case(verified=False, evidence=[])
        assert derive_closure_reason(case) != "closed_not_reproduced"

    def test_a_still_verified_symptom_is_never_not_reproduced(self):
        """An absence row alongside a STANDING symptom claim is the ordinary
        post-fix re-verification, not a case that failed to reproduce."""

        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )

        case = _case(verified=True, evidence=[self._absence()])
        assert derive_closure_reason(case) != "closed_not_reproduced"

    def test_the_finding_outranks_the_insufficient_evidence_cell(self):
        """Both can hold at once; the finding is the more specific truth."""

        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )
        from faultmaven.modules.case.contracts import VerificationStatus

        case = _case(verified=False, evidence=[self._absence()])
        case.progress.verification_status = VerificationStatus.INSUFFICIENT_EVIDENCE
        assert derive_closure_reason(case) == "closed_not_reproduced"

    def test_inquiry_close_is_unaffected(self):
        from faultmaven.core.investigation.terminal_transitions import (
            derive_closure_reason,
        )

        case = _case(verified=False, evidence=[self._absence()])
        case.state = CaseState.INQUIRY
        assert derive_closure_reason(case) == "inquiry_only"

    def test_the_reason_is_accepted_by_the_case_validator(self):
        """VALID_CLOSURE_REASONS is enforced by a Pydantic validator; the DB
        column is a plain String(100) with no CHECK, so no migration."""

        from faultmaven.modules.case.domain.models import VALID_CLOSURE_REASONS

        assert "closed_not_reproduced" in VALID_CLOSURE_REASONS
