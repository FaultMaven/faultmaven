"""Phase 3a — coverage timestamps populated by preprocessor.

Covers:

- ``extract_time_range_ts`` returns datetime objects where
  ``extract_time_range`` previously only produced a string.
- ``PreprocessingService`` populates
  ``PreprocessingResult.coverage_start_ts`` / ``coverage_end_ts``
  from the raw content when the file has parseable timestamps.
- Timeless content (configs, code, short prose) leaves both NULL.
- ``EvidenceMetadata.coverage`` block is populated only when a
  start timestamp was parsed — absent for timeless evidence.

The Phase 3b repository query + agent tool depend on these fields
being populated, so this test pack pins the producer side before the
consumer side lands.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from faultmaven.models.api import DataType
from faultmaven.modules.preprocessing.extractors.utils import extract_time_range_ts
from faultmaven.modules.preprocessing.preprocessing_service import PreprocessingService


def _make_classifier(data_type: DataType = DataType.LOGS_AND_ERRORS):
    classifier = MagicMock()
    result = MagicMock()
    result.data_type = data_type
    result.confidence = 0.9
    result.source = "rule_based"
    result.source_type = None
    result.classification_failed = False
    result.suggested_types = None
    classifier.classify.return_value = result
    return classifier


def _make_extractor(output: str = "extracted"):
    ext = MagicMock()
    ext.strategy_name = "crime_scene"
    ext.llm_calls_used = 0
    ext.extract.return_value = output
    return ext


# ---------------------------------------------------------------------------
# extract_time_range_ts — producer helper
# ---------------------------------------------------------------------------


class TestExtractTimeRangeTs:
    def test_iso8601_log_bounds(self):
        """``extract_time_range_ts`` scans the first 10 and last 10
        lines; the tail scan only fires when the total line count
        exceeds 10. Use a longer log so head and tail are disjoint."""
        lines = ["2026-04-23T14:00:00 INFO service started"]
        # Padding with filler lines so head (first 10) and tail (last 10)
        # don't overlap — otherwise end_ts stays None.
        lines.extend([f"2026-04-23T14:{m:02d}:00 DEBUG noise" for m in range(1, 15)])
        lines.append("2026-04-23T14:59:59 INFO service stopped")
        content = "\n".join(lines)
        start, end = extract_time_range_ts(content)
        assert start == datetime(2026, 4, 23, 14, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 4, 23, 14, 59, 59, tzinfo=timezone.utc)

    def test_no_timestamps_returns_none_pair(self):
        """Config files, code, short prose — no parseable timestamps.
        Must return (None, None) rather than raising."""
        content = "server:\n  port: 8080\n  workers: 4\n"
        start, end = extract_time_range_ts(content)
        assert start is None
        assert end is None

    def test_head_only_timestamp_returns_start_no_end(self):
        """A single timestamp in the head returns (ts, None) — the
        end-of-range hasn't been established. Callers who need a
        strict span check the end for None."""
        content = "2026-04-23T14:00:00 error\n" + "\n".join(["no timestamp here"] * 20)
        start, end = extract_time_range_ts(content)
        assert start is not None
        assert end is None


# ---------------------------------------------------------------------------
# PreprocessingService populates coverage timestamps on the result
# ---------------------------------------------------------------------------


class TestPreprocessingServiceCoverage:
    @pytest.mark.asyncio
    async def test_log_content_populates_coverage_timestamps(self):
        """Long log (head and tail disjoint) — both coverage bounds
        are populated. See extract_time_range_ts semantics: single-
        turn pastes without enough lines produce (start, None), which
        is also valid but tested separately."""
        lines = ["2026-04-23T14:00:00 INFO starting"]
        lines.extend([f"2026-04-23T14:{m:02d}:00 DEBUG noise" for m in range(1, 15)])
        lines.append("2026-04-23T14:59:59 INFO recovered")
        log_content = "\n".join(lines)

        classifier = _make_classifier(DataType.LOGS_AND_ERRORS)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        result = await service.classify_and_extract(content=log_content)

        assert result.coverage_start_ts == datetime(
            2026, 4, 23, 14, 0, 0, tzinfo=timezone.utc
        )
        assert result.coverage_end_ts == datetime(
            2026, 4, 23, 14, 59, 59, tzinfo=timezone.utc
        )

    @pytest.mark.asyncio
    async def test_timeless_content_leaves_coverage_null(self):
        """A config file has no timestamps → both coverage fields
        should be None. Represents the majority of evidence that is
        not time-bound (configs, source, short pastes, screenshots)."""
        config_content = (
            "database:\n  host: db.example.com\n  port: 5432\n"
            "service:\n  workers: 4\n"
        )
        classifier = _make_classifier(DataType.STRUCTURED_CONFIG)
        service = PreprocessingService(
            classifier=classifier,
            logs_extractor=_make_extractor(),
            config_extractor=_make_extractor(),
        )
        result = await service.classify_and_extract(content=config_content)
        assert result.coverage_start_ts is None
        assert result.coverage_end_ts is None


# ---------------------------------------------------------------------------
# EvidenceMetadata.coverage block — absent for timeless, present otherwise
# ---------------------------------------------------------------------------


class TestEvidenceMetadataCoverageBlock:
    @pytest.mark.asyncio
    async def test_coverage_block_present_when_timestamps_parsed(self):
        """Phase 1's metadata contract reserves a ``coverage`` key for
        Phase 3. When at least a start timestamp is parsed, the block
        must appear so downstream consumers (future: context builder
        rerank in 3c) can read the source-pattern signal."""
        log_content = "\n".join(["2026-04-23T14:00:00 INFO starting"] + ["filler"] * 20)
        classifier = _make_classifier(DataType.LOGS_AND_ERRORS)
        service = PreprocessingService(
            classifier=classifier, logs_extractor=_make_extractor()
        )
        result = await service.classify_and_extract(content=log_content)

        ev_meta = result.extraction_metadata["evidence_metadata"]
        assert "coverage" in ev_meta

    @pytest.mark.asyncio
    async def test_coverage_block_absent_when_timeless(self):
        """No timestamps → no coverage block — the namespaced contract
        says "a key is present iff the owning phase wrote it". Absent
        is the valid state for timeless evidence."""
        classifier = _make_classifier(DataType.STRUCTURED_CONFIG)
        service = PreprocessingService(
            classifier=classifier,
            logs_extractor=_make_extractor(),
            config_extractor=_make_extractor(),
        )
        result = await service.classify_and_extract(content="server:\n  port: 8080\n")

        ev_meta = result.extraction_metadata["evidence_metadata"]
        assert "coverage" not in ev_meta
