"""
Shared utilities for Tier 1 extractors.

Provides:
- Token budget constants
- Input validation (empty/whitespace guard)
- Output truncation (keep beginning + end, truncate middle)
- Coverage metadata formatting and timestamp extraction
"""

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    pass

# Token budget: approximate maximum output size for any extractor.
# Individual extractors may set lower budgets but should never exceed this.
MAX_STRUCTURAL_INDEX_TOKENS = 2500
MAX_STRUCTURAL_INDEX_CHARS = (
    MAX_STRUCTURAL_INDEX_TOKENS * 4
)  # ~10000 chars at 4 chars/token

# Standardized response for empty/degenerate input
EMPTY_CONTENT_RESPONSE = "[No content to analyze]"

# Minimum content length to attempt extraction
_MIN_CONTENT_LENGTH = 10

# Head/tail scan windows for ``extract_time_range_ts``. The tail window is
# walked backwards line-by-line until a parseable timestamp is found, so a
# wider window only matters when a file has many trailing blank lines —
# ISS-036 widened it from 10 to 100 so trailing whitespace cannot mask the
# end-of-range value while keeping the scan cost bounded.
_HEAD_SCAN_LINES = 10
_TAIL_SCAN_LINES = 100


def has_content(content: str) -> bool:
    """Check if content is non-empty and worth analyzing.

    Returns False for empty strings, whitespace-only strings,
    and strings shorter than 10 characters after stripping.
    """
    if not content:
        return False
    stripped = content.strip()
    return len(stripped) >= _MIN_CONTENT_LENGTH


def truncate_output(text: str, max_chars: int = MAX_STRUCTURAL_INDEX_CHARS) -> str:
    """Truncate text to fit within a character budget.

    Strategy: keep the first 40% and last 40% of the budget,
    insert a marker in the middle showing how much was removed.
    This preserves context from both the beginning (headers, first
    findings) and end (summaries, final results) of extractor output.

    Args:
        text: The output text to truncate.
        max_chars: Maximum character budget (default: MAX_STRUCTURAL_INDEX_CHARS).

    Returns:
        The text unchanged if within budget, or truncated with a marker.
    """
    if len(text) <= max_chars:
        return text

    keep_start = int(max_chars * 0.4)
    keep_end = int(max_chars * 0.4)
    removed = len(text) - keep_start - keep_end

    marker = f"\n\n... [Truncated {removed} characters for context budget] ...\n\n"

    return text[:keep_start] + marker + text[-keep_end:]


# ---------------------------------------------------------------------------
# Timestamp extraction (for coverage time-range detection)
# ---------------------------------------------------------------------------

# Compiled once at import time — order matters (most specific first).
#
# The BSD-syslog family covers a spectrum of formats that share the same
# "Mon DD HH:MM:SS" core but differ in whether a day-of-week prefix and/or
# a 4-digit year are present. Rather than enumerate each variant as a
# separate pattern (which silently discards information), one pattern
# captures the optional prefix and suffix as named groups and the parser
# uses whichever parts were present.
_TS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("iso8601_t", re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")),
    ("iso8601", re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")),
    # Android HealthApp / similar mobile-app logs (ISS-017):
    #   YYYYMMDD-HH:MM:SS:mmm  e.g. "20171223-22:15:29:606" or
    #   "20171224-1:2:35:789" (non-zero-padded hour after midnight).
    # Listed before yymmdd/epoch_s so it captures the full timestamp
    # rather than letting a unix-epoch substring in the message body win.
    # Strict line-anchored format — must be at start of line followed by
    # a pipe ``|`` field separator, which is the HealthApp delimiter.
    (
        "healthapp",
        re.compile(
            r"^(\d{4})(\d{2})(\d{2})-"  # YYYYMMDD-
            r"(\d{1,2}):(\d{1,2}):(\d{1,2}):(\d{1,3})"  # H:M:S:ms (1-2 digits each)
            r"\|"  # pipe field separator
        ),
    ),
    (
        "syslog_bsd",
        re.compile(
            r"(?:[A-Z][a-z]{2}\s+)?"  # optional day-of-week prefix
            r"(?P<month>[A-Z][a-z]{2})\s+"  # month abbreviation
            r"(?P<day>\d{1,2})\s+"  # day-of-month
            r"(?P<time>\d{2}:\d{2}:\d{2})"  # HH:MM:SS
            r"(?:\s+(?P<year>\d{4}))?"  # optional explicit year
        ),
    ),
    # Compact numeric timestamp used by HDFS and some Hadoop ecosystem logs:
    # YYMMDD HHMMSS — e.g. "081109 203615" = 2008-11-09 20:36:15.
    # Pattern must be strict enough to avoid matching random 6-digit pairs;
    # the parsed values are range-validated in the handler.
    (
        "yymmdd",
        re.compile(r"\b(\d{2})(\d{2})(\d{2}) (\d{2})(\d{2})(\d{2})\b"),
    ),
    # Spark / log4j compact form: YY/MM/DD HH:MM:SS
    #   e.g. "17/06/09 20:10:40 INFO executor.Executor: ..."
    # ISS-027: previously fell through to epoch_s and similar fallback
    # patterns, which never matched the 8-digit numeric prefix, leaving
    # Spark logs with time_range="unknown" and the agent inferring the
    # span from later embedded HH:MM:SS fragments — clipping by ~5s on
    # both ends.
    (
        "yy_slash_mmdd",
        re.compile(
            r"\b(\d{2})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})\b",
        ),
    ),
    ("epoch_ms", re.compile(r"\b([12]\d{12})\b")),
    ("epoch_s", re.compile(r"\b([12]\d{9})\b")),
]

_SYSLOG_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def _extract_timestamp_with_source(line: str) -> tuple[datetime | None, str | None]:
    """Extract the first recognisable timestamp and its pattern name from *line*.

    Returns ``(dt, pattern_name)`` on success, ``(None, None)`` on no match.
    Pattern names match the keys in ``_TS_PATTERNS`` and the documented
    vocabulary of ``CoverageMetadata.source``.
    """
    for name, pat in _TS_PATTERNS:
        m = pat.search(line)
        if not m:
            continue
        try:
            if name == "iso8601_t":
                dt = datetime.fromisoformat(m.group(0))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt, name
            if name == "iso8601":
                return (
                    datetime.strptime(m.group(0), "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=UTC
                    ),
                    name,
                )
            if name == "healthapp":
                # YYYYMMDD-H:M:S:ms (HealthApp). All seven groups are
                # range-validated; ms is dropped (datetime requires
                # microseconds, not milliseconds, and the precision is
                # not load-bearing for time-range computation).
                yyyy, mo, dd, hh, mi, ss, _ms = (int(x) for x in m.groups())
                if not (
                    1970 <= yyyy <= 2100
                    and 1 <= mo <= 12
                    and 1 <= dd <= 31
                    and 0 <= hh <= 23
                    and 0 <= mi <= 59
                    and 0 <= ss <= 59
                ):
                    continue
                return datetime(yyyy, mo, dd, hh, mi, ss, tzinfo=UTC), name
            if name == "syslog_bsd":
                # Single parser for the full BSD-syslog family. Use the
                # explicit year when the input provides one; fall back to the
                # "now or previous year" heuristic only when the year is
                # genuinely absent from the log line.
                month = _SYSLOG_MONTHS.get(m.group("month"), 1)
                day = int(m.group("day"))
                hh, mm, ss = (int(x) for x in m.group("time").split(":"))
                year_str = m.group("year")
                if year_str is not None:
                    return (
                        datetime(int(year_str), month, day, hh, mm, ss, tzinfo=UTC),
                        name,
                    )

                # No year in the line, so one is invented. Report that under a
                # DIFFERENT source name: the instant is a guess about which year
                # this "Mon DD HH:MM:SS" belongs to, and a consumer that states
                # it as an absolute observation time would be asserting the
                # guess. ``extract_time_range`` already refuses to print the
                # year for this reason; naming it here lets every other consumer
                # make the same distinction instead of re-deriving it.
                now = datetime.now(tz=UTC)
                dt = datetime(now.year, month, day, hh, mm, ss, tzinfo=UTC)
                if dt > now:
                    dt = dt.replace(year=now.year - 1)
                return dt, "syslog_bsd_noyear"
            if name == "yymmdd":
                yy, mo, dd, hh, mm, ss = (int(x) for x in m.groups())
                year = 2000 + yy
                if not (
                    1 <= mo <= 12
                    and 1 <= dd <= 31
                    and 0 <= hh <= 23
                    and 0 <= mm <= 59
                    and 0 <= ss <= 59
                ):
                    continue
                return datetime(year, mo, dd, hh, mm, ss, tzinfo=UTC), name
            if name == "yy_slash_mmdd":
                yy, mo, dd, hh, mm, ss = (int(x) for x in m.groups())
                # Two-digit year: 00-69 → 2000s, 70-99 → 1900s. Spark and
                # most modern log4j configs emit this format with current
                # decade values, so the cutoff lands well in the past.
                year = 2000 + yy if yy <= 69 else 1900 + yy
                if not (
                    1 <= mo <= 12
                    and 1 <= dd <= 31
                    and 0 <= hh <= 23
                    and 0 <= mm <= 59
                    and 0 <= ss <= 59
                ):
                    continue
                return datetime(year, mo, dd, hh, mm, ss, tzinfo=UTC), name
            if name in ("epoch_ms", "epoch_s"):
                val = int(m.group(1))
                secs = val / 1000 if name == "epoch_ms" else val
                dt = datetime.fromtimestamp(secs, tz=UTC)
                if 2000 <= dt.year <= 2100:
                    return dt, name
                continue
        except (ValueError, OSError, OverflowError):
            continue
    return None, None


def extract_timestamp(line: str) -> datetime | None:
    """Extract the first recognisable timestamp from *line*.

    Supports ISO-8601 (with/without ``T``), syslog BSD, epoch seconds,
    epoch milliseconds, and the compact log4j/Hadoop variants. Returns
    ``None`` when no pattern matches. Callers that also need the
    matched pattern name should call ``_extract_timestamp_with_source``.
    """
    dt, _ = _extract_timestamp_with_source(line)
    return dt


def has_yearless_timestamps(content: str) -> tuple[bool, str | None]:
    """Return (is_yearless, sample_raw_timestamp) when leading timestamps are syslog BSD without year.

    Checks only the first 20 non-empty lines for speed. Returns (False, None) when no
    syslog_bsd timestamps are found (the file may use a format that always
    includes a year, or no recognisable timestamps at all).
    """
    syslog_pat = next(pat for name, pat in _TS_PATTERNS if name == "syslog_bsd")
    for line in content.split("\n")[:20]:
        m = syslog_pat.search(line)
        if m:
            is_yearless = m.group("year") is None
            return is_yearless, (m.group(0).strip() if is_yearless else None)
    return False, None


def extract_time_range_ts(
    content: str,
) -> tuple[Optional["datetime"], Optional["datetime"], Optional[str]]:
    """Return ``(start_ts, end_ts, source)`` from *content*.

    Scans the first ``_HEAD_SCAN_LINES`` lines and walks backwards through
    the trailing ``_TAIL_SCAN_LINES`` lines, returning the first parseable
    timestamp from each end. Walking backwards (rather than capping at the
    last 10 lines) means a small amount of trailing blank/whitespace
    content does not collapse the end-of-range value to ``None`` —
    real-world log files re-emitted by tooling routinely carry a few
    trailing newlines, and HealthApp ``YYYYMMDD-H:M:S:ms`` files in
    particular surface their last timestamp via FILE SUMMARY (ISS-036).
    Files shorter than the tail window are scanned end-to-start.

    Phase 3 — promoted from an internal helper so PreprocessingService
    can populate ``evidence.coverage_start_ts`` / ``coverage_end_ts``
    without having to re-parse the strings emitted by
    ``extract_time_range``.

    ``source`` is the name of the ``_TS_PATTERNS`` entry that matched the
    head timestamp (one of ``iso8601_t``, ``iso8601``, ``healthapp``,
    ``syslog_bsd``, ``yymmdd``, ``yy_slash_mmdd``, ``epoch_ms``,
    ``epoch_s``), or ``None`` when no pattern matched. The head wins the
    label because a file usually emits a single timestamp format; the
    tail pattern is captured for diagnostics but only the head value is
    returned. See ``CoverageMetadata.source`` in
    ``core/preprocessing/evidence_metadata.py``.
    """
    lines = content.split("\n")
    head = lines[:_HEAD_SCAN_LINES]

    first_ts: datetime | None = None
    source: str | None = None
    head_match_idx: int | None = None
    for i, line in enumerate(head):
        ts, src = _extract_timestamp_with_source(line)
        if ts:
            first_ts = ts
            source = src
            head_match_idx = i
            break

    # Walk backwards through the trailing window so trailing blank lines
    # do not mask the actual final timestamp. ISS-036: a fixed
    # ``lines[-10:]`` slice returned ``None`` whenever the last 10 lines
    # were non-timestamped, even when the real final line sat at index
    # ``[-11]``. The wider scan keeps short files (<10 lines) behaving
    # correctly too — the previous ``len(lines) > 10`` guard returned an
    # empty tail for any file under 10 lines and so left ``last_ts``
    # always ``None``.
    #
    # The tail scan stops one line past the head's matched index so that
    # a single-timestamped-line file does not double-count itself as both
    # start and end. The previous fixed-window implementation achieved
    # this implicitly via the disjoint head/tail slicing; with
    # walk-backward semantics we need an explicit floor.
    tail_floor = (head_match_idx + 1) if head_match_idx is not None else 0
    tail_start = max(tail_floor, len(lines) - _TAIL_SCAN_LINES)
    last_ts: datetime | None = None
    for line in reversed(lines[tail_start:]):
        last_ts = extract_timestamp(line)
        if last_ts:
            # If the head never matched, fall back to the tail's pattern
            # so a single-bounded file still gets a ``source`` value.
            if source is None:
                _, tail_src = _extract_timestamp_with_source(line)
                source = tail_src
            break

    # Meaningful coverage span requires both bounds. A single head
    # timestamp without a tail bound collapses to (ts, None, source) —
    # the Phase 3 repository query handles this by treating single-point
    # evidence as covering [ts, ts]; callers who need a strict range
    # check the end_ts for None.
    return first_ts, last_ts, source


def extract_time_range(content: str) -> dict[str, str]:
    """Return ``{"Time range": "<start> to <end>"}`` from content.

    String-formatting helper used by extractors that embed the range in
    their coverage-metadata text block. Callers that need the
    ``datetime`` objects or the matched pattern name should call
    ``extract_time_range_ts`` directly.

    When the source has yearless syslog BSD timestamps (e.g.,
    ``"Jun 14 15:16:01"``) we omit the year from the output. The
    underlying ``datetime`` objects do carry a synthetic year (the
    parser uses ``datetime.now().year`` as a fallback), but emitting
    that year as an ISO date misleads downstream consumers — the LLM
    treats the formatted string as a known fact even when accompanied
    by a hedge. Better to render the format that the source actually
    has.
    """
    first_ts, last_ts, _ = extract_time_range_ts(content)
    yearless, _ = has_yearless_timestamps(content)
    fmt = "%b %d %H:%M:%S" if yearless else "%Y-%m-%d %H:%M:%S"

    if first_ts and last_ts:
        return {"Time range": f"{first_ts.strftime(fmt)} to {last_ts.strftime(fmt)}"}
    if first_ts:
        return {"Time range": first_ts.strftime(fmt)}
    return {"Time range": "unknown"}
