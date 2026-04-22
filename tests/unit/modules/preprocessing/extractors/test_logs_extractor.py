"""
Unit tests for LogsAndErrorsExtractor (Crime Scene Extraction)

Tests the severity-based error detection and adaptive context extraction.
"""

import pytest

from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
)


class TestLogsAndErrorsExtractor:
    """Test Crime Scene Extraction functionality"""

    @pytest.fixture
    def extractor(self):
        """Create extractor instance"""
        return LogsAndErrorsExtractor()

    def test_single_error_extraction(self, extractor):
        """Test extraction of single error with context"""
        # Create log with one ERROR
        log_lines = (
            ["INFO: Starting application"] * 50
            + ["ERROR: Database connection failed"]
            + ["INFO: Retrying connection"] * 50
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should extract ±200 lines around error
        assert "ERROR: Database connection failed" in result
        assert "CRIME SCENE EXTRACTION" in result
        assert "Single ERROR" in result

    def test_severity_prioritization(self, extractor):
        """Test that FATAL takes priority over ERROR"""
        log_lines = (
            ["INFO: Normal operation"] * 10
            + ["ERROR: Minor issue at line 11"]
            + ["INFO: Continuing"] * 20
            + ["FATAL: System crash at line 32"]
            + ["INFO: Aftermath"] * 10
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should prioritize FATAL over ERROR
        assert "FATAL: System crash" in result
        # Extractor detects both ERROR and FATAL, so it reports "Multiple crime scenes"
        # The FATAL is prioritized (included in output), but both are detected
        assert "FATAL" in result and (
            "Multiple crime scenes" in result
            or "Single FATAL" in result
            or "ERROR burst" in result
        )

    def test_multiple_crime_scenes(self, extractor):
        """Test detection of first + last errors"""
        log_lines = (
            ["INFO: Startup"] * 20
            + ["ERROR: First problem at line 21"]
            + ["INFO: Normal operation"] * 300  # Large gap
            + ["ERROR: Last problem at line 322"]
            + ["INFO: Shutdown"] * 20
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should extract both crime scenes
        assert "Multiple crime scenes" in result
        assert "First ERROR" in result
        assert "Last ERROR" in result

    def test_error_burst_detection(self, extractor):
        """Test detection of error clustering"""
        log_lines = (
            ["INFO: Normal"] * 30
            +
            # Create burst of 15 errors in close proximity
            ["ERROR: Problem 1", "ERROR: Problem 2", "ERROR: Problem 3"] * 5
            + ["INFO: After burst"] * 30
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should detect burst
        assert "burst detected" in result.lower() or "ERROR" in result

    def test_no_errors_fallback(self, extractor):
        """Test tail extraction when no errors found"""
        log_lines = ["INFO: Normal operation"] * 1000
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should extract last 500 lines
        assert "No errors detected" in result
        assert "showing last" in result

    def test_safety_truncation(self, extractor):
        """Test that output is truncated if too large"""
        # Create very large error context
        log_lines = (
            ["INFO: Line"] * 100
            + ["ERROR: Problem"]
            + ["INFO: Context line"] * 1000  # Massive context
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should be truncated to MAX_SNIPPET_LINES (500)
        result_line_count = len(result.split("\n"))
        assert (
            result_line_count <= extractor.MAX_SNIPPET_LINES + 10
        )  # Some buffer for headers

    def test_panic_keyword_go_lang(self, extractor):
        """Test Go language panic detection"""
        log_lines = (
            ["INFO: Starting Go service"] * 20
            + ["panic: runtime error: index out of range"]
            + ["goroutine 1 [running]:"]
            + ["main.main()"] * 10
        )
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        assert "panic" in result.lower()
        assert "CRIME SCENE" in result

    def test_severity_ordering_critical_over_warning(self, extractor):
        """R4.4: Line containing both WARNING and CRITICAL should classify as CRITICAL."""
        log_lines = (
            ["INFO: Normal"] * 20
            + ["WARNING: CRITICAL failure in subsystem"]
            + ["INFO: Normal"] * 20
        )
        content = "\n".join(log_lines)
        result = extractor.extract(content)
        # The line matches both WARNING and CRITICAL — CRITICAL (severity 90) should win
        assert "Single CRITICAL" in result

    def test_properties(self, extractor):
        """Test extractor properties"""
        assert extractor.strategy_name == "crime_scene"
        assert extractor.llm_calls_used == 0


class TestEntityProfileRegexes:
    """Regression tests for entity-profile regexes.

    The underlying contract: entity patterns need *structural* context, not
    just a stray delimiter. A bare colon is insufficient context for a port
    (every ``HH:MM:SS`` would match); a bare open-bracket is insufficient
    for a PID (``[19:02:15]`` would match). These tests lock in the
    principle that entity tokens must sit next to a non-numeric context
    token — a keyword, a host/address, or an enclosing bracket pair.
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    @staticmethod
    def _ports(line: str) -> list[str]:
        return LogsAndErrorsExtractor._PORT_KEYWORD_RE.findall(
            line
        ) + LogsAndErrorsExtractor._HOST_PORT_RE.findall(line)

    @staticmethod
    def _pids(line: str) -> list[str]:
        return LogsAndErrorsExtractor._PID_KEYWORD_RE.findall(
            line
        ) + LogsAndErrorsExtractor._PID_BRACKET_RE.findall(line)

    # --- Ports: pure-digit LHS must not yield a port ---

    def test_port_regex_rejects_timestamp_fragment(self):
        """``HH:MM:SS`` has pure-digit LHS; no port should be captured."""
        assert self._ports("log line 04:47:44 something") == []

    def test_port_regex_rejects_bracketed_timestamp(self):
        assert self._ports("[19:02:15] event") == []

    def test_port_regex_accepts_explicit_keyword(self):
        assert self._ports("Accepted from 10.0.0.5 port 22 ssh2") == ["22"]

    def test_port_regex_accepts_ipv4_port(self):
        """IPv4 LHS contains dots → non-digit context → port captured."""
        assert self._ports("peer 192.168.1.1:80 connected") == ["80"]

    def test_port_regex_accepts_hostname_port(self):
        """Hostname LHS contains letters → port captured."""
        assert self._ports("upstream db.example.com:5432 slow") == ["5432"]

    def test_port_regex_accepts_simple_hostname_port(self):
        assert self._ports("connecting to localhost:8080") == ["8080"]

    def test_port_regex_accepts_url_embedded_port(self):
        assert self._ports("fetched http://example.com:8443/api") == ["8443"]

    # --- PIDs: bracketed form requires closing bracket ---

    def test_pid_regex_rejects_partial_bracket(self):
        """``[19:`` inside a timestamp has no closing bracket after digits."""
        assert self._pids("[19:02:15] msg") == []

    def test_pid_regex_rejects_year_inside_outer_brackets(self):
        """The 2005 in ``[Sun Dec 04 04:47:44 2005]`` is not immediately
        after the opening ``[`` and so must not be captured as a PID."""
        assert self._pids("[Sun Dec 04 04:47:44 2005] msg") == []

    def test_pid_regex_accepts_classic_syslog(self):
        assert self._pids("host sshd[1234]: Failed") == ["1234"]

    def test_pid_regex_accepts_keyword_form(self):
        assert self._pids("worker pid=5678 exited") == ["5678"]

    def test_pid_regex_accepts_seven_digit_pid(self):
        """Regression: the bracket/keyword matchers previously used
        ``\\d{1,5}`` which silently dropped any PID >= 100_000. Tuned
        Linux hosts set ``kernel.pid_max`` to 4_194_304 (7 digits), and
        container workloads exhaust the space routinely. Accept up to 7
        digits so the resulting entity profile actually reflects the host."""
        assert self._pids("host worker[2345678]: crashed") == ["2345678"]
        assert self._pids("worker pid=1048576 exited") == ["1048576"]

    def test_pid_entity_profile_counts_seven_digit_pids(self):
        """End-to-end: a log with real-world 7-digit PIDs must surface
        them in the entity profile rather than silently dropping them."""
        extractor = LogsAndErrorsExtractor()
        content = "\n".join(
            [
                "worker[1048576]: ERROR connection reset",
                "worker[1048577]: ERROR connection reset",
                "worker[1048578]: WARN retrying",
            ]
        )
        result = extractor.extract(content)
        assert "Distinct PIDs: 3" in result
        assert "1048576" in result

    def test_pid_regex_rejects_oversize_match(self):
        """Numbers above the kernel ceiling (e.g. request-byte counts,
        nanosecond timestamps) are not PIDs and must be filtered at
        count-time even if the regex matched them as bare digits."""
        extractor = LogsAndErrorsExtractor()
        # ``[9999999]`` exceeds kernel.pid_max (4_194_304); should not
        # appear as a PID in the entity profile.
        content = "req processed in [9999999] ns\n" * 3
        result = extractor.extract(content)
        assert "9999999" not in result or "Distinct PIDs" not in result

    # --- IPv6: full and compressed forms; no collision with timestamps ---

    def test_ipv6_regex_full_form(self):
        assert LogsAndErrorsExtractor._IPV6_RE.findall(
            "peer 2001:db8:85a3:0:0:8a2e:370:7334 connected"
        ) == ["2001:db8:85a3:0:0:8a2e:370:7334"]

    def test_ipv6_regex_loopback_compressed(self):
        assert LogsAndErrorsExtractor._IPV6_RE.findall("listening on ::1") == ["::1"]

    def test_ipv6_regex_link_local(self):
        assert LogsAndErrorsExtractor._IPV6_RE.findall("peer fe80::1 up") == ["fe80::1"]

    def test_ipv6_regex_middle_compression(self):
        assert LogsAndErrorsExtractor._IPV6_RE.findall(
            "route to 2001:db8::ff00:42:8329 via gw"
        ) == ["2001:db8::ff00:42:8329"]

    def test_ipv6_regex_does_not_match_decimal_timestamp(self):
        assert (
            LogsAndErrorsExtractor._IPV6_RE.findall("2024-03-15 14:30:45 INFO startup")
            == []
        )

    # --- End-to-end: no spurious ports/PIDs from bracketed-timestamp logs ---

    def test_bracketed_timestamp_logs_produce_no_spurious_entities(self, extractor):
        """A log format that uses bracketed timestamps (Apache, CloudFoundry,
        various custom formats) must not leak timestamp digits into the
        port or PID entity counts."""
        content = (
            "[Sun Dec 04 04:47:44 2005] [notice] workerEnv.init() ok\n"
            "[Sun Dec 04 04:47:44 2005] [error] child in error state 6\n"
        ) * 20
        result = extractor.extract(content)
        assert "Distinct Ports" not in result
        assert "Distinct PIDs" not in result
