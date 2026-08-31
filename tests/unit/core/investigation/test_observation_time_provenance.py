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
    _file_observed_attr,
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


def _file(start=None, end=None, source="caller_declared") -> UploadedFile:
    f = UploadedFile(
        file_id="file_0123456789ab",
        filename="pasted-content-20260804T193617.txt",
        size_bytes=10,
        uploaded_at_turn=3,
    )
    f.coverage_start_ts = start
    f.coverage_end_ts = end
    # Provenance rides with the span (#1274). Defaulted so the span-focused
    # fixtures above read as one fact, not two.
    f.coverage_source = source
    return f


# -- the writer that never existed -------------------------------------------
def test_evidence_inherits_its_source_files_coverage():
    f = _file(_ALERT_POSTED, _ALERT_POSTED)
    start, end, source = _evidence_coverage(_case([f]), f.file_id)
    assert (start, end) == (_ALERT_POSTED, _ALERT_POSTED)
    # The provenance rides along: a row inheriting an epoch_s guess is exactly
    # as unfounded as the file it came from.
    assert source == "caller_declared"


def test_fileless_evidence_has_unknown_coverage():
    """A chat-quoted row has no file to inherit from. Unknown must stay
    unknown — defaulting to now would assert currency nobody established."""

    assert _evidence_coverage(_case(), None) == (None, None, None)


def test_unresolvable_source_file_yields_unknown_coverage():
    """Mirrors the source guard: a hallucinated id must not crash the writer."""

    assert _evidence_coverage(_case(), "file_ffffffffffff") == (None, None, None)


def test_file_without_parseable_timestamps_yields_unknown_coverage():
    f = _file(None, None)
    assert _evidence_coverage(_case([f]), f.file_id) == (None, None, None)


# -- rendering: the model has to be able to READ the age ----------------------
class _Ev:
    def __init__(self, end, source="caller_declared"):
        self.coverage_end_ts = end
        self.coverage_source = source


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


# -- provenance: WHICH pattern produced the span decides what may be said ------
def test_a_fabricated_year_is_named_as_a_different_source():
    """The BSD-syslog handler invents a year when the line has none. Reporting
    that under the same name as a dated line is the information loss #1274 is
    about — it happens here, at the parse."""

    from faultmaven.modules.preprocessing.extractors.utils import (
        extract_time_range_ts,
    )

    assert extract_time_range_ts("Jun 14 15:16:01 host sshd[1]: x")[2] == (
        "syslog_bsd_noyear"
    )
    assert extract_time_range_ts("Jun 14 15:16:01 2024 host sshd[1]: x")[2] == (
        "syslog_bsd"
    )


def test_mis_parsed_epoch_coverage_is_named_epoch_s():
    """Pins the provenance of the 29-year config span, not just its shape."""

    from faultmaven.modules.preprocessing.extractors.utils import (
        extract_time_range_ts,
    )

    assert (
        extract_time_range_ts("serverId: 1234567890\nmaxBytes: 2147483647\n")[2]
        == "epoch_s"
    )


# -- the contract the model is given for reading any of it --------------------
def test_the_prompt_defines_the_pair_it_renders():
    """Rendering the attribute is half a fix. `fresh_this_turn` has had a stated
    rule since it shipped; `observed_through`/`age` had none, so the documented
    attribute had every reason to win the currency judgement — the original
    defect wearing a new attribute."""

    from faultmaven.core.investigation.prompts.templates import (
        _EVIDENCE_GROUNDING_BLOCK,
        INQUIRY_TEMPLATE,
    )

    # BOTH states. INV-07 keeps a forwarded alert un-promoted through INQUIRY,
    # so turn 1 — where the age decides whether there is an incident at all —
    # is rendered by the state whose template used to say nothing about it.
    for block in (INQUIRY_TEMPLATE, _EVIDENCE_GROUNDING_BLOCK):
        # the pair, and which half governs currency
        assert "observed_through" in block and "fresh_this_turn" in block
        assert "not a contradiction" in block
        # absence means unknown, never fresh — _observed_attr's own contract
        assert "UNKNOWN" in block
        # and the second element kind that carries them
        assert "<uploaded_file>" in block


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


def test_ranged_orphan_file_states_nothing():
    """``age`` is computed from the span END, so a dump covering 12:00-19:45
    whose symptom sits at 12:05 would read ``age="3h"`` — the staleness masking
    ``_evidence_coverage`` refuses ranged inheritance to prevent."""

    end = datetime.now(timezone.utc) - timedelta(hours=3)
    block = _orphan(_file(end - timedelta(hours=7), end))

    assert "observed_through=" not in block
    assert "age=" not in block


def test_the_two_renders_agree_on_what_may_be_claimed():
    """The block states exactly what an Evidence row citing the file would
    inherit. Anything wider is asserted on turn 1 and RETRACTED the turn the
    row is written, when _evidence_coverage refuses it."""

    point = datetime.now(timezone.utc) - timedelta(hours=7)
    for f in (
        _file(point, point),
        _file(point - timedelta(hours=7), point),
        _file(None, None),
    ):
        inheritable = _evidence_coverage(_case([f]), f.file_id)[:2] != (None, None)
        assert bool(_file_observed_attr(f)) is inheritable


def test_mis_parsed_epoch_coverage_never_reaches_the_prompt():
    """``extract_time_range_ts`` runs on every upload, ungated by data type,
    and its ``epoch_s`` pattern matches ordinary config integers. The point-span
    guard is what keeps the resulting 29-year span out of the prompt."""

    from faultmaven.modules.preprocessing.extractors.utils import (
        extract_time_range_ts,
    )

    start, end, _ = extract_time_range_ts(
        "serverId: 1234567890\nmaxBytes: 2147483647\n"
    )
    assert start is not None and start != end  # parsed as 2009-02-13 -> 2038-01-19
    assert "observed_through=" not in _orphan(_file(start, end))


# -- what each provenance licenses --------------------------------------------
def test_a_vouched_span_is_stated_plainly():
    stale = datetime.now(timezone.utc) - timedelta(hours=7)
    for source in ("caller_declared", "iso8601", "iso8601_t", "syslog_bsd", "epoch_s"):
        attr = _observed_attr(_Ev(stale, source))
        assert 'age="7h"' in attr, source
        assert "observed_basis" not in attr, source


def test_an_inferred_year_is_stated_with_its_uncertainty_not_withheld():
    """Classic syslog carries no year, so silence here would put the engine
    back to asking for a timestamp it very nearly has — #1271's failure, for
    one of the commonest formats a troubleshooting product ingests."""

    stale = datetime.now(timezone.utc) - timedelta(hours=7)
    attr = _observed_attr(_Ev(stale, "syslog_bsd_noyear"))

    assert 'age="7h"' in attr
    assert 'observed_basis="inferred_year"' in attr


def test_unrecorded_provenance_is_not_trusted():
    """NULL means nobody recorded where the span came from (rows predating the
    column). Unknown is not the same as fine."""

    assert _observed_attr(_Ev(datetime.now(timezone.utc), None)) == ""


def test_an_unclassified_pattern_defaults_to_withholding():
    """A pattern added to _TS_PATTERNS later must not start asserting instants
    just by existing — the change that adds it has to classify it."""

    assert _observed_attr(_Ev(datetime.now(timezone.utc), "some_new_pattern")) == ""


def test_a_known_observation_time_is_never_reported_as_missing():
    """The point of capturing `observed_at` is that the engine stops asking for
    the time. Verified on the onprem cluster (case_7bad3d1ac083): intake logged
    `Seeded coverage … observed_at 2026-08-31T04:13:38`, so the prompt carried
    `observed_through` with `age="3m"` — and the reply still said *"The pasted
    alert does not include the firing timestamp"*, sending the reporter to fetch
    a value the case already held.

    The old wording said not to ask for a time "the item already states". An
    alert's TEXT states no firing time; the observation time is a separate
    attribute, so the rule read as inapplicable and the model listed the
    timestamp as a gap. It must instead be told the question is answered.
    """

    import re

    from faultmaven.core.investigation.prompts.templates import (
        _EVIDENCE_GROUNDING_BLOCK,
        INQUIRY_TEMPLATE,
    )

    for block in (INQUIRY_TEMPLATE, _EVIDENCE_GROUNDING_BLOCK):
        text = re.sub(r"\s+", " ", block)
        # the question is answered, not open
        assert "treat it as answered, not missing" in text
        # and specifically must not be enumerated as absent data
        assert "Do NOT list a timestamp, firing time" in text
        # asking for startsAt is allowed, but only as a refinement
        assert "is a REFINEMENT when a precise duration" in text


def test_the_prompt_defines_the_inferred_marker():
    from faultmaven.core.investigation.prompts.templates import (
        _EVIDENCE_GROUNDING_BLOCK,
        INQUIRY_TEMPLATE,
    )

    for block in (INQUIRY_TEMPLATE, _EVIDENCE_GROUNDING_BLOCK):
        assert "observed_basis" in block
        assert "approximate" in block


# -- the false positive is prevented at the source, not distrusted downstream --
def test_bare_integers_are_not_read_as_dates_in_a_config():
    """`maxBytes: 2147483647` is a size. Gating the pattern by data type is why
    epoch_s can stay trusted for the logs where it is a real timestamp."""

    from faultmaven.modules.preprocessing.extractors.utils import (
        extract_time_range_ts,
    )

    config = "serverId: 1234567890\nmaxBytes: 2147483647\n"
    assert extract_time_range_ts(config, allow_bare_epoch=False) == (None, None, None)
    # ...and the same content ungated is exactly the 29-year span this prevents
    start, end, source = extract_time_range_ts(config)
    assert source == "epoch_s" and start.year == 2009 and end.year == 2038


def test_a_log_keeps_its_epoch_timestamps():
    """The cure must not be worse than the disease: epoch-formatted logs are
    common and their integers ARE timestamps."""

    from faultmaven.modules.preprocessing.extractors.utils import (
        extract_time_range_ts,
    )

    log = (
        "1700000000 service started\n"
        + "\n".join(["noise"] * 20)
        + "\n1700003600 service stopped"
    )
    assert extract_time_range_ts(log)[2] == "epoch_s"


def test_the_service_gates_by_data_type():
    from faultmaven.models.api import DataType
    from faultmaven.modules.preprocessing.preprocessing_service import (
        _NO_BARE_EPOCH_TYPES,
    )

    assert DataType.STRUCTURED_CONFIG in _NO_BARE_EPOCH_TYPES
    assert DataType.SOURCE_CODE in _NO_BARE_EPOCH_TYPES
    # stream-shaped types keep parsing epochs
    assert DataType.LOGS_AND_ERRORS not in _NO_BARE_EPOCH_TYPES
    assert DataType.METRICS_AND_PERFORMANCE not in _NO_BARE_EPOCH_TYPES
