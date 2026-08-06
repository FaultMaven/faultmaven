"""Observation time must survive from the caller to the evidence row.

The engine could only ever see WHEN THE AGENT LOOKED (``collected_at_turn``),
never how old the observation was. So a two-hour-old alert forwarded into a
case satisfied the symptom gate exactly as a live measurement would.

``Evidence.coverage_start_ts`` / ``coverage_end_ts`` and their DB index have
existed since the case-timeline work, and the model docstring has always
claimed the system fills them — but no writer ever did. These cover the writer,
the prompt rendering that makes the span readable, and the clock that makes any
of it interpretable.
"""

from datetime import datetime, timedelta, timezone

import pytest

from faultmaven.core.investigation.milestone_engine import _evidence_coverage
from faultmaven.core.investigation.prompts.context_builder import _observed_attr
from faultmaven.modules.case.contracts import (
    Case,
    CaseSeverity,
    CaseState,
    InquiryData,
    ProblemVerification,
    UploadedFile,
)

pytestmark = pytest.mark.unit

_ALERT_POSTED = datetime(2026, 8, 4, 17, 36, 17, tzinfo=timezone.utc)


def _case(uploaded=None) -> Case:
    case = Case(
        case_id="case_000000000001",
        user_id="u",
        organization_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        current_turn=5,
        inquiry=InquiryData(
            proposed_problem_statement="etcd quorum lost",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="etcd quorum lost", severity=CaseSeverity.HIGH
        ),
    )
    case.uploaded_files = uploaded or []
    return case


def _file(start=None, end=None) -> UploadedFile:
    f = UploadedFile(
        file_id="file_0123456789ab",
        filename="pasted-content-20260804T193617.txt",
        size_bytes=10,
        uploaded_at_turn=3,
    )
    f.coverage_start_ts = start
    f.coverage_end_ts = end
    return f


# -- the writer that never existed -------------------------------------------
def test_evidence_inherits_its_source_files_coverage():
    f = _file(_ALERT_POSTED, _ALERT_POSTED)
    start, end = _evidence_coverage(_case([f]), f.file_id)
    assert (start, end) == (_ALERT_POSTED, _ALERT_POSTED)


def test_fileless_evidence_has_unknown_coverage():
    """A chat-quoted row has no file to inherit from. Unknown must stay
    unknown — defaulting to now would assert currency nobody established."""

    assert _evidence_coverage(_case(), None) == (None, None)


def test_unresolvable_source_file_yields_unknown_coverage():
    """Mirrors the source guard: a hallucinated id must not crash the writer."""

    assert _evidence_coverage(_case(), "file_ffffffffffff") == (None, None)


def test_file_without_parseable_timestamps_yields_unknown_coverage():
    f = _file(None, None)
    assert _evidence_coverage(_case([f]), f.file_id) == (None, None)


# -- rendering: the model has to be able to READ the age ----------------------
class _Ev:
    def __init__(self, end):
        self.coverage_end_ts = end


def test_age_is_rendered_for_a_stale_observation():
    """The case that started this: an alert forwarded two hours after it fired
    must not look current in the prompt."""

    attr = _observed_attr(_Ev(datetime.now(timezone.utc) - timedelta(hours=2)))
    assert 'age="2h"' in attr
    assert "observed_through=" in attr


def test_age_units_scale():
    now = datetime.now(timezone.utc)
    assert 'age="30m"' in _observed_attr(_Ev(now - timedelta(minutes=30)))
    assert 'age="5h"' in _observed_attr(_Ev(now - timedelta(hours=5)))
    assert 'age="3d"' in _observed_attr(_Ev(now - timedelta(days=3)))


def test_unknown_coverage_renders_nothing_rather_than_claiming_freshness():
    """Absence must read as "unknown", so it has to be genuinely absent — an
    ``age="0m"`` default would be a fabricated assurance."""

    assert _observed_attr(_Ev(None)) == ""


def test_naive_coverage_is_read_as_utc_not_crashed_on():
    """SQLite round-trips these columns without tzinfo; comparing a naive
    datetime to an aware one raises, which would take down prompt assembly."""

    attr = _observed_attr(_Ev(datetime.utcnow() - timedelta(hours=1)))
    assert 'age="1h"' in attr


def test_future_coverage_withholds_the_age_instead_of_printing_a_negative():
    attr = _observed_attr(_Ev(datetime.now(timezone.utc) + timedelta(hours=1)))
    assert "observed_through=" in attr
    assert "age=" not in attr


# -- the clock that makes all of the above interpretable ----------------------
def test_prompt_states_the_current_time():
    """Without this the model cannot compute an age at all: its own sense of
    "now" is its training cutoff, and no timestamp in the prompt is anchored."""

    from faultmaven.core.investigation.prompts.context_builder import (
        build_investigation_context,
    )

    # Returns the assembled sections; the clock belongs with case identity, so
    # it survives every section-level budget trim.
    sections = build_investigation_context(_case(), "what is happening?")
    identity = sections["identity"]
    assert "CURRENT_TIME:" in identity
    assert str(datetime.now(timezone.utc).year) in identity
