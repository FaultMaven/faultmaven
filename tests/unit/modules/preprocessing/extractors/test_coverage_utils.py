"""Tests for coverage metadata utilities in extractors/utils.py."""

import time

import pytest

from faultmaven.modules.preprocessing.extractors.utils import (
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

    def test_syslog_family_uses_explicit_year_when_present(self):
        """Root-cause contract: when the input line carries an explicit year
        adjacent to the timestamp, that year must be used — regardless of
        which "variant" of the BSD-syslog family produced the line.

        This covers the bug where Apache error logs (`[Sun Dec 04 04:47:44
        2005]`) were parsed by a pattern that discarded the 2005 and
        synthesised a year via `datetime.now()`. The fix collapsed the
        syslog/asctime/apache-ctime variants into one generic pattern with
        an optional year suffix — this test pins that contract.
        """
        inputs_with_expected_year = [
            # Apache error-log format (day-of-week prefix + year suffix)
            ("[Sun Dec 04 04:47:44 2005] [error] mod_jk child", 2005),
            # asctime-style (no day-of-week, year suffix)
            ("Dec 04 04:47:44 2005 something", 2005),
            # Day-of-week and year present, no surrounding brackets
            ("Mon Jan 06 15:08:24 2003 httpd starting", 2003),
        ]
        for line, expected_year in inputs_with_expected_year:
            ts = extract_timestamp(line)
            assert ts is not None, f"failed to extract from {line!r}"
            assert (
                ts.year == expected_year
            ), f"expected year {expected_year} from {line!r}, got {ts.year}"

    def test_syslog_family_falls_back_to_heuristic_without_year(self):
        """When no explicit year appears, the "now or previous year" heuristic
        is applied. This is the original BSD-syslog behaviour and must remain
        intact after the generic-pattern refactor."""
        # Line with no year — heuristic kicks in and picks current or last year
        ts = extract_timestamp("Mar 15 14:30:45 host sshd[1234]: Failed")
        assert ts is not None
        assert ts.month == 3 and ts.day == 15
        # Year is either current or previous, never synthesised as anything else
        import datetime as _dt

        now_year = _dt.datetime.now(_dt.UTC).year
        assert ts.year in (now_year, now_year - 1)

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

    def test_apache_log_time_range_uses_explicit_year(self):
        """Regression: Apache log coverage metadata must report 2005, not 2025.
        Previously, syslog_bsd greedily matched the 'Dec 04 04:47:44' substring
        and synthesised a now()-based year, producing wrong coverage metadata
        that propagated into LLM narrative answers."""
        content = (
            "[Sun Dec 04 04:47:44 2005] [error] first line\n"
            + "[Sun Dec 04 04:51:08 2005] [notice] middle\n" * 20
            + "[Mon Dec 05 19:15:57 2005] [error] last line"
        )
        result = extract_time_range(content)
        assert "2005" in result["Time range"]
        assert "2025" not in result["Time range"]
        assert "2026" not in result["Time range"]


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
