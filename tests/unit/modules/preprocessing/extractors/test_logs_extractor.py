"""
Unit tests for LogsAndErrorsExtractor (Crime Scene Extraction)

Tests the severity-based error detection and adaptive context extraction.
"""

import pytest

from faultmaven.modules.preprocessing.extractors.logs_extractor import (
    LogsAndErrorsExtractor,
    _normalize_template,
)


def _fe(result) -> str:
    """file_extract field — orientation content (FILE SUMMARY + crime scene)."""
    return result.file_extract or ""


def _sm(result) -> str:
    """search_map field — entity profile + template counts."""
    return result.search_map or ""


def _all(result) -> str:
    """Combined text for 'not in' assertions spanning both fields."""
    return _fe(result) + "\n" + _sm(result)


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
        assert "ERROR: Database connection failed" in _fe(result)
        assert "CRIME SCENE EXTRACTION" in _fe(result)
        assert "Single ERROR" in _fe(result)

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
        assert "FATAL: System crash" in _fe(result)
        # Extractor detects both ERROR and FATAL, so it reports "Multiple crime scenes"
        # The FATAL is prioritized (included in output), but both are detected
        assert "FATAL" in _fe(result) and (
            "Multiple crime scenes" in _fe(result)
            or "Single FATAL" in _fe(result)
            or "ERROR burst" in _fe(result)
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
        assert "Multiple crime scenes" in _fe(result)
        assert "First ERROR" in _fe(result)
        assert "Last ERROR" in _fe(result)

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
        assert "burst detected" in _fe(result).lower() or "ERROR" in _fe(result)

    def test_no_errors_fallback(self, extractor):
        """Test tail extraction when no errors found"""
        log_lines = ["INFO: Normal operation"] * 1000
        content = "\n".join(log_lines)

        result = extractor.extract(content)

        # Should extract last 500 lines
        assert "No errors detected" in _fe(result)
        assert "showing last" in _fe(result)

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
        result_line_count = len(_fe(result).split("\n"))
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

        assert "panic" in _fe(result).lower()
        assert "CRIME SCENE" in _fe(result)

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
        assert "Single CRITICAL" in _fe(result)

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
        assert "Distinct PIDs: 3" in _sm(result)
        assert "1048576" in _sm(result)

    def test_pid_regex_rejects_oversize_match(self):
        """Numbers above the kernel ceiling (e.g. request-byte counts,
        nanosecond timestamps) are not PIDs and must be filtered at
        count-time even if the regex matched them as bare digits."""
        extractor = LogsAndErrorsExtractor()
        # ``[9999999]`` exceeds kernel.pid_max (4_194_304); should not
        # appear as a PID in the entity profile.
        content = "req processed in [9999999] ns\n" * 3
        result = extractor.extract(content)
        assert "9999999" not in _sm(result) or "Distinct PIDs" not in _sm(result)

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
        assert "Distinct Ports" not in _sm(result)
        assert "Distinct PIDs" not in _sm(result)


class TestNormalizeTemplate:
    """_normalize_template strips per-occurrence variable parts, preserving
    the semantic message template so that identical log message types
    accumulate into a single counter bucket."""

    # --- Timestamp stripping ---

    def test_apache_clf_timestamp_stripped(self):
        line = (
            "[Sun Dec 04 04:47:44 2005] [error] mod_jk child workerEnv in error state 6"
        )
        assert (
            _normalize_template(line)
            == "[error] mod_jk child workerEnv in error state 6"
        )

    def test_apache_clf_timestamp_single_digit_day(self):
        line = "[Mon Jan  2 15:04:05 2006] [notice] workerEnv.init() ok /etc/httpd/conf/w.properties"
        result = _normalize_template(line)
        assert result.startswith("[notice]")
        assert "2006" not in result

    def test_iso_timestamp_stripped(self):
        line = "2026-04-25T12:34:56Z ERROR connection refused on port 5432"
        result = _normalize_template(line)
        assert "2026" not in result
        assert "ERROR connection refused on port 5432" == result

    def test_iso_timestamp_with_offset_stripped(self):
        line = "2026-04-25T12:34:56+05:30 WARN high memory usage"
        result = _normalize_template(line)
        assert "2026" not in result
        assert "WARN high memory usage" == result

    def test_syslog_preamble_stripped(self):
        line = "Dec  4 04:47:44 myhost sshd[1234]: Failed password for root"
        result = _normalize_template(line)
        assert "Dec" not in result
        assert "sshd" in result

    # --- Hex stripping ---

    def test_hex_address_replaced(self):
        line = "2026-04-25T00:00:00Z ERROR segfault at 0xdeadbeef in libfoo.so"
        result = _normalize_template(line)
        assert "0xdeadbeef" not in result
        assert "<addr>" in result

    # --- PID bracket stripping ---

    def test_pid_bracket_stripped(self):
        line = "Dec  4 04:47:44 host kernel[9876]: out of memory"
        result = _normalize_template(line)
        assert "[9876]" not in result

    def test_level_bracket_preserved(self):
        """[error] and [notice] brackets must survive — they contain letters."""
        line = "[Sun Dec 04 04:47:44 2005] [error] something bad"
        result = _normalize_template(line)
        assert "[error]" in result

    # --- Semantic number preservation ---

    def test_state_code_preserved(self):
        """Numeric state codes at line-end must not be stripped — they
        distinguish distinct error types (state 6 vs state 7)."""
        l6 = (
            "[Sun Dec 04 04:47:44 2005] [error] mod_jk child workerEnv in error state 6"
        )
        l7 = (
            "[Sun Dec 04 04:47:44 2005] [error] mod_jk child workerEnv in error state 7"
        )
        assert _normalize_template(l6) != _normalize_template(l7)

    def test_error_code_preserved(self):
        line = "2026-04-25T00:00:00Z ERROR exit code 137"
        result = _normalize_template(line)
        assert "137" in result

    # --- Idempotency and edge cases ---

    def test_already_normalized_line_unchanged(self):
        line = "[error] something went wrong"
        assert _normalize_template(line) == line

    def test_empty_line_returns_empty(self):
        assert _normalize_template("") == ""

    def test_whitespace_only_returns_empty(self):
        assert _normalize_template("   ") == ""


class TestBuildTemplateCounts:
    """_build_template_counts produces an EVENT TEMPLATE COUNTS block that
    reflects full-file frequencies, not the truncated crime-scene window."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _make_errors(self, templates: list[tuple[str, int]]) -> list[dict]:
        """Build a fake errors list: (template, count) pairs."""
        errors = []
        for template, count in templates:
            for _ in range(count):
                errors.append(
                    {"line_text": template, "severity": 50, "keyword": "ERROR"}
                )
        return errors

    def test_header_contains_counts(self, extractor):
        errors = self._make_errors([("ERROR disk full", 5), ("ERROR timeout", 3)])
        block = extractor._build_template_counts(errors)
        assert "EVENT TEMPLATE COUNTS" in block
        assert "8 lines matched severity keywords" in block
        assert "2 distinct templates" in block

    def test_sorted_descending_by_count(self, extractor):
        errors = self._make_errors([("ERROR minor", 2), ("ERROR major", 10)])
        block = extractor._build_template_counts(errors)
        major_pos = block.index("major")
        minor_pos = block.index("minor")
        assert major_pos < minor_pos

    def test_apache_state_counts_distinct(self, extractor):
        """The key regression test: state 6 and state 7 must not merge."""
        state6 = (
            "[Sun Dec 04 04:47:44 2005] [error] mod_jk child workerEnv in error state 6"
        )
        state7 = (
            "[Mon Dec 05 19:15:57 2005] [error] mod_jk child workerEnv in error state 7"
        )
        errors = self._make_errors([(state6, 369), (state7, 101)])
        block = extractor._build_template_counts(errors)
        assert "[ 369x] [error] mod_jk child workerEnv in error state 6" in block
        assert "[ 101x] [error] mod_jk child workerEnv in error state 7" in block

    def test_empty_errors_returns_empty_string(self, extractor):
        assert extractor._build_template_counts([]) == ""

    def test_singular_template_label(self, extractor):
        errors = self._make_errors([("ERROR boom", 1)])
        block = extractor._build_template_counts(errors)
        assert "1 distinct template)" in block  # not "templates"

    def test_template_truncated_at_120_chars(self, extractor):
        long_msg = "ERROR " + "x" * 200
        errors = self._make_errors([(long_msg, 1)])
        block = extractor._build_template_counts(errors)
        assert "..." in block
        for line in block.split("\n"):
            if "x" * 10 in line:
                assert len(line) < 200  # truncated


class TestExtractTemplateCounts:
    """Integration: extract() output contains EVENT TEMPLATE COUNTS, not
    the old TOP ERROR MESSAGES block."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _apache_log(self, n6: int, n7: int) -> str:
        state6 = "[Sun Dec 04 04:47:44 2005] [error] mod_jk child workerEnv in error state 6\n"
        state7 = "[Mon Dec 05 00:00:01 2005] [error] mod_jk child workerEnv in error state 7\n"
        notice = "[Sun Dec 04 04:47:44 2005] [notice] workerEnv.init() ok /etc/httpd/conf/workers2.properties\n"
        return (state6 * n6) + (state7 * n7) + (notice * 50)

    def test_no_top_error_messages_heading(self, extractor):
        result = extractor.extract(self._apache_log(10, 5))
        assert "TOP ERROR MESSAGES" not in _all(result)

    def test_event_template_counts_heading_present(self, extractor):
        result = extractor.extract(self._apache_log(10, 5))
        assert "EVENT TEMPLATE COUNTS" in _sm(result)

    def test_state6_and_state7_counts_in_output(self, extractor):
        result = extractor.extract(self._apache_log(369, 101))
        assert "369x" in _sm(result)
        assert "101x" in _sm(result)
        assert "state 6" in _sm(result)
        assert "state 7" in _sm(result)

    def test_no_errors_triggers_all_lines_fallback(self, extractor):
        """No error-keyword lines → all-lines fallback produces a template block."""
        content = "INFO startup complete\n" * 50
        result = extractor.extract(content)
        assert "EVENT TEMPLATE COUNTS" in _sm(result)
        assert "all lines" in _sm(result)


class TestTemplateCoverageSuppression:
    """Step 2 — template block suppressed when severity-keyword lines are a
    small fraction of the file (< MIN_TEMPLATE_COVERAGE_FRACTION)."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _ssh_log(self, total: int, error_count: int) -> str:
        """Build a synthetic SSH log with error_count WARN lines and
        (total - error_count) INFO lines.  Coverage = error_count / total."""
        lines = []
        for i in range(total):
            if i < error_count:
                lines.append(f"Dec 10 12:00:{i:02d} host sshd[1000]: WARN attempt {i}")
            else:
                lines.append(
                    f"Dec 10 12:00:00 host sshd[1000]: Failed password for user{i} from 10.0.0.1"
                )
        return "\n".join(lines)

    def test_low_coverage_triggers_all_lines_fallback(self, extractor):
        """Coverage 2% (48 of 2000) → all-lines fallback produces a template block."""
        content = self._ssh_log(total=2000, error_count=48)
        result = extractor.extract(content)
        assert "EVENT TEMPLATE COUNTS" in _sm(result)
        assert "all lines" in _sm(result)

    def test_template_block_shown_when_sufficient_coverage(self, extractor):
        """Coverage 25% (500 of 2000) → template block must appear."""
        content = self._ssh_log(total=2000, error_count=500)
        result = extractor.extract(content)
        assert "EVENT TEMPLATE COUNTS" in _sm(result)

    def test_coverage_threshold_boundary(self, extractor):
        """Exactly at threshold (15%) — block should appear."""
        content = self._ssh_log(total=100, error_count=15)
        result = extractor.extract(content)
        assert "EVENT TEMPLATE COUNTS" in _sm(result)

    def test_zero_total_lines_skips_coverage_check(self, extractor):
        """When total_lines=0 (unit-test mode), coverage gate is skipped."""
        errors = [
            {"line_text": "ERROR disk full", "severity": 50, "keyword": "ERROR"}
        ] * 2
        block = extractor._build_template_counts(errors, total_lines=0)
        assert "EVENT TEMPLATE COUNTS" in block


class TestEntityProfileMergedBuckets:
    """Step 1 — entity profile merges error/standard IP buckets and ranks by
    total mentions.  The old split (error mentions first) caused the wrong IP
    to appear at the top for SSH brute-force logs."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _ssh_brute_force_log(self) -> str:
        """Synthetic SSH log that mirrors the real openssh-01 structure:
        - 183.62.140.253: 867 total lines, none with ERROR/WARN keywords
        - 103.99.0.122:    45 lines total, all with ERROR/WARN keyword
        In the old split, 103.99.0.122 appeared first ("error mentions").
        With merged sort-by-total, 183.62.140.253 must come first.
        """
        lines = []
        # High-volume attacker — no ERROR keyword
        for i in range(867):
            lines.append(
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for root from 183.62.140.253 port {22000 + i}"
            )
        # Low-volume but hits ERROR lines
        for i in range(45):
            lines.append(
                f"Dec 10 12:00:00 host sshd[1001]: ERROR connection from 103.99.0.122 port {33000 + i}"
            )
        return "\n".join(lines)

    def test_ip_merged_by_total_mentions(self, extractor):
        """183.62.140.253 (867 total) must rank above 103.99.0.122 (45 total)."""
        result = extractor.extract(self._ssh_brute_force_log())
        sm = _sm(result)
        pos_high = sm.index("183.62.140.253")
        pos_low = sm.index("103.99.0.122")
        assert (
            pos_high < pos_low
        ), "High-volume IP must appear before low-volume error-only IP"

    def test_ip_section_has_no_error_standard_split(self, extractor):
        """The old 'error mentions' / 'standard mentions' labels must be gone."""
        result = extractor.extract(self._ssh_brute_force_log())
        assert "error mentions" not in _all(result).lower()
        assert "standard mentions" not in _all(result).lower()

    def test_error_line_annotation_present_for_qualifying_ip(self, extractor):
        """The optional '(N on error lines)' annotation appears for IPs that
        hit severity-keyword lines, but doesn't change the ranking."""
        result = extractor.extract(self._ssh_brute_force_log())
        # 103.99.0.122 appears on ERROR lines → annotation expected
        assert "on error lines" in _sm(result)

    def test_entity_profile_full_file_label(self, extractor):
        """Entity profile header must say 'full file scan'."""
        result = extractor.extract(self._ssh_brute_force_log())
        assert "ENTITY PROFILE (full file scan)" in _sm(result)


class TestUsernameProtocolTermFilter:
    """Step 1 (G7) — SSH/TLS protocol keywords must not be captured as
    usernames by the 'for <word>' branch of _USER_FOR_RE."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def test_authentication_not_captured_as_username(self, extractor):
        """'No more user authentication methods available for authentication from …'
        must not add 'authentication' to the username list."""
        line = (
            "Dec 10 12:00:00 host sshd[1000]: "
            "No more user authentication methods available for authentication from 10.0.0.1"
        )
        result = extractor.extract(line)
        # 'authentication' must not appear as a username entry
        assert "authentication: " not in _sm(result)

    def test_publickey_not_captured_as_username(self, extractor):
        line = "Dec 10 12:00:00 host sshd[1000]: Accepted publickey for admin from 10.0.0.2"
        result = extractor.extract(line)
        assert "publickey: " not in _sm(result)

    def test_real_username_still_captured(self, extractor):
        """Protocol-term filter must not suppress real account names."""
        lines = "\n".join(
            [
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for testuser from 10.0.0.3 port 22"
            ]
            * 5
        )
        result = extractor.extract(lines)
        assert "testuser" in _sm(result)

    def test_root_captured_as_username(self, extractor):
        """'root' is a real username and must NOT be filtered."""
        lines = "\n".join(
            [
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for root from 10.0.0.4 port 22"
            ]
            * 3
        )
        result = extractor.extract(lines)
        assert "root" in _sm(result)


class TestFileSummary:
    """Step 3 — FILE SUMMARY prepended to entity profile with dominant
    activity, top source, and absence declarations."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def test_file_summary_present_in_extract(self, extractor):
        lines = "\n".join(
            [
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for root from 10.0.0.5 port 22"
            ]
            * 20
        )
        result = extractor.extract(lines)
        assert "FILE SUMMARY:" in _fe(result)

    def test_file_summary_shows_dominant_event(self, extractor):
        lines = "\n".join(
            [
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for user{i} from 10.0.0.5 port 22"
                for i in range(50)
            ]
        )
        result = extractor.extract(lines)
        assert "failed password" in _fe(result).lower()

    def test_file_summary_absent_http_traffic_declared(self, extractor):
        """SSH log with no HTTP entries must declare 'no HTTP traffic' absent."""
        lines = "\n".join(
            [
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for root from 10.0.0.5 port 22"
            ]
            * 10
        )
        result = extractor.extract(lines)
        assert "no HTTP traffic" in _fe(result)

    def test_file_summary_no_absent_http_when_paths_present(self, extractor):
        """When HTTP paths are found, the absence declaration must NOT appear."""
        lines = "\n".join(
            [
                "[Sun Dec 04 04:47:44 2005] [error] GET /index.html HTTP/1.1 400",
                "[Sun Dec 04 04:47:45 2005] [error] POST /api/login HTTP/1.1 500",
            ]
            * 10
        )
        result = extractor.extract(lines)
        assert "no HTTP traffic" not in _all(result)

    def test_summary_precedes_entity_profile(self, extractor):
        """FILE SUMMARY is in file_extract; ENTITY PROFILE is in search_map (separate fields)."""
        lines = "\n".join(
            [
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for root from 10.0.0.5 port 22"
            ]
            * 10
        )
        result = extractor.extract(lines)
        assert "FILE SUMMARY:" in _fe(result)
        assert "ENTITY PROFILE" in _sm(result)


class TestSearchHints:
    """Step 4 — entity profile sections include [search: …] hints so the
    agent knows exactly what to pass to search_file."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def test_ip_section_has_search_hint(self, extractor):
        lines = "\n".join(
            [
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for root from 10.0.0.5 port 22"
            ]
            * 5
        )
        result = extractor.extract(lines)
        assert "[search:" in _sm(result)
        # IP section specifically
        assert "Distinct IPs" in _sm(result)

    def test_event_section_has_search_hint(self, extractor):
        lines = "\n".join(
            [
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for root from 10.0.0.5 port 22"
            ]
            * 5
        )
        result = extractor.extract(lines)
        # Event entry format: failed_password: 5  ["Failed password"]
        assert '"Failed password"' in _sm(result)

    def test_username_section_has_search_hint(self, extractor):
        lines = "\n".join(
            [
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for admin from 10.0.0.5 port 22"
            ]
            * 5
        )
        result = extractor.extract(lines)
        assert "Distinct usernames" in _sm(result)
        assert "[search:" in _sm(result)


class TestDetectLogPattern:
    """_detect_log_pattern — interpretation-first FILE SUMMARY heuristic."""

    def _counts(self, **kwargs):
        from collections import Counter

        return Counter(kwargs)

    def test_ssh_bruteforce_no_successes(self):
        event_counts = self._counts(failed_password=20)
        label = LogsAndErrorsExtractor._detect_log_pattern(event_counts, {}, 20, 200)
        assert "brute-force" in label.lower() or "credential" in label.lower()

    def test_ssh_bruteforce_ratio(self):
        """failures ≥ 3× successes triggers brute-force label."""
        event_counts = self._counts(failed_password=30, accepted_login=5)
        label = LogsAndErrorsExtractor._detect_log_pattern(event_counts, {}, 35, 300)
        assert "brute-force" in label.lower() or "credential" in label.lower()

    def test_ssh_auth_activity_not_bruteforce(self):
        """failures < 3× successes → general auth activity, not brute-force."""
        event_counts = self._counts(failed_password=3, accepted_login=5)
        label = LogsAndErrorsExtractor._detect_log_pattern(event_counts, {}, 8, 100)
        assert "authentication" in label.lower()
        assert "brute-force" not in label.lower()

    def test_ssh_invalid_user_triggers_auth_label(self):
        event_counts = self._counts(invalid_user=10)
        label = LogsAndErrorsExtractor._detect_log_pattern(event_counts, {}, 10, 100)
        assert "authentication" in label.lower()

    def test_http_error_pattern(self):
        from collections import Counter

        path_counts = Counter({"/api/login": 50})
        event_counts = self._counts()
        # 15 errors out of 100 lines = 15% > 5% threshold
        label = LogsAndErrorsExtractor._detect_log_pattern(
            event_counts, path_counts, 15, 100
        )
        assert "http" in label.lower() and "error" in label.lower()

    def test_http_access_log_low_errors(self):
        from collections import Counter

        path_counts = Counter({"/index.html": 100})
        event_counts = self._counts()
        # 2 errors out of 200 lines = 1% < 5% threshold
        label = LogsAndErrorsExtractor._detect_log_pattern(
            event_counts, path_counts, 2, 200
        )
        assert "http" in label.lower()
        assert "error" not in label.lower()

    def test_no_pattern_returns_empty(self):
        event_counts = self._counts()
        label = LogsAndErrorsExtractor._detect_log_pattern(event_counts, {}, 0, 100)
        assert label == ""

    def test_pattern_appears_first_in_file_summary(self):
        """Interpretation prefix must be the first sentence of FILE SUMMARY."""
        extractor = LogsAndErrorsExtractor()
        lines = "\n".join(
            [
                f"Dec 10 12:00:00 host sshd[1000]: Failed password for root from 10.0.0.5 port 22"
            ]
            * 30
        )
        result = extractor.extract(lines)
        fe = _fe(result)
        summary_start = fe.index("FILE SUMMARY:")
        summary_line = fe[summary_start:].split("\n")[0]
        assert (
            "brute-force" in summary_line.lower()
            or "credential" in summary_line.lower()
        )

    def test_template_counts_in_search_map_crime_scene_in_file_extract(self):
        """EVENT TEMPLATE COUNTS is in search_map; CRIME SCENE is in file_extract."""
        extractor = LogsAndErrorsExtractor()
        # Need enough errors for coverage threshold (≥15%): 50 WARN out of 100 lines
        error_lines = [
            f"Dec 10 12:00:00 host sshd[1000]: WARN bad attempt {i}" for i in range(50)
        ]
        normal_lines = [
            f"Dec 10 12:00:00 host sshd[1000]: normal line {i}" for i in range(50)
        ]
        lines = "\n".join(error_lines + normal_lines)
        result = extractor.extract(lines)
        if result.search_map and "EVENT TEMPLATE COUNTS" in result.search_map:
            assert "CRIME SCENE" in _fe(result)


class TestIPAuthBreakdown:
    """IP auth breakdown table — per-IP per-event-type counts emitted in
    search_map so the agent can answer auth-attempt-count questions directly."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _ssh_log_with_mixed_events(self) -> str:
        lines = []
        # IP 10.0.0.1: 5 failed_password + 3 invalid_user = 8 auth events
        for _ in range(5):
            lines.append(
                "Dec 10 06:55:00 host sshd[1]: Failed password for root from 10.0.0.1 port 22"
            )
        for _ in range(3):
            lines.append(
                "Dec 10 07:00:00 host sshd[1]: Invalid user admin from 10.0.0.1 port 22"
            )
        # IP 10.0.0.2: 2 failed_password only = 2 auth events
        for _ in range(2):
            lines.append(
                "Dec 10 07:30:00 host sshd[2]: Failed password for guest from 10.0.0.2 port 22"
            )
        return "\n".join(lines)

    def test_auth_breakdown_present_in_search_map(self, extractor):
        result = extractor.extract(self._ssh_log_with_mixed_events())
        assert "IP auth breakdown" in _sm(result)

    def test_auth_breakdown_shows_event_counts(self, extractor):
        result = extractor.extract(self._ssh_log_with_mixed_events())
        sm = _sm(result)
        assert "failed_password=5" in sm
        assert "invalid_user=3" in sm

    def test_auth_breakdown_shows_total(self, extractor):
        result = extractor.extract(self._ssh_log_with_mixed_events())
        # 10.0.0.1: 5 failed + 3 invalid = 8
        assert "auth total=8" in _sm(result)

    def test_auth_breakdown_absent_for_non_auth_logs(self, extractor):
        """Logs with only HTTP paths and no auth events must not emit a breakdown."""
        lines = "\n".join(
            ["[Sun Dec 04 04:47:44 2005] [error] GET /index.html HTTP/1.1 400"] * 10
        )
        result = extractor.extract(lines)
        assert "IP auth breakdown" not in _sm(result)

    def test_auth_breakdown_ip_mention_count_unchanged(self, extractor):
        """The line-occurrence count in Distinct IPs section must not change."""
        result = extractor.extract(self._ssh_log_with_mixed_events())
        sm = _sm(result)
        # 10.0.0.1 appears on 8 lines total
        assert "10.0.0.1: 8 line occurrences (all event types)" in sm


class TestAllUsernamesEmitted:
    """q3 fix — all distinct usernames must appear in search_map regardless
    of how many there are; the old top-20 cap caused misses."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def test_more_than_twenty_usernames_all_emitted(self, extractor):
        """25 distinct usernames must all appear (old cap was 20)."""
        lines = []
        for i in range(25):
            lines.append(
                f"Dec 10 12:00:00 host sshd[1]: Invalid user user{i:02d} from 10.0.0.1 port 22"
            )
        result = extractor.extract("\n".join(lines))
        sm = _sm(result)
        for i in range(25):
            assert f"user{i:02d}" in sm, f"user{i:02d} not found in search_map"

    def test_username_header_shows_total_count(self, extractor):
        lines = []
        for i in range(5):
            lines.append(
                f"Dec 10 12:00:00 host sshd[1]: Failed password for person{i} from 10.0.0.1 port 22"
            )
        result = extractor.extract("\n".join(lines))
        assert "Distinct usernames (5 total)" in _sm(result)

    def test_no_incomplete_notice_emitted(self, extractor):
        """The old 'INCOMPLETE: N more not shown' message must not appear."""
        lines = []
        for i in range(25):
            lines.append(
                f"Dec 10 12:00:00 host sshd[1]: Invalid user user{i:02d} from 10.0.0.1 port 22"
            )
        result = extractor.extract("\n".join(lines))
        assert "INCOMPLETE" not in _sm(result)


class TestNumericStateCodeNote:
    """q6 fix — FILE SUMMARY must contain a note about internal state codes
    when the log has 'error state N' lines (e.g. mod_jk)."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _apache_modjk_log(self) -> str:
        # Include a PID-bearing line so pid_counts is non-empty and
        # _build_entity_profile() doesn't hit the early-return path.
        lines = [
            "[Sun Dec 04 04:47:44 2005] [error] mod_jk child workerEnv in error state 6 1",
            "[Sun Dec 04 04:47:44 2005] [error] mod_jk child workerEnv in error state 6 2",
            "[Sun Dec 04 04:47:45 2005] [notice] jk2_init() Found child [6725] in scoreboard slot 10",
        ] * 10
        return "\n".join(lines)

    def test_state_code_note_in_file_summary(self, extractor):
        result = extractor.extract(self._apache_modjk_log())
        assert "numeric state codes" in _fe(result).lower()
        assert "not documented in this log" in _fe(result)

    def test_state_code_note_absent_for_ssh_logs(self, extractor):
        """SSH logs with no 'error state N' lines must not get the note."""
        lines = "\n".join(
            [
                "Dec 10 12:00:00 host sshd[1]: Failed password for root from 10.0.0.1 port 22"
            ]
            * 20
        )
        result = extractor.extract(lines)
        assert "numeric state codes" not in _fe(result).lower()

    def test_state_code_note_in_file_summary_not_search_map(self, extractor):
        """The note belongs in file_extract (FILE SUMMARY), not search_map."""
        result = extractor.extract(self._apache_modjk_log())
        assert "numeric state codes" in _fe(result).lower()
        # It may optionally appear in search_map too, but must be in file_extract


class TestSyslogServiceBreakdown:
    """Service breakdown section appears for multi-service syslog logs."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _linux_mixed_log(self) -> str:
        lines = []
        # 50 ftpd lines
        for _ in range(50):
            lines.append(
                "Jun 17 07:07:00 combo ftpd[29504]: connection from 1.2.3.4 (host.example.com) at Mon Jun 17 07:07:00 2005"
            )
        # 20 sshd lines
        for _ in range(20):
            lines.append(
                "Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure; logname= uid=0 euid=0 tty=NODEVssh ruser= rhost=218.188.2.4"
            )
        # 10 su lines
        for _ in range(10):
            lines.append(
                "Jun 15 04:06:18 combo su(pam_unix)[21416]: session opened for user cyrus by (uid=0)"
            )
        return "\n".join(lines)

    def test_top_services_section_present_for_multi_service(self, extractor):
        result = extractor.extract(self._linux_mixed_log())
        assert "Top services" in _sm(result)

    def test_most_common_service_listed_first(self, extractor):
        result = extractor.extract(self._linux_mixed_log())
        sm = _sm(result)
        ftpd_pos = sm.find("ftpd:")
        sshd_pos = sm.find("sshd:")
        assert ftpd_pos != -1, "ftpd should appear in Top services"
        assert ftpd_pos < sshd_pos, "ftpd (50 lines) should precede sshd (20 lines)"

    def test_service_section_absent_for_single_service(self, extractor):
        """Pure sshd log — no service breakdown needed."""
        lines = "\n".join(
            [
                "Dec 10 12:00:00 host sshd[1]: Failed password for root from 10.0.0.1 port 22"
            ]
            * 30
        )
        result = extractor.extract(lines)
        assert "Top services" not in _sm(result)

    def test_service_counts_reflect_line_counts(self, extractor):
        result = extractor.extract(self._linux_mixed_log())
        sm = _sm(result)
        assert "50 lines" in sm, "ftpd should show 50 lines"


class TestSSHSessionOpened:
    """ssh_session_opened event type — counts and FILE SUMMARY note."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _linux_session_log(self) -> str:
        lines = []
        # Auth failures from attacker
        for _ in range(40):
            lines.append(
                "Jun 14 15:16:01 combo sshd(pam_unix)[1000]: authentication failure; logname= uid=0 euid=0 tty=NODEVssh ruser= rhost=150.183.249.110"
            )
        # Successful SSH sessions
        for _ in range(10):
            lines.append(
                "Jun 17 20:29:26 combo sshd(pam_unix)[30631]: session opened for user test by (uid=509)"
            )
        return "\n".join(lines)

    def test_ssh_session_count_in_event_types(self, extractor):
        result = extractor.extract(self._linux_session_log())
        # Event key uses underscores in the entity profile
        assert "ssh_session_opened" in _sm(result).lower()

    def test_ssh_session_count_correct(self, extractor):
        result = extractor.extract(self._linux_session_log())
        assert "10" in _sm(result)

    def test_file_summary_mentions_successful_sessions(self, extractor):
        result = extractor.extract(self._linux_session_log())
        assert "successful SSH session" in _fe(result)

    def test_su_sessions_not_counted_as_ssh(self, extractor):
        """su(pam_unix) session opens must not be counted as ssh_session_opened."""
        lines = "\n".join(
            [
                "Jun 15 04:06:18 combo su(pam_unix)[21416]: session opened for user cyrus by (uid=0)"
            ]
            * 20
            + [
                "Jun 14 15:16:01 combo sshd(pam_unix)[1000]: authentication failure; logname= uid=0 euid=0 tty=NODEVssh ruser= rhost=150.183.249.110"
            ]
            * 20
        )
        result = extractor.extract(lines)
        # su sessions should not appear as ssh_session_opened
        assert "successful SSH session" not in _fe(result)


class TestYYMMDDTimestamp:
    """YYMMDD HHMMSS format (HDFS / Hadoop ecosystem logs)."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _hdfs_log(self) -> str:
        # Needs >10 lines so extract_time_range_ts scans both head and tail.
        early = "081109 203615 148 INFO dfs.DataNode$PacketResponder: PacketResponder 1 for block blk_123 terminating"
        mid = "081109 214043 2561 WARN dfs.DataNode$DataXceiver: 10.251.30.85:50010:Got exception while serving blk_456"
        late = "081111 102017 26347 INFO dfs.DataNode$DataXceiver: Receiving block blk_abc src: /10.250.9.2:50010"
        lines = [early] * 5 + [mid] * 5 + [late] * 5
        return "\n".join(lines)

    def test_time_range_extracted_from_hdfs_format(self, extractor):
        result = extractor.extract(self._hdfs_log())
        # The time range should be populated from YYMMDD timestamps
        assert "2008-11-09" in _fe(result), "Start date should be 2008-11-09"
        assert "2008-11-11" in _fe(result), "End date should be 2008-11-11"

    def test_hdfs_time_range_not_unknown(self, extractor):
        result = extractor.extract(self._hdfs_log())
        assert "unknown" not in _fe(result).lower()

    def test_warn_only_note_in_file_summary(self, extractor):
        """WARN-only logs should say 'no ERROR or FATAL entries' in FILE SUMMARY."""
        result = extractor.extract(self._hdfs_log())
        assert "no error or fatal" in _fe(result).lower()

    def test_warn_only_note_absent_for_error_logs(self, extractor):
        """Logs with ERROR lines must NOT get the warn-only note."""
        lines = "\n".join(["Dec 10 12:00:00 host sshd[1]: ERROR failed hard"] * 15)
        result = extractor.extract(lines)
        assert "no error or fatal" not in _fe(result).lower()


class TestNonAuthForWordFilter:
    """_USER_FOR_RE is only applied on lines with explicit auth-context keywords.
    Kernel and service messages like 'for high-res timesource' or 'for PnP cards'
    must not pollute the username list, while 'session opened for user <name>'
    and 'Failed password for <name>' must still capture the username."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _make_log(self, *lines, pad=15) -> str:
        """Pad with neutral filler to meet >10-line thresholds."""
        filler = "Jun 14 12:00:00 host kernel: generic info message"
        return "\n".join(list(lines) + [filler] * pad)

    def test_kernel_for_word_not_captured(self, extractor):
        """'for high-res timesource' must not produce a 'high-res' username entry."""
        log = self._make_log(
            "Jul 27 14:41:57 combo kernel: Using tsc for high-res timesource"
        )
        result = extractor.extract(log)
        # "high-res: N mentions" format indicates a username entry; the raw token
        # may still appear in template-count lines that reproduce the original text.
        assert "high-res: " not in _sm(result)

    def test_isapnp_for_word_not_captured(self, extractor):
        """'for PnP cards' must not produce a 'PnP' username entry."""
        log = self._make_log(
            "Jul 27 14:42:00 combo kernel: isapnp: Scanning for PnP cards..."
        )
        result = extractor.extract(log)
        assert "PnP: " not in _sm(result)

    def test_failed_password_for_username_captured(self, extractor):
        """Auth context ('Failed password') must still enable 'for <user>' capture."""
        log = self._make_log(
            *[
                "Dec 10 12:00:00 host sshd[1]: Failed password for guest from 1.2.3.4 port 22 ssh2"
            ]
            * 5
        )
        result = extractor.extract(log)
        assert "guest" in _sm(result)

    def test_session_opened_for_user_captured(self, extractor):
        """'session opened for user <name>' must capture the username via _USER_FIELD_RE."""
        log = self._make_log(
            *[
                "Jun 15 10:00:00 host sshd(pam_unix)[999]: session opened for user cyrus by (uid=0)"
            ]
            * 5
        )
        result = extractor.extract(log)
        assert "cyrus" in _sm(result)


class TestYearlessTimestampNote:
    """Syslog BSD logs without an explicit year get a FILE SUMMARY note explaining
    that the displayed year is inferred from the current date."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _syslog_yearless(self, n: int = 15) -> str:
        """Minimal syslog BSD log with no year in timestamps."""
        lines = [
            f"Jun {14 + i % 3} 12:00:{i:02d} host sshd[1]: Failed password for root from 1.2.3.4 port 22 ssh2"
            for i in range(n)
        ]
        return "\n".join(lines)

    def _iso_log(self, n: int = 15) -> str:
        """ISO-8601 log — year always present."""
        lines = [
            f"2023-06-{14 + i % 3:02d} 12:00:00 ERROR host sshd: Failed password for root from 1.2.3.4"
            for i in range(n)
        ]
        return "\n".join(lines)

    def test_yearless_note_present_in_file_summary(self, extractor):
        result = extractor.extract(self._syslog_yearless())
        assert "timestamps in this log have no year" in _fe(result).lower()

    def test_yearless_note_absent_for_iso_timestamps(self, extractor):
        result = extractor.extract(self._iso_log())
        assert "timestamps in this log have no year" not in _fe(result).lower()

    def test_yearless_note_contains_inferred_year(self, extractor):
        """The note should mention the inferred year (a 4-digit number)."""
        import re

        result = extractor.extract(self._syslog_yearless())
        note = _fe(result)
        # The note format: "[Note: timestamps in this log have no year — YYYY is inferred...]"
        assert re.search(r"is inferred from the current date", note)

    def test_yearless_note_includes_sample_raw_timestamp(self, extractor):
        """The note should include a raw sample timestamp so the agent can see the format."""
        import re

        result = extractor.extract(self._syslog_yearless())
        note = _fe(result)
        # The note includes e.g. '(e.g., 'Jun 14 12:00:00')' — check for the month abbreviation
        assert re.search(r"e\.g\.,\s*'Jun\s+\d+\s+\d{2}:\d{2}:\d{2}'", note)
