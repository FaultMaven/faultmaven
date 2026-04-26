"""
Shared utilities for Tier 1 extractors.

Provides:
- Token budget constants
- Input validation (empty/whitespace guard)
- Output truncation (keep beginning + end, truncate middle)
- Coverage metadata formatting and timestamp extraction
"""

import re
from datetime import UTC, datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from faultmaven.models.interfaces import ISanitizer, ITracer, IVectorStore

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
# Coverage metadata
# ---------------------------------------------------------------------------

COVERAGE_SEPARATOR = "\n\n--- COVERAGE METADATA ---\n"


def format_coverage_metadata(**kwargs: object) -> str:
    """Format coverage metadata as key-value pairs after the separator.

    Keys with ``None`` values are omitted.  All other values are converted
    to strings via ``str()``.
    """
    lines = [f"{key}: {value}" for key, value in kwargs.items() if value is not None]
    return COVERAGE_SEPARATOR + "\n".join(lines)


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


def extract_timestamp(line: str) -> datetime | None:
    """Extract the first recognisable timestamp from *line*.

    Supports ISO-8601 (with/without ``T``), syslog BSD, epoch seconds
    and epoch milliseconds.  Returns ``None`` when no pattern matches.
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
                return dt
            if name == "iso8601":
                return datetime.strptime(m.group(0), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=UTC
                )
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
                    return datetime(int(year_str), month, day, hh, mm, ss, tzinfo=UTC)

                now = datetime.now(tz=UTC)
                dt = datetime(now.year, month, day, hh, mm, ss, tzinfo=UTC)
                if dt > now:
                    dt = dt.replace(year=now.year - 1)
                return dt
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
                return datetime(year, mo, dd, hh, mm, ss, tzinfo=UTC)
            if name in ("epoch_ms", "epoch_s"):
                val = int(m.group(1))
                secs = val / 1000 if name == "epoch_ms" else val
                dt = datetime.fromtimestamp(secs, tz=UTC)
                if 2000 <= dt.year <= 2100:
                    return dt
                continue
        except (ValueError, OSError, OverflowError):
            continue
    return None


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
) -> tuple[Optional["datetime"], Optional["datetime"]]:
    """Return ``(start_ts, end_ts)`` datetime objects from *content*.

    Scans only the first 10 and last 10 lines for performance. Returns
    ``(None, None)`` when no timestamps are found, ``(ts, None)`` when
    only the head has a timestamp, ``(None, None)`` when only the tail
    does (since a meaningful time range needs both bounds).

    Phase 3 — promoted from an internal helper so PreprocessingService
    can populate ``evidence.coverage_start_ts`` / ``coverage_end_ts``
    without having to re-parse the strings emitted by
    ``extract_time_range``.
    """
    lines = content.split("\n")
    head = lines[:10]
    tail = lines[-10:] if len(lines) > 10 else []

    first_ts: datetime | None = None
    for line in head:
        first_ts = extract_timestamp(line)
        if first_ts:
            break

    last_ts: datetime | None = None
    for line in reversed(tail):
        last_ts = extract_timestamp(line)
        if last_ts:
            break

    # Meaningful coverage span requires both bounds. A single head
    # timestamp without a tail bound collapses to (ts, None) — the
    # Phase 3 repository query handles this by treating single-point
    # evidence as covering [ts, ts]; callers who need a strict range
    # check the end_ts for None.
    return first_ts, last_ts


def extract_time_range(content: str) -> dict[str, str]:
    """Return ``{"Time range": "<start> to <end>"}`` from content.

    Thin string-formatting wrapper around ``extract_time_range_ts`` —
    kept for backward-compatibility with extractors that embed the
    range in their coverage metadata text block. New code that needs
    the timestamps themselves should call ``extract_time_range_ts``.
    """
    first_ts, last_ts = extract_time_range_ts(content)

    if first_ts and last_ts:
        fmt = "%Y-%m-%d %H:%M:%S"
        return {"Time range": f"{first_ts.strftime(fmt)} to {last_ts.strftime(fmt)}"}
    if first_ts:
        return {"Time range": first_ts.strftime("%Y-%m-%d %H:%M:%S")}
    return {"Time range": "unknown"}
