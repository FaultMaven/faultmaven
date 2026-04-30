"""Unit tests for ISS-030: content preview on classification_failed.

When the classifier returns classification_failed=True for a structured-data
file (CSV/TSV) or any other ambiguous text input, the preprocessing pipeline
must surface a small content preview (header columns + a sample data row, or
the first few text lines) inside the placeholder ``structural_index``. The
preview lets the agent describe *what* about the file is ambiguous when the
user asks q4-style questions ("what columns/structure was detected?").

The preview is NOT a re-classification or an extraction — it's a thin orienting
hint glued onto the existing placeholder text. The classifier's
``suggested_types`` and ``classification_failed`` flag are unchanged.

Run with:
    pytest tests/unit/modules/preprocessing/test_classification_failed_preview.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.preprocessing_service import (
    PreprocessingService,
)


@pytest.fixture
def mock_logs_extractor():
    extractor = MagicMock()
    extractor.strategy_name = "crime_scene"
    extractor.llm_calls_used = 0
    extractor.extract.return_value = "irrelevant — never called for failed paths"
    return extractor


@pytest.fixture
def real_classifier_service(mock_logs_extractor):
    """Service wired with the real DataClassifier so we exercise the
    classification_failed path end-to-end."""
    return PreprocessingService(
        classifier=DataClassifier(),
        logs_extractor=mock_logs_extractor,
    )


# Mirror of the fm-data-exam ambiguous-service-data.csv fixture — keeps the
# unit test self-contained without depending on the external test repo.
AMBIGUOUS_CSV = (
    "service,endpoint,region,latency_p99_ms,error_count,status,owner_team\n"
    "auth,/v1/login,us-east-1,145,3,degraded,platform\n"
    "auth,/v1/logout,us-east-1,89,0,ok,platform\n"
    "auth,/v1/refresh,us-east-1,112,1,ok,platform\n"
    "payments,/v1/charge,us-east-1,342,12,degraded,payments\n"
    "payments,/v1/refund,us-east-1,289,2,ok,payments\n"
    "payments,/v1/status,us-east-1,98,0,ok,payments\n"
    "notifications,/v1/send,us-west-2,201,5,ok,infra\n"
    "notifications,/v1/batch,us-west-2,1847,23,critical,infra\n"
    "search,/v1/query,eu-west-1,78,0,ok,search\n"
    "search,/v1/suggest,eu-west-1,45,0,ok,search\n"
    "search,/v1/index,eu-west-1,3421,8,degraded,search\n"
    "storage,/v1/upload,us-east-1,892,4,ok,platform\n"
    "storage,/v1/download,us-east-1,234,1,ok,platform\n"
    "storage,/v1/delete,us-east-1,67,0,ok,platform\n"
)


class TestClassificationFailedContentPreviewCsv:
    """The benchmark scenario: an ambiguous service-metrics-or-config CSV."""

    @pytest.mark.asyncio
    async def test_csv_preview_includes_column_names(self, real_classifier_service):
        """The placeholder structural_index must surface the CSV column names
        so the agent can cite them when explaining the ambiguity."""
        result = await real_classifier_service.classify_and_extract(
            content=AMBIGUOUS_CSV, filename="ambiguous-service-data.csv"
        )

        assert result.extraction_method == "classification_failed"
        idx = result.structural_index
        # Each header column from the fixture must appear verbatim — these are
        # the columns the standard answer expects the agent to cite.
        for column in (
            "service",
            "endpoint",
            "region",
            "latency_p99_ms",
            "error_count",
            "status",
            "owner_team",
        ):
            assert column in idx, (
                f"Expected column {column!r} to appear in structural_index "
                f"so the agent can describe what was detected, got:\n{idx}"
            )

    @pytest.mark.asyncio
    async def test_csv_preview_includes_sample_row(self, real_classifier_service):
        """At least one concrete data row must appear so the agent can ground
        its answer with example values (e.g. 'latency 145, status degraded')."""
        result = await real_classifier_service.classify_and_extract(
            content=AMBIGUOUS_CSV, filename="ambiguous-service-data.csv"
        )

        idx = result.structural_index
        # The first data row from the fixture: auth /v1/login ... 145 3 degraded
        # We pick a few distinctive cell values that would only appear if a
        # real data row was rendered (not just the header).
        assert "/v1/login" in idx or "auth" in idx
        assert "145" in idx or "342" in idx, (
            "Expected at least one numeric data cell in the preview so the "
            "agent can describe the metrics-like signal."
        )

    @pytest.mark.asyncio
    async def test_csv_preview_preserves_classification_failed_flag(
        self, real_classifier_service
    ):
        """The preview is additive — the classification_failed signal and the
        suggested_types must still propagate so the cooperative-clarification
        UX still fires."""
        result = await real_classifier_service.classify_and_extract(
            content=AMBIGUOUS_CSV, filename="ambiguous-service-data.csv"
        )

        meta = result.extraction_metadata or {}
        evidence_meta = meta.get("evidence_metadata", {})
        classification_meta = evidence_meta.get("classification", {})
        assert classification_meta.get("failed") is True
        # suggested_types list must still be populated (METRICS + CONFIG for CSV)
        suggested = classification_meta.get("suggested_types") or []
        assert len(suggested) >= 1

    @pytest.mark.asyncio
    async def test_csv_preview_is_bounded(self, real_classifier_service):
        """Preview must be small — never echo the whole file. ~5-10 lines max
        keeps the agent context budget intact."""
        result = await real_classifier_service.classify_and_extract(
            content=AMBIGUOUS_CSV, filename="ambiguous-service-data.csv"
        )

        idx = result.structural_index
        # Heuristic upper bound — the placeholder + a 5-10 line preview should
        # easily fit under 1500 chars. The full CSV is 621 bytes so anything
        # over ~2000 chars indicates the cap was bypassed.
        assert (
            len(idx) < 2000
        ), f"Preview should be bounded, got {len(idx)} chars:\n{idx}"


class TestClassificationFailedContentPreviewText:
    """Generic short ambiguous text (ISS-023 path) also benefits from a
    preview, though the columns concept doesn't apply."""

    @pytest.mark.asyncio
    async def test_short_text_preview_includes_first_lines(
        self, real_classifier_service
    ):
        """For non-tabular ambiguous text, the first few lines surface verbatim
        so the agent has something concrete to describe."""
        # ISS-023 fixture shape — short maintenance notice with mixed signals.
        ambiguous_text = (
            "Scheduled maintenance window\n"
            "When: 2026-04-30T02:00 UTC\n"
            "Contact: ops@example.com\n"
            "Runbook: https://wiki.example.com/maint\n"
            "Tag: v2.3.8\n"
        )
        result = await real_classifier_service.classify_and_extract(
            content=ambiguous_text, filename="notice.txt"
        )

        if result.extraction_method != "classification_failed":
            pytest.skip(
                "Classifier did not route this fixture through "
                "classification_failed; preview only applies on that path."
            )

        idx = result.structural_index
        # At least the leading distinctive line ("Scheduled maintenance window")
        # should be visible in the preview so the agent can cite it.
        assert "Scheduled maintenance" in idx or "maintenance" in idx.lower()


class TestClassificationFailedContentPreviewIsAdditive:
    """The preview must not break existing placeholder semantics."""

    @pytest.mark.asyncio
    async def test_preview_keeps_classification_uncertain_marker(
        self, real_classifier_service
    ):
        """The original 'Classification uncertain' line must still appear so
        anything downstream that greps for it (logs, UI, tests) keeps working."""
        result = await real_classifier_service.classify_and_extract(
            content=AMBIGUOUS_CSV, filename="ambiguous-service-data.csv"
        )

        assert "Classification uncertain" in result.structural_index
        assert "Suggested types" in result.structural_index

    @pytest.mark.asyncio
    async def test_preview_skipped_for_unanalyzable_path(self, real_classifier_service):
        """UNANALYZABLE (empty file) should NOT carry a content preview —
        there's nothing to preview, and it would muddy the 'file is empty'
        message."""
        result = await real_classifier_service.classify_and_extract(
            content="", filename="empty.bin"
        )

        # No 'Preview:' or 'Columns:' marker should appear on the empty path.
        idx = result.structural_index.lower()
        assert "preview" not in idx
        assert "columns:" not in idx

    @pytest.mark.asyncio
    async def test_preview_includes_explicit_marker(self, real_classifier_service):
        """The preview must be visually distinct in the placeholder — a label
        like 'Columns:' or 'Preview:' so the agent (and a human reading logs)
        can see where the orienting hint starts."""
        result = await real_classifier_service.classify_and_extract(
            content=AMBIGUOUS_CSV, filename="ambiguous-service-data.csv"
        )

        idx_lower = result.structural_index.lower()
        # Either label is acceptable — implementation can pick.
        assert "columns:" in idx_lower or "preview:" in idx_lower, (
            f"Expected an explicit preview/columns marker in:\n"
            f"{result.structural_index}"
        )
