"""Phase 3c — context builder time-window rerank.

Covers:

- ``_extract_time_window_from_query`` recognises the supported phrasings
  and returns None on unrecognised text.
- ``_coverage_overlaps_window`` semantics — overlap, disjoint, NULL
  coverage excluded.
- End-to-end: when the feature flag is on and the user turn names a
  time window, evidence whose coverage intersects the window outranks
  otherwise-higher-scoring evidence.
- Flag OFF preserves Phase 2 ranking (no rerank applied).

These tests pin the contract so a future change to the score weights
can't silently break the Phase 3c promise that "evidence matching a
user-named time window reaches Tier A".
"""

from datetime import UTC, datetime, time, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest

from faultmaven.core.investigation.prompts.context_builder import (
    _build_evidence_context,
    _coverage_overlaps_window,
    _extract_time_window_from_query,
    _score_evidence_for_tier_a,
)
from faultmaven.modules.case.domain.models import (
    Case,
    CaseState,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
    UploadedFile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ev(
    start: datetime | None = None,
    end: datetime | None = None,
    data_type: str = "logs",
    content: str = "structural index content " * 20,
    source_file_id: str = "file_aabb12345678",
) -> Evidence:
    # Map legacy `data_type` strings to EvidenceSourceType.
    source_map = {
        "logs": EvidenceSourceType.LOGS,
        "configuration": EvidenceSourceType.CONFIGURATION,
        "metrics": EvidenceSourceType.METRICS,
        "code": EvidenceSourceType.CODE,
    }
    source_type = source_map.get(data_type, EvidenceSourceType.LOGS)
    ev = Evidence(
        evidence_id=f"ev_{uuid4().hex[:12]}",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="Test",
        summary="Test evidence",
        # Post-010: ``content`` is the file's structural index; production
        # writes it to uploaded_files.structural_index. _case() mirrors it
        # onto the synthesized UploadedFile.
        extract=None,
        source_type=source_type,
        source_file_id=source_file_id,
        collected_by="user",
        collected_at=datetime.now(UTC),
        collected_at_turn=1,
        coverage_start_ts=start,
        coverage_end_ts=end,
    )
    ev.__test_structural_index__ = content  # type: ignore[attr-defined]
    return ev


def _case(evidence_list: list[Evidence]) -> Case:
    # Synthesize the backing UploadedFile rows so the file-level
    # structural_index from the test fixture is visible to the context
    # builder (which reads from uploaded_files post-010).
    seen: set[str] = set()
    uploaded_files: list = []
    for ev in evidence_list:
        fid = getattr(ev, "source_file_id", None)
        if fid and fid not in seen:
            seen.add(fid)
            uploaded_files.append(
                UploadedFile(
                    file_id=fid,
                    filename=f"{ev.source_type.value}.dat",
                    size_bytes=128,
                    uploaded_at_turn=1,
                    structural_index=getattr(ev, "__test_structural_index__", None),
                )
            )
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        title="Test",
        description="Test",
        user_id="user_1",
        organization_id="org_1",
        state=CaseState.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Test",
        ),
        evidence=evidence_list,
        uploaded_files=uploaded_files,
    )


def _patch_flag(value: bool):
    class _Prep:
        timeline_rerank_enabled = value
        confidence_marker_enabled = False
        reclassify_enabled = False
        extractor_retry_enabled = False

    class _Settings:
        preprocessing = _Prep()

    return patch(
        "faultmaven.config.settings.get_settings",
        return_value=_Settings(),
    )


# ---------------------------------------------------------------------------
# _extract_time_window_from_query — pure parsing
# ---------------------------------------------------------------------------


class TestExtractTimeWindow:
    def test_between_range_recognised(self):
        ref = datetime(2026, 4, 23, 12, 0)
        win = _extract_time_window_from_query(
            "what happened between 14:30 and 14:45?", reference=ref
        )
        assert win is not None
        start, end = win
        assert (start.hour, start.minute) == (14, 30)
        assert (end.hour, end.minute) == (14, 45)

    def test_from_to_range_recognised(self):
        ref = datetime(2026, 4, 23, 12, 0)
        win = _extract_time_window_from_query(
            "errors from 09:00 to 09:15", reference=ref
        )
        assert win is not None
        start, end = win
        assert start.hour == 9 and start.minute == 0
        assert end.hour == 9 and end.minute == 15

    def test_at_point_collapses_to_single_instant(self):
        """Point queries return (ts, ts) — the overlap predicate treats
        this as a zero-length window, still matching any evidence
        covering that instant."""
        ref = datetime(2026, 4, 23, 12, 0)
        win = _extract_time_window_from_query("what happened at 14:30?", reference=ref)
        assert win is not None
        start, end = win
        assert start == end
        assert (start.hour, start.minute) == (14, 30)

    def test_iso_range_recognised(self):
        win = _extract_time_window_from_query(
            "errors 2026-04-23T14:00:00 to 2026-04-23T15:00:00"
        )
        assert win is not None
        start, end = win
        assert start == datetime(2026, 4, 23, 14, 0, 0)
        assert end == datetime(2026, 4, 23, 15, 0, 0)

    def test_no_time_phrase_returns_none(self):
        assert _extract_time_window_from_query("what caused the outage?") is None

    def test_empty_query_returns_none(self):
        assert _extract_time_window_from_query("") is None
        assert _extract_time_window_from_query(None) is None

    def test_malformed_hours_rejected(self):
        """Out-of-range clock values don't produce a valid window."""
        ref = datetime(2026, 4, 23, 12, 0)
        assert (
            _extract_time_window_from_query(
                "errors between 25:00 and 26:00", reference=ref
            )
            is None
        )


# ---------------------------------------------------------------------------
# _coverage_overlaps_window — overlap semantics
# ---------------------------------------------------------------------------


class TestCoverageOverlap:
    def test_fully_inside_window_overlaps(self):
        ev = _ev(
            start=datetime(2026, 4, 23, 14, 5),
            end=datetime(2026, 4, 23, 14, 25),
        )
        win = (
            datetime(2026, 4, 23, 14, 0),
            datetime(2026, 4, 23, 14, 30),
        )
        assert _coverage_overlaps_window(ev, win) is True

    def test_partial_overlap_matches(self):
        ev = _ev(
            start=datetime(2026, 4, 23, 14, 0),
            end=datetime(2026, 4, 23, 14, 20),
        )
        win = (
            datetime(2026, 4, 23, 14, 15),
            datetime(2026, 4, 23, 14, 30),
        )
        assert _coverage_overlaps_window(ev, win) is True

    def test_fully_before_window_does_not_match(self):
        ev = _ev(
            start=datetime(2026, 4, 23, 13, 0),
            end=datetime(2026, 4, 23, 13, 30),
        )
        win = (
            datetime(2026, 4, 23, 14, 0),
            datetime(2026, 4, 23, 14, 30),
        )
        assert _coverage_overlaps_window(ev, win) is False

    def test_null_coverage_never_overlaps(self):
        ev = _ev(start=None, end=None)
        win = (
            datetime(2026, 4, 23, 14, 0),
            datetime(2026, 4, 23, 14, 30),
        )
        assert _coverage_overlaps_window(ev, win) is False

    def test_tz_aware_and_naive_compared_ok(self):
        """Repository loads evidence with timezone-aware timestamps
        under Postgres; the rerank uses naive datetimes parsed from
        user text. The overlap check strips tzinfo so the two can be
        compared — ranking accuracy is acceptable across timezones
        because this is a nudge, not a filter."""
        ev = _ev(
            start=datetime(2026, 4, 23, 14, 0, tzinfo=timezone.utc),
            end=datetime(2026, 4, 23, 14, 30, tzinfo=timezone.utc),
        )
        win = (
            datetime(2026, 4, 23, 14, 15),  # naive
            datetime(2026, 4, 23, 14, 45),  # naive
        )
        assert _coverage_overlaps_window(ev, win) is True


# ---------------------------------------------------------------------------
# End-to-end: flag on promotes matching evidence to Tier A
# ---------------------------------------------------------------------------


class TestRerankScoring:
    """Pin the scoring weights directly. The Tier A selection is a
    sorted(... by score) call — if scoring is correct, selection is
    correct, and the display layer's ordering (which preserves the
    evidence list's original order within a tier) is a separate
    concern."""

    def test_matching_evidence_outscores_higher_type_evidence(self):
        """The +4 coverage bonus must exceed the +2 log-over-config
        type gap so a matching config beats a non-matching log."""
        matching_config = _ev(
            start=datetime(2026, 4, 23, 14, 0),
            end=datetime(2026, 4, 23, 14, 30),
            data_type="configuration",
        )
        unrelated_log = _ev(
            start=datetime(2026, 4, 23, 20, 0),
            end=datetime(2026, 4, 23, 20, 30),
            data_type="logs",
        )
        case = _case([matching_config, unrelated_log])
        window = (
            datetime(2026, 4, 23, 14, 0),
            datetime(2026, 4, 23, 14, 30),
        )

        config_score = _score_evidence_for_tier_a(
            matching_config, case, time_window=window
        )
        log_score = _score_evidence_for_tier_a(unrelated_log, case, time_window=window)

        assert config_score > log_score

    def test_no_window_matches_type_priority(self):
        """Without a window, log beats config on type bonus —
        confirms the rerank only fires when a window is present."""
        config_ev = _ev(
            start=datetime(2026, 4, 23, 14, 0),
            end=datetime(2026, 4, 23, 14, 30),
            data_type="configuration",
        )
        log_ev = _ev(
            start=datetime(2026, 4, 23, 20, 0),
            end=datetime(2026, 4, 23, 20, 30),
            data_type="logs",
        )
        case = _case([config_ev, log_ev])

        config_score = _score_evidence_for_tier_a(config_ev, case, time_window=None)
        log_score = _score_evidence_for_tier_a(log_ev, case, time_window=None)

        assert log_score > config_score

    def test_non_matching_evidence_not_boosted(self):
        """A log whose coverage doesn't overlap the window must not
        receive the +4 bonus."""
        non_matching_log = _ev(
            start=datetime(2026, 4, 23, 20, 0),
            end=datetime(2026, 4, 23, 20, 30),
            data_type="logs",
        )
        case = _case([non_matching_log])
        window = (
            datetime(2026, 4, 23, 14, 0),
            datetime(2026, 4, 23, 14, 30),
        )

        with_window = _score_evidence_for_tier_a(
            non_matching_log, case, time_window=window
        )
        without_window = _score_evidence_for_tier_a(
            non_matching_log, case, time_window=None
        )
        # Scores identical — no bonus applied when coverage is outside
        # the window.
        assert with_window == without_window

    def test_timeless_evidence_never_boosted(self):
        """NULL coverage → overlap check returns False → no bonus,
        regardless of how well the window matches anything else."""
        timeless_config = _ev(start=None, end=None, data_type="configuration")
        case = _case([timeless_config])
        window = (
            datetime(2026, 4, 23, 14, 0),
            datetime(2026, 4, 23, 14, 30),
        )

        with_window = _score_evidence_for_tier_a(
            timeless_config, case, time_window=window
        )
        without_window = _score_evidence_for_tier_a(
            timeless_config, case, time_window=None
        )
        assert with_window == without_window


class TestContextBuilderFlagIntegration:
    """End-to-end: the feature flag gates whether the rerank fires at
    all. Uses Tier A / Tier B membership (structural_index present vs
    absent) rather than intra-tier ordering, because the display layer
    preserves list-order within a tier."""

    def test_flag_off_matching_evidence_not_promoted_to_tier_a(self):
        """Three evidence items, all higher-type than our config
        (recent logs). Without rerank, the matching config is one of
        the three lowest-scoring items — it ends up in Tier B (summary
        only, no structural_index block). Pins that flag-OFF
        behaviour matches Phase 2."""
        # 3 log items — these fill Tier A on type bonus alone.
        fillers = [
            _ev(
                start=datetime(2026, 4, 23, 20, 0),
                end=datetime(2026, 4, 23, 20, 30),
                data_type="logs",
            )
            for _ in range(3)
        ]
        matching_config = _ev(
            start=datetime(2026, 4, 23, 14, 0),
            end=datetime(2026, 4, 23, 14, 30),
            data_type="configuration",
        )
        case = _case([matching_config] + fillers)

        with _patch_flag(False):
            out = _build_evidence_context(
                case, user_query="errors between 14:00 and 14:30"
            )

        # Config appears (summary) but NOT with its structural_index
        # block — Tier B placement.
        assert f'id="{matching_config.evidence_id}"' in out
        # Tier A items are emitted with `<structural_index>` tags; a
        # Tier B item's block is a single-line `<evidence ...>
        # <summary>...</summary></evidence>`. Find the tag pair.
        config_tag_start = out.find(f'id="{matching_config.evidence_id}"')
        # Look for structural_index *immediately* after (within 300 chars
        # of) the config tag; its absence means Tier B.
        local_window = out[config_tag_start : config_tag_start + 300]
        assert "<file_extract" not in local_window

    def test_flag_on_matching_evidence_promoted_to_tier_a(self):
        """Same setup, flag on — coverage match pushes the config into
        Tier A, which surfaces its structural_index."""
        # Coverage dates must align with how the query time window is
        # parsed. `_extract_time_window_from_query` anchors bare HH:MM
        # tokens to `datetime.now()` when no reference is provided, and
        # `_build_evidence_context` does not pass one — so the parsed
        # window lands on today's date. Evidence coverage must use
        # today too, otherwise there is no overlap and the +4 bonus
        # does not fire.
        today = datetime.now().date()
        fillers = [
            _ev(
                start=datetime.combine(today, time(20, 0)),
                end=datetime.combine(today, time(20, 30)),
                data_type="logs",
            )
            for _ in range(3)
        ]
        matching_config = _ev(
            start=datetime.combine(today, time(14, 0)),
            end=datetime.combine(today, time(14, 30)),
            data_type="configuration",
        )
        case = _case([matching_config] + fillers)

        with _patch_flag(True):
            out = _build_evidence_context(
                case, user_query="errors between 14:00 and 14:30"
            )

        # Anchored on the element, not on a fixed character window: the
        # promoted item is Tier A iff its own <evidence> element contains a
        # <file_extract>. A 300-char proximity window used to stand in for
        # that, and the fence attributes ate most of its margin (#1217) —
        # one more attribute on either tag would have turned it into a false
        # regression with nothing about the rerank having changed.
        config_tag_start = out.find(f'id="{matching_config.evidence_id}"')
        assert config_tag_start != -1, out
        element_end = out.find("</evidence", config_tag_start)
        assert element_end != -1, out
        # Phase 3c bonus ran → config is now in Tier A with structural_index.
        assert "<file_extract" in out[config_tag_start:element_end]

    def test_flag_on_no_time_phrase_behaves_like_flag_off(self):
        """User turn without a time phrase — rerank doesn't fire, and
        the matching config stays in Tier B just as it would with the
        flag off. Ensures no false promotion on turns that didn't ask
        for a time range."""
        fillers = [
            _ev(
                start=datetime(2026, 4, 23, 20, 0),
                end=datetime(2026, 4, 23, 20, 30),
                data_type="logs",
            )
            for _ in range(3)
        ]
        matching_config = _ev(
            start=datetime(2026, 4, 23, 14, 0),
            end=datetime(2026, 4, 23, 14, 30),
            data_type="configuration",
        )
        case = _case([matching_config] + fillers)

        with _patch_flag(True):
            out = _build_evidence_context(case, user_query="what caused the issue?")

        config_tag_start = out.find(f'id="{matching_config.evidence_id}"')
        local_window = out[config_tag_start : config_tag_start + 300]
        assert "<file_extract" not in local_window
