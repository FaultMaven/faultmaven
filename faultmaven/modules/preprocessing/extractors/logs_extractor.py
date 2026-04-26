"""
Crime Scene Extraction for LOGS_AND_ERRORS data type

Implements severity-based error detection with adaptive context extraction.
No LLM calls required - pure keyword-based extraction.
"""

import re
from collections import Counter
from typing import TYPE_CHECKING

from faultmaven.modules.preprocessing.extractors.protocol import ExtractResult
from faultmaven.modules.preprocessing.extractors.utils import (
    EMPTY_CONTENT_RESPONSE,
    extract_time_range,
    has_content,
)

if TYPE_CHECKING:
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore

# ---------------------------------------------------------------------------
# Log-template normalisation — strips per-line variable parts so that
# "mod_jk child workerEnv in error state 6 1" and
# "mod_jk child workerEnv in error state 6 2" collapse to a single template.
#
# Patterns applied in order, then trailing whitespace stripped:
#   1. Apache CLF timestamp  [Mon Jan  2 15:04:05 2006]
#   2. ISO-8601 / RFC-3339   2006-01-02T15:04:05Z or 2006-01-02 15:04:05
#   3. Syslog preamble       Jan  2 15:04:05 hostname
#   4. Hex literals          0xDEADBEEF → <addr>
#   5. Pure-digit brackets   [1234] (PID) — letters preserved ([error] safe)
#   6. Trailing numeric run  " 42" " 1 2" at end-of-line (process ordinals)
# ---------------------------------------------------------------------------
_TPL_TS_APACHE = re.compile(r"\[\w{3} +\w{3} +\d{1,2} +\d{2}:\d{2}:\d{2} +\d{4}\]\s*")
_TPL_TS_ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\s*"
)
_TPL_TS_SYSLOG = re.compile(r"^\w{3} +\d{1,2} +\d{2}:\d{2}:\d{2} +\S+ +")
_TPL_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_TPL_PID_BRACKET = re.compile(r"\[\d+\]")
# Normalizes ALL standalone numbers (including trailing) — used only for
# non-error prominence detection where process IDs and slot numbers should
# collapse to the same template.
_TPL_ANY_NUMBER = re.compile(r"\b\d+\b")


def _normalize_template(line: str) -> str:
    """Return the message-template of a log line with variable parts removed.

    Strips timestamps, hex literals, and pure-digit brackets (PIDs), then
    trims whitespace. Trailing numeric tokens are intentionally left intact:
    they are often semantic (e.g. "error state 6" vs "error state 7") rather
    than variable process ordinals, so collapsing them would merge distinct
    templates.
    """
    s = line.strip()
    s = _TPL_TS_APACHE.sub("", s)
    s = _TPL_TS_ISO.sub("", s)
    s = _TPL_TS_SYSLOG.sub("", s)
    s = _TPL_HEX.sub("<addr>", s)
    s = _TPL_PID_BRACKET.sub("", s)
    return s.strip()


class LogsAndErrorsExtractor:
    """Crime Scene Extraction for logs and error reports (0 LLM calls)"""

    # Severity weights for error prioritization
    SEVERITY_WEIGHTS = {
        "FATAL": 100,
        "CRITICAL": 90,
        "panic": 90,  # Go panic
        "ERROR": 50,
        "WARN": 10,
        "WARNING": 10,
    }

    # Configuration constants
    MAX_SNIPPET_LINES = 500  # Safety limit
    SINGLE_ERROR_CONTEXT_LINES = 200  # ±200 lines around single error
    MULTIPLE_CRIMES_CONTEXT_LINES = 100  # ±100 lines around first + last
    TAIL_EXTRACTION_LINES = 500  # Last 500 lines if no errors
    ERROR_BURST_WINDOW = 50  # Lines to check for clustering
    ERROR_BURST_THRESHOLD = 10  # Min errors to trigger burst mode

    @property
    def strategy_name(self) -> str:
        return "crime_scene"

    @property
    def llm_calls_used(self) -> int:
        return 0

    def extract(self, content: str) -> ExtractResult:
        """
        Crime Scene Extraction algorithm:
        1. Find all errors with severity tracking
        2. Prioritize highest-severity error
        3. Detect multiple crime scenes or error bursts
        4. Extract context with adaptive sizing
        5. Return structured ExtractResult with three distinct parts
        """
        content = content.lstrip("\ufeff")
        if len(content) > 50_000_000:
            return ExtractResult(
                file_extract="[File exceeds 50MB maximum size limit for extraction]"
            )

        if not has_content(content):
            return ExtractResult(file_extract=EMPTY_CONTENT_RESPONSE)

        lines = content.split("\n")
        total_lines = len(lines)

        # 1. Find all errors with severity
        errors = self._find_all_errors_with_severity(lines)

        if not errors:
            crime_scene = self._extract_tail(lines)
        else:
            primary_error = max(errors, key=lambda e: e["severity"])
            high_severity = [
                e for e in errors if e["severity"] >= self.SEVERITY_WEIGHTS["ERROR"]
            ]

            if len(high_severity) > 1:
                crime_scene = self._extract_multiple_crime_scenes(
                    lines, high_severity[0], high_severity[-1]
                )
            else:
                burst_window = self._detect_error_burst(
                    lines, primary_error["line_idx"]
                )
                if burst_window:
                    crime_scene = self._extract_burst_context(
                        lines, burst_window, primary_error
                    )
                else:
                    crime_scene = self._extract_single_error_context(
                        lines, primary_error
                    )

        # Entity profiling \u2014 returns (summary, profile_body) separately
        error_lines = {e["line_idx"] for e in errors}
        file_summary, profile_body = self._build_entity_profile(content, error_lines)

        # Template counts \u2192 search map
        # Pass all lines so the function can fall back to counting every line
        # when error-keyword coverage is too sparse (< MIN_TEMPLATE_COVERAGE_FRACTION).
        template_block = self._build_template_counts(errors, total_lines, lines) or ""

        # --- Assemble file_extract (default question answer) ---
        file_extract_parts = [p for p in [file_summary, crime_scene] if p]
        file_extract = "\n\n".join(file_extract_parts)

        # --- Assemble search_map (navigation for search_file) ---
        search_map_parts = [p for p in [profile_body, template_block] if p]
        search_map = "\n\n".join(search_map_parts) or None

        # --- Assemble file_meta (facts about the raw file) ---
        severity_counts = Counter(e["keyword"] for e in errors)
        time_range = extract_time_range(content)
        truncated = total_lines > self.MAX_SNIPPET_LINES
        file_meta: dict = {
            "lines": total_lines,
            "lines_extracted": min(total_lines, self.MAX_SNIPPET_LINES),
            "size_bytes": len(content.encode("utf-8", errors="replace")),
            "truncated": truncated,
            "errors": len(errors),
        }
        if severity_counts:
            file_meta["severity"] = dict(severity_counts)
        if time_range.get("Time range"):
            file_meta["time_range"] = time_range["Time range"]

        return ExtractResult(
            file_extract=file_extract,
            search_map=search_map,
            file_meta=file_meta,
        )

    def _find_all_errors_with_severity(self, lines: list[str]) -> list[dict]:
        """
        Scan all lines for error keywords and track severity

        Returns:
            List of dicts with {line_idx, line_text, severity, keyword}
        """
        errors = []

        # Sort by severity descending so the highest-severity match wins per line
        sorted_keywords = sorted(
            self.SEVERITY_WEIGHTS.items(), key=lambda x: x[1], reverse=True
        )

        for idx, line in enumerate(lines):
            # Check each severity keyword (highest severity first)
            for keyword, severity in sorted_keywords:
                # Case-insensitive match with word boundary
                pattern = rf"\b{re.escape(keyword)}\b"
                if re.search(pattern, line, re.IGNORECASE):
                    errors.append(
                        {
                            "line_idx": idx,
                            "line_text": line,
                            "severity": severity,
                            "keyword": keyword,
                        }
                    )
                    break  # Only count first match per line

        return errors

    def _detect_error_burst(
        self, lines: list[str], error_idx: int, window: int | None = None
    ) -> tuple[int, int] | None:
        """
        Detect error burst (multiple errors clustered together)

        Args:
            lines: All log lines
            error_idx: Index of primary error
            window: Window size (default: ERROR_BURST_WINDOW)

        Returns:
            (burst_start, burst_end) if burst detected, else None
        """
        if window is None:
            window = self.ERROR_BURST_WINDOW

        # Check ±window lines for error density
        start = max(0, error_idx - window)
        end = min(len(lines), error_idx + window)

        burst_errors = []
        for idx in range(start, end):
            line = lines[idx]
            # Check for any error keyword
            for keyword in self.SEVERITY_WEIGHTS.keys():
                pattern = rf"\b{re.escape(keyword)}\b"
                if re.search(pattern, line, re.IGNORECASE):
                    burst_errors.append(idx)
                    break

        # If >threshold errors in window, it's a burst
        if len(burst_errors) >= self.ERROR_BURST_THRESHOLD:
            return (min(burst_errors), max(burst_errors))

        return None

    def _extract_single_error_context(self, lines: list[str], error: dict) -> str:
        """
        Extract ±200 lines around a single error

        Args:
            lines: All log lines
            error: Error dict with line_idx

        Returns:
            Formatted snippet with context
        """
        error_idx = error["line_idx"]
        context = self.SINGLE_ERROR_CONTEXT_LINES

        start = max(0, error_idx - context)
        end = min(len(lines), error_idx + context + 1)

        snippet = lines[start:end]

        # Safety check
        snippet = self._truncate_if_needed(snippet, error_idx - start)

        return self._format_snippet(
            snippet, f"Single {error['keyword']} at line {error_idx + 1}"
        )

    def _extract_multiple_crime_scenes(
        self, lines: list[str], first_error: dict, last_error: dict
    ) -> str:
        """
        Extract first + last crime scenes
        Captures error onset + current state

        Args:
            lines: All log lines
            first_error: First high-severity error
            last_error: Last high-severity error

        Returns:
            Combined snippet with both scenes
        """
        context = self.MULTIPLE_CRIMES_CONTEXT_LINES

        # Extract around first error (onset)
        first_start = max(0, first_error["line_idx"] - context)
        first_end = min(len(lines), first_error["line_idx"] + context + 1)
        first_snippet = lines[first_start:first_end]

        # Extract around last error (current state)
        last_start = max(0, last_error["line_idx"] - context)
        last_end = min(len(lines), last_error["line_idx"] + context + 1)
        last_snippet = lines[last_start:last_end]

        # Combine snippets with separator
        combined = (
            first_snippet
            + ["\n... [Multiple errors occurred between crime scenes] ...\n"]
            + last_snippet
        )

        # Safety check
        combined = self._truncate_if_needed(combined, len(first_snippet))

        error_count = last_error["line_idx"] - first_error["line_idx"]
        return self._format_snippet(
            combined,
            f"Multiple crime scenes: First {first_error['keyword']} at line {first_error['line_idx'] + 1}, "
            f"Last {last_error['keyword']} at line {last_error['line_idx'] + 1} ({error_count} lines apart)",
        )

    def _extract_burst_context(
        self, lines: list[str], burst_window: tuple[int, int], primary_error: dict
    ) -> str:
        """
        Extract error burst with expanded window

        Args:
            lines: All log lines
            burst_window: (start_idx, end_idx) of burst
            primary_error: Primary error dict

        Returns:
            Formatted snippet covering full burst
        """
        burst_start, burst_end = burst_window

        # Add padding around burst
        padding = 50
        start = max(0, burst_start - padding)
        end = min(len(lines), burst_end + padding + 1)

        snippet = lines[start:end]

        # Safety check
        snippet = self._truncate_if_needed(snippet, primary_error["line_idx"] - start)

        burst_size = burst_end - burst_start + 1
        return self._format_snippet(
            snippet,
            f"Error burst detected: {burst_size} lines with {primary_error['keyword']} storm",
        )

    def _extract_tail(self, lines: list[str]) -> str:
        """
        Fallback: Extract last N lines if no errors found

        Args:
            lines: All log lines

        Returns:
            Formatted tail snippet
        """
        tail_lines = self.TAIL_EXTRACTION_LINES
        start = max(0, len(lines) - tail_lines)
        snippet = lines[start:]

        return self._format_snippet(
            snippet, f"No errors detected - showing last {len(snippet)} lines"
        )

    def _truncate_if_needed(self, snippet: list[str], error_offset: int) -> list[str]:
        """
        Safety check: Truncate snippet if exceeds MAX_SNIPPET_LINES

        Strategy: Keep lines around error, truncate from middle

        Args:
            snippet: Lines to check
            error_offset: Offset of error within snippet

        Returns:
            Potentially truncated snippet
        """
        if len(snippet) <= self.MAX_SNIPPET_LINES:
            return snippet

        # Truncate from middle, keeping start and end around error
        keep_before = 200
        keep_after = 200

        return (
            snippet[:keep_before]
            + [
                f"\n... [Truncated {len(snippet) - keep_before - keep_after} lines for size] ...\n"
            ]
            + snippet[-keep_after:]
        )

    def _format_snippet(self, lines: list[str], header: str) -> str:
        """
        Format extracted lines with header

        Args:
            lines: Lines to format
            header: Description header

        Returns:
            Formatted string ready for LLM
        """
        formatted = [
            "=" * 60,
            f"CRIME SCENE EXTRACTION: {header}",
            "=" * 60,
            "",
            *lines,
            "",
            "=" * 60,
        ]

        return "\n".join(formatted)

    def _build_template_counts(
        self,
        errors: list[dict],
        total_lines: int = 0,
        all_lines: list[str] | None = None,
    ) -> str:
        """Normalise log lines to message templates and count occurrences.

        Primary mode: count only severity-keyword-matched lines (the `errors`
        list). When those lines represent fewer than MIN_TEMPLATE_COVERAGE_FRACTION
        of the file (e.g. SSH auth logs where attack lines carry no ERROR keyword),
        fall back to counting ALL lines so that notice/info-level events are not
        invisible.

        Pass total_lines=0 to skip the coverage gate (e.g. in unit tests).
        """
        if not errors and not all_lines:
            return ""

        use_all_lines = False
        if total_lines > 0 and errors:
            coverage = len(errors) / total_lines
            if coverage < self.MIN_TEMPLATE_COVERAGE_FRACTION:
                use_all_lines = True
        elif not errors:
            use_all_lines = True

        if use_all_lines:
            if not all_lines:
                return ""
            non_blank = [ln for ln in all_lines if ln.strip()]
            template_counts: Counter[str] = Counter(
                _normalize_template(ln) for ln in non_blank
            )
            if not template_counts:
                return ""
            total = len(non_blank)
            distinct = len(template_counts)
            scope = f"all lines: {total} of {total_lines or total}"
        else:
            template_counts = Counter(
                _normalize_template(e["line_text"]) for e in errors
            )
            if not template_counts:
                return ""
            total = len(errors)
            distinct = len(template_counts)
            if total_lines > 0:
                scope = (
                    f"error-keyword lines: {total} of {total_lines},"
                    f" {total / total_lines:.0%} coverage"
                )
            else:
                scope = f"{total} lines matched severity keywords"

            # Augment with prominent non-error templates so that high-frequency
            # info/notice events (e.g. Apache mod_jk init messages) are visible
            # even when error-keyword lines dominate by keyword count.
            # Uses aggressive number normalization so that lines differing only
            # in variable process IDs or slot numbers collapse to one template.
            if all_lines and total_lines > 0:
                error_line_set = {e["line_text"] for e in errors}
                non_error_non_blank = [
                    ln for ln in all_lines if ln.strip() and ln not in error_line_set
                ]
                if non_error_non_blank:
                    non_error_counts: Counter[str] = Counter(
                        _TPL_ANY_NUMBER.sub("N", _normalize_template(ln))
                        for ln in non_error_non_blank
                    )
                    prominent = [
                        (tpl, cnt)
                        for tpl, cnt in non_error_counts.most_common(10)
                        if cnt >= self.MIN_PROMINENT_NON_ERROR_COUNT
                    ]
                    if prominent:
                        for tpl, cnt in prominent:
                            template_counts[tpl] += cnt
                        scope += f" + {len(prominent)} prominent non-error"

            distinct = len(template_counts)

        header = (
            f"EVENT TEMPLATE COUNTS"
            f" ({scope},"
            f" {distinct} distinct template{'s' if distinct != 1 else ''}):"
        )
        output_lines = [header]
        for template, count in template_counts.most_common():
            truncated = template[:120] + "..." if len(template) > 120 else template
            output_lines.append(f"  [{count:>4}x] {truncated}")
        return "\n".join(output_lines)

    # Regex patterns for entity profiling (compiled once)
    _IPV4_RE = re.compile(
        r"\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b"
    )
    # IPv6 including compressed forms (::1, fe80::1, 2001:db8::ff00:42:8329).
    # Negative look-around rejects runs of hex-and-colons that are really
    # timestamps or MAC addresses. An 8-group full form is the widest match;
    # each of the following branches covers a different `::` position.
    _IPV6_RE = re.compile(
        r"(?<![0-9A-Fa-f:.])"
        r"(?:"
        r"(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,7}:"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,6}:[0-9A-Fa-f]{1,4}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,5}(?::[0-9A-Fa-f]{1,4}){1,2}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,4}(?::[0-9A-Fa-f]{1,4}){1,3}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,3}(?::[0-9A-Fa-f]{1,4}){1,4}"
        r"|(?:[0-9A-Fa-f]{1,4}:){1,2}(?::[0-9A-Fa-f]{1,4}){1,5}"
        r"|[0-9A-Fa-f]{1,4}:(?::[0-9A-Fa-f]{1,4}){1,6}"
        r"|::(?:[0-9A-Fa-f]{1,4}:){0,6}[0-9A-Fa-f]{1,4}"
        r")"
        r"(?![0-9A-Fa-f:.])"
    )
    _USER_RE = re.compile(
        r"(?:\buser[= ]+|\bfor (?:invalid user )?\b|\buser=)([a-zA-Z_][a-zA-Z0-9._\-]{0,31})\b",
        re.IGNORECASE,
    )
    # Port matchers. A port number is a numeric token that needs *structural*
    # context on the left: either an explicit `port` keyword, or a
    # host-or-address token before the colon. A bare `:\d+` would match every
    # `MM:SS` fragment in a timestamp.
    #
    # The general rule for `<lhs>:<port>` is that the left-hand side contains
    # at least one non-digit character (a hostname letter, or an IPv4's dot).
    # Pure-digit LHS (timestamp fragments like `04:47`) is rejected.
    _PORT_KEYWORD_RE = re.compile(r"\bport[= :]+(\d{1,5})\b", re.IGNORECASE)
    _HOST_PORT_RE = re.compile(
        # LHS must contain at least one non-digit char (letter or dot).
        # `(?<![\w.-])` pins the start; `[\w.-]*[A-Za-z.]` requires a
        # non-digit somewhere in the LHS.
        r"(?<![\w.-])[\w-]*[A-Za-z.][\w.-]*:(\d{1,5})\b"
    )
    # PID matchers. Same principle: a PID needs structural context.
    # `[\d+]` alone (with a closing bracket) is the classic syslog form;
    # `pid=N` is the explicit keyword form. We do NOT accept `[\d+` without
    # the closing bracket, which would otherwise match `[19:02:15]` and
    # capture `19` as a PID.
    #
    # Digit width: Linux ``kernel.pid_max`` defaults to 32768 on desktops
    # but is routinely raised to 4_194_304 (7 digits) on servers, and
    # containerised workloads can cycle through them quickly. Capping at
    # 5 digits silently dropped any PID >= 100_000 — including every PID
    # on tuned hosts. Allow up to 7 digits; the numeric range check below
    # filters out impossibly large matches.
    _PID_KEYWORD_RE = re.compile(r"\bpid[= ]+(\d{1,7})\b", re.IGNORECASE)
    _PID_BRACKET_RE = re.compile(r"\[(\d{1,7})\]")
    # Absolute ceiling matching the current kernel maximum. Anything above
    # this is almost certainly not a PID (timestamp ns, request byte count,
    # etc.) and is filtered at count time.
    _PID_MAX = 4_194_304
    _HTTP_PATH_RE = re.compile(r"\b(?:GET|POST|PUT|DELETE|PATCH)\s+(/[^\s\?]*)\b")

    # SSH event type patterns for semantic counting
    _FAILED_PASSWORD_RE = re.compile(r"Failed password", re.IGNORECASE)
    _ACCEPTED_PASSWORD_RE = re.compile(
        r"Accepted (?:password|publickey)", re.IGNORECASE
    )
    _INVALID_USER_RE = re.compile(r"invalid user", re.IGNORECASE)
    _CONNECTION_CLOSED_RE = re.compile(
        r"Connection closed|Connection reset", re.IGNORECASE
    )
    _BREAK_IN_ATTEMPT_RE = re.compile(r"POSSIBLE BREAK-IN ATTEMPT", re.IGNORECASE)
    # PAM authentication failure lines accompany sshd "Failed password" events —
    # they are separate log lines for the same auth event (two lines per failure).
    # Counting them separately gives a more complete per-IP auth failure tally.
    _PAM_AUTH_FAILURE_RE = re.compile(
        r"pam_unix\([^)]*\):\s*authentication failure", re.IGNORECASE
    )

    # Reverse-DNS hostname pattern — syslog's rhost field stores the PTR record
    # of the connecting IP (e.g. customer-187-141-143-180-sta.) which the entity
    # extractor would otherwise count as a login username. Three or more numeric
    # segments separated by hyphens or dots identify this pattern reliably.
    _REVERSE_DNS_RE = re.compile(r"\d{1,3}(?:[.-]\d{1,3}){2,}")

    # Minimum fraction of total lines that must match severity keywords for
    # the template counts block to be shown. Below this threshold the block
    # is suppressed — it covers too small a portion of the file to be
    # representative (e.g. SSH brute-force logs where attack lines contain
    # no ERROR/WARN/FATAL keywords).
    MIN_TEMPLATE_COVERAGE_FRACTION = 0.15
    # Minimum occurrences for a non-error-keyword line template to be considered
    # "prominent" and included alongside error-only template counts.
    MIN_PROMINENT_NON_ERROR_COUNT = 50

    # SSH/TLS protocol terms that _USER_RE's broad "for <word>" branch would
    # otherwise capture as usernames. These are structural keywords in auth
    # log messages, never actual account names.
    _USER_PROTOCOL_TERMS: frozenset = frozenset(
        {
            "authentication",
            "publickey",
            "preauth",
            "key",
            "address",
            "the",
            "a",
            "an",
            # PAM/sshd structural words captured by the "user <word>" pattern
            # that are log-message tokens, never actual account names.
            "unknown",  # "check pass; user unknown" — PAM status, not username
            "invalid",  # "invalid user admin" — adjective, not username
            "user",  # "user=root" field name
            "none",
            "null",
            "request",  # "input_userauth_request" function name fragment
            "sshd",  # process name captured via "for sshd" in some PAM messages
        }
    )

    # Exact search strings for each semantic event type — surfaced in the
    # entity profile so the agent knows what to pass to search_file.
    _EVENT_SEARCH_STRINGS: dict = {
        "failed_password": "Failed password",
        "pam_auth_failure": "authentication failure; logname=",
        "accepted_login": "Accepted password",
        "invalid_user": "Invalid user",
        "connection_closed": "Connection closed",
        "break_in_attempt": "POSSIBLE BREAK-IN ATTEMPT",
    }

    def _build_entity_profile(
        self, content: str, error_lines: set[int] = None, top_n: int = 20
    ) -> str:
        """Scan the full file for key entities and produce a frequency summary.

        All IP and username counts reflect the complete file — not just
        severity-keyword lines. Entities are ranked by total mentions so the
        most active actor always appears first regardless of which log level
        its lines carry.

        An optional annotation "(N on error lines)" is added when an entity
        appears on severity-keyword lines, but this does NOT affect ordering.
        """
        error_lines = error_lines or set()
        ip_all_counts: Counter = Counter()
        ip_error_counts: Counter = Counter()
        user_all_counts: Counter = Counter()
        user_error_counts: Counter = Counter()
        event_counts: Counter = Counter()
        port_counts: Counter = Counter()
        pid_counts: Counter = Counter()
        path_counts: Counter = Counter()

        lines = content.split("\n")
        for i, line in enumerate(lines):
            is_error = i in error_lines
            for ip in self._IPV4_RE.findall(line) + self._IPV6_RE.findall(line):
                ip_all_counts[ip] += 1
                if is_error:
                    ip_error_counts[ip] += 1
            for user in self._USER_RE.findall(line):
                # Skip SSH/TLS protocol keywords and reverse-DNS hostnames.
                # Syslog stores the PTR record of the connecting IP in the
                # rhost field (e.g. customer-187-141-143-180-sta.), which the
                # "for <word>" branch of _USER_RE would otherwise count as a
                # login username. _REVERSE_DNS_RE detects embedded IP octets.
                if (
                    user
                    and user.lower() not in self._USER_PROTOCOL_TERMS
                    and not self._REVERSE_DNS_RE.search(user)
                    and not user[
                        0
                    ].isdigit()  # filter pure-numeric tokens like "0", "1234"
                    and user[
                        -1
                    ].isalnum()  # filter tokens with trailing punctuation like "request."
                ):
                    user_all_counts[user] += 1
                    if is_error:
                        user_error_counts[user] += 1
            for port_str in self._PORT_KEYWORD_RE.findall(
                line
            ) + self._HOST_PORT_RE.findall(line):
                if port_str.isdigit() and 0 < int(port_str) <= 65535:
                    port_counts[port_str] += 1
            for pid_str in self._PID_KEYWORD_RE.findall(
                line
            ) + self._PID_BRACKET_RE.findall(line):
                if pid_str.isdigit() and 0 < int(pid_str) <= self._PID_MAX:
                    pid_counts[pid_str] += 1
            for path in self._HTTP_PATH_RE.findall(line):
                path_counts[path] += 1

            # Semantic event classification
            if self._FAILED_PASSWORD_RE.search(line):
                event_counts["failed_password"] += 1
            if self._ACCEPTED_PASSWORD_RE.search(line):
                event_counts["accepted_login"] += 1
            if self._INVALID_USER_RE.search(line):
                event_counts["invalid_user"] += 1
            if self._CONNECTION_CLOSED_RE.search(line):
                event_counts["connection_closed"] += 1
            if self._BREAK_IN_ATTEMPT_RE.search(line):
                event_counts["break_in_attempt"] += 1
            if self._PAM_AUTH_FAILURE_RE.search(line):
                event_counts["pam_auth_failure"] += 1

        if not (
            ip_all_counts
            or user_all_counts
            or event_counts
            or port_counts
            or pid_counts
            or path_counts
        ):
            return "", "ENTITY PROFILE: No entities found"

        # FILE SUMMARY — returned separately so extract() can place it in file_extract
        summary = self._build_summary(
            event_counts,
            ip_all_counts,
            user_all_counts,
            path_counts,
            len(lines),
            len(error_lines),
        )

        # ENTITY PROFILE body — the search map
        parts: list[str] = ["ENTITY PROFILE (full file scan):"]

        if event_counts:
            parts.append(
                "  Event types"
                "  [search: use the exact string shown with search_file]:"
            )
            for event, count in event_counts.most_common():
                search_str = self._EVENT_SEARCH_STRINGS.get(event, event)
                parts.append(f'    {event}: {count}  ["{search_str}"]')

        if ip_all_counts:
            parts.append(
                "  Distinct IPs"
                "  [search: pass the IP string to search_file;"
                " mention count = all line occurrences across all event types,"
                " not auth-specific — use search_file for event-specific counts]:"
            )
            for ip, total in ip_all_counts.most_common(top_n):
                error_n = ip_error_counts.get(ip, 0)
                annotation = f"  ({error_n} on error lines)" if error_n else ""
                parts.append(f"    {ip}: {total} line occurrences{annotation}")

        if user_all_counts:
            total_distinct = len(user_all_counts)
            more_note = (
                f" — INCOMPLETE: {total_distinct - min(top_n, total_distinct)} more"
                f" not shown; call search_file('Invalid user') for the full list"
                if total_distinct > top_n
                else ""
            )
            parts.append(
                f"  Distinct usernames (top {min(top_n, total_distinct)} of {total_distinct}{more_note}):"
            )
            for user, total in user_all_counts.most_common(top_n):
                error_n = user_error_counts.get(user, 0)
                annotation = f"  ({error_n} on error lines)" if error_n else ""
                parts.append(f"    {user}: {total} mentions{annotation}")

        if port_counts:
            parts.append(f"  Distinct Ports: {len(port_counts)}")
            for port, count in port_counts.most_common(top_n):
                parts.append(f"    {port}: {count} mentions")

        if pid_counts:
            parts.append(f"  Distinct PIDs: {len(pid_counts)}")
            for pid, count in pid_counts.most_common(top_n):
                parts.append(f"    {pid}: {count} mentions")

        if path_counts:
            parts.append(f"  HTTP Paths: {len(path_counts)}")
            for path, count in path_counts.most_common(top_n):
                parts.append(f"    {path}: {count} requests")

        return summary, "\n".join(parts)

    @staticmethod
    def _detect_log_pattern(
        event_counts: "Counter[str]",
        path_counts: "Counter[str]",
        error_count: int,
        total_lines: int,
    ) -> str:
        """Return a one-phrase pattern interpretation for the FILE SUMMARY, or ''."""
        failed = event_counts.get("failed_password", 0)
        accepted = event_counts.get("accepted_login", 0)
        invalid = event_counts.get("invalid_user", 0)

        # SSH brute-force: failures dominate with few or no successes
        if failed > 0 and (accepted == 0 or failed >= 3 * accepted):
            return "SSH credential-stuffing/brute-force pattern"

        # General SSH authentication activity
        if failed > 0 or invalid > 0 or accepted > 0:
            return "SSH authentication activity"

        # HTTP: paths detected
        if path_counts:
            if error_count > 0 and error_count / max(total_lines, 1) > 0.05:
                return "HTTP error pattern"
            return "HTTP access log"

        return ""

    def _build_summary(
        self,
        event_counts: "Counter[str]",
        ip_counts: "Counter[str]",
        user_counts: "Counter[str]",
        path_counts: "Counter[str]",
        total_lines: int,
        error_count: int,
    ) -> str:
        """Return a compact FILE SUMMARY (2–4 sentences) describing dominant
        activity, top source, and key absences.

        Prepended to the entity profile so it survives context truncation and
        gives the agent an immediate orientation without requiring it to parse
        the full entity table.
        """
        sentences = []

        # Interpretation-first: name the pattern before listing counts
        pattern = self._detect_log_pattern(
            event_counts, path_counts, error_count, total_lines
        )
        if pattern:
            sentences.append(f"{pattern}.")

        # Dominant activity counts
        top_events = event_counts.most_common(3)
        if top_events:
            ev_summary = ", ".join(
                f"{name.replace('_', ' ')} ({count})" for name, count in top_events
            )
            sentences.append(f"Dominant activity: {ev_summary}.")
        elif error_count > 0:
            sentences.append(
                f"{error_count} severity-flagged lines out of {total_lines} total."
            )

        # Top source IP
        if ip_counts:
            top_ip, top_count = ip_counts.most_common(1)[0]
            sentences.append(f"Top source: {top_ip} ({top_count} mentions).")

        # Key absences the agent can confidently answer without searching
        absent = []
        if not path_counts:
            absent.append("no HTTP traffic")
        if absent:
            sentences.append(f"Absent: {', '.join(absent)}.")

        if not sentences:
            return ""
        return "FILE SUMMARY: " + " ".join(sentences)
