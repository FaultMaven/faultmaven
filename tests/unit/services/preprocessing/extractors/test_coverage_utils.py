"""Tests for coverage metadata utilities in extractors/utils.py."""

import time

import pytest

from faultmaven.services.preprocessing.extractors.utils import (
    COVERAGE_SEPARATOR,
    extract_time_range,
    extract_timestamp,
    format_coverage_metadata,
)


@pytest.mark.unit
class TestExtractTimestamp:
    """Tests for extract_timestamp() — 5 formats + edge cases."""

    def test_iso8601_with_t(self):
        ts = extract_timestamp("2024-03-15T14:30:45 something happened")
        assert ts is not None
        assert ts.hour == 14
        assert ts.minute == 30
        assert ts.second == 45

    def test_iso8601_without_t(self):
        ts = extract_timestamp("2024-03-15 14:30:45 ERROR connection refused")
        assert ts is not None
        assert ts.year == 2024
        assert ts.month == 3
        assert ts.day == 15

    def test_syslog_bsd(self):
        ts = extract_timestamp("Mar 15 14:30:45 myhost sshd[1234]: Failed password")
        assert ts is not None
        assert ts.month == 3
        assert ts.day == 15
        assert ts.hour == 14

    def test_epoch_seconds(self):
        ts = extract_timestamp("1710510645 INFO startup complete")
        assert ts is not None
        assert ts.year >= 2024

    def test_epoch_milliseconds(self):
        ts = extract_timestamp("1710510645000 WARN high latency")
        assert ts is not None
        assert ts.year >= 2024

    def test_no_timestamp(self):
        assert extract_timestamp("just some random text") is None

    def test_empty_string(self):
        assert extract_timestamp("") is None

    def test_performance_under_1ms(self):
        line = "2024-03-15T14:30:45.123Z ERROR something broke"
        start = time.perf_counter()
        for _ in range(1000):
            extract_timestamp(line)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"1000 calls took {elapsed:.3f}s, expected <1s"


@pytest.mark.unit
class TestExtractTimeRange:
    """Tests for extract_time_range()."""

    def test_both_endpoints(self):
        content = (
            "2024-03-15 13:42:00 first line\n"
            "some middle content\n" * 20 + "2024-03-15 13:57:00 last line"
        )
        result = extract_time_range(content)
        assert "Time range" in result
        assert "13:42:00" in result["Time range"]
        assert "13:57:00" in result["Time range"]

    def test_only_head_timestamp(self):
        content = "2024-03-15 13:42:00 first line\n" + "no timestamps here\n" * 20
        result = extract_time_range(content)
        assert "13:42:00" in result["Time range"]

    def test_no_timestamps(self):
        content = "no timestamps at all\n" * 5
        result = extract_time_range(content)
        assert result["Time range"] == "unknown"

    def test_short_content(self):
        content = "2024-03-15 10:00:00 only line"
        result = extract_time_range(content)
        assert "10:00:00" in result["Time range"]


@pytest.mark.unit
class TestFormatCoverageMetadata:
    """Tests for format_coverage_metadata()."""

    def test_basic_format(self):
        result = format_coverage_metadata(Lines="100 of 500", Format="json")
        assert result.startswith(COVERAGE_SEPARATOR)
        assert "Lines: 100 of 500" in result
        assert "Format: json" in result

    def test_none_values_omitted(self):
        result = format_coverage_metadata(Lines="50", Format=None)
        assert "Lines: 50" in result
        assert "Format" not in result

    def test_empty_kwargs(self):
        result = format_coverage_metadata()
        assert result == COVERAGE_SEPARATOR
