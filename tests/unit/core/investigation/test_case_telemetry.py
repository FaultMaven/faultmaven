"""Per-turn case-telemetry stream (#1142).

The stream exists to answer "did the ENGINE stall this case?", so the tests are
organised around the three ways that answer can be lost: the arms of the
progress decision going unrecorded, the event failing to reach a log at
production level, and transcript prose riding along into the aggregator.
"""

import hashlib
import logging
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from faultmaven.core.investigation.case_telemetry import (
    CASE_TELEMETRY_SCHEMA_VERSION,
    FIELD_ALLOWLIST,
    PROGRESS_ARM_KEYS,
    TELEMETRY_LOGGER_NAME,
    TurnPath,
    build_case_turn_event,
    collect_progress_arms,
    emit_case_turn,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseSeverity,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceNeed,
    EvidenceSourceType,
    Hypothesis,
    HypothesisCategory,
    HypothesisGenerationMode,
    HypothesisState,
    InquiryData,
    NeedState,
    ProblemVerification,
    TurnOutcome,
    UploadedFile,
)

pytestmark = pytest.mark.unit


def _case(current_turn: int = 5, turns_without_progress: int = 0) -> Case:
    case = Case(
        case_id=f"case_{uuid4().hex[:12]}",
        user_id="u",
        enterprise_id="o",
        title="t",
        description="d",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            proposed_problem_statement="db slow",
            problem_statement_confirmed=True,
            decided_to_investigate=True,
        ),
        problem_verification=ProblemVerification(
            symptom_statement="db slow", severity=CaseSeverity.HIGH
        ),
    )
    case.current_turn = current_turn
    case.turns_without_progress = turns_without_progress
    return case


def _hex(label: str, width: int = 12) -> str:
    return hashlib.md5(label.encode()).hexdigest()[:width]


def _file(case: Case, label: str, turn: int) -> UploadedFile:
    f = UploadedFile(
        file_id="file_" + _hex(label),
        filename=f"{label}.log",
        size_bytes=10,
        content_type="text/plain",
        content_hash=_hex(label, 16),
        uploaded_at_turn=turn,
    )
    case.uploaded_files.append(f)
    return f


def _evidence(case: Case, label: str, source_label: str | None, turn: int) -> None:
    case.evidence.append(
        Evidence(
            evidence_id="ev_" + _hex(label),
            source_file_id=("file_" + _hex(source_label)) if source_label else None,
            summary="s",
            primary_purpose="diagnosis",
            category=EvidenceCategory.CAUSAL_EVIDENCE,
            source_type=EvidenceSourceType.USER_DESCRIPTION,
            collected_by="llm",
            collected_at_turn=turn,
            collected_at=datetime.now(timezone.utc),
        )
    )


def _need(case: Case, label: str, state: NeedState, created_at_turn: int) -> None:
    case.evidence_needs.append(
        EvidenceNeed(
            need_id="eneed_" + _hex(label),
            case_id=case.case_id,
            purpose="causal_verification",
            request_text="rt",
            rationale="r",
            state=state,
            # The model refuses a FULFILLED need with nothing fulfilling it.
            fulfilling_evidence_ids=(
                ["ev_" + _hex(label + "-fulfil")]
                if state in (NeedState.FULFILLED, NeedState.PARTIALLY_MET)
                else []
            ),
            superseded_reason=(
                "motivating hypothesis terminal"
                if state == NeedState.SUPERSEDED
                else None
            ),
            created_at_turn=created_at_turn,
        )
    )


def _hyp(case: Case, label: str, state: HypothesisState) -> None:
    hid = "hyp_" + _hex(label)
    case.hypotheses[hid] = Hypothesis(
        hypothesis_id=hid,
        statement="a database theory",
        category=HypothesisCategory.DATABASE,
        state=state,
        generation_mode=HypothesisGenerationMode.OPPORTUNISTIC,
        rationale="r",
        generated_at_turn=1,
    )


# ---------------------------------------------------------------------------
# The arms of the progress decision
# ---------------------------------------------------------------------------


def test_every_arm_the_predicate_reads_is_recorded():
    """The event audits ``_check_if_progress_made``, so it must cover its arms.

    The keys are EXTRACTED from the predicate's source, not intersected with a
    list written here. That difference is the whole test: a hard-coded candidate
    set can only notice a key going missing from ``PROGRESS_ARM_KEYS``, and the
    direction that actually hurts is an arm being ADDED to the predicate — the
    turn it fires on then emits ``progress_made=True`` with every recorded arm
    0, which the counter-integrity rule reads as a lying counter and, for an
    engine-side arm, as an idle engine. Both are false accusations aimed at the
    engine this stream exists to judge.
    """
    import inspect
    import re

    # The module-level predicate, not the method: #1264 moved the reading out
    # so the service's consumed-turn backstop could score with the same
    # predicate, leaving ``MilestoneEngine._check_if_progress_made`` a thin
    # delegate whose source contains no arms at all. Reading the delegate would
    # make this guard silently vacuous.
    from faultmaven.core.investigation.milestone_engine import (
        check_if_progress_made,
    )

    src = inspect.getsource(check_if_progress_made)
    body = src[src.index('"""', src.index('"""') + 3) :]  # skip the docstring

    read_keys = set(re.findall(r'metadata\.get\(\s*"(\w+)"', body))
    # ``structural_keys`` is a list literal the predicate then loops over.
    listed = re.search(r"structural_keys = \[(.*?)\]", body, re.S)
    if listed:
        read_keys |= set(re.findall(r'"(\w+)"', listed.group(1)))
    # The outcome arm has no metadata key of its own — the predicate reads
    # ``outcome in (...)``. It is carried as the derived ``outcome_progress``.
    if "TurnOutcome." in body:
        read_keys.add("outcome_progress")
    read_keys.discard("outcome")

    assert len(read_keys) >= 8, f"predicate source did not parse: {read_keys}"
    missing = read_keys - set(PROGRESS_ARM_KEYS)
    assert not missing, (
        f"arms scored by _check_if_progress_made but not carried by the "
        f"telemetry event: {sorted(missing)}"
    )
    # Every predicate arm must also count toward attribution, or a turn the
    # engine advanced on reads as idle.
    from faultmaven.core.investigation.case_telemetry import (
        _ENGINE_ARM_KEYS,
        PREDICATE_ARM_KEYS,
    )

    assert read_keys <= set(PREDICATE_ARM_KEYS)
    unattributed = set(PREDICATE_ARM_KEYS) - _ENGINE_ARM_KEYS - {"novel_files_uploaded"}
    assert (
        not unattributed
    ), f"predicate arms attributed to neither side: {sorted(unattributed)}"


def test_an_outcome_only_progress_turn_is_not_reported_as_an_idle_engine():
    """The arm with no metadata key of its own.

    ``HYPOTHESIS_TESTED`` means the engine tested a hypothesis this turn and it
    came back neither validated nor refuted — real engine work that touches no
    artifact list. Before ``outcome_progress`` existed the row read
    ``progress_made=true`` with every arm 0 and ``engine_advanced=false``: a
    counter-integrity violation and an idle-engine flag, both on a healthy turn.
    """
    case = _case()
    for outcome in (TurnOutcome.HYPOTHESIS_TESTED, TurnOutcome.DATA_REQUESTED):
        event = build_case_turn_event(
            case,
            path=TurnPath.LLM,
            arms=collect_progress_arms({"outcome": outcome}),
            progress_made=True,
            outcome=outcome,
        )
        assert event["engine_advanced"] is True, outcome
        assert event["arms"]["outcome_progress"] == 1, outcome
        assert any(event["arms"].values()), outcome

    quiet = build_case_turn_event(
        case,
        path=TurnPath.LLM,
        arms=collect_progress_arms({"outcome": TurnOutcome.CONVERSATION}),
    )
    assert quiet["arms"]["outcome_progress"] == 0
    assert quiet["engine_advanced"] is False


def test_arms_are_counts_not_identifiers():
    """Cardinality, not identity: evidence and hypothesis ids join back to case
    content, and no rule over this stream needs them."""
    arms = collect_progress_arms(
        {
            "evidence_added": ["e1", "e2", "e3"],
            "status_transitioned": True,
            "hypothesis_evidence_links_applied": 2,
            "novel_files_uploaded": [],
        }
    )
    assert arms["evidence_added"] == 3
    assert arms["status_transitioned"] == 1
    assert arms["hypothesis_evidence_links_applied"] == 2
    assert arms["novel_files_uploaded"] == 0
    # Absent keys still report, so an arm is never missing from the row.
    assert set(arms) == set(PROGRESS_ARM_KEYS)


def test_attribution_separates_the_two_sides():
    """The whole point: a turn where the user supplied data and the engine did
    nothing is distinguishable from one where the engine advanced."""
    case = _case()
    user_only = build_case_turn_event(
        case,
        path=TurnPath.LLM,
        arms=collect_progress_arms(
            {"novel_files_uploaded": ["f1"], "files_uploaded": ["f1"]}
        ),
        progress_made=True,
    )
    assert user_only["user_supplied_new"] is True
    assert user_only["engine_advanced"] is False

    engine_only = build_case_turn_event(
        case,
        path=TurnPath.LLM,
        arms=collect_progress_arms({"novel_evidence_added": ["e1"]}),
        progress_made=True,
    )
    assert engine_only["user_supplied_new"] is False
    assert engine_only["engine_advanced"] is True


def test_engine_advanced_counts_a_need_raised_this_turn():
    """Raising a NEW outstanding need is engine work — it is one of the
    predicate's arms (via the DATA_REQUESTED outcome) and would otherwise read
    as an idle engine on a turn the engine spent asking."""
    case = _case(current_turn=4)
    _need(case, "n1", NeedState.PENDING, created_at_turn=4)
    event = build_case_turn_event(case, path=TurnPath.LLM, arms={})
    assert event["needs_raised_this_turn"] == 1
    assert event["engine_advanced"] is True


# ---------------------------------------------------------------------------
# Ledgers
# ---------------------------------------------------------------------------


def test_input_disposition_ledger_ages_undisposed_inputs():
    case = _case(current_turn=9)
    _file(case, "f1", turn=2)  # disposed
    _file(case, "f2", turn=7)  # undisposed, 2 turns old
    _file(case, "f3", turn=3)  # undisposed, 6 turns old
    _evidence(case, "e1", "f1", turn=2)

    event = build_case_turn_event(case, path=TurnPath.LLM)
    assert event["inputs_total"] == 3
    assert event["inputs_disposed"] == 1
    assert event["inputs_undisposed"] == 2
    assert event["oldest_undisposed_input_age"] == 6


def test_input_ledger_reports_zero_age_when_everything_is_disposed():
    """A stale max age on a fully-disposed case would flag a healthy engine."""
    case = _case(current_turn=9)
    _file(case, "f1", turn=2)
    _evidence(case, "e1", "f1", turn=3)
    event = build_case_turn_event(case, path=TurnPath.LLM)
    assert event["inputs_undisposed"] == 0
    assert event["oldest_undisposed_input_age"] == 0


def test_ask_ledger_counts_partially_met_as_outstanding():
    """A partially met need is still an open ask. Folding it into "fulfilled"
    would report the user as owing nothing while the engine is still blocked —
    which flips attribution to the wrong side."""
    case = _case(current_turn=8)
    _need(case, "n1", NeedState.PENDING, created_at_turn=3)
    _need(case, "n2", NeedState.PARTIALLY_MET, created_at_turn=6)
    _need(case, "n3", NeedState.FULFILLED, created_at_turn=2)
    _need(case, "n4", NeedState.SUPERSEDED, created_at_turn=2)

    event = build_case_turn_event(case, path=TurnPath.LLM)
    assert event["needs_total"] == 4
    assert event["needs_outstanding"] == 2
    assert event["needs_fulfilled"] == 1
    assert event["needs_superseded"] == 1
    assert event["oldest_outstanding_need_age"] == 5


def test_frontier_carries_state_histograms_not_node_lists():
    """The counterweight to a gameable ``engine_advanced`` ships in the same
    event, as counts — the grounding trace's per-node list is O(nodes) per event
    and O(nodes x turns) per case."""
    case = _case()
    for label, state in (
        ("a", HypothesisState.ACTIVE),
        ("b", HypothesisState.ACTIVE),
        ("c", HypothesisState.RETIRED),
    ):
        _hyp(case, label, state)
    event = build_case_turn_event(case, path=TurnPath.LLM)
    assert event["hypothesis_count"] == 3
    assert event["hypothesis_states"] == {"active": 2, "retired": 1}


# ---------------------------------------------------------------------------
# The content guard
# ---------------------------------------------------------------------------


def test_transcript_prose_is_dropped_not_emitted():
    """The natural way to extend this event is to lift a field off
    ``TurnProgress`` — which carries ``user_message_summary`` and
    ``agent_response_summary``, i.e. raw transcript text. The allowlist has to
    stop that, because a leak cannot be un-shipped."""
    from faultmaven.core.investigation.case_telemetry import _sanitize

    clean = _sanitize(
        {
            "case_id": "case_0123456789ab",
            "user_message_summary": "the database is refusing connections again",
            "agent_response_summary": "I think the pool is exhausted",
        }
    )
    assert clean == {"case_id": "case_0123456789ab"}


def test_the_name_allowlist_bites_on_its_own():
    """Separates the two halves of the guard.

    The prose test above is also caught by the token check, so on its own it
    cannot show the NAME allowlist does anything. These values are perfectly
    token-shaped and would sail through the value check — they are rejected
    only because nobody named them, which is the property that stops an
    identifier or a filename being added to the stream by a later refactor.
    """
    from faultmaven.core.investigation.case_telemetry import _sanitize

    assert _sanitize({"user_id": "user_9f31c2", "filename": "prod-db.log"}) == {}


def test_an_allowlisted_field_carrying_prose_is_still_dropped():
    """Naming a field is not enough — a summary assigned to an allowlisted key
    would sail straight through a name-only allowlist."""
    from faultmaven.core.investigation.case_telemetry import _sanitize

    clean = _sanitize({"gate_name": "the user has not yet provided the logs we need"})
    assert "gate_name" not in clean


def test_a_histogram_key_carrying_prose_is_dropped():
    """Histogram keys are data too. A bucket keyed by a free-text label is the
    one place prose could still ride out under a value-only check."""
    from faultmaven.core.investigation.case_telemetry import _sanitize

    clean = _sanitize(
        {
            "hypothesis_states": {
                "active": 2,
                "the connection pool is exhausted under load": 1,
            }
        }
    )
    assert clean["hypothesis_states"] == {"active": 2}


def test_one_bad_value_does_not_erase_the_whole_mapping():
    """Rejection is per entry, never per mapping.

    Dropping the entire ``arms`` dict over one malformed member ships a row with
    ``progress_made`` and NO ``arms`` key, which makes the counter-integrity
    rule silently UNEVALUABLE rather than false — strictly worse than one wrong
    count, and the exact failure ``build_case_turn_event``'s arm normalisation
    exists to prevent.
    """
    import datetime

    from faultmaven.core.investigation.case_telemetry import _sanitize

    clean = _sanitize(
        {
            "arms": {
                "novel_evidence_added": 2,
                "novel_files_uploaded": datetime.datetime.now(),
            },
            "progress_made": True,
        }
    )
    assert "arms" in clean, "one bad member erased the whole mapping"
    assert clean["arms"] == {"novel_evidence_added": 2}


def test_a_broken_builder_says_so_once_at_warning(caplog):
    """Failure isolation must not become failure INVISIBILITY.

    A builder broken by a renamed model field silences the stream, and a total
    absence of rows is indistinguishable from "no turns happened" — the same
    level-gate failure that made the DEBUG grounding trace useless. One WARNING
    per process says it out loud; the rest stay at DEBUG so a systematic break
    does not flood the log it is trying to appear in.
    """
    import faultmaven.core.investigation.case_telemetry as telemetry

    original = telemetry._emit_failure_reported
    telemetry._emit_failure_reported = False
    try:
        with caplog.at_level(logging.WARNING, logger=telemetry.__name__):
            emit_case_turn(object(), path=TurnPath.LLM)  # no .progress at all
            emit_case_turn(object(), path=TurnPath.LLM)
    finally:
        telemetry._emit_failure_reported = original

    warnings = [
        r
        for r in caplog.records
        if r.name == telemetry.__name__ and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1, "expected exactly one WARNING for a broken builder"
    assert not [r for r in caplog.records if r.name == TELEMETRY_LOGGER_NAME]


def test_real_gate_names_survive_the_guard():
    """The guard must not be so tight that it eats the field it protects."""
    from faultmaven.core.investigation.case_telemetry import _sanitize

    for gate in (
        "disposition",
        "gate1",
        "insufficient_evidence",
        "restatement_held",
        "not_yet_productive",
    ):
        assert _sanitize({"gate_name": gate}) == {"gate_name": gate}


def test_every_emitted_field_is_allowlisted_and_content_free():
    case = _case(current_turn=6, turns_without_progress=2)
    _file(case, "f1", turn=1)
    _need(case, "n1", NeedState.PENDING, created_at_turn=2)
    event = build_case_turn_event(
        case,
        path=TurnPath.LLM,
        arms=collect_progress_arms({"novel_files_uploaded": ["f1"]}),
        gate_name="insufficient_evidence",
        progress_made=True,
        outcome=TurnOutcome.DATA_REQUESTED,
        user_message_chars=120,
        attachment_count=1,
    )
    assert set(event) <= FIELD_ALLOWLIST
    assert event["schema_version"] == CASE_TELEMETRY_SCHEMA_VERSION
    for key, value in event.items():
        if isinstance(value, dict):
            assert all(isinstance(v, (int, float, bool)) for v in value.values()), key
        else:
            assert value is None or isinstance(value, (int, float, bool, str)), key


# ---------------------------------------------------------------------------
# Reaching a log at production level
# ---------------------------------------------------------------------------


def test_stream_survives_a_root_logger_raised_above_info(caplog):
    """The defect that made the pre-existing grounding trace useless was a level
    gate, not a missing field: 0 hits in 5,576 lines of a real run. Pinning the
    stream's own level is what stops that recurring."""
    root = logging.getLogger()
    original = root.level
    root.setLevel(logging.WARNING)
    try:
        with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
            emit_case_turn(_case(), path=TurnPath.LLM)
        assert [r for r in caplog.records if r.name == TELEMETRY_LOGGER_NAME]
    finally:
        # The level that was THERE, not a hardcoded WARNING. Restoring a
        # constant leaves the root logger permanently lowered for every test
        # that runs after this one in the same worker — the process-global
        # leak class that makes whole-suite runs flaky.
        root.setLevel(original)

    assert logging.getLogger(TELEMETRY_LOGGER_NAME).level == logging.INFO


def test_fields_ride_on_the_record_for_the_json_formatter(caplog):
    """``extra=`` is how the structlog ``ProcessorFormatter`` (via ``ExtraAdder``)
    renders these as top-level JSON keys, so the payload has to be ON the record
    rather than interpolated into the message."""
    with caplog.at_level(logging.INFO, logger=TELEMETRY_LOGGER_NAME):
        emit_case_turn(
            _case(current_turn=3, turns_without_progress=2),
            path=TurnPath.DETERMINISTIC,
            progress_made=False,
        )
    record = next(r for r in caplog.records if r.name == TELEMETRY_LOGGER_NAME)
    assert record.event == "case_turn"
    assert record.turn == 3
    assert record.turns_without_progress == 2
    assert record.path == "deterministic"


def test_emission_never_breaks_the_turn(caplog):
    """A diagnostic must not be able to fail the turn it observes."""

    class Broken:
        case_id = "case_0123456789ab"

        @property
        def progress(self):  # noqa: D401
            raise RuntimeError("half-built case")

    emit_case_turn(Broken(), path=TurnPath.LLM)  # must not raise
    assert not [r for r in caplog.records if r.name == TELEMETRY_LOGGER_NAME]
