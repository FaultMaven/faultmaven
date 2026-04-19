"""Unit tests for evidence-domain Prometheus metrics.

Covers:
- All 6 metrics defined per PLAN-evidence-failure-modes-implementation.md §M2
  are importable and have the expected name + type.
- `evidence_dedup_hits_total` increments when `_preprocess_attachment` hits
  the dedup short-circuit (integration with PLAN-content-hash-deduplication).
- When prometheus_client is unavailable, metrics fall back to no-op objects
  without raising.

Run with:
    pytest tests/unit/infrastructure/observability/test_evidence_metrics.py -v
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from faultmaven.core.investigation.schemas import Attachment, TurnPayload
from faultmaven.infrastructure.observability import evidence_metrics as m
from faultmaven.models.api_models import IntentType, QueryIntent
from faultmaven.modules.agent.domain.services.investigation_service import (
    InvestigationService,
)
from faultmaven.modules.case.domain.models import (
    Evidence,
    EvidenceCategory,
    EvidenceForm,
    EvidenceSourceType,
)

# Import test fixtures from the agent-module conftest
from tests.unit.modules.agent.conftest import (  # noqa: E402
    MockCaseRepository,
    MockMilestoneEngine,
    create_sample_case,
)

# ============================================================
# Registration / Type tests
# ============================================================


class TestEvidenceMetricsRegistration:
    """All six metrics are importable and expose the expected API."""

    def test_all_metrics_exported(self):
        for name in (
            "EVIDENCE_DEDUP_HITS_TOTAL",
            "EVIDENCE_ORPHAN_FILES_FOUND_TOTAL",
            "EVIDENCE_ORPHAN_FILES_DELETED_TOTAL",
            "EVIDENCE_TURN_ASYNC_RETRY_ENQUEUED_TOTAL",
            "EVIDENCE_TURN_ASYNC_RETRY_OUTCOME_TOTAL",
            "EVIDENCE_TURN_ASYNC_RETRY_LATENCY_SECONDS",
        ):
            assert hasattr(m, name), f"Metric {name} not exported"
            metric = getattr(m, name)
            # Works under both PROMETHEUS_AVAILABLE and no-op fallback paths
            assert (
                hasattr(metric, "inc")
                or hasattr(metric, "observe")
                or hasattr(metric, "labels")
            )

    @pytest.mark.skipif(
        not m.PROMETHEUS_AVAILABLE,
        reason="prometheus_client not installed; names not meaningful",
    )
    def test_metric_names_follow_convention(self):
        # Counters/Histograms expose their name via _name; labels become suffixes
        # like _total at scrape time. We verify the base names are faultmaven-prefixed.
        assert m.EVIDENCE_DEDUP_HITS_TOTAL._name.startswith("faultmaven_")
        assert m.EVIDENCE_ORPHAN_FILES_FOUND_TOTAL._name.startswith("faultmaven_")
        assert m.EVIDENCE_TURN_ASYNC_RETRY_ENQUEUED_TOTAL._name.startswith(
            "faultmaven_"
        )

    @pytest.mark.skipif(
        not m.PROMETHEUS_AVAILABLE,
        reason="prometheus_client not installed",
    )
    def test_labeled_counters_accept_labels(self):
        # Smoke: calling .labels() must not raise. No-op on actual value.
        m.EVIDENCE_TURN_ASYNC_RETRY_ENQUEUED_TOTAL.labels(reason="timeout")
        m.EVIDENCE_TURN_ASYNC_RETRY_OUTCOME_TOTAL.labels(outcome="success")


# ============================================================
# Dedup emission (integration with the dedup short-circuit)
# ============================================================


def _make_preprocessing_result(content_hash: str = "hash_xyz"):
    result = MagicMock()
    result.summary = "Log summary"
    result.structural_index = "ERROR line 1"
    result.data_type = MagicMock(value="logs")
    result.content_hash = content_hash
    result.extraction_method = "crime_scene"
    return result


def _make_existing_evidence(content_hash: str) -> Evidence:
    return Evidence(
        evidence_id="ev_abcdef123456",
        category=EvidenceCategory.SYMPTOM_EVIDENCE,
        primary_purpose="symptom_verified",
        summary="existing evidence",
        preprocessed_content="prior",
        content_size_bytes=100,
        preprocessing_method="crime_scene",
        source_type=EvidenceSourceType.LOGS,
        form=EvidenceForm.DOCUMENT,
        collected_by="user",
        collected_at_turn=2,
        content_hash=content_hash,
    )


class _DedupCapableRepo(MockCaseRepository):
    def __init__(self):
        super().__init__()
        self.find_by_content_hash = AsyncMock(return_value=None)


class TestDedupHitCounterEmission:
    """`evidence_dedup_hits_total` ticks exactly when the dedup path fires."""

    def _current(self) -> float:
        """Read the current counter value (0.0 when no emissions)."""
        if not m.PROMETHEUS_AVAILABLE:
            return 0.0
        return m.EVIDENCE_DEDUP_HITS_TOTAL._value.get()

    def _make_service(self, repo, file_storage=None):
        mock_preprocessing = AsyncMock()
        mock_preprocessing.classify_and_extract = AsyncMock(
            return_value=_make_preprocessing_result(content_hash="hash_xyz")
        )
        return InvestigationService(
            milestone_engine=MockMilestoneEngine(),
            case_repository=repo,
            preprocessing_service=mock_preprocessing,
            file_storage_service=file_storage,
        )

    @pytest.mark.asyncio
    async def test_counter_increments_on_dedup_hit(self):
        repo = _DedupCapableRepo()
        existing = _make_existing_evidence(content_hash="hash_xyz")
        repo.find_by_content_hash.return_value = existing
        case = create_sample_case()
        case.user_id = "user_owner"
        case.evidence.append(existing)
        await repo.save(case)

        before = self._current()

        service = self._make_service(repo, file_storage=AsyncMock())
        payload = TurnPayload(
            query="Analyze",
            attachments=[
                Attachment(
                    content=b"log data",
                    filename="app.log",
                    content_type="text/plain",
                )
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )
        await service.process_turn(case.case_id, "user_owner", payload)

        after = self._current()
        if m.PROMETHEUS_AVAILABLE:
            assert (
                after == before + 1
            ), f"Dedup counter did not increment: before={before}, after={after}"

    @pytest.mark.asyncio
    async def test_counter_does_not_increment_on_new_upload(self):
        repo = _DedupCapableRepo()
        repo.find_by_content_hash.return_value = None  # no match → new evidence
        case = create_sample_case()
        case.user_id = "user_owner"
        await repo.save(case)

        before = self._current()

        file_storage = AsyncMock()
        file_storage.store_file = AsyncMock(return_value={"file_path": "/stored/x"})
        service = self._make_service(repo, file_storage=file_storage)
        payload = TurnPayload(
            query="Analyze",
            attachments=[
                Attachment(
                    content=b"new log data",
                    filename="app.log",
                    content_type="text/plain",
                )
            ],
            intent=QueryIntent(type=IntentType.CONVERSATION),
        )
        await service.process_turn(case.case_id, "user_owner", payload)

        after = self._current()
        assert (
            after == before
        ), f"Counter incremented on new upload: before={before}, after={after}"
