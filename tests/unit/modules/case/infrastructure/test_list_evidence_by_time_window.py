"""Phase 3b — CaseRepository.list_evidence_by_time_window

Covers overlap semantics (coverage_start_ts <= end AND coverage_end_ts
>= start) plus the null-bound shortcuts. Uses the InMemoryCaseRepository
for isolation — SQL semantics are mirrored by the in-memory impl, so
testing there is both cheap and sufficient for the contract.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from faultmaven.modules.case.domain.models import (
    Case,
    CaseStatus,
    Evidence,
    EvidenceCategory,
    EvidenceSourceType,
    InquiryData,
)
from faultmaven.modules.case.infrastructure.case_repository import (
    InMemoryCaseRepository,
)


def _evidence(
    start: datetime | None,
    end: datetime | None,
    evidence_id: str | None = None,
    data_type: str = "logs",
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id or f"ev_{uuid4().hex[:12]}",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="Test",
        summary="Test evidence",
        preprocessed_content="...",
        content_size_bytes=100,
        data_type=data_type,
        source_type=EvidenceSourceType.LOGS,
        source_file_id="file_aabb12345678",
        preprocessing_method="crime_scene",
        extraction_method="crime_scene",
        collected_by="user",
        collected_at=datetime.now(UTC),
        collected_at_turn=0,
        coverage_start_ts=start,
        coverage_end_ts=end,
    )


def _case_with(*evidence: Evidence) -> Case:
    return Case(
        case_id=f"case_{uuid4().hex[:12]}",
        title="Test",
        description="Test",
        user_id="user_1",
        organization_id="org_1",
        status=CaseStatus.INVESTIGATING,
        inquiry=InquiryData(
            problem_statement_confirmed=True,
            decided_to_investigate=True,
            proposed_problem_statement="Test",
        ),
        evidence=list(evidence),
    )


@pytest.fixture
async def repo_with_cases():
    repo = InMemoryCaseRepository()

    # Evidence A covers 14:00–14:20.
    ev_a = _evidence(
        start=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
        end=datetime(2026, 4, 23, 14, 20, tzinfo=UTC),
        evidence_id="ev_aaaaaaaaaaaa",
    )
    # Evidence B covers 14:15–14:45 (overlaps with A, starts during).
    ev_b = _evidence(
        start=datetime(2026, 4, 23, 14, 15, tzinfo=UTC),
        end=datetime(2026, 4, 23, 14, 45, tzinfo=UTC),
        evidence_id="ev_bbbbbbbbbbbb",
    )
    # Evidence C covers 15:00–15:30 (disjoint from A and B).
    ev_c = _evidence(
        start=datetime(2026, 4, 23, 15, 0, tzinfo=UTC),
        end=datetime(2026, 4, 23, 15, 30, tzinfo=UTC),
        evidence_id="ev_cccccccccccc",
    )
    # Timeless evidence (config-style): NULL coverage.
    ev_config = _evidence(start=None, end=None, evidence_id="ev_cccccccccccf")

    case = _case_with(ev_a, ev_b, ev_c, ev_config)
    await repo.save(case)
    return repo, case


class TestListEvidenceByTimeWindow:
    @pytest.mark.asyncio
    async def test_point_query_returns_overlapping_evidence_only(self, repo_with_cases):
        """Query at 14:10 overlaps A only. B starts at 14:15, after the
        query point; C is in a different hour; config has NULL coverage."""
        repo, case = repo_with_cases
        point = datetime(2026, 4, 23, 14, 10, tzinfo=UTC)
        result = await repo.list_evidence_by_time_window(
            case.case_id, start=point, end=point
        )
        ids = [ev.evidence_id for ev in result]
        assert ids == ["ev_aaaaaaaaaaaa"]

    @pytest.mark.asyncio
    async def test_wide_window_returns_all_overlapping(self, repo_with_cases):
        """14:00–15:30 covers A, B, and C; timeless evidence excluded."""
        repo, case = repo_with_cases
        result = await repo.list_evidence_by_time_window(
            case.case_id,
            start=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
            end=datetime(2026, 4, 23, 15, 30, tzinfo=UTC),
        )
        ids = [ev.evidence_id for ev in result]
        assert ids == [
            "ev_aaaaaaaaaaaa",
            "ev_bbbbbbbbbbbb",
            "ev_cccccccccccc",
        ]
        assert "ev_cccccccccccf" not in ids

    @pytest.mark.asyncio
    async def test_no_bounds_returns_all_with_coverage(self, repo_with_cases):
        """Both start and end None → every evidence with non-NULL
        coverage, ordered by coverage_start_ts."""
        repo, case = repo_with_cases
        result = await repo.list_evidence_by_time_window(case.case_id)
        ids = [ev.evidence_id for ev in result]
        assert ids == [
            "ev_aaaaaaaaaaaa",
            "ev_bbbbbbbbbbbb",
            "ev_cccccccccccc",
        ]
        # Timeless evidence must never appear in time-window queries.
        assert "ev_cccccccccccf" not in ids

    @pytest.mark.asyncio
    async def test_end_only_returns_evidence_up_to_bound(self, repo_with_cases):
        """start=None, end=14:30 — evidence must start on or before
        14:30. A (14:00–14:20) and B (14:15–14:45) qualify; C (15:00+)
        does not."""
        repo, case = repo_with_cases
        result = await repo.list_evidence_by_time_window(
            case.case_id,
            end=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
        )
        ids = [ev.evidence_id for ev in result]
        assert ids == ["ev_aaaaaaaaaaaa", "ev_bbbbbbbbbbbb"]

    @pytest.mark.asyncio
    async def test_start_only_returns_evidence_ending_on_or_after_bound(
        self, repo_with_cases
    ):
        """end=None, start=14:30 — evidence must end on or after 14:30.
        A ends at 14:20 — excluded. B ends at 14:45 — included.
        C ends at 15:30 — included."""
        repo, case = repo_with_cases
        result = await repo.list_evidence_by_time_window(
            case.case_id,
            start=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
        )
        ids = [ev.evidence_id for ev in result]
        assert ids == ["ev_bbbbbbbbbbbb", "ev_cccccccccccc"]

    @pytest.mark.asyncio
    async def test_nonexistent_case_returns_empty(self):
        repo = InMemoryCaseRepository()
        result = await repo.list_evidence_by_time_window("case_missing")
        assert result == []

    @pytest.mark.asyncio
    async def test_case_with_no_time_bounded_evidence_returns_empty(self):
        """All-talk / all-timeless case — no evidence is returned by
        time-window queries. Callers must use get(case_id).evidence
        for the unfiltered view."""
        repo = InMemoryCaseRepository()
        case = _case_with(_evidence(start=None, end=None))
        await repo.save(case)
        result = await repo.list_evidence_by_time_window(case.case_id)
        assert result == []

    @pytest.mark.asyncio
    async def test_results_ordered_by_coverage_start(self, repo_with_cases):
        """Ordering matters — the agent tool (Phase 3b) relies on
        ascending coverage_start_ts so the LLM reads evidence in
        timeline order."""
        repo, case = repo_with_cases
        result = await repo.list_evidence_by_time_window(case.case_id)
        starts = [ev.coverage_start_ts for ev in result]
        assert starts == sorted(starts)
