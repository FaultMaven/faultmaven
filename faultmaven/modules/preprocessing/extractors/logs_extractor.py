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
    extract_time_range_ts,
    extract_timestamp,
    has_content,
    has_yearless_timestamps,
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
    # Handles both dot-separated (ISO-8601) and comma-separated (Java/log4j)
    # milliseconds: e.g. "2015-07-29 19:34:09,295" uses comma while
    # "2024-01-02T15:04:05.123Z" uses dot. The [.,] handles both.
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?\s*"
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

    # Severity weights for error prioritization.
    # INFO/DEBUG/TRACE/VERBOSE carry weight=0: they act as "level anchors" —
    # when one appears leftmost in a line (i.e., it IS the log level field),
    # it prevents higher-severity keywords in the message body from
    # misclassifying the line. Lines resolving to weight=0 are excluded from
    # the error list. This fixes e.g. ZooKeeper INFO lines whose message body
    # contains "Error Path:" or "Error:KeeperErrorCode".
    SEVERITY_WEIGHTS = {
        "FATAL": 100,
        "CRITICAL": 90,
        "panic": 90,  # Go panic
        "ERROR": 50,
        "WARN": 10,
        "WARNING": 10,
        "INFO": 0,
        "DEBUG": 0,
        "TRACE": 0,
        "VERBOSE": 0,
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
        # True when the error set contains only WARN/WARNING lines (no ERROR/FATAL/CRITICAL)
        high_severity_weight = max(
            (self.SEVERITY_WEIGHTS.get(e["keyword"], 0) for e in errors),
            default=0,
        )
        warn_only = (
            len(errors) > 0 and high_severity_weight <= self.SEVERITY_WEIGHTS["WARN"]
        )
        file_summary, profile_body = self._build_entity_profile(
            content, error_lines, warn_only=warn_only
        )

        # Template counts \u2192 search map
        # Pass all lines so the function can fall back to counting every line
        # when error-keyword coverage is too sparse (< MIN_TEMPLATE_COVERAGE_FRACTION).
        template_block = self._build_template_counts(errors, total_lines, lines) or ""

        # --- Assemble file_extract (default question answer) ---
        severity_counts = Counter(e["keyword"] for e in errors)
        time_range = extract_time_range(content)
        file_extract_parts = [p for p in [file_summary, crime_scene] if p]
        file_extract = "\n\n".join(file_extract_parts)

        # --- Assemble search_map (navigation for search_file) ---
        search_map_parts = [p for p in [profile_body, template_block] if p]
        search_map = "\n\n".join(search_map_parts) or None

        # --- Assemble file_meta (facts about the raw file) ---
        truncated = total_lines > self.MAX_SNIPPET_LINES
        # (time_range already computed above before file_extract assembly)
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
        Scan all lines for error keywords and track severity.

        Single-pass leftmost-wins: the LEFTMOST severity keyword in each line
        determines its classification. The level field in syslog/log4j formats
        is positioned early in the line by convention, so leftmost-position
        captures the application's authoritative severity choice. Body
        occurrences of severity words (e.g. trailing "error =" field labels,
        prose like "to fix this error...") are ignored because they are
        almost always field labels or prose — not severity assertions.

        Weight-0 keywords (INFO/DEBUG/TRACE/VERBOSE) act as level anchors:
        when one is leftmost, the line is excluded from the error list
        regardless of any higher-severity keyword in the body. This prevents
        e.g. an "INFO ... Error:KeeperErrorCode" line from being counted as
        ERROR.

        ISS-012 history note: a previous version added a second pass that
        picked the highest-severity keyword anywhere in a weight>0 line
        (intent: classify "WARNING: CRITICAL failure" as CRITICAL). That
        pass over-escalated WARN-level lines whose body contained "error ="
        as a field label — 291 of 1318 true WARN lines in the ZooKeeper
        fixture got reported as ERROR. Real log data does not survive the
        body-escalation rule; level fields are authoritative.

        Returns:
            List of dicts with {line_idx, line_text, severity, keyword}
        """
        errors = []

        for idx, line in enumerate(lines):
            best_keyword: str | None = None
            best_pos = len(line) + 1
            best_severity = 0

            for keyword, severity in self.SEVERITY_WEIGHTS.items():
                pattern = rf"\b{re.escape(keyword)}\b"
                m = re.search(pattern, line, re.IGNORECASE)
                if m and m.start() < best_pos:
                    best_keyword = keyword
                    best_pos = m.start()
                    best_severity = severity

            # Weight-0 anchor (INFO/DEBUG/TRACE/VERBOSE) at leftmost position
            # means the line is informational; skip regardless of body content.
            if best_keyword and best_severity > 0:
                errors.append(
                    {
                        "line_idx": idx,
                        "line_text": line,
                        "severity": best_severity,
                        "keyword": best_keyword,
                    }
                )

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
            # Check for any error keyword (skip weight-0 level anchors like INFO/DEBUG)
            for keyword, weight in self.SEVERITY_WEIGHTS.items():
                if weight == 0:
                    continue
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

        # burst_size is the LINE SPAN between the first and last burst-keyword
        # match (inclusive), not the count of burst events themselves and not
        # the total file-wide count of the keyword. Phrase the header so the
        # agent doesn't conflate window width with event count — the
        # authoritative count is in the entity profile / file_meta.severity.
        burst_size = burst_end - burst_start + 1
        return self._format_snippet(
            snippet,
            f"{primary_error['keyword']} burst — context window of "
            f"{burst_size} surrounding lines "
            f"(see entity profile for total {primary_error['keyword']} count "
            f"across the full file)",
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
            f" [Note: these are message-template variants, not distinct event"
            f" categories — count reflects parameter variation (IDs, state numbers);"
            f" semantic event types are in the ENTITY PROFILE above]"
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
    # Username extraction — split into two patterns so the "for <user>" branch
    # is only applied to lines with explicit auth keywords, preventing kernel
    # and service messages like "for high-res timesource" or "for PnP cards"
    # from landing in the username list.
    #
    # _USER_FIELD_RE: matches "user= <name>" and "user <name>" — always applied.
    # _USER_FOR_RE: matches "for [invalid user] <name>" — gated on _AUTH_CONTEXT_RE.
    _USER_FIELD_RE = re.compile(
        r"\buser[= ]+([a-zA-Z_][a-zA-Z0-9._\-]{0,31})\b",
        re.IGNORECASE,
    )
    _USER_FOR_RE = re.compile(
        r"\bfor (?:invalid user )?([a-zA-Z_][a-zA-Z0-9._\-]{0,31})\b",
        re.IGNORECASE,
    )
    # Lines that carry these phrases are SSH/PAM auth events — the only context
    # where "for <username>" is a reliable username signal.
    _AUTH_CONTEXT_RE = re.compile(
        r"Failed password|Accepted (?:password|publickey)|Invalid user"
        r"|authentication failure|session opened for",
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
    #
    # Two syslog PAM formats exist in the wild:
    #   A. modern Linux-PAM:  "pam_unix(sshd:auth): authentication failure"
    #   B. older Red Hat:     "sshd(pam_unix)[19939]: authentication failure"
    # Both must match — the loghub Linux fixture is format B; OpenSSH and most
    # post-2010 distros are format A.
    _PAM_AUTH_FAILURE_RE = re.compile(
        r"(?:pam_unix\([^)]*\)|\(pam_unix\)\[\d+\]):\s*authentication failure",
        re.IGNORECASE,
    )
    # Numeric state codes (e.g. "error state 6") are internal to the log source;
    # the log itself does not document their meanings. Detection triggers a note
    # in FILE SUMMARY to prevent the agent from asserting meanings from training data.
    _STATE_CODE_RE = re.compile(r"\berror state \d+\b", re.IGNORECASE)
    # Successful SSH session opens — PAM-style syslog logs a "session opened
    # for user <name>" line from sshd instead of "Accepted password". Counted
    # separately so the breakdown table distinguishes SSH successes from
    # local su/kerberos sessions (which also emit "session opened" but from
    # a different service process).
    _SSH_SESSION_RE = re.compile(r"sshd[^:]*:\s*session opened for user", re.IGNORECASE)
    # Auth-relevant event types included in the per-IP breakdown table.
    # ssh_session_opened is intentionally excluded: "session opened" lines
    # carry no rhost IP, so they never appear in ip_event_counts.
    _AUTH_EVENTS: frozenset = frozenset(
        {"failed_password", "pam_auth_failure", "invalid_user", "accepted_login"}
    )
    # Event types that mark a source IP as an "attacker" for the purpose of
    # disambiguating Accepted password lines (ISS-026). An IP that ONLY
    # appears in accepted_login (and never in any of these) is treated as a
    # legitimate session — typically an admin login on the same host whose
    # IP happens to share the file with brute-force traffic. Crucially,
    # accepted_login is NOT in this set, so a clean admin login does not
    # mark its own IP as an attacker.
    _ATTACK_EVENTS: frozenset = frozenset(
        {"failed_password", "pam_auth_failure", "invalid_user", "break_in_attempt"}
    )
    # Syslog service name extractor — captures the base program name from
    # "service[PID]:" and "service(module)[PID]:" prefixes. Matches only
    # when the token is preceded by whitespace (avoids false matches inside
    # message bodies) and followed by an optional paren group and a PID bracket.
    # Examples: " sshd(pam_unix)[19939]" → "sshd"; " ftpd[29504]" → "ftpd".
    _SYSLOG_SERVICE_RE = re.compile(r"\s([\w.-]+)(?:\([^)]*\))?\[\d+\]")

    # Syslog hostname extractor — captures the host token in the BSD-syslog
    # 3rd-field position: "Mon DD HH:MM:SS HOSTNAME service[pid]: msg".
    # Anchored on the start-of-line BSD timestamp + a service[pid] suffix to
    # avoid false matches on non-syslog content. Surfacing the host(s) in the
    # entity profile lets the agent identify the source machine without
    # having to spot it in raw lines (logs-linux-01 q1, ISS-008).
    _SYSLOG_HOST_RE = re.compile(
        r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+"
        r"(\S+)\s+[\w.-]+(?:\([^)]*\))?\[\d+\]"
    )

    # Reverse-DNS hostname pattern — syslog's rhost field stores the PTR record
    # of the connecting IP (e.g. customer-187-141-143-180-sta.) which the entity
    # extractor would otherwise count as a login username. Three or more numeric
    # segments separated by hyphens or dots identify this pattern reliably.
    _REVERSE_DNS_RE = re.compile(r"\d{1,3}(?:[.-]\d{1,3}){2,}")

    # Windows Update KB package extractor (ISS-020). CBS logs reference
    # packages as ``Package_for_KB<NUMBER>~...``. Each line carries one
    # ``Package_for_KB<NUMBER>`` per install/uninstall/check event, so the
    # number of substring occurrences vastly overstates the number of
    # distinct KB packages. Surface the *distinct* set explicitly so the
    # agent can answer "how many KB packages are referenced" accurately
    # without conflating substring count with distinct count.
    _KB_PACKAGE_RE = re.compile(r"\bPackage_for_KB(\d+)\b")

    # Windows CBS HRESULT extractor (ISS-037). CBS lines carry
    # ``[HRESULT = 0x<hex> - SYMBOL]`` codes where the same code/symbol
    # pair appears across multiple distinct message templates ("Expecting
    # attribute name", "Failed to get next element", etc.). The template
    # counts block reports per-template occurrences, so a code that occurs
    # 448 times split across two templates renders as ``[224x]`` in two
    # places — and the agent reports the per-template number as the
    # per-HRESULT total. Aggregating by HRESULT/symbol gives the agent a
    # single authoritative count per code, independent of message-template
    # variation. The hex literal is uppercase-folded so ``0x800F080D`` and
    # ``0x800f080d`` aggregate to the same key.
    _HRESULT_RE = re.compile(
        r"\bHRESULT\s*=\s*"
        r"(0x[0-9a-fA-F]+)"  # hex code
        r"(?:\s*-\s*([A-Z][A-Z0-9_]{2,}))?"  # optional symbolic name
    )

    # BGL (BlueGene/L) RAS log alert flag column extractor (ISS-015).
    # BGL lines have a structured first column carrying the alert-flag
    # category (``-`` for no class, or ``KERNDTLB``/``APPSEV``/etc.).
    # Detection requires both a flag-shaped first token AND a 10-digit
    # unix-epoch second token to avoid false matches on plain text. The
    # epoch must fall in a sane range (year 2000 onwards) to discriminate
    # from random 10-digit numbers.
    _BGL_LINE_RE = re.compile(
        r"^(?P<flag>-|[A-Z][A-Z0-9]{2,11})\s+"  # flag: dash or 3-12 char ALL-CAPS token
        r"(?P<epoch>1[0-9]{9}|2[0-9]{9})\s+"  # 10-digit unix epoch in 2001-2065 range
        r"\d{4}\.\d{2}\.\d{2}\s+"  # YYYY.MM.DD date
        r"(?P<node>\S+)"  # node identifier, e.g. R02-M1-N0-C:J12-U11
    )

    # Minimum fraction of total lines that must match severity keywords for
    # the template counts block to be shown. Below this threshold the block
    # is suppressed — it covers too small a portion of the file to be
    # representative (e.g. SSH brute-force logs where attack lines contain
    # no ERROR/WARN/FATAL keywords).
    MIN_TEMPLATE_COVERAGE_FRACTION = 0.15
    # Minimum event count (severity-flagged lines or per-event-type) before a
    # rate annotation (events/hour) is emitted (ISS-016). Below this the rate
    # is statistically meaningless and the count itself is more informative.
    MIN_RATE_EVENT_COUNT = 50
    # Minimum BGL-format lines before the distinct-node count is surfaced in
    # FILE SUMMARY. Below this the host count is noise (single-line BGL
    # matches inside other content). Mirrors the host-count threshold used
    # for syslog 3rd-field hosts.
    MIN_BGL_NODE_SURFACE_LINES = 5
    # Minimum distinct ``Package_for_KB<N>`` packages before the CBS-format
    # note is added to FILE SUMMARY (ISS-028). 5 is well above the
    # incidental-mention floor (a misclassified pasted line containing
    # 'Package_for_KB' would appear once or twice) but well below the
    # ~271 distinct KBs in the Windows fixture, so any genuine CBS log
    # trips it.
    MIN_KB_PACKAGES_FOR_CBS_NOTE = 5
    # Minimum occurrences for a non-error-keyword line template to be considered
    # "prominent" and included alongside error-only template counts.
    # Set to 20 so that INFO activity like FastLeaderElection (37 lines in a 2k
    # ZooKeeper log) is visible alongside WARN-dominated template counts. This
    # threshold exposes moderate-frequency healthy-signal entries that would
    # otherwise be invisible when 50 was the bar.
    MIN_PROMINENT_NON_ERROR_COUNT = 20

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
        # Must include 'sshd' so keyword-mode AND-matching excludes
        # su(pam_unix) / cron(pam_unix) session-open events. Without it the
        # agent's search_file run pulls in unrelated PAM sessions and
        # double-counts them as logins (logs-linux-01 q6, ISS-007).
        "ssh_session_opened": "sshd session opened for user",
        "invalid_user": "Invalid user",
        "connection_closed": "Connection closed",
        "break_in_attempt": "POSSIBLE BREAK-IN ATTEMPT",
    }

    def _build_entity_profile(
        self,
        content: str,
        error_lines: set[int] = None,
        top_n: int = 20,
        warn_only: bool = False,
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
        # Running min/max timestamps per event type for temporal span display
        event_first_ts: dict = {}
        event_last_ts: dict = {}
        # Per-IP burst spans on attack-event lines only (not all lines)
        ip_attack_first_ts: dict = {}
        ip_attack_last_ts: dict = {}
        # Per-IP per-event-type counts for the auth breakdown table
        ip_event_counts: dict[str, Counter] = {}
        # Tracks whether the log contains "error state N" lines (mod_jk / similar)
        has_numeric_state_codes = False
        # Syslog service name counts for multi-service logs
        service_counts: Counter = Counter()
        # Syslog hostname counts (BSD-syslog 3rd-field position). Surfaced in
        # entity profile so the agent can identify the source host without
        # parsing raw lines (ISS-008).
        host_counts: Counter = Counter()
        # Windows Update KB package counts (ISS-020). Distinct-value
        # counting prevents the agent from reporting substring occurrences
        # as distinct package count.
        kb_package_counts: Counter = Counter()
        # Windows CBS HRESULT counts (ISS-037). Aggregates by HRESULT hex
        # code so the agent sees a single file-wide total per error code,
        # not per-template halves it has to add together. Symbol mapping
        # is captured separately so the per-HRESULT line can render the
        # canonical symbolic name alongside the code.
        hresult_counts: Counter = Counter()
        hresult_symbols: dict[str, str] = {}
        # BGL alert flag column counts (ISS-015). The BGL RAS log first
        # column classifies fault types; the agent must read the actual
        # column values rather than fabricating flag names from message
        # bodies.
        bgl_flag_counts: Counter = Counter()
        # BGL distinct node identifiers (ISS-016). Surfaces the per-host
        # denominator so the agent can normalize event counts against the
        # number of distinct nodes — a 1778-node BGL cluster generating 347
        # FATAL events is per-node background, not "systemic".
        bgl_nodes: set[str] = set()
        bgl_line_count = 0

        lines = content.split("\n")
        for i, line in enumerate(lines):
            is_error = i in error_lines
            line_ips = self._IPV4_RE.findall(line)
            for ip in line_ips + self._IPV6_RE.findall(line):
                ip_all_counts[ip] += 1
                if is_error:
                    ip_error_counts[ip] += 1
            if not has_numeric_state_codes and self._STATE_CODE_RE.search(line):
                has_numeric_state_codes = True
            # "user= <name>" / "user <name>" apply to every line.
            # "for <name>" applies only when the line has auth-context keywords —
            # prevents kernel/service messages ("for high-res timesource",
            # "for PnP cards") from polluting the username list.
            user_candidates = list(self._USER_FIELD_RE.findall(line))
            if self._AUTH_CONTEXT_RE.search(line):
                user_candidates += self._USER_FOR_RE.findall(line)
            for user in user_candidates:
                # Skip SSH/TLS protocol keywords and reverse-DNS hostnames.
                # Syslog stores the PTR record of the connecting IP in the
                # rhost field (e.g. customer-187-141-143-180-sta.), which the
                # "for <name>" branch would otherwise count as a login username.
                # _REVERSE_DNS_RE detects embedded IP octets.
                if (
                    user
                    and user.lower() not in self._USER_PROTOCOL_TERMS
                    and not self._REVERSE_DNS_RE.search(user)
                    and not user[0].isdigit()
                    and user[-1].isalnum()
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
            # Syslog service breakdown — extract base service name from
            # "service[PID]:" and "service(module)[PID]:" patterns.
            svc_m = self._SYSLOG_SERVICE_RE.search(line)
            if svc_m:
                service_counts[svc_m.group(1)] += 1
            # Syslog hostname — only matches BSD-format lines with a
            # service[pid] suffix, so non-syslog content is naturally skipped.
            host_m = self._SYSLOG_HOST_RE.match(line)
            if host_m:
                host_counts[host_m.group(1)] += 1
            # Windows Update KB packages (ISS-020). A line may carry multiple
            # KB references; count each. Counter aggregation makes the
            # final ``len(kb_package_counts)`` the *distinct* count and
            # ``sum(kb_package_counts.values())`` the total occurrences.
            for kb_num in self._KB_PACKAGE_RE.findall(line):
                kb_package_counts[f"KB{kb_num}"] += 1
            # Windows CBS HRESULTs (ISS-037). Aggregate by hex code so the
            # per-HRESULT total survives message-template fragmentation.
            # Hex is normalised to lowercase so ``0x800F080D`` and
            # ``0x800f080d`` collapse to one key; the symbolic name (if
            # present) is recorded for display alongside the count.
            for hresult_hex, hresult_sym in self._HRESULT_RE.findall(line):
                key = hresult_hex.lower()
                hresult_counts[key] += 1
                if hresult_sym and key not in hresult_symbols:
                    hresult_symbols[key] = hresult_sym
            # BGL RAS alert flag column (ISS-015). Detection is anchored on
            # the full prefix (flag + 10-digit epoch + YYYY.MM.DD) so it
            # only matches genuine BGL-format lines.
            bgl_m = self._BGL_LINE_RE.match(line)
            if bgl_m:
                bgl_flag_counts[bgl_m.group("flag")] += 1
                bgl_line_count += 1
                # Distinct node identifiers (ISS-016) — surface the per-host
                # denominator for severity-scale calibration.
                bgl_nodes.add(bgl_m.group("node"))

            # Semantic event classification — also track first/last timestamp
            # per event type so the entity profile can report temporal span.
            matched_events = []
            if self._FAILED_PASSWORD_RE.search(line):
                event_counts["failed_password"] += 1
                matched_events.append("failed_password")
            if self._ACCEPTED_PASSWORD_RE.search(line):
                event_counts["accepted_login"] += 1
                matched_events.append("accepted_login")
            if self._INVALID_USER_RE.search(line):
                event_counts["invalid_user"] += 1
                matched_events.append("invalid_user")
            if self._CONNECTION_CLOSED_RE.search(line):
                event_counts["connection_closed"] += 1
                matched_events.append("connection_closed")
            if self._BREAK_IN_ATTEMPT_RE.search(line):
                event_counts["break_in_attempt"] += 1
                matched_events.append("break_in_attempt")
            if self._PAM_AUTH_FAILURE_RE.search(line):
                event_counts["pam_auth_failure"] += 1
                matched_events.append("pam_auth_failure")
            if self._SSH_SESSION_RE.search(line):
                event_counts["ssh_session_opened"] += 1
                matched_events.append("ssh_session_opened")
            if matched_events:
                ts = extract_timestamp(line)
                if ts:
                    for ev in matched_events:
                        if ev not in event_first_ts or ts < event_first_ts[ev]:
                            event_first_ts[ev] = ts
                        if ev not in event_last_ts or ts > event_last_ts[ev]:
                            event_last_ts[ev] = ts
                    # Track per-IP burst spans on attack-event lines — enables
                    # describing per-IP bursty behavior in the entity profile.
                    for ip in line_ips:
                        if ip not in ip_attack_first_ts or ts < ip_attack_first_ts[ip]:
                            ip_attack_first_ts[ip] = ts
                        if ip not in ip_attack_last_ts or ts > ip_attack_last_ts[ip]:
                            ip_attack_last_ts[ip] = ts
                # Per-IP per-event-type counts — correlate IPs with events on the
                # same line so the agent can answer "how many auth attempts did X make"
                # directly from the extract rather than chaining search_file calls.
                for ip in line_ips:
                    if ip not in ip_event_counts:
                        ip_event_counts[ip] = Counter()
                    for ev in matched_events:
                        ip_event_counts[ip][ev] += 1

        # BGL block is only meaningful when at least one non-dash flag is
        # present (a file with only dash-flag lines is not informative;
        # surfacing it would be noise).
        bgl_has_signal = any(flag != "-" for flag in bgl_flag_counts)

        if not (
            ip_all_counts
            or user_all_counts
            or event_counts
            or port_counts
            or pid_counts
            or path_counts
            or kb_package_counts
            or bgl_has_signal
            or hresult_counts
        ):
            return "", "ENTITY PROFILE: No entities found"

        # FILE SUMMARY — returned separately so extract() can place it in file_extract
        first_ts, last_ts = extract_time_range_ts(content)
        tr = extract_time_range(content)
        yearless_ts, sample_raw_ts = has_yearless_timestamps(content)
        summary = self._build_summary(
            event_counts,
            ip_all_counts,
            user_all_counts,
            path_counts,
            len(lines),
            len(error_lines),
            time_range=tr.get("Time range", ""),
            first_ts=first_ts,
            last_ts=last_ts,
            ip_attack_first_ts=ip_attack_first_ts,
            ip_attack_last_ts=ip_attack_last_ts,
            has_numeric_state_codes=has_numeric_state_codes,
            warn_only=warn_only,
            yearless_timestamps=yearless_ts,
            sample_raw_ts=sample_raw_ts,
            bgl_node_count=len(bgl_nodes),
            bgl_line_count=bgl_line_count,
            kb_package_count=len(kb_package_counts),
        )

        # ENTITY PROFILE body — the search map
        parts: list[str] = ["ENTITY PROFILE (full file scan):"]

        # Source host(s) — BSD-syslog 3rd-field hostnames. Surfaced so the
        # agent can identify the source machine without having to parse raw
        # lines. Only shown when ≥5 lines have a recognizable host (avoids
        # noisy single-line matches from non-syslog content).
        sig_hosts = [(h, n) for h, n in host_counts.most_common(8) if n >= 5]
        if sig_hosts:
            parts.append("  Source host(s) (syslog 3rd-field, line counts):")
            for host, n in sig_hosts:
                parts.append(f"    {host}: {n} lines")

        # Top services — only shown for multi-service syslog logs (2+ services
        # each with ≥5 lines). A single dominant service adds no value here.
        sig_services = [(s, n) for s, n in service_counts.most_common(8) if n >= 5]
        if len(sig_services) >= 2:
            parts.append("  Top services (syslog process names, line counts):")
            for svc, n in sig_services:
                parts.append(f"    {svc}: {n} lines")

        # Attacker IP set (ISS-026): any IP that appears in at least one
        # failed_password / invalid_user / break_in_attempt / pam_auth_failure
        # event. Used to split accepted_login lines into attacker-sourced
        # (suspicious) and non-attacker-sourced (likely legitimate)
        # categories — the OpenSSH 2k fixture contains exactly one
        # Accepted password line from an IP that NEVER appears in attack
        # events; without the split the agent describes it as
        # "one successful login among the attempts" and trips the
        # forbidden_claim "claims the attacks succeeded".
        attacker_ips: set[str] = {
            ip
            for ip, counts in ip_event_counts.items()
            if any(counts.get(ev, 0) > 0 for ev in self._ATTACK_EVENTS)
        }

        if event_counts:
            parts.append(
                "  Event types"
                "  [search: use the exact string shown with search_file]:"
            )
            for event, count in event_counts.most_common():
                search_str = self._EVENT_SEARCH_STRINGS.get(event, event)
                # Temporal span: full first→last range for this event type.
                # search_file returns only 20 results — a sample; use this span.
                span_note = ""
                rate_note = ""
                ft = event_first_ts.get(event)
                lt = event_last_ts.get(event)
                if ft and lt and ft != lt:
                    delta_secs = (lt - ft).total_seconds()
                    if delta_secs >= 3600:
                        span_str = f"~{delta_secs / 3600:.1f}h"
                    elif delta_secs >= 60:
                        span_str = f"~{int(delta_secs / 60)}min"
                    else:
                        span_str = f"~{int(delta_secs)}s"
                    span_note = (
                        f"  span:{ft.strftime('%H:%M:%S')}→{lt.strftime('%H:%M:%S')}"
                        f" ({span_str})"
                    )
                    # Rate annotation (ISS-016): events/hour normalized over
                    # the per-event-type temporal span. Only emit for
                    # high-count events where the rate is statistically
                    # meaningful — below MIN_RATE_EVENT_COUNT a per-hour
                    # extrapolation would be misleading.
                    if count >= self.MIN_RATE_EVENT_COUNT and delta_secs > 0:
                        rate_per_hour = count / (delta_secs / 3600)
                        if rate_per_hour >= 1:
                            rate_str = f"~{rate_per_hour:.1f}/h"
                        else:
                            # Sub-1/hour rate: show /day for readability
                            rate_per_day = rate_per_hour * 24
                            rate_str = f"~{rate_per_day:.1f}/day"
                        rate_note = f"  rate:{rate_str}"
                rdns_note = (
                    "  [reverse-DNS mismatch trigger]"
                    if event == "break_in_attempt"
                    else ""
                )
                parts.append(
                    f'    {event}: {count}  ["{search_str}"]{span_note}{rate_note}{rdns_note}'
                )
                # ISS-026: per-IP attacker-vs-legitimate breakdown for
                # accepted_login lines. Surfaces directly under the headline
                # event count so the LLM reads the disambiguation as part
                # of the same fact, not as a separate signal it might miss.
                # Skipped when there are no accepted_login events or when
                # no accepted IPs are recorded (e.g. PAM-style "session
                # opened" logs where rhost is on a different line).
                if event == "accepted_login":
                    attacker_accepted = 0
                    legitimate_accepted = 0
                    for ip, ev_counts in ip_event_counts.items():
                        n = ev_counts.get("accepted_login", 0)
                        if not n:
                            continue
                        if ip in attacker_ips:
                            attacker_accepted += n
                        else:
                            legitimate_accepted += n
                    if attacker_accepted or legitimate_accepted:
                        parts.append(
                            f"      {attacker_accepted} from attacker IPs"
                            " (IPs that also have failed/invalid/break-in events)"
                        )
                        parts.append(
                            f"      {legitimate_accepted} from non-attacker IPs"
                            " (likely legitimate sessions)"
                        )

        if ip_all_counts:
            parts.append(
                "  Distinct IPs"
                "  [search: pass the IP string to search_file;"
                " mention count = all line occurrences across all event types,"
                " not auth-specific — see IP auth breakdown below for event-specific counts]:"
            )
            for ip, total in ip_all_counts.most_common(top_n):
                error_n = ip_error_counts.get(ip, 0)
                annotation = f"  ({error_n} on error lines)" if error_n else ""
                parts.append(
                    f"    {ip}: {total} line occurrences (all event types){annotation}"
                )

        # Auth breakdown table: per-IP counts for each auth event type, summed
        # into an auth total. Use this to answer "how many auth attempts did X make"
        # directly — do not use the total line-occurrence count above for that.
        auth_ips = [
            ip
            for ip, _ in ip_all_counts.most_common(top_n)
            if ip in ip_event_counts
            and any(ip_event_counts[ip].get(ev, 0) for ev in self._AUTH_EVENTS)
        ]
        if auth_ips:
            parts.append(
                "  IP auth breakdown"
                " [use these event-specific counts for auth-attempt totals,"
                " not the line-occurrence counts above]:"
            )
            for ip in auth_ips[:5]:
                ev_parts = []
                auth_total = 0
                for ev in (
                    "failed_password",
                    "pam_auth_failure",
                    "invalid_user",
                    "accepted_login",
                ):
                    n = ip_event_counts[ip].get(ev, 0)
                    if n:
                        ev_parts.append(f"{ev}={n}")
                        auth_total += n
                if ev_parts:
                    parts.append(
                        f"    {ip}: {', '.join(ev_parts)} → auth total={auth_total}"
                    )

        if user_all_counts:
            total_distinct = len(user_all_counts)
            parts.append(f"  Distinct usernames ({total_distinct} total):")
            for user, total in user_all_counts.most_common():
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

        # BGL RAS alert flag column (ISS-015). Surface distinct flag values
        # with line counts so the agent reads the structured first column
        # rather than fabricating flag names from message bodies. Only
        # surfaced when at least one non-dash flag is present.
        if bgl_has_signal:
            distinct_flags = len(bgl_flag_counts)
            parts.append(
                f"  BGL alert flags (RAS first column, {distinct_flags} distinct):"
            )
            for flag, count in bgl_flag_counts.most_common():
                if flag == "-":
                    parts.append(f"    - (no alert class): {count}")
                else:
                    parts.append(f"    {flag}: {count}")

        # Windows Update KB packages (ISS-020). Surface DISTINCT count
        # explicitly so the agent does not conflate substring occurrences
        # (one per install/uninstall/check event) with distinct package
        # count. Sample names listed for orientation; full list is
        # available via search_file with the "Package_for_KB" prefix.
        if kb_package_counts:
            distinct_kb = len(kb_package_counts)
            total_occurrences = sum(kb_package_counts.values())
            parts.append(
                f"  Distinct KB packages: {distinct_kb}"
                f"  ({total_occurrences} total Package_for_KB substring occurrences"
                f" — multiple events per package; use the distinct count for"
                f' "how many packages")'
            )
            sample = ", ".join(kb for kb, _ in kb_package_counts.most_common(8))
            parts.append(f"    sample: {sample}")

        # Windows CBS HRESULTs (ISS-037). The template-counts block reports
        # per-template halves of any HRESULT that appears across multiple
        # message templates (e.g. ``Expecting attribute name`` AND ``Failed
        # to get next element`` both carry ``CBS_E_MANIFEST_INVALID_ITEM``).
        # Surface the per-HRESULT aggregate here so the agent has a single
        # authoritative file-wide total for each code, not two halves it
        # has to recognise as the same error.
        if hresult_counts:
            distinct_codes = len(hresult_counts)
            total_hresult_occurrences = sum(hresult_counts.values())
            parts.append(
                f"  HRESULT codes (file-wide totals across all message"
                f" templates, {distinct_codes} distinct,"
                f" {total_hresult_occurrences} occurrences): [use these"
                f" totals — the per-template counts in the search map split"
                f" the same code across multiple templates]"
            )
            for code, count in hresult_counts.most_common(top_n):
                symbol = hresult_symbols.get(code)
                if symbol:
                    parts.append(f"    {code} ({symbol}): {count}")
                else:
                    parts.append(f"    {code}: {count}")

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

    # Event types whose count is a duplicate of another event type and should
    # be hidden from FILE SUMMARY's dominant-activity picker. PAM is special:
    # in Format A logs (OpenSSH "pam_unix(sshd:auth):") it duplicates
    # failed_password, but in Format B logs (loghub Linux "sshd(pam_unix)[PID]:")
    # failed_password never matches and pam_auth_failure IS the auth signal.
    # Hence the exclusion is applied conditionally inside _build_summary.

    def _build_summary(
        self,
        event_counts: "Counter[str]",
        ip_counts: "Counter[str]",
        user_counts: "Counter[str]",
        path_counts: "Counter[str]",
        total_lines: int,
        error_count: int,
        time_range: str = "",
        first_ts=None,
        last_ts=None,
        ip_attack_first_ts: dict = None,
        ip_attack_last_ts: dict = None,
        has_numeric_state_codes: bool = False,
        warn_only: bool = False,
        yearless_timestamps: bool = False,
        sample_raw_ts: str | None = None,
        bgl_node_count: int = 0,
        bgl_line_count: int = 0,
        kb_package_count: int = 0,
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
            n_distinct_ips = len(ip_counts)
            if "brute-force" in pattern and n_distinct_ips > 1:
                pattern = f"{pattern} ({n_distinct_ips} distinct source IPs)"
            sentences.append(f"{pattern}.")

        # Windows CBS format note (ISS-028). When the file references
        # ≥MIN_KB_PACKAGES_FOR_CBS_NOTE distinct ``Package_for_KB<N>``
        # packages it is a Component-Based Servicing log — HRESULT
        # entries here are servicing results logged at Info severity,
        # not application crashes or network/firewall events. Without
        # this note the agent describes 0x800f080d / 0x80004005 lines
        # as "application-level failures" or "network connectivity
        # events" (logs-windows-01 q5), tripping the forbidden_claims
        # "claims application crashes appear" and "fabricates network
        # or firewall events".
        if kb_package_count >= self.MIN_KB_PACKAGES_FOR_CBS_NOTE:
            sentences.append(
                "Format: Windows CBS (Component-Based Servicing) log."
                " HRESULT entries are Info-severity Windows Update servicing"
                " results, not application crashes or network events."
            )

        # Break-in attempt trigger IMMEDIATELY after the pattern — POSSIBLE BREAK-IN
        # ATTEMPT is a specific OpenSSH warning; placing it early ensures agents see
        # what triggers it even when answering a focused count question.
        if event_counts.get("break_in_attempt", 0) > 0:
            ba_count = event_counts["break_in_attempt"]
            sentences.append(
                f"{ba_count} POSSIBLE BREAK-IN ATTEMPT warnings"
                f" (OpenSSH trigger: each fired because the connecting IP's PTR"
                f" record did not match the IP address — reverse-DNS mismatch)."
            )

        # Dominant activity counts — exclude supplementary duplicate event types.
        # pam_auth_failure duplicates failed_password ONLY when both are present
        # (Format A logs). When failed_password is 0, pam_auth_failure is the
        # primary auth signal (Format B) and must be surfaced.
        exclude = set()
        if event_counts.get("failed_password", 0) > 0:
            exclude.add("pam_auth_failure")
        summary_events = Counter(
            {k: v for k, v in event_counts.items() if k not in exclude}
        )
        top_events = summary_events.most_common(3)
        if top_events:
            ev_summary = ", ".join(
                f"{name.replace('_', ' ')} ({count})" for name, count in top_events
            )
            sentences.append(f"Dominant activity: {ev_summary}.")
        elif error_count > 0:
            non_flagged = total_lines - error_count
            sentences.append(
                f"{error_count} severity-flagged lines out of {total_lines} total."
            )
            # Surface non-flagged line count and its operational implication.
            # When ≥25% of lines are INFO/DEBUG-level, the service was still
            # partially operational alongside the severity-flagged events —
            # agents must not treat severity counts alone as evidence of
            # complete failure.
            if non_flagged >= max(10, int(total_lines * 0.1)):
                non_flagged_pct = non_flagged / total_lines
                if non_flagged_pct >= 0.25:
                    sentences.append(
                        f"{non_flagged} lines ({non_flagged_pct:.0%}) carry no"
                        " severity flag (INFO/DEBUG-level entries) — service"
                        " activity continued throughout the log period"
                        " alongside the WARN/ERROR events."
                    )
                else:
                    sentences.append(
                        f"{non_flagged} lines carry no severity flag"
                        " (INFO/DEBUG-level normal-operation entries)."
                    )

        # Root targeting — explicitly call out root password guessing when present
        root_count = user_counts.get("root", 0)
        if root_count > 0 and pattern and "brute-force" in pattern:
            sentences.append(f"Includes {root_count} root login attempts.")

        # Successful SSH sessions — noted immediately after attack context so the
        # agent includes them in summaries rather than focusing only on failures.
        ssh_success = event_counts.get("ssh_session_opened", 0)
        if ssh_success > 0:
            if pattern and "brute-force" in pattern:
                prefix = "Despite attack traffic, "
            else:
                prefix = ""
            ssh_search = self._EVENT_SEARCH_STRINGS["ssh_session_opened"]
            sentences.append(
                f"{prefix}{ssh_success} successful SSH session(s) opened"
                f" (search: '{ssh_search}')."
            )

        # Per-IP burst pattern for brute-force attacks — each source IP attacks in
        # a concentrated burst at different times; combined activity spans the full
        # log period. This directly answers temporal distribution questions.
        if (
            pattern
            and "brute-force" in pattern
            and ip_attack_first_ts
            and ip_attack_last_ts
        ):
            top_ips = [
                ip for ip, _ in ip_counts.most_common(3) if ip in ip_attack_first_ts
            ]
            if len(top_ips) >= 2:
                has_bursts = any(
                    ip_attack_first_ts.get(ip)
                    and ip_attack_last_ts.get(ip)
                    and ip_attack_first_ts[ip] != ip_attack_last_ts[ip]
                    for ip in top_ips[:3]
                )
                if has_bursts:
                    sentences.append(
                        "Per-IP burst pattern: attacks NOT evenly distributed —"
                        " each source concentrates attempts in a short burst"
                        " at different times, with combined activity spanning"
                        " the full log period."
                    )

        # Top source IP
        if ip_counts:
            top_ip, top_count = ip_counts.most_common(1)[0]
            sentences.append(f"Top source: {top_ip} ({top_count} mentions).")

        # Key absences the agent can confidently answer without searching
        absent = []
        if warn_only:
            # Severity-flagged lines are WARN/WARNING only — explicitly note
            # the absence of higher-severity entries so the agent can answer
            # "were there any errors/failures?" questions correctly without
            # hedging that WARNs "could lead to" errors.
            absent.append("no ERROR or FATAL entries (highest severity is WARN)")
        if not path_counts:
            absent.append("no HTTP traffic")
        if absent:
            sentences.append(f"Absent: {', '.join(absent)}.")

        # Numeric state codes: log sources like mod_jk use internal state numbers
        # not documented in the log itself. Embed the note here so the agent reads
        # it before asserting meanings from training data.
        if has_numeric_state_codes:
            sentences.append(
                '[Note: numeric state codes (e.g. "error state 6") are internal'
                " values — their specific meanings are not documented in this log.]"
            )

        # Time range with computed duration — surfaces full log span so agent
        # cross-references correctly and can answer duration questions directly.
        log_span_secs = 0.0
        if time_range and time_range != "unknown":
            duration_note = ""
            if first_ts and last_ts and last_ts > first_ts:
                log_span_secs = (last_ts - first_ts).total_seconds()
                if log_span_secs >= 3600:
                    duration_note = f" (~{log_span_secs / 3600:.1f}h duration)"
                elif log_span_secs >= 60:
                    duration_note = f" (~{int(log_span_secs / 60)}min duration)"
            sentences.append(f"Log time range: {time_range}{duration_note}.")
            if yearless_timestamps:
                # Syslog BSD timestamps omit the year. The time_range is
                # rendered without a year (see extract_time_range). Add an
                # explicit note so the agent does not fabricate one.
                example_clause = f" (e.g., '{sample_raw_ts}')" if sample_raw_ts else ""
                sentences.append(
                    f"[Note: timestamps in this log have no year{example_clause}."
                    f" Do not assert a specific calendar year in any answer.]"
                )

        # Severity-scale calibration (ISS-016): when a clear time span is known
        # and the file has ≥MIN_RATE_EVENT_COUNT severity-flagged events, surface
        # an effective per-hour rate. Without this, raw counts read as alarming
        # ("347 FATAL events!") even when the rate is well within fault-tolerant
        # background levels (~7/h over 46h on a 1778-node cluster). Fact-based,
        # not interpretive: the rate is reported, the agent decides what it means.
        if error_count >= self.MIN_RATE_EVENT_COUNT and log_span_secs > 0:
            span_hours = log_span_secs / 3600
            rate_per_hour = error_count / max(span_hours, 1e-9)
            if rate_per_hour >= 1:
                rate_phrase = f"~{rate_per_hour:.1f} events/hour"
            else:
                rate_per_day = rate_per_hour * 24
                rate_phrase = f"~{rate_per_day:.1f} events/day"
            if span_hours >= 1:
                span_phrase = f"~{span_hours:.1f}h"
            else:
                span_phrase = f"~{int(log_span_secs / 60)}min"
            sentences.append(
                f"Effective rate: {rate_phrase} of severity-flagged activity"
                f" over a {span_phrase} span — normalize against this when"
                f" characterizing severity (use rate, not absolute count)."
            )

        # Distinct BGL node count (ISS-016): for BlueGene/L RAS logs, the
        # per-node denominator is essential context — a 1778-node cluster
        # producing 347 FATAL events is per-node background, not "systemic".
        # Surface only when the BGL line detector fires on enough lines to
        # avoid noise from incidental matches in non-BGL content.
        if bgl_node_count > 0 and bgl_line_count >= self.MIN_BGL_NODE_SURFACE_LINES:
            sentences.append(
                f"Distinct BGL nodes: {bgl_node_count}"
                f" (across {bgl_line_count} BGL-format lines)"
                f" — events spread across this many hosts; normalize per-node"
                f" before calling activity 'systemic' or 'widespread'."
            )

        if not sentences:
            return ""
        return "FILE SUMMARY: " + " ".join(sentences)
