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

The placeholder exists only for an upload of ``MIN_EXTRACTION_LINES`` lines or
more (#2355): a shorter uncertain upload passes through raw, so every fixture
here that must reach the placeholder is at least that long. The short path is
pinned in ``test_short_uncertain_upload_passthrough_2355.py``.

Run with:
    pytest tests/unit/modules/preprocessing/test_classification_failed_preview.py -v
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.classifier import DataClassifier
from faultmaven.modules.preprocessing.preprocessing_service import (
    MIN_EXTRACTION_LINES,
    PreprocessingService,
)
from tests.unit.modules.preprocessing.extraction_stubs import CrimeScene


@pytest.fixture
def real_classifier_service():
    """Service wired with the real DataClassifier so we exercise the
    classification_failed path end-to-end. Its extractor never runs on it."""
    return PreprocessingService(
        classifier=DataClassifier(),
        logs_extractor=CrimeScene(),
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

# The same export at a length where the type picks the extractor, so the
# uncertain verdict is put to the user as a placeholder (#2355). The header
# and the first data rows are the short fixture's, which is what the preview
# shows.
_DATA_ROWS = AMBIGUOUS_CSV.splitlines()[1:]
AMBIGUOUS_CSV_LONG = AMBIGUOUS_CSV + "".join(
    _DATA_ROWS[i % len(_DATA_ROWS)] + "\n" for i in range(MIN_EXTRACTION_LINES)
)


class TestClassificationFailedContentPreviewCsv:
    """The benchmark scenario: an ambiguous service-metrics-or-config CSV."""

    @pytest.mark.asyncio
    async def test_csv_preview_includes_column_names(self, real_classifier_service):
        """The placeholder structural_index must surface the CSV column names
        so the agent can cite them when explaining the ambiguity."""
        result = await real_classifier_service.classify_and_extract(
            content=AMBIGUOUS_CSV_LONG,
            filename="ambiguous-service-data.csv",
            principal="u1",
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
            content=AMBIGUOUS_CSV_LONG,
            filename="ambiguous-service-data.csv",
            principal="u1",
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
            content=AMBIGUOUS_CSV_LONG,
            filename="ambiguous-service-data.csv",
            principal="u1",
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
            content=AMBIGUOUS_CSV_LONG,
            filename="ambiguous-service-data.csv",
            principal="u1",
        )

        idx = result.structural_index
        # Heuristic upper bound — the placeholder + a 5-10 line preview should
        # easily fit under 1500 chars. The full CSV is several KB, so anything
        # over ~2000 chars indicates the cap was bypassed.
        assert (
            len(idx) < 2000
        ), f"Preview should be bounded, got {len(idx)} chars:\n{idx}"


class TestClassificationFailedContentPreviewText:
    """Non-tabular uncertain text gets a preview too, though the columns
    concept doesn't apply. The ISS-023 notice itself is always under
    ``MIN_EXTRACTION_LINES`` and so never reaches the placeholder (#2355);
    this drives the text arm with a long upload the classifier is forced to
    be unsure about."""

    @pytest.mark.asyncio
    async def test_text_preview_includes_first_lines(self):
        """For non-tabular uncertain text, the first few lines surface verbatim
        so the agent has something concrete to describe."""
        text = "Scheduled maintenance window\n" + "".join(
            f"note {i}: see the runbook\n" for i in range(MIN_EXTRACTION_LINES)
        )
        svc = (
            TestClassificationFailedPlaceholderNaming._service_that_always_fails_classification()
        )
        result = await svc.classify_and_extract(
            content=text, filename="notice.txt", principal="u1"
        )

        assert result.extraction_method == "classification_failed"
        idx = result.structural_index
        assert "Preview:" in idx
        assert "Scheduled maintenance window" in idx
        # The preview is the first lines, not the file.
        assert f"note {MIN_EXTRACTION_LINES - 1}:" not in idx


class TestClassificationFailedContentPreviewIsAdditive:
    """The preview must not break existing placeholder semantics."""

    @pytest.mark.asyncio
    async def test_preview_keeps_classification_uncertain_marker(
        self, real_classifier_service
    ):
        """The original 'Classification uncertain' line must still appear so
        anything downstream that greps for it (logs, UI, tests) keeps working."""
        result = await real_classifier_service.classify_and_extract(
            content=AMBIGUOUS_CSV_LONG,
            filename="ambiguous-service-data.csv",
            principal="u1",
        )

        assert "Classification uncertain" in result.structural_index
        assert "Suggested types" in result.structural_index

    @pytest.mark.asyncio
    async def test_preview_skipped_for_unanalyzable_path(self, real_classifier_service):
        """UNANALYZABLE (empty file) should NOT carry a content preview —
        there's nothing to preview, and it would muddy the 'file is empty'
        message."""
        result = await real_classifier_service.classify_and_extract(
            content="", filename="empty.bin", principal="u1"
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
            content=AMBIGUOUS_CSV_LONG,
            filename="ambiguous-service-data.csv",
            principal="u1",
        )

        idx_lower = result.structural_index.lower()
        # Either label is acceptable — implementation can pick.
        assert "columns:" in idx_lower or "preview:" in idx_lower, (
            f"Expected an explicit preview/columns marker in:\n"
            f"{result.structural_index}"
        )


class TestClassificationFailedPlaceholderNaming:
    """#666 / #1198 review: the placeholder is NOT internal text.

    ``_build_placeholder_result`` writes it to both ``summary`` and
    ``structural_index``, so it is rendered into ``<summary>`` and
    ``<file_extract>`` for the model, and ``_handle_file_reclassification``
    appends it verbatim to ``agent_response``. Naming a route-minted
    ``pasted-content-<ts>.txt`` here put the leaked filename beside a
    sentence that had just called it "the text you pasted".

    The classifier is forced to fail rather than coaxed: a minted name ends
    in ``.txt``, which gives the real classifier enough confidence that no
    content in reach of a unit test drives it down the failed path — while
    in production an ambiguous paste does exactly that (see the staging case
    cited in tests/unit/modules/agent/test_file_reclassification_intent.py).
    Forcing the flag exercises the placeholder-building code under test and
    nothing else.
    """

    MINTED_PASTE = "pasted-content-20260709T105531.txt"
    MINTED_CAPTURE = "page-capture-20260709T105531.txt"

    @staticmethod
    def _service_that_always_fails_classification():
        real = DataClassifier()

        class _AlwaysUncertain(DataClassifier):
            def classify(self, *args, **kwargs):
                result = real.classify(*args, **kwargs)
                result.classification_failed = True
                result.confidence = 0.10
                return result

        return PreprocessingService(
            classifier=_AlwaysUncertain(),
            logs_extractor=CrimeScene(),
        )

    @pytest.mark.asyncio
    async def test_paste_placeholder_does_not_name_the_minted_file(self):
        svc = self._service_that_always_fails_classification()
        result = await svc.classify_and_extract(
            content=AMBIGUOUS_CSV_LONG, filename=self.MINTED_PASTE, principal="u1"
        )
        assert result.extraction_method == "classification_failed"
        assert self.MINTED_PASTE not in result.structural_index
        assert self.MINTED_PASTE not in (result.summary or "")
        assert "the text you pasted" in result.structural_index

    @pytest.mark.asyncio
    async def test_capture_placeholder_does_not_name_the_minted_file(self):
        svc = self._service_that_always_fails_classification()
        result = await svc.classify_and_extract(
            content=AMBIGUOUS_CSV_LONG, filename=self.MINTED_CAPTURE, principal="u1"
        )
        assert result.extraction_method == "classification_failed"
        assert self.MINTED_CAPTURE not in result.structural_index
        assert "the page you captured" in result.structural_index

    @pytest.mark.asyncio
    async def test_a_real_filename_is_still_quoted(self, real_classifier_service):
        """Unforced — this one reaches the failed path on its own."""
        result = await real_classifier_service.classify_and_extract(
            content=AMBIGUOUS_CSV_LONG,
            filename="ambiguous-service-data.csv",
            principal="u1",
        )
        assert result.extraction_method == "classification_failed"
        assert "'ambiguous-service-data.csv'" in result.structural_index

    @pytest.mark.asyncio
    async def test_a_users_prefix_colliding_file_is_still_quoted(self):
        """Rule 2 of the detection rule: the user named this one."""
        svc = self._service_that_always_fails_classification()
        result = await svc.classify_and_extract(
            content=AMBIGUOUS_CSV_LONG,
            filename="pasted-content-notes.txt",
            principal="u1",
        )
        assert result.extraction_method == "classification_failed"
        assert "'pasted-content-notes.txt'" in result.structural_index
