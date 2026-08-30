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
from faultmaven.core.investigation.prompts.context_builder import (
    _observed_attr,
    _render_orphan_file_block,
)
from faultmaven.core.investigation.prompts.fence import PromptFence, mint_token
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


# -- the render an UN-PROMOTED file gets --------------------------------------
# INV-07 forbids Evidence creation during INQUIRY, so a forwarded alert has no
# Evidence row on turn 1 and is rendered by _render_orphan_file_block — shared
# by the INQUIRY fallback, the INV-EC-1 current-turn floor and the Tier-D fill.
# Every assertion above is about a row this file may never become.
def _orphan(uf, turn=3, **kw) -> str:
    return _render_orphan_file_block(
        uf, {}, turn, fence=PromptFence(mint_token()), **kw
    )


def test_orphan_file_block_states_when_its_content_was_observed():
    """The case that reopened this: an alert forwarded 7h35m after it fired was
    rendered with ``fresh_this_turn="true"`` and nothing else, so the engine told
    the reporter it had no firing time — while the instant sat on the file."""

    posted = datetime.now(timezone.utc) - timedelta(hours=7, minutes=35)
    block = _orphan(_file(posted, posted))

    assert f'observed_through="{posted.isoformat()}"' in block
    assert 'age="7h"' in block


def test_orphan_file_block_renders_both_halves_of_the_pair():
    """``fresh_this_turn`` answers when the AGENT looked, ``observed_through``
    how old the observation is. Emitting the first alone is what made a stale
    alert read as current."""

    posted = datetime.now(timezone.utc) - timedelta(hours=2)
    block = _orphan(_file(posted, posted))

    assert 'fresh_this_turn="true"' in block
    assert "observed_through=" in block


def test_orphan_file_without_coverage_claims_nothing():
    """Same contract as the evidence tiers: absent means unknown, never fresh."""

    assert "observed_through=" not in _orphan(_file(None, None))


def test_degraded_orphan_render_keeps_the_observation_time():
    """``summary_only`` drops the body to fit the budget. Dropping the age with
    it would make a budget decision silently change what the file claims."""

    posted = datetime.now(timezone.utc) - timedelta(hours=7)
    block = _orphan(_file(posted, posted), summary_only=True)

    assert "file_extract" in block  # the degraded stub, not the content
    assert 'age="7h"' in block


def test_ranged_orphan_file_reports_the_end_of_its_own_span():
    """_evidence_coverage refuses to INHERIT a ranged span onto a narrower
    slice. Stating a file's own span on the file's own block asserts nothing
    about any slice, so it is rendered whatever its shape."""

    end = datetime.now(timezone.utc) - timedelta(hours=3)
    block = _orphan(_file(end - timedelta(hours=7), end))

    assert f'observed_through="{end.isoformat()}"' in block
    assert 'age="3h"' in block
