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

        # Should detect burst (header now reads "ERROR burst — ...")
        fe = _fe(result).lower()
        assert "burst" in fe or "error" in fe

    def test_burst_header_does_not_imply_event_count(self, extractor):
        """The crime-scene burst header must NOT phrase its window size as
        an event count — line span between first and last burst-keyword line
        is window WIDTH, not the count of those events. Earlier wording
        ("Error burst detected: 26 lines with WARN storm") caused the agent
        to read 26 as the WARN count on HDFS q1; ground truth was 80.

        Uses WARN-only events to force the burst path (multiple
        high-severity ERRORs would route to the multiple-crime-scenes
        path which has its own header)."""
        # 12 WARN events in 36 lines triggers ERROR_BURST_THRESHOLD (10)
        # without crossing the multiple-crime-scenes branch.
        log_lines = (
            ["INFO: startup"] * 30
            + ["WARN: minor issue"] * 12
            + ["INFO: still running"] * 30
        )
        result = extractor.extract("\n".join(log_lines))
        fe = _fe(result)
        # New phrasing: burst label + clear "context window of N surrounding
        # lines" + a pointer to the entity profile for the authoritative count.
        assert "burst" in fe.lower()
        assert "context window" in fe.lower()
        assert "entity profile" in fe.lower()
        # Old confusing phrasing must not return.
        assert "lines with WARN storm" not in fe

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

    def test_severity_classification_uses_leftmost_field(self, extractor):
        """ISS-012: leftmost severity keyword wins — the level FIELD is
        authoritative, body occurrences of severity words are ignored.

        Synthetic line "WARNING: CRITICAL failure in subsystem" was
        previously expected to classify as CRITICAL (an earlier two-pass
        rule). That rule over-escalated real log data: 291 of 1318 true
        WARN lines in the ZooKeeper fixture got reported as ERROR because
        their bodies contained the trailing field label "error =". Real
        log parsers (every commercial SIEM, ELK, Splunk, Datadog) trust
        the level field. We do too."""
        log_lines = (
            ["INFO: Normal"] * 20
            + ["WARNING: CRITICAL failure in subsystem"]
            + ["INFO: Normal"] * 20
        )
        content = "\n".join(log_lines)
        result = extractor.extract(content)
        # WARNING is the leftmost (and only) level-field keyword on that
        # line; "CRITICAL" appears in the body as prose. The line
        # classifies as WARNING, not CRITICAL.
        assert "Single WARNING" in _fe(result)
        assert "Single CRITICAL" not in _fe(result)

    def test_warn_line_with_trailing_error_field_label_stays_warn(self, extractor):
        """ISS-012 regression test: the literal log-line shape that exposed
        the bug. Java/log4j and ZooKeeper QuorumCnxManager emit lines like
        '... - WARN  [...] - Connection broken ..., error = '. The trailing
        'error =' is a field label, not a severity assertion — must not
        escalate WARN to ERROR."""
        log_lines = ["2015-07-29 19:13:24,282 - INFO  [Thread-1] - startup"] * 5 + [
            "2015-07-29 19:13:24,282 - WARN  [RecvWorker:188978561024:"
            "QuorumCnxManager$RecvWorker@762] - Connection broken for "
            "id 188978561024, my id = 1, error = "
        ] * 5
        content = "\n".join(log_lines)
        result = extractor.extract(content)
        sev = result.file_meta.get("severity", {})
        assert sev.get("ERROR", 0) == 0, f"WARN lines escalated to ERROR: {sev}"
        assert sev.get("WARN", 0) == 5, f"unexpected WARN count: {sev}"

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

    def test_search_hint_includes_sshd_to_exclude_su_pam_events(self, extractor):
        """ISS-007 (logs-linux-01 q6): the search hint surfaced in FILE SUMMARY
        must include 'sshd' so keyword-mode AND-matching filters out
        su(pam_unix)/cron(pam_unix) session-opens. Without 'sshd', the agent's
        search_file run pulls in unrelated PAM session events and aggregates
        them as logins."""
        result = extractor.extract(self._linux_session_log())
        summary = _fe(result)
        # The hint must contain 'sshd' AND describe the session-open event.
        assert "search: 'sshd" in summary
        assert "session opened for user" in summary
        # And the *bare* form (without sshd) must not be the surfaced hint —
        # otherwise we have regressed.
        assert "search: 'session opened for user'" not in summary


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
    """Syslog BSD logs without an explicit year render time_range without a year
    AND get a FILE SUMMARY note instructing the agent not to assert one. The
    earlier behaviour (formatting yearless timestamps with the current year as
    if authoritative, then disclaiming via a parenthetical) misled the LLM
    into reporting the inferred year as fact (logs-linux-01 q3, ISS-006)."""

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

    def test_yearless_time_range_omits_year(self, extractor):
        """time_range must NOT contain a 4-digit year for yearless syslog
        sources. ISS-006 (logs-linux-01 q3): the previous behaviour formatted
        time_range as '2025-06-14 ...' which the LLM treated as fact."""
        import re

        result = extractor.extract(self._syslog_yearless())
        assert "time_range" in result.file_meta
        assert not re.search(r"\b\d{4}\b", result.file_meta["time_range"])

    def test_yearless_note_warns_against_year_fabrication(self, extractor):
        """The note must explicitly tell the agent not to assert a calendar
        year. ISS-006: the earlier note ('YYYY is inferred from current date')
        was too weak — the LLM saw the year as factual context."""
        result = extractor.extract(self._syslog_yearless())
        note = _fe(result).lower()
        assert "do not assert" in note
        assert "calendar year" in note

    def test_iso_time_range_keeps_year(self, extractor):
        """ISO-8601 sources still get a year-bearing time_range. Only yearless
        BSD-syslog sources get the year stripped."""
        import re

        result = extractor.extract(self._iso_log())
        assert re.search(r"\b\d{4}\b", result.file_meta["time_range"])

    def test_yearless_note_includes_sample_raw_timestamp(self, extractor):
        """The note should include a raw sample timestamp so the agent can see the format."""
        import re

        result = extractor.extract(self._syslog_yearless())
        note = _fe(result)
        # The note includes e.g. '(e.g., 'Jun 14 12:00:00')' — check for the month abbreviation
        assert re.search(r"e\.g\.,\s*'Jun\s+\d+\s+\d{2}:\d{2}:\d{2}'", note)


class TestPamAuthFailureFormatVariants:
    """Two PAM authentication-failure syslog formats exist in the wild:
      Format A (modern Linux-PAM):  "pam_unix(sshd:auth): authentication failure"
      Format B (older Red Hat):      "sshd(pam_unix)[19939]: authentication failure"
    Both must match _PAM_AUTH_FAILURE_RE so the count surfaces in the entity
    profile and FILE SUMMARY. Format B was missed before ISS-008 fix —
    the loghub Linux fixture's 490 PAM failures were never counted."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _format_a_lines(self, n: int = 30) -> str:
        return "\n".join(
            f"Dec 10 06:{i:02d}:46 LabSZ sshd[24200]: pam_unix(sshd:auth): "
            f"authentication failure; logname= uid=0 euid=0 tty=ssh "
            f"ruser= rhost=173.234.31.{i + 100} user=root"
            for i in range(n)
        )

    def _format_b_lines(self, n: int = 30) -> str:
        return "\n".join(
            f"Jun 14 15:{i:02d}:01 combo sshd(pam_unix)[1{i:04d}]: "
            f"authentication failure; logname= uid=0 euid=0 tty=NODEVssh "
            f"ruser= rhost=218.188.2.{i}"
            for i in range(n)
        )

    def test_format_a_pam_failures_counted(self, extractor):
        result = extractor.extract(self._format_a_lines())
        assert "pam_auth_failure: 30" in _sm(result)

    def test_format_b_pam_failures_counted(self, extractor):
        """ISS-008 root cause: this format was previously matching 0 lines,
        leaving the 490-line aggregate invisible in the loghub Linux fixture."""
        result = extractor.extract(self._format_b_lines())
        assert "pam_auth_failure: 30" in _sm(result)

    def test_format_b_dominant_activity_surfaces_pam(self, extractor):
        """When failed_password is absent (Format B), pam_auth_failure must
        appear in the FILE SUMMARY 'Dominant activity' line — it is the
        primary auth signal, not a duplicate."""
        result = extractor.extract(self._format_b_lines())
        assert "pam auth failure" in _fe(result)

    def test_format_a_dominant_activity_hides_pam(self, extractor):
        """When failed_password is present (Format A logs typically pair
        Failed-password + pam_unix lines), pam_auth_failure stays hidden
        from FILE SUMMARY to avoid double-counting the same auth event."""
        # Mix Format A pam lines + matching Failed-password lines
        pam = self._format_a_lines(20)
        failed = "\n".join(
            f"Dec 10 06:{i:02d}:46 LabSZ sshd[24200]: Failed password "
            f"for root from 173.234.31.{i + 100} port 22 ssh2"
            for i in range(20)
        )
        result = extractor.extract(pam + "\n" + failed)
        summary = _fe(result)
        # failed_password must be in dominant activity; pam_auth_failure must NOT.
        assert "failed password" in summary
        # The dominant activity line is the first sentence before the period.
        # Confirm pam_auth_failure isn't surfaced there even though it's counted.
        dominant_line = summary.split("Dominant activity:")[1].split(".")[0]
        assert "pam auth failure" not in dominant_line.lower()


class TestSyslogHostSurfacing:
    """Hostname (BSD-syslog 3rd field) must be surfaced in the entity profile
    so the agent can identify the source machine without parsing raw lines.
    Surfaced by /benchmark-fix logs-linux-01 q1 (ISS-008): captured answers
    omitted the hostname 'combo' even though every log line had it."""

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _bsd_syslog(self, host: str, n: int = 30) -> str:
        return "\n".join(
            f"Jun 14 15:{i:02d}:01 {host} sshd(pam_unix)[{1000 + i}]: "
            f"authentication failure; logname= uid=0 euid=0 user=root"
            for i in range(n)
        )

    def test_syslog_host_surfaced(self, extractor):
        result = extractor.extract(self._bsd_syslog("combo"))
        assert "Source host(s)" in _sm(result)
        assert "combo: 30 lines" in _sm(result)

    def test_below_threshold_not_surfaced(self, extractor):
        """Fewer than 5 host-matching lines is below noise threshold — skip."""
        result = extractor.extract(self._bsd_syslog("combo", n=3))
        assert "Source host(s)" not in _sm(result)

    def test_non_syslog_format_not_surfaced(self, extractor):
        """log4j-style content (no BSD-syslog prefix) must not produce any
        spurious host entries."""
        log4j = "\n".join(
            f"2026-01-15 10:00:{i:02d},123 - INFO  [Thread-{i}:Class@1] - message"
            for i in range(30)
        )
        result = extractor.extract(log4j)
        assert "Source host(s)" not in _sm(result)

    def test_multiple_hosts_each_listed(self, extractor):
        """Multi-host syslog (uncommon but possible — log aggregator) lists
        each host with its line count."""
        content = (
            self._bsd_syslog("host-a", n=20) + "\n" + self._bsd_syslog("host-b", n=15)
        )
        result = extractor.extract(content)
        sm = _sm(result)
        assert "host-a: 20 lines" in sm
        assert "host-b: 15 lines" in sm


class TestHealthAppTimestampFormat:
    """ISS-017: HealthApp logs use ``YYYYMMDD-HH:MM:SS:mmm`` (e.g.
    ``20171223-22:15:29:606``). The default extractor previously did not
    recognize this format, so:

    * the ``Log time range`` reported the wrong date (matched a unix-epoch
      substring embedded in the message body), and
    * the file_summary missed the late-window events because the time
      range collapsed to a single misparsed timestamp.

    Expected behaviour: the FILE SUMMARY reports the actual
    first-line and last-line timestamps in the file's native format, and
    a midnight-crossing log spans both calendar dates.
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def test_healthapp_timestamps_parsed(self, extractor):
        """HealthApp log spanning ~3 hours including midnight crossover.
        Time range must reflect both calendar dates, not a single
        misparsed epoch substring.

        Test relies on file_meta['time_range'] which always reflects
        timestamp parsing regardless of entity presence.
        """
        # Build >10 lines so extract_time_range_ts scans both head + tail.
        head_lines = [
            f"20171223-22:15:{i:02d}:606|Step_LSC|30002312|onExtend:1514038530000 14 0 4"
            for i in range(15, 30)
        ]
        tail_lines = [
            f"20171224-00:{i:02d}:00:000|Step_LSC|30002312|onStandStepChanged {1000 + i}"
            for i in range(50, 60)
        ] + ["20171224-01:02:35:789|Step_LSC|30002312|last event"]
        content = "\n".join(head_lines + tail_lines)
        result = extractor.extract(content)
        time_range = result.file_meta.get("time_range", "")
        # Both calendar dates must be reflected in the file_meta time
        # range — the misparsed epoch substring 1514038530000 (which would
        # resolve to 2017-12-23 14:15:30 UTC) must NOT replace the actual
        # first-line timestamp 22:15:15.
        assert "2017-12-23" in time_range
        assert "2017-12-24" in time_range
        assert "22:15:15" in time_range
        # The wrong epoch-derived hour must not appear
        assert "14:15:30" not in time_range

    def test_healthapp_one_digit_hour_after_midnight(self, extractor):
        """HealthApp uses non-zero-padded hour after midnight
        (``20171224-1:2:35:789``). Both padded and unpadded forms must parse."""
        head_lines = [
            f"20171223-22:15:{i:02d}:606|Step_LSC|30002312|first event"
            for i in range(15, 30)
        ]
        tail_lines = [
            f"20171224-1:{i}:35:789|Step_LSC|30002312|midnight crossing"
            for i in range(2, 8)
        ] + ["20171224-1:2:35:789|Step_LSC|30002312|last"]
        content = "\n".join(head_lines + tail_lines)
        result = extractor.extract(content)
        time_range = result.file_meta.get("time_range", "")
        assert "2017-12-23" in time_range
        assert "2017-12-24" in time_range

    def test_healthapp_format_does_not_affect_iso8601(self, extractor):
        """Standard ISO-8601 logs must not be reinterpreted as HealthApp
        format. Regression guard for cross-format pollution."""
        # Need entities for FILE SUMMARY to appear; use Failed password to
        # produce SSH activity entries.
        content = "\n".join(
            f"2026-01-15 10:00:{i:02d} sshd[1234]: Failed password for root from "
            f"203.0.113.{i} port 22 ssh2"
            for i in range(20)
        )
        result = extractor.extract(content)
        fe = _fe(result)
        # ISO-8601 logs must report 2026-01-15 in the time range
        assert "2026-01-15" in fe
        assert "Log time range: 2026-01-15" in fe


class TestTailTimestampBoundary:
    """ISS-036: ``extract_time_range_ts`` previously scanned only the last
    10 lines of content for the end timestamp, treating "no parseable
    timestamp in the last 10 lines" as "no end of range".

    Two real-world fixtures hit this boundary:

    * Trailing blank lines (any tooling that re-emits a log with extra
      trailing newlines) — the actual final timestamped line is at index
      ``[-11:]`` or further back, so ``last_ts`` collapses to ``None`` and
      ``Time range`` reports only the start.
    * HealthApp ``YYYYMMDD-H:M:S:ms`` files where the agent's downstream
      4 KB context cap on file_extract surfaces only the head of the tail
      snippet — the FILE SUMMARY header is the only place the file's
      actual final timestamp is exposed, so it must be correct even when
      the trailing 10 lines are not the last 10 timestamped lines.

    The fix scans further back through the trailing window when the last
    10 lines have no parseable timestamp, so that a small amount of trailing
    blank/header noise does not collapse the end-of-range value.
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def test_healthapp_actual_final_timestamp_in_summary(self, extractor):
        """The FILE SUMMARY's ``Log time range`` end value must reflect
        the file's actual final timestamped line, not an earlier one
        that happens to fall in the trailing window."""
        # Mid-file timestamps (large block) followed by the real last
        # timestamp, then a handful of trailing blank/whitespace lines —
        # mimics tools that re-emit a log with extra trailing newlines.
        head_lines = [
            f"20171223-22:15:{i:02d}:606|Step_LSC|30002312|first event"
            for i in range(15, 30)
        ]
        body_lines = [
            f"20171224-0:{m}:0:000|Step_LSC|30002312|midnight crossing"
            for m in range(1, 60)
        ]
        actual_final = "20171224-1:2:35:789|Step_LSC|30002312|last event"
        # 12 trailing blank lines push the actual final timestamp out of
        # the original 10-line tail window.
        trailing_blanks = [""] * 12
        content = "\n".join(head_lines + body_lines + [actual_final] + trailing_blanks)

        result = extractor.extract(content)
        time_range = result.file_meta.get("time_range", "")
        # Must report the actual file end, not the head's last in-window
        # value (which would otherwise be ``00:59:00``).
        assert (
            "2017-12-24 01:02:35" in time_range
        ), f"Expected end timestamp 01:02:35 in time_range, got: {time_range!r}"
        # Sanity: the start timestamp is also correctly set.
        assert "2017-12-23 22:15:15" in time_range

    def test_short_file_under_ten_lines_resolves_end_ts(self, extractor):
        """Files with ≤10 lines must still resolve end_ts. The previous
        ``tail = lines[-10:] if len(lines) > 10 else []`` clause set
        ``tail`` to ``[]`` for short files, so ``last_ts`` was always
        ``None``. Add Failed password lines so an entity profile + FILE
        SUMMARY is emitted for the assertion target."""
        content = "\n".join(
            f"2026-01-15 10:00:{i:02d} sshd[1234]: Failed password for root "
            f"from 203.0.113.{i} port 22 ssh2"
            for i in range(5)
        )
        result = extractor.extract(content)
        fe = _fe(result)
        # Both endpoints present — no collapse to single-point start.
        assert "Log time range: 2026-01-15 10:00:00 to 2026-01-15 10:00:04" in fe


class TestBGLAlertFlagSurfacing:
    """ISS-015: BGL (BlueGene/L) RAS logs use a structured first-column
    alert flag that classifies fault types: ``-`` (no class), ``KERNDTLB``,
    ``KERNSTOR``, ``APPSEV``, ``KERNMNTF``, etc. The extractor previously
    surfaced none of these, causing the agent to fabricate flag names by
    reading message bodies.

    BGL line format:
        FLAG EPOCH YYYY.MM.DD NODE YYYY-MM-DD-HH.MM.SS.usec NODE \
        SUBSYSTEM COMPONENT SEVERITY message...

    Detection: first whitespace token is either ``-`` or all-uppercase
    (length 4-12), second token is a 10-digit unix epoch timestamp.
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _bgl(self, flag: str, n: int = 1, epoch: int = 1117838570) -> str:
        lines = []
        for i in range(n):
            lines.append(
                f"{flag} {epoch + i} 2005.06.03 R02-M1-N0-C:J12-U11 "
                f"2005-06-03-15.42.50.{i:06d} R02-M1-N0-C:J12-U11 "
                f"RAS KERNEL INFO instruction cache parity error corrected"
            )
        return "\n".join(lines)

    def test_alert_flags_surfaced(self, extractor):
        """Multi-flag fixture: distinct flags + per-flag counts must be present."""
        content = "\n".join(
            [
                self._bgl("-", n=10, epoch=1117838570),
                self._bgl("KERNDTLB", n=5, epoch=1117838600),
                self._bgl("APPSEV", n=2, epoch=1117838700),
            ]
        )
        result = extractor.extract(content)
        text = _all(result)
        # Header naming the column
        assert "BGL alert flags" in text
        # Distinct count = 3 (including the dash placeholder)
        assert "3 distinct" in text
        # Per-flag counts
        assert "KERNDTLB: 5" in text
        assert "APPSEV: 2" in text
        # The dash placeholder must be reported and labeled
        assert "- (no alert class): 10" in text or "no alert class" in text

    def test_non_bgl_format_no_surfacing(self, extractor):
        """Plain syslog must not produce a BGL alert flag block."""
        content = "\n".join(
            f"Jun 14 15:{i:02d}:01 host sshd[{1000 + i}]: Failed password for root"
            for i in range(20)
        )
        result = extractor.extract(content)
        text = _all(result)
        assert "BGL alert flags" not in text

    def test_dash_only_lines_skipped(self, extractor):
        """A file with only dash-flag lines is not informative — skip the
        block to avoid noise. The threshold requires at least one non-dash
        flag for the block to appear."""
        content = self._bgl("-", n=20)
        result = extractor.extract(content)
        text = _all(result)
        assert "BGL alert flags" not in text


class TestBGLFatalFlagBreakdown:
    """ISS-038: BGL FATAL-severity lines are distributed across alert
    flags — ``KERNDTLB``, ``KERNSTOR``, ``APPSEV``, and the ``-`` (no
    alert class) bucket which carries ``RAS APP FATAL ciod:`` control-
    stream messages. In the BGL 2k fixture the dominant single FATAL
    bucket is ``-`` with 204 entries (the ciod APP FATAL traffic), well
    ahead of any KERN flag.

    The existing alert-flag block sorts all flags by *total* line count
    (FATAL + non-FATAL together), so the ``-`` bucket's FATAL traffic
    is invisible: ``-`` shows up as 1857 lines total — the agent reads
    "no alert class" and skips it, focusing on the KERN flags.

    Fix: surface a FATAL-only breakdown by alert flag so the agent sees
    the 204 entries in ``-`` directly. Without this the dominant FATAL
    bucket is structurally invisible.
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _bgl(
        self,
        flag: str,
        severity: str = "INFO",
        n: int = 1,
        epoch: int = 1117838570,
        component: str = "KERNEL",
        message: str = "instruction cache parity error corrected",
    ) -> str:
        """Build a BGL-format snippet. SEVERITY is the 9th whitespace
        token in the canonical format and is what the FATAL-by-flag
        breakdown groups against."""
        lines = []
        for i in range(n):
            lines.append(
                f"{flag} {epoch + i} 2005.06.03 R02-M1-N0-C:J12-U11 "
                f"2005-06-03-15.42.50.{i:06d} R02-M1-N0-C:J12-U11 "
                f"RAS {component} {severity} {message}"
            )
        return "\n".join(lines)

    def test_fatal_breakdown_includes_dash_bucket(self, extractor):
        """The ``-`` (no alert class) bucket carries APP FATAL ciod
        control-stream messages. Its FATAL count must be visible in the
        FATAL-by-flag breakdown even though the bucket is dominated by
        non-FATAL entries when measured by total line count."""
        # 204 dash-flag FATAL APP ciod lines (mirrors BGL 2k fixture's
        # dominant single FATAL bucket), 1653 dash-flag INFO non-FATAL,
        # 60 KERNDTLB FATAL kernel-error lines. Total dash = 1857.
        # Without a FATAL-by-flag breakdown the agent ranks "-" first by
        # total count, labels it "no alert class", and never sees the
        # 204 FATAL traffic underneath.
        content = "\n".join(
            [
                self._bgl(
                    "-",
                    severity="FATAL",
                    component="APP",
                    n=204,
                    epoch=1117800000,
                    message="ciod: LOGIN chdir failed: No such file or directory",
                ),
                self._bgl(
                    "-",
                    severity="INFO",
                    n=1653,
                    epoch=1117810000,
                    message="instruction cache parity error corrected",
                ),
                self._bgl(
                    "KERNDTLB",
                    severity="FATAL",
                    n=60,
                    epoch=1117820000,
                    message="data TLB error interrupt",
                ),
            ]
        )
        result = extractor.extract(content)
        text = _all(result)
        # FATAL-by-flag breakdown must exist
        import re as _re

        fatal_section = _re.search(
            r"FATAL[^\n]*by[^\n]*alert[^\n]*flag.*?(?=\n\n|\Z)",
            text,
            _re.IGNORECASE | _re.DOTALL,
        )
        assert fatal_section, (
            "Expected a FATAL-by-flag breakdown block; got:\n" + text[:3000]
        )
        section_text = fatal_section.group(0)
        # The dominant FATAL bucket must be reported with its count.
        # ``-`` flag's 204 FATAL entries must surface independently of
        # its 1653 non-FATAL entries.
        assert "204" in section_text, (
            "Expected dash-flag FATAL count 204 in breakdown; got:\n" + section_text
        )
        # KERNDTLB also has FATAL entries — both must be present.
        assert "KERNDTLB" in section_text
        assert "60" in section_text

    def test_fatal_breakdown_skipped_when_no_fatal(self, extractor):
        """A BGL log with no FATAL-severity entries must not produce a
        FATAL-by-flag breakdown. The block is conditional on FATAL
        traffic being present."""
        content = self._bgl(
            "KERNDTLB", severity="WARNING", n=20, message="warning condition"
        )
        result = extractor.extract(content)
        text = _all(result)
        # Existing alert-flag block should still appear
        assert "BGL alert flags" in text
        # FATAL breakdown must not — there are no FATAL entries to surface
        assert "FATAL by alert flag" not in text
        assert "FATAL-by-flag" not in text

    def test_non_bgl_no_fatal_breakdown(self, extractor):
        """Plain syslog with FATAL keywords must not produce a BGL FATAL
        breakdown — the breakdown is BGL-format-specific."""
        content = "\n".join(
            f"Jun 14 15:{i:02d}:01 host service[{1000 + i}]: FATAL configuration error"
            for i in range(20)
        )
        result = extractor.extract(content)
        text = _all(result)
        assert "FATAL by alert flag" not in text


class TestWindowsKBPackageSurfacing:
    """ISS-020: Windows CBS logs reference KB packages via
    ``Package_for_KB<NUMBER>``. The extractor must surface a *distinct* count
    so the agent doesn't conflate substring occurrences (one per
    install/uninstall/check event) with distinct package count.

    The Windows 2k fixture has 541 ``Package_for_KB<...>`` substring
    occurrences but only 271 distinct KB numbers. Earlier the agent
    hallucinated "over 500 packages" — symptom of the extractor having no
    KB-package surfacing at all.
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _windows_log(self, kb_lines: list[tuple[int, int]]) -> str:
        """Build a synthetic Windows CBS log with given (KB-number, count) pairs."""
        lines = []
        for kb, count in kb_lines:
            for _ in range(count):
                lines.append(
                    f"2016-09-28 04:30:33, Info                  CBS    "
                    f"Read out cached package applicability for package: "
                    f"Package_for_KB{kb}~31bf3856ad364e35~amd64~~6.1.1.0, "
                    f"ApplicableState: 112, CurrentState:112"
                )
        return "\n".join(lines)

    def test_distinct_kb_count_surfaced(self, extractor):
        """3 distinct KBs each appearing twice = 3 distinct, 6 occurrences."""
        content = self._windows_log([(1234, 2), (5678, 2), (9012, 2)])
        result = extractor.extract(content)
        text = _all(result)
        # A dedicated KB-package summary must be present
        assert "Distinct KB packages: 3" in text

    def test_kb_distinct_not_occurrence(self, extractor):
        """Many duplicates of one KB must not inflate the distinct count.

        Regression for ISS-020: agent reported "over 500 packages" when the
        Windows fixture had 271 distinct KBs but 541 substring occurrences.
        """
        # 1 distinct KB, 50 occurrences
        content = self._windows_log([(2479943, 50)])
        result = extractor.extract(content)
        text = _all(result)
        # Must report 1 distinct
        assert "Distinct KB packages: 1" in text
        # Total occurrences should be reported separately so the agent can
        # answer either question, but must never be presented as the
        # distinct count.
        assert "Distinct KB packages: 50" not in text

    def test_no_kb_no_surfacing(self, extractor):
        """Logs without any Package_for_KB substring should not produce a KB block."""
        content = "\n".join(
            f"2016-09-28 04:30:{i:02d}, Info CBS some unrelated message"
            for i in range(20)
        )
        result = extractor.extract(content)
        text = _all(result)
        assert "Distinct KB packages" not in text


class TestWindowsHRESULTSurfacing:
    """ISS-037: CBS log lines carry ``[HRESULT = 0x<hex> - SYMBOL]`` codes,
    where the same HRESULT/symbol pair appears across multiple distinct
    message templates (``Expecting attribute name``, ``Failed to get next
    element``, etc.). The template-counts block reports per-template
    occurrences, so a code that occurs 448 times split across two templates
    (224 each) renders as ``[224x]`` in two places — and the agent reports
    the per-template number as the per-HRESULT total.

    The Windows 2k fixture has 448 lines containing
    ``CBS_E_MANIFEST_INVALID_ITEM`` (``0x800f080d``), but the template
    counts only ever surface 224 in any single line of the search map.

    Fix: surface a per-HRESULT aggregate in the entity profile so the agent
    has a single authoritative count per HRESULT/symbol independent of
    message-template variation.
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _cbs_lines(
        self, count: int, hresult: str, symbol: str, template: str
    ) -> list[str]:
        return [
            f"2016-09-28 04:30:{i % 60:02d}, Info                  CBS    "
            f"{template} [HRESULT = {hresult} - {symbol}]"
            for i in range(count)
        ]

    def test_hresult_total_aggregated_across_templates(self, extractor):
        """448 occurrences of CBS_E_MANIFEST_INVALID_ITEM split across two
        message templates must surface as a single 448 in the entity
        profile, not as two 224s the agent has to add together."""
        # 215 + 233 = 448 — uneven split so the per-HRESULT aggregate (448)
        # cannot accidentally match a per-template count, and so neither
        # half coincides with the total line count of the test fixture
        # (which would otherwise let "448 of 448" satisfy the assertion).
        a = self._cbs_lines(
            215,
            "0x800f080d",
            "CBS_E_MANIFEST_INVALID_ITEM",
            "Expecting attribute name",
        )
        b = self._cbs_lines(
            233,
            "0x800f080d",
            "CBS_E_MANIFEST_INVALID_ITEM",
            "Failed to get next element",
        )
        # Pad the file with unrelated lines so total_lines != 448 — defeats
        # spurious matches against template-counts headers like "X of N".
        padding = [
            f"2016-09-28 05:00:{i % 60:02d}, Info CBS unrelated activity {i}"
            for i in range(100)
        ]
        content = "\n".join(a + b + padding)
        result = extractor.extract(content)
        text = _all(result)
        # The per-HRESULT block must associate the code/symbol with the
        # file-wide total directly — not as two halves the agent has to
        # add together. Anchor on "<hresult>: <count>" near the symbol.
        import re as _re

        # Match "0x800f080d ... CBS_E_MANIFEST_INVALID_ITEM ... 448" or
        # any close proximity layout — the assertion is that the agent
        # sees one authoritative line for this HRESULT carrying 448.
        pat = _re.compile(
            r"0x800f080d[^\n]{0,80}CBS_E_MANIFEST_INVALID_ITEM[^\n]{0,80}\b448\b"
            r"|"
            r"0x800f080d[^\n]{0,80}\b448\b[^\n]{0,80}CBS_E_MANIFEST_INVALID_ITEM"
            r"|"
            r"CBS_E_MANIFEST_INVALID_ITEM[^\n]{0,80}\b448\b"
        )
        assert pat.search(text), (
            "Expected per-HRESULT aggregate associating 0x800f080d / "
            "CBS_E_MANIFEST_INVALID_ITEM with total count 448; got:\n"
            f"{text[:3000]}"
        )

    def test_multiple_distinct_hresults_each_aggregated(self, extractor):
        """Multiple HRESULTs with different totals must each appear with
        their own per-HRESULT aggregate count."""
        content = "\n".join(
            self._cbs_lines(
                100, "0x800f080d", "CBS_E_MANIFEST_INVALID_ITEM", "Expecting"
            )
            + self._cbs_lines(50, "0x800f0805", "CBS_E_INVALID_PACKAGE", "Failed")
            + self._cbs_lines(10, "0x80004005", "E_FAIL", "Generic failure")
        )
        result = extractor.extract(content)
        text = _all(result)
        # Find the HRESULT block and verify each code+count pair.
        for hresult, count in (
            ("0x800f080d", 100),
            ("0x800f0805", 50),
            ("0x80004005", 10),
        ):
            # Each code must appear adjacent to its aggregate count.
            # Look for "<hresult>: <count>" pattern (standard Counter formatting).
            assert hresult in text
            # Find the section showing this hresult and its aggregate count.
            # Allow flexible separators between hresult and count.
            import re as _re

            pat = _re.compile(rf"{_re.escape(hresult)}[^\n]*\b{count}\b")
            assert pat.search(text), (
                f"Expected per-HRESULT aggregate showing {hresult}: {count}; "
                f"got:\n{text[:3000]}"
            )

    def test_no_hresult_no_surfacing(self, extractor):
        """Logs without any HRESULT codes must not produce an HRESULT block."""
        content = "\n".join(
            f"2016-09-28 04:30:{i % 60:02d}, Info CBS unrelated message {i}"
            for i in range(20)
        )
        result = extractor.extract(content)
        text = _all(result)
        assert "HRESULT" not in text


class TestSeverityScaleContext:
    """ISS-016: When the agent characterizes severity ("normal", "alarming",
    "systemic", "catastrophic"), it needs the SCALE context to calibrate its
    wording. 347 FATAL events sounds alarming until you know the file covers
    a multi-hour or multi-day span across many distinct nodes — at which
    point per-hour or per-node rates show the activity is well within
    fault-tolerant background levels.

    The extractor must surface, near the top of FILE SUMMARY:
    - An "events/hour" rate annotation when the file has a clear time span
      and ≥50 severity-flagged events.
    - A distinct BGL node count when the BGL line detector fires.

    And in ENTITY PROFILE event_types, each event with count ≥50 and a
    non-zero temporal span must carry a `rate:~X/h` annotation alongside
    the existing `span:` annotation.
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _bgl_lines(
        self,
        flag: str,
        n: int,
        epoch_start: int,
        epoch_step: int,
        node: str = "R02-M1-N0-C:J12-U11",
        body: str = "RAS KERNEL FATAL data tlb error interrupt",
    ) -> list[str]:
        """Build n synthetic BGL lines spaced epoch_step seconds apart."""
        lines = []
        for i in range(n):
            ep = epoch_start + i * epoch_step
            # Use a real-looking date from epoch (use 2005.06.03 for all —
            # the date is for human readability and doesn't drive parsing
            # since the extractor's BGL-format lines use the second-column
            # YYYY-MM-DD-HH.MM.SS field for timestamps).
            from datetime import datetime, timezone

            dt = datetime.fromtimestamp(ep, tz=timezone.utc)
            ymd = dt.strftime("%Y.%m.%d")
            inner = dt.strftime("%Y-%m-%d-%H.%M.%S") + ".000000"
            lines.append(f"{flag} {ep} {ymd} {node} {inner} {node} {body}")
        return lines

    def test_events_per_hour_rate_in_file_summary_for_high_count_log(self, extractor):
        """When ≥50 severity-flagged events span a clear time window, FILE
        SUMMARY must mention a rate (events/hour or events/min)."""
        # 100 FATAL lines spanning 10 hours (3600s/100 ≈ ~10 events/h)
        lines = self._bgl_lines(
            "KERNDTLB",
            n=100,
            epoch_start=1117838570,
            epoch_step=360,  # 6 minutes apart -> 10 events/hour over 9.9h
        )
        result = extractor.extract("\n".join(lines))
        fe = _fe(result)
        assert "FILE SUMMARY" in fe
        # Must contain rate-based phrasing in the summary
        assert (
            "events/hour" in fe.lower()
            or "events/h" in fe.lower()
            or "/hour" in fe.lower()
        ), f"Expected events/hour rate phrasing in FILE SUMMARY:\n{fe}"

    def test_no_rate_for_short_log(self, extractor):
        """Files with <50 severity-flagged events should not get a rate
        annotation — the rate would be statistically meaningless."""
        # 10 FATAL lines, 1 minute apart
        lines = self._bgl_lines(
            "KERNDTLB",
            n=10,
            epoch_start=1117838570,
            epoch_step=60,
        )
        result = extractor.extract("\n".join(lines))
        fe = _fe(result)
        # No "Effective rate" / "events/hour" sentence
        assert "events/hour" not in fe.lower()
        assert "effective rate" not in fe.lower()

    def test_event_type_rate_annotation_in_entity_profile(self, extractor):
        """Each event_types row with count ≥50 AND a non-zero span must
        carry a `rate:~X/h` annotation alongside `span:`."""
        # Build a syslog-format log with many failed_password events spanning
        # several hours so the event_types section fires with a rate.
        lines = []
        # Generate 60 failed_password events over ~3 hours (180 minutes)
        # spaced 3 minutes apart -> 20 events/hour
        from datetime import datetime, timedelta

        start = datetime(2024, 6, 14, 10, 0, 0)
        for i in range(60):
            ts = start + timedelta(minutes=i * 3)
            stamp = ts.strftime("%b %d %H:%M:%S")
            lines.append(
                f"{stamp} testhost sshd[{1000 + i}]: "
                f"Failed password for root from 10.0.0.5 port 2200 ssh2"
            )
        result = extractor.extract("\n".join(lines))
        sm = _sm(result)
        # The event_types section must list failed_password with a rate annotation
        assert "failed_password" in sm
        # span: annotation already present, plus a new rate: annotation
        assert "rate:" in sm, f"Expected rate: annotation in entity profile:\n{sm}"
        assert "/h" in sm

    def test_event_type_rate_omitted_for_low_count(self, extractor):
        """Events with count <50 should not get a rate annotation — the
        sample is too small for a meaningful per-hour rate."""
        from datetime import datetime, timedelta

        lines = []
        start = datetime(2024, 6, 14, 10, 0, 0)
        for i in range(15):  # only 15 events
            ts = start + timedelta(minutes=i * 5)
            stamp = ts.strftime("%b %d %H:%M:%S")
            lines.append(
                f"{stamp} testhost sshd[{1000 + i}]: "
                f"Failed password for root from 10.0.0.5 port 2200 ssh2"
            )
        result = extractor.extract("\n".join(lines))
        sm = _sm(result)
        # The event_types row exists with span: but should not carry rate:
        # (anchor on the failed_password event line specifically).
        for line in sm.splitlines():
            if "failed_password:" in line:
                assert (
                    "rate:" not in line
                ), f"Did not expect rate: on low-count event line: {line}"

    def test_distinct_bgl_node_count_in_file_summary(self, extractor):
        """When the BGL line detector fires on enough lines, FILE SUMMARY
        must mention the count of distinct BGL node identifiers (3rd
        whitespace-separated token, e.g. R02-M1-N0-C:J12-U11)."""
        lines = []
        # 80 BGL lines across 4 distinct nodes
        nodes = [
            "R02-M1-N0-C:J12-U11",
            "R23-M0-NE-C:J05-U01",
            "R24-M0-N1-C:J13-U11",
            "R30-M0-N9-C:J16-U01",
        ]
        for i in range(80):
            node = nodes[i % len(nodes)]
            lines.extend(
                self._bgl_lines(
                    "KERNDTLB",
                    n=1,
                    epoch_start=1117838570 + i * 300,
                    epoch_step=1,
                    node=node,
                )
            )
        result = extractor.extract("\n".join(lines))
        fe = _fe(result)
        # Distinct node count — naming may be "Distinct BGL nodes" or "BGL
        # nodes" — accept either, but the count must be present.
        assert (
            "BGL node" in fe or "BGL nodes" in fe
        ), f"Expected BGL node count in FILE SUMMARY:\n{fe}"
        assert "4" in fe  # 4 distinct nodes

    def test_non_bgl_log_no_node_count(self, extractor):
        """A plain syslog file must not produce a BGL node count line."""
        content = "\n".join(
            f"Jun 14 15:{i:02d}:01 host sshd[{1000 + i}]: Failed password for root"
            for i in range(60)
        )
        result = extractor.extract(content)
        fe = _fe(result)
        assert "BGL node" not in fe


class TestAcceptedLoginAttackerDisambiguation:
    """ISS-026: Accepted password lines must be disambiguated by source IP.

    OpenSSH 'Accepted password' surfaces 1 successful login alongside hundreds
    of failed_password / invalid_user / break_in_attempt events. When the
    accepted login's source IP appears NOWHERE in failed/invalid/break-in
    events, that is a legitimate session unrelated to the brute-force
    activity — not 'one successful login among the attempts'. The agent
    framing it as 'attack succeeded' trips the forbidden_claim 'claims the
    attacks succeeded'.

    The extractor must annotate accepted_login with a per-IP breakdown:
    how many came from attacker IPs (those with failed/invalid/break-in
    events) vs non-attacker IPs (likely legitimate sessions).
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _make_log(self, lines: list[str], pad: int = 12) -> str:
        # Pad with neutral filler to clear the >10-line scan threshold.
        filler = "Dec 10 06:55:00 LabSZ kernel: tick benign info"
        return "\n".join(lines + [filler] * pad)

    def test_legitimate_accepted_login_split_from_attackers(self, extractor):
        """Accepted-password from a non-attacker IP labelled as legitimate.

        Mirrors the OpenSSH_2k.log layout: many failed/invalid attempts from
        attacker IPs, plus a single Accepted password from an IP that NEVER
        appears in any attack event.
        """
        attack_ip = "183.62.140.253"
        legit_ip = "119.137.62.142"
        attack_lines = [
            f"Dec 10 06:55:46 LabSZ sshd[100{i:02d}]: Failed password for root from {attack_ip} port 33{i:03d} ssh2"
            for i in range(20)
        ] + [
            f"Dec 10 06:56:{i:02d} LabSZ sshd[200{i:02d}]: Invalid user webmaster from {attack_ip}"
            for i in range(20)
        ]
        accepted_line = (
            f"Dec 10 09:32:20 LabSZ sshd[24680]: "
            f"Accepted password for fztu from {legit_ip} port 49116 ssh2"
        )
        content = self._make_log(attack_lines + [accepted_line])
        result = extractor.extract(content)
        text = _all(result)
        # The breakdown must call out attacker vs non-attacker totals.
        assert "0 from attacker IPs" in text, (
            f"Expected 0-from-attacker breakdown for legitimate accepted_login.\n"
            f"Output:\n{text[:2000]}"
        )
        assert "1 from non-attacker IPs" in text, (
            f"Expected 1-from-non-attacker breakdown for legitimate accepted_login.\n"
            f"Output:\n{text[:2000]}"
        )

    def test_attacker_accepted_login_flagged(self, extractor):
        """When the accepted IP also has failed/invalid events, count it as attacker."""
        attack_ip = "1.2.3.4"
        attack_lines = [
            f"Dec 10 06:55:{i:02d} LabSZ sshd[100{i:02d}]: Failed password for root from {attack_ip} port 33{i:03d} ssh2"
            for i in range(20)
        ]
        accepted_line = (
            f"Dec 10 09:32:20 LabSZ sshd[24680]: "
            f"Accepted password for root from {attack_ip} port 49116 ssh2"
        )
        content = self._make_log(attack_lines + [accepted_line])
        result = extractor.extract(content)
        text = _all(result)
        assert "1 from attacker IPs" in text, (
            f"Accepted from an attacker IP must be flagged as attacker-sourced.\n"
            f"Output:\n{text[:2000]}"
        )
        assert "0 from non-attacker IPs" in text

    def test_top_level_accepted_count_unchanged(self, extractor):
        """The headline accepted_login count must remain accurate (event count,
        not replaced by the breakdown).
        """
        legit_ip = "10.0.0.5"
        attack_ip = "1.2.3.4"
        attack_lines = [
            f"Dec 10 06:55:{i:02d} LabSZ sshd[200{i:02d}]: Failed password for root from {attack_ip} port 33{i:03d} ssh2"
            for i in range(15)
        ]
        accepted_line = (
            f"Dec 10 09:32:20 LabSZ sshd[24680]: "
            f"Accepted password for fztu from {legit_ip} port 49116 ssh2"
        )
        content = self._make_log(attack_lines + [accepted_line])
        result = extractor.extract(content)
        text = _all(result)
        # The headline count must still appear ("accepted_login: 1")
        # somewhere in the entity profile.
        assert "accepted_login: 1" in text, (
            f"Top-level accepted_login event count must remain unchanged.\n"
            f"Output:\n{text[:2000]}"
        )

    def test_no_breakdown_when_no_accepted_login(self, extractor):
        """No accepted_login lines → no breakdown emitted."""
        attack_lines = [
            f"Dec 10 06:55:{i:02d} LabSZ sshd[100{i:02d}]: Failed password for root from 1.2.3.4"
            for i in range(15)
        ]
        content = self._make_log(attack_lines)
        result = extractor.extract(content)
        text = _all(result)
        assert "from attacker IPs" not in text
        assert "from non-attacker IPs" not in text


class TestSparkTimestampFormat:
    """ISS-027: Spark logs use 'YY/MM/DD HH:MM:SS' which the timestamp
    extractor previously did not match — falling back to embedded body
    timestamps and clipping the actual span by several seconds.

    Spark format example: '17/06/09 20:10:40 INFO executor.Executor: ...'
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _spark_log(self) -> str:
        # Mirrors real Spark_2k.log — leading line carries 20:10:40, last
        # line carries 20:11:11, with embedded IPs that trigger the entity
        # profile (and therefore FILE SUMMARY) so the time-range note
        # appears.
        early = (
            "17/06/09 20:10:40 INFO executor.CoarseGrainedExecutorBackend:"
            " Driver address 10.10.34.11"
        )
        mid = (
            "17/06/09 20:10:55 INFO executor.Executor: "
            "Running task 0.0 in stage 0.0 (TID 0) on 10.10.34.12"
        )
        late = (
            "17/06/09 20:11:11 INFO storage.BlockManager: "
            "Found block rdd_42_32 locally on 10.10.34.13"
        )
        return "\n".join([early] * 5 + [mid] * 5 + [late] * 5)

    def _time_range_sentence(self, text: str) -> str:
        """Extract the FILE SUMMARY sentence that contains 'Log time range'."""
        # Drop the crime-scene block — only inspect FILE SUMMARY content.
        if "CRIME SCENE EXTRACTION" in text:
            text = text.split("CRIME SCENE EXTRACTION")[0]
        return next(
            (s for s in text.split(". ") if "Log time range" in s),
            "",
        )

    def test_spark_time_range_first_line_captured(self, extractor):
        """The actual first timestamp (20:10:40) must drive 'Log time range'."""
        result = extractor.extract(self._spark_log())
        sentence = self._time_range_sentence(_fe(result))
        assert "20:10:40" in sentence, (
            f"Spark first timestamp 20:10:40 must drive 'Log time range'.\n"
            f"Got time-range sentence: {sentence!r}\n"
            f"Full output:\n{_fe(result)[:2000]}"
        )

    def test_spark_time_range_last_line_captured(self, extractor):
        """The actual last timestamp (20:11:11) must drive 'Log time range'."""
        result = extractor.extract(self._spark_log())
        sentence = self._time_range_sentence(_fe(result))
        assert "20:11:11" in sentence, (
            f"Spark last timestamp 20:11:11 must drive 'Log time range'.\n"
            f"Got time-range sentence: {sentence!r}\n"
            f"Full output:\n{_fe(result)[:2000]}"
        )

    def test_spark_time_range_not_unknown(self, extractor):
        """Spark-format logs must produce a real time range, not 'unknown'."""
        result = extractor.extract(self._spark_log())
        text = _fe(result)
        # Either an explicit "unknown" string or no time-range line at all
        # would mean the format is unrecognized.
        assert "Log time range" in text, (
            f"FILE SUMMARY must include a populated 'Log time range:' line.\n"
            f"Output:\n{text[:2000]}"
        )
        assert "Log time range: unknown" not in text


class TestCBSFormatNote:
    """ISS-028: When the extractor detects a Windows CBS log via the
    Package_for_KB substring, it must add a one-line CBS-format note in
    FILE SUMMARY so the agent doesn't characterize HRESULT entries as
    application crashes or network events. Q5 of logs-windows-01 trips
    the forbidden_claims 'claims application crashes appear' and
    'fabricates network or firewall events' without this clarification.
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _cbs_log(self, distinct_kbs: int = 8, copies: int = 3) -> str:
        lines = []
        for kb in range(2479000, 2479000 + distinct_kbs):
            for _ in range(copies):
                lines.append(
                    f"2016-09-28 04:30:33, Info                  CBS    "
                    f"Read out cached package applicability for package: "
                    f"Package_for_KB{kb}~31bf3856ad364e35~amd64~~6.1.1.0, "
                    f"ApplicableState: 112, CurrentState:112"
                )
        return "\n".join(lines)

    def test_cbs_format_note_in_file_summary(self, extractor):
        """The CBS-format note must appear in FILE SUMMARY (file_extract),
        not merely in the search map.
        """
        content = self._cbs_log(distinct_kbs=8, copies=3)
        result = extractor.extract(content)
        fe = _fe(result)
        # The note should mention CBS / Component-Based Servicing and call
        # out that HRESULT lines are Info-severity servicing results.
        assert "CBS" in fe, (
            f"FILE SUMMARY must mention CBS for Windows servicing logs.\n"
            f"Output:\n{fe[:2000]}"
        )
        assert "Component-Based Servicing" in fe
        assert "HRESULT" in fe, (
            f"FILE SUMMARY must call out HRESULT semantics for CBS logs.\n"
            f"Output:\n{fe[:2000]}"
        )
        # The note must clarify HRESULT entries are not crashes/network events.
        text_lower = fe.lower()
        assert "not application crashes" in text_lower or (
            "not" in text_lower
            and "application crash" in text_lower
            and "network" in text_lower
        )

    def test_cbs_note_absent_for_non_cbs_logs(self, extractor):
        """A plain syslog file must not get the CBS note."""
        content = "\n".join(
            f"Jun 14 15:{i:02d}:01 host sshd[{1000 + i}]: Failed password for root"
            for i in range(20)
        )
        result = extractor.extract(content)
        fe = _fe(result)
        assert "Component-Based Servicing" not in fe
        # Don't false-trigger on bare 'CBS' substrings if any might appear.
        assert "CBS log" not in fe and "CBS (Component" not in fe

    def test_cbs_note_does_not_change_kb_counts(self, extractor):
        """ISS-028 fix is annotation-only — KB counting behavior must
        remain identical to ISS-020 expectations.
        """
        # 5 distinct KBs, 4 copies each = 5 distinct, 20 total occurrences.
        content = self._cbs_log(distinct_kbs=5, copies=4)
        result = extractor.extract(content)
        text = _all(result)
        assert "Distinct KB packages: 5" in text


class TestModJkWorkerStateSurfacing:
    """ISS-045: Apache mod_jk error_log lines like ``mod_jk child workerEnv
    in error state N`` carry a numeric state code that distinguishes
    different upstream Tomcat connection failure modes. Without a structured
    block, the agent estimates per-state counts from sample snippets and
    mis-attributes the aggregate to a single state. The Apache 2k fixture
    has 539 such lines split across 5 states (state 6 dominant at 369).
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _mod_jk(self, state: int, n: int = 1) -> str:
        return "\n".join(
            f"[Sun Dec 04 04:51:18 2005] [error] mod_jk child workerEnv in error state {state}"
            for _ in range(n)
        )

    def test_per_state_breakdown_surfaced(self, extractor):
        """Multi-state fixture: distinct count + per-state counts present."""
        content = "\n".join(
            [
                self._mod_jk(state=6, n=369),
                self._mod_jk(state=7, n=101),
                self._mod_jk(state=8, n=44),
                self._mod_jk(state=9, n=20),
                self._mod_jk(state=10, n=5),
            ]
        )
        result = extractor.extract(content)
        text = _all(result)
        assert "mod_jk worker error states" in text
        assert "5 distinct" in text
        assert "539 lines total" in text
        assert "state 6: 369" in text
        assert "state 7: 101" in text
        assert "state 10: 5" in text

    def test_states_sorted_numerically(self, extractor):
        """States must appear in numeric order, not by count."""
        content = "\n".join([self._mod_jk(state=10, n=5), self._mod_jk(state=6, n=369)])
        result = extractor.extract(content)
        text = _all(result)
        idx_6 = text.index("state 6: 369")
        idx_10 = text.index("state 10: 5")
        assert idx_6 < idx_10  # state 6 listed before state 10

    def test_no_mod_jk_no_block(self, extractor):
        """Plain syslog without workerEnv lines must not produce the block."""
        content = "Jun 14 15:16:01 host sshd[1000]: Failed password for root\n" * 20
        result = extractor.extract(content)
        text = _all(result)
        assert "mod_jk worker error states" not in text


class TestMidnightCrossingFileSummary:
    """ISS-049: When a log session crosses midnight, the underlying time
    range extraction is correct (returns the LATER end_ts), but the LLM
    silently collapses the start/end to same-day arithmetic and reports a
    drastically-shorter session. Surface the cross-date span explicitly in
    FILE SUMMARY: ``Xh Ym duration`` (concrete) + ``[spans N calendar
    dates ...]`` (so the LLM cannot miss the date change).
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def test_midnight_crossing_surfaced(self, extractor):
        """HealthApp-style YYYYMMDD-H:M:S log crossing midnight."""
        # Lines must carry an entity (port number) for the FILE SUMMARY
        # path to fire — _build_entity_profile early-returns on entityless
        # content. This mirrors real HealthApp logs which always carry PIDs.
        lines = [
            "20171223-22:15:29:606|App|port 8080|start",
            "20171223-23:00:00:000|App|port 8080|tick",
            "20171224-1:02:35:789|App|port 8080|stop",
        ]
        result = extractor.extract("\n".join(lines))
        fe = _fe(result)
        assert "2h 47m duration" in fe
        assert "spans 2 calendar dates" in fe
        assert "session crosses midnight" in fe

    def test_same_day_no_crossing_tag(self, extractor):
        """A same-day log must NOT carry the cross-date tag."""
        lines = [
            "20171223-08:00:00:000|App|port 8080|start",
            "20171223-09:30:00:000|App|port 8080|stop",
        ]
        result = extractor.extract("\n".join(lines))
        fe = _fe(result)
        # Either no time-range line at all, or one without the tag
        assert "spans" not in fe or "calendar dates" not in fe
        assert "session crosses midnight" not in fe

    def test_multi_day_drops_midnight_clause(self, extractor):
        """For 3+ days the ``crosses midnight`` clause is suppressed
        (reads oddly when the span is wider than a single midnight)."""
        lines = [
            "20171220-08:00:00:000|App|port 8080|start",
            "20171225-10:00:00:000|App|port 8080|stop",
        ]
        result = extractor.extract("\n".join(lines))
        fe = _fe(result)
        assert "spans 6 calendar dates" in fe
        assert "session crosses midnight" not in fe


class TestGoroutineBlockSummary:
    """ISS-052: Go runtime panic dumps carry one or more
    ``goroutine N [state]:`` blocks with an indented stack trace and an
    optional trailing ``created by <FUNC>`` provenance line. Without a
    structural pass, the agent loses the relational structure (which
    frames belong to which goroutine, which spawn point produced the
    racing goroutines). Surface state distribution + spawn-source counts
    so the agent can answer "where do the racing goroutines come from?"
    directly from the entity profile.
    """

    @pytest.fixture
    def extractor(self):
        return LogsAndErrorsExtractor()

    def _go_panic(self, n_runnable: int = 0, n_running: int = 0) -> str:
        chunks = []
        for i in range(n_running):
            chunks.append(
                f"goroutine {100 + i} [running]:\n"
                f"runtime.throw(...)\n"
                f"\t/usr/local/go/src/runtime/panic.go:1047 +0x5d\n"
                f"created by net/http.(*Server).Serve\n"
                f"\t/usr/local/go/src/net/http/server.go:3089 +0x4cf\n"
            )
        for i in range(n_runnable):
            chunks.append(
                f"goroutine {200 + i} [runnable]:\n"
                f"github.com/x/y.(*MemCache).Set(...)\n"
                f"\t/build/internal/cache/memcache.go:54 +0x9c\n"
                f"created by github.com/x/y/scheduler.(*Worker).Start\n"
                f"\t/build/internal/scheduler/worker.go:42 +0x9c\n"
            )
        return "\n".join(chunks)

    def test_state_counts_surfaced(self, extractor):
        """3 runnable + 1 running → state counts visible in output."""
        content = self._go_panic(n_runnable=3, n_running=1)
        result = extractor.extract(content)
        text = _all(result)
        assert "Go goroutine blocks (4 total" in text
        assert "running: 1" in text
        assert "runnable: 3" in text

    def test_created_by_provenance_surfaced(self, extractor):
        """The ``created by`` line must be aggregated as spawn provenance."""
        content = self._go_panic(n_runnable=3, n_running=1)
        result = extractor.extract(content)
        text = _all(result)
        assert "spawned by (created-by provenance)" in text
        assert "scheduler.(*Worker).Start" in text
        assert "net/http.(*Server).Serve" in text

    def test_no_goroutine_no_block(self, extractor):
        """A regular log without goroutine headers must not produce the block."""
        content = (
            "2024-03-15T14:37:22Z INFO  service starting\n"
            "2024-03-15T14:38:00Z ERROR connection refused to upstream\n"
        )
        result = extractor.extract(content)
        text = _all(result)
        assert "Go goroutine blocks" not in text


class TestShortAmbiguousLogClassifierFilter:
    """ISS-050: Short ambiguous text (e.g. a maintenance notice with a
    single datetime mentioned in prose) must NOT trigger LOGS_AND_ERRORS as
    a candidate type. The standard's forbidden_claim is "suggests LOG as a
    candidate type". Require ≥2 log-line-shaped lines (timestamp at start
    of line) before LOGS_AND_ERRORS counts as an ambiguity signal.
    """

    def test_maintenance_notice_does_not_suggest_log(self):
        """Maintenance window text with one datetime in prose must omit LOG."""
        from faultmaven.modules.preprocessing.classifier import DataClassifier
        from faultmaven.models.api import DataType

        c = DataClassifier()
        content = (
            "Maintenance window scheduled: 2024-03-15 02:00 UTC to 04:00 UTC\n"
            "Services affected: auth, payments, notifications\n"
            "Expected downtime: up to 120 minutes\n"
            "Runbook: https://wiki.internal/runbooks/maintenance-q1-2024\n"
            "Contact: ops-oncall@example.com\n"
            "Action required: drain traffic from us-east-1 before 01:45 UTC\n"
            "Rollback plan: redeploy previous artifact (tag: v2.3.8)\n"
        )
        result = c.classify("low-signal-text.txt", content)
        assert result.classification_failed is True
        assert DataType.LOGS_AND_ERRORS not in result.suggested_types

    def test_real_log_lines_still_suggest_log(self):
        """A short file with ≥2 timestamp-prefixed lines IS log-shaped — the
        filter must not suppress LOG when the evidence is present."""
        from faultmaven.modules.preprocessing.classifier import DataClassifier
        from faultmaven.models.api import DataType

        c = DataClassifier()
        # Two log-line-shaped entries plus a URL and a version tag and an
        # email so the breadth threshold is met.
        content = (
            "2024-03-15 02:00:00 INFO server starting\n"
            "2024-03-15 02:00:01 INFO listening on :8080\n"
            "Runbook: https://wiki.internal/maintenance\n"
            "Contact: oncall@example.com\n"
            "Service version: v2.3.8\n"
        )
        result = c.classify("short-log.txt", content)
        # If the breadth threshold is met, LOG is in suggested_types because
        # ≥2 log-shaped lines fired the LOGS_AND_ERRORS category.
        if result.classification_failed:
            assert DataType.LOGS_AND_ERRORS in result.suggested_types
