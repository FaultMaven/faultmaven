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
_TS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("iso8601_t", re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")),
    ("iso8601", re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")),
    (
        "syslog_bsd",
        re.compile(r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}"),
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
                parts = m.group(0).split()
                month = _SYSLOG_MONTHS.get(parts[0], 1)
                day = int(parts[1])
                time_parts = parts[2].split(":")

                year = datetime.now(tz=UTC).year
                dt = datetime(
                    year,
                    month,
                    day,
                    int(time_parts[0]),
                    int(time_parts[1]),
                    int(time_parts[2]),
                    tzinfo=UTC,
                )
                if dt > datetime.now(tz=UTC):
                    dt = dt.replace(year=year - 1)
                return dt
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


def extract_time_range(content: str) -> dict[str, str]:
    """Return ``{"Time range": "<start> to <end>"}`` from content.

    Scans only the first 10 and last 10 lines for performance.
    Returns ``{"Time range": "unknown"}`` when no timestamps are found.
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

    if first_ts and last_ts:
        fmt = "%Y-%m-%d %H:%M:%S"
        return {"Time range": f"{first_ts.strftime(fmt)} to {last_ts.strftime(fmt)}"}
    if first_ts:
        return {"Time range": first_ts.strftime("%Y-%m-%d %H:%M:%S")}
    return {"Time range": "unknown"}
