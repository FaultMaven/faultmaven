"""Tests for coverage metadata utilities in extractors/utils.py."""

import pytest

from faultmaven.modules.preprocessing.extractors.utils import (
    extract_time_range,
    extract_timestamp,
    split_log_lines,
)


@pytest.mark.unit
class TestSplitLogLines:
    """``split_log_lines`` — and why it is not ``str.splitlines()``.

    Two callers count usernames per line (fm#1574), so what counts as a line
    is now load-bearing rather than cosmetic. ``str.split("\n")`` read a
    bare-``\r`` file as ONE line and floored every per-line count at 1.
    ``str.splitlines()`` is the obvious replacement and is wrong twice; both
    ways are pinned here, because a reader who does not know that will
    "simplify" this helper into ``splitlines()`` and the username tests
    alone would not notice.
    """

    @pytest.mark.parametrize(
        "ending",
        [
            pytest.param("\n", id="LF"),
            pytest.param("\r", id="CR"),
            pytest.param("\r\n", id="CRLF"),
        ],
    )
    def test_all_three_line_endings_split(self, ending):
        """What the helper exists for."""
        assert split_log_lines(f"alpha{ending}beta{ending}gamma")[:3] == [
            "alpha",
            "beta",
            "gamma",
        ]

    def test_a_crlf_leaves_no_carriage_return_on_the_line(self):
        """``split("\\n")`` left a trailing ``\r`` on every CRLF line."""
        assert split_log_lines("alpha\r\nbeta") == ["alpha", "beta"]

    @pytest.mark.parametrize(
        "char,name",
        [
            ("\x0b", "vertical-tab"),
            ("\x0c", "form-feed"),
            ("\x1c", "file-separator"),
            ("\x1d", "group-separator"),
            ("\x1e", "record-separator"),
            ("\x85", "NEL"),
            ("\u2028", "LINE-SEPARATOR"),
            ("\u2029", "PARAGRAPH-SEPARATOR"),
        ],
    )
    def test_no_other_code_point_ends_a_line(self, char, name):
        """The first reason this is not ``str.splitlines()``.

        ``splitlines()`` breaks on all eight of these. None ends a line in
        any log format, and a form feed inside a Windows CBS line would be
        reported as two lines the file does not have — inventing a line
        inflates every per-line count taken over it.
        """
        assert split_log_lines(f"alpha{char}beta") == [f"alpha{char}beta"]
        assert len(f"alpha{char}beta".splitlines()) == 2  # the rejected remedy

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param("alpha\nbeta\n", id="trailing-LF"),
            pytest.param("alpha\nbeta", id="no-trailing-LF"),
            pytest.param("alpha\r\nbeta\r\n", id="CRLF"),
            pytest.param("", id="empty"),
            pytest.param("\n", id="lone-LF"),
        ],
    )
    def test_line_count_matches_split_on_lf_when_no_bare_cr(self, content):
        """The second reason, and what keeps line INDICES in step.

        ``splitlines()`` drops the trailing empty element, so ``len()`` falls
        by one on every file ending in a newline — and that number is
        rendered as "N severity-flagged lines out of M total". This helper is
        element-count-identical to ``split("\\n")`` wherever there is no bare
        ``\r``, which is also why the entity profile's line index still
        agrees with the error-line set computed beside it.
        """
        assert len(split_log_lines(content)) == len(content.split("\n"))


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

    # Per-call cost ("under 1 ms", 1000 calls against a raw 1 s here before
    # #1579) is ``test_timestamp_extraction_per_line`` in
    # ``tests/performance/test_extraction_speed.py``.


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
